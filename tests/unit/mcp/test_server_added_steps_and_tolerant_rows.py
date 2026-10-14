"""`/sc:swarmcloud` starts a row for every step SwarmCloud runs, and every row ends with a result.

MEASURED, 2026-10-14, plugin sc 0.5.27 (SPEED-D5, owner decision on the
delivery speed plan):

  1. A spec of 3 steps launched through the run script came back from the API
     with 5 -- swarm-api appended `re-review` and `merge` -- and the run ended
     SUBMITTED_UNVERIFIED: "the reply relayed back does not match the spec
     given, so no row was started: the reply names a step the spec does not
     have: re-review; ... merge". The operator had to attach by hand. The
     script only accepted the extra steps it could PREDICT from its own copy
     of the server's merge rules, and the prediction missed.
  2. `sc:step` rows ended with "agent({schema}): StructuredOutput retry cap
     (5) exceeded ... must have required property 'state' ... 'answer_excerpt'
     ... 'pr_url'" or "subagent completed without calling StructuredOutput",
     an error row over a step that had finished.

What these tests hold:

  * the bridge marks every reply step the spec it submitted did not name as
    `added: true` (and every spec step `added: false`), so the script does
    not have to predict what the server adds;
  * run.js accepts an extra step the bridge marked added, or one that depends
    on the spec's steps, and starts its row -- whatever the spec's strategy or
    merge choice;
  * run.js still refuses (SUBMITTED_UNVERIFIED, no row) a reply that DROPS or
    RENAMES a spec step, and an unmarked extra step tied to no spec step;
  * the row schema takes any answer, with every field optional and nullable,
    and a row that throws, or answers with no state, takes its state from one
    workflow status read instead of ending as an error row.
"""

from __future__ import annotations

import json

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmClient

from test_plugin_agents_and_workflows import _PLUGIN, _ANSWERS, _SPEC, _bridge_digest, _load, _run, _step
from test_step_row_skip_and_ci_park import _errors, _schema

# The shape the owner measured: three steps, an integrator gated on nothing
# the script's merge rules recognise, so it predicts no appended step at all.
_THREE = {
    "label": "d5",
    "strategy": "collect",
    "steps": [
        {"step_id": "implement", "prompt": "implement the change"},
        {"step_id": "review", "prompt": "review it", "depends_on": ["implement"]},
        {"step_id": "fix", "prompt": "fix every finding", "depends_on": ["review"]},
    ],
}

_ADDED = [
    {"step_id": "re-review", "depends_on": ["fix", "implement", "review"]},
    {"step_id": "merge", "depends_on": ["fix", "re-review"]},
]


def _reply(spec: dict, extra: list[dict], *, mark: bool | None = True, drop: str | None = None,
           rename: tuple[str, str] | None = None) -> dict:
    """The submit row's answer: the spec's steps (one dropped or renamed when
    asked), then `extra`, each marked `added` the way the bridge marks it
    (`mark=None`: the field left out, as a bridge before 0.5.32 sends it)."""
    steps = []
    for step in spec["steps"]:
        if step["step_id"] == drop:
            continue
        step_id = rename[1] if rename and step["step_id"] == rename[0] else step["step_id"]
        steps.append({"step_id": step_id, "depends_on": list(step.get("depends_on") or []),
                      **({} if mark is None else {"added": False})})
    for step in extra:
        steps.append({"step_id": step["step_id"], "depends_on": list(step["depends_on"]),
                      **({} if mark is None else {"added": mark})})
    return {
        "workflow_id": "wf_d5", "console": None, "repository": "https://github.com/acme/widgets.git",
        "repository_notes": [], "spec_digest": _bridge_digest(spec),
        "bridge_version": None, "follow_error": None, "error": None,
        "steps": [{**s, "task_id": "task_" + s["step_id"], "console": None} for s in steps],
    }


def _answers(reply: dict, status_steps: list | None = None) -> dict:
    answers = {"SUBMIT": reply, "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None,
                                           "steps": status_steps or []}}
    for step in reply["steps"]:
        answers["step:" + step["step_id"]] = _step("SUCCEEDED", duration_s=30)
    return answers


def _rows(got: dict) -> list[str]:
    return [c["label"].rsplit(" · ", 1)[-1] for c in got["calls"] if c["agentType"] == "sc:step"]


def _refused(got: dict) -> str:
    assert "error" not in got, got.get("error")
    assert [c["agentType"] for c in got["calls"]] == ["sc:workflow"], "a row started on an unverified reply"
    assert got["result"]["state"] == "SUBMITTED_UNVERIFIED"
    return got["result"]["error"]


# --------------------------------------------------------------------------
# 1. The bridge marks what the server added
# --------------------------------------------------------------------------


class _AppendingApi:
    """The transport, stubbed at `request`: answers with the steps it was sent
    and then a re-review and a merge, as swarm-api's `_with_merge_step` does."""

    dispatch = SwarmClient.dispatch

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        sent = [{"step_id": s["step_id"], "task_id": f"task_{s['step_id']}", "runner_profile": s["runner_profile"],
                 "depends_on": s.get("depends_on") or []} for s in (payload or {}).get("steps", [])]
        added = [{**s, "task_id": f"task_{s['step_id']}", "runner_profile": "claude-code"} for s in _ADDED]
        return {"workflow": {"workflow_id": "wf_d5", "steps": sent + added}, "dispatch": {}}


def test_the_bridge_marks_each_step_the_spec_did_not_name_as_added():
    spec = {k: v for k, v in _THREE.items() if k != "strategy"}
    reply = json.loads(server._call(_AppendingApi(), "swarm_workflow", {"spec": spec}))
    marked = {step["step_id"]: step["added"] for step in reply["steps"]}
    assert marked == {"implement": False, "review": False, "fix": False, "re-review": True, "merge": True}, marked


def test_the_bridge_marks_steps_given_as_steps_too():
    """The `steps` form, not a whole spec: the same comparison, the same marks."""
    steps = [{"step_id": s["step_id"], "prompt": s["prompt"], "depends_on": s.get("depends_on", [])}
             for s in _THREE["steps"]]
    reply = json.loads(server._call(_AppendingApi(), "swarm_workflow", {"steps": steps}))
    assert [s["step_id"] for s in reply["steps"] if s["added"]] == ["re-review", "merge"]


# --------------------------------------------------------------------------
# 2. run.js accepts the server's additions and starts their rows
# --------------------------------------------------------------------------


def test_the_measured_three_step_spec_with_five_replied_steps_starts_five_rows(tmp_path):
    got = _run(tmp_path, _THREE, _answers(_reply(_THREE, _ADDED)))

    assert "error" not in got, got.get("error")
    assert got["result"]["state"] == "SUCCEEDED", got["result"].get("error")
    assert _rows(got) == ["implement", "review", "fix", "re-review", "merge"], _rows(got)
    assert {row["step_id"] for row in got["result"]["steps"]} == {"implement", "review", "fix", "re-review", "merge"}
    assert "wf_d5 · SwarmCloud added re-review, merge" in got["logs"], got["logs"]


def test_unmarked_extra_steps_that_depend_on_spec_steps_are_accepted(tmp_path):
    """A bridge older than the mark (no `added` field): a step that hangs off
    the spec's own steps is the server's addition, not a relay's invention."""
    got = _run(tmp_path, _THREE, _answers(_reply(_THREE, _ADDED, mark=None)))

    assert "error" not in got, got.get("error")
    assert _rows(got) == ["implement", "review", "fix", "re-review", "merge"], _rows(got)


def test_a_marked_step_whose_dependencies_the_script_did_not_predict_is_accepted(tmp_path):
    """The prediction is not the gate any more: a merge on `fix` alone is
    what the server built, so it is what the rows follow."""
    merge = {"step_id": "merge", "depends_on": ["fix"]}
    got = _run(tmp_path, _THREE, _answers(_reply(_THREE, [merge])))

    assert "error" not in got, got.get("error")
    assert _rows(got) == ["implement", "review", "fix", "merge"], _rows(got)


# --------------------------------------------------------------------------
# 3. ...and still refuses a reply that lost or renamed a spec step
# --------------------------------------------------------------------------


def test_a_reply_that_drops_a_spec_step_is_still_refused(tmp_path):
    error = _refused(_run(tmp_path, _THREE, _answers(_reply(_THREE, _ADDED, drop="review"))))
    assert "step review is in the spec and missing from the reply" in error, error


def test_a_reply_that_renames_a_spec_step_is_still_refused_even_marked_added(tmp_path):
    error = _refused(_run(tmp_path, _THREE, _answers(_reply(_THREE, _ADDED, rename=("review", "reviewed")))))
    assert "step review is in the spec and missing from the reply" in error, error


def test_an_unmarked_extra_step_tied_to_no_spec_step_is_refused(tmp_path):
    loose = [{"step_id": "deploy", "depends_on": []}, {"step_id": "after", "depends_on": ["nowhere"]}]
    error = _refused(_run(tmp_path, _THREE, _answers(_reply(_THREE, loose, mark=None))))
    assert "the reply names a step the spec does not have: deploy" in error, error
    assert "the reply names a step the spec does not have: after" in error, error


def test_an_extra_step_the_bridge_marked_not_added_is_refused(tmp_path):
    """`added: false` on a step the spec does not have is the relay and the
    bridge disagreeing: the bridge compared against the spec it submitted."""
    error = _refused(_run(tmp_path, _THREE, _answers(_reply(_THREE, _ADDED, mark=False))))
    assert "the reply names a step the spec does not have: re-review" in error, error


# --------------------------------------------------------------------------
# 4. A step row's schema takes any answer; a row with none reads the status
# --------------------------------------------------------------------------


@pytest.mark.parametrize("answer", [
    {},
    {"result": None},
    {"state": None, "result": {"state": None}},
    {"result": {"answer_excerpt": None, "pr_url": None}},
], ids=["empty", "null-result", "null-state", "no-state"])
def test_the_step_schema_takes_an_answer_with_any_field_missing_or_null(answer):
    assert _errors(_schema("STEP_RESULT"), answer) == []


@pytest.mark.parametrize("failure", [
    "agent({schema}): StructuredOutput retry cap (5) exceeded: root: must have required property 'state'",
    "subagent completed without calling StructuredOutput",
], ids=["retry-cap", "no-structured-output"])
def test_a_row_that_throws_takes_its_state_from_a_status_read_not_an_error_row(tmp_path, failure):
    answers = {**_ANSWERS, "step:join": {"__throw": failure},
               "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None,
                          "steps": [{"step_id": "join", "state": "SUCCEEDED"}]}}

    got = _run(tmp_path, _SPEC, answers)

    assert "error" not in got, got.get("error")
    join = {row["step_id"]: row for row in got["result"]["steps"]}["join"]
    assert join["state"] == "SUCCEEDED" and join["state_from"] == "workflow status", join
    assert "row_error" not in join, join
    assert failure[:40] in join["row_note"], join
    # The read is made for the row, at once, under a label that names it.
    labels = [c["label"] for c in got["calls"] if c["prompt"].startswith("STATUS\n")]
    assert "[SwarmCloud] scan · join · setup · status" in labels, labels
    assert any(line.startswith("join SUCCEEDED (from the workflow status; its row could not answer: ")
               for line in got["logs"]), got["logs"]


def test_a_row_that_answers_with_no_state_reads_the_status_too(tmp_path):
    answers = {**_ANSWERS, "step:report": {"result": {"answer_excerpt": None}},
               "STATUS": {"state": "FAILED", "state_note": None, "console": None,
                          "steps": [{"step_id": "report", "state": "SUCCEEDED"}]}}

    got = _run(tmp_path, _SPEC, answers)

    report = {row["step_id"]: row for row in got["result"]["steps"]}["report"]
    assert report["state"] == "SUCCEEDED" and report["state_from"] == "workflow status", report
    assert "row_error" not in report, report


def test_a_row_whose_status_read_names_no_state_stays_an_error_row(tmp_path):
    """The control: nothing here guesses a state. The read named no `join`."""
    got = _run(tmp_path, _SPEC, _ANSWERS)
    join = {row["step_id"]: row for row in got["result"]["steps"]}["join"]
    assert join["state"] is None and "validation" in join["row_error"], join


def test_the_step_agent_spells_out_the_exact_object_and_allows_null():
    _, body = _load(_PLUGIN / "agents" / "step.md")
    flat = " ".join(body.split())
    assert "## The exact object" in body
    assert '{"state": "<state>", "result": {' in flat
    assert "null is accepted for any field" in flat
    assert "never end without calling `StructuredOutput`" in flat


def test_the_workflow_agent_copies_the_added_mark():
    _, body = _load(_PLUGIN / "agents" / "workflow.md")
    flat = " ".join(body.split())
    assert "its `added` (null when that entry has none)" in flat
    assert "never drop a step because the spec did not name it" in flat
