"""Repair, in the only order that cannot cause duplicate execution.

    1. INVALIDATE the generation      -- the running worker now fences itself
    2. TERMINATE the execution        -- and confirm the backend accepted it
    3. RELEASE the slot               -- only now, and only if 2 succeeded
    4. REPAIR the task state          -- back to READY, or FAILED if spent

Step 3 after step 2 is the whole point of this module. Releasing first returns a
slot to the scheduler, which admits the next task in milliseconds, while the
agent whose slot it was is still running: two agents, one task, one credential,
one repository. So a termination that is not confirmed does not release. The
cost of getting that wrong in the safe direction is one stuck slot until the
next pass; the cost of getting it wrong in the other direction is silent data
loss in a tenant's repository.

Step 1 before step 2 matters for the same reason from the other side. Killing an
execution can fail, hang, or race a container that is already restarting;
bumping the generation is a single Firestore write that makes the old worker
stop by itself at its next poll -- a second, independent brake that does not
depend on the backend being reachable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from swarm_common.models import utcnow
from swarm_common.states import EventType, TaskState

from .backends import Backend
from .config import ReconcilerConfig
from .detect import Finding, FindingKind, detect_all, detect_empty_namespaces, detect_unused_job_resources
from .model import ControlSnapshot, ExecutionView, JobResourceView
from .store import ControlStore


@dataclass
class RepairOutcome:
    kind: str
    reason: str
    tenant_id: str | None = None
    task_id: str | None = None
    lease_id: str | None = None
    execution: str | None = None
    invalidated_to: int | None = None
    terminated: bool = False
    released: bool = False
    repaired_to: str | None = None
    deleted: str | None = None
    skipped: str | None = None
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "lease_id": self.lease_id,
            "execution": self.execution,
            "invalidated_to": self.invalidated_to,
            "terminated": self.terminated,
            "released": self.released,
            "repaired_to": self.repaired_to,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "actions": self.actions,
        }


@dataclass
class ReconcileReport:
    started_at: datetime
    finished_at: datetime | None = None
    tasks_examined: int = 0
    leases_examined: int = 0
    executions_examined: int = 0
    findings: int = 0
    outcomes: list[RepairOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": (
                round((self.finished_at - self.started_at).total_seconds(), 3)
                if self.finished_at
                else None
            ),
            "dry_run": self.dry_run,
            "tasks_examined": self.tasks_examined,
            "leases_examined": self.leases_examined,
            "executions_examined": self.executions_examined,
            "findings": self.findings,
            "slots_released": sum(1 for o in self.outcomes if o.released),
            "executions_terminated": sum(1 for o in self.outcomes if o.terminated),
            "resources_deleted": sum(1 for o in self.outcomes if o.deleted),
            "skipped": sum(1 for o in self.outcomes if o.skipped),
            "outcomes": [o.as_dict() for o in self.outcomes],
            "errors": self.errors,
        }


class Reconciler:
    def __init__(
        self,
        *,
        store: ControlStore,
        backends: list[Backend],
        config: ReconcilerConfig,
        logger: Any,
    ) -> None:
        self._store = store
        self._backends = backends
        self._config = config
        self._log = logger

    # ------------------------------------------------------------------
    def run_once(self) -> ReconcileReport:
        report = ReconcileReport(started_at=utcnow(), dry_run=self._config.dry_run)
        snapshot = self._store.snapshot()
        report.tasks_examined = len(snapshot.tasks)
        report.leases_examined = len(snapshot.leases)

        executions: list[ExecutionView] = []
        backend_by_name: dict[str, Backend] = {}
        for backend in self._backends:
            backend_by_name[backend.name] = backend
            try:
                found = backend.list_executions()
            except Exception as exc:
                # A backend we cannot see is a backend we must not act on: with
                # no execution list, every task there looks like a missing
                # execution, and repairing those would kill healthy work.
                message = f"{backend.name}: listing executions failed: {exc}"
                self._log.error("backend unavailable; skipping its findings", error=message)
                report.errors.append(message)
                backend_by_name.pop(backend.name, None)
                continue
            executions.extend(found)
        report.executions_examined = len(executions)

        findings = detect_all(
            snapshot, executions, self._config, now=snapshot.taken_at, logger=self._log
        )
        findings = [f for f in findings if self._is_actionable(f, snapshot, backend_by_name)]
        report.findings = len(findings)

        for finding in findings:
            try:
                report.outcomes.append(self._repair(finding, snapshot, backend_by_name))
            except Exception as exc:
                message = f"{finding.kind.value} {finding.task_id or finding.lease_id}: {exc}"
                self._log.exception("repair failed", exc, finding=finding.kind.value)
                report.errors.append(message)

        if self._config.enable_gc:
            try:
                report.outcomes.extend(self._collect_garbage(snapshot, backend_by_name))
            except Exception as exc:
                self._log.exception("garbage collection failed", exc)
                report.errors.append(f"gc: {exc}")

        report.finished_at = utcnow()
        self._log.info("reconciliation pass complete", **{
            k: v for k, v in report.as_dict().items() if k != "outcomes"
        })
        return report

    #: Findings that mean "nothing seems to be running" -- and are therefore
    #: only trustworthy if every backend that COULD be running it was readable.
    _ABSENCE_KINDS = (
        FindingKind.STALE_LEASE,
        FindingKind.DEAD_WORKER,
        FindingKind.MISSING_EXECUTION,
    )

    def _is_actionable(
        self,
        finding: Finding,
        snapshot: ControlSnapshot,
        backends: dict[str, Backend],
    ) -> bool:
        """Drop findings that depend on a backend this pass could not read.

        An unreadable backend makes every task on it look abandoned. Repairing
        on that basis would release slots for agents that are alive and well,
        and the scheduler would immediately start a second copy of each -- the
        failure mode this whole service exists to prevent, caused by the service
        itself. So: no execution list, no conclusions about absence.
        """
        if finding.execution is not None:
            return finding.execution.backend in backends

        if finding.kind in self._ABSENCE_KINDS or (
            finding.kind is FindingKind.ORPHAN_LEASE
            and (task := snapshot.tasks.get(finding.task_id or "")) is not None
            and task.holds_capacity
        ):
            attempt = snapshot.attempts.get(finding.attempt_id or "")
            backend_name = attempt.backend if attempt else None
            if backend_name is not None:
                readable = backend_name in backends
            else:
                readable = len(backends) == len(self._backends)
            if not readable:
                self._log.warning(
                    "not repairing: the backend that would hold this execution was unreadable",
                    task_id=finding.task_id,
                    lease_id=finding.lease_id,
                    kind=finding.kind.value,
                    backend=backend_name,
                )
                return False
        return True

    # ------------------------------------------------------------------
    def _repair(
        self,
        finding: Finding,
        snapshot: ControlSnapshot,
        backends: dict[str, Backend],
    ) -> RepairOutcome:
        outcome = RepairOutcome(
            kind=finding.kind.value,
            reason=finding.reason,
            tenant_id=finding.tenant_id,
            task_id=finding.task_id,
            lease_id=finding.lease_id,
            execution=finding.execution.name if finding.execution else None,
        )
        if self._config.dry_run:
            outcome.skipped = "dry_run"
            outcome.actions.append("would invalidate, terminate, release and repair")
            return outcome

        # ---- STEP 1: invalidate the generation --------------------------
        if finding.task_id and finding.generation is not None:
            new_generation = self._store.invalidate_generation(
                finding.task_id, finding.generation
            )
            outcome.invalidated_to = new_generation
            if new_generation is not None:
                outcome.actions.append(f"generation {finding.generation} -> {new_generation}")
                self._store.emit(
                    task_id=finding.task_id,
                    tenant_id=finding.tenant_id,
                    event_type=EventType.GENERATION_FENCED,
                    detail={
                        "reason": finding.reason,
                        "finding": finding.kind.value,
                        "invalidated_generation": finding.generation,
                        "new_generation": new_generation,
                    },
                    attempt_id=finding.attempt_id,
                    lease_id=finding.lease_id,
                    generation=finding.generation,
                )

        # ---- STEP 2: terminate the execution ----------------------------
        termination_required = finding.execution is not None and finding.execution.is_active
        if termination_required:
            backend = backends.get(finding.execution.backend)
            if backend is None:
                outcome.skipped = "backend_unavailable"
                outcome.actions.append("did NOT release: backend unavailable to confirm the kill")
                return outcome
            try:
                outcome.terminated = bool(backend.terminate(finding.execution))
            except Exception as exc:
                self._log.error(
                    "termination failed; NOT releasing the slot",
                    execution=finding.execution.name,
                    error=str(exc),
                )
                outcome.skipped = f"termination_failed: {exc}"
                outcome.actions.append("did NOT release: termination was not confirmed")
                return outcome
            if not outcome.terminated:
                outcome.skipped = "termination_unconfirmed"
                outcome.actions.append("did NOT release: termination was not confirmed")
                return outcome
            outcome.actions.append(f"terminated {finding.execution.name}")

        # ---- STEP 3: release the slot -----------------------------------
        if finding.lease_id:
            outcome.released = self._store.release_lease(
                finding.lease_id, f"reconciler:{finding.kind.value}"
            )
            if outcome.released:
                outcome.actions.append(f"released {finding.lease_id}")
                if finding.task_id:
                    self._store.emit(
                        task_id=finding.task_id,
                        tenant_id=finding.tenant_id,
                        event_type=EventType.LEASE_RELEASED,
                        detail={"reason": finding.kind.value, "detail": finding.reason},
                        attempt_id=finding.attempt_id,
                        lease_id=finding.lease_id,
                    )

        # ---- STEP 4: repair the task state ------------------------------
        if finding.task_id:
            task = snapshot.tasks.get(finding.task_id)
            if task is not None and not task.is_terminal:
                repaired = self._store.repair_task_state(
                    finding.task_id,
                    to_state=TaskState.READY,
                    error=f"reconciled: {finding.reason}",
                    next_eligible_at=utcnow(),
                )
                if repaired is not None:
                    outcome.repaired_to = repaired.value
                    outcome.actions.append(f"task -> {repaired.value}")
                    self._store.emit(
                        task_id=finding.task_id,
                        tenant_id=finding.tenant_id,
                        event_type=(
                            EventType.READY if repaired is TaskState.READY else EventType.FAILED
                        ),
                        detail={"reason": finding.kind.value, "detail": finding.reason},
                        attempt_id=finding.attempt_id,
                        lease_id=finding.lease_id,
                    )
        return outcome

    # ------------------------------------------------------------------
    def _collect_garbage(
        self, snapshot: ControlSnapshot, backends: dict[str, Backend]
    ) -> list[RepairOutcome]:
        """Remove per-tenant infrastructure nothing is using any more.

        Cloud Run pins the service account on the Job resource, so the
        dispatcher creates one per (tenant, profile) and they accumulate. This
        is where they stop accumulating -- and it only ever removes resources
        carrying this platform's own label.
        """
        outcomes: list[RepairOutcome] = []
        active = self._store.active_tenants(snapshot)
        # A namespace is protected by mere registration; a Job resource is not.
        # The difference is recoverability: `ensure_job` recreates a deleted Job
        # on the next dispatch, whereas nothing recreates a namespace, its
        # service account or its workload-identity binding.
        protected_namespaces = active | self._store.registered_tenants()
        now = snapshot.taken_at
        for backend in backends.values():
            resources = backend.list_job_resources()
            if backend.name == "GKE_AUTOPILOT":
                findings = detect_empty_namespaces(
                    resources, protected_namespaces, self._config, now
                )
            else:
                findings = detect_unused_job_resources(resources, active, self._config, now)
            for finding in findings:
                resource: JobResourceView = finding.resource  # type: ignore[assignment]
                outcome = RepairOutcome(
                    kind=finding.kind.value,
                    reason=finding.reason,
                    tenant_id=finding.tenant_id,
                )
                if self._config.dry_run:
                    outcome.skipped = "dry_run"
                    outcome.actions.append(f"would delete {resource.name}")
                    outcomes.append(outcome)
                    continue
                try:
                    deleted = backend.delete_job_resource(resource)
                except PermissionError as exc:
                    outcome.skipped = str(exc)
                    self._log.warning("refused to delete an unmanaged resource", error=str(exc))
                    outcomes.append(outcome)
                    continue
                if deleted:
                    outcome.deleted = resource.name
                    outcome.actions.append(f"deleted {resource.name}")
                else:
                    outcome.skipped = "not_empty"
                outcomes.append(outcome)
        return outcomes
