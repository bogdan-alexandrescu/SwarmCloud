"""A failed dispatch records the end of the attempt it created (#630).

`_admit_one` writes the attempt document before it dispatches. When the
dispatch failed, `return_to_ready_after_failed_dispatch` gave the lease back and
returned the task, and the attempt was left with no `completed_at` and no
`exit_code` for ever: no container was ever started for it, so nothing else
would write its end. The 2026-10-05 history analysis counted those among 183
open attempts that inflated every duration and concurrency figure.

Pinned here, each against the production `SchedulerStore` over the in-memory
Firestore the control-plane suite uses:

  D-1  A dispatch that failed with the task still LEASED on its lease closes
       the attempt, cause `dispatch_failed`, in the transaction that returns
       the task -- through `drain()` and through the store.
  D-2  A task its worker walked past LEASED anyway (the create call reported
       failure but the execution exists) keeps its attempt open: that attempt
       is running, and its worker writes its own end.
  D-3  A reclaim the reconciler left pending (fenced, lease unreleased) keeps
       its attempt open: the reconciler decides it.
  D-4  A task fenced and re-admitted while the dispatch hung: the failed
       dispatch's own (older) attempt is closed and the newer generation's
       attempt is not written (invariant 5).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import (  # noqa: E402
    RecordingDispatcher,
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeFirestore  # noqa: E402

from swarm_common.admission import AdmissionConfig  # noqa: E402
from swarm_common.models import EndCause, Lease, Task  # noqa: E402
from swarm_common.states import TaskState  # noqa: E402

from agent_worker.control import ControlPlane  # noqa: E402
from agent_worker.logs import build_logger as worker_logger  # noqa: E402
from reconciler.logs import build_logger as reconciler_logger  # noqa: E402
from reconciler.store import ControlStore  # noqa: E402
from scheduler.dispatch import BackendRouter  # noqa: E402
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.store import SchedulerStore  # noqa: E402

TENANT = "eng"
BACKEND = "CLOUD_RUN_JOB"
CAUSE = EndCause.DISPATCH_FAILED.value


@pytest.fixture
def db() -> FakeFirestore:
    store = FakeFirestore()
    seed_tenant(store, TENANT, max_active=10)
    seed_pool(store, "global", hard_limit=10)
    return store


def _admitted(db: FakeFirestore, task_id: str) -> tuple[Task, Lease]:
    """What the drain holds after admitting a task and writing its attempt."""
    seed_task(db, task_id=task_id, tenant_id=TENANT)
    store = SchedulerStore(db)
    snapshot = store.get_task(task_id)
    lease = store.acquire_lease(snapshot, units=1, backend=BACKEND, config=AdmissionConfig())
    store.create_attempt(snapshot, lease, BACKEND)
    return snapshot, lease


def _attempt(db: FakeFirestore, lease: Lease) -> dict[str, Any]:
    return db.docs[f"attempts/{lease.attempt_id}"]


def _reconciler() -> Any:
    return reconciler_logger(stream=io.StringIO())


# --------------------------------------------------------------------------
# D-1
# --------------------------------------------------------------------------

def test_a_failed_dispatch_closes_its_attempt_through_the_drain(db):
    seed_task(db, task_id="task_d1", tenant_id=TENANT)
    settings = scheduler_settings()
    dispatcher = RecordingDispatcher(fail_for={"task_d1"})
    scheduler = Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(),
    )

    report = scheduler.drain()

    assert report.dispatch_failures >= 1
    attempts = [d for k, d in db.docs.items() if k.startswith("attempts/")]
    assert attempts, "the drain wrote no attempt"
    for attempt in attempts:
        assert attempt["completed_at"] is not None, "a failed dispatch left its attempt open"
        assert attempt["exit_code"] is None, "no container ran; no exit code may be invented"
        assert attempt["error"].startswith(f"{CAUSE}: "), attempt["error"]


def test_the_store_closes_the_attempt_in_the_returning_transaction(db):
    snapshot, lease = _admitted(db, "task_d2")

    outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
        snapshot, lease, "cloud_run_run_job_failed"
    )

    assert outcome.applied and outcome.target == TaskState.READY.value
    attempt = _attempt(db, lease)
    assert attempt["completed_at"] is not None
    assert attempt["exit_code"] is None
    assert attempt["error"] == f"{CAUSE}: cloud_run_run_job_failed"


def test_a_failed_dispatch_on_the_last_attempt_closes_it_too(db):
    snapshot, lease = _admitted(db, "task_d3")
    db.docs["tasks/task_d3"]["attempt_count"] = 3

    outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
        snapshot, lease, "gke_create_job_failed"
    )

    assert outcome.target == TaskState.FAILED.value
    assert _attempt(db, lease)["error"] == f"{CAUSE}: gke_create_job_failed"


# --------------------------------------------------------------------------
# D-2, D-3: an attempt that may still run is not this path's
# --------------------------------------------------------------------------

def test_an_attempt_whose_worker_started_anyway_stays_open(db):
    snapshot, lease = _admitted(db, "task_d4")
    control = ControlPlane(
        db, task_id="task_d4", attempt_id=lease.attempt_id, lease_id=lease.lease_id,
        tenant_id=TENANT, generation=lease.generation,
        logger=worker_logger(task_id="task_d4", attempt_id=lease.attempt_id,
                             tenant_id=TENANT, generation=lease.generation,
                             runner_profile="mock", stream=io.StringIO()),
    )
    control.validate_generation()
    control.advance_to_running()

    SchedulerStore(db).return_to_ready_after_failed_dispatch(
        snapshot, lease, "cloud_run_run_job_failed"
    )

    assert db.docs["tasks/task_d4"]["state"] == "RUNNING"
    assert _attempt(db, lease)["completed_at"] is None, (
        "the attempt of a running worker was closed by a dispatch error"
    )


def test_a_reclaim_left_pending_keeps_the_attempt_for_the_reconciler(db):
    snapshot, lease = _admitted(db, "task_d5")
    # The reconciler fenced (a generation bump only) and has not released.
    db.docs["tasks/task_d5"]["current_generation"] = lease.generation + 1

    SchedulerStore(db).return_to_ready_after_failed_dispatch(
        snapshot, lease, "cloud_run_run_job_failed"
    )

    assert db.docs[f"leases/{lease.lease_id}"]["released_at"] is None
    assert _attempt(db, lease)["completed_at"] is None


# --------------------------------------------------------------------------
# D-4: only the attempt of this dispatch's generation
# --------------------------------------------------------------------------

def test_a_newer_generations_attempt_is_untouched(db):
    snapshot, first = _admitted(db, "task_d6")
    # The dispatch hung past its deadline: the reconciler reclaimed it through
    # its own store (fence, release, requeue) WITHOUT closing the attempt --
    # as it did before #630 -- and a second admission re-leased the task.
    control = ControlStore(db, logger=_reconciler())
    assert control.invalidate_generation("task_d6", first.generation) == first.generation + 1
    control.release_lease(first.lease_id, "stale_lease")
    assert control.repair_task_state(
        "task_d6", to_state=TaskState.READY, expected_lease_id=first.lease_id
    ) is TaskState.READY
    store = SchedulerStore(db)
    second = store.acquire_lease(
        store.get_task("task_d6"), units=1, backend=BACKEND, config=AdmissionConfig()
    )
    store.create_attempt(store.get_task("task_d6"), second, BACKEND)

    store.return_to_ready_after_failed_dispatch(snapshot, first, "cloud_run_run_job_failed")

    assert db.docs["tasks/task_d6"]["current_lease_id"] == second.lease_id
    assert _attempt(db, first)["error"] == f"{CAUSE}: cloud_run_run_job_failed"
    newer = _attempt(db, second)
    assert (newer["completed_at"], newer["error"]) == (None, None), (
        "the newer generation's attempt was written by an older dispatch's failure"
    )


def test_an_attempt_already_ended_keeps_its_own_end(db):
    snapshot, lease = _admitted(db, "task_d7")
    db.docs[f"attempts/{lease.attempt_id}"].update(error="worker: exit 78", exit_code=78)
    marker = db.docs[f"attempts/{lease.attempt_id}"]["created_at"]
    db.docs[f"attempts/{lease.attempt_id}"]["completed_at"] = marker

    SchedulerStore(db).return_to_ready_after_failed_dispatch(
        snapshot, lease, "cloud_run_run_job_failed"
    )

    attempt = _attempt(db, lease)
    assert (attempt["completed_at"], attempt["error"], attempt["exit_code"]) == (
        marker, "worker: exit 78", 78
    )


def test_an_attempt_document_of_another_generation_is_not_written(db):
    """The attempt is found by this lease's attempt id; it is written only if it
    is at this lease's generation and of this task, whatever the id says."""
    snapshot, lease = _admitted(db, "task_d8")
    db.docs[f"attempts/{lease.attempt_id}"]["generation"] = lease.generation + 1

    SchedulerStore(db).return_to_ready_after_failed_dispatch(
        snapshot, lease, "cloud_run_run_job_failed"
    )

    assert db.docs["tasks/task_d8"]["state"] == "READY"
    assert _attempt(db, lease)["completed_at"] is None
