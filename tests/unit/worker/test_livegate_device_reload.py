"""A preset change that changes the user agent reloads the page on screen (part of #1346).

Visual QA 2026-10-15 (task_5e949434) saw Wikipedia's desktop layout squashed
into an iPhone 15 frame. The gate does set the preset's user agent on its own
per-page session (device.py `_apply_page`), but a page loaded before the
switch keeps the document the site served to the desktop user agent until it
navigates again. So `DeviceController.select` reloads the page on screen when
the user agent or the `mobile` flag changed, on the gate's session for that
page, and says so in the timeline. A page on about:blank and a page that is
not shown are left alone, and a same-UA orientation change reloads nothing.

The Chromium here is scripted: what reached it is read off what it received.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_worker.livegate.device import DeviceController

UA_OVERRIDE = "Emulation.setUserAgentOverride"
RELOAD = "Page.reload"


class GateCdp:
    """The gate's pipe: pages with a URL and whether each is shown, and every call."""

    def __init__(self, pages: dict[str, tuple[str, str]]) -> None:
        self.pages = pages  # targetId -> (url, document.visibilityState)
        self.sessions: dict[str, str] = {}  # sessionId -> targetId
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.failing: set[str] = set()

    async def call(self, method: str, params: Any = None, session_id: str | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, params, session_id))
        if method in self.failing:
            raise RuntimeError(f"{method} failed")
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": t, "type": "page", "url": url}
                                    for t, (url, _) in self.pages.items()]}
        if method == "Target.attachToTarget":
            session = f"G{len(self.sessions) + 1}"
            self.sessions[session] = params["targetId"]
            return {"sessionId": session}
        if method == "Runtime.evaluate" and session_id is not None:
            shown = self.pages[self.sessions[session_id]][1]
            return {"result": {"type": "string", "value": shown}}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 1}
        if method == "Browser.getVersion":
            return {"product": "Chrome/153.0.8010.12", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)"}
        return {}

    def session_of(self, target: str) -> str:
        return next(s for s, t in self.sessions.items() if t == target)

    def sent(self, method: str) -> list[str | None]:
        return [s for m, _, s in self.calls if m == method]


class Display:
    async def resize(self, width: int, height: int) -> None:
        pass


PAGES = {
    "SHOWN": ("https://example.test/wiki/Page", "visible"),
    "BLANK": ("about:blank", "visible"),
    "BEHIND": ("https://example.test/other", "hidden"),
}


def _run(pages: dict[str, tuple[str, str]], *steps: tuple[str, str | None],
         failing: frozenset[str] = frozenset()) -> tuple[GateCdp, list[tuple[str, dict[str, str]]], int]:
    """Start on desktop, then select each (preset, orientation); the calls from the last select."""
    cdp = GateCdp(dict(pages))
    records: list[tuple[str, dict[str, str]]] = []
    controller = DeviceController(cdp, Display(), record=lambda k, f: records.append((k, dict(f))))
    mark = 0

    async def main() -> None:
        nonlocal mark
        await controller.start()
        cdp.failing.update(failing)
        for name, orientation in steps:
            mark = len(cdp.calls)
            await controller.select(name, orientation)

    asyncio.run(main())
    return cdp, records, mark


def test_desktop_to_iphone_15_sets_the_user_agent_and_then_reloads_the_page_on_screen():
    cdp, records, mark = _run(PAGES, ("iphone-15", "portrait"))
    shown = cdp.session_of("SHOWN")
    after = cdp.calls[mark:]
    methods = [(m, s) for m, _, s in after]
    assert (RELOAD, shown) in methods, "the page on screen keeps its desktop document: it was never reloaded"
    assert methods.index((UA_OVERRIDE, shown)) < methods.index((RELOAD, shown)), \
        "the reload must come after the new user agent is set, or the site sees the old one"
    assert cdp.sent(RELOAD) == [shown], "only the shown page with a document is reloaded"
    assert ("device_reloaded", {"preset": "iphone-15", "orientation": "portrait"}) in records
    assert ("device_set", {"preset": "iphone-15", "orientation": "portrait"}) in records


def test_a_same_user_agent_orientation_change_does_not_reload():
    cdp, records, mark = _run(PAGES, ("iphone-15", "portrait"), ("iphone-15", "landscape"))
    assert [m for m, _, _ in cdp.calls[mark:] if m == RELOAD] == []
    assert sum(1 for kind, _ in records if kind == "device_reloaded") == 1, "only the desktop -> phone switch"


def test_a_page_on_about_blank_is_not_reloaded():
    cdp, _, _ = _run({"BLANK": PAGES["BLANK"]}, ("iphone-15", "portrait"))
    assert cdp.sent(RELOAD) == []


def test_a_reload_that_fails_does_not_fail_the_switch():
    cdp, records, _ = _run(PAGES, ("pixel-8", "portrait"), failing=frozenset({RELOAD}))
    assert cdp.sent(RELOAD) == [cdp.session_of("SHOWN")]
    assert ("device_set", {"preset": "pixel-8", "orientation": "portrait"}) in records
    assert ("device_reload_failed", {"preset": "pixel-8", "cause": "RuntimeError"}) in records
