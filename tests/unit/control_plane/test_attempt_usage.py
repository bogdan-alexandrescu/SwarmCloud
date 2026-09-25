"""`GET /v1/tasks/{id}/attempts?include=usage` -- CPU peak and mean for the Details bar.

#184: Details draws memory and workspace against their limits, and the owner
asked for CPU beside them -- the PEAK and the MEAN cores, as a fraction of the
runtime's CPU limit. The frozen `Attempt` has no CPU fields (contract request
#15), so the worker puts them on its HEARTBEAT events and this opt-in block
reads them back: per attempt, the newest reading, with a status that says
exactly how much that reading can be trusted.

What is pinned:

  * OPT-IN. Without `include=usage` the rows are byte-for-byte what they were;
    the Overview's spend rollup calls this route once per task and must not
    pay for an events read.
  * SEVEN STATUSES, never collapsed: final, live, last_reading, never_ran,
    absent, beyond_window, unread -- and a failed events read still answers
    200, because the attempt rows are real.
  * NULL IS NEVER ZERO, and a reading is the NEWEST one for its attempt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)

FIGURES = {
    "cpu_seconds": 42.5,
    "peak_cpu_cores": 1.875,
    "mean_cpu_cores": 0.472,
    "cpu_wall_seconds": 90.041,
    "cpu_source": "cgroup",
    "cpu_limit_cores": 4.0,
    "cpu_limit_source": "cgroup",
    "peak_rss_bytes": 734003200,
}


def a_task(db, *, state="SUCCEEDED", current_lease_id=None):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state=state)
    db.docs["tasks/task_a"]["current_lease_id"] = current_lease_id
    db.docs["tasks/task_a"]["updated_at"] = T0 + timedelta(hours=5)


def an_attempt(db, attempt_id, *, created, started=True, completed=False):
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "created_at": created,
        "started_at": created + timedelta(seconds=10) if started else None,
        "completed_at": created + timedelta(minutes=30) if completed else None,
        "exit_code": 0 if completed else None,
        "checkpoints": [],
    }


def an_event(db, event_id, *, at, attempt_id, kind="heartbeat", detail=None):
    db.docs[f"tasks/task_a/events/{event_id}"] = {
        "event_id": event_id, "task_id": "task_a", "tenant_id": "eng",
        "type": kind, "at": at, "attempt_id": attempt_id,
        "lease_id": f"lease_{attempt_id}", "generation": 1,
        "detail": detail if detail is not None else {},
    }


def reading(**overrides):
    detail = {"elapsed_seconds": 60.0, "checkpoints": 1, "final": False, **FIGURES}
    detail.update(overrides)
    return detail


def get(client, query="?include=usage", user="alice"):
    return client.get(f"/v1/tasks/task_a/attempts{query}", headers=auth_header(user))


def usage_of(body, attempt_id):
    return next(row["usage"] for row in body["attempts"] if row["attempt_id"] == attempt_id)


def _parse(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def test_without_include_the_rows_are_what_they_always_were(client, db):
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)
    an_event(db, "ev_1", at=T0 + timedelta(minutes=1), attempt_id="att_1", detail=reading())

    body = get(client, query="").json()
    assert "usage_read" not in body
    assert all("usage" not in row for row in body["attempts"])


def test_an_unknown_include_is_refused(client, db):
    a_task(db)
    response = get(client, query="?include=cpu")
    assert response.status_code == 422, response.text


def test_a_final_reading_is_served_whole_with_its_age(client, db):
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)
    an_event(db, "ev_1", at=T0 + timedelta(minutes=5), attempt_id="att_1",
             detail=reading(cpu_seconds=10.0))
    an_event(db, "ev_2", at=T0 + timedelta(minutes=29), attempt_id="att_1",
             detail=reading(final=True))

    response = get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    usage = usage_of(body, "att_1")
    assert usage["status"] == "final"
    assert usage["final"] is True
    assert usage["event_id"] == "ev_2"
    for key, value in FIGURES.items():
        assert usage[key] == value, key
    measured = _parse(usage["measured_at"])
    assert measured == T0 + timedelta(minutes=29)
    read_at = _parse(body["usage_read"]["read_at"])
    assert usage["age_seconds"] == round((read_at - measured).total_seconds(), 3)
    assert body["usage_read"]["status"] == "ok"
    assert body["usage_read"]["events_examined"] == 2
    assert body["usage_read"]["window_full"] is False


def test_a_running_attempts_periodic_reading_is_live(client, db):
    a_task(db, state="RUNNING", current_lease_id="lease_att_1")
    an_attempt(db, "att_1", created=T0)
    an_event(db, "ev_1", at=T0 + timedelta(minutes=3), attempt_id="att_1", detail=reading())

    usage = usage_of(get(client).json(), "att_1")
    assert usage["status"] == "live"
    assert usage["final"] is False


def test_an_attempt_that_ended_without_a_final_reading_shows_its_last(client, db):
    """Killed or reclaimed: the periodic reading is the last word, and says so."""
    a_task(db, state="FAILED")
    an_attempt(db, "att_1", created=T0, completed=True)
    an_event(db, "ev_1", at=T0 + timedelta(minutes=3), attempt_id="att_1", detail=reading())

    assert usage_of(get(client).json(), "att_1")["status"] == "last_reading"


def test_the_newest_reading_per_attempt_wins_and_other_attempts_do_not_count(client, db):
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)
    an_attempt(db, "att_2", created=T0 + timedelta(hours=1), completed=True)
    an_event(db, "ev_a", at=T0 + timedelta(minutes=1), attempt_id="att_1",
             detail=reading(cpu_seconds=1.0))
    an_event(db, "ev_b", at=T0 + timedelta(minutes=20), attempt_id="att_1",
             detail=reading(cpu_seconds=2.0, final=True))
    an_event(db, "ev_c", at=T0 + timedelta(hours=1, minutes=5), attempt_id="att_2",
             detail=reading(cpu_seconds=3.0, final=True))
    an_event(db, "ev_d", at=T0 + timedelta(hours=1, minutes=6), attempt_id="att_2",
             kind="succeeded", detail={"cpu_seconds": 99.0})

    body = get(client).json()
    assert usage_of(body, "att_1")["cpu_seconds"] == 2.0
    assert usage_of(body, "att_1")["event_id"] == "ev_b"
    assert usage_of(body, "att_2")["cpu_seconds"] == 3.0, "only HEARTBEAT events are readings"


def test_a_heartbeat_without_the_cpu_key_is_not_a_reading(client, db):
    """A worker from before CPU was measured wrote heartbeats with no
    `cpu_seconds` key at all. That is not a reading of zero."""
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)
    an_event(db, "ev_1", at=T0 + timedelta(minutes=1), attempt_id="att_1",
             detail={"elapsed_seconds": 60.0, "peak_rss_bytes": 1})

    usage = usage_of(get(client).json(), "att_1")
    assert usage["status"] == "absent"
    assert usage["cpu_seconds"] is None
    assert usage["peak_cpu_cores"] is None


def test_an_all_null_reading_is_a_reading_with_nothing_measured(client, db):
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)
    nothing = {key: None for key in FIGURES}
    an_event(db, "ev_1", at=T0 + timedelta(minutes=1), attempt_id="att_1",
             detail={**nothing, "final": True})

    usage = usage_of(get(client).json(), "att_1")
    assert usage["status"] == "final"
    assert all(usage[key] is None for key in FIGURES), usage


def test_an_attempt_that_never_started_never_ran(client, db):
    a_task(db, state="READY")
    an_attempt(db, "att_1", created=T0, started=False)

    usage = usage_of(get(client).json(), "att_1")
    assert usage["status"] == "never_ran"
    assert usage["cpu_seconds"] is None


def test_a_window_that_covers_the_attempt_and_finds_nothing_is_absent(client, db):
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)
    an_event(db, "ev_1", at=T0 + timedelta(minutes=1), attempt_id="att_1", kind="running")

    assert usage_of(get(client).json(), "att_1")["status"] == "absent"


def test_a_full_window_that_does_not_reach_the_attempt_is_beyond_window(client, db):
    """200 newer events filled the one read. The older attempt's reading, if it
    has one, is further back -- that is not "absent"."""
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)
    an_attempt(db, "att_2", created=T0 + timedelta(hours=1), completed=True)
    an_event(db, "ev_old", at=T0 + timedelta(minutes=1), attempt_id="att_1",
             detail=reading(final=True))
    for n in range(200):
        an_event(db, f"ev_{n:03d}", at=T0 + timedelta(hours=1, seconds=n + 1),
                 attempt_id="att_2", detail=reading(cpu_seconds=float(n)))

    body = get(client).json()
    assert body["usage_read"]["window_full"] is True
    assert body["usage_read"]["events_examined"] == 200
    assert usage_of(body, "att_1")["status"] == "beyond_window"
    newest = usage_of(body, "att_2")
    assert newest["cpu_seconds"] == 199.0
    assert newest["status"] == "last_reading"


def test_a_failed_events_read_marks_every_block_unread_and_still_answers(
    client, db, api_context
):
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)
    an_attempt(db, "att_2", created=T0 + timedelta(hours=1), completed=True)

    def broken(*_args, **_kwargs):
        raise RuntimeError("firestore is on fire")

    api_context.store.list_events = broken
    response = get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["usage_read"]["status"] == "unread"
    assert "on fire" in body["usage_read"]["detail"]
    assert [row["usage"]["status"] for row in body["attempts"]] == ["unread", "unread"]
    assert all(row["usage"]["cpu_seconds"] is None for row in body["attempts"])


def test_another_tenants_attempts_are_the_404_a_missing_task_gets(client, db):
    a_task(db)
    an_attempt(db, "att_1", created=T0, completed=True)

    response = get(client, user="bob")
    assert response.status_code == 404, response.text
