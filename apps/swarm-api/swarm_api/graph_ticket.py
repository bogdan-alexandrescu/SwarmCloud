"""The attempt graph ticket, swarm-api's half of the shared graph service
(docs/design/graph-service.md §3.1-§3.2; lane GS-TICKET, part of #1384).

`POST /v1/attempts/{attempt_id}/graph-ticket` (routes/graph_ticket.py). The
WORKER calls it, proven the way it is for `/typesafe-ticket`: its tenant's
worker ID token, and the attempt key's Ed25519 signature over the raw body
under this route's own purpose (TICKET_PURPOSE), so an egress, TypeSafe, live
or child signature never verifies here. The fence is theirs: the task is
RUNNING under this attempt's live lease at this generation and no cancel was
requested. A stale generation gets 409 and nothing is minted (invariant 5).

WHAT THE TICKET PINS, and where each part comes from:

  * the tenant: the fenced task's, never the body's;
  * the repository: the TASK's own `repository_url`, resolved with
    `repositories.repo_id_for` to the tenant's own registration
    (`Repositories.find`, which never returns another tenant's). The body
    cannot name one: the request model forbids extra fields;
  * the commit: the body's `index_sha`, the commit the worker's ancestry proof
    chose (§4.1), accepted only when `repositories/{repo_id}/index_versions/
    {index_sha}` exists and carries a `graph_digest`;
  * the manifest digest: read from that document, which only swarm-api writes
    (`repoindex.RepoIndex._promote_from`). Any agent of the tenant can write
    under `tenants/<t>/`, so a digest taken from a request or from GCS could
    name a rewritten manifest; taking it from Firestore closes that.

THE STORED SETTING DECIDES (invariant 10): when `codeintel.effective_context`
says `off` for this tenant and registration, the answer is 403 with
`detail.reason == "context_disabled"`, and nothing is minted.

THE CODES. Every refusal here is an ESTABLISHED code (`forbidden`, `conflict`,
`validation_failed`, `no_graph`), always enforced: continuing past any of them
is the breach itself, and a new code would need a switch or a listing outside
this lane's files (`swarm_api.refusals`; tests/unit/control_plane/
test_new_refusals_ship_off.py). The design's `context_disabled` is therefore
carried as `detail.reason` on the established 403, and the fence as
`detail.reason == "fenced"` on the established 409. 503
`graph_ticket_unavailable` is not a refusal: the deployment has no key.

The service (lane GS-SVC, apps/swarm-graph/) verifies with its own module;
`verify_graph_ticket` here is the reference that a parity test holds it to.

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

from pydantic import Field

from swarm_common.models import utcnow
from swarm_common.states import TaskState

from .attemptcontext import repository_key
from .childkey import AttemptTuple, verify_proof
from .children import ChildService, _lease_is_live, parse_body
from .codeintel import OFF, CodeIntelligenceStore, effective_context
from .errors import ApiError, Conflict, Forbidden, ValidationFailed
from .live import LiveSessionOpen
from .repoindex import VERSIONS_COLLECTION
from .repograph import NoGraph
from .repositories import COLLECTION as REPOSITORIES
from .repositories import Repositories, parse_repository, repo_id_for
from .store import LEASES, TASKS

#: The worker's signing purpose for this route; never another route's (§3.2).
TICKET_PURPOSE = "swarm-graph-ticket-request/v1"
#: The ticket MAC's domain (§3.2): never an egress, TypeSafe or live ticket's.
TICKET_DOMAIN = b"swarm-graph-ticket/v1\0"
TICKET_FIELDS = frozenset({"tenant_id", "repo_id", "task_id", "attempt_id", "generation",
                           "index_sha", "manifest_digest", "exp", "nonce"})
#: The service refuses a ticket claiming more life than this (§3.2); the floor
#: keeps a renewal (a third of the life left, §4.1) from becoming a hot loop on
#: a misconfigured setting.
MAX_TICKET_SECONDS = 900
MIN_TICKET_SECONDS = 60
MAX_REQUEST_BYTES = 4096
#: `detail.reason` on the established 403 when the stored setting is off.
CONTEXT_DISABLED = "context_disabled"
#: `detail.reason` on the established 409 when the fence does not hold.
FENCED = "fenced"

_SHA = r"^[0-9a-f]{40}$"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class GraphTicketUnavailable(ApiError):
    status_code = 503
    code = "graph_ticket_unavailable"


class TicketRefused(Exception):
    """A ticket that does not verify; the message never carries it."""


class GraphTicketRequest(LiveSessionOpen):
    """`{tenant_id, task_id, attempt_id, lease_id, generation, index_sha}` and
    nothing else: a repository, a tenant or a digest in the body is a 422."""

    index_sha: str = Field(pattern=_SHA)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: str, payload: str) -> bytes:
    return hmac.new(key.encode("utf-8"), TICKET_DOMAIN + payload.encode("ascii"),
                    hashlib.sha256).digest()


def mint_graph_ticket(key: str, *, tenant_id: str, repo_id: str, task_id: str,
                      attempt_id: str, generation: int, index_sha: str, manifest_digest: str,
                      exp: int) -> str:
    if not key:
        raise GraphTicketUnavailable("this deployment has no swarm-graph-ticket-key")
    body = {"tenant_id": tenant_id, "repo_id": repo_id, "task_id": task_id,
            "attempt_id": attempt_id, "generation": generation, "index_sha": index_sha,
            "manifest_digest": manifest_digest, "exp": exp, "nonce": secrets.token_hex(8)}
    payload = _b64(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return f"{payload}.{_b64(_mac(key, payload))}"


def verify_graph_ticket(keys: Sequence[str], ticket: Any, *, now: float) -> dict[str, Any]:
    """The claims, when one of `keys` made `ticket` and it is live. `keys` is a
    ring: the current key and the previous one for a ticket's life after a
    rotation. The service has its own verifier (§3.2); this one is the
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


def graph_ticket_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs to ask for a graph ticket: the TypeSafe ticket's
    shape under this route's own purpose."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{TICKET_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode()


def ticket_seconds(configured: Any) -> int:
    return max(MIN_TICKET_SECONDS, min(int(configured), MAX_TICKET_SECONDS))


class GraphTicketService:
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
            raise GraphTicketUnavailable(
                "this deployment has no swarm-child-key, so no attempt key can be proven")
        try:
            children.authenticate_worker(authorization, attempt.tenant_id)
        except ApiError as exc:
            if exc.status_code == 429:
                raise
            raise Forbidden("only this tenant's worker service account may call this route") \
                from None
        unproven = Forbidden("the attempt proof did not verify for this attempt")
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        if abs(self._clock() - int(timestamp)) > int(self._s("child_proof_skew_seconds", 120)):
            raise unproven
        registration = children._registration(attempt)
        if registration is None:
            raise unproven
        if not verify_proof(registration["public_key"], proof,
                            graph_ticket_message("POST", path, body, timestamp)):
            raise unproven

    def _fence(self, attempt: AttemptTuple) -> dict[str, Any]:
        task_snap = self._db.collection(TASKS).document(attempt.task_id).get()
        lease_snap = self._db.collection(LEASES).document(attempt.lease_id).get()
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        if (
            task is None
            or task.get("tenant_id") != attempt.tenant_id
            or task.get("state") != TaskState.RUNNING.value
            or task.get("cancel_requested")
            or not _lease_is_live(task, lease, attempt, self._now())
        ):
            raise Conflict(
                "the task is not RUNNING under this attempt's live lease, or a cancel was "
                "requested; no graph ticket was minted", detail={"reason": FENCED})
        return task

    def _registration(self, tenant_id: str, task: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """The tenant's own registration of the TASK's repository (§3.1)."""
        no_graph = NoGraph("this task's repository is not registered in its workspace, so it "
                           "has no graph; no graph ticket was minted")
        key = repository_key(task.get("repository_url"))
        if key is None:
            raise no_graph
        try:
            owner, repo = parse_repository(key)
        except ValueError:
            raise no_graph from None
        repo_id = repo_id_for(tenant_id, owner, repo)
        record = Repositories(self._db, now=self._now).find(tenant_id, repo_id)
        if record is None or record.get("tenant_id") != tenant_id:
            raise no_graph
        return repo_id, record

    def _digest(self, repo_id: str, index_sha: str) -> str:
        """The `graph_digest` swarm-api recorded when it promoted `index_sha`."""
        snap = (self._db.collection(REPOSITORIES).document(repo_id)
                .collection(VERSIONS_COLLECTION).document(index_sha).get())
        data = (snap.to_dict() or {}) if snap.exists else {}
        digest = data.get("graph_digest")
        if not isinstance(digest, str) or not _DIGEST.match(digest):
            raise NoGraph(f"no graph was promoted for {index_sha[:12]} of this repository; "
                          "no graph ticket was minted")
        return digest

    def mint(self, authorization: str | None, attempt_id: str, raw: bytes, *, path: str,
             proof: str | None, timestamp: str | None) -> dict[str, Any]:
        key = self._s("graph_ticket_key", "")
        if not key:
            raise GraphTicketUnavailable(
                "this deployment has no swarm-graph-ticket-key, so it runs no graph service")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValidationFailed(f"a graph-ticket request is at most {MAX_REQUEST_BYTES} bytes")
        body: GraphTicketRequest = parse_body(GraphTicketRequest, raw)
        if body.attempt_id != attempt_id:
            raise ValidationFailed("the body's attempt_id is not the path's")
        attempt = AttemptTuple(body.tenant_id, body.task_id, body.attempt_id, body.lease_id,
                               body.generation)
        self._prove(authorization, attempt, path=path, body=raw, proof=proof,
                    timestamp=timestamp)
        task = self._fence(attempt)

        # The fence held, so the task's tenant IS the attempt's.
        tenant_id = attempt.tenant_id
        repo_id, registration = self._registration(tenant_id, task)
        settings = CodeIntelligenceStore(self._db, now=self._now).get(tenant_id)
        if effective_context(settings, registration) == OFF:
            raise Forbidden(
                "graph context is off for this repository in this workspace's code-intelligence "
                "setting; no graph ticket was minted", detail={"reason": CONTEXT_DISABLED})
        digest = self._digest(repo_id, body.index_sha)

        seconds = ticket_seconds(self._s("graph_ticket_seconds", 600))
        ticket = mint_graph_ticket(
            key, tenant_id=tenant_id, repo_id=repo_id, task_id=attempt.task_id,
            attempt_id=attempt.attempt_id, generation=attempt.generation,
            index_sha=body.index_sha, manifest_digest=digest,
            exp=int(self._clock()) + seconds)
        return {"ticket": ticket, "expires_in": seconds, "index_sha": body.index_sha,
                "manifest_digest": digest}


__all__ = [
    "CONTEXT_DISABLED",
    "FENCED",
    "MAX_TICKET_SECONDS",
    "MIN_TICKET_SECONDS",
    "TICKET_DOMAIN",
    "TICKET_FIELDS",
    "TICKET_PURPOSE",
    "GraphTicketRequest",
    "GraphTicketService",
    "GraphTicketUnavailable",
    "TicketRefused",
    "graph_ticket_message",
    "mint_graph_ticket",
    "ticket_seconds",
    "verify_graph_ticket",
]
