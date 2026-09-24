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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

#: The slug character class is IMPORTED, never restated. `sanitised()` below
#: undoes what the dispatcher's `sanitize_name` did, so the two escaping
#: rules are one rule -- a private copy here is the half that would
#: silently stop round-tripping, and the reconciler would then either kill
#: a live execution it read as orphaned or never find a real orphan.
#: docs/audits/2026-09-18/08-frozen-contract-restatements.md, finding 1.
from swarm_common.identity import _TENANT_SAFE as _NAME_SAFE
from swarm_common.models import utcnow
from swarm_common.profiles import Backend
from swarm_common.states import TaskState

from .config import ReconcilerConfig
from .model import ControlSnapshot, ExecutionView, JobResourceView, LeaseView, TaskView

#: The backend the two eviction rules act on, spelled by the frozen contract.
#: `backends.GkeBackend.name` is the same string; importing that module here
#: would be circular (it imports `sanitised` from this one).
GKE = Backend.GKE_AUTOPILOT.value


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


def detect_orphan_executions(
    snapshot: ControlSnapshot,
    executions: Iterable[ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """Compute that is running with no live lease behind it."""
    now = now or utcnow()
    findings: list[Finding] = []
    # Read with defaults, as `logger` below is: this rule is also called with a
    # bare snapshot and config that predate the by-id reads and the eviction
    # rules, and for those it must behave exactly as it did before them.
    settled_tasks: dict[str, TaskView] = getattr(snapshot, "settled", None) or {}
    unreadable_tasks: set[str] = getattr(snapshot, "unreadable_tasks", None) or set()
    evicting = bool(getattr(config, "enable_gke_eviction", False))
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

        if (
            task is None
            and not execution.claim_refused
            and execution.task_id in unreadable_tasks
        ):
            # Its task exists or not, finished or re-admitted -- this pass could
            # not look. Terminating on "I could not read it" is the same
            # mistake as releasing on "I could not list it".
            continue
        if task is None and settled is not None and settled.holds_capacity:
            # Re-admitted between the snapshot and the read by id. Whatever
            # this execution is -- the new attempt, or an old one -- the next
            # pass holds the task, its lease and its generation together.
            continue
        named = task or settled
        # A generation NO admission issued. `admission.py` raises
        # `current_generation` in the same transaction that writes the lease,
        # before any dispatch, and the task was read after the backends were
        # listed -- so an execution claiming more than the task records was not
        # created by this platform's dispatch path. It is not the "newer
        # attempt" the eviction rules protect; that one is never newer than its
        # own task. The obsolete-generation rule already treats such a claim
        # on a running task the same way.
        unissued = (
            named is not None
            and execution.generation is not None
            and execution.generation > named.generation
        )
        if (
            named is not None
            and named.is_terminal
            and not unissued
            and execution.backend == GKE
            and evicting
        ):
            continue  # detect_left_running owns it, grace and all

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


def detect_orphan_leases(snapshot: ControlSnapshot, now: datetime | None = None) -> list[Finding]:
    """Leases held by tasks that are finished, gone, or pointing elsewhere."""
    findings: list[Finding] = []
    for lease in snapshot.leases.values():
        if lease.is_released:
            continue
        # A finished task read by id says what it is; only a task no read found
        # is "no longer exists". Same action either way -- the lease is released.
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
        if reference is None:
            continue
        after = (now - reference).total_seconds()
        if after < config.left_running_grace_seconds:
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
        findings.append(
            Finding(
                kind=FindingKind.LEFT_RUNNING,
                reason=(
                    f"task is {task.state.value} but its GKE job {where} is still active "
                    f"{after:.0f}s after it finished (grace {config.left_running_grace_seconds}s)"
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
                    "active_seconds_after_finish": round(after, 1),
                    "lease_unreleased": lease is not None,
                    "namespace": execution.namespace,
                },
            )
        )
    return findings


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


#: Findings about a lease that a left-running finding already repairs: kill the
#: Job, then release. Each of these would do a subset of that, or the release
#: without the kill.
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
        *detect_orphan_leases(snapshot, now),
        *detect_left_running(snapshot, executions, config, now),
    ]
    # A left-running repair releases its Job's lease only AFTER the kill is
    # confirmed. The orphan-lease rule would release the same lease without
    # looking for compute at all, so it stands aside for that one lease; and a
    # stale-lease finding over the same Job would terminate and release it a
    # second time under another name, so it stands aside too.
    left = {
        f.lease_id for f in findings if f.kind is FindingKind.LEFT_RUNNING and f.lease_id
    }
    findings = [
        f
        for f in findings
        if not (f.kind in _LEFT_RUNNING_SUPERSEDES and f.lease_id in left)
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
