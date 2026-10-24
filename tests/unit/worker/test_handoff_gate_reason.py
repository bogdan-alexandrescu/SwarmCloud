"""`swarm-handoff device` and `theme` wait out a restarting gate and say why it is down (GATE-STAYS-UP; #2205).

MEASURED 2026-10-24: task_f99357b6f05743d1b9c7's `swarm-handoff device
desktop` answered a bare exit 5 `gate_unavailable` four times in three
minutes, and the review could not tell a browser coming back from one that
never would. Now the worker waits a bounded time (`GATE_WAIT_SECONDS`) for a
gate whose browser is `starting` or `restarting`, says it waited, and answers
anything else with the reason the gate gave (`crashed`, `wiped`,
`unresponsive`) or its own (`not_enabled`).

The worker's real `HandoffPath` and the real `swarm-handoff` command, over a
scripted gate client that raises L3's errors by name. The clock and the sleep
are the test's, so a 15 s wait takes no time. No cloud credential.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_handoff import (  # noqa: F401 -- `trace` is a fixture
    CTL,
    CtlError,
    FakeGateClient,
    FakeLiveRoutes,
    _answer,
    _command_module,
    _Log,
    trace,
)

from agent_worker import children as children_mod
from agent_worker import handoff as handoff_mod
from agent_worker.hardening import PROTECTED, MemoryProtection


class CtlTimeout(CtlError):
    """L3's name for a call the gate did not answer in time."""


class CtlRefused(CtlError):
    """L3's name for a refusal, with the gate's error word (`refused:restarting`)."""

    def __init__(self, message: str, error: str | None = None) -> None:
        super().__init__(message)
        self.error = error


class RestartingGate(FakeGateClient):
    """A gate whose browser is down for the first `down_for` seconds of the
    test's clock, answering `refused:<reason>` meanwhile, as `LiveGate._need` does."""

    def __init__(self, trace: list[str], clock: "Clock", *, down_for: float, reason: str = "restarting") -> None:
        super().__init__(trace)
        self.clock = clock
        self.down_for = down_for
        self.reason = reason

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        result = super().call(op, args)
        if op in ("device", "theme") and self.clock.now < self.down_for:
            raise CtlRefused(f"the gate refused {op}: refused:{self.reason}", f"refused:{self.reason}")
        return result


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _clocked(clock: Clock) -> dict[str, Any]:
    """The test's monotonic clock, where `HandoffPath` takes one (so the same
    test reads red on a worker that has none, rather than not constructing)."""
    if "monotonic" in inspect.signature(handoff_mod.HandoffPath).parameters:
        return {"monotonic": clock.monotonic}
    return {}


def _path(tmp_path: Path, db, trace, gate, clock: Clock):
    routes = FakeLiveRoutes(db, trace)
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    cfg = SimpleNamespace(tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
                          generation=1, runner_profile="mock")
    path = handoff_mod.HandoffPath(
        cfg, logger=_Log(), memory=MemoryProtection(PROTECTED, "test"),
        children=SimpleNamespace(_key=key, _api=routes, unavailable=None),
        live=True, gate_client=lambda p: gate, environ={"SWARM_GATE_CTL": CTL},
        sleep=clock.sleep, **_clocked(clock),
    )
    path.setup()
    work = tmp_path / "work"
    work.mkdir()
    path.prepare(work)
    db.seed("tasks/task_1", {"id": "task_1", "tenant_id": "eng", "state": "RUNNING"})
    return path, work


def _ask(work: Path, kind: str, request_id: str, document: dict[str, Any]) -> None:
    (work / handoff_mod.SPOOL_DIR_NAME / kind / f"{request_id}.json").write_text(json.dumps(document))


def _warnings(path) -> list[dict[str, Any]]:
    return [kw for msg, kw in path.log.lines if msg == "the live browser is unavailable"]


def test_a_restarting_gate_is_waited_for_and_the_switch_applies_when_it_comes_back(tmp_path, db, trace):
    clock = Clock()
    gate = RestartingGate(trace, clock, down_for=4.0)
    path, work = _path(tmp_path, db, trace, gate, clock)
    _ask(work, "devices", "d1", {"preset": "desktop"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    answer = _answer(work, "d1")
    assert answer["outcome"] == "applied" and answer["exit"] == 0 and answer["preset"] == "desktop"
    assert answer["waited_seconds"] == 4
    assert "waited 4 s" in answer["message"]
    assert 4.0 <= clock.now <= handoff_mod.GATE_WAIT_SECONDS
    assert ("the live browser came back", {"waited_seconds": 4, "task_id": "task_1"}) in path.log.lines


def test_a_theme_waits_for_a_starting_gate_the_same_way(tmp_path, db, trace):
    clock = Clock()
    gate = RestartingGate(trace, clock, down_for=2.0, reason="starting")
    path, work = _path(tmp_path, db, trace, gate, clock)
    _ask(work, "themes", "t1", {"theme": "dark"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    answer = _answer(work, "t1")
    assert answer["outcome"] == "applied" and answer["theme"] == "dark" and answer["waited_seconds"] == 2


def test_a_gate_that_never_comes_back_is_refused_with_its_reason_after_the_bound(tmp_path, db, trace):
    clock = Clock()
    gate = RestartingGate(trace, clock, down_for=10_000.0)
    path, work = _path(tmp_path, db, trace, gate, clock)
    _ask(work, "devices", "d1", {"preset": "desktop"})
    _ask(work, "themes", "t1", {"theme": "dark"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    answer = _answer(work, "d1")
    assert answer["outcome"] == "refused" and answer["exit"] == 5
    assert answer["code"] == handoff_mod.GATE_UNAVAILABLE and answer["reason"] == "restarting"
    assert 0 < answer["waited_seconds"] <= handoff_mod.GATE_WAIT_SECONDS <= 60
    assert "restarting" in answer["message"] and "waited" in answer["message"]
    # One budget per poll: the theme behind it does not wait as long again.
    theme = _answer(work, "t1")
    assert theme["reason"] == "restarting"
    assert clock.now <= handoff_mod.GATE_WAIT_SECONDS
    # The worker's log line says why, with the task.
    warning = _warnings(path)[0]
    assert warning["reason"] == "restarting" and warning["task_id"] == "task_1" and warning["op"] == "device"


def test_a_crashed_gate_is_refused_at_once_with_crashed(tmp_path, db, trace):
    clock = Clock()
    gate = RestartingGate(trace, clock, down_for=10_000.0, reason="crashed")
    path, work = _path(tmp_path, db, trace, gate, clock)
    _ask(work, "devices", "d1", {"preset": "desktop"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    answer = _answer(work, "d1")
    assert answer["reason"] == "crashed" and answer["waited_seconds"] == 0 and clock.slept == []


def test_a_lost_channel_is_crashed_and_a_timed_out_call_is_unresponsive(tmp_path, db, trace):
    clock = Clock()
    gate = FakeGateClient(trace)
    path, work = _path(tmp_path, db, trace, gate, clock)
    gate.lose_on = "device"
    _ask(work, "devices", "d1", {"preset": "desktop"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    assert _answer(work, "d1")["reason"] == "crashed"

    class Slow(FakeGateClient):
        def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
            if op == "theme":
                raise CtlTimeout("the gate did not answer within 10s")
            return super().call(op, args)

    (tmp_path / "again").mkdir()
    path, work = _path(tmp_path / "again", db, trace, Slow(trace), clock)
    _ask(work, "themes", "t1", {"theme": "dark"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    answer = _answer(work, "t1")
    assert answer["code"] == handoff_mod.GATE_UNAVAILABLE and answer["reason"] == "unresponsive"


def test_a_preset_the_gate_refuses_is_still_preset_refused_and_the_next_switch_works(tmp_path, db, trace):
    """A refusal is answered as a refusal and nothing else changes."""

    class Picky(FakeGateClient):
        def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
            result = super().call(op, args)
            if op == "device" and args["preset"] == "fridge":
                raise CtlRefused("the gate refused device: failed", "failed")
            return result

    clock = Clock()
    gate = Picky(trace)
    path, work = _path(tmp_path, db, trace, gate, clock)
    _ask(work, "devices", "d1", {"preset": "fridge"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    assert _answer(work, "d1")["code"] == "preset_refused"
    _ask(work, "devices", "d2", {"preset": "desktop"})
    _ask(work, "themes", "t2", {"theme": "light"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    assert _answer(work, "d2")["outcome"] == "applied" and "waited_seconds" not in _answer(work, "d2")
    assert _answer(work, "t2")["outcome"] == "applied"
    assert clock.slept == []


def test_a_device_still_switches_after_a_pause_the_gate_did_not_confirm(tmp_path, db, trace):
    """The hand-off's failure is the hand-off's: the gate and the agent's browser
    are still there (NOLIVE-GUARD), so a device switch still reaches them."""
    clock = Clock()
    gate = FakeGateClient(trace)
    path, work = _path(tmp_path, db, trace, gate, clock)
    path.unavailable = handoff_mod.GATE_UNAVAILABLE  # what a lost `pause` leaves behind
    _ask(work, "devices", "d1", {"preset": "desktop"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    assert _answer(work, "d1")["outcome"] == "applied"
    assert ("device", {"preset": "desktop"}) in gate.ops


def test_an_attempt_without_the_gate_says_not_enabled(tmp_path, db, trace):
    clock = Clock()
    gate = FakeGateClient(trace)
    gate.refuse_registration = True
    path, work = _path(tmp_path, db, trace, gate, clock)
    _ask(work, "devices", "d1", {"preset": "desktop"})
    path.tick(work, lambda: db.doc("tasks/task_1"))
    answer = _answer(work, "d1")
    assert answer["code"] == handoff_mod.GATE_UNAVAILABLE and answer["reason"] == "not_enabled"
    assert "device" not in gate.names()


def test_the_command_prints_the_reason_and_the_wait(tmp_path, monkeypatch, capsys):
    """`swarm-handoff device` prints the worker's answer as it is: the reason
    and the seconds waited reach the agent."""
    cmd = _command_module()
    root = tmp_path / "spool"
    (root / "responses").mkdir(parents=True)
    monkeypatch.setenv("SWARM_HANDOFF", str(root))
    answer = {"outcome": "refused", "exit": 5, "code": "gate_unavailable", "reason": "restarting",
              "message": "the live browser is restarting after Chromium exited (waited 15 s for it)",
              "waited_seconds": 15}

    def answered(path, document):
        (root / "responses" / (path.stem + ".json")).write_text(json.dumps({"request_id": path.stem, **answer}))

    monkeypatch.setattr(cmd, "_write_atomic", answered)
    assert cmd.main(["device", "desktop", "--max-seconds", "1", "--poll", "0.1"]) == 5
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["reason"] == "restarting" and printed["waited_seconds"] == 15
