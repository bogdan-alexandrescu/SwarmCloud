"""`carrier: branches` is refused when the tenant's forge token cannot push (D13).

A branches carrier pushes the step's committed work to its branch at park,
cancel, SIGTERM and finish. With a token that cannot push, every one of those
pushes is refused at the forge and the caller who asked for durable branches
gets none. Owner decision, 2026-10-02: the API refuses it with a 422 from the
token's REAL write scope, asked the way the worker's publish asks it
(`swarm_api.forge_scope`, which calls `agent_worker.secrets.resolve_git_token`
and `agent_worker.forge.probe_repository`), not from a declared list.

MUTATIONS: make `_require_push_scope` return at once -- the read-only tests
fail with 201. Ask it for `checkpoints` too -- the checkpoints test fails on
the scope's call count. Put the token into the 503's message or a log line --
the token tests fail.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge_scope import SecretManagerForgeScope, StaticForgeScope
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_tenant

REPO = "https://github.com/saga-xyz/payments.git"
#: A token-shaped value that must never reach a response or a log line.
TOKEN = "ghp_SECRETtokenVALUEnever0in0output0001"


def _client(db, tokens, group_map, objects, scope: Any) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_scope=scope,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def _task(client: TestClient, carrier: str = "branches", repository_url: str | None = REPO):
    body: dict[str, Any] = {"runner_profile": "mock", "strategy": "collect", "carrier": carrier}
    if repository_url is not None:
        body["repository_url"] = repository_url
    return client.post("/v1/tasks", headers=auth_header("alice"), json=body)


# --------------------------------------------------------------------------
# The service's decision, over a fixed scope
# --------------------------------------------------------------------------


def test_a_token_that_cannot_push_is_refused_branches_with_a_422(db, tokens, group_map, objects):
    scope = StaticForgeScope(can_push=False, reason="the token has pull but not push")
    response = _task(_client(db, tokens, group_map, objects, scope))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["forge_access"] == "read-only"
    assert body["detail"]["reason"] == "the token has pull but not push"
    assert scope.calls == [("eng", REPO)]


def test_a_token_that_can_push_is_accepted(db, tokens, group_map, objects):
    scope = StaticForgeScope(can_push=True)
    response = _task(_client(db, tokens, group_map, objects, scope))
    assert response.status_code == 201, response.text
    assert response.json()["task"]["dispatch"]["carrier"] == "branches"
    assert scope.calls == [("eng", REPO)]


def test_checkpoints_never_asks_the_forge(db, tokens, group_map, objects):
    scope = StaticForgeScope(can_push=False)
    response = _task(_client(db, tokens, group_map, objects, scope), carrier="checkpoints")
    assert response.status_code == 201, response.text
    assert scope.calls == [], "a carrier that pushes nothing read a secret"


def test_branches_with_no_repository_is_refused_before_any_forge_call(db, tokens, group_map, objects):
    scope = StaticForgeScope(can_push=True)
    response = _task(_client(db, tokens, group_map, objects, scope), repository_url=None)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["missing"] == "repository_url"
    assert scope.calls == []


def test_a_workflow_is_refused_the_same_way(db, tokens, group_map, objects):
    scope = StaticForgeScope(can_push=False)
    response = _client(db, tokens, group_map, objects, scope).post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "repository_url": REPO,
            "carrier": "branches",
            "steps": [{"step_id": "a", "runner_profile": "mock"}],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["forge_access"] == "read-only"


# --------------------------------------------------------------------------
# The deployed scope: the tenant's git secret, asked of the forge
# --------------------------------------------------------------------------


class _Secrets:
    """Secret Manager's `access_secret_version`, over one payload or one failure."""

    def __init__(self, payload: str | None = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.names: list[str] = []

    def access_secret_version(self, request: dict[str, str]) -> Any:
        self.names.append(request["name"])
        if self.error is not None:
            raise self.error
        return SimpleNamespace(payload=SimpleNamespace(data=str(self.payload).encode()))


@pytest.fixture
def forge(monkeypatch):
    """`agent_worker.forge._request`, answering with fixed permissions."""
    seen: dict[str, Any] = {"calls": []}

    def fake_request(url: str, *, token: str, method: str = "GET", payload: Any = None):
        seen["calls"].append((method, url, token))
        return 200, {"default_branch": "main", "permissions": seen["permissions"]}

    import agent_worker.forge as forge_mod

    monkeypatch.setattr(forge_mod, "_request", fake_request)
    return seen


def test_the_deployed_scope_reads_the_tenants_git_secret_and_probes_push(
    db, tokens, group_map, objects, forge, caplog
):
    seed_tenant(db, "eng", credentials=("git",))
    secrets = _Secrets(payload=TOKEN)
    forge["permissions"] = {"admin": False, "push": False, "pull": True}
    caplog.set_level(logging.DEBUG)

    response = _task(_client(db, tokens, group_map, objects, SecretManagerForgeScope("p", client=secrets)))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "the token has pull but not push"
    # Exactly the tenant's own git secret, latest version; one GET on the repo.
    assert secrets.names == ["projects/p/secrets/swarm-tenant-eng-git/versions/latest"]
    assert forge["calls"] == [("GET", "https://api.github.com/repos/saga-xyz/payments", TOKEN)]
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text


def test_the_deployed_scope_admits_a_token_that_can_push(db, tokens, group_map, objects, forge, caplog):
    seed_tenant(db, "eng", credentials=("git",))
    forge["permissions"] = {"admin": False, "push": True, "pull": True}
    caplog.set_level(logging.DEBUG)

    response = _task(
        _client(db, tokens, group_map, objects, SecretManagerForgeScope("p", client=_Secrets(payload=TOKEN)))
    )

    assert response.status_code == 201, response.text
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text


def test_a_tenant_with_no_git_credential_is_refused_without_a_secret_read(
    db, tokens, group_map, objects, forge
):
    seed_tenant(db, "eng", credentials=())
    secrets = _Secrets(payload=TOKEN)
    forge["permissions"] = {"push": True}

    response = _task(_client(db, tokens, group_map, objects, SecretManagerForgeScope("p", client=secrets)))

    assert response.status_code == 422, response.text
    assert "no git credential" in response.json()["detail"]["reason"]
    assert secrets.names == []
    assert forge["calls"] == []


def test_an_unreadable_secret_is_a_503_that_names_no_token(db, tokens, group_map, objects, forge, caplog):
    seed_tenant(db, "eng", credentials=("git",))
    # An error whose text carries the token: neither the response nor the
    # log may repeat it, so only the exception's type is ever logged.
    secrets = _Secrets(error=PermissionError(f"denied while holding {TOKEN}"))
    forge["permissions"] = {"push": True}
    caplog.set_level(logging.DEBUG)

    response = _task(_client(db, tokens, group_map, objects, SecretManagerForgeScope("p", client=secrets)))

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["forge_access"] == "unknown"
    assert forge["calls"] == [], "the forge was asked with no token in hand"
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text
