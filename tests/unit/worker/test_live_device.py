"""The live browser as a phone or a tablet, and touch from the console (L3b, LB-6).

docs/design/live-browser.md section 6.1. The acceptance test of lane L3b:

* each preset sets metrics, user agent and touch on the gate-owned context and
  resizes the display;
* an unknown preset name is refused;
* the agent's own `Emulation.*` calls are refused by the method policy;
* in control with touch on, a press/move/release arrives as
  touchStart/Move/End;
* in view mode no input is sent;
* the mask covers a password field on every preset at its device scale factor.

The expected sizes below are the design's list (section 6.1), written out
again here rather than read from presets.py, so a preset that drifts from the
design fails here. Every test drives the real modules against a scripted
Chromium (what reached it is read off what it received) and, for the display,
a scripted X server on a unix socket. No Chromium, no Xvfb, no network.
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import cdppolicy
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
from agent_worker.livegate.mask import MASK_RGB, MaskState, Outcome
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


class FakeScale:
    def __init__(self) -> None:
        self.scales: list[float] = []

    def set_scale(self, scale: float) -> None:
        self.scales.append(scale)


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
        display, mask = FakeDisplay(), FakeScale()
        device = DeviceController(proxy, display, mask, record=lambda k, f: records.append((k, dict(f))))
        await proxy.pause()
        fresh = await proxy.open_fresh_page("https://login.example.test/")
        await device.attach_page(fresh)
        assert chromium.contexts[fresh].startswith("CTX"), "the fresh page is in the gate's own context"

        chosen = await device.select(name, orientation)
        assert (chosen.width, chosen.height) == (width, height)

        page_w, page_h = math.ceil(width * scale), math.ceil(height * scale)
        assert display.sizes[-1] == (page_w, page_h + BROWSER_UI_HEIGHT)
        assert mask.scales[-1] == scale
        bounds = chromium.calls("Browser.setWindowBounds")[-1]["params"]["bounds"]
        assert (bounds["width"], bounds["height"]) == display.sizes[-1]

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


def test_xvfb_is_started_large_enough_for_every_device():
    assert MAX_DISPLAY_WIDTH >= max(d.display_width for d in all_devices())
    assert MAX_DISPLAY_HEIGHT >= max(d.display_height for d in all_devices())
    assert (MAX_DISPLAY_WIDTH, MAX_DISPLAY_HEIGHT) == (852 * 3, 852 * 3 + BROWSER_UI_HEIGHT)  # iPhone 15, both ways


def test_a_page_created_later_is_given_the_current_device(tmp_path):
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> None:
        proxy, chromium = _proxy(tmp_path, records)
        device = DeviceController(proxy, FakeDisplay(), FakeScale(), record=lambda k, f: None)
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


# ------------------------------------------------------- unknown is refused


@pytest.mark.parametrize("name", ["galaxy-s24", "Desktop", "", None, 393, "iphone-15 "])
def test_an_unknown_preset_name_is_refused(tmp_path, name):
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> None:
        cdp, display, mask = FakeCdp(), FakeDisplay(), FakeScale()
        device = DeviceController(cdp, display, mask, record=lambda k, f: records.append((k, dict(f))))
        with pytest.raises(UnknownPreset):
            await device.select(name)
        assert device.device.name == "desktop"
        assert cdp.calls == [] and display.sizes == [] and mask.scales == []

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
    {},
])
def test_a_device_request_carrying_anything_but_a_name_is_refused(args):
    records: list[tuple[str, dict[str, str]]] = []

    async def main() -> None:
        cdp, display = FakeCdp(), FakeDisplay()
        device = DeviceController(cdp, display, FakeScale(), record=lambda k, f: records.append((k, dict(f))))
        with pytest.raises(DeviceRefused):
            await device.handle_ctl(args)
        assert cdp.calls == [] and display.sizes == []

    asyncio.run(main())
    assert records == [("device_refused", {"cause": "not_a_preset_name"})]


def test_the_ctl_device_operation_selects_by_name():
    async def main() -> dict[str, Any]:
        device = DeviceController(FakeCdp(), FakeDisplay(), FakeScale(), record=lambda k, f: None)
        return await device.handle_ctl({"preset": "ipad-7", "orientation": "landscape"})

    assert asyncio.run(main()) == {"preset": "ipad-7", "orientation": "landscape"}


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
    d = devices.device
    top = d.page_top

    async def main() -> tuple[FakeCdp, FakeX]:
        cdp, x11 = FakeCdp(), FakeX()
        touch = TouchInjector(x11, cdp, devices)
        touch.begin("PAGE")
        await touch.pointer_event(1, 300, top + 600)
        await touch.pointer_event(1, 300, top + 300)
        await touch.pointer_event(0, 300, top + 300)
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
        await touch.pointer_event(1, 50, devices.device.page_top + 50)
        return cdp, x11

    cdp, x11 = asyncio.run(main())
    assert _touches(cdp) == []
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


def test_the_address_bar_is_clicked_not_touched():
    devices = FixedDevices("iphone-se")

    async def main() -> tuple[FakeCdp, FakeX]:
        cdp, x11 = FakeCdp(), FakeX()
        touch = TouchInjector(x11, cdp, devices)
        touch.begin("PAGE")
        await touch.pointer_event(1, 200, 40)
        return cdp, x11

    cdp, x11 = asyncio.run(main())
    assert _touches(cdp) == [] and x11.events == [("pointer", 1, 200, 40)]


def test_a_wheel_is_a_touch_scroll_and_ctrl_wheel_is_a_pinch():
    devices = FixedDevices("iphone-15")
    top = devices.device.page_top

    async def main() -> FakeCdp:
        cdp = FakeCdp()
        touch = TouchInjector(FakeX(), cdp, devices)
        touch.begin("PAGE")
        await touch.pointer_event(0x10, 300, top + 900)  # wheel down
        await touch.pointer_event(0, 300, top + 900)
        await touch.key(0xFFE3, True)  # Control_L: noVNC's two-finger pinch
        await touch.pointer_event(0x08, 300, top + 900)  # wheel up
        await touch.pointer_event(0, 300, top + 900)
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
        await touch.pointer_event(1, 300, devices.device.page_top + 300)
        await touch.end()
        await touch.pointer_event(0, 300, devices.device.page_top + 300)
        return cdp

    assert [t[0] for t in _touches(asyncio.run(main()))] == ["touchStart", "touchCancel"]


# -------------------------------------------------------------- view mode


def test_in_view_mode_no_input_is_sent():
    devices = FixedDevices("iphone-15")
    top = devices.device.page_top

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


# ------------------------------------------------------------ the mask


@pytest.mark.parametrize(("name", "orientation"), DEVICES)
def test_the_mask_covers_a_password_field_on_every_preset_at_its_device_scale_factor(name, orientation):
    width, height, scale, _touch, _ua = _expected(name, orientation)
    state = MaskState()
    state.session_up()
    state.frame_attached("TOP", None)
    state.navigated("TOP", "https://login.example.test/")

    async def main() -> Any:
        device = DeviceController(FakeCdp(), FakeDisplay(), state, record=lambda k, f: None)
        return await device.select(name, orientation)

    device = asyncio.run(main())
    # The observer reports in CSS pixels, after the device change.
    field = (20, 40, 200, 30)
    state.on_message("TOP", (
        '{"v": 1, "k": "report", "doc": "D1", "e": 1, "vp": [%d, %d, 0, 0, 1], '
        '"f": [{"id": 1, "type": "password", "ac": "", "ever": false, "r": [%d, %d, %d, %d]}], '
        '"t": [], "o": [], "tr": false}' % (width, height, *field)))
    frame = Frame.blank(device.display_width, device.display_height, WHITE)
    assert state.apply(frame) is Outcome.MASKED

    top = device.display_height - math.ceil(height * scale)
    x0, y0 = math.ceil(field[0] * scale), top + math.ceil(field[1] * scale)
    x1, y1 = math.floor((field[0] + field[2]) * scale), top + math.floor((field[1] + field[3]) * scale)
    for y in range(y0, y1, 3):
        for x in range(x0, x1, 5):
            assert frame.rgb_at(x, y) == MASK_RGB, (name, orientation, x, y)
    assert frame.rgb_at(x1 - 1, y1 - 1) == MASK_RGB
    # The box is the field at this scale, not the whole page: well past it is untouched.
    assert frame.rgb_at(min(x1 + 40, device.display_width - 1), min(y1 + 40, device.display_height - 1)) == WHITE


# ------------------------------------------------------------ the display


class FakeXServer:
    """Enough of X11 and RandR to answer `XRandrDisplay`, recording each request."""

    ROOT = 0x2A1
    RANDR = 140

    def __init__(self, *, refuse_size: bool = False, randr: bool = True) -> None:
        self.requests: list[tuple[int, int, bytes]] = []
        self._refuse = refuse_size
        self._randr = randr

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
                if major == 98:  # QueryExtension
                    writer.write(struct.pack("<BxHIBBBB20x", 1, seq, 0, int(self._randr), self.RANDR, 0, 0))
                elif major == self.RANDR and minor == 0:
                    writer.write(struct.pack("<BxHIII16x", 1, seq, 0, 1, 6))
                elif major == self.RANDR and minor == 7:
                    if self._refuse:
                        writer.write(struct.pack("<BBHI24x", 0, 2, seq, 0))  # BadValue
                elif major == 43:  # GetInputFocus
                    writer.write(struct.pack("<BBHII20x", 1, 1, seq, 0, self.ROOT))
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
        await display.resize(1179, 2644)

    _with_x(server, body)
    sizes = [r for r in server.requests if r[0] == FakeXServer.RANDR and r[1] == 7]
    assert len(sizes) == 1
    root, width, height, _mm_w, _mm_h = struct.unpack("<IHHII", sizes[0][2])
    assert (root, width, height) == (FakeXServer.ROOT, 1179, 2644)
    version = [r for r in server.requests if r[0] == FakeXServer.RANDR and r[1] == 0]
    assert struct.unpack("<II", version[0][2]) == (1, 2)


@pytest.mark.parametrize("server", [FakeXServer(refuse_size=True), FakeXServer(randr=False)])
def test_a_display_that_will_not_resize_fails_the_change_and_keeps_the_device(server):
    records: list[tuple[str, dict[str, str]]] = []

    async def body(display: XRandrDisplay) -> str:
        cdp, mask = FakeCdp(), FakeScale()
        device = DeviceController(cdp, display, mask, record=lambda k, f: records.append((k, dict(f))))
        with pytest.raises(DisplayError):
            await device.select("iphone-15")
        assert mask.scales == [] and not [c for c in cdp.calls if c[0].startswith("Emulation.")]
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
    frame.fill(0, 0, 4, 3, MASK_RGB)
    out = crop(frame, 4, 3)
    assert (out.width, out.height) == (4, 3)
    assert all(out.rgb_at(x, y) == MASK_RGB for x in range(4) for y in range(3))
    assert crop(out, 4, 3) is out
