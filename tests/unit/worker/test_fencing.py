"""Fencing: a superseded worker must not run the agent.

This is the most important test in the execution plane. If it ever fails, two
agents can run the same task concurrently with the same tenant credentials
against the same repository, and the slower one silently overwrites the faster
one's work. Everything else in the worker is recoverable; this is not.
"""

from __future__ import annotations

import threading
import time

import pytest
from fakes import ExplodingChildProcess

from agent_worker import lifecycle
from agent_worker.errors import ExitCode, FencedError
from swarm_common.states import EventType, TaskState

from conftest import seed_attempt


def test_stale_generation_exits_without_running_the_agent(
    db, store, tmp_path, worker_factory, monkeypatch
):
    # The task has moved on to generation 7; this worker was launched for 6.
    seed_attempt(db, generation=6, task_generation=7, state=TaskState.RUNNING)
    worker, config, _ = worker_factory(generation=6)

    # Any attempt to start the runner is a test failure, not an assertion later.
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    exit_code = worker.run()

    assert exit_code == ExitCode.GENERATION_FENCED
    # Nothing ran: no workspace was even created.
    assert not (tmp_path / "workspace" / "att_1").exists()
    # Nothing was written to the bucket.
    assert store.list_keys("tenants/") == []
    # The fencing decision is auditable.
    assert EventType.GENERATION_FENCED.value in db.event_types("task_1")
    fenced = [e for e in db.events("task_1") if e["type"] == EventType.GENERATION_FENCED.value][0]
    assert fenced["detail"]["expected_generation"] == 6
    assert fenced["detail"]["observed_generation"] == 7
    assert fenced["detail"]["phase"] == "startup"


def test_fenced_worker_does_not_touch_the_lease(db, worker_factory, monkeypatch):
    """The live lease belongs to the newer generation. Releasing it here would
    decrement pools the current attempt is still holding."""
    seed_attempt(db, generation=6, task_generation=7, state=TaskState.RUNNING, pool_active=3)
    worker, _, _ = worker_factory(generation=6)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    assert worker.run() == ExitCode.GENERATION_FENCED

    assert db.doc("leases/lease_1")["released_at"] is None
    assert db.doc("pools/global")["active"] == 3
    assert db.doc(f"pools/tenant:eng")["active"] == 3
    # The task document is untouched: it belongs to the live attempt.
    assert db.doc("tasks/task_1")["state"] == TaskState.RUNNING.value
    assert db.doc("tasks/task_1")["current_generation"] == 7


def test_released_lease_is_fenced(db, worker_factory, monkeypatch):
    seed_attempt(db, generation=4, lease_released=True, state=TaskState.LEASED)
    worker, _, _ = worker_factory(generation=4)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    assert worker.run() == ExitCode.GENERATION_FENCED


def test_terminal_task_is_fenced(db, worker_factory, monkeypatch):
    seed_attempt(db, generation=4, state=TaskState.LEASED)
    db.doc("tasks/task_1")["state"] = TaskState.CANCELLED.value
    worker, _, _ = worker_factory(generation=4)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    assert worker.run() == ExitCode.GENERATION_FENCED


def test_lease_pointing_at_another_task_is_fenced(db, worker_factory, monkeypatch):
    seed_attempt(db, generation=2)
    db.doc("leases/lease_1")["task_id"] = "someone_elses_task"
    worker, _, _ = worker_factory(generation=2)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    assert worker.run() == ExitCode.GENERATION_FENCED


def test_current_generation_validates_and_returns_signals(db, worker_factory):
    seed_attempt(db, generation=3, state=TaskState.DISPATCHED)
    worker, _, _ = worker_factory(generation=3)
    signals = worker.control.validate_generation()
    assert signals.generation == 3
    assert signals.is_fenced(3) is False
    assert signals.cancel_requested is False


def test_generation_bumped_mid_run_stops_the_agent_and_keeps_the_lease(db, worker_factory):
    """A reconciler can invalidate a generation while the agent is working."""
    seed_attempt(
        db,
        generation=1,
        state=TaskState.LEASED,
        task_input={"prompt": "long", "steps": 40, "sleep_seconds": 8.0},
    )
    worker, _, _ = worker_factory(generation=1, control_poll_seconds=1, timeout_seconds=30)

    def bump() -> None:
        time.sleep(1.5)
        db.doc("tasks/task_1")["current_generation"] = 99

    thread = threading.Thread(target=bump)
    thread.start()
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.GENERATION_FENCED
    # The lease is left alone even mid-run: the reconciler owns it now.
    assert db.doc("leases/lease_1")["released_at"] is None
    fenced = [e for e in db.events("task_1") if e["type"] == EventType.GENERATION_FENCED.value]
    assert fenced and fenced[-1]["detail"]["phase"] == "running"
