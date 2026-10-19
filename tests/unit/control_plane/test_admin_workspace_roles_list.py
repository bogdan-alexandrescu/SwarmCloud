"""GET /v1/admin/workspaces/roles: every registered group workspace's roles,
for Admin › People's Workspace roles card (docs/design/workspace-roles.md
§4.3, §4.8; lane ROLES-ADMIN-LIST, part of #1518).

What each case holds:

* a platform admin gets one entry per registered group workspace -- the
  Terraform groups (eng, research) and a `ready` group the workspace job made
  (design) -- with its default, each person's own role and who granted it
  when; never a personal record, never a group record that is not `ready`;
* the entries say what `GET /v1/admin/workspaces/{id}/roles` says for the
  same workspace, so the card and the per-workspace route cannot disagree;
* a non-admin is 403 and reads no grant;
* a page costs ONE grants query, whatever its size: never one per workspace;
* a forged grant is skipped exactly as the gates skip it, an id that collides
  with another principal's tenant is served `unavailable`, and a page token
  the list does not hold is 422.

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
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings
from .test_roles_store import Clock, Recording, worker_create

DESIGN_GROUP = "design@saga.xyz"
GROUPS = {
    "alice@saga.xyz": (ENG_GROUP,),
    "ops@saga.xyz": (ADMIN_GROUP,),
}
LIST = "/v1/admin/workspaces/roles"


def h(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-{user}"}


@pytest.fixture
def db() -> Recording:
    db = Recording()
    ready = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
    # A ready group the workspace job made: registered after the Terraform ones.
    db.document("workspaces/design").set({
        "kind": "group", "principal": DESIGN_GROUP, "state": "ready",
        "workspace_id": "w-0000d1", "ready_at": ready})
    # Neither of these registers a group workspace.
    db.document("workspaces/sales").set({
        "kind": "group", "principal": "sales@saga.xyz", "state": "applying",
        "workspace_id": "w-0000d2", "ready_at": None})
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
        settings=api_settings(tenant_groups=(ENG_GROUP, RESEARCH_GROUP),
                              settings_key=secrets.token_hex(32)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(), now=clock,
    )


@pytest.fixture
def client(app_ctx):
    return TestClient(create_app(app_ctx), raise_server_exceptions=False)


def grant(app_ctx, clock, tenant_id: str, subject: str, role: str | None,
          by: str = "ops@saga.xyz", via: str = "platform_admin") -> str:
    """A grant written the way the role routes write one."""
    clock.tick(seconds=1)
    store = app_ctx.roles
    return store.write(tenant_id=tenant_id, subject=subject, role=role, by=by, via=via,
                       reason="", previous=store.newest_valid(tenant_id, subject, fresh=True),
                       action=roles.AUDIT_GRANTED, detail={})


def by_id(body: dict) -> dict[str, dict]:
    return {w["tenant_id"]: w for w in body["workspaces"]}


def grant_reads(db: Recording) -> int:
    return db.asked.count(roles.GRANTS_COLLECTION)


def test_a_platform_admin_gets_every_group_workspace_including_a_job_made_one(
        client, app_ctx, clock):
    # MUTATION: list `roles.registered_group_tenants(settings)` alone, and
    # design (job-made) is missing.
    bob_at = grant(app_ctx, clock, "eng", "bob@saga.xyz", "admin")
    grant(app_ctx, clock, "eng", "carol@saga.xyz", "viewer", by="bob@saga.xyz", via="console")
    grant(app_ctx, clock, "eng", roles.DEFAULT_SUBJECT, "viewer")
    grant(app_ctx, clock, "design", "dave@saga.xyz", "admin")
    # Back to the default: an override no more, so not listed.
    grant(app_ctx, clock, "research", "erin@saga.xyz", "viewer")
    grant(app_ctx, clock, "research", "erin@saga.xyz", None)

    response = client.get(LIST, headers=h("ops"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert [w["tenant_id"] for w in body["workspaces"]] == ["eng", "research", "design"]
    assert body["next_page_token"] is None
    assert [r["name"] for r in body["roles"]] == list(roles.ROLES)
    ws = by_id(body)

    eng = ws["eng"]
    assert (eng["group"], eng["registered_by"], eng["kind"]) == (ENG_GROUP, "terraform", "group")
    assert (eng["default_role"], eng["default_source"]) == ("viewer", "grant")
    assert eng["people"] == [
        {"email": "carol@saga.xyz", "role": "viewer", "source": "override",
         "granted_by": "bob@saga.xyz", "granted_at": eng["people"][0]["granted_at"],
         "granted_via": "console", "reason": ""},
        {"email": "bob@saga.xyz", "role": "admin", "source": "override",
         "granted_by": "ops@saga.xyz", "granted_at": bob_at,
         "granted_via": "platform_admin", "reason": ""},
    ]

    research = ws["research"]
    assert (research["default_role"], research["default_source"]) == ("member", "migration")
    assert research["people"] == []

    design = ws["design"]
    assert (design["group"], design["registered_by"]) == (DESIGN_GROUP, "workspace_job")
    assert [(p["email"], p["role"]) for p in design["people"]] == [("dave@saga.xyz", "admin")]
    # Never a personal workspace, never a group that is not ready.
    assert not {"u-carol", "sales"} & set(ws)


def test_each_entry_says_what_the_per_workspace_route_says(client, app_ctx, clock):
    grant(app_ctx, clock, "eng", "bob@saga.xyz", "admin")
    grant(app_ctx, clock, "eng", "carol@saga.xyz", "viewer")
    grant(app_ctx, clock, "eng", "carol@saga.xyz", "admin")
    grant(app_ctx, clock, "eng", roles.DEFAULT_SUBJECT, "viewer")
    listed = by_id(client.get(LIST, headers=h("ops")).json())["eng"]
    alone = client.get("/v1/admin/workspaces/eng/roles", headers=h("ops")).json()
    for field in ("tenant_id", "kind", "default_role", "default_source", "people"):
        assert listed[field] == alone[field], field


def test_a_non_admin_is_403_and_reads_no_grant(client, db):
    db.asked.clear()
    response = client.get(LIST, headers=h("alice"))
    assert response.status_code == 403, response.text
    assert grant_reads(db) == 0


@pytest.mark.parametrize("page_size", [1, 2, 3])
def test_one_batched_grants_read_per_page_never_one_per_workspace(
        client, app_ctx, clock, db, page_size):
    # MUTATION: read each workspace with `RoleStore.overrides`, and a page of
    # three costs three grants reads.
    for tenant_id in ("eng", "research", "design"):
        grant(app_ctx, clock, tenant_id, "bob@saga.xyz", "admin")
    seen: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        params = {"page_size": page_size} | ({"page_token": token} if token else {})
        db.asked.clear()
        response = client.get(LIST, headers=h("ops"), params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert grant_reads(db) == 1
        assert 1 <= len(body["workspaces"]) <= page_size
        assert all(w["people"][0]["email"] == "bob@saga.xyz" for w in body["workspaces"])
        seen += [w["tenant_id"] for w in body["workspaces"]]
        pages += 1
        token = body["next_page_token"]
        if token is None:
            break
    assert seen == ["eng", "research", "design"]
    assert pages == -(-3 // page_size)


def test_a_page_is_at_most_what_one_in_query_takes(client):
    assert client.get(LIST, headers=h("ops"), params={"page_size": 31}).status_code == 422


def test_a_page_token_the_list_does_not_hold_is_422(client):
    response = client.get(LIST, headers=h("ops"), params={"page_token": "nope"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {"field": "page_token"}


def test_a_forged_grant_is_skipped_as_the_gates_skip_it(client, app_ctx, clock):
    grant(app_ctx, clock, "eng", "bob@saga.xyz", "viewer")
    # A tenant worker's newer, unsigned grant: never the answer.
    worker_create(app_ctx.db, {"tenant_id": "eng", "subject": "bob@saga.xyz", "role": "admin",
                               "granted_by": "bob@saga.xyz",
                               "granted_at": "2099-01-01T00:00:00.000000Z"})
    eng = by_id(client.get(LIST, headers=h("ops")).json())["eng"]
    assert [(p["email"], p["role"]) for p in eng["people"]] == [("bob@saga.xyz", "viewer")]
    gate = app_ctx.roles.person_role("eng", "bob@saga.xyz", fresh=True)
    assert gate.role == "viewer"


def test_an_id_held_by_another_principal_is_unavailable_and_the_rest_is_served(
        client, app_ctx, clock, db):
    grant(app_ctx, clock, "research", "bob@saga.xyz", "admin")
    db.document("tenants/eng").set({"tenant_id": "eng", "principal": "eng@partner.com",
                                    "kind": "group"})
    ws = by_id(client.get(LIST, headers=h("ops")).json())
    assert ws["eng"]["unavailable"] is True and ws["eng"]["code"] == "tenant_collision"
    assert "people" not in ws["eng"]
    assert [p["email"] for p in ws["research"]["people"]] == ["bob@saga.xyz"]
