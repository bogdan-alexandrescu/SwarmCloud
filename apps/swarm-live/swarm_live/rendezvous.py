"""Where the gate's tunnel and the viewers' streams meet (LB-L7, wired by LB-L8d).

docs/design/live-browser.md §3.3 (the relay, and why it is one process), §5.2
(every connection checked), §5.10 (viewer caps) and §5.11 (replay, a stale
generation). The relay is NOT trusted with authorisation: swarm-api decides who
may watch and who may control, and says so with a ticket. The relay's job is to
check that each ticket is genuine, unspent, unexpired and names exactly the
session it is used on -- then to splice bytes.

THE RELAY READS NOTHING BUT THE TICKET (owner decision (a), 2026-10-11, §3.3).
It has no Firestore client and never reads `live_sessions`: Firestore IAM cannot
name one document, so "only the session document" was not a grant anyone could
make, and the closest faithful one is none. What the relay knows of a session
is what its tunnel ticket said, under swarm-api's MAC. When swarm-api closes a
session, or sees its generation go stale, it says so on `POST /internal/close`
(`verify_close`), and the relay drops every connection of that session and
refuses any ticket for it that is still inside its lifetime.

TICKETS. swarm-api mints them (`swarm_api.live.mint_ticket`); this module
restates the format, and tests/unit/live/test_relay_wiring.py holds the two to
each other by verifying here what swarm-api minted:

    v1.<b64url(payload)>.<b64url(HMAC-SHA256(key, "swarm-live-ticket/v1\\n" + b64url(payload)))>

`payload` is JSON of `session_id, tenant_id, task_id, attempt_id, generation,
role, exp, nonce` and, for a viewer, `email`. The key is the bytes of
`swarm-live-key`'s value. Roles:

* `tunnel` -- the gate's tunnel, minted for the worker;
* `viewer` -- one console websocket, minted by swarm-api for one person and
  presented here by swarm-api's proxy (browsers never reach the relay,
  decision (b)).

A ticket is refused if its MAC is wrong, it has expired, its `exp` is more than
`MAX_TICKET_SECONDS` ahead, it was presented before, its role is not the one
the route takes, its session was closed, or -- for a viewer -- its tenant,
task, attempt or generation differs from what the session's tunnel ticket
said. Every refusal of a ticket closes with the same code, so a caller probing
for another tenant's session learns nothing it would not learn from a session
that does not exist (§5.2 step 2: "a refusal is a 404").

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
swarm-api's control messages as text: `{"type": "control", "controller":
true|false}`, sent when swarm-api's transaction on `live_sessions/{id}` gives
or takes this person's control (§4.2). Those carry no ticket of their own: the
socket they arrive on was opened with a one-use viewer ticket only swarm-api
presents, and only swarm-api's egress reaches `/v1/view` (§8.3).

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

#: The ticket MAC's domain and version, as swarm_api.live mints them.
TICKET_DOMAIN = b"swarm-live-ticket/v1\n"
TICKET_VERSION = "v1"
ROLES = ("tunnel", "viewer")

#: The longest a ticket may live (owner decision (a): `exp` at most 60 s
#: ahead). A ticket claiming more is refused, so a minting bug that issued
#: hour-long tickets cannot become replayable ones.
MAX_TICKET_SECONDS = 60

#: The close request's MAC domain: a MAC for a ticket is never a close request,
#: and the other way round. The signature travels in CLOSE_SIGNATURE_HEADER over
#: the request's raw body.
CLOSE_DOMAIN = b"swarm-live-close/v1\n"
CLOSE_SIGNATURE_HEADER = "x-swarm-live-signature"
MAX_CLOSE_BODY = 1024

#: Clock skew tolerated between swarm-api's clock and the relay's, on expiry and
#: on the lifetime check. Both run on Google infrastructure with NTP.
CLOCK_SKEW_SECONDS = 5

#: How long a closed session's id is remembered: until every ticket minted
#: before the close could no longer verify. swarm-api mints none after it.
CLOSED_MEMORY_SECONDS = MAX_TICKET_SECONDS + CLOCK_SKEW_SECONDS

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
                  "role", "exp", "nonce")
_CLOSE_FIELDS = ("session_id", "cause", "exp", "nonce")


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
    """A ticket in the format swarm-api mints. The relay never mints one in
    production; this is for the tests, which also hold it to
    `swarm_api.live.mint_ticket`."""
    if role not in ROLES:
        raise ValueError(f"unknown ticket role {role!r}")
    issued = int(time.time() if now is None else now)
    body: dict[str, Any] = {
        "session_id": session_id, "tenant_id": tenant_id, "task_id": task_id,
        "attempt_id": attempt_id, "generation": int(generation), "role": role,
        "exp": issued + (MAX_TICKET_SECONDS if ttl is None else int(ttl)),
        "nonce": secrets.token_urlsafe(16),
    }
    if email:
        body["email"] = email
    encoded = _b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return f"{TICKET_VERSION}.{encoded}.{_b64encode(_mac(key, encoded))}"


def verify_ticket(key: bytes, raw: str, *, now: float) -> Ticket:
    """Check the MAC, the shape and the clock. One-use is the ledger's job."""
    if not isinstance(raw, str) or len(raw) > MAX_TICKET_CHARS or raw.count(".") != 2:
        raise Refused("malformed")
    version, encoded, mac = raw.split(".")
    if version != TICKET_VERSION:
        raise Refused("malformed")
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
    if not isinstance(body, dict):
        raise Refused("malformed")
    # A person's ticket names the person (§5.2 step 3); the gate's names nobody.
    expected = set(_TICKET_FIELDS) | ({"email"} if body.get("role") == "viewer" else set())
    if set(body) != expected:
        raise Refused("malformed")
    strings = [body[f] for f in expected if f not in ("generation", "exp")]
    if (not all(isinstance(s, str) and s for s in strings)
            or not all(isinstance(body[f], int) and not isinstance(body[f], bool)
                       for f in ("generation", "exp"))):
        raise Refused("malformed")
    ticket = Ticket(**body)
    if ticket.role not in ROLES:
        raise Refused("malformed")
    if now > ticket.exp:
        raise Refused("expired")
    if ticket.exp - now > MAX_TICKET_SECONDS + CLOCK_SKEW_SECONDS:
        raise Refused("lifetime")
    return ticket


class TicketLedger:
    """Nonces already presented, kept until their ticket could no longer verify."""

    def __init__(self) -> None:
        self._spent: dict[str, int] = {}

    def spend(self, nonce: str, exp: int, now: float) -> None:
        if len(self._spent) > 1024:
            self._spent = {n: e for n, e in self._spent.items() if e + CLOCK_SKEW_SECONDS >= now}
        if nonce in self._spent:
            raise Refused("replayed")
        self._spent[nonce] = exp


# ---------------------------------------------------------------- the close request


def sign_close(key: bytes, body: bytes) -> str:
    """The signature swarm-api sends with a close request's raw body. Here for
    the tests; `swarm_api.live.relay_close_request` is the minter."""
    return _b64encode(hmac.new(key, CLOSE_DOMAIN + body, hashlib.sha256).digest())


@dataclass(frozen=True)
class CloseRequest:
    session_id: str
    cause: str
    exp: int
    nonce: str


def verify_close(key: bytes, body: bytes, signature: str | None, *, now: float) -> CloseRequest:
    """A close request swarm-api signed: MAC over the raw body, then shape and clock."""
    if not signature or not body or len(body) > MAX_CLOSE_BODY or len(signature) > 128:
        raise Refused("malformed")
    try:
        given = _b64decode(signature)
    except (binascii.Error, ValueError):
        raise Refused("malformed") from None
    if not hmac.compare_digest(given, hmac.new(key, CLOSE_DOMAIN + body, hashlib.sha256).digest()):
        raise Refused("bad_signature")
    try:
        doc = json.loads(body)
    except ValueError:
        raise Refused("malformed") from None
    if not isinstance(doc, dict) or set(doc) != set(_CLOSE_FIELDS):
        raise Refused("malformed")
    if (not all(isinstance(doc[f], str) and doc[f] for f in ("session_id", "cause", "nonce"))
            or not isinstance(doc["exp"], int) or isinstance(doc["exp"], bool)):
        raise Refused("malformed")
    request = CloseRequest(**doc)
    if now > request.exp:
        raise Refused("expired")
    if request.exp - now > MAX_TICKET_SECONDS + CLOCK_SKEW_SECONDS:
        raise Refused("lifetime")
    return request


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


# ---------------------------------------------------------------- the session


@dataclass(frozen=True)
class SessionRecord:
    """A session as its tunnel ticket named it: what every viewer ticket for it
    must match. Nothing here was read from Firestore."""

    session_id: str
    tenant_id: str
    task_id: str
    attempt_id: str
    generation: int

    @classmethod
    def of(cls, ticket: Ticket) -> "SessionRecord":
        return cls(session_id=ticket.session_id, tenant_id=ticket.tenant_id,
                   task_id=ticket.task_id, attempt_id=ticket.attempt_id,
                   generation=ticket.generation)


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
    def __init__(self, key: bytes, *, clock: Callable[[], float] = time.time,
                 recorder_factory: RecorderFactory | None = None,
                 max_viewers_per_task: int = MAX_VIEWERS_PER_TASK,
                 max_viewers_per_tenant: int = MAX_VIEWERS_PER_TENANT,
                 queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"swarm-live-key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key
        self._clock = clock
        self._recorder_factory = recorder_factory
        self._max_task = max_viewers_per_task
        self._max_tenant = max_viewers_per_tenant
        self._queue_size = queue_size
        self._ledger = TicketLedger()
        self._tunnels: dict[str, Tunnel] = {}
        #: session id -> when swarm-api closed it; see CLOSED_MEMORY_SECONDS.
        self._closed: dict[str, float] = {}
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

    def _is_closed(self, session_id: str, now: float) -> bool:
        self._closed = {sid: at for sid, at in self._closed.items()
                        if now - at <= CLOSED_MEMORY_SECONDS}
        return session_id in self._closed

    def _admit(self, raw: str, role: str) -> Ticket:
        """Verify, spend, and check a ticket's role and session.

        Everything runs on the event loop with no await, so two presentations
        of one ticket cannot both pass the ledger.
        """
        now = self._clock()
        ticket = verify_ticket(self._key, raw, now=now)
        self._ledger.spend(ticket.nonce, ticket.exp, now)
        if ticket.role != role:
            raise Refused("wrong_role")
        if self._is_closed(ticket.session_id, now):
            raise Refused("session_closed")
        return ticket

    # -- the gate's tunnel ---------------------------------------------

    async def open_tunnel(self, raw: str, endpoint: Endpoint) -> Tunnel:
        ticket = self._admit(raw, "tunnel")
        record = SessionRecord.of(ticket)
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
        ticket = self._admit(raw, "viewer")
        record = SessionRecord.of(ticket)
        async with self._lock:
            tunnel = self._tunnels.get(record.session_id)
            if tunnel is None or tunnel.closed:
                raise Refused("no_tunnel")
            # What the tunnel ticket said is the session; a viewer ticket for
            # another tenant, task, attempt or generation does not join it.
            for name in ("tenant_id", "task_id", "attempt_id", "generation"):
                if getattr(record, name) != getattr(tunnel.session, name):
                    raise Refused("wrong_" + name.removesuffix("_id"))
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
        """swarm-api's control message: `{"type": "control", "controller": bool}`.

        No ticket: this socket was opened with a one-use viewer ticket only
        swarm-api presents, from the only egress the relay admits on /v1/view,
        and swarm-api sends this after its own transaction gave or took this
        person's control (§4.2). Anything else is refused, and nothing reaches
        the gate.
        """
        if viewer.closed:
            return
        try:
            message = json.loads(text)
        except ValueError:
            message = None
        kind = message.get("type") if isinstance(message, dict) else None
        grant = message.get("controller") if isinstance(message, dict) else None
        if kind != "control" or not isinstance(grant, bool):
            viewer.outbox.put(False, json.dumps({"type": "refused", "reason": "malformed"}))
            return
        tunnel = viewer.tunnel
        if grant:
            if tunnel.controller == viewer.conn_id:
                return
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

    async def close_session(self, session_id: str, cause: str) -> bool:
        """swarm-api closed this session, or saw its generation go stale
        (invariant 5, §5.11): drop the tunnel and every viewer, and refuse any
        ticket for it still inside its lifetime. True when anything was open."""
        self._is_closed(session_id, self._clock())
        self._closed[session_id] = self._clock()
        tunnel = self._tunnels.get(session_id)
        if tunnel is None:
            log.info("live relay: session=%s closed by swarm-api (%s); nothing was open",
                     session_id, cause)
            return False
        await self._end_tunnel(tunnel, CLOSE_ENDED, "session_closed")
        log.info("live relay: session=%s closed by swarm-api (%s)", session_id, cause)
        return True

    async def close_request(self, body: bytes, signature: str | None) -> CloseRequest:
        """`POST /internal/close`: verified, spent once, then acted on."""
        now = self._clock()
        request = verify_close(self._key, body, signature, now=now)
        self._ledger.spend("close:" + request.nonce, request.exp, now)
        await self.close_session(request.session_id, request.cause)
        return request

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
