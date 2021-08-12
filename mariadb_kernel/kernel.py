"""The MariaDB kernel"""
# Copyright (c) MariaDB Foundation.
# Distributed under the terms of the Modified BSD License.

from ipykernel.kernelbase import Kernel
from prompt_toolkit.document import Document

from ._version import version as __version__
from mariadb_kernel.client_config import ClientConfig
from mariadb_kernel.mariadb_client import (
    MariaDBClient,
    ServerIsDownError,
)
from mariadb_kernel.code_parser import CodeParser
from mariadb_kernel.mariadb_server import MariaDBServer
from mariadb_kernel.maria_magics.maria_magic import MariaMagic
from mariadb_kernel.autocompleter import Autocompleter
from mariadb_kernel.introspection_provider import IntrospectionProvider

import logging
import pexpect
import re
import pandas
from bs4 import BeautifulSoup
import os
import signal

_EXPERIMENTAL_KEY_NAME = "_jupyter_types_experimental"


class MariadbClientManagager:
    client_for_code_block: MariaDBClient
    client_for_autocompleter: MariaDBClient

    def __init__(self, log: logging.Logger, client_config: ClientConfig) -> None:
        self.client_for_code_block = MariaDBClient(log, client_config)
        self.client_for_autocompleter = MariaDBClient(log, client_config)

    def start(self):
        self.client_for_code_block.start()
        self.client_for_autocompleter.start()

    def stop(self):
        self.client_for_code_block.stop()
        self.client_for_autocompleter.stop()


class MariaDBKernel(Kernel):
    implementation = "MariaDB"
    implementation_version = __version__
    language_info = {
        "name": "SQL",
        "mimetype": "text/plain",
        "file_extension": ".sql",
    }
    banner = "MariaDB kernel"

    def __init__(self, **kwargs):
        Kernel.__init__(self, **kwargs)
        self.delimiter = ";"
        self.client_config = ClientConfig(self.log)
        self.mariadb_client_manager = MariadbClientManagager(
            self.log, self.client_config
        )
        self.mariadb_client = self.mariadb_client_manager.client_for_code_block
        self.mariadb_server = None
        self.data = {"last_select": pandas.DataFrame([])}

        if self.client_config.debug_logging():
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.INFO)

        try:
            self.mariadb_client_manager.start()
        except ServerIsDownError:
            if not self.client_config.start_server():
                self.log.error(
                    "The options passed through mariadb_kernel.json "
                    "prevent the kernel from starting a testing "
                    "MariaDB server instance"
                )
                raise

            # Start a single MariaDB server for a better experience
            # if user wants to quickly test the kernel
            self.mariadb_server = MariaDBServer(self.log, self.client_config)
            self.mariadb_server.start()

            # Reconnect the client now that the server is up
            if self.mariadb_server.is_up():
                self.mariadb_client_manager.start()

        self.autocompleter = Autocompleter(
            self.mariadb_client_manager.client_for_autocompleter,
            self.mariadb_client_manager.client_for_code_block,
            self.log,
        )

    def get_delimiter(self):
        return self.delimiter

    def set_delimiter(self, delimiter):
        self.mariadb_client.run_statement(f"delimiter {delimiter}")
        self.delimiter = delimiter

    def _execute_magics(self, magics):
        for magic in magics:
            magic.execute(self, self.data)

    def _update_data(self, result_html):
        if not result_html or not result_html.startswith("<TABLE"):
            return

        df = pandas.read_html(result_html)
        self.data["last_select"] = df[0]

    def _styled_result(self, result_html):
        if not result_html or not result_html.startswith("<TABLE"):
            return result_html

        soup = BeautifulSoup(result_html)
        cells = soup.find_all(["td", "th"])
        for cell in cells:
            cell["style"] = "text-align:left;white-space:pre"

        return str(soup)

    def _send_message(self, stream, message):
        error = {"name": stream, "text": message + "\n"}
        self.send_response(self.iopub_socket, "stream", error)

    def do_execute(
        self, code, silent, store_history=True, user_expressions=None, allow_stdin=False
    ):
        rv = {
            "status": "ok",
            # The base class increments the execution count
            "execution_count": self.execution_count,
            "payload": [],
            "user_expressions": {},
        }

        try:
            parser = CodeParser(self.log, code, self.delimiter)
        except ValueError as e:
            self._send_message("stderr", str(e))
            return rv

        statements = parser.get_sql()
        for s in statements:
            result = self.mariadb_client.run_statement(s)

            if self.mariadb_client.iserror():
                self._send_message("stderr", self.mariadb_client.error_message())
                continue

            self._update_data(result)
            result_str = str(result)
            if not silent:
                display_content = {
                    "data": {"text/html": self._styled_result(result_str)},
                    "metadata": {},
                }
                self.send_response(self.iopub_socket, "display_data", display_content)

        self._execute_magics(parser.get_magics())
        if self.autocompleter:
            self.autocompleter.refresh(False)
        return rv

    def num_connected_clients(self):
        sql_query = "show status like 'Threads_connected';"
        # If there is any errors we raise
        # (not much we can do if there is a error)
        result_html = self.mariadb_client.run_statement(code=sql_query)
        if self.mariadb_client.iserror():
            raise Exception(f"Client returned an error : {result_html}")
        try:
            df = pandas.read_html(result_html)
            num_clients = int(df[0]["Value"][0])
        except Exception:
            self.log.error(f"Pandas failed to parse html : {result_html}")
            raise

        return num_clients

    def kill_server(self):
        if self.mariadb_server and self.mariadb_server.is_up():
            self.log.info(f"Stopping (own) MariaDB server")
            self.mariadb_server.stop()
        elif not self.mariadb_server:
            pidfile = self.client_config.get_server_pidfile()
            try:
                with open(pidfile, "r") as f:
                    pid = int(f.read())
            except Exception as e:
                self.log.error(f"Failed reading pid file {pidfile}, error: {e}")
                return

            if pid:
                self.log.info(
                    f"Sending stop signal to MariaDB server started by another kernel {pid}"
                )
                os.kill(pid, signal.SIGQUIT)

    def do_shutdown(self, restart):
        num_clients = None
        if self.client_config.start_server():
            try:
                num_clients = self.num_connected_clients()
            except Exception as e:
                self.log.error(
                    f"Failed querying server (of number of clients connected), error: {e}"
                )

        self.mariadb_client_manager.stop()

        if num_clients is not None and num_clients <= 1:
            self.log.info("No more clients connected to server")
            self.kill_server()

    def do_complete(self, code, cursor_pos):
        if hasattr(self, "autocompleter") is False:
            self.log.info("No autocompleter, create one")
            self.autocompleter = Autocompleter(
                self.mariadb_client_manager.client_for_autocompleter,
                self.mariadb_client_manager.client_for_code_block,
                self.log,
            )
        completion_list = self.autocompleter.get_suggestions(code, cursor_pos)
        match_text_list = [completion.text for completion in completion_list]
        self.log.info(f"match_text_list: {match_text_list}")
        offset = 0
        if len(completion_list) > 0:
            offset = completion_list[
                0
            ].start_position  # if match part is 'sel', then start_position would be -3
        type_dict_list = []
        for completion in completion_list:
            if completion.display_meta is not None:
                type_dict_list.append(
                    dict(
                        start=completion.start_position,
                        end=len(completion.text) + completion.start_position,
                        text=completion.text,
                        type=completion.display_meta_text,  # display_meta is FormattedText object
                    )
                )
        return {
            "status": "ok",
            "matches": match_text_list,
            "cursor_start": cursor_pos + offset,
            "cursor_end": cursor_pos,
            "metadata": {_EXPERIMENTAL_KEY_NAME: type_dict_list},
        }

    def do_inspect(self, code, cursor_pos, detail_level):
        introspection_provider = IntrospectionProvider()
        result_html = (
            introspection_provider.get_introspection_explain_html(
                Document(code, int(cursor_pos)), self.autocompleter
            )
            or ""
        )
        return {
            "status": "ok",
            "data": {"text/html": result_html},
            "metadata": {},
            "found": True,
        }
