"""U5: `GET /v1/tasks?submitted_by=` filters by owner on the server.

`me` is the caller's verified email; any other value must be the caller's own
unless the caller is an admin (403 otherwise). The filter always narrows the
CHECKED tenant -- an admin's filter still reads one tenant's tasks. A
continuation-scoped caller keeps its forced own-tasks filter and is refused for
naming anyone else. Alone, the filter runs IN THE QUERY, so pages are full and
the cursor is exact; combined with another filter it stays a post-filter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_common.identity import TenantMember

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ENG_GROUP, PROJECT, api_settings, auth_header, seed_task, seed_tenant
from .fakes import FakeQuery

ALICE = "alice@saga.xyz"
ROOT = "root@saga.xyz"
OTHER = "dave@saga.xyz"
FIXER = f"swarm-ci-fix@{PROJECT}.iam.gserviceaccount.com"
FIXER_UID = "104857600000000000002"


def _seed(db, *, mine: int, others: int, tenant: str = "eng", owner: str = ALICE) -> None:
    """Interleaved, so a post-filter over an unfiltered page would come up short."""
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    n = 0
    for i in range(max(mine, others)):
        for who, count in ((owner, mine), (OTHER, others)):
            if i >= count:
                continue
            doc = seed_task(db, task_id=f"{tenant}_{who.split('@')[0]}_{i:03d}",
                            tenant_id=tenant, state="SUCCEEDED",
                            created_at=base + timedelta(seconds=n))
            doc["submitted_by"] = who
            n += 1


def _list(client, user: str = "alice", **params: Any):
    return client.get("/v1/tasks", headers=auth_header(user), params=params)


def _all_pages(client, user: str = "alice", **params: Any) -> list[list[dict]]:
    pages: list[list[dict]] = []
    token = None
    for _ in range(20):
        query = dict(params)
        if token:
            query["page_token"] = token
        response = _list(client, user, **query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body["tasks"])
        token = body["next_page_token"]
        if not token:
            return pages
    raise AssertionError("paging never ended")


def test_me_returns_only_the_callers_tasks_in_full_pages_with_a_working_cursor(client, db):
    seed_tenant(db, "eng")
    _seed(db, mine=12, others=12)
    pages = _all_pages(client, submitted_by="me", limit=5)
    sizes = [len(p) for p in pages]
    assert sizes == [5, 5, 2], f"pages were {sizes}; an in-query filter fills every page"
    rows = [row for page in pages for row in page]
    assert {row["submitted_by"] for row in rows} == {ALICE}
    ids = [row["id"] for row in rows]
    assert len(set(ids)) == 12, "the cursor repeated or skipped a row"
    created = [row["created_at"] for row in rows]
    assert created == sorted(created, reverse=True), "not newest first"


def test_the_query_carries_the_filter_when_it_is_the_only_one(client, db, monkeypatch):
    seed_tenant(db, "eng")
    _seed(db, mine=2, others=2)
    seen: list[tuple] = []
    original = FakeQuery.stream

    def recording(self, *args, **kwargs):
        if self._path == "tasks":
            seen.append(self._filters)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FakeQuery, "stream", recording)
    assert _list(client, submitted_by="me").status_code == 200
    assert any(("submitted_by", "==", ALICE) in filters for filters in seen), seen
    assert all(("tenant_id", "==", "eng") in filters for filters in seen), seen

    # Combined with state, it is a post-filter (no index covers the pair).
    seen.clear()
    response = _list(client, submitted_by="me", state="SUCCEEDED")
    assert response.status_code == 200
    assert {row["submitted_by"] for row in response.json()["tasks"]} == {ALICE}
    assert seen and not any(f[0] == "submitted_by" for filters in seen for f in filters)


def test_naming_your_own_email_is_allowed(client, db):
    seed_tenant(db, "eng")
    _seed(db, mine=2, others=2)
    response = _list(client, submitted_by=ALICE)
    assert response.status_code == 200, response.text
    assert {row["submitted_by"] for row in response.json()["tasks"]} == {ALICE}


def test_another_members_email_is_a_403_for_a_non_admin(client, db):
    seed_tenant(db, "eng")
    _seed(db, mine=2, others=2)
    response = _list(client, submitted_by=OTHER)
    assert response.status_code == 403, response.text


def test_an_admin_may_filter_by_another_member(client, db):
    seed_tenant(db, "eng")
    _seed(db, mine=2, others=3)
    response = _list(client, "root", submitted_by=OTHER)
    assert response.status_code == 200, response.text
    rows = response.json()["tasks"]
    assert len(rows) == 3
    assert {row["submitted_by"] for row in rows} == {OTHER}


def test_an_admins_filter_never_crosses_tenants(client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _seed(db, mine=2, others=2, tenant="eng")
    _seed(db, mine=2, others=2, tenant="research")
    response = _list(client, "root", submitted_by=OTHER)
    assert response.status_code == 200, response.text
    body = response.json()
    tenants = {row["tenant_id"] for row in body["tasks"]}
    assert tenants == {body["tenant_id"]}, f"one filter read tenants {tenants}"
    assert len(body["tasks"]) == 2


# -- a continuation-scoped caller ---------------------------------------------

@pytest.fixture
def fixer_client(db, tokens, group_map, objects) -> TestClient:
    tokens = dict(tokens)
    tokens["token-fixer"] = {"email": FIXER, "email_verified": True, "sub": FIXER_UID}
    context = build_context(
        settings=api_settings(
            tenant_service_accounts=(
                TenantMember(email=FIXER, kind="group", principal=ENG_GROUP, uid=FIXER_UID),
            )
        ),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


FIXER_HEADERS = {"Authorization": "Bearer token-fixer"}


def test_a_continuation_scoped_caller_naming_someone_else_is_a_403(fixer_client, db):
    seed_tenant(db, "eng")
    _seed(db, mine=2, others=2, owner=FIXER)
    response = fixer_client.get(
        "/v1/tasks", headers=FIXER_HEADERS, params={"submitted_by": OTHER}
    )
    assert response.status_code == 403, response.text


def test_a_continuation_scoped_caller_keeps_its_forced_filter(fixer_client, db):
    seed_tenant(db, "eng")
    _seed(db, mine=3, others=3, owner=FIXER)
    for params in ({}, {"submitted_by": "me"}, {"submitted_by": FIXER}):
        response = fixer_client.get("/v1/tasks", headers=FIXER_HEADERS, params=params)
        assert response.status_code == 200, (params, response.text)
        rows = response.json()["tasks"]
        assert {row["submitted_by"] for row in rows} == {FIXER}, params
        assert len(rows) == 3, params


def test_the_owner_index_is_declared():
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    text = (repo / "terraform/modules/firestore/indexes.tf").read_text()
    match = re.search(r'"tasks-tenant-submitter-created"\s*=\s*\{(.*?)\n    \}', text, re.S)
    assert match, "tasks-tenant-submitter-created is not declared in indexes.tf"
    block = match.group(1)
    assert 'collection  = "tasks"' in block
    assert 'query_scope = "COLLECTION"' in block
    fields = re.findall(r'field_path = "([^"]+)", order = "([A-Z]+)"', block)
    assert fields == [
        ("tenant_id", "ASCENDING"), ("submitted_by", "ASCENDING"), ("created_at", "DESCENDING"),
    ]
