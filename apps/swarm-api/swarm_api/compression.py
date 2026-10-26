"""Gzip for whole answers, never for streams (lane API-GZIP, part of #1822).

WHY IT IS HERE. Until this, nothing between swarm-api and its callers
compressed. Measured 2026-10-21 (the system analysis behind epic #1822): 3.23
GB left the API in 48 h; a build-board task page averaged 1.43 MB; `sc
overview --json` was 1.24 MB for 101 tasks; and the owner's bridge
(apps/swarm-mcp, Python urllib) made 64% of all requests. These are JSON
documents of repeated keys, which gzip shrinks several times over.

WHY NOT STARLETTE'S `GZipMiddleware` AS IT STANDS. Starlette 1.6 excludes
`text/event-stream` by default, but it compresses every OTHER streamed body
chunk by chunk with a sync flush. A streamed answer here -- an artifact's raw
bytes, a log or recording read as it is written, any later live view -- must
reach the caller exactly as the route sent it: a compressed stream is a stream
a proxy or a client may hold until it has enough to inflate, and `text/plain`
followed with `curl` without `--compressed` arrives as binary. So this
compresses only an answer whose FIRST body message is also its last (a
`JSONResponse`, a `Response`), and passes anything sent in parts through
untouched, whatever its type. `text/event-stream` and already-compressed or
media types are excluded by name as well, so a one-message event stream is
not encoded either.

It must sit INSIDE every `BaseHTTPMiddleware` (`observe` in main.py):
`call_next` re-sends every body in parts, so outside it every answer would
look streamed and nothing would be compressed.
"""

from __future__ import annotations

import zlib
from typing import Any

import anyio.to_thread
from starlette.datastructures import Headers, MutableHeaders

#: Below this, gzip's header and the CPU cost more than the bytes saved: an
#: error envelope or a health check is a few hundred bytes. "About 1 KB" in
#: the lane's brief.
MINIMUM_SIZE = 1024
#: zlib's default: level 9 costs several times the CPU for a few percent on
#: JSON, and this runs on every large read.
COMPRESS_LEVEL = 6
#: A body at least this big is compressed on a worker thread, so a 1.4 MB
#: board page does not stall the event loop for every other request.
THREAD_MINIMUM_SIZE = 128 * 1024
#: Never encoded, whole or not. `text/event-stream` is the one that matters
#: (an event must reach the caller when it is sent); the rest are already
#: compressed, so gzip only adds bytes.
EXCLUDED_TYPES = frozenset({
    "text/event-stream",
    "application/gzip", "application/x-gzip", "application/zip",
    "image/*", "audio/*", "video/*", "font/woff", "font/woff2",
})


def accepts_gzip(value: str) -> bool:
    """Whether an `Accept-Encoding` value accepts gzip: named (or `*`) and not
    refused with `q=0`."""
    for item in value.split(","):
        name, _, params = item.strip().partition(";")
        if name.strip().lower() not in ("gzip", "*"):
            continue
        q = params.strip().lower()
        if q.startswith("q="):
            try:
                return float(q[2:]) > 0
            except ValueError:
                return False
        return True
    return False


def _excluded(media_type: str) -> bool:
    media_type = media_type.partition(";")[0].strip().lower()
    return bool({media_type, media_type.partition("/")[0] + "/*"} & EXCLUDED_TYPES)


def _gzip(body: bytes) -> bytes:
    compressor = zlib.compressobj(COMPRESS_LEVEL, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return compressor.compress(body) + compressor.flush()


class GzipWholeResponses:
    """Pure ASGI: gzip a whole answer of at least `minimum_size` bytes for a
    caller that accepts it; send everything else exactly as the route did."""

    def __init__(self, app: Any, *, minimum_size: int = MINIMUM_SIZE) -> None:
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not accepts_gzip(
                Headers(scope=scope).get("accept-encoding", "")):
            await self.app(scope, receive, send)
            return
        start: dict | None = None
        decided = False

        async def send_maybe_compressed(message: dict) -> None:
            nonlocal start, decided
            if decided:
                await send(message)
                return
            if message["type"] == "http.response.start":
                # Held until the first body says whether this is one message.
                start = message
                return
            decided = True
            if start is None:  # pragma: no cover - ASGI always starts first
                await send(message)
                return
            headers = MutableHeaders(raw=start["headers"])
            body = message.get("body", b"")
            if (message["type"] != "http.response.body"
                    or message.get("more_body", False)
                    or "content-encoding" in headers
                    or start["status"] in (204, 206, 304)
                    or _excluded(headers.get("content-type", ""))
                    or len(body) < self.minimum_size):
                await send(start)
                await send(message)
                return
            if len(body) >= THREAD_MINIMUM_SIZE:
                compressed = await anyio.to_thread.run_sync(_gzip, body)
            else:
                compressed = _gzip(body)
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(compressed))
            headers.add_vary_header("Accept-Encoding")
            await send(start)
            await send({**message, "body": compressed})

        await self.app(scope, receive, send_maybe_compressed)
