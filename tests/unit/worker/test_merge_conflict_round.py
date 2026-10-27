"""A CONFLICTING pull request goes to a conflict fix round, never to a wait for checks (#1076).

Measured on PR 1028 (workflow wf_14b4278c117f47fda785, merge task
task_9c21161ee37a46b48f0e): the step waited 366 min and refused
`checks_timeout`, while GitHub said mergeable CONFLICTING and "no checks
reported" -- GitHub runs no checks on a pull request that conflicts with its
base. The step now reads mergeability BEFORE the checks; a conflict parks
for a conflict fix round (submitted by swarm-api's wake tick, as a CI-fix
round is), whose pushed head is then a fix round's head. With the rounds
spent it fails fast with `merge_conflict`, naming the pull request.

What the worker holds here:

  1. CONFLICTING (`mergeable` false, or `mergeable_state` dirty) parks
     CONFLICT_FIX_PENDING without reading a single check.
  2. A round running at this head parks CONFLICT_FIX_RUNNING; one that ended
     at this head, or was refused, is `merge_conflict`; with every round
     spent, `merge_conflict` at once, naming the pull request.
  3. The head a conflict fix round pushed is accepted and merged there.
  4. A clean pull request is unchanged: no round, no extra read.

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import API, NUMBER, OPENER, PINNED, TASK, TENANT, MergeWorld

ROUND_TASK = "task_conflict1"
RESOLVED = "7" * 40


def _conflicting(world: MergeWorld, *, mergeable: Any = False, state: str = "dirty") -> None:
    world.pr.update(mergeable=mergeable, mergeable_state=state)
    # GitHub reports no checks on a conflicting pull request.
    world.runs = []


def _round(world: MergeWorld, *, task_id: str | None = ROUND_TASK, head: str = PINNED,
           **extra: Any) -> None:
    entry: dict[str, Any] = {"round": 1, "head": head, **extra}
    if task_id is not None:
        entry["task_id"] = task_id
        entry["workflow_id"] = "wf_conflict"
    world.docs[TASK]["metadata"] = {
        merge.MERGE_FIX_METADATA_KEY: {merge.CONFLICT_ROUNDS_KEY: [entry]},
    }


def _round_task(world: MergeWorld, *, state: str = "SUCCEEDED", pushed: str | None = RESOLVED,
                continues: str = OPENER) -> None:
    world.docs[ROUND_TASK] = {
        "tenant_id": TENANT, "workflow_id": "wf_conflict", "state": state,
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": continues}},
        "result_summary": {"git": {"pushed_head": pushed} if pushed else {}},
    }


def _check_reads(world: MergeWorld) -> list[Any]:
    return [s for s in world.github.seen if s.method == "GET"
            and s.path.startswith(f"{API}/commits/") and s.path.endswith(("/check-runs", "/status"))]


def _parked(outcome, code: str, head: str = PINNED) -> None:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.end_cause is None
    assert outcome.ci_wait["code"] == code, outcome.ci_wait
    assert outcome.ci_wait["head"] == head and outcome.ci_wait["pull_request"] == NUMBER
    assert "refusal" not in outcome.summary


@pytest.mark.parametrize(("mergeable", "state"), [(False, "dirty"), (False, "unknown"),
                                                  (None, "dirty")])
def test_conflicting_starts_the_conflict_fixer_and_never_reads_the_checks(
    tmp_path, mergeable, state
):
    world = MergeWorld(tmp_path)
    _conflicting(world, mergeable=mergeable, state=state)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_PENDING)
    assert f"#{NUMBER} conflicts with main" in outcome.summary["wait"]["message"]
    assert "round 1 of 2" in outcome.summary["wait"]["message"]
    assert _check_reads(world) == [], "checks were read on a conflicting pull request"
    assert world.merge_calls() == [] and world.update_calls() == []


def test_a_conflict_park_gives_the_merge_slot_back(tmp_path):
    """A round runs an agent for as long as it takes: the slot is not held through it."""
    assert merge.CONFLICT_FIX_PENDING in merge._RELEASE_ON_PARK
    assert merge.CONFLICT_FIX_RUNNING in merge._RELEASE_ON_PARK


def test_a_conflict_round_running_at_this_head_parks(tmp_path):
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world)
    _round_task(world, state="RUNNING", pushed=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_RUNNING)
    assert _check_reads(world) == []


def test_a_conflict_round_claimed_and_not_yet_submitted_parks(tmp_path):
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world, task_id=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_RUNNING)


def _refused(outcome) -> str:
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "merge_conflict", outcome.summary
    return outcome.summary["refusal"]["message"]


def test_exhausted_conflict_rounds_fail_fast_with_merge_conflict(tmp_path):
    world = MergeWorld(tmp_path)
    _conflicting(world)
    world.spend_conflict_rounds()

    message = _refused(merge.run_merge(world.context()))

    assert f"pull request #{NUMBER}" in message
    assert f"after {merge.MERGE_CONFLICT_ROUNDS_MAX} conflict fix round(s)" in message
    assert _check_reads(world) == []
    assert world.merge_calls() == []


EARLIER = "a" * 40


def test_a_conflict_round_running_at_an_earlier_head_parks_running_not_pending(tmp_path):
    """#2577: on 2026-10-27 the merge parked conflict_fix_pending at 40d1cf15
    while round 1 (task_78214f85, claimed at a8cb2f5) was still running, and
    the tick submitted round 2 onto the same branch. One writer per pull
    request: a live round at any head holds the next one."""
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world, head=EARLIER)
    _round_task(world, state="RUNNING", pushed=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_RUNNING)
    message = outcome.summary["wait"]["message"]
    assert "round 1" in message and ROUND_TASK in message
    assert _check_reads(world) == []


def test_a_conflict_round_that_ended_at_an_earlier_head_leaves_the_next_round_due(tmp_path):
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world, head=EARLIER)
    _round_task(world, state="SUCCEEDED", pushed=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_PENDING)
    assert "round 2 of 2" in outcome.summary["wait"]["message"]


def test_spent_rounds_with_the_last_still_running_park_instead_of_refusing(tmp_path):
    world = MergeWorld(tmp_path)
    _conflicting(world)
    world.spend_conflict_rounds()
    rounds = world.docs[TASK]["metadata"][merge.MERGE_FIX_METADATA_KEY][merge.CONFLICT_ROUNDS_KEY]
    rounds[-1].update(task_id=ROUND_TASK, workflow_id="wf_conflict")
    _round_task(world, state="RUNNING", pushed=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_RUNNING)
    assert ROUND_TASK in outcome.summary["wait"]["message"]


def test_a_running_ci_fix_round_at_another_head_holds_the_conflict_round(tmp_path):
    world = MergeWorld(tmp_path)
    _conflicting(world)
    world.docs[TASK]["metadata"] = {merge.MERGE_FIX_METADATA_KEY: {
        "rounds": [{"round": 1, "head": EARLIER, "task_id": ROUND_TASK,
                    "workflow_id": "wf_conflict"}]}}
    _round_task(world, state="RUNNING", pushed=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_RUNNING)
    message = outcome.summary["wait"]["message"]
    assert "round 1" in message and ROUND_TASK in message


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_a_conflict_round_that_ended_at_this_head_is_merge_conflict(tmp_path, state):
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world)
    _round_task(world, state=state, pushed=None)

    message = _refused(merge.run_merge(world.context()))

    assert "conflict fix round 1" in message and ROUND_TASK in message


def test_a_refused_conflict_round_is_merge_conflict(tmp_path):
    world = MergeWorld(tmp_path)
    _conflicting(world)
    _round(world, task_id=None, error="the submitter is no longer a member")

    assert "was not run" in _refused(merge.run_merge(world.context()))


def test_a_named_pull_request_that_conflicts_fails_fast(tmp_path):
    """A `merge_pr` target is on a branch no SwarmCloud task pushes: no round can fix it."""
    world = MergeWorld(tmp_path)
    world.target = {"number": NUMBER, "head_sha": PINNED}
    _conflicting(world)

    message = _refused(merge.run_merge(world.context()))

    assert "no conflict fix round" in message
    assert _check_reads(world) == []


def test_a_conflict_at_update_time_goes_to_a_round(tmp_path):
    world = MergeWorld(tmp_path)
    world.pr["mergeable_state"] = "behind"
    world.update_answer = (422, {}, {"message": "merge conflict between base and head"})

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_PENDING)
    assert world.merge_calls() == []


def test_a_conflict_the_merge_call_found_goes_to_a_round(tmp_path):
    world = MergeWorld(tmp_path)
    world.merge_answer = (405, {}, {"message": "Merge conflict between base and head"})

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CONFLICT_FIX_PENDING)
    assert "merge_called" not in outcome.summary
    assert len(world.merge_calls()) == 1


def test_the_head_a_conflict_round_pushed_is_accepted_and_merged_there(tmp_path):
    world = MergeWorld(tmp_path)
    _round(world)
    _round_task(world)
    world.pr["head"]["sha"] = RESOLVED
    world.serve_checks(RESOLVED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == RESOLVED
    assert outcome.summary["head_pushed_by"] == ROUND_TASK
    # Verified against the round's own workflow, which is not the merge's.
    assert (ROUND_TASK, "wf_conflict") in world.verified


def test_a_conflict_round_continuing_another_root_is_head_moved(tmp_path):
    world = MergeWorld(tmp_path)
    _round(world)
    _round_task(world, continues="task_other")
    world.pr["head"]["sha"] = RESOLVED
    world.serve_checks(RESOLVED)

    assert merge.run_merge(world.context()).summary["refusal"]["code"] == "head_moved"


def test_a_ci_fix_rounds_head_is_verified_against_the_rounds_own_workflow(tmp_path):
    """A CI-fix round is its own workflow too: verified against this merge's,
    every round's head would be refused (`workflow_mismatch`)."""
    world = MergeWorld(tmp_path)
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 1, merge.MERGE_FIX_METADATA_KEY: {
        "rounds": [{"round": 1, "head": PINNED, "task_id": ROUND_TASK,
                    "workflow_id": "wf_conflict"}]}}
    _round_task(world)
    world.pr["head"]["sha"] = RESOLVED
    world.serve_checks(RESOLVED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert (ROUND_TASK, "wf_conflict") in world.verified


def test_a_clean_pull_request_is_unchanged(tmp_path):
    world = MergeWorld(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    assert "wait" not in outcome.summary and "refusal" not in outcome.summary


def test_mergeability_not_computed_yet_still_reads_the_checks(tmp_path):
    """`mergeable` null is not a conflict: the checks are read as before."""
    world = MergeWorld(tmp_path)
    world.pr.update(mergeable=None, mergeable_state="unknown")
    world.runs[0].update(status="in_progress", conclusion=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, "checks_pending")
    assert _check_reads(world) != []


def test_the_tick_and_the_worker_agree_on_the_conflict_rounds():
    from swarm_api import mergewake

    assert merge.CONFLICT_ROUNDS_KEY == mergewake.CONFLICT_ROUNDS_KEY
    assert merge.MERGE_CONFLICT_ROUNDS_MAX == mergewake.MERGE_CONFLICT_ROUNDS_MAX
