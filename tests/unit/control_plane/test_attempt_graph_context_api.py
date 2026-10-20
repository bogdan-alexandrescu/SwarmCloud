"""The graph context route: swarm-api resolves a step's graph snapshot (lane GRAPH-STAGING, part of #1694).

`POST /v1/attempts/{attempt_id}/context/graph` answers what
`agent_worker.graphstage` needs to stage a step's graph -- the registration of
the task's SIGNED repository, its kept index versions (the promoted one among
them) and the two settings documents the decision reads -- so the worker
never lists Firestore documents itself. Its role has no `entities.list`
(docs/design/tenant-settings-integrity.md §1.3), and in 73 of 74 lanes on
2026-10-19/20 the list query in `graphstage._candidates` was refused or never
reached, so no implement step got the code map.

Drives the real route over the in-memory Firestore, with a worker identity
and an attempt key registered as a worker registers one, and a spec signed by
a key the settings trust, exactly as `test_attempt_context_api` drives the
in-flight route beside it.

What each case pins:

* a promoted index of the signed repository is served, with its versions and
  settings (the control for every refusal below);
* a repository with no registration, and one registered by ANOTHER tenant,
  are served as no registration and no versions (invariant 9);
* the caller names no repository, tenant or commit (invariant 10);
* an unproven caller is one 403, a stale generation 409, an unsigned spec 422,
  and nothing is read for any of them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker

from .conftest import seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    KEY,
    OTHER_WORKER_TOKEN,
    WORKER_TOKEN,
    Attempt,
    ready_parent,
    worker_tokens,
)

SIGNER = LocalSpecSigner()
REPOSITORY_URL = "https://github.com/acme/widgets.git"
OWN_REPO_ID = repo_id_for("eng", "acme", "widgets")
THEIR_REPO_ID = repo_id_for("research", "acme", "widgets")
PROMOTED = "c" * 40
OLDER = "b" * 40


@pytest.fixture
def client(db, worker_tokens, group_map, objects) -> TestClient:  # noqa: F811
    from .conftest import api_settings

    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build_context(
        settings=api_settings(
            child_key=KEY,
            spec_signing_key_version=KEY_VERSION,
            spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}),
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


def running_task(client, db, *, sign: bool = True,
                 repository_url: str = REPOSITORY_URL) -> tuple[Attempt, dict[str, Any]]:
    """A RUNNING implement step, its worker keyed, its spec signed."""
    attempt = Attempt()
    doc = ready_parent(client, db, attempt, runner_profile="claude-code",
                       repository_url=repository_url, workflow_id="wf_own",
                       step_id="impl-s1")
    if sign:
        SIGNER.sign_document(doc, doc["id"])
    return attempt, doc


def promote(db, repo_id: str = OWN_REPO_ID, tenant: str = "eng") -> None:
    """A registration whose promoted index is PROMOTED, with one older kept version."""
    db.docs[f"repositories/{repo_id}"] = {"tenant_id": tenant, "repo_id": repo_id,
                                          "index": {"current_sha": PROMOTED}}
    for sha, built in ((PROMOTED, "2026-10-19T00:00:00Z"), (OLDER, "2026-10-18T00:00:00Z")):
        db.docs[f"repositories/{repo_id}/index_versions/{sha}"] = {
            "commit_sha": sha, "graph_digest": "sha256:" + "d" * 64, "built_at": built}


def call(client, attempt: Attempt, *, token: str = WORKER_TOKEN, sign: bool = True,
         signer: Attempt | None = None, extra: dict[str, Any] | None = None,
         tamper: bool = False) -> Any:
    """The worker's call, shaped exactly as `agent_worker.graphroute.GraphContextRoute` sends it."""
    t = attempt.tuple
    body = json.dumps({
        "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "lease_id": t.lease_id, "generation": t.generation, **(extra or {}),
    }, sort_keys=True).encode("utf-8")
    path = f"/v1/attempts/{t.attempt_id}/context/graph"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sign:
        headers.update((signer or attempt).sign("POST", path, body, at=None))
    if tamper:
        body = body.replace(t.lease_id.encode(), b"lease_other")
    return client.post(path, content=body, headers=headers)


# --------------------------------------------------------------------------
# what is served
# --------------------------------------------------------------------------


def test_a_promoted_index_of_the_signed_repository_is_served(client, db):
    attempt, _ = running_task(client, db)
    promote(db)
    db.docs["tenant_settings/eng"] = {"code_intelligence": {"context": "on"}}
    db.docs["workspace_settings/eng"] = {"code_intelligence": {"graph_delivery": "staged"}}
    response = call(client, attempt)
    # MUTATION: no `/context/graph` route in swarm_api.routes.attemptcontext -> 404, not 200.
    assert response.status_code == 200, response.text
    answer = response.json()
    # MUTATION: derive repo_id from anything but the signed spec's URL -> another id.
    assert answer["repo_id"] == OWN_REPO_ID
    # MUTATION: serve the registration without its `index` -> no promoted commit to stage.
    assert answer["registration"]["index"]["current_sha"] == PROMOTED
    # MUTATION: drop the versions query from `AttemptContext.graph_context` -> [].
    assert sorted(v["id"] for v in answer["versions"]) == [OLDER, PROMOTED]
    assert {v["commit_sha"] for v in answer["versions"]} == {OLDER, PROMOTED}
    # MUTATION: leave the settings out -> the worker cannot decide whether to stage.
    assert answer["tenant_settings"] == {"code_intelligence": {"context": "on"}}
    assert answer["workspace_settings"] == {"code_intelligence": {"graph_delivery": "staged"}}


def test_a_repository_without_a_registration_is_served_as_none(client, db):
    attempt, _ = running_task(client, db)
    answer = call(client, attempt).json()
    # MUTATION: answer a missing registration as an error -> the worker cannot say "no index".
    assert answer["repo_id"] == OWN_REPO_ID
    assert answer["registration"] is None and answer["versions"] == []


# --------------------------------------------------------------------------
# tenant isolation (invariant 9) and what the caller may name (invariant 10)
# --------------------------------------------------------------------------


def test_another_tenants_registration_and_versions_are_never_served(client, db):
    attempt, _ = running_task(client, db)
    # The same repository, registered by another tenant, under the OWN id too.
    promote(db, THEIR_REPO_ID, tenant="research")
    promote(db, OWN_REPO_ID, tenant="research")
    db.docs["tenant_settings/research"] = {"code_intelligence": {"context": "off"}}
    response = call(client, attempt)
    assert response.status_code == 200, response.text
    answer = response.json()
    # MUTATION: drop the registration's tenant check -> research's versions are served.
    assert answer["registration"] is None and answer["versions"] == []
    assert answer["tenant_settings"] is None
    assert THEIR_REPO_ID not in response.text and "research" not in response.text


def test_the_caller_names_no_repository_tenant_or_commit(client, db):
    attempt, _ = running_task(client, db)
    promote(db)
    for extra in ({"repository_url": "https://github.com/someone/else"},
                  {"repo_id": THEIR_REPO_ID}, {"index_sha": OLDER}):
        response = call(client, attempt, extra=extra)
        # MUTATION: a body model without extra="forbid" -> 200 for a caller-named field.
        assert response.status_code == 422, (extra, response.text)


# --------------------------------------------------------------------------
# who may ask, and the fence (invariant 5)
# --------------------------------------------------------------------------


def test_an_unproven_caller_is_the_same_403_and_reads_nothing(client, db):
    attempt, _ = running_task(client, db)
    promote(db)
    answers = {
        "no ID token": call(client, attempt, token=""),
        "a person's ID token": call(client, attempt, token="token-alice"),
        "another tenant's worker": call(client, attempt, token=OTHER_WORKER_TOKEN),
        "no proof": call(client, attempt, sign=False),
        "a key this attempt never registered": call(client, attempt, signer=Attempt()),
        "a body the proof did not sign": call(client, attempt, tamper=True),
    }
    statuses = {cause: r.status_code for cause, r in answers.items()}
    # MUTATION: skip `AttemptContext._prove` in the graph handler -> 200 for these.
    assert set(statuses.values()) == {403}, statuses
    assert {r.json()["code"] for r in answers.values()} == {"child_submit_unproven"}
    assert all(PROMOTED not in r.text for r in answers.values())
    assert call(client, attempt).status_code == 200  # the control


def test_a_stale_generation_is_409_and_reads_nothing(client, db):
    attempt, doc = running_task(client, db)
    promote(db)
    doc["current_generation"] = 2
    doc["current_lease_id"] = "lease_parent_2"
    response = call(client, attempt)
    # MUTATION: skip `AttemptContext._fence` in the graph handler -> 200 to a stale attempt.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "child_submit_fenced"
    assert PROMOTED not in response.text


def test_an_unsigned_spec_is_refused_before_anything_is_read(client, db):
    attempt, _ = running_task(client, db, sign=False)
    promote(db)
    response = call(client, attempt)
    # MUTATION: skip `spec_refusal` -> the repository is read from an unsigned document.
    assert response.status_code == 422, response.text
    assert "spec_unverified" in response.json()["message"]
    assert PROMOTED not in response.text
