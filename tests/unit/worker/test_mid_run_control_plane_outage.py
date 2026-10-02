"""A Firestore outage while the agent runs is survived, or requeued, never FAILED (#70).

Owner decision, 2026-09-28. Every Firestore call a worker made mid-run kept
the client library's defaults, and any error it finally raised reached the
crash handler, which wrote FAILED -- terminal, with no retry, for an outage the
next attempt would not have met. Now:

  * each mid-run call has an explicit budget (`control.MID_RUN_BUDGETS`), the
    heartbeat's under the lease extension it grants;
  * a call that fails while the lease this worker last extended is still live
    is logged, and the run goes on;
  * once that lease has run out with Firestore still unreachable, the worker
    checkpoints (`control_plane_outage`), stops the runner, writes its own
    attempt document and exits 69, which the reconciler requeues;
  * a checkpoint whose record cannot be written no longer reaches the crash
    handler;
  * the heartbeat is CONDITIONAL on the lease, so a stale or retried beat never
    touches a released lease (invariant 5).

The fake Firestore here is the suite's own, with an outage switched on for
the task and lease documents once the runner has started. The attempt
documents stay writable, so what the worker writes on its way out can be read.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from google.api_core import exceptions as core

from agent_worker.control import MID_RUN_BUDGETS, ControlPlane
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from swarm_common.states import EventType, TaskState

import fakes
from conftest import TENANT, seed_attempt

QUIET = dict(
    heartbeat_interval_seconds=1,
    control_poll_seconds=1,
    checkpoint_interval_seconds=60,
    termination_grace_seconds=1,
)


class Outage:
    """Firestore unreachable for the task and lease documents, while switched on.

    `failures` bounds it: that many calls fail and then the service is back.
    None fails every call for as long as it is on.
    """

    def __init__(self, failures: int | None) -> None:
        self.on = False
        self.failures = failures
        self.failed = 0

    def hit(self, path: str) -> None:
        if not self.on or not path.startswith(("tasks/", "leases/")):
            return
        if self.failures is not None and self.failed >= self.failures:
            return
        self.failed += 1
        raise core.ServiceUnavailable(f"firestore unreachable for {path}")


@pytest.fixture
def outage(monkeypatch) -> Outage:
    box: dict[str, Outage] = {}

    def install(failures: int | None) -> Outage:
        box["o"] = Outage(failures)
        state = box["o"]
        for name in ("get", "set", "update"):
            real = getattr(fakes.FakeDocumentRef, name)

            def wrapped(self, *args: Any, _real=real, **kwargs: Any) -> Any:
                state.hit(self.path)
                return _real(self, *args, **kwargs)

            monkeypatch.setattr(fakes.FakeDocumentRef, name, wrapped)
        return state

    return install  # type: ignore[return-value]


def _down_once_the_runner_starts(worker, state: Outage) -> None:
    supervised = worker._run_child_supervised

    def run(child_env):
        state.on = True
        return supervised(child_env)

    worker._run_child_supervised = run  # type: ignore[method-assign]


def test_an_outage_shorter_than_the_budget_ends_succeeded(db, worker_factory, outage):
    seed_attempt(db, task_input={"prompt": "x", "steps": 6, "sleep_seconds": 3.5})
    worker, _, _ = worker_factory(heartbeat_extension_seconds=120, **QUIET)
    state = outage(2)
    _down_once_the_runner_starts(worker, state)

    assert worker.run() == ExitCode.OK
    assert state.failed == 2, "the outage never happened, so nothing was measured"
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


def test_an_outage_past_the_lease_exits_69_with_a_checkpoint_first_never_failed(
    db, store, worker_factory, outage, log_stream
):
    seed_attempt(db, task_input={"prompt": "x", "steps": 40, "sleep_seconds": 40})
    worker, _, _ = worker_factory(heartbeat_extension_seconds=2, **QUIET)
    state = outage(None)
    _down_once_the_runner_starts(worker, state)

    assert worker.run() == ExitCode.UNAVAILABLE
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value, "the task was written through an outage"
    assert task.get("last_error") is None
    assert EventType.FAILED.value not in db.event_types("task_1")

    attempt = db.doc("attempts/att_1")
    assert attempt["exit_code"] == ExitCode.UNAVAILABLE == 69
    assert "control_plane_outage" in attempt["error"]
    # The checkpoint was taken first: its archive and manifest are in the bucket,
    # labelled for the outage, and listed on the attempt's own document.
    labels = [
        json.loads(store.download_bytes(key)).get("label")
        for key in store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/att_1/")
        if key.endswith("/manifest.json")
    ]
    assert "control_plane_outage" in labels, labels
    assert attempt["checkpoints"], "the checkpoint is not on the attempt's document"
    records = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    assert not any("worker crashed" in r.get("message", "") for r in records)


def test_a_checkpoint_record_that_cannot_be_written_does_not_fail_the_attempt(
    db, worker_factory, monkeypatch
):
    seed_attempt(db, task_input={"prompt": "x", "steps": 4, "sleep_seconds": 2.5})
    worker, _, _ = worker_factory(checkpoint_interval_seconds=1, heartbeat_interval_seconds=1,
                                  control_poll_seconds=1)
    real = worker.control.record_checkpoint
    calls = {"n": 0}

    def flaky(**kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise core.ServiceUnavailable("the pointer could not be written")
        return real(**kwargs)

    worker.control.record_checkpoint = flaky  # type: ignore[method-assign]

    assert worker.run() == ExitCode.OK
    assert calls["n"] >= 2
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


def _control(db) -> ControlPlane:
    return ControlPlane(
        db, task_id="task_1", attempt_id="att_1", lease_id="lease_1", tenant_id=TENANT,
        generation=1,
        logger=build_logger(task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
                            generation=1, runner_profile="mock", stream=io.StringIO()),
        txn_runner=fakes.FakeTransactionRunner(db),
    )


def test_a_heartbeat_after_release_writes_nothing(db):
    seed_attempt(db, state=TaskState.RUNNING, lease_released=True)
    before = dict(db.doc("leases/lease_1"))
    db.writes.clear()

    assert _control(db).heartbeat() is False
    assert db.writes == []
    assert db.doc("leases/lease_1") == before


def test_a_heartbeat_at_another_generation_writes_nothing(db):
    seed_attempt(db, state=TaskState.RUNNING)
    db.doc("leases/lease_1")["generation"] = 2
    db.writes.clear()

    assert _control(db).heartbeat() is False
    assert db.writes == []


def test_a_live_lease_is_extended(db):
    seed_attempt(db, state=TaskState.RUNNING)
    before = db.doc("leases/lease_1")["expires_at"]

    assert _control(db).heartbeat() is True
    lease = db.doc("leases/lease_1")
    assert lease["heartbeat_at"] is not None and lease["expires_at"] > before


def test_each_mid_run_call_carries_its_budget_and_the_heartbeat_stays_under_the_lease():
    control = _control(fakes.FakeFirestore())
    for call, (deadline, timeout) in MID_RUN_BUDGETS.items():
        options = control.call_options(call)
        assert options["timeout"] == timeout, call
        expected = min(deadline, 0.75 * control.heartbeat_extension_seconds) if call == "heartbeat" else deadline
        assert options["retry"].timeout == expected, call
    assert MID_RUN_BUDGETS["heartbeat"][0] == 90
    assert control.call_options("heartbeat")["retry"].timeout < control.heartbeat_extension_seconds
    assert control.call_options() == {}, "a call naming no budget keeps the library's defaults"
