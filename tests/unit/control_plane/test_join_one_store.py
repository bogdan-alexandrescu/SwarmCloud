"""Every platform-membership write goes through the authenticator's one
MembershipStore, removal as well as approval (docs/design/join-requests.md
§4.4; lane JOIN-ONE-STORE, part of #2325; BGD's answer to JOIN-WIRE's owner
question, 2026-10-26).

Before this lane `DELETE /v1/workspace/members/person` wrote through
`people.membership_store(ctx)`, a second store cached on the AppContext. Its
eviction never reached the authenticator's cache, so with
PLATFORM_MEMBERSHIPS on a removed person kept selecting the workspace for up
to `role_cache_seconds`. Per-tenant isolation (CONTRACT invariant 9) is the
property at stake: bounded, but a removed person acting in a workspace.

What is held:

* switch on, a person removed through the route is refused (403
  `tenant_not_member`) on their very next request naming that workspace;
* a Google-group member is unaffected, even when their own platform
  membership is the one removed;
* switch off, no request reads `workspace_memberships`;
* the route's store IS the authenticator's, and only a context whose
  authenticator has none builds one of its own.

Fixtures are JOIN-API's (`test_join_requests_routes`): `lee` is in no group;
`bob` is in eng's Google group; `alice` is eng's admin.
"""

from __future__ import annotations

from swarm_api.memberships import MembershipStore
from swarm_api.routes.people import membership_store

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
from .test_join_wire import code_of, holds, membership_reads

BOB = "bob@example.com"


def remove(client, subject: str = LEE):
    return client.request("DELETE", "/v1/workspace/members/person", headers=h("alice", "eng"),
                          json={"email": subject, "reason": "left the rota"})


def test_the_routes_store_is_the_authenticators(ctx) -> None:
    # MUTATION: membership_store() building its own store -> a second
    # instance, and the identity fails.
    assert ctx.authenticator.memberships is not None
    assert membership_store(ctx) is ctx.authenticator.memberships


def test_a_context_whose_authenticator_has_no_store_keeps_one_of_its_own(
        ctx, monkeypatch) -> None:
    monkeypatch.setattr(ctx.authenticator, "_memberships", None)
    own = membership_store(ctx)
    assert isinstance(own, MembershipStore)
    assert membership_store(ctx) is own


def test_a_removed_person_is_refused_on_their_very_next_request(client, ctx) -> None:
    switch_on(ctx)
    # The control: the cache is on, so a second store's removal would leave
    # the authenticator's cached `member` read standing for these seconds.
    assert ctx.settings.role_cache_seconds > 0
    ref, _code = asked(client)
    assert approve(client, "alice", ref).status_code == 200
    before = client.get("/v1/workspace/roles", headers=h("lee", "eng"))
    assert before.status_code == 200, before.text
    assert before.json()["me"]["via"] == "platform_member"

    removed = remove(client)
    assert removed.status_code == 200, removed.text
    assert removed.json()["state"] == "removed"
    # MUTATION: remove_member writing through a second store -> lee's cached
    # read answers, and this is 200.
    after = client.get("/v1/workspace/roles", headers=h("lee", "eng"))
    assert after.status_code == 403, after.text
    assert code_of(after) == "tenant_not_member"
    mine = client.get("/v1/tenants/mine", headers=h("lee"))
    assert mine.status_code == 200, mine.text
    assert "eng" not in {c["tenant_id"] for c in mine.json()}


def test_a_group_member_is_unaffected_by_a_removal(client, ctx) -> None:
    switch_on(ctx)
    holds(ctx)
    holds(ctx, BOB)
    assert client.get("/v1/workspace/roles", headers=h("lee", "eng")).status_code == 200
    assert client.get("/v1/workspace/roles", headers=h("bob", "eng")).status_code == 200

    gone = remove(client, BOB)
    assert gone.status_code == 200, gone.text
    assert gone.json()["still_in_group"] is True
    # Bob's Google group still admits him, as a group member now.
    bob = client.get("/v1/workspace/roles", headers=h("bob", "eng"))
    assert bob.status_code == 200, bob.text
    assert bob.json()["me"]["via"] == "member"
    # And removing lee leaves bob exactly where he was.
    assert remove(client).status_code == 200
    assert client.get("/v1/workspace/roles", headers=h("lee", "eng")).status_code == 403
    assert client.get("/v1/workspace/roles", headers=h("bob", "eng")).status_code == 200


def test_switch_off_no_request_reads_workspace_memberships(client, ctx, db, monkeypatch) -> None:
    holds(ctx)
    assert membership_store(ctx) is ctx.authenticator.memberships
    reads = membership_reads(db, monkeypatch)
    refused = client.get("/v1/workspace/roles", headers=h("lee", "eng"))
    assert refused.status_code == 403 and code_of(refused) == "tenant_not_member", refused.text
    assert client.get("/v1/workspace/roles", headers=h("bob", "eng")).status_code == 200
    assert client.get("/v1/tenants/mine", headers=h("lee")).status_code == 200
    assert reads == []
