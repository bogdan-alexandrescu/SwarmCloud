"""Workflow DAG validation.

The headline case is the cycle. A cyclic workflow is the worst kind of bad
submission because it fails SILENTLY: every step parks as DEPENDENCY_INCOMPLETE
waiting for a parent that is itself waiting, nothing holds capacity, nothing
errors, and nothing alerts. It has to be rejected at submission or it is never
rejected at all.
"""

from __future__ import annotations

import pytest

from swarm_api.errors import ValidationFailed
from swarm_api.validation import StepSpec, find_cycle, topological_order, validate_dag

from .conftest import auth_header


def spec(step_id: str, *depends_on: str, input_from: tuple[str, ...] = ()) -> StepSpec:
    return StepSpec(step_id=step_id, depends_on=tuple(depends_on), input_from=input_from)


# -- the required case: a cycle is rejected -------------------------------

def test_two_step_cycle_is_rejected():
    steps = [spec("a", "b"), spec("b", "a")]
    with pytest.raises(ValidationFailed) as exc:
        validate_dag(steps, max_steps=50)
    assert "cycle" in str(exc.value).lower()
    assert exc.value.code == "invalid_dag"
    cycle = exc.value.detail["cycle"]
    assert cycle[0] == cycle[-1], "the reported cycle should be a closed path"
    assert set(cycle) == {"a", "b"}


def test_long_cycle_is_rejected_and_named():
    steps = [spec("a", "d"), spec("b", "a"), spec("c", "b"), spec("d", "c")]
    with pytest.raises(ValidationFailed) as exc:
        validate_dag(steps, max_steps=50)
    cycle = exc.value.detail["cycle"]
    assert cycle[0] == cycle[-1]
    assert set(cycle[:-1]) == {"a", "b", "c", "d"}


def test_self_dependency_is_rejected():
    with pytest.raises(ValidationFailed) as exc:
        validate_dag([spec("a", "a")], max_steps=50)
    assert "depends on itself" in str(exc.value)


def test_cycle_reachable_only_from_a_later_root_is_still_found():
    # `root` is acyclic and is visited first; the cycle hides behind it.
    steps = [spec("root"), spec("x", "y"), spec("y", "x")]
    assert find_cycle(steps) is not None
    with pytest.raises(ValidationFailed):
        validate_dag(steps, max_steps=50)


def test_diamond_is_not_a_cycle():
    steps = [spec("a"), spec("b", "a"), spec("c", "a"), spec("d", "b", "c")]
    order = validate_dag(steps, max_steps=50)
    assert order.index("a") < order.index("b") < order.index("d")
    assert order.index("a") < order.index("c") < order.index("d")


def test_deep_chain_does_not_blow_the_stack():
    # Iterative DFS, not recursion: a 5000-deep chain must validate, not crash.
    steps = [spec("s0")] + [spec(f"s{i}", f"s{i - 1}") for i in range(1, 5000)]
    assert find_cycle(steps) is None
    assert topological_order(steps)[0] == "s0"


# -- the other DAG rules --------------------------------------------------

def test_dependency_outside_the_workflow_is_rejected():
    with pytest.raises(ValidationFailed) as exc:
        validate_dag([spec("a"), spec("b", "ghost")], max_steps=50)
    assert exc.value.detail["missing_dependency"] == "ghost"


def test_duplicate_step_ids_are_rejected():
    with pytest.raises(ValidationFailed) as exc:
        validate_dag([spec("a"), spec("a")], max_steps=50)
    assert "duplicate" in str(exc.value)


def test_input_from_must_also_be_a_dependency():
    steps = [spec("a"), spec("b", input_from=("a",))]
    with pytest.raises(ValidationFailed) as exc:
        validate_dag(steps, max_steps=50)
    assert "does not depend on it" in str(exc.value)


def test_step_limit_is_enforced():
    steps = [spec(f"s{i}") for i in range(11)]
    with pytest.raises(ValidationFailed) as exc:
        validate_dag(steps, max_steps=10)
    assert exc.value.detail["max_workflow_steps"] == 10


def test_empty_workflow_is_rejected():
    with pytest.raises(ValidationFailed):
        validate_dag([], max_steps=50)


# -- and the same thing through the real HTTP surface ---------------------

def test_api_rejects_a_cyclic_workflow(client):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "build", "runner_profile": "mock", "depends_on": ["test"]},
                {"step_id": "test", "runner_profile": "mock", "depends_on": ["build"]},
            ]
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_dag"
    assert "cycle" in body["message"]


def test_api_accepts_a_valid_workflow_and_parks_dependent_steps(client, db):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "build", "runner_profile": "mock"},
                {
                    "step_id": "test",
                    "runner_profile": "mock",
                    "depends_on": ["build"],
                    "input_from": {"build": "artifact.tar"},
                },
            ]
        },
    )
    assert response.status_code == 201, response.text
    workflow = response.json()["workflow"]
    by_step = {s["step_id"]: s for s in workflow["steps"]}
    build_task = db.docs[f"tasks/{by_step['build']['task_id']}"]
    test_task = db.docs[f"tasks/{by_step['test']['task_id']}"]

    # The root is immediately admissible; the dependent step costs nothing
    # until its parent succeeds (invariant 1).
    assert build_task["state"] == "READY"
    assert test_task["state"] == "PARKED"
    assert test_task["park_reason"] == "DEPENDENCY_INCOMPLETE"
    assert test_task["depends_on"] == [build_task["id"]]
    # The artifact reference was rewritten from step id to task id.
    assert test_task["metadata"]["input_from"] == {build_task["id"]: "artifact.tar"}
