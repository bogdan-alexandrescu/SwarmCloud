"""Task lifecycle states and the invariants that govern transitions.

The single most important rule in this platform lives here:

    Only tasks at LEASED or later may create infrastructure demand.

Everything else -- QUEUED, PARKED, READY -- is durable state in Firestore that
costs nothing. A backlog of ten thousand tasks produces zero pods, zero Cloud Run
executions and zero nodes.
"""

from __future__ import annotations

from enum import Enum


class TaskState(str, Enum):
    SUBMITTED = "SUBMITTED"
    QUEUED = "QUEUED"
    PARKED = "PARKED"
    READY = "READY"
    LEASED = "LEASED"
    DISPATCHED = "DISPATCHED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEAD_LETTERED = "DEAD_LETTERED"


#: States that hold capacity. Accounting starts at LEASED rather than RUNNING so
#: that slow container starts cannot oversubscribe a pool: a task that has been
#: admitted but is still pulling its image already counts against the limit.
CONCURRENCY_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.LEASED,
        TaskState.DISPATCHED,
        TaskState.STARTING,
        TaskState.RUNNING,
    }
)

#: Terminal states. Reaching any of these MUST release every pool reservation
#: held by the task's lease, exactly once.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.DEAD_LETTERED,
    }
)

#: States from which a task may still be admitted in the future.
PENDING_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.SUBMITTED,
        TaskState.QUEUED,
        TaskState.PARKED,
        TaskState.READY,
    }
)

_ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.SUBMITTED: frozenset({TaskState.QUEUED, TaskState.CANCELLED}),
    TaskState.QUEUED: frozenset({TaskState.READY, TaskState.PARKED, TaskState.CANCELLED}),
    # PARKED -> SUCCEEDED: a step the control plane finishes without a worker
    # (#748, contract request 52). It held no lease, so there is nothing to
    # release; only swarm-api writes it, guarded on its own claim.
    TaskState.PARKED: frozenset(
        {TaskState.READY, TaskState.CANCELLED, TaskState.DEAD_LETTERED, TaskState.SUCCEEDED}
    ),
    TaskState.READY: frozenset({TaskState.LEASED, TaskState.PARKED, TaskState.CANCELLED}),
    # A lease can be lost before dispatch (reconciler reclaim, limit reduction,
    # cancellation) which returns the task to READY without an attempt.
    TaskState.LEASED: frozenset(
        {TaskState.DISPATCHED, TaskState.READY, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.DISPATCHED: frozenset(
        {TaskState.STARTING, TaskState.READY, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.STARTING: frozenset(
        {TaskState.RUNNING, TaskState.READY, TaskState.FAILED, TaskState.CANCELLED}
    ),
    # PARKED from RUNNING is the quota-exhaustion path: the worker checkpoints,
    # releases its lease and exits rather than sleeping through a long window.
    TaskState.RUNNING: frozenset(
        {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.PARKED,
            TaskState.READY,
        }
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset({TaskState.READY, TaskState.DEAD_LETTERED}),
    TaskState.CANCELLED: frozenset(),
    TaskState.DEAD_LETTERED: frozenset(),
}


class InvalidTransition(ValueError):
    """Raised when code attempts a transition the state machine forbids."""

    def __init__(self, frm: TaskState, to: TaskState) -> None:
        super().__init__(f"illegal task transition {frm.value} -> {to.value}")
        self.frm = frm
        self.to = to


def can_transition(frm: TaskState, to: TaskState) -> bool:
    return to in _ALLOWED[frm]


def assert_transition(frm: TaskState, to: TaskState) -> None:
    if not can_transition(frm, to):
        raise InvalidTransition(frm, to)


def holds_capacity(state: TaskState) -> bool:
    return state in CONCURRENCY_STATES


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_STATES


class ParkReason(str, Enum):
    """Why a task is durably ineligible. None of these may cost compute."""

    PROVIDER_QUOTA_EXHAUSTED = "PROVIDER_QUOTA_EXHAUSTED"
    PROVIDER_COOLDOWN = "PROVIDER_COOLDOWN"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    SCHEDULED_RETRY = "SCHEDULED_RETRY"
    DEPENDENCY_INCOMPLETE = "DEPENDENCY_INCOMPLETE"
    MANUAL_PAUSE = "MANUAL_PAUSE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    # Multi-tenant addition: the tenant has not registered a key for the
    # provider this runner profile requires.
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    #: The task's agent asked to await the child tasks it submitted. Promoted by
    #: the scheduler when every child is terminal; see docs/design/child-tasks.md.
    #: Contract request 40, applied 2026-10-02.
    CHILDREN_INCOMPLETE = "CHILDREN_INCOMPLETE"
    #: A merge step waits for its pull request's checks; promoted by the
    #: scheduler's CI-wait sweep on the wake marker or the fallback instant.
    #: Contract request 49 (docs/merge-step.md, 2026-10-06, request (A)),
    #: applied 2026-10-06.
    CI_PENDING = "CI_PENDING"
    #: A live-browser hand-off whose hold expired with nobody in control: the
    #: worker checkpointed, released its lease and exited (invariant 4).
    #: Promoted ONLY by a person, through swarm-api's `human-ready`; no sweep
    #: and no timer promotes it, which is why MANUAL_PAUSE (which the scheduler
    #: promotes) could not be reused. Past its maximum it is dead-lettered.
    #: Contract request 68 (LB-A, docs/design/live-browser.md section 4.3),
    #: accepted by the owner 2026-10-11 (#1030).
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class BlockedReason(str, Enum):
    """Why a READY task was not admitted on this scheduler pass.

    Surfaced verbatim through the API so a caller can tell 'the platform is
    busy' apart from 'you personally are at your limit'.
    """

    GLOBAL_CONCURRENCY_LIMIT = "GLOBAL_CONCURRENCY_LIMIT"
    PROVIDER_CONCURRENCY_LIMIT = "PROVIDER_CONCURRENCY_LIMIT"
    RESOURCE_CLASS_LIMIT = "RESOURCE_CLASS_LIMIT"
    RUNNER_LIMIT = "RUNNER_LIMIT"
    BACKEND_LIMIT = "BACKEND_LIMIT"
    TENANT_LIMIT = "TENANT_LIMIT"
    BUDGET_LIMIT = "BUDGET_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    COOLDOWN = "COOLDOWN"
    DEPENDENCY = "DEPENDENCY"
    SCHEDULED_RETRY = "SCHEDULED_RETRY"
    MANUAL_PAUSE = "MANUAL_PAUSE"
    #: The pool's document carries no `hard_limit`: nobody set its ceiling, so
    #: admission refuses through it (contract request 38, #374). Not a limit of
    #: 0, which says an operator chose zero, and never read as unlimited.
    POOL_LIMIT_UNSET = "POOL_LIMIT_UNSET"


class EventType(str, Enum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    PARKED = "parked"
    READY = "ready"
    LEASE_ACQUIRED = "lease_acquired"
    LEASE_RELEASED = "lease_released"
    DISPATCHED = "dispatched"
    STARTING = "starting"
    RUNNING = "running"
    HEARTBEAT = "heartbeat"
    CHECKPOINT_STARTED = "checkpoint_started"
    CHECKPOINT_COMPLETED = "checkpoint_completed"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: A cancel was REQUESTED and nothing has been cancelled yet. NOT TERMINAL.
    #:
    #: Written by the API's `request_cancel` when the task holds capacity
    #: (LEASED, DISPATCHED, STARTING, RUNNING): it sets `cancel_requested` and
    #: nothing else, because only the worker, which knows its container has
    #: stopped, or the reconciler, which fences the generation first, may
    #: release the lease (invariant 1). The terminal CANCELLED that follows is
    #: written by whichever of them finishes the task.
    #:
    #: Added 2026-09-24 by contract request 17, accepted by the owner. Before
    #: it the API wrote CANCELLED with `detail.phase = "cancel_requested"`, so
    #: every reader of the TYPE saw a cancelled task that still held its lease
    #: (incident wf_ebb3ab2d65664707a559: four tasks, over an hour). Events
    #: stored before then keep that shape; `swarm_api.codec.event_from_dict`
    #: reads them as this type.
    CANCEL_REQUESTED = "cancel_requested"
    #: The task REACHED CANCELLED. Terminal. Written by whoever made the
    #: transition: the API for a task that held nothing, the worker
    #: (`control.finish`), the reconciler (F-3) or the scheduler (`cancel`).
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"
    GENERATION_FENCED = "generation_fenced"
    #: The live-browser hand-off events (docs/design/live-browser.md section 4).
    #: The task stays RUNNING throughout; `Task.human_wait` carries the wait.
    #: Contract request 69 (LB-B), accepted by the owner 2026-10-11 (#1030).
    #:
    #: The agent asked for a person; the worker paused its CDP and swarm-api
    #: set `human_wait`.
    HUMAN_HANDOFF_REQUESTED = "human_handoff_requested"
    #: A person took control of the browser.
    HUMAN_CONTROL_TAKEN = "human_control_taken"
    #: Control ended, by the Return press or by any of the audited causes.
    HUMAN_CONTROL_RETURNED = "human_control_returned"
    #: The wait is over and `human_wait` is cleared; the agent carries on.
    HUMAN_HANDOFF_RESOLVED = "human_handoff_resolved"
