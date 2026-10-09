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

OWNER = "owner@saga.xyz"
#: Any admin-only route: what "is this caller an admin now?" is asked through.
ADMIN_ROUTE = "/v1/admin/tenants"

PEOPLE = {
    "alice@saga.xyz": (ENG_GROUP,),
    "carol@saga.xyz": (),
    "dave@saga.xyz": (),
    "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
    OWNER: (ENG_GROUP,),
    "gate@saga.xyz": (),
}


def _tokens() -> dict[str, dict[str, Any]]:
    return {
        f"token-{email.split('@')[0]}": {
            "email": email,
            "email_verified": True,
            "sub": f"sub-{email}",
            "hd": "saga.xyz",
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
    roles = AdminRoles(db, owner=OWNER, config_admins=("Carol@saga.xyz", OWNER),
                       allowed_domains=("saga.xyz",))
    roles.ensure_migrated()

    docs = _roles(db)
    assert docs[OWNER]["role"] == "owner"
    assert docs["carol@saga.xyz"]["role"] == "admin"
    assert docs["carol@saga.xyz"]["granted_by"] == CONFIG_MIGRATION
    assert set(docs) == {OWNER, "carol@saga.xyz"}, "the owner is not also migrated as an admin"
    assert len(_actions(db, "migrate")) == 1
    assert len(_actions(db, "seed_owner")) == 1

    before = db.dump()
    # A second process, and a second call in the same one: nothing written.
    again = AdminRoles(db, owner=OWNER, config_admins=("carol@saga.xyz", OWNER),
                       allowed_domains=("saga.xyz",))
    again.ensure_migrated()
    roles.ensure_migrated()
    assert db.dump() == before


def test_migration_does_not_grant_again_an_admin_removed_after_it(db):
    roles = AdminRoles(db, owner=OWNER, config_admins=("carol@saga.xyz",),
                       allowed_domains=("saga.xyz",))
    roles.ensure_migrated()
    roles.remove("carol@saga.xyz", by=OWNER)
    assert "carol@saga.xyz" not in _roles(db)

    AdminRoles(db, owner=OWNER, config_admins=("carol@saga.xyz",),
               allowed_domains=("saga.xyz",)).ensure_migrated()
    assert "carol@saga.xyz" not in _roles(db)


def test_a_changed_owner_demotes_the_previous_one_to_admin(db):
    AdminRoles(db, owner="old@saga.xyz").ensure_migrated()
    AdminRoles(db, owner=OWNER).ensure_migrated()
    docs = _roles(db)
    assert docs[OWNER]["role"] == "owner"
    assert docs["old@saga.xyz"]["role"] == "admin"
    assert [e["target_email"] for e in _actions(db, "owner_changed")] == ["old@saga.xyz"]


def test_the_first_request_runs_the_migration(db):
    client = _client(db, platform_owner=OWNER, admin_users=("carol@saga.xyz",))
    assert client.get("/v1/tenants/me", headers=auth_header("alice")).status_code == 200
    docs = _roles(db)
    assert docs[OWNER]["role"] == "owner"
    assert docs["carol@saga.xyz"]["role"] == "admin"


# -- grant and remove ----------------------------------------------------------


def test_grant_makes_an_admin_and_is_audited(db):
    client = _client(db)
    assert client.get(ADMIN_ROUTE, headers=auth_header("carol")).status_code == 403

    response = client.put("/v1/admin/admins/Carol@saga.xyz", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "carol@saga.xyz"
    assert body["role"] == "admin"
    assert body["granted_by"] == "root@saga.xyz"
    assert body["changed"] is True
    assert _roles(db)["carol@saga.xyz"]["granted_by"] == "root@saga.xyz"

    [entry] = _actions(db, "grant")
    assert entry["target_email"] == "carol@saga.xyz"
    assert entry["by"] == "root@saga.xyz"
    assert entry["at"] is not None

    assert client.get(ADMIN_ROUTE, headers=auth_header("carol")).status_code == 200

    again = client.put("/v1/admin/admins/carol@saga.xyz", headers=auth_header("carol"))
    assert again.status_code == 200
    assert again.json()["changed"] is False
    assert len(_actions(db, "grant")) == 1, "a no-op grant is not a change and is not audited"


def test_remove_takes_the_right_away_and_is_audited(db):
    client = _client(db)
    for person in ("carol", "dave"):
        assert client.put(f"/v1/admin/admins/{person}@saga.xyz",
                          headers=auth_header("root")).status_code == 200
    assert client.get(ADMIN_ROUTE, headers=auth_header("carol")).status_code == 200

    response = client.delete("/v1/admin/admins/carol@saga.xyz", headers=auth_header("dave"))
    assert response.status_code == 200, response.text
    assert response.json() == {"email": "carol@saga.xyz", "removed": True}
    assert "carol@saga.xyz" not in _roles(db)
    [entry] = _actions(db, "remove")
    assert (entry["target_email"], entry["by"]) == ("carol@saga.xyz", "dave@saga.xyz")
    assert entry["detail"] == {"previous_role": "admin"}

    # The instance that removed the right forgets it at once.
    assert client.get(ADMIN_ROUTE, headers=auth_header("carol")).status_code == 403


def test_removing_the_last_admin_is_refused(db):
    client = _client(db)
    assert client.put("/v1/admin/admins/carol@saga.xyz",
                      headers=auth_header("root")).status_code == 200

    response = client.delete("/v1/admin/admins/carol@saga.xyz", headers=auth_header("root"))
    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMIN"
    assert "carol@saga.xyz" in _roles(db)
    assert _actions(db, "remove") == []

    # Removing yourself is refused the same way.
    response = client.delete("/v1/admin/admins/carol@saga.xyz", headers=auth_header("carol"))
    assert response.json()["code"] == "LAST_ADMIN"


def test_the_owner_cannot_be_demoted_by_anyone_else(db):
    client = _client(db, platform_owner=OWNER)
    assert client.put("/v1/admin/admins/carol@saga.xyz",
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
    assert client.put("/v1/admin/admins/carol@saga.xyz",
                      headers=auth_header("root")).status_code == 200
    response = client.delete("/v1/admin/admins/carol@saga.xyz", headers=auth_header("owner"))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_non_admin_is_refused(db, method):
    client = _client(db, platform_owner=OWNER)
    response = getattr(client, method)("/v1/admin/admins/alice@saga.xyz",
                                       headers=auth_header("alice"))
    assert response.status_code == 403
    assert "alice@saga.xyz" not in _roles(db)
    assert _actions(db, "grant") == [] and _actions(db, "remove") == []


@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_pool_admin_is_refused(db, method):
    client = _client(db, admin_pool_users=("gate@saga.xyz",))
    response = getattr(client, method)("/v1/admin/admins/gate@saga.xyz",
                                       headers=auth_header("gate"))
    assert response.status_code == 403
    assert "gate@saga.xyz" not in _roles(db)


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
    client = _client(db, admin_users=("carol@saga.xyz",))
    # The migration gave carol a document; take it away, keep ADMIN_USERS.
    assert client.put("/v1/admin/admins/dave@saga.xyz",
                      headers=auth_header("root")).status_code == 200
    assert client.delete("/v1/admin/admins/carol@saga.xyz",
                         headers=auth_header("root")).status_code == 200
    response = client.delete("/v1/admin/admins/carol@saga.xyz", headers=auth_header("root"))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_AN_ADMIN_DOCUMENT"
    assert "ADMIN_USERS" in response.json()["message"]


# -- configuration is the fallback ---------------------------------------------


class FailingRoles:
    """Firestore does not answer the role read."""

    def holds_admin(self, email: str) -> bool:
        raise RuntimeError("firestore unavailable")


def test_config_admins_and_the_owner_are_admins_with_firestore_down(db):
    client = _client(db, platform_owner=OWNER, admin_users=("carol@saga.xyz",))
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
    db.document(f"{ROLES_COLLECTION}/dave@saga.xyz").set(
        {"role": "admin", "granted_by": "root@saga.xyz", "granted_at": None})
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
    assert client.put("/v1/admin/admins/carol@saga.xyz",
                      headers=auth_header("owner")).status_code == 200
    body = client.get("/v1/admin/people", headers=auth_header("root")).json()
    assert body["admins_error"] is None
    assert [(a["email"], a["role"]) for a in body["admins"]] == [
        (OWNER, "owner"), ("carol@saga.xyz", "admin")]
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
