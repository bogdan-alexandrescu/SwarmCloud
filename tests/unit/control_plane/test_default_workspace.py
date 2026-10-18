"""The stored default workspace applies in authentication (PROFILE-DEFAULT).

docs/design/user-profile.md §4.5 and Q9/Q11 of §7.1. A person's
`user_preferences` document names a default workspace; `Authenticator`
moves it to the front of `tenant_choices` when it is one of them, so a
request that names no workspace acts in it and `GET /v1/tenants/mine` lists
it first. An explicit `X-Swarm-Tenant` still wins. A document that cannot be
read is a 503 `preferences_unavailable` for a request naming no workspace --
never the computed default -- except on the three routes a person needs to
pick one.

`dave` is in eng and research (eng first in the admin order) with a ready
personal workspace; `alice` is in eng only; `carol` is in no group.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import (
    PREFERENCE_FREE_ROUTES,
    TENANT_HEADER,
    Authenticator,
    PersonalTenants,
    StaticTokenVerifier,
    require_continuation_route,
)
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import PreferencesUnavailable
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.preferences import COLLECTION, Preferences
from swarm_api.waker import NullWaker

from .conftest import ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore
from .test_continuation_scope_is_narrow import SWEPT

SVC = "ci-bot@saga-agents-staging.iam.gserviceaccount.com"

GROUPS: dict[str, tuple[str, ...]] = {
    "alice@saga.xyz": (ENG_GROUP,),
    "carol@saga.xyz": (),
    "dave@saga.xyz": (ENG_GROUP, RESEARCH_GROUP),
    SVC: (ENG_GROUP,),
}


class FlakyPreferencesFirestore(FakeFirestore):
    """A Firestore whose `user_preferences` reads fail while `failing` is set,
    and which counts them, so "unreadable" and "never read" are measurable.
    Every other read is the fake's."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = False
        self.preference_reads = 0

    def get_all(self, references, field_paths=None, transaction=None):  # type: ignore[override]
        references = list(references)
        if any(ref.path.startswith(COLLECTION + "/") for ref in references):
            self.preference_reads += 1
            if self.failing:
                raise RuntimeError("firestore unavailable")
        return super().get_all(references, field_paths, transaction)


def _claims(email: str, **extra: Any) -> dict[str, Any]:
    return {"email": email, "email_verified": True, "sub": f"sub-{email}", **extra}


def _tokens() -> dict[str, dict[str, Any]]:
    tokens = {f"token-{email.split('@')[0]}": _claims(email) for email in GROUPS}
    tokens["token-ci-bot"] = _claims(SVC)
    return tokens


def seed_personal(db: FakeFirestore, user: str) -> None:
    """`tenants/u-<user>` and `workspaces/u-<user>`, ready, as the job leaves them."""
    tenant_id = f"u-{user}"
    owner = f"{user}@saga.xyz"
    seed_tenant(db, tenant_id)
    db.docs[f"tenants/{tenant_id}"].update(kind="user", principal=owner, display_name=owner)
    db.docs[f"workspaces/{tenant_id}"] = {
        "tenant_id": tenant_id, "workspace_id": f"w-{user[:6]:0<6}",
        "principal": owner, "state": "ready",
    }


@pytest.fixture
def db() -> FlakyPreferencesFirestore:
    db = FlakyPreferencesFirestore()
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_personal(db, "dave")
    return db


@pytest.fixture
def prefs(db) -> Preferences:
    # No cache, so a test that flips `failing` sees the next read fail.
    return Preferences(db, [secrets.token_hex(32)], ttl=0.0)


def authenticator(db, prefs, tokens: dict[str, dict[str, Any]] | None = None) -> Authenticator:
    settings = api_settings(allowed_users=(SVC,))
    return Authenticator(
        settings, StaticTokenVerifier(tokens or _tokens()), StaticGroups(GROUPS),
        personal=PersonalTenants(db, cache_seconds=0), preferences=prefs,
    )


@pytest.fixture
def client(db, prefs) -> TestClient:
    ctx = build_context(
        settings=api_settings(allowed_users=(SVC,)), db=db,
        verifier=StaticTokenVerifier(_tokens()), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(),
    )
    # The deployed wiring is deps.build_context's; here the same authenticator
    # carries the preferences this lane applies.
    ctx.authenticator = authenticator(db, prefs)
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def store_default(prefs: Preferences, user: str, workspace: str | None, *,
                  display_name: str | None = None) -> None:
    email = f"{user}@saga.xyz"
    prefs.save(email, display_name=display_name, default_workspace=workspace,
               muted=[], reminder="as_set", revision=prefs.read(email).revision)


def as_(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = auth_header(user)
    if tenant is not None:
        headers[TENANT_HEADER] = tenant
    return headers


def submit(client: TestClient, user: str, tenant: str | None = None) -> Any:
    body = {"runner_profile": "mock", "input": {"prompt": f"hello from {user}"}}
    return client.post("/v1/tasks", headers=as_(user, tenant), json=body)


def tasks(db: FakeFirestore) -> dict[str, dict[str, Any]]:
    return db.dump("tasks/")


def mine(client: TestClient, user: str, tenant: str | None = None) -> list[dict[str, Any]]:
    response = client.get("/v1/tenants/mine", headers=as_(user, tenant))
    assert response.status_code == 200, response.text
    return response.json()


def headerless_tenant(client: TestClient, user: str) -> str:
    response = client.get("/v1/tenants/me", headers=as_(user))
    assert response.status_code == 200, response.text
    return response.json()["tenant"]["tenant_id"]


def assert_mine_leads_with_the_headerless_tenant(client: TestClient, user: str) -> None:
    listed = mine(client, user)
    assert listed[0]["tenant_id"] == headerless_tenant(client, user)
    assert [e["is_default"] for e in listed] == [i == 0 for i in range(len(listed))]
    assert all("default_source" not in e for e in listed[1:])


# -- the stored default applies --------------------------------------------------

def test_a_headerless_request_acts_in_the_stored_group_default(client, prefs):
    assert submit(client, "dave").json()["task"]["tenant_id"] == "u-dave"  # today's
    store_default(prefs, "dave", "research")

    created = submit(client, "dave")
    assert created.status_code == 201, created.text
    assert created.json()["task"]["tenant_id"] == "research"
    listed = mine(client, "dave")
    assert [e["tenant_id"] for e in listed] == ["research", "u-dave", "eng"]
    assert listed[0]["default_source"] == "profile"
    assert_mine_leads_with_the_headerless_tenant(client, "dave")


def test_a_header_naming_the_personal_workspace_still_wins(client, prefs):
    store_default(prefs, "dave", "research")
    chosen = submit(client, "dave", "u-dave")
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["task"]["tenant_id"] == "u-dave"
    # The list does not depend on the header: the default is still first.
    assert mine(client, "dave", "u-dave")[0]["tenant_id"] == "research"


def test_a_default_the_person_left_is_ignored_and_kept(client, db, prefs):
    store_default(prefs, "alice", "research")  # alice is in eng only
    stored = db.dump(COLLECTION + "/")

    created = submit(client, "alice")
    assert created.status_code == 201, created.text
    assert created.json()["task"]["tenant_id"] == "eng"
    listed = mine(client, "alice")
    assert listed[0]["tenant_id"] == "eng"
    assert listed[0]["default_source"] == "first_group"
    assert listed[0]["default_ignored"] == "not_a_member"
    assert_mine_leads_with_the_headerless_tenant(client, "alice")
    assert db.dump(COLLECTION + "/") == stored  # untouched


def test_a_personal_default_that_is_not_ready_is_ignored_as_not_ready(db, prefs):
    store_default(prefs, "alice", "u-alice")  # no workspace record for alice
    ctx = authenticator(db, prefs).authenticate("Bearer token-alice")
    assert ctx.tenant_id == "eng"
    assert (ctx.default_source, ctx.default_ignored) == ("first_group", "not_ready")
    assert prefs.read("alice@saga.xyz").default_workspace == "u-alice"


def test_no_stored_default_is_todays_rule_and_says_so(client, db, prefs):
    assert mine(client, "dave")[0] == {
        "tenant_id": "u-dave", "display_name": "Your workspace (u-dave)", "kind": "user",
        "is_default": True, "default_source": "personal",
    }
    assert mine(client, "alice")[0]["default_source"] == "first_group"
    assert mine(client, "carol") == []
    for user in ("dave", "alice"):
        assert_mine_leads_with_the_headerless_tenant(client, user)


# -- the preferences cannot be read (Q9) ----------------------------------------

def test_an_unreadable_document_refuses_a_headerless_submission_with_503(client, db):
    db.failing = True
    before = tasks(db)

    refused = submit(client, "dave")
    assert refused.status_code == 503, refused.text
    body = refused.json()
    assert body["code"] == "preferences_unavailable"
    for how in ("workspace switcher", "SWARM_TENANT", "--tenant"):
        assert how in body["message"]
    assert tasks(db) == before  # nothing submitted


def test_an_unreadable_document_with_a_header_is_served_normally(client, db):
    db.failing = True
    chosen = submit(client, "dave", "research")
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["task"]["tenant_id"] == "research"
    listed = client.get("/v1/tasks", headers=as_("dave", "research"))
    assert listed.status_code == 200, listed.text


def test_the_preference_free_routes_are_exactly_the_three():
    assert PREFERENCE_FREE_ROUTES == frozenset({
        ("GET", "/v1/tenants/me"),
        ("GET", "/v1/tenants/mine"),
        ("GET", "/v1/profile"),
    })
    assert ("GET", "/v1/notifications") not in PREFERENCE_FREE_ROUTES


def test_the_preference_free_routes_are_served_with_the_default_unavailable(client, db, prefs):
    store_default(prefs, "dave", "research")
    db.failing = True

    me = client.get("/v1/tenants/me", headers=as_("dave"))
    assert me.status_code == 200, me.text
    listed = mine(client, "dave")
    # Today's order, not the stored default nobody could read.
    assert [e["tenant_id"] for e in listed] == ["u-dave", "eng", "research"]
    assert listed[0]["default_source"] == "unavailable"
    assert_mine_leads_with_the_headerless_tenant(client, "dave")

    # GET /v1/profile is PROFILE-API's route; the gate it meets is this one.
    ctx = authenticator(db, prefs).authenticate("Bearer token-dave")
    assert ctx.default_source == "unavailable" and ctx.default_unknown
    for route in sorted(PREFERENCE_FREE_ROUTES):
        assert require_continuation_route(ctx, route) is ctx


def test_every_other_route_refuses_a_headerless_request_it_cannot_place(client, db, prefs):
    db.failing = True
    ctx = authenticator(db, prefs).authenticate("Bearer token-dave")
    # Every authenticated route in every router, as the scope sweeps take them.
    others = sorted(set(SWEPT) - PREFERENCE_FREE_ROUTES)
    assert {("GET", "/v1/tenants/me"), ("GET", "/v1/tenants/mine")} <= set(SWEPT)
    assert ("GET", "/v1/notifications") in others and ("POST", "/v1/tasks") in others
    for route in [*others, None]:
        with pytest.raises(PreferencesUnavailable):
            require_continuation_route(ctx, route)
    bell = client.get("/v1/notifications", headers=as_("dave"))
    assert bell.status_code == 503 and bell.json()["code"] == "preferences_unavailable"


# -- the context ----------------------------------------------------------------

def test_the_context_holds_the_document_read_and_none_when_it_could_not_be_read(db, prefs):
    store_default(prefs, "dave", "research")
    auth = authenticator(db, prefs)

    ctx = auth.authenticate("Bearer token-dave")
    assert ctx.preferences == prefs.read("dave@saga.xyz")
    assert ctx.preferences is not None and ctx.preferences.default_workspace == "research"
    assert ctx.tenant_choices[0] == ("research", RESEARCH_GROUP)
    assert (ctx.tenant_id, ctx.tenant_principal) == ("research", RESEARCH_GROUP)

    db.failing = True
    unread = auth.authenticate("Bearer token-dave")
    assert unread.preferences is None
    named = auth.authenticate("Bearer token-dave", None, tenant="eng")
    assert named.preferences is None and not named.default_unknown
    assert named.tenant_id == "eng"


def test_a_person_without_a_document_reads_the_defaults(db, prefs):
    ctx = authenticator(db, prefs).authenticate("Bearer token-alice")
    assert ctx.preferences is not None and not ctx.preferences.exists
    assert ctx.default_source == "first_group" and not ctx.default_unknown


def test_a_service_identity_never_reads_preferences(db, prefs):
    db.failing = True  # a read would make this caller's request a 503
    ctx = authenticator(db, prefs).authenticate("Bearer token-ci-bot")
    assert db.preference_reads == 0
    assert ctx.preferences is None and not ctx.default_unknown
    assert ctx.tenant_id == "eng"
    assert require_continuation_route(ctx, ("POST", "/v1/tasks")) is ctx


def test_a_header_outside_the_choices_is_refused_without_reading_preferences(db, prefs):
    from swarm_api.errors import Forbidden

    with pytest.raises(Forbidden):
        authenticator(db, prefs).authenticate("Bearer token-alice", None, tenant="research")
    assert db.preference_reads == 0


# -- Q11 (a): who the commits name -----------------------------------------------

def test_the_display_name_is_the_stored_name_else_the_claim_else_the_local_part(db, prefs):
    tokens = {
        "token-dave": _claims("dave@saga.xyz", name="David From The Token"),
        "token-alice": _claims("alice@saga.xyz"),
    }
    auth = authenticator(db, prefs, tokens)

    assert auth.authenticate("Bearer token-dave").display_name == "David From The Token"
    assert auth.authenticate("Bearer token-alice").display_name == "alice"

    store_default(prefs, "dave", None, display_name="Dave Stored")
    assert auth.authenticate("Bearer token-dave").display_name == "Dave Stored"
    # A request naming its workspace is named the same.
    assert auth.authenticate("Bearer token-dave", None, tenant="eng").display_name == "Dave Stored"

    # Unreadable: the claim, then the local part -- never a guessed stored name.
    db.failing = True
    assert auth.authenticate("Bearer token-dave", None, tenant="eng").display_name == \
        "David From The Token"
