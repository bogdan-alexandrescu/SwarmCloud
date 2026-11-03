"""`state/jev-usage` and `state/jev-settle`: the Jev ledger over the state routes.

WHY (docs/design/agent-quality/trusted-state.md §3). A tenant switched to
the state routes holds no Firestore client, so the worker's two Jev ledger
operations -- reading what the month and the task already spent
(`judge.attempt._carried`) and settling this attempt's calls into
`jev_usage` (`judge.attempt.settle`) -- had no path: the read counted the
month as spent (so the judgments never ran) and the settle wrote nothing (so
the spend went unrecorded). These routes serve both with every document id
derived from the attempt's tuple, the settle fenced as the direct path's is
(invariant 5) and idempotent on its request id. Offline.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from .test_attemptstate import (  # noqa: F401 - fixtures
    OTHER_WORKER,
    TASK,
    Worker,
    client,
    opened,
    worker_tokens,
)

NOW = datetime.now(timezone.utc)
MONTH = NOW.strftime("%Y-%m")
ROW = f"jev_usage/eng_{TASK}_g1"


def _settle(request_id: str = "s-1", **extra) -> dict:
    return {"request_id": request_id, "calls": 3, "input_tokens": 1200,
            "by_kind": {"J1": {"calls": 2, "input_tokens": 1000, "output_tokens": 40,
                               "outcomes": {"ok": 2}},
                        "J3": {"calls": 1, "input_tokens": 200, "output_tokens": None,
                               "outcomes": {"failed": 1, "skipped:cap": 1}}},
            **extra}


def test_settle_writes_this_attempts_row_and_adds_to_the_month(client, db):
    worker = Worker()
    opened(client, db, worker)
    db.docs[f"jev_usage/eng_{MONTH}"] = {"tenant_id": "eng", "month": MONTH,
                                         "input_tokens": 500}

    response = worker.post(client, "jev-settle", _settle())

    assert response.status_code == 200, response.text
    assert response.json()["settled"] == {"calls": 3, "input_tokens": 1200}
    assert db.docs[f"jev_usage/eng_{MONTH}"]["input_tokens"] == 1700
    row = db.docs[ROW]
    assert (row["kind"], row["tenant_id"], row["task_id"], row["generation"], row["month"]) == (
        "attempt", "eng", TASK, 1, MONTH)
    assert (row["calls"], row["input_tokens"]) == (3, 1200)
    assert row["by_kind"]["J1"] == {"calls": 2, "input_tokens": 1000, "output_tokens": 40,
                                    "outcomes": {"ok": 2}}
    assert row["by_kind"]["J3"]["outcomes"] == {"failed": 1, "skipped:cap": 1}
    assert row["settled_at"] is not None


def test_a_second_settle_adds_to_the_row_never_replaces_it(client, db):
    worker = Worker()
    opened(client, db, worker)
    worker.post(client, "jev-settle", _settle("s-1"))

    response = worker.post(client, "jev-settle", _settle("s-2"))

    assert response.status_code == 200, response.text
    row = db.docs[ROW]
    assert (row["calls"], row["input_tokens"]) == (6, 2400)
    assert row["by_kind"]["J1"]["calls"] == 4 and row["by_kind"]["J1"]["output_tokens"] == 80
    assert row["by_kind"]["J3"]["output_tokens"] is None
    assert db.docs[f"jev_usage/eng_{MONTH}"]["input_tokens"] == 2400


def test_a_repeated_settle_request_counts_once(client, db):
    """A retry after an answer the worker never received lands nothing twice."""
    worker = Worker()
    opened(client, db, worker)
    worker.post(client, "jev-settle", _settle("s-1"))
    before = copy.deepcopy(db.docs)

    response = worker.post(client, "jev-settle", _settle("s-1"))

    assert response.status_code == 200, response.text
    assert response.json()["repeated"] is True
    assert db.docs == before


def test_a_settle_of_nothing_writes_the_row_and_leaves_the_month(client, db):
    worker = Worker()
    opened(client, db, worker)

    response = worker.post(client, "jev-settle", {"request_id": "s-0", "calls": 1,
                                                  "input_tokens": 0, "by_kind": {}})

    assert response.status_code == 200, response.text
    assert f"jev_usage/eng_{MONTH}" not in db.docs
    assert db.docs[ROW]["calls"] == 1


def test_a_stale_generations_settle_is_409_and_writes_nothing(client, db):
    worker = Worker()
    opened(client, db, worker)
    db.docs[f"tasks/{TASK}"].update({"current_generation": 2, "current_lease_id": "lease_new"})
    before = copy.deepcopy(db.docs)

    response = worker.post(client, "jev-settle", _settle())

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "attempt_fenced"
    assert db.docs == before


@pytest.mark.parametrize("field", ["tenant", "task", "generation", "month", "settled_at",
                                   "collection", "document"])
def test_a_settle_that_names_a_document_is_422(client, db, field):
    """Every id is the tuple's: a body cannot point the write elsewhere."""
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)

    response = worker.post(client, "jev-settle", _settle(**{field: "research"}))

    assert response.status_code == 422, response.text
    assert db.docs == before


@pytest.mark.parametrize("bad", [
    {"calls": -1},
    {"input_tokens": -5},
    {"by_kind": {"J1": {"calls": 1, "input_tokens": 0, "outcomes": {"approved": 1}}}},
    {"by_kind": {"J1": {"calls": 1, "input_tokens": 0, "price_usd": 0}}},
    {"by_kind": {"bad kind!": {"calls": 1, "input_tokens": 0}}},
    {"by_kind": {f"K{i}": {"calls": 0, "input_tokens": 0} for i in range(40)}},
])
def test_a_malformed_settle_is_422(client, db, bad):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)

    response = worker.post(client, "jev-settle", {**_settle(), **bad})

    assert response.status_code == 422, response.text
    assert db.docs == before


def test_another_tenants_token_cannot_settle(client, db):
    worker = Worker()
    opened(client, db, worker)
    before = copy.deepcopy(db.docs)

    response = worker.post(client, "jev-settle", _settle(), token=OTHER_WORKER)

    assert response.status_code == 401, response.text
    assert db.docs == before


def test_usage_serves_this_tenants_month_and_this_tasks_rows_only(client, db):
    worker = Worker()
    opened(client, db, worker)
    db.docs[f"jev_usage/eng_{MONTH}"] = {"tenant_id": "eng", "month": MONTH,
                                         "input_tokens": 900}
    db.docs[f"jev_usage/research_{MONTH}"] = {"tenant_id": "research", "month": MONTH,
                                              "input_tokens": 7}
    rows = {
        f"eng_{TASK}_g1": {"kind": "attempt", "tenant_id": "eng", "task_id": TASK,
                           "calls": 2, "input_tokens": 300},
        f"eng_{TASK}_g0": {"kind": "attempt", "tenant_id": "eng", "task_id": TASK,
                           "calls": 1, "input_tokens": 100},
        "eng_task_other_g1": {"kind": "attempt", "tenant_id": "eng", "task_id": "task_other",
                              "calls": 50, "input_tokens": 5000},
        f"research_{TASK}_g1": {"kind": "attempt", "tenant_id": "research", "task_id": TASK,
                                "calls": 60, "input_tokens": 6000},
    }
    for doc_id, row in rows.items():
        db.docs[f"jev_usage/{doc_id}"] = row

    response = worker.get(client, "jev-usage")

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "month": MONTH, "month_used_tokens": 900,
                               "task_calls": 3, "task_input_tokens": 400}


def test_usage_of_an_unspent_month_is_zero_because_it_was_read(client, db):
    worker = Worker()
    opened(client, db, worker)

    answer = worker.get(client, "jev-usage").json()

    assert (answer["month_used_tokens"], answer["task_calls"], answer["task_input_tokens"]) == (
        0, 0, 0)


def test_usage_is_refused_without_the_attempt_proof(client, db):
    worker = Worker()
    opened(client, db, worker)

    assert worker.get(client, "jev-usage", sign=False).status_code == 403
    assert worker.get(client, "jev-usage", token=OTHER_WORKER).status_code == 401
