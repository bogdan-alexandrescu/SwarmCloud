"""Child tasks: a running agent submits helpers THROUGH ITS WORKER.

docs/design/child-tasks.md is the design this implements, and the brief it
was built from; read its §3 (flows) and §6 (routes) before changing anything
here. In short:

  * `register_key` is §6.1a: before its agent exists, a worker registers the
    Ed25519 public key it generated in its non-dumpable heap, spending the
    one-use nonce the scheduler passed at dispatch. First wins, once per
    generation, only while the task is STARTING with this live lease.
  * `submit` is §6.1: the worker relays one child request its agent spooled,
    signed with that key. ONE transaction re-reads the parent and its lease
    (invariant 5), checks depth and fan-out, dedupes on the request id, and
    creates the child READY -- costing nothing until admission leases it
    (invariant 1).
  * `list_children` is §6.2: what a resumed worker stages before its agent
    starts again.
  * `cascade` is §3.4 step 1: cancelling a parent cancels its non-terminal
    children, tenant-scoped, without ever releasing capacity.

WHO MAY CALL. The ID token proves the TENANT (its worker service account,
derived from the tenant id by `identity.worker_service_account_id`, never read
from `tenants/<id>`, which a tenant identity can rewrite). The attempt proof
proves the WORKER: an agent can mint the same token from the metadata server
and read the whole container environment, but not its worker's heap, and it
was spawned only after the key was registered. A person is refused here,
tenant admins included: a person has no attempt.

Nothing here logs, stores or serves the token, the nonce, the attestation, the
registration id or `swarm-child-key`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Literal

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from swarm_common.admission import _snapshot
from swarm_common.identity import AuthError, worker_service_account_id
from swarm_common.models import EndCause, Task, TaskEvent, new_id, utcnow
from swarm_common.states import (
    PENDING_STATES,
    TERMINAL_STATES,
    EventType,
    TaskState,
    assert_transition,
)

from . import gitidentity
from .auth import bearer_tokens
from .childkey import (
    CHILD_KEYS,
    WORKER_UNPROTECTED,
    AttemptTuple,
    ChildKeys,
    request_message,
    valid_public_key,
    verify_proof,
)
from .codec import event_to_firestore, task_from_dict, task_to_firestore
from .errors import ApiError, Forbidden, ValidationFailed
from .schemas import TaskCreate
from .executioncancel import ExecutionTarget
from .store import EVENTS, LEASES, TASKS, Store, first_cancel_target
from .validation import (
    CHILD_CASCADE_METADATA_KEY,
    CHILD_REQUEST_ID_METADATA_KEY,
    resolve_dispatch_options,
)

log = logging.getLogger(__name__)

#: The agent's request id (§3.1 step 1). Also the spool's file name, so the
#: worker refuses anything else before reading it.
REQUEST_ID = re.compile(r"^[a-z0-9-]{1,64}$")

#: A constant, not a setting (§7): a deeper tree would need a different
#: cascade and await design, not a bigger number.
MAX_CHILD_DEPTH = 1

#: The proof headers (§3.2 step 5).
PROOF_HEADER = "X-Swarm-Attempt-Proof"
TIMESTAMP_HEADER = "X-Swarm-Attempt-Timestamp"

#: The cascade's reasons (§3.4), event detail under `EndCause.CHILD_CASCADE`.
PARENT_CANCELLED = "parent_cancelled"
PARENT_ENDED = "parent_ended"
AWAIT_EXPIRED = "await_expired"

#: How many children a cascade or a listing reads at most. The route caps a
#: task's children at `max_children_per_task`; this is the backstop for a
#: setting raised after children were made, and for a child document whose
#: `parent_task_id` a tenant rewrote to point here (§5 F11).
_LIST_CAP = 200


# --------------------------------------------------------------------------
# Refusals: a stable code for each answer in §6.1 and §6.1a
# --------------------------------------------------------------------------


class ChildUnavailable(ApiError):
    status_code = 503
    code = "child_submit_unavailable"


class ChildUnauthenticated(ApiError):
    status_code = 403
    code = "child_submit_unauthenticated"


class ChildUnproven(ApiError):
    status_code = 403
    code = "child_submit_unproven"


class ChildFenced(ApiError):
    status_code = 409
    code = "child_submit_fenced"


class ChildDepthExceeded(ApiError):
    status_code = 409
    code = "child_depth_exceeded"


class ChildFanOutExceeded(ApiError):
    status_code = 409
    code = "child_fan_out_exceeded"


class ChildKeyUnproven(ApiError):
    status_code = 403
    code = "child_key_unproven"


class ChildKeyTaken(ApiError):
    status_code = 409
    code = "child_key_taken"


class ChildKeyWindowClosed(ApiError):
    status_code = 409
    code = "child_key_window_closed"


# --------------------------------------------------------------------------
# Bodies. Strict: an unknown key is a 422 (invariants 7 and 10).
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChildSpec(_Strict):
    """What an agent may say about a child. Nothing else is accepted (§6.1).

    No image, command, resource figure, backend parameter, repository URL,
    priority, metadata, parent or workflow field exists here, so none can be
    sent. `repository_url` and `priority` are the parent's (§6.4).
    """

    runner_profile: str = Field(min_length=1, max_length=64)
    resource_class: str | None = Field(default=None, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    repository_ref: str | None = Field(default=None, max_length=256)


class _AttemptFields(_Strict):
    tenant_id: str = Field(min_length=1, max_length=64)
    attempt_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)


class ChildSubmit(_AttemptFields):
    request_id: str = Field(min_length=1, max_length=64)
    child: ChildSpec


class ChildKeyRegistration(_Strict):
    tenant_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    nonce: str = Field(min_length=1, max_length=128)
    public_key: str | None = Field(default=None, max_length=64)
    #: The tombstone's shape (§6.1a): `"key": null` with a refusal.
    key: None = None
    refused: Literal["worker_unprotected"] | None = None


def parse_body(model: type[BaseModel], raw: bytes) -> Any:
    """The route reads the RAW body (the proof signs its bytes), so it parses here."""
    try:
        data = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValidationFailed("request body is not JSON") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = [
            {
                "loc": [str(p) for p in (e.get("loc") or ())],
                "msg": e.get("msg"),
                "type": e.get("type"),
            }
            for e in exc.errors()
        ]
        raise ValidationFailed("request body failed validation", detail={"errors": errors}) from None


@dataclass(frozen=True)
class SubmitAnswer:
    task: Task
    created: bool


def worker_email(tenant_id: str, project_id: str) -> str:
    """The tenant's worker service account, by the rule Terraform names it by."""
    return f"{worker_service_account_id(tenant_id)}@{project_id}.iam.gserviceaccount.com"


def _lease_is_live(
    task: dict[str, Any] | None,
    lease: dict[str, Any] | None,
    attempt: AttemptTuple,
    now: datetime,
) -> bool:
    """Is `lease` this attempt's live lease on `task`? Invariant 5, as §4 lists it."""
    if task is None or lease is None:
        return False
    if task.get("tenant_id") != attempt.tenant_id or lease.get("tenant_id") != attempt.tenant_id:
        return False
    if task.get("current_lease_id") != attempt.lease_id:
        return False
    if int(task.get("current_generation") or 0) != attempt.generation:
        return False
    if lease.get("task_id") != attempt.task_id or lease.get("attempt_id") != attempt.attempt_id:
        return False
    if int(lease.get("generation") or -1) != attempt.generation:
        return False
    if lease.get("released_at") is not None:
        return False
    expires = lease.get("expires_at")
    if isinstance(expires, datetime) and expires <= now:
        return False
    return True


class ChildService:
    """The child routes' logic. Built per request from the app context."""

    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        store: Store,
        submissions: Any,
        verifier: Any,
        limiter: Any = None,
        metrics: Any = None,
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._metrics = metrics
        self._db = db
        self._store = store
        self._submissions = submissions
        self._verifier = verifier
        self._limiter = limiter
        self._now = now
        self._clock = clock
        self._keys = ChildKeys(
            getattr(settings, "child_key", ""), getattr(settings, "child_key_previous", "")
        )

    # -- authentication ----------------------------------------------------

    def _require_configured(self) -> None:
        if not self._keys.configured:
            raise ChildUnavailable(
                "this deployment has no swarm-child-key, so it offers no child path"
            )

    def authenticate_worker(self, authorization: str | None, tenant_id: str) -> str:
        """§6.1 step 1: the tenant's worker service account, or 403.

        Every bearer credential in the header is tried, as `current_auth` does,
        because Cloud Run may add its own. The refusal says which rule failed
        and never echoes a token.
        """
        expected = worker_email(tenant_id, self._settings.project_id)
        try:
            tokens = bearer_tokens(authorization)
        except ApiError:
            raise ChildUnauthenticated("a worker ID token is required") from None
        for token in tokens:
            try:
                claims = self._verifier.verify(token)
            except AuthError:
                continue
            except Exception:  # a verifier fault is a refusal, never a pass
                continue
            if claims.get("email_verified") is not True:
                continue
            if str(claims.get("email") or "").lower() != expected:
                continue
            if self._limiter is not None:
                self._limiter.check(expected)
            return expected
        raise ChildUnauthenticated(
            "only this tenant's worker service account may call this route; a person "
            "has no attempt"
        )

    def _registration(self, attempt: AttemptTuple) -> dict[str, Any] | None:
        """The trusted registration for `attempt`: attested, matching, with a key."""
        for reg_id in self._keys.registration_ids(attempt):
            snap = self._db.collection(CHILD_KEYS).document(reg_id).get()
            if not snap.exists:
                continue
            doc = snap.to_dict() or {}
            if not attempt.matches(doc) or not self._keys.attestation_holds(attempt, doc):
                # A rewritten registration: a denial for this attempt, never a
                # key of the writer's choosing (§3.2 step 4).
                return None
            if not valid_public_key(doc.get("public_key")):
                return None  # a tombstone (§5 F12)
            return doc
        return None

    def verify_proof(
        self,
        attempt: AttemptTuple,
        *,
        method: str,
        path: str,
        body: bytes,
        proof: str | None,
        timestamp: str | None,
    ) -> None:
        """§6.1 step 2. Every failure is the same 403, so none is an oracle."""
        unproven = ChildUnproven("the attempt proof did not verify for this attempt")
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        skew = int(getattr(self._settings, "child_proof_skew_seconds", 120))
        if abs(self._clock() - int(timestamp)) > skew:
            raise unproven
        registration = self._registration(attempt)
        if registration is None:
            raise unproven
        message = request_message(method, path, body, timestamp)
        if not verify_proof(registration["public_key"], proof, message):
            raise unproven

    # -- §6.1a: the registration ------------------------------------------

    def register_key(self, authorization: str | None, attempt_id: str, raw: bytes) -> dict:
        self._require_configured()
        body: ChildKeyRegistration = parse_body(ChildKeyRegistration, raw)
        self.authenticate_worker(authorization, body.tenant_id)
        attempt = AttemptTuple(
            tenant_id=body.tenant_id,
            task_id=body.task_id,
            attempt_id=attempt_id,
            lease_id=body.lease_id,
            generation=body.generation,
        )
        if not self._keys.verify_nonce(attempt, body.nonce):
            raise ChildKeyUnproven("the registration nonce does not verify for this attempt")
        if body.public_key is not None:
            if body.refused is not None or not valid_public_key(body.public_key):
                raise ValidationFailed("public_key must be a base64url Ed25519 public key")
            public_key, refused = body.public_key, None
        elif body.refused == WORKER_UNPROTECTED:
            public_key, refused = None, WORKER_UNPROTECTED
        else:
            raise ValidationFailed("send a public_key, or key null with refused")

        task_ref = self._db.collection(TASKS).document(attempt.task_id)
        lease_ref = self._db.collection(LEASES).document(attempt.lease_id)
        slots = [self._db.collection(CHILD_KEYS).document(i) for i in self._keys.registration_ids(attempt)]
        now = self._now()
        attestation = self._keys.attest(attempt, public_key, refused)

        @firestore.transactional
        def _apply(txn: Any) -> None:
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            slot_snaps = [_snapshot(txn.get(ref)) for ref in slots]
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            if any(s.exists for s in slot_snaps):
                raise ChildKeyTaken("this attempt's child key is already registered")
            if (
                task is None
                or task.get("state") != TaskState.STARTING.value
                or not _lease_is_live(task, lease, attempt, now)
            ):
                raise ChildKeyWindowClosed(
                    "a child key is registered only while the task is STARTING under "
                    "this attempt's live lease"
                )
            txn.set(
                slots[0],
                {
                    "tenant_id": attempt.tenant_id,
                    "task_id": attempt.task_id,
                    "attempt_id": attempt.attempt_id,
                    "lease_id": attempt.lease_id,
                    "generation": attempt.generation,
                    "public_key": public_key,
                    "refused": refused,
                    "attestation": attestation,
                    "created_at": now,
                },
            )

        _apply(self._db.transaction())
        # Nothing secret is returned: not the nonce, the attestation or the registration id.
        return {"registered": "key" if public_key else "tombstone", "attempt_id": attempt_id}

    # -- §6.1: the submission ---------------------------------------------

    def submit(
        self,
        authorization: str | None,
        parent_task_id: str,
        raw: bytes,
        *,
        path: str,
        proof: str | None,
        timestamp: str | None,
    ) -> SubmitAnswer:
        self._require_configured()
        # max_child_request_bytes (§7): the input's own bound, plus the
        # envelope. The worker refuses a larger file before reading it.
        limit = int(self._settings.core.max_input_bytes) + 8192
        if len(raw) > limit:
            raise ValidationFailed(f"a child request is at most {limit} bytes")
        body: ChildSubmit = parse_body(ChildSubmit, raw)
        self.authenticate_worker(authorization, body.tenant_id)
        attempt = AttemptTuple(
            tenant_id=body.tenant_id,
            task_id=parent_task_id,
            attempt_id=body.attempt_id,
            lease_id=body.lease_id,
            generation=body.generation,
        )
        self.verify_proof(
            attempt, method="POST", path=path, body=raw, proof=proof, timestamp=timestamp
        )
        if not REQUEST_ID.fullmatch(body.request_id):
            raise ValidationFailed("request_id must match [a-z0-9-]{1,64}")

        parent = self._read_parent(attempt)
        child = self._build_child(parent, body)

        task_ref = self._db.collection(TASKS).document(parent_task_id)
        lease_ref = self._db.collection(LEASES).document(attempt.lease_id)
        children_query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", attempt.tenant_id))
            .where(filter=FieldFilter("parent_task_id", "==", parent_task_id))
            .limit(_LIST_CAP)
        )
        cap = int(getattr(self._settings, "max_children_per_task", 16))
        now = self._now()
        event = TaskEvent(
            event_id=new_id("ev"),
            task_id=child.id,
            tenant_id=child.tenant_id,
            type=EventType.SUBMITTED,
            at=now,
            detail={
                "runner_profile": child.runner_profile,
                "resource_class": child.resource_class,
                "state": child.state.value,
                "workflow_id": None,
                "submitted_by": child.submitted_by,
                "via": "worker",
                "parent_task_id": parent_task_id,
                "parent_attempt_id": attempt.attempt_id,
                "request_id": body.request_id,
            },
        )

        @firestore.transactional
        def _apply(txn: Any) -> SubmitAnswer:
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            siblings = [s.to_dict() or {} for s in txn.get(children_query)]
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            if (
                task is None
                or task.get("state") != TaskState.RUNNING.value
                or task.get("cancel_requested")
                or not _lease_is_live(task, lease, attempt, now)
            ):
                raise ChildFenced(
                    "the parent is not RUNNING under this attempt's live lease, or a "
                    "cancel was requested; nothing was created"
                )
            if task.get("parent_task_id"):
                raise ChildDepthExceeded(
                    f"a child may not submit children: the depth limit is {MAX_CHILD_DEPTH}"
                )
            mine = [
                d for d in siblings
                if d.get("tenant_id") == attempt.tenant_id
                and d.get("parent_task_id") == parent_task_id
            ]
            for existing in mine:
                metadata = existing.get("metadata") or {}
                if metadata.get(CHILD_REQUEST_ID_METADATA_KEY) == body.request_id:
                    return SubmitAnswer(task=task_from_dict(existing), created=False)
            if len(mine) >= cap:
                raise ChildFanOutExceeded(
                    f"this task already has {len(mine)} children; the cap is {cap} "
                    "across all its attempts"
                )
            child_ref = self._db.collection(TASKS).document(child.id)
            txn.set(child_ref, task_to_firestore(child))
            txn.set(
                child_ref.collection(EVENTS).document(event.event_id),
                event_to_firestore(event),
            )
            return SubmitAnswer(task=child, created=True)

        answer = _apply(self._db.transaction())
        if answer.created and self._metrics is not None:
            # A child is a submitted task like any other, counted under its own
            # tenant and profile as `POST /v1/tasks` counts one; a dedupe
            # answer (created=False) made nothing and counts nothing.
            self._metrics.tasks_submitted.labels(
                tenant=answer.task.tenant_id, runner_profile=answer.task.runner_profile
            ).inc()
        return answer

    def _read_parent(self, attempt: AttemptTuple) -> Task:
        """The parent as stored, for building the child. The fence is checked
        again inside the creating transaction; this read only shapes the child."""
        snap = self._db.collection(TASKS).document(attempt.task_id).get()
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != attempt.tenant_id:
            raise ChildFenced("the parent is not this attempt's task; nothing was created")
        if data.get("parent_task_id"):
            raise ChildDepthExceeded(
                f"a child may not submit children: the depth limit is {MAX_CHILD_DEPTH}"
            )
        return task_from_dict(data)

    def _build_child(self, parent: Task, body: ChildSubmit) -> Task:
        """The child, validated by the same code `POST /v1/tasks` uses, then signed.

        The platform decides everything the agent may not: the tenant, the
        submitter (the parent's, so `submission_scope` shows a person their own
        tree), the priority, the repository URL and the parent fields. The
        timeout is clamped to the parent's own, so a child cannot buy a longer
        execution than the task that asked for it (invariant 7).
        """
        spec = body.child
        tenant = self._store.get_tenant(parent.tenant_id)
        if tenant is None or not tenant.enabled:
            raise Forbidden(f"tenant {parent.tenant_id!r} is missing or disabled")
        from .validation import validate_runner_profile

        profile = validate_runner_profile(spec.runner_profile)
        if profile.worker_action is not None:
            raise ValidationFailed(
                f"runner_profile {profile.name!r} acts on a forge with a credential no "
                "agent may steer, so it cannot be a child",
                detail={"runner_profile": profile.name, "worker_action": True},
            )
        if spec.provider is not None and spec.provider != profile.provider:
            raise ValidationFailed(
                f"runner_profile {profile.name!r} runs on provider {profile.provider!r}, "
                f"not {spec.provider!r}; the provider is the profile's",
                detail={"runner_profile": profile.name, "provider": profile.provider},
            )
        timeout = spec.timeout_seconds
        if timeout is None or timeout > parent.timeout_seconds:
            timeout = parent.timeout_seconds
        create = TaskCreate(
            runner_profile=spec.runner_profile,
            input=spec.input,
            model=spec.model,
            timeout_seconds=timeout,
        )
        repository_ref = spec.repository_ref or parent.repository_ref
        task = self._submissions._build_task(
            spec=create,
            tenant=tenant,
            ctx=None,
            submitted_by=parent.submitted_by,
            # The person the parent's commits name, so a child's commits do
            # too (P37); its bare submitter, or the bot, when it records none.
            git_identity=(
                gitidentity.from_task(parent.metadata, parent.submitted_by)
                or dict(gitidentity.BOT_IDENTITY)
            ),
            now=self._now(),
            dispatch=resolve_dispatch_options(
                strategy=create.strategy,
                carrier=create.carrier,
                scale="task",
                repository_url=parent.repository_url,
            ),
            resource_class_override=spec.resource_class,
            priority=parent.priority,
            repository_url=parent.repository_url,
            repository_ref=repository_ref,
        )
        # A child's timeout is never above its parent's, whatever the profile.
        task = replace(
            task,
            timeout_seconds=min(task.timeout_seconds, parent.timeout_seconds),
            parent_task_id=parent.id,
            parent_attempt_id=body.attempt_id,
            # The parent's GitHub credential and access mode (#780 OB7): the
            # same submitter on the same repository, resolved and signed when
            # the parent was submitted. Inherited, never widened: a child of a
            # read grant cannot push, and the worker re-reads the grant itself
            # before cloning (OB5).
            forge_credential=parent.forge_credential,
            forge_access=parent.forge_access,
        )
        task.metadata[CHILD_REQUEST_ID_METADATA_KEY] = body.request_id
        # Signed over the task as it will be stored, parent fields included
        # (contract request 42).
        self._submissions._sign([task])
        return task

    # -- §6.2: the listing for a resumed worker ----------------------------

    def list_children(
        self,
        authorization: str | None,
        parent_task_id: str,
        *,
        tenant_id: str,
        attempt_id: str,
        lease_id: str,
        generation: int,
        path: str,
        proof: str | None,
        timestamp: str | None,
    ) -> list[dict[str, Any]]:
        self._require_configured()
        self.authenticate_worker(authorization, tenant_id)
        attempt = AttemptTuple(
            tenant_id=tenant_id,
            task_id=parent_task_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            generation=generation,
        )
        self.verify_proof(
            attempt, method="GET", path=path, body=b"", proof=proof, timestamp=timestamp
        )
        task = self._db.collection(TASKS).document(parent_task_id).get()
        lease = self._db.collection(LEASES).document(lease_id).get()
        if not _lease_is_live(
            task.to_dict() if task.exists else None,
            lease.to_dict() if lease.exists else None,
            attempt,
            self._now(),
        ):
            raise ChildFenced("the parent is not held under this attempt's live lease")
        out = []
        for child in self.children_of(tenant_id, parent_task_id):
            manifest = Store.artifact_manifest(child)
            out.append(
                {
                    "task_id": child.id,
                    "request_id": child.metadata.get(CHILD_REQUEST_ID_METADATA_KEY),
                    "state": child.state.value,
                    "end_cause": child.end_cause.value if child.end_cause else None,
                    "parent_attempt_id": child.parent_attempt_id,
                    "artifacts": [
                        {k: e.get(k) for k in ("name", "uri", "bytes") if k in e}
                        for e in manifest.artifacts
                    ]
                    if child.state is TaskState.SUCCEEDED
                    else [],
                    "manifest_complete": manifest.complete,
                }
            )
        return out

    def children_of(self, tenant_id: str, parent_task_id: str) -> list[Task]:
        """Every child of a parent, in its tenant only (§3.4 step 4, §5 F11)."""
        query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("parent_task_id", "==", parent_task_id))
            .limit(_LIST_CAP)
        )
        rows = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            if data.get("tenant_id") != tenant_id or data.get("parent_task_id") != parent_task_id:
                continue
            rows.append(task_from_dict(data))
        rows.sort(key=lambda t: (t.created_at, t.id))
        return rows

    # -- §3.4: the cancel cascade -----------------------------------------

    def cascade(
        self,
        tenant_id: str,
        parent_task_id: str,
        *,
        why: str,
        by: str,
        targets: list[ExecutionTarget] | None = None,
        ended: list[str] | None = None,
    ) -> int:
        """Cancel `parent`'s non-terminal children. Returns how many were changed.

        Called after the parent's own cancel committed. Each child goes through
        one transaction of its own, `request_cancel`'s shape: a child holding
        no capacity becomes CANCELLED with `EndCause.CHILD_CASCADE`; one that
        holds capacity is flagged, and keeps its lease until its worker or the
        reconciler releases it (invariant 1). The scheduler's sweep makes this
        certain if this process dies part-way.

        `targets`, when given, collects each newly flagged child's execution
        for the route to ask to be stopped, as the parent's own (#627).
        `ended`, when given, collects the id of each child this call made
        CANCELLED, for the route to ring the finish wake for (#636).
        """
        changed = 0
        for child in self.children_of(tenant_id, parent_task_id):
            if child.state in TERMINAL_STATES:
                continue
            try:
                if cascade_cancel_child(
                    self._db,
                    child.id,
                    tenant_id=tenant_id,
                    parent_task_id=parent_task_id,
                    why=why,
                    by=by,
                    now=self._now(),
                    targets=targets,
                    ended=ended,
                ):
                    changed += 1
            except Exception:  # one child's failure must not strand its siblings
                log.exception(
                    "child cascade failed task=%s parent=%s; the scheduler sweep retries it",
                    child.id,
                    parent_task_id,
                )
        return changed


def cascade_children(
    ctx: Any,
    parent: Task,
    *,
    why: str,
    by: str,
    targets: list[ExecutionTarget] | None = None,
    ended: list[str] | None = None,
) -> int:
    """The cancel route's cascade (§3.4 step 1). Never fails the parent's cancel,
    which has already committed: a failure here is logged, and the scheduler's
    sweep cancels whatever this left. `ended` receives the ids of the children
    it made CANCELLED (`ChildService.cascade`)."""
    try:
        return ChildService(
            settings=ctx.settings,
            db=ctx.db,
            store=ctx.store,
            submissions=ctx.submissions,
            verifier=None,
            now=ctx.now,
        ).cascade(
            parent.tenant_id, parent.id, why=why, by=by, targets=targets, ended=ended
        )
    except Exception:
        log.exception("child cascade of parent=%s failed; the scheduler sweep retries it", parent.id)
        return 0


def cascade_cancel_child(
    db: Any,
    child_id: str,
    *,
    tenant_id: str,
    parent_task_id: str,
    why: str,
    by: str,
    now: datetime,
    targets: list[ExecutionTarget] | None = None,
    ended: list[str] | None = None,
) -> bool:
    """One child, one transaction. True when it wrote anything.

    `targets`, when given, receives the child's execution on its FIRST cancel
    (`store.first_cancel_target`, #627), only once the transaction committed.
    `ended`, when given, receives `child_id` when that commit made the child
    CANCELLED -- it held no capacity, so no worker will ring the finish wake
    for it (#636) -- and nothing when it only flagged a live one.

    Tenant first: a child whose `tenant_id` is not its parent's, or whose
    `parent_task_id` no longer names this parent, is left alone and logged.
    """
    ref = db.collection(TASKS).document(child_id)

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if data is None:
            return False
        if data.get("tenant_id") != tenant_id or data.get("parent_task_id") != parent_task_id:
            log.warning(
                "child cascade skipped task=%s: it does not belong to parent=%s in its tenant",
                child_id,
                parent_task_id,
            )
            return False
        state = TaskState(data["state"])
        if state in TERMINAL_STATES:
            return False
        metadata = dict(data.get("metadata") or {})
        if data.get("cancel_requested") and metadata.get(CHILD_CASCADE_METADATA_KEY):
            return False
        # Read before any write, as a transaction requires.
        found[:] = [
            first_cancel_target(db, txn, data, tenant_id=tenant_id, task_id=child_id)
        ]
        metadata[CHILD_CASCADE_METADATA_KEY] = {"why": why, "parent_task_id": parent_task_id}
        patch: dict[str, Any] = {
            "cancel_requested": True,
            "metadata": metadata,
            "updated_at": now,
        }
        immediate = state in PENDING_STATES
        made_terminal[:] = [immediate]
        if immediate:
            assert_transition(state, TaskState.CANCELLED)
            patch.update(
                {
                    "state": TaskState.CANCELLED.value,
                    "completed_at": now,
                    "end_cause": EndCause.CHILD_CASCADE.value,
                    "park_reason": None,
                    "blocked_by": [],
                    "next_eligible_at": None,
                    "last_error": f"cancelled because of its parent {parent_task_id}: {why}",
                }
            )
        txn.update(ref, patch)
        event = TaskEvent(
            event_id=new_id("ev"),
            task_id=child_id,
            tenant_id=tenant_id,
            type=EventType.CANCELLED if immediate else EventType.CANCEL_REQUESTED,
            at=now,
            detail={
                "requested_by": f"cascade:{parent_task_id}",
                "by": by,
                "why": why,
                "from_state": state.value,
                "phase": "cancelled" if immediate else "cancel_requested",
            },
        )
        txn.set(ref.collection(EVENTS).document(event.event_id), event_to_firestore(event))
        return True

    # Set by the attempt that commits: a retried transaction overwrites it.
    found: list[ExecutionTarget | None] = []
    made_terminal: list[bool] = []
    wrote = _apply(db.transaction())
    if wrote and targets is not None and found and found[0] is not None:
        targets.append(found[0])
    if wrote and ended is not None and made_terminal and made_terminal[0]:
        ended.append(child_id)
    return wrote
