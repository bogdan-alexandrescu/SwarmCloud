"""Admin › People and roles, scoped by who reads it (ADMIN-PEOPLE-ROLES; part
of #2157; docs/web-ui/mockups/config-e.html frames S5c and S5f).

`GET /v1/admin/people` was a platform admin's alone. It is now also a
WORKSPACE ADMIN's, narrowed to the group workspaces whose verified membership
and signed grant make them its admin, and to the people in those workspaces;
and `PUT|DELETE /v1/admin/people/workspaces/{tenant_id}/role` lets either act
on a person's role -- the workspace admin only in a workspace they administer.

Read with an attacker's eye, what is held:

* a workspace admin of eng reads eng's column and eng's people, and nothing
  that names research, a person only in research, a personal workspace's
  request, a Claude account, a loan, the platform's admins or its audit;
* a role write in a workspace they do not administer is 403 and writes
  nothing, by the path, and a header naming another workspace changes nothing;
* the personal-workspace and loan routes stay a platform admin's;
* a demotion takes effect on the next request (the scope is read fresh);
* a platform admin still reads everyone and every workspace;
* a member or viewer is refused as before.

Who is who (groups as in test_roles_api.py): `alice`, `bob` in eng; `dave` in
eng and research; `erin` in research only; `root` a platform admin also in
eng; `ops` a platform admin in no group; `carol` in none.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import roles
from swarm_api import workspaces as ws
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings
from .test_people_admin import FakePool, FakePublisher
from .test_roles_store import Clock, Recording

GROUPS = {
    "alice@example.com": (ENG_GROUP,),
    "bob@example.com": (ENG_GROUP,),
    "dave@example.com": (ENG_GROUP, RESEARCH_GROUP),
    "erin@example.com": (RESEARCH_GROUP,),
    "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    "ops@example.com": (ADMIN_GROUP,),
    "carol@example.com": (),
}
#: Each person's `people/` document: `teams` is what `touch_person` records.
TEAMS = {
    "alice@example.com": ["eng"],
    "bob@example.com": ["eng"],
    "dave@example.com": ["eng", "research"],
    "erin@example.com": ["research"],
    "root@example.com": ["eng"],
    "carol@example.com": [],
}
ROLE_ROUTE = "/v1/admin/people/workspaces/{tenant}/role"


@pytest.fixture
def db() -> Recording:
    return Recording()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(db, clock) -> TestClient:
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    ctx = build_context(
        settings=api_settings(platform_admin_reach=True, settings_key=secrets.token_hex(32)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(), now=clock,
    )
    app = create_app(ctx)
    app.state.workspace_publisher = FakePublisher()
    app.state.account_pool = FakePool()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _people(db) -> None:
    now = datetime.now(timezone.utc)
    for email, teams in TEAMS.items():
        tenant = ws.personal_tenant_id(email)
        db.collection(ws.PEOPLE).document(tenant).set(
            {"principal": email, "teams": teams, "first_seen": now, "last_seen": now})
    # Two personal-workspace requests: carol's (in no group) and bob's (in
    # eng). Both are a platform admin's to answer, never eng's admin's.
    for email, wid in (("carol@example.com", "w-c0ffee"), ("bob@example.com", "w-b0b0b0")):
        tenant = ws.personal_tenant_id(email)
        db.collection(ws.WORKSPACES).document(tenant).set({
            "tenant_id": tenant, "workspace_id": wid, "principal": email,
            "state": "requested", "request_id": str(uuid.uuid4()), "requested_at": now,
            "requested_via": "console", "decision": None, "history": [],
            "limits": dict(ws.DEFAULT_LIMITS), "run": None, "steps": {}, "failure": None,
            "ready_at": None, "migrated": False})
        db.collection(ws.WORKSPACE_IDS).document(wid).set({"tenant_id": tenant})


def h(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if tenant:
        headers["X-Swarm-Tenant"] = tenant
    return headers


def grants(db) -> dict[str, Any]:
    return db.dump(f"{roles.GRANTS_COLLECTION}/")


def writes(db) -> dict[str, Any]:
    """Every document but the platform-admin audit's own rows, which a
    platform admin's request writes before the route runs, by design."""
    return {k: v for k, v in db.dump().items()
            if not str(v.get("action", "")).startswith("platform_admin_")}


def name(client, who: str, tenant: str, role: str = "admin") -> None:
    response = client.put(f"/v1/admin/workspaces/{tenant}/roles/person", headers=h("ops"),
                          json={"email": who, "role": role})
    assert response.status_code == 200, response.text


@pytest.fixture
def alice_admin(client, clock) -> None:
    """alice is eng's admin, named by a platform admin; erin is research's."""
    name(client, "alice@example.com", "eng")
    clock.tick(seconds=1)
    name(client, "erin@example.com", "research")
    clock.tick(seconds=1)


# -- a workspace admin's read --------------------------------------------------


def test_a_workspace_admin_reads_only_their_workspaces_people_and_columns(client, alice_admin):
    response = client.get("/v1/admin/people", headers=h("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "workspace_admin"
    assert [w["tenant_id"] for w in body["workspaces"]] == ["eng"]
    emails = sorted(p["email"] for p in body["people"])
    # erin (research only) and carol (no group) are not eng's.
    assert emails == ["alice@example.com", "bob@example.com", "dave@example.com",
                      "root@example.com"]
    assert body["count"] == 4
    by_email = {p["email"]: p for p in body["people"]}
    # dave is in research too: only eng's cell is served.
    assert by_email["dave@example.com"]["roles"] == {"eng": "member"}
    assert by_email["alice@example.com"]["roles"] == {"eng": "admin"}
    # Nothing the platform holds: no personal-workspace state or request, no
    # Claude account or loan, no teams list, no admins, no audit.
    for row in body["people"]:
        assert set(row) == {"email", "display_name", "roles", "last_active"}
    assert body["pending"] == 0
    assert body["audit"] == []
    assert body["admins"] is None and body["lendable_accounts"] is None
    assert body["admins_error"] == body["lendable_error"] == "platform_admin_only"
    assert body["provisioning"] is None
    text = response.text
    for absent in ("research", "erin@", "carol@", "w-c0ffee", "w-b0b0b0", "ops@"):
        assert absent not in text, absent


def test_a_header_naming_another_workspace_does_not_widen_the_read(client, alice_admin):
    # alice is not in research, so selecting it is refused before the route;
    # selecting eng serves exactly what no header serves.
    refused = client.get("/v1/admin/people", headers=h("alice", "research"))
    assert refused.status_code == 403, refused.text
    plain = client.get("/v1/admin/people", headers=h("alice")).json()
    eng = client.get("/v1/admin/people", headers=h("alice", "eng")).json()
    assert [w["tenant_id"] for w in eng["workspaces"]] == ["eng"]
    assert sorted(p["email"] for p in eng["people"]) == sorted(
        p["email"] for p in plain["people"])


def test_a_role_override_holder_outside_the_group_is_still_a_row(client, clock, alice_admin):
    # A role given to someone not (yet) in eng is eng's roster (Q3): its
    # admin already reads it on Config › People and roles.
    name(client, "carol@example.com", "eng", role="viewer")
    body = client.get("/v1/admin/people", headers=h("alice")).json()
    carol = next(p for p in body["people"] if p["email"] == "carol@example.com")
    assert carol["roles"] == {"eng": "viewer"}


def test_a_demoted_workspace_admin_is_refused_on_the_next_read(client, clock, alice_admin):
    assert client.get("/v1/admin/people", headers=h("alice")).status_code == 200
    clock.tick(seconds=1)
    name(client, "alice@example.com", "eng", role="member")
    response = client.get("/v1/admin/people", headers=h("alice"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


# -- a workspace admin's actions -----------------------------------------------


def test_a_workspace_admin_changes_a_role_in_their_workspace(client, db, clock, alice_admin):
    before = len(grants(db))
    response = client.put(ROLE_ROUTE.format(tenant="eng"), headers=h("alice"),
                          json={"email": "bob@example.com", "role": "viewer"})
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "viewer"
    assert len(grants(db)) == before + 1
    row = next(p for p in client.get("/v1/admin/people", headers=h("alice")).json()["people"]
               if p["email"] == "bob@example.com")
    assert row["roles"] == {"eng": "viewer"}
    clock.tick(seconds=1)
    cleared = client.request("DELETE", ROLE_ROUTE.format(tenant="eng"), headers=h("alice"),
                             json={"email": "bob@example.com"})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["role"] == "member"


@pytest.mark.parametrize("tenant", ["research", "u-carol", "nope", "admin"])
def test_a_workspace_admin_acting_on_another_workspace_is_403_and_writes_nothing(
        client, db, alice_admin, tenant):
    before = writes(db)
    for method, body in (("PUT", {"email": "dave@example.com", "role": "admin"}),
                         ("DELETE", {"email": "erin@example.com"})):
        response = client.request(method, ROLE_ROUTE.format(tenant=tenant),
                                  headers=h("alice"), json=body)
        assert response.status_code == 403, (method, tenant, response.text)
        assert response.json()["code"] == "forbidden"
        # The refusal names nobody in the other workspace.
        assert "erin@" not in response.text
    assert writes(db) == before


def test_a_workspace_admin_selecting_eng_still_cannot_write_research(client, db, alice_admin):
    before = writes(db)
    response = client.put(ROLE_ROUTE.format(tenant="research"), headers=h("alice", "eng"),
                          json={"email": "dave@example.com", "role": "viewer"})
    assert response.status_code == 403, response.text
    assert writes(db) == before


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/admin/workspaces/w-b0b0b0/approve", None),
    ("POST", "/v1/admin/workspaces/w-b0b0b0/deny", {"reason": "no"}),
    ("PUT", "/v1/admin/workspaces/w-b0b0b0/limits", {"max_active": 3}),
    ("PUT", "/v1/admin/people/w-b0b0b0/loan", {"account_id": "acct-1", "lend": True}),
    ("POST", "/v1/admin/people/w-b0b0b0/loan/decline", None),
    ("PUT", "/v1/admin/workspaces/eng/roles/person", {"email": "bob@example.com",
                                                      "role": "viewer"}),
    ("GET", "/v1/admin/workspaces/roles", None),
])
def test_the_platform_routes_on_the_page_stay_a_platform_admins(
        client, db, alice_admin, method, path, body):
    # Even on bob, who is in eng: a personal workspace, its ceiling and its
    # loans are the platform's (S5f: "A request for a personal workspace is a
    # platform admin's").
    before = writes(db)
    response = client.request(method, path, headers=h("alice"), json=body)
    assert response.status_code == 403, response.text
    assert writes(db) == before


# -- a platform admin, a member ------------------------------------------------


def test_a_platform_admin_still_reads_everyone_and_every_workspace(client, alice_admin):
    response = client.get("/v1/admin/people", headers=h("ops"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "platform"
    assert [w["tenant_id"] for w in body["workspaces"]] == ["eng", "research"]
    emails = {p["email"] for p in body["people"]}
    assert {"carol@example.com", "erin@example.com", "alice@example.com"} <= emails
    by_email = {p["email"]: p for p in body["people"]}
    assert by_email["dave@example.com"]["roles"] == {"eng": "member", "research": "member"}
    assert by_email["erin@example.com"]["roles"] == {"eng": "none", "research": "admin"}
    assert by_email["carol@example.com"]["roles"] == {"eng": "none", "research": "none"}
    # Everything the page had before is still served.
    assert body["pending"] == 2
    assert by_email["carol@example.com"]["workspace"]["state"] == "requested"
    assert body["admins"] is not None


def test_a_platform_admin_changes_a_role_in_any_registered_workspace(client, db, alice_admin):
    response = client.put(ROLE_ROUTE.format(tenant="research"), headers=h("ops"),
                          json={"email": "dave@example.com", "role": "viewer"})
    assert response.status_code == 200, response.text
    rows = [r for r in db.dump("admin_audit/").values()
            if r["action"] == roles.AUDIT_GRANTED and r.get("target_email") == "dave@example.com"]
    assert rows and rows[-1]["detail"]["via"] == roles.VIA_PLATFORM_ADMIN
    missing = client.put(ROLE_ROUTE.format(tenant="u-carol"), headers=h("ops"),
                         json={"email": "carol@example.com", "role": "viewer"})
    assert missing.status_code == 404, missing.text


@pytest.mark.parametrize("who", ["bob", "carol"])
def test_a_member_or_a_person_with_no_group_is_refused_as_before(client, db, alice_admin, who):
    before = writes(db)
    response = client.get("/v1/admin/people", headers=h(who))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    write = client.put(ROLE_ROUTE.format(tenant="eng"), headers=h(who),
                       json={"email": "dave@example.com", "role": "admin"})
    assert write.status_code == 403, write.text
    assert writes(db) == before


def test_a_viewer_is_refused(client, clock, alice_admin):
    name(client, "bob@example.com", "eng", role="viewer")
    response = client.get("/v1/admin/people", headers=h("bob"))
    assert response.status_code == 403, response.text
