"""A workspace's `admin` manages its secrets, MCP servers, tenant provider key,
git-token slots and settings (docs/design/workspace-roles.md §4.1, §4.8, §4.9;
owner's Q5, Q6 and Q13; lane ROLES-GATES-ADMIN, part of #1518).

Who is who, in eng (`eng@example.com`) unless said otherwise:

* `alice` -- a member of eng whom a platform admin names eng's `admin`. She
  is NOT a platform admin: everything she manages here she manages through
  the role alone;
* `bob` -- a member of eng with no grant: `member`, the migration default;
* `dave` -- a member of eng AND research, named `admin` of eng only;
* `ops` -- a platform admin in no tenant group: reaches eng only through
  `platform_admin_reach` (`tenant_via == "platform_admin"`);
* `carol` -- in no group: her tenant is her own personal workspace `u-carol`.

The attacker these tests stand in for is a member who is not an admin, and a
person whose admin role is in ANOTHER workspace: neither may write a secret,
a key, a server or a setting, and with the role switch off neither gets more
than they had before this lane -- a switch that is off must never widen what a
member can do to a secret.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like
a credential).
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import codeintel, refusals
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.gittokens import COLLECTION as GIT_TOKENS
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.mcp_servers import FLAG as MCP_FLAG
from swarm_api.mcp_servers import COLLECTION as SERVERS_COLLECTION
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_api.workspace_secrets import SECRETS_COLLECTION

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, seed_tenant
from .test_account_api import FakePool, account
from .test_child_tasks_api import KEY
from .test_roles_store import Clock
from .test_workspace_secrets import FakeSecretManager, RecordingFirestore, fake_value, seed_personal

GROUPS = {
    "alice@example.com": (ENG_GROUP,),
    "bob@example.com": (ENG_GROUP,),
    "dave@example.com": (ENG_GROUP, RESEARCH_GROUP),
    "ops@example.com": (ADMIN_GROUP,),
    "carol@example.com": (),
}
ADMIN_SWITCH = refusals.SWITCHES["role_admin_required"].env
MERGE_SWITCH = refusals.SWITCHES["merge_settings_owner_required"].env
CODEINTEL_SWITCH = refusals.SWITCHES[codeintel.OwnerRequired.code].env
SECRET_ROUTES = "/v1/workspace/secrets"
KEY_ROUTES = "/v1/provider-keys/typesafe"
SETTINGS = {
    "merge": ("/v1/workspaces/{}/merge-settings", {"mode": "train"}),
    "code_intelligence": ("/v1/workspaces/{}/code-intelligence", {"jev": True}),
    "judgment": ("/v1/workspaces/{}/judgment-backend", {"judgment_backend": "typesafe"}),
}


def fake_key(fill: str = "k") -> str:
    """Shaped like a provider key, built at runtime."""
    return "ts" + "-" + fill * 12 + secrets.token_hex(8)


@pytest.fixture
def db() -> RecordingFirestore:
    db = RecordingFirestore()
    seed_tenant(db, "eng", credentials=("git",))
    seed_tenant(db, "research")
    seed_personal(db, "carol")
    return db


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sm() -> FakeSecretManager:
    return FakeSecretManager()


@pytest.fixture
def credentials() -> InMemoryCredentials:
    return InMemoryCredentials()


@pytest.fixture
def client(db, clock, sm, credentials, monkeypatch) -> TestClient:
    monkeypatch.setenv(MCP_FLAG, "on")
    monkeypatch.delenv(ADMIN_SWITCH, raising=False)
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    ctx = build_context(
        settings=api_settings(platform_admin_reach=True, settings_key=secrets.token_hex(32),
                              child_key=KEY, console_url="https://console.example.test"),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=credentials, waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(), now=clock,
    )
    app = create_app(ctx)
    app.state.workspace_secret_writer = sm
    app.state.account_pool = FakePool(account("eng", "team-max"))
    return TestClient(app, raise_server_exceptions=False)


def h(user: str, tenant: str | None = "eng") -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if tenant:
        headers[TENANT_HEADER] = tenant
    return headers


def name_role(client, who: str, role: str = "admin", tenant: str = "eng") -> None:
    response = client.put(f"/v1/admin/workspaces/{tenant}/roles/person", headers=h("ops", None),
                          json={"email": who, "role": role})
    assert response.status_code == 200, response.text


def server(name: str = "kb") -> dict[str, Any]:
    return {"name": name, "url": "https://kb.example.com/mcp", "description": "decisions",
            "auth": {"mode": "dispatch"}}


def admin_writes(client, who: str, tenant: str = "eng") -> dict[str, Any]:
    """One write of each kind a workspace admin makes (Q6), as `who`."""
    return {
        "secret": client.post(SECRET_ROUTES, headers=h(who, tenant),
                              json={"name": f"key-{who}", "provider": "typesafe",
                                    "kind": "api_key", "value": fake_value("v")}),
        "mcp": client.post("/v1/mcp-servers", headers=h(who, tenant), json=server(f"kb-{who}")),
        "tenant_key": client.put(f"{KEY_ROUTES}/tenant", headers=h(who, tenant),
                                 json={"api_key": fake_key()}),
        "git_token": client.post("/v1/git-tokens", headers=h(who, tenant),
                                 json={"scope": "repository", "repository": f"example-org/{who}"}),
    }


def stored(db, sm, credentials) -> dict[str, Any]:
    """Everything an admin write would add: secret records, Secret Manager
    calls, MCP servers, tenant keys, git-token slots, and settings."""
    return {
        "secrets": sorted(p for p in db.docs if p.startswith(f"{SECRETS_COLLECTION}/")),
        "sm": list(sm.calls),
        "servers": sorted(p for p in db.docs if p.startswith(f"{SERVERS_COLLECTION}/")),
        "keys": dict(credentials._versions),
        "git": sorted(p for p, d in db.docs.items() if p.startswith(f"{GIT_TOKENS}/")
                      and d.get("scope") == "repository"),
        "settings": {p: d for p, d in db.docs.items() if p.startswith("workspace_settings/")},
    }


def platform_rows(db, action: str = "platform_admin_acted") -> list[dict]:
    return [d for p, d in db.docs.items()
            if p.startswith(f"{AUDIT_COLLECTION}/") and d.get("action") == action]


def put_setting(client, kind: str, who: str, tenant: str = "eng", header: str | None = "eng"):
    route, body = SETTINGS[kind]
    return client.put(route.format(tenant), headers=h(who, header), json=body)


# -- Q6: secrets, MCP servers, the tenant provider key, git-token slots -----------------------

def test_a_group_workspace_admin_who_is_not_a_platform_admin_manages_them(client, db, caplog):
    name_role(client, "alice@example.com")
    caplog.set_level(logging.WARNING)
    written = admin_writes(client, "alice")
    assert {k: r.status_code for k, r in written.items()} == {
        "secret": 201, "mcp": 201, "tenant_key": 200, "git_token": 201}, \
        {k: r.text for k, r in written.items()}
    assert client.get(SECRET_ROUTES, headers=h("alice")).json()["can_manage"] is True
    assert client.get("/v1/mcp-servers", headers=h("alice")).json()["can_manage"] is True
    # The role alone let her through: no platform-admin row, no would-be refusal.
    assert platform_rows(db) == []
    assert "code=role_admin_required" not in caplog.text
    # And she rotates and revokes what she made.
    token_id = written["git_token"].json()["token"]["token_id"]
    assert client.delete(f"/v1/git-tokens/{token_id}", headers=h("alice")).status_code == 200
    rotated = client.put(f"{SECRET_ROUTES}/key-alice/value", headers=h("alice"),
                         json={"value": fake_value("w")})
    assert rotated.status_code == 200, rotated.text
    assert client.delete(f"{KEY_ROUTES}/tenant", headers=h("alice")).status_code == 200


def test_a_non_member_platform_admin_manages_them_with_a_platform_admin_acted_row(client, db):
    written = admin_writes(client, "ops")
    assert {k: r.status_code for k, r in written.items()} == {
        "secret": 201, "mcp": 201, "tenant_key": 200, "git_token": 201}, \
        {k: r.text for k, r in written.items()}
    routes = sorted(r["detail"]["route"] for r in platform_rows(db)
                    if r["target_tenant_id"] == "eng" and r["by"] == "ops@example.com")
    assert routes == sorted(["/v1/workspace/secrets", "/v1/mcp-servers",
                             "/v1/provider-keys/{provider}/tenant", "/v1/git-tokens"])
    assert client.get(SECRET_ROUTES, headers=h("ops")).json()["can_manage"] is True


def test_a_member_is_refused_role_admin_required_with_the_switch_on(
        client, db, sm, credentials, monkeypatch):
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    before = stored(db, sm, credentials)
    written = admin_writes(client, "bob")
    assert {k: (r.status_code, r.json()["code"]) for k, r in written.items()} == {
        k: (403, "role_admin_required") for k in written}, {k: r.text for k, r in written.items()}
    detail = written["secret"].json()["detail"]
    assert detail | {"admins": None} == {"tenant_id": "eng", "role": "member", "required": "admin",
                                         "source": "default", "admins": None}
    assert stored(db, sm, credentials) == before
    assert client.get(SECRET_ROUTES, headers=h("bob")).json()["can_manage"] is False
    assert client.get("/v1/mcp-servers", headers=h("bob")).json()["can_manage"] is False


def test_with_the_switch_off_a_member_is_logged_and_still_holds_no_more_than_before(
        client, db, sm, credentials, caplog):
    # Off is report-only for the NEW code. It never widens: a group member
    # was refused these writes before this lane, and still is.
    caplog.set_level(logging.WARNING)
    before = stored(db, sm, credentials)
    written = admin_writes(client, "bob")
    assert {k: r.status_code for k, r in written.items()} == {k: 403 for k in written}
    assert all(r.json()["code"] != "role_admin_required" for r in written.values())
    assert stored(db, sm, credentials) == before
    reported = [r.getMessage() for r in caplog.records
                if "refusal report-only code=role_admin_required" in r.getMessage()]
    assert len(reported) >= len(written), caplog.text
    assert all("bob@example.com" in m or "eng" in m for m in reported)


def test_an_admin_narrowed_a_moment_ago_is_refused_at_once(client, db, sm, credentials,
                                                           monkeypatch):
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    name_role(client, "alice@example.com")
    # Primes the role cache with `admin`.
    assert client.get(SECRET_ROUTES, headers=h("alice")).json()["can_manage"] is True
    name_role(client, "alice@example.com", "member")
    before = stored(db, sm, credentials)
    written = admin_writes(client, "alice")
    assert {k: r.status_code for k, r in written.items()} == {k: 403 for k in written}
    assert stored(db, sm, credentials) == before


def test_an_admin_grant_to_a_non_member_admits_nobody(client, db, sm, credentials):
    # Q3: Google owns membership. carol's grant is stored and inert.
    name_role(client, "carol@example.com")
    before = stored(db, sm, credentials)
    written = admin_writes(client, "carol")
    assert {k: (r.status_code, r.json()["code"]) for k, r in written.items()} == {
        k: (403, "tenant_not_member") for k in written}
    for kind in SETTINGS:
        assert put_setting(client, kind, "carol", header=None).status_code in (403, 404), kind
    assert stored(db, sm, credentials) == before


def test_a_personal_workspaces_person_manages_theirs_and_nobody_else_selects_it(client, db):
    response = client.post(SECRET_ROUTES, headers=h("carol", "u-carol"),
                           json={"name": "mine", "provider": "typesafe", "kind": "api_key",
                                 "value": fake_value("c")})
    assert response.status_code == 201, response.text
    registered = client.post("/v1/mcp-servers", headers=h("carol", "u-carol"), json=server())
    assert registered.status_code == 201, registered.text
    assert client.get(SECRET_ROUTES, headers=h("carol", "u-carol")).json()["can_manage"] is True
    # alice is eng's admin; that reaches nobody's personal workspace.
    name_role(client, "alice@example.com")
    for who in ("bob", "alice", "ops"):
        refused = client.get(SECRET_ROUTES, headers=h(who, "u-carol"))
        assert refused.status_code == 403, (who, refused.text)
        assert refused.json()["code"] == "tenant_not_member"


# -- Q13: merge settings, code intelligence, the judgment backend ------------------------------

def test_a_group_workspace_admin_changes_its_settings_and_each_write_is_stored(
        client, db, caplog, monkeypatch):
    monkeypatch.setenv(MERGE_SWITCH, "on")
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    name_role(client, "alice@example.com")
    caplog.set_level(logging.WARNING)
    for kind in SETTINGS:
        response = put_setting(client, kind, "alice")
        assert response.status_code == 200, (kind, response.text)
        assert response.json()["can_edit"] is True, kind
    doc = db.docs["workspace_settings/eng"]
    assert doc["merge"]["mode"] == "train"
    assert doc["code_intelligence"]["jev"] is True
    assert doc["judgment_backend"] == "typesafe"
    assert platform_rows(db) == []
    assert "report-only" not in caplog.text
    for kind, (route, _body) in SETTINGS.items():
        assert client.get(route.format("eng"), headers=h("alice")).json()["can_edit"] is True, kind


@pytest.mark.parametrize("kind,code", [
    ("merge", "merge_settings_owner_required"),
    ("code_intelligence", "role_admin_required"),
    ("judgment", "role_admin_required"),
])
def test_a_member_is_refused_each_setting_and_nothing_is_written(client, db, monkeypatch,
                                                                 kind, code):
    monkeypatch.setenv(MERGE_SWITCH, "on")
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    audit_before = sorted(p for p in db.docs if p.startswith(f"{AUDIT_COLLECTION}/"))
    response = put_setting(client, kind, "bob")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == code
    assert "workspace_settings/eng" not in db.docs
    assert sorted(p for p in db.docs if p.startswith(f"{AUDIT_COLLECTION}/")) == audit_before
    route, _ = SETTINGS[kind]
    assert client.get(route.format("eng"), headers=h("bob")).json()["can_edit"] is False


def test_with_the_role_switch_off_a_member_holds_no_more_than_before(client, db, caplog,
                                                                     monkeypatch):
    # Before this lane code intelligence refused a member (its own switch,
    # on) and the judgment backend refused them outright. Off, the role code
    # is reported and the old refusal still stands.
    monkeypatch.delenv(CODEINTEL_SWITCH, raising=False)
    caplog.set_level(logging.WARNING)
    intel = put_setting(client, "code_intelligence", "bob")
    assert (intel.status_code, intel.json()["code"]) == (403, codeintel.OwnerRequired.code)
    judged = put_setting(client, "judgment", "bob")
    assert judged.status_code == 403, judged.text
    assert "workspace_settings/eng" not in db.docs
    reported = [r.getMessage() for r in caplog.records
                if "refusal report-only code=role_admin_required" in r.getMessage()]
    assert len(reported) >= 2, caplog.text


def test_an_admin_of_one_workspace_is_a_member_of_another(client, db, monkeypatch):
    monkeypatch.setenv(MERGE_SWITCH, "on")
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    name_role(client, "dave@example.com")
    for kind in SETTINGS:
        # dave selects eng, where he is admin, and names research in the path.
        response = put_setting(client, kind, "dave", tenant="research", header="eng")
        assert response.status_code in (403, 404), (kind, response.text)
        response = put_setting(client, kind, "dave", tenant="research", header="research")
        assert response.status_code == 403, (kind, response.text)
    assert "workspace_settings/research" not in db.docs
    written = admin_writes(client, "dave", "research")
    assert {k: r.status_code for k, r in written.items()} == {k: 403 for k in written}
    # CONTROL: in eng he is its admin.
    assert put_setting(client, "merge", "dave").status_code == 200


def test_a_non_member_platform_admin_changes_them_with_a_platform_admin_acted_row(client, db):
    for kind in SETTINGS:
        response = put_setting(client, kind, "ops")
        assert response.status_code == 200, (kind, response.text)
    routes = sorted(r["detail"]["route"] for r in platform_rows(db)
                    if r["target_tenant_id"] == "eng")
    assert routes == sorted(route.replace("{}", "{tenant_id}")
                            for route, _ in SETTINGS.values())
    doc = db.docs["workspace_settings/eng"]
    assert doc["merge"]["mode"] == "train" and doc["judgment_backend"] == "typesafe"


def test_a_personal_workspaces_person_still_changes_theirs(client, db, monkeypatch):
    monkeypatch.setenv(MERGE_SWITCH, "on")
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    for kind in SETTINGS:
        response = put_setting(client, kind, "carol", tenant="u-carol", header="u-carol")
        assert response.status_code == 200, (kind, response.text)
    assert db.docs["workspace_settings/u-carol"]["judgment_backend"] == "typesafe"
    for kind in SETTINGS:
        response = put_setting(client, kind, "bob", tenant="u-carol")
        assert response.status_code in (403, 404), (kind, response.text)
