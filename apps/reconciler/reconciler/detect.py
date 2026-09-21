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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

from swarm_common.models import utcnow
from swarm_common.states import TaskState

from .config import ReconcilerConfig
from .model import ControlSnapshot, ExecutionView, JobResourceView


class FindingKind(str, Enum):
    STALE_LEASE = "stale_lease"
    MISSING_EXECUTION = "missing_execution"
    ORPHAN_EXECUTION = "orphan_execution"
    ORPHAN_LEASE = "orphan_lease"
    DEAD_WORKER = "dead_worker"
    OBSOLETE_GENERATION = "obsolete_generation"
    UNUSED_JOB_RESOURCE = "unused_job_resource"
    EMPTY_NAMESPACE = "empty_namespace"


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
        ) or (self.execution is not None and self.execution.is_active)


_NAME_SAFE = re.compile(r"[^a-z0-9-]+")


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
        task = snapshot.tasks.get(execution.task_id or "")
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
        scoped.append(replace(execution, task_id=None, attempt_id=None, generation=None))
    return scoped


def detect_stale_leases(
    snapshot: ControlSnapshot,
    executions_by_attempt: dict[str, ExecutionView],
    config: ReconcilerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    """Leases whose worker has stopped proving it is alive."""
    now = now or utcnow()
    findings: list[Finding] = []
    for lease in snapshot.leases.values():
        if lease.is_released:
            continue
        task = snapshot.tasks.get(lease.task_id)
        silent = lease.silent_seconds(now)
        expired = lease.expires_at is not None and now > lease.expires_at
        overdue_dispatch = (
            lease.state is TaskState.LEASED
            and lease.dispatch_deadline is not None
            and now > lease.dispatch_deadline
        )
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
    for execution in executions:
        if not execution.is_active:
            continue
        age = (now - execution.created_at).total_seconds() if execution.created_at else None
        if age is not None and age < config.orphan_execution_grace_seconds:
            continue  # the dispatcher may not have written the lease yet

        task = snapshot.tasks.get(execution.task_id) if execution.task_id else None
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
        if execution.task_id is None:
            log = getattr(config, "logger", None)
            if log is not None:
                log.info(
                    "ignoring an execution that carries no task id; not a worker execution",
                    execution=getattr(execution, "name", ""),
                    parent=getattr(execution, "parent", ""),
                )
            continue

        if task is None:
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
        task = snapshot.tasks.get(lease.task_id)
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
    ]
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
