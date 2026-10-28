"""A platform admin's cell in Admin › People and roles' grid is their EFFECTIVE
role (lane ROLE-EFFECTIVE-CELL, part of #2391; BGD 2026-10-26, VQ-ROLES).

`people.role_cell` is the grid's one cell. It now applies `RoleStore.resolve`'s
platform-admin rule (roles.py, Q5): a platform admin is `admin` in every group
workspace, whatever their stored grant says, and no grant is read for them --
so the cell is `admin` even in a column whose roles could not be read. The
Config page, This workspace and Profile give the same answer; the grid said
the stored `member` (visual QA VQ-044). Everyone else's cell is unchanged.

Pure: no Firestore, no app, no credentials.
"""

from __future__ import annotations

from swarm_api.people import ROLE_NONE, ROLE_REQUESTED, role_cell
from swarm_api.roles import ADMIN

ENG = {"tenant_id": "eng", "group": "eng@example.com", "display_name": "eng@example.com",
       "default_role": "member", "unavailable": False}
OPS = "ops@example.com"
BOB = "bob@example.com"


def test_a_platform_admins_cell_is_admin_whatever_their_stored_grant_says() -> None:
    grants = {"eng": {OPS: "member", BOB: "member"}}
    # MUTATION: return the stored grant first -> `member`, and this fails.
    assert role_cell(OPS, {"eng"}, ENG, grants, platform_admins={OPS}) == ADMIN


def test_a_platform_admin_is_admin_where_they_were_never_seen_or_granted() -> None:
    # "in or reaches": every registered group workspace is a platform admin's reach.
    assert role_cell(OPS, set(), ENG, {"eng": {}}, platform_admins={OPS}) == ADMIN


def test_a_platform_admin_is_admin_in_a_column_whose_roles_were_not_read() -> None:
    # resolve reads no grant for a platform admin, so an unread roster changes nothing.
    assert role_cell(OPS, {"eng"}, {**ENG, "unavailable": True}, {"eng": None},
                     platform_admins={OPS}) == ADMIN


def test_a_plain_members_cell_is_unchanged() -> None:
    grants = {"eng": {BOB: "viewer"}}
    assert role_cell(BOB, {"eng"}, ENG, grants, platform_admins={OPS}) == "viewer"
    assert role_cell("fay@example.com", {"eng"}, ENG, grants, platform_admins={OPS}) == "member"
    assert role_cell("gail@example.com", set(), ENG, grants, platform_admins={OPS}) == ROLE_NONE
    assert role_cell("hal@example.com", set(), ENG, grants, {"eng": {"hal@example.com"}},
                     platform_admins={OPS}) == ROLE_REQUESTED
    assert role_cell(BOB, {"eng"}, ENG, {"eng": None}, platform_admins={OPS}) is None


def test_without_the_platform_admins_the_cell_is_the_stored_role_as_before() -> None:
    # A caller that passes no set (today's route) draws what it drew before.
    assert role_cell(OPS, {"eng"}, ENG, {"eng": {OPS: "member"}}) == "member"
