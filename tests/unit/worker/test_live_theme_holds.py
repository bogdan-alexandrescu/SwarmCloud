"""The gate's theme holds against the agent's own Playwright (QAFIX2-QATOOL, WG-ENV2; part of #1680).

Visual QA run 2 (wf_b6e2441b419f423a84eb, 2026-10-20) ran `swarm-handoff theme
dark` and then read `matchMedia('(prefers-color-scheme: dark)')` false in a
fresh tab, so its dark flow ran on the console's own Dark switch instead.

The gate set the override once per page, from its own session (device.py),
and nothing put it back. But a media override in Chromium is the PAGE's, and
the agent writes over it. Playwright sends
`Emulation.setEmulatedMedia` with `prefers-color-scheme: light` on every page
it attaches to, light being its default `colorScheme`. And Blink lifts a
feature a session set when that session detaches, the gate's value with it.
So a fresh tab the agent opened was dark for as long as it took the agent's
Playwright to attach, and light after.

`Chromium` below is that model: one effective value per page, the last
write wins, a detaching session clears what it wrote, and a session sees
`Page.frameNavigated` only once it has sent `Page.enable`. Each test drives the
gate's `theme` command, then plays the agent's part and reads the page's
effective value. No Chromium, no network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_worker.livegate import device as device_module
from agent_worker.livegate.device import DeviceController

MEDIA = "Emulation.setEmulatedMedia"
#: The session the agent's root commands arrive on, through the proxy.
AGENT_ROOT = "B-agent"


class Chromium:
    """The gate's pipe, plus each page's effective `prefers-color-scheme`."""

    def __init__(self, pages: tuple[str, ...] = ("P1",)) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.sessions: dict[str, str] = {}  # session -> targetId
        self.scheme: dict[str, str] = {}  # targetId -> the page's effective value ("" = Chromium's own)
        self.wrote: dict[str, set[str]] = {}  # targetId -> sessions that set the feature
        self.page_enabled: set[str] = set()
        self._n = 0

    async def call(self, method: str, params: Any = None, session_id: str | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, params, session_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": t, "type": "page"} for t in self.pages]}
        if method == "Target.attachToTarget":
            self._n += 1
            session = f"S{self._n}"
            self.sessions[session] = params["targetId"]
            return {"sessionId": session}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 1}
        if method == "Browser.getVersion":
            return {"product": "Chrome/130.0.6723.58", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)"}
        if method == MEDIA and session_id is not None:
            self._write(session_id, params["features"][0]["value"])
        if method == "Page.enable" and session_id is not None:
            self.page_enabled.add(session_id)
        return {}

    def _write(self, session: str, value: str) -> None:
        target = self.sessions[session]
        self.scheme[target] = value
        self.wrote.setdefault(target, set()).add(session)

    # -- the agent's part ---------------------------------------------------

    def agent_attaches(self, target: str) -> tuple[str, dict[str, Any]]:
        """Playwright attaches to `target`: the event every listener sees."""
        self._n += 1
        session = f"A{self._n}"
        self.sessions[session] = target
        return session, {"method": "Target.attachedToTarget", "sessionId": AGENT_ROOT,
                         "params": {"sessionId": session, "waitingForDebugger": False,
                                    "targetInfo": {"targetId": target, "type": "page"}}}

    def agent_media(self, session: str, value: str) -> None:
        """The agent's session sends `Emulation.setEmulatedMedia` (Playwright's init sends light)."""
        self._write(session, value)

    def agent_detaches(self, session: str) -> dict[str, Any]:
        """Blink lifts what a detaching session set: the page goes back to Chromium's own."""
        target = self.sessions.pop(session)
        if session in self.wrote.get(target, set()):
            self.wrote[target].discard(session)
            self.scheme[target] = ""
        return {"method": "Target.detachedFromTarget", "sessionId": AGENT_ROOT,
                "params": {"sessionId": session, "targetId": target}}

    def navigates(self, target: str, *, restored_by: str | None = None,
                  parent: str | None = None) -> list[dict[str, Any]]:
        """A navigation of `target`: a new renderer restores `restored_by`'s value
        last, and every session with `Page.enable` is told the frame navigated."""
        if restored_by is not None:
            self.scheme[target] = "light"
        frame: dict[str, Any] = {"id": parent and "F-child" or target, "url": "https://console.test/"}
        if parent is not None:
            frame["parentId"] = parent
        return [{"method": "Page.frameNavigated", "sessionId": s, "params": {"frame": frame}}
                for s, t in self.sessions.items() if t == target and s in self.page_enabled]

    def gate_media(self) -> list[tuple[dict[str, Any], str | None]]:
        return [(p, s) for m, p, s in self.calls if m == MEDIA]


class Display:
    async def resize(self, width: int, height: int) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    # The wait after another session attaches, cut to nothing so the test does
    # not sleep. `raising=False`: the setting is part of the change under test.
    monkeypatch.setattr(device_module, "THEME_SETTLE_SECONDS", 0.0, raising=False)


async def _spin(until=lambda: False) -> None:
    for _ in range(200):
        await asyncio.sleep(0)
        if until():
            return


async def _started(chromium: Chromium) -> DeviceController:
    device = DeviceController(chromium, Display(), record=lambda k, f: None)
    await device.start()
    return device


async def _fresh_tab(chromium: Chromium, device: DeviceController, target: str) -> None:
    chromium.pages.append(target)
    device.on_event({"method": "Target.targetCreated",
                     "params": {"targetInfo": {"targetId": target, "type": "page"}}})
    await _spin(lambda: chromium.scheme.get(target) == "dark")


def test_a_fresh_tab_the_agent_opens_stays_dark_after_the_agents_playwright_attaches_with_light():
    async def main() -> Chromium:
        chromium = Chromium()
        device = await _started(chromium)
        await device.handle_theme({"theme": "dark"})
        await _fresh_tab(chromium, device, "P2")
        # MUTATION: drop the gate's attach of a page created later and the fresh tab is never dark.
        assert chromium.scheme.get("P2") == "dark"
        agent, event = chromium.agent_attaches("P2")
        device.on_event(event)
        chromium.agent_media(agent, "light")
        await _spin(lambda: chromium.scheme["P2"] == "dark")
        return chromium

    chromium = asyncio.run(main())
    # MUTATION: drop the re-apply after another session attaches and this reads "light",
    # the run-2 measurement (WG-ENV2).
    assert chromium.scheme["P2"] == "dark"


def test_every_page_the_agent_attaches_to_is_put_back_to_the_theme_not_only_the_fresh_one():
    async def main() -> Chromium:
        chromium = Chromium(pages=("P1", "P2"))
        device = await _started(chromium)
        await device.handle_theme({"theme": "dark"})
        for target in ("P1", "P2"):
            agent, event = chromium.agent_attaches(target)
            device.on_event(event)
            chromium.agent_media(agent, "light")
        await _spin(lambda: chromium.scheme == {"P1": "dark", "P2": "dark"})
        return chromium

    # MUTATION: re-apply on the first held page only and P2 reads "light".
    assert asyncio.run(main()).scheme == {"P1": "dark", "P2": "dark"}


def test_a_navigation_that_restores_the_agents_light_last_is_put_back_to_dark():
    async def main() -> Chromium:
        chromium = Chromium()
        device = await _started(chromium)
        await device.handle_theme({"theme": "dark"})
        agent, event = chromium.agent_attaches("P1")
        device.on_event(event)
        chromium.agent_media(agent, "light")
        await _spin(lambda: chromium.scheme["P1"] == "dark")
        events = chromium.navigates("P1", restored_by=agent)
        # MUTATION: skip `Page.enable` on the gate's session and Chromium never tells it the page navigated.
        assert events, "the gate's own session is told of the navigation"
        for message in events:
            device.on_event(message)
        await _spin(lambda: chromium.scheme["P1"] == "dark")
        return chromium

    # MUTATION: drop the re-apply on a top-frame navigation and this reads "light".
    assert asyncio.run(main()).scheme["P1"] == "dark"


def test_the_agents_playwright_detaching_does_not_lift_the_theme():
    async def main() -> Chromium:
        chromium = Chromium()
        device = await _started(chromium)
        await device.handle_theme({"theme": "dark"})
        agent, event = chromium.agent_attaches("P1")
        device.on_event(event)
        chromium.agent_media(agent, "dark")  # the agent's own emulate_media, to the same scheme
        device.on_event(chromium.agent_detaches(agent))
        await _spin(lambda: chromium.scheme["P1"] == "dark")
        return chromium

    # MUTATION: drop the re-apply on another session's detach and Blink's clear leaves "" (light).
    assert asyncio.run(main()).scheme["P1"] == "dark"


def test_a_child_frames_navigation_sends_nothing():
    async def main() -> tuple[Chromium, int]:
        chromium = Chromium()
        device = await _started(chromium)
        await device.handle_theme({"theme": "dark"})
        before = len(chromium.gate_media())
        for message in chromium.navigates("P1", parent="P1"):
            device.on_event(message)
        await _spin()
        return chromium, before

    chromium, before = asyncio.run(main())
    # MUTATION: re-apply on every frameNavigated and each iframe's load sends the theme again.
    assert len(chromium.gate_media()) == before


def test_with_no_theme_asked_for_the_agents_attach_navigation_and_detach_send_nothing():
    """The control: a browser nobody asked to theme gets exactly the calls it got before."""

    async def main() -> Chromium:
        chromium = Chromium()
        device = await _started(chromium)
        agent, event = chromium.agent_attaches("P1")
        device.on_event(event)
        chromium.agent_media(agent, "light")
        for message in chromium.navigates("P1", restored_by=agent):
            device.on_event(message)
        device.on_event(chromium.agent_detaches(agent))
        await _spin()
        return chromium

    chromium = asyncio.run(main())
    # MUTATION: re-apply whatever the theme and `system` sends an empty override nobody asked for.
    assert chromium.gate_media() == []
    # MUTATION: enable `Page` on every gate session and an unthemed browser gets a call it never got.
    assert not [c for c in chromium.calls if c[0] == "Page.enable"]


def test_back_to_system_the_agents_light_is_left_alone():
    async def main() -> tuple[Chromium, int]:
        chromium = Chromium()
        device = await _started(chromium)
        await device.handle_theme({"theme": "dark"})
        await device.handle_theme({"theme": "system"})
        before = len(chromium.gate_media())
        agent, event = chromium.agent_attaches("P1")
        device.on_event(event)
        chromium.agent_media(agent, "light")
        for message in chromium.navigates("P1", restored_by=agent):
            device.on_event(message)
        await _spin()
        return chromium, before

    chromium, before = asyncio.run(main())
    # MUTATION: keep re-applying after `system` and the agent's own light is overwritten by "".
    assert len(chromium.gate_media()) == before and chromium.scheme["P1"] == "light"
