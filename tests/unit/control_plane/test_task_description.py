"""A single task's description (owner, 2026-11-07, part of #2688).

A task had a name (`metadata.unit`, its label) and nothing else a person
could read about it. A submission's `metadata.description` is now checked by
the rule a workflow's is held to (`schemas._description`: plain text, at most
`DESCRIPTION_MAX_CHARS`, refused, never cut), stored, and served as
`description` on every read of the task. A workflow step's task inherits
nothing: the workflow's description is the workflow's, so a step serves null.

Offline: the real routes over FakeFirestore, and `TaskCreate` itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from swarm_api import wfdescription
from swarm_api.schemas import DESCRIPTION_MAX_CHARS, TaskCreate

from .conftest import auth_header

DESCRIPTION = ("Rebuilds the size chart from the catalogue. The chart reads one table, "
               "and the page shows it under the product name.")


def _body(**metadata: Any) -> dict[str, Any]:
    return {"runner_profile": "mock", "input": {"prompt": "build it"}, "metadata": metadata}


def _submit(client, **metadata: Any) -> dict[str, Any]:
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_body(**metadata))
    assert response.status_code == 201, response.text
    return response.json()["task"]


def _refusal(**metadata: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        TaskCreate.model_validate(_body(**metadata))
    return str(caught.value)


def test_a_description_is_trimmed_and_kept():
    created = TaskCreate.model_validate(_body(description=f"  {DESCRIPTION}\n"))
    assert created.metadata["description"] == DESCRIPTION


def test_the_cap_is_the_workflows_and_one_past_it_is_refused_not_cut():
    assert DESCRIPTION_MAX_CHARS == 600
    at_cap = TaskCreate.model_validate(_body(description="a" * DESCRIPTION_MAX_CHARS))
    assert at_cap.metadata["description"] == "a" * DESCRIPTION_MAX_CHARS
    assert "601 characters and holds at most 600" in _refusal(description="a" * (DESCRIPTION_MAX_CHARS + 1))


@pytest.mark.parametrize("value", ["", "   ", 7, ["a"], "Adds a <b>size</b> chart."])
def test_an_empty_non_text_or_marked_up_description_is_refused(value):
    assert "metadata.description" in _refusal(description=value)


def test_a_task_without_one_is_unchanged():
    assert "description" not in TaskCreate.model_validate(_body()).metadata


def test_an_over_long_description_is_a_422_and_nothing_is_stored(client, db):
    response = client.post("/v1/tasks", headers=auth_header("alice"),
                           json=_body(description="a" * (DESCRIPTION_MAX_CHARS + 1)))
    assert response.status_code == 422
    assert "holds at most 600" in response.text
    listed = client.get("/v1/tasks", headers=auth_header("alice")).json()
    assert listed["tasks"] == []


def test_every_read_serves_the_description(client):
    task = _submit(client, unit="Size chart", description=DESCRIPTION)
    assert task["description"] == DESCRIPTION
    read = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    assert read.json()["task"]["description"] == DESCRIPTION
    for view in (None, "summary"):
        params = {"view": view} if view else {}
        (row,) = client.get("/v1/tasks", headers=auth_header("alice"), params=params).json()["tasks"]
        assert row["description"] == DESCRIPTION, view


def test_the_batch_route_checks_and_serves_each_tasks_own(client):
    response = client.post("/v1/tasks/batch", headers=auth_header("alice"),
                           json={"tasks": [_body(description=DESCRIPTION), _body()]})
    assert response.status_code == 201, response.text
    served = [t["description"] for t in response.json()["tasks"]]
    assert served == [DESCRIPTION, None]
    refused = client.post("/v1/tasks/batch", headers=auth_header("alice"),
                          json={"tasks": [_body(description="a" * (DESCRIPTION_MAX_CHARS + 1))]})
    assert refused.status_code == 422


def test_a_task_without_one_serves_null(client):
    task = _submit(client)
    assert task["description"] is None
    read = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("alice")).json()
    assert read["task"]["description"] is None


def test_a_workflow_step_inherits_nothing(client, monkeypatch):
    # The workflow's description is copied onto its step tasks' metadata;
    # the task read serves only a single task's own.
    monkeypatch.setattr(wfdescription, "after_submission", lambda *a, **k: None)
    spec = {"strategy": "collect", "metadata": {"description": DESCRIPTION},
            "steps": [{"step_id": "build", "runner_profile": "mock", "input": {"prompt": "build it"}}]}
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 201, response.text
    (step,) = response.json()["workflow"]["steps"]
    read = client.get(f"/v1/tasks/{step['task_id']}", headers=auth_header("alice")).json()
    assert read["task"]["metadata"].get("description") == DESCRIPTION
    assert read["task"]["description"] is None
