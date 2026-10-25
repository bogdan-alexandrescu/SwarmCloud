"""Measure the live gate's capture -> encode -> send path (STREAM-SMOOTH; part of #1030).

    uv run python -m agent_worker.livegate.bench [--seconds 4] [--json out.json]

WHY THIS EXISTS. "Taking control is choppy" is a sentence; this is the number
under it. It runs the gate's own code -- `XvfbFramebuffer.grab`, `captured`,
`FrameHub`, `RfbSession` and `LiveGate._capture` -- on
synthetic but realistic frames, with no Xvfb, no Chromium and no network, so
the same command measures any checkout: the before and after in the
STREAM-SMOOTH pull request are this module run on origin/main and on the
branch. It also runs in the browser sidecar's image, where a pod's own CPU
can be measured with it.

TWO PARTS.

* `frames`: per frame, at 1440x987 (Desktop) and 393x852 (iPhone 15), for a
  static page, a page scrolling and a field being typed into: milliseconds to
  grab, and to encode and send one update to a viewer that speaks as
  noVNC 1.4.0 does (its encoding list, its pixel format, one incremental
  request per update), and the bytes of that update.
* `session`: the real capture loop, a person in control (or only watching),
  a noVNC-like viewer pulling updates as fast as it receives them and the
  relay's recorder pulling at 10 per second beside it (it is on every
  tunnel), for
  `--seconds`: updates per second reaching the viewer, bytes per second, the
  input-to-frame latency (a key the viewer sends, to the first update it
  receives that covers the character the key drew) at p50 and p95, and the
  process's CPU per second of stream.

WHAT IT CANNOT SEE. Chromium's own paint time (a key is drawn one 16 ms
"frame" after the gate injects it here), the network between the pod, the
relay, swarm-api and the browser, and noVNC's decode time. Inside-the-pod
numbers are what the gate controls; the rest is reported separately when
measured live.

NOTHING HERE IS A SECRET OR PAGE DATA: the pages are drawn from a seeded
random glyph set, and the typed keys are letters.

NO MASK COLUMN. Until LIVE-NOMASK (#2298) the frames part also timed the mask
(`mask_ms`); the gate paints nothing on a frame now (§5.3; owner decision,
BGD 2026-10-25), so there is no step between the grab and the encode to time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mmap
import os
import random
import socket
import statistics
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import __main__ as gate_main
from . import rfb
from .capture import Frame, XvfbFramebuffer
from .device import captured
from .presets import resolve

#: The two sizes the owner named: Desktop and iPhone 15, as the viewer gets them.
SIZES = {"desktop": ("desktop", "landscape"), "iphone": ("iphone-15", "portrait")}
#: Per frame: nothing changed, the page scrolled, a character appeared.
SCENARIOS = ("static", "scroll", "typing")
#: Per session: "idle" is a mostly static page -- a caret blinking every
#: 530 ms (Chromium's rate) and a small spinner turning at 10 Hz -- which is
#: what a page waiting for the person looks like.
SESSION_SCENARIOS = ("idle", "scroll", "typing")

#: What noVNC 1.4.0 sends in SetEncodings (core/rfb.js `_sendEncodings`), in
#: its order, with its default quality 6 and compression 2: Tight, TightPNG,
#: ZRLE, JPEG, Hextile, RRE and Raw -- no Zlib (6).
NOVNC_ENCODINGS = (1, 7, -260, 16, 21, 5, 2, 0, -32 + 6, -256 + 2, -223, -224, -258, -308,
                   -309, -312, -313, -307, -1063131698, 0x574D5664, -239)
#: The relay's recorder (swarm_live/recorder.py): Zlib, Raw, DesktopSize.
RECORDER_ENCODINGS = (6, 0, -223)

_WHITE = (255, 255, 255)
_INK = (36, 41, 47)
_CHROME = (222, 225, 230)
_FIELD = (250, 250, 250)
_BORDER = (140, 149, 159)

GLYPH_W, GLYPH_H, LINE_H = 7, 12, 20
#: How long the fake browser takes to draw a key's effect: one 60 Hz frame.
RENDER_SECONDS = 1 / 60
#: How often the scrolling page moves, and by how much: a steady wheel scroll.
SCROLL_EVERY, SCROLL_PX = 1 / 60, 24
#: How often the typing viewer presses a key: a quick typist.
KEY_EVERY = 0.12


# --------------------------------------------------------------------------
# Synthetic pages, in Xvfb's byte order (B, G, R, X)
# --------------------------------------------------------------------------


def _px(rgb: tuple[int, int, int]) -> bytes:
    return bytes((rgb[2], rgb[1], rgb[0], 0))


def _glyphs(rng: random.Random, count: int = 64) -> list[list[bytes]]:
    """Letter-like bitmaps with anti-aliased edges: rows of 7 pixels each."""
    greys = [_px((v, v, v)) for v in (255, 220, 170, 110, 60, 36)]
    out = []
    for _ in range(count):
        rows = []
        for y in range(GLYPH_H):
            if y < 2 or y > 10:
                rows.append(greys[0] * GLYPH_W)
                continue
            rows.append(b"".join(greys[rng.choice((0, 0, 0, 1, 2, 3, 4, 5, 5))] if 0 < x < 6 else greys[0]
                                 for x in range(GLYPH_W)))
        out.append(rows)
    return out


class Page:
    """A tall document: browser chrome, then paragraphs, a photo and a form field."""

    def __init__(self, width: int, viewport: int, *, seed: int = 7, chrome: int = 80) -> None:
        self.width = width
        self.viewport = viewport
        self.chrome = chrome
        self.row = width * 4
        rng = random.Random(seed)
        glyphs = self._glyphs = _glyphs(rng)
        white = _px(_WHITE)
        height = viewport * 4
        doc = bytearray(white * (width * height))
        margin = max(16, width // 12)
        chars = (width - 2 * margin) // GLYPH_W
        y = 24
        while y + LINE_H < height:
            if (y // LINE_H) % 14 == 6 and width > 300:
                # A photo: anti-aliased noise, which no lossless coder shrinks much.
                h, w = min(220, height - y - 1), min(360, width - 2 * margin)
                table = bytes((60 + (i * 140) // 255) for i in range(256))
                for r in range(h):
                    at = (y + r) * self.row + margin * 4
                    doc[at:at + w * 4] = rng.randbytes(w * 4).translate(table)
                y += h + LINE_H
                continue
            n = rng.randint(chars // 2, chars)
            line = [glyphs[rng.randrange(len(glyphs))] for _ in range(n)]
            for gy in range(GLYPH_H):
                at = (y + gy) * self.row + margin * 4
                data = b"".join(g[gy] for g in line)
                doc[at:at + len(data)] = data
            y += LINE_H
        self.doc = bytes(doc)
        self.chrome_rows = (_px(_CHROME) * width) * chrome
        # The form field the typing scenario types into, on the first screen.
        self.field_x = margin
        self.field_y = chrome + min(160, viewport // 3)
        self.field_w = min(width - 2 * margin, 420)
        self.field_h = 28
        self.typed = 0

    def screen(self, offset: int = 0) -> bytearray:
        """The display: the chrome, the document from `offset`, the field."""
        body = self.viewport - self.chrome
        start = offset * self.row
        out = bytearray(self.chrome_rows + self.doc[start:start + body * self.row])
        return out

    def field(self, out: bytearray) -> None:
        f = Frame(self.width, self.viewport, self.row, out)
        f.fill(self.field_x, self.field_y, self.field_x + self.field_w, self.field_y + self.field_h, _BORDER)
        f.fill(self.field_x + 1, self.field_y + 1, self.field_x + self.field_w - 1,
               self.field_y + self.field_h - 1, _FIELD)

    def char_box(self, i: int) -> tuple[int, int, int, int]:
        x = self.field_x + 6 + (i % ((self.field_w - 12) // GLYPH_W)) * GLYPH_W
        y = self.field_y + 8
        return x, y, x + GLYPH_W, y + GLYPH_H

    def type_char(self, out: bytearray | mmap.mmap, base: int = 0) -> tuple[int, int, int, int]:
        """Draw the next character into the field (in `out`, from byte `base`)."""
        box = self.char_box(self.typed)
        glyph = self._glyphs[self.typed % len(self._glyphs)]
        x0, y0, _, _ = box
        for gy in range(GLYPH_H):
            at = base + (y0 + gy) * self.row + x0 * 4
            out[at:at + GLYPH_W * 4] = glyph[gy]
        self.typed += 1
        return box


def _xwd_header(width: int, height: int) -> bytes:
    """An XWD v7 ZPixmap header as Xvfb -fbdir writes it (LSB first, 32 bpp)."""
    name = b"Xvfb main window\x00"
    header_size = 100 + len(name)
    return struct.pack(">25I", header_size, 7, 2, 24, width, height, 0, 0, 32, 0, 32, 32,
                       width * 4, 4, 0xFF0000, 0x00FF00, 0x0000FF, 8, 256, 0,
                       width, height, 0, 0, 0) + name


class Screen:
    """Xvfb's framebuffer file, which this harness draws into as Xvfb would."""

    def __init__(self, directory: Path, page: Page) -> None:
        self.page = page
        self.path = directory / "Xvfb_screen0"
        header = _xwd_header(page.width, page.viewport)
        self.base = len(header)
        self.path.write_bytes(header + bytes(page.screen()))
        self._fd = os.open(self.path, os.O_RDWR)
        self.map = mmap.mmap(self._fd, 0)
        self.offset = 0
        self.show(0)

    def show(self, offset: int) -> None:
        self.offset = offset
        pixels = self.page.screen(offset)
        self.page.field(pixels)
        self.map[self.base:self.base + len(pixels)] = pixels
        typed, self.page.typed = self.page.typed, 0
        while self.page.typed < typed:
            self.page.type_char(self.map, self.base)

    def type_char(self) -> tuple[int, int, int, int]:
        return self.page.type_char(self.map, self.base)

    def _box(self, x0: int, y0: int, x1: int, y1: int, rgb: tuple[int, int, int]) -> None:
        run = _px(rgb) * (x1 - x0)
        for y in range(y0, y1):
            at = self.base + y * self.page.row + x0 * 4
            self.map[at:at + len(run)] = run

    def caret(self, on: bool) -> None:
        """The text caret after the last character typed, on or off."""
        x0, y0, _, y1 = self.page.char_box(self.page.typed)
        self._box(x0, y0 - 2, x0 + 1, y1 + 2, _INK if on else _FIELD)

    def spinner(self, step: int) -> None:
        """A 24x24 loading indicator in the page's top-right corner: one of 8 phases."""
        x0, y0 = self.page.width - 40, self.page.chrome + 12
        self._box(x0, y0, x0 + 24, y0 + 24, _WHITE)
        for i in range(8):
            shade = 60 + ((i - step) % 8) * 24
            dx, dy = ((0, 0), (9, 0), (18, 0), (18, 9), (18, 18), (9, 18), (0, 18), (0, 9))[i]
            self._box(x0 + dx, y0 + dy, x0 + dx + 6, y0 + dy + 6, (shade, shade, shade))

    def close(self) -> None:
        self.map.close()
        os.close(self._fd)


# --------------------------------------------------------------------------
# A viewer that speaks as noVNC 1.4.0 does
# --------------------------------------------------------------------------


class _Injector:
    def __init__(self, on_key: Any = None) -> None:
        self.on_key = on_key

    async def key(self, keysym: int, down: bool) -> None:
        if down and self.on_key is not None:
            self.on_key()

    async def pointer_event(self, buttons: int, x: int, y: int) -> None:
        pass

    async def type_text(self, text: str) -> bool:
        return True

    async def release_all(self) -> None:
        pass


@dataclass
class Update:
    received: float
    nbytes: int
    rects: list[tuple[int, int, int, int, int]] = field(default_factory=list)


class Client:
    """Reads updates and asks for the next one as soon as one has arrived."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.width = self.height = 0

    async def handshake(self, encodings: tuple[int, ...]) -> None:
        r, w = self.reader, self.writer
        w.write(await r.readexactly(12))
        n = (await r.readexactly(1))[0]
        await r.readexactly(n)
        w.write(b"\x01")
        await r.readexactly(4)
        w.write(b"\x01")
        head = await r.readexactly(24)
        self.width, self.height = struct.unpack(">HH", head[:4])
        await r.readexactly(struct.unpack(">I", head[20:24])[0])
        # noVNC's pixel format: 32 bpp, depth 24, little-endian, R at bit 0.
        w.write(b"\0\0\0\0" + struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16))
        w.write(struct.pack(">BxH", 2, len(encodings)) + b"".join(struct.pack(">i", e) for e in encodings))
        await w.drain()

    def request(self, incremental: bool) -> None:
        self.writer.write(struct.pack(">BBHHHH", 3, int(incremental), 0, 0, self.width, self.height))

    async def _compact(self) -> tuple[int, int]:
        n, size = 0, 0
        for shift in (0, 7, 14):
            b = (await self.reader.readexactly(1))[0]
            n += 1
            size |= (b & (0x7F if shift < 14 else 0xFF)) << shift
            if not b & 0x80 or shift == 14:
                break
        return size, n

    async def read_update(self) -> Update:
        r = self.reader
        kind, _pad, count = struct.unpack(">BBH", await r.readexactly(4))
        assert kind == 0, f"server message {kind}"
        total = 4
        rects = []
        for _ in range(count):
            x, y, w, h, enc = struct.unpack(">HHHHi", await r.readexactly(12))
            total += 12
            if enc == 1:
                n = 4  # CopyRect: the source's x and y
            elif enc == 0:
                n = w * h * 4
            elif enc == 6:
                (n,) = struct.unpack(">I", await r.readexactly(4))
                total += 4
            elif enc == 7:
                ctl = (await r.readexactly(1))[0] >> 4
                total += 1
                if ctl == 0x08:
                    n = 3
                else:
                    if ctl & 0x04:
                        await r.readexactly(1)
                        total += 1
                    if w * h * 3 < 12:
                        n = w * h * 3
                    else:
                        n, extra = await self._compact()
                        total += extra
            elif enc == -239:
                n = w * h * 4 + (w + 7) // 8 * h
            else:
                n = 0
            await r.readexactly(n)
            total += n
            if enc in (0, 1, 6, 7):
                rects.append((x, y, w, h, enc))
        return Update(time.perf_counter(), total, rects)


async def _pair() -> tuple[Any, Any]:
    a, b = socket.socketpair()
    for s in (a, b):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
    return await asyncio.open_connection(sock=a, limit=1 << 24), await asyncio.open_connection(sock=b, limit=1 << 24)


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


# --------------------------------------------------------------------------
# Part 1: per frame
# --------------------------------------------------------------------------


def _frames_for(scenario: str, screen: Screen, n: int) -> list[Any]:
    """Actions that change the screen between frames, one per frame."""
    if scenario == "static":
        return [None] * n
    if scenario == "scroll":
        return [("scroll", (i + 1) * SCROLL_PX * 2) for i in range(n)]
    return [("type",)] * n


async def _frames(size: str, scenario: str, encodings: tuple[int, ...], n: int) -> dict[str, Any]:
    name, orientation = SIZES[size]
    device = resolve(name, orientation)
    with tempfile.TemporaryDirectory() as tmp:
        page = Page(device.display_width, device.display_height,
                    chrome=device.frame_top if device.frame_top else 80)
        screen = Screen(Path(tmp), page)
        hub = rfb.FrameHub(device.display_width, device.frame_height)
        control = rfb.ControlState(_Injector())
        (sr, sw), (cr, cw) = await _pair()
        session = rfb.RfbSession("bench", sr, sw, hub, control, _Injector())
        task = asyncio.create_task(session.serve())
        client = Client(cr, cw)
        grab, send, sizes = [], [], []
        with XvfbFramebuffer(screen.path) as fb:
            first = captured(fb.grab(), device)
            hub.publish(first)
            await client.handshake(encodings)
            client.request(False)
            await cw.drain()
            await client.read_update()
            for action in _frames_for(scenario, screen, n):
                if action is not None and action[0] == "scroll":
                    screen.show(action[1])
                elif action is not None:
                    screen.type_char()
                t0 = time.perf_counter()
                frame = captured(fb.grab(), device)
                t1 = time.perf_counter()
                hub.publish(frame)
                client.request(True)
                await cw.drain()
                grab.append((t1 - t0) * 1000)
                if scenario == "static":
                    # Nothing changed, so no update comes (on main and on the
                    # branch): there is nothing to time at the viewer. The
                    # check's cost is in the session part's CPU.
                    await asyncio.sleep(0.005)
                    sizes.append(0)
                    continue
                update = await client.read_update()
                send.append((update.received - t1) * 1000)
                sizes.append(update.nbytes)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cw.close()
        screen.close()
    return {
        "grab_ms": round(statistics.median(grab), 2),
        "encode_send_ms": round(statistics.median(send), 2) if send else None,
        "bytes_per_update": int(statistics.median(sizes)),
    }


# --------------------------------------------------------------------------
# Part 2: a session, through the real capture loop
# --------------------------------------------------------------------------


def _gate(tmp: Path) -> Any:
    for name in ("profile", "downloads", "uploads"):
        (tmp / name).mkdir(exist_ok=True)
    cfg = gate_main.GateConfig(ctl_path=str(tmp / "ctl.sock"), profile_dir=tmp / "profile",
                               downloads_dir=tmp / "downloads", uploads_dir=tmp / "uploads",
                               cdp_host="127.0.0.1", cdp_port=9222,
                               relay_url="ws://relay.test:8080/v1/tunnel", fbdir=tmp)
    return gate_main.LiveGate(cfg, gate_main.Deps(), record=lambda event, fields: None)


async def _session(size: str, scenario: str, *, control: bool, seconds: float,
                   encodings: tuple[int, ...] = NOVNC_ENCODINGS) -> dict[str, Any]:
    name, orientation = SIZES[size]
    device = resolve(name, orientation)
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory() as tmp:
        page = Page(device.display_width, device.display_height,
                    chrome=device.frame_top if device.frame_top else 80)
        screen = Screen(Path(tmp), page)
        gate = _gate(Path(tmp))
        gate.devices = SimpleNamespace(device=device)
        gate.framebuffer = XvfbFramebuffer(screen.path)
        sent: list[tuple[float, tuple[int, int, int, int]]] = []

        draws: list[asyncio.TimerHandle] = []

        def drawn() -> None:
            # The browser draws the key one frame after it arrives.
            draws.append(loop.call_later(RENDER_SECONDS, screen.type_char))

        injector = _Injector(drawn if scenario == "typing" else None)
        gate.control = rfb.ControlState(injector)
        (sr, sw), (cr, cw) = await _pair()
        session = rfb.RfbSession("person-1", sr, sw, gate.hub, gate.control, injector)
        gate.viewers = 1
        gate._watching.set()
        serve = asyncio.create_task(session.serve())
        if control:
            await gate.control.grant("person-1")
        capture = asyncio.create_task(gate._capture())
        # The first full update is of a real frame, never of the hub's blank
        # placeholder: a page arriving inside the measured window is not "idle".
        while gate.hub.version == 0:
            await asyncio.sleep(0.005)
        client = Client(cr, cw)
        await client.handshake(encodings)
        client.request(False)
        await cw.drain()
        await client.read_update()
        # The relay's recorder is a second stream on every tunnel, pulling at
        # its own rate (recorder.py: 10 fps while someone watches).
        (rr, rw), (qr, qw) = await _pair()
        recording = rfb.RfbSession("rec-1", rr, rw, gate.hub, gate.control, injector)
        record_task = asyncio.create_task(recording.serve())
        recorder = Client(qr, qw)
        await recorder.handshake(RECORDER_ENCODINGS)

        async def record() -> None:
            recorder.request(False)
            while True:
                await qw.drain()
                await recorder.read_update()
                await asyncio.sleep(0.1)
                recorder.request(True)

        record_pull = asyncio.create_task(record())

        async def scroller() -> None:
            offset = 0
            while True:
                await asyncio.sleep(SCROLL_EVERY)
                offset = (offset + SCROLL_PX) % (page.viewport * 2)
                screen.show(offset)

        async def typist() -> None:
            i = page.typed
            while True:
                await asyncio.sleep(KEY_EVERY)
                x0, y0, x1, y1 = page.char_box(i)
                # In the viewer's frame, which starts `frame_top` rows down the display.
                sent.append((time.perf_counter(), (x0, y0 - device.frame_top, x1, y1 - device.frame_top)))
                i += 1
                cw.write(struct.pack(">BBxxI", 4, 1, 0x61))
                cw.write(struct.pack(">BBxxI", 4, 0, 0x61))
                await cw.drain()

        async def idler() -> None:
            step = 0
            while True:
                await asyncio.sleep(0.1)
                step += 1
                screen.spinner(step)
                if step % 5 == 0:
                    screen.caret(step % 10 == 0)

        actor = None
        if scenario == "idle":
            actor = asyncio.create_task(idler())
        elif scenario == "scroll":
            actor = asyncio.create_task(scroller())
        elif scenario == "typing" and control:
            actor = asyncio.create_task(typist())
        latencies: list[float] = []
        updates = nbytes = 0
        cpu0, wall0 = time.process_time(), time.perf_counter()
        end = wall0 + seconds
        while time.perf_counter() < end:
            client.request(True)
            await cw.drain()
            try:
                update = await asyncio.wait_for(client.read_update(), max(0.01, end - time.perf_counter()))
            except asyncio.TimeoutError:
                break
            updates += 1
            nbytes += update.nbytes
            while sent:
                at, (x0, y0, x1, y1) = sent[0]
                if any(x < x1 and x0 < x + w and y < y1 and y0 < y + h for x, y, w, h, _ in update.rects):
                    latencies.append((update.received - at) * 1000)
                    sent.pop(0)
                else:
                    break
        wall = time.perf_counter() - wall0
        cpu = time.process_time() - cpu0
        for handle in draws:
            handle.cancel()
        tasks = [t for t in (actor, capture, serve, record_pull, record_task) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        cw.close()
        qw.close()
        gate.framebuffer.close()
        screen.close()
    return {
        "updates_per_s": round(updates / wall, 1),
        "bytes_per_s": int(nbytes / wall),
        "latency_p50_ms": None if not latencies else round(_pct(latencies, 50), 1),
        "latency_p95_ms": None if not latencies else round(_pct(latencies, 95), 1),
        "cpu_per_s": round(cpu / wall, 3),
    }


# --------------------------------------------------------------------------


def _with_level(level: int) -> tuple[int, ...]:
    """noVNC's encodings with its CompressLevel pseudo-encoding set to `level`."""
    return tuple(-256 + level if -256 <= e <= -247 else e for e in NOVNC_ENCODINGS)


async def run(*, seconds: float, frames: int, sizes: tuple[str, ...] = tuple(SIZES),
              levels: tuple[int, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"frames": {}, "session": {}, "levels": {}}
    for level in levels:
        for scenario in ("scroll", "typing"):
            key = f"desktop/{scenario}/novnc level {level}"
            out["levels"][key] = await _frames("desktop", scenario, _with_level(level), frames)
    for size in sizes:
        for scenario in SCENARIOS:
            for viewer, encodings in (("novnc", NOVNC_ENCODINGS), ("recorder", RECORDER_ENCODINGS)):
                key = f"{size}/{scenario}/{viewer}"
                out["frames"][key] = await _frames(size, scenario, encodings, frames)
    for size in sizes:
        for scenario in SESSION_SCENARIOS:
            for mode in ("control", "watch"):
                if scenario == "typing" and mode == "watch":
                    continue
                key = f"{size}/{scenario}/{mode}"
                out["session"][key] = await _session(size, scenario, control=mode == "control",
                                                      seconds=seconds)
    return out


def _table(result: dict[str, Any]) -> str:
    lines = ["| frames: size/scenario/viewer | grab ms | encode+send ms | bytes/update |",
             "|---|---:|---:|---:|"]
    for key, r in result["frames"].items():
        lines.append(f"| {key} | {r['grab_ms']} | {r['encode_send_ms']} | {r['bytes_per_update']:,} |")
    if result.get("levels"):
        lines += ["", "| zlib level: size/scenario | encode+send ms | bytes/update |", "|---|---:|---:|"]
        for key, r in result["levels"].items():
            lines.append(f"| {key} | {r['encode_send_ms']} | {r['bytes_per_update']:,} |")
    lines += ["", "| session: size/scenario/mode | updates/s | bytes/s | input->frame p50 ms | p95 ms | CPU s/s |",
              "|---|---:|---:|---:|---:|---:|"]
    for key, r in result["session"].items():
        lines.append(f"| {key} | {r['updates_per_s']} | {r['bytes_per_s']:,} | {r['latency_p50_ms']} | "
                     f"{r['latency_p95_ms']} | {r['cpu_per_s']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_worker.livegate.bench", description=__doc__.split("\n")[0])
    parser.add_argument("--seconds", type=float, default=4.0, help="length of each session run")
    parser.add_argument("--frames", type=int, default=30, help="frames per per-frame scenario")
    parser.add_argument("--size", choices=tuple(SIZES), action="append", help="only these sizes")
    parser.add_argument("--levels", default="", help="also compare these zlib levels, e.g. 1,2,6,9")
    parser.add_argument("--json", type=Path, help="also write the result here")
    args = parser.parse_args(argv)
    levels = tuple(int(v) for v in args.levels.split(",") if v)
    result = asyncio.run(run(seconds=args.seconds, frames=args.frames, sizes=tuple(args.size or SIZES),
                             levels=levels))
    print(_table(result))
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
