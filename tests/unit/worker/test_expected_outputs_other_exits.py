"""A missing expected output fails ONE way out of an attempt, and a stale one writes nothing (#149).

The owner decided on #149 (2026-09-25) that an attempt ending without an
expected output FAILS, retryably, with the missing names as its cause. #153
implemented that in `lifecycle._finalise`, for a runner that finished cleanly,
and `test_expected_outputs_reach_the_agent.py` holds that path: requeued while
attempts are left, FAILED when they are spent, SUCCEEDED when the file is
there, a requested cancel kept, a runner's own failure kept.

These tests hold the edges of that decision, which nothing pinned:

* A STALE GENERATION WRITES NOTHING. `control.fail_retryably` reads the task
  and picks READY, FAILED or CANCELLED inside one fenced transaction. A worker
  whose attempt was superseded after its runner finished, and before that
  transaction, must leave the task, the lease, the pools and the event stream
  exactly as the fence left them, and record the fence on its own attempt
  document only. Without the fence the retry would send a newer attempt's task
  back to READY and clear its lease pointer. Invariant 5.

* PARKS, CANCELS AND CRASHES ARE UNCHANGED. #153 kept them off this path on
  purpose: a parked or interrupted attempt resumes later and may still write
  the file, a cancelled one was stopped by a person, and a crash has its own
  cause. Each of them ends as it did before #149, with no `retrying` event, no
  missing-output cause in `last_error` and no `expected_outputs_missing` in
  what it recorded, although every one of them ended without the file.

The fence helpers are the ones `test_fenced_sigterm.py` defines, imported
rather than restated, so "left alone" means the same thing in both files: the
stale worker wrote its own attempt document and nothing else.
"""

from __future__ import annotations

import io
import json
import threading
import time
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from swarm_common.states import EventType, ParkReason, TaskState

from conftest import seed_attempt
from test_fenced_sigterm import (
    LONG_RUN,
    QUIET,
    Trigger,
    World,
    assert_left_alone,
    assert_the_attempt_says_it_stood_down,
    fence,
    freeze,
    re_lease,
    sigterm,
)

#: `task.metadata` key the API writes and the worker reads.
METADATA_KEY = "expected_outputs"
#: `result_summary` key naming what the attempt did not upload.
MISSING_KEY = "expected_outputs_missing"
#: The cause `control.fail_retryably` records for this failure.
CAUSE = "expected_outputs_missing"

#: A mock runner that exits cleanly almost at once, having written notes.md.
CLEAN_RUN = {
    "prompt": "write the notes",
    "steps": 1,
    "sleep_seconds": 0.01,
    "artifact_name": "notes.md",
    "artifact_text": "the notes\n",
}

#: A mock runner that reports a thirty-minute rate limit and exits before it
#: writes any artifact. Thirty minutes is past the in-place retry ceiling, so
#: the worker parks.
QUOTA_RUN = {
    "prompt": "burn quota",
    "steps": 1,
    "sleep_seconds": 0.05,
    "quota_exhausted": True,
    "provider": "anthropic",
    "retry_after_seconds": 1800,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _expect(db: Any, *names: str) -> None:
    """What the API writes on an upstream step's task at submission."""
    db.doc("tasks/task_1")["metadata"] = {METADATA_KEY: list(names)}


def _records(log_stream: io.StringIO) -> list[dict[str, Any]]:
    out = []
    for line in log_stream.getvalue().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _missing_output_lines(log_stream: io.StringIO) -> list[dict[str, Any]]:
    return [
        r
        for r in _records(log_stream)
        if "expected outputs missing" in str(r.get("message", ""))
    ]


def assert_not_failed_for_missing_outputs(db: Any, log_stream: io.StringIO) -> None:
    """Nothing the attempt wrote says it failed, or was retried, for a missing file.

    Checked on every place the cause could land: the task's `last_error` and
    `result_summary`, every event's detail (a park's detail carries the upload
    summary), the attempt document's error, and the worker's own log.
    """
    task = db.doc("tasks/task_1")
    assert "expected outputs" not in str(task.get("last_error") or ""), task.get("last_error")
    assert MISSING_KEY not in (task.get("result_summary") or {}), task.get("result_summary")
    events = db.events("task_1")
    assert [e for e in events if e["type"] == EventType.RETRYING.value] == [], [
        (e["type"], e.get("detail")) for e in events
    ]
    for event in events:
        detail = event.get("detail") or {}
        assert detail.get("cause") != CAUSE, (event["type"], detail)
        assert MISSING_KEY not in detail, (event["type"], detail)
    attempt = db.doc("attempts/att_1")
    assert "expected outputs" not in str(attempt.get("error") or ""), attempt.get("error")
    assert _missing_output_lines(log_stream) == []


# ---------------------------------------------------------------------------
# a stale generation writes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "supersede",
    ["fenced", "re-leased-LEASED", "re-leased-RUNNING"],
)
def test_a_stale_generation_writes_nothing_when_an_expected_output_is_missing(
    db, worker_factory, log_stream, supersede
):
    """The runner finished cleanly without scan-01.md, so this attempt would
    fail retryably. Between the upload and that write the attempt is
    superseded: fenced by the reconciler, or fenced and re-leased to
    generation 2 by the scheduler, LEASED or already RUNNING.

    The retry must be refused inside its own transaction. Unfenced, it would
    send generation 2's task back to READY, clear its lease pointer, emit
    `retrying` into its stream and release a lease it no longer owns.
    """
    seed_attempt(db, task_input=CLEAN_RUN, pool_active=4)
    _expect(db, "notes.md", "scan-01.md")
    worker, _config, _exporter = worker_factory(**QUIET)
    upload = worker._upload_outputs
    frozen: dict[str, World] = {}
    new_state = None if supersede == "fenced" else TaskState[supersede.rsplit("-", 1)[1]]

    def upload_then_supersede(**kwargs: Any) -> dict[str, Any]:
        summary = upload(**kwargs)
        if "world" not in frozen:
            if new_state is None:
                fence(db)
            else:
                re_lease(db, state=new_state)
            frozen["world"] = freeze(db)
        return summary

    worker._upload_outputs = upload_then_supersede  # type: ignore[method-assign]

    exit_code = worker.run()

    assert "world" in frozen, "the attempt never reached its upload; nothing was tested"
    assert exit_code == ExitCode.GENERATION_FENCED
    assert_left_alone(db, frozen["world"])
    assert_the_attempt_says_it_stood_down(db)
    task = db.doc("tasks/task_1")
    if new_state is None:
        # The reconciler holds the lease until it has seen the Job go.
        assert task["state"] == TaskState.RUNNING.value
        assert db.doc("leases/lease_1")["released_at"] is None
    else:
        assert task["state"] == new_state.value
        assert task["current_lease_id"] == "lease_2"
        assert db.doc("leases/lease_2")["released_at"] is None
    assert "expected outputs" not in str(task.get("last_error") or "")


# ---------------------------------------------------------------------------
# parks, cancels and crashes end as they did before #149
# ---------------------------------------------------------------------------


def test_a_quota_park_with_an_expected_output_missing_parks_as_before(
    db, worker_factory, log_stream
):
    """The runner hit a rate limit and exited before writing anything. The
    attempt parks on quota and resumes later, when it may still write the
    file; it is not failed for the file it has not written yet."""
    seed_attempt(db, task_input=QUOTA_RUN, pool_active=4)
    _expect(db, "scan-01.md")
    worker, _config, _exporter = worker_factory()

    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value, task["state"]
    assert task["park_reason"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    assert task["current_lease_id"] is None
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert EventType.PARKED.value in db.event_types("task_1")
    assert_not_failed_for_missing_outputs(db, log_stream)


def test_an_interrupted_attempt_with_an_expected_output_missing_parks_as_before(
    db, worker_factory, log_stream
):
    """SIGTERM to the worker (an instance reclaimed, a backend deadline). The
    attempt checkpoints and parks SCHEDULED_RETRY, as it did before #149."""
    seed_attempt(db, task_input=LONG_RUN, pool_active=4)
    _expect(db, "scan-01.md")
    worker, _config, _exporter = worker_factory(**QUIET)

    trigger = Trigger(worker, lambda: sigterm(worker))
    exit_code = worker.run()
    trigger.join()

    assert exit_code == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value, task["state"]
    assert task["park_reason"] == ParkReason.SCHEDULED_RETRY.value
    assert db.doc("leases/lease_1")["released_at"] is not None
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked and parked[-1]["detail"].get("cause") == "worker_interrupted", parked
    assert_not_failed_for_missing_outputs(db, log_stream)


def test_a_cancel_with_an_expected_output_missing_is_cancelled_as_before(
    db, worker_factory, log_stream
):
    """A person cancelled the task mid-run. It ends CANCELLED with the cancel as
    its cause, not with the file the stopped agent had not written."""
    seed_attempt(db, task_input={"prompt": "cancel me", "steps": 40, "sleep_seconds": 8.0})
    _expect(db, "scan-01.md")
    worker, _config, _exporter = worker_factory(control_poll_seconds=1, timeout_seconds=30)

    def cancel() -> None:
        time.sleep(1.5)
        db.doc("tasks/task_1")["cancel_requested"] = True

    thread = threading.Thread(target=cancel)
    thread.start()
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.CANCELLED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.CANCELLED.value, task["state"]
    assert task["last_error"] == "cancelled by request", task["last_error"]
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert_not_failed_for_missing_outputs(db, log_stream)


def test_a_crash_with_an_expected_output_missing_keeps_its_own_cause(
    db, worker_factory, log_stream
):
    """The worker itself failed mid-run (a heartbeat write raising, which is how
    a Firestore outage reaches the supervision loop). The crash handler ends
    the task FAILED with the crash as its cause, terminal, as before #149."""
    seed_attempt(db, task_input=LONG_RUN, pool_active=4)
    _expect(db, "scan-01.md")
    worker, _config, _exporter = worker_factory(
        heartbeat_interval_seconds=1,
        checkpoint_interval_seconds=60,
        control_poll_seconds=60,
        timeout_seconds=60,
    )
    crashed = threading.Event()
    real_heartbeat = worker._heartbeat

    def heartbeat() -> None:
        if crashed.is_set():
            raise RuntimeError("the heartbeat write failed")
        real_heartbeat()

    worker._heartbeat = heartbeat  # type: ignore[method-assign]

    trigger = Trigger(worker, crashed.set)
    exit_code = worker.run()
    trigger.join()

    assert exit_code == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert "the heartbeat write failed" in str(task["last_error"]), task["last_error"]
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert_not_failed_for_missing_outputs(db, log_stream)
