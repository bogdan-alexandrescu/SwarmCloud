"""The live browser, swarm-api's half (docs/design/live-browser.md §3.3, §4.2, §5.1-§5.2, §5.8, §5.10).

Lane LB-L9. Read the design before changing anything here. In short, swarm-api
is the ONE place that decides who may see and drive an agent's browser:

  * `open_session` is §3.3 step 1. The WORKER, proving itself with the attempt
    key under its own purpose (`swarm-live-session/v1`, §5.4.1), asks for a
    TUNNEL ticket. One transaction checks the fence (invariant 5: RUNNING under
    this lease and generation, no cancel) and writes `live_sessions/{id}`; the
    ticket is minted after it, valid 60 seconds, and never stored.
  * `viewer_ticket` is §5.2 step 3: a member of the task's tenant gets a
    one-person, one-session, 30-second VIEWER ticket in a response BODY. The
    console sends it as the first message of the socket; the URL carries only
    the session id (§5.2, "Nothing secret travels in a URL").
  * `take`, `renew`, `return_control` and `open_to_members` are §4.2 and §5.1:
    one controller at a time, preemption by the task's owner only, and every
    change written in ONE transaction together with its `live_audit` entry
    (§5.8) and its task event (request LB-B).
  * `admit` and `recheck` are the socket's checks (§5.2): the identity, the
    tenant and the ticket on connect, and membership again every
    `live_membership_recheck_seconds` while it stays open.

THE TICKET. Lane L7's relay verifies what this module mints, against the same
`swarm-live-key` (held by swarm-api and swarm-live only, created by
`scripts/create-secrets.sh`, never a Terraform secret version). The RELAY'S
format (`swarm_live.rendezvous.mint_ticket`) is the one used, restated here,
and tests/unit/control_plane/test_live_relay_wire.py holds the two together:

    <b64url(payload)>.<b64url(HMAC-SHA256(key, "swarm-live-ticket/v1\\0" + b64url(payload)))>

`payload` is canonical JSON (sorted keys, no spaces) of exactly `session_id`,
`tenant_id`, `task_id`, `attempt_id`, `generation`, `role`, `exp` (unix
seconds, at most 60 s ahead), `nonce` (random, the one-use id the relay
remembers) and `email` (the person's; empty for the gate's tunnel). The key is
the UTF-8 bytes of the secret's value. Base64url is unpadded. The relay
refuses a ticket used twice, expired, or naming another session, tenant or
generation (§5.11), and it trusts NOTHING but the ticket: it has no Firestore
access (owner decision 2026-10-11 (a)).

Roles `tunnel` and `viewer` open a connection. `control` and `release` are
MESSAGES swarm-api sends on a viewer connection it already holds open, when
this person takes or loses control (§4.2): the relay grants the gate's input
to a viewer only on a ticket.

CLOSING. When a session closes, or a stale generation is seen, swarm-api POSTs
a close request to the relay's `/internal/close` (`mint_close`: the same shape
under its own domain, `session_id`, `exp`, `nonce`), and the relay drops every
connection of that session (§5.11, invariant 5).

WHO MAY CONTROL (§5.1, Q1, Q6). The owner is `handoff.control_owner`: the
submitter, or for a service account's task its workflow's or schedule's human
owner. With no human owner any member may control. While the agent is asking
for help (`Task.human_wait` set) the owner may open control to every member of
the tenant; a take the agent did not ask for is the owner's alone (Q6).

Nothing here logs, stores or serves a key, a signature or a ticket's value;
the audit records counts, never content (§5.3.3).
"""

from __future__ import annotations

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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field

from swarm_common.admission import _snapshot
from swarm_common.models import Task, TaskEvent, new_id, utcnow
from swarm_common.profiles import RUNNER_PROFILES, Backend
from swarm_common.states import EventType, ParkReason, TaskState

from .childkey import AttemptTuple, verify_proof
from .children import ChildService, _AttemptFields, _lease_is_live, parse_body
from .codec import event_to_firestore
from .errors import ApiError, NotFound, ValidationFailed
from .handoff import LIVE_AUDIT, control_owner, resolution_writes
from .objects import ObjectAbsent, ObjectUnreadable
from .store import ATTEMPTS, EVENTS, LEASES, TASKS, Store

log = logging.getLogger(__name__)

#: swarm-api's own collection (§8.4): one document per attempt that opened a
#: live path. Not a frozen model, as `admin_roles` is not.
LIVE_SESSIONS = "live_sessions"

#: The worker's signing purpose for the tunnel-ticket route (§5.4.1). Lane L4
#: restates it; a hand-off or child signature never verifies as this one.
LIVE_SESSION_PURPOSE = "swarm-live-session/v1"
#: The ticket MAC's domain: the relay's (`swarm_live.rendezvous.TICKET_DOMAIN`).
TICKET_PURPOSE = b"swarm-live-ticket/v1\0"
#: A close request's MAC domain (`swarm_live.rendezvous.CLOSE_DOMAIN`), so a
#: ticket is never a close request and a close request never a ticket.
CLOSE_PURPOSE = b"swarm-live-close/v1\0"
ROLE_TUNNEL = "tunnel"
ROLE_VIEWER = "viewer"
ROLE_CONTROL = "control"
ROLE_RELEASE = "release"
#: The longest life the relay accepts for each role's ticket
#: (`swarm_live.rendezvous.TICKET_TTL_SECONDS`). A setting above it is clamped,
#: since the relay would refuse the ticket anyway (decision (a): exp at most
#: 60 s ahead).
TICKET_MAX_SECONDS = {ROLE_TUNNEL: 60, ROLE_VIEWER: 30, ROLE_CONTROL: 30, ROLE_RELEASE: 30}
#: A close request's life (`swarm_live.rendezvous.CLOSE_TTL_SECONDS`).
CLOSE_SECONDS = 60
#: The relay's field set; a ticket with any other is refused there.
TICKET_FIELDS = frozenset({"session_id", "tenant_id", "task_id", "attempt_id", "generation",
                           "role", "exp", "nonce", "email"})
#: A ticket is short; anything longer is refused before it is decoded.
MAX_TICKET_CHARS = 2048
#: How long swarm-api waits for the relay to answer a close request.
RELAY_CLOSE_TIMEOUT_SECONDS = 5.0

#: Why control ended without a Return press (§4.2, §5.8). `attempt_ended` is
#: this lane's addition: the attempt stopped holding its lease for a reason
#: that is neither a cancel nor an expired hold (a crash, a reclaim).
END_CAUSES = (
    "idle", "renew_lapsed", "preempted", "membership_lost", "cancelled", "hold_expired",
    "attempt_ended",
)

#: RFB client-to-server message types the audit counts (RFC 6143 §7.5): a key
#: and a pointer event. Counted by their first byte, never read further.
RFB_KEY_EVENT = 4
RFB_POINTER_EVENT = 5


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class LiveUnavailable(ApiError):
    status_code = 503
    code = "live_unavailable"


class LiveUnauthenticated(ApiError):
    status_code = 403
    code = "live_unauthenticated"


class LiveUnproven(ApiError):
    status_code = 403
    code = "live_unproven"


class LiveFenced(ApiError):
    status_code = 409
    code = "live_fenced"


class LiveNotAvailable(ApiError):
    """The task's profile is not a live-browser profile, or it is not on GKE (§8.1)."""

    status_code = 409
    code = "live_not_available"


class LiveSessionClosed(ApiError):
    status_code = 409
    code = "live_session_closed"


class ControlNotEligible(ApiError):
    status_code = 403
    code = "control_not_eligible"


class ControlHeld(ApiError):
    status_code = 409
    code = "control_held"


class NotController(ApiError):
    status_code = 409
    code = "not_controller"


class TicketRefused(Exception):
    """A ticket that does not verify. Its message never contains the ticket."""


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _ticket_mac(key: str, payload: str, purpose: bytes = TICKET_PURPOSE) -> bytes:
    return hmac.new(key.encode("utf-8"), purpose + payload.encode("ascii"),
                    hashlib.sha256).digest()


def _signed(key: str, body: Mapping[str, Any], purpose: bytes) -> str:
    if not key:
        raise LiveUnavailable("this deployment has no swarm-live-key")
    payload = _b64(json.dumps(dict(body), sort_keys=True, separators=(",", ":")).encode())
    return f"{payload}.{_b64(_ticket_mac(key, payload, purpose))}"


def mint_ticket(key: str, claims: Mapping[str, Any]) -> str:
    """The ticket for `claims`, in the shape the module docstring fixes: a
    fresh `nonce` and an empty `email` are filled in when absent, and any
    field the relay does not know is refused here rather than there."""
    body = {"email": "", **dict(claims)}
    body.setdefault("nonce", secrets.token_hex(16))
    if set(body) != TICKET_FIELDS:
        raise ValueError(f"a live ticket has exactly the fields {sorted(TICKET_FIELDS)}")
    return _signed(key, body, TICKET_PURPOSE)


def mint_close(key: str, session_id: str, *, now: float) -> str:
    """A request to the relay to drop every connection of `session_id`."""
    return _signed(key, {"session_id": session_id, "exp": int(now) + CLOSE_SECONDS,
                         "nonce": secrets.token_hex(16)}, CLOSE_PURPOSE)


def ticket_seconds(role: str, configured: Any) -> int:
    """`configured`, never longer than the relay accepts for `role`."""
    return max(1, min(int(configured), TICKET_MAX_SECONDS[role]))


def verify_ticket(keys: Sequence[str], ticket: Any, *, now: float) -> dict[str, Any]:
    """The claims of `ticket` when any of `keys` made it and it has not expired."""
    if not isinstance(ticket, str) or not ticket or len(ticket) > MAX_TICKET_CHARS:
        raise TicketRefused("no ticket")
    parts = ticket.split(".")
    if len(parts) != 2:
        raise TicketRefused("not a live ticket")
    payload, mac = parts
    try:
        presented = _unb64(mac)
    except (binascii.Error, ValueError):
        raise TicketRefused("malformed") from None
    if not any(
        key and hmac.compare_digest(_ticket_mac(key, payload), presented) for key in keys
    ):
        raise TicketRefused("the ticket did not verify")
    try:
        claims = json.loads(_unb64(payload))
    except (binascii.Error, ValueError):
        raise TicketRefused("malformed") from None
    if not isinstance(claims, dict) or set(claims) != TICKET_FIELDS:
        raise TicketRefused("malformed")
    exp = claims.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool) or exp <= now:
        raise TicketRefused("expired")
    if claims.get("role") not in (ROLE_TUNNEL, ROLE_VIEWER):
        raise TicketRefused("unknown role")
    return claims


def live_session_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs to ask for a tunnel ticket: the child path's shape
    under its own purpose, so neither signature verifies as the other."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{LIVE_SESSION_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode()


def socket_path(session_id: str) -> str:
    """Where the console opens a session's websocket (decision (b)): through
    IAP to swarm-api, which proxies it to the relay. The session id is not
    secret; the ticket is the first message, never part of this."""
    return f"/v1/live/{session_id}/stream"


def session_id_for(attempt_id: str) -> str:
    """One session per attempt, so a re-ticket after a dropped tunnel finds it."""
    return f"live_{attempt_id}"


# --------------------------------------------------------------------------
# The relay's close route (owner decision 2026-10-11 (a))
# --------------------------------------------------------------------------


class RelayCloser(Protocol):
    def close_session(self, session_id: str) -> None: ...


def relay_http_url(ws_url: str, path: str) -> str:
    """`SWARM_LIVE_URL` (`ws://<internal address>:8080`) as the http URL of
    one of the relay's routes on the same port."""
    base = ws_url.strip().rstrip("/")
    if base.startswith("ws://"):
        base = "http://" + base[len("ws://"):]
    elif base.startswith("wss://"):
        base = "https://" + base[len("wss://"):]
    return base + path


class HttpRelayCloser:
    """POSTs a close request to the relay's `/internal/close`, plain HTTP on the
    internal load balancer as the viewer sockets are (decision (b)).

    Never raises. A close the relay did not receive is logged; the relay still
    refuses every ticket for the session once swarm-api stops minting them,
    and the tunnel ends with the stale worker's pod. What is logged is the
    session id and the status, never the request (it is MAC'd).
    """

    def __init__(self, url: str, key: str, *, clock: Callable[[], float] = time.time,
                 timeout: float = RELAY_CLOSE_TIMEOUT_SECONDS) -> None:
        self._url = relay_http_url(url, "/internal/close")
        self._key = key
        self._clock = clock
        self._timeout = timeout

    def close_session(self, session_id: str) -> None:
        import urllib.error
        import urllib.request

        body = mint_close(self._key, session_id, now=self._clock()).encode("ascii")
        request = urllib.request.Request(self._url, data=body, method="POST",
                                         headers={"Content-Type": "text/plain"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - our own internal URL
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (OSError, ValueError) as exc:
            log.warning("live relay close for session=%s did not reach the relay (%s)",
                        session_id, type(exc).__name__)
            return
        if status != 200:
            log.warning("live relay refused the close for session=%s (HTTP %s)", session_id, status)


def relay_closer_for(settings: Any) -> RelayCloser | None:
    """The deployment's closer: none without a relay address or a key."""
    url = getattr(settings, "live_relay_url", "") or ""
    key = getattr(settings, "live_key", "") or ""
    return HttpRelayCloser(url, key) if url and key else None


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------


def fresh_membership(authenticator: Any, email: str, tenant: Any) -> bool:
    """Whether `email` belongs to `tenant` NOW, past any cached answer (§5.2:
    "a taking of control always runs a fresh check").

    `Authenticator.is_tenant_member` asks per registered group, as every
    request's tenant resolution does, but through the resolver's 120-second
    cache. The cache entry for this one (person, group) is dropped first: by
    the resolver's own `forget` when it has one, or, for `CloudIdentityGroups`,
    from its membership cache under its lock. A personal tenant is its own
    address and needs no directory.
    """
    principal = (getattr(tenant, "principal", "") or "").strip().lower()
    who = (email or "").strip().lower()
    groups = getattr(authenticator, "_groups", None)
    if principal and getattr(tenant, "kind", "") == "group" and groups is not None:
        forget = getattr(groups, "forget", None)
        if callable(forget):
            forget(who, principal)
        else:
            cache = getattr(groups, "_memberships", None)
            lock = getattr(groups, "_lock", None)
            if isinstance(cache, dict) and lock is not None:
                with lock:
                    cache.pop((who, principal), None)
    return bool(authenticator.is_tenant_member(who, tenant))


# --------------------------------------------------------------------------
# Bodies
# --------------------------------------------------------------------------


class LiveSessionOpen(_AttemptFields):
    task_id: str = Field(min_length=1, max_length=128)


#: The longest domain a take's `confirmed_mismatch` may name (RFC 1035: 253).
MAX_DOMAIN_CHARS = 253


class ConfirmedMismatch(BaseModel):
    """§5.7 item 2: the person was asked twice because the top frame's
    registrable domain was not the one the hand-off expected, and confirmed
    past it. Both names go into the `control_taken` audit entry."""

    model_config = ConfigDict(extra="forbid")

    expected: str = Field(min_length=1, max_length=MAX_DOMAIN_CHARS)
    actual: str = Field(min_length=1, max_length=MAX_DOMAIN_CHARS)


class TakeControl(BaseModel):
    """The body of `POST /v1/tasks/{task_id}/live/control`, as the console
    sends it (`takeLiveControl` in apps/swarm-ui/src/api.ts)."""

    model_config = ConfigDict(extra="forbid")

    preempt: bool = False
    confirmed_mismatch: ConfirmedMismatch | None = None


# --------------------------------------------------------------------------
# What the console is served (the wire the Browser tab parses)
# --------------------------------------------------------------------------
#
# LANE LB-SEAM (#1030). Lanes L9 (this module) and L11 (the console) were
# built in parallel against different shapes: this route served
# `{available, session_id, state, close_cause, you: {may_take, ...}}` while
# the console reads `{live_browser, session: {..., you: {can_control, ...}}}`,
# so a working live session drew NO Browser tab. The design names the fields
# and not their JSON, so the console's types (`LiveRead` in
# apps/swarm-ui/src/api.ts) are the wire, and these models are the API's
# statement of it. Every live route's `response_model` is one of them, so a
# key this module forgets or renames fails the route itself, and
# tests/fixtures/live/*.json is validated by these models in pytest AND parsed
# by the console's own loaders in vitest -- the shape seam the path-only
# check in test_runtimes_screen.py never was.


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiveOrigin(_Wire):
    """The top frame's origin as the GATE read it over CDP (§5.7)."""

    origin: str
    host: str
    display_host: str | None = None
    registrable_domain: str
    scheme: str
    ip_literal: bool | None = None
    cert_error: bool | None = None


class LiveDevice(_Wire):
    """The device the gate has applied (§6.1)."""

    preset: str
    orientation: Literal["portrait", "landscape"]
    touch: bool


class LivePreset(_Wire):
    """One of the gate's fixed device presets (§6.1)."""

    name: str
    width: int
    height: int
    scale: float
    mobile: bool


class LiveController(_Wire):
    """The person in control (§4.2). `renew_by` is when control lapses
    unless renewed; the console does not read it."""

    email: str
    since: str | None
    renew_by: str | None = None


class LiveHumanWait(_Wire):
    """`Task.human_wait` as a viewer sees it (§4.1 step 3): no request id,
    no generation."""

    reason: str | None
    expect: str | None
    requested_at: str | None
    deadline: str | None


class LiveYou(_Wire):
    """What THIS caller may do (§5.1). The server decides; the console draws."""

    email: str
    #: May take control now: the owner, or any member once control is opened
    #: while the agent asks for help (§5.1, Q1, Q6).
    can_control: bool
    #: May take control from someone else who holds it: the owner only.
    can_preempt: bool
    #: May open control to every member: the owner only.
    can_open: bool


class LiveSessionView(_Wire):
    session_id: str
    state: Literal["open", "closed"]
    closed_reason: str | None
    #: NOT REPORTED SERVER-SIDE YET: the gate reads the origin over CDP but
    #: nothing carries it to swarm-api (no relay message, no ctl report, no
    #: field on `live_sessions`), so this is always null rather than invented.
    origin: LiveOrigin | None
    human_wait: LiveHumanWait | None
    controller: LiveController | None
    open_to_members: bool
    #: NOT REPORTED SERVER-SIDE YET, as `origin`: the gate applies the device
    #: (`agent_worker/livegate/device.py`) and nothing reports which one.
    device: LiveDevice | None
    presets: list[LivePreset]
    you: LiveYou


class LiveRead(_Wire):
    """`GET /v1/tasks/{task_id}/live/session`, and the answer of every control
    route. `live_browser` is the task's profile's catalogue property
    (`RunnerProfile.live_browser`, contract request 71), true for a
    live-browser task before, during and after its session; `session` is null
    until the gate's tunnel has opened one for the task's current attempt."""

    live_browser: bool
    session: LiveSessionView | None


class LiveTicketView(_Wire):
    """`POST /v1/tasks/{task_id}/live/ticket` (§5.2 step 3)."""

    ticket: str
    session_id: str
    expires_in: int
    socket_path: str


#: The gate's fixed device list (§6.1), as the console's switcher lists it.
#: A RESTATEMENT of `agent_worker.livegate.presets.PRESETS` -- swarm-api does
#: not import the worker -- held equal to it by
#: tests/unit/control_plane/test_live_wire_shapes.py. It is a catalogue, not a
#: reading: which preset is APPLIED is `device`, which is not reported yet.
LIVE_PRESETS: tuple[dict[str, Any], ...] = (
    {"name": "desktop", "width": 1440, "height": 900, "scale": 1.0, "mobile": False},
    {"name": "iphone-15", "width": 393, "height": 852, "scale": 3.0, "mobile": True},
    {"name": "pixel-8", "width": 412, "height": 915, "scale": 2.625, "mobile": True},
    {"name": "iphone-se", "width": 375, "height": 667, "scale": 2.0, "mobile": True},
    {"name": "ipad-7", "width": 810, "height": 1080, "scale": 2.0, "mobile": True},
)


def profile_is_live(runner_profile: Any) -> bool:
    """`RunnerProfile.live_browser` for this profile name: the catalogue's
    word, never inferred from the name (contract request 71, invariant 10)."""
    profile = RUNNER_PROFILES.get(str(runner_profile or ""))
    return bool(profile is not None and getattr(profile, "live_browser", False))


# --------------------------------------------------------------------------
# One transaction's worth of changes
# --------------------------------------------------------------------------


@dataclass
class _Plan:
    """Everything one transaction writes, flushed once at its end, so a
    control change and its audit entry cannot be separated (§5.8)."""

    db: Any
    task: dict[str, Any]
    session: dict[str, Any] | None
    session_ref: Any
    now: datetime
    session_update: dict[str, Any] = field(default_factory=dict)
    task_update: dict[str, Any] = field(default_factory=dict)
    audits: list[tuple[str, str | None, dict[str, Any]]] = field(default_factory=list)
    events: list[tuple[EventType, dict[str, Any]]] = field(default_factory=list)
    resolve_by: str | None = None
    resolve: bool = False

    # -- reads of the planned state ---------------------------------------

    @property
    def state(self) -> dict[str, Any]:
        return {**(self.session or {}), **self.session_update}

    @property
    def controller(self) -> dict[str, Any] | None:
        current = self.state.get("controller")
        return current if isinstance(current, dict) else None

    @property
    def wait(self) -> dict[str, Any] | None:
        wait = self.task_update.get("human_wait", self.task.get("human_wait"))
        return wait if isinstance(wait, dict) else None

    # -- writes -------------------------------------------------------------

    def audit(self, entry: str, email: str | None, detail: dict[str, Any]) -> None:
        self.audits.append((entry, email, detail))

    def set_wait_controller(self, value: dict[str, Any] | None, deadline: datetime | None) -> None:
        wait = self.wait
        if wait is None:
            return
        wait = dict(wait)
        wait["controller"] = value
        if deadline is not None:
            wait["deadline"] = deadline
        self.task_update["human_wait"] = wait

    def end_control(self, cause: str, *, keys: int = 0, pointers: int = 0) -> None:
        """Control ends: `control_returned` for the Return press, else
        `control_ended` with its cause. One episode, one entry, with counts."""
        current = self.controller
        if current is None:
            return
        since = current.get("since")
        held = (self.now - since).total_seconds() if isinstance(since, datetime) else None
        detail = {
            "cause": cause,
            "episode_id": current.get("episode_id"),
            "keys": int(current.get("keys") or 0) + keys,
            "pointer_events": int(current.get("pointers") or 0) + pointers,
            "held_seconds": int(held) if held is not None else None,
        }
        self.session_update["controller"] = None
        self.audit("control_returned" if cause == "returned" else "control_ended",
                   current.get("email"), detail)
        self.events.append((EventType.HUMAN_CONTROL_RETURNED,
                            {**detail, "email": current.get("email")}))
        self.set_wait_controller(None, None)

    def close(self, cause: str) -> None:
        control_cause = cause if cause in END_CAUSES else "attempt_ended"
        self.end_control(control_cause)
        self.session_update.update(
            {"state": "closed", "closed_at": self.now, "close_cause": cause}
        )
        self.audit("session_closed", None, {"cause": cause})

    def flush(self, txn: Any) -> None:
        session = self.state
        task_ref = self.db.collection(TASKS).document(self.task["id"])
        if self.session_update:
            txn.update(self.session_ref, {**self.session_update, "updated_at": self.now})
        if self.resolve:
            resolution_writes(txn, self.db, self.task, outcome="returned", now=self.now,
                              by=self.resolve_by)
        elif self.task_update:
            txn.update(task_ref, {**self.task_update, "updated_at": self.now})
        for event_type, detail in self.events:
            event = TaskEvent(
                event_id=new_id("ev"),
                task_id=self.task["id"],
                tenant_id=self.task["tenant_id"],
                type=event_type,
                at=self.now,
                detail={**detail, "session_id": session.get("id"),
                        "attempt_id": session.get("attempt_id"),
                        "generation": session.get("generation")},
            )
            txn.set(task_ref.collection(EVENTS).document(event.event_id),
                    event_to_firestore(event))
        for entry, email, detail in self.audits:
            write_audit(txn, self.db, session, entry, email=email, detail=detail, now=self.now)


def write_audit(
    writer: Any, db: Any, session: Mapping[str, Any], entry: str, *, email: str | None,
    detail: Mapping[str, Any], now: datetime,
) -> None:
    """One `live_audit` entry, in the shape `handoff._audit` writes, through
    `writer` (the caller's transaction, or the collection for an entry that
    records no other change: a viewer joining or leaving)."""
    entry_id = new_id("la")
    doc = {
        "id": entry_id,
        "entry": entry,
        "task_id": session.get("task_id"),
        "tenant_id": session.get("tenant_id"),
        "session_id": session.get("id"),
        "attempt_id": session.get("attempt_id"),
        "generation": session.get("generation"),
        "email": email,
        "at": now,
        "detail": dict(detail),
    }
    ref = db.collection(LIVE_AUDIT).document(entry_id)
    if writer is None:
        ref.set(doc)
    else:
        writer.set(ref, doc)


# --------------------------------------------------------------------------
# What a person is told
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    """A socket that passed §5.2: who, which tenant, which session."""

    email: str
    tenant_id: str
    task_id: str
    session_id: str
    attempt_id: str
    generation: int
    ticket: str


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


class LiveService:
    """The live routes' logic. Built per request from the app context."""

    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        store: Store,
        authenticator: Any,
        children: ChildService | None = None,
        relay: RelayCloser | None = None,
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._db = db
        self._store = store
        self._authenticator = authenticator
        self._children = children
        self._relay = relay
        self._now = now
        self._clock = clock

    # -- settings ------------------------------------------------------------

    def _s(self, name: str, default: Any) -> Any:
        return getattr(self._settings, name, default)

    @property
    def _keys(self) -> tuple[str, ...]:
        return tuple(k for k in (self._s("live_key", ""), self._s("live_key_previous", "")) if k)

    def _require_key(self) -> str:
        key = self._s("live_key", "")
        if not key:
            raise LiveUnavailable(
                "this deployment has no swarm-live-key, so it offers no live browser"
            )
        return key

    # -- §3.3 step 1: the worker's tunnel ticket ---------------------------

    def _prove(self, authorization: str | None, attempt: AttemptTuple, *, path: str,
               body: bytes, proof: str | None, timestamp: str | None) -> None:
        """§5.4.1, as the hand-off routes prove the worker. Every failure of
        the signature is the same 403."""
        children = self._children
        if children is None or not children._keys.configured:
            raise LiveUnavailable(
                "this deployment has no swarm-child-key, so no attempt key can be proven"
            )
        try:
            children.authenticate_worker(authorization, attempt.tenant_id)
        except ApiError as exc:
            if exc.status_code == 429:
                raise
            raise LiveUnauthenticated(
                "only this tenant's worker service account may call this route"
            ) from None
        unproven = LiveUnproven("the attempt proof did not verify for this attempt")
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        skew = int(self._s("child_proof_skew_seconds", 120))
        if abs(self._clock() - int(timestamp)) > skew:
            raise unproven
        registration = children._registration(attempt)
        if registration is None:
            raise unproven
        if not verify_proof(registration["public_key"], proof,
                            live_session_message("POST", path, body, timestamp)):
            raise unproven

    def open_session(
        self, authorization: str | None, attempt_id: str, raw: bytes, *, path: str,
        proof: str | None, timestamp: str | None,
    ) -> tuple[dict[str, Any], bool]:
        key = self._require_key()
        if len(raw) > 4096:
            raise ValidationFailed("a live-session request is at most 4096 bytes")
        body: LiveSessionOpen = parse_body(LiveSessionOpen, raw)
        if body.attempt_id != attempt_id:
            raise ValidationFailed("the body's attempt_id is not the path's")
        attempt = AttemptTuple(body.tenant_id, body.task_id, body.attempt_id, body.lease_id,
                               body.generation)
        self._prove(authorization, attempt, path=path, body=raw, proof=proof,
                    timestamp=timestamp)

        task_ref = self._db.collection(TASKS).document(attempt.task_id)
        lease_ref = self._db.collection(LEASES).document(attempt.lease_id)
        attempt_ref = self._db.collection(ATTEMPTS).document(attempt.attempt_id)
        session_id = session_id_for(attempt.attempt_id)
        session_ref = self._db.collection(LIVE_SESSIONS).document(session_id)
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            attempt_snap = _snapshot(txn.get(attempt_ref))
            session_snap = _snapshot(txn.get(session_ref))
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            if (
                task is None
                or task.get("state") != TaskState.RUNNING.value
                or task.get("cancel_requested")
                or not _lease_is_live(task, lease, attempt, now)
            ):
                raise LiveFenced(
                    "the task is not RUNNING under this attempt's live lease, or a cancel "
                    "was requested; no live session was opened"
                )
            # §8.1: a live-browser profile, on GKE, in this attempt's record.
            backend = (attempt_snap.to_dict() or {}).get("backend") if attempt_snap.exists else None
            if (
                not profile_is_live(task.get("runner_profile"))
                or backend != Backend.GKE_AUTOPILOT.value
            ):
                raise LiveNotAvailable(
                    "this task's profile is not a live-browser profile running on GKE "
                    "Autopilot, so it has no live browser"
                )
            if session_snap.exists:
                existing = session_snap.to_dict() or {}
                if existing.get("state") != "open":
                    raise LiveSessionClosed(
                        "this attempt's live session was closed; its live path has ended"
                    )
                txn.update(session_ref, {"tunnel_tickets": int(existing.get("tunnel_tickets")
                                                               or 0) + 1, "updated_at": now})
                return False
            session = {
                "id": session_id,
                "tenant_id": attempt.tenant_id,
                "task_id": attempt.task_id,
                "attempt_id": attempt.attempt_id,
                "lease_id": attempt.lease_id,
                "generation": attempt.generation,
                "state": "open",
                "opened_at": now,
                "updated_at": now,
                "closed_at": None,
                "close_cause": None,
                "controller": None,
                "open_to_members": False,
                "opened_to_members_by": None,
                "tunnel_tickets": 1,
            }
            txn.set(session_ref, session)
            write_audit(txn, self._db, session, "session_opened", email=None,
                        detail={"profile": task.get("runner_profile")}, now=now)
            return True

        try:
            created = _apply(self._db.transaction())
        except LiveFenced:
            # A stale generation, seen: its session (if it ever opened one)
            # closes, and the relay drops it (invariant 5, §5.11).
            self._close_stale(attempt.tenant_id, attempt.task_id, session_id)
            raise
        if created:
            # A new attempt's session makes every earlier one of this task
            # stale; nobody may be watching them, so nothing else would close
            # them at the relay.
            self._close_others(attempt.tenant_id, attempt.task_id, session_id)
        seconds = ticket_seconds(ROLE_TUNNEL, self._s("live_tunnel_ticket_seconds", 60))
        exp = int(self._clock()) + seconds
        ticket = mint_ticket(key, {
            "session_id": session_id,
            "tenant_id": attempt.tenant_id,
            "task_id": attempt.task_id,
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
            "role": ROLE_TUNNEL,
            "exp": exp,
        })
        return {"session_id": session_id, "ticket": ticket, "expires_in": seconds}, created

    # -- telling the relay (decision (a)) ----------------------------------

    def close_relay(self, session_id: str) -> None:
        """Ask the relay to drop every connection of `session_id`. Never raises."""
        if self._relay is None:
            return
        try:
            self._relay.close_session(session_id)
        except Exception as exc:  # noqa: BLE001 - a close is best effort; the caller's answer stands
            log.warning("live relay close for session=%s failed (%s)", session_id, type(exc).__name__)

    def _close_stale(self, tenant_id: str, task_id: str, session_id: str) -> None:
        """Settle `session_id` -- which closes it, with its audit, when its
        attempt no longer holds the lease -- and tell the relay either way:
        `_transact` already has when it closed it just now."""
        try:
            plan, _ = self._transact(tenant_id, task_id, None, session_id=session_id)
        except NotFound:
            plan = None
        if plan is None or plan.session_update.get("state") != "closed":
            self.close_relay(session_id)

    def _close_others(self, tenant_id: str, task_id: str, session_id: str) -> None:
        query = self._db.collection(LIVE_SESSIONS).where(
            filter=FieldFilter("task_id", "==", task_id))
        for snap in query.stream():
            doc = snap.to_dict() or {}
            if (doc.get("id") != session_id and doc.get("tenant_id") == tenant_id
                    and doc.get("state") == "open"):
                self._close_stale(tenant_id, task_id, str(doc.get("id")))

    # -- reading and settling a session -----------------------------------

    def _load(self, get: Callable[[Any], Any], tenant_id: str, task_id: str,
              session_id: str | None) -> tuple[dict, dict | None, Any, dict | None]:
        """The task, the lease the session is judged by, the session's ref and
        the session, through `get` (a transaction's, or a plain read).

        With `session_id`, THAT session (a socket's own) and the lease it was
        opened under, so a session left behind by an earlier attempt is seen
        to have ended. Without, the session of the task's current attempt.
        Another tenant's task is a 404.
        """
        task_snap = _snapshot(get(self._db.collection(TASKS).document(task_id)))
        task = task_snap.to_dict() if task_snap.exists else None
        if task is None or task.get("tenant_id") != tenant_id:
            raise NotFound(f"task {task_id!r} not found")

        def lease_of(lease_id: Any) -> dict | None:
            if not lease_id:
                return None
            snap = _snapshot(get(self._db.collection(LEASES).document(str(lease_id))))
            return snap.to_dict() if snap.exists else None

        if session_id is None:
            lease = lease_of(task.get("current_lease_id"))
            attempt_id = (lease or {}).get("attempt_id")
            if not attempt_id:
                return task, lease, None, None
            session_id = session_id_for(str(attempt_id))
        else:
            lease = None
        ref = self._db.collection(LIVE_SESSIONS).document(session_id)
        snap = _snapshot(get(ref))
        session = snap.to_dict() if snap.exists else None
        if session is not None and (
            session.get("tenant_id") != tenant_id or session.get("task_id") != task_id
        ):
            session = None
        if session is not None and lease is None:
            lease = lease_of(session.get("lease_id"))
        return task, lease, ref, session

    def _closing_cause(self, task: Mapping[str, Any], lease: Mapping[str, Any] | None,
                       session: Mapping[str, Any], now: datetime) -> str | None:
        """Why an open session must close now, or None (invariant 5)."""
        if task.get("cancel_requested") or task.get("state") == TaskState.CANCELLED.value:
            return "cancelled"
        if (
            task.get("state") == TaskState.PARKED.value
            and task.get("park_reason") == ParkReason.HUMAN_REQUIRED.value
        ):
            return "hold_expired"
        attempt = AttemptTuple(
            str(session.get("tenant_id")), str(session.get("task_id")),
            str(session.get("attempt_id")), str(session.get("lease_id")),
            int(session.get("generation") or 0),
        )
        if task.get("state") != TaskState.RUNNING.value or not _lease_is_live(
            dict(task), dict(lease) if lease else None, attempt, now
        ):
            return "attempt_ended"
        return None

    def _lapse_cause(self, plan: _Plan) -> str | None:
        """Why the current controller's control has ended by itself (§4.2)."""
        current = plan.controller
        if current is None:
            return None
        renew_by = current.get("renew_by")
        if isinstance(renew_by, datetime) and renew_by < plan.now:
            return "renew_lapsed"
        idle = int(self._s("control_idle_seconds", 300))
        last = current.get("last_input_at") or current.get("since")
        if isinstance(last, datetime) and (plan.now - last).total_seconds() > idle:
            return "idle"
        wait = plan.wait
        deadline = wait.get("deadline") if wait else None
        if isinstance(deadline, datetime) and deadline < plan.now:
            return "hold_expired"
        return None

    def _settle(self, plan: _Plan, lease: Mapping[str, Any] | None) -> None:
        if plan.session is None or plan.state.get("state") != "open":
            return
        cause = self._closing_cause(plan.task, lease, plan.state, plan.now)
        if cause is not None:
            plan.close(cause)
            return
        lapse = self._lapse_cause(plan)
        if lapse is not None:
            plan.end_control(lapse)

    def _transact(self, tenant_id: str, task_id: str,
                  change: Callable[[_Plan], Any] | None,
                  session_id: str | None = None) -> tuple[_Plan, Any]:
        """Read the task, its lease and its session; settle what has lapsed;
        apply `change`; write everything in one transaction.

        `change` is not called on a session that is missing or closed; the
        closing writes are committed and the caller decides what to say.
        An error raised by `change` rolls back everything, settling included.
        """
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[_Plan, Any]:
            task, lease, ref, session = self._load(txn.get, tenant_id, task_id, session_id)
            plan = _Plan(db=self._db, task=task, session=session, session_ref=ref, now=now)
            self._settle(plan, lease)
            result = None
            if change is not None and session is not None and plan.state.get("state") == "open":
                result = change(plan)
            plan.flush(txn)
            return plan, result

        plan, result = _apply(self._db.transaction())
        if (plan.session is not None and plan.session.get("state") == "open"
                and plan.session_update.get("state") == "closed"):
            # Committed: the session is closed, so the relay drops it (§5.11).
            self.close_relay(str(plan.session.get("id")))
        return plan, result

    def _needs_settling(self, tenant_id: str, task_id: str, session_id: str) -> bool:
        """A cheap, non-transactional look: is there anything to settle?"""
        task, lease, ref, session = self._load(lambda r: r.get(), tenant_id, task_id, session_id)
        if session is None or session.get("state") != "open":
            return False
        plan = _Plan(db=self._db, task=task, session=session, session_ref=ref, now=self._now())
        return (self._closing_cause(task, lease, session, plan.now) is not None
                or self._lapse_cause(plan) is not None)

    # -- who may do what (§5.1) ---------------------------------------------

    def _rights(self, task: Task, plan: _Plan, email: str, owner: str | None) -> dict[str, Any]:
        """`LiveYou`: what this caller may do. Every member may watch (§5.1);
        a caller who can see the task is a member, so watching is not a field."""
        email = email.strip().lower()
        is_owner = owner is None or owner == email
        waiting = plan.wait is not None
        opened = bool(plan.state.get("open_to_members"))
        current = plan.controller
        open_ = plan.state.get("state") == "open"
        may_take = open_ and (is_owner or (waiting and opened))
        return {
            "email": email,
            "can_control": bool(may_take),
            "can_preempt": bool(open_ and owner is not None and owner == email and current
                                is not None and current.get("email") != email),
            "can_open": bool(open_ and owner is not None and owner == email),
        }

    def _view(self, task: Task, plan: _Plan | None, email: str, owner: str | None
              ) -> dict[str, Any]:
        """`LiveRead`. `session` is null until a session exists for the
        task's current attempt; a closed one is still served, with its
        `closed_reason`, so the tab can say why the browser went away."""
        live_browser = profile_is_live(getattr(task, "runner_profile", None))
        if plan is None or plan.session is None:
            return {"live_browser": live_browser, "session": None}
        state = plan.state
        current = plan.controller
        wait = plan.wait
        return {
            "live_browser": live_browser,
            "session": {
                "session_id": state.get("id"),
                "state": "open" if state.get("state") == "open" else "closed",
                "closed_reason": state.get("close_cause"),
                "origin": None,
                "human_wait": None if wait is None else {
                    k: _iso(wait.get(k)) for k in ("reason", "expect", "requested_at", "deadline")
                },
                "controller": None if current is None else {
                    "email": current.get("email"),
                    "since": _iso(current.get("since")),
                    "renew_by": _iso(current.get("renew_by")),
                },
                "open_to_members": bool(state.get("open_to_members")),
                "device": None,
                "presets": [dict(p) for p in LIVE_PRESETS],
                "you": self._rights(task, plan, email, owner),
            },
        }

    def _task(self, tenant_id: str, task_id: str, submitted_by: str | None) -> Task:
        # The tenant read first: another tenant's task is a 404 (§5.2 step 2).
        return self._store.get_task(tenant_id, task_id, submitted_by=submitted_by)

    # -- the routes ------------------------------------------------------------

    def view(self, tenant_id: str, task_id: str, *, email: str,
             submitted_by: str | None) -> dict[str, Any]:
        task = self._task(tenant_id, task_id, submitted_by)
        owner = control_owner(task, self._db)
        plan, _ = self._transact(tenant_id, task_id, None)
        return self._view(task, plan, email, owner)

    def viewer_ticket(self, tenant_id: str, task_id: str, *, email: str,
                      submitted_by: str | None) -> dict[str, Any]:
        key = self._require_key()
        self._task(tenant_id, task_id, submitted_by)
        plan, _ = self._transact(tenant_id, task_id, None)
        self._require_open(plan)
        seconds = ticket_seconds(ROLE_VIEWER, self._s("live_viewer_ticket_seconds", 30))
        state = plan.state
        ticket = mint_ticket(key, {
            "session_id": state["id"],
            "tenant_id": tenant_id,
            "task_id": task_id,
            "attempt_id": state.get("attempt_id"),
            "generation": state.get("generation"),
            "role": ROLE_VIEWER,
            "email": email.strip().lower(),
            "exp": int(self._clock()) + seconds,
        })
        return {
            "ticket": ticket,
            "session_id": state["id"],
            "expires_in": seconds,
            # The socket's address carries the session id and nothing else;
            # the ticket goes in its first message (§5.2).
            "socket_path": socket_path(str(state["id"])),
        }

    def control_ticket(self, admission: Admission, granted: bool) -> str:
        """The relay's word that this viewer now holds control, or no longer
        does (§4.2): a `control` or `release` ticket for this person, session
        and generation, sent on the viewer's own connection."""
        role = ROLE_CONTROL if granted else ROLE_RELEASE
        return mint_ticket(self._require_key(), {
            "session_id": admission.session_id,
            "tenant_id": admission.tenant_id,
            "task_id": admission.task_id,
            "attempt_id": admission.attempt_id,
            "generation": admission.generation,
            "role": role,
            "email": admission.email,
            "exp": int(self._clock()) + TICKET_MAX_SECONDS[role],
        })

    @staticmethod
    def _require_open(plan: _Plan) -> None:
        if plan.session is None:
            raise LiveSessionClosed("this task has no live browser session open")
        if plan.state.get("state") != "open":
            raise LiveSessionClosed(
                f"this task's live session is closed ({plan.state.get('close_cause')})"
            )

    def take(self, tenant_id: str, task_id: str, *, email: str, submitted_by: str | None,
             preempt: bool = False, confirmed_mismatch: ConfirmedMismatch | None = None,
             ) -> dict[str, Any]:
        task = self._task(tenant_id, task_id, submitted_by)
        tenant = self._store.get_tenant(tenant_id)
        if tenant is None or not fresh_membership(self._authenticator, email, tenant):
            # Removed from the tenant since the cached answer: the same 404 as
            # a stranger gets (§5.2 step 2).
            raise NotFound(f"task {task_id!r} not found")
        owner = control_owner(task, self._db)
        who = email.strip().lower()
        renew = int(self._s("live_control_renew_seconds", 60))
        idle = int(self._s("control_idle_seconds", 300))

        def change(plan: _Plan) -> None:
            rights = self._rights(task, plan, who, owner)
            current = plan.controller
            if current is not None and current.get("email") == who:
                plan.session_update["controller"] = {**current, "renew_by": plan.now
                                                     + timedelta(seconds=renew)}
                return
            if not rights["can_control"]:
                raise ControlNotEligible(
                    "you may watch this browser but not control it: control is its "
                    "submitter's, unless they opened it to the tenant while the agent "
                    "asks for help"
                )
            if current is not None:
                if not (preempt and rights["can_preempt"]):
                    raise ControlHeld(
                        f"{current.get('email')} is in control; one person at a time",
                        detail={"controller": current.get("email"),
                                "may_preempt": rights["can_preempt"]},
                    )
                plan.end_control("preempted")
            episode = new_id("lc")
            controller = {
                "email": who,
                "since": plan.now,
                "renew_by": plan.now + timedelta(seconds=renew),
                "last_input_at": plan.now,
                "episode_id": episode,
                "keys": 0,
                "pointers": 0,
            }
            plan.session_update["controller"] = controller
            taken: dict[str, Any] = {"episode_id": episode, "requested": plan.wait is not None}
            if confirmed_mismatch is not None:
                # §5.7 item 2: what the person confirmed past, both names.
                taken["confirmed_mismatch"] = confirmed_mismatch.model_dump()
            plan.audit("control_taken", who, taken)
            plan.events.append((EventType.HUMAN_CONTROL_TAKEN,
                                {"email": who, "episode_id": episode,
                                 "requested": plan.wait is not None}))
            plan.set_wait_controller({"email": who, "since": plan.now},
                                     self._extended_deadline(plan, idle))

        plan, _ = self._transact(tenant_id, task_id, change)
        self._require_open(plan)
        return self._view(task, plan, who, owner)

    def _extended_deadline(self, plan: _Plan, idle: int) -> datetime | None:
        """§4.3: control moves the hold's deadline to min(now + idle, the
        attempt's own deadline), and never earlier than it already was."""
        wait = plan.wait
        if wait is None:
            return None
        current = wait.get("deadline")
        target = plan.now + timedelta(seconds=idle)
        started = plan.task.get("started_at")
        timeout = plan.task.get("timeout_seconds")
        if isinstance(started, datetime) and isinstance(timeout, int):
            target = min(target, started + timedelta(seconds=timeout))
        if isinstance(current, datetime):
            target = max(current, target)
        return target

    def _controller_change(self, tenant_id: str, task_id: str, email: str,
                           submitted_by: str | None, change: Callable[[_Plan, dict], None]
                           ) -> dict[str, Any]:
        task = self._task(tenant_id, task_id, submitted_by)
        owner = control_owner(task, self._db)
        who = email.strip().lower()

        def guarded(plan: _Plan) -> bool:
            current = plan.controller
            if current is None or current.get("email") != who:
                # Not raised here: settling may have just ended this person's
                # control (a lapse), and that end must commit with its audit.
                return False
            change(plan, current)
            return True

        plan, held = self._transact(tenant_id, task_id, guarded)
        self._require_open(plan)
        if not held:
            raise NotController("you are not in control of this browser")
        return self._view(task, plan, who, owner)

    def renew(self, tenant_id: str, task_id: str, *, email: str,
              submitted_by: str | None) -> dict[str, Any]:
        renew = int(self._s("live_control_renew_seconds", 60))

        def change(plan: _Plan, current: dict) -> None:
            plan.session_update["controller"] = {
                **current, "renew_by": plan.now + timedelta(seconds=renew)}

        return self._controller_change(tenant_id, task_id, email, submitted_by, change)

    def return_control(self, tenant_id: str, task_id: str, *, email: str,
                       submitted_by: str | None) -> dict[str, Any]:
        def change(plan: _Plan, current: dict) -> None:
            plan.end_control("returned")
            if plan.task.get("human_wait") is not None:
                # §4.2 step 3: the hand-off is over; one shape for both writers.
                plan.resolve = True
                plan.resolve_by = email.strip().lower()

        return self._controller_change(tenant_id, task_id, email, submitted_by, change)

    def open_to_members(self, tenant_id: str, task_id: str, *, email: str,
                        submitted_by: str | None) -> dict[str, Any]:
        task = self._task(tenant_id, task_id, submitted_by)
        owner = control_owner(task, self._db)
        who = email.strip().lower()

        def change(plan: _Plan) -> None:
            if owner is None or owner != who:
                raise ControlNotEligible(
                    "only the task's submitter may open control to the tenant"
                )
            if plan.state.get("open_to_members"):
                return
            plan.session_update.update({"open_to_members": True,
                                        "opened_to_members_by": who})
            plan.audit("control_opened_to_members", who, {})

        plan, _ = self._transact(tenant_id, task_id, change)
        self._require_open(plan)
        return self._view(task, plan, who, owner)

    def audit_entries(self, tenant_id: str, task_id: str, *, submitted_by: str | None,
                      limit: int = 500) -> list[dict[str, Any]]:
        self._task(tenant_id, task_id, submitted_by)
        rows = []
        # Equality on the task only: a single-field index, no composite needed.
        query = self._db.collection(LIVE_AUDIT).where(filter=FieldFilter("task_id", "==", task_id))
        for snap in query.stream():
            doc = snap.to_dict() or {}
            if doc.get("tenant_id") != tenant_id:
                continue
            rows.append({k: _iso(v) for k, v in doc.items()})
        rows.sort(key=lambda r: (str(r.get("at")), str(r.get("id"))))
        return rows[-limit:]

    # -- the socket (§5.2) ---------------------------------------------------

    def admit(self, auth: Any, session_id: str, ticket: Any, *, submitted_by: str | None
              ) -> Admission:
        """Every check of §5.2 for one socket on `/v1/live/{session_id}/stream`.
        Any refusal is NotFound, so a foreign session, a foreign ticket and a
        closed session read the same.

        The task is the one the VERIFIED ticket names; the caller must reach it
        exactly as every live route reaches a task (their own tenant, and a
        personal workspace's member scope through `submitted_by`), and the
        session must be that task's current one, open, at the ticket's
        generation (invariant 5)."""
        refused = NotFound(f"live session {session_id!r} not found")
        try:
            claims = verify_ticket(self._keys, ticket, now=self._clock())
        except TicketRefused:
            raise refused from None
        email = (auth.email or "").strip().lower()
        task_id = claims.get("task_id")
        if (
            claims.get("role") != ROLE_VIEWER
            or claims.get("session_id") != session_id
            or not isinstance(task_id, str) or not task_id
            or claims.get("tenant_id") != auth.tenant_id
            or str(claims.get("email") or "") != email
        ):
            raise refused
        try:
            self._task(auth.tenant_id, task_id, submitted_by)
        except NotFound:
            raise refused from None
        plan, _ = self._transact(auth.tenant_id, task_id, None)
        state = plan.state
        if plan.session is None or state.get("id") != session_id or state.get("state") != "open":
            raise refused
        if state.get("generation") != claims.get("generation"):
            # A stale generation, seen: the relay must not keep it either.
            self.close_relay(session_id)
            raise refused
        return Admission(email=email, tenant_id=auth.tenant_id, task_id=task_id,
                         session_id=session_id, attempt_id=str(state.get("attempt_id")),
                         generation=int(state["generation"]), ticket=ticket)

    def recheck(self, admission: Admission) -> bool:
        """§5.2: is this person still a member of the task's tenant? Through
        the ordinary per-group check and its cache, as every request is."""
        tenant = self._store.get_tenant(admission.tenant_id)
        if tenant is None:
            return False
        try:
            return bool(self._authenticator.is_tenant_member(admission.email, tenant))
        except ApiError:
            # The directory did not answer: not a refusal, ask again next time.
            return True

    def status(self, admission: Admission) -> dict[str, Any]:
        """What the socket needs each tick: open or not, and who controls."""
        if self._needs_settling(admission.tenant_id, admission.task_id, admission.session_id):
            self._transact(admission.tenant_id, admission.task_id, None,
                           session_id=admission.session_id)
        snap = self._db.collection(LIVE_SESSIONS).document(admission.session_id).get()
        session = snap.to_dict() if snap.exists else None
        if session is None:
            return {"open": False, "cause": "attempt_ended", "in_control": False}
        current = session.get("controller") if isinstance(session.get("controller"), dict) else None
        return {
            "open": session.get("state") == "open"
            and session.get("generation") == admission.generation,
            "cause": session.get("close_cause"),
            "in_control": bool(current and current.get("email") == admission.email),
            "controller": current.get("email") if current else None,
        }

    def record_input(self, admission: Admission, *, keys: int, pointers: int) -> bool:
        """The controller's input since the last flush: counts for the audit,
        and the hold's deadline moved (§4.3). False when they no longer hold
        control, and nothing is written."""
        idle = int(self._s("control_idle_seconds", 300))

        def change(plan: _Plan) -> bool:
            current = plan.controller
            if (
                plan.state.get("id") != admission.session_id
                or current is None
                or current.get("email") != admission.email
            ):
                return False
            plan.session_update["controller"] = {
                **current,
                "last_input_at": plan.now,
                "keys": int(current.get("keys") or 0) + keys,
                "pointers": int(current.get("pointers") or 0) + pointers,
            }
            deadline = self._extended_deadline(plan, idle)
            if deadline is not None:
                plan.set_wait_controller(plan.wait.get("controller") if plan.wait else None,
                                         deadline)
            return True

        _, held = self._transact(admission.tenant_id, admission.task_id, change,
                                 session_id=admission.session_id)
        return bool(held)

    def end_control(self, admission: Admission, cause: str, *, keys: int = 0,
                    pointers: int = 0) -> bool:
        """End this person's control with `cause` (a socket's membership lapsed)."""

        def change(plan: _Plan) -> bool:
            current = plan.controller
            if (
                plan.state.get("id") != admission.session_id
                or current is None
                or current.get("email") != admission.email
            ):
                return False
            plan.end_control(cause, keys=keys, pointers=pointers)
            return True

        _, ended = self._transact(admission.tenant_id, admission.task_id, change,
                                  session_id=admission.session_id)
        return bool(ended)

    def viewer_event(self, admission: Admission, entry: str, detail: Mapping[str, Any]) -> None:
        """`viewer_joined` / `viewer_left`: an entry that records no other
        change, so it is its own write."""
        snap = self._db.collection(LIVE_SESSIONS).document(admission.session_id).get()
        session = snap.to_dict() if snap.exists else {
            "id": admission.session_id, "task_id": admission.task_id,
            "tenant_id": admission.tenant_id, "generation": admission.generation,
        }
        write_audit(None, self._db, session, entry, email=admission.email, detail=detail,
                    now=self._now())


# --------------------------------------------------------------------------
# Per-person rate limits (§5.10), in-process like `ratelimit` itself
# --------------------------------------------------------------------------


class LiveLimits:
    """Viewer tickets per person, and takes per person per task.

    In-process, as `TokenBucketLimiter` is: N instances allow N times this,
    which that module records."""

    def __init__(self, settings: Any) -> None:
        from .ratelimit import TokenBucketLimiter

        tickets = int(getattr(settings, "live_viewer_tickets_per_minute", 30))
        takes = int(getattr(settings, "live_takes_per_minute", 6))
        self.tickets = TokenBucketLimiter(tickets / 60.0, tickets)
        self.takes = TokenBucketLimiter(takes / 60.0, takes)

    def ticket(self, email: str) -> None:
        self.tickets.check(email.strip().lower())

    def take(self, email: str, task_id: str) -> None:
        self.takes.check(f"{email.strip().lower()}|{task_id}")


def count_input(frame: bytes) -> tuple[int, int]:
    """(keys, pointer events) in one viewer frame, by its first byte only."""
    if not frame:
        return 0, 0
    if frame[0] == RFB_KEY_EVENT:
        return 1, 0
    if frame[0] == RFB_POINTER_EVENT:
        return 0, 1
    return 0, 0


__all__ = [
    "Admission",
    "ControlHeld",
    "ControlNotEligible",
    "LIVE_SESSIONS",
    "LIVE_SESSION_PURPOSE",
    "LiveLimits",
    "LiveService",
    "NotController",
    "HttpRelayCloser",
    "ROLE_CONTROL",
    "ROLE_RELEASE",
    "ROLE_TUNNEL",
    "ROLE_VIEWER",
    "RelayCloser",
    "TicketRefused",
    "count_input",
    "fresh_membership",
    "live_session_message",
    "mint_close",
    "mint_ticket",
    "relay_closer_for",
    "relay_http_url",
    "session_id_for",
    "socket_path",
    "ticket_seconds",
    "verify_ticket",
]


# --------------------------------------------------------------------------
# Recordings and the action timeline (§7), read back for tenant members
# --------------------------------------------------------------------------
#
# LANE LB-SEAM. The console reads `GET .../live/recordings` (Recordings.tsx,
# Theater.tsx), streams a segment from `.../live/recordings/raw?attempt_id=
# &name=` (Recordings.tsx) or `.../attempts/{attempt}/live/recordings/{seq}`
# (Theater.tsx), and reads `.../live/actions` (TheaterTimeline.tsx). No route
# served any of them, so every recording and timeline read as "unserved".
#
# §7: "Members of the task's tenant, through a swarm-api route that checks the
# tenant as §5.2 does and streams the object." The task is read through the
# tenant first (another tenant's is the same 404 as a missing one), and every
# object key is BUILT here from that tenant, that task and a validated attempt
# id and segment number -- a caller never names an object. The relay writes
# the names (`swarm_live.recorder.segment_name`): a 13-digit sequence.

#: The relay's segment names (`swarm_live.recorder._LIVE_NAME`), matched on the
#: part after `.../attempts/`.
_RECORDING = re.compile(r"(?P<a>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})/live/rec-(?P<seq>[0-9]{13})\.live\.webm")
_ACTIONS = re.compile(r"(?P<a>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})/live/actions-(?P<seq>[0-9]{13})\.live\.jsonl")
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
#: How many objects one listing of a task's attempts reads. A task's attempts
#: hold logs and outputs as well as recordings; a listing cut short says so
#: (`truncated`) rather than passing for the whole.
RECORDING_SCAN_LIMIT = 5000
#: The action timeline reads at most this many bytes of action logs, newest
#: segments kept: the relay writes up to 20,000 lines a minute.
MAX_ACTION_BYTES = 2 * 1024 * 1024
#: The fields an action line may carry to the console (`swarm_live.recorder.
#: ACTION_FIELDS`, less nothing): the relay already refuses any other.
ACTION_FIELDS = ("kind", "method", "url", "length", "event", "cause", "outcome")
#: One read of a streamed segment.
SEGMENT_CHUNK_BYTES = 1024 * 1024


class RecordingSegmentView(_Wire):
    """One segment, in both of the console's shapes: Recordings.tsx reads
    `name`, `key`, `bytes`, `created_at`; Theater.tsx reads `seq`, `object`,
    `started_at`."""

    attempt_id: str
    seq: int
    name: str
    key: str
    object: str
    bytes: int | None
    created_at: str | None
    #: The object's write time. The relay writes a segment when it closes, so
    #: this is the END of the minute it holds; the relay records no start.
    started_at: str | None


class RecordingsView(_Wire):
    """`GET /v1/tasks/{task_id}/live/recordings`, oldest first."""

    tenant_id: str
    task_id: str
    recordings: list[RecordingSegmentView]
    segments: list[RecordingSegmentView]
    truncated: bool


class LiveActionView(_Wire):
    at: str
    kind: str
    method: str | None = None
    url: str | None = None
    length: int | None = None
    event: str | None = None
    cause: str | None = None
    outcome: str | None = None


class LiveActionsView(_Wire):
    """`GET /v1/tasks/{task_id}/live/actions`: one attempt's timeline."""

    attempt_id: str | None
    actions: list[LiveActionView]
    truncated: bool


def _attempts_prefix(tenant_id: str, task_id: str) -> str:
    return f"tenants/{tenant_id}/tasks/{task_id}/attempts/"


def recording_key(tenant_id: str, task_id: str, attempt_id: str, seq: int) -> str:
    if not _ATTEMPT_ID.fullmatch(attempt_id) or ".." in attempt_id:
        raise NotFound("no such recording")
    if not 0 <= seq < 10**13:
        raise NotFound("no such recording")
    return f"{_attempts_prefix(tenant_id, task_id)}{attempt_id}/live/rec-{seq:013d}.live.webm"


class LiveRecordings:
    """§7's read side. Built per request from the app context."""

    def __init__(self, *, store: Store, objects: Any) -> None:
        self._store = store
        self._objects = objects

    def _reader(self) -> Any:
        if self._objects is None:
            raise LiveUnavailable("this deployment has no artifact bucket to read recordings from")
        return self._objects

    def _listing(self, tenant_id: str, task_id: str) -> Any:
        return self._reader().list_objects(_attempts_prefix(tenant_id, task_id),
                                           limit=RECORDING_SCAN_LIMIT)

    def recordings(self, tenant_id: str, task_id: str, *, submitted_by: str | None
                   ) -> dict[str, Any]:
        self._store.get_task(tenant_id, task_id, submitted_by=submitted_by)
        prefix = _attempts_prefix(tenant_id, task_id)
        listing = self._listing(tenant_id, task_id)
        rows = []
        for info in listing.objects:
            if not info.key.startswith(prefix):
                continue
            match = _RECORDING.fullmatch(info.key[len(prefix):])
            if match is None:
                continue
            written = _iso(info.updated)
            rows.append({
                "attempt_id": match.group("a"),
                "seq": int(match.group("seq")),
                "name": info.name,
                "key": info.key,
                "object": info.key,
                "bytes": info.size,
                "created_at": written,
                "started_at": written,
            })
        rows.sort(key=lambda r: (str(r["created_at"] or ""), r["attempt_id"], r["seq"]))
        return {"tenant_id": tenant_id, "task_id": task_id, "recordings": rows,
                "segments": [dict(r) for r in rows], "truncated": bool(listing.truncated)}

    def segment(self, tenant_id: str, task_id: str, attempt_id: str, seq: int, *,
                submitted_by: str | None) -> tuple[Any, int]:
        """The segment's bytes, as an iterator of chunks, and its size. Every
        failure before the first byte is the JSON envelope."""
        self._store.get_task(tenant_id, task_id, submitted_by=submitted_by)
        key = recording_key(tenant_id, task_id, attempt_id, seq)
        reader = self._reader()
        try:
            first = reader.read_range(key, offset=0, length=SEGMENT_CHUNK_BYTES)
        except ObjectAbsent:
            raise NotFound("no such recording (they are kept 7 days)") from None
        except ObjectUnreadable:
            raise LiveUnavailable("the recording could not be read") from None

        def chunks() -> Any:
            yield first.data
            offset = first.end
            while offset < first.total_bytes:
                part = reader.read_range(key, offset=offset, length=SEGMENT_CHUNK_BYTES)
                if not part.data:
                    return
                yield part.data
                offset = part.end

        return chunks(), first.total_bytes

    def actions(self, tenant_id: str, task_id: str, *, submitted_by: str | None
                ) -> dict[str, Any]:
        """The newest attempt's action lines, oldest first. The newest attempt
        is the one whose action log was written last."""
        self._store.get_task(tenant_id, task_id, submitted_by=submitted_by)
        prefix = _attempts_prefix(tenant_id, task_id)
        listing = self._listing(tenant_id, task_id)
        found: list[tuple[str, int, Any]] = []
        for info in listing.objects:
            if not info.key.startswith(prefix):
                continue
            match = _ACTIONS.fullmatch(info.key[len(prefix):])
            if match is not None:
                found.append((match.group("a"), int(match.group("seq")), info))
        if not found:
            return {"attempt_id": None, "actions": [], "truncated": bool(listing.truncated)}
        newest = max(found, key=lambda f: (str(_iso(f[2].updated) or ""), f[1]))[0]
        mine = sorted((f for f in found if f[0] == newest), key=lambda f: f[1])
        budget = MAX_ACTION_BYTES
        kept: list[Any] = []
        for _, _, info in reversed(mine):
            if info.size > budget:
                break
            kept.append(info)
            budget -= info.size
        truncated = bool(listing.truncated) or len(kept) < len(mine)
        lines: list[dict[str, Any]] = []
        for info in reversed(kept):
            data = self._reader().read_range(info.key, offset=0, length=max(info.size, 1)).data
            for raw in data.decode("utf-8", errors="replace").splitlines():
                line = _action_line(raw)
                if line is not None:
                    lines.append(line)
        return {"attempt_id": newest, "actions": lines, "truncated": truncated}


def _action_line(raw: str) -> dict[str, Any] | None:
    """One relay line (`{"t": <unix seconds>, ...ACTION_FIELDS}`) as the
    console reads it (`LiveAction`: `at` ISO). A line of any other shape is
    dropped whole, as the relay drops one."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("t"), (int, float)):
        return None
    if not isinstance(doc.get("kind"), str):
        return None
    out: dict[str, Any] = {
        "at": datetime.fromtimestamp(float(doc["t"]), tz=timezone.utc).isoformat(),
    }
    for name in ACTION_FIELDS:
        value = doc.get(name)
        if value is None:
            continue
        if name == "length":
            if isinstance(value, bool) or not isinstance(value, int):
                return None
        elif not isinstance(value, str):
            return None
        out[name] = value
    return out
