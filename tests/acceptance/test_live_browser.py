#!/usr/bin/env python3
"""The live-browser acceptance check: a person signs in on an agent's browser (LB-L13).

docs/design/live-browser.md section 9, lane L13, and docs/acceptance.md,
"live-browser". On a deployed environment, against one `claude-code-browser`
task that `scripts/acceptance/groups/live-browser.sh` submitted:

  handoff    the agent opens tests/acceptance/fixtures/login.html in the
             platform's Chromium and asks for help (`swarm-handoff request`);
             the task's live session shows the hand-off.
  isolation  the same task, asked for by a member of ANOTHER tenant, is a 404
             on every live route, and a viewer ticket re-pointed at that
             tenant is refused on the socket (4404).
  frames     a scripted viewer opens the console's socket (wss to swarm-api,
             /v1/live/<session>/stream, ticket as the first message), takes
             control, clicks the password field and types a password generated
             here at run time. Every frame it receives that shows the page has
             the field's box painted with the mask's colour, and no pixel of the
             field's own magenta anywhere.
  recording  the relay's recording of the session: segments are written under
             the task's attempt `live/` key, and their frames, decoded, have the
             field painted the same way.
  returned   control is returned, the agent's `wait` ends, it reads the code
             the page derived from the password (12 hex digits of its SHA-256)
             and reports it; the task SUCCEEDED; the four human_* events exist.
  no_leak    the password is in no object under the task's GCS prefix --
             transcript, logs, checkpoint archives (decompressed), outputs,
             recordings -- and not in the task, its events or its live audit.

STANDARD LIBRARY ONLY. It runs in the swarm-verify image (alpine, python3 with
no pip) inside the VPC, next to the shell suite: a websocket client, an RFB
3.8 viewer and a GCS reader are written out here rather than installed.

THE PASSWORD NEVER LEAVES THIS PROCESS. It is generated here, typed into the
socket as RFB key events, and compared here. It is never printed, never
written to the results file, never put in an argv or an environment. What is
printed is the code the page shows, which the agent reports anyway.

THE BASH SIDE OWNS THE TASK. live-browser.sh submits it, waits on it with the
suite's own `acc_settle` (so a closed pool or a missing credential is the same
SKIP as everywhere else), and writes the state it settled on to
`<control-dir>/settled`. This process waits for that file before it judges
anything that needs the task to have ended. The credentials are handed over
as files, read once and deleted.

The results file gets one JSON line per check, in CHECKS order:
`{"check": key, "verdict": "PASS"|"FAIL"|"SKIP", "reason": "..."}`.
"""

from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

#: The checks, in the order live-browser.sh lists them.
CHECKS = ("handoff", "isolation", "frames", "recording", "returned", "no_leak")

# --------------------------------------------------------------------------
# The fixture's layout (tests/acceptance/fixtures/login.html). A unit test
# holds these equal to the page's CSS.
# --------------------------------------------------------------------------

#: The cyan marker's size, in CSS pixels; it sits above the password field.
MARKER_CSS = (320, 40)
#: How far below the marker's top edge the field's top edge is, in CSS pixels.
FIELD_OFFSET_CSS = 80
#: The field's size, in CSS pixels (same left edge and width as the marker).
FIELD_CSS = (320, 40)
MARKER_RGB = (0x00, 0xFF, 0xFF)
FIELD_RGB = (0xFF, 0x00, 0xFF)
#: `livegate.mask.MASK_RGB`: what the gate paints a sensitive box with.
MASK_RGB = (0x22, 0x22, 0x22)
#: Where the agent serves the fixture directory, inside its own pod.
FIXTURE_PORT = 8765
FIXTURE_URL = f"http://127.0.0.1:{FIXTURE_PORT}/login.html"
#: The agent's reply when `swarm-handoff request` refused (exit 5).
REFUSED_PREFIX = "HANDOFF-REFUSED"
#: The output the agent writes the code to, under $SWARM_ARTIFACTS_DIR.
CODE_OUTPUT = "session-code.txt"

#: The events a hand-off that was answered and returned leaves
#: (`swarm_common.states.EventType`, read here by value).
HANDOFF_EVENTS = ("human_handoff_requested", "human_control_taken",
                  "human_control_returned", "human_handoff_resolved")
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")

#: The password's alphabet: lower-case letters and digits, each its own
#: keysym (X11 keysyms equal Latin-1 code points) on an unshifted key, so a
#: key press is exactly one character whatever the keyboard map.
PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789"
PASSWORD_LENGTH = 24
XK_RETURN = 0xFF0D

#: What one object may cost the sweep to read. An object it cannot read is
#: reported, never assumed clean.
MAX_OBJECT_BYTES = 256 * 1024 * 1024

# swarm-api's socket close codes (apps/swarm-api/swarm_api/routes/live.py).
CLOSE_REFUSED = 4404


def log(message: str) -> None:
    sys.stderr.write(f"live-browser: {message}\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------
# The password and what the page derives from it
# --------------------------------------------------------------------------


def new_password() -> str:
    """A password made here, now, for this run only."""
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def session_code(password: str) -> str:
    """What login.html shows after a sign-in: 12 hex digits of the SHA-256."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:12]


def keysyms(text: str) -> list[int]:
    """The keysym for each character; only the password's alphabet is typed."""
    out = []
    for char in text:
        if char not in PASSWORD_ALPHABET:
            raise ValueError("only the password alphabet is typed")
        out.append(ord(char))
    return out


# --------------------------------------------------------------------------
# Frames: where the marker is, where the field must be, and what it shows
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def _table(lo: int, hi: int) -> bytes:
    return bytes(1 if lo <= v <= hi else 0 for v in range(256))


def _near(value: int, tol: int) -> tuple[int, int]:
    return max(0, value - tol), min(255, value + tol)


def colour_map(pixels: bytes, bpp: int, rgb: tuple[int, int, int], tol: int) -> bytes:
    """One byte per pixel: 1 where the pixel is within `tol` of `rgb` on every
    channel. `pixels` is R, G, B first in each `bpp`-byte pixel."""
    r, g, b = (bytes(pixels[i::bpp]) for i in range(3))
    n = len(r)
    if n == 0:
        return b""
    hits = -1
    for channel, value in zip((r, g, b), rgb):
        lo, hi = _near(value, tol)
        hits &= int.from_bytes(channel.translate(_table(lo, hi)), "big")
    return hits.to_bytes(n, "big")


@dataclass(frozen=True)
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def area(self) -> int:
        return max(0, self.x1 - self.x0) * max(0, self.y1 - self.y0)

    def inset(self, px: int) -> "Box":
        return Box(self.x0 + px, self.y0 + px, self.x1 - px, self.y1 - px)

    def centre(self) -> tuple[int, int]:
        return (self.x0 + self.x1) // 2, (self.y0 + self.y1) // 2

    def within(self, width: int, height: int) -> bool:
        return 0 <= self.x0 < self.x1 <= width and 0 <= self.y0 < self.y1 <= height


def find_marker(pixels: bytes, width: int, height: int, bpp: int, tol: int) -> Box | None:
    """The marker's box: the bounding box of its colour, when that colour fills
    it (a rectangle, at least a third of its CSS width wide). None otherwise."""
    hits = colour_map(pixels[:width * height * bpp], bpp, MARKER_RGB, tol)
    first = hits.find(b"\x01")
    if first < 0:
        return None
    last = hits.rfind(b"\x01")
    box = Box(first % width, first // width, last % width + 1, last // width + 1)
    if box.x1 - box.x0 < MARKER_CSS[0] // 3 or box.y1 - box.y0 < MARKER_CSS[1] // 3:
        return None
    if hits.count(b"\x01") < 0.9 * box.area:
        return None
    return box


def field_box(marker: Box) -> Box:
    """Where login.html puts the password field, from where the marker is."""
    scale = (marker.x1 - marker.x0) / MARKER_CSS[0]
    top = marker.y0 + FIELD_OFFSET_CSS * scale
    return Box(marker.x0, round(top), marker.x0 + round(FIELD_CSS[0] * scale),
               round(top + FIELD_CSS[1] * scale))


def _region(pixels: bytes, width: int, bpp: int, box: Box) -> bytes:
    row = width * bpp
    return b"".join(bytes(pixels[y * row + box.x0 * bpp:y * row + box.x1 * bpp])
                    for y in range(box.y0, box.y1))


@dataclass(frozen=True)
class Judgement:
    """What one frame shows. `kind`: `no_page` (the fixture is not on screen,
    or the mask blanked the whole page), `painted`, or `leak`."""

    kind: str
    marker: Box | None = None
    field: Box | None = None
    painted: float = 0.0
    field_pixels: int = 0

    def describe(self) -> str:
        if self.kind == "no_page":
            return "the fixture's marker is not on screen"
        return (f"field box {self.field.x0},{self.field.y0}-{self.field.x1},{self.field.y1}: "
                f"{self.painted:.0%} mask paint, {self.field_pixels} field-coloured pixel(s) on screen")


def judge(pixels: bytes, width: int, height: int, *, bpp: int = 4, tol: int = 0,
          field: Box | None = None) -> Judgement:
    """Is the password field painted in this frame?

    `tol` 0 is for the RFB frames, which carry the gate's paint exactly; the
    recording is VP8, lossy, and is judged with a tolerance. `field`, when
    given, is where the field was measured to be (the recording is judged at
    the box the live frames found); the marker must still be on screen.
    """
    marker = find_marker(pixels, width, height, bpp, max(tol, 0x20))
    if marker is None:
        return Judgement("no_page")
    box = field or field_box(marker)
    if not box.within(width, height):
        return Judgement("no_page", marker)
    inner = box.inset(2 if tol == 0 else 3)
    region = _region(pixels, width, bpp, inner)
    painted = colour_map(region, bpp, MASK_RGB, tol).count(b"\x01") / max(1, inner.area)
    frame = pixels[:width * height * bpp]
    leaked = colour_map(frame, bpp, FIELD_RGB, max(tol, 0x20)).count(b"\x01")
    allowed = 0 if tol == 0 else box.area // 100
    ok = leaked <= allowed and painted >= (1.0 if tol == 0 else 0.95)
    return Judgement("painted" if ok else "leak", marker, box, painted, leaked)


@dataclass
class FrameTally:
    """Every judgement of one phase of the session."""

    judged: int = 0
    painted: int = 0
    no_page: int = 0
    leaks: list[str] = field(default_factory=list)
    field: Box | None = None
    marker: Box | None = None

    def add(self, verdict: Judgement) -> None:
        if verdict.kind == "no_page":
            self.no_page += 1
            return
        self.judged += 1
        if verdict.kind == "painted":
            self.painted += 1
            self.field, self.marker = verdict.field, verdict.marker
        elif len(self.leaks) < 5:
            self.leaks.append(verdict.describe())
        else:
            self.leaks.append("...")


# --------------------------------------------------------------------------
# RFB 3.8, the viewer's side (livegate/rfb.py is the server)
# --------------------------------------------------------------------------


class RfbError(Exception):
    """The server sent something the gate never sends."""


RFB_VERSION = b"RFB 003.008\n"
ENCODING_RAW = 0
ENCODING_ZLIB = 6
ENCODING_DESKTOP_SIZE = -223


class RfbClient:
    """An RFB 3.8 viewer over a byte stream that arrives in pieces.

    Asks for 32-bit pixels laid out R, G, B, X; offers Zlib, Raw and
    DesktopSize -- the encodings the gate sends -- and keeps the framebuffer.
    `feed` takes what arrived; `take_outbound` is what to send back.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._out: list[bytes] = []
        self._state = "version"
        self._zlib = zlib.decompressobj()
        self.width = 0
        self.height = 0
        self.fb = bytearray()
        self.updates = 0
        self.ready = False

    def take_outbound(self) -> list[bytes]:
        """Each message to send, one per websocket frame (swarm-api counts a
        controller's input by each frame's first byte)."""
        out, self._out = self._out, []
        return out

    def feed(self, data: bytes) -> int:
        """Consume `data`; returns how many framebuffer updates completed."""
        self._buf += data
        done = 0
        while True:
            before = self.updates
            if not self._step():
                break
            done += self.updates - before
        return done

    # -- input ----------------------------------------------------------------

    def key(self, keysym: int, down: bool) -> None:
        self._out.append(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    def pointer(self, x: int, y: int, buttons: int) -> None:
        self._out.append(struct.pack(">BBHH", 5, buttons, x, y))

    def request(self, incremental: bool) -> None:
        self._out.append(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0,
                                     self.width, self.height))

    # -- the stream -----------------------------------------------------------

    def _need(self, n: int) -> bool:
        return len(self._buf) >= n

    def _take(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def _step(self) -> bool:
        if self._state == "version":
            if not self._need(12):
                return False
            if not self._take(12).startswith(b"RFB 003.00"):
                raise RfbError("not an RFB server")
            self._out.append(RFB_VERSION)
            self._state = "security"
            return True
        if self._state == "security":
            if not self._need(1):
                return False
            count = self._buf[0]
            if count == 0:
                raise RfbError("the server refused the connection")
            if not self._need(1 + count):
                return False
            types = self._take(1 + count)[1:]
            if 1 not in types:
                raise RfbError("the server offers no security type None")
            self._out.append(b"\x01")
            self._state = "result"
            return True
        if self._state == "result":
            if not self._need(4):
                return False
            if struct.unpack(">I", self._take(4))[0] != 0:
                raise RfbError("security handshake failed")
            self._out.append(b"\x01")  # ClientInit: shared
            self._state = "init"
            return True
        if self._state == "init":
            if not self._need(24):
                return False
            (name_len,) = struct.unpack(">I", bytes(self._buf[20:24]))
            if not self._need(24 + name_len):
                return False
            header = self._take(24 + name_len)
            width, height = struct.unpack(">HH", header[:4])
            self._resize(width, height)
            self._out.append(struct.pack(">B3x", 0)
                             + struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16))
            self._out.append(struct.pack(">BxH", 2, 3)
                             + struct.pack(">3i", ENCODING_ZLIB, ENCODING_RAW, ENCODING_DESKTOP_SIZE))
            self.request(False)
            self._state = "normal"
            self.ready = True
            return True
        return self._message()

    def _resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.fb = bytearray(width * height * 4)

    def _message(self) -> bool:
        if not self._need(1):
            return False
        if self._buf[0] != 0:
            raise RfbError(f"server message type {self._buf[0]} (the gate sends only updates)")
        if not self._need(4):
            return False
        (count,) = struct.unpack(">H", bytes(self._buf[2:4]))
        # Walk the rectangles first: nothing is consumed (and the zlib stream
        # is not advanced) until the whole update is here.
        at = 4
        rects = []
        for _ in range(count):
            if not self._need(at + 12):
                return False
            x, y, w, h, enc = struct.unpack(">HHHHi", bytes(self._buf[at:at + 12]))
            at += 12
            if enc == ENCODING_RAW:
                size = w * h * 4
            elif enc == ENCODING_ZLIB:
                if not self._need(at + 4):
                    return False
                (size,) = struct.unpack(">I", bytes(self._buf[at:at + 4]))
                at += 4
            elif enc == ENCODING_DESKTOP_SIZE:
                size = 0
            else:
                raise RfbError(f"encoding {enc} was not offered")
            if not self._need(at + size):
                return False
            rects.append((x, y, w, h, enc, at, size))
            at += size
        message = self._take(at)
        resized = False
        for x, y, w, h, enc, start, size in rects:
            if enc == ENCODING_DESKTOP_SIZE:
                self._resize(w, h)
                resized = True
                continue
            data = message[start:start + size]
            if enc == ENCODING_ZLIB:
                data = self._zlib.decompress(data)
            if len(data) != w * h * 4 or x + w > self.width or y + h > self.height:
                raise RfbError("a rectangle does not fit the framebuffer")
            row = self.width * 4
            for line in range(h):
                at_fb = (y + line) * row + x * 4
                self.fb[at_fb:at_fb + w * 4] = data[line * w * 4:(line + 1) * w * 4]
        self.updates += 1
        self.request(not resized)
        return True


# --------------------------------------------------------------------------
# A websocket client (RFC 6455), enough for one console socket
# --------------------------------------------------------------------------

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def encode_frame(opcode: int, payload: bytes, mask: bytes | None = None) -> bytes:
    """One client frame: FIN set, masked (a client must mask, RFC 6455 5.3)."""
    mask = mask if mask is not None else secrets.token_bytes(4)
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    key = (mask * (n // 4 + 1))[:n]
    body = (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big") if n else b""
    return head + mask + body


class FrameReader:
    """Server frames from a byte stream, fragments joined. `next()` returns
    `(opcode, payload)` or None until a whole message is buffered."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._parts: list[bytes] = []
        self._opcode: int | None = None

    def feed(self, data: bytes) -> None:
        self._buf += data

    def next(self) -> tuple[int, bytes] | None:
        while True:
            if len(self._buf) < 2:
                return None
            b0, b1 = self._buf[0], self._buf[1]
            fin, opcode, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            at = 2
            if n == 126:
                if len(self._buf) < 4:
                    return None
                (n,) = struct.unpack(">H", bytes(self._buf[2:4]))
                at = 4
            elif n == 127:
                if len(self._buf) < 10:
                    return None
                (n,) = struct.unpack(">Q", bytes(self._buf[2:10]))
                at = 10
            key = b""
            if masked:
                key = bytes(self._buf[at:at + 4])
                at += 4
            if len(self._buf) < at + n:
                return None
            payload = bytes(self._buf[at:at + n])
            del self._buf[:at + n]
            if masked and n:
                stream = (key * (n // 4 + 1))[:n]
                payload = (int.from_bytes(payload, "big") ^ int.from_bytes(stream, "big")).to_bytes(n, "big")
            if opcode >= 0x8:  # control frames are never fragmented
                return opcode, payload
            if opcode != OP_CONT:
                self._opcode, self._parts = opcode, []
            self._parts.append(payload)
            if fin:
                whole = b"".join(self._parts)
                op, self._opcode, self._parts = self._opcode, None, []
                return (op if op is not None else OP_BINARY), whole


class WebSocket:
    def __init__(self, url: str, headers: dict[str, str], timeout: float = 30.0) -> None:
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if secure else raw
        key = base64.b64encode(secrets.token_bytes(16))
        lines = [f"GET {parts.path or '/'} HTTP/1.1", f"Host: {parts.netloc}",
                 "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key.decode()}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("the server closed the connection during the upgrade")
            response += chunk
            if len(response) > 65536:
                raise ConnectionError("no end to the upgrade response")
        head, _, rest = response.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError(f"the upgrade was answered {status.decode('latin-1')[:80]}")
        accept = base64.b64encode(hashlib.sha1(key + _WS_GUID).digest())
        if accept.lower() not in head.lower():
            raise ConnectionError("the upgrade's Sec-WebSocket-Accept is wrong")
        self.reader = FrameReader()
        self.reader.feed(rest)
        self.close_code: int | None = None
        self.closed = False

    def send(self, opcode: int, payload: bytes) -> None:
        self.sock.sendall(encode_frame(opcode, payload))

    def recv(self, timeout: float) -> tuple[int, bytes] | None:
        """The next text or binary message, None on a timeout; sets
        `closed` and `close_code` when the server closes."""
        deadline = time.monotonic() + timeout
        while not self.closed:
            message = self.reader.next()
            if message is not None:
                opcode, payload = message
                if opcode == OP_PING:
                    self.send(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode == OP_CLOSE:
                    self.close_code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                    self.closed = True
                    try:
                        self.send(OP_CLOSE, payload[:2])
                    except OSError:
                        pass
                    return None
                return opcode, payload
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(1 << 16)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                self.closed = True
                if self.close_code is None:
                    self.close_code = 1006
                return None
            self.reader.feed(chunk)
        return None

    def close(self) -> None:
        if not self.closed:
            try:
                self.send(OP_CLOSE, struct.pack(">H", 1000))
            except OSError:
                pass
            self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# swarm-api and GCS
# --------------------------------------------------------------------------


class Api:
    def __init__(self, base: str, credential: str, tenant: str) -> None:
        self.base = base.rstrip("/")
        self._credential = credential
        self.tenant = tenant

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._credential}"}

    def call(self, method: str, path: str, body: Any = None, *,
             tenant: str | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        for k, v in self.auth_header().items():
            request.add_header(k, v)
        request.add_header("X-Swarm-Tenant", tenant or self.tenant)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            return 0, {"error": {"code": type(exc).__name__}}
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, {"text": raw[:200].decode("utf-8", "replace")}


class Gcs:
    BASE = "https://storage.googleapis.com/storage/v1/b/"

    def __init__(self, bucket: str, token: str) -> None:
        self.bucket = bucket
        self._token = token

    def _get(self, url: str, limit: int | None = None) -> bytes:
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(request, timeout=120) as response:
            if limit is None:
                return response.read()
            data = response.read(limit + 1)
            if len(data) > limit:
                raise OverflowError("object larger than the sweep reads")
            return data

    def list(self, prefix: str) -> list[dict]:
        items: list[dict] = []
        token = None
        while True:
            query = {"prefix": prefix, "fields": "items(name,size),nextPageToken"}
            if token:
                query["pageToken"] = token
            url = self.BASE + urllib.parse.quote(self.bucket, safe="") + "/o?" + urllib.parse.urlencode(query)
            page = json.loads(self._get(url))
            items += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return items

    def read(self, name: str) -> bytes:
        url = (self.BASE + urllib.parse.quote(self.bucket, safe="") + "/o/"
               + urllib.parse.quote(name, safe="") + "?alt=media")
        return self._get(url, MAX_OBJECT_BYTES)


def gunzip_all(data: bytes) -> bytes:
    """Every gzip member of `data`, decompressed (a checkpoint archive)."""
    out = []
    while data:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out.append(d.decompress(data))
        out.append(d.flush())
        data = d.unused_data
        if not d.eof:
            break
    return b"".join(out)


def contains(data: bytes, needle: bytes) -> bool:
    """`needle` in `data`, or in it once decompressed when it is gzip."""
    if needle in data:
        return True
    if data[:2] == b"\x1f\x8b":
        try:
            return needle in gunzip_all(data)
        except zlib.error:
            return False
    return False


def category(name: str) -> str:
    """Which of the row's outputs an object under the task's prefix is."""
    if re.search(r"/logs/(live/)?agent_stdout(\.tail)?\.log$", name):
        return "transcript"
    if "/logs/" in name:
        return "logs"
    if "/checkpoints/" in name:
        return "checkpoints"
    if "/live/" in name:
        return "recording"
    return "outputs"


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


@dataclass
class Config:
    task: str
    tenant: str
    api: str
    bucket: str
    control_dir: str
    results: str
    timeout: float
    other_tenant: str | None
    credential: str
    gcs_token: str | None


class Results:
    def __init__(self, path: str) -> None:
        self.path = path
        self.written: dict[str, str] = {}

    def put(self, check: str, verdict: str, reason: str) -> None:
        if check in self.written:
            return
        self.written[check] = verdict
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"check": check, "verdict": verdict, "reason": reason}) + "\n")
        log(f"{verdict} {check}: {reason}")

    def rest(self, verdict: str, reason: str) -> None:
        for check in CHECKS:
            self.put(check, verdict, reason)


def settled(cfg: Config) -> str | None:
    try:
        with open(os.path.join(cfg.control_dir, "settled"), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def wait_settled(cfg: Config, seconds: float) -> str | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = settled(cfg)
        if state is not None:
            return state
        time.sleep(2)
    return settled(cfg)


def not_run_reason(state: str) -> tuple[str, str]:
    """A state `acc_settle` stopped on without the task ending: SKIP when the
    platform holds it on purpose (the suite's own rule, lib.sh), FAIL else."""
    if state == "PARKED:CREDENTIAL_MISSING":
        return "SKIP", ("not measured: the caller's tenant holds no credential for this profile's "
                        "provider (PARKED on CREDENTIAL_MISSING)")
    if state.startswith("HELD:"):
        return "SKIP", (f"not measured: admission held the task without leasing it ({state[5:]}) "
                        "-- a closed pool or a limit, not a result")
    # The group writes `<state>[:<park_reason>[:eligible again at <when>]]`
    # (live_browser_stuck in live-browser.sh), so the verdict says which park.
    name, _, why = state.partition(":")
    park_reason, _, when = why.partition(":")
    if name == "PARKED" and park_reason == "HUMAN_REQUIRED":
        # swarm_api.handoff: the hold ended with nobody in control (unwatched
        # for human_unwatched_seconds, or the gate's channel dropped), the
        # worker parked, and only a person's "I'm ready" promotes it.
        return "FAIL", ("the task was PARKED on HUMAN_REQUIRED when the wait ended: its hand-off hold "
                        "ran out with nobody in control and the worker parked it, which only a "
                        "person's \"I'm ready\" undoes")
    detail = f"park_reason {park_reason}" if park_reason else "no park_reason"
    if when:
        detail += f", {when}"
    if name != "PARKED":
        detail = "not parked"
    return "FAIL", f"the task was still {name} when the wait ended ({detail})"


class Viewer:
    """The scripted person: one console socket, one RFB stream."""

    def __init__(self, api: Api, ws: WebSocket) -> None:
        self.api = api
        self.ws = ws
        self.rfb = RfbClient()
        self.in_control = False
        self.status: dict | None = None
        self.on_update: Callable[[RfbClient], None] | None = None

    def flush(self) -> None:
        for message in self.rfb.take_outbound():
            self.ws.send(OP_BINARY, message)

    def pump(self, seconds: float, until: Callable[[], bool] | None = None) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if until is not None and until():
                return True
            left = deadline - time.monotonic()
            if left <= 0 or self.ws.closed:
                return until is not None and until()
            message = self.ws.recv(min(left, 1.0))
            if message is None:
                continue
            opcode, payload = message
            if opcode == OP_BINARY:
                if self.rfb.feed(payload) and self.on_update is not None:
                    self.on_update(self.rfb)
                self.flush()
            elif opcode == OP_TEXT:
                try:
                    status = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(status, dict) and status.get("type") == "status":
                    self.status = status
                    self.in_control = bool(status.get("in_control"))

    def judge_now(self) -> Judgement:
        return judge(bytes(self.rfb.fb), self.rfb.width, self.rfb.height)


def socket_url(base: str, path: str) -> str:
    parts = urllib.parse.urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urllib.parse.urlunsplit((scheme, parts.netloc, path, "", ""))


def repoint(ticket: str, tenant: str) -> str:
    """The same ticket with its claimed tenant changed and its MAC kept."""
    payload, _, mac = ticket.partition(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["tenant_id"] = tenant
    forged = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=")
    return forged.decode() + "." + mac


def other_tenant(api: Api, cfg: Config) -> str | None:
    if cfg.other_tenant:
        return cfg.other_tenant
    status, mine = api.call("GET", "/v1/tenants/mine")
    if status != 200 or not isinstance(mine, list):
        return None
    for entry in mine:
        tenant = entry.get("tenant_id") if isinstance(entry, dict) else None
        if isinstance(tenant, str) and tenant and tenant != cfg.tenant:
            return tenant
    return None


def check_isolation(api: Api, cfg: Config, session: dict, results: Results) -> None:
    other = other_tenant(api, cfg)
    if other is None:
        results.put("isolation", "SKIP", f"not measured: this identity is a member of no tenant but "
                    f"{cfg.tenant} (GET /v1/tenants/mine), so no other tenant's member can ask; "
                    "set SWARM_ACCEPTANCE_OTHER_TENANT")
        return
    t = cfg.task
    probes = [("GET", f"/v1/tasks/{t}"), ("GET", f"/v1/tasks/{t}/live/session"),
              ("POST", f"/v1/tasks/{t}/live/ticket"), ("POST", f"/v1/tasks/{t}/live/control"),
              ("GET", f"/v1/tasks/{t}/live/audit")]
    wrong = []
    for method, path in probes:
        status, _ = api.call(method, path, {} if method == "POST" else None, tenant=other)
        if status != 404:
            wrong.append(f"{method} {path.replace(t, '<task>')} answered {status}")
    if wrong:
        results.put("isolation", "FAIL", f"as a member of {other}: " + "; ".join(wrong))
        return
    status, minted = api.call("POST", f"/v1/tasks/{t}/live/ticket", {})
    if status != 200 or not isinstance(minted, dict) or not minted.get("ticket"):
        results.put("isolation", "FAIL", f"could not mint the {cfg.tenant} viewer ticket the socket probe "
                    f"re-points (HTTP {status})")
        return
    code = None
    try:
        ws = WebSocket(socket_url(api.base, f"/v1/live/{session['session_id']}/stream"), api.auth_header())
        ws.send(OP_TEXT, json.dumps({"ticket": repoint(minted["ticket"], other)}).encode())
        deadline = time.monotonic() + 20
        while not ws.closed and time.monotonic() < deadline:
            ws.recv(2.0)
        code = ws.close_code
        ws.close()
    except (OSError, ConnectionError) as exc:
        results.put("isolation", "FAIL", f"the socket probe could not connect ({type(exc).__name__})")
        return
    if code != CLOSE_REFUSED:
        results.put("isolation", "FAIL", f"a viewer ticket re-pointed at {other} was answered with close code "
                    f"{code}, not {CLOSE_REFUSED}")
        return
    results.put("isolation", "PASS", f"as a member of {other}, all {len(probes)} task and live routes "
                f"answered 404, and a ticket re-pointed at {other} was refused on the socket ({code})")


def drive_viewer(api: Api, cfg: Config, session: dict, password: str, results: Results,
                 while_watching: Callable[[], None] | None = None) -> FrameTally | None:
    """Watch, take control, click the field, type, submit, return control.
    `while_watching` runs once the page is on screen, before control is
    taken, with this viewer's socket open (so the hold is held)."""
    t = cfg.task
    status, minted = api.call("POST", f"/v1/tasks/{t}/live/ticket", {})
    if status != 200 or not isinstance(minted, dict) or not minted.get("ticket"):
        results.put("frames", "FAIL", f"POST /live/ticket answered {status}")
        return None
    path = minted.get("socket_path") or f"/v1/live/{minted.get('session_id')}/stream"
    if not str(path).startswith("/v1/live/") or "?" in str(path):
        results.put("frames", "FAIL", "the ticket's socket_path is not a bare /v1/live/ path")
        return None
    try:
        ws = WebSocket(socket_url(api.base, str(path)), api.auth_header())
    except (OSError, ConnectionError) as exc:
        results.put("frames", "FAIL", f"the console socket did not open ({exc})")
        return None
    viewer = Viewer(api, ws)
    watching, typing = FrameTally(), FrameTally()
    phase = {"tally": watching}
    viewer.on_update = lambda rfb: phase["tally"].add(
        judge(bytes(rfb.fb), rfb.width, rfb.height))
    try:
        ws.send(OP_TEXT, json.dumps({"ticket": minted["ticket"]}).encode())
        if not viewer.pump(90, lambda: viewer.rfb.updates > 0 and watching.painted > 0):
            results.put("frames", "FAIL", f"no frame showed the fixture page with its field painted within "
                        f"90 s ({viewer.rfb.updates} updates; {watching.no_page} without the page; leaks "
                        f"{watching.leaks or 'none'}; socket close code {ws.close_code})")
            return watching
        if while_watching is not None:
            while_watching()
            viewer.pump(1)  # what arrived meanwhile
        status, taken = api.call("POST", f"/v1/tasks/{t}/live/control", {})
        if status != 200:
            results.put("frames", "FAIL", f"take control answered {status}: "
                        f"{(taken or {}).get('error', {}).get('code') if isinstance(taken, dict) else taken}")
            return watching
        if not viewer.pump(30, lambda: viewer.in_control):
            results.put("frames", "FAIL", "the socket never said this viewer is in control")
            return watching
        viewer.pump(2)  # the relay passes control on to the gate
        target = watching.field
        assert target is not None
        x, y = target.centre()
        viewer.rfb.pointer(x, y, 0)
        viewer.rfb.pointer(x, y, 1)
        viewer.rfb.pointer(x, y, 0)
        viewer.flush()
        viewer.pump(1)
        phase["tally"] = typing
        for keysym in keysyms(password):
            viewer.rfb.key(keysym, True)
            viewer.rfb.key(keysym, False)
        viewer.flush()
        viewer.pump(4)
        if typing.judged == 0:
            # A static page sends no update: ask for a whole frame and judge it.
            viewer.rfb.request(False)
            viewer.flush()
            viewer.pump(10, lambda: typing.judged > 0)
        phase["tally"] = watching
        # The console renews every few seconds; control lapses at 60 s without it.
        api.call("POST", f"/v1/tasks/{t}/live/control/renew", {})
        viewer.rfb.key(XK_RETURN, True)
        viewer.rfb.key(XK_RETURN, False)
        viewer.flush()
        changed = viewer.pump(15, lambda: viewer.judge_now().kind == "no_page")
        status, _ = api.call("POST", f"/v1/tasks/{t}/live/control/return", {})
        returned = status == 200
        viewer.pump(5, lambda: not viewer.in_control)
    finally:
        ws.close()
    leaks = watching.leaks + typing.leaks
    if leaks:
        results.put("frames", "FAIL", "a frame showed the password field unpainted: " + "; ".join(leaks[:3]))
    elif typing.judged == 0:
        results.put("frames", "FAIL", "no frame showing the page arrived while the person typed")
    elif not returned:
        results.put("frames", "FAIL", f"return control answered {status}")
    else:
        f = typing.field
        results.put("frames", "PASS", f"{typing.judged} frame(s) while the person typed and {watching.judged} "
                    f"otherwise showed the page; in every one the field's box ({f.x0},{f.y0}-{f.x1},{f.y1}) "
                    f"was the mask's #222222 and no field-coloured pixel showed"
                    + ("" if changed else "; the page did not visibly change after Enter"))
    typing.field = typing.field or watching.field
    typing.marker = typing.marker or watching.marker
    return typing


def all_events(api: Api, task: str) -> tuple[list[dict], int]:
    """Every event of the task, every page; and the last status read."""
    events: list[dict] = []
    token = None
    for _ in range(50):
        path = f"/v1/tasks/{task}/events?limit=200" + (
            f"&page_token={urllib.parse.quote(token)}" if token else "")
        status, page = api.call("GET", path)
        if status != 200 or not isinstance(page, dict):
            return events, status
        events += [e for e in page.get("events", []) if isinstance(e, dict)]
        token = page.get("next_page_token")
        if not token:
            break
    return events, 200


def check_returned(api: Api, cfg: Config, state: str, code: str, outputs: dict[str, bytes],
                   results: Results) -> None:
    t = cfg.task
    problems = []
    if state != "SUCCEEDED":
        problems.append(f"the task ended {state}")
    events, status = all_events(api, t)
    if status != 200:
        problems.append(f"GET /events answered {status}")
    types = {e.get("type") for e in events}
    missing = [e for e in HANDOFF_EVENTS if e not in types]
    if missing:
        problems.append("no " + ", ".join(missing) + " event")
    status, answer = api.call("GET", f"/v1/tasks/{t}/answer")
    content = (answer or {}).get("content") if isinstance(answer, dict) else None
    if status != 200 or not isinstance(content, str) or code not in content:
        problems.append(f"the agent's answer does not carry the session code the typed password makes "
                        f"(HTTP {status}, {len(content or '')} chars)")
    written = [v for k, v in outputs.items() if k.endswith("/artifacts/" + CODE_OUTPUT)]
    if not written or written[-1].decode("utf-8", "replace").strip() != code:
        problems.append(f"its {CODE_OUTPUT} output does not hold the code")
    if problems:
        results.put("returned", "FAIL", "; ".join(problems))
    else:
        results.put("returned", "PASS", f"control returned, the agent read session code {code} (which only "
                    "the typed password produces) into its answer and its output, the task SUCCEEDED, "
                    "and " + ", ".join(HANDOFF_EVENTS) + " were recorded")


def ffmpeg_frames(path: str) -> tuple[int, int, Any] | None:
    """(width, height, iterator of rgb24 frames at 5 fps) for a segment, or None."""
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-i", path], capture_output=True, text=True,
                           check=False, timeout=60)
    found = re.search(r"Video: vp8.*?(\d{2,5})x(\d{2,5})", probe.stderr)
    if not found:
        return None
    width, height = int(found.group(1)), int(found.group(2))
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", "fps=5", "-f", "rawvideo",
                             "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)

    def frames():
        size = width * height * 3
        try:
            while True:
                chunk = proc.stdout.read(size)
                if len(chunk) < size:
                    return
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait(timeout=60)

    return width, height, frames()


def stray_live_objects(prefix: str, names: list[str]) -> list[str]:
    """Objects in an attempt's own `live/` directory that are neither a
    recording segment nor its actions file. Only `<prefix><attempt>/live/`:
    the worker's live log tails sit under `<attempt>/logs/live/`
    (agent_worker/runners/streams.py) and are not the recording's. Matching
    any `/live/` counted those two tails as strays and failed `recording` on
    every run that streamed its logs (lane SPEED-C2, 2026-10-14)."""
    own = re.compile(re.escape(prefix) + r"[A-Za-z0-9_-]+/live/(?P<rest>.+)")
    keep = re.compile(r"(rec-[0-9]{13}\.live\.webm|actions-[0-9]{13}\.live\.jsonl)")
    stray = []
    for name in names:
        hit = own.fullmatch(name)
        if hit and not keep.fullmatch(hit.group("rest")):
            stray.append(name)
    return stray


def check_recording(cfg: Config, gcs: Gcs | None, tally: FrameTally | None,
                    results: Results) -> None:
    prefix = f"tenants/{cfg.tenant}/tasks/{cfg.task}/attempts/"
    if gcs is None:
        results.put("recording", "FAIL", "no access token for the artifact bucket: the recording was not read")
        return
    segments: list[str] = []
    deadline = time.monotonic() + 150
    while True:
        try:
            names = [i["name"] for i in gcs.list(prefix)]
        except (urllib.error.URLError, OSError, ValueError) as exc:
            results.put("recording", "FAIL", f"listing {prefix} failed ({type(exc).__name__})")
            return
        segments = sorted(n for n in names if re.fullmatch(
            re.escape(prefix) + r"[A-Za-z0-9_-]+/live/rec-[0-9]{13}\.live\.webm", n))
        if segments or time.monotonic() > deadline:
            break
        time.sleep(10)
    stray = stray_live_objects(prefix, names)
    if stray:
        results.put("recording", "FAIL", f"{len(stray)} object(s) under live/ are not recording segments")
        return
    if not segments:
        results.put("recording", "FAIL", f"no recording segment under {prefix}*/live/ 150 s after the end")
        return
    if tally is None or tally.field is None:
        results.put("recording", "SKIP", f"{len(segments)} segment(s) recorded; not judged: the live frames "
                    "never located the field to judge them at")
        return
    if shutil.which("ffmpeg") is None:
        results.put("recording", "SKIP", f"{len(segments)} VP8 segment(s) recorded under {prefix}*/live/; their "
                    "frames were not decoded: no ffmpeg here (the swarm-verify image carries none). The live "
                    "frames, the same masked stream the relay records, are judged by the frames check")
        return
    seen = FrameTally()
    with tempfile.TemporaryDirectory() as scratch:
        for name in segments:
            local = os.path.join(scratch, "segment.webm")
            with open(local, "wb") as fh:
                fh.write(gcs.read(name))
            decoded = ffmpeg_frames(local)
            if decoded is None:
                results.put("recording", "FAIL", f"{name.rsplit('/', 1)[-1]} is not a VP8 video ffmpeg can read")
                return
            width, height, frames = decoded
            for pixels in frames:
                box = tally.field if tally.field.within(width, height) else None
                seen.add(judge(pixels, width, height, bpp=3, tol=24, field=box))
    if seen.leaks:
        results.put("recording", "FAIL", "a recorded frame shows the field unpainted: " + "; ".join(seen.leaks[:3]))
    elif seen.judged == 0:
        results.put("recording", "FAIL", f"no recorded frame of {len(segments)} segment(s) shows the page")
    else:
        results.put("recording", "PASS", f"{seen.judged} recorded frame(s) of {len(segments)} segment(s) show "
                    "the page, and in every one the field's box is the mask's paint")


def sweep(api: Api, cfg: Config, gcs: Gcs | None, password: str, results: Results) -> dict[str, bytes]:
    """The no_leak check. Returns the outputs it read (the returned check
    reads the agent's code file from them)."""
    outputs: dict[str, bytes] = {}
    needle = password.encode("utf-8")
    if gcs is None:
        results.put("no_leak", "FAIL", "no access token for the artifact bucket: nothing was swept")
        return outputs
    prefix = f"tenants/{cfg.tenant}/tasks/{cfg.task}/"
    try:
        items = gcs.list(prefix)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        results.put("no_leak", "FAIL", f"listing {prefix} failed ({type(exc).__name__})")
        return outputs
    counts = {c: 0 for c in ("transcript", "logs", "checkpoints", "outputs", "recording")}
    found, unread = [], []
    for item in items:
        name = item["name"]
        try:
            data = gcs.read(name)
        except (urllib.error.URLError, OSError, OverflowError) as exc:
            unread.append(f"{name[len(prefix):]} ({type(exc).__name__})")
            continue
        kind = category(name)
        counts[kind] += 1
        if kind == "outputs":
            outputs[name] = data
        if contains(data, needle):
            found.append(name[len(prefix):])
    t = cfg.task
    documents = 0
    for path in (f"/v1/tasks/{t}", f"/v1/tasks/{t}/live/audit", f"/v1/tasks/{t}/live/session",
                 f"/v1/tasks/{t}/answer"):
        status, body = api.call("GET", path)
        if status != 200:
            unread.append(f"GET {path.replace(t, '<task>')} ({status})")
            continue
        documents += 1
        if password in json.dumps(body):
            found.append(f"GET {path.replace(t, '<task>')}")
    events, status = all_events(api, t)
    if status != 200:
        unread.append(f"GET /v1/tasks/<task>/events ({status})")
    elif password in json.dumps(events):
        found.append(f"one of the task's {len(events)} events")
    empty = [c for c in ("transcript", "logs", "checkpoints", "outputs") if counts[c] == 0]
    if found:
        results.put("no_leak", "FAIL", "the password is in: " + ", ".join(found[:10]))
    elif unread:
        results.put("no_leak", "FAIL", "not read, so not known clean: " + ", ".join(unread[:10]))
    elif empty:
        results.put("no_leak", "FAIL", "the sweep found no " + ", ".join(empty) + " to read under "
                    f"{prefix}: an unread category is not a clean one")
    else:
        results.put("no_leak", "PASS", f"the {PASSWORD_LENGTH}-character password made at run time is in "
                    f"none of {len(items)} objects (transcript {counts['transcript']}, logs {counts['logs']}, "
                    f"checkpoints {counts['checkpoints']}, outputs {counts['outputs']}, recording "
                    f"{counts['recording']}; gzip read through) nor in {documents} API documents (the task, "
                    f"its live audit and session, its answer) or its {len(events)} events")
    return outputs


def wait_for_handoff(api: Api, cfg: Config) -> tuple[dict | None, str | None]:
    """(the live session view, None) once the agent has asked, or (None, the
    state the task settled on) when it ended or stopped being waited on first."""
    deadline = time.monotonic() + cfg.timeout
    while time.monotonic() < deadline:
        state = settled(cfg)
        status, view = api.call("GET", f"/v1/tasks/{cfg.task}/live/session")
        # The console's wire (LB-SEAM, tests/fixtures/live): the session sits
        # under `session`, null until the tunnel is up.
        session = view.get("session") if status == 200 and isinstance(view, dict) else None
        if isinstance(session, dict) and session.get("state") == "open" and session.get("human_wait"):
            return session, None
        if state is not None:
            return None, state
        time.sleep(3)
    return None, settled(cfg) or "RUNNING"


def run(cfg: Config) -> None:
    results = Results(cfg.results)
    api = Api(cfg.api, cfg.credential, cfg.tenant)
    gcs = Gcs(cfg.bucket, cfg.gcs_token) if cfg.gcs_token else None
    password = new_password()
    code = session_code(password)
    try:
        session, state = wait_for_handoff(api, cfg)
        if session is None:
            assert state is not None
            if state not in TERMINAL:
                verdict, reason = not_run_reason(state)
                results.put("handoff", verdict, reason)
                results.rest("SKIP", "not measured: the task never asked for help (see the hand-off check)")
                return
            status, answer = api.call("GET", f"/v1/tasks/{cfg.task}/answer")
            content = (answer or {}).get("content") or "" if isinstance(answer, dict) else ""
            refused = re.search(REFUSED_PREFIX + r"\s+([a-z_]+)", content)
            if refused and refused.group(1) == "no_live_path":
                results.rest("SKIP", "not measured: this deployment ran the task with no live path "
                             "(swarm-handoff refused: no_live_path; enable_live_browser or "
                             "LIVE_BROWSER_SIDECAR is off)")
                return
            why = f"swarm-handoff refused: {refused.group(1)}" if refused else "it did not ask"
            results.put("handoff", "FAIL", f"the task ended {state} without a live hand-off ({why})")
            results.rest("SKIP", "not measured: the task never asked for help (see the hand-off check)")
            return
        wait = session["human_wait"]
        results.put("handoff", "PASS", f"the agent asked for help ({wait.get('reason')!r}); session "
                    f"{session.get('session_id')} is open and this submitter may take control: "
                    f"{bool((session.get('you') or {}).get('can_control'))}")
        # The viewer joins first and the probes run while it watches: the
        # socket's join is what swarm-api counts as someone watching
        # (LiveService.watched), and an unwatched hold ends after 60 s
        # (human_unwatched_seconds). Probed first, the minute went on six API
        # calls and a socket that may wait 20 s, and the task parked on
        # HUMAN_REQUIRED (LIVEBROWSER-SCHED, #1333).
        probed: list[bool] = []

        def probe() -> None:
            probed.append(True)
            check_isolation(api, cfg, session, results)

        tally = drive_viewer(api, cfg, session, password, results, while_watching=probe)
        if not probed:  # the viewer stopped before the page was on screen
            check_isolation(api, cfg, session, results)
        state = wait_settled(cfg, cfg.timeout + 120)
        if state is None or state not in TERMINAL:
            verdict, reason = not_run_reason(state or "RUNNING")
            results.put("returned", verdict, reason)
        outputs = sweep(api, cfg, gcs, password, results)
        check_recording(cfg, gcs, tally, results)
        if state in TERMINAL:
            check_returned(api, cfg, state, code, outputs, results)
    except Exception as exc:  # a crash must still name every unjudged check
        results.rest("FAIL", f"the check crashed ({type(exc).__name__}) before judging this")
        raise
    finally:
        results.rest("FAIL", "not judged")


def test_a_settle_that_stopped_short_is_judged_by_its_state_and_park_reason() -> None:
    """pytest's entry (as test_caller_mcp.py's), offline and standard library
    only: what the group writes when its wait stops short becomes a SKIP only
    when the platform holds the task, and every FAIL names the state and the
    park, never just "did not finish in time" (#1333). The full matrix is in
    tests/unit/scripts/test_live_browser_acceptance.py."""
    assert not_run_reason("PARKED:CREDENTIAL_MISSING")[0] == "SKIP"
    assert not_run_reason("HELD:QUEUED:pool-gke MANUAL_PAUSE limit 0")[0] == "SKIP"
    for state, words in (
        ("PARKED:HUMAN_REQUIRED", ("PARKED", "HUMAN_REQUIRED", "nobody in control")),
        ("PARKED:PROVIDER_COOLDOWN:eligible again at 2026-10-19T05:40:00Z",
         ("park_reason PROVIDER_COOLDOWN", "eligible again at 2026-10-19T05:40:00Z")),
        ("PARKED", ("PARKED", "no park_reason")),
        ("RUNNING", ("still RUNNING", "not parked")),
    ):
        verdict, reason = not_run_reason(state)
        assert verdict == "FAIL", state
        assert "did not finish in time" not in reason, reason
        assert all(word in reason for word in words), (state, reason)


def _read_once(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return value or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--api", required=True, help="swarm-api's base URL")
    parser.add_argument("--bucket", required=True, help="the artifact bucket")
    parser.add_argument("--control-dir", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--credential-file", required=True, help="read once and deleted")
    parser.add_argument("--gcs-token-file", help="read once and deleted")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--other-tenant")
    args = parser.parse_args(argv)
    credential = _read_once(args.credential_file)
    gcs_token = _read_once(args.gcs_token_file)
    cfg = Config(task=args.task, tenant=args.tenant, api=args.api, bucket=args.bucket,
                 control_dir=args.control_dir, results=args.results, timeout=args.timeout,
                 other_tenant=args.other_tenant or None, credential=credential or "",
                 gcs_token=gcs_token)
    if not credential:
        Results(args.results).rest("FAIL", "no API credential was handed over")
        return 1
    run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
