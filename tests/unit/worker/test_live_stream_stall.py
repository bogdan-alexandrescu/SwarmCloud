"""Input never holds up the frame stream or loses a key (STREAM-STALL; part of #1030).

MEASURED 2026-10-14 02:29-02:34 UTC, pod swarm-84cb2038aa704c829d48-5-lqs4k,
container `display`: the owner held control for 188 s, on iphone-15 and then
the desktop, and their connection's close line read

    keys=0 pointer=87 dropped=1 updates=8 bytes=3755
    input_to_frame_ms p50=9777 p95=133915

-- eight updates in five minutes, ten seconds from input to frame, and not
one key, though the form was typed into. The recorder on the same gate got
37 updates.

THE CAUSE. `RfbSession._read_loop` awaited every injection before it read
the next message. On a touch preset a pointer event is
`Input.dispatchTouchEvent`, which Chromium answers only once the renderer has
handled it; while it waited, nothing behind it on the stream was read --
not the next FramebufferUpdateRequest, so no update was sent, and not the
KeyEvents, which were still unread when the connection closed (keys=0). noVNC
sends a move every 17 ms, so the backlog only grew. `livegate.bench`-style
reproduction on origin/main: 29 updates a second with an injector that
answers at once, 0.2 a second with one that takes 0.5 s a pointer event.

Each test drives the real capture loop (`LiveGate._capture`, grabbing from a
fake screen that changes every frame), the gate's own `_serve_rfb` and a real
`RfbSession` over a socket pair, with a viewer that speaks as noVNC 1.4.0
does: its SetPixelFormat and SetEncodings, a full request and then an
incremental one after every update, KeyEvents and PointerEvents while the
page changes. No Xvfb, no Chromium, no network, no credential.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from pathlib import Path
from typing import Any

import pytest
from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import rfb
from agent_worker.livegate.capture import Frame
from agent_worker.livegate.mask import MASK_RGB
from agent_worker.livegate.presets import resolve
from test_live_stream_smooth import COPY_RECT, NOVNC, Viewer, _BoxMask, _gate, _pair

DESKTOP_SIZE = -223
CURSOR = -239
CONN = "person-1"
#: The box the mask paints, as a password field: inside every frame size here.
MASKED_BOX = (20, 20, 120, 60)
#: What the stream must carry while the page changes. The capture target in
#: control is `CONTROL_FPS` (30); a third of it is asserted because CI runs
#: these under `-n auto` on shared cores. origin/main sent 0.2 a second here.
MIN_UPDATES_PER_SECOND = gate_main.WATCH_FPS
#: How long one touch event takes to be acknowledged, in the slow tests:
#: Chromium answers `Input.dispatchTouchEvent` once the renderer handled it.
TOUCH_ACK_SECONDS = 0.3

# noVNC's keysyms for what a person types into a form: a, Shift, A, Return, BackSpace.
TYPED = (0x61, 0xFFE1, 0x41, 0xFF0D, 0xFF08)


class Screen:
    """Xvfb's framebuffer, at the current device's screen size, changing every grab."""

    def __init__(self, devices: Devices) -> None:
        self._devices = devices
        self.grabs = 0

    def grab(self) -> Frame:
        device = self._devices.device
        self.grabs += 1
        frame = Frame.blank(device.display_width, device.display_height, (250, 250, 250))
        # A caret blinking and a block moving: something changes every frame.
        x = (self.grabs * 7) % (device.display_width - 32)
        frame.fill(x, device.frame_top + 200, x + 32, device.frame_top + 232, (200, 30, 30))
        frame.fill(4, device.frame_top + 300, 6, device.frame_top + 316,
                   (0, 0, 0) if self.grabs % 2 else (250, 250, 250))
        return frame

    def close(self) -> None:
        pass


class Devices:
    """The device controller's `device`, switched by the test as the console does."""

    def __init__(self, name: str) -> None:
        self.device = resolve(name)


class Injector:
    """The touch injector: keys at once, a pointer event `pointer_seconds` late."""

    def __init__(self, pointer_seconds: float = 0.0) -> None:
        self.pointer_seconds = pointer_seconds
        self.keys: list[tuple[int, bool]] = []
        self.pointer: list[tuple[int, int, int]] = []
        self.fail_pointer: BaseException | None = None
        self.hang_pointer = False

    async def key(self, keysym: int, down: bool) -> None:
        self.keys.append((keysym, down))

    async def pointer_event(self, buttons: int, x: int, y: int) -> None:
        if self.hang_pointer:
            self.hang_pointer = False
            await asyncio.Event().wait()  # a CDP call no reply ever comes to
        if self.fail_pointer is not None:
            exc, self.fail_pointer = self.fail_pointer, None
            raise exc
        if self.pointer_seconds:
            await asyncio.sleep(self.pointer_seconds)
        self.pointer.append((buttons, x, y))

    async def type_text(self, text: str) -> bool:
        return True

    async def release_all(self) -> None:
        pass


class NoVnc(Viewer):
    """The smooth tests' viewer, with noVNC's PointerEvent and its request loop."""

    def pointer(self, buttons: int, x: int, y: int) -> None:
        self.writer.write(struct.pack(">BBHH", 5, buttons, x, y))

    async def follow(self, seconds: float) -> list[list[tuple[int, int, int, int, int]]]:
        """noVNC's loop: read an update, ask for the next one incrementally. Until `seconds` pass."""
        updates = []
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while (left := end - loop.time()) > 0:
            try:
                rects = await self.read_update(left)
            except TimeoutError:
                break
            updates.append(rects)
            self.request(True)
            await self.flush()
        return updates


class Rig:
    def __init__(self, gate: Any, devices: Devices, injector: Injector) -> None:
        self.gate, self.devices, self.injector = gate, devices, injector
        self.tasks: list[asyncio.Task[Any]] = []
        self.session: rfb.RfbSession | None = None

    @classmethod
    async def start(cls, tmp: Path, injector: Injector, device: str = "desktop") -> tuple[Rig, NoVnc]:
        gate = _gate(tmp)
        devices = Devices(device)
        gate.devices = devices
        gate.framebuffer = Screen(devices)
        gate.mask_state = _BoxMask(MASKED_BOX)
        gate.control = rfb.ControlState(injector)
        gate.touch = injector
        rig = cls(gate, devices, injector)
        sessions: list[rfb.RfbSession] = []
        real = rfb.RfbSession

        def session(*args: Any, **kwargs: Any) -> rfb.RfbSession:
            sessions.append(real(*args, **kwargs))
            return sessions[-1]

        (sr, sw), (vr, vw) = await _pair()
        gate_main.RfbSession = session  # type: ignore[assignment]
        try:
            rig.tasks.append(asyncio.create_task(gate._serve_rfb(CONN, sr, sw)))
            rig.tasks.append(asyncio.create_task(gate._capture()))
            viewer = NoVnc(vr, vw)
            await viewer.handshake(NOVNC)
        finally:
            gate_main.RfbSession = real  # type: ignore[assignment]
        rig.session = sessions[0]
        viewer.request(False)
        await viewer.flush()
        return rig, viewer

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def _until(check: Any, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        await asyncio.sleep(0.02)
    return bool(check())


def _area(rects: list[tuple[int, int, int, int, int]]) -> int:
    """The pixels an update paints, CopyRect and the cursor left out."""
    return sum(w * h for _, _, w, h, enc in rects if enc not in (COPY_RECT, CURSOR, DESKTOP_SIZE))


async def _person(viewer: NoVnc, seconds: float, sent: list[tuple[int, bool]]) -> None:
    """A person in control: the mouse moving as noVNC sends it, a key now and then."""
    end = time.monotonic() + seconds
    i = 0
    while time.monotonic() < end:
        viewer.pointer(0, 100 + (i * 3) % 300, 400)
        if i % 8 == 4:
            keysym = TYPED[(i // 8) % len(TYPED)]
            viewer.key(keysym, True)
            viewer.key(keysym, False)
            sent += [(keysym, True), (keysym, False)]
        await viewer.flush()
        await asyncio.sleep(0.017)
        i += 1


# --------------------------------------------------------------------------
# (1) a slow injection holds up neither the frames nor the keys
# --------------------------------------------------------------------------


def test_a_controller_whose_pointer_injects_slowly_still_gets_a_steady_stream_and_every_key(
        tmp_path: Path) -> None:
    """The field's case: a touch preset, each pointer event acknowledged late."""

    async def main() -> None:
        rig, viewer = await Rig.start(tmp_path, Injector(pointer_seconds=TOUCH_ACK_SECONDS),
                                      device="iphone-15")
        await rig.gate.control.grant(CONN)
        sent: list[tuple[int, bool]] = []
        seconds = 2.0
        person = asyncio.create_task(_person(viewer, seconds, sent))
        updates = await viewer.follow(seconds)
        await person
        session = rig.session
        assert session is not None
        rate = len(updates) / seconds
        assert rate >= MIN_UPDATES_PER_SECOND, (
            f"{rate:.1f} updates a second while the page changed: the stream waits behind input")
        assert sent, "the person typed nothing: the test proves nothing"
        keys_in = await _until(lambda: len(rig.injector.keys) == len(sent), 10)
        assert keys_in, f"{len(rig.injector.keys)} of {len(sent)} key events reached the display"
        assert rig.injector.keys == sent, "keys reach the display in the order typed"
        assert session.counts.key_events == len(sent)
        assert rig.injector.pointer, "the pointer reached the display too"
        await rig.close()

    asyncio.run(main())


def test_a_pointer_event_the_page_refused_does_not_end_the_connection(tmp_path: Path) -> None:
    """A touch on a page that went (CdpError) used to raise out of the read loop and close the stream."""

    async def main() -> None:
        injector = Injector()
        injector.fail_pointer = RuntimeError("the page's session went")
        rig, viewer = await Rig.start(tmp_path, injector)
        await rig.gate.control.grant(CONN)
        viewer.pointer(1, 50, 50)
        viewer.key(0x61, True)
        viewer.key(0x61, False)
        await viewer.flush()
        assert await _until(lambda: injector.keys == [(0x61, True), (0x61, False)], 5), injector.keys
        updates = await viewer.follow(1.0)
        assert len(updates) >= MIN_UPDATES_PER_SECOND // 2, "the stream went on"
        session = rig.session
        assert session is not None and session.counts.dropped >= 1
        await rig.close()

    asyncio.run(main())


def test_a_pointer_event_never_answered_does_not_hold_input_for_ever(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        monkeypatch.setattr(rfb, "INJECT_TIMEOUT_SECONDS", 0.2, raising=False)
        injector = Injector()
        injector.hang_pointer = True
        rig, viewer = await Rig.start(tmp_path, injector)
        await rig.gate.control.grant(CONN)
        viewer.pointer(1, 50, 50)
        viewer.key(0x61, True)
        viewer.key(0x61, False)
        await viewer.flush()
        updates = await viewer.follow(1.0)
        assert len(updates) >= MIN_UPDATES_PER_SECOND // 2, "the stream went on"
        assert await _until(lambda: injector.keys == [(0x61, True), (0x61, False)], 3), injector.keys
        await rig.close()

    asyncio.run(main())


# --------------------------------------------------------------------------
# (2) a full frame on connect and after a device change, every pixel masked
# --------------------------------------------------------------------------


def test_the_first_update_and_the_first_after_a_device_change_are_the_whole_masked_frame(
        tmp_path: Path) -> None:
    async def main() -> None:
        rig, viewer = await Rig.start(tmp_path, Injector())
        await rig.gate.control.grant(CONN)
        first = await viewer.read_update()
        desktop = resolve("desktop")
        assert (viewer.width, viewer.height) == (desktop.display_width, desktop.frame_height)
        assert _area(first) == viewer.width * viewer.height, "the first update is the whole frame"
        viewer.request(True)
        await viewer.flush()
        assert len(await viewer.follow(0.5)) >= 2, "the page changes and the viewer is sent it"

        rig.devices.device = phone = resolve("iphone-15")  # the console's device switch
        updates = await viewer.follow(1.5)
        sizes = [i for i, rects in enumerate(updates) if any(enc == DESKTOP_SIZE for *_, enc in rects)]
        assert sizes, "the resize was announced with DesktopSize"
        assert (viewer.width, viewer.height) == (phone.display_width, phone.frame_height)
        after = updates[sizes[-1] + 1:]
        assert after, "a frame follows the DesktopSize"
        assert _area(after[0]) == viewer.width * viewer.height, "the first update after it is the whole frame"
        assert len(after) >= 5, f"{len(after)} updates in the second after the device change"
        x0, y0, x1, y1 = MASKED_BOX
        for y in range(y0, y1):
            for x in range(x0, x1):
                at = (y * viewer.width + x) * 3
                assert tuple(viewer.fb[at:at + 3]) == MASK_RGB, "every pixel under the mask stays masked"
        await rig.close()

    asyncio.run(main())


# --------------------------------------------------------------------------
# (3) keys: noVNC's keysyms are counted and injected while the page changes
# --------------------------------------------------------------------------


def test_keys_are_counted_and_injected_in_order_while_frames_change(tmp_path: Path) -> None:
    async def main() -> None:
        rig, viewer = await Rig.start(tmp_path, Injector())
        await rig.gate.control.grant(CONN)
        sent: list[tuple[int, bool]] = []
        person = asyncio.create_task(_person(viewer, 1.0, sent))
        updates = await viewer.follow(1.0)
        await person
        assert len(updates) >= MIN_UPDATES_PER_SECOND
        assert await _until(lambda: rig.injector.keys == sent, 5), "every key, in order"
        session = rig.session
        assert session is not None
        assert session.counts.key_events == len(sent) > 0
        await rig.close()

    asyncio.run(main())


# --------------------------------------------------------------------------
# (4) a stalled viewer is visible in the gate's log
# --------------------------------------------------------------------------


def test_a_connection_sent_nothing_while_the_frame_changes_is_warned_about(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        monkeypatch.setattr(rfb, "STALL_SECONDS", 0.3, raising=False)
        rig, viewer = await Rig.start(tmp_path, Injector())
        await viewer.read_update()
        # The viewer asks for nothing more -- from the gate's side the same as
        # a request it never got round to reading.
        await asyncio.sleep(1.0)
        await rig.close()

    with caplog.at_level(logging.INFO, logger="agent_worker.livegate.rfb"):
        asyncio.run(main())
    stalls = [r for r in caplog.records if "stalled" in r.getMessage()]
    assert stalls, "a stalled connection is said in the log"
    assert stalls[0].levelno == logging.WARNING
    assert CONN in stalls[0].getMessage()
    closed = [r.getMessage() for r in caplog.records if " closed " in r.getMessage()]
    assert closed and "stalls=1" in closed[-1], closed
