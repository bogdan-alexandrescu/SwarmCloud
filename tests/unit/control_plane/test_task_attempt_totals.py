"""A task's spend and time across EVERY attempt, on the task the API serves.

MEASURED, 2026-10-05 (lane review P1): `swarm result` and the follow outcome
reported only the final attempt. UR1's implement step served $0.51 while its
two attempts cost $9.64; across 17 lanes $65.77 was served against $77.53
spent. `result_summary` is written by `finish()` for the attempt that ended
last, and the task's `started_at` is overwritten by every attempt's STARTING,
so nothing on the task document could say what the whole task cost.

Owner decision, 2026-10-05: the task the API serves carries `cost_usd_total`,
`attempts`, `duration_s_total` and `first_started_at`, derived from the
per-attempt records, with the last-attempt fields kept under their names. A
missing attempt cost makes the total a floor with `cost_incomplete: true` --
never a silent 0 -- and no recorded cost at all stays null.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_api.attempt_totals import attempt_totals
from swarm_api.codec import attempt_from_dict

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _attempt_doc(
    attempt_id: str,
    *,
    task_id: str = "t_two",
    generation: int = 1,
    started_minutes_ago: float,
    ran_seconds: float | None,
    cost_usd: float | None = None,
    tenant_id: str = "eng",
) -> dict[str, Any]:
    started = NOW - timedelta(minutes=started_minutes_ago)
    doc: dict[str, Any] = {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": tenant_id,
        "generation": generation,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": None,
        "created_at": started - timedelta(seconds=5),
        "started_at": started,
        "completed_at": None if ran_seconds is None else started + timedelta(seconds=ran_seconds),
        "exit_code": None if ran_seconds is None else 0,
        "error": None,
        "peak_rss_bytes": None,
        "oom_near_miss": False,
        "checkpoints": [],
    }
    # Absent, never zero, as `control.record_spend` writes it.
    if cost_usd is not None:
        doc["cost_usd"] = cost_usd
    return doc


def _seed(db, *docs: dict[str, Any]) -> None:
    for doc in docs:
        db.docs[f"attempts/{doc['attempt_id']}"] = doc


def _finished_task(db, task_id: str = "t_two", *, attempt_count: int) -> None:
    seed_task(db, task_id=task_id, tenant_id="eng", state="SUCCEEDED", runner_profile="claude-code")
    doc = db.docs[f"tasks/{task_id}"]
    doc["attempt_count"] = attempt_count
    # The LAST attempt's start and end, as the worker leaves them.
    doc["started_at"] = NOW - timedelta(minutes=10)
    doc["completed_at"] = NOW - timedelta(minutes=10) + timedelta(seconds=120)
    doc["result_summary"] = {"runner": {"usage": {"total_cost_usd": 0.51}}, "duration_seconds": 120.0}


def _get(client, task_id: str = "t_two") -> dict[str, Any]:
    response = client.get(f"/v1/tasks/{task_id}", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()["task"]


# --------------------------------------------------------------------------
# The served task
# --------------------------------------------------------------------------


def test_a_two_attempt_task_totals_both_attempts(client, db):
    """UR1's shape: a costly first attempt, a cheap second one."""
    seed_tenant(db, "eng")
    _finished_task(db, attempt_count=2)
    _seed(
        db,
        _attempt_doc("a1", generation=1, started_minutes_ago=60, ran_seconds=1800, cost_usd=9.13),
        _attempt_doc("a2", generation=2, started_minutes_ago=10, ran_seconds=120, cost_usd=0.51),
    )

    task = _get(client)

    assert task["attempts"] == 2
    assert task["cost_usd_total"] == 9.64, "both attempts' cost, not the last one's $0.51"
    assert task["cost_incomplete"] is False
    assert task["duration_s_total"] == 1920.0
    assert task["first_started_at"].startswith("2026-10-05T11:00:00")
    # The last attempt, beside the total and under its own name.
    assert task["last_attempt_cost_usd"] == 0.51
    assert task["last_attempt_duration_s"] == 120.0
    # The last-attempt fields keep their names and meaning.
    assert task["started_at"].startswith("2026-10-05T11:50:00")
    assert task["result_summary"]["runner"]["usage"]["total_cost_usd"] == 0.51


def test_a_missing_attempt_cost_makes_the_total_a_floor_never_a_silent_zero(client, db):
    seed_tenant(db, "eng")
    _finished_task(db, attempt_count=2)
    _seed(
        db,
        # The SIGTERMed CLI run: it spent, and recorded nothing.
        _attempt_doc("a1", generation=1, started_minutes_ago=60, ran_seconds=1800),
        _attempt_doc("a2", generation=2, started_minutes_ago=10, ran_seconds=120, cost_usd=0.51),
    )

    task = _get(client)

    assert task["cost_usd_total"] == 0.51, "at least $0.51: what was recorded, summed"
    assert task["cost_incomplete"] is True, "a total missing an attempt's cost must say it is a floor"
    assert task["attempts"] == 2


def test_no_recorded_cost_at_all_stays_null(client, db):
    seed_tenant(db, "eng")
    _finished_task(db, attempt_count=2)
    _seed(
        db,
        _attempt_doc("a1", generation=1, started_minutes_ago=60, ran_seconds=1800),
        _attempt_doc("a2", generation=2, started_minutes_ago=10, ran_seconds=120),
    )

    task = _get(client)

    assert task["cost_usd_total"] is None, "nothing recorded is null, never $0.00"
    assert task["cost_incomplete"] is True
    assert task["last_attempt_cost_usd"] is None


def test_a_single_attempt_task_totals_are_its_attempt(client, db):
    seed_tenant(db, "eng")
    _finished_task(db, attempt_count=1)
    _seed(db, _attempt_doc("a1", started_minutes_ago=10, ran_seconds=120, cost_usd=0.51))

    task = _get(client)

    assert (task["attempts"], task["cost_usd_total"], task["cost_incomplete"]) == (1, 0.51, False)
    assert task["duration_s_total"] == task["last_attempt_duration_s"] == 120.0
    assert task["last_attempt_cost_usd"] == 0.51
    assert task["first_started_at"] == task["started_at"]


def test_a_task_never_attempted_has_no_figures_and_zero_attempts(client, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t_new", tenant_id="eng", state="QUEUED")

    task = _get(client, "t_new")

    assert task["attempts"] == 0, "zero attempts is a count, and it was counted"
    assert task["cost_usd_total"] is None and task["duration_s_total"] is None
    assert task["first_started_at"] is None
    assert task["cost_incomplete"] is False


def test_another_tenants_attempt_is_never_summed(client, db):
    seed_tenant(db, "eng")
    _finished_task(db, attempt_count=1)
    _seed(
        db,
        _attempt_doc("a1", started_minutes_ago=10, ran_seconds=120, cost_usd=0.51),
        _attempt_doc("x1", started_minutes_ago=90, ran_seconds=60, cost_usd=50.0, tenant_id="other"),
    )

    task = _get(client)

    assert task["attempts"] == 1 and task["cost_usd_total"] == 0.51


def test_the_workflow_read_serves_each_step_tasks_totals(client, db):
    """The console's Workflows cost columns read the tasks this route serves."""
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
    _seed(
        db,
        _attempt_doc("w1", task_id=task_id, generation=1, started_minutes_ago=30, ran_seconds=60, cost_usd=1.25),
        _attempt_doc("w2", task_id=task_id, generation=2, started_minutes_ago=5, ran_seconds=30, cost_usd=0.25),
    )

    got = client.get(f"/v1/workflows/{workflow['workflow_id']}", headers=alice)

    assert got.status_code == 200, got.text
    (row,) = [t for t in got.json()["tasks"] if t["id"] == task_id]
    assert (row["attempts"], row["cost_usd_total"], row["last_attempt_cost_usd"]) == (2, 1.5, 0.25)


# --------------------------------------------------------------------------
# The derivation itself
# --------------------------------------------------------------------------


def test_an_attempt_still_running_leaves_the_duration_incomplete():
    attempts = [
        attempt_from_dict(_attempt_doc("a1", generation=1, started_minutes_ago=60, ran_seconds=600, cost_usd=1.0)),
        attempt_from_dict(_attempt_doc("a2", generation=2, started_minutes_ago=5, ran_seconds=None)),
    ]

    got = attempt_totals(attempts)

    assert got["duration_s_total"] == 600.0
    assert got["duration_incomplete"] is True
    assert got["last_attempt_duration_s"] is None, "the running attempt has not ended"
    assert got["cost_usd_total"] == 1.0 and got["cost_incomplete"] is True


def test_the_last_attempt_is_the_newest_generation_whatever_the_order_read():
    attempts = [
        attempt_from_dict(_attempt_doc("a2", generation=2, started_minutes_ago=10, ran_seconds=120, cost_usd=0.51)),
        attempt_from_dict(_attempt_doc("a1", generation=1, started_minutes_ago=60, ran_seconds=1800, cost_usd=9.13)),
    ]

    got = attempt_totals(list(reversed(attempts)))

    assert got["last_attempt_cost_usd"] == 0.51
    assert got["first_started_at"] == NOW - timedelta(minutes=60)


def test_a_failed_attempt_read_is_unread_never_zero_attempts():
    from swarm_api.attempt_totals import totals_for

    class _Broken:
        def collection(self, name):
            raise RuntimeError("firestore unavailable")

    got = totals_for(_Broken(), "eng", ["t_two"])["t_two"]

    assert got["attempts_read"] == "failed"
    assert got["attempts"] is None, "an unread task is not a task with no attempts"
    assert got["cost_usd_total"] is None and got["cost_incomplete"] is None
