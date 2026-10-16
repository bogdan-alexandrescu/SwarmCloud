"""The HTTP side: the standard library's threading server around Receiver.

Cloud Run terminates TLS in front of it. One request per connection: every
answer carries `Connection: close`, so a refused request's unread body can
never be read as a second request.

What the standard library does that must NOT happen here, and is overridden
(swarm_typesafe_fwd/server.py does the same, for the same reasons):

  * an unknown method is 501 from `BaseHTTPRequestHandler`: here every method
    reaches Receiver.handle, which answers 404;
  * `send_error` writes an HTML page quoting the request line: here a request
    that cannot be parsed is a bare 400;
  * `log_message` writes the request line to stderr: here it writes nothing,
    and Receiver logs its own line;
  * a `Server` header naming the Python version: not sent.
"""

from __future__ import annotations

import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .receiver import Receiver

#: How long a connection may take to send its request head and body.
REQUEST_TIMEOUT_SECONDS = 30


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "swarm-github-hooks"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS
    receiver: Receiver

    def __getattr__(self, name: str) -> Any:
        # Every `do_<METHOD>` the standard library looks for is the one
        # handler, so no method is ever 501 and none skips the checks.
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        try:
            self.send_response_only(400)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass
        self.close_connection = True

    def _read(self, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except (OSError, socket.timeout):
            pass
        return b"".join(chunks)

    def _handle(self) -> None:
        self.close_connection = True
        reply = self.receiver.handle(self.command, self.path, list(self.headers.items()), self._read)
        body = reply.body
        try:
            self.send_response_only(reply.status)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass


class HooksServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback, which may quote request data.
        return


def make_server(receiver: Receiver, host: str, port: int) -> HooksServer:
    handler = type("Handler", (_Handler,), {"receiver": receiver})
    return HooksServer((host, port), handler)


__all__ = ["HooksServer", "make_server"]
