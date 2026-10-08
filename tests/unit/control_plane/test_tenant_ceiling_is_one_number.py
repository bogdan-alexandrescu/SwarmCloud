"""A tenant ceiling edit sets the ceiling the admin typed.

WHY THIS EXISTS
---------------
Measured live on 2026-10-07: the owner raised tenant `smoke` 8 -> 20 from the
console (Pool limits). `PUT /v1/admin/limits/tenant/smoke` wrote
`max_active=20` and nothing else; `capacity_units` stayed 8, and the pool --
`min(max_active, capacity_units)`, `Store.set_tenant_limits` -- stayed 8. The
route answered 200 with that pool, the admin record read "hard_limit 8 -> 8",
and the console said saved.

Owner decision, same day: the ceiling is ONE number. The ceiling route sets
both fields to the limit, so the pool becomes exactly the limit. The min()
rule stays for `PUT /v1/admin/tenants/{id}/limits`, which sets them
separately; the last test here pins that it was not loosened.
"""

from __future__ import annotations

from .conftest import auth_header, seed_tenant


def _smoke_at_eight(db) -> None:
    """`smoke` as it was live: max_active 20, capacity_units 8, pool 8."""
    seed_tenant(db, "smoke", max_active=20)
    db.docs["tenants/smoke"]["capacity_units"] = 8
    db.docs["pools/tenant:smoke"]["hard_limit"] = 8


def test_raising_the_ceiling_past_capacity_units_moves_the_pool(client, db):
    _smoke_at_eight(db)

    response = client.put(
        "/v1/admin/limits/tenant/smoke", headers=auth_header("root"), json={"limit": 20}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pool"]["hard_limit"] == 20, "the pool stayed where capacity_units held it"
    assert body["tenant"]["max_active"] == 20
    assert body["tenant"]["capacity_units"] == 20
    assert body["capped_by"] is None
    assert db.docs["pools/tenant:smoke"]["hard_limit"] == 20
    assert db.docs["tenants/smoke"]["max_active"] == 20
    assert db.docs["tenants/smoke"]["capacity_units"] == 20


def test_lowering_the_ceiling_moves_the_pool_and_both_fields(client, db):
    seed_tenant(db, "eng", max_active=20)  # capacity_units 40

    response = client.put(
        "/v1/admin/limits/tenant/eng", headers=auth_header("root"), json={"limit": 5}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pool"]["hard_limit"] == 5
    assert body["tenant"]["max_active"] == 5
    assert body["tenant"]["capacity_units"] == 5
    assert body["capped_by"] is None
    assert db.docs["pools/tenant:eng"]["hard_limit"] == 5


def test_the_admin_record_says_from_and_to_of_the_real_move(client, db):
    """It read "8 -> 8" for a raise to 20, which looked like nothing was asked."""
    _smoke_at_eight(db)

    client.put(
        "/v1/admin/limits/tenant/smoke", headers=auth_header("root"), json={"limit": 20}
    )

    change = db.docs["pools/tenant:smoke"]["admin_change"]
    assert change["hard_limit"] == {"from": 8, "to": 20}


def test_the_separate_limits_route_still_takes_the_smaller(client, db):
    """The min() rule is kept for the caller that sets the two separately."""
    _smoke_at_eight(db)

    response = client.put(
        "/v1/admin/tenants/smoke/limits",
        headers=auth_header("root"),
        json={"max_active": 20},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["max_active"] == 20
    assert response.json()["tenant"]["capacity_units"] == 8
    assert response.json()["pool"]["hard_limit"] == 8


def test_capped_by_names_what_held_the_pool_below_the_limit():
    """The console says this instead of a success when a pool did not move."""
    from types import SimpleNamespace

    from swarm_api.routes.admin import _tenant_capped_by

    tenant = SimpleNamespace(max_active=20, capacity_units=8)
    assert _tenant_capped_by(tenant, SimpleNamespace(hard_limit=20), 20) is None
    assert _tenant_capped_by(tenant, SimpleNamespace(hard_limit=8), 20) == "capacity_units"
    assert (
        _tenant_capped_by(SimpleNamespace(max_active=8, capacity_units=20),
                          SimpleNamespace(hard_limit=8), 20)
        == "max_active"
    )
    assert _tenant_capped_by(tenant, None, 20) == "pool_missing"
