"""Contract request 132: `GET /v1/tasks` serves the task's STORED attempt
totals and current account, and derives them only for a task that has none.

Before it, a 200-row page read the page, then ceil(200/30) = 7 attempt
queries and up to 7 account-event queries, the leases and the pools: about
16 round trips, run concurrently since part of #2688 (LOAD-SPEED). With the
fields written when an attempt ends and when an account is assigned, the
same page is the page, the leases and the pools.

A task written before the fields (or before the backfill reached it) falls
back to today's derivation, row by row, and is served exactly as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import task_accounts
from swarm_api.attempt_totals import attempt_totals

from .conftest import seed_task, seed_tenant
from .fakes import FakeFirestore, FakeQuery
from .test_task_list_load_speed import _list, meter  # noqa: F401  (the fixture)

BASE = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
STATES = ("READY", "RUNNING", "SUCCEEDED", "FAILED", "PARKED")


@pytest.fixture(autouse=True)
def _events_group(monkeypatch):
    """The fake with `collection_group("events")` over top-level `events/*`,
    so a fallback account read is a real (metered) query."""
    monkeypatch.setattr(FakeFirestore, "collection_group",
                        lambda self, path: FakeQuery(self, path), raising=False)
    task_accounts.FINISHED.clear()
    yield
    task_accounts.FINISHED.clear()


def _attempt(i: int, task_id: str) -> dict:
    return {
        "attempt_id": f"att_{i:03d}", "tenant_id": "eng", "task_id": task_id,
        "generation": 1, "lease_id": f"lease_{i:03d}", "backend": "gke_autopilot",
        "created_at": BASE, "started_at": BASE,
        "completed_at": BASE + timedelta(seconds=30), "cost_usd": 0.25,
    }


def _stored_totals(i: int) -> dict:
    from swarm_api.codec import attempt_from_dict

    return {**attempt_totals([attempt_from_dict(_attempt(i, f"task_{i:03d}"))]),
            "as_of": {"attempt_id": f"att_{i:03d}", "generation": 1}}


def _stored_account(i: int) -> dict:
    return {"status": "assigned", "account_id": f"acct_{i % 3}", "provider": "anthropic",
            "attempt_id": f"att_{i:03d}", "generation": 1, "swapped_from": None, "swaps": []}


def _seed(db, rows: int = 200, *, stored: bool = True) -> None:
    """`rows` claude-code tasks, each with one attempt and one account event;
    with `stored`, each also carries the two stored fields."""
    seed_tenant(db, "eng")
    for i in range(rows):
        state = STATES[i % len(STATES)]
        task_id = f"task_{i:03d}"
        doc = seed_task(db, task_id=task_id, tenant_id="eng", state=state,
                        runner_profile="claude-code", provider="anthropic",
                        created_at=BASE + timedelta(seconds=i))
        doc["submitted_by"] = "alice@example.com"
        doc["current_generation"] = 1
        if state == "RUNNING":
            doc["current_lease_id"] = f"lease_{i:03d}"
            db.docs[f"leases/lease_{i:03d}"] = {
                "lease_id": f"lease_{i:03d}", "tenant_id": "eng", "task_id": task_id,
                "generation": 1, "heartbeat_at": BASE, "released_at": None,
            }
        db.docs[f"attempts/att_{i:03d}"] = _attempt(i, task_id)
        db.docs[f"events/ev_{i:03d}"] = {
            "event_id": f"ev_{i:03d}", "tenant_id": "eng", "task_id": task_id, "at": BASE,
            "generation": 1, "attempt_id": f"att_{i:03d}", "type": "RUNNING",
            "detail": {"cause": "account_assigned", "account_id": f"acct_{i % 3}",
                       "provider": "anthropic"},
        }
        if stored:
            doc["attempt_totals"] = _stored_totals(i)
            doc["current_account"] = _stored_account(i)


def _paths(meter) -> list[str]:  # noqa: F811
    """The request's reads from its page on: the caller's workspace and role
    reads before it are the auth dependency's, the same on every route."""
    paths = [path for path, _, _ in meter.projections]
    return paths[paths.index("tasks"):]


def test_a_200_row_page_of_stored_tasks_takes_three_round_trips(client, db, meter):  # noqa: F811
    _seed(db)
    meter.projections.clear()
    response = _list(client, limit=200, view="summary")
    assert response.status_code == 200, response.text
    rows = response.json()["tasks"]
    assert len(rows) == 200
    assert all(r["attempts"] == 1 and r["cost_usd_total"] == 0.25 for r in rows)
    assert all(r["attempts_read"] == "ok" for r in rows)
    assert all(r["account"]["status"] == "assigned" for r in rows)
    paths = _paths(meter)
    # MUTATION: keep deriving the totals and the accounts for every row.
    assert paths.count("attempts") == 0, paths
    assert paths.count("events") == 0, paths
    # The page, the lease-holding rows' leases, the READY rows' pools.
    assert paths.count("tasks") == 1
    assert len(paths) <= 3, paths


def test_the_stored_page_serves_what_the_derivation_serves(client, db):
    _seed(db, rows=25, stored=False)
    derived = {r["id"]: r for r in _list(client, limit=25).json()["tasks"]}
    task_accounts.FINISHED.clear()
    _seed(db, rows=25, stored=True)
    stored = {r["id"]: r for r in _list(client, limit=25).json()["tasks"]}
    keys = ("attempts", "attempts_with_cost", "cost_usd_total", "cost_incomplete",
            "duration_s_total", "duration_incomplete", "first_started_at",
            "last_attempt_cost_usd", "last_attempt_duration_s", "attempts_read", "account")
    for task_id, row in derived.items():
        assert {k: stored[task_id][k] for k in keys} == {k: row[k] for k in keys}, task_id
        # The stored maps themselves are never served raw.
        assert "attempt_totals" not in stored[task_id]
        assert "current_account" not in stored[task_id]


def test_an_old_task_without_the_fields_falls_back_to_the_derivation(client, db, meter):  # noqa: F811
    _seed(db, rows=40)
    for i in (3, 7):  # written before the fields: no stored copy at all
        db.docs[f"tasks/task_{i:03d}"].pop("attempt_totals")
        db.docs[f"tasks/task_{i:03d}"].pop("current_account")
    meter.projections.clear()
    rows = {r["id"]: r for r in _list(client, limit=40).json()["tasks"]}
    paths = _paths(meter)
    # One attempts query and one account query, for the two old rows only.
    assert paths.count("attempts") == 1, paths
    assert paths.count("events") == 1, paths
    for i in (3, 7):
        row = rows[f"task_{i:03d}"]
        assert row["attempts"] == 1 and row["cost_usd_total"] == 0.25
        assert row["account"]["account_id"] == f"acct_{i % 3}"


def test_a_stored_copy_from_an_earlier_generation_is_not_served(client, db, meter):  # noqa: F811
    """A retry's new attempt has not ended yet: its totals and its account are
    not the stored ones, so the row is derived."""
    _seed(db, rows=5)
    doc = db.docs["tasks/task_001"]  # RUNNING
    doc["current_generation"] = 2
    meter.projections.clear()
    row = {r["id"]: r for r in _list(client, limit=5).json()["tasks"]}["task_001"]
    paths = _paths(meter)
    assert paths.count("attempts") == 1 and paths.count("events") == 1, paths
    # Derived: generation 2 has no account event yet.
    assert row["account"]["status"] == "not_assigned_yet"


def test_a_stored_account_that_never_landed_reads_not_assigned_once_finished(client, db):
    _seed(db, rows=5)
    doc = db.docs["tasks/task_002"]  # SUCCEEDED
    doc["current_account"] = {**_stored_account(2), "status": "not_assigned_yet",
                              "account_id": None, "provider": None}
    row = {r["id"]: r for r in _list(client, limit=5).json()["tasks"]}["task_002"]
    assert row["account"]["status"] == "not_assigned"
