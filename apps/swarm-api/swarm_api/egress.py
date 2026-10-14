"""The attempt egress ticket, swarm-api's half (docs/browser-egress.md §2.2, §2.5, §3.1, §6.1;
lane EGRESS-SVC, part of #1030).

`POST /v1/attempts/{attempt_id}/egress-ticket` (routes/live.py). The WORKER
calls it, proven the way it is proven for `/live-session`: its tenant's
worker ID token, and the attempt key's Ed25519 signature over the raw body
under this route's own purpose (EGRESS_TICKET_PURPOSE), so a live-session or
child signature never verifies here. The fence is `/live-session`'s: the task
is RUNNING under this attempt's live lease, no cancel was requested, and the
attempt is a live-browser profile on GKE.

THE SWITCH (§6.1) is checked after the fence, so a stale worker learns it is
fenced rather than that egress is off. A ticket is minted only when
`browser_egress_enabled`, the tenant is listed and the profile is listed and
is a live-browser profile in the frozen catalogue. Otherwise 409
`egress_off`, whose message names the cause (`disabled`, `tenant_off`,
`profile_off`), and the worker runs the attempt DIRECT for its whole life
(§2.5) -- never proxied for part of it.

THE TICKET is `swarm_egress.ticket`'s wire, restated here because swarm-api
cannot import swarm-egress; tests/unit/egress/test_egress_ticket.py holds the
two together. Read-only: minting writes nothing, so a renewal every few
minutes costs three document reads.

Nothing here logs, stores or serves a key, a signature or a ticket, other
than the one ticket in the response body.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Callable, Sequence

from swarm_common.models import utcnow
from swarm_common.profiles import Backend
from swarm_common.states import TaskState

from .childkey import AttemptTuple, verify_proof
from .children import ChildService, _lease_is_live, parse_body
from . import refusals
from .errors import ApiError, Conflict, Forbidden, ValidationFailed
from .live import LiveSessionOpen, profile_is_live
from .store import ATTEMPTS, LEASES, TASKS

#: The worker's signing purpose for this route; never the live session's.
EGRESS_TICKET_PURPOSE = "swarm-egress-ticket-request/v1"
#: The ticket MAC's domain: `swarm_egress.ticket.TICKET_DOMAIN`.
EGRESS_TICKET_DOMAIN = b"swarm-egress-ticket/v1\0"
EGRESS_TICKET_FIELDS = frozenset({"tenant_id", "task_id", "attempt_id", "generation", "exp",
                                  "nonce"})
#: swarm-egress refuses a ticket claiming more life than this
#: (`swarm_egress.ticket.MAX_TICKET_SECONDS`); the floor keeps a renewal from
#: becoming a hot loop on a misconfigured setting.
MAX_TICKET_SECONDS = 900
MIN_TICKET_SECONDS = 60
MAX_REQUEST_BYTES = 4096


class EgressUnavailable(ApiError):
    status_code = 503
    code = "egress_unavailable"


class EgressOff(ApiError):
    status_code = 409
    code = "egress_off"


class EgressUnauthenticated(ApiError):
    status_code = 403
    code = "egress_unauthenticated"


class EgressUnproven(ApiError):
    status_code = 403
    code = "egress_unproven"


class EgressFenced(ApiError):
    status_code = 409
    code = "egress_fenced"


def _refuse(error: ApiError, established: type[ApiError]) -> ApiError:
    """A NEW refusal ships off (observer proposal I; swarm_api.refusals). The
    egress codes are switched: with a switch on, `error` is raised with its own
    code. With it off (the default), `refusals.refuse` logs what would have been
    said and returns, and the caller gets the same status and message under the
    ESTABLISHED code `established` (`conflict` 409, `forbidden` 403), which every
    client already handles. The route itself stays off by default
    (`browser_egress_enabled`), so nothing changes either way until both are on."""
    refusals.refuse(error)
    return established(error.message)


class EgressTicketRefused(Exception):
    """A ticket that does not verify; the message never carries it."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: str, payload: str) -> bytes:
    return hmac.new(key.encode("utf-8"), EGRESS_TICKET_DOMAIN + payload.encode("ascii"),
                    hashlib.sha256).digest()


def mint_egress_ticket(key: str, *, tenant_id: str, task_id: str, attempt_id: str,
                       generation: int, exp: int) -> str:
    if not key:
        raise EgressUnavailable("this deployment has no swarm-egress-key")
    body = {"tenant_id": tenant_id, "task_id": task_id, "attempt_id": attempt_id,
            "generation": generation, "exp": exp, "nonce": secrets.token_hex(8)}
    payload = _b64(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return f"{payload}.{_b64(_mac(key, payload))}"


def verify_egress_ticket(keys: Sequence[str], ticket: Any, *, now: float) -> dict[str, Any]:
    """The claims, when one of `keys` made `ticket` and it is live. swarm-egress
    has its own verifier; this one is for swarm-api's tests and tools."""
    if not isinstance(ticket, str) or ticket.count(".") != 1:
        raise EgressTicketRefused("malformed")
    payload, mac = ticket.split(".")
    try:
        presented = _unb64(mac)
        claims = json.loads(_unb64(payload))
    except (binascii.Error, ValueError):
        raise EgressTicketRefused("malformed") from None
    if not any(k and hmac.compare_digest(_mac(k, payload), presented) for k in keys):
        raise EgressTicketRefused("the ticket did not verify")
    if not isinstance(claims, dict) or set(claims) != EGRESS_TICKET_FIELDS:
        raise EgressTicketRefused("malformed")
    if not isinstance(claims["exp"], int) or claims["exp"] <= now:
        raise EgressTicketRefused("expired")
    return claims


def egress_ticket_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs to ask for an egress ticket: the live-session
    shape under this route's own purpose."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{EGRESS_TICKET_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode()


def ticket_seconds(configured: Any) -> int:
    return max(MIN_TICKET_SECONDS, min(int(configured), MAX_TICKET_SECONDS))


class EgressService:
    def __init__(self, *, settings: Any, db: Any, children: ChildService | None,
                 now: Callable[[], Any] = utcnow, clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._db = db
        self._children = children
        self._now = now
        self._clock = clock

    def _s(self, name: str, default: Any) -> Any:
        return getattr(self._settings, name, default)

    def _prove(self, authorization: str | None, attempt: AttemptTuple, *, path: str,
               body: bytes, proof: str | None, timestamp: str | None) -> None:
        children = self._children
        if children is None or not children._keys.configured:
            raise EgressUnavailable(
                "this deployment has no swarm-child-key, so no attempt key can be proven")
        try:
            children.authenticate_worker(authorization, attempt.tenant_id)
        except ApiError as exc:
            if exc.status_code == 429:
                raise
            raise _refuse(EgressUnauthenticated(
                "only this tenant's worker service account may call this route"),
                Forbidden) from None
        unproven = _refuse(EgressUnproven("the attempt proof did not verify for this attempt"), Forbidden)
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        if abs(self._clock() - int(timestamp)) > int(self._s("child_proof_skew_seconds", 120)):
            raise unproven
        registration = children._registration(attempt)
        if registration is None:
            raise unproven
        if not verify_proof(registration["public_key"], proof,
                            egress_ticket_message("POST", path, body, timestamp)):
            raise unproven

    def _switch(self, tenant_id: str, profile: Any) -> None:
        if not self._s("browser_egress_enabled", False):
            raise _refuse(EgressOff("browser egress is disabled for this platform; run direct"), Conflict)
        if tenant_id not in tuple(self._s("browser_egress_tenants", ())):
            raise _refuse(EgressOff("browser egress is tenant_off for this tenant; run direct"), Conflict)
        profiles = tuple(self._s("browser_egress_profiles", ()))
        if str(profile or "") not in profiles or not profile_is_live(profile):
            raise _refuse(EgressOff("browser egress is profile_off for this profile; run direct"), Conflict)

    def mint(self, authorization: str | None, attempt_id: str, raw: bytes, *, path: str,
             proof: str | None, timestamp: str | None) -> dict[str, Any]:
        key = self._s("egress_key", "")
        if not key:
            raise EgressUnavailable(
                "this deployment has no swarm-egress-key, so it offers no proxied browsing")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValidationFailed(f"an egress-ticket request is at most {MAX_REQUEST_BYTES} bytes")
        body: LiveSessionOpen = parse_body(LiveSessionOpen, raw)
        if body.attempt_id != attempt_id:
            raise ValidationFailed("the body's attempt_id is not the path's")
        attempt = AttemptTuple(body.tenant_id, body.task_id, body.attempt_id, body.lease_id,
                               body.generation)
        self._prove(authorization, attempt, path=path, body=raw, proof=proof,
                    timestamp=timestamp)

        task_snap = self._db.collection(TASKS).document(attempt.task_id).get()
        lease_snap = self._db.collection(LEASES).document(attempt.lease_id).get()
        attempt_snap = self._db.collection(ATTEMPTS).document(attempt.attempt_id).get()
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        if (
            task is None
            or task.get("state") != TaskState.RUNNING.value
            or task.get("cancel_requested")
            or not _lease_is_live(task, lease, attempt, self._now())
        ):
            raise _refuse(EgressFenced(
                "the task is not RUNNING under this attempt's live lease, or a cancel was "
                "requested; no egress ticket was minted"), Conflict)
        backend = (attempt_snap.to_dict() or {}).get("backend") if attempt_snap.exists else None
        if backend != Backend.GKE_AUTOPILOT.value:
            raise _refuse(
                EgressOff("browser egress is profile_off: this attempt is not on GKE; run direct"),
                Conflict)
        self._switch(attempt.tenant_id, task.get("runner_profile"))

        seconds = ticket_seconds(self._s("egress_ticket_seconds", 600))
        ticket = mint_egress_ticket(key, tenant_id=attempt.tenant_id, task_id=attempt.task_id,
                                    attempt_id=attempt.attempt_id, generation=attempt.generation,
                                    exp=int(self._clock()) + seconds)
        return {"route": "proxy", "ticket": ticket, "expires_in": seconds,
                "country": self._s("browser_egress_country", "us")}


__all__ = [
    "EGRESS_TICKET_DOMAIN",
    "EGRESS_TICKET_PURPOSE",
    "EgressService",
    "egress_ticket_message",
    "mint_egress_ticket",
    "verify_egress_ticket",
]
