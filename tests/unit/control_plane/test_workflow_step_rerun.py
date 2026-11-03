"""Retry on a failed workflow step re-runs it inside its workflow.

`POST /v1/workflows/{workflow_id}/steps/{step_id}/rerun` (`swarm_api.steprerun`,
the owner's pick on PR #3123: "Offer it, new route"). Pinned here:

* the failed step and the steps its failure cancelled get NEW tasks, PARKED
  on their parents as submission stores a step, with no attempts, and the workflow's steps point at them; the old tasks
  stay FAILED / CANCELLED and are not written;
* a re-run dependant's `depends_on` names the new task, a succeeded upstream
  step is not re-run and keeps its task id;
* the scheduler admits the re-run step, so the workflow runs again;
* refused while a step still runs, for a step that did not fail, for a
  cancelled workflow and for a step the workflow has not;
* a second Retry of the same step races and writes nothing.
"""

from __future__ import annotations

from swarm_common.states import TaskState

from .conftest import auth_header, seed_pool


def submit_chain(client) -> dict:
    """`build` -> `test` -> `ship`, and `docs` beside them on `build`."""
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "build", "runner_profile": "mock"},
                {"step_id": "test", "runner_profile": "mock", "depends_on": ["build"]},
                {"step_id": "ship", "runner_profile": "mock", "depends_on": ["test"]},
                {"step_id": "docs", "runner_profile": "mock", "depends_on": ["build"]},
            ],
        },
    )
    assert created.status_code == 201, created.text
    workflow = created.json()["workflow"]
    return {
        "workflow_id": workflow["workflow_id"],
        "steps": {s["step_id"]: s["task_id"] for s in workflow["steps"]},
    }


def end(db, task_id: str, state: str, end_cause: str | None = None) -> None:
    doc = db.docs[f"tasks/{task_id}"]
    doc["state"] = state
    doc["end_cause"] = end_cause
    doc["attempt_count"] = 3 if state == "FAILED" else doc.get("attempt_count", 0)


def failed_at_test(db, wf: dict) -> None:
    """What a finished, failed workflow leaves: `build` and `docs` succeeded,
    `test` failed, `ship` was cancelled behind it."""
    end(db, wf["steps"]["build"], "SUCCEEDED")
    end(db, wf["steps"]["docs"], "SUCCEEDED")
    end(db, wf["steps"]["test"], "FAILED", "runner_error")
    end(db, wf["steps"]["ship"], "CANCELLED", "failed_parent")


def rerun(client, wf: dict, step: str, user: str = "alice"):
    return client.post(
        f"/v1/workflows/{wf['workflow_id']}/steps/{step}/rerun", headers=auth_header(user)
    )


def test_rerun_writes_new_tasks_for_the_failed_step_and_what_it_cancelled(client, db) -> None:
    wf = submit_chain(client)
    failed_at_test(db, wf)

    response = rerun(client, wf, "test")

    assert response.status_code == 200, response.text
    replaced = response.json()["replaced"]
    assert set(replaced) == {"test", "ship"}
    assert replaced["test"]["from"] == wf["steps"]["test"]
    new_test, new_ship = replaced["test"]["to"], replaced["ship"]["to"]
    assert new_test != wf["steps"]["test"] and new_ship != wf["steps"]["ship"]

    stored = db.docs[f"workflows/{wf['workflow_id']}"]
    pointers = {s["step_id"]: s["task_id"] for s in stored["steps"]}
    assert pointers == {
        "build": wf["steps"]["build"], "docs": wf["steps"]["docs"],
        "test": new_test, "ship": new_ship,
    }
    assert stored["state"] == "QUEUED"

    test_doc = db.docs[f"tasks/{new_test}"]
    assert test_doc["state"] == "PARKED"
    assert test_doc["park_reason"] == "DEPENDENCY_INCOMPLETE"
    assert test_doc["attempt_count"] == 0
    assert test_doc["end_cause"] is None
    assert test_doc["current_generation"] == 0
    assert test_doc["depends_on"] == [wf["steps"]["build"]]
    assert test_doc["metadata"]["rerun_of"] == wf["steps"]["test"]
    assert test_doc["workflow_id"] == wf["workflow_id"] and test_doc["step_id"] == "test"
    assert db.docs[f"tasks/{new_ship}"]["depends_on"] == [new_test]

    # The old tasks are left as they ended.
    assert db.docs[f"tasks/{wf['steps']['test']}"]["state"] == "FAILED"
    assert db.docs[f"tasks/{wf['steps']['ship']}"]["state"] == "CANCELLED"


def test_the_scheduler_admits_the_rerun_step(client, db, make_scheduler) -> None:
    seed_pool(db, "global", hard_limit=10)
    wf = submit_chain(client)
    failed_at_test(db, wf)
    replaced = rerun(client, wf, "test").json()["replaced"]

    make_scheduler().drain()

    assert db.docs[f"tasks/{replaced['test']['to']}"]["state"] not in {
        TaskState.PARKED.value, TaskState.CANCELLED.value,
    }
    # Its dependant waits on the new task, not the old failed one.
    assert db.docs[f"tasks/{replaced['ship']['to']}"]["state"] != TaskState.CANCELLED.value


def test_a_step_the_sweep_cancelled_is_rerun_too(client, db) -> None:
    wf = submit_chain(client)
    end(db, wf["steps"]["build"], "SUCCEEDED")
    end(db, wf["steps"]["test"], "FAILED", "runner_error")
    end(db, wf["steps"]["ship"], "CANCELLED", "failed_parent")
    end(db, wf["steps"]["docs"], "CANCELLED", "workflow_sweep")

    replaced = rerun(client, wf, "test").json()["replaced"]

    assert set(replaced) == {"test", "ship", "docs"}


def test_refused_while_a_step_still_runs(client, db) -> None:
    wf = submit_chain(client)
    end(db, wf["steps"]["build"], "SUCCEEDED")
    end(db, wf["steps"]["test"], "FAILED", "runner_error")
    db.docs[f"tasks/{wf['steps']['docs']}"]["state"] = "RUNNING"

    response = rerun(client, wf, "test")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "workflow_still_running"
    assert db.docs[f"workflows/{wf['workflow_id']}"]["steps"][1]["task_id"] == wf["steps"]["test"]


def test_refused_for_a_step_that_did_not_fail(client, db) -> None:
    wf = submit_chain(client)
    failed_at_test(db, wf)

    response = rerun(client, wf, "build")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "step_not_failed"


def test_refused_for_a_cancelled_workflow(client, db) -> None:
    wf = submit_chain(client)
    failed_at_test(db, wf)
    db.docs[f"workflows/{wf['workflow_id']}"]["cancel_requested"] = True

    response = rerun(client, wf, "test")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "workflow_cancelled"


def test_unknown_step_is_404(client, db) -> None:
    wf = submit_chain(client)
    failed_at_test(db, wf)

    response = rerun(client, wf, "nope")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "step_not_found"


def test_a_second_retry_of_the_same_step_is_refused(client, db) -> None:
    wf = submit_chain(client)
    failed_at_test(db, wf)
    assert rerun(client, wf, "test").status_code == 200
    tasks_before = sorted(k for k in db.docs if k.startswith("tasks/") and k.count("/") == 1)

    second = rerun(client, wf, "test")

    # The new step is PARKED, so the workflow is running again.
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "workflow_still_running"
    tasks_after = sorted(k for k in db.docs if k.startswith("tasks/") and k.count("/") == 1)
    assert tasks_after == tasks_before


def test_the_store_refuses_a_rerun_that_raced(client, db, api_store) -> None:
    wf = submit_chain(client)
    failed_at_test(db, wf)
    assert rerun(client, wf, "test").status_code == 200
    # The same re-run, prepared from the read before the first one landed.
    import pytest

    from swarm_api.errors import Conflict

    with pytest.raises(Conflict) as refused:
        api_store.rerun_workflow_steps(
            "eng", wf["workflow_id"], replaced={"test": wf["steps"]["test"]},
            tasks=[], by="alice@example.com",
        )
    assert refused.value.detail["reason"] == "step_rerun_raced"


def test_the_workflow_read_serves_each_rerun_step_once(client, db) -> None:
    wf = submit_chain(client)
    failed_at_test(db, wf)
    replaced = rerun(client, wf, "test").json()["replaced"]

    body = client.get(f"/v1/workflows/{wf['workflow_id']}", headers=auth_header("alice")).json()

    served = {t["id"] if "id" in t else t["task_id"] for t in body["tasks"]}
    assert replaced["test"]["to"] in served and replaced["ship"]["to"] in served
    assert wf["steps"]["test"] not in served and wf["steps"]["ship"] not in served
    assert wf["steps"]["build"] in served
    assert body["workflow"]["state"] not in {"FAILED", "CANCELLED"}
