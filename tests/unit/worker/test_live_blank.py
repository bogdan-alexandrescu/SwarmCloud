"""The live view shows the agent's page whenever Chromium shows one (LIVE-BLANK; part of #1030).

MEASURED 2026-10-13 by the operator's QA on SwarmCloud release 1e86c37e, the
canvas sampled from the console at full resolution:

(1) task_2ac5d7e22a51460995a2 (claude-code-browser, Desktop 1440x987) on
    Google's reCAPTCHA page, `google.com/sorry/index`, hand-off requested
    23:09:54Z: for ten minutes and more the browser's tab strip and address
    bar were drawn and the whole PAGE area below them was flat rgb(34,34,34),
    `mask.MASK_RGB` painted over the page area -- `Outcome.PAGE_BLANKED`.
    The same on task_9d2890cce74147f985ff on www.thelist.com, an article with
    many ad iframes.
(2) task_84cb2038aa704c829d48 resumed at 22:20:44Z (generation 2): the WHOLE
    frame, browser chrome included, rgb(85,85,85) -- `mask.UNAVAILABLE_RGB`,
    `Outcome.UNAVAILABLE` or the hub's first blank -- for 30 s and more while
    the hand-off banner was up, and no `first_frame` for generation 2.

What the gate did. The page follower followed the NEWEST page target ("the
visible tab is the one opened last"), and on start the LAST one
`Target.getTargets` listed. An agent driving Chromium over the CDP proxy
(Playwright MCP, swarm_browser) opens tabs and brings others to the front; a
profile restored on a resumed attempt brings back several tabs in an order
that says nothing about which one is shown. The mask then held the report of
a page that was not on screen: a hidden tab runs no animation frames, so its
observer never measures again, and the first layout change left its report
stale for good -- the page area blanked for as long as the hidden tab lived,
over the page Chromium WAS showing. And a child frame that could not vouch
for itself -- an ad iframe whose observer never reported, an OOPIF -- blanked
the whole page area rather than its own box.

What the gate does now, each held below over a scripted Chromium or over
observer reports built the way `observer.js` writes them:

* the followed page is the one Chromium shows -- read as
  `document.visibilityState` in an isolated world of each page, which no
  page script can reach -- on start, when a tab is opened, and when the agent
  activates one through the CDP proxy;
* a child frame that cannot vouch for itself covers ITS box, never the page:
  an iframe-heavy article and the /sorry page show the page, with only the
  password field and the unreadable frames covered;
* a frame the mask cannot place, a page it cannot read, a tab switch under
  way: the frame is still filled -- every pixel sent passes the mask -- and
  the viewer is told why, as the RFB desktop name ("Can't show this page
  safely: ..."), which the console's strip shows.

Offline: no Chromium, Xvfb, relay or network. Every credential-shaped value is
built at run time.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import struct
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import rfb
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.mask import (
    BINDING_NAME,
    MASK_RGB,
    UNAVAILABLE_RGB,
    WORLD_NAME,
    REASON_HIDDEN,
    MaskCdp,
    MaskState,
    Outcome,
    observer_source,
)

import test_live_device_recover as recover
from test_live_rfb import Gate as RfbGate

WHITE = (0xFF, 0xFF, 0xFF)
#: What the strip says before the reason (the lane's brief, word for word).
CANNOT_SHOW = "Can't show this page safely"

# The Desktop preset's screen (presets.py): 1440 x 987, the browser's own UI
# in the top 87 rows and the page's 1440 x 900 below it.
SCREEN_W, SCREEN_H = 1440, 987
PAGE_W, PAGE_H = 1440, 900
CHROME_H = SCREEN_H - PAGE_H


# --------------------------------------------------------------------------
# Observer reports, as observer.js writes them
# --------------------------------------------------------------------------


def _report(doc: str, epoch: int = 1, *, fields=(), texts=(), owners=(),
            viewport=(PAGE_W, PAGE_H, 0, 0, 1, PAGE_W, PAGE_H)) -> str:
    return json.dumps({"v": 1, "k": "report", "doc": doc, "e": epoch, "vp": list(viewport),
                       "f": list(fields), "t": list(texts), "o": list(owners), "tr": False})


def _layout(doc: str, epoch: int) -> str:
    return json.dumps({"v": 1, "k": "layout", "doc": doc, "e": epoch})


def _password(fid: int, x: float, y: float, w: float = 300, h: float = 40) -> dict:
    return {"id": fid, "type": "password", "ac": "current-password", "ever": True, "r": [x, y, w, h]}


def _text(s: str, block: int, x: float, y: float, w: float, h: float) -> dict:
    return {"s": s, "b": block, "r": [x, y, w, h]}


def _owner(oid: int, x: float, y: float, w: float, h: float) -> dict:
    return {"id": oid, "r": [x, y, w, h]}


def _screen() -> Frame:
    return Frame.blank(SCREEN_W, SCREEN_H, WHITE)


def _covered(frame: Frame, x0: int, y0: int, x1: int, y1: int, rgb=MASK_RGB) -> bool:
    """Every pixel of the box, in page coordinates, is `rgb`."""
    return all(frame.rgb_at(x, CHROME_H + y) == rgb for y in range(y0, y1, 3) for x in range(x0, x1, 3))


def _shown(frame: Frame, x0: int, y0: int, x1: int, y1: int) -> bool:
    return _covered(frame, x0, y0, x1, y1, rgb=WHITE)


def _top(url: str) -> MaskState:
    state = MaskState()
    state.session_up()
    state.frame_attached("TOP", None)
    state.navigated("TOP", url)
    return state


# --------------------------------------------------------------------------
# (1) An iframe-heavy article and Google's /sorry page show the page
# --------------------------------------------------------------------------


def test_an_iframe_heavy_article_shows_the_page_with_only_the_password_and_the_silent_ads_covered():
    """www.thelist.com's shape: an article, a sign-in box, six ad slots.

    Two ads are cross-site frames whose observer never reported (an OOPIF the
    gate's session cannot run a world in), one is a friendly iframe that did
    report, one is a 1x1 tracker, one is parked off screen, and one is an
    iframe element whose frame swapped out of the page's process, so no child
    frame claims it at all.
    """
    state = _top("https://www.thelist.com/1234/an-article/")
    silent_ads = {"AD1": (11, (1100, 120, 300, 250)), "AD2": (12, (1100, 420, 300, 600))}
    for frame_id, (owner, _box) in silent_ads.items():
        state.frame_attached(frame_id, "TOP")
        state.frame_linked(frame_id, owner)
    state.frame_attached("FRIENDLY", "TOP")
    state.frame_linked("FRIENDLY", 13)
    state.frame_attached("PIXEL", "TOP")
    state.frame_linked("PIXEL", 14)
    state.frame_attached("PARKED", "TOP")
    state.frame_linked("PARKED", 15)
    texts = [_text("Celebrities who quietly left Hollywood", 1, 40, 40, 900, 48)]
    texts += [_text(f"Paragraph {i} of the article, ordinary prose.", 2 + i, 40, 120 + 60 * i, 900, 40)
              for i in range(10)]
    state.on_message("TOP", _report(
        "article", fields=[_password(1, 40, 760)], texts=texts,
        owners=[_owner(11, 1100, 120, 300, 250), _owner(12, 1100, 420, 300, 600),
                _owner(13, 40, 700, 728, 50), _owner(14, 0, 0, 1, 1),
                _owner(15, -5000, -5000, 300, 250), _owner(16, 600, 820, 320, 50)]))
    state.on_message("FRIENDLY", _report("friendly", viewport=(728, 50, 0, 0, 1, 728, 50),
                                         texts=[_text("Advertisement", 1, 0, 0, 200, 20)]))

    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED, "the article's page area was blanked whole"
    # The article is shown...
    assert _shown(frame, 40, 40, 940, 88)
    assert _shown(frame, 40, 300, 1000, 680)
    # ...and the friendly ad, which vouched for itself.
    assert _shown(frame, 260, 710, 760, 740)
    # Covered: the password field, the two silent ads and the unclaimed frame.
    assert _covered(frame, 40, 760, 340, 800)
    assert _covered(frame, 1100, 120, 1400, 370)
    assert _covered(frame, 1100, 420, 1400, 900)
    assert _covered(frame, 600, 820, 920, 870)
    # The strip says parts are covered, and why.
    assert state.reason is not None and "frame" in state.reason


def test_googles_sorry_page_shows_the_recaptcha_and_covers_the_password_field():
    """google.com/sorry: white, a reCAPTCHA anchor iframe, and its challenge
    iframe, which is on screen once a challenge opens and has not reported
    yet. A password field is added to hold that it stays covered here too."""
    state = _top("https://www.google.com/sorry/index?continue=https://www.google.com/search")
    state.frame_attached("ANCHOR", "TOP")
    state.frame_linked("ANCHOR", 21)
    state.frame_attached("CHALLENGE", "TOP")
    state.frame_linked("CHALLENGE", 22)
    state.on_message("TOP", _report(
        "sorry",
        fields=[_password(1, 40, 600)],
        texts=[_text("Our systems have detected unusual traffic from your computer network.", 1,
                     40, 40, 900, 40),
               _text("This page checks to see if it's really you sending the requests, and not a robot.",
                     2, 40, 100, 900, 40)],
        owners=[_owner(21, 40, 200, 304, 78), _owner(22, 400, 200, 400, 580)]))
    state.on_message("ANCHOR", _report("anchor", viewport=(304, 78, 0, 0, 1, 304, 78),
                                       texts=[_text("I'm not a robot", 1, 50, 30, 120, 20)]))

    frame = _screen()
    assert state.apply(frame) is Outcome.MASKED, "the /sorry page area was blanked whole"
    assert _shown(frame, 40, 40, 940, 140), "the page's text is not shown"
    assert _shown(frame, 50, 210, 330, 270), "the reCAPTCHA checkbox is not shown"
    assert _covered(frame, 40, 600, 340, 640), "the password field is not covered"
    assert _covered(frame, 400, 200, 800, 780), "the silent challenge frame is not covered"


def test_a_child_frame_the_gate_cannot_place_still_blanks_the_page_area_and_says_why():
    state = _top("https://example.test/")
    state.frame_attached("CHILD", "TOP")  # never linked: where it is drawn is unknown
    # ...and no iframe box is left for it: no element the observer reported.
    state.on_message("TOP", _report("doc"))
    frame = _screen()
    assert state.apply(frame) is Outcome.PAGE_BLANKED
    assert _covered(frame, 0, 0, PAGE_W, PAGE_H)
    assert state.reason is not None


def test_a_frame_that_swapped_into_its_own_process_stays_placed_and_reports_through_its_own_session():
    """A cross-site iframe becomes an OOPIF: Chromium detaches it from the
    page's session with reason `swap` and auto-attaches its own target. The
    frame keeps its place (its box is covered until it reports), and once its
    observer reports on the child session it is shown, its password covered."""
    replies = {"DOM.getFrameOwner": {"backendNodeId": 7},
               "DOM.resolveNode": {"object": {"objectId": "o1"}},
               "Runtime.callFunctionOn": {"result": {"value": 31}},
               "Page.getFrameTree": {"frameTree": {"frame": {"id": "OOPIF", "parentId": "TOP",
                                                             "url": "https://ads.example/slot"}}}}
    sent: list[tuple[str, dict]] = []

    async def send(method: str, params: dict) -> dict:
        sent.append((method, params))
        return replies.get(method, {})

    state = MaskState()

    async def run() -> None:
        top = MaskCdp(state, send)
        state.session_up()
        await top.on_event("Page.frameNavigated", {"frame": {"id": "TOP", "url": "https://news.example/"}})
        await top.on_event("Runtime.executionContextCreated", {"context": {
            "id": 1, "name": WORLD_NAME, "auxData": {"frameId": "TOP"}}})
        await top.on_event("Page.frameAttached", {"frameId": "OOPIF", "parentFrameId": "TOP"})
        await top.on_event("Runtime.bindingCalled", {"name": BINDING_NAME, "executionContextId": 1,
                                                     "payload": _report("top", owners=[_owner(31, 200, 200, 400, 300)])})
        await top.on_event("Page.frameDetached", {"frameId": "OOPIF", "reason": "swap"})
        frame = _screen()
        assert state.apply(frame) is Outcome.MASKED
        assert _covered(frame, 200, 200, 600, 500), "the swapped frame's box is not covered"
        assert _shown(frame, 0, 0, 180, 180)

        child = MaskCdp(state, send, child=True)
        await child.install()
        await child.on_event("Runtime.executionContextCreated", {"context": {
            "id": 1, "name": WORLD_NAME, "auxData": {"frameId": "OOPIF"}}})
        await child.on_event("Runtime.bindingCalled", {
            "name": BINDING_NAME, "executionContextId": 1,
            "payload": _report("ad", viewport=(400, 300, 0, 0, 1, 400, 300), fields=[_password(1, 10, 10, 100, 30)],
                               texts=[_text("Sponsored", 1, 10, 200, 100, 20)])})
        frame = _screen()
        assert state.apply(frame) is Outcome.MASKED
        assert _shown(frame, 230, 260, 600, 500), "the OOPIF that reported is not shown"
        assert _covered(frame, 210, 210, 310, 240), "the OOPIF's password field is not covered"

    asyncio.run(run())
    # The child session is never the top frame, and asks for its own children.
    assert ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False,
                                     "flatten": True}) in sent


# --------------------------------------------------------------------------
# (2), (3) The followed page is the one Chromium shows
# --------------------------------------------------------------------------


class TabsChromium(recover.FakeChromium):
    """`FakeChromium` with tabs: one of them is shown, and the page's own
    `document.visibilityState` says which, as headed Chromium's does."""

    def __init__(self, pages: list[str], shown: str) -> None:
        super().__init__()
        self.pages = {t: {"url": f"https://{t.lower()}.example.test/", "crashed": False} for t in pages}
        self.shown = shown
        self._worlds = 0
        #: Out-of-process frames' sessions: session -> (frame id, parent frame id).
        self.oopifs: dict[str, tuple[str, str]] = {}

    def show(self, target: str) -> None:
        self.shown = target

    def _answer(self, method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        if method == "Target.attachToBrowserTarget":
            self._next += 1
            return {"result": {"sessionId": f"BROWSER{self._next}"}}
        if session is not None and session.startswith("BROWSER"):
            # The agent's browser session, through the CDP proxy.
            if method == "Target.createTarget":
                target = f"PAGE{len(self.pages) + 1}"
                self.open_page(target, f"https://{target.lower()}.example.test/")
                if not params.get("background"):
                    self.shown = target
                return {"result": {"targetId": target}}
            if method == "Target.activateTarget":
                if params.get("targetId") in self.pages:
                    self.shown = params["targetId"]
                return {"result": {}}
            return {"result": {}}
        if method == "Target.activateTarget" and params.get("targetId") in self.pages:
            self.shown = params["targetId"]
        if session in self.oopifs:
            frame_id, parent = self.oopifs[session]
            if method == "Page.getFrameTree":
                return {"result": {"frameTree": {"frame": {"id": frame_id, "parentId": parent,
                                                           "url": "https://ads.example.test/slot"}}}}
            return {"result": {"identifier": "1"} if method == "Page.addScriptToEvaluateOnNewDocument" else {}}
        if session is not None and session in self.sessions:
            target = self.sessions[session]
            if method == "Page.createIsolatedWorld":
                self._worlds += 1
                return {"result": {"executionContextId": 5000 + self._worlds}}
            if method == "Runtime.evaluate" and params.get("contextId", 0) >= 5000:
                state = "visible" if target == self.shown else "hidden"
                return {"result": {"result": {"type": "string", "value": state}}}
        return super()._answer(method, params, session)

    def report(self, session: str, target: str, payload: str, context: int = 77) -> None:
        """`target`'s observer world reports on `session`, as Chromium delivers it."""
        self.emit("Runtime.executionContextCreated", {"context": {
            "id": context, "name": WORLD_NAME, "auxData": {"frameId": target}}}, session)
        self.emit("Runtime.bindingCalled", {"name": BINDING_NAME, "payload": payload,
                                            "executionContextId": context}, session)


class Framebuffer:
    def grab(self) -> Frame:
        return Frame.blank(SCREEN_W, SCREEN_H, WHITE)

    def close(self) -> None: ...


def _tabs_rig(tmp: Path, pages: list[str], shown: str) -> recover.Rig:
    rig = recover.Rig(tmp)
    rig.browser.transport = rig.chromium = TabsChromium(pages, shown)
    return rig


def _run(rig: recover.Rig, body: Any) -> None:
    async def main() -> None:
        try:
            await rig.start()
            await body(rig)
        finally:
            await rig.stop()

    asyncio.run(asyncio.wait_for(main(), 20))


def _page_report(fields=()) -> str:
    return _report("doc-" + secrets.token_hex(4), fields=list(fields))


def test_a_resumed_attempt_with_restored_tabs_follows_the_tab_on_screen_and_publishes_the_page(tmp_path):
    """Generation 2's profile restored three tabs; the one shown is the FIRST
    `Target.getTargets` lists, and the gate used to follow the last."""
    rig = _tabs_rig(tmp_path, ["PAGE1", "PAGE2", "PAGE3"], shown="PAGE1")

    async def body(rig: recover.Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        await recover._until(lambda: gate.pages.current == "PAGE1" and gate.pages._session is not None)
        chromium.report(gate.pages._session, "PAGE1", _page_report([_password(1, 40, 400)]))
        await recover._until(lambda: gate.mask_state.apply(_screen()) is Outcome.MASKED)

        gate.framebuffer = Framebuffer()
        gate._tasks["capture"] = asyncio.create_task(gate._capture())
        gate._watching.set()
        await recover._until(lambda: "first_frame" in gate.startup)
        current = gate.hub.current
        assert current.outcome is Outcome.MASKED, "the resumed attempt's frame is not the page"
        assert current.frame.rgb_at(10, CHROME_H + 10) == WHITE
        assert current.frame.rgb_at(100, CHROME_H + 410) == MASK_RGB, "the password field is not covered"

    _run(rig, body)


def test_a_tab_opened_behind_the_shown_one_is_not_followed(tmp_path):
    rig = _tabs_rig(tmp_path, ["PAGE1"], shown="PAGE1")

    async def body(rig: recover.Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        await recover._until(lambda: gate.pages.current == "PAGE1")
        chromium.open_page("PAGE2", "https://page2.example.test/")  # in the background
        await recover._until(lambda: "PAGE2" in gate.pages.pages)
        for _ in range(30):
            await asyncio.sleep(0.02)
        assert gate.pages.current == "PAGE1", "the gate followed a tab Chromium does not show"

    _run(rig, body)


def test_an_agent_that_opens_and_activates_tabs_through_the_cdp_proxy_is_followed_to_the_tab_shown(tmp_path):
    rig = _tabs_rig(tmp_path, ["PAGE1"], shown="PAGE1")

    async def body(rig: recover.Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        await recover._until(lambda: gate.pages.current == "PAGE1")
        chromium.report(gate.pages._session, "PAGE1", _page_report())
        await recover._until(lambda: gate.mask_state.apply(_screen()) is Outcome.MASKED)

        sent: list[dict[str, Any]] = []

        class Link:
            async def send(self, message: dict[str, Any]) -> None:
                sent.append(message)

            async def close(self) -> None: ...

        agent = await gate.proxy.connect_agent(Link())
        assert agent is not None
        # The agent opens a tab (Playwright's newPage): Chromium shows it.
        await gate.proxy.on_agent_message(agent, {"id": 1, "method": "Target.createTarget",
                                                  "params": {"url": "about:blank"}})
        await recover._until(lambda: gate.pages.current == "PAGE2" and gate.pages._session is not None)
        chromium.report(gate.pages._session, "PAGE2", _page_report(), context=78)
        await recover._until(lambda: gate.mask_state.apply(_screen()) is Outcome.MASKED)

        # It brings the first tab back to the front (Playwright's bringToFront).
        await gate.proxy.on_agent_message(agent, {"id": 2, "method": "Target.activateTarget",
                                                  "params": {"targetId": "PAGE1"}})
        # From the moment it asked, PAGE2's report is never painted over PAGE1.
        frame = _screen()
        assert gate.mask_state.apply(frame) is Outcome.UNAVAILABLE
        assert gate.mask_state.reason is not None
        await recover._until(lambda: gate.pages.current == "PAGE1" and gate.pages._session is not None)
        chromium.report(gate.pages._session, "PAGE1", _page_report(), context=79)
        await recover._until(lambda: gate.mask_state.apply(_screen()) is Outcome.MASKED)

    _run(rig, body)


def test_a_capture_that_fails_unexpectedly_still_publishes_a_frame_and_keeps_capturing(tmp_path):
    rig = _tabs_rig(tmp_path, ["PAGE1"], shown="PAGE1")

    async def body(rig: recover.Rig) -> None:
        gate = rig.gate
        grabs = {"n": 0}

        class Flaky(Framebuffer):
            def grab(self) -> Frame:
                grabs["n"] += 1
                if grabs["n"] == 1:
                    raise ValueError("a framebuffer header mid-rewrite")
                return super().grab()

        gate.framebuffer = Flaky()
        gate._tasks["capture"] = asyncio.create_task(gate._capture())
        gate._watching.set()
        await recover._until(lambda: grabs["n"] >= 3)
        assert not gate._tasks["capture"].done(), "the capture loop died on an unexpected error"
        assert "first_frame" in gate.startup

    _run(rig, body)


# --------------------------------------------------------------------------
# (4) The strip says why when the page cannot be shown
# --------------------------------------------------------------------------


@pytest.mark.parametrize("setup", ["no_session", "no_report", "too_wide", "stale"])
def test_a_frame_that_is_not_the_page_carries_the_reason(setup):
    state = MaskState()
    if setup != "no_session":
        state = _top("https://example.test/")
    if setup == "too_wide":
        state.on_message("TOP", _report("d", viewport=(4000, 900, 0, 0, 1, 4000, 900)))
    elif setup == "stale":
        state.on_message("TOP", _report("d"))
        state.on_message("TOP", _layout("d", 2))
    frame = _screen()
    outcome = state.apply(frame)
    assert outcome is not Outcome.MASKED
    assert isinstance(state.reason, str) and state.reason
    masked = rfb.masked(_screen(), state)
    assert masked.reason == state.reason


def test_the_page_shown_carries_no_reason():
    state = _top("https://example.test/")
    state.on_message("TOP", _report("d", fields=[_password(1, 10, 10)]))
    assert state.apply(_screen()) is Outcome.MASKED
    assert state.reason is None


def _read_name_update(viewer: Any) -> Any:
    async def read() -> tuple[list[int], str | None]:
        kind = (await viewer.reader.readexactly(1))[0]
        assert kind == 0
        await viewer.reader.readexactly(1)
        (n,) = struct.unpack(">H", await viewer.reader.readexactly(2))
        encodings, name = [], None
        for _ in range(n):
            x, y, w, h, enc = struct.unpack(">HHHHi", await viewer.reader.readexactly(12))
            encodings.append(enc)
            if enc == rfb.ENCODING_DESKTOP_NAME:
                (length,) = struct.unpack(">I", await viewer.reader.readexactly(4))
                name = (await viewer.reader.readexactly(length)).decode("utf-8")
            elif enc == 0:
                await viewer.reader.readexactly(w * h * 4)
        return encodings, name

    return read()


def test_the_viewer_is_told_why_as_the_desktop_name_and_told_again_when_the_page_is_back():
    async def main() -> None:
        gate = RfbGate(width=64, height=40)
        viewer = await gate.connect("v1", encodings=(0, rfb.ENCODING_DESKTOP_NAME))
        blank = MaskState()  # no session: unavailable
        gate.hub.publish(rfb.masked(Frame.blank(64, 40, WHITE), blank))
        viewer.request(incremental=False)
        await viewer.flush()
        encodings, name = await asyncio.wait_for(_read_name_update(viewer), 5)
        assert name is not None and name.startswith(CANNOT_SHOW + ": "), name
        assert blank.reason in name

        shown = MaskState()
        shown.session_up()
        shown.frame_attached("TOP", None)
        shown.navigated("TOP", "https://example.test/")
        shown.on_message("TOP", _report("d", viewport=(64, 30, 0, 0, 1, 64, 30)))
        gate.hub.publish(rfb.masked(Frame.blank(64, 40, WHITE), shown))
        viewer.request(incremental=True)
        await viewer.flush()
        encodings, name = await asyncio.wait_for(_read_name_update(viewer), 5)
        assert name is not None and not name.startswith(CANNOT_SHOW)
        await gate.close()

    asyncio.run(main())


def test_a_viewer_that_does_not_take_desktop_names_is_never_sent_one():
    async def main() -> None:
        gate = RfbGate(width=64, height=40)
        viewer = await gate.connect("v1", encodings=(0,))
        gate.hub.publish(rfb.masked(Frame.blank(64, 40, WHITE), MaskState()))
        viewer.request(incremental=False)
        await viewer.flush()
        encodings, name = await asyncio.wait_for(_read_name_update(viewer), 5)
        assert rfb.ENCODING_DESKTOP_NAME not in encodings and name is None
        await gate.close()

    asyncio.run(main())


def test_the_unavailable_fill_is_still_the_whole_frame():
    """Saying why never replaces the fill: every pixel sent passes the mask."""
    frame = _screen()
    assert MaskState().apply(frame) is Outcome.UNAVAILABLE
    assert all(frame.rgb_at(x, y) == UNAVAILABLE_RGB for x in range(0, SCREEN_W, 97) for y in range(0, SCREEN_H, 89))


# --------------------------------------------------------------------------
# The pieces under the above
# --------------------------------------------------------------------------


def test_a_report_from_a_hidden_tab_is_never_painted_over_the_screen():
    """The observer says when its tab is hidden (`h`), the moment it is
    (`visibilitychange`): from then its report describes other pixels."""
    state = _top("https://example.test/")
    state.on_message("TOP", _report("d", fields=[_password(1, 40, 40)]))
    assert state.apply(_screen()) is Outcome.MASKED
    state.on_message("TOP", json.dumps({"v": 1, "k": "layout", "doc": "d", "e": 2, "h": True}))
    frame = _screen()
    assert state.apply(frame) is Outcome.UNAVAILABLE
    assert state.reason == REASON_HIDDEN
    assert frame.rgb_at(700, CHROME_H + 400) == UNAVAILABLE_RGB
    # Shown again and measured: the page is back.
    state.on_message("TOP", _report("d", epoch=3, fields=[_password(1, 40, 40)]))
    assert state.apply(_screen()) is Outcome.MASKED


def test_a_visibility_flag_that_is_not_a_boolean_makes_the_frame_stale():
    state = _top("https://example.test/")
    state.on_message("TOP", _report("d"))
    state.on_message("TOP", json.dumps({"v": 1, "k": "layout", "doc": "d", "e": 2, "h": "no"}))
    assert state.apply(_screen()) is not Outcome.MASKED


def test_the_observer_announces_a_tab_hidden_or_shown_and_flags_every_message():
    source = observer_source()
    assert '"visibilitychange"' in source
    assert 'message.h = document.visibilityState !== "visible"' in source


def test_the_follower_installs_the_observer_on_an_out_of_process_frame_and_routes_its_reports(tmp_path):
    rig = _tabs_rig(tmp_path, ["PAGE1"], shown="PAGE1")

    async def body(rig: recover.Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        await recover._until(lambda: gate.pages.current == "PAGE1" and gate.pages._session is not None)
        page_session = gate.pages._session
        # The page reports an iframe element; its frame is out of process.
        state = gate.mask_state
        state.frame_attached("AD", "PAGE1")
        state.frame_linked("AD", 31)
        chromium.report(page_session, "PAGE1", _report("top", owners=[_owner(31, 200, 200, 400, 300)]))
        await recover._until(lambda: state.apply(_screen()) is Outcome.MASKED)
        frame = _screen()
        state.apply(frame)
        assert frame.rgb_at(400, CHROME_H + 350) == MASK_RGB, "a silent cross-site frame is not covered"

        chromium.oopifs["OOP1"] = ("AD", "PAGE1")
        chromium.emit("Target.attachedToTarget", {"sessionId": "OOP1", "waitingForDebugger": False,
                                                  "targetInfo": {"targetId": "AD", "type": "iframe"}},
                      page_session)
        # A worker the auto-attach brought is let go.
        chromium.emit("Target.attachedToTarget", {"sessionId": "WRK1", "waitingForDebugger": False,
                                                  "targetInfo": {"targetId": "W", "type": "worker"}},
                      page_session)
        await recover._until(lambda: any(m == "Runtime.addBinding" and sess == "OOP1"
                                          for m, _p, sess in chromium.sent))
        await recover._until(lambda: any(m == "Target.detachFromTarget" and p.get("sessionId") == "WRK1"
                                          for m, p, _s in chromium.sent))
        chromium.report("OOP1", "AD", _report("ad", viewport=(400, 300, 0, 0, 1, 400, 300),
                                              fields=[_password(1, 10, 10, 100, 30)]), context=90)

        def shown() -> bool:
            frame = _screen()
            return (state.apply(frame) is Outcome.MASKED and frame.rgb_at(400, CHROME_H + 350) == WHITE
                    and frame.rgb_at(250, CHROME_H + 225) == MASK_RGB)

        await recover._until(shown)

    _run(rig, body)
