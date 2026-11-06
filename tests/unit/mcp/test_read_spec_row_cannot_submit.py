"""The `setup · read spec` row cannot submit, and a submission it made anyway is THE submission.

AI Studio's session, 5 November 2026 (plugin 0.5.79): twice a run ended
NOT_SUBMITTED with "the row reading <spec_handle path> did not hand back the
spec_ref, spec_digest and outline the bridge returns, so nothing was
submitted", yet the workflow existed and ran, and a relaunch made a
duplicate. The run's per-agent journal showed the read row answering
`{path: null, spec_ref: null, spec_digest: <digest>, outline: null,
workflow_id: <a real id>, workspace, console, steps: [...]}`: the row's agent
(`sc:workflow`, which also holds `swarm_workflow`) had SUBMITTED the spec
instead of reading its outline.

So (1) the read row runs `sc:spec`, an agent whose only SwarmCloud tool is
`swarm_workflow_spec`; and (2) a row before the submit row that hands back a
workflow id anyway is taken as the submission: the run reports that id,
follows it, and never submits again.
"""

from __future__ import annotations

from swarm_mcp import workflows

from test_plugin_agents_and_workflows import _PLUGIN, _PREFIXES, _granted, _load, _step
from test_plugin_rows_match_console import _run

_SPEC = {
    "label": "studio-fix",
    "title": "Studio fix",
    "steps": [
        {"step_id": "implement", "prompt": "Do it.", "runner_profile": "claude-code"},
        {"step_id": "review", "prompt": "Review it.", "runner_profile": "claude-code", "depends_on": ["implement"]},
    ],
}
_DIGEST = workflows.spec_digest(_SPEC)
_HANDLE = {"path": "/work/project/.sc-swarmcloud-runs/spec-0011aabb-00112233aabb.json", "digest": _DIGEST}
_CONSOLE = "https://console.example.test/workflows/wf_read"


def _read_that_submitted() -> dict:
    """The read row's answer from the journal: a submit reply, no outline."""
    return {
        "path": None, "spec_ref": None, "spec_digest": _DIGEST, "outline": None, "error": None,
        "workflow_id": "wf_read", "workspace": "studio", "console": _CONSOLE,
        "steps": [
            {"step_id": "implement", "task_id": "task_i", "depends_on": [], "console": None, "added": None},
            {"step_id": "review", "task_id": "task_r", "depends_on": ["implement"], "console": None, "added": None},
        ],
    }


_ATTACHED = {
    "workflow_id": "wf_read", "workspace": "studio", "title": "Studio fix", "label": "studio-fix",
    "console": _CONSOLE, "state": "RUNNING", "state_note": None, "bridge_version": "0.1.0",
    "follow_error": None, "error": None,
    "steps": [
        {"step_id": "implement", "task_id": "task_i", "depends_on": [], "state": "RUNNING",
         "runner_profile": "claude-code", "console": None},
        {"step_id": "review", "task_id": "task_r", "depends_on": ["implement"], "state": "QUEUED",
         "runner_profile": "claude-code", "console": None},
    ],
}


def _answers() -> dict:
    return {
        "READ SPEC": _read_that_submitted(),
        "ATTACH": _ATTACHED,
        "SUBMIT": {"workflow_id": "wf_second", "error": None, "steps": []},
        "step:implement": _step("SUCCEEDED"),
        "step:review": _step("SUCCEEDED"),
        "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": _CONSOLE, "steps": []},
        "WAIT": {"waited": True, "error": None},
    }


def _first_lines(got) -> list[str]:
    return [c["prompt"].split("\n")[0] for c in got["calls"]]


def test_the_read_spec_row_runs_an_agent_that_has_no_submit_tool(tmp_path):
    got = _run(tmp_path, "specs/scan.json", {"READ SPEC": {
        "path": None, "spec_ref": None, "spec_digest": None, "outline": None, "error": "not there"}})
    (read,) = got["calls"]
    agent = read["agentType"].split(":", 1)[1]
    fields, _ = _load(_PLUGIN / "agents" / f"{agent}.md")
    granted = _granted(fields["tools"])
    # MUTATION: a read row run by sc:workflow, which holds swarm_workflow, fails here.
    assert "swarm_workflow" not in granted[_PREFIXES[1]] and "swarm_workflow" not in granted[_PREFIXES[0]]
    assert granted[_PREFIXES[1]] == {"swarm_workflow_spec"}, granted


def test_a_read_row_that_hands_back_a_workflow_id_is_the_submission(tmp_path):
    got = _run(tmp_path, {"spec_handle": _HANDLE}, _answers())
    lines = _first_lines(got)
    # MUTATION: a script that judges this NOT_SUBMITTED, or submits a second copy, fails here.
    assert "SUBMIT" not in lines, lines
    assert got["result"]["workflow_id"] == "wf_read"
    assert got["result"]["state"] not in ("NOT_SUBMITTED", "SUBMISSION_UNKNOWN"), got["result"]
    assert got["result"]["submitted_by"] == "read spec"
    # It is followed: the workflow is read back, and its steps get rows.
    assert lines[:2] == ["READ SPEC", "ATTACH"], lines
    assert any(c["agentType"] != "sc:spec" and c["prompt"].startswith("task_id: task_r") for c in got["calls"]), lines
    assert any("wf_read submitted by the read spec row" in line for line in got["logs"]), got["logs"]


def test_a_read_row_that_hands_back_a_workflow_id_reports_submitted_in_the_band_layout(tmp_path):
    got = _run(tmp_path, {"spec_handle": _HANDLE, "rows": "band"}, _answers())
    assert _first_lines(got) == ["READ SPEC"]
    assert got["result"]["state"] == "SUBMITTED"
    assert got["result"]["workflow_id"] == "wf_read"
    assert [s["task_id"] for s in got["result"]["steps"]] == ["task_i", "task_r"]


def test_a_read_row_with_a_workflow_id_and_a_good_outline_still_submits_nothing_again(tmp_path):
    read = _read_that_submitted()
    read.update({"path": _HANDLE["path"], "spec_ref": "spec_00112233aabbccdd", "outline": {
        "title": "Studio fix", "label": "studio-fix", "strategy": None, "merge": None, "steps": [
            {"step_id": "implement", "depends_on": [], "stage": None, "runner_profile": "claude-code",
             "when_step": None, "input_from": {}},
            {"step_id": "review", "depends_on": ["implement"], "stage": None, "runner_profile": "claude-code",
             "when_step": None, "input_from": {}},
        ]}})
    got = _run(tmp_path, {"spec_handle": _HANDLE}, {**_answers(), "READ SPEC": read})
    assert "SUBMIT" not in _first_lines(got)
    assert got["result"]["workflow_id"] == "wf_read"
