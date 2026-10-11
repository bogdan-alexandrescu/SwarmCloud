"""The gate applies the device, not the agent (live browser L3b, LB-6).

docs/design/live-browser.md section 6.1. The gate owns Chromium's pipe
(section 3.2), so the device is set from the gate's OWN CDP sessions, never
the agent's: for every page, the gate attaches a session of its own and sends

* `Emulation.setDeviceMetricsOverride` -- the preset's CSS viewport, device
  scale factor, `mobile` and screen orientation;
* `Emulation.setUserAgentOverride` -- the preset's user agent, platform and
  client hints;
* `Emulation.setTouchEmulationEnabled` -- touch on for a phone or tablet.

and it resizes the virtual display to the preset's frame (`Device`), so the
frames carry no letterbox. The agent's own calls to those methods are refused
by the method policy (`cdppolicy.DENIED`, section 5.4.4); an agent picks a
preset through the gate (`swarm-handoff device <preset>`, the `device`
operation on ctl.sock), by name.

WHY A SESSION PER PAGE, KEPT. An emulation override in CDP belongs to the
session that set it and is lifted when that session detaches. The proxy's own
watch sessions are detached on return (`CdpProxy._hand_over`), so the device
cannot ride on them: this module holds one session per page for as long as
the page lives. Those sessions are the gate's, so the proxy never routes them
to an agent (`CdpProxy.on_chromium_message` drops events of a session no
agent owns).

THE ORDER OF A CHANGE: the name is checked first, so an unknown name changes
nothing; then the display is resized (the step most likely to fail: a size
past what Xvfb was started with); then the window is fitted to it, the device
set on every page, the mask told the new scale (which makes every report stale
-- the mask fails closed until the page reports at the new size), and the
injector told the new bounds. A failure part-way is recorded and raised; the
next change starts again from the name.

NEW PAGES. `Target.setDiscoverTargets` on the gate's root session reports
every page created; the gate's pipe reader hands those events to `on_event`,
which schedules the attach on its own task (the reader must never wait on a
reply, cdpproxy.py). Between a page's creation and the override, the page has
the window's own metrics: its viewport is wider than the device's page area,
and the mask treats a viewport larger than the frame as unplaceable and blanks
the frame (`Outcome.UNAVAILABLE`). It fails closed; it does not leak.

THE DISPLAY. `XRandrDisplay` speaks RandR's `RRSetScreenSize` to Xvfb over
its unix socket, in the sidecar's own /tmp, as inject.py speaks XTEST: no
`xrandr` binary, no Xlib. RandR shrinks a screen below the size Xvfb was
started with but never grows it, so the sidecar starts Xvfb at
`presets.MAX_DISPLAY_WIDTH` x `MAX_DISPLAY_HEIGHT`. Whether Xvfb rewrites its
`-fbdir` file's header on a resize is not something this lane could measure,
so `crop` cuts a captured frame to the device's display either way; on a file
that already shrank it returns the frame unchanged.

NOTHING HERE IS LOGGED BUT NAMES. A device change records the preset name and
orientation; a page's URL, a session id or a coordinate is never a log
argument.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .capture import BYTES_PER_PIXEL, Frame
from .presets import DEFAULT_PRESET, MAX_TOUCH_POINTS, Device, UnknownPreset, resolve

log = logging.getLogger(__name__)

Record = Callable[[str, Mapping[str, str]], None]

#: Where Xvfb `:0` listens, inside the sidecar's own /tmp (section 3.1).
DEFAULT_DISPLAY_SOCKET = "/tmp/.X11-unix/X0"

#: The arguments a `device` request may carry. Anything else -- a width, a
#: scale, a user agent -- is refused: a caller names a preset, never a size.
_CTL_ARGS = frozenset({"preset", "orientation"})

# X11 core and RandR requests used here.
_X_QUERY_EXTENSION = 98
_X_GET_INPUT_FOCUS = 43
_RR_QUERY_VERSION = 0
_RR_SET_SCREEN_SIZE = 7

#: Millimetres are what RandR stores; at 96 dpi, which is what Xvfb assumes.
_MM_PER_PIXEL = 25.4 / 96


class GateCdp(Protocol):
    """The gate's own CDP, on Chromium's pipe. `CdpProxy.call` is the real one."""

    async def call(self, method: str, params: Mapping[str, Any] | None = None,
                   session_id: str | None = None) -> dict[str, Any]: ...


class Display(Protocol):
    """The virtual display Chromium draws on."""

    async def resize(self, width: int, height: int) -> None: ...


class ScaleSink(Protocol):
    """`MaskState`: screen pixels per CSS pixel."""

    def set_scale(self, scale: float) -> None: ...


class DisplayError(Exception):
    """The display could not be resized."""


class DeviceRefused(ValueError):
    """A `device` request that is not a preset by name. Nothing was changed."""


# ---------------------------------------------------------------- the display


class XRandrDisplay:
    """Xvfb's screen size, set with RandR 1.2 `RRSetScreenSize` over the X socket."""

    def __init__(self, path: str = DEFAULT_DISPLAY_SOCKET, *, timeout: float = 5.0) -> None:
        self._path = path
        self._timeout = timeout

    async def resize(self, width: int, height: int) -> None:
        try:
            await asyncio.wait_for(self._resize(width, height), self._timeout)
        except asyncio.TimeoutError:
            raise DisplayError("display did not answer") from None

    async def _resize(self, width: int, height: int) -> None:
        try:
            reader, writer = await asyncio.open_unix_connection(self._path)
        except OSError as exc:
            raise DisplayError(f"display unreachable: {exc.strerror or type(exc).__name__}") from None
        try:
            root = await _x_setup(reader, writer)
            name = b"RANDR"
            writer.write(struct.pack("<BxHH2x", _X_QUERY_EXTENSION, 2 + len(_pad(name)) // 4, len(name))
                         + _pad(name))
            reply = await _x_reply(reader, 1)
            if not reply[8]:
                raise DisplayError("display has no RandR extension")
            randr = reply[9]
            # RandR answers 1.2 requests only to a client that asked for 1.2.
            writer.write(struct.pack("<BBHII", randr, _RR_QUERY_VERSION, 3, 1, 2))
            await _x_reply(reader, 2)
            mm_w = max(1, round(width * _MM_PER_PIXEL))
            mm_h = max(1, round(height * _MM_PER_PIXEL))
            writer.write(struct.pack("<BBHIHHII", randr, _RR_SET_SCREEN_SIZE, 5, root, width, height, mm_w, mm_h))
            # RRSetScreenSize has no reply: a GetInputFocus after it does, and
            # an error for request 3 arrives before that reply.
            writer.write(struct.pack("<BxH", _X_GET_INPUT_FOCUS, 1))
            await writer.drain()
            await _x_reply(reader, 4)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
        log.info("live device: display resized")


def _pad(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


async def _x_setup(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    """Connect (little-endian, 11.0, no authorisation: section 3.1) and return the root window."""
    writer.write(struct.pack("<BxHHHH2x", ord("l"), 11, 0, 0, 0))
    await writer.drain()
    try:
        status, _, _major, _minor, length = struct.unpack("<BBHHH", await reader.readexactly(8))
        body = await reader.readexactly(length * 4)
    except asyncio.IncompleteReadError:
        raise DisplayError("display closed the connection during setup") from None
    if status != 1:
        raise DisplayError("display refused the connection")
    vendor_len, _max_req, _screens, formats = struct.unpack_from("<HHBB", body, 16)
    (root,) = struct.unpack_from("<I", body, 32 + vendor_len + (-vendor_len % 4) + 8 * formats)
    return int(root)


async def _x_reply(reader: asyncio.StreamReader, seq: int) -> bytes:
    """The reply to request `seq`; any X error before it is `DisplayError`."""
    while True:
        try:
            head = await reader.readexactly(32)
        except asyncio.IncompleteReadError:
            raise DisplayError("display closed the connection") from None
        kind = head[0]
        if kind == 0:
            raise DisplayError(f"display answered with X error code={head[1]}")
        if kind != 1:
            continue  # an event (RRScreenChangeNotify and the like)
        (got,) = struct.unpack_from("<H", head, 2)
        (extra,) = struct.unpack_from("<I", head, 4)
        body = await reader.readexactly(extra * 4) if extra else b""
        if got == seq:
            return head + body


def crop(frame: Frame, width: int, height: int) -> Frame:
    """`frame`'s top-left `width` x `height`, copied; `frame` itself when it is no larger."""
    if frame.width <= width and frame.height <= height:
        return frame
    width, height = min(width, frame.width), min(height, frame.height)
    row = width * BYTES_PER_PIXEL
    pixels = bytearray(row * height)
    for y in range(height):
        at = y * frame.stride
        pixels[y * row:(y + 1) * row] = frame.pixels[at:at + row]
    return Frame(width, height, row, pixels, frame.red_mask, frame.green_mask, frame.blue_mask,
                 frame.little_endian)


# ------------------------------------------------------------ the controller


class DeviceController:
    """Which device the browser is, applied from the gate's own sessions."""

    def __init__(self, cdp: GateCdp, display: Display, mask: ScaleSink, *, record: Record,
                 on_resize: Callable[[Device], None] | None = None) -> None:
        self._cdp = cdp
        self._display = display
        self._mask = mask
        self._record = record
        self._on_resize = on_resize
        self._device = resolve(DEFAULT_PRESET)
        self._pages: dict[str, str] = {}  # targetId -> the gate's session on it
        self._browser: tuple[str, str] | None = None  # (user agent, version)
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def device(self) -> Device:
        return self._device

    def session_for(self, target_id: str) -> str | None:
        """The gate's own session on `target_id`, for touch input (touch.py)."""
        return self._pages.get(target_id)

    async def start(self) -> None:
        """Discover pages, and apply the default device to those already open."""
        await self._cdp.call("Target.setDiscoverTargets", {"discover": True})
        targets = (await self._cdp.call("Target.getTargets", {})).get("targetInfos") or []
        async with self._lock:
            for info in targets:
                if info.get("type") == "page" and isinstance(info.get("targetId"), str):
                    await self._attach(info["targetId"])
            await self._apply_display(self._device)

    async def handle_ctl(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The ctl.sock `device` operation (`swarm-handoff device <preset>`)."""
        if not isinstance(args, Mapping) or not set(args) <= _CTL_ARGS or "preset" not in args:
            self._record("device_refused", {"cause": "not_a_preset_name"})
            raise DeviceRefused("a device is chosen by preset name only")
        device = await self.select(args["preset"], args.get("orientation"))
        return {"preset": device.name, "orientation": device.orientation}

    async def select(self, name: Any, orientation: Any = None) -> Device:
        """Make the browser `name` in `orientation`. `UnknownPreset` changes nothing."""
        try:
            device = resolve(name, orientation)
        except UnknownPreset:
            self._record("device_refused", {"cause": "unknown_preset"})
            raise
        async with self._lock:
            try:
                await self._apply_display(device)
                for target, session in list(self._pages.items()):
                    await self._apply_page(device, target, session)
            except Exception as exc:
                self._record("device_failed", {"preset": device.name, "orientation": device.orientation,
                                               "cause": type(exc).__name__})
                raise
            self._device = device
        self._record("device_set", {"preset": device.name, "orientation": device.orientation})
        log.info("live device: preset=%s orientation=%s", device.name, device.orientation)
        return device

    async def attach_page(self, target_id: str) -> None:
        """Apply the current device to a page (the fresh page, or one just discovered)."""
        async with self._lock:
            await self._attach(target_id)

    def on_event(self, message: Mapping[str, Any]) -> None:
        """A message from Chromium's pipe. Never waits: the attach runs on its own task."""
        method = message.get("method")
        raw = message.get("params")
        params: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        if method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            target = info.get("targetId")
            if info.get("type") == "page" and isinstance(target, str) and target not in self._pages:
                task = asyncio.get_running_loop().create_task(self._attach_quietly(target))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        elif method == "Target.targetDestroyed":
            target = params.get("targetId")
            if isinstance(target, str):
                self._pages.pop(target, None)
        elif method == "Target.detachedFromTarget":
            session = params.get("sessionId")
            for target, held in list(self._pages.items()):
                if held == session:
                    del self._pages[target]

    async def _attach_quietly(self, target_id: str) -> None:
        try:
            await self.attach_page(target_id)
        except Exception as exc:  # a page that closed before the attach answered
            log.info("live device: page attach failed (%s)", type(exc).__name__)

    async def _attach(self, target_id: str) -> None:
        session = self._pages.get(target_id)
        if session is None:
            result = await self._cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session = str(result["sessionId"])
            self._pages[target_id] = session
        await self._apply_page(self._device, target_id, session)

    async def _browser_version(self) -> tuple[str, str]:
        if self._browser is None:
            version = await self._cdp.call("Browser.getVersion", {})
            product = str(version.get("product", ""))
            number = product.split("/", 1)[1] if "/" in product else "0.0.0.0"
            self._browser = (str(version.get("userAgent", "")), number)
        return self._browser

    async def _apply_display(self, device: Device) -> None:
        await self._display.resize(device.display_width, device.display_height)
        self._mask.set_scale(device.scale)
        if self._on_resize is not None:
            self._on_resize(device)
        for target in list(self._pages):
            window = await self._cdp.call("Browser.getWindowForTarget", {"targetId": target})
            await self._cdp.call("Browser.setWindowBounds", {
                "windowId": window["windowId"],
                "bounds": {"left": 0, "top": 0, "width": device.display_width,
                           "height": device.display_height, "windowState": "normal"},
            })
            break  # one window (section 3.1)

    async def _apply_page(self, device: Device, target_id: str, session: str) -> None:
        user_agent, version = await self._browser_version()
        await self._cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": device.width,
            "height": device.height,
            "deviceScaleFactor": device.scale,
            "mobile": device.mobile,
            # The view is drawn at `scale` screen pixels per CSS pixel, so the
            # page fills the display and the mask's mapping holds.
            "scale": device.scale,
            "screenWidth": device.width,
            "screenHeight": device.height,
            "screenOrientation": device.screen_orientation(),
        }, session)
        await self._cdp.call("Emulation.setUserAgentOverride", {
            "userAgent": device.user_agent(user_agent, version),
            "platform": device.preset.platform,
            "userAgentMetadata": device.user_agent_metadata(version),
        }, session)
        touch: dict[str, Any] = {"enabled": device.touch}
        if device.touch:
            touch["maxTouchPoints"] = MAX_TOUCH_POINTS
        await self._cdp.call("Emulation.setTouchEmulationEnabled", touch, session)
