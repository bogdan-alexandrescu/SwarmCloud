"""`swarm_follow` `format: "progress"`: a workflow row that costs bytes, not megabytes.

MEASURED, 2026-10-01. Six wave-1 workflows of three steps each ran under
`/sc:swarmcloud` (then `/sc:run`): eighteen haiku `sc:step` rows, each looping
`swarm_follow` with `format: "lines"` and `wait_seconds: 90`. Every reply carried
5-16 KB of the remote agent's narrated log, and each row re-read its growing
history on every turn: one row used 4.0M tokens over 41 calls, and the six
workflows about 22M tokens in all. Rows whose parents had not started polled
the whole time. What the orchestrator ACTS on is one terminal line per step.

Owner decision, 2026-10-01: keep a live row per step, slim it. So the row
follows `format: "progress"`: no log lines at all, one short progress line per
task -- state, elapsed, attempt n/m, last checkpoint age, tokens and cost so
far, and for a waiting task what it waits for -- the state transitions since
`since`, and nothing it already said. `format: "lines"` stays, opt-in.

`swarm_mcp.compact` is imported PER TEST, through the fixture below, so the
red-first commit failed each of these tests by name rather than failing the
whole collection on one ImportError.
"""

from __future__ import annotations

import importlib
import json

import pytest

from swarm_common.states import EventType
from swarm_mcp import server
from swarm_mcp.client import SwarmError

from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures

#: The byte bound one progress reply for one unfinished task is held to. The
#: `lines` replies this replaces measured 5-16 KB each.
BOUND = 768


@pytest.fixture()
def compact():
    return importlib.import_module("swarm_mcp.compact")


class _Time:
    """A clock the test advances, and a sleep that advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.on_sleep = None

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.slept))


def _stream(count: int) -> str:
    """A claude-code `stream-json` stdout with a large tool result per line."""
    blob = "x" * 4000
    rows = []
    for index in range(count):
        rows.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"LOGMARKER step {index}: reading the whole module"}]}}))
        rows.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": f"LOGMARKER {blob}"}]}}))
    return "\n".join(rows) + "\n"


def _running(world: World, task_id: str = "task_a", step_id: str = "review") -> None:
    world.task(task_id, state="RUNNING")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["step_id"] = step_id
    doc["started_at"] = NOW
    doc["attempt_count"] = 1
    doc["max_attempts"] = 3
    world.attempt(task_id, "att_1")
    # Large logs in every stream a `lines` row would read.
    world.live(task_id, "att_1", _stream(40), stream="agent_stdout")
    world.live(task_id, "att_1", '{"event": "LOGMARKER runner line"}\n' * 500)


def _call(client, **arguments) -> str:
    return server._call(client, "swarm_follow", {"task_ids": ["task_a"], "format": "progress", **arguments})


# --------------------------------------------------------------------------
# No log lines, and a byte bound
# --------------------------------------------------------------------------


def test_the_progress_format_returns_no_log_line_and_stays_under_the_bound(swarm, world):
    _running(world)

    text = _call(swarm, step_id="review")
    reply = json.loads(text)

    assert "LOGMARKER" not in text, "a progress reply carried the agent's log"
    assert "lines" not in reply, "the progress format has no `lines`; that is the opt-in log stream"
    assert len(text.encode("utf-8")) <= BOUND, f"{len(text.encode('utf-8'))} bytes: {text}"
    (line,) = reply["progress"]
    assert "RUNNING" in line and "review" in line, line
    assert "attempt 1/3" in line, line
    assert reply["changed"] is True, "the first call always says where the task is"
    assert reply["stop"] is False and reply["since"]


def test_a_second_call_with_nothing_new_repeats_nothing(swarm, world, compact):
    _running(world)
    clock = _Time()
    first = compact.watch_progress(swarm, ["task_a"], sleep=clock.sleep, clock=clock.clock)

    got = compact.watch_progress(
        swarm, ["task_a"], since=first["since"], wait_seconds=120, sleep=clock.sleep, clock=clock.clock,
    )

    assert got["progress"] == [], "an unchanged task must not be said again"
    assert got["changed"] is False and got["transitions"] == []
    assert sum(clock.slept) == 120, "nothing changed, so the call holds for the whole window"
    assert len(json.dumps(got, separators=(",", ":")).encode("utf-8")) <= BOUND


def test_the_lines_format_is_still_there_as_an_opt_in(swarm, world):
    _running(world)
    reply = json.loads(server._call(swarm, "swarm_follow", {"task_ids": ["task_a"], "format": "lines"}))
    assert any("LOGMARKER" in line for line in reply["lines"]), "`lines` must still stream the log"
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_follow"]
    assert tool["inputSchema"]["properties"]["format"]["enum"] == ["json", "lines", "progress"]


# --------------------------------------------------------------------------
# A step whose parents have not finished does not stream
# --------------------------------------------------------------------------


class _Spy:
    """The real client, with every log, event and attempt read refused."""

    def __init__(self, client) -> None:
        self._client = client
        self.calls: list[str] = []

    def task(self, task_id):
        self.calls.append("task")
        return self._client.task(task_id)

    def __getattr__(self, name):
        def _refused(*_args, **_kwargs):
            self.calls.append(name)
            raise AssertionError(f"a waiting row read {name}()")
        return _refused


def test_a_step_waiting_on_its_parents_makes_no_streaming_read_and_says_why_once(swarm, world, compact):
    world.task("task_a", state="READY")
    doc = world.db.docs["tasks/task_a"]
    doc["park_reason"] = "DEPENDENCY_INCOMPLETE"
    doc["step_id"] = "fix"
    spy = _Spy(swarm)
    clock = _Time()

    first = compact.watch_progress(spy, ["task_a"], step_id="fix", sleep=clock.sleep, clock=clock.clock)
    (line,) = first["progress"]
    assert "waits: dependency" in line, line

    got = compact.watch_progress(
        spy, ["task_a"], since=first["since"], wait_seconds=600, step_id="fix",
        sleep=clock.sleep, clock=clock.clock,
    )

    assert set(spy.calls) == {"task"}, f"a waiting row read more than the task: {spy.calls}"
    assert got["progress"] == [] and got["changed"] is False
    assert sum(clock.slept) == 600, "ONE call holds the whole wait"
    assert max(clock.slept) <= 30, "a pending task is re-read at most every 30 s"
    assert len(spy.calls) <= 25, f"{len(spy.calls)} task reads in ten minutes is a poll loop"


def test_a_step_that_starts_ends_the_wait_and_reports_the_transition(swarm, world, compact):
    world.task("task_a", state="READY")
    doc = world.db.docs["tasks/task_a"]
    doc["park_reason"] = "DEPENDENCY_INCOMPLETE"
    clock = _Time()
    first = compact.watch_progress(swarm, ["task_a"], sleep=clock.sleep, clock=clock.clock)

    def _start(sleeps: int) -> None:
        if sleeps == 2:
            doc["state"] = "RUNNING"
            doc["park_reason"] = None
            doc["started_at"] = NOW
            doc["attempt_count"] = 1

    clock.on_sleep = _start
    got = compact.watch_progress(
        swarm, ["task_a"], since=first["since"], wait_seconds=600, sleep=clock.sleep, clock=clock.clock,
    )

    assert len(clock.slept) == 2, "the call must return on the poll that saw the task start"
    assert got["changed"] is True
    assert any("READY → RUNNING" in t for t in got["transitions"]), got["transitions"]
    (line,) = got["progress"]
    assert "RUNNING" in line


# --------------------------------------------------------------------------
# The fields a progress line carries, and the words for what is not known
# --------------------------------------------------------------------------


def test_cost_tokens_and_checkpoint_are_read_not_invented(swarm, world, compact):
    _running(world)
    world.db.docs["attempts/att_1"].update(
        {"cost_usd": 0.84, "input_tokens": 1_000_000, "output_tokens": 200_000}
    )
    world.event("task_a", EventType.CHECKPOINT_COMPLETED)

    (line,) = compact.watch_progress(swarm, ["task_a"])["progress"]

    assert "$0.84" in line, line
    assert "1.2M tok" in line, line
    assert "checkpoint" in line and "checkpoint none" not in line, line


def test_tokens_count_the_cache_too(swarm, world, compact):
    # #322: input + output alone read 10,007 for a run that used 1.41M.
    _running(world)
    world.db.docs["attempts/att_1"].update(
        {
            "input_tokens": 52,
            "output_tokens": 9_955,
            "cache_read_input_tokens": 1_337_190,
            "cache_creation_input_tokens": 59_715,
        }
    )

    (line,) = compact.watch_progress(swarm, ["task_a"])["progress"]

    assert "1.4M tok" in line, line


def test_a_figure_that_was_not_recorded_says_so_in_one_word(swarm, world, compact):
    _running(world)

    (line,) = compact.watch_progress(swarm, ["task_a"])["progress"]

    assert "cost unrecorded" in line, line
    assert "tokens unrecorded" in line, line
    assert "checkpoint none" in line, line
    assert "$0" not in line, "an unrecorded cost is never $0"


def test_an_unreadable_figure_says_unreadable(swarm, world, compact, monkeypatch):
    _running(world)

    def _boom(*_args, **_kwargs):
        raise SwarmError("attempts route down", status=503)

    monkeypatch.setattr(swarm, "attempts", _boom)
    (line,) = compact.watch_progress(swarm, ["task_a"])["progress"]
    assert "cost unreadable" in line and "tokens unreadable" in line, line


# --------------------------------------------------------------------------
# Finishing, and giving up -- the per-step result is unchanged
# --------------------------------------------------------------------------

#: What `sc:step` answers with, field for field (`STEP_RESULT` in run.js).
STEP_RESULT_FIELDS = {"state", "answer_excerpt", "cost_usd", "duration_s", "pr_url", "artifacts", "last_error"}


def test_a_finished_task_carries_every_field_the_step_result_needs(swarm, world, compact):
    _running(world)
    doc = world.db.docs["tasks/task_a"]
    doc["state"] = "SUCCEEDED"
    doc["completed_at"] = NOW.replace(minute=4, second=12)
    doc["result_summary"] = {
        "runner": {"summary": "done"},
        "artifacts": [{"name": "report.md", "uri": "gs://b/report.md", "bytes": 12}],
        "git": {"commit_count": 1, "pull_request": {"number": 7, "url": "https://github.com/acme/w/pull/7"}},
    }
    world.db.docs["attempts/att_1"]["cost_usd"] = 0.21

    got = compact.watch_progress(swarm, ["task_a"])

    assert got["stop"] is True
    outcome = got["tasks"][0]["outcome"]
    assert STEP_RESULT_FIELDS <= set(outcome), sorted(outcome)
    assert outcome["state"] == "SUCCEEDED"
    assert outcome["cost_usd"] == 0.21
    assert outcome["duration_s"] == 252.0
    assert outcome["pr_url"] == "https://github.com/acme/w/pull/7"
    assert outcome["artifacts"] == ["report.md"], "the row answers with artifact NAMES"
    assert "answer" not in outcome, "the whole answer is not a row's to carry; the excerpt is"


def test_a_task_the_api_does_not_have_stops_the_row_at_once(swarm, world, compact):
    clock = _Time()
    got = compact.watch_progress(swarm, ["task_missing"], wait_seconds=120, sleep=clock.sleep, clock=clock.clock)
    assert got["stop"] is True
    assert got["tasks"][0]["abandoned"] is True
    assert "404" in got["tasks"][0]["abandoned_because"]


def test_another_steps_task_is_not_followed(swarm, world, compact):
    _running(world, step_id="implement")
    got = compact.watch_progress(swarm, ["task_a"], step_id="review")
    assert got["stop"] is True and got["tasks"][0]["abandoned"] is True
    assert "implement" in got["tasks"][0]["abandoned_because"]
    assert "outcome" not in got["tasks"][0]


def test_step_id_is_accepted_with_the_progress_format_through_the_tool(swarm, world):
    _running(world)
    reply = json.loads(_call(swarm, step_id="review"))
    assert reply["tasks"][0]["step_id"] == "review"


def test_a_changed_since_token_is_refused(swarm, world, compact):
    _running(world)
    token = compact.watch_progress(swarm, ["task_a"])["since"]
    flipped = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(SwarmError, match="checksum"):
        compact.watch_progress(swarm, ["task_a"], since=flipped)
