"""The merge step reads a required check its head's failed Actions run never reported as failed (#2999).

PR #2959 at 5a5d673b7: both `application` runs at the head concluded
`failure` on a red unit shard and ended without a `ci-gate` check run. The
step read the absent required check as pending and parked 13 hours on
CI_PENDING. Now, once every Actions run at the head that is not superseded
has concluded and one failed, the absent check is failed, naming the run
and its failing jobs: the first such reading parks once for the free re-run
(`ci_rerun_due`), a step that asked for rounds parks for one, and a red
reading after them refuses `checks_failed` -- exactly as a red ci-gate. A
run still in progress, or superseded by a newer run of its workflow, event
and branch, keeps it pending. swarm-api's reading is the same rule
(tests/unit/control_plane/test_unreported_required_check.py).

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from typing import Any

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import ACTIONS_APP, CHECK, OPENER, PINNED, TASK, MergeWorld

SHARD = "console component tests (4/4)"
RUN, NEWER_RUN, SUITE, NEWER_SUITE = 41290837472, 41290999999, 901, 904
SHARD_JOB = 124801931332


def _talk_pane(world: MergeWorld, *, run_conclusion: str | None = "failure") -> None:
    """The head requires CHECK; its one Actions run concluded `run_conclusion`
    (None: still running) with a red shard, and reported no CHECK."""
    world.runs[:] = [{"id": SHARD_JOB, "name": SHARD, "status": "completed",
                      "conclusion": "failure", "app": {"id": ACTIONS_APP},
                      "check_suite": {"id": SUITE}}]
    world.workflow_runs[:] = [{
        "id": RUN, "check_suite_id": SUITE, "event": "pull_request",
        "head_branch": f"swarm/{OPENER}", "workflow_id": 11, "head_sha": PINNED,
        "status": "completed" if run_conclusion else "in_progress",
        "conclusion": run_conclusion}]


def _parked(outcome: Any, code: str) -> None:
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert outcome.ci_wait["code"] == code, outcome.ci_wait


def test_an_unreported_gate_after_a_failed_run_parks_once_for_the_free_rerun(tmp_path):
    world = MergeWorld(tmp_path)
    _talk_pane(world)

    outcome = merge.run_merge(world.context())

    # MUTATION (merge.py `run_merge`): read an absent required check as
    # pending, as before #2999 -> `checks_pending`, for 13 hours.
    _parked(outcome, merge.CI_RERUN_DUE)
    message = outcome.summary["wait"]["message"]
    assert CHECK in message and str(RUN) in message and SHARD in message, message
    assert outcome.ci_wait["failed"] == [CHECK]
    assert outcome.ci_wait["pending"] == []
    assert world.merge_calls() == []


def test_with_rounds_asked_it_parks_for_a_ci_fix_round(tmp_path):
    world = MergeWorld(tmp_path)
    _talk_pane(world)
    world.docs[TASK]["metadata"] = {"merge_fix_rounds": 1}

    outcome = merge.run_merge(world.context())

    _parked(outcome, merge.CI_FIX_PENDING)


def test_after_the_free_rerun_answered_it_refuses_checks_failed(tmp_path):
    """The step's own last park was the re-run's: the second red reading refuses."""
    world = MergeWorld(tmp_path)
    _talk_pane(world)
    world.docs[TASK].setdefault("metadata", {})[merge.MERGE_WAIT_METADATA_KEY] = {
        "code": merge.CI_RERUN_DUE, "head": PINNED, "pull_request": 1, "pending": []}

    outcome = merge.run_merge(world.context())

    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.message
    assert outcome.summary["refusal"]["code"] == "checks_failed", outcome.summary
    assert world.merge_calls() == []


def test_a_run_still_in_progress_keeps_the_gate_pending(tmp_path):
    world = MergeWorld(tmp_path)
    _talk_pane(world, run_conclusion=None)

    outcome = merge.run_merge(world.context())

    _parked(outcome, "checks_pending")
    assert outcome.ci_wait["pending"] == [CHECK]


def test_a_superseded_failed_run_keeps_the_gate_pending(tmp_path):
    world = MergeWorld(tmp_path)
    _talk_pane(world)
    world.workflow_runs.append({**world.workflow_runs[0], "id": NEWER_RUN,
                                "check_suite_id": NEWER_SUITE, "status": "queued",
                                "conclusion": None})

    outcome = merge.run_merge(world.context())

    # MUTATION (merge.py `unreported_failure`): ignore `workflow_superseded`
    # -> `ci_rerun_due` on a run already replaced.
    _parked(outcome, "checks_pending")
    assert outcome.ci_wait["pending"] == [CHECK]
