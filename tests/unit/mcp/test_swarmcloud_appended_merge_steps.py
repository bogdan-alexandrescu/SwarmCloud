"""`/sc:swarmcloud` accepts the merge steps SwarmCloud appends, and starts their rows.

Measured 2026-10-11: a spec with `"merge": "on_merge_verdict"` (workflow
wf_40dbd4392f184080ab49) came back SUBMITTED_UNVERIFIED -- "the reply names a
step the spec does not have: re-review; ... merge" -- and no row started,
although swarm-api had appended exactly the steps it documents
(`swarm_api.service._with_merge_step`). The workflow ran and merged PR 1081
with no rows.

What these tests hold, all against run.js under node:

  1. "on_merge_verdict" accepts the appended `re-review` and `merge`, with the
     `depends_on` swarm-api's own `rereview_step_for` and `merge_step_for`
     build -- the reply here is made by those functions, so run.js is held to
     the server, not to a copy -- and starts a row for each.
  2. A spec that names no `merge`, on a repository whose `merge_policy`
     merges, is accepted the same way -- given as an object, and given as a
     file whose outline the bridge's own `_outline` made.
  3. Since SPEED-D5 (owner decision 2026-10-14) the shapes above name the
     reason in the log and gate nothing: an extra step the prediction did not
     expect is accepted when it hangs off the spec's steps (or the bridge
     marked it added -- test_server_added_steps_and_tolerant_rows.py). An
     extra step tied to no spec step, and an explicit choice whose reply
     names no step beyond the spec at all, are still SUBMITTED_UNVERIFIED
     with no row started.
"""

from __future__ import annotations

import json

from swarm_api.validation import StepSpec, merge_step_for, rereview_step_for
from swarm_mcp import server, workflows

from test_plugin_agents_and_workflows import _bridge_digest, _run, _step

_REVIEW_SHAPE = {
    "label": "ship",
    "strategy": "integrate",
    "repository_url": "https://github.com/acme/widgets.git",
    "steps": [
        {"step_id": "implement", "prompt": "implement the change"},
        {"step_id": "review", "prompt": "review; write verdict.json", "depends_on": ["implement"],
         "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"}},
        {"step_id": "fix", "prompt": "fix every finding", "depends_on": ["review"],
         "builds_on": "implement", "input_from": {"review": "verdict.json"},
         "when": {"step": "review", "verdict_in": ["NOT_YET"]}},
    ],
}


def _spec(**top) -> dict:
    spec = json.loads(json.dumps(_REVIEW_SHAPE))
    spec.update(top)
    return {k: v for k, v in spec.items() if v is not None}


def _server_appended(spec: dict) -> list[dict]:
    """The re-review and merge swarm-api appends under "on_merge_verdict",
    built by its own functions (`service._with_verdict_merge`'s calls)."""
    def specs(steps):
        return [StepSpec(
            step_id=s["step_id"], depends_on=tuple(s.get("depends_on") or ()),
            input_from=dict(s.get("input_from") or {}),
            when_step=(s.get("when") or {}).get("step"),
            when_verdicts=tuple((s.get("when") or {}).get("verdict_in") or ()),
            builds_on=s.get("builds_on"), runner_profile=s.get("runner_profile", "claude-code"),
        ) for s in steps]

    steps = [{**s, "runner_profile": s.get("runner_profile", "claude-code"),
              "input": {"prompt": s["prompt"]}} for s in spec["steps"]]
    review = next(s for s in steps if s["step_id"] == "review")
    rereview = rereview_step_for(specs(steps), review, spec["strategy"])
    merge = merge_step_for(specs([*steps, rereview]), spec["strategy"], rereview=rereview["step_id"])
    assert rereview is not None and merge is not None
    return [rereview, merge]


def _reply(spec: dict, appended: list[dict]) -> dict:
    steps = [*spec["steps"], *appended]
    return {
        "workflow_id": "wf_m", "console": None, "repository": spec.get("repository_url"),
        "repository_notes": [], "spec_digest": _bridge_digest(spec),
        "bridge_version": None, "follow_error": None, "error": None,
        "steps": [{"step_id": s["step_id"], "task_id": "task_" + s["step_id"],
                   "depends_on": list(s.get("depends_on") or []), "console": None}
                  for s in reversed(steps)],
    }


def _answers(reply: dict) -> dict:
    answers = {"SUBMIT": reply, "STATUS": {"state": "SUCCEEDED", "state_note": None, "steps": []}}
    for step in reply["steps"]:
        answers["step:" + step["step_id"]] = _step("SUCCEEDED", duration_s=30)
    return answers


def _rows(got: dict) -> list[str]:
    return [c["label"].rsplit(" · ", 1)[-1] for c in got["calls"] if c["agentType"] == "sc:step"]


def _accepted(got: dict) -> None:
    assert "error" not in got, got.get("error")
    assert got["result"]["state"] == "SUCCEEDED", got["result"].get("error")
    # A row per step, the appended two included and last, as the console counts.
    assert _rows(got) == ["implement", "review", "fix", "re-review", "merge"], _rows(got)
    assert {row["step_id"] for row in got["result"]["steps"]} == {
        "implement", "review", "fix", "re-review", "merge"}
    by_label = {c["label"].rsplit(" · ", 1)[-1]: c["prompt"] for c in got["calls"] if c["agentType"] == "sc:step"}
    assert "depends_on: fix, re-review" in by_label["merge"], by_label["merge"]
    assert "task_id: task_re-review" in by_label["re-review"]


def _refused(got: dict) -> str:
    assert "error" not in got, got.get("error")
    assert [c["agentType"] for c in got["calls"]] == ["sc:workflow"], "a row started on an unverified reply"
    assert got["result"]["state"] == "SUBMITTED_UNVERIFIED"
    return got["result"]["error"]


def test_the_server_builds_the_shape_run_js_accepts():
    """The control: the reply below is the server's, and it is the shape the
    owner's measurement saw -- a re-review on fix, review and implement, and a
    merge on fix and the re-review."""
    rereview, merge = _server_appended(_spec(merge="on_merge_verdict"))
    assert (rereview["step_id"], sorted(rereview["depends_on"])) == ("re-review", ["fix", "implement", "review"])
    assert (merge["step_id"], merge["depends_on"]) == ("merge", ["fix", "re-review"])


def test_merge_on_merge_verdict_accepts_the_appended_steps_and_starts_their_rows(tmp_path):
    spec = _spec(merge="on_merge_verdict")
    got = _run(tmp_path, spec, _answers(_reply(spec, _server_appended(spec))))
    _accepted(got)
    assert "wf_m · SwarmCloud appended re-review, merge for the spec's merge on_merge_verdict" in got["logs"], got["logs"]
    assert "wf_m submitted · 5 step(s) · clones https://github.com/acme/widgets.git" in got["logs"]


def test_a_spec_without_merge_on_a_repo_whose_policy_merges_is_accepted_the_same_way(tmp_path):
    spec = _spec()
    assert "merge" not in spec
    got = _run(tmp_path, spec, _answers(_reply(spec, _server_appended(_spec(merge="on_merge_verdict")))))
    _accepted(got)
    assert any("for the repository's merge_policy on_merge_verdict" in line for line in got["logs"]), got["logs"]


def test_a_spec_file_without_merge_is_checked_against_the_bridges_outline(tmp_path):
    """Given a path, the script sees only the outline the bridge hands back:
    `server._outline` itself is what is relayed here, so the two stay in step."""
    spec = _spec()
    read = {"path": "/work/widgets/specs/ship.json", "spec_ref": "spec_00112233aabbccdd",
            "spec_digest": _bridge_digest(spec), "outline": server._outline(spec), "error": None}
    answers = {**_answers(_reply(spec, _server_appended(_spec(merge="on_merge_verdict")))), "READ SPEC": read}
    got = _run(tmp_path, "specs/ship.json", answers)
    _accepted(got)


def test_a_spec_file_with_merge_on_merge_verdict_carries_it_in_the_outline(tmp_path):
    spec = _spec(merge="on_merge_verdict")
    outline = server._outline(spec)
    assert (outline["strategy"], outline["merge"]) == ("integrate", "on_merge_verdict")
    read = {"path": "/work/widgets/specs/ship.json", "spec_ref": "spec_00112233aabbccdd",
            "spec_digest": _bridge_digest(spec), "outline": outline, "error": None}
    got = _run(tmp_path, "specs/ship.json", {**_answers(_reply(spec, _server_appended(spec))), "READ SPEC": read})
    _accepted(got)


def test_a_spec_without_merge_accepts_the_platform_defaults_lone_merge_step(tmp_path):
    """merge_by_default means "on": one merge, on the integrator and its review."""
    spec = _spec()
    merge = {"step_id": "merge", "runner_profile": "merge", "depends_on": ["fix", "review"]}
    got = _run(tmp_path, spec, _answers(_reply(spec, [merge])))
    assert "error" not in got, got.get("error")
    assert _rows(got) == ["implement", "review", "fix", "merge"], _rows(got)


def test_an_extra_step_tied_to_no_spec_step_is_still_submitted_unverified(tmp_path):
    spec = _spec(merge="on_merge_verdict")
    deploy = {"step_id": "deploy", "depends_on": []}
    error = _refused(_run(tmp_path, spec, _answers(_reply(spec, [*_server_appended(spec), deploy]))))
    assert "the reply names a step the spec does not have: deploy" in error, error

    # The same extra on a spec that says nothing about merging.
    plain = _spec()
    error = _refused(_run(tmp_path, plain, _answers(_reply(plain, [{"step_id": "deploy", "depends_on": ["gone"]}]))))
    assert "the reply names a step the spec does not have: deploy" in error, error


def test_an_unpredicted_extra_step_on_the_specs_steps_starts_its_row(tmp_path):
    """SPEED-D5: the prediction is not the gate. A `deploy` on `fix` the
    script never expected is the server's, and the rows follow it."""
    spec = _spec(merge="on_merge_verdict")
    deploy = {"step_id": "deploy", "depends_on": ["fix"]}
    got = _run(tmp_path, spec, _answers(_reply(spec, [*_server_appended(spec), deploy])))
    assert "error" not in got, got.get("error")
    assert "deploy" in _rows(got) and "merge" in _rows(got), _rows(got)


def test_appended_steps_the_script_did_not_predict_start_their_rows(tmp_path):
    """`collect` and merge "off" predict nothing appended; what the server
    appended anyway is what runs, so it is what the rows show (SPEED-D5: the
    measured 3-step spec came back with 5 and no row started)."""
    for spec in (_spec(strategy="collect"), _spec(merge="off")):
        got = _run(tmp_path, spec, _answers(_reply(spec, _server_appended(_spec(merge="on_merge_verdict")))))
        assert "error" not in got, got.get("error")
        assert got["result"]["state"] == "SUCCEEDED", got["result"].get("error")
        assert _rows(got) == ["implement", "review", "fix", "re-review", "merge"], _rows(got)
        assert "wf_m · SwarmCloud added merge, re-review" in got["logs"], got["logs"]


def test_an_appended_step_with_another_dependency_starts_its_row(tmp_path):
    """The rows follow the dependencies SwarmCloud served, not the script's copy."""
    spec = _spec(merge="on_merge_verdict")
    rereview, merge = _server_appended(spec)
    got = _run(tmp_path, spec, _answers(_reply(spec, [rereview, {**merge, "depends_on": ["fix"]}])))
    assert "error" not in got, got.get("error")
    by_label = {c["label"].rsplit(" · ", 1)[-1]: c["prompt"] for c in got["calls"] if c["agentType"] == "sc:step"}
    assert "depends_on: fix\n" in by_label["merge"], by_label["merge"]


def test_an_explicit_choice_whose_steps_are_missing_is_refused(tmp_path):
    spec = _spec(merge="on_merge_verdict")
    error = _refused(_run(tmp_path, spec, _answers(_reply(spec, []))))
    assert ("the reply names no step beyond the spec, and SwarmCloud appends re-review, merge for the spec's merge "
            "on_merge_verdict") in error, error

    # Any step the server added satisfies it: the shape is the server's to decide.
    rereview, _ = _server_appended(spec)
    got = _run(tmp_path, spec, _answers(_reply(spec, [rereview])))
    assert "error" not in got and got["result"]["state"] == "SUCCEEDED", got.get("error") or got["result"]


def test_the_digest_covers_the_merge_choice():
    """The control for the outline: the merge key is part of what the bridge
    digests, so a relay that dropped it is refused before anything else."""
    assert workflows.spec_digest(_spec()) != workflows.spec_digest(_spec(merge="on_merge_verdict"))
