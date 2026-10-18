"""A portrait device draws the live screen, not the grey "masking unavailable" frame (QA12-01).

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

THE FIX (mask.py, observer.js): the page's size on screen is its visible size
in CSS px times the page scale (`visualViewport.width * scale`), which is the
device's viewport however the page is zoomed. A page drawn at the window's
own metrics before the override (wider than the device at scale 1) is still
larger than the frame, and still unavailable.

The expected values below are written out, not computed from presets.py.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.device import DeviceController, captured
from agent_worker.livegate.mask import MASK_RGB, MaskState, Outcome, observer_source
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
UNAVAILABLE = (0x55, 0x55, 0x55)


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


def _selected(name: str, orientation: str) -> tuple[DeviceController, GateCdp, Display, MaskState]:
    cdp, display = GateCdp(), Display()
    state = MaskState()
    state.session_up()
    state.frame_attached("TOP", None)
    state.navigated("TOP", "https://login.example.test/")
    controller = DeviceController(cdp, display, state, record=lambda k, f: None)

    async def main() -> None:
        await controller.start()
        await controller.select(name, orientation)

    asyncio.run(main())
    return controller, cdp, display, state


def _report(viewport: list[float], fields: list[dict[str, Any]] = ()) -> str:  # type: ignore[assignment]
    return json.dumps({"v": 1, "k": "report", "doc": "D1", "e": 1, "vp": viewport,
                       "f": list(fields), "t": [], "o": [], "tr": False})


def _password(x: float, y: float, w: float, h: float) -> dict[str, Any]:
    return {"id": 1, "type": "password", "ac": "", "ever": False, "r": [x, y, w, h]}


@pytest.mark.parametrize(("name", "orientation"), sorted(GEOMETRY))
def test_the_framebuffer_the_window_and_the_emulation_agree(name: str, orientation: str):
    (css_w, css_h), display_size, frame_size, _layout = GEOMETRY[(name, orientation)]
    controller, cdp, display, state = _selected(name, orientation)
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
    # And the mask places a page that fills the emulated viewport on that frame.
    state.on_message("TOP", _report([css_w, css_h, 0, 0, 1, css_w, css_h]))
    assert state.apply(frame) is Outcome.MASKED
    assert frame.rgb_at(0, frame.height - 1) == WHITE


@pytest.mark.parametrize(("name", "orientation"), sorted(GEOMETRY))
def test_a_page_zoomed_out_to_fit_the_device_draws_and_is_masked_where_it_is(name: str, orientation: str):
    """The QA's own case: a page laid out at 980 CSS px, which a phone or a portrait tablet zooms out."""
    (css_w, css_h), _display, frame_size, layout = GEOMETRY[(name, orientation)]
    controller, _cdp, _display_, state = _selected(name, orientation)
    page_scale = css_w / layout
    visible_h = css_h / page_scale
    # A password field in the middle of the layout, 200 x 40 CSS px.
    fx, fy = layout / 2 - 100, visible_h / 2 - 20
    # What Chromium's observer reports for that page: innerWidth x innerHeight
    # are the visible area in CSS px (integers), visualViewport the same in
    # floats and the page scale that fits it onto the device.
    state.on_message("TOP", _report([layout, math.floor(visible_h), 0, 0, page_scale, layout, visible_h],
                                    [_password(fx, fy, 200, 40)]))
    frame = captured(Frame.blank(MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT, WHITE), controller.device)
    assert (frame.width, frame.height) == frame_size

    assert state.apply(frame) is Outcome.MASKED, "the zoomed-out page is placed, not a grey frame"
    assert frame.rgb_at(0, 0) != UNAVAILABLE
    page_top = frame.height - css_h
    cx, cy = (fx + 100) * page_scale, page_top + (fy + 20) * page_scale
    assert frame.rgb_at(int(cx), int(cy)) == MASK_RGB, "the field is covered where the zoomed page draws it"
    assert frame.rgb_at(int((fx + 200) * page_scale) + 9, int(cy)) == WHITE, "and nothing far right of it"
    assert frame.rgb_at(int(cx), page_top + int((fy + 40) * page_scale) + 9) == WHITE


@pytest.mark.parametrize(("name", "orientation"), [k for k, v in sorted(GEOMETRY.items()) if v[3] > v[0][0]])
def test_a_zoomed_out_page_draws_from_the_integer_viewport_alone(name: str, orientation: str):
    """A report carrying only `innerWidth` x `innerHeight` and the scale still places the page."""
    (css_w, css_h), _display, _frame, layout = GEOMETRY[(name, orientation)]
    controller, _cdp, _display_, state = _selected(name, orientation)
    page_scale = css_w / layout
    state.on_message("TOP", _report([layout, math.floor(css_h / page_scale), 0, 0, page_scale]))
    frame = captured(Frame.blank(MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT, WHITE), controller.device)
    assert state.apply(frame) is Outcome.MASKED


def test_a_pinch_zoomed_phone_masks_a_field_across_the_whole_page():
    """Pinch 2x on iPhone 15 portrait: innerWidth is half the device, and the page is still all of it."""
    controller, _cdp, _display, state = _selected("iphone-15", "portrait")
    # Scrolled (50, 100) CSS px into the layout; a field at (180, 400) is drawn
    # at ((180-50)*2, (400-100)*2) = (260, 600) on the 393 x 852 page.
    state.on_message("TOP", _report([196, 426, 50, 100, 2, 196.5, 426],
                                    [_password(180, 400, 40, 20)]))
    frame = captured(Frame.blank(MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT, WHITE), controller.device)
    assert state.apply(frame) is Outcome.MASKED
    assert frame.rgb_at(270, 610) == MASK_RGB, "a field in the lower half of a pinch-zoomed page is masked"
    assert frame.rgb_at(339, 639) == MASK_RGB
    assert frame.rgb_at(100, 100) == WHITE


@pytest.mark.parametrize("name", ["iphone-15", "pixel-8", "iphone-se"])
def test_a_page_still_at_the_windows_metrics_is_unavailable(name: str):
    """Before the override lands the page has the window's size at scale 1: it fails closed, as before.

    A portrait phone's window is held at Chromium's 500 px minimum, wider than
    its display. (A window that fits is one pixel past its display, which the
    mask's one-pixel tolerance places, as it always has.)
    """
    controller, _cdp, _display, state = _selected(name, "portrait")
    device = controller.device
    window_w, window_h = max(device.display_width + 1, 500), device.display_height + 1 - 87
    state.on_message("TOP", _report([window_w, window_h, 0, 0, 1, window_w, window_h]))
    frame = captured(Frame.blank(MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT, WHITE), device)
    assert state.apply(frame) is Outcome.UNAVAILABLE


def test_the_observer_reports_the_visual_viewports_size():
    """The mask sizes the page from the visual viewport, so the observer must send it."""
    source = observer_source()
    assert "vv.width" in source and "vv.height" in source


def test_the_gate_draws_a_zoomed_out_iphone_15_portrait_end_to_end(tmp_path: Path):
    """One pass of the gate's capture loop on the QA's device, with the QA's kind of page."""
    cfg = gate_main.GateConfig(ctl_path=str(tmp_path / "ctl.sock"), profile_dir=tmp_path / "profile",
                               downloads_dir=tmp_path / "d", uploads_dir=tmp_path / "u",
                               cdp_host="127.0.0.1", cdp_port=9222, relay_url="ws://relay.test/v1/tunnel")
    gate = gate_main.LiveGate(cfg, record=lambda kind, fields: None)
    controller, _cdp, _display, state = _selected("iphone-15", "portrait")
    state.on_message("TOP", _report([980, 2124, 0, 0, 393 / 980, 980, 852 * 980 / 393]))

    class Screen:
        grabs = 0

        def grab(self) -> Frame:
            self.grabs += 1
            return Frame.blank(MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT, WHITE)

    gate.devices = controller  # type: ignore[assignment]
    gate.mask_state = state
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
    frame, outcome = gate.hub.current.frame, gate.hub.current.outcome
    assert (frame.width, frame.height) == (393, 852)
    assert outcome is Outcome.MASKED, "the live screen draws instead of a grey #555 canvas"
    assert frame.rgb_at(196, 426) == WHITE
