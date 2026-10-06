"""#133: a pool says WHAT its last admin change was, not only who made it.

`admin_changed_by` / `admin_changed_at` already said who changed a pool and
when (test_admin_limit_attribution.py). They did not say what changed: a pool
read "ops@ changed this 3h ago" with no way to tell a raise from a cut to 0,
and no way to tell whether the ceiling on screen is the one that admin set or
one written since by something that records nothing (scripts/pool-limit.sh,
which writes Firestore directly).

So an admin write also stamps `admin_change`: each field the write named, with
the value before and the value after. It is served beside the other two on the
admin pool read, and -- like them -- never on `/v1/capacity`.

MUTATION: drop `admin_change` from the store's attribution and every test
below fails on the missing key; serve it from `pool_to_api` and the last one
does.
"""

from __future__ import annotations

from .conftest import auth_header, seed_pool, seed_tenant

ROOT = "root@saga.xyz"


def _admin_pools(client) -> dict[str, dict]:
    response = client.get("/v1/admin/pools", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return {p["name"]: p for p in response.json()["pools"]}


def test_a_limit_write_records_the_ceiling_before_and_after(client, db):
    seed_tenant(db, "eng")
    seed_pool(db, "runner:claude-code", hard_limit=20, active=4)
    changed = client.put(
        "/v1/admin/limits/runner/claude-code", headers=auth_header("root"), json={"limit": 10}
    )
    assert changed.status_code == 200, changed.text

    assert db.docs["pools/runner:claude-code"]["admin_change"] == {
        "hard_limit": {"from": 20, "to": 10}
    }
    pool = _admin_pools(client)["runner:claude-code"]
    assert pool["admin_change"] == {"hard_limit": {"from": 20, "to": 10}}
    assert pool["admin_changed_by"] == ROOT
    # `active` is admission's, and a record of a ceiling change never writes it.
    assert db.docs["pools/runner:claude-code"]["active"] == 4


def test_a_drain_records_the_switch_before_and_after(client, db):
    seed_tenant(db, "eng")
    seed_pool(db, "resource:standard", hard_limit=20)
    drained = client.post(
        "/v1/admin/resources/standard/drain",
        headers=auth_header("root"),
        json={"drain": True, "reason": "node pool upgrade"},
    )
    assert drained.status_code == 200, drained.text
    assert _admin_pools(client)["resource:standard"]["admin_change"] == {
        "enabled": {"from": True, "to": False}
    }


def test_the_newest_write_replaces_the_record(client, db):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=40)
    for limit in (30, 0):
        r = client.put("/v1/admin/limits/global", headers=auth_header("root"), json={"limit": limit})
        assert r.status_code == 200, r.text
    assert _admin_pools(client)["global"]["admin_change"] == {"hard_limit": {"from": 30, "to": 0}}


def test_a_pool_no_admin_changed_serves_null(client, db):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=20)
    pool = _admin_pools(client)["global"]
    assert "admin_change" in pool
    assert pool["admin_change"] is None


def test_an_internal_write_leaves_the_record_alone(client, db, api_context):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=40)
    r = client.put("/v1/admin/limits/global", headers=auth_header("root"), json={"limit": 30})
    assert r.status_code == 200, r.text
    # The quota broker and tenant creation pass no `by`: they are not admins,
    # and the last admin's record stays the last admin's.
    api_context.store.upsert_pool("global", quota_derived_limit=12)
    assert db.docs["pools/global"]["admin_change"] == {"hard_limit": {"from": 40, "to": 30}}


def test_capacity_never_serves_the_change_record(client, db):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=20)
    r = client.put("/v1/admin/limits/global", headers=auth_header("root"), json={"limit": 7})
    assert r.status_code == 200, r.text
    for user in ("alice", "root"):
        response = client.get("/v1/capacity", headers=auth_header(user))
        assert response.status_code == 200, response.text
        for pool in response.json()["pools"]:
            assert "admin_change" not in pool, (user, pool["name"])
