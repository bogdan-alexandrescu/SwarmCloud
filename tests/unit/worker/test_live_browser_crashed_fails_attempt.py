"""A live browser gone for good ends the attempt retryably, named on the task (part of issue 272).

MEASURED 2026-11-23: two review tasks on the browser runner profile lost the
hosted live browser. The gate had given up starting Chromium again, the
agent reported "not rendered", and the task ended as if nothing had failed:
nobody could tell from the task that its browser was what broke.

The previous step made the worker know it (`HandoffPath.browser_gone`,
`drain_browser_notes`). Now the worker:

* records each restart (`live_browser_restarted`) and the loss
  (`live_browser_crashed`) once on the task's events, with the exit's cause,
  signal, exit code and `oom_kills` -- never Chromium's stderr;
* when the browser is gone for good while the agent runs, ends an open
  hand-off, stops the agent, CHECKPOINTS (invariant 8), and fails the attempt
  retryably with cause `live_browser_crashed` and `result_summary.live_browser`:
  only a new attempt brings a new sidecar (NOLIVE-GUARD's reasoning);
* does none of that on a fenced attempt, or when a hand-off's hold has just
  expired (that path parks for a person, unchanged).

The worker is the real one over test_handoff's scripted gate client and
swarm-api routes, and the suite's fake Firestore and bucket. No cloud.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_handoff import (  # noqa: F401 -- `agent` and `trace` are fixtures
    REQUEST,
    WAIT,
    FakeGateClient,
    FakeLiveRoutes,
    _live_worker,
    agent,
    trace,
)

from agent_worker import handoff as handoff_mod
from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import EventType, ParkReason, TaskState

from worker_seeds import seed_attempt

QUIET = dict(heartbeat_interval_seconds=1, control_poll_seconds=1, termination_grace_seconds=1)

# Chromium's stderr, as the gate would have seen it: never on the task.
STDERR = "[1:1:FATAL:memory.cc(22)] Out of memory"

RUNNING = {"browser": "running", "browser_restarts": 0, "browser_exit": None}
RESTARTED = {"browser": "running", "browser_restarts": 1,
             "browser_exit": {"exit_cause": "signal_SIGKILL", "signal": "SIGKILL", "exit_code": -9,
                              "oom_kills": 1, "at": 1_790_000_000.0}}
GONE = {"browser": "closed", "browser_restarts": 1,
        "browser_exit": {"exit_cause": "signal_SIGKILL", "signal": "SIGKILL", "exit_code": -9,
                         "oom_kills": 2, "at": 1_790_000_060.0}}

SLEEPER = r'''
import json, os, pathlib, sys, time
record = pathlib.Path(sys.argv[1])
record.write_text("started\n")
time.sleep(float(sys.argv[2]))
with record.open("a") as handle:
    handle.write("finished\n")
pathlib.Path(os.environ["SWARM_RESULT"]).write_text(json.dumps(
    {"status": "succeeded", "summary": "", "output": {}, "error": None,
     "metrics": {}, "artifacts": []}))
'''


class BrowserGate(FakeGateClient):
    """The gate, whose `status` says what its Chromium is doing: one script
    entry per `status` read, the last repeated."""

    def __init__(self, trace: list[str], script: list[dict[str, Any]]) -> None:
        super().__init__(trace)
        self.script = script
        self.reads = 0

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        result = super().call(op, args)
        if op == "status":
            browser = self.script[min(self.reads, len(self.script) - 1)]
            self.reads += 1
            return {**result, **browser, "stderr": STDERR}
        return result


@pytest.fixture
def sleeper(tmp_path, monkeypatch):
    """An agent that runs `seconds` unless it is stopped; says whether it finished."""
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    record = tmp_path / "sleeper.txt"

    def arm(seconds: float) -> Path:
        monkeypatch.setattr(lifecycle, "_runner_argv",
                            lambda cfg: [sys.executable, str(script), str(record), str(seconds)])
        return record

    return arm


def _marks(db, cause: str) -> list[dict[str, Any]]:
    return [e["detail"] for e in db.events("task_1")
            if e["type"] == EventType.RUNNING.value and (e.get("detail") or {}).get("cause") == cause]


def _capacity_released(db) -> bool:
    lease = db.doc("leases/lease_1")
    pools = [db.doc(name if name.startswith("pools/") else f"pools/{name}") for name in lease["pools"]]
    return lease.get("released_at") is not None and all(int(p["active"]) < 1 for p in pools)


def _crash_run(db, worker_factory, trace, monkeypatch, sleeper, script):
    record = sleeper(30)
    seed_attempt(db)
    gate = BrowserGate(trace, script)
    worker = _live_worker(worker_factory, FakeLiveRoutes(db, trace), gate, trace, monkeypatch, **QUIET)
    retried: list[dict[str, Any]] = []
    real = worker.control.fail_retryably

    def fail_retryably(**kw: Any) -> TaskState:
        trace.append("fail_retryably")
        retried.append(kw)
        return real(**kw)

    monkeypatch.setattr(worker.control, "fail_retryably", fail_retryably)
    return worker, gate, record, retried


# --------------------------------------------------------------------------
# each restart, and the loss, recorded once on the task's events
# --------------------------------------------------------------------------


def test_each_restart_is_recorded_once_with_its_exit_cause_and_oom_kills(
    db, worker_factory, trace, monkeypatch, sleeper
):
    script = [RUNNING, RUNNING, RESTARTED, RESTARTED, RESTARTED, GONE]
    worker, gate, record, _ = _crash_run(db, worker_factory, trace, monkeypatch, sleeper, script)
    assert worker.run() == ExitCode.FAILED
    assert gate.reads >= len(script), "the gate's status was not read through the script"

    # MUTATION: record a note on every status read, not once per change, and this fails.
    (restarted,) = _marks(db, "live_browser_restarted")
    assert restarted["restarts"] == 1
    assert restarted["exit_cause"] == "signal_SIGKILL"
    assert restarted["signal"] == "SIGKILL" and restarted["exit_code"] == -9
    # MUTATION: drop oom_kills from the mark and the operator cannot tell memory from a crash.
    assert restarted["oom_kills"] == 1

    (crashed,) = _marks(db, handoff_mod.LIVE_BROWSER_CRASHED)
    assert crashed["restarts"] == 1 and crashed["exit_cause"] == "signal_SIGKILL"
    assert crashed["oom_kills"] == 2

    # Never Chromium's stderr, on any event or on the task.
    assert STDERR not in json.dumps(db.events("task_1"), default=str)
    assert STDERR not in json.dumps(db.doc("tasks/task_1"), default=str)


# --------------------------------------------------------------------------
# gone for good: the agent is stopped, checkpointed, the attempt fails retryably
# --------------------------------------------------------------------------


def test_a_browser_gone_for_good_stops_the_agent_and_fails_the_attempt_retryably(
    db, worker_factory, trace, monkeypatch, sleeper
):
    worker, gate, record, retried = _crash_run(
        db, worker_factory, trace, monkeypatch, sleeper, [RUNNING, RESTARTED, GONE])
    assert worker.run() == ExitCode.FAILED

    # The agent was started, then stopped: it never ran its 30 s to the end.
    assert record.read_text().splitlines() == ["started"], record.read_text()

    (call,) = retried
    assert call["cause"] == handoff_mod.LIVE_BROWSER_CRASHED == "live_browser_crashed"
    assert call["exit_code"] is None
    assert call["retry_delay_seconds"] == handoff_mod.LIVE_GATE_RETRY_DELAY_SECONDS
    assert call["end_cause"] is EndCause.RUNNER_ERROR
    live = call["result_summary"]["live_browser"]
    assert live["state"] == "crashed" and live["restarts"] == 1
    assert live["last_exit"]["cause"] == "signal_SIGKILL" and live["last_exit"]["oom_kills"] == 2
    error = call["error"]
    assert error.startswith("live_browser_crashed: "), error
    assert "SIGKILL" in error and "1 restart" in error
    assert "the display sidecar ran out of memory" in error
    assert "new pod" in error

    # The task names the browser, is back in line, and costs nothing (invariant 1).
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.READY.value, task.get("last_error")
    assert task["last_error"].startswith("live_browser_crashed: ")
    assert task["result_summary"]["live_browser"]["state"] == "crashed"
    assert _capacity_released(db), db.doc("leases/lease_1")
    retrying = [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value]
    assert retrying and retrying[-1]["detail"]["cause"] == handoff_mod.LIVE_BROWSER_CRASHED


def test_the_checkpoint_is_taken_before_the_attempt_is_failed(
    db, worker_factory, trace, monkeypatch, sleeper
):
    worker, gate, record, retried = _crash_run(
        db, worker_factory, trace, monkeypatch, sleeper, [RUNNING, GONE])
    assert worker.run() == ExitCode.FAILED
    label = f"checkpoint:{handoff_mod.LIVE_BROWSER_CRASHED}"
    # MUTATION: fail the attempt before (or without) the checkpoint and this fails (invariant 8).
    assert label in trace, trace
    assert trace.index(label) < trace.index("fail_retryably"), trace
    # The browser is wiped before the checkpoint, as on the park.
    assert trace.index("gate:end_control:wipe") < trace.index(label), trace
    assert db.doc("tasks/task_1")["latest_checkpoint"]


def test_a_browser_that_comes_back_does_not_fail_the_attempt(
    db, worker_factory, trace, monkeypatch, sleeper
):
    record = sleeper(3)
    seed_attempt(db)
    gate = BrowserGate(trace, [RUNNING, RESTARTED])
    worker = _live_worker(worker_factory, FakeLiveRoutes(db, trace), gate, trace, monkeypatch, **QUIET)
    assert worker.run() == ExitCode.OK
    assert record.read_text().splitlines() == ["started", "finished"]
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert len(_marks(db, "live_browser_restarted")) == 1
    assert _marks(db, handoff_mod.LIVE_BROWSER_CRASHED) == []


# --------------------------------------------------------------------------
# not when: fenced, writes forbidden, control plane down, no live browser, wiped
# --------------------------------------------------------------------------


class _Child:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def terminate(self, grace: float, *, reason: str) -> None:
        self.calls.append("terminate")

    def finish(self) -> Any:
        self.calls.append("finish")

    def poll(self) -> Any:
        return None


@pytest.mark.parametrize("flag", ["_fenced_exit", "_writes_forbidden", "_control_plane_down"])
def test_a_fenced_attempt_writes_nothing_on_that_path(db, worker_factory, trace, monkeypatch, tmp_path, flag):
    seed_attempt(db)
    gate = BrowserGate(trace, [GONE])
    worker = _live_worker(worker_factory, FakeLiveRoutes(db, trace), gate, trace, monkeypatch, **QUIET)
    worker.handoff.note_browser(tmp_path, GONE)
    assert worker.handoff.browser_gone is not None
    setattr(worker, flag, True)
    written: list[Any] = []
    monkeypatch.setattr(worker.control, "fail_retryably", lambda **kw: written.append(kw))
    monkeypatch.setattr(worker.control, "emit", lambda *a, **kw: written.append(a))
    child = _Child()
    before = json.dumps(db.doc("tasks/task_1"), default=str)

    # MUTATION: drop the fence guard in Worker._live_browser_after_tick and this fails.
    assert worker._live_browser_after_tick(child, parking=False) is None
    assert written == [] and child.calls == []
    assert "checkpoint:" + handoff_mod.LIVE_BROWSER_CRASHED not in trace
    assert json.dumps(db.doc("tasks/task_1"), default=str) == before


def test_a_fenced_attempt_end_to_end_never_fails_the_task(db, worker_factory, trace, monkeypatch, sleeper):
    """Another attempt took the task as the browser died: the stale worker
    stands down and writes no retry (invariants 3 and 5)."""
    worker, gate, record, retried = _crash_run(
        db, worker_factory, trace, monkeypatch, sleeper, [RUNNING, GONE])

    def superseded(op: str, args: dict[str, Any], _call=gate.call) -> dict[str, Any]:
        if op == "status" and gate.reads >= 1:
            db.doc("tasks/task_1")["current_generation"] = 2
            db.doc("leases/lease_1")["generation"] = 2
        return _call(op, args)

    monkeypatch.setattr(gate, "call", superseded)
    assert worker.run() == ExitCode.GENERATION_FENCED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.RUNNING.value
    assert not (task.get("last_error") or "").startswith(handoff_mod.LIVE_BROWSER_CRASHED)
    assert all(e["type"] != EventType.RETRYING.value for e in db.events("task_1"))
    assert db.doc("leases/lease_1").get("released_at") is None


def test_no_live_browser_or_a_wiped_one_is_not_this_path(db, worker_factory, trace, monkeypatch):
    seed_attempt(db)
    gate = BrowserGate(trace, [GONE])
    worker = _live_worker(worker_factory, FakeLiveRoutes(db, trace), gate, trace, monkeypatch, **QUIET)
    worker.handoff.browser_gone = {"state": "crashed", "restarts": 0, "last_exit": None, "message": "x"}
    called: list[Any] = []
    monkeypatch.setattr(worker.control, "fail_retryably", lambda **kw: called.append(kw))
    child = _Child()
    worker.handoff.live = False
    assert worker._live_browser_after_tick(child, parking=False) is None
    worker.handoff.live = True
    worker.handoff._wiped = True
    assert worker._live_browser_after_tick(child, parking=False) is None
    assert called == [] and child.calls == []


# --------------------------------------------------------------------------
# a hold that expired while a person was waited for still parks for the person
# --------------------------------------------------------------------------


def test_an_expired_hand_off_still_parks_for_a_person(db, worker_factory, agent, trace, monkeypatch):
    write_plan, records = agent
    write_plan(REQUEST, WAIT)
    seed_attempt(db)
    routes = FakeLiveRoutes(db, trace, hold_seconds=0)
    gate = BrowserGate(trace, [RUNNING])

    def dies_while_waited_for() -> None:
        if db.doc("tasks/task_1").get("human_wait"):
            gate.script = [GONE]  # the read that finds the hold expired finds the browser gone

    gate.on_status = dies_while_waited_for
    worker = _live_worker(worker_factory, routes, gate, trace, monkeypatch, **QUIET)
    retried: list[Any] = []
    monkeypatch.setattr(worker.control, "fail_retryably", lambda **kw: retried.append(kw))
    assert worker.run() == ExitCode.PARKED
    assert worker.handoff.browser_gone is not None, "the browser never died: nothing was tested"
    # MUTATION: check browser_gone before the tick's HOLD_EXPIRED and the person's park is lost.
    assert retried == []
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert "park" in trace and "checkpoint:human-wait" in trace
    # The loss is still on the task's events.
    assert len(_marks(db, handoff_mod.LIVE_BROWSER_CRASHED)) == 1
