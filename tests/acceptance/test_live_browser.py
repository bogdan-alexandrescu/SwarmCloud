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
             the field's box in the field's own magenta, as Chromium drew it:
             nothing is painted over it (BGD, 2026-10-25; design §5.3).
  recording  the relay's recording of the session: segments are written under
             the task's attempt `live/` key, and their frames, decoded, show the
             field the same way.
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

`--qa-identity` judges a second task the same way (QI-ACCEPT,
docs/design/qa-identity.md §6, part of #1649): a `claude-code-browser`
agent whose input names `qa_identity: "console"` lands on the console's
Overview signed in as the QA identity, its hand-off on the console host is
refused `qa_identity_host`, the same submission from an ungranted tenant is
refused `qa_identity_not_granted`, and no credential's shape is in anything
the task left. `qa-identity-submission <console-url>` prints that task's
POST /v1/tasks body. Its checks are QA_IDENTITY_CHECKS.

The results file gets one JSON line per check, in CHECKS order:
`{"check": key, "verdict": "PASS"|"FAIL"|"SKIP", "reason": "..."}`.
"""

from __future__ import annotations

import argparse
import base64
import functools
import gzip
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
#: What the gate's mask painted a sensitive box with until LIVE-NOMASK
#: (#2298): a frame whose field box is this colour was painted over.
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
    """What one frame shows. `kind`: `no_page` (the fixture is not on screen),
    `shown` (the field's box is the field as drawn), or `covered`."""

    kind: str
    marker: Box | None = None
    field: Box | None = None
    shown: float = 0.0
    painted: float = 0.0

    def describe(self) -> str:
        if self.kind == "no_page":
            return "the fixture's marker is not on screen"
        return (f"field box {self.field.x0},{self.field.y0}-{self.field.x1},{self.field.y1}: "
                f"{self.shown:.0%} field colour, {self.painted:.0%} mask paint")


def judge(pixels: bytes, width: int, height: int, *, bpp: int = 4, tol: int = 0,
          field: Box | None = None) -> Judgement:
    """Is the password field shown as Chromium drew it in this frame?

    login.html draws the field, its dots and its caret all in its magenta, so
    a field shown as drawn is magenta across its box, typed into or not, and
    one painted over is not (BGD, 2026-10-25: the live view paints nothing).
    `tol` 0 is for the RFB frames, which carry Chromium's pixels exactly; the
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
    shown = colour_map(region, bpp, FIELD_RGB, max(tol, 0x20)).count(b"\x01") / max(1, inner.area)
    painted = colour_map(region, bpp, MASK_RGB, max(tol, 0x10)).count(b"\x01") / max(1, inner.area)
    # Not every pixel: this viewer lists no Cursor encoding, so the gate paints
    # the pointer's arrow on its copy (rfb.py, THE POINTER), and after the
    # click the arrow sits on the field -- a few hundred pixels of its ~11,000.
    if tol == 0:
        ok = shown >= 0.95 and painted == 0.0
    else:
        ok = shown >= 0.9 and painted <= 0.05
    return Judgement("shown" if ok else "covered", marker, box, shown, painted)


@dataclass
class FrameTally:
    """Every judgement of one phase of the session."""

    judged: int = 0
    shown: int = 0
    no_page: int = 0
    covers: list[str] = field(default_factory=list)
    field: Box | None = None
    marker: Box | None = None

    def add(self, verdict: Judgement) -> None:
        if verdict.kind == "no_page":
            self.no_page += 1
            return
        self.judged += 1
        if verdict.kind == "shown":
            self.shown += 1
            self.field, self.marker = verdict.field, verdict.marker
        elif len(self.covers) < 5:
            self.covers.append(verdict.describe())
        else:
            self.covers.append("...")


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
    console_url: str | None = None


class Results:
    def __init__(self, path: str, checks: tuple[str, ...] = CHECKS) -> None:
        self.path = path
        self.checks = checks
        self.written: dict[str, str] = {}

    def put(self, check: str, verdict: str, reason: str) -> None:
        if check in self.written:
            return
        self.written[check] = verdict
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"check": check, "verdict": verdict, "reason": reason}) + "\n")
        log(f"{verdict} {check}: {reason}")

    def rest(self, verdict: str, reason: str) -> None:
        for check in self.checks:
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


#: The longest the probes run beside the viewer before it goes on without
#: them: six API calls of 30 s each at worst, and a socket held 20 s.
PROBE_SECONDS = 240.0


def watch_while(viewer: Viewer, work: Callable[[], None], seconds: float = PROBE_SECONDS) -> None:
    """Run `work` on a thread while this viewer goes on reading its socket.

    A person with the view open keeps receiving it. Called inline, the probes
    left the socket unread for as long as they took (six API calls and a
    socket that may wait 20 s): no frame request was answered, nothing was
    read off the wire, and the viewer swarm-api counts as watching was, for
    that time, a connection nobody served (LIVEBROWSER-WATCH, #1333). What
    `work` raises is raised here, after the viewer has read what came."""
    raised: list[BaseException] = []

    def body() -> None:
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 -- re-raised on this thread
            raised.append(exc)

    thread = threading.Thread(target=body, name="live-browser-probe", daemon=True)
    thread.start()
    viewer.pump(seconds, lambda: not thread.is_alive())
    thread.join(seconds)
    if raised:
        raise raised[0]


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
        if not viewer.pump(90, lambda: viewer.rfb.updates > 0 and watching.shown > 0):
            results.put("frames", "FAIL", f"no frame showed the fixture page with its field as drawn within "
                        f"90 s ({viewer.rfb.updates} updates; {watching.no_page} without the page; covered "
                        f"{watching.covers or 'none'}; socket close code {ws.close_code})")
            return watching
        if while_watching is not None:
            watch_while(viewer, while_watching)
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
    covers = watching.covers + typing.covers
    if covers:
        results.put("frames", "FAIL", "a frame covered the password field: " + "; ".join(covers[:3]))
    elif typing.judged == 0:
        results.put("frames", "FAIL", "no frame showing the page arrived while the person typed")
    elif not returned:
        results.put("frames", "FAIL", f"return control answered {status}")
    else:
        f = typing.field
        results.put("frames", "PASS", f"{typing.judged} frame(s) while the person typed and {watching.judged} "
                    f"otherwise showed the page; in every one the field's box ({f.x0},{f.y0}-{f.x1},{f.y1}) "
                    f"was the field as Chromium drew it, nothing painted over it"
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
                    "frames, the same stream the relay records, are judged by the frames check")
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
    if seen.covers:
        results.put("recording", "FAIL", "a recorded frame covers the field: " + "; ".join(seen.covers[:3]))
    elif seen.judged == 0:
        results.put("recording", "FAIL", f"no recorded frame of {len(segments)} segment(s) shows the page")
    else:
        results.put("recording", "PASS", f"{seen.judged} recorded frame(s) of {len(segments)} segment(s) show "
                    "the page, and in every one the field's box is the field as drawn")


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


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_deadline(api: Api, cfg: Config) -> str | None:
    """The open hand-off's deadline as GET /live/session serves it now."""
    status, view = api.call("GET", f"/v1/tasks/{cfg.task}/live/session")
    session = view.get("session") if status == 200 and isinstance(view, dict) else None
    wait = session.get("human_wait") if isinstance(session, dict) else None
    deadline = wait.get("deadline") if isinstance(wait, dict) else None
    return deadline if isinstance(deadline, str) else None


@dataclass
class Hold:
    """The hand-off's deadline as this viewer read it: as the agent's request
    announced it, right after this viewer's join, and once the probes were
    done, before control was taken.

    A verdict of PARKED on HUMAN_REQUIRED said only that the hold ran out
    with nobody in control, and three different defects end that way: the
    join not counted as watching (swarm-api's `LiveService.watched`), the
    socket gone before control was taken, or the worker parking a hold that
    was being watched (an expiry it misjudged, or `gate_lost`). The scheduled
    runs of 2026-10-15..19 could not be told apart (#1333); these readings,
    with the task's own events (`explain_human_park`), say which.

    The join's reading is exact, not a race: swarm-api's socket relays no
    frame until the join's `watched` has returned (`_Proxy.run`), so a read
    made after the first frame sees what the join wrote."""

    opened: str | None
    joined: str | None = None
    before_control: str | None = None

    def counted(self) -> bool | None:
        """Whether the join moved the deadline; None when it was not read."""
        opened, joined = _instant(self.opened), _instant(self.joined)
        if opened is None or joined is None:
            return None
        return joined > opened

    def describe(self) -> str:
        if self.opened is None:
            return "the hand-off was announced with no deadline"
        counted = self.counted()
        if counted is None:
            return (f"the hold opened to {self.opened}, and its deadline was not read after this "
                    "viewer's join")
        if not counted:
            return (f"this viewer's join did NOT move the hold's deadline: it stayed {self.opened}, so "
                    "swarm-api did not count the join as watching (LiveService.watched)")
        said = f"this viewer's join moved the hold's deadline from {self.opened} to {self.joined}"
        if self.before_control is not None:
            said += f", and it stood at {self.before_control} before control was taken"
        return said


def explain_human_park(events: list[dict], hold: Hold | None) -> str:
    """Which way a hand-off ended in PARKED(HUMAN_REQUIRED), from the task's
    own events: how many times the agent asked, what each resolved hold
    recorded of a viewer and of control (swarm-api's funnel, in seconds from
    the request), the worker's park cause (`hold_expired` or `gate_lost`),
    and whether a return re-queued the task so that a later attempt asked
    again with nobody watching."""
    def detail(event: dict) -> dict:
        value = event.get("detail")
        return value if isinstance(value, dict) else {}

    asked = sum(1 for e in events if e.get("type") == "human_handoff_requested")
    said = [f"the agent asked for help {asked} time(s)"]
    for event in events:
        if event.get("type") != "human_handoff_resolved":
            continue
        d = detail(event)
        funnel = d.get("funnel") if isinstance(d.get("funnel"), dict) else {}
        viewer = funnel.get("first_viewer_s")
        control = funnel.get("control_taken_s")
        said.append(
            f"a hold of attempt {event.get('attempt_id') or d.get('attempt_id')} resolved "
            f"{d.get('outcome')} after {d.get('held_seconds')} s, "
            + ("with no viewer counted" if viewer is None else f"a viewer counted at {viewer} s")
            + ("" if control is None else f", control taken at {control} s"))
    for event in events:
        d = detail(event)
        if event.get("type") == "parked" and d.get("park_phase") == "human_wait":
            said.append(f"the worker of attempt {event.get('attempt_id')} parked on {d.get('cause')}")
        elif event.get("type") == "ready" and d.get("reason") == "human_returned":
            said.append("a return of control re-queued the task after its park (LB-RETURN), so a "
                        "later attempt ran and asked again")
    if hold is not None:
        said.append(hold.describe())
    return "; ".join(said)


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
                if state.startswith("PARKED:HUMAN_REQUIRED"):
                    # It parked before this viewer ever saw the hand-off open.
                    events, _ = all_events(api, cfg.task)
                    reason += "; " + explain_human_park(events, None)
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
        hold = Hold(opened=wait.get("deadline") if isinstance(wait.get("deadline"), str) else None)

        def probe() -> None:
            probed.append(True)
            hold.joined = read_deadline(api, cfg)
            check_isolation(api, cfg, session, results)
            hold.before_control = read_deadline(api, cfg)
            log(hold.describe())

        tally = drive_viewer(api, cfg, session, password, results, while_watching=probe)
        if not probed:  # the viewer stopped before the page was on screen
            check_isolation(api, cfg, session, results)
        state = wait_settled(cfg, cfg.timeout + 120)
        if state is None or state not in TERMINAL:
            verdict, reason = not_run_reason(state or "RUNNING")
            if (state or "").startswith("PARKED:HUMAN_REQUIRED"):
                events, _ = all_events(api, cfg.task)
                reason += "; " + explain_human_park(events, hold)
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


# --------------------------------------------------------------------------
# QI-ACCEPT: the QA identity signs the agent's browser in to the console
# --------------------------------------------------------------------------
#
# docs/design/qa-identity.md §6 row 6 (part of #1649). A second
# `claude-code-browser` task, in a tenant a platform admin has granted the
# QA identity, names `qa_identity: "console"`. Its live gate then adds the
# QA account's IAP credential to the console's requests, so the console
# opens signed in, and refuses a hand-off on the console host
# (`qa_identity_host`): a person's own sign-in would hand the agent their
# workspaces. The same shape as the hand-off case: the group submits the task
# (`qa_identity_submission`) and settles it; this process judges it.
#
#   qa_signed_in          the agent's first navigation to the console ends on
#                         the console's own host, on its Overview, with no
#                         Google sign-in page in between
#   qa_handoff_refused    `swarm-handoff request` there exits 5 with
#                         `qa_identity_host`, and the task has no hand-off event
#   qa_ungranted_refused  the same submission from another tenant is refused
#                         at submission, 403 `qa_identity_not_granted`
#   qa_no_leak            no credential, by its shape, is in the task's
#                         transcript, logs, outputs, recordings, checkpoints,
#                         API documents or events
#
# THE CREDENTIAL NEVER REACHES THIS PROCESS: swarm-api signs it for the
# attempt and the gate holds it. So the sweep looks for what any credential
# looks like (a JWT, a Bearer value, the proxy credential header's name),
# never for a value. A checkpoint archives the agent's workspace, whose clone
# can hold documents that name the header, so checkpoints are read for values
# only, never for the header's name. No pattern below is written whole, so
# this file, read from a clone, never matches itself.

#: The QA identity case's checks, in the order the group lists them.
QA_IDENTITY_CHECKS = ("qa_signed_in", "qa_handoff_refused", "qa_ungranted_refused", "qa_no_leak")
#: The catalogue's one choice (`claude-code-browser` declares `qa_identity`).
QA_IDENTITY = "console"
QA_PROFILE = "claude-code-browser"
#: What the agent writes under $SWARM_ARTIFACTS_DIR: what it saw, and what
#: the hand-off answered.
QA_OUTPUT = "qa-console.json"
#: The gate's refusal of a hand-off on the console host (livegate/__main__.py).
QA_HOST_REFUSAL = "qa_identity_host"
#: swarm-api's refusal of an ungranted tenant (swarm_api/qa_identity.py).
QA_NOT_GRANTED = "qa_identity_not_granted"
#: The page an IAP sign-in that did not happen lands on.
GOOGLE_SIGN_IN_HOST = "accounts.google.com"
#: The console's first screen (apps/swarm-ui/src/App.tsx, SECTIONS).
OVERVIEW = "Overview"
#: The hand-off's exit codes (agent_worker/handoff.py: EXIT_REFUSED).
HANDOFF_EXIT_REFUSED = 5

#: What a credential looks like, each built from parts.
_JWT = re.compile(("ey" + "J") + r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}")
_BEARER = re.compile(r"(?i)\b" + "bear" + r"er\s+[A-Za-z0-9._~+/-]{20,}")
_PROXY_HEADER = re.compile(r"(?i)\b" + "proxy-" + "authorization" + r"\b")


def qa_identity_prompt(console_url: str) -> str:
    """What the QA identity's agent is asked. Every step is spelled out
    because a check reads each: one navigation, nothing typed, the hand-off
    tried once and its answer read once, bounded, and the report file.

    THE ANSWER IS `wait`'S (#1333). `swarm-handoff request` only queues the
    request: it prints `requested` and exits 0 whatever the gate will say
    (images/agent-runtime-browser/swarm-handoff, `cmd_request`). The
    worker answers on its next tick, and `wait` prints that answer and exits
    with its code. Read from `request`, the check saw "exited 0 with code
    None" on every run. `--max-seconds 60` bounds it: a hold the gate did not
    refuse exits 75 rather than waiting for a person, and fails the check."""
    console = console_url.rstrip("/") + "/"
    return (
        "Check that this browser is signed in to the SwarmCloud console, and that a hand-off "
        "is refused there. Never type into any page and never ask a person for anything else. "
        "1) In a Python script using Playwright, connect to the platform browser with "
        'p.chromium.connect_over_cdp(os.environ["SWARM_CDP_ENDPOINT"]), take browser.contexts[0] '
        f"and its first page (or a new page), goto {console} with wait_until=\"networkidle\" and "
        "a 60000 ms timeout, then wait 5 s. Read page.url, page.title() and the inner text of "
        "the body (its first 4000 characters), and end the script without closing the page or "
        "the browser. 2) Run once: swarm-handoff request --reason \"QA identity acceptance: a "
        "hand-off on the console\", then run once: swarm-handoff wait --max-seconds 60. Note the "
        "exit status of swarm-handoff wait and the \"code\" field of the JSON it printed (null "
        "when there is none). Run neither again. 3) Write to "
        f"$SWARM_ARTIFACTS_DIR/{QA_OUTPUT} one JSON object with the keys url, title, text, "
        "handoff_exit and handoff_code, and reply with only that JSON."
    )


def qa_identity_submission(prompt: str) -> dict:
    """POST /v1/tasks's body for the QA identity's task. One attempt: a second
    would ask for the hand-off again and add nothing to the verdict."""
    return {"runner_profile": QA_PROFILE, "input": {"prompt": prompt, "qa_identity": QA_IDENTITY},
            "max_attempts": 1}


def _report(raw: bytes | None) -> dict | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def judge_signed_in(raw: bytes | None, console_url: str) -> tuple[str, str]:
    """The agent's report, judged: it landed on the console's own host, on
    the Overview, and not on Google's sign-in page."""
    report = _report(raw)
    if report is None:
        return "FAIL", f"the agent wrote no readable {QA_OUTPUT}"
    url = report.get("url") if isinstance(report.get("url"), str) else ""
    text = report.get("text") if isinstance(report.get("text"), str) else ""
    want = urllib.parse.urlsplit(console_url).hostname
    got = urllib.parse.urlsplit(url).hostname
    if got == GOOGLE_SIGN_IN_HOST or "Sign in with Google" in text:
        return "FAIL", (f"the console sent the browser to Google's sign-in ({got}): the gate did not "
                        "add the QA identity's credential, or IAP refused it")
    if got != want:
        return "FAIL", f"the first navigation to the console ended on {got!r}, not the console's {want!r}"
    if OVERVIEW not in text:
        return "FAIL", (f"the page on {got} did not show the {OVERVIEW} "
                        f"({len(text)} characters read, title {report.get('title')!r})")
    return "PASS", (f"the agent's first navigation to {want} landed on the console's {OVERVIEW}, signed "
                    "in with no person's hand-off and no Google sign-in page")


def judge_handoff_refused(report: dict | None, events: list[dict]) -> tuple[str, str]:
    if report is None:
        return "FAIL", f"the agent wrote no readable {QA_OUTPUT}, so the hand-off's answer is unknown"
    asked = [e.get("type") for e in events if e.get("type") in HANDOFF_EVENTS]
    exit_code, code = report.get("handoff_exit"), report.get("handoff_code")
    if exit_code != HANDOFF_EXIT_REFUSED or code != QA_HOST_REFUSAL:
        return "FAIL", (f"swarm-handoff request on the console exited {exit_code!r} with code {code!r}, "
                        f"not {HANDOFF_EXIT_REFUSED} with {QA_HOST_REFUSAL}")
    if asked:
        return "FAIL", ("the gate refused the hand-off but the task recorded " + ", ".join(asked)
                        + ": a hold was opened on the console host")
    return "PASS", (f"swarm-handoff request on the console was refused {QA_HOST_REFUSAL} (exit "
                    f"{HANDOFF_EXIT_REFUSED}) and the task has none of {', '.join(HANDOFF_EVENTS)}")


def judge_ungranted(status: int, body: Any) -> tuple[str, str]:
    code = None
    if isinstance(body, dict):
        code = body.get("code")
        if code is None and isinstance(body.get("error"), dict):
            code = body["error"].get("code")
    if status == 403 and code == QA_NOT_GRANTED:
        return "PASS", f"the submission was refused at submission, 403 {QA_NOT_GRANTED}"
    if 200 <= status < 300:
        return "FAIL", ("the submission was ACCEPTED: an ungranted tenant's agent may name the QA "
                        "identity (it was cancelled; check the tenant is not granted)")
    return "FAIL", f"the submission answered {status} {code!r}, not 403 {QA_NOT_GRANTED}"


def credential_marks(data: bytes, *, names: bool = True) -> list[str]:
    """Which credential shapes `data` (or its gzip contents) holds, by kind
    only: a value found is never echoed. `names=False` skips the header's
    name, for a checkpoint's clone."""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gunzip_all(data)
        except zlib.error:
            pass
    text = data.decode("utf-8", "replace")
    marks = []
    if _JWT.search(text):
        marks.append("a JWT")
    if _BEARER.search(text):
        marks.append("a Bearer credential")
    if names and _PROXY_HEADER.search(text):
        marks.append("a " + "Proxy-" + "Authorization header")
    return marks


def check_ungranted(api: Api, cfg: Config, results: Results) -> None:
    other = other_tenant(api, cfg)
    if other is None:
        results.put("qa_ungranted_refused", "SKIP", f"not measured: this identity is a member of no tenant "
                    f"but {cfg.tenant}; set SWARM_ACCEPTANCE_OTHER_TENANT to an ungranted one")
        return
    prompt = qa_identity_prompt(cfg.console_url or "https://console.invalid")
    status, body = api.call("POST", "/v1/tasks", qa_identity_submission(prompt), tenant=other)
    if 200 <= status < 300 and isinstance(body, dict):
        made = body.get("id") or body.get("task_id")
        if isinstance(made, str):
            api.call("POST", f"/v1/tasks/{made}/cancel", {}, tenant=other)
    verdict, reason = judge_ungranted(status, body)
    results.put("qa_ungranted_refused", verdict, f"as a member of {other}: {reason}")


def qa_sweep(api: Api, cfg: Config, gcs: Gcs | None, results: Results) -> dict[str, bytes]:
    """The qa_no_leak check. Returns the outputs it read."""
    outputs: dict[str, bytes] = {}
    if gcs is None:
        results.put("qa_no_leak", "FAIL", "no access token for the artifact bucket: nothing was swept")
        return outputs
    prefix = f"tenants/{cfg.tenant}/tasks/{cfg.task}/"
    try:
        items = gcs.list(prefix)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        results.put("qa_no_leak", "FAIL", f"listing {prefix} failed ({type(exc).__name__})")
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
        marks = credential_marks(data, names=kind != "checkpoints")
        if marks:
            found.append(f"{name[len(prefix):]} ({', '.join(marks)})")
    t = cfg.task
    documents = 0
    for path in (f"/v1/tasks/{t}", f"/v1/tasks/{t}/live/audit", f"/v1/tasks/{t}/answer"):
        status, body = api.call("GET", path)
        if status != 200:
            unread.append(f"GET {path.replace(t, '<task>')} ({status})")
            continue
        documents += 1
        marks = credential_marks(json.dumps(body).encode())
        if marks:
            found.append(f"GET {path.replace(t, '<task>')} ({', '.join(marks)})")
    events, status = all_events(api, t)
    if status != 200:
        unread.append(f"GET /v1/tasks/<task>/events ({status})")
    else:
        marks = credential_marks(json.dumps(events).encode())
        if marks:
            found.append(f"one of the task's {len(events)} events ({', '.join(marks)})")
    empty = [c for c in ("transcript", "logs", "outputs") if counts[c] == 0]
    if found:
        results.put("qa_no_leak", "FAIL", "a credential's shape is in: " + ", ".join(found[:10]))
    elif unread:
        results.put("qa_no_leak", "FAIL", "not read, so not known clean: " + ", ".join(unread[:10]))
    elif empty:
        results.put("qa_no_leak", "FAIL", "the sweep found no " + ", ".join(empty) + " to read under "
                    f"{prefix}: an unread category is not a clean one")
    else:
        results.put("qa_no_leak", "PASS", f"no JWT, Bearer value or proxy credential header in any of "
                    f"{len(items)} objects (transcript {counts['transcript']}, logs {counts['logs']}, "
                    f"checkpoints {counts['checkpoints']}, outputs {counts['outputs']}, recording "
                    f"{counts['recording']}; gzip read through) nor in {documents} API documents or the "
                    f"task's {len(events)} events")
    return outputs


def run_qa_identity(cfg: Config) -> None:
    results = Results(cfg.results, QA_IDENTITY_CHECKS)
    api = Api(cfg.api, cfg.credential, cfg.tenant)
    gcs = Gcs(cfg.bucket, cfg.gcs_token) if cfg.gcs_token else None
    try:
        if not cfg.console_url:
            results.rest("FAIL", "no --console-url: the case cannot say which host is the console")
            return
        check_ungranted(api, cfg, results)
        state = wait_settled(cfg, cfg.timeout + 120)
        if state is None or state not in TERMINAL:
            verdict, reason = not_run_reason(state or "RUNNING")
            results.put("qa_signed_in", verdict, reason)
            results.rest("SKIP", "not measured: the task did not end (see the signed-in check)")
            return
        outputs = qa_sweep(api, cfg, gcs, results)
        written = [v for k, v in outputs.items() if k.endswith("/artifacts/" + QA_OUTPUT)]
        raw = written[-1] if written else None
        verdict, reason = judge_signed_in(raw, cfg.console_url)
        if state != "SUCCEEDED":
            verdict, reason = "FAIL", f"the task ended {state}; " + reason
        results.put("qa_signed_in", verdict, reason)
        events, status = all_events(api, cfg.task)
        if status != 200:
            results.put("qa_handoff_refused", "FAIL", f"GET /events answered {status}")
        else:
            results.put("qa_handoff_refused", *judge_handoff_refused(_report(raw), events))
    except Exception as exc:  # a crash must still name every unjudged check
        results.rest("FAIL", f"the check crashed ({type(exc).__name__}) before judging this")
        raise
    finally:
        results.rest("FAIL", "not judged")


def _fake_jwt() -> str:
    """A JWT-shaped value built here at run time, never written as one literal."""
    def part(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return ".".join((part(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()),
                     part(json.dumps({"aud": "x", "exp": 1}).encode()), part(secrets.token_bytes(64))))


def test_the_qa_identity_task_names_the_console_and_asks_for_the_refused_hand_off() -> None:
    """QI-ACCEPT (#1649): the task the group submits names the QA identity by
    the catalogue's one choice and the console it must land on, and asks the
    agent to try the hand-off the gate must refuse there."""
    console = "https://console.example.test"
    prompt = qa_identity_prompt(console)
    assert console + "/" in prompt and "swarm-handoff request" in prompt
    assert "$SWARM_ARTIFACTS_DIR/" + QA_OUTPUT in prompt and "never type" in prompt.lower()
    body = qa_identity_submission(prompt)
    assert body["runner_profile"] == "claude-code-browser"
    assert body["input"] == {"prompt": prompt, "qa_identity": QA_IDENTITY} and QA_IDENTITY == "console"
    assert body["max_attempts"] == 1


def test_the_console_counts_as_signed_in_only_on_its_own_overview() -> None:
    console = "https://console.example.test"
    good = {"url": console + "/#/now", "title": "SwarmCloud", "text": "Overview\nAgents running 0"}
    assert judge_signed_in(json.dumps(good).encode(), console)[0] == "PASS"
    for report in (
        None,
        b"not json",
        json.dumps({**good, "url": "https://accounts.google.com/v3/signin"}).encode(),
        json.dumps({**good, "url": "https://console.example.test.evil.example/"}).encode(),
        json.dumps({**good, "text": "Sign in with Google"}).encode(),
        json.dumps({**good, "text": "Agents"}).encode(),
    ):
        verdict, reason = judge_signed_in(report, console)
        assert verdict == "FAIL" and reason, report


def test_the_hand_off_on_the_console_passes_only_when_the_gate_refused_it() -> None:
    refused = {"handoff_exit": 5, "handoff_code": QA_HOST_REFUSAL}
    assert judge_handoff_refused(refused, [{"type": "started"}])[0] == "PASS"
    assert judge_handoff_refused({"handoff_exit": 0, "handoff_code": None}, [])[0] == "FAIL"
    assert judge_handoff_refused({"handoff_exit": 5, "handoff_code": "no_live_path"}, [])[0] == "FAIL"
    assert judge_handoff_refused(refused, [{"type": "human_handoff_requested"}])[0] == "FAIL"
    assert judge_handoff_refused(None, [])[0] == "FAIL"


def test_an_ungranted_tenant_passes_only_when_refused_qa_identity_not_granted() -> None:
    assert judge_ungranted(403, {"code": QA_NOT_GRANTED})[0] == "PASS"
    assert judge_ungranted(201, {"id": "task_1"})[0] == "FAIL"
    assert judge_ungranted(403, {"code": "forbidden"})[0] == "FAIL"
    assert judge_ungranted(0, {"error": {"code": "URLError"}})[0] == "FAIL"


def test_the_leak_scan_finds_a_credential_by_its_shape_and_never_echoes_it() -> None:
    token = _fake_jwt()
    header = "Proxy-" + "Authorization"
    assert credential_marks(b"clean log line\n") == []
    assert credential_marks(f"x {token} y".encode()) == ["a JWT"]
    assert credential_marks(f"{header}: x".encode()) == [f"a {header} header"]
    scheme = "Bear" + "er"
    assert credential_marks(f"Authorization: {scheme} {token}".encode()) == ["a JWT", "a Bearer credential"]
    zipped = gzip.compress(json.dumps({"authorization": f"{scheme} {secrets.token_hex(24)}"}).encode())
    assert credential_marks(zipped) == ["a Bearer credential"]
    # The header's name is no leak where the clone's own docs name it.
    assert credential_marks(f"{header}: x".encode(), names=False) == []
    assert token not in " ".join(credential_marks(token.encode()))


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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["qa-identity-submission"] and len(argv) == 2:
        # The group's body for the QA identity's task: the prompt lives here,
        # beside the judges that read what it asks for.
        print(json.dumps(qa_identity_submission(qa_identity_prompt(argv[1]))))
        return 0
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
    parser.add_argument("--qa-identity", action="store_true",
                        help="judge the QA identity's task (QI-ACCEPT) instead of the hand-off's")
    parser.add_argument("--console-url", help="the console's URL, for --qa-identity")
    args = parser.parse_args(argv)
    credential = _read_once(args.credential_file)
    gcs_token = _read_once(args.gcs_token_file)
    cfg = Config(task=args.task, tenant=args.tenant, api=args.api, bucket=args.bucket,
                 control_dir=args.control_dir, results=args.results, timeout=args.timeout,
                 other_tenant=args.other_tenant or None, credential=credential or "",
                 gcs_token=gcs_token, console_url=args.console_url or None)
    checks = QA_IDENTITY_CHECKS if args.qa_identity else CHECKS
    if not credential:
        Results(args.results, checks).rest("FAIL", "no API credential was handed over")
        return 1
    if args.qa_identity:
        run_qa_identity(cfg)
    else:
        run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
