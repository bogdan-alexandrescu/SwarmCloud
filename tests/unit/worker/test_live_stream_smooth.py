"""Taking control of the live browser is smooth (STREAM-SMOOTH; part of #1030).

Owner, 2026-10-13: "the take control experience is choppy, can we make it
more smooth and high quality?" Measured on origin/main with
`python -m agent_worker.livegate.bench` (the numbers are in the pull request):
10 frames a second whoever was watching, every update a full-width band of
Raw pixels (noVNC 1.4.0 offers no Zlib), and the grab and every encode on the
gate's event loop, so a key waited behind a frame. Each test here holds one
part of the fix:

* the capture rate follows control: 30 a second while a person holds it, 10
  while people only watch or the relay records, none with nobody there;
* an update carries only the 64x64 tiles that changed, and never a pixel the
  mask did not leave -- a masked area whose contents change stays masked in
  the update;
* a viewer that asks once is sent once, however many frames are published
  meanwhile: nothing queues behind a slow viewer;
* Tight, which noVNC offers, is lossless here: Fill and zlib'd Basic, never
  JPEG, and what the viewer rebuilds is the masked frame byte for byte;
* a controller's key reaches the display while an update is being encoded,
  and the framebuffer is grabbed off the event loop.

Every test drives a real `RfbSession` over a real socket pair; no Xvfb, no
Chromium, no network, no credential.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import rfb
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.mask import MASK_RGB, MaskState, Outcome

TIGHT = 7
ZLIB = 6
COPY_RECT = 1
RAW = 0
#: noVNC 1.4.0's SetEncodings (core/rfb.js `_sendEncodings`), in its order.
NOVNC = (1, 7, -260, 16, 21, 5, 2, 0, -26, -254, -223, -224, -258, -308, -309, -312, -313,
         -307, -1063131698, 0x574D5664, -239)
#: noVNC's pixel format: R in the lowest byte.
NOVNC_SHIFTS = (0, 8, 16)


# --------------------------------------------------------------------------
# A viewer that rebuilds its framebuffer from what it is sent
# --------------------------------------------------------------------------


class Injector:
    def __init__(self) -> None:
        self.keys: list[tuple[int, bool, float]] = []

    async def key(self, keysym: int, down: bool) -> None:
        self.keys.append((keysym, down, time.monotonic()))

    async def pointer_event(self, buttons: int, x: int, y: int) -> None:
        pass

    async def type_text(self, text: str) -> bool:
        return True

    async def release_all(self) -> None:
        pass


class Viewer:
    """noVNC's side, decoding Raw, Zlib and Tight into an RGB framebuffer."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.width = self.height = 0
        self.fb = bytearray()
        self._zlib = zlib.decompressobj()
        self._tight = [zlib.decompressobj() for _ in range(4)]
        self.tight_ctls: list[int] = []
        self.wire: list[bytes] = []  # every rect's pixel payload, decoded to RGB

    async def handshake(self, encodings: tuple[int, ...] = NOVNC) -> None:
        r, w = self.reader, self.writer
        w.write(await r.readexactly(12))
        await r.readexactly((await r.readexactly(1))[0])
        w.write(b"\x01")
        assert await r.readexactly(4) == b"\0\0\0\0"
        w.write(b"\x01")
        head = await r.readexactly(24)
        self.width, self.height = struct.unpack(">HH", head[:4])
        await r.readexactly(struct.unpack(">I", head[20:24])[0])
        self.fb = bytearray(self.width * self.height * 3)
        rs, gs, bs = NOVNC_SHIFTS
        w.write(b"\0\0\0\0" + struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, rs, gs, bs))
        w.write(struct.pack(">BxH", 2, len(encodings)) + b"".join(struct.pack(">i", e) for e in encodings))
        await w.drain()

    def request(self, incremental: bool) -> None:
        self.writer.write(struct.pack(">BBHHHH", 3, int(incremental), 0, 0, self.width, self.height))

    def key(self, keysym: int, down: bool) -> None:
        self.writer.write(struct.pack(">BBxxI", 4, int(down), keysym))

    async def flush(self) -> None:
        await self.writer.drain()

    async def _compact(self) -> int:
        size = 0
        for shift in (0, 7, 14):
            b = (await self.reader.readexactly(1))[0]
            size |= (b & (0x7F if shift < 14 else 0xFF)) << shift
            if not b & 0x80 or shift == 14:
                break
        return size

    def _blit(self, x: int, y: int, w: int, h: int, rgb: bytes) -> None:
        assert x + w <= self.width and y + h <= self.height, "a rect outside the screen"
        assert len(rgb) == w * h * 3
        self.wire.append(rgb)
        for r in range(h):
            at = ((y + r) * self.width + x) * 3
            self.fb[at:at + w * 3] = rgb[r * w * 3:(r + 1) * w * 3]

    @staticmethod
    def _rgbx_to_rgb(data: bytes) -> bytes:
        out = bytearray(len(data) // 4 * 3)
        out[0::3], out[1::3], out[2::3] = data[0::4], data[1::4], data[2::4]
        return bytes(out)

    async def read_update(self, timeout: float = 5) -> list[tuple[int, int, int, int, int]]:
        return await asyncio.wait_for(self._read_update(), timeout)

    async def _read_update(self) -> list[tuple[int, int, int, int, int]]:
        r = self.reader
        kind, _, count = struct.unpack(">BBH", await r.readexactly(4))
        assert kind == 0
        rects = []
        for _ in range(count):
            x, y, w, h, enc = struct.unpack(">HHHHi", await r.readexactly(12))
            rects.append((x, y, w, h, enc))
            if enc == COPY_RECT:
                sx, sy = struct.unpack(">HH", await r.readexactly(4))
                assert sx + w <= self.width and sy + h <= self.height, "a copy from outside the screen"
                src = [bytes(self.fb[((sy + i) * self.width + sx) * 3:((sy + i) * self.width + sx + w) * 3])
                       for i in range(h)]
                for i, line in enumerate(src):
                    at = ((y + i) * self.width + x) * 3
                    self.fb[at:at + w * 3] = line
            elif enc == RAW:
                self._blit(x, y, w, h, self._rgbx_to_rgb(await r.readexactly(w * h * 4)))
            elif enc == ZLIB:
                (n,) = struct.unpack(">I", await r.readexactly(4))
                self._blit(x, y, w, h, self._rgbx_to_rgb(self._zlib.decompress(await r.readexactly(n))))
            elif enc == TIGHT:
                ctl = (await r.readexactly(1))[0]
                self.tight_ctls.append(ctl >> 4)
                for i in range(4):
                    if ctl >> i & 1:
                        self._tight[i] = zlib.decompressobj()
                kind = ctl >> 4
                if kind == 0x08:
                    self._blit(x, y, w, h, await r.readexactly(3) * (w * h))
                    continue
                assert kind & 0x08 == 0, f"Tight compression {kind:#x} is not lossless Basic or Fill"
                stream = kind & 0x03
                if kind & 0x04:
                    assert (await r.readexactly(1))[0] == 0, "only the copy filter"
                size = w * h * 3
                if size < 12:
                    self._blit(x, y, w, h, await r.readexactly(size))
                else:
                    data = await r.readexactly(await self._compact())
                    self._blit(x, y, w, h, self._tight[stream].decompress(data))
            elif enc == -239:
                await r.readexactly(w * h * 4 + (w + 7) // 8 * h)
            elif enc == -223:
                self.width, self.height = w, h
                self.fb = bytearray(w * h * 3)
            else:
                raise AssertionError(f"encoding {enc}")
        return rects


def _rgb_of(frame: Frame) -> bytes:
    out = bytearray(frame.width * frame.height * 3)
    i = 0
    for y in range(frame.height):
        for x in range(frame.width):
            out[i:i + 3] = bytes(frame.rgb_at(x, y))
            i += 3
    return bytes(out)


async def _pair() -> tuple[Any, Any]:
    a, b = socket.socketpair()
    return await asyncio.open_connection(sock=a), await asyncio.open_connection(sock=b)


class Gate:
    def __init__(self, width: int, height: int) -> None:
        self.injector = Injector()
        self.hub = rfb.FrameHub(width, height)
        self.control = rfb.ControlState(self.injector)
        self.sessions: dict[str, rfb.RfbSession] = {}
        self.tasks: list[asyncio.Task[None]] = []

    async def connect(self, conn_id: str, encodings: tuple[int, ...] = NOVNC) -> Viewer:
        (sr, sw), (vr, vw) = await _pair()
        session = rfb.RfbSession(conn_id, sr, sw, self.hub, self.control, self.injector)
        self.sessions[conn_id] = session
        self.tasks.append(asyncio.create_task(session.serve()))
        viewer = Viewer(vr, vw)
        await viewer.handshake(encodings)
        return viewer

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def _clean() -> MaskState:
    class _Clean(MaskState):
        def apply(self, frame: Frame):  # type: ignore[override]
            return Outcome.MASKED

    return _Clean()


class _BoxMask(MaskState):
    """A mask over one box -- a password field -- as `MaskState.apply` paints it."""

    def __init__(self, box: tuple[int, int, int, int]) -> None:
        super().__init__()
        self.box = box

    def apply(self, frame: Frame):  # type: ignore[override]
        frame.fill(*self.box, MASK_RGB)
        return Outcome.MASKED


def _page(width: int, height: int, seed: int = 1) -> Frame:
    """A frame with text-like detail, so no encoder can pass it as one colour."""
    frame = Frame.blank(width, height, (255, 255, 255))
    for y in range(4, height - 4, 9):
        for x in range(3, width - 6, 5 + (y + seed) % 4):
            v = (x * 37 + y * 11 + seed * 7) % 200
            frame.fill(x, y, x + 3, y + 5, (v, 255 - v, (v * 3) % 256))
    return frame


# --------------------------------------------------------------------------
# (a) the capture rate follows control
# --------------------------------------------------------------------------


def test_the_capture_rate_is_30_in_control_10_watching_and_0_with_nobody() -> None:
    assert gate_main.capture_fps(watching=False, controlled=False) == 0
    assert gate_main.capture_fps(watching=False, controlled=True) == 0
    assert gate_main.capture_fps(watching=True, controlled=False) == gate_main.WATCH_FPS == 10
    assert gate_main.capture_fps(watching=True, controlled=True) == gate_main.CONTROL_FPS == 30


class _CountingFramebuffer:
    def __init__(self) -> None:
        self.grabs = 0
        self.threads: set[int] = set()

    def grab(self) -> Frame:
        self.grabs += 1
        self.threads.add(threading.get_ident())
        return Frame.blank(1440, 987, (255, 255, 255))

    def close(self) -> None:
        pass


def _gate(tmp: Path) -> Any:
    for name in ("profile", "downloads", "uploads"):
        (tmp / name).mkdir(exist_ok=True)
    cfg = gate_main.GateConfig(ctl_path=str(tmp / "ctl.sock"), profile_dir=tmp / "profile",
                               downloads_dir=tmp / "downloads", uploads_dir=tmp / "uploads",
                               cdp_host="127.0.0.1", cdp_port=9222,
                               relay_url="ws://relay.test:8080/v1/tunnel", fbdir=tmp)
    return gate_main.LiveGate(cfg, gate_main.Deps(), record=lambda event, fields: None)


def test_the_capture_loop_paces_itself_by_control_and_grabs_off_the_loop(tmp_path: Path) -> None:
    """What the loop asks to wait between frames, read off `_pace`, so the
    test holds the rate and not the runner's load (CI runs it under -n)."""

    async def main() -> None:
        gate = _gate(tmp_path)
        fb = gate.framebuffer = _CountingFramebuffer()
        gate.control = rfb.ControlState(Injector())
        gate.viewers = 1
        gate._watching.set()
        loop = asyncio.get_running_loop()
        waits: list[float] = []

        async def pace(until: float) -> None:
            waits.append(until - loop.time())
            await asyncio.sleep(0.001)

        gate._pace = pace
        loop_thread = threading.get_ident()
        capture = asyncio.create_task(gate._capture())
        while len(waits) < 5:
            await asyncio.sleep(0.005)
        watching, waits[:] = list(waits), []
        await gate.control.grant("person-1")
        while len(waits) < 5:
            await asyncio.sleep(0.005)
        controlled = list(waits[1:])  # the first may be a frame started before the grant
        gate._watching.clear()
        await asyncio.sleep(0.05)
        grabs = fb.grabs
        await asyncio.sleep(0.1)
        nobody = fb.grabs - grabs
        capture.cancel()
        await asyncio.gather(capture, return_exceptions=True)
        # A frame's wait is what is left of 1/fps after the grab and the mask.
        assert all(w <= 1 / 10 + 0.005 for w in watching) and max(watching) > 1 / 30, watching
        assert all(w <= 1 / 30 + 0.005 for w in controlled), controlled
        assert nobody == 0
        # (d) the grab is a 4 MB copy: never on the loop that reads the person's input.
        assert fb.threads and loop_thread not in fb.threads

    asyncio.run(main())


def test_the_wait_between_frames_ends_when_control_changes_hands(tmp_path: Path) -> None:
    async def main() -> None:
        gate = _gate(tmp_path)
        loop = asyncio.get_running_loop()
        started = loop.time()
        waiting = asyncio.create_task(gate._pace(started + 5))
        await asyncio.sleep(0.01)
        gate.control_changed()
        await asyncio.wait_for(waiting, 2)
        assert loop.time() - started < 2

    asyncio.run(main())


# --------------------------------------------------------------------------
# (b) only the tiles that changed, and never an unmasked pixel
# --------------------------------------------------------------------------


def test_an_update_carries_only_the_tiles_that_changed() -> None:
    async def main() -> None:
        gate = Gate(320, 200)
        base = _page(320, 200)
        gate.hub.publish(rfb.masked(base, _clean()))
        viewer = await gate.connect("person-1")
        viewer.request(False)
        await viewer.flush()
        await viewer.read_update()
        changed = _page(320, 200)
        changed.fill(200, 70, 207, 82, (1, 2, 3))  # one character typed, inside tile (3, 1)
        gate.hub.publish(rfb.masked(changed, _clean()))
        viewer.request(True)
        await viewer.flush()
        rects = [r for r in await viewer.read_update() if r[4] >= 0]
        assert len(rects) == 1, rects
        x, y, w, h, _ = rects[0]
        # Inside the one 64x64 tile it touched, and covering the change.
        assert 192 <= x and x + w <= 256 and 64 <= y and y + h <= 128, rects
        assert x <= 200 and 207 <= x + w and y <= 70 and 82 <= y + h
        assert viewer.fb == _rgb_of(changed)
        await gate.close()

    asyncio.run(main())


def test_a_masked_area_whose_contents_change_stays_masked_in_the_update() -> None:
    async def main() -> None:
        box = (40, 30, 150, 60)  # a password field, across two tiles
        mask = _BoxMask(box)
        gate = Gate(256, 128)
        secret = (0x5E, 0xC2, 0xE7)
        first = _page(256, 128)
        first.fill(*box, secret)
        gate.hub.publish(rfb.masked(first, mask))
        viewer = await gate.connect("person-1")
        recorder = await gate.connect("rec-1", encodings=(ZLIB, RAW, -223))
        for v in (viewer, recorder):
            v.request(False)
            await v.flush()
            await v.read_update()
        # The person types: what is under the mask changes, and so does a
        # character next to it, in the same tiles.
        for i in range(5):
            typed = _page(256, 128)
            typed.fill(*box, secret)
            typed.fill(42 + 9 * i, 36, 48 + 9 * i, 50, (i, 9, 250 - i))  # under the mask: a colour _page never draws
            typed.fill(160 + 4 * i, 40, 163 + 4 * i, 50, (0, 0, 0))  # beside it
            expected = rfb.masked(typed, mask)
            gate.hub.publish(expected)
            for v in (viewer, recorder):
                v.request(True)
                await v.flush()
                await v.read_update()
                assert v.fb == _rgb_of(expected.frame), "the viewer holds exactly the masked frame"
        hidden = {bytes(secret)} | {bytes((i, 9, 250 - i)) for i in range(5)}
        for v in (viewer, recorder):
            # Every pixel of every rect either viewer was sent, as the viewer reads them.
            sent = {rgb[at:at + 3] for rgb in v.wire for at in range(0, len(rgb), 3)}
            assert not sent & hidden
        await gate.close()

    asyncio.run(main())


def _scrolled_page(width: int, height: int, offset: int, chrome: int = 24) -> Frame:
    """A browser: a fixed bar on top, a tall document under it scrolled by `offset` rows."""
    tall = _page(width, height * 3, seed=11)
    frame = Frame.blank(width, height, (230, 230, 235))
    row = width * 4
    body = height - chrome
    frame.pixels[chrome * row:height * row] = tall.pixels[offset * row:(offset + body) * row]
    # A scrollbar thumb on the right edge, moving against the scroll.
    thumb = chrome + offset // 3
    frame.fill(width - 10, thumb, width - 2, thumb + 30, (120, 120, 120))
    return frame


def test_a_scroll_is_sent_as_a_copy_of_what_the_viewer_has_and_stays_masked() -> None:
    async def main() -> None:
        width, height = 320, 240
        secret = (0x5E, 0xC2, 0xE7)
        gate = Gate(width, height)
        mask = _BoxMask((60, 100, 200, 130))  # a password field the page scrolls under
        first = _scrolled_page(width, height, 0)
        first.fill(60, 100, 200, 130, secret)
        gate.hub.publish(rfb.masked(first, mask))
        viewer = await gate.connect("person-1")
        viewer.request(False)
        await viewer.flush()
        await viewer.read_update()
        full = gate.sessions["person-1"].counts.bytes_sent
        for step in range(1, 6):
            frame = _scrolled_page(width, height, 17 * step)
            frame.fill(60, 100, 200, 130, secret)
            expected = rfb.masked(frame, mask)
            gate.hub.publish(expected)
            before = gate.sessions["person-1"].counts.bytes_sent
            viewer.request(True)
            await viewer.flush()
            rects = await viewer.read_update()
            assert viewer.fb == _rgb_of(expected.frame), "the viewer holds exactly the masked frame"
            copies = [r for r in rects if r[4] == COPY_RECT]
            assert copies, rects
            # The rows scrolled in, the scrollbar, and the rows beside the fixed
            # mask (their old copy holds the mask, so they cannot be copied):
            # on a frame this small that is still a good share of it.
            assert gate.sessions["person-1"].counts.bytes_sent - before < full / 2
        sent = {rgb[at:at + 3] for rgb in viewer.wire for at in range(0, len(rgb), 3)}
        assert bytes(secret) not in sent
        await gate.close()

    asyncio.run(main())


def test_a_viewer_that_does_not_offer_copy_rect_is_never_sent_one() -> None:
    async def main() -> None:
        gate = Gate(320, 240)
        gate.hub.publish(rfb.masked(_scrolled_page(320, 240, 0), _clean()))
        recorder = await gate.connect("rec-1", encodings=(ZLIB, RAW, -223))
        recorder.request(False)
        await recorder.flush()
        await recorder.read_update()
        nxt = _scrolled_page(320, 240, 40)
        gate.hub.publish(rfb.masked(nxt, _clean()))
        recorder.request(True)
        await recorder.flush()
        rects = await recorder.read_update()
        assert {r[4] for r in rects} == {ZLIB}
        assert recorder.fb == _rgb_of(nxt)
        await gate.close()

    asyncio.run(main())


# --------------------------------------------------------------------------
# Incremental requests: the viewer pulls at its own pace
# --------------------------------------------------------------------------


def test_frames_published_while_a_viewer_reads_are_not_queued_for_it() -> None:
    async def main() -> None:
        gate = Gate(128, 64)
        gate.hub.publish(rfb.masked(_page(128, 64), _clean()))
        viewer = await gate.connect("person-1")
        viewer.request(False)
        await viewer.flush()
        await viewer.read_update()
        viewer.request(True)
        await viewer.flush()
        last = None
        for i in range(30):
            last = _page(128, 64, seed=i + 2)
            gate.hub.publish(rfb.masked(last, _clean()))
            await asyncio.sleep(0.005)
        await viewer.read_update()  # the one it asked for
        with pytest.raises(asyncio.TimeoutError):
            await viewer.read_update(timeout=0.3)  # and nothing it did not
        # The next request is answered with the newest frame, not one in between.
        viewer.request(True)
        await viewer.flush()
        await viewer.read_update()
        assert viewer.fb == _rgb_of(last)
        assert gate.sessions["person-1"].counts.updates == 3
        await gate.close()

    asyncio.run(main())


# --------------------------------------------------------------------------
# (c) Tight for noVNC, lossless
# --------------------------------------------------------------------------


def test_novnc_is_sent_tight_and_rebuilds_the_frame_byte_for_byte() -> None:
    async def main() -> None:
        gate = Gate(300, 170)  # not a multiple of the tile: edge tiles are partial
        frame = _page(300, 170)
        frame.fill(0, 64, 128, 128, (30, 40, 50))  # two whole tiles of one colour: Fill
        frame.fill(299, 169, 300, 170, (7, 8, 9))  # a lone pixel in the last tile
        gate.hub.publish(rfb.masked(frame, _clean()))
        viewer = await gate.connect("person-1")
        viewer.request(False)
        await viewer.flush()
        rects = await viewer.read_update()
        assert {r[4] for r in rects if r[4] >= 0} == {TIGHT}
        assert viewer.fb == _rgb_of(frame)
        assert 0x08 in viewer.tight_ctls, "a one-colour tile goes as Fill"
        assert 0x09 not in viewer.tight_ctls and 0x0A not in viewer.tight_ctls, "never JPEG or PNG"
        # And again incrementally, through the same zlib stream.
        for seed in (3, 4, 5):
            nxt = _page(300, 170, seed=seed)
            gate.hub.publish(rfb.masked(nxt, _clean()))
            viewer.request(True)
            await viewer.flush()
            await viewer.read_update()
            assert viewer.fb == _rgb_of(nxt)
        await gate.close()

    asyncio.run(main())


def test_the_recorder_still_gets_zlib_and_rebuilds_the_frame_byte_for_byte() -> None:
    async def main() -> None:
        gate = Gate(200, 130)
        frame = _page(200, 130)
        gate.hub.publish(rfb.masked(frame, _clean()))
        recorder = await gate.connect("rec-1", encodings=(ZLIB, RAW, -223))
        recorder.request(False)
        await recorder.flush()
        rects = await recorder.read_update()
        assert {r[4] for r in rects} == {ZLIB}
        assert recorder.fb == _rgb_of(frame)
        nxt = _page(200, 130)
        nxt.fill(10, 100, 30, 110, (0, 0, 0))
        gate.hub.publish(rfb.masked(nxt, _clean()))
        recorder.request(True)
        await recorder.flush()
        rects = await recorder.read_update()
        assert all(w <= 64 for _x, _y, w, _h, _e in rects)
        assert recorder.fb == _rgb_of(nxt)
        await gate.close()

    asyncio.run(main())


# --------------------------------------------------------------------------
# (d) input is never behind a frame
# --------------------------------------------------------------------------


def test_a_key_reaches_the_display_while_an_update_is_being_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    encoding = threading.Event()
    original = rfb.RfbSession._render

    def slow_render(self: rfb.RfbSession, *args: Any, **kwargs: Any) -> Any:
        encoding.set()
        release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(rfb.RfbSession, "_render", slow_render)

    async def main() -> None:
        gate = Gate(128, 64)
        gate.hub.publish(rfb.masked(_page(128, 64), _clean()))
        viewer = await gate.connect("person-1")
        await gate.control.grant("person-1")
        viewer.request(False)
        await viewer.flush()
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, encoding.wait, 5), "the update never started encoding"
        viewer.key(0x61, True)
        viewer.key(0x61, False)
        await viewer.flush()
        for _ in range(100):
            if len(gate.injector.keys) == 2:
                break
            await asyncio.sleep(0.01)
        assert [(k, d) for k, d, _ in gate.injector.keys] == [(0x61, True), (0x61, False)]
        assert not release.is_set()  # the key went while the frame was still encoding
        release.set()
        await viewer.read_update()
        await gate.close()

    try:
        asyncio.run(main())
    finally:
        release.set()


def test_the_time_from_a_key_to_the_first_frame_after_it_is_measured() -> None:
    async def main() -> None:
        gate = Gate(128, 64)
        gate.hub.publish(rfb.masked(_page(128, 64), _clean()))
        viewer = await gate.connect("person-1")
        await gate.control.grant("person-1")
        viewer.request(False)
        await viewer.flush()
        await viewer.read_update()
        viewer.key(0x61, True)
        viewer.request(True)
        await viewer.flush()
        await asyncio.sleep(0.05)
        drawn = _page(128, 64)
        drawn.fill(10, 10, 17, 22, (0, 0, 0))
        gate.hub.publish(rfb.masked(drawn, _clean()))
        await viewer.read_update()
        counts = gate.sessions["person-1"].counts
        assert len(counts.input_to_frame_ms) == 1
        assert 40 <= counts.input_to_frame_ms[0] < 2000
        assert counts.bytes_sent > 0
        await gate.close()

    asyncio.run(main())


# --------------------------------------------------------------------------
# Recordings: the relay's own decoder still rebuilds every frame
# --------------------------------------------------------------------------


def test_the_relays_recorder_rebuilds_every_masked_frame_from_tiled_updates() -> None:
    """The recorder (swarm_live/recorder.py) is an RFB client of its own; what
    it decodes is what the recording holds. Fed the gate's tiled Zlib updates,
    it must hold exactly the masked frame after each one, scroll and all."""
    # swarm-live is not a member of the root uv workspace; tests/unit/live/test_relay.py
    # puts it on the path the same way.
    app = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
    if app not in sys.path:
        sys.path.insert(0, app)
    from swarm_live.recorder import RfbDecoder

    async def main() -> None:
        width, height = 320, 240
        mask = _BoxMask((60, 100, 200, 130))
        gate = Gate(width, height)
        gate.hub.publish(rfb.masked(_scrolled_page(width, height, 0), mask))
        (sr, sw), (cr, cw) = await _pair()
        session = rfb.RfbSession("rec-1", sr, sw, gate.hub, gate.control, gate.injector)
        task = asyncio.create_task(session.serve())
        decoder = RfbDecoder()

        async def until(done: Any) -> None:
            for _ in range(500):
                data = await asyncio.wait_for(cr.read(1 << 20), 5)
                decoder.feed(data)
                cw.write(decoder.take_outbound())
                await cw.drain()
                if done():
                    return
            raise AssertionError("the recorder never caught up")

        await until(lambda: decoder.ready)
        for step in range(1, 6):
            expected = rfb.masked(_scrolled_page(width, height, 23 * step), mask)
            seen = decoder.version
            gate.hub.publish(expected)
            decoder.request(incremental=True)
            cw.write(decoder.take_outbound())
            await cw.drain()
            await until(lambda: decoder.version > seen)
            assert decoder.rgb() == _rgb_of(expected.frame)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cw.close()

    asyncio.run(main())


def test_a_full_request_made_while_an_unchanged_frame_renders_is_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The render runs on a thread now, so a request can arrive during it. A
    full refresh asked for then must not be lost when that render finds
    nothing changed (review of STREAM-SMOOTH)."""
    release = threading.Event()
    rendering = threading.Event()
    original = rfb.RfbSession._render

    def held(self: rfb.RfbSession, *args: Any, **kwargs: Any) -> Any:
        rendering.set()
        release.wait(5)
        return original(self, *args, **kwargs)

    async def main() -> None:
        gate = Gate(128, 64)
        page = _page(128, 64)
        gate.hub.publish(rfb.masked(page, _clean()))
        viewer = await gate.connect("person-1")
        viewer.request(False)
        await viewer.flush()
        await viewer.read_update()
        monkeypatch.setattr(rfb.RfbSession, "_render", held)
        viewer.request(True)
        await viewer.flush()
        gate.hub.publish(rfb.masked(_page(128, 64), _clean()))  # the same picture again
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, rendering.wait, 5)
        viewer.request(False)  # a full refresh, while that render runs
        await viewer.flush()
        release.set()
        rects = await viewer.read_update(timeout=2)
        assert sum(w * h for _x, _y, w, h, enc in rects if enc >= 0) == 128 * 64
        await gate.close()

    try:
        asyncio.run(main())
    finally:
        release.set()
