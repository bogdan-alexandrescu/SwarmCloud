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

    def _task_and_lease(self, db, *, attempts: int, cap: int = 3):
        from swarm_common.models import Lease, Task

        task = Task(
            id="task_x", tenant_id="eng", runner_profile="browser",
            resource_class="browser", state=TaskState.LEASED,
            created_at=NOW, updated_at=NOW,
            input={}, submitted_by='bogdan@saga.xyz',
            attempt_count=attempts, max_attempts=cap,
        )
        lease = Lease(
            lease_id="lease_x", task_id="task_x", attempt_id="att_x",
            tenant_id="eng", generation=attempts, units=2,
            pools=["global"], state=TaskState.LEASED,
            created_at=NOW, dispatch_deadline=NOW + timedelta(seconds=300),
            expires_at=NOW + timedelta(seconds=120),
        )
        db.collection("tasks").document("task_x").set(task.to_firestore())
        return task, lease

    def test_a_task_under_its_cap_returns_to_ready(self, db):
        """The normal case must still retry -- the fix must not break recovery."""
        store = self._store(db)
        task, lease = self._task_and_lease(db, attempts=1)
        store.return_to_ready_after_failed_dispatch(task, lease, "gke_create_job_failed")
        got = db.collection("tasks").document("task_x").get().to_dict()
        assert got["state"] == TaskState.READY.value
        assert got.get("next_eligible_at") is not None, "a retry must be scheduled"

    def test_a_task_at_its_cap_fails_instead_of_retrying(self, db):
        """THE ONE THAT WOULD HAVE CAUGHT IT."""
        store = self._store(db)
        task, lease = self._task_and_lease(db, attempts=3)
        store.return_to_ready_after_failed_dispatch(task, lease, "gke_create_job_failed")
        got = db.collection("tasks").document("task_x").get().to_dict()
        assert got["state"] == TaskState.FAILED.value, (
            "a task that has used its last attempt must not return to READY; "
            "this is how one reached 83 attempts against a cap of 3"
        )

    def test_a_terminal_task_is_not_given_a_retry_time(self, db):
        """`next_eligible_at` on a FAILED task promises a retry that is not
        coming, and the console renders it as a scheduled attempt."""
        store = self._store(db)
        task, lease = self._task_and_lease(db, attempts=3)
        store.return_to_ready_after_failed_dispatch(task, lease, "gke_create_job_failed")
        got = db.collection("tasks").document("task_x").get().to_dict()
        assert got.get("completed_at") is not None, "a terminal task has a completion time"
        assert got.get("next_eligible_at") is None
