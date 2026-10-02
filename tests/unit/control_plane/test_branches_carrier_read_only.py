"""`carrier: branches` is refused for a tenant whose forge credential is read-only (D13).

A branches carrier pushes every checkpoint's committed work to the step's
branch. With a read-only token every one of those pushes is refused at the
forge, the worker logs it and goes on, and the caller who asked for durable
branches gets none. The API never reads the token, so it cannot probe the
forge the way the worker's publish does (`forge.probe_repository`'s
`permissions.push`); the scope is the operator's declaration,
`FORGE_READ_ONLY_TENANTS`, read into `ApiSettings.forge_read_only_tenants`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.validation import DispatchOptionError, resolve_dispatch_options
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header

REPO = "https://github.com/saga-xyz/payments.git"


@pytest.fixture
def read_only_client(db, tokens, group_map, objects) -> TestClient:
    ctx = build_context(
        settings=api_settings(forge_read_only_tenants=("eng",)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def test_a_read_only_tenant_is_refused_branches_with_a_422():
    with pytest.raises(DispatchOptionError) as refused:
        resolve_dispatch_options(
            strategy="collect",
            carrier="branches",
            scale="task",
            repository_url=REPO,
            forge_read_only=True,
        )
    assert refused.value.detail["carrier"] == "branches"
    assert refused.value.detail["forge_access"] == "read-only"


def test_a_read_only_tenant_keeps_the_checkpoints_carrier():
    options = resolve_dispatch_options(
        strategy="collect",
        carrier="checkpoints",
        scale="task",
        repository_url=REPO,
        forge_read_only=True,
    )
    assert options.carrier == "checkpoints"


def test_a_read_only_tenant_is_refused_over_http(read_only_client):
    response = read_only_client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "strategy": "collect",
            "carrier": "branches",
            "repository_url": REPO,
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["forge_access"] == "read-only"


def test_a_read_only_tenant_is_refused_for_a_workflow(read_only_client):
    response = read_only_client.post(
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


def test_another_tenant_is_not_affected(read_only_client):
    # bob is in research, which is not on the read-only list.
    response = read_only_client.post(
        "/v1/tasks",
        headers=auth_header("bob"),
        json={
            "runner_profile": "mock",
            "strategy": "collect",
            "carrier": "branches",
            "repository_url": REPO,
        },
    )
    assert response.status_code == 201, response.text
