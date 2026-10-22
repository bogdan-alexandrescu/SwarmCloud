"""A required check is decided by the latest run of EACH event (CHECK-RULE; part of #1999 and #1778).

The merge step called a required check green only when EVERY run of it at
the head had succeeded, so one earlier flaky run kept it red after a later
run of the same check passed: PR 1966's ci-gate read `(failure, success)`,
the step spent a CI-fix round on a head that was already green and then
refused `checks_failed` (#1999); #1778 is the same site with
`(cancelled, success)`.

Owner decision, BGD 2026-10-22: a required check is decided by the LATEST
run of each event that ran it (push, pull_request, any other). A re-run
replaces the earlier runs of its own event; every event's latest run must be
green. CI-EARLY's guarantee (#1694, test_ci_early.py) holds: a red push run is
never outvoted by a green pull_request run. The same rule is swarm-api's
`forgechecks.required_check_state` (test_forgechecks_per_event.py runs both
over one table).

No credentials, no network, no emulator. Each assertion names the mutation it
catches.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import ACTIONS_APP, API, CHECK, PINNED, TASK, MergeWorld

PUSH_SUITE = 101
PR_SUITE = 102
RERUN_SUITE = 103


def _run(run_id: int, conclusion: str | None, event: str | None, *,
         status: str = "completed") -> dict[str, Any]:
    """One run of a required check, as `CheckFacts.as_run` hands it over."""
    return {"name": CHECK, "status": status, "conclusion": conclusion,
            "run_id": run_id, "event": event}


# ---------------------------------------------------------------------------
# The rule, over one run list per case
# ---------------------------------------------------------------------------


def test_a_failed_push_run_re_run_green_is_green():
    # MUTATION: judge every run at the head (`all(...)` over the runs, the
    # old rule) -> the failure(1) still holds the check -> "failed".
    assert merge.required_check_state(
        [_run(1, "failure", "push"), _run(2, "success", "push")]) == "green"


def test_a_green_push_run_and_a_red_pull_request_run_is_failed():
    # MUTATION: judge only the push event (the first group) -> "green".
    assert merge.required_check_state(
        [_run(1, "success", "push"), _run(2, "failure", "pull_request")]) == "failed"
    # MUTATION: drop the per-event grouping and take the newest run overall
    # -> the pull_request run's success (2) outvotes the red push run -> "green".
    assert merge.required_check_state(
        [_run(1, "failure", "push"), _run(2, "success", "pull_request")]) == "failed"


def test_a_green_push_run_then_a_red_push_run_is_failed():
    # MUTATION: take the OLDEST run of each event (ordered[0]) -> "green".
    assert merge.required_check_state(
        [_run(1, "success", "push"), _run(2, "failure", "push")]) == "failed"


def test_a_cancelled_run_superseded_by_a_success_is_green():
    """#1778's shape."""
    # MUTATION: count `cancelled` as superseded only when listed first (order
    # by position, not run id) -> listed here newest-first, it is taken -> "failed".
    assert merge.required_check_state(
        [_run(2, "success", "push"), _run(1, "cancelled", "push")]) == "green"


@pytest.mark.parametrize("runs", [
    [_run(1, "success", "push"), _run(2, None, "pull_request", status="in_progress")],
    [_run(1, "failure", "push"), _run(2, None, "push", status="queued")],
])
def test_any_event_whose_latest_run_is_still_running_is_pending(runs):
    # MUTATION: judge only completed runs (skip the not-completed latest) ->
    # the first case reads "green", the second "failed".
    assert merge.required_check_state(runs) == "pending"


def test_a_running_run_superseded_within_its_event_does_not_hold_the_check():
    # MUTATION: keep the old `any(status != completed)` over every run -> "pending".
    assert merge.required_check_state(
        [_run(1, None, "push", status="in_progress"), _run(2, "success", "push")]) == "green"


def test_the_failed_label_names_the_latest_run_per_event():
    runs = [_run(1, "failure", "push"), _run(2, "success", "push"),
            _run(3, "failure", "pull_request")]
    # MUTATION: build the label from the SET of every run's conclusion (the
    # old `sorted({...})`) -> "failure, success".
    # MUTATION: order the events by name, not by when their latest run ran ->
    # "pull_request: failure, push: success".
    assert merge.required_check_label(runs) == "push: success, pull_request: failure"


def test_runs_with_no_event_are_one_group_ordered_by_run_id():
    """A commit status, a test's run with no event: as before, one group."""
    # MUTATION: give each event-less run its own group -> "failed".
    assert merge.required_check_state(
        [_run(2, "success", None), _run(1, "failure", None)]) == "green"
    # MUTATION: label an event-less group "None: ..." -> fails.
    assert merge.required_check_label([_run(1, "failure", None)]) == "failure"


# ---------------------------------------------------------------------------
# The forge read: the event comes from the check suite's workflow run
# ---------------------------------------------------------------------------


def _gh_run(run_id: int, conclusion: str, suite: int) -> dict[str, Any]:
    return {"id": run_id, "name": CHECK, "status": "completed", "conclusion": conclusion,
            "app": {"id": ACTIONS_APP}, "check_suite": {"id": suite}}


def _suites(world: MergeWorld, **events: int) -> None:
    world.workflow_runs = [{"id": 900 + suite, "check_suite_id": suite, "event": event,
                            "head_sha": PINNED} for event, suite in events.items()]


def test_the_merge_step_with_a_re_run_green_merges_without_a_fix_round(tmp_path):
    """PR 1966's shape: ci-gate (failure, success) at one head, one event."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(1, "failure", PUSH_SUITE), _gh_run(2, "success", RERUN_SUITE)]
    world.workflow_runs = [
        {"id": 901, "check_suite_id": PUSH_SUITE, "event": "pull_request", "head_sha": PINNED},
        {"id": 903, "check_suite_id": RERUN_SUITE, "event": "pull_request", "head_sha": PINNED},
    ]
    # A red reading would park CI_FIX_PENDING for a round: one is left.
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 1}

    outcome = merge.run_merge(world.context())

    # MUTATION: keep the every-run rule -> CI_FIX_PENDING park, no merge.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    # MUTATION: refuse `checks_failed` on the superseded failure -> fails.
    assert outcome.end_cause is not EndCause.MERGE_REFUSED, outcome.summary
    assert len(world.merge_calls()) == 1
    # MUTATION: never read the event (no `actions/runs` call) -> fails.
    assert world.github.calls("GET", f"{API}/actions/runs"), "the event was never read"


def test_the_merge_step_holds_a_red_push_run_beside_a_green_pull_request_run(tmp_path):
    """CI-EARLY: the green pull_request run does not outvote the red push run."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(1, "failure", PUSH_SUITE), _gh_run(2, "success", PR_SUITE)]
    _suites(world, push=PUSH_SUITE, pull_request=PR_SUITE)

    outcome = merge.run_merge(world.context())

    # MUTATION: take the newest run overall -> merged.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    # MUTATION: label from the set of conclusions -> "(failure, success)".
    assert f"{CHECK} (push: failure, pull_request: success)" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_without_the_event_read_runs_are_grouped_by_check_suite(tmp_path):
    """A credential without `actions: read` (403) still never lets a green
    pull_request suite outvote a red push suite: each suite is its own group."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(1, "failure", PUSH_SUITE), _gh_run(2, "success", PR_SUITE)]
    world.github.route("GET", f"{API}/actions/runs",
                       (403, {}, {"message": "Resource not accessible by integration"}))

    outcome = merge.run_merge(world.context())

    # MUTATION: read every unknown event as one group -> merged.
    # MUTATION: let the 403 escape the read -> an unavailable outcome, not this refusal.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert world.merge_calls() == []


def test_a_single_run_per_check_reads_no_workflow_runs(tmp_path):
    """The event is read only when a check has more than one run: the usual
    head costs no extra request."""
    world = MergeWorld(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    # MUTATION: read `actions/runs` on every head -> fails.
    assert world.github.calls("GET", f"{API}/actions/runs") == []
