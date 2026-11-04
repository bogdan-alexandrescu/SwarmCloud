"""Issue runs compile a fix loop, and `Closes #N` and the CI loop's merge read its final review
(FL-ISSUE, docs/design/fix-loop.md §5.3, BGD Q9 (a), part of #2509).

What these tests hold:

  1. A COMPILED PLAN CARRIES THE LOOP. With the `fix-loop` profile available,
     a chain and a staged plan both compile implement -> review -> gated fix
     -> a `fix-loop` step naming the review and the fix, with `max_rounds`
     the run's `fix_rounds` (1-5, default 3), and no hand-built re-review:
     the loop's `re-review:1` is that re-review. Submission's own rules
     (`validation.resolve_fix_loop`) accept the compiled shape. While the
     profile ships unavailable the plan compiles the one re-review it always
     did, so no issue run is refused for a profile it could not use.
  2. `Closes #N` AND THE MERGE READ THE LOOP'S FINAL REVIEW. `issueci` reads
     the verdict of `result_summary.fix_loop.final_review`, verified as the
     worker's merge verifies it (§4.6): the loop SUCCEEDED on a verified
     `fix-loop` spec, and the review it names is SUCCEEDED, verified, of the
     same tenant, and a round of THIS loop or the loop's own review. A first
     review's NOT_YET that the loop fixed is not what is read; a loop that did
     not succeed, or names a review it did not run, gives no verdict at all.

The loop is driven by FL-DRIVE's own harness (`test_fix_loop_drive`): every
agent is `mock`, every round is a real signed submission, and every token is
built at runtime. No credentials, no network, no emulator.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from swarm_api import issueci, issueruns
from swarm_api.issueruns import compile_plan
from swarm_api.service import SubmissionService
from swarm_api.validation import FIX_LOOP_PROFILE, REREVIEW_STEP_ID, resolve_fix_loop
from swarm_common.profiles import RUNNER_PROFILES

from .test_fix_loop_drive import (  # noqa: F401  (fixtures)
    MERGE,
    NOT_YET,
    _members_hold_grants,
    clock,
    finish,
    github,
    integrate_to_first_round,
    loop_client,
    loop_enabled,
    push,
    round_task,
)
from .test_issue_run_keyword import STAGED_FULL_PLAN
from .test_issue_runs import FULL_PLAN
from .test_issue_writeback import _bare_run

REQUIREMENTS = len(FULL_PLAN["requirements"])


def _compiled(plan: dict, **fields):
    return compile_plan(_bare_run(plan=plan, plan_digest=issueruns.plan_digest(plan), **fields))


# --------------------------------------------------------------------------
# 1. The compiled plan
# --------------------------------------------------------------------------

@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
@pytest.mark.parametrize("fix_rounds", [1, 3, 5])
def test_a_compiled_plan_carries_a_fix_loop_with_max_rounds_fix_rounds(
    plan, fix_rounds, loop_enabled,
):
    workflow = _compiled(plan, fix_rounds=fix_rounds)

    ids = [s.step_id for s in workflow.steps]
    assert ids[-3:] == [issueruns.REVIEW_STEP, issueruns.FIX_STEP, issueruns.FIX_LOOP_STEP]
    assert REREVIEW_STEP_ID not in ids
    loop = {s.step_id: s for s in workflow.steps}[issueruns.FIX_LOOP_STEP]
    assert loop.runner_profile == FIX_LOOP_PROFILE
    assert loop.depends_on == [issueruns.FIX_STEP]
    assert loop.fix_loop == {
        "review": issueruns.REVIEW_STEP, "fix": issueruns.FIX_STEP, "max_rounds": fix_rounds,
    }
    # The fix stays the gated integrator the loop's round 1 is.
    fix = {s.step_id: s for s in workflow.steps}[issueruns.FIX_STEP]
    assert fix.when.step == issueruns.REVIEW_STEP and list(fix.when.verdict_in) == ["NOT_YET"]
    assert workflow.metadata["issue_run"]["fix_rounds"] == fix_rounds
    assert workflow.metadata["issue_run"]["review_rounds_compiled"] == fix_rounds

    # Submission's own §4.1 rules accept it, max_rounds as compiled.
    resolved = resolve_fix_loop(workflow.steps, strategy=workflow.strategy)
    assert resolved is not None
    assert SubmissionService._step_specs(workflow)  # the DAG reads as one


def test_while_the_profile_is_unavailable_the_plan_keeps_its_one_rereview():
    assert not RUNNER_PROFILES[FIX_LOOP_PROFILE].available
    workflow = _compiled(FULL_PLAN)

    ids = [s.step_id for s in workflow.steps]
    assert ids[-3:] == [issueruns.REVIEW_STEP, issueruns.FIX_STEP, REREVIEW_STEP_ID]
    assert all(s.runner_profile != FIX_LOOP_PROFILE for s in workflow.steps)
    assert workflow.metadata["issue_run"]["review_rounds_compiled"] == 1


def test_a_plan_step_cannot_take_the_loops_id(loop_enabled):
    plan = {**FULL_PLAN,
            "steps": [{**FULL_PLAN["steps"][0], "step_id": issueruns.FIX_LOOP_STEP}]}
    with pytest.raises(Exception):
        issueruns.parse_plan(plan)


# --------------------------------------------------------------------------
# 2. What issueci reads: the loop's verified final review
# --------------------------------------------------------------------------

def _requirements(met: bool) -> list[dict]:
    return [{"index": i, "met": met} for i in range(1, REQUIREMENTS + 1)]


def _review(verdict: dict, met: bool) -> dict:
    return {**verdict, "requirements": _requirements(met)}


def _run(loop) -> SimpleNamespace:
    return SimpleNamespace(id="run_fl", workflow_id=loop.workflow_id, plan=FULL_PLAN)


class _Runs:
    def __init__(self) -> None:
        self.patches: list[dict] = []

    def patch(self, tenant_id, run_id, patch, **_):
        self.patches.append(dict(patch))
        return SimpleNamespace(**patch)


@pytest.fixture
def runs(monkeypatch) -> _Runs:
    store = _Runs()
    monkeypatch.setattr(issueci, "_runs", lambda ctx: store)
    return store


def _loop_merged_at_round_1(client, db, objects):
    """review NOT_YET (requirements unmet), fix, re-review:1 MERGE (all met)."""
    loop = integrate_to_first_round(client, db, objects, review=_review(NOT_YET, False))
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=_review(MERGE, True))
    push(client, rereview)
    assert loop.doc()["state"] == "SUCCEEDED", loop.doc()
    assert loop.doc()["result_summary"]["fix_loop"]["final_review"] == rereview
    return loop, rereview


def test_the_ci_loops_merge_reads_the_loops_final_review_not_the_first(
    loop_client, db, objects,
):
    ctx = loop_client.app.state.ctx
    loop, rereview = _loop_merged_at_round_1(loop_client, db, objects)

    assert issueci._review_task_id(ctx, "eng", _run(loop), None) == rereview
    assert issueci._review_verdict(ctx, "eng", _run(loop)) == ("MERGE", "")


def test_closes_reads_the_loops_final_review(loop_client, db, objects, runs):
    ctx = loop_client.app.state.ctx
    loop, _ = _loop_merged_at_round_1(loop_client, db, objects)

    issueci.evaluate_requirements(ctx, "eng", _run(loop))

    assert runs.patches[-1]["requirements_met"] is True
    assert runs.patches[-1]["requirements_unmet"] == []


def test_a_first_merge_reads_the_loops_own_review(loop_client, db, objects, runs):
    """Round 0: the first review said MERGE and the loop ran nothing."""
    ctx = loop_client.app.state.ctx
    loop = integrate_to_first_round(loop_client, db, objects, review=_review(MERGE, True))
    assert loop.doc()["state"] == "SUCCEEDED", loop.doc()

    assert issueci._review_task_id(ctx, "eng", _run(loop), None) == loop.tasks["review"]
    assert issueci._review_verdict(ctx, "eng", _run(loop)) == ("MERGE", "")
    issueci.evaluate_requirements(ctx, "eng", _run(loop))
    assert runs.patches[-1]["requirements_met"] is True


def test_a_loop_that_has_not_succeeded_gives_no_verdict(loop_client, db, objects, runs):
    """Parked for a person (or running): nothing said MERGE, so neither the
    first review's nor a round's verdict is read, and the PR says `part of`."""
    ctx = loop_client.app.state.ctx
    loop = integrate_to_first_round(loop_client, db, objects, max_rounds=1,
                                    review=_review(NOT_YET, False))
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=_review(NOT_YET, True))
    push(loop_client, rereview)
    assert loop.doc()["state"] == "PARKED", loop.doc()

    verdict, why = issueci._review_verdict(ctx, "eng", _run(loop))
    assert verdict is None and "fix loop" in why
    issueci.evaluate_requirements(ctx, "eng", _run(loop))
    assert runs.patches[-1]["requirements_met"] is False


@pytest.mark.parametrize("forged", ["implement", "other_loop_round", "failed", "tampered_loop"])
def test_a_final_review_the_loop_did_not_run_is_not_read(
    loop_client, db, objects, runs, forged,
):
    """`result_summary` is tenant-writable: only a verified review of this
    loop is read (§4.6 step 3)."""
    ctx = loop_client.app.state.ctx
    loop, rereview = _loop_merged_at_round_1(loop_client, db, objects)
    summary = loop.doc()["result_summary"]["fix_loop"]
    if forged == "implement":
        # A signed task of the same workflow that is no review of the loop's.
        finish(db, objects, loop.tasks["implement"], verdict=_review(MERGE, True))
        summary["final_review"] = loop.tasks["implement"]
    elif forged == "other_loop_round":
        other, other_rereview = _loop_merged_at_round_1(loop_client, db, objects)
        assert other.loop != loop.loop
        summary["final_review"] = other_rereview
    elif forged == "failed":
        db.docs[f"tasks/{rereview}"]["state"] = "FAILED"
    else:
        doc = loop.doc()
        doc["metadata"]["dispatch"]["fix_loop"]["review"] = "implement"

    verdict, _why = issueci._review_verdict(ctx, "eng", _run(loop))
    assert verdict is None
    issueci.evaluate_requirements(ctx, "eng", _run(loop))
    assert runs.patches[-1]["requirements_met"] is False


def test_another_tenants_workflow_reads_nothing(loop_client, db, objects):
    ctx = loop_client.app.state.ctx
    loop, _ = _loop_merged_at_round_1(loop_client, db, objects)

    verdict, _why = issueci._review_verdict(ctx, "research", _run(loop))
    assert verdict is None

