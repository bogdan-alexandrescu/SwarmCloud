"""An attempt that ended before its runner started is not counted, three times (#67).

THE DECISION. Owner, 2026-09-28: an execution that exits before its worker
reaches its runner -- a 143 SIGTERM from the platform, or any exit but 78
while the task is still DISPATCHED or STARTING, which is what
`detect_ended_at_startup` finds as WORKER_ENDED_AT_STARTUP -- did no work, and
must not use up one of the task's `max_attempts`. The reconciler requeues it in
the same transaction that repairs the task, taking the attempt back
(`attempt_count` - 1, never below 0) and recording the refund in
`Task.metadata['startup_refunds']`. The frozen `Task` is not changed: its
`metadata` is free-form.

THE BOUND. Three refunds. After them an early end counts as it always did, so
a task killed at every start still reaches FAILED: three refunded ends, then
`max_attempts` counted ones.

WHAT IT MUST NOT DO. Refund an attempt whose runner started (the task reached
RUNNING: the heartbeat rules own it and it did work), refund a stale
generation (invariant 5: nothing about an old attempt may change the task),
or change how the lease is released (invariant 2: the frozen
`release_lease_in_transaction`, exactly once).

These run the production `Reconciler`, `ControlStore` (with its production
transaction runner) and `CloudRunBackend`, over fake `run_v2` clients that
answer the way Cloud Run answered for an execution that exited early.
"""

from __future__ import annotations

import io
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from reconciler.backends import CloudRunBackend
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import TaskState

from .conftest import seed_tenant
from .fakes import FakeFirestore

PROJECT = "saga-agents-staging"
REGION = "us-central1"
TENANT = "eng"
JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-job-eng-mock"
SHORT = "swarm-job-eng-mock-r3fnd"
EXECUTION = f"{JOB}/executions/{SHORT}"
TASK, LEASE, ATTEMPT = "task_refund0000000000001", "lease_refund_1", "att_refund0000000000001"
POOLS = pool_names_for(
    tenant_id=TENANT,
    provider=None,
    resource_class="standard",
    runner_profile="mock",
    backend="CLOUD_RUN_JOB",
)

#: The exits, restated: they are the contract between the worker's process and
#: whoever reads the execution's exit status.
SIGTERM = 143
UNAVAILABLE = 69
CANNOT_START = 78

KIND = "worker_ended_at_startup"


# ---------------------------------------------------------------------------
# Cloud Run, as `run_v2` answers
# ---------------------------------------------------------------------------


class _Jobs:
    def list_jobs(self, parent: str) -> list[Any]:
        return [
            SimpleNamespace(
                name=JOB,
                labels={
                    "managed-by": "swarm-terraform",
                    "swarm-tenant": TENANT,
                    "swarm-gc-exempt": "true",
                },
                create_time=utcnow() - timedelta(days=3),
            )
        ]


class _Executions:
    def __init__(self, execution: Any) -> None:
        self.execution = execution
        self.cancelled: list[Any] = []

    def list_executions(self, parent: str) -> list[Any]:
        return [self.execution] if str(self.execution.name).startswith(f"{parent}/") else []

    def get_execution(self, name: str) -> Any:
        if self.execution.name == name:
            return self.execution
        from google.api_core import exceptions as core

        raise core.NotFound(name)

    def cancel_execution(self, request: Any = None, **_kwargs: Any) -> Any:
        self.cancelled.append(request)
        raise AssertionError("a finished execution was asked to stop")


class _Tasks:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code

    def list_tasks(self, *, parent: str) -> list[Any]:
        return [
            SimpleNamespace(
                name=f"{EXECUTION}/tasks/{SHORT}-task0",
                last_attempt_result=SimpleNamespace(
                    exit_code=self.exit_code,
                    status=SimpleNamespace(
                        code=2,
                        message=f"Task {SHORT}-task0 failed with exit code: {self.exit_code}",
                    ),
                ),
            )
        ]


def execution(*, generation: int, ended_seconds_ago: float = 50.0) -> Any:
    env = {
        "TASK_ID": TASK,
        "ATTEMPT_ID": ATTEMPT,
        "TENANT_ID": TENANT,
        "GENERATION": str(generation),
    }
    return SimpleNamespace(
        name=EXECUTION,
        labels={"managed-by": "swarm-scheduler"},
        create_time=utcnow() - timedelta(seconds=ended_seconds_ago + 40),
        running_count=0,
        cancelled_count=0,
        succeeded_count=0,
        failed_count=1,
        completion_time=utcnow() - timedelta(seconds=ended_seconds_ago),
        reconciling=False,
        template=SimpleNamespace(
            containers=[
                SimpleNamespace(env=[SimpleNamespace(name=k, value=v) for k, v in env.items()])
            ]
        ),
    )


# ---------------------------------------------------------------------------
# the control plane
# ---------------------------------------------------------------------------


def seed(
    db: FakeFirestore,
    *,
    state: TaskState = TaskState.DISPATCHED,
    generation: int = 1,
    attempt_count: int = 1,
    max_attempts: int = 3,
    metadata: dict[str, Any] | None = None,
    heartbeat_seconds_ago: float | None = None,
    age_seconds: int = 60,
) -> None:
    """A task the scheduler admitted `age_seconds` ago: lease held, attempt counted."""
    now = utcnow()
    admitted = now - timedelta(seconds=age_seconds)
    if f"tenants/{TENANT}" not in db.docs:
        seed_tenant(db, TENANT)
    task: dict[str, Any] = {
        "id": TASK, "tenant_id": TENANT, "state": state.value,
        "runner_profile": "mock", "resource_class": "standard",
        "current_generation": generation, "current_lease_id": LEASE,
        "attempt_count": attempt_count, "max_attempts": max_attempts,
        "updated_at": admitted, "cancel_requested": False, "last_error": None,
        "metadata": dict(metadata or {}),
    }
    if state is TaskState.RUNNING:
        task["started_at"] = admitted
    previous = db.docs.get(f"tasks/{TASK}")
    if previous is not None:
        task["metadata"] = dict(previous.get("metadata") or {})
    db.docs[f"tasks/{TASK}"] = task
    db.docs[f"leases/{LEASE}"] = {
        "lease_id": LEASE, "task_id": TASK, "attempt_id": ATTEMPT,
        "tenant_id": TENANT, "generation": generation,
        "pools": POOLS, "units": 1, "state": state.value,
        "created_at": admitted,
        "dispatch_deadline": admitted + timedelta(seconds=300),
        "expires_at": admitted + timedelta(seconds=120),
        "heartbeat_at": (
            now - timedelta(seconds=heartbeat_seconds_ago)
            if heartbeat_seconds_ago is not None else None
        ),
        "released_at": None,
    }
    db.docs[f"attempts/{ATTEMPT}"] = {
        "attempt_id": ATTEMPT, "task_id": TASK, "tenant_id": TENANT,
        "generation": generation, "lease_id": LEASE, "backend": "CLOUD_RUN_JOB",
        "created_at": admitted, "execution_name": EXECUTION,
    }
    for pool in POOLS:
        db.docs[f"pools/{pool}"] = {
            "name": pool, "hard_limit": 10, "active": 1, "enabled": True, "updated_at": now,
        }


def config(**overrides: Any) -> ReconcilerConfig:
    values: dict[str, Any] = dict(
        project_id=PROJECT,
        region=REGION,
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
        enable_gc=False,
        enable_checkpoint_gc=False,
    )
    values.update(overrides)
    return ReconcilerConfig(**values)


def reconcile(db: FakeFirestore, *, exit_code: int, generation: int | None = None) -> Any:
    """One reconciler pass over one failed execution carrying `generation`."""
    task = db.docs[f"tasks/{TASK}"]
    carried = task["current_generation"] if generation is None else generation
    logger = build_logger(stream=io.StringIO())
    backend = CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=_Jobs(),
        executions_client=_Executions(execution(generation=carried)),
        tasks_client=_Tasks(exit_code),
        logger=logger,
    )
    store = ControlStore(db, logger=logger)
    return Reconciler(store=store, backends=[backend], config=config(), logger=logger).run_once()


def task_doc(db: FakeFirestore) -> dict[str, Any]:
    return db.docs[f"tasks/{TASK}"]


def refunds(db: FakeFirestore) -> Any:
    return (task_doc(db).get("metadata") or {}).get("startup_refunds")


def assert_released_once(db: FakeFirestore) -> None:
    lease = db.docs[f"leases/{LEASE}"]
    assert lease["released_at"] is not None, lease
    for pool in POOLS:
        assert db.docs[f"pools/{pool}"]["active"] == 0, pool


# ---------------------------------------------------------------------------
# (a) the refund
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [TaskState.DISPATCHED, TaskState.STARTING])
def test_a_sigterm_before_the_runner_started_is_requeued_and_not_counted(db, state):
    """The attempt the scheduler counted at admission is taken back: net unchanged."""
    seed(db, state=state, attempt_count=1, max_attempts=3)

    report = reconcile(db, exit_code=SIGTERM)

    assert [o.kind for o in report.outcomes] == [KIND], report.as_dict()
    task = task_doc(db)
    assert task["state"] == TaskState.READY.value, task
    assert task["attempt_count"] == 0, "the attempt that never ran was counted"
    assert refunds(db) == 1, task.get("metadata")
    error = task["last_error"]
    assert f"exited {SIGTERM} before its runner started" in error, error
    assert "not counted (1/3)" in error, error
    # Fencing and capacity exactly as before: the generation is bumped once,
    # the lease released once through the frozen release.
    assert task["current_generation"] == 2
    assert task["current_lease_id"] is None
    assert_released_once(db)


def test_the_refund_is_applied_before_the_retries_exhausted_decision(db):
    """The attempt that spent the last of `max_attempts` never ran: requeue it, do not fail it."""
    seed(db, attempt_count=3, max_attempts=3)

    report = reconcile(db, exit_code=SIGTERM)

    task = task_doc(db)
    assert task["state"] == TaskState.READY.value, task
    assert task["attempt_count"] == 2
    assert refunds(db) == 1
    assert "not counted (1/3)" in task["last_error"], task["last_error"]
    assert "no attempts left" not in report.outcomes[0].reason, report.as_dict()
    assert_released_once(db)


def test_the_refunds_are_counted_up_across_passes(db):
    seed(db, attempt_count=1, metadata={"startup_refunds": 2, "owner_note": "kept"})

    reconcile(db, exit_code=UNAVAILABLE)

    task = task_doc(db)
    assert task["state"] == TaskState.READY.value
    assert task["attempt_count"] == 0
    assert task["metadata"] == {"startup_refunds": 3, "owner_note": "kept"}, task["metadata"]
    assert "not counted (3/3)" in task["last_error"], task["last_error"]


def test_the_count_never_goes_below_zero(db):
    seed(db, attempt_count=0)

    reconcile(db, exit_code=SIGTERM)

    assert task_doc(db)["attempt_count"] == 0
    assert refunds(db) == 1


# ---------------------------------------------------------------------------
# (b) the bound
# ---------------------------------------------------------------------------


def test_after_three_refunds_an_early_end_counts(db):
    seed(db, attempt_count=1, max_attempts=3, metadata={"startup_refunds": 3})

    reconcile(db, exit_code=SIGTERM)

    task = task_doc(db)
    assert task["state"] == TaskState.READY.value, task
    assert task["attempt_count"] == 1, "a fourth early end was refunded"
    assert refunds(db) == 3
    assert "not counted" not in task["last_error"], task["last_error"]
    assert "retrying" in task["last_error"], task["last_error"]
    assert_released_once(db)


def test_after_three_refunds_a_spent_task_fails(db):
    seed(db, attempt_count=3, max_attempts=3, metadata={"startup_refunds": 3})

    reconcile(db, exit_code=SIGTERM)

    task = task_doc(db)
    assert task["state"] == TaskState.FAILED.value, task
    assert task["attempt_count"] == 3
    assert "no attempts left" in task["last_error"], task["last_error"]
    assert_released_once(db)


def test_a_task_killed_at_every_start_still_terminates(db):
    """Three refunded ends, then `max_attempts` counted ones, then FAILED."""
    attempt_count, generation = 0, 1
    for early_end in range(1, 20):
        attempt_count += 1  # what admission does
        seed(db, attempt_count=attempt_count, generation=generation)

        reconcile(db, exit_code=SIGTERM)

        task = task_doc(db)
        attempt_count, generation = task["attempt_count"], task["current_generation"]
        if task["state"] == TaskState.FAILED.value:
            break
        assert task["state"] == TaskState.READY.value, task
    assert task["state"] == TaskState.FAILED.value, task
    assert early_end == 3 + 3, early_end
    assert refunds(db) == 3


# ---------------------------------------------------------------------------
# (c) a runner that started, and (d) a stale generation, are never refunded
# ---------------------------------------------------------------------------


def test_an_attempt_whose_runner_started_is_never_refunded(db):
    """RUNNING: its worker heartbeated, so its runner ran. Its silence is the heartbeat rules'."""
    seed(
        db,
        state=TaskState.RUNNING,
        attempt_count=1,
        heartbeat_seconds_ago=900,
        age_seconds=1200,
    )

    report = reconcile(db, exit_code=SIGTERM)

    assert KIND not in [o.kind for o in report.outcomes], report.as_dict()
    task = task_doc(db)
    assert task["attempt_count"] == 1, "an attempt whose runner started was refunded"
    assert refunds(db) is None, task.get("metadata")
    assert "not counted" not in (task.get("last_error") or "")


def test_a_worker_that_could_not_start_is_never_refunded(db):
    """Exit 78 fails the task outright; refunding it would only retry the same failure."""
    seed(db, attempt_count=1)

    reconcile(db, exit_code=CANNOT_START)

    task = task_doc(db)
    assert task["state"] == TaskState.FAILED.value, task
    assert task["attempt_count"] == 1
    assert refunds(db) is None


def test_a_stale_generation_is_not_refunded(db):
    """The execution belongs to generation 1; the task is at 2. Nothing of it touches the task."""
    seed(db, generation=2, attempt_count=2)

    report = reconcile(db, exit_code=SIGTERM, generation=1)

    assert KIND not in [o.kind for o in report.outcomes], report.as_dict()
    task = task_doc(db)
    assert task["attempt_count"] == 2
    assert refunds(db) is None
    assert task["current_generation"] == 2


def test_the_store_refuses_a_refund_for_a_generation_it_did_not_fence(db):
    """Inside the transaction: someone fenced past this pass's fence, so this pass's refund is void."""
    seed(db, generation=3, attempt_count=1)
    store = ControlStore(db, logger=build_logger(stream=io.StringIO()))

    repaired = store.repair_task_state(
        TASK,
        to_state=TaskState.READY,
        expected_lease_id=LEASE,
        error="reconciled: execution x exited 143 before its runner started",
        only_from=(TaskState.DISPATCHED, TaskState.STARTING),
        startup_refund_limit=3,
        fenced_generation=2,
    )

    assert repaired is TaskState.READY
    task = task_doc(db)
    assert task["attempt_count"] == 1
    assert refunds(db) is None
    assert "not counted" not in task["last_error"]
