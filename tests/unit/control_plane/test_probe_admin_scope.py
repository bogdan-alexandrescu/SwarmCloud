"""A scoped probe admin is `admin` only in the group workspaces it is listed
for, and is never a platform admin (owner decision 2026-11-19; the roles and
join acceptance probes, docs/design/workspace-roles.md section 8 and
docs/design/join-requests.md section 8).

`ApiSettings.probe_admin_users` (env PROBE_ADMIN_USERS) lists (principal,
tenant ids). Who is who, over an in-memory Firestore:

* `probe` -- in eng and research, listed for `eng` only;
* `alice`, `bob` -- members of eng, no grant;
* `ops` -- a platform admin in no tenant group (it names a workspace admin);
* `carol` -- on ADMIN_USERS, so the config migration writes her document;
* `dave` -- in no group: his tenant is his personal workspace `u-dave`.

What is held: admin inside the listed tenant; outside it, exactly the role
the caller has without the entry; no `admin_roles` document and no entry in
the admin listing; every platform-admin route, and every grant or removal of
an admin, refused; another person's personal workspace still refused; an
empty setting changes nothing; and the setting's startup refusals.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import roles
from swarm_api.admins import ROLES_COLLECTION
from swarm_api.auth import AuthContext, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.settings import ApiSettings
from swarm_api.waker import NullWaker
from swarm_common.identity import Principal

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings
from .test_roles_store import Clock, Recording

PROBE = "probe@example.com"
GROUPS = {
    PROBE: (ENG_GROUP, RESEARCH_GROUP),
    "alice@example.com": (ENG_GROUP,),
    "bob@example.com": (ENG_GROUP,),
    "ops@example.com": (ADMIN_GROUP,),
    "carol@example.com": (),
    "dave@example.com": (),
}
LISTED = ((PROBE, ("eng",)),)
ADMIN_SWITCH = "REFUSAL_ROLE_ADMIN_REQUIRED"
#: One signing key for every client a test builds over the same store, so a
#: grant one writes verifies in the other.
KEY = secrets.token_hex(32)


def _client(db: Recording, clock: Clock, **settings: Any) -> TestClient:
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    context = build_context(
        settings=api_settings(settings_key=KEY, **settings),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(), now=clock,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


@pytest.fixture
def db() -> Recording:
    return Recording()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(db, clock) -> TestClient:
    return _client(db, clock, probe_admin_users=LISTED, admin_users=("carol@example.com",))


@pytest.fixture
def unlisted(db, clock) -> TestClient:
    """The same deployment with the setting empty."""
    return _client(db, clock, admin_users=("carol@example.com",))


def h(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if tenant:
        headers["X-Swarm-Tenant"] = tenant
    return headers


def me(client: TestClient, user: str, tenant: str) -> dict[str, Any]:
    response = client.get("/v1/workspace/roles", headers=h(user, tenant))
    assert response.status_code == 200, response.text
    return response.json()


def name_admin(client: TestClient, who: str) -> None:
    response = client.put("/v1/admin/workspaces/eng/roles/person", headers=h("ops"),
                          json={"email": who, "role": "admin"})
    assert response.status_code == 200, response.text


def set_default(client: TestClient, user: str, tenant: str, role: str) -> Any:
    return client.put("/v1/workspace/roles/default", headers=h(user, tenant),
                      json={"role": role, "reason": "probe"})


# -- inside its tenant ---------------------------------------------------------

def test_admin_inside_a_listed_tenant(client):
    body = me(client, "probe", "eng")
    assert (body["me"]["role"], body["me"]["source"]) == ("admin", "probe_admin")
    assert body["can_manage"] is True


def test_it_narrows_the_default_and_reads_the_audit_as_the_roles_probe_does(client, clock):
    assert set_default(client, "probe", "eng", "viewer").status_code == 200
    assert me(client, "alice", "eng")["me"]["role"] == "viewer"
    # A grant never narrows the probe admin itself: the default it set is inert for it.
    assert me(client, "probe", "eng")["me"]["role"] == "admin"
    clock.tick(seconds=1)
    assert set_default(client, "probe", "eng", "member").status_code == 200
    audit = me(client, "probe", "eng")["audit"]
    assert [e["action"] for e in audit][:2] == [roles.AUDIT_DEFAULT_SET] * 2
    assert all(e["by"] == PROBE for e in audit[:2])


# -- outside it ----------------------------------------------------------------

def test_member_outside_a_listed_tenant(client):
    body = me(client, "probe", "research")
    assert (body["me"]["role"], body["me"]["source"]) == ("member", "default")
    assert body["can_manage"] is False


def test_outside_it_resolves_exactly_as_without_the_entry(client, unlisted, clock):
    # research narrowed to viewer by a platform admin's default: the probe is
    # a viewer there, as it would be with no entry at all.
    assert client.put("/v1/admin/workspaces/research/roles/default", headers=h("ops"),
                      json={"role": "viewer"}).status_code == 200
    listed = me(client, "probe", "research")["me"]
    plain = me(unlisted, "probe", "research")["me"]
    assert listed == plain
    assert (listed["role"], listed["source"]) == ("viewer", "default")
    response = set_default(client, "probe", "research", "member")
    assert response.status_code == 403
    assert response.json()["code"] == "role_admin_required"


def test_it_selects_no_workspace_it_is_not_a_member_of(db, clock):
    # Listed for eng but in no group: the header still selects only the
    # caller's own memberships -- a role never admits anyone.
    client = _client(db, clock, probe_admin_users=(("dave@example.com", ("eng",)),))
    response = client.get("/v1/workspace/roles", headers=h("dave", "eng"))
    assert response.status_code == 403
    assert response.json()["code"] == "tenant_not_member"


def test_another_persons_personal_workspace_is_still_refused(client):
    # The roles probe's personal check: no entry reaches a `u-` workspace.
    assert client.get("/v1/tenants/me", headers=h("dave")).status_code == 200
    response = client.get("/v1/tenants/me", headers=h("probe", "u-dave"))
    assert response.status_code == 403
    assert response.json()["code"] == "tenant_not_member"


def test_resolve_never_matches_a_personal_or_unlisted_tenant():
    settings = api_settings(probe_admin_users=((PROBE, ("eng", "u-probe")),))
    store = roles.RoleStore(Recording(), settings)

    def ctx(tenant: str, principal: str) -> AuthContext:
        return AuthContext(principal=Principal(email=PROBE, subject="s", domain="example.com"),
                           tenant_id=tenant, is_admin=False, tenant_principal=principal)

    assert store.resolve(ctx("eng", ENG_GROUP)).source == roles.SOURCE_PROBE_ADMIN
    # Its own personal workspace is its own (`personal`), not the entry's.
    assert store.resolve(ctx("u-probe", PROBE)).source == roles.SOURCE_PERSONAL
    # A `u-` id under any other principal never matches the entry.
    assert not roles.probe_admin_scope(settings, ctx("u-probe", ENG_GROUP))
    assert not roles.probe_admin_scope(settings, ctx("research", RESEARCH_GROUP))


# -- never a platform admin ------------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/admin/tenants", "/v1/admin/workspaces/eng/roles"])
def test_every_platform_admin_route_is_refused(client, path):
    assert client.get(path, headers=h("probe", "eng")).status_code == 403


def test_admin_people_is_a_workspace_admins_view_of_its_listed_workspace(client):
    # Served as to a workspace admin (`scope: workspaces`), of eng alone,
    # and without the platform's admin list.
    response = client.get("/v1/admin/people", headers=h("probe"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] != "platform"
    assert [w["tenant_id"] for w in body["workspaces"]] == ["eng"]
    assert not body.get("admins")


def test_tenants_me_does_not_call_it_an_admin(client):
    body = client.get("/v1/tenants/me", headers=h("probe", "eng")).json()
    assert body["principal"]["is_admin"] is False
    assert body["role"] == {"name": "admin", "source": "probe_admin"}


def test_no_admin_roles_write_and_no_admin_listing_entry(client, db):
    # The first request runs the config migration (ADMIN_USERS -> admin_roles).
    assert client.get("/v1/tenants/me", headers=h("probe", "eng")).status_code == 200
    docs = {p.rsplit("/", 1)[1] for p in db.paths(f"{ROLES_COLLECTION}/")}
    assert "carol@example.com" in docs, "the migration ran"
    assert PROBE not in docs
    listing = client.get("/v1/admin/people", headers=h("ops"))
    assert listing.status_code == 200, listing.text
    admins = listing.json()["admins"]
    assert admins is not None
    assert PROBE not in {str(a["email"]).lower() for a in admins}


@pytest.mark.parametrize("method", ["put", "delete"])
@pytest.mark.parametrize("target", [PROBE, "carol@example.com", "bob@example.com"])
def test_it_cannot_grant_or_remove_a_platform_admin(client, db, method, target):
    response = getattr(client, method)(f"/v1/admin/admins/{target}", headers=h("probe", "eng"))
    assert response.status_code == 403
    docs = {p.rsplit("/", 1)[1] for p in db.paths(f"{ROLES_COLLECTION}/")}
    assert PROBE not in docs and "bob@example.com" not in docs


def test_it_cannot_name_a_workspace_admin(client, db, monkeypatch):
    monkeypatch.delenv(ADMIN_SWITCH, raising=False)
    response = client.put("/v1/workspace/roles/person", headers=h("probe", "eng"),
                          json={"email": "bob@example.com", "role": "admin"})
    assert response.status_code == 403
    assert response.json()["code"] == "role_admin_required"
    assert response.json()["detail"]["source"] == "probe_admin"
    assert me(client, "bob", "eng")["me"]["role"] == "member"


@pytest.mark.parametrize("change", ["narrow", "clear"])
def test_it_cannot_remove_a_workspace_admin(client, clock, change):
    name_admin(client, "alice@example.com")
    clock.tick(seconds=1)
    if change == "narrow":
        response = client.put("/v1/workspace/roles/person", headers=h("probe", "eng"),
                              json={"email": "alice@example.com", "role": "viewer"})
    else:
        response = client.request("DELETE", "/v1/workspace/roles/person",
                                  headers=h("probe", "eng"),
                                  json={"email": "alice@example.com"})
    assert response.status_code == 403
    assert response.json()["code"] == "role_admin_required"
    assert me(client, "alice", "eng")["me"]["role"] == "admin"


def test_it_may_narrow_a_member(client, clock):
    response = client.put("/v1/workspace/roles/person", headers=h("probe", "eng"),
                          json={"email": "bob@example.com", "role": "viewer"})
    assert response.status_code == 200, response.text
    assert me(client, "bob", "eng")["me"]["role"] == "viewer"


# -- an empty setting changes nothing --------------------------------------------

def test_an_empty_setting_changes_nothing(unlisted):
    body = me(unlisted, "probe", "eng")
    assert (body["me"]["role"], body["me"]["source"]) == ("member", "default")
    response = set_default(unlisted, "probe", "eng", "viewer")
    assert response.status_code == 403
    assert response.json()["code"] == "role_admin_required"
    assert ApiSettings.__dataclass_fields__["probe_admin_users"].default == ()


# -- the setting, refused at startup ---------------------------------------------

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "example-project")
    for name in ("PROBE_ADMIN_USERS", "ADMIN_USERS", "ADMIN_POOL_USERS", "PLATFORM_OWNER",
                 "ROLLUP_SWEEPER_USERS", "TENANT_SERVICE_ACCOUNTS", "SECRET_ADMIN_PRINCIPALS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("raw", [None, "", "[]"])
def test_no_setting_is_an_empty_tuple(env, raw):
    if raw is not None:
        env.setenv("PROBE_ADMIN_USERS", raw)
    assert ApiSettings.from_env().probe_admin_users == ()


def test_the_setting_is_parsed_lower_cased(env):
    env.setenv("PROBE_ADMIN_USERS", json.dumps(
        [{"principal": "Probe@Example.com", "tenants": ["Eng", "smoke", "eng"]}]))
    assert ApiSettings.from_env().probe_admin_users == ((PROBE, ("eng", "smoke")),)


def test_a_pool_admin_may_also_be_a_probe_admin(env):
    env.setenv("ADMIN_POOL_USERS", PROBE)
    env.setenv("PROBE_ADMIN_USERS", json.dumps([{"principal": PROBE, "tenants": ["eng"]}]))
    assert ApiSettings.from_env().probe_admin_users == ((PROBE, ("eng",)),)


@pytest.mark.parametrize("raw", [
    "not json",
    json.dumps({"principal": PROBE, "tenants": ["eng"]}),
    json.dumps([{"principal": PROBE}]),
    json.dumps([{"principal": PROBE, "tenants": ["eng"], "role": "admin"}]),
    json.dumps([{"principal": "serviceAccount:" + PROBE, "tenants": ["eng"]}]),
    json.dumps([{"principal": "a@example.com,b@example.com", "tenants": ["eng"]}]),
    json.dumps([{"principal": PROBE, "tenants": []}]),
    json.dumps([{"principal": PROBE, "tenants": "eng"}]),
    json.dumps([{"principal": PROBE, "tenants": ["u-someone"]}]),
    json.dumps([{"principal": PROBE, "tenants": ["eng@example.com"]}]),
    json.dumps([{"principal": PROBE, "tenants": ["eng"]},
                {"principal": PROBE.upper(), "tenants": ["smoke"]}]),
])
def test_a_malformed_setting_is_refused(env, raw):
    env.setenv("PROBE_ADMIN_USERS", raw)
    with pytest.raises(ValueError, match="PROBE_ADMIN_USERS"):
        ApiSettings.from_env()


@pytest.mark.parametrize("name", ["ADMIN_USERS", "PLATFORM_OWNER", "ROLLUP_SWEEPER_USERS"])
def test_a_wider_role_on_the_same_principal_is_refused(env, name):
    env.setenv(name, PROBE)
    env.setenv("PROBE_ADMIN_USERS", json.dumps([{"principal": PROBE, "tenants": ["eng"]}]))
    with pytest.raises(ValueError, match=name):
        ApiSettings.from_env()
