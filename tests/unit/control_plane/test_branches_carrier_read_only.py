"""swarm-api admits `carrier: branches` without reading any tenant's git secret (D13).

A branches carrier pushes the step's committed work to its branch, and a
token that cannot push would make every one of those pushes fail. Owner
decision, 2026-10-02: that check belongs to the WORKER, not the API. Answering
it means reading the tenant's git secret, and exactly one identity -- the
tenant's own worker GSA -- may read each secret
(terraform/modules/secret_manager/main.tf). So the API checks only what it can
check without a secret: a branches carrier needs a repository (422 without
one). Whether the token can push is asked by the worker before the agent runs,
and a read-only token fails the attempt `forge_read_only`
(tests/unit/worker/test_carrier_branches.py).

MUTATIONS: construct a `SecretManagerServiceClient` on the submit path -- the
"never reads a secret" tests fail on the recorded construction. Import
`agent_worker` anywhere in swarm_api -- the dependency test fails. Drop the
repository requirement for branches -- the 422 test fails with 201.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

from .conftest import auth_header, seed_tenant

REPO = "https://github.com/saga-xyz/payments.git"
API_ROOT = pathlib.Path(__file__).resolve().parents[3] / "apps" / "swarm-api"


@pytest.fixture
def secret_clients(monkeypatch) -> list[tuple[Any, ...]]:
    """Every Secret Manager client constructed while the test runs."""
    from google.cloud import secretmanager

    built: list[tuple[Any, ...]] = []

    class _Recorder:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append((args, kwargs))

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"swarm-api called Secret Manager's {name}")

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", _Recorder)
    return built


def _task(client, carrier: str = "branches", repository_url: str | None = REPO):
    body: dict[str, Any] = {"runner_profile": "mock", "strategy": "collect", "carrier": carrier}
    if repository_url is not None:
        body["repository_url"] = repository_url
    return client.post("/v1/tasks", headers=auth_header("alice"), json=body)


def test_branches_with_a_repository_is_accepted_and_reads_no_secret(client, db, secret_clients):
    # A tenant that HAS a git credential: the API still never reads it.
    seed_tenant(db, "eng", credentials=("git",))

    response = _task(client)

    assert response.status_code == 201, response.text
    assert response.json()["task"]["dispatch"]["carrier"] == "branches"
    assert secret_clients == [], "swarm-api constructed a Secret Manager client on submit"


def test_a_branches_workflow_is_accepted_and_reads_no_secret(client, db, secret_clients):
    seed_tenant(db, "eng", credentials=("git",))

    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "repository_url": REPO,
            "carrier": "branches",
            "steps": [{"step_id": "a", "runner_profile": "mock"}],
        },
    )

    assert response.status_code == 201, response.text
    assert secret_clients == []


def test_branches_with_no_repository_is_refused_with_a_422(client, secret_clients):
    response = _task(client, repository_url=None)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["missing"] == "repository_url"
    assert secret_clients == []


def test_swarm_api_does_not_depend_on_the_worker():
    """The API holds no path to a tenant's git secret: no agent_worker import, no dependency."""
    pyproject = (API_ROOT / "pyproject.toml").read_text()
    assert "swarm-agent-worker" not in pyproject
    # Import statements only: several modules name agent_worker in prose,
    # describing the worker's side of a shared format.
    importing = re.compile(r"^\s*(?:from|import)\s+agent_worker\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(API_ROOT))
        for path in (API_ROOT / "swarm_api").rglob("*.py")
        if importing.search(path.read_text())
    ]
    assert offenders == [], f"swarm_api imports the worker: {offenders}"
