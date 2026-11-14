"""A GKE pod that is never scheduled is reclaimed naming why, and its attempt is not counted (#3531).

WHAT WAS WRONG. On 2026-11-06 two tasks' GKE pods sat with `PodScheduled`
False for the whole 480 s dispatch deadline, three attempts each. Each lease
never heartbeated, so `detect_stale_leases` reclaimed it by its dispatch
deadline alone, with the reason "lease silent for 485s (grace 90s,
expired=True, dispatch_overdue=True)" and nothing about the pod; each reclaim
spent one of `max_attempts`, and both tasks ended FAILED lost_worker.

WHAT EACH TEST PINS:

  * the reclaim's reason -- on the task's `last_error`, the attempt's end and
    the `generation_fenced` event -- carries the start problem recorded for
    the attempt's pod: the scheduler's reason and message;
  * a never-heartbeated DISPATCHED lease reclaimed past its dispatch deadline
    with `attempt_count == max_attempts` goes back to READY, its attempt not
    counted, by the bounded never-started requeue main already has (part of
    #2688); before that it ended FAILED lost_worker;
  * the reclaim is unchanged otherwise: the Job is killed, the generation is
    bumped, the lease released and every pool unit returned (invariants 1-3
    and 5);
  * an earlier attempt's record names nothing.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore;
only the backend is scripted.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner
from worker_seeds import TENANT, seed_tenant

from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler
from reconciler.startproblem import START_PROBLEM_KEY
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import EventType, TaskState

GKE = "GKE_AUTOPILOT"
NAMESPACE = "swarm-tenant-eng"
TASK, LEASE, ATTEMPT = "task_unsched000000000001", "lease_unsched", "att_unsched"
JOB_NAME = "swarm-unsched000000000001-3"
POOLS = pool_names_for(
    tenant_id=TENANT,
    provider="anthropic",
    resource_class="large",
    runner_profile="claude-code",
    backend=GKE,
)
SCHEDULER_SAYS = "0/3 nodes are available: 3 node(s) had volume node affinity conflict"


def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="example-project",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def unscheduled_record(attempt_id: str = ATTEMPT) -> dict[str, Any]:
    """What `_watch_starts` wrote on the task on an earlier pass."""
    return {
        "container": "", "reason": "Unschedulable", "restarts": 0,
        "message": SCHEDULER_SAYS, "pod_started": False,
        "attempt_id": attempt_id, "seen_at": utcnow() - timedelta(minutes=5),
    }


def seed_never_scheduled(
    db: FakeFirestore, *, attempt_count: int, record: dict[str, Any] | None
) -> None:
    """A task DISPATCHED 500 s ago whose lease never heartbeated: past its 480 s deadline."""
    now = utcnow()
    admitted = now - timedelta(seconds=500)
    db.seed(
        f"tasks/{TASK}",
        {
            "id": TASK, "tenant_id": TENANT, "state": TaskState.DISPATCHED.value,
            "runner_profile": "claude-code", "resource_class": "large",
            "current_generation": 3, "current_lease_id": LEASE,
            "attempt_count": attempt_count, "max_attempts": 3,
            "updated_at": admitted, "cancel_requested": False, "started_at": None,
            "last_error": None,
            "metadata": {START_PROBLEM_KEY: record} if record is not None else {},
        },
    )
    db.seed(
        f"leases/{LEASE}",
        {
            "lease_id": LEASE, "task_id": TASK, "attempt_id": ATTEMPT,
            "tenant_id": TENANT, "generation": 3,
            "pools": POOLS, "units": 1, "state": TaskState.DISPATCHED.value,
            "created_at": admitted,
            "dispatch_deadline": admitted + timedelta(seconds=480),
            "expires_at": admitted + timedelta(seconds=120),
            "heartbeat_at": None,
            "released_at": None,
        },
    )
    db.seed(
        f"attempts/{ATTEMPT}",
        {
            "attempt_id": ATTEMPT, "task_id": TASK, "tenant_id": TENANT,
            "generation": 3, "lease_id": LEASE, "backend": GKE,
            "created_at": admitted, "execution_name": f"{NAMESPACE}/{JOB_NAME}",
        },
    )
    for pool in POOLS:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 1,
                                  "enabled": True, "updated_at": now})
    seed_tenant(db)


def unscheduled_job() -> ExecutionView:
    """The Job the dispatcher created: active, its one pod never placed."""
    return ExecutionView(
        name=JOB_NAME,
        backend=GKE,
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(seconds=495),
        task_id=TASK,
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        generation=3,
        namespace=NAMESPACE,
    )


def reconcile(db: FakeFirestore) -> tuple[Any, FakeBackend]:
    logger = build_logger(stream=io.StringIO())
    backend = FakeBackend(name=GKE, executions=[unscheduled_job()], journal=db.writes)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(store=store, backends=[backend], config=config(), logger=logger).run_once()
    return report, backend


def test_the_reclaim_names_the_schedulers_reason(db):
    seed_never_scheduled(db, attempt_count=1, record=unscheduled_record())

    report, _ = reconcile(db)

    task = db.doc(f"tasks/{TASK}")
    # MUTATION: leave the record out of the reason and last_error reads only
    # "lease silent for 500s", as on 2026-11-06.
    assert "Unschedulable" in task["last_error"], (task["last_error"], report.as_dict())
    assert SCHEDULER_SAYS in task["last_error"], task["last_error"]
    fenced = [e for e in db.events(TASK) if e["type"] == EventType.GENERATION_FENCED.value]
    assert fenced and "Unschedulable" in fenced[-1]["detail"]["reason"], fenced
    attempt = db.doc(f"attempts/{ATTEMPT}")
    assert "Unschedulable" in str(attempt.get("error")), attempt


def test_a_never_scheduled_attempt_at_its_last_attempt_goes_back_to_ready_uncounted(db):
    seed_never_scheduled(db, attempt_count=3, record=unscheduled_record())

    report, backend = reconcile(db)

    task = db.doc(f"tasks/{TASK}")
    lease = db.doc(f"leases/{LEASE}")
    assert task["state"] == TaskState.READY.value, (task, report.as_dict())
    # The bounded refund: the attempt that never ran is not counted.
    assert task["attempt_count"] == 2, task
    assert task["metadata"]["never_started_requeues"] == 1, task["metadata"]
    # Invariant 5: the attempt's generation is superseded, and its Job killed.
    assert task["current_generation"] == 4, task
    assert backend.terminated, "the Job was not killed before the release"
    # Invariants 1-3: the lease is released and every pool's unit returned.
    assert lease["released_at"] is not None, lease
    assert task["current_lease_id"] is None
    assert {int(db.doc(f"pools/{p}")["active"]) for p in POOLS} == {0}


def test_an_earlier_attempts_record_names_nothing(db):
    seed_never_scheduled(db, attempt_count=1, record=unscheduled_record("att_earlier"))

    reconcile(db)

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.READY.value, task
    assert "Unschedulable" not in task["last_error"], task["last_error"]
