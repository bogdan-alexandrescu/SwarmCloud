"""`sc whoami` shows the three features `/v1/tenants/me` serves (FEATURES-READ, part of #1748).

Beside the tenant and its role, the same read: `caller_mcp`, `await_input`
and `service_answer`, each "on", "off" or "not reported". "not reported" is
what an `"unknown"` from the API reads as, and what an API older than the
field reads as: neither is ever shown as "off", which is the misreading the
issue was filed over (Oracle told its users agents could not read it).
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_mcp import sc
from swarm_mcp.client import SwarmError


class Api:
    def __init__(self, features: Any) -> None:
        self.features = features

    def request(self, method: str, path: str, **_kwargs: Any) -> Any:
        if (method, path) == ("GET", "/v1/tenants/me"):
            me: dict[str, Any] = {"tenant": {"tenant_id": "eng"},
                                  "principal": {"email": "carol@example.com"}}
            if self.features is not None:
                me["features"] = self.features
            return me
        if (method, path) == ("GET", "/v1/tenants/mine"):
            return []
        raise SwarmError(f"{method} {path} -> 404: no such route", status=404)

    def tenants_mine(self) -> list[dict[str, Any]]:
        return self.request("GET", "/v1/tenants/mine")


@pytest.fixture
def whoami(monkeypatch):
    def run(features: Any, *, as_json: bool) -> str:
        api = Api(features)

        class _Ctx:
            def __enter__(self):
                return api

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(sc, "_resolve", lambda args: SimpleNamespace(
            context="qa", url="http://api.invalid", source="test", current=True,
            client_id="", front_door=False))
        monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: _Ctx())
        monkeypatch.setenv("SWARM_TENANT", "eng")
        monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
        out = io.StringIO()
        sc.cmd_whoami(None, SimpleNamespace(json=as_json, context=None), out)
        return out.getvalue()

    return run


def test_on_and_off_are_shown_as_the_api_served_them(whoami):
    served = {"caller_mcp": True, "await_input": False, "service_answer": True}
    shown = json.loads(whoami(served, as_json=True))
    # MUTATION: dropping the field, or inverting a boolean, fails here.
    assert shown["features"] == {"caller_mcp": "on", "await_input": "off",
                                 "service_answer": "on"}, shown
    line = next(row for row in whoami(served, as_json=False).splitlines()
                if row.startswith("features "))
    # MUTATION: a text view that leaves the line out fails the `next` above;
    # one that prints the wrong state fails here.
    assert "caller_mcp on" in line and "await_input off" in line, line
    assert "service_answer on" in line, line


def test_unknown_reads_as_not_reported_never_as_off(whoami):
    served = {"caller_mcp": "unknown", "await_input": True, "service_answer": "unknown"}
    shown = json.loads(whoami(served, as_json=True))
    # MUTATION: `bool(value)` / `value is True` renders "unknown" as on or
    # off; only the explicit three-way read passes.
    assert shown["features"] == {"caller_mcp": "not reported", "await_input": "on",
                                 "service_answer": "not reported"}, shown
    text = whoami(served, as_json=False)
    assert "caller_mcp not reported" in text, text
    assert "caller_mcp off" not in text, text


def test_an_api_older_than_the_field_reports_all_three_as_not_reported(whoami):
    shown = json.loads(whoami(None, as_json=True))
    # MUTATION: defaulting an absent block to off fails here.
    assert shown["features"] == {"caller_mcp": "not reported", "await_input": "not reported",
                                 "service_answer": "not reported"}, shown
