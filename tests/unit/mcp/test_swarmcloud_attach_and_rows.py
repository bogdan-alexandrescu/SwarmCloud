"""`/sc:swarmcloud`: attach, a bridge that refuses the rows' follow, and submission by reference.

Owner decisions, 2026-10-01 (lane B21, functionality wave 2):

* ATTACH. `/sc:swarmcloud {attach: "<workflow_id>"}` submits NOTHING. It reads
  the workflow once, reports finished steps once without a row, starts the
  same `sc:step` row for every unfinished step, and ends with the same final
  status read -- so a session that restarted can watch a workflow still
  running in SwarmCloud. An unknown or other-tenant id fails with the API's
  error.
* FAIL LOUDLY. 19 rows over 4 workflows silently fell back to `lines` because a
  pinned bridge answered `unknown format 'progress'`. The submit phase probes
  the bridge once and fails the workflow with the bridge's version and the
  likely cause (a SWARM_MCP_FROM override); no row ever tries another format.
* SUBMIT WITHOUT RETYPING. Specs of 11-44 KB came back altered from the haiku
  relay three times in one evening. A spec given with its file path, or by
  path alone, is submitted by reference (`spec_path` / `spec_ref`); no agent
  prompt carries it.
* UNSTARTED STEPS DON'T POLL. A row is handed its unfinished parents' task ids
  and makes one call that holds until they finish.

run.js is run under node with the workflow globals stubbed, by the harness in
`test_plugin_agents_and_workflows`.
"""

from __future__ import annotations

import json
import re

from swarm_common.states import TERMINAL_STATES
from swarm_mcp import workflows

from test_plugin_agents_and_workflows import (
    _ANSWERS,
    _PLUGIN,
    _RUN_JS,
    _SPEC,
    _SUBMITTED,
    _load,
    _meta,
    _run,
    _step,
)

_STEP_MD = _PLUGIN / "agents" / "step.md"
_WORKFLOW_MD = _PLUGIN / "agents" / "workflow.md"


def _flat(path) -> str:
    return " ".join(path.read_text().split())


# --------------------------------------------------------------------------
# Attach
# --------------------------------------------------------------------------

_ATTACHED = {
    "workflow_id": "wf_9",
    "state": "RUNNING",
    "state_note": None,
    "bridge_version": "0.1.0",
    "follow_error": None,
    "error": None,
    "steps": [
        {"step_id": "implement", "task_id": "task_1", "depends_on": [], "state": "SUCCEEDED"},
        {"step_id": "review", "task_id": "task_2", "depends_on": ["implement"], "state": "RUNNING"},
        {"step_id": "fix", "task_id": "task_3", "depends_on": ["review"], "state": "PARKED"},
    ],
}

_ATTACH_ANSWERS = {
    "ATTACH": _ATTACHED,
    "step:review": _step("SUCCEEDED", cost_usd=0.4, duration_s=600),
    "step:fix": _step("SUCCEEDED", cost_usd=0.2, duration_s=300),
    "STATUS": {"state": "SUCCEEDED", "state_note": None, "steps": []},
}


def test_attach_makes_no_submit_call(tmp_path):
    got = _run_attach(tmp_path)
    first_lines = [c["prompt"].split("\n")[0] for c in got["calls"]]
    assert "SUBMIT" not in first_lines and "READ SPEC" not in first_lines, first_lines
    assert first_lines[0] == "ATTACH" and first_lines[-1] == "STATUS", first_lines
    attach = got["calls"][0]
    assert attach["prompt"] == "ATTACH\nworkflow_id: wf_9"
    assert (attach["agentType"], attach["phase"]) == ("sc:workflow", "Attach")
    assert got["result"]["workflow_id"] == "wf_9" and got["result"]["state"] == "SUCCEEDED"


def test_attach_starts_no_row_for_a_finished_step_and_reports_it_once(tmp_path):
    got = _run_attach(tmp_path)
    rows = [c for c in got["calls"] if c["agentType"] == "sc:step"]
    assert [c["label"].rsplit(" · ", 1)[-1] for c in rows] == ["review", "fix"]
    said = [line for line in got["logs"] if line.startswith("implement ")]
    assert said == ["implement SUCCEEDED · finished before attach"], got["logs"]
    by_id = {row["step_id"]: row for row in got["result"]["steps"]}
    assert by_id["implement"]["state"] == "SUCCEEDED" and by_id["implement"]["task_id"] == "task_1"


def test_attach_starts_one_row_for_a_running_step(tmp_path):
    got = _run_attach(tmp_path)
    review = [c for c in got["calls"] if c["agentType"] == "sc:step" and "step_id: review" in c["prompt"]]
    assert len(review) == 1
    assert "task_id: task_2" in review[0]["prompt"] and "workflow_id: wf_9" in review[0]["prompt"]
    # Its parent finished before the attach, so it waits on nothing.
    assert "parent_task_ids: none" in review[0]["prompt"]


def test_an_attached_row_waits_only_on_its_unfinished_parents(tmp_path):
    got = _run_attach(tmp_path)
    (fix,) = [c for c in got["calls"] if c["agentType"] == "sc:step" and "step_id: fix" in c["prompt"]]
    assert "parent_task_ids: task_2" in fix["prompt"], fix["prompt"]


def test_an_unknown_or_other_tenant_workflow_fails_with_the_apis_error(tmp_path):
    refused = {**{k: None for k in _ATTACHED}, "steps": [],
               "error": "GET /v1/workflows/wf_x -> 404: workflow wf_x not found"}
    got = _run(tmp_path, {"attach": "wf_x"}, {"ATTACH": refused})
    assert len(got["calls"]) == 1
    assert got["result"]["state"] == "NOT_ATTACHED"
    assert got["result"]["error"] == refused["error"], "the API's error, verbatim"
    assert got["result"]["steps"] == []


def test_attach_needs_a_workflow_id(tmp_path):
    got = _run(tmp_path, {"attach": "  "}, {})
    assert "attach" in got["error"] and got["calls"] == []


def test_attach_is_described_in_the_workflows_meta():
    meta = _meta()
    assert "attach" in meta["whenToUse"].lower()
    # `Tasks`: the {attach_tasks} run's single-task rows (#830).
    assert [p["title"] for p in meta["phases"]] == ["Submit", "Attach", "Tasks", "Result"]


def _run_attach(tmp_path, answers=None):
    got = _run(tmp_path, {"attach": "wf_9"}, answers or _ATTACH_ANSWERS)
    assert "error" not in got, got.get("error")
    return got


# --------------------------------------------------------------------------
# A bridge that refuses the rows' follow fails the workflow, loudly
# --------------------------------------------------------------------------

_REFUSAL = "unknown format 'progress'; swarm_follow takes json or lines"


def test_a_bridge_that_refuses_the_progress_follow_fails_the_workflow_naming_its_version(tmp_path):
    submitted = {**_SUBMITTED, "bridge_version": "0.0.9", "follow_error": _REFUSAL}
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "SUBMIT": submitted})
    assert "error" not in got, got.get("error")
    assert [c["agentType"] for c in got["calls"]] == ["sc:workflow"], "no row may start on a refusing bridge"
    result = got["result"]
    assert result["state"] == "FOLLOW_REFUSED" and result["workflow_id"] == "wf_1"
    error = result["error"]
    assert "swarm-mcp 0.0.9" in error and _REFUSAL in error
    assert "SWARM_MCP_FROM" in error
    assert "attach" in error, "the way back, once the bridge is fixed, is attach"


def test_a_bridge_that_reports_no_version_is_named_as_older_than_the_probe(tmp_path):
    submitted = {**_SUBMITTED, "bridge_version": None, "follow_error": _REFUSAL}
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "SUBMIT": submitted})
    assert "reported no version" in got["result"]["error"]


def test_the_submit_row_is_told_to_probe_once_with_the_progress_format():
    flat = _flat(_WORKFLOW_MD)
    assert '"format": "progress"' in flat and '"wait_seconds": 0' in flat
    assert "follow_error" in flat and "bridge_version" in flat


def test_attach_fails_loudly_on_a_refusing_bridge_too(tmp_path):
    got = _run(tmp_path, {"attach": "wf_9"}, {**_ATTACH_ANSWERS, "ATTACH": {**_ATTACHED, "follow_error": _REFUSAL}})
    assert got["result"]["state"] == "FOLLOW_REFUSED"
    assert [c["agentType"] for c in got["calls"]] == ["sc:workflow"]


def test_step_md_and_run_js_name_no_follow_format_but_progress():
    """A row that "falls back" to another format is the defect; the only
    format any of these files may name is `progress`."""
    for path in (_STEP_MD, _RUN_JS, _WORKFLOW_MD):
        text = path.read_text()
        named = re.findall(r"""format["'`]?\s*[:=]\s*["'`]([a-z]+)""", text)
        assert named and set(named) == {"progress"}, f"{path.name} names formats {sorted(set(named))}"
        for other in ("lines", "json"):
            for quote in ('"', "'", "`"):
                assert f"{quote}{other}{quote}" not in text, f"{path.name} names the {other} format"


def test_step_md_ends_the_row_on_a_format_error_and_never_retries_another_format():
    flat = _flat(_STEP_MD)
    assert "ONLY" in flat and '`format: "progress"`' in flat
    assert "never retry in another format" in flat.lower() or "never retries in another format" in flat.lower()
    assert "format error" in flat.lower()


# --------------------------------------------------------------------------
# Rows hold, and an unstarted step does not poll
# --------------------------------------------------------------------------


def test_step_md_uses_the_maximum_hold_for_every_call():
    from swarm_mcp import compact

    flat = _flat(_STEP_MD)
    assert f"`wait_seconds: {compact.MAX_WAIT_SECONDS}`" in flat
    holds = set(re.findall(r"`wait_seconds: (\d+)`", flat))
    assert holds == {str(compact.MAX_WAIT_SECONDS)}, holds


def test_step_md_makes_one_parent_call_for_an_unstarted_step():
    flat = _flat(_STEP_MD)
    assert "`parents`" in flat and "parent_task_ids" in flat


def test_a_submitted_dependent_steps_row_is_handed_its_parents_task_ids(tmp_path):
    got = _run(tmp_path, _SPEC, _ANSWERS)
    prompts = {c["label"].rsplit(" · ", 1)[-1]: c["prompt"] for c in got["calls"] if c["agentType"] == "sc:step"}
    assert "parent_task_ids: task_1, task_2" in prompts["join"], prompts["join"]
    assert "parent_task_ids: task_3" in prompts["report"]
    assert "parent_task_ids: none" in prompts["scan-01"]


def test_run_js_terminal_states_are_the_contracts():
    source = _RUN_JS.read_text()
    match = re.search(r"const TERMINAL = \[([^\]]*)\]", source)
    assert match, "run.js must name the terminal states once, as TERMINAL"
    named = set(re.findall(r"'([A-Z_]+)'", match.group(1)))
    assert named == {s.value for s in TERMINAL_STATES}


# --------------------------------------------------------------------------
# Submission by reference: no agent prompt carries the spec
# --------------------------------------------------------------------------


def _big_spec() -> dict:
    filler = "Read the module, then write the change and its tests. " * 20
    steps = [{"step_id": f"s{i:02d}", "prompt": f"PROMPTMARK {i} " + filler,
              **({"depends_on": [f"s{i - 1:02d}"]} if i else {})} for i in range(45)]
    spec = {"label": "big", "steps": steps}
    assert len(json.dumps(spec)) >= 50 * 1024
    return spec


def test_a_50_kb_spec_given_with_its_path_submits_by_spec_path_and_no_prompt_carries_it(tmp_path):
    spec = _big_spec()
    digest = workflows.spec_digest(spec)
    submitted = {
        **_SUBMITTED, "spec_digest": digest,
        "steps": [{"step_id": s["step_id"], "task_id": "t_" + s["step_id"], "depends_on": s.get("depends_on", [])}
                  for s in spec["steps"]],
    }
    answers = {**_ANSWERS, "SUBMIT": submitted, **{"step:" + s["step_id"]: _step("SUCCEEDED") for s in spec["steps"]}}
    got = _run(tmp_path, {"spec": spec, "spec_path": "/work/widgets/specs/big.json"}, answers)

    assert "error" not in got, got.get("error")
    submit = got["calls"][0]
    assert submit["prompt"] == "SUBMIT\nspec_digest: " + digest + "\nspec_path: /work/widgets/specs/big.json"
    assert all("PROMPTMARK" not in c["prompt"] for c in got["calls"]), "an agent prompt carried the spec"
    assert got["result"]["workflow_id"] == "wf_1"


def test_a_spec_given_by_path_alone_is_submitted_by_the_ref_the_bridge_read(tmp_path):
    outline = {"label": "scan", "steps": [
        {"step_id": s["step_id"], "depends_on": s.get("depends_on", []), "stage": s.get("stage")} for s in _SPEC["steps"]
    ]}
    read = {"path": "/work/widgets/specs/scan.json", "spec_ref": "spec_0123456789abcdef",
            "spec_digest": workflows.spec_digest(_SPEC), "outline": outline, "error": None}
    got = _run(tmp_path, "specs/scan.json", {**_ANSWERS, "READ SPEC": read})

    assert "error" not in got, got.get("error")
    read_call, submit = got["calls"][0], got["calls"][1]
    assert read_call["prompt"] == "READ SPEC\npath: specs/scan.json"
    assert submit["prompt"] == "SUBMIT\nspec_digest: " + read["spec_digest"] + "\nspec_ref: spec_0123456789abcdef"
    assert "scan a" not in submit["prompt"]
    labels = [c["label"] for c in got["calls"] if c["agentType"] == "sc:step"]
    # The console's stage (level + 1), not the outline's `stage` name.
    assert "[SwarmCloud] scan · stage 3 · report" in labels, labels


def test_the_read_row_copies_the_outline_and_never_the_spec():
    flat = _flat(_WORKFLOW_MD)
    assert "spec_ref" in flat and "outline" in flat and "spec_path" in flat
    assert "never retype" in flat.lower() or "never copy the spec" in flat.lower()


def test_an_inline_spec_still_submits_inline(tmp_path):
    got = _run(tmp_path, _SPEC, _ANSWERS)
    lines = got["calls"][0]["prompt"].split("\n")
    assert lines[:3] == ["SUBMIT", "spec_digest: " + workflows.spec_digest(_SPEC), "BEGIN SPEC"]
    assert got["result"]["workflow_id"] == "wf_1"


# --------------------------------------------------------------------------
# The delegate skill says what attach is for
# --------------------------------------------------------------------------


def test_the_delegate_skill_describes_attach():
    _, body = _load(_PLUGIN / "skills" / "delegate" / "SKILL.md")
    flat = " ".join(body.split())
    assert '{attach: "<workflow_id>"}' in flat
    assert "submits nothing" in flat.lower()
