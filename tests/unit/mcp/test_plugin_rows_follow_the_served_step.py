"""`/sc:swarmcloud` rows follow the step the API serves, and a spec is the request.

Four findings on epic #1334, each measured on a live session:

* a platform CI-fix or conflict-fix round is served as step `ci-fix:<n>` /
  `conflict-fix:<n>` (swarm_api.mergewake `_append_round_step`), but its task
  is step `ci-fix` / `conflict-fix`; the attach row was handed the round id,
  swarm_follow refused the task as "another step's task id", and the row ended
  UNKNOWN (wf_605ebc3f, wf_abc3039f, wf_77ec307c, 2026-10-15);
* an attach row handed a quoted step id (`"implement"`) followed nothing and
  ended UNKNOWN (GS-WORKER / GS-INFRA, 2026-10-16), and a 429 on the follow
  ended a row (the bridge half is in test_follow_progress_format.py);
* the submit row weighed the owner's last typed message ("status") against a
  prepared spec and submitted nothing (wf_c7292607-6ef, 2026-10-16).

run.js is run under node with the workflow globals stubbed, by the harness in
`test_plugin_agents_and_workflows`.
"""

from __future__ import annotations

from test_plugin_agents_and_workflows import _ANSWERS, _PLUGIN, _SPEC, _run, _step

_AGENTS = _PLUGIN / "agents"


def _flat(path) -> str:
    return " ".join(path.read_text().split())


def _attached(steps) -> dict:
    return {"workflow_id": "wf_r", "state": "RUNNING", "state_note": None, "title": "lane", "label": None,
            "console": None, "bridge_version": "0.1.0", "follow_error": None, "error": None, "steps": steps}


def _rows(got) -> list[dict]:
    return [c for c in got["calls"] if c["agentType"] == "sc:step"]


def _line(prompt: str, key: str) -> str:
    (line,) = [entry for entry in prompt.split("\n") if entry.startswith(key + ": ")]
    return line[len(key) + 2:]


# --------------------------------------------------------------------------
# A fix round's row follows the step its task is
# --------------------------------------------------------------------------


def test_a_fix_round_row_follows_its_tasks_step_and_keeps_the_round_in_its_title(tmp_path):
    steps = [
        {"step_id": "implement", "task_id": "task_i", "depends_on": [], "state": "SUCCEEDED", "console": None},
        {"step_id": "merge", "task_id": "task_m", "depends_on": ["implement"], "state": "PARKED", "console": None},
        {"step_id": "ci-fix:1", "task_id": "task_c", "depends_on": [], "state": "RUNNING", "console": None},
        {"step_id": "conflict-fix:2", "task_id": "task_f", "depends_on": [], "state": "QUEUED", "console": None},
    ]
    answers = {"ATTACH": _attached(steps), "STATUS": {"state": "SUCCEEDED", "state_note": None, "steps": []},
               "step:merge": _step("SUCCEEDED"), "step:ci-fix": _step("SUCCEEDED", duration_s=60),
               "step:conflict-fix": _step("SUCCEEDED", duration_s=30)}

    got = _run(tmp_path, {"attach": "wf_r"}, answers)

    assert "error" not in got, got.get("error")
    by_task = {_line(c["prompt"], "task_id"): c for c in _rows(got)}
    assert _line(by_task["task_c"]["prompt"], "step_id") == "ci-fix"
    assert _line(by_task["task_f"]["prompt"], "step_id") == "conflict-fix"
    assert _line(by_task["task_m"]["prompt"], "step_id") == "merge"  # the control: no round, unchanged
    assert by_task["task_c"]["label"].endswith(" · ci-fix:1"), by_task["task_c"]["label"]
    assert by_task["task_f"]["label"].endswith(" · conflict-fix:2"), by_task["task_f"]["label"]
    states = {row["step_id"]: row.get("state") for row in got["result"]["steps"]}
    assert states == {"implement": "SUCCEEDED", "merge": "SUCCEEDED", "ci-fix:1": "SUCCEEDED",
                      "conflict-fix:2": "SUCCEEDED"}, states


def test_the_attach_probe_is_told_to_follow_a_rounds_task_step():
    flat = _flat(_AGENTS / "workflow.md")
    assert "ci-fix:1" in flat and "the part before the `:`" in flat


# --------------------------------------------------------------------------
# A quoted step id is the step id
# --------------------------------------------------------------------------


def test_a_quoted_step_id_enters_its_row_unquoted(tmp_path):
    steps = [
        {"step_id": '"implement"', "task_id": "task_i", "depends_on": [], "state": "RUNNING", "console": None},
        {"step_id": "'review'", "task_id": "task_r", "depends_on": ['"implement"'], "state": "QUEUED",
         "console": None},
    ]
    answers = {"ATTACH": _attached(steps), "STATUS": {"state": "SUCCEEDED", "state_note": None, "steps": []},
               "step:implement": _step("SUCCEEDED"), "step:review": _step("SUCCEEDED")}

    got = _run(tmp_path, {"attach": "wf_r"}, answers)

    assert "error" not in got, got.get("error")
    rows = {_line(c["prompt"], "task_id"): c for c in _rows(got)}
    assert _line(rows["task_i"]["prompt"], "step_id") == "implement"
    assert _line(rows["task_r"]["prompt"], "step_id") == "review"
    assert _line(rows["task_r"]["prompt"], "depends_on") == "implement"
    # The parent is found under its unquoted id, so the row waits on its task.
    assert _line(rows["task_r"]["prompt"], "parent_task_ids") == "task_i"
    assert all('"' not in c["label"] and "'" not in c["label"] for c in rows.values())
    assert {row["step_id"]: row["state"] for row in got["result"]["steps"]} == {
        "implement": "SUCCEEDED", "review": "SUCCEEDED"}


def test_a_rate_limited_follow_is_repeated_not_counted_by_both_row_agents():
    for name in ("step.md", "task.md"):
        flat = _flat(_AGENTS / name)
        assert "`rate_limited` is not a failure" in flat, name
        assert "count it toward neither limit" in flat, name


# --------------------------------------------------------------------------
# A launch with a spec is the request
# --------------------------------------------------------------------------


def _submit_prompt(tmp_path, args, answers=None) -> str:
    got = _run(tmp_path, args, answers or _ANSWERS)
    (submit,) = [c for c in got["calls"] if c["prompt"].startswith("SUBMIT")]
    return submit["prompt"]


def _says_the_spec_is_the_request(prompt: str) -> None:
    last = prompt.split("\n")[-1]
    assert last.startswith("request: this spec is the whole request -- submit it."), last
    assert "never weigh it" in last and "never refuse" in last, last


def test_an_inline_spec_prompt_says_the_spec_is_the_request(tmp_path):
    _says_the_spec_is_the_request(_submit_prompt(tmp_path, _SPEC))


def test_a_spec_path_prompt_says_the_spec_is_the_request(tmp_path):
    _says_the_spec_is_the_request(_submit_prompt(tmp_path, {"spec": _SPEC, "spec_path": "/work/specs/scan.json"}))


def test_a_spec_ref_prompt_says_the_spec_is_the_request(tmp_path):
    from swarm_mcp import workflows

    outline = {"label": "scan", "steps": [
        {"step_id": s["step_id"], "depends_on": s.get("depends_on", []), "stage": s.get("stage")}
        for s in _SPEC["steps"]
    ]}
    read = {"path": "/work/specs/scan.json", "spec_ref": "spec_0123456789abcdef",
            "spec_digest": workflows.spec_digest(_SPEC), "outline": outline, "error": None}
    _says_the_spec_is_the_request(_submit_prompt(tmp_path, "specs/scan.json", {**_ANSWERS, "READ SPEC": read}))


def test_the_submit_agent_is_told_never_to_weigh_the_persons_last_message():
    flat = _flat(_AGENTS / "workflow.md")
    assert "THE SPEC IS THE REQUEST" in flat
    assert "Never weigh it against the spec" in flat
