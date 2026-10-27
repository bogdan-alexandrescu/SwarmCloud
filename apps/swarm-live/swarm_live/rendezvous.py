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
* `release` -- swarm-api ending that viewer's control (30 s).

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

DEVICE REQUESTS (LB-DEV-B). `POST /internal/device`, the same shape under a
third domain tag: swarm-api's word that the person in control of a session
chose a device, after it checked that they hold control (§6.1, Q13). The body
names the session as a ticket does (`session_id, tenant_id, task_id,
attempt_id, generation`, matched against the tunnel ticket like every later
ticket), the device (`preset`, `orientation`, `touch`: a NAME, an
orientation or null, a bool or null -- never a size, invariant 10), `exp`
(at most 30 s ahead) and a one-use `nonce`. The relay checks the MAC, the
clock, single use and the session, and passes the device to the gate as a
`DEVICE` frame; it does not know the preset list -- the gate refuses a name
that is not on it, changing nothing.

THE TUNNEL'S FRAMES. After the ticket (the first message, text) and the
relay's `{"type": "ready"}`, every tunnel message is binary:
`op (1 byte) | len(conn_id) (1 byte) | conn_id (ascii) | payload`.

* `OPEN`    relay -> gate: a new RFB connection with this id (a viewer, or the
            recorder).
* `DATA`    both ways: RFB bytes of that connection.
* `CLOSE`   both ways: that connection ended.
* `CONTROL` relay -> gate: the connection that now holds control, or an empty
            id for none. The gate's `ControlState.grant`/`revoke` act on it.
            The same controller again is a renewal of the gate's control
            lease. With an empty id the payload may name why control ended
            (a revoke's cause); empty, it is a release.
* `ACTION`  gate -> relay: one action-log line (JSON) for the recording (§7).
* `DEVICE`  relay -> gate: a device request swarm-api MAC'd, as JSON
            `{"preset", "orientation", "touch"}`, with an empty id. The gate
            applies it through its device controller and reports the result
            with `REPORT`, which is how the session read learns it.
* `REPORT`  gate -> relay: the top frame's origin and the device, as JSON
            `{"origin": {...} | null, "device": {...} | null}` (LB-DEV-A,
            agent_worker/livegate/report.py). The relay keeps the latest per
            session -- in memory, it has no Firestore (decision (a)) -- and
            sends it to every viewer link as text, `{"type": "report", ...}`,
            on each change and after `ready` to a viewer that joins later.
            swarm-api, which proxies every viewer link, validates it field by
            field and records it on `live_sessions/{id}`; the relay checks
            only that it is a small object of those two keys. It is page data:
            never logged, and dropped with its session. A gate from
            LIVE-UNUSED adds a third, `agent` (whether the agent is driving
            this browser, report.py `agent_report`); it is optional, so a
            gate that sends only the two is kept as before. A gate from
            BSTATUS-API adds `browser` (the browser tool and the egress,
            report.py `browser_report`), optional in the same way.

A viewer's websocket carries that viewer's RFB bytes as binary messages, and
swarm-api's control messages as text: `{"type": "control"|"release",
"ticket": ...}`. A control message the relay refuses is answered on that
link with `{"type": "refused", "reason": "control", "cause": <why>}`, and
swarm-api passes it to the console, which says why.

CONTROL FOLLOWS THE PERSON'S INPUT (LB-CURSOR2; part of #1030). swarm-api
decides control per PERSON, and sends a control ticket on every link of
theirs: a second console tab, a hidden one, a thumbnail. Each used to take the
gate's single controller slot as its ticket arrived, so the last link to be
polled won it, and the canvas the person was moving and typing on became a
viewer whose input the gate discarded. MEASURED 2026-10-13: a controller's
input from a hidden console tab counted 0 pointer events and 0 keys. So a link
with a control ticket is GRANTED; the first one is the controller at once, and
another granted link becomes it when its first key, pointer or paste arrives
(sent to the gate as `CONTROL` before that input). A release takes the grant
from every link of that person (swarm-api ends control per person). When the
controller's link leaves, or is released, the most recently granted link still
open takes over -- another of the person's tabs, or the person swarm-api gave
control to next -- so control does not lapse while somebody holds it.

CONTROL REQUESTS (CONTROL-LAPSE; part of #1030). `POST /internal/control`,
the same shape under a fourth domain tag: swarm-api's word that a person's
control ENDED (`revoke`) or was RENEWED (`renew`), sent after the transaction
that recorded it. The body names the session as a ticket does, the person
(`email`), the `action`, a `cause` (why it ended: a short code such as
`renew_lapsed`; empty for a renewal), `exp` (at most 30 s ahead) and a
one-use `nonce`. A control ticket travels on the person's own viewer link, so
an end of control used to reach the gate only if that link was still open
when swarm-api noticed it: MEASURED 2026-10-13 (task e8cb...), control lapsed
at 15:11:12Z and the gate held it until a relay restart at 16:03:47Z, while
the agent was refused 72 times. A revoke takes the grant from every link of
that person, and when nobody is left in control it sends the gate `CONTROL`
with an empty id and the cause as its payload -- even when the relay had
already forgotten the controller (its link left), since the gate had not. It
is idempotent: a second revoke changes nothing at the relay and the gate's
revoke of nobody is a no-op. A renewal re-sends `CONTROL` naming the current
controller when that link is the person's, which the gate counts as a renewal
of its control lease (agent_worker/livegate/__main__.py
`CONTROL_LEASE_SECONDS`): a gate that hears nothing for longer takes control
back by itself.

NOTHING READ FROM A CONNECTION IS LOGGED. RFB bytes are a person's keystrokes
(§5.3.3), and a ticket admits whoever holds it. Log lines carry reason codes,
session ids, connection ids and counts.

THE CAPS (§5.10). 10 concurrent viewers per task, 50 per tenant. The relay is a
single replica (§3.3), so these counts are global. The recorder's connection is
not a viewer and is not counted.

THE RECORDER'S CONNECTION IS OPENED AGAIN WHEN THE GATE ENDS IT (part of
#1346). The gate's `StreamMux` sends CLOSE when the `RfbSession` serving a
connection returns. For a viewer the relay drops the viewer and noVNC
reconnects; the recorder has no one to reconnect it, and until this change
its CLOSE was dropped on the floor, so the recorder kept the last frame it
decoded and recorded it for the rest of the session. Now the recorder is given
a new connection id and a fresh decoder (`Recorder.reopen`) and the gate an
OPEN for it, after `RECORDER_REOPEN_DELAYS`: fast at first, longer while a
reopened stream closes again before its first frame, back to the first once a
frame arrives. Nothing is recorded between the CLOSE and that frame. #1346
comment 5 holds a run's recorder view to 10 s unavailable, so the first tries
fit well inside it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)

TICKET_DOMAIN = b"swarm-live-ticket/v1\0"
#: A close request's MAC domain: never a ticket's.
CLOSE_DOMAIN = b"swarm-live-close/v1\0"
#: A device request's MAC domain (LB-DEV-B): never a ticket's or a close's.
DEVICE_DOMAIN = b"swarm-live-device/v1\0"
#: A control request's MAC domain (CONTROL-LAPSE): never a ticket's, a
#: close's or a device's.
CONTROL_DOMAIN = b"swarm-live-control/v1\0"

#: Lifetime of each role's ticket (§3.3: 60 s for the tunnel; §5.2: 30 s for a
#: viewer). Control tickets are minted by swarm-api the moment its transaction
#: commits and presented at once, so they get the viewer's 30 s.
TICKET_TTL_SECONDS = {"tunnel": 60, "viewer": 30, "control": 30, "release": 30}

#: Clock skew tolerated between swarm-api's clock and the relay's, on expiry and
#: on the lifetime check. Both run on Google infrastructure with NTP.
CLOCK_SKEW_SECONDS = 5

#: A close request's lifetime: the decision's 60 s, as the tunnel ticket's.
CLOSE_TTL_SECONDS = 60

#: A device request's lifetime: minted the moment swarm-api has checked the
#: controller and posted at once, so the control ticket's 30 s.
DEVICE_TTL_SECONDS = 30

#: A control request's lifetime: minted the moment swarm-api's transaction
#: commits and posted at once, so the control ticket's 30 s.
CONTROL_TTL_SECONDS = 30

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

OP_OPEN, OP_DATA, OP_CLOSE, OP_CONTROL, OP_ACTION, OP_REPORT, OP_DEVICE = 1, 2, 3, 4, 5, 6, 7
_OPS = frozenset({OP_OPEN, OP_DATA, OP_CLOSE, OP_CONTROL, OP_ACTION, OP_REPORT, OP_DEVICE})
#: RFB client messages that are a person's input (RFC 6143 §7.5): KeyEvent,
#: PointerEvent, ClientCutText. By the first byte, as swarm-api counts them.
_RFB_INPUT = frozenset({4, 5, 6})
#: The largest REPORT kept (the gate's `report.MAX_REPORT_BYTES`): a
#: 253-character host and a device fit in well under half of it.
MAX_REPORT_BYTES = 4096
_REPORT_KEYS = frozenset({"origin", "device"})
#: Keys a report MAY carry besides those (LIVE-UNUSED, BSTATUS-API): passed
#: on when present.
_REPORT_OPTIONAL = frozenset({"agent", "browser"})

#: The waits before the recorder's connection is opened again after the gate
#: closed it (module docstring), the last repeated. A reopened stream that
#: closes again before a frame takes the next; one that delivers a frame puts
#: the next drop back at the first. Fast first, because a QA run fails at 10 s
#: of recorder view unavailable (#1346 comment 5); longer later, so a gate that
#: refuses every recorder stream is sent an OPEN every 5 s, not in a loop.
RECORDER_REOPEN_DELAYS = (0.1, 0.5, 1.0, 2.0, 5.0)

#: Websocket close codes. Every ticket refusal is CLOSE_REFUSED ("404").
CLOSE_REFUSED = 4404
CLOSE_BUSY = 4409
CLOSE_ENDED = 4410
CLOSE_FULL = 4429
CLOSE_SLOW = 4408
#: The relay is restarting (RELAY-ROLL): reconnect now. RFC 6455's 1012
#: "Service Restart", the code uvicorn itself closes with on SIGTERM and the
#: one swarm-api already closes a viewer with when its relay link ends, so the
#: gate, swarm-api and the console all read one code. Unlike CLOSE_ENDED it
#: says nothing about the session: that is still open, on the next relay.
CLOSE_RESTARTING = 1012

_TICKET_FIELDS = ("session_id", "tenant_id", "task_id", "attempt_id", "generation",
                  "role", "exp", "nonce", "email")
_CLOSE_FIELDS = ("session_id", "exp", "nonce")
_DEVICE_FIELDS = ("session_id", "tenant_id", "task_id", "attempt_id", "generation",
                  "preset", "orientation", "touch", "exp", "nonce")
_CONTROL_FIELDS = ("session_id", "tenant_id", "task_id", "attempt_id", "generation",
                   "email", "action", "cause", "exp", "nonce")
#: What a control request may ask: end a person's control, or say it goes on.
CONTROL_ACTIONS = ("revoke", "renew")
#: A revoke's cause: a short code (swarm-api's END_CAUSES), passed to the gate.
_CAUSE = re.compile(r"[a-z_]{1,32}")
#: A preset name is short; the gate holds the list (presets.py).
MAX_PRESET_CHARS = 32
_ORIENTATIONS = ("portrait", "landscape")
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


def mint_ticket(key: bytes, *, session_id: str, tenant_id: str, task_id: str, attempt_id: str,
                generation: int, role: str, email: str = "", now: float | None = None,
                ttl: int | None = None) -> str:
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
    return _signed(key, body, TICKET_DOMAIN)


def verify_ticket(key: bytes, raw: str, *, now: float) -> Ticket:
    """Check the MAC, the shape and the clock. One-use is the ledger's job."""
    body = _unsigned(key, raw, TICKET_DOMAIN)
    if set(body) != set(_TICKET_FIELDS):
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


def mint_device(key: bytes, *, session_id: str, tenant_id: str, task_id: str, attempt_id: str,
                generation: int, preset: str, orientation: str | None = None,
                touch: bool | None = None, now: float | None = None,
                ttl: int = DEVICE_TTL_SECONDS) -> str:
    """A device request as swarm-api mints it (`swarm_api.live.mint_device`
    restates it; tests/unit/control_plane/test_live_relay_wire.py holds the two
    to each other)."""
    issued = int(time.time() if now is None else now)
    return _signed(key, {
        "session_id": session_id, "tenant_id": tenant_id, "task_id": task_id,
        "attempt_id": attempt_id, "generation": int(generation), "preset": preset,
        "orientation": orientation, "touch": touch, "exp": issued + int(ttl),
        "nonce": secrets.token_hex(16),
    }, DEVICE_DOMAIN)


@dataclass(frozen=True)
class DeviceRequest:
    session_id: str
    tenant_id: str
    task_id: str
    attempt_id: str
    generation: int
    preset: str
    orientation: str | None
    touch: bool | None
    exp: int
    nonce: str

    def payload(self) -> bytes:
        """The `DEVICE` frame's payload: the device and nothing else."""
        return json.dumps({"preset": self.preset, "orientation": self.orientation,
                           "touch": self.touch}, separators=(",", ":")).encode()


def verify_device(key: bytes, raw: str, *, now: float) -> DeviceRequest:
    """A device request's MAC, shape and clock. A preset is a short string,
    never a size; whether it is ON the list is the gate's check."""
    body = _unsigned(key, raw, DEVICE_DOMAIN)
    if set(body) != set(_DEVICE_FIELDS):
        raise Refused("malformed")
    strings = ("session_id", "tenant_id", "task_id", "attempt_id", "preset", "nonce")
    if (not all(isinstance(body[f], str) and body[f] for f in strings)
            or len(body["preset"]) > MAX_PRESET_CHARS
            or body["orientation"] not in (None, *_ORIENTATIONS)
            or not (body["touch"] is None or isinstance(body["touch"], bool))
            or not all(isinstance(body[f], int) and not isinstance(body[f], bool)
                       for f in ("generation", "exp"))):
        raise Refused("malformed")
    request = DeviceRequest(**body)
    if now > request.exp:
        raise Refused("expired")
    if request.exp - now > DEVICE_TTL_SECONDS + CLOCK_SKEW_SECONDS:
        raise Refused("lifetime")
    return request


def mint_control(key: bytes, *, session_id: str, tenant_id: str, task_id: str, attempt_id: str,
                 generation: int, email: str, action: str, cause: str = "",
                 now: float | None = None, ttl: int = CONTROL_TTL_SECONDS) -> str:
    """A control request as swarm-api mints it (`swarm_api.live.mint_control`
    restates it; tests/unit/control_plane/test_live_relay_wire.py holds the two
    to each other)."""
    issued = int(time.time() if now is None else now)
    return _signed(key, {
        "session_id": session_id, "tenant_id": tenant_id, "task_id": task_id,
        "attempt_id": attempt_id, "generation": int(generation), "email": email,
        "action": action, "cause": cause, "exp": issued + int(ttl),
        "nonce": secrets.token_hex(16),
    }, CONTROL_DOMAIN)


@dataclass(frozen=True)
class ControlRequest:
    session_id: str
    tenant_id: str
    task_id: str
    attempt_id: str
    generation: int
    email: str
    action: str
    cause: str
    exp: int
    nonce: str


def verify_control(key: bytes, raw: str, *, now: float) -> ControlRequest:
    """A control request's MAC, shape and clock. A revoke carries a short
    cause code; a renewal carries none."""
    body = _unsigned(key, raw, CONTROL_DOMAIN)
    if set(body) != set(_CONTROL_FIELDS):
        raise Refused("malformed")
    strings = ("session_id", "tenant_id", "task_id", "attempt_id", "email", "nonce")
    if (not all(isinstance(body[f], str) and body[f] for f in strings)
            or body["action"] not in CONTROL_ACTIONS
            or not isinstance(body["cause"], str)
            or (body["action"] == "revoke" and not _CAUSE.fullmatch(body["cause"]))
            or (body["action"] == "renew" and body["cause"])
            or not all(isinstance(body[f], int) and not isinstance(body[f], bool)
                       for f in ("generation", "exp"))):
        raise Refused("malformed")
    request = ControlRequest(**body)
    if now > request.exp:
        raise Refused("expired")
    if request.exp - now > CONTROL_TTL_SECONDS + CLOCK_SKEW_SECONDS:
        raise Refused("lifetime")
    return request


class TicketLedger:
    """Nonces already presented, kept until their ticket could no longer verify."""

    def __init__(self) -> None:
        self._spent: dict[str, int] = {}

    def spend(self, ticket: Ticket | CloseRequest | DeviceRequest | ControlRequest,
              now: float) -> None:
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


def _match(ticket: Ticket | DeviceRequest | ControlRequest, session: SessionRecord) -> None:
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
    @property
    def ready(self) -> bool: ...
    def reopen(self) -> None: ...
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
    #: Links holding a control ticket, oldest first (module docstring).
    granted: list[str] = field(default_factory=list)
    recording: Recording | None = None
    recording_task: asyncio.Task[None] | None = None
    #: The wait before the recorder's connection is opened again, while one
    #: runs, and how many reopens in a row have had no frame yet
    #: (`RECORDER_REOPEN_DELAYS`).
    recording_reopen: asyncio.Task[None] | None = None
    recording_reopens: int = 0
    #: The gate's latest REPORT, as the text every viewer link is sent.
    report: str | None = None
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
                 queue_size: int = DEFAULT_QUEUE_SIZE,
                 recorder_reopen_delays: tuple[float, ...] = RECORDER_REOPEN_DELAYS) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"swarm-live-key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key
        self._clock = clock
        self._recorder_factory = recorder_factory
        self._max_task = max_viewers_per_task
        self._max_tenant = max_viewers_per_tenant
        self._queue_size = queue_size
        if not recorder_reopen_delays:
            raise ValueError("recorder_reopen_delays needs at least one delay")
        self._reopen_delays = recorder_reopen_delays
        self._ledger = TicketLedger()
        self._tunnels: dict[str, Tunnel] = {}
        #: session_id -> when it may be forgotten (CLOSED_MEMORY_SECONDS).
        self._closed: dict[str, float] = {}
        self._lock = asyncio.Lock()
        #: Set by `restarting()`: every connection is told to reconnect, and
        #: every new one is refused with CLOSE_RESTARTING until the process ends.
        self._restarting = False

    # -- counts --------------------------------------------------------

    def tunnel_count(self) -> int:
        return len(self._tunnels)

    def viewer_count(self, *, task_id: str | None = None, tenant_id: str | None = None) -> int:
        return sum(len(t.viewers) for t in self._tunnels.values()
                   if (task_id is None or t.session.task_id == task_id)
                   and (tenant_id is None or t.session.tenant_id == tenant_id))

    def controlled_count(self) -> int:
        """Sessions a person is in control of here: what a relay roll waits for."""
        return sum(1 for t in self._tunnels.values() if t.controller is not None)

    def controller(self, session_id: str) -> str | None:
        tunnel = self._tunnels.get(session_id)
        return tunnel.controller if tunnel else None

    def report(self, session_id: str) -> dict[str, Any] | None:
        """The gate's latest report for `session_id`: `{"origin", "device"}`, or None."""
        tunnel = self._tunnels.get(session_id)
        if tunnel is None or tunnel.report is None:
            return None
        message = json.loads(tunnel.report)
        return {key: message[key] for key in _REPORT_KEYS | _REPORT_OPTIONAL if key in message}

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
        if self._restarting:
            # Before the ticket is spent: the next relay may still admit it.
            raise Refused("relay_restarting", CLOSE_RESTARTING)
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
        if op == OP_REPORT:
            self._take_report(tunnel, payload)
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
                if rec.ready:
                    tunnel.recording_reopens = 0
            elif op == OP_CLOSE:
                self._reopen_recording(tunnel, rec)
            return
        viewer = tunnel.viewers.get(conn_id)
        if viewer is None:
            return
        if op == OP_DATA:
            viewer.outbox.put(True, payload)
        elif op == OP_CLOSE:
            await self._drop_viewer(viewer, CLOSE_ENDED, "closed_by_gate", tell_gate=False)

    def _take_report(self, tunnel: Tunnel, payload: bytes) -> None:
        """Keep the gate's report and pass it to every viewer link. Anything
        that is not a small object of `origin` and `device` (and, from a
        LIVE-UNUSED gate, `agent`), each an object or null, is dropped: the report is page data (swarm-api checks it again)."""
        report: Any = None
        if len(payload) <= MAX_REPORT_BYTES:
            try:
                report = json.loads(payload)
            except ValueError:
                report = None
        if (not isinstance(report, dict) or not _REPORT_KEYS <= set(report)
                or not set(report) <= _REPORT_KEYS | _REPORT_OPTIONAL
                or not all(v is None or isinstance(v, dict) for v in report.values())):
            log.info("live relay: session=%s dropped a malformed report", tunnel.session.session_id)
            return
        text = json.dumps({"type": "report", **{k: report[k] for k in sorted(report)}},
                          separators=(",", ":"))
        tunnel.report = text
        for viewer in tunnel.viewers.values():
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
        # A restart is said to the viewers too: their session is not over.
        viewer_code = CLOSE_RESTARTING if code == CLOSE_RESTARTING else CLOSE_ENDED
        for viewer in list(tunnel.viewers.values()):
            await self._drop_viewer(viewer, viewer_code, reason, tell_gate=False)
        rec, tunnel.recording = tunnel.recording, None
        if rec is not None:
            await self._stop_recording(tunnel, rec)
        await tunnel.outbox.shut(code, reason, drain=drain and code != CLOSE_SLOW)
        log.info("live relay: session=%s tunnel closed: %s", tunnel.session.session_id, reason)

    def _reopen_recording(self, tunnel: Tunnel, rec: Recording) -> None:
        """The gate ended the recorder's connection: open another after the
        backoff's next delay (module docstring). Never waits."""
        if tunnel.recording_reopen is not None and not tunnel.recording_reopen.done():
            return
        delays = self._reopen_delays
        delay = delays[min(tunnel.recording_reopens, len(delays) - 1)]
        tunnel.recording_reopens += 1
        log.info("live relay: session=%s recorder connection %s closed by the gate; reopening in %.1fs (try %d)",
                 tunnel.session.session_id, rec.conn_id, delay, tunnel.recording_reopens)
        tunnel.recording_reopen = asyncio.create_task(self._reopen_after(tunnel, rec, delay))

    async def _reopen_after(self, tunnel: Tunnel, rec: Recording, delay: float) -> None:
        await asyncio.sleep(delay)
        if tunnel.closed or tunnel.recording is not rec:
            return
        rec.reopen()
        tunnel.outbox.put(True, encode_frame(OP_OPEN, rec.conn_id))

    async def _stop_recording(self, tunnel: Tunnel, rec: Recording) -> None:
        reopen, tunnel.recording_reopen = tunnel.recording_reopen, None
        if reopen is not None:
            reopen.cancel()
            await asyncio.gather(reopen, return_exceptions=True)
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
        if tunnel.report is not None:
            outbox.put(False, tunnel.report)
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
        tunnel = viewer.tunnel
        if (data[:1] and data[0] in _RFB_INPUT and tunnel.controller != viewer.conn_id
                and viewer.conn_id in tunnel.granted):
            # This person's input comes from this link now (module docstring).
            self._set_controller(tunnel, viewer.conn_id)
        tunnel.outbox.put(True, encode_frame(OP_DATA, viewer.conn_id, data))

    def _set_controller(self, tunnel: Tunnel, conn_id: str | None) -> None:
        if tunnel.controller == conn_id:
            return
        tunnel.controller = conn_id
        tunnel.outbox.put(True, encode_frame(OP_CONTROL, conn_id or ""))

    def _ungrant(self, tunnel: Tunnel, conn_ids: set[str]) -> bool:
        """Take these links' grants away; True when one was the controller,
        which passes to the newest granted link left, or to none."""
        tunnel.granted = [c for c in tunnel.granted if c not in conn_ids]
        if tunnel.controller not in conn_ids:
            return False
        live = [c for c in tunnel.granted if c in tunnel.viewers]
        tunnel.controller = None
        if live:
            self._set_controller(tunnel, live[-1])
        return True

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
            ticket = self._admit(raw, ("control", "release"))
            if ticket.session_id != viewer.tunnel.session.session_id:
                raise Refused("wrong_session")
            _match(ticket, viewer.tunnel.session)
            if ticket.email != viewer.email:
                raise Refused("wrong_person")
        except Refused as exc:
            log.info("live relay: session=%s conn=%s control refused: %s",
                     viewer.tunnel.session.session_id, viewer.conn_id, exc.reason)
            viewer.outbox.put(False, json.dumps({"type": "refused", "reason": "control",
                                                 "cause": exc.reason}))
            return
        tunnel = viewer.tunnel
        if ticket.role == "control":
            if viewer.conn_id not in tunnel.granted:
                tunnel.granted.append(viewer.conn_id)
            current = tunnel.controller
            if current is not None and current in tunnel.granted and current in tunnel.viewers:
                # Another link of this person's already holds it; this one
                # takes it with its first input (module docstring).
                return
            self._set_controller(tunnel, viewer.conn_id)
            event = "control_taken"
        else:
            # swarm-api ends control per person: every link of theirs lets go.
            mine = {c for c, v in tunnel.viewers.items() if v.email == viewer.email}
            if not self._ungrant(tunnel, mine | {viewer.conn_id}):
                return
            if tunnel.controller is not None:
                return  # handed to another granted link: control goes on
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
        if tell_gate and not tunnel.closed:
            tunnel.outbox.put(True, encode_frame(OP_CLOSE, viewer.conn_id))
        # The gate revokes control when the connection closes
        # (`ControlState.connection_closed`); the relay forgets it too, and a
        # granted link of the same person left open takes over.
        if tunnel.closed:
            tunnel.granted = [c for c in tunnel.granted if c != viewer.conn_id]
            if tunnel.controller == viewer.conn_id:
                tunnel.controller = None
        else:
            self._ungrant(tunnel, {viewer.conn_id})
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

    async def device_request(self, raw: str) -> str:
        """swarm-api's `POST /internal/device`: verified and spent like a
        ticket, matched to its session's tunnel ticket, then sent to the gate
        as a `DEVICE` frame. The session id, or `Refused` -- for a session with
        no tunnel here too, since the gate is the only thing that can apply it."""
        now = self._clock()
        request = verify_device(self._key, raw, now=now)
        self._ledger.spend(request, now)
        if self.is_closed(request.session_id):
            raise Refused("session_closed")
        tunnel = self._tunnels.get(request.session_id)
        if tunnel is None or tunnel.closed:
            raise Refused("no_tunnel")
        _match(request, tunnel.session)
        tunnel.outbox.put(True, encode_frame(OP_DEVICE, "", request.payload()))
        return request.session_id

    async def control_request(self, raw: str) -> tuple[str, str, bool]:
        """swarm-api's `POST /internal/control` (module docstring): verified
        and spent like a ticket and matched to its session's tunnel ticket.
        (session id, action, whether the gate was sent a `CONTROL` frame), or
        `Refused` -- for a session with no tunnel here too."""
        now = self._clock()
        request = verify_control(self._key, raw, now=now)
        self._ledger.spend(request, now)
        if self.is_closed(request.session_id):
            raise Refused("session_closed")
        tunnel = self._tunnels.get(request.session_id)
        if tunnel is None or tunnel.closed:
            raise Refused("no_tunnel")
        _match(request, tunnel.session)
        if request.action == "renew":
            current = tunnel.controller
            link = tunnel.viewers.get(current) if current is not None else None
            if link is None or link.email != request.email:
                return request.session_id, request.action, False
            # The same controller again: the gate renews its control lease.
            tunnel.outbox.put(True, encode_frame(OP_CONTROL, current))
            return request.session_id, request.action, True
        mine = {c for c, v in tunnel.viewers.items() if v.email == request.email}
        held = self._ungrant(tunnel, mine)
        if tunnel.controller is not None:
            # Another person's granted link holds it now (`_ungrant`).
            return request.session_id, request.action, held
        # Nobody is in control: tell the gate even when the relay had already
        # forgotten the controller, because the gate had not (module docstring).
        tunnel.outbox.put(True, encode_frame(OP_CONTROL, "", request.cause.encode("ascii")))
        log.info("live relay: session=%s control revoked by swarm-api: %s",
                 tunnel.session.session_id, request.cause)
        if held and tunnel.recording is not None:
            await tunnel.recording.action({"kind": "control", "event": "control_returned"})
        return request.session_id, request.action, True

    async def flush(self) -> None:
        """Wait until every open connection's queue has been written. For tests
        and for an orderly shutdown."""
        for _ in range(3):
            for tunnel in list(self._tunnels.values()):
                await tunnel.outbox.join()
                for viewer in list(tunnel.viewers.values()):
                    await viewer.outbox.join()
            await asyncio.sleep(0)

    async def restarting(self) -> int:
        """The relay is going (SIGTERM, a roll): tell every gate and viewer to
        reconnect now, with CLOSE_RESTARTING, and refuse every new connection
        the same way. Each tunnel's queue is drained first and its recording's
        last segment written. The number of connections told.

        WHY THE RELAY SAYS IT, AND NOT ONLY UVICORN. uvicorn also closes every
        socket with 1012 on SIGTERM, but by cutting it: what was queued to a
        viewer is lost, the recorder's last segment is not written, and a gate
        or viewer that dials in during the drain is admitted to a process about
        to exit. RELAY-ROLL, 2026-10-13: 40 restarts in ~48 h, each one this."""
        self._restarting = True
        tunnels = list(self._tunnels.values())
        told = sum(1 + len(t.viewers) for t in tunnels)
        # Together: each drain may wait 2 s, and the pod's grace is 30 s.
        await asyncio.gather(*(self._end_tunnel(t, CLOSE_RESTARTING, "relay_restarting") for t in tunnels))
        if told:
            log.info("live relay: restarting; %d connection(s) told to reconnect", told)
        return told

    async def shutdown(self) -> None:
        await self.restarting()
