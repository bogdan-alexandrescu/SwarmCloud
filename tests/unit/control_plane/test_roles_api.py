"""The workspace-role routes, the platform-admin audit and the role fields on
`/v1/tenants` (docs/design/workspace-roles.md §4.2.4, §4.3, §4.6, §4.9;
lane ROLES-API-CORE, part of #1518).

Who is who, in eng (`eng@saga.xyz`) unless said otherwise:

* `alice`, `bob` -- members, no grant (so `member`, the migration default);
* `root` -- a platform admin who is ALSO a member of eng;
* `ops` -- a platform admin who is in no tenant group: reaches eng only
  through `platform_admin_reach` (`tenant_via == "platform_admin"`);
* `carol` -- in no group: her tenant is her own personal workspace `u-carol`.

Every response any test here receives is kept, and after each test every grant
document id in the store is searched for in all of them: no response may ever
carry one (§4.4: the id is what a worker would need to overwrite a grant).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import replace
from typing import Any

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from swarm_api import roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context, current_auth
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings
from .test_roles_store import Clock, Recording

GROUPS = {
    "alice@saga.xyz": (ENG_GROUP,),
    "bob@saga.xyz": (ENG_GROUP,),
    "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
    "ops@saga.xyz": (ADMIN_GROUP,),
    "carol@saga.xyz": (),
    "dave@saga.xyz": (ENG_GROUP, RESEARCH_GROUP),
}
SERVICE_ACCOUNT = "robot@proj-123456" + ".iam.gserviceaccount.com"
ADMIN_SWITCH = "REFUSAL_ROLE_ADMIN_REQUIRED"
#: A string no route may ever write into an audit row: it rides in bodies.
SENTINEL = "never-in-the-audit-" + secrets.token_hex(4)

RESPONSES: list[str] = []


class Kept(TestClient):
    """A TestClient that keeps every response body it receives."""

    def request(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().request(*args, **kwargs)
        RESPONSES.append(response.text)
        return response


@pytest.fixture
def db() -> Recording:
    return Recording()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app_ctx(db, clock):
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "saga.xyz"}
              for e in GROUPS}
    return build_context(
        settings=api_settings(platform_admin_reach=True, settings_key=secrets.token_hex(32)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(), now=clock,
    )


@pytest.fixture
def probe_calls() -> list[list[dict]]:
    return []


@pytest.fixture
def client(app_ctx, db, probe_calls):
    app = create_app(app_ctx)

    def platform_rows() -> list[dict]:
        return [r for r in db.dump("admin_audit/").values()
                if r["action"].startswith("platform_admin_")]

    # A route of the ordinary kind, so "the body never ran" and "the row was
    # written before it ran" can be observed directly.
    @app.post("/v1/probe/{thing_id}")
    def probe_write(thing_id: str, body: dict, auth=Depends(current_auth)) -> dict:
        probe_calls.append(platform_rows())
        return {"ok": True}

    @app.get("/v1/probe/{thing_id}")
    def probe_read(thing_id: str, auth=Depends(current_auth)) -> dict:
        probe_calls.append(platform_rows())
        return {"ok": True}

    # The shape of the routes whose path names the workspace they change
    # (merge settings, code intelligence, the judgment backend).
    @app.put("/v1/workspaces/{tenant_id}/probe")
    def probe_workspace(tenant_id: str, body: dict, auth=Depends(current_auth)) -> dict:
        probe_calls.append(platform_rows())
        return {"ok": True}

    return Kept(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_response_carries_a_grant_id(db):
    RESPONSES.clear()
    yield
    ids = [path.rsplit("/", 1)[1] for path in db.paths(f"{roles.GRANTS_COLLECTION}/")]
    leaked = [i for i in ids for body in RESPONSES if i in body]
    assert not leaked, f"a response carried a grant document id: {leaked}"
    RESPONSES.clear()


def h(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if tenant:
        headers["X-Swarm-Tenant"] = tenant
    return headers


def roles_state(db) -> dict:
    """Every document but the platform-admin audit's rows: what a refused role
    write must leave as it was. (A platform admin's request writes its own
    `platform_admin_*` row before the route runs, by design.)"""
    return {k: v for k, v in db.dump().items()
            if not str(v.get("action", "")).startswith("platform_admin_")}


def audit_rows(db, action: str) -> list[dict]:
    return [r for r in db.dump("admin_audit/").values() if r["action"] == action]


def name_admin(client, who: str = "alice@saga.xyz", tenant: str = "eng") -> None:
    response = client.put(f"/v1/admin/workspaces/{tenant}/roles/person", headers=h("ops"),
                          json={"email": who, "role": "admin"})
    assert response.status_code == 200, response.text


def narrow(client, who: str, role: str = "viewer") -> None:
    response = client.put("/v1/admin/workspaces/eng/roles/person", headers=h("ops"),
                          json={"email": who, "role": role})
    assert response.status_code == 200, response.text


# -- who may write a role --------------------------------------------------------

def test_a_member_putting_a_person_is_403_and_writes_nothing(client, db, monkeypatch):
    # With the switch OFF: the role routes raise, they never `refuse`.
    monkeypatch.delenv(ADMIN_SWITCH, raising=False)
    before = roles_state(db)
    response = client.put("/v1/workspace/roles/person", headers=h("alice"),
                          json={"email": "bob@saga.xyz", "role": "viewer"})
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "role_admin_required"
    assert body["detail"] | {"admins": []} == {
        "tenant_id": "eng", "role": "member", "required": "admin", "source": "default",
        "admins": []}
    assert roles_state(db) == before


def test_a_workspace_admin_named_by_a_platform_admin_grants_and_revokes(client, db, clock):
    name_admin(client)
    named = audit_rows(db, "workspace_role_granted")
    assert len(named) == 1
    assert named[0]["detail"]["via"] == "platform_admin"
    assert named[0]["target_email"] == "alice@saga.xyz"

    clock.tick(seconds=1)
    put = client.put("/v1/workspace/roles/person", headers=h("alice"),
                     json={"email": "bob@saga.xyz", "role": "viewer", "reason": "audit"})
    assert put.status_code == 200, put.text
    assert put.json() | {"granted_at": None} == {
        "email": "bob@saga.xyz", "role": "viewer", "previous_role": "member",
        "source": "override", "granted_at": None, "member": True}

    listed = client.get("/v1/workspace/roles", headers=h("bob")).json()
    assert listed["me"]["role"] == "viewer" and listed["me"]["source"] == "override"
    assert listed["can_manage"] is False
    assert {p["email"]: p["role"] for p in listed["people"]} == {
        "alice@saga.xyz": "admin", "bob@saga.xyz": "viewer"}
    assert [e["action"] for e in listed["audit"]][:2] == ["workspace_role_granted"] * 2

    clock.tick(seconds=1)
    cleared = client.request("DELETE", "/v1/workspace/roles/person", headers=h("alice"),
                             json={"email": "bob@saga.xyz"})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["role"] == "member"
    assert len(audit_rows(db, "workspace_role_cleared")) == 1
    me = client.get("/v1/workspace/roles", headers=h("bob")).json()["me"]
    assert (me["role"], me["source"]) == ("member", "default")


def test_the_default_may_not_be_admin(client):
    response = client.put("/v1/workspace/roles/default", headers=h("root"),
                          json={"role": "admin"})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "default_role_not_allowed"


def test_a_default_of_viewer_applies_and_a_repeat_is_unchanged(client, db, clock):
    first = client.put("/v1/workspace/roles/default", headers=h("root"), json={"role": "viewer"})
    assert first.status_code == 200, first.text
    me = client.get("/v1/workspace/roles", headers=h("bob")).json()
    assert (me["default_role"], me["default_source"]) == ("viewer", "grant")
    assert me["me"]["role"] == "viewer"
    before = roles_state(db)
    clock.tick(seconds=1)
    again = client.put("/v1/workspace/roles/default", headers=h("root"), json={"role": "viewer"})
    assert again.status_code == 409 and again.json()["code"] == "role_unchanged"
    assert roles_state(db) == before


def test_a_personal_workspace_write_is_409(client, db):
    before = roles_state(db)
    response = client.put("/v1/workspace/roles/person", headers=h("carol"),
                          json={"email": "bob@saga.xyz", "role": "viewer"})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "personal_workspace_roles_fixed"
    assert roles_state(db) == before
    listed = client.get("/v1/workspace/roles", headers=h("carol")).json()
    assert listed["kind"] == "user" and listed["people"] == []
    assert (listed["me"]["role"], listed["me"]["source"]) == ("admin", "personal")


def test_a_personal_workspace_is_404_on_the_platform_admins_routes(client):
    for path in ("/v1/admin/workspaces/u-carol/roles", "/v1/admin/workspaces/nope/roles"):
        assert client.get(path, headers=h("ops")).status_code == 404


def test_a_service_account_is_403_role_person_required(client, db):
    before = roles_state(db)
    response = client.put("/v1/workspace/roles/person", headers=h("root"),
                          json={"email": SERVICE_ACCOUNT, "role": "viewer"})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_person_required"
    assert roles_state(db) == before


def test_an_unknown_role_is_422(client):
    response = client.put("/v1/workspace/roles/person", headers=h("root"),
                          json={"email": "bob@saga.xyz", "role": "owner"})
    assert response.status_code == 422 and response.json()["code"] == "unknown_role"


def test_a_write_and_its_audit_row_commit_together(client, db, monkeypatch):
    def fail(row):
        raise RuntimeError("injected")

    monkeypatch.setattr(roles.RoleStore, "audit_row", staticmethod(fail))
    before = roles_state(db)
    response = client.put("/v1/workspace/roles/person", headers=h("root"),
                          json={"email": "bob@saga.xyz", "role": "viewer"})
    assert response.status_code == 500
    assert roles_state(db) == before  # neither the grant nor its row


def test_no_signing_key_is_503_and_writes_nothing(client, app_ctx, db, monkeypatch):
    monkeypatch.setattr(app_ctx.roles, "_settings", replace(app_ctx.settings, settings_key=""))
    before = roles_state(db)
    response = client.put("/v1/workspace/roles/person", headers=h("root"),
                          json={"email": "bob@saga.xyz", "role": "viewer"})
    assert response.status_code == 503 and response.json()["code"] == "role_signing_unavailable"
    assert roles_state(db) == before


def test_a_reason_is_redacted_before_it_is_written(client, db):
    pasted = "ghp_" + secrets.token_hex(18)
    response = client.put("/v1/workspace/roles/person", headers=h("root"),
                          json={"email": "bob@saga.xyz", "role": "viewer",
                                "reason": f"pasted {pasted}"})
    assert response.status_code == 200, response.text
    assert pasted not in repr(db.dump())


def test_the_platform_admin_can_read_one_persons_roles_across_workspaces(client):
    narrow(client, "dave@saga.xyz")
    response = client.get("/v1/admin/roles/person", headers=h("ops"),
                          params={"email": "dave@saga.xyz"})
    assert response.status_code == 200, response.text
    (entry,) = response.json()["workspaces"]
    assert (entry["tenant_id"], entry["role"], entry["granted_via"]) == (
        "eng", "viewer", "platform_admin")
    assert entry["key"]
    assert client.get("/v1/admin/roles/person", headers=h("alice"),
                      params={"email": "dave@saga.xyz"}).status_code == 403


# -- the platform-admin audit (§4.2.4, Q12) ---------------------------------------

def test_a_non_member_platform_admins_post_is_audited_before_the_body(client, db, probe_calls):
    response = client.post("/v1/probe/p-1?q=" + SENTINEL, headers=h("ops", "eng"),
                           json={"prompt": SENTINEL})
    assert response.status_code == 200, response.text
    (row,) = audit_rows(db, "platform_admin_acted")
    # Written BEFORE the route body ran: the body saw it.
    assert probe_calls == [[row]]
    assert row["target_tenant_id"] == "eng" and row["by"] == "ops@saga.xyz"
    assert row["detail"] == {"method": "POST", "route": "/v1/probe/{thing_id}",
                             "path_params": {"thing_id": "p-1"}, "via": "platform_admin",
                             "reach": "non_member"}
    assert SENTINEL not in repr(db.dump("admin_audit/"))


def test_an_audit_failure_on_a_write_is_503_and_the_body_never_runs(client, db, probe_calls):
    db.broken.add("admin_audit")
    response = client.post("/v1/probe/p-1", headers=h("ops", "eng"), json={})
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "platform_admin_audit_unavailable"
    assert probe_calls == []


def test_reads_write_one_row_per_utc_hour(client, db, clock, probe_calls):
    for _ in range(2):
        assert client.get("/v1/probe/p-1", headers=h("ops", "eng")).status_code == 200
    (row,) = audit_rows(db, "platform_admin_viewed")
    assert row["detail"] == {"route": "/v1/probe/{thing_id}", "hour": "2026-10-20T09",
                             "via": "platform_admin", "reach": "non_member"}
    assert probe_calls[0] == [row]
    clock.tick(hours=1)
    assert client.get("/v1/tasks", headers=h("ops", "eng")).status_code == 200
    hours = sorted(r["detail"]["hour"] for r in audit_rows(db, "platform_admin_viewed"))
    assert hours == ["2026-10-20T09", "2026-10-20T10"]


def test_an_audit_failure_on_a_read_is_503(client, db, probe_calls):
    db.broken.add("admin_audit")
    response = client.get("/v1/probe/p-1", headers=h("ops", "eng"))
    assert response.status_code == 503
    assert response.json()["code"] == "platform_admin_audit_unavailable"
    assert probe_calls == []


def test_a_member_platform_admin_whose_role_is_enough_writes_none(client, db, probe_calls):
    assert client.post("/v1/probe/p-1", headers=h("root"), json={}).status_code == 200
    assert client.get("/v1/probe/p-1", headers=h("root")).status_code == 200
    submitted = client.post("/v1/tasks", headers=h("root"),
                            json={"runner_profile": "mock", "input": {"prompt": "hi"}})
    assert submitted.status_code in (200, 201, 202), submitted.text
    assert [r for r in db.dump("admin_audit/").values()
            if r["action"].startswith("platform_admin_")] == []


def test_a_member_platform_admin_narrowed_to_viewer_is_audited(client, db, probe_calls):
    narrow(client, "root@saga.xyz")
    response = client.post("/v1/probe/p-1", headers=h("root"), json={"x": SENTINEL})
    assert response.status_code == 200
    (row,) = audit_rows(db, "platform_admin_acted")
    assert probe_calls == [[row]]
    assert row["detail"]["reach"] == "role_below"
    assert row["detail"]["via"] == "platform_admin"
    # A real submission: one more row, before it ran.
    submitted = client.post("/v1/tasks", headers=h("root"),
                            json={"runner_profile": "mock", "input": {"prompt": SENTINEL}})
    assert submitted.status_code in (200, 201, 202), submitted.text
    assert [r["detail"]["route"] for r in audit_rows(db, "platform_admin_acted")] \
        .count("/v1/tasks") == 1
    # And reads write the hourly row.
    assert client.get("/v1/probe/p-1", headers=h("root")).status_code == 200
    assert client.get("/v1/tasks", headers=h("root")).status_code == 200
    (viewed,) = audit_rows(db, "platform_admin_viewed")
    assert viewed["detail"]["reach"] == "role_below"
    assert SENTINEL not in repr(db.dump("admin_audit/"))


def test_a_narrowed_member_platform_admins_audit_failure_is_503(client, db, probe_calls):
    narrow(client, "root@saga.xyz")
    tasks_before = db.paths("tasks/")
    db.broken.add("admin_audit")
    response = client.post("/v1/probe/p-1", headers=h("root"), json={})
    assert response.status_code == 503
    assert response.json()["code"] == "platform_admin_audit_unavailable"
    submitted = client.post("/v1/tasks", headers=h("root"),
                            json={"runner_profile": "mock", "input": {"prompt": "hi"}})
    assert submitted.status_code == 503
    assert probe_calls == [] and db.paths("tasks/") == tasks_before


def test_a_route_naming_its_workspace_is_audited_against_that_workspace(client, db):
    """root's selected tenant is eng, where its own role (member) is enough
    for a member write; research it is not in; u-carol is no group."""
    for tenant_id in ("eng", "research", "u-carol"):
        response = client.put(f"/v1/workspaces/{tenant_id}/probe", headers=h("root"), json={})
        assert response.status_code == 200, response.text
    rows = audit_rows(db, "platform_admin_acted")
    assert [(r["target_tenant_id"], r["detail"]["reach"]) for r in rows] == [
        ("research", "non_member")]
    assert rows[0]["detail"]["path_params"] == {"tenant_id": "research"}


def test_a_non_platform_admin_is_never_audited(client, db):
    narrow(client, "bob@saga.xyz")
    assert client.post("/v1/probe/p-1", headers=h("bob"), json={}).status_code == 200
    assert [r for r in db.dump("admin_audit/").values()
            if r["action"].startswith("platform_admin_")] == []


# -- /v1/tenants ------------------------------------------------------------------

def test_tenants_mine_carries_role_and_via(client):
    narrow(client, "dave@saga.xyz")
    dave = {e["tenant_id"]: e for e in client.get("/v1/tenants/mine", headers=h("dave")).json()}
    assert dave["eng"]["via"] == "member"
    assert dave["eng"]["role"] == {"name": "viewer", "source": "override"}
    assert dave["research"]["role"] == {"name": "member", "source": "default"}

    ops = client.get("/v1/tenants/mine", headers=h("ops")).json()
    reach = [e for e in ops if e["via"] == "platform_admin"]
    assert [e["tenant_id"] for e in reach] == ["eng", "research"]
    assert all(e["role"] == {"name": "admin", "source": "platform_admin"} for e in reach)
    assert all(e["is_default"] is False for e in reach)

    carol = client.get("/v1/tenants/mine", headers=h("carol")).json()
    assert carol == []
    me = client.get("/v1/tenants/me", headers=h("carol")).json()
    assert me["role"] == {"name": "admin", "source": "personal"}
    assert client.get("/v1/tenants/me", headers=h("dave", "eng")).json()["role"] == {
        "name": "viewer", "source": "override"}


def credential(client, user: str, tenant: str | None = None):
    return client.post("/v1/tenants/me/credentials", headers=h(user, tenant),
                       json={"provider": "anthropic", "api_key": "sk-" + secrets.token_hex(20)})


def test_credentials_by_a_member_are_report_only_with_the_switch_off(client, monkeypatch, caplog):
    monkeypatch.delenv(ADMIN_SWITCH, raising=False)
    with caplog.at_level(logging.WARNING):
        response = credential(client, "alice")
    assert response.status_code == 201, response.text
    assert "refusal report-only code=role_admin_required" in caplog.text


def test_credentials_by_a_member_are_403_with_the_switch_on(client, monkeypatch):
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    response = credential(client, "alice")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_admin_required"
    # A workspace admin, a platform admin and a personal workspace's person pass.
    name_admin(client)
    assert credential(client, "alice").status_code == 201
    # And the 403 names whom to ask.
    refused = credential(client, "bob")
    assert refused.status_code == 403
    assert refused.json()["detail"]["admins"] == ["alice@saga.xyz"]
    assert "alice@saga.xyz" in refused.json()["message"]
    assert credential(client, "root").status_code == 201
    assert credential(client, "carol").status_code == 201


def test_a_failed_role_read_is_503_with_the_switch_on_and_passes_with_it_off(
        client, db, monkeypatch):
    db.broken.add(roles.GRANTS_COLLECTION)
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    refused = credential(client, "alice")
    assert refused.status_code == 503, refused.text
    assert refused.json()["code"] == "role_unavailable"
    monkeypatch.setenv(ADMIN_SWITCH, "off")
    assert credential(client, "alice").status_code == 201


def test_a_failed_role_read_leaves_the_role_field_out(client, db):
    db.broken.add(roles.GRANTS_COLLECTION)
    entries = client.get("/v1/tenants/mine", headers=h("alice")).json()
    assert entries and "role" not in entries[0]
    assert "role" not in client.get("/v1/tenants/me", headers=h("alice")).json()


def test_a_viewer_reads_the_roster(client):
    narrow(client, "bob@saga.xyz")
    response = client.get("/v1/workspace/roles", headers=h("bob"))
    assert response.status_code == 200
    body = response.json()
    assert [r["name"] for r in body["roles"]] == ["viewer", "member", "admin"]
    assert body["default_source"] == "migration"
