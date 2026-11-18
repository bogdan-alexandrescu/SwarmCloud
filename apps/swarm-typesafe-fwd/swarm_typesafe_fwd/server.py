"""The HTTP side: the standard library's threading server around Forwarder.

Plain HTTP on 8080 inside the VPC (the ticket is the only credential on that
hop, and it is one attempt's for minutes; the NetworkPolicy admits only
labelled tenant pods). One request per connection: every answer carries
`Connection: close`, so a refused request's unread body can never be read as
a second request.

What the standard library does that must NOT happen here, and is overridden:

  * an unknown method is 501 from `BaseHTTPRequestHandler`: here every method
    reaches Forwarder.handle, which answers 404 `not_found`;
  * `send_error` writes an HTML page quoting the request line: here a request
    that cannot be parsed is 400 `bad_request` with no echo;
  * `log_message` writes the request line (and so any query string) to
    stderr: here it writes nothing, and Forwarder logs its own redacted line;
  * a `Server` header naming the Python version: not sent.

A forwarded answer is written with chunked transfer encoding as it arrives.
If the upstream breaks off mid-body the connection is dropped without the
final chunk, so the caller sees a truncated answer, never a complete-looking
one.
"""

from __future__ import annotations

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .forwarder import Forwarder, Reply
from .upstream import UpstreamFailed, UpstreamTimeout

#: How long a connection may take to send its request head and body.
REQUEST_TIMEOUT_SECONDS = 30


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "swarm-typesafe-fwd"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS
    forwarder: Forwarder

    def __getattr__(self, name: str) -> Any:
        # Every `do_<METHOD>` the standard library looks for is the one
        # handler, so no method is ever 501 and none skips the checks.
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        body = json.dumps({"error": "bad_request"}).encode()
        try:
            self.send_response_only(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Swarm-Typesafe-Refused", "bad_request")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    def _read(self, length: int) -> bytes:
        try:
            return self.rfile.read(length)
        except (OSError, socket.timeout):
            return b""

    def _handle(self) -> None:
        self.close_connection = True
        reply = self.forwarder.handle(self.command, self.path, list(self.headers.items()),
                                      self._read)
        self._write(reply)

    def _write(self, reply: Reply) -> None:
        streamed = reply.code is None
        self.send_response_only(reply.status)
        for name, value in reply.headers:
            self.send_header(name, value)
        if streamed:
            self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        written, cause, finished = 0, None, False
        try:
            self.end_headers()
            for chunk in reply.chunks:
                if not chunk:
                    continue
                if streamed:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
                written += len(chunk)
            if streamed:
                # The log line BEFORE the last chunk: once the caller has the
                # whole answer, its line already exists, so a reader of the
                # log (or a test) never races the end of the body.
                finished = True
                reply.finish(written, None)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except UpstreamTimeout:
            cause = "upstream_timeout"
        except UpstreamFailed:
            cause = "upstream_failed"
        except OSError:
            cause = "client_gone"
        except Exception:  # noqa: BLE001 - never a traceback quoting the body
            cause = "upstream_failed"
        finally:
            close = getattr(reply.chunks, "close", None)
            if callable(close):
                close()
            if not finished:
                reply.finish(written, cause)


class FwdServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback, which may quote request data.
        return


def make_server(forwarder: Forwarder, host: str, port: int) -> FwdServer:
    handler = type("Handler", (_Handler,), {"forwarder": forwarder})
    return FwdServer((host, port), handler)


__all__ = ["FwdServer", "make_server"]
