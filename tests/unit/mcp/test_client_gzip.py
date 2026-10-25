"""The bridge's and `sc`'s one HTTP client asks for gzip and decodes it, and
still reads a plain body from an API that does not compress (lane API-GZIP,
part of #1822).

The owner's bridge was 64% of all requests to swarm-api on 2026-10-21, and
none of them asked for compression. Offline: the real `SwarmClient` over a
replaced `client._open`, the same seam test_bridge_pacing.py uses.
"""

from __future__ import annotations

import email.message
import gzip
import io
import json
import urllib.error

import pytest

from swarm_mcp import client as mcp_client
from swarm_mcp.client import RawBody, SwarmClient, SwarmError

BIG = {"tasks": [{"task_id": f"t-{i}", "state": "RUNNING"} for i in range(300)]}


class _Response:
    status = 200

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = io.BytesIO(body)
        self.headers = email.message.Message()
        for name, value in headers.items():
            self.headers[name] = value

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture()
def wire(monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    state = type("Wire", (), {})()
    state.answer = None
    state.sent = []

    def _opener(req, timeout=None):  # noqa: ARG001
        state.sent.append(req)
        answer = state.answer
        if callable(answer):
            raise answer(req.full_url)
        return answer

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return state


def _gzipped(payload) -> bytes:
    return gzip.compress(json.dumps(payload).encode())


def test_the_client_asks_for_gzip(wire):
    wire.answer = _Response(json.dumps(BIG).encode(), {"Content-Type": "application/json"})
    SwarmClient(base_url="http://api.invalid").request("GET", "/v1/tasks")
    assert wire.sent[0].get_header("Accept-encoding") == "gzip"  # MUTATION: send no Accept-Encoding -- None.


def test_a_gzip_body_is_decoded(wire):
    wire.answer = _Response(_gzipped(BIG), {"Content-Type": "application/json",
                                            "Content-Encoding": "gzip"})
    assert SwarmClient(base_url="http://api.invalid").request("GET", "/v1/tasks") == BIG  # MUTATION: no decoding -- "not JSON" SwarmError.


def test_a_plain_body_from_an_older_api_is_still_read(wire):
    wire.answer = _Response(json.dumps(BIG).encode(), {"Content-Type": "application/json"})
    assert SwarmClient(base_url="http://api.invalid").request("GET", "/v1/tasks") == BIG  # MUTATION: always gunzip -- BadGzipFile.


def test_a_gzip_error_body_is_decoded_into_its_explanation(wire):
    def _refuse(url):
        headers = email.message.Message()
        headers["Content-Encoding"] = "gzip"
        body = _gzipped({"code": "not_found", "message": "no such task " + "q" * 2000})
        return urllib.error.HTTPError(url, 404, "Not Found", headers, io.BytesIO(body))

    wire.answer = _refuse
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="http://api.invalid").request("GET", "/v1/tasks/x")
    assert caught.value.status == 404
    assert caught.value.code == "not_found"  # MUTATION: leave the error body encoded -- None.


def test_a_raw_gzip_body_is_decoded_within_its_limit(wire):
    data = b"log line\n" * 2000
    wire.answer = _Response(gzip.compress(data), {"Content-Type": "text/plain",
                                                  "Content-Encoding": "gzip"})
    body = SwarmClient(base_url="http://api.invalid").artifact_raw(
        "x", "out.log", max_bytes=len(data))
    assert isinstance(body, RawBody)
    assert body.data == data  # MUTATION: return the compressed bytes -- differs.
    assert "content-encoding" not in body.headers  # MUTATION: keep the header over decoded bytes -- gzip.


def test_a_raw_gzip_body_over_its_limit_is_refused_once_decoded(wire):
    data = b"z" * 100_000  # compresses to a few hundred bytes
    wire.answer = _Response(gzip.compress(data), {"Content-Type": "text/plain",
                                                  "Content-Encoding": "gzip"})
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="http://api.invalid").artifact_raw(
            "x", "out.log", max_bytes=10_000)
    assert "over 10000 bytes" in str(caught.value)  # MUTATION: bound the compressed size, not the decoded one -- accepted.


def test_a_raw_plain_body_is_still_read(wire):
    data = b"plain\n" * 100
    wire.answer = _Response(data, {"Content-Type": "text/plain"})
    body = SwarmClient(base_url="http://api.invalid").artifact_raw(
        "x", "out.log", max_bytes=len(data))
    assert body.data == data  # MUTATION: always gunzip -- error.


def test_a_body_labelled_gzip_but_already_decoded_is_read_as_it_is(wire):
    # A proxy or a test transport (httpx's TestClient) that inflated the body
    # on the way and kept the header.
    wire.answer = _Response(json.dumps(BIG).encode(), {"Content-Type": "application/json",
                                                       "Content-Encoding": "gzip"})
    assert SwarmClient(base_url="http://api.invalid").request("GET", "/v1/tasks") == BIG  # MUTATION: gunzip on the header alone -- zlib.error.
