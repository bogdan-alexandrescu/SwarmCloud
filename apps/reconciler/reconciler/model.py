"""Flattened views of control-plane and backend state.

The reconciler compares two pictures of the world, and the comparison has to be
pure: given these documents and these executions, what is wrong? Keeping the
views as frozen dataclasses -- rather than passing Firestore snapshots and
protobufs into the detection logic -- is what makes every rule in `detect.py`
testable without a project, and what stops a subtle read of a half-populated
protobuf field from becoming a decision to kill someone's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from swarm_common.models import EndCause, WorkflowStep, retries_exhausted, utcnow
from swarm_common.states import CONCURRENCY_STATES, TERMINAL_STATES, TaskState


def as_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    to_dt = getattr(value, "ToDatetime", None)
    if callable(to_dt):
        return to_dt()
    return None


def _state(value: Any, default: TaskState = TaskState.QUEUED) -> TaskState:
    try:
        return TaskState(value)
    except (ValueError, TypeError):
        return default


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: The marker the child cascade sets on a child cancelled because of its
#: parent (docs/design/child-tasks.md §3.4), `swarm_api.validation.
#: CHILD_CASCADE_METADATA_KEY` restated: this image carries neither swarm-api
#: nor the scheduler. tests/unit/worker/test_end_cause_reconciler.py
#: holds it equal to both.
CHILD_CASCADE_METADATA_KEY = "child_cascade"


def cancel_end_cause(metadata: Any) -> EndCause:
    """Why a cancelled task ended: CHILD_CASCADE when its parent's cancel, end
    or await deadline flagged it (contract request 41), else CANCEL_REQUESTED.

    The rule `agent_worker.control.cancel_end_cause` and
    `scheduler.children.cancel_end_cause` apply, so a flagged child whose
    worker died ends `child_cascade` here exactly as it would had its worker
    finished it.
    """
    if isinstance(metadata, dict) and metadata.get(CHILD_CASCADE_METADATA_KEY):
        return EndCause.CHILD_CASCADE
    return EndCause.CANCEL_REQUESTED


#: Where a task records the attempts the reconciler took back because they
#: ended before their runner started (#67). `Task.metadata` is free-form, so
#: the frozen `Task` needs no field for it.
STARTUP_REFUNDS_KEY = "startup_refunds"

#: Firestore stores an integer as a signed 64-bit value; anything past this
#: cannot round-trip and is not a real refund count. Read alongside
#: `_startup_refunds_int`'s type check -- this bound is what turns away
#: `10 ** 30` once the type check alone would let it through.
_FIRESTORE_INT64_MAX = 2**63 - 1


def _startup_refunds_int(value: Any) -> int | None:
    """`metadata.startup_refunds`, read totally: a real refund count or None.

    This field used to go through the general-purpose `_int_or_none`, which
    only catches `TypeError`/`ValueError` -- so `int(float("inf"))` raised
    `OverflowError` straight out of `TaskView.from_doc`, which `snapshot()`
    calls for every task in an unguarded loop (`store.py`). One task with
    `metadata.startup_refunds = Infinity` crashed the reconciler pass for
    every tenant (security review, PR #290).

    Reserved at the API (`swarm_api.validation.RESERVED_METADATA_KEYS`) so a
    caller cannot submit this key going forward, but a document written
    before that reservation -- or reached some other way -- can still carry
    anything JSON allows: `Infinity`, `NaN`, a numeric string, `True` (a
    `bool` IS an `int` in Python, and is deliberately excluded here), a
    `float` even when it holds a whole number, or an integer too large for
    Firestore's int64 column. None of that should raise, and none of it is a
    real refund count, so all of it reads as "not a valid count" (the caller
    then treats that as 0 -- see `startup_refunds_used`).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        # `bool` is excluded first because `isinstance(True, int)` is `True`.
        # `float` is excluded even when finite and whole (`3.0`): the field
        # is a count, and only a document written by this reconciler's own
        # write path (an `int` literal, see `store.py`) should ever count.
        return None
    if not (0 <= value <= _FIRESTORE_INT64_MAX):
        return None
    return value


def startup_refunds_used(metadata: Any) -> int:
    """How many startup refunds a task's `metadata` records. Absent or unreadable is 0."""
    if not isinstance(metadata, dict):
        return 0
    used = _startup_refunds_int(metadata.get(STARTUP_REFUNDS_KEY))
    return used if used is not None else 0


@dataclass(frozen=True)
class StartupEnd:
    """What an attempt that ended before its runner started does to its task's count.

    `attempt_count` and `refunds_used` are the values the task should hold
    afterwards; `tail` ends its `last_error`.
    """

    attempt_count: int
    refunds_used: int
    refunded: bool
    exhausted: bool
    tail: str


def count_startup_end(
    attempt_count: int, max_attempts: int, refunds_used: int, limit: int
) -> StartupEnd:
    """Refund the attempt while fewer than `limit` refunds are used; else count it (#67).

    THE REFUND COMES FIRST, then `retries_exhausted`: the attempt that took a
    task to its `max_attempts` never ran, so it is requeued rather than
    failed. THE BOUND is what still ends a task killed at every start: once
    `limit` refunds are used, an early end counts exactly as it did before.

    Pure, so the snapshot's prediction (`detect_ended_at_startup`) and the
    transaction's decision (`ControlStore.repair_task_state`) are one rule.
    A refund that would still leave the task exhausted (a count already past
    its cap) is not spent: the task fails either way.
    """
    if refunds_used < limit:
        refunded_count = max(0, attempt_count - 1)
        if not retries_exhausted(refunded_count, max_attempts):
            used = refunds_used + 1
            return StartupEnd(
                attempt_count=refunded_count,
                refunds_used=used,
                refunded=True,
                exhausted=False,
                tail=f"not counted ({used}/{limit}); retrying",
            )
    spent = retries_exhausted(attempt_count, max_attempts)
    tail = "no attempts left" if spent else "retrying"
    if limit > 0 and refunds_used >= limit:
        tail += f" (counted: {refunds_used}/{limit} early ends already refunded)"
    return StartupEnd(
        attempt_count=attempt_count,
        refunds_used=refunds_used,
        refunded=False,
        exhausted=spent,
        tail=tail,
    )


@dataclass(frozen=True)
class TaskView:
    task_id: str
    tenant_id: str
    state: TaskState
    generation: int
    lease_id: str | None
    runner_profile: str
    resource_class: str
    updated_at: datetime | None
    attempt_count: int = 0
    max_attempts: int = 3
    cancel_requested: bool = False
    started_at: datetime | None = None
    #: The resume pointer `agent_worker.lifecycle._restore_checkpoint` reads
    #: first. Carried here because checkpoint retention has to know which
    #: checkpoint a future attempt of this task would come back to; the
    #: reconciliation pass itself never looks at it.
    latest_checkpoint: str | None = None
    #: When the task reached its terminal state. The left-running rule measures
    #: its grace from here: a Job still active a moment after the worker wrote
    #: SUCCEEDED is a worker finishing its own cleanup, not one left open.
    completed_at: datetime | None = None
    #: `metadata.startup_refunds`: attempts already taken back because they
    #: ended before their runner started (#67, `count_startup_end`).
    startup_refunds: int = 0

    @property
    def holds_capacity(self) -> bool:
        return self.state in CONCURRENCY_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @classmethod
    def from_doc(cls, doc: dict[str, Any], task_id: str | None = None) -> "TaskView":
        return cls(
            task_id=task_id or doc.get("id", ""),
            tenant_id=doc.get("tenant_id", ""),
            state=_state(doc.get("state")),
            generation=int(doc.get("current_generation", 0)),
            lease_id=doc.get("current_lease_id"),
            runner_profile=doc.get("runner_profile", ""),
            resource_class=doc.get("resource_class", "standard"),
            updated_at=as_datetime(doc.get("updated_at")),
            attempt_count=int(doc.get("attempt_count", 0)),
            max_attempts=int(doc.get("max_attempts", 3)),
            cancel_requested=bool(doc.get("cancel_requested")),
            started_at=as_datetime(doc.get("started_at")),
            latest_checkpoint=doc.get("latest_checkpoint"),
            completed_at=as_datetime(doc.get("completed_at")),
            startup_refunds=startup_refunds_used(doc.get("metadata")),
        )


@dataclass(frozen=True)
class LeaseView:
    lease_id: str
    task_id: str
    attempt_id: str
    tenant_id: str
    generation: int
    pools: tuple[str, ...]
    units: int
    state: TaskState
    created_at: datetime | None
    dispatch_deadline: datetime | None
    expires_at: datetime | None
    heartbeat_at: datetime | None = None
    released_at: datetime | None = None

    @property
    def is_released(self) -> bool:
        return self.released_at is not None

    def last_signal_at(self) -> datetime | None:
        return self.heartbeat_at or self.created_at

    def silent_seconds(self, now: datetime | None = None) -> float:
        """Seconds since the worker last proved it was alive.

        A lease that has never heartbeated falls back to its creation time, so a
        worker that died during startup is still reaped."""
        last = self.last_signal_at()
        if last is None:
            return float("inf")
        return ((now or utcnow()) - last).total_seconds()

    @classmethod
    def from_doc(cls, doc: dict[str, Any], lease_id: str | None = None) -> "LeaseView":
        return cls(
            lease_id=lease_id or doc.get("lease_id", ""),
            task_id=doc.get("task_id", ""),
            attempt_id=doc.get("attempt_id", ""),
            tenant_id=doc.get("tenant_id", ""),
            generation=int(doc.get("generation", 0)),
            pools=tuple(doc.get("pools", [])),
            units=int(doc.get("units", 1)),
            state=_state(doc.get("state"), TaskState.LEASED),
            created_at=as_datetime(doc.get("created_at")),
            dispatch_deadline=as_datetime(doc.get("dispatch_deadline")),
            expires_at=as_datetime(doc.get("expires_at")),
            heartbeat_at=as_datetime(doc.get("heartbeat_at")),
            released_at=as_datetime(doc.get("released_at")),
        )


@dataclass(frozen=True)
class AttemptView:
    attempt_id: str
    task_id: str
    tenant_id: str
    generation: int
    backend: str
    execution_name: str | None
    created_at: datetime | None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: What the worker recorded on its OWN attempt document when it ended
    #: (`ControlPlane.record_attempt_end`), when it reached Firestore at all.
    #: Read only to name the cause of a worker that could not start.
    exit_code: int | None = None
    error: str | None = None

    @classmethod
    def from_doc(cls, doc: dict[str, Any], attempt_id: str | None = None) -> "AttemptView":
        error = doc.get("error")
        return cls(
            attempt_id=attempt_id or doc.get("attempt_id", ""),
            task_id=doc.get("task_id", ""),
            tenant_id=doc.get("tenant_id", ""),
            generation=int(doc.get("generation", 0)),
            backend=doc.get("backend", ""),
            execution_name=doc.get("execution_name"),
            created_at=as_datetime(doc.get("created_at")),
            started_at=as_datetime(doc.get("started_at")),
            completed_at=as_datetime(doc.get("completed_at")),
            exit_code=_int_or_none(doc.get("exit_code")),
            error=error if isinstance(error, str) and error.strip() else None,
        )


class ExecutionPhase(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ExecutionView:
    """One real unit of execution on a backend: a Cloud Run execution or a k8s Job."""

    name: str
    backend: str
    phase: ExecutionPhase
    created_at: datetime | None
    #: Identifiers the dispatcher stamped on the resource as labels/annotations.
    task_id: str | None = None
    attempt_id: str | None = None
    tenant_id: str | None = None
    generation: int | None = None
    parent: str | None = None          # the Job resource this execution belongs to
    namespace: str | None = None
    #: This execution CLAIMED a task in another tenant and the claim was
    #: refused (see scope_executions_to_tenant).
    #:
    #: Recorded rather than inferred from `task_id is None`, because those are
    #: two different facts and collapsing them costs one of two properties
    #: depending on which way you collapse them. Stripping the claim and
    #: leaving no trace makes a forged execution look like a non-worker
    #: execution -- so a reconciler that (correctly) declines to terminate
    #: compute it cannot attribute to a task would leave real compute running
    #: in the attacker's tenant with a refused claim on the victim's work.
    #: Caught by test_a_forged_execution_does_not_fence_the_victims_live_attempt.
    claim_refused: bool = False
    #: When the backend recorded this execution as over: a Cloud Run
    #: execution's `completion_time`, a Job's completion time or the time its
    #: Failed condition became true. None while it runs, and whenever the
    #: backend recorded no such time. The ended-at-startup rule measures its
    #: grace from here and does nothing without it (`detect.detect_ended_at_startup`).
    completed_at: datetime | None = None
    #: The backend's own record PROVES this execution's compute is gone: on
    #: Cloud Run, `backends.execution_is_finished` (completion time set,
    #: nothing running, not reconciling). Stronger than "not active": the
    #: phase's UNKNOWN bucket is not active and proves nothing. False wherever
    #: a backend does not establish it (GKE leaves it False), which is the
    #: direction that holds a lease. Read by `detect.detect_stale_leases`: a
    #: lease past its TTL whose execution has ENDED is a lost worker, and needs
    #: no by-name probe to say so (2026-10-03, task_8fce64316ad14fc981fb).
    ended: bool = False

    @property
    def is_active(self) -> bool:
        return self.phase is ExecutionPhase.RUNNING


@dataclass(frozen=True)
class Termination:
    """How a FINISHED execution's worker container ended, in the backend's own record.

    Read by name, one execution at a time, and only for an execution the
    cannot-start rule could act on (`detect.cannot_start_candidates`), so a
    pass that has none costs no call.
    """

    #: The worker container's exit code: a Cloud Run task's
    #: `last_attempt_result.exit_code`, or a pod's `state.terminated.exitCode`.
    #: None when the backend recorded none.
    exit_code: int | None
    #: What the WORKER wrote to explain itself: the pod's termination message
    #: (`agent_worker.startup.write_termination_message`). Text a tenant's
    #: pod produced, so it is bounded before it reaches a task
    #: (`detect.worker_cause`). None on Cloud Run, which keeps no such thing.
    message: str | None = None
    #: The backend's own words about the end, for the finding and the log
    #: line only: Cloud Run's task status message, a pod's `reason`. Never
    #: presented as the worker's cause.
    detail: str = ""


@dataclass(frozen=True)
class JobResourceView:
    """A per-tenant-per-profile Cloud Run Job resource, or a k8s namespace."""

    name: str
    tenant_id: str | None
    runner_profile: str | None
    created_at: datetime | None
    last_execution_at: datetime | None
    managed: bool = False
    active_executions: int = 0


@dataclass
class ControlSnapshot:
    """Everything the reconciler read from Firestore in one pass."""

    tasks: dict[str, TaskView] = field(default_factory=dict)
    leases: dict[str, LeaseView] = field(default_factory=dict)
    attempts: dict[str, AttemptView] = field(default_factory=dict)
    taken_at: datetime = field(default_factory=utcnow)
    #: Tasks OUTSIDE the four concurrency states that an active execution
    #: names, read by id after the backends were listed. `tasks` holds only
    #: the concurrency states (see `ControlStore.snapshot`), so without this a
    #: Job still running after its task SUCCEEDED looked like a Job naming a
    #: task nobody knows -- and so did the old Job of a task re-queued to
    #: READY. Empty unless something active points outside `tasks`.
    settled: dict[str, TaskView] = field(default_factory=dict)
    #: Task ids an active execution names that could NOT be read this pass.
    #: Nothing is concluded about them: see `detect.detect_orphan_executions`.
    unreadable_tasks: set[str] = field(default_factory=set)
    #: attempt id -> the progress evidence read for it (`progress.py`). Only
    #: attempts the stuck rule could act on are read; absence means "not
    #: judged", never "no progress".
    progress: dict[str, Any] = field(default_factory=dict)
    #: attempt id -> how its FINISHED execution ended (`Termination`, or any
    #: object with `exit_code`, `message` and `detail`). Only attempts the
    #: cannot-start rule could act on are read, and a read that failed leaves
    #: no entry: absence means "not known", never "did not exit 78".
    terminations: dict[str, Any] = field(default_factory=dict)
    #: Tasks an UNRELEASED LEASE names that `tasks` lacks, read by id
    #: (`Reconciler._read_lease_tasks`, #332). Read by `detect_orphan_leases`
    #: ALONE. Kept apart from `settled` on purpose: `settled` is the eviction
    #: rules' input, and `orphan_rule_defers` reads it, so writing these there
    #: would bring the eviction stand-asides back with
    #: `RECONCILER_ENABLE_GKE_EVICTION=false` -- the switch that exists to
    #: restore the reconciler as it was before them.
    lease_tasks: dict[str, TaskView] = field(default_factory=dict)
    #: Task ids an unreleased lease names whose by-id read FAILED this pass.
    #: The orphan-lease rule concludes nothing about their leases.
    lease_unreadable: set[str] = field(default_factory=set)

    def task_named(self, task_id: str | None) -> TaskView | None:
        """The task an execution names, whichever read found it."""
        if not task_id:
            return None
        return self.tasks.get(task_id) or self.settled.get(task_id)

    def lease_for_task(self, task_id: str) -> LeaseView | None:
        task = self.tasks.get(task_id)
        if task and task.lease_id:
            return self.leases.get(task.lease_id)
        for lease in self.leases.values():
            if lease.task_id == task_id and not lease.is_released:
                return lease
        return None

    def attempt_for_lease(self, lease: LeaseView) -> AttemptView | None:
        return self.attempts.get(lease.attempt_id)


# ---------------------------------------------------------------------------
# Workflows (#616): what the workflow stall check reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepTaskView:
    """A workflow step's task, as the stall check needs it.

    `task` is the same `TaskView` every other rule reads, so a step task is
    decoded by the one task decoder this service has. The three fields beside
    it are the ones only the stall check asks about: why the step is parked,
    which tasks it waits on, and its `result_summary` (for the derivation's
    SKIPPED rule, `swarm_rollup.skipped_task_ids`, which reads it by name).
    """

    task: TaskView
    park_reason: str | None = None
    #: The TASK ids this step waits on: what the scheduler's dependency sweep
    #: (`scheduler.loop._promote_dependencies`) gates a promotion on.
    depends_on: tuple[str, ...] = ()
    result_summary: dict[str, Any] | None = None

    @property
    def id(self) -> str:
        return self.task.task_id

    @property
    def state(self) -> TaskState:
        return self.task.state

    @classmethod
    def from_doc(cls, doc: dict[str, Any], task_id: str) -> "StepTaskView":
        summary = doc.get("result_summary")
        return cls(
            task=TaskView.from_doc(doc, task_id),
            park_reason=doc.get("park_reason") or None,
            depends_on=tuple(str(t) for t in (doc.get("depends_on") or [])),
            result_summary=dict(summary) if isinstance(summary, dict) else None,
        )


@dataclass(frozen=True)
class WorkflowView:
    """One workflow document, decoded strictly.

    STRICT, unlike `TaskView`'s state, which falls back to QUEUED: the stall
    check compares this state against the derived one and may WRITE the
    derived one over it, so a state it cannot decode must make the workflow a
    finding (`unreadable`), never a QUEUED it would then "repair".

    `steps` are the frozen contract's `WorkflowStep`, carrying only what the
    derivation and the dependency rule read (`step_id`, `task_id`,
    `depends_on`), so `swarm_rollup.read_steps` takes them unchanged.
    """

    workflow_id: str
    tenant_id: str
    state: TaskState
    steps: tuple[WorkflowStep, ...]
    updated_at: datetime | None = None
    on_step_failure: str = "fail_workflow"
    cancel_requested: bool = False

    @classmethod
    def from_doc(cls, doc: dict[str, Any], workflow_id: str) -> "WorkflowView":
        steps = tuple(
            WorkflowStep(
                step_id=str(step["step_id"]),
                runner_profile=str(step.get("runner_profile") or ""),
                input={},
                depends_on=[str(s) for s in (step.get("depends_on") or [])],
                task_id=step.get("task_id") or None,
            )
            for step in (doc.get("steps") or [])
        )
        return cls(
            workflow_id=str(doc.get("workflow_id") or workflow_id),
            tenant_id=str(doc.get("tenant_id") or ""),
            state=TaskState(doc["state"]),
            steps=steps,
            updated_at=as_datetime(doc.get("updated_at")),
            on_step_failure=str(doc.get("on_step_failure") or "fail_workflow"),
            cancel_requested=bool(doc.get("cancel_requested")),
        )


@dataclass
class WorkflowRead:
    """What one pass read for the stall check. Truncation is said, not hidden."""

    workflows: list[WorkflowView] = field(default_factory=list)
    #: task id -> the step task, for every step task that was read.
    tasks: dict[str, StepTaskView] = field(default_factory=dict)
    #: Step task ids read and found missing: a data fault, which the
    #: derivation reports as `step_tasks_missing`.
    absent: set[str] = field(default_factory=set)
    #: Step task ids whose document could not be decoded.
    unreadable_tasks: set[str] = field(default_factory=set)
    #: Workflow documents that could not be decoded:
    #: {"workflow_id", "tenant_id", "error"}.
    malformed: list[dict[str, Any]] = field(default_factory=list)
    #: The read stopped at its limit, so these are not every live workflow.
    truncated: bool = False
