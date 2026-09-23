"""The bounded drain loop.

Shape, and why it is this shape:

    woken by Pub/Sub
      -> promote what has become runnable (dependencies, credentials, prewarm)
      -> while admissible work exists and the run is within budget:
           read a slice of READY work
           interleave it round-robin across tenants
           try to admit each, in order
      -> exit

It EXITS. It does not sit in a loop waiting for capacity, because a scheduler
that waits is a scheduler that is billed for waiting and that gets killed
mid-transaction when the platform reclaims it. Whatever it could not admit stays
READY in Firestore, costing nothing, and the next push -- or the 1-minute Cloud
Scheduler safety tick -- picks it up.

AdmissionDenied is NOT an error and never stops the loop. It is written to
`task.blocked_by` so the caller can see why, and the loop moves to the next
task -- which will usually belong to a different tenant, and will usually be
admissible. Breaking out on the first denial is how one full pool ends up
stalling every other tenant's work.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from swarm_common.admission import AdmissionConfig, AdmissionDenied
from swarm_common.models import Lease, Task, Tenant, utcnow
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend
from swarm_common.states import EventType, ParkReason, TaskState

from .dispatch import BackendRouter, DispatchError
from .fairness import AgingConfig, round_robin_order
from .metrics import SchedulerMetrics
from .settings import SchedulerSettings
from .store import SchedulerStore

log = logging.getLogger(__name__)

#: Park reasons that a provider pool's `quota_derived_limit` already guards, so
#: promoting them early is safe: admission still refuses until quota returns.
PREWARM_REASONS = (
    ParkReason.PROVIDER_QUOTA_EXHAUSTED,
    ParkReason.PROVIDER_COOLDOWN,
    ParkReason.PROVIDER_OUTAGE,
)

_FAILED_PARENT_STATES = frozenset(
    {TaskState.FAILED, TaskState.CANCELLED, TaskState.DEAD_LETTERED}
)


@dataclass
class DrainReport:
    passes: int = 0
    scanned: int = 0
    leased: int = 0
    dispatched: int = 0
    denied: int = 0
    parked: int = 0
    cancelled: int = 0
    skipped: int = 0
    dispatch_failures: int = 0
    promoted_dependencies: int = 0
    promoted_credentials: int = 0
    promoted_prewarm: int = 0
    tenants_seen: int = 0
    topped_up_tenants: int = 0
    stop_reason: str = "not_started"
    duration_seconds: float = 0.0
    blockers: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Scheduler:
    def __init__(
        self,
        *,
        settings: SchedulerSettings,
        store: SchedulerStore,
        router: BackendRouter,
        metrics: SchedulerMetrics | None = None,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._store = store
        self._router = router
        self._metrics = metrics or SchedulerMetrics()
        self._now = now
        self._monotonic = monotonic
        self._aging = AgingConfig(
            interval_seconds=settings.aging_interval_seconds,
            step=settings.aging_step,
            max_bonus=settings.aging_max_bonus,
        )
        # Populated per drain; initialised here so `_admit_one` is callable in
        # isolation (the unit tests exercise it directly).
        self._tenant_cache: dict[str, Tenant | None] = {}
        self._admission = AdmissionConfig(
            dispatch_timeout_seconds=settings.core.dispatch_timeout_seconds,
            lease_timeout_seconds=settings.core.lease_timeout_seconds,
            heartbeat_interval_seconds=settings.core.heartbeat_interval_seconds,
        )

    @property
    def metrics(self) -> SchedulerMetrics:
        return self._metrics

    @property
    def store(self) -> SchedulerStore:
        return self._store

    @property
    def settings(self) -> SchedulerSettings:
        return self._settings

    # -- entry point ------------------------------------------------------

    def drain(self) -> DrainReport:
        started = self._monotonic()
        report = DrainReport()
        self._tenant_cache: dict[str, Tenant | None] = {}
        self._topup_tenant_ids: list[str] | None = None

        if self._store.dispatch_paused():
            self._metrics.paused.set(1)
            report.stop_reason = "dispatch_paused"
            report.duration_seconds = self._monotonic() - started
            self._metrics.runs.labels(stop_reason=report.stop_reason).inc()
            self._metrics.run_seconds.observe(report.duration_seconds)
            log.info("drain skipped: dispatch is paused by an admin")
            return report
        self._metrics.paused.set(0)

        report.promoted_dependencies = self._promote_dependencies()
        report.promoted_credentials = self._promote_credentials()
        if self._settings.enable_prewarm:
            report.promoted_prewarm = self._prewarm()

        while True:
            if report.passes >= self._settings.max_passes_per_run:
                report.stop_reason = "max_passes"
                break
            if report.leased >= self._settings.max_leases_per_run:
                report.stop_reason = "max_leases"
                break
            if self._monotonic() - started >= self._settings.max_run_seconds:
                report.stop_reason = "time_budget"
                break

            candidates = self._store.ready_tasks(self._settings.candidate_batch_size)
            if not candidates:
                self._metrics.candidates.observe(0)
                report.stop_reason = "queue_empty"
                break
            candidates, topped_up = self._top_up_starved_tenants(candidates)
            report.topped_up_tenants = max(report.topped_up_tenants, topped_up)
            self._metrics.candidates.observe(len(candidates))

            ordered = round_robin_order(
                candidates,
                now=self._now(),
                config=self._aging,
                active_by_tenant=self._store.active_by_tenant(),
            )
            tenants = {task.tenant_id for task in ordered}
            report.tenants_seen = max(report.tenants_seen, len(tenants))
            self._metrics.tenants_in_rotation.set(len(tenants))

            leased_this_pass = 0
            for task in ordered:
                report.scanned += 1
                if report.leased >= self._settings.max_leases_per_run:
                    break
                if self._monotonic() - started >= self._settings.max_run_seconds:
                    break
                if self._admit_one(task, report):
                    leased_this_pass += 1
            report.passes += 1

            if leased_this_pass == 0:
                # Nothing in this slice could be admitted. Re-reading the same
                # slice would produce the same answer, so the run is over.
                report.stop_reason = "no_admissible_work"
                break

        report.duration_seconds = self._monotonic() - started
        self._metrics.runs.labels(stop_reason=report.stop_reason).inc()
        self._metrics.run_seconds.observe(report.duration_seconds)
        log.info("drain finished %s", report.to_dict())
        return report

    # -- candidate selection ----------------------------------------------

    def _top_up_starved_tenants(self, candidates: list[Task]) -> tuple[list[Task], int]:
        """Add candidates for tenants the global slice left out entirely.

        `ready_tasks` returns the globally highest-priority slice. Round-robin
        and starvation aging both operate on that slice, so neither can help a
        tenant that is not IN it -- and a tenant holding more than
        `candidate_batch_size` high-priority tasks fills it by itself. Priority
        is a number the caller chooses, so that is a one-line denial of service
        against every other tenant, and it is exactly the failure round-robin
        exists to prevent.

        The top-up runs only when the slice came back FULL. A short slice is the
        entire READY queue, so nobody can be missing from it and the extra
        queries would buy nothing. That keeps the ordinary case at exactly one
        query per pass, which is what the hot path was designed around.
        """
        budget = self._settings.tenant_topup_candidates
        if budget <= 0 or len(candidates) < self._settings.candidate_batch_size:
            return candidates, 0

        if self._topup_tenant_ids is None:
            # Once per drain, not once per pass: the tenant roster does not
            # change inside a 45-second run.
            self._topup_tenant_ids = self._store.enabled_tenant_ids(
                self._settings.max_topup_tenants
            )

        represented = {task.tenant_id for task in candidates}
        missing = [tid for tid in self._topup_tenant_ids if tid not in represented]
        if not missing:
            return candidates, 0

        seen = {task.id for task in candidates}
        topped = list(candidates)
        tenants_added = 0
        for tenant_id in missing:
            extra = self._store.ready_tasks_for_tenant(tenant_id, budget)
            added = False
            for task in extra:
                if task.id in seen:
                    continue
                seen.add(task.id)
                topped.append(task)
                added = True
            if added:
                tenants_added += 1
        if tenants_added:
            self._metrics.topped_up.inc(tenants_added)
            log.info(
                "candidate slice was saturated; topped up %d starved tenant(s)",
                tenants_added,
            )
        return topped, tenants_added

    # -- one task ---------------------------------------------------------

    def _tenant(self, tenant_id: str) -> Tenant | None:
        if tenant_id not in self._tenant_cache:
            self._tenant_cache[tenant_id] = self._store.get_tenant(tenant_id)
        return self._tenant_cache[tenant_id]

    def _admit_one(self, task: Task, report: DrainReport) -> bool:
        """Try to admit and dispatch one task. Returns True only if leased."""
        now = self._now()

        if task.cancel_requested:
            # It holds no capacity in READY, so it can be finished here.
            self._store.cancel(task, "cancellation requested before admission")
            report.cancelled += 1
            return False

        if task.next_eligible_at is not None and task.next_eligible_at > now:
            report.skipped += 1
            return False

        profile = RUNNER_PROFILES.get(task.runner_profile)
        if profile is None:
            # The catalogue no longer has this profile. Parking rather than
            # failing keeps the work recoverable if an admin restores it.
            self._store.park(
                task,
                ParkReason.MANUAL_PAUSE,
                detail={"error": f"runner_profile {task.runner_profile!r} is not in the catalogue"},
            )
            self._metrics.parked.labels(reason=ParkReason.MANUAL_PAUSE.value).inc()
            report.parked += 1
            return False

        if task.depends_on:
            states = self._store.task_states(task.depends_on)
            failed = [tid for tid, state in states.items() if state in _FAILED_PARENT_STATES]
            if failed:
                self._store.cancel(
                    task,
                    "an upstream workflow step did not succeed",
                    {"failed_parents": failed},
                )
                report.cancelled += 1
                return False
            if any(states.get(tid) is not TaskState.SUCCEEDED for tid in task.depends_on):
                self._store.park(
                    task,
                    ParkReason.DEPENDENCY_INCOMPLETE,
                    detail={
                        "waiting_on": [
                            tid
                            for tid in task.depends_on
                            if states.get(tid) is not TaskState.SUCCEEDED
                        ]
                    },
                )
                self._metrics.parked.labels(
                    reason=ParkReason.DEPENDENCY_INCOMPLETE.value
                ).inc()
                report.parked += 1
                return False

        tenant = self._tenant(task.tenant_id)
        if tenant is None or not tenant.enabled:
            self._store.park(
                task,
                ParkReason.MANUAL_PAUSE,
                detail={"error": "tenant is missing or disabled"},
            )
            self._metrics.parked.labels(reason=ParkReason.MANUAL_PAUSE.value).inc()
            report.parked += 1
            return False

        if profile.provider and profile.provider not in tenant.credentials:
            # Admitting this would start a container that can only fail, and it
            # would hold a slot while doing so.
            self._store.park(
                task,
                ParkReason.CREDENTIAL_MISSING,
                detail={"provider": profile.provider},
            )
            self._metrics.parked.labels(reason=ParkReason.CREDENTIAL_MISSING.value).inc()
            report.parked += 1
            return False

        backend = resolve_backend(profile)
        units = RESOURCE_CLASSES[task.resource_class].units

        try:
            lease = self._store.acquire_lease(
                task, units=units, backend=backend.value, config=self._admission
            )
        except AdmissionDenied as denied:
            self._store.record_blockers(task, denied.reasons)
            report.denied += 1
            first = denied.reasons[0] if denied.reasons else {}
            reason = str(first.get("reason", "unknown"))
            report.blockers[reason] = report.blockers.get(reason, 0) + 1
            self._metrics.denied.labels(reason=reason).inc()
            # Deliberately NOT a break: the next task is probably another
            # tenant's, and probably admissible.
            return False

        report.leased += 1
        self._metrics.leased.labels(
            tenant=task.tenant_id, runner_profile=task.runner_profile
        ).inc()

        # EVERYTHING FROM HERE IS INSIDE THE GUARD, and the guard catches more
        # than DispatchError. `acquire_lease_in_transaction` has committed: the
        # pools are incremented, the lease document exists and the task is
        # LEASED. Nothing in this service ever looks at a LEASED task again --
        # `ready_tasks()` selects `state == "READY"` only -- so any exception
        # escaping from here is a slot this scheduler can neither see nor undo,
        # and invariant 3 counts it as occupied until the reconciler's deadline
        # sweep reaches it minutes later, in another service.
        #
        # `create_attempt` and `append_event` used to sit above the try entirely,
        # and the try caught DispatchError alone -- which the Cloud Run client
        # construction inside `ensure_job` does NOT raise: a credential refresh
        # failure there is a `DefaultCredentialsError` and went straight out of
        # `drain()`. docs/audits/2026-09-18/05-scheduler-capacity-leaks.md,
        # findings 1 and 2.
        try:
            self._store.create_attempt(task, lease, backend.value)
            self._store.append_event(
                task,
                EventType.LEASE_ACQUIRED,
                {"pools": lease.pools, "units": lease.units, "backend": backend.value},
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                generation=lease.generation,
            )
            execution = self._router.dispatch(
                task=task, lease=lease, profile=profile, tenant=tenant, backend=backend
            )
        except DispatchError as exc:
            # Give the capacity straight back. Leaving the lease would hold a
            # slot no container will ever occupy until the dispatch deadline.
            #
            # The tenant gets `exc.code` plus the attempt id; the upstream API's
            # own message goes to the log line below and nowhere else. Backend
            # errors routinely echo the resource they were handed -- the tenant
            # service account, the job name, the secret names -- and
            # `task.last_error` is returned to the caller verbatim.
            self._store.return_to_ready_after_failed_dispatch(
                task, lease, exc.code, correlation_id=lease.attempt_id
            )
            report.dispatch_failures += 1
            self._metrics.dispatch_failures.labels(backend=backend.value).inc()
            log.warning(
                "dispatch failed task=%s attempt=%s backend=%s code=%s: %s",
                task.id,
                lease.attempt_id,
                backend.value,
                exc.code,
                exc,
            )
            # False: no capacity is held and nothing started, so this is not
            # progress. Reporting it as progress would keep the loop spinning on
            # a backend that is refusing dispatches.
            return False
        except Exception as exc:
            # Not a dispatch failure -- an unexpected one. Give the capacity back
            # and RE-RAISE. Swallowing it would turn a broken credential or a
            # broken Firestore into a scheduler that quietly admits, rolls back
            # and retries the whole queue on every tick with nothing red
            # anywhere; the leak is the bug being fixed here, not the crash.
            self._rollback_admission(task, lease, exc)
            raise

        self._store.mark_dispatched(task, lease, execution, backend.value)
        report.dispatched += 1
        self._metrics.dispatched.labels(backend=backend.value).inc()
        # A SUCCESS LINE, because the absence of one is what hid a total outage.
        #
        # Only `dispatch failed` was ever logged. That reads as reasonable --
        # why log the happy path -- until you try to answer "has GKE_AUTOPILOT
        # ever dispatched?" and find the query returns nothing whether the
        # backend is healthy or has never worked once. On 2026-09-23 it had
        # never worked once: seven browser tasks, seven failures, over two days,
        # and the only way to establish that was to read task documents.
        #
        # `swarm_scheduler_dispatched_total{backend}` already counts this, and a
        # counter that stays at zero is exactly as invisible as a log line that
        # is never written unless something is watching it -- which nothing was.
        # The log line is the cheap half of the fix; the alert on the metric is
        # in terraform/modules/monitoring/alerts.tf.
        log.info(
            "dispatch ok task=%s attempt=%s backend=%s profile=%s execution=%s",
            task.id,
            lease.attempt_id,
            backend.value,
            profile.name,
            execution,
        )
        return True

    def _rollback_admission(self, task: Task, lease: Lease, cause: BaseException) -> None:
        """Return a lease taken by an admission whose follow-through blew up.

        Best effort, and it must never replace `cause` with its own failure: the
        operator needs the fault that started this, not the second one it caused.
        `return_to_ready_after_failed_dispatch` releases the lease BEFORE it
        writes the task and the event, so even a Firestore that is refusing
        writes usually gets the pools back -- the ordering that matters here.

        `scheduler_internal_error` is a stable code, not the exception text:
        `task.last_error` is returned to the tenant verbatim by
        `codec.task_to_api`, and an auth or transport error routinely echoes the
        service account, the job name or the secret names it was handed.
        """
        self._metrics.admission_rollbacks.inc()
        log.exception(
            "admission follow-through failed task=%s lease=%s attempt=%s: %s",
            task.id,
            lease.lease_id,
            lease.attempt_id,
            cause,
        )
        try:
            self._store.return_to_ready_after_failed_dispatch(
                task, lease, "scheduler_internal_error", correlation_id=lease.attempt_id
            )
        except Exception:
            log.exception(
                "could not return the capacity for task=%s lease=%s; the reconciler's "
                "deadline sweep is now the only thing that will reclaim it",
                task.id,
                lease.lease_id,
            )

    # -- promotion sweeps -------------------------------------------------

    def _promote_dependencies(self) -> int:
        """Return DEPENDENCY_INCOMPLETE tasks to READY once every parent SUCCEEDED.

        This is the "returns to READY when the last parent succeeds" half of
        dependency resolution. It runs at the top of the drain rather than being
        triggered by the succeeding worker, so a worker that dies immediately
        after writing SUCCEEDED cannot strand its children.
        """
        promoted = 0
        for task in self._store.parked_tasks(
            ParkReason.DEPENDENCY_INCOMPLETE, self._settings.dependency_sweep_size
        ):
            if not task.depends_on:
                self._store.promote_to_ready(task, detail={"reason": "no_dependencies"})
                # Counted in the metric as well as the report. Incrementing only
                # the report here made `swarm_scheduler_promoted{kind=
                # "dependency"}` disagree with DrainReport.promoted_dependencies,
                # so a dashboard built on the metric under-counted promotions.
                self._metrics.promoted.labels(kind="dependency").inc()
                promoted += 1
                continue
            states = self._store.task_states(task.depends_on)
            failed = [tid for tid, state in states.items() if state in _FAILED_PARENT_STATES]
            if failed:
                self._store.cancel(
                    task, "an upstream workflow step did not succeed",
                    {"failed_parents": failed},
                )
                continue
            if all(states.get(tid) is TaskState.SUCCEEDED for tid in task.depends_on):
                self._store.promote_to_ready(
                    task, detail={"reason": "dependencies_satisfied"}
                )
                self._metrics.promoted.labels(kind="dependency").inc()
                promoted += 1
        return promoted

    def _promote_credentials(self) -> int:
        """Re-ready tasks whose tenant has since registered the missing key."""
        promoted = 0
        for task in self._store.parked_tasks(
            ParkReason.CREDENTIAL_MISSING, self._settings.dependency_sweep_size
        ):
            tenant = self._tenant(task.tenant_id)
            if tenant is None or not tenant.enabled:
                continue
            profile = RUNNER_PROFILES.get(task.runner_profile)
            if profile is None:
                continue
            if not profile.provider or profile.provider in tenant.credentials:
                self._store.promote_to_ready(
                    task, detail={"reason": "credential_registered",
                                  "provider": profile.provider}
                )
                self._metrics.promoted.labels(kind="credential").inc()
                promoted += 1
        return promoted

    def _prewarm(self) -> int:
        """Promote quota-parked work just before its window reopens.

        BOUNDED by `prewarm_max_agents`, and safe by construction: the only
        tasks promoted are ones whose provider pool carries a
        `quota_derived_limit`, so if the estimate is wrong admission simply
        refuses and the task stays READY -- which costs nothing (invariant 1).
        No container is started ahead of time; that would be spending money on a
        guess.
        """
        budget = max(0, self._settings.core.prewarm_max_agents)
        if budget == 0:
            return 0
        horizon = self._now() + timedelta(seconds=self._settings.core.prewarm_lead_seconds)
        # Read once: the guard below needs to know which provider pools exist.
        pools = self._store.pools()
        promoted = 0
        for reason in PREWARM_REASONS:
            if promoted >= budget:
                break
            for task in self._store.parked_tasks(reason, self._settings.dependency_sweep_size):
                if promoted >= budget:
                    break
                eligible_at = task.next_eligible_at
                if eligible_at is not None and eligible_at > horizon:
                    continue
                guard = f"provider:{task.provider}:tenant:{task.tenant_id}"
                if not task.provider or guard not in pools:
                    # Without that pool there is nothing carrying the provider's
                    # quota cap, so promoting early would let the task be
                    # admitted BEFORE the window reopens -- a container started
                    # on a guess. Leave it parked for the reconciler.
                    continue
                self._store.promote_to_ready(
                    task,
                    detail={"reason": "prewarm", "park_reason": reason.value,
                            "was_eligible_at": eligible_at.isoformat() if eligible_at else None},
                )
                self._metrics.promoted.labels(kind="prewarm").inc()
                promoted += 1
        return promoted
