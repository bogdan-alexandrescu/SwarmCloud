"""An execution that ended before its runner started is noticed in the pass that sees it (#198).

WHAT HAPPENED. Task task_5254e8f1cb31446ca20b was dispatched at 20:34:11Z on
2026-09-25. Its execution, swarm-job-eng-mock-9ngvq, started its worker at
20:37:26Z, could not reach Firestore, and exited 69 at 20:37:56Z: "a dependency
was unavailable; retry me". Nothing read that. The task sat DISPATCHED, and
the CLI truthfully showed DISPATCHED, until the reconciler's 20:40 pass found
the lease past its 300 s dispatch deadline and fenced it as a silent lease. Its
`last_error` said the lease had been silent. It did not say the execution had
exited 69 two minutes earlier.

The reconciler already reads the exit code of every FAILED execution whose
attempt holds its task's current lease (`Reconciler._read_terminations`, added
for exit 78). It acted on 78 alone. Now any other exit of an execution whose
task is still DISPATCHED or STARTING is acted on too, in the ordinary repair
order: fence the generation, confirm nothing runs (nothing does: Cloud Run says
the execution is over), release the lease through the frozen
`release_lease_in_transaction`, then READY with the exit code and the
execution in `last_error`, or FAILED once the task's attempts are spent.

WHAT KEEPS IT SAFE.

  * Only an execution Cloud Run records as finished (`completion_time`), and
    only once it finished `ended_execution_grace_seconds` before the pass read
    Firestore. Every write the worker made came before its container exited,
    so a snapshot read after that instant has seen them all. A worker that
    parked its task (exit 75) or failed it (exit 1) is not requeued on the
    strength of a snapshot taken a moment before it did so.
  * Inside the transactions as well: the fence and the repair are refused
    unless the task is still DISPATCHED or STARTING.
  * The release is the frozen one, which is idempotent per lease, and a
    second pass finds a READY task that holds no capacity.
  * A task that reached RUNNING is not this rule's. Its worker heartbeats, and
    the heartbeat rules own it.

HOW THE TESTS WERE MADE RED FIRST. The Cloud Run backend under test is the
production `CloudRunBackend`, fed by fake `run_v2` clients that answer the way
Cloud Run answered for swarm-job-eng-mock-9ngvq. Only names that exist on main
are used, except where a test says otherwise, so on main the behavioural tests
fail on what the task and the lease look like after the pass.
"""

from __future__ import annotations

import io
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeFirestore, FakeTransactionRunner

from reconciler.backends import CloudRunBackend
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import EventType, TaskState

from conftest import TENANT, seed_tenant

PROJECT = "saga-agents-staging"
REGION = "us-central1"
JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-job-eng-mock"
SHORT = "swarm-job-eng-mock-9ngvq"
EXECUTION = f"{JOB}/executions/{SHORT}"
GENERATION = 1
TASK, LEASE, ATTEMPT = "task_5254e8f1cb31446ca20b", "lease_1", "att_916323e782cb4f70bfee"
POOLS = pool_names_for(
    tenant_id=TENANT,
    provider=None,
    resource_class="standard",
    runner_profile="mock",
    backend="CLOUD_RUN_JOB",
)

#: The worker's exits, restated: the numbers are the contract between its
#: process and whoever reads the execution's exit status.
UNAVAILABLE = 69
CANNOT_START = 78

KIND = "worker_ended_at_startup"


# ---------------------------------------------------------------------------
# Cloud Run, as `run_v2` answers
# ---------------------------------------------------------------------------


class FakeJobsClient:
    def __init__(self, jobs: list[Any]) -> None:
        self.jobs = jobs

    def list_jobs(self, parent: str) -> list[Any]:
        return list(self.jobs)


class FakeExecutionsClient:
    def __init__(self, executions: list[Any]) -> None:
        self.executions = executions
        self.cancelled: list[Any] = []

    def list_executions(self, parent: str) -> list[Any]:
        return [e for e in self.executions if str(e.name).startswith(f"{parent}/")]

    def get_execution(self, name: str) -> Any:
        for execution in self.executions:
            if execution.name == name:
                return execution
        from google.api_core import exceptions as core

        raise core.NotFound(name)

    def cancel_execution(self, request: Any = None, **_kwargs: Any) -> Any:
        self.cancelled.append(request)
        raise AssertionError("a finished execution was asked to stop")


class FakeTasksClient:
    def __init__(self, tasks: list[Any], raises: Exception | None = None) -> None:
        self.tasks = tasks
        self.raises = raises
        self.parents: list[str] = []

    def list_tasks(self, *, parent: str) -> list[Any]:
        self.parents.append(parent)
        if self.raises is not None:
            raise self.raises
        return list(self.tasks)


def cloud_run_job() -> Any:
    """The per-tenant Job terraform creates (modules/cloud_run_jobs)."""
    return SimpleNamespace(
        name=JOB,
        labels={"managed-by": "swarm-terraform", "swarm-tenant": TENANT, "swarm-gc-exempt": "true"},
        create_time=utcnow() - timedelta(days=3),
    )


def cloud_run_execution(
    *, ended_seconds_ago: float | None = 50.0, generation: int = GENERATION
) -> Any:
    """swarm-job-eng-mock-9ngvq as `list_executions` returned it: one task, failed.

    `ended_seconds_ago=None` is an execution still running.
    """
    finished = ended_seconds_ago is not None
    env = {
        "TASK_ID": TASK,
        "ATTEMPT_ID": ATTEMPT,
        "TENANT_ID": TENANT,
        "GENERATION": str(generation),
    }
    return SimpleNamespace(
        name=EXECUTION,
        labels={"managed-by": "swarm-scheduler"},
        create_time=utcnow() - timedelta(seconds=(ended_seconds_ago or 0) + 40),
        running_count=0 if finished else 1,
        cancelled_count=0,
        succeeded_count=0,
        failed_count=1 if finished else 0,
        completion_time=(
            utcnow() - timedelta(seconds=ended_seconds_ago) if finished else None
        ),
        reconciling=False,
        template=SimpleNamespace(
            containers=[
                SimpleNamespace(
                    env=[SimpleNamespace(name=k, value=v) for k, v in env.items()]
                )
            ]
        ),
    )


def cloud_run_task(exit_code: int) -> Any:
    """The execution's one task, with the exit code on its last attempt."""
    return SimpleNamespace(
        name=f"{EXECUTION}/tasks/{SHORT}-task0",
        last_attempt_result=SimpleNamespace(
            exit_code=exit_code,
            status=SimpleNamespace(
                code=2, message=f"Task {SHORT}-task0 failed with exit code: {exit_code}"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# the control plane
# ---------------------------------------------------------------------------


def seed(
    db: FakeFirestore,
    *,
    state: TaskState = TaskState.DISPATCHED,
    age_seconds: int = 60,
    attempt_count: int = 1,
    max_attempts: int = 3,
    heartbeat_seconds_ago: float | None = None,
) -> None:
    """What the incident left: admitted `age_seconds` ago, lease held, deadline ahead."""
    now = utcnow()
    admitted = now - timedelta(seconds=age_seconds)
    db.seed(
        f"tasks/{TASK}",
        {
            "id": TASK, "tenant_id": TENANT, "state": state.value,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": GENERATION, "current_lease_id": LEASE,
            "attempt_count": attempt_count, "max_attempts": max_attempts,
            "updated_at": admitted, "cancel_requested": False, "last_error": None,
        },
    )
    db.seed(
        f"leases/{LEASE}",
        {
            "lease_id": LEASE, "task_id": TASK, "attempt_id": ATTEMPT,
            "tenant_id": TENANT, "generation": GENERATION,
            "pools": POOLS, "units": 1, "state": state.value,
            "created_at": admitted,
            "dispatch_deadline": admitted + timedelta(seconds=300),
            "expires_at": admitted + timedelta(seconds=120),
            "heartbeat_at": (
                now - timedelta(seconds=heartbeat_seconds_ago)
                if heartbeat_seconds_ago is not None else None
            ),
            "released_at": None,
        },
    )
    db.seed(
        f"attempts/{ATTEMPT}",
        {
            "attempt_id": ATTEMPT, "task_id": TASK, "tenant_id": TENANT,
            "generation": GENERATION, "lease_id": LEASE, "backend": "CLOUD_RUN_JOB",
            "created_at": admitted, "execution_name": EXECUTION,
        },
    )
    for pool in POOLS:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 1,
                                  "enabled": True, "updated_at": now})
    seed_tenant(db)


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
    )
    values.update(overrides)
    return ReconcilerConfig(**values)


def reconcile(
    db: FakeFirestore,
    *,
    execution: Any,
    exit_code: int = UNAVAILABLE,
    tasks_raise: Exception | None = None,
    settings: ReconcilerConfig | None = None,
) -> tuple[Any, FakeExecutionsClient, FakeTasksClient]:
    logger = build_logger(stream=io.StringIO())
    executions = FakeExecutionsClient([execution])
    tasks = FakeTasksClient([cloud_run_task(exit_code)], raises=tasks_raise)
    backend = CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([cloud_run_job()]),
        executions_client=executions,
        tasks_client=tasks,
        logger=logger,
    )
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(
        store=store, backends=[backend], config=settings or config(), logger=logger
    ).run_once()
    return report, executions, tasks


def _write_index(db: FakeFirestore, path: str, field: str) -> int:
    for index, (_op, written, data) in enumerate(db.writes):
        if written == path and field in data:
            return index
    raise AssertionError(f"no write of {field} to {path}: {db.writes}")


# ---------------------------------------------------------------------------
# the incident
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [TaskState.DISPATCHED, TaskState.STARTING])
def test_an_execution_that_exited_69_at_startup_requeues_its_task_in_the_pass_that_sees_it(
    db, state
):
    """60 s after admission: the dispatch deadline is 240 s away, and no longer matters."""
    seed(db, state=state, age_seconds=60, attempt_count=1, max_attempts=3)

    report, executions, tasks = reconcile(db, execution=cloud_run_execution(ended_seconds_ago=50))

    assert tasks.parents == [EXECUTION], "the exit code was never read"
    assert [o.kind for o in report.outcomes] == [KIND], report.as_dict()
    [outcome] = report.outcomes
    assert outcome.released is True
    assert outcome.repaired_to == TaskState.READY.value
    assert executions.cancelled == [], "a finished execution was asked to stop"

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.READY.value, task
    assert task["current_lease_id"] is None
    assert task["current_generation"] == GENERATION + 1
    error = task["last_error"]
    assert "exited 69" in error, error
    assert SHORT in error, error
    assert state.value in error, error
    assert "retrying" in error, error

    lease = db.doc(f"leases/{LEASE}")
    assert lease["released_at"] is not None
    assert lease["release_reason"] == f"reconciler:{KIND}"
    for pool in POOLS:
        assert db.doc(f"pools/{pool}")["active"] == 0, pool


def test_the_generation_is_fenced_before_the_lease_is_released_and_the_task_requeued(db):
    """Invariant 5, then 2: the same order every other repair keeps."""
    seed(db)

    reconcile(db, execution=cloud_run_execution())

    fenced = _write_index(db, f"tasks/{TASK}", "current_generation")
    released = _write_index(db, f"leases/{LEASE}", "released_at")
    requeued = _write_index(db, f"tasks/{TASK}", "state")
    assert fenced < released < requeued, (fenced, released, requeued)
    assert db.event_types(TASK) == [
        EventType.GENERATION_FENCED.value,
        EventType.LEASE_RELEASED.value,
        EventType.READY.value,
    ], db.event_types(TASK)
    ready = db.events(TASK)[-1]
    assert ready["detail"]["reason"] == KIND
    assert ready["detail"]["exit_code"] == UNAVAILABLE
    assert ready["detail"]["execution"] == EXECUTION


def test_with_its_attempts_spent_the_task_fails_and_says_why(db):
    seed(db, attempt_count=3, max_attempts=3)

    reconcile(db, execution=cloud_run_execution())

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value, task
    assert "exited 69" in task["last_error"], task["last_error"]
    assert "retrying" not in task["last_error"], task["last_error"]
    assert db.doc(f"leases/{LEASE}")["released_at"] is not None


def test_a_second_pass_releases_nothing_again(db):
    """No double release: the frozen release is idempotent, and READY holds no capacity."""
    seed(db)

    reconcile(db, execution=cloud_run_execution())
    released_once = [w for w in db.writes if w[1] == f"leases/{LEASE}" and "released_at" in w[2]]
    report, _, _ = reconcile(db, execution=cloud_run_execution())

    released = [w for w in db.writes if w[1] == f"leases/{LEASE}" and "released_at" in w[2]]
    assert len(released_once) == 1 and len(released) == 1, released
    assert report.outcomes == [], report.as_dict()
    assert db.event_types(TASK).count(EventType.LEASE_RELEASED.value) == 1
    for pool in POOLS:
        assert db.doc(f"pools/{pool}")["active"] == 0, pool


# ---------------------------------------------------------------------------
# what this rule leaves alone
# ---------------------------------------------------------------------------


def test_an_execution_that_ended_moments_ago_is_left_for_the_next_pass(db):
    """Five seconds is inside the grace: its worker's last writes may postdate the snapshot."""
    seed(db)

    report, _, _ = reconcile(db, execution=cloud_run_execution(ended_seconds_ago=5))

    assert report.outcomes == [], report.as_dict()
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.DISPATCHED.value
    assert db.doc(f"leases/{LEASE}")["released_at"] is None


def test_an_execution_still_running_is_not_judged_by_its_exit_code(db):
    seed(db)

    report, executions, tasks = reconcile(db, execution=cloud_run_execution(ended_seconds_ago=None))

    assert report.outcomes == [], report.as_dict()
    assert tasks.parents == [], "a running execution's exit code was read"
    assert executions.cancelled == []


def test_a_task_that_reached_running_is_left_to_the_heartbeat_rules(db):
    """Its worker wrote RUNNING and heartbeats. Silence, not this rule, decides it."""
    seed(db, state=TaskState.RUNNING, heartbeat_seconds_ago=10)

    report, _, _ = reconcile(db, execution=cloud_run_execution())

    assert KIND not in [o.kind for o in report.outcomes], report.as_dict()
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.RUNNING.value


def test_exit_78_is_still_the_cannot_start_rule(db):
    """78 fails the task with no retry (owner, 2026-09-25). This rule does not requeue it."""
    seed(db)

    report, _, _ = reconcile(db, execution=cloud_run_execution(), exit_code=CANNOT_START)

    assert [o.kind for o in report.outcomes] == ["worker_cannot_start"], report.as_dict()
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.FAILED.value


def test_an_exit_code_that_could_not_be_read_concludes_nothing(db):
    seed(db)

    report, _, _ = reconcile(
        db, execution=cloud_run_execution(), tasks_raise=PermissionError("403 run.tasks.list")
    )

    assert report.outcomes == [], report.as_dict()
    assert db.doc(f"tasks/{TASK}")["state"] == TaskState.DISPATCHED.value


def test_a_dry_run_reads_the_exit_code_and_writes_nothing(db):
    from dataclasses import replace

    seed(db)
    before = len(db.writes)

    report, _, tasks = reconcile(
        db, execution=cloud_run_execution(), settings=replace(config(), dry_run=True)
    )

    assert tasks.parents == [EXECUTION]
    assert [o.kind for o in report.outcomes] == [KIND], report.as_dict()
    assert report.outcomes[0].skipped == "dry_run"
    assert db.writes[before:] == []


# ---------------------------------------------------------------------------
# the pieces
# ---------------------------------------------------------------------------


def test_the_transactions_refuse_a_task_that_has_left_dispatched_and_starting(db):
    """The second guard: a task its worker parked after the snapshot is not fenced or requeued.

    New keyword, so red on main for its absence; the behavioural red is above.
    """
    seed(db)
    db.doc(f"tasks/{TASK}")["state"] = TaskState.PARKED.value
    store = ControlStore(db, logger=build_logger(stream=io.StringIO()),
                         txn_runner=FakeTransactionRunner(db))
    only = (TaskState.DISPATCHED, TaskState.STARTING)

    assert store.invalidate_generation(TASK, GENERATION, only_from=only) is None
    assert store.repair_task_state(
        TASK, to_state=TaskState.READY, expected_lease_id=LEASE, only_from=only
    ) is None
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.PARKED.value
    assert task["current_generation"] == GENERATION


def test_the_cloud_run_view_carries_when_the_execution_ended():
    """`completion_time` is what the grace is measured from."""
    execution = cloud_run_execution(ended_seconds_ago=50)
    backend = CloudRunBackend(
        PROJECT, REGION,
        jobs_client=FakeJobsClient([cloud_run_job()]),
        executions_client=FakeExecutionsClient([execution]),
    )

    [view] = backend.list_executions()

    assert getattr(view, "completed_at", None) == execution.completion_time


def test_a_gke_job_carries_when_it_finished():
    """A failed Job has no `completion_time`: its Failed condition says when."""
    from reconciler.backends import job_finished_at

    failed_at = utcnow() - timedelta(seconds=40)
    failed = SimpleNamespace(status=SimpleNamespace(
        completion_time=None,
        conditions=[SimpleNamespace(type="Failed", status="True", last_transition_time=failed_at)],
    ))
    running = SimpleNamespace(status=SimpleNamespace(completion_time=None, conditions=[]))

    assert job_finished_at(failed) == failed_at
    assert job_finished_at(running) is None


def test_the_grace_is_thirty_seconds_and_configurable(monkeypatch):
    assert config().ended_execution_grace_seconds == 30
    monkeypatch.setenv("ENDED_EXECUTION_GRACE_SECONDS", "45")
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    assert ReconcilerConfig.from_env().ended_execution_grace_seconds == 45
