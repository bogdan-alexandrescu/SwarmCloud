"""The relay's recorder gets its RFB stream back when the gate drops it (part of #1346).

#1346 comment 5: the recorder's view of a live-browser task was "unavailable"
for 11.7-17 s on 5 of 28 QA runs, over the 10 s limit `check_qa.py`'s
`mask_blank` holds a run to. The recorder is the relay's own RFB client
(`swarm_live.recorder.Recorder`), on a connection the relay opens to the gate
(`livegate/rfb.py::RfbSession`) when the tunnel opens. Until this change a
connection the gate ended -- the gate's `StreamMux` sends OP_CLOSE when an
`RfbSession` returns -- was never opened again: `Rendezvous.from_tunnel`
dropped an OP_CLOSE for the recorder's id on the floor, the recorder kept the
last frame it had decoded, and the rest of the session was recorded as that
one frame. These tests drive the relay as the gate does, over its tunnel
frames, and hold it to opening a new recorder connection within the budget.

Nothing here needs a credential, an emulator or ffmpeg: the segment writer and
the encoder are in-memory doubles, and every key and ticket is built at
runtime (`secrets.token_bytes`, `mint_ticket`).
"""

from __future__ import annotations

import asyncio
import secrets
import struct
import sys
from pathlib import Path

# swarm-live is not a member of the root uv workspace; its package is put on
# the path the way tests/unit/live/test_relay.py does.
_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_live import recorder as rec  # noqa: E402
from swarm_live import rendezvous as rz  # noqa: E402
from swarm_live.rendezvous import (  # noqa: E402
    OP_CLOSE,
    OP_DATA,
    OP_OPEN,
    Rendezvous,
    decode_frame,
    encode_frame,
    mint_ticket,
)

#: `check_qa.py`'s `mask_blank` limit: a run whose recorder view is
#: unavailable for longer than this fails (#1346 comment 5).
VIEW_UNAVAILABLE_LIMIT_SECONDS = 10.0

TENANT, TASK, ATTEMPT, SESSION, GEN = "eng", "task_r1", "att_r1", "ls_r1", 4


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Endpoint:
    """The gate's websocket, as the relay writes to it."""

    def __init__(self) -> None:
        self.binary: list[bytes] = []
        self.text: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send_bytes(self, data: bytes) -> None:
        self.binary.append(data)

    async def send_text(self, text: str) -> None:
        self.text.append(text)

    async def close(self, code: int, reason: str) -> None:
        if self.closed is None:
            self.closed = (code, reason)

    def opens(self) -> list[str]:
        return [conn for op, conn, _ in (decode_frame(b) for b in self.binary) if op == OP_OPEN]


class Writer:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def write(self, name: str, data: bytes, content_type: str) -> None:
        self.objects[name] = (data, content_type)


class Stream:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def write(self, rgb: bytes) -> None:
        self.frames.append(rgb)

    async def finish(self) -> bytes:
        return b"%d" % len(self.frames)


class Encoder:
    def __init__(self) -> None:
        self.streams: list[Stream] = []

    async def open(self, width: int, height: int, fps: int) -> Stream:
        stream = Stream()
        self.streams.append(stream)
        return stream


def gate_handshake(width: int = 4, height: int = 2, pixel: tuple[int, int, int] = (9, 9, 9)) -> list[bytes]:
    """What `RfbSession` sends a new connection: version, security, ServerInit, one Raw update."""
    name = b"swarm live browser"
    fmt = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
    init = struct.pack(">I", 0) + struct.pack(">HH", width, height) + fmt + struct.pack(">I", len(name)) + name
    r, g, b = pixel
    update = (struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, width, height, 0)
              + bytes([r, g, b, 0]) * (width * height))
    return [b"RFB 003.008\n", bytes([1, 1]), init, update]


def build(**kwargs):
    key, clock, writer, encoder = secrets.token_bytes(32), Clock(), Writer(), Encoder()
    relay = Rendezvous(key, clock=clock, recorder_factory=lambda s, send: rec.Recorder(
        tenant_id=s.tenant_id, task_id=s.task_id, attempt_id=s.attempt_id,
        send=send, writer=writer, encoder=encoder, clock=clock), **kwargs)
    raw = mint_ticket(key, session_id=SESSION, tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT,
                      generation=GEN, role="tunnel", now=clock())
    return relay, raw, encoder


async def connect(relay: Rendezvous, tunnel: rz.Tunnel, conn: str, pixel=(9, 9, 9)) -> None:
    for chunk in gate_handshake(pixel=pixel):
        await relay.from_tunnel(tunnel, encode_frame(OP_DATA, conn, chunk))


async def wait_for_open(relay: Rendezvous, gate: Endpoint, count: int, limit: float) -> float | None:
    """Seconds until the gate has been sent `count` OPENs, or None past `limit`."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < limit:
        await relay.flush()
        if len(gate.opens()) >= count:
            return loop.time() - start
        await asyncio.sleep(0.02)
    return None


def test_a_recorder_connection_the_gate_closes_is_opened_again_within_the_budget():
    relay, raw, encoder = build()
    gate = Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(raw, gate)
        await relay.flush()
        (first,) = gate.opens()
        await connect(relay, tunnel, first)
        recorder = relay.recorder(SESSION)
        assert recorder is not None and recorder.ready
        await recorder.tick()

        # The gate's RfbSession for the recorder returned: its mux says so.
        await relay.from_tunnel(tunnel, encode_frame(OP_CLOSE, first))
        took = await wait_for_open(relay, gate, 2, VIEW_UNAVAILABLE_LIMIT_SECONDS)
        assert took is not None, (
            "the gate closed the recorder's connection and the relay never opened another")
        assert took < VIEW_UNAVAILABLE_LIMIT_SECONDS
        second = gate.opens()[1]
        assert second.startswith("rec-") and second != first
        assert relay.recorder(SESSION) is recorder and recorder.conn_id == second
        # Until the new stream has a frame, nothing stale is recorded as current.
        assert not recorder.ready

        # Bytes still in flight on the dead connection reach nobody.
        await relay.from_tunnel(tunnel, encode_frame(OP_DATA, first, gate_handshake()[0]))
        await connect(relay, tunnel, second, pixel=(200, 10, 10))
        assert recorder.ready
        await recorder.tick()
        assert encoder.streams[-1].frames[-1][:3] == bytes([200, 10, 10])
        await relay.close_tunnel(tunnel)

    asyncio.run(go())


def test_the_reopen_backoff_starts_fast_and_its_first_tries_fit_the_budget():
    delays = rz.RECORDER_REOPEN_DELAYS
    assert delays and all(d >= 0 for d in delays)
    assert list(delays) == sorted(delays), "the backoff never shortens"
    assert delays[0] <= 0.5
    # Three tries before the view has been away for the QA limit.
    assert sum(delays[:3]) < VIEW_UNAVAILABLE_LIMIT_SECONDS
    assert delays[-1] < VIEW_UNAVAILABLE_LIMIT_SECONDS


def test_a_reopen_that_never_gets_a_frame_backs_off_and_a_frame_resets_it():
    relay, raw, _ = build(recorder_reopen_delays=(0.0, 0.3))
    gate = Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(raw, gate)
        await relay.flush()
        (first,) = gate.opens()
        await connect(relay, tunnel, first)
        await relay.from_tunnel(tunnel, encode_frame(OP_CLOSE, first))
        assert await wait_for_open(relay, gate, 2, 0.2) is not None
        # Closed again before a frame: the next try waits the longer delay.
        await relay.from_tunnel(tunnel, encode_frame(OP_CLOSE, gate.opens()[1]))
        assert await wait_for_open(relay, gate, 3, 0.1) is None
        assert await wait_for_open(relay, gate, 3, 1.0) is not None
        # This one gets a frame, so the next drop is retried at once again.
        third = gate.opens()[2]
        await connect(relay, tunnel, third)
        await relay.from_tunnel(tunnel, encode_frame(OP_CLOSE, third))
        assert await wait_for_open(relay, gate, 4, 0.2) is not None
        await relay.close_tunnel(tunnel)

    asyncio.run(go())


def test_a_tunnel_that_ends_while_a_reopen_waits_opens_nothing():
    relay, raw, _ = build(recorder_reopen_delays=(0.2,))
    gate = Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(raw, gate)
        await relay.flush()
        (first,) = gate.opens()
        await connect(relay, tunnel, first)
        await relay.from_tunnel(tunnel, encode_frame(OP_CLOSE, first))
        await relay.close_tunnel(tunnel)
        await asyncio.sleep(0.4)
        assert gate.opens() == [first]
        assert relay.recorder(SESSION) is None
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not [t for t in pending if "reopen" in repr(t.get_coro())]

    asyncio.run(go())


def test_a_close_for_a_viewer_does_not_reopen_the_recorder():
    relay, raw, _ = build(recorder_reopen_delays=(0.0,))
    gate = Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(raw, gate)
        await relay.flush()
        (first,) = gate.opens()
        await connect(relay, tunnel, first)
        await relay.from_tunnel(tunnel, encode_frame(OP_CLOSE, "v-not-the-recorder"))
        assert await wait_for_open(relay, gate, 2, 0.2) is None
        assert relay.recorder(SESSION).conn_id == first
        await relay.close_tunnel(tunnel)

    asyncio.run(go())
