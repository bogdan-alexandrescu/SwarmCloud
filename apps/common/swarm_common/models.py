"""Firestore document shapes for the control plane.

Firestore has no joins, so every document carries the denormalised fields the
scheduler needs to make an admission decision without a second read. The
scheduler's hot path reads exactly one query (READY tasks, ordered) plus the
slot-pool documents; nothing else.

Collection layout (`swarm` database, not `(default)`, so the project's default
database stays free for other teams):

    tenants/{tenant_id}
    tasks/{task_id}
    tasks/{task_id}/events/{event_id}
    attempts/{attempt_id}
    leases/{lease_id}
    pools/{pool_name}
    quota/{provider}:{tenant_id}
    workflows/{workflow_id}
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime, timezone
from typing import Any

from .states import TaskState, ParkReason, BlockedReason, EventType


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# --------------------------------------------------------------------------
# Slot pools
# --------------------------------------------------------------------------

@dataclass
class SlotPool:
    """A named concurrency budget.

    `active` is the authoritative count of leases currently holding this pool.
    It is mutated ONLY inside the admission transaction and the release
    transaction, never by a background job, so it cannot drift under contention.

    `effective_limit` is the real ceiling and is always the minimum of the
    configured hard limit, the adaptive target, and any quota-derived cap.
    Adaptive logic may lower it; nothing may raise it above `hard_limit`.
    """

    name: str
    hard_limit: int
    adaptive_target: int | None = None
    quota_derived_limit: int | None = None
    active: int = 0
    enabled: bool = True
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def effective_limit(self) -> int:
        candidates = [self.hard_limit]
        if self.adaptive_target is not None:
            candidates.append(self.adaptive_target)
        if self.quota_derived_limit is not None:
            candidates.append(self.quota_derived_limit)
        return max(0, min(candidates))

    @property
    def available(self) -> int:
        return max(0, self.effective_limit - self.active)

    def has_capacity(self, units: int = 1) -> bool:
        return self.enabled and self.active + units <= self.effective_limit


def pool_names_for(
    *,
    tenant_id: str,
    provider: str | None,
    resource_class: str,
    runner_profile: str,
    backend: str,
) -> list[str]:
    """Every pool a task must simultaneously acquire.

    Admission is all-or-nothing across this list. Reserving some and failing on
    the rest would leak capacity that nothing would ever release.
    """
    pools = [
        "global",
        f"tenant:{tenant_id}",
        f"resource:{resource_class}",
        f"runner:{runner_profile}",
        f"backend:{backend}",
    ]
    if provider:
        pools.append(f"provider:{provider}")
        # Provider quota is tracked per tenant because tenants bring their own
        # keys -- one tenant's 429 must not throttle another's.
        pools.append(f"provider:{provider}:tenant:{tenant_id}")
    return pools


# --------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------

@dataclass
class Lease:
    """Authoritative record that a task holds capacity.

    The lease -- not the pod, not the Cloud Run execution -- is what concurrency
    accounting counts. Infrastructure is downstream of the lease, never upstream.
    """

    lease_id: str
    task_id: str
    attempt_id: str
    tenant_id: str
    generation: int
    pools: list[str]
    units: int
    state: TaskState
    created_at: datetime
    dispatch_deadline: datetime
    expires_at: datetime
    heartbeat_at: datetime | None = None
    released_at: datetime | None = None
    release_reason: str | None = None

    @property
    def is_released(self) -> bool:
        return self.released_at is not None

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) > self.expires_at

    def dispatch_overdue(self, now: datetime | None = None) -> bool:
        """Past the dispatch deadline with no worker to show for it.

        NOT guarded on `state is LEASED`. `mark_dispatched`
        (scheduler/store.py) writes DISPATCHED to the lease the moment the
        backend ACCEPTS the create call -- long before a container runs -- so
        that guard excluded essentially every lease this method names. Measured
        on task_b5dc2568713a40158851 in saga-agents-staging on 2026-09-22:
        `dispatch_overdue` was still False 301 seconds after admission, and the
        `overdue_only=1` operator query returned an empty list at exactly the
        moment it was asked.

        A `heartbeat_at is None` conjunct is deliberately NOT added. The
        reconciler needs that distinction because it decides whether to fence a
        generation; this method answers the narrower question -- has the
        deadline passed -- and a lease that heartbeated and then went quiet past
        the deadline is still, factually, overdue.

        Changed 2026-09-22 by the owner's decision on contract change request 9.
        """
        return (now or utcnow()) > self.dispatch_deadline


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

class EndCause(str, Enum):
    """Why a task reached a FAILED or CANCELLED state, written by the writer that ended it.

    Contract request 23, ACCEPTED by the owner on 2026-09-25 (#185, the
    decisions comment, item 9). Until this existed the outcome ledger
    (`swarm_api.outcomes`) read `last_error` -- free text from four writers the
    API image does not carry -- and sorted it into classes by prefix. One case
    was not recoverable from the text at all: "an upstream workflow step did
    not succeed" is written after a FAILED parent and after a CANCELLED one,
    so a cancel somebody pressed read the same as a failure.

    Each value is written by exactly one kind of writer, beside `completed_at`:

      * the worker (`agent_worker.control`): TIMEOUT, OUTPUTS_MISSING,
        INPUTS_UNAVAILABLE, CANNOT_START, RUNNER_ERROR, CANCEL_REQUESTED;
      * the reconciler (`repair_task_state`): LOST_WORKER, CANNOT_START,
        CANCEL_REQUESTED;
      * the scheduler: DISPATCH_FAILED, FAILED_PARENT, CANCELLED_PARENT,
        WORKFLOW_SWEEP, CANCEL_REQUESTED;
      * the API's cancel: CANCEL_REQUESTED.

    SUCCEEDED carries None: nothing about a success needs a cause. So does a
    task that ended before this field existed, and one a writer ended for a
    reason with no value here (the worker's "runner stopped on SIGTERM"
    without a requested cancel); the ledger falls back to its text classifier
    for exactly those, never for a task that carries a cause.

    FAILED_PARENT and CANCELLED_PARENT are decided by each parent's OWN END,
    not its state: a CANCELLED parent that is itself a failure's cascade (or
    was taken by the workflow sweep) passes a failure down, so a failure's
    cascade stays FAILED_PARENT however many steps down it reaches (the review
    of PR #217). A cascade below a parent cancelled before this field existed,
    with no cancel flag, is written with None: the scheduler cannot name that
    parent's end, and the ledger splits the step by its chain of parents.

    INPUTS_UNAVAILABLE was not in the request as filed. It is the owner's
    decision item 4 on the same comment: the worker refusing to stage a
    declared `input_from` artifact (`agent_worker.errors.InputUnavailable`) is
    its own class, not a runner error -- 10 of dev's 13 "runner errors" on
    2026-09-25 were exactly that. CANCELLED_PARENT is the request's own, and
    what decision item 2 (split "after a cancel" from "after a failure") needs.
    """

    TIMEOUT = "timeout"
    CANNOT_START = "cannot_start"
    LOST_WORKER = "lost_worker"
    OUTPUTS_MISSING = "outputs_missing"
    INPUTS_UNAVAILABLE = "inputs_unavailable"
    DISPATCH_FAILED = "dispatch_failed"
    RUNNER_ERROR = "runner_error"
    CANCEL_REQUESTED = "cancel_requested"
    FAILED_PARENT = "failed_parent"
    CANCELLED_PARENT = "cancelled_parent"
    WORKFLOW_SWEEP = "workflow_sweep"


@dataclass
class Task:
    id: str
    tenant_id: str
    created_at: datetime
    updated_at: datetime
    state: TaskState
    runner_profile: str
    resource_class: str
    input: dict[str, Any]
    submitted_by: str                       # verified email from the ID token
    provider: str | None = None
    model: str | None = None
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    repository_url: str | None = None
    repository_ref: str | None = None
    timeout_seconds: int = 3600
    max_attempts: int = 3
    attempt_count: int = 0
    next_eligible_at: datetime | None = None
    park_reason: ParkReason | None = None
    blocked_by: list[dict[str, Any]] = field(default_factory=list)
    current_lease_id: str | None = None
    current_generation: int = 0
    # Workflow membership. Null for a standalone task.
    workflow_id: str | None = None
    step_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    result_summary: dict[str, Any] | None = None
    latest_checkpoint: str | None = None
    #: Why the task ended, typed. See `EndCause` (contract request 23). None on
    #: every task that has not ended, on a success, and on anything written
    #: before 2026-09-25 -- an old document decodes exactly as it did.
    end_cause: EndCause | None = None

    def retries_exhausted(self) -> bool:
        """This task has used its last attempt. See `retries_exhausted`."""
        return retries_exhausted(self.attempt_count, self.max_attempts)

    def to_firestore(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["park_reason"] = self.park_reason.value if self.park_reason else None
        d["end_cause"] = (
            self.end_cause.value if isinstance(self.end_cause, EndCause) else self.end_cause
        )
        return d



def retries_exhausted(attempt_count: int, max_attempts: int) -> bool:
    """Whether a task has used its last attempt.

    A FREE FUNCTION as well as a method because the two enforcement points do
    not both hold a `Task`: the reconciler decides inside a Firestore
    transaction, from the raw document, where constructing one would mean
    decoding a task to read two integers.

    THIS EXISTS BECAUSE THE RULE WAS RESTATED AND THEN OMITTED. It lived only
    in `reconciler/store.py`, on the path that repairs a task to READY. The
    scheduler's `return_to_ready_after_failed_dispatch` -- the OTHER path that
    returns a task to READY -- wrote the state unconditionally, so a task whose
    dispatch kept failing retried for ever. Observed 2026-09-23:
    `task_d18d8d8b044d469cb43c` reached 83 attempts against a cap of 3,
    re-dispatching every 30 seconds for hours on `gke_create_job_failed`.

    The scheduler and the reconciler are separate images and cannot import each
    other, so a rule they both need has exactly one home that is not a
    restatement: here.
    """
    return attempt_count >= max_attempts

@dataclass
class Attempt:
    """One execution of a task. A retry is a new attempt with a new generation."""

    attempt_id: str
    task_id: str
    tenant_id: str
    generation: int
    lease_id: str
    backend: str
    created_at: datetime
    execution_name: str | None = None       # Cloud Run execution or k8s Job name
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    error: str | None = None
    peak_rss_bytes: int | None = None       # feeds the sizing tuning report
    peak_disk_bytes: int | None = None
    oom_near_miss: bool = False
    checkpoints: list[str] = field(default_factory=list)

    # What the attempt SPENT. Added 2026-09-19 as change request #2 in
    # docs/contract-change-requests.md, approved by the platform owner.
    #
    # Until this existed an attempt recorded precisely how much MEMORY and DISK
    # it used and nothing at all about tokens -- on a platform whose entire cost
    # is tokens. The numbers were already being captured
    # (agent_worker.lifecycle._usage_summary) and landed in an untyped runner
    # summary, where no index can reach them: "spend per tenant last week" meant
    # scanning and parsing rather than querying.
    #
    # All optional, defaulting to None, so every existing document stays valid
    # and no migration runs. None means NOT REPORTED, which is genuinely
    # different from zero: a mock task costs nothing on purpose, and a run whose
    # result could not be parsed costs an unknown amount. A UI that renders
    # those two the same way is lying about one of them.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cost_usd: float | None = None


@dataclass
class TaskEvent:
    event_id: str
    task_id: str
    tenant_id: str
    type: EventType
    at: datetime
    attempt_id: str | None = None
    lease_id: str | None = None
    generation: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Tenants, quota, workflows
# --------------------------------------------------------------------------

@dataclass
class Tenant:
    """A Google group, or a single user as a personal fallback tenant."""

    tenant_id: str
    kind: str                               # "group" | "user"
    principal: str                          # eng@saga.xyz | alice@saga.xyz
    created_at: datetime
    display_name: str | None = None
    max_active: int = 20
    capacity_units: int = 40
    monthly_budget_usd: float | None = None
    enabled: bool = True
    #: Providers this tenant has registered a key for. A runner whose provider
    #: is absent parks as CREDENTIAL_MISSING rather than failing at runtime.
    credentials: list[str] = field(default_factory=list)
    service_account: str | None = None
    gcs_prefix: str | None = None
    namespace: str | None = None

    def secret_name(self, provider: str) -> str:
        return f"swarm-tenant-{self.tenant_id}-{provider}"


class ProviderState(str, Enum):
    AVAILABLE = "AVAILABLE"
    THROTTLED = "THROTTLED"
    EXHAUSTED = "EXHAUSTED"
    COOLDOWN = "COOLDOWN"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"


@dataclass
class QuotaState:
    """Per (provider, tenant) health. Keyed that way because keys are per tenant."""

    provider: str
    tenant_id: str
    state: ProviderState
    updated_at: datetime
    configured_hard_max: int = 50
    adaptive_target: int | None = None
    quota_derived_limit: int | None = None
    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    reset_at: datetime | None = None
    cooldown_until: datetime | None = None
    last_429_at: datetime | None = None
    retry_after_seconds: int | None = None
    success_count: int = 0
    rate_limit_count: int = 0

    @property
    def effective_limit(self) -> int:
        candidates = [self.configured_hard_max]
        if self.adaptive_target is not None:
            candidates.append(self.adaptive_target)
        if self.quota_derived_limit is not None:
            candidates.append(self.quota_derived_limit)
        limit = min(candidates)
        if self.state in (ProviderState.EXHAUSTED, ProviderState.DISABLED):
            return 0
        if self.state is ProviderState.COOLDOWN:
            return 0
        return max(0, limit)


@dataclass
class WorkflowStep:
    step_id: str
    runner_profile: str
    input: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    resource_class: str | None = None
    #: Map of upstream step_id -> artifact filename to stage into this step's
    #: workspace. Artifacts pass by GCS reference, never inline through Firestore.
    input_from: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int | None = None
    task_id: str | None = None


@dataclass
class Workflow:
    workflow_id: str
    tenant_id: str
    created_at: datetime
    updated_at: datetime
    state: TaskState
    submitted_by: str
    steps: list[WorkflowStep] = field(default_factory=list)
    on_step_failure: str = "fail_workflow"   # or "continue"
    priority: int = 0
    cancel_requested: bool = False
