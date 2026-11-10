"""An issue run's optional security review step (owner, 2026-11-04).

docs/design/security-review-step.md. `IssueRun.security_review`, off by
default, adds one step after the fix, beside the re-review: `claude-code`,
building on the fix's pushed head, with a work contract whose capabilities
load the `security-audit` plugin in unattended mode -- the signed field the
worker knows the step by. Off, the compiled workflow is exactly today's.

The plugin's catalogue entry lands in a sibling lane, so the catalogue is
given the entry here: nothing depends on an image. No credentials, no
network, no emulator.
"""

from __future__ import annotations

import pytest

from swarm_api import issueruns, workcontract
from swarm_api.issueruns import IssueRun, InvalidPlan, compile_plan

from .test_issue_run_keyword import STAGED_FULL_PLAN
from .test_issue_runs import FULL_PLAN
from .test_issue_writeback import _bare_run


@pytest.fixture
def catalogue(monkeypatch):
    monkeypatch.setenv("SWARM_WORK_CONTRACTS", "on")
    monkeypatch.setattr(workcontract, "PLUGIN_CATALOGUE", {
        **workcontract.PLUGIN_CATALOGUE,
        issueruns.SECURITY_PLUGIN: {"unattended": (issueruns.SECURITY_PLUGIN,)},
    })


def _compiled(plan: dict, **fields):
    return compile_plan(_bare_run(plan=plan, plan_digest=issueruns.plan_digest(plan), **fields))


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_without_the_option_the_workflow_is_todays(plan):
    workflow = _compiled(plan)
    ids = [s.step_id for s in workflow.steps]
    assert ids[-3:] == [issueruns.REVIEW_STEP, issueruns.FIX_STEP, issueruns.REREVIEW_STEP]
    assert issueruns.SECURITY_REVIEW_STEP not in ids
    assert _bare_run().security_review is False


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_the_option_adds_a_security_review_after_the_fix(plan, catalogue):
    workflow = _compiled(plan, security_review=True)
    ids = [s.step_id for s in workflow.steps]
    assert ids[-4:] == [issueruns.REVIEW_STEP, issueruns.FIX_STEP, issueruns.REREVIEW_STEP,
                        issueruns.SECURITY_REVIEW_STEP]
    step = workflow.steps[-1]
    assert step.runner_profile == "claude-code"
    assert list(step.depends_on) == [issueruns.FIX_STEP]
    assert step.builds_on == issueruns.FIX_STEP
    assert step.when is None, "it runs whatever the review said"
    contract = step.metadata[workcontract.METADATA_KEY]
    assert contract["capabilities"]["plugins"] == {
        "security-audit": {"mode": "unattended", "skills": ["security-audit"]}}
    assert contract["limits"]["runner_profile"] == "claude-code"
    assert contract["scope"]["write"] == []
    # The re-review is untouched: the merge's review verdict is still its.
    rereview = next(s for s in workflow.steps if s.step_id == issueruns.REREVIEW_STEP)
    assert issueruns.SECURITY_REVIEW_STEP not in rereview.depends_on


def test_the_option_is_refused_while_work_contracts_are_off(monkeypatch):
    monkeypatch.delenv("SWARM_WORK_CONTRACTS", raising=False)
    with pytest.raises(InvalidPlan, match="work contracts are off"):
        _compiled(FULL_PLAN, security_review=True)


def test_the_option_round_trips_through_the_run_document():
    on = _bare_run(security_review=True)
    stored = on.to_firestore()
    assert stored["security_review"] is True
    assert IssueRun.from_firestore(stored).security_review is True
    assert on.to_api()["security_review"] is True

    off = _bare_run()
    assert "security_review" not in off.to_firestore()
    assert IssueRun.from_firestore(off.to_firestore()).security_review is False
