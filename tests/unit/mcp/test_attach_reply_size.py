"""An attach, attach-all, dispatch and swarm_check reply names each launch
argument once, and step details go to the titled copy (improvement plan
2026-11-23, item 2).

Measured 2026-11-23: an attach of one 49-step workflow returned every step's
details line twice -- under the reply's top-level copy of the first launch
and again under `launches` -- and a dispatch reply repeated its launch
arguments the same way; each scheduled check cost about 24,000 tokens. Now
`launches` carries each launch's `args` once, the step rows' details lines
are written into the titled copy the run is launched from (run.js's
BRIDGE_STEP_DETAILS line), and a caller that wants them in the reply asks
with `details: true`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_mcp import launch, server, workflows

from test_plugin_agents_and_workflows import _PLUGIN, _step
from test_sc_desc_details import _STATUS, ASSIGNED, SESSION, _Api, _envelope, _task

#: The bytes a 49-step attach reply may take, as `swarm_workflow_launch`
#: serves it (indent 2). Measured 2026-11-23 on this fixture: 29,562 before
#: (every details line twice), 2,313 after.
MAX_49_STEP_ATTACH_BYTES = 4_000


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(launch.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS", "SWARM_SC_SESSION_ID",
                 "SWARM_PLUGIN_ROWS", "CLAUDE_PLUGIN_OPTION_ROWS"):
        monkeypatch.delenv(name, raising=False)
    launch.forget_attached()


def _forty_nine(monkeypatch) -> list[dict]:
    tasks = [_task(f"step-{i:02d}", account=ASSIGNED) for i in range(49)]
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: _envelope(tasks))
    return tasks


def _reply(args: dict) -> str:
    return server._call(_Api(), "swarm_workflow_launch", args)


def test_a_49_step_attach_reply_stays_under_its_byte_budget(monkeypatch):
    _forty_nine(monkeypatch)
    text = _reply({"attach": "wf_a"})
    assert len(text.encode()) < MAX_49_STEP_ATTACH_BYTES, len(text.encode())
    reply = json.loads(text)
    assert "step-00" not in json.dumps(reply["launches"][0]["args"]), "details go to the copy"


def test_an_attach_reply_carries_each_launchs_args_once(monkeypatch):
    _forty_nine(monkeypatch)
    reply = json.loads(_reply({"attach": "wf_a"}))
    assert "args" not in reply, "no top-level copy of the first launch's args"
    (one,) = reply["launches"]
    # Every key the Workflow call needs is still there.
    for key in ("script_path", "args", "title", "rows", "workspace"):
        assert key in one, key
    assert one["args"]["attach"] == "wf_a"
    assert one["args"]["titled_copy"] == one["script_path"]
    assert reply["script_path"] == one["script_path"]


def test_the_titled_copy_holds_every_step_rows_details(monkeypatch):
    _forty_nine(monkeypatch)
    reply = json.loads(_reply({"attach": "wf_a"}))
    one = reply["launches"][0]
    copy = Path(one["script_path"]).read_text(encoding="utf-8").split("\n")
    (line,) = [entry for entry in copy if entry.startswith("const BRIDGE_STEP_DETAILS = ")]
    embedded = json.loads(line[len("const BRIDGE_STEP_DETAILS = "):])
    assert sorted(embedded) == [f"step-{i:02d}" for i in range(49)]
    assert "Started by: dev@example.com" in embedded["step-07"]
    assert one["step_details_in_copy"] == 49


def test_details_true_puts_them_in_the_args_and_not_in_the_copy(monkeypatch):
    _forty_nine(monkeypatch)
    reply = json.loads(_reply({"attach": "wf_a", "details": True}))
    one = reply["launches"][0]
    assert sorted(one["args"]["details"]["steps"]) == [f"step-{i:02d}" for i in range(49)]
    assert launch.STEP_DETAILS_LINE in Path(one["script_path"]).read_text(encoding="utf-8").split("\n")


def test_an_attach_all_reply_names_each_launchs_args_once(monkeypatch):
    from swarm_mcp import sc

    _forty_nine(monkeypatch)
    monkeypatch.setattr(sc, "running_workflows", lambda client, **_: {"count": 1, "complete": True, "workflows": [
        {"workflow_id": "wf_a", "workspace": "eng", "title": "flow", "label": None, "steps_total": 49,
         "claude_session": SESSION}]})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    text = _reply({"attach": "all"})
    reply = json.loads(text)
    (one,) = reply["launches"]
    assert "details" not in one["args"]
    assert len(text.encode()) < MAX_49_STEP_ATTACH_BYTES, len(text.encode())


def test_a_dispatch_reply_names_its_launch_arguments_once():
    out = launch.for_tasks([{"task_id": "t_x", "label": "lane-a", "workspace": "eng",
                             "task": _task("x", step_id=None)}])
    assert "args" not in out
    (one,) = out["launches"]
    assert one["args"]["attach_tasks"][0]["task_id"] == "t_x"
    assert json.dumps(out).count('"attach_tasks"') == 1


def test_a_rowless_task_names_its_launch_arguments_once():
    out = launch.for_tasks([{"task_id": "t_y", "label": "lane-b", "workspace": "eng",
                             "task": _task("y", step_id=None)}])
    launch.remember_row_launch("t_y", out["launches"][0])
    (entry,) = launch.rowless([{"task_id": "t_y", "state": "RUNNING", "label": "lane-b"}])
    assert entry["launch"]["args"]["attach_tasks"][0]["task_id"] == "t_y"
    assert json.dumps(entry).count('"attach_tasks"') == 1
    assert "launch.args" in entry["line"]


def test_a_step_row_launched_from_the_copy_ends_with_the_embedded_details(monkeypatch, tmp_path):
    tasks = [_task("implement", account=ASSIGNED)]
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: _envelope(tasks))
    one = json.loads(_reply({"attach": "wf_a", "rows": "per-step"}))["launches"][0]
    copy = Path(one["script_path"]).read_text(encoding="utf-8")
    embedded = json.loads([entry for entry in copy.split("\n")
                           if entry.startswith("const BRIDGE_STEP_DETAILS = ")][0].split(" = ", 1)[1])
    attached = {
        "workflow_id": "wf_a", "workspace": "u-owner", "title": "gate", "label": None, "console": None,
        "state": "RUNNING", "state_note": None, "bridge_version": "0.1.0", "follow_error": None, "error": None,
        "steps": [{"step_id": "implement", "task_id": "t1", "depends_on": [], "state": "RUNNING", "console": None}],
    }
    got = _run_copy(tmp_path, Path(one["script_path"]), one["args"], {
        "ATTACH\nworkflow_id: wf_a": attached, "step:implement": _step("SUCCEEDED"), "STATUS": _STATUS,
    })
    (row,) = [c for c in got["calls"] if c["agentType"] == "sc:step"]
    assert row["prompt"].split("\n")[-1] == "details: " + embedded["implement"]


def _run_copy(tmp_path, script: Path, args, answers) -> dict:
    """`test_sc_desc_details._run`, over the titled copy instead of run.js."""
    import subprocess

    from test_auto_attach import _BY_PROMPT
    from test_plugin_agents_and_workflows import _node

    harness = tmp_path / "harness.cjs"
    harness.write_text(_BY_PROMPT)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": args, "answers": answers}))
    done = subprocess.run([_node(), str(harness), str(script), str(fixture)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert "error" not in got, got.get("error")
    return got
