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
  * the RUN KINDS are docs/design/agent-autonomy.md §3.6 (lane AU-6): a child
    `issue_run` (an issue NUMBER in the parent's own repository, started by
    `routes.runs.start_run` as the parent's submitter, optionally HELD
    `after_merged` another of the parent's runs) and a child `workflow` (a
    saved custom-prompt spec BY NAME whose digest a platform admin approved,
    never a body). Their link to the parent lives in swarm-api's own ledger,
    `child_runs`, which needs no frozen field; they count toward the parent's
    `max_children_per_task` and obey the depth rule exactly as a task child.

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

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Annotated, Any, Callable, Literal, Union

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StrictInt,
    Tag,
    ValidationError,
)

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
    PURPOSE_CHILD,
    PURPOSE_STATE,
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
from .store import EVENTS, LEASES, TASKS, WORKFLOWS, Store, first_cancel_target
from .validation import (
    CHILD_CASCADE_METADATA_KEY,
    CHILD_REQUEST_ID_METADATA_KEY,
    mcp_server_names,
    resolve_dispatch_options,
    timeout_asked,
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

#: The lowest spec format whose signature covers `forge_credential` and
#: `forge_access` (`swarm_common.specsign._FORMAT_3_FIELDS`; the broker's
#: `credbroker.MIN_SPEC_FORMAT`, which imports this module).
_FORGE_SPEC_FORMAT = 3

#: The child kinds (agent-autonomy.md §3.6). `agent` is today's task child and
#: the kind of a request that names none.
KIND_AGENT = "agent"
KIND_ISSUE_RUN = "issue_run"
KIND_WORKFLOW = "workflow"

#: swarm-api's own children ledger for the run kinds: one document per child
#: issue run or workflow, keyed by (tenant, parent, request id). The parent's
#: link is recorded HERE rather than on the run or workflow document, so no
#: frozen field is needed (§3.6).
CHILD_RUNS = "child_runs"
#: A ledger row's states. HELD: an `after_merged` run waiting for its
#: predecessor -- no run, no planner, no workflow, so no capacity (invariant
#: 1). STARTING: claimed by the one request or tick that starts it. STARTED:
#: its run or workflow exists. FAILED: the start was refused or interrupted.
#: CANCELLED: it never started and never will (its predecessor ended without
#: merging, or its parent ended).
HELD = "HELD"
STARTING = "STARTING"
STARTED = "STARTED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
#: A row that will never start anything more.
LEDGER_ENDED = frozenset({FAILED, CANCELLED})
#: A STARTING row older than this was interrupted mid-start (the process died
#: between the claim and the run's id). It is FAILED, never started again: a
#: second start could make a second run of the same issue, and a parent
#: told "failed" can ask again under a new request id.
START_STALE_SECONDS = 600
#: Held rows one tick reads per tenant at most.
RELEASE_PAGE = 50
#: The mark a child workflow's steps carry in `metadata`, beside the merge
#: choice: which parent and ledger row it came from. A label, not a signed
#: field; the depth rule reads the signed `workflow_id` against the ledger.
CHILD_RUN_METADATA_KEY = "child_run"


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


class ChildParentUnsigned(ApiError):
    """The stored parent's spec signature does not verify (#1041).

    Not a switch (tests/unit/control_plane/test_new_refusals_ship_off.py):
    continuing past it is the breach -- a child signed by swarm-api over a
    submitter and forge slot the agent wrote into its own task.
    """

    status_code = 403
    code = "child_parent_unsigned"


class ChildRunOtherRepository(ApiError):
    """A child issue run names an issue outside the parent's own repository."""

    status_code = 403
    code = "child_run_other_repository"


class ChildRunNoRepository(ApiError):
    """The parent has no GitHub repository, so it has no issues to run."""

    status_code = 409
    code = "child_run_no_repository"


class ChildWorkflowUnapproved(ApiError):
    """No saved spec of this tenant by that name, or its digest is not approved."""

    status_code = 403
    code = "child_workflow_unapproved"


class ChildAfterMergedUnknown(ApiError):
    """`after_merged` names no issue run this parent started."""

    status_code = 422
    code = "child_after_merged_unknown"


class ChildRunsUnavailable(ApiError):
    """This deployment's child route was built without the context a run needs."""

    status_code = 503
    code = "child_runs_unavailable"


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

    #: Optional: a request that names no kind is an agent child, as before
    #: the run kinds existed.
    kind: Literal["agent"] = KIND_AGENT
    runner_profile: str = Field(min_length=1, max_length=64)
    resource_class: str | None = Field(default=None, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    repository_ref: str | None = Field(default=None, max_length=256)
    #: The child's model class, by name, as `TaskCreate.model_class` (#1435).
    #: None: the CHILD profile's default, never the parent's class (R3).
    model_class: str | None = Field(default=None, max_length=32)


class ChildIssueRun(_Strict):
    """A child issue run (§3.6): an issue of the PARENT'S repository, nothing more.

    `issue` is the number, or the parent's own repository spelled out
    (`owner/repo#N`, the issue's URL) -- any other repository is refused
    (`ChildRunOtherRepository`). No repository, profile, plan, gate or
    merge field exists here: the run is planned, approved and merged under
    the run's own gates, as `POST /v1/runs` makes it.
    """

    kind: Literal["issue_run"]
    issue: StrictInt | str
    #: Another issue run THIS parent started, by the `child_id` (or `run_id`)
    #: it was answered with. This run is HELD until that one is DONE with its
    #: pull request merged.
    after_merged: str | None = Field(default=None, min_length=1, max_length=128)


class ChildWorkflow(_Strict):
    """A child workflow (§3.6): a saved, admin-approved spec BY NAME.

    The name of one of the tenant's custom-prompt schedules
    (docs/schedules.md §3.12), whose spec digest a platform admin approved.
    Never a body: `steps`, `spec` or anything else is an unknown key, a 422,
    because a body is images and commands by another name (invariant 10).
    """

    kind: Literal["workflow"]
    spec_name: str = Field(min_length=1, max_length=128)


def _child_kind(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("kind") or KIND_AGENT
    return getattr(value, "kind", KIND_AGENT)


ChildRequest = Annotated[
    Union[
        Annotated[ChildSpec, Tag(KIND_AGENT)],
        Annotated[ChildIssueRun, Tag(KIND_ISSUE_RUN)],
        Annotated[ChildWorkflow, Tag(KIND_WORKFLOW)],
    ],
    Discriminator(_child_kind),
]


class _AttemptFields(_Strict):
    tenant_id: str = Field(min_length=1, max_length=64)
    attempt_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)


class ChildSubmit(_AttemptFields):
    request_id: str = Field(min_length=1, max_length=64)
    child: ChildRequest


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
    #: The task child made or found; None for a run kind.
    task: Task | None
    created: bool
    #: The ledger row of a run kind (`ledger_to_api`); None for a task child.
    child: dict[str, Any] | None = None


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
        # NOT YET ALIVE IS NOT EXPIRED. A lease is written with
        # `heartbeat_at: None` and `expires_at = created_at + 120 s`, and until
        # the worker's first beat the reconciler judges it by
        # `dispatch_deadline` alone (`reconciler/detect.py`
        # `detect_stale_leases`). The worker registers its child key, and opens
        # its live session, before that beat, so a pod that took longer than
        # 120 s to start was refused here on a lease nothing else considered
        # dead: task_23cc2e82178640709a45, 2026-10-12, 154 s from dispatch to
        # STARTING, `child_key_window_closed`, no live browser (LB-KEYRACE).
        # The fence is unchanged -- task, lease, attempt, generation and
        # `released_at` are checked above; this is only the liveness clock.
        deadline = lease.get("dispatch_deadline")
        if not (
            lease.get("heartbeat_at") is None
            and isinstance(deadline, datetime)
            and deadline > now
        ):
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
        ctx: Any = None,
    ) -> None:
        #: The app context, for the run kinds only (§3.6): starting an issue
        #: run reads GitHub and the directory through it. None refuses them
        #: with `ChildRunsUnavailable`; the agent kind never reads it.
        self._ctx = ctx
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

    def _registration(
        self, attempt: AttemptTuple, purpose: str = PURPOSE_CHILD
    ) -> dict[str, Any] | None:
        """The trusted registration for `attempt`: attested, matching, with a
        key that may sign for `purpose`.

        Every existing caller asks for CHILD, which a v1 registration and a
        protected v2 one (`state/open`, docs/design/tenant-settings-integrity.md
        §4.5) both grant. Only the state routes ask for STATE, which a v1
        registration never grants: it was attested before state proofs existed.
        """
        for reg_id in self._keys.registration_ids(attempt):
            snap = self._db.collection(CHILD_KEYS).document(reg_id).get()
            if not snap.exists:
                continue
            doc = snap.to_dict() or {}
            # A rewritten registration, a tombstone (§5 F12) or a key not
            # registered for `purpose`: a denial for this attempt, never a key
            # of the writer's choosing (§3.2 step 4).
            if purpose not in self._keys.purposes_of(attempt, doc):
                return None
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
        """§6.1a. DEPTH-AGNOSTIC ON PURPOSE: a child task's key registers too.

        The attempt key proves "the worker, not the agent" for every signed
        call, not only for submitting children: the git tools are signed with it
        (docs/runner-git-tools.md §1.4, lane G2), and the scheduler issues a
        child the nonce when its profile offers them. So nothing here reads
        `parent_task_id`. The depth limit is `submit`'s, read from the task
        document in its own transaction (`ChildDepthExceeded`); a registered key
        does not lift it. Refusing a child here would quietly take the git tools
        away from every fan-out lane.
        """
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

        now = self._now()
        self._write_registration(
            attempt,
            {
                "public_key": public_key,
                "refused": refused,
                "attestation": self._keys.attest(attempt, public_key, refused),
            },
            window=(TaskState.STARTING,),
            now=now,
        )
        # Nothing secret is returned: not the nonce, the attestation or the registration id.
        # `search_tool`: whether this worker names the search tool to its agent
        # (docs/search-tool.md). The flag, and a key to sign the calls with; the
        # route re-checks both, and the profile, on every call.
        search_tool = bool(public_key) and bool(getattr(self._settings, "search_tool_enabled", False))
        return {"registered": "key" if public_key else "tombstone", "attempt_id": attempt_id,
                "search_tool": search_tool}

    def _write_registration(
        self,
        attempt: AttemptTuple,
        fields: dict[str, Any],
        *,
        window: tuple[TaskState, ...],
        now: datetime,
        same_key: Callable[[dict[str, Any]], bool] | None = None,
    ) -> bool:
        """Write `attempt`'s registration once, in one transaction, while the
        task is in `window` under this attempt's live lease.

        Returns False when `same_key` recognises the registration already
        stored (a retried `state/open` whose answer was lost): nothing is
        written and the caller answers as if it had been. Anything else
        already stored is `ChildKeyTaken`: first wins, once per generation.
        """
        task_ref = self._db.collection(TASKS).document(attempt.task_id)
        lease_ref = self._db.collection(LEASES).document(attempt.lease_id)
        slots = [self._db.collection(CHILD_KEYS).document(i) for i in self._keys.registration_ids(attempt)]
        names = " or ".join(state.value for state in window)

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            slot_snaps = [_snapshot(txn.get(ref)) for ref in slots]
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            for snap in slot_snaps:
                if not snap.exists:
                    continue
                if same_key is not None and same_key(snap.to_dict() or {}):
                    return False
                raise ChildKeyTaken("this attempt's child key is already registered")
            if (
                task is None
                or task.get("state") not in {state.value for state in window}
                or not _lease_is_live(task, lease, attempt, now)
            ):
                raise ChildKeyWindowClosed(
                    f"a key is registered only while the task is {names} under "
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
                    **fields,
                    "created_at": now,
                },
            )
            return True

        return bool(_apply(self._db.transaction()))

    def register_state_key(
        self, attempt: AttemptTuple, *, nonce: str, public_key: str, unprotected: bool
    ) -> dict[str, Any]:
        """`state/open`'s registration (docs/design/tenant-settings-integrity.md §4.3, §4.5).

        The caller has authenticated the worker. This spends the nonce on a v2
        registration: the window is LEASED, DISPATCHED or STARTING under this
        attempt's live lease, because `state/open` is the worker's FIRST call,
        made before it writes anything. A key from a protected heap may sign
        for children and state; one from an unprotected heap (owner decision
        Q4) is flagged `unprotected` and may sign for STATE only -- such a
        worker has registered a tombstone and run without children since
        §5 F12, and this keeps that. A retry carrying the same key and flag
        is answered as the first was.
        """
        self._require_configured()
        if not self._keys.verify_nonce(attempt, nonce):
            raise ChildKeyUnproven("the registration nonce does not verify for this attempt")
        if not valid_public_key(public_key):
            raise ValidationFailed("public_key must be a base64url Ed25519 public key")
        purposes = (PURPOSE_STATE,) if unprotected else (PURPOSE_CHILD, PURPOSE_STATE)

        def _same(doc: dict[str, Any]) -> bool:
            return (
                doc.get("public_key") == public_key
                and doc.get("unprotected") is unprotected
                and self._keys.purposes_of(attempt, doc) == frozenset(purposes)
            )

        created = self._write_registration(
            attempt,
            {
                "version": 2,
                "public_key": public_key,
                "refused": None,
                "purpose": list(purposes),
                "unprotected": unprotected,
                "attestation": self._keys.attest_v2(attempt, public_key, purposes, unprotected),
            },
            window=(TaskState.LEASED, TaskState.DISPATCHED, TaskState.STARTING),
            now=self._now(),
            same_key=_same,
        )
        return {"registered": "key", "purpose": list(purposes), "unprotected": unprotected,
                "created": created}

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
        # A task of a child workflow or child issue run is a child too (§3.6):
        # it may have no children of its own, of any kind.
        self._refuse_nested(parent)
        if not isinstance(body.child, ChildSpec):
            return self._submit_run(parent, body, attempt)
        child = self._build_child(parent, body)
        cap = self._cap()
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
            mine, ledger = self._family(txn, attempt, now)
            found = _existing(mine, ledger, body.request_id)
            if found is not None:
                return found
            _check_cap(mine, ledger, cap)
            child_ref = self._db.collection(TASKS).document(child.id)
            txn.set(child_ref, task_to_firestore(child))
            txn.set(
                child_ref.collection(EVENTS).document(event.event_id),
                event_to_firestore(event),
            )
            return SubmitAnswer(task=child, created=True)

        answer = _apply(self._db.transaction())
        if answer.created and answer.task is not None and self._metrics is not None:
            # A child is a submitted task like any other, counted under its own
            # tenant and profile as `POST /v1/tasks` counts one; a dedupe
            # answer (created=False) made nothing and counts nothing.
            self._metrics.tasks_submitted.labels(
                tenant=answer.task.tenant_id, runner_profile=answer.task.runner_profile
            ).inc()
        return answer

    # -- the family: what the fan-out and the dedupe count ------------------

    def _cap(self) -> int:
        return int(getattr(self._settings, "max_children_per_task", 16))

    def _family(
        self, txn: Any, attempt: AttemptTuple, now: datetime
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Inside the creating transaction: the fence, the depth rule, and every
        child the parent has -- its task children and its ledger rows, both
        tenant-scoped -- which together are what the cap counts (§3.6: "both
        count against the parent's 16")."""
        parent_task_id = attempt.task_id
        task_ref = self._db.collection(TASKS).document(parent_task_id)
        lease_ref = self._db.collection(LEASES).document(attempt.lease_id)
        children_query = (
            self._db.collection(TASKS)
            .where(filter=FieldFilter("tenant_id", "==", attempt.tenant_id))
            .where(filter=FieldFilter("parent_task_id", "==", parent_task_id))
            .limit(_LIST_CAP)
        )
        task_snap = _snapshot(txn.get(task_ref))
        lease_snap = _snapshot(txn.get(lease_ref))
        siblings = [s.to_dict() or {} for s in txn.get(children_query)]
        rows = [s.to_dict() or {} for s in txn.get(self._ledger_query(attempt.tenant_id, parent_task_id))]
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
            if d.get("tenant_id") == attempt.tenant_id and d.get("parent_task_id") == parent_task_id
        ]
        ledger = [
            d for d in rows
            if d.get("tenant_id") == attempt.tenant_id and d.get("parent_task_id") == parent_task_id
        ]
        return mine, ledger

    def _ledger_query(self, tenant_id: str, parent_task_id: str) -> Any:
        return (
            self._db.collection(CHILD_RUNS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("parent_task_id", "==", parent_task_id))
            .limit(_LIST_CAP)
        )

    def _ledger_hit(self, tenant_id: str, field: str, value: str) -> bool:
        query = (
            self._db.collection(CHILD_RUNS)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter(field, "==", value))
            .limit(1)
        )
        return any(
            (snap.to_dict() or {}).get("tenant_id") == tenant_id for snap in query.stream()
        )

    def _refuse_nested(self, parent: Task) -> None:
        """The depth rule for the run kinds (§3.6: "a child cannot have children").

        A step of a child WORKFLOW is found by its `workflow_id`, which its
        signed spec covers. A task of a child ISSUE RUN -- its planner, its
        build workflow, a CI fix round, its merge -- is found by the run id
        its `metadata.issue_run` carries (a string on the planner, `run_id`
        inside a map on the workflows). Either one in the parent's tenant's
        ledger makes it a child: nothing is created.
        """
        tenant_id = parent.tenant_id
        nested = bool(parent.workflow_id) and self._ledger_hit(
            tenant_id, "workflow_id", parent.workflow_id or ""
        )
        run_id = _issue_run_id(parent.metadata)
        if not nested and run_id:
            nested = self._ledger_hit(tenant_id, "run_id", run_id)
        if nested:
            raise ChildDepthExceeded(
                "this task belongs to a child workflow or child issue run, and a child may "
                f"not submit children: the depth limit is {MAX_CHILD_DEPTH}"
            )

    # -- §3.6: the run kinds ------------------------------------------------

    def _submit_run(self, parent: Task, body: ChildSubmit, attempt: AttemptTuple) -> SubmitAnswer:
        """A child issue run or child workflow: checked, recorded, then started.

        Everything that can refuse without a write is asked first -- the
        repository, the spec's approval -- so a refusal leaves nothing. Then ONE
        transaction re-reads the fence and the family, dedupes on the request
        id, counts the cap and writes the ledger row: HELD for an
        `after_merged` run, else STARTING, claimed by this request. Only then is
        the run or workflow started, outside the transaction (it reads GitHub
        and submits tasks); a start that is refused deletes the row it claimed
        and answers the refusal, so the slot is not spent and the same request
        id may be sent again.
        """
        spec = body.child
        if self._ctx is None:
            raise ChildRunsUnavailable("this route was built without the context a run needs")
        now = self._now()
        row: dict[str, Any] = {
            "child_id": ledger_id(attempt.tenant_id, parent.id, body.request_id),
            "tenant_id": attempt.tenant_id,
            "parent_task_id": parent.id,
            "parent_attempt_id": attempt.attempt_id,
            "request_id": body.request_id,
            "kind": spec.kind,
            "issue": None,
            "spec_name": None,
            "schedule_id": None,
            "spec_digest": None,
            "after_merged": None,
            "state": STARTING,
            "run_id": None,
            "workflow_id": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        workflow_spec = None
        if isinstance(spec, ChildIssueRun):
            ref = _issue_of_parent(parent, spec.issue)
            row["issue"] = {"owner": ref.owner, "repo": ref.repo, "number": ref.number}
        else:
            schedule, workflow_spec, digest = self._approved_spec(attempt.tenant_id, spec.spec_name)
            row.update({"spec_name": schedule.get("name"), "schedule_id": schedule.get("schedule_id"),
                        "spec_digest": digest})
        after = spec.after_merged if isinstance(spec, ChildIssueRun) else None
        cap = self._cap()
        ref_doc = self._db.collection(CHILD_RUNS).document(row["child_id"])

        @firestore.transactional
        def _apply(txn: Any) -> SubmitAnswer:
            mine, ledger = self._family(txn, attempt, now)
            found = _existing(mine, ledger, body.request_id)
            if found is not None:
                return found
            _check_cap(mine, ledger, cap)
            if after is not None:
                before = next(
                    (r for r in ledger if r.get("kind") == KIND_ISSUE_RUN
                     and after in (r.get("child_id"), r.get("run_id"))),
                    None,
                )
                if before is None:
                    raise ChildAfterMergedUnknown(
                        "after_merged must name an issue run this task started, by the "
                        "child_id or run_id it was answered with; nothing was created"
                    )
                row["after_merged"] = before["child_id"]
                row["state"] = HELD
            txn.set(ref_doc, dict(row))
            return SubmitAnswer(task=None, child=ledger_to_api(row), created=True)

        answer = _apply(self._db.transaction())
        if not answer.created or row["state"] != STARTING:
            return answer
        try:
            started = self._start(parent, row, workflow_spec)
        except Exception:
            # Refused before anything existed: the claim goes, so the slot is
            # not spent and the agent may send the same request again.
            ref_doc.delete()
            raise
        return SubmitAnswer(task=None, child=ledger_to_api(started), created=True)

    def _approved_spec(self, tenant_id: str, name: str) -> tuple[dict[str, Any], Any, str]:
        """(the schedule, its validated spec, its digest) for a saved spec of
        THIS tenant whose digest a platform admin approved; else
        `ChildWorkflowUnapproved`, with nothing written.

        "Saved, admin-approved" is the custom-prompt rule (docs/schedules.md
        §3.12): the spec is a custom-prompt schedule's `params.spec`, by the
        schedule's name, and the approval is that schedule's `spec` approval
        OF THIS DIGEST -- a spec edited since it was approved is a new digest,
        unapproved. Nothing here asks for an approval: a child is not a
        firing, and only the schedule's own firing opens a request.
        """
        from . import approvals
        from .schedtypes import custom_prompt
        from .schedules import COLLECTION as SCHEDULES
        from .schemas import WorkflowCreate

        wanted = name.strip().casefold()
        query = (
            self._db.collection(SCHEDULES)
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("type", "==", custom_prompt.TYPE))
            .limit(_LIST_CAP)
        )
        schedule = next(
            (doc for doc in (s.to_dict() or {} for s in query.stream())
             if doc.get("tenant_id") == tenant_id and doc.get("type") == custom_prompt.TYPE
             and str(doc.get("name") or "").strip().casefold() == wanted),
            None,
        )
        if schedule is None:
            raise ChildWorkflowUnapproved(
                f"this tenant has no saved custom-prompt spec named {name!r}; a workflow "
                "child names one, and is never a spec body"
            )
        try:
            spec = WorkflowCreate.model_validate((schedule.get("params") or {}).get("spec"))
        except ValidationError:
            raise ChildWorkflowUnapproved(
                f"the saved spec {name!r} no longer validates, so it cannot run"
            ) from None
        digest = custom_prompt.spec_digest(spec)
        approval, _round = custom_prompt.find_spec_approval(self._db, schedule, digest)
        state = (approval or {}).get("state")
        if state != approvals.APPROVED:
            raise ChildWorkflowUnapproved(
                f"the saved spec {name!r} at digest {digest[:19]} is not approved by a "
                "platform admin; nothing was created",
                detail={"spec_name": name, "spec_digest": digest, "approval_state": state},
            )
        return schedule, spec, digest

    def _owner(self, parent: Task, child_id: str) -> Any:
        """Whom a child run or workflow is submitted as: the PARENT'S submitter,
        in the parent's tenant, and only while they may still act there.

        `routes.runs.run_owner_auth` is the one rule for submitting on a
        member's behalf with nobody on the call -- membership asked of the
        directory again, a narrowed role refused, a platform admin's action
        audited -- so it is asked here, over the three fields it reads of a
        run, rather than restated.
        """
        from .routes.runs import run_owner_auth

        return run_owner_auth(
            self._ctx,
            _Submitter(id=child_id, tenant_id=parent.tenant_id, created_by=parent.submitted_by),
        )

    def _start(self, parent: Task, row: dict[str, Any], workflow_spec: Any) -> dict[str, Any]:
        """Start a claimed (STARTING) row's run or workflow; record it STARTED."""
        owner = self._owner(parent, row["child_id"])
        if row["kind"] == KIND_ISSUE_RUN:
            patch = {"run_id": self._start_issue_run(owner, row)}
        else:
            patch = {"workflow_id": self._start_workflow(owner, row, workflow_spec)}
        patch.update({"state": STARTED, "updated_at": self._now(), "error": None})
        self._db.collection(CHILD_RUNS).document(row["child_id"]).update(patch)
        log.info("child %s started tenant=%s parent=%s child=%s %s", row["kind"], row["tenant_id"],
                 row["parent_task_id"], row["child_id"], patch.get("run_id") or patch.get("workflow_id"))
        return {**row, **patch}

    def _start_issue_run(self, owner: Any, row: dict[str, Any]) -> str:
        """`POST /v1/runs`'s path, as the parent's submitter: the merge choice
        the repository's registration (or the platform) gives a run that does
        not say, the open-work read whose refusal refuses the run, and
        `start_run` -- planner, run document, write-back -- under the run's
        own gates. The gates are `RunCreate`'s defaults: an agent names an
        issue, never a gate."""
        from .forge import read_open_work
        from .issueruns import auto_merge_default, refuse_auto_merge
        from .repositories import registered_merge_policy
        from .routes.runs import start_run
        from .schemas import RunCreate
        from .validation import IssueRef

        ctx = self._ctx
        issue = row["issue"]
        ref = IssueRef(owner=issue["owner"], repo=issue["repo"], number=int(issue["number"]))
        tenant = ctx.submissions.tenant_for(owner)
        defaults = RunCreate(issue=f"{ref.owner}/{ref.repo}#{ref.number}")
        auto_merge = auto_merge_default(
            registered_merge_policy(ctx.db, ctx.now, tenant.tenant_id, ref.owner, ref.repo),
            bool(ctx.store.get_platform_settings().get("merge_by_default")),
        )
        refuse_auto_merge(auto_merge)
        open_work = read_open_work(
            ref, tenant, tokens=ctx.forge_tokens, issues=ctx.forge, read_at=ctx.now(),
            person=owner.email,
        )
        run = start_run(
            ctx, owner, ref, open_work=open_work, plan_approval=defaults.plan_approval,
            auto_merge=auto_merge, fix_rounds=defaults.fix_rounds,
        )
        return run.id

    def _start_workflow(self, owner: Any, row: dict[str, Any], spec: Any) -> str:
        """The approved spec, submitted as one workflow with `merge` forced off,
        as a custom-prompt firing submits it (`custom_prompt.submitted_spec`):
        the pull request it opens waits for a person."""
        from .schedtypes import custom_prompt

        if spec is None:
            _schedule, spec, digest = self._approved_spec(row["tenant_id"], row["spec_name"])
        submitted = custom_prompt.submitted_spec(spec)
        submitted = submitted.model_copy(update={"metadata": {
            **submitted.metadata,
            CHILD_RUN_METADATA_KEY: {"child_id": row["child_id"],
                                     "parent_task_id": row["parent_task_id"]},
        }})
        result = self._ctx.submissions.submit_workflow(owner, submitted)
        return result.workflow.workflow_id

    # -- the release of held runs (the tick) --------------------------------

    def release_held(self, tenant_id: str, *, limit: int = RELEASE_PAGE) -> dict[str, int]:
        """Start each HELD run of `tenant_id` whose predecessor is DONE with its
        pull request merged; cancel each that never can start; fail each
        STARTING row a crash left behind. ONE tenant's ledger only (invariant 9).

        A held row costs nothing: no run, no planner, no workflow, so no lease
        (invariant 1). One row's failure is that row's: it is recorded FAILED
        and the rest of the page goes on.
        """
        report = {"visited": 0, "started": 0, "cancelled": 0, "failed": 0, "waiting": 0}
        if self._ctx is None:
            raise ChildRunsUnavailable("the release needs the app context")
        ledger = self._db.collection(CHILD_RUNS)
        now = self._now()
        for state in (STARTING, HELD):
            query = (
                ledger.where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("state", "==", state))
                .limit(limit)
            )
            for snap in query.stream():
                row = snap.to_dict() or {}
                if row.get("tenant_id") != tenant_id or row.get("state") != state:
                    continue
                report["visited"] += 1
                if state == STARTING:
                    since = row.get("updated_at") or row.get("created_at")
                    if since is not None and (now - since).total_seconds() > START_STALE_SECONDS:
                        self._settle(row, FAILED, "start_interrupted", from_state=STARTING)
                        report["failed"] += 1
                    continue
                outcome = self._release_one(row)
                report[outcome] += 1
        return report

    def _release_one(self, row: dict[str, Any]) -> str:
        tenant_id = row["tenant_id"]
        parent_snap = self._db.collection(TASKS).document(row["parent_task_id"]).get()
        parent_doc = parent_snap.to_dict() if parent_snap.exists else None
        if (
            parent_doc is None
            or parent_doc.get("tenant_id") != tenant_id
            or parent_doc.get("cancel_requested")
            or _task_state(parent_doc.get("state")) in TERMINAL_STATES
        ):
            self._settle(row, CANCELLED, "parent_ended", from_state=HELD)
            return "cancelled"
        verdict = self._predecessor(row)
        if verdict == "wait":
            return "waiting"
        if verdict != "start":
            self._settle(row, CANCELLED, verdict, from_state=HELD)
            return "cancelled"
        from .verdictpublish import spec_refusal

        if spec_refusal(self._settings, parent_doc, row["parent_task_id"]) is not None:
            # The submitter is read from the parent: only from one whose
            # signature verifies (#1041), as `_read_parent` insists.
            self._settle(row, CANCELLED, "parent_unsigned", from_state=HELD)
            return "cancelled"
        if not self._claim(row):
            return "waiting"
        try:
            self._start(task_from_dict(parent_doc), {**row, "state": STARTING}, None)
        except Exception as exc:  # one row's failure is that row's
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            log.warning("held child run %s tenant=%s not started (%s)", row["child_id"], tenant_id, code)
            self._settle(row, FAILED, code, from_state=STARTING)
            return "failed"
        return "started"

    def _predecessor(self, row: dict[str, Any]) -> str:
        """"start", "wait", or why the row will never start."""
        from .errors import NotFound
        from .issueruns import TERMINAL_RUN_STATES, IssueRuns, RunState

        snap = self._db.collection(CHILD_RUNS).document(row.get("after_merged") or "-").get()
        before = snap.to_dict() if snap.exists else None
        if (
            before is None
            or before.get("tenant_id") != row["tenant_id"]
            or before.get("parent_task_id") != row["parent_task_id"]
        ):
            return "predecessor_missing"
        if before.get("state") in LEDGER_ENDED:
            return "predecessor_ended"
        if before.get("state") != STARTED or not before.get("run_id"):
            return "wait"
        try:
            run = IssueRuns(self._db, now=self._now).get(row["tenant_id"], before["run_id"])
        except NotFound:
            return "predecessor_missing"
        if run.state == RunState.DONE and (run.pull_request or {}).get("merged") is True:
            return "start"
        if run.state in TERMINAL_RUN_STATES:
            # DONE without a merged pull request (green, not auto-merged; or
            # already on main) is not "merged", and nothing will merge it now.
            return "predecessor_ended_unmerged"
        return "wait"

    def _claim(self, row: dict[str, Any]) -> bool:
        """HELD -> STARTING in one transaction: exactly one tick starts a row."""
        ref = self._db.collection(CHILD_RUNS).document(row["child_id"])
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("state") != HELD or data.get("tenant_id") != row["tenant_id"]:
                return False
            txn.update(ref, {"state": STARTING, "updated_at": now})
            return True

        return _apply(self._db.transaction())

    def _settle(self, row: dict[str, Any], state: str, why: str, *, from_state: str) -> None:
        """A row that will never start: FAILED or CANCELLED, with why. Only from
        `from_state`, so a row another tick moved is left alone."""
        ref = self._db.collection(CHILD_RUNS).document(row["child_id"])
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("state") != from_state:
                return
            txn.update(ref, {"state": state, "error": why, "updated_at": now})

        _apply(self._db.transaction())
        log.info("child %s %s tenant=%s parent=%s child=%s (%s)", row.get("kind"), state.lower(),
                 row.get("tenant_id"), row.get("parent_task_id"), row.get("child_id"), why)

    def _ledger_listing(self, row: dict[str, Any]) -> dict[str, Any]:
        """A ledger row as the resumed worker lists it: its run's or workflow's
        own state once started, and whether it has ENDED -- for an issue run
        `DONE`, `FAILED`, `REJECTED`, `CANCELLED` or `NOT_READY` (§3.6), for a
        workflow a terminal task state, and for a row that never started,
        FAILED or CANCELLED. HELD and STARTING rows have not ended."""
        from .errors import NotFound
        from .issueruns import TERMINAL_RUN_STATES, IssueRuns

        out = {**ledger_to_api(row), "task_id": None, "ended": row.get("state") in LEDGER_ENDED}
        if row.get("state") != STARTED:
            return out
        tenant_id = row["tenant_id"]
        if row.get("run_id"):
            try:
                run = IssueRuns(self._db, now=self._now).get(tenant_id, row["run_id"])
            except NotFound:
                return {**out, "state": "missing", "ended": True}
            pull = run.pull_request or {}
            return {
                **out,
                "state": run.state.value,
                "ended": run.state in TERMINAL_RUN_STATES,
                "pull_request": {k: pull.get(k) for k in ("number", "url", "merged")} if pull else None,
            }
        if row.get("workflow_id"):
            snap = self._db.collection(WORKFLOWS).document(row["workflow_id"]).get()
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("tenant_id") != tenant_id:
                return {**out, "state": "missing", "ended": True}
            state = _task_state(data.get("state"))
            return {**out, "state": data.get("state"),
                    "ended": state is not None and state in TERMINAL_STATES}
        return out

    def _read_parent(self, attempt: AttemptTuple) -> Task:
        """The parent as stored AND AS SIGNED, for building the child. The fence
        is checked again inside the creating transaction; this read only shapes
        the child.

        `_build_child` copies the parent's `submitted_by`, `repository_url`,
        `forge_credential` and `forge_access` into a child that swarm-api then
        signs with its own key. A worker may update any task document
        (terraform/bootstrap/platform_roles.tf, datastore.entities.update), so
        an agent can rewrite its own task as another member's; read unchecked,
        its child would carry a fresh signature over that member's `git-u-`
        slot, and the forge broker (`credbroker`) would release that member's
        token to it (#1041, security review of PR 1070). So the signature is
        verified here, on the exact dict the child is built from, and anything
        else is `ChildParentUnsigned` -- never a switch, and nothing created.

        Any signed format is enough, not only 3 and up: below format 3 the
        canonical form refuses a document that sets `forge_credential` or
        `forge_access` (`swarm_common.specsign`), so a verified format-1 or 2
        parent carries neither and its child runs on the tenant token, as
        today. The explicit check below says so rather than leaning on that.
        """
        from .verdictpublish import spec_refusal

        snap = self._db.collection(TASKS).document(attempt.task_id).get()
        data = snap.to_dict() if snap.exists else None
        if data is None or data.get("tenant_id") != attempt.tenant_id:
            raise ChildFenced("the parent is not this attempt's task; nothing was created")
        if data.get("parent_task_id"):
            raise ChildDepthExceeded(
                f"a child may not submit children: the depth limit is {MAX_CHILD_DEPTH}"
            )
        refusal = spec_refusal(self._settings, data, attempt.task_id)
        spec_format = data.get("spec_format")
        if refusal is None and (data.get("forge_credential") is not None
                                or data.get("forge_access") is not None) and (
                isinstance(spec_format, bool) or not isinstance(spec_format, int)
                or spec_format < _FORGE_SPEC_FORMAT):
            refusal = "the step's spec is signed at a format that does not cover its forge fields"
        if refusal is not None:
            log.warning("child refused: the parent's spec does not verify tenant=%s task=%s "
                        "reason=%s", attempt.tenant_id, attempt.task_id, refusal)
            raise ChildParentUnsigned(
                f"{refusal}; a child is built only from a parent whose signature verifies, "
                "so nothing was created"
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
            model_class=spec.model_class,
            timeout_seconds=timeout,
        )
        repository_ref = spec.repository_ref or parent.repository_ref
        # Q7 of #1433 (docs/design/registered-mcp-servers.md §4.3): a child
        # names only servers its parent named, and uses the credential rows
        # the parent's submission stored, the parent's own or its workflow's
        # (the attach reads the same owners, from the child's signed
        # `parent_task_id`). It has no `mcp_credentials` of its own.
        mcp_inherited = self._mcp_inheritance(parent) if mcp_server_names(spec.input) else None
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
            mcp_inherited=mcp_inherited,
        )
        # A child's timeout is never above its parent's, whatever the profile.
        child_timeout = min(task.timeout_seconds, parent.timeout_seconds)
        task = replace(
            task,
            timeout_seconds=child_timeout,
            # Contract request 131: the CHILD's own ask, against its final
            # timeout. `create` was already lowered to the parent's, so what
            # `_build_task` recorded from it is not what the child asked. A
            # record only; nothing reads it as a limit (invariant 10).
            timeout_seconds_asked=timeout_asked(spec.timeout_seconds, child_timeout),
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

    def _mcp_inheritance(self, parent: Task) -> Any:
        """The parent's servers as its SIGNED spec names them, and whose
        credential rows a child may use. The agent can rewrite its own task
        document, so a list read from a parent whose signature does not
        verify is no list: the child inherits no server."""
        from .service import McpInheritance
        from .verdictpublish import spec_refusal

        snap = self._db.collection(TASKS).document(parent.id).get()
        doc = (snap.to_dict() or {}) if snap.exists else {}
        signed = (doc.get("tenant_id") == parent.tenant_id
                  and spec_refusal(self._settings, doc, parent.id) is None)
        return McpInheritance(
            servers=tuple(mcp_server_names(doc.get("input"))) if signed else (),
            owners=tuple(o for o in (parent.id, parent.workflow_id or "") if o),
        )

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
        # The run kinds (§3.6), so a resumed worker waits for them too.
        for row in self.runs_of(tenant_id, parent_task_id):
            out.append(self._ledger_listing(row))
        return out

    def runs_of(self, tenant_id: str, parent_task_id: str) -> list[dict[str, Any]]:
        """Every child run and child workflow of a parent, in its tenant only."""
        rows = [
            data for data in (s.to_dict() or {} for s in
                              self._ledger_query(tenant_id, parent_task_id).stream())
            if data.get("tenant_id") == tenant_id and data.get("parent_task_id") == parent_task_id
        ]
        rows.sort(key=lambda r: (r.get("created_at"), r.get("child_id")))
        return rows

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


# --------------------------------------------------------------------------
# The run kinds' ledger, as data (§3.6)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Submitter:
    """The three fields `routes.runs.run_owner_auth` reads of a run, for a
    child run or workflow: the ledger row's id names it in the audit."""

    id: str
    tenant_id: str
    created_by: str
    on_behalf_of: str | None = None


def ledger_id(tenant_id: str, parent_task_id: str, request_id: str) -> str:
    """One ledger row per (tenant, parent, request id): the dedupe is the id."""
    digest = hashlib.sha256(f"{tenant_id}\n{parent_task_id}\n{request_id}".encode()).hexdigest()
    return f"crun_{digest[:32]}"


def ledger_to_api(row: dict[str, Any]) -> dict[str, Any]:
    """What the worker (and so the agent) is answered with: ids, names and state."""
    issue = row.get("issue") or None
    return {
        "child_id": row.get("child_id"),
        "kind": row.get("kind"),
        "request_id": row.get("request_id"),
        "state": row.get("state"),
        "run_id": row.get("run_id"),
        "workflow_id": row.get("workflow_id"),
        "after_merged": row.get("after_merged"),
        "issue": f"{issue['owner']}/{issue['repo']}#{issue['number']}" if issue else None,
        "spec_name": row.get("spec_name"),
        "error": row.get("error"),
    }


def _existing(
    mine: list[dict[str, Any]], ledger: list[dict[str, Any]], request_id: str
) -> SubmitAnswer | None:
    """The child this request id already made, of either kind: a request id is
    the parent's idempotency key whatever it asked for."""
    for existing in mine:
        if (existing.get("metadata") or {}).get(CHILD_REQUEST_ID_METADATA_KEY) == request_id:
            return SubmitAnswer(task=task_from_dict(existing), created=False)
    for row in ledger:
        if row.get("request_id") == request_id:
            return SubmitAnswer(task=None, child=ledger_to_api(row), created=False)
    return None


def _check_cap(mine: list[dict[str, Any]], ledger: list[dict[str, Any]], cap: int) -> None:
    held = len(mine) + len(ledger)
    if held >= cap:
        raise ChildFanOutExceeded(
            f"this task already has {held} children (tasks, issue runs and workflows); "
            f"the cap is {cap} across all its attempts"
        )


def _issue_run_id(metadata: Any) -> str | None:
    """The issue run a task belongs to: `metadata.issue_run`, a run id on a
    planner and `{run_id, ...}` on a run's workflows (`issueruns`, `issueci`)."""
    value = (metadata or {}).get("issue_run") if isinstance(metadata, dict) else None
    if isinstance(value, dict):
        value = value.get("run_id")
    return value if isinstance(value, str) and value else None


def _issue_of_parent(parent: Task, issue: int | str) -> Any:
    """The IssueRef of `issue` in the PARENT'S repository, or a refusal.

    A number is an issue there. A string must name that same repository
    (GitHub's names are case-insensitive); any other is
    `ChildRunOtherRepository`, and a parent with no GitHub repository has no
    issue to run (`ChildRunNoRepository`).
    """
    from .validation import IssueRef, merge_repository, parse_issue_ref

    named = merge_repository(parent.repository_url)
    if named is None:
        raise ChildRunNoRepository(
            "this task has no GitHub repository, so a child issue run has no issue to name"
        )
    owner, repo = named
    if isinstance(issue, bool):
        raise ValidationFailed("issue is a number, or owner/repo#N of this task's repository")
    if isinstance(issue, int):
        if issue < 1:
            raise ValidationFailed("issue is a positive number")
        return IssueRef(owner=owner, repo=repo, number=issue)
    try:
        ref = parse_issue_ref(issue)
    except ValueError:
        raise ValidationFailed(
            "issue must be a number, owner/repo#N or the issue's URL, in this task's repository"
        ) from None
    if (ref.owner.casefold(), ref.repo.casefold()) != (owner.casefold(), repo.casefold()):
        raise ChildRunOtherRepository(
            "a child issue run names an issue of this task's own repository only; "
            "nothing was created"
        )
    return IssueRef(owner=owner, repo=repo, number=ref.number)


def _task_state(value: Any) -> TaskState | None:
    try:
        return TaskState(value)
    except ValueError:
        return None


def release_held_runs(ctx: Any, tenant_id: str, *, limit: int = RELEASE_PAGE) -> dict[str, int]:
    """The schedule tick's half of `after_merged` (§3.6), for ONE tenant:
    `ChildService.release_held`. Returns its counts."""
    return ChildService(
        settings=ctx.settings,
        db=ctx.db,
        store=ctx.store,
        submissions=ctx.submissions,
        verifier=None,
        now=ctx.now,
        ctx=ctx,
    ).release_held(tenant_id, limit=limit)


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
