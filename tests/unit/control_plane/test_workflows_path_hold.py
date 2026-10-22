"""The `.github/workflows/` refusal becomes a hold where the `-git-wf` slot
can push it (docs/design/agent-autonomy.md §3.3, owner decision Q3; lane FR-1).

  * WITHOUT the slot -- or outside a `platform: true` repository, where the
    slot is never used -- a plan naming a workflow file is refused exactly as
    before: `workflow_paths` names it and the run fails WORKFLOWS_PATH.
  * WITH the slot, in a `platform: true` repository, it is not refused: the
    plan is held NEEDS_OWNER (owner_only) with the stop `workflows`, and so
    is any merge whose pull request changes `.github/workflows/` or
    `.github/actions/`, whatever the run's merge gate or the hold switch says.

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


def test_without_the_slot_a_workflow_plan_is_still_refused():
    # MUTATION: make workflow_paths return [] whatever the slot says and both are red.
    assert issueruns.workflow_paths(WORKFLOW_PLAN) == [WORKFLOW]
    assert issueruns.workflow_paths(WORKFLOW_PLAN, platform=True, workflow_slot=False) == [WORKFLOW]
    assert issueruns.WORKFLOWS_PATH in issueruns.PLANNING_REFUSALS


def test_outside_a_platform_repository_the_slot_changes_nothing():
    # MUTATION: test only `workflow_slot`, not `platform`, and this is [].
    assert issueruns.workflow_paths(WORKFLOW_PLAN, platform=False, workflow_slot=True) == [WORKFLOW]
    assert issueruns.plan_hold(WORKFLOW_PLAN, platform=False, hard_stop_paths=[".github/workflows/**"]) is None


def test_with_the_slot_a_workflow_plan_is_not_refused_but_held_for_the_owner():
    # MUTATION: drop the slot branch of workflow_paths and this names the file.
    assert issueruns.workflow_paths(WORKFLOW_PLAN, platform=True, workflow_slot=True) == []
    held = issueruns.plan_hold(WORKFLOW_PLAN, platform=True, hard_stop_paths=[".github/workflows/**"])
    # MUTATION: leave the workflow pattern out of hard_stop_hold in a platform repository: None.
    assert held is not None and held["reasons"] == ["workflows"]
    assert held["code"] == "NEEDS_OWNER" and held["approvers"] == "owner_only"
    assert held["matched"] == [WORKFLOW]


def test_a_workflow_hold_is_enforced_whatever_the_switch_says():
    """Q3: "merge held NEEDS_OWNER", whatever the run's merge gate says."""
    held = issueruns.plan_hold(WORKFLOW_PLAN, platform=True, hard_stop_paths=[])
    # MUTATION: drop STOP_WORKFLOWS from ENFORCED_STOPS and this is False.
    assert issueruns.hold_always_enforced(held) is True


@pytest.mark.parametrize("path", [
    ".github/workflows/release.yml",
    ".github/actions/setup-uv/action.yml",
])
def test_a_pull_request_changing_workflows_or_actions_is_held_at_merge(path):
    held = issueruns.hard_stop_hold([path, "apps/x.py"], platform=True, hard_stop_paths=[])
    # MUTATION: drop `.github/actions/**` from WORKFLOW_HOLD_PATTERNS and the actions case is None.
    assert held is not None and held["reasons"] == ["workflows"]
    assert held["matched"] == [path]


def test_a_prompt_naming_a_workflow_file_is_held_though_files_are_silent():
    plan = _plan("src/widgets/list.py", prompt="Also add a job to .github/workflows/application.yml.")
    # The refusal reads `files` only (holding on a mention is cheap, refusing is not).
    assert issueruns.workflow_paths(plan) == []
    held = issueruns.plan_hold(plan, platform=True, hard_stop_paths=[])
    # MUTATION: hold on plan_files only, not prompt_paths, and this is None.
    assert held is not None and held["reasons"] == ["workflows"]
