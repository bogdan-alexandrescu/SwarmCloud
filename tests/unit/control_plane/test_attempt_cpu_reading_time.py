"""The attempt's CPU reading is served with its time, its age and its limit's source (contract request #26).

The owner ACCEPTED request #26 on #184 (2026-09-26): `Attempt` gains
`cpu_measured_at` and `cpu_limit_source`. The API serves both on every
attempt row, and the reading's age against its own clock, as #188 served the
heartbeat reading's: an age computed in the browser would be a guess drawn as
a measurement (the #187 review).

What is pinned: the two fields round-trip and are served; the age is the
route's `read_at` minus the reading's time, on both attempt routes, and is
never negative; an attempt from before the change serves nulls, never a
guessed time or source.

MUTATIONS: drop either field from `attempt_from_dict` or `attempt_to_api`;
age against the attempt's end or its start; serve a negative age for a worker
clock ahead of the API's; fill a default source.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .conftest import auth_header, seed_task, seed_tenant


def a_task(db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")


def an_attempt(db, attempt_id, **fields):
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "created_at": started, "started_at": started + timedelta(seconds=10),
        "completed_at": None, "exit_code": None, "checkpoints": [],
        "cpu_seconds": 40.0, "peak_cpu_cores": 1.5, "mean_cpu_cores": 0.4, "cpu_limit_cores": 2.0,
        **fields,
    }


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def test_the_reading_is_served_with_its_time_its_source_and_its_age(client, db):
    a_task(db)
    measured = datetime.now(timezone.utc) - timedelta(seconds=20)
    an_attempt(db, "att_1", cpu_measured_at=measured, cpu_limit_source="cgroup")

    response = client.get("/v1/tasks/task_a/attempts", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    (row,) = body["attempts"]
    assert row["cpu_limit_source"] == "cgroup"
    assert abs((_parse(row["cpu_measured_at"]) - measured).total_seconds()) < 1
    read_at = _parse(body["read_at"])
    assert row["cpu_reading_age_seconds"] == round((read_at - measured).total_seconds(), 1)
    assert 19 <= row["cpu_reading_age_seconds"] <= 60


def test_the_tenant_wide_attempts_route_ages_it_the_same_way(client, db):
    a_task(db)
    measured = datetime.now(timezone.utc) - timedelta(seconds=90)
    an_attempt(db, "att_1", cpu_measured_at=measured, cpu_limit_source="resource_class")

    response = client.get("/v1/attempts", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    (row,) = body["attempts"]
    assert row["cpu_limit_source"] == "resource_class"
    assert row["cpu_reading_age_seconds"] == round((_parse(body["read_at"]) - measured).total_seconds(), 1)


def test_a_worker_clock_ahead_of_the_apis_is_not_a_reading_from_the_future(client, db):
    a_task(db)
    an_attempt(db, "att_1", cpu_measured_at=datetime.now(timezone.utc) + timedelta(seconds=30),
               cpu_limit_source="cgroup")
    (row,) = client.get("/v1/tasks/task_a/attempts", headers=auth_header("alice")).json()["attempts"]
    assert row["cpu_reading_age_seconds"] == 0


def test_an_attempt_from_before_the_change_serves_nulls_not_a_guess(client, db):
    a_task(db)
    an_attempt(db, "att_old")
    (row,) = client.get("/v1/tasks/task_a/attempts", headers=auth_header("alice")).json()["attempts"]
    assert row["cpu_measured_at"] is None
    assert row["cpu_limit_source"] is None
    assert row["cpu_reading_age_seconds"] is None
    assert row["cpu_seconds"] == 40.0, "the figures themselves are still served"
