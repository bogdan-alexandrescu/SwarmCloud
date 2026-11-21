"""The last-admin guard on every route that lowers who administers a group
workspace, not only the self-leave route (issue 47; docs/design/join-requests.md
§4.4; `routes/last_admin.py`).

Read with an attacker's eye. The attacks are:

* the last admin REMOVING THEMSELVES through `DELETE /v1/workspace/members/person`
  instead of leaving: the same 409 `workspace_leave_last_admin`, nothing written;
* a platform admin removing the last admin, as if their own reach were an
  admin of the workspace: it is not, so the same 409 -- and they can still
  name a new admin, after which the removal goes through;
* the last admin DEMOTING or CLEARING their own override (or a platform admin
  doing it for them) and then leaving: the demotion is refused;
* a leave and a demotion of the only two admins AT ONCE: they conflict on the
  guard document and at least one admin is left;
* an unreadable roster or directory read as "someone else is an admin": 503.

Every refusal leaves one `workspace_last_admin_refused` audit row and writes
nothing else. Who is who is `test_workspace_leave.py`'s: `ops` a platform
admin; `alice` and `bob` in eng's group; `lee` and `dan` in no group, members
of eng by platform membership.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import memberships, roles
from swarm_api.errors import RoleUnavailable
from swarm_api.groups import GroupLookupError
from swarm_api.routes.join_requests import membership_store

from .conftest import seed_tenant
from .test_workspace_leave import (  # noqa: F401 - fixtures
    ALICE,
    DAN,
    LEAVE,
    LEE,
    RacingFirestore,
    build,
    client,
    ctx,
    db,
    grant_member,
    h,
    joined,
    refused,
    selects,
    serve,
    set_role,
    written,
)

MEMBERS = "/v1/workspace/members/person"
ROLES = "/v1/workspace/roles/person"
ADMIN_ROLES = "/v1/admin/workspaces/eng/roles/person"
LAST = "workspace_leave_last_admin"


def remove(client, user: str, email: str):
    return client.request("DELETE", MEMBERS, headers=h(user, "eng"), json={"email": email})


def lee_is_the_last_admin(client) -> None:
    """lee joins eng and is named its admin; alice, the group's admin, is
    cleared, so lee is eng's only admin who can still select it."""
    joined(client)
    set_role(client, LEE, "admin")
    set_role(client, ALICE, None)


def platform_admin_reach(ctx) -> None:
    """ops selects eng as a platform admin (`tenant_via: platform_admin`)."""
    object.__setattr__(ctx.settings, "platform_admin_reach", True)


def grants(db) -> dict[str, Any]:
    """The memberships, role grants and guard documents: what a refused change
    must not write (a platform admin's own `platform_admin_acted` row aside)."""
    kept = (memberships.COLLECTION, roles.GRANTS_COLLECTION,
            memberships.LEAVE_GUARD_COLLECTION)
    return {k: v for k, v in db.dump().items() if k.split("/", 1)[0] in kept}


def role_of(ctx, email: str) -> str | None:
    grant = ctx.roles.newest_valid("eng", email, fresh=True)
    return grant.role if grant is not None else None


# -- removal -----------------------------------------------------------------------

def test_the_last_admin_cannot_remove_themselves(client, ctx, db) -> None:
    lee_is_the_last_admin(client)
    before = written(db)
    response = remove(client, "lee", LEE)
    # MUTATION: drop the guard from remove_member -> 200, eng has no admin of its own.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == LAST
    assert written(db) == before
    assert selects(client, "lee", "eng") == 200
    (row,) = refused(db)
    assert (row["by"], row["target_email"]) == (LEE, LEE)
    assert row["detail"] == {"route": "member_removed", "via": "workspace_admin",
                             "refused": LAST}


def test_an_admin_removes_themselves_while_another_admin_remains(client, ctx, db) -> None:
    joined(client)
    set_role(client, LEE, "admin")
    # alice is eng's admin too, and its group holds her.
    response = remove(client, "lee", LEE)
    assert response.status_code == 200, response.text
    assert membership_store(ctx).read(LEE, fresh=True).newest["eng"].state == memberships.REMOVED
    assert not refused(db)


def test_a_platform_admin_is_not_counted_and_can_name_a_new_admin(client, ctx, db) -> None:
    platform_admin_reach(ctx)
    lee_is_the_last_admin(client)
    before = grants(db)
    response = remove(client, "ops", LEE)
    # MUTATION: count the platform admin removing lee as another admin -> 200.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == LAST
    assert grants(db) == before
    (row,) = refused(db)
    assert row["by"] == "ops@example.com" and row["detail"]["via"] == "platform_admin"

    # Naming an admin is never guarded; then the removal goes through.
    set_role(client, ALICE, "admin")
    response = remove(client, "ops", LEE)
    assert response.status_code == 200, response.text


def test_removing_a_person_who_is_no_admin_is_not_guarded(ctx, db) -> None:
    platform_admin_reach(ctx)
    bare = serve(ctx)  # eng has no admin of its own at all
    grant_member(ctx, DAN)
    response = remove(bare, "ops", DAN)
    assert response.status_code == 200, response.text
    assert not refused(db)


# -- the roles routes ----------------------------------------------------------------

@pytest.mark.parametrize("route,user,method,body", [
    (ROLES, "lee", "PUT", {"email": LEE, "role": "member"}),
    (ROLES, "lee", "PUT", {"email": LEE, "role": "viewer"}),
    (ROLES, "lee", "DELETE", {"email": LEE}),
    (ADMIN_ROLES, "ops", "PUT", {"email": LEE, "role": "member"}),
    (ADMIN_ROLES, "ops", "DELETE", {"email": LEE}),
])
def test_the_last_admins_override_cannot_be_lowered_or_cleared(client, ctx, db, route, user,
                                                               method, body) -> None:
    lee_is_the_last_admin(client)
    before = written(db)
    tenant = "eng" if route == ROLES else None
    response = client.request(method, route, headers=h(user, tenant), json=body)
    # MUTATION: drop the guard from _set_person/_clear_person -> 200, no admin left.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == LAST
    assert written(db) == before
    assert role_of(ctx, LEE) == roles.ADMIN
    (row,) = refused(db)
    assert row["target_email"] == LEE and row["by"] == f"{user}@example.com"
    assert row["detail"]["route"] == ("role_set" if method == "PUT" else "role_cleared")


@pytest.mark.parametrize("method,body", [
    ("PUT", {"email": LEE, "role": "member"}),
    ("DELETE", {"email": LEE}),
])
def test_an_admin_is_lowered_while_another_admin_remains(client, ctx, db, method, body) -> None:
    joined(client)
    set_role(client, LEE, "admin")
    # alice, whom eng's group holds, is still its admin.
    response = client.request(method, ROLES, headers=h("lee", "eng"), json=body)
    assert response.status_code == 200, response.text
    assert role_of(ctx, LEE) != roles.ADMIN
    assert not refused(db)


def test_a_kept_override_of_someone_who_left_is_no_other_admin(client, ctx, db) -> None:
    """dan holds `admin` but has left: he cannot select eng, so lee remains
    its last admin and may not lower herself."""
    lee_is_the_last_admin(client)
    grant_member(ctx, DAN)
    set_role(client, DAN, "admin")
    assert client.post(LEAVE, headers=h("dan", "eng")).status_code == 200
    response = client.put(ROLES, headers=h("lee", "eng"), json={"email": LEE, "role": "member"})
    assert response.status_code == 409, response.text


def test_naming_an_admin_is_never_guarded(ctx, db) -> None:
    bare = serve(ctx)  # eng has no admin of its own at all
    set_role(bare, DAN, "admin")
    assert role_of(ctx, DAN) == roles.ADMIN
    assert not refused(db)


# -- fail closed ---------------------------------------------------------------------

def _unreadable_roster(self, tenant_id: str, *, fresh: bool = False) -> list:
    raise RoleUnavailable("this workspace's roles could not be read; retry shortly")


@pytest.mark.parametrize("call", ["leave", "remove", "demote", "clear"])
def test_an_unreadable_roster_is_a_503_and_nothing_is_written(client, ctx, db, monkeypatch,
                                                              call) -> None:
    joined(client)
    set_role(client, LEE, "admin")
    monkeypatch.setattr(roles.RoleStore, "overrides", _unreadable_roster)
    before = written(db)
    if call == "leave":
        response = client.post(LEAVE, headers=h("lee", "eng"))
    elif call == "remove":
        response = remove(client, "lee", LEE)
    else:
        response = client.request("PUT" if call == "demote" else "DELETE", ROLES,
                                  headers=h("lee", "eng"),
                                  json={"email": LEE, "role": "member"} if call == "demote"
                                  else {"email": LEE})
    # MUTATION: treat an unreadable roster as "another admin remains" -> 200.
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "role_unavailable"
    assert written(db) == before
    assert role_of(ctx, LEE) == roles.ADMIN


def test_an_unreadable_role_of_the_person_removed_is_a_503(client, ctx, db, monkeypatch) -> None:
    joined(client)
    set_role(client, LEE, "admin")
    real = roles.RoleStore.newest_valid

    def newest_valid(self, tenant_id, subject, **kwargs):
        if subject == LEE:
            raise RoleUnavailable("this workspace's roles could not be read; retry shortly")
        return real(self, tenant_id, subject, **kwargs)

    monkeypatch.setattr(roles.RoleStore, "newest_valid", newest_valid)
    before = written(db)
    response = remove(client, "alice", LEE)
    # MUTATION: read a failed role read as "not an admin" -> 200.
    assert response.status_code == 503, response.text
    assert written(db) == before


class DownDirectory:
    def groups_for(self, email: str, candidates: tuple[str, ...], *,
                   fresh: bool = False) -> tuple[str, ...]:
        raise GroupLookupError("directory down")


def test_an_unreadable_directory_is_a_503_for_a_demotion(client, ctx, db) -> None:
    """lee's only other admin is bob, whom only the group could vouch for."""
    lee_is_the_last_admin(client)
    set_role(client, "bob@example.com", "admin")
    client.app.state.workspace_directory = DownDirectory()
    before = written(db)
    response = client.put(ROLES, headers=h("lee", "eng"), json={"email": LEE, "role": "member"})
    # MUTATION: answer a failed group check with "bob counts" -> 200.
    assert response.status_code == 503, response.text
    assert written(db) == before


# -- a leave and a demotion at once --------------------------------------------------

@pytest.mark.parametrize("first", ["leave", "demote"])
def test_a_leave_and_a_demotion_of_the_only_two_admins_leave_one(first: str) -> None:
    """lee and dan are eng's only admins, both by platform membership. lee
    leaves while ops lowers dan to member: whichever commits first, the
    other's transaction sees the guard document change, is retried, and is
    refused -- eng keeps an admin who can select it."""
    db = RacingFirestore()
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build(db)
    client = serve(ctx)
    set_role(client, ALICE, "admin")
    joined(client)
    grant_member(ctx, DAN)
    set_role(client, LEE, "admin")
    set_role(client, DAN, "admin")
    set_role(client, ALICE, None)

    def leave() -> Any:
        return client.post(LEAVE, headers=h("lee", "eng"))

    def demote() -> Any:
        return client.put(ADMIN_ROLES, headers=h("ops"), json={"email": DAN, "role": "member"})

    outer, inner = (leave, demote) if first == "leave" else (demote, leave)
    answers: list[Any] = []
    db.on_commit = lambda: answers.append(inner())
    outcome = outer()
    (nested,) = answers
    # MUTATION: check outside the transaction, or skip the guard on either -> both 200.
    assert (nested.status_code, outcome.status_code) == (200, 409), (nested.text, outcome.text)
    assert outcome.json()["code"] == LAST
    assert db.aborts == 1

    store = membership_store(ctx)
    lee_member = store.read(LEE, fresh=True).newest["eng"].state == memberships.MEMBER
    dan_admin = role_of(ctx, DAN) == roles.ADMIN
    lee_admin = role_of(ctx, LEE) == roles.ADMIN
    # At least one admin who can still select eng remains.
    assert (lee_admin and lee_member) or dan_admin
    if first == "leave":
        # The demotion ran inside lee's commit and won; lee stayed.
        assert lee_member and lee_admin and not dan_admin
    else:
        # lee's leave ran inside the demotion's commit and won; dan stayed admin.
        assert not lee_member and dan_admin
    assert [r["detail"]["route"] for r in refused(db)] == [
        "leave" if first == "leave" else "role_set"]
