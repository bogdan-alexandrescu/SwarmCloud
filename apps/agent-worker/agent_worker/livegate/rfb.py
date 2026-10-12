"""The live gate's RFB server: masked frames out, the controller's input in (L3).

docs/design/live-browser.md §3.1 (the gate encodes the masked framebuffer as
RFB), §4.2 (control mode), §5.6 (keyboard and clipboard), §5.3.3 (keys never
logged) and §5.10 (controller input is rate limited at the gate). The console
runs noVNC; between it and this module are swarm-api and the relay, which
carry each viewer's RFB byte stream through the tunnel as its own connection
with its own id. This module speaks RFB 3.8 on one such stream.

ONLY MASKED FRAMES. `FrameHub.publish` takes a `MaskedFrame`, and the only way
to make one is `masked(frame, mask_state)`, which paints the frame with the
mask (§5.3) first. A raw `Frame` is refused with `TypeError`, so a capture
loop that forgot the mask fails at once instead of streaming.

INPUT ONLY FROM THE CONTROLLER, ONLY IN CONTROL MODE (§5.6). Every key,
pointer and paste message is read -- the stream must stay in step -- and then
discarded unless `ControlState.controller` is this connection's id. With no
controller (view mode) every connection's input is discarded; with one, every
other connection's is. Control changes hands through `ControlState.grant` and
`revoke`, which release every key and button the previous controller held.

THE CLIPBOARD IS ONE-WAY (§5.6, Q10). This server NEVER SENDS ServerCutText:
pod-to-console clipboard would hand a viewer whatever the agent copied,
unmasked, and the module has no code that writes that message. Console-to-pod
paste (`ClientCutText`) from the controller is typed as keys through the
injector, so it is masked and unlogged like typing; from anyone else it is
discarded.

RATE LIMIT (§5.10). The controller's injected events are limited to
`MAX_INPUT_EVENTS_PER_SECOND` by a token bucket; a paste costs one token per
character and is dropped whole if it does not fit -- half a password typed is
worse than none.

NOTHING READ FROM A VIEWER IS LOGGED (§5.3.3). Log lines carry the connection
id, counts and outcomes. No keysym, character, pasted text or coordinate is
ever an argument to a log call; `SessionCounts` is what the audit's "41 key
events, 9 pointer events" is made from.

AUTHENTICATION IS NOT HERE. The security type offered is None: the viewer was
authenticated by swarm-api on its own websocket (§5.2), and the relay admits
the stream only with a one-use ticket. A VNC password would be a second,
weaker secret on the same path.

ENCODINGS. Raw always; Zlib when the viewer offers it (noVNC does), one zlib
stream per connection. Each update carries the band of rows that changed since
the last one sent to that viewer; a resize is announced with the DesktopSize
pseudo-encoding, and a viewer that cannot take one is disconnected (noVNC
reconnects).
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Callable

from .capture import Frame
from .inject import MAX_TYPED_CHARS, Injector
from .mask import UNAVAILABLE_RGB, MaskState, Outcome

log = logging.getLogger(__name__)

PROTOCOL_VERSION = b"RFB 003.008\n"
_SECURITY_NONE = 1

ENCODING_RAW = 0
ENCODING_ZLIB = 6
ENCODING_DESKTOP_SIZE = -223

#: §5.10: controller input events per second, dropped above.
MAX_INPUT_EVENTS_PER_SECOND = 200

#: A ClientCutText longer than this is not read into memory; the connection
#: is closed. A paste longer than `MAX_TYPED_CHARS` is read and discarded.
MAX_CUT_TEXT_BYTES = 64 * 1024
_MAX_ENCODINGS = 256

_DESKTOP_NAME = b"swarm live browser"

# Client -> server message types (RFC 6143 §7.5).
_SET_PIXEL_FORMAT = 0
_SET_ENCODINGS = 2
_FRAMEBUFFER_UPDATE_REQUEST = 3
_KEY_EVENT = 4
_POINTER_EVENT = 5
_CLIENT_CUT_TEXT = 6

# Server -> client: FramebufferUpdate is the only message this server sends.
_FRAMEBUFFER_UPDATE = 0

_PIXEL_FORMAT = struct.Struct(">BBBBHHHBBB3x")


class RfbError(Exception):
    """The viewer sent something this server will not handle; the connection ends."""


class MaskedFrame:
    """A frame the mask has painted. Made only by `masked`."""

    __slots__ = ("frame", "outcome")

    def __init__(self, frame: Frame, outcome: Outcome, *, _token: object) -> None:
        if _token is not _MASKED:
            raise TypeError("a MaskedFrame is made by rfb.masked(), which applies the mask")
        self.frame = frame
        self.outcome = outcome


_MASKED = object()


def masked(frame: Frame, mask: MaskState) -> MaskedFrame:
    """Paint `frame` with the mask, in place, and mark it fit to leave the pod."""
    outcome = mask.apply(frame)
    return MaskedFrame(frame, outcome, _token=_MASKED)


class FrameHub:
    """The latest masked frame, and a wake-up for every session waiting on one."""

    def __init__(self, width: int = 1280, height: int = 800) -> None:
        # Until the first frame: the "masking unavailable" fill (§5.3.2).
        blank = Frame.blank(width, height, UNAVAILABLE_RGB)
        self._current = MaskedFrame(blank, Outcome.UNAVAILABLE, _token=_MASKED)
        self.version = 0
        self._waiters: set[asyncio.Event] = set()

    @property
    def current(self) -> MaskedFrame:
        return self._current

    def publish(self, frame: MaskedFrame) -> None:
        if not isinstance(frame, MaskedFrame):
            raise TypeError("only a MaskedFrame may be published: apply the mask with rfb.masked()")
        self._current = frame
        self.version += 1
        for event in self._waiters:
            event.set()

    def subscribe(self, event: asyncio.Event) -> None:
        self._waiters.add(event)

    def unsubscribe(self, event: asyncio.Event) -> None:
        self._waiters.discard(event)


class ControlState:
    """Which connection, if any, is the person in control (§4.2).

    Set by the gate when the relay passes on swarm-api's decision; this
    object does not decide who may control (§5.1), only enforces it.
    """

    def __init__(self, injector: Injector) -> None:
        self._injector = injector
        self._controller: str | None = None
        self._lock = asyncio.Lock()

    @property
    def controller(self) -> str | None:
        return self._controller

    def holds(self, conn_id: str) -> bool:
        return self._controller is not None and self._controller == conn_id

    async def grant(self, conn_id: str) -> None:
        async with self._lock:
            if self._controller == conn_id:
                return
            if self._controller is not None:
                await self._injector.release_all()
            self._controller = conn_id
            log.info("live rfb: control granted conn=%s", conn_id)

    async def revoke(self) -> None:
        async with self._lock:
            if self._controller is None:
                return
            await self._injector.release_all()
            log.info("live rfb: control revoked conn=%s", self._controller)
            self._controller = None

    async def connection_closed(self, conn_id: str) -> None:
        """The controller's stream ended: let go of what it held.

        Control itself is swarm-api's to end (renewal, idle, §4.2); until it
        does, the connection id stays the controller and a reconnect under a
        new id is a viewer.
        """
        async with self._lock:
            if self._controller == conn_id:
                await self._injector.release_all()


@dataclass
class SessionCounts:
    """What the audit records about a connection's input: counts, never content."""

    key_events: int = 0
    pointer_events: int = 0
    pastes: int = 0
    dropped: int = 0
    updates: int = 0


class _Bucket:
    def __init__(self, rate: int, clock: Callable[[], float]) -> None:
        self._rate = rate
        self._clock = clock
        self._tokens = float(rate)
        self._at = clock()

    def take(self, n: int = 1) -> bool:
        now = self._clock()
        self._tokens = min(float(self._rate), self._tokens + (now - self._at) * self._rate)
        self._at = now
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False


@dataclass(frozen=True)
class _Format:
    """A 32-bit true-colour pixel format: where each channel's byte sits."""

    big_endian: bool
    shifts: tuple[int, int, int]

    def offsets(self) -> tuple[int, int, int]:
        return tuple(  # type: ignore[return-value]
            (3 - s // 8) if self.big_endian else s // 8 for s in self.shifts)

    def pack(self) -> bytes:
        r, g, b = self.shifts
        return _PIXEL_FORMAT.pack(32, 24, int(self.big_endian), 1, 255, 255, 255, r, g, b)


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _frame_format(frame: Frame) -> _Format:
    return _Format(not frame.little_endian,
                   (_shift(frame.red_mask), _shift(frame.green_mask), _shift(frame.blue_mask)))


def _convert(frame: Frame, source: _Format, target: _Format) -> bytes:
    """The frame's pixels, rows packed, in `target`'s byte layout."""
    row = frame.width * 4
    if frame.stride == row:
        packed = bytes(frame.pixels[:row * frame.height])
    else:
        packed = b"".join(bytes(frame.pixels[y * frame.stride:y * frame.stride + row])
                          for y in range(frame.height))
    if source == target or source.offsets() == target.offsets():
        return packed
    out = bytearray(len(packed))
    for src, dst in zip(source.offsets(), target.offsets()):
        out[dst::4] = packed[src::4]
    return bytes(out)


class RfbSession:
    """One viewer's RFB connection, through the tunnel."""

    def __init__(self, conn_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 hub: FrameHub, control: ControlState, injector: Injector, *,
                 max_events_per_second: int = MAX_INPUT_EVENTS_PER_SECOND,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.conn_id = conn_id
        self._reader = reader
        self._writer = writer
        self._hub = hub
        self._control = control
        self._injector = injector
        self._bucket = _Bucket(max_events_per_second, clock)
        self.counts = SessionCounts()
        self._format: _Format | None = None  # the viewer's; None until ServerInit
        self._encodings: tuple[int, ...] = (ENCODING_RAW,)
        self._zlib: zlib._Compress | None = None
        self._size = (0, 0)
        self._last: bytes | None = None  # what this viewer has, in its format
        self._sent_version = -1
        self._pending: bool | None = None  # None: no request; else incremental?
        self._wake = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    async def serve(self) -> None:
        """Run the connection until either side ends it."""
        self._hub.subscribe(self._wake)
        reader: asyncio.Task[None] | None = None
        writer: asyncio.Task[None] | None = None
        try:
            await self._handshake()
            reader = asyncio.create_task(self._read_loop())
            writer = asyncio.create_task(self._write_loop())
            done, _ = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, (asyncio.IncompleteReadError, ConnectionError)):
                    raise exc
        except RfbError as exc:
            log.info("live rfb: conn=%s refused: %s", self.conn_id, exc)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for task in (reader, writer):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (reader, writer) if t is not None), return_exceptions=True)
            self._hub.unsubscribe(self._wake)
            await self._control.connection_closed(self.conn_id)
            self._writer.close()
            c = self.counts
            log.info("live rfb: conn=%s closed keys=%d pointer=%d pastes=%d dropped=%d updates=%d",
                     self.conn_id, c.key_events, c.pointer_events, c.pastes, c.dropped, c.updates)

    async def _handshake(self) -> None:
        self._writer.write(PROTOCOL_VERSION)
        await self._writer.drain()
        version = await self._reader.readexactly(12)
        if version != PROTOCOL_VERSION:
            raise RfbError("protocol version is not 3.8")
        self._writer.write(bytes([1, _SECURITY_NONE]))
        await self._writer.drain()
        chosen = (await self._reader.readexactly(1))[0]
        if chosen != _SECURITY_NONE:
            self._writer.write(struct.pack(">I", 1))
            await self._writer.drain()
            raise RfbError("unsupported security type")
        self._writer.write(struct.pack(">I", 0))
        # DRAIN BEFORE THE NEXT READ. The tunnel's writer sends nothing on
        # write(), only on drain(), and the viewer sends ClientInit only after
        # it has read this SecurityResult: without the drain both sides wait
        # and the Browser tab stays on "Connecting" (measured on SwarmCloud
        # 2026-10-12, test_live_rfb_tunnel_flush.py).
        await self._writer.drain()
        await self._reader.readexactly(1)  # ClientInit: shared or not, it is shared
        frame = self._hub.current.frame
        native = _frame_format(frame)
        self._format = native
        self._size = (frame.width, frame.height)
        self._writer.write(struct.pack(">HH", frame.width, frame.height) + native.pack()
                           + struct.pack(">I", len(_DESKTOP_NAME)) + _DESKTOP_NAME)
        await self._writer.drain()
        log.info("live rfb: conn=%s open", self.conn_id)

    # -- client messages --------------------------------------------------

    async def _read_loop(self) -> None:
        read = self._reader.readexactly
        while True:
            kind = (await read(1))[0]
            if kind == _SET_PIXEL_FORMAT:
                body = await read(19)
                self._set_pixel_format(body[3:])
            elif kind == _SET_ENCODINGS:
                (count,) = struct.unpack(">xH", await read(3))
                if count > _MAX_ENCODINGS:
                    raise RfbError("too many encodings")
                self._encodings = struct.unpack(f">{count}i", await read(4 * count))
                self._zlib = None
            elif kind == _FRAMEBUFFER_UPDATE_REQUEST:
                incremental = (await read(9))[0] != 0
                # A full request wins over an incremental one still pending.
                self._pending = incremental if self._pending is None else (self._pending and incremental)
                self._wake.set()
            elif kind == _KEY_EVENT:
                down, keysym = struct.unpack(">BxxI", await read(7))
                if self._admit(1):
                    self.counts.key_events += 1
                    await self._injector.key(keysym, bool(down))
            elif kind == _POINTER_EVENT:
                buttons, x, y = struct.unpack(">BHH", await read(5))
                if self._admit(1):
                    self.counts.pointer_events += 1
                    w, h = self._hub.current.frame.width, self._hub.current.frame.height
                    await self._injector.pointer_event(buttons, min(x, w - 1), min(y, h - 1))
            elif kind == _CLIENT_CUT_TEXT:
                (length,) = struct.unpack(">xxxI", await read(7))
                if length > MAX_CUT_TEXT_BYTES:
                    raise RfbError("cut text too long")
                text = (await read(length)).decode("latin-1")
                await self._paste(text)
            else:
                raise RfbError(f"unknown client message type {kind}")

    def _admit(self, cost: int) -> bool:
        """May this connection's input reach the display now? Counts a drop if not."""
        if not self._control.holds(self.conn_id) or not self._bucket.take(cost):
            self.counts.dropped += 1
            return False
        return True

    async def _paste(self, text: str) -> None:
        if not self._control.holds(self.conn_id) or not text or len(text) > MAX_TYPED_CHARS:
            self.counts.dropped += 1
            return
        if not self._admit(len(text)):
            return
        if await self._injector.type_text(text):
            self.counts.pastes += 1
        else:
            self.counts.dropped += 1

    def _set_pixel_format(self, body: bytes) -> None:
        bpp, _depth, big, true_colour, rmax, gmax, bmax, rs, gs, bs = _PIXEL_FORMAT.unpack(body)
        shifts = (rs, gs, bs)
        if (bpp != 32 or not true_colour or (rmax, gmax, bmax) != (255, 255, 255)
                or any(s % 8 or s > 24 for s in shifts) or len(set(shifts)) != 3):
            raise RfbError("unsupported pixel format")
        self._format = _Format(bool(big), shifts)
        self._last = None

    # -- server messages --------------------------------------------------

    async def _write_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._pending is None:
                continue
            incremental = self._pending
            if incremental and self._hub.version == self._sent_version:
                continue  # nothing new; answer when the next frame is published
            self._pending = None
            await self._send_update(incremental)

    async def _send_update(self, incremental: bool) -> None:
        current = self._hub.current
        frame = current.frame
        self._sent_version = self._hub.version
        if (frame.width, frame.height) != self._size:
            if ENCODING_DESKTOP_SIZE not in self._encodings:
                raise RfbError("display resized and the viewer cannot take DesktopSize")
            self._size = (frame.width, frame.height)
            self._last = None
            self._writer.write(struct.pack(">BxH", _FRAMEBUFFER_UPDATE, 1)
                               + struct.pack(">HHHHi", 0, 0, frame.width, frame.height,
                                             ENCODING_DESKTOP_SIZE))
            self.counts.updates += 1
            await self._writer.drain()
            # The viewer requests again at the new size.
            return
        assert self._format is not None
        pixels = _convert(frame, _frame_format(frame), self._format)
        row = frame.width * 4
        top, bottom = 0, frame.height
        if incremental and self._last is not None and len(self._last) == len(pixels):
            while top < bottom and pixels[top * row:(top + 1) * row] == self._last[top * row:(top + 1) * row]:
                top += 1
            while bottom > top and (pixels[(bottom - 1) * row:bottom * row]
                                    == self._last[(bottom - 1) * row:bottom * row]):
                bottom -= 1
            if top == bottom:
                # Published but identical (a static page): nothing to send,
                # so the request stays pending for the next frame.
                self._pending = True
                return
        self._last = pixels
        band = pixels[top * row:bottom * row]
        encoding = self._encoding()
        header = struct.pack(">BxH", _FRAMEBUFFER_UPDATE, 1) + struct.pack(
            ">HHHHi", 0, top, frame.width, bottom - top, encoding)
        if encoding == ENCODING_ZLIB:
            if self._zlib is None:
                self._zlib = zlib.compressobj()
            data = self._zlib.compress(band) + self._zlib.flush(zlib.Z_SYNC_FLUSH)
            self._writer.write(header + struct.pack(">I", len(data)) + data)
        else:
            self._writer.write(header + band)
        self.counts.updates += 1
        await self._writer.drain()

    def _encoding(self) -> int:
        for encoding in self._encodings:
            if encoding in (ENCODING_ZLIB, ENCODING_RAW):
                return encoding
        return ENCODING_RAW
