"""#133 (API half): the admin pool read serves who last changed a pool, and when.

`Store.upsert_pool` stamps `admin_changed_by` / `admin_changed_at` on a pool an
admin route changes, and `codec.pool_to_api` dropped both, so Admin > Pool
limits could not say who narrowed a ceiling. They are served on the ADMIN pool
read (`GET /v1/admin/pools`), null for a pool never changed through an admin
route -- and deliberately NOT on `GET /v1/capacity`, which every tenant member
reads: an admin's email is not tenant data.
"""

from __future__ import annotations

from datetime import datetime

from .conftest import auth_header, seed_pool, seed_tenant

ROOT = "root@saga.xyz"


def _admin_pools(client) -> dict[str, dict]:
    response = client.get("/v1/admin/pools", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return {p["name"]: p for p in response.json()["pools"]}


def test_the_admin_pool_read_serves_who_changed_the_pool(client, db):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=20)
    changed = client.put("/v1/admin/limits/global", headers=auth_header("root"), json={"limit": 7})
    assert changed.status_code == 200, changed.text

    pool = _admin_pools(client)["global"]
    assert pool["admin_changed_by"] == ROOT
    assert pool["admin_changed_at"] is not None
    stamped = db.docs["pools/global"]["admin_changed_at"]
    served = datetime.fromisoformat(pool["admin_changed_at"].replace("Z", "+00:00"))
    assert abs((served - stamped).total_seconds()) < 1


def test_a_pool_never_changed_by_an_admin_serves_null(client, db):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=20)
    pools = _admin_pools(client)
    for name in ("global", "tenant:eng"):
        assert "admin_changed_by" in pools[name], name
        assert pools[name]["admin_changed_by"] is None, name
        assert pools[name]["admin_changed_at"] is None, name


def test_capacity_never_serves_the_admin_attribution(client, db):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=20)
    changed = client.put("/v1/admin/limits/global", headers=auth_header("root"), json={"limit": 7})
    assert changed.status_code == 200, changed.text

    for user in ("alice", "root"):
        response = client.get("/v1/capacity", headers=auth_header(user))
        assert response.status_code == 200, response.text
        assert ROOT not in response.text, f"/v1/capacity served the admin's email to {user}"
        for pool in response.json()["pools"]:
            assert "admin_changed_by" not in pool, pool["name"]
            assert "admin_changed_at" not in pool, pool["name"]
