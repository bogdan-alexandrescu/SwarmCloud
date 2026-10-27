"""The membership store wired into a deployment's authenticator, approvals
writing through it, and `/v1/workspace/roles` naming a membership
selection (docs/design/join-requests.md §4.4, §4.5; lane JOIN-WIRE, part of
#2325; BGD's answers to JOIN-RESOLVE's questions 0 and 1, 2026-10-26).

Every app here is built by `deps.build_context`, the factory a deployment
uses, over the in-memory Firestore: before this lane it built the
authenticator with no membership store, so `PLATFORM_MEMBERSHIPS=true`
admitted nobody however the unit tests of the resolver came out.

What is held, per-tenant isolation (CONTRACT invariant 9) being the property
at stake:

* switch on, a person holding a valid membership is admitted to that
  workspace and to no other;
* switch off, no request reads `workspace_memberships` at all, and the same
  membership admits nobody;
* after an approval, the person's very next request selects the workspace:
  the approval's write evicts the cache the authenticator reads, not a
  second store's;
* `me.via` is "platform_member" for a membership selection and "member" for
  a Google-group member, and nothing else about the role differs: the same
  routes allow and refuse the two the same way.

Fixtures are JOIN-API's (`test_join_requests_routes`): `lee` is in no group
and asks to join eng; `bob` is in eng's Google group with no grant.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import memberships, refusals, roles
from swarm_api.memberships import MembershipStore

from .conftest import ENG_GROUP
from .test_join_requests_routes import (  # noqa: F401 - pytest fixtures
    LEE,
    approve,
    asked,
    client,
    ctx,
    db,
    h,
    switch_on,
)
from .test_roles_store import grant

MEMBER_SWITCH = refusals.env_name("role_member_required")
ADMIN_SWITCH = refusals.SWITCHES["role_admin_required"].env


def holds(ctx, subject: str = LEE, tenant_id: str = "eng") -> None:
    """A signed `member` grant written by a store of its own, as an earlier
    approval on another instance would have left it."""
    MembershipStore(ctx.db, ctx.settings).write(
        tenant_id=tenant_id, group=ENG_GROUP, subject=subject, state=memberships.MEMBER,
        by="alice@example.com", via=memberships.VIA_JOIN_REQUEST)


def membership_reads(db, monkeypatch) -> list[str]:
    """Every `workspace_memberships` collection opened from here on."""
    seen: list[str] = []
    real = db.collection

    def spy(path: str, *args: Any, **kwargs: Any):
        if path == memberships.COLLECTION:
            seen.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(db, "collection", spy)
    return seen


def code_of(response) -> str | None:
    try:
        return response.json().get("code")
    except ValueError:
        return None


# -- the wiring (deps.build_context) ------------------------------------------------

def test_the_factory_hands_the_authenticator_a_membership_store(ctx) -> None:
    # MUTATION: drop `memberships=` from deps' Authenticator(...) -> None.
    assert isinstance(ctx.authenticator.memberships, MembershipStore)


def test_switch_on_a_valid_membership_admits_to_that_workspace_and_no_other(client, ctx) -> None:
    switch_on(ctx)
    holds(ctx)
    response = client.get("/v1/workspace/roles", headers=h("lee", "eng"))
    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == "eng"
    refused = client.get("/v1/workspace/roles", headers=h("lee", "research"))
    assert refused.status_code == 403, refused.text
    assert code_of(refused) == "tenant_not_member"


def test_switch_off_reads_no_membership_and_the_same_one_admits_nobody(
        client, ctx, db, monkeypatch) -> None:
    holds(ctx)
    assert ctx.authenticator.memberships is not None
    reads = membership_reads(db, monkeypatch)
    refused = client.get("/v1/workspace/roles", headers=h("lee", "eng"))
    assert refused.status_code == 403, refused.text
    assert code_of(refused) == "tenant_not_member"
    mine = client.get("/v1/tenants/mine", headers=h("lee"))
    assert mine.status_code == 200, mine.text
    assert "eng" not in {c["tenant_id"] for c in mine.json()}
    assert client.get("/v1/workspace/roles", headers=h("bob", "eng")).status_code == 200
    assert reads == []


# -- an approval and the next request (§4.4) ----------------------------------------

def test_after_approval_the_next_request_selects_the_workspace_without_the_cache(
        client, ctx) -> None:
    switch_on(ctx)
    # The control: the cache is on, so a second store's write would leave
    # the authenticator's empty read standing for these 15 seconds.
    assert ctx.settings.role_cache_seconds > 0
    ref, _code = asked(client)
    before = client.get("/v1/workspace/roles", headers=h("lee", "eng"))
    assert before.status_code == 403 and code_of(before) == "tenant_not_member", before.text

    assert approve(client, "alice", ref).status_code == 200
    # MUTATION: write through `membership_store(ctx)` -> lee's cached empty
    # read answers, and this is 403.
    after = client.get("/v1/workspace/roles", headers=h("lee", "eng"))
    assert after.status_code == 200, after.text
    assert after.json()["me"]["via"] == "platform_member"


# -- the label (roles.tenant_via) ---------------------------------------------------

def test_me_via_is_platform_member_for_a_membership_and_member_for_a_group_member(
        client, ctx) -> None:
    switch_on(ctx)
    holds(ctx)
    lee = client.get("/v1/workspace/roles", headers=h("lee", "eng"))
    bob = client.get("/v1/workspace/roles", headers=h("bob", "eng"))
    assert lee.status_code == 200 and bob.status_code == 200, (lee.text, bob.text)
    # MUTATION: tenant_via() ignoring platform_memberships -> "member".
    assert lee.json()["me"]["via"] == "platform_member"
    assert bob.json()["me"]["via"] == roles.VIA_MEMBER
    # Only the label: the role, its source and what the page offers match.
    mine = {k: v for k, v in lee.json()["me"].items() if k not in ("email", "via")}
    theirs = {k: v for k, v in bob.json()["me"].items() if k not in ("email", "via")}
    assert mine == theirs
    assert lee.json()["can_manage"] is bob.json()["can_manage"] is False


#: Routes the roles gate decides, each with a body its route accepts, so the
#: answer is the role's (or the tenant's) and never the body's.
ROUTES: list[tuple[str, str, str, str, Any]] = [
    ("read roles", "GET", "/v1/workspace/roles", "eng", None),
    ("submit", "POST", "/v1/tasks", "eng", {"runner_profile": "mock"}),
    ("name an admin", "PUT", "/v1/workspace/roles/person", "eng",
     {"email": "kim@example.com", "role": "admin"}),
    ("join link", "PUT", "/v1/workspace/join-link", "eng", {"on": False}),
    ("members list", "GET", "/v1/workspace/members", "eng", None),
    ("another workspace", "GET", "/v1/workspace/roles", "research", None),
]


def answers(client, user: str) -> dict[str, tuple[int, str | None]]:
    out = {}
    for name, method, path, tenant, body in ROUTES:
        kwargs: dict[str, Any] = {"headers": h(user, tenant)}
        if body is not None:
            kwargs["json"] = body
        response = client.request(method, path, **kwargs)
        out[name] = (response.status_code, code_of(response))
    return out


@pytest.mark.parametrize("role", [None, roles.VIEWER], ids=["default", "viewer"])
def test_a_membership_members_allowed_and_refused_routes_are_a_group_members(
        client, ctx, monkeypatch, role) -> None:
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    monkeypatch.setenv(ADMIN_SWITCH, "on")
    switch_on(ctx)
    ref, _code = asked(client)
    assert approve(client, "alice", ref, role).status_code == 200
    if role is not None:
        grant(ctx.roles, "bob@example.com", role)
    lee, bob = answers(client, "lee"), answers(client, "bob")
    assert lee == bob
    # The control: the set both get holds an allowed and a refused route.
    statuses = {status for status, _ in lee.values()}
    assert 200 in statuses and 403 in statuses, lee
    assert lee["another workspace"] == (403, "tenant_not_member")
    assert lee["submit"][0] == (201 if role is None else 403)
