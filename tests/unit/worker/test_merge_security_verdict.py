"""The merge requires the security review's verdict beside the review's (owner, 2026-11-04).

docs/design/security-review-step.md. A merge step that staged the security
review's `security-verdict.json` reads it after the review's verdict:

  * MERGE (no confirmed critical or high finding) -> the merge goes on;
  * NOT_YET with unanswered owner questions -> parked for a person, the same
    owner-question park the review's `needs_owner` uses (Needs you);
  * NOT_YET, every question answered "accept the risk" or "false positive"
    -> the merge goes on, with the answers recorded;
  * NOT_YET with any "fix it now" -> refused `security_fix_requested`, no
    merge call;
  * a merge that staged no security verdict merges exactly as before.

The real merge action on `merge_world`'s fake GitHub. No credentials, no network.
"""

from __future__ import annotations

import json
from typing import Any

from agent_worker import merge
from agent_worker import security_review as sr
from swarm_common.states import TaskState

from merge_world import PINNED, VERDICT_FILE, WORKFLOW, MergeWorld

SECURITY = "task_sec"


def _world(tmp_path, security: dict[str, Any] | None,
           answers: list[dict[str, Any]] | None = None) -> tuple[MergeWorld, Any]:
    world = MergeWorld(tmp_path)
    world.docs[SECURITY] = {"tenant_id": "eng", "workflow_id": WORKFLOW,
                            "step_id": "security-review", "state": "SUCCEEDED",
                            "metadata": {"owner_answers": answers or []}}
    ctx = world.context()
    if security is not None:
        path = tmp_path / sr.VERDICT_FILE
        path.write_text(json.dumps(security))
        ctx.staged = {**ctx.staged, sr.VERDICT_FILE: path}
    return world, ctx


def _blocking() -> dict[str, Any]:
    return {
        "verdict": "NOT_YET",
        "findings": [{"summary": "[high] SQL injection (apps/api/q.py:3)", "severity": "high"}],
        "needs_owner": [{"task_id": SECURITY, "step_id": "security-review",
                         "question_index": 0, "question": "The security review confirmed..."}],
    }


def test_a_workflow_without_a_security_review_merges_as_today(tmp_path):
    world, ctx = _world(tmp_path, None)
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PINNED
    assert sr.SUMMARY_KEY not in outcome.summary


def test_a_security_merge_verdict_lets_the_merge_go_on(tmp_path):
    world, ctx = _world(tmp_path, {"verdict": "MERGE", "findings": [],
                                   "security_review": {"counts": {}}})
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    assert outcome.summary[sr.SUMMARY_KEY]["verdict"] == "MERGE"


def test_a_confirmed_high_parks_the_merge_for_the_owner(tmp_path):
    world, ctx = _world(tmp_path, _blocking())
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.owner_wait is not None
    (held,) = outcome.owner_wait["questions"]
    assert (held["task_id"], held["question_index"]) == (SECURITY, 0)
    assert world.merge_calls() == []
    assert world.token_reads == 0, "the hold is decided before any credential"


def test_the_owner_accepting_the_risk_lets_the_merge_go_on(tmp_path):
    world, ctx = _world(tmp_path, _blocking(), answers=[
        {"question_index": 0, "choice": sr.ACCEPT_RISK, "text": "internal tool"}])
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    (answer,) = outcome.summary[sr.SUMMARY_KEY]["answers"]
    assert answer["answer"]["choice"] == sr.ACCEPT_RISK


def test_the_owner_marking_a_false_positive_lets_the_merge_go_on(tmp_path):
    world, ctx = _world(tmp_path, _blocking(), answers=[
        {"question_index": 0, "choice": sr.FALSE_POSITIVE, "text": "parameterised"}])
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_the_owner_asking_for_the_fix_stops_the_merge(tmp_path):
    world, ctx = _world(tmp_path, _blocking(), answers=[
        {"question_index": 0, "choice": sr.FIX_NOW}])
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == merge.SECURITY_FIX_REQUESTED
    assert world.merge_calls() == []


def test_an_answer_on_another_workflows_task_does_not_count(tmp_path):
    world, ctx = _world(tmp_path, _blocking(), answers=[
        {"question_index": 0, "choice": sr.ACCEPT_RISK}])
    world.docs[SECURITY]["workflow_id"] = "wf_other"
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.PARKED, outcome.message
    assert world.merge_calls() == []


def test_a_not_yet_naming_no_question_refuses(tmp_path):
    world, ctx = _world(tmp_path, {"verdict": "NOT_YET", "findings": ["x"]})
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.FAILED
    assert outcome.summary["refusal"]["code"] == merge.SECURITY_VERDICT_NOT_MERGE
    assert world.merge_calls() == []


def test_an_unreadable_security_verdict_refuses(tmp_path):
    world, ctx = _world(tmp_path, {"verdict": "MAYBE"})
    outcome = merge.run_merge(ctx)

    assert outcome.state is TaskState.FAILED
    assert outcome.summary["refusal"]["code"] == merge.SECURITY_VERDICT_UNREADABLE
    assert world.merge_calls() == []


def test_the_reviews_not_yet_still_refuses_first(tmp_path):
    world, ctx = _world(tmp_path, {"verdict": "MERGE", "findings": []})
    world.verdict = {"verdict": "NOT_YET", "findings": ["still broken"]}
    ctx.staged[VERDICT_FILE].write_text(json.dumps(world.verdict))
    outcome = merge.run_merge(ctx)

    assert outcome.summary["refusal"]["code"] == "verdict_not_merge"
    assert world.merge_calls() == []

