"""swarm-github-hooks over a real socket: the standard library server around
Receiver answers what Receiver decided, writes no HTML and no request line, and
never reads a body the length refused (lane GS-HOOKS)."""

from __future__ import annotations

import http.client
import threading

import pytest

from swarm_github_hooks.receiver import MAX_BODY_BYTES, Receiver, WebhookSecret
from swarm_github_hooks.server import make_server

from hooks_fixtures import NOW, Forward, Secret, Store, new_secret, push


@pytest.fixture
def served():
    key = new_secret()
    store, forward = Store(), Forward()
    receiver = Receiver(secret=WebhookSecret(Secret(key)), deliveries=store, forward=forward,
                        clock=lambda: NOW)
    server = make_server(receiver, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield key, server.server_address[1], store, forward
    finally:
        server.shutdown()
        server.server_close()


def _request(port: int, method: str, path: str, body: bytes = b"", headers=()):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest(method, path, skip_accept_encoding=True)
    for name, value in headers:
        conn.putheader(name, value)
    conn.endheaders()
    if body:
        conn.send(body)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def test_a_signed_push_is_forwarded_over_http(served):
    key, port, store, forward = served
    delivery = push(key)
    response, data = _request(port, "POST", "/github/events", delivery.body, delivery.headers())
    assert response.status == 200, data
    assert b"forwarded" in data
    assert forward.calls == [("Saga-XYZ/Widgets",)]
    assert response.getheader("Server") in (None, "swarm-github-hooks")


def test_a_ping_is_an_empty_204(served):
    key, port, _, _ = served
    delivery = push(key)
    delivery.event = "ping"
    response, data = _request(port, "POST", "/github/events", delivery.body, delivery.headers())
    assert response.status == 204 and data == b""


def test_an_unknown_method_is_404_not_501(served):
    _, port, _, _ = served
    response, data = _request(port, "BREW", "/github/events", headers=[("Content-Length", "0")])
    assert response.status == 404
    assert b"<html" not in data.lower()


def test_an_oversized_length_is_413_without_reading(served):
    key, port, store, forward = served
    delivery = push(key)
    headers = [(n, v) for n, v in delivery.headers() if n != "Content-Length"]
    headers.append(("Content-Length", str(MAX_BODY_BYTES + 1)))
    # Only the head is sent: the answer must not wait for a body.
    response, _ = _request(port, "POST", "/github/events", b"", headers)
    assert response.status == 413
    assert store.calls == [] and forward.calls == []


def test_a_bad_signature_is_401_over_http(served):
    key, port, store, _ = served
    delivery = push(key)
    delivery.signature = "sha256=" + "0" * 64
    response, _ = _request(port, "POST", "/github/events", delivery.body, delivery.headers())
    assert response.status == 401 and store.calls == []
