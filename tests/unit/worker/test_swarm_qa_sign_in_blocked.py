"""A capture that never got past sign-in says so (part of #2688).

On 2026-11-02 a visual QA run on an own-login target captured only the app's
"Couldn't check your session" card: the live gate refused the app's POST to
its sign-in provider's token endpoint on every screen, and the run read
green. Held here, with test_swarm_qa's fake page and a fake of the gate's
refusal record:

* a refused POST to another origin's token endpoint makes the manifest
  `reviewable: false`, with `sign_in_blocked.idp_token_refused` counting it;
* every route reading the same body text does too (`same_screen`);
* a refusal that is not a token request, a token refresh the gate let
  through, or routes that read differently leave the capture reviewable;
* the verdict line on stdout carries the same verdict.
"""

from __future__ import annotations

import json
import time
from typing import Any

from test_swarm_qa import _capture, _manifest, qa, world  # noqa: F401  (fixtures)

IDP = "https://sso.example.net"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"
CARD = "Couldn't check your session: the sign-in server could not be reached. Retry"


def _gate(qa, monkeypatch, refusals: list[dict[str, Any]], allowed: list[dict[str, Any]] | None = None) -> None:
    monkeypatch.setattr(qa, "_gate_document", lambda: {"format": 1, "armed": True, "refusals": list(refusals),
                                                       "dropped": 0, "allowed": list(allowed or []),
                                                       "allowed_dropped": 0})


def _refused(n: int, origin: str = IDP, path: str = TOKEN_PATH, method: str = "POST",
             code: str = "qa_write_refused") -> dict[str, Any]:
    # Dated inside the capture: the record is read for what the gate did after it began.
    return {"n": n, "at": time.time() + 60, "method": method, "origin": origin, "path": path, "code": code}


def test_a_refused_token_request_makes_the_capture_not_reviewable(qa, world, monkeypatch, capsys):
    _gate(qa, monkeypatch, [_refused(1), _refused(2)])
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    # The tarball is still written and the exit is unchanged: it is the evidence.
    assert code == 0
    manifest = _manifest(bundle)
    # MUTATION: drop sign_in_blocked from capture() and the run reviews the sign-in card as the app.
    assert manifest["reviewable"] is False
    blocked = manifest["sign_in_blocked"]
    assert blocked["idp_token_refused"] == [{"method": "POST", "origin": IDP, "path": TOKEN_PATH, "count": 2}]
    assert "token request" in blocked["why"] and f"{IDP}{TOKEN_PATH}" in blocked["why"]
    verdict = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # MUTATION: leave the verdict line as it was and the capture step's answer says nothing.
    assert verdict["reviewable"] is False
    assert verdict["not_reviewable"] == blocked["why"]


def test_every_route_reading_the_same_text_makes_the_capture_not_reviewable(qa, world, monkeypatch):
    _gate(qa, monkeypatch, [])
    monkeypatch.setattr(world.page, "inner_text", lambda *_a, **_kw: f"  {CARD}\n")
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    # MUTATION: return False from same_screen and an error card on every route reads as the app.
    assert manifest["reviewable"] is False
    assert manifest["sign_in_blocked"]["same_screen"] is True
    assert manifest["sign_in_blocked"]["idp_token_refused"] == []
    assert "same page" in manifest["sign_in_blocked"]["why"]


def test_routes_that_read_differently_and_no_token_refusal_stay_reviewable(qa, world, monkeypatch, capsys):
    # A write to the app's own origin, a GET elsewhere, a token path on the base origin and a
    # non-token path on the IdP: none is the sign-in provider's token request.
    _gate(qa, monkeypatch, [
        _refused(1, origin="http://localhost:5173", path="/api/token"),
        _refused(2, path="/realms/apps/account", method="POST"),
        _refused(3, method="PUT"),
        _refused(4, code="qa_target_left"),
    ])
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    manifest = _manifest(bundle)
    # MUTATION: count every refusal to another origin as a token refusal and a reviewable leg is dropped.
    assert manifest["reviewable"] is True
    assert manifest["sign_in_blocked"] is None
    verdict = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert verdict["reviewable"] is True and "not_reviewable" not in verdict


def test_a_token_refresh_the_gate_let_through_is_no_block(qa, world, monkeypatch):
    allowed = [{**_refused(1), "code": "idp_token_refresh", "label": "allowed: IdP token refresh"}]
    _gate(qa, monkeypatch, [], allowed)
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    # MUTATION: read the record's `allowed` as refusals and an approved refresh blocks the leg.
    assert _manifest(bundle)["reviewable"] is True


def test_one_route_alone_is_never_the_same_screen(qa, world, monkeypatch):
    _gate(qa, monkeypatch, [])
    monkeypatch.setattr(world.page, "inner_text", lambda *_a, **_kw: CARD)
    code, _out, bundle = _capture(qa, world, "--settle", "generic", group="other", presets="desktop",
                                  themes="light")
    assert code == 0
    # One route at one width is one text: nothing to compare it with.
    assert _manifest(bundle)["reviewable"] is True


def test_idp_token_refusals_counts_by_method_origin_and_path(qa):
    records = [_refused(1), _refused(2), _refused(3, origin="https://login.example.org", path="/oauth/token/")]
    assert qa.idp_token_refusals(records, "https://app.example.com/base") == [
        {"method": "POST", "origin": "https://login.example.org", "path": "/oauth/token/", "count": 1},
        {"method": "POST", "origin": IDP, "path": TOKEN_PATH, "count": 2},
    ]
