"""A reclaim re-reads the heartbeat before it acts, and a reclaimed task waits before it runs again (#1822).

WHAT WAS WRONG. Between 07:00Z and 09:00Z on 2026-10-21 three tasks ended
FAILED lost_worker after "lease silent for 104-117 s (grace 90 s)", each
losing all 3 attempts. The reconciler's reclaim (`Reconciler._repair` ->
`ControlStore.fence_release_repair`) acted on a lease snapshot that can be
minutes old by the time slow terminations ahead of it are done, never looked
at `heartbeat_at` again, and requeued with `next_eligible_at = now`: a lapse
with a deterministic cause burned every attempt back to back.

WHAT EACH TEST PINS:

  * a lease whose worker beat AFTER the snapshot, within the grace, is NOT
    reclaimed: its execution is not killed, its generation is not bumped, its
    slot is not released and its task stays where it was;
  * the same check is made INSIDE the reclaim's transaction, on the lease it
    reads there, and an aborted transaction writes nothing at all;
  * a beat newer than the snapshot but itself older than the grace does not
    stop the reclaim, nor does a beat at a superseded generation;
  * a lease still silent IS reclaimed: generation bumped, slot released
    exactly once (a second pass releases nothing), task requeued;
  * the requeue's `next_eligible_at` grows with the attempt number and is
    capped.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore;
only the backend is scripted.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner
from test_worker_evicted import (
    ATTEMPT,
    GENERATION,
    GKE,
    JOB_NAME,
    LEASE,
    NAMESPACE,
    POOLS,
    TASK,
    seed_a_running_attempt,
)
from worker_seeds import TENANT

from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger as build_reconciler_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import (
    REQUEUE_BACKOFF_BASE_SECONDS,
    REQUEUE_BACKOFF_CAP_SECONDS,
    Reconciler,
    requeue_backoff_seconds,
)
from reconciler.store import ControlStore, RepairPlan
from swarm_common.models import utcnow
from swarm_common.states import EventType, TaskState

GRACE = 90


@pytest.fixture
def reconciler_config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=GRACE,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def running_job() -> ExecutionView:
    """The worker's Job, still active as the backend lists it."""
    return ExecutionView(
        name=JOB_NAME,
        backend=GKE,
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(minutes=9),
        task_id=TASK,
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        generation=GENERATION,
        namespace=NAMESPACE,
    )


class BeatsAfterTheSnapshot(ControlStore):
    """The worker heartbeats the moment after this pass took its snapshot."""

    def __init__(self, db: FakeFirestore, *args: Any, **kwargs: Any) -> None:
        super().__init__(db, *args, **kwargs)
        self._fake = db

    def snapshot(self, **kwargs: Any) -> Any:
        taken = super().snapshot(**kwargs)
        self._fake.document(f"leases/{LEASE}").update({"heartbeat_at": utcnow()})
        return taken


def reconcile(
    db: FakeFirestore, config: ReconcilerConfig, *, store_cls: type[ControlStore] = ControlStore
) -> tuple[Any, FakeBackend]:
    logger = build_reconciler_logger(stream=io.StringIO())
    backend = FakeBackend(name=GKE, executions=[running_job()], journal=db.writes)
    store = store_cls(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()
    return report, backend


def pools_active(db: FakeFirestore) -> list[int]:
    return [int(db.doc(f"pools/{pool}")["active"]) for pool in POOLS]


# ---------------------------------------------------------------------------
# The reconciler: a worker that beat after the snapshot is left alone
# ---------------------------------------------------------------------------


def test_a_lease_whose_heartbeat_advanced_after_the_snapshot_is_not_reclaimed(
    db, reconciler_config
):
    seed_a_running_attempt(db, heartbeat_seconds_ago=110)

    report, backend = reconcile(db, reconciler_config, store_cls=BeatsAfterTheSnapshot)

    task = db.doc(f"tasks/{TASK}")
    lease = db.doc(f"leases/{LEASE}")
    # MUTATION: drop the heartbeat re-read in `Reconciler._repair` and the
    # live worker's Job is killed on the stale snapshot.
    assert backend.terminated == [], backend.terminated
    # MUTATION: re-read but still fence and the worker exits as superseded.
    assert task["current_generation"] == GENERATION, task
    # MUTATION: release anyway and the slot is double-booked (invariant 3).
    assert lease.get("released_at") is None, lease
    assert pools_active(db) == [1] * len(POOLS)
    # MUTATION: requeue anyway and the attempt is burned for nothing.
    assert task["state"] == TaskState.RUNNING.value, task
    assert EventType.GENERATION_FENCED.value not in db.event_types(TASK)
    skipped = [o.skipped for o in report.outcomes]
    # MUTATION: abort silently and nothing in the pass report says why.
    assert skipped == ["heartbeat_resumed"], report.as_dict()


def test_a_lease_still_silent_is_reclaimed_and_its_slot_released_exactly_once(
    db, reconciler_config
):
    seed_a_running_attempt(db, heartbeat_seconds_ago=110)

    reconcile(db, reconciler_config)

    task = db.doc(f"tasks/{TASK}")
    # MUTATION: treat any beat (or none) as "resumed" and nothing is ever
    # reclaimed; the generation must move (invariant 5).
    assert task["current_generation"] == GENERATION + 1, task
    assert task["state"] == TaskState.READY.value, task
    assert db.doc(f"leases/{LEASE}").get("released_at") is not None
    assert pools_active(db) == [0] * len(POOLS)

    # A second pass over the reclaimed task finds nothing to release again.
    reconcile(db, reconciler_config)

    # MUTATION: release outside the fenced transaction, or twice, and a pool
    # goes negative or a second LEASE_RELEASED is written (invariants 2, 3).
    assert pools_active(db) == [0] * len(POOLS)
    released = [t for t in db.event_types(TASK) if t == EventType.LEASE_RELEASED.value]
    assert len(released) == 1, db.event_types(TASK)


# ---------------------------------------------------------------------------
# The store: the same check, inside the reclaim's transaction
# ---------------------------------------------------------------------------


def _store(db: FakeFirestore) -> ControlStore:
    logger = build_reconciler_logger(stream=io.StringIO())
    return ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))


def _reclaim(db: FakeFirestore, *, seen: Any) -> Any:
    return _store(db).fence_release_repair(
        TASK,
        expected_generation=GENERATION,
        lease_id=LEASE,
        release_reason="reconciler:stale_lease",
        repair=RepairPlan(
            to_state=TaskState.READY,
            expected_lease_id=LEASE,
            error="reconciled: lease silent",
            next_eligible_at=utcnow(),
        ),
        attempt_id=ATTEMPT,
        attempt_reason="lease silent",
        heartbeat_seen=seen,
        heartbeat_grace_seconds=GRACE,
    )


def test_the_transaction_aborts_on_a_beat_newer_than_the_snapshot(db):
    seed_a_running_attempt(db, heartbeat_seconds_ago=5)
    writes_before = len(db.writes)

    done = _reclaim(db, seen=utcnow() - timedelta(seconds=110))

    # MUTATION: decide on the snapshot's `heartbeat_seen` instead of the
    # lease read in the transaction and this commits the reclaim.
    assert done.heartbeat_resumed is True, done
    assert done.new_generation is None and not done.released, done
    assert done.repaired_to is None and done.attempt_ended is None, done
    # MUTATION: write the fence before checking and the abort leaves a bumped
    # generation behind with the lease still held.
    assert len(db.writes) == writes_before, db.writes[writes_before:]
    assert db.doc(f"tasks/{TASK}")["current_generation"] == GENERATION


def test_a_newer_beat_that_is_itself_past_the_grace_does_not_stop_the_reclaim(db):
    seed_a_running_attempt(db, heartbeat_seconds_ago=GRACE + 20)

    done = _reclaim(db, seen=utcnow() - timedelta(seconds=GRACE + 60))

    # MUTATION: drop the "within the grace" half and a worker that beat once
    # and went silent again is never reclaimed.
    assert done.heartbeat_resumed is False, done
    assert done.new_generation == GENERATION + 1 and done.released, done
    assert done.repaired_to is TaskState.READY, done


def test_a_beat_no_newer_than_the_snapshot_does_not_stop_the_reclaim(db):
    seed_a_running_attempt(db, heartbeat_seconds_ago=110)
    seen = db.doc(f"leases/{LEASE}")["heartbeat_at"]

    done = _reclaim(db, seen=seen)

    # MUTATION: compare with >= and the silent lease the finding was made on
    # stops its own reclaim.
    assert done.heartbeat_resumed is False, done
    assert done.new_generation == GENERATION + 1 and done.released, done


def test_a_beat_on_a_lease_of_another_generation_does_not_stop_the_reclaim(db):
    seed_a_running_attempt(db, heartbeat_seconds_ago=5)
    db.document(f"leases/{LEASE}").update({"generation": GENERATION + 7})

    done = _reclaim(db, seen=utcnow() - timedelta(seconds=110))

    # MUTATION: ignore the generation and a beat that is not this attempt's
    # keeps the finding's attempt alive.
    assert done.heartbeat_resumed is False, done


def test_without_a_grace_the_transaction_does_not_check(db):
    """Every other repair calls the store exactly as before."""
    seed_a_running_attempt(db, heartbeat_seconds_ago=5)

    done = _store(db).fence_release_repair(
        TASK,
        expected_generation=GENERATION,
        lease_id=LEASE,
        release_reason="reconciler:dead_worker",
        repair=RepairPlan(to_state=TaskState.READY, expected_lease_id=LEASE),
    )

    # MUTATION: check on every call and the kill-confirmed repairs, whose
    # worker is already gone, start refusing.
    assert done.heartbeat_resumed is False, done
    assert done.new_generation == GENERATION + 1, done


# ---------------------------------------------------------------------------
# The requeue backs off by attempt number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "seconds"),
    [(0, 30), (1, 30), (2, 120), (3, 480), (4, 900), (9, 900), (10_000, 900)],
)
def test_the_requeue_backoff_grows_with_the_attempt_and_is_capped(attempt, seconds):
    # MUTATION: a constant backoff, or none, fails every row past the first;
    # drop the cap and rows 4+ wait half an hour or overflow.
    assert requeue_backoff_seconds(attempt) == seconds
    assert REQUEUE_BACKOFF_BASE_SECONDS <= seconds <= REQUEUE_BACKOFF_CAP_SECONDS


@pytest.mark.parametrize("attempt_count", [1, 2])
def test_a_reclaimed_task_is_eligible_only_after_its_backoff(
    db, reconciler_config, attempt_count
):
    seed_a_running_attempt(db, attempt_count=attempt_count, heartbeat_seconds_ago=110)
    before = utcnow()

    reconcile(db, reconciler_config)
    after = utcnow()

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.READY.value, task
    wait = timedelta(seconds=requeue_backoff_seconds(attempt_count))
    # MUTATION: requeue with `next_eligible_at = utcnow()` and the task is
    # dispatched again the same second, burning its attempts back to back.
    assert before + wait <= task["next_eligible_at"] <= after + wait, task


class BeatsAfterThePreKillRead(ControlStore):
    """The worker beats after the pass's own re-read, before its transaction."""

    def __init__(self, db: FakeFirestore, *args: Any, **kwargs: Any) -> None:
        super().__init__(db, *args, **kwargs)
        self._fake = db

    def lease_heartbeat_resumed(self, lease_id: str, **kwargs: Any) -> bool:
        resumed = super().lease_heartbeat_resumed(lease_id, **kwargs)
        self._fake.document(f"leases/{lease_id}").update({"heartbeat_at": utcnow()})
        return resumed


def test_a_beat_between_the_reread_and_the_transaction_stops_a_reclaim_that_killed_nothing(
    db, reconciler_config
):
    seed_a_running_attempt(db, heartbeat_seconds_ago=110)
    logger = build_reconciler_logger(stream=io.StringIO())
    # No execution listed, and none found by name: there is nothing to kill.
    backend = FakeBackend(name=GKE, executions=[], journal=db.writes)
    store = BeatsAfterThePreKillRead(db, logger=logger, txn_runner=FakeTransactionRunner(db))

    # Long enough that the missing-execution rule leaves this lease to the
    # silence rule, which is the one under test.
    config = replace(reconciler_config, missing_execution_grace_seconds=3600)

    report = Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()

    task = db.doc(f"tasks/{TASK}")
    # MUTATION: drop the `heartbeat_grace_seconds` the reclaim passes to
    # `fence_release_repair` and this beat, newer than the pre-kill read,
    # is missed: the task is fenced and requeued under a live worker.
    assert task["current_generation"] == GENERATION, (task, report.as_dict())
    assert task["state"] == TaskState.RUNNING.value, task
    assert db.doc(f"leases/{LEASE}").get("released_at") is None
    assert pools_active(db) == [1] * len(POOLS)
    assert backend.terminated == []
