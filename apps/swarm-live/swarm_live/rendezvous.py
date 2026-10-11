"""Where the gate's tunnel and the viewers' streams meet (LB-L7).

docs/design/live-browser.md §3.3 (the relay, and why it is one process), §5.2
(every connection checked), §5.10 (viewer caps) and §5.11 (replay, a stale
generation). The relay is NOT trusted with authorisation: swarm-api decides who
may watch and who may control, and says so with a ticket. The relay's job is to
check that each ticket is genuine, unspent, unexpired and names exactly the
session it is used on -- then to splice bytes.

TICKETS. `<base64url(json)>.<base64url(hmac-sha256)>`, the MAC under
`swarm-live-key` (held by swarm-api and swarm-live only) over a domain tag and
the encoded body, so a MAC for any other purpose is never a ticket. The body
names `session_id, tenant_id, task_id, attempt_id, generation, role, exp` (§3.3)
plus `email` for a person's role and a random `nonce` that makes each ticket
one-use. Roles:

* `tunnel`  -- the gate's tunnel, minted for the worker (60 s);
* `viewer`  -- one proxied console websocket, minted by swarm-api (30 s);
* `control` -- swarm-api granting control to that viewer, after its
  transaction on `live_sessions/{id}` (§4.2) (30 s);
* `release` -- swarm-api ending that viewer's control (30 s).

A ticket is refused if its MAC is wrong, it has expired, it claims a lifetime
longer than its role's, it was presented before, its role is not the one the
route takes, or its tenant, task, attempt or generation differs from the
session document's. Every refusal of a ticket closes with the same code, so a
caller probing for another tenant's session learns nothing it would not learn
from a session that does not exist (§5.2 step 2: "a refusal is a 404").

THE TUNNEL'S FRAMES. After the ticket (the first message, text) and the
relay's `{"type": "ready"}`, every tunnel message is binary:
`op (1 byte) | len(conn_id) (1 byte) | conn_id (ascii) | payload`.

* `OPEN`    relay -> gate: a new RFB connection with this id (a viewer, or the
            recorder).
* `DATA`    both ways: RFB bytes of that connection.
* `CLOSE`   both ways: that connection ended.
* `CONTROL` relay -> gate: the connection that now holds control, or an empty
            id for none. The gate's `ControlState.grant`/`revoke` act on it.
* `ACTION`  gate -> relay: one action-log line (JSON) for the recording (§7).

A viewer's websocket carries that viewer's RFB bytes as binary messages, and
swarm-api's control messages as text: `{"type": "control"|"release",
"ticket": ...}`.

NOTHING READ FROM A CONNECTION IS LOGGED. RFB bytes are a person's keystrokes
(§5.3.3), and a ticket admits whoever holds it. Log lines carry reason codes,
session ids, connection ids and counts.

THE CAPS (§5.10). 10 concurrent viewers per task, 50 per tenant. The relay is a
single replica (§3.3), so these counts are global. The recorder's connection is
not a viewer and is not counted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)

TICKET_DOMAIN = b"swarm-live-ticket/v1\0"

#: Lifetime of each role's ticket (§3.3: 60 s for the tunnel; §5.2: 30 s for a
#: viewer). Control tickets are minted by swarm-api the moment its transaction
#: commits and presented at once, so they get the viewer's 30 s.
TICKET_TTL_SECONDS = {"tunnel": 60, "viewer": 30, "control": 30, "release": 30}

#: Clock skew tolerated between swarm-api's clock and the relay's, on expiry and
#: on the lifetime check. Both run on Google infrastructure with NTP.
CLOCK_SKEW_SECONDS = 5

#: §5.10. The relay is one replica, so these are global counts.
MAX_VIEWERS_PER_TASK = 10
MAX_VIEWERS_PER_TENANT = 50

#: Outbound messages buffered per connection before it is treated as too slow
#: and closed. A viewer that cannot keep up must not stall the tunnel every
#: other viewer and the recorder share; noVNC reconnects on its own.
DEFAULT_QUEUE_SIZE = 256

MIN_KEY_BYTES = 32
MAX_TICKET_CHARS = 4096
MAX_CONN_ID = 64

OP_OPEN, OP_DATA, OP_CLOSE, OP_CONTROL, OP_ACTION = 1, 2, 3, 4, 5
_OPS = frozenset({OP_OPEN, OP_DATA, OP_CLOSE, OP_CONTROL, OP_ACTION})

#: Websocket close codes. Every ticket refusal is CLOSE_REFUSED ("404").
CLOSE_REFUSED = 4404
CLOSE_BUSY = 4409
CLOSE_ENDED = 4410
CLOSE_FULL = 4429
CLOSE_SLOW = 4408

_TICKET_FIELDS = ("session_id", "tenant_id", "task_id", "attempt_id", "generation",
                  "role", "exp", "nonce", "email")


class Refused(Exception):
    """A connection or ticket the relay will not accept. `reason` is a code, never content."""

    def __init__(self, reason: str, code: int = CLOSE_REFUSED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


# ---------------------------------------------------------------- tickets


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, body: str) -> bytes:
    return hmac.new(key, TICKET_DOMAIN + body.encode("ascii"), hashlib.sha256).digest()


@dataclass(frozen=True)
class Ticket:
    session_id: str
    tenant_id: str
    task_id: str
    attempt_id: str
    generation: int
    role: str
    exp: int
    nonce: str
    email: str = ""


def mint_ticket(key: bytes, *, session_id: str, tenant_id: str, task_id: str, attempt_id: str,
                generation: int, role: str, email: str = "", now: float | None = None,
                ttl: int | None = None) -> str:
    """A ticket as swarm-api mints it. The relay never mints one in production;
    this is the single statement of the format, for swarm-api to use (see the
    PR's contract request) and for the tests."""
    if role not in TICKET_TTL_SECONDS:
        raise ValueError(f"unknown ticket role {role!r}")
    issued = int(time.time() if now is None else now)
    body = {
        "session_id": session_id, "tenant_id": tenant_id, "task_id": task_id,
        "attempt_id": attempt_id, "generation": int(generation), "role": role,
        "exp": issued + (TICKET_TTL_SECONDS[role] if ttl is None else int(ttl)),
        "nonce": secrets.token_hex(16), "email": email,
    }
    encoded = _b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return encoded + "." + _b64encode(_mac(key, encoded))


def verify_ticket(key: bytes, raw: str, *, now: float) -> Ticket:
    """Check the MAC, the shape and the clock. One-use is the ledger's job."""
    if not isinstance(raw, str) or len(raw) > MAX_TICKET_CHARS or raw.count(".") != 1:
        raise Refused("malformed")
    encoded, mac = raw.split(".")
    try:
        given = _b64decode(mac)
    except (binascii.Error, ValueError):
        raise Refused("malformed") from None
    if not encoded or not hmac.compare_digest(given, _mac(key, encoded)):
        raise Refused("bad_signature")
    try:
        body = json.loads(_b64decode(encoded))
    except (binascii.Error, ValueError):
        raise Refused("malformed") from None
    if not isinstance(body, dict) or set(body) != set(_TICKET_FIELDS):
        raise Refused("malformed")
    strings = [body[f] for f in _TICKET_FIELDS if f not in ("generation", "exp")]
    if (not all(isinstance(s, str) for s in strings)
            or not all(isinstance(body[f], int) and not isinstance(body[f], bool)
                       for f in ("generation", "exp"))):
        raise Refused("malformed")
    ticket = Ticket(**body)
    if ticket.role not in TICKET_TTL_SECONDS or not ticket.nonce:
        raise Refused("malformed")
    if (ticket.role == "tunnel") != (ticket.email == ""):
        # A person's ticket names the person (§5.2 step 3); the gate's names nobody.
        raise Refused("malformed")
    if now > ticket.exp:
        raise Refused("expired")
    if ticket.exp - now > TICKET_TTL_SECONDS[ticket.role] + CLOCK_SKEW_SECONDS:
        raise Refused("lifetime")
    return ticket


class TicketLedger:
    """Nonces already presented, kept until their ticket could no longer verify."""

    def __init__(self) -> None:
        self._spent: dict[str, int] = {}

    def spend(self, ticket: Ticket, now: float) -> None:
        if len(self._spent) > 1024:
            self._spent = {n: exp for n, exp in self._spent.items() if exp + CLOCK_SKEW_SECONDS >= now}
        if ticket.nonce in self._spent:
            raise Refused("replayed")
        self._spent[ticket.nonce] = ticket.exp


# ---------------------------------------------------------------- tunnel frames


def encode_frame(op: int, conn_id: str, payload: bytes = b"") -> bytes:
    raw = conn_id.encode("ascii")
    if op not in _OPS or len(raw) > MAX_CONN_ID:
        raise ValueError("bad tunnel frame")
    return bytes([op, len(raw)]) + raw + payload


def decode_frame(data: bytes) -> tuple[int, str, bytes]:
    if len(data) < 2 or data[0] not in _OPS or data[1] > MAX_CONN_ID or len(data) < 2 + data[1]:
        raise ValueError("bad tunnel frame")
    try:
        conn_id = data[2:2 + data[1]].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("bad tunnel frame") from None
    return data[0], conn_id, bytes(data[2 + data[1]:])


# ---------------------------------------------------------------- the session document


@dataclass(frozen=True)
class SessionRecord:
    """The fields of `live_sessions/{session_id}` the relay checks a ticket against."""

    session_id: str
    tenant_id: str
    task_id: str
    attempt_id: str
    generation: int
    open: bool


class SessionStore(Protocol):
    def get(self, session_id: str) -> SessionRecord | None: ...


class FirestoreSessionStore:
    """Reads `live_sessions/{id}`, which swarm-api writes (§3.3, §8.4).

    The relay reads this document and nothing else in Firestore. A session is
    open while swarm-api's `state` field says `"open"`; any other value, or a
    missing document, refuses every ticket for it.
    """

    COLLECTION = "live_sessions"

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, session_id: str) -> SessionRecord | None:
        snap = self._client.collection(self.COLLECTION).document(session_id).get()
        if not snap.exists:
            return None
        doc = snap.to_dict() or {}
        try:
            return SessionRecord(
                session_id=session_id, tenant_id=str(doc["tenant_id"]), task_id=str(doc["task_id"]),
                attempt_id=str(doc["attempt_id"]), generation=int(doc["generation"]),
                open=doc.get("state") == "open")
        except (KeyError, TypeError, ValueError):
            log.warning("live relay: session=%s document is malformed; treated as closed", session_id)
            return None


# ---------------------------------------------------------------- connections


class Endpoint(Protocol):
    """One websocket, as the relay writes to it."""

    async def send_bytes(self, data: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def close(self, code: int, reason: str) -> None: ...


class Recording(Protocol):
    conn_id: str
    async def feed(self, data: bytes) -> None: ...
    async def action(self, entry: Any) -> None: ...
    def set_viewers(self, count: int) -> None: ...
    async def run(self) -> None: ...
    async def close(self) -> None: ...


RecorderFactory = Callable[[SessionRecord, Callable[[bytes], Awaitable[None]]], Recording]


class _Outbox:
    """A bounded queue in front of an endpoint, so one slow socket stalls nobody else."""

    def __init__(self, endpoint: Endpoint, size: int, on_overflow: Callable[[], Awaitable[None]]) -> None:
        self.endpoint = endpoint
        self._queue: asyncio.Queue[tuple[bool, Any]] = asyncio.Queue(maxsize=size)
        self._on_overflow = on_overflow
        self._pump = asyncio.create_task(self._run())
        self._shut = asyncio.Event()
        self.closed = False

    def put(self, binary: bool, payload: Any) -> None:
        if self.closed:
            return
        try:
            self._queue.put_nowait((binary, payload))
        except asyncio.QueueFull:
            asyncio.get_running_loop().create_task(self._on_overflow())

    async def _run(self) -> None:
        while True:
            binary, payload = await self._queue.get()
            try:
                if binary:
                    await self.endpoint.send_bytes(payload)
                else:
                    await self.endpoint.send_text(payload)
            except Exception:  # noqa: BLE001 - a dead socket ends at its reader
                pass
            finally:
                self._queue.task_done()

    async def join(self) -> None:
        """Until the queue is written, or the connection is shut with it unwritten."""
        if self.closed:
            return
        joined = asyncio.create_task(self._queue.join())
        shut = asyncio.create_task(self._shut.wait())
        await asyncio.wait({joined, shut}, return_when=asyncio.FIRST_COMPLETED)
        for task in (joined, shut):
            task.cancel()

    async def shut(self, code: int, reason: str, *, drain: bool = False) -> None:
        if self.closed:
            return
        if drain:
            try:
                await asyncio.wait_for(self._queue.join(), 2)
            except asyncio.TimeoutError:
                pass
        self.closed = True
        self._shut.set()
        self._pump.cancel()
        await asyncio.gather(self._pump, return_exceptions=True)
        try:
            await self.endpoint.close(code, reason)
        except Exception:  # noqa: BLE001 - already gone
            pass


@dataclass(eq=False)
class Tunnel:
    session: SessionRecord
    outbox: _Outbox
    viewers: dict[str, "Viewer"] = field(default_factory=dict)
    controller: str | None = None
    recording: Recording | None = None
    recording_task: asyncio.Task[None] | None = None
    closed: bool = False


@dataclass(eq=False)
class Viewer:
    conn_id: str
    email: str
    tunnel: Tunnel
    outbox: _Outbox
    closed: bool = False


class Rendezvous:
    def __init__(self, key: bytes, store: SessionStore, *, clock: Callable[[], float] = time.time,
                 recorder_factory: RecorderFactory | None = None,
                 max_viewers_per_task: int = MAX_VIEWERS_PER_TASK,
                 max_viewers_per_tenant: int = MAX_VIEWERS_PER_TENANT,
                 queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"swarm-live-key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key
        self._store = store
        self._clock = clock
        self._recorder_factory = recorder_factory
        self._max_task = max_viewers_per_task
        self._max_tenant = max_viewers_per_tenant
        self._queue_size = queue_size
        self._ledger = TicketLedger()
        self._tunnels: dict[str, Tunnel] = {}
        self._lock = asyncio.Lock()

    # -- counts --------------------------------------------------------

    def tunnel_count(self) -> int:
        return len(self._tunnels)

    def viewer_count(self, *, task_id: str | None = None, tenant_id: str | None = None) -> int:
        return sum(len(t.viewers) for t in self._tunnels.values()
                   if (task_id is None or t.session.task_id == task_id)
                   and (tenant_id is None or t.session.tenant_id == tenant_id))

    def controller(self, session_id: str) -> str | None:
        tunnel = self._tunnels.get(session_id)
        return tunnel.controller if tunnel else None

    def recorder(self, session_id: str) -> Recording | None:
        tunnel = self._tunnels.get(session_id)
        return tunnel.recording if tunnel else None

    # -- tickets -------------------------------------------------------

    async def _admit(self, raw: str, role: str | tuple[str, ...]) -> tuple[Ticket, SessionRecord]:
        """Verify, spend, and match a ticket to its session document.

        The MAC, the clock and the spend run on the event loop, so two
        presentations of one ticket cannot both pass the ledger; only the
        document read goes to a thread.
        """
        now = self._clock()
        ticket = verify_ticket(self._key, raw, now=now)
        self._ledger.spend(ticket, now)
        roles = (role,) if isinstance(role, str) else role
        if ticket.role not in roles:
            raise Refused("wrong_role")
        record = await asyncio.to_thread(self._store.get, ticket.session_id)
        if record is None:
            raise Refused("unknown_session")
        if not record.open:
            raise Refused("session_closed")
        for name in ("tenant_id", "task_id", "attempt_id", "generation"):
            if getattr(ticket, name) != getattr(record, name):
                raise Refused("wrong_" + name.removesuffix("_id"))
        return ticket, record

    # -- the gate's tunnel ---------------------------------------------

    async def open_tunnel(self, raw: str, endpoint: Endpoint) -> Tunnel:
        ticket, record = await self._admit(raw, "tunnel")
        async with self._lock:
            if record.session_id in self._tunnels:
                raise Refused("tunnel_exists", CLOSE_BUSY)
            tunnel: Tunnel
            outbox = _Outbox(endpoint, self._queue_size,
                             lambda: self._end_tunnel(tunnel, CLOSE_SLOW, "too_slow"))
            tunnel = Tunnel(session=record, outbox=outbox)
            self._tunnels[record.session_id] = tunnel
        outbox.put(False, json.dumps({"type": "ready"}))
        if self._recorder_factory is not None:
            async def send(data: bytes, _t: Tunnel = tunnel) -> None:
                if _t.recording is not None:
                    _t.outbox.put(True, encode_frame(OP_DATA, _t.recording.conn_id, data))

            tunnel.recording = self._recorder_factory(record, send)
            outbox.put(True, encode_frame(OP_OPEN, tunnel.recording.conn_id))
            tunnel.recording_task = asyncio.create_task(self._run_recording(tunnel))
        log.info("live relay: session=%s tenant=%s tunnel open", record.session_id, record.tenant_id)
        return tunnel

    async def _run_recording(self, tunnel: Tunnel) -> None:
        assert tunnel.recording is not None
        try:
            await tunnel.recording.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - recording must not end the session
            log.warning("live relay: session=%s recording stopped: %s",
                        tunnel.session.session_id, type(exc).__name__)

    async def from_tunnel(self, tunnel: Tunnel, data: bytes) -> None:
        if tunnel.closed:
            return
        try:
            op, conn_id, payload = decode_frame(data)
        except ValueError:
            log.info("live relay: session=%s dropped a malformed tunnel frame", tunnel.session.session_id)
            return
        rec = tunnel.recording
        if op == OP_ACTION:
            if rec is not None:
                try:
                    entry = json.loads(payload)
                except ValueError:
                    return
                await rec.action(entry)
            return
        if rec is not None and conn_id == rec.conn_id:
            if op == OP_DATA:
                try:
                    await rec.feed(payload)
                except Exception as exc:  # noqa: BLE001 - a bad stream ends the recording only
                    log.warning("live relay: session=%s recording stream refused: %s",
                                tunnel.session.session_id, type(exc).__name__)
                    tunnel.outbox.put(True, encode_frame(OP_CLOSE, rec.conn_id))
                    tunnel.recording = None
                    await self._stop_recording(tunnel, rec)
            return
        viewer = tunnel.viewers.get(conn_id)
        if viewer is None:
            return
        if op == OP_DATA:
            viewer.outbox.put(True, payload)
        elif op == OP_CLOSE:
            await self._drop_viewer(viewer, CLOSE_ENDED, "closed_by_gate", tell_gate=False)

    async def close_tunnel(self, tunnel: Tunnel) -> None:
        """The gate's socket ended: nothing is left to drain to it."""
        await self._end_tunnel(tunnel, CLOSE_ENDED, "tunnel_closed", drain=False)

    async def _end_tunnel(self, tunnel: Tunnel, code: int, reason: str, *, drain: bool = True) -> None:
        if tunnel.closed:
            return
        tunnel.closed = True
        if self._tunnels.get(tunnel.session.session_id) is tunnel:
            del self._tunnels[tunnel.session.session_id]
        for viewer in list(tunnel.viewers.values()):
            await self._drop_viewer(viewer, CLOSE_ENDED, reason, tell_gate=False)
        rec, tunnel.recording = tunnel.recording, None
        if rec is not None:
            await self._stop_recording(tunnel, rec)
        await tunnel.outbox.shut(code, reason, drain=drain and code != CLOSE_SLOW)
        log.info("live relay: session=%s tunnel closed: %s", tunnel.session.session_id, reason)

    async def _stop_recording(self, tunnel: Tunnel, rec: Recording) -> None:
        task, tunnel.recording_task = tunnel.recording_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await rec.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("live relay: session=%s last segment not written: %s",
                        tunnel.session.session_id, type(exc).__name__)

    # -- viewers -------------------------------------------------------

    async def open_viewer(self, raw: str, endpoint: Endpoint) -> Viewer:
        ticket, record = await self._admit(raw, "viewer")
        async with self._lock:
            tunnel = self._tunnels.get(record.session_id)
            if tunnel is None or tunnel.closed:
                raise Refused("no_tunnel")
            if tunnel.session.generation != record.generation:
                raise Refused("wrong_generation")
            if self.viewer_count(task_id=record.task_id) >= self._max_task:
                raise Refused("viewers_per_task", CLOSE_FULL)
            if self.viewer_count(tenant_id=record.tenant_id) >= self._max_tenant:
                raise Refused("viewers_per_tenant", CLOSE_FULL)
            viewer: Viewer
            outbox = _Outbox(endpoint, self._queue_size,
                             lambda: self._drop_viewer(viewer, CLOSE_SLOW, "too_slow"))
            viewer = Viewer(conn_id="v-" + secrets.token_hex(8), email=ticket.email,
                            tunnel=tunnel, outbox=outbox)
            tunnel.viewers[viewer.conn_id] = viewer
        outbox.put(False, json.dumps({"type": "ready"}))
        tunnel.outbox.put(True, encode_frame(OP_OPEN, viewer.conn_id))
        self._viewers_changed(tunnel)
        log.info("live relay: session=%s conn=%s viewer joined (%d on task)",
                 record.session_id, viewer.conn_id, len(tunnel.viewers))
        return viewer

    def _viewers_changed(self, tunnel: Tunnel) -> None:
        if tunnel.recording is not None:
            tunnel.recording.set_viewers(len(tunnel.viewers))

    async def from_viewer(self, viewer: Viewer, data: bytes) -> None:
        if viewer.closed or viewer.tunnel.closed:
            return
        viewer.tunnel.outbox.put(True, encode_frame(OP_DATA, viewer.conn_id, data))

    async def viewer_text(self, viewer: Viewer, text: str) -> None:
        """swarm-api's control messages, each carrying a ticket it minted."""
        if viewer.closed:
            return
        try:
            message = json.loads(text)
        except ValueError:
            message = None
        kind = message.get("type") if isinstance(message, dict) else None
        raw = message.get("ticket") if isinstance(message, dict) else None
        if kind != "control" or not isinstance(raw, str):
            viewer.outbox.put(False, json.dumps({"type": "refused", "reason": "malformed"}))
            return
        try:
            ticket, record = await self._admit(raw, ("control", "release"))
            if record.session_id != viewer.tunnel.session.session_id:
                raise Refused("wrong_session")
            if ticket.email != viewer.email:
                raise Refused("wrong_person")
        except Refused as exc:
            log.info("live relay: session=%s conn=%s control refused: %s",
                     viewer.tunnel.session.session_id, viewer.conn_id, exc.reason)
            viewer.outbox.put(False, json.dumps({"type": "refused", "reason": "control"}))
            return
        tunnel = viewer.tunnel
        if ticket.role == "control":
            tunnel.controller = viewer.conn_id
            tunnel.outbox.put(True, encode_frame(OP_CONTROL, viewer.conn_id))
            event = "control_taken"
        else:
            if tunnel.controller != viewer.conn_id:
                return
            tunnel.controller = None
            tunnel.outbox.put(True, encode_frame(OP_CONTROL, ""))
            event = "control_returned"
        log.info("live relay: session=%s conn=%s %s", tunnel.session.session_id, viewer.conn_id, event)
        if tunnel.recording is not None:
            await tunnel.recording.action({"kind": "control", "event": event})

    async def close_viewer(self, viewer: Viewer) -> None:
        await self._drop_viewer(viewer, CLOSE_ENDED, "viewer_closed")

    async def _drop_viewer(self, viewer: Viewer, code: int, reason: str, *, tell_gate: bool = True) -> None:
        if viewer.closed:
            return
        viewer.closed = True
        tunnel = viewer.tunnel
        tunnel.viewers.pop(viewer.conn_id, None)
        if tunnel.controller == viewer.conn_id:
            # The gate revokes control when the connection closes
            # (`ControlState.connection_closed`); the relay forgets it too.
            tunnel.controller = None
        if tell_gate and not tunnel.closed:
            tunnel.outbox.put(True, encode_frame(OP_CLOSE, viewer.conn_id))
        self._viewers_changed(tunnel)
        await viewer.outbox.shut(code, reason)
        log.info("live relay: session=%s conn=%s viewer left: %s",
                 tunnel.session.session_id, viewer.conn_id, reason)

    # -- keeping the check true while connections are open --------------

    async def sweep(self) -> None:
        """Drop every session whose document closed or whose generation moved
        (invariant 5: a stale generation's session is closed, §8.5)."""
        for tunnel in list(self._tunnels.values()):
            s = tunnel.session
            try:
                current = await asyncio.to_thread(self._store.get, s.session_id)
            except Exception as exc:  # noqa: BLE001 - keep serving; try again next sweep
                log.warning("live relay: session=%s sweep read failed: %s", s.session_id, type(exc).__name__)
                continue
            if (current is None or not current.open
                    or (current.tenant_id, current.task_id, current.attempt_id, current.generation)
                    != (s.tenant_id, s.task_id, s.attempt_id, s.generation)):
                await self._end_tunnel(tunnel, CLOSE_ENDED, "session_ended")

    async def flush(self) -> None:
        """Wait until every open connection's queue has been written. For tests
        and for an orderly shutdown."""
        for _ in range(3):
            for tunnel in list(self._tunnels.values()):
                await tunnel.outbox.join()
                for viewer in list(tunnel.viewers.values()):
                    await viewer.outbox.join()
            await asyncio.sleep(0)

    async def shutdown(self) -> None:
        for tunnel in list(self._tunnels.values()):
            await self._end_tunnel(tunnel, CLOSE_ENDED, "relay_stopping")
