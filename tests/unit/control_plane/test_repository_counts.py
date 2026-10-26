"""`GET /v1/repositories/counts`: each registration's secret and QA-target counts
(REPOS-FOLLOWUPS, part of #2157).

Config › Repositories draws "Secrets: N" and "QA targets: N" on every
registered card (config-e.html S3a). Neither count was served in one read:
`GET /v1/repositories/{repo_id}/secrets` is one call per card, and BGD's
answer of 2026-10-25 was one call for the page's repositories, counts only.

What each case holds, and the attacker it stands in for:

  * THE COUNTS ARE THE REGISTRATION'S OWN: a workspace secret, another
    repository's secret, a destroyed secret and another workspace's secret
    bound to the same `repo_id` are not counted.
  * COUNTS ONLY: no name, kind, value, length or last characters leaves the
    route, though every fake value below sits beside its record.
  * ANOTHER WORKSPACE'S REPOSITORY: named in `repo_id`, it is the same 404
    as a missing one, and its counts are never in an unfiltered answer.
  * A VIEWER READS IT: it is a read, open to every member as the repository
    list is.

Offline: FakeFirestore, seeded with each store's documents directly. Every
fake value is built at runtime.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api import refusals, roles
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.qatargets import COLLECTION as QA_TARGETS
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker
from swarm_api.workspace_secrets import ACTIVE, SECRETS_COLLECTION, repository_hex, workspace_hex

from .conftest import ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, auth_header, \
    seed_tenant
from .fakes import FakeFirestore
from .test_roles_store import grant

URL = "/v1/repositories/counts"
AT = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
WIDGETS = repo_id_for("eng", "acme", "widgets")
GADGETS = repo_id_for("eng", "acme", "gadgets")
EMPTY = repo_id_for("eng", "acme", "empty")
THEIRS = repo_id_for("research", "acme", "widgets")


def fake_value() -> str:
    return "sk" + "-" + secrets.token_hex(20)


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """alice in eng, bob in research."""
    return {"alice@example.com": (ENG_GROUP,), "bob@example.com": (RESEARCH_GROUP,)}


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def client(db, tokens, group_map, objects) -> TestClient:
    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=objects, forge_tokens=NoForgeTokens(), now=lambda: AT,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def as_(who: str, tenant: str) -> dict[str, str]:
    return {**auth_header(who), TENANT_HEADER: tenant}


def register(db: FakeFirestore, tenant_id: str, repo: str) -> str:
    repo_id = repo_id_for(tenant_id, "acme", repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github", "owner": "acme",
        "repo": repo, "repository_url": f"https://github.com/acme/{repo}"}
    return repo_id


def seed_secret(db: FakeFirestore, tenant_id: str, name: str, *, repo_id: str | None = None,
                state: str = ACTIVE) -> str:
    """A store record as `WorkspaceSecrets` writes it, its value's last
    characters beside it where a careless writer could have left them."""
    tail = repository_hex() if repo_id else workspace_hex()
    secret_id = f"swarm-tenant-{tenant_id}-wsec-{tail}"
    db.docs[f"{SECRETS_COLLECTION}/{secret_id}"] = {
        "secret_id": secret_id, "tenant_id": tenant_id, "name": name, "provider": "other",
        "kind": "api_key", "scope": "repository" if repo_id else "workspace",
        "repo_id": repo_id, "state": state, "managed": "store",
        "current_version": f"projects/test/secrets/{secret_id}/versions/1",
        "last4": secrets.token_hex(2), "value": fake_value(),
        "created_at": AT, "created_by": "alice@example.com"}
    return secret_id


def seed_target(db: FakeFirestore, tenant_id: str, name: str, repo_id: str) -> None:
    target_id = "qat_" + secrets.token_hex(8)
    db.docs[f"{QA_TARGETS}/{target_id}"] = {
        "id": target_id, "tenant_id": tenant_id, "name": name,
        "base_url": f"https://{name}.example.com/", "host": f"{name}.example.com",
        "host_state": "approved", "repo_id": repo_id,
        "repository": "https://github.com/acme/widgets", "revision": 1}


@pytest.fixture
def seeded(db: FakeFirestore) -> None:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    register(db, "eng", "widgets")
    register(db, "eng", "gadgets")
    register(db, "eng", "empty")
    register(db, "research", "widgets")
    # widgets: two of its own; the workspace's, gadgets' and a destroyed one are not its.
    seed_secret(db, "eng", "deploy-key", repo_id=WIDGETS)
    seed_secret(db, "eng", "test-db-password", repo_id=WIDGETS)
    seed_secret(db, "eng", "old-key", repo_id=WIDGETS, state="destroyed")
    seed_secret(db, "eng", "workspace-wide")
    seed_secret(db, "eng", "gadgets-only", repo_id=GADGETS)
    # Another workspace's secret claiming eng's widgets: never counted for eng.
    seed_secret(db, "research", "theirs", repo_id=WIDGETS)
    seed_secret(db, "research", "theirs-own", repo_id=THEIRS)
    seed_target(db, "eng", "widgets-staging", WIDGETS)
    seed_target(db, "eng", "gadgets-staging", GADGETS)
    seed_target(db, "eng", "gadgets-prod", GADGETS)
    seed_target(db, "research", "their-staging", WIDGETS)


def by_id(body: dict) -> dict[str, dict]:
    return {row["repo_id"]: row for row in body["counts"]}


def test_every_registration_has_its_own_counts_in_one_read(client, seeded):
    r = client.get(URL, headers=as_("alice", "eng"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "eng"
    rows = by_id(body)
    # MUTATION: count every active secret of the workspace, or count across
    # workspaces, or keep the destroyed one, and widgets is not 2.
    assert rows[WIDGETS] == {"repo_id": WIDGETS, "repository": "acme/widgets",
                             "secrets": 2, "qa_targets": 1}
    assert rows[GADGETS] == {"repo_id": GADGETS, "repository": "acme/gadgets",
                             "secrets": 1, "qa_targets": 2}
    # MUTATION: leave a registration with nothing out and the card cannot say "none".
    assert rows[EMPTY] == {"repo_id": EMPTY, "repository": "acme/empty",
                           "secrets": 0, "qa_targets": 0}
    # MUTATION: list another workspace's registrations and THEIRS is here.
    assert set(rows) == {WIDGETS, GADGETS, EMPTY}


def test_counts_only_never_a_name_or_a_value(client, seeded, db):
    text = client.get(URL, headers=as_("alice", "eng")).text
    for doc in db.docs.values():
        for key in ("value", "last4"):
            if isinstance(doc.get(key), str):
                # MUTATION: serve the records instead of their counts and these appear.
                assert doc[key] not in text
    assert "deploy-key" not in text and "widgets-staging" not in text


def test_another_workspaces_repository_is_404_like_a_missing_one(client, seeded):
    theirs = client.get(URL, params={"repo_id": THEIRS}, headers=as_("alice", "eng"))
    missing = client.get(URL, params={"repo_id": repo_id_for("eng", "acme", "nothing")},
                         headers=as_("alice", "eng"))
    # MUTATION: read the registration without the caller's tenant and THEIRS answers 200.
    assert theirs.status_code == 404, theirs.text
    assert missing.status_code == 404
    assert theirs.json()["code"] == missing.json()["code"]
    # And from its own workspace it is served, with its own counts only.
    own = client.get(URL, params={"repo_id": THEIRS}, headers=as_("bob", "research"))
    assert own.status_code == 200, own.text
    assert by_id(own.json()) == {THEIRS: {"repo_id": THEIRS, "repository": "acme/widgets",
                                          "secrets": 1, "qa_targets": 0}}


def test_repo_id_narrows_the_answer_to_those_named(client, seeded):
    r = client.get(URL, params=[("repo_id", WIDGETS), ("repo_id", GADGETS)],
                   headers=as_("alice", "eng"))
    assert r.status_code == 200, r.text
    assert set(by_id(r.json())) == {WIDGETS, GADGETS}


def test_an_unknown_parameter_is_refused_rather_than_ignored(client, seeded):
    r = client.get(URL, params={"tenant_id": "research"}, headers=as_("alice", "eng"))
    # MUTATION: ignore it and a caller believes it read research's counts.
    assert r.status_code == 422, r.text


def test_a_viewer_reads_the_counts(db, tokens, group_map, objects, seeded, monkeypatch):
    monkeypatch.setenv(refusals.env_name("role_member_required"), "on")
    ctx = build_context(
        settings=api_settings(settings_key=secrets.token_hex(32),
                              tenant_groups=(ENG_GROUP, RESEARCH_GROUP)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens(), now=lambda: AT,
    )
    assert ctx.roles is not None
    grant(ctx.roles, "alice@example.com", roles.VIEWER)
    viewer = TestClient(create_app(ctx), raise_server_exceptions=False)
    # The control: with the switch on, a viewer's write is refused here.
    refused = viewer.delete(f"/v1/repositories/{WIDGETS}", headers=as_("alice", "eng"))
    assert refused.status_code == 403, refused.text
    r = viewer.get(URL, headers=as_("alice", "eng"))
    # MUTATION: gate the read on `member` and a viewer's cards lose both lines.
    assert r.status_code == 200, r.text
    assert by_id(json.loads(r.text))[WIDGETS]["secrets"] == 2
