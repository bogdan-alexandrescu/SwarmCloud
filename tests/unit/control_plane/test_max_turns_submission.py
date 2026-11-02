"""A step's turn cap on swarm-api's doors (contract request 133, part of #2524).

The owner accepted the request on 2 November 2026. A step asks a cap by
number, `max_turns`, as it asks a shorter `timeout_seconds`: the profile says
whether its runner passes a cap at all (`supports_max_turns`) and may set a
ceiling no step raises (`max_turns`). Held here, red first:

  * a task, a workflow step, a child and both profiles of a `browser_if_ui`
    step resolve the ask beside `validate_timeout`, and refuse it for a
    profile that does not cap turns, creating nothing;
  * a profile ceiling lowers a higher ask, and is the cap of a step that
    names none;
  * a child's cap is never above its parent's resolved cap;
  * no cap asked means no `max_turns`, so the task keeps the format and the
    bytes it had (no running agent changes);
  * the step records what it asked (`WorkflowStep.max_turns`), its task what
    it resolved (`Task.max_turns`, signed at spec format 7);
  * `input.max_turns` stays refused: the cap is a field, never an input;
  * the issue run's requirements check asks for 30 turns, as a field.

Offline: FakeFirestore, a local spec signer. No credentials, no emulator.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import issueruns
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import compile_plan
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.validation import validate_max_turns
from swarm_api.waker import NullWaker
from swarm_common import specsign
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import PROJECT, api_settings, auth_header, seed_tenant
from .spec_signer import LocalSpecSigner
from .test_child_tasks_api import (  # noqa: F401 -- `worker_tokens` is a fixture
    KEY,
    SIGNER as CHILD_SIGNER,
    Attempt,
    _children,
    ready_parent,
    spec_settings,
    submit,
    worker_tokens,
)
from .test_review_gate_default import PLAN, _run

BROWSER = "claude-code-browser"
CODE = "claude-code"
POLICY = {"policy": "browser_if_ui", "browser": BROWSER, "code": CODE}


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def client(db, tokens, group_map, signer) -> TestClient:
    seed_tenant(db, "eng")
    context = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=signer,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _tasks(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("tasks/") and p.count("/") == 1]


def _by_step(db) -> dict[str, dict[str, Any]]:
    return {d["step_id"]: d for d in _tasks(db)}


def _workflow(db) -> dict[str, Any]:
    (workflow,) = [d for p, d in db.docs.items()
                   if p.startswith("workflows/") and p.count("/") == 1]
    return workflow


def _task(client, **extra: Any):
    body = {"runner_profile": CODE, "input": {"prompt": "hello"}, **extra}
    return client.post("/v1/tasks", headers=auth_header("alice"), json=body)


def _steps(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"on_step_failure": "continue", "strategy": "direct-pr",
            "repository_url": "https://github.com/acme/widgets.git", "steps": list(steps)}


def _ceiling(monkeypatch, name: str, **fields: Any) -> None:
    monkeypatch.setitem(RUNNER_PROFILES, name, replace(RUNNER_PROFILES[name], **fields))


# --------------------------------------------------------------------------
# A task
# --------------------------------------------------------------------------


def test_a_task_asking_30_on_claude_code_stores_and_signs_30(client, db, signer):
    response = _task(client, max_turns=30)
    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    # MUTATION: drop `max_turns` from `_build_task`'s Task and this is None.
    assert task["max_turns"] == 30
    assert task["spec_format"] == 7 == specsign.signing_format(task)
    assert signer.verifies(task, task["id"])
    # Inside the signed bytes: raising or removing it fails verification.
    assert not signer.verifies({**task, "max_turns": 300}, task["id"])
    assert not signer.verifies({**task, "max_turns": None}, task["id"])
    assert response.json()["task"]["max_turns"] == 30


def test_no_cap_asked_is_no_cap_and_the_old_format(client, db, signer):
    response = _task(client)
    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    assert task["max_turns"] is None
    assert task["spec_format"] == 1
    assert signer.verifies(task, task["id"])
    assert response.json()["task"]["max_turns"] is None


@pytest.mark.parametrize("profile", ["mock", "codex", "browser"])
def test_a_cap_on_a_profile_that_does_not_cap_turns_is_refused(client, db, profile):
    if not RUNNER_PROFILES[profile].available:
        pytest.skip(f"{profile} is disabled; its refusal comes first")
    response = _task(client, runner_profile=profile, max_turns=30)
    # MUTATION: drop the `supports_max_turns` refusal and this is 201.
    assert response.status_code == 422, response.text
    assert "max_turns" in response.text and "does not cap" in response.text
    assert _tasks(db) == []


@pytest.mark.parametrize("bad", [0, -3, "thirty", 2.5])
def test_a_cap_that_is_not_a_positive_integer_is_refused(client, db, bad):
    assert _task(client, max_turns=bad).status_code == 422
    assert _tasks(db) == []


def test_a_profile_ceiling_lowers_a_higher_ask_and_caps_a_step_that_asks_none(
    client, db, monkeypatch
):
    _ceiling(monkeypatch, CODE, max_turns=20)
    assert _task(client, max_turns=50).status_code == 201
    assert _task(client, max_turns=10, input={"prompt": "lower"}).status_code == 201
    assert _task(client, input={"prompt": "none"}).status_code == 201
    caps = sorted(t["max_turns"] for t in _tasks(db))
    # MUTATION: return the ask unclamped and the 50 is stored.
    assert caps == [10, 20, 20]


def test_input_max_turns_stays_refused(client, db):
    response = _task(client, input={"prompt": "hello", "max_turns": 30})
    assert response.status_code == 422, response.text
    assert _tasks(db) == []


def test_the_resolution_on_its_own():
    code = RUNNER_PROFILES[CODE]
    assert validate_max_turns(code, None) is None
    assert validate_max_turns(code, 30) == 30
    assert validate_max_turns(replace(code, max_turns=12), 30) == 12
    assert validate_max_turns(replace(code, max_turns=12), None) == 12
    assert validate_max_turns(RUNNER_PROFILES["mock"], None) is None
    # A ceiling on a profile whose runner passes no cap caps nothing.
    assert validate_max_turns(replace(RUNNER_PROFILES["mock"], max_turns=5), None) is None


# --------------------------------------------------------------------------
# A workflow step, and both profiles of a browser_if_ui step
# --------------------------------------------------------------------------


def test_a_workflow_step_records_its_ask_and_its_task_the_resolved_cap(
    client, db, signer, monkeypatch
):
    _ceiling(monkeypatch, CODE, max_turns=40)
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_steps(
        {"step_id": "check", "runner_profile": CODE, "input": {"prompt": "check"},
         "max_turns": 60},
        {"step_id": "impl", "runner_profile": "mock", "input": {"prompt": "impl"}},
    ))
    assert response.status_code == 201, response.text
    asked = {s["step_id"]: s.get("max_turns") for s in _workflow(db)["steps"]}
    assert asked == {"check": 60, "impl": None}
    tasks = _by_step(db)
    assert tasks["check"]["max_turns"] == 40 and tasks["impl"]["max_turns"] is None
    assert signer.verifies(tasks["check"], tasks["check"]["id"])
    served = {s["step_id"]: s["max_turns"] for s in response.json()["workflow"]["steps"]}
    assert served == {"check": 60, "impl": None}


def test_a_workflow_step_asking_a_cap_on_a_profile_without_one_is_refused(client, db):
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_steps(
        {"step_id": "impl", "runner_profile": "mock", "input": {"prompt": "impl"},
         "max_turns": 30},
    ))
    assert response.status_code == 422, response.text
    assert "max_turns" in response.text
    assert _tasks(db) == []


def _policy_step(**extra: Any) -> dict[str, Any]:
    return _steps(
        {"step_id": "impl", "runner_profile": CODE, "input": {"prompt": "implement"}},
        {"step_id": "review", "runner_profile": BROWSER, "input": {"prompt": "review"},
         "depends_on": ["impl"], "metadata": {"runtime": dict(POLICY)}, **extra},
    )


def test_a_browser_if_ui_step_carries_one_cap_under_both_its_profiles(client, db, signer):
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=_policy_step(max_turns=30))
    assert response.status_code == 201, response.text
    review = _by_step(db)["review"]
    assert review["max_turns"] == 30
    alternate = review["metadata"]["runtime"]["alternate"]
    assert "max_turns" not in alternate, "one cap on the task, not a swap field"
    assert signer.verifies(review, review["id"])
    swapped = {**review, **{k: alternate[k] for k in alternate}}
    # The alternate's signature covers the same cap: the scheduler's swap
    # leaves `max_turns` as stored, and the claude-code spec still verifies.
    assert signer.verifies(swapped, review["id"])


def test_a_browser_if_ui_step_takes_the_lower_of_its_two_ceilings(client, db, monkeypatch):
    _ceiling(monkeypatch, CODE, max_turns=25)
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=_policy_step(max_turns=30))
    assert response.status_code == 201, response.text
    # MUTATION: drop the alternate's cap from `_build_task` and this is 30.
    assert _by_step(db)["review"]["max_turns"] == 25


@pytest.mark.parametrize("which", [BROWSER, CODE])
def test_a_browser_if_ui_step_is_refused_when_either_profile_does_not_cap(
    client, db, monkeypatch, which
):
    _ceiling(monkeypatch, which, supports_max_turns=False)
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=_policy_step(max_turns=30))
    assert response.status_code == 422, response.text
    assert which in response.text
    assert _tasks(db) == []


# --------------------------------------------------------------------------
# A child
# --------------------------------------------------------------------------


@pytest.fixture
def child_client(db, worker_tokens, group_map, objects) -> TestClient:  # noqa: F811
    ctx = build_context(
        settings=api_settings(child_key=KEY, **spec_settings()), signer=CHILD_SIGNER, db=db,
        verifier=StaticTokenVerifier(worker_tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects,
    )
    seed_tenant(db, "eng")
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def _child(child_client, db, parent_cap: int | None, **child: Any):
    attempt = Attempt()
    ready_parent(child_client, db, attempt, runner_profile=CODE,
                 **({"max_turns": parent_cap} if parent_cap is not None else {}))
    return submit(child_client, attempt, child={
        "runner_profile": CODE, "input": {"prompt": "help"}, **child})


@pytest.mark.parametrize("parent_cap, asked, expected", [
    (30, 50, 30),      # never above the parent's
    (30, None, 30),    # a child naming none inherits the parent's
    (30, 10, 10),      # a lower ask is the child's own
    (None, 12, 12),    # a parent without a cap leaves the child's ask
    (None, None, None),
])
def test_a_childs_cap_is_never_above_its_parents(child_client, db, parent_cap, asked, expected):
    response = _child(child_client, db, parent_cap,
                      **({"max_turns": asked} if asked is not None else {}))
    assert response.status_code == 201, response.text
    (child,) = _children(db).values()
    # MUTATION: drop the parent's cap from `_build_child` and the first is 50.
    assert child["max_turns"] == expected
    assert CHILD_SIGNER.verifies(child, child["id"])


def test_a_child_asking_a_cap_on_a_profile_without_one_is_refused(child_client, db):
    response = _child(child_client, db, 30, runner_profile="mock", max_turns=5)
    assert response.status_code == 422, response.text
    assert _children(db) == {}


# --------------------------------------------------------------------------
# The issue run's requirements check
# --------------------------------------------------------------------------


def test_the_requirements_check_asks_30_turns_as_a_field_not_a_prompt_line():
    spec = compile_plan(_run(PLAN), class_models={"sonnet": "a-sonnet-model"})
    check = {s.step_id: s for s in spec.steps}["requirements-check"]
    # MUTATION: leave `max_turns` off `_requirements_check` and this is None.
    assert check.max_turns == issueruns.REQUIREMENTS_CHECK_MAX_TURNS == 30
    assert check.runner_profile == CODE and RUNNER_PROFILES[CODE].supports_max_turns
    assert "turns" not in check.input["prompt"]


def test_the_requirements_checks_task_carries_30(client, db, signer):
    spec = compile_plan(_run(PLAN))
    check = next(s for s in spec.steps if s.step_id == "requirements-check")
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_steps(
        {"step_id": "impl", "runner_profile": CODE, "input": {"prompt": "implement"}},
        {**check.model_dump(exclude_none=True, exclude_defaults=True),
         "step_id": "requirements-check", "runner_profile": check.runner_profile,
         "depends_on": ["impl"], "builds_on": "impl", "input_from": {}},
    ))
    assert response.status_code == 201, response.text
    task = _by_step(db)["requirements-check"]
    assert task["max_turns"] == 30
    assert signer.verifies(task, task["id"])
