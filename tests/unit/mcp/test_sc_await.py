"""`sc await`: the push path from the bridge (plan wave 2).

A session that attaches no follower rows heard of a finished task only at its
thirty-minute scheduled check. `sc await`, run in the background, exits on the
FIRST change the session should hear of -- and Claude Code re-invokes the
session when a background command exits. Held here, offline, against a fake
API whose tasks move on a timeline read from the same fake clock the follow
sleeps on, so no test waits:

* the first change wins, and a change that is not news (QUEUED to RUNNING)
  does not end the wait;
* the deadline is always there, at most an hour, and its exit code is its own;
* an id that cannot be read is one line, not a crash;
* at most one held follow call at a time, and never a read faster than the
  follow's own interval; a bounded number of tries;
* a line carries no free-text error and nothing credential-shaped;
* the replies that start work, and the check, carry the one field.
"""

from __future__ import annotations

import io
import json
import secrets
import types
from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.states import CONCURRENCY_STATES, TERMINAL_STATES
from swarm_mcp import awaitchange, compact, sc, server
from swarm_mcp.client import SwarmError
from swarm_mcp.invocation import terminal_command

T0 = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)
_HOLDING = {s.value for s in CONCURRENCY_STATES}
_TERMINAL = {s.value for s in TERMINAL_STATES}


class World:
    """Tasks whose documents are a function of simulated time; `gone` ids 404."""

    def __init__(self, phases, *, gone=(), extra=None, workflows=None):
        self.phases = phases
        self.gone = set(gone)
        self.extra = extra or {}
        self.workflows = workflows or {}
        self.now = 0.0
        self.reads: list[tuple[float, str]] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.now)

    def _phase(self, task_id):
        current = self.phases[task_id][0]
        for phase in self.phases[task_id]:
            if phase[0] <= self.now:
                current = phase
        return current

    def task(self, task_id):
        self.reads.append((self.now, task_id))
        if task_id in self.gone:
            raise SwarmError(f"task {task_id} not found", status=404)
        start, state, reason = self._phase(task_id)
        doc = {
            "task_id": task_id, "state": state, "park_reason": reason,
            "created_at": T0.isoformat(), "updated_at": (T0 + timedelta(seconds=start)).isoformat(),
            "attempt_count": 1, "max_attempts": 3,
        }
        if state in _HOLDING:
            doc["started_at"] = T0.isoformat()
        if state in _TERMINAL:
            doc["completed_at"] = (T0 + timedelta(seconds=start)).isoformat()
            doc["result_summary"] = {"artifacts": []}
            doc.update(self.extra.get(task_id, {}))
        return doc

    def request(self, method, path, **_):
        workflow_id = path.rsplit("/", 1)[-1]
        if workflow_id not in self.workflows:
            raise SwarmError(f"workflow {workflow_id} not found", status=404)
        return self.workflows[workflow_id]

    def attempts(self, task_id):
        return []

    def events_page(self, task_id, *, limit, newest_first):
        return [], None, True

    def __getattr__(self, name):
        def _nothing(*_args, **_kwargs):
            raise SwarmError(f"{name} is not part of this fake", status=404)
        return _nothing


def _wait(world, ids, timeout=awaitchange.DEFAULT_TIMEOUT_SECONDS, **kw):
    targets = awaitchange.resolve(world, ids)
    return awaitchange.wait(world, targets, timeout=timeout, sleep=world.sleep,
                            clock=world.clock, now=world.wall, **kw)


# --------------------------------------------------------------------------
# The first change wins
# --------------------------------------------------------------------------


def test_the_first_task_to_finish_ends_the_wait_with_one_line_for_it():
    world = World({
        "task_a": [(0, "RUNNING", None), (900, "SUCCEEDED", None)],
        "task_b": [(0, "RUNNING", None), (300, "FAILED", None)],
    })
    code, lines = _wait(world, ["task_a", "task_b"])
    assert code == awaitchange.EXIT_CHANGED == 0
    assert len(lines) == 1, lines
    assert lines[0].startswith("task_b · ") and " · FAILED · PR none" in lines[0]
    assert 300 <= world.now < 300 + compact.IDLE_POLL_CAP_SECONDS + 1, world.now


def test_a_start_is_not_news_and_a_person_wait_is():
    world = World({"task_a": [(0, "QUEUED", None), (60, "RUNNING", None), (600, "PARKED", "HUMAN_REQUIRED")]})
    code, lines = _wait(world, ["task_a"])
    assert code == 0
    (line,) = lines
    assert "PARKED waiting on a person" in line
    assert world.now >= 600


def test_a_task_already_finished_at_the_start_is_the_baseline_not_a_change():
    world = World({
        "task_done": [(0, "SUCCEEDED", None)],
        "task_run": [(0, "RUNNING", None), (400, "SUCCEEDED", None)],
    })
    code, lines = _wait(world, ["task_done", "task_run"])
    assert code == 0
    assert [ln.split(" · ")[0] for ln in lines] == ["task_run"]


def test_every_item_settled_at_the_start_is_said_at_once():
    world = World({"task_done": [(0, "SUCCEEDED", None)]})
    code, lines = _wait(world, ["task_done"])
    assert code == 0 and lines[0].startswith("task_done · ") and world.now < compact.POLL_SECONDS + 1


def test_a_workflow_id_stands_for_its_steps_and_titles_them():
    world = World(
        {"task_s1": [(0, "RUNNING", None), (120, "SUCCEEDED", None)], "task_s2": [(0, "QUEUED", None)]},
        workflows={"wf_one": {"workflow": {"workflow_id": "wf_one", "steps": [
            {"step_id": "build", "task_id": "task_s1"}, {"step_id": "merge", "task_id": "task_s2"}]},
            "tasks": [{"id": "task_s1", "metadata": {"title": "Nightly"}}]}},
    )
    code, lines = _wait(world, ["wf_one"])
    assert code == 0
    assert lines == [ln for ln in lines if ln.startswith("task_s1 · Nightly / build · SUCCEEDED")], lines


# --------------------------------------------------------------------------
# The deadline
# --------------------------------------------------------------------------


def test_the_deadline_passing_with_nothing_changed_exits_with_its_own_code():
    world = World({"task_a": [(0, "RUNNING", None)]})
    code, lines = _wait(world, ["task_a"], timeout=300)
    assert code == awaitchange.EXIT_DEADLINE
    assert code not in (sc.EXIT_OK, sc.EXIT_FAIL, sc.EXIT_USAGE, sc.EXIT_TROUBLE)
    assert 300 <= world.now < 300 + compact.POLL_SECONDS + 1
    (line,) = lines
    assert line.startswith("nothing changed in 5 minutes")


def test_the_timeout_has_a_default_and_never_exceeds_an_hour():
    assert 0 < awaitchange.DEFAULT_TIMEOUT_SECONDS <= awaitchange.MAX_TIMEOUT_SECONDS == 3600
    assert awaitchange.timeout_seconds("3600") == 3600
    for bad in ("3601", "0", "-5", "an hour"):
        with pytest.raises(SwarmError):
            awaitchange.timeout_seconds(bad)
    assert sc.cmd_await(None, types.SimpleNamespace(timeout="7200", ids=["task_a"], mine=False),
                        io.StringIO()) == sc.EXIT_USAGE


def test_ids_and_mine_are_one_or_the_other():
    for ids, mine in (([], False), (["task_a"], True)):
        args = types.SimpleNamespace(timeout="60", ids=ids, mine=mine)
        assert sc.cmd_await(None, args, io.StringIO()) == sc.EXIT_USAGE


def test_the_tries_are_bounded_even_when_the_clock_does_not_move():
    calls = []

    def hold(client, task_ids, *, since=None, **_):
        calls.append(since)
        return {"tasks": [{"task_id": "task_a", "state": "RUNNING", "read": "ok"}], "since": "tok"}

    code, lines = awaitchange.wait(None, awaitchange.Targets(task_ids=["task_a"]), timeout=60, hold=hold,
                                   sleep=lambda s: None, clock=lambda: 0.0, max_holds=7)
    assert code == awaitchange.EXIT_DEADLINE and len(calls) == 7
    assert "stopped after 7 follow calls" in lines[-1]


# --------------------------------------------------------------------------
# An id it cannot read
# --------------------------------------------------------------------------


def test_an_unreadable_task_is_one_line_and_the_rest_are_still_waited_on():
    world = World({"task_ok": [(0, "RUNNING", None), (200, "SUCCEEDED", None)], "task_gone": [(0, "RUNNING", None)]},
                  gone={"task_gone"})
    code, lines = _wait(world, ["task_gone", "task_ok"])
    assert code == 0
    assert [ln.split(" · ")[0] for ln in lines] == ["task_gone", "task_ok"]
    assert "cannot be read" in lines[0] and "404" in lines[0]


def test_an_unreadable_workflow_is_one_line_and_nothing_readable_exits_1():
    world = World({}, gone=set())
    code, lines = _wait(world, ["wf_missing"])
    assert code == awaitchange.EXIT_NOTHING
    (line,) = lines
    assert line.startswith("wf_missing · cannot be read: HTTP 404")


def test_only_unreadable_tasks_exit_1_with_a_line_each():
    world = World({"task_x": [(0, "RUNNING", None)]}, gone={"task_x"})
    code, lines = _wait(world, ["task_x"])
    assert code == awaitchange.EXIT_NOTHING
    assert len(lines) == 1 and lines[0].startswith("task_x · cannot be read")


# --------------------------------------------------------------------------
# One held call at a time, never faster than the follow
# --------------------------------------------------------------------------


def test_no_second_held_call_while_one_is_held_and_no_read_faster_than_the_follow(monkeypatch):
    world = World({
        "task_a": [(0, "QUEUED", None), (15, "RUNNING", None), (30, "PARKED", "PROVIDER_QUOTA_EXHAUSTED"),
                   (45, "RUNNING", None), (2000, "SUCCEEDED", None)],
        "task_done": [(0, "SUCCEEDED", None)],
    })
    held = {"now": 0, "most": 0, "calls": 0}
    starts: list[float] = []
    real = compact.watch_progress

    def counting(*args, **kwargs):
        starts.append(world.now)
        held["now"] += 1
        held["calls"] += 1
        held["most"] = max(held["most"], held["now"])
        try:
            return real(*args, **kwargs)
        finally:
            held["now"] -= 1

    code, lines = _wait(world, ["task_a", "task_done"], timeout=3600, hold=counting)
    assert code == 0 and lines[0].startswith("task_a · ")
    assert held["most"] == 1
    assert held["calls"] >= 4, "the not-news changes should each have ended a hold"
    # One follow call re-reads a task that changed within its own poll, as
    # the rows' follow does; what this command adds is the next call, and
    # never sooner than the follow's own interval after the last began.
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert gaps and min(gaps) >= compact.POLL_SECONDS, gaps


# --------------------------------------------------------------------------
# The line
# --------------------------------------------------------------------------


def test_a_refused_merge_names_its_code_and_failing_checks_and_no_free_text():
    leaked = "ghp_" + secrets.token_hex(18)
    world = World(
        {"task_m": [(0, "RUNNING", None), (100, "FAILED", None)],
         "task_f": [(0, "RUNNING", None), (100, "FAILED", None)]},
        extra={"task_m": {"last_error": "checks_failed: at 0123abcd: lint, unit tests; CI fix round 1 was not run"},
               "task_f": {"last_error": f"the runner said {leaked}"}},
    )
    code, lines = _wait(world, ["task_m", "task_f"])
    assert code == 0
    merged = "\n".join(lines)
    assert "refused checks_failed" in merged and "failing check: lint, unit tests" in merged
    assert leaked not in merged and "runner said" not in merged


# --------------------------------------------------------------------------
# The reply field
# --------------------------------------------------------------------------


def test_the_command_names_ids_and_nothing_else():
    assert awaitchange.await_command(["task_a", "task_b"]) == terminal_command("sc await task_a task_b")
    assert awaitchange.await_command(mine=True) == terminal_command("sc await --mine")
    assert awaitchange.await_command([]) is None


def test_a_workflow_submission_reply_carries_the_one_field(monkeypatch):
    from swarm_mcp import launch

    monkeypatch.setattr(launch, "record", lambda *a, **k: None)
    reply = json.loads(server._workflow_created(
        {"workflow": {"workflow_id": "wf_new", "steps": [{"step_id": "a", "task_id": "task_1"}]}},
        placed={}, repository={"url": "https://example.invalid/r"}))
    assert reply[awaitchange.FIELD] == terminal_command("sc await wf_new")
    assert [k for k in reply if "await" in k] == [awaitchange.FIELD]


def test_the_dispatch_replies_carry_the_one_field():
    with open(server.__file__, encoding="utf-8") as handle:
        source = handle.read()
    single = source[source.index('if name == "swarm_dispatch":'):]
    single = single[:single.index("\n    if name == ", 10)]
    assert "awaitchange.FIELD: awaitchange.await_command([task_id])" in single
    batch = source[source.index("def _dispatch_batch("):]
    batch = batch[:batch.index("\ndef ", 10)]
    assert "awaitchange.FIELD: awaitchange.await_command(ids)" in batch


def test_the_check_reply_names_the_mine_form_only_when_something_runs(monkeypatch):
    from swarm_mcp import check

    def run_with(running):
        monkeypatch.setattr(check, "_readers", lambda client, tenants: [client])
        monkeypatch.setattr(check, "deployment", lambda *a, **k: {"unchecked": None, "deployment": None,
                                                                  "flag": None})
        monkeypatch.setattr(check, "_check_tenant", lambda *a, **k: {
            "tenant_id": "eng", "flagged": [], "awaiting_input": [], "owner_questions": [],
            "running": {"workflows": running, "single_tasks": []},
            "out_of_scope": {"workflows": 0, "single_tasks": 0},
            "other_sessions": {g: {"other_session": 0, "untagged": 0} for g in ("workflows", "single_tasks")},
            "unchecked": []})
        monkeypatch.setattr(check, "plugin_files_flag", lambda: None)
        return check.run(object(), repository="all", names=lambda _c: {})

    assert run_with([{"workflow_id": "wf_r"}])[awaitchange.FIELD] == terminal_command("sc await --mine")
    assert run_with([])[awaitchange.FIELD] is None
