"""Three sc plugin faults the Product Oracle session reported (2026-11-06).

1. A TASK ROW ENDED AT ONCE ON A TASK STILL DISPATCHED. Run wa0iq1ptb, the
   sc:task row for task_4447f83c08cc45959dc7, returned after about 7 seconds
   with the run's state FOLLOWED while the task was DISPATCHED, right after a
   swarm_dispatch with ref refs/pull/1309/head and strategy collect. run.js
   took the row's first answer, whatever its state. A task row now holds
   until its task finishes (SUCCEEDED, FAILED, CANCELLED, DEAD_LETTERED) or
   parks for a person: any other answer starts the row again.
2. A DISPATCH'S ROWS SAID `summary` IN A PER-STEP PROJECT. swarm_dispatch had
   no `rows` argument: its rows follow the same setting an attach does -- the
   plugin option, the project's saved layout, and the call's own `rows`
   outranking both.
3. A DISPATCH WHOSE ROWS LAUNCH NEVER STARTED had a running task with no row
   that nothing caught: `new_only` leaves out a task whose launch was written.
   swarm_check now names each running task this session dispatched that no
   row has followed, with the launch that starts its row.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest

from swarm_mcp import check, launch, layout, progress, server

# The real application and SwarmClient's real transport into it.
from test_against_the_real_api import api, db, swarm  # noqa: F401
from test_plugin_agents_and_workflows import _PLUGIN, _RUN_JS, _node, _step
from test_summary_row_step_lines import _SEQUENCE

_REF = "refs/pull/1309/head"


@pytest.fixture(autouse=True)
def _bridge(monkeypatch, tmp_path):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("SWARM_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "project").mkdir(exist_ok=True)
    for name in ("SWARM_PLUGIN_ROWS", "SWARM_PLUGIN_LAYOUT"):
        monkeypatch.delenv(name, raising=False)
    launch.forget_attached()
    progress.HANDLES.clear()
    yield
    launch.forget_attached()


def _dispatch(swarm, **extra) -> dict:  # noqa: F811
    args = {"prompt": "re-review the pull request", "profile": "mock", "label": "oracle-rereview", **extra}
    return json.loads(server._call(swarm, "swarm_dispatch", args))


def _run(tmp_path, args: dict, answers: dict) -> dict:
    """run.js under the harness that hands out a list of answers in turn."""
    harness = tmp_path / "harness.cjs"
    harness.write_text(_SEQUENCE)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": args, "answers": answers}))
    done = subprocess.run([_node(), str(harness), str(_RUN_JS), str(fixture)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert "error" not in got, got.get("error")
    return got


# --------------------------------------------------------------------------
# 1. A task row holds until its task ends
# --------------------------------------------------------------------------


def test_the_oracle_sequence_a_row_that_answers_while_dispatched_is_started_again(tmp_path, swarm, api, db):  # noqa: F811
    """Replays wa0iq1ptb: swarm_dispatch with a pull request's ref and
    `collect`, its reply's rows launched, the row answering at once with the
    task still DISPATCHED. The run starts the row again; it ends on the
    task's real end."""
    payload = _dispatch(swarm, ref=_REF, strategy="collect")
    task_id = payload["task_id"]
    assert db.docs[f"tasks/{task_id}"]["state"] not in ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")
    rows = payload["rows"]
    early = _step("DISPATCHED", last_error="")
    got = _run(tmp_path, rows["args"], {f"task_id: {task_id}": [early, _step("SUCCEEDED", cost_usd=0.4)]})
    calls = [c for c in got["calls"] if c["agentType"] == "sc:task"]
    # MUTATION: run.js returns the row's first answer (the defect): one call, DISPATCHED.
    assert len(calls) == 2, "the row that answered while DISPATCHED was not started again"
    assert calls[0]["label"] == calls[1]["label"], "the row comes back under the same title"
    (row,) = got["result"]["tasks"]
    assert (row["task_id"], row["state"]) == (task_id, "SUCCEEDED")
    assert any(f"{task_id} · its row answered DISPATCHED before the task ended" in line for line in got["logs"])


@pytest.mark.parametrize("early", [
    _step("running", last_error="resume with: swarm follow task_a"),
    _step("QUEUED"),
    _step("PARKED"),  # parked, but not for a person: the bridge never stops a task row on it
    {"answer_excerpt": None},  # no state at all
])
def test_every_answer_short_of_the_tasks_end_starts_the_row_again(tmp_path, early):
    got = _run(tmp_path, {"attach_tasks": [{"task_id": "task_a", "title": "lane-a"}]},
               {"task_id: task_a": [early, _step("FAILED", last_error="boom")]})
    assert [c["agentType"] for c in got["calls"]] == ["sc:task", "sc:task"]
    (row,) = got["result"]["tasks"]
    assert row["state"] == "FAILED"


@pytest.mark.parametrize("final", [
    _step("SUCCEEDED"), _step("FAILED"), _step("CANCELLED"), _step("DEAD_LETTERED"),
    _step("UNKNOWN", last_error="three reads in a row failed"),
    _step("PARKED", waiting_on_person={"kind": "owner_questions", "since": "2026-11-06T03:05:00Z",
                                        "questions": [], "console": "https://c.test/agents/task_a"}),
])
def test_a_row_ends_on_its_tasks_end_or_a_wait_for_a_person(tmp_path, final):
    got = _run(tmp_path, {"attach_tasks": [{"task_id": "task_a", "title": "lane-a"}]},
               {"task_id: task_a": [final]})
    assert [c["agentType"] for c in got["calls"]] == ["sc:task"], "a final answer starts no second row"
    (row,) = got["result"]["tasks"]
    assert row["state"] == final["state"]


def test_a_row_that_keeps_answering_early_ends_saying_so_with_the_resume(tmp_path):
    got = _run(tmp_path, {"attach_tasks": [{"task_id": "task_a", "title": "lane-a"}]},
               {"task_id: task_a": [_step("DISPATCHED")]})
    assert len(got["calls"]) == 6, "five restarts, then the row's answer stands"
    (row,) = got["result"]["tasks"]
    assert row["state"] == "DISPATCHED"
    assert "before the task ended, 6 times" in row["last_error"]
    assert row["last_error"].endswith("resume: /sc attach --all")


def test_the_task_row_is_told_to_answer_only_when_the_bridge_stops():
    text = (_PLUGIN / "agents" / "task.md").read_text(encoding="utf-8")
    assert "A first reply, a DISPATCHED, QUEUED or RUNNING task, or a quiet reply is never a reason to answer" in text


# --------------------------------------------------------------------------
# 2. A dispatch's rows follow the setting an attach does
# --------------------------------------------------------------------------


def test_a_dispatch_honours_the_plugins_rows_option(swarm, api, db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SWARM_PLUGIN_ROWS", "per-step")
    rows = _dispatch(swarm)["rows"]
    assert rows["args"]["rows"] == "per-step"
    assert rows["rows"] == "per-step"


def test_a_dispatch_honours_the_projects_saved_layout(swarm, api, db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SWARM_PLUGIN_ROWS", "per-step")
    layout.save("pane")
    assert _dispatch(swarm)["rows"]["args"]["rows"] == "pane"


def test_a_dispatchs_own_rows_outranks_the_option_and_the_saved_layout(swarm, api, db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SWARM_PLUGIN_ROWS", "summary")
    # MUTATION: `rows` not read by swarm_dispatch (the defect): "summary".
    assert _dispatch(swarm, rows="per-step")["rows"]["args"]["rows"] == "per-step"
    layout.save("pane")
    assert _dispatch(swarm, rows="summary", prompt="another lane")["rows"]["args"]["rows"] == "summary"
    batch = json.loads(server._call(swarm, "swarm_dispatch", {"rows": "per-step", "tasks": [
        {"prompt": "one", "runner_profile": "mock", "label": "lane-1"},
        {"prompt": "two", "runner_profile": "mock", "label": "lane-2"},
    ]}))
    assert [copy["args"]["rows"] for copy in batch["rows"]["launches"]] == ["per-step", "per-step"]


def test_a_dispatch_with_an_unknown_rows_sends_nothing(swarm, api, db):  # noqa: F811
    before = len([k for k in db.docs if k.startswith("tasks/")])
    with pytest.raises(Exception, match="`rows` is one of"):
        _dispatch(swarm, rows="lanes")
    assert len([k for k in db.docs if k.startswith("tasks/")]) == before, "a task was sent"
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_dispatch"]
    assert tool["inputSchema"]["properties"]["rows"]["enum"] == ["summary", "per-step", "pane"]


# --------------------------------------------------------------------------
# 3. swarm_check names a dispatched task with no row
# --------------------------------------------------------------------------


def test_check_names_a_dispatched_task_whose_row_never_started(swarm, api, db, claude_session):  # noqa: F811
    payload = _dispatch(swarm)
    task_id = payload["task_id"]
    result = check.run(swarm, repository="all", names=lambda _c: {})
    # MUTATION: no rowless line (the defect): KeyError.
    (entry,) = result["rowless_tasks"]
    assert entry["task_id"] == task_id
    assert entry["launch"] == {key: payload["rows"][key] for key in ("title", "script_path", "args")}
    assert entry["line"].startswith(f"{task_id} (oracle-rereview) is ")
    assert "with no row" in entry["line"]
    assert json.dumps({"scriptPath": payload["rows"]["script_path"], "args": payload["rows"]["args"]},
                      separators=(",", ":")) in entry["line"]
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_check"]
    assert "`rowless_tasks`" in tool["description"]


def test_check_says_nothing_once_a_row_follows_the_task(swarm, api, db, claude_session):  # noqa: F811
    task_id = _dispatch(swarm)["task_id"]
    server._call(swarm, "swarm_follow", {"task_ids": [task_id], "format": "progress", "wait_seconds": 0})
    result = check.run(swarm, repository="all", names=lambda _c: {})
    assert "rowless_tasks" not in result, "a task its row follows is named rowless"


def test_check_never_names_a_finished_task_or_one_it_did_not_dispatch(swarm, api, db, claude_session):  # noqa: F811
    done = _dispatch(swarm)["task_id"]
    db.docs[f"tasks/{done}"]["state"] = "SUCCEEDED"
    launch.for_tasks([{"task_id": "task_attached", "label": "x"}], dispatched=False)
    assert launch.rowless([{"task_id": "task_attached", "state": "RUNNING"}]) == []
    assert "rowless_tasks" not in check.run(swarm, repository="all", names=lambda _c: {})
