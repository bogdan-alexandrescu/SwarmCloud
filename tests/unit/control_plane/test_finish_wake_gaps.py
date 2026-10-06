"""The finish wake's two remaining gaps (#636).

The worker rings `task_finished` after its terminal write, and the scheduler
answers it with `release_dependants` (test_finish_event_releases_dependants.py).
Two hand-offs still waited for the one-minute safety tick:

  G-1  A CHAIN OF CANCELS. A FAILED parent's dependant is cancelled by the
       event, which is itself a task reaching a terminal state; its own
       dependant was left PARKED until the next drain's sweep, one tick per
       link. The event now resolves the chain it ended, bounded by the sweep
       size, through the same `_resolve_dependency`.
  G-2  A TASK swarm-api ENDS ITSELF. `POST /v1/tasks/{id}/cancel` ends a task
       that holds no capacity, or one with no live worker, in its own
       transaction; no worker exists to ring the wake, so its dependants and
       the capacity it released waited for the tick. The route now rings the
       same `task_finished` wake, and a workflow cancel rings one drain.

And the tick stays the safety net: a wake that is lost leaves exactly what the
next drain releases, as before.
"""

from __future__ import annotations

import base64
import json

from .conftest import auth_header, seed_pool, seed_task, seed_tenant


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _waiting(db, task_id: str, *depends_on: str, tenant_id: str = "eng") -> None:
    seed_task(
        db,
        task_id=task_id,
        tenant_id=tenant_id,
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=depends_on,
    )


# -- G-1: a chain of cancels resolves in one event ----------------------------


def test_one_event_cancels_a_whole_chain_below_a_failed_parent(db, make_scheduler, dispatcher):
    _base(db)
    seed_task(db, task_id="task_a", tenant_id="eng", state="FAILED")
    _waiting(db, "task_b", "task_a")
    _waiting(db, "task_c", "task_b")
    _waiting(db, "task_d", "task_c")

    report = make_scheduler().release_dependants("task_a")

    assert [db.docs[f"tasks/task_{x}"]["state"] for x in "bcd"] == ["CANCELLED"] * 3
    assert report.cancelled == 3, report.to_dict()
    assert report.leased == 0
    assert dispatcher.dispatched == []
    assert db.docs["pools/global"]["active"] == 0


def test_the_chain_stops_at_a_dependant_still_waiting_on_a_live_parent(
    db, make_scheduler, dispatcher
):
    """A cancelled link releases nothing the dependency rule would not: a
    dependant of B that also waits on a RUNNING task is cancelled (B did not
    succeed), but a dependant of a still-PARKED task is not reached at all."""
    _base(db)
    seed_task(db, task_id="task_a", tenant_id="eng", state="FAILED")
    seed_task(db, task_id="task_live", tenant_id="eng", state="RUNNING")
    _waiting(db, "task_b", "task_a")
    _waiting(db, "task_c", "task_b", "task_live")
    _waiting(db, "task_x", "task_live")

    report = make_scheduler().release_dependants("task_a")

    assert db.docs["tasks/task_b"]["state"] == "CANCELLED"
    assert db.docs["tasks/task_c"]["state"] == "CANCELLED"
    assert db.docs["tasks/task_x"]["state"] == "PARKED"
    assert report.cancelled == 2, report.to_dict()
    assert dispatcher.dispatched == []


def test_the_tick_still_cancels_the_chain_when_no_event_arrives(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_a", tenant_id="eng", state="FAILED")
    _waiting(db, "task_b", "task_a")
    _waiting(db, "task_c", "task_b")
    scheduler = make_scheduler()

    scheduler.drain()
    scheduler.drain()

    assert db.docs["tasks/task_b"]["state"] == "CANCELLED"
    assert db.docs["tasks/task_c"]["state"] == "CANCELLED"


# -- G-2: a cancel swarm-api ends itself rings the wake -----------------------


def _scheduler_envelope(reason: str, attributes: dict[str, str]) -> dict:
    """The push body Pub/Sub delivers for what `PubSubWaker.wake` publishes."""
    payload = json.dumps({"reason": reason, **attributes}).encode("utf-8")
    return {
        "message": {
            "data": base64.b64encode(payload).decode("ascii"),
            "attributes": {"reason": reason, **attributes},
            "messageId": "1",
        }
    }


def test_cancelling_a_parked_parent_rings_task_finished(client, api_context, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_parent", tenant_id="eng", state="PARKED")

    body = client.post("/v1/tasks/task_parent/cancel", headers=auth_header("alice")).json()

    assert body["released_immediately"] is True
    assert api_context.waker.calls == [
        ("task_finished", {"task_id": "task_parent", "tenant_id": "eng", "state": "CANCELLED"})
    ]


def test_the_wake_the_api_rings_releases_the_dependant_without_a_tick(
    client, api_context, db, make_scheduler, dispatcher
):
    """End to end through the scheduler's own decoding: what the API publishes
    is a `task_finished` the push route routes to `release_dependants`."""
    from scheduler.main import decode_push_envelope, finished_task_id

    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="READY")
    _waiting(db, "task_child", "task_parent")

    client.post("/v1/tasks/task_parent/cancel", headers=auth_header("alice"))
    [(reason, attributes)] = api_context.waker.calls
    task_id = finished_task_id(decode_push_envelope(_scheduler_envelope(reason, attributes)))
    assert task_id == "task_parent"

    report = make_scheduler().release_dependants(task_id)

    assert db.docs["tasks/task_child"]["state"] == "CANCELLED"
    assert report.cancelled == 1, report.to_dict()
    assert dispatcher.dispatched == []


def test_a_cancel_that_only_flags_a_live_worker_rings_nothing(client, api_context, db):
    """The worker ends that task, and rings the wake itself once it has."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_running", tenant_id="eng", state="RUNNING")

    body = client.post("/v1/tasks/task_running/cancel", headers=auth_header("alice")).json()

    assert body["released_immediately"] is False
    assert db.docs["tasks/task_running"]["state"] == "RUNNING"
    assert api_context.waker.calls == []


def test_a_wake_that_raises_does_not_fail_the_cancel(client, api_context, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_parent", tenant_id="eng", state="READY")

    def broken(reason: str, **attributes: str) -> bool:
        raise RuntimeError("pubsub unavailable")

    api_context.waker.wake = broken  # type: ignore[method-assign]
    response = client.post("/v1/tasks/task_parent/cancel", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert db.docs["tasks/task_parent"]["state"] == "CANCELLED"


def test_a_lost_api_wake_leaves_the_dependant_to_the_tick(
    client, api_context, db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="READY")
    _waiting(db, "task_child", "task_parent")

    client.post("/v1/tasks/task_parent/cancel", headers=auth_header("alice"))
    assert db.docs["tasks/task_child"]["state"] == "PARKED"

    make_scheduler().drain()

    assert db.docs["tasks/task_child"]["state"] == "CANCELLED"


def test_a_workflow_cancel_rings_one_drain(client, api_context, db):
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=10)
    submitted = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "a", "runner_profile": "mock"},
                {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]},
            ]
        },
    )
    assert submitted.status_code == 201, submitted.text
    workflow_id = submitted.json()["workflow"]["workflow_id"]
    api_context.waker.calls.clear()

    result = client.post(
        f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("alice")
    ).json()

    assert len(result["tasks_cancelled"]) == 2, result
    assert api_context.waker.calls == [
        ("workflow_cancelled", {"tenant_id": "eng", "workflow_id": workflow_id})
    ]
