"""The merge action on a pull request NAMED by number and head sha (#352, MS0 question 2).

Owner decision 2026-10-07: a merge-only workflow may name a pull request no
workflow opened, as `merge_pr: {number, head_sha}`; swarm-api resolves the
repository from the tenant's registry and writes `merge_target: {number,
head_sha, base}` into the signed dispatch block. The worker then merges that
pull request at that head, through the same gate as every merge step: the
required checks green at the head, no fork, open, the registered base.

`MergeWorld` is green; each test here names the pull request instead of the
task that opened it, on a branch no SwarmCloud task pushed, and the opener's
document is removed so a merge that still read it would fail.
"""

from __future__ import annotations

import pytest

from agent_worker import merge
from swarm_common.states import TaskState

from merge_world import NUMBER, OPENER, OTHER, PINNED, MergeWorld


def _named(world: MergeWorld) -> MergeWorld:
    world.target = {"number": NUMBER, "head_sha": PINNED, "base": "main"}
    world.verdict = None
    world.pr["head"] = {**world.pr["head"], "ref": "feature/price-sort"}
    del world.docs[OPENER]
    return world


def test_swarm_apis_named_target_is_one_the_worker_reads():
    from swarm_api.validation import DispatchOptions

    block = DispatchOptions(strategy="direct-pr", carrier="patches").with_merge_target(
        number=NUMBER, head_sha=PINNED, base="main",
    ).to_metadata()
    assert merge.parse_merge_target(block) == merge.MergeTarget(
        None, number=NUMBER, head_sha=PINNED, base="main",
    )


def test_a_named_pull_request_green_at_its_head_is_merged_at_that_head(tmp_path):
    world = _named(MergeWorld(tmp_path))
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PINNED
    assert outcome.summary["pull_request"] == NUMBER
    assert outcome.summary["pinned"] == PINNED
    assert "pull_request_task" not in outcome.summary


def test_a_named_pull_request_whose_head_moved_is_refused(tmp_path):
    world = _named(MergeWorld(tmp_path))
    world.pr["head"] = {**world.pr["head"], "sha": OTHER}
    world.serve_checks(OTHER)
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "head_moved", outcome.message
    assert world.merge_calls() == []


def test_a_named_pull_request_red_at_its_head_is_refused(tmp_path):
    world = _named(MergeWorld(tmp_path))
    world.runs = [{**world.runs[0], "conclusion": "failure"}]
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "checks_failed", outcome.message
    assert world.merge_calls() == []


def test_a_named_pull_request_from_a_fork_is_refused(tmp_path):
    world = _named(MergeWorld(tmp_path))
    world.pr["head"] = {**world.pr["head"], "repo": {"full_name": "someone/widget-shop"}}
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "from_fork", outcome.message
    assert world.merge_calls() == []


def test_a_named_pull_request_that_was_closed_is_refused(tmp_path):
    world = _named(MergeWorld(tmp_path))
    world.pr["state"] = "closed"
    outcome = merge.run_merge(world.context())
    assert outcome.summary["refusal"]["code"] == "pull_request_closed", outcome.message
    assert world.merge_calls() == []


@pytest.mark.parametrize("target", [
    {"number": NUMBER},
    {"head_sha": PINNED},
    {"number": 0, "head_sha": PINNED},
    {"number": True, "head_sha": PINNED},
    {"number": "41", "head_sha": PINNED},
    {"number": NUMBER, "head_sha": "abc"},
    {"number": NUMBER, "head_sha": PINNED, "pull_request": OPENER},
    {"number": NUMBER, "head_sha": PINNED, "review": "task_rev", "verdict_file": "v.json"},
])
def test_a_named_target_that_is_not_exactly_a_number_and_a_sha_is_invalid(target):
    with pytest.raises(merge.TargetInvalid):
        merge.parse_merge_target({"merge_target": target})
