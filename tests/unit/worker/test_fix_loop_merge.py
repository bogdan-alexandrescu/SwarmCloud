"""A merge behind a fix loop merges on the loop's verified final review (FL-WORKER).

docs/design/fix-loop.md §4.6. The merge cannot stage a verdict file from the
`fix-loop` step, which writes none, so `merge_step_for` signs
`merge_target.review_loop = <loop task>` in place of `review`. The worker:

  1. requires the loop SUCCEEDED, its signed spec verified, profile
     `fix-loop`, of this workflow;
  2. reads its `result_summary.fix_loop.final_review`, or for `rounds: 0`
     the loop's own signed `fix_loop.review_task_id` (`fix_loop.review` is
     the review's STEP id, as `validation.FixLoopSpec.block` signs it);
  3. verifies that review: signed spec (of its own one-step workflow), this
     tenant, SUCCEEDED, and its signed `fix_loop_round.loop_task` is the loop
     -- or it is the loop's declared review -- and reads its `verdict.json`
     through the tenant-checked artifact read;
  4. pins the head that review judged (its signed `repository_ref`; round 0:
     the head the opener pushed, the one its gate judged), so the merge is
     of exactly what the review said MERGE about;
  5. and everything after is the merge it always was.

The loop's `result_summary` is tenant-writable: step 3 is what makes it safe.
No credentials, no network: GitHub is `merge_world`'s fake.
"""

from __future__ import annotations

import json
from typing import Any

from agent_worker import merge
from swarm_common.states import TaskState

from merge_world import NUMBER, OPENER, OTHER, PINNED, REVIEW, TENANT, WORKFLOW, MergeWorld

LOOP = "task_loop"
RR1 = "task_rr1"
RR2 = "task_rr2"
FOREIGN = "task_rr_other_loop"
#: The head review-fix:2 pushed, which re-review:2 judged.
FIX2_HEAD = "f" * 40


def _review_doc(world: MergeWorld, task_id: str, verdict: dict[str, Any], *,
                loop: str = LOOP, head: str = FIX2_HEAD, round_: int = 2,
                state: str = TaskState.SUCCEEDED.value) -> dict[str, Any]:
    """A `re-review:<k>`: its own one-step `collect` workflow, pinned to `head`."""
    key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att_{task_id}/artifacts/verdict.json"
    size = world.store.upload_bytes(key, json.dumps(verdict).encode())
    return {
        "tenant_id": TENANT, "workflow_id": f"wf_{task_id}", "state": state,
        "runner_profile": "claude-code", "repository_ref": head,
        "metadata": {"dispatch": {"strategy": "collect", "fix_loop_round": {
            "loop_task": loop, "round": round_, "kind": "review", "head": head,
            "pull_request": NUMBER}}},
        "result_summary": {"artifacts": [
            {"name": "verdict.json", "uri": world.store.uri(key), "bytes": size}]},
    }


def _loop_world(tmp_path, *, final_review: str | None = RR2, rounds: int = 2,
                verdict: dict[str, Any] | None = None) -> MergeWorld:
    world = MergeWorld(tmp_path)
    # Nothing is staged: a loop's merge stages no verdict file.
    world.verdict = None
    world.target = {"pull_request": OPENER, "review_loop": LOOP}
    world.docs[LOOP] = {
        "tenant_id": TENANT, "workflow_id": WORKFLOW, "state": TaskState.SUCCEEDED.value,
        "runner_profile": "fix-loop",
        # The block as `validation.FixLoopSpec.block` signs it: the step ids
        # beside their TASK ids. MUTATION: put the task id in `review` and the
        # round-0 tests below no longer catch a reader of the step id.
        "metadata": {"dispatch": {"fix_loop": {"review": "review", "review_task_id": REVIEW,
                                               "fix": "fix", "fix_task_id": OPENER,
                                               "max_rounds": 3}}},
        "result_summary": {"fix_loop": {"rounds": [{"round": k} for k in range(1, rounds + 1)],
                                        "final_review": final_review,
                                        "final_head": FIX2_HEAD if rounds else PINNED,
                                        "verdict": "MERGE"}},
    }
    world.docs[RR1] = _review_doc(world, RR1, {"verdict": "NOT_YET", "findings": ["a blocker"]},
                                  head=PINNED, round_=1)
    world.docs[RR2] = _review_doc(world, RR2, verdict or {"verdict": "MERGE", "findings": []})
    # The PR's head is the one review-fix:2 pushed, and CI is green there.
    world.pr["head"]["sha"] = FIX2_HEAD
    world.serve_checks(FIX2_HEAD)
    return world


def _refusal(outcome) -> str:
    return outcome.summary["refusal"]["code"]


def test_the_final_rereviews_merge_merges_at_the_head_it_judged(tmp_path):
    world = _loop_world(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    # MUTATION: pin the opener's head as before and this is a head_moved.
    assert world.merge_calls()[0].body["sha"] == FIX2_HEAD
    assert outcome.summary["review_loop"] == LOOP
    assert outcome.summary["review_task"] == RR2
    assert outcome.summary["pinned"] == FIX2_HEAD
    assert outcome.summary["verdict"] == "MERGE"
    # The loop is verified as a step of this workflow, the re-review as one
    # of its own one-step workflow.
    assert (LOOP, None) in world.verified
    assert (RR2, f"wf_{RR2}") in world.verified


def test_a_final_rereviews_not_yet_is_verdict_not_merge(tmp_path):
    world = _loop_world(tmp_path, verdict={"verdict": "NOT_YET", "findings": ["still broken"]})

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert _refusal(outcome) == "verdict_not_merge"
    assert world.merge_calls() == []


def test_rounds_zero_merges_on_the_loops_own_review_at_the_pinned_head(tmp_path):
    world = _loop_world(tmp_path, final_review=None, rounds=0)
    world.pr["head"]["sha"] = PINNED
    key = f"tenants/{TENANT}/tasks/{REVIEW}/attempts/att_rev/artifacts/verdict.json"
    size = world.store.upload_bytes(key, json.dumps({"verdict": "MERGE"}).encode())
    world.docs[REVIEW].update({
        "state": TaskState.SUCCEEDED.value,
        "result_summary": {"artifacts": [
            {"name": "verdict.json", "uri": world.store.uri(key), "bytes": size}]}})

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert world.merge_calls()[0].body["sha"] == PINNED
    assert outcome.summary["review_task"] == REVIEW


def _round_zero_world(tmp_path, *, final_review: str | None = REVIEW,
                      artifact: dict[str, Any] | None = None,
                      skipped: bool = False) -> MergeWorld:
    """A loop whose first review said MERGE: `rounds: []`, the head the opener pushed.

    As the live loop records it (`fixloop._finish` after `_round_zero`):
    `final_review` is the declared review's task id. `artifact` is that
    review's verdict.json entry; None uploads one, as a review that ran does.
    """
    world = _loop_world(tmp_path, final_review=final_review, rounds=0)
    world.pr["head"]["sha"] = PINNED
    world.serve_checks(PINNED)
    if artifact is None:
        key = f"tenants/{TENANT}/tasks/{REVIEW}/attempts/att_rev/artifacts/verdict.json"
        size = world.store.upload_bytes(key, json.dumps({"verdict": "MERGE"}).encode())
        artifact = {"name": "verdict.json", "uri": world.store.uri(key), "bytes": size}
    summary: dict[str, Any] = {"artifacts": [artifact]}
    if skipped:
        summary["review_skipped"] = "small_change"
    world.docs[REVIEW].update({"state": TaskState.SUCCEEDED.value, "result_summary": summary})
    return world


def test_a_round_zero_merge_recorded_as_the_final_review_merges(tmp_path):
    # The live finding: `rounds: []`, `final_review` = the declared review's
    # task id, the block's `review` its step id. Refused as
    # fix_loop_review_not_its_own before the merge read `review_task_id`.
    world = _round_zero_world(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert world.merge_calls()[0].body["sha"] == PINNED
    assert outcome.summary["review_task"] == REVIEW
    assert outcome.summary["verdict"] == "MERGE"
    # The declared review is verified as a step of this workflow.
    assert (REVIEW, None) in world.verified


def test_a_round_zero_merge_on_a_gate_skipped_review_merges(tmp_path):
    # The review gate skipped the review as a small change: swarm-api ended
    # it and wrote its verdict inline (`inputs.inline_artifact`).
    world = _round_zero_world(tmp_path, skipped=True, artifact={
        "name": "verdict.json", "written_by": "control_plane",
        "inline": json.dumps({"verdict": "MERGE", "findings": []})})

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert world.merge_calls()[0].body["sha"] == PINNED
    assert outcome.summary["review_task"] == REVIEW
    assert (REVIEW, None) in world.verified


def test_a_round_zero_final_review_naming_another_task_is_refused(tmp_path):
    # `rounds: []` is no licence: a forged final review that is neither the
    # signed declared review nor this loop's re-review is still refused.
    world = _round_zero_world(tmp_path, final_review=OPENER)
    world.docs[OPENER]["state"] = TaskState.SUCCEEDED.value

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert _refusal(outcome) == merge.REVIEW_NOT_THE_LOOPS
    assert world.merge_calls() == []


def test_the_blocks_review_step_field_is_never_read_as_the_declared_review(tmp_path):
    # `fix_loop.review` is the step id; only `review_task_id` names the
    # declared review, so a task id in `review` admits nothing.
    world = _round_zero_world(tmp_path, final_review=OPENER)
    world.docs[OPENER]["state"] = TaskState.SUCCEEDED.value
    world.docs[LOOP]["metadata"]["dispatch"]["fix_loop"]["review"] = OPENER

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == merge.REVIEW_NOT_THE_LOOPS
    assert world.merge_calls() == []


def test_a_final_review_of_another_loop_is_refused(tmp_path):
    world = _loop_world(tmp_path, final_review=FOREIGN)
    world.docs[FOREIGN] = _review_doc(world, FOREIGN, {"verdict": "MERGE"},
                                      loop="task_another_loop")

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert _refusal(outcome) == merge.REVIEW_NOT_THE_LOOPS
    assert world.token_reads == 0 and world.merge_calls() == []


def test_a_forged_result_summary_naming_a_task_that_is_no_round_is_refused(tmp_path):
    # The loop's result_summary is tenant-writable: naming the opener (or any
    # task this tenant can read) as the final review must not merge.
    world = _loop_world(tmp_path, final_review=OPENER)
    world.docs[OPENER]["state"] = TaskState.SUCCEEDED.value

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert _refusal(outcome) == merge.REVIEW_NOT_THE_LOOPS
    assert world.merge_calls() == []


def test_a_forged_round_record_outside_the_signed_dispatch_is_refused(tmp_path):
    world = _loop_world(tmp_path, final_review=FOREIGN)
    forged = _review_doc(world, FOREIGN, {"verdict": "MERGE"}, loop="task_another_loop")
    # The tenant can write `metadata` beside the signed dispatch block; only
    # the signed block names the loop a round belongs to.
    forged["metadata"]["fix_loop_round"] = {"loop_task": LOOP, "round": 2, "kind": "review"}
    world.docs[FOREIGN] = forged

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == merge.REVIEW_NOT_THE_LOOPS
    assert world.merge_calls() == []


def test_a_fix_rounds_record_is_no_review(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[RR2]["metadata"]["dispatch"]["fix_loop_round"]["kind"] = "fix"

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == merge.REVIEW_NOT_THE_LOOPS
    assert world.merge_calls() == []


def test_a_final_review_that_did_not_succeed_is_refused(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[RR2]["state"] = TaskState.FAILED.value

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert _refusal(outcome) == merge.LOOP_REVIEW_NOT_SUCCEEDED
    assert world.merge_calls() == []


def test_a_final_review_that_judged_another_head_does_not_merge_the_live_one(tmp_path):
    # re-review:2 judged OTHER; the pull request's head is FIX2_HEAD.
    world = _loop_world(tmp_path)
    world.docs[RR2] = _review_doc(world, RR2, {"verdict": "MERGE"}, head=OTHER)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert _refusal(outcome) == "head_moved"
    assert world.merge_calls() == []


def test_a_round_record_whose_head_is_not_its_pinned_ref_is_refused(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[RR2]["metadata"]["dispatch"]["fix_loop_round"]["head"] = OTHER

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == merge.LOOP_REVIEW_HEAD_UNKNOWN
    assert world.merge_calls() == []


def test_a_rereview_pinned_to_no_full_sha_is_refused(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[RR2]["repository_ref"] = "main"
    world.docs[RR2]["metadata"]["dispatch"]["fix_loop_round"]["head"] = "main"

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == merge.LOOP_REVIEW_HEAD_UNKNOWN
    assert world.merge_calls() == []


def test_the_loop_must_have_succeeded(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[LOOP]["state"] = TaskState.PARKED.value

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert _refusal(outcome) == merge.LOOP_NOT_SUCCEEDED
    assert world.token_reads == 0 and world.merge_calls() == []


def test_the_loop_must_be_a_fix_loop_step(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[LOOP]["runner_profile"] = "claude-code"

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == merge.LOOP_NOT_A_FIX_LOOP
    assert world.merge_calls() == []


def test_a_loop_of_another_workflow_is_spec_unverified(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[LOOP]["workflow_id"] = "wf_other"

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == "spec_unverified"
    assert world.merge_calls() == []


def test_an_unsigned_final_review_is_spec_unverified(tmp_path):
    world = _loop_world(tmp_path)
    world.unverified[RR2] = "unsigned"

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == "spec_unverified"
    assert world.merge_calls() == []


def test_a_review_of_another_tenant_is_refused(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[RR2]["tenant_id"] = "someone-else"

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == merge.REVIEW_NOT_THE_LOOPS
    assert world.merge_calls() == []


def test_an_unreadable_final_verdict_is_verdict_unreadable(tmp_path):
    world = _loop_world(tmp_path)
    world.docs[RR2]["result_summary"]["artifacts"] = []

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == "verdict_unreadable"
    assert world.merge_calls() == []


def test_a_loop_with_no_final_review_after_rounds_is_refused(tmp_path):
    world = _loop_world(tmp_path, final_review=None, rounds=2)

    outcome = merge.run_merge(world.context())

    assert _refusal(outcome) == merge.REVIEW_NOT_THE_LOOPS
    assert world.merge_calls() == []


def test_a_target_naming_a_loop_and_a_review_is_invalid():
    for raw in ({"pull_request": OPENER, "review_loop": LOOP, "review": REVIEW,
                 "verdict_file": "verdict.json"},
                {"pull_request": OPENER, "review_loop": "not a task"},
                {"pull_request": OPENER, "review_loop": LOOP, "pull_request_workflow": WORKFLOW},
                {"number": NUMBER, "head_sha": PINNED, "review_loop": LOOP}):
        try:
            merge.parse_merge_target({"merge_target": raw})
        except merge.TargetInvalid:
            continue
        raise AssertionError(f"{raw} parsed")
    parsed = merge.parse_merge_target({"merge_target": {"pull_request": OPENER,
                                                        "review_loop": LOOP}})
    assert parsed.review_loop == LOOP and parsed.review is None
