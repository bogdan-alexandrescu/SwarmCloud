"""A check that concluded `failure` inside a workflow run concurrency cancelled is cancelled, not red (#2582).

Measured on PR #2565 at 968d9949 (merge task_0eba9b59, 2026-10-27): the
required `ci-gate`, `secret scan` and `platform policy assertions` run under
`if: always()`, so they still ran inside push runs that concurrency
cancelled and concluded `failure` ("not passed: ... (cancelled)"), not
`cancelled`. Each was the push event's latest run -- the branch's newer push
run had not created its ci-gate yet -- so the step refused `checks_failed`
although every check had passed in the pull_request run and no job failed.

The merge step now reads each Actions run's own conclusion beside its event:
a check run that finished red in a workflow run that concluded `cancelled`
is read as `cancelled` (PR 2573's pending rule, #1778) when the head's other
event passed the check or a newer run of the same workflow and event is at
the head. A red check in a run nobody cancelled stays red; so does a red
check in a cancelled run with nothing to supersede it.

No credentials, no network, no emulator. Each assertion names the mutation it
catches.
"""

from __future__ import annotations

from typing import Any

from agent_worker import merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import ACTIONS_APP, API, CHECK, PINNED, TASK, MergeWorld

CANCELLED_PUSH = 301
PULL_REQUEST = 302
NEWER_PUSH = 303
WORKFLOW = 77


def _gh_run(run_id: int, conclusion: str | None, suite: int, *,
            status: str = "completed") -> dict[str, Any]:
    return {"id": run_id, "name": CHECK, "status": status, "conclusion": conclusion,
            "app": {"id": ACTIONS_APP}, "check_suite": {"id": suite}}


def _workflow_run(suite: int, event: str, branch: str, conclusion: str | None, *,
                  status: str = "completed", workflow: int = WORKFLOW) -> dict[str, Any]:
    """An Actions run at the head, as `GET .../actions/runs` lists it; its id
    grows with the suite's, so a later suite is a newer run."""
    return {"id": 9000 + suite, "check_suite_id": suite, "event": event, "head_branch": branch,
            "head_sha": PINNED, "workflow_id": workflow, "status": status,
            "conclusion": conclusion}


def _rerun_spent(world: MergeWorld) -> None:
    """The step already parked `ci_rerun_due` once at this head (#2351): a red
    reading now refuses `checks_failed`, which is what main did at 968d9949."""
    world.docs[TASK].setdefault("metadata", {})[merge.MERGE_WAIT_METADATA_KEY] = {
        "code": merge.CI_RERUN_DUE, "head": PINNED}


def _branch(world: MergeWorld) -> str:
    return world.pr["head"]["ref"]


def test_issue_2582_a_failed_gate_in_a_cancelled_push_run_beside_a_newer_one_is_pending(tmp_path):
    """#2582's exact shape: the cancelled push run's ci-gate concluded
    failure, the pull_request run passed it, and the newer push run is still
    running with no ci-gate yet."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(2, "success", PULL_REQUEST),
                  _gh_run(3, "failure", CANCELLED_PUSH)]
    world.workflow_runs = [
        _workflow_run(CANCELLED_PUSH, "push", _branch(world), "cancelled"),
        _workflow_run(PULL_REQUEST, "pull_request", _branch(world), "success"),
        _workflow_run(NEWER_PUSH, "push", _branch(world), None, status="in_progress"),
    ]
    _rerun_spent(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: read the check run's own conclusion and ignore its workflow
    # run's `cancelled` (main before #2582) -> MERGE_REFUSED checks_failed
    # "ci-gate (pull_request: success, push: failure)".
    assert outcome.end_cause is not EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.summary["wait"]["code"] == "checks_pending", outcome.summary
    assert outcome.ci_wait["pending"] == [CHECK], outcome.ci_wait
    assert world.merge_calls() == []


def test_a_failed_gate_in_a_cancelled_push_run_the_pull_request_run_passed_is_pending(tmp_path):
    """No newer push run yet: the pull_request run's pass alone is enough."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(2, "success", PULL_REQUEST),
                  _gh_run(3, "failure", CANCELLED_PUSH)]
    world.workflow_runs = [
        _workflow_run(CANCELLED_PUSH, "push", _branch(world), "cancelled"),
        _workflow_run(PULL_REQUEST, "pull_request", _branch(world), "success"),
    ]
    _rerun_spent(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: accept only a newer run as the reason -> checks_failed.
    assert outcome.end_cause is not EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["wait"]["code"] == "checks_pending", outcome.summary
    # MUTATION: read the cancelled run's failure as green -> the merge is made.
    assert world.merge_calls() == []


def test_a_failed_gate_in_a_push_run_nobody_cancelled_still_refuses(tmp_path):
    """The control: the same check runs, the push run concluded failure."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(2, "success", PULL_REQUEST),
                  _gh_run(3, "failure", CANCELLED_PUSH)]
    world.workflow_runs = [
        _workflow_run(CANCELLED_PUSH, "push", _branch(world), "failure"),
        _workflow_run(PULL_REQUEST, "pull_request", _branch(world), "success"),
        _workflow_run(NEWER_PUSH, "push", _branch(world), None, status="in_progress"),
    ]
    _rerun_spent(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: read every red check beside a newer run as cancelled, whatever
    # its workflow run concluded -> checks_pending, a real failure hidden.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert f"{CHECK} (pull_request: success, push: failure)" in outcome.summary["refusal"]["message"]


def test_a_failed_gate_in_a_cancelled_run_with_nothing_to_supersede_it_stays_red(tmp_path):
    """The other control: cancelled, but no other event passed the check and no
    newer run of its workflow is coming -- nothing says the failure is not real."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(3, "failure", CANCELLED_PUSH)]
    world.workflow_runs = [
        _workflow_run(CANCELLED_PUSH, "push", _branch(world), "cancelled"),
        # Another workflow's newer push run does not supersede this one.
        _workflow_run(NEWER_PUSH, "push", _branch(world), None, status="in_progress",
                      workflow=WORKFLOW + 1),
    ]
    _rerun_spent(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: read every red check in a cancelled run as cancelled, or count
    # any newer run at the head as its successor -> checks_pending.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["refusal"]["code"] == "checks_failed"


def test_a_failed_pull_request_run_beside_a_cancelled_push_run_still_refuses(tmp_path):
    """A red latest run of another event still wins (#1778)."""
    world = MergeWorld(tmp_path)
    world.runs = [_gh_run(2, "failure", PULL_REQUEST),
                  _gh_run(3, "failure", CANCELLED_PUSH)]
    world.workflow_runs = [
        _workflow_run(CANCELLED_PUSH, "push", _branch(world), "cancelled"),
        _workflow_run(PULL_REQUEST, "pull_request", _branch(world), "failure"),
        _workflow_run(NEWER_PUSH, "push", _branch(world), None, status="in_progress"),
    ]
    _rerun_spent(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: let the cancelled push run's reading decide the whole check ->
    # checks_pending although the pull_request run failed it.
    assert outcome.end_cause is EndCause.MERGE_REFUSED, outcome.summary
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert world.github.calls("GET", f"{API}/actions/runs"), "the Actions runs were never read"


def test_the_pure_rule_reads_the_workflow_runs_conclusion():
    """`required_check_state` over runs as `CheckFacts.as_run` hands them over."""
    def run(run_id: int, conclusion: str, event: str, *, workflow: str | None = None,
            superseded: bool = False) -> dict[str, Any]:
        return merge.CheckFacts(name=CHECK, status="completed", conclusion=conclusion,
                                run_id=run_id, event=event, workflow_conclusion=workflow,
                                workflow_superseded=superseded).as_run()

    cancelled_push = run(3, "failure", "push", workflow="cancelled", superseded=True)
    # MUTATION: drop the rule -> "failed".
    assert merge.required_check_state([cancelled_push]) == "pending"
    assert merge.failing_run_ids([cancelled_push], merge.REQUIRED_GREEN) == set()
    assert merge.required_check_label([cancelled_push]) == "push: cancelled"
    # A pass in a cancelled run is still a pass.
    assert merge.required_check_state(
        [run(3, "success", "push", workflow="cancelled", superseded=True)]) == "green"
    # MUTATION: read the rule without its condition -> "pending".
    assert merge.required_check_state([run(3, "failure", "push", workflow="cancelled")]) == "failed"
    assert merge.required_check_state(
        [run(2, "success", "pull_request"), run(3, "failure", "push", workflow="cancelled")]
    ) == "pending"
