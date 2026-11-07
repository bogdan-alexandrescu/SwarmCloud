"""The bell's join items, through the app (docs/design/join-requests.md §4.6;
lane JOIN-BELL-WIRE, part of #2325).

`notifications.join_requests_to_decide` and `own_join_requests` read nothing
unless the route hands `Notifications` the join store and the reader's
administered workspaces. These drive `GET /v1/notifications` end to end:

* a workspace admin is rung for their workspace's open request and not for
  another workspace's; a platform admin for every one; a member for none;
* the requester is rung once after the decision;
* an administered-workspaces read that fails reports `partial: true` and no
  admin join item, never a 5xx for the whole bell;
* with no join request the feed is what it was before the wiring.

Same people and groups as test_join_requests_routes.py; no credentials, no
emulator. Every key and code is made at runtime.
"""

from __future__ import annotations

import pytest

from swarm_api import notifications as notif
from swarm_api.errors import RoleUnavailable
from swarm_api.routes import notifications as notif_routes

from .test_join_requests_routes import (  # noqa: F401 - the fixtures are used by name
    approve,
    ask,
    client,
    ctx,
    db,
    h,
    link_on,
)


def feed(client, user: str, tenant: str | None = None) -> dict:
    response = client.get("/v1/notifications", headers=h(user, tenant))
    assert response.status_code == 200, response.text
    return response.json()


def ids(body: dict) -> set[str]:
    return {item["id"] for item in body["items"]}


def two_asked(client) -> tuple[str, str]:
    """lee asks to join eng (alice administers it), kim research (erin's)."""
    lee_ref = ask(client, "lee", link_on(client))["ref"]
    kim_ref = ask(client, "kim", link_on(client, "erin", "research"))["ref"]
    return lee_ref, kim_ref


def test_a_workspace_admin_is_rung_for_their_workspaces_request_only(client) -> None:
    lee_ref, kim_ref = two_asked(client)

    # MUTATION: build the Viewer without `administers` -> alice is rung for nothing.
    body = feed(client, "alice", "eng")
    assert f"admin:join:{lee_ref}" in ids(body)
    # MUTATION: hand over every workspace -> alice is rung for kim's request too.
    assert f"admin:join:{kim_ref}" not in ids(body)
    assert body["partial"] is False

    body = feed(client, "erin", "research")
    assert f"admin:join:{kim_ref}" in ids(body)
    assert f"admin:join:{lee_ref}" not in ids(body)


def test_a_platform_admin_is_rung_for_every_request(client) -> None:
    lee_ref, kim_ref = two_asked(client)
    # MUTATION: pass no join store -> the platform admin is rung for nothing.
    got = ids(feed(client, "ops"))
    assert {f"admin:join:{lee_ref}", f"admin:join:{kim_ref}"} <= got


def test_a_member_who_administers_nothing_is_rung_for_none(client) -> None:
    two_asked(client)
    body = feed(client, "bob", "eng")
    assert not [i for i in ids(body) if i.startswith("admin:join:")]
    assert body["partial"] is False


def test_the_requester_is_rung_once_after_approval(client) -> None:
    lee_ref = ask(client, "lee", link_on(client))["ref"]
    assert not [i for i in ids(feed(client, "lee")) if i.startswith("join:")]

    assert approve(client, "alice", lee_ref).status_code == 200
    joined = [i for i in ids(feed(client, "lee")) if i.startswith("join:")]
    # MUTATION: pass no join store -> the requester hears nothing.
    assert joined == [f"join:{lee_ref}:approved"]
    # Decided, it rings its deciders no more.
    assert f"admin:join:{lee_ref}" not in ids(feed(client, "alice", "eng"))


def test_a_failed_administered_read_is_partial_not_a_5xx(client, ctx, monkeypatch) -> None:
    lee_ref, _ = two_asked(client)
    real = ctx.roles.person_role

    def unread(tenant_id: str, email: str, *, fresh: bool = False):
        # Only the fresh read `administered_workspaces` makes fails; the
        # request's own authentication still reads its role.
        if fresh:
            raise RoleUnavailable("the role store is down")
        return real(tenant_id, email, fresh=fresh)

    monkeypatch.setattr(ctx.roles, "person_role", unread)
    # MUTATION: let the read's RoleUnavailable escape -> a 503 for the whole bell.
    body = feed(client, "alice", "eng")
    # MUTATION: answer a failed read with () -> "administers nothing", not partial.
    assert body["partial"] is True
    assert f"admin:join:{lee_ref}" not in ids(body)
    # Read-all derives the feed too, and is not refused for it.
    response = client.post("/v1/notifications/read-all", headers=h("alice", "eng"))
    assert response.status_code == 200, response.text


def test_read_all_marks_the_admins_join_item_read(client) -> None:
    lee_ref, _ = two_asked(client)
    response = client.post("/v1/notifications/read-all", headers=h("alice", "eng"))
    assert response.status_code == 200, response.text
    (item,) = [i for i in feed(client, "alice", "eng")["items"]
               if i["id"] == f"admin:join:{lee_ref}"]
    assert item["read"] is True


@pytest.mark.parametrize("user,tenant", [("alice", "eng"), ("bob", "eng"), ("ops", None),
                                         ("lee", None)])
def test_with_no_join_request_the_feed_is_what_it_was(client, monkeypatch, user, tenant) -> None:
    wired = client.get("/v1/notifications", headers=h(user, tenant))
    assert wired.status_code == 200, wired.text

    # Today's route, as it was before the wiring: no join store, no administers.
    class Unwired(notif.Notifications):
        def __init__(self, *args, **kwargs) -> None:
            kwargs.pop("joins", None)
            super().__init__(*args, **kwargs)

        def feed(self, viewer):
            return super().feed(notif.Viewer(**{**viewer.__dict__, "administers": ()}))

    monkeypatch.setattr(notif_routes, "Notifications", Unwired)
    before = client.get("/v1/notifications", headers=h(user, tenant))
    assert before.status_code == 200, before.text
    assert wired.content == before.content
