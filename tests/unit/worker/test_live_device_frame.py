"""A mobile device preset shows the page, not Chrome's desktop UI (LB-R5).

THE DEFECT. Re-QA on 2026-10-12 10:24-10:30 PDT (task_3fda0f52db4640c6a91e,
after release 2f00a1e2): with LB-R3 in, iPhone 15 portrait laid example.com
out correctly at 393 x 852, but the frame still carried Chrome's desktop tab
strip ("Example Domain", "+") and the omnibox above the page, inside the
phone outline -- nothing like a phone browser. The owner's decision: a mobile
preset shows only the page; the console's origin strip, which reads the top
frame's origin over CDP (section 5.7), already shows the URL.

THE FIX keeps the display and the window as they are (the page at the
bottom, the browser's UI band above it: the agent's CDP targets, the
bottom-anchored placement and the desktop preset are all unchanged) and cuts
the captured frame to the page rows on a mobile preset. The controller's
pointer arrives in that frame's coordinates, so the gate moves it back down
by the band before it reaches the X display.

The expected sizes are written out, not computed from presets.py, so a change
to the arithmetic there cannot move the test with it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.presets import resolve
from agent_worker.livegate.touch import TouchInjector

#: (preset, orientation) -> the captured frame, in screen pixels.
MOBILE_FRAMES: dict[tuple[str, str], tuple[int, int]] = {
    ("iphone-15", "portrait"): (393, 852),
    ("iphone-15", "landscape"): (852, 393),
    ("pixel-8", "portrait"): (412, 915),
    ("pixel-8", "landscape"): (915, 412),
    ("iphone-se", "portrait"): (375, 667),
    ("iphone-se", "landscape"): (667, 375),
    ("ipad-7", "portrait"): (810, 1080),
    ("ipad-7", "landscape"): (1080, 810),
}

#: The desktop keeps its tab strip and address bar: the viewport plus the band.
DESKTOP_FRAMES: dict[tuple[str, str], tuple[int, int]] = {
    ("desktop", "landscape"): (1440, 987),
    ("desktop", "portrait"): (900, 1527),
}

#: Measured: the tab strip and toolbar, with `--disable-infobars` (LB-R3).
UI_ROWS = 87

UI_GREY = (0xDE, 0xE1, 0xE6)
PAGE_WHITE = (0xFF, 0xFF, 0xFF)
LAST_ROW = (0x12, 0x34, 0x56)


class Devices:
    def __init__(self, name: str, orientation: str | None = None) -> None:
        self.device = resolve(name, orientation)

    def session_for(self, target_id: str) -> str | None:
        return "G1" if target_id == "PAGE" else None


class Screen:
    """Xvfb's framebuffer: the browser's UI band over the page, as Chromium draws it.

    `extra` rows and columns past the display stand for a `-fbdir` file that
    kept Xvfb's starting size after a RandR shrink (device.py, `crop`).
    """

    def __init__(self, width: int, height: int, *, extra: int = 0) -> None:
        self.frame = Frame.blank(width + extra, height + extra, (0, 0, 0))
        self.frame.fill(0, 0, width, UI_ROWS, UI_GREY)
        self.frame.fill(0, UI_ROWS, width, height - 1, PAGE_WHITE)
        self.frame.fill(0, height - 1, width, height, LAST_ROW)
        self.grabs = 0

    def grab(self) -> Frame:
        self.grabs += 1
        return Frame(self.frame.width, self.frame.height, self.frame.stride, bytearray(self.frame.pixels))


def _captured(tmp_path: Path, name: str, orientation: str, *, extra: int = 0) -> tuple[Frame, str | None]:
    """One pass of the gate's capture loop on `name` in `orientation`: what the viewer is sent."""
    cfg = gate_main.GateConfig(ctl_path=str(tmp_path / "ctl.sock"), profile_dir=tmp_path / "profile",
                               downloads_dir=tmp_path / "d", uploads_dir=tmp_path / "u",
                               cdp_host="127.0.0.1", cdp_port=9222, relay_url="ws://relay.test/v1/tunnel")
    gate = gate_main.LiveGate(cfg, record=lambda kind, fields: None)
    devices = Devices(name, orientation)
    device = devices.device
    gate.devices = devices  # type: ignore[assignment]
    screen = Screen(device.display_width, device.display_height, extra=extra)
    gate.framebuffer = screen

    async def main() -> None:
        gate._watching.set()
        task = asyncio.create_task(gate._capture())
        while gate.hub.version == 0:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert screen.grabs >= 1
    return gate.hub.current.frame, gate.hub.current.reason


@pytest.mark.parametrize(("name", "orientation"), sorted(MOBILE_FRAMES))
@pytest.mark.parametrize("extra", [0, 64], ids=["fbdir-shrank", "fbdir-kept-its-size"])
def test_a_mobile_preset_sends_the_page_and_no_browser_ui_rows(tmp_path: Path, name: str, orientation: str,
                                                                extra: int):
    frame, reason = _captured(tmp_path, name, orientation, extra=extra)
    assert (frame.width, frame.height) == MOBILE_FRAMES[(name, orientation)], \
        "the frame is the preset's viewport: no tab strip, no address bar"
    assert reason is None
    assert frame.rgb_at(0, 0) == PAGE_WHITE, "the frame's first row is the page's first row"
    assert frame.rgb_at(frame.width - 1, 0) == PAGE_WHITE
    assert frame.rgb_at(0, frame.height - 1) == LAST_ROW, "and its last row the page's last row"
    assert all(frame.rgb_at(x, y) != UI_GREY for y in (0, frame.height // 2) for x in range(frame.width))


@pytest.mark.parametrize(("name", "orientation"), sorted(DESKTOP_FRAMES))
def test_the_desktop_preset_still_sends_its_tab_strip_and_address_bar(tmp_path: Path, name: str,
                                                                      orientation: str):
    frame, reason = _captured(tmp_path, name, orientation)
    assert (frame.width, frame.height) == DESKTOP_FRAMES[(name, orientation)]
    assert reason is None
    assert frame.rgb_at(0, 0) == UI_GREY and frame.rgb_at(0, UI_ROWS - 1) == UI_GREY
    assert frame.rgb_at(0, UI_ROWS) == PAGE_WHITE
    assert frame.rgb_at(0, frame.height - 1) == LAST_ROW


# ------------------------------------------------- the pointer, in that frame


class FakeCdp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def call(self, method: str, params: Any = None, session_id: str | None = None) -> dict[str, Any]:
        self.calls.append((method, dict(params or {}), session_id))
        return {}


class FakeX:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def pointer_event(self, buttons: int, x: int, y: int) -> None:
        self.events.append(("pointer", buttons, x, y))

    async def release_all(self) -> None:
        pass


def _drive(devices: Devices, events: list[tuple[int, int, int]], *, touch_on: bool = True
           ) -> tuple[FakeCdp, FakeX]:
    async def main() -> tuple[FakeCdp, FakeX]:
        cdp, x11 = FakeCdp(), FakeX()
        touch = TouchInjector(x11, cdp, devices)
        touch.set_touch(touch_on)
        touch.begin("PAGE")
        for buttons, x, y in events:
            await touch.pointer_event(buttons, x, y)
        return cdp, x11

    return asyncio.run(main())


def test_a_tap_on_the_mobile_frames_first_row_is_a_touch_on_the_pages_first_row():
    cdp, x11 = _drive(Devices("iphone-15"), [(1, 100, 0), (0, 100, 0)])
    starts = [p for m, p, _ in cdp.calls if m == "Input.dispatchTouchEvent" and p["type"] == "touchStart"]
    assert starts[0]["touchPoints"] == [{"x": 100.0, "y": 0.0, "id": 0}]
    assert x11.events == [], "the frame has no address bar left to click"


def test_a_tap_on_the_mobile_frames_last_row_is_a_touch_on_the_pages_last_row():
    cdp, _ = _drive(Devices("pixel-8", "landscape"), [(1, 914, 411), (0, 914, 411)])
    starts = [p for m, p, _ in cdp.calls if m == "Input.dispatchTouchEvent" and p["type"] == "touchStart"]
    assert starts[0]["touchPoints"] == [{"x": 914.0, "y": 411.0, "id": 0}]


def test_with_touch_off_the_mobile_pointer_reaches_the_screen_below_the_browser_ui():
    cdp, x11 = _drive(Devices("iphone-se"), [(1, 50, 10)], touch_on=False)
    assert [m for m, _, _ in cdp.calls if m == "Input.dispatchTouchEvent"] == []
    assert x11.events == [("pointer", 1, 50, UI_ROWS + 10)], "frame row 10 is screen row 87 + 10"


def test_the_desktop_pointer_is_where_the_frame_says():
    _, x11 = _drive(Devices("desktop"), [(1, 10, 40), (0, 10, 500)])
    assert x11.events == [("pointer", 1, 10, 40), ("pointer", 0, 10, 500)]


def test_a_frame_shorter_than_the_page_top_is_cut_from_its_last_row_not_raised():
    # A display caught mid-resize: the grab is smaller than the device's display.
    from agent_worker.livegate.device import captured

    device = resolve("iphone-15")
    out = captured(Frame.blank(393, 50, PAGE_WHITE), device)
    assert (out.width, out.height) == (393, 1)
