"""The live screen comes back after device switches: the gate follows the agent's page (DEVICE-RECOVER; part of #1030).

MEASURED 2026-10-13 11:30-11:40 UTC (task_3458d6bfc7d14db79af1 and
task_c6bf0368442642f784e3): the owner took control and switched devices
eight times in about two and a half minutes; afterwards the whole canvas was
the mask's "unavailable" grey and the strip read "The gate has not reported
the page's address yet", for good, and a resume after the hand-off came back
`GateRefused`. The page REPORT had stopped: the gate's own session on the
agent's page was gone and nothing attached it again.

Each test here drives the real gate (`LiveGate.start_browser`: the proxy, the
device controller, the page follower and the mask's CDP side) against a
scripted Chromium that keeps pages and sessions the way Chromium does -- a
command on a session that is gone is answered "Session with given id not
found", and a detach is announced with `Target.detachedFromTarget` -- and can
lose a session in the middle of a device apply, crash a page, or close it.
No Chromium, no Xvfb, no network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate.presets import resolve

URL = "https://example.test/form"
NOT_FOUND = {"code": -32001, "message": "Session with given id not found."}


class FakeChromium:
    """Chromium's pipe, with pages and the sessions attached to them."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.pages: dict[str, dict[str, Any]] = {"PAGE1": {"url": URL, "crashed": False}}
        self.sessions: dict[str, str] = {}  # session -> targetId
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        #: Called with the command before it is answered: what Chromium does meanwhile.
        self.during: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._next = 0
        self.closed = False

    # -- what a page does --------------------------------------------------

    def emit(self, method: str, params: dict[str, Any], session: str | None = None) -> None:
        message: dict[str, Any] = {"method": method, "params": params}
        if session is not None:
            message["sessionId"] = session
        self.queue.put_nowait(message)

    def detach_all(self, target: str) -> None:
        """Every session on `target` detached; the page itself lives on (a renderer swap)."""
        for session, held in list(self.sessions.items()):
            if held == target:
                del self.sessions[session]
                self.emit("Target.detachedFromTarget", {"sessionId": session, "targetId": target})

    def close_page(self, target: str) -> None:
        self.detach_all(target)
        del self.pages[target]
        self.emit("Target.targetDestroyed", {"targetId": target})

    def open_page(self, target: str, url: str = URL) -> None:
        self.pages[target] = {"url": url, "crashed": False}
        self.emit("Target.targetCreated", {"targetInfo": {"targetId": target, "type": "page", "url": url}})

    def crash(self, target: str) -> None:
        self.pages[target]["crashed"] = True
        self.emit("Target.targetCrashed", {"targetId": target, "status": "crashed", "errorCode": 139})
        for session, held in self.sessions.items():
            if held == target:
                self.emit("Inspector.targetCrashed", {}, session)

    def reload(self, target: str) -> None:
        self.pages[target]["crashed"] = False
        for session, held in self.sessions.items():
            if held == target:
                self.emit("Page.frameNavigated", {"frame": {"id": target, "url": URL,
                                                            "domainAndRegistry": "example.test"}}, session)

    def close_pipe(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)

    def methods_on(self, target: str, method: str) -> list[tuple[dict[str, Any], str | None]]:
        """`method` as sent on any session that was `target`'s when it was sent."""
        return [(p, s) for m, p, s in self.sent if m == method and s is not None and s.endswith(f"@{target}")]

    # -- the pipe ------------------------------------------------------------

    def _answer(self, method: str, params: dict[str, Any], session: str | None) -> dict[str, Any]:
        if session is not None:
            target = self.sessions.get(session)
            if target is None:
                return {"error": NOT_FOUND}
            if self.pages[target]["crashed"] and method in ("Runtime.enable", "Page.getFrameTree",
                                                             "Runtime.addBinding"):
                return {"error": {"code": -32000, "message": "Target crashed"}}
        result: dict[str, Any] = {}
        if method == "Browser.getVersion":
            result = {"product": "Chrome/130.0.6723.0", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)"}
        elif method == "Target.getTargets":
            result = {"targetInfos": [{"targetId": t, "type": "page", "url": p["url"]}
                                      for t, p in self.pages.items()]}
        elif method == "Target.attachToTarget":
            target = params["targetId"]
            if target not in self.pages:
                return {"error": {"code": -32602, "message": "No target with given id found"}}
            self._next += 1
            new = f"S{self._next}@{target}"
            self.sessions[new] = target
            result = {"sessionId": new}
        elif method == "Target.detachFromTarget":
            gone = params.get("sessionId")
            target = self.sessions.pop(gone, None)
            if target is None:
                return {"error": NOT_FOUND}
            self.emit("Target.detachedFromTarget", {"sessionId": gone, "targetId": target})
        elif method in ("Browser.getWindowForTarget", "Target.getTargetInfo", "Target.activateTarget"):
            target = params.get("targetId")
            if target not in self.pages:
                return {"error": {"code": -32602, "message": "No target with given id found"}}
            if method == "Browser.getWindowForTarget":
                result = {"windowId": 1}
            elif method == "Target.getTargetInfo":
                result = {"targetInfo": {"targetId": target, "type": "page",
                                         "url": self.pages[target]["url"], "title": "Form"}}
        elif method == "Page.getFrameTree":
            target = self.sessions[session] if session else ""
            result = {"frameTree": {"frame": {"id": target, "url": self.pages[target]["url"],
                                              "domainAndRegistry": "example.test"}}}
        elif method == "Page.addScriptToEvaluateOnNewDocument":
            result = {"identifier": "1"}
        elif method == "Security.enable":
            self.emit("Security.visibleSecurityStateChanged",
                      {"visibleSecurityState": {"securityState": "secure"}}, session)
        return {"result": result}

    async def send(self, message: dict[str, Any]) -> None:
        if self.closed:
            raise BrokenPipeError(32, "Broken pipe")
        method, params, session = message["method"], message.get("params") or {}, message.get("sessionId")
        self.sent.append((method, params, session))
        hook = self.during.get(method)
        if hook is not None:
            hook(message)
        self.queue.put_nowait({"id": message["id"], **self._answer(method, params, session)})

    async def run(self, sink: Any) -> None:
        while True:
            message = await self.queue.get()
            if message is None:
                return
            await sink.on_chromium_message(message)

    async def close(self) -> None:
        self.close_pipe()


class FakeBrowser:
    def __init__(self) -> None:
        self.transport = FakeChromium()

    async def stop(self) -> None:
        self.transport.close_pipe()


class FakeDisplay:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    async def resize(self, width: int, height: int) -> None:
        self.sizes.append((width, height))
        await asyncio.sleep(0)


class FakeInjector:
    async def key(self, keysym: int, down: bool) -> None: ...

    async def pointer_event(self, buttons: int, x: int, y: int) -> None: ...

    async def type_text(self, text: str) -> bool:
        return True

    async def release_all(self) -> None: ...

    def resize(self, width: int, height: int) -> None: ...


class FakeServer:
    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class FakeTunnel:
    """The tunnel, as far as the timeline and the report go."""

    closed = False

    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    async def action(self, entry: dict[str, Any]) -> None:
        self.actions.append(dict(entry))

    async def report(self, payload: bytes) -> None: ...


def _config(tmp: Path) -> gate_main.GateConfig:
    for name in ("profile", "downloads", "uploads"):
        (tmp / name).mkdir(exist_ok=True)
    return gate_main.GateConfig(
        ctl_path=str(tmp / "ctl.sock"), marker=str(tmp / "started"), profile_dir=tmp / "profile",
        downloads_dir=tmp / "downloads", uploads_dir=tmp / "uploads", cdp_host="127.0.0.1",
        cdp_port=9222, relay_url="ws://relay.test:8080/v1/tunnel", fbdir=tmp,
    )


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.browser = FakeBrowser()
        self.chromium = self.browser.transport
        self.display = FakeDisplay()
        self.records: list[tuple[str, dict[str, Any]]] = []

        async def start_chromium(cfg: Any) -> FakeBrowser:
            return self.browser

        async def serve_cdp(proxy: Any, host: str, port: int) -> FakeServer:
            return FakeServer()

        deps = gate_main.Deps(start_chromium=start_chromium, serve_cdp=serve_cdp,
                              open_display=lambda cfg: self.display)
        self.gate = gate_main.LiveGate(_config(tmp), deps,
                                       record=lambda e, f: self.records.append((e, dict(f))))
        self.gate.injector = FakeInjector()
        self.tunnel = FakeTunnel()

    async def start(self) -> None:
        await self.gate.start_browser()
        self.gate.tunnel = self.tunnel  # type: ignore[assignment]
        await _until(self.showing)

    def showing(self) -> bool:
        """The mask has a live session on the agent's page, and its origin is reported."""
        pages = self.gate.pages
        return bool(pages is not None and pages.current in self.chromium.pages
                    and pages._session in self.chromium.sessions
                    and self.gate.mask_state._session and self.gate.origin is not None)

    def events(self) -> list[str]:
        return [str(a.get("event")) for a in self.tunnel.actions if a.get("kind") == "control"]

    async def stop(self) -> None:
        gate = self.gate
        if gate.pages is not None:
            await gate.pages.close()
        tasks = [*gate._tasks.values(), *gate._spawned]
        if gate.devices is not None:
            tasks.extend(gate.devices._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _frame(preset: str, orientation: str | None = None, touch: bool | None = None) -> bytes:
    return json.dumps({"preset": preset, "orientation": orientation, "touch": touch}).encode()


def _run(body: Callable[[Rig], Any], tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path)
        try:
            await rig.start()
            await body(rig)
        finally:
            await rig.stop()

    asyncio.run(asyncio.wait_for(main(), 20))


def test_a_session_lost_during_a_device_apply_is_attached_again_and_the_screen_comes_back(tmp_path):
    async def body(rig: Rig) -> None:
        chromium = rig.chromium
        before = len(chromium.methods_on("PAGE1", "Page.addScriptToEvaluateOnNewDocument"))
        lost: list[bool] = []

        def swap(message: dict[str, Any]) -> None:
            if not lost:
                lost.append(True)
                chromium.detach_all("PAGE1")

        chromium.during["Emulation.setDeviceMetricsOverride"] = swap
        device = await rig.gate.apply_device(_frame("iphone-15", "portrait"))
        assert (device.name, device.orientation) == ("iphone-15", "portrait")
        assert lost, "the scripted swap never ran"

        # The page report and the mask come back on a NEW session on the same page.
        await _until(rig.showing)
        assert rig.gate.pages.state == "following"
        assert len(chromium.methods_on("PAGE1", "Page.addScriptToEvaluateOnNewDocument")) > before, \
            "the observer was not injected again on the re-attached session"
        # The device is on the page through a session that is live now.
        applied = [(p, s) for p, s in chromium.methods_on("PAGE1", "Emulation.setDeviceMetricsOverride")
                   if s in chromium.sessions]
        assert applied and applied[-1][0]["width"] == 393
        assert "device_refused" not in rig.events()
        # Frames are masked again, not the blank "unavailable" grey: the session is up.
        assert rig.gate.mask_state._session is True

    _run(body, tmp_path)


def test_eight_rapid_switches_with_a_rotate_end_on_the_last_device_with_a_page_report(tmp_path):
    async def body(rig: Rig) -> None:
        chromium = rig.chromium
        applies: list[int] = []

        def swap_sometimes(message: dict[str, Any]) -> None:
            applies.append(1)
            if len(applies) % 3 == 0:
                chromium.detach_all("PAGE1")

        chromium.during["Emulation.setDeviceMetricsOverride"] = swap_sometimes
        start = len(rig.display.sizes)
        # The owner's sequence, 2026-10-13 04:30:56 PDT onwards.
        switches = [("desktop", "portrait"), ("desktop", "landscape"), ("iphone-15", "portrait"),
                    ("pixel-8", "portrait"), ("iphone-se", "portrait"), ("ipad-7", "portrait"),
                    ("iphone-se", "landscape"), ("desktop", "landscape")]
        for preset, orientation in switches:
            await rig.gate._on_device(_frame(preset, orientation))
            await asyncio.sleep(0)
        await _until(lambda: not rig.gate._spawned)
        last = resolve("desktop", "landscape")
        assert (rig.gate.devices.device.name, rig.gate.devices.device.orientation) == ("desktop", "landscape")
        assert rig.display.sizes[-1] == (last.display_width, last.display_height)
        # Latest wins: switches queued behind a running one are not each applied in turn.
        assert len(rig.display.sizes) - start < len(switches), rig.display.sizes[start:]
        await _until(rig.showing)
        assert rig.gate.device_reported is not None and rig.gate.device_reported["preset"] == "desktop"
        # Nothing in a device switch closes the agent's page.
        assert "PAGE1" in chromium.pages
        assert not [m for m, _, _ in chromium.sent if m in ("Target.closeTarget", "Browser.close", "Page.close")]

    _run(body, tmp_path)


def test_a_switch_that_loses_every_session_still_applies_and_says_nothing_was_refused(tmp_path):
    async def body(rig: Rig) -> None:
        chromium = rig.chromium

        def swap(message: dict[str, Any]) -> None:
            if message.get("sessionId") in chromium.sessions:
                chromium.detach_all("PAGE1")
                chromium.during.pop("Emulation.setUserAgentOverride", None)

        chromium.during["Emulation.setUserAgentOverride"] = swap
        await rig.gate.apply_device(_frame("pixel-8", "landscape"))
        assert rig.gate.devices.device.name == "pixel-8"
        await _until(rig.showing)
        assert "device_refused" not in rig.events()

    _run(body, tmp_path)


def test_a_crashed_page_fails_closed_then_comes_back(tmp_path):
    async def body(rig: Rig) -> None:
        chromium = rig.chromium
        before = len(chromium.methods_on("PAGE1", "Page.addScriptToEvaluateOnNewDocument"))
        chromium.crash("PAGE1")
        await _until(lambda: rig.gate.pages.state == "reconnecting")
        assert rig.gate.mask_state._session is False, "a crashed page must not be shown as masked"
        assert "reconnecting_to_page" in rig.events()
        chromium.reload("PAGE1")
        await _until(rig.showing)
        assert rig.gate.pages.state == "following"
        assert len(chromium.methods_on("PAGE1", "Page.addScriptToEvaluateOnNewDocument")) > before
        assert "page_reconnected" in rig.events()

    _run(body, tmp_path)


def test_the_agent_closing_its_page_is_said_and_the_next_page_is_followed(tmp_path):
    async def body(rig: Rig) -> None:
        chromium = rig.chromium
        await rig.gate.apply_device(_frame("iphone-15", "portrait"))
        chromium.close_page("PAGE1")
        await _until(lambda: rig.gate.pages.state == "closed")
        assert rig.gate.origin is None
        assert "agent_page_closed" in rig.events()
        chromium.open_page("PAGE3")
        await _until(rig.showing)
        assert rig.gate.pages.current == "PAGE3"
        assert rig.gate.pages.state == "following"
        assert "page_reconnected" in rig.events()
        # The current device is on the new page.
        await _until(lambda: any(p["width"] == 393 for p, _ in
                                 chromium.methods_on("PAGE3", "Emulation.setDeviceMetricsOverride")))

    _run(body, tmp_path)


def test_a_resume_after_a_hand_off_on_a_page_that_was_re_attached_succeeds(tmp_path):
    async def body(rig: Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        await gate.apply_device(_frame("iphone-15", "portrait"))
        await gate.pause({})
        await gate._grant("c1")
        assert gate._episode is not None
        # A finger is down on the gate's touch session when that session goes.
        stale = gate.devices.session_for("PAGE1")
        gate.touch._touching = (stale, 10.0, 10.0)
        chromium.detach_all("PAGE1")
        await _until(rig.showing)
        page = await gate.resume()
        assert page.get("url") == URL
        assert gate.proxy is not None and not gate.proxy.paused

    _run(body, tmp_path)


def test_a_resume_after_the_browser_closed_lets_the_agent_go_instead_of_refusing(tmp_path):
    async def body(rig: Rig) -> None:
        gate, chromium = rig.gate, rig.chromium
        await gate.pause({})
        await gate._grant("c1")
        chromium.close_pipe()
        await _until(lambda: gate.browser_closed)
        page = await asyncio.wait_for(gate.resume(), 10)
        assert page == {}
        assert gate.proxy is not None and not gate.proxy.paused
        assert "browser_closed" in rig.events()
        # Nothing keeps knocking on a pipe that is gone.
        reconnecting = gate.pages._reconnecting
        assert reconnecting is None or reconnecting.done()

    _run(body, tmp_path)


@pytest.mark.parametrize("cause", ["detached", "attach_failed"])
def test_the_follower_never_stays_detached_from_a_live_page(tmp_path, cause):
    async def body(rig: Rig) -> None:
        chromium = rig.chromium
        if cause == "detached":
            chromium.detach_all("PAGE1")
        else:
            # The attach answers, then the mask's install finds the session gone.
            def drop(message: dict[str, Any]) -> None:
                chromium.during.pop("Runtime.addBinding", None)
                chromium.detach_all("PAGE1")

            chromium.during["Runtime.addBinding"] = drop
            chromium.detach_all("PAGE1")
        await _until(lambda: rig.gate.pages.state == "reconnecting" or not rig.showing())
        await _until(rig.showing)
        assert rig.gate.pages.state == "following"

    _run(body, tmp_path)
