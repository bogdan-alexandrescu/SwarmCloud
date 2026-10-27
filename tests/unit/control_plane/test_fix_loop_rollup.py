"""The rollup knows a fix loop's rounds by their step ids (FL-ROLLUP, part of #2509).

docs/design/fix-loop.md §4.2 numbers a loop's rounds `review-fix:<k>` and
`re-review:<k>`, k >= 1, appended to the workflow by swarm-api as the merge
step's rounds are. §6.1: they are judged by their owner step, the `fix-loop`
step, whose own ending is the verdict on them -- so a round that FAILED
before a later re-review said MERGE leaves the workflow SUCCEEDED, and a loop
that ended FAILED (`fix_loop_exhausted`) fails it by its own state.

A bare `re-review` is NOT a round: it is the step `on_merge_verdict` appends
today (`validation.REREVIEW_STEP_ID`), caller-visible and judged like any
step. The colon is what tells the two apart, and a caller's step id can never
hold one (`WorkflowStepCreate.step_id`).

Pure: no credentials, no emulator, no store.
"""

from __future__ import annotations

import pytest

from swarm_common.models import WorkflowStep
from swarm_common.states import TaskState
from swarm_rollup import derive, is_fix_round_step, read_steps


def _derive(**states: str):
    steps = [WorkflowStep(step_id=sid, runner_profile="claude-code", input={},
                          task_id=f"task_{n}")
             for n, sid in enumerate(states)]
    by_task = {f"task_{n}": TaskState(state) for n, state in enumerate(states.values())}
    return derive(read_steps(steps, by_task))


@pytest.mark.parametrize("step_id", ["re-review:1", "re-review:2", "review-fix:1",
                                     "review-fix:3", "review-fix:10", "re-review:999"])
def test_a_fix_loops_round_ids_are_rounds(step_id):
    # MUTATION: drop `review` from the fix kinds or `|re-review` from
    # swarm_rollup.FIX_ROUND_STEP_RE and FL-DRIVE's append of this round is
    # refused (`mergewake._append_round_step` reads the same predicate).
    assert is_fix_round_step(step_id), step_id


@pytest.mark.parametrize("step_id", [
    "re-review",       # today's on_merge_verdict step: an ordinary step
    "review-fix",      # no round number
    "re-review:0",     # rounds count from 1 (§4.2)
    "review-fix:0",
    "review-fix:x",    # not a number
    "re-review:01",    # no leading zero: one round, one spelling
    "review-fix:1000",  # past the three digits every round kind allows
    "re-review-2",     # a caller-shaped id
    "re-reviews:2",
    "pre-review:2",
    "review-fix:2 ",
])
def test_ids_that_only_resemble_a_loop_round_are_not_rounds(step_id):
    # MUTATION: loosen the number to `[0-9]+`, or match with `search` instead
    # of `fullmatch`, and one of these ids is excused from the rollup.
    assert not is_fix_round_step(step_id), step_id


def test_a_failed_review_fix_under_a_loop_that_succeeded_rolls_up_succeeded():
    """§6.1: a FAILED `review-fix:2` before a MERGE in `re-review:3`."""
    rollup = _derive(**{
        "implement": "SUCCEEDED", "review": "SUCCEEDED", "fix": "SUCCEEDED",
        "loop": "SUCCEEDED",
        "re-review:1": "SUCCEEDED", "review-fix:2": "FAILED",
        "re-review:2": "SUCCEEDED", "review-fix:3": "SUCCEEDED",
        "re-review:3": "SUCCEEDED",
    })
    # MUTATION: leave `review-fix` out of FIX_ROUND_STEP_RE and the failed
    # round is judged as an ordinary step: the workflow reads FAILED.
    assert rollup.state == "SUCCEEDED", rollup
    assert rollup.reason == "all_steps_succeeded"
    # MUTATION: count rounds out of `counts` and the failure disappears from
    # the one place a reader sees that a round failed.
    assert rollup.counts == {"SUCCEEDED": 8, "FAILED": 1}
    # MUTATION: drop `re-review` from the pattern and the re-reviews vanish
    # from the named rounds.
    assert rollup.fix_round_steps == ["re-review:1", "review-fix:2", "re-review:2",
                                      "review-fix:3", "re-review:3"]


def test_a_failed_re_review_round_under_a_loop_that_succeeded_rolls_up_succeeded():
    rollup = _derive(**{"review": "SUCCEEDED", "loop": "SUCCEEDED",
                        "review-fix:1": "SUCCEEDED", "re-review:1": "FAILED",
                        "review-fix:2": "SUCCEEDED", "re-review:2": "SUCCEEDED"})
    # MUTATION: drop `|re-review` from FIX_ROUND_STEP_RE and this reads FAILED.
    assert rollup.state == "SUCCEEDED", rollup


def test_a_loop_that_ended_failed_fails_the_workflow():
    """`fix_loop_exhausted`: the person accepted the last NOT_YET (§4.5)."""
    rollup = _derive(**{"review": "SUCCEEDED", "fix": "SUCCEEDED", "loop": "FAILED",
                        "re-review:1": "SUCCEEDED", "review-fix:2": "SUCCEEDED",
                        "re-review:2": "SUCCEEDED"})
    # MUTATION: excuse every step from severity whenever rounds are present
    # (judge only rounds, or nothing) and an exhausted loop reads SUCCEEDED.
    assert rollup.state == "FAILED", rollup
    assert rollup.reason == "worst_terminal_step_failed"


def test_a_round_running_under_a_parked_loop_keeps_the_workflow_running():
    """§6.1: between halves the loop is PARKED; while a round runs, RUNNING."""
    rollup = _derive(**{"review": "SUCCEEDED", "fix": "SUCCEEDED", "loop": "PARKED",
                        "re-review:1": "SUCCEEDED", "review-fix:2": "RUNNING"})
    # MUTATION: skip rounds before the capacity test and a live pod reads PARKED.
    assert rollup.state == "RUNNING", rollup


def test_a_loop_parked_between_rounds_is_pending_never_finished():
    rollup = _derive(**{"review": "SUCCEEDED", "fix": "SUCCEEDED", "loop": "PARKED",
                        "re-review:1": "SUCCEEDED", "review-fix:2": "FAILED"})
    # MUTATION: judge only terminal steps and the round_failed park reads
    # FAILED, so the person it waits for is told the workflow is over.
    assert rollup.state == "PARKED", rollup


def test_a_bare_re_review_that_failed_still_fails_the_workflow():
    """The `on_merge_verdict` re-review is the caller's, not a round."""
    rollup = _derive(**{"implement": "SUCCEEDED", "review": "SUCCEEDED",
                        "fix": "SUCCEEDED", "re-review": "FAILED", "merge": "SUCCEEDED"})
    # MUTATION: make the round number optional in FIX_ROUND_STEP_RE and a
    # failed review is excused: the workflow reads SUCCEEDED.
    assert rollup.state == "FAILED", rollup
    assert rollup.fix_round_steps == []
