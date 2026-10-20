"""Admin roles live in Firestore (docs/workspaces.md §6.5, lane W2 of #847).

What is held here, each against the shipped routes over an in-memory Firestore:

* the one-time migration of ADMIN_USERS is idempotent, and does not grant
  again an admin someone removed after it ran;
* PLATFORM_OWNER is seeded as the one `role: owner` document;
* grant and remove work, and each writes an `admin_audit` entry;
* removing the last admin is refused (`LAST_ADMIN`);
* the owner cannot be demoted by anyone else (`OWNER_PROTECTED`);
* a non-admin, and a pool admin, are refused with 403;
* configuration stays the fallback: the owner and ADMIN_USERS are admins with
  no document and with Firestore failing, and a failed role read for anyone
  else is a 503 on an admin route, never a 403 and never a grant.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.admins import (
    AUDIT_COLLECTION,
    CONFIG_MIGRATION,
    ROLES_COLLECTION,
    AdminRoles,
)
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, NoForgeTokens, api_settings, auth_header
from .fakes import FakeFirestore

OWNER = "owner@example.com"
#: Any admin-only route: what "is this caller an admin now?" is asked through.
ADMIN_ROUTE = "/v1/admin/tenants"

PEOPLE = {
    "alice@example.com": (ENG_GROUP,),
    "carol@example.com": (),
    "dave@example.com": (),
    "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    OWNER: (ENG_GROUP,),
    "gate@example.com": (),
}


def _tokens() -> dict[str, dict[str, Any]]:
    return {
        f"token-{email.split('@')[0]}": {
            "email": email,
            "email_verified": True,
            "sub": f"sub-{email}",
            "hd": "example.com",
        }
        for email in PEOPLE
    }


def _client(db: FakeFirestore, **settings: Any) -> TestClient:
    context = build_context(
        settings=api_settings(**settings),
        db=db,
        verifier=StaticTokenVerifier(_tokens()),
        groups=StaticGroups(PEOPLE),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=None,
        forge_tokens=NoForgeTokens(),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _roles(db: FakeFirestore) -> dict[str, dict[str, Any]]:
    prefix = f"{ROLES_COLLECTION}/"
    return {path[len(prefix):]: doc for path, doc in db.dump(prefix).items()}


def _audit(db: FakeFirestore) -> list[dict[str, Any]]:
    return list(db.dump(f"{AUDIT_COLLECTION}/").values())


def _actions(db: FakeFirestore, action: str) -> list[dict[str, Any]]:
    return [entry for entry in _audit(db) if entry["action"] == action]


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


# -- the migration and the owner's seed --------------------------------------


def test_migration_writes_each_config_admin_once_and_seeds_the_owner(db):
    roles = AdminRoles(db, owner=OWNER, config_admins=("Carol@example.com", OWNER),
                       allowed_domains=("example.com",))
    roles.ensure_migrated()

    docs = _roles(db)
    assert docs[OWNER]["role"] == "owner"
    assert docs["carol@example.com"]["role"] == "admin"
    assert docs["carol@example.com"]["granted_by"] == CONFIG_MIGRATION
    assert set(docs) == {OWNER, "carol@example.com"}, "the owner is not also migrated as an admin"
    assert len(_actions(db, "migrate")) == 1
    assert len(_actions(db, "seed_owner")) == 1

    before = db.dump()
    # A second process, and a second call in the same one: nothing written.
    again = AdminRoles(db, owner=OWNER, config_admins=("carol@example.com", OWNER),
                       allowed_domains=("example.com",))
    again.ensure_migrated()
    roles.ensure_migrated()
    assert db.dump() == before


def test_migration_does_not_grant_again_an_admin_removed_after_it(db):
    roles = AdminRoles(db, owner=OWNER, config_admins=("carol@example.com",),
                       allowed_domains=("example.com",))
    roles.ensure_migrated()
    roles.remove("carol@example.com", by=OWNER)
    assert "carol@example.com" not in _roles(db)

    AdminRoles(db, owner=OWNER, config_admins=("carol@example.com",),
               allowed_domains=("example.com",)).ensure_migrated()
    assert "carol@example.com" not in _roles(db)


def test_a_changed_owner_demotes_the_previous_one_to_admin(db):
    AdminRoles(db, owner="old@example.com").ensure_migrated()
    AdminRoles(db, owner=OWNER).ensure_migrated()
    docs = _roles(db)
    assert docs[OWNER]["role"] == "owner"
    assert docs["old@example.com"]["role"] == "admin"
    assert [e["target_email"] for e in _actions(db, "owner_changed")] == ["old@example.com"]


def test_the_first_request_runs_the_migration(db):
    client = _client(db, platform_owner=OWNER, admin_users=("carol@example.com",))
    assert client.get("/v1/tenants/me", headers=auth_header("alice")).status_code == 200
    docs = _roles(db)
    assert docs[OWNER]["role"] == "owner"
    assert docs["carol@example.com"]["role"] == "admin"


# -- grant and remove ----------------------------------------------------------


def test_grant_makes_an_admin_and_is_audited(db):
    client = _client(db)
    assert client.get(ADMIN_ROUTE, headers=auth_header("carol")).status_code == 403

    response = client.put("/v1/admin/admins/Carol@example.com", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "carol@example.com"
    assert body["role"] == "admin"
    assert body["granted_by"] == "root@example.com"
    assert body["changed"] is True
    assert _roles(db)["carol@example.com"]["granted_by"] == "root@example.com"

    [entry] = _actions(db, "grant")
    assert entry["target_email"] == "carol@example.com"
    assert entry["by"] == "root@example.com"
    assert entry["at"] is not None

    assert client.get(ADMIN_ROUTE, headers=auth_header("carol")).status_code == 200

    again = client.put("/v1/admin/admins/carol@example.com", headers=auth_header("carol"))
    assert again.status_code == 200
    assert again.json()["changed"] is False
    assert len(_actions(db, "grant")) == 1, "a no-op grant is not a change and is not audited"


def test_remove_takes_the_right_away_and_is_audited(db):
    client = _client(db)
    for person in ("carol", "dave"):
        assert client.put(f"/v1/admin/admins/{person}@example.com",
                          headers=auth_header("root")).status_code == 200
    assert client.get(ADMIN_ROUTE, headers=auth_header("carol")).status_code == 200

    response = client.delete("/v1/admin/admins/carol@example.com", headers=auth_header("dave"))
    assert response.status_code == 200, response.text
    assert response.json() == {"email": "carol@example.com", "removed": True}
    assert "carol@example.com" not in _roles(db)
    [entry] = _actions(db, "remove")
    assert (entry["target_email"], entry["by"]) == ("carol@example.com", "dave@example.com")
    assert entry["detail"] == {"previous_role": "admin"}

    # The instance that removed the right forgets it at once.
    assert client.get(ADMIN_ROUTE, headers=auth_header("carol")).status_code == 403


def test_removing_the_last_admin_is_refused(db):
    client = _client(db)
    assert client.put("/v1/admin/admins/carol@example.com",
                      headers=auth_header("root")).status_code == 200

    response = client.delete("/v1/admin/admins/carol@example.com", headers=auth_header("root"))
    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMIN"
    assert "carol@example.com" in _roles(db)
    assert _actions(db, "remove") == []

    # Removing yourself is refused the same way.
    response = client.delete("/v1/admin/admins/carol@example.com", headers=auth_header("carol"))
    assert response.json()["code"] == "LAST_ADMIN"


def test_the_owner_cannot_be_demoted_by_anyone_else(db):
    client = _client(db, platform_owner=OWNER)
    assert client.put("/v1/admin/admins/carol@example.com",
                      headers=auth_header("root")).status_code == 200

    for caller in ("root", "carol"):
        response = client.delete(f"/v1/admin/admins/{OWNER}", headers=auth_header(caller))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "OWNER_PROTECTED"
        response = client.put(f"/v1/admin/admins/{OWNER}", headers=auth_header(caller))
        assert response.status_code == 403
        assert response.json()["code"] == "OWNER_PROTECTED"

    # The owner themselves: configuration makes the owner, so not here either.
    response = client.delete(f"/v1/admin/admins/{OWNER}", headers=auth_header("owner"))
    assert response.status_code == 409
    assert response.json()["code"] == "OWNER_FROM_CONFIG"

    assert _roles(db)[OWNER]["role"] == "owner"
    assert _actions(db, "remove") == []


def test_the_owner_may_remove_an_admin(db):
    client = _client(db, platform_owner=OWNER)
    assert client.put("/v1/admin/admins/carol@example.com",
                      headers=auth_header("root")).status_code == 200
    response = client.delete("/v1/admin/admins/carol@example.com", headers=auth_header("owner"))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_non_admin_is_refused(db, method):
    client = _client(db, platform_owner=OWNER)
    response = getattr(client, method)("/v1/admin/admins/alice@example.com",
                                       headers=auth_header("alice"))
    assert response.status_code == 403
    assert "alice@example.com" not in _roles(db)
    assert _actions(db, "grant") == [] and _actions(db, "remove") == []


@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_pool_admin_is_refused(db, method):
    client = _client(db, admin_pool_users=("gate@example.com",))
    response = getattr(client, method)("/v1/admin/admins/gate@example.com",
                                       headers=auth_header("gate"))
    assert response.status_code == 403
    assert "gate@example.com" not in _roles(db)


@pytest.mark.parametrize("target", [
    "worker@saga-agents-staging.iam.gserviceaccount.com",
    "someone@elsewhere.com",
    "not-an-email",
])
def test_grant_is_only_for_an_allowed_domain_person(db, target):
    client = _client(db)
    response = client.put(f"/v1/admin/admins/{target}", headers=auth_header("root"))
    assert response.status_code == 422, response.text
    assert _roles(db) == {}


def test_removing_a_config_admin_says_configuration_removes_it(db):
    client = _client(db, admin_users=("carol@example.com",))
    # The migration gave carol a document; take it away, keep ADMIN_USERS.
    assert client.put("/v1/admin/admins/dave@example.com",
                      headers=auth_header("root")).status_code == 200
    assert client.delete("/v1/admin/admins/carol@example.com",
                         headers=auth_header("root")).status_code == 200
    response = client.delete("/v1/admin/admins/carol@example.com", headers=auth_header("root"))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_AN_ADMIN_DOCUMENT"
    assert "ADMIN_USERS" in response.json()["message"]


# -- configuration is the fallback ---------------------------------------------


class FailingRoles:
    """Firestore does not answer the role read."""

    def holds_admin(self, email: str) -> bool:
        raise RuntimeError("firestore unavailable")


def test_config_admins_and_the_owner_are_admins_with_firestore_down(db):
    client = _client(db, platform_owner=OWNER, admin_users=("carol@example.com",))
    client.app.state.ctx.authenticator.admin_roles = FailingRoles()
    for caller in ("owner", "carol", "root"):
        assert client.get(ADMIN_ROUTE, headers=auth_header(caller)).status_code == 200, caller
    assert _roles(db) == {}, "nothing was migrated, and config still decided"


def test_a_failed_role_read_is_a_503_not_a_403(db):
    client = _client(db)
    client.app.state.ctx.authenticator.admin_roles = FailingRoles()
    response = client.get(ADMIN_ROUTE, headers=auth_header("dave"))
    assert response.status_code == 503
    me = client.get("/v1/tenants/me", headers=auth_header("dave")).json()
    assert me["principal"]["is_admin"] is False
    assert me["principal"]["is_admin_unresolved"] is True


def test_a_firestore_admin_is_an_admin_without_any_config(db):
    db.document(f"{ROLES_COLLECTION}/dave@example.com").set(
        {"role": "admin", "granted_by": "root@example.com", "granted_at": None})
    client = _client(db, admin_groups=())
    assert client.get(ADMIN_ROUTE, headers=auth_header("dave")).status_code == 200
    assert client.get(ADMIN_ROUTE, headers=auth_header("alice")).status_code == 403


# -- who the admins are: the People read's `admins` section ------------------


class FailingHolders(AdminRoles):
    """Firestore does not answer the holders' read; the role check still does."""

    def holders(self) -> list[dict[str, Any]]:
        raise RuntimeError("firestore unavailable")


def test_the_people_read_lists_the_admins_with_the_owner_first(db):
    client = _client(db, platform_owner=OWNER)
    assert client.put("/v1/admin/admins/carol@example.com",
                      headers=auth_header("owner")).status_code == 200
    body = client.get("/v1/admin/people", headers=auth_header("root")).json()
    assert body["admins_error"] is None
    assert [(a["email"], a["role"]) for a in body["admins"]] == [
        (OWNER, "owner"), ("carol@example.com", "admin")]
    carol = body["admins"][1]
    assert carol["granted_by"] == OWNER and carol["granted_at"] is not None
    assert "changed" not in carol, "a listing is not a change"


def test_the_configured_owner_is_listed_before_their_document_exists():
    roles = AdminRoles(FakeFirestore(), owner=OWNER)
    roles._migrated = True  # the seed has not run: no document at all
    assert roles.holders() == [{"email": OWNER, "role": "owner",
                                "granted_by": "config:PLATFORM_OWNER", "granted_at": None}]


def test_the_people_read_is_served_when_the_admins_are_not_read(db):
    client = _client(db, platform_owner=OWNER)
    client.app.state.ctx.authenticator.admin_roles = FailingHolders(db, owner=OWNER)
    response = client.get("/v1/admin/people", headers=auth_header("owner"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admins"] is None and body["admins_error"] == "RuntimeError"
    assert "people" in body
