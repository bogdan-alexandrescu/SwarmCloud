"""The reconciler's one non-negotiable ordering rule.

    invalidate the generation -> terminate the execution -> release the slot

Releasing first hands the slot to the scheduler, which admits the next task
immediately, while the agent that owned the slot is still running: two agents,
one task, one set of credentials, one repository. These tests assert the order
across the document store AND the backend, using a single shared journal, and
assert that a termination which is not confirmed does not release at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView, JobResourceView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import EventType, TaskState

TENANT = "eng"


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


def seed_running_task(
    db: FakeFirestore,
    *,
    task_id: str = "task_1",
    lease_id: str = "lease_1",
    attempt_id: str = "att_1",
    generation: int = 3,
    silent_seconds: int = 600,
    state: TaskState = TaskState.RUNNING,
    pool_active: int = 2,
) -> None:
    now = utcnow()
    pools = pool_names_for(
        tenant_id=TENANT, provider=None, resource_class="standard",
        runner_profile="mock", backend="CLOUD_RUN_JOB",
    )
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id, "tenant_id": TENANT, "state": state.value,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": generation, "current_lease_id": lease_id,
            "attempt_count": 1, "max_attempts": 3, "updated_at": now,
            "cancel_requested": False,
        },
    )
    db.seed(
        f"leases/{lease_id}",
        {
            "lease_id": lease_id, "task_id": task_id, "attempt_id": attempt_id,
            "tenant_id": TENANT, "generation": generation, "pools": pools, "units": 1,
            "state": state.value,
            # Exactly what the admission transaction would have written:
            # a dispatch deadline 300s after creation and an expiry 120s after
            # the last heartbeat.
            "created_at": now - timedelta(seconds=silent_seconds + 60),
            "dispatch_deadline": now - timedelta(seconds=silent_seconds + 60) + timedelta(seconds=300),
            "expires_at": now - timedelta(seconds=silent_seconds) + timedelta(seconds=120),
            "heartbeat_at": now - timedelta(seconds=silent_seconds),
            "released_at": None,
        },
    )
    db.seed(
        f"attempts/{attempt_id}",
        {
            "attempt_id": attempt_id, "task_id": task_id, "tenant_id": TENANT,
            "generation": generation, "lease_id": lease_id, "backend": "CLOUD_RUN_JOB",
            "created_at": now - timedelta(seconds=silent_seconds + 60),
            "execution_name": "projects/p/locations/us-central1/jobs/swarm-eng-mock/executions/x1",
            "started_at": now - timedelta(seconds=silent_seconds + 30),
        },
    )
    for pool in pools:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": pool_active,
                                  "enabled": True, "updated_at": now})


def running_execution(attempt_id: str = "att_1", generation: int = 3, age_seconds: int = 600):
    return ExecutionView(
        name="projects/p/locations/us-central1/jobs/swarm-eng-mock/executions/x1",
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(seconds=age_seconds),
        task_id="task_1",
        attempt_id=attempt_id,
        tenant_id=TENANT,
        generation=generation,
        parent="projects/p/locations/us-central1/jobs/swarm-eng-mock",
    )


def build(db: FakeFirestore, config: ReconcilerConfig, backend: FakeBackend) -> Reconciler:
    logger = build_logger(stream=__import__("io").StringIO())
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=[backend], config=config, logger=logger)


def index_of(journal, predicate) -> int:
    for i, entry in enumerate(journal):
        if predicate(entry):
            return i
    raise AssertionError("entry not found in journal")


def test_slot_is_released_only_after_the_execution_is_terminated(db, config):
    seed_running_task(db, pool_active=2)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)
    report = build(db, config, backend).run_once()

    assert report.findings == 1
    outcome = report.outcomes[0]
    assert outcome.kind == "dead_worker"
    assert outcome.invalidated_to == 4
    assert outcome.terminated is True
    assert outcome.released is True

    journal = db.writes
    invalidated = index_of(
        journal, lambda e: e[1] == "tasks/task_1" and "current_generation" in e[2]
    )
    terminated = index_of(journal, lambda e: e[0] == "terminate")
    released = index_of(journal, lambda e: e[1].startswith("pools/") and "active" in e[2])

    assert invalidated < terminated < released, (
        "the slot must come back only after the execution is confirmed dead"
    )
    assert db.doc("pools/global")["active"] == 1
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("tasks/task_1")["current_generation"] == 4
    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value


def test_failed_termination_keeps_the_slot_held(db, config):
    seed_running_task(db, pool_active=2)
    backend = FakeBackend(
        executions=[running_execution()],
        journal=db.writes,
        terminate_raises=RuntimeError("Cloud Run said no"),
    )
    report = build(db, config, backend).run_once()

    outcome = report.outcomes[0]
    assert outcome.released is False
    assert outcome.skipped and "termination_failed" in outcome.skipped
    # The slot stays held: a stuck slot is recoverable, a duplicate agent is not.
    assert db.doc("pools/global")["active"] == 2
    assert db.doc("leases/lease_1")["released_at"] is None
    # But the generation was already invalidated, so the worker fences itself.
    assert db.doc("tasks/task_1")["current_generation"] == 4


def test_unconfirmed_termination_keeps_the_slot_held(db, config):
    seed_running_task(db)
    backend = FakeBackend(
        executions=[running_execution()], journal=db.writes, terminate_returns=False
    )
    report = build(db, config, backend).run_once()
    assert report.outcomes[0].skipped == "termination_unconfirmed"
    assert db.doc("leases/lease_1")["released_at"] is None


def test_stale_lease_with_no_execution_releases_and_requeues(db, config):
    seed_running_task(db, pool_active=1)
    backend = FakeBackend(executions=[], journal=db.writes)
    report = build(db, config, backend).run_once()

    outcome = report.outcomes[0]
    assert outcome.kind in ("stale_lease", "missing_execution")
    assert outcome.released is True
    assert outcome.repaired_to == TaskState.READY.value
    assert db.doc("pools/global")["active"] == 0
    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value
    assert EventType.GENERATION_FENCED.value in db.event_types("task_1")


def test_exhausted_attempts_are_failed_rather_than_requeued(db, config):
    seed_running_task(db)
    db.doc("tasks/task_1")["attempt_count"] = 3
    db.doc("tasks/task_1")["max_attempts"] = 3
    backend = FakeBackend(executions=[], journal=db.writes)
    report = build(db, config, backend).run_once()
    assert report.outcomes[0].repaired_to == TaskState.FAILED.value
    assert db.doc("tasks/task_1")["state"] == TaskState.FAILED.value


def test_orphan_execution_for_a_terminal_task_is_terminated(db, config):
    seed_running_task(db, state=TaskState.RUNNING)
    db.doc("tasks/task_1")["state"] = TaskState.SUCCEEDED.value
    db.doc("leases/lease_1")["released_at"] = utcnow()
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)
    report = build(db, config, backend).run_once()

    assert any(o.kind == "orphan_execution" for o in report.outcomes)
    assert backend.terminated, "compute with no lease behind it must be stopped"


def test_obsolete_generation_execution_is_terminated(db, config):
    """A previous generation's execution is still running after a re-admission."""
    seed_running_task(db, generation=5, silent_seconds=0)
    stale = running_execution(attempt_id="att_old", generation=4)
    backend = FakeBackend(executions=[stale], journal=db.writes)
    report = build(db, config, backend).run_once()

    kinds = {o.kind for o in report.outcomes}
    assert "obsolete_generation" in kinds
    assert backend.terminated == [stale.name]
    # The live lease of generation 5 is untouched.
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("pools/global")["active"] == 2
    # And so is the TASK. This assertion is the one this test was missing: the
    # lease document survived, but step 4 used to reset the task to READY and
    # clear `current_lease_id`, unhooking that very lease. See
    # test_repair_respects_the_current_lease.py.
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("tasks/task_1")["current_lease_id"] == "lease_1"


def test_healthy_running_task_is_left_completely_alone(db, config):
    seed_running_task(db, silent_seconds=5)
    backend = FakeBackend(executions=[running_execution(age_seconds=300)], journal=db.writes)
    before = len(db.writes)
    report = build(db, config, backend).run_once()

    assert report.findings == 0
    assert report.outcomes == []

    # The property is that a healthy task is not TOUCHED -- not that the pass
    # is invisible. Every pass now records itself in `reconciler_passes`
    # (without it the platform's account of runtime faults died with the
    # instance), so this names the collections whose modification would be the
    # actual bug rather than counting writes.
    touched = [
        (op, path) for op, path, *_ in db.writes[before:]
        if not path.startswith("reconciler_passes/")
    ]
    assert touched == [], f"a healthy task was modified: {touched}"
    assert db.doc("pools/global")["active"] == 2


def test_an_unreadable_backend_blocks_repairs_rather_than_guessing(db, config):
    seed_running_task(db)
    backend = FakeBackend(
        executions=[running_execution()],
        journal=db.writes,
        list_raises=RuntimeError("Cloud Run API unavailable"),
    )
    report = build(db, config, backend).run_once()

    assert report.errors, "the failure must be reported"
    assert report.findings == 0, "no execution list means no safe conclusions"
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("pools/global")["active"] == 2


def test_dry_run_changes_nothing(db, config):
    seed_running_task(db)
    dry = ReconcilerConfig(**{**config.__dict__, "dry_run": True})
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)
    before = len(db.writes)
    report = build(db, dry, backend).run_once()

    assert report.findings == 1
    assert report.outcomes[0].skipped == "dry_run"
    assert db.writes[before:] == []
    assert backend.terminated == []


def test_gc_refuses_resources_this_platform_does_not_own(db, config):
    seed_running_task(db, silent_seconds=5)
    old = utcnow() - timedelta(days=30)
    unmanaged = JobResourceView(
        name="someone-elses-job", tenant_id="other", runner_profile=None,
        created_at=old, last_execution_at=old, managed=False, active_executions=0,
    )
    managed = JobResourceView(
        name="swarm-finance-mock", tenant_id="finance", runner_profile="mock",
        created_at=old, last_execution_at=old, managed=True, active_executions=0,
    )
    backend = FakeBackend(
        executions=[running_execution(age_seconds=300)],
        resources=[unmanaged, managed],
        journal=db.writes,
    )
    report = build(db, config, backend).run_once()

    assert backend.deleted == ["swarm-finance-mock"]
    assert "someone-elses-job" not in backend.deleted
    assert any(o.deleted == "swarm-finance-mock" for o in report.outcomes)


def test_gc_leaves_job_resources_of_active_tenants_alone(db, config):
    seed_running_task(db, silent_seconds=5)
    old = utcnow() - timedelta(days=30)
    in_use = JobResourceView(
        name="swarm-eng-mock", tenant_id=TENANT, runner_profile="mock",
        created_at=old, last_execution_at=old, managed=True, active_executions=0,
    )
    backend = FakeBackend(
        executions=[running_execution(age_seconds=300)], resources=[in_use], journal=db.writes
    )
    build(db, config, backend).run_once()
    assert backend.deleted == []
