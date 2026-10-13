"""A resumed attempt's live view paints for viewers, and says what page it is on (LB-R6, part of #1030).

MEASURED 2026-10-12, re-QA pass 8 (task_4e0cf1c649634b949703): attempt 2
resumed on GKE with the iPhone 15 preset, the owner took and returned
control, and at 21:28Z a fresh console tab showed LIVE, `https://github.com`
and iPhone 15 over a phone frame that stayed solid black for 30 s, captioned
"iPhone 15 · 393×852 · path not reported".

The strip and the chips are what `live_sessions/{id}` keeps of the gate's
last report; they do not say a tunnel is there. The gate opened its tunnel
once and nothing opened it again (test_live_tunnel_reopens.py has the
worker's half). This file holds the gate's:

  * a tunnel reopened with a fresh ticket serves a viewer that joins later a
    FramebufferUpdate carrying pixels, at the resumed mobile preset's size,
    and the report (device included) is sent again on it, so the relay has
    one for that viewer. The viewer speaks as noVNC 1.4.0 does: Raw, not
    Zlib -- 1.4.0 has no Zlib decoder -- and DesktopSize;
  * a relay end that went silent (no frame, not even the relay's ping, for
    `SILENT_SECONDS`) is closed by the gate, so `status` says `closed` and the
    worker reopens it. A pod that vanished sends no FIN, and a tunnel that
    waits on it for ever is a tunnel nobody reopens;
  * the report carries the top frame's PATH -- redacted, with no query, no
    fragment and no user name -- so the caption under the device says what
    page it is (§6, console decision 11: "<device> · <W>×<H> · <path>").

Offline: the gate runs over test_livegate_main.py's fakes. Every ticket is
built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import struct
import time
from typing import Any

from agent_worker.livegate import report, tunnel

import test_livegate_main as rigs
from test_livegate_main import short_tmp  # noqa: F401  (a pytest fixture, used by name)

# noVNC 1.4.0's `_sendEncodings`, in its order (core/rfb.js): CopyRect, Tight,
# TightPNG, ZRLE, JPEG, Hextile, RRE, Raw, then the pseudo-encodings it asks
# for, DesktopSize (-223) among them. No Zlib (6): 1.4.0 ships no decoder for it.
NOVNC_ENCODINGS = (1, 7, -260, 16, 21, 5, 2, 0, -26, -2, -223, -224, -258, -308, -309, -312, -313, -307,
                   0xC0A1E5CE - (1 << 32), -314, -239)
NOVNC_PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)


class NoVncViewer:
    """The bytes noVNC 1.4.0 sends and reads on one viewer stream, through the relay's frames."""

    def __init__(self, relay: Any, conn_id: str) -> None:
        self.relay, self.conn_id = relay, conn_id
        self.buf = bytearray()
        self.state = "version"
        self.size = (0, 0)
        self.sizes: list[tuple[int, int]] = []
        self.painted: list[tuple[int, int, int, int]] = []
        #: Each update's painted rectangles, in the order they came.
        self.updates: list[list[tuple[int, int, int, int]]] = []
        self._seen = 0

    def _send(self, data: bytes) -> None:
        self.relay.push(tunnel.OP_DATA, self.conn_id, data)

    def pump(self) -> None:
        mine = [f for f in self.relay.frames if f[:2] == (tunnel.OP_DATA, self.conn_id)]
        for _, _, payload in mine[self._seen:]:
            self.buf += payload
            while self._step():
                pass
        self._seen = len(mine)

    def _step(self) -> bool:
        b = self.buf
        if self.state == "version":
            if len(b) < 12:
                return False
            del b[:12]
            self._send(b"RFB 003.008\n")
            self.state = "security"
            return True
        if self.state == "security":
            if len(b) < 1 or len(b) < 1 + b[0]:
                return False
            del b[:1 + b[0]]
            self._send(bytes([1]))
            self.state = "result"
            return True
        if self.state == "result":
            if len(b) < 4:
                return False
            assert struct.unpack(">I", bytes(b[:4]))[0] == 0
            del b[:4]
            self._send(bytes([1]))  # ClientInit: shared
            self.state = "init"
            return True
        if self.state == "init":
            if len(b) < 24:
                return False
            (name,) = struct.unpack(">I", bytes(b[20:24]))
            if len(b) < 24 + name:
                return False
            self.size = struct.unpack(">HH", bytes(b[:4]))
            self.sizes.append(self.size)
            del b[:24 + name]
            self._send(bytes([0, 0, 0, 0]) + NOVNC_PIXEL_FORMAT)
            self._send(struct.pack(f">BxH{len(NOVNC_ENCODINGS)}i", 2, len(NOVNC_ENCODINGS), *NOVNC_ENCODINGS))
            self._send(struct.pack(">BBHHHH", 3, 0, 0, 0, *self.size))
            self.state = "normal"
            return True
        if len(b) < 4:
            return False
        assert b[0] == 0, "the gate sends FramebufferUpdate only"
        (count,) = struct.unpack(">H", bytes(b[2:4]))
        at, rects = 4, []
        for _ in range(count):
            if len(b) < at + 12:
                return False
            x, y, w, h, enc = struct.unpack(">HHHHi", bytes(b[at:at + 12]))
            at += 12
            assert enc in (0, 1, 7, -223, -239), f"the gate sent encoding {enc}, which noVNC 1.4.0 did not ask for"
            if enc == 7:
                # Tight (STREAM-SMOOTH): Fill, or Basic with the copy filter -- never JPEG or PNG.
                if len(b) < at + 1:
                    return False
                kind = b[at] >> 4
                assert kind == 0x08 or kind & 0x08 == 0, "the gate sends lossless Tight only"
                head = 1
                if kind == 0x08:
                    length = 3
                else:
                    head += 1 if kind & 0x04 else 0
                    length = w * h * 3
                    if length >= 12:
                        length = 0
                        for i, shift in enumerate((0, 7, 14)):
                            if len(b) < at + head + 1:
                                return False
                            byte = b[at + head]
                            head += 1
                            length |= (byte & (0x7F if i < 2 else 0xFF)) << shift
                            if not byte & 0x80 or i == 2:
                                break
                length += head
            else:
                # CopyRect (1): the source's x and y. Cursor (-239, RFC 6143 §7.8.1): the
                # shape's pixels, then a 1-bit mask padded to whole bytes per row.
                length = (w * h * 4 if enc == 0 else 4 if enc == 1
                          else w * h * 4 + (w + 7) // 8 * h if enc == -239 else 0)
            if len(b) < at + length:
                return False
            rects.append((x, y, w, h, enc))
            at += length
        del b[:at]
        painted = [(x, y, w, h) for x, y, w, h, enc in rects if enc in (0, 1, 7) and w and h]
        if painted:
            self.updates.append(painted)
        for x, y, w, h, enc in rects:
            if enc == -223:
                self.size = (w, h)
                self.sizes.append(self.size)
            elif enc in (0, 1, 7) and w and h:
                self.painted.append((x, y, w, h))
        self._send(struct.pack(">BBHHHH", 3, 1, 0, 0, *self.size))
        return True


async def _watch(viewer: NoVncViewer, done, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        viewer.pump()
        if done():
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no frame reached the viewer: sizes={viewer.sizes} painted={viewer.painted}")
        await asyncio.sleep(0.01)


def _reports(relay: Any) -> list[dict[str, Any]]:
    return [json.loads(f[2]) for f in relay.frames if f[0] == tunnel.OP_REPORT]


def test_a_reopened_tunnel_paints_a_late_viewer_at_the_resumed_mobile_preset(short_tmp):  # noqa: F811
    async def body() -> None:
        rig = rigs.Rig(short_tmp)
        stop, task = await rigs._started(rig)
        gate = rig.gate
        # The resumed agent puts the browser back on the phone it was using.
        await gate.devices.select("iphone-15")
        await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        await rigs._until(lambda: any(r["device"] and r["device"]["preset"] == "iphone-15"
                                      for r in _reports(rig.relay)))

        # The relay restarts: its end of the tunnel closes.
        rig.relay.inbox.put_nowait(None)
        await rigs._until(lambda: gate.status()["tunnel"] == "closed")

        # The worker hands over a fresh ticket; the gate dials the relay again.
        rig.relay = rigs.FakeRelaySocket()
        assert await gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)}) == {"tunnel": "open"}
        assert gate.status()["tunnel"] == "open"
        await rigs._until(lambda: any(r["device"] and r["device"]["preset"] == "iphone-15"
                                      for r in _reports(rig.relay)))

        # A console tab opened afterwards: one viewer, view only.
        viewer = NoVncViewer(rig.relay, "v-late")
        rig.relay.push(tunnel.OP_OPEN, viewer.conn_id)
        await _watch(viewer, lambda: bool(viewer.painted))
        device = gate.devices.device
        assert viewer.size == (device.display_width, device.frame_height) == (393, 852)
        # The first update is the whole phone frame: its rectangles (tiles,
        # since STREAM-SMOOTH) lie inside it and cover all of it.
        first = viewer.updates[0]
        assert all(x + w <= 393 and y + h <= 852 for x, y, w, h in first)
        assert sum(w * h for _, _, w, h in first) == 393 * 852, "the first update is the whole phone frame"

        stop.set()
        assert await task == 0

    asyncio.run(body())


class _SilentRelay:
    """A relay end that answered `ready` and then nothing: a pod that vanished without a FIN."""

    def __init__(self, *, pings: bool) -> None:
        self.pings = pings
        self.last_heard = time.monotonic()
        self.closed = asyncio.Event()
        self._ready = True

    async def send_text(self, text: str) -> None:
        pass

    async def send_bytes(self, data: bytes) -> None:
        pass

    async def receive(self) -> tuple[bool, bytes] | None:
        if self._ready:
            self._ready = False
            self.last_heard = time.monotonic()
            return True, json.dumps({"type": "ready"}).encode()
        while not self.closed.is_set():
            if self.pings:
                self.last_heard = time.monotonic()  # what WebSocketClient does on every frame, a ping included
            try:
                await asyncio.wait_for(self.closed.wait(), 0.05)
            except asyncio.TimeoutError:
                pass
        return None

    async def close(self, code: int = 1000) -> None:
        self.closed.set()


def test_a_relay_that_went_silent_is_closed_so_the_worker_can_reopen_it():
    async def body() -> None:
        silent = _SilentRelay(pings=False)

        async def connect(url: str) -> _SilentRelay:
            return silent

        t = tunnel.Tunnel("ws://relay.test/v1/tunnel", connect=connect, silent_seconds=0.3)
        await t.open("t." + secrets.token_urlsafe(8))

        async def no_frames(op: int, conn_id: str, payload: bytes) -> None:
            raise AssertionError("no frame was sent")

        await asyncio.wait_for(t.run(no_frames), 3)
        assert t.closed and silent.closed.is_set()

    asyncio.run(body())


def test_a_relay_that_keeps_pinging_keeps_its_tunnel():
    async def body() -> None:
        alive = _SilentRelay(pings=True)

        async def connect(url: str) -> _SilentRelay:
            return alive

        t = tunnel.Tunnel("ws://relay.test/v1/tunnel", connect=connect, silent_seconds=0.3)
        await t.open("t." + secrets.token_urlsafe(8))

        async def no_frames(op: int, conn_id: str, payload: bytes) -> None:
            pass

        running = asyncio.create_task(t.run(no_frames))
        await asyncio.sleep(1.0)
        assert not running.done() and not t.closed, "a relay that answers pings is not silent"
        await t.close()
        await asyncio.wait_for(running, 3)

    asyncio.run(body())


def test_the_websocket_client_hears_a_ping_as_the_relay_being_there():
    async def body() -> None:
        reader = asyncio.StreamReader()
        sent: list[bytes] = []

        class Writer:
            def write(self, data: bytes) -> None:
                sent.append(data)

            async def drain(self) -> None:
                pass

            def is_closing(self) -> bool:
                return False

            def close(self) -> None:
                pass

        ws = tunnel.WebSocketClient(reader, Writer())  # type: ignore[arg-type]
        before = ws.last_heard
        await asyncio.sleep(0.02)
        reader.feed_data(bytes([0x89, 0x00]))  # PING, empty, unmasked (the server's)
        reader.feed_data(bytes([0x82, 0x01, 0x07]))  # one binary byte
        assert await ws.receive() == (False, b"\x07")
        assert ws.last_heard > before
        assert sent, "the ping was answered"

    asyncio.run(body())


# --------------------------------------------------------------------------
# The path, for the caption under the device
# --------------------------------------------------------------------------


def test_the_report_carries_the_top_frames_path_without_query_or_fragment():
    origin = report.page_origin("https://github.com/settings/profile?tab=emails#primary", "github.com")
    assert origin is not None
    assert origin["origin"] == "https://github.com"
    assert origin["path"] == "/settings/profile"


def test_an_origin_with_no_path_reports_the_root():
    assert report.page_origin("https://github.com", "github.com")["path"] == "/"


def test_a_secret_in_the_path_is_redacted_before_it_leaves_the_gate():
    token = "ghp_" + secrets.token_hex(18)
    origin = report.page_origin(f"https://example.test/reset/{token}/confirm", "example.test")
    assert token not in json.dumps(origin)
    assert origin["path"].startswith("/reset/") and origin["path"].endswith("/confirm")


def test_a_user_name_in_the_url_is_never_the_path():
    who = "ada"
    origin = report.page_origin(f"https://{who}:{secrets.token_hex(6)}@example.test/home", "example.test")
    assert origin["path"] == "/home"
    assert who not in json.dumps(origin)


def test_a_long_path_is_cut_and_says_so_and_the_report_still_fits():
    origin = report.page_origin("https://example.test/" + "a" * 5000, "example.test")
    assert len(origin["path"]) <= report.MAX_PATH_CHARS
    assert origin["path"].endswith("…")
    report.encode_report(origin, None)  # within the relay's limit


def test_the_gate_reports_the_path_the_top_frame_navigated_to(short_tmp):  # noqa: F811
    async def body() -> None:
        rig = rigs.Rig(short_tmp)
        stop, task = await rigs._started(rig)
        await rig.gate.open_tunnel({"ticket": "t." + secrets.token_urlsafe(8)})
        rigs._navigated(rig, "https://github.com/bogdan/swarm/pulls?q=is%3Aopen", "github.com")
        await rigs._until(lambda: any((r["origin"] or {}).get("path") == "/bogdan/swarm/pulls"
                                      for r in _reports(rig.relay)))
        stop.set()
        assert await task == 0

    asyncio.run(body())
