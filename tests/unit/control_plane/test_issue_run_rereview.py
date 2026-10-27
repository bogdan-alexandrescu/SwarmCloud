"""An issue run's compiled workflow re-reviews the fix's head, and submission accepts it (#2217).

wf_3cc4026eb34a43a885a4 (PR #2147): the review said NOT_YET, the fix
resolved the one finding, and the run's `auto_merge` failed "the review's
verdict is NOT_YET, not MERGE" -- the compiled workflow was implement ->
review -> fix, and nothing judged the fix. What these tests hold:

  1. BOTH COMPILE PATHS APPEND THE RE-REVIEW `metadata.merge`
     "on_merge_verdict" appends (`validation.rereview_step_for`), after the
     fix: gated on the review's NOT_YET like the fix, building on the fix,
     staging the review's verdict by parent, on the review's own profile and
     prompt behind `REREVIEW_PREAMBLE`. The fix stays the integrator.
  2. SUBMISSION ACCEPTS EXACTLY THAT STEP under `metadata.merge` "off" (the
     CI loop merges an issue run), and still refuses a caller's own gated
     non-integrator, even one that takes the re-review's id.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import pytest

from swarm_api import issueruns
from swarm_api.issueruns import compile_plan
from swarm_api.service import SubmissionService
from swarm_api.validation import (
    INPUT_LAYOUT_BY_PARENT,
    INPUT_LAYOUT_METADATA_KEY,
    REREVIEW_PREAMBLE,
    REREVIEW_STEP_ID,
    StepSpec,
    resolve_integrator_step,
    rereview_step_for,
    validate_step_routing,
)

from .test_issue_run_keyword import STAGED_FULL_PLAN
from .test_issue_runs import FULL_PLAN
from .test_issue_writeback import _bare_run
from .test_merge_verdict_workflows import _by_id, _post, _review_shape, _task


def _compiled(plan: dict):
    return compile_plan(_bare_run(plan=plan, plan_digest=issueruns.plan_digest(plan)))


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_a_chain_and_a_staged_plan_compile_a_gated_rereview_after_the_fix(plan):
    workflow = _compiled(plan)
    ids = [s.step_id for s in workflow.steps]
    assert issueruns.REREVIEW_STEP == REREVIEW_STEP_ID
    assert ids[-3:] == [issueruns.REVIEW_STEP, issueruns.FIX_STEP, issueruns.REREVIEW_STEP]
    assert ids.count(issueruns.REREVIEW_STEP) == 1
    by_id = {s.step_id: s for s in workflow.steps}
    review, rereview = by_id[issueruns.REVIEW_STEP], by_id[issueruns.REREVIEW_STEP]

    assert rereview.when is not None
    assert rereview.when.step == issueruns.REVIEW_STEP
    assert list(rereview.when.verdict_in) == ["NOT_YET"]
    assert rereview.builds_on == issueruns.FIX_STEP
    assert rereview.input_from[issueruns.REVIEW_STEP] == issueruns.VERDICT_FILE
    assert {**review.input_from, issueruns.REVIEW_STEP: issueruns.VERDICT_FILE} == dict(
        rereview.input_from
    )
    assert rereview.metadata[INPUT_LAYOUT_METADATA_KEY] == INPUT_LAYOUT_BY_PARENT
    assert issueruns.FIX_STEP in rereview.depends_on
    assert issueruns.REVIEW_STEP in rereview.depends_on
    # The review's own profile, by name (invariant 10), and its prompt.
    assert rereview.runner_profile == review.runner_profile
    preamble = REREVIEW_PREAMBLE.split("{", 1)[0]
    assert rereview.input["prompt"].startswith(preamble)
    assert rereview.input["prompt"].endswith(review.input["prompt"])

    # Exactly the step `rereview_step_for` derives from the other steps.
    specs = SubmissionService._step_specs(workflow)
    rest = [s for s in specs if s.step_id != issueruns.REREVIEW_STEP]
    derived = rereview_step_for(rest, review.model_dump(), "integrate")
    assert derived is not None
    assert type(rereview).model_validate(derived).model_dump() == rereview.model_dump()

    # The fix is still the integrator, so the re-review never publishes.
    assert resolve_integrator_step(specs, rereview=issueruns.REREVIEW_STEP) == issueruns.FIX_STEP
    # One fix round: the re-review judges it and starts no second fix.
    assert workflow.metadata["issue_run"]["review_rounds_compiled"] == 1
    assert workflow.metadata["merge"] == "off"


def test_a_plan_step_cannot_take_the_rereviews_id():
    plan = {**FULL_PLAN, "steps": [{**FULL_PLAN["steps"][0], "step_id": REREVIEW_STEP_ID}]}
    with pytest.raises(issueruns.InvalidPlan):
        issueruns.parse_plan(plan)


def _off_with_rereview(**changes) -> dict:
    """`_review_shape` under merge "off", with the re-review stated as the
    issue-run compile states it, then `changes` applied to that step."""
    spec = _review_shape(metadata={"merge": "off"})
    specs = [
        StepSpec(s["step_id"], tuple(s.get("depends_on") or ()),
                 input_from=dict(s.get("input_from") or {}),
                 when_step=(s.get("when") or {}).get("step"),
                 when_verdicts=tuple((s.get("when") or {}).get("verdict_in") or ()),
                 builds_on=s.get("builds_on"), runner_profile=s.get("runner_profile"))
        for s in spec["steps"]
    ]
    review = next(s for s in spec["steps"] if s["step_id"] == "review")
    rereview = rereview_step_for(specs, review, "integrate")
    assert rereview is not None
    rereview.update(changes)
    spec["steps"].append(rereview)
    return spec


def test_the_compiled_rereview_is_accepted_under_merge_off_and_a_callers_own_gated_step_is_not(
    client, db,
):
    # The issue run's own compile, recognised by the service.
    workflow = _compiled(FULL_PLAN)
    assert SubmissionService._stated_rereview(
        workflow, SubmissionService._step_specs(workflow), "integrate",
    ) == issueruns.REREVIEW_STEP
    specs = SubmissionService._step_specs(workflow)
    validate_step_routing(specs, strategy="integrate", integrator_step_id=issueruns.FIX_STEP,
                          rereview=issueruns.REREVIEW_STEP)

    # Over the API: accepted, gated on the review, not the publisher, no merge.
    response = _post(client, _off_with_rereview())
    assert response.status_code == 201, response.text
    steps = _by_id(response.json())
    assert list(steps) == ["implement", "review", "fix", "re-review"]
    block = _task(db, steps["re-review"]["task_id"])["metadata"]["dispatch"]
    assert block["verdict_gate"] == {"task_id": steps["review"]["task_id"],
                                     "verdict_in": ["NOT_YET"]}
    assert block["role"] == "contributor"
    assert "pr_label" not in block and "findings_epic" not in block

    # Anything looser is a caller's own gated non-integrator: refused. Which
    # gated step the refusal names depends on which one is then the sink.
    for changes in (
        {"builds_on": "implement"},                          # not the fixed head
        {"input": {"prompt": "review again"}},               # not the review's prompt
        {"when": {"step": "review", "verdict_in": ["MERGE", "NOT_YET"]}},  # not the fix's gate
        {"input_from": {"review": "verdict.json"}},          # not what the review staged
        {"step_id": "second-look"},                          # not the re-review's id
    ):
        refused = _post(client, _off_with_rereview(**changes))
        assert refused.status_code == 422, (changes, refused.text)
        detail = refused.json()["detail"]
        assert detail["step_id"] in ("fix", "re-review", "second-look"), changes
        assert "integrator_step_id" in detail, changes

    # And the same step under the platform's own merge choice is not stated:
    # "on_merge_verdict" appends its own; an explicit "on" leaves it refused.
    on = _off_with_rereview()
    on["metadata"] = {"merge": "on"}
    assert _post(client, on).status_code == 422
