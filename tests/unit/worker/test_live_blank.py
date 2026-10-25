"""The live view follows the tab Chromium shows, and shows it as drawn (LIVE-BLANK, #1030; LIVE-NOMASK, #2298).

MEASURED 2026-10-13 by the operator's QA on SwarmCloud release 1e86c37e: on
Google's reCAPTCHA page (task_2ac5d7e22a51460995a2) and on an article with
many ad iframes (task_9d2890cce74147f985ff) the page area was the mask's flat
#222 for ten minutes and more, and a resumed attempt (task_84cb2038aa704c829d48)
showed the whole frame #555 for 30 s and more. Two causes: the page follower
followed the NEWEST page target rather than the one shown, and the mask failed
closed on any frame it could not check. BGD decided on 2026-10-25 that the
live view paints nothing (docs/design/live-browser.md §5.3), so the second
cause is gone with the mask; the first still matters, because the caption's
origin and the CAPTCHA hand-off are read from the page the follower follows.

Held here, over a scripted Chromium:

* the followed page is the one Chromium shows -- read as
  `document.visibilityState` in an isolated world of each page, which no
  page script can reach -- on start, when a tab is opened, and when the agent
  activates one through the CDP proxy; and the capture publishes the screen;
* an out-of-process frame gets its own page-watch session, so a reCAPTCHA in
  a renderer of its own still raises the hand-off; a worker the auto-attach
  brought is let go;
* the RFB desktop name is the browser-closed sentence or the plain name,
  never a "cannot show" one.

Offline: no Chromium, Xvfb, relay or network.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from typing import Any

from agent_worker.livegate import rfb
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.mask import REASON_BROWSER_CLOSED

import test_live_device_recover as recover
from test_live_rfb import Gate as RfbGate

WHITE = (0xFF, 0xFF, 0xFF)

# The Desktop preset's screen (presets.py): 1440 x 987.
SCREEN_W, SCREEN_H = 1440, 987


# --------------------------------------------------------------------------
# The followed page is the one Chromium shows
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


def test_a_resumed_attempt_with_restored_tabs_follows_the_tab_on_screen_and_publishes_the_page(tmp_path):
    """Generation 2's profile restored three tabs; the one shown is the FIRST
    `Target.getTargets` lists, and the gate used to follow the last."""
    rig = _tabs_rig(tmp_path, ["PAGE1", "PAGE2", "PAGE3"], shown="PAGE1")

    async def body(rig: recover.Rig) -> None:
        gate = rig.gate
        await recover._until(lambda: gate.pages.current == "PAGE1" and gate.pages._session is not None)
        await recover._until(lambda: gate.origin is not None and gate.origin.get("origin") == "https://page1.example.test")

        gate.framebuffer = Framebuffer()
        gate._tasks["capture"] = asyncio.create_task(gate._capture())
        gate._watching.set()
        await recover._until(lambda: "first_frame" in gate.startup)
        current = gate.hub.current
        assert current.reason is None, "the resumed attempt's frame says something besides the page"
        assert current.frame.rgb_at(10, 10) == WHITE and current.frame.rgb_at(700, 500) == WHITE

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
        gate = rig.gate
        await recover._until(lambda: gate.pages.current == "PAGE1")

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

        # It brings the first tab back to the front (Playwright's bringToFront).
        await gate.proxy.on_agent_message(agent, {"id": 2, "method": "Target.activateTarget",
                                                  "params": {"targetId": "PAGE1"}})
        await recover._until(lambda: gate.pages.current == "PAGE1" and gate.pages._session is not None)

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


def test_an_out_of_process_challenge_frame_gets_its_own_session_and_raises_the_hand_off(tmp_path):
    rig = _tabs_rig(tmp_path, ["PAGE1"], shown="PAGE1")

    async def body(rig: recover.Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        await recover._until(lambda: gate.pages.current == "PAGE1" and gate.pages._session is not None)
        page_session = gate.pages._session
        # The page holds a cross-site iframe; it swaps into a renderer of its own.
        chromium.emit("Page.frameAttached", {"frameId": "AD", "parentFrameId": "PAGE1"}, page_session)
        chromium.emit("Page.frameDetached", {"frameId": "AD", "reason": "swap"}, page_session)
        await recover._until(lambda: gate.mask_state.has_frame("AD"))

        chromium.oopifs["OOP1"] = ("AD", "PAGE1")
        chromium.emit("Target.attachedToTarget", {"sessionId": "OOP1", "waitingForDebugger": False,
                                                  "targetInfo": {"targetId": "AD", "type": "iframe"}},
                      page_session)
        # A worker the auto-attach brought is let go.
        chromium.emit("Target.attachedToTarget", {"sessionId": "WRK1", "waitingForDebugger": False,
                                                  "targetInfo": {"targetId": "W", "type": "worker"}},
                      page_session)
        await recover._until(lambda: any(m == "Page.getFrameTree" and sess == "OOP1"
                                          for m, _p, sess in chromium.sent))
        await recover._until(lambda: any(m == "Target.detachFromTarget" and p.get("sessionId") == "WRK1"
                                          for m, p, _s in chromium.sent))
        # No script is run in the frame, and none in the page: the watch reads, it never paints.
        assert not [m for m, _p, _s in chromium.sent
                    if m in ("Runtime.addBinding", "Page.addScriptToEvaluateOnNewDocument")]
        assert gate.challenge is None
        # The out-of-process frame navigates to a reCAPTCHA: heard on its own session.
        chromium.emit("Page.frameNavigated", {"frame": {
            "id": "AD", "url": "https://www.google.com/recaptcha/api2/anchor?k=site-key-under-test"}}, "OOP1")
        await recover._until(lambda: gate.challenge is not None)
        assert gate.challenge["expect"] == "page1.example.test"

    _run(rig, body)


# --------------------------------------------------------------------------
# The desktop name
# --------------------------------------------------------------------------


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


def test_the_viewer_is_told_the_agent_finished_and_the_plain_name_when_a_browser_is_back():
    async def main() -> None:
        gate = RfbGate(width=64, height=40)
        viewer = await gate.connect("v1", encodings=(0, rfb.ENCODING_DESKTOP_NAME))
        gate.hub.publish(Frame.blank(64, 40, WHITE), reason=REASON_BROWSER_CLOSED)
        viewer.request(incremental=False)
        await viewer.flush()
        encodings, name = await asyncio.wait_for(_read_name_update(viewer), 5)
        assert name == REASON_BROWSER_CLOSED

        gate.hub.publish(Frame.blank(64, 40, (1, 2, 3)))
        viewer.request(incremental=True)
        await viewer.flush()
        encodings, name = await asyncio.wait_for(_read_name_update(viewer), 5)
        assert name == "swarm live browser"
        await gate.close()

    asyncio.run(main())


def test_a_viewer_that_does_not_take_desktop_names_is_never_sent_one():
    async def main() -> None:
        gate = RfbGate(width=64, height=40)
        viewer = await gate.connect("v1", encodings=(0,))
        gate.hub.publish(Frame.blank(64, 40, WHITE), reason=REASON_BROWSER_CLOSED)
        viewer.request(incremental=False)
        await viewer.flush()
        encodings, name = await asyncio.wait_for(_read_name_update(viewer), 5)
        assert rfb.ENCODING_DESKTOP_NAME not in encodings and name is None
        await gate.close()

    asyncio.run(main())
