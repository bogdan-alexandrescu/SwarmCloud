"""swarm-api reads a red check in a cancelled workflow run as the merge step does (#2582).

`ci-gate` and security.yml's folded name-keepers run under `if: always()`, so
inside an Actions run concurrency cancelled they conclude `failure`, not
`cancelled` (PR #2565 at 968d9949). The merge step reads such a check run as
`cancelled` -- pending, never green -- when a newer run of the same workflow
and event is at the head or another event passed the check
(`agent_worker.merge.cancelled_by_its_workflow`). `forgechecks` states the
same rule, and THE PARITY TABLE below runs both over the same cases, so the
console, the train and the merge never disagree. `forgechecks.
mark_workflow_runs` puts the Actions run's conclusion and whether it is
superseded on each check run, as the worker's `actions_suites` does.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import merge as worker_merge
from swarm_api import forgechecks

NAME = "ci-gate"

#: (case, [(run id, conclusion, event, workflow run's conclusion, superseded)], state)
PARITY = [
    ("#2582: red in a cancelled push run, newer push run coming",
     [(2, "success", "pull_request", "success", False),
      (3, "failure", "push", "cancelled", True)], "pending"),
    ("#2582: red in a cancelled push run, pull_request passed",
     [(2, "success", "pull_request", "success", False),
      (3, "failure", "push", "cancelled", False)], "pending"),
    ("#2582: red in a lone cancelled run superseded", [(3, "failure", "push", "cancelled", True)],
     "pending"),
    ("red in a cancelled run with nothing to supersede it",
     [(3, "failure", "push", "cancelled", False)], "red"),
    ("red in a run nobody cancelled, newer run coming",
     [(2, "success", "pull_request", "success", False),
      (3, "failure", "push", "failure", True)], "red"),
    ("red pull_request beside a cancelled push run",
     [(2, "failure", "pull_request", "failure", False),
      (3, "failure", "push", "cancelled", True)], "red"),
    ("green in a cancelled run is green", [(3, "success", "push", "cancelled", True)], "green"),
    # A gate under `!cancelled()` may say `skipped` in a cancelled run: that
    # must not read green while the newer run is still to come.
    ("skipped in a cancelled run, newer run coming",
     [(2, "success", "pull_request", "success", False),
      (3, "skipped", "push", "cancelled", True)], "pending"),
    ("skipped in a run nobody cancelled", [(3, "skipped", "push", "success", True)], "green"),
    ("no workflow run read", [(2, "success", "pull_request", None, False),
                              (3, "failure", "push", None, False)], "red"),
]


def _github_run(run_id: int, conclusion: str, event: str, workflow: str | None,
                superseded: bool) -> dict[str, Any]:
    run: dict[str, Any] = {"id": run_id, "name": NAME, "status": "completed",
                           "conclusion": conclusion, "app": {"id": 1},
                           "check_suite": {"id": 500 + run_id}, "event": event,
                           "workflow_superseded": superseded}
    if workflow is not None:
        run["workflow_conclusion"] = workflow
    return run


def _worker_run(run_id: int, conclusion: str, event: str, workflow: str | None,
                superseded: bool) -> dict[str, Any]:
    return worker_merge.CheckFacts(
        name=NAME, status="completed", conclusion=conclusion, app_id=1, run_id=run_id,
        event=event, check_suite_id=500 + run_id, workflow_conclusion=workflow,
        workflow_superseded=superseded).as_run()


@pytest.mark.parametrize(("case", "runs", "state"), PARITY, ids=[c[0] for c in PARITY])
def test_swarm_api_and_the_merge_step_read_a_cancelled_workflow_run_alike(case, runs, state):
    api = forgechecks.required_check_state([_github_run(*r) for r in runs])
    worker = worker_merge.required_check_state([_worker_run(*r) for r in runs])
    # MUTATION (forgechecks.py): drop `cancelled_by_its_workflow` -> the
    # #2582 cases read "red".
    # MUTATION (either side): drop its condition -> "red in a cancelled run
    # with nothing to supersede it" reads "pending".
    # MUTATION (either side): keep every tolerated conclusion, not success
    # alone -> "skipped in a cancelled run, newer run coming" reads "green".
    assert api == state, case
    # MUTATION (one side only) -> the two disagree here.
    assert {"failed": "red"}.get(worker, worker) == api, (case, worker, api)


def test_the_reading_waits_and_excerpts_no_failing_run():
    required = [forgechecks.RequiredCheck(NAME, 1)]
    runs = [_github_run(2, "success", "pull_request", "success", False),
            _github_run(3, "failure", "push", "cancelled", True)]
    reading = forgechecks.evaluate(required, runs, [])
    # MUTATION: decide the state on the rule but excerpt the raw run -> red.
    assert reading.state == forgechecks.PENDING, reading
    assert reading.failing_runs == [] and reading.pending == [NAME]
    # The no-required path reads it the same way.
    reading = forgechecks.evaluate([], runs, [])
    assert reading.state == forgechecks.PENDING, reading
    assert reading.failing_runs == []


def _workflow_run(run_id: int, suite: int, event: str, conclusion: str | None, *,
                  workflow: int = 77, branch: str = "swarm/task_x") -> dict[str, Any]:
    return {"id": run_id, "check_suite_id": suite, "event": event, "workflow_id": workflow,
            "head_branch": branch, "conclusion": conclusion}


def test_mark_workflow_runs_names_the_cancelled_and_superseded_run():
    checks = [{"id": 2, "name": NAME, "check_suite": {"id": 502}},
              {"id": 3, "name": NAME, "check_suite": {"id": 503}},
              {"id": 4, "name": "other app", "check_suite": {"id": 999}}]
    workflow_runs = [_workflow_run(9002, 502, "pull_request", "success"),
                     _workflow_run(9003, 503, "push", "cancelled"),
                     _workflow_run(9004, 504, "push", None),
                     # Another workflow, and another branch: neither supersedes.
                     _workflow_run(9005, 505, "pull_request", None, workflow=78),
                     _workflow_run(9006, 506, "pull_request", None, branch="swarm/task_y")]
    marked = forgechecks.mark_workflow_runs(checks, workflow_runs)
    by_id = {run["id"]: run for run in marked}
    # MUTATION: skip the mark -> the Actions run's conclusion never reaches the rule.
    assert by_id[3]["workflow_conclusion"] == "cancelled"
    assert by_id[3]["workflow_superseded"] is True
    # MUTATION: compare event or workflow loosely -> the pull_request run reads superseded.
    assert by_id[2]["workflow_conclusion"] == "success"
    assert by_id[2]["workflow_superseded"] is False
    assert "workflow_conclusion" not in by_id[4]
    # The input is not mutated.
    assert "workflow_conclusion" not in checks[1]
    # The worker reads the same Actions runs the same way.
    worker = worker_merge.actions_suites(workflow_runs)
    assert (worker[503].conclusion, worker[503].superseded) == ("cancelled", True)
    assert (worker[502].conclusion, worker[502].superseded) == ("success", False)
    assert worker[505].superseded is False and worker[506].superseded is False
