"""A fence or reclaim records the end of the attempt it supersedes (#630).

The history analysis of 2026-10-05 found 183 attempts with no `completed_at`
and no `exit_code` -- almost all generation-N attempts the reconciler had
fenced or reclaimed, whose worker never wrote its own end because a fenced
worker exits without touching anything. Every duration and concurrency figure
read from attempts counted them as still running: a first pass read a peak of
202 concurrent attempts where the truth was 20.

Pinned here:

  S-1  A reclaim (`fence_release_repair`: kill, fence, release, requeue)
       writes the superseded attempt's `completed_at`, with cause `reclaimed`
       at the head of its `error`, in the same transaction.
  S-2  A fence alone (the stuck rule, `fence_attempt`) writes it with cause
       `fenced`.
  S-3  Only the attempt of the generation being fenced. An attempt at the
       task's CURRENT generation -- a newer admission, or a fence that lost
       its race -- is never written (invariant 5).
  S-4  An attempt its worker already ended keeps the worker's account.
  S-5  A dry run writes nothing.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import timedelta

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.detect import Finding, FindingKind
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler, RepairOutcome
from reconciler.store import ATTEMPT_FENCED, ATTEMPT_RECLAIMED, ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import TaskState

TENANT = "eng"
EXECUTION = "projects/p/locations/us-central1/jobs/swarm-eng-mock/executions/x1"


class FenceLosesItsRace(ControlStore):
    """The fence refused inside its transaction: the task stays at its generation."""

    def _fence_decision(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


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


def _pools() -> list[str]:
    return pool_names_for(
        tenant_id=TENANT, provider=None, resource_class="standard",
        runner_profile="mock", backend="CLOUD_RUN_JOB",
    )


def seed_attempt(db: FakeFirestore, attempt_id: str, *, generation: int, lease_id: str,
                 **extra) -> None:
    now = utcnow()
    db.seed(
        f"attempts/{attempt_id}",
        {
            "attempt_id": attempt_id, "task_id": "task_1", "tenant_id": TENANT,
            "generation": generation, "lease_id": lease_id, "backend": "CLOUD_RUN_JOB",
            "created_at": now - timedelta(seconds=660), "execution_name": EXECUTION,
            "started_at": now - timedelta(seconds=630), "completed_at": None,
            "exit_code": None, "error": None,
            **extra,
        },
    )


def seed_dead_worker(db: FakeFirestore, *, generation: int = 3, **attempt) -> None:
    """A RUNNING task whose lease went silent ten minutes ago."""
    now = utcnow()
    silent = 600
    pools = _pools()
    db.seed(
        "tasks/task_1",
        {
            "id": "task_1", "tenant_id": TENANT, "state": TaskState.RUNNING.value,
            "runner_profile": "mock", "resource_class": "standard",
            "current_generation": generation, "current_lease_id": "lease_1",
            "attempt_count": 1, "max_attempts": 3, "updated_at": now,
            "cancel_requested": False,
        },
    )
    db.seed(
        "leases/lease_1",
        {
            "lease_id": "lease_1", "task_id": "task_1", "attempt_id": "att_1",
            "tenant_id": TENANT, "generation": generation, "pools": pools, "units": 1,
            "state": TaskState.RUNNING.value,
            "created_at": now - timedelta(seconds=silent + 60),
            "dispatch_deadline": now - timedelta(seconds=silent - 240),
            "expires_at": now - timedelta(seconds=silent - 120),
            "heartbeat_at": now - timedelta(seconds=silent),
            "released_at": None,
        },
    )
    seed_attempt(db, "att_1", generation=generation, lease_id="lease_1", **attempt)
    for pool in pools:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 2,
                                  "enabled": True, "updated_at": now})


def running_execution(generation: int = 3) -> ExecutionView:
    return ExecutionView(
        name=EXECUTION,
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(seconds=600),
        task_id="task_1",
        attempt_id="att_1",
        tenant_id=TENANT,
        generation=generation,
        parent="projects/p/locations/us-central1/jobs/swarm-eng-mock",
    )


def store_for(db: FakeFirestore, store_class=ControlStore) -> ControlStore:
    return store_class(
        db, logger=build_logger(stream=io.StringIO()), txn_runner=FakeTransactionRunner(db)
    )


def build(db, config, *backends, store_class=ControlStore) -> Reconciler:
    logger = build_logger(stream=io.StringIO())
    store = store_class(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=list(backends), config=config, logger=logger)


def stuck_finding(generation: int = 3, attempt_id: str = "att_1") -> Finding:
    return Finding(
        kind=FindingKind.STUCK_NO_PROGRESS,
        reason="no progress for 1800s",
        task_id="task_1",
        lease_id="lease_1",
        attempt_id=attempt_id,
        tenant_id=TENANT,
        generation=generation,
    )


def _outcome(finding: Finding) -> RepairOutcome:
    return RepairOutcome(kind=finding.kind.value, reason=finding.reason,
                         tenant_id=finding.tenant_id, task_id=finding.task_id,
                         lease_id=finding.lease_id)


def _open(db: FakeFirestore, attempt_id: str) -> bool:
    return db.doc(f"attempts/{attempt_id}").get("completed_at") is None


# --------------------------------------------------------------------------
# S-1  a reclaim
# --------------------------------------------------------------------------

def test_a_reclaimed_attempt_records_its_end_with_the_reclaim(db, config):
    seed_dead_worker(db, generation=3)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, backend).run_once()

    outcome = report.outcomes[0]
    assert outcome.invalidated_to == 4
    assert outcome.released is True
    attempt = db.doc("attempts/att_1")
    assert attempt["completed_at"] is not None, "the reclaimed attempt was left open"
    assert attempt["exit_code"] is None, "no exit code is known; none may be invented"
    assert attempt["error"].startswith(f"{ATTEMPT_RECLAIMED}: "), attempt["error"]
    assert f"recorded the end of att_1 as {ATTEMPT_RECLAIMED}" in outcome.actions


def test_the_reclaim_writes_the_end_in_its_own_transaction(db, config):
    """The attempt's end is written by the commit that fences: the task is at
    the new generation and the attempt is closed by the same `_apply`."""
    seed_dead_worker(db, generation=3)

    done = store_for(db).fence_release_repair(
        "task_1",
        expected_generation=3,
        lease_id="lease_1",
        release_reason="reconciler:dead_worker",
        attempt_id="att_1",
        attempt_reason="lease silent for 600s",
    )

    assert done.new_generation == 4
    assert done.attempt_ended == ATTEMPT_RECLAIMED
    assert db.doc("attempts/att_1")["error"] == f"{ATTEMPT_RECLAIMED}: lease silent for 600s"


def test_a_reclaim_behind_an_earlier_fence_still_closes_the_superseded_attempt(db):
    """Somebody else fenced generation 3 (a fence-only pass); this transaction
    releases its lease. The attempt is superseded either way."""
    seed_dead_worker(db, generation=3)
    db.documents["tasks/task_1"]["current_generation"] = 4

    done = store_for(db).fence_release_repair(
        "task_1",
        expected_generation=3,
        lease_id="lease_1",
        release_reason="reconciler:orphan_lease",
        attempt_id="att_1",
        attempt_reason="superseded lease",
    )

    assert done.new_generation is None
    assert done.released is True
    assert done.attempt_ended == ATTEMPT_RECLAIMED
    assert not _open(db, "att_1")


# --------------------------------------------------------------------------
# S-2  a fence alone
# --------------------------------------------------------------------------

def test_a_stuck_fence_records_the_attempt_end_as_fenced(db, config):
    seed_dead_worker(db, generation=3)
    rec = build(db, config)
    finding = stuck_finding()

    outcome = rec._repair(finding, None, {})

    assert outcome.invalidated_to == 4
    assert db.doc("tasks/task_1")["current_generation"] == 4
    attempt = db.doc("attempts/att_1")
    assert attempt["completed_at"] is not None, "the fenced attempt was left open"
    assert attempt["exit_code"] is None
    assert attempt["error"] == f"{ATTEMPT_FENCED}: no progress for 1800s"
    assert f"recorded the end of att_1 as {ATTEMPT_FENCED}" in outcome.actions


def test_invalidate_generation_still_fences_without_an_attempt(db):
    """The old entry point keeps its contract: a generation, nothing else."""
    seed_dead_worker(db, generation=3)

    assert store_for(db).invalidate_generation("task_1", 3) == 4
    assert _open(db, "att_1")


# --------------------------------------------------------------------------
# S-3  never a newer generation's attempt
# --------------------------------------------------------------------------

def test_a_newer_generations_attempt_is_untouched_by_a_stale_reclaim(db):
    """The finding was about generation 3; by the transaction the task was
    fenced and re-admitted at generation 4 on lease_2, whose attempt is live.
    The old attempt is closed; the new one is not written."""
    seed_dead_worker(db, generation=3)
    now = utcnow()
    db.documents["tasks/task_1"].update(current_generation=4, current_lease_id="lease_2",
                                        state=TaskState.LEASED.value)
    db.seed("leases/lease_2", {
        "lease_id": "lease_2", "task_id": "task_1", "attempt_id": "att_2",
        "tenant_id": TENANT, "generation": 4, "pools": _pools(), "units": 1,
        "state": TaskState.LEASED.value, "created_at": now, "released_at": None,
        "expires_at": now + timedelta(seconds=120),
    })
    seed_attempt(db, "att_2", generation=4, lease_id="lease_2")

    done = store_for(db).fence_release_repair(
        "task_1",
        expected_generation=3,
        lease_id="lease_1",
        release_reason="reconciler:orphan_lease",
        repair=None,
        attempt_id="att_1",
        attempt_reason="superseded lease",
    )

    assert done.new_generation is None
    assert not _open(db, "att_1")
    assert _open(db, "att_2"), "the newer generation's attempt was written"
    assert db.doc("attempts/att_2")["error"] is None


def test_a_finding_naming_a_newer_attempt_writes_nothing(db):
    """The attempt id must be AT the generation being fenced. A finding whose
    attempt is at the task's current generation is refused, whatever the fence
    decided."""
    seed_dead_worker(db, generation=4)

    fenced = store_for(db).fence_attempt(
        "task_1", 3, attempt_id="att_1", attempt_reason="stale"
    )

    assert fenced.new_generation is None
    assert fenced.attempt_ended is None
    assert _open(db, "att_1")


def test_only_the_attempt_at_the_fenced_generation_is_written(db):
    """The fence of generation 3 commits; the attempt id it was handed names an
    attempt of generation 2. Only an attempt at the generation being fenced is
    this fence's to end."""
    seed_dead_worker(db, generation=3)
    seed_attempt(db, "att_0", generation=2, lease_id="lease_0")

    fenced = store_for(db).fence_attempt(
        "task_1", 3, attempt_id="att_0", attempt_reason="no progress"
    )

    assert fenced.new_generation == 4
    assert fenced.attempt_ended is None
    assert _open(db, "att_0")
    assert _open(db, "att_1")


def test_a_fence_that_lost_its_race_leaves_the_attempt_open(db, config):
    seed_dead_worker(db, generation=3)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    report = build(db, config, backend, store_class=FenceLosesItsRace).run_once()

    assert report.outcomes[0].invalidated_to is None
    assert db.doc("tasks/task_1")["current_generation"] == 3
    assert _open(db, "att_1"), "an attempt at the task's current generation was closed"


def test_a_stuck_fence_that_lost_its_race_leaves_the_attempt_open(db, config):
    seed_dead_worker(db, generation=3)
    rec = build(db, config, store_class=FenceLosesItsRace)

    outcome = rec._repair(stuck_finding(), None, {})

    assert outcome.invalidated_to is None
    assert _open(db, "att_1")


# --------------------------------------------------------------------------
# S-4  the worker's own end wins
# --------------------------------------------------------------------------

def test_an_attempt_its_worker_already_ended_keeps_the_workers_account(db):
    ended = utcnow() - timedelta(seconds=30)
    seed_dead_worker(db, generation=3, completed_at=ended, exit_code=0, error=None)

    fenced = store_for(db).fence_attempt(
        "task_1", 3, attempt_id="att_1", attempt_reason="no progress"
    )

    assert fenced.new_generation == 4
    assert fenced.attempt_ended is None
    attempt = db.doc("attempts/att_1")
    assert (attempt["completed_at"], attempt["exit_code"], attempt["error"]) == (ended, 0, None)


# --------------------------------------------------------------------------
# S-5  dry run
# --------------------------------------------------------------------------

def test_a_dry_run_closes_nothing(db, config):
    seed_dead_worker(db)
    backend = FakeBackend(executions=[running_execution()], journal=db.writes)

    build(db, replace(config, dry_run=True), backend).run_once()
    build(db, replace(config, dry_run=True))._repair(stuck_finding(), None, {})

    assert _open(db, "att_1")
