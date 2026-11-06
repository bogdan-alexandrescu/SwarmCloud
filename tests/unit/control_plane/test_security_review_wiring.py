"""The security review step wired through the API and the workflow checks (owner, 2026-11-04).

docs/design/security-review-step.md left two follow-ups, built here:

  1. THE ISSUE-RUN REQUEST TAKES THE OPTION. `RunCreate.security_review`,
     default off, reaches `IssueRun.security_review` through `start_run`, so
     the compiled workflow carries the step after the fix. Asked for while
     work contracts are off, it is refused before the planner is paid for.
  2. AN `integrate` WORKFLOW MAY HOLD BOTH REVIEWS. A final step whose work
     contract loads the `security-audit` plugin -- the worker's own mark, not
     a step id ending in `review` -- that builds on the integrator is a
     verdict source, not a second final step. The appended merge depends on
     it and stages its `security-verdict.json` beside the re-review's
     `verdict.json`; the issue run's CI loop reads both before it submits its
     merge-only continuation.

Two real final steps are still refused. No credentials, no network, no
emulator.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_api import issueci, issueruns, workcontract as wc
from swarm_api.issueruns import compile_plan
from swarm_api.schemas import RunCreate, WorkflowCreate
from swarm_api.service import SubmissionService
from swarm_api.validation import (
    SECURITY_VERDICT_FILE,
    DispatchOptionError,
    StepSpec,
    is_security_review,
    merge_sources,
    merge_step_for,
    plan_merge,
    resolve_integrator_step,
    validate_step_routing,
)

from .conftest import auth_header
from .test_issue_run_security_review import catalogue  # noqa: F401 - fixture used by name
from .test_issue_runs import (  # noqa: F401 - fixtures used by name
    FULL_PLAN,
    _create,
    _docs,
    api_context,
    forge_tokens,
    github,
)
from .test_issue_writeback import _bare_run
from .test_plugins_setting import (  # noqa: F401 - fixtures used by name
    _allow,
    settings_key,
    sub_client,
    switch_on,
)

REPO = "https://github.com/octo-org/widget-shop.git"


def _security_contract(tenant_id: str = "eng") -> dict[str, Any]:
    return {
        "schema": wc.SCHEMA_VERSION, "contract_id": "wc-security-review", "tenant_id": tenant_id,
        "revision": 1, "strictness": wc.ADVISORY,
        "requirements": [{"id": "REQ-1", "text": "Audit the diff; change no file."}],
        "capabilities": {"plugins": {"security-audit": {
            "mode": "unattended", "skills": ["security-audit"]}}},
        "limits": {"runner_profile": "claude-code"},
        "ownership": {"owner": "alice@example.com"},
    }


def _specs(*, security: bool = True, security_builds_on: str | None = "fix",
           extra_final: bool = False) -> list[StepSpec]:
    """implement -> review -> fix (gated, the integrator), then the security
    review on the fix's head, and optionally a second ordinary final step."""
    steps = [
        StepSpec("implement", ()),
        StepSpec("review", ("implement",), input_from={"implement": "swarm-work.patch"},
                 builds_on="implement"),
        StepSpec("fix", ("review",), input_from={"review": "verdict.json"},
                 when_step="review", when_verdicts=("NOT_YET",), builds_on="implement"),
        StepSpec("security-review", ("fix",), builds_on=security_builds_on,
                 runner_profile="claude-code", security_review=security),
    ]
    if extra_final:
        steps.append(StepSpec("docs", ("implement",)))
    return steps


def _rereview(depends_on=("fix", "review")) -> StepSpec:
    return StepSpec("re-review", depends_on, input_from={"review": "verdict.json"},
                    when_step="review", when_verdicts=("NOT_YET",), builds_on="fix")


# -- 2: validation -------------------------------------------------------------------


def test_a_spec_with_both_reviews_is_accepted_and_its_merge_stages_both_verdicts():
    """MUTATION: drop `_final_security_reviews` from `resolve_integrator_step` and
    the workflow is refused as having two final steps, fix and security-review."""
    specs = [*_specs(), _rereview()]
    assert resolve_integrator_step(specs, rereview="re-review") == "fix"
    validate_step_routing(specs, strategy="integrate", integrator_step_id="fix",
                          rereview="re-review")

    merge = merge_step_for(specs, "integrate", rereview="re-review")
    assert merge is not None
    assert merge["depends_on"] == ["fix", "re-review", "security-review"]
    # Keyed by file name in the merge's staged inputs: two names, no collision.
    assert merge["input_from"] == {"re-review": "verdict.json",
                                   "security-review": SECURITY_VERDICT_FILE}

    # The stated merge is held to exactly that: a merge staging only the
    # re-review's verdict would merge without the security review's say.
    merge_spec = StepSpec("merge", tuple(merge["depends_on"]),
                          input_from=merge["input_from"], runner_profile="merge")
    planned = plan_merge([*specs, merge_spec], "integrate", rereview="re-review")
    assert planned is not None and planned.sources.security_review == "security-review"
    short = StepSpec("merge", tuple(merge["depends_on"]),
                     input_from={"re-review": "verdict.json"}, runner_profile="merge")
    with pytest.raises(Exception, match="security review's verdict files"):
        plan_merge([*specs, short], "integrate", rereview="re-review")


def test_without_a_rereview_the_merge_stages_the_reviews_and_the_security_verdict():
    specs = _specs()
    merge = merge_step_for(specs, "integrate")
    assert merge is not None
    assert merge["depends_on"] == ["fix", "review", "security-review"]
    assert merge["input_from"] == {"review": "verdict.json",
                                   "security-review": SECURITY_VERDICT_FILE}


def test_a_workflow_without_a_security_review_merges_exactly_as_before():
    specs = _specs()[:3]
    merge = merge_step_for(specs, "integrate")
    assert merge is not None
    assert merge["depends_on"] == ["fix", "review"]
    assert merge["input_from"] == {"review": "verdict.json"}


def test_two_real_final_steps_are_still_refused():
    with pytest.raises(DispatchOptionError, match="exactly one step must be final") as refused:
        resolve_integrator_step(_specs(extra_final=True))
    assert sorted(refused.value.detail["terminal_steps"]) == ["docs", "fix"]
    assert merge_sources(_specs(extra_final=True), "integrate") is None


def test_a_step_named_security_review_without_the_plugin_is_an_ordinary_final_step():
    """The mark is the work contract's plugin, never the step id: unmarked, it
    is the sink the integrator is, and beside the stated re-review it is a
    second final step."""
    unmarked = _specs(security=False)
    sources = merge_sources(unmarked, "integrate")
    assert sources is not None
    assert (sources.pull_request, sources.security_review) == ("security-review", None)
    with pytest.raises(DispatchOptionError, match="exactly one step must be final") as refused:
        resolve_integrator_step([*unmarked, _rereview()])
    assert sorted(refused.value.detail["terminal_steps"]) == ["re-review", "security-review"]


def test_a_security_review_of_another_head_is_refused():
    with pytest.raises(DispatchOptionError, match="builds on the integrating step 'fix'"):
        resolve_integrator_step(_specs(security_builds_on="implement"))
    with pytest.raises(DispatchOptionError, match="builds on no step"):
        resolve_integrator_step(_specs(security_builds_on=None))
    assert merge_sources(_specs(security_builds_on="implement"), "integrate") is None


def test_two_security_reviews_are_refused():
    specs = [*_specs(), StepSpec("security-review-2", ("fix",), builds_on="fix",
                                 security_review=True)]
    with pytest.raises(DispatchOptionError, match="at most one security review"):
        resolve_integrator_step(specs)
    assert merge_sources(specs, "integrate") is None


def test_the_mark_is_the_work_contracts_plugin_while_contracts_are_on(monkeypatch):
    metadata = {wc.METADATA_KEY: _security_contract()}
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    assert is_security_review(metadata, {wc.SWITCH_ENV: "on"})
    other = {wc.METADATA_KEY: {**_security_contract(), "capabilities": {"plugins": {
        "gstack": {"mode": "unattended", "skills": ["review"]}}}}}
    assert not is_security_review(other, {wc.SWITCH_ENV: "on"})
    assert not is_security_review({}, {wc.SWITCH_ENV: "on"})
    # Off, the plugin never loads and the worker writes no security verdict.
    assert not is_security_review(metadata, {})

    spec = WorkflowCreate.model_validate({
        "strategy": "integrate", "repository_url": REPO,
        "steps": [
            {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"}},
            {"step_id": "audit", "runner_profile": "claude-code", "depends_on": ["a"],
             "builds_on": "a", "input": {"prompt": "audit"}, "metadata": metadata},
        ],
    })
    marks = {s.step_id: s.security_review for s in SubmissionService._step_specs(spec)}
    assert marks == {"a": False, "audit": True}
    monkeypatch.delenv(wc.SWITCH_ENV)
    assert not any(s.security_review for s in SubmissionService._step_specs(spec))


# -- 2: over the API -----------------------------------------------------------------


def _workflow(merge: str) -> dict[str, Any]:
    return {
        "strategy": "integrate", "repository_url": REPO,
        "metadata": {"merge": merge},
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
             "input": {"prompt": "review; write verdict.json"}},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
             "builds_on": "implement", "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": "fix every finding"}},
            {"step_id": "security-review", "runner_profile": "claude-code",
             "depends_on": ["fix"], "builds_on": "fix", "allow_empty_diff": True,
             "input": {"prompt": "Security review of this pull request. Do not edit files."},
             "metadata": {wc.METADATA_KEY: _security_contract()}},
        ],
    }


def test_on_merge_verdict_with_a_security_review_appends_a_merge_staging_both(
        sub_client, db, switch_on, settings_key):
    _allow(db, settings_key, names=("security-audit",))
    response = sub_client.post("/v1/workflows", headers=auth_header("alice"),
                               json=_workflow("on_merge_verdict"))
    assert response.status_code == 201, response.text
    body = response.json()
    steps = {s["step_id"]: s for s in body["workflow"]["steps"]}
    assert list(steps) == ["implement", "review", "fix", "security-review", "re-review",
                           "merge"]
    assert body["dispatch"]["integrator_step_id"] == "fix"
    merge = steps["merge"]
    assert merge["depends_on"] == ["fix", "re-review", "security-review"]
    assert merge["input_from"] == {"re-review": "verdict.json",
                                   "security-review": SECURITY_VERDICT_FILE}
    block = db.docs[f"tasks/{merge['task_id']}"]["metadata"]["dispatch"]
    # The review the merge target names is still the re-review; the security
    # verdict reaches the worker through the signed input_from.
    assert block["merge_target"]["review"] == steps["re-review"]["task_id"]


def test_the_same_workflow_without_the_plugin_mark_has_no_review_to_merge_on(
        sub_client, db, switch_on, settings_key):
    """Unmarked, the last step is the integrator, which is gated on nothing,
    so "on_merge_verdict" has no review to read: refused, nothing stored."""
    spec = _workflow("on_merge_verdict")
    del spec["steps"][-1]["metadata"]
    response = sub_client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 422, response.text
    assert "not gated on a review" in response.json()["message"]
    assert not [p for p in db.docs if p.startswith("tasks/")]


# -- 1 and 2: the issue run -------------------------------------------------------------


def test_the_request_field_defaults_off():
    assert RunCreate.model_validate({"issue": "octo-org/widget-shop#7"}).security_review is False
    asked = RunCreate.model_validate({"issue": "octo-org/widget-shop#7", "security_review": True})
    assert asked.security_review is True


def test_an_issue_run_asked_for_the_review_compiles_a_workflow_submission_accepts(catalogue):
    """MUTATION: drop `security_review=` from `_step_specs` and the compiled
    workflow's re-review is no longer recognised, so it is refused."""
    run = _bare_run(plan=FULL_PLAN, plan_digest=issueruns.plan_digest(FULL_PLAN),
                    security_review=True)
    workflow = compile_plan(run)
    ids = [s.step_id for s in workflow.steps]
    assert ids[-2:] == [issueruns.REREVIEW_STEP, issueruns.SECURITY_REVIEW_STEP]
    specs = SubmissionService._step_specs(workflow)
    assert [s.step_id for s in specs if s.security_review] == [issueruns.SECURITY_REVIEW_STEP]
    rereview = SubmissionService._stated_rereview(workflow, specs, "integrate")
    assert rereview == issueruns.REREVIEW_STEP
    assert resolve_integrator_step(specs, rereview=rereview) == issueruns.FIX_STEP
    validate_step_routing(specs, strategy="integrate", integrator_step_id=issueruns.FIX_STEP,
                          rereview=rereview)
    # A merge for this workflow stages both verdicts.
    merge = merge_step_for(specs, "integrate", rereview=rereview)
    assert merge is not None and merge["input_from"] == {
        issueruns.REREVIEW_STEP: issueruns.VERDICT_FILE,
        issueruns.SECURITY_REVIEW_STEP: SECURITY_VERDICT_FILE,
    }


def test_post_runs_records_the_option_and_refuses_it_while_contracts_are_off(
        client, db, monkeypatch):
    monkeypatch.delenv(wc.SWITCH_ENV, raising=False)
    refused = _create(client, security_review=True)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["field"] == "security_review"
    assert _docs(db, "issue_runs") == {} and _docs(db, "tasks") == {}

    plain = _create(client)
    assert plain.status_code == 201, plain.text
    assert plain.json()["run"].get("security_review") in (None, False)

    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    asked = _create(client, security_review=True)
    assert asked.status_code == 201, asked.text
    run = asked.json()["run"]
    assert run["security_review"] is True
    stored = issueruns.IssueRun.from_firestore(db.docs[f"issue_runs/{run['id']}"])
    assert stored.security_review is True


# -- 2: the issue run's merge reads the security verdict ---------------------------------


class _Inspection:
    def __init__(self, files: dict[tuple[str, str], str]) -> None:
        self.files = files

    def read_artifact(self, tenant_id, task_id, *, submitted_by, name, limit_bytes):
        from swarm_api.errors import NotFound

        if (task_id, name) not in self.files:
            raise NotFound(f"{task_id} wrote no {name}")
        return {"status": "ok", "content": self.files[(task_id, name)], "truncated": False}


class _Step:
    def __init__(self, step_id: str, task_id: str) -> None:
        self.step_id, self.task_id = step_id, task_id


class _Store:
    def __init__(self, steps: list[_Step]) -> None:
        self.steps = steps

    def get_workflow(self, tenant_id, workflow_id, *, submitted_by):
        return type("Workflow", (), {"steps": self.steps})()


class _Ctx:
    def __init__(self, files, steps) -> None:
        self.inspection = _Inspection(files)
        self.store = _Store(steps)


def _security_ctx(verdict: str | None):
    steps = [_Step(issueruns.REREVIEW_STEP, "t-rereview"),
             _Step(issueruns.SECURITY_REVIEW_STEP, "t-security")]
    files = {("t-rereview", issueruns.VERDICT_FILE): json.dumps({"verdict": "MERGE"})}
    if verdict is not None:
        files[("t-security", SECURITY_VERDICT_FILE)] = json.dumps({"verdict": verdict})
    return _Ctx(files, steps)


@pytest.mark.parametrize("verdict, read, why", [
    ("MERGE", "MERGE", ""),
    ("NOT_YET", "NOT_YET", ""),
    (None, None, f"wrote no {SECURITY_VERDICT_FILE}"),
])
def test_the_issue_runs_merge_reads_the_security_verdict_beside_the_rereviews(
        verdict, read, why):
    """MUTATION: return `(None, "")` from `_security_verdict` unconditionally and
    a NOT_YET security review no longer holds the run's merge."""
    run = _bare_run(security_review=True, workflow_id="wf_1")
    ctx = _security_ctx(verdict)
    assert issueci._review_verdict(ctx, "eng", run) == ("MERGE", "")
    found, problem = issueci._security_verdict(ctx, "eng", run)
    assert found == read
    assert why in problem


def test_a_run_that_did_not_ask_reads_no_security_verdict():
    run = _bare_run(workflow_id="wf_1")
    assert issueci._security_verdict(_security_ctx("NOT_YET"), "eng", run) == (None, "")
