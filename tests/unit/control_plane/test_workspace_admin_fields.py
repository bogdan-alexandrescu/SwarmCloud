"""Renaming a workspace and naming a new workspace's first admin
(WORKSPACE-ADMIN-FIELDS, part of #2157; docs/web-ui/mockups/config-e.html
S6a and S6c, docs/design/workspace-roles.md §4.6 and §8).

    PUT  /v1/workspaces/{tenant_id}/name   {display_name}   its admins and platform admins
    POST /v1/admin/workspaces              ... first_admin?  a platform admin's

Who is who, in eng (`eng@example.com`) unless said otherwise:

* `alice`, `bob` -- members of eng with no grant (so `member`);
* `erin` -- a member of research only, named research's admin in a test;
* `ops` -- a platform admin in no tenant group (reaches eng through
  `platform_admin_reach`);
* `root` -- a platform admin who is also in eng;
* `carol` -- in no group: her tenant is her personal workspace `u-carol`.

What these hold: a rename changes the DISPLAY NAME and never the id; only the
workspace's admins and platform admins may make it, and everyone else is
refused 403 with nothing written -- another workspace's admin included, and an
id that names nothing answers exactly as one that names someone else's; each
rename is one audit row (who, old name, new name). `first_admin` grants that
person `admin` in the new workspace and in no other, through the signed,
audited role-override write, and a bad address is refused before anything is
created.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import CloudIdentityGroups, StaticGroups
from swarm_api.judgment import settings_keys
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import (
    ADMIN_GROUP,
    ENG_GROUP,
    PROJECT,
    RESEARCH_GROUP,
    NoForgeTokens,
    api_settings,
    seed_tenant,
)
from .test_roles_store import Clock, Recording
from .test_workspace_kinds_record import (
    SIGNING_VERSION,
    FakePublisher,
    InProcessSigner,
    RecordingDirectory,
)

GROUPS = {
    "alice@example.com": (ENG_GROUP,),
    "bob@example.com": (ENG_GROUP,),
    "erin@example.com": (RESEARCH_GROUP,),
    "ops@example.com": (ADMIN_GROUP,),
    "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    "carol@example.com": (),
}
RENAMED = "workspace_renamed"
#: The role-grant MAC key, made at run time.
SETTINGS_KEY = secrets.token_hex(32)


@pytest.fixture
def db() -> Recording:
    return Recording()


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def client(db, publisher) -> TestClient:
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    signer = InProcessSigner()
    context = build_context(
        settings=api_settings(platform_admin_reach=True, settings_key=SETTINGS_KEY,
                              platform_owner="root@example.com",
                              spec_signing_key_version=SIGNING_VERSION,
                              spec_verify_keys=signer.verify_keys()),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(), now=Clock(),
    )
    app = create_app(context)
    app.state.workspace_publisher = publisher
    app.state.workspace_directory = CloudIdentityGroups(PROJECT, session=RecordingDirectory())
    app.state.workspace_approval_signer = signer
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return TestClient(app, raise_server_exceptions=False)


def h(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if tenant:
        headers["X-Swarm-Tenant"] = tenant
    return headers


def state(db) -> dict[str, Any]:
    """Everything but what the platform writes about the CALLER before any
    route runs: a platform admin's own `platform_admin_*` rows
    (workspace-roles §4.2.4) and the configured owner's seeded admin role."""
    return {k: dict(v) for k, v in db.docs.items()
            if not k.startswith("admin_roles/")
            and v.get("action") != "seed_owner"
            and not str(v.get("action", "")).startswith("platform_admin_")}


def rows(db, action: str) -> list[dict[str, Any]]:
    return [v for k, v in db.docs.items() if k.startswith("admin_audit/")
            and v.get("action") == action]


def grants(db) -> list[dict[str, Any]]:
    return [v for k, v in db.docs.items() if k.startswith(f"{roles.GRANTS_COLLECTION}/")]


def name_admin(client, who: str, tenant: str) -> None:
    response = client.put(f"/v1/admin/workspaces/{tenant}/roles/person", headers=h("ops"),
                          json={"email": who, "role": "admin"})
    assert response.status_code == 200, response.text


def rename(client, tenant: str, user: str, name: Any, **extra: Any):
    return client.put(f"/v1/workspaces/{tenant}/name", headers=h(user),
                      json={"display_name": name, **extra})


# --------------------------------------------------------------------------
# Rename (S6a)
# --------------------------------------------------------------------------

def test_a_workspace_admin_renames_their_workspace_and_its_id_is_unchanged(client, db):
    name_admin(client, "alice@example.com", "eng")
    response = rename(client, "eng", "alice", "  Example Team  ")
    assert response.status_code == 200, response.text
    assert response.json() == {"tenant_id": "eng", "display_name": "Example Team",
                               "previous_display_name": "eng", "changed": True}
    stored = db.docs["tenants/eng"]
    assert stored["display_name"] == "Example Team"
    assert stored["tenant_id"] == "eng" and stored["principal"] == "eng@example.com"
    assert "tenants/example-team" not in db.docs
    # The shell's header and switcher draw `/v1/tenants/me`'s name.
    me = client.get("/v1/tenants/me", headers=h("bob", "eng")).json()["tenant"]
    assert (me["tenant_id"], me["display_name"]) == ("eng", "Example Team")


def test_a_rename_is_one_audit_row_naming_who_the_old_and_the_new_name(client, db):
    name_admin(client, "alice@example.com", "eng")
    assert rename(client, "eng", "alice", "Example Team").status_code == 200
    (row,) = rows(db, RENAMED)
    assert row["by"] == "alice@example.com"
    assert row["target_tenant_id"] == "eng"
    assert row["detail"]["from"] == "eng" and row["detail"]["to"] == "Example Team"
    assert row["detail"]["via"] == "workspace_admin"


def test_a_platform_admin_renames_a_workspace_they_are_not_in(client, db):
    response = rename(client, "eng", "ops", "Engineering")
    assert response.status_code == 200, response.text
    assert db.docs["tenants/eng"]["display_name"] == "Engineering"
    (row,) = rows(db, RENAMED)
    assert row["by"] == "ops@example.com" and row["detail"]["via"] == "platform_admin"


def test_the_record_of_a_job_made_workspace_is_renamed_with_its_tenant(client, db):
    db.docs["workspaces/eng"] = {"tenant_id": "eng", "workspace_id": "w-0e0e", "kind": "group",
                                 "principal": "eng@example.com", "display_name": "eng"}
    assert rename(client, "eng", "root", "Engineering").status_code == 200
    assert db.docs["workspaces/eng"]["display_name"] == "Engineering"
    assert db.docs["workspaces/eng"]["workspace_id"] == "w-0e0e"


def test_the_owner_renames_their_personal_workspace(client, db):
    assert client.get("/v1/tenants/me", headers=h("carol")).status_code == 200
    response = rename(client, "u-carol", "carol", "Carol's sandbox")
    assert response.status_code == 200, response.text
    assert db.docs["tenants/u-carol"]["display_name"] == "Carol's sandbox"


def test_a_member_is_refused_403_and_nothing_is_written(client, db):
    before = state(db)
    response = rename(client, "eng", "bob", "Bob's now")
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_admin_required"
    assert state(db) == before


def test_a_viewer_is_refused_403_and_nothing_is_written(client, db):
    response = client.put("/v1/admin/workspaces/eng/roles/person", headers=h("ops"),
                          json={"email": "bob@example.com", "role": "viewer"})
    assert response.status_code == 200, response.text
    before = state(db)
    response = rename(client, "eng", "bob", "Viewer's name")
    assert response.status_code == 403, response.text
    assert state(db) == before


def test_another_workspaces_admin_is_refused_403_and_nothing_is_written(client, db):
    name_admin(client, "erin@example.com", "research")
    assert rename(client, "research", "erin", "Research").status_code == 200
    before = state(db)
    for target in ("eng", "u-carol", "no-such-workspace"):
        response = rename(client, target, "erin", "Taken over")
        assert response.status_code == 403, (target, response.text)
        assert response.json()["code"] == "role_admin_required"
    # The header that selects eng is not a way in either: she is not in it.
    response = client.put("/v1/workspaces/eng/name", headers=h("erin", "eng"),
                          json={"display_name": "Taken over"})
    assert response.status_code == 403, response.text
    assert state(db) == before


def test_the_body_names_only_the_name(client, db):
    before = state(db)
    for extra in ({"tenant_id": "research"}, {"principal": "x@example.com"}):
        response = rename(client, "eng", "root", "Fine", **extra)
        assert response.status_code == 422, (extra, response.text)
    assert state(db) == before


@pytest.mark.parametrize("name", [
    "",
    "   ",
    "x" * 61,
    "bell\x07name",
    "new\nline",
    "zero​width",
    "right‮left",
    "Your workspace",
    " your  WORKSPACE ",
    42,
])
def test_a_bad_name_is_refused_422_and_nothing_is_written(client, db, name):
    before = state(db)
    response = rename(client, "eng", "root", name)
    assert response.status_code == 422, response.text
    assert state(db) == before


def test_a_name_of_sixty_characters_with_accents_is_accepted(client, db):
    name = "Équipe données " + "é" * 45
    assert len(name) == 60
    assert rename(client, "eng", "root", name).status_code == 200
    assert db.docs["tenants/eng"]["display_name"] == name


def test_the_same_name_again_changes_nothing_and_audits_nothing(client, db):
    assert rename(client, "eng", "root", "Engineering").status_code == 200
    before = state(db)
    again = rename(client, "eng", "root", "Engineering")
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False
    assert state(db) == before
    assert len(rows(db, RENAMED)) == 1


def test_a_workspace_with_no_tenant_document_is_404_to_its_platform_admin(client, db):
    del db.docs["tenants/research"]
    before = state(db)
    response = rename(client, "research", "ops", "Research")
    assert response.status_code == 404, response.text
    assert state(db) == before


def test_the_rename_is_an_admin_write_in_the_role_table():
    assert roles.required_role("PUT", "/v1/workspaces/{tenant_id}/name") == roles.ADMIN


# --------------------------------------------------------------------------
# First admin on Add a new workspace (S6c)
# --------------------------------------------------------------------------

def _create(client, body: dict[str, Any], user: str = "root"):
    return client.post("/v1/admin/workspaces", headers=h(user), json=body)


def _created(db) -> dict[str, Any]:
    return {k: v for k, v in db.docs.items() if k.startswith(("workspaces/", "workspace_ids/"))}


def test_first_admin_is_granted_admin_in_the_new_workspace_and_audited(client, db, publisher):
    response = _create(client, {"kind": "group", "principal": "reports@example.com",
                                "display_name": "Reports", "first_admin": "Frank@Example.com"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["first_admin"] == {"email": "frank@example.com", "role": "admin",
                                   "granted": True, "member": False}
    (grant,) = grants(db)
    assert (grant["tenant_id"], grant["subject"], grant["role"]) == (
        "reports", "frank@example.com", "admin")
    assert grant["granted_by"] == "root@example.com" and grant["via"] == "platform_admin"
    assert roles.grant_verifies(settings_keys(SETTINGS_KEY), grant)
    (row,) = rows(db, roles.AUDIT_GRANTED)
    assert row["target_tenant_id"] == "reports"
    assert row["target_email"] == "frank@example.com"
    assert row["by"] == "root@example.com"
    assert row["detail"]["first_admin"] is True and row["detail"]["to"] == "admin"


def test_first_admin_is_admin_there_and_nowhere_else(client, db):
    assert _create(client, {"kind": "group", "principal": "reports@example.com",
                            "first_admin": "alice@example.com"}).status_code == 202
    store = client.app.state.ctx.roles
    assert store.person_role("reports", "alice@example.com", fresh=True).role == roles.ADMIN
    assert store.person_role("eng", "alice@example.com", fresh=True).role == roles.MEMBER
    assert {g["tenant_id"] for g in grants(db)} == {"reports"}
    # ...and she cannot rename eng on the strength of it.
    assert rename(client, "eng", "alice", "Mine now").status_code == 403


@pytest.mark.parametrize("address", [
    "not-an-email",
    "two@@example.com",
    "a b@example.com",
    "robot@proj-123456" + ".iam.gserviceaccount.com",
    "x" * 250 + "@example.com",
])
def test_a_bad_first_admin_is_refused_before_anything_is_created(client, db, publisher,
                                                                  address):
    before = state(db)
    response = _create(client, {"kind": "group", "principal": "reports@example.com",
                                "first_admin": address})
    assert response.status_code in (403, 422), response.text
    assert _created(db) == {}
    assert grants(db) == []
    assert state(db) == before
    assert publisher.messages == []


@pytest.mark.parametrize("kind,principal", [
    ("personal", "carol@example.com"),
    ("service", f"reports-bot@{PROJECT}.iam.gserviceaccount.com"),
])
def test_first_admin_is_for_a_group_workspace_only(client, db, kind, principal):
    before = state(db)
    response = _create(client, {"kind": kind, "principal": principal,
                                "first_admin": "alice@example.com"})
    assert response.status_code == 422, response.text
    assert state(db) == before


def test_a_non_admin_cannot_name_a_first_admin(client, db):
    before = state(db)
    for user in ("alice", "erin", "carol"):
        response = _create(client, {"kind": "group", "principal": "reports@example.com",
                                    "first_admin": f"{user}@example.com"}, user=user)
        assert response.status_code == 403, response.text
    assert state(db) == before


def test_without_first_admin_no_role_is_written(client, db):
    response = _create(client, {"kind": "group", "principal": "reports@example.com"})
    assert response.status_code == 202, response.text
    assert response.json().get("first_admin") is None
    assert grants(db) == []


def test_first_admin_on_a_ready_workspace_grants_nothing(client, db):
    assert _create(client, {"kind": "group", "principal": "reports@example.com"}).status_code == 202
    db.docs["workspaces/reports"]["state"] = "ready"
    response = _create(client, {"kind": "group", "principal": "reports@example.com",
                                "first_admin": "alice@example.com"})
    assert response.status_code == 200, response.text
    assert response.json()["first_admin"] == {
        "email": "alice@example.com", "role": "admin", "granted": False,
        "reason": "workspace_exists"}
    assert grants(db) == []


def test_a_repeated_request_while_it_is_being_created_grants_once(client, db):
    body = {"kind": "group", "principal": "reports@example.com", "first_admin": "alice@example.com"}
    assert _create(client, body).status_code == 202
    again = _create(client, body)
    assert again.status_code == 200, again.text
    assert again.json()["first_admin"]["granted"] is True
    assert len(grants(db)) == 1
    assert len(rows(db, roles.AUDIT_GRANTED)) == 1
