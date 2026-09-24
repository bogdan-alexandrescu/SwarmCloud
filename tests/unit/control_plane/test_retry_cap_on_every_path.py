"""The retry cap is enforced wherever a task returns to READY.

WHY THIS FILE EXISTS. The cap lived in exactly one place --
`reconciler/store.py`, on the path that REPAIRS a task to READY -- and the
scheduler's `return_to_ready_after_failed_dispatch`, the OTHER path that
returns a task to READY, wrote the state unconditionally.

So a task whose dispatch kept failing retried for ever. Observed live on
2026-09-23: `task_d18d8d8b044d469cb43c`, the browser step of a twenty-step
workflow, reached 83 attempts against a cap of 3, re-dispatching every 30
seconds for hours on `gke_create_job_failed`. It was still climbing when it was
stopped by hand.

The backoff in that function is not the cap and was never meant to be. Its
docstring reasons carefully about retry PRESSURE -- "a tight retry would burn
the whole run's lease budget on one broken task" -- and nothing counted the
attempts.

These tests pin BOTH paths, because pinning one is what produced the bug.

AND THEY THEN HID THE NEXT ONE, which is why `TestTheSchedulerPath` changed on
2026-09-24. The cap on this path was evaluated against `task.attempt_count` --
the in-memory Task the drain loop READ while the task was still READY. The
frozen admission transaction increments `attempt_count` in Firestore
(`admission.py`, `"attempt_count": ... + 1`) and never touches that object, so
the count the check saw was always one behind: a task with `max_attempts` 3
got a FOURTH attempt. Measured on `task_b568a623be8645eb87c6`, which ended
FAILED at `attempt_count` 4 against a cap of 3.

The tests here did not see it because they handed the store a Task whose
`attempt_count` was already the POST-admission value -- the one number the
production caller never has. `_task_and_lease` now builds what the drain loop
really passes (the READY snapshot) and seeds the document with what admission
really wrote, and `TestThroughTheDrainLoop` drives the real loop from zero
instead of trusting any hand-built count at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import retries_exhausted
from swarm_common.states import TaskState

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


class TestThePredicate:
    def test_under_the_cap_is_not_exhausted(self):
        assert retries_exhausted(0, 3) is False
        assert retries_exhausted(2, 3) is False

    def test_at_the_cap_is_exhausted(self):
        """The third attempt of three is the last one, not the one before it."""
        assert retries_exhausted(3, 3) is True

    def test_past_the_cap_is_exhausted(self):
        """The state the live task was in: 83 against 3."""
        assert retries_exhausted(83, 3) is True

    def test_a_cap_of_one_means_one_attempt(self):
        assert retries_exhausted(0, 1) is False
        assert retries_exhausted(1, 1) is True


class TestTheSchedulerPath:
    """`return_to_ready_after_failed_dispatch` -- the path that had no cap."""

    def _store(self, db):
        from scheduler.store import SchedulerStore

        return SchedulerStore(db, now=lambda: NOW)

    def _task_and_lease(self, db, *, attempts: int, cap: int = 3,
                        previous_retry_at=None):
        """`attempts` is the attempt that has just FAILED TO DISPATCH -- the
        count Firestore holds once admission has run.

        The Task handed to the store is what the drain loop actually passes: the
        snapshot it read while the task was still READY, one attempt behind,
        because `acquire_lease_in_transaction` increments `attempt_count` in the
        document and never in that object. Handing it the post-admission count
        instead is what made these tests agree with the off-by-one.

        `previous_retry_at` is the `next_eligible_at` the PREVIOUS failed
        dispatch left on the document -- present on every real task that
        reaches its last attempt through this path.
        """
        from swarm_common.models import Lease, Task

        in_memory = Task(
            id="task_x", tenant_id="eng", runner_profile="browser",
            resource_class="browser", state=TaskState.READY,
            created_at=NOW, updated_at=NOW,
            input={}, submitted_by='bogdan@saga.xyz',
            attempt_count=attempts - 1, max_attempts=cap,
        )
        lease = Lease(
            lease_id="lease_x", task_id="task_x", attempt_id="att_x",
            tenant_id="eng", generation=attempts, units=2,
            pools=["global"], state=TaskState.LEASED,
            created_at=NOW, dispatch_deadline=NOW + timedelta(seconds=300),
            expires_at=NOW + timedelta(seconds=120),
        )
        stored = in_memory.to_firestore()
        stored.update(
            {
                "state": TaskState.LEASED.value,
                "attempt_count": attempts,
                "current_lease_id": lease.lease_id,
                "current_generation": attempts,
                "next_eligible_at": previous_retry_at,
            }
        )
        db.collection("tasks").document("task_x").set(stored)
        return in_memory, lease

    def test_a_task_under_its_cap_returns_to_ready(self, db):
        """The normal case must still retry -- the fix must not break recovery."""
        store = self._store(db)
        task, lease = self._task_and_lease(db, attempts=1)
        store.return_to_ready_after_failed_dispatch(task, lease, "gke_create_job_failed")
        got = db.collection("tasks").document("task_x").get().to_dict()
        assert got["state"] == TaskState.READY.value
        assert got.get("next_eligible_at") is not None, "a retry must be scheduled"

    def test_a_task_at_its_cap_fails_instead_of_retrying(self, db):
        """THE ONE THAT WOULD HAVE CAUGHT IT -- now fed what production feeds it.

        Attempt 3 of 3 failed to dispatch. The document says 3; the Task the
        loop holds says 2. Reading the Task returned it to READY and bought a
        fourth attempt, which is what task_b568a623be8645eb87c6 got.
        """
        store = self._store(db)
        task, lease = self._task_and_lease(db, attempts=3)
        store.return_to_ready_after_failed_dispatch(task, lease, "gke_create_job_failed")
        got = db.collection("tasks").document("task_x").get().to_dict()
        assert got["state"] == TaskState.FAILED.value, (
            "a task that has used its last attempt must not return to READY; "
            "the cap must be read from the document admission wrote, not from "
            "the READY snapshot the loop is holding"
        )

    def test_a_terminal_task_is_not_given_a_retry_time(self, db):
        """`next_eligible_at` on a FAILED task promises a retry that is not
        coming, and the console renders it as a scheduled attempt.

        Seeded with the retry time the PREVIOUS failure wrote, because that is
        the state every real task reaches its last attempt in -- and a write
        that merely omits the field leaves that stale promise in place.
        """
        store = self._store(db)
        task, lease = self._task_and_lease(
            db, attempts=3, previous_retry_at=NOW - timedelta(seconds=5)
        )
        store.return_to_ready_after_failed_dispatch(task, lease, "gke_create_job_failed")
        got = db.collection("tasks").document("task_x").get().to_dict()
        assert got.get("completed_at") is not None, "a terminal task has a completion time"
        assert got.get("next_eligible_at") is None

    def test_the_cap_is_read_from_the_document_not_the_callers_copy(self, db):
        """Whatever the caller's Task says, the document is the authority.

        A caller holding a Task that claims no attempts at all still cannot buy
        another one once the document says the cap is spent.
        """
        from swarm_common.models import Task

        store = self._store(db)
        _, lease = self._task_and_lease(db, attempts=3)
        stale = Task(
            id="task_x", tenant_id="eng", runner_profile="browser",
            resource_class="browser", state=TaskState.READY,
            created_at=NOW, updated_at=NOW, input={}, submitted_by="bogdan@saga.xyz",
            attempt_count=0, max_attempts=3,
        )
        store.return_to_ready_after_failed_dispatch(stale, lease, "gke_create_job_failed")
        got = db.collection("tasks").document("task_x").get().to_dict()
        assert got["state"] == TaskState.FAILED.value


class TestThroughTheDrainLoop:
    """The real drain loop, the real admission transaction, a backend that
    refuses every dispatch -- counted from zero, trusting no hand-built count.

    `max_attempts` 3 must mean three dispatch attempts and then FAILED. Before
    2026-09-24 it meant four: the third failure was checked against the
    pre-admission count (2), returned the task to READY, and the next drain
    leased it a fourth time.
    """

    START = datetime(2026, 9, 24, 3, 30, tzinfo=timezone.utc)

    def _scheduler(self, db, clock, attempts_seen):
        from scheduler.dispatch import BackendRouter, DispatchError
        from scheduler.loop import Scheduler
        from scheduler.metrics import SchedulerMetrics
        from scheduler.store import SchedulerStore

        from .conftest import scheduler_settings

        class RefusingDispatcher:
            def dispatch(self, *, task, lease, profile, tenant):
                attempts_seen.append(lease.generation)
                raise DispatchError(
                    "jobs.batch is forbidden (injected)", code="gke_create_job_forbidden"
                )

        settings = scheduler_settings()
        refusing = RefusingDispatcher()
        return Scheduler(
            settings=settings,
            store=SchedulerStore(db, now=lambda: clock[0]),
            router=BackendRouter(cloud_run=refusing, gke=refusing, settings=settings),
            metrics=SchedulerMetrics(),
            now=lambda: clock[0],
        )

    def test_three_failed_dispatches_of_three_end_failed_not_ready(self, db):
        from .conftest import seed_pool, seed_task, seed_tenant

        seed_tenant(db, "eng", max_active=5)
        seed_pool(db, "global", hard_limit=5)
        seed_task(db, task_id="task_b568", tenant_id="eng", created_at=self.START)
        assert db.docs["tasks/task_b568"]["attempt_count"] == 0
        assert db.docs["tasks/task_b568"]["max_attempts"] == 3

        clock = [self.START]
        attempts_seen: list[int] = []
        scheduler = self._scheduler(db, clock, attempts_seen)

        states = []
        for _ in range(3):
            report = scheduler.drain()
            assert report.dispatch_failures == 1, report.to_dict()
            states.append(db.docs["tasks/task_b568"]["state"])
            # Past the dispatch-failure backoff, so the next drain may lease it.
            clock[0] = clock[0] + timedelta(seconds=31)

        stored = db.docs["tasks/task_b568"]
        assert stored["attempt_count"] == 3
        assert states == ["READY", "READY", "FAILED"], (
            f"after each failed dispatch the task was {states}; the third of three "
            f"must be terminal, or the next drain leases a fourth attempt"
        )
        assert stored["completed_at"] is not None
        assert stored["next_eligible_at"] is None, (
            "the second failure's retry time must not survive onto a FAILED task"
        )

        # And there is no fourth attempt: another drain finds nothing to lease.
        report = scheduler.drain()
        assert report.leased == 0
        assert attempts_seen == [1, 2, 3], f"dispatch was attempted at generations {attempts_seen}"
        assert db.docs["pools/global"]["active"] == 0, "every failed attempt gave its slot back"
