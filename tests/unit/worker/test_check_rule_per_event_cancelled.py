"""A cancelled latest run is pending, not failed; and a first red reading waits once for the free re-run (#1778, part of #2351).

#1778's second expectation (BGD released bundle H09, 2026-10-27): "a check
whose LATEST run is cancelled is still not green and waits, rather than
failing the merge". The CHECK-RULE (BGD 2026-10-22) already judges a check by
the latest run of each event, so a cancelled run superseded by a success is
green. What was left: a cancelled LATEST run read as `failed`, so the step
refused `checks_failed` on a run the operator had cancelled to re-run. Now it
is pending -- unless another event's latest run is red, which a re-run of the
cancelled one cannot make green. Red still wins.

#2351's merge half (BGD 2026-10-27, "Both halves"): with no CI-fix round
asked and no re-run recorded at the head, `_red` refused on its FIRST red
reading, so swarm-api's free re-run (`mergewake._rerun_first`, which runs only
for a merge parked CI_PENDING) never ran: a transient red (trivy failing to
download its DB) refused a whole lane. Now that reading parks ONCE with
`ci_rerun_due`; a red re-run refuses as before, a green one merges.

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import CHECK, PINNED, TASK, MergeWorld

OTHER_HEAD = "e" * 40
#: The park code of a first red reading (`merge.CI_RERUN_DUE`), spelt out so
#: the tests read the behaviour, not the name.
RERUN_DUE = "ci_rerun_due"


def _facts(run_id: int, conclusion: str | None, event: str, *, name: str = CHECK,
           status: str = "completed") -> merge.CheckFacts:
    return merge.CheckFacts(name=name, status=status, conclusion=conclusion, app_id=1,
                            run_id=run_id, event=event, check_suite_id=500 + run_id)


def _runs(*rows: tuple[int, str | None, str]) -> list[dict[str, Any]]:
    return [_facts(*row).as_run() for row in rows]


def _parked(outcome, code: str, head: str = PINNED) -> None:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.end_cause is None
    assert outcome.ci_wait["code"] == code, outcome.ci_wait
    assert outcome.ci_wait["head"] == head
    assert "refusal" not in outcome.summary, outcome.summary


def _refused_checks_failed(outcome) -> None:
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed", outcome.summary


# ---------------------------------------------------------------------------
# 1. #1778: a cancelled latest run is pending
# ---------------------------------------------------------------------------

#: (case, runs as (run id, conclusion, event), the merge step's state)
CANCELLED = [
    ("latest cancelled", [(1, "cancelled", "push")], "pending"),
    ("green then cancelled in one event", [(1, "success", "push"), (2, "cancelled", "push")],
     "pending"),
    ("cancelled push, green pull_request", [(1, "cancelled", "push"),
                                            (2, "success", "pull_request")], "pending"),
    ("cancelled push, red pull_request", [(1, "cancelled", "push"),
                                          (2, "failure", "pull_request")], "failed"),
    ("cancelled then green (the CHECK-RULE control)", [(1, "cancelled", "push"),
                                                       (2, "success", "push")], "green"),
]


def test_a_cancelled_latest_run_is_pending_and_red_still_wins():
    for case, rows, state in CANCELLED:
        # MUTATION (merge.py `_judge_latest`): read cancelled as not green ->
        # the first three rows read "failed".
        # MUTATION: let any cancelled run make it pending -> the red
        # pull_request row reads "pending".
        assert merge.required_check_state(_runs(*rows)) == state, case


def test_failing_run_ids_never_names_a_cancelled_run():
    """What `_rerun_settling` matches against the re-run's jobs: a cancelled
    run is not a failure a re-run is waited on for."""
    # MUTATION (merge.py `failing_run_ids`): keep cancelled -> {1, 2}.
    assert merge.failing_run_ids(_runs((1, "cancelled", "push"), (2, "failure", "pull_request")),
                                 merge.REQUIRED_GREEN) == {2}
    assert merge.failing_run_ids(_runs((1, "cancelled", "push")), merge.REQUIRED_GREEN) == set()


def test_the_park_records_a_cancelled_check_as_pending_not_failed():
    """WAIT-RECORD: the "Why it's waiting" panel reads what the decision reads."""
    pending, failed = merge.latest_reading({
        "cancelled": [_facts(1, "cancelled", "push", name="cancelled")],
        "red beside cancelled": [_facts(2, "cancelled", "push", name="red beside cancelled"),
                                 _facts(3, "failure", "pull_request", name="red beside cancelled")],
    }, merge.REQUIRED_GREEN)
    # MUTATION (merge.py `latest_reading`): record cancelled as failed ->
    # "cancelled" in `failed`.
    assert pending == ["cancelled"]
    assert failed == ["red beside cancelled"]


def test_with_no_required_check_a_cancelled_latest_run_is_pending():
    """One rule for every check (CHECK-RULE-2): the no-required path too."""
    pending, failed = merge.judge_other_checks([_facts(1, "cancelled", "push", name="lint")])
    # MUTATION (merge.py `judge_other_checks`): a finished blocking run is
    # failed -> "lint (cancelled)" in `failed`.
    assert failed == [] and pending == ["lint (push: cancelled)"], (pending, failed)
    pending, failed = merge.judge_other_checks([
        _facts(1, "cancelled", "push", name="lint"), _facts(2, "failure", "pull_request", name="lint")])
    assert pending == [] and failed == ["lint (push: cancelled, pull_request: failure)"]


def test_the_merge_step_waits_on_a_required_check_whose_latest_run_is_cancelled(tmp_path):
    """Issue #1778: the operator cancelled a stranded run to re-run it. The
    step parks on the checks, as for one still running, and never refuses."""
    world = MergeWorld(tmp_path)
    world.runs[0].update(conclusion="cancelled")

    outcome = merge.run_merge(world.context())

    # MUTATION (merge.py): read cancelled as failed -> `checks_failed`.
    _parked(outcome, "checks_pending")
    assert CHECK in outcome.summary["wait"]["message"]
    assert outcome.ci_wait["pending"] == [CHECK]
    assert outcome.ci_wait["failed"] == []
    assert world.merge_calls() == []


# ---------------------------------------------------------------------------
# 2. #2351: a first red reading parks once for the free re-run
# ---------------------------------------------------------------------------

def _red(world: MergeWorld, run_id: int | None = 7) -> None:
    """The required check red at the head, as an Actions job `run_id` the
    tick could re-run; None is a run with no id, which no re-run replaces."""
    world.runs[0].update(conclusion="failure")
    if run_id is not None:
        world.runs[0]["id"] = run_id


def _rerun(world: MergeWorld, *, head: str = PINNED, ago: float = 60, **extra: Any) -> None:
    """swarm-api's free re-run of job 7 at `head` (SPEED-D2), as its tick records it."""
    since = datetime.now(timezone.utc) - timedelta(seconds=ago)
    world.docs[TASK].setdefault("metadata", {})[merge.MERGE_FIX_METADATA_KEY] = {
        merge.RERUNS_KEY: [{"round": 1, "head": head, "claimed_at": since, "jobs": [7],
                            "requested_at": since, **extra}]}


def _parked_before(world: MergeWorld, code: str, head: str = PINNED) -> None:
    """The step's own last park, as `park_ci_pending` wrote it."""
    world.docs[TASK].setdefault("metadata", {})[merge.MERGE_WAIT_METADATA_KEY] = {
        "code": code, "head": head, "pull_request": 1, "pending": []}


def test_a_first_red_reading_with_no_rounds_parks_once_for_the_free_rerun(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)

    outcome = merge.run_merge(world.context())

    # MUTATION (merge.py `_red`): refuse on the first red reading, as before
    # #2351 -> `checks_failed`, and mergewake's re-run never runs.
    _parked(outcome, RERUN_DUE)
    assert merge.CI_RERUN_DUE == RERUN_DUE
    assert CHECK in outcome.summary["wait"]["message"]
    assert outcome.ci_wait["failed"] == [CHECK]
    assert world.merge_calls() == []


def test_a_red_rerun_then_refuses(tmp_path):
    """The re-run's own answer is a new id, red again."""
    world = MergeWorld(tmp_path)
    _red(world)
    _rerun(world)
    _parked_before(world, RERUN_DUE)
    world.runs.append({**world.runs[0], "id": 8})

    outcome = merge.run_merge(world.context())

    _refused_checks_failed(outcome)
    assert world.merge_calls() == []


def test_a_green_rerun_then_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _rerun(world)
    _parked_before(world, RERUN_DUE)
    world.runs.append({**world.runs[0], "id": 8, "conclusion": "success"})

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == PINNED


def test_a_rerun_github_refused_at_this_head_refuses(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _rerun(world, error="the job is not re-runnable")

    outcome = merge.run_merge(world.context())

    # MUTATION: park whenever no re-run answered -> parks for ever on a
    # re-run that will never come.
    _refused_checks_failed(outcome)


def test_it_parks_once_when_the_tick_found_nothing_to_rerun(tmp_path):
    """The tick re-runs only Actions jobs; with none it wakes the step red,
    recording no re-run. The second red reading at the same head refuses."""
    world = MergeWorld(tmp_path)
    _red(world)
    _parked_before(world, RERUN_DUE)

    outcome = merge.run_merge(world.context())

    # MUTATION (merge.py `_red`): park on every red reading with no re-run
    # recorded -> parks again, and the tick wakes it again, until 6 h.
    _refused_checks_failed(outcome)


def test_a_park_once_at_another_head_does_not_count_here(tmp_path):
    world = MergeWorld(tmp_path)
    _red(world)
    _parked_before(world, RERUN_DUE, head=OTHER_HEAD)
    _rerun(world, head=OTHER_HEAD, ago=3600)

    outcome = merge.run_merge(world.context())

    _parked(outcome, RERUN_DUE)


def test_a_red_run_no_rerun_replaces_refuses_at_once(tmp_path):
    """A failing run with no id -- a commit status -- is one no re-run
    replaces (`failing_run_ids` None): waiting for the tick changes nothing."""
    world = MergeWorld(tmp_path)
    _red(world, run_id=None)

    outcome = merge.run_merge(world.context())

    _refused_checks_failed(outcome)


def test_with_rounds_asked_a_first_red_reading_still_waits_for_the_round(tmp_path):
    """The round's park is unchanged: the tick re-runs first there too."""
    world = MergeWorld(tmp_path)
    _red(world)
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 1}

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_PENDING)
