"""The merge step and the CI-fix rounds of a workflow that is not an issue run (lane MS7).

docs/merge-step.md "Revised 2026-10-06" §1 and §6 MS7: a workflow asks for
up to `metadata.merge_fix_rounds` CI-fix rounds. When the wake tick reads a
red required check on a CI_PENDING merge with rounds left, it claims a round
in the merge task's `metadata.merge_fix` and submits a one-step continuation
of the task whose branch the pull request is on (swarm-api's
`issueci.ci_fix_continuation`). The merge stays parked; the round pushes a
new head to the same branch.

What the WORKER holds here:

  1. The head a fix round pushed is accepted -- by itself, or under GitHub's
     own base merges -- only when the round's task is this tenant's, its
     signed spec verifies, and its signed dispatch block continues the same
     root as the signed target. Every other head is still `head_moved`.
  2. Red with a round running at this head, or with rounds left that the
     tick has not claimed yet, parks CI_PENDING: never `checks_failed`
     while a round can still fix it. Red with the rounds spent, or a round
     that ended at this head, is `checks_failed`.

And the MS3 review finding (2026-10-06): GitHub's update-branch call is
ASYNCHRONOUS. A 202 whose head has not moved yet parks at the old head with
`branch_update_pending`, which the tick waits on until the head moves rather
than reading the old head's green checks; a head that moves within the
bounded re-read parks at the NEW head. The old head is never merged.

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

import merge_world
from merge_world import CHECK, OPENER, PINNED, TASK, TENANT, MergeWorld

FIX_TASK = "task_fix1"
FIXED = "f" * 40


def _round(world: MergeWorld, *, task_id: str | None = FIX_TASK, head: str = PINNED,
           rounds: int = 2, **extra: Any) -> dict[str, Any]:
    """The tick's claim of round 1, at `head`, on the merge task's own document."""
    entry: dict[str, Any] = {"round": 1, "head": head, **extra}
    if task_id is not None:
        entry["task_id"] = task_id
    world.docs[TASK]["metadata"] = {
        "merge_fix_rounds": rounds,
        merge.MERGE_FIX_METADATA_KEY: {"rounds": [entry]},
    }
    return entry


def _fix_task(world: MergeWorld, *, continues: str = OPENER, pushed: str | None = FIXED,
              state: str = "SUCCEEDED", tenant: str = TENANT) -> None:
    git = {"pushed_head": pushed} if pushed else {}
    world.docs[FIX_TASK] = {
        "tenant_id": tenant, "workflow_id": "wf_fix", "state": state,
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": continues}},
        "result_summary": {"git": git},
    }


def _at(world: MergeWorld, head: str) -> None:
    world.pr["head"]["sha"] = head
    world.serve_checks(head)


def _red(world: MergeWorld) -> None:
    world.runs[0].update(conclusion="failure")


# ---------------------------------------------------------------------------
# 1. the head a fix round pushed
# ---------------------------------------------------------------------------

def test_the_head_a_fix_round_pushed_is_accepted_and_merged_there(tmp_path):
    world = MergeWorld(tmp_path)
    _round(world)
    _fix_task(world)
    _at(world, FIXED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == FIXED
    assert outcome.summary["head"] == FIXED
    assert outcome.summary["head_pushed_by"] == FIX_TASK
    assert outcome.summary["updates"] == 0


def test_a_fix_rounds_head_under_githubs_own_base_merge_is_accepted(tmp_path):
    world = MergeWorld(tmp_path)
    _round(world)
    _fix_task(world)
    updated = world.github_merge(FIXED)
    world.pr["head"]["sha"] = updated

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == updated
    assert outcome.summary["updates"] == 1


def _no_round(world: MergeWorld) -> None:
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 2}


def _other_root(world: MergeWorld) -> None:
    _round(world)
    _fix_task(world, continues="task_other")


def _not_a_continuation(world: MergeWorld) -> None:
    _round(world)
    _fix_task(world)
    world.docs[FIX_TASK]["metadata"]["dispatch"].pop("continues")


def _unsigned(world: MergeWorld) -> None:
    _round(world)
    _fix_task(world)
    world.unverified[FIX_TASK] = "signature does not verify"


def _another_tenant(world: MergeWorld) -> None:
    _round(world)
    _fix_task(world, tenant="research")


def _pushed_elsewhere(world: MergeWorld) -> None:
    _round(world)
    _fix_task(world, pushed="9" * 40)


def _unreadable(world: MergeWorld) -> None:
    _round(world, task_id="task_missing")


HEAD_MOVED = [
    ("no_round_recorded", _no_round),
    ("round_continues_another_root", _other_root),
    ("round_task_is_not_a_continuation", _not_a_continuation),
    ("round_spec_unverified", _unsigned),
    ("round_task_of_another_tenant", _another_tenant),
    ("round_pushed_another_head", _pushed_elsewhere),
    ("round_task_unreadable", _unreadable),
]


@pytest.mark.parametrize(("case", "mutate"), HEAD_MOVED, ids=[c[0] for c in HEAD_MOVED])
def test_any_other_head_is_still_head_moved(tmp_path, case, mutate):
    """The control is the first test of this file: the same head, accepted
    there, refused here for exactly one broken fact about the round."""
    world = MergeWorld(tmp_path)
    mutate(world)
    _at(world, FIXED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, (case, outcome.message)
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "head_moved", (case, outcome.summary)
    assert world.merge_calls() == [] and world.update_calls() == []


# ---------------------------------------------------------------------------
# 2. red, and the rounds
# ---------------------------------------------------------------------------

def _parked(outcome, code: str, head: str) -> None:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.end_cause is None and outcome.retryable is False
    assert outcome.ci_wait["code"] == code, outcome.ci_wait
    assert outcome.ci_wait["head"] == head
    assert "refusal" not in outcome.summary


def test_red_with_a_round_running_at_this_head_parks(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _round(world)
    _fix_task(world, state="RUNNING", pushed=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_RUNNING, PINNED)
    assert CHECK in outcome.summary["wait"]["message"]
    assert world.merge_calls() == []


def test_red_with_a_claimed_round_not_yet_submitted_parks(tmp_path):
    """Claimed, its workflow not recorded yet: the tick is between its claim
    and its record. It parks; the tick wakes it if the round is lost."""
    world = MergeWorld(tmp_path)
    _red(world)
    _round(world, task_id=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_RUNNING, PINNED)


def test_red_with_rounds_left_and_none_claimed_parks_for_the_tick(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 1}

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_PENDING, PINNED)
    assert world.merge_calls() == []


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_red_after_a_round_ended_at_this_head_is_checks_failed(tmp_path, state):
    world = MergeWorld(tmp_path)
    _red(world)
    _round(world)
    _fix_task(world, state=state, pushed=PINNED if state == "SUCCEEDED" else None)

    outcome = merge.run_merge(world.context())

    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert "round 1" in outcome.summary["refusal"]["message"]


def test_red_after_a_refused_round_is_checks_failed(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _round(world, task_id=None, error="the submitter is no longer a member")

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "checks_failed"


def test_red_with_the_rounds_spent_is_checks_failed(tmp_path):
    """One round asked for, spent at an earlier head; red again at its head."""
    world = MergeWorld(tmp_path)
    _round(world, rounds=1)
    _fix_task(world)
    _at(world, FIXED)
    _red(world)

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "checks_failed", outcome.summary
    assert world.merge_calls() == []


def test_red_with_rounds_left_after_a_round_parks_again(tmp_path):
    """Two asked for, one spent at an earlier head: red at the round's head
    waits for the tick to claim the second."""
    world = MergeWorld(tmp_path)
    _round(world, rounds=2)
    _fix_task(world)
    _at(world, FIXED)
    _red(world)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_PENDING, FIXED)


@pytest.mark.parametrize("rounds", [None, 0, "5", True, -1])
def test_red_with_no_rounds_asked_for_is_checks_failed(tmp_path, rounds):
    world = MergeWorld(tmp_path)
    _red(world)
    if rounds is not None:
        world.docs[TASK]["metadata"] = {"merge_fix_rounds": rounds}

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "checks_failed"


def test_a_forged_round_budget_is_capped_at_the_apis_maximum(tmp_path):
    from swarm_api import validation

    assert merge.MERGE_FIX_ROUNDS_MAX == validation.MERGE_FIX_ROUNDS_MAX
    world = MergeWorld(tmp_path)
    _red(world)
    world.docs[TASK]["metadata"] = {
        "merge_fix_rounds": 99,
        merge.MERGE_FIX_METADATA_KEY: {"rounds": [
            {"round": n, "head": f"{n:040x}"} for n in range(1, merge.MERGE_FIX_ROUNDS_MAX + 1)
        ]},
    }
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "checks_failed"


def test_the_tick_and_the_worker_read_the_same_keys_and_codes():
    from swarm_api import mergewake

    assert merge.MERGE_FIX_METADATA_KEY == mergewake.MERGE_FIX_METADATA_KEY == "merge_fix"
    assert merge.BRANCH_UPDATE_PENDING == mergewake.BRANCH_UPDATE_PENDING
    assert merge.MERGE_FIX_ROUNDS_MAX == mergewake.MERGE_FIX_ROUNDS_MAX


# ---------------------------------------------------------------------------
# 3. update-branch is asynchronous (MS3 review finding, 2026-10-06)
# ---------------------------------------------------------------------------

ACCEPTED = (202, {}, {"message": "Updating pull request branch."})


def test_an_update_whose_head_has_not_moved_yet_parks_pending_at_the_old_head(tmp_path):
    world = MergeWorld(tmp_path)
    world.pr["mergeable_state"] = "behind"
    world.update_answer = ACCEPTED  # GitHub accepted it, and has not moved the head yet

    outcome = merge.run_merge(world.context())

    assert len(world.update_calls()) == 1
    _parked(outcome, merge.BRANCH_UPDATE_PENDING, PINNED)
    # It re-read the head a bounded number of times before parking.
    assert world.slept == [merge.MERGEABLE_REREAD_SECONDS] * merge.MERGEABLE_REREADS
    assert outcome.summary["branch_updated"] == {"from": PINNED, "to": None, "updates": 1}
    assert world.merge_calls() == [], "the old head was merged after an update was asked for"


def test_an_update_whose_head_moves_on_a_reread_parks_at_the_new_head(tmp_path):
    world = MergeWorld(tmp_path)
    world.pr["mergeable_state"] = "behind"
    world.update_answer = ACCEPTED
    new_head = world.github_merge(PINNED)
    reads = {"n": 0}
    serve = world.github.routes[("GET", merge_world.PR)]

    def pull(seen):
        if world.update_calls():
            reads["n"] += 1
            if reads["n"] >= 2:
                world.pr["head"]["sha"] = new_head
        return serve(seen)

    world.github.route("GET", merge_world.PR, pull)

    outcome = merge.run_merge(world.context())

    _parked(outcome, "branch_updated", new_head)
    assert outcome.ci_wait["pending"] == []
    assert outcome.summary["branch_updated"]["to"] == new_head
    assert world.merge_calls() == []


def test_the_wake_after_a_pending_update_never_merges_the_old_head(tmp_path):
    """Woken by the fallback with GitHub's update still not made: the branch
    is still behind, so the step asks again, at the same head, and merges
    nothing."""
    world = MergeWorld(tmp_path)
    world.pr["mergeable_state"] = "behind"
    world.update_answer = ACCEPTED
    for _ in range(2):
        outcome = merge.run_merge(world.context())
        _parked(outcome, merge.BRANCH_UPDATE_PENDING, PINNED)
    assert [c.body for c in world.update_calls()] == [{"expected_head_sha": PINNED}] * 2
    assert world.merge_calls() == []
