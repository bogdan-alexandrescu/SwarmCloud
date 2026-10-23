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

from datetime import timedelta
from typing import Any

import pytest

from agent_worker import control as control_mod
from agent_worker import merge
from swarm_common.models import EndCause, utcnow
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


# ---------------------------------------------------------------------------
# No required check: every check, by the same rule (CHECK-RULE-2)
# ---------------------------------------------------------------------------
#
# BGD, 2026-10-22, answering CHECK-RULE's second question: "Yes, one rule for
# every check." With no required check the step holds on every check at the
# head, and judged EVERY run of it, so a superseded failure, or a superseded
# run still in progress, held the merge. Now each check is judged by the
# latest run of each event, with `other_check_blocks` (the tolerated set is
# unchanged, stricter than gate 5).

OTHER = "lint"


def _other_run(run_id: int, suite: int, *, conclusion: str | None = "success",
               status: str = "completed") -> dict[str, Any]:
    return {"id": run_id, "name": OTHER, "status": status, "conclusion": conclusion,
            "app": {"id": ACTIONS_APP}, "check_suite": {"id": suite}}


def _unprotected(world: MergeWorld, runs: list[dict[str, Any]], **events: int) -> None:
    world.rules = []
    world.branch = {"name": "main", "protected": False}
    world.runs = runs
    world.workflow_runs = [{"id": 900 + int(suite), "check_suite_id": int(suite), "event": event,
                            "head_sha": PINNED} for suite, event in events.items()]


def test_no_required_check_a_failure_re_run_green_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _unprotected(world, [_other_run(1, PUSH_SUITE, conclusion="failure"),
                         _other_run(2, RERUN_SUITE)],
                 **{str(PUSH_SUITE): "push", str(RERUN_SUITE): "push"})
    outcome = merge.run_merge(world.context())
    # MUTATION: judge every run instead of each event's latest -> checks_failed.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_no_required_check_a_run_in_progress_superseded_by_a_green_one_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _unprotected(world, [_other_run(1, PUSH_SUITE, conclusion=None, status="in_progress"),
                         _other_run(2, RERUN_SUITE)],
                 **{str(PUSH_SUITE): "push", str(RERUN_SUITE): "push"})
    outcome = merge.run_merge(world.context())
    # MUTATION: judge every run instead of each event's latest -> checks_pending.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_no_required_check_a_red_pull_request_run_beside_a_green_push_run_refuses(tmp_path):
    world = MergeWorld(tmp_path)
    _unprotected(world, [_other_run(1, PUSH_SUITE), _other_run(2, PR_SUITE, conclusion="failure")],
                 **{str(PUSH_SUITE): "push", str(PR_SUITE): "pull_request"})
    outcome = merge.run_merge(world.context())
    # MUTATION: take the newest run of the check whatever its event, or judge
    # only the first event -> one of the two cases merges.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    # MUTATION: label each run, not the deciding run per event -> fails.
    assert f"{OTHER} (push: success, pull_request: failure)" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_no_required_check_green_then_red_in_one_event_refuses(tmp_path):
    world = MergeWorld(tmp_path)
    _unprotected(world, [_other_run(1, PUSH_SUITE), _other_run(2, RERUN_SUITE, conclusion="failure")],
                 **{str(PUSH_SUITE): "push", str(RERUN_SUITE): "push"})
    outcome = merge.run_merge(world.context())
    # MUTATION: take the OLDEST run of each event -> merged.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert f"{OTHER} (push: failure)" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_no_required_check_still_waits_on_an_events_latest_run_in_progress(tmp_path):
    """The control: a run in progress that nothing superseded still holds."""
    world = MergeWorld(tmp_path)
    _unprotected(world, [_other_run(1, PUSH_SUITE),
                         _other_run(2, RERUN_SUITE, conclusion=None, status="in_progress")],
                 **{str(PUSH_SUITE): "push", str(RERUN_SUITE): "push"})
    outcome = merge.run_merge(world.context())
    # MUTATION: drop the not-completed rule from the no-required path -> merged.
    assert outcome.summary["wait"]["code"] == "checks_pending", outcome.summary
    # MUTATION: label each run, not the deciding run per event -> fails.
    assert outcome.ci_wait["pending"] == [f"{OTHER} (push: in_progress)"], outcome.ci_wait
    assert world.merge_calls() == []


# ---------------------------------------------------------------------------
# A run GitHub concluded but left `in_progress` (CHECK-STUCK, part of #2031)
# ---------------------------------------------------------------------------
#
# BGD, 2026-10-23: a check run that carries a conclusion AND a completed_at
# is judged by its conclusion, whatever `status` says. PR 2006's merge step
# parked CI_PENDING 23 times and failed `checks_timeout` on `secret scan`,
# which read `status: in_progress`, `conclusion: success`, `completed_at:
# 2026-10-22T19:05:52Z`; its workflow run was completed/success. A run with
# no conclusion stays pending; `completed` with no conclusion is not green
# (failed, as the step already read it).

STUCK_AT = "2026-10-22T19:05:52Z"


@pytest.mark.parametrize(("status", "conclusion", "completed_at", "finished"), [
    ("completed", "success", STUCK_AT, True),
    ("completed", None, None, True),
    ("in_progress", "success", STUCK_AT, True),
    ("in_progress", "failure", STUCK_AT, True),
    ("in_progress", None, None, False),
    ("in_progress", "success", None, False),
    ("in_progress", None, STUCK_AT, False),
    ("queued", "", "", False),
])
def test_a_run_is_finished_when_completed_or_concluded_with_a_completed_at(
        status, conclusion, completed_at, finished):
    run = {"status": status, "conclusion": conclusion, "completed_at": completed_at}
    # MUTATION: read status alone -> the in_progress rows with both a
    # conclusion and a completed_at read unfinished. MUTATION: accept either
    # field alone -> the rows with one of the two read finished.
    assert merge.check_run_finished(run) is finished, run


#: (case, status, conclusion, completed_at, required_check_state)
STUCK = [
    ("in_progress, success, completed_at", "in_progress", "success", STUCK_AT, "green"),
    ("in_progress, failure, completed_at", "in_progress", "failure", STUCK_AT, "failed"),
    ("in_progress, no conclusion", "in_progress", None, None, "pending"),
    ("completed, no conclusion", "completed", None, None, "failed"),
]


@pytest.mark.parametrize(("case", "status", "conclusion", "completed_at", "state"), STUCK,
                         ids=[row[0] for row in STUCK])
def test_a_stuck_run_is_judged_by_its_conclusion_in_every_worker_rule(
        case, status, conclusion, completed_at, state):
    facts = merge.CheckFacts(name=CHECK, status=status, conclusion=conclusion, run_id=1,
                             completed_at=completed_at)
    # MUTATION: read status alone in `_judge_latest` -> the first two rows
    # read "pending".
    assert merge.required_check_state([facts.as_run()]) == state, case
    # MUTATION: read status alone in `other_check_blocks` -> the first row
    # blocks.
    assert merge.other_check_blocks(facts.as_run()) is (state != "green"), case
    pending, failed = merge.judge_other_checks([facts])
    # MUTATION: bucket by `status == "completed"` in `judge_other_checks` ->
    # the second row is listed pending, not failed.
    assert (bool(pending), bool(failed)) == (state == "pending", state == "failed"), (pending, failed)
    # MUTATION: read status alone in `latest_reading` -> the record names a
    # concluded check pending, which the "Why it's waiting" panel shows.
    recorded = merge.latest_reading({CHECK: [facts]}, merge.REQUIRED_GREEN)
    assert recorded == ([CHECK] if state == "pending" else [],
                        [CHECK] if state == "failed" else []), recorded


def _stuck(conclusion: str = "success", completed_at: str | None = STUCK_AT) -> dict[str, Any]:
    """The check run as GitHub served PR 2006's `secret scan`."""
    return {"id": 120028057811, "name": CHECK, "status": "in_progress", "conclusion": conclusion,
            "completed_at": completed_at, "app": {"id": ACTIONS_APP}, "check_suite": {"id": PUSH_SUITE}}


def _waited_since(world: MergeWorld, seconds: float) -> None:
    world.docs[TASK]["metadata"] = {control_mod.MERGE_WAIT_METADATA_KEY: {
        "first_parked_at": utcnow() - timedelta(seconds=seconds), "wakes": 23, "updates": 0}}


@pytest.mark.parametrize("waited", [None, merge.MERGE_CI_MAX_SECONDS + 600],
                         ids=["first-reading", "past-6h"])
def test_the_merge_step_merges_on_a_run_concluded_success_but_left_in_progress(tmp_path, waited):
    """PR 2006's head: the step neither parks nor times out."""
    world = MergeWorld(tmp_path)
    world.runs = [_stuck()]
    if waited is not None:
        _waited_since(world, waited)

    outcome = merge.run_merge(world.context())

    # MUTATION: read status alone (`checks_at` drops `completed_at`, or the
    # rule ignores it) -> parks CI_PENDING, or past 6 h fails checks_timeout.
    assert outcome.state is TaskState.SUCCEEDED, (outcome.message, outcome.summary)
    assert outcome.ci_wait is None
    assert "wait" not in outcome.summary and "refusal" not in outcome.summary, outcome.summary
    assert len(world.merge_calls()) == 1


def test_the_merge_step_refuses_a_run_concluded_failure_but_left_in_progress(tmp_path):
    world = MergeWorld(tmp_path)
    world.runs = [_stuck("failure")]

    outcome = merge.run_merge(world.context())

    # MUTATION: read status alone -> parks CI_PENDING on a run that failed,
    # and past 6 h ends `checks_timeout` instead of naming the failure.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.ci_wait is None
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert f"{CHECK} (check suite {PUSH_SUITE}: failure)" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_the_merge_step_still_waits_on_a_run_with_a_conclusion_but_no_completed_at(tmp_path):
    """The control: a conclusion alone does not finish a run."""
    world = MergeWorld(tmp_path)
    world.runs = [_stuck(completed_at=None)]

    outcome = merge.run_merge(world.context())

    # MUTATION: judge by the conclusion alone -> merged.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == "checks_pending", outcome.ci_wait
    assert world.merge_calls() == []


def test_no_required_check_a_run_concluded_success_but_left_in_progress_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _unprotected(world, [{**_other_run(1, PUSH_SUITE, status="in_progress"), "completed_at": STUCK_AT}])
    outcome = merge.run_merge(world.context())
    # MUTATION: read status alone in `other_check_blocks` -> checks_pending.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_no_required_check_a_run_concluded_failure_but_left_in_progress_refuses(tmp_path):
    world = MergeWorld(tmp_path)
    _unprotected(world, [{**_other_run(1, PUSH_SUITE, conclusion="failure", status="in_progress"),
                          "completed_at": STUCK_AT}])
    outcome = merge.run_merge(world.context())
    # MUTATION: bucket by `status == "completed"` in `judge_other_checks` ->
    # checks_pending, a 6 h wait on a run that failed.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert f"{OTHER} (check suite {PUSH_SUITE}: failure)" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []
