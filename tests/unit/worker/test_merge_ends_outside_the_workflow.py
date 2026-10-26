"""A held merge woken because its pull request ended outside the workflow finishes.

MERGE-OUTSIDE-ENDS, part of #2441 (BGD 2026-10-26). swarm-api's wake tick marks
an owner-question hold whose pull request was merged or closed
(`merge_wait.owner_hold.wake_reason`), and the scheduler returns it to READY.
The woken step re-reads the pull request with the tenant's token BEFORE the
owner-questions hold, because a merged pull request needs no answers:

  * merged, by anyone: SUCCEEDED, naming the merge commit, who and when,
    merging nothing and asking nothing again;
  * closed unmerged: CANCELLED, saying so;
  * still open (a forged or stale mark): it parks on the hold exactly as before.

An unmarked hold stays credential-free, as #2070 made it.

`merge_world.MergeWorld` is green in every respect; each test adds the hold.
"""

from __future__ import annotations

from typing import Any

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import MERGED, OPENER, PR, TASK, WORKFLOW, MergeWorld

IMPLEMENT = "task_impl"
QUESTION = {"task_id": IMPLEMENT, "step_id": "implement", "question_index": 1,
            "question": "Does the route join UNCAPTURED_ROUTES?"}


def _held(tmp_path, *, wake_reason: str | None) -> MergeWorld:
    world = MergeWorld(tmp_path)
    world.docs[IMPLEMENT] = {"tenant_id": "eng", "workflow_id": WORKFLOW,
                             "step_id": "implement", "metadata": {"owner_answers": []}}
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [dict(QUESTION)]}
    if wake_reason is not None:
        hold: dict[str, Any] = {"parked_at": "2026-10-26T01:52:00+00:00", "pull_request": 41,
                                "wake_requested_at": "2026-10-26T07:26:00+00:00",
                                "wake_reason": wake_reason}
        world.docs[TASK]["metadata"] = {"merge_wait": {
            "wake_requested_at": hold["wake_requested_at"], "wake_reason": wake_reason,
            merge.OWNER_HOLD_KEY: hold}}
    return world


def _merged_by_hand(world: MergeWorld) -> None:
    world.pr.update(state="closed", merged=True, merge_commit_sha=MERGED,
                    merged_by={"login": "bogdan-alexandrescu"},
                    merged_at="2026-10-26T07:25:00Z")


def test_a_pull_request_merged_by_hand_ends_the_step_succeeded(tmp_path):
    world = _held(tmp_path, wake_reason=merge.PULL_REQUEST_MERGED)
    _merged_by_hand(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: hold before reading the pull request (the #2441 bug) and this parks.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.owner_wait is None and outcome.ci_wait is None
    assert world.merge_calls() == []
    summary = outcome.summary
    assert summary["merged_outside_workflow"] is True
    assert summary["merged_by_this_task"] is False
    # MUTATION: drop who, when or the commit and the summary cannot say what happened.
    assert summary["merge_commit"] == MERGED
    assert summary["merged_by"] == "bogdan-alexandrescu"
    assert summary["merged_at"] == "2026-10-26T07:25:00Z"
    assert "merged outside the workflow" in summary["outcome"]
    assert MERGED in summary["outcome"] and "bogdan-alexandrescu" in summary["outcome"]
    assert world.token_reads == 1


def test_a_pull_request_closed_unmerged_ends_the_step_cancelled(tmp_path):
    world = _held(tmp_path, wake_reason=merge.PULL_REQUEST_CLOSED)
    world.pr.update(state="closed", merged=False)

    outcome = merge.run_merge(world.context())

    # MUTATION: park again on a closed pull request and it waits for ever.
    assert outcome.state is TaskState.CANCELLED, outcome.message
    assert outcome.exit_code == ExitCode.CANCELLED
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "pull_request_closed"
    assert "closed without merging" in outcome.message
    assert world.merge_calls() == []


def test_a_forged_mark_on_an_open_pull_request_parks_again_as_before(tmp_path):
    world = _held(tmp_path, wake_reason=merge.PULL_REQUEST_MERGED)

    outcome = merge.run_merge(world.context())

    # MUTATION: trust the mark without reading GitHub and this ends SUCCEEDED.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.owner_wait is not None
    assert outcome.owner_wait["questions"] == [QUESTION]
    assert world.merge_calls() == []


def test_a_merged_pull_request_of_another_branch_does_not_end_the_step(tmp_path):
    world = _held(tmp_path, wake_reason=merge.PULL_REQUEST_MERGED)
    _merged_by_hand(world)
    world.pr["head"] = dict(world.pr["head"], ref="someone-else/branch")

    outcome = merge.run_merge(world.context())

    # MUTATION: skip the branch check and an opener's forged number ends the step.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.owner_wait is not None


def test_an_unmarked_hold_reads_no_token_even_when_the_pull_request_merged(tmp_path):
    world = _held(tmp_path, wake_reason=None)
    _merged_by_hand(world)

    outcome = merge.run_merge(world.context())

    # The hold stays credential-free unless the tick marked it (#2070).
    assert outcome.state is TaskState.PARKED
    assert world.token_reads == 0
    assert world.github.calls("GET", PR) == []


def test_the_merge_facts_carry_who_merged_and_when(tmp_path):
    world = _held(tmp_path, wake_reason=merge.PULL_REQUEST_MERGED)
    _merged_by_hand(world)
    merger = merge.merger_for(world.repository_url, token=world.token,
                              transport=world.github)

    facts = merger.pull_request(41)

    assert facts.merged and facts.merged_by == "bogdan-alexandrescu"
    assert facts.merged_at == "2026-10-26T07:25:00Z"
    assert OPENER in (facts.head_ref or "")
