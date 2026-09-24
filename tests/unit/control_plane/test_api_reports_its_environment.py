"""`GET /v1/tenants/me` says which environment this API is running as.

WHY. `apps/swarm-ui/src/Brand.tsx` draws the environment badge every screen
carries, and says at length why it may only draw something MEASURED: "a
console that says 'dev' while pointed at production is worse than one that
says nothing". Its comment records the gap this closes -- "the API serves 41
routes and none of them report `Settings.core.environment`" -- and
docs/web-ui/ui-audit-and-build-prompt.md §B9.S5 specifies the shape:

    GET /v1/tenants/me  ->  { ..., "environment": "dev" | "staging" | "prod" }

WHY A SECOND FIELD. The frozen `Settings.from_env` DEFAULTS `ENVIRONMENT` to
"dev" when the variable is unset. Served alone, that default is
indistinguishable from a deployment that declared "dev" -- the exact
unmeasured badge Brand.tsx refuses to draw. `environment_declared` is false
unless the process was actually told, so a client can treat an undeclared
environment as unknown (Brand.tsx's loud case) instead of as dev.

Server side only. Reading it is the UI lane's change.

Offline: the real route over FakeFirestore.
"""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.settings import ApiSettings
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, core_settings


def _client(db, tokens, group_map, objects, settings: ApiSettings) -> TestClient:
    ctx = build_context(
        settings=settings,
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def _me(client, user: str = "alice") -> dict:
    response = client.get("/v1/tenants/me", headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


def test_me_reports_a_declared_environment(db, tokens, group_map, objects):
    settings = dataclasses.replace(
        api_settings(core=core_settings(environment="staging")),
        environment_declared=True,
    )
    body = _me(_client(db, tokens, group_map, objects, settings))

    assert body["environment"] == "staging", (
        "the environment must be the value this process runs as, served as the "
        "plain string §B9.S5 specifies"
    )
    assert body["environment_declared"] is True


def test_me_says_when_nobody_declared_the_environment(db, tokens, group_map, objects):
    """The frozen default is "dev". Reporting it as though it were measured is
    the badge Brand.tsx exists not to draw."""
    settings = dataclasses.replace(
        api_settings(core=core_settings(environment="dev")),
        environment_declared=False,
    )
    body = _me(_client(db, tokens, group_map, objects, settings))

    assert body["environment"] == "dev"
    assert body["environment_declared"] is False


def test_every_caller_gets_it_not_only_admins(db, tokens, group_map, objects):
    """The badge is on every screen, for every signed-in user."""
    settings = dataclasses.replace(
        api_settings(core=core_settings(environment="prod")),
        environment_declared=True,
    )
    client = _client(db, tokens, group_map, objects, settings)
    for user in ("alice", "carol", "root"):
        assert _me(client, user)["environment"] == "prod", user


def test_from_env_marks_an_unset_environment_as_undeclared(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    settings = ApiSettings.from_env()

    assert settings.core.environment == "dev", "the frozen default this guards against"
    assert settings.environment_declared is False


def test_from_env_marks_an_empty_environment_as_undeclared(monkeypatch):
    """`ENVIRONMENT=` is a variable that exists and says nothing."""
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "  ")

    assert ApiSettings.from_env().environment_declared is False


def test_from_env_marks_a_set_environment_as_declared(monkeypatch):
    """terraform/infra/locals.tf sets ENVIRONMENT = var.environment on every
    service through `common_env`, so a deployed API reports declared."""
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    settings = ApiSettings.from_env()

    assert settings.core.environment == "dev"
    assert settings.environment_declared is True


def test_a_directly_built_settings_object_does_not_claim_a_declaration():
    """The safe default. Anything that builds ApiSettings by hand and forgets
    the flag gets 'not declared' -- the loud badge -- rather than a quiet one."""
    assert api_settings().environment_declared is False
