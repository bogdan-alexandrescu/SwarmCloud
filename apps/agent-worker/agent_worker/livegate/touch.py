"""The person in control works a phone by touch (live browser L3b, LB-6).

docs/design/live-browser.md section 6.1. When the device is a phone or a
tablet and the person in control has touch on, the controller's pointer
becomes touch, sent from the gate's own session on the controlled page
(`DeviceController.session_for`) with `Input.dispatchTouchEvent`:

* a press, a move with the button held and a release become `touchStart`,
  `touchMove` and `touchEnd`;
* a two-finger gesture becomes a pinch. noVNC turns a two-finger pinch on the
  console into Ctrl held with the wheel, so a wheel click with Ctrl down is
  `Input.synthesizePinchGesture` with a touch source;
* a wheel without Ctrl is a touch scroll: a one-finger drag of `SCROLL_STEP`
  CSS pixels against the wheel's direction, as a finger scrolls a page.

Everything else -- keys, a paste, a pointer on the browser's own UI above the
page (the address bar is not a touch surface; a mobile frame no longer shows
it, LB-R5, but a drag held past the frame's edge still clamps as below), the
pointer on a desktop preset or with touch off -- goes to the X injector as
before.

`TouchInjector` is an injector in the shape `livegate.rfb` injects through
(`inject.Injector`: `key`, `pointer_event`, `type_text`, `release_all`), so
the RFB session's rule stays the one rule: input only from the controller,
only in control mode. This module adds its own: it sends NOTHING, touch or
pointer, until the gate has said which page is under control (`begin`), and
nothing after `end`. In view mode no input is sent at all, even if a caller
upstream got the controller wrong.

COORDINATES. The display is the page at `Device.screen_scale` (one screen
pixel per CSS pixel, NOT the device scale factor: presets.py) plus the
browser's UI band above it, and the page is anchored at the bottom-left
(presets.py `page_top`). A screen point in the page area is
`((x - 0) / screen_scale, (y - page_top) / screen_scale)` CSS pixels, which is what
`dispatchTouchEvent` takes. A drag that leaves the page area is clamped to its
edge rather than turned into a mouse event half-way through.

THE FRAME IS NOT ALWAYS THE SCREEN. On a mobile preset the viewer is sent the
page alone, from `Device.frame_top` down (LB-R5, presets.py), so the pointer
arrives in that frame's coordinates. `pointer_event` moves it down by
`frame_top` first, once, and everything after it -- the page test, the CSS
mapping, the X injector -- works in screen pixels. On the desktop
`frame_top` is 0 and the pointer is where the frame says.

NOTHING HERE IS LOGGED BUT COUNTS (section 5.3.3): no coordinate, keysym or
gesture position is a log argument.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .device import GateCdp
from .presets import Device

log = logging.getLogger(__name__)

#: How far one wheel click scrolls, as a finger drag, in CSS pixels.
SCROLL_STEP = 100

#: How much one wheel click with Ctrl zooms a pinch in (and its inverse out).
PINCH_STEP = 1.25

_BUTTON_1 = 0x01
# RFB's wheel: buttons 4 and 5 vertical, 6 and 7 horizontal (bits 3 to 6).
_WHEEL_UP, _WHEEL_DOWN, _WHEEL_LEFT, _WHEEL_RIGHT = 0x08, 0x10, 0x20, 0x40
_WHEEL = _WHEEL_UP | _WHEEL_DOWN | _WHEEL_LEFT | _WHEEL_RIGHT
_CONTROL_KEYSYMS = frozenset({0xFFE3, 0xFFE4})  # Control_L, Control_R


class Injector(Protocol):
    """The X injector (`inject.XInjector`), in the shape `livegate.rfb` uses."""

    async def key(self, keysym: int, down: bool) -> None: ...

    async def pointer_event(self, buttons: int, x: int, y: int) -> None: ...

    async def type_text(self, text: str) -> bool: ...

    async def release_all(self) -> None: ...


class Devices(Protocol):
    """`DeviceController`: the current device and the gate's session per page."""

    @property
    def device(self) -> Device: ...

    def session_for(self, target_id: str) -> str | None: ...


class TouchInjector:
    """The controller's input: touch on a touch device, the X injector otherwise."""

    def __init__(self, inner: Injector, cdp: GateCdp, devices: Devices) -> None:
        self._inner = inner
        self._cdp = cdp
        self._devices = devices
        self._target: str | None = None
        self._touch_on = True
        self._touching: tuple[str, float, float] | None = None  # (session, x, y) of the finger down
        self._buttons = 0
        self._control_down: set[int] = set()
        self.touch_events = 0
        self.gestures = 0
        self.dropped = 0

    # -- what the gate tells it --------------------------------------------

    @property
    def touch_on(self) -> bool:
        return self._touch_on

    def set_touch(self, on: bool) -> None:
        """The person's touch toggle in the console (section 6)."""
        self._touch_on = bool(on)

    def begin(self, target_id: str) -> None:
        """Control was granted on `target_id` (`CdpProxy.take_control`)."""
        self._target = target_id
        self._buttons = 0

    async def end(self) -> None:
        """Control ended: lift a finger still down, release what is held, send nothing more."""
        try:
            await self.release_all()
        finally:
            self._target = None
        log.info("live touch: control ended touch_events=%d gestures=%d dropped=%d",
                 self.touch_events, self.gestures, self.dropped)

    # -- the injector --------------------------------------------------------

    async def key(self, keysym: int, down: bool) -> None:
        if self._target is None:
            self.dropped += 1
            return
        if keysym in _CONTROL_KEYSYMS:
            if down:
                self._control_down.add(keysym)
            else:
                self._control_down.discard(keysym)
        await self._inner.key(keysym, down)

    async def type_text(self, text: str) -> bool:
        if self._target is None:
            self.dropped += 1
            return False
        return await self._inner.type_text(text)

    async def release_all(self) -> None:
        await self._lift("touchCancel")
        self._control_down.clear()
        self._buttons = 0
        await self._inner.release_all()

    def resize(self, width: int, height: int) -> None:
        """The display changed size: the X injector clamps to the new one."""
        resize = getattr(self._inner, "resize", None)
        if resize is not None:
            resize(width, height)

    async def pointer_event(self, buttons: int, x: int, y: int) -> None:
        if self._target is None:
            self.dropped += 1
            return
        device = self._devices.device
        y += device.frame_top  # the frame's coordinates to the screen's (module docstring)
        session = self._devices.session_for(self._target)
        if not (self._touch_on and device.touch and session is not None):
            await self._lift("touchCancel")
            self._buttons = buttons
            await self._inner.pointer_event(buttons, x, y)
            return
        pressed = buttons & ~self._buttons
        self._buttons = buttons
        in_page = 0 <= x < device.page_width and device.page_top <= y < device.display_height
        point = self._css(device, x, y)

        if self._touching is None and not in_page:
            # The browser's own UI above the page: a mouse, not a finger.
            await self._inner.pointer_event(buttons, x, y)
            return
        if buttons & _BUTTON_1:
            if self._touching is None:
                await self._touch(session, "touchStart", point)
            elif (self._touching[1], self._touching[2]) != point:
                await self._touch(session, "touchMove", point)
        elif self._touching is not None:
            await self._lift("touchEnd")
        if pressed & _WHEEL and self._touching is None and in_page:
            await self._wheel(session, device, pressed & _WHEEL, point)

    # -- touch -------------------------------------------------------------

    @staticmethod
    def _css(device: Device, x: int, y: int) -> tuple[float, float]:
        cx = min(max(x, 0), device.page_width - 1) / device.screen_scale
        cy = min(max(y - device.page_top, 0), device.page_height - 1) / device.screen_scale
        return (cx, cy)

    async def _send(self, session: str, kind: str, points: list[dict[str, Any]]) -> None:
        await self._cdp.call("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points}, session)
        self.touch_events += 1

    async def _touch(self, session: str, kind: str, point: tuple[float, float]) -> None:
        await self._send(session, kind, [{"x": point[0], "y": point[1], "id": 0}])
        self._touching = (session, point[0], point[1])

    async def _lift(self, kind: str) -> None:
        if self._touching is None:
            return
        session = self._touching[0]
        self._touching = None
        try:
            await self._send(session, kind, [])
        except Exception as exc:  # noqa: BLE001 -- the finger's session went with its page: nothing to lift
            log.info("live touch: lift not sent (%s)", type(exc).__name__)

    async def _wheel(self, session: str, device: Device, wheel: int, point: tuple[float, float]) -> None:
        if self._control_down:
            scale = PINCH_STEP if wheel & _WHEEL_UP else 1 / PINCH_STEP
            await self._cdp.call("Input.synthesizePinchGesture", {
                "x": point[0], "y": point[1], "scaleFactor": scale, "gestureSourceType": "touch",
            }, session)
            self.gestures += 1
            return
        # The finger moves against the wheel: wheel down drags the page up.
        dx = (SCROLL_STEP if wheel & _WHEEL_LEFT else 0) - (SCROLL_STEP if wheel & _WHEEL_RIGHT else 0)
        dy = (SCROLL_STEP if wheel & _WHEEL_UP else 0) - (SCROLL_STEP if wheel & _WHEEL_DOWN else 0)
        end = (min(max(point[0] + dx, 0), device.width - 1), min(max(point[1] + dy, 0), device.height - 1))
        await self._send(session, "touchStart", [{"x": point[0], "y": point[1], "id": 0}])
        await self._send(session, "touchMove", [{"x": end[0], "y": end[1], "id": 0}])
        await self._send(session, "touchEnd", [])
        self.gestures += 1
