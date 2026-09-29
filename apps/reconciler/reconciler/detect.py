"""Detection: what does the control plane believe that reality disagrees with?

Pure functions over the views in `model.py`. Nothing here writes, terminates or
releases anything -- it produces `Finding` objects, and `repair.py` decides what
to do with them in the one order that is safe.

Four disagreements matter, and each one costs something different:

    stale lease        the worker is gone but the slot is still reserved
                       -> capacity leaks; the platform slowly stops admitting
    missing execution  the control plane thinks it dispatched, but no execution
                       exists -> the task is stuck forever holding a slot
    orphan execution   compute is running that no lease accounts for
                       -> money, and a second agent on someone's repository
    orphan lease       a lease outlives its task
                       -> capacity leaks, same as stale

Note what is NOT a finding: a task in LEASED with a fresh lease and no execution
yet. Dispatch takes time, image pulls take minutes, and a reconciler that treats
"not started yet" as "dead" would kill every cold start on the platform.

Two more apply to GKE Jobs only, because browser pods carry
`safe-to-evict=false` and nothing else in the cluster will ever reclaim one:

    stuck, no progress the worker is alive and heartbeating, but the attempt
                       has shown no progress (`progress.py`) for longer than
                       `stuck_after_seconds` -> fence now; the worker stops
                       itself, and a later pass terminates if need be,
                       releases, and requeues or fails by the retry rule
    left running       the task is already terminal and its Job is still
                       active -> terminate; release only that Job's own lease

And two about how a worker ENDED, on either backend:

    cannot start       the current attempt's execution finished with exit 78,
                       "the worker cannot start" -> fence, release, and FAIL
                       the task now, with the worker's cause, no retry
                       (owner, 2026-09-25; `detect_cannot_start`)
    ended at startup   the current attempt's execution finished with any
                       other code while its task is still DISPATCHED or
                       STARTING, so its runner never started -> fence,
                       release and requeue now, with the exit code and the
                       execution in `last_error`, instead of waiting for the
                       dispatch deadline (#198; `detect_ended_at_startup`)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

#: The slug character class is IMPORTED, never restated. `sanitised()` below
#: undoes what the dispatcher's `sanitize_name` did, so the two escaping
#: rules are one rule -- a private copy here is the half that would
#: silently stop round-tripping, and the reconciler would then either kill
#: a live execution it read as orphaned or never find a real orphan.
#: docs/audits/2026-09-18/08-frozen-contract-restatements.md, finding 1.
from swarm_common.identity import _TENANT_SAFE as _NAME_SAFE
from swarm_common.models import retries_exhausted, utcnow
from swarm_common.profiles import Backend
from swarm_common.states import TaskState

from .config import ReconcilerConfig
from .model import (
    AttemptView,
    ControlSnapshot,
    ExecutionPhase,
    ExecutionView,
    JobResourceView,
    LeaseView,
    TaskView,
)

#: The backend the two eviction rules act on, spelled by the frozen contract.
#: `backends.GkeBackend.name` is the same string; importing that module here
#: would be circular (it imports `sanitised` from this one).
GKE = Backend.GKE_AUTOPILOT.value

#: The worker's exit code for "cannot start, and another attempt would fail the
#: same way": `agent_worker.errors.ExitCode.CONFIG`.
#:
#: RESTATED, because nothing both images share can hold it. The reconciler's
#: image carries `apps/common` and `apps/reconciler` and nothing else
#: (images/swarm-reconciler/Dockerfile), and the frozen contract names no worker
#: exit code. Contract request 21 (docs/contract-change-requests.md) asks for
#: one. Until then, tests/unit/worker/test_worker_cannot_start.py
#: (`test_the_reconcilers_78_is_the_workers_78`) holds the two together.
WORKER_EXIT_CANNOT_START = 78

#: How much of a worker's own account of itself reaches a task's `last_error`.
#: It is text a tenant's pod produced, and the store keeps 2000 characters of
#: `last_error`, so the cause gets a quarter of that and one line.
MAX_CAUSE_CHARS = 500


class FindingKind(str, Enum):
    STALE_LEASE = "stale_lease"
    MISSING_EXECUTION = "missing_execution"
    ORPHAN_EXECUTION = "orphan_execution"
    ORPHAN_LEASE = "orphan_lease"
    DEAD_WORKER = "dead_worker"
    OBSOLETE_GENERATION = "obsolete_generation"
    UNUSED_JOB_RESOURCE = "unused_job_resource"
    EMPTY_NAMESPACE = "empty_namespace"
    #: A checkpoint nothing can resume from: see `checkpoints.classify`.
    RECLAIMABLE_CHECKPOINT = "reclaimable_checkpoint"
    #: A checkpoint whose task or attempt document is gone, so no reference can
    #: be established either way. The only finding in this module decided by a
    #: clock rather than by state.
    ORPHAN_CHECKPOINT = "orphan_checkpoint"
    #: A GKE attempt whose lease is heartbeating and which has shown no
    #: progress for `stuck_after_seconds`. See `progress.py` for what progress is.
    STUCK_NO_PROGRESS = "stuck_no_progress"
    #: A GKE Job still active after its task reached a terminal state.
    LEFT_RUNNING = "left_running"
    #: The current attempt's execution finished with `WORKER_EXIT_CANNOT_START`.
    WORKER_CANNOT_START = "worker_cannot_start"
    #: The current attempt's execution finished with any other code while its
    #: task was still DISPATCHED or STARTING: see `detect_ended_at_startup`.
    WORKER_ENDED_AT_STARTUP = "worker_ended_at_startup"


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    reason: str
    task_id: str | None = None
    lease_id: str | None = None
    attempt_id: str | None = None
    tenant_id: str | None = None
    generation: int | None = None
    execution: ExecutionView | None = None
    resource: JobResourceView | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def requires_termination(self) -> bool:
        """True when something might still be executing.

        This is the flag that orders the repair: a finding that requires
        termination must have its execution stopped and its generation
        invalidated BEFORE its slot is returned, or the platform can hand the
        slot to a second attempt while the first is still running.
        """
        return self.kind in (
            FindingKind.DEAD_WORKER,
            FindingKind.ORPHAN_EXECUTION,
            FindingKind.OBSOLETE_GENERATION,
            FindingKind.STUCK_NO_PROGRESS,
            FindingKind.LEFT_RUNNING,
        ) or (self.execution is not None and self.execution.is_active)


def sanitised(value: str) -> str:
    """The shape an identifier takes once it has been through a label.

    Mirrors the dispatcher's `sanitize_name` for the cases that matter here:
    `task_9f3a` becomes `task-9f3a`. Reproducing it is what lets a label-derived
    identifier be matched back to the Firestore document it names.
    """
    slug = _NAME_SAFE.sub("-", str(value).lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _resolve(value: str | None, index: dict[str, str]) -> str | None:
    """Map a possibly-sanitised identifier back to the real one."""
    if not value:
        return None
    return index.get(sanitised(value), value)


def normalise_executions(
    snapshot: ControlSnapshot, executions: Iterable[ExecutionView]
) -> list[ExecutionView]:
    """Resolve label-derived identifiers back to control-plane document ids.

    A backend that could only read an identifier from a label hands us a
    sanitised copy of it -- `task-9f3a` where Firestore holds `task_9f3a`. Left
    alone, that fails every lookup in this module, and an execution whose task
    cannot be found is an orphan, which gets terminated. Terminating a healthy
    agent because of a character-class rule is not a trade worth making, so the
    ids are resolved against the snapshot before any rule runs.
    """
    task_index = {sanitised(task_id): task_id for task_id in snapshot.tasks}
    attempt_index = {
        sanitised(attempt_id): attempt_id
        for attempt_id in {
            *snapshot.attempts.keys(),
            *(lease.attempt_id for lease in snapshot.leases.values() if lease.attempt_id),
        }
    }
    resolved: list[ExecutionView] = []
    for execution in executions:
        task_id = _resolve(execution.task_id, task_index)
        attempt_id = _resolve(execution.attempt_id, attempt_index)
        if task_id == execution.task_id and attempt_id == execution.attempt_id:
            resolved.append(execution)
            continue
        resolved.append(replace(execution, task_id=task_id, attempt_id=attempt_id))
    return resolved


def scope_executions_to_their_tenant(
    snapshot: ControlSnapshot,
    executions: Iterable[ExecutionView],
    logger: Any | None = None,
) -> list[ExecutionView]:
    """Refuse an execution's claim on a task belonging to a different tenant.

    `backends.owning_tenant` has already replaced whatever the container's
    environment said with the tenant of the object that encloses it -- its
    namespace, or the per-tenant Cloud Run Job resource. This is the other half:
    the TASK id is still container-supplied, and every repair in `repair.py`
    acts on it with a cluster-wide identity. An execution naming another
    tenant's task and its current generation would otherwise invalidate that
    generation, release the victim's lease and re-queue their work -- a
    cross-tenant kill switch pressed by the one component trusted to press it.

    So a mismatch strips the task and attempt from the view rather than
    correcting them. What remains is an execution the control plane cannot
    account for, running in the claimant's own namespace, which the orphan rule
    then terminates: the blast radius is the attacker's own pod.
    """
    scoped: list[ExecutionView] = []
    for execution in executions:
        # `task_named`, not `tasks`: a finished task is read by id (`settled`),
        # and the left-running rule WRITES an event onto it. A claim on another
        # tenant's finished task must be refused as firmly as one on a running
        # task, or that event lands in the victim's stream.
        task = snapshot.task_named(execution.task_id)
        if (
            task is None
            or not execution.tenant_id
            or not task.tenant_id
            or task.tenant_id == execution.tenant_id
        ):
            scoped.append(execution)
            continue
        if logger is not None:
            logger.error(
                "refusing an execution's claim on another tenant's task",
                execution=execution.name,
                execution_tenant=execution.tenant_id,
                claimed_task=execution.task_id,
                task_tenant=task.tenant_id,
            )
        # The claim is stripped AND the refusal is recorded. Stripping alone
        # would make this indistinguishable from an execution that never
        # claimed anything, which is the shape detect_orphan_executions must
        # leave alone.
        scoped.append(
            replace(
                execution,
                task_id=None,
                attempt_id=None,
                generation=None,
                claim_refused=True,
            )
        )
    return scoped


def detect_stale_leases(
    snapshot: ControlSnapshot,
    executions_by_attempt: dict[str, ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """Leases whose worker has stopped proving it is alive.

    NOT YET ALIVE is a different condition from SILENT, and only one clock is
    entitled to judge each of them.

    A lease is written with `heartbeat_at: None` (`swarm_common/admission.py:224`),
    so until the worker's first control-plane write `silent_seconds` falls back
    to `created_at`. During that window both liveness clocks --
    `heartbeat_grace_seconds` (90) and `expires_at`, which is
    `created_at + lease_timeout_seconds` (120) -- are not measuring silence at
    all. There is no process alive to be silent. They are measuring how long the
    dispatch has been in flight, which is exactly what `dispatch_deadline`
    (`created_at + dispatch_timeout_seconds`, 300) is for. The two answers
    disagree by 180 seconds, and the shorter one was winning.

    Measured 2026-09-22 from the live event streams, `dispatched` -> the
    worker's first control-plane write, Cloud Run Jobs:

        claude-code   n=41   min  46.2s   p50 122.6s   p90 159.0s   max 226.2s
        mock          n=27   min  68.5s   p50 250.1s   p90 254.5s   max 256.1s

    55 of those 68 attempts exceed the 90s grace and 44 exceed the 120s lease
    timeout. NONE exceeds the 300s dispatch deadline.

    Watched live on task_b5dc2568713a40158851, whose first attempt was killed
    183s in by this very function, with its own reason string recording the
    contradiction: "lease silent for 183s (grace 90s, expired=True,
    dispatch_overdue=False)". The dispatch window still had 117 seconds to run.
    The replacement worker booted, found its generation superseded and exited 70
    without running the agent -- invariant 5 working perfectly on a worker that
    was never unhealthy. The task succeeded on generation 3, twelve minutes
    after submission.

    Raising the timeouts was the other option and is worse: p90 is 159s and the
    worst observed is 256s, so any value large enough would simply be
    `dispatch_timeout_seconds` under a second name, and every GENUINE dead
    worker would then wait that long to be reclaimed. Heartbeating earlier in
    the worker cannot help either -- it already does (`lifecycle.py:332`, moved
    there on 2026-09-19) and this whole window is before any worker code runs.

    So: before the first heartbeat, the dispatch deadline is the only clock that
    applies. After it, the worker has proven it exists, `silent_seconds` means
    what it says, and the liveness clocks take over.
    """
    now = now or utcnow()
    findings: list[Finding] = []
    for lease in snapshot.leases.values():
        if lease.is_released:
            continue
        task = snapshot.tasks.get(lease.task_id)
        silent = lease.silent_seconds(now)
        expired = lease.expires_at is not None and now > lease.expires_at
        deadline_passed = lease.dispatch_deadline is not None and now > lease.dispatch_deadline

        if lease.heartbeat_at is None and lease.dispatch_deadline is not None:
            # Never alive. Judged by the dispatch deadline alone -- and by it
            # whatever the lease's state, which is the second half of the same
            # defect. `mark_dispatched` moves the lease to DISPATCHED
            # (`scheduler/store.py:235-237`) the moment the backend ACCEPTS the
            # create call, long before a container runs. The old
            # `state is TaskState.LEASED` guard therefore switched the deadline
            # off for precisely the leases it was meant to bound: measured on
            # the same task above, `dispatch_overdue` was still False 301s after
            # admission. Suppressing the liveness clocks without widening this
            # would have left such a lease with no clock at all and leaked its
            # slots for ever.
            #
            # A lease with no `dispatch_deadline` -- a document written before
            # the field existed -- falls through to the liveness clocks below,
            # for that same reason: some clock has to reclaim it.
            overdue_dispatch = deadline_passed
            if not overdue_dispatch:
                continue
        else:
            overdue_dispatch = lease.state is TaskState.LEASED and deadline_passed
            if not (expired or overdue_dispatch) and silent <= config.heartbeat_grace_seconds:
                continue
        if task is not None and task.generation != lease.generation:
            # Superseded. The generation rule handles the case where an
            # EXECUTION is still running under the old generation -- but it acts
            # on executions, and a lease that never dispatched has none. Skipping
            # unconditionally here is how the reconciler strands its own partial
            # repair: `invalidate_generation` and `release_lease` are separate
            # transactions, so anything interrupting between them leaves the
            # lease unreleased at the old generation, and every later pass then
            # skips it as "superseded". The slots it holds are never returned.
            #
            # Observed live: task gen 2, lease gen 1, released_at None, five
            # pools each holding active=1 an hour after the dispatch deadline.
            #
            # So skip only when there is something else to act on -- an
            # execution the generation rule will reach. An unreleased lease with
            # no execution is a leak, and is reported.
            if executions_by_attempt.get(lease.attempt_id) is not None:
                continue
            findings.append(
                Finding(
                    kind=FindingKind.ORPHAN_LEASE,
                    reason=(
                        f"lease generation {lease.generation} superseded by task "
                        f"generation {task.generation} with no execution; slots "
                        "would never be returned"
                    ),
                    task_id=lease.task_id,
                    lease_id=lease.lease_id,
                    attempt_id=lease.attempt_id,
                    tenant_id=lease.tenant_id,
                    generation=lease.generation,
                )
            )
            continue

        execution = executions_by_attempt.get(lease.attempt_id)
        kind = (
            FindingKind.DEAD_WORKER
            if execution is not None and execution.is_active
            else FindingKind.STALE_LEASE
        )
        findings.append(
            Finding(
                kind=kind,
                reason=(
                    f"lease silent for {silent:.0f}s "
                    f"(grace {config.heartbeat_grace_seconds}s, expired={expired}, "
                    f"dispatch_overdue={overdue_dispatch})"
                ),
                task_id=lease.task_id,
                lease_id=lease.lease_id,
                attempt_id=lease.attempt_id,
                tenant_id=lease.tenant_id,
                generation=lease.generation,
                execution=execution,
                detail={
                    "silent_seconds": round(silent, 1),
                    "expired": expired,
                    "dispatch_overdue": overdue_dispatch,
                    "task_state": task.state.value if task else None,
                },
            )
        )
    return findings


def detect_missing_executions(
    snapshot: ControlSnapshot,
    executions_by_attempt: dict[str, ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """Tasks the control plane believes are running, with nothing behind them."""
    now = now or utcnow()
    findings: list[Finding] = []
    for task in snapshot.tasks.values():
        if task.state not in (TaskState.DISPATCHED, TaskState.STARTING, TaskState.RUNNING):
            continue
        lease = snapshot.lease_for_task(task.task_id)
        if lease is None or lease.is_released:
            continue
        if lease.attempt_id in executions_by_attempt:
            continue
        attempt = snapshot.attempts.get(lease.attempt_id)
        reference = (attempt.created_at if attempt else None) or lease.created_at
        if reference is None:
            continue
        age = (now - reference).total_seconds()
        if age < config.missing_execution_grace_seconds:
            continue  # still plausibly starting: image pulls are slow
        findings.append(
            Finding(
                kind=FindingKind.MISSING_EXECUTION,
                reason=(
                    f"task is {task.state.value} but no backend execution exists "
                    f"{age:.0f}s after dispatch"
                ),
                task_id=task.task_id,
                lease_id=lease.lease_id,
                attempt_id=lease.attempt_id,
                tenant_id=task.tenant_id,
                generation=lease.generation,
                detail={"age_seconds": round(age, 1), "task_state": task.state.value},
            )
        )
    return findings


#: Why `detect_orphan_executions` leaves an active execution to a later
#: judgement instead of judging it now. Each is a promise that SOME rule will
#: own this execution -- kill it, then release -- so for the pass, no rule that
#: only releases may hand its lease back either (`detect_all`).
DEFER_TO_LEFT_RUNNING = "its task is terminal and detect_left_running owns its Job, grace and all"
DEFER_TASK_UNREADABLE = "its task could not be read this pass"
DEFER_TASK_READMITTED = "its task was re-admitted after the snapshot"


def _settled_tasks(snapshot: ControlSnapshot) -> dict[str, TaskView]:
    # Read with defaults: the rules here are also called with a bare snapshot
    # that predates the by-id reads, and for those they must behave exactly as
    # they did before them.
    return getattr(snapshot, "settled", None) or {}


def _unreadable_tasks(snapshot: ControlSnapshot) -> set[str]:
    return getattr(snapshot, "unreadable_tasks", None) or set()


def _unissued(execution: ExecutionView, task: TaskView | None) -> bool:
    """The execution claims a generation NO admission issued.

    `admission.py` raises `current_generation` in the same transaction that
    writes the lease, before any dispatch, and the task was read after the
    backends were listed -- so an execution claiming more than the task records
    was not created by this platform's dispatch path. It is not the "newer
    attempt" the eviction rules protect; that one is never newer than its own
    task. The obsolete-generation rule already treats such a claim on a running
    task the same way.
    """
    return (
        task is not None
        and execution.generation is not None
        and execution.generation > task.generation
    )


def orphan_rule_defers(
    snapshot: ControlSnapshot, execution: ExecutionView, config: ReconcilerConfig
) -> str | None:
    """Why the orphan-execution rule leaves this execution for later, or None.

    Only the three stand-asides the by-id read introduced. The older skips --
    too young to judge, no task id at all -- are not deferrals: the first is a
    grace every rule shares, and the second is compute this service has no
    authority over, which no rule will ever own.
    """
    if not execution.is_active or execution.claim_refused or not execution.task_id:
        return None
    task = snapshot.tasks.get(execution.task_id)
    settled = _settled_tasks(snapshot).get(execution.task_id)
    if task is None and execution.task_id in _unreadable_tasks(snapshot):
        # Its task exists or not, finished or re-admitted -- this pass could
        # not look. Terminating on "I could not read it" is the same mistake
        # as releasing on "I could not list it".
        return DEFER_TASK_UNREADABLE
    if task is None and settled is not None and settled.holds_capacity:
        # Re-admitted between the snapshot and the read by id. Whatever this
        # execution is -- the new attempt, or an old one -- the next pass holds
        # the task, its lease and its generation together.
        return DEFER_TASK_READMITTED
    named = task or settled
    if (
        named is not None
        and named.is_terminal
        and not _unissued(execution, named)
        and execution.backend == GKE
        and bool(getattr(config, "enable_gke_eviction", False))
    ):
        return DEFER_TO_LEFT_RUNNING
    return None


def detect_orphan_executions(
    snapshot: ControlSnapshot,
    executions: Iterable[ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """Compute that is running with no live lease behind it."""
    now = now or utcnow()
    findings: list[Finding] = []
    settled_tasks = _settled_tasks(snapshot)
    for execution in executions:
        if not execution.is_active:
            continue
        age = (now - execution.created_at).total_seconds() if execution.created_at else None
        if age is not None and age < config.orphan_execution_grace_seconds:
            continue  # the dispatcher may not have written the lease yet

        task = snapshot.tasks.get(execution.task_id) if execution.task_id else None
        #: The task read by id when `snapshot()` did not return it: a task
        #: outside the concurrency states, finished ones included.
        settled = settled_tasks.get(execution.task_id) if execution.task_id else None
        lease = None
        if execution.attempt_id:
            lease = next(
                (
                    candidate
                    for candidate in snapshot.leases.values()
                    if candidate.attempt_id == execution.attempt_id
                ),
                None,
            )

        reason: str | None = None
        kind = FindingKind.ORPHAN_EXECUTION

        # NO task_id AT ALL IS NOT AN ORPHAN. It means this is not a worker
        # execution, and this service has no authority over it.
        #
        # These were one condition, and the difference is the whole blast
        # radius. The dispatcher sets TASK_ID on every worker execution it
        # creates, so an execution WITHOUT one was created by something else.
        # The Cloud Run backend selects jobs by the `swarm-` name prefix plus a
        # platform label, and `swarm-verify` -- the verification gate, created
        # by terraform with managed-by=swarm-terraform -- matches both. On
        # 2026-09-21 this cancelled the gate's own executions four times, at
        # about four minutes each, while the task the gate was waiting on took
        # five minutes seventeen: the one run that would have proved the
        # platform works was killed by the platform.
        #
        # It is worse than an own goal. saga-agents-staging is SHARED, and the
        # only things between this loop and another team's Cloud Run job are a
        # name prefix and a label neither of which they are obliged to avoid. A
        # reconciler that cannot attribute compute to a task must not terminate
        # it -- that is what its authority is FOR.
        if execution.task_id is None and not execution.claim_refused:
            log = getattr(config, "logger", None)
            if log is not None:
                log.info(
                    "ignoring an execution that carries no task id; not a worker execution",
                    execution=getattr(execution, "name", ""),
                    parent=getattr(execution, "parent", ""),
                )
            continue

        if orphan_rule_defers(snapshot, execution, config) is not None:
            # Left for a later judgement. `detect_all` holds this execution's
            # lease against every rule that only releases, for the same pass.
            continue
        unissued = _unissued(execution, task or settled)

        if execution.claim_refused:
            # Real compute, running in its own tenant, that asserted a claim on
            # another tenant's task. The claim is void; the compute is not.
            reason = "execution claimed a task in another tenant; the claim was refused"
        elif task is None and settled is not None:
            if unissued:
                reason = (
                    f"execution claims generation {execution.generation} but its task is "
                    f"{settled.state.value} at generation {settled.generation}; no admission "
                    "issued it"
                )
            elif settled.is_terminal:
                reason = f"task is already {settled.state.value}"
            else:
                reason = f"task is {settled.state.value} and holds no capacity"
        elif task is None:
            reason = "execution carries no task this control plane knows about"
        elif task.is_terminal:
            reason = f"task is already {task.state.value}"
        elif execution.generation is not None and execution.generation != task.generation:
            # Checked before the lease rules: a superseded generation whose
            # lease was already released is the COMMON case, and calling it a
            # plain orphan would hide the fact that a newer attempt is running
            # the same task right now.
            kind = FindingKind.OBSOLETE_GENERATION
            reason = (
                f"execution is generation {execution.generation} but the task is at "
                f"{task.generation}"
            )
        elif lease is None:
            reason = "no lease exists for this execution's attempt"
        elif lease.is_released:
            reason = "the lease for this execution has been released"
        if reason is None:
            continue

        findings.append(
            Finding(
                kind=kind,
                reason=reason,
                task_id=execution.task_id,
                lease_id=lease.lease_id if lease else None,
                attempt_id=execution.attempt_id,
                tenant_id=execution.tenant_id,
                generation=execution.generation,
                execution=execution,
                detail={"age_seconds": round(age, 1) if age is not None else None},
            )
        )
    return findings


def detect_orphan_leases(
    snapshot: ControlSnapshot,
    now: datetime | None = None,
    executions_by_attempt: dict[str, ExecutionView] | None = None,
) -> list[Finding]:
    """Leases held by tasks that are finished, gone, or pointing elsewhere.

    This rule releases WITHOUT looking for compute: its repair has no execution
    to kill. So it stands aside for any lease whose own attempt still has an
    active execution in `executions_by_attempt`. That execution belongs to the
    rules that kill before they release -- orphan execution, obsolete
    generation, dead worker, left running -- and until one of them has stopped
    it, the slot it holds is in use. Returning it lets the scheduler admit
    another task onto capacity that is still being spent: for a browser
    attempt, 2 units on each of seven pools under an 8 vCPU pod. Once the
    execution has ended it is no longer listed as active, and this rule
    releases as it always did.

    A lease whose task could not be read this pass is held too, and is never
    reported as naming a task that "no longer exists": that would be a read
    that FAILED recorded as an absence.
    """
    findings: list[Finding] = []
    running = executions_by_attempt or {}
    unreadable = _unreadable_tasks(snapshot)
    for lease in snapshot.leases.values():
        if lease.is_released:
            continue
        execution = running.get(lease.attempt_id) if lease.attempt_id else None
        if execution is not None and execution.is_active:
            continue
        if lease.task_id not in snapshot.tasks and lease.task_id in unreadable:
            continue
        # A finished task read by id says what it is. A task absent from both
        # reads -- outside the concurrency states and never read by id, or
        # read and not found -- keeps the wording this rule always used. Same
        # action either way: the lease is released.
        task = snapshot.task_named(lease.task_id)
        if task is None:
            reason = "lease references a task that no longer exists"
        elif task.is_terminal:
            reason = f"task is {task.state.value} but the lease was never released"
        elif not task.holds_capacity:
            reason = f"task is {task.state.value} and should hold no capacity"
        elif task.lease_id not in (None, lease.lease_id):
            reason = "task points at a different lease"
        else:
            continue
        findings.append(
            Finding(
                kind=FindingKind.ORPHAN_LEASE,
                reason=reason,
                task_id=lease.task_id,
                lease_id=lease.lease_id,
                attempt_id=lease.attempt_id,
                tenant_id=lease.tenant_id,
                generation=lease.generation,
                detail={"task_state": task.state.value if task else None},
            )
        )
    return findings


@dataclass(frozen=True)
class StuckSubject:
    """A running GKE attempt the stuck rule could act on, before any evidence."""

    task: TaskView
    lease: LeaseView
    execution: ExecutionView
    started_at: datetime | None


def _stuck_subject(
    snapshot: ControlSnapshot,
    execution: ExecutionView,
    config: ReconcilerConfig,
    now: datetime,
) -> StuckSubject | None:
    """The attempt behind this execution, IF it is one the stuck rule may judge.

    Every condition here narrows the rule to one precise situation: a live
    worker, on the task's CURRENT generation, holding the task's CURRENT lease,
    in RUNNING. Anything else belongs to a rule that already exists -- a silent
    lease to `detect_stale_leases`, an old generation to the obsolete-generation
    rule, a finished task to `detect_left_running` -- and must not be judged
    twice by rules that would disagree about what to do with it.
    """
    if execution.backend != GKE or not execution.is_active:
        return None
    if execution.claim_refused or not execution.task_id or not execution.attempt_id:
        return None
    task = snapshot.tasks.get(execution.task_id)
    if task is None or task.state is not TaskState.RUNNING:
        return None
    lease = next(
        (
            candidate
            for candidate in snapshot.leases.values()
            if candidate.attempt_id == execution.attempt_id and not candidate.is_released
        ),
        None,
    )
    if lease is None or lease.task_id != task.task_id:
        return None
    if lease.generation != task.generation:
        return None
    if execution.generation is not None and execution.generation != lease.generation:
        return None
    if task.lease_id not in (None, lease.lease_id):
        return None
    # Alive by the ordinary liveness rule, or this is not the stuck rule's case.
    if lease.heartbeat_at is None or lease.silent_seconds(now) > config.heartbeat_grace_seconds:
        return None
    attempt = snapshot.attempts.get(execution.attempt_id)
    started = (attempt.started_at if attempt else None) or task.started_at
    if started is None:
        started = lease.created_at
    if started is not None and (now - started).total_seconds() < config.stuck_after_seconds:
        return None  # has not been running long enough to be stuck at all
    return StuckSubject(task=task, lease=lease, execution=execution, started_at=started)


def stuck_candidates(
    snapshot: ControlSnapshot,
    executions: Iterable[ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[StuckSubject]:
    """The attempts whose progress evidence is worth reading this pass.

    Used by the reconciler to decide which event streams to read, so that an
    attempt younger than the threshold -- nearly all of them -- costs nothing.
    """
    if not config.enable_gke_eviction:
        return []
    now = now or utcnow()
    prepared = scope_executions_to_their_tenant(
        snapshot, normalise_executions(snapshot, executions)
    )
    subjects: list[StuckSubject] = []
    for execution in prepared:
        subject = _stuck_subject(snapshot, execution, config, now)
        if subject is not None:
            subjects.append(subject)
    return subjects


def detect_stuck_executions(
    snapshot: ControlSnapshot,
    executions_by_attempt: dict[str, ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """GKE attempts that are alive, current, and making no progress.

    Only on EVIDENCE: an attempt with no assessment in `snapshot.progress`, or
    one whose assessment could not be judged, produces nothing. The repair
    (`repair.Reconciler._repair_stuck`) FENCES in this pass and nothing more:
    the live worker stops its agent at its next poll, and a later pass finds a
    superseded, silent lease that the existing rules release -- terminating
    the Job first if it is still active -- through the frozen
    `release_lease_in_transaction`, then READY, or FAILED once the attempts
    are spent, or CANCELLED if a cancel was asked for.
    """
    if not config.enable_gke_eviction:
        return []
    now = now or utcnow()
    findings: list[Finding] = []
    for attempt_id, execution in executions_by_attempt.items():
        subject = _stuck_subject(snapshot, execution, config, now)
        if subject is None:
            continue
        evidence = snapshot.progress.get(attempt_id)
        if evidence is None or not getattr(evidence, "stuck", False):
            continue
        findings.append(
            Finding(
                kind=FindingKind.STUCK_NO_PROGRESS,
                reason=evidence.reason(),
                task_id=subject.task.task_id,
                lease_id=subject.lease.lease_id,
                attempt_id=attempt_id,
                tenant_id=subject.task.tenant_id or execution.tenant_id,
                generation=subject.lease.generation,
                execution=execution,
                detail={
                    **evidence.as_detail(),
                    "task_state": subject.task.state.value,
                    "namespace": execution.namespace,
                },
            )
        )
    return findings


def detect_left_running(
    snapshot: ControlSnapshot,
    executions: Iterable[ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """GKE Jobs still active after their task reached a terminal state.

    Nothing is fenced: a terminal task has no generation left to run under,
    and `invalidate_generation` would refuse it anyway. The Job is terminated,
    and a lease is named on the finding only when it is THIS Job's own -- the
    same attempt, the same generation, still unreleased. A lease of any other
    generation is never touched.

    Inside the grace this rule produces nothing, and nothing else acts either:
    the orphan rule defers to this one (`orphan_rule_defers`), and `detect_all`
    holds the Job's lease against every rule that only releases. So a worker
    wedged between `finish()`'s terminal write and its release keeps its slot
    until this rule has killed its Job -- it is still spending it.

    A Job claiming a generation above the task's is not this rule's: no
    admission issued that generation, and `detect_orphan_executions` names it
    for what it is.
    """
    if not config.enable_gke_eviction:
        return []
    now = now or utcnow()
    findings: list[Finding] = []
    for execution in executions:
        if execution.backend != GKE or not execution.is_active:
            continue
        if execution.claim_refused or not execution.task_id:
            continue
        task = snapshot.task_named(execution.task_id)
        if task is None or not task.is_terminal:
            continue
        if execution.generation is not None and execution.generation > task.generation:
            continue
        finished = task.completed_at or task.updated_at
        reference = finished or execution.created_at
        # No clock at all is not a reason to wait for ever: the orphan rule
        # stood aside for this Job on the promise that this rule owns it, and
        # its lease is held for as long as it runs. With nothing to say it
        # finished recently, it is treated as past the grace -- which is what
        # the orphan rule would have done with it.
        after = (now - reference).total_seconds() if reference is not None else None
        if after is not None and after < config.left_running_grace_seconds:
            continue  # the worker may still be exiting on its own
        lease = next(
            (
                candidate
                for candidate in snapshot.leases.values()
                if candidate.attempt_id == execution.attempt_id
                and candidate.task_id == task.task_id
                and not candidate.is_released
                and (execution.generation is None or candidate.generation == execution.generation)
            ),
            None,
        )
        where = f"{execution.namespace}/{execution.name}" if execution.namespace else execution.name
        since = (
            f"{after:.0f}s after it finished" if after is not None
            else "and nothing records when it finished"
        )
        findings.append(
            Finding(
                kind=FindingKind.LEFT_RUNNING,
                reason=(
                    f"task is {task.state.value} but its GKE job {where} is still active "
                    f"{since} (grace {config.left_running_grace_seconds}s)"
                ),
                task_id=task.task_id,
                lease_id=lease.lease_id if lease else None,
                attempt_id=execution.attempt_id,
                tenant_id=task.tenant_id or execution.tenant_id,
                generation=execution.generation,
                execution=execution,
                detail={
                    "task_state": task.state.value,
                    "finished_at": finished.isoformat() if finished else None,
                    "active_seconds_after_finish": round(after, 1) if after is not None else None,
                    "lease_unreleased": lease is not None,
                    "namespace": execution.namespace,
                },
            )
        )
    return findings


@dataclass(frozen=True)
class CannotStartSubject:
    """A finished execution whose attempt still holds its task's current lease."""

    task: TaskView
    lease: LeaseView
    execution: ExecutionView


def _cannot_start_subject(
    snapshot: ControlSnapshot, execution: ExecutionView
) -> CannotStartSubject | None:
    """The attempt behind this FINISHED execution, if its exit code could decide the task.

    Narrow on purpose, like `_stuck_subject`: the execution FAILED (a worker
    that exits 78 fails its Cloud Run task and its Job), it names a task this
    snapshot holds in a concurrency state, and its attempt holds that task's
    CURRENT lease at the task's CURRENT generation. Anything else belongs to a
    rule that already exists. A superseded attempt's 78 says nothing about
    the attempt that replaced it.
    """
    if execution.is_active or execution.phase is not ExecutionPhase.FAILED:
        return None
    if execution.claim_refused or not execution.task_id or not execution.attempt_id:
        return None
    task = snapshot.tasks.get(execution.task_id)
    if task is None or not task.holds_capacity:
        return None
    lease = next(
        (
            candidate
            for candidate in snapshot.leases.values()
            if candidate.attempt_id == execution.attempt_id and not candidate.is_released
        ),
        None,
    )
    if lease is None or lease.task_id != task.task_id:
        return None
    if lease.generation != task.generation:
        return None
    if execution.generation is not None and execution.generation != lease.generation:
        return None
    if task.lease_id not in (None, lease.lease_id):
        return None
    return CannotStartSubject(task=task, lease=lease, execution=execution)


def cannot_start_candidates(
    snapshot: ControlSnapshot, executions: Iterable[ExecutionView]
) -> list[CannotStartSubject]:
    """The finished executions whose exit code is worth reading this pass.

    The subjects of both exit-code rules: `detect_cannot_start` and
    `detect_ended_at_startup`.

    Used by the reconciler to decide which terminations to read, so that a
    pass with no failed execution under a live lease, which is nearly every
    pass, costs no call. An attempt that ALSO has an active execution is left
    out: something of it is still running, and the rules that kill before
    they release own it.
    """
    prepared = scope_executions_to_their_tenant(
        snapshot, normalise_executions(snapshot, executions)
    )
    active = {e.attempt_id for e in prepared if e.is_active and e.attempt_id}
    subjects: list[CannotStartSubject] = []
    seen: set[str] = set()
    for execution in prepared:
        if execution.attempt_id in active:
            continue
        subject = _cannot_start_subject(snapshot, execution)
        if subject is None or subject.lease.lease_id in seen:
            continue
        seen.add(subject.lease.lease_id)
        subjects.append(subject)
    return subjects


def _one_line(text: Any) -> str:
    """Printable, single-line, bounded: what may be put in a task's `last_error`."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(text))
    collapsed = " ".join(cleaned.split())
    if len(collapsed) > MAX_CAUSE_CHARS:
        collapsed = collapsed[: MAX_CAUSE_CHARS - 3].rstrip() + "..."
    return collapsed


def worker_cause(message: str | None) -> str | None:
    """The worker's own one-line cause, from its termination message, or None.

    The worker writes ONE JSON line, its last structured log line with a
    short `cause` beside the `message` (`agent_worker.startup.
    write_termination_message`). The last line that parses is read, `cause`
    before `message`. Anything else there, a crash's traceback or a
    kubelet-supplied log tail, is taken as its last non-empty line. Always
    one printable line of at most `MAX_CAUSE_CHARS`: it is text a tenant's
    pod wrote, and it is about to be shown on the tenant's task.
    """
    if not message or not str(message).strip():
        return None
    lines = [line for line in str(message).splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        for key in ("cause", "message"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return _one_line(value) or None
    return _one_line(lines[-1]) or None


def cannot_start_error(
    execution: ExecutionView, termination: Any, attempt: AttemptView | None
) -> tuple[str, str]:
    """The task's `last_error` for a worker that could not start, and where it came from.

    The owner's order (2026-09-25): the worker's own cause if it could leave
    one, else a line composed from the exit code that names the execution.

      1. its attempt document, if the worker reached Firestore and recorded
         a 78 there. No 78 path writes it today, since none of them has
         passed the generation check, but a worker that does is the best
         witness there is;
      2. its termination message (GKE): its last structured log line;
      3. "worker exited 78: could not start (see execution logs: <execution>)".
    """
    if (
        attempt is not None
        and attempt.exit_code == WORKER_EXIT_CANNOT_START
        and attempt.error
        and _one_line(attempt.error)
    ):
        return f"worker could not start: {_one_line(attempt.error)}", "attempt"
    cause = worker_cause(getattr(termination, "message", None))
    if cause:
        return f"worker could not start: {cause}", "termination_message"
    where = (
        f"{execution.namespace}/{execution.name}" if execution.namespace else execution.name
    )
    return (
        f"worker exited {WORKER_EXIT_CANNOT_START}: could not start "
        f"(see execution logs: {where})",
        "exit_code",
    )


def detect_cannot_start(
    snapshot: ControlSnapshot,
    executions: Iterable[ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """Attempts whose execution finished with 78: fail the task, now, with the cause.

    OWNER'S DECISION, 2026-09-25. A worker that cannot start exits 78, and 78
    is NON-RETRYABLE at the platform level. Before this rule, nothing read the
    code. A 78 before the first heartbeat waited out the lease's 300 s
    dispatch deadline, and was reclaimed as a silent `stale_lease` and
    requeued. Every attempt then met the same broken DNS or configuration
    until `max_attempts` was spent, and the task failed with "reconciled:
    lease silent ...", its cause only in a container log.

    On EVIDENCE only: the exit code the backend recorded for the finished
    execution, read by `Reconciler._read_terminations` into
    `snapshot.terminations`. An attempt whose termination was not read, or
    could not be, produces nothing here, and today's rules judge its lease as
    they always have. So does every exit code but 78.

    No clock. The execution is over and its code says why; no grace would
    change the answer. The repair (`Reconciler._repair`) is the ordinary
    one, in the ordinary order: fence the generation, confirm nothing runs
    (nothing does: the execution finished), release the lease through the
    frozen `release_lease_in_transaction`, then FAILED, or CANCELLED if a
    cancel was asked for.
    """
    terminations = getattr(snapshot, "terminations", None) or {}
    findings: list[Finding] = []
    for subject in cannot_start_candidates(snapshot, executions):
        attempt_id = subject.lease.attempt_id
        ended = terminations.get(attempt_id)
        if ended is None or getattr(ended, "exit_code", None) != WORKER_EXIT_CANNOT_START:
            continue
        execution = subject.execution
        error, source = cannot_start_error(
            execution, ended, snapshot.attempts.get(attempt_id)
        )
        where = (
            f"{execution.namespace}/{execution.name}" if execution.namespace else execution.name
        )
        findings.append(
            Finding(
                kind=FindingKind.WORKER_CANNOT_START,
                reason=error,
                task_id=subject.task.task_id,
                lease_id=subject.lease.lease_id,
                attempt_id=attempt_id,
                tenant_id=subject.task.tenant_id or execution.tenant_id,
                generation=subject.lease.generation,
                execution=execution,
                detail={
                    "exit_code": WORKER_EXIT_CANNOT_START,
                    "cause_source": source,
                    "execution": where,
                    "backend_detail": str(getattr(ended, "detail", "") or ""),
                    "task_state": subject.task.state.value,
                    "attempt_count": subject.task.attempt_count,
                    "max_attempts": subject.task.max_attempts,
                },
            )
        )
    return findings


#: The task states in which an execution that has ENDED cannot have reached
#: its runner. The worker moves its task DISPATCHED -> STARTING -> RUNNING in
#: one step, before it creates the workspace (`agent_worker.control.
#: advance_to_running`), and a RUNNING task's worker heartbeats, so the
#: heartbeat rules own it.
ENDED_AT_STARTUP_STATES: tuple[TaskState, ...] = (TaskState.DISPATCHED, TaskState.STARTING)


def _as_utc(value: datetime) -> datetime:
    """A backend's time as UTC. A naive one (a protobuf `ToDatetime`) is UTC already."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def detect_ended_at_startup(
    snapshot: ControlSnapshot,
    executions: Iterable[ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """Attempts whose execution ended before their runner started: requeue them, now (#198).

    WHAT HAPPENED. Execution swarm-job-eng-mock-9ngvq exited 69 at 20:37:56Z
    on 2026-09-25, having never reached Firestore. Its task sat DISPATCHED,
    and the CLI truthfully showed DISPATCHED, until the 20:40 pass found the
    lease past its 300 s dispatch deadline and reclaimed it as "lease silent".
    The exit code, already read for the cannot-start rule, said the execution
    was over two minutes earlier, and why.

    THE RULE. The same subjects as `detect_cannot_start` (a FAILED execution
    whose attempt holds its task's current lease at the current generation,
    with nothing of that attempt still active), when:

      * the task is still DISPATCHED or STARTING (`ENDED_AT_STARTUP_STATES`),
        so its worker never reached its runner and wrote no end of its own;
      * the exit code was READ (`snapshot.terminations`), and is not 78,
        which `detect_cannot_start` fails without a retry. A read that found no
        exit code counts as read; a read that failed does not, and today's
        rules judge the lease;
      * the backend recorded when the execution ended, at least
        `ended_execution_grace_seconds` before this pass read Firestore. The
        worker makes every write before its container exits, so the snapshot
        has seen them all (see the setting for why that matters).

    The repair is the ordinary one: fence the generation, confirm nothing
    runs (nothing does), release the lease through the frozen
    `release_lease_in_transaction`, then READY, or FAILED once the task's
    attempts are spent, or CANCELLED if a cancel was asked for. The fence and
    the requeue are each refused, inside their transactions, for a task no
    longer in `ENDED_AT_STARTUP_STATES`.

    Mutually exclusive with `detect_cannot_start` by exit code, and it
    supersedes the absence rules for the same lease in `detect_all`, as that
    rule does.
    """
    terminations = getattr(snapshot, "terminations", None) or {}
    taken = _as_utc(getattr(snapshot, "taken_at", None) or now or utcnow())
    grace = int(getattr(config, "ended_execution_grace_seconds", 30))
    findings: list[Finding] = []
    for subject in cannot_start_candidates(snapshot, executions):
        task, execution = subject.task, subject.execution
        if task.state not in ENDED_AT_STARTUP_STATES:
            continue
        attempt_id = subject.lease.attempt_id
        ended = terminations.get(attempt_id)
        if ended is None:
            continue
        exit_code = getattr(ended, "exit_code", None)
        if exit_code == WORKER_EXIT_CANNOT_START:
            continue
        completed = execution.completed_at
        if completed is None:
            continue
        ended_before = (taken - _as_utc(completed)).total_seconds()
        if ended_before < grace:
            continue  # its worker's last writes may postdate the snapshot
        where = (
            f"{execution.namespace}/{execution.name}" if execution.namespace else execution.name
        )
        short = where if execution.namespace else where.rsplit("/", 1)[-1]
        how = f"exited {exit_code}" if exit_code is not None else "ended with no exit code recorded"
        spent = retries_exhausted(task.attempt_count, task.max_attempts)
        reason = (
            f"execution {short} {how} before its runner started (task was "
            f"{task.state.value}); {'no attempts left' if spent else 'retrying'}"
        )
        findings.append(
            Finding(
                kind=FindingKind.WORKER_ENDED_AT_STARTUP,
                reason=reason,
                task_id=task.task_id,
                lease_id=subject.lease.lease_id,
                attempt_id=attempt_id,
                tenant_id=task.tenant_id or execution.tenant_id,
                generation=subject.lease.generation,
                execution=execution,
                detail={
                    "exit_code": exit_code,
                    "execution": where,
                    "backend_detail": str(getattr(ended, "detail", "") or ""),
                    "task_state": task.state.value,
                    "ended_seconds_before_snapshot": round(ended_before, 1),
                    "attempt_count": task.attempt_count,
                    "max_attempts": task.max_attempts,
                },
            )
        )
    return findings


#: Findings about a lease that a cannot-start or ended-at-startup finding
#: repairs more exactly: each would requeue the task (or release its lease
#: without failing it) on the strength of silence, where the exit code says
#: the execution is over, and for a 78 that the next attempt would fail the
#: same way.
_CANNOT_START_SUPERSEDES = (
    FindingKind.STALE_LEASE,
    FindingKind.MISSING_EXECUTION,
    FindingKind.ORPHAN_LEASE,
)


def detect_unused_job_resources(
    resources: Iterable[JobResourceView],
    active_tenants: set[str],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """Per-tenant Job resources nobody has used in a long time.

    Only ever resources this platform created: the `managed` flag comes from the
    label check, and an unlabelled resource in this shared project belongs to
    another team.
    """
    now = now or utcnow()
    findings: list[Finding] = []
    for resource in resources:
        if not resource.managed:
            continue
        if resource.active_executions:
            continue
        last = resource.last_execution_at or resource.created_at
        if last is None:
            continue
        idle = (now - last).total_seconds()
        if idle < config.unused_job_ttl_seconds:
            continue
        if resource.tenant_id in active_tenants:
            continue
        findings.append(
            Finding(
                kind=FindingKind.UNUSED_JOB_RESOURCE,
                reason=f"job resource idle for {idle / 3600:.1f}h with no active tenant work",
                tenant_id=resource.tenant_id,
                resource=resource,
                detail={"idle_seconds": round(idle, 1), "name": resource.name},
            )
        )
    return findings


def detect_empty_namespaces(
    namespaces: Iterable[JobResourceView],
    active_tenants: set[str],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    now = now or utcnow()
    findings: list[Finding] = []
    for namespace in namespaces:
        if not namespace.managed or namespace.active_executions:
            continue
        if namespace.tenant_id in active_tenants:
            continue
        last = namespace.last_execution_at or namespace.created_at
        if last is None:
            continue
        idle = (now - last).total_seconds()
        if idle < config.empty_namespace_ttl_seconds:
            continue
        findings.append(
            Finding(
                kind=FindingKind.EMPTY_NAMESPACE,
                reason=f"tenant namespace empty for {idle / 3600:.1f}h",
                tenant_id=namespace.tenant_id,
                resource=namespace,
                detail={"idle_seconds": round(idle, 1), "name": namespace.name},
            )
        )
    return findings


#: Findings about a lease that a left-running finding already repairs -- kill
#: the Job, then release -- or that a later judgement owns (`orphan_rule_defers`).
#: Each of these would do a subset of that, or the release without the kill.
_LEFT_RUNNING_SUPERSEDES = (
    FindingKind.ORPHAN_LEASE,
    FindingKind.STALE_LEASE,
    FindingKind.DEAD_WORKER,
)


def detect_all(
    snapshot: ControlSnapshot,
    executions: list[ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
    logger: Any | None = None,
) -> list[Finding]:
    """Every rule, deduplicated by (kind, lease, execution), most urgent first.

    Ordering matters downstream: findings that require termination are repaired
    before ones that only release, so a single pass can never return a slot for
    an execution it has not yet stopped.
    """
    now = now or utcnow()
    executions = normalise_executions(snapshot, executions)
    # Resolve the ids first, then check who owns them: an execution whose task
    # belongs to another tenant must not reach any rule below.
    executions = scope_executions_to_their_tenant(snapshot, executions, logger)
    by_attempt = {
        execution.attempt_id: execution
        for execution in executions
        if execution.attempt_id and execution.is_active
    }
    findings = [
        *detect_orphan_executions(snapshot, executions, config, now),
        *detect_stale_leases(snapshot, by_attempt, config, now),
        *detect_missing_executions(snapshot, by_attempt, config, now),
        *detect_orphan_leases(snapshot, now, by_attempt),
        *detect_left_running(snapshot, executions, config, now),
    ]
    # A lease whose attempt exited 78 is repaired by the cannot-start rule
    # alone: fenced, released and FAILED. The absence rules would requeue the
    # same task on the strength of silence, and in the same pass their READY
    # would race its FAILED. A lease whose attempt ended before its runner
    # started is repaired by the ended-at-startup rule alone, for the same
    # reason: one repair per lease, and the one that says why (#198).
    cannot_start = detect_cannot_start(snapshot, executions, config, now)
    ended_at_startup = detect_ended_at_startup(snapshot, executions, config, now)
    failing = {f.lease_id for f in (*cannot_start, *ended_at_startup) if f.lease_id}
    findings = [
        *(
            f
            for f in findings
            if not (f.kind in _CANNOT_START_SUPERSEDES and f.lease_id in failing)
        ),
        *cannot_start,
        *ended_at_startup,
    ]
    # A left-running repair releases its Job's lease only AFTER the kill is
    # confirmed. The orphan-lease rule would release the same lease without
    # looking for compute at all, so it stands aside for that one lease; and a
    # stale-lease finding over the same Job would terminate and release it a
    # second time under another name, so it stands aside too.
    left = {
        f.lease_id for f in findings if f.kind is FindingKind.LEFT_RUNNING and f.lease_id
    }
    # The same holds BEFORE the left-running rule acts -- inside its grace --
    # and for the other two executions the orphan rule leaves for later: one
    # whose task could not be read, and one whose task was re-admitted
    # mid-pass. The orphan rule stood aside on the promise that a later
    # judgement owns that execution. A dead_worker kill in the same pass would
    # break the promise (and, for a finished task, SIGTERM a worker that may be
    # finishing its own cleanup), so every rule about its lease waits too.
    deferred = {
        execution.attempt_id
        for execution in executions
        if execution.attempt_id and orphan_rule_defers(snapshot, execution, config)
    }
    findings = [
        f
        for f in findings
        if not (
            f.kind in _LEFT_RUNNING_SUPERSEDES
            and (f.lease_id in left or (f.attempt_id is not None and f.attempt_id in deferred))
        )
    ]
    # The stuck rule only ever adds. A lease another rule already acts on --
    # a dead worker, a superseded generation -- is that rule's, and repairing
    # it twice would count one termination twice in the pass report.
    covered = {f.lease_id for f in findings if f.lease_id}
    findings.extend(
        f
        for f in detect_stuck_executions(snapshot, by_attempt, config, now)
        if f.lease_id not in covered
    )
    seen: set[tuple[str, str | None, str | None]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (
            finding.kind.value,
            finding.lease_id,
            finding.execution.name if finding.execution else None,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    unique.sort(key=lambda f: (not f.requires_termination, f.kind.value))
    return unique[: config.max_findings_per_pass]
