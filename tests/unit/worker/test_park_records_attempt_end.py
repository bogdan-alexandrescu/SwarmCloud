"""A parked attempt closes its own attempt document (#163).

`ControlPlane.park()` moved the task to PARKED, emitted PARKED and released
the lease, and never wrote the attempt's end. The attempt document of every
parked attempt therefore kept `completed_at: None` and `exit_code: None`, and
read as an attempt that was still running, in the UI's timeline and in the
checkpoint collector, which holds an open attempt's checkpoints.

The end is written AFTER the fenced transition to PARKED, never before: a
fenced park raises with nothing written, as it always has, so a superseded
attempt never closes a document on the strength of a park it did not make.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from agent_worker.control import ControlPlane
from agent_worker.errors import ExitCode, FencedWriteRefused
from agent_worker.logs import build_logger
from swarm_common.models import utcnow
from swarm_common.states import EventType, ParkReason, TaskState

from worker_seeds import TENANT, seed_attempt
from fakes import FakeFirestore, FakeTransactionRunner


def _control(db: FakeFirestore, *, generation: int = 1) -> ControlPlane:
    return ControlPlane(
        db,
        task_id="task_1",
        attempt_id="att_1",
        lease_id="lease_1",
        tenant_id=TENANT,
        generation=generation,
        logger=build_logger(
            task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
            generation=generation, runner_profile="mock", stream=io.StringIO(),
        ),
        txn_runner=FakeTransactionRunner(db),
    )


def test_a_park_records_the_attempt_end_with_exit_75_and_the_reason():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)

    control.park(
        reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
        next_eligible_at=utcnow() + timedelta(minutes=5),
    )

    attempt = db.doc("attempts/att_1")
    assert attempt["exit_code"] == ExitCode.PARKED == 75
    assert attempt["error"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    assert attempt["completed_at"] is not None
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value


def test_the_end_is_written_only_after_the_fenced_transition():
    """The order in the write log: the task's PARKED update, then the attempt's end."""
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    db.writes.clear()

    control.park(reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=utcnow())

    parked_at = next(
        index for index, (op, path, data) in enumerate(db.writes)
        if op == "update" and path == "tasks/task_1" and data.get("state") == "PARKED"
    )
    ended_at = next(
        index for index, (op, path, data) in enumerate(db.writes)
        if path == "attempts/att_1" and data.get("exit_code") == ExitCode.PARKED
    )
    assert parked_at < ended_at, db.writes
    # And before the PARKED event and the release, the order `finish` uses.
    released_at = next(
        index for index, (op, path, data) in enumerate(db.writes)
        if path == "leases/lease_1" and data.get("released_at") is not None
    )
    assert ended_at < released_at
    assert EventType.PARKED.value in db.event_types("task_1")


def test_a_fenced_park_writes_no_attempt_end():
    """The reconciler fenced this attempt: the park raises before any write."""
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING, generation=1, task_generation=2)
    control = _control(db, generation=1)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    db.writes.clear()

    with pytest.raises(FencedWriteRefused):
        control.park(reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=utcnow())

    assert db.writes == [], "a fenced park wrote something"
    assert db.doc("attempts/att_1")["completed_at"] is None
    assert db.doc("attempts/att_1")["exit_code"] is None


def test_a_failed_attempt_end_write_still_releases_the_lease():
    """The park has landed: the slot goes back even if the attempt end is lost."""
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)

    def unreachable(**_kwargs):
        raise OSError("firestore unavailable")

    control.record_attempt_end = unreachable  # type: ignore[method-assign]

    control.park(reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=utcnow())

    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert EventType.PARKED.value in db.event_types("task_1")


# -- the await park (child tasks, docs/design/child-tasks.md §3.3) -----------
#
# `park_awaiting_children` is a park too, in its own transaction rather than
# through `park()`, and it closed no attempt document: a parent cancelled
# while it waited on its children read its last start to the cancel as one
# open attempt, the defect #163 is about.


def test_an_await_park_records_the_attempt_end_with_exit_75_and_the_reason():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)

    control.park_awaiting_children(max_resumes=3)

    attempt = db.doc("attempts/att_1")
    assert attempt["exit_code"] == ExitCode.PARKED == 75
    assert attempt["error"] == ParkReason.CHILDREN_INCOMPLETE.value
    assert attempt["completed_at"] is not None
    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value


def test_an_await_park_writes_the_end_after_its_transition_and_before_the_release():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    db.writes.clear()

    control.park_awaiting_children(max_resumes=3)

    parked_at = next(
        index for index, (op, path, data) in enumerate(db.writes)
        if op == "update" and path == "tasks/task_1" and data.get("state") == "PARKED"
    )
    ended_at = next(
        index for index, (op, path, data) in enumerate(db.writes)
        if path == "attempts/att_1" and data.get("exit_code") == ExitCode.PARKED
    )
    released_at = next(
        index for index, (op, path, data) in enumerate(db.writes)
        if path == "leases/lease_1" and data.get("released_at") is not None
    )
    assert parked_at < ended_at < released_at, db.writes


def test_a_fenced_await_park_writes_no_attempt_end():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING, generation=1, task_generation=2)
    control = _control(db, generation=1)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    db.writes.clear()

    with pytest.raises(FencedWriteRefused):
        control.park_awaiting_children(max_resumes=3)

    assert db.writes == [], "a fenced await park wrote something"
    assert db.doc("attempts/att_1")["completed_at"] is None
    assert db.doc("attempts/att_1")["exit_code"] is None


def test_a_failed_await_park_attempt_end_write_still_releases_the_lease():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)

    def unreachable(**_kwargs):
        raise OSError("firestore unavailable")

    control.record_attempt_end = unreachable  # type: ignore[method-assign]

    control.park_awaiting_children(max_resumes=3)

    assert db.doc("tasks/task_1")["state"] == TaskState.PARKED.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert EventType.PARKED.value in db.event_types("task_1")
