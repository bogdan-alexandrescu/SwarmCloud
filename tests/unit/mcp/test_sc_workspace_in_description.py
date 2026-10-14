"""The run is named by its workflow or task; the workspace is in its description (SC-WS-DESC).

Owner request, 2026-10-14, seeing the rows after plugin 0.5.35: "instead of
[eng@saga.xyz] in the main row name, we should have it in the description of
the workflow or task instead". Held here:

* THE NAME. A `/sc:swarmcloud` run's meta.name is `SC · <name> · N steps`
  (a workflow) or `SC · <label> · task` (single tasks): no `[<workspace>] `
  segment, at most 150 characters, the step count never cut.
* THE DESCRIPTION. meta.description STARTS with `Workspace: <shown>
  (<tenant>) · `, so DESCRIPTION_CHARS clips the end of it and never the
  workspace; with no workspace named by the API it says `Workspace: not
  reported` instead of leaving it out.
* THE DOCS. The skill and the swarm_workflow_launch tool description state the
  format launch.py writes.

The per-step `[SwarmCloud] [<workspace>] ...` row labels inside a run are not
part of this: test_sc_rows_workspace_and_cap holds those.

THE MUTATIONS THESE CATCH: pass the workspace to workflow_title again (the
name tests go red); append the workspace to the description instead of
leading with it (the clipped-description test goes red); omit it when unknown
(the "not reported" control goes red).
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from swarm_mcp import launch, sc, server, workflows

from test_plugin_agents_and_workflows import _PLUGIN

_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"

#: The release this lane follows: the titles change in the bridge the plugin pins.
_PREVIOUS_RELEASED_VERSION = (0, 5, 35)


@pytest.fixture
def _plugin_root(monkeypatch, tmp_path):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(launch.tempfile, "tempdir", str(tmp_path))
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS"):
        monkeypatch.delenv(name, raising=False)
    launch.forget_attached()


def _meta(script: str) -> tuple[str, str]:
    """`(meta.name, meta.description)` of a written copy, read off its two
    JSON-literal lines (launch._js_string writes them as JSON)."""
    lines = Path(script).read_text(encoding="utf-8").split("\n")
    name = json.loads(lines[1][len("  name: "):-1])
    description = json.loads(lines[2][len("  description: "):-1])
    return name, description


class _Me:
    """A client whose `GET /v1/tenants/me` names `tenant`, shown as `shown`."""

    def __init__(self, tenant: str | None, shown: str | None = None):
        self.tenant, self.shown = tenant, shown

    def request(self, method, path):
        assert (method, path) == ("GET", "/v1/tenants/me")
        if self.tenant is None:
            raise RuntimeError("no tenant read")
        record = {"tenant_id": self.tenant}
        if self.shown:
            record["display_name"] = self.shown
        return {"tenant": record, "principal": {}}

    def tenants_mine(self):
        return [{"tenant_id": self.tenant, "display_name": self.shown}] if self.tenant and self.shown else []


def _spec(label: str, steps: int = 2) -> dict:
    return {"label": label, "steps": [
        {"step_id": f"s{n}", "prompt": "p", "runner_profile": "claude-code"} for n in range(steps)
    ]}


# --------------------------------------------------------------------------
# The name: no workspace segment
# --------------------------------------------------------------------------


def test_a_workflow_run_is_named_by_its_workflow_alone(_plugin_root):
    out = launch.for_spec(_spec("gate"), _Me("eng", "eng@saga.xyz"))
    name, _ = _meta(out["script_path"])
    assert out["title"] == name == "SC · gate · 2 steps"
    assert "[" not in name and "eng" not in name


def test_a_long_workflow_name_is_cut_at_150_and_keeps_its_step_count(_plugin_root):
    out = launch.for_spec(_spec("word " * 60, steps=12), _Me("eng", "eng@saga.xyz"))
    name, _ = _meta(out["script_path"])
    assert len(name) <= workflows.TITLE_CHARS
    assert name.startswith("SC · word") and name.endswith("… · 12 steps")
    assert "[" not in name
    # The workspace's room went back to the name: it is the same cut as a
    # title that never knew a workspace.
    assert name == workflows.workflow_title("word " * 60, 12)


def test_a_task_run_is_named_by_its_label_alone(_plugin_root):
    out = launch.for_tasks([{"task_id": "task_a", "label": "lane-a", "workspace": "eng@saga.xyz"}])
    name, _ = _meta(out["script_path"])
    assert out["title"] == name == "SC · lane-a · task"
    several = launch.for_tasks([
        {"task_id": "task_a", "label": "lane-a", "workspace": "eng"},
        {"task_id": "task_b", "label": "lane-b", "workspace": "u-bogdan"},
    ])
    assert several["title"] == "SC · lane-a +1 · 2 tasks"


def test_a_long_task_label_is_cut_at_150_and_keeps_its_count(_plugin_root):
    out = launch.for_tasks([{"task_id": "task_a", "label": "x" * 400, "workspace": "eng"}])
    assert len(out["title"]) <= workflows.TITLE_CHARS
    assert out["title"].startswith("SC · xxx") and out["title"].endswith("… · task")
    assert "[" not in out["title"]


def test_an_attached_workflow_is_named_by_its_workflow_alone(_plugin_root, monkeypatch):
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: {
        "workflow": {"workflow_id": wid, "tenant_id": "eng", "steps": [{}, {}, {}]},
        "tasks": [{"metadata": {"title": "nightly"}}],
    })
    out = launch.for_attach(_Me("eng", "eng@saga.xyz"), "wf_a")
    name, description = _meta(out["script_path"])
    assert out["title"] == name == "SC · nightly · 3 steps"
    assert description.startswith("Workspace: eng@saga.xyz (eng) · ")
    # The row's own workspace still travels to run.js for its step rows.
    assert out["args"]["workspace"] == "eng@saga.xyz"


# --------------------------------------------------------------------------
# The description: the workspace first
# --------------------------------------------------------------------------


def test_a_workflow_runs_description_starts_with_its_workspace(_plugin_root):
    out = launch.for_spec(_spec("gate"), _Me("eng", "eng@saga.xyz"))
    _, description = _meta(out["script_path"])
    assert description.startswith("Workspace: eng@saga.xyz (eng) · SC · gate · 2 steps: ")


def test_a_workspace_with_no_display_name_is_its_tenant_once(_plugin_root):
    out = launch.for_spec(_spec("gate"), _Me("eng"))
    _, description = _meta(out["script_path"])
    assert description.startswith("Workspace: eng · SC · gate")


def test_a_task_runs_description_starts_with_its_workspace(_plugin_root):
    out = launch.for_tasks([{"task_id": "task_a", "label": "lane-a", "workspace": "eng"}])
    _, description = _meta(out["script_path"])
    assert description.startswith("Workspace: eng · SC · lane-a · task: ")


def test_tasks_in_several_workspaces_name_each_once(_plugin_root):
    out = launch.for_tasks([
        {"task_id": "t1", "label": "a", "workspace": "eng"},
        {"task_id": "t2", "label": "b", "workspace": "u-bogdan"},
        {"task_id": "t3", "label": "c", "workspace": "eng"},
    ])
    _, description = _meta(out["script_path"])
    assert description.startswith("Workspace: eng, u-bogdan · ")


def test_a_clipped_description_keeps_the_workspace(_plugin_root, monkeypatch):
    """The base description of an attach with a 150-character title is long
    enough that DESCRIPTION_CHARS clips it: the clip takes the end, never the
    workspace at the start."""
    long_name = "a very long workflow name " * 10
    workflow_id = "wf_" + "0" * 40
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: {
        "workflow": {"workflow_id": wid, "tenant_id": "eng", "steps": [{}]},
        "tasks": [{"metadata": {"title": long_name}}],
    })
    out = launch.for_attach(_Me("eng", "eng@saga.xyz"), workflow_id)
    _, description = _meta(out["script_path"])
    assert len(description) == launch.DESCRIPTION_CHARS and description.endswith("…"), "it was clipped"
    assert description.startswith("Workspace: eng@saga.xyz (eng) · SC · a very long")


def test_no_workspace_reported_says_so_rather_than_omitting_it(_plugin_root):
    """The control: a spec whose tenant read fails, and a task whose API
    document named no tenant, say `Workspace: not reported`."""
    out = launch.for_spec(_spec("gate"), _Me(None))
    _, description = _meta(out["script_path"])
    assert description.startswith("Workspace: not reported · SC · gate · 2 steps")
    out = launch.for_tasks([{"task_id": "task_a", "label": "lane-a"}])
    _, description = _meta(out["script_path"])
    assert description.startswith("Workspace: not reported · SC · lane-a · task")


def test_attach_all_names_runs_alone_and_describes_each_workspace(_plugin_root, monkeypatch):
    monkeypatch.setattr(sc, "running_workflows", lambda client: {"count": 2, "complete": True, "workflows": [
        {"workflow_id": "wf_00", "workspace": "eng", "title": "flow 00", "label": None, "steps_total": 3},
        {"workflow_id": "wf_01", "workspace": "u-bogdan", "title": "flow 01", "label": None, "steps_total": 1},
    ]})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 1, "tasks": [
        {"task_id": "task_a", "label": "lane-a", "workspace": "eng"}]})
    client = types.SimpleNamespace(tenants_mine=lambda: [{"tenant_id": "eng", "display_name": "eng@saga.xyz"}])
    out = launch.for_all(client)
    got = [(l["title"], _meta(l["script_path"])[1].split(" · SC · ")[0]) for l in out["launches"]]
    assert got == [
        ("SC · flow 00 · 3 steps", "Workspace: eng@saga.xyz (eng)"),
        ("SC · flow 01 · 1 step", "Workspace: u-bogdan"),
        ("SC · lane-a · task", "Workspace: eng@saga.xyz (eng)"),
    ]
    assert out["launches"][0]["args"]["workspace"] == "eng@saga.xyz"


# --------------------------------------------------------------------------
# The docs state what launch.py writes
# --------------------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_skill_doc_states_the_title_format_launch_writes(_plugin_root):
    text = _flat(_SKILL.read_text(encoding="utf-8"))
    assert "SC · [<workspace>]" not in text, "the run's name carries no workspace any more"
    assert "`SC · <name> · N steps`" in text and "`SC · <label> · task`" in text
    assert "`Workspace: <workspace> (<tenant>) · `" in text and "`Workspace: not reported`" in text
    # The stated formats, filled in, are launch.py's output.
    flow = launch.for_spec(_spec("gate", steps=4), _Me("eng", "eng@saga.xyz"))
    assert flow["title"] == "SC · <name> · N steps".replace("<name>", "gate").replace("N", "4")
    task = launch.for_tasks([{"task_id": "task_a", "label": "lane-a", "workspace": "eng"}])
    assert task["title"] == "SC · <label> · task".replace("<label>", "lane-a")
    _, description = _meta(flow["script_path"])
    assert description.startswith("Workspace: <workspace> (<tenant>) · "
                                  .replace("<workspace>", "eng@saga.xyz").replace("<tenant>", "eng"))


def test_the_launch_tool_description_states_the_same_format():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_workflow_launch")
    text = _flat(tool["description"])
    assert "SC · [<workspace>]" not in text
    assert "`SC · <name> · N steps`" in text and "`SC · <label> · task`" in text
    assert "Workspace: <workspace> (<tenant>)" in text and "Workspace: not reported" in text


def test_the_plugin_is_bumped_and_pins_the_bridge_at_its_own_tag():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version > _PREVIOUS_RELEASED_VERSION, manifest["version"]
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#subdirectory=apps/swarm-mcp" in arg for arg in pinned), pinned
