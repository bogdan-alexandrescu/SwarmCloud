"""After the attempt ends the viewer is told the agent finished (GREY-TAIL; part of #1344).

MEASURED 2026-10-23 (batch mask1344-2026-10-23, task_33c888fd5fb947c99b74):
the gate closed Chromium at the attempt's end (`browser_wiped attempt_end`,
17:42:43.889) and stopped 12.26 s later; for all of that a person watching was
told "Can't show this page safely: the gate is not attached to the page on
screen", and the metrics booked it as `no_session`, a page failure. Owner
decision (BGD, 2026-10-23 ~20:20Z): "Say the agent finished."

So a wipe whose cause is the attempt's end gives the page watch (mask.py) its
own reason, `REASON_BROWSER_CLOSED`, said to the viewer as one sentence, and
counted under its own metrics key. Since LIVE-NOMASK (BGD, 2026-10-25) it is
the only reason a captured frame carries: any other way the session goes
publishes the capture with the plain desktop name, and the frame itself is
always the capture, never a fill.

The gate tests drive the real `LiveGate` over test_live_browser_relaunch's
scripted Chromium: no Chromium, no Xvfb, no network.
"""

from __future__ import annotations

import asyncio

import pytest
from test_live_browser_relaunch import Rig, _run
from test_live_rfb import _frame

from agent_worker.livegate import mask, rfb
from agent_worker.livegate.mask import MaskState

#: The owner's words (BGD, 2026-10-23), held here as written rather than read
#: from the module, so a rewording is a decision this test makes visible.
FINISHED = "The agent has finished; its browser is closed."


def _shown(state: MaskState) -> rfb.Published:
    """A captured frame as the gate's capture loop publishes it, with `state`'s reason."""
    hub = rfb.FrameHub(64, 40)
    hub.publish(_frame(), reason=state.reason)
    return hub.current


# --------------------------------------------------------------------------
# The gate: wipe() at the attempt's end, and every other cause
# --------------------------------------------------------------------------


def test_a_wipe_at_the_attempts_end_tells_the_viewer_the_agent_finished(tmp_path):
    async def body(rig: Rig) -> None:
        await rig.gate.wipe("attempt_end")
        await _until_closed(rig)
        frame = _shown(rig.gate.mask_state)
        # MUTATION: wipe() not calling browser_closed() for attempt_end gives no reason here.
        assert frame.reason == FINISHED
        # The frame is the capture, as every frame is (LIVE-NOMASK).
        assert frame.frame.rgb_at(0, 0) == (10, 20, 30)
        # MUTATION: desktop_name() putting a lead before it says "Can't show this page safely: ...".
        assert rfb.desktop_name(frame) == FINISHED.encode("utf-8")
        # MUTATION: dropping the record loses the line the tail is measured from.
        assert [r["cause"] for r in rig.recorded("browser_wiped")] == ["attempt_end"]

    _run(body, tmp_path)


def test_the_finished_reason_outlasts_the_page_followers_session_going_after_the_wipe(tmp_path):
    # Chromium's pipe closes after the wipe, and the page follower then loses
    # its session: that is the same closed browser.
    async def body(rig: Rig) -> None:
        await rig.gate.wipe("attempt_end")
        await _until_closed(rig)
        await asyncio.sleep(0.2)
        assert _shown(rig.gate.mask_state).reason == FINISHED

    _run(body, tmp_path)


@pytest.mark.parametrize("cause", ["cancelled", "parked", "attempt_ended", "ended"])
def test_a_wipe_for_any_other_cause_publishes_the_capture_with_the_plain_name(tmp_path, cause):
    async def body(rig: Rig) -> None:
        await rig.gate.wipe(cause)
        await _until_closed(rig)
        frame = _shown(rig.gate.mask_state)
        # MUTATION: wipe() calling browser_closed() for every cause says the agent finished on a cancel.
        assert frame.reason is None
        name = rfb.desktop_name(frame).decode("utf-8")
        assert name == "swarm live browser"
        assert not name.startswith(("Can't show this page safely", "Part of this page is covered"))

    _run(body, tmp_path)


async def _until_closed(rig: Rig) -> None:
    for _ in range(200):
        if rig.gate.browser_closed:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the gate never saw Chromium close")


# --------------------------------------------------------------------------
# The page watch: the reason, and nothing else
# --------------------------------------------------------------------------


def test_the_page_watch_names_the_closed_browser_and_nothing_else():
    assert _shown(MaskState()).reason is None

    closed = MaskState()
    closed.session_up()
    closed.browser_closed()
    # MUTATION: browser_closed() not setting the closed state gives no reason.
    assert mask.REASON_BROWSER_CLOSED == FINISHED
    assert _shown(closed).reason == FINISHED


def test_a_session_up_after_the_close_is_not_said_to_be_finished():
    state = MaskState()
    state.browser_closed()
    state.session_up()
    # MUTATION: session_up() not clearing the closed state carries "finished" into a later session.
    assert _shown(state).reason is None


# --------------------------------------------------------------------------
# The metrics: the tail is its own key, never no_session
# --------------------------------------------------------------------------


def test_viewer_metrics_book_the_tail_under_browser_closed():
    m = rfb.ViewerMetrics("v-1", now=0.0)
    m.frame(1.0, changed=True, at=1.0)
    m.frame(1.5, changed=True, at=1.5, reason=FINISHED)
    m.frame(3.0, changed=False, at=3.0, reason=FINISHED)
    line = m.snapshot(13.5, rfb.SessionCounts(), final=True)
    # MUTATION: no key for the reason books the 12 s tail under "other".
    assert line["unavailable_ms_by_reason"] == {"browser_closed": pytest.approx(12000, abs=1)}
    # MUTATION: the same, for the longest stretch.
    assert line["unavailable_longest_ms_by_reason"] == {"browser_closed": pytest.approx(12000, abs=1)}
    assert line["unavailable_ms"] == pytest.approx(12000, abs=1)
    assert line["blank_ms"] == line["unavailable_ms"]
    # MUTATION: mapping the reason to anything else counts the tail as something it is not.
    assert rfb.unavailable_reason_key(FINISHED) == "browser_closed"
