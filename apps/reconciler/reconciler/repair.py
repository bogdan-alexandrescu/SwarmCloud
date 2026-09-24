"""Repair, in the only order that cannot cause duplicate execution.

    1. INVALIDATE the generation      -- the running worker now fences itself
    2. TERMINATE the execution        -- and confirm the backend accepted it
    3. RELEASE the slot               -- only now, and only if 2 succeeded
    4. REPAIR the task state          -- back to READY, or FAILED if spent,
                                         or CANCELLED if a cancel was requested

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

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from swarm_common.models import utcnow
from swarm_common.states import EventType, TaskState

from .backends import Backend, NamespacedBackend, NamespacedListing, Probe, ProbeOutcome
from .checkpoints import CheckpointCollector, CheckpointStore
from .config import ReconcilerConfig
from .detect import (
    Finding,
    FindingKind,
    detect_all,
    detect_empty_namespaces,
    detect_orphan_executions,
    detect_stale_leases,
    detect_unused_job_resources,
    normalise_executions,
    sanitised,
    stuck_candidates,
)
from .model import AttemptView, ControlSnapshot, ExecutionView, JobResourceView, LeaseView
from .progress import assess
from .store import ControlStore

#: The two lines the reconciler writes when it holds back, spelled ONCE.
#:
#: terraform/modules/monitoring/alerts.tf builds log-based metrics on these exact
#: strings, and a log-based metric whose filter matches nothing is not an error
#: anywhere -- it is a number that stays at zero and an alert that never fires.
#: `test_reconciler_gke_namespaced.py` parses that file and asserts each appears
#: verbatim in the `filter` of the metric that counts it -- not merely somewhere
#: in the file, where a comment quoting it would do -- so rewording one here
#: without the other fails a test instead of an alert.
#:
#: Why they are alerted on at all: from 2026-09-16 the GKE listing failed on
#: essentially every pass, and nothing paged. On 2026-09-24 that blindness held
#: five dead leases for hours, and the only record was a WARNING per lease per
#: pass that nobody was watching.
BACKEND_UNAVAILABLE = "backend unavailable; skipping its findings"
NOT_REPAIRING = "not repairing: the backend that would hold this execution was unreadable"

#: The line written once per eviction -- a stuck attempt fenced, or a Job left
#: running terminated -- so an operator can find every one with a single
#: filter (docs/runbooks/browser-eviction.md). The persisted pass carries the
#: same outcome.
EVICTED = "evicted a GKE job"

#: Findings repaired by terminating a Job and never by fencing it.
_TERMINATE_ONLY = (FindingKind.LEFT_RUNNING,)
#: Findings repaired by fencing alone in the pass that finds them. See
#: `_repair_stuck` for why the kill waits for the next pass.
_FENCE_ONLY = (FindingKind.STUCK_NO_PROGRESS,)

#: What happens after a stuck attempt is fenced, written onto its event so the
#: task's timeline says what to expect next.
_AFTER_THE_FENCE = (
    "the worker stops the agent at its next control poll and exits without "
    "touching the lease or the task; a later pass releases the lease once the "
    "Job has ended, terminating it first if it has not, and requeues or fails "
    "the task by the retry rules"
)

#: The event each repaired state is announced with.
_EVENT_FOR_REPAIR: dict[TaskState, EventType] = {
    TaskState.READY: EventType.READY,
    TaskState.FAILED: EventType.FAILED,
    TaskState.CANCELLED: EventType.CANCELLED,
}


class _Disproved:
    """`_admit`'s answer when a probe proved a finding WRONG.

    A third answer beside "act on this" and "hold it back", and it has to be
    distinct from both. Acting is what killed healthy agents: a missing_execution
    raised only because a LIST failed was turned into a dead_worker by an active
    Job found by name. Holding it back is wrong too -- it would be counted in
    `findings_suppressed` and logged as NOT_REPAIRING, which pages after thirty
    minutes, for an agent that is doing exactly what it should.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "DISPROVED"


_DISPROVED = _Disproved()


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


@dataclass(frozen=True)
class SuppressedFinding:
    """A finding this pass DETECTED and then declined to act on.

    Kept apart from `findings` because the two numbers mean opposite things.
    `findings` counts repairs attempted; this counts leases the reconciler knows
    about and is deliberately leaving alone because it cannot see the backend
    that would prove them dead. For six days both were folded into one number
    that read `findings=0` -- a healthy pass and a blind one looked identical,
    in the log line and in the persisted pass history alike.
    """

    kind: str
    lease_id: str | None
    task_id: str | None
    backend: str | None
    namespace: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "backend": self.backend,
            "namespace": self.namespace,
        }


@dataclass
class ReconcileReport:
    started_at: datetime
    finished_at: datetime | None = None
    tasks_examined: int = 0
    leases_examined: int = 0
    executions_examined: int = 0
    findings: int = 0
    #: Detected, then held back because what would prove them was unreadable.
    #: See SuppressedFinding. Zero on a healthy pass; anything else is a lease
    #: the platform is holding on purpose and somebody should know about.
    findings_suppressed: int = 0
    suppressed: list[SuppressedFinding] = field(default_factory=list)
    #: backend -> namespace -> why it could not be read. A namespace here that
    #: holds none of our attempts is noise (a registered tenant whose namespace
    #: was never provisioned); one that does also appears in `errors`.
    unreadable_namespaces: dict[str, dict[str, str]] = field(default_factory=dict)
    outcomes: list[RepairOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    #: The checkpoint sweep's own counters. None when it did not run on this
    #: pass -- which is the usual case, because it runs on its own slower clock.
    #: None and a zero-filled report mean different things and are reported as
    #: different things.
    checkpoint_sweep: dict[str, Any] | None = None

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
            "findings_suppressed": self.findings_suppressed,
            "suppressed": [s.as_dict() for s in self.suppressed],
            "unreadable_namespaces": {
                backend: dict(namespaces)
                for backend, namespaces in self.unreadable_namespaces.items()
            },
            "slots_released": sum(1 for o in self.outcomes if o.released),
            "executions_terminated": sum(1 for o in self.outcomes if o.terminated),
            "resources_deleted": sum(1 for o in self.outcomes if o.deleted),
            "skipped": sum(1 for o in self.outcomes if o.skipped),
            "checkpoint_sweep": self.checkpoint_sweep,
            "outcomes": [o.as_dict() for o in self.outcomes],
            "errors": self.errors,
        }


@dataclass
class _Sight:
    """What one pass could see, kept apart from what it may act through.

    `handles` is every configured backend, readable or not. It is what a
    termination is issued through, and it must NOT shrink when a listing fails:
    a probe by name can find a live Job on a backend whose list was refused,
    and the kill that must precede the release has to be sent somewhere. These
    used to be one dict with the unreadable backends popped out, so a backend
    that could not be listed could not be told to stop anything either.
    """

    handles: dict[str, Any] = field(default_factory=dict)
    #: Backends whose listing succeeded -- for a namespaced backend, for at
    #: least one namespace.
    readable: set[str] = field(default_factory=set)
    #: backend name -> its per-namespace listing, for namespaced backends only.
    namespaced: dict[str, NamespacedListing] = field(default_factory=dict)
    #: tenant id -> the namespace its document records (None if it records none).
    tenant_namespaces: dict[str, str | None] = field(default_factory=dict)
    executions: list[ExecutionView] = field(default_factory=list)
    #: "<backend>:<execution_name>" -> the probe answer, so the two findings a
    #: stranded lease produces (stale_lease, missing_execution) cost one GET.
    probes: dict[str, Probe] = field(default_factory=dict)


def _is_namespaced(backend: Any) -> bool:
    return callable(getattr(backend, "list_executions_in", None))


class Reconciler:
    def __init__(
        self,
        *,
        store: ControlStore,
        backends: list[Backend | NamespacedBackend],
        config: ReconcilerConfig,
        logger: Any,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self._store = store
        self._backends = backends
        self._config = config
        self._log = logger
        # None disables the sweep outright. An environment with no artifact
        # bucket configured gets no collector rather than one that deletes
        # nothing quietly while claiming to have run.
        self._checkpoints = (
            CheckpointCollector(
                reader=store, objects=checkpoint_store, config=config, logger=logger
            )
            if checkpoint_store is not None
            else None
        )

    # ------------------------------------------------------------------
    def run_once(self) -> ReconcileReport:
        report = ReconcileReport(started_at=utcnow(), dry_run=self._config.dry_run)
        snapshot = self._store.snapshot()
        report.tasks_examined = len(snapshot.tasks)
        report.leases_examined = len(snapshot.leases)

        sight = self._look(snapshot, report)
        report.executions_examined = len(sight.executions)
        # Both AFTER the listing, and in this order: the settled read decides
        # whose task an execution is (and so its tenant), and the progress read
        # is only for executions whose task is known.
        self._read_settled(snapshot, sight.executions, report)
        self._read_progress(snapshot, sight.executions, report)

        findings = detect_all(
            snapshot, sight.executions, self._config, now=snapshot.taken_at, logger=self._log
        )
        actionable: list[Finding] = []
        seen: set[tuple[str, str | None, str | None]] = set()
        for finding in findings:
            admitted = self._admit(finding, snapshot, sight)
            if isinstance(admitted, _Disproved):
                # Neither repaired nor held back: nothing is wrong. See _Disproved.
                continue
            if admitted is None:
                report.suppressed.append(self._suppressed(finding, snapshot, sight))
                continue
            # A probe can turn a stale_lease and a missing_execution about the
            # same lease into the same dead_worker; act on it once.
            key = (
                admitted.kind.value,
                admitted.lease_id,
                admitted.execution.name if admitted.execution else None,
            )
            if key in seen:
                continue
            seen.add(key)
            actionable.append(admitted)
        # Re-sorted because a probe may have turned an absence into a
        # dead_worker: anything that needs a kill goes first, so a pass never
        # returns a slot for an execution it has not stopped yet.
        actionable.sort(key=lambda f: (not f.requires_termination, f.kind.value))
        report.findings = len(actionable)
        report.findings_suppressed = len(report.suppressed)

        # Leases whose execution this pass tried to stop and could not. The sort
        # above runs every kill before any repair that only releases, so a
        # lease-only finding about one of these would hand back a slot whose
        # execution is still running -- the one thing the repair order exists
        # to prevent. `detect_orphan_leases` already stands aside for an
        # execution it can SEE in the listing; this covers the one it cannot:
        # a Job found by name after its namespace's list failed.
        unstopped: set[str] = set()
        for finding in actionable:
            kills = finding.execution is not None and finding.execution.is_active
            if finding.lease_id in unstopped and not kills:
                report.outcomes.append(self._held_for_unstopped(finding))
                continue
            try:
                outcome = self._repair(finding, snapshot, sight.handles)
            except Exception as exc:
                message = f"{finding.kind.value} {finding.task_id or finding.lease_id}: {exc}"
                self._log.exception("repair failed", exc, finding=finding.kind.value)
                report.errors.append(message)
                if kills and finding.lease_id:
                    unstopped.add(finding.lease_id)
                continue
            report.outcomes.append(outcome)
            if kills and finding.lease_id and not outcome.terminated and not self._config.dry_run:
                unstopped.add(finding.lease_id)

        if self._config.enable_gc:
            self._collect_garbage(snapshot, sight, report)

        try:
            self._collect_checkpoints(report, now=snapshot.taken_at)
        except Exception as exc:
            # Storage cleanup never fails a pass that has already terminated
            # executions and released leases: that work happened, and reporting
            # the pass as failed would send a retry over state that is now
            # correct. The error is on the report, not swallowed.
            self._log.exception("checkpoint collection failed", exc)
            report.errors.append(f"checkpoint_gc: {exc}")

        report.finished_at = utcnow()
        self._log.info("reconciliation pass complete", **{
            k: v for k, v in report.as_dict().items() if k != "outcomes"
        })

        # Persist the pass. Until this existed the report lived only in a
        # per-instance dict, and swarm-reconciler runs with min_instance_count =
        # 0 -- so the platform's entire account of what went wrong at runtime
        # was discarded whenever the service scaled down.
        #
        # Deliberately NOT fatal. A pass that has already terminated executions
        # and released leases has done real work; failing it here would report
        # that work as not having happened, and a caller retrying would
        # re-examine state that is now correct. The write failing is worth
        # knowing about, which is why it becomes an error on the report rather
        # than a silent pass.
        #
        # A DRY RUN WRITES NOTHING, including this. "dry run changes nothing" is
        # a guarantee worth keeping absolute: the moment it means "changes
        # nothing except some bookkeeping", nobody can use it to answer "is it
        # safe to run this against prod".
        try:
            if not self._config.dry_run:
                self._store.record_pass(
                    report.as_dict(), retain_hours=self._config.pass_retention_hours
                )
        except Exception as exc:
            self._log.exception("could not persist the reconciliation pass", exc)
            report.errors.append(f"record_pass: {exc}")

        return report

    # ------------------------------------------------------------------
    # Seeing
    # ------------------------------------------------------------------
    def _look(self, snapshot: ControlSnapshot, report: ReconcileReport) -> _Sight:
        """List every backend, recording what could and could not be read."""
        sight = _Sight()
        tenants_read = False
        for backend in self._backends:
            sight.handles[backend.name] = backend
            try:
                if _is_namespaced(backend):
                    if not tenants_read:
                        sight.tenant_namespaces = self._tenant_namespaces(report)
                        tenants_read = True
                    found = self._look_namespaced(backend, snapshot, sight, report)
                else:
                    found = backend.list_executions()
            except Exception as exc:
                # A backend we cannot see is a backend we must not act on: with
                # no execution list, every task there looks like a missing
                # execution, and repairing those would kill healthy work.
                message = f"{backend.name}: listing executions failed: {exc}"
                self._log.error(BACKEND_UNAVAILABLE, backend=backend.name, error=message)
                report.errors.append(message)
                continue
            sight.readable.add(backend.name)
            sight.executions.extend(found)
        return sight

    def _look_namespaced(
        self,
        backend: Any,
        snapshot: ControlSnapshot,
        sight: _Sight,
        report: ReconcileReport,
    ) -> list[ExecutionView]:
        """Read a namespaced backend in exactly the namespaces the control plane names.

        Raises when EVERY namespace failed, which `_look` reports as the whole
        backend being unavailable -- the one case the backend-unavailable alert
        is for. Anything less is per-namespace: logged, kept on the report, and
        an error only where one of our live attempts is actually in it.
        """
        held: set[str] = set()
        for attempt in snapshot.attempts.values():
            if attempt.backend != backend.name:
                continue
            namespace = self._attempt_namespace(backend, attempt, sight.tenant_namespaces)
            if namespace:
                held.add(namespace)
        wanted = {
            backend.namespace_for(tenant_id, recorded)
            for tenant_id, recorded in sight.tenant_namespaces.items()
        } | held

        listing: NamespacedListing = backend.list_executions_in(wanted)
        sight.namespaced[backend.name] = listing
        if listing.unreadable:
            report.unreadable_namespaces[backend.name] = dict(listing.unreadable)
        if listing.blind:
            raise RuntimeError(
                f"every namespace unreadable ({len(listing.unreadable)}): "
                + "; ".join(f"{ns}: {err}" for ns, err in sorted(listing.unreadable.items()))
            )
        for namespace, error in sorted(listing.unreadable.items()):
            holds_attempt = namespace in held
            self._log.warning(
                "namespace unreadable; findings that depend on it are held",
                backend=backend.name,
                namespace=namespace,
                error=error,
                holds_attempt=holds_attempt,
            )
            if holds_attempt:
                report.errors.append(f"{backend.name}: namespace {namespace} unreadable: {error}")
        return list(listing.executions)

    def _tenant_namespaces(self, report: ReconcileReport) -> dict[str, str | None]:
        """Registered tenants and their recorded namespaces, or {} on failure.

        A failed read here narrows what is listed to the namespaces the live
        attempts name, which is every namespace a finding can be about -- so it
        costs orphan detection in idle namespaces and nothing that releases.
        """
        try:
            return self._store.tenant_namespaces()
        except Exception as exc:
            self._log.exception("could not read the registered tenants", exc)
            report.errors.append(f"tenants: {exc}")
            return {}

    def _read_settled(
        self,
        snapshot: ControlSnapshot,
        executions: list[ExecutionView],
        report: ReconcileReport,
    ) -> None:
        """Read, by id, every task an ACTIVE execution names that the snapshot lacks.

        `snapshot()` reads only the four concurrency states, so a task that
        finished -- or was re-queued, or parked -- is simply absent from it. An
        execution still running for such a task used to be judged as naming "no
        task this control plane knows about", which cannot tell apart the cases
        that matter here: a finished task whose Job was left running, a
        re-queued task whose OLD Job is still going, and a task re-admitted
        between the snapshot and now, whose Job may be its NEW attempt. One
        `get` each tells them apart.

        Bounded by the executions that need it: ordinarily none at all.

        Only with `enable_gke_eviction`. This read is the eviction rules' input,
        and the three stand-asides in `detect.orphan_rule_defers` exist only
        because of it -- so turning eviction off turns it off too, and the pass
        judges these executions exactly as it did before the rules existed: as
        orphans, killed first and only then released. A kill switch that left
        the read running would leave its failure modes running with it.
        """
        if not self._config.enable_gke_eviction:
            return
        wanted = sorted(
            {
                execution.task_id
                for execution in normalise_executions(snapshot, executions)
                if execution.is_active
                and execution.task_id
                and execution.task_id not in snapshot.tasks
            }
        )
        for task_id in wanted:
            try:
                task = self._store.task_by_id(task_id)
            except Exception as exc:
                snapshot.unreadable_tasks.add(task_id)
                self._log.warning(
                    "could not read the task an active execution names; "
                    "nothing is concluded about that execution or its lease this pass",
                    task_id=task_id,
                    error=str(exc),
                )
                report.errors.append(f"task {task_id}: {exc}")
                continue
            if task is not None:
                snapshot.settled[task_id] = task

    def _read_progress(
        self,
        snapshot: ControlSnapshot,
        executions: list[ExecutionView],
        report: ReconcileReport,
    ) -> None:
        """Read the progress evidence of every attempt the stuck rule could act on.

        Only attempts `stuck_candidates` names: running on GKE, on the current
        generation, with a heartbeating lease, and running longer than the
        threshold. A read that fails leaves that attempt unassessed, which the
        rule treats as "not judged" -- never as "no progress".
        """
        now = snapshot.taken_at
        for subject in stuck_candidates(snapshot, executions, self._config, now):
            attempt_id = subject.lease.attempt_id
            try:
                events = self._store.attempt_events(subject.task.task_id, attempt_id)
            except Exception as exc:
                self._log.warning(
                    "could not read an attempt's progress; not judging it this pass",
                    task_id=subject.task.task_id,
                    attempt_id=attempt_id,
                    error=str(exc),
                )
                report.errors.append(f"progress {attempt_id}: {exc}")
                continue
            verdict = assess(
                events,
                attempt_id=attempt_id,
                generation=subject.lease.generation,
                started_at=subject.started_at,
                now=now,
                stuck_after_seconds=self._config.stuck_after_seconds,
                cpu_floor_cores=self._config.stuck_cpu_floor_cores,
                max_gap_seconds=self._config.stuck_evidence_max_gap_seconds,
            )
            snapshot.progress[attempt_id] = verdict
            if not verdict.judged:
                # Worth a line: an attempt this old that cannot be judged is
                # one the rule is blind to, and "why" is the whole answer.
                self._log.info(
                    "not judging an attempt's progress: the evidence is incomplete",
                    task_id=subject.task.task_id,
                    attempt_id=attempt_id,
                    because=verdict.unjudged_because,
                )

    @staticmethod
    def _attempt_namespace(
        backend: Any, attempt: AttemptView, tenants: dict[str, str | None]
    ) -> str | None:
        """Where this attempt's execution is, or would be, on a namespaced backend.

        Its own `execution_name` first -- that is where the dispatcher actually
        created it. An attempt not dispatched yet has none, and falls back to the
        tenant's namespace by the dispatcher's own rule.
        """
        recorded = backend.namespace_of(attempt.execution_name)
        if recorded:
            return recorded
        if attempt.tenant_id:
            return backend.namespace_for(attempt.tenant_id, tenants.get(attempt.tenant_id))
        return None

    #: Findings that mean "nothing seems to be running" -- and are therefore
    #: only trustworthy if every backend that COULD be running it was readable.
    _ABSENCE_KINDS = (
        FindingKind.STALE_LEASE,
        FindingKind.DEAD_WORKER,
        FindingKind.MISSING_EXECUTION,
    )

    def _never_dispatched(self, finding: Finding, snapshot: ControlSnapshot) -> bool:
        """True when this task provably never reached a backend.

        The lease exists and the task is still LEASED -- it never advanced to
        DISPATCHED -- and no attempt for it records an execution. Dispatch is
        what creates an execution, so nothing can be running.
        """
        task = snapshot.tasks.get(finding.task_id or "")
        if task is None or task.state is not TaskState.LEASED:
            return False
        for attempt in snapshot.attempts.values():
            if attempt.task_id == finding.task_id and getattr(attempt, "execution_name", None):
                return False
        return True

    def _depends_on_absence(self, finding: Finding, snapshot: ControlSnapshot) -> bool:
        return finding.kind in self._ABSENCE_KINDS or (
            finding.kind is FindingKind.ORPHAN_LEASE
            and (task := snapshot.tasks.get(finding.task_id or "")) is not None
            and task.holds_capacity
        )

    def _is_actionable(
        self,
        finding: Finding,
        snapshot: ControlSnapshot,
        sight: _Sight,
    ) -> bool:
        """Drop findings that depend on something this pass could not read.

        An unreadable backend makes every task on it look abandoned. Repairing
        on that basis would release slots for agents that are alive and well,
        and the scheduler would immediately start a second copy of each -- the
        failure mode this whole service exists to prevent, caused by the service
        itself. So: no execution list, no conclusions about absence.

        "Could not read" is judged where the attempt LIVES. On a namespaced
        backend that is the attempt's own namespace, not the backend as a
        whole: another tenant's namespace answering 403 says nothing about
        this one, and this one answering 403 is not cured by another being
        fine.
        """
        if finding.execution is not None:
            return finding.execution.backend in sight.readable

        if not self._depends_on_absence(finding, snapshot):
            return True
        attempt = snapshot.attempts.get(finding.attempt_id or "")
        backend_name = attempt.backend if attempt else None
        if attempt is not None and backend_name is not None:
            return self._attempt_visible(attempt, sight)
        if self._never_dispatched(finding, snapshot):
            # No attempt names a backend AND the task never left LEASED, so
            # no execution was ever created and no backend can be running
            # it. Refusing to repair here cannot prevent a duplicate -- there
            # is nothing to duplicate -- it only strands the lease for ever.
            #
            # This mattered in practice: an unreachable GKE backend held
            # every never-dispatched lease hostage, including tasks destined
            # for Cloud Run Jobs that GKE had no part in. The task sat LEASED
            # while the drain, which scans only READY, never looked at it
            # again.
            return True
        # An attempt exists but names no backend we can read. Something may
        # genuinely be running, so the anti-duplicate rule holds everywhere.
        return self._everything_visible_for(finding, sight)

    def _attempt_visible(self, attempt: AttemptView, sight: _Sight) -> bool:
        if attempt.backend not in sight.readable:
            return False
        listing = sight.namespaced.get(attempt.backend)
        if listing is None:
            return True          # read in one call; readable means all of it
        namespace = self._attempt_namespace(
            sight.handles[attempt.backend], attempt, sight.tenant_namespaces
        )
        # Positively read, not merely "not reported unreadable": a namespace
        # this pass never asked about proves nothing about what runs in it.
        return namespace is not None and namespace in listing.readable

    def _everything_visible_for(self, finding: Finding, sight: _Sight) -> bool:
        if len(sight.readable) != len(self._backends):
            return False
        for name, listing in sight.namespaced.items():
            if not listing.unreadable:
                continue
            if not finding.tenant_id:
                return False
            namespace = sight.handles[name].namespace_for(
                finding.tenant_id, sight.tenant_namespaces.get(finding.tenant_id)
            )
            if namespace in listing.unreadable:
                return False
        return True

    def _admit(
        self, finding: Finding, snapshot: ControlSnapshot, sight: _Sight
    ) -> Finding | _Disproved | None:
        """The finding to act on -- possibly re-stated by a probe -- or why not.

        None means held back: logged as NOT_REPAIRING and counted as
        suppressed. `_DISPROVED` means a probe showed the finding was wrong,
        and it is dropped without either.
        """
        if self._is_actionable(finding, snapshot, sight):
            return finding
        probed = self._probe(finding, snapshot, sight)
        if probed is not None:
            return probed
        attempt = snapshot.attempts.get(finding.attempt_id or "")
        self._log.warning(
            NOT_REPAIRING,
            task_id=finding.task_id,
            lease_id=finding.lease_id,
            kind=finding.kind.value,
            backend=attempt.backend if attempt else None,
            namespace=self._namespace_for_report(attempt, sight),
        )
        return None

    def _probe(
        self, finding: Finding, snapshot: ControlSnapshot, sight: _Sight
    ) -> Finding | _Disproved | None:
        """Ask for THIS attempt's execution by name when its list was unreadable.

        The list and the name are different questions. The 2026-09-24 leases
        each named their Job exactly (`swarm-tenant-eng/swarm-<task>-1`), the
        Jobs had been deleted by their TTL an hour earlier, and a namespaced GET
        would have answered 404 on every pass -- proof of absence that the list
        refusal was hiding. What each answer allows is in `GkeBackend.probe`;
        what this does with it:

        * absent or finished -- the finding stands as found, and is repaired;
        * active -- the probe has found what the failed list would have
          returned, so the verdict is the one the LIST path reaches with this
          Job in hand (`_listed_verdict`): a DEAD_WORKER carrying the live Job
          where that path would terminate -- `_repair` then fences, TERMINATES
          and only then releases -- and `_DISPROVED` where it would do nothing;
        * anything else -- nothing is proven and the lease stays held.

        The active case used to become a DEAD_WORKER unconditionally, and that
        killed healthy agents. A missing_execution is raised only because the
        attempt was absent from `by_attempt`; when the list fails, EVERY
        attempt in that namespace is absent, including ones whose lease
        heartbeated seconds ago. Listed, such a Job produces no finding at all.
        Probed, it was fenced, deleted, released and re-queued -- on every pass
        where a LIST failed and a GET did not (APF 429, a 5xx blip, a timeout,
        or RBAC granting `get` without `list`).
        """
        if not self._depends_on_absence(finding, snapshot):
            return None
        attempt = snapshot.attempts.get(finding.attempt_id or "")
        if attempt is None or not attempt.execution_name:
            return None
        backend = sight.handles.get(attempt.backend)
        probe = getattr(backend, "probe", None)
        if not callable(probe):
            return None
        key = f"{attempt.backend}:{attempt.execution_name}"
        result = sight.probes.get(key)
        if result is None:
            try:
                result = probe(attempt.execution_name)
            except Exception as exc:
                result = Probe(ProbeOutcome.UNREADABLE, detail=f"{type(exc).__name__}: {exc}")
            sight.probes[key] = result
            self._log.info(
                "probed an execution by name",
                execution=attempt.execution_name,
                backend=attempt.backend,
                outcome=result.outcome.value,
                detail=result.detail,
            )
        if result.outcome is ProbeOutcome.UNREADABLE:
            return None
        detail = {**finding.detail, "probe": result.outcome.value, "probe_detail": result.detail}
        if result.outcome in (ProbeOutcome.ABSENT, ProbeOutcome.FINISHED):
            return replace(
                finding,
                detail=detail,
                reason=f"{finding.reason}; confirmed by name: {result.detail}",
                execution=result.execution,
            )
        execution = result.execution
        if execution is None or not self._probe_matches(finding, execution, snapshot):
            return None
        verdict = self._listed_verdict(finding, execution, snapshot)
        if verdict is None:
            # No lease in the snapshot to judge: prove nothing, hold.
            return None
        if isinstance(verdict, _Disproved):
            self._log.info(
                "probe found the execution alive under a live lease; finding disproved",
                task_id=finding.task_id,
                lease_id=finding.lease_id,
                kind=finding.kind.value,
                execution=attempt.execution_name,
                backend=attempt.backend,
            )
            return verdict
        return replace(
            finding,
            kind=FindingKind.DEAD_WORKER,
            execution=execution,
            detail={**detail, "terminates_because": verdict},
            reason=f"{finding.reason}; its job is still active by name: {result.detail}",
        )

    def _listed_verdict(
        self, finding: Finding, execution: ExecutionView, snapshot: ControlSnapshot
    ) -> str | _Disproved | None:
        """What the LIST path concludes about this lease once it holds this Job.

        The probe has found, by name, the active Job a readable list would have
        returned. Acting on it may go no further than the list path would have
        gone with that same Job in hand: a probe is a way to SEE past a failed
        list, never a reason to act more harshly than seeing does. So the list
        path's own execution rules are re-run -- not restated -- over this one
        lease with this one Job:

        * `detect_stale_leases`, with the Job in `by_attempt`: a stale lease
          over a live Job is `dead_worker`, and a live lease is nothing at all.
        * `detect_orphan_executions`: a Job whose generation is not the task's
          is `obsolete_generation` (the partial-repair case: fenced, never
          released); one whose task this snapshot does not hold is an orphan.

        Whichever of those requires termination names the reason, and the
        caller kills before it releases. If none does -- in practice a
        missing_execution raised only because the list failed, under a lease
        that is heartbeating -- the finding is `_DISPROVED`.

        `orphan_lease` is the one kind answered directly, and in the stricter
        direction. Its task points at a different lease (or its lease is
        superseded), and the list path's orphan-lease rule releases without
        looking for compute at all. Releasing capacity while its Job still runs
        is the thing this module exists to prevent, so here the Job is killed
        first.

        None when there is no lease in the snapshot to judge.
        """
        if finding.kind is FindingKind.ORPHAN_LEASE:
            return FindingKind.ORPHAN_LEASE.value
        lease: LeaseView | None = snapshot.leases.get(finding.lease_id or "")
        if lease is None:
            return None
        # The probe read the Job this attempt's own record names, and
        # `_probe_matches` tied it to the task, tenant and generation: it IS
        # this lease's execution, whatever spelling its labels gave the ids.
        held = replace(execution, task_id=lease.task_id, attempt_id=lease.attempt_id)
        only_this = replace(snapshot, leases={lease.lease_id: lease})
        now = snapshot.taken_at
        listed = [
            *detect_stale_leases(only_this, {lease.attempt_id: held}, self._config, now),
            *detect_orphan_executions(only_this, [held], self._config, now),
        ]
        terminating = sorted({f.kind.value for f in listed if f.requires_termination})
        return terminating[0] if terminating else _DISPROVED

    @staticmethod
    def _probe_matches(
        finding: Finding, execution: ExecutionView, snapshot: ControlSnapshot
    ) -> bool:
        """The probed Job is this finding's, by task, tenant and generation.

        The name came from the attempt's own record, so a mismatch here is not
        expected -- and is exactly when a kill must not be sent. Compared in
        sanitised form, because a label-sourced identifier has been through it.
        """
        if not execution.task_id or not finding.task_id:
            return False
        if sanitised(execution.task_id) != sanitised(finding.task_id):
            return False
        task = snapshot.tasks.get(finding.task_id)
        tenant = (task.tenant_id if task else None) or finding.tenant_id
        if tenant and execution.tenant_id and sanitised(tenant) != sanitised(execution.tenant_id):
            return False
        if (
            execution.generation is not None
            and finding.generation is not None
            and execution.generation != finding.generation
        ):
            return False
        return True

    def _namespace_for_report(self, attempt: AttemptView | None, sight: _Sight) -> str | None:
        if attempt is None or attempt.backend not in sight.namespaced:
            return None
        return self._attempt_namespace(
            sight.handles[attempt.backend], attempt, sight.tenant_namespaces
        )

    def _suppressed(
        self, finding: Finding, snapshot: ControlSnapshot, sight: _Sight
    ) -> SuppressedFinding:
        attempt = snapshot.attempts.get(finding.attempt_id or "")
        return SuppressedFinding(
            kind=finding.kind.value,
            lease_id=finding.lease_id,
            task_id=finding.task_id,
            backend=(attempt.backend or None) if attempt else None,
            namespace=self._namespace_for_report(attempt, sight),
        )

    # ------------------------------------------------------------------
    def _repair(
        self,
        finding: Finding,
        snapshot: ControlSnapshot,
        handles: dict[str, Any],
    ) -> RepairOutcome:
        """Invalidate, terminate, release, repair -- in that order, or not at all.

        `handles` is every configured backend, NOT only the ones this pass could
        list: see `_Sight`. Whether a finding may be acted on was settled by
        `_admit`; this only needs somewhere to send the kill.
        """
        outcome = RepairOutcome(
            kind=finding.kind.value,
            reason=finding.reason,
            tenant_id=finding.tenant_id,
            task_id=finding.task_id,
            lease_id=finding.lease_id,
            execution=finding.execution.name if finding.execution else None,
        )
        if finding.kind in _TERMINATE_ONLY:
            return self._repair_left_running(finding, outcome, handles)
        if finding.kind in _FENCE_ONLY:
            return self._repair_stuck(finding, outcome)
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
        if not self._terminate(finding, outcome, handles):
            return outcome

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
                # A requested cancel is FINISHED here, in one hop. Returning the
                # task to READY and leaving the flag for the scheduler's drain
                # to act on is a second hop through a second service, and it
                # records the wrong outcome whenever retries are spent: READY
                # is downgraded to FAILED, so a task somebody stopped was
                # written down as having failed. DISPATCHED/STARTING/RUNNING/
                # LEASED -> CANCELLED are all legal (swarm_common.states), and
                # `repair_task_state` re-reads the flag inside its transaction,
                # so a cancel pressed after this snapshot is honoured too.
                cancelling = task.cancel_requested
                repaired = self._store.repair_task_state(
                    finding.task_id,
                    to_state=TaskState.CANCELLED if cancelling else TaskState.READY,
                    # Only the task this finding is actually about. A snapshot is
                    # minutes old by the time slow terminations ahead of it are
                    # done, and the task may legitimately be on a newer lease by
                    # now -- see `repair_task_state`.
                    expected_lease_id=finding.lease_id,
                    error=(
                        f"cancelled on request; reconciled: {finding.reason}"
                        if cancelling
                        else f"reconciled: {finding.reason}"
                    ),
                    next_eligible_at=utcnow(),
                )
                if repaired is not None:
                    outcome.repaired_to = repaired.value
                    outcome.actions.append(f"task -> {repaired.value}")
                    detail: dict[str, Any] = {
                        "reason": finding.kind.value,
                        "detail": finding.reason,
                    }
                    if repaired is TaskState.CANCELLED:
                        # The API's flag-only cancel wrote `cancel_requested`
                        # (before 2026-09-24: a `cancelled` with
                        # phase=cancel_requested). THIS is the terminal
                        # `cancelled`, written by the component that released
                        # the lease -- contract request 17.
                        detail.update(phase="cancelled", from_state=task.state.value)
                    self._store.emit(
                        task_id=finding.task_id,
                        tenant_id=finding.tenant_id,
                        event_type=_EVENT_FOR_REPAIR.get(repaired, EventType.FAILED),
                        detail=detail,
                        attempt_id=finding.attempt_id,
                        lease_id=finding.lease_id,
                    )
        return outcome

    def _held_for_unstopped(self, finding: Finding) -> RepairOutcome:
        """The outcome of a release this pass refuses: its lease's execution still runs."""
        self._log.warning(
            "not releasing: this pass could not stop the execution holding the lease",
            kind=finding.kind.value,
            task_id=finding.task_id,
            lease_id=finding.lease_id,
        )
        return RepairOutcome(
            kind=finding.kind.value,
            reason=finding.reason,
            tenant_id=finding.tenant_id,
            task_id=finding.task_id,
            lease_id=finding.lease_id,
            skipped="execution_not_stopped",
            actions=[
                "did NOT release: an earlier repair in this pass could not stop the "
                "execution holding this lease"
            ],
        )

    def _terminate(
        self, finding: Finding, outcome: RepairOutcome, handles: dict[str, Any]
    ) -> bool:
        """Stop the finding's execution; True only when there is nothing left running.

        True, too, when there was nothing to stop. False means the caller must
        NOT go on to release anything: the kill was refused, failed, or not
        confirmed, and `outcome` already says which.
        """
        if finding.execution is None or not finding.execution.is_active:
            return True
        backend = handles.get(finding.execution.backend)
        if backend is None:
            outcome.skipped = "backend_unavailable"
            outcome.actions.append("did NOT release: backend unavailable to confirm the kill")
            return False
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
            return False
        if not outcome.terminated:
            outcome.skipped = "termination_unconfirmed"
            outcome.actions.append("did NOT release: termination was not confirmed")
            return False
        outcome.actions.append(f"terminated {finding.execution.name}")
        return True

    def _repair_stuck(self, finding: Finding, outcome: RepairOutcome) -> RepairOutcome:
        """Fence a stuck attempt, and leave the rest to the worker and the next pass.

        The worker under a stuck browser is ALIVE -- that is what makes it
        stuck rather than dead -- and a live worker handles the two ways of
        being stopped very differently:

        * **It notices the fence** at its next control poll (every 10s):
          `lifecycle._apply_control_signals` stops the runner child, emits
          `generation_fenced` and exits 70, writing nothing to the task or the
          lease (invariant 5). The Job then ends on its own (`backoffLimit: 0`).
        * **It receives SIGTERM** -- which is what deleting its Job sends it:
          `lifecycle._handle_interruption` checkpoints and PARKS the task
          `SCHEDULED_RETRY` without checking whether it was fenced, and nothing
          in the platform promotes that park (scheduler/dispatch.py says so at
          WORKER_FINALISE_BUDGET_SECONDS). Racing the reconciler's own READY, it
          would turn an evicted task into one parked for ever, or -- when the
          scheduler had already leased it again, so PARKED is illegal -- into
          FAILED through `_safe_finish`. Read from the code, not observed: no
          browser attempt has reached RUNNING on this platform yet.

        So this pass only fences, and names the reason on the task's timeline.
        What the owner asked for still happens, in the order that cannot race:
        the worker ends the agent itself; then, on a later pass, the lease is
        superseded and silent, so the existing rules take it -- the
        obsolete-generation rule terminates the Job first if it is somehow
        still active, the partial-repair rule in `detect_stale_leases` releases
        through the frozen `release_lease_in_transaction`, and
        `repair_task_state` requeues it or fails it once its attempts are spent.
        Nothing here can re-find it meanwhile: the stuck rule only judges an
        attempt on its task's CURRENT generation.
        """
        if self._config.dry_run:
            outcome.skipped = "dry_run"
            outcome.actions.append(
                f"would fence generation {finding.generation}; then {_AFTER_THE_FENCE}"
            )
            return outcome
        if not finding.task_id or finding.generation is None:
            outcome.skipped = "nothing_to_fence"
            return outcome
        new_generation = self._store.invalidate_generation(finding.task_id, finding.generation)
        outcome.invalidated_to = new_generation
        if new_generation is None:
            # Moved on since the snapshot -- fenced by someone else, finished,
            # or re-admitted. The attempt this finding is about has already
            # been superseded, and nothing of the newer one is touched.
            outcome.skipped = "already_superseded"
            outcome.actions.append(
                f"did not fence: the task is no longer at generation {finding.generation}"
            )
            return outcome
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
                "then": _AFTER_THE_FENCE,
            },
            attempt_id=finding.attempt_id,
            lease_id=finding.lease_id,
            generation=finding.generation,
        )
        outcome.actions.append(f"then: {_AFTER_THE_FENCE}")
        self._log_eviction(finding, outcome)
        return outcome

    def _repair_left_running(
        self, finding: Finding, outcome: RepairOutcome, handles: dict[str, Any]
    ) -> RepairOutcome:
        """Terminate a Job whose task already finished; release only its own lease.

        No fence. The task is terminal, so there is no generation left for this
        Job to run under -- and `invalidate_generation` on a task that has been
        re-opened since (FAILED -> READY is legal) would bump a generation that
        belongs to the NEXT attempt. No task-state repair either: a terminal
        state is the worker's own record of how the task ended.

        The lease on the finding, if any, is this Job's own: same attempt, same
        generation, unreleased (`detect_left_running`). It is released only
        after the kill is confirmed, for the same reason every release here is.
        """
        name = finding.execution.name if finding.execution else None
        if self._config.dry_run:
            outcome.skipped = "dry_run"
            outcome.actions.append(
                f"would terminate {name}"
                + (f" and release {finding.lease_id}" if finding.lease_id else "")
            )
            return outcome
        if not self._terminate(finding, outcome, handles):
            return outcome
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
        if finding.task_id:
            # The event that names the reason on the task's own timeline. The
            # frozen EventType has no "execution evicted"; GENERATION_FENCED is
            # the nearest -- the reconciler stopped an execution of this
            # generation that had no right to run -- and `phase` says which
            # kind of stop it was. Contract request 19 asks for a type of its own.
            self._store.emit(
                task_id=finding.task_id,
                tenant_id=finding.tenant_id,
                event_type=EventType.GENERATION_FENCED,
                detail={
                    "reason": finding.reason,
                    "finding": finding.kind.value,
                    "phase": "left_running",
                    "execution": name,
                    "task_state": finding.detail.get("task_state"),
                    "lease_released": outcome.released,
                },
                attempt_id=finding.attempt_id,
                lease_id=finding.lease_id,
                generation=finding.generation,
            )
        self._log_eviction(finding, outcome)
        return outcome

    def _log_eviction(self, finding: Finding, outcome: RepairOutcome) -> None:
        self._log.info(
            EVICTED,
            kind=finding.kind.value,
            task_id=finding.task_id,
            tenant_id=finding.tenant_id,
            execution=outcome.execution,
            namespace=finding.detail.get("namespace"),
            terminated=outcome.terminated,
            released=outcome.released,
            repaired_to=outcome.repaired_to,
            reason=finding.reason,
        )

    # ------------------------------------------------------------------
    def _collect_checkpoints(self, report: ReconcileReport, *, now: datetime) -> None:
        """Reclaim checkpoints nothing can resume from.

        Separate from `_collect_garbage` for two reasons. It asks a different
        question of the control plane -- which tasks are FINISHED, where
        `snapshot()` reads only the four states that hold capacity -- and it
        lists an entire bucket, so it runs on its own slower clock instead of on
        every five-minute tick.
        """
        if self._checkpoints is None or not self._config.enable_checkpoint_gc:
            return
        if self._config.dry_run:
            # The claim is a WRITE. A dry run that took it would make the next
            # real pass skip its sweep, so "dry run changes nothing" has to mean
            # this too -- but the sweep itself still runs and still reports what
            # it would have deleted, because a dry run that reported nothing
            # would be useless for answering "is it safe to enable this".
            #
            # The consequence is deliberate and worth knowing: a reconciler left
            # in dry run lists the bucket on EVERY pass rather than hourly.
            sweep = self._checkpoints.sweep(now=now)
        else:
            if not self._store.claim_checkpoint_sweep(
                min_interval_seconds=self._config.checkpoint_sweep_interval_seconds, now=now
            ):
                return
            sweep = self._checkpoints.sweep(now=now)

        report.checkpoint_sweep = sweep.as_dict()
        report.errors.extend(sweep.errors)
        for item in sweep.outcomes:
            outcome = RepairOutcome(
                kind=item.kind,
                reason=item.reason,
                tenant_id=item.tenant_id,
                task_id=item.task_id,
                skipped=item.skipped,
            )
            if item.deleted:
                outcome.deleted = item.prefix
                outcome.actions.append(f"deleted {item.objects_deleted} objects under {item.prefix}")
            elif item.skipped == "dry_run":
                outcome.actions.append(f"would delete {item.prefix}")
            else:
                outcome.actions.append(f"kept {item.prefix}")
            report.outcomes.append(outcome)

    # ------------------------------------------------------------------
    def _collect_garbage(
        self, snapshot: ControlSnapshot, sight: _Sight, report: ReconcileReport
    ) -> None:
        """Remove per-tenant infrastructure nothing is using any more.

        Cloud Run pins the service account on the Job resource, so the
        dispatcher creates one per (tenant, profile) and they accumulate. This
        is where they stop accumulating -- and it only ever removes resources
        carrying this platform's own label.

        Only on backends this pass could read, and EACH IN ITS OWN TRY: one
        backend's collection failing used to abort the loop, so a GKE fault
        stopped Cloud Run's Job resources being collected as well.
        """
        try:
            active = self._store.active_tenants(snapshot)
            # A namespace is protected by mere registration; a Job resource is
            # not. The difference is recoverability: `ensure_job` recreates a
            # deleted Job on the next dispatch, whereas nothing recreates a
            # namespace, its service account or its workload-identity binding.
            protected_namespaces = active | self._store.registered_tenants()
        except Exception as exc:
            self._log.exception("garbage collection failed", exc)
            report.errors.append(f"gc: {exc}")
            return
        for backend in self._backends:
            if backend.name not in sight.readable:
                continue
            try:
                report.outcomes.extend(
                    self._collect_garbage_on(
                        backend, snapshot, active=active, protected=protected_namespaces
                    )
                )
            except Exception as exc:
                self._log.exception("garbage collection failed", exc, backend=backend.name)
                report.errors.append(f"gc {backend.name}: {exc}")

    def _collect_garbage_on(
        self,
        backend: Any,
        snapshot: ControlSnapshot,
        *,
        active: set[str],
        protected: set[str],
    ) -> list[RepairOutcome]:
        outcomes: list[RepairOutcome] = []
        now = snapshot.taken_at
        resources = backend.list_job_resources()
        if backend.name == "GKE_AUTOPILOT":
            findings = detect_empty_namespaces(resources, protected, self._config, now)
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
