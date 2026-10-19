"""A platform admin's role routes reach a `ready` group the workspace job made
exactly as they reach a Terraform group (docs/design/workspace-roles.md §4.3;
owner decision 2026-10-19; lane ROLES-JOBGROUP-ROUTES, part of #1518).

`research` is NOT in `TENANT_GROUPS` here: only its `ready` registry record
registers it. What each case holds:

* `PUT .../research/roles/person` names research's first admin, with the same
  signed grant and the same `admin_audit` row (`via: platform_admin`) a
  Terraform group's write makes; its default can be set and an override
  cleared;
* a service workspace, a group whose record is not `ready`, a personal
  workspace and an unknown id are 404 on every one of the routes, and write
  nothing;
* a non-admin is 403 and writes nothing;
* the routes and `GET /v1/admin/workspaces/roles` read the authenticator's
  cached `registered_group_workspaces()`: building a `WorkspaceRegistry` in a
  request fails the request, and so the test.

`ops` is a platform admin in no tenant group; `alice` is a member of eng.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api import roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.registry import WorkspaceRegistry
from swarm_api.waker import NullWaker
from swarm_common.identity import tenant_id_for_user

from .conftest import (
    ADMIN_GROUP,
    ENG_GROUP,
    PROJECT,
    RESEARCH_GROUP,
    NoForgeTokens,
    api_settings,
)
from .test_roles_store import Clock, Recording

SERVICE = f"ci-bot@{PROJECT}.iam.gserviceaccount.com"
SERVICE_ID = tenant_id_for_user(SERVICE)
GROUPS = {
    "alice@saga.xyz": (ENG_GROUP,),
    "bob@saga.xyz": (RESEARCH_GROUP,),
    "ops@saga.xyz": (ADMIN_GROUP,),
}
ROUTES = (
    ("GET", "/v1/admin/workspaces/{t}/roles", None),
    ("PUT", "/v1/admin/workspaces/{t}/roles/person", {"email": "bob@saga.xyz", "role": "admin"}),
    ("DELETE", "/v1/admin/workspaces/{t}/roles/person", {"email": "bob@saga.xyz"}),
    ("PUT", "/v1/admin/workspaces/{t}/roles/default", {"role": "viewer"}),
)


def h(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-{user}"}


@pytest.fixture
def db() -> Recording:
    db = Recording()
    ready = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
    db.document("workspaces/research").set({
        "kind": "group", "principal": RESEARCH_GROUP, "state": "ready",
        "workspace_id": "w-0000c1", "ready_at": ready})
    # None of these registers a group workspace a platform admin may reach.
    db.document("workspaces/sales").set({
        "kind": "group", "principal": "sales@saga.xyz", "state": "applying",
        "workspace_id": "w-0000c2", "ready_at": None})
    db.document(f"workspaces/{SERVICE_ID}").set({
        "kind": "service", "principal": SERVICE, "state": "ready",
        "workspace_id": "w-0000c3", "ready_at": ready,
        "service": {"unique_id": "s-" + secrets.token_hex(4)}})
    db.document("workspaces/u-carol").set({
        "kind": "personal", "principal": "carol@saga.xyz", "state": "ready"})
    return db


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app_ctx(db, clock):
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "saga.xyz"}
              for e in GROUPS}
    return build_context(
        settings=api_settings(tenant_groups=(ENG_GROUP,), settings_key=secrets.token_hex(32)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(), now=clock,
    )


@pytest.fixture
def client(app_ctx):
    return TestClient(create_app(app_ctx), raise_server_exceptions=False)


def audit_rows(db, action: str) -> list[dict]:
    return [r for r in db.dump("admin_audit/").values() if r["action"] == action]


def grants(db) -> list[dict]:
    return list(db.dump(f"{roles.GRANTS_COLLECTION}/").values())


def call(client, method: str, path: str, body: dict | None, user: str = "ops"):
    return client.request(method, path, headers=h(user), json=body)


def test_the_accessor_is_terraform_groups_then_ready_job_made_groups(app_ctx):
    # MUTATION: answer `roles.registered_group_tenants(settings)` alone, and
    # research (job-made) is missing.
    assert app_ctx.authenticator.registered_group_workspaces() == {
        "eng": ENG_GROUP, "research": RESEARCH_GROUP}


def test_a_platform_admin_names_a_job_made_groups_first_admin(client, app_ctx, db):
    # MUTATION: `_registered` reading `TENANT_GROUPS` alone, and this is 404.
    response = call(client, "PUT", "/v1/admin/workspaces/research/roles/person",
                    {"email": "bob@saga.xyz", "role": "admin", "reason": "first admin"})
    assert response.status_code == 200, response.text
    assert response.json() | {"granted_at": None} == {
        "email": "bob@saga.xyz", "role": "admin", "previous_role": "member",
        "source": "override", "granted_at": None, "member": True}

    [row] = audit_rows(db, roles.AUDIT_GRANTED)
    assert row["target_tenant_id"] == "research"
    assert row["target_email"] == "bob@saga.xyz"
    assert row["by"] == "ops@saga.xyz"
    assert row["detail"]["via"] == roles.VIA_PLATFORM_ADMIN
    [doc] = grants(db)
    assert (doc["tenant_id"], doc["subject"], doc["role"], doc["via"]) == (
        "research", "bob@saga.xyz", "admin", roles.VIA_PLATFORM_ADMIN)
    # Signed exactly as a Terraform group's grant: the gates take it.
    assert app_ctx.roles.person_role("research", "bob@saga.xyz", fresh=True).role == "admin"

    read = call(client, "GET", "/v1/admin/workspaces/research/roles", None)
    assert read.status_code == 200, read.text
    assert {p["email"]: p["role"] for p in read.json()["people"]} == {"bob@saga.xyz": "admin"}

    # The same write again is the same refusal a Terraform group gives.
    again = call(client, "PUT", "/v1/admin/workspaces/research/roles/person",
                 {"email": "bob@saga.xyz", "role": "admin"})
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "role_unchanged"


def test_a_job_made_groups_default_role_can_be_set(client, app_ctx, db):
    response = call(client, "PUT", "/v1/admin/workspaces/research/roles/default",
                    {"role": "viewer"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["tenant_id"], body["default_role"], body["previous_default"]) == (
        "research", "viewer", "member")
    [row] = audit_rows(db, roles.AUDIT_DEFAULT_SET)
    assert row["target_tenant_id"] == "research"
    assert row["detail"]["via"] == roles.VIA_PLATFORM_ADMIN
    assert app_ctx.roles.default_role("research", fresh=True)[0] == "viewer"

    admin = call(client, "PUT", "/v1/admin/workspaces/research/roles/default", {"role": "admin"})
    assert admin.status_code == 409, admin.text
    assert admin.json()["code"] == "default_role_not_allowed"


def test_a_job_made_groups_override_can_be_cleared(client, app_ctx, db, clock):
    put = call(client, "PUT", "/v1/admin/workspaces/research/roles/person",
               {"email": "bob@saga.xyz", "role": "viewer"})
    assert put.status_code == 200, put.text
    clock.tick(seconds=1)
    cleared = call(client, "DELETE", "/v1/admin/workspaces/research/roles/person",
                   {"email": "bob@saga.xyz"})
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["role"], cleared.json()["previous_role"]) == ("member", "viewer")
    [row] = audit_rows(db, roles.AUDIT_CLEARED)
    assert row["target_tenant_id"] == "research"
    assert row["detail"]["via"] == roles.VIA_PLATFORM_ADMIN
    assert app_ctx.roles.person_role("research", "bob@saga.xyz", fresh=True).role == "member"


@pytest.mark.parametrize("tenant_id", [SERVICE_ID, "sales", "u-carol", "nope"])
@pytest.mark.parametrize("method,path,body", ROUTES)
def test_a_service_a_not_ready_group_and_a_personal_workspace_are_404(
        client, db, tenant_id, method, path, body):
    response = call(client, method, path.format(t=tenant_id), body)
    assert response.status_code == 404, response.text
    assert grants(db) == []
    assert not [r for r in db.dump("admin_audit/").values()
                if r["action"].startswith("workspace_")]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_a_non_admin_is_403_on_a_job_made_group_and_writes_nothing(
        client, db, method, path, body):
    response = call(client, method, path.format(t="research"), body, user="alice")
    assert response.status_code == 403, response.text
    assert grants(db) == []
    assert db.dump("admin_audit/") == {}


def test_a_platform_admin_reads_a_persons_roles_in_a_job_made_group(client):
    put = call(client, "PUT", "/v1/admin/workspaces/research/roles/person",
               {"email": "bob@saga.xyz", "role": "admin"})
    assert put.status_code == 200, put.text
    response = client.get("/v1/admin/roles/person", headers=h("ops"),
                          params={"email": "bob@saga.xyz"})
    assert response.status_code == 200, response.text
    assert [(w["tenant_id"], w["group"], w["role"]) for w in response.json()["workspaces"]] == [
        ("research", RESEARCH_GROUP, "admin")]


def test_no_route_builds_a_registry_per_request(client, app_ctx, monkeypatch):
    # MUTATION: `admin._role_workspaces` building
    # `WorkspaceRegistry.from_settings(...)` again, and the list is 500.
    def built(*args, **kwargs):
        raise AssertionError("a request built its own WorkspaceRegistry")

    monkeypatch.setattr(WorkspaceRegistry, "__init__", built)
    listed = client.get("/v1/admin/workspaces/roles", headers=h("ops"))
    assert listed.status_code == 200, listed.text
    assert [w["tenant_id"] for w in listed.json()["workspaces"]] == ["eng", "research"]
    for method, path, body in ROUTES:
        response = call(client, method, path.format(t="research"), body)
        assert response.status_code in (200, 409), (method, path, response.text)
    person = client.get("/v1/admin/roles/person", headers=h("ops"),
                        params={"email": "bob@saga.xyz"})
    assert person.status_code == 200, person.text


def test_the_routes_read_the_authenticators_cached_registry(client, db):
    # One request reads the registry; the next ones are served from its cache.
    assert client.get("/v1/admin/workspaces/roles", headers=h("ops")).status_code == 200
    db.asked.clear()
    assert client.get("/v1/admin/workspaces/roles", headers=h("ops")).status_code == 200
    put = call(client, "PUT", "/v1/admin/workspaces/research/roles/default", {"role": "viewer"})
    assert put.status_code == 200, put.text
    assert "workspaces" not in db.asked
