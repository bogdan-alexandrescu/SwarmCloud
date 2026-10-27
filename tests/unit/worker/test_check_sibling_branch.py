"""A push suite from another branch at the same sha is not this PR's result (#2365).

`commits/{sha}/check-runs` lists every check suite at the sha, from whichever
branch pushed it. swarm/ lanes branch from one another, so two branches can
carry the one commit: at edb9c942 (PR #2340) the sibling branch
swarm/task_43f879fb's red push ci-gate (run 40185474045) stood as the PR's
push result while the PR branch's own push run (40186863356) was still
running, and the merge step refused `checks_failed: ci-gate (push: failure)`.

The merge step now reads each Actions run's `head_branch` beside its event
and drops a push suite whose branch is known and is not the PR's head ref.
pull_request suites, commit statuses and suites whose branch is unknown are
kept as they were.

No credentials, no network, no emulator. Each assertion names the mutation it
catches.
"""

from __future__ import annotations

from typing import Any

from agent_worker import merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import ACTIONS_APP, API, CHECK, PINNED, MergeWorld

SIBLING_SUITE = 201
OWN_SUITE = 202
SIBLING = "swarm/task_sibling"


def _gh_run(run_id: int, conclusion: str | None, suite: int, *,
            status: str = "completed") -> dict[str, Any]:
    return {"id": run_id, "name": CHECK, "status": status, "conclusion": conclusion,
            "app": {"id": ACTIONS_APP}, "check_suite": {"id": suite}}


def _suite(suite: int, event: str, branch: str) -> dict[str, Any]:
    return {"id": 900 + suite, "check_suite_id": suite, "event": event,
            "head_branch": branch, "head_sha": PINNED}


def _head_ref(world: MergeWorld) -> str:
    return world.pr["head"]["ref"]


def test_a_red_push_suite_on_a_sibling_branch_beside_a_pending_one_on_the_pr_branch_is_pending(
        tmp_path):
    """#2365's shape: the sibling's red push run and the PR's own push run
    still in progress, at one sha. The sibling's check run is the newer one
    (it is created when its job starts), so within the push event it is the
    latest run unless it is dropped."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(2, "failure", SIBLING_SUITE),
                  _gh_run(1, None, OWN_SUITE, status="in_progress")]
    world.workflow_runs = [_suite(SIBLING_SUITE, "push", SIBLING),
                           _suite(OWN_SUITE, "push", _head_ref(world))]

    outcome = merge.run_merge(world.context())

    # MUTATION: keep every push suite at the sha (no head_branch filter) ->
    # the sibling's red run 2 is the push event's latest -> MERGE_REFUSED
    # checks_failed.
    assert outcome.end_cause is not EndCause.MERGE_REFUSED, outcome.summary
    assert world.merge_calls() == []
    # MUTATION: drop the PR branch's own suite instead of the sibling's ->
    # the sibling's red run alone decides -> checks_failed, not this wait.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.summary["wait"]["code"] == "checks_pending", outcome.summary
    assert outcome.ci_wait["pending"] == [CHECK], outcome.ci_wait


def test_a_lone_red_push_suite_on_a_sibling_branch_is_not_a_failure(tmp_path):
    """The sibling's red run is the only ci-gate run: no name repeats, so the
    Actions read must also be made because a run finished red."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(1, "failure", SIBLING_SUITE)]
    world.workflow_runs = [_suite(SIBLING_SUITE, "push", SIBLING)]

    outcome = merge.run_merge(world.context())

    # MUTATION: read actions/runs only when a check name repeats -> the red
    # sibling run is never filtered -> MERGE_REFUSED checks_failed.
    assert outcome.end_cause is not EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["wait"]["code"] == "checks_pending", outcome.summary
    assert world.merge_calls() == []
    assert world.github.calls("GET", f"{API}/actions/runs"), "the suites were never read"


def test_a_lone_red_push_suite_on_a_sibling_branch_with_no_required_check_is_no_checks(tmp_path):
    """The all-checks path: with the sibling's run dropped, no check has reported."""
    world = MergeWorld(tmp_path)
    world.rules = []
    world.branch = {"name": "main", "protected": False}
    world.runs = [_gh_run(1, "failure", SIBLING_SUITE)]
    world.workflow_runs = [_suite(SIBLING_SUITE, "push", SIBLING)]

    outcome = merge.run_merge(world.context())

    # MUTATION: filter only the required-check path -> checks_failed here.
    assert outcome.end_cause is not EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["wait"]["code"] == "no_checks", outcome.summary
    assert world.merge_calls() == []


def test_a_red_push_suite_on_the_pr_branch_still_refuses(tmp_path):
    """The control: the red push suite is the PR branch's own."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(2, "failure", OWN_SUITE),
                  _gh_run(1, None, SIBLING_SUITE, status="in_progress")]
    world.workflow_runs = [_suite(OWN_SUITE, "push", _head_ref(world)),
                           _suite(SIBLING_SUITE, "push", SIBLING)]

    outcome = merge.run_merge(world.context())

    # MUTATION: drop every push suite (or compare against the base ref) ->
    # the PR's own red run is dropped -> checks_pending, not a refusal.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert f"{CHECK} (push: failure)" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_a_pull_request_suite_is_never_dropped_by_branch(tmp_path):
    """A fork's pull_request suite names the fork's branch: it is still the PR's."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(1, "failure", SIBLING_SUITE)]
    world.workflow_runs = [_suite(SIBLING_SUITE, "pull_request", "fork-owner/feature")]

    outcome = merge.run_merge(world.context())

    # MUTATION: drop every suite whose branch is not the head ref, whatever
    # its event -> the red pull_request run is dropped -> checks_pending.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert world.merge_calls() == []


def test_without_the_actions_read_nothing_is_dropped(tmp_path):
    """A 403 on actions/runs knows no event and no branch: the red run stays,
    grouped by its check suite, and the step refuses as before."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(1, "failure", SIBLING_SUITE)]
    world.workflow_runs = [_suite(SIBLING_SUITE, "push", SIBLING)]
    world.github.route("GET", f"{API}/actions/runs",
                       (403, {}, {"message": "Resource not accessible by integration"}))

    outcome = merge.run_merge(world.context())

    # MUTATION: treat an unknown branch as another branch -> dropped -> checks_pending.
    # MUTATION: let the 403 escape the read -> an unavailable outcome, not this refusal.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert world.merge_calls() == []
