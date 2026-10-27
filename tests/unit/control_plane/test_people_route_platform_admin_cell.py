"""`GET /v1/admin/people` sends a platform admin's EFFECTIVE role in the grid
(lane ROLE-ROUTE, part of #2391; BGD 2026-10-26, "Wire it in a follow-up lane").

`people.role_cell` applies `RoleStore.resolve`'s platform-admin rule since
ROLE-EFFECTIVE-CELL (PR 2499), but the route passed it no platform admins, so
the grid still drew the stored grant. The route now reads the admin holders
before the grid and passes their emails to `People.add_roles` (a platform
admin's read) and `People.in_workspaces` (a workspace admin's scoped read,
which is served no admins list, so the cell is the only place it shows).

`bob` is in eng with an explicit stored `member` grant there and is made a
platform admin through `PUT /v1/admin/admins/{email}`, the way the console
does it. Fixtures and people are `test_people_scoped`'s.
"""

from __future__ import annotations

from swarm_api.admins import AdminRoles

from .test_people_scoped import ROUTE, client, columns, db, grid, h, name_role  # noqa: F401

BOB = "bob@example.com"


def _make_bob_a_platform_admin(client) -> None:  # noqa: F811
    name_role(client, BOB, "member", "eng")
    response = client.put(f"/v1/admin/admins/{BOB}", headers=h("ops"))
    assert response.status_code == 200, response.text


def test_a_platform_admins_cell_is_admin_in_every_group_workspace_of_the_platform_read(client) -> None:  # noqa: F811
    _make_bob_a_platform_admin(client)
    body = client.get(ROUTE, headers=h("ops")).json()
    assert body["scope"] == "platform"
    assert columns(body) == ["eng", "research"]
    # MUTATION: call add_roles without platform_admins -> {"eng": "member", "research": "none"}.
    assert grid(body)[BOB] == {"eng": "admin", "research": "admin"}
    assert BOB in {a["email"] for a in body["admins"]}


def test_a_platform_admins_cell_is_admin_in_a_workspace_admins_scoped_read(client) -> None:  # noqa: F811
    _make_bob_a_platform_admin(client)
    body = client.get(ROUTE, headers=h("alice")).json()
    assert body["scope"] == "workspaces"
    # MUTATION: call in_workspaces without platform_admins -> {"eng": "member"}.
    assert grid(body)[BOB] == {"eng": "admin"}
    # The scoped read still serves no admins list.
    assert not body.get("admins")


def test_a_plain_members_cell_is_unchanged_in_both_reads(client) -> None:  # noqa: F811
    _make_bob_a_platform_admin(client)
    platform = grid(client.get(ROUTE, headers=h("ops")).json())
    assert platform["dave@example.com"] == {"eng": "admin", "research": "member"}
    assert platform["erin@example.com"] == {"eng": "none", "research": "admin"}
    assert platform["carol@example.com"] == {"eng": "none", "research": "none"}
    scoped = grid(client.get(ROUTE, headers=h("alice")).json())
    assert scoped["gail@example.com"] == {"eng": "viewer"}
    assert scoped["alice@example.com"] == {"eng": "admin"}
    assert set(scoped) == {"alice@example.com", BOB, "dave@example.com", "gail@example.com"}


def test_unread_holders_leave_the_grid_on_stored_roles_and_the_admins_card_says_why(
        client, monkeypatch) -> None:  # noqa: F811
    _make_bob_a_platform_admin(client)

    def unreadable(self):
        raise RuntimeError("admin_roles could not be read")

    monkeypatch.setattr(AdminRoles, "holders", unreadable)
    response = client.get(ROUTE, headers=h("ops"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admins"] is None
    assert body["admins_error"] == "RuntimeError"
    assert grid(body)[BOB] == {"eng": "member", "research": "none"}
