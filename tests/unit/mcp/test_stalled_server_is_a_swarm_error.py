"""A server that accepts and then stalls is a SwarmError naming the route (issue 53).

urllib wraps only the SEND in `URLError`. A timeout while the answer is read --
in `getresponse()` or in `read()` -- is a bare `TimeoutError`, an `OSError`
that is not a `URLError`, so it went past every `except URLError` in the
bridge: each `except SwarmError` fallback was skipped and the tool call ended
as "TimeoutError: timed out" with no route and no base URL.

Every server here is on 127.0.0.1 and either never answers (a listening
socket nobody accepts from: the kernel completes the handshake, so the request
is sent and the answer never comes) or answers a status line and headers and
then stalls in the body. The control is a port nobody listens on, which is
refused and was always a `URLError`.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Iterator

import pytest

from swarm_mcp import awaitinput, patches, signin
from swarm_mcp import client as client_module
from swarm_mcp.client import SwarmClient, SwarmError

#: Seconds; long enough for a loopback answer, short enough to keep the file quick.
SHORT = 0.5


@contextlib.contextmanager
def _silent() -> Iterator[int]:
    """A port that accepts connections and never answers on any of them."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        yield sock.getsockname()[1]


@contextlib.contextmanager
def _half_answer(status: int) -> Iterator[int]:
    """A port that answers `status` and its headers, promises a body, and stalls."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    done = threading.Event()
    held: list[socket.socket] = []

    def serve() -> None:
        with contextlib.suppress(OSError):
            conn, _ = sock.accept()
            held.append(conn)
            conn.recv(65536)
            conn.sendall(
                f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                "Content-Length: 100\r\n\r\n{".encode()
            )
            done.wait(10)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        done.set()
        for conn in held:
            conn.close()
        sock.close()
        thread.join(5)


def _refused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    """An ID token from the environment, so no credential is minted on the wire."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))


def _client(port: int) -> SwarmClient:
    return SwarmClient(base_url=f"http://127.0.0.1:{port}")


# -- the transport every tool uses ---------------------------------------------
def test_a_stalled_api_is_a_swarm_error_naming_the_route_and_base_url():
    with _silent() as port:
        with pytest.raises(SwarmError) as caught:
            _client(port).request("GET", "/v1/tasks/t-1", timeout=SHORT)
    message = str(caught.value)
    # MUTATION: no OSError branch -- a bare TimeoutError escapes pytest.raises.
    assert "GET /v1/tasks/t-1" in message
    assert f"http://127.0.0.1:{port}" in message
    assert caught.value.status is None


def test_a_body_that_stalls_after_a_2xx_is_a_swarm_error():
    with _half_answer(200) as port:
        with pytest.raises(SwarmError) as caught:
            _client(port).request("GET", "/v1/tasks", timeout=SHORT)
    assert "GET /v1/tasks" in str(caught.value)


def test_an_error_body_that_stalls_keeps_its_status():
    """The HTTPError branch reads the body too, and that read can time out."""
    with _half_answer(503) as port:
        with pytest.raises(SwarmError) as caught:
            _client(port).request("GET", "/v1/tasks", timeout=SHORT)
    # MUTATION: an unguarded body read -- TimeoutError escapes the except clause.
    assert caught.value.status == 503
    assert "GET /v1/tasks" in str(caught.value)
    assert f"http://127.0.0.1:{port}" in str(caught.value)


def test_control_a_refused_connection_is_still_could_not_reach():
    port = _refused_port()
    with pytest.raises(SwarmError) as caught:
        _client(port).request("GET", "/v1/tasks", timeout=SHORT)
    assert str(caught.value).startswith(f"could not reach http://127.0.0.1:{port}")


# -- the multipart answer route --------------------------------------------------
def test_a_stalled_answer_upload_is_a_swarm_error_naming_the_route(monkeypatch):
    real = client_module._open
    monkeypatch.setattr(client_module, "_open", lambda req, timeout: real(req, timeout=SHORT))
    with _silent() as port:
        with pytest.raises(SwarmError) as caught:
            awaitinput.post_multipart(_client(port), "/v1/answers/a-1", "multipart/form-data; boundary=b", b"--b--\r\n")
    assert "POST /v1/answers/a-1" in str(caught.value)
    assert f"http://127.0.0.1:{port}" in str(caught.value)


def test_control_a_refused_answer_upload_is_could_not_reach():
    port = _refused_port()
    with pytest.raises(SwarmError, match="could not reach"):
        awaitinput.post_multipart(_client(port), "/v1/answers/a-1", "multipart/form-data; boundary=b", b"--b--\r\n")


# -- the GCS read behind a patch -----------------------------------------------------
class _Reader:
    """A client with only what `patches._get` reads: a runtime-built token."""

    def __init__(self) -> None:
        import secrets

        self._token = secrets.token_hex(16)

    def access_token(self) -> str:
        return self._token


def test_a_stalled_object_read_is_a_swarm_error_naming_the_object():
    uri = "gs://bucket/tenant/patch.diff"
    with _silent() as port:
        with pytest.raises(SwarmError) as caught:
            patches._get(_Reader(), uri, f"http://127.0.0.1:{port}/o/patch", timeout=SHORT)
    assert uri in str(caught.value)


def test_control_a_refused_object_read_is_a_swarm_error():
    uri = "gs://bucket/tenant/patch.diff"
    port = _refused_port()
    with pytest.raises(SwarmError, match="could not read gs://bucket/tenant/patch.diff"):
        patches._get(_Reader(), uri, f"http://127.0.0.1:{port}/o/patch", timeout=SHORT)


# -- the sign-in's token endpoint ------------------------------------------------------
def test_a_stalled_token_endpoint_is_a_swarm_error_naming_it():
    with _silent() as port:
        with pytest.raises(SwarmError) as caught:
            signin._post(f"http://127.0.0.1:{port}/token", {"grant_type": "x"}, timeout=SHORT)
    assert f"127.0.0.1:{port}" in str(caught.value)
    assert "/token" in str(caught.value)


def test_control_a_refused_token_endpoint_is_could_not_reach():
    port = _refused_port()
    with pytest.raises(SwarmError, match=f"could not reach 127.0.0.1:{port}"):
        signin._post(f"http://127.0.0.1:{port}/token", {"grant_type": "x"}, timeout=SHORT)
