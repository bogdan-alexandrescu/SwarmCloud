"""One read of every scope's secrets, never a value (SSC-OVERVIEW, part of #2158).

Design: docs/design/secrets-scopes.md §4 (what Config › Secrets lists, by
scope) and §6.2 (`GET /v1/secrets/overview`), with the owner's Q1 of its §11:
one `(slot, name)` offered by several scopes reaches an agent from the
person's, else the repository's, else the workspace's, and the overview says
which won.

The §9 row's tests, and the attackers they stand in for:

  * EVERY §4 STORE IS PRESENT IN ITS SCOPE: a store the page forgot is a
    secret nobody can find to rotate.
  * A READER OF THE PAGE: no row carries a value, a length or `last4`, though
    `git_tokens` stores `last4` and every fake value below sits beside its
    record.
  * ANOTHER MEMBER: personal rows are only the caller's, in every scope.
  * TypeSafe rows are read from `provider_keys`, not from the tenant's
    `credentials` (which the TypeSafe routes never write).
  * SHADOWING IS REPORTED: a workspace row shadowed by a repository's or a
    person's secret of the same name and slot names the winner.

Offline: FakeFirestore, seeded with each store's documents directly. Every
fake value is built at runtime.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.actions_secrets import WRITES_COLLECTION, write_id
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.browser_logins import COLLECTION as BROWSER_LOGINS
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.gittokens import COLLECTION as GIT_TOKENS
from swarm_api.gittokens import Scope, record_for_slot
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.mcp_servers import COLLECTION as MCP_SERVERS
from swarm_api.mcp_servers import server_id
from swarm_api.metrics import ApiMetrics
from swarm_api.provider_keys import KEYS_COLLECTION
from swarm_api.qatargets import COLLECTION as QA_TARGETS
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker
from swarm_api.workspace_secrets import (
    ACTIVE,
    GRANTS_COLLECTION,
    SECRETS_COLLECTION,
    repository_hex,
    workspace_hex,
)

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, \
    auth_header, seed_tenant
from .fakes import FakeFirestore

URL = "/v1/secrets/overview"
AT = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
WIDGETS = repo_id_for("eng", "acme", "widgets")
GADGETS = repo_id_for("eng", "acme", "gadgets")


def fake_value() -> str:
    return "sk" + "-" + secrets.token_hex(20)


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """alice and dave in eng, bob in research, root a platform admin in eng."""
    return {
        "alice@example.com": (ENG_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "dave@example.com": (ENG_GROUP,),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    }


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


def seed_personal(db: FakeFirestore, user: str) -> str:
    tenant_id = f"u-{user}"
    owner = f"{user}@example.com"
    seed_tenant(db, tenant_id)
    db.docs[f"tenants/{tenant_id}"].update(kind="user", principal=owner, display_name=owner)
    db.docs[f"workspaces/{tenant_id}"] = {
        "tenant_id": tenant_id, "workspace_id": f"w-{user[:6]:0<6}",
        "principal": owner, "state": "ready"}
    return tenant_id


def register(db: FakeFirestore, repo: str, tenant_id: str = "eng") -> str:
    repo_id = repo_id_for(tenant_id, "acme", repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github", "owner": "acme",
        "repo": repo, "repository_url": f"https://github.com/acme/{repo}"}
    return repo_id


def seed_secret(db: FakeFirestore, tenant_id: str, name: str, *, by: str,
                scope: str = "workspace", repo_id: str | None = None,
                grants: tuple[str, ...] = ("profile:claude-code",),
                use_in: tuple[str, ...] = ()) -> str:
    """A store record and its enabled grants, as `WorkspaceSecrets` writes them."""
    tail = repository_hex() if repo_id else workspace_hex()
    secret_id = f"swarm-tenant-{tenant_id}-wsec-{tail}"
    db.docs[f"{SECRETS_COLLECTION}/{secret_id}"] = {
        "secret_id": secret_id, "tenant_id": tenant_id, "name": name, "provider": "other",
        "kind": "api_key", "scope": scope, "repo_id": repo_id,
        "repository": "acme/widgets" if repo_id == WIDGETS else None,
        "state": ACTIVE, "managed": "store",
        "current_version": f"projects/test/secrets/{secret_id}/versions/1",
        "versions": [{"version": f"projects/test/secrets/{secret_id}/versions/1",
                      "added_at": AT, "added_by": by, "state": "enabled"}],
        "created_at": AT, "created_by": by, "use_in_workspaces": list(use_in)}
    for consumer in grants:
        grant = {"secret_id": secret_id, "tenant_id": tenant_id, "consumer": consumer,
                 "slot": "files", "enabled": True, "changed_at": AT, "changed_by": by}
        if repo_id:
            grant["repo_id"] = repo_id
        db.docs[f"{GRANTS_COLLECTION}/{secret_id}~{consumer}"] = grant
    return secret_id


@pytest.fixture
def seeded(db: FakeFirestore) -> dict[str, str]:
    """One document in every store §4 lists, each holding a runtime value
    beside it wherever its real writer could have left one by mistake."""
    leaks = {"last4": secrets.token_hex(2), "value": fake_value()}
    seed_tenant(db, "eng", credentials=("anthropic",))
    seed_tenant(db, "research")
    seed_personal(db, "alice")
    seed_personal(db, "bob")
    register(db, "widgets")
    register(db, "gadgets")
    # The workspace store, a repository's, alice's and bob's personal.
    seed_secret(db, "eng", "deploy-key", by="root@example.com")
    seed_secret(db, "eng", "test-db-password", by="root@example.com")
    seed_secret(db, "eng", "test-db-password", by="root@example.com", scope="repository",
                repo_id=WIDGETS)
    seed_secret(db, "eng", "gadgets-only", by="root@example.com", scope="repository",
                repo_id=GADGETS)
    seed_secret(db, "u-alice", "deploy-key", by="alice@example.com", scope="personal",
                use_in=("eng",))
    seed_secret(db, "u-alice", "alice-private", by="alice@example.com", scope="personal")
    seed_secret(db, "u-bob", "bob-private", by="bob@example.com", scope="personal")
    # An MCP server whose credential is a store secret granted to it.
    db.docs[f"{MCP_SERVERS}/{server_id('eng', 'linear')}"] = {
        "server_id": server_id("eng", "linear"), "tenant_id": "eng", "name": "linear",
        "url": "https://mcp.linear.example/mcp", "transport": "streamable-http",
        "auth": {"mode": "workspace_secret", "header": "Authorization", "scheme": "Bearer"},
        "enabled": True}
    db.docs[f"{MCP_SERVERS}/{server_id('eng', 'notion')}"] = {
        "server_id": server_id("eng", "notion"), "tenant_id": "eng", "name": "notion",
        "url": "https://mcp.notion.example/mcp", "transport": "streamable-http",
        "auth": {"mode": "dispatch", "header": "Authorization", "scheme": "Bearer"},
        "enabled": True}
    seed_secret(db, "eng", "linear-key", by="root@example.com", grants=("mcp:linear",))
    # TypeSafe: the tenant's key and alice's own, in `provider_keys`.
    for tenant_id, by in (("eng", "root@example.com"), ("u-alice", "alice@example.com")):
        db.docs[f"{KEYS_COLLECTION}/swarm-tenant-{tenant_id}-typesafe"] = {
            "set": True, "set_at": AT, "set_by": by,
            "version": f"projects/test/secrets/swarm-tenant-{tenant_id}-typesafe/versions/1"}
    # Saved browser logins: alice's and bob's.
    for tenant_id, name in (("u-alice", "staging-console"), ("u-bob", "bob-console")):
        db.docs[f"{BROWSER_LOGINS}/{tenant_id}~{name}"] = {
            "tenant_id": tenant_id, "name": name, "site": "staging.example.test",
            "state": "stored", "saved_at": AT, "expires_at": AT}
    # A QA target of widgets with its credential set, and one of gadgets.
    for target_id, repo_id in (("qa_" + "a" * 16, WIDGETS), ("qa_" + "b" * 16, GADGETS)):
        db.docs[f"{QA_TARGETS}/{target_id}"] = {
            "id": target_id, "tenant_id": "eng", "name": f"app-{repo_id[-4:]}",
            "base_url": "https://staging.example.test", "repository": "acme/widgets",
            "repo_id": repo_id, "host": "staging.example.test",
            "credential": {"set": True, "set_at": AT, "set_by": "root@example.com",
                           "version": "projects/test/secrets/swarm-tenant-eng-qa/versions/3"}}
    # GitHub tokens: the tenant's, widgets', alice's and dave's user slots.
    for scope, kwargs in ((Scope.TENANT, {}), (Scope.REPOSITORY, {"repo_id": WIDGETS}),
                          (Scope.USER, {"user": "alice@example.com"}),
                          (Scope.USER, {"user": "dave@example.com"})):
        record = record_for_slot("eng", scope, registered_by="root@example.com", now=AT,
                                 **kwargs)
        data = record.to_firestore()
        data.update(last4=leaks["last4"], forge_login="octo", state="active", verified_at=AT)
        db.docs[f"{GIT_TOKENS}/{record.token_id}"] = data
    # SwarmCloud's record of an Actions secret it wrote into widgets.
    db.docs[f"{WRITES_COLLECTION}/{write_id('eng', WIDGETS, 'repo-secrets', 'MERGE_KEY')}"] = {
        "repo_id": WIDGETS, "tenant_id": "eng", "repository": "acme/widgets",
        "environment": "repo-secrets", "name": "MERGE_KEY", "set_by": "root@example.com",
        "set_at": AT, "github_status": 201, "state": "set"}
    # Platform secrets and QA identity grants are Admin's, never Config's.
    db.docs["qa_identity_grants/eng~x"] = {"tenant_id": "eng", "identity": "qa-bot"}
    return leaks


def overview(client: TestClient, who: str, tenant: str, scope: str,
             repo_id: str | None = None) -> dict[str, Any]:
    params = {"scope": scope, **({"repo_id": repo_id} if repo_id else {})}
    response = client.get(URL, headers=as_(who, tenant), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def stores(body: dict[str, Any]) -> set[str]:
    return {row["store"] for row in body["rows"]}


def rows_of(body: dict[str, Any], store: str) -> list[dict[str, Any]]:
    return [row for row in body["rows"] if row["store"] == store]


# -- every §4 store is present in its scope ---------------------------------------


def test_every_section_4_store_is_present_in_its_scope(client, seeded):
    workspace = overview(client, "root", "eng", "workspace")
    assert stores(workspace) == {"workspace_secrets", "provider_keys", "runner_provider_keys",
                                 "mcp_servers", "pool_accounts", "git_tokens"}
    personal = overview(client, "alice", "eng", "personal")
    assert stores(personal) == {"workspace_secrets", "provider_keys", "browser_logins",
                                "git_tokens"}
    repository = overview(client, "root", "eng", "repository", WIDGETS)
    assert stores(repository) == {"workspace_secrets", "qa_targets", "git_tokens",
                                  "actions_secret_writes"}
    assert repository["repository"] == "acme/widgets"
    # What §4 keeps off Config: a dispatch-mode MCP credential, QA identity
    # grants and platform secrets.
    for body in (workspace, personal, repository):
        text = json.dumps(body)
        assert "notion" not in text and "qa-bot" not in text


def test_each_scope_lists_its_own_rows_and_no_other_scopes(client, seeded):
    workspace = overview(client, "root", "eng", "workspace")
    assert sorted(r["name"] for r in rows_of(workspace, "workspace_secrets")) == \
        ["deploy-key", "linear-key", "test-db-password"]
    assert {r["scope"] for r in rows_of(workspace, "workspace_secrets")} == {"workspace"}
    [runner] = rows_of(workspace, "runner_provider_keys")
    assert (runner["name"], runner["status"]) == ("anthropic", "set")
    [mcp] = rows_of(workspace, "mcp_servers")
    assert (mcp["name"], mcp["consumer"], mcp["secrets"], mcp["status"]) == \
        ("linear", "mcp:linear", ["linear-key"], "set")
    [git] = rows_of(workspace, "git_tokens")
    assert git["scope"] == "tenant" and git["status"] == "active" and git["forge_login"] == "octo"

    repository = overview(client, "root", "eng", "repository", WIDGETS)
    [secret] = rows_of(repository, "workspace_secrets")
    assert (secret["name"], secret["scope"], secret["repo_id"]) == \
        ("test-db-password", "repository", WIDGETS)
    [qa] = rows_of(repository, "qa_targets")
    assert qa["field"] == "credential" and qa["status"] == "set"
    assert qa["set_by"] == "root@example.com" and qa["target_id"] == "qa_" + "a" * 16
    [git] = rows_of(repository, "git_tokens")
    assert git["scope"] == "repository"
    [actions] = rows_of(repository, "actions_secret_writes")
    assert (actions["name"], actions["environment"], actions["set_by"]) == \
        ("MERGE_KEY", "repo-secrets", "root@example.com")


# -- never a value, a length or last4 ---------------------------------------------


FORBIDDEN_KEYS = {"value", "values", "length", "last4", "payload", "ciphertext",
                  "encrypted_value", "token", "fields"}


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_no_row_carries_a_value_a_length_or_last4(client, seeded):
    bodies = [overview(client, "root", "eng", "workspace"),
              overview(client, "alice", "eng", "personal"),
              overview(client, "alice", "u-alice", "workspace"),
              overview(client, "root", "eng", "repository", WIDGETS)]
    for body in bodies:
        assert not (_keys(body) & FORBIDDEN_KEYS), _keys(body) & FORBIDDEN_KEYS
        assert seeded["last4"] not in json.dumps(body)
        assert seeded["value"] not in json.dumps(body)


# -- personal rows are only the caller's -------------------------------------------


def test_personal_rows_are_only_the_callers(client, seeded):
    alice = overview(client, "alice", "eng", "personal")
    names = {r["name"] for r in alice["rows"]}
    assert {"deploy-key", "alice-private", "staging-console"} <= names
    assert not names & {"bob-private", "bob-console"}
    # alice's opt-in to eng is shown; her other secret is not opted in.
    by_name = {r["name"]: r for r in rows_of(alice, "workspace_secrets")}
    assert by_name["deploy-key"]["opted_in"] is True
    assert by_name["alice-private"]["opted_in"] is False
    # Her own git user slot, never dave's.
    [git] = rows_of(alice, "git_tokens")
    assert git["user"] == "alice@example.com"
    [typesafe] = rows_of(alice, "provider_keys")
    assert typesafe["tenant_id"] == "u-alice" and typesafe["set_by"] == "alice@example.com"
    assert all(r["can_manage"] is True for r in rows_of(alice, "workspace_secrets"))

    dave = overview(client, "dave", "eng", "personal")
    text = json.dumps(dave)
    assert "alice" not in text and "bob" not in text
    # A workspace listing never carries a person's rows either.
    workspace = overview(client, "dave", "eng", "workspace")
    assert not {r["name"] for r in workspace["rows"]} & {"alice-private", "staging-console"}
    assert all(r["scope"] != "personal" for r in rows_of(workspace, "workspace_secrets"))


# -- TypeSafe rows are read from provider_keys --------------------------------------


def test_typesafe_rows_are_read_from_provider_keys(client, seeded, db):
    [tenant_key] = rows_of(overview(client, "dave", "eng", "workspace"), "provider_keys")
    assert (tenant_key["name"], tenant_key["status"], tenant_key["set_by"]) == \
        ("typesafe", "set", "root@example.com")
    # The tenant's `credentials` naming typesafe is not a key: provider_keys says.
    db.docs["tenants/eng"]["credentials"] = ["anthropic", "typesafe"]
    del db.docs[f"{KEYS_COLLECTION}/swarm-tenant-eng-typesafe"]
    body = overview(client, "dave", "eng", "workspace")
    [tenant_key] = rows_of(body, "provider_keys")
    assert tenant_key["status"] == "unset" and tenant_key["set_by"] is None
    assert [r["name"] for r in rows_of(body, "runner_provider_keys")] == ["anthropic"]


# -- shadowing is reported, with its winner ----------------------------------------


def test_shadowing_is_reported_with_its_winner(client, seeded):
    workspace = overview(client, "dave", "eng", "workspace")
    by_name = {r["name"]: r for r in rows_of(workspace, "workspace_secrets")}
    # alice's opted-in personal secret wins on her own agents.
    assert by_name["deploy-key"]["shadowed_by"] == [
        {"scope": "personal", "person": "alice@example.com", "slot": "files",
         "where": "agents of alice@example.com"}]
    # widgets' secret wins on agents that clone widgets.
    assert by_name["test-db-password"]["shadowed_by"] == [
        {"scope": "repository", "repo_id": WIDGETS, "repository": "acme/widgets",
         "slot": "files", "where": "agents that clone acme/widgets"}]
    assert by_name["linear-key"]["shadowed_by"] == []

    repository = overview(client, "dave", "eng", "repository", WIDGETS)
    [secret] = rows_of(repository, "workspace_secrets")
    assert secret["shadowed_by"] == []
    assert secret["shadows"] == [{"scope": "workspace", "slot": "files",
                                  "where": "agents of this workspace"}]

    alice = overview(client, "alice", "eng", "personal")
    [mine] = [r for r in rows_of(alice, "workspace_secrets") if r["name"] == "deploy-key"]
    assert mine["shadowed_by"] == [] and mine["shadows"] == [
        {"scope": "workspace", "slot": "files", "where": "agents of this workspace"}]


def test_a_secret_with_no_enabled_grant_shadows_nothing(client, seeded, db):
    for path in [p for p, d in db.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")
                 and d.get("tenant_id") == "u-alice"]:
        db.docs[path]["enabled"] = False
    by_name = {r["name"]: r for r in rows_of(overview(client, "dave", "eng", "workspace"),
                                             "workspace_secrets")}
    assert by_name["deploy-key"]["shadowed_by"] == []


# -- who may read which scope -----------------------------------------------------


def test_can_manage_is_the_manager_rule_per_row(client, seeded):
    member = overview(client, "dave", "eng", "workspace")
    assert member["can_manage"] is False
    assert all(r["can_manage"] is False for r in member["rows"])
    admin = overview(client, "root", "eng", "workspace")
    assert admin["can_manage"] is True
    assert all(r["can_manage"] is True for r in rows_of(admin, "workspace_secrets"))
    # GitHub tokens and pool accounts are status only on this page.
    assert all(r["can_manage"] is False for r in admin["rows"]
               if r["store"] in ("git_tokens", "pool_accounts"))


def test_another_workspaces_repository_is_404_and_the_scope_is_checked(client, seeded, db):
    theirs = register(db, "widgets", "research")
    response = client.get(URL, headers=as_("bob", "research"),
                          params={"scope": "repository", "repo_id": WIDGETS})
    assert response.status_code == 404, response.text
    assert client.get(URL, headers=as_("bob", "research"),
                      params={"scope": "repository", "repo_id": theirs}).status_code == 200
    for params in ({"scope": "repository"}, {"scope": "workspace", "repo_id": WIDGETS},
                   {"scope": "tenant"}, {"scope": "workspace", "tenant_id": "research"}):
        refused = client.get(URL, headers=as_("root", "eng"), params=params)
        assert refused.status_code == 422, (params, refused.text)


def test_a_caller_without_a_personal_workspace_reads_an_empty_personal_scope(client, seeded):
    body = overview(client, "dave", "eng", "personal")
    assert body["personal_tenant_id"] is None
    # dave has no personal workspace: only his own GitHub user slot in eng.
    assert [(r["store"], r["user"]) for r in body["rows"]] == \
        [("git_tokens", "dave@example.com")]
