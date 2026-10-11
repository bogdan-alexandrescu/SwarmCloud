"""Recording a live session's masked frames and its action log (LB-L7, LB-4).

docs/design/live-browser.md §7. The RELAY records, not the pod: it already
carries every masked frame, and an object written by the relay's identity
cannot be forged or padded by the agent, which shares the tenant's GSA with
everything else in its pod. Nothing unmasked reaches the relay, so nothing
unmasked can be recorded.

HOW IT SEES FRAMES. For each tunnel the relay opens one more RFB connection of
its own (`rec-...`, see rendezvous.py), and this module is an RFB 3.8 client on
it: it asks the gate (`livegate/rfb.py::RfbSession`) for 32-bit RGBX, offers
Zlib, Raw and DesktopSize -- exactly the encodings the gate sends -- keeps the
framebuffer, and samples it into one-minute VP8 segments, at 1 fps while
nobody watches and 10 fps while someone does (Q11: record when nobody watches,
at 1 fps). A segment ends early when the rate or the display size changes, so
each one has one rate and one size.

WHAT IS WRITTEN, AND WHERE. Only these two names, under the attempt's `live/`
key, which the bucket's 7-day suffix rule and the relay's create-only IAM
condition both match (§7):

    tenants/<t>/tasks/<task>/attempts/<a>/live/rec-<seq>.live.webm
    tenants/<t>/tasks/<task>/attempts/<a>/live/actions-<seq>.live.jsonl

`<seq>` is the segment's start time in epoch milliseconds, 13 digits: ordered,
and unique across relay restarts, which a counter from 1 would not be -- and a
create-only writer cannot overwrite a name an earlier relay process used.
`GcsSegmentWriter` refuses any other name before it reaches GCS, and every
path component is checked so a tenant, task or attempt id cannot climb out of
the prefix.

THE ACTION LOG keeps only named fields (`ACTION_FIELDS`) of each line the gate
sends, bounded in size: the gate already redacts URLs and sends
`Input.insertText` as a length (§7), and the relay drops anything else, so a
parameter that could hold typed text never reaches a recording even if a gate
sent one.

THE SERVER NEVER SENDS ServerCutText (the gate has no code for it, §5.6). A
recorder that receives one, or a colour map, stops: the stream is not the gate's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import time
import zlib
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)

SUFFIX_VIDEO = ".live.webm"
SUFFIX_ACTIONS = ".live.jsonl"
_KINDS = {"rec": (SUFFIX_VIDEO, "video/webm"), "actions": (SUFFIX_ACTIONS, "application/x-ndjson")}

#: §7: one-minute segments; 1 fps with nobody watching, up to 10 with a viewer.
SEGMENT_SECONDS = 60
IDLE_FPS = 1
WATCHED_FPS = 10

ACTION_FIELDS = ("kind", "method", "url", "length", "event", "cause", "outcome")
MAX_ACTION_VALUE = 2048
MAX_ACTION_LINES_PER_SEGMENT = 20_000

#: A display the gate can produce is at most a few megapixels (LB-6 presets
#: top out at 1440x900); anything near this is not the gate.
MAX_PIXELS = 4096 * 4096

_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_LIVE_NAME = re.compile(
    r"tenants/(?P<t>[^/]+)/tasks/(?P<k>[^/]+)/attempts/(?P<a>[^/]+)/live/"
    r"(?:rec-[0-9]{13}\.live\.webm|actions-[0-9]{13}\.live\.jsonl)")


class RecordingError(Exception):
    """The recorder's RFB stream is not one the gate would send."""


def _component(value: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value) or ".." in value:
        raise ValueError("an id in a recording's path must be a single safe path component")
    return value


def live_prefix(tenant_id: str, task_id: str, attempt_id: str) -> str:
    return (f"tenants/{_component(tenant_id)}/tasks/{_component(task_id)}"
            f"/attempts/{_component(attempt_id)}/live/")


def segment_name(tenant_id: str, task_id: str, attempt_id: str, kind: str, seq: int) -> str:
    if kind not in _KINDS:
        raise ValueError(f"unknown segment kind {kind!r}")
    if not 0 <= int(seq) < 10**13:
        raise ValueError("segment sequence out of range")
    return f"{live_prefix(tenant_id, task_id, attempt_id)}{kind}-{int(seq):013d}{_KINDS[kind][0]}"


def is_live_name(name: str) -> bool:
    match = _LIVE_NAME.fullmatch(name)
    if match is None:
        return False
    try:
        for part in ("t", "k", "a"):
            _component(match.group(part))
    except ValueError:
        return False
    return True


class SegmentWriter(Protocol):
    def write(self, name: str, data: bytes, content_type: str) -> None: ...


class GcsSegmentWriter:
    """Creates segment objects in the artifact bucket, and nothing else.

    `if_generation_match=0` makes every write a create: the relay's IAM grant is
    `storage.objects.create` only (§7), so an overwrite would be refused there
    anyway; asking for create-only here makes that the code's rule too.
    """

    def __init__(self, bucket: Any) -> None:
        self._bucket = bucket

    def write(self, name: str, data: bytes, content_type: str) -> None:
        if not is_live_name(name):
            raise ValueError("a recording is written only under an attempt's live/ key, "
                             f"ending {SUFFIX_VIDEO} or {SUFFIX_ACTIONS}")
        self._bucket.blob(name).upload_from_string(data, content_type=content_type, if_generation_match=0)


# ---------------------------------------------------------------- encoding


class EncoderStream(Protocol):
    async def write(self, rgb: bytes) -> None: ...
    async def finish(self) -> bytes: ...


class Encoder(Protocol):
    async def open(self, width: int, height: int, fps: int) -> EncoderStream: ...


class FfmpegVp8Encoder:
    """VP8 in WebM through an `ffmpeg` binary on the relay's image.

    Frames go in as raw RGB24 on stdin; the segment comes back on stdout, read
    concurrently so neither pipe can fill and stall the other.
    """

    def __init__(self, binary: str = "ffmpeg") -> None:
        self._binary = binary

    async def open(self, width: int, height: int, fps: int) -> EncoderStream:
        proc = await asyncio.create_subprocess_exec(
            self._binary, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps),
            "-i", "pipe:0",
            "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
            "-b:v", "0", "-crf", "30", "-pix_fmt", "yuv420p", "-f", "webm", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        return _FfmpegStream(proc)


class _FfmpegStream:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        assert proc.stdout is not None
        self._out = asyncio.create_task(proc.stdout.read())

    async def write(self, rgb: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(rgb)
        await self._proc.stdin.drain()

    async def finish(self) -> bytes:
        assert self._proc.stdin is not None
        self._proc.stdin.close()
        data = await self._out
        code = await self._proc.wait()
        if code != 0:
            raise RecordingError(f"ffmpeg exited {code}")
        return data


# ---------------------------------------------------------------- the RFB client


_VERSION = b"RFB 003.008\n"
_ENC_RAW, _ENC_ZLIB, _ENC_DESKTOP_SIZE = 0, 6, -223
# 32 bpp, depth 24, little-endian, true colour, 8 bits a channel, red in the
# lowest byte: each pixel arrives as R, G, B, X.
_PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)


class RfbDecoder:
    """The client half of RFB 3.8, enough to keep the gate's framebuffer."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._out = bytearray()
        self._state = "version"
        self._zlib = zlib.decompressobj()
        self.width = 0
        self.height = 0
        self._fb = bytearray()
        self.version = 0  # bumped on every update applied
        self.resized = 0  # bumped on every size change

    @property
    def ready(self) -> bool:
        return self._state == "normal" and self.version > 0

    def take_outbound(self) -> bytes:
        out, self._out = bytes(self._out), bytearray()
        return out

    def request(self, incremental: bool) -> None:
        if self._state == "normal":
            self._out += struct.pack(">BBHHHH", 3, int(incremental), 0, 0, self.width, self.height)

    def rgb(self) -> bytes:
        out = bytearray(self.width * self.height * 3)
        out[0::3] = self._fb[0::4]
        out[1::3] = self._fb[1::4]
        out[2::3] = self._fb[2::4]
        return bytes(out)

    def feed(self, data: bytes) -> None:
        self._buf += data
        while self._step():
            pass

    def _resize(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
            raise RecordingError("display size out of range")
        self.width, self.height = width, height
        self._fb = bytearray(width * height * 4)
        self.resized += 1

    def _step(self) -> bool:
        buf = self._buf
        if self._state == "version":
            if len(buf) < 12:
                return False
            if bytes(buf[:12]) != _VERSION:
                raise RecordingError("the gate does not speak RFB 3.8")
            del buf[:12]
            self._out += _VERSION
            self._state = "security"
            return True
        if self._state == "security":
            if not buf or len(buf) < 1 + buf[0]:
                return False
            types = bytes(buf[1:1 + buf[0]])
            del buf[:1 + len(types)]
            if 1 not in types:
                raise RecordingError("the gate offered no security type None")
            self._out += bytes([1])
            self._state = "result"
            return True
        if self._state == "result":
            if len(buf) < 4:
                return False
            (result,) = struct.unpack(">I", bytes(buf[:4]))
            del buf[:4]
            if result != 0:
                raise RecordingError("the gate refused the recorder's handshake")
            self._out += bytes([1])  # ClientInit: shared
            self._state = "init"
            return True
        if self._state == "init":
            if len(buf) < 24:
                return False
            (name_len,) = struct.unpack(">I", bytes(buf[20:24]))
            if name_len > 4096:
                raise RecordingError("desktop name too long")
            if len(buf) < 24 + name_len:
                return False
            width, height = struct.unpack(">HH", bytes(buf[:4]))
            del buf[:24 + name_len]
            self._resize(width, height)
            self._out += bytes([0, 0, 0, 0]) + _PIXEL_FORMAT
            self._out += struct.pack(">BxH3i", 2, 3, _ENC_ZLIB, _ENC_RAW, _ENC_DESKTOP_SIZE)
            self._state = "normal"
            self.request(incremental=False)
            return True
        return self._message()

    def _message(self) -> bool:
        buf = self._buf
        if not buf:
            return False
        kind = buf[0]
        if kind == 2:  # Bell
            del buf[:1]
            return True
        if kind != 0:
            # SetColourMapEntries (1) and ServerCutText (3) are messages the
            # gate never sends; the second would carry the pod's clipboard.
            raise RecordingError(f"server message {kind} is not one the gate sends")
        if len(buf) < 4:
            return False
        (count,) = struct.unpack(">H", bytes(buf[2:4]))
        offset = 4
        rects: list[tuple[int, int, int, int, int, int, int]] = []
        for _ in range(count):
            if len(buf) < offset + 12:
                return False
            x, y, w, h, enc = struct.unpack(">HHHHi", bytes(buf[offset:offset + 12]))
            offset += 12
            if enc == _ENC_RAW:
                start, length = offset, w * h * 4
            elif enc == _ENC_ZLIB:
                if len(buf) < offset + 4:
                    return False
                (length,) = struct.unpack(">I", bytes(buf[offset:offset + 4]))
                start = offset + 4
            elif enc == _ENC_DESKTOP_SIZE:
                start, length = offset, 0
            else:
                raise RecordingError(f"encoding {enc} is not one the recorder asked for")
            if len(buf) < start + length:
                return False
            rects.append((x, y, w, h, enc, start, length))
            offset = start + length
        payload = bytes(buf[:offset])
        del buf[:offset]
        for x, y, w, h, enc, start, length in rects:
            if enc == _ENC_DESKTOP_SIZE:
                self._resize(w, h)
                # The gate keeps one zlib stream per connection across a
                # resize, so the decompressor carries on too.
                self.request(incremental=False)
                continue
            pixels = payload[start:start + length]
            if enc == _ENC_ZLIB:
                pixels = self._zlib.decompress(pixels, w * h * 4 + 1)
            self._blit(x, y, w, h, pixels)
        if rects:
            self.version += 1
        return True

    def _blit(self, x: int, y: int, w: int, h: int, pixels: bytes) -> None:
        if x + w > self.width or y + h > self.height or len(pixels) != w * h * 4:
            raise RecordingError("an update does not fit the display")
        row, stride = w * 4, self.width * 4
        for r in range(h):
            at = (y + r) * stride + x * 4
            self._fb[at:at + row] = pixels[r * row:(r + 1) * row]


# ---------------------------------------------------------------- the recorder


class Recorder:
    """One tunnel's recording: an RFB client on its own connection, a segment
    encoder, and the action log, written as create-only objects under `live/`."""

    def __init__(self, *, tenant_id: str, task_id: str, attempt_id: str,
                 send: Callable[[bytes], Awaitable[None]], writer: SegmentWriter, encoder: Encoder,
                 clock: Callable[[], float] = time.time, segment_seconds: int = SEGMENT_SECONDS,
                 conn_id: str | None = None) -> None:
        live_prefix(tenant_id, task_id, attempt_id)  # refuse a bad id before anything streams
        self.tenant_id, self.task_id, self.attempt_id = tenant_id, task_id, attempt_id
        self.conn_id = conn_id or "rec-" + f"{int(clock() * 1000):x}"
        self._send = send
        self._writer = writer
        self._encoder = encoder
        self._clock = clock
        self._segment_seconds = segment_seconds
        self._decoder = RfbDecoder()
        self._viewers = 0
        self._stream: EncoderStream | None = None
        self._stream_shape: tuple[int, int, int] | None = None
        self._segment_start: float | None = None
        self._actions: list[str] = []
        self._closed = False

    @property
    def ready(self) -> bool:
        return self._decoder.ready

    @property
    def fps(self) -> int:
        return WATCHED_FPS if self._viewers > 0 else IDLE_FPS

    def set_viewers(self, count: int) -> None:
        self._viewers = max(0, count)

    async def _flush_outbound(self) -> None:
        out = self._decoder.take_outbound()
        if out:
            await self._send(out)

    async def feed(self, data: bytes) -> None:
        if self._closed:
            return
        self._decoder.feed(data)
        await self._flush_outbound()

    async def action(self, entry: Any) -> None:
        if self._closed or not isinstance(entry, dict):
            return
        line: dict[str, Any] = {"t": self._clock()}
        for name in ACTION_FIELDS:
            value = entry.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not (
                    isinstance(value, int) or (isinstance(value, str) and len(value) <= MAX_ACTION_VALUE)):
                # Not the shape the gate sends: the whole line is dropped, not
                # half of it, so a recording never holds a line that misleads.
                return
            line[name] = value
        if len(line) == 1 or len(self._actions) >= MAX_ACTION_LINES_PER_SEGMENT:
            return
        if self._segment_start is None:
            self._segment_start = self._clock()
        self._actions.append(json.dumps(line, separators=(",", ":"), sort_keys=True))

    async def tick(self) -> None:
        """Add the current frame to the segment and ask the gate for the next."""
        if self._closed:
            return
        now = self._clock()
        await self._rotate_if_due(now)
        if not self._decoder.ready:
            return
        shape = (self._decoder.width, self._decoder.height, self.fps)
        if self._stream is not None and self._stream_shape != shape:
            await self._end_segment()
        if self._stream is None:
            self._stream = await self._encoder.open(*shape)
            self._stream_shape = shape
            if self._segment_start is None:
                self._segment_start = now
        await self._stream.write(self._decoder.rgb())
        self._decoder.request(incremental=True)
        await self._flush_outbound()

    async def _rotate_if_due(self, now: float) -> None:
        if self._segment_start is not None and now - self._segment_start >= self._segment_seconds:
            await self._end_segment()

    async def _end_segment(self) -> None:
        start, self._segment_start = self._segment_start, None
        stream, self._stream, self._stream_shape = self._stream, None, None
        actions, self._actions = self._actions, []
        if start is None:
            return
        seq = int(start * 1000)
        if stream is not None:
            data = await stream.finish()
            await self._write("rec", seq, data)
        if actions:
            await self._write("actions", seq, ("\n".join(actions) + "\n").encode())

    async def _write(self, kind: str, seq: int, data: bytes) -> None:
        name = segment_name(self.tenant_id, self.task_id, self.attempt_id, kind, seq)
        await asyncio.to_thread(self._writer.write, name, data, _KINDS[kind][1])
        log.info("live recorder: wrote %s (%d bytes)", name, len(data))

    async def run(self) -> None:
        while not self._closed:
            await self.tick()
            await asyncio.sleep(1 / self.fps)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._end_segment()
