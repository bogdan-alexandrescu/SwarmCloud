"""The gate lets one IdP token path through, after its own sign-in (QA-IDP-POST; part of #2067).

BGD decided on 2026-10-23: a QA target may carry an admin-set `idp_token_path`
on its approved `extra_origin`, and the live gate lets exactly
`POST <extra_origin><idp_token_path>` through, only once its own sign-in has
completed. An app that refreshes its SSO token after sign-in (AI Studio,
Keycloak's `/protocol/openid-connect/token`) otherwise renders signed out.
Every other non-GET to the extra origin, and to any other non-target origin,
is refused exactly as before. Held here, read with an attacker's lens:

* the exact URL after sign-in continues, and is kept in the record as an
  allowed IdP token refresh, never as a refusal;
* the same before the gate signed in, after a sign-in that failed or met a
  challenge, and on a target that never signs in, is refused;
* a query, an empty query, a fragment, a trailing slash, a double slash, an
  encoded or upper-case variant, a dot segment, another port, userinfo and
  another path are refused, and a redirect is judged by its own URL;
* PUT, PATCH and DELETE to the token path are refused;
* the base origin's same path is not the token path;
* a target with no `idp_token_path` judges exactly as before;
* the hand-off's path is checked again (a path, no query, no `..`, an
  extra origin to put it on), and the worker carries it from swarm-api's
  answer to the gate.

Pure: `QaTarget` and the worker's `handoff_args`, no Chromium, no network,
no cloud credential.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping
from typing import Any

import pytest

from agent_worker import qa_target as worker_qt
from agent_worker.livegate import qatarget as gate_qt

ORIGIN = "https://app.example.com"
IDP = "https://sso.example.net"
ELSEWHERE = "https://pay.example.net"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"
TOKEN_URL = f"{IDP}{TOKEN_PATH}"


def _args(**over: Any) -> dict[str, Any]:
    """An own-login target signing in through the IdP, with its token path."""
    args: dict[str, Any] = {
        "target_id": "qat_" + secrets.token_hex(8), "base_url": f"{ORIGIN}/home",
        "origins": [ORIGIN, IDP], "environment": "production",
        "rate": {"navigation_ms": 1000, "requests_per_s": 10}, "read_posts": ["/graphql"],
        "exclusions": {"paths": ["/logout"]}, "mutating": False,
        "sign_in": {"kind": "own_login", "login_url": f"{ORIGIN}/login", "signed_in_selector": "#me"},
        # Built at runtime: nothing here is a literal credential.
        "credential": {"username": "qa-" + secrets.token_hex(4) + "@example.com",
                       "password": "pw-" + secrets.token_hex(12)},
        "idp_token_path": TOKEN_PATH,
    }
    args.update(over)
    return args


def _target(**over: Any) -> gate_qt.QaTarget:
    target = gate_qt.QaTarget(lambda e, f: None, wall=lambda: 1_700_000_000.0)
    target.handle(_args(**over))
    return target


def _signed_in(**over: Any) -> gate_qt.QaTarget:
    target = _target(**over)
    target.signing_in = True
    target.finished(gate_qt.STATE_SIGNED_IN)
    return target


def _post(target: gate_qt.QaTarget, url: str, method: str = "POST") -> gate_qt.Verdict:
    return target.judge(method, url, top_document=False)


# --------------------------------------------------------------------------
# The one exception
# --------------------------------------------------------------------------


def test_a_post_to_the_exact_token_path_after_sign_in_continues():
    target = _signed_in()
    verdict = _post(target, TOKEN_URL)
    # MUTATION: drop the token-path branch from `QaTarget.judge` and this is "refuse".
    assert verdict.action == "continue"
    # MUTATION: return plain CONTINUE and the guard cannot record it as allowed.
    assert verdict.allowed == gate_qt.IDP_TOKEN_ALLOWED


def test_the_same_post_before_the_gates_sign_in_is_refused():
    target = _target()
    # Handed over, the sign-in queued and not yet started.
    # MUTATION: test the token path before the sign-in state and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"


@pytest.mark.parametrize("state", [gate_qt.STATE_FAILED, gate_qt.STATE_CHALLENGE])
def test_a_sign_in_that_did_not_complete_lets_nothing_through(state):
    target = _target()
    target.signing_in = True
    target.finished(state, "qa_sign_in_failed")
    # MUTATION: test `sign_ins > 0` (a sign-in was tried) instead of its state and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"


def test_a_failed_second_sign_in_takes_the_exception_back():
    target = _signed_in()
    assert _post(target, TOKEN_URL).action == "continue"
    # The session was lost; the hand-off asks again and that sign-in fails.
    target.handle(_args(target_id=target.target_id))
    # MUTATION: remember "signed in once" rather than reading the state now and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"
    target.signing_in = True
    target.finished(gate_qt.STATE_FAILED, "qa_sign_in_failed")
    assert _post(target, TOKEN_URL).action == "refuse"


def test_after_the_wipe_nothing_is_let_through():
    target = _signed_in()
    target.wipe()
    # MUTATION: leave `wiped` out of the exception's test and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"


@pytest.mark.parametrize("url", [
    f"{TOKEN_URL}?grant_type=refresh_token",       # a query
    f"{TOKEN_URL}?",                                # an empty query
    f"{TOKEN_URL}#x",                               # a fragment
    f"{TOKEN_URL}/",                                # a trailing slash
    f"{IDP}/realms//apps/protocol/openid-connect/token",   # a double slash
    f"{IDP}/{TOKEN_PATH}",                          # a leading double slash
    f"{IDP}/realms/apps/protocol/openid-connect/%74oken",  # an encoded letter
    f"{IDP}/realms/apps/protocol/openid-connect/token%2F",  # an encoded slash
    f"{IDP}/realms/apps/protocol/openid-connect/TOKEN",    # upper case
    f"{IDP}/realms/apps/protocol/openid-connect/./token",  # a dot segment
    f"{IDP}/realms/apps/protocol/x/../openid-connect/token",  # a dot-dot segment
    f"{IDP}:8443{TOKEN_PATH}",                      # another port
    f"{IDP}:443{TOKEN_PATH}",                       # the default port, spelled
    f"https://user@sso.example.net{TOKEN_PATH}",    # userinfo
    f"http://sso.example.net{TOKEN_PATH}",          # another scheme
    f"https://SSO.example.net{TOKEN_PATH.upper()}",  # an upper-case path on an upper-case host
    f"{IDP}/realms/apps/protocol/openid-connect/logout",   # another path
    f"{IDP}/realms/apps/protocol/openid-connect/token-x",  # a longer path
    f"{IDP}/realms/apps/protocol/openid-connect/toke",     # a prefix of it
])
def test_every_variant_of_the_token_url_is_refused(url):
    target = _signed_in()
    # MUTATION: compare `urlsplit(url).path` (or a prefix, or case-folded) instead of the whole URL
    # and one of these continues.
    assert _post(target, url).action == "refuse", url


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "post"])
def test_another_method_to_the_token_path_is_refused(method):
    target = _signed_in()
    verdict = _post(target, TOKEN_URL, method)
    if method == "post":
        # The method is read upper case, as every other rule reads it: this is POST.
        assert verdict.action == "continue"
        return
    # MUTATION: let any write method through on the token path and this continues.
    assert verdict.action == "refuse"


def test_the_token_path_on_the_base_origin_or_elsewhere_is_not_the_token_path():
    target = _signed_in()
    # MUTATION: match the path on any approved origin and the app's own write continues.
    assert _post(target, f"{ORIGIN}{TOKEN_PATH}").action == "refuse"
    # MUTATION: match the path alone, whatever the origin, and an outside origin is written to.
    assert _post(target, f"{ELSEWHERE}{TOKEN_PATH}").action == "refuse"


def test_a_redirect_of_the_token_post_is_judged_by_its_own_url():
    target = _signed_in()
    assert _post(target, TOKEN_URL).action == "continue"
    # Chromium pauses a 307's request again, at its new URL, with the method kept.
    # MUTATION: remember the allowed request id or origin and let its redirect through.
    assert _post(target, f"{IDP}/realms/apps/account").action == "refuse"
    assert _post(target, f"{ELSEWHERE}/collect").action == "refuse"


def test_a_top_frame_post_to_the_token_path_still_leaves_the_target():
    target = _signed_in()
    # A form posting the top frame to the IdP is a navigation off the target, not a token refresh.
    # MUTATION: check the token path before the top-frame rule and the page goes to the IdP.
    assert target.judge("POST", TOKEN_URL, top_document=True).action == "leave"


def test_a_target_with_no_token_path_is_unchanged():
    target = _signed_in(idp_token_path=None)
    # MUTATION: let any POST to the extra origin through after sign-in and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"
    assert _post(target, f"{ORIGIN}/graphql").action == "continue"
    assert _post(target, f"{ORIGIN}/api/items").action == "refuse"
    assert _post(target, f"{ELSEWHERE}/x").action == "refuse"
    assert target.judge("GET", TOKEN_URL, top_document=False).action == "continue"


def test_the_gates_own_sign_in_is_unchanged():
    target = _target()
    target.signing_in = True
    # The gate's own submission, as before: any write to an approved origin while it signs in.
    assert _post(target, f"{IDP}/realms/apps/login-actions/authenticate").action == "continue"
    assert _post(target, f"{ELSEWHERE}/x").action == "refuse"


# --------------------------------------------------------------------------
# The record names what it let through
# --------------------------------------------------------------------------


def test_an_allowed_token_refresh_is_recorded_apart_from_the_refusals():
    target = _signed_in()
    target.note_allowed(gate_qt.IDP_TOKEN_ALLOWED, "POST", TOKEN_URL)
    target.note_refusal(gate_qt.WRITE_REFUSED, "POST", f"{IDP}/realms/apps/x")
    record = target.refusals()
    # MUTATION: keep the allowed request among the refusals and the run blames the gate for it.
    assert [r["path"] for r in record["refusals"]] == ["/realms/apps/x"]
    # MUTATION: drop `allowed` from `refusals()` and a run cannot show what the gate let through.
    assert record["allowed"] == [{"n": 1, "at": 1_700_000_000.0, "method": "POST", "origin": IDP,
                                  "path": TOKEN_PATH, "code": "idp_token_refresh",
                                  "label": "allowed: IdP token refresh"}]
    assert record["allowed_dropped"] == 0


def test_the_allowed_record_is_bounded():
    target = _signed_in()
    for _ in range(gate_qt.MAX_REFUSALS + 3):
        target.note_allowed(gate_qt.IDP_TOKEN_ALLOWED, "POST", TOKEN_URL)
    record = target.refusals()
    # MUTATION: keep every allowed request and a long run's record grows without bound.
    assert len(record["allowed"]) == gate_qt.MAX_REFUSALS
    assert record["allowed_dropped"] == 3


class _Cdp:
    """The gate's pipe, as far as `_paused` uses it: what it was sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: Mapping[str, Any] | None = None,
                   session_id: str | None = None) -> dict[str, Any]:
        self.sent.append((method, dict(params or {})))
        return {}


def _paused_through_the_guard(target: gate_qt.QaTarget, url: str) -> tuple[_Cdp, list[str]]:
    cdp, events = _Cdp(), []
    guard = gate_qt.QaTargetGuard(cdp, target, record=lambda e, f: events.append(e),
                                  sleep=lambda s: asyncio.sleep(0))
    params = {"requestId": "R1", "resourceType": "Fetch", "frameId": "F1",
              "request": {"url": url, "method": "POST", "headers": {}}}
    asyncio.run(guard._paused("S1", params))
    return cdp, events


def test_the_guard_continues_the_token_post_and_records_it_as_allowed():
    target = _signed_in()
    cdp, events = _paused_through_the_guard(target, TOKEN_URL)
    # MUTATION: answer it as a refused write in `_paused` and the app's token refresh is a 403 again.
    assert [m for m, _ in cdp.sent] == ["Fetch.continueRequest"]
    # MUTATION: never call `note_allowed` in `_paused` and the report cannot name it.
    assert [r["path"] for r in target.refusals()["allowed"]] == [TOKEN_PATH]
    assert target.refusals()["refusals"] == []
    assert gate_qt.IDP_TOKEN_ALLOWED in events


def test_the_guard_still_refuses_the_token_post_before_sign_in():
    target = _target()
    cdp, _ = _paused_through_the_guard(target, TOKEN_URL)
    assert [m for m, _ in cdp.sent] == ["Fetch.fulfillRequest"]
    assert cdp.sent[0][1]["responseCode"] == 403
    assert target.refusals()["allowed"] == []
    assert [r["code"] for r in target.refusals()["refusals"]] == [gate_qt.WRITE_REFUSED]


# --------------------------------------------------------------------------
# The hand-off, checked again at the gate, and carried by the worker
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "realms/token", "/realms/token?x=1", "/realms/token#f", "/realms/../token", "/realms/./token",
    "/realms//token", "/realms/token/", "/realms/%74oken", "/realms/to ken", "/", "", 7,
    "/*", "/realms/*/token", "/" + "a" * 600,
])
def test_the_gate_refuses_a_token_path_that_is_not_one_plain_path(path):
    target = gate_qt.QaTarget(lambda e, f: None)
    # MUTATION: accept the hand-off's path unchecked (or as a glob) and this is taken.
    with pytest.raises(gate_qt.QaTargetRefused) as caught:
        target.handle(_args(idp_token_path=path))
    assert caught.value.code == "idp_token_path"
    assert not target.armed


def test_the_gate_refuses_a_token_path_with_no_extra_origin_to_put_it_on():
    target = gate_qt.QaTarget(lambda e, f: None)
    # MUTATION: put the path on the base origin when no extra origin was approved and the app is written to.
    with pytest.raises(gate_qt.QaTargetRefused) as caught:
        target.handle(_args(origins=[ORIGIN]))
    assert caught.value.code == "idp_token_path"


def test_the_gate_refuses_a_token_path_on_a_target_that_never_signs_in():
    args = _args(sign_in={"kind": "none"})
    args.pop("credential")
    target = gate_qt.QaTarget(lambda e, f: None)
    # MUTATION: accept it on a public target and nothing ever signs in to bound it.
    with pytest.raises(gate_qt.QaTargetRefused) as caught:
        target.handle(args)
    assert caught.value.code == "idp_token_path"


def _answer(**over: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "target_id": "qat_" + secrets.token_hex(8), "revision": 1, "base_url": f"{ORIGIN}/home",
        "origins": [ORIGIN, IDP], "environment": "production",
        "rate": {"navigation_ms": 2000, "requests_per_s": 10}, "read_posts": [],
        "exclusions": {"paths": [], "controls": []},
        "sign_in": {"kind": "own_login", "login_url": f"{ORIGIN}/login", "signed_in_selector": "#me",
                    "username": "qa-" + secrets.token_hex(4), "password": "pw-" + secrets.token_hex(12)},
    }
    answer.update(over)
    return answer


def test_the_worker_carries_the_token_path_to_the_gate():
    args = worker_qt.handoff_args(_answer(idp_token_path=TOKEN_PATH))
    # MUTATION: leave `idp_token_path` out of `handoff_args` and the gate never learns it.
    assert args["idp_token_path"] == TOKEN_PATH
    # And the gate takes what the worker hands it.
    target = gate_qt.QaTarget(lambda e, f: None)
    target.handle(args)
    assert target.idp_token_path == TOKEN_PATH


def test_the_worker_adds_no_token_path_swarm_api_did_not_serve():
    # MUTATION: default the field to "/" or "" and the gate refuses every hand-off, or allows one.
    assert "idp_token_path" not in worker_qt.handoff_args(_answer())
