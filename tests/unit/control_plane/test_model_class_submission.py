"""A step picks its model class by name, on every surface that submits one (#1435).

docs/design/model-per-step.md §4.4, lane MODEL-API, with the owner's
decisions of 2026-10-17: a task, a batch entry, a workflow step and a child
each carry `model_class`, a NAME from the frozen `profiles.MODEL_CLASSES`
(`opus`, `fable`, `sonnet`, `haiku`), never a model ID (invariant 10). The
class resolves by the frozen `profiles.step_model_class`, the function the
scheduler and the worker resolve with, so the three cannot disagree.

What is held:

  * each surface accepts the four classes on `claude-code`, and refuses a
    name outside the vocabulary (`unknown_model_class`) and a class the
    profile does not run (`model_class_not_accepted`), creating nothing;
  * a class other than the default is refused `model_class_not_yet_available`
    while this deployment pins no model for it (WORKER_CLASS_MODELS unset).
    There is no enablement flag (owner's R4): the class map is the gate;
  * the default class is NOT stored, so a default step signs at the format it
    had before classes existed; a `fable` step stores its class and signs at
    the format that covers it (5: contract request 104 -- the design called
    it 4, which request 96 had taken first);
  * `Task.model` is filled from the class's pin; a caller's `model` equal to
    it is accepted; another class's pin is refused naming that class, and any
    other value is refused listing the models -- behind its refusal switch,
    which ships off because today's `--model` callers would meet it;
  * `/v1/runtimes` serves each profile's classes, default and models;
  * a served task and workflow step carry the RESOLVED class and whether it
    was defaulted; `attempt_to_api` serves `Attempt.model`;
  * the child route accepts `model_class`, and a child naming none runs its
    own profile's default, never its parent's (owner's R3).

MUTATIONS each test catches (proven in CI by the red-first commit): drop the
`validate_model_class` call in `_build_task` (every refusal test passes a
bad class through); store `model_class` unconditionally (the default-is-not-
stored and old-format tests); drop `check_model_class_available` (the gate
test); fill `model` from `spec.model` alone (the pin tests); drop
`model_class` from `ChildSpec` (the child tests, and the worker's
CHILD_FIELDS equality in tests/unit/worker/test_child_tasks_worker.py).

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups, a local P-256
signer. No credentials, no emulator, no network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.codec import attempt_from_dict, attempt_to_api
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.schedtypes import custom_prompt
from swarm_api.schemas import WorkflowCreate
from swarm_api.waker import NullWaker
from swarm_common.profiles import MODEL_CLASSES, RUNNER_PROFILES

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

#: terraform/infra/locals.tf `runner_models`, as WORKER_CLASS_MODELS carries
#: it to swarm-api (`local.model_class_env`).
CLASS_MODELS = {
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}
#: The three profiles that accept every class (owner's Q3).
CLASSED_PROFILES = ("claude-code", "claude-code-browser", "indexer")
SWITCH_ENV = "REFUSAL_MODEL_IS_CHOSEN_BY_MODEL_CLASS"


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


def _context(db, tokens, group_map, signer, **settings: Any):
    return build_context(
        settings=api_settings(**{"worker_class_models": dict(CLASS_MODELS), **settings}),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=signer,
    )


@pytest.fixture
def client(db, tokens, group_map, signer) -> TestClient:
    return TestClient(create_app(_context(db, tokens, group_map, signer)),
                      raise_server_exceptions=False)


@pytest.fixture
def unpinned_client(db, tokens, group_map, signer) -> TestClient:
    """A deployment with no WORKER_CLASS_MODELS: the gate is closed."""
    return TestClient(
        create_app(_context(db, tokens, group_map, signer, worker_class_models={})),
        raise_server_exceptions=False,
    )


def _tasks(db) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items() if path.startswith("tasks/") and path.count("/") == 1]


def _workflows(db) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items() if path.startswith("workflows/") and path.count("/") == 1]


def _task_body(model_class: str | None = None, profile: str = "claude-code",
               prompt: str = "classed", **extra: Any) -> dict:
    body: dict[str, Any] = {"runner_profile": profile, "input": {"prompt": prompt}, **extra}
    if model_class is not None:
        body["model_class"] = model_class
    return body


def _post(client: TestClient, surface: str, model_class: str | None,
          profile: str = "claude-code", **extra: Any):
    """Submit one claude-code step carrying `model_class` through `surface`."""
    task = _task_body(model_class, profile, **extra)
    if surface == "task":
        return client.post("/v1/tasks", headers=auth_header("alice"), json=task)
    if surface == "batch":
        return client.post("/v1/tasks/batch", headers=auth_header("alice"),
                           json={"tasks": [_task_body(prompt="filler"), task]})
    assert surface == "workflow"
    step = {"step_id": "only", **task}
    step.pop("model", None)
    return client.post("/v1/workflows", headers=auth_header("alice"), json={"steps": [step]})


SURFACES = ("task", "batch", "workflow")


def _the_classed_task(db) -> dict[str, Any]:
    """The task the step under test created (a batch also has a filler)."""
    (task,) = [t for t in _tasks(db) if t["input"].get("prompt") == "classed"]
    return task


# --------------------------------------------------------------------------
# Accepted: the four classes on claude-code, on every surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("model_class", sorted(MODEL_CLASSES))
def test_each_surface_accepts_every_class_on_claude_code(client, db, surface, model_class):
    response = _post(client, surface, model_class)

    assert response.status_code == 201, response.text
    task = _the_classed_task(db)
    # The default is not stored; every other class is.
    assert task["model_class"] == (None if model_class == "opus" else model_class)
    # Task.model is the class's pin at submission (owner's Q4).
    assert task["model"] == CLASS_MODELS[model_class]
    if surface == "workflow":
        (workflow,) = _workflows(db)
        (step,) = workflow["steps"]
        assert step["model_class"] == task["model_class"]


@pytest.mark.parametrize("profile", CLASSED_PROFILES)
def test_the_three_classed_profiles_accept_a_class(profile):
    """The catalogue half of Q3, read the way the route reads it."""
    from swarm_api.validation import validate_model_class

    assert validate_model_class(RUNNER_PROFILES[profile], "haiku") == "haiku"
    assert validate_model_class(RUNNER_PROFILES[profile], None) == "opus"


# --------------------------------------------------------------------------
# Refused: a name outside the vocabulary, a class the profile does not run
# --------------------------------------------------------------------------


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("name", ["gpt-5", "claude-opus-5-5", "Opus"])
def test_a_name_outside_the_vocabulary_is_refused_and_creates_nothing(client, db, surface, name):
    response = _post(client, surface, name)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "unknown_model_class", body
    assert body["detail"]["known_model_classes"] == list(MODEL_CLASSES)
    assert _tasks(db) == [] and _workflows(db) == []


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("profile", ["mock", "browser"])
def test_a_class_on_a_profile_that_runs_none_is_refused_and_creates_nothing(
    client, db, surface, profile
):
    response = _post(client, surface, "sonnet", profile=profile)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "model_class_not_accepted", body
    assert body["detail"]["runner_profile"] == profile
    assert body["detail"]["accepted_model_classes"] == []
    assert "runs no Claude model" in body["message"]
    assert _tasks(db) == [] and _workflows(db) == []


def test_a_workflow_refusal_names_the_step(client, db):
    response = client.post("/v1/workflows", headers=auth_header("alice"), json={"steps": [
        {"step_id": "plan", "runner_profile": "claude-code", "input": {"prompt": "p"},
         "model_class": "fable"},
        {"step_id": "tidy", "runner_profile": "claude-code", "input": {"prompt": "t"},
         "depends_on": ["plan"], "model_class": "tiny"},
    ]})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["step_id"] == "tidy"
    assert _tasks(db) == [] and _workflows(db) == []


# --------------------------------------------------------------------------
# The gate: a class with no pin on this deployment
# --------------------------------------------------------------------------


@pytest.mark.parametrize("surface", SURFACES)
def test_a_non_default_class_is_not_yet_available_without_a_class_map(
    unpinned_client, db, surface
):
    response = _post(unpinned_client, surface, "sonnet")

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "model_class_not_yet_available", body
    assert body["detail"]["default_model_class"] == "opus"
    assert _tasks(db) == [] and _workflows(db) == []


@pytest.mark.parametrize("surface", SURFACES)
def test_the_default_class_is_available_without_a_class_map(unpinned_client, db, surface):
    response = _post(unpinned_client, surface, "opus")

    assert response.status_code == 201, response.text
    for task in _tasks(db):
        assert task["model_class"] is None
        # No pin to fill from: recorded as sent, here not at all.
        assert task["model"] is None


# --------------------------------------------------------------------------
# What is stored and what is signed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("model_class", [None, "opus"])
def test_the_default_is_not_stored_and_signs_at_the_old_format(client, db, signer, model_class):
    response = _post(client, "task", model_class)

    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    assert task["model_class"] is None
    # Format 1: no parent, no forge field, no repositories, no class -- the
    # bytes this task had before model classes existed.
    assert task["spec_format"] == 1
    assert signer.verifies(task, task["id"])


@pytest.mark.parametrize("surface", ["task", "workflow"])
def test_a_fable_step_is_stored_and_signs_at_the_format_that_covers_its_class(
    client, db, signer, surface
):
    response = _post(client, surface, "fable")

    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    assert task["model_class"] == "fable"
    # Format 5: the class's own; 6 (request 120) covers a budget, set on none here.
    assert task["spec_format"] == 5
    assert signer.verifies(task, task["id"])
    # The class is inside the signed bytes: rewriting it fails verification.
    rewritten = {**task, "model_class": "haiku"}
    assert not signer.verifies(rewritten, task["id"])


# --------------------------------------------------------------------------
# TaskCreate.model: filled from the pin (owner's Q4)
# --------------------------------------------------------------------------


def test_model_is_filled_from_the_pin_when_none_is_sent(client, db):
    response = _post(client, "task", "haiku")

    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    assert task["model"] == CLASS_MODELS["haiku"]


@pytest.mark.parametrize("model_class", [None, "sonnet"])
def test_a_model_equal_to_the_pin_is_accepted(client, db, model_class):
    pin = CLASS_MODELS[model_class or "opus"]
    response = _post(client, "task", model_class, model=pin)

    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    assert task["model"] == pin


def test_another_classes_pin_is_refused_naming_that_class(client, db, monkeypatch):
    monkeypatch.setenv(SWITCH_ENV, "on")
    response = _post(client, "task", None, model=CLASS_MODELS["sonnet"])

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "model_is_chosen_by_model_class", body
    assert body["detail"]["model_class"] == "sonnet"
    assert 'to run claude-sonnet-5-5, send model_class: "sonnet"' in body["message"]
    assert _tasks(db) == []


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "gpt-5", "claude-opus-5"])
def test_any_other_model_is_refused_listing_the_classes_and_models(client, db, monkeypatch, model):
    monkeypatch.setenv(SWITCH_ENV, "on")
    response = _post(client, "task", "opus", model=model)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "model_is_chosen_by_model_class", body
    assert body["detail"]["models"] == CLASS_MODELS
    assert "model_class" not in body["detail"]
    for name, model_id in CLASS_MODELS.items():
        assert f"{name} ({model_id})" in body["message"]
    assert _tasks(db) == []


def test_the_model_refusal_ships_off_and_off_records_the_model_as_sent(client, db, monkeypatch):
    """refusals.py: a new refusal that can meet an existing caller is a
    switch, report-only by default. Off, the request is let through."""
    monkeypatch.delenv(SWITCH_ENV, raising=False)
    response = _post(client, "task", None, model="claude-opus-5")

    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    assert task["model"] == "claude-opus-5"
    assert task["model_class"] is None


# --------------------------------------------------------------------------
# What is served
# --------------------------------------------------------------------------


def test_runtimes_serves_each_profiles_classes_and_models(client):
    response = client.get("/v1/runtimes", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    served = response.json()["runtimes"]

    for name in CLASSED_PROFILES:
        assert served[name]["model_classes"] == ["opus", "fable", "sonnet", "haiku"]
        assert served[name]["default_model_class"] == "opus"
        assert served[name]["models"] == CLASS_MODELS
    for name in ("mock", "codex", "browser", "merge"):
        assert served[name]["model_classes"] == []
        assert served[name]["default_model_class"] is None
        assert served[name]["models"] == {}


def test_runtimes_serves_no_models_on_a_deployment_that_pins_none(unpinned_client):
    served = unpinned_client.get("/v1/runtimes", headers=auth_header("alice")).json()["runtimes"]
    assert served["claude-code"]["model_classes"] == ["opus", "fable", "sonnet", "haiku"]
    assert served["claude-code"]["models"] == {}


@pytest.mark.parametrize("model_class, served, defaulted", [
    (None, "opus", True), ("opus", "opus", True), ("haiku", "haiku", False),
])
def test_a_served_task_and_step_carry_the_resolved_class(client, db, model_class, served, defaulted):
    response = _post(client, "workflow", model_class)
    assert response.status_code == 201, response.text
    workflow_id = response.json()["workflow"]["workflow_id"]
    (task,) = _tasks(db)

    got = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("alice")).json()
    task_view = got.get("task", got)
    assert task_view["model_class"] == served
    assert task_view["model_class_defaulted"] is defaulted

    wf = client.get(f"/v1/workflows/{workflow_id}", headers=auth_header("alice")).json()
    (step,) = wf["workflow"]["steps"]
    assert step["model_class"] == served
    assert step["model_class_defaulted"] is defaulted


def test_served_model_class_on_a_classless_or_unknown_profile():
    from swarm_api.codec import served_model_class

    assert served_model_class("mock", None) == {"model_class": None, "model_class_defaulted": False}
    # Written past the API: served as stored, not resolved, never raised.
    assert served_model_class("mock", "haiku") == {"model_class": "haiku", "model_class_defaulted": False}
    assert served_model_class("gone", None) == {"model_class": None, "model_class_defaulted": False}


def test_attempt_to_api_serves_the_model_the_attempt_ran():
    now = datetime.now(timezone.utc)
    doc = {
        "attempt_id": "att_1", "task_id": "task_1", "tenant_id": "eng", "lease_id": "lease_1",
        "generation": 1, "backend": "GKE_AUTOPILOT", "state": "RUNNING", "created_at": now,
        "started_at": now, "model": "claude-haiku-5-5",
    }
    assert attempt_to_api(attempt_from_dict(doc))["model"] == "claude-haiku-5-5"
    assert attempt_to_api(attempt_from_dict({**doc, "model": None}))["model"] is None


# --------------------------------------------------------------------------
# The class map swarm-api reads, refused as the scheduler refuses its copy
# --------------------------------------------------------------------------


def test_the_class_map_is_parsed_and_checked_against_the_catalogue():
    import json

    from swarm_api.settings import parse_worker_class_models

    assert parse_worker_class_models(json.dumps(CLASS_MODELS)) == CLASS_MODELS
    assert parse_worker_class_models("") == {}
    for wrong in (
        {**CLASS_MODELS, "haiku": "claude-opus-5-5"},     # an ID of another class
        {**CLASS_MODELS, "sonnet": "claude-sonnet-4-6"},  # below the class's floor
        {k: v for k, v in CLASS_MODELS.items() if k != "fable"},  # a class with no pin
        {**CLASS_MODELS, "opus-fast": "claude-opus-5-5"},  # not a class
        {"claude-code": "claude-opus-5-5"},               # WORKER_MODELS's shape
    ):
        with pytest.raises(ValueError, match="WORKER_CLASS_MODELS"):
            parse_worker_class_models(json.dumps(wrong))


# --------------------------------------------------------------------------
# custom-prompt: a saved spec keeps its approved digest
# --------------------------------------------------------------------------


def test_a_custom_prompt_spec_without_a_class_keeps_its_digest_and_one_with_a_class_changes_it():
    steps = [{"step_id": "only", "runner_profile": "claude-code", "input": {"prompt": "p"}}]
    plain = WorkflowCreate.model_validate({"steps": steps})
    assert "model_class" not in custom_prompt.spec_json(plain)["steps"][0]
    classed = WorkflowCreate.model_validate({"steps": [{**steps[0], "model_class": "haiku"}]})
    assert custom_prompt.spec_json(classed)["steps"][0]["model_class"] == "haiku"
    assert custom_prompt.spec_digest(plain) != custom_prompt.spec_digest(classed)


# --------------------------------------------------------------------------
# The child route
# --------------------------------------------------------------------------


def _child_client(db, worker_tokens, group_map, objects) -> TestClient:
    ctx = build_context(
        settings=api_settings(child_key=KEY, worker_class_models=dict(CLASS_MODELS),
                              **spec_settings()),
        signer=CHILD_SIGNER,
        db=db,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.mark.parametrize("model_class", sorted(MODEL_CLASSES))
def test_the_child_route_accepts_every_class(db, worker_tokens, group_map, objects, model_class):
    child_client = _child_client(db, worker_tokens, group_map, objects)
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    response = submit(child_client, attempt, child={
        "runner_profile": "claude-code", "input": {"prompt": "help"}, "model_class": model_class,
    })

    assert response.status_code == 201, response.text
    (child,) = _children(db).values()
    assert child["model_class"] == (None if model_class == "opus" else model_class)
    assert child["model"] == CLASS_MODELS[model_class]


def test_a_child_naming_no_class_runs_its_own_profiles_default_not_its_parents(
    db, worker_tokens, group_map, objects
):
    child_client = _child_client(db, worker_tokens, group_map, objects)
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt, runner_profile="claude-code", model_class="haiku")
    response = submit(child_client, attempt, child={
        "runner_profile": "claude-code", "input": {"prompt": "help"},
    })

    assert response.status_code == 201, response.text
    (child,) = _children(db).values()
    assert child["model_class"] is None
    assert child["model"] == CLASS_MODELS["opus"]


@pytest.mark.parametrize("child, code", [
    ({"runner_profile": "claude-code", "model_class": "claude-haiku-5-5"}, "unknown_model_class"),
    ({"runner_profile": "mock", "model_class": "haiku"}, "model_class_not_accepted"),
])
def test_the_child_route_refuses_a_bad_class(db, worker_tokens, group_map, objects, child, code):
    child_client = _child_client(db, worker_tokens, group_map, objects)
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    response = submit(child_client, attempt, child={**child, "input": {"prompt": "help"}})

    assert response.status_code == 422, response.text
    assert response.json()["code"] == code
    assert _children(db) == {}


def test_the_child_routes_model_is_held_to_the_pin(db, worker_tokens, group_map, objects, monkeypatch):
    monkeypatch.setenv(SWITCH_ENV, "on")
    child_client = _child_client(db, worker_tokens, group_map, objects)
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    response = submit(child_client, attempt, child={
        "runner_profile": "claude-code", "input": {"prompt": "help"},
        "model": CLASS_MODELS["haiku"],
    })

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "model_is_chosen_by_model_class"
    assert response.json()["detail"]["model_class"] == "haiku"
    assert _children(db) == {}
