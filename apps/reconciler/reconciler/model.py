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

from swarm_common.models import utcnow
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
