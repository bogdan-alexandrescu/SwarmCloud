"""The workspace secrets release (SEC-RELEASE, phase 2 of #1347).

    POST /v1/attempts/{attempt_id}/secrets:release      body {generation}, nothing else

docs/design/workspace-secrets.md §3.3, §4.1, §4.2, §4.4 and §4.5. THE ONE ROUTE
THAT SERVES CREDENTIAL MATERIAL (owner decision Q2, §10), named as the only
carve-out by tests/unit/control_plane/test_no_route_serves_credentials.py. It
is WORKER-ONLY, like the git tools' and the search tool's routes: it declares
neither `current_auth` nor `tenant_scope`, because no person may call it, and
the request carries no tenant, task, lease, name or consumer to choose.

IN ORDER, REFUSING AT THE FIRST FAILURE, AND NOTHING IS READ FROM SECRET
MANAGER BEFORE STEP 5:

  1. THE ATTEMPT. The path's attempt id names exactly one lease; the lease
     names the tenant and the task. The tenant's worker identity, then the
     attempt key's signature over the raw body under its own purpose,
     `swarm-secrets-release/v1`, by the key registered for
     (tenant, task, attempt, lease, generation). Every failure is the same
     403, so none says which part was wrong.
  2. THE FENCE (invariant 5). The task is STARTING or RUNNING at this lease and
     THIS generation, and the lease is unreleased. Anything else is 409 and
     nothing. Re-checked in the transaction that writes the rows, so a worker
     superseded while Secret Manager answered is still sent nothing.
  3. ONCE. A second release for the attempt is 410: a release is per attempt,
     and a parked task that resumes is a new attempt with its own.
  4. THE SIGNED SPEC (`verdictpublish.spec_refusal`, the worker's checks
     restated). The consumers -- profile, the RUNTIME_TOOLS its `browser_tool`
     launches, the components it runs -- and the submitter are read from the
     task document, which the tenant's own agents can write; an unverified
     document is released nothing.
  5. THE QUERY (`workspace_secrets.release_plan`): enabled grants of the task's
     tenant naming those consumers, and a person's own opted-in secrets under
     owner decision Q3, each grant's attestation checked (SEC-GRANT-ATTEST:
     `grant_attest.GrantPolicy`; a forged or altered grant is dropped as a
     disabled one is, logged `grant_attestation_invalid`, and an unattested
     one too once `grant_attestation_required` is on). A REPOSITORY secret
     (SSC-REPO-SCOPE, docs/design/secrets-scopes.md §2.2) only when the signed
     spec clones its repository (`attempt_repositories`) and its grant is
     bound to that repository and attested "v2", whatever that switch says;
     one `(slot, name)` offered by several scopes is released once, the
     person's, then the repository's, then the workspace's (Q1), the others
     withheld `shadowed_by_<scope>`. Then each due record's
     current version, read from
     Secret Manager under the conditioned grant SEC-IAM wrote. A read swarm-api
     may not make (the grant not yet applied) is 503 with a reason and no name.
  6. THE ROWS (§4.4): one `workspace_secret_releases` row per secret, with an
     `expire_at` 90 days out for the Firestore TTL (Q6), and the attempt's
     mark, in the transaction that re-checks the fence.

NO VALUE IS LOGGED, STORED OR ECHOED. The values are locals of `release`, put
in the response and nowhere else. The handler reads the raw body itself, so
FastAPI's validation (whose error carries the input) never sees it; a refusal
says which rule failed and never repeats a body, a name or a value. The
request logger is `main.create_app`'s `observe`, which records the route
template, the method and the status class, and NEVER a body or a response;
`main.UNCAPTURED_ROUTES` names this route so a body capture added there later
has to skip it (held by tests/unit/control_plane/test_secret_release.py).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import Field, StrictInt
from starlette.concurrency import run_in_threadpool

from swarm_common.admission import _snapshot
from swarm_common.identity import tenant_id_for_group, tenant_id_for_user
from swarm_common.states import TaskState

from ..childkey import AttemptTuple, verify_proof
from ..children import (
    PROOF_HEADER,
    TIMESTAMP_HEADER,
    ChildService,
    ChildUnauthenticated,
    _lease_is_live,
    _Strict,
    parse_body,
)
from ..deps import AppContext, get_context
from ..errors import ApiError, UpstreamUnavailable
from ..grant_attest import GrantPolicy
from ..store import LEASES, TASKS
from ..verdictpublish import spec_refusal
from ..workspace_secrets import (
    RELEASE_MARKS_COLLECTION,
    RELEASE_RETENTION,
    RELEASES_COLLECTION,
    Due,
    attempt_consumers,
    attempt_repositories,
    release_plan,
    release_row,
    version_to_read,
)
from .children import _presented, _worker_verifier

log = logging.getLogger(__name__)

router = APIRouter(tags=["workspace-secrets-release"])

#: The route, as the app mounts it. `main.UNCAPTURED_ROUTES` and the
#: credential sweep's carve-out name this string.
RELEASE_ROUTE = "/v1/attempts/{attempt_id}/secrets:release"

#: The attempt key's signing purpose for this route (design §3.3). The worker
#: restates it (`agent_worker.secretrelease.RELEASE_PURPOSE`); a test holds the two equal.
RELEASE_PURPOSE = "swarm-secrets-release/v1"

#: A body is `{"generation": <int>}`; anything longer is not one.
MAX_BODY_BYTES = 256

UNPROVEN_MESSAGE = "this request is not proven for this attempt"
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


# --------------------------------------------------------------------------
# answers
# --------------------------------------------------------------------------


class ReleaseUnproven(ApiError):
    status_code = 403
    code = "secrets_release_unproven"


class ReleaseFenced(ApiError):
    status_code = 409
    code = "secrets_release_fenced"


class ReleaseSpent(ApiError):
    status_code = 410
    code = "secrets_release_spent"


class ReleaseSpecUnverified(ApiError):
    status_code = 403
    code = "secrets_release_spec_unverified"


class ReleaseUnavailable(UpstreamUnavailable):
    code = "secrets_release_unavailable"


class ReleaseBody(_Strict):
    """The whole body. Strict: a name, a tenant or a consumer is a 422."""

    generation: StrictInt = Field(ge=1)


def release_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the attempt key signs for one release: the child path's shape
    under this route's own purpose, so no other route's signature verifies."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{RELEASE_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


# --------------------------------------------------------------------------
# the one payload read
# --------------------------------------------------------------------------


class ReadUnavailable(Exception):
    """swarm-api could not read a version. The message is a reason class, never
    a name or a value."""


class SecretReader(Protocol):
    def access(self, version: str) -> bytes: ...


@dataclass
class SecretManagerReader:
    """`secretmanager.versions.access` on a `-wsec-` version, and nothing else.

    Under SEC-IAM's conditioned grant (terraform/bootstrap/workspace_secrets.tf,
    off until the owner sets `enable_workspace_secrets` and applies bootstrap).
    Until then every read is PERMISSION_DENIED, which the route answers 503.
    The client is built on first use, so `create_app()` needs no credentials."""

    project_id: str
    client: Any = None

    def access(self, version: str) -> bytes:
        from google.api_core import exceptions as gexc

        if self.client is None:
            from google.cloud import secretmanager

            self.client = secretmanager.SecretManagerServiceClient()
        try:
            answer = self.client.access_secret_version(request={"name": version})
        except gexc.PermissionDenied:
            raise ReadUnavailable("permission_denied") from None
        except (gexc.NotFound, gexc.FailedPrecondition):
            raise ReadUnavailable("version_unavailable") from None
        except Exception as exc:  # noqa: BLE001 -- said by type, never a message
            raise ReadUnavailable(type(exc).__name__) from None
        return bytes(answer.payload.data)


def secret_reader(request: Request, ctx: AppContext = Depends(get_context)) -> SecretReader:
    """The reader, built once per process on `app.state` (as the store's
    writer is) so a test substitutes one without rebuilding the context.

    THE ONLY PLACE A WORKSPACE SECRET READER IS MADE. The credential sweep
    finds every route that depends on this function and requires exactly
    one, this module's release."""
    reader = getattr(request.app.state, "workspace_secret_reader", None)
    if reader is None:
        reader = SecretManagerReader(ctx.settings.project_id)
        request.app.state.workspace_secret_reader = reader
    return reader


# --------------------------------------------------------------------------
# the release
# --------------------------------------------------------------------------


@dataclass
class SecretRelease:
    ctx: AppContext
    children: ChildService
    reader: SecretReader
    now: Callable[[], datetime]

    def release(self, attempt_id: str, raw: bytes, *, authorization: str | None,
                proof: str | None, timestamp: str | None, path: str) -> dict[str, Any]:
        self.children._require_configured()
        if len(raw) > MAX_BODY_BYTES:
            raise ReleaseUnproven(UNPROVEN_MESSAGE)
        body: ReleaseBody = parse_body(ReleaseBody, raw)
        attempt = self._prove(attempt_id, body.generation, raw=raw, authorization=authorization,
                              proof=proof, timestamp=timestamp, path=path)
        task = self._fenced(attempt, write=None)
        refusal = spec_refusal(self.ctx.settings, task, attempt.task_id)
        if refusal:
            log.warning("secrets release refused tenant=%s task=%s attempt=%s: %s",
                        attempt.tenant_id, attempt.task_id, attempt.attempt_id, refusal)
            raise ReleaseSpecUnverified(f"{refusal}; nothing was released")

        consumers = attempt_consumers(task)
        project_id = self.ctx.settings.project_id
        plan = release_plan(
            self.ctx.db, tenant_id=attempt.tenant_id, consumers=consumers,
            project_id=project_id, grants=GrantPolicy.from_settings(self.ctx.settings),
            personal=self._personal(task),
            personal_refusal=lambda: self._membership_refusal(task),
            repositories=attempt_repositories(task),
        )
        released: list[dict[str, Any]] = []
        for due in plan.due:
            try:
                payload = self.reader.access(version_to_read(due.record, project_id))
                value = payload.decode("utf-8")
            except ReadUnavailable as exc:
                log.error("secrets release could not read a workspace secret tenant=%s "
                          "attempt=%s (%s)", attempt.tenant_id, attempt.attempt_id, exc)
                raise ReleaseUnavailable(
                    "swarm-api cannot read this workspace's secrets yet (the conditioned "
                    "read grant, terraform/bootstrap/workspace_secrets.tf, is not applied, "
                    "or a version is disabled); nothing was released, and asking on the "
                    "next attempt is safe") from None
            except (UnicodeDecodeError, ValueError):
                raise ReleaseUnavailable(
                    "a workspace secret could not be read as text; nothing was "
                    "released") from None
            item = {
                "name": due.name, "slot": due.slot, "kind": due.record.get("kind"),
                "version": due.record.get("current_version"), "value": value,
            }
            if due.hosts:
                # CF-ACCESS-TOKEN: where the gate may send it, from the signed grant.
                item["hosts"] = list(due.hosts)
            released.append(item)
        self._record(attempt, task, plan.due)
        withheld = [{"name": name, "reason": reason} for name, reason in plan.withheld]
        log.info("workspace secrets released tenant=%s task=%s attempt=%s generation=%s "
                 "names=%s withheld=%s", attempt.tenant_id, attempt.task_id,
                 attempt.attempt_id, attempt.generation,
                 [item["name"] for item in released], [w["name"] for w in withheld])
        return {"attempt_id": attempt.attempt_id, "generation": attempt.generation,
                "consumers": list(consumers), "secrets": released, "withheld": withheld}

    # -- step 1 ----------------------------------------------------------------

    def _prove(self, attempt_id: str, generation: int, *, raw: bytes,
               authorization: str | None, proof: str | None, timestamp: str | None,
               path: str) -> AttemptTuple:
        unproven = ReleaseUnproven(UNPROVEN_MESSAGE)
        if not _ATTEMPT_ID.fullmatch(attempt_id):
            raise unproven
        query = self.ctx.db.collection(LEASES) \
            .where(filter=FieldFilter("attempt_id", "==", attempt_id)).limit(2)
        leases = [(snap.id, snap.to_dict() or {}) for snap in query.stream()]
        if len(leases) != 1:
            raise unproven
        lease_id, lease = leases[0]
        tenant_id, task_id = lease.get("tenant_id"), lease.get("task_id")
        if (not isinstance(tenant_id, str) or not tenant_id or not isinstance(task_id, str)
                or not task_id or lease.get("attempt_id") != attempt_id):
            raise unproven
        attempt = AttemptTuple(tenant_id, task_id, attempt_id, lease_id, generation)
        try:
            self.children.authenticate_worker(authorization, tenant_id)
        except ChildUnauthenticated:
            raise unproven from None
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        skew = int(getattr(self.ctx.settings, "child_proof_skew_seconds", 120))
        if abs(self.children._clock() - int(timestamp)) > skew:
            raise unproven
        registration = self.children._registration(attempt)
        if registration is None or not verify_proof(
                registration["public_key"], proof,
                release_message("POST", path, raw, timestamp)):
            raise unproven
        return attempt

    # -- steps 2, 3 and 6 --------------------------------------------------------

    def _refs(self, attempt: AttemptTuple) -> tuple[Any, Any, Any]:
        db = self.ctx.db
        return (db.collection(TASKS).document(attempt.task_id),
                db.collection(LEASES).document(attempt.lease_id),
                db.collection(RELEASE_MARKS_COLLECTION).document(attempt.attempt_id))

    def _check(self, txn: Any, attempt: AttemptTuple, now: datetime) -> tuple[str, dict[str, Any]]:
        task_ref, lease_ref, mark_ref = self._refs(attempt)
        task_snap = _snapshot(txn.get(task_ref))
        lease_snap = _snapshot(txn.get(lease_ref))
        mark_snap = _snapshot(txn.get(mark_ref))
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        if (task is None
                or task.get("state") not in (TaskState.STARTING.value, TaskState.RUNNING.value)
                or not _lease_is_live(task, lease, attempt, now)):
            return "fenced", {}
        if mark_snap.exists:
            return "spent", {}
        return "ok", task

    def _fenced(self, attempt: AttemptTuple, *, write: Callable[[Any, dict], None] | None
                ) -> dict[str, Any]:
        now = self.now()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[str, dict[str, Any]]:
            outcome, task = self._check(txn, attempt, now)
            if outcome == "ok" and write is not None:
                write(txn, task)
            return outcome, task

        outcome, task = _apply(self.ctx.db.transaction())
        if outcome == "fenced":
            raise ReleaseFenced("this attempt is not STARTING or RUNNING under its live lease "
                                "at this generation; nothing was released")
        if outcome == "spent":
            raise ReleaseSpent("this attempt's secrets were already released; a release is "
                               "once per attempt, and the next attempt gets its own")
        task.setdefault("id", attempt.task_id)
        return task

    def _record(self, attempt: AttemptTuple, task: dict[str, Any], due: list[Due]) -> None:
        db = self.ctx.db
        at = self.now()

        def write(txn: Any, current: dict[str, Any]) -> None:
            _, _, mark_ref = self._refs(attempt)
            current = {**current, "id": attempt.task_id}
            txn.set(mark_ref, {
                "attempt_id": attempt.attempt_id, "task_id": attempt.task_id,
                "tenant_id": attempt.tenant_id, "generation": attempt.generation,
                "at": at, "expire_at": at + RELEASE_RETENTION, "released": len(due),
            })
            for item in due:
                row_id = f"{attempt.attempt_id}~{item.record.get('secret_id')}~{item.slot}"
                txn.set(db.collection(RELEASES_COLLECTION).document(row_id), release_row(
                    item, task=current, attempt_id=attempt.attempt_id,
                    generation=attempt.generation, at=at))

        self._fenced(attempt, write=write)

    # -- owner decision Q3 ---------------------------------------------------------

    def _registered_group(self, tenant_id: str) -> str | None:
        """The registered group a group tenant id is, from configuration -- the
        walk tenant resolution makes (`Authenticator._tenant_choices`) -- never
        from the tenant document, which a tenant's agent can rewrite."""
        for group in getattr(self.ctx.settings, "tenant_groups", ()) or ():
            if tenant_id_for_group(group) == tenant_id:
                return group.lower()
        return None

    def _personal(self, task: dict[str, Any]) -> tuple[str, str] | None:
        """(personal tenant, email) of the SIGNED submitter, on a group task,
        when that personal tenant's document names them; else None."""
        tenant_id = str(task.get("tenant_id") or "")
        email = str(task.get("submitted_by") or "").strip().lower()
        if "@" not in email or self._registered_group(tenant_id) is None:
            return None
        personal = tenant_id_for_user(email)
        if personal == tenant_id:
            return None
        snap = self.ctx.db.collection("tenants").document(personal).get()
        doc = (snap.to_dict() or {}) if snap.exists else {}
        if doc.get("kind") != "user" or str(doc.get("principal") or "").strip().lower() != email:
            return None
        return personal, email

    def _membership_refusal(self, task: dict[str, Any]) -> str | None:
        """None while the submitter is a member of the task's group NOW, asked
        of the directory per registered group; else why not. Logged by name."""
        tenant_id = str(task.get("tenant_id") or "")
        email = str(task.get("submitted_by") or "").strip().lower()
        group = self._registered_group(tenant_id)
        if group is None:
            return "membership_lapsed"
        try:
            member = self.ctx.authenticator.is_tenant_member(
                email, SimpleNamespace(kind="group", principal=group))
        except ApiError:
            log.warning("personal workspace secrets withheld tenant=%s task=%s: membership "
                        "unresolved", tenant_id, task.get("id"))
            return "membership_unresolved"
        if not member:
            log.warning("personal workspace secrets withheld tenant=%s task=%s: the submitter "
                        "is no longer a member", tenant_id, task.get("id"))
            return "membership_lapsed"
        return None


def secret_release(ctx: AppContext = Depends(get_context),
                   reader: SecretReader = Depends(secret_reader)) -> SecretRelease:
    children = ChildService(
        settings=ctx.settings, db=ctx.db, store=ctx.store, submissions=ctx.submissions,
        verifier=_worker_verifier(ctx), limiter=ctx.limiter, metrics=ctx.metrics, now=ctx.now,
    )
    return SecretRelease(ctx=ctx, children=children, reader=reader, now=ctx.now)


@router.post(RELEASE_ROUTE)
async def release_secrets(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: SecretRelease = Depends(secret_release),
) -> JSONResponse:
    raw = await request.body()
    answer = await run_in_threadpool(
        lambda: service.release(
            attempt_id, raw, authorization=_presented(authorization, serverless_authorization),
            proof=proof, timestamp=timestamp, path=request.url.path,
        )
    )
    # No cache, anywhere between here and the worker.
    return JSONResponse(answer, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
