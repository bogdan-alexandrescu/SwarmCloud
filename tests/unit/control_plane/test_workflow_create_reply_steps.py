"""`POST /v1/workflows` names every step it created, each with its task (#2206).

The /sc:swarmcloud launch reads the create reply, relayed, to start one row
per step; a reply missing a step leaves that step's task running unwatched.
Measured 2026-10-24 on wf_effa3d00346b40d1a1d3: the relayed reply named no
step. These tests hold that the loss was not here -- the route serves every
step of a new workflow, with its task id and profile -- and that there is no
idempotent replay that could answer with an older, thinner document: two
identical submissions are two workflows.

`spec_digest` is not the API's: it is the bridge's fnv1a32 of the spec it
received (`swarm_mcp.workflows.spec_digest`), added to `swarm_workflow`'s
reply, and the frozen `Workflow` has no field for it. So no stored workflow
carries one, and the create reply's half of the contract is the steps.
"""

from __future__ import annotations

from typing import Any

from .conftest import auth_header


def _spec() -> dict[str, Any]:
    return {
        "on_step_failure": "continue",
        "steps": [
            {"step_id": "build", "runner_profile": "mock", "input": {"prompt": "build it"}},
            {"step_id": "check", "runner_profile": "mock", "depends_on": ["build"],
             "input": {"prompt": "check it"}},
            {"step_id": "report", "runner_profile": "mock", "depends_on": ["build", "check"],
             "input": {"prompt": "report"}},
        ],
    }


def _post(client) -> dict[str, Any]:
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_spec())
    assert response.status_code == 201, response.text
    return response.json()


def test_a_new_workflows_reply_names_every_step_with_its_task_and_profile(client, db):
    workflow = _post(client)["workflow"]
    steps = {step["step_id"]: step for step in workflow["steps"]}
    assert set(steps) == {"build", "check", "report"}, steps
    for step_id, step in steps.items():
        assert step["task_id"], f"step {step_id} was served with no task"
        assert step["runner_profile"] == "mock"
        assert f"tasks/{step['task_id']}" in db.docs, f"step {step_id} names a task that was not written"
    assert sorted(steps["report"]["depends_on"]) == ["build", "check"]
    # The reply's steps are the stored document's steps, none dropped.
    stored = db.docs[f"workflows/{workflow['workflow_id']}"]
    assert [s["step_id"] for s in stored["steps"]] == [s["step_id"] for s in workflow["steps"]]


def test_two_identical_submissions_are_two_workflows_not_a_replay(client):
    first, second = _post(client)["workflow"], _post(client)["workflow"]
    assert first["workflow_id"] != second["workflow_id"]
    assert len(second["steps"]) == 3
    assert {s["task_id"] for s in first["steps"]}.isdisjoint({s["task_id"] for s in second["steps"]})
