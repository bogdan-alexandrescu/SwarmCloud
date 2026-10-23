"""A plan naming workflow files is refused unless the registration's
`workflow_files` switch is on, and held for the owner when it is (#2029).

BGD, 2026-10-22 (WF-APP-TOKEN): the switch replaces `platform: true` + the
`-git-wf` slot as what decides whether workflow files can be pushed.

  * Switch OFF -- whatever `platform` says -- a plan naming a workflow file
    is refused exactly as before: `workflow_paths` names it and the run fails
    WORKFLOWS_PATH.
  * Switch ON, it is not refused: the plan is held NEEDS_OWNER (owner_only)
    with the stop `workflows`, and so is any merge whose pull request changes
    `.github/workflows/` or `.github/actions/`, whatever the run's merge gate
    or the hold switch says (owner decision Q3, unchanged).

Pure: no app, no Firestore, no credentials, no network.
"""

from __future__ import annotations

import copy

import pytest

from swarm_api import issueruns

from .test_issue_runs import PLAN

WORKFLOW = ".github/workflows/ci.yml"


def _plan(*files: str, prompt: str = "Change what the issue asks.") -> dict:
    plan = copy.deepcopy(PLAN)
    plan["steps"][0]["files"] = list(files)
    plan["steps"][0]["prompt"] = prompt
    return plan


WORKFLOW_PLAN = _plan(WORKFLOW, "src/widgets/list.py")


def test_with_the_switch_off_a_workflow_plan_is_refused():
    # MUTATION: make workflow_paths return [] whatever the switch says and both are red.
    assert issueruns.workflow_paths(WORKFLOW_PLAN) == [WORKFLOW]
    assert issueruns.workflow_paths(WORKFLOW_PLAN, workflow_files=False) == [WORKFLOW]
    assert issueruns.WORKFLOWS_PATH in issueruns.PLANNING_REFUSALS


def test_platform_alone_no_longer_lifts_the_refusal():
    import inspect

    # MUTATION: keep a `platform` parameter that lifts the refusal and this fails.
    assert "platform" not in inspect.signature(issueruns.workflow_paths).parameters
    assert "workflow_slot" not in inspect.signature(issueruns.workflow_paths).parameters


def test_with_the_switch_on_a_workflow_plan_is_not_refused_but_held_for_the_owner():
    # MUTATION: drop the switch branch of workflow_paths and this names the file.
    assert issueruns.workflow_paths(WORKFLOW_PLAN, workflow_files=True) == []
    held = issueruns.plan_hold(WORKFLOW_PLAN, platform=False, workflow_files=True,
                               hard_stop_paths=[".github/workflows/**"])
    # MUTATION: key STOP_WORKFLOWS on `platform` and this is None.
    assert held is not None and held["reasons"] == ["workflows"]
    # MUTATION: leave the code to `platform` and this is NEEDS_SECOND_MEMBER.
    assert held["code"] == "NEEDS_OWNER" and held["approvers"] == "owner_only"
    assert held["matched"] == [WORKFLOW]


def test_with_the_switch_off_there_is_no_workflow_hold_to_hide_a_refusal():
    # Refused, not held, outside the switch.
    held = issueruns.plan_hold(WORKFLOW_PLAN, platform=False, workflow_files=False,
                               hard_stop_paths=[".github/workflows/**"])
    # MUTATION: hold STOP_WORKFLOWS whatever the switch says and this is a hold.
    assert held is None


def test_the_platform_repository_still_holds_a_workflow_change_at_merge():
    """The issue: "The hold now covers every repository whose switch is on,
    not only `platform: true`" -- a person may still push a platform
    repository's `workflow-lines.patch` by hand, and its merge stays the owner's."""
    held = issueruns.hard_stop_hold([WORKFLOW], platform=True, workflow_files=False,
                                    hard_stop_paths=[])
    # MUTATION: key STOP_WORKFLOWS on `workflow_files` alone and this is None.
    assert held is not None and held["reasons"] == ["workflows"]
    assert held["code"] == "NEEDS_OWNER"


def test_a_workflow_hold_is_enforced_whatever_the_switch_says():
    """Q3: "merge held NEEDS_OWNER", whatever the run's merge gate says."""
    held = issueruns.plan_hold(WORKFLOW_PLAN, platform=False, workflow_files=True,
                               hard_stop_paths=[])
    # MUTATION: drop STOP_WORKFLOWS from ENFORCED_STOPS and this is False.
    assert issueruns.hold_always_enforced(held) is True
    assert issueruns.STOP_WORKFLOWS in issueruns.ENFORCED_STOPS


@pytest.mark.parametrize("path", [
    ".github/workflows/release.yml",
    ".github/actions/setup-uv/action.yml",
])
def test_a_pull_request_changing_workflows_or_actions_is_held_at_merge(path):
    held = issueruns.hard_stop_hold([path, "apps/x.py"], platform=False, workflow_files=True,
                                    hard_stop_paths=[])
    # MUTATION: drop `.github/actions/**` from WORKFLOW_HOLD_PATTERNS and the actions case is None.
    assert held is not None and held["reasons"] == ["workflows"]
    assert held["matched"] == [path]
    assert held["code"] == "NEEDS_OWNER"


def test_a_prompt_naming_a_workflow_file_is_held_though_files_are_silent():
    plan = _plan("src/widgets/list.py", prompt="Also add a job to .github/workflows/application.yml.")
    # The refusal reads `files` only (holding on a mention is cheap, refusing is not).
    assert issueruns.workflow_paths(plan) == []
    held = issueruns.plan_hold(plan, platform=False, workflow_files=True, hard_stop_paths=[])
    # MUTATION: hold on plan_files only, not prompt_paths, and this is None.
    assert held is not None and held["reasons"] == ["workflows"]
