"""A parked attempt closes its own attempt document (#163).

`ControlPlane.park()` moved the task to PARKED, emitted PARKED and released
the lease, and never wrote the attempt's end. The attempt document of every
parked attempt therefore kept `completed_at: None` and `exit_code: None`, and
read as an attempt that was still running, in the UI's timeline and in the
checkpoint collector, which holds an open attempt's checkpoints.

The end is written IN the park's own fenced transaction, with PARKED and the
lease release (#2064): a fenced park raises with nothing written, as it
always has, so a superseded attempt never closes a document on the strength
of a park it did not make, and a park that commits has closed its document.
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


class CommitRunner(FakeTransactionRunner):
    """Records which writes each transaction made: `commits[i]` is the i-th's."""

    def __init__(self, db: FakeFirestore) -> None:
        super().__init__(db)
        self.commits: list[list[tuple[str, str, dict]]] = []

    def run(self, fn, *, call_options=None):
        start = len(self._db.writes)
        result = super().run(fn, call_options=call_options)
        self.commits.append(list(self._db.writes[start:]))
        return result


def _one_commit_holds_the_park_the_end_and_the_release(runner: CommitRunner) -> None:
    """PARKED, the attempt's end (exit 75) and the lease release: one commit."""

    def holds(writes: list, predicate) -> bool:
        return any(predicate(op, path, data) for op, path, data in writes)

    carrying = [
        writes for writes in runner.commits
        if holds(writes, lambda op, path, data: path == "tasks/task_1"
                 and data.get("state") == "PARKED")
    ]
    assert len(carrying) == 1, runner.commits
    writes = carrying[0]
    # MUTATION: write the attempt's end after the park's commit, best effort.
    assert holds(writes, lambda op, path, data: path == "attempts/att_1"
                 and data.get("exit_code") == ExitCode.PARKED), writes
    # MUTATION: release the lease in a second transaction (#2064).
    assert holds(writes, lambda op, path, data: path == "leases/lease_1"
                 and data.get("released_at") is not None), writes


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


def test_the_end_is_written_in_the_parks_own_commit_with_the_release():
    """Not after it: a park whose commit landed has closed its attempt and
    given its slot back, and one that did not has done neither."""
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    runner = CommitRunner(db)
    control = _control(db)
    control._txn = runner
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    db.writes.clear()

    control.park(reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=utcnow())

    _one_commit_holds_the_park_the_end_and_the_release(runner)
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


def test_a_park_whose_commit_is_refused_writes_neither_its_end_nor_its_release():
    """The end is in the commit: a refused park closes nothing and frees nothing."""
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    real_run = control._txn.run

    def refused(fn, **kwargs):
        def body(txn):
            fn(txn)
            raise OSError("firestore unavailable")

        return real_run(body, **kwargs)

    control._txn.run = refused  # type: ignore[method-assign]

    with pytest.raises(OSError):
        control.park(reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=utcnow())

    # MUTATION: write the attempt's end or release the lease outside the commit.
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("attempts/att_1")["completed_at"] is None
    assert EventType.PARKED.value not in db.event_types("task_1")
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


def test_an_await_park_writes_the_end_in_its_own_commit_with_the_release():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    runner = CommitRunner(db)
    control = _control(db)
    control._txn = runner
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    db.writes.clear()

    control.park_awaiting_children(max_resumes=3)

    _one_commit_holds_the_park_the_end_and_the_release(runner)
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


def test_an_await_park_whose_commit_is_refused_writes_neither_its_end_nor_its_release():
    db = FakeFirestore()
    seed_attempt(db, state=TaskState.RUNNING)
    control = _control(db)
    control.record_attempt_start(backend="cloud_run_job", execution_name=None)
    real_run = control._txn.run

    def refused(fn, **kwargs):
        def body(txn):
            fn(txn)
            raise OSError("firestore unavailable")

        return real_run(body, **kwargs)

    control._txn.run = refused  # type: ignore[method-assign]

    with pytest.raises(OSError):
        control.park_awaiting_children(max_resumes=3)

    # MUTATION: write the attempt's end or release the lease outside the commit.
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("attempts/att_1")["completed_at"] is None
