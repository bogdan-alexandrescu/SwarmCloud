"""The attempt's CPU is four typed fields on its document, served on every row.

Contract request #15, ACCEPTED by the owner on #184 (2026-09-25): `Attempt`
gains `cpu_seconds`, `peak_cpu_cores`, `mean_cpu_cores` and `cpu_limit_cores`,
and "they replace the interim heartbeat-event path". That path was #188's:
the worker put the figures on HEARTBEAT events and
`GET /v1/tasks/{id}/attempts?include=usage` read them back with one
descending events read per request.

What is pinned:

  * THE FOUR FIELDS ROUND-TRIP: written on the attempt document, decoded by
    `attempt_from_dict`, served by `attempt_to_api` -- on EVERY row, with no
    opt-in, because serving them costs no read the route was not already
    making. (The spend fields were once dropped by the decoder and served as
    null for every attempt that ever ran; `test_api_contract_shapes.py` is
    the general guard, this is the CPU case.)
  * NULL IS NOT ZERO. A field the worker never wrote is null; a measured 0.0
    stays 0.0.
  * THE EVENTS PATH IS GONE. The route reads no events for CPU, with or
    without `include=usage`: a task whose events cannot be read at all still
    serves its attempts, and no row carries the interim `usage` block.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
CPU = ("cpu_seconds", "peak_cpu_cores", "mean_cpu_cores", "cpu_limit_cores")


def a_task(db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")


def an_attempt(db, attempt_id, **fields):
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "created_at": T0, "started_at": T0 + timedelta(seconds=10),
        "completed_at": T0 + timedelta(minutes=30), "exit_code": 0, "checkpoints": [],
        **fields,
    }


def rows(client, query=""):
    response = client.get(f"/v1/tasks/task_a/attempts{query}", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return {row["attempt_id"]: row for row in response.json()["attempts"]}


def test_the_four_fields_are_served_on_every_row(client, db):
    a_task(db)
    an_attempt(
        db, "att_1",
        cpu_seconds=402.311, peak_cpu_cores=1.62, mean_cpu_cores=0.842, cpu_limit_cores=2.0,
    )
    row = rows(client)["att_1"]
    assert row["cpu_seconds"] == 402.311
    assert row["peak_cpu_cores"] == 1.62
    assert row["mean_cpu_cores"] == 0.842
    assert row["cpu_limit_cores"] == 2.0


def test_a_field_the_worker_never_wrote_is_null_and_a_measured_zero_stays_zero(client, db):
    a_task(db)
    an_attempt(db, "att_old")
    an_attempt(db, "att_idle", cpu_seconds=0.0, peak_cpu_cores=0.0, mean_cpu_cores=0.0,
               cpu_limit_cores=1.0)
    served = rows(client)
    for field in CPU:
        assert served["att_old"][field] is None, f"{field} was invented for an attempt with none"
    for field in ("cpu_seconds", "peak_cpu_cores", "mean_cpu_cores"):
        assert served["att_idle"][field] == 0.0
        assert served["att_idle"][field] is not None


@pytest.mark.parametrize("query", ["", "?include=usage"])
def test_no_events_are_read_for_cpu_and_no_row_carries_the_interim_block(client, db, api_context, query):
    """A failing events read used to turn every row's `usage` to `unread`; now
    nothing reads events here at all, so it cannot matter."""
    a_task(db)
    an_attempt(db, "att_1", cpu_seconds=1.0, peak_cpu_cores=0.5, mean_cpu_cores=0.25,
               cpu_limit_cores=2.0)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the attempts route read the task's events")

    api_context.store.list_events = refuse
    response = client.get(f"/v1/tasks/task_a/attempts{query}", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert "usage_read" not in body
    row = body["attempts"][0]
    assert "usage" not in row, "the interim heartbeat-event block is still served"
    assert row["peak_cpu_cores"] == 0.5
