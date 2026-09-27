"""A caller never chooses the model an agent runs (#226, invariant 10).

Owner decision of 2026-09-26: the `claude-code` profile runs the model its
Cloud Run Job's `MODEL` names, set in Terraform, and "a caller still cannot
choose the model". The issue took for granted that `input.model` was refused.

Measured before this lane: it was not. It was refused only by the swarm-mcp
bridge. `POST /v1/tasks` stored it, the console's Submit form offered it for
claude-code and codex ("overrides the image default"), and `runners/cliagent.py`
passed it to the CLI as `--model` ahead of the Job's own value. So a caller of
the API or the console did choose the model. Then #213 (contract request 25,
merged 2026-09-26) made the API refuse every key a profile's
`RunnerProfile.inputs` does not declare. claude-code declares none, so
`input.model` is refused there now, with 422 `invalid_input`, and the Submit
form stopped offering it.

This file pins that refusal for the key #226 is about, so that a later
declaration cannot quietly let `model` back in. It is green on main by design:
the refusal is #213's, and this lane does not restate it. The part this lane
adds -- the runner reading `--model` from the Job's MODEL and never from
`input`, and the worker dropping a stored `input.model` -- is in
tests/unit/worker/test_step_parity.py.

What is held:

  * `input.model` on claude-code is refused on a task, a batch and a workflow
    step, the refusal names the key, and nothing is created -- asserted on the
    store, not only on the status;
  * the top-level `model` field is still accepted. It is attribution: "recorded
    for attribution and cost reporting; it selects nothing about the container"
    (`TaskCreate.model`), and `swarm dispatch --model` sends it;
  * the same submission without `input.model` is accepted (the control).
"""

from __future__ import annotations

import pytest

from .conftest import auth_header

#: #213's code for a key the profile does not declare.
REFUSAL_CODE = "invalid_input"


def _task_documents(db) -> list[str]:
    # Task documents only: an accepted task also writes events under
    # tasks/<id>/events/, which are not a second task.
    return [path for path in db.paths("tasks/") if path.count("/") == 1]


def _created(db) -> dict[str, list[str]]:
    return {"tasks": _task_documents(db), "workflows": db.paths("workflows/")}


def _assert_model_refusal(response) -> dict:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == REFUSAL_CODE, body
    assert body["detail"]["key"] == "model", body
    assert body["detail"]["runner_profile"] == "claude-code", body
    assert "model" not in body["detail"]["declared"], (
        "claude-code must not declare `model`: the model is the Job's MODEL, "
        "set in Terraform (#226), and a caller does not choose it"
    )
    return body


def test_a_claude_code_task_carrying_input_model_is_refused_and_creates_nothing(client, db):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "claude-code",
            "input": {"prompt": "fix the bug", "model": "claude-haiku-5"},
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_model_refusal(response)


def test_a_batch_with_one_task_carrying_input_model_creates_none_of_them(client, db):
    response = client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={
            "tasks": [
                {"runner_profile": "claude-code", "input": {"prompt": "clean"}},
                {
                    "runner_profile": "claude-code",
                    "input": {"prompt": "sneaky", "model": "claude-haiku-5"},
                },
            ]
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_model_refusal(response)


def test_a_workflow_step_carrying_input_model_is_refused_and_creates_nothing(client, db):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "scan", "runner_profile": "claude-code", "input": {"prompt": "scan"}},
                {
                    "step_id": "merge",
                    "runner_profile": "claude-code",
                    "depends_on": ["scan"],
                    "input": {"prompt": "merge", "model": "claude-haiku-5"},
                },
            ]
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    body = _assert_model_refusal(response)
    assert body["detail"].get("step_id") == "merge"


def test_the_top_level_model_is_still_attribution_and_is_accepted(client, db):
    """`TaskCreate.model` is recorded, not executed; refusing `input.model`
    must not take the attribution field with it."""
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "claude-code",
            "input": {"prompt": "fix the bug"},
            "model": "claude-opus-5-5",
        },
    )

    assert response.status_code == 201, response.text
    task_id = response.json()["task"]["id"]
    stored = db.docs[f"tasks/{task_id}"]
    assert stored["model"] == "claude-opus-5-5"
    assert "model" not in stored["input"]


@pytest.mark.parametrize("path", ["/v1/tasks", "/v1/workflows"])
def test_the_same_submission_without_input_model_is_accepted(client, db, path):
    """The control: the refusal is about the key, not the profile or the prompt."""
    if path == "/v1/tasks":
        payload = {"runner_profile": "claude-code", "input": {"prompt": "fix the bug"}}
    else:
        payload = {
            "steps": [
                {"step_id": "scan", "runner_profile": "claude-code", "input": {"prompt": "scan"}}
            ]
        }

    response = client.post(path, headers=auth_header("alice"), json=payload)

    assert response.status_code == 201, response.text
    assert _task_documents(db), "an accepted submission created no task"
