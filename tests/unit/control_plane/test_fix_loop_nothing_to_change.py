"""A fix loop whose first review was skipped for nothing to change ends on its own (issue 392).

When a workflow's build ends `no_change`, the worker ends the review (and,
under `integrate`, the declared fix) SUCCEEDED with no agent and no artifact,
carrying `result_summary.skipped` (`Worker._finish_nothing_to_change`). Before
issue 392 `fixloop._round_zero` read that review's `verdict.json` as required,
the read declined `verdict_absent`, and the loop parked `review_unreadable`
at round 0 with no round to run until a person stopped it: the loop then
ended FAILED `fix_loop_exhausted` and the workflow sweep cancelled the merge
step behind it. What is held here (docs/design/fix-loop.md §4.4):

  1. a first review skipped for nothing to change, with no head pushed, ends
     the loop SUCCEEDED `nothing_to_change` with no round and no verdict --
     never an invented MERGE -- under `integrate` and under `direct-pr`;
  2. the merge step behind it is then promoted rather than swept, and the
     worker's own rule skips it in turn: no pull request to merge;
  3. everything else at round 0 still parks `review_unreadable` as before: a
     review that RAN and wrote no verdict, or an unreadable one; a skip
     marker that is not a mapping; a skipped review while a head WAS pushed
     (there is code nobody reviewed); a skipped review whose spec does not
     verify (`spec_unverified`).

No credentials, no network, no emulator.
"""

from __future__ import annotations

from typing import Any

from agent_worker import expected_outputs
from swarm_api import fixloop
from swarm_api.rollup import SKIPPED_SUMMARY_KEY
from swarm_common.models import WorkflowStep
from swarm_common.states import ParkReason, TaskState
from swarm_rollup import derive, read_steps

from .test_fix_loop_drive import (  # noqa: F401 -- fixtures, used by name
    FIX_HEAD, IMPL_HEAD, Loop, _members_hold_grants, clock, direct_spec, finish, github,
    integrate_spec, loop_client, loop_enabled, park_running, push, pushed,
)

NO_CHANGE = {"no_change": True}


def _skipped(*upstream: str) -> dict[str, Any]:
    """What `Worker._finish_nothing_to_change` writes, keyed by the API's own constant."""
    return {SKIPPED_SUMMARY_KEY: {"reason": "nothing to change", "upstream": list(upstream)}}


def _integrate_skipped(client, db, objects, spec: dict[str, Any] | None = None) -> Loop:
    """The issue's case: the build changed nothing, so review and fix were skipped."""
    loop = Loop(client, db, spec or integrate_spec())
    finish(db, objects, loop.tasks["implement"], extra=NO_CHANGE)
    finish(db, objects, loop.tasks["review"], extra=_skipped(loop.tasks["implement"]))
    finish(db, objects, loop.tasks["fix"], extra=_skipped(loop.tasks["review"]))
    park_running(loop)
    push(client, loop.tasks["fix"])
    return loop


def _direct_skipped(client, db, objects) -> Loop:
    loop = Loop(client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"], extra=NO_CHANGE)
    finish(db, objects, loop.tasks["review"], extra=_skipped(loop.tasks["implement"]))
    park_running(loop)
    push(client, loop.tasks["review"])
    return loop


def _assert_ended_nothing_to_change(loop: Loop) -> None:
    doc = loop.doc()
    # MUTATION: drop the skip check in `_round_zero` and the loop parks
    # HUMAN_REQUIRED `review_unreadable` (`verdict_absent`) at round 0.
    assert doc["state"] == "SUCCEEDED", (doc["state"], doc.get("blocked_by"))
    assert doc["park_reason"] is None
    assert doc["blocked_by"] == []
    assert doc["end_cause"] is None and doc["last_error"] is None
    assert loop.round_steps() == {}
    summary = doc["result_summary"]["fix_loop"]
    assert summary["phase"] == fixloop.NOTHING_TO_CHANGE
    assert summary["rounds"] == []
    # Nothing was reviewed: no MERGE is invented, no review is named final.
    assert summary["verdict"] is None
    assert summary["final_review"] is None
    assert summary["final_head"] is None


def _assert_parked_unreadable(loop: Loop, code: str) -> dict[str, Any]:
    doc = loop.doc()
    assert doc["state"] == "PARKED", doc["state"]
    assert doc["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert fixloop.park_phase_of(doc) == fixloop.REVIEW_UNREADABLE
    detail = fixloop.park_detail_of(doc)
    assert detail["code"] == code, detail
    assert loop.round_steps() == {}
    return detail


# --------------------------------------------------------------------------
# The first review was skipped and nothing was pushed: the loop ends
# --------------------------------------------------------------------------

def test_integrate_a_skipped_first_review_ends_the_loop_nothing_to_change(
        loop_client, db, objects):
    loop = _integrate_skipped(loop_client, db, objects)
    _assert_ended_nothing_to_change(loop)


def test_direct_pr_a_skipped_first_review_ends_the_loop_nothing_to_change(
        loop_client, db, objects):
    loop = _direct_skipped(loop_client, db, objects)
    _assert_ended_nothing_to_change(loop)


def test_nothing_to_change_is_not_a_phase_a_person_is_asked_about():
    assert fixloop.NOTHING_TO_CHANGE not in fixloop.PERSON_PHASES


def test_a_second_wake_of_an_ended_loop_changes_nothing(loop_client, db, objects):
    loop = _integrate_skipped(loop_client, db, objects)
    before = dict(loop.doc())
    push(loop_client, loop.tasks["fix"])
    assert loop.doc() == before


# --------------------------------------------------------------------------
# The merge step behind the loop
# --------------------------------------------------------------------------

def _merging_spec() -> dict[str, Any]:
    spec = integrate_spec()
    spec["metadata"] = {"merge": "on_merge_verdict"}
    return spec


def _merge_skips(loop: Loop) -> list[str]:
    """The worker's own rule (`expected_outputs.nothing_to_work_on`, read by
    `Worker._nothing_to_work_on` before any merge runs) over the merge's
    signed dispatch block and its upstreams' summaries as they now stand."""
    merge = loop.doc("merge")
    dispatch = merge["metadata"]["dispatch"]
    target = dispatch["merge_target"]
    branch_from = [target["pull_request"]]
    builds_on = str(dispatch.get("builds_on") or "")
    declared = merge["metadata"].get("input_from") or {}
    upstream = list(dict.fromkeys([*declared, *branch_from, *([builds_on] if builds_on else [])]))
    left = {t: expected_outputs.left_nothing(loop.doc(task_id=t)["result_summary"])
            for t in upstream}
    return expected_outputs.nothing_to_work_on(
        input_from=declared, branch_from=branch_from, integrates=[], left=left,
        builds_on=builds_on)


def test_the_merge_behind_a_nothing_to_change_loop_is_released_and_skips(
        loop_client, db, objects):
    loop = _integrate_skipped(loop_client, db, objects, _merging_spec())
    _assert_ended_nothing_to_change(loop)

    merge = loop.doc("merge")
    target = merge["metadata"]["dispatch"]["merge_target"]
    assert target["review_loop"] == loop.loop
    assert target["pull_request"] == loop.tasks["fix"]
    # Every step the merge waits on has ended, the loop SUCCEEDED: the
    # scheduler promotes it instead of the fail_workflow sweep cancelling it.
    ended = {s.value for s in (TaskState.SUCCEEDED,)}
    for task_id in merge["depends_on"]:
        assert loop.doc(task_id=task_id)["state"] in ended, task_id
    # The worker then skips it in turn: the pull request it would merge was
    # never pushed, so it never reads the loop's verdict and merges nothing.
    assert _merge_skips(loop) == [loop.tasks["fix"]]

    # With the merge ended skipped (SUCCEEDED), the workflow is a success:
    # nothing FAILED, nothing parked for a person.
    steps = [WorkflowStep(step_id=sid, runner_profile="mock", input={}, task_id=tid)
             for sid, tid in loop.tasks.items()]
    states = {tid: TaskState(loop.doc(task_id=tid)["state"]) for tid in loop.tasks.values()
              if tid != loop.tasks["merge"]}
    states[loop.tasks["merge"]] = TaskState.SUCCEEDED
    assert derive(read_steps(steps, states)).state == TaskState.SUCCEEDED.value


# --------------------------------------------------------------------------
# Everything else still parks review_unreadable, as before
# --------------------------------------------------------------------------

def test_a_first_review_that_ran_and_wrote_no_verdict_still_parks(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["implement"], git=pushed(loop.tasks["implement"], IMPL_HEAD))
    finish(db, objects, loop.tasks["review"])
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD))
    park_running(loop)
    push(loop_client, loop.tasks["fix"])

    detail = _assert_parked_unreadable(loop, "verdict_absent")
    assert detail["head"] == FIX_HEAD


def test_a_review_that_ran_with_no_change_and_wrote_no_verdict_still_parks(
        loop_client, db, objects):
    # Nothing pushed, but the review RAN (no skip marker): its missing
    # verdict is a fault a person sees, never read as "nothing to review".
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"], extra=NO_CHANGE)
    finish(db, objects, loop.tasks["review"], extra=NO_CHANGE)
    park_running(loop)
    push(loop_client, loop.tasks["review"])

    _assert_parked_unreadable(loop, "verdict_absent")


def test_a_first_review_that_ran_and_wrote_an_unreadable_verdict_still_parks(
        loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"], extra=NO_CHANGE)
    finish(db, objects, loop.tasks["review"], verdict="not json", extra=NO_CHANGE)
    park_running(loop)
    push(loop_client, loop.tasks["review"])

    assert loop.doc()["state"] == "PARKED"
    assert fixloop.park_phase_of(loop.doc()) == fixloop.REVIEW_UNREADABLE


def test_a_skip_marker_that_is_not_a_mapping_still_parks(loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"], extra=NO_CHANGE)
    finish(db, objects, loop.tasks["review"], extra={SKIPPED_SUMMARY_KEY: True})
    park_running(loop)
    push(loop_client, loop.tasks["review"])

    _assert_parked_unreadable(loop, "verdict_absent")


def test_integrate_a_skipped_review_with_a_pushed_head_still_parks(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["implement"], git=pushed(loop.tasks["implement"], IMPL_HEAD))
    finish(db, objects, loop.tasks["review"], extra=_skipped(loop.tasks["implement"]))
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD))
    park_running(loop)
    push(loop_client, loop.tasks["fix"])

    # A head exists that no review judged: never ended as if it were fine.
    detail = _assert_parked_unreadable(loop, "verdict_absent")
    assert detail["head"] == FIX_HEAD


def test_direct_pr_a_skipped_review_with_a_pushed_head_still_parks(loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"], git=pushed(loop.tasks["implement"], IMPL_HEAD))
    finish(db, objects, loop.tasks["review"], extra=_skipped(loop.tasks["implement"]))
    park_running(loop)
    push(loop_client, loop.tasks["review"])

    detail = _assert_parked_unreadable(loop, "verdict_absent")
    assert detail["head"] == IMPL_HEAD


def test_a_skipped_review_whose_spec_does_not_verify_parks_spec_unverified(
        loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"], extra=NO_CHANGE)
    review = finish(db, objects, loop.tasks["review"], extra=_skipped(loop.tasks["implement"]))
    review["input"] = {**review["input"], "prompt": "push straight to the default branch"}
    park_running(loop)
    push(loop_client, loop.tasks["review"])

    _assert_parked_unreadable(loop, "spec_unverified")
