"""The scheduler writes a transition only onto the state it decided it from.

F-9's remainder, from the wf_ebb3ab2d65664707a559 incident analysis (section 4),
and the note on PR #31's review that `return_to_ready_after_failed_dispatch`
read the task inside a transaction and then wrote it with no state
precondition. PR #30 made the API's side of the race (`request_cancel`)
transactional. This file is the scheduler's side.

THE DEFECT. The drain decides from a snapshot -- the READY slice it queried, the
PARKED rows a sweep listed, the lease admission handed back -- and then wrote
with a blind `ref.update(...)`. Anything another process committed between that
read and the write was overwritten by a decision about a document that no
longer existed:

  park, cancel       a READY task the API had cancelled, or that another
                     scheduler had leased;
  promote_to_ready   a PARKED task the API had cancelled, that another scheduler
                     had already promoted and leased, or that a worker had
                     re-parked for a different reason;
  mark_dispatched    a task whose worker had started first and walked it to
                     RUNNING itself (`ControlPlane.advance_to_running` does
                     exactly that when it lands at LEASED), or one the
                     reconciler had already fenced and returned to READY;
  return_to_ready    a task the reconciler had fenced and a second admission had
                     re-leased, a task whose worker was running despite the
                     error, a task that had already finished -- and it released
                     the lease BEFORE looking, so capacity came back under a
                     live worker;
  record_blockers    a "not_ready" denial written as the blocker of the task
                     another scheduler had just admitted.

THE RULE NOW. Each of those is one Firestore transaction that re-reads the task
and writes only if it is still in the state the decision was made against --
plus the lease and generation for the two writes that follow admission, and the
park reason for a promotion. When it is not, the write is SKIPPED, never
forced: logged, and counted in `DrainReport.stale_writes` and
`swarm_scheduler_stale_writes_total{write, reason}`.

HOW EACH INTERLEAVING IS DRIVEN. The way it happens: the drain holds its
snapshot, the concurrent writer commits through ITS OWN production code -- the
API's `Store.request_cancel`, the reconciler's `ControlStore`, the worker's
`ControlPlane`, a second `SchedulerStore.acquire_lease` -- and then the stale
write runs. Three tests (`TestTheCheckAndTheWriteAreOneTransaction`) go further
and land the concurrent write BETWEEN the store's own transactional read and its
commit, using PR #30's `ContendedFirestore`, whose commit aborts when its read
set changed so that the real `firestore.transactional` re-runs the body. That is
what distinguishes one transaction from a pre-read followed by a blind write.
The fake is imported from that file rather than copied, so there is one
contended fake in this repository, not two drifting apart.

A RECLAIM THE RECONCILER DID NOT FINISH (`TestAPartialReclaim`). The
reconciler's repair is four transactions -- invalidate, terminate, release,
repair -- and it stops after the first when termination is unconfirmed, or
wherever its instance dies. `_reconciler_reclaims` runs all four, so the first
version of this file never drove the two states a partial reclaim leaves: the
task still pointing at the lease at a bumped generation, with the lease
unreleased (stopped after step 1) or released (stopped after step 3). A failed
dispatch landing on either used to release and walk away, and the task sat
holding capacity on a released lease that no reconciler rule looks at. Those
tests run the reconciler's NEXT pass for real -- the production `Reconciler`
over the production `ControlStore` -- because "the reconciler will pick it up"
is the claim being tested, not an assumption to build on.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any, Callable

import pytest

from swarm_common.admission import AdmissionConfig
from swarm_common.models import EndCause, Lease, Task, utcnow
from swarm_common.states import ParkReason, TaskState

from agent_worker.control import ControlPlane
from agent_worker.logs import build_logger as worker_logger
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger as reconciler_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import ReconcileReport, Reconciler
from reconciler.store import ControlStore
from scheduler.dispatch import BackendRouter, DispatchError
from scheduler.loop import DrainReport, Scheduler
from scheduler.metrics import SchedulerMetrics
from scheduler.store import SchedulerStore

from .conftest import RecordingDispatcher, scheduler_settings, seed_pool, seed_task, seed_tenant
from .test_request_cancel_is_transactional import ContendedFirestore

TENANT = "eng"
BACKEND = "CLOUD_RUN_JOB"
ADMISSION = AdmissionConfig()
BY = "alice@saga.xyz"


@pytest.fixture
def db() -> ContendedFirestore:
    """Overrides conftest's `db`, so `api_store` and `make_scheduler` share it.

    With no interleaving registered it behaves exactly like `FakeFirestore`.
    """
    return ContendedFirestore()


# --------------------------------------------------------------------------
# The concurrent writers, each through its own production code
# --------------------------------------------------------------------------

def _world(db: ContendedFirestore) -> None:
    seed_tenant(db, TENANT, max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _doc(db: ContendedFirestore, task_id: str) -> dict[str, Any]:
    return db.docs[f"tasks/{task_id}"]


def _events(db: ContendedFirestore, task_id: str, kind: str) -> list[dict[str, Any]]:
    return [e for e in db.collection_docs(f"tasks/{task_id}/events") if e.get("type") == kind]


def _held(db: ContendedFirestore) -> int:
    return int(db.docs["pools/global"]["active"])


def _admit(db: ContendedFirestore, task_id: str) -> Lease:
    """Another scheduler admits the task: the frozen admission transaction."""
    store = SchedulerStore(db)
    return store.acquire_lease(
        store.get_task(task_id), units=1, backend=BACKEND, config=ADMISSION
    )


def _admitted(db: ContendedFirestore, task_id: str) -> tuple[Task, Lease]:
    """What THIS drain holds after admitting a task and before its next write.

    The Task is the READY snapshot the drain read; `acquire_lease_in_transaction`
    changes the document and never that object. The attempt document exists,
    because `_admit_one` writes it before it dispatches.
    """
    store = SchedulerStore(db)
    snapshot = store.get_task(task_id)
    lease = store.acquire_lease(snapshot, units=1, backend=BACKEND, config=ADMISSION)
    store.create_attempt(snapshot, lease, BACKEND)
    return snapshot, lease


def _reconciler_reclaims(db: ContendedFirestore, task_id: str, lease: Lease) -> None:
    """The reconciler's repair of a lease it judged dead, through its own store:
    fence the generation, release the lease, return the task to READY."""
    store = ControlStore(db, logger=reconciler_logger(stream=io.StringIO()))
    assert store.invalidate_generation(task_id, lease.generation) == lease.generation + 1
    store.release_lease(lease.lease_id, "stale_lease")
    repaired = store.repair_task_state(
        task_id, to_state=TaskState.READY, expected_lease_id=lease.lease_id
    )
    assert repaired is TaskState.READY, "the reconciler's repair did not land"


def _worker(db: ContendedFirestore, task_id: str, lease: Lease) -> ControlPlane:
    return ControlPlane(
        db,
        task_id=task_id,
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
        tenant_id=TENANT,
        generation=lease.generation,
        logger=worker_logger(
            task_id=task_id,
            attempt_id=lease.attempt_id,
            tenant_id=TENANT,
            generation=lease.generation,
            runner_profile="mock",
            stream=io.StringIO(),
        ),
    )


def _worker_starts(db: ContendedFirestore, task_id: str, lease: Lease) -> ControlPlane:
    """A worker that starts before the scheduler's `mark_dispatched` lands.

    It passes fencing and walks LEASED -> DISPATCHED -> STARTING -> RUNNING
    itself -- `advance_to_running` exists for precisely this ordering.
    """
    control = _worker(db, task_id, lease)
    control.validate_generation()
    control.advance_to_running()
    assert _doc(db, task_id)["state"] == "RUNNING", "the worker did not start"
    return control


def _stale_writes(scheduler: Scheduler, write: str, reason: str) -> float:
    """`swarm_scheduler_stale_writes_total{write=..., reason=...}`, or 0."""
    rendered = scheduler.metrics.render()[0].decode("utf-8")
    total = 0.0
    for line in rendered.splitlines():
        if not line.startswith("swarm_scheduler_stale_writes_total{"):
            continue
        labels, value = line.rsplit(" ", 1)
        if f'write="{write}"' in labels and f'reason="{reason}"' in labels:
            total += float(value)
    return total


class _InterleavedStore(SchedulerStore):
    """The production store, with one concurrent writer landed right after a
    query returns rows -- the drain has READ and has not yet WRITTEN."""

    def __init__(self, db: Any, *, after: str, writer: Callable[[], None]) -> None:
        super().__init__(db)
        self._after = after
        self._writer: Callable[[], None] | None = writer
        self.fired = False

    def _fire(self, name: str, rows: list[Task]) -> list[Task]:
        if rows and name == self._after and self._writer is not None:
            writer, self._writer = self._writer, None
            writer()
            self.fired = True
        return rows

    def parked_tasks(self, reason: ParkReason, limit: int) -> list[Task]:
        return self._fire("parked_tasks", super().parked_tasks(reason, limit))

    def ready_tasks(self, limit: int) -> list[Task]:
        return self._fire("ready_tasks", super().ready_tasks(limit))


def _scheduler(store: SchedulerStore, dispatcher: Any) -> Scheduler:
    settings = scheduler_settings()
    return Scheduler(
        settings=settings,
        store=store,
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(),
    )


# --------------------------------------------------------------------------
# park
# --------------------------------------------------------------------------

class TestPark:
    def test_a_cancel_that_landed_after_the_read_is_not_parked_over(self, db, api_store):
        """Parked MANUAL_PAUSE, a task the user cancelled is resurrected into
        the one park reason nothing ever promotes."""
        _world(db)
        seed_task(db, task_id="task_p1", tenant_id=TENANT)
        store = SchedulerStore(db)
        snapshot = store.get_task("task_p1")
        api_store.request_cancel(TENANT, "task_p1", by=BY)
        cancelled_at = _doc(db, "task_p1")["completed_at"]

        outcome = store.park(
            snapshot, ParkReason.MANUAL_PAUSE, detail={"error": "tenant is missing or disabled"}
        )

        stored = _doc(db, "task_p1")
        assert stored["state"] == "CANCELLED", (
            f"the task is {stored['state']}: park wrote its READY-snapshot "
            "decision over the user's cancel"
        )
        assert stored["completed_at"] == cancelled_at
        assert stored["park_reason"] is None
        assert _events(db, "task_p1", "parked") == [], "an event for a write that did not happen"
        assert outcome.applied is False
        assert (outcome.write, outcome.reason, outcome.found) == ("park", "state_changed", "CANCELLED")

    def test_a_lease_another_scheduler_took_after_the_read_is_not_parked_over(self, db):
        """PARKED with a live lease: pools counted for a task no drain will
        ever dispatch or release."""
        _world(db)
        seed_task(db, task_id="task_p2", tenant_id=TENANT)
        store = SchedulerStore(db)
        snapshot = store.get_task("task_p2")
        lease = _admit(db, "task_p2")

        outcome = store.park(
            snapshot, ParkReason.CREDENTIAL_MISSING, detail={"provider": "anthropic"}
        )

        stored = _doc(db, "task_p2")
        assert stored["state"] == "LEASED", (
            f"the task is {stored['state']} while lease {lease.lease_id} holds its slot"
        )
        assert stored["current_lease_id"] == lease.lease_id
        assert _held(db) == 1
        assert outcome.applied is False and outcome.reason == "state_changed"


# --------------------------------------------------------------------------
# promote_to_ready
# --------------------------------------------------------------------------

class TestPromote:
    def _parked_child(self, db) -> None:
        _world(db)
        seed_task(db, task_id="task_parent", tenant_id=TENANT, state="SUCCEEDED")
        seed_task(
            db,
            task_id="task_child",
            tenant_id=TENANT,
            state="PARKED",
            park_reason="DEPENDENCY_INCOMPLETE",
            depends_on=("task_parent",),
        )

    def test_a_cancel_that_landed_after_the_sweep_read_is_not_promoted_over(self, db, api_store):
        self._parked_child(db)
        store = SchedulerStore(db)
        snapshot = store.get_task("task_child")
        api_store.request_cancel(TENANT, "task_child", by=BY)

        outcome = store.promote_to_ready(snapshot, detail={"reason": "dependencies_satisfied"})

        stored = _doc(db, "task_child")
        assert stored["state"] == "CANCELLED", (
            f"the task is {stored['state']}: a cancelled task was promoted back "
            "into the admission queue"
        )
        assert _events(db, "task_child", "ready") == []
        assert (outcome.applied, outcome.reason, outcome.found) == (False, "state_changed", "CANCELLED")

    def test_a_task_the_worker_re_parked_for_another_reason_is_not_promoted(self, db):
        """The sweep read PARKED(DEPENDENCY_INCOMPLETE). Before its write, another
        drain promoted and admitted the task, and its worker parked it on quota
        until the provider's window reopens. Promoting it on the dependency
        decision admits it before that window -- a container started on a
        guess, the one thing `_prewarm`'s pool guard exists to prevent."""
        self._parked_child(db)
        store = SchedulerStore(db)
        snapshot = store.get_task("task_child")

        other = SchedulerStore(db)
        other.promote_to_ready(other.get_task("task_child"), detail={"reason": "dependencies_satisfied"})
        _, lease = _admitted(db, "task_child")
        reopens = utcnow() + timedelta(hours=1)
        _worker_starts(db, "task_child", lease).park(
            reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED, next_eligible_at=reopens
        )
        assert _doc(db, "task_child")["park_reason"] == "PROVIDER_QUOTA_EXHAUSTED"

        outcome = store.promote_to_ready(snapshot, detail={"reason": "dependencies_satisfied"})

        stored = _doc(db, "task_child")
        assert stored["state"] == "PARKED", (
            "a task parked on quota was promoted on a decision about its "
            "dependencies"
        )
        assert stored["park_reason"] == "PROVIDER_QUOTA_EXHAUSTED"
        assert stored["next_eligible_at"] == reopens
        assert (outcome.applied, outcome.reason) == (False, "park_reason_changed")

    def test_the_sweep_does_not_promote_a_task_another_scheduler_already_leased(self, db):
        """THE DOUBLE ADMISSION. The sweep listed the task PARKED; another
        scheduler then promoted and leased it. The stale promotion wrote READY
        over LEASED, the same drain found it READY and leased it a second time,
        and the first lease was left holding capacity for an attempt that had
        been fenced out from under it."""
        self._parked_child(db)
        taken: list[Lease] = []

        def another_scheduler_takes_it() -> None:
            other = SchedulerStore(db)
            other.promote_to_ready(
                other.get_task("task_child"), detail={"reason": "dependencies_satisfied"}
            )
            taken.append(_admit(db, "task_child"))

        dispatcher = RecordingDispatcher()
        store = _InterleavedStore(db, after="parked_tasks", writer=another_scheduler_takes_it)
        scheduler = _scheduler(store, dispatcher)

        report = scheduler.drain()

        assert store.fired, "the concurrent admission never ran; this test raced nothing"
        stored = _doc(db, "task_child")
        assert stored["current_lease_id"] == taken[0].lease_id, (
            f"the task moved from lease {taken[0].lease_id} to "
            f"{stored['current_lease_id']}: it was admitted twice"
        )
        assert stored["state"] == "LEASED"
        assert dispatcher.dispatched == [], "this drain dispatched a task another scheduler held"
        assert _held(db) == 1, "two leases are holding one task's slot"
        assert report.promoted_dependencies == 0
        assert report.stale_writes == 1
        assert _stale_writes(scheduler, "promote", "state_changed") == 1.0


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------

class TestCancel:
    def test_a_task_another_scheduler_leased_after_the_read_is_not_cancelled_over(self, db):
        """CANCELLED over LEASED is a terminal task holding a live lease: its
        pools stay counted and nothing in its record says a container exists."""
        _world(db)
        seed_task(db, task_id="task_c1", tenant_id=TENANT)
        store = SchedulerStore(db)
        snapshot = store.get_task("task_c1")
        lease = _admit(db, "task_c1")

        outcome = store.cancel(
            snapshot,
            "an upstream workflow step did not succeed",
            {"failed_parents": ["task_parent"]},
            # Required since contract request 23: every cancel says why.
            end_cause=EndCause.FAILED_PARENT,
        )

        stored = _doc(db, "task_c1")
        assert stored["state"] == "LEASED", (
            f"the task is {stored['state']} while lease {lease.lease_id} holds its slot"
        )
        assert stored["completed_at"] is None
        assert stored["current_lease_id"] == lease.lease_id
        assert _held(db) == 1
        assert (outcome.applied, outcome.reason, outcome.found) == (False, "state_changed", "LEASED")

    def test_the_drain_does_not_re_cancel_what_the_user_already_cancelled(
        self, db, api_store, make_scheduler
    ):
        """The drain read READY, found the parent FAILED, and cancelled -- after
        the user had. The task's record then said the upstream step killed it,
        and its history held two cancellations; the drain counted one."""
        _world(db)
        seed_task(db, task_id="task_parent", tenant_id=TENANT, state="FAILED")
        seed_task(db, task_id="task_c2", tenant_id=TENANT, depends_on=("task_parent",))
        scheduler = make_scheduler()
        snapshot = scheduler.store.get_task("task_c2")
        api_store.request_cancel(TENANT, "task_c2", by=BY)

        report = DrainReport()
        scheduler._admit_one(snapshot, report)

        stored = _doc(db, "task_c2")
        assert stored["last_error"] is None, (
            f"last_error={stored['last_error']!r}: the drain rewrote why the task "
            "ended over the user's own cancel"
        )
        assert len(_events(db, "task_c2", "cancelled")) == 1
        assert report.cancelled == 0
        assert report.stale_writes == 1
        assert _stale_writes(scheduler, "cancel", "state_changed") == 1.0


# --------------------------------------------------------------------------
# mark_dispatched
# --------------------------------------------------------------------------

class TestMarkDispatched:
    def test_a_worker_that_started_first_is_not_rewound_to_dispatched(self, db):
        """The worker walked the task to RUNNING before the scheduler's write
        landed. DISPATCHED over RUNNING is not just a wrong label: DISPATCHED ->
        SUCCEEDED is illegal, so the worker could no longer finish its own task."""
        _world(db)
        seed_task(db, task_id="task_d1", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_d1")
        control = _worker_starts(db, "task_d1", lease)

        outcome = SchedulerStore(db).mark_dispatched(
            snapshot, lease, "executions/task_d1-1", BACKEND
        )

        assert _doc(db, "task_d1")["state"] == "RUNNING", (
            f"the task is {_doc(db, 'task_d1')['state']}: mark_dispatched rewound "
            "a running worker"
        )
        # The LEASE still moves. After its first heartbeat the reconciler treats
        # a lease still at LEASED past its dispatch deadline as overdue
        # (detect.py `overdue_dispatch`), so leaving it would reap a healthy
        # worker five minutes in.
        assert db.docs[f"leases/{lease.lease_id}"]["state"] == "DISPATCHED"
        assert db.docs[f"attempts/{lease.attempt_id}"]["execution_name"] == "executions/task_d1-1"
        assert _events(db, "task_d1", "dispatched") == []
        assert (outcome.applied, outcome.reason, outcome.found) == (False, "state_changed", "RUNNING")

        control.finish(state=TaskState.SUCCEEDED, exit_code=0)
        assert _doc(db, "task_d1")["state"] == "SUCCEEDED"
        assert _held(db) == 0

    def test_a_lease_the_reconciler_reclaimed_is_not_marked_dispatched(self, db):
        """DISPATCHED over READY, with the lease already released: a task that
        claims to hold capacity it does not, and that no drain will look at."""
        _world(db)
        seed_task(db, task_id="task_d2", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_d2")
        _reconciler_reclaims(db, "task_d2", lease)

        outcome = SchedulerStore(db).mark_dispatched(
            snapshot, lease, "executions/task_d2-1", BACKEND
        )

        stored = _doc(db, "task_d2")
        assert stored["state"] == "READY", (
            f"the task is {stored['state']} on released lease {lease.lease_id}"
        )
        assert stored["current_lease_id"] is None
        assert stored["current_generation"] == lease.generation + 1
        released = db.docs[f"leases/{lease.lease_id}"]
        assert released["released_at"] is not None
        assert released["state"] == "LEASED", "a released lease was relabelled as dispatched"
        assert _events(db, "task_d2", "dispatched") == []
        assert (outcome.applied, outcome.reason, outcome.found) == (False, "lease_superseded", "READY")

    def test_a_cancel_requested_while_leased_survives_the_dispatch(self, db, api_store):
        """A GUARD, green before and after. The API flags a LEASED task and
        leaves its state alone; the container has been created, and the worker
        is what honours the flag. The write must move the state and keep the
        flag -- a guard that refused here would strand a live execution at
        LEASED."""
        _world(db)
        seed_task(db, task_id="task_d3", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_d3")
        api_store.request_cancel(TENANT, "task_d3", by=BY)
        assert _doc(db, "task_d3")["state"] == "LEASED"

        SchedulerStore(db).mark_dispatched(snapshot, lease, "executions/task_d3-1", BACKEND)

        stored = _doc(db, "task_d3")
        assert stored["state"] == "DISPATCHED"
        assert stored["cancel_requested"] is True

    def test_the_drain_counts_a_dispatch_the_worker_overtook(self, db):
        """Through `drain()`: a backend whose container starts before `run_job`
        returns. The dispatch still counts -- the execution exists -- and so
        does the write it did not make."""

        class StartsBeforeItReturns:
            def dispatch(self, *, task, lease, profile, tenant):  # noqa: ANN001
                _worker_starts(db, task.id, lease)
                return f"executions/{task.id}-{lease.generation}"

        _world(db)
        seed_task(db, task_id="task_d5", tenant_id=TENANT)
        scheduler = _scheduler(SchedulerStore(db), StartsBeforeItReturns())

        report = scheduler.drain()

        assert _doc(db, "task_d5")["state"] == "RUNNING"
        assert report.dispatched == 1
        assert report.stale_writes == 1
        assert _stale_writes(scheduler, "mark_dispatched", "state_changed") == 1.0


# --------------------------------------------------------------------------
# return_to_ready_after_failed_dispatch
# --------------------------------------------------------------------------

class TestReturnToReady:
    def test_a_failed_dispatch_does_not_orphan_the_lease_a_later_admission_took(self, db):
        """The dispatch hung past its deadline. The reconciler fenced it and a
        second admission re-leased the task. The first dispatch's error then
        came back and wrote READY, `current_lease_id=None` over the second
        lease: its slot held by nothing, and the next drain leasing it again."""
        _world(db)
        seed_task(db, task_id="task_e1", tenant_id=TENANT)
        snapshot, first = _admitted(db, "task_e1")
        _reconciler_reclaims(db, "task_e1", first)
        second = _admit(db, "task_e1")

        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, first, "cloud_run_run_job_failed"
        )

        stored = _doc(db, "task_e1")
        assert stored["state"] == "LEASED", (
            f"the task is {stored['state']}: lease {second.lease_id} was orphaned "
            "by a failure that belonged to the lease before it"
        )
        assert stored["current_lease_id"] == second.lease_id
        assert db.docs[f"leases/{second.lease_id}"]["released_at"] is None
        assert _held(db) == 1
        assert (outcome.applied, outcome.reason) == (False, "lease_superseded")

    def test_a_failed_dispatch_does_not_release_capacity_under_a_running_worker(self, db):
        """The create call reported failure -- a timeout, say -- but the
        execution had been created and its worker is RUNNING. Releasing the
        lease there frees a slot a live container occupies (CONTRACT invariants
        1 and 3), and READY over RUNNING admits a second worker onto it."""
        _world(db)
        seed_task(db, task_id="task_e2", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_e2")
        _worker_starts(db, "task_e2", lease)

        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, lease, "cloud_run_run_job_failed"
        )

        assert db.docs[f"leases/{lease.lease_id}"]["released_at"] is None, (
            "the scheduler released the lease of a running worker"
        )
        assert _held(db) == 1
        stored = _doc(db, "task_e2")
        assert stored["state"] == "RUNNING"
        assert stored["current_lease_id"] == lease.lease_id
        assert (outcome.applied, outcome.reason, outcome.found) == (False, "state_changed", "RUNNING")

    def test_a_failed_dispatch_does_not_rerun_a_task_that_already_succeeded(self, db):
        """The same late error, after the worker had finished. READY over
        SUCCEEDED runs completed work again."""
        _world(db)
        seed_task(db, task_id="task_e3", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_e3")
        _worker_starts(db, "task_e3", lease).finish(state=TaskState.SUCCEEDED, exit_code=0)

        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, lease, "cloud_run_run_job_failed"
        )

        stored = _doc(db, "task_e3")
        assert stored["state"] == "SUCCEEDED", f"a finished task was returned to {stored['state']}"
        assert _held(db) == 0
        assert (outcome.applied, outcome.reason, outcome.found) == (False, "state_changed", "SUCCEEDED")

    def test_a_task_cancelled_by_hand_stays_cancelled_and_gets_its_capacity_back(self, db):
        """Remediation R2 in the incident analysis: an operator writes the state
        only and leaves the lease for someone to release. The failed dispatch
        is that someone -- its lease backs nothing -- and it must not also
        write the task back to READY."""
        _world(db)
        seed_task(db, task_id="task_e4", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_e4")
        _doc(db, "task_e4").update(
            {
                "state": "CANCELLED",
                "completed_at": utcnow(),
                "current_lease_id": None,
                "last_error": "operator cancel: worker never started",
            }
        )

        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, lease, "gke_create_job_failed"
        )

        stored = _doc(db, "task_e4")
        assert stored["state"] == "CANCELLED", f"a cancelled task was returned to {stored['state']}"
        assert stored["last_error"] == "operator cancel: worker never started"
        assert db.docs[f"leases/{lease.lease_id}"]["released_at"] is not None
        assert _held(db) == 0
        assert (outcome.applied, outcome.reason) == (False, "state_changed")

    def test_a_cancel_requested_while_leased_ends_cancelled_when_the_dispatch_fails(
        self, db, api_store
    ):
        """`cancel_requested` is part of the precondition here. The API flags a
        LEASED task and waits for "the worker or the reconciler"; when the
        dispatch fails there is no worker, and READY is a second hop through the
        next drain. It ends here, the way the reconciler's repair ends it."""
        _world(db)
        seed_task(db, task_id="task_e5", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_e5")
        api_store.request_cancel(TENANT, "task_e5", by=BY)

        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, lease, "cloud_run_run_job_failed"
        )

        stored = _doc(db, "task_e5")
        assert stored["state"] == "CANCELLED", f"a requested cancel ended {stored['state']}"
        assert stored["completed_at"] is not None
        assert stored["next_eligible_at"] is None, "a retry time on a task that will never run"
        assert stored["current_lease_id"] is None
        assert _held(db) == 0
        finished = [e for e in _events(db, "task_e5", "cancelled") if e["detail"].get("phase") == "cancelled"]
        assert len(finished) == 1, "nothing in the task's history says the cancel completed"
        assert (outcome.applied, outcome.target) == (True, "CANCELLED")

    def test_a_requested_cancel_on_the_last_attempt_is_cancelled_not_failed(self, db, api_store):
        """Attempt 3 of 3. The user's decision is recorded as a cancel: FAILED
        would say it broke, and a workflow settles on its WORST terminal step."""
        _world(db)
        seed_task(db, task_id="task_e6", tenant_id=TENANT)
        _doc(db, "task_e6")["attempt_count"] = 2
        snapshot, lease = _admitted(db, "task_e6")
        assert _doc(db, "task_e6")["attempt_count"] == 3
        api_store.request_cancel(TENANT, "task_e6", by=BY)

        SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, lease, "cloud_run_run_job_failed"
        )

        assert _doc(db, "task_e6")["state"] == "CANCELLED"

    def test_the_drain_counts_a_failed_dispatch_whose_task_had_moved_on(self, db):
        """Through `drain()`: the reclaim and the second admission land while
        the dispatch is still in flight, and then it fails."""
        second: list[Lease] = []

        class HangsThenRefuses:
            def dispatch(self, *, task, lease, profile, tenant):  # noqa: ANN001
                _reconciler_reclaims(db, task.id, lease)
                second.append(_admit(db, task.id))
                raise DispatchError(
                    "deadline exceeded creating the execution (injected)",
                    code="cloud_run_run_job_failed",
                )

        _world(db)
        seed_task(db, task_id="task_e7", tenant_id=TENANT)
        scheduler = _scheduler(SchedulerStore(db), HangsThenRefuses())

        report = scheduler.drain()

        stored = _doc(db, "task_e7")
        assert stored["state"] == "LEASED", f"the second lease was orphaned; task is {stored['state']}"
        assert stored["current_lease_id"] == second[0].lease_id
        assert report.dispatch_failures == 1
        assert report.stale_writes == 1
        assert _stale_writes(scheduler, "return_to_ready", "lease_superseded") == 1.0


# --------------------------------------------------------------------------
# return_to_ready_after_failed_dispatch, over a reclaim that stopped part way
# --------------------------------------------------------------------------

RECONCILER = ReconcilerConfig(
    project_id="saga-agents-staging",
    region="us-central1",
    firestore_database="swarm",
    enable_gc=False,
    enable_checkpoint_gc=False,
)


def _reconciler_stops_after(db: ContendedFirestore, task_id: str, lease: Lease, step: str) -> None:
    """A reclaim that stopped short, through the reconciler's own store.

    `repair.py` runs invalidate, terminate, release and repair as separate
    transactions, returns from the second when termination raises or comes back
    unconfirmed ("did NOT release: termination was not confirmed"), and can
    lose its instance between any two -- `detect.py` records a live one that
    stopped between the first and the third. The two places it can stop with
    the task still pointing at the lease:

      "fence"    step 1 committed and nothing after it. The task is at a bumped
                 generation on an UNRELEASED lease.
      "release"  steps 1 and 3 committed -- termination was confirmed, or
                 nothing was running -- and the instance died before step 4.
                 The task is at a bumped generation on a RELEASED lease.
    """
    store = ControlStore(db, logger=reconciler_logger(stream=io.StringIO()))
    assert store.invalidate_generation(task_id, lease.generation) == lease.generation + 1
    if step == "release":
        assert store.release_lease(lease.lease_id, "reconciler:stale_lease") is True
    assert _doc(db, task_id)["current_lease_id"] == lease.lease_id, (
        "the reclaim went further than this test means it to"
    )


def _outlived_the_deadline(db: ContendedFirestore, lease: Lease) -> None:
    """`run_job` hung past the dispatch deadline before it came back with an
    error -- the case where the reconciler reclaims at all. The lease's clocks
    are moved back instead of waited out."""
    doc = db.docs[f"leases/{lease.lease_id}"]
    shift = timedelta(seconds=ADMISSION.dispatch_timeout_seconds + 60)
    for field in ("created_at", "dispatch_deadline", "expires_at"):
        if doc.get(field) is not None:
            doc[field] = doc[field] - shift


def _still_running(task_id: str, lease: Lease) -> ExecutionView:
    """The execution `run_job` did create before its call reported failure."""
    return ExecutionView(
        name=(
            "projects/saga-agents-staging/locations/us-central1/jobs/swarm-eng-mock/"
            f"executions/{task_id}-{lease.generation}"
        ),
        backend=BACKEND,
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(minutes=10),
        task_id=task_id,
        attempt_id=lease.attempt_id,
        tenant_id=TENANT,
        generation=lease.generation,
    )


class _Backend:
    """A Cloud Run-shaped backend for the reconciler's next pass: listed in one
    call, scripted. It records how many slots were still reserved at each kill,
    because a slot returned BEFORE the kill is the defect under test."""

    name = BACKEND

    def __init__(self, db: ContendedFirestore, *executions: ExecutionView) -> None:
        self._db = db
        self._executions = list(executions)
        self.reserved_at_kill: list[int] = []

    def list_executions(self) -> list[ExecutionView]:
        return list(self._executions)

    def terminate(self, execution: ExecutionView) -> bool:
        self.reserved_at_kill.append(_held(self._db))
        return True


def _next_pass(db: ContendedFirestore, backend: _Backend) -> ReconcileReport:
    """The reconciler's next pass: the production `Reconciler` over the
    production `ControlStore`, the same detection and the same repair order."""
    report = Reconciler(
        store=ControlStore(db, logger=reconciler_logger(stream=io.StringIO())),
        backends=[backend],
        config=RECONCILER,
        logger=reconciler_logger(stream=io.StringIO()),
    ).run_once()
    assert report.errors == [], f"the reconciler's pass itself failed: {report.errors}"
    return report


def _where(db: ContendedFirestore, task_id: str) -> str:
    stored = _doc(db, task_id)
    lease_id = stored.get("current_lease_id")
    held = db.docs.get(f"leases/{lease_id}") if lease_id else None
    status = "gone" if held is None else (
        "released" if held.get("released_at") is not None else "unreleased"
    )
    return (
        f"{stored['state']} at generation {stored['current_generation']} on lease "
        f"{lease_id} ({status})"
    )


class TestAPartialReclaim:
    def test_a_task_the_reconciler_only_fenced_is_left_for_it_to_finish(self, db):
        """The finding on PR #45. The dispatch hung past its deadline, the
        reconciler fenced the attempt and stopped (termination unconfirmed, or
        its instance died), and then `run_job` raised.

        Releasing there erased the reconciler's only evidence. The task was
        LEASED at generation 2 on a released lease: `detect_stale_leases` and
        `detect_orphan_leases` skip released leases, `detect_missing_executions`
        ignores LEASED, the drain scans only READY, and a cancel on a LEASED
        task only sets a flag. Stuck for ever. Main recovered from the same
        interleaving, because it released and then wrote READY blind.

        Left alone, the reconciler's next pass finds the superseded, unreleased
        lease with nothing running under it and finishes its own repair.
        """
        _world(db)
        seed_task(db, task_id="task_p1", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_p1")
        _outlived_the_deadline(db, lease)
        _reconciler_stops_after(db, "task_p1", lease, "fence")

        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, lease, "cloud_run_run_job_failed"
        )
        kept = db.docs[f"leases/{lease.lease_id}"]["released_at"] is None
        after_dispatch = _where(db, "task_p1")
        report = _next_pass(db, _Backend(db))

        acted = [o.as_dict() for o in report.outcomes if o.task_id == "task_p1"]
        stored = _doc(db, "task_p1")
        assert stored["state"] == "READY", (
            f"after the failed dispatch the task was {after_dispatch}, and the "
            f"reconciler's next pass acted on it {len(acted)} time(s): it is "
            f"{_where(db, 'task_p1')}, stranded"
        )
        assert stored["current_lease_id"] is None
        assert _held(db) == 0
        assert kept, "the failed dispatch released a lease the reconciler had fenced and kept"
        assert (outcome.applied, outcome.reason, outcome.found) == (False, "lease_superseded", "LEASED")
        assert [(a["lease_id"], a["released"], a["repaired_to"]) for a in acted] == [
            (lease.lease_id, True, "READY")
        ]

    def test_a_running_attempt_the_reconciler_could_not_kill_keeps_its_slots(self, db):
        """The same interleaving after the worker reached RUNNING. The
        reconciler fenced it, its kill came back unconfirmed, and it kept the
        slots on purpose: "did NOT release: termination was not confirmed".
        The failed dispatch read the bumped generation as superseded and
        released them anyway, under a container nobody had confirmed dead.

        Kept, the reconciler's next pass finds the obsolete generation, kills
        it, and only then returns the slots.
        """
        _world(db)
        seed_task(db, task_id="task_p2", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_p2")
        _worker_starts(db, "task_p2", lease).heartbeat()
        _reconciler_stops_after(db, "task_p2", lease, "fence")

        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, lease, "cloud_run_run_job_failed"
        )

        assert db.docs[f"leases/{lease.lease_id}"]["released_at"] is None, (
            "the failed dispatch released the slots of a RUNNING attempt whose "
            "termination the reconciler had not confirmed"
        )
        assert _held(db) == 1
        stored = _doc(db, "task_p2")
        assert (stored["state"], stored["current_lease_id"]) == ("RUNNING", lease.lease_id)
        assert stored["current_generation"] == lease.generation + 1
        assert (outcome.applied, outcome.reason, outcome.found) == (False, "lease_superseded", "RUNNING")

        backend = _Backend(db, _still_running("task_p2", lease))
        _next_pass(db, backend)

        assert backend.reserved_at_kill == [1], (
            "the slot was not still reserved when the reconciler killed the execution"
        )
        assert _doc(db, "task_p2")["state"] == "READY"
        assert _held(db) == 0

    @pytest.mark.parametrize(
        "started, cancel, target",
        [
            pytest.param(False, False, "READY", id="leased"),
            pytest.param(True, False, "READY", id="running"),
            pytest.param(False, True, "CANCELLED", id="cancel-requested"),
        ],
    )
    def test_a_reclaim_that_stopped_after_its_release_is_finished_here(
        self, db, api_store, started, cancel, target
    ):
        """The reconciler fenced the attempt, killed what ran (or found nothing),
        released the lease, and died before `repair_task_state`. The task is
        still in a concurrency state on a lease that is released -- and a
        released lease is outside every reconciler rule, since the snapshot
        reads only unreleased ones. Main's blind write recovered it; skipping
        stranded it.

        Only step 4 is left, and the release that preceded it was the
        reconciler's own judgment that nothing runs. So the failed dispatch
        finishes it, with `repair_task_state`'s rule: READY, or CANCELLED when a
        cancel was requested, or FAILED when retries are spent.
        """
        _world(db)
        seed_task(db, task_id="task_p3", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_p3")
        if started:
            _worker_starts(db, "task_p3", lease)
        if cancel:
            api_store.request_cancel(TENANT, "task_p3", by=BY)
        _reconciler_stops_after(db, "task_p3", lease, "release")
        before = _where(db, "task_p3")

        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, lease, "cloud_run_run_job_failed"
        )

        stored = _doc(db, "task_p3")
        assert stored["state"] == target, (
            f"the task was {before} and is now {_where(db, 'task_p3')}: nothing "
            "will ever move it"
        )
        assert stored["current_lease_id"] is None
        assert stored["current_generation"] == lease.generation + 1
        assert _held(db) == 0, "the reconciler's release was decremented twice"
        assert (outcome.applied, outcome.target) == (True, target)
        announced = [
            e for e in _events(db, "task_p3", target.lower())
            if e["detail"].get("completed_reclaim") is True
        ]
        assert len(announced) == 1, "the task's timeline does not say how it left the lease"
        if target == "CANCELLED":
            assert stored["completed_at"] is not None
            assert stored["next_eligible_at"] is None
            assert announced[0]["detail"].get("phase") == "cancelled"


# --------------------------------------------------------------------------
# record_blockers, and the drain's own counting of a skipped park
# --------------------------------------------------------------------------

class TestThroughAdmitOne:
    def test_a_park_that_lost_to_a_cancel_is_counted_not_parked(self, db, api_store, make_scheduler):
        _world(db)
        db.docs[f"tenants/{TENANT}"]["enabled"] = False
        seed_task(db, task_id="task_f1", tenant_id=TENANT)
        scheduler = make_scheduler()
        snapshot = scheduler.store.get_task("task_f1")
        api_store.request_cancel(TENANT, "task_f1", by=BY)

        report = DrainReport()
        scheduler._admit_one(snapshot, report)

        assert _doc(db, "task_f1")["state"] == "CANCELLED", "the user's cancel was parked over"
        assert report.parked == 0
        assert report.stale_writes == 1
        assert _stale_writes(scheduler, "park", "state_changed") == 1.0

    def test_a_not_ready_denial_is_not_recorded_on_the_task_that_was_admitted(self, db, make_scheduler):
        """Admission refused THIS drain because another scheduler had just
        admitted the task. Writing that refusal as the task's blocker put
        `not_ready` on a LEASED task, which the console shows as why it is
        waiting."""
        _world(db)
        seed_task(db, task_id="task_g1", tenant_id=TENANT)
        scheduler = make_scheduler()
        snapshot = scheduler.store.get_task("task_g1")
        _admit(db, "task_g1")

        report = DrainReport()
        scheduler._admit_one(snapshot, report)

        stored = _doc(db, "task_g1")
        assert stored["blocked_by"] == [], (
            f"blocked_by={stored['blocked_by']}: a LEASED task was labelled with "
            "the denial another drain got for it"
        )
        assert stored["state"] == "LEASED"
        assert report.denied == 1
        assert report.stale_writes == 1
        assert _stale_writes(scheduler, "record_blockers", "state_changed") == 1.0


# --------------------------------------------------------------------------
# The check and the write commit together
# --------------------------------------------------------------------------

class TestTheCheckAndTheWriteAreOneTransaction:
    """The concurrent write lands INSIDE the store's own transaction, after its
    read and before its commit. `ContendedFirestore` aborts that commit, the
    real `firestore.transactional` re-runs the body, and the re-run must see the
    new state and skip. A store that checked with a plain read and then wrote
    blind would pass every test above and fail these."""

    def test_promote(self, db, api_store):
        _world(db)
        seed_task(db, task_id="task_t1", tenant_id=TENANT, state="PARKED",
                  park_reason="DEPENDENCY_INCOMPLETE")
        store = SchedulerStore(db)
        snapshot = store.get_task("task_t1")
        fired: list[bool] = []

        def the_user_cancels() -> None:
            fired.append(True)
            api_store.request_cancel(TENANT, "task_t1", by=BY)

        db.interleave("tasks/task_t1", the_user_cancels)
        outcome = store.promote_to_ready(snapshot, detail={"reason": "dependencies_satisfied"})

        assert fired, (
            "promote_to_ready never read the task it writes; a write that reads "
            "nothing cannot have a precondition"
        )
        assert "tasks/task_t1" in db.aborts, "the first attempt was not retried"
        assert _doc(db, "task_t1")["state"] == "CANCELLED"
        assert outcome.applied is False

    def test_mark_dispatched(self, db):
        _world(db)
        seed_task(db, task_id="task_t2", tenant_id=TENANT)
        snapshot, lease = _admitted(db, "task_t2")
        fired: list[bool] = []

        def the_reconciler_reclaims() -> None:
            fired.append(True)
            _reconciler_reclaims(db, "task_t2", lease)

        db.interleave("tasks/task_t2", the_reconciler_reclaims)
        outcome = SchedulerStore(db).mark_dispatched(
            snapshot, lease, "executions/task_t2-1", BACKEND
        )

        assert fired, (
            "mark_dispatched never read the task it writes; a write that reads "
            "nothing cannot have a precondition"
        )
        assert "tasks/task_t2" in db.aborts, "the first attempt was not retried"
        assert _doc(db, "task_t2")["state"] == "READY"
        assert outcome.applied is False and outcome.reason == "lease_superseded"

    def test_return_to_ready(self, db):
        """Red before the fix for the right reason: the old code DID read
        transactionally, so the retry saw the second lease -- and wrote READY
        over it anyway, because nothing checked what it read."""
        _world(db)
        seed_task(db, task_id="task_t3", tenant_id=TENANT)
        snapshot, first = _admitted(db, "task_t3")
        fired: list[bool] = []
        second: list[Lease] = []

        def reclaimed_and_re_leased() -> None:
            fired.append(True)
            _reconciler_reclaims(db, "task_t3", first)
            second.append(_admit(db, "task_t3"))

        db.interleave("tasks/task_t3", reclaimed_and_re_leased)
        SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, first, "cloud_run_run_job_failed"
        )

        assert fired
        assert "tasks/task_t3" in db.aborts
        stored = _doc(db, "task_t3")
        assert stored["state"] == "LEASED", f"the second lease was orphaned; task is {stored['state']}"
        assert stored["current_lease_id"] == second[0].lease_id
        assert _held(db) == 1

    def test_finishing_a_reclaim_the_reconciler_finished_first(self, db):
        """A GUARD, green before and after by design. It pins the new branch
        that finishes a reclaim which stopped after its release
        (`TestAPartialReclaim`): that write must be decided inside the same
        transaction too.

        The reconciler's own step 4 lands between the failed dispatch's read and
        its commit, and a second admission re-leases the task. A finish written
        from the first read would put READY with no lease over the new lease.
        The re-run has to see that the task no longer points at this lease, and
        leave it alone."""
        _world(db)
        seed_task(db, task_id="task_t4", tenant_id=TENANT)
        snapshot, first = _admitted(db, "task_t4")
        _reconciler_stops_after(db, "task_t4", first, "release")
        fired: list[bool] = []
        second: list[Lease] = []

        def the_reconciler_finishes_and_it_is_re_leased() -> None:
            fired.append(True)
            store = ControlStore(db, logger=reconciler_logger(stream=io.StringIO()))
            assert store.repair_task_state(
                "task_t4", to_state=TaskState.READY, expected_lease_id=first.lease_id
            ) is TaskState.READY
            second.append(_admit(db, "task_t4"))

        db.interleave("tasks/task_t4", the_reconciler_finishes_and_it_is_re_leased)
        outcome = SchedulerStore(db).return_to_ready_after_failed_dispatch(
            snapshot, first, "cloud_run_run_job_failed"
        )

        assert fired
        assert "tasks/task_t4" in db.aborts, "the first attempt was not retried"
        stored = _doc(db, "task_t4")
        assert stored["state"] == "LEASED", f"the second lease was orphaned; task is {_where(db, 'task_t4')}"
        assert stored["current_lease_id"] == second[0].lease_id
        assert _held(db) == 1
        assert (outcome.applied, outcome.reason) == (False, "lease_superseded")
