"""Every web call the sc bridge makes names it in `User-Agent` (the owner's
decision, 2026-11-06): `client.py` already did, but the metadata probe and
token, the deployment discovery, the GCS patch reads, the multipart answer and
the OAuth sign-in posts went out as urllib's bare `Python-urllib/3.x`, so the
API's logs could not tell the bridge and its version apart.

Offline: each module's own opener is replaced; nothing reaches a network.
"""

from __future__ import annotations

import io
import secrets
import urllib.request
from types import SimpleNamespace

import pytest

from swarm_mcp import auth, awaitinput, discovery, patches, signin
from swarm_mcp import client as mcp_client


class _Response:
    status = 200

    def __init__(self, body: bytes = b"{}") -> None:
        self._body = io.BytesIO(body)
        self.headers = {}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture()
def sent(monkeypatch):
    """Every Request any of the six modules hands to an opener."""
    requests: list[urllib.request.Request] = []

    def _open(request, timeout=None, **_):  # noqa: ARG001
        requests.append(request)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setattr(mcp_client, "_open", _open)
    monkeypatch.setattr(discovery, "_OPENER", SimpleNamespace(open=_open))
    monkeypatch.setattr(signin, "_OPENER", SimpleNamespace(open=_open))
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    return requests


def _fake_client(agent: str | None) -> SimpleNamespace:
    fake = SimpleNamespace(
        base_url="http://api.invalid",
        sends_own_token=False,
        tenant="",
        access_token=lambda: secrets.token_hex(8),
    )
    if agent is not None:
        fake.user_agent = agent
    return fake


CALLS = {
    "auth metadata probe": lambda: auth._metadata_available(timeout=0.1),
    "auth metadata token": lambda: auth.id_token_for("http://api.invalid", tier=auth.Tier.METADATA),
    "discovery": lambda: discovery.http_get("http://api.invalid/.well-known/x", 1.0),
    "patches": lambda: patches._get(_fake_client(None), "gs://b/o", "http://gcs.invalid/o", timeout=1),
    "awaitinput": lambda: awaitinput.post_multipart(
        _fake_client(None), "/v1/x", "multipart/form-data; boundary=b", b"--b--"),
    "signin": lambda: signin._post("http://oauth.invalid/token", {"grant_type": "x"}, timeout=1.0),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_the_request_names_the_package_and_its_version(sent, name):
    CALLS[name]()
    assert sent, f"{name} made no request"
    for request in sent:
        assert request.get_header("User-agent") == mcp_client.user_agent("cli"), name


def test_a_call_through_a_client_names_that_clients_kind(sent):
    agent = mcp_client.user_agent("bridge")
    patches._get(_fake_client(agent), "gs://b/o", "http://gcs.invalid/o", timeout=1)
    awaitinput.post_multipart(_fake_client(agent), "/v1/x", "multipart/form-data; boundary=b", b"--b--")
    assert [r.get_header("User-agent") for r in sent] == [agent, agent]


def test_the_header_carries_nothing_but_the_name_and_version(sent):
    auth._metadata_available(timeout=0.1)
    (request,) = sent
    assert request.get_header("User-agent") == (
        f"{mcp_client.USER_AGENT_PRODUCT}/{mcp_client.client_version()} (cli)")
