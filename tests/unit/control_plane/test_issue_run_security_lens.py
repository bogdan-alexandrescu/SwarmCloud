"""One review round that covers security: the security lens BESIDE the first review.

When the approved plan's files touch a risk path (the review gate's own
list, `reviewgate.DEFAULT_RISK_PATHS`), the compiled workflow gains one
step, `security-lens`, depending on the same build steps as the review and
running beside it. Each writes its own verdict file; the one fix stages
both and its gate reads both (`when.also`), so its agent runs when EITHER
says NOT_YET; the re-review covers both lenses and is the verdict the merge
reads. A plan that touches no risk path compiles exactly as before. No
cloud, no emulator.
"""

from __future__ import annotations

import copy

import pytest

from swarm_api import issueruns, validation
from swarm_api.issueruns import compile_plan
from swarm_api.schemas import WorkflowStepCreate
from swarm_api.validation import DagError, REREVIEW_STEP_ID, SECURITY_VERDICT_FILE, StepSpec

from .test_issue_run_keyword import STAGED_FULL_PLAN
from .test_issue_runs import (  # noqa: F401 -- the fixtures the client is built from
    FULL_PLAN, PLAN, _approve, _create, _docs, _finish_planner, _members_hold_grants, _run,
    api_context, forge_tokens, github,
)
from .test_issue_writeback import _bare_run

#: Holds the one-re-review shape: `fix-loop` switched off (tests/unit/conftest.py).
pytestmark = pytest.mark.usefixtures("fix_loop_off")

LENS = issueruns.SECURITY_LENS_STEP
REVIEW = issueruns.REVIEW_STEP
FIX = issueruns.FIX_STEP


def _risky(plan: dict, path: str = "apps/swarm-api/swarm_api/credentials.py") -> dict:
    risky = copy.deepcopy(plan)
    risky["steps"][-1]["files"] = [*risky["steps"][-1].get("files", []), path]
    return risky


def _compiled(plan: dict):
    return compile_plan(_bare_run(plan=plan, plan_digest=issueruns.plan_digest(plan)))


def _by_id(workflow) -> dict:
    return {step.step_id: step for step in workflow.steps}


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_a_plan_touching_no_risk_path_compiles_exactly_as_before(plan, monkeypatch):
    assert issueruns.risk_path_hits(plan) == []
    compiled = _compiled(plan).model_dump()
    # The workflow with the lens taken out of the compiler altogether.
    monkeypatch.setattr(issueruns, "_with_security_lens", lambda run, p, steps, **_: steps)
    assert compiled == _compiled(plan).model_dump()
    ids = [step["step_id"] for step in compiled["steps"]]
    assert LENS not in ids
    assert ids[-3:] == [REVIEW, FIX, REREVIEW_STEP_ID]
    fix = next(step for step in compiled["steps"] if step["step_id"] == FIX)
    # Exactly the gate it always was: no `also` key at all.
    assert fix["when"] == {"step": REVIEW, "verdict_in": ["NOT_YET"]}
    assert fix["input_from"] == {REVIEW: issueruns.VERDICT_FILE}


@pytest.mark.parametrize("path", [
    "apps/common/swarm_common/models.py", "apps/redaction/redact.py",
    "apps/swarm-api/swarm_api/credentials.py", "apps/swarm-api/swarm_api/auth.py",
    "terraform/infra/iam.tf", ".github/workflows/ci.yml",
])
def test_the_risk_paths_are_the_review_gates(path):
    assert issueruns.risk_path_hits(_risky(FULL_PLAN, path)) == [path]


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_a_risk_path_plan_runs_the_two_reviews_beside_each_other(plan):
    workflow = _compiled(_risky(plan))
    steps = _by_id(workflow)
    ids = [step.step_id for step in workflow.steps]
    assert ids[-4:] == [REVIEW, LENS, FIX, REREVIEW_STEP_ID]
    review, lens = steps[REVIEW], steps[LENS]
    # The same build steps, and neither waits for the other.
    builds = [d for d in review.depends_on if d != issueruns.REQUIREMENTS_CHECK_STEP]
    assert list(lens.depends_on) == builds
    assert LENS not in review.depends_on and REVIEW not in lens.depends_on
    assert lens.builds_on == review.builds_on
    # The security lens, on the default coding class: no class is chosen.
    assert lens.runner_profile == issueruns.STEP_PROFILE
    assert lens.model_class is None
    assert "security lens" in lens.input["prompt"]
    assert SECURITY_VERDICT_FILE in lens.input["prompt"]
    assert lens.when is None, "it runs whatever the review says"


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_the_fix_stages_both_verdicts_and_runs_on_either(plan):
    fix = _by_id(_compiled(_risky(plan)))[FIX]
    assert set(fix.depends_on) == {REVIEW, LENS}
    # Each review's own, distinctly named verdict file.
    assert fix.input_from == {REVIEW: issueruns.VERDICT_FILE, LENS: SECURITY_VERDICT_FILE}
    assert fix.when.step == REVIEW and fix.when.verdict_in == ["NOT_YET"]
    # MUTATION: drop `also` and the fix runs on the code review's NOT_YET alone.
    assert fix.when.also == [LENS]
    assert issueruns.VERDICT_FILE in fix.input["prompt"]
    assert SECURITY_VERDICT_FILE in fix.input["prompt"]


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_the_re_review_covers_both_lenses_and_is_the_merges_one_verdict(plan):
    workflow = _compiled(_risky(plan))
    steps = _by_id(workflow)
    rereview, fix, lens = steps[REREVIEW_STEP_ID], steps[FIX], steps[LENS]
    assert rereview.builds_on == FIX
    assert rereview.input_from[REVIEW] == issueruns.VERDICT_FILE
    assert rereview.input_from[LENS] == SECURITY_VERDICT_FILE
    # The fix's gate: on any NOT_YET its agent judges the fixed head through
    # both lenses; only when both said MERGE does it pass the MERGE on.
    assert rereview.when == fix.when
    assert lens.input["prompt"] in rereview.input["prompt"]
    assert "covers them all" in rereview.input["prompt"]
    # The issue run merges on the re-review's verdict (`issueci`), and no
    # merge step reading one review's verdict alone can be planned.
    assert not any(validation.is_merge_step(step.runner_profile) for step in workflow.steps)
    specs = issueruns._step_specs([step.model_dump() for step in workflow.steps])
    assert validation.merge_step_for(specs, "integrate") is None


def test_the_re_review_a_submission_derives_is_the_compiled_one():
    """Submission accepts a stated re-review only when it is EXACTLY what
    `rereview_step_for` derives (`SubmissionService._stated_rereview`)."""
    workflow = _compiled(_risky(FULL_PLAN))
    dumped = [step.model_dump() for step in workflow.steps]
    rest = [s for s in dumped if s["step_id"] != REREVIEW_STEP_ID]
    by_id = {s["step_id"]: s for s in rest}
    derived = validation.rereview_step_for(
        issueruns._step_specs(rest), by_id[REVIEW], "integrate", None, {LENS: by_id[LENS]})
    stated = next(s for s in dumped if s["step_id"] == REREVIEW_STEP_ID)
    assert WorkflowStepCreate.model_validate(derived).model_dump() == stated
    # Without the lens's step it derives nothing rather than a one-lens re-review.
    assert validation.rereview_step_for(
        issueruns._step_specs(rest), by_id[REVIEW], "integrate", None) is None


def test_a_merge_step_reading_one_review_is_refused_when_the_fix_reads_two():
    steps = [
        StepSpec("impl", ()),
        StepSpec("review", ("impl",), {"impl": "swarm-work.patch"}),
        StepSpec("lens", ("impl",)),
        StepSpec("fix", ("review", "lens"), {"review": "verdict.json",
                                            "lens": SECURITY_VERDICT_FILE},
                 when_step="review", when_verdicts=("NOT_YET",), when_also=("lens",),
                 builds_on="impl"),
        StepSpec("merge", ("fix", "review"), {"review": "verdict.json"},
                 runner_profile="merge"),
    ]
    with pytest.raises(DagError, match="when.also"):
        validation.plan_merge(steps, "integrate")


def test_a_gate_naming_a_further_review_it_stages_nothing_from_is_refused():
    steps = [
        StepSpec("impl", ()),
        StepSpec("review", ("impl",)),
        StepSpec("lens", ("impl",)),
        StepSpec("fix", ("review", "lens"), {"review": "verdict.json"},
                 when_step="review", when_verdicts=("NOT_YET",), when_also=("lens",),
                 builds_on="impl"),
    ]
    with pytest.raises(DagError, match="stages no file"):
        validation.validate_step_routing(steps, strategy="integrate", integrator_step_id="fix")


# --------------------------------------------------------------------------
# through submission: the stored tasks' signed gates
# --------------------------------------------------------------------------

def _approved_workflow_tasks(client, db, objects, plan: dict) -> dict:
    created = _create(client, "alice")
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    read = _run(client, run["id"], "alice").json()["run"]
    response = _approve(client, run["id"], read["plan_digest"])
    assert response.status_code == 200, response.text
    workflow_id = response.json()["run"]["workflow_id"]
    return {t["step_id"]: t for t in _docs(db, "tasks").values()
            if t.get("workflow_id") == workflow_id}


def test_a_risk_path_run_submits_both_gates_signed(client, db, objects):
    tasks = _approved_workflow_tasks(client, db, objects, _risky(PLAN))
    assert set(tasks) >= {REVIEW, LENS, FIX, REREVIEW_STEP_ID}
    lens_task = tasks[LENS]["id"]
    for step in (FIX, REREVIEW_STEP_ID):
        gate = tasks[step]["metadata"]["dispatch"]["verdict_gate"]
        assert gate == {"task_id": tasks[REVIEW]["id"], "verdict_in": ["NOT_YET"],
                        "also_task_ids": [lens_task]}, step
    # The security lens runs on the default coding class: none was chosen.
    assert tasks[LENS]["runner_profile"] == "claude-code"
    assert "dispatch" not in tasks[LENS]["metadata"] or \
        "verdict_gate" not in tasks[LENS]["metadata"]["dispatch"]


def test_a_run_touching_no_risk_path_submits_todays_gate(client, db, objects):
    tasks = _approved_workflow_tasks(client, db, objects, copy.deepcopy(PLAN))
    assert LENS not in tasks
    assert tasks[FIX]["metadata"]["dispatch"]["verdict_gate"] == {
        "task_id": tasks[REVIEW]["id"], "verdict_in": ["NOT_YET"]}


# --------------------------------------------------------------------------
# the control plane's shortcuts leave a gate with further reviews to the worker
# --------------------------------------------------------------------------

def _gate_block(**gate) -> dict:
    return {"builds_on": "task_fix", "verdict_gate": {
        "task_id": "task_review", "verdict_in": ["NOT_YET"], **gate}}


def test_the_scheduler_predicts_no_shut_gate_from_one_review_of_two():
    from scheduler import runtime as runtime_mod

    fix = {"state": "SUCCEEDED", "result_summary": {"verdict_gate": {
        "task_id": "task_review", "verdict": "MERGE", "verdict_in": ["NOT_YET"]}}}
    read = {"task_fix": fix}.get
    # The one review's gate: its MERGE is a certain skip.
    assert runtime_mod.republishes(["task_fix"], "task_review", _gate_block(), read) is not None
    # MUTATION: drop the `also_task_ids` check and the security review's
    # NOT_YET is predicted away: the re-review is resolved as a copy.
    assert runtime_mod.republishes(
        ["task_fix"], "task_review", _gate_block(also_task_ids=["task_lens"]), read) is None


def test_swarm_api_does_not_publish_a_gate_with_further_reviews_itself():
    from swarm_api import verdictpublish

    doc = {"depends_on": ["task_review", "task_lens"],
           "metadata": {"dispatch": {**_gate_block(also_task_ids=["task_lens"]),
                                     "builds_on": "task_impl"}}}
    with pytest.raises(verdictpublish.Decline, match="further reviews"):
        verdictpublish._dispatch_facts(doc)


# --------------------------------------------------------------------------
# review round 1: directory entries and spellings; the real-diff backstop
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "apps/common/swarm_common", "apps/common/swarm_common/", "./apps/common/swarm_common",
    ".github/workflows", "terraform/infra", "apps/redaction",
    "apps/x/../common/swarm_common/m.py", "apps\\common\\swarm_common\\m.py",
    "Apps/Common/Swarm_Common/m.py",
])
def test_a_directory_or_a_respelled_risk_path_in_a_plan_is_a_hit(path):
    assert issueruns.risk_path_hits(_risky(FULL_PLAN, path)) != []


@pytest.mark.parametrize("path", ["docs", "apps/swarm-api/swarm_api/issues.py", "tests/unit"])
def test_a_plain_path_is_no_hit(path):
    assert [h for h in issueruns.risk_path_hits({"steps": [{"files": [path]}]})] == []


class _Store:
    def __init__(self, ids):
        self.ids = ids

    def get_workflow(self, *_a, **_k):
        from types import SimpleNamespace
        return SimpleNamespace(steps=[SimpleNamespace(step_id=i) for i in self.ids])


def _lacks(files, ids, security_review=False):
    from types import SimpleNamespace

    from swarm_api import issueci
    run = SimpleNamespace(security_review=security_review, workflow_id="wf")
    return issueci._lacks_security_review(SimpleNamespace(store=_Store(ids)), "t", run, files)


def test_the_merge_backstop_reads_the_real_diff_not_the_plan():
    risky = ["apps/common/swarm_common/models.py"]
    assert _lacks(risky, ["implement", REVIEW]) is True
    assert _lacks(risky, ["implement", REVIEW, LENS]) is False
    assert _lacks(risky, ["implement", REVIEW], security_review=True) is False
    assert _lacks(["docs/a.md"], ["implement", REVIEW]) is False
