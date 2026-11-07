"""The approved IdP token POST is let through during the gate's own sign-in, not only after it.

On 3 November 2026 five production legs of one own-login target (their
provider on an approved extra origin, its `/realms/<realm>/protocol/
openid-connect/token` the approved token path) failed visual QA sign-in: the
gate refused 8 or 9 `POST <extra_origin><idp_token_path>` with nothing
allowed, and the app read "could not reach production sign-in (Failed to
fetch)". The staging leg of the same app signed in, its two refreshes let
through.

The cause: `CdpProxy.qa_sign_in` sends the agent's tab to `base_url` and
releases the agent after `signing_in` is cleared, and the target's state
becomes `signed_in` only once that is done (`QaTarget.finished`). An app
that exchanges or refreshes its token as its page loads (A peer application's
production chooser: "Checking your session...") sends that POST in the
window between the two, while neither "signing in" nor "signed in" held,
and the gate answered it 403 as a write. The app then keeps a saved
sign-in it could not refresh, and the next sign-in finds no 'Production'
button to click.

Held here: the exact approved token URL is let through, and recorded as an
IdP token refresh, from the moment the gate's own sign-in begins until it
ends, as well as after it completes; and nothing else is: no other path on
the provider's origin outside the gate's own steps, no other origin, no other
method, nothing before the sign-in begins, nothing after the wipe. (Since
the same day's later run, a sign-in that ended failed keeps it too:
test_qa_idp_token_production_legs.)

The end-to-end half is test_qa_target_signin's scripted Chromium and the real
`QaTarget`, `QaTargetGuard`, `SignIn` and `CdpProxy`. No Chromium, network or
cloud credential; every credential is built at run time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import qatarget as gate_qt

import test_qa_target_signin as qs

ORIGIN = qs.ORIGIN
IDP = qs.LOGIN_ORIGIN
ELSEWHERE = "https://pay.example.net"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"
TOKEN_URL = f"{IDP}{TOKEN_PATH}"
OTHER_IDP_PATH = f"{IDP}/realms/apps/account/credentials"


def _leg_args(**over: Any) -> dict[str, Any]:
    """A production leg: the app chooses Production, the provider on the
    approved extra origin, and its token path approved."""
    sign_in = {"kind": "own_login", "login_url": f"{ORIGIN}/?env=production", "signed_in_selector": "#account",
               "start_clicks": [{"text": "Production"}]}
    args = qs._args(qs._target_id(), qs._password(), sign_in=sign_in, idp_token_path=TOKEN_PATH)
    args.update(over)
    return args


def _target(**over: Any) -> gate_qt.QaTarget:
    target = gate_qt.QaTarget(lambda _e, _f: None, wall=lambda: 1_700_000_000.0)
    target.handle(_leg_args(**over))
    return target


def _post(target: gate_qt.QaTarget, url: str, method: str = "POST") -> gate_qt.Verdict:
    return target.judge(method, url, top_document=False)


# --------------------------------------------------------------------------
# The verdicts, through the sign-in's phases
# --------------------------------------------------------------------------


def test_the_token_post_on_the_gates_own_steps_is_allowed_and_recorded():
    target = _target()
    target.signing_in = True
    verdict = _post(target, TOKEN_URL)
    assert verdict.action == "continue"
    # MUTATION: judge writes during the sign-in before the token path and the
    # run's record cannot show the provider was reached while signing in.
    assert verdict.allowed == gate_qt.IDP_TOKEN_ALLOWED


def test_the_token_post_while_the_sign_in_hands_back_is_allowed():
    target = _target()
    target.signing_in = True
    # `CdpProxy.qa_sign_in`: the gate's tab closed, `signing_in` cleared, the
    # agent's tab sent to base_url and the agent released: `finished` not yet called.
    target.signing_in = False
    verdict = _post(target, TOKEN_URL)
    # MUTATION: require `state == signed_in` alone (the old rule) and the app's
    # token exchange on landing is refused: the production legs' failure.
    assert verdict.action == "continue"
    assert verdict.allowed == gate_qt.IDP_TOKEN_ALLOWED
    target.finished(gate_qt.STATE_SIGNED_IN)
    assert _post(target, TOKEN_URL).allowed == gate_qt.IDP_TOKEN_ALLOWED


def test_while_the_sign_in_hands_back_nothing_else_is_let_through():
    target = _target()
    target.signing_in = True
    target.signing_in = False
    # MUTATION: let any write to an approved origin through until `finished` and these continue.
    assert _post(target, OTHER_IDP_PATH).action == "refuse"
    assert _post(target, f"{ORIGIN}/api/items").action == "refuse"
    assert _post(target, f"{ORIGIN}{TOKEN_PATH}").action == "refuse"
    # MUTATION: match the path alone and another origin is written to.
    assert _post(target, f"{ELSEWHERE}{TOKEN_PATH}").action == "refuse"
    assert _post(target, f"{TOKEN_URL}?grant_type=password").action == "refuse"
    for method in ("PUT", "PATCH", "DELETE"):
        # MUTATION: let any write method through on the token path and this continues.
        assert _post(target, TOKEN_URL, method).action == "refuse", method
    # A top frame posted to the provider still leaves the target.
    assert target.judge("POST", TOKEN_URL, top_document=True).action == "leave"


def test_nothing_before_the_sign_in_begins():
    target = _target()
    # Handed over, the sign-in queued and not started.
    assert target.state == gate_qt.STATE_SIGNING_IN
    # MUTATION: allow on `state == signing_in` (the hand-off) and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"


@pytest.mark.parametrize("state", [gate_qt.STATE_FAILED, gate_qt.STATE_CHALLENGE])
def test_the_token_post_still_goes_once_a_sign_in_ended_without_completing(state):
    target = _target()
    target.signing_in = True
    target.signing_in = False
    target.finished(state, gate_qt.SIGN_IN_FAILED)
    assert not target.in_flight
    # MUTATION: hold the exception on `in_flight or state == signed_in` and
    # this is refused (part of #2688, test_qa_idp_token_production_legs).
    assert _post(target, TOKEN_URL).action == "continue"
    # A second hand-off queues a sign-in: the first one has begun, so it holds.
    target.handle(_leg_args(target_id=target.target_id))
    assert _post(target, TOKEN_URL).action == "continue"
    assert _post(target, OTHER_IDP_PATH).action == "refuse"


def test_nothing_after_the_wipe_even_mid_sign_in():
    target = _target()
    target.signing_in = True
    target.wipe()
    # MUTATION: leave `wiped` out of the in-flight test and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"


def test_a_target_with_no_token_path_gains_nothing_while_it_hands_back():
    target = _target(idp_token_path=None)
    target.signing_in = True
    target.signing_in = False
    assert _post(target, TOKEN_URL).action == "refuse"


# --------------------------------------------------------------------------
# End to end: a leg whose app exchanges its token as the page loads
# --------------------------------------------------------------------------


class AppChromium(qs.LoginChromium):
    """A login page Chromium whose app POSTs as a page loads: `on_navigate`
    runs for every `Page.navigate`, with its URL, before it is answered."""

    def __init__(self) -> None:
        super().__init__()
        self.on_navigate: Any = None

    async def send(self, message: dict[str, Any]) -> None:
        if message["method"] == "Page.navigate" and self.on_navigate is not None:
            self.on_navigate(message["params"]["url"])
        await super().send(message)

    def post(self, session: str, url: str, frame: str) -> str:
        """The page's script POSTs `url` (a fetch: not the top frame's document)."""
        request_id = self._next("R")
        params: dict[str, Any] = {
            "requestId": request_id, "frameId": frame, "resourceType": "Fetch", "networkId": self._next("N"),
            "request": {"url": url, "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}},
        }
        self.paused[request_id] = (session, params)
        self.emit("Fetch.requestPaused", params, session)
        return request_id


def _guarded(rig: qs.Rig, target_id: str) -> str:
    return next(sid for sid, s in rig.proxy._qa_guard._sessions.items()
                if s.kind == "page" and s.target_id == target_id)


def _gate_tab(rig: qs.Rig) -> str:
    return next(s.target_id for s in rig.proxy._qa_guard._sessions.values()
                if s.kind == "page" and s.target_id.startswith("TAB"))


def test_a_leg_whose_app_needs_the_token_post_on_its_own_sign_in_signs_in(tmp_path: Path, monkeypatch):
    # The rig builds its Chromium by this name: this one's app POSTs as it loads.
    monkeypatch.setattr(qs, "LoginChromium", AppChromium)

    async def body() -> None:
        rig = qs.Rig(tmp_path)
        fake = rig.fake
        assert isinstance(fake, AppChromium)
        fake.start(rig.sink)
        fake.controls = {"Production": 1}
        fake.probes = ["loading"]

        def choose_production() -> None:
            # The app reaches the provider as Production is chosen, on the gate's tab.
            tab = _gate_tab(rig)
            fake.post(_guarded(rig, tab), TOKEN_URL, tab)
            fake.probes = ["form"]

        def landed(url: str) -> None:
            if url != qs.BASE_URL:
                return
            # The agent's tab loads the app, which exchanges its token at once,
            # and (the negative controls) writes elsewhere on the provider and off it.
            session = _guarded(rig, "PAGE1")
            fake.post(session, TOKEN_URL, "PAGE1")
            fake.post(session, OTHER_IDP_PATH, "PAGE1")
            fake.post(session, f"{ELSEWHERE}{TOKEN_PATH}", "PAGE1")

        fake.on_click["Production"] = choose_production
        fake.on_navigate = landed
        await gate_qt.handle_ctl(rig.target, rig.proxy, _leg_args())
        await rig.finished()
        await rig.settle()
        status = rig.target.status()
        assert status["qa_sign_in"] == "signed_in", status
        assert fake.clicked == ["Production"]
        reached = [url for url, _ in fake.network]
        # MUTATION: keep the old rule (`state == signed_in` only) and the
        # landing exchange is answered 403: the app reads "Failed to fetch".
        assert reached.count(TOKEN_URL) == 2, reached
        record = rig.target.refusals()
        assert [r["path"] for r in record["allowed"]] == [TOKEN_PATH, TOKEN_PATH]
        # Nothing else was let through: the provider's other path and the other origin were answered by the gate.
        assert OTHER_IDP_PATH not in reached and f"{ELSEWHERE}{TOKEN_PATH}" not in reached
        assert sorted((r["origin"], r["path"]) for r in record["refusals"]) == sorted(
            [(IDP, "/realms/apps/account/credentials"), (ELSEWHERE, TOKEN_PATH)])
        await fake.stop()

    asyncio.run(body())

