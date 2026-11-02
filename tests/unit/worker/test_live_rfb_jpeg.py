"""Adaptive JPEG in the gate's Tight encoder, on at quality 70 (LIVE-STREAM-SPEED, part of #2688).

MEASURED 2026-11-02 in the owner's Chrome: the live views moved at under one
frame a second, and the owner picked adaptive JPEG at q70 over STREAM-SMOOTH's
lossless-only Tight. By default (`SWARM_LIVE_JPEG_QUALITY` unset), an update whose changes cover at least
`JPEG_MOTION_SHARE` of the frame sends its large rectangles as Tight JPEG,
a small change (a caret, a typed character) stays lossless, and once the
frame stands still for `JPEG_SETTLE_SECONDS` the whole frame is sent again
losslessly.

The JPEG encoder is a fake: unit tests run without Pillow, and what is
asserted is the gate's wire -- Tight's JPEG control byte, the compact length,
the encoder's bytes -- and when it is chosen.

`SWARM_LIVE_JPEG_QUALITY=0` keeps lossless-only Tight.

MUTATIONS: default the quality to anything but 70 (1 and 2 fail); read 0 as
the default (2 and 6 fail); drop the motion
share check (3 fails); drop the settle refresh (4 fails); ignore an encoder
that answers None (5 fails).
"""

from __future__ import annotations

import asyncio
import socket
import struct
import zlib
from typing import Any

from agent_worker.livegate import rfb
from agent_worker.livegate.capture import Frame

from test_live_rfb import RecordingInjector

TIGHT = 7
W, H = 256, 128


def _noise(seed: int, x0: int = 0, y0: int = 0, x1: int = W, y1: int = H) -> Frame:
    """A frame of a flat colour with a box of non-repeating pixels, so it is never a Fill."""
    frame = Frame.blank(W, H, (240, 240, 240))
    for y in range(y0, y1):
        for x in range(x0, x1):
            at = y * frame.stride + x * 4
            frame.pixels[at:at + 3] = bytes(((x * 7 + seed) % 256, (y * 13 + seed) % 256,
                                             (x * y + seed) % 256))
    return frame


class TightViewer:
    """noVNC's side, as far as Tight's Fill, Basic and JPEG go."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.inflate = zlib.decompressobj()

    async def handshake(self) -> None:
        version = await self.reader.readexactly(12)
        self.writer.write(version)
        count = (await self.reader.readexactly(1))[0]
        await self.reader.readexactly(count)
        self.writer.write(bytes([1]))
        await self.reader.readexactly(4)
        self.writer.write(bytes([1]))
        head = await self.reader.readexactly(24)
        (name_len,) = struct.unpack(">I", head[20:24])
        await self.reader.readexactly(name_len)
        self.writer.write(struct.pack(">BxHi", 2, 1, TIGHT))
        await self.writer.drain()

    def request(self, incremental: bool) -> None:
        self.writer.write(struct.pack(">BBHHHH", 3, int(incremental), 0, 0, W, H))

    async def _compact(self) -> int:
        n, shift = 0, 0
        for _ in range(3):
            b = (await self.reader.readexactly(1))[0]
            n |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        return n

    async def read_update(self) -> list[tuple[str, int, int, bytes]]:
        """(kind, w, h, payload) per rectangle: kind is fill, basic or jpeg."""
        kind = (await self.reader.readexactly(1))[0]
        assert kind == 0
        await self.reader.readexactly(1)
        (n,) = struct.unpack(">H", await self.reader.readexactly(2))
        out = []
        for _ in range(n):
            _x, _y, w, h, enc = struct.unpack(">HHHHi", await self.reader.readexactly(12))
            assert enc == TIGHT, enc
            ctl = (await self.reader.readexactly(1))[0]
            if ctl == 0x80:
                out.append(("fill", w, h, await self.reader.readexactly(3)))
            elif ctl == 0x90:
                out.append(("jpeg", w, h, await self.reader.readexactly(await self._compact())))
            else:
                if ctl & 0x01:
                    self.inflate = zlib.decompressobj()
                if w * h * 3 < 12:
                    out.append(("basic", w, h, await self.reader.readexactly(w * h * 3)))
                else:
                    data = await self.reader.readexactly(await self._compact())
                    out.append(("basic", w, h, self.inflate.decompress(data)))
        return out


class FakeJpeg:
    def __init__(self, answer: bool = True) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.answer = answer

    def __call__(self, rgb: bytes, width: int, height: int, quality: int) -> bytes | None:
        assert len(rgb) == width * height * 3
        self.calls.append((width, height, quality))
        return (b"\xff\xd8" + struct.pack(">HHB", width, height, quality) + b"\xff\xd9") if self.answer else None


async def _connect(hub: rfb.FrameHub, **kwargs: Any) -> TightViewer:
    a, b = socket.socketpair()
    sr, sw = await asyncio.open_connection(sock=a)
    vr, vw = await asyncio.open_connection(sock=b)
    injector = RecordingInjector()
    session = rfb.RfbSession("viewer-a", sr, sw, hub, rfb.ControlState(injector), injector, **kwargs)
    task = asyncio.create_task(session.serve())
    viewer = TightViewer(vr, vw)
    viewer.task = task  # type: ignore[attr-defined]
    await viewer.handshake()
    return viewer


def _run(body: Any) -> None:
    async def main() -> None:
        hub = rfb.FrameHub(W, H)
        hub.publish(_noise(0))
        await body(hub)

    asyncio.run(asyncio.wait_for(main(), 10))


def test_1_on_by_default_a_moving_page_is_jpeg_at_q70(monkeypatch) -> None:
    monkeypatch.delenv(rfb.JPEG_QUALITY_ENV, raising=False)
    jpeg = FakeJpeg()

    async def body(hub: rfb.FrameHub) -> None:
        viewer = await _connect(hub, jpeg_encoder=jpeg)
        viewer.request(False)
        await viewer.read_update()
        hub.publish(_noise(99))
        viewer.request(True)
        assert "jpeg" in {k for k, *_ in await viewer.read_update()}
        viewer.task.cancel()  # type: ignore[attr-defined]

    _run(body)
    assert jpeg.calls and {q for *_, q in jpeg.calls} == {70}


def test_2_the_switch_is_read_from_the_environment() -> None:
    env = rfb.JPEG_QUALITY_ENV
    assert rfb.JPEG_QUALITY_DEFAULT == 70
    assert rfb.jpeg_quality_from_env({}) == 70
    assert rfb.jpeg_quality_from_env({env: ""}) == 70
    assert rfb.jpeg_quality_from_env({env: "lossless"}) == 70
    assert rfb.jpeg_quality_from_env({env: "0"}) == 0
    assert rfb.jpeg_quality_from_env({env: "60"}) == 60
    assert rfb.jpeg_quality_from_env({env: "400"}) == 95
    assert rfb.jpeg_quality_from_env({env: "-3"}) == 0


def test_6_zero_in_the_environment_is_lossless_only(monkeypatch) -> None:
    monkeypatch.setenv(rfb.JPEG_QUALITY_ENV, "0")
    jpeg = FakeJpeg()

    async def body(hub: rfb.FrameHub) -> None:
        viewer = await _connect(hub, jpeg_encoder=jpeg)
        viewer.request(False)
        assert {k for k, *_ in await viewer.read_update()} <= {"fill", "basic"}
        hub.publish(_noise(99))
        viewer.request(True)
        assert {k for k, *_ in await viewer.read_update()} <= {"fill", "basic"}
        viewer.task.cancel()  # type: ignore[attr-defined]

    _run(body)
    assert jpeg.calls == []


def test_3_a_moving_page_is_jpeg_and_a_typed_character_is_not() -> None:
    jpeg = FakeJpeg()

    async def body(hub: rfb.FrameHub) -> None:
        viewer = await _connect(hub, jpeg_quality=60, jpeg_encoder=jpeg)
        viewer.request(False)
        first = await viewer.read_update()
        assert any(k == "jpeg" for k, *_ in first), "the first picture of a moving page"
        # The whole page moved: JPEG, at the quality asked for.
        hub.publish(_noise(50))
        viewer.request(True)
        moved = await viewer.read_update()
        assert any(k == "jpeg" for k, *_ in moved)
        assert all(q == 60 for _w, _h, q in jpeg.calls)
        # One character's box changed: under the motion share, lossless.
        typed = _noise(50)
        typed.fill(10, 10, 18, 26, (0, 0, 0))
        hub.publish(typed)
        viewer.request(True)
        assert {k for k, *_ in await viewer.read_update()} <= {"fill", "basic"}
        viewer.task.cancel()  # type: ignore[attr-defined]

    _run(body)


def test_4_a_page_that_stops_moving_is_sent_again_losslessly() -> None:
    jpeg = FakeJpeg()

    async def body(hub: rfb.FrameHub) -> None:
        viewer = await _connect(hub, jpeg_quality=60, jpeg_encoder=jpeg)
        viewer.request(False)
        await viewer.read_update()
        hub.publish(_noise(77))
        viewer.request(True)
        assert any(k == "jpeg" for k, *_ in await viewer.read_update())
        # Nothing changes now; the viewer asks as noVNC does, and is answered
        # with the whole frame, losslessly, once it has settled.
        viewer.request(True)
        refined = await asyncio.wait_for(viewer.read_update(), rfb.JPEG_SETTLE_SECONDS + 2)
        assert {k for k, *_ in refined} <= {"fill", "basic"}
        assert sum(w * h for _k, w, h, _p in refined) == W * H
        # Settled and sharp: the next request waits for a change.
        viewer.request(True)
        try:
            await asyncio.wait_for(viewer.read_update(), rfb.JPEG_SETTLE_SECONDS * 2)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("a settled, lossless frame was sent again")
        viewer.task.cancel()  # type: ignore[attr-defined]

    _run(body)


def test_5_without_an_encoder_the_rectangle_is_lossless() -> None:
    jpeg = FakeJpeg(answer=False)

    async def body(hub: rfb.FrameHub) -> None:
        viewer = await _connect(hub, jpeg_quality=60, jpeg_encoder=jpeg)
        viewer.request(False)
        rects = await viewer.read_update()
        assert {k for k, *_ in rects} <= {"fill", "basic"}
        assert sum(w * h for _k, w, h, _p in rects) == W * H
        viewer.task.cancel()  # type: ignore[attr-defined]

    _run(body)
    assert jpeg.calls, "the encoder was asked, and its None was honoured"
