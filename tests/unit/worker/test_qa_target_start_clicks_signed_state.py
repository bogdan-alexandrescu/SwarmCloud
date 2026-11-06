"""The gate's start clicks and its signed-in verdict, read off the page (part of #2688).

AI Studio's staging quality-check run of 4 November 2026 (about 11:53 UTC):

(1) THE REVIEWER LEG FOUND NO FORM AFTER ITS START CLICKS. The chooser
    ("Sign in to AI Studio", Staging | Production, then "Sign in") is drawn
    by the server before its script runs, and the gate's own rate limit
    slows the rest of the load. The gate clicked each control the moment it
    was visible, so a click could land before the page listened, did
    nothing, and nothing clicked again: the form never appeared, and the
    cause said only `qa_sign_in_no_form`. Now a click waits for the page to
    finish loading, the first control is waited for as long as a form is
    (FORM_SECONDS), clicks the page did not act on are made again, a page
    that is already the form (an entry query) skips them, and the cause's
    sentence says which click matched nothing or that the form never
    appeared after them, and where the tab is.
(2) THE MODERATOR LEG READ AS FAILED WHILE SIGNED IN. The verdict was
    `signed_in_selector` within 30 s and nothing else. Now it is the page's
    own state: the selector, or the target's own origin settled with no
    login form, no OAuth callback and not back on the start page reads as
    signed in; the form, an error or the start page reads as failed, with a
    sentence; a page still loading is waited for up to SIGNED_IN_MAX_SECONDS.

The real `QaTarget`, `SignIn` and `CdpProxy` on test_qa_target_signin's
scripted Chromium, its clock moved only by the gate's own sleeps. No
Chromium, network or cloud credential; every credential is built at run time.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_worker import qa_target as worker_qt
from agent_worker.livegate import qatarget as gate_qt

from test_qa_target_signin import (  # noqa: F401  (tmp is a fixture)
    LOGIN_ORIGIN, ORIGIN, Rig, _chooser_args, _password, _username, run, tmp,
)

IDP_PAGE = f"{LOGIN_ORIGIN}/realms/studio/protocol/openid-connect/auth"


async def _sign_in(rig: Rig, args: dict[str, Any]) -> dict[str, Any]:
    rig.fake.start(rig.sink)
    await gate_qt.handle_ctl(rig.target, rig.proxy, args)
    await rig.finished()
    return rig.target.status()


def _no_secret(rig: Rig, password: str, username: str) -> None:
    status = json.dumps(rig.target.status(), ensure_ascii=False)
    records = json.dumps(rig.records, ensure_ascii=False)
    assert password not in status and username not in status
    assert password not in records and username not in records


# --------------------------------------------------------------------------
# (1) Start clicks
# --------------------------------------------------------------------------


def test_a_chooser_drawn_after_the_click_window_is_still_clicked_and_signs_in(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        t0 = rig.clock.now

        def page(submitted: bool) -> str:
            # The chooser is drawn 15 s into the load (past START_CLICK_SECONDS).
            if rig.clock.now - t0 >= 15:
                rig.fake.controls = {"Staging": 1, "Sign in": 1}
            if submitted:
                return "signed_in"
            return "form" if "Sign in" in rig.fake.clicked else "loading"

        rig.fake.probe_fn = page
        status = await _sign_in(rig, _chooser_args(_password()))
        # MUTATION: wait START_CLICK_SECONDS for the first control and this ends start_click_missing.
        assert status["qa_sign_in"] == "signed_in", status
        assert rig.fake.clicked == ["Staging", "Sign in"]
        await rig.fake.stop()

    run(body)


def test_clicks_the_page_did_not_act_on_are_made_again_and_the_late_form_signs_in(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.controls = {"Staging": 1, "Sign in": 1}
        presses: list[float] = []

        def press() -> None:
            presses.append(rig.clock.now)

        rig.fake.on_click["Sign in"] = press

        def after_click() -> str:
            # The first press landed before the page's script ran: nothing happened.
            return "moved" if len(presses) >= 2 else "same"

        def page(submitted: bool) -> str:
            if submitted:
                return "signed_in"
            # The IdP's form is drawn 8 s after the press the page acted on.
            return "form" if len(presses) >= 2 and rig.clock.now - presses[1] >= 8 else "loading"

        rig.fake.after_click = after_click
        rig.fake.probe_fn = page
        password, username = _password(), _username()
        status = await _sign_in(rig, _chooser_args(password, username))
        # MUTATION: never click again in `_wait_form` and this ends qa_sign_in_no_form.
        assert status["qa_sign_in"] == "signed_in", status
        # The clicks were made again in order, the toggle before the button.
        assert rig.fake.clicked == ["Staging", "Sign in", "Staging", "Sign in"]
        assert [t for _, t in rig.fake.typed] == [username, password]
        _no_secret(rig, password, username)
        await rig.fake.stop()

    run(body)


def test_an_entry_query_that_lands_on_the_form_skips_the_start_clicks(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        # `?env=staging` took the app past its chooser: the login page is the form.
        rig.fake.controls = {}
        rig.fake.probes = ["form"]
        status = await _sign_in(rig, _chooser_args(_password()))
        # MUTATION: look for the click's control before the form and this ends start_click_missing.
        assert status["qa_sign_in"] == "signed_in", status
        assert rig.fake.clicked == []
        await rig.fake.stop()

    run(body)


@pytest.mark.parametrize("missing", ["Staging", "Sign in"])
def test_a_start_click_that_matches_nothing_is_named(tmp, missing):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.controls = {"Staging": 1, "Sign in": 1, missing: 0}
        rig.fake.probes = ["loading"]
        status = await _sign_in(rig, _chooser_args(_password()))
        assert status["qa_sign_in"] == "failed" and status["cause"] == gate_qt.START_CLICK_MISSING
        assert f"start click '{missing}' found none visible controls" in status["sentence"]
        # The first control is waited for as a form is; a later one for START_CLICK_SECONDS.
        seconds = gate_qt.FORM_SECONDS if missing == "Staging" else gate_qt.START_CLICK_SECONDS
        # MUTATION: wait START_CLICK_SECONDS for the first control and the sentence says 10 s.
        assert f"in {seconds:.0f} s" in status["sentence"]
        assert rig.fake.typed == [] and rig.fake.closed and not rig.proxy.paused
        await rig.fake.stop()

    run(body)


def test_a_form_that_never_appears_after_the_clicks_says_so_and_where_the_tab_is(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.controls = {"Staging": 1, "Sign in": 1}
        rig.fake.probes = ["loading"]
        rig.fake.where = IDP_PAGE
        status = await _sign_in(rig, _chooser_args(_password()))
        assert status["qa_sign_in"] == "failed" and status["cause"] == gate_qt.NO_FORM
        # MUTATION: leave `_no_form_sentence` out and the cause says nothing of the clicks.
        assert status["sentence"].startswith(
            "after the start clicks 'Staging', 'Sign in' the login form never appeared")
        assert IDP_PAGE in status["sentence"]
        # A page that moved on is not clicked again.
        assert rig.fake.clicked == ["Staging", "Sign in"]
        # The worker passes the gate's sentence on to the agent's notice.
        assert worker_qt._SENTENCE.fullmatch(status["sentence"])
        assert status["sentence"] in worker_qt.notice(status["cause"], status["sentence"])
        await rig.fake.stop()

    run(body)


def test_a_page_that_never_acts_on_its_clicks_is_clicked_at_most_three_times_and_said(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.controls = {"Staging": 1, "Sign in": 1}
        rig.fake.probes = ["loading"]
        rig.fake.after_click = "same"
        rig.fake.where = f"{ORIGIN}/"
        t0 = rig.clock.now
        status = await _sign_in(rig, _chooser_args(_password()))
        assert status["cause"] == gate_qt.NO_FORM
        # MUTATION: drop the MAX_RECLICKS bound and the clicks go on.
        assert rig.fake.clicked == ["Staging", "Sign in"] * (1 + gate_qt.MAX_RECLICKS)
        assert "did not change after 'Sign in'" in status["sentence"] and f"{ORIGIN}/" in status["sentence"]
        assert rig.clock.now - t0 <= gate_qt.FORM_SECONDS * 2
        await rig.fake.stop()

    run(body)


def test_a_selector_the_page_cannot_take_is_named_rather_than_read_as_no_form(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.probes = ["bad:signed_in_selector"]
        status = await _sign_in(rig, _chooser_args(_password(), clicks=None) | {"sign_in": {
            "kind": "own_login", "login_url": f"{ORIGIN}/login", "signed_in_selector": "#account"}})
        assert status["cause"] == gate_qt.NO_FORM
        # MUTATION: return the probe's state unread and the cause names no selector.
        assert "signed_in_selector is not a CSS selector" in status["sentence"]
        await rig.fake.stop()

    run(body)


def test_the_probe_reads_each_selector_apart_and_the_page_state_after_the_submit():
    probe = gate_qt._js(gate_qt._PROBE, {})
    # A selector that throws is caught and named, never the whole probe.
    assert "catch (e) { if (!bad.includes(key)) bad.push(key)" in probe
    assert "'bad:' + bad.join(',')" in probe
    # The signed-in element is looked for in open shadow roots too.
    assert "el.shadowRoot" in probe
    # After the submit: the form, an error, a load or a callback, the start page, the app.
    for state in ("'error'", "'form'", "'pending'", "'chooser'", "'app'"):
        assert f"return {state}" in probe or f"? {state}" in probe or f": {state}" in probe, state
    click = gate_qt._js(gate_qt._START_CLICK, {})
    # MUTATION: click before the page finished loading and this fails.
    assert "document.readyState !== 'complete' && !a.now) return -2" in click
    assert ".replace(/\\s+/g, ' ').trim()" in click


# --------------------------------------------------------------------------
# (2) The verdict after the submit
# --------------------------------------------------------------------------


def _after(rig: Rig, script: list[tuple[float, str]]) -> None:
    """After the submit the page reads as each state from its second on."""
    submitted_at: list[float] = []

    def page(submitted: bool) -> str:
        if not submitted:
            return "form"
        if not submitted_at:
            submitted_at.append(rig.clock.now)
        elapsed = rig.clock.now - submitted_at[0]
        return [state for at, state in script if elapsed >= at][-1]

    rig.fake.probe_fn = page


def _plain_args(password: str, username: str) -> dict[str, Any]:
    return _chooser_args(password, username) | {"sign_in": {
        "kind": "own_login", "login_url": f"{LOGIN_ORIGIN}/login", "signed_in_selector": "#account"}}


@pytest.mark.parametrize("landing", ["app", "signed_in"])
def test_a_slow_redirect_that_lands_signed_in_reads_signed_in(tmp, landing):
    async def body() -> None:
        rig = Rig(tmp)
        # The rate-limited redirect chain and the app's load take 40 s.
        _after(rig, [(0, "form"), (2, "pending"), (40, landing)])
        password, username = _password(), _username()
        status = await _sign_in(rig, _plain_args(password, username))
        # MUTATION: judge at SIGNED_IN_SECONDS whatever is loading and this is failed.
        assert status["qa_sign_in"] == "signed_in", status
        assert rig.clock.now - 1000.0 <= gate_qt.FORM_SECONDS + gate_qt.SIGNED_IN_MAX_SECONDS
        _no_secret(rig, password, username)
        await rig.fake.stop()

    run(body)


def test_the_app_drawn_without_the_selector_reads_signed_in_once_settled(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        # The moderator lands on a page that does not draw `signed_in_selector`.
        _after(rig, [(0, "loading"), (5, "app")])
        status = await _sign_in(rig, _plain_args(_password(), _username()))
        # MUTATION: read only `signed_in_selector` and this is failed.
        assert status["qa_sign_in"] == "signed_in", status
        assert "cause" not in status and "sentence" not in status
        await rig.fake.stop()

    run(body)


def test_an_app_page_seen_for_a_moment_between_redirects_is_not_the_verdict(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        _after(rig, [(0, "app"), (1, "form")])
        status = await _sign_in(rig, _plain_args(_password(), _username()))
        # MUTATION: take one `app` reading as signed in and this is signed_in.
        assert status["qa_sign_in"] == "failed" and status["cause"] == gate_qt.SIGN_IN_FAILED
        await rig.fake.stop()

    run(body)


def test_a_submit_that_stays_on_the_form_reads_failed_and_says_so(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.after_submit = "form"
        password, username = _password(), _username()
        status = await _sign_in(rig, _plain_args(password, username))
        assert status["qa_sign_in"] == "failed" and status["cause"] == gate_qt.SIGN_IN_FAILED
        # MUTATION: leave `_failed_sentence` out and the cause does not say the form stayed.
        assert status["sentence"].startswith("after the submit the page still shows the login form")
        _no_secret(rig, password, username)
        await rig.fake.stop()

    run(body)


def test_an_error_on_the_form_reads_failed_without_waiting_the_window_out(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.after_submit = "error"
        t0 = rig.clock.now
        status = await _sign_in(rig, _plain_args(_password(), _username()))
        assert status["qa_sign_in"] == "failed" and status["cause"] == gate_qt.SIGN_IN_FAILED
        assert "shows an error" in status["sentence"]
        # MUTATION: wait SIGNED_IN_SECONDS out on an error and the clock runs past this.
        assert rig.clock.now - t0 < gate_qt.SIGNED_IN_SECONDS
        await rig.fake.stop()

    run(body)


def test_landing_back_on_the_start_page_reads_failed_and_names_its_control(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.controls = {"Staging": 1, "Sign in": 1}
        rig.fake.probes = ["loading"]
        rig.fake.on_click["Sign in"] = lambda: setattr(rig.fake, "probes", ["form"])
        rig.fake.after_submit = "chooser"
        status = await _sign_in(rig, _chooser_args(_password()))
        assert status["qa_sign_in"] == "failed" and status["cause"] == gate_qt.SIGN_IN_FAILED
        assert "back on its start page, showing 'Sign in'" in status["sentence"]
        # The probe was told which control the start page shows: the last click's.
        probes = [json.loads(m["params"]["expression"].rsplit(")(", 1)[1][:-1]) for m in rig.fake.received
                  if str((m.get("params") or {}).get("expression", "")).startswith("/*swarm-qa:probe")]
        after = [p for p in probes if p.get("after")]
        assert after and after[-1]["chooser"] == {"text": "Sign in"} and after[-1]["home"] == ORIGIN
        await rig.fake.stop()

    run(body)


def test_a_page_still_loading_is_waited_for_no_longer_than_the_bound(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        _after(rig, [(0, "pending")])
        rig.fake.where = f"{ORIGIN}/callback"
        submitted = []
        rig.fake.on_first_probe = lambda: submitted.append(rig.clock.now)
        status = await _sign_in(rig, _plain_args(_password(), _username()))
        assert status["qa_sign_in"] == "failed" and "still loading" in status["sentence"]
        assert f"{ORIGIN}/callback" in status["sentence"]
        # MUTATION: wait on a loading page without SIGNED_IN_MAX_SECONDS and this never ends.
        assert rig.clock.now - submitted[0] <= gate_qt.SIGNED_IN_MAX_SECONDS + 5
        await rig.fake.stop()

    run(body)


def test_the_worker_awaits_the_gates_whole_bound_for_a_form_and_its_verdict():
    # MUTATION: keep FIRST_WAIT_SECONDS at 75 and the runner starts while the gate still reads the page.
    assert worker_qt.FIRST_WAIT_SECONDS >= gate_qt.FORM_SECONDS + gate_qt.SIGNED_IN_MAX_SECONDS
