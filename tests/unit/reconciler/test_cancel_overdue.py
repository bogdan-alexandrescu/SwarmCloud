"""A cancel the execution ignores is enforced by the reconciler within a bound (#627).

The 2026-10-05 history analysis: cancel_requested -> cancelled was 10 s at
p50, but four mock steps ran 12.8 h past their cancel (2026-10-04) and two
browser steps 7.6 h (2026-09-24). The 10-04 cases ended only when the
reconciler noticed a generation mismatch.

WHY NOTHING STOPPED THEM. A cancelled task whose worker is alive keeps its
lease heartbeating -- the beat runs on its own thread and is deliberately not
stopped by `cancel_requested` (`lifecycle`, the heartbeat docstring) -- so no
absence rule ever fires on it, and no rule looked at the cancel flag at all.
The worker acts on the flag only from its control poll; a worker blocked
anywhere else never does. The API's direct stop (`swarm_api.executioncancel`
-> `/stop-execution`) is one attempt, acknowledged whatever its outcome: an
execution it could not read, or a stop the backend did not confirm, was left
running with nothing to try again.

Pinned here:

  C-1  A task whose cancel is older than `cancel_enforce_after_seconds`, with
       its current attempt's execution still active, has that execution
       terminated through the backend, then -- in one transaction -- its
       generation fenced, its lease released (every pool given back) and the
       task CANCELLED. Termination precedes the release.
  C-2  Inside the bound nothing is touched: the worker's own cancel path
       (poll, stop the runner, checkpoint, price the usage, finish) is given
       the time it needs to record the attempt's cost.
  C-3  The bound is measured from `cancel_requested_at`; a task written
       before that field existed falls back to `updated_at`, which is never
       earlier than the cancel, so the fallback can only wait longer.
  C-4  A kill the backend does not confirm releases nothing and fences
       nothing: the slot stays with the execution still spending it.
  C-5  A dry run changes nothing.
  C-6  An execution of an older generation is not this rule's (the
       obsolete-generation rule's), and the rule never fires twice for one
       lease alongside a rule that already terminates it.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.detect import FindingKind, detect_all
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import TaskState

TENANT = "eng"
EXECUTION = "projects/p/locations/us-central1/jobs/swarm-eng-mock/executions/x1"


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
        cancel_enforce_after_seconds=600,
    )


def _pools() -> list[str]:
    return pool_names_for(
        tenant_id=TENANT, provider=None, resource_class="standard",
        runner_profile="mock", backend="CLOUD_RUN_JOB",
    )


def seed_cancelled_running(
    db: FakeFirestore,
    *,
    cancel_age: float | None,
    updated_age: float = 5,
    generation: int = 3,
) -> None:
    """A RUNNING task whose worker heartbeats, with a cancel `cancel_age` seconds old."""
    now = utcnow()
    pools = _pools()
    task = {
        "id": "task_1", "tenant_id": TENANT, "state": TaskState.RUNNING.value,
        "runner_profile": "mock", "resource_class": "standard",
        "current_generation": generation, "current_lease_id": "lease_1",
        "attempt_count": 1, "max_attempts": 3,
        "updated_at": now - timedelta(seconds=updated_age),
        "cancel_requested": True,
    }
    if cancel_age is not None:
        task["cancel_requested_at"] = now - timedelta(seconds=cancel_age)
    db.seed("tasks/task_1", task)
    db.seed(
        "leases/lease_1",
        {
            "lease_id": "lease_1", "task_id": "task_1", "attempt_id": "att_1",
            "tenant_id": TENANT, "generation": generation, "pools": pools, "units": 1,
            "state": TaskState.RUNNING.value,
            "created_at": now - timedelta(hours=13),
            "dispatch_deadline": now - timedelta(hours=12),
            # Alive: the heartbeat thread keeps beating through a cancel.
            "expires_at": now + timedelta(seconds=120),
            "heartbeat_at": now - timedelta(seconds=10),
            "released_at": None,
        },
    )
    db.seed(
        "attempts/att_1",
        {
            "attempt_id": "att_1", "task_id": "task_1", "tenant_id": TENANT,
            "generation": generation, "lease_id": "lease_1", "backend": "CLOUD_RUN_JOB",
            "created_at": now - timedelta(hours=13), "execution_name": EXECUTION,
            "started_at": now - timedelta(hours=13), "completed_at": None,
            "exit_code": None, "error": None,
        },
    )
    for pool in pools:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 2,
                                  "enabled": True, "updated_at": now})


def running_execution(generation: int = 3) -> ExecutionView:
    return ExecutionView(
        name=EXECUTION,
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(hours=13),
        task_id="task_1",
        attempt_id="att_1",
        tenant_id=TENANT,
        generation=generation,
        parent="projects/p/locations/us-central1/jobs/swarm-eng-mock",
    )


def build(db, config, *backends) -> Reconciler:
    logger = build_logger(stream=io.StringIO())
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=list(backends), config=config, logger=logger)


def _untouched(db: FakeFirestore, backend: FakeBackend) -> None:
    assert backend.terminated == [], "an execution was stopped inside the cancel bound"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value
    assert task["current_generation"] == 3, "the generation was fenced"
    assert db.doc("leases/lease_1")["released_at"] is None, "the lease was released"
    for pool in _pools():
        assert db.doc(f"pools/{pool}")["active"] == 2


# --------------------------------------------------------------------------
# C-1  past the bound: kill, fence, release, CANCELLED
# --------------------------------------------------------------------------

def test_an_overdue_cancel_stops_the_execution_and_releases_the_lease(db, config):
    seed_cancelled_running(db, cancel_age=12.8 * 3600)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, backend).run_once()

    kinds = [outcome.kind for outcome in report.outcomes]
    assert FindingKind.CANCEL_OVERDUE.value in kinds, kinds
    assert backend.terminated == [EXECUTION], "the ignored cancel did not stop the execution"

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.CANCELLED.value, task["state"]
    assert task["current_generation"] == 4, "the stopped attempt's generation was not fenced"
    assert task["end_cause"] == "cancel_requested", task.get("end_cause")
    lease = db.doc("leases/lease_1")
    assert lease["released_at"] is not None, "the cancelled attempt's lease was kept"
    for pool in _pools():
        assert db.doc(f"pools/{pool}")["active"] == 1, f"{pool} was not given back"
    assert db.doc("attempts/att_1")["completed_at"] is not None

    # The kill comes BEFORE the release: a slot is never returned while the
    # execution holding it may still run (invariants 1-3).
    ops = [(entry[0], entry[1]) for entry in db.writes]
    terminate_at = ops.index(("terminate", EXECUTION))
    lease_written_at = next(i for i, (_, path) in enumerate(ops) if path == "leases/lease_1")
    assert terminate_at < lease_written_at, "the lease was written before the execution was stopped"


# --------------------------------------------------------------------------
# C-2  inside the bound: the worker's own cancel path is left alone
# --------------------------------------------------------------------------

def test_a_fresh_cancel_is_left_to_the_worker(db, config):
    seed_cancelled_running(db, cancel_age=120)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, backend).run_once()

    assert FindingKind.CANCEL_OVERDUE.value not in [o.kind for o in report.outcomes]
    _untouched(db, backend)


# --------------------------------------------------------------------------
# C-3  the reference time
# --------------------------------------------------------------------------

def test_without_cancel_requested_at_the_bound_runs_from_updated_at(db, config):
    seed_cancelled_running(db, cancel_age=None, updated_age=3600)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    build(db, config, backend).run_once()

    assert backend.terminated == [EXECUTION]
    assert db.doc("tasks/task_1")["state"] == TaskState.CANCELLED.value


def test_a_recent_update_never_makes_an_old_cancel_look_fresh(db, config):
    """A checkpoint pointer bumps `updated_at`; the cancel's own time decides."""
    seed_cancelled_running(db, cancel_age=3600, updated_age=5)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    build(db, config, backend).run_once()

    assert backend.terminated == [EXECUTION]


def test_without_cancel_requested_at_a_recent_update_waits(db, config):
    seed_cancelled_running(db, cancel_age=None, updated_age=60)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    build(db, config, backend).run_once()

    _untouched(db, backend)


# --------------------------------------------------------------------------
# C-4  an unconfirmed kill releases nothing
# --------------------------------------------------------------------------

def test_an_unconfirmed_kill_releases_and_fences_nothing(db, config):
    seed_cancelled_running(db, cancel_age=3600)
    backend = FakeBackend(
        executions=[running_execution()], journal=db.writes, terminate_returns=False
    )

    report = build(db, config, backend).run_once()

    outcome = next(o for o in report.outcomes if o.kind == FindingKind.CANCEL_OVERDUE.value)
    assert outcome.released is False
    assert outcome.skipped == "termination_unconfirmed"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value
    assert task["current_generation"] == 3
    assert db.doc("leases/lease_1")["released_at"] is None


# --------------------------------------------------------------------------
# C-5  dry run
# --------------------------------------------------------------------------

def test_a_dry_run_changes_nothing(db, config):
    from dataclasses import replace

    seed_cancelled_running(db, cancel_age=3600)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, replace(config, dry_run=True), backend).run_once()

    outcome = next(o for o in report.outcomes if o.kind == FindingKind.CANCEL_OVERDUE.value)
    assert outcome.skipped == "dry_run"
    _untouched(db, backend)


# --------------------------------------------------------------------------
# C-6  scope
# --------------------------------------------------------------------------

def test_an_older_generations_execution_is_not_this_rules(db, config):
    seed_cancelled_running(db, cancel_age=3600)
    store = ControlStore(db, logger=build_logger(stream=io.StringIO()),
                         txn_runner=FakeTransactionRunner(db))
    snapshot = store.snapshot()

    findings = detect_all(snapshot, [running_execution(generation=2)], config,
                          now=snapshot.taken_at)

    assert FindingKind.CANCEL_OVERDUE not in [f.kind for f in findings]


def test_the_rule_names_the_lease_its_attempt_and_generation(db, config):
    seed_cancelled_running(db, cancel_age=3600)
    store = ControlStore(db, logger=build_logger(stream=io.StringIO()),
                         txn_runner=FakeTransactionRunner(db))
    snapshot = store.snapshot()

    findings = detect_all(snapshot, [running_execution()], config, now=snapshot.taken_at)

    overdue = [f for f in findings if f.kind is FindingKind.CANCEL_OVERDUE]
    assert len(overdue) == 1, [f.kind for f in findings]
    finding = overdue[0]
    assert (finding.task_id, finding.lease_id, finding.attempt_id, finding.generation) == (
        "task_1", "lease_1", "att_1", 3
    )
    assert finding.requires_termination
    assert finding.execution is not None and finding.execution.name == EXECUTION


# --------------------------------------------------------------------------
# the bound itself
# --------------------------------------------------------------------------

def test_the_bound_defaults_to_ten_minutes_and_is_configurable(monkeypatch):
    monkeypatch.delenv("CANCEL_ENFORCE_AFTER_SECONDS", raising=False)
    assert ReconcilerConfig(project_id="p", region="r", firestore_database="d") \
        .cancel_enforce_after_seconds == 600
    monkeypatch.setenv("PROJECT_ID", "swarm-unit-test")
    monkeypatch.setenv("CANCEL_ENFORCE_AFTER_SECONDS", "900")
    assert ReconcilerConfig.from_env().cancel_enforce_after_seconds == 900
