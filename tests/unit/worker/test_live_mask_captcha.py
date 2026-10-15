"""The mask keeps a reCAPTCHA visible through a hand-off, and blanks less (issue #1340).

docs/design/live-browser.md §5.3. MEASURED 2026-10-15 on
task_2ac5d7e22a51460995a2, release b93ad6cb: `live mask: frame owner
unresolved (CdpError)` twice at attempt start; both reCAPTCHA frames stayed
unlinked, so their boxes stayed covered for the whole hand-off, and the page
went PAGE_BLANKED 0.3-1.2 s in every metrics window while it kept changing.
Owner decisions 2026-10-14, all three built here:

1. a failed `MaskCdp.link` is retried on the parent's next fresh report and on
   the child's next navigation, with a bounded backoff;
2. while a person holds control, a child frame on an exact allowlist of
   challenge hosts is shown when it cannot vouch for itself -- never the top
   frame, never while the agent drives -- and each one is recorded, host only;
3. a top frame stale only because of a layout change keeps the last masked
   frame on screen for up to `HOLD_CHANGING_SECONDS` before blanking.

Every case builds its frames and reports by hand: no browser, no display.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from agent_worker.livegate.capture import Frame
from agent_worker.livegate.cdpproxy import CdpError
from agent_worker.livegate.mask import (
    BINDING_NAME,
    HOLD_CHANGING_SECONDS,
    LINK_TRIES,
    MASK_RGB,
    REASON_CHANGING,
    REASON_SHOWN_IN_CONTROL,
    WORLD_NAME,
    MaskCdp,
    MaskState,
    Outcome,
)

from test_livegate_main import short_tmp  # noqa: F401  (a pytest fixture, used by name)

WHITE = (0xFF, 0xFF, 0xFF)
GREEN = (0x10, 0xC0, 0x10)
TOP = "frame-top"
CHILD = "frame-captcha"
DOC = "doc-1"
SCREEN_W, SCREEN_H = 200, 150
PAGE_H = 120
CHROME_H = SCREEN_H - PAGE_H
# The child's iframe element: (100, 50) 80 x 60 in the top page.
BOX = (100, CHROME_H + 50, 180, CHROME_H + 110)
# A site key the record must never carry; fixed, so xdist workers collect the same ids.
SITE_KEY = "k=site-key-under-test"
RECAPTCHA = f"https://www.google.com/recaptcha/api2/anchor?{SITE_KEY}"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _report(epoch: int = 1, *, doc: str = DOC, fields=(), owners=(),
            viewport=(SCREEN_W, PAGE_H, 0, 0, 1)) -> str:
    return json.dumps({"v": 1, "k": "report", "doc": doc, "e": epoch, "vp": list(viewport),
                       "f": [dict(f) for f in fields], "t": [], "o": [dict(o) for o in owners],
                       "tr": False})


def _layout(epoch: int, *, doc: str = DOC) -> str:
    return json.dumps({"v": 1, "k": "layout", "doc": doc, "e": epoch})


def _owner() -> dict:
    return {"id": 42, "r": [100, 50, 80, 60]}


def _password(fid: int, x: float, y: float, w: float, h: float) -> dict:
    return {"id": fid, "type": "password", "ac": "", "ever": False, "r": [x, y, w, h]}


def _screen(rgb=WHITE) -> Frame:
    return Frame.blank(SCREEN_W, SCREEN_H, rgb)


def _all(frame: Frame, box, rgb) -> bool:
    x0, y0, x1, y1 = box
    return all(frame.rgb_at(x, y) == rgb for y in range(y0, y1) for x in range(x0, x1))


def _covered(frame: Frame, box=BOX) -> bool:
    return _all(frame, box, MASK_RGB)


def _shown(frame: Frame, box=BOX) -> bool:
    return _all(frame, box, WHITE)


# --------------------------------------------------------------------------
# 1. A failed link is retried
# --------------------------------------------------------------------------


class FlakyCdp:
    """`DOM.getFrameOwner` raises `CdpError` for the first `failures` calls."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.sent: list[str] = []

    async def __call__(self, method: str, params: dict) -> dict:
        self.sent.append(method)
        if method == "DOM.getFrameOwner":
            if self.failures > 0:
                self.failures -= 1
                raise CdpError("No frame for given id found")
            return {"backendNodeId": 99}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-1"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "number", "value": 42}}
        return {}

    @property
    def tries(self) -> int:
        return self.sent.count("DOM.getFrameOwner")


def _linking_rig(failures: int):
    clock = Clock()
    state = MaskState(clock=clock)
    state.session_up()
    cdp = FlakyCdp(failures)
    mask = MaskCdp(state, cdp, clock=clock)

    async def start() -> None:
        await mask.on_event("Page.frameNavigated", {"frame": {"id": TOP, "url": "https://example.test/"}})
        await mask.on_event("Runtime.executionContextCreated", {"context": {
            "id": 2, "name": WORLD_NAME, "auxData": {"frameId": TOP}}})
        await mask.on_event("Page.frameAttached", {"frameId": CHILD, "parentFrameId": TOP})

    asyncio.run(start())
    return clock, state, cdp, mask


def _top_reports(mask: MaskCdp, epoch: int) -> None:
    asyncio.run(mask.on_event("Runtime.bindingCalled", {
        "name": BINDING_NAME, "payload": _report(epoch, owners=[_owner()]), "executionContextId": 2}))


def test_a_link_that_fails_once_is_linked_on_the_next_report_and_the_cover_lifts(caplog):
    caplog.set_level(logging.INFO, logger="agent_worker.livegate.mask")
    clock, state, cdp, mask = _linking_rig(failures=1)
    assert cdp.tries == 1
    # The CDP error's message is logged with its class: it is Chromium's, never the page's.
    assert "CdpError: No frame for given id found" in caplog.text
    _top_reports(mask, 1)
    # Unlinked: where it is drawn is unknown, so the page area is blanked.
    assert state.apply(_screen()) is Outcome.PAGE_BLANKED

    clock.now += 0.3  # past the first backoff (250 ms)
    _top_reports(mask, 2)
    assert cdp.tries == 2
    state.on_message(CHILD, _report(doc="captcha-doc", viewport=(80, 60, 0, 0, 1)))
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED
    assert _shown(frame)
    assert state.reason is None


def test_a_report_inside_the_backoff_does_not_retry_and_the_tries_stop_at_the_bound():
    clock, state, cdp, mask = _linking_rig(failures=100)
    _top_reports(mask, 1)  # 0 s after the failure: inside 250 ms
    assert cdp.tries == 1
    waits = []
    epoch = 2
    while cdp.tries < LINK_TRIES:
        before = cdp.tries
        clock.now += 0.01
        _top_reports(mask, epoch)
        epoch += 1
        if cdp.tries == before:
            continue
        waits.append(round(clock.now, 2))
    # Backoff from each failure: 250 ms, 500 ms, 1 s, 2 s.
    gaps = [round(b - a, 2) for a, b in zip([100.0] + waits, waits)]
    assert gaps == pytest.approx([0.25, 0.5, 1.0, 2.0], abs=0.011)
    clock.now += 60
    _top_reports(mask, epoch)
    assert cdp.tries == LINK_TRIES
    # It stays covered as before: unplaced, so the page area is blanked.
    assert state.apply(_screen()) is Outcome.PAGE_BLANKED


def test_the_childs_navigation_retries_the_link():
    clock, state, cdp, mask = _linking_rig(failures=1)
    clock.now += 0.3
    asyncio.run(mask.on_event("Page.frameNavigated", {"frame": {
        "id": CHILD, "parentId": TOP, "url": RECAPTCHA}}))
    assert cdp.tries == 2
    _top_reports(mask, 1)
    state.on_message(CHILD, _report(doc="captcha-doc", viewport=(80, 60, 0, 0, 1)))
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED
    assert _shown(frame)


# --------------------------------------------------------------------------
# 2. Challenge frames are shown while a person holds control
# --------------------------------------------------------------------------


def _captcha_page(url: str, record=None, clock=None) -> MaskState:
    state = MaskState(record=record, clock=clock or Clock())
    state.session_up()
    state.frame_attached(TOP, None)
    state.navigated(TOP, "https://www.google.com/sorry/index")
    state.frame_attached(CHILD, TOP)
    state.navigated(CHILD, url)
    state.frame_linked(CHILD, 42)
    state.on_message(TOP, _report(owners=[_owner()]))
    return state


@pytest.mark.parametrize("url", [
    RECAPTCHA,
    "https://www.recaptcha.net/recaptcha/api2/bframe?" + SITE_KEY,
    "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge",
    "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0",
])
def test_a_challenge_frame_that_cannot_vouch_for_itself_is_shown_while_a_person_holds_control(url):
    records: list[tuple[str, dict]] = []
    state = _captcha_page(url, record=lambda event, fields: records.append((event, dict(fields))))
    state.set_in_control(True)
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED
    assert _shown(frame)
    host = url.split("/")[2]
    assert records == [("mask_frame_shown", {"host": host, "reason": REASON_SHOWN_IN_CONTROL})]
    # Recorded once per frame, not once per captured frame.
    state.apply(_screen())
    assert len(records) == 1
    # Host only: never the path, the query or anything the page drew.
    assert SITE_KEY not in json.dumps(records)


def test_the_same_frame_is_covered_while_the_agent_drives():
    records: list = []
    state = _captcha_page(RECAPTCHA, record=lambda event, fields: records.append(event))
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED
    assert _covered(frame)
    state.set_in_control(True)
    assert _shown(_applied(state))
    state.set_in_control(False)
    frame = _applied(state)
    assert _covered(frame)
    assert state.reason == "an embedded frame on it cannot be checked"
    assert records == ["mask_frame_shown"]


def _applied(state: MaskState) -> Frame:
    frame = _screen()
    state.apply(frame)
    return frame


@pytest.mark.parametrize("url", [
    "https://recaptcha.evil.com/recaptcha/api2/anchor",
    "https://www.google.com.evil.com/recaptcha/api2/anchor",
    "https://www.google.com/recaptchaX/api2/anchor",
    "https://www.google.com/search?q=recaptcha",
    "https://www.google.com/x/recaptcha/",
    "https://evil.com/recaptcha/?www.google.com/recaptcha/",
    "https://www.google.com@evil.com/recaptcha/",
    "https://hcaptcha.com.evil.com/",
    "https://evilhcaptcha.com/",
    "https://challenges.cloudflare.com.evil.com/",
    "http://www.google.com/recaptcha/api2/anchor",
    "https://www.google.com:8443/recaptcha/api2/anchor",
    "about:blank",
    "",
])
def test_a_lookalike_host_or_another_path_stays_covered_in_control(url):
    records: list = []
    state = _captcha_page(url, record=lambda event, fields: records.append(event))
    state.set_in_control(True)
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED
    assert _covered(frame)
    assert records == []


def test_the_top_frame_is_never_shown_unchecked_even_on_a_challenge_url():
    state = MaskState(clock=Clock())
    state.session_up()
    state.frame_attached(TOP, None)
    state.navigated(TOP, RECAPTCHA)
    state.set_in_control(True)
    assert state.apply(_screen()) is Outcome.UNAVAILABLE
    state.on_message(TOP, _report())
    state.on_message(TOP, _layout(2))
    state.url_changed(TOP, RECAPTCHA + "#x")  # not a hold either: a navigation
    assert state.apply(_screen()) is Outcome.PAGE_BLANKED


def test_a_shown_challenge_frame_still_masks_the_password_fields_it_reported():
    state = _captcha_page(RECAPTCHA)
    state.on_message(CHILD, _report(doc="captcha-doc", viewport=(80, 60, 0, 0, 1),
                                    fields=[_password(1, 5, 5, 30, 8)]))
    state.on_message(CHILD, _layout(2, doc="captcha-doc"))  # stale: cannot vouch
    state.set_in_control(True)
    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED
    # The field from its last report: (105, 85) to (135, 93) on screen.
    assert _covered(frame, (105, CHROME_H + 55, 135, CHROME_H + 63))
    # Beyond the field and its dilation (DILATE_PX), the challenge is shown.
    assert _shown(frame, (150, CHROME_H + 75, 180, CHROME_H + 110))


# --------------------------------------------------------------------------
# 3. A layout change holds the last masked frame for up to 300 ms
# --------------------------------------------------------------------------


def _page(clock: Clock) -> MaskState:
    state = MaskState(clock=clock)
    state.session_up()
    state.frame_attached(TOP, None)
    state.navigated(TOP, "https://example.test/")
    state.on_message(TOP, _report(1))
    return state


def _changing_rig():
    clock = Clock()
    state = _page(clock)
    shown = _screen(WHITE)
    assert state.apply(shown) is Outcome.MASKED
    clock.now += 0.05
    state.on_message(TOP, _layout(2))
    return clock, state


def test_a_change_shorter_than_the_hold_sends_no_blank_frame():
    assert HOLD_CHANGING_SECONDS == 0.3
    clock, state = _changing_rig()
    for _ in range(5):
        clock.now += 0.05
        # The screen moved on (green); what is sent is the last masked frame.
        frame = _screen(GREEN)
        assert state.apply(frame) is Outcome.MASKED
        assert _all(frame, (0, 0, SCREEN_W, SCREEN_H), WHITE)
    state.on_message(TOP, _report(2))
    frame = _screen(GREEN)
    assert state.apply(frame) is Outcome.MASKED
    assert _all(frame, (0, 0, SCREEN_W, SCREEN_H), GREEN)


def test_a_change_longer_than_the_hold_blanks():
    clock, state = _changing_rig()
    clock.now += 0.25
    assert state.apply(_screen(GREEN)) is Outcome.MASKED
    clock.now += 0.06  # 0.31 s since the change was announced
    frame = _screen(GREEN)
    assert state.apply(frame) is Outcome.PAGE_BLANKED
    assert state.reason == REASON_CHANGING
    assert _all(frame, (0, CHROME_H, SCREEN_W, SCREEN_H), MASK_RGB)


def test_a_new_layout_announcement_does_not_extend_the_hold():
    clock, state = _changing_rig()
    for epoch in range(3, 9):
        clock.now += 0.05
        state.on_message(TOP, _layout(epoch))
        state.apply(_screen(GREEN))
    # 0.3 s since the first announcement: the page has not settled since.
    clock.now += 0.01
    assert state.apply(_screen(GREEN)) is Outcome.PAGE_BLANKED


def test_a_navigation_blanks_at_once():
    clock, state = _changing_rig()
    clock.now += 0.05
    state.navigated(TOP, "https://example.test/next")
    assert state.apply(_screen(GREEN)) is Outcome.UNAVAILABLE


def test_a_same_document_navigation_blanks_at_once():
    clock, state = _changing_rig()
    clock.now += 0.05
    state.url_changed(TOP, "https://example.test/#two")
    frame = _screen(GREEN)
    assert state.apply(frame) is Outcome.PAGE_BLANKED


@pytest.mark.parametrize("cut", ["hold", "session_down", "set_scale", "hidden", "in_control"])
def test_a_tab_switch_a_lost_session_or_a_device_change_is_never_held(cut):
    clock, state = _changing_rig()
    clock.now += 0.05
    if cut == "hold":
        state.hold("the browser is switching tabs")
        state.release()
    elif cut == "session_down":
        state.session_down()
        assert state.apply(_screen(GREEN)) is Outcome.UNAVAILABLE
        state.session_up()
        state.on_message(TOP, _report(2))
        state.on_message(TOP, _layout(3))
    elif cut == "set_scale":
        state.set_scale(1.0)
    elif cut == "hidden":
        state.on_message(TOP, json.dumps({"v": 1, "k": "layout", "doc": DOC, "e": 3, "h": True}))
        assert state.apply(_screen(GREEN)) is Outcome.UNAVAILABLE
        state.on_message(TOP, json.dumps({"v": 1, "k": "layout", "doc": DOC, "e": 4, "h": False}))
    elif cut == "in_control":
        state.set_in_control(True)
    frame = _screen(GREEN)
    assert state.apply(frame) is not Outcome.MASKED
    assert not _all(frame, (0, CHROME_H, SCREEN_W, SCREEN_H), WHITE)


def test_a_held_frame_is_never_one_from_another_document():
    clock = Clock()
    state = _page(clock)
    assert state.apply(_screen(WHITE)) is Outcome.MASKED
    state.navigated(TOP, "https://example.test/next")
    state.on_message(TOP, _layout(1, doc="doc-2"))
    clock.now += 0.05
    assert state.apply(_screen(GREEN)) is Outcome.UNAVAILABLE
    state.on_message(TOP, _report(1, doc="doc-2"))
    state.on_message(TOP, _layout(2, doc="doc-2"))
    frame = _screen(GREEN)
    # No frame of this document was masked yet: nothing to hold, so it blanks.
    assert state.apply(frame) is Outcome.PAGE_BLANKED


# --------------------------------------------------------------------------
# The gate tells the mask who drives
# --------------------------------------------------------------------------


def test_the_gate_says_a_person_holds_control_from_the_hand_off_to_the_resume(short_tmp):  # noqa: F811
    from agent_worker.livegate.ctl import CtlClient
    from test_livegate_main import Rig, _started

    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        gate = rig.gate
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        assert gate.person_in_control is False  # the agent drives
        await asyncio.to_thread(client.call, "pause", {"request_id": "r1", "expect": "example.test"})
        assert gate.person_in_control is True
        await asyncio.to_thread(client.call, "resume", {})
        assert gate.person_in_control is False
        await asyncio.to_thread(client.call, "pause", {"request_id": "r2", "expect": "example.test"})
        await asyncio.to_thread(client.call, "end_control", {"cause": "timeout"})
        assert gate.person_in_control is False
        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())
