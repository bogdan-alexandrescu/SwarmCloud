"""A mobile device preset lays the page out at the preset's size (LB-R3).

THE DEFECT. Re-QA on 2026-10-12 (task_e8cb274977d545a48710, release
f1c242c8): iPhone 15 rotated to landscape drew https://example.com as a grey
block in the top-left of a 2556 x 1267 frame, with a huge clipped "This dom"
and the rest blank. Reproduced the same day against the image's own Chromium
(Playwright 1.63.0, chromium-1243) on Xvfb: headed Chromium draws an emulated
device at ONE screen pixel per CSS pixel and clips the view there, and the
gate's `scale: 3` magnified the content inside an 852 x 393 clip. The fix
(presets.py, device.py): the display is the CSS viewport plus the browser's
UI band, the override carries no `scale`, the window is fitted one pixel past
the display, and touch maps CSS to the screen at 1.

The expected values below are written out, not computed from presets.py, so
a change to the arithmetic there cannot move the test with it. Every row was
measured on that Chromium: `innerWidth` x `innerHeight` and
`devicePixelRatio` equal the preset, and a fixed box at the page's
bottom-right corner is drawn at the frame's last row and column.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate.device import DeviceController
from agent_worker.livegate.presets import (
    BROWSER_UI_HEIGHT,
    MAX_DISPLAY_HEIGHT,
    MAX_DISPLAY_WIDTH,
    all_devices,
    resolve,
)
from agent_worker.livegate.touch import TouchInjector

#: (preset, orientation) -> CSS width, CSS height, devicePixelRatio, mobile and
#: touch, the display (screen pixels), and the screen orientation.
EXPECTED: dict[tuple[str, str], tuple[int, int, float, bool, tuple[int, int], dict[str, Any]]] = {
    ("desktop", "landscape"): (1440, 900, 1.0, False, (1440, 987), {"type": "landscapePrimary", "angle": 0}),
    ("desktop", "portrait"): (900, 1440, 1.0, False, (900, 1527), {"type": "portraitPrimary", "angle": 90}),
    ("iphone-15", "portrait"): (393, 852, 3.0, True, (393, 939), {"type": "portraitPrimary", "angle": 0}),
    ("iphone-15", "landscape"): (852, 393, 3.0, True, (852, 480), {"type": "landscapePrimary", "angle": 90}),
    ("pixel-8", "portrait"): (412, 915, 2.625, True, (412, 1002), {"type": "portraitPrimary", "angle": 0}),
    ("pixel-8", "landscape"): (915, 412, 2.625, True, (915, 499), {"type": "landscapePrimary", "angle": 90}),
    ("iphone-se", "portrait"): (375, 667, 2.0, True, (375, 754), {"type": "portraitPrimary", "angle": 0}),
    ("iphone-se", "landscape"): (667, 375, 2.0, True, (667, 462), {"type": "landscapePrimary", "angle": 90}),
    ("ipad-7", "portrait"): (810, 1080, 2.0, True, (810, 1167), {"type": "portraitPrimary", "angle": 0}),
    ("ipad-7", "landscape"): (1080, 810, 2.0, True, (1080, 897), {"type": "landscapePrimary", "angle": 90}),
}

#: Measured: the tab strip and toolbar, with `--disable-infobars`.
UI_ROWS = 87


class GateCdp:
    """The gate's pipe to Chromium: pages, the windows they sit in, and every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.targets: dict[str, int] = {"PAGE": 1}  # targetId -> windowId
        self.sessions: dict[str, str] = {}  # sessionId -> targetId

    async def call(self, method: str, params: Any = None, session_id: str | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, params, session_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": t, "type": "page"} for t in self.targets]}
        if method == "Target.attachToTarget":
            session = f"G{len(self.sessions) + 1}"
            self.sessions[session] = params["targetId"]
            return {"sessionId": session}
        if method == "Browser.getWindowForTarget":
            return {"windowId": self.targets[params["targetId"]]}
        if method == "Browser.getVersion":
            return {"product": "Chrome/153.0.8010.12", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)"}
        return {}

    def last(self, method: str, session: str | None = None) -> tuple[dict[str, Any], str | None]:
        found = [(p, s) for m, p, s in self.calls if m == method and (session is None or s == session)]
        assert found, f"{method} was never sent"
        return found[-1]

    def bounds(self) -> list[tuple[int, dict[str, Any]]]:
        return [(p["windowId"], p["bounds"]) for m, p, _ in self.calls if m == "Browser.setWindowBounds"]


class Display:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    async def resize(self, width: int, height: int) -> None:
        self.sizes.append((width, height))


class Scale:
    def __init__(self) -> None:
        self.scales: list[float] = []

    def set_scale(self, scale: float) -> None:
        self.scales.append(scale)


def _started() -> tuple[DeviceController, GateCdp, Display, Scale]:
    cdp, display, mask = GateCdp(), Display(), Scale()
    controller = DeviceController(cdp, display, mask, record=lambda k, f: None)
    return controller, cdp, display, mask


def _assert_laid_out(cdp: GateCdp, display: Display, mask: Scale, session: str, name: str, orientation: str,
                     window: int = 1) -> None:
    width, height, dpr, mobile, screen, screen_orientation = EXPECTED[(name, orientation)]
    assert display.sizes[-1] == screen
    assert screen == (width, height + UI_ROWS), "the display is the CSS viewport plus the browser UI"
    assert mask.scales[-1] == 1.0, "the frame shows one screen pixel per CSS pixel"
    fitted = [b for w, b in cdp.bounds() if w == window]
    assert fitted[-1] == {"left": 0, "top": 0, "width": screen[0] + 1, "height": screen[1] + 1,
                          "windowState": "normal"}, "the window is fitted one pixel past the display"
    metrics, _ = cdp.last("Emulation.setDeviceMetricsOverride", session)
    # Exactly these keys: a `scale` (the defect) or a position would move the
    # page off the display's layout.
    assert metrics == {
        "width": width, "height": height, "deviceScaleFactor": dpr, "mobile": mobile,
        "screenWidth": width, "screenHeight": height, "screenOrientation": screen_orientation,
    }
    touch, _ = cdp.last("Emulation.setTouchEmulationEnabled", session)
    assert touch["enabled"] is mobile


@pytest.mark.parametrize(("name", "orientation"), sorted(EXPECTED))
def test_each_preset_and_orientation_lays_the_page_out_at_the_presets_size(name, orientation):
    controller, cdp, display, mask = _started()

    async def main() -> None:
        await controller.start()
        await controller.select(name, orientation)

    asyncio.run(main())
    session = controller.session_for("PAGE")
    assert session is not None
    _assert_laid_out(cdp, display, mask, session, name, orientation)


@pytest.mark.parametrize(("name", "orientation"), sorted(EXPECTED))
def test_the_frame_is_the_page_and_the_browser_ui_and_nothing_else(name, orientation):
    width, height, _dpr, _mobile, screen, _o = EXPECTED[(name, orientation)]
    device = resolve(name, orientation)
    assert BROWSER_UI_HEIGHT == UI_ROWS
    assert (device.display_width, device.display_height) == screen
    assert (device.page_width, device.page_height) == (width, height)
    assert device.page_top == UI_ROWS
    assert device.screen_scale == 1.0 and device.scale == EXPECTED[(name, orientation)][2]


def test_rotating_iphone_15_lays_the_page_out_landscape_not_a_clipped_corner():
    """The re-QA's own sequence: iPhone 15, then rotate to landscape, then back."""
    controller, cdp, display, mask = _started()

    async def main() -> None:
        await controller.start()
        await controller.select("iphone-15", "portrait")
        await controller.select("iphone-15", "landscape")

    asyncio.run(main())
    session = controller.session_for("PAGE")
    assert session is not None
    _assert_laid_out(cdp, display, mask, session, "iphone-15", "landscape")
    # The frame the defect drew in: 852 x 393 at 3 is 2556 x 1179, plus the UI.
    assert (2556, 1267) not in display.sizes
    assert all("scale" not in p for m, p, _ in cdp.calls if m == "Emulation.setDeviceMetricsOverride")

    asyncio.run(controller.select("iphone-15", "portrait"))
    _assert_laid_out(cdp, display, mask, session, "iphone-15", "portrait")


def test_a_new_page_in_its_own_window_has_the_window_fitted_and_the_device_set():
    controller, cdp, display, mask = _started()

    async def main() -> None:
        await controller.start()
        await controller.select("iphone-15", "landscape")
        cdp.targets["POPUP"] = 2  # a window.open() with features: a new window
        cdp.targets["TAB"] = 1  # a new tab in the same window
        for target in ("POPUP", "TAB"):
            controller.on_event({"method": "Target.targetCreated",
                                 "params": {"targetInfo": {"targetId": target, "type": "page"}}})
        for _ in range(100):
            await asyncio.sleep(0)
            if controller.session_for("POPUP") and controller.session_for("TAB") and len(cdp.bounds()) >= 3:
                break

    asyncio.run(main())
    for target, window in (("POPUP", 2), ("TAB", 1)):
        session = controller.session_for(target)
        assert session is not None
        _assert_laid_out(cdp, display, mask, session, "iphone-15", "landscape", window=window)
    # Window 1 was fitted once for the default device and once for the iPhone;
    # the tab in it did not refit it.
    assert [w for w, _ in cdp.bounds()] == [1, 1, 2]


def test_the_device_follows_the_page_across_its_navigations():
    """The override is the target's: a navigation changes the document, not the gate's session."""
    controller, cdp, display, mask = _started()

    async def main() -> None:
        await controller.start()
        await controller.select("pixel-8", "landscape")
        session = controller.session_for("PAGE")
        controller.on_event({"method": "Target.targetInfoChanged", "params": {"targetInfo": {
            "targetId": "PAGE", "type": "page", "url": "https://example.com/"}}})
        await asyncio.sleep(0)
        assert controller.session_for("PAGE") == session
        assert [m for m, _, _ in cdp.calls].count("Target.attachToTarget") == 1
        await controller.select("pixel-8", "portrait")

    asyncio.run(main())
    session = controller.session_for("PAGE")
    assert session is not None
    _assert_laid_out(cdp, display, mask, session, "pixel-8", "portrait")


def test_a_touch_maps_one_screen_pixel_to_one_css_pixel():
    devices = resolve("iphone-15", "landscape")

    class Devices:
        device = devices

        @staticmethod
        def session_for(target_id: str) -> str | None:
            return "G1" if target_id == "PAGE" else None

    class X:
        async def pointer_event(self, buttons: int, x: int, y: int) -> None:
            raise AssertionError("a touch in the page is not a mouse")

        async def release_all(self) -> None:
            pass

    cdp = GateCdp()

    async def main() -> None:
        touch = TouchInjector(X(), cdp, Devices())
        touch.begin("PAGE")
        # Near the page's bottom-right corner. The frame is the page alone on a
        # phone (LB-R5), so its coordinates are the page's.
        await touch.pointer_event(1, 840, 380)
        await touch.pointer_event(0, 840, 380)

    asyncio.run(main())
    starts = [p for m, p, _ in cdp.calls if m == "Input.dispatchTouchEvent" and p["type"] == "touchStart"]
    assert starts[0]["touchPoints"] == [{"x": 840.0, "y": 380.0, "id": 0}]


def test_chromium_starts_without_the_infobar_and_one_pixel_past_the_desktop_display(tmp_path: Path):
    cfg = gate_main.GateConfig(ctl_path=str(tmp_path / "ctl.sock"), profile_dir=tmp_path / "profile",
                               downloads_dir=tmp_path / "d", uploads_dir=tmp_path / "u",
                               cdp_host="127.0.0.1", cdp_port=9222, relay_url="ws://relay.test/v1/tunnel")
    args = gate_main.chromium_args(cfg)
    assert "--disable-infobars" in args
    assert "--window-size=1441,988" in args


def test_xvfb_starts_at_the_largest_display_a_device_needs():
    assert (MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT) == (1440, 1527)  # desktop, both ways
    assert all(d.display_width <= MAX_DISPLAY_WIDTH and d.display_height <= MAX_DISPLAY_HEIGHT
               for d in all_devices())
