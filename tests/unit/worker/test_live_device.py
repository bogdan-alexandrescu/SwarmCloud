"""The live browser as a phone or a tablet, and touch from the console (L3b, LB-6).

docs/design/live-browser.md section 6.1. The acceptance test of lane L3b:

* each preset sets metrics, user agent and touch on the gate-owned context and
  resizes the display;
* an unknown preset name is refused;
* the agent's own `Emulation.*` calls are refused by the method policy;
* in control with touch on, a press/move/release arrives as
  touchStart/Move/End;
* in view mode no input is sent.

Until LIVE-NOMASK (#2298) the list ended "the mask covers a password field on
every preset where the page draws it"; the live view paints nothing since
BGD's decision of 2026-10-25 (design §5.3), so that case went with the mask.

The expected sizes below are the design's list (section 6.1), written out
again here rather than read from presets.py, so a preset that drifts from the
design fails here. Every test drives the real modules against a scripted
Chromium (what reached it is read off what it received) and, for the display,
a scripted X server on a unix socket. No Chromium, no Xvfb, no network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import cdppolicy
from agent_worker.livegate import device as device_module
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.cdppolicy import PolicyPaths
from agent_worker.livegate.cdpproxy import CdpProxy
from agent_worker.livegate.device import (
    DeviceController,
    DeviceRefused,
    DisplayError,
    XRandrDisplay,
    crop,
)
from agent_worker.livegate.presets import (
    BROWSER_UI_HEIGHT,
    MAX_DISPLAY_HEIGHT,
    MAX_DISPLAY_WIDTH,
    PRESETS,
    UnknownPreset,
    all_devices,
    resolve,
)
from agent_worker.livegate.touch import SCROLL_STEP, TouchInjector

# The design's list (section 6.1): CSS size in the natural orientation, scale,
# whether it is a touch device, and what its user agent must say.
DESIGN = {
    "desktop": (1440, 900, 1.0, False, "X11; Linux"),
    "iphone-15": (393, 852, 3.0, True, "iPhone OS"),
    # QA-KIT (owner decision 2026-10-19): the 390-wide phone a visual QA sweep
    # names, with iPhone 15's mobile flags.
    "phone-390": (390, 844, 3.0, True, "iPhone OS"),
    # PHONE-380 (BGD, 2026-10-26, part of #2136): AI Studio's phone mock-up
    # frame, 380 x 800, with phone-390's mobile flags, user agent and scale.
    "phone-380": (380, 800, 3.0, True, "iPhone OS"),
    "pixel-8": (412, 915, 2.625, True, "Android"),
    "iphone-se": (375, 667, 2.0, True, "iPhone OS"),
    "ipad-7": (810, 1080, 2.0, True, "Macintosh"),
}

CHROME_VERSION = "130.0.6723.58"
CHROME_UA = f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VERSION} Safari/537.36"
WHITE = (0xFF, 0xFF, 0xFF)
DEVICE_METHODS = (
    "Emulation.setDeviceMetricsOverride",
    "Emulation.clearDeviceMetricsOverride",
    "Emulation.setUserAgentOverride",
    "Emulation.setTouchEmulationEnabled",
    "Emulation.setEmitTouchEventsForMouse",
    "Network.setUserAgentOverride",
)


def _expected(name: str, orientation: str) -> tuple[int, int, float, bool, str]:
    w, h, scale, touch, ua = DESIGN[name]
    natural_landscape = w > h
    if (orientation == "landscape") != natural_landscape:
        w, h = h, w
    return w, h, scale, touch, ua


DEVICES = [(name, o) for name in DESIGN for o in ("portrait", "landscape")]


# ---------------------------------------------------------------- fakes


class FakeChromium:
    """Answers the gate's pipe the way Chromium does, keeping what it received."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.proxy: CdpProxy | None = None
        self.contexts: dict[str, str] = {}  # targetId -> browserContextId
        self.sessions: dict[str, str] = {}  # sessionId -> targetId
        self._n = 0
        self._tasks: set[asyncio.Task[None]] = set()

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def page(self, context: str = "DEFAULT") -> str:
        target = self._next("T")
        self.contexts[target] = context
        return target

    async def send(self, message: dict[str, Any]) -> None:
        self.received.append(message)
        method, params = message["method"], message.get("params", {})
        result: dict[str, Any] = {}
        if method == "Target.attachToBrowserTarget":
            result = {"sessionId": self._next("B")}
        elif method == "Target.attachToTarget":
            session = self._next("S")
            self.sessions[session] = params["targetId"]
            result = {"sessionId": session}
        elif method == "Target.createBrowserContext":
            result = {"browserContextId": self._next("CTX")}
        elif method == "Target.createTarget":
            result = {"targetId": self.page(params.get("browserContextId", "DEFAULT"))}
        elif method == "Target.getTargets":
            result = {"targetInfos": [{"targetId": t, "type": "page", "browserContextId": c}
                                      for t, c in self.contexts.items()]}
        elif method == "Browser.getVersion":
            result = {"product": f"Chrome/{CHROME_VERSION}", "userAgent": CHROME_UA}
        elif method == "Browser.getWindowForTarget":
            result = {"windowId": 1}
        reply = {"id": message["id"], "result": result}
        assert self.proxy is not None
        task = asyncio.get_running_loop().create_task(self.proxy.on_chromium_message(reply))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m["method"] == method]


class FakeDisplay:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    async def resize(self, width: int, height: int) -> None:
        self.sizes.append((width, height))


class FakeLink:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        pass


class FakeX:
    """The X injector as touch.py sees it: records what it was asked to inject."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def key(self, keysym: int, down: bool) -> None:
        self.events.append(("key", keysym, down))

    async def pointer_event(self, buttons: int, x: int, y: int) -> None:
        self.events.append(("pointer", buttons, x, y))

    async def type_text(self, text: str) -> bool:
        self.events.append(("type", len(text)))
        return True

    async def release_all(self) -> None:
        self.events.append(("release_all",))


class FakeCdp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def call(self, method: str, params: Any = None, session_id: str | None = None) -> dict[str, Any]:
        self.calls.append((method, dict(params or {}), session_id))
        return {}


class FixedDevices:
    def __init__(self, name: str, orientation: str | None = None, session: str | None = "G1") -> None:
        self.device = resolve(name, orientation)
        self._session = session

    def session_for(self, target_id: str) -> str | None:
        return self._session if target_id == "PAGE" else None


def _proxy(tmp_path: Path, records: list[tuple[str, dict[str, str]]]) -> tuple[CdpProxy, FakeChromium]:
    chromium = FakeChromium()
    proxy = CdpProxy(chromium.send,
                     paths=PolicyPaths(downloads_dir=tmp_path / "downloads", uploads_dir=tmp_path / "uploads"),
                     record=lambda kind, fields: records.append((kind, dict(fields))))
    chromium.proxy = proxy
    return proxy, chromium


# ------------------------------------------- presets set on the gate's context


@pytest.mark.parametrize(("name", "orientation"), DEVICES)
def test_each_preset_sets_metrics_user_agent_and_touch_on_the_gate_owned_context_and_resizes_the_display(
        tmp_path, name, orientation):
    width, height, scale, touch, ua = _expected(name, orientation)
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> None:
        proxy, chromium = _proxy(tmp_path, records)
        display, resized = FakeDisplay(), []
        device = DeviceController(proxy, display, record=lambda k, f: records.append((k, dict(f))),
                                  on_resize=resized.append)
        await proxy.pause()
        fresh = await proxy.open_fresh_page("https://login.example.test/")
        await device.attach_page(fresh)
        assert chromium.contexts[fresh].startswith("CTX"), "the fresh page is in the gate's own context"

        chosen = await device.select(name, orientation)
        assert (chosen.width, chosen.height) == (width, height)

        # The page is drawn at one screen pixel per CSS pixel, whatever the
        # device scale factor (presets.py, LB-R3), and the window one pixel
        # past the display (device.py).
        assert display.sizes[-1] == (width, height + BROWSER_UI_HEIGHT)
        assert resized[-1] == chosen and chosen.screen_scale == 1.0
        bounds = chromium.calls("Browser.setWindowBounds")[-1]["params"]["bounds"]
        assert (bounds["width"] - 1, bounds["height"] - 1) == display.sizes[-1]

        # Every override went over the gate's pipe on the gate's own session
        # on the fresh page -- no agent connection exists to have sent it --
        # and not on the proxy's watch session, which is detached on return
        # and would take the overrides with it.
        gate_session = device.session_for(fresh)
        assert gate_session is not None and chromium.sessions[gate_session] == fresh
        watch = await proxy.watch_page(fresh)
        assert gate_session != watch
        metrics = chromium.calls("Emulation.setDeviceMetricsOverride")[-1]
        assert metrics["sessionId"] == gate_session
        assert metrics["params"]["width"] == width
        assert metrics["params"]["height"] == height
        assert metrics["params"]["deviceScaleFactor"] == scale
        assert "scale" not in metrics["params"]
        assert metrics["params"]["mobile"] is touch
        agent = chromium.calls("Emulation.setUserAgentOverride")[-1]
        assert agent["sessionId"] == gate_session
        assert ua in agent["params"]["userAgent"]
        assert agent["params"]["userAgentMetadata"]["mobile"] is touch
        assert agent["params"]["userAgentMetadata"]["brands"][0]["version"] == CHROME_VERSION.split(".")[0]
        touch_call = chromium.calls("Emulation.setTouchEmulationEnabled")[-1]
        assert touch_call["sessionId"] == gate_session
        assert touch_call["params"]["enabled"] is touch

    asyncio.run(main())
    assert ("device_set", {"preset": name, "orientation": orientation}) in records


def test_the_preset_list_is_the_designs_and_nothing_more():
    assert set(PRESETS) == set(DESIGN)
    for name, (w, h, scale, touch, _ua) in DESIGN.items():
        preset = PRESETS[name]
        assert (preset.width, preset.height, preset.scale, preset.touch) == (w, h, scale, touch)


def test_phone_380_is_phone_390_at_380_by_800():
    """PHONE-380 (part of #2136): the same phone in every trait but its size --
    touch, the mobile flag, the scale, the iOS user agent, platform and hints."""
    p380, p390 = PRESETS["phone-380"], PRESETS["phone-390"]
    assert (p380.width, p380.height) == (380, 800)
    # MUTATION: give phone-380 the desktop user agent, or mobile=False, and this fails.
    assert dataclasses.replace(p380, name=p390.name, width=p390.width, height=p390.height) == p390


def test_xvfb_is_started_large_enough_for_every_device():
    assert MAX_DISPLAY_WIDTH >= max(d.display_width for d in all_devices())
    assert MAX_DISPLAY_HEIGHT >= max(d.display_height for d in all_devices())
    assert (MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT) == (1440, 1440 + BROWSER_UI_HEIGHT)  # desktop, both ways


def test_a_page_created_later_is_given_the_current_device(tmp_path):
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> None:
        proxy, chromium = _proxy(tmp_path, records)
        device = DeviceController(proxy, FakeDisplay(), record=lambda k, f: None)
        await device.start()
        assert chromium.calls("Target.setDiscoverTargets")[0]["params"] == {"discover": True}
        await device.select("pixel-8")
        target = chromium.page()
        device.on_event({"method": "Target.targetCreated",
                         "params": {"targetInfo": {"targetId": target, "type": "page"}}})
        for _ in range(50):
            await asyncio.sleep(0)
            if device.session_for(target) and chromium.calls("Emulation.setTouchEmulationEnabled"):
                break
        session = device.session_for(target)
        assert session is not None
        metrics = [m for m in chromium.calls("Emulation.setDeviceMetricsOverride") if m["sessionId"] == session]
        assert metrics and metrics[-1]["params"]["width"] == 412
        device.on_event({"method": "Target.targetDestroyed", "params": {"targetId": target}})
        assert device.session_for(target) is None

    asyncio.run(main())


# ------------------------------------- the device again after a clipped capture


def test_reassert_clears_and_sets_the_active_presets_metrics_on_the_gates_own_session(tmp_path):
    """QA-REASSERT (part of #1981): an agent's clipped `Page.captureScreenshot`
    lifts the gate's metrics override (QA-DEVICE, Chrome 153), and Chromium
    skips a `setDeviceMetricsOverride` identical to the one it last applied,
    so the gate clears first and then sets the active preset again."""
    async def main() -> None:
        proxy, chromium = _proxy(tmp_path, [])
        device = DeviceController(proxy, FakeDisplay(), record=lambda k, f: None)
        target = chromium.page()
        await device.attach_page(target)
        await device.select("phone-390")
        session = device.session_for(target)
        assert session is not None
        applied = chromium.calls("Emulation.setDeviceMetricsOverride")[-1]["params"]
        before = len(chromium.received)

        await device.reassert(target)

        sent = [(m["method"], m.get("params"), m.get("sessionId")) for m in chromium.received[before:]]
        # MUTATION: send only the set and Chromium skips it as identical:
        # the clear must come first, on the same session.
        # MUTATION: send them on the root session (None), or on a session
        # that is not the gate's, and the override is not the gate's.
        # MUTATION: set the desktop (or any preset but the active one) and
        # the params differ from what `select` applied.
        assert sent == [("Emulation.clearDeviceMetricsOverride", {}, session),
                        ("Emulation.setDeviceMetricsOverride", applied, session)], sent
        assert applied["width"] == 390 and applied["deviceScaleFactor"] == 3.0 and applied["mobile"] is True

    asyncio.run(main())


def test_reassert_on_a_target_the_gate_holds_no_session_on_sends_nothing(tmp_path):
    async def main() -> None:
        proxy, chromium = _proxy(tmp_path, [])
        device = DeviceController(proxy, FakeDisplay(), record=lambda k, f: None)
        held = chromium.page()
        await device.attach_page(held)
        await device.select("phone-390")
        before = len(chromium.received)
        # MUTATION: reassert whatever session `_pages` holds (or attach to
        # the target) and a page the gate never set a device on gets one.
        await device.reassert(chromium.page())
        assert chromium.received[before:] == []

    asyncio.run(main())


def test_a_device_controller_on_the_gates_proxy_is_what_a_clipped_capture_reasserts(tmp_path):
    """The wiring: a `DeviceController` built on the proxy registers its
    `reassert` with it, so __main__.py needs no line of its own."""
    async def main() -> None:
        proxy, _chromium = _proxy(tmp_path, [])
        device = DeviceController(proxy, FakeDisplay(), record=lambda k, f: None)
        # MUTATION: never register, and the proxy has nothing to call.
        assert proxy._after_clipped_capture == device.reassert

    asyncio.run(main())


# ------------------------------------------------------- unknown is refused


@pytest.mark.parametrize("name", ["galaxy-s24", "Desktop", "", None, 393, "iphone-15 ", 380, "380", "380x800"])
def test_an_unknown_preset_name_is_refused(tmp_path, name):
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> None:
        cdp, display, resized = FakeCdp(), FakeDisplay(), []
        device = DeviceController(cdp, display, record=lambda k, f: records.append((k, dict(f))),
                                  on_resize=resized.append)
        with pytest.raises(UnknownPreset):
            await device.select(name)
        assert device.device.name == "desktop"
        assert cdp.calls == [] and display.sizes == [] and resized == []

    asyncio.run(main())
    assert records == [("device_refused", {"cause": "unknown_preset"})]


def test_an_unknown_orientation_is_refused():
    with pytest.raises(UnknownPreset):
        resolve("iphone-15", "upside-down")


@pytest.mark.parametrize("args", [
    {"preset": "iphone-15", "width": 2000},
    {"preset": "iphone-15", "scale": 1},
    {"preset": "iphone-15", "userAgent": "x"},
    {"width": 393, "height": 852},
    {"width": 380, "height": 800},
    {"preset": "phone-380", "width": 380},
    {},
])
def test_a_device_request_carrying_anything_but_a_name_is_refused(args):
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> None:
        cdp, display = FakeCdp(), FakeDisplay()
        device = DeviceController(cdp, display, record=lambda k, f: records.append((k, dict(f))))
        with pytest.raises(DeviceRefused):
            await device.handle_ctl(args)
        assert cdp.calls == [] and display.sizes == []

    asyncio.run(main())
    assert records == [("device_refused", {"cause": "not_a_preset_name"})]


def test_the_ctl_device_operation_selects_by_name():
    async def main() -> dict[str, Any]:
        device = DeviceController(FakeCdp(), FakeDisplay(), record=lambda k, f: None)
        return await device.handle_ctl({"preset": "ipad-7", "orientation": "landscape"})

    assert asyncio.run(main()) == {"preset": "ipad-7", "orientation": "landscape"}


def test_the_controller_takes_a_display_and_no_scale_sink():
    """The scale sink's only user was the mask, gone with LIVE-NOMASK (#2298):
    BGD, 2026-10-25, "remove it". The display change is observed through the
    display and `on_resize`, which the tests above read."""
    params = inspect.signature(DeviceController).parameters
    positional = [n for n, p in params.items() if p.kind is not inspect.Parameter.KEYWORD_ONLY]
    assert positional == ["cdp", "display"]
    assert not hasattr(device_module, "ScaleSink")
    with pytest.raises(TypeError):
        DeviceController(FakeCdp(), FakeDisplay(), object(), record=lambda k, f: None)


# ----------------------------------------- the agent's own Emulation calls


@pytest.mark.parametrize("method", DEVICE_METHODS)
def test_the_agents_own_emulation_calls_are_refused_by_the_method_policy(tmp_path, method):
    records: list[tuple[str, dict[str, str]]] = []
    params = {"width": 393, "height": 852, "deviceScaleFactor": 3, "mobile": True,
              "userAgent": "agent-chosen", "enabled": True}

    async def main() -> FakeLink:
        proxy, chromium = _proxy(tmp_path, records)
        link = FakeLink()
        connection = await proxy.connect_agent(link)
        assert connection is not None
        await proxy.on_agent_message(connection, {"id": 7, "method": method, "params": params})
        assert chromium.calls(method) == [], "a refused method never reaches Chromium"
        return link

    link = asyncio.run(main())
    assert link.sent[-1]["id"] == 7 and "error" in link.sent[-1]
    assert "method policy" in link.sent[-1]["error"]["message"]
    assert ("cdp_method_refused", {"method": method}) in records
    assert cdppolicy.check(method, params, PolicyPaths(tmp_path, tmp_path)) is not None


def test_emulation_playwright_needs_on_attach_still_reaches_chromium(tmp_path):
    # The control for the test above: the refusal is of the device methods,
    # not of the Emulation domain.
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> FakeChromium:
        proxy, chromium = _proxy(tmp_path, records)
        connection = await proxy.connect_agent(FakeLink())
        assert connection is not None
        await proxy.on_agent_message(connection, {"id": 1, "method": "Emulation.setFocusEmulationEnabled",
                                                  "params": {"enabled": True}})
        return chromium

    assert asyncio.run(main()).calls("Emulation.setFocusEmulationEnabled")
    assert not [r for r in records if r[0] == "cdp_method_refused"]


# ------------------------------------------------------------------ touch


def _touches(cdp: FakeCdp) -> list[tuple[str, list[dict[str, Any]], str | None]]:
    return [(p["type"], p["touchPoints"], s) for m, p, s in cdp.calls if m == "Input.dispatchTouchEvent"]


def test_in_control_with_touch_on_a_press_move_release_arrives_as_touch_start_move_end():
    devices = FixedDevices("iphone-15")
    top = 0  # a mobile frame starts at the page's first row (LB-R5)

    async def main() -> tuple[FakeCdp, FakeX]:
        cdp, x11 = FakeCdp(), FakeX()
        touch = TouchInjector(x11, cdp, devices)
        touch.begin("PAGE")
        await touch.pointer_event(1, 100, top + 200)
        await touch.pointer_event(1, 100, top + 100)
        await touch.pointer_event(0, 100, top + 100)
        return cdp, x11

    cdp, x11 = asyncio.run(main())
    assert _touches(cdp) == [
        ("touchStart", [{"x": 100.0, "y": 200.0, "id": 0}], "G1"),
        ("touchMove", [{"x": 100.0, "y": 100.0, "id": 0}], "G1"),
        ("touchEnd", [], "G1"),
    ]
    assert x11.events == [], "a touch is not also a mouse click"


def test_with_touch_off_the_pointer_is_a_mouse():
    devices = FixedDevices("pixel-8")

    async def main() -> tuple[FakeCdp, FakeX]:
        cdp, x11 = FakeCdp(), FakeX()
        touch = TouchInjector(x11, cdp, devices)
        touch.set_touch(False)
        touch.begin("PAGE")
        await touch.pointer_event(1, 50, 50)
        return cdp, x11

    cdp, x11 = asyncio.run(main())
    assert _touches(cdp) == []
    # The frame starts at the page (LB-R5); the screen has the browser UI above it.
    assert x11.events == [("pointer", 1, 50, devices.device.page_top + 50)]


def test_the_desktop_preset_is_a_mouse_even_with_touch_on():
    devices = FixedDevices("desktop")

    async def main() -> tuple[FakeCdp, FakeX]:
        cdp, x11 = FakeCdp(), FakeX()
        touch = TouchInjector(x11, cdp, devices)
        touch.begin("PAGE")
        await touch.pointer_event(1, 10, 500)
        return cdp, x11

    cdp, x11 = asyncio.run(main())
    assert _touches(cdp) == [] and x11.events == [("pointer", 1, 10, 500)]


def test_a_mobile_frame_has_no_address_bar_so_a_press_at_its_top_is_a_touch():
    # LB-R5: a phone is shown as the page alone, so frame row 40 is page row 40.
    devices = FixedDevices("iphone-se")

    async def main() -> tuple[FakeCdp, FakeX]:
        cdp, x11 = FakeCdp(), FakeX()
        touch = TouchInjector(x11, cdp, devices)
        touch.begin("PAGE")
        await touch.pointer_event(1, 200, 40)
        return cdp, x11

    cdp, x11 = asyncio.run(main())
    assert _touches(cdp) == [("touchStart", [{"x": 200.0, "y": 40.0, "id": 0}], "G1")]
    assert x11.events == []


def test_a_wheel_is_a_touch_scroll_and_ctrl_wheel_is_a_pinch():
    devices = FixedDevices("iphone-15")
    top = 0  # a mobile frame starts at the page's first row (LB-R5)

    async def main() -> FakeCdp:
        cdp = FakeCdp()
        touch = TouchInjector(FakeX(), cdp, devices)
        touch.begin("PAGE")
        await touch.pointer_event(0x10, 100, top + 300)  # wheel down
        await touch.pointer_event(0, 100, top + 300)
        await touch.key(0xFFE3, True)  # Control_L: noVNC's two-finger pinch
        await touch.pointer_event(0x08, 100, top + 300)  # wheel up
        await touch.pointer_event(0, 100, top + 300)
        await touch.key(0xFFE3, False)
        return cdp

    cdp = asyncio.run(main())
    assert _touches(cdp) == [
        ("touchStart", [{"x": 100.0, "y": 300.0, "id": 0}], "G1"),
        ("touchMove", [{"x": 100.0, "y": 300.0 - SCROLL_STEP, "id": 0}], "G1"),
        ("touchEnd", [], "G1"),
    ]
    pinches = [p for m, p, _ in cdp.calls if m == "Input.synthesizePinchGesture"]
    assert len(pinches) == 1 and pinches[0]["scaleFactor"] > 1 and pinches[0]["gestureSourceType"] == "touch"


def test_control_ending_mid_touch_lifts_the_finger():
    devices = FixedDevices("iphone-15")

    async def main() -> FakeCdp:
        cdp = FakeCdp()
        touch = TouchInjector(FakeX(), cdp, devices)
        touch.begin("PAGE")
        await touch.pointer_event(1, 300, 300)
        await touch.end()
        await touch.pointer_event(0, 300, 300)
        return cdp

    assert [t[0] for t in _touches(asyncio.run(main()))] == ["touchStart", "touchCancel"]


# -------------------------------------------------------------- view mode


def test_in_view_mode_no_input_is_sent():
    devices = FixedDevices("iphone-15")
    top = 0  # a mobile frame starts at the page's first row (LB-R5)

    async def drive(touch: TouchInjector) -> None:
        await touch.pointer_event(1, 300, top + 300)
        await touch.pointer_event(1, 300, top + 600)
        await touch.pointer_event(0, 300, top + 600)
        await touch.pointer_event(0x10, 300, top + 600)
        await touch.key(0x61, True)
        await touch.key(0x61, False)
        assert await touch.type_text("typed-" + os.urandom(4).hex()) is False

    async def main() -> tuple[FakeCdp, FakeX, int]:
        cdp, x11 = FakeCdp(), FakeX()
        touch = TouchInjector(x11, cdp, devices)
        await drive(touch)  # never in control
        touch.begin("PAGE")
        await touch.end()
        x11.events.clear()
        cdp.calls.clear()
        await drive(touch)  # control has ended
        return cdp, x11, touch.dropped

    cdp, x11, dropped = asyncio.run(main())
    assert cdp.calls == [] and x11.events == []
    assert dropped == 14


# ------------------------------------------------------------ the display


class FakeXServer:
    """X11 and RandR as Xvfb answers them, recording each request.

    MODELLED ON XVFB, NOT ON WHAT device.py SENDS. Xvfb's `vfbRandRInit`
    (xserver hw/vfb/InitOutput.c) sets the screen-size range to 1x1 ..
    the size it was started at, and makes ONE CRTC driving ONE output with a
    mode named "<w>x<h>" as large as the whole screen. RandR's
    `ProcRRSetScreenSize` (randr/rrscreen.c) refuses with BadMatch any size
    smaller than an active CRTC's mode, and `ProcRRSetCrtcConfig` refuses
    with BadValue a mode larger than the screen. That pair is the gate's
    DisplayError on `desktop` (measured 2026-10-11 on task_8cf26eb6e1f94e85adf8):
    Xvfb starts at the largest preset, and the screen cannot shrink while the
    start-up CRTC still covers it.
    """

    ROOT = 0x2A1
    RANDR = 140
    CRTC = 0x40
    OUTPUT = 0x41
    CONFIG_TIME = 7
    BAD_VALUE, BAD_MATCH, BAD_NAME = 2, 8, 15

    def __init__(self, *, refuse_size: bool = False, randr: bool = True,
                 width: int = MAX_DISPLAY_WIDTH, height: int = MAX_DISPLAY_HEIGHT) -> None:
        self.requests: list[tuple[int, int, bytes]] = []
        self._refuse = refuse_size
        self._randr = randr
        self.max = (width, height)
        self.screen = (width, height)
        first = f"{width}x{height}"
        self.modes: dict[int, tuple[int, int, str]] = {0x50: (width, height, first)}
        self.output_modes = {0x50}
        self.crtc_mode = 0x50
        self._next_mode = 0x51

    @property
    def crtc_size(self) -> tuple[int, int] | None:
        if not self.crtc_mode:
            return None
        w, h, _ = self.modes[self.crtc_mode]
        return w, h

    def _error(self, code: int, seq: int) -> bytes:
        return struct.pack("<BBHI24x", 0, code, seq, 0)

    def _resources(self, seq: int) -> bytes:
        names = b"".join(n.encode() for _, _, n in self.modes.values())
        body = struct.pack("<II", self.CRTC, self.OUTPUT)
        for mode_id, (w, h, n) in self.modes.items():
            body += struct.pack("<IHHIHHHHHHHHI", mode_id, w, h, w * h * 60, w, w, w, 0, h, h, h, len(n), 0)
        body += names + b"\0" * (-len(names) % 4)
        head = struct.pack("<BxHIIIHHHH8x", 1, seq, len(body) // 4, 1, self.CONFIG_TIME,
                           1, 1, len(self.modes), len(names))
        return head + body

    def _answer(self, major: int, minor: int, rest: bytes, seq: int) -> bytes:
        if major == 98:  # QueryExtension
            return struct.pack("<BxHIBBBB20x", 1, seq, 0, int(self._randr), self.RANDR, 0, 0)
        if major == 43:  # GetInputFocus
            return struct.pack("<BBHII20x", 1, 1, seq, 0, self.ROOT)
        if major != self.RANDR:
            return self._error(1, seq)
        if minor == 0:  # QueryVersion
            return struct.pack("<BxHIII16x", 1, seq, 0, 1, 6)
        if minor == 25:  # GetScreenResourcesCurrent
            return self._resources(seq)
        if minor == 16:  # CreateMode
            _window, _id, w, h = struct.unpack_from("<IIHH", rest)
            (name_len,) = struct.unpack_from("<H", rest, 4 + 26)
            name = rest[36:36 + name_len].decode()
            if any(n == name for _, _, n in self.modes.values()):
                return self._error(self.BAD_NAME, seq)
            mode_id, self._next_mode = self._next_mode, self._next_mode + 1
            self.modes[mode_id] = (w, h, name)
            return struct.pack("<BxHII20x", 1, seq, 0, mode_id)
        if minor == 18:  # AddOutputMode
            output, mode = struct.unpack("<II", rest)
            if output != self.OUTPUT or mode not in self.modes:
                return self._error(self.BAD_MATCH, seq)
            self.output_modes.add(mode)
            return b""
        if minor == 21:  # SetCrtcConfig
            crtc, _ts, config_ts, x, y, mode, rotation = struct.unpack_from("<IIIhhIH2x", rest)
            outputs = list(struct.unpack_from(f"<{(len(rest) - 24) // 4}I", rest, 24))
            reply = struct.pack("<BBHII20x", 1, 0, seq, 0, 1)
            if config_ts != self.CONFIG_TIME:
                return struct.pack("<BBHII20x", 1, 1, seq, 0, 1)  # InvalidConfigTime
            if crtc != self.CRTC:
                return self._error(self.BAD_MATCH, seq)
            if mode == 0:
                if outputs:
                    return self._error(self.BAD_MATCH, seq)
                self.crtc_mode = 0
                return reply
            if mode not in self.output_modes or outputs != [self.OUTPUT] or rotation != 1:
                return self._error(self.BAD_MATCH, seq)
            w, h, _ = self.modes[mode]
            if x + w > self.screen[0] or y + h > self.screen[1]:
                return self._error(self.BAD_VALUE, seq)
            self.crtc_mode = mode
            return reply
        if minor == 7:  # SetScreenSize
            _root, w, h = struct.unpack_from("<IHH", rest)
            if self._refuse or not (1 <= w <= self.max[0] and 1 <= h <= self.max[1]):
                return self._error(self.BAD_VALUE, seq)
            size = self.crtc_size
            if size is not None and (size[0] > w or size[1] > h):
                return self._error(self.BAD_MATCH, seq)
            self.screen = (w, h)
            return b""
        return self._error(1, seq)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(12)
        vendor = b"fake"
        body = struct.pack("<IIIIHHBBBBBBBB4x", 0, 0, 0, 0, len(vendor), 0xFFFF, 1, 0, 0, 0, 32, 32, 8, 255) + vendor
        body += struct.pack("<I", self.ROOT) + b"\0" * 36
        writer.write(struct.pack("<BBHHH", 1, 0, 11, 0, len(body) // 4) + body)
        seq = 0
        try:
            while True:
                head = await reader.readexactly(4)
                major, minor, length = struct.unpack("<BBH", head)
                rest = await reader.readexactly(length * 4 - 4)
                seq += 1
                self.requests.append((major, minor, rest))
                writer.write(self._answer(major, minor, rest, seq))
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        writer.close()


def _with_x(server: FakeXServer, body):
    async def main() -> Any:
        with tempfile.TemporaryDirectory(prefix="xd", dir="/tmp") as where:
            path = os.path.join(where, "X0")
            listener = await asyncio.start_unix_server(server.handle, path)
            try:
                return await body(XRandrDisplay(path))
            finally:
                listener.close()
                await listener.wait_closed()

    return asyncio.run(main())


def test_the_display_is_resized_with_randr_on_the_root_window():
    server = FakeXServer()

    async def body(display: XRandrDisplay) -> None:
        await display.resize(393, 939)

    _with_x(server, body)
    sizes = [r for r in server.requests if r[0] == FakeXServer.RANDR and r[1] == 7]
    assert len(sizes) == 1
    root, width, height, _mm_w, _mm_h = struct.unpack("<IHHII", sizes[0][2])
    assert (root, width, height) == (FakeXServer.ROOT, 393, 939)
    version = [r for r in server.requests if r[0] == FakeXServer.RANDR and r[1] == 0]
    assert struct.unpack("<II", version[0][2]) == (1, 3)


def test_the_fake_x_server_refuses_what_xvfb_refuses():
    """The control: the bare `RRSetScreenSize` the gate sent before LB-DEV-A is
    refused here exactly as Xvfb refused it, so the tests below could fail."""
    server = FakeXServer()

    async def body(path_display: XRandrDisplay) -> None:
        reader, writer = await asyncio.open_unix_connection(path_display._path)
        try:
            from agent_worker.livegate import device as device_module

            root = await device_module._x_setup(reader, writer)
            writer.write(struct.pack("<BxHH2x", 98, 4, 5) + b"RANDR\0\0\0")
            await device_module._x_reply(reader, 1)
            desktop = resolve("desktop")
            writer.write(struct.pack("<BBHIHHII", FakeXServer.RANDR, 7, 5, root,
                                     desktop.display_width, desktop.display_height, 1, 1))
            writer.write(struct.pack("<BxH", 43, 1))
            await writer.drain()
            with pytest.raises(DisplayError, match="code=8"):
                await device_module._x_reply(reader, 3)
        finally:
            writer.close()

    _with_x(server, body)
    assert server.screen == (MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT)


@pytest.mark.parametrize(("name", "orientation"), DEVICES)
def test_every_preset_in_both_orientations_resizes_an_xvfb_started_at_the_largest_size(name, orientation):
    """THE DEFECT (LB-DEV-A): `desktop` failed with DisplayError on a real pod.
    Each device, from Xvfb's start-up state, ends with the screen AND its CRTC
    at the device's frame."""
    server = FakeXServer()
    device = resolve(name, orientation)

    async def body(display: XRandrDisplay) -> None:
        await display.resize(device.display_width, device.display_height)

    _with_x(server, body)
    assert server.screen == (device.display_width, device.display_height)
    assert server.crtc_size == (device.display_width, device.display_height)


def test_one_display_goes_through_every_device_and_back_reusing_its_modes():
    server = FakeXServer()
    order = [*all_devices(), *reversed(all_devices()), resolve("desktop")]

    async def body(display: XRandrDisplay) -> None:
        for device in order:
            await display.resize(device.display_width, device.display_height)
            assert server.screen == (device.display_width, device.display_height), device
            assert server.crtc_size == server.screen, device

    _with_x(server, body)
    # A size seen before is reused by name, never created twice (BadName).
    names = [n for _, _, n in server.modes.values()]
    assert len(names) == len(set(names))


def test_the_default_device_is_applied_at_start_on_an_xvfb_started_at_the_largest_size():
    records: list[tuple[str, dict[str, str]]] = []
    server = FakeXServer()

    async def body(display: XRandrDisplay) -> list[float]:
        resized: list[Any] = []
        device = DeviceController(FakeCdp(), display, record=lambda k, f: records.append((k, dict(f))),
                                  on_resize=resized.append)
        await device.start()
        return [d.screen_scale for d in resized]

    assert _with_x(server, body) == [1.0]
    desktop = resolve("desktop")
    assert server.screen == (desktop.display_width, desktop.display_height)
    assert records == []


@pytest.mark.parametrize("server", [FakeXServer(refuse_size=True), FakeXServer(randr=False)])
def test_a_display_that_will_not_resize_fails_the_change_and_keeps_the_device(server):
    records: list[tuple[str, dict[str, str]]] = []

    async def body(display: XRandrDisplay) -> str:
        cdp, resized = FakeCdp(), []
        device = DeviceController(cdp, display, record=lambda k, f: records.append((k, dict(f))),
                                  on_resize=resized.append)
        with pytest.raises(DisplayError):
            await device.select("iphone-15")
        assert resized == [] and not [c for c in cdp.calls if c[0].startswith("Emulation.")]
        return device.device.name

    assert _with_x(server, body) == "desktop"
    assert records == [("device_failed", {"preset": "iphone-15", "orientation": "portrait",
                                          "cause": "DisplayError"})]


def test_an_unreachable_display_is_a_display_error():
    async def main() -> None:
        with pytest.raises(DisplayError):
            await XRandrDisplay("/tmp/no-such-dir-for-x/X0").resize(10, 10)

    asyncio.run(main())


def test_a_frame_larger_than_the_device_is_cropped_to_it():
    frame = Frame.blank(20, 10, WHITE)
    frame.fill(0, 0, 4, 3, (0x22, 0x22, 0x22))
    out = crop(frame, 4, 3)
    assert (out.width, out.height) == (4, 3)
    assert all(out.rgb_at(x, y) == (0x22, 0x22, 0x22) for x in range(4) for y in range(3))
    assert crop(out, 4, 3) is out
