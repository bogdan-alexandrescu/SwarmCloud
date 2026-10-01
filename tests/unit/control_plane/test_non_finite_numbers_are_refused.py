"""NaN and +/-Infinity never reach a stored task (#294, S0).

Python's `json.loads` -- which is what reads a request body here -- accepts the
bare tokens `NaN`, `Infinity` and `-Infinity`, and `TaskCreate.input` and
`.metadata` are `dict[str, Any]`, so until this check a caller could store a
non-finite float anywhere in a task's input or metadata. Firestore keeps it.
Every control-plane reader that later does `int(value)` on such a field raises
(`int(float("inf"))` is an `OverflowError`, `int(float("nan"))` a
`ValueError`), and a reader inside a platform-wide loop then stops that loop
for every tenant.

What is asserted for every value, at every nesting shape: a 422 whose detail
names the JSON path, and nothing written -- no task, no workflow, no wake.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from swarm_api.errors import ValidationFailed
from swarm_api.validation import reject_non_finite, validate_storable

from .conftest import auth_header

#: The three spellings Python's JSON reader accepts and JSON does not.
TOKENS = ("NaN", "Infinity", "-Infinity")

#: (where the value sits, the path the refusal must name). The body is built
#: by substituting the token for the string "@" in the JSON text, so the value
#: arrives exactly as a caller would send it -- not as a Python float that the
#: test client might refuse to serialise.
TASK_PLACEMENTS = [
    pytest.param({"input": {"prompt": "p", "limit": "@"}}, "input.limit", id="input-top"),
    pytest.param(
        {"input": {"prompt": "p", "extra": {"deep": [1, "@"]}}},
        "input.extra.deep[1]",
        id="input-nested",
    ),
    pytest.param({"metadata": {"score": "@"}}, "metadata.score", id="metadata-top"),
    pytest.param(
        {"metadata": {"a": {"b": [{"c": "@"}]}}}, "metadata.a.b[0].c", id="metadata-nested"
    ),
]


def _body(template: dict[str, Any], token: str) -> str:
    return json.dumps(template).replace('"@"', token)


def _created(db) -> list[str]:
    return sorted(key for key in db.docs if key.startswith(("tasks/", "workflows/")))


def _post_raw(client, path: str, text: str):
    return client.post(
        path,
        headers={**auth_header("alice"), "Content-Type": "application/json"},
        content=text,
    )


def _assert_refused_naming(response, db, api_context, path: str) -> dict[str, Any]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["detail"]["path"] == path, body
    assert path in body["message"], body
    assert "finite" in body["message"], body
    assert _created(db) == [], f"refused, but documents were written: {_created(db)}"
    assert api_context.waker.calls == []
    return body


@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize(("placement", "path"), TASK_PLACEMENTS)
def test_a_task_with_a_non_finite_number_is_refused_naming_the_path(
    client, db, api_context, token, placement, path
):
    template = {"runner_profile": "mock", "input": {"prompt": "p"}, **placement}
    response = _post_raw(client, "/v1/tasks", _body(template, token))

    body = _assert_refused_naming(response, db, api_context, path)
    assert body["detail"]["value"] == token


@pytest.mark.parametrize("token", TOKENS)
def test_a_batch_is_refused_whole_when_one_task_carries_a_non_finite_number(
    client, db, api_context, token
):
    template = {
        "tasks": [
            {"runner_profile": "mock", "input": {"prompt": "fine"}},
            {"runner_profile": "mock", "input": {"prompt": "p"}, "metadata": {"x": ["@"]}},
        ]
    }
    response = _post_raw(client, "/v1/tasks/batch", _body(template, token))

    _assert_refused_naming(response, db, api_context, "metadata.x[0]")


WORKFLOW_PLACEMENTS = [
    pytest.param("step_input", "input.weights[2]", id="step-input"),
    pytest.param("step_metadata", "metadata.cost.estimate", id="step-metadata"),
    pytest.param("workflow_metadata", "metadata.budget", id="workflow-metadata"),
]


@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize(("where", "path"), WORKFLOW_PLACEMENTS)
def test_a_workflow_with_a_non_finite_number_is_refused_naming_the_path(
    client, db, api_context, token, where, path
):
    step: dict[str, Any] = {"step_id": "only", "runner_profile": "mock", "input": {"prompt": "p"}}
    template: dict[str, Any] = {"steps": [step]}
    if where == "step_input":
        step["input"]["weights"] = [0.5, 1, "@"]
    elif where == "step_metadata":
        step["metadata"] = {"cost": {"estimate": "@"}}
    else:
        template["metadata"] = {"budget": "@"}

    response = _post_raw(client, "/v1/workflows", _body(template, token))

    body = _assert_refused_naming(response, db, api_context, path)
    if where != "workflow_metadata":
        assert body["detail"]["step_id"] == "only"


def test_finite_floats_are_still_accepted(client, db):
    """The check is about the three non-finite values, not about floats."""
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "input": {"prompt": "p"},
            "metadata": {"ratio": 0.25, "big": 1.0e300, "neg": -3.5, "nested": [{"x": 2.0}]},
        },
    )
    assert response.status_code == 201, response.text
    assert len([k for k in db.docs if k.startswith("tasks/") and k.count("/") == 1]) == 1


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_the_shared_walk_refuses_every_non_finite_float(value):
    for check in (reject_non_finite, validate_storable):
        with pytest.raises(ValidationFailed) as exc:
            check({"a": [{"b": value}]}, label="input", step_id="s1")
        assert exc.value.detail["path"] == "input.a[0].b"
        assert exc.value.detail["step_id"] == "s1"


def test_a_deeply_nested_non_finite_value_is_found_without_recursion():
    payload: Any = math.nan
    for _ in range(5000):
        payload = [payload]
    with pytest.raises(ValidationFailed) as exc:
        reject_non_finite({"deep": payload}, label="metadata")
    assert exc.value.detail["path"].startswith("metadata.deep[0]")
