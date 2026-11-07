"""The agent's tab lands after the gate's sign-in is said complete (QA-IDP-POST; part of #2688).

`CdpProxy.qa_sign_in` sends the agent's tab to the target's base URL once
its own sign-in tab has closed. An app that refreshes its IdP token on load
(Keycloak's `/protocol/openid-connect/token`) POSTs it during that first
load. The sign-in's outcome was recorded on the target only after
`qa_sign_in` returned, so that POST met `signing_in` with the sign-in over
and was refused even on a target whose platform admin approved the path,
and the agent's first page was the app's session-error card. Held here with
a fake proxy and a fake sign-in: no Chromium, no cloud.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import pytest

from agent_worker.livegate import cdpproxy as cdpproxy_mod
from agent_worker.livegate import qatarget as gate_qt

ORIGIN = "https://app.example.com"
IDP = "https://sso.example.net"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"
TOKEN_URL = f"{IDP}{TOKEN_PATH}"


def _target() -> gate_qt.QaTarget:
    target = gate_qt.QaTarget(lambda _e, _f: None, wall=lambda: 1_700_000_000.0)
    target.handle({
        "target_id": "qat_" + secrets.token_hex(8), "base_url": f"{ORIGIN}/home", "origins": [ORIGIN, IDP],
        "sign_in": {"kind": "own_login", "login_url": f"{IDP}/login", "signed_in_selector": "#me"},
        # Built at runtime: nothing here is a literal credential.
        "credential": {"username": "qa-" + secrets.token_hex(4) + "@example.com",
                       "password": "pw-" + secrets.token_hex(12)},
        "idp_token_path": TOKEN_PATH,
    })
    return target


class _SignIn:
    outcome: tuple[str, str | None] = (gate_qt.STATE_SIGNED_IN, None)

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def run(self) -> tuple[str, str | None]:
        return self.outcome

    async def close(self) -> None:
        return None


class _Proxy:
    """What `CdpProxy.qa_sign_in` and `_run_qa_sign_in` read of their proxy."""

    def __init__(self, target: gate_qt.QaTarget) -> None:
        self._qa_target = target
        self._mode = cdpproxy_mod._OPEN
        self._controlled = None
        self._qa_signing = False
        self._gone = False
        self.landed: list[gate_qt.Verdict] = []

    def _wall(self) -> float:
        return 1_700_000_000.0

    async def _qa_sleep(self, _seconds: float) -> None:
        return None

    async def pause(self) -> None:
        return None

    async def resume(self) -> None:
        return None

    async def _qa_land(self, _url: str) -> None:
        # The app's first load on the agent's tab refreshes its token.
        self.landed.append(self._qa_target.judge("POST", TOKEN_URL, top_document=False))

    qa_sign_in = cdpproxy_mod.CdpProxy.qa_sign_in
    _run_qa_sign_in = cdpproxy_mod.CdpProxy._run_qa_sign_in


@pytest.fixture(autouse=True)
def _fake_sign_in(monkeypatch):
    monkeypatch.setattr(cdpproxy_mod, "SignIn", _SignIn)


def test_the_token_refresh_on_the_landing_load_continues_after_a_completed_sign_in():
    target = _target()
    proxy = _Proxy(target)
    asyncio.run(proxy._run_qa_sign_in())
    (verdict,) = proxy.landed
    # MUTATION: record the outcome only after qa_sign_in returns and this is "refuse".
    assert verdict.action == "continue" and verdict.allowed == gate_qt.IDP_TOKEN_ALLOWED
    assert target.state == gate_qt.STATE_SIGNED_IN


@pytest.mark.parametrize("outcome", [(gate_qt.STATE_FAILED, gate_qt.SIGN_IN_FAILED),
                                     (gate_qt.STATE_CHALLENGE, gate_qt.CHALLENGE)])
def test_a_sign_in_that_did_not_complete_is_recorded_before_the_landing(monkeypatch, outcome):
    monkeypatch.setattr(_SignIn, "outcome", outcome)
    target = _target()
    proxy = _Proxy(target)
    asyncio.run(proxy._run_qa_sign_in())
    (verdict,) = proxy.landed
    # The approved token path continues in every sign-in state (the gate's rule since the
    # IdP-token-during-sign-in change); what this change adds is the outcome on the target
    # before the landing load, so that load's records read failed or challenge, not signing_in.
    assert verdict.action == "continue" and verdict.allowed == gate_qt.IDP_TOKEN_ALLOWED
    assert (target.state, target.cause) == outcome


def test_the_outcome_is_recorded_once():
    records: list[str] = []
    target = _target()
    target._record = lambda event, _fields: records.append(event)
    asyncio.run(_Proxy(target)._run_qa_sign_in())
    # MUTATION: call `finished` in both qa_sign_in and _run_qa_sign_in and this is two.
    assert records.count("qa_sign_in_finished") == 1
