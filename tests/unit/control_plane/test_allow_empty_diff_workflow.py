"""`allow_empty_diff`, SKIPPED steps and the MERGE label: the API half (owner, 2026-10-05).

What the API accepts and stores, and how it reads the result back:

  * `allow_empty_diff: true` on a step is stored as
    `metadata.dispatch.allow_empty_diff`, inside the block the spec signature
    covers, and served in the task's `dispatch`. A step that does not ask
    stores exactly the block it stored before.
  * The gated integrator of a review shape carries the workflow's label as
    `dispatch.pr_label`, so a MERGE verdict -- which runs no fix agent -- can
    still title its pull request when the implementer wrote no title.
  * A step the worker skipped for "nothing to change" is a SUCCEEDED task with
    `result_summary.skipped`; the workflow derivation counts it as a success
    and names it in `rollup.skipped_steps`. A `skipped` marker on a task that
    did not SUCCEED is not a skip.

The worker half is tests/unit/worker/test_allow_empty_diff.py and
tests/unit/worker/test_merge_verdict_runs_no_fix_agent.py.
"""

from __future__ import annotations

from typing import Any

from swarm_common.states import TaskState

from swarm_api.rollup import derive_for

from .conftest import auth_header, seed_task, seed_tenant
from .test_workflow_state_rollup import make_workflow, seed_workflow, states_of

REPO = "https://github.com/saga-xyz/example.git"
SKIPPED = {"reason": "nothing to change", "upstream": ["t1"]}


def _post(client: Any, spec: dict[str, Any]) -> Any:
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _steps(body: dict[str, Any]) -> dict[str, str]:
    return {s["step_id"]: s["task_id"] for s in body["workflow"]["steps"]}


def _review_shape(**top: Any) -> dict[str, Any]:
    spec = {
        "strategy": "integrate",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock", "allow_empty_diff": True,
             "input": {"prompt": "fix it if it is broken"}},
            {"step_id": "review", "runner_profile": "mock",
             "depends_on": ["implement"], "builds_on": "implement",
             "input_from": {"implement": "swarm-work.patch"},
             "input": {"prompt": "review swarm-work.patch; write verdict.json"}},
            {"step_id": "fix", "runner_profile": "mock",
             "depends_on": ["review"], "builds_on": "implement",
             "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": "fix every finding in verdict.json"}},
        ],
    }
    spec.update(top)
    return spec


# --------------------------------------------------------------------------
# allow_empty_diff is stored where the worker reads it, and signed
# --------------------------------------------------------------------------

def test_the_flag_is_stored_in_the_dispatch_block_and_served(client, db):
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    steps = _steps(response.json())

    implement = db.docs[f"tasks/{steps['implement']}"]
    assert implement["metadata"]["dispatch"]["allow_empty_diff"] is True
    for other in ("review", "fix"):
        assert "allow_empty_diff" not in db.docs[f"tasks/{steps[other]}"]["metadata"]["dispatch"]

    read = client.get(f"/v1/tasks/{steps['implement']}", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    assert read.json()["task"]["dispatch"]["allow_empty_diff"] is True
    plain = client.get(f"/v1/tasks/{steps['review']}", headers=auth_header("alice"))
    assert "allow_empty_diff" not in plain.json()["task"]["dispatch"]


def test_the_flag_is_covered_by_the_spec_signature():
    from swarm_common.specsign import SIGNED_METADATA_KEYS

    from swarm_api.validation import DispatchOptions

    block = DispatchOptions(strategy="collect", allow_empty_diff=True).to_metadata()
    assert block["allow_empty_diff"] is True
    # It lives in `dispatch`, which the signature covers whole.
    assert "dispatch" in SIGNED_METADATA_KEYS
    assert "allow_empty_diff" not in DispatchOptions(strategy="collect").to_metadata()


def test_a_flag_that_is_not_a_boolean_is_refused(client, db):
    spec = _review_shape()
    spec["steps"][0]["allow_empty_diff"] = "yes"
    response = _post(client, spec)
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------
# the label the MERGE path titles its pull request with
# --------------------------------------------------------------------------

def test_the_gated_integrator_carries_the_workflow_label(client, db):
    response = _post(client, _review_shape(metadata={"unit": "Widget feature"}))
    assert response.status_code == 201, response.text
    steps = _steps(response.json())

    fix = db.docs[f"tasks/{steps['fix']}"]["metadata"]["dispatch"]
    assert fix["pr_label"] == "Widget feature"
    for other in ("implement", "review"):
        assert "pr_label" not in db.docs[f"tasks/{steps[other]}"]["metadata"]["dispatch"]


def test_the_title_stands_in_for_a_missing_label(client, db):
    response = _post(client, _review_shape(metadata={"title": "Fix the widget"}))
    assert response.status_code == 201, response.text
    fix = db.docs[f"tasks/{_steps(response.json())['fix']}"]["metadata"]["dispatch"]
    assert fix["pr_label"] == "Fix the widget"


def test_no_label_stores_no_key(client, db):
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    fix = db.docs[f"tasks/{_steps(response.json())['fix']}"]["metadata"]["dispatch"]
    assert "pr_label" not in fix


# --------------------------------------------------------------------------
# SKIPPED counts as success
# --------------------------------------------------------------------------

def test_a_skipped_step_derives_success_and_is_named():
    workflow = make_workflow(steps=[("implement", "t1"), ("review", "t2"), ("fix", "t3")])
    rollup = derive_for(
        workflow,
        states_of(t1="SUCCEEDED", t2="SUCCEEDED", t3="SUCCEEDED"),
        skipped=["t2", "t3"],
    )

    assert rollup.state == TaskState.SUCCEEDED.value
    assert rollup.complete is True
    assert rollup.skipped_steps == ["review", "fix"]
    assert rollup.to_api()["skipped_steps"] == ["review", "fix"]
    # `counts` keeps counting them as SUCCEEDED, which is what the UI's
    # "n/m done" reads.
    assert rollup.counts == {"SUCCEEDED": 3}


def test_a_skip_marker_on_a_failed_step_is_not_a_skip():
    workflow = make_workflow(steps=[("implement", "t1"), ("review", "t2")])
    rollup = derive_for(
        workflow, states_of(t1="SUCCEEDED", t2="FAILED"), skipped=["t2"],
    )

    assert rollup.state == TaskState.FAILED.value
    assert rollup.skipped_steps == []


def test_the_workflow_read_names_the_skipped_steps(db, client):
    seed_tenant(db, "eng")
    implement = seed_task(db, task_id="t1", tenant_id="eng", state="SUCCEEDED",
                          workflow_id="wf_test")
    implement["result_summary"] = {"no_change": True}
    review = seed_task(db, task_id="t2", tenant_id="eng", state="SUCCEEDED",
                       workflow_id="wf_test")
    review["result_summary"] = {"skipped": dict(SKIPPED)}
    fix = seed_task(db, task_id="t3", tenant_id="eng", state="SUCCEEDED",
                    workflow_id="wf_test")
    fix["result_summary"] = {"skipped": dict(SKIPPED)}
    seed_workflow(
        db, make_workflow(steps=[("implement", "t1"), ("review", "t2"), ("fix", "t3")])
    )

    response = client.get("/v1/workflows/wf_test", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    workflow = response.json()["workflow"]
    assert workflow["state"] == "SUCCEEDED"
    assert workflow["rollup"]["skipped_steps"] == ["review", "fix"]

    listed = client.get("/v1/workflows", headers=auth_header("alice")).json()
    assert listed["workflows"][0]["rollup"]["skipped_steps"] == ["review", "fix"]
