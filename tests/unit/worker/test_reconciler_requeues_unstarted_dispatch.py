"""A dispatched task whose worker never started is re-queued, not failed (part of #2688).

WHAT WAS WRONG. On 2026-11-06, about 07:40Z, a Product Oracle task sat
DISPATCHED and ended FAILED with last_error "reconciled: lease silent for 485s
(grace 90s, expired=True, dispatch_overdue=True)": no attempt, no cost, no
artifacts. The platform was at its API and pool limits, four other tasks sat
DISPATCHED for over ten minutes at the same time, and the reconciler's reclaim
of the silent lease counted an attempt that never ran against `max_attempts`
exactly as it counts one whose agent ran and died.

WHAT EACH TEST PINS:

  * a silent DISPATCHED task with no attempt that reached STARTING goes back
    to READY: its lease and its pool units released, its generation bumped,
    the attempt not counted, a re-queue count on the task and an event saying
    it never started;
  * the fourth time in a row it FAILS, saying it never started;
  * a task whose attempt reached RUNNING is handled as before: requeued with
    its attempt counted, and FAILED as a lost worker once attempts are spent;
  * a stale worker of the superseded generation that boots after the requeue
    exits without running the agent and without touching the lease.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore;
only the backend is scripted.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

import pytest
from fakes import ExplodingChildProcess, FakeBackend, FakeFirestore, FakeTransactionRunner
from worker_seeds import TENANT, seed_attempt

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from reconciler.config import NEVER_STARTED_REQUEUE_LIMIT_MAX, ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView, count_never_started
from reconciler.repair import Reconciler
from reconciler.store import ControlStore, RepairPlan
from swarm_common.models import utcnow
from swarm_common.states import EventType, TaskState

#: Matches the setting's default, so the fourth silence is the one that fails.
LIMIT = 3
#: Where the task records the count (`reconciler.model.NEVER_STARTED_REQUEUES_KEY`),
#: spelled out so the test reads the document, not the code under test.
NEVER_STARTED_REQUEUES_KEY = "never_started_requeues"


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def execution_name(attempt_id: str) -> str:
    return f"projects/p/locations/us-central1/jobs/swarm-{TENANT}-mock/executions/{attempt_id}"


def seed_silent_dispatch(
    db: FakeFirestore,
    *,
    attempt_id: str = "att_1",
    lease_id: str = "lease_1",
    generation: int = 1,
    attempt_count: int = 1,
    state: TaskState = TaskState.DISPATCHED,
    metadata: dict[str, Any] | None = None,
) -> None:
    """A task admitted 485 s ago whose worker never wrote a heartbeat."""
    seed_attempt(
        db,
        attempt_id=attempt_id,
        lease_id=lease_id,
        generation=generation,
        state=state,
        attempt_count=attempt_count,
    )
    now = utcnow()
    admitted = now - timedelta(seconds=485)
    db.document(f"leases/{lease_id}").update(
        {
            "created_at": admitted,
            "dispatch_deadline": admitted + timedelta(seconds=300),
            "expires_at": admitted + timedelta(seconds=120),
        }
    )
    if metadata is not None:
        db.document("tasks/task_1").update({"metadata": dict(metadata)})
    db.seed(
        f"attempts/{attempt_id}",
        {
            "attempt_id": attempt_id, "task_id": "task_1", "tenant_id": TENANT,
            "generation": generation, "lease_id": lease_id, "backend": "CLOUD_RUN_JOB",
            "created_at": admitted, "execution_name": execution_name(attempt_id),
        },
    )


def seed_silent_run(db: FakeFirestore, *, attempt_count: int = 1) -> None:
    """The same task, but its attempt reached RUNNING and then went silent."""
    seed_silent_dispatch(db, state=TaskState.RUNNING, attempt_count=attempt_count)
    now = utcnow()
    db.document("leases/lease_1").update({"heartbeat_at": now - timedelta(seconds=400)})
    db.document("tasks/task_1").update({"started_at": now - timedelta(seconds=450)})
    db.document("attempts/att_1").update({"started_at": now - timedelta(seconds=450)})


def scheduled_execution(attempt_id: str = "att_1", generation: int = 1) -> ExecutionView:
    """The Cloud Run execution the dispatcher created: accepted, never booted a worker."""
    return ExecutionView(
        name=execution_name(attempt_id),
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(seconds=480),
        task_id="task_1",
        attempt_id=attempt_id,
        tenant_id=TENANT,
        generation=generation,
        parent=f"projects/p/locations/us-central1/jobs/swarm-{TENANT}-mock",
    )


def reconcile(db: FakeFirestore, config: ReconcilerConfig, execution: ExecutionView) -> Any:
    logger = build_logger(stream=io.StringIO())
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    backend = FakeBackend(executions=[execution], journal=db.writes)
    return Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()


def pools_active(db: FakeFirestore) -> list[int]:
    lease = db.doc("leases/lease_1")
    return [int(db.doc(f"pools/{pool}")["active"]) for pool in lease["pools"]]


def events_of(db: FakeFirestore, event_type: EventType) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1") if e["type"] == event_type.value]


def test_a_silent_dispatch_with_no_started_attempt_goes_back_to_ready(db, config):
    seed_silent_dispatch(db)
    assert pools_active(db) and set(pools_active(db)) == {1}

    report = reconcile(db, config, scheduled_execution())

    task = db.doc("tasks/task_1")
    lease = db.doc("leases/lease_1")
    assert task["state"] == TaskState.READY.value, (task, report.as_dict())
    # Invariant 5: the attempt's generation is superseded.
    assert task["current_generation"] == 2, task
    # Invariants 1-3: the lease is released and every pool's unit returned.
    assert lease["released_at"] is not None, lease
    assert set(pools_active(db)) == {0}, pools_active(db)
    assert task["current_lease_id"] is None
    # MUTATION: count the attempt and three such silences fail the task on
    # `max_attempts` before the bound below is ever reached.
    assert task["attempt_count"] == 0, task
    assert task["metadata"][NEVER_STARTED_REQUEUES_KEY] == 1, task
    assert "never started" in task["last_error"], task["last_error"]
    ready = events_of(db, EventType.READY)
    assert ready, db.event_types("task_1")
    detail = ready[-1]["detail"]
    assert detail["never_started"] is True, detail
    assert detail["never_started_requeues"] == 1, detail
    assert detail["never_started_requeue_limit"] == LIMIT, detail
    assert "never started" in detail["why"], detail


def test_the_fourth_silence_fails_the_task_saying_it_never_started(db, config):
    generation = 1
    metadata: dict[str, Any] = {}
    attempt_count = 1
    for n in range(1, LIMIT + 2):
        attempt = f"att_{n}"
        seed_silent_dispatch(
            db,
            attempt_id=attempt,
            generation=generation,
            attempt_count=attempt_count,
            metadata=metadata,
        )
        reconcile(db, config, scheduled_execution(attempt, generation))
        task = db.doc("tasks/task_1")
        if n <= LIMIT:
            assert task["state"] == TaskState.READY.value, (n, task)
            assert task["metadata"][NEVER_STARTED_REQUEUES_KEY] == n, (n, task)
        generation = int(task["current_generation"])
        metadata = dict(task.get("metadata") or {})
        # What admission writes on the next lease: one more attempt.
        attempt_count = int(task["attempt_count"]) + 1

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task
    assert task["last_error"].startswith("never started"), task["last_error"]
    assert task["end_cause"] == "lost_worker", task
    assert set(pools_active(db)) == {0}, pools_active(db)
    failed = events_of(db, EventType.FAILED)
    assert failed and failed[-1]["detail"]["never_started"] is True, failed
    assert failed[-1]["detail"]["never_started_requeues"] == LIMIT, failed


def test_a_task_whose_attempt_was_running_is_requeued_and_counted_as_before(db, config):
    seed_silent_run(db)

    reconcile(db, config, scheduled_execution())

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task
    assert task["current_generation"] == 2
    assert task["attempt_count"] == 1, task
    assert NEVER_STARTED_REQUEUES_KEY not in (task.get("metadata") or {}), task
    assert "never started" not in task["last_error"], task["last_error"]
    assert "never_started" not in events_of(db, EventType.READY)[-1]["detail"]


def test_a_running_attempt_with_attempts_spent_fails_as_a_lost_worker_as_before(db, config):
    seed_silent_run(db, attempt_count=3)

    reconcile(db, config, scheduled_execution())

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task
    assert task["end_cause"] == "lost_worker", task
    assert task["last_error"].startswith("reconciled: lease silent"), task["last_error"]


def test_a_stale_worker_of_the_old_generation_exits_without_running(
    db, config, worker_factory, monkeypatch
):
    seed_silent_dispatch(db)
    reconcile(db, config, scheduled_execution())
    assert db.doc("tasks/task_1")["current_generation"] == 2
    released = db.doc("leases/lease_1")["released_at"]
    pools = pools_active(db)

    # The container the backend accepted finally boots, for generation 1.
    worker, _, _ = worker_factory(generation=1)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    assert worker.run() == ExitCode.GENERATION_FENCED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task
    assert task["current_generation"] == 2
    assert db.doc("leases/lease_1")["released_at"] == released
    assert pools_active(db) == pools


def test_a_worker_that_started_after_the_snapshot_is_judged_as_before(db):
    """The store re-reads the state inside the transaction: STARTING is a start."""
    seed_silent_dispatch(db)
    db.document("tasks/task_1").update({"state": TaskState.STARTING.value})
    store = ControlStore(
        db, logger=build_logger(stream=io.StringIO()), txn_runner=FakeTransactionRunner(db)
    )

    done = store.fence_release_repair(
        "task_1",
        expected_generation=1,
        lease_id="lease_1",
        release_reason="reconciler:stale_lease",
        repair=RepairPlan(
            to_state=TaskState.READY,
            expected_lease_id="lease_1",
            error="reconciled: lease silent",
            never_started_requeue_limit=LIMIT,
        ),
    )

    task = db.doc("tasks/task_1")
    assert done.repaired_to is TaskState.READY and done.never_started_requeues is None, done
    assert task["attempt_count"] == 1, task
    assert NEVER_STARTED_REQUEUES_KEY not in (task.get("metadata") or {}), task


@pytest.mark.parametrize(
    ("used", "limit", "requeues", "exhausted"),
    [(0, 3, 1, False), (2, 3, 3, False), (3, 3, 3, True), (0, 0, 0, True)],
)
def test_the_count_requeues_up_to_its_bound_then_fails(used, limit, requeues, exhausted):
    counted = count_never_started(1, 3, used, limit)
    assert (counted.requeues, counted.exhausted) == (requeues, exhausted), counted
    assert counted.attempt_count == (1 if exhausted else 0), counted
    assert ("never started" in counted.words) and (
        counted.words.startswith("never started") is exhausted
    ), counted


def test_the_setting_defaults_to_three_and_is_bounded(monkeypatch):
    assert ReconcilerConfig(
        project_id="p", region="r", firestore_database="d"
    ).never_started_requeue_limit == LIMIT
    monkeypatch.setenv("PROJECT_ID", "swarm-unit-test")
    monkeypatch.setenv("NEVER_STARTED_REQUEUE_LIMIT", "1000")
    assert ReconcilerConfig.from_env().never_started_requeue_limit == (
        NEVER_STARTED_REQUEUE_LIMIT_MAX
    )
