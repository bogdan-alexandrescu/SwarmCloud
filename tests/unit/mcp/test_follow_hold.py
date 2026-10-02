"""A `progress` follow wakes on a STATE change, and an unstarted step does not poll.

MEASURED, 2026-10-01 (#448). The largest `sc:step` row of that evening made 20
`swarm_follow` calls over 39 turns and re-read 830k cached tokens, to report a
step whose state changed four times. Two things made it poll:

* a held call ended on any change to its wake key, and the row's instructions
  capped every running call at 120 s, so a twenty-minute RUNNING step was ten
  calls whose replies said nothing;
* a step whose parents had not finished made a long call per turn for as long
  as they ran -- the row had no way to say "wake me when my parents are done".

Owner decisions, 2026-10-01: a progress follow holds until the task's STATE
changes or it is terminal, up to `compact.MAX_WAIT_SECONDS` (1800 s, stated
once, with why it sits under the MCP tool-call timeout); progress ticks inside
one state do not end it. A row whose step has unfinished parents makes ONE call
that holds until every parent is terminal or the task leaves its dependency
wait. And a simulated three-step workflow costs at most five calls a step.

Everything here runs against a fake API whose tasks move on a timeline read
from the same fake clock the hold sleeps on, so "ten minutes" is ten simulated
minutes and no test waits.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.states import CONCURRENCY_STATES, TERMINAL_STATES
from swarm_mcp import compact, server

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class Timeline:
    """Tasks whose documents are a function of simulated time.

    `phases[task_id]` is a list of `(start_second, state, park_reason)`; the
    task is in the last phase whose start has passed. A RUNNING task's attempt
    spend and checkpoint move every minute, which is the "progress tick" a hold
    must not wake on.
    """

    def __init__(self, phases: dict[str, list[tuple[float, str, str | None]]], steps: dict[str, str]) -> None:
        self.phases = phases
        self.steps = steps
        self.now = 0.0
        self.reads: list[str] = []

    # The clock and sleep a hold is given.
    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.now)

    def _phase(self, task_id: str) -> tuple[float, str, str | None]:
        current = self.phases[task_id][0]
        for phase in self.phases[task_id]:
            if phase[0] <= self.now:
                current = phase
        return current

    def _started(self, task_id: str) -> float | None:
        for start, state, _ in self.phases[task_id]:
            if state in {s.value for s in CONCURRENCY_STATES}:
                return start
        return None

    # The client surface `compact` reads.
    def task(self, task_id: str) -> dict:
        self.reads.append(task_id)
        start, state, reason = self._phase(task_id)
        doc = {
            "task_id": task_id,
            "step_id": self.steps[task_id],
            "state": state,
            "park_reason": reason,
            "attempt_count": 1 if self._started(task_id) is not None and self.now >= self._started(task_id) else 0,
            "max_attempts": 3,
            "created_at": T0.isoformat(),
            "updated_at": (T0 + timedelta(seconds=start)).isoformat(),
        }
        began = self._started(task_id)
        if began is not None and self.now >= began:
            doc["started_at"] = (T0 + timedelta(seconds=began)).isoformat()
        if state in {s.value for s in TERMINAL_STATES}:
            doc["completed_at"] = (T0 + timedelta(seconds=start)).isoformat()
            doc["result_summary"] = {"runner": {"summary": "done"}, "artifacts": []}
        return doc

    def attempts(self, task_id: str) -> list[dict]:
        minutes = int(self.now // 60)
        return [{"attempt_id": "att_1", "cost_usd": 0.01 * minutes, "input_tokens": 1000 * minutes, "output_tokens": 0}]

    def events_page(self, task_id: str, *, limit: int, newest_first: bool):
        minutes = int(self.now // 60)
        at = (T0 + timedelta(minutes=minutes)).isoformat()
        return [{"type": "checkpoint_completed", "at": at}], None, True

    def __getattr__(self, name):
        # `progress.outcome` reads more routes for a finished task; each one
        # answers "nothing recorded", which the outcome reports as such.
        def _nothing(*_args, **_kwargs):
            from swarm_mcp.client import SwarmError

            raise SwarmError(f"{name} is not part of this fake", status=404)
        return _nothing


def _running_then_done(run_for: float) -> list[tuple[float, str, str | None]]:
    return [(0, "RUNNING", None), (run_for, "SUCCEEDED", None)]


# --------------------------------------------------------------------------
# The constant, stated once, under the tool-call timeout
# --------------------------------------------------------------------------


def test_the_maximum_hold_is_1800_seconds_and_names_the_tool_call_timeout_beside_it():
    assert compact.MAX_WAIT_SECONDS == 1800
    with open(compact.__file__, encoding="utf-8") as handle:
        source = handle.read()
    at = source.index("MAX_WAIT_SECONDS = 1800")
    comment = source[max(0, at - 2500):at]
    assert "MCP_TOOL_TIMEOUT" in comment, "the hold's comment must name the tool-call timeout it sits under"
    assert re.search(r"\b1800\b", comment) or "thirty minutes" in comment.lower()


def test_the_tool_schema_states_the_maximum_hold_from_the_constant():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_follow"]
    text = json.dumps(tool)
    assert str(compact.MAX_WAIT_SECONDS) in text
    assert "600" not in tool["inputSchema"]["properties"]["wait_seconds"]["description"]


# --------------------------------------------------------------------------
# Wake on a state change, not on a tick
# --------------------------------------------------------------------------


@pytest.mark.parametrize("minutes", [10, 25], ids=["ten-minutes", "past-the-old-600s-cap"])
def test_minutes_of_progress_ticks_in_running_end_one_call_at_the_state_change(minutes):
    """A RUNNING task whose spend and checkpoint move every minute for ten
    minutes (and, as the control, for 25 -- past the 600 s the hold used to
    stop at), then SUCCEEDS: one held call, returning at the change."""
    world = Timeline({"task_a": _running_then_done(minutes * 60)}, {"task_a": "review"})
    first = compact.watch_progress(world, ["task_a"], step_id="review",
                                   sleep=world.sleep, clock=world.clock, now=world.wall)
    assert first["stop"] is False

    calls = 0
    reply = first
    while not reply["stop"]:
        calls += 1
        reply = compact.watch_progress(
            world, ["task_a"], since=reply["since"], wait_seconds=compact.MAX_WAIT_SECONDS,
            step_id="review", sleep=world.sleep, clock=world.clock, now=world.wall,
        )
        assert calls < 10, "the hold woke on progress ticks"

    assert calls == 1, f"{calls} held calls for one state change; ticks must not end a hold"
    assert minutes * 60 <= world.now < minutes * 60 + compact.POLL_SECONDS + 1, world.now
    assert any("RUNNING → SUCCEEDED" in t for t in reply["transitions"]), reply["transitions"]
    assert reply["tasks"][0]["outcome"]["state"] == "SUCCEEDED"


def test_start_up_inside_capacity_is_one_state_for_the_hold():
    """LEASED → DISPATCHED → STARTING → RUNNING is start-up the row has nothing
    to do about, and each would cost a call. The hold wakes on entering
    capacity and on leaving it."""
    world = Timeline(
        {"task_a": [(0, "READY", None), (20, "LEASED", None), (30, "DISPATCHED", None),
                    (45, "STARTING", None), (70, "RUNNING", None), (900, "SUCCEEDED", None)]},
        {"task_a": "a"},
    )
    reply = compact.watch_progress(world, ["task_a"], sleep=world.sleep, clock=world.clock, now=world.wall)
    states = []
    while not reply["stop"]:
        reply = compact.watch_progress(world, ["task_a"], since=reply["since"],
                                       wait_seconds=compact.MAX_WAIT_SECONDS,
                                       sleep=world.sleep, clock=world.clock, now=world.wall)
        states.append(reply["tasks"][0]["state"])
    assert len(states) == 2, states
    assert states[-1] == "SUCCEEDED"


def test_a_hold_that_sees_no_change_lasts_the_whole_maximum():
    world = Timeline({"task_a": _running_then_done(10_000)}, {"task_a": "a"})
    first = compact.watch_progress(world, ["task_a"], sleep=world.sleep, clock=world.clock, now=world.wall)
    got = compact.watch_progress(world, ["task_a"], since=first["since"], wait_seconds=99_999,
                                 sleep=world.sleep, clock=world.clock, now=world.wall)
    assert world.now == compact.MAX_WAIT_SECONDS == 1800
    assert got["stop"] is False


# --------------------------------------------------------------------------
# An unstarted step does not poll
# --------------------------------------------------------------------------


def test_a_dependent_steps_row_makes_one_call_while_its_parent_runs():
    """The parent runs for twenty minutes; the child waits PARKED on it. The
    child's FIRST call, naming the parent, holds the whole time and returns
    when the parent finishes."""
    world = Timeline(
        {
            "task_parent": [(0, "RUNNING", None), (1200, "SUCCEEDED", None)],
            "task_child": [(0, "PARKED", "DEPENDENCY_INCOMPLETE"), (1205, "READY", None)],
        },
        {"task_parent": "implement", "task_child": "review"},
    )
    got = compact.watch_progress(
        world, ["task_child"], step_id="review", parents=["task_parent"],
        wait_seconds=compact.MAX_WAIT_SECONDS, sleep=world.sleep, clock=world.clock, now=world.wall,
    )
    assert 1200 <= world.now <= 1200 + compact.PENDING_POLL_CAP_SECONDS + 1, world.now
    assert got["tasks"][0]["state"] in ("PARKED", "READY")
    assert got["parents"] == {"task_parent": "SUCCEEDED"}
    (line,) = got["progress"]
    assert "review" in line


def test_the_parent_hold_ends_when_the_task_leaves_its_dependency_wait():
    world = Timeline(
        {
            "task_parent": [(0, "RUNNING", None), (5000, "SUCCEEDED", None)],
            "task_child": [(0, "QUEUED", None), (300, "CANCELLED", None)],
        },
        {"task_parent": "p", "task_child": "c"},
    )
    got = compact.watch_progress(
        world, ["task_child"], parents=["task_parent"], wait_seconds=compact.MAX_WAIT_SECONDS,
        sleep=world.sleep, clock=world.clock, now=world.wall,
    )
    assert 300 <= world.now <= 300 + compact.PENDING_POLL_CAP_SECONDS + 1, world.now
    assert got["stop"] is True and got["tasks"][0]["state"] == "CANCELLED"


def test_the_parent_hold_is_reachable_through_the_tool(monkeypatch):
    world = Timeline(
        {"task_parent": [(0, "SUCCEEDED", None)], "task_child": [(0, "READY", None)]},
        {"task_parent": "p", "task_child": "c"},
    )
    reply = json.loads(server._call(world, "swarm_follow", {
        "task_ids": ["task_child"], "format": "progress", "step_id": "c",
        "parents": ["task_parent"], "wait_seconds": 0,
    }))
    assert reply["parents"] == {"task_parent": "SUCCEEDED"}
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_follow"]
    assert "parents" in tool["inputSchema"]["properties"]


def test_parents_are_refused_outside_the_progress_format():
    world = Timeline({"task_a": [(0, "READY", None)]}, {"task_a": "a"})
    from swarm_mcp.client import SwarmError

    with pytest.raises(SwarmError, match="parents"):
        server._call(world, "swarm_follow", {"task_ids": ["task_a"], "format": "lines", "parents": ["x"]})


# --------------------------------------------------------------------------
# A three-step workflow, row by row: at most five calls a step
# --------------------------------------------------------------------------


def _row(world: Timeline, task_id: str, step_id: str, parents: list[str]) -> int:
    """`plugin/agents/step.md`, as a loop: the first call (naming the parents
    when there are any), then held calls with `since` until `stop`. Every call
    uses the maximum hold. Returns how many calls the row made."""
    world.now = 0.0
    arguments = {"step_id": step_id, "wait_seconds": compact.MAX_WAIT_SECONDS,
                 "sleep": world.sleep, "clock": world.clock, "now": world.wall}
    reply = compact.watch_progress(world, [task_id], parents=parents or None, **arguments)
    calls = 1
    while not reply["stop"]:
        reply = compact.watch_progress(world, [task_id], since=reply["since"], **arguments)
        calls += 1
        assert calls <= 50, f"{step_id}'s row is polling"
    assert reply["tasks"][0]["outcome"]["state"] == "SUCCEEDED"
    return calls


def test_a_simulated_three_step_workflow_costs_at_most_five_calls_a_step(record_property):
    """implement → review → fix, each running twenty minutes after a realistic
    admission and start-up. Every state a task passes through is on the
    timeline -- the hold is what keeps them from each costing a call."""
    def step(after: float) -> list[tuple[float, str, str | None]]:
        phases: list[tuple[float, str, str | None]] = []
        if after:
            phases.append((0, "PARKED", "DEPENDENCY_INCOMPLETE"))
        else:
            phases.append((0, "QUEUED", None))
        phases += [
            (after + 10, "READY", None),
            (after + 25, "LEASED", None),
            (after + 35, "DISPATCHED", None),
            (after + 50, "STARTING", None),
            (after + 80, "RUNNING", None),
            (after + 1280, "SUCCEEDED", None),
        ]
        return phases

    world = Timeline(
        {"t_impl": step(0), "t_review": step(1280), "t_fix": step(2560)},
        {"t_impl": "implement", "t_review": "review", "t_fix": "fix"},
    )
    counts = {
        "implement": _row(world, "t_impl", "implement", []),
        "review": _row(world, "t_review", "review", ["t_impl"]),
        "fix": _row(world, "t_fix", "fix", ["t_review"]),
    }
    record_property("calls_per_row", json.dumps(counts))
    print("calls per row:", counts)
    assert all(n <= 5 for n in counts.values()), counts
