"""Thirty live rows, and the workspace on every one of them (SC-ROWS).

Owner request, 2026-10-14: "increase live rows to 30 but allow scrolling and
also include the workspace where the workflow or task is running in the info
that is easily displayed to the user via the sc plugin output on each
workflow or task line in the details". Held here:

* THE CAP. `/sc attach --all` follows up to 30 workflows and 30 single tasks
  (10 each before), the plugin's `max_live_rows` option moves it within 1..50,
  and everything past it stays in `not_followed`. The bridge reads the option
  (`config.max_live_rows`) and hands it to run.js as each copy's `max_rows`;
  run.js clamps it the same way and defaults to the same 30.
* THE WORKSPACE. run.js's Result title is `SC · [<workspace>] <name> · N
  steps` (the name is cut, never the workspace or the count, at most 150) --
  the launched copy's meta.name has no workspace since SC-WS-DESC, its
  description leads with it (test_sc_workspace_in_description) -- every
  row label is `[SwarmCloud] [<workspace>] ...`, and every result object and
  summary line carries it -- read off the API's workflow and task documents
  (`tenant_id`), never off the session's configured tenant.
* THE ORDER. Newest first, grouped by workspace, stably.
* `/sc workflows` and `/sc status` print the workspace first on each line.

Scrolling is Claude Code's: /workflows is its panel, not the plugin's. The
skill doc says so, and that each row is one compact line.

THE MUTATIONS THESE CATCH: put either cap back to 10 (the cap tests go red);
read the workspace from SWARM_TENANT instead of the document (the
several-workspace tests go red); cut the workspace to fit the title (the
truncation test goes red); drop the grouping (the order tests go red).
"""

from __future__ import annotations

import io
import json
import types

import pytest

from swarm_mcp import config, launch, sc, server, workflows

from test_auto_attach import _BY_PROMPT
from test_plugin_agents_and_workflows import _PLUGIN, _RUN_JS, _node, _step

_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"

#: The release before this lane: SC-ROWS changes the bridge the plugin pins.
_PREVIOUS_RELEASED_VERSION = (0, 5, 33)


@pytest.fixture
def _plugin_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setattr(launch.tempfile, "tempdir", str(tmp_path))
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS"):
        monkeypatch.delenv(name, raising=False)


def _run(tmp_path, args, answers) -> dict:
    import subprocess

    harness = tmp_path / "harness.cjs"
    harness.write_text(_BY_PROMPT)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": args, "answers": answers}))
    done = subprocess.run(
        [_node(), str(harness), str(_RUN_JS), str(fixture)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert "error" not in got, got.get("error")
    return got


# --------------------------------------------------------------------------
# The option and its clamp
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("given", "cap"), [
    (None, 30), ("", 30), ("${user_config.max_live_rows}", 30), ("lots", 30), ("nan", 30),
    ("12", 12), ("30", 30), ("50", 50), ("51", 50), ("1000", 50), ("1", 1), ("0", 1), ("-4", 1),
    ("7.9", 7),
])
def test_the_bridge_reads_max_live_rows_and_clamps_it_to_1_through_50(given, cap):
    environ = {} if given is None else {"SWARM_PLUGIN_MAX_LIVE_ROWS": given}
    assert config.max_live_rows(environ) == cap


def test_the_hook_reads_the_same_option_under_its_hook_name():
    assert config.max_live_rows({"CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS": "8"}) == 8
    assert config.max_live_rows({"SWARM_PLUGIN_MAX_LIVE_ROWS": "9",
                                 "CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS": "8"}) == 9


def test_the_manifest_declares_max_live_rows_defaulting_to_30_within_1_to_50():
    manifest = json.loads(_MANIFEST.read_text())
    option = manifest["userConfig"]["max_live_rows"]
    assert option["type"] == "number"
    assert option["default"] == config.DEFAULT_MAX_LIVE_ROWS == 30
    assert (option["min"], option["max"]) == (1, config.MAX_LIVE_ROWS_LIMIT) == (1, 50)
    env = manifest["mcpServers"]["swarmcloud"]["env"]
    assert env["SWARM_PLUGIN_MAX_LIVE_ROWS"] == "${user_config.max_live_rows}"


def test_run_js_restates_the_bridges_default_and_limit():
    text = _RUN_JS.read_text()
    assert f"const MAX_ATTACHED_WORKFLOWS = {launch.MAX_ATTACHED_WORKFLOWS}" in text
    assert f"const MAX_ATTACHED_TASKS = {launch.MAX_ATTACHED_TASKS}" in text
    assert f"const MAX_LIVE_ROWS_LIMIT = {config.MAX_LIVE_ROWS_LIMIT}" in text
    assert launch.MAX_ATTACHED_WORKFLOWS == launch.MAX_ATTACHED_TASKS == 30


def test_the_plugin_version_moved_past_the_release_before_sc_rows():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version > _PREVIOUS_RELEASED_VERSION, manifest["version"]
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#subdirectory=apps/swarm-mcp" in arg for arg in pinned), pinned


# --------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------


def test_a_title_starts_with_the_workspace_after_the_prefix():
    assert workflows.workflow_title("DEVB-GATE: ship it", 5, "u-bogdan") == "SC · [u-bogdan] DEVB-GATE: ship it · 5 steps"
    assert workflows.workflow_title("ship it", 1) == "SC · ship it · 1 step", "no workspace known, as before"


def test_a_long_title_cuts_the_name_never_the_workspace_or_the_step_count():
    name = "word " * 60
    title = workflows.workflow_title(name, 12, "u-bogdan")
    assert len(title) <= workflows.TITLE_CHARS
    assert title.startswith("SC · [u-bogdan] word")
    assert title.endswith("… · 12 steps")


def test_the_run_js_title_is_the_bridges_title(tmp_path):
    """run.js's Result title is workflow_title's, workspace and cut included
    (the launched copy's meta.name drops the workspace: SC-WS-DESC)."""
    name = "a very long workflow name " * 8
    got = _run(tmp_path, {"attach": "wf_a", "title": name, "workspace": "u-bogdan"}, {
        "ATTACH\nworkflow_id: wf_a": {
            "workflow_id": "wf_a", "workspace": "u-bogdan", "title": name, "label": None, "console": None,
            "state": "RUNNING", "state_note": None, "bridge_version": "0.1.0", "follow_error": None, "error": None,
            "steps": [{"step_id": "implement", "task_id": "t1", "depends_on": [], "state": "RUNNING", "console": None}],
        },
        "step:implement": _step("SUCCEEDED"),
        "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []},
    })
    result = got["result"]
    assert result["title"] == workflows.workflow_title(workflows.title_name(stored=name), 1, "u-bogdan")
    assert result["workspace"] == "u-bogdan"


# --------------------------------------------------------------------------
# run.js rows: label, cap, order
# --------------------------------------------------------------------------


def test_every_step_row_and_setup_row_is_labelled_with_the_workspace(tmp_path):
    link = "https://c.test/agents/t1"
    got = _run(tmp_path, {"attach": "wf_a", "title": "gate"}, {
        "ATTACH\nworkflow_id: wf_a": {
            "workflow_id": "wf_a", "workspace": "u-bogdan", "title": "gate", "label": None, "console": None,
            "state": "RUNNING", "state_note": None, "bridge_version": "0.1.0", "follow_error": None, "error": None,
            "steps": [{"step_id": "implement", "task_id": "t1", "depends_on": [], "state": "RUNNING", "console": link}],
        },
        "step:implement": _step("SUCCEEDED"),
        "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []},
    })
    labels = [c["label"] for c in got["calls"]]
    assert labels[0] == "[SwarmCloud] gate · setup · attach", "the launch gave no workspace; the read had not answered"
    assert labels[1] == f"[SwarmCloud] [u-bogdan] gate · stage 1 · implement · {link}"
    assert labels[2] == "[SwarmCloud] [u-bogdan] gate · 1 step · wf_a · setup · result"
    assert any(line.startswith("[u-bogdan] implement SUCCEEDED") for line in got["logs"]), got["logs"]
    assert any(line.startswith("[u-bogdan] wf_a SUCCEEDED") for line in got["logs"]), got["logs"]


def test_the_attach_row_carries_the_workspace_the_launch_read(tmp_path):
    got = _run(tmp_path, {"attach": "wf_a", "title": "gate", "workspace": "eng"}, {
        "ATTACH\nworkflow_id: wf_a": {"__throw": "boom"},
    })
    assert got["calls"][0]["label"] == "[SwarmCloud] [eng] gate · setup · attach"
    assert got["result"]["workspace"] == "eng" and got["result"]["state"] == "NOT_ATTACHED"


def test_a_submitted_workflows_rows_carry_the_workspace_the_reply_names(tmp_path):
    spec = {"label": "gate", "steps": [{"step_id": "implement", "prompt": "p", "runner_profile": "claude-code"}]}
    got = _run(tmp_path, {"spec": spec}, {
        "SUBMIT": {
            "workflow_id": "wf_s", "workspace": "u-bogdan", "console": None,
            "steps": [{"step_id": "implement", "task_id": "t1", "depends_on": [], "console": None}],
            "repository": None, "repository_notes": [], "spec_digest": workflows.spec_digest(spec),
            "bridge_version": "0.1.0",
            "follow_error": None, "error": None,
        },
        "step:implement": _step("SUCCEEDED"),
        "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []},
    })
    step_rows = [c["label"] for c in got["calls"] if c["agentType"] == "sc:step"]
    assert got["result"]["state"] == "SUCCEEDED", got["result"]
    assert step_rows == ["[SwarmCloud] [u-bogdan] gate · stage 1 · implement"]
    assert got["result"]["workspace"] == "u-bogdan"
    assert got["result"]["title"] == "SC · [u-bogdan] gate · 1 step"


def _tasks(count: int, workspace: str = "eng") -> list[dict]:
    return [{"task_id": f"task_{n:02d}", "title": f"lane-{n:02d}", "workspace": workspace} for n in range(count)]


def _task_answers(entries: list[dict]) -> dict:
    return {f"task_id: {e['task_id']}": _step("SUCCEEDED") for e in entries}


@pytest.mark.parametrize(("max_rows", "followed"), [(None, 30), (5, 5), (99, 35), ("12", 12), (0, 1)])
def test_attach_tasks_follows_up_to_the_cap_it_is_handed_clamped(tmp_path, max_rows, followed):
    entries = _tasks(35)
    args = {"attach_tasks": entries}
    if max_rows is not None:
        args["max_rows"] = max_rows
    got = _run(tmp_path, args, _task_answers(entries))
    rows = [c for c in got["calls"] if c["agentType"] == "sc:task"]
    assert len(rows) == min(followed, 50)
    assert len(got["result"]["not_followed"]) == 35 - len(rows)
    assert all(n["workspace"] == "eng" for n in got["result"]["not_followed"])


def test_a_task_row_is_labelled_with_its_workspace_and_its_result_carries_it(tmp_path):
    entries = [{"task_id": "task_a", "title": "lane-a", "workspace": "u-bogdan"}]
    got = _run(tmp_path, {"attach_tasks": entries}, _task_answers(entries))
    assert got["calls"][0]["label"] == "[SwarmCloud] [u-bogdan] lane-a · task"
    assert got["result"]["tasks"][0]["workspace"] == "u-bogdan"
    assert got["logs"][-1].startswith("[u-bogdan] lane-a SUCCEEDED")


def test_task_rows_are_grouped_by_workspace_newest_first_within_each(tmp_path):
    entries = [
        {"task_id": "t1", "title": "a1", "workspace": "a"},
        {"task_id": "t2", "title": "b1", "workspace": "b"},
        {"task_id": "t3", "title": "a2", "workspace": "a"},
        {"task_id": "t4", "title": "b2", "workspace": "b"},
    ]
    got = _run(tmp_path, {"attach_tasks": entries}, _task_answers(entries))
    assert [t["task_id"] for t in got["result"]["tasks"]] == ["t1", "t3", "t2", "t4"]


def _listed_workflows(count: int, workspaces=("eng",)) -> dict:
    flows = [{"workflow_id": f"wf_{n:02d}", "workspace": workspaces[n % len(workspaces)], "title": f"flow {n:02d}",
              "label": None, "state": "RUNNING", "console": None} for n in range(count)]
    return {"count": count, "error": None, "workflows": flows, "single_tasks": [], "single_tasks_error": None}


@pytest.mark.parametrize(("max_rows", "followed"), [(None, 30), (12, 12), (70, 35)])
def test_attach_all_hands_back_up_to_the_cap_and_lists_the_rest(tmp_path, max_rows, followed):
    args = {"attach": "all"}
    if max_rows is not None:
        args["max_rows"] = max_rows
    got = _run(tmp_path, args, {"LIST": _listed_workflows(35)})
    result = got["result"]
    assert len(result["attach_calls"]) == followed
    assert len(result["not_followed"]) == 35 - followed
    assert all(call["workspace"] == "eng" for call in result["attach_calls"])
    assert all(entry["workspace"] == "eng" for entry in result["workflows"] + result["not_followed"])


def test_attach_all_groups_by_workspace_keeping_newest_first(tmp_path):
    got = _run(tmp_path, {"attach": "all"}, {"LIST": _listed_workflows(6, ("u-bogdan", "eng"))})
    calls = got["result"]["attach_calls"]
    assert [(c["workspace"], c["attach"]) for c in calls] == [
        ("u-bogdan", "wf_00"), ("u-bogdan", "wf_02"), ("u-bogdan", "wf_04"),
        ("eng", "wf_01"), ("eng", "wf_03"), ("eng", "wf_05"),
    ]
    assert any(line.startswith("[eng] wf_01 flow 01 RUNNING") for line in got["logs"]), got["logs"]


# --------------------------------------------------------------------------
# The bridge: launch(all), titles, workspace from the documents
# --------------------------------------------------------------------------


class _NoTenants:
    """A client that only refuses the display-name read: launches must not
    need it."""

    def tenants_mine(self):
        raise RuntimeError("no /v1/tenants/mine here")


def _running(count: int, workspaces=("eng",)) -> dict:
    return {"count": count, "complete": True, "workflows": [
        {"workflow_id": f"wf_{n:02d}", "workspace": workspaces[n % len(workspaces)], "title": f"flow {n:02d}",
         "label": None, "steps_total": 3, "console": None}
        for n in range(count)
    ]}


def test_launch_all_honours_the_30_cap_and_names_each_workspace(_plugin_root, monkeypatch):
    monkeypatch.setattr(sc, "running_workflows", lambda client: _running(35))
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    out = launch.for_all(_NoTenants())
    assert out["max_live_rows"] == 30
    assert len(out["launches"]) == 30 and len(out["not_followed"]) == 5
    first = out["launches"][0]
    # SC-WS-DESC: the run's name has no workspace; its description leads with it.
    assert first["title"] == "SC · flow 00 · 3 steps"
    assert first["workspace"] == "eng"
    assert first["args"] == {"attach": "wf_00", "title": "flow 00", "workspace": "eng"}
    assert out["not_followed"][0] == {"workflow_id": "wf_30", "workspace": "eng", "title": "flow 30",
                                      "attach_with": "/sc attach wf_30"}


def test_launch_all_honours_the_plugin_option(_plugin_root, monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_MAX_LIVE_ROWS", "4")
    monkeypatch.setattr(sc, "running_workflows", lambda client: _running(6))
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 7, "tasks": [
        {"task_id": f"task_{n}", "label": f"lane {n}", "workspace": "eng"} for n in range(7)]})
    out = launch.for_all(_NoTenants())
    workflow_launches = [l for l in out["launches"] if "workflow_id" in l]
    task_launch = [l for l in out["launches"] if "task_ids" in l][0]
    assert len(workflow_launches) == 4
    assert len(task_launch["task_ids"]) == 4 and task_launch["args"]["max_rows"] == 4
    assert all(r["workspace"] == "eng" for r in task_launch["args"]["attach_tasks"])
    assert task_launch["title"] == "SC · lane 0 +3 · 4 tasks"
    assert len(out["not_followed"]) == 2 + 3


def test_launch_all_groups_several_workspaces_and_uses_display_names(_plugin_root, monkeypatch):
    monkeypatch.setattr(sc, "running_workflows", lambda client: _running(4, ("u-bogdan", "eng")))
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    client = types.SimpleNamespace(tenants_mine=lambda: [
        {"tenant_id": "eng", "display_name": "Engineering"},
        {"tenant_id": "u-bogdan", "display_name": "x" * (workflows.WORKSPACE_NAME_CHARS + 1)},
    ])
    out = launch.for_all(client)
    assert [(l["workspace"], l["workflow_id"]) for l in out["launches"]] == [
        ("u-bogdan", "wf_00"), ("u-bogdan", "wf_02"), ("Engineering", "wf_01"), ("Engineering", "wf_03"),
    ], "a display name too long to fit is not used: the tenant id is"


def test_for_tasks_follows_30_and_lists_the_rest_with_their_workspace(_plugin_root):
    entries = [{"task_id": f"task_{n:02d}", "label": f"lane {n}", "workspace": "u-bogdan"} for n in range(33)]
    out = launch.for_tasks(entries)
    assert len(out["args"]["attach_tasks"]) == 30 and out["args"]["max_rows"] == 30
    assert out["args"]["attach_tasks"][0] == {"task_id": "task_00", "title": "lane 0", "workspace": "u-bogdan"}
    assert [n["workspace"] for n in out["not_followed"]] == ["u-bogdan"] * 3


def test_the_workspace_is_read_from_the_document_never_from_the_session(monkeypatch):
    """Two documents from two workspaces, read by a session configured for a
    third: each row names its own."""
    monkeypatch.setenv("SWARM_TENANT", "configured-elsewhere")
    assert workflows.workspace_of({"tenant_id": "eng"}) == "eng"
    assert workflows.workspace_of({"tenant_id": "u-bogdan"}) == "u-bogdan"
    assert workflows.workspace_of({}, fallback="eng") == "eng", "the listing page's tenant, the API's answer"
    assert workflows.workspace_of({}) is None


def test_running_workflows_and_tasks_carry_the_workspace_of_each_document():
    class _Pages:
        def workflows(self, **_):
            return {"tenant_id": "eng", "workflows": [
                {"workflow_id": "wf_1", "tenant_id": "u-bogdan", "state": "RUNNING", "steps": []},
                {"workflow_id": "wf_2", "state": "RUNNING", "steps": []},
            ]}

    listing = sc.running_workflows(_Pages(), detail_reads=0)
    assert [(e["workflow_id"], e["workspace"]) for e in listing["workflows"]] == [("wf_1", "u-bogdan"), ("wf_2", "eng")]


def test_a_workflow_report_and_a_created_reply_name_the_workspace():
    envelope = {"workflow": {"workflow_id": "wf_1", "tenant_id": "u-bogdan", "steps": [], "state": "RUNNING"},
                "tasks": []}
    assert workflows.report(envelope)["workspace"] == "u-bogdan"
    created = json.loads(server._workflow_created(envelope, placed=None, repository=None, digest=None,
                                                  expected_digest=None, given=[]))
    assert created["workspace"] == "u-bogdan"


def test_swarm_status_names_each_tasks_workspace():
    client = types.SimpleNamespace(task=lambda task_id: {"id": task_id, "tenant_id": "eng", "state": "RUNNING"})
    rows = json.loads(server._call(client, "swarm_status", {"task_ids": ["task_a"]}))
    assert rows[0]["workspace"] == "eng"


# --------------------------------------------------------------------------
# /sc workflows text, the hook's notice, the skill doc
# --------------------------------------------------------------------------


def test_sc_workflows_prints_the_workspace_first_on_every_line(monkeypatch):
    monkeypatch.setattr(sc, "running_workflows", lambda client: {
        "tenant_id": "eng", "count": 1, "complete": True,
        "workflows": [{"workflow_id": "wf_1", "workspace": "u-bogdan", "label": "gate", "state": "RUNNING",
                       "age_seconds": 90, "current_steps": []}],
    })
    monkeypatch.setattr(sc, "running_tasks", lambda client: {
        "tenant_id": "eng", "count": 1, "complete": True,
        "tasks": [{"task_id": "task_a", "workspace": "eng", "label": "lane", "state": "QUEUED", "age_seconds": 5}],
    })
    out = io.StringIO()
    args = types.SimpleNamespace(json=False, width=200, color="never", ascii=True, session_start=False)
    sc.cmd_workflows(_NoTenants(), args, out)
    text = out.getvalue()
    assert "  [u-bogdan] wf_1  gate  RUNNING" in text, text
    assert "  [eng] task_a  lane  QUEUED" in text, text


def test_the_session_start_notice_names_the_workspace_and_the_cap(monkeypatch):
    monkeypatch.delenv("SWARM_PLUGIN_MAX_LIVE_ROWS", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS", raising=False)
    text = sc.session_start_context(
        {"workflows": [{"workflow_id": "wf_1", "workspace": "u-bogdan", "state": "RUNNING", "current_steps": []}]},
        {"tasks": [{"task_id": "task_a", "workspace": "eng", "state": "RUNNING"}]},
    )
    assert "[u-bogdan] wf_1" in text and "[eng] task_a" in text
    assert "at most 30 workflows and 30 single tasks" in text


def test_the_group_order_is_stable_and_newest_first_within_each_workspace():
    entries = [{"id": 1, "workspace": "a"}, {"id": 2, "workspace": "b"}, {"id": 3, "workspace": "a"},
               {"id": 4, "workspace": None}, {"id": 5, "workspace": "b"}]
    assert [e["id"] for e in sc.group_by_workspace(entries)] == [1, 3, 2, 5, 4]


def test_the_skill_doc_says_30_and_that_scrolling_is_claude_codes():
    text = " ".join(_SKILL.read_text().split())
    assert "at most 10" not in text and "past 10 workflows" not in text
    assert "max_live_rows" in text and "30" in text
    assert "cannot add scrolling" in text
    assert "[<workspace>]" in text
