"""After a merge, the merge step deletes the pull request's platform branch
(the owner, 2026-11-02: clean up a task's branch once its changes are merged).

The owner's rule: a branch is deleted only when a pull request from it merged
and its head is still that pull request's head commit. Never the default
branch, a protected branch, a branch another open pull request is from, or a
`keep/` branch. A branch already gone (GitHub's auto-delete) counts as
deleted; a failed delete never fails the merge, it is recorded for the daily
sweep. `merge_world.MergeWorld`'s pull request is from `swarm/task_int`, the
opener's own branch.
"""

from __future__ import annotations

import json

import pytest

from agent_worker import merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import API, OPENER, OTHER, PINNED, PR, MergeWorld

BRANCH = f"swarm/{OPENER}"
BRANCH_PATH = f"{API}/branches/swarm%2F{OPENER}"
REF_PATH = f"{API}/git/refs/heads/{BRANCH}"


def _world(tmp_path, *, sha: str = PINNED, protected: bool = False,
           open_pulls: list | None = None, delete=(204, {}, None)) -> MergeWorld:
    world = MergeWorld(tmp_path)
    world.github.route("GET", BRANCH_PATH, (200, {}, {
        "name": BRANCH, "commit": {"sha": sha}, "protected": protected}))
    world.github.route("GET", f"{API}/pulls", (200, {}, open_pulls or []))
    world.github.route("DELETE", REF_PATH, delete)
    return world


def _deletes(world: MergeWorld) -> list:
    return world.github.calls("DELETE", REF_PATH)


def test_a_merged_pull_requests_platform_branch_is_deleted_after_the_merge(tmp_path):
    world = _world(tmp_path)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(_deletes(world)) == 1
    order = [(s.method, s.path) for s in world.github.seen]
    assert order.index(("PUT", f"{PR}/merge")) < order.index(("DELETE", REF_PATH))
    (pulls,) = world.github.calls("GET", f"{API}/pulls")
    assert pulls.query == {"state": ["open"], "head": [f"octo-org:{BRANCH}"]}
    assert outcome.summary["branch_deleted"] is True
    assert outcome.summary["branch_cleanup"] == {
        "branch": BRANCH, "head": PINNED, "outcome": "deleted"}


def test_a_head_moved_after_the_merge_is_kept(tmp_path):
    world = _world(tmp_path, sha=OTHER)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert _deletes(world) == []
    assert outcome.summary["branch_deleted"] is False
    assert outcome.summary["branch_cleanup"]["outcome"] == "kept"
    assert outcome.summary["branch_cleanup"]["reason"] == "head_moved"
    assert outcome.summary["branch_cleanup"]["branch_head"] == OTHER


@pytest.mark.parametrize("kwargs, reason", [
    ({"protected": True}, "protected"),
    ({"open_pulls": [{"number": 99}]}, "open_pull_request"),
])
def test_a_protected_branch_or_one_with_an_open_pull_request_is_kept(tmp_path, kwargs, reason):
    world = _world(tmp_path, **kwargs)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert _deletes(world) == []
    assert outcome.summary["branch_deleted"] is False
    assert outcome.summary["branch_cleanup"]["reason"] == reason


def test_a_branch_that_is_not_the_workflows_own_is_kept(tmp_path):
    world = _world(tmp_path)
    world.pr["head"]["ref"] = "feature/by-hand"
    outcome = merge.run_merge(world.context())

    # The pull request is refused as not this workflow's, and nothing is deleted.
    assert outcome.state is not TaskState.SUCCEEDED
    assert _deletes(world) == []


def test_a_branch_github_already_deleted_counts_as_deleted(tmp_path):
    world = _world(tmp_path)
    world.github.route("GET", BRANCH_PATH, (404, {}, {"message": "Branch not found"}))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert _deletes(world) == []
    assert outcome.summary["branch_deleted"] is True
    assert outcome.summary["branch_cleanup"]["outcome"] == "already_deleted"


@pytest.mark.parametrize("answer", [
    (404, {}, {"message": "Not Found"}),
    (422, {}, {"message": "Reference does not exist"}),
])
def test_a_delete_answered_gone_counts_as_deleted(tmp_path, answer):
    world = _world(tmp_path, delete=answer)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(_deletes(world)) == 1
    assert outcome.summary["branch_deleted"] is True
    assert outcome.summary["branch_cleanup"]["outcome"] == "already_deleted"


@pytest.mark.parametrize("answer", [
    (403, {}, {"message": "Resource not accessible by integration"}),
    (422, {}, {"message": "Cannot delete this protected branch"}),
    (502, {}, {"message": "Bad Gateway"}),
])
def test_a_failed_delete_leaves_the_merge_succeeded_and_recorded(tmp_path, answer):
    world = _world(tmp_path, delete=answer)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.end_cause is None
    assert outcome.summary["merged_by_this_task"] is True
    assert outcome.summary["branch_deleted"] is False
    assert outcome.summary["branch_cleanup"]["outcome"] == "failed"
    assert outcome.summary["branch_cleanup"]["reason"]
    # The token is in no record.
    assert world.token not in json.dumps(outcome.summary, default=str)
    assert all(world.token not in line for line in world.log.lines)


def test_an_unreadable_branch_leaves_the_merge_succeeded_and_recorded(tmp_path):
    world = _world(tmp_path)
    world.github.route("GET", BRANCH_PATH, (500, {}, {"message": "Server Error"}))
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.end_cause is not EndCause.MERGE_FAILED
    assert _deletes(world) == []
    assert outcome.summary["branch_cleanup"]["outcome"] == "failed"


def test_an_already_merged_pull_request_still_deletes_its_unmoved_branch(tmp_path):
    """A lost attempt that merged and never reached the delete: the retry does it."""
    world = _world(tmp_path)
    world.pr.update(merged=True, state="closed", merge_commit_sha="d" * 40)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["already_merged"] is True
    assert len(_deletes(world)) == 1
    assert outcome.summary["branch_deleted"] is True
