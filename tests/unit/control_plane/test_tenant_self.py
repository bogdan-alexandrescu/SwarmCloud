"""A person's own personal workspace is their DEFAULT tenant; their groups are
what they switch to (#1200, owner decision 2026-10-12 15:55 PDT).

Invariant 9 (per-tenant isolation) is the risk, so most of this file is the
attacker's side. The personal tenant offered is derived ONLY from the verified
caller's email (`tenant_id_for_user`), and only when its tenant document is
`kind: user` with that exact email as principal AND its workspace record is
`ready` with that same principal. `X-Swarm-Tenant` still selects and never
grants: another person's `u-` id, a near miss, a case variant, or a personal
tenant whose documents name somebody else is one 403, before any route body.

`alice` is in eng with a ready workspace; `dave` is in eng and research with a
ready workspace; `erin` is in eng with none; `fay` is in eng with one still
`approved`; `gus` is in eng and his `u-gus` documents name gus@partner.com (the
frozen slug drops the domain, so that is a different person's tenant); `carol`
is in no group with a ready workspace; `hank` is in no group with none.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import TENANT_HEADER, Authenticator, PersonalTenants, StaticTokenVerifier
from swarm_api.errors import Forbidden, UpstreamUnavailable
from swarm_api.groups import StaticGroups

from .conftest import ENG_GROUP, RESEARCH_GROUP, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore

SVC = "ci-bot@saga-agents-staging.iam.gserviceaccount.com"


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        "alice@example.com": (ENG_GROUP,),
        "dave@example.com": (ENG_GROUP, RESEARCH_GROUP),
        "erin@example.com": (ENG_GROUP,),
        "fay@example.com": (ENG_GROUP,),
        "gus@example.com": (ENG_GROUP,),
        "carol@example.com": (),
        "hank@example.com": (),
    }


@pytest.fixture
def tokens(group_map) -> dict[str, dict[str, Any]]:
    return {
        f"token-{email.split('@')[0]}": {
            "email": email, "email_verified": True, "sub": f"sub-{email}", "hd": "example.com",
        }
        for email in group_map
    }


def seed_personal(db: FakeFirestore, user: str, *, state: str = "ready",
                  principal: str | None = None, kind: str = "user",
                  record_principal: str | None = None) -> None:
    """`tenants/u-<user>` and `workspaces/u-<user>`, as the workspace job leaves them."""
    tenant_id = f"u-{user}"
    owner = principal or f"{user}@example.com"
    seed_tenant(db, tenant_id)
    db.docs[f"tenants/{tenant_id}"].update(kind=kind, principal=owner, display_name=owner)
    db.docs[f"workspaces/{tenant_id}"] = {
        "tenant_id": tenant_id, "workspace_id": f"w-{user[:6]:0<6}",
        "principal": record_principal or owner, "state": state,
    }


@pytest.fixture
def seeded(db: FakeFirestore) -> FakeFirestore:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_personal(db, "alice")
    seed_personal(db, "dave")
    seed_personal(db, "fay", state="approved")
    seed_personal(db, "gus", principal="gus@partner.com")
    seed_personal(db, "carol")
    return db


def as_(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = auth_header(user)
    if tenant is not None:
        headers[TENANT_HEADER] = tenant
    return headers


def submit(client: TestClient, user: str, tenant: str | None = None) -> Any:
    body = {"runner_profile": "mock", "input": {"prompt": f"hello from {user}"}}
    return client.post("/v1/tasks", headers=as_(user, tenant), json=body)


def mine(client: TestClient, user: str, tenant: str | None = None) -> list[dict[str, str]]:
    response = client.get("/v1/tenants/mine", headers=as_(user, tenant))
    assert response.status_code == 200, response.text
    listed = response.json()
    # The first entry is the default and says why (docs/design/user-profile.md
    # §4.5); test_default_workspace.py holds those fields. These tests are
    # about which entries are listed and in what order.
    assert [e.pop("is_default") for e in listed] == [i == 0 for i in range(len(listed))]
    for entry in listed[:1]:
        assert entry.pop("default_source") in ("personal", "first_group")
    # Each entry's `via` and `role` (docs/design/workspace-roles.md §4.3) are
    # test_roles_api.py's; here they only have to agree with the entry's kind.
    for entry in listed:
        via = entry.pop("via")
        role = entry.pop("role")
        assert via == ("personal" if entry["kind"] == "user" else "member"), entry
        assert role["source"] in ("personal", "default", "platform_admin"), role
    return listed


def own(user: str) -> dict[str, str]:
    return {"tenant_id": f"u-{user}", "display_name": f"Your workspace (u-{user})", "kind": "user"}


ENG = {"tenant_id": "eng", "display_name": ENG_GROUP, "kind": "group"}
RESEARCH = {"tenant_id": "research", "display_name": RESEARCH_GROUP, "kind": "group"}


# -- the list ------------------------------------------------------------------

def test_a_group_member_with_a_ready_workspace_has_it_listed_first(client, seeded):
    assert mine(client, "alice") == [own("alice"), ENG]
    assert mine(client, "dave") == [own("dave"), ENG, RESEARCH]


def test_the_list_does_not_depend_on_which_tenant_is_selected(client, seeded):
    """The console reads /mine under the switcher's header; the list stays whole."""
    assert mine(client, "dave", "research") == mine(client, "dave", "u-dave") == mine(client, "dave")


def test_no_ready_workspace_is_not_listed(client, seeded):
    assert mine(client, "erin") == [ENG]      # no record at all
    assert mine(client, "fay") == [ENG]       # approved, not yet ready
    assert mine(client, "gus") == [ENG]       # u-gus is gus@partner.com's


@pytest.mark.parametrize("state", ["requested", "approved", "applying", "needs_owner",
                                   "denied", "failed", "", "READY"])
def test_only_a_ready_workspace_is_listed(client, db, state):
    seed_personal(db, "alice", state=state)
    assert mine(client, "alice") == [ENG]


def test_a_group_kind_document_at_the_personal_id_is_not_offered(client, db):
    seed_personal(db, "alice", kind="group")
    assert mine(client, "alice") == [ENG]


def test_a_workspace_record_naming_someone_else_is_not_offered(client, db):
    seed_personal(db, "alice", record_principal="alice@partner.com")
    assert mine(client, "alice") == [ENG]


def test_a_service_identity_is_never_offered_a_personal_tenant(group_map, db):
    """CI and the acceptance callers are service accounts: their headerless
    default stays the group even if a record at their slug says ready.

    Naming it is another matter since HW-ACCEPT-ID (owner decision
    2026-10-19): a service account may select its OWN `kind: user` tenant
    whose principal is exactly its address -- test_service_own_tenant.py
    holds that path -- and the selection is never its default."""
    seed_personal(db, "ci-bot", principal=SVC)
    claims = {"t-svc": {"email": SVC, "email_verified": True, "sub": "s-svc"}}
    auth = Authenticator(api_settings(allowed_users=(SVC,)), StaticTokenVerifier(claims),
                         StaticGroups({**group_map, SVC: (ENG_GROUP,)}),
                         personal=PersonalTenants(db))
    ctx = auth.authenticate("Bearer t-svc")
    assert ctx.tenant_id == "eng"
    assert ctx.tenant_choices == (("eng", ENG_GROUP),)
    named = auth.authenticate("Bearer t-svc", None, tenant="u-ci-bot")
    assert (named.tenant_id, named.tenant_principal) == ("u-ci-bot", SVC)
    assert auth.authenticate("Bearer t-svc").tenant_id == "eng"
    # Never as a person's ready workspace: a document naming someone else is refused.
    db.docs["tenants/u-ci-bot"]["principal"] = "ci-bot@other-project.iam.gserviceaccount.com"
    fresh = Authenticator(api_settings(allowed_users=(SVC,)), StaticTokenVerifier(claims),
                          StaticGroups({**group_map, SVC: (ENG_GROUP,)}),
                          personal=PersonalTenants(db))
    with pytest.raises(Forbidden):
        fresh.authenticate("Bearer t-svc", None, tenant="u-ci-bot")


def test_a_personal_only_caller_with_a_ready_workspace_lists_it(client, seeded):
    assert mine(client, "carol") == [own("carol")]
    assert mine(client, "hank") == []


# -- the default ---------------------------------------------------------------

def test_with_a_ready_workspace_the_default_is_the_personal_tenant(client, seeded):
    response = submit(client, "alice")
    assert response.status_code == 201, response.text
    assert response.json()["task"]["tenant_id"] == "u-alice"
    assert submit(client, "dave").json()["task"]["tenant_id"] == "u-dave"


def test_the_header_still_selects_a_group(client, seeded):
    assert submit(client, "alice", "eng").json()["task"]["tenant_id"] == "eng"
    assert submit(client, "dave", "research").json()["task"]["tenant_id"] == "research"


def test_without_a_ready_workspace_the_default_is_still_the_first_group(client, seeded):
    for user in ("erin", "fay", "gus"):
        response = submit(client, user)
        assert response.status_code == 201, (user, response.text)
        assert response.json()["task"]["tenant_id"] == "eng", user


def test_a_personal_only_caller_is_unchanged(client, seeded):
    for user in ("carol", "hank"):
        assert submit(client, user).json()["task"]["tenant_id"] == f"u-{user}", user


def test_me_reports_the_personal_tenant_by_default(client, seeded):
    body = client.get("/v1/tenants/me", headers=as_("alice")).json()
    assert body["tenant"]["tenant_id"] == "u-alice"
    assert body["tenant"]["kind"] == "user"
    # The caller's groups are about the caller and are unchanged.
    assert body["principal"]["groups"] == [ENG_GROUP]


# -- the header ------------------------------------------------------------------

def test_the_header_selects_the_callers_own_ready_workspace(client, seeded):
    response = submit(client, "alice", "u-alice")
    assert response.status_code == 201, response.text
    assert response.json()["task"]["tenant_id"] == "u-alice"


def test_the_callers_own_workspace_reads_are_scoped_to_it(client, seeded):
    personal = submit(client, "dave", "u-dave").json()["task"]["id"]
    group = submit(client, "dave", "eng").json()["task"]["id"]
    listed = {t["id"] for t in client.get("/v1/tasks", headers=as_("dave")).json()["tasks"]}
    assert personal in listed and group not in listed
    # alice is in eng too; dave's personal task is not hers to read.
    assert client.get(f"/v1/tasks/{personal}", headers=as_("alice", "eng")).status_code == 404
    assert client.get(f"/v1/tasks/{group}", headers=as_("alice", "eng")).status_code == 200


@pytest.mark.parametrize("user,header", [
    ("alice", "u-dave"),        # another person's ready workspace
    ("alice", "u-carol"),       # a personal-only person's
    ("erin", "u-erin"),         # own, but no workspace
    ("fay", "u-fay"),           # own, approved not ready
    ("gus", "u-gus"),           # the id derives from gus's email; the tenant is not his
    ("alice", "U-alice"),       # case
    ("alice", "u-alice "),      # trailing space
    ("alice", "u-alice@example.com"),
    ("alice", "alice@example.com"),
    ("alice", "alice"),
    ("hank", "u-hank"),         # personal-only, no workspace: unchanged
])
def test_anything_but_the_callers_own_ready_workspace_is_403(client, seeded, user, header):
    before = dict(seeded.docs)
    response = submit(client, user, header)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "tenant_not_member"
    assert seeded.docs == before


# -- the authenticator, directly -----------------------------------------------

def _authenticator(group_map, db, **kw) -> Authenticator:
    claims = {
        f"t-{email.split('@')[0]}": {"email": email, "email_verified": True, "sub": f"s-{email}"}
        for email in group_map
    }
    return Authenticator(api_settings(), StaticTokenVerifier(claims), StaticGroups(group_map),
                         personal=PersonalTenants(db, **kw))


def test_the_personal_choice_carries_the_callers_email_as_principal(group_map, seeded):
    auth = _authenticator(group_map, seeded)
    ctx = auth.authenticate("Bearer t-dave")
    assert ctx.tenant_id == "u-dave"
    assert ctx.tenant_principal == "dave@example.com"
    assert ctx.tenant_choices == (("u-dave", "dave@example.com"), ("eng", ENG_GROUP),
                                  ("research", RESEARCH_GROUP))
    # Selecting it explicitly is the same context as not selecting.
    assert auth.authenticate("Bearer t-dave", None, tenant="u-dave") == ctx
    chosen = auth.authenticate("Bearer t-dave", None, tenant="eng")
    assert (chosen.tenant_id, chosen.tenant_principal) == ("eng", ENG_GROUP)
    assert chosen.tenant_choices == ctx.tenant_choices


def test_admins_gain_nothing(group_map, seeded):
    gm = {**group_map, "root@example.com": ("swarm-admins@example.com", ENG_GROUP)}
    auth = _authenticator(gm, seeded)
    root = auth.authenticate("Bearer t-root")
    assert root.is_admin is True
    assert root.tenant_id == "eng"   # no workspace of his own
    for header in ("u-alice", "u-root"):
        with pytest.raises(Forbidden):
            auth.authenticate("Bearer t-root", None, tenant=header)


def test_a_refused_header_reads_nothing_for_the_personal_tenant(group_map):
    """A header naming neither a group the caller is in nor their own personal
    id is refused before the personal documents are asked about."""
    class Exploding:
        def collection(self, path: str):
            raise AssertionError(f"read {path}")

    claims = {"t-alice": {"email": "alice@example.com", "email_verified": True, "sub": "s"}}
    auth = Authenticator(api_settings(), StaticTokenVerifier(claims), StaticGroups(group_map),
                         personal=PersonalTenants(Exploding()))
    with pytest.raises(Forbidden):
        auth.authenticate("Bearer t-alice", None, tenant="u-dave")
    with pytest.raises(Forbidden):
        auth.authenticate("Bearer t-alice", None, tenant="research")


def test_an_unreadable_workspace_is_a_503_never_a_guess(group_map):
    """Falling back to the group would run the caller's work in a DIFFERENT
    tenant from the one they act in by default; a 503 is recoverable."""
    class Broken:
        def collection(self, path: str):
            raise RuntimeError("firestore unavailable")

    claims = {"t-alice": {"email": "alice@example.com", "email_verified": True, "sub": "s"}}
    auth = Authenticator(api_settings(), StaticTokenVerifier(claims), StaticGroups(group_map),
                         personal=PersonalTenants(Broken()))
    with pytest.raises(UpstreamUnavailable):
        auth.authenticate("Bearer t-alice")
    with pytest.raises(UpstreamUnavailable):
        auth.authenticate("Bearer t-alice", None, tenant="u-alice")
    # Naming a group needs no answer about the workspace: nothing can be misfiled.
    assert auth.authenticate("Bearer t-alice", None, tenant="eng").tenant_id == "eng"


def test_a_ready_answer_is_cached_briefly_and_then_read_again(group_map, db):
    seed_personal(db, "alice")
    clock = [0.0]
    auth = _authenticator(group_map, db, cache_seconds=30.0, clock=lambda: clock[0])
    assert auth.authenticate("Bearer t-alice").tenant_id == "u-alice"
    db.docs["workspaces/u-alice"]["state"] = "applying"
    clock[0] = 29.0
    assert auth.authenticate("Bearer t-alice").tenant_id == "u-alice"
    clock[0] = 31.0
    assert auth.authenticate("Bearer t-alice").tenant_id == "eng"


def test_a_not_ready_answer_is_never_cached(group_map, db):
    """E2E-3: the console polls while the workspace is built; the request that
    follows the build is admitted at once, not after the cache window."""
    seed_personal(db, "alice", state="applying")
    clock = [0.0]
    auth = _authenticator(group_map, db, cache_seconds=30.0, clock=lambda: clock[0])
    assert auth.authenticate("Bearer t-alice").tenant_id == "eng"
    with pytest.raises(Forbidden):
        auth.authenticate("Bearer t-alice", None, tenant="u-alice")
    db.docs["workspaces/u-alice"]["state"] = "ready"
    assert auth.authenticate("Bearer t-alice", None, tenant="u-alice").tenant_id == "u-alice"
    assert auth.authenticate("Bearer t-alice").tenant_id == "u-alice"


def test_an_absent_workspace_is_read_again_and_isolation_holds(group_map, db):
    """No record at all is not cached either; and another person's ready
    workspace never makes this caller's id admissible."""
    seed_personal(db, "bob")
    clock = [0.0]
    auth = _authenticator(group_map, db, cache_seconds=30.0, clock=lambda: clock[0])
    with pytest.raises(Forbidden):
        auth.authenticate("Bearer t-alice", None, tenant="u-alice")
    with pytest.raises(Forbidden):
        auth.authenticate("Bearer t-alice", None, tenant="u-bob")
    seed_personal(db, "alice")
    assert auth.authenticate("Bearer t-alice", None, tenant="u-alice").tenant_id == "u-alice"
    with pytest.raises(Forbidden):
        auth.authenticate("Bearer t-alice", None, tenant="u-bob")


def test_no_reader_is_todays_behaviour(group_map, seeded):
    claims = {"t-alice": {"email": "alice@example.com", "email_verified": True, "sub": "s"}}
    auth = Authenticator(api_settings(), StaticTokenVerifier(claims), StaticGroups(group_map))
    ctx = auth.authenticate("Bearer t-alice")
    assert ctx.tenant_id == "eng" and ctx.tenant_choices == (("eng", ENG_GROUP),)


def test_the_people_sighting_lists_groups_only(group_map, seeded):
    """`people/u-*.teams` is the groups a person is in, not their own workspace."""
    from swarm_api.workspaces import Workspaces

    ctx = _authenticator(group_map, seeded).authenticate("Bearer t-dave")
    Workspaces(seeded, now=lambda: __import__("datetime").datetime(2026, 10, 12), gate=False) \
        .touch_person(ctx)
    assert seeded.docs["people/u-dave"]["teams"] == ["eng", "research"]


def test_a_cached_context_is_not_shared_between_callers(group_map, seeded):
    auth = _authenticator(group_map, seeded)
    a = auth.authenticate("Bearer t-alice")
    e = auth.authenticate("Bearer t-erin")
    assert a.tenant_id == "u-alice" and e.tenant_id == "eng"
    assert replace(a, tenant_choices=()) != replace(e, tenant_choices=())
