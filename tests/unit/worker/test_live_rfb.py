"""The live gate's input side: RFB, XTEST injection and ctl.sock (live browser L3).

docs/design/live-browser.md section 5.6 (keyboard and clipboard), 5.4.2 (the
worker talks to the gate on a channel the agent cannot use), 5.3.3 (keys never
logged) and 5.10 (controller input is rate limited at the gate). The
acceptance row of lane L3:

* key events from a viewer, or in view mode, are discarded;
* pod-to-console clipboard is never sent;
* a second `ctl.sock` registration is refused, and a restarted gate refuses
  even the first;
* captured logs contain no keysym.

Every RFB test drives a real `RfbSession` over a real socket pair with a
scripted viewer, and reads what reached the injector -- so "the key was
discarded" is read off what would have reached Xvfb. The XTEST tests drive the
real `XInjector` against a scripted X server on a unix socket, and read the
requests it received. No test runs Xvfb, Chromium, a network or a cloud
credential; every key, paste and ticket is built at run time.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import socket
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import ctl, inject, rfb
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.mask import UNAVAILABLE_RGB, MaskState

LIVEGATE_LOGGER = "agent_worker.livegate"
#: The Cursor pseudo-encoding (RFC 6143 §7.8.1), spelled here so a renamed constant cannot hide it.
CURSOR = -239


@pytest.fixture()
def short_dir():
    """A directory whose socket paths fit in sun_path's 108 bytes."""
    path = Path(tempfile.mkdtemp(prefix="lg", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# A scripted injector and a scripted RFB viewer
# --------------------------------------------------------------------------


class RecordingInjector:
    """Records what would have reached Xvfb through XTEST."""

    def __init__(self) -> None:
        self.keys: list[tuple[int, bool]] = []
        self.pointer: list[tuple[int, int, int]] = []
        self.typed: list[str] = []
        self.released = 0

    async def key(self, keysym: int, down: bool) -> None:
        self.keys.append((keysym, down))

    async def pointer_event(self, buttons: int, x: int, y: int) -> None:
        self.pointer.append((buttons, x, y))

    async def type_text(self, text: str) -> bool:
        self.typed.append(text)
        return True

    async def release_all(self) -> None:
        self.released += 1

    @property
    def anything(self) -> bool:
        return bool(self.keys or self.pointer or self.typed)


class Viewer:
    """The console's side of one RFB connection, as noVNC speaks it."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.width = 0
        self.height = 0
        self.server_format: bytes = b""
        self.message_types: list[int] = []

    async def handshake(self, encodings: tuple[int, ...] = (0,)) -> None:
        version = await self.reader.readexactly(12)
        assert version == b"RFB 003.008\n"
        self.writer.write(version)
        count = (await self.reader.readexactly(1))[0]
        types = await self.reader.readexactly(count)
        assert types == bytes([1])  # None: the relay and swarm-api authenticate
        self.writer.write(bytes([1]))
        assert await self.reader.readexactly(4) == b"\0\0\0\0"
        self.writer.write(bytes([1]))  # shared
        head = await self.reader.readexactly(24)
        self.width, self.height = struct.unpack(">HH", head[:4])
        self.server_format = head[4:20]
        (name_len,) = struct.unpack(">I", head[20:24])
        await self.reader.readexactly(name_len)
        self.writer.write(struct.pack(">BxH", 2, len(encodings))
                          + b"".join(struct.pack(">i", e) for e in encodings))
        await self.writer.drain()

    def set_pixel_format(self, *, big_endian: bool, shifts: tuple[int, int, int]) -> None:
        r, g, b = shifts
        fmt = struct.pack(">BBBBHHHBBB3x", 32, 24, int(big_endian), 1, 255, 255, 255, r, g, b)
        self.writer.write(b"\0\0\0\0" + fmt)

    def request(self, incremental: bool = False) -> None:
        self.writer.write(struct.pack(">BBHHHH", 3, int(incremental), 0, 0, self.width, self.height))

    def key(self, keysym: int, down: bool) -> None:
        self.writer.write(struct.pack(">BBxxI", 4, int(down), keysym))

    def pointer(self, buttons: int, x: int, y: int) -> None:
        self.writer.write(struct.pack(">BBHH", 5, buttons, x, y))

    def cut_text(self, text: str) -> None:
        data = text.encode("latin-1")
        self.writer.write(struct.pack(">BxxxI", 6, len(data)) + data)

    async def flush(self) -> None:
        await self.writer.drain()
        # Let the session's reader consume what was written.
        for _ in range(20):
            await asyncio.sleep(0)
        await asyncio.sleep(0.02)

    async def read_update(self) -> list[tuple[int, int, int, int, int, bytes]]:
        """One server message; it must be a FramebufferUpdate."""
        kind = (await self.reader.readexactly(1))[0]
        self.message_types.append(kind)
        assert kind == 0, f"server sent message type {kind}"
        await self.reader.readexactly(1)
        (n,) = struct.unpack(">H", await self.reader.readexactly(2))
        rects = []
        for _ in range(n):
            x, y, w, h, enc = struct.unpack(">HHHHi", await self.reader.readexactly(12))
            if enc == 0:
                data = await self.reader.readexactly(w * h * 4)
            elif enc == 6:
                (length,) = struct.unpack(">I", await self.reader.readexactly(4))
                data = await self.reader.readexactly(length)
            elif enc == CURSOR:
                # RFC 6143 §7.8.1: the shape's pixels, then its bitmask.
                data = await self.reader.readexactly(w * h * 4 + (w + 7) // 8 * h)
            else:
                data = b""
            rects.append((x, y, w, h, enc, data))
        return rects

    async def drain_server(self) -> bytes:
        """Everything else the server sends before the connection closes."""
        self.writer.close()
        try:
            return await asyncio.wait_for(self.reader.read(), 2)
        except (ConnectionError, asyncio.TimeoutError):
            return b""


async def _pair() -> tuple[tuple[asyncio.StreamReader, asyncio.StreamWriter],
                           tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    a, b = socket.socketpair()
    return await asyncio.open_connection(sock=a), await asyncio.open_connection(sock=b)


def _frame(width: int = 64, height: int = 40, rgb: tuple[int, int, int] = (10, 20, 30)) -> Frame:
    return Frame.blank(width, height, rgb)


class Gate:
    """The gate's RFB side with a hub, a control state and a recording injector."""

    def __init__(self, width: int = 64, height: int = 40) -> None:
        self.injector = RecordingInjector()
        self.hub = rfb.FrameHub(width, height)
        self.control = rfb.ControlState(self.injector)
        self.sessions: dict[str, rfb.RfbSession] = {}
        self.tasks: list[asyncio.Task[None]] = []

    async def connect(self, conn_id: str, encodings: tuple[int, ...] = (0,), **kwargs: Any) -> Viewer:
        (sr, sw), (vr, vw) = await _pair()
        session = rfb.RfbSession(conn_id, sr, sw, self.hub, self.control, self.injector, **kwargs)
        self.sessions[conn_id] = session
        self.tasks.append(asyncio.create_task(session.serve()))
        viewer = Viewer(vr, vw)
        await viewer.handshake(encodings)
        return viewer

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


# --------------------------------------------------------------------------
# Key events from a viewer, or in view mode, are discarded (section 5.6)
# --------------------------------------------------------------------------


def test_view_mode_discards_every_input_event() -> None:
    async def main() -> None:
        gate = Gate()
        viewer = await gate.connect("viewer-a")
        viewer.key(0x61, True)
        viewer.key(0x61, False)
        viewer.pointer(1, 5, 5)
        viewer.pointer(0, 6, 6)
        viewer.cut_text(secrets.token_urlsafe(12))
        await viewer.flush()
        assert gate.control.controller is None
        assert not gate.injector.anything
        session = gate.sessions["viewer-a"]
        assert session.counts.dropped == 5
        assert session.counts.key_events == 0
        await gate.close()

    asyncio.run(main())


def test_only_the_controllers_input_is_injected() -> None:
    async def main() -> None:
        gate = Gate()
        controller = await gate.connect("person-1")
        bystander = await gate.connect("person-2")
        await gate.control.grant("person-1")

        bystander.key(0x62, True)
        bystander.pointer(1, 3, 4)
        bystander.cut_text(secrets.token_urlsafe(12))
        await bystander.flush()
        controller.key(0x61, True)
        controller.key(0x61, False)
        controller.pointer(1, 7, 8)
        await controller.flush()

        assert gate.injector.keys == [(0x61, True), (0x61, False)]
        assert gate.injector.pointer == [(1, 7, 8)]
        assert gate.injector.typed == []
        assert gate.sessions["person-2"].counts.dropped == 3
        assert gate.sessions["person-1"].counts.key_events == 2
        assert gate.sessions["person-1"].counts.pointer_events == 1
        await gate.close()

    asyncio.run(main())


def test_control_revoked_returns_the_connection_to_view_mode_and_releases_keys() -> None:
    async def main() -> None:
        gate = Gate()
        person = await gate.connect("person-1")
        await gate.control.grant("person-1")
        person.key(0xFFE1, True)  # Shift held when control ends
        await person.flush()
        await gate.control.revoke()
        assert gate.injector.released == 1  # nothing stays pressed in Xvfb

        person.key(0x61, True)
        person.pointer(1, 1, 1)
        await person.flush()
        assert gate.injector.keys == [(0xFFE1, True)]
        assert gate.injector.pointer == []
        await gate.close()

    asyncio.run(main())


def test_handing_control_to_another_person_releases_the_first_persons_keys() -> None:
    async def main() -> None:
        gate = Gate()
        first = await gate.connect("person-1")
        second = await gate.connect("person-2")
        await gate.control.grant("person-1")
        first.key(0xFFE3, True)  # Control_L held
        await first.flush()
        await gate.control.grant("person-2")
        assert gate.injector.released == 1
        first.key(0x61, True)
        second.key(0x62, True)
        await first.flush()
        await second.flush()
        assert gate.injector.keys == [(0xFFE3, True), (0x62, True)]
        await gate.close()

    asyncio.run(main())


def test_console_to_pod_paste_is_typed_only_for_the_controller() -> None:
    async def main() -> None:
        gate = Gate()
        person = await gate.connect("person-1")
        pasted = secrets.token_urlsafe(18)
        person.cut_text(pasted)  # view mode: discarded
        await person.flush()
        assert gate.injector.typed == []
        await gate.control.grant("person-1")
        person.cut_text(pasted)
        await person.flush()
        assert gate.injector.typed == [pasted]
        assert gate.sessions["person-1"].counts.pastes == 1
        await gate.close()

    asyncio.run(main())


def test_controller_input_above_the_rate_limit_is_dropped() -> None:
    async def main() -> None:
        now = [100.0]
        gate = Gate()
        person = await gate.connect("person-1", clock=lambda: now[0])
        await gate.control.grant("person-1")
        # Keys and clicks are dropped above the limit; a move is coalesced
        # instead (test_live_cursor2.py, LB-CURSOR2).
        for i in range(rfb.MAX_INPUT_EVENTS_PER_SECOND + 50):
            person.key(0x61, i % 2 == 0)
        await person.flush()
        assert len(gate.injector.keys) == rfb.MAX_INPUT_EVENTS_PER_SECOND
        assert gate.sessions["person-1"].counts.dropped == 50
        now[0] += 1.0
        person.key(0x61, False)
        await person.flush()
        assert len(gate.injector.keys) == rfb.MAX_INPUT_EVENTS_PER_SECOND + 1
        await gate.close()

    asyncio.run(main())


def test_a_paste_that_does_not_fit_the_rate_limit_is_dropped_whole() -> None:
    """A partly typed password is worse than none: all of it or nothing."""

    async def main() -> None:
        now = [5.0]
        gate = Gate()
        person = await gate.connect("person-1", clock=lambda: now[0])
        await gate.control.grant("person-1")
        # Spend the input limit on keys (moves have a limit of their own).
        for i in range(rfb.MAX_INPUT_EVENTS_PER_SECOND - 3):
            person.key(0x61, i % 2 == 0)
        person.cut_text(secrets.token_hex(8))
        await person.flush()
        assert gate.injector.typed == []
        await gate.close()

    asyncio.run(main())


def test_pointer_coordinates_are_clamped_to_the_screen() -> None:
    async def main() -> None:
        gate = Gate(width=64, height=40)
        person = await gate.connect("person-1")
        await gate.control.grant("person-1")
        person.pointer(0, 5000, 5000)
        await person.flush()
        assert gate.injector.pointer == [(0, 63, 39)]
        await gate.close()

    asyncio.run(main())


# --------------------------------------------------------------------------
# Pod-to-console clipboard is never sent (section 5.6)
# --------------------------------------------------------------------------


def test_the_server_sends_frame_updates_and_nothing_else() -> None:
    async def main() -> None:
        gate = Gate()
        gate.hub.publish(rfb.masked(_frame(), _unmasking()))
        person = await gate.connect("person-1")
        await gate.control.grant("person-1")
        person.cut_text(secrets.token_urlsafe(10))
        person.key(0x61, True)
        person.request(incremental=False)
        await person.flush()
        await person.read_update()
        gate.hub.publish(rfb.masked(_frame(rgb=(1, 2, 3)), _unmasking()))
        person.request(incremental=True)
        await person.flush()
        await person.read_update()
        rest = await person.drain_server()
        assert rest == b""  # no ServerCutText (3), no Bell (2), nothing
        assert set(person.message_types) == {0}
        await gate.close()

    asyncio.run(main())


def test_the_rfb_module_has_no_way_to_send_cut_text() -> None:
    """Not off by a flag: absent. A flag is one line from being on."""
    assert not any("cut" in name.lower() and "send" in name.lower() for name in dir(rfb.RfbSession))


# --------------------------------------------------------------------------
# Only masked frames leave (section 5.3), in the viewer's pixel format
# --------------------------------------------------------------------------


def test_the_hub_accepts_only_frames_that_went_through_the_mask() -> None:
    hub = rfb.FrameHub(64, 40)
    with pytest.raises(TypeError):
        hub.publish(_frame())  # type: ignore[arg-type]


def test_a_frame_reaches_the_viewer_as_the_mask_left_it() -> None:
    async def main() -> None:
        gate = Gate()
        # A mask whose CDP session is down blanks the whole frame (§5.3.2):
        # the viewer must receive the blank, not the pixels underneath.
        gate.hub.publish(rfb.masked(_frame(rgb=(200, 100, 50)), MaskState()))
        viewer = await gate.connect("viewer-a")
        viewer.set_pixel_format(big_endian=False, shifts=(0, 8, 16))  # noVNC's RGBX
        viewer.request(incremental=False)
        await viewer.flush()
        [(x, y, w, h, enc, data)] = await viewer.read_update()
        assert (x, y, w, h, enc) == (0, 0, 64, 40, 0)
        r, g, b = UNAVAILABLE_RGB
        assert data[:4] == bytes([r, g, b, 0])
        assert data == bytes([r, g, b, 0]) * (64 * 40)
        await gate.close()

    asyncio.run(main())


def test_server_init_declares_the_frames_own_format() -> None:
    async def main() -> None:
        gate = Gate(width=64, height=40)
        viewer = await gate.connect("viewer-a")
        assert (viewer.width, viewer.height) == (64, 40)
        bpp, depth, big, true_colour, rmax, gmax, bmax, rs, gs, bs = struct.unpack(
            ">BBBBHHHBBB3x", viewer.server_format)
        assert (bpp, depth, big, true_colour) == (32, 24, 0, 1)
        assert (rmax, gmax, bmax, rs, gs, bs) == (255, 255, 255, 16, 8, 0)
        await gate.close()

    asyncio.run(main())


def test_an_incremental_update_carries_only_the_rows_that_changed() -> None:
    async def main() -> None:
        gate = Gate()
        base = _frame()
        gate.hub.publish(rfb.masked(base, _unmasking()))
        viewer = await gate.connect("viewer-a")
        viewer.request(incremental=False)
        await viewer.flush()
        await viewer.read_update()
        changed = _frame()
        changed.fill(0, 10, 64, 13, (255, 255, 255))
        gate.hub.publish(rfb.masked(changed, _unmasking()))
        viewer.request(incremental=True)
        await viewer.flush()
        [(x, y, w, h, enc, _data)] = await viewer.read_update()
        assert (x, y, w, h, enc) == (0, 10, 64, 3, 0)
        await gate.close()

    asyncio.run(main())


def test_an_identical_frame_is_not_resent_and_the_request_waits_for_a_change() -> None:
    async def main() -> None:
        gate = Gate()
        gate.hub.publish(rfb.masked(_frame(), _unmasking()))
        viewer = await gate.connect("viewer-a")
        viewer.request(incremental=False)
        await viewer.flush()
        await viewer.read_update()
        gate.hub.publish(rfb.masked(_frame(), _unmasking()))  # a static page
        viewer.request(incremental=True)
        await viewer.flush()
        assert gate.sessions["viewer-a"].counts.updates == 1
        changed = _frame()
        changed.fill(0, 0, 64, 1, (9, 9, 9))
        gate.hub.publish(rfb.masked(changed, _unmasking()))
        [(_x, y, _w, h, _enc, _)] = await asyncio.wait_for(viewer.read_update(), 2)
        assert (y, h) == (0, 1)
        await gate.close()

    asyncio.run(main())


def test_zlib_is_used_when_the_viewer_offers_it() -> None:
    async def main() -> None:
        gate = Gate()
        gate.hub.publish(rfb.masked(_frame(), MaskState()))
        viewer = await gate.connect("viewer-a", encodings=(6, 0))
        viewer.request(incremental=False)
        await viewer.flush()
        [(_x, _y, w, h, enc, data)] = await viewer.read_update()
        assert enc == 6
        assert len(zlib.decompressobj().decompress(data)) == w * h * 4
        await gate.close()

    asyncio.run(main())


def test_a_display_resize_is_announced_with_desktop_size() -> None:
    async def main() -> None:
        gate = Gate(width=64, height=40)
        viewer = await gate.connect("viewer-a", encodings=(0, rfb.ENCODING_DESKTOP_SIZE))
        gate.hub.publish(rfb.masked(_frame(32, 48), MaskState()))
        viewer.request(incremental=True)
        await viewer.flush()
        [(_x, _y, w, h, enc, _)] = await viewer.read_update()
        assert (w, h, enc) == (32, 48, rfb.ENCODING_DESKTOP_SIZE)
        await gate.close()

    asyncio.run(main())


def test_the_controllers_keys_are_injected_and_counted_after_a_device_switch() -> None:
    """KEYS-FOCUS (part of #1030). MEASURED 2026-10-14 on task_84cb2038aa704c829d48
    generation 5: after two device switches the gate's summary for the controller
    read keys=0 pointer=87. A device switch is a resize the viewer is told of with
    DesktopSize; the connection, its control and its key path all outlive it."""
    async def main() -> None:
        gate = Gate(width=64, height=40)
        person = await gate.connect("person-1", encodings=(0, rfb.ENCODING_DESKTOP_SIZE))
        await gate.control.grant("person-1")
        person.key(0x61, True)
        person.key(0x61, False)
        await person.flush()

        # iphone-15 -> desktop: the display changes shape under the open connection.
        gate.hub.publish(rfb.masked(_frame(48, 32), MaskState()))
        person.request(incremental=True)
        await person.flush()
        [(_x, _y, w, h, enc, _)] = await person.read_update()
        assert (w, h, enc) == (48, 32, rfb.ENCODING_DESKTOP_SIZE)

        person.key(0x62, True)
        person.key(0x62, False)
        person.pointer(0, 5, 6)
        await person.flush()

        assert gate.injector.keys == [(0x61, True), (0x61, False), (0x62, True), (0x62, False)]
        counts = gate.sessions["person-1"].counts
        assert counts.key_events == 4
        assert counts.pointer_events == 1
        assert counts.dropped == 0
        await gate.close()

    asyncio.run(main())


def _unmasking() -> MaskState:
    """A mask that paints nothing: stands in for a clean report in a test
    about encoding, which is not the mask's test."""

    class _Clean(MaskState):
        def apply(self, frame: Frame):  # type: ignore[override]
            from agent_worker.livegate.mask import Outcome
            return Outcome.MASKED

    return _Clean()


# --------------------------------------------------------------------------
# Captured logs contain no keysym (section 5.3.3)
# --------------------------------------------------------------------------


def _assert_absent(caplog: pytest.LogCaptureFixture, needles: list[str]) -> None:
    text = "\n".join(
        f"{r.name} {r.getMessage()} {r.args!r} {getattr(r, '__dict__', {})!r}" for r in caplog.records
    )
    assert caplog.records, "no log line was captured, so the check proved nothing"
    for needle in needles:
        assert needle not in text, "a key event's content reached a log line"


def _keysym_needles(keysym: int) -> list[str]:
    return [str(keysym), hex(keysym), f"{keysym:x}", f"{keysym:X}", chr(keysym & 0xFFFFFF)]


def test_no_log_line_carries_a_keysym_or_pasted_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LIVEGATE_LOGGER)
    keysym = 0x01000000 | (0x4E00 + secrets.randbelow(0x5000))  # a CJK ideograph
    pasted = secrets.token_urlsafe(24)

    async def main() -> None:
        gate = Gate()
        person = await gate.connect("person-1")
        bystander = await gate.connect("person-2")
        person.key(keysym, True)  # view mode: dropped
        await gate.control.grant("person-1")
        person.key(keysym, True)
        person.key(keysym, False)
        person.cut_text(pasted)
        bystander.key(keysym, True)
        bystander.cut_text(pasted)
        await person.flush()
        await bystander.flush()
        await gate.control.revoke()
        await person.drain_server()
        await bystander.drain_server()
        await asyncio.sleep(0.05)
        await gate.close()

    asyncio.run(main())
    _assert_absent(caplog, _keysym_needles(keysym) + [pasted])


# --------------------------------------------------------------------------
# XTEST injection against a scripted X server
# --------------------------------------------------------------------------

XK_a, XK_A, XK_1, XK_exclam, XK_Shift_L, XK_Return = 0x61, 0x41, 0x31, 0x21, 0xFFE1, 0xFF0D
ROOT = 0x2B
XTEST_OPCODE = 132


class FakeXServer:
    """Just enough of an X server for XTEST: setup, QueryExtension,
    GetKeyboardMapping, ChangeKeyboardMapping and XTestFakeInput."""

    def __init__(self, path: Path, *, xtest: bool = True) -> None:
        self.path = path
        self.xtest = xtest
        self.mapping: dict[int, list[int]] = {
            8: [XK_a, XK_A],
            9: [XK_1, XK_exclam],
            10: [XK_Shift_L, 0],
            11: [XK_Return, 0],
            12: [0, 0],
            13: [0, 0],
        }
        self.inputs: list[tuple[int, int, int, int, int]] = []  # type, detail, root, x, y
        self.remaps: list[tuple[int, list[int]]] = []
        self.server: asyncio.base_events.Server | None = None
        self.got_input = asyncio.Event()

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._client, path=str(self.path))

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readexactly(12)
        assert head[0:1] == b"l"
        major, _minor, n_len, d_len = struct.unpack("<HHHH", head[2:10])
        assert major == 11
        await reader.readexactly(_pad4(n_len) + _pad4(d_len))
        vendor = b"fake"
        fixed = struct.pack("<IIIIHHBBBBBBBB4x", 1, 0x200000, 0x1FFFFF, 0, len(vendor), 65535,
                            1, 0, 0, 0, 32, 32, 8, 13)
        screen = struct.pack("<IIIIIHHHHHHIBBBB", ROOT, 0x20, 0xFFFFFF, 0, 0, 64, 40, 10, 10,
                             1, 1, 0x21, 0, 0, 24, 0)
        body = fixed + vendor + b"\0" * (_pad4(len(vendor)) - len(vendor)) + screen
        writer.write(struct.pack("<BxHHH", 1, 11, 0, len(body) // 4) + body)
        seq = 0
        try:
            while True:
                req = await reader.readexactly(4)
                opcode, data, length = struct.unpack("<BBH", req)
                rest = await reader.readexactly(length * 4 - 4)
                seq = (seq + 1) & 0xFFFF
                if opcode == 98:  # QueryExtension
                    (n,) = struct.unpack_from("<H", rest, 0)
                    name = rest[4:4 + n]
                    present = self.xtest and name == b"XTEST"
                    writer.write(struct.pack("<BxHIBBBB20x", 1, seq, 0, int(present),
                                             XTEST_OPCODE if present else 0, 0, 0))
                elif opcode == 101:  # GetKeyboardMapping
                    first, count = rest[0], rest[1]
                    syms = [s for k in range(first, first + count) for s in self.mapping[k]]
                    writer.write(struct.pack("<BBHI24x", 1, 2, seq, len(syms))
                                 + struct.pack(f"<{len(syms)}I", *syms))
                elif opcode == 100:  # ChangeKeyboardMapping
                    first, per = rest[0], rest[1]
                    syms = list(struct.unpack_from(f"<{data * per}I", rest, 4))
                    for i in range(data):
                        self.mapping[first + i] = syms[i * per:(i + 1) * per]
                    self.remaps.append((first, syms))
                elif opcode == 43:  # GetInputFocus: what XSync sends
                    writer.write(struct.pack("<BBHII20x", 1, 0, seq, 0, ROOT))
                elif opcode == XTEST_OPCODE and data == 2:  # XTestFakeInput
                    kind, detail = rest[0], rest[1]
                    (root,) = struct.unpack_from("<I", rest, 8)
                    x, y = struct.unpack_from("<hh", rest, 20)
                    self.inputs.append((kind, detail, root, x, y))
                    self.got_input.set()
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def _pad4(n: int) -> int:
    return (n + 3) & ~3


async def _settle(server: FakeXServer, injector: inject.XInjector) -> None:
    await injector.sync()
    for _ in range(5):
        await asyncio.sleep(0)


def test_xtest_presses_the_keycode_a_keysym_is_on(short_dir: Path) -> None:
    async def main() -> None:
        server = FakeXServer(short_dir / "X0")
        await server.start()
        injector = await inject.XInjector.open(str(server.path), width=64, height=40)
        await injector.key(XK_a, True)
        await injector.key(XK_a, False)
        await _settle(server, injector)
        assert server.inputs == [(2, 8, 0, 0, 0), (3, 8, 0, 0, 0)]
        await injector.close()
        await server.stop()

    asyncio.run(main())


def test_typed_text_presses_shift_for_a_shifted_symbol_and_returns_on_newline(short_dir: Path) -> None:
    async def main() -> None:
        server = FakeXServer(short_dir / "X0")
        await server.start()
        injector = await inject.XInjector.open(str(server.path), width=64, height=40)
        assert await injector.type_text("A!\n")
        await _settle(server, injector)
        assert [(k, d) for k, d, *_ in server.inputs] == [
            (2, 10), (2, 8), (3, 8), (3, 10),   # Shift + a -> 'A'
            (2, 10), (2, 9), (3, 9), (3, 10),   # Shift + 1 -> '!'
            (2, 11), (3, 11),                   # Return
        ]
        await injector.close()
        await server.stop()

    asyncio.run(main())


def test_a_keysym_on_no_key_is_bound_to_a_spare_keycode_first(short_dir: Path) -> None:
    async def main() -> None:
        server = FakeXServer(short_dir / "X0")
        await server.start()
        injector = await inject.XInjector.open(str(server.path), width=64, height=40)
        euro = 0x20AC  # keysym EuroSign
        await injector.key(euro, True)
        await injector.key(euro, False)
        await _settle(server, injector)
        assert server.remaps == [(12, [euro, euro])]
        assert server.inputs == [(2, 12, 0, 0, 0), (3, 12, 0, 0, 0)]
        await injector.close()
        await server.stop()

    asyncio.run(main())


def test_pointer_buttons_and_motion_are_absolute_on_the_root(short_dir: Path) -> None:
    async def main() -> None:
        server = FakeXServer(short_dir / "X0")
        await server.start()
        injector = await inject.XInjector.open(str(server.path), width=64, height=40)
        await injector.pointer_event(0b001, 10, 20)  # move, then press button 1
        await injector.pointer_event(0b000, 11, 21)  # move, then release
        await injector.pointer_event(0b1000, 11, 21)  # wheel up: button 4
        await injector.pointer_event(0b0000, 11, 21)
        await _settle(server, injector)
        assert server.inputs == [
            (6, 0, ROOT, 10, 20), (4, 1, 0, 0, 0),
            (6, 0, ROOT, 11, 21), (5, 1, 0, 0, 0),
            (4, 4, 0, 0, 0),
            (5, 4, 0, 0, 0),
        ]
        await injector.close()
        await server.stop()

    asyncio.run(main())


def test_release_all_lets_go_of_every_held_key_and_button(short_dir: Path) -> None:
    async def main() -> None:
        server = FakeXServer(short_dir / "X0")
        await server.start()
        injector = await inject.XInjector.open(str(server.path), width=64, height=40)
        await injector.key(XK_Shift_L, True)
        await injector.pointer_event(0b001, 1, 1)
        await injector.release_all()
        await injector.release_all()  # idempotent: nothing is pressed twice-released
        await injector.key(XK_a, False)  # a release with no press is not sent
        await _settle(server, injector)
        kinds = [(k, d) for k, d, *_ in server.inputs]
        assert kinds == [(2, 10), (6, 0), (4, 1), (3, 10), (5, 1)]
        await injector.close()
        await server.stop()

    asyncio.run(main())


def test_a_display_without_xtest_is_refused(short_dir: Path) -> None:
    async def main() -> None:
        server = FakeXServer(short_dir / "X0", xtest=False)
        await server.start()
        with pytest.raises(inject.InjectError):
            await inject.XInjector.open(str(server.path), width=64, height=40)
        await server.stop()

    asyncio.run(main())


def test_injection_logs_no_keysym(short_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LIVEGATE_LOGGER)
    keysym = 0x01000000 | (0x4E00 + secrets.randbelow(0x5000))
    pasted = secrets.token_urlsafe(20)

    async def main() -> None:
        server = FakeXServer(short_dir / "X0")
        await server.start()
        injector = await inject.XInjector.open(str(server.path), width=64, height=40)
        await injector.key(keysym, True)
        await injector.key(keysym, False)
        await injector.type_text(pasted)
        await _settle(server, injector)
        await injector.close()
        await server.stop()

    asyncio.run(main())
    _assert_absent(caplog, _keysym_needles(keysym) + [pasted])


# --------------------------------------------------------------------------
# ctl.sock: one registration, and none after a restart (section 5.4.2)
# --------------------------------------------------------------------------


class CtlRig:
    def __init__(self, root: Path) -> None:
        self.sock = root / "ctl.sock"
        self.marker = root / "private" / "gate-started"
        self.marker.parent.mkdir(exist_ok=True)
        self.records: list[tuple[str, dict[str, str]]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.server: ctl.CtlServer | None = None

    async def handler(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, args))
        return {"done": op}

    def record(self, event: str, fields: dict[str, str]) -> None:
        self.records.append((event, dict(fields)))

    async def start(self) -> ctl.CtlServer:
        self.server = ctl.CtlServer(str(self.sock), str(self.marker), self.handler, self.record)
        await self.server.start()
        return self.server

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.close()

    def events(self) -> list[str]:
        return [e for e, _ in self.records]


def _client(rig: CtlRig) -> ctl.CtlClient:
    return ctl.CtlClient(str(rig.sock), timeout=5)


def test_the_first_registration_is_the_workers_channel(short_dir: Path) -> None:
    async def main() -> None:
        rig = CtlRig(short_dir)
        await rig.start()
        worker = _client(rig)
        await asyncio.to_thread(worker.register)
        ticket = "t." + secrets.token_urlsafe(24)
        result = await asyncio.to_thread(worker.call, "tunnel_ticket", {"ticket": ticket})
        assert result == {"done": "tunnel_ticket"}
        assert rig.calls == [("tunnel_ticket", {"ticket": ticket})]
        await asyncio.to_thread(worker.close)
        await rig.stop()

    asyncio.run(main())


def test_a_second_registration_is_refused_and_the_first_still_works(short_dir: Path) -> None:
    async def main() -> None:
        rig = CtlRig(short_dir)
        await rig.start()
        worker = _client(rig)
        await asyncio.to_thread(worker.register)
        agent = _client(rig)
        with pytest.raises(ctl.CtlRefused):
            await asyncio.to_thread(agent.register)
        assert ("gate_ctl_refused", {"cause": "already_registered"}) in rig.records
        # The agent's own key does not authorise anything either.
        with pytest.raises((ctl.CtlRefused, ctl.CtlError)):
            await asyncio.to_thread(agent.call, "resume", {})
        assert await asyncio.to_thread(worker.call, "pause", {}) == {"done": "pause"}
        assert rig.calls == [("pause", {})]
        await asyncio.to_thread(worker.close)
        await asyncio.to_thread(agent.close)
        await rig.stop()

    asyncio.run(main())


def test_a_restarted_gate_refuses_even_the_first_registration(short_dir: Path) -> None:
    async def main() -> None:
        first = CtlRig(short_dir)
        await first.start()
        assert first.marker.exists()
        assert not first.server.restarted  # type: ignore[union-attr]
        await first.stop()  # the gate process dies; the native sidecar restarts it

        second = CtlRig(short_dir)  # same socket path, same marker
        server = await second.start()
        assert server.restarted
        assert "gate_restarted" in second.events()
        assert first.sock.exists()  # still listening: the startup probe is satisfied
        worker = ctl.CtlClient(str(second.sock), timeout=5)
        with pytest.raises(ctl.CtlRefused):
            await asyncio.to_thread(worker.register)
        assert ("gate_ctl_refused", {"cause": "restarted"}) in second.records
        assert second.calls == []
        await asyncio.to_thread(worker.close)
        await second.stop()

    asyncio.run(main())


def test_a_marker_the_gate_cannot_write_is_treated_as_a_restart(short_dir: Path) -> None:
    """Fail closed: a gate that cannot prove it is the first never opens the slot."""

    async def main() -> None:
        rig = CtlRig(short_dir)
        rig.marker = short_dir / "missing-dir" / "gate-started"
        server = await rig.start()
        assert server.restarted
        worker = _client(rig)
        with pytest.raises(ctl.CtlRefused):
            await asyncio.to_thread(worker.register)
        await asyncio.to_thread(worker.close)
        await rig.stop()

    asyncio.run(main())


def _raw(path: Path, *lines: dict[str, Any]) -> list[dict[str, Any]]:
    """Send raw protocol lines on one connection and read every reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(path))
        replies = []
        f = s.makefile("rwb")
        for line in lines:
            f.write(json.dumps(line).encode() + b"\n")
            f.flush()
            got = f.readline()
            if not got:
                break
            replies.append(json.loads(got))
        return replies


def test_unregistered_forged_and_replayed_commands_are_refused(short_dir: Path) -> None:
    async def main() -> None:
        rig = CtlRig(short_dir)
        await rig.start()
        # Before any registration a command is refused.
        [reply] = await asyncio.to_thread(_raw, rig.sock, {"seq": 1, "op": "pause", "args": {}, "mac": "00"})
        assert reply["ok"] is False
        worker = _client(rig)
        await asyncio.to_thread(worker.register)
        # A forged MAC.
        forged = {"seq": 99, "op": "resume", "args": {}, "mac": secrets.token_hex(32)}
        [reply] = await asyncio.to_thread(_raw, rig.sock, forged)
        assert reply["ok"] is False
        # A replayed counter: the worker's own message, sent again.
        message = worker.sign("pause", {})
        [first] = await asyncio.to_thread(_raw, rig.sock, message)
        assert first["ok"] is True
        [again] = await asyncio.to_thread(_raw, rig.sock, message)
        assert again["ok"] is False
        # An operation the gate does not know.
        [unknown] = await asyncio.to_thread(_raw, rig.sock, worker.sign("exec", {}))
        assert unknown["ok"] is False
        assert [op for op, _ in rig.calls] == ["pause"]
        causes = [f.get("cause") for e, f in rig.records if e == "gate_ctl_refused"]
        assert {"unregistered", "bad_mac", "replay", "unknown_op"} <= set(causes)
        await asyncio.to_thread(worker.close)
        await rig.stop()

    asyncio.run(main())


def test_the_worker_refuses_a_reply_not_signed_with_its_key(short_dir: Path) -> None:
    """An agent that swapped ctl.sock for its own socket cannot answer as the gate."""

    async def main() -> None:
        path = short_dir / "ctl.sock"

        async def impostor(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while line := await reader.readline():
                msg = json.loads(line)
                reply = {"ok": True} if "register" in msg else {
                    "seq": msg["seq"], "ok": True, "result": {}, "mac": secrets.token_hex(32)}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(impostor, path=str(path))
        worker = ctl.CtlClient(str(path), timeout=5)
        await asyncio.to_thread(worker.register)
        with pytest.raises(ctl.CtlError):
            await asyncio.to_thread(worker.call, "pause", {})
        await asyncio.to_thread(worker.close)
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_the_registration_key_must_be_32_bytes(short_dir: Path) -> None:
    async def main() -> None:
        rig = CtlRig(short_dir)
        await rig.start()
        short = base64.b64encode(os.urandom(8)).decode()
        [reply] = await asyncio.to_thread(_raw, rig.sock, {"register": short})
        assert reply["ok"] is False
        # A malformed registration does not take the slot.
        worker = _client(rig)
        await asyncio.to_thread(worker.register)
        await asyncio.to_thread(worker.close)
        await rig.stop()

    asyncio.run(main())


def test_ctl_logs_no_ticket(short_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LIVEGATE_LOGGER)
    ticket = "t." + secrets.token_urlsafe(30)

    rig = CtlRig(short_dir)

    async def main() -> None:
        await rig.start()
        worker = _client(rig)
        await asyncio.to_thread(worker.register)
        await asyncio.to_thread(worker.call, "tunnel_ticket", {"ticket": ticket})
        agent = _client(rig)
        with pytest.raises(ctl.CtlRefused):
            await asyncio.to_thread(agent.register)
        await asyncio.to_thread(worker.close)
        await asyncio.to_thread(agent.close)
        await rig.stop()

    asyncio.run(main())
    _assert_absent(caplog, [ticket])
    assert ticket not in json.dumps(rig.records)


# --------------------------------------------------------------------------
# The pointer (LB-CURSOR; part of #1030): a shape for the controller's noVNC,
# a mark in every other viewer's frame and in the recording
# --------------------------------------------------------------------------
#
# Measured 2026-10-12 on task_4e0cf1c649634b949703: noVNC's canvas had
# `cursor: none`, the gate never offered -239, and Xvfb's -fbdir file never
# holds the X pointer, so the person in control clicked blind.

_NOVNC_RGBX = {"big_endian": False, "shifts": (0, 8, 16)}
_BACKGROUND = (10, 20, 30)


def _cursor_rects(rects: list[tuple[int, int, int, int, int, bytes]]) -> list[tuple[int, int, int, int, int, bytes]]:
    return [r for r in rects if r[4] == CURSOR]


def _rgbx_at(data: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    at = (y * width + x) * 4
    return (data[at], data[at + 1], data[at + 2])


def test_a_viewer_that_lists_the_cursor_encoding_is_sent_the_arrow_with_its_bitmask() -> None:
    async def main() -> None:
        from agent_worker.livegate import pointer
        gate = Gate()
        gate.hub.publish(rfb.masked(_frame(), _unmasking()))
        viewer = await gate.connect("person-1", encodings=(0, rfb.ENCODING_DESKTOP_SIZE, CURSOR))
        viewer.set_pixel_format(**_NOVNC_RGBX)
        viewer.request(incremental=False)
        await viewer.flush()
        rects = await viewer.read_update()
        [(x, y, w, h, _enc, data)] = _cursor_rects(rects)
        assert (x, y) == pointer.HOTSPOT
        assert (w, h) == (pointer.WIDTH, pointer.HEIGHT)
        mask_len = (w + 7) // 8 * h
        assert len(data) == w * h * 4 + mask_len
        pixels, bitmask = data[:w * h * 4], data[w * h * 4:]
        assert bitmask == pointer.bitmask()
        # The tip is the outline, the inside is the fill, both in the viewer's RGBX.
        assert _rgbx_at(pixels, w, 0, 0) == pointer.OUTLINE_RGB
        assert _rgbx_at(pixels, w, 1, 2) == pointer.FILL_RGB
        assert bitmask[0] & 0x80, "the tip is transparent"
        assert not bitmask[0] & 0x40, "the right of the tip on its first row is drawn"
        # The frame itself still arrives with it.
        assert [r[4] for r in rects if r[4] != CURSOR] == [0]
        # Sent once: the next update carries the frame alone.
        changed = _frame()
        changed.fill(0, 5, 64, 6, (1, 2, 3))
        gate.hub.publish(rfb.masked(changed, _unmasking()))
        viewer.request(incremental=True)
        await viewer.flush()
        assert _cursor_rects(await viewer.read_update()) == []
        await gate.close()

    asyncio.run(main())


def test_a_viewer_that_does_not_list_the_cursor_encoding_is_sent_no_cursor() -> None:
    async def main() -> None:
        gate = Gate()
        gate.hub.publish(rfb.masked(_frame(), _unmasking()))
        viewer = await gate.connect("recorder", encodings=(6, 0, rfb.ENCODING_DESKTOP_SIZE))
        viewer.request(incremental=False)
        await viewer.flush()
        rects = await viewer.read_update()
        assert [r[4] for r in rects] == [6]
        await gate.close()

    asyncio.run(main())


def test_the_cursor_is_sent_again_after_every_desktop_size_change() -> None:
    async def main() -> None:
        gate = Gate(width=64, height=40)
        gate.hub.publish(rfb.masked(_frame(64, 40), _unmasking()))
        viewer = await gate.connect("person-1", encodings=(0, rfb.ENCODING_DESKTOP_SIZE, CURSOR))
        viewer.request(incremental=False)
        await viewer.flush()
        assert len(_cursor_rects(await viewer.read_update())) == 1
        gate.hub.publish(rfb.masked(_frame(32, 48), _unmasking()))
        viewer.request(incremental=True)
        await viewer.flush()
        [(_x, _y, w, h, enc, _)] = await viewer.read_update()
        assert (w, h, enc) == (32, 48, rfb.ENCODING_DESKTOP_SIZE)
        viewer.width, viewer.height = 32, 48
        viewer.request(incremental=False)
        await viewer.flush()
        rects = await viewer.read_update()
        assert len(_cursor_rects(rects)) == 1
        assert [(r[2], r[3]) for r in rects if r[4] == 0] == [(32, 48)]
        await gate.close()

    asyncio.run(main())


def _xwd(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """An XWD file of one colour, laid out as Xvfb's -fbdir screen file."""
    header = struct.pack(">25I", 100, 7, 2, 24, width, height, 0, 0, 32, 0, 32, 32, width * 4,
                         4, 0xFF0000, 0x00FF00, 0x0000FF, 8, 256, 0, width, height, 0, 0, 0)
    value = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
    return header + value.to_bytes(4, "little") * (width * height)


def test_a_watcher_sees_the_pointer_where_the_controller_moved_it_and_xvfbs_buffer_is_untouched() -> None:
    async def main() -> None:
        from agent_worker.livegate import pointer
        from agent_worker.livegate.capture import XvfbFramebuffer
        with tempfile.TemporaryDirectory() as tmp:
            screen = Path(tmp) / "Xvfb_screen0"
            screen.write_bytes(_xwd(64, 40, _BACKGROUND))
            before = screen.read_bytes()
            with XvfbFramebuffer(screen) as fb:
                gate = Gate(width=64, height=40)
                gate.hub.publish(rfb.masked(fb.grab(), _unmasking()))
                watcher = await gate.connect("watcher", encodings=(0, CURSOR))
                watcher.set_pixel_format(**_NOVNC_RGBX)
                person = await gate.connect("person-1", encodings=(0, CURSOR))
                await gate.control.grant("person-1")
                person.pointer(0, 20, 10)
                await person.flush()
                watcher.request(incremental=False)
                await watcher.flush()
                [(_x, _y, w, _h, _enc, data)] = [r for r in await watcher.read_update() if r[4] == 0]
                for dx, dy, rgb in pointer.opaque():
                    if 20 + dx < 64 and 10 + dy < 40:
                        assert _rgbx_at(data, w, 20 + dx, 10 + dy) == rgb
                # Away from the arrow, the frame as it was.
                assert _rgbx_at(data, w, 5, 5) == _BACKGROUND
                assert _rgbx_at(data, w, 19, 10) == _BACKGROUND
                # Painted on a copy: the published frame and Xvfb's file never are.
                hub = gate.hub.current.frame
                assert all(hub.rgb_at(x, y) == _BACKGROUND for y in range(40) for x in range(64))
                assert screen.read_bytes() == before
                await gate.close()

    asyncio.run(main())


def test_the_agents_pointer_reaches_a_watcher_too() -> None:
    async def main() -> None:
        from agent_worker.livegate import pointer
        gate = Gate(width=64, height=40)
        gate.hub.publish(rfb.masked(_frame(rgb=_BACKGROUND), _unmasking()))
        watcher = await gate.connect("watcher", encodings=(0,))
        watcher.set_pixel_format(**_NOVNC_RGBX)
        gate.hub.pointer.move(40, 30)
        watcher.request(incremental=False)
        await watcher.flush()
        [(_x, _y, w, _h, _enc, data)] = await watcher.read_update()
        tip = pointer.opaque()[0]
        assert _rgbx_at(data, w, 40 + tip[0], 30 + tip[1]) == tip[2]
        # A move with no new capture still reaches a waiting viewer.
        watcher.request(incremental=True)
        await watcher.flush()
        gate.hub.pointer.move(5, 5)
        [(_x, y, _w, h, _enc, band)] = await asyncio.wait_for(watcher.read_update(), 2)
        assert y <= 5 < y + h
        await gate.close()

    asyncio.run(main())


def test_the_pointer_never_reveals_what_the_mask_covered() -> None:
    async def main() -> None:
        from agent_worker.livegate import pointer
        gate = Gate(width=64, height=40)
        # The mask is down: the whole frame is the "unavailable" fill (§5.3.2).
        gate.hub.publish(rfb.masked(_frame(rgb=(200, 100, 50)), MaskState()))
        watcher = await gate.connect("watcher", encodings=(0,))
        watcher.set_pixel_format(**_NOVNC_RGBX)
        gate.hub.pointer.move(10, 10)
        watcher.request(incremental=False)
        await watcher.flush()
        [(_x, _y, w, h, _enc, data)] = await watcher.read_update()
        allowed = {UNAVAILABLE_RGB, pointer.OUTLINE_RGB, pointer.FILL_RGB}
        seen = {_rgbx_at(data, w, x, y) for y in range(h) for x in range(w)}
        assert seen <= allowed
        assert pointer.OUTLINE_RGB in seen
        await gate.close()

    asyncio.run(main())


def test_the_controller_with_a_local_cursor_is_not_sent_a_second_lagging_one() -> None:
    async def main() -> None:
        gate = Gate(width=64, height=40)
        gate.hub.publish(rfb.masked(_frame(rgb=_BACKGROUND), _unmasking()))
        person = await gate.connect("person-1", encodings=(0, CURSOR))
        person.set_pixel_format(**_NOVNC_RGBX)
        await gate.control.grant("person-1")
        person.pointer(0, 20, 10)
        person.request(incremental=False)
        await person.flush()
        [(_x, _y, w, h, _enc, data)] = [r for r in await person.read_update() if r[4] == 0]
        assert {_rgbx_at(data, w, x, y) for y in range(h) for x in range(w)} == {_BACKGROUND}
        await gate.close()

    asyncio.run(main())
