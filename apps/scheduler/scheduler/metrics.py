"""Scheduler metrics.

Private registry, same reason as the API: the global default registry collides
when two schedulers are constructed in one test process.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST


class SchedulerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.runs = Counter(
            "swarm_scheduler_runs_total",
            "Drain runs, by how the loop stopped.",
            ["stop_reason"],
            registry=self.registry,
        )
        self.run_seconds = Histogram(
            "swarm_scheduler_run_seconds",
            "Wall-clock duration of a drain run.",
            registry=self.registry,
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
        )
        self.leased = Counter(
            "swarm_scheduler_leases_total",
            "Leases acquired, by tenant and runner profile.",
            ["tenant", "runner_profile"],
            registry=self.registry,
        )
        self.denied = Counter(
            "swarm_scheduler_admission_denied_total",
            "Admission denials, by the first blocking reason.",
            ["reason"],
            registry=self.registry,
        )
        self.dispatched = Counter(
            "swarm_scheduler_dispatched_total",
            "Successful dispatches, by backend.",
            ["backend"],
            registry=self.registry,
        )
        self.dispatch_failures = Counter(
            "swarm_scheduler_dispatch_failures_total",
            "Dispatches that failed after a lease was taken, by backend.",
            ["backend"],
            registry=self.registry,
        )
        self.admission_rollbacks = Counter(
            "swarm_scheduler_admission_rollbacks_total",
            "Leases returned because the work AFTER a committed admission raised "
            "something other than a DispatchError -- a credential refresh, a "
            "Firestore write. Unlabelled on purpose: the cause is in the log "
            "line, and any value above zero is worth reading it for.",
            registry=self.registry,
        )
        self.parked = Counter(
            "swarm_scheduler_parked_total",
            "Tasks parked during a drain, by park reason.",
            ["reason"],
            registry=self.registry,
        )
        self.promoted = Counter(
            "swarm_scheduler_promoted_total",
            "PARKED tasks returned to READY, by why.",
            ["kind"],
            registry=self.registry,
        )
        #: Every cancel a drain makes, on every path, and the same number
        #: `DrainReport.cancelled` carries. There was no cancel counter at all
        #: until 2026-09-24, and the report missed the dependency sweep's
        #: cancels: the drain that cancelled a workflow's `synthesis` step at
        #: 07:40:11Z (incident wf_ebb3ab2d65664707a559) reported `cancelled: 0`.
        #: `reason` is `cancel_requested` (the caller asked, before admission)
        #: or `failed_parent` (an upstream step FAILED, was CANCELLED or was
        #: DEAD_LETTERED). The scheduler only ever cancels work that holds no
        #: capacity, so none of these released a lease.
        self.cancelled = Counter(
            "swarm_scheduler_cancelled_total",
            "Tasks the scheduler cancelled during a drain, by why. Only READY or "
            "PARKED work is cancelled here, so no capacity is involved.",
            ["reason"],
            registry=self.registry,
        )
        #: Transitions a drain decided and did NOT write, because the task had
        #: moved on since the drain read it. Every one of these was a blind
        #: overwrite until 2026-09-24 (incident wf_ebb3ab2d65664707a559, F-9):
        #: a user's cancel parked or promoted back into the queue, another
        #: scheduler's lease cancelled or promoted over, a RUNNING worker rewound
        #: to DISPATCHED, a re-leased task returned to READY with its new lease
        #: orphaned. `write` is park / promote / cancel / record_blockers /
        #: mark_dispatched / return_to_ready; `reason` is one of the stable codes
        #: in scheduler/store.py (state_changed, park_reason_changed,
        #: lease_superseded, task_missing). A steady trickle of
        #: `mark_dispatched{state_changed}` is workers starting before the
        #: scheduler's write lands and is benign; `return_to_ready` and
        #: `lease_superseded` are dispatches that outlived their attempt and are
        #: worth reading the WARNING line for.
        self.stale_writes = Counter(
            "swarm_scheduler_stale_writes_total",
            "Transitions the scheduler did not write because the task had changed "
            "since the drain read it, by write and reason.",
            ["write", "reason"],
            registry=self.registry,
        )
        self.candidates = Histogram(
            "swarm_scheduler_candidates_per_pass",
            "READY tasks examined per pass.",
            registry=self.registry,
            buckets=(0, 1, 5, 10, 25, 50, 100, 200, 500),
        )
        self.topped_up = Counter(
            "swarm_scheduler_tenants_topped_up_total",
            "Tenants added to the rotation because a saturated candidate slice "
            "had left them out entirely. A rising value means someone is "
            "flooding the queue with high-priority work.",
            registry=self.registry,
        )
        self.tenants_in_rotation = Gauge(
            "swarm_scheduler_tenants_in_rotation",
            "Distinct tenants in the last round-robin rotation.",
            registry=self.registry,
        )
        self.paused = Gauge(
            "swarm_scheduler_dispatch_paused",
            "1 when an admin has paused dispatch.",
            registry=self.registry,
        )

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
