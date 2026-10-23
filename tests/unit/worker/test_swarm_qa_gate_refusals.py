"""The capture writes the gate's refusals into each screen's errors file
(QA-GATE-REFUSALS; part of #2067).

On 2026-10-23 a capture's `errors.json` showed a CORS block and a failed
request to the app's identity provider, and nothing said the live gate had
answered that request itself. Held here, with test_swarm_qa's fake page and a
fake of the gate's refusal record (`swarm_qa._gate_document`):

* a refusal made while a screen was loading is in that screen's errors file
  as `gate_refused: [{method, origin, path, code}]`, and in no other screen's;
* the failed request it caused is marked as the gate's, not the app's;
* the manifest counts the run's refusals by method and origin;
* a gate whose record cannot be read leaves `gate_refused` null, never an
  empty list that would read as "the gate refused nothing".
"""

from __future__ import annotations

import json
import time
from typing import Any

from test_swarm_qa import _capture, _manifest, qa, world  # noqa: F401  (fixtures)

IDP = "https://sso.example.net"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"


class _Request:
    def __init__(self, url: str) -> None:
        self.url = url


def _refusing_gate(qa, world, monkeypatch, refused_on: str) -> list[dict[str, Any]]:
    """A gate that refuses the token POST whenever `refused_on` loads: the page
    reports the failed request, as Chrome does after a 403 with no CORS header,
    and the gate's record holds the refusal."""
    gate: list[dict[str, Any]] = []
    loaded = world.page.goto

    def goto(url, **kw):
        answer = loaded(url, **kw)
        if url.endswith(refused_on):
            gate.append({"n": len(gate) + 1, "at": time.time(), "method": "POST", "origin": IDP,
                         "path": TOKEN_PATH, "code": "qa_write_refused"})
            for handler in world.page.handlers.get("requestfailed", []):
                handler(_Request(f"{IDP}{TOKEN_PATH}?state=x"))
        return answer

    monkeypatch.setattr(world.page, "goto", goto)
    monkeypatch.setattr(qa, "_gate_document", lambda: {"format": 1, "armed": True, "refusals": list(gate),
                                                       "dropped": 0})
    return gate


def _errors(out, stem: str) -> dict[str, Any]:
    return json.loads((out / "work-a" / f"{stem}.errors.json").read_text(encoding="utf-8"))


def test_a_refusal_while_a_screen_loads_is_in_that_screens_errors_file(qa, world, monkeypatch):
    _refusing_gate(qa, world, monkeypatch, "/runs")
    code, out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    runs = _errors(out, "runs--desktop--light")
    # MUTATION: never read the gate's record in _capture_route and gate_refused is absent.
    assert runs["gate_refused"] == [{"method": "POST", "origin": IDP, "path": TOKEN_PATH,
                                     "code": "qa_write_refused"}]
    # MUTATION: leave failed_requests as the page reported it and the app is blamed for the gate's 403.
    assert len(runs["failed_requests"]) == 1
    assert "refused by the live gate" in runs["failed_requests"][0]
    assert "qa_write_refused" in runs["failed_requests"][0]
    assert runs["failed_requests"][0].startswith(f"{IDP}{TOKEN_PATH}")
    # MUTATION: give every screen the whole record (no time window) and /timeline carries /runs's refusal.
    assert _errors(out, "timeline--desktop--light")["gate_refused"] == []
    manifest = _manifest(bundle)
    entry = next(r for r in manifest["routes"] if r["id"] == "runs")
    assert entry["gate_refused"] == 1
    # MUTATION: leave the run's count out of the manifest and the report cannot open with it.
    assert manifest["gate_refusals"]["read"] is True
    assert manifest["gate_refusals"]["count"] == 1
    assert manifest["gate_refusals"]["by"] == [{"method": "POST", "origin": IDP, "count": 1}]


def test_a_failed_request_the_gate_did_not_refuse_stays_the_apps(qa, world, monkeypatch):
    _refusing_gate(qa, world, monkeypatch, "/never-loaded")
    loaded = world.page.goto

    def goto(url, **kw):
        answer = loaded(url, **kw)
        for handler in world.page.handlers.get("requestfailed", []):
            handler(_Request(f"{IDP}/other"))
        return answer

    monkeypatch.setattr(world.page, "goto", goto)
    code, out, _bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    runs = _errors(out, "runs--desktop--light")
    # MUTATION: mark every failed request to the refused origin as the gate's and the app's own failure is hidden.
    assert runs["failed_requests"] == [f"{IDP}/other"]
    assert runs["gate_refused"] == []


def test_a_gate_whose_record_cannot_be_read_leaves_gate_refused_null(qa, world, monkeypatch):
    def unreadable():
        raise OSError("connection refused")

    monkeypatch.setattr(qa, "_gate_document", unreadable)
    code, out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    # MUTATION: write [] when the record could not be read and an unread gate reads as one that refused nothing.
    assert _errors(out, "runs--desktop--light")["gate_refused"] is None
    manifest = _manifest(bundle)
    assert manifest["gate_refusals"]["read"] is False
    assert "connection refused" in manifest["gate_refusals"]["why"]


def test_the_capture_reads_the_path_the_gate_serves_the_record_at(qa):
    from agent_worker.livegate import qatarget

    # MUTATION: rename either constant and the capture reads a 404 on every run.
    assert qa.GATE_REFUSALS_PATH == qatarget.REFUSALS_PATH
    assert qa._gate_url("http://127.0.0.1:9222") == "http://127.0.0.1:9222" + qatarget.REFUSALS_PATH
