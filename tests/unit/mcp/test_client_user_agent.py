"""Every request the bridge and `sc` send names them in `User-Agent` (BGD's
pick, 2026-11-06): 3,299 of 3,300 single-task reads in a 20-minute sample came
from a caller whose user agent was the bare `Python-urllib` default, so the
request log could not say which client to slow down.

Offline: the real `SwarmClient` over a replaced `client._open`.
"""

from __future__ import annotations

import io
import json

import pytest

from swarm_mcp import client as mcp_client
from swarm_mcp.client import SwarmClient


class _Response:
    status = 200

    def __init__(self) -> None:
        self._body = io.BytesIO(json.dumps({"task": {"id": "t", "state": "RUNNING"}}).encode())
        self.headers = {}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture()
def sent(monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    requests: list = []

    def _opener(req, timeout=None):  # noqa: ARG001
        requests.append(req)
        return _Response()

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return requests


def test_the_cli_names_itself_on_every_request(sent):
    api = SwarmClient(base_url="http://api.invalid")
    api.task("t")
    api.request("POST", "/v1/tasks/t/cancel", payload={})
    agents = {req.get_header("User-agent") for req in sent}
    assert agents == {f"swarm-mcp/{mcp_client.client_version()} (cli)"}


def test_the_bridge_names_itself_as_the_bridge(sent):
    api = SwarmClient(base_url="http://api.invalid")
    api.pace_for_bridge()
    api.task("t")
    api.request("GET", "/v1/workflows/w")
    agents = {req.get_header("User-agent") for req in sent}
    assert agents == {f"swarm-mcp/{mcp_client.client_version()} (bridge)"}


def test_the_version_is_never_empty():
    assert mcp_client.client_version()
    assert mcp_client.user_agent("cli").startswith("swarm-mcp/")
