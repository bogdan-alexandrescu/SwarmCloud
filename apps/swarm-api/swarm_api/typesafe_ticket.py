"""The attempt TypeSafe ticket, swarm-api's half of the `inject` delivery mode
(docs/design/workspace-secrets.md §3.5; owner decision §10 Q8, 2026-10-14;
lane SEC-INJECT-A, part of #1347).

`POST /v1/attempts/{attempt_id}/typesafe-ticket` (routes/typesafe_ticket.py).
The WORKER calls it, proven the way it is for `/egress-ticket`: its tenant's
worker ID token, and the attempt key's Ed25519 signature over the raw body
under this route's own purpose (TICKET_PURPOSE), so a live-session, egress or
child signature never verifies here. The fence is the egress ticket's: the
task is RUNNING under this attempt's live lease at this generation and no
cancel was requested. A stale generation gets 409 and nothing is minted
(invariant 5).

WHICH KEY. swarm-api decides, never the caller: the workspace (the task's
tenant) must have chosen the `typesafe` judgment backend (swarm_api.judgment),
and `provider_keys.resolve_provider_key` picks the submitter's CONFIRMED
personal key, else the tenant's. The request names no tenant, no secret and
no provider: the tenant is the fenced task's, so an attempt of tenant A can
never be handed a ticket naming tenant B's key.

THE TICKET names that secret by id and version, with the attempt, the tenant,
the generation, the scope and a short expiry, MAC'd under `swarm-typesafe-fwd-key`
with its own domain. It carries NO VALUE: swarm-api never reads one here, and
the forwarder (lane SEC-INJECT-B, apps/swarm-typesafe-fwd/) reads the named
version with its own Workload Identity. The wire is specified in the design's
§3.5 so the forwarder implements exactly this; tests/unit/control_plane/
test_typesafe_ticket_api.py holds it.

Read-only: minting writes nothing. Nothing here logs, stores or serves a key,
a signature or a ticket, other than the one ticket in the response body.
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
from swarm_common.states import TaskState

from . import refusals
from .childkey import AttemptTuple, verify_proof
from .children import ChildService, _lease_is_live, parse_body
from .errors import ApiError, Conflict, Forbidden, ValidationFailed
from .gittokens import secret_name_for
from .judgment import TYPESAFE_BACKEND, JudgmentSettings
from .live import LiveSessionOpen
from .provider_keys import (
    TENANT,
    TYPESAFE,
    USER,
    ProviderKeys,
    resolve_provider_key,
    submitter_personal_tenant,
)
from .store import LEASES, TASKS, Store

#: The worker's signing purpose for this route; never another route's.
TICKET_PURPOSE = "swarm-typesafe-ticket-request/v1"
#: The ticket MAC's domain (design §3.5): never an egress or live ticket's.
TICKET_DOMAIN = b"swarm-typesafe-ticket/v1\0"
TICKET_FIELDS = frozenset({"tenant_id", "task_id", "attempt_id", "generation", "scope",
                           "secret_id", "secret_version", "exp", "nonce"})
#: The forwarder refuses a ticket claiming more life than this (§3.5); the
#: floor keeps a renewal from becoming a hot loop on a misconfigured setting.
MAX_TICKET_SECONDS = 900
MIN_TICKET_SECONDS = 60
MAX_REQUEST_BYTES = 4096
#: What the ticket pins when the status record's version name is not this
#: secret's numbered version: the forwarder then reads `latest` of the SAME
#: secret, never a version name taken from anywhere but here.
LATEST = "latest"


class TypesafeUnavailable(ApiError):
    status_code = 503
    code = "typesafe_unavailable"


class TypesafeFenced(ApiError):
    status_code = 409
    code = "typesafe_fenced"


class TypesafeUnproven(ApiError):
    status_code = 403
    code = "typesafe_unproven"


class TypesafeUnauthenticated(ApiError):
    status_code = 403
    code = "typesafe_unauthenticated"


class TypesafeBackendOff(ApiError):
    status_code = 403
    code = "typesafe_backend_off"


class TypesafeNoKey(ApiError):
    status_code = 403
    code = "typesafe_no_key"


def _refuse(error: ApiError, established: type[ApiError]) -> ApiError:
    """A NEW refusal ships off (swarm_api.refusals), as the egress codes do:
    on, `error` is raised under its own code; off (the default), the caller
    gets the same status and message under the ESTABLISHED code. Either way
    nothing is minted."""
    refusals.refuse(error)
    return established(error.message)


class TicketRefused(Exception):
    """A ticket that does not verify; the message never carries it."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: str, payload: str) -> bytes:
    return hmac.new(key.encode("utf-8"), TICKET_DOMAIN + payload.encode("ascii"),
                    hashlib.sha256).digest()


def mint_typesafe_ticket(key: str, *, tenant_id: str, task_id: str, attempt_id: str,
                         generation: int, scope: str, secret_id: str, secret_version: str,
                         exp: int) -> str:
    if not key:
        raise TypesafeUnavailable("this deployment has no swarm-typesafe-fwd-key")
    body = {"tenant_id": tenant_id, "task_id": task_id, "attempt_id": attempt_id,
            "generation": generation, "scope": scope, "secret_id": secret_id,
            "secret_version": secret_version, "exp": exp, "nonce": secrets.token_hex(8)}
    payload = _b64(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return f"{payload}.{_b64(_mac(key, payload))}"


def verify_typesafe_ticket(keys: Sequence[str], ticket: Any, *, now: float) -> dict[str, Any]:
    """The claims, when one of `keys` made `ticket` and it is live. `keys` is a
    ring: the current key and the previous one for a ticket's life after a
    rotation. The forwarder has its own verifier (§3.5); this one is the
    reference it is tested against."""
    if not isinstance(ticket, str) or ticket.count(".") != 1:
        raise TicketRefused("malformed")
    payload, mac = ticket.split(".")
    try:
        presented = _unb64(mac)
        claims = json.loads(_unb64(payload))
    except (binascii.Error, ValueError):
        raise TicketRefused("malformed") from None
    if not any(k and hmac.compare_digest(_mac(k, payload), presented) for k in keys):
        raise TicketRefused("the ticket did not verify")
    if not isinstance(claims, dict) or set(claims) != TICKET_FIELDS:
        raise TicketRefused("malformed")
    if not isinstance(claims["exp"], int) or claims["exp"] <= now:
        raise TicketRefused("expired")
    if claims["exp"] > now + MAX_TICKET_SECONDS + 1:
        raise TicketRefused("the ticket claims more life than a ticket may have")
    return claims


def typesafe_ticket_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs to ask for a TypeSafe ticket: the live-session
    shape under this route's own purpose."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{TICKET_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode()


def ticket_seconds(configured: Any) -> int:
    return max(MIN_TICKET_SECONDS, min(int(configured), MAX_TICKET_SECONDS))


def pinned_version(secret_id: str, version_name: Any) -> str:
    """The version number the status record names for `secret_id`, or LATEST.
    Only a numbered version OF THIS SECRET is pinned: a record naming another
    secret's version is never copied into a ticket."""
    if isinstance(version_name, str):
        found = re.fullmatch(rf"projects/[^/]+/secrets/{re.escape(secret_id)}/versions/(\d+)",
                             version_name)
        if found:
            return found.group(1)
    return LATEST


class TypesafeTicketService:
    def __init__(self, *, settings: Any, db: Any, store: Store, children: ChildService | None,
                 now: Callable[[], Any] = utcnow, clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._db = db
        self._store = store
        self._children = children
        self._now = now
        self._clock = clock

    def _s(self, name: str, default: Any) -> Any:
        return getattr(self._settings, name, default)

    def _prove(self, authorization: str | None, attempt: AttemptTuple, *, path: str,
               body: bytes, proof: str | None, timestamp: str | None) -> None:
        children = self._children
        if children is None or not children._keys.configured:
            raise TypesafeUnavailable(
                "this deployment has no swarm-child-key, so no attempt key can be proven")
        try:
            children.authenticate_worker(authorization, attempt.tenant_id)
        except ApiError as exc:
            if exc.status_code == 429:
                raise
            raise _refuse(TypesafeUnauthenticated(
                "only this tenant's worker service account may call this route"),
                Forbidden) from None

        def unproven() -> ApiError:
            return _refuse(TypesafeUnproven("the attempt proof did not verify for this attempt"),
                           Forbidden)

        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven()
        if abs(self._clock() - int(timestamp)) > int(self._s("child_proof_skew_seconds", 120)):
            raise unproven()
        registration = children._registration(attempt)
        if registration is None:
            raise unproven()
        if not verify_proof(registration["public_key"], proof,
                            typesafe_ticket_message("POST", path, body, timestamp)):
            raise unproven()

    def _key(self, tenant_id: str, submitted_by: Any) -> tuple[str, str, str]:
        """(scope, secret id, version) of the key this task would spend, from
        status records only -- never Secret Manager, never a value."""
        keys = ProviderKeys(self._db, now=self._now)
        personal = submitter_personal_tenant(
            self._store, submitted_by if isinstance(submitted_by, str) else None)
        # Owner decision Q3 (docs/design/workspace-secrets.md §10): a person's own
        # key is spent on a GROUP workspace's task only by per-secret opt-in, and
        # that opt-in does not exist for this key yet. Until it does, a group task
        # names only the workspace's key; the submitter's own key is spent only in
        # their own personal workspace (answer to SEC-INJECT-A's question,
        # 2026-10-15, under the owner's Q3).
        if personal != tenant_id:
            personal = None
        statuses: dict[str, dict[str, Any]] = {}
        for owner in (personal, tenant_id):
            if owner:
                status = keys.status(owner, TYPESAFE)
                if status["set"]:
                    statuses[str(status["secret_id"])] = status
        chosen = resolve_provider_key(tenant_id, personal, TYPESAFE, set(statuses))
        if chosen is None:
            raise _refuse(TypesafeNoKey(
                "no TypeSafe key resolves for this task: the submitter has none of their own "
                "and the workspace has no tenant key; no TypeSafe ticket was minted"), Forbidden)
        scope = USER if personal and chosen == secret_name_for(personal, TYPESAFE) else TENANT
        return scope, chosen, pinned_version(chosen, statuses[chosen].get("version"))

    def mint(self, authorization: str | None, attempt_id: str, raw: bytes, *, path: str,
             proof: str | None, timestamp: str | None) -> dict[str, Any]:
        key = self._s("typesafe_fwd_key", "")
        if not key:
            raise TypesafeUnavailable(
                "this deployment has no swarm-typesafe-fwd-key, so it runs no TypeSafe forwarder")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValidationFailed(f"a typesafe-ticket request is at most {MAX_REQUEST_BYTES} bytes")
        body: LiveSessionOpen = parse_body(LiveSessionOpen, raw)
        if body.attempt_id != attempt_id:
            raise ValidationFailed("the body's attempt_id is not the path's")
        attempt = AttemptTuple(body.tenant_id, body.task_id, body.attempt_id, body.lease_id,
                               body.generation)
        self._prove(authorization, attempt, path=path, body=raw, proof=proof,
                    timestamp=timestamp)

        task_snap = self._db.collection(TASKS).document(attempt.task_id).get()
        lease_snap = self._db.collection(LEASES).document(attempt.lease_id).get()
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        if (
            task is None
            or task.get("state") != TaskState.RUNNING.value
            or task.get("cancel_requested")
            or not _lease_is_live(task, lease, attempt, self._now())
        ):
            raise _refuse(TypesafeFenced(
                "the task is not RUNNING under this attempt's live lease, or a cancel was "
                "requested; no TypeSafe ticket was minted"), Conflict)

        # The fence held, so the task's tenant IS the attempt's (_lease_is_live).
        tenant_id = attempt.tenant_id
        backend = JudgmentSettings(self._db, now=self._now).read(tenant_id)["judgment_backend"]
        if backend != TYPESAFE_BACKEND:
            raise _refuse(TypesafeBackendOff(
                "this workspace's judgment backend is not TypeSafe, so its judgments are never "
                "sent to TypeSafe; no TypeSafe ticket was minted"), Forbidden)
        scope, secret_id, version = self._key(tenant_id, task.get("submitted_by"))

        seconds = ticket_seconds(self._s("typesafe_ticket_seconds", 300))
        ticket = mint_typesafe_ticket(
            key, tenant_id=tenant_id, task_id=attempt.task_id, attempt_id=attempt.attempt_id,
            generation=attempt.generation, scope=scope, secret_id=secret_id,
            secret_version=version, exp=int(self._clock()) + seconds)
        return {"ticket": ticket, "expires_in": seconds, "scope": scope}


__all__ = [
    "MAX_TICKET_SECONDS",
    "TICKET_DOMAIN",
    "TICKET_FIELDS",
    "TICKET_PURPOSE",
    "TicketRefused",
    "TypesafeTicketService",
    "mint_typesafe_ticket",
    "pinned_version",
    "typesafe_ticket_message",
    "verify_typesafe_ticket",
]
