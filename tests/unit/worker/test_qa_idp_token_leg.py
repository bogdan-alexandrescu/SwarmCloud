"""Where an approved IdP token path goes, for one leg of a target with
`environments[]` (part of #2688: which of the three causes it is).

On 2026-11-02 a visual QA run on an own-login target whose legs sign in
through a Keycloak on another origin had the live gate refuse the app's
`POST <idp>/realms/<realm>/protocol/openid-connect/token` on every screen.
These hold the path end to end, from the stored target to the gate's
verdict, with no cloud credential and no emulator:

* (a) is NOT the cause: a path a platform admin approved on one leg of a
  migrated target reaches that leg's hand-off and the gate, which lets the
  exact POST through once its sign-in has completed; the other leg gets none;
* (c) reproduces the refusal: a leg approved with its extra origin and no
  token path is answered without one, and the gate refuses the same POST,
  `qa_write_refused`, exactly as the run recorded;
* the URL comparison is exact by design, so an approved path that differs from
  the app's request by case, a prefix or a port is refused too (a trailing
  slash cannot be approved at all): the approval must name the exact path.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from agent_worker import qa_target as worker_qt
from agent_worker.livegate import qatarget as gate_qt
from swarm_api import qaspec
from swarm_api.qatargets import QaTargetIssuer

APP = "https://studio.example.com"
IDP = "https://sso.example.net"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"
TOKEN_URL = f"{IDP}{TOKEN_PATH}"


def _entry(name: str, environment: str, token_path: str | None) -> dict[str, Any]:
    return {
        "name": name, "base_url": APP, "origin": APP, "host": APP.split("://", 1)[1],
        "environment": environment,
        "sign_in": {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me",
                    "extra_origin": IDP},
        "host_state": "approved", "approved_origins": [APP, IDP], "approved_by": "root@example.com",
        "approved_idp_token_path": token_path,
    }


def _target(staging_path: str | None) -> dict[str, Any]:
    return {
        "id": "qat_" + secrets.token_hex(8), "tenant_id": "eng", "name": "Studio", "revision": 3,
        "screens": [{"id": "home", "path": "/"}],
        "environments": [_entry("staging", "non_production", staging_path),
                         _entry("production", "production", None)],
        qaspec.MIGRATED: True,
    }


def _gate_for(target: dict[str, Any], leg: str) -> tuple[gate_qt.QaTarget, dict[str, Any]]:
    """The leg's answer as `QaTargetIssuer.issue` builds it, through the
    worker's hand-off, to a gate whose own sign-in has completed."""
    answer = QaTargetIssuer._answer(qaspec.leg(target, leg))
    answer["environment_name"] = leg
    # `issue` adds the credential read from Secret Manager: built here at run time.
    answer["sign_in"].update({"username": "qa-" + secrets.token_hex(4) + "@example.com",
                              "password": "pw-" + secrets.token_hex(12)})
    args = worker_qt.handoff_args(answer)
    gate = gate_qt.QaTarget(lambda _e, _f: None, wall=lambda: 1_700_000_000.0)
    gate.handle(args)
    gate.signing_in = True
    gate.finished(gate_qt.STATE_SIGNED_IN)
    return gate, args


def test_an_approved_path_on_one_leg_reaches_that_legs_gate():
    target = _target(TOKEN_PATH)
    gate, args = _gate_for(target, "staging")
    # MUTATION: drop approved_idp_token_path from qaspec.LEG_FIELDS and the leg's view loses it.
    assert args["idp_token_path"] == TOKEN_PATH
    assert gate.idp_token_url == TOKEN_URL
    verdict = gate.judge("POST", TOKEN_URL, top_document=False)
    assert verdict.action == "continue" and verdict.allowed == gate_qt.IDP_TOKEN_ALLOWED


def test_the_other_leg_gets_no_path_and_its_refresh_is_refused():
    gate, args = _gate_for(_target(TOKEN_PATH), "production")
    # One leg's approval never crosses to another (qa-environments.md §1.5).
    assert "idp_token_path" not in args
    assert gate.judge("POST", TOKEN_URL, top_document=False).action == "refuse"


def test_a_leg_approved_without_a_token_path_refuses_the_refresh_as_the_run_recorded():
    """Cause (c): the extra origin approved, no token path. The gate's sign-in
    completes, and every page load's token POST is refused, `qa_write_refused`."""
    gate, args = _gate_for(_target(None), "staging")
    assert "idp_token_path" not in args
    assert gate.state == gate_qt.STATE_SIGNED_IN
    verdict = gate.judge("POST", TOKEN_URL, top_document=False)
    assert verdict.action == "refuse"
    gate.note_refusal(gate_qt.WRITE_REFUSED, verdict.method, TOKEN_URL)
    (refusal,) = gate.refusals()["refusals"]
    assert (refusal["method"], refusal["origin"], refusal["path"], refusal["code"]) == (
        "POST", IDP, TOKEN_PATH, "qa_write_refused")


@pytest.mark.parametrize("approved", [TOKEN_PATH.upper(), "/auth" + TOKEN_PATH])
def test_an_approved_path_that_is_not_the_apps_request_is_refused(approved):
    gate, _args = _gate_for(_target(approved), "staging")
    # The comparison is the whole URL, by design: the approval names the exact path.
    assert gate.judge("POST", TOKEN_URL, top_document=False).action == "refuse"


def test_a_port_on_the_request_is_another_url():
    gate, _args = _gate_for(_target(TOKEN_PATH), "staging")
    assert gate.judge("POST", f"{IDP}:443{TOKEN_PATH}", top_document=False).action == "refuse"
