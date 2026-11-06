"""The gate's thumbnail mode: scaled Tight JPEG for the Live Browsers wall (part of #2688).

BGD's pick of 6 November 2026: the Live Browsers wall moves at 10 fps with six
or fewer cards on screen, 5 fps up to 20 and 2 fps beyond. Before this the
console's thumbnail asked for RAW 32 bpp frames at the full 1440x900 size and
shrank them in the browser: 1-5 MB a frame of a moving page. Now a viewer
names `ENCODING_THUMB_WIDTH_0 + width` in its SetEncodings and the gate scales
the frame to that width (aspect kept, at most `THUMB_MAX_WIDTH`), diffs at the
scaled size and sends the changes as Tight JPEG at `THUMB_JPEG_QUALITY`.

The JPEG encoder is a fake (unit tests run without Pillow); it keeps the RGB
it was handed, so the scaled pixels themselves are asserted.

MUTATIONS: ignore the pseudo-encoding (1 fails: full size, no DesktopSize);
drop the clamp (2 fails); encode at any other quality (1 fails); diff at the
full size or always resend (3 fails); route the full-size path through the
thumbnail renderer or change its bytes in any way (4 fails).
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import struct
from typing import Any

from agent_worker.livegate import rfb
from agent_worker.livegate.capture import Frame

from test_live_rfb import RecordingInjector
from test_live_rfb_jpeg import FakeJpeg, TightViewer, _noise

TIGHT = 7
DESKTOP_SIZE = -223
RED, BLUE = (200, 10, 10), (10, 10, 200)


def _halves(width: int, height: int) -> Frame:
    """Red on the left half, blue on the right: what a scale must keep apart."""
    frame = Frame.blank(width, height, RED)
    for y in range(height):
        for x in range(width // 2, width):
            at = y * frame.stride + x * 4
            frame.pixels[at:at + 3] = bytes(BLUE)
    return frame


class KeepingJpeg(FakeJpeg):
    def __init__(self) -> None:
        super().__init__()
        self.rgb: list[bytes] = []

    def __call__(self, rgb: bytes, width: int, height: int, quality: int) -> bytes | None:
        self.rgb.append(rgb)
        return super().__call__(rgb, width, height, quality)


class ThumbViewer:
    """LiveThumb's side: Tight, DesktopSize and the thumbnail pseudo-encoding."""

    def __init__(self, inner: TightViewer) -> None:
        self.inner = inner
        self.reader, self.writer = inner.reader, inner.writer

    def ask(self, width: int) -> None:
        encodings = (TIGHT, DESKTOP_SIZE, rfb.ENCODING_THUMB_WIDTH_0 + width)
        self.writer.write(struct.pack(f">BxH{len(encodings)}i", 2, len(encodings), *encodings))

    def request(self, incremental: bool, w: int = 640, h: int = 640) -> None:
        self.writer.write(struct.pack(">BBHHHH", 3, int(incremental), 0, 0, w, h))

    async def read_update(self) -> list[tuple[str, int, int, int, int, bytes]]:
        """(kind, x, y, w, h, payload) per rectangle: size, fill or jpeg."""
        head = await self.reader.readexactly(4)
        assert head[0] == 0
        (n,) = struct.unpack(">H", head[2:4])
        out = []
        for _ in range(n):
            x, y, w, h, enc = struct.unpack(">HHHHi", await self.reader.readexactly(12))
            if enc == DESKTOP_SIZE:
                out.append(("size", x, y, w, h, b""))
                continue
            assert enc == TIGHT, enc
            ctl = (await self.reader.readexactly(1))[0]
            if ctl == 0x80:
                out.append(("fill", x, y, w, h, await self.reader.readexactly(3)))
            else:
                assert ctl == 0x90, hex(ctl)
                out.append(("jpeg", x, y, w, h,
                            await self.reader.readexactly(await self.inner._compact())))
        return out


async def _thumb(hub: rfb.FrameHub, width: int, **kwargs: Any) -> ThumbViewer:
    a, b = socket.socketpair()
    sr, sw = await asyncio.open_connection(sock=a)
    vr, vw = await asyncio.open_connection(sock=b)
    injector = RecordingInjector()
    session = rfb.RfbSession("thumb-a", sr, sw, hub, rfb.ControlState(injector), injector, **kwargs)
    inner = TightViewer(vr, vw)
    inner.task = asyncio.create_task(session.serve())  # type: ignore[attr-defined]
    await inner.handshake()
    viewer = ThumbViewer(inner)
    viewer.ask(width)
    return viewer


def _run(body: Any, frame: Frame) -> None:
    async def main() -> None:
        hub = rfb.FrameHub(frame.width, frame.height)
        hub.publish(frame)
        await asyncio.wait_for(body(hub), 10)

    asyncio.run(main())


def test_a_thumbnail_request_is_answered_with_scaled_jpeg_rectangles() -> None:
    jpeg = KeepingJpeg()

    async def body(hub: rfb.FrameHub) -> None:
        viewer = await _thumb(hub, 120, jpeg_encoder=jpeg)
        viewer.request(False)
        await viewer.writer.drain()
        # The scaled size first, as DesktopSize: 256x128 at 120 wide is 120x60.
        assert await viewer.read_update() == [("size", 0, 0, 120, 60, b"")]
        viewer.request(False, 120, 60)
        await viewer.writer.drain()
        rects = await viewer.read_update()
        assert rects and all(kind in ("fill", "jpeg") for kind, *_ in rects)
        assert any(kind == "jpeg" for kind, *_ in rects)
        assert all(x + w <= 120 and y + h <= 60 for _k, x, y, w, h, _p in rects)
        # Every pixel of the scaled frame is covered once.
        assert sum(w * h for _k, _x, _y, w, h, _p in rects) == 120 * 60
        assert {q for _w, _h, q in jpeg.calls} == {rfb.THUMB_JPEG_QUALITY} == {60}

    _run(body, _noise(3))


def test_the_thumbnail_keeps_the_aspect_and_is_at_most_640_wide() -> None:
    jpeg = KeepingJpeg()

    async def body(hub: rfb.FrameHub) -> None:
        viewer = await _thumb(hub, 2000, jpeg_encoder=jpeg)
        viewer.request(False)
        await viewer.writer.drain()
        assert await viewer.read_update() == [("size", 0, 0, 640, 400, b"")]
        viewer.request(False, 640, 400)
        await viewer.writer.drain()
        rects = await viewer.read_update()
        # The scale kept the halves apart: the left is red, the right blue.
        fills = {(x, bytes(p)) for kind, x, _y, _w, _h, p in rects if kind == "fill"}
        assert (0, bytes(RED)) in fills
        assert any(x >= 320 and p == bytes(BLUE) for x, p in fills)
        assert all(x + w <= 640 and y + h <= 400 for _k, x, y, w, h, _p in rects)

    assert rfb.THUMB_MAX_WIDTH == 640
    _run(body, _halves(1280, 800))


def test_an_unchanged_frame_sends_nothing_and_a_change_is_sent_scaled() -> None:
    jpeg = KeepingJpeg()

    async def body(hub: rfb.FrameHub) -> None:
        viewer = await _thumb(hub, 128, jpeg_encoder=jpeg)
        viewer.request(False)
        await viewer.writer.drain()
        await viewer.read_update()  # the size
        viewer.request(False, 128, 64)
        await viewer.writer.drain()
        await viewer.read_update()  # the whole thumbnail
        viewer.request(True, 128, 64)
        await viewer.writer.drain()
        hub.publish(_noise(0))  # the same pixels, published again
        try:
            got = await asyncio.wait_for(viewer.reader.read(1), 0.6)
        except TimeoutError:
            got = b""
        assert got == b"", "an unchanged frame was sent"
        hub.publish(_noise(9, 0, 0, 64, 64))  # the top-left quarter changes
        rects = await viewer.read_update()
        # Only the changed part of the 128x64 thumbnail: its left half.
        assert rects and all(x + w <= 64 for _k, x, _y, w, _h, _p in rects)

    _run(body, _noise(0))


#: The Browser tab's full-size Tight bytes for `_full_size_session`, from the
#: gate before thumbnail mode existed: the full-size path must not move a byte.
FULL_SIZE_SHA256 = "19ef5723edc189e494f5bfa477f20c6a2bb85e4600f520b2a7579f9a6bb12112"


async def _full_size_session(hub: rfb.FrameHub) -> bytes:
    viewer = await asyncio.wait_for(_tight(hub), 5)
    got = bytearray()
    for incremental, frame in ((False, None), (True, _noise(5, 10, 10, 200, 100)),
                               (True, _noise(6, 0, 0, 20, 20))):
        if frame is not None:
            hub.publish(frame)
        viewer.request(incremental)
        await viewer.writer.drain()
        before = len(got)
        while len(got) == before or not await _quiet(viewer, got):
            got += await viewer.reader.read(1 << 20)
    return bytes(got)


async def _quiet(viewer: TightViewer, got: bytearray) -> bool:
    try:
        more = await asyncio.wait_for(viewer.reader.read(1 << 20), 0.2)
    except TimeoutError:
        return True
    got += more
    return False


async def _tight(hub: rfb.FrameHub) -> TightViewer:
    a, b = socket.socketpair()
    sr, sw = await asyncio.open_connection(sock=a)
    vr, vw = await asyncio.open_connection(sock=b)
    injector = RecordingInjector()
    session = rfb.RfbSession("viewer-a", sr, sw, hub, rfb.ControlState(injector), injector,
                             jpeg_quality=70, jpeg_encoder=FakeJpeg())
    viewer = TightViewer(vr, vw)
    viewer.task = asyncio.create_task(session.serve())  # type: ignore[attr-defined]
    await viewer.handshake()
    return viewer


def test_the_browser_tabs_full_size_tight_path_is_byte_for_byte_unchanged() -> None:
    out: list[bytes] = []

    async def body(hub: rfb.FrameHub) -> None:
        out.append(await _full_size_session(hub))

    _run(body, _noise(0))
    assert hashlib.sha256(out[0]).hexdigest() == FULL_SIZE_SHA256, len(out[0])
