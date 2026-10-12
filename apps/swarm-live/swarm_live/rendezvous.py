"""Where the gate's tunnel and the viewers' streams meet (LB-L7).

docs/design/live-browser.md §3.3 (the relay, and why it is one process), §5.2
(every connection checked), §5.10 (viewer caps) and §5.11 (replay, a stale
generation). The relay is NOT trusted with authorisation: swarm-api decides who
may watch and who may control, and says so with a ticket. The relay's job is to
check that each ticket is genuine, unspent, unexpired and names exactly the
session it is used on -- then to splice bytes.

THE RELAY READS NOTHING BUT THE TICKET (owner decision 2026-10-11 (a), §3.3).
It has no Firestore client and no session document. A tunnel ticket DEFINES
the session it opens: its tenant, task, attempt and generation. Every later
ticket for that session -- a viewer's, a control message's -- must name the
same four, or it is refused. swarm-api ends a session by POSTing a close
request (below) when the session closes or its generation goes stale; the
relay then drops every connection of it and refuses every ticket for it that
is still in flight.

TICKETS. `<base64url(json)>.<base64url(hmac-sha256)>`, the MAC under
`swarm-live-key` (held by swarm-api and swarm-live only) over a domain tag and
the encoded body, so a MAC for any other purpose is never a ticket. The body
names `session_id, tenant_id, task_id, attempt_id, generation, role, exp` (§3.3)
plus `email` for a person's role and a random `nonce` that makes each ticket
one-use. Roles:

* `tunnel`  -- the gate's tunnel, minted for the worker (60 s);
* `viewer`  -- one proxied console websocket, minted by swarm-api (30 s).

Those two open connections. Two more are MESSAGES on a viewer connection that
is already open, never a connection of their own:

* `control` -- swarm-api granting control to that viewer, after its
  transaction on `live_sessions/{id}` (§4.2) (30 s);
* `release` -- swarm-api ending that viewer's control (30 s);
* `device`  -- swarm-api passing on that viewer's device change (30 s, lane
  LB-DEV-B). Its body carries one more field, `device`: the change itself
  (`preset` by name, optional `orientation` and `touch`), so the MAC covers
  what is applied and a ticket for one preset is never a ticket for another.

A ticket is refused if its MAC is wrong, it has expired, it claims a lifetime
longer than its role's, it was presented before, its role is not the one the
route takes, its session was closed, or its tenant, task, attempt or
generation differs from the session's tunnel ticket. Every refusal of a ticket
closes with the same code, so a caller probing for another tenant's session
learns nothing it would not learn from a session that does not exist (§5.2
step 2: "a refusal is a 404").

CLOSE REQUESTS. `POST /internal/close`, body `<base64url(json)>.<base64url(mac)>`
in the ticket's shape under its OWN domain tag, so no ticket is ever a close
request and no close request is ever a ticket. The body names `session_id`,
`exp` (at most 60 s ahead) and a one-use `nonce`.

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
* `DEVICE`  relay -> gate: the controller's device change, JSON
            `{"preset", "orientation"?, "touch"?}`, with the controller's
            conn id. gate -> relay: the gate's report, JSON `{"ok", "preset",
            "orientation", "touch", "cause"?}`; an empty conn id is every
            viewer (the device as applied), a viewer's id that viewer alone
            (its change was refused). The relay keeps the last applied report
            and sends it to each viewer that joins, so swarm-api learns the
            device without waiting for a change (LB-DEV-B).

A viewer's websocket carries that viewer's RFB bytes as binary messages, and
swarm-api's control messages as text: `{"type": "control", "ticket": ...}`
(a `control` or `release` ticket) and `{"type": "device", "ticket": ...}` (a
`device` ticket). The relay sends a viewer the gate's device report as text,
`{"type": "device", "ok", "preset", "orientation", "touch", "cause"?}`.

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
#: A close request's MAC domain: never a ticket's.
CLOSE_DOMAIN = b"swarm-live-close/v1\0"

#: Lifetime of each role's ticket (§3.3: 60 s for the tunnel; §5.2: 30 s for a
#: viewer). Control tickets are minted by swarm-api the moment its transaction
#: commits and presented at once, so they get the viewer's 30 s.
TICKET_TTL_SECONDS = {"tunnel": 60, "viewer": 30, "control": 30, "release": 30, "device": 30}

#: Clock skew tolerated between swarm-api's clock and the relay's, on expiry and
#: on the lifetime check. Both run on Google infrastructure with NTP.
CLOCK_SKEW_SECONDS = 5

#: A close request's lifetime: the decision's 60 s, as the tunnel ticket's.
CLOSE_TTL_SECONDS = 60

#: How long a closed session's id is remembered. Every ticket minted before
#: the close has expired by then -- the longest lives 60 s, plus skew either
#: way -- and swarm-api mints none after it (a closed session never reopens:
#: `LiveService.open_session` refuses it), so forgetting it then admits nothing.
CLOSED_MEMORY_SECONDS = max(TICKET_TTL_SECONDS.values()) + 2 * CLOCK_SKEW_SECONDS

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

OP_OPEN, OP_DATA, OP_CLOSE, OP_CONTROL, OP_ACTION, OP_DEVICE = 1, 2, 3, 4, 5, 6
_OPS = frozenset({OP_OPEN, OP_DATA, OP_CLOSE, OP_CONTROL, OP_ACTION, OP_DEVICE})

#: Websocket close codes. Every ticket refusal is CLOSE_REFUSED ("404").
CLOSE_REFUSED = 4404
CLOSE_BUSY = 4409
CLOSE_ENDED = 4410
CLOSE_FULL = 4429
CLOSE_SLOW = 4408

_TICKET_FIELDS = ("session_id", "tenant_id", "task_id", "attempt_id", "generation",
                  "role", "exp", "nonce", "email")
_CLOSE_FIELDS = ("session_id", "exp", "nonce")
#: The one field a `device` ticket carries beyond the others: the change.
DEVICE_FIELD = "device"
#: What a device change may name. A size, a scale or a user agent is not here:
#: a preset is chosen by name (invariant 10's spirit, §6.1); the gate checks the
#: name against its own list.
DEVICE_KEYS = frozenset({"preset", "orientation", "touch"})
ORIENTATIONS = ("portrait", "landscape")
MAX_PRESET_CHARS = 32
#: The roles that open a connection; the others are messages on one.
CONNECTION_ROLES = ("tunnel", "viewer")


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


def _mac(key: bytes, body: str, domain: bytes = TICKET_DOMAIN) -> bytes:
    return hmac.new(key, domain + body.encode("ascii"), hashlib.sha256).digest()


def _signed(key: bytes, body: dict[str, Any], domain: bytes) -> str:
    encoded = _b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return encoded + "." + _b64encode(_mac(key, encoded, domain))


def _unsigned(key: bytes, raw: Any, domain: bytes) -> dict[str, Any]:
    """The JSON body of `raw` when `key` signed it under `domain`."""
    if not isinstance(raw, str) or len(raw) > MAX_TICKET_CHARS or raw.count(".") != 1:
        raise Refused("malformed")
    encoded, mac = raw.split(".")
    try:
        given = _b64decode(mac)
    except (binascii.Error, ValueError):
        raise Refused("malformed") from None
    if not encoded or not hmac.compare_digest(given, _mac(key, encoded, domain)):
        raise Refused("bad_signature")
    try:
        body = json.loads(_b64decode(encoded))
    except (binascii.Error, ValueError):
        raise Refused("malformed") from None
    if not isinstance(body, dict):
        raise Refused("malformed")
    return body


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
    #: A `device` ticket's change; None on every other role.
    device: dict[str, Any] | None = None


def device_change(value: Any) -> dict[str, Any]:
    """`value` as a device change -- a preset by name, an orientation, touch on
    or off -- or `Refused`. The relay checks the shape; the gate checks the name."""
    if (not isinstance(value, dict) or not set(value) <= DEVICE_KEYS
            or not isinstance(value.get("preset"), str)
            or not 0 < len(value["preset"]) <= MAX_PRESET_CHARS
            or not all(c.isascii() and (c.isalnum() or c == "-") for c in value["preset"])
            or value.get("orientation", "portrait") not in ORIENTATIONS
            or not isinstance(value.get("touch", False), bool)):
        raise Refused("malformed")
    return {k: v for k, v in value.items() if v is not None}


def mint_ticket(key: bytes, *, session_id: str, tenant_id: str, task_id: str, attempt_id: str,
                generation: int, role: str, email: str = "", now: float | None = None,
                ttl: int | None = None, device: dict[str, Any] | None = None) -> str:
    """A ticket as swarm-api mints it. The relay never mints one in production;
    `swarm_api.live.mint_ticket` restates this format, and
    tests/unit/control_plane/test_live_relay_wire.py holds the two to each
    other (a frozen-contract home for it is a request, not a change)."""
    if role not in TICKET_TTL_SECONDS:
        raise ValueError(f"unknown ticket role {role!r}")
    issued = int(time.time() if now is None else now)
    body = {
        "session_id": session_id, "tenant_id": tenant_id, "task_id": task_id,
        "attempt_id": attempt_id, "generation": int(generation), "role": role,
        "exp": issued + (TICKET_TTL_SECONDS[role] if ttl is None else int(ttl)),
        "nonce": secrets.token_hex(16), "email": email,
    }
    if (role == "device") != (device is not None):
        raise ValueError("a device ticket, and only a device ticket, carries a device")
    if device is not None:
        body[DEVICE_FIELD] = device_change(device)
    return _signed(key, body, TICKET_DOMAIN)


def verify_ticket(key: bytes, raw: str, *, now: float) -> Ticket:
    """Check the MAC, the shape and the clock. One-use is the ledger's job."""
    body = _unsigned(key, raw, TICKET_DOMAIN)
    device = None
    if body.get("role") == "device":
        device = device_change(body.pop(DEVICE_FIELD, None))
    if set(body) != set(_TICKET_FIELDS):
        raise Refused("malformed")
    strings = [body[f] for f in _TICKET_FIELDS if f not in ("generation", "exp")]
    if (not all(isinstance(s, str) for s in strings)
            or not all(isinstance(body[f], int) and not isinstance(body[f], bool)
                       for f in ("generation", "exp"))):
        raise Refused("malformed")
    ticket = Ticket(**body, device=device)
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


def mint_close(key: bytes, session_id: str, *, now: float | None = None,
               ttl: int = CLOSE_TTL_SECONDS) -> str:
    """A close request as swarm-api mints it (`swarm_api.live.mint_close`
    restates it; tests/unit/control_plane/test_live_relay_wire.py holds the two
    to each other)."""
    issued = int(time.time() if now is None else now)
    return _signed(key, {"session_id": session_id, "exp": issued + int(ttl),
                         "nonce": secrets.token_hex(16)}, CLOSE_DOMAIN)


@dataclass(frozen=True)
class CloseRequest:
    session_id: str
    exp: int
    nonce: str


def verify_close(key: bytes, raw: str, *, now: float) -> CloseRequest:
    """A close request's MAC, shape and clock, as `verify_ticket` checks a ticket's."""
    body = _unsigned(key, raw, CLOSE_DOMAIN)
    if (set(body) != set(_CLOSE_FIELDS)
            or not isinstance(body["session_id"], str) or not body["session_id"]
            or not isinstance(body["nonce"], str) or not body["nonce"]
            or not isinstance(body["exp"], int) or isinstance(body["exp"], bool)):
        raise Refused("malformed")
    request = CloseRequest(**body)
    if now > request.exp:
        raise Refused("expired")
    if request.exp - now > CLOSE_TTL_SECONDS + CLOCK_SKEW_SECONDS:
        raise Refused("lifetime")
    return request


class TicketLedger:
    """Nonces already presented, kept until their ticket could no longer verify."""

    def __init__(self) -> None:
        self._spent: dict[str, int] = {}

    def spend(self, ticket: Ticket | CloseRequest, now: float) -> None:
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


# ---------------------------------------------------------------- the session


@dataclass(frozen=True)
class SessionRecord:
    """A session as its tunnel ticket names it. The relay has no other source."""

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


#: The fields every later ticket must share with the session's tunnel ticket.
_SESSION_FIELDS = ("tenant_id", "task_id", "attempt_id", "generation")


def _match(ticket: Ticket, session: SessionRecord) -> None:
    for name in _SESSION_FIELDS:
        if getattr(ticket, name) != getattr(session, name):
            raise Refused("wrong_" + name.removesuffix("_id"))


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
    #: The gate's last applied DEVICE report, for each viewer that joins.
    device: dict[str, Any] | None = None


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
        #: session_id -> when it may be forgotten (CLOSED_MEMORY_SECONDS).
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

    def is_closed(self, session_id: str) -> bool:
        now = self._clock()
        self._closed = {s: until for s, until in self._closed.items() if until >= now}
        return session_id in self._closed

    def _admit(self, raw: str, role: str | tuple[str, ...]) -> Ticket:
        """Verify and spend a ticket, and refuse one for a closed session.

        Synchronous, on the event loop, so two presentations of one ticket
        cannot both pass the ledger. Matching it to its session is the
        caller's: a tunnel ticket defines the session, every other names it.
        """
        now = self._clock()
        ticket = verify_ticket(self._key, raw, now=now)
        self._ledger.spend(ticket, now)
        roles = (role,) if isinstance(role, str) else role
        if ticket.role not in roles:
            raise Refused("wrong_role")
        if self.is_closed(ticket.session_id):
            raise Refused("session_closed")
        return ticket

    # -- the gate's tunnel ---------------------------------------------

    async def open_tunnel(self, raw: str, endpoint: Endpoint) -> Tunnel:
        record = SessionRecord.of(self._admit(raw, "tunnel"))
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
        if op == OP_DEVICE:
            await self._device_report(tunnel, conn_id, payload)
            return
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

    async def _device_report(self, tunnel: Tunnel, conn_id: str, payload: bytes) -> None:
        """The gate's DEVICE report, to every viewer (empty id) or one."""
        try:
            entry = json.loads(payload)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            log.info("live relay: session=%s dropped a malformed device report", tunnel.session.session_id)
            return
        report = {"type": "device", "ok": entry.get("ok") is True,
                  **{k: entry.get(k) for k in ("preset", "orientation", "touch")}}
        if isinstance(entry.get("cause"), str):
            report["cause"] = entry["cause"][:64]
        text = json.dumps(report)
        if not conn_id:
            if report["ok"]:
                tunnel.device = report
            for viewer in list(tunnel.viewers.values()):
                viewer.outbox.put(False, text)
        else:
            viewer = tunnel.viewers.get(conn_id)
            if viewer is not None:
                viewer.outbox.put(False, text)

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
        async with self._lock:
            tunnel = self._tunnels.get(ticket.session_id)
            if tunnel is None or tunnel.closed:
                raise Refused("no_tunnel")
            _match(ticket, tunnel.session)
            record = tunnel.session
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
        if tunnel.device is not None:
            outbox.put(False, json.dumps(tunnel.device))
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
        if kind not in ("control", "device") or not isinstance(raw, str):
            viewer.outbox.put(False, json.dumps({"type": "refused", "reason": "malformed"}))
            return
        if kind == "device":
            await self._device_change(viewer, raw)
            return
        try:
            ticket = self._admit(raw, ("control", "release"))
            if ticket.session_id != viewer.tunnel.session.session_id:
                raise Refused("wrong_session")
            _match(ticket, viewer.tunnel.session)
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

    async def _device_change(self, viewer: Viewer, raw: str) -> None:
        """swarm-api's `device` ticket: this viewer's device change, passed to
        the gate only while this connection holds control (§6.1, Q13)."""
        tunnel = viewer.tunnel
        try:
            ticket = self._admit(raw, "device")
            if ticket.session_id != tunnel.session.session_id:
                raise Refused("wrong_session")
            _match(ticket, tunnel.session)
            if ticket.email != viewer.email:
                raise Refused("wrong_person")
            if tunnel.controller != viewer.conn_id:
                raise Refused("not_controller")
        except Refused as exc:
            log.info("live relay: session=%s conn=%s device refused: %s",
                     tunnel.session.session_id, viewer.conn_id, exc.reason)
            viewer.outbox.put(False, json.dumps({"type": "refused", "reason": "device"}))
            return
        assert ticket.device is not None  # verify_ticket requires it on this role
        tunnel.outbox.put(True, encode_frame(OP_DEVICE, viewer.conn_id,
                                             json.dumps(ticket.device, sort_keys=True).encode()))
        log.info("live relay: session=%s conn=%s device change passed to the gate",
                 tunnel.session.session_id, viewer.conn_id)

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

    async def close_session(self, session_id: str) -> int:
        """Drop every connection of `session_id` and refuse every ticket for it
        still in flight (invariant 5: a stale generation's session is closed,
        §8.5). The number of connections dropped -- the tunnel and its viewers;
        0 for a session with no tunnel here, which is still remembered closed."""
        self._closed[session_id] = self._clock() + CLOSED_MEMORY_SECONDS
        tunnel = self._tunnels.get(session_id)
        if tunnel is None:
            log.info("live relay: session=%s closed with no tunnel open", session_id)
            return 0
        dropped = 1 + len(tunnel.viewers)
        await self._end_tunnel(tunnel, CLOSE_ENDED, "session_ended")
        return dropped

    async def close_request(self, raw: str) -> tuple[str, int]:
        """swarm-api's `POST /internal/close`: verified and spent like a
        ticket, then `close_session`. (session_id, connections dropped)."""
        now = self._clock()
        request = verify_close(self._key, raw, now=now)
        self._ledger.spend(request, now)
        return request.session_id, await self.close_session(request.session_id)

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
