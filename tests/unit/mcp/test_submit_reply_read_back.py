"""A thin submit reply is checked by reading the workflow back, never by leaving it unwatched (#2206).

MEASURED, 2026-10-24, sc plugin 0.5.49 bridge, AI Studio session (tenant
eng): a one-step `claude-code` spec launched through /sc:swarmcloud came back
SUBMITTED_UNVERIFIED for wf_effa3d00346b40d1a1d3 -- "the reply carries no spec
digest ...; step build is in the spec and missing from the reply" -- and no
row was started. The workflow was RUNNING with step `build` and later
SUCCEEDED. One of seven launches of the same shape.

Where the digest and steps were lost: NOT in swarm-api, and not in the
bridge. `POST /v1/workflows` serves every step it created (there is no
idempotent replay: each call creates a workflow), and `swarm_workflow` adds
`spec_digest` -- its own fnv1a32 of the spec it received, which the API never
stores, so no stored workflow has the field -- to every spec submission. The
reply run.js reads is the one the `sc:workflow` relay copies into its
StructuredOutput, and that copy came back thin.

What these tests hold:

  * a reply with no digest, or missing a spec step, is followed by ONE
    read-back (`ATTACH`, which is `swarm_workflow_status`); when the steps it
    shows are the spec's (ids, dependencies and the profiles the spec names),
    the workflow is reported submitted and its rows start, with a note that
    the digest was not served;
  * a read-back showing different steps is SUBMITTED_UNVERIFIED, and rows
    still start for every step it shows with a task, so nothing runs unseen;
  * a read-back that fails is SUBMITTED_UNVERIFIED with the read's error;
  * a reply with its digest and every step makes no read-back at all;
  * the two sources hold their half: the API's create reply names every
    step, and the bridge's reply carries the digest of the spec it received.
"""

from __future__ import annotations

import json

from swarm_mcp import server

from test_plugin_agents_and_workflows import _bridge_digest, _run, _step
from test_server_added_steps_and_tolerant_rows import _AppendingApi

# The measured shape: one step, a named profile.
_ONE = {
    "label": "build",
    "steps": [{"step_id": "build", "runner_profile": "claude-code", "prompt": "build it"}],
}

_WF = "wf_effa3d00346b40d1a1d3"
_TASK = "task_57f2c0804bb34030a096"


def _thin(**fields) -> dict:
    """The submit row's answer as measured: a workflow id, no digest, no steps."""
    reply = {
        "workflow_id": _WF, "console": None, "repository": "https://github.com/acme/widgets.git",
        "repository_notes": [], "spec_digest": None, "bridge_version": "0.5.49",
        "follow_error": None, "error": None, "steps": [],
    }
    reply.update(fields)
    return reply


def _read_back(steps: list[dict] | None = None, **fields) -> dict:
    """The ATTACH row's answer: swarm_workflow_status as the relay copies it."""
    answer = {
        "workflow_id": _WF, "workspace": "eng", "title": None, "label": "build", "console": None,
        "state": "RUNNING", "state_note": None, "bridge_version": "0.5.49", "follow_error": None,
        "error": None,
        "steps": steps if steps is not None else [
            {"step_id": "build", "task_id": _TASK, "depends_on": [], "state": "RUNNING",
             "runner_profile": "claude-code", "console": None},
        ],
    }
    answer.update(fields)
    return answer


def _answers(submit: dict, read_back) -> dict:
    return {
        "SUBMIT": submit,
        "ATTACH": read_back,
        "step:build": _step("SUCCEEDED", duration_s=30),
        "step:built": _step("SUCCEEDED", duration_s=30),
        "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []},
    }


def _rows(got: dict) -> list[str]:
    return [c["label"].rsplit(" · ", 1)[-1] for c in got["calls"] if c["agentType"] == "sc:step"]


def _reads(got: dict) -> list[str]:
    return [c["prompt"] for c in got["calls"] if c["prompt"].split("\n")[0] == "ATTACH"]


# --------------------------------------------------------------------------
# 1. A thin reply whose read-back matches is submitted, with rows
# --------------------------------------------------------------------------


def test_a_reply_without_digest_or_steps_whose_read_back_matches_is_submitted_with_rows(tmp_path):
    got = _run(tmp_path, _ONE, _answers(_thin(), _read_back()))

    assert "error" not in got, got.get("error")
    result = got["result"]
    assert result["state"] == "SUCCEEDED", result.get("error")
    assert _reads(got) == ["ATTACH\nworkflow_id: " + _WF], "exactly one read-back of the workflow submitted"
    assert _rows(got) == ["build"], _rows(got)
    row = next(c for c in got["calls"] if c["agentType"] == "sc:step")
    assert "task_id: " + _TASK in row["prompt"], "the row follows the task the read-back names"
    # It says how the submission was verified, and that the digest was not served.
    assert result["verified_by"] == "read_back", result
    assert "no spec digest" in result["verify_note"], result["verify_note"]
    assert any(_WF + " · the reply carried no spec digest" in line for line in got["logs"]), got["logs"]
    assert any(_WF + " submitted · 1 step(s)" in line for line in got["logs"]), got["logs"]


def test_a_reply_with_every_step_but_no_digest_is_read_back_too(tmp_path):
    """The steps alone show nothing about the spec SwarmCloud received; the
    server's own read of them does."""
    replied = [{"step_id": "build", "task_id": _TASK, "depends_on": [], "console": None}]
    got = _run(tmp_path, _ONE, _answers(_thin(steps=replied), _read_back()))

    assert "error" not in got, got.get("error")
    assert len(_reads(got)) == 1
    assert got["result"]["state"] == "SUCCEEDED", got["result"].get("error")
    assert _rows(got) == ["build"]


def test_a_spec_step_that_names_no_profile_matches_whatever_the_server_defaulted(tmp_path):
    spec = {"label": "build", "steps": [{"step_id": "build", "prompt": "build it"}]}
    got = _run(tmp_path, spec, _answers(_thin(), _read_back()))

    assert got["result"]["state"] == "SUCCEEDED", got["result"].get("error")
    assert _rows(got) == ["build"]


def test_a_complete_reply_makes_no_read_back(tmp_path):
    """The control: the read-back exists for a thin reply only."""
    full = _thin(spec_digest=_bridge_digest(_ONE),
                 steps=[{"step_id": "build", "task_id": _TASK, "depends_on": [], "console": None}])
    got = _run(tmp_path, _ONE, _answers(full, {"__throw": "a complete reply must not be read back"}))

    assert "error" not in got, got.get("error")
    assert _reads(got) == []
    assert got["result"]["state"] == "SUCCEEDED", got["result"].get("error")
    assert "verified_by" not in got["result"]


# --------------------------------------------------------------------------
# 2. A read-back with different steps is unverified, and its rows still start
# --------------------------------------------------------------------------


def test_a_read_back_with_a_renamed_step_is_unverified_and_its_rows_start(tmp_path):
    renamed = [{"step_id": "built", "task_id": _TASK, "depends_on": [], "state": "RUNNING",
                "runner_profile": "claude-code", "console": None}]
    got = _run(tmp_path, _ONE, _answers(_thin(), _read_back(renamed)))

    assert "error" not in got, got.get("error")
    result = got["result"]
    assert result["state"] == "SUBMITTED_UNVERIFIED", result
    assert "step build is in the spec and missing from the read-back" in result["error"], result["error"]
    assert "swarm_workflow_cancel" in result["error"]
    # Nothing runs unseen: the step SwarmCloud shows gets its row.
    assert _rows(got) == ["built"], _rows(got)
    assert {row["step_id"] for row in result["steps"]} == {"built"}
    assert result["workflow_state"] == "SUCCEEDED", "the derived state is still read and handed back"
    assert "arm_watch" in result["arm_watch"]


def test_a_read_back_with_another_profile_is_unverified_and_its_rows_start(tmp_path):
    other = [{"step_id": "build", "task_id": _TASK, "depends_on": [], "state": "RUNNING",
              "runner_profile": "codex", "console": None}]
    got = _run(tmp_path, _ONE, _answers(_thin(), _read_back(other)))

    result = got["result"]
    assert result["state"] == "SUBMITTED_UNVERIFIED", result
    assert "step build runs on codex in the read-back and on claude-code in the spec" in result["error"], result["error"]
    assert _rows(got) == ["build"], _rows(got)


def test_a_read_back_with_a_step_the_spec_does_not_have_is_unverified(tmp_path):
    extra = _read_back()["steps"] + [{"step_id": "deploy", "task_id": "task_x", "depends_on": [],
                                      "state": "QUEUED", "runner_profile": "claude-code", "console": None}]
    answers = _answers(_thin(), _read_back(extra))
    answers["step:deploy"] = _step("SUCCEEDED", duration_s=1)
    got = _run(tmp_path, _ONE, answers)

    result = got["result"]
    assert result["state"] == "SUBMITTED_UNVERIFIED", result
    assert "the read-back names a step the spec does not have: deploy" in result["error"], result["error"]
    assert sorted(_rows(got)) == ["build", "deploy"], _rows(got)


def test_a_read_back_whose_probe_is_refused_starts_no_row(tmp_path):
    """The probe the attach read makes still decides whether a row can follow."""
    got = _run(tmp_path, _ONE, _answers(_thin(), _read_back(follow_error="unknown format: progress")))

    assert got["result"]["state"] == "FOLLOW_REFUSED", got["result"]
    assert _rows(got) == []


# --------------------------------------------------------------------------
# 3. A read-back that fails is unverified with the read's error
# --------------------------------------------------------------------------


def test_a_read_back_that_throws_is_unverified_with_the_reads_error(tmp_path):
    got = _run(tmp_path, _ONE, _answers(_thin(), {"__throw": "StructuredOutput retry cap (5) exceeded"}))

    assert "error" not in got, got.get("error")
    result = got["result"]
    assert result["state"] == "SUBMITTED_UNVERIFIED", result
    assert result["workflow_id"] == _WF
    assert "StructuredOutput retry cap (5) exceeded" in result["error"], result["error"]
    assert "the reply carries no spec digest" in result["error"], "the reply's own problems are still named"
    assert _rows(got) == [], "with no read-back there is no step to start a row for"
    assert len(_reads(got)) == 1, "read back once, never retried"


def test_a_read_back_answered_with_an_error_is_unverified_with_that_error(tmp_path):
    refused = _read_back(steps=[], error="workflow wf_effa3d00346b40d1a1d3 was not found")
    got = _run(tmp_path, _ONE, _answers(_thin(), refused))

    result = got["result"]
    assert result["state"] == "SUBMITTED_UNVERIFIED", result
    assert "workflow wf_effa3d00346b40d1a1d3 was not found" in result["error"], result["error"]
    assert _rows(got) == []


# --------------------------------------------------------------------------
# 4. Each source holds its half
# --------------------------------------------------------------------------


def test_the_bridges_reply_carries_the_digest_and_every_step_of_a_new_spec():
    spec = {"label": "build", "steps": [
        {"step_id": "build", "runner_profile": "claude-code", "prompt": "build it"},
        {"step_id": "check", "runner_profile": "claude-code", "prompt": "check it", "depends_on": ["build"]},
    ]}
    reply = json.loads(server._call(_AppendingApi(), "swarm_workflow",
                                    {"spec": spec, "spec_digest": _bridge_digest(spec)}))
    assert reply["spec_digest"] == _bridge_digest(spec)
    assert reply["spec_digest_checked"] is True
    given = [s["step_id"] for s in reply["steps"] if not s["added"]]
    assert given == ["build", "check"], reply["steps"]
    assert all(s["task_id"] and s["runner_profile"] == "claude-code" for s in reply["steps"])


def test_the_agent_relays_the_read_backs_profiles():
    """The ATTACH relay copies each step's runner_profile, which the
    read-back compares; the schema takes it, and the agent is told to."""
    from test_plugin_agents_and_workflows import _PLUGIN

    agent = (_PLUGIN / "agents" / "workflow.md").read_text()
    attach = agent.split("## ATTACH", 1)[1].split("## LIST", 1)[0]
    assert "`runner_profile`" in attach, attach
    source = (_PLUGIN / "workflows" / "run.js").read_text()
    attached = source.split("const ATTACHED = {", 1)[1].split("\n}\n", 1)[0]
    assert "runner_profile:" in attached, attached
