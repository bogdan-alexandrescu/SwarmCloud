"""A finished live-browser task that never used its browser gets ONE line (2026-11-05, part of #2688).

WHY. Over the 24 hours to 10:00Z on 5 November, 76 of 642 claude-code-browser
tasks made no browser call and produced no image -- about 46 hours of browser
runtime -- mostly code changes whose only browser need was a headless
Playwright spec, which plain claude-code runs. So wherever a finished task's
result reaches a session -- `swarm_result`, `swarm_follow`'s outcome and its
progress and summary rows, `swarm_status`, a workflow's result -- a task on a
live-browser profile whose `browser_used` is false says so, and how long it
ran. True adds nothing; null adds nothing, because it is no measurement; a
non-browser profile never does. Advice only: no dispatch is refused or changed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_mcp import compact, progress, server, workflows

from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures

BROWSER = "claude-code-browser"

#: The advice after the task's name and seconds, exactly as the session reads it.
ADVICE = (
    "claude-code runs Playwright suites and Postgres too; pick it unless the agent must look at a page"
)


def _line(who: str, seconds: str) -> str:
    return f"{who} ran on {BROWSER} and never used its browser ({seconds} s): {ADVICE}"


def _use(seconds: float | None = 700.0) -> dict[str, Any]:
    return {"tool_calls": 0, "mcp_calls": 0, "command_calls": 0, "images": 0,
            "complete": True, "seconds": seconds}


def _finish(world: World, task_id: str, *, profile: str = BROWSER, used: bool | None = False,
            state: str = "SUCCEEDED") -> None:
    world.task(task_id, state=state)
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["runner_profile"] = profile
    doc["started_at"] = NOW
    doc["completed_at"] = NOW.replace(minute=4)
    doc["result_summary"] = {"runner": {"summary": "done"}, "artifacts": [], "logs": {},
                             "browser_used": used, "browser_use": _use()}


# --------------------------------------------------------------------------
# The line itself
# --------------------------------------------------------------------------


def test_false_on_a_finished_browser_task_is_the_one_line():
    task = {"id": "task_x", "runner_profile": BROWSER, "state": "SUCCEEDED",
            "browser_used": False, "browser_use": _use(712.4)}
    # MUTATION: return None for false and this is None.
    assert progress.browser_unused_line(task) == _line("task_x", "712")
    assert progress.browser_unused_line(task, label="step build (task_x)") == _line("step build (task_x)", "712")


@pytest.mark.parametrize("used", [True, None])
def test_true_or_null_adds_nothing(used):
    task = {"id": "task_x", "runner_profile": BROWSER, "state": "SUCCEEDED",
            "browser_used": used, "browser_use": _use()}
    # MUTATION: treat null as false and an unmeasured run is called unused.
    assert progress.browser_unused_line(task) is None


def test_a_non_browser_profile_never_gets_it():
    task = {"id": "task_x", "runner_profile": "claude-code", "state": "SUCCEEDED",
            "browser_used": False, "browser_use": _use()}
    assert progress.browser_unused_line(task) is None


def test_an_unfinished_task_does_not_get_it():
    task = {"id": "task_x", "runner_profile": BROWSER, "state": "RUNNING", "browser_used": False}
    assert progress.browser_unused_line(task) is None


def test_the_seconds_fall_back_to_the_runner_duration_and_never_invent_one():
    task = {"id": "task_x", "runner_profile": BROWSER, "state": "FAILED", "browser_used": False,
            "browser_use": _use(None), "result_summary": {"duration_seconds": 95.6}}
    assert progress.browser_unused_line(task) == _line("task_x", "96")
    assert progress.browser_unused_line({**task, "result_summary": {}}) == _line("task_x", "?")


# --------------------------------------------------------------------------
# Every place a finished task's result is reported
# --------------------------------------------------------------------------


def _result(swarm, task_id: str) -> dict[str, Any]:
    return json.loads(server._call(swarm, "swarm_result", {"task_id": task_id}))


def test_swarm_result_carries_the_line(swarm, world):
    _finish(world, "task_n")
    assert _result(swarm, "task_n")["browser_unused"] == _line("task_n", "700")


@pytest.mark.parametrize(("profile", "used"), [(BROWSER, True), (BROWSER, None), ("claude-code", False)])
def test_swarm_result_carries_no_line_otherwise(swarm, world, profile, used):
    _finish(world, "task_c", profile=profile, used=used)
    assert "browser_unused" not in _result(swarm, "task_c")


def test_swarm_status_carries_the_line_for_a_finished_task(swarm, world):
    _finish(world, "task_n")
    _finish(world, "task_u", used=True)
    rows = {r["task_id"]: r for r in json.loads(server._call(swarm, "swarm_status",
                                                              {"task_ids": ["task_n", "task_u"]}))}
    assert rows["task_n"]["browser_unused"] == _line("task_n", "700")
    assert "browser_unused" not in rows["task_u"]


def test_swarm_follow_puts_the_line_in_a_finished_tasks_result(swarm, world):
    _finish(world, "task_n")
    reply = json.loads(server._call(swarm, "swarm_follow", {"task_ids": ["task_n"]}))
    (task,) = reply["tasks"]
    assert task["result"]["browser_unused"] == _line("task_n", "700")


def test_the_follow_outcome_carries_it(swarm, world):
    _finish(world, "task_n")
    assert progress.outcome(swarm, swarm.task("task_n"))["browser_unused"] == _line("task_n", "700")
    _finish(world, "task_u", used=None)
    assert "browser_unused" not in progress.outcome(swarm, swarm.task("task_u"))


def test_a_progress_row_writes_it_under_the_finish_line(swarm, world):
    _finish(world, "task_n")
    got = compact.watch_progress(swarm, ["task_n"])
    assert got["tasks"][0]["outcome"]["browser_unused"] == _line("task_n", "700")
    # MUTATION: leave it out of the row's lines and the session never sees it.
    assert [p for p in got["progress"] if "never used its browser" in p] == [f"[task_n] {_line('task_n', '700')}"]
    # Once: the next call, whose finish line is not news, does not repeat it.
    again = compact.watch_progress(swarm, ["task_n"], since=got["since"])
    assert not [p for p in again["progress"] if "never used its browser" in p]


def test_a_progress_row_of_a_used_browser_writes_nothing(swarm, world):
    _finish(world, "task_u", used=True)
    got = compact.watch_progress(swarm, ["task_u"])
    assert not [p for p in got["progress"] if "never used its browser" in p]
    assert "browser_unused" not in got["tasks"][0]["outcome"]


def test_a_summary_row_names_it_on_the_call_the_task_finished(swarm, world):
    _finish(world, "task_n")
    _finish(world, "task_u", used=True)
    summary_of = {"noun": "step", "total": 2, "counted": {"running": 0, "queued": 0, "done": 0, "failed": 0},
                  "prs": [], "console": "", "line": ""}
    got = compact.watch_summary(swarm, ["task_n", "task_u"], summary_of)
    assert got["browser_unused"] == [_line("task_n", "700")]
    later = compact.watch_summary(swarm, ["task_n", "task_u"], summary_of, since=got["since"])
    assert "browser_unused" not in later


# --------------------------------------------------------------------------
# A workflow's result names the steps that left it unused, once each
# --------------------------------------------------------------------------


def _task(task_id: str, used: bool | None, profile: str = BROWSER) -> dict[str, Any]:
    return {"id": task_id, "state": "SUCCEEDED", "runner_profile": profile,
            "browser_used": used, "browser_use": _use(300.0)}


def _envelope() -> dict[str, Any]:
    def step(step_id: str, task_id: str | None, profile: str = BROWSER) -> dict[str, Any]:
        return {"step_id": step_id, "task_id": task_id, "runner_profile": profile,
                "depends_on": [], "input_from": {}, "links": {"console": None}}

    return {
        "workflow": {
            "workflow_id": "wf_x", "state": "SUCCEEDED", "state_source": "derived",
            "cancel_requested": False, "on_step_failure": "fail_workflow", "links": {"console": None},
            "steps": [step("build", "task_a"), step("qa", "task_b"), step("code", "task_c", "claude-code"),
                      step("unknown", "task_d"), step("later", None)],
        },
        "tasks": [_task("task_a", False), _task("task_b", True), _task("task_c", False, "claude-code"),
                  _task("task_d", None)],
    }


def test_a_workflow_names_each_unused_step_once():
    assert workflows.browser_unused_steps(_envelope()) == [_line("step build (task_a)", "300")]


class _WorkflowApi:
    def __init__(self) -> None:
        self.envelope = _envelope()

    def request(self, method, path, *, payload=None, timeout=60):  # noqa: ARG002
        return self.envelope


def test_swarm_workflow_result_carries_the_steps():
    body = json.loads(server._call(_WorkflowApi(), "swarm_workflow_result", {"workflow_id": "wf_x"}))
    assert body["browser_unused"] == [_line("step build (task_a)", "300")]


def test_a_workflow_with_no_unused_step_carries_no_key():
    api = _WorkflowApi()
    api.envelope["tasks"][0]["browser_used"] = True
    body = json.loads(server._call(api, "swarm_workflow_result", {"workflow_id": "wf_x"}))
    assert "browser_unused" not in body
