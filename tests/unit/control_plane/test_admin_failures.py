"""U19: `GET /v1/admin/failures` lists FAILED tasks across every tenant.

`GET /v1/tasks` is tenant-scoped and every task index leads with tenant_id, so
the platform's failures had no read. This route is full-admin only (403 for a
member and for the pool-admin capability), newest first, each row a task row
with its tenant_id, paged by the same cursor as `GET /v1/tasks`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_api.auth import POOL_ADMIN_ROUTES

from .conftest import auth_header, seed_task, seed_tenant


def _seed(db) -> None:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    for i in range(7):
        tenant = "eng" if i % 2 == 0 else "research"
        doc = seed_task(db, task_id=f"fail_{i}", tenant_id=tenant, state="FAILED",
                        created_at=base + timedelta(minutes=i))
        doc["last_error"] = f"exit 1 on step {i}"
    seed_task(db, task_id="ok_1", tenant_id="eng", state="SUCCEEDED",
              created_at=base + timedelta(minutes=30))


def test_a_member_is_refused(client, db):
    _seed(db)
    response = client.get("/v1/admin/failures", headers=auth_header("alice"))
    assert response.status_code == 403, response.text


def test_the_pool_admin_capability_does_not_reach_it():
    assert not any(path.endswith("/failures") for _method, path in POOL_ADMIN_ROUTES)


def test_an_admin_reads_every_tenants_failures_newest_first(client, db):
    _seed(db)
    response = client.get("/v1/admin/failures", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    rows = response.json()["tasks"]
    assert [row["id"] for row in rows] == [f"fail_{i}" for i in range(6, -1, -1)]
    assert {row["tenant_id"] for row in rows} == {"eng", "research"}
    assert all(row["state"] == "FAILED" for row in rows)
    # The same shape as a task row.
    member_row = client.get("/v1/tasks/fail_0", headers=auth_header("alice")).json()["task"]
    assert set(rows[-1]) == set(member_row)
    # And the member task list's row, attempt totals included (P1 follow-up).
    listed = client.get("/v1/tasks", headers=auth_header("alice")).json()["tasks"]
    (list_row,) = [row for row in listed if row["id"] == "fail_0"]
    assert set(list_row) == set(member_row)
    assert "cost_usd_total" in list_row and "attempts_read" in list_row


def test_it_pages_with_the_task_cursor(client, db):
    _seed(db)
    seen: list[str] = []
    token = None
    sizes = []
    for _ in range(10):
        params = {"limit": 3}
        if token:
            params["page_token"] = token
        response = client.get("/v1/admin/failures", headers=auth_header("root"), params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        sizes.append(len(body["tasks"]))
        seen.extend(row["id"] for row in body["tasks"])
        token = body["next_page_token"]
        if not token:
            break
    assert sizes == [3, 3, 1]
    assert seen == [f"fail_{i}" for i in range(6, -1, -1)]


def test_a_bad_cursor_is_a_422(client, db):
    _seed(db)
    response = client.get(
        "/v1/admin/failures", headers=auth_header("root"), params={"page_token": "nope"}
    )
    assert response.status_code == 422, response.text


def test_the_failures_index_is_declared():
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    text = (repo / "terraform/modules/firestore/indexes.tf").read_text()
    match = re.search(r'"tasks-state-created"\s*=\s*\{(.*?)\n    \}', text, re.S)
    assert match, "tasks-state-created is not declared in indexes.tf"
    block = match.group(1)
    assert 'collection  = "tasks"' in block
    assert 'query_scope = "COLLECTION"' in block
    fields = re.findall(r'field_path = "([^"]+)", order = "([A-Z]+)"', block)
    assert fields == [("state", "ASCENDING"), ("created_at", "DESCENDING")]
