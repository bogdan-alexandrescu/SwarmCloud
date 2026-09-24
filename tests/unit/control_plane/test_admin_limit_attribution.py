"""A ceiling changed through the admin API says who changed it.

WHY THIS EXISTS
---------------
On 2026-09-24 the owner decided that race-test narrows `runner:mock` through
`PUT /v1/admin/limits/runner/mock` rather than a raw Firestore PATCH, "so the
write is authorised and attributed by the API". The first half was already
true. The second was not, and docs/audits/2026-09-22/race-test-needs-a-write.md
said so in as many words: every limit route incremented
`admin_actions{action=...}` -- a counter with no caller, no pool and no value --
and `Store.upsert_pool` wrote `updated_at` and no actor. The only admin route
that recorded WHO was `dispatch/pause`.

So a ceiling the verification gate narrowed was indistinguishable, afterwards,
from one an operator narrowed during an incident. These tests pin the fix: every
admin route that writes a pool's ceiling or its switch stamps
`admin_changed_by` (the email from the VERIFIED token, never from the body) and
`admin_changed_at` on the pool document.

WHY NOT `updated_by`. `updated_at` on a pool is rewritten by the admission and
release transactions on every lease (`swarm_common/admission.py`), so an
`updated_by` beside it would pair one writer's timestamp with another writer's
name. The `admin_` fields are written by nothing else.

Nothing here changes what admission reads: the scheduler and admission decode
pools field by field and ignore unknown keys, and `active` is asserted
untouched below.
"""

from __future__ import annotations

import pytest

from .conftest import auth_header, seed_pool, seed_tenant

ROOT = "root@saga.xyz"

#: Every admin route that writes a pool document through `upsert_pool`, and the
#: pools each one touches. A route added later that writes a ceiling and is not
#: in this list is the omission this file exists to prevent, so the list is
#: long on purpose rather than representative.
POOL_WRITES = [
    ("put", "/v1/admin/limits/global", {"limit": 3}, ["global"]),
    ("put", "/v1/admin/limits/provider/anthropic", {"limit": 3}, ["provider:anthropic"]),
    (
        "put",
        "/v1/admin/limits/provider/anthropic/tenant/eng",
        {"limit": 3},
        ["provider:anthropic:tenant:eng"],
    ),
    ("put", "/v1/admin/limits/backend/CLOUD_RUN_JOB", {"limit": 3}, ["backend:CLOUD_RUN_JOB"]),
    ("put", "/v1/admin/limits/resource/standard", {"limit": 3}, ["resource:standard"]),
    ("put", "/v1/admin/limits/runner/mock", {"limit": 1}, ["runner:mock"]),
    (
        "post",
        "/v1/admin/resources/standard/drain",
        {"drain": True, "reason": "node pool upgrade"},
        ["resource:standard"],
    ),
    (
        "post",
        "/v1/admin/providers/anthropic/drain",
        {"drain": True, "reason": "billing hold"},
        # The provider pool AND every per-tenant slice of it -- the drain that
        # reaches every tenant is the one most worth being able to attribute.
        ["provider:anthropic", "provider:anthropic:tenant:eng"],
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "body", "pools"),
    POOL_WRITES,
    ids=[path for _, path, _, _ in POOL_WRITES],
)
def test_every_admin_pool_write_records_the_verified_caller(
    client, db, method, path, body, pools
):
    seed_tenant(db, "eng")
    for name in pools:
        seed_pool(db, name, hard_limit=20, active=2)

    response = getattr(client, method)(path, headers=auth_header("root"), json=body)
    assert response.status_code == 200, response.text

    for name in pools:
        doc = db.docs[f"pools/{name}"]
        assert doc.get("admin_changed_by") == ROOT, (
            f"{method.upper()} {path} changed pools/{name} and did not record who: "
            f"{sorted(doc)}"
        )
        assert doc.get("admin_changed_at") is not None, (
            f"{method.upper()} {path} recorded who but not when on pools/{name}"
        )
        # The one field no admin write may ever touch.
        assert doc["active"] == 2, f"pools/{name} active moved to {doc['active']}"


def test_a_pool_the_admin_route_creates_is_attributed_too(client, db):
    """`upsert_pool` creates a pool it cannot find, and that document is the
    one with the least history -- so it is the one that most needs a name."""
    response = client.put(
        "/v1/admin/limits/runner/mock", headers=auth_header("root"), json={"limit": 4}
    )
    assert response.status_code == 200, response.text

    doc = db.docs["pools/runner:mock"]
    assert doc["hard_limit"] == 4
    assert doc["active"] == 0
    assert doc.get("admin_changed_by") == ROOT
    assert doc.get("admin_changed_at") is not None


def test_the_attribution_cannot_be_supplied_by_the_caller(client, db):
    """The name comes from the token. A body that tries to set it is refused
    whole, and the pool is left exactly as it was."""
    seed_pool(db, "runner:mock", hard_limit=20)

    response = client.put(
        "/v1/admin/limits/runner/mock",
        headers=auth_header("root"),
        json={"limit": 1, "admin_changed_by": "someone-else@saga.xyz"},
    )
    assert response.status_code == 422, response.text
    doc = db.docs["pools/runner:mock"]
    assert doc["hard_limit"] == 20
    assert "admin_changed_by" not in doc


def test_a_refused_caller_leaves_no_attribution(client, db):
    """A non-admin is refused before anything is written, so there is no
    half-applied change carrying their name."""
    seed_pool(db, "runner:mock", hard_limit=20)

    response = client.put(
        "/v1/admin/limits/runner/mock", headers=auth_header("alice"), json={"limit": 1}
    )
    assert response.status_code == 403, response.text
    doc = db.docs["pools/runner:mock"]
    assert doc["hard_limit"] == 20
    assert "admin_changed_by" not in doc
