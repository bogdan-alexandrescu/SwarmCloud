"""`GET /v1/tasks/{id}/utilisation`: a task's CPU, memory and throttling over time (LIVE-UTIL-API, #1689).

The owner, 2026-10-20: close to real-time use of a running agent, not only its
limit and its peak, with throttling measured. The worker puts the 5-second
points it took since the previous beat on each HEARTBEAT event
(`agent_worker.lifecycle.Worker._usage_reading`); this route turns them into:

    {"task_id", "read_at",
     "attempts": [{"attempt_id", "generation", "cpu_limit_cores",
                   "memory_limit_bytes", "throttled_seconds",
                   "peak_throttled_share", "mean_throttled_share",
                   "points": [{"at", "cpu_cores", "memory_bytes", "throttled_share"}]}]}

WHAT EACH TEST PINS:

  * a point's `at` is its heartbeat's time less how long before the beat it
    was taken (`series_clock - t`), oldest point first, across heartbeats;
  * attempts newest first, each with its own limits and figures; an attempt
    with no heartbeat yet is served with no points;
  * None means not measured: a point the worker could not read is null;
  * another tenant's task is the same 404 as a missing one.

Over the in-memory Firestore, through the real app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 10, 20, 9, 0, 0, tzinfo=timezone.utc)
GIB = 1024**3

_seq = [0]


def _attempt(db, attempt_id: str, *, created: datetime, generation: int = 1,
             task_id: str = "task_u", tenant: str = "eng", **fields: Any) -> None:
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant,
        "generation": generation, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "created_at": created, "started_at": created, "completed_at": None,
        "exit_code": None, "checkpoints": [], **fields,
    }


def _beat(db, attempt_id: str, *, at: datetime, clock: float, points: list[dict[str, Any]],
          generation: int = 1, task_id: str = "task_u", tenant: str = "eng", **detail: Any) -> None:
    _seq[0] += 1
    event_id = f"evt_{_seq[0]:06d}"
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {
        "event_id": event_id, "task_id": task_id, "tenant_id": tenant, "type": "heartbeat",
        "at": at, "attempt_id": attempt_id, "lease_id": f"lease_{attempt_id}",
        "generation": generation,
        "detail": {"elapsed_seconds": clock, "cpu_seconds": 1.0, "cpu_source": "cgroup",
                   "series": points, "series_clock": clock, **detail},
    }


def _point(t: float, cpu: float | None, mem: int | None, thr: float | None) -> dict[str, Any]:
    return {"t": t, "cpu": cpu, "mem": mem, "thr": thr}


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read(client, task_id: str = "task_u", user: str = "alice"):
    return client.get(f"/v1/tasks/{task_id}/utilisation", headers=auth_header(user))


def test_heartbeat_points_become_one_timed_series(client, db):
    # MUTATION: compute `at` as the heartbeat time PLUS `t` in
    # `swarm_api.utilisation.points_of` -- every point lands in the future and fails.
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_u", tenant_id="eng", state="RUNNING")
    _attempt(db, "att_1", created=T0, cpu_limit_cores=4.0)
    # Two beats, written newest first so the route must order them itself.
    _beat(db, "att_1", at=T0 + timedelta(seconds=160), clock=160.0,
          points=[_point(155.0, 3.9, 6 * GIB, 0.6), _point(160.0, None, None, None)],
          memory_limit_bytes=8 * GIB, throttled_seconds=12.5,
          peak_throttled_share=0.6, mean_throttled_share=0.25)
    _beat(db, "att_1", at=T0 + timedelta(seconds=12), clock=10.0,
          points=[_point(5.0, 1.25, 2 * GIB, 0.0), _point(10.0, 2.5, 3 * GIB, 0.2)],
          memory_limit_bytes=8 * GIB, throttled_seconds=0.4,
          peak_throttled_share=0.2, mean_throttled_share=0.1)

    response = read(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == "task_u" and "read_at" in body
    (attempt,) = body["attempts"]
    assert attempt["attempt_id"] == "att_1" and attempt["generation"] == 1
    assert attempt["cpu_limit_cores"] == 4.0
    assert attempt["memory_limit_bytes"] == 8 * GIB
    # The newest beat's attempt figures, not the first one's.
    assert attempt["throttled_seconds"] == 12.5
    assert attempt["peak_throttled_share"] == 0.6
    assert attempt["mean_throttled_share"] == 0.25
    points = attempt["points"]
    assert [set(p) for p in points] == [{"at", "cpu_cores", "memory_bytes", "throttled_share"}] * 4
    # The second beat was written 2 s after its clock's 10.0: the points are
    # 7 s and 2 s before the beat, i.e. at T0+7 and T0+12.
    assert [_at(p["at"]) for p in points] == [
        T0 + timedelta(seconds=7), T0 + timedelta(seconds=12),
        T0 + timedelta(seconds=155), T0 + timedelta(seconds=160),
    ]
    assert [p["cpu_cores"] for p in points] == [1.25, 2.5, 3.9, None]
    assert [p["memory_bytes"] for p in points] == [2 * GIB, 3 * GIB, 6 * GIB, None]
    assert [p["throttled_share"] for p in points] == [0.0, 0.2, 0.6, None]


def test_attempts_are_newest_first_each_with_its_own_series(client, db):
    # MUTATION: serve the attempts in `list_attempts` order reversed -- the
    # retry's series is drawn as the first attempt's and this fails.
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_u", tenant_id="eng", state="RUNNING")
    _attempt(db, "att_1", created=T0, cpu_limit_cores=4.0)
    _attempt(db, "att_2", created=T0 + timedelta(minutes=30), generation=2, cpu_limit_cores=8.0)
    _attempt(db, "att_3", created=T0 + timedelta(minutes=40), generation=3)  # no beat yet
    _beat(db, "att_1", at=T0 + timedelta(seconds=10), clock=10.0,
          points=[_point(10.0, 1.0, GIB, 0.0)], memory_limit_bytes=8 * GIB)
    _beat(db, "att_2", at=T0 + timedelta(minutes=31), clock=60.0, generation=2,
          points=[_point(60.0, 6.0, 20 * GIB, 0.4)], memory_limit_bytes=32 * GIB)

    body = read(client).json()

    assert [a["attempt_id"] for a in body["attempts"]] == ["att_3", "att_2", "att_1"]
    newest, retry, first = body["attempts"]
    assert newest["points"] == [] and newest["cpu_limit_cores"] is None
    assert newest["memory_limit_bytes"] is None and newest["throttled_seconds"] is None
    assert retry["cpu_limit_cores"] == 8.0 and retry["memory_limit_bytes"] == 32 * GIB
    assert [p["cpu_cores"] for p in retry["points"]] == [6.0]
    assert [p["cpu_cores"] for p in first["points"]] == [1.0]


def test_a_beat_from_before_the_series_existed_contributes_no_points(client, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_u", tenant_id="eng", state="RUNNING")
    _attempt(db, "att_1", created=T0, cpu_limit_cores=4.0)
    _seq[0] += 1
    db.docs[f"tasks/task_u/events/evt_{_seq[0]:06d}"] = {
        "event_id": f"evt_{_seq[0]:06d}", "task_id": "task_u", "tenant_id": "eng",
        "type": "heartbeat", "at": T0, "attempt_id": "att_1", "generation": 1,
        "detail": {"elapsed_seconds": 5.0, "cpu_seconds": 1.0, "cpu_source": "cgroup",
                   "peak_rss_bytes": GIB, "checkpoints": 0},
    }

    response = read(client)

    assert response.status_code == 200, response.text
    (attempt,) = response.json()["attempts"]
    assert attempt["points"] == []
    assert attempt["throttled_seconds"] is None


def test_another_tenants_task_is_a_404_worded_as_a_missing_one(client, db):
    # MUTATION: read the task with `tenant_id=task.tenant_id` from the document
    # instead of the caller's `tenant_scope` -- bob reads eng's series and this fails.
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_u", tenant_id="eng", state="RUNNING")
    _attempt(db, "att_1", created=T0, cpu_limit_cores=4.0)
    _beat(db, "att_1", at=T0 + timedelta(seconds=10), clock=10.0,
          points=[_point(10.0, 1.0, GIB, 0.0)], memory_limit_bytes=8 * GIB)

    stolen = read(client, user="bob")
    missing = read(client, task_id="task_does_not_exist", user="bob")

    assert stolen.status_code == 404 and missing.status_code == 404
    assert stolen.json()["code"] == missing.json()["code"]
    assert "attempts" not in stolen.json()


def test_an_unauthenticated_read_is_refused(client, db):
    seed_task(db, task_id="task_u", tenant_id="eng", state="RUNNING")
    assert client.get("/v1/tasks/task_u/utilisation").status_code == 401
