"""A cancelled-only reading waits at most 60 minutes, then refuses `checks_cancelled` (#1778).

BGD's answer of 2026-10-27 ("refuse after a shorter bound", 60 min): a
cancelled latest run is pending, not failed, but when it is the ONLY problem
in a reading -- nothing failed, nothing still running -- the merge step waits
`MERGE_CI_CANCELLED_MAX_SECONDS` from the first such reading at the head,
then refuses with its own reason. A re-run that goes green inside the bound
merges as usual; a reading with anything still running keeps the 6 h
`MERGE_CI_MAX_SECONDS`.

The first such reading is when the reading's last deciding run finished
(`merge.cancelled_since`): from then on nothing in it changes.

No credentials, no network, no emulator. Each assertion names the mutation it
catches.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_worker import control as control_mod
from agent_worker import merge
from swarm_common.models import EndCause, utcnow
from swarm_common.states import TaskState

from merge_world import ACTIONS_APP, API, CHECK, TASK, MergeWorld

LINT = "ci / lint"
BOUND = merge.MERGE_CI_CANCELLED_MAX_SECONDS


def _ago(seconds: float) -> str:
    return (utcnow() - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _run(name: str, run_id: int, conclusion: str | None, *, finished_ago: float | None
         ) -> dict[str, Any]:
    run: dict[str, Any] = {"id": run_id, "name": name, "app": {"id": ACTIONS_APP},
                           "status": "in_progress" if conclusion is None else "completed",
                           "conclusion": conclusion}
    if finished_ago is not None:
        run["completed_at"] = _ago(finished_ago)
    return run


def _two_required(world: MergeWorld) -> None:
    world.rules = [{"type": "required_status_checks", "parameters": {
        "required_status_checks": [{"context": CHECK, "integration_id": ACTIONS_APP},
                                   {"context": LINT, "integration_id": ACTIONS_APP}]}}]


def _waited_since(world: MergeWorld, seconds: float) -> None:
    world.docs[TASK]["metadata"] = {control_mod.MERGE_WAIT_METADATA_KEY: {
        "first_parked_at": utcnow() - timedelta(seconds=seconds), "wakes": 3, "updates": 0}}


def test_the_bound_is_sixty_minutes_and_shorter_than_the_ci_bound():
    # MUTATION: give a cancelled-only reading the 6 h bound -> fails.
    assert merge.MERGE_CI_CANCELLED_MAX_SECONDS == 60 * 60
    assert merge.MERGE_CI_CANCELLED_MAX_SECONDS < merge.MERGE_CI_MAX_SECONDS


def test_a_cancelled_only_reading_inside_the_bound_parks(tmp_path):
    world = MergeWorld(tmp_path)
    world.runs = [_run(CHECK, 20, "cancelled", finished_ago=BOUND - 600)]

    outcome = merge.run_merge(world.context())

    # MUTATION: refuse a cancelled-only reading at once (cancelled read as
    # failed, or a bound of 0) -> FAILED -> fails.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait is not None and outcome.ci_wait["code"] == "checks_pending"
    assert world.merge_calls() == []


def test_a_cancelled_only_reading_past_the_bound_refuses_checks_cancelled(tmp_path):
    world = MergeWorld(tmp_path)
    _two_required(world)
    world.runs = [_run(CHECK, 20, "cancelled", finished_ago=BOUND + 600),
                  _run(LINT, 21, "success", finished_ago=BOUND + 900)]

    outcome = merge.run_merge(world.context())

    # MUTATION: drop the bound (a cancelled-only reading waits out the 6 h)
    # -> PARKED checks_pending -> fails.
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    # MUTATION: refuse it as checks_timeout or checks_failed -> fails.
    assert outcome.summary["refusal"]["code"] == "checks_cancelled", outcome.summary
    # MUTATION: a message that does not name the cancelled check -> fails.
    assert CHECK in outcome.summary["refusal"]["message"]
    assert LINT not in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_the_bound_starts_at_the_first_cancelled_only_reading_not_the_cancel(tmp_path):
    """Cancelled two hours ago, but the other required check finished green
    ten minutes ago: the reading has been cancelled-only for ten minutes."""
    world = MergeWorld(tmp_path)
    _two_required(world)
    world.runs = [_run(CHECK, 20, "cancelled", finished_ago=2 * 3600),
                  _run(LINT, 21, "success", finished_ago=600)]

    outcome = merge.run_merge(world.context())

    # MUTATION: time the bound from the cancelled run's own completed_at
    # (`min` in place of `max` in `cancelled_since`) -> checks_cancelled -> fails.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == "checks_pending", outcome.ci_wait


def test_a_green_re_run_inside_the_bound_merges(tmp_path):
    world = MergeWorld(tmp_path)
    world.runs = [_run(CHECK, 20, "cancelled", finished_ago=BOUND - 900),
                  _run(CHECK, 21, "success", finished_ago=60)]

    outcome = merge.run_merge(world.context())

    # MUTATION: judge a check by any cancelled run rather than its latest
    # -> parks or refuses -> fails.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_a_cancelled_run_beside_one_still_running_keeps_the_six_hour_bound(tmp_path):
    world = MergeWorld(tmp_path)
    _two_required(world)
    world.runs = [_run(CHECK, 20, "cancelled", finished_ago=2 * 3600),
                  _run(LINT, 21, None, finished_ago=None)]

    outcome = merge.run_merge(world.context())

    # MUTATION: apply the 60 min bound whenever a cancelled run is pending,
    # whatever else is running -> checks_cancelled -> fails.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == "checks_pending", outcome.ci_wait


def test_a_running_reading_past_six_hours_still_refuses_checks_timeout(tmp_path):
    world = MergeWorld(tmp_path)
    _two_required(world)
    world.runs = [_run(CHECK, 20, "cancelled", finished_ago=7 * 3600),
                  _run(LINT, 21, None, finished_ago=None)]
    _waited_since(world, merge.MERGE_CI_MAX_SECONDS + 600)

    outcome = merge.run_merge(world.context())

    # MUTATION: the cancelled bound replaces the 6 h one for any reading with
    # a cancelled run -> checks_cancelled -> fails.
    assert outcome.summary["refusal"]["code"] == "checks_timeout", outcome.summary


def test_a_cancelled_only_reading_with_no_completed_at_keeps_the_six_hour_bound(tmp_path):
    """A deciding run with no `completed_at` cannot say when the reading
    became cancelled-only: it waits under MERGE_CI_MAX_SECONDS."""
    world = MergeWorld(tmp_path)
    world.runs = [_run(CHECK, 20, "cancelled", finished_ago=None)]
    _waited_since(world, BOUND + 600)

    outcome = merge.run_merge(world.context())

    # MUTATION: time an unknown onset from `first_parked_at`, or from now
    # minus anything -> refuses -> fails.
    assert outcome.state is TaskState.PARKED, outcome.message


def test_with_no_required_check_a_cancelled_only_reading_past_the_bound_refuses(tmp_path):
    """The no-required path (every check at the head) applies the same bound."""
    world = MergeWorld(tmp_path)
    world.github.route("GET", f"{API}/rules/branches/main", (403, {}, {
        "message": "Upgrade to GitHub Pro or make this repository public to enable this feature."}))
    world.branch = {"name": "main", "protected": False}
    world.runs = [_run("lint", 30, "success", finished_ago=BOUND + 1200),
                  _run("unit", 31, "cancelled", finished_ago=BOUND + 600)]

    outcome = merge.run_merge(world.context())

    # MUTATION: apply the bound on the required path only -> PARKED -> fails.
    assert outcome.summary.get("refusal", {}).get("code") == "checks_cancelled", outcome.summary
    assert world.merge_calls() == []


def test_cancelled_since_is_none_unless_the_reading_is_cancelled_only():
    done = {"status": "completed", "completed_at": _ago(100)}
    # MUTATION: return an instant for an all-green reading -> fails.
    assert merge.cancelled_since([[{**done, "conclusion": "success"}]]) is None
    # MUTATION: skip the "every check has runs" test (a missing required
    # check is pending for ever) -> fails.
    assert merge.cancelled_since([[{**done, "conclusion": "cancelled"}], []]) is None
    assert merge.cancelled_since([[{**done, "conclusion": "cancelled"}]]) is not None
