"""A tenant document without `created_at` serves null, not the time it was read (F10).

THE DEFECT. Both tenant decoders (`swarm_api.codec.tenant_from_dict`,
`scheduler.codec.tenant_from_dict`) filled a missing `created_at` with
`datetime.now()`. Every read of such a tenant then reported a different,
recent creation time -- a fabricated fact that moved on every refresh, and
looked exactly like a real one. A value the document does not hold is served
as null.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scheduler.codec import tenant_from_dict as scheduler_tenant_from_dict
from swarm_api.codec import tenant_from_dict, tenant_to_api

from .conftest import auth_header, seed_tenant


def _bare(tenant_id: str = "eng") -> dict:
    return {"tenant_id": tenant_id, "kind": "group", "principal": f"{tenant_id}@saga.xyz"}


def test_the_api_decoder_leaves_a_missing_created_at_null():
    tenant = tenant_from_dict(_bare())

    assert tenant.created_at is None
    assert tenant_to_api(tenant)["created_at"] is None


def test_the_scheduler_decoder_leaves_a_missing_created_at_null():
    assert scheduler_tenant_from_dict(_bare()).created_at is None


def test_a_stored_created_at_is_still_served():
    stamp = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    assert tenant_from_dict({**_bare(), "created_at": stamp}).created_at == stamp
    assert scheduler_tenant_from_dict({**_bare(), "created_at": stamp}).created_at == stamp


def test_the_admin_tenant_list_serves_null_for_a_tenant_without_created_at(db, client):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    del db.docs["tenants/research"]["created_at"]

    response = client.get("/v1/admin/tenants", headers=auth_header("root"))

    assert response.status_code == 200, response.text
    served = {t["tenant_id"]: t["created_at"] for t in response.json()["tenants"]}
    assert served["research"] is None
    assert served["eng"] is not None
