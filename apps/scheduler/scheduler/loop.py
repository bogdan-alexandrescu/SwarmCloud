"""The bounded drain loop.

Shape, and why it is this shape:

    woken by Pub/Sub
      -> promote what has become runnable (dependencies, credentials, prewarm),
         and cancel what a failed `fail_workflow` workflow will never run
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
from swarm_common.models import EndCause, Lease, Task, Tenant, utcnow
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend
from swarm_common.states import PENDING_STATES, EventType, ParkReason, TaskState

from .credentials import AccountPool, CredentialSource, credential_for
from .dispatch import BackendRouter, DispatchError
from .fairness import AgingConfig, round_robin_order
from .metrics import SchedulerMetrics
from .settings import SchedulerSettings
from .store import GuardedWrite, ParentEnd, SchedulerStore

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


#: A CANCELLED parent with one of these causes was ended by a FAILURE -- its
#: own failed parent, or the fail_workflow sweep -- and passes a failure on.
_FAILURE_SENT_CAUSES = frozenset({EndCause.FAILED_PARENT.value, EndCause.WORKFLOW_SWEEP.value})
#: ... and with one of these, by a cancel somebody asked for, at that step or
#: above it.
_CANCEL_SENT_CAUSES = frozenset({EndCause.CANCEL_REQUESTED.value, EndCause.CANCELLED_PARENT.value})


def _parent_cause(parents: dict[str, ParentEnd], failed: list[str]) -> EndCause | None:
    """Why a dependant of `failed` is cancelled: after a failure, or after a cancel.

    The text the cancel writes -- "an upstream workflow step did not succeed"
    -- is the same for both, which is what made the outcome ledger count a
    cancel somebody pressed as a failure (#185, decision 2).

    A PARENT'S OWN END DECIDES, NOT ITS STATE (the review of #217). The rule
    is transitive -- `_FAILED_PARENT_STATES` holds CANCELLED, so a step
    cancelled after a failure is itself a "failed parent" to its own
    dependants -- and read by state alone, every step two or more hops below a
    FAILED one was recorded as "after a cancel" that nobody made. So:

      * FAILED_PARENT when any parent is FAILED or DEAD_LETTERED, or is
        CANCELLED by a failure (`failed_parent`, `workflow_sweep`). A failure
        wins over a stop beside it: the failure is what the dependant could
        not have survived.
      * CANCELLED_PARENT only when every cancelled parent was ended by a
        cancel somebody asked for (`cancel_requested`, `cancelled_parent`, or
        -- for a parent that ended before causes were recorded -- the flag).
      * None otherwise: a cancelled parent whose own end this read cannot name
        (it ended before causes were recorded, with no flag, or carries a cause
        this image does not know). The scheduler does not guess; the outcome
        ledger splits a task without a cause by following its chain of parents
        (`swarm_api.outcomes.cancel_cause`), which it can read and this sweep
        does not.
    """
    ends = [parents[tid] for tid in failed if tid in parents]
    if any(end.state in (TaskState.FAILED, TaskState.DEAD_LETTERED) for end in ends):
        return EndCause.FAILED_PARENT
    cancelled = [end for end in ends if end.state is TaskState.CANCELLED]
    if any(end.end_cause in _FAILURE_SENT_CAUSES for end in cancelled):
        return EndCause.FAILED_PARENT
    if cancelled and all(
        end.end_cause in _CANCEL_SENT_CAUSES or (end.end_cause is None and end.cancel_requested)
        for end in cancelled
    ):
        return EndCause.CANCELLED_PARENT
    return None


#: The one `on_step_failure` value the scheduler acts on. Anything else,
#: `continue` included, leaves only the dependency rule above in force.
#:
#: RESTATED, and the restatement is pinned. The vocabulary has no home in the
#: frozen contract: `Workflow.on_step_failure` is a bare `str`. The API
#: validates it as `Literal["fail_workflow", "continue"]`
#: (`swarm_api.schemas.WorkflowCreate`) and the MCP tool advertises it as an
#: enum. A rename on either side would make this scheduler ignore the setting
#: again, silently, which is the defect this code fixes.
#: `test_the_policy_vocabulary_agrees_everywhere_it_is_stated` holds all four
#: statements together, and contract request 20 asks for one home.
FAIL_WORKFLOW = "fail_workflow"

#: Step states that fail a workflow under `fail_workflow` (owner, 2026-09-24).
#:
#: CANCELLED IS DELIBERATELY ABSENT, although `_FAILED_PARENT_STATES` has it. A
#: step is CANCELLED because somebody stopped it, or because this sweep did.
#: If a stop counted as a failure, pressing stop on one agent would end the
#: whole run under the default setting, and the stop dialog's "the rest of the
#: run keeps going" would be false. A stop still takes its own dependents, by
#: the dependency rule.
_WORKFLOW_FAILING_STATES = (TaskState.FAILED, TaskState.DEAD_LETTERED)

#: "Has not started": SUBMITTED, QUEUED, READY, PARKED, which are exactly the
#: states that hold no capacity. Derived from the frozen set rather than
#: listed, and sorted only so that the sweep's query order is stable.
_NOT_STARTED_STATES = tuple(sorted(PENDING_STATES, key=lambda state: state.value))

#: How many failed steps a cancel event names. Ten is enough to say which ones
#: without sending one document per failure. A workflow with more failures
#: than that is no less failed, and the count is not what the reader needs.
_FAILED_STEPS_NAMED = 10


@dataclass(frozen=True)
class FailedWorkflow:
    """A `fail_workflow` workflow with at least one FAILED or DEAD_LETTERED step."""

    tenant_id: str
    workflow_id: str
    #: [{"step_id", "task_id", "state"}], in a stable order. This is what every
    #: cancel event carries, so a cancelled step's own record names the failure.
    failed_steps: tuple[dict[str, Any], ...]


def read_workflow_failure(
    store: SchedulerStore,
    tenant_id: str,
    workflow_id: str,
    *,
    enforced_since: datetime | None,
) -> FailedWorkflow | None:
    """Is this workflow failed under `fail_workflow`? One read of the answer.

    THE ONE STATEMENT OF THE VERDICT. The drain calls it (through its per-drain
    cache) and so does the read-only `on_step_failure_audit`, so what the audit
    says the next drain will cancel cannot drift from what the drain cancels.

    Costs one point read for the policy. Only under `fail_workflow` does it
    also cost one equality query per failing state. A `continue` workflow, and
    one created before `enforced_since`, is never asked about its failures,
    because nothing is done with the answer.
    """
    policy = store.workflow_on_step_failure(
        tenant_id, workflow_id, enforced_since=enforced_since
    )
    if policy != FAIL_WORKFLOW:
        return None
    failed: list[Task] = []
    for state in _WORKFLOW_FAILING_STATES:
        failed.extend(
            store.workflow_steps_in_state(tenant_id, workflow_id, state, _FAILED_STEPS_NAMED)
        )
    if not failed:
        return None
    failed.sort(key=lambda step: (step.step_id or "", step.id))
    return FailedWorkflow(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        failed_steps=tuple(
            {"step_id": step.step_id, "task_id": step.id, "state": step.state.value}
            for step in failed[:_FAILED_STEPS_NAMED]
        ),
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
    #: Workflows this drain found failed under `on_step_failure: fail_workflow`
    #: and swept. Their cancels are in `cancelled` like every other cancel.
    #: This field says how many workflows those cancels came from.
    failed_workflows_swept: int = 0
    #: Transitions this drain decided and did NOT write, because the task had
    #: moved on since it was read -- a cancel, another scheduler, the reconciler
    #: or the worker got there first. Each one used to be a blind overwrite
    #: (incident wf_ebb3ab2d65664707a559, F-9). The breakdown by write and
    #: reason is `swarm_scheduler_stale_writes_total{write, reason}`.
    stale_writes: int = 0
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
        pool: AccountPool | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._router = router
        self._metrics = metrics or SchedulerMetrics()
        # The account pool admission and the credential sweep ask about
        # (credentials.py). `main.build_scheduler` hands the SAME instance to
        # the Cloud Run dispatcher, so the Job's secret mount is decided on the
        # list admission read, and `drain()` forgets it at the top of each run.
        self._pool = pool if pool is not None else AccountPool.for_deployment(
            settings, store.db
        )
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
        # Per drain as well, keyed by (tenant_id, workflow_id). A verdict is at
        # most one drain old, except that a failure the drain itself writes
        # evicts it at once (see the DispatchError branch of `_admit_one`). A
        # failure a WORKER or the reconciler writes mid-drain is not seen until
        # the next drain, so a sibling admitted in between starts, holds
        # capacity and runs to completion (docs/workflows.md says so).
        self._workflow_verdicts: dict[tuple[str, str], FailedWorkflow | None] = {}
        self._swept_workflows: set[tuple[str, str]] = set()
        cutoff = settings.on_step_failure_enforced_since
        log.info(
            "on_step_failure: fail_workflow applies to %s",
            "every workflow, whenever it was submitted"
            if cutoff is None
            else f"workflows created at or after {cutoff.isoformat()} "
            "(ON_STEP_FAILURE_ENFORCED_SINCE); older ones keep the dependency rule",
        )
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
        self._workflow_verdicts = {}
        self._swept_workflows = set()
        # A loan made or withdrawn since the last drain is seen by this one.
        self._pool.forget()

        if self._store.dispatch_paused():
            self._metrics.paused.set(1)
            report.stop_reason = "dispatch_paused"
            report.duration_seconds = self._monotonic() - started
            self._metrics.runs.labels(stop_reason=report.stop_reason).inc()
            self._metrics.run_seconds.observe(report.duration_seconds)
            log.info("drain skipped: dispatch is paused by an admin")
            return report
        self._metrics.paused.set(0)

        report.promoted_dependencies = self._promote_dependencies(report)
        report.promoted_credentials = self._promote_credentials(report)
        if self._settings.enable_prewarm:
            report.promoted_prewarm = self._prewarm(report)

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
            self._cancel(
                task,
                "cancellation requested before admission",
                report,
                why="cancel_requested",
                end_cause=EndCause.CANCEL_REQUESTED,
            )
            return False

        # THE ADMISSION GATE for `on_step_failure: fail_workflow`. Every step
        # that starts passes through here, and an independent READY sibling of
        # a failed step shares no dependency edge with it, so no dependency
        # check would ever stop it. It sees failures as of this drain's verdict
        # (see `_stop_for_failed_workflow` for the window that leaves).
        if self._stop_for_failed_workflow(task, report):
            return False

        if task.next_eligible_at is not None and task.next_eligible_at > now:
            report.skipped += 1
            return False

        profile = RUNNER_PROFILES.get(task.runner_profile)
        if profile is None:
            # The catalogue no longer has this profile. Parking rather than
            # failing keeps the work recoverable if an admin restores it.
            self._park(
                task,
                ParkReason.MANUAL_PAUSE,
                report,
                detail={"error": f"runner_profile {task.runner_profile!r} is not in the catalogue"},
            )
            return False

        if task.depends_on:
            parents = self._store.parent_ends(task.depends_on)
            states = {tid: end.state for tid, end in parents.items()}
            failed = [tid for tid, state in states.items() if state in _FAILED_PARENT_STATES]
            if failed:
                self._cancel(
                    task,
                    "an upstream workflow step did not succeed",
                    report,
                    why="failed_parent",
                    detail={"failed_parents": failed},
                    end_cause=_parent_cause(parents, failed),
                )
                return False
            if any(states.get(tid) is not TaskState.SUCCEEDED for tid in task.depends_on):
                self._park(
                    task,
                    ParkReason.DEPENDENCY_INCOMPLETE,
                    report,
                    detail={
                        "waiting_on": [
                            tid
                            for tid in task.depends_on
                            if states.get(tid) is not TaskState.SUCCEEDED
                        ]
                    },
                )
                return False

        tenant = self._tenant(task.tenant_id)
        if tenant is None or not tenant.enabled:
            self._park(
                task,
                ParkReason.MANUAL_PAUSE,
                report,
                detail={"error": "tenant is missing or disabled"},
            )
            return False

        # ONE QUESTION, asked here, in the credential sweep and by the Cloud
        # Run Job's secret mount: can this tenant run this profile, on a key of
        # its own or on a pool account it may use (credentials.py, #169). No
        # means a container that can only park or fail, holding a slot while
        # it does, so it parks here, before any lease. The detail names what
        # the account pool answered, so the park says what would clear it.
        credential = credential_for(profile, tenant, self._pool)
        if not credential.runnable:
            self._park(
                task,
                ParkReason.CREDENTIAL_MISSING,
                report,
                detail=credential.park_detail(),
            )
            return False

        backend = resolve_backend(profile)
        units = RESOURCE_CLASSES[task.resource_class].units

        try:
            lease = self._store.acquire_lease(
                task, units=units, backend=backend.value, config=self._admission
            )
        except AdmissionDenied as denied:
            recorded = self._store.record_blockers(task, denied.reasons)
            if not recorded.applied:
                # A `not_ready` denial: another scheduler admitted it first.
                self._count_stale(recorded, report)
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
            returned = self._store.return_to_ready_after_failed_dispatch(
                task, lease, exc.code, correlation_id=lease.attempt_id
            )
            if not returned.applied:
                self._count_stale(returned, report)
            elif returned.target == TaskState.FAILED.value and task.workflow_id:
                # This drain just wrote FAILED on a workflow step. Its siblings
                # may be next in this very pass, and the verdict cached for the
                # workflow was read before the failure existed.
                self._workflow_verdicts.pop((task.tenant_id, task.workflow_id), None)
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

        marked = self._store.mark_dispatched(task, lease, execution, backend.value)
        if not marked.applied:
            # Usually its worker started first and already walked the task past
            # DISPATCHED. The dispatch itself succeeded -- the execution exists
            # -- so it is still counted below; only the state write was not made.
            self._count_stale(marked, report)
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
        `return_to_ready_after_failed_dispatch` releases the lease and returns
        the task in ONE transaction and writes the event only after it commits,
        so a failing event write never keeps the pools -- and a lease whose task
        has since moved on is still released, while one a running worker holds,
        or one the reconciler has fenced and not yet released, is not.

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
            returned = self._store.return_to_ready_after_failed_dispatch(
                task, lease, "scheduler_internal_error", correlation_id=lease.attempt_id
            )
            if not returned.applied:
                self._count_stale(returned, None)
        except Exception:
            log.exception(
                "could not return the capacity for task=%s lease=%s; the reconciler's "
                "deadline sweep is now the only thing that will reclaim it",
                task.id,
                lease.lease_id,
            )

    def _count_cancel(self, report: DrainReport, *, reason: str) -> None:
        """One place that counts a cancel, so the report and the metric agree.

        The dependency sweep below used to cancel without counting at all: the
        drain that cancelled a workflow's `synthesis` step at 07:40:11Z on
        2026-09-24 reported `cancelled: 0`. Every cancel path now calls this.
        """
        report.cancelled += 1
        self._metrics.cancelled.labels(reason=reason).inc()

    def _count_stale(self, outcome: GuardedWrite, report: DrainReport | None) -> None:
        """One place that counts a write the store refused to force.

        `report` is None only on the rollback path, which has none; the metric
        still counts it there.
        """
        if report is not None:
            report.stale_writes += 1
        self._metrics.stale_writes.labels(
            write=outcome.write, reason=outcome.reason or "unknown"
        ).inc()

    def _park(
        self,
        task: Task,
        reason: ParkReason,
        report: DrainReport,
        *,
        detail: dict[str, Any],
    ) -> None:
        outcome = self._store.park(task, reason, detail=detail)
        if outcome.applied:
            self._metrics.parked.labels(reason=reason.value).inc()
            report.parked += 1
        else:
            self._count_stale(outcome, report)

    def _cancel(
        self,
        task: Task,
        text: str,
        report: DrainReport,
        *,
        why: str,
        end_cause: EndCause | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        outcome = self._store.cancel(task, text, detail, end_cause=end_cause)
        if outcome.applied:
            self._count_cancel(report, reason=why)
        else:
            self._count_stale(outcome, report)

    def _promote(
        self, task: Task, *, kind: str, detail: dict[str, Any], report: DrainReport | None
    ) -> bool:
        """Promote one PARKED task; True only if the promotion was written."""
        outcome = self._store.promote_to_ready(task, detail=detail)
        if not outcome.applied:
            self._count_stale(outcome, report)
            return False
        self._metrics.promoted.labels(kind=kind).inc()
        return True

    # -- on_step_failure --------------------------------------------------

    def _stop_for_failed_workflow(self, task: Task, report: DrainReport) -> bool:
        """Apply `on_step_failure: fail_workflow` from a step that has not started.

        Returns True when the caller must leave `task` alone for the rest of
        this drain. That happens when its workflow has failed and has now been
        swept, which cancelled `task` along with every other step of the
        workflow that had not started.

        HOW A FAILED WORKFLOW IS FOUND. From its not-started steps, never from
        the failure. Every touch point the scheduler already has on waiting
        work calls this: the dependency sweep, the credential sweep, the
        prewarm sweep and, above all, admission, which every step passes
        through before it can start. Asking "which steps failed recently"
        instead would mean a query over FAILED tasks, which grows with history,
        re-read on every drain to rediscover workflows that were finished long
        ago. The price of doing it this way: a workflow whose every
        remaining step is PARKED on a reason the scheduler never reads
        (MANUAL_PAUSE, BUDGET_EXHAUSTED, SCHEDULED_RETRY) is swept only when one
        of those steps is promoted and reaches admission. Until then it holds no
        capacity, so it costs nothing (invariant 1). Only the derived state
        lags: it reads PARKED rather than FAILED.

        WHAT IT DOES NOT PROMISE: that nothing starts after the failure. The
        verdict is cached for the drain, so a failure a worker or the reconciler
        writes while a drain is running is not seen until the next drain, and
        no read can close the gap between reading the verdict and taking the
        lease anyway. A sibling admitted in that window holds capacity, so it
        is left to run to completion like any other running step. The window
        is the rest of the drain that was running when the failure was
        written, which `max_run_seconds` bounds. A failure the drain writes
        itself is seen at once (the DispatchError branch of `_admit_one`).
        """
        if not task.workflow_id:
            return False
        key = (task.tenant_id, task.workflow_id)
        if key in self._swept_workflows:
            # Swept earlier in this drain. `task` is a snapshot from before the
            # sweep, so acting on it could cancel twice or admit a step the
            # sweep has just cancelled.
            return True
        failed = self._workflow_failure(task)
        if failed is None:
            return False
        self._sweep_failed_workflow(failed, report)
        return True

    def _workflow_failure(self, task: Task) -> FailedWorkflow | None:
        """The workflow's failure verdict (`read_workflow_failure`), read at most once per drain."""
        workflow_id = task.workflow_id
        if not workflow_id:
            return None
        key = (task.tenant_id, workflow_id)
        if key not in self._workflow_verdicts:
            self._workflow_verdicts[key] = read_workflow_failure(
                self._store,
                task.tenant_id,
                workflow_id,
                enforced_since=self._settings.on_step_failure_enforced_since,
            )
        return self._workflow_verdicts[key]

    def _sweep_failed_workflow(self, failed: FailedWorkflow, report: DrainReport) -> None:
        """Cancel every step of `failed` that has not started, and nothing else.

        "Has not started" is SUBMITTED, QUEUED, READY or PARKED, and each
        cancel is re-checked inside a transaction
        (`SchedulerStore.cancel_if_not_started`), so a step leased by a
        concurrent drain since it was read is left to run. Steps holding
        capacity are not cancelled, not flagged with `cancel_requested` and not
        written to. They run to completion, and the workflow derives FAILED once
        they finish. Killing them would throw away work that checkpointing
        exists to keep, and releasing their capacity from here would decrement
        pools that a live container still occupies (invariant 1).

        Each query is bounded by `dependency_sweep_size`. A workflow with more
        not-started steps than that in one state is finished by the next drain,
        because the failed step is still FAILED and the verdict is re-read.
        """
        key = (failed.tenant_id, failed.workflow_id)
        self._swept_workflows.add(key)
        report.failed_workflows_swept += 1

        first = failed.failed_steps[0]
        label = first["step_id"] or first["task_id"]
        reason = (
            f"workflow step {label} is {first['state']} and on_step_failure is "
            f"{FAIL_WORKFLOW}, so steps that had not started were cancelled"
        )
        detail = {
            "workflow_id": failed.workflow_id,
            "on_step_failure": FAIL_WORKFLOW,
            "failed_steps": [dict(step) for step in failed.failed_steps],
        }

        cancelled = 0
        for state in _NOT_STARTED_STATES:
            for step in self._store.workflow_steps_in_state(
                failed.tenant_id,
                failed.workflow_id,
                state,
                self._settings.dependency_sweep_size,
            ):
                outcome = self._store.cancel_if_not_started(
                    step, reason, detail, end_cause=EndCause.WORKFLOW_SWEEP
                )
                if outcome.applied:
                    self._count_cancel(report, reason="workflow_failed")
                    cancelled += 1
                else:
                    # Leased by a concurrent drain, or finished, since the
                    # query read it: left alone, and counted like every other
                    # write the store refused to force.
                    self._count_stale(outcome, report)
        log.info(
            "workflow %s failed at %s (%s); on_step_failure=%s cancelled %d step(s) "
            "that had not started; steps holding capacity were left to finish",
            failed.workflow_id,
            label,
            first["state"],
            FAIL_WORKFLOW,
            cancelled,
        )

    # -- promotion sweeps -------------------------------------------------

    def _promote_dependencies(self, report: DrainReport) -> int:
        """Return DEPENDENCY_INCOMPLETE tasks to READY once every parent SUCCEEDED.

        This is the "returns to READY when the last parent succeeds" half of
        dependency resolution. It runs at the top of the drain rather than being
        triggered by the succeeding worker, so a worker that dies immediately
        after writing SUCCEEDED cannot strand its children.

        A dependent whose parent did NOT succeed is cancelled here, and counted
        in `report.cancelled` (and `swarm_scheduler_cancelled_total`) exactly as
        `_admit_one` counts the same cancel. Returns the number PROMOTED -- a
        promotion the store refused to force (the task had moved on since this
        sweep listed it) is counted in `report.stale_writes` instead.

        `on_step_failure` is read here first. Under `fail_workflow` a FAILED or
        DEAD_LETTERED step anywhere in the workflow cancels every step that has
        not started, whether or not it depends on the failure
        (`_stop_for_failed_workflow`). Under `continue`, and for a CANCELLED
        parent under either setting, only the dependency rule below applies. It
        is transitive: a cancelled child is itself a "failed parent" to its own
        children, on this drain or the next.
        """
        promoted = 0
        for task in self._store.parked_tasks(
            ParkReason.DEPENDENCY_INCOMPLETE, self._settings.dependency_sweep_size
        ):
            if self._stop_for_failed_workflow(task, report):
                continue
            if not task.depends_on:
                # Counted in the metric as well as the report. Incrementing only
                # the report here made `swarm_scheduler_promoted{kind=
                # "dependency"}` disagree with DrainReport.promoted_dependencies,
                # so a dashboard built on the metric under-counted promotions.
                if self._promote(
                    task, kind="dependency", detail={"reason": "no_dependencies"}, report=report
                ):
                    promoted += 1
                continue
            parents = self._store.parent_ends(task.depends_on)
            states = {tid: end.state for tid, end in parents.items()}
            failed = [tid for tid, state in states.items() if state in _FAILED_PARENT_STATES]
            if failed:
                self._cancel(
                    task,
                    "an upstream workflow step did not succeed",
                    report,
                    why="failed_parent",
                    detail={"failed_parents": failed},
                    end_cause=_parent_cause(parents, failed),
                )
                continue
            if all(states.get(tid) is TaskState.SUCCEEDED for tid in task.depends_on):
                if self._promote(
                    task,
                    kind="dependency",
                    detail={"reason": "dependencies_satisfied"},
                    report=report,
                ):
                    promoted += 1
        return promoted

    def _promote_credentials(self, report: DrainReport) -> int:
        """Re-ready tasks that admission would now let run.

        The same question admission asks (`credential_for`): the tenant has
        since registered the missing key, or has since been lent -- or has
        registered -- a pool account that serves the profile. Asked in any
        other words, a task this sweep promotes could be parked again by
        admission a moment later, or one admission would run could wait here
        for a key nobody needs to register.

        A step of a failed `fail_workflow` workflow is cancelled here instead,
        which can happen long before its key is registered. `report` is
        required, not optional: that cancel, and any promotion the store
        refused to force, is counted in it.

        A POOL ANSWER WAITS FOR THE PARK'S OWN INSTANT. "Could this tenant run
        on paper" is not what a WORKER's CREDENTIAL_MISSING park is about. A
        worker that could not reach the broker falls back to the tenant's key,
        finds none, and parks for an hour (`lifecycle._park_credential_missing`);
        one the broker refused, or that could read no account, parks on
        `_park_no_account`'s fallback. The accounts in Firestore still say the
        pool serves the tenant, so promoting on that answer -- which also
        clears `next_eligible_at` -- started the task again on every drain, a
        container that could only park, for as long as the cause lasted. So
        when the answer is ACCOUNT_POOL and the park carries an instant still
        ahead, the task waits for it. Admission's own parks set no instant and
        are unaffected. A key registered meanwhile makes the answer TENANT_KEY,
        which promotes at once: the wait never delays a fix an admin made.
        """
        promoted = 0
        now = self._now()
        for task in self._store.parked_tasks(
            ParkReason.CREDENTIAL_MISSING, self._settings.dependency_sweep_size
        ):
            if self._stop_for_failed_workflow(task, report):
                continue
            tenant = self._tenant(task.tenant_id)
            if tenant is None or not tenant.enabled:
                continue
            profile = RUNNER_PROFILES.get(task.runner_profile)
            if profile is None:
                continue
            credential = credential_for(profile, tenant, self._pool)
            if (
                credential.source is CredentialSource.ACCOUNT_POOL
                and task.next_eligible_at is not None
                and task.next_eligible_at > now
            ):
                continue
            if credential.runnable:
                if self._promote(
                    task,
                    kind="credential",
                    detail=credential.promote_detail(),
                    report=report,
                ):
                    promoted += 1
        return promoted

    def _prewarm(self, report: DrainReport) -> int:
        """Promote quota-parked work just before its window reopens.

        BOUNDED by `prewarm_max_agents`, and safe by construction: the only
        tasks promoted EARLY are ones with a `provider:{p}:tenant:{t}` pool,
        whose `quota_derived_limit` holds admission shut, so if the estimate is
        wrong admission simply refuses and the task stays READY -- which costs
        nothing (invariant 1). No container is started ahead of time; that would
        be spending money on a guess.

        A step of a failed `fail_workflow` workflow met here is cancelled rather
        than promoted.

        THIS IS ALSO WHERE THESE PARKS END, not only where they end early.
        Nothing else in the platform returns a PROVIDER_QUOTA_EXHAUSTED,
        PROVIDER_COOLDOWN or PROVIDER_OUTAGE park to READY: the reconciler has
        no rule for parked tasks. So the pool guard below applies to EARLY
        promotion only. A task whose `next_eligible_at` has passed is promoted
        whether or not the guard pool exists, because by then there is no
        window left to promote it ahead of. Before this, a task with no
        `provider:{p}:tenant:{t}` pool stayed PARKED for ever. Terraform
        creates that pool only from a tenant's declared `providers`, and a
        tenant that runs on a lent pool account declares none, so the worker's
        wait on a spent, unobserved or paused account
        (`lifecycle._park_no_account`) never ended (#171 review). A park with
        no instant at all is still promoted only under a guard: without one
        there is nothing to say the window has reopened.

        NOT FIXED HERE: because the end of the wait lives in this sweep,
        `enable_prewarm=False` or a zero `prewarm_max_agents` stops these parks
        ending at all, not only ending early. docs/quota-management.md section
        5 says so.
        """
        budget = max(0, self._settings.core.prewarm_max_agents)
        if budget == 0:
            return 0
        now = self._now()
        horizon = now + timedelta(seconds=self._settings.core.prewarm_lead_seconds)
        # Read once: the guard below needs to know which provider pools exist.
        pools = self._store.pools()
        promoted = 0
        for reason in PREWARM_REASONS:
            if promoted >= budget:
                break
            for task in self._store.parked_tasks(reason, self._settings.dependency_sweep_size):
                if promoted >= budget:
                    break
                if self._stop_for_failed_workflow(task, report):
                    continue
                eligible_at = task.next_eligible_at
                if eligible_at is not None and eligible_at > horizon:
                    continue
                due = eligible_at is not None and eligible_at <= now
                guard = f"provider:{task.provider}:tenant:{task.tenant_id}"
                if not due and (not task.provider or guard not in pools):
                    # Without that pool there is nothing carrying the provider's
                    # quota cap, so promoting EARLY would let the task be
                    # admitted before the window reopens -- a container started
                    # on a guess. It waits for its own instant instead.
                    continue
                if self._promote(
                    task,
                    kind="prewarm",
                    detail={"reason": "window_reopened" if due else "prewarm",
                            "park_reason": reason.value,
                            "was_eligible_at": eligible_at.isoformat() if eligible_at else None},
                    report=report,
                ):
                    promoted += 1
        return promoted
