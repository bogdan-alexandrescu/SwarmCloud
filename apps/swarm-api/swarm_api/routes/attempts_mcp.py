"""The MCP attach: an attempt's registered MCP servers, with their credentials (MCP-API).

    POST /v1/attempts/{attempt_id}/mcp:attach      body {generation}, nothing else

docs/design/registered-mcp-servers.md §4.6, part of #1433. THE SECOND ROUTE
THAT SERVES CREDENTIAL MATERIAL, by owner decision Q9 of 2026-10-17, beside
`secrets:release`; named as a carve-out by
tests/unit/control_plane/test_no_route_serves_credentials.py and listed in
`main.UNCAPTURED_ROUTES`. WORKER-ONLY, like the release: it declares neither
`current_auth` nor `tenant_scope`, and the request carries no tenant, task,
server name or consumer to choose.

Why a sibling and not more of the release (§4.6): the release writes every
value to a file the agent can read, and an MCP credential must never be a
file -- the worker keeps it in its own heap and adds it to the relay's
upstream requests. So the release never hands out an `mcp:` grant
(`workspace_secrets.attempt_consumers` never derives one), and this route is
the only reader of one. It reuses the release's lease lookup, attempt-key
verification and fence by import (`childkey.verify_proof`,
`children._lease_is_live`, `ChildService`), under its own purpose, so a
release, child, egress or live signature never verifies here.

IN ORDER, REFUSING AT THE FIRST FAILURE, AND NOTHING IS READ FROM SECRET
MANAGER BEFORE STEP 6:

  0. THE SWITCH. SWARM_MCP_SERVERS off (the default) is 503 `mcp_unavailable`.
  1. THE ATTEMPT. The path's attempt id names exactly one lease, which names
     the tenant and the task; the tenant's worker identity; the attempt key's
     signature over the raw body under `swarm-mcp-attach/v1`. Every failure
     is the same 403.
  2. THE FENCE (invariant 5). STARTING or RUNNING at this lease and THIS
     generation, the lease unreleased; else 409 and nothing. Re-checked in
     the transaction that writes the rows.
  3. ONCE. A second attach for the attempt is 410.
  4. THE SIGNED SPEC. The server names are the task's verified
     `input.mcp_servers` (`verdictpublish.spec_refusal`); an unverified task
     is attached nothing.
  5. THE REGISTRATIONS, re-read now: one disabled since submission, or whose
     `allowed_profiles` no longer includes the task's profile, is WITHHELD
     with that reason. The current `revision` is used and recorded.
  6. THE CREDENTIALS. `dispatch`: the task's row, its workflow's, or its
     parent's (Q7), live and unexpired, then its Secret Manager version.
     `workspace_secret`: the tenant's enabled, attested grant `mcp:<name>`,
     then its record's current version. Missing or expired is withheld with a
     reason; a read swarm-api may not make (the IAM not applied) is 503 with
     a reason and no name.
  7. THE ROWS. One `mcp_attach_releases` row per attached server, and the
     attempt's mark, in the transaction that re-checks the fence; 90 days of
     Firestore TTL on `expire_at`, as the release's rows.

NO VALUE IS LOGGED, STORED OR ECHOED but in the one answer. The handler reads
the raw body itself, so FastAPI's validation (whose error carries the input)
never sees it; the request logger records the route template, the method and
the status class only.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import Field, StrictInt
from starlette.concurrency import run_in_threadpool

from swarm_common.admission import _snapshot
from swarm_common.models import MCP_SERVER_NAME, consumer_name
from swarm_common.profiles import MCP_SECRET_SLOTS
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
from ..mcp_credentials import CREDENTIAL_MISSING, RowKeys, credential_for
from ..mcp_credentials import version_to_read as credential_version
from ..mcp_servers import (
    MODE_DISPATCH,
    MODE_NONE,
    MODE_WORKSPACE,
    McpUnavailable,
    mcp_servers_enabled,
    profile_allowed,
    read_registration,
)
from ..store import LEASES, TASKS
from ..verdictpublish import spec_refusal
from ..workspace_secrets import (
    GRANTS_COLLECTION,
    RELEASE_RETENTION,
    SECRETS_COLLECTION,
    owned_by,
    version_to_read,
)
from .attempts_secrets import ReadUnavailable, SecretReader, secret_reader
from .children import _presented, _worker_verifier

log = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-attach"])

#: The route, as the app mounts it. `main.UNCAPTURED_ROUTES` names it.
ATTACH_ROUTE = "/v1/attempts/{attempt_id}/mcp:attach"

#: The attempt key's signing purpose for this route (§4.6). MCP-WORKER
#: restates it on the worker's side; a test there holds the two equal.
ATTACH_PURPOSE = "swarm-mcp-attach/v1"

#: One row per attached server per attempt. Never a value.
ATTACH_ROWS_COLLECTION = "mcp_attach_releases"
#: One document per attempt that was attached anything or nothing: once.
ATTACH_MARKS_COLLECTION = "mcp_attach_marks"

#: A body is `{"generation": <int>}`; anything longer is not one.
MAX_BODY_BYTES = 256
#: §4.3: at most four servers per step; a longer signed list is attached
#: no further than this.
MAX_SERVERS = 4

UNPROVEN_MESSAGE = "this request is not proven for this attempt"
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_HEADER_SAFE = re.compile(r"[\x20-\x7e]{1,8192}")

#: Why a named server was not attached (§4.6's `withheld`).
DISABLED = "disabled"
PROFILE_REFUSED = "profile_refused"
UNREADABLE = "unreadable"


class AttachUnproven(ApiError):
    status_code = 403
    code = "mcp_attach_unproven"


class AttachFenced(ApiError):
    status_code = 409
    code = "mcp_attach_fenced"


class AttachSpent(ApiError):
    status_code = 410
    code = "mcp_attach_spent"


class AttachSpecUnverified(ApiError):
    status_code = 403
    code = "mcp_attach_spec_unverified"


class AttachUnavailable(UpstreamUnavailable):
    code = "mcp_attach_unavailable"


class AttachBody(_Strict):
    """The whole body. Strict: a server, a tenant or a URL is a 422."""

    generation: StrictInt = Field(ge=1)


def attach_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the attempt key signs for one attach: the release's shape under
    this route's own purpose, so no other route's signature verifies."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{ATTACH_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def signed_server_names(task: Mapping[str, Any]) -> list[str]:
    """The names the SIGNED input names, in order, deduplicated, spelt as
    contract request 90 spells them; anything else is not a server."""
    inputs = task.get("input") if isinstance(task.get("input"), Mapping) else {}
    raw = inputs.get("mcp_servers") if isinstance(inputs, Mapping) else None
    if not isinstance(raw, list):
        return []
    names = [n for n in raw if isinstance(n, str) and MCP_SERVER_NAME.fullmatch(n)]
    return list(dict.fromkeys(names))[:MAX_SERVERS]


@dataclass
class _Attached:
    registration: Mapping[str, Any]
    credential: str | None
    secret_id: str | None
    version: str | None


@dataclass
class McpAttach:
    ctx: AppContext
    children: ChildService
    reader: SecretReader
    now: Callable[[], datetime]

    def attach(self, attempt_id: str, raw: bytes, *, authorization: str | None,
               proof: str | None, timestamp: str | None, path: str) -> dict[str, Any]:
        if not mcp_servers_enabled():
            raise McpUnavailable(
                "registered MCP servers are not enabled in this deployment; nothing "
                "was attached")
        self.children._require_configured()
        if len(raw) > MAX_BODY_BYTES:
            raise AttachUnproven(UNPROVEN_MESSAGE)
        body: AttachBody = parse_body(AttachBody, raw)
        attempt = self._prove(attempt_id, body.generation, raw=raw, authorization=authorization,
                              proof=proof, timestamp=timestamp, path=path)
        task = self._fenced(attempt, write=None)
        refusal = spec_refusal(self.ctx.settings, task, attempt.task_id)
        if refusal:
            log.warning("mcp attach refused tenant=%s task=%s attempt=%s: %s",
                        attempt.tenant_id, attempt.task_id, attempt.attempt_id, refusal)
            raise AttachSpecUnverified(f"{refusal}; nothing was attached")

        attached: list[_Attached] = []
        withheld: list[dict[str, str]] = []
        profile = str(task.get("runner_profile") or "")
        for name in signed_server_names(task):
            registration = read_registration(self.ctx.db, attempt.tenant_id, name)
            # A registration is never deleted, so a missing one reads as disabled.
            if registration is None or registration.get("enabled") is not True:
                withheld.append({"name": name, "reason": DISABLED})
                continue
            if not profile_allowed(registration, profile):
                withheld.append({"name": name, "reason": PROFILE_REFUSED})
                continue
            item, reason = self._credential(attempt, task, registration)
            if item is None:
                withheld.append({"name": name, "reason": reason or CREDENTIAL_MISSING})
                continue
            attached.append(item)

        self._record(attempt, attached, withheld)
        log.info("mcp servers attached tenant=%s task=%s attempt=%s generation=%s "
                 "names=%s withheld=%s", attempt.tenant_id, attempt.task_id,
                 attempt.attempt_id, attempt.generation,
                 [a.registration.get("name") for a in attached],
                 [(w["name"], w["reason"]) for w in withheld])
        return {
            "attempt_id": attempt.attempt_id, "generation": attempt.generation,
            "servers": [self._answer(a) for a in attached], "withheld": withheld,
        }

    @staticmethod
    def _answer(item: _Attached) -> dict[str, Any]:
        reg = item.registration
        auth = dict(reg.get("auth") or {})
        return {
            "name": reg.get("name"), "url": reg.get("url"), "revision": reg.get("revision"),
            "description": reg.get("description") or "",
            "auth": {"header": auth.get("header"), "scheme": auth.get("scheme"),
                     "credential": item.credential},
            "allowed_tools": list(reg.get("allowed_tools") or []),
            "limits": dict(reg.get("limits") or {}),
        }

    # -- step 6 ------------------------------------------------------------------

    def _owners(self, attempt: AttemptTuple, task: Mapping[str, Any]) -> list[str]:
        """Whose stored credential this task may use, each from a SIGNED
        field: its own, its workflow's, its parent's, its parent's workflow's
        (Q7). The parent's workflow is read from the parent's document, so it
        is taken only when the PARENT's own signature verifies too."""
        owners = [attempt.task_id, str(task.get("workflow_id") or "")]
        parent_id = str(task.get("parent_task_id") or "")
        if parent_id:
            owners.append(parent_id)
            snap = self.ctx.db.collection(TASKS).document(parent_id).get()
            parent = (snap.to_dict() or {}) if snap.exists else {}
            if (parent.get("tenant_id") == attempt.tenant_id
                    and spec_refusal(self.ctx.settings, parent, parent_id) is None):
                owners.append(str(parent.get("workflow_id") or ""))
        return [o for o in dict.fromkeys(owners) if o]

    def _read(self, version: str) -> str | None:
        """The value at `version`, or None when it is unreadable as a header
        value. PERMISSION_DENIED (the IAM not applied) is the 503."""
        try:
            value = self.reader.access(version).decode("ascii")
        except ReadUnavailable as exc:
            if str(exc) == "permission_denied":
                raise AttachUnavailable(
                    "swarm-api cannot read this workspace's MCP credentials yet (the "
                    "conditioned read grant is not applied); nothing was attached, and "
                    "asking on the next attempt is safe") from None
            return None
        except (UnicodeDecodeError, ValueError):
            return None
        return value if _HEADER_SAFE.fullmatch(value) else None

    def _credential(self, attempt: AttemptTuple, task: Mapping[str, Any],
                    registration: Mapping[str, Any]) -> tuple[_Attached | None, str | None]:
        mode = (registration.get("auth") or {}).get("mode")
        project_id = self.ctx.settings.project_id
        if mode == MODE_NONE:
            return _Attached(registration, None, None, None), None
        if mode == MODE_DISPATCH:
            row, reason = credential_for(
                self.ctx.db, tenant_id=attempt.tenant_id,
                server_id=str(registration.get("server_id")),
                owner_ids=self._owners(attempt, task), now=self.now(),
                keys=RowKeys.from_settings(self.ctx.settings))
            if row is None:
                return None, reason
            value = self._read(credential_version(row, project_id))
            if value is None:
                return None, UNREADABLE
            return _Attached(registration, value, row.get("secret_id"), row.get("version")), None
        if mode == MODE_WORKSPACE:
            record = self._granted_record(attempt.tenant_id, str(registration.get("name")))
            if record is None:
                return None, CREDENTIAL_MISSING
            value = self._read(version_to_read(record, project_id))
            if value is None:
                return None, UNREADABLE
            return _Attached(registration, value, record.get("secret_id"),
                             record.get("current_version")), None
        return None, CREDENTIAL_MISSING

    def _granted_record(self, tenant_id: str, name: str) -> Mapping[str, Any] | None:
        """The workspace's own secret granted to `mcp:<name>` in the `auth`
        slot: the enabled grant of THIS tenant, its attestation admitted, on a
        record `owned_by` this tenant, of a kind the slot reads. Never a
        personal secret's opt-in: an MCP server is the workspace's. With more
        than one, the first by the secret's name, so the choice is stable."""
        consumer = consumer_name("mcp", name)
        policy = GrantPolicy.from_settings(self.ctx.settings)
        kinds = MCP_SECRET_SLOTS["auth"].kinds
        query = self.ctx.db.collection(GRANTS_COLLECTION) \
            .where(filter=FieldFilter("tenant_id", "==", tenant_id)) \
            .where(filter=FieldFilter("consumer", "==", consumer)) \
            .where(filter=FieldFilter("enabled", "==", True))
        found: list[Mapping[str, Any]] = []
        for snap in query.stream():
            grant = snap.to_dict() or {}
            if (grant.get("tenant_id") != tenant_id or grant.get("consumer") != consumer
                    or grant.get("enabled") is not True or grant.get("slot") != "auth"
                    or not policy.admits(grant, snap.id)):
                continue
            secret_id = str(grant.get("secret_id") or "")
            record_snap = self.ctx.db.collection(SECRETS_COLLECTION).document(secret_id).get()
            record = (record_snap.to_dict() or {}) if record_snap.exists else {}
            if (record.get("secret_id") == secret_id
                    and owned_by(record, tenant_id, self.ctx.settings.project_id)
                    and record.get("kind") in kinds):
                found.append(record)
        return min(found, key=lambda r: str(r.get("name"))) if found else None

    # -- step 1 ------------------------------------------------------------------

    def _prove(self, attempt_id: str, generation: int, *, raw: bytes,
               authorization: str | None, proof: str | None, timestamp: str | None,
               path: str) -> AttemptTuple:
        unproven = AttachUnproven(UNPROVEN_MESSAGE)
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
                attach_message("POST", path, raw, timestamp)):
            raise unproven
        return attempt

    # -- steps 2, 3 and 7 ------------------------------------------------------------

    def _refs(self, attempt: AttemptTuple) -> tuple[Any, Any, Any]:
        db = self.ctx.db
        return (db.collection(TASKS).document(attempt.task_id),
                db.collection(LEASES).document(attempt.lease_id),
                db.collection(ATTACH_MARKS_COLLECTION).document(attempt.attempt_id))

    def _fenced(self, attempt: AttemptTuple, *, write: Callable[[Any], None] | None
                ) -> dict[str, Any]:
        now = self.now()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[str, dict[str, Any]]:
            task_ref, lease_ref, mark_ref = self._refs(attempt)
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            mark_snap = _snapshot(txn.get(mark_ref))
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            if (task is None
                    or task.get("state") not in (TaskState.STARTING.value,
                                                 TaskState.RUNNING.value)
                    or not _lease_is_live(task, lease, attempt, now)):
                return "fenced", {}
            if mark_snap.exists:
                return "spent", {}
            if write is not None:
                write(txn)
            return "ok", task

        outcome, task = _apply(self.ctx.db.transaction())
        if outcome == "fenced":
            raise AttachFenced("this attempt is not STARTING or RUNNING under its live lease "
                               "at this generation; nothing was attached")
        if outcome == "spent":
            raise AttachSpent("this attempt's MCP servers were already attached; an attach "
                              "is once per attempt, and the next attempt gets its own")
        task.setdefault("id", attempt.task_id)
        return task

    def _record(self, attempt: AttemptTuple, attached: list[_Attached],
                withheld: list[dict[str, str]]) -> None:
        db = self.ctx.db
        at = self.now()
        task_snap = db.collection(TASKS).document(attempt.task_id).get()
        submitted_by = (task_snap.to_dict() or {}).get("submitted_by") if task_snap.exists \
            else None

        def write(txn: Any) -> None:
            _, _, mark_ref = self._refs(attempt)
            txn.set(mark_ref, {
                "attempt_id": attempt.attempt_id, "task_id": attempt.task_id,
                "tenant_id": attempt.tenant_id, "generation": attempt.generation,
                "at": at, "expire_at": at + RELEASE_RETENTION, "attached": len(attached),
                "withheld": [[w["name"], w["reason"]] for w in withheld],
            })
            for item in attached:
                reg = item.registration
                row_id = f"{attempt.attempt_id}~{reg.get('server_id')}"
                txn.set(db.collection(ATTACH_ROWS_COLLECTION).document(row_id), {
                    "at": at, "expire_at": at + RELEASE_RETENTION,
                    "task_id": attempt.task_id, "attempt_id": attempt.attempt_id,
                    "generation": attempt.generation, "tenant_id": attempt.tenant_id,
                    "server_id": reg.get("server_id"), "name": reg.get("name"),
                    "revision": reg.get("revision"),
                    "auth_mode": (reg.get("auth") or {}).get("mode"),
                    "secret_id": item.secret_id, "version": item.version,
                    "submitted_by": submitted_by,
                })

        self._fenced(attempt, write=write)


def mcp_attach(ctx: AppContext = Depends(get_context),
               reader: SecretReader = Depends(secret_reader)) -> McpAttach:
    children = ChildService(
        settings=ctx.settings, db=ctx.db, store=ctx.store, submissions=ctx.submissions,
        verifier=_worker_verifier(ctx), limiter=ctx.limiter, metrics=ctx.metrics, now=ctx.now,
    )
    return McpAttach(ctx=ctx, children=children, reader=reader, now=ctx.now)


@router.post(ATTACH_ROUTE)
async def attach_mcp_servers(
    attempt_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    serverless_authorization: str | None = Header(
        default=None, alias="X-Serverless-Authorization"
    ),
    proof: str | None = Header(default=None, alias=PROOF_HEADER),
    timestamp: str | None = Header(default=None, alias=TIMESTAMP_HEADER),
    service: McpAttach = Depends(mcp_attach),
) -> JSONResponse:
    raw = await request.body()
    answer = await run_in_threadpool(
        lambda: service.attach(
            attempt_id, raw, authorization=_presented(authorization, serverless_authorization),
            proof=proof, timestamp=timestamp, path=request.url.path,
        )
    )
    # No cache, anywhere between here and the worker.
    return JSONResponse(answer, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
