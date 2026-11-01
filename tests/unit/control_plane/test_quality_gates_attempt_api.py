"""The worker's question: did this attempt's workspace turn the quality gates on, by a block swarm-api signed? (contract request 128)

`POST /v1/attempts/{attempt_id}/quality-gates` answers
`swarm_api.qualitygates.worker_verdict` for the attempt's OWN tenant. The
block's MAC is an HMAC under the one `swarm-settings-key`, which no worker
holds (a worker that did could sign any tenant's block), so the worker asks
the service that holds it. Drives the real route over the in-memory
Firestore with a worker identity, an attempt key and a signed spec, exactly
as `test_attempt_graph_context_api` drives the graph route.

What each case pins:

* a block swarm-api signed is on; one that says on with no MAC, with a MAC
  over other values, or with another tenant's MAC is off, and says why;
* the tenant read is the proven attempt's, never one the caller names
  (invariants 9 and 10);
* an unproven caller is one 403 and a stale generation 409 (invariant 5).
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import judgment, qualitygates
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import seed_tenant
from .spec_signer import KEY_VERSION
from .test_attempt_graph_context_api import SIGNER, running_task
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    KEY,
    OTHER_WORKER_TOKEN,
    WORKER_TOKEN,
    Attempt,
    worker_tokens,
)

SETTINGS_KEY = "settings-" + secrets.token_hex(24)


def build(db, worker_tokens, group_map, objects, *, settings_key: str) -> TestClient:  # noqa: F811
    from .conftest import api_settings

    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build_context(
        settings=api_settings(
            child_key=KEY,
            spec_signing_key_version=KEY_VERSION,
            spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}),
            settings_key=settings_key,
        ),
        db=db,
        signer=SIGNER,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def client(db, worker_tokens, group_map, objects) -> TestClient:  # noqa: F811
    return build(db, worker_tokens, group_map, objects, settings_key=SETTINGS_KEY)


def signed(tenant_id: str, jev: bool, key: str = SETTINGS_KEY) -> dict[str, Any]:
    """The block as `QualityGatesStore.put` writes it under `key`."""
    block: dict[str, Any] = {"jev": jev, "set_by": "root@example.com"}
    block["mac"] = judgment.sign_block(key, tenant_id=tenant_id, block="quality_gates",
                                       fields=block)
    return block


def call(client, attempt: Attempt, *, token: str = WORKER_TOKEN, sign: bool = True,
         signer: Attempt | None = None, extra: dict[str, Any] | None = None) -> Any:
    """The worker's call, shaped exactly as `agent_worker.qualitygates.GatesRoute` sends it."""
    t = attempt.tuple
    body = json.dumps({
        "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "lease_id": t.lease_id, "generation": t.generation, **(extra or {}),
    }, sort_keys=True).encode("utf-8")
    path = f"/v1/attempts/{t.attempt_id}/quality-gates"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sign:
        headers.update((signer or attempt).sign("POST", path, body, at=None))
    return client.post(path, content=body, headers=headers)


def test_a_signed_on_is_on(client, db):
    attempt, _ = running_task(client, db)
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "quality_gates": signed("eng", True)}
    response = call(client, attempt)
    # MUTATION: no worker route in routes/quality_gates.py -> 404, not 200.
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "jev": True, "refused": None}


@pytest.mark.parametrize("block, refused", [
    ({"jev": True}, qualitygates.REFUSED_UNSIGNED),
    ({**signed("eng", False), "jev": True}, qualitygates.REFUSED_MISMATCH),
    (signed("research", True), qualitygates.REFUSED_MISMATCH),
    (signed("eng", True, key="settings-" + secrets.token_hex(24)), qualitygates.REFUSED_MISMATCH),
], ids=["no MAC", "a MAC over off", "research's MAC", "another key's MAC"])
def test_an_on_swarm_api_did_not_sign_is_off_and_says_why(client, db, block, refused):
    attempt, _ = running_task(client, db)
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "quality_gates": block}
    response = call(client, attempt)
    assert response.status_code == 200, response.text
    # MUTATION: read `jev is True` without block_verifies -> a forged on is served as on.
    assert response.json() == {"ok": True, "jev": False, "refused": refused}


def test_off_and_unset_are_off_with_no_refusal(client, db):
    attempt, _ = running_task(client, db)
    assert call(client, attempt).json() == {"ok": True, "jev": False, "refused": None}
    db.docs["workspace_settings/eng"] = {"quality_gates": signed("eng", False)}
    assert call(client, attempt).json() == {"ok": True, "jev": False, "refused": None}


def test_a_deployment_with_no_settings_key_trusts_nothing(db, worker_tokens, group_map,  # noqa: F811
                                                          objects):
    client = build(db, worker_tokens, group_map, objects, settings_key="")
    attempt, _ = running_task(client, db)
    db.docs["workspace_settings/eng"] = {"quality_gates": signed("eng", True)}
    assert call(client, attempt).json() == {
        "ok": True, "jev": False, "refused": qualitygates.REFUSED_NO_KEY}


def test_only_the_attempts_own_workspace_is_read(client, db):
    attempt, _ = running_task(client, db)
    db.docs["workspace_settings/research"] = {"quality_gates": signed("research", True)}
    # MUTATION: read the tenant from anything but the proven tuple -> research's on.
    assert call(client, attempt).json()["jev"] is False
    response = call(client, attempt, extra={"target_tenant_id": "research"})
    # MUTATION: a body model without extra="forbid" -> 200 for a caller-named tenant.
    assert response.status_code == 422, response.text


def test_an_unproven_caller_is_the_same_403(client, db):
    attempt, _ = running_task(client, db)
    db.docs["workspace_settings/eng"] = {"quality_gates": signed("eng", True)}
    answers = {
        "no ID token": call(client, attempt, token=""),
        "a person's ID token": call(client, attempt, token="token-alice"),
        "another tenant's worker": call(client, attempt, token=OTHER_WORKER_TOKEN),
        "no proof": call(client, attempt, sign=False),
        "a key this attempt never registered": call(client, attempt, signer=Attempt()),
    }
    statuses = {cause: r.status_code for cause, r in answers.items()}
    # MUTATION: skip `AttemptContext._prove` -> 200 for these.
    assert set(statuses.values()) == {403}, statuses
    assert call(client, attempt).status_code == 200  # the control


def test_a_stale_generation_is_409(client, db):
    attempt, doc = running_task(client, db)
    db.docs["workspace_settings/eng"] = {"quality_gates": signed("eng", True)}
    doc["current_generation"] = 2
    doc["current_lease_id"] = "lease_parent_2"
    response = call(client, attempt)
    # MUTATION: skip `AttemptContext._fence` -> 200 to a stale attempt.
    assert response.status_code == 409, response.text
