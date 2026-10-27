"""`GET /v1/admin/people` marks a platform admin's row, `platform_admin: true`,
in both reads (lane ROLE-MARK, part of #2391; BGD 2026-10-26, "Mark the
source").

Every reader of Admin › People and roles, a workspace admin's scoped read
included, sees a platform admin's cell as "admin (platform admin)". The page
drew the suffix from the admins list, which the scoped read does not serve, so
a workspace admin saw a bare `admin` they could not tell from a grant. The row
now says where its cells came from; a plain member's row carries no marker.

Fixtures and people are `test_people_scoped`'s; `bob` is made a platform admin
as `test_people_route_platform_admin_cell` does it.
"""

from __future__ import annotations

from .test_people_route_platform_admin_cell import BOB, _make_bob_a_platform_admin
from .test_people_scoped import ROUTE, client, db, h, name_role  # noqa: F401


def _rows(body: dict) -> dict[str, dict]:
    return {row["email"]: row for row in body["people"]}


def test_the_scoped_read_marks_the_platform_admins_row(client) -> None:  # noqa: F811
    _make_bob_a_platform_admin(client)
    body = client.get(ROUTE, headers=h("alice")).json()
    assert body["scope"] == "workspaces"
    # MUTATION: drop the marker from in_workspaces' rows -> KeyError here.
    assert _rows(body)[BOB]["platform_admin"] is True
    # The marker is the only place it says so: still no admins list.
    assert not body.get("admins")


def test_the_platform_read_marks_the_platform_admins_row(client) -> None:  # noqa: F811
    _make_bob_a_platform_admin(client)
    body = client.get(ROUTE, headers=h("ops")).json()
    assert body["scope"] == "platform"
    # MUTATION: drop the marker from add_roles -> KeyError here.
    assert _rows(body)[BOB]["platform_admin"] is True


def test_a_plain_members_row_carries_no_marker_in_either_read(client) -> None:  # noqa: F811
    _make_bob_a_platform_admin(client)
    scoped = _rows(client.get(ROUTE, headers=h("alice")).json())
    platform = _rows(client.get(ROUTE, headers=h("ops")).json())
    # MUTATION: write `platform_admin: False` on every row -> these fail.
    for rows, plain in ((scoped, ("dave@example.com", "gail@example.com")),
                        (platform, ("dave@example.com", "erin@example.com"))):
        for email in plain:
            assert "platform_admin" not in rows[email], email
    # A workspace admin by grant is not a platform admin.
    assert "platform_admin" not in scoped["alice@example.com"]
