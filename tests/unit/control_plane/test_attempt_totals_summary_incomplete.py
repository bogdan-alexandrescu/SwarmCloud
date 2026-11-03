"""A cost the worker says it could not read in full is served as a floor.

WHY (owner's pick "Worker follow-up", 2026-11-03, part of #2688). The worker
writes `result_summary.cost_incomplete: true` when a session's usage was not
read (a session the context ceiling stopped and restarted). The served
`cost_incomplete` only counted attempts that recorded no `cost_usd`, so a step
whose last attempt recorded a PARTIAL cost was served as complete, and the
console drew its total without "at least". Now:

* every attempt recorded a cost, the summary says incomplete -> served true;
* the same task with no such key                            -> served false;
* the attempts could not be read                            -> every figure
  null and `attempts_read: "failed"`, unchanged.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api.attempt_totals import attempt_totals, unread_totals, with_totals

from .conftest import auth_header, seed_tenant
from .test_task_attempt_totals import _attempt_doc, _finished_task, _get, _seed


def _two_costed_attempts(db, task_id: str = "t_two", *, flagged: bool | None) -> None:
    """A finished task whose two attempts both recorded a cost; its summary
    says `cost_incomplete` when `flagged` is not None."""
    _finished_task(db, task_id, attempt_count=2)
    if flagged is not None:
        db.docs[f"tasks/{task_id}"]["result_summary"]["cost_incomplete"] = flagged
    _seed(
        db,
        _attempt_doc(f"{task_id}_a1", task_id=task_id, generation=1, started_minutes_ago=60,
                     ran_seconds=1800, cost_usd=9.13),
        _attempt_doc(f"{task_id}_a2", task_id=task_id, generation=2, started_minutes_ago=10,
                     ran_seconds=120, cost_usd=0.51),
    )


def test_a_summary_marked_incomplete_serves_the_total_as_a_floor(client, db):
    seed_tenant(db, "eng")
    _two_costed_attempts(db, flagged=True)

    task = _get(client)

    assert task["attempts_with_cost"] == 2, "every attempt recorded a cost"
    assert task["cost_usd_total"] == 9.64
    assert task["cost_incomplete"] is True, "the worker said a session's usage was not read"


def test_the_same_task_without_the_key_is_served_complete(client, db):
    seed_tenant(db, "eng")
    _two_costed_attempts(db, flagged=None)

    task = _get(client)

    assert task["cost_usd_total"] == 9.64
    assert task["cost_incomplete"] is False


def test_a_summary_marked_complete_keeps_todays_answer(client, db):
    seed_tenant(db, "eng")
    _two_costed_attempts(db, flagged=False)

    assert _get(client)["cost_incomplete"] is False


@pytest.mark.parametrize("view", [None, "full", "summary"])
def test_every_list_view_serves_the_floor(client, db, view):
    """The summary view drops `result_summary` from the row, and the console's
    Overview reads that view: the flag still reaches the row."""
    seed_tenant(db, "eng")
    _two_costed_attempts(db, "t_flagged", flagged=True)
    _two_costed_attempts(db, "t_plain", flagged=None)
    params = {"view": view} if view else {}

    response = client.get("/v1/tasks", headers=auth_header("alice"), params=params)

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["tasks"]}
    assert rows["t_flagged"]["cost_incomplete"] is True
    assert rows["t_plain"]["cost_incomplete"] is False
    assert rows["t_flagged"]["cost_usd_total"] == 9.64


def test_the_workflow_read_serves_the_floor(client, db):
    """The console's step DAG (`dag.ts` totalCostOf) reads these rows."""
    seed_tenant(db, "eng")
    alice = auth_header("alice")
    created = client.post(
        "/v1/workflows",
        headers=alice,
        json={"steps": [{"step_id": "build", "runner_profile": "mock"}]},
    )
    assert created.status_code == 201, created.text
    workflow = created.json()["workflow"]
    task_id = workflow["steps"][0]["task_id"]
    db.docs[f"tasks/{task_id}"]["result_summary"] = {"cost_incomplete": True}
    _seed(
        db,
        _attempt_doc("w1", task_id=task_id, generation=1, started_minutes_ago=30, ran_seconds=60, cost_usd=1.25),
    )

    got = client.get(f"/v1/workflows/{workflow['workflow_id']}", headers=alice)

    assert got.status_code == 200, got.text
    (row,) = [t for t in got.json()["tasks"] if t["id"] == task_id]
    assert (row["cost_usd_total"], row["cost_incomplete"]) == (1.25, True)


def test_an_unread_attempts_read_stays_null_whatever_the_summary_says(client, db, monkeypatch):
    seed_tenant(db, "eng")
    _two_costed_attempts(db, flagged=True)
    original = db.collection

    def broken(path: str):
        if path == "attempts":
            raise RuntimeError("firestore unavailable")
        return original(path)

    monkeypatch.setattr(db, "collection", broken)

    for task in (_get(client), *client.get("/v1/tasks", headers=auth_header("alice")).json()["tasks"]):
        assert task["attempts_read"] == "failed"
        assert task["cost_incomplete"] is None, "an unread task has no figure, not a floor"
        assert task["cost_usd_total"] is None


def test_with_totals_folds_the_rows_own_summary_flag():
    """Pure: the row's summary flag over read totals; never over unread ones."""
    read = {**attempt_totals([]), "attempts_read": "ok"}
    flagged: dict[str, Any] = {"id": "t", "result_summary": {"cost_incomplete": True}}
    plain: dict[str, Any] = {"id": "t", "result_summary": {"cost_incomplete": "yes"}}

    assert with_totals(dict(flagged), {"t": read})["cost_incomplete"] is True
    assert with_totals(dict(plain), {"t": read})["cost_incomplete"] is False, "only a literal true"
    assert with_totals(dict(flagged), {})["cost_incomplete"] is None
    assert with_totals(dict(flagged), {"t": unread_totals()})["cost_incomplete"] is None
    assert read["cost_incomplete"] is False, "the shared totals are not mutated"
