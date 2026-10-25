"""swarm-api gzips a whole response when the caller asks, and never a stream
(lane API-GZIP, part of #1822).

Measured 2026-10-21: nothing between swarm-api and its callers compressed, and
3.23 GB went out in 48 h -- a build-board task page averaged 1.43 MB and
`sc overview --json` was 1.24 MB for 101 tasks. A streamed body (a log follow,
a live view, an event stream) must reach the caller chunk by chunk, so it is
never encoded.

Offline: the real `create_app` over the in-memory context of conftest.py.
"""

from __future__ import annotations

import gzip
import json

from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from swarm_api.main import create_app

GZIP = {"Accept-Encoding": "gzip"}
PLAIN = {"Accept-Encoding": "identity"}


def _raw(client: TestClient, path: str, headers: dict[str, str]):
    """The response and its body exactly as it came off the wire."""
    with client.stream("GET", path, headers=headers) as response:
        return response, b"".join(response.iter_raw())


def _app(api_context):
    app = create_app(api_context)
    # Routes added after the factory still run under its middleware stack:
    # Starlette builds the stack on the first request.
    big = {"tasks": [{"task_id": f"t-{i}", "state": "RUNNING", "note": "x" * 40}
                     for i in range(400)]}

    def _big() -> JSONResponse:
        return JSONResponse(big)

    def _events() -> StreamingResponse:
        def chunks():
            for i in range(50):
                yield f"data: {json.dumps(big['tasks'][i])}\n\n".encode()
        return StreamingResponse(chunks(), media_type="text/event-stream")

    def _follow() -> StreamingResponse:
        def chunks():
            for i in range(50):
                yield ("line %d %s\n" % (i, "y" * 200)).encode()
        return StreamingResponse(chunks(), media_type="text/plain")

    app.add_api_route("/__gzip/big", _big)
    app.add_api_route("/__gzip/events", _events)
    app.add_api_route("/__gzip/follow", _follow)
    return app, big


def test_a_large_json_answer_is_gzipped_when_asked_and_identical_once_decoded(api_context):
    app, big = _app(api_context)
    client = TestClient(app, raise_server_exceptions=False)
    response, raw = _raw(client, "/__gzip/big", GZIP)
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"  # MUTATION: no middleware -- absent.
    assert json.loads(gzip.decompress(raw)) == big  # MUTATION: corrupt or re-serialise the body -- differs.
    assert len(raw) < len(json.dumps(big)) // 4  # MUTATION: compresslevel 0 -- the body is not smaller.
    assert int(response.headers["content-length"]) == len(raw)  # MUTATION: keep the plain length -- differs.
    assert "accept-encoding" in response.headers.get("vary", "").lower()  # MUTATION: no Vary -- a cache serves gzip to a plain caller.


def test_a_real_routes_large_answer_is_gzipped(client):
    # The real app's own large JSON, through every middleware it ships with.
    response, raw = _raw(client, "/openapi.json", GZIP)
    assert response.headers.get("content-encoding") == "gzip"  # MUTATION: add it OUTSIDE `observe`, which re-chunks every body -- absent.
    plain_response, plain = _raw(client, "/openapi.json", PLAIN)
    assert gzip.decompress(raw) == plain  # MUTATION: corrupt the body -- differs.


def test_a_caller_that_does_not_ask_gets_the_plain_body(api_context):
    app, big = _app(api_context)
    client = TestClient(app, raise_server_exceptions=False)
    response, raw = _raw(client, "/__gzip/big", PLAIN)
    assert "content-encoding" not in response.headers  # MUTATION: compress regardless of Accept-Encoding -- gzip.
    assert json.loads(raw) == big  # MUTATION: same -- not JSON.


def test_a_small_answer_is_not_gzipped(client):
    response, raw = _raw(client, "/healthz", GZIP)
    assert response.status_code == 200
    assert len(raw) < 1024
    assert "content-encoding" not in response.headers  # MUTATION: minimum_size 0 -- gzip.


def test_an_event_stream_is_never_gzipped(api_context):
    app, _ = _app(api_context)
    client = TestClient(app, raise_server_exceptions=False)
    response, raw = _raw(client, "/__gzip/events", GZIP)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in response.headers  # MUTATION: compress text/event-stream -- gzip.
    assert raw.startswith(b"data: ")  # MUTATION: same -- the gzip magic.


def test_a_streamed_answer_of_any_type_is_never_gzipped(api_context):
    app, _ = _app(api_context)
    client = TestClient(app, raise_server_exceptions=False)
    response, raw = _raw(client, "/__gzip/follow", GZIP)
    assert "content-encoding" not in response.headers  # MUTATION: Starlette's stock GZipMiddleware, which compresses a text/plain stream -- gzip.
    assert raw.startswith(b"line 0 ")  # MUTATION: same -- the gzip magic.


def test_a_streamed_chunk_reaches_the_caller_before_the_stream_ends():
    # Pure ASGI, no TestClient: the first chunk must be sent while the
    # application is still producing, which a buffering middleware breaks.
    import asyncio

    from swarm_api.compression import GzipWholeResponses

    sent: list[dict] = []
    seen_before_end: list[int] = []

    async def stream_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"z" * 4096, "more_body": True})
        seen_before_end.append(len(sent))
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"accept-encoding", b"gzip")]}
    asyncio.run(GzipWholeResponses(stream_app)(scope, receive, send))
    assert seen_before_end == [2]  # MUTATION: buffer the stream -- 0 messages sent before it ends.
    assert sent[1]["body"] == b"z" * 4096  # MUTATION: compress the chunk -- differs.
