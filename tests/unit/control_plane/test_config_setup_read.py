"""`GET /v1/onboarding` serves the Config strip's steps 3 and 4 (CONFIG-SETUP-READ,
part of #2157; docs/web-ui/mockups/config-e.html S1).

`config_setup.secrets` counts what is NEEDED AND NOT STORED -- an MCP server
whose credential is not stored, a QA entry signing in with its own login and
no credential, the workspace's TypeSafe slot while Jev reads it -- each item
naming the console address that sets it. `config_setup.people` is left to
do in a group workspace only while requests to join wait on the reader, an
admin who decides them; otherwise it does not apply there, since the group
supplies the people, nor in a personal one.

The attackers these stand in for:

  * A READER OF THE RESPONSE: every fixture value is built at runtime and
    stored beside its record wherever a careless writer could have left one;
    none, nor any version name, may appear in the serialized answer.
  * A MEMBER OF ANOTHER WORKSPACE: research's needs are never in eng's answer.
  * A FAILING STORE: an unread source is null with its reason, never 0 or done.

Offline: FakeFirestore, each store seeded directly; role grants written
through `PUT /v1/workspace/roles/person`, so they are signed as in production.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.joinrequests import JoinStore
from swarm_api.main import create_app
from swarm_api.mcp_servers import COLLECTION as MCP_SERVERS
from swarm_api.mcp_servers import McpServers, server_id
from swarm_api.metrics import ApiMetrics
from swarm_api.provider_keys import KEYS_COLLECTION
from swarm_api.qatargets import COLLECTION as QA_TARGETS
from swarm_api.roles import RoleStore, RoleUnavailable
from swarm_api.waker import NullWaker
from swarm_api.workspace_secrets import ACTIVE, GRANTS_COLLECTION, SECRETS_COLLECTION, workspace_hex

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, \
    auth_header, seed_tenant
from .fakes import FakeFirestore

URL = "/v1/onboarding"
AT = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
QA_WHERE = "config/repositories?page=qa-targets"
VALUES: list[str] = []


def fake_value() -> str:
    """A runtime value, remembered so the leak test can look for every one."""
    value = "pw" + "-" + secrets.token_hex(16)
    VALUES.append(value)
    return value


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """alice and dave in eng, bob in research, carol in none, root a platform admin in eng."""
    return {
        "alice@example.com": (ENG_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "carol@example.com": (),
        "dave@example.com": (ENG_GROUP,),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def db() -> FakeFirestore:
    VALUES.clear()
    return FakeFirestore()


@pytest.fixture
def settings() -> Any:
    return api_settings(settings_key=secrets.token_hex(32))


@pytest.fixture
def client(db, settings, tokens, group_map, objects) -> TestClient:
    ctx = build_context(
        settings=settings, db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens(), now=lambda: AT,
    )
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def as_(who: str, tenant: str | None = None) -> dict[str, str]:
    return {**auth_header(who), **({TENANT_HEADER: tenant} if tenant else {})}


def setup_of(client: TestClient, who: str = "alice", tenant: str | None = "eng") -> dict[str, Any]:
    response = client.get(URL, headers=as_(who, tenant))
    assert response.status_code == 200, response.text
    return response.json()["config_setup"]


def qa_target(db: FakeFirestore, tenant_id: str, name: str, *, kind: str = "own_login",
              credential_set: bool = False, environment: str = "staging") -> str:
    target_id = "qa_" + secrets.token_hex(8)
    credential: dict[str, Any] = {"set": False}
    if credential_set:
        credential = {"set": True, "set_at": AT, "set_by": "root@example.com",
                      "version": f"projects/test/secrets/swarm-tenant-{tenant_id}-qa/versions/7"}
    sign_in: dict[str, Any] = {"kind": kind}
    if kind == "own_login":
        sign_in["login_path"] = "/login"
    db.docs[f"{QA_TARGETS}/{target_id}"] = {
        "id": target_id, "tenant_id": tenant_id, "name": name, "repo_id": "r-x",
        "repository": "acme/widgets", "environments_migrated": True,
        "environments": [{"name": environment, "base_url": "https://staging.example.test",
                          "host": "staging.example.test", "sign_in": sign_in,
                          "credential": credential,
                          # A careless writer's leftovers: never served.
                          "password": fake_value()}],
    }
    return target_id


def mcp_server(db: FakeFirestore, tenant_id: str, name: str, *, mode: str = "workspace_secret",
               enabled: bool = True) -> None:
    db.docs[f"{MCP_SERVERS}/{server_id(tenant_id, name)}"] = {
        "server_id": server_id(tenant_id, name), "tenant_id": tenant_id, "name": name,
        "url": f"https://mcp.{name}.example/mcp", "transport": "streamable-http",
        "auth": {"mode": mode, "header": "Authorization", "scheme": "Bearer"},
        "enabled": enabled}


def store_secret(db: FakeFirestore, tenant_id: str, name: str, consumer: str) -> None:
    secret_id = f"swarm-tenant-{tenant_id}-wsec-{workspace_hex()}"
    db.docs[f"{SECRETS_COLLECTION}/{secret_id}"] = {
        "secret_id": secret_id, "tenant_id": tenant_id, "name": name, "provider": "other",
        "kind": "api_key", "scope": "workspace", "repo_id": None, "state": ACTIVE,
        "managed": "store", "current_version": f"projects/test/secrets/{secret_id}/versions/1",
        "created_at": AT, "created_by": "root@example.com", "use_in_workspaces": [],
        "value": fake_value()}
    db.docs[f"{GRANTS_COLLECTION}/{secret_id}~{consumer}"] = {
        "secret_id": secret_id, "tenant_id": tenant_id, "consumer": consumer,
        "slot": "env", "enabled": True, "changed_at": AT, "changed_by": "root@example.com"}


def typesafe_key(db: FakeFirestore, tenant_id: str) -> str:
    doc_id = f"{KEYS_COLLECTION}/swarm-tenant-{tenant_id}-typesafe"
    db.docs[doc_id] = {"set": True, "set_at": AT, "set_by": "root@example.com",
                       "version": f"projects/test/secrets/swarm-tenant-{tenant_id}-typesafe/versions/2",
                       "api_key": fake_value()}
    return doc_id


def grant(client: TestClient, email: str, role: str) -> None:
    response = client.put("/v1/workspace/roles/person", headers=as_("root", "eng"),
                          json={"email": email, "role": role})
    assert response.status_code == 200, response.text


# -- Secrets set --------------------------------------------------------------------


def test_nothing_stored_and_nothing_registered_reads_done(client):
    secrets_step = setup_of(client)["secrets"]
    assert secrets_step["needed"] == 0 and secrets_step["done"] is True
    assert secrets_step["items"] == []
    assert {k: v["needed"] for k, v in secrets_step["sources"].items()} == \
        {"mcp_servers": 0, "qa_targets": 0, "provider_key": 0}


def test_a_qa_entry_with_own_login_and_no_credential_is_needed_and_names_its_address(client, db):
    target_id = qa_target(db, "eng", "storefront")
    secrets_step = setup_of(client)["secrets"]
    assert secrets_step["needed"] == 1 and secrets_step["done"] is False
    (item,) = secrets_step["items"]
    assert item["kind"] == "qa_target" and item["name"] == "storefront"
    assert item["target_id"] == target_id and item["environment"] == "staging"
    assert item["where"] == QA_WHERE and "storefront" in item["needed_by"]


def test_setting_the_qa_credential_removes_it(client, db):
    target_id = qa_target(db, "eng", "storefront")
    assert setup_of(client)["secrets"]["needed"] == 1
    db.docs[f"{QA_TARGETS}/{target_id}"]["environments"][0]["credential"] = {
        "set": True, "set_at": AT, "set_by": "root@example.com",
        "version": "projects/test/secrets/swarm-tenant-eng-qa/versions/8"}
    secrets_step = setup_of(client)["secrets"]
    assert secrets_step["needed"] == 0 and secrets_step["items"] == []


def test_a_qa_entry_that_signs_in_with_nothing_needs_nothing(client, db):
    qa_target(db, "eng", "public-site", kind="none")
    assert setup_of(client)["secrets"]["needed"] == 0


def test_every_entry_of_a_target_is_counted(client, db):
    target_id = qa_target(db, "eng", "storefront")
    doc = db.docs[f"{QA_TARGETS}/{target_id}"]
    doc["environments"].append({**doc["environments"][0], "name": "production"})
    items = setup_of(client)["secrets"]["items"]
    assert sorted(i["environment"] for i in items) == ["production", "staging"]


def test_an_mcp_server_with_no_stored_credential_counts(client, db):
    mcp_server(db, "eng", "linear")
    secrets_step = setup_of(client)["secrets"]
    assert secrets_step["needed"] == 1 and secrets_step["done"] is False
    (item,) = secrets_step["items"]
    assert item == {"kind": "mcp_server", "name": "linear", "needed_by": "mcp:linear",
                    "where": "config/mcp-servers"}
    store_secret(db, "eng", "linear-key", "mcp:linear")
    assert setup_of(client)["secrets"]["needed"] == 0


def test_an_mcp_server_that_holds_no_workspace_credential_needs_nothing(client, db):
    mcp_server(db, "eng", "notion", mode="dispatch")
    mcp_server(db, "eng", "open", mode="none")
    mcp_server(db, "eng", "paused", enabled=False)
    assert setup_of(client)["secrets"]["needed"] == 0


def test_the_typesafe_slot_is_needed_only_while_jev_reads_it(client, db):
    key = typesafe_key(db, "eng")
    response = client.put("/v1/workspaces/eng/judgment-backend", headers=as_("root", "eng"),
                          json={"judgment_backend": "typesafe"})
    assert response.status_code == 200, response.text
    assert setup_of(client)["secrets"]["needed"] == 0
    del db.docs[key]
    secrets_step = setup_of(client)["secrets"]
    assert secrets_step["needed"] == 1
    assert secrets_step["items"] == [{"kind": "provider_key", "name": "typesafe",
                                      "needed_by": "jev", "where": "settings/keys"}]


def test_an_unset_typesafe_slot_on_an_internal_backend_is_not_needed(client):
    assert setup_of(client)["secrets"]["sources"]["provider_key"]["needed"] == 0


def test_an_unread_source_is_null_with_a_reason_and_never_done(client, db, monkeypatch):
    qa_target(db, "eng", "storefront")

    def broken(self, tenant_id):
        raise RuntimeError("store unavailable " + fake_value())

    monkeypatch.setattr(McpServers, "list", broken)
    secrets_step = setup_of(client)["secrets"]
    assert secrets_step["needed"] is None and secrets_step["done"] is None
    assert secrets_step["sources"]["mcp_servers"]["needed"] is None
    assert secrets_step["sources"]["mcp_servers"]["reason"]
    # The sources that were read still list what they found.
    assert secrets_step["sources"]["qa_targets"] == {"needed": 1, "reason": None}
    assert [i["kind"] for i in secrets_step["items"]] == ["qa_target"]


def test_another_workspaces_needs_are_not_in_this_answer(client, db):
    qa_target(db, "research", "lab-console")
    mcp_server(db, "research", "lab-tools")
    secrets_step = setup_of(client, "alice", "eng")["secrets"]
    assert secrets_step["needed"] == 0
    assert setup_of(client, "bob", "research")["secrets"]["needed"] == 2


# -- People invited -----------------------------------------------------------------


def test_a_personal_workspace_reports_people_invited_as_not_applying(client):
    people = setup_of(client, "carol", None)["people"]
    assert people["applies"] is False and people["done"] is None
    assert people["holders"] is None and people["reason"]


def ask_to_join(db: FakeFirestore, settings: Any, requester: str, *,
                group: str = ENG_GROUP) -> None:
    """An open request to join eng, signed with the app's own settings key."""
    JoinStore(db, settings, metrics=ApiMetrics(), now=lambda: AT).ask(
        tenant_id="eng", group=group, requester=requester)


def test_a_group_workspace_with_no_overrides_and_no_requests_has_nothing_to_invite(client):
    # The spine's Config dot that never cleared (2026-11-01): a group
    # workspace's people come from its group, so with no request waiting the
    # step does not apply -- it never reads "not done" -- and says why.
    grant(client, "alice@example.com", "admin")
    people = setup_of(client, "alice", "eng")["people"]
    assert people["applies"] is False and people["done"] is None
    assert people["reason"] and "group" in people["reason"]
    assert people["holders"] == 0 and people["admins"] == 0 and people["pending"] == 0


def test_a_member_who_decides_nothing_has_nothing_to_invite_either(client):
    people = setup_of(client, "dave", "eng")["people"]
    assert people["applies"] is False and people["done"] is None and people["reason"]


def test_someone_else_holding_a_role_is_still_counted(client):
    grant(client, "alice@example.com", "admin")
    grant(client, "dave@example.com", "member")
    people = setup_of(client, "alice", "eng")["people"]
    assert people["applies"] is False and people["holders"] == 1 and people["admins"] == 0
    seen_by_dave = setup_of(client, "dave", "eng")["people"]
    assert seen_by_dave["holders"] == 1 and seen_by_dave["admins"] == 1


def test_an_open_request_to_join_is_left_for_the_admin_who_decides_it(client, db, settings):
    grant(client, "alice@example.com", "admin")
    ask_to_join(db, settings, "lee@example.com")
    ask_to_join(db, settings, "kim@example.com")
    people = setup_of(client, "alice", "eng")["people"]
    assert people["applies"] is True and people["done"] is False
    assert people["pending"] == 2 and "2 requests to join" in people["reason"]
    # A platform admin decides it too; a member, who cannot, is not handed it.
    assert setup_of(client, "root", "eng")["people"]["pending"] == 2
    seen_by_dave = setup_of(client, "dave", "eng")["people"]
    assert seen_by_dave["applies"] is False and seen_by_dave["pending"] is None
    assert seen_by_dave["pending_reason"]


def test_a_request_naming_another_group_is_not_this_workspaces(client, db, settings):
    grant(client, "alice@example.com", "admin")
    ask_to_join(db, settings, "lee@example.com", group="old-eng@example.com")
    people = setup_of(client, "alice", "eng")["people"]
    assert people["applies"] is False and people["pending"] == 0


def test_unread_requests_to_join_are_unknown_never_done(client, monkeypatch):
    def broken(self, tenant_ids):
        raise RuntimeError("firestore down")

    grant(client, "alice@example.com", "admin")
    monkeypatch.setattr(JoinStore, "open_for", broken)
    people = setup_of(client, "alice", "eng")["people"]
    assert people["applies"] is True and people["done"] is None
    assert people["pending"] is None and people["reason"]


def test_pending_is_null_with_its_reason_where_a_workspace_takes_no_requests(client):
    people = setup_of(client, "alice", "eng")["people"]
    assert people["pending"] is None and people["pending_reason"]


def test_an_unread_roster_is_null_with_a_reason_never_0(client, monkeypatch):
    def broken(self, tenant_id):
        raise RoleUnavailable("roles could not be read")

    monkeypatch.setattr(RoleStore, "overrides", broken)
    people = setup_of(client, "alice", "eng")["people"]
    assert people["applies"] is True and people["done"] is None
    assert people["holders"] is None and people["admins"] is None and people["reason"]


# -- Names only ---------------------------------------------------------------------


def test_no_response_or_log_carries_a_secret_value_a_length_or_a_version(client, db, monkeypatch,
                                                                         caplog):
    qa_target(db, "eng", "storefront")
    qa_target(db, "eng", "billing", credential_set=True)
    mcp_server(db, "eng", "linear")
    mcp_server(db, "eng", "github-mcp")
    store_secret(db, "eng", "github-mcp-key", "mcp:github-mcp")
    typesafe_key(db, "eng")
    text = client.get(URL, headers=as_("alice", "eng")).text
    assert VALUES, "the fixtures stored no values to look for"
    for value in VALUES:
        assert value not in text
    setup = json.dumps(json.loads(text)["config_setup"])
    assert "versions/" not in setup and "github-mcp-key" not in setup
    assert '"length"' not in setup and '"version"' not in setup

    def broken(self, tenant_id):
        raise RuntimeError("store unavailable " + fake_value())

    monkeypatch.setattr(McpServers, "list", broken)
    caplog.set_level("INFO")
    text = client.get(URL, headers=as_("alice", "eng")).text
    assert json.loads(text)["config_setup"]["secrets"]["needed"] is None
    for value in VALUES:
        assert value not in text and value not in caplog.text
