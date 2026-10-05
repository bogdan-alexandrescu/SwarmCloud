"""A finished task releases its dependants at once, not on the next tick (#636).

Measured 2026-10-05 (history report §2.4): parent done -> child READY took p50
27 s, p90 54 s, max 76 s, because `_promote_dependencies` only runs at the top
of a drain, and nothing woke a drain when a task ended. The worker now
publishes a `task_finished` wake on the scheduler's existing wake topic, and
`/pubsub/push` answers it with `Scheduler.release_dependants`: the dependency
rule `_promote_dependencies` applies, for that parent's dependants only, then
one admission pass through the same guarded transactions.

What is held here:

    a finished parent releases and admits its child without a tick
    a duplicate event changes nothing
    the tick after an event finds nothing left to do
    an event that failed leaves the tick to recover, as before
    an event never leases what the dependency rule would not release
    an event for a task that has not ended is a no-op
    another tenant's task naming the parent is never touched
    the push route sends `task_finished` to the event path, and a failure is a 5xx
"""

from __future__ import annotations

import base64
import json

import pytest

from .conftest import seed_pool, seed_task, seed_tenant


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10)
    seed_tenant(db, "research", max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _child(db, task_id: str = "task_child", *, tenant_id: str = "eng", depends_on=("task_parent",)):
    seed_task(
        db,
        task_id=task_id,
        tenant_id=tenant_id,
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=depends_on,
    )


def test_a_finished_parent_releases_and_admits_its_child_without_a_tick(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)

    report = make_scheduler().release_dependants("task_parent")

    assert report.promoted_dependencies == 1, report.to_dict()
    assert report.leased == 1, report.to_dict()
    assert db.docs["tasks/task_child"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_child"]
    # Reserved through the ordinary admission transaction (invariant 2).
    assert db.docs["pools/global"]["active"] == 1


def test_a_duplicate_event_is_a_no_op(db, make_scheduler, dispatcher):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)
    scheduler = make_scheduler()

    scheduler.release_dependants("task_parent")
    again = scheduler.release_dependants("task_parent")

    assert again.promoted_dependencies == 0, again.to_dict()
    assert again.leased == 0, again.to_dict()
    assert len(dispatcher.dispatched) == 1
    assert db.docs["pools/global"]["active"] == 1


def test_the_tick_after_an_event_finds_nothing_to_do(db, make_scheduler, dispatcher):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)
    scheduler = make_scheduler()

    scheduler.release_dependants("task_parent")
    tick = scheduler.drain()

    assert tick.promoted_dependencies == 0, tick.to_dict()
    assert tick.leased == 0, tick.to_dict()
    assert tick.stale_writes == 0, tick.to_dict()
    assert len(dispatcher.dispatched) == 1
    assert db.docs["pools/global"]["active"] == 1


def test_an_event_that_failed_leaves_the_tick_to_recover(
    db, make_scheduler, dispatcher, monkeypatch
):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)
    scheduler = make_scheduler()

    def unavailable(*args, **kwargs):
        raise RuntimeError("Firestore unavailable")

    monkeypatch.setattr(scheduler.store, "dependants_waiting_on", unavailable)
    with pytest.raises(RuntimeError):
        scheduler.release_dependants("task_parent")
    assert db.docs["tasks/task_child"]["state"] == "PARKED"
    assert db.docs["pools/global"]["active"] == 0
    monkeypatch.undo()

    tick = scheduler.drain()

    assert tick.promoted_dependencies == 1, tick.to_dict()
    assert db.docs["tasks/task_child"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_child"]


def test_an_event_never_leases_a_child_still_waiting_on_another_parent(
    db, make_scheduler, dispatcher
):
    """Invariant 1: the event creates no demand for a task the dependency
    rule would not release. The child stays PARKED and holds nothing."""
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="task_other", tenant_id="eng", state="RUNNING")
    _child(db, depends_on=("task_parent", "task_other"))

    report = make_scheduler().release_dependants("task_parent")

    assert report.promoted_dependencies == 0
    assert db.docs["tasks/task_child"]["state"] == "PARKED"
    assert dispatcher.dispatched == []
    assert db.docs["pools/global"]["active"] == 0
    assert not any(key.startswith("leases/") for key in db.docs)


def test_an_event_for_a_failed_parent_cancels_its_child_and_leases_nothing(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="FAILED")
    _child(db)

    report = make_scheduler().release_dependants("task_parent")

    assert db.docs["tasks/task_child"]["state"] == "CANCELLED"
    assert report.cancelled == 1
    assert report.leased == 0
    assert dispatcher.dispatched == []


def test_an_event_for_a_task_that_has_not_ended_is_a_no_op(db, make_scheduler, dispatcher):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="RUNNING")
    _child(db)
    seed_task(db, task_id="task_ready", tenant_id="eng")

    report = make_scheduler().release_dependants("task_parent")

    assert report.stop_reason == "not_terminal"
    assert report.leased == 0
    assert db.docs["tasks/task_child"]["state"] == "PARKED"
    assert db.docs["tasks/task_ready"]["state"] == "READY"
    assert dispatcher.dispatched == []


def test_an_event_for_an_unknown_task_is_a_no_op(db, make_scheduler, dispatcher):
    _base(db)
    report = make_scheduler().release_dependants("task_nobody")
    assert report.stop_reason == "not_terminal"
    assert dispatcher.dispatched == []


def test_another_tenants_task_naming_the_parent_is_not_touched(db, make_scheduler, dispatcher):
    """Invariant 9: the event path reads the parent's tenant's dependants only."""
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db, "task_foreign", tenant_id="research")

    report = make_scheduler().release_dependants("task_parent")

    assert report.promoted_dependencies == 0
    assert db.docs["tasks/task_foreign"]["state"] == "PARKED"
    assert dispatcher.dispatched == []


def test_an_event_while_dispatch_is_paused_releases_nothing(db, make_scheduler, dispatcher):
    _base(db)
    db.docs["control/dispatch"] = {"dispatch_paused": True}
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)

    report = make_scheduler().release_dependants("task_parent")

    assert report.stop_reason == "dispatch_paused"
    assert db.docs["tasks/task_child"]["state"] == "PARKED"
    assert dispatcher.dispatched == []


# -- the push route ------------------------------------------------------------


def _push(client, *, reason: str, **attributes: str):
    payload = base64.b64encode(json.dumps({"reason": reason, **attributes}).encode()).decode()
    return client.post(
        "/pubsub/push",
        json={
            "message": {
                "data": payload,
                "attributes": {"reason": reason, **attributes},
                "messageId": "1",
            },
            "subscription": "s",
        },
    )


def _client(scheduler):
    from fastapi.testclient import TestClient

    from scheduler.main import create_app

    return TestClient(create_app(scheduler))


def test_the_push_route_sends_a_finished_task_to_the_event_path(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)
    scheduler = make_scheduler()
    calls: list[str] = []
    real = scheduler.release_dependants

    def recording(task_id: str):
        calls.append(task_id)
        return real(task_id)

    scheduler.release_dependants = recording  # type: ignore[method-assign]
    response = _push(_client(scheduler), reason="task_finished", task_id="task_parent")

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["reason"] == "task_finished"
    assert body["report"]["promoted_dependencies"] == 1
    assert calls == ["task_parent"]
    assert db.docs["tasks/task_child"]["state"] == "DISPATCHED"


def test_a_failed_event_answers_5xx_so_pubsub_redelivers(db, make_scheduler, monkeypatch):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)
    scheduler = make_scheduler()

    def unavailable(*args, **kwargs):
        raise RuntimeError("Firestore unavailable")

    monkeypatch.setattr(scheduler.store, "dependants_waiting_on", unavailable)
    response = _push(_client(scheduler), reason="task_finished", task_id="task_parent")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "detail": "RuntimeError"}
    assert db.docs["tasks/task_child"]["state"] == "PARKED"


def test_a_finish_wake_with_a_malformed_task_id_falls_back_to_a_drain(
    db, make_scheduler, dispatcher
):
    """A document path is never built from an unchecked string: an id that is
    not an id gets the ordinary drain, which releases the child anyway."""
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)
    scheduler = make_scheduler()

    def refuse(task_id: str):
        raise AssertionError(f"event path called with {task_id!r}")

    scheduler.release_dependants = refuse  # type: ignore[method-assign]
    response = _push(_client(scheduler), reason="task_finished", task_id="tasks/../x")

    assert response.status_code == 200, response.json()
    assert response.json()["report"]["promoted_dependencies"] == 1
    assert db.docs["tasks/task_child"]["state"] == "DISPATCHED"


def test_a_finish_event_behind_a_running_drain_is_redelivered_not_dropped(
    db, make_scheduler, monkeypatch
):
    """A drain that began before the parent ended may be past its dependency
    sweep; acking the event then would leave the child for the next tick."""
    import scheduler.main as main_mod

    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    _child(db)
    monkeypatch.setattr(main_mod, "FINISH_LOCK_WAIT_SECONDS", 0.01)
    client = _client(make_scheduler())
    lock = client.app.state.drain_lock
    lock.acquire()
    try:
        response = _push(client, reason="task_finished", task_id="task_parent")
    finally:
        lock.release()

    assert response.status_code == 503
    assert response.json()["status"] == "busy"
    assert db.docs["tasks/task_child"]["state"] == "PARKED"

    assert _push(client, reason="task_finished", task_id="task_parent").status_code == 200
    assert db.docs["tasks/task_child"]["state"] == "DISPATCHED"
