"""The gate applies the device, not the agent (live browser L3b, LB-6).

docs/design/live-browser.md section 6.1. The gate owns Chromium's pipe
(section 3.2), so the device is set from the gate's OWN CDP sessions, never
the agent's: for every page, the gate attaches a session of its own and sends

* `Emulation.setDeviceMetricsOverride` -- the preset's CSS viewport, device
  scale factor, `mobile`, screen size and orientation, and NO `scale`;
* `Emulation.setUserAgentOverride` -- the preset's user agent, platform and
  client hints;
* `Emulation.setTouchEmulationEnabled` -- touch on for a phone or tablet.

and it resizes the virtual display to the preset's frame (`Device`) and fits
every Chromium window to it, so the frames carry no letterbox. The agent's own calls to those methods are refused
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

WHY NO `scale`, AND THE DISPLAY AT CSS SIZE. Headed Chromium draws an
emulated device at one screen pixel per CSS pixel and clips the view there;
the override's `scale` magnifies the content inside that clip, not the clip.
Sent with `scale` = the device scale factor on a display `scale` times the
CSS size, iPhone 15 landscape became an 852 x 393 corner of the page at 3x in
a 2556 x 1267 frame (re-QA 2026-10-12, task_e8cb274977d545a48710; reproduced
against the image's Chromium the same day). presets.py records the
measurement; the page is told the device scale factor, the frame shows it at
`Device.screen_scale`.

THE WINDOW IS ONE PIXEL LARGER THAN THE SCREEN. Measured on the same
Chromium: `Browser.setWindowBounds` to exactly the screen's size leaves the
window one pixel short each way (852 x 481 asked, 851 x 480 got), which is a
one-pixel black letterbox and a page clipped by a column; any larger size is
kept as asked. So every window is fitted at the display plus `WINDOW_OVERHANG`,
and the overhang is off screen. A window narrower than Chromium's minimum
(500 px: the portrait phones) is wider than the display the same way: the
page is at the window's left edge, so it is still the whole frame.

NEW PAGES. `Target.setDiscoverTargets` on the gate's root session reports
every page created; the gate's pipe reader hands those events to `on_event`,
which schedules the attach on its own task (the reader must never wait on a
reply, cdpproxy.py). A page in a window the gate has not fitted yet (a popup
opened as a window) has that window fitted too. The override belongs to the
page's target, not its document, so it holds across the page's navigations,
cross-site ones included (a new renderer under the same target). Between a page's creation and the override, the page has
the window's own metrics: its viewport is wider than the device's page area,
and the mask treats a viewport larger than the frame as unplaceable and blanks
the frame (`Outcome.UNAVAILABLE`). It fails closed; it does not leak.

THE DISPLAY. `XRandrDisplay` speaks RandR to Xvfb over its unix socket
(the CRTC switched to a mode of the frame's size around `RRSetScreenSize`:
its docstring says why a bare screen-size change is refused), in the sidecar's own /tmp, as inject.py speaks XTEST: no
`xrandr` binary, no Xlib. RandR shrinks a screen below the size Xvfb was
started with but never grows it, so the sidecar starts Xvfb at
`presets.MAX_DISPLAY_WIDTH` x `MAX_DISPLAY_HEIGHT`. Whether Xvfb rewrites its
`-fbdir` file's header on a resize is not something this lane could measure,
so `crop` cuts a captured frame to the device's display either way; on a file
that already shrank it returns the frame unchanged. `captured` is that cut as
the viewer is sent it: on a mobile preset it also drops the browser's UI band
above the page (LB-R5, presets.py), so a phone shows the page alone.

THE THEME (QA-KIT, owner decision 2026-10-19, part of #1649). The agent
asks for a colour scheme by name (`swarm-handoff theme dark|light|system`,
the `theme` operation on ctl.sock), and the gate sends
`Emulation.setEmulatedMedia` with the one feature `prefers-color-scheme` on
the SAME per-page sessions that carry the device. So it holds the way the
device holds: the override belongs to the page's target, not its document,
and survives the page's navigations; a page created later, or attached again
after its session went (a renderer swap, DEVICE-RECOVER), is given it by
`_apply_page`; a device switch re-applies it with the rest. `system` sends the
feature with an empty value, which lifts the override. While the theme is
`system` no media call is made at all, so a browser nobody asked to theme
gets exactly the calls it got before. The agent may call
`Emulation.setEmulatedMedia` on its own sessions too (cdppolicy.py allows
it), which is one more reason the instructions name the command rather than
the method.

THE THEME IS PUT BACK, because a media override is the page's, not the
session's (QAFIX2-QATOOL, WG-ENV2; part of #1680). Visual QA run 2
(wf_b6e2441b419f423a84eb, 2026-10-20) ran `theme dark` and read
`matchMedia('(prefers-color-scheme: dark)')` false in a fresh tab. Whichever
session writes last is what the page shows, and the gate wrote once. The
agent's Playwright sends `prefers-color-scheme: light` (its default
`colorScheme`) on every page it attaches to, and Blink lifts a feature a
session set when that session detaches, the gate's value with it. So while a
theme other than `system` is set, the gate re-sends it on a page

* `THEME_SETTLE_SECONDS` after another session attaches to it: Playwright's
  own write comes after the attach, and Chromium reports nothing at its end;
* when another session detaches from it, which may have lifted the value;
* when its top frame navigates (`Page.frameNavigated`, which is why the
  gate's page session enables `Page` once a theme is set). A new renderer
  restores every session's value in an order nobody chose.

The value is the one the agent asked the gate for by name. An agent that
also calls `page.emulate_media` with the same scheme, as swarm_qa.py does, is
not contradicted. At `system` none of this sends anything.

THE DEVICE AGAIN AFTER A CLIPPED CAPTURE (QA-REASSERT, decided 2026-10-22;
part of #1981). MEASURED by lane QA-DEVICE (task_b3f3786bde2144389073,
Chrome 153.0.8010.12, Playwright 1.63.0): the device IS in force before an
agent's first screenshot, but every Playwright screenshot sends a clipped
`Page.captureScreenshot` from the agent's own session, and that lifts this
module's `Emulation.setDeviceMetricsOverride` for the rest of the document:
every later shot was dpr 1 with a desktop scrollbar. Sending the same
parameters again does nothing (Chromium skips an override identical to the
one it last applied), so `reassert` clears and then sets the active preset
on the gate's session for that page. The proxy calls it after relaying the
reply to such a capture (cdpproxy.py); a `DeviceController` registers it
with the proxy it is built on (`after_clipped_capture`). A page the gate
holds no session on gets nothing: there is no device of the gate's on it to
put back.

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
from .presets import DEFAULT_PRESET, DEFAULT_THEME, MAX_TOUCH_POINTS, THEMES, Device, UnknownPreset, resolve

log = logging.getLogger(__name__)

Record = Callable[[str, Mapping[str, str]], None]

#: Where Xvfb `:0` listens, inside the sidecar's own /tmp (section 3.1).
DEFAULT_DISPLAY_SOCKET = "/tmp/.X11-unix/X0"

#: The arguments a `device` request may carry. Anything else -- a width, a
#: scale, a user agent -- is refused: a caller names a preset, never a size.
_CTL_ARGS = frozenset({"preset", "orientation"})
#: The arguments a `theme` request may carry: a name, never a media feature.
_THEME_ARGS = frozenset({"theme"})

#: How long after another session (the agent's Playwright) attaches to a page
#: the gate sends its theme again. Playwright writes its own
#: `prefers-color-scheme` during the attach's set-up, and Chromium reports
#: nothing when that ends. One second is longer than that set-up on a page
#: already loaded, and short enough that a QA step's first read after the
#: attach is dark. A navigation re-sends it anyway (module docstring).
THEME_SETTLE_SECONDS = 1.0

# X11 core and RandR requests used here.
_X_QUERY_EXTENSION = 98
_X_GET_INPUT_FOCUS = 43
_RR_QUERY_VERSION = 0
_RR_SET_SCREEN_SIZE = 7
_RR_CREATE_MODE = 16
_RR_ADD_OUTPUT_MODE = 18
_RR_SET_CRTC_CONFIG = 21
_RR_GET_SCREEN_RESOURCES_CURRENT = 25
_RR_ROTATE_0 = 1

#: Screen pixels a window is fitted past the display, each way (module docstring).
WINDOW_OVERHANG = 1

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
    """Xvfb's screen size, set with RandR 1.3 over the X socket: the CRTC first.

    WHY NOT `RRSetScreenSize` ALONE. Xvfb starts with one CRTC driving one
    output (`vfbRandRInit`, xserver hw/vfb/InitOutput.c) in a mode as large as
    the whole screen, and RandR refuses with BadMatch any screen size smaller
    than an active CRTC's mode (`ProcRRSetScreenSize`, randr/rrscreen.c). The
    sidecar starts Xvfb at the largest preset, so a bare `RRSetScreenSize` to
    any smaller frame -- `desktop` at start-up first of all -- was refused:
    the DisplayError measured on 2026-10-11 (task_8cf26eb6e1f94e85adf8). The
    order here is what `xrandr` itself does: find (or create, once, by name)
    a mode the size of the frame and add it to the output, switch the CRTC
    off, set the screen size, and switch the CRTC back on in the new mode,
    so Chromium sees one output exactly as large as the screen again.
    """

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
        x = _XConnection(reader, writer)
        try:
            root = await _x_setup(reader, writer)
            name = b"RANDR"
            seq = x.send(struct.pack("<BxHH2x", _X_QUERY_EXTENSION, 2 + len(_pad(name)) // 4, len(name))
                         + _pad(name))
            reply = await x.reply(seq)
            if not reply[8]:
                raise DisplayError("display has no RandR extension")
            randr = reply[9]
            # RandR answers 1.3 requests only to a client that asked for 1.3.
            await x.reply(x.send(struct.pack("<BBHII", randr, _RR_QUERY_VERSION, 3, 1, 3)))
            resources = _ScreenResources.parse(
                await x.reply(x.send(struct.pack("<BBHI", randr, _RR_GET_SCREEN_RESOURCES_CURRENT, 2, root))))
            mm_w = max(1, round(width * _MM_PER_PIXEL))
            mm_h = max(1, round(height * _MM_PER_PIXEL))
            size = struct.pack("<BBHIHHII", randr, _RR_SET_SCREEN_SIZE, 5, root, width, height, mm_w, mm_h)
            if not resources.crtcs or not resources.outputs:
                # No CRTC holds the screen open: the size alone is enough.
                x.send(size)
                await x.reply(x.send(struct.pack("<BxH", _X_GET_INPUT_FOCUS, 1)))
            else:
                crtc, output = resources.crtcs[0], resources.outputs[0]
                mode = resources.mode_named(_mode_name(width, height))
                if mode is None:
                    mode = await self._create_mode(x, randr, root, width, height)
                x.send(struct.pack("<BBHII", randr, _RR_ADD_OUTPUT_MODE, 3, output, mode))
                await self._set_crtc(x, randr, crtc, resources.config_timestamp, 0, [])
                # RRSetScreenSize has no reply: an error for it arrives before
                # the next request's reply, which `_set_crtc` waits for.
                x.send(size)
                await self._set_crtc(x, randr, crtc, resources.config_timestamp, mode, [output])
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
        log.info("live device: display resized")

    @staticmethod
    async def _create_mode(x: "_XConnection", randr: int, root: int, width: int, height: int) -> int:
        name = _mode_name(width, height).encode("ascii")
        # Timings only make the mode well-formed; Xvfb drives no monitor.
        info = struct.pack("<IHHIHHHHHHHHI", 0, width, height, width * height * 60,
                           width, width, width, 0, height, height, height, len(name), 0)
        body = struct.pack("<I", root) + info + _pad(name)
        reply = await x.reply(x.send(struct.pack("<BBH", randr, _RR_CREATE_MODE, 1 + len(body) // 4) + body))
        (mode,) = struct.unpack_from("<I", reply, 8)
        return int(mode)

    @staticmethod
    async def _set_crtc(x: "_XConnection", randr: int, crtc: int, config_timestamp: int, mode: int,
                        outputs: list[int]) -> None:
        request = struct.pack("<BBHIIIhhIH2x", randr, _RR_SET_CRTC_CONFIG, 7 + len(outputs), crtc,
                              0, config_timestamp, 0, 0, mode, _RR_ROTATE_0)
        request += b"".join(struct.pack("<I", o) for o in outputs)
        reply = await x.reply(x.send(request))
        if reply[1] != 0:
            raise DisplayError(f"display refused the CRTC change (status={reply[1]})")


def _mode_name(width: int, height: int) -> str:
    """Xvfb's own name for a mode (`vfbRandRInit`), so its start-up mode is reused, not duplicated."""
    return f"{width}x{height}"


class _XConnection:
    """Numbers each request as the server does, so a reply is read by its sequence."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._seq = 0

    def send(self, request: bytes) -> int:
        self._writer.write(request)
        self._seq += 1
        return self._seq

    async def reply(self, seq: int) -> bytes:
        await self._writer.drain()
        return await _x_reply(self._reader, seq)


class _ScreenResources:
    """`RRGetScreenResourcesCurrent`'s reply: CRTCs, outputs and modes by name."""

    def __init__(self, config_timestamp: int, crtcs: list[int], outputs: list[int],
                 modes: dict[str, int]) -> None:
        self.config_timestamp = config_timestamp
        self.crtcs = crtcs
        self.outputs = outputs
        self.modes = modes

    @classmethod
    def parse(cls, reply: bytes) -> "_ScreenResources":
        try:
            _ts, config, n_crtcs, n_outputs, n_modes, n_names = struct.unpack_from("<IIHHHH", reply, 8)
            at = 32
            crtcs = list(struct.unpack_from(f"<{n_crtcs}I", reply, at))
            at += 4 * n_crtcs
            outputs = list(struct.unpack_from(f"<{n_outputs}I", reply, at))
            at += 4 * n_outputs
            infos = [struct.unpack_from("<IHHIHHHHHHHHI", reply, at + 32 * i) for i in range(n_modes)]
            at += 32 * n_modes
            names = reply[at:at + n_names]
            modes: dict[str, int] = {}
            offset = 0
            for info in infos:
                length = info[11]
                modes.setdefault(names[offset:offset + length].decode("latin-1"), int(info[0]))
                offset += length
        except struct.error:
            raise DisplayError("display sent malformed screen resources") from None
        return cls(int(config), [int(c) for c in crtcs], [int(o) for o in outputs], modes)

    def mode_named(self, name: str) -> int | None:
        return self.modes.get(name)


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


def crop(frame: Frame, width: int, height: int, top: int = 0) -> Frame:
    """`frame`'s `width` x `height` from row `top` at the left edge, copied; `frame` itself when that is all of it.

    A `top` past the frame (a display caught mid-resize) is held to its last row.
    """
    top = max(0, min(top, frame.height - 1))
    if top == 0 and frame.width <= width and frame.height <= height:
        return frame
    width, height = min(width, frame.width), min(height, frame.height - top)
    row = width * BYTES_PER_PIXEL
    pixels = bytearray(row * height)
    for y in range(height):
        at = (top + y) * frame.stride
        pixels[y * row:(y + 1) * row] = frame.pixels[at:at + row]
    return Frame(width, height, row, pixels, frame.red_mask, frame.green_mask, frame.blue_mask,
                 frame.little_endian)


def captured(frame: Frame, device: Device) -> Frame:
    """What the viewer is sent of a captured screen: `device`'s frame, from `Device.frame_top` down."""
    return crop(frame, device.display_width, device.frame_height, top=device.frame_top)


# ------------------------------------------------------------ the controller


class DeviceController:
    """Which device the browser is, applied from the gate's own sessions."""

    def __init__(self, cdp: GateCdp, display: Display, mask: ScaleSink, *, record: Record,
                 on_resize: Callable[[Device], None] | None = None,
                 on_change: Callable[[Device], None] | None = None) -> None:
        self._cdp = cdp
        self._display = display
        self._mask = mask
        self._record = record
        self._on_resize = on_resize
        #: Told the device once it is applied -- at start and on every change --
        #: so the gate can report it (report.py); never told of a failed one.
        self._on_change = on_change
        self._device = resolve(DEFAULT_PRESET)
        self._theme = DEFAULT_THEME
        self._pages: dict[str, str] = {}  # targetId -> the gate's session on it
        self._page_events: set[str] = set()  # the gate's sessions with `Page` enabled, for the theme
        self._windows: set[Any] = set()  # windowIds fitted to the current display
        self._browser: tuple[str, str] | None = None  # (user agent, version)
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        # The proxy puts the device back after an agent's clipped capture
        # (module docstring); a test's scripted CDP has no such hook.
        watch = getattr(cdp, "after_clipped_capture", None)
        if callable(watch):
            watch(self.reassert)

    @property
    def device(self) -> Device:
        return self._device

    @property
    def theme(self) -> str:
        """The colour scheme every page is told to prefer (`presets.THEMES`)."""
        return self._theme

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
                    await self._attach(info["targetId"], fit=False)
            await self._apply_display(self._device)
        if self._on_change is not None:
            self._on_change(self._device)

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
                for target in list(self._pages):
                    await self._apply_page_again(device, target)
            except Exception as exc:
                self._record("device_failed", {"preset": device.name, "orientation": device.orientation,
                                               "cause": type(exc).__name__})
                raise
            self._device = device
        if self._on_change is not None:
            self._on_change(device)
        self._record("device_set", {"preset": device.name, "orientation": device.orientation})
        log.info("live device: preset=%s orientation=%s", device.name, device.orientation)
        return device

    async def handle_theme(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The ctl.sock `theme` operation (`swarm-handoff theme <name>`).

        A name off `THEMES`, or anything beside the name, is refused before
        anything is sent. A page whose session went is attached again, as a
        device switch does; one that is gone for good is dropped.
        """
        theme = args.get("theme") if isinstance(args, Mapping) else None
        if not isinstance(args, Mapping) or set(args) != _THEME_ARGS or theme not in THEMES:
            self._record("theme_refused", {"cause": "not_a_theme"})
            raise DeviceRefused("a theme is chosen by name only")
        async with self._lock:
            previous, self._theme = self._theme, theme
            try:
                for target in list(self._pages):
                    session = self._pages.get(target)
                    if session is None:
                        continue
                    try:
                        await self._apply_theme(session, force=True)
                    except Exception:  # noqa: BLE001 -- a session that went: the device path attaches again
                        await self._apply_page_again(self._device, target)
            except Exception as exc:
                self._theme = previous
                self._record("theme_failed", {"theme": theme, "cause": type(exc).__name__})
                raise
        self._record("theme_set", {"theme": theme})
        log.info("live theme: %s", theme)
        return {"theme": theme}

    async def reassert(self, target_id: str) -> None:
        """The active preset's metrics again on the gate's session on
        `target_id`: cleared first, because Chromium skips parameters
        identical to the last it applied (module docstring, THE DEVICE AGAIN
        AFTER A CLIPPED CAPTURE). Nothing when the gate holds no session on it."""
        async with self._lock:
            session = self._pages.get(target_id)
            if session is None:
                return
            await self._cdp.call("Emulation.clearDeviceMetricsOverride", {}, session)
            await self._cdp.call("Emulation.setDeviceMetricsOverride", _metrics(self._device), session)

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
                self._page_events.discard(self._pages.pop(target, None) or "")
        elif method == "Target.detachedFromTarget":
            session = params.get("sessionId")
            if message.get("sessionId") is None and session in self._pages.values():
                for target, held in list(self._pages.items()):
                    if held == session:
                        del self._pages[target]
                        self._page_events.discard(session)
                        # The overrides went with the session; the page may not
                        # have (a renderer swap). A closed page fails the attach
                        # quietly, its `targetDestroyed` right behind.
                        self._spawn(self._attach_quietly(target))
            else:
                # Another session left the page, and Blink lifted what it set.
                self._theme_again(params.get("targetId"), 0.0)
        elif method == "Target.attachedToTarget":
            info = params.get("targetInfo")
            if params.get("sessionId") not in self._pages.values() and isinstance(info, Mapping):
                self._theme_again(info.get("targetId"), THEME_SETTLE_SECONDS)
        elif method == "Page.frameNavigated":
            frame = params.get("frame")
            if isinstance(frame, Mapping) and frame.get("parentId") is None:
                session = message.get("sessionId")
                for target, held in list(self._pages.items()):
                    if held == session:
                        self._theme_again(target, 0.0)

    def _theme_again(self, target: Any, delay: float) -> None:
        """Send the theme to `target` again, after `delay`, if one is set and the
        gate holds the page (module docstring, THE THEME IS PUT BACK)."""
        if self._theme != DEFAULT_THEME and isinstance(target, str) and target in self._pages:
            self._spawn(self._theme_again_later(target, delay))

    async def _theme_again_later(self, target: str, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            async with self._lock:
                session = self._pages.get(target)
                if session is not None and self._theme != DEFAULT_THEME:
                    await self._apply_theme(session)
        except Exception as exc:  # noqa: BLE001 -- a page that went; its own events follow
            log.info("live theme: not sent again (%s)", type(exc).__name__)

    def _spawn(self, work: Any) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _attach_quietly(self, target_id: str) -> None:
        try:
            await self.attach_page(target_id)
        except Exception as exc:  # a page that closed before the attach answered
            log.info("live device: page attach failed (%s)", type(exc).__name__)

    async def _attach(self, target_id: str, *, fit: bool = True) -> None:
        session = self._pages.get(target_id)
        if session is not None:
            try:
                await self._apply_page(self._device, target_id, session)
            except Exception:  # noqa: BLE001 -- a session that went: attach again, below
                if self._pages.get(target_id) == session:
                    del self._pages[target_id]
                session = None
        if session is None:
            result = await self._cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session = str(result["sessionId"])
            self._pages[target_id] = session
            await self._apply_page(self._device, target_id, session)
        if fit:
            await self._fit_window(self._device, target_id)

    async def _apply_page_again(self, device: Device, target_id: str) -> None:
        """`device` on one page, through a fresh session when its own went.

        A device switch never fails because one page's session is gone
        (DEVICE-RECOVER; part of #1030). MEASURED 2026-10-13: a switch that met
        a session Chromium had dropped raised "Session with given id not
        found" out of `select`, the whole change was refused, and the dead
        session stayed in `_pages` to fail the next switch the same way. A
        page that is gone for good is dropped, not an error: its
        `targetDestroyed` is on the way.
        """
        session = self._pages.get(target_id)
        if session is not None:
            try:
                await self._apply_page(device, target_id, session)
                return
            except Exception as exc:  # noqa: BLE001 -- attach again, below
                log.info("live device: page session lost (%s); attaching again", type(exc).__name__)
                if self._pages.get(target_id) == session:
                    del self._pages[target_id]
        try:
            result = await self._cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session = str(result["sessionId"])
            self._pages[target_id] = session
            await self._apply_page(device, target_id, session)
        except Exception as exc:  # noqa: BLE001 -- the page closed; nothing to apply to
            self._pages.pop(target_id, None)
            self._record("device_page_lost", {"preset": device.name, "cause": type(exc).__name__})

    async def _browser_version(self) -> tuple[str, str]:
        if self._browser is None:
            version = await self._cdp.call("Browser.getVersion", {})
            product = str(version.get("product", ""))
            number = product.split("/", 1)[1] if "/" in product else "0.0.0.0"
            self._browser = (str(version.get("userAgent", "")), number)
        return self._browser

    async def _apply_display(self, device: Device) -> None:
        await self._display.resize(device.display_width, device.display_height)
        self._mask.set_scale(device.screen_scale)
        if self._on_resize is not None:
            self._on_resize(device)
        self._windows.clear()
        for target in list(self._pages):
            try:
                await self._fit_window(device, target)
            except Exception as exc:  # noqa: BLE001 -- a page that closed: the next one fits the window
                log.info("live device: window not fitted (%s)", type(exc).__name__)

    async def _fit_window(self, device: Device, target_id: str) -> None:
        """The window holding `target_id` at the display's size plus the overhang, once per display size."""
        window = (await self._cdp.call("Browser.getWindowForTarget", {"targetId": target_id}))["windowId"]
        if window in self._windows:
            return  # one window (section 3.1): every page after the first is in it
        await self._cdp.call("Browser.setWindowBounds", {
            "windowId": window,
            "bounds": {"left": 0, "top": 0, "width": device.display_width + WINDOW_OVERHANG,
                       "height": device.display_height + WINDOW_OVERHANG, "windowState": "normal"},
        })
        self._windows.add(window)

    async def _apply_page(self, device: Device, target_id: str, session: str) -> None:
        user_agent, version = await self._browser_version()
        await self._cdp.call("Emulation.setDeviceMetricsOverride", _metrics(device), session)
        await self._cdp.call("Emulation.setUserAgentOverride", {
            "userAgent": device.user_agent(user_agent, version),
            "platform": device.preset.platform,
            "userAgentMetadata": device.user_agent_metadata(version),
        }, session)
        touch: dict[str, Any] = {"enabled": device.touch}
        if device.touch:
            touch["maxTouchPoints"] = MAX_TOUCH_POINTS
        await self._cdp.call("Emulation.setTouchEmulationEnabled", touch, session)
        await self._apply_theme(session)

    async def _apply_theme(self, session: str, *, force: bool = False) -> None:
        """The theme on one page's session. `system` sends nothing unless `force`
        (the change TO system, which lifts the override): see the module docstring."""
        if self._theme == DEFAULT_THEME and not force:
            return
        value = "" if self._theme == DEFAULT_THEME else self._theme
        await self._cdp.call("Emulation.setEmulatedMedia", {
            "features": [{"name": "prefers-color-scheme", "value": value}],
        }, session)
        if self._theme != DEFAULT_THEME and session not in self._page_events:
            # The page's navigations, so the theme is sent again after each.
            await self._cdp.call("Page.enable", {}, session)
            self._page_events.add(session)


def _metrics(device: Device) -> dict[str, Any]:
    """`Emulation.setDeviceMetricsOverride`'s parameters for `device`."""
    return {
        "width": device.width,
        "height": device.height,
        "deviceScaleFactor": device.scale,
        "mobile": device.mobile,
        # No `scale`: it magnifies the page inside a view Chromium still
        # clips at the CSS size (module docstring). The page fills the
        # display because the display IS the CSS size.
        "screenWidth": device.width,
        "screenHeight": device.height,
        "screenOrientation": device.screen_orientation(),
    }
