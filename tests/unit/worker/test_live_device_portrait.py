"""A portrait device draws the live screen, never a grey frame (QA12-01, LIVE-NOMASK).

THE DEFECT. Live-browser QA p12 on 2026-10-13 (task_4defa61c5eea4a5c87f5):
switching to iPhone 15 (393 x 852) or iPad portrait (810 x 1080) left the live
screen solid #555 for 40 s and more, and after a reload; iPad landscape and
Desktop drew. POST /v1/tasks/{id}/live/device answered 200. #555 is
`mask.UNAVAILABLE_RGB`: the display, the window and the emulation had all been
set, and the MASK refused to place the page.

THE CAUSE. A phone or tablet preset is emulated with `mobile: true`, so
Chromium honours (or, without a `<meta name=viewport>`, defaults) the page's
viewport the way a phone does: a page laid out wider than the device -- 980
CSS px when it has no viewport tag, its content's width when that overflows --
is ZOOMED OUT to fit. `innerWidth` x `innerHeight` then report the visible area
in CSS px (980 x 2123 on iPhone 15 portrait) and `visualViewport.scale` the
page scale that fits it onto the device (393 / 980). The mask took the page's
size on screen as `innerWidth * screen_scale`, 980 screen px on a 393 px
frame, called that "a viewport larger than the frame" and blanked everything.
Every device narrower than the page's layout width did it: iPad landscape
(1080) and the desktop (not `mobile`, so never zoomed) did not. The same
arithmetic shrank the page area under a pinch zoom (innerWidth 196 at scale
2), so a field outside that shrunken box went unmasked.

THE FIX (mask.py, observer.js, 2026-10-13) sized the page from the visual
viewport. Since LIVE-NOMASK (BGD, 2026-10-25, design §5.3) the gate paints
nothing at all, so no device, zoom or page width can turn a frame grey:
what is held here is that the framebuffer, the window and the emulation
agree, and that the capture loop publishes the device's frame as captured.

The expected values below are written out, not computed from presets.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.device import DeviceController, captured
from agent_worker.livegate.presets import MAX_DISPLAY_HEIGHT, MAX_DISPLAY_WIDTH

#: (preset, orientation) -> emulated CSS width x height, the Xvfb display, the
#: captured frame the viewer is sent, and the layout width Chromium gives a
#: page with no viewport tag (980 on a mobile preset narrower than that; the
#: device's own width otherwise, which is no zoom).
GEOMETRY: dict[tuple[str, str], tuple[tuple[int, int], tuple[int, int], tuple[int, int], int]] = {
    ("desktop", "landscape"): ((1440, 900), (1440, 987), (1440, 987), 1440),
    ("desktop", "portrait"): ((900, 1440), (900, 1527), (900, 1527), 900),
    ("iphone-15", "portrait"): ((393, 852), (393, 939), (393, 852), 980),
    ("iphone-15", "landscape"): ((852, 393), (852, 480), (852, 393), 980),
    ("pixel-8", "portrait"): ((412, 915), (412, 1002), (412, 915), 980),
    ("pixel-8", "landscape"): ((915, 412), (915, 499), (915, 412), 980),
    ("iphone-se", "portrait"): ((375, 667), (375, 754), (375, 667), 980),
    ("iphone-se", "landscape"): ((667, 375), (667, 462), (667, 375), 980),
    ("ipad-7", "portrait"): ((810, 1080), (810, 1167), (810, 1080), 980),
    ("ipad-7", "landscape"): ((1080, 810), (1080, 897), (1080, 810), 1080),
}

WHITE = (0xFF, 0xFF, 0xFF)


class GateCdp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def call(self, method: str, params: Any = None, session_id: str | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, params, session_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "PAGE", "type": "page"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "G1"}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 1}
        if method == "Browser.getVersion":
            return {"product": "Chrome/153.0.8010.12", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)"}
        return {}

    def last(self, method: str) -> dict[str, Any]:
        return [p for m, p, _ in self.calls if m == method][-1]


class Display:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    async def resize(self, width: int, height: int) -> None:
        self.sizes.append((width, height))


def _selected(name: str, orientation: str) -> tuple[DeviceController, GateCdp, Display]:
    cdp, display = GateCdp(), Display()
    controller = DeviceController(cdp, display, record=lambda k, f: None)

    async def main() -> None:
        await controller.start()
        await controller.select(name, orientation)

    asyncio.run(main())
    return controller, cdp, display


@pytest.mark.parametrize(("name", "orientation"), sorted(GEOMETRY))
def test_the_framebuffer_the_window_and_the_emulation_agree(name: str, orientation: str):
    (css_w, css_h), display_size, frame_size, _layout = GEOMETRY[(name, orientation)]
    controller, cdp, display = _selected(name, orientation)
    device = controller.device

    # Xvfb: the CSS viewport plus the browser's UI band, inside what Xvfb starts at.
    assert display.sizes[-1] == display_size
    assert display_size[0] <= MAX_DISPLAY_WIDTH and display_size[1] <= MAX_DISPLAY_HEIGHT
    # The window: one pixel past the display each way (device.py, WINDOW_OVERHANG).
    bounds = cdp.last("Browser.setWindowBounds")["bounds"]
    assert (bounds["width"], bounds["height"]) == (display_size[0] + 1, display_size[1] + 1)
    # The emulation: the same CSS viewport, as the screen too.
    metrics = cdp.last("Emulation.setDeviceMetricsOverride")
    assert (metrics["width"], metrics["height"]) == (css_w, css_h)
    assert (metrics["screenWidth"], metrics["screenHeight"]) == (css_w, css_h)
    assert "scale" not in metrics
    # The captured frame, cut from a framebuffer that kept Xvfb's starting size.
    frame = captured(Frame.blank(MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT, WHITE), device)
    assert (frame.width, frame.height) == frame_size
    assert frame.rgb_at(0, frame.height - 1) == WHITE


def test_the_gate_draws_a_zoomed_out_iphone_15_portrait_end_to_end(tmp_path: Path):
    """One pass of the gate's capture loop on the QA's device: the device's frame, as captured."""
    cfg = gate_main.GateConfig(ctl_path=str(tmp_path / "ctl.sock"), profile_dir=tmp_path / "profile",
                               downloads_dir=tmp_path / "d", uploads_dir=tmp_path / "u",
                               cdp_host="127.0.0.1", cdp_port=9222, relay_url="ws://relay.test/v1/tunnel")
    gate = gate_main.LiveGate(cfg, record=lambda kind, fields: None)
    controller, _cdp, _display = _selected("iphone-15", "portrait")

    class Screen:
        grabs = 0

        def grab(self) -> Frame:
            self.grabs += 1
            return Frame.blank(MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT, WHITE)

    gate.devices = controller  # type: ignore[assignment]
    gate.framebuffer = Screen()  # type: ignore[assignment]

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
    frame, reason = gate.hub.current.frame, gate.hub.current.reason
    assert (frame.width, frame.height) == (393, 852)
    assert reason is None
    assert all(frame.rgb_at(x, y) == WHITE for x in range(0, 393, 7) for y in range(0, 852, 11)), \
        "the live screen draws instead of a grey #555 canvas"
