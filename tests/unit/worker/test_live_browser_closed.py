"""After the attempt ends the viewer is told the agent finished (GREY-TAIL; part of #1344).

MEASURED 2026-10-23 (batch mask1344-2026-10-23, task_33c888fd5fb947c99b74):
the gate closed Chromium at the attempt's end (`browser_wiped attempt_end`,
17:42:43.889) and stopped 12.26 s later; for all of that a person watching was
told "Can't show this page safely: the gate is not attached to the page on
screen", and the metrics booked it as `no_session`, a page failure. Owner
decision (BGD, 2026-10-23 ~20:20Z): "Say the agent finished."

So a wipe whose cause is the attempt's end gives the mask its own reason,
`REASON_BROWSER_CLOSED`, said to the viewer as one sentence with no lead, and
counted under its own metrics key. Every other way the session goes keeps
`no_session`.

The gate tests drive the real `LiveGate` over test_live_browser_relaunch's
scripted Chromium: no Chromium, no Xvfb, no network.
"""

from __future__ import annotations

import asyncio

import pytest
from test_live_browser_relaunch import Rig, _run
from test_live_rfb import _frame

from agent_worker.livegate import mask, rfb
from agent_worker.livegate.mask import CANNOT_SHOW, MaskState, Outcome

#: The owner's words (BGD, 2026-10-23), held here as written rather than read
#: from the module, so a rewording is a decision this test makes visible.
FINISHED = "The agent has finished; its browser is closed."


def _shown(state: MaskState) -> rfb.MaskedFrame:
    return rfb.masked(_frame(), state)


# --------------------------------------------------------------------------
# The gate: wipe() at the attempt's end, and every other cause
# --------------------------------------------------------------------------


def test_a_wipe_at_the_attempts_end_tells_the_viewer_the_agent_finished(tmp_path):
    async def body(rig: Rig) -> None:
        await rig.gate.wipe("attempt_end")
        await _until_closed(rig)
        frame = _shown(rig.gate.mask_state)
        # MUTATION: wipe() calling session_down() for attempt_end gives REASON_NO_SESSION here.
        assert frame.reason == FINISHED
        # MUTATION: a closed browser shown as the page would leave pixels the mask never vouched for.
        assert frame.outcome is Outcome.UNAVAILABLE
        # MUTATION: desktop_name() putting CANNOT_SHOW before it says "Can't show this page safely: ...".
        assert rfb.desktop_name(frame) == FINISHED.encode("utf-8")
        # MUTATION: dropping the record loses the line the tail is measured from.
        assert [r["cause"] for r in rig.recorded("browser_wiped")] == ["attempt_end"]

    _run(body, tmp_path)


def test_the_finished_reason_outlasts_the_session_going_after_the_wipe(tmp_path):
    # Chromium's pipe closes after the wipe, and the page follower then says its
    # session went: that is the same closed browser, not a page failure.
    async def body(rig: Rig) -> None:
        await rig.gate.wipe("attempt_end")
        await _until_closed(rig)
        await asyncio.sleep(0.2)
        rig.gate.mask_state.session_down()
        # MUTATION: session_down() clearing the closed state brings back REASON_NO_SESSION for the tail.
        assert _shown(rig.gate.mask_state).reason == FINISHED

    _run(body, tmp_path)


@pytest.mark.parametrize("cause", ["cancelled", "parked", "attempt_ended", "ended"])
def test_a_wipe_for_any_other_cause_still_says_no_session(tmp_path, cause):
    async def body(rig: Rig) -> None:
        await rig.gate.wipe(cause)
        await _until_closed(rig)
        frame = _shown(rig.gate.mask_state)
        # MUTATION: wipe() calling browser_closed() for every cause says the agent finished on a cancel.
        assert frame.reason == mask.REASON_NO_SESSION
        # MUTATION: desktop_name() dropping the lead for every reason loses "Can't show this page safely".
        assert rfb.desktop_name(frame).decode("utf-8") == f"{CANNOT_SHOW}: {mask.REASON_NO_SESSION}"

    _run(body, tmp_path)


async def _until_closed(rig: Rig) -> None:
    for _ in range(200):
        if rig.gate.browser_closed:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the gate never saw Chromium close")


# --------------------------------------------------------------------------
# The mask: the reason, and nothing else changes
# --------------------------------------------------------------------------


def test_the_mask_names_the_closed_browser_and_session_down_keeps_no_session():
    lost = MaskState()
    lost.session_up()
    lost.session_down()
    # MUTATION: session_down() setting the closed reason says the agent finished on any lost session.
    assert _shown(lost).reason == mask.REASON_NO_SESSION

    closed = MaskState()
    closed.session_up()
    closed.browser_closed()
    # MUTATION: browser_closed() only clearing the session gives REASON_NO_SESSION.
    assert mask.REASON_BROWSER_CLOSED == FINISHED
    assert _shown(closed).reason == FINISHED


def test_the_closed_browser_beats_a_tab_switch_held_when_it_closed():
    state = MaskState()
    state.session_up()
    state.hold(mask.REASON_SWITCHING)
    state.browser_closed()
    # MUTATION: checking `_held` before the closed browser says "switching tabs" until the pod stops.
    assert _shown(state).reason == FINISHED


def test_a_session_up_after_the_close_is_not_said_to_be_finished():
    state = MaskState()
    state.browser_closed()
    state.session_up()
    state.session_down()
    # MUTATION: session_up() not clearing the closed state carries "finished" into a later session.
    assert _shown(state).reason == mask.REASON_NO_SESSION


# --------------------------------------------------------------------------
# The metrics: the tail is its own key, never no_session
# --------------------------------------------------------------------------


def test_viewer_metrics_book_the_tail_under_browser_closed():
    m = rfb.ViewerMetrics("v-1", now=0.0, outcome=Outcome.MASKED)
    m.frame(1.0, changed=True, outcome=Outcome.UNAVAILABLE, at=1.0, reason=mask.REASON_NO_SESSION)
    m.frame(1.5, changed=True, outcome=Outcome.UNAVAILABLE, at=1.5, reason=FINISHED)
    m.frame(3.0, changed=False, outcome=Outcome.UNAVAILABLE, at=3.0, reason=FINISHED)
    line = m.snapshot(13.5, rfb.SessionCounts(), final=True)
    # MUTATION: no key for the reason books the 12 s tail under "other".
    assert line["unavailable_ms_by_reason"] == {
        "no_session": pytest.approx(500, abs=1), "browser_closed": pytest.approx(12000, abs=1)}
    # MUTATION: the same, for the longest stretch.
    assert line["unavailable_longest_ms_by_reason"] == {
        "no_session": pytest.approx(500, abs=1), "browser_closed": pytest.approx(12000, abs=1)}
    # MUTATION: mapping the reason to "no_session" counts the tail as a page failure.
    assert rfb.unavailable_reason_key(FINISHED) == "browser_closed"
