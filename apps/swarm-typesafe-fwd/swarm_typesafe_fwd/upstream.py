"""The one upstream: https://api.typesafe.ai/v1/systemone (docs/design/workspace-secrets.md §3.5).

A CONSTANT, never derived from the request: not its Host, not its path, not a
ticket claim. The forwarder therefore cannot be pointed anywhere else, which
is what keeps it from being an SSRF path into the VPC or the metadata server.
TLS is verified (`ssl.create_default_context()`: the system CA bundle the
image's ca-certificates installs, and hostname checking).

The answer is STREAMED: chunks are handed to the server as they arrive, under
an overall deadline of `max_response_seconds` as well as the per-read socket
timeout, so a slow upstream never holds a thread past the setting.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping, Protocol

UPSTREAM_HOST = "api.typesafe.ai"
UPSTREAM_PORT = 443
UPSTREAM_PATH = "/v1/systemone"
UPSTREAM_URL = f"https://{UPSTREAM_HOST}{UPSTREAM_PATH}"
CHUNK = 65536


class UpstreamTimeout(Exception):
    """TypeSafe did not answer within max_response_seconds."""


class UpstreamFailed(Exception):
    """TypeSafe could not be reached or broke off. The message is a cause
    word, never anything the upstream sent."""


@dataclass
class UpstreamResponse:
    status: int
    content_type: str
    chunks: Iterator[bytes]
    close: Callable[[], None]


class Upstream(Protocol):
    def send(self, body: bytes, headers: Mapping[str, str],
             timeout: float) -> UpstreamResponse: ...


class HttpsUpstream:
    """POST to UPSTREAM_URL over verified TLS, one connection per request."""

    def __init__(self, *, context: ssl.SSLContext | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._context = context or ssl.create_default_context()
        self._clock = clock

    def send(self, body: bytes, headers: Mapping[str, str], timeout: float) -> UpstreamResponse:
        deadline = self._clock() + timeout
        conn = http.client.HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=timeout,
                                           context=self._context)
        try:
            conn.request("POST", UPSTREAM_PATH, body=body, headers=dict(headers))
            response = conn.getresponse()
        except (TimeoutError, socket.timeout):
            conn.close()
            raise UpstreamTimeout("timeout") from None
        except (OSError, http.client.HTTPException):
            conn.close()
            raise UpstreamFailed("unreachable") from None

        def chunks() -> Iterator[bytes]:
            try:
                while True:
                    if self._clock() > deadline:
                        raise UpstreamTimeout("timeout")
                    try:
                        data = response.read1(CHUNK)
                    except (TimeoutError, socket.timeout):
                        raise UpstreamTimeout("timeout") from None
                    except (OSError, http.client.HTTPException):
                        raise UpstreamFailed("broken") from None
                    if not data:
                        return
                    yield data
            finally:
                conn.close()

        content_type = response.getheader("Content-Type") or "application/octet-stream"
        return UpstreamResponse(status=response.status, content_type=content_type,
                                chunks=chunks(), close=conn.close)


__all__ = ["HttpsUpstream", "Upstream", "UpstreamFailed", "UpstreamResponse", "UpstreamTimeout",
           "UPSTREAM_HOST", "UPSTREAM_PATH", "UPSTREAM_URL"]
