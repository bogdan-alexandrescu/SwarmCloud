"""The live-browser relay, swarm-live (LB-L7, docs/design/live-browser.md §3.3, §5.2, §5.10, §7).

Acceptance for the lane: a ticket used twice, expired, or for another tenant,
session or generation is refused; one tunnel per session; viewer caps; segments
written only under `.../live/` with the two suffixes.

And for LB-L8d (owner decisions 2026-10-11 (a)-(c)): the relay starts and
serves with no Firestore client and no certificate, on plain WS port 8080,
with its key read by Secret Manager version reference; a session is what its
tunnel ticket names, and every later ticket must match it; a close request
from swarm-api drops every connection of its session.

Every key and ticket here is built at runtime (`secrets.token_bytes`,
`mint_ticket`), never written as a literal. Nothing needs a credential, an
emulator or ffmpeg: the segment writer, the encoder and Secret Manager are
in-memory doubles defined in this file.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import struct
import sys
import zlib
from pathlib import Path

import pytest

# swarm-live is not (yet) a member of the root uv workspace -- the root
# pyproject and uv.lock are outside this lane -- so its package is put on the
# path here, the way tests/unit/reconciler/conftest.py reaches the worker suite.
_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_live import recorder as rec  # noqa: E402
from swarm_live import rendezvous as rz  # noqa: E402
from swarm_live.rendezvous import (  # noqa: E402
    CLOSE_BUSY,
    CLOSE_ENDED,
    CLOSE_FULL,
    CLOSE_REFUSED,
    OP_ACTION,
    OP_CLOSE,
    OP_CONTROL,
    OP_DATA,
    OP_OPEN,
    Refused,
    Rendezvous,
    decode_frame,
    encode_frame,
    mint_close,
    mint_ticket,
    verify_close,
    verify_ticket,
)

# ---------------------------------------------------------------- doubles


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Endpoint:
    """One websocket's far end: what the relay sent it, and whether it closed it."""

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

    def frames(self) -> list[tuple[int, str, bytes]]:
        return [decode_frame(b) for b in self.binary]


class Writer:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def write(self, name: str, data: bytes, content_type: str) -> None:
        assert name not in self.objects, "a segment is created once, never overwritten"
        self.objects[name] = (data, content_type)


class Stream:
    def __init__(self, width: int, height: int, fps: int) -> None:
        self.width, self.height, self.fps = width, height, fps
        self.frames: list[bytes] = []

    async def write(self, rgb: bytes) -> None:
        assert len(rgb) == self.width * self.height * 3
        self.frames.append(rgb)

    async def finish(self) -> bytes:
        return json.dumps({"w": self.width, "h": self.height, "fps": self.fps,
                           "frames": len(self.frames)}).encode()


class Encoder:
    def __init__(self) -> None:
        self.streams: list[Stream] = []

    async def open(self, width: int, height: int, fps: int) -> Stream:
        stream = Stream(width, height, fps)
        self.streams.append(stream)
        return stream


TENANT, TASK, ATTEMPT, SESSION, GEN = "eng", "task_a1", "att_a1", "ls_a1", 3


def ticket(key: bytes, clock: Clock, role: str = "tunnel", **overrides) -> str:
    fields = dict(session_id=SESSION, tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT,
                  generation=GEN, role=role, now=clock())
    if role != "tunnel":
        fields["email"] = "viewer@example.test"
    fields.update(overrides)
    return mint_ticket(key, **fields)


def build(**kwargs):
    key = secrets.token_bytes(32)
    clock = Clock()
    relay = Rendezvous(key, clock=clock, **kwargs)
    return key, clock, relay


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- tickets


def test_a_minted_ticket_verifies_and_names_what_it_was_minted_for():
    key, clock = secrets.token_bytes(32), Clock()
    t = verify_ticket(key, ticket(key, clock, "viewer"), now=clock())
    assert (t.session_id, t.tenant_id, t.task_id, t.attempt_id, t.generation, t.role, t.email) == (
        SESSION, TENANT, TASK, ATTEMPT, GEN, "viewer", "viewer@example.test")
    assert t.exp == int(clock()) + rz.TICKET_TTL_SECONDS["viewer"]


def test_a_ticket_under_another_key_or_altered_is_refused():
    key, clock = secrets.token_bytes(32), Clock()
    raw = ticket(key, clock)
    with pytest.raises(Refused) as e:
        verify_ticket(secrets.token_bytes(32), raw, now=clock())
    assert e.value.reason == "bad_signature"
    body, mac = raw.split(".")
    forged = json.loads(rz._b64decode(body))
    forged["tenant_id"] = "other"
    with pytest.raises(Refused) as e:
        verify_ticket(key, rz._b64encode(json.dumps(forged).encode()) + "." + mac, now=clock())
    assert e.value.reason == "bad_signature"
    for junk in ("", "x", "a.b.c", "....", "%%%.%%%"):
        with pytest.raises(Refused):
            verify_ticket(key, junk, now=clock())


def test_an_expired_ticket_is_refused():
    key, clock = secrets.token_bytes(32), Clock()
    raw = ticket(key, clock, "viewer")
    clock.now += rz.TICKET_TTL_SECONDS["viewer"] + 1
    with pytest.raises(Refused) as e:
        verify_ticket(key, raw, now=clock())
    assert e.value.reason == "expired"


def test_a_ticket_living_longer_than_its_role_allows_is_refused():
    # A minting bug that issued hour-long tickets must not become replayable ones.
    key, clock = secrets.token_bytes(32), Clock()
    raw = ticket(key, clock, "viewer", ttl=3600)
    with pytest.raises(Refused) as e:
        verify_ticket(key, raw, now=clock())
    assert e.value.reason == "lifetime"


def test_a_persons_ticket_must_name_the_person_and_the_gates_must_not():
    key, clock = secrets.token_bytes(32), Clock()
    for role, email in (("viewer", ""), ("control", ""), ("tunnel", "a@example.test")):
        with pytest.raises(Refused) as e:
            verify_ticket(key, ticket(key, clock, role, email=email), now=clock())
        assert e.value.reason == "malformed"


def test_a_short_key_is_refused_at_construction():
    with pytest.raises(ValueError):
        Rendezvous(secrets.token_bytes(8))


# ---------------------------------------------------------------- tunnels


def test_a_tunnel_opens_with_its_ticket_and_the_relay_opens_no_viewer_until_one_joins():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        await relay.flush()

    run(go())
    assert gate.closed is None
    assert relay.tunnel_count() == 1
    assert gate.frames() == []


def test_a_tunnel_ticket_used_twice_is_refused():
    key, clock, relay = build()
    raw = ticket(key, clock)

    async def go():
        tunnel = await relay.open_tunnel(raw, Endpoint())
        await relay.close_tunnel(tunnel)
        with pytest.raises(Refused) as e:
            await relay.open_tunnel(raw, Endpoint())
        assert e.value.reason == "replayed"
        assert e.value.code == CLOSE_REFUSED

    run(go())


def test_a_tunnel_ticket_defines_its_session_with_no_document_read():
    # Decision (a): the relay has no Firestore client. What the session IS --
    # tenant, task, attempt, generation -- is what swarm-api MAC'd into the
    # tunnel ticket, and nothing else.
    key, clock, relay = build()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), Endpoint())
        return tunnel.session

    session = run(go())
    assert (session.session_id, session.tenant_id, session.task_id, session.attempt_id,
            session.generation) == (SESSION, TENANT, TASK, ATTEMPT, GEN)
    assert not hasattr(relay, "_store")


def test_a_tunnel_for_a_closed_session_is_refused():
    key, clock, relay = build()

    async def go():
        await relay.close_session(SESSION)
        with pytest.raises(Refused) as e:
            await relay.open_tunnel(ticket(key, clock), Endpoint())
        assert e.value.reason == "session_closed"
        assert e.value.code == CLOSE_REFUSED

    run(go())
    assert relay.tunnel_count() == 0


def test_a_viewer_ticket_cannot_open_a_tunnel():
    key, clock, relay = build()

    async def go():
        with pytest.raises(Refused) as e:
            await relay.open_tunnel(ticket(key, clock, "viewer"), Endpoint())
        assert e.value.reason == "wrong_role"

    run(go())


def test_one_tunnel_per_session_and_the_first_wins():
    key, clock, relay = build()
    first, second = Endpoint(), Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), first)
        with pytest.raises(Refused) as e:
            await relay.open_tunnel(ticket(key, clock), second)
        assert e.value.reason == "tunnel_exists"
        assert e.value.code == CLOSE_BUSY
        assert first.closed is None
        # Once the first is gone, a fresh ticket opens a fresh tunnel.
        await relay.close_tunnel(tunnel)
        await relay.open_tunnel(ticket(key, clock), Endpoint())

    run(go())
    assert relay.tunnel_count() == 1


# ---------------------------------------------------------------- viewers


def test_a_viewer_is_spliced_to_the_tunnel_both_ways_under_its_own_connection_id():
    key, clock, relay = build()
    gate, a, b = Endpoint(), Endpoint(), Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), gate)
        va = await relay.open_viewer(ticket(key, clock, "viewer"), a)
        vb = await relay.open_viewer(ticket(key, clock, "viewer", email="b@example.test"), b)
        await relay.from_viewer(va, b"RFB 003.008\n")
        await relay.from_tunnel(tunnel, encode_frame(OP_DATA, va.conn_id, b"to-a"))
        await relay.from_tunnel(tunnel, encode_frame(OP_DATA, vb.conn_id, b"to-b"))
        await relay.from_tunnel(tunnel, encode_frame(OP_DATA, "nobody", b"dropped"))
        await relay.flush()
        return va, vb

    va, vb = run(go())
    assert va.conn_id != vb.conn_id
    assert gate.frames() == [(OP_OPEN, va.conn_id, b""), (OP_OPEN, vb.conn_id, b""),
                             (OP_DATA, va.conn_id, b"RFB 003.008\n")]
    assert a.binary == [b"to-a"] and b.binary == [b"to-b"]


def test_a_viewer_that_leaves_is_closed_at_the_gate_and_one_the_gate_closes_is_closed_here():
    key, clock, relay = build()
    gate, a, b = Endpoint(), Endpoint(), Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), gate)
        va = await relay.open_viewer(ticket(key, clock, "viewer"), a)
        vb = await relay.open_viewer(ticket(key, clock, "viewer"), b)
        await relay.close_viewer(va)
        await relay.from_tunnel(tunnel, encode_frame(OP_CLOSE, vb.conn_id))
        await relay.flush()
        return va

    va = run(go())
    assert (OP_CLOSE, va.conn_id, b"") in gate.frames()
    assert b.closed is not None and b.closed[0] == CLOSE_ENDED
    assert relay.viewer_count() == 0


def test_a_viewer_ticket_used_twice_is_refused():
    key, clock, relay = build()
    raw = ticket(key, clock, "viewer")

    async def go():
        await relay.open_tunnel(ticket(key, clock), Endpoint())
        await relay.open_viewer(raw, Endpoint())
        with pytest.raises(Refused) as e:
            await relay.open_viewer(raw, Endpoint())
        assert e.value.reason == "replayed"

    run(go())


def test_an_expired_viewer_ticket_is_refused():
    key, clock, relay = build()

    async def go():
        await relay.open_tunnel(ticket(key, clock), Endpoint())
        raw = ticket(key, clock, "viewer")
        clock.now += rz.TICKET_TTL_SECONDS["viewer"] + 1
        with pytest.raises(Refused) as e:
            await relay.open_viewer(raw, Endpoint())
        assert e.value.reason == "expired"

    run(go())


@pytest.mark.parametrize("field,value,reason", [
    ("tenant_id", "other-tenant", "wrong_tenant"),
    ("task_id", "task_other", "wrong_task"),
    ("attempt_id", "att_other", "wrong_attempt"),
    ("generation", GEN - 1, "wrong_generation"),
    # Session B's id with session A's task: refused against B's tunnel ticket.
    ("session_id", "ls_other", "wrong_task"),
])
def test_a_viewer_ticket_for_another_tenant_session_or_generation_is_refused(field, value, reason):
    key, clock, relay = build()

    async def go():
        await relay.open_tunnel(ticket(key, clock), Endpoint())
        # Another session that IS live, so "another session" is refused for
        # naming a session other than the one it was minted for, not merely an
        # absent one.
        await relay.open_tunnel(ticket(key, clock, session_id="ls_other", task_id="task_b"), Endpoint())
        with pytest.raises(Refused) as e:
            await relay.open_viewer(ticket(key, clock, "viewer", **{field: value}), Endpoint())
        assert e.value.code == CLOSE_REFUSED
        return e.value.reason

    assert run(go()) == reason
    assert relay.viewer_count() == 0


def test_a_tunnel_ticket_cannot_open_a_viewer():
    key, clock, relay = build()

    async def go():
        await relay.open_tunnel(ticket(key, clock), Endpoint())
        with pytest.raises(Refused) as e:
            await relay.open_viewer(ticket(key, clock), Endpoint())
        assert e.value.reason == "wrong_role"

    run(go())


def test_a_viewer_with_no_tunnel_is_refused():
    key, clock, relay = build()

    async def go():
        with pytest.raises(Refused) as e:
            await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        assert e.value.reason == "no_tunnel"

    run(go())


def test_at_most_ten_viewers_per_task():
    key, clock, relay = build()

    async def go():
        await relay.open_tunnel(ticket(key, clock), Endpoint())
        viewers = [await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
                   for _ in range(rz.MAX_VIEWERS_PER_TASK)]
        with pytest.raises(Refused) as e:
            await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        assert e.value.reason == "viewers_per_task"
        assert e.value.code == CLOSE_FULL
        await relay.close_viewer(viewers[0])
        await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())

    assert rz.MAX_VIEWERS_PER_TASK == 10
    run(go())
    assert relay.viewer_count(task_id=TASK) == 10


def test_at_most_fifty_viewers_per_tenant_across_its_tasks():
    key, clock, relay = build()
    tasks = [f"task_{i}" for i in range(6)]

    async def go():
        for i, task in enumerate(tasks):
            await relay.open_tunnel(ticket(key, clock, session_id=f"ls_{i}", task_id=task), Endpoint())
        opened = 0
        for i, task in enumerate(tasks[:5]):
            for _ in range(10):
                await relay.open_viewer(
                    ticket(key, clock, "viewer", session_id=f"ls_{i}", task_id=task), Endpoint())
                opened += 1
        with pytest.raises(Refused) as e:
            await relay.open_viewer(
                ticket(key, clock, "viewer", session_id="ls_5", task_id="task_5"), Endpoint())
        assert e.value.reason == "viewers_per_tenant"
        assert e.value.code == CLOSE_FULL
        # Another tenant is not counted against this one.
        await relay.open_tunnel(ticket(key, clock, session_id="ls_x", tenant_id="ops",
                                       task_id="task_x"), Endpoint())
        await relay.open_viewer(ticket(key, clock, "viewer", session_id="ls_x", tenant_id="ops",
                                       task_id="task_x"), Endpoint())
        return opened

    assert rz.MAX_VIEWERS_PER_TENANT == 50
    assert run(go()) == 50
    assert relay.viewer_count(tenant_id=TENANT) == 50


def test_a_slow_viewer_is_dropped_instead_of_stalling_the_tunnel():
    key, clock, relay = build(queue_size=4)

    class Stuck(Endpoint):
        async def send_bytes(self, data: bytes) -> None:
            await asyncio.sleep(3600)

    stuck, fine = Stuck(), Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), Endpoint())
        vs = await relay.open_viewer(ticket(key, clock, "viewer"), stuck)
        vf = await relay.open_viewer(ticket(key, clock, "viewer"), fine)
        for i in range(20):
            await relay.from_tunnel(tunnel, encode_frame(OP_DATA, vs.conn_id, b"x"))
            await relay.from_tunnel(tunnel, encode_frame(OP_DATA, vf.conn_id, bytes([i])))
            await asyncio.sleep(0)  # as a websocket read would between messages
        await asyncio.wait_for(relay.flush(), 5)

    run(go())
    assert stuck.closed is not None
    assert fine.closed is None
    assert len(fine.binary) == 20


# ---------------------------------------------------------------- control


def test_control_needs_a_control_ticket_for_this_viewer_and_reaches_the_gate():
    key, clock, relay = build()
    gate, a = Endpoint(), Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        va = await relay.open_viewer(ticket(key, clock, "viewer", email="a@example.test"), a)
        # A viewer cannot grant itself control without swarm-api's ticket.
        await relay.viewer_text(va, json.dumps({"type": "control"}))
        # Nor with a ticket minted for another person, or a viewer ticket.
        await relay.viewer_text(va, json.dumps({"type": "control", "ticket": ticket(
            key, clock, "control", email="b@example.test")}))
        await relay.viewer_text(va, json.dumps({"type": "control", "ticket": ticket(
            key, clock, "viewer", email="a@example.test")}))
        await relay.flush()
        assert [f for f in gate.frames() if f[0] == OP_CONTROL] == []
        refused = [t for t in a.text if json.loads(t)["type"] == "refused"]
        assert len(refused) == 3
        await relay.viewer_text(va, json.dumps({"type": "control", "ticket": ticket(
            key, clock, "control", email="a@example.test")}))
        await relay.viewer_text(va, json.dumps({"type": "control", "ticket": ticket(
            key, clock, "release", email="a@example.test")}))
        await relay.flush()
        return va

    va = run(go())
    assert [f for f in gate.frames() if f[0] == OP_CONTROL] == [
        (OP_CONTROL, va.conn_id, b""), (OP_CONTROL, "", b"")]


def test_a_controller_that_leaves_releases_control():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        va = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        await relay.viewer_text(va, json.dumps({"type": "control", "ticket": ticket(key, clock, "control")}))
        assert relay.controller(SESSION) == va.conn_id
        await relay.close_viewer(va)
        await relay.flush()
        return va

    va = run(go())
    assert relay.controller(SESSION) is None
    assert gate.frames()[-1] == (OP_CLOSE, va.conn_id, b"")


# ---------------------------------------------------------------- the session moves


def test_a_close_request_drops_a_connected_viewer_and_its_tunnel_and_nothing_else():
    key, clock, relay = build()
    gate, viewer, gate_b, viewer_b = Endpoint(), Endpoint(), Endpoint(), Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        await relay.open_viewer(ticket(key, clock, "viewer"), viewer)
        await relay.open_tunnel(ticket(key, clock, session_id="ls_b", task_id="task_b"), gate_b)
        await relay.open_viewer(ticket(key, clock, "viewer", session_id="ls_b", task_id="task_b"),
                                viewer_b)
        return await relay.close_request(mint_close(key, SESSION, now=clock()))

    assert run(go()) == (SESSION, 2)
    assert viewer.closed == (CLOSE_ENDED, "session_ended")
    assert gate.closed == (CLOSE_ENDED, "session_ended")
    # Another session is untouched.
    assert gate_b.closed is None and viewer_b.closed is None
    assert relay.tunnel_count() == 1 and relay.viewer_count() == 1


def test_a_closed_session_refuses_every_ticket_minted_before_the_close():
    key, clock, relay = build()

    async def go():
        await relay.open_tunnel(ticket(key, clock), Endpoint())
        in_flight = [ticket(key, clock), ticket(key, clock, "viewer")]
        await relay.close_request(mint_close(key, SESSION, now=clock()))
        for raw, opener in zip(in_flight, (relay.open_tunnel, relay.open_viewer)):
            with pytest.raises(Refused) as e:
                await opener(raw, Endpoint())
            assert e.value.reason == "session_closed"
        # Remembered until every ticket minted before the close has expired.
        clock.now += rz.CLOSED_MEMORY_SECONDS + 1
        assert not relay.is_closed(SESSION)

    run(go())


def test_a_close_request_must_be_swarm_apis_fresh_and_used_once():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        for raw, reason in (
            (mint_close(secrets.token_bytes(32), SESSION, now=clock()), "bad_signature"),
            # A ticket's MAC is under another domain: a ticket is never a close.
            (ticket(key, clock), "bad_signature"),
            (mint_close(key, SESSION, now=clock() - rz.CLOSE_TTL_SECONDS - 1), "expired"),
            (mint_close(key, SESSION, now=clock(), ttl=3600), "lifetime"),
            ("not-a-close", "malformed"),
        ):
            with pytest.raises(Refused) as e:
                await relay.close_request(raw)
            assert e.value.reason == reason, raw
        assert gate.closed is None
        raw = mint_close(key, SESSION, now=clock())
        await relay.close_request(raw)
        with pytest.raises(Refused) as e:
            await relay.close_request(raw)
        assert e.value.reason == "replayed"

    run(go())
    assert verify_close(key, mint_close(key, "ls_z", now=1000.0), now=1000.0).session_id == "ls_z"


def test_a_control_ticket_for_the_session_with_another_generation_is_refused():
    key, clock, relay = build()
    gate, a = Endpoint(), Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        va = await relay.open_viewer(ticket(key, clock, "viewer"), a)
        await relay.viewer_text(va, json.dumps({"type": "control", "ticket": ticket(
            key, clock, "control", generation=GEN + 1)}))
        await relay.flush()
        return va

    run(go())
    assert relay.controller(SESSION) is None
    assert [json.loads(t) for t in a.text][-1] == {"type": "refused", "reason": "control",
                                                  "cause": "wrong_generation"}


def test_a_tunnel_that_drops_closes_its_viewers():
    key, clock, relay = build()
    viewer = Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), Endpoint())
        await relay.open_viewer(ticket(key, clock, "viewer"), viewer)
        await relay.close_tunnel(tunnel)

    run(go())
    assert viewer.closed is not None and viewer.closed[0] == CLOSE_ENDED


# ---------------------------------------------------------------- the gate's report (LB-DEV-A)

ORIGIN = {"origin": "https://github.com", "host": "github.com", "display_host": "github.com",
          "registrable_domain": "github.com", "scheme": "https:", "ip_literal": False, "cert_error": None}
DEVICE = {"preset": "iphone-15", "orientation": "portrait", "touch": True}


def _report(**fields) -> bytes:
    return encode_frame(rz.OP_REPORT, "", json.dumps(fields).encode())


def _reports(endpoint: Endpoint) -> list[dict]:
    return [m for m in (json.loads(t) for t in endpoint.text) if m.get("type") == "report"]


def test_the_gates_report_reaches_every_viewer_and_a_late_joiner_gets_the_latest():
    """The top frame's origin and the device travel gate -> relay -> each
    viewer link (swarm-api's proxy, which records them). The relay keeps the
    last one for a viewer that joins later, and it is sent after `ready`."""
    key, clock, relay = build()
    gate, a, late = Endpoint(), Endpoint(), Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), gate)
        await relay.open_viewer(ticket(key, clock, "viewer"), a)
        await relay.from_tunnel(tunnel, _report(origin=None, device=DEVICE))
        await relay.from_tunnel(tunnel, _report(origin=ORIGIN, device=DEVICE))
        await relay.open_viewer(ticket(key, clock, "viewer", email="late@example.test"), late)
        await relay.flush()
        return relay.report(SESSION)

    stored = run(go())
    assert stored == {"origin": ORIGIN, "device": DEVICE}
    assert _reports(a) == [{"type": "report", "origin": None, "device": DEVICE},
                           {"type": "report", "origin": ORIGIN, "device": DEVICE}]
    assert json.loads(late.text[0]) == {"type": "ready"}
    assert _reports(late) == [{"type": "report", "origin": ORIGIN, "device": DEVICE}]
    # A report is the relay's to keep, never the gate's stream: nothing went back.
    assert all(op == OP_OPEN for op, _, _ in gate.frames())


@pytest.mark.parametrize("payload", [
    b"not json",
    b"[1, 2]",
    json.dumps({"origin": ORIGIN, "device": DEVICE, "url": "https://github.com/login?token=x"}).encode(),
    json.dumps({"origin": "https://github.com", "device": DEVICE}).encode(),
    json.dumps({"origin": {**ORIGIN, "host": "h" * rz.MAX_REPORT_BYTES}, "device": DEVICE}).encode(),
])
def test_a_report_that_is_not_two_objects_or_is_too_large_is_dropped(payload):
    """Origin is untrusted page data: the relay keeps a report only when it is
    a small JSON object of `origin` and `device`, each an object or null.
    swarm-api validates every field again before it stores one."""
    key, clock, relay = build()
    gate, a = Endpoint(), Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), gate)
        await relay.open_viewer(ticket(key, clock, "viewer"), a)
        await relay.from_tunnel(tunnel, encode_frame(rz.OP_REPORT, "", payload))
        await relay.flush()
        return relay.report(SESSION)

    assert run(go()) is None
    assert _reports(a) == []


def test_a_report_with_the_agents_connection_is_kept_and_passed_on():
    """LIVE-UNUSED (#1030): a gate reports whether the agent is driving its
    browser as a third key, `agent`. The relay keeps and passes it like the
    other two; a report without it is kept as before (an older gate)."""
    key, clock, relay = build()
    gate, a = Endpoint(), Endpoint()
    agent = {"connected_at": None, "connections": 0, "first_frame_at": 1_791_900_000.0}

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), gate)
        await relay.open_viewer(ticket(key, clock, "viewer"), a)
        await relay.from_tunnel(tunnel, _report(origin=ORIGIN, device=DEVICE, agent=agent))
        await relay.flush()
        return relay.report(SESSION)

    assert run(go()) == {"origin": ORIGIN, "device": DEVICE, "agent": agent}
    assert _reports(a) == [{"type": "report", "agent": agent, "origin": ORIGIN, "device": DEVICE}]


@pytest.mark.parametrize("payload", [
    json.dumps({"origin": ORIGIN, "agent": None}).encode(),
    json.dumps({"origin": ORIGIN, "device": DEVICE, "agent": "connected"}).encode(),
])
def test_an_agent_key_does_not_let_a_report_without_origin_or_device_or_a_bad_agent_through(payload):
    key, clock, relay = build()
    gate, a = Endpoint(), Endpoint()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), gate)
        await relay.open_viewer(ticket(key, clock, "viewer"), a)
        await relay.from_tunnel(tunnel, encode_frame(rz.OP_REPORT, "", payload))
        await relay.flush()
        return relay.report(SESSION)

    assert run(go()) is None
    assert _reports(a) == []


def test_a_report_ends_with_its_session():
    key, clock, relay = build()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), Endpoint())
        await relay.from_tunnel(tunnel, _report(origin=ORIGIN, device=DEVICE))
        await relay.close_session(SESSION)
        return relay.report(SESSION)

    assert run(go()) is None


# ---------------------------------------------------------------- device requests (LB-DEV-B)


def device(key: bytes, clock: Clock, **overrides) -> str:
    fields = dict(session_id=SESSION, tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT,
                  generation=GEN, preset="iphone-15", orientation="landscape", touch=None,
                  now=clock())
    fields.update(overrides)
    return rz.mint_device(key, **fields)


def _devices(endpoint: Endpoint) -> list[tuple[str, dict]]:
    return [(c, json.loads(p)) for op, c, p in endpoint.frames() if op == rz.OP_DEVICE]


def test_a_device_request_reaches_the_sessions_gate_as_one_device_frame():
    """swarm-api checked the controller and MAC'd the choice; the relay checks
    the MAC, the clock, single use and the session, and hands the device --
    and nothing else -- to the gate."""
    key, clock, relay = build()
    gate, other_gate = Endpoint(), Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        await relay.open_tunnel(ticket(key, clock, session_id="ls_other", task_id="task_b"), other_gate)
        assert await relay.device_request(device(key, clock)) == SESSION
        await relay.flush()

    run(go())
    assert _devices(gate) == [("", {"preset": "iphone-15", "orientation": "landscape", "touch": None})]
    assert _devices(other_gate) == []


@pytest.mark.parametrize("overrides, reason", [
    ({"tenant_id": "research"}, "wrong_tenant"),
    ({"task_id": "task_other"}, "wrong_task"),
    ({"generation": GEN - 1}, "wrong_generation"),
    ({"session_id": "ls_none"}, "no_tunnel"),
])
def test_a_device_request_for_another_session_or_generation_is_refused(overrides, reason):
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        with pytest.raises(Refused) as e:
            await relay.device_request(device(key, clock, **overrides))
        await relay.flush()
        return e.value.reason

    assert run(go()) == reason
    assert _devices(gate) == []


def test_a_device_request_must_be_swarm_apis_fresh_and_used_once():
    key, clock, relay = build()
    gate = Endpoint()

    async def go():
        await relay.open_tunnel(ticket(key, clock), gate)
        reasons = []
        for raw in (device(secrets.token_bytes(32), clock),
                    device(key, clock, now=clock() - 60),
                    device(key, clock, ttl=600),
                    ticket(key, clock, "control"),
                    mint_close(key, SESSION, now=clock())):
            with pytest.raises(Refused) as e:
                await relay.device_request(raw)
            reasons.append(e.value.reason)
        good = device(key, clock)
        await relay.device_request(good)
        with pytest.raises(Refused) as e:
            await relay.device_request(good)
        reasons.append(e.value.reason)
        await relay.close_session(SESSION)
        with pytest.raises(Refused) as e:
            await relay.device_request(device(key, clock))
        reasons.append(e.value.reason)
        return reasons

    assert run(go()) == ["bad_signature", "expired", "lifetime", "bad_signature", "bad_signature",
                         "replayed", "session_closed"]


@pytest.mark.parametrize("field, value", [
    ("preset", ""), ("preset", "p" * 64), ("preset", 393), ("orientation", "upside-down"),
    ("touch", 1), ("touch", "yes"), ("generation", True),
])
def test_a_device_request_carries_a_short_name_never_a_size(field, value):
    key, clock, _ = build()
    body = {"session_id": SESSION, "tenant_id": TENANT, "task_id": TASK, "attempt_id": ATTEMPT,
            "generation": GEN, "preset": "desktop", "orientation": None, "touch": None,
            "exp": int(clock()) + 20, "nonce": secrets.token_hex(16), field: value}
    with pytest.raises(Refused) as e:
        rz.verify_device(key, rz._signed(key, body, rz.DEVICE_DOMAIN), now=clock())
    assert e.value.reason == "malformed"
    extra = {**body, field: "desktop" if field == "preset" else None, "width": 1920}
    with pytest.raises(Refused):
        rz.verify_device(key, rz._signed(key, extra, rz.DEVICE_DOMAIN), now=clock())


def test_post_internal_device_sends_the_frame_and_refuses_a_forgery_alike():
    from fastapi.testclient import TestClient

    from swarm_live.main import create_app

    key, clock, relay = build()
    with TestClient(create_app(relay)) as client:
        with client.websocket_connect("/v1/tunnel") as gate:
            gate.send_text(ticket(key, clock))
            assert json.loads(gate.receive_text()) == {"type": "ready"}
            forged = client.post("/internal/device", content=device(secrets.token_bytes(32), clock))
            assert forged.status_code == 404 and forged.json() == {"sent": False}
            none = client.post("/internal/device", content=device(key, clock, session_id="ls_none"))
            assert none.status_code == 404 and none.json() == {"sent": False}
            sent = client.post("/internal/device", content=device(key, clock, orientation=None, touch=False))
            assert sent.status_code == 200, sent.text
            assert sent.json() == {"sent": True, "session_id": SESSION}
            op, conn, payload = decode_frame(gate.receive_bytes())
            assert (op, conn, json.loads(payload)) == (
                rz.OP_DEVICE, "", {"preset": "iphone-15", "orientation": None, "touch": False})


# ---------------------------------------------------------------- frames


def test_tunnel_frames_round_trip_and_refuse_junk():
    for op, conn, payload in [(OP_DATA, "c1", b"\x00\xff"), (OP_OPEN, "rec-1", b""),
                              (OP_CONTROL, "", b""), (OP_ACTION, "", b"{}"), (rz.OP_REPORT, "", b"{}"),
                              (rz.OP_DEVICE, "", b"{}")]:
        assert decode_frame(encode_frame(op, conn, payload)) == (op, conn, payload)
    for junk in (b"", b"\x02", b"\x09\x00", b"\x02\x05ab"):
        with pytest.raises(ValueError):
            decode_frame(junk)
    with pytest.raises(ValueError):
        encode_frame(OP_DATA, "x" * 300)


# ---------------------------------------------------------------- recordings: names


def test_segment_names_sit_under_the_attempts_live_key_with_the_two_suffixes():
    assert rec.segment_name(TENANT, TASK, ATTEMPT, "rec", 7) == (
        f"tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/live/rec-0000000000007.live.webm")
    assert rec.segment_name(TENANT, TASK, ATTEMPT, "actions", 7) == (
        f"tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/live/actions-0000000000007.live.jsonl")
    for name in (rec.segment_name(TENANT, TASK, ATTEMPT, k, 1) for k in ("rec", "actions")):
        assert rec.is_live_name(name)
        assert "/live/" in name and name.endswith((".live.webm", ".live.jsonl"))


@pytest.mark.parametrize("tenant,task,attempt", [
    ("..", TASK, ATTEMPT), (TENANT, "a/b", ATTEMPT), (TENANT, TASK, ""),
    (TENANT, TASK, "x\n"), (".hidden", TASK, ATTEMPT), (TENANT, TASK, "live/../x"),
])
def test_a_path_component_that_could_leave_the_prefix_is_refused(tenant, task, attempt):
    with pytest.raises(ValueError):
        rec.segment_name(tenant, task, attempt, "rec", 1)


def test_an_unknown_segment_kind_is_refused():
    with pytest.raises(ValueError):
        rec.segment_name(TENANT, TASK, ATTEMPT, "checkpoint", 1)


@pytest.mark.parametrize("name", [
    f"tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/artifacts/rec-0000000000001.live.webm",
    f"tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/live/rec-0000000000001.webm",
    f"tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/live/rec-0000000000001.live.jsonl",
    f"tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/live/actions-0000000000001.live.webm",
    f"tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/live/sub/rec-0000000000001.live.webm",
    f"tenants/{TENANT}/tasks/{TASK}/attempts/{ATTEMPT}/checkpoints/live/rec-0000000000001.live.webm",
    "live/rec-0000000000001.live.webm",
])
def test_the_gcs_writer_refuses_every_name_outside_live_and_the_two_suffixes(name):
    class Bucket:
        def blob(self, name):  # pragma: no cover - must not be reached
            raise AssertionError("refused names never reach GCS")

    assert not rec.is_live_name(name)
    with pytest.raises(ValueError):
        rec.GcsSegmentWriter(Bucket()).write(name, b"x", "video/webm")


def test_the_gcs_writer_creates_and_never_overwrites():
    calls = []

    class Blob:
        def __init__(self, name):
            self.name = name

        def upload_from_string(self, data, **kwargs):
            calls.append((self.name, data, kwargs))

    class Bucket:
        def blob(self, name):
            return Blob(name)

    name = rec.segment_name(TENANT, TASK, ATTEMPT, "rec", 1)
    rec.GcsSegmentWriter(Bucket()).write(name, b"webm", "video/webm")
    assert calls == [(name, b"webm", {"content_type": "video/webm", "if_generation_match": 0})]


# ---------------------------------------------------------------- recordings: the recorder


class FakeGate:
    """The gate's side of the recorder's RFB 3.8 stream, enough to drive it.

    Speaks the server half of the handshake the L3 gate speaks
    (`livegate/rfb.py::RfbSession`) and sends updates in Raw or Zlib.
    """

    def __init__(self, width: int = 4, height: int = 2) -> None:
        self.width, self.height = width, height
        self.from_client = b""
        self.zlib = zlib.compressobj()

    def hello(self) -> bytes:
        return b"RFB 003.008\n"

    def after_version(self) -> bytes:
        return bytes([1, 1])

    def after_security(self) -> bytes:
        name = b"swarm live browser"
        fmt = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        return struct.pack(">I", 0) + struct.pack(">HH", self.width, self.height) + fmt + \
            struct.pack(">I", len(name)) + name

    def update(self, pixel: tuple[int, int, int], encoding: int = 0) -> bytes:
        r, g, b = pixel
        band = bytes([r, g, b, 0]) * (self.width * self.height)
        header = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.width, self.height, encoding)
        if encoding == 6:
            data = self.zlib.compress(band) + self.zlib.flush(zlib.Z_SYNC_FLUSH)
            return header + struct.pack(">I", len(data)) + data
        return header + band

    def resize(self, width: int, height: int) -> bytes:
        self.width, self.height = width, height
        return struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, width, height, -223)


def make_recorder(clock: Clock, sent: list[bytes], **kwargs):
    writer, encoder = Writer(), Encoder()

    async def send(data: bytes) -> None:
        sent.append(data)

    r = rec.Recorder(tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT, send=send,
                     writer=writer, encoder=encoder, clock=clock, **kwargs)
    return r, writer, encoder


def test_the_recorder_handshakes_as_an_rfb_client_and_asks_for_32bit_rgbx():
    clock, sent = Clock(), []
    r, _, _ = make_recorder(clock, sent)
    gate = FakeGate()

    async def go():
        await r.feed(gate.hello())
        await r.feed(gate.after_version())
        await r.feed(gate.after_security())

    run(go())
    out = b"".join(sent)
    assert out.startswith(b"RFB 003.008\n" + bytes([1]) + bytes([1]))
    rest = out[14:]
    # SetPixelFormat: 32 bpp, little-endian, red in the lowest byte.
    assert rest[:20] == bytes([0, 0, 0, 0]) + struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
    # SetEncodings offers Zlib, Raw and DesktopSize; then a full update request.
    assert rest[20:24] == struct.pack(">BxH", 2, 3)
    assert struct.unpack(">3i", rest[24:36]) == (6, 0, -223)
    assert rest[36:] == struct.pack(">BBHHHH", 3, 0, 0, 0, gate.width, gate.height)
    # Handshaken, but nothing to record until the first update arrives.
    assert not r.ready
    run(r.feed(gate.update((1, 2, 3))))
    assert r.ready


def _ready(r, gate):
    async def go():
        for chunk in (gate.hello(), gate.after_version(), gate.after_security()):
            await r.feed(chunk)
    run(go())


def test_the_recorder_writes_one_minute_segments_at_1fps_with_nobody_watching():
    clock, sent = Clock(), []
    r, writer, encoder = make_recorder(clock, sent)
    gate = FakeGate()
    _ready(r, gate)

    async def go():
        await r.feed(gate.update((10, 20, 30)))
        for _ in range(61):
            await r.tick()
            clock.now += 1
        await r.action({"kind": "cdp", "method": "Page.navigate", "url": "https://a.test/x",
                        "params": {"text": "never"}})
        await r.close()

    run(go())
    assert r.fps == rec.IDLE_FPS == 1
    names = sorted(writer.objects)
    assert all(rec.is_live_name(n) for n in names)
    webms = [n for n in names if n.endswith(".live.webm")]
    assert len(webms) == 2, names
    assert encoder.streams[0].fps == 1 and len(encoder.streams[0].frames) == 60
    assert encoder.streams[0].frames[0] == bytes([10, 20, 30]) * 8
    assert writer.objects[webms[0]][1] == "video/webm"
    jsonl = [n for n in names if n.endswith(".live.jsonl")]
    assert len(jsonl) == 1
    lines = [json.loads(x) for x in writer.objects[jsonl[0]][0].decode().splitlines()]
    assert lines[0]["method"] == "Page.navigate" and "params" not in lines[0]
    assert writer.objects[jsonl[0]][1] == "application/x-ndjson"


def test_the_recorder_records_at_10fps_while_someone_watches_and_rotates_on_the_change():
    clock, sent = Clock(), []
    r, writer, encoder = make_recorder(clock, sent)
    gate = FakeGate()
    _ready(r, gate)

    async def go():
        await r.feed(gate.update((1, 2, 3), encoding=6))
        await r.tick()
        r.set_viewers(2)
        assert r.fps == rec.WATCHED_FPS == 10
        for _ in range(5):
            clock.now += 0.1
            await r.tick()
        await r.feed(gate.update((4, 5, 6), encoding=6))
        await r.tick()
        await r.close()

    run(go())
    assert [s.fps for s in encoder.streams] == [1, 10]
    assert encoder.streams[1].frames[-1] == bytes([4, 5, 6]) * 8
    assert len([n for n in writer.objects if n.endswith(".live.webm")]) == 2


def test_the_recorder_starts_a_new_segment_when_the_display_is_resized():
    clock, sent = Clock(), []
    r, writer, encoder = make_recorder(clock, sent)
    gate = FakeGate(4, 2)
    _ready(r, gate)

    async def go():
        await r.feed(gate.update((1, 1, 1)))
        await r.tick()
        sent.clear()
        await r.feed(gate.resize(2, 3))
        # A resize is answered with a full update request at the new size.
        assert b"".join(sent).endswith(struct.pack(">BBHHHH", 3, 0, 0, 0, 2, 3))
        await r.feed(gate.update((2, 2, 2)))
        clock.now += 1
        await r.tick()
        await r.close()

    run(go())
    assert [(s.width, s.height) for s in encoder.streams] == [(4, 2), (2, 3)]


def test_the_recorder_refuses_a_server_cut_text_and_a_colour_map():
    clock = Clock()
    for message in (struct.pack(">BxxxI", 3, 4) + b"leak", struct.pack(">BxHH", 1, 0, 0)):
        r, writer, _ = make_recorder(clock, [])
        _ready(r, FakeGate())
        with pytest.raises(rec.RecordingError):
            run(r.feed(message))


def test_an_action_line_keeps_only_the_named_fields_and_bounds_their_size():
    clock, sent = Clock(), []
    r, writer, _ = make_recorder(clock, sent)

    async def go():
        await r.action({"kind": "cdp", "method": "Input.insertText", "length": 12, "text": "never"})
        await r.action({"kind": "cdp", "method": "x" * 10_000})
        await r.action(["not", "an", "object"])
        await r.close()

    run(go())
    (name,) = writer.objects
    lines = [json.loads(x) for x in writer.objects[name][0].decode().splitlines()]
    assert lines[0] == {"t": clock(), "kind": "cdp", "method": "Input.insertText", "length": 12}
    assert len(lines) == 1


# ---------------------------------------------------------------- relay + recorder


def test_the_relay_opens_a_recording_connection_per_tunnel_that_is_not_a_viewer():
    clock = Clock()
    key = secrets.token_bytes(32)
    writer, encoder = Writer(), Encoder()
    relay = Rendezvous(key, clock=clock,
                       recorder_factory=lambda s, send: rec.Recorder(
                           tenant_id=s.tenant_id, task_id=s.task_id, attempt_id=s.attempt_id,
                           send=send, writer=writer, encoder=encoder, clock=clock))
    gate_end, fake = Endpoint(), FakeGate()

    async def go():
        tunnel = await relay.open_tunnel(ticket(key, clock), gate_end)
        await relay.flush()
        (op, conn, _), = gate_end.frames()
        assert op == OP_OPEN and conn.startswith("rec-")
        for chunk in (fake.hello(), fake.after_version(), fake.after_security(), fake.update((9, 9, 9))):
            await relay.from_tunnel(tunnel, encode_frame(OP_DATA, conn, chunk))
        await relay.from_tunnel(tunnel, encode_frame(OP_ACTION, "", json.dumps(
            {"kind": "cdp", "method": "Page.navigate", "url": "https://a.test/"}).encode()))
        recorder = relay.recorder(SESSION)
        await recorder.tick()
        await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        assert recorder.fps == rec.WATCHED_FPS
        assert relay.viewer_count(task_id=TASK) == 1
        await relay.close_tunnel(tunnel)

    run(go())
    assert writer.objects and all(rec.is_live_name(n) for n in writer.objects)
    assert {n.rsplit(".", 2)[-2] + "." + n.rsplit(".", 1)[-1] for n in writer.objects} == {
        "live.webm", "live.jsonl"}


# ---------------------------------------------------------------- the HTTP surface


def test_the_websocket_routes_refuse_a_bad_ticket_and_splice_a_good_one():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from swarm_live.main import create_app

    key, clock, relay = build()
    # `with`: one event loop for every socket, as in the relay process. Without
    # it the test client gives each websocket its own loop thread.
    with TestClient(create_app(relay)) as client:
        assert client.get("/healthz").json() == {"ok": True, "tunnels": 0, "viewers": 0}
        with client.websocket_connect("/v1/tunnel") as ws:
            ws.send_text("not-a-ticket")
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_bytes()
            assert e.value.code == CLOSE_REFUSED
        with client.websocket_connect("/v1/tunnel") as gate:
            gate.send_text(ticket(key, clock))
            assert json.loads(gate.receive_text()) == {"type": "ready"}
            with client.websocket_connect("/v1/view") as viewer:
                viewer.send_text(ticket(key, clock, "viewer"))
                assert json.loads(viewer.receive_text()) == {"type": "ready"}
                op, conn, _ = decode_frame(gate.receive_bytes())
                assert op == OP_OPEN
                viewer.send_bytes(b"hello")
                assert decode_frame(gate.receive_bytes()) == (OP_DATA, conn, b"hello")
                gate.send_bytes(encode_frame(OP_DATA, conn, b"frame"))
                assert viewer.receive_bytes() == b"frame"


def test_post_internal_close_drops_a_connected_viewer():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from swarm_live.main import create_app

    key, clock, relay = build()
    with TestClient(create_app(relay)) as client:
        with client.websocket_connect("/v1/tunnel") as gate:
            gate.send_text(ticket(key, clock))
            assert json.loads(gate.receive_text()) == {"type": "ready"}
            with client.websocket_connect("/v1/view") as viewer:
                viewer.send_text(ticket(key, clock, "viewer"))
                assert json.loads(viewer.receive_text()) == {"type": "ready"}
                # Not swarm-api's: refused, alike, and nothing is dropped.
                forged = client.post("/internal/close",
                                     content=mint_close(secrets.token_bytes(32), SESSION, now=clock()))
                assert forged.status_code == 404 and forged.json() == {"closed": False}
                assert relay.viewer_count() == 1
                closed = client.post("/internal/close", content=mint_close(key, SESSION, now=clock()))
                assert closed.status_code == 200, closed.text
                assert closed.json() == {"closed": True, "session_id": SESSION, "dropped": 2}
                with pytest.raises(WebSocketDisconnect) as e:
                    viewer.receive_bytes()
                assert e.value.code == CLOSE_ENDED
        assert client.get("/healthz").json() == {"ok": True, "tunnels": 0, "viewers": 0}


# ---------------------------------------------------------------- start-up (decisions (b), (c))


def test_the_relay_starts_and_serves_with_no_firestore_client_and_no_certificate(monkeypatch):
    from fastapi.testclient import TestClient

    from swarm_live import main as relay_main

    key = secrets.token_hex(24).encode()
    asked: list[str] = []

    def access(version: str) -> bytes:
        asked.append(version)
        return key + b"\n"

    version = "projects/p-test/secrets/swarm-live-key/versions/latest"
    # Any import of the Firestore client now fails, so building the relay
    # proves it needs none.
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", None)
    monkeypatch.setattr(relay_main, "_access_secret", access)
    monkeypatch.setenv("SWARM_LIVE_KEY_SECRET", version)
    monkeypatch.setenv("SWARM_LIVE_RECORD", "false")
    for gone in ("SWARM_LIVE_KEY_FILE", "SWARM_LIVE_TLS_CERT", "SWARM_LIVE_TLS_KEY"):
        monkeypatch.delenv(gone, raising=False)

    with TestClient(relay_main.create_app()) as client:
        assert client.get("/healthz").json() == {"ok": True, "tunnels": 0, "viewers": 0}
        with client.websocket_connect("/v1/tunnel") as gate:
            gate.send_text(mint_ticket(key, session_id=SESSION, tenant_id=TENANT, task_id=TASK,
                                       attempt_id=ATTEMPT, generation=GEN, role="tunnel"))
            assert json.loads(gate.receive_text()) == {"type": "ready"}
    assert asked == [version]

    served: dict = {}
    monkeypatch.setitem(sys.modules, "uvicorn", type(sys)("uvicorn"))
    sys.modules["uvicorn"].run = lambda *a, **kw: served.update(kw)
    relay_main.main()
    assert served["port"] == relay_main.PORT == 8080
    assert not [k for k in served if k.startswith("ssl")]


@pytest.mark.parametrize("version", [
    None, "", "swarm-live-key", "projects/p/secrets/swarm-live-key",
    "/var/run/secrets/swarm-live-key/key",
])
def test_the_key_is_read_only_by_a_secret_manager_version_reference(version):
    from swarm_live.main import read_key

    def access(_: str) -> bytes:
        raise AssertionError("nothing is fetched for a reference that is not a version")

    with pytest.raises(RuntimeError):
        read_key(version, access)


def test_a_short_key_from_secret_manager_is_refused():
    from swarm_live.main import read_key

    with pytest.raises(RuntimeError, match="shorter"):
        read_key("projects/p/secrets/swarm-live-key/versions/3", lambda _: b"k" * 8)
    assert read_key("projects/p/secrets/swarm-live-key/versions/3", lambda _: b"k" * 32 + b"\n") == b"k" * 32
