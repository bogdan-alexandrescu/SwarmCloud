"""A task the SCHEDULER ends resolves its dependants in the same run (#636).

#741 made a worker's finish, and a cancel swarm-api ends itself, ring the
`task_finished` wake. What it left (its "Not done" (b)): the scheduler's own
terminal writes. No worker rings for those, and `_promote_dependencies` runs
at the TOP of a drain, before the sweeps and admission that write them, so a
dependant of a task this drain ended waited for the next safety tick -- one
tick per link of a chain.

The scheduler is the receiver of the wake, so it does not publish one to
itself (that message would queue behind the very drain lock it holds); it
resolves what it ended in process, through the finish event's own
`_release_chain` and so the same `_resolve_dependency` rule.

What is held here, per path that writes a terminal state:

    a SCHEDULED_RETRY, CI_PENDING or CHILDREN_INCOMPLETE park dead-lettered
        on its last attempt
    a READY task cancelled at admission (its cancel was requested)
    a step cancelled by an `on_step_failure: fail_workflow` sweep, reached
        from the credential sweep and from the prewarm sweep
    a task whose dispatch failed on its last attempt (FAILED), or after its
        cancel was requested (CANCELLED)
    a chain: the dependant this cancels takes its own dependant with it
    a terminal write made inside a finish event resolves in that event

each resolves its dependant exactly once (one cancel event, and the tick after
finds nothing to do), and a resolution that fails leaves the tick to recover.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .conftest import auth_header, seed_pool, seed_task, seed_tenant


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _dependant(db, task_id: str, *, on: str) -> None:
    seed_task(
        db,
        task_id=task_id,
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=(on,),
    )


def _cancel_events(db, task_id: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [
        doc
        for path, doc in db.docs.items()
        if path.startswith(prefix) and doc.get("type") == "cancelled"
    ]


def _resolved_once(db, scheduler, task_id: str) -> None:
    """CANCELLED with exactly one event, and the next tick has nothing to do."""
    assert db.docs[f"tasks/{task_id}"]["state"] == "CANCELLED"
    assert len(_cancel_events(db, task_id)) == 1, _cancel_events(db, task_id)
    tick = scheduler.drain()
    assert tick.cancelled == 0, tick.to_dict()
    assert tick.stale_writes == 0, tick.to_dict()
    assert len(_cancel_events(db, task_id)) == 1


def _spent(db, task_id: str, park_reason: str) -> None:
    seed_task(
        db,
        task_id=task_id,
        tenant_id="eng",
        state="PARKED",
        park_reason=park_reason,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.docs[f"tasks/{task_id}"].update({"attempt_count": 3, "max_attempts": 3})


# -- dead-lettered parks -------------------------------------------------------


def test_a_dead_lettered_retry_cancels_its_dependant_in_the_same_drain(db, make_scheduler):
    _base(db)
    _spent(db, "task_a", "SCHEDULED_RETRY")
    _dependant(db, "task_b", on="task_a")
    scheduler = make_scheduler()

    report = scheduler.drain()

    assert db.docs["tasks/task_a"]["state"] == "DEAD_LETTERED"
    assert report.dead_lettered == 1, report.to_dict()
    assert report.cancelled == 1, report.to_dict()
    _resolved_once(db, scheduler, "task_b")


def test_a_dead_lettered_ci_wait_cancels_its_dependant_in_the_same_drain(db, make_scheduler):
    _base(db)
    _spent(db, "task_a", "CI_PENDING")
    _dependant(db, "task_b", on="task_a")
    scheduler = make_scheduler()

    scheduler.drain()

    assert db.docs["tasks/task_a"]["state"] == "DEAD_LETTERED"
    _resolved_once(db, scheduler, "task_b")


def test_a_dead_lettered_child_await_cancels_its_dependant_in_the_same_drain(
    db, make_scheduler
):
    _base(db)
    _spent(db, "task_a", "CHILDREN_INCOMPLETE")
    _dependant(db, "task_b", on="task_a")
    scheduler = make_scheduler()

    scheduler.drain()

    assert db.docs["tasks/task_a"]["state"] == "DEAD_LETTERED"
    _resolved_once(db, scheduler, "task_b")


def test_a_chain_is_resolved_link_by_link_in_one_drain(db, make_scheduler):
    """Before: one tick per link. Each cancel here is itself an end."""
    _base(db)
    _spent(db, "task_a", "SCHEDULED_RETRY")
    _dependant(db, "task_b", on="task_a")
    _dependant(db, "task_c", on="task_b")
    scheduler = make_scheduler()

    report = scheduler.drain()

    assert report.cancelled == 2, report.to_dict()
    assert db.docs["tasks/task_b"]["state"] == "CANCELLED"
    _resolved_once(db, scheduler, "task_c")


# -- admission's own ends ------------------------------------------------------


def test_a_task_cancelled_at_admission_cancels_its_dependant_in_the_same_drain(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_a", tenant_id="eng", cancel_requested=True)
    _dependant(db, "task_b", on="task_a")
    scheduler = make_scheduler()

    report = scheduler.drain()

    assert db.docs["tasks/task_a"]["state"] == "CANCELLED"
    assert report.cancelled == 2, report.to_dict()
    assert dispatcher.dispatched == []
    _resolved_once(db, scheduler, "task_b")


def test_a_dispatch_that_fails_on_its_last_attempt_cancels_its_dependant_in_the_same_drain(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_a", tenant_id="eng")
    db.docs["tasks/task_a"].update({"attempt_count": 2, "max_attempts": 3})
    _dependant(db, "task_b", on="task_a")
    dispatcher.fail_for = {"task_a"}
    scheduler = make_scheduler()

    scheduler.drain()

    assert db.docs["tasks/task_a"]["state"] == "FAILED"
    # The failed dispatch gave its capacity straight back (invariants 2-3).
    assert db.docs["pools/global"]["active"] == 0
    _resolved_once(db, scheduler, "task_b")


def test_a_dispatch_that_fails_after_a_cancel_cancels_its_dependant_in_the_same_drain(
    db, make_scheduler, dispatcher, monkeypatch
):
    # The cancel lands between admission and the failed dispatch, so the
    # return writes CANCELLED (store `_returned`), not READY or FAILED.
    _base(db)
    seed_task(db, task_id="task_a", tenant_id="eng")
    _dependant(db, "task_b", on="task_a")
    dispatcher.fail_for = {"task_a"}
    fail = dispatcher.dispatch

    def cancel_then_fail(**kwargs):
        db.docs["tasks/task_a"]["cancel_requested"] = True
        return fail(**kwargs)

    monkeypatch.setattr(dispatcher, "dispatch", cancel_then_fail)
    scheduler = make_scheduler()

    scheduler.drain()

    assert db.docs["tasks/task_a"]["state"] == "CANCELLED"
    assert db.docs["pools/global"]["active"] == 0
    _resolved_once(db, scheduler, "task_b")


def test_a_terminal_write_inside_a_finish_event_resolves_in_that_event(
    db, make_scheduler, dispatcher
):
    _base(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="task_a", tenant_id="eng", cancel_requested=True)
    _dependant(db, "task_b", on="task_a")
    scheduler = make_scheduler()

    report = scheduler.release_dependants("task_parent")

    assert report.stop_reason == "task_finished"
    assert db.docs["tasks/task_a"]["state"] == "CANCELLED"
    assert report.cancelled == 2, report.to_dict()
    _resolved_once(db, scheduler, "task_b")


# -- the fail_workflow sweep, from the credential and prewarm sweeps -----------


def _failed_workflow(client, db, park_reason: str, **fields) -> str:
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "on_step_failure": "fail_workflow",
            "steps": [
                {"step_id": "broken", "runner_profile": "mock"},
                {"step_id": "parked", "runner_profile": "mock"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    steps = {s["step_id"]: s["task_id"] for s in created.json()["workflow"]["steps"]}
    db.docs[f"tasks/{steps['parked']}"].update(
        {
            "state": "PARKED",
            "park_reason": park_reason,
            "runner_profile": "claude-code",
            "provider": "anthropic",
            **fields,
        }
    )
    db.docs[f"tasks/{steps['broken']}"].update(
        {"state": "FAILED", "completed_at": datetime.now(timezone.utc)}
    )
    return steps["parked"]


def _outside_dependant(db, task_id: str, *, on: str) -> None:
    """A task outside the workflow that names one of its steps: the sweep
    cancels only the workflow's own steps, so this one waits on the rule."""
    tenant_id = db.docs[f"tasks/{on}"]["tenant_id"]
    seed_task(
        db,
        task_id=task_id,
        tenant_id=tenant_id,
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=(on,),
    )


def test_a_step_the_credential_sweep_cancels_cancels_its_dependant_in_the_same_drain(
    client, db, make_scheduler
):
    seed_pool(db, "global", hard_limit=10)
    parked = _failed_workflow(client, db, "CREDENTIAL_MISSING")
    _outside_dependant(db, "task_after", on=parked)
    scheduler = make_scheduler()

    report = scheduler.drain()

    assert db.docs[f"tasks/{parked}"]["state"] == "CANCELLED"
    assert report.promoted_credentials == 0
    _resolved_once(db, scheduler, "task_after")


def test_a_step_the_prewarm_sweep_cancels_cancels_its_dependant_in_the_same_drain(
    client, db, make_scheduler
):
    seed_pool(db, "global", hard_limit=10)
    parked = _failed_workflow(
        client,
        db,
        "PROVIDER_QUOTA_EXHAUSTED",
        next_eligible_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    _outside_dependant(db, "task_after", on=parked)
    scheduler = make_scheduler()

    report = scheduler.drain()

    assert db.docs[f"tasks/{parked}"]["state"] == "CANCELLED"
    assert report.promoted_prewarm == 0
    _resolved_once(db, scheduler, "task_after")


# -- the tick stays the safety net ---------------------------------------------


def test_a_resolution_that_fails_does_not_stop_the_drain_and_the_tick_recovers(
    db, make_scheduler, dispatcher, monkeypatch
):
    _base(db)
    _spent(db, "task_a", "SCHEDULED_RETRY")
    _dependant(db, "task_b", on="task_a")
    seed_task(db, task_id="task_ready", tenant_id="eng")
    scheduler = make_scheduler()

    def unavailable(*args, **kwargs):
        raise RuntimeError("Firestore unavailable")

    monkeypatch.setattr(scheduler.store, "dependants_waiting_on", unavailable)
    report = scheduler.drain()

    # Admission still ran: the in-run resolution is a latency win, never a gate.
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_ready"]
    assert report.leased == 1, report.to_dict()
    assert db.docs["tasks/task_b"]["state"] == "PARKED"
    monkeypatch.undo()

    tick = scheduler.drain()

    assert tick.cancelled == 1, tick.to_dict()
    assert db.docs["tasks/task_b"]["state"] == "CANCELLED"
    assert len(_cancel_events(db, "task_b")) == 1
