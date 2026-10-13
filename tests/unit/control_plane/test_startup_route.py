"""`GET /v1/tasks/{id}/startup`: the current attempt's start-up steps and an estimate (STARTUP-API).

The owner, 2026-10-13: a progress bar while a browser agent starts. The
console (lane STARTUP-UI) codes against exactly this shape:

    {"attempt_id", "generation", "steps": [{"step", "at"}],
     "estimate": {"<step>": median seconds from dispatched}, "sample_size", "done"}

WHAT EACH TEST PINS:

  * the steps are the CURRENT attempt's `lease_acquired`, `dispatched`,
    `starting`, `running` and every `startup_progress` step, each once at its
    earliest time, in the order they happened -- an earlier attempt's, and a
    stale generation's, are not served;
  * the estimate is the median from `dispatched` over the profile's completed
    start-ups (those that reached `first_frame`) of the caller's tenant, at
    most 20; no history is `{}` and 0;
  * `done` is `first_frame` seen, or a task no longer starting or running;
  * the route has GET /v1/tasks/{id}'s scope: another tenant's task is 404,
    worded as a missing one.

Over the in-memory Firestore, through the real app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_api.startup import ESTIMATE_SAMPLE

from .conftest import auth_header, seed_task

PROFILE = "claude-code-browser"
T0 = datetime(2026, 10, 13, 9, 0, 0, tzinfo=timezone.utc)

STAGES = ("lease_acquired", "dispatched", "starting", "running")
STEPS = ("pod_scheduled", "image_pulling", "image_pulled", "container_started",
         "browser_launching", "browser_ready", "first_frame")

#: Seconds after `dispatched` each step happened, in one complete start-up.
OFFSETS = {
    "lease_acquired": -1, "dispatched": 0, "pod_scheduled": 2, "image_pulling": 4,
    "browser_launching": 30, "browser_ready": 32, "image_pulled": 28, "container_started": 40,
    "starting": 42, "running": 45, "first_frame": 50,
}

_seq = [0]


def _event(db, task_id: str, *, step: str, attempt: str, generation: int, at: datetime,
           tenant: str = "eng", detail: dict[str, Any] | None = None) -> None:
    _seq[0] += 1
    event_id = f"evt_{_seq[0]:06d}"
    if step in STAGES:
        kind, body = step, dict(detail or {})
    else:
        kind, body = "startup_progress", {"step": step, "attempt_id": attempt, "generation": generation}
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {
        "event_id": event_id, "task_id": task_id, "tenant_id": tenant, "type": kind, "at": at,
        "attempt_id": attempt, "lease_id": f"lease_{attempt}", "generation": generation, "detail": body,
    }


def seed_running(db, task_id: str, *, attempt: str = "att_cur", generation: int = 1, state: str = "RUNNING",
                 tenant: str = "eng", created: datetime | None = None) -> dict[str, Any]:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state=state, runner_profile=PROFILE,
                    created_at=created)
    doc["current_lease_id"] = f"lease_{attempt}"
    doc["current_generation"] = generation
    db.docs[f"leases/lease_{attempt}"] = {
        "lease_id": f"lease_{attempt}", "task_id": task_id, "attempt_id": attempt, "tenant_id": tenant,
        "generation": generation, "pools": [], "units": 1, "state": state, "created_at": T0,
        "released_at": None,
    }
    return doc


def seed_startup(db, task_id: str, *, attempt: str, generation: int = 1, start: datetime = T0,
                 steps: tuple[str, ...] = STAGES + STEPS, scale: float = 1.0, tenant: str = "eng") -> None:
    for step in steps:
        _event(db, task_id, step=step, attempt=attempt, generation=generation, tenant=tenant,
               at=start + timedelta(seconds=OFFSETS[step] * scale))


def read(client, task_id: str, user: str = "alice"):
    return client.get(f"/v1/tasks/{task_id}/startup", headers=auth_header(user))


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def test_the_current_attempts_steps_in_the_order_they_happened(client, db):
    seed_running(db, "task_now")
    seed_startup(db, "task_now", attempt="att_cur",
                 steps=("lease_acquired", "dispatched", "pod_scheduled", "image_pulling"))

    response = read(client, "task_now")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"attempt_id", "generation", "steps", "estimate", "sample_size", "done"}
    assert body["attempt_id"] == "att_cur" and body["generation"] == 1
    assert [s["step"] for s in body["steps"]] == ["lease_acquired", "dispatched", "pod_scheduled", "image_pulling"]
    assert _at(body["steps"][2]["at"]) == T0 + timedelta(seconds=2)
    assert body["done"] is False


def test_an_earlier_attempts_steps_and_a_stale_generation_are_not_served(client, db):
    seed_running(db, "task_retry", attempt="att_two", generation=2)
    seed_startup(db, "task_retry", attempt="att_one", generation=1)  # the first attempt, complete
    seed_startup(db, "task_retry", attempt="att_two", generation=2, start=T0 + timedelta(minutes=10),
                 steps=("lease_acquired", "dispatched"))
    # A late write by the first attempt's worker, carrying the new attempt's id
    # but its own generation, must not show as this attempt's first frame.
    _event(db, "task_retry", step="first_frame", attempt="att_two", generation=1,
           at=T0 + timedelta(minutes=11))

    body = read(client, "task_retry").json()

    assert body["attempt_id"] == "att_two" and body["generation"] == 2
    assert [s["step"] for s in body["steps"]] == ["lease_acquired", "dispatched"]
    assert body["done"] is False


def test_each_step_is_served_once_at_its_earliest_time(client, db):
    """The worker writes `running` again as start-up marks (`cause`); the bar wants the first."""
    seed_running(db, "task_marks")
    seed_startup(db, "task_marks", attempt="att_cur", steps=("dispatched", "running"))
    _event(db, "task_marks", step="running", attempt="att_cur", generation=1,
           at=T0 + timedelta(seconds=90), detail={"cause": "account_assigned"})

    steps = read(client, "task_marks").json()["steps"]

    assert [s["step"] for s in steps] == ["dispatched", "running"]
    assert _at(steps[1]["at"]) == T0 + timedelta(seconds=45)


def test_first_frame_ends_the_start_up(client, db):
    seed_running(db, "task_up")
    seed_startup(db, "task_up", attempt="att_cur")

    body = read(client, "task_up").json()

    assert body["done"] is True
    assert [s["step"] for s in body["steps"]][-1] == "first_frame"
    assert len(body["steps"]) == len(STAGES + STEPS)


def test_a_task_that_ended_is_done_without_a_first_frame(client, db):
    seed_running(db, "task_gone", state="FAILED")
    seed_startup(db, "task_gone", attempt="att_cur", steps=("lease_acquired", "dispatched"))

    assert read(client, "task_gone").json()["done"] is True


def test_a_task_with_no_lease_has_no_steps(client, db):
    seed_task(db, task_id="task_waiting", tenant_id="eng", state="READY", runner_profile=PROFILE)

    body = read(client, "task_waiting").json()

    assert body["attempt_id"] is None and body["steps"] == []
    assert body["done"] is True


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------


def test_no_history_is_an_empty_estimate(client, db):
    seed_running(db, "task_first")
    seed_startup(db, "task_first", attempt="att_cur", steps=("dispatched",))

    body = read(client, "task_first").json()

    assert body["estimate"] == {} and body["sample_size"] == 0


def test_the_estimate_is_the_median_from_dispatched_of_completed_start_ups(client, db):
    # Three past start-ups at 1x, 2x and 3x the offsets: the median is 2x.
    for n, scale in enumerate((1.0, 2.0, 3.0)):
        seed_running(db, f"task_past_{n}", attempt=f"att_past_{n}", state="SUCCEEDED",
                     created=T0 - timedelta(hours=n + 1))
        seed_startup(db, f"task_past_{n}", attempt=f"att_past_{n}", scale=scale,
                     start=T0 - timedelta(hours=n + 1))
    # One that never reached its first frame: not a completed start-up.
    seed_running(db, "task_stuck", attempt="att_stuck", state="FAILED", created=T0 - timedelta(hours=5))
    seed_startup(db, "task_stuck", attempt="att_stuck", start=T0 - timedelta(hours=5),
                 steps=("dispatched", "pod_scheduled"), scale=100.0)
    # Another profile's start-up: not this profile's estimate.
    other = seed_task(db, task_id="task_other_profile", tenant_id="eng", state="SUCCEEDED",
                      runner_profile="claude-code", created_at=T0 - timedelta(hours=6))
    other["current_lease_id"] = None
    seed_startup(db, "task_other_profile", attempt="att_other", scale=50.0)
    seed_running(db, "task_now")

    body = read(client, "task_now").json()

    assert body["sample_size"] == 3
    assert body["estimate"]["first_frame"] == 100.0
    assert body["estimate"]["pod_scheduled"] == 4.0
    assert body["estimate"]["running"] == 90.0
    assert "dispatched" not in body["estimate"] and "lease_acquired" not in body["estimate"]
    assert set(body["estimate"]) == set(STEPS) | {"starting", "running"}


def test_another_tenants_start_ups_are_not_in_the_estimate(client, db):
    seed_running(db, "task_research", attempt="att_research", state="SUCCEEDED", tenant="research")
    seed_startup(db, "task_research", attempt="att_research", tenant="research")
    seed_running(db, "task_now")

    body = read(client, "task_now").json()

    assert body["estimate"] == {} and body["sample_size"] == 0


def test_the_estimate_reads_at_most_the_last_twenty(client, db):
    for n in range(ESTIMATE_SAMPLE + 5):
        seed_running(db, f"task_many_{n}", attempt=f"att_many_{n}", state="SUCCEEDED",
                     created=T0 - timedelta(minutes=n + 1))
        seed_startup(db, f"task_many_{n}", attempt=f"att_many_{n}", start=T0 - timedelta(minutes=n + 1))
    seed_running(db, "task_now")

    assert read(client, "task_now").json()["sample_size"] == ESTIMATE_SAMPLE


def test_the_estimate_is_cached_per_tenant_and_profile(client, db, api_context):
    seed_running(db, "task_now")
    assert read(client, "task_now").json()["sample_size"] == 0

    seed_running(db, "task_later", attempt="att_later", state="SUCCEEDED")
    seed_startup(db, "task_later", attempt="att_later")

    assert read(client, "task_now").json()["sample_size"] == 0, "served from the cache"
    api_context.startup_estimates._cache.clear()
    assert read(client, "task_now").json()["sample_size"] == 1


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_another_tenants_task_is_a_404_worded_as_a_missing_one(client, db):
    seed_running(db, "task_eng")
    seed_startup(db, "task_eng", attempt="att_cur")

    stolen = read(client, "task_eng", user="bob")
    missing = read(client, "task_does_not_exist", user="bob")

    assert stolen.status_code == 404 and missing.status_code == 404
    assert stolen.json()["code"] == missing.json()["code"]
    assert "attempt_id" not in stolen.json() and "steps" not in stolen.json()


def test_an_unauthenticated_read_is_refused(client, db):
    seed_running(db, "task_eng")
    assert client.get("/v1/tasks/task_eng/startup").status_code == 401
