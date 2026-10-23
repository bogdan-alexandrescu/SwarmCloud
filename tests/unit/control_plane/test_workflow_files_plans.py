"""A plan naming workflow files, through the run routes, keys on `workflow_files` (#2029).

BGD, 2026-10-22 (WF-APP-TOKEN): with the registration's switch off -- even on
the platform's own `platform: true` registration -- the planner's plan is
refused WORKFLOWS_PATH and a plan edit adding a workflow file is a 422. With
it on, the plan is stored and held STOP_WORKFLOWS, NEEDS_OWNER (owner decision
Q3), whatever the hold switch says.
"""

from __future__ import annotations

from swarm_api.repositories import repo_id_for

from .conftest import auth_header
from .test_issue_run_holds import (  # noqa: F401  (fixtures)
    PLAIN_PLAN,
    WORKFLOW_PLAN,
    _grants,
    _planned,
    _workflows,
    api_context,
    client,
    github,
    group_map,
    labels,
)


def _register(db, *, platform: bool, workflow_files: bool | None) -> None:
    repo_id = repo_id_for("eng", "example-org", "widgets")
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": "eng", "owner": "example-org", "repo": "widgets",
        "platform": platform,
        **({"workflow_files": workflow_files} if workflow_files is not None else {}),
    }


def test_with_the_switch_off_a_platform_repositorys_workflow_plan_is_refused(client, db, objects):
    _register(db, platform=True, workflow_files=None)
    run = _planned(client, db, objects, WORKFLOW_PLAN)
    # MUTATION: key the refusal on `platform` and this is PLANNED.
    assert run["state"] == "FAILED"
    assert run["error"].startswith("WORKFLOWS_PATH: ")
    assert "workflow_files" in run["error"]
    assert "made by a person or split out of the issue" in run["error"]
    assert run["plan"] is None
    assert not _workflows(db)


def test_with_the_switch_on_the_plan_is_held_for_the_owner(client, db, objects):
    _register(db, platform=False, workflow_files=True)
    run = _planned(client, db, objects, WORKFLOW_PLAN)
    # MUTATION: call `workflow_paths(plan)` without the switch and this is FAILED.
    assert run["state"] == "PLANNED", run.get("error")
    hold = run["approval_hold"]
    # MUTATION: leave `workflow_files` out of `hard_stops_at_plan` and there is no hold.
    assert hold is not None and "workflows" in hold["reasons"]
    assert hold["code"] == "NEEDS_OWNER" and hold["approvers"] == "owner_only"
    assert ".github/workflows/ci.yml" in hold["matched"]


def test_a_plan_edit_adding_a_workflow_file_is_refused_only_with_the_switch_off(client, db, objects):
    _register(db, platform=True, workflow_files=False)
    run = _planned(client, db, objects, PLAIN_PLAN)
    response = client.post(f"/v1/runs/{run['id']}/plan:edit", headers=auth_header("alice"),
                           json={"plan_digest": run["plan_digest"], "plan": WORKFLOW_PLAN})
    # MUTATION: lift the edit's refusal on `platform` and this is 200.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "WORKFLOWS_PATH"

    db.docs[f"repositories/{repo_id_for('eng', 'example-org', 'widgets')}"]["workflow_files"] = True
    response = client.post(f"/v1/runs/{run['id']}/plan:edit", headers=auth_header("alice"),
                           json={"plan_digest": run["plan_digest"], "plan": WORKFLOW_PLAN})
    # MUTATION: keep the edit's `workflow_paths(plan)` without the switch and this is 422.
    assert response.status_code == 200, response.text
    hold = response.json()["run"]["approval_hold"]
    assert "workflows" in hold["reasons"] and hold["code"] == "NEEDS_OWNER"
