"""A scoped probe admin decides join requests in its listed workspace, as the
join acceptance probe's admin does, and nowhere else (owner decision
2026-11-19; docs/design/join-requests.md section 8).

The probe admin here is a SERVICE ACCOUNT on ALLOWED_USERS, in eng's group
and research's, listed for `eng` only -- the shape the probe's
`JOIN_ADMIN_SA` takes. Without the entry Admin › People refuses a service
account outright (`people_scope`: it holds no person's role); with it, the
entry is its workspace role there and nowhere else.
"""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from swarm_api import roles
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import (
    ADMIN_GROUP,
    ENG_GROUP,
    RESEARCH_GROUP,
    NoForgeTokens,
    api_settings,
    seed_tenant,
)
from .fakes import FakeFirestore
from .test_join_requests_routes import (
    CONSOLE,
    approve,
    ask,
    decisions,
    h,
    link_on,
    written,
)
from .test_people_admin import FakePool, FakePublisher

#: The row a probe admin's refused change leaves (`roles.AUDIT_PROBE_ADMIN_REFUSED`).
PROBE_REFUSED = "workspace_probe_admin_refused"
#: Built at runtime, like every identity here.
PROBE = "roles-probe@" + "example-project.iam.gserviceaccount.com"
GROUPS = {
    PROBE: (ENG_GROUP, RESEARCH_GROUP),
    "ops@example.com": (ADMIN_GROUP,),
    "erin@example.com": (RESEARCH_GROUP,),
    "lee@example.com": (),
    "kim@example.com": (),
}


def _client(db, probe_admin_users, **settings) -> TestClient:
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    ctx = build_context(
        settings=api_settings(settings_key=secrets.token_hex(32), console_url=CONSOLE,
                              rate_limit_burst=10_000, allowed_users=(PROBE,),
                              probe_admin_users=probe_admin_users, **settings),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(),
    )
    app = create_app(ctx)
    app.state.workspace_publisher = FakePublisher()
    app.state.account_pool = FakePool()
    client = TestClient(app, raise_server_exceptions=False)
    response = client.put("/v1/admin/workspaces/research/roles/person", headers=h("ops"),
                          json={"email": "erin@example.com", "role": "admin"})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def db() -> FakeFirestore:
    db = FakeFirestore()
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return db


@pytest.fixture
def client(db) -> TestClient:
    return _client(db, ((PROBE, ("eng",)),))


def test_it_turns_the_link_on_and_approves_in_its_listed_workspace(client, db) -> None:
    code = link_on(client, "roles-probe", "eng")
    ref = ask(client, "lee", code)["ref"]
    response = approve(client, "roles-probe", ref)
    assert response.status_code == 200, response.text
    assert [d["via"] for d in decisions(db)] == ["probe_admin"]


def test_it_decides_nothing_outside_it(client, db) -> None:
    ref = ask(client, "kim", link_on(client, "erin", "research"))["ref"]
    before = written(db)
    response = approve(client, "roles-probe", ref)
    assert response.status_code == 403
    assert response.json()["code"] == "join_decider_required"
    response = client.put("/v1/workspace/join-link", headers=h("roles-probe", "research"),
                          json={"on": False})
    assert response.status_code == 403
    assert written(db) == before


def test_its_admin_people_view_is_its_listed_workspace_only(client) -> None:
    response = client.get("/v1/admin/people", headers=h("roles-probe"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] != "platform"
    assert [w["tenant_id"] for w in body["workspaces"]] == ["eng"]
    assert not body.get("admins")


def test_without_the_entry_the_service_account_is_refused(db) -> None:
    client = _client(db, ())
    ref = ask(client, "kim", link_on(client, "erin", "research"))["ref"]
    assert approve(client, "roles-probe", ref).status_code == 403
    assert client.get("/v1/admin/people", headers=h("roles-probe")).status_code == 403
    response = client.put("/v1/workspace/join-link", headers=h("roles-probe", "eng"),
                          json={"on": True})
    assert response.status_code == 403


# -- least privilege (owner decision 2026-11-20) -------------------------------------


def _grant(client, tenant: str, who: str) -> dict:
    response = client.get(f"/v1/admin/workspaces/{tenant}/roles", headers=h("ops"))
    assert response.status_code == 200, response.text
    return {p["email"]: p["role"] for p in response.json()["people"]}.get(who)


def _refusal_rows(db) -> list[dict]:
    return [d for p, d in db.dump(f"{AUDIT_COLLECTION}/").items()
            if d.get("action") == PROBE_REFUSED]


def _unrefused(db) -> dict:
    """`written`, less a probe-admin refusal's own row (asserted apart)."""
    return {k: v for k, v in written(db).items() if v.get("action") != PROBE_REFUSED}


def _one_refusal(db, route: str, target: str, *, refused: str = "role_admin_required",
                 **detail) -> None:
    (row,) = _refusal_rows(db)
    assert (row["by"], row["target_email"], row["target_tenant_id"]) == (PROBE, target, "eng")
    assert row["detail"] == dict(detail, route=route, via="probe_admin", refused=refused)


def test_an_approval_never_clears_an_admin_override_stored_ahead(db) -> None:
    """A platform admin names kim eng's admin before kim joins; the probe
    admin's approval with the default role would CLEAR that override
    (`_role_change`), so it is refused and nothing is written."""
    client = _client(db, ((PROBE, ("eng",)),), platform_memberships=True)
    response = client.put("/v1/admin/workspaces/eng/roles/person", headers=h("ops"),
                          json={"email": "kim@example.com", "role": "admin"})
    assert response.status_code == 200, response.text
    ref = ask(client, "kim", link_on(client, "roles-probe", "eng"))["ref"]
    before = _unrefused(db)
    response = approve(client, "roles-probe", ref)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_admin_required"
    assert _unrefused(db) == before
    _one_refusal(db, "join_approved", "kim@example.com", to="member")
    assert _grant(client, "eng", "kim@example.com") == "admin"
    # A platform admin's approval still decides it, as before.
    assert approve(client, "ops", ref).status_code == 200


def test_its_approval_and_its_role_write_say_probe_admin(db) -> None:
    client = _client(db, ((PROBE, ("eng",)),), platform_memberships=True)
    ref = ask(client, "lee", link_on(client, "roles-probe", "eng"))["ref"]
    response = approve(client, "roles-probe", ref, role="viewer")
    assert response.status_code == 200, response.text
    assert [d["via"] for d in decisions(db)] == ["probe_admin"]
    vias = {d.get("via") for p, d in db.dump().items()
            if p.startswith(f"{roles.GRANTS_COLLECTION}/") and d.get("subject") == "lee@example.com"}
    assert vias == {"probe_admin"}


def test_it_denies_in_its_listed_workspace_recorded_as_probe_admin(client, db) -> None:
    ref = ask(client, "lee", link_on(client, "roles-probe", "eng"))["ref"]
    response = client.post(f"/v1/admin/join-requests/{ref}/deny", headers=h("roles-probe"),
                           json={"reason": "the probe's own request"})
    assert response.status_code == 200, response.text
    assert [d["via"] for d in decisions(db)] == ["probe_admin"]


def test_it_lists_members_and_never_removes_one(db) -> None:
    client = _client(db, ((PROBE, ("eng",)),), platform_memberships=True)
    ref = ask(client, "lee", link_on(client, "roles-probe", "eng"))["ref"]
    assert approve(client, "ops", ref).status_code == 200
    listed = client.get("/v1/workspace/members", headers=h("roles-probe", "eng"))
    assert listed.status_code == 200, listed.text
    assert [m["email"] for m in listed.json()["members"]] == ["lee@example.com"]
    before = _unrefused(db)
    response = client.request("DELETE", "/v1/workspace/members/person",
                              headers=h("roles-probe", "eng"),
                              json={"email": "lee@example.com", "reason": "probe"})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "probe_admin_not_permitted"
    assert _unrefused(db) == before
    _one_refusal(db, "member_removed", "lee@example.com", refused="probe_admin_not_permitted")


def test_an_admin_grant_stored_between_its_check_and_its_write_is_never_cleared(db, monkeypatch):
    """The race the security re-check found (epic 21): a platform admin names
    kim eng's admin AFTER the probe admin's approval passed its early check
    and BEFORE its transaction writes. The refusal is decided again inside
    the transaction, on the role read after the last-admin guard, so the
    approval is still 403 `role_admin_required` and writes nothing."""
    client = _client(db, ((PROBE, ("eng",)),), platform_memberships=True)
    ref = ask(client, "kim", link_on(client, "roles-probe", "eng"))["ref"]
    real = roles.probe_admin_admin_target
    raced: list[int] = []
    before: list[dict] = []

    def check_then_race(*args, **kwargs):
        real(*args, **kwargs)
        response = client.put("/v1/admin/workspaces/eng/roles/person", headers=h("ops"),
                              json={"email": "kim@example.com", "role": "admin"})
        raced.append(response.status_code)
        before.append(_unrefused(db))

    monkeypatch.setattr(roles, "probe_admin_admin_target", check_then_race)
    response = approve(client, "roles-probe", ref)
    monkeypatch.setattr(roles, "probe_admin_admin_target", real)
    assert raced == [200]
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_admin_required"
    assert decisions(db) == []
    assert _unrefused(db) == before[0]
    # Refused inside the transaction: one row, written after it rolled back.
    # MUTATION: drop the row from the approval's refusal -> none here.
    _one_refusal(db, "join_approved", "kim@example.com", to="member")
    assert _grant(client, "eng", "kim@example.com") == "admin"


@pytest.mark.parametrize("tenant", ["eng", "research"])
def test_its_notification_feed_is_its_listed_workspaces_join_requests_only(client, tenant):
    """Pinned so a later widening of the feed cannot widen it silently (epic
    21): the probe admin is rung for the open join request of eng, the one
    workspace its entry lists, whichever workspace it reads the feed in --
    never research's (erin's), never an admin's platform-wide view, and
    nothing besides that one item."""
    lee_ref = ask(client, "lee", link_on(client, "roles-probe", "eng"))["ref"]
    ask(client, "kim", link_on(client, "erin", "research"))
    response = client.get("/v1/notifications", headers=h("roles-probe", tenant))
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [f"admin:join:{lee_ref}"]
    assert body["partial"] is False
