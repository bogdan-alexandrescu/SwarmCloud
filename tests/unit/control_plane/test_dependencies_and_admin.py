"""Dependency resolution, prewarm, and the admin control surface.

The dependency contract, end to end: a task whose `depends_on` are not all
SUCCEEDED parks as DEPENDENCY_INCOMPLETE, holds no capacity while it waits, and
returns to READY when the last parent succeeds. The promotion is a sweep at the
top of the drain rather than something the succeeding worker triggers, so a
worker that dies immediately after writing SUCCEEDED cannot strand its children.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .conftest import auth_header, seed_pool, seed_task, seed_tenant

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def base_pools(db):
    seed_tenant(db, "eng", max_active=10)
    seed_pool(db, "global", hard_limit=10)


# -- dependency resolution ------------------------------------------------

def test_a_parked_child_returns_to_ready_when_its_last_parent_succeeds(
    db, make_scheduler, dispatcher
):
    base_pools(db)
    seed_task(db, task_id="task_p1", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="task_p2", tenant_id="eng", state="RUNNING")
    seed_task(
        db,
        task_id="task_child",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=("task_p1", "task_p2"),
    )

    scheduler = make_scheduler()

    # One parent still running: the child stays parked and costs nothing.
    first = scheduler.drain()
    assert first.promoted_dependencies == 0
    assert db.docs["tasks/task_child"]["state"] == "PARKED"
    assert db.docs["pools/global"]["active"] == 0

    db.docs["tasks/task_p2"]["state"] = "SUCCEEDED"

    second = scheduler.drain()
    assert second.promoted_dependencies == 1
    assert second.dispatched == 1
    assert db.docs["tasks/task_child"]["state"] == "DISPATCHED"
    assert dispatcher.dispatched[-1]["task_id"] == "task_child"


def test_a_child_is_cancelled_when_a_parent_fails(db, make_scheduler):
    base_pools(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="FAILED")
    seed_task(
        db,
        task_id="task_child",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=("task_parent",),
    )

    make_scheduler().drain()

    child = db.docs["tasks/task_child"]
    assert child["state"] == "CANCELLED"
    assert "upstream" in child["last_error"]


def test_a_ready_task_with_unmet_dependencies_is_parked_not_admitted(
    db, make_scheduler, dispatcher
):
    """Belt and braces: even if something re-readied it early, it must not run."""
    base_pools(db)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="RUNNING")
    seed_task(db, task_id="task_child", tenant_id="eng", depends_on=("task_parent",))

    make_scheduler().drain()

    child = db.docs["tasks/task_child"]
    assert child["state"] == "PARKED"
    assert child["park_reason"] == "DEPENDENCY_INCOMPLETE"
    assert dispatcher.dispatched == []
    assert db.docs["pools/global"]["active"] == 0


def test_workflow_runs_step_by_step_through_the_real_api(client, db, make_scheduler, dispatcher):
    seed_pool(db, "global", hard_limit=10)
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "one", "runner_profile": "mock"},
                {"step_id": "two", "runner_profile": "mock", "depends_on": ["one"]},
                {"step_id": "three", "runner_profile": "mock", "depends_on": ["two"]},
            ]
        },
    )
    steps = {s["step_id"]: s["task_id"] for s in created.json()["workflow"]["steps"]}
    scheduler = make_scheduler()

    scheduler.drain()
    assert db.docs[f"tasks/{steps['one']}"]["state"] == "DISPATCHED"
    assert db.docs[f"tasks/{steps['two']}"]["state"] == "PARKED"
    assert db.docs[f"tasks/{steps['three']}"]["state"] == "PARKED"

    db.docs[f"tasks/{steps['one']}"]["state"] = "SUCCEEDED"
    scheduler.drain()
    assert db.docs[f"tasks/{steps['two']}"]["state"] == "DISPATCHED"
    assert db.docs[f"tasks/{steps['three']}"]["state"] == "PARKED"

    db.docs[f"tasks/{steps['two']}"]["state"] = "SUCCEEDED"
    scheduler.drain()
    assert db.docs[f"tasks/{steps['three']}"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == [
        steps["one"], steps["two"], steps["three"]
    ]


def test_credential_parked_tasks_are_promoted_once_the_key_is_registered(
    client, db, make_scheduler, dispatcher
):
    seed_pool(db, "global", hard_limit=10)
    task = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "claude-code", "input": {}},
    ).json()["task"]
    assert task["park_reason"] == "CREDENTIAL_MISSING"

    scheduler = make_scheduler()
    scheduler.drain()
    assert db.docs[f"tasks/{task['id']}"]["state"] == "PARKED"

    client.post(
        "/v1/tenants/me/credentials",
        headers=auth_header("alice"),
        json={"provider": "anthropic", "api_key": "sk-ant-" + "z" * 20},
    )

    report = scheduler.drain()
    assert report.promoted_credentials == 1
    assert db.docs[f"tasks/{task['id']}"]["state"] == "DISPATCHED"


# -- prewarm ---------------------------------------------------------------

def test_prewarm_promotes_quota_parked_work_inside_the_lead_window(db, make_scheduler):
    base_pools(db)
    soon = datetime.now(timezone.utc) + timedelta(seconds=30)
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    # The provider pool is still capped at zero, so promoting is safe: admission
    # refuses until quota actually returns.
    seed_pool(db, "provider:anthropic", hard_limit=10, quota_derived_limit=0)
    seed_pool(db, "provider:anthropic:tenant:eng", hard_limit=10, quota_derived_limit=0)
    seed_task(
        db, task_id="task_soon", tenant_id="eng", state="PARKED",
        park_reason="PROVIDER_COOLDOWN", provider="anthropic", next_eligible_at=soon,
    )
    seed_task(
        db, task_id="task_later", tenant_id="eng", state="PARKED",
        park_reason="PROVIDER_COOLDOWN", provider="anthropic", next_eligible_at=later,
    )

    report = make_scheduler().drain()

    assert report.promoted_prewarm == 1
    assert db.docs["tasks/task_soon"]["state"] == "READY"
    assert db.docs["tasks/task_later"]["state"] == "PARKED"
    assert report.skipped == 0
    # Promoted, but not admitted: the quota cap still holds the pool at zero.
    assert db.docs["pools/provider:anthropic:tenant:eng"]["active"] == 0


def test_prewarm_refuses_when_nothing_caps_the_provider(db, make_scheduler):
    """No provider pool means nothing is holding the quota window shut."""
    base_pools(db)
    soon = datetime.now(timezone.utc) + timedelta(seconds=30)
    seed_task(
        db, task_id="task_unguarded", tenant_id="eng", state="PARKED",
        park_reason="PROVIDER_COOLDOWN", provider="anthropic", next_eligible_at=soon,
    )

    report = make_scheduler().drain()

    assert report.promoted_prewarm == 0
    assert db.docs["tasks/task_unguarded"]["state"] == "PARKED"


def test_prewarm_is_bounded(db, make_scheduler):
    from .conftest import core_settings, scheduler_settings

    base_pools(db)
    seed_pool(db, "provider:anthropic:tenant:eng", hard_limit=10, quota_derived_limit=0)
    soon = datetime.now(timezone.utc) + timedelta(seconds=10)
    for i in range(10):
        seed_task(
            db, task_id=f"task_{i}", tenant_id="eng", state="PARKED",
            park_reason="PROVIDER_QUOTA_EXHAUSTED", provider="anthropic",
            next_eligible_at=soon,
        )

    settings = scheduler_settings(core=core_settings(prewarm_max_agents=3))
    report = make_scheduler(settings=settings).drain()

    assert report.promoted_prewarm == 3


# -- admin controls --------------------------------------------------------

def test_admin_pause_stops_dispatch_and_resume_restarts_it(
    client, db, make_scheduler, dispatcher
):
    base_pools(db)
    seed_task(db, task_id="task_1", tenant_id="eng")

    assert client.post(
        "/v1/admin/dispatch/pause", headers=auth_header("root"),
        json={"reason": "incident 4312"},
    ).status_code == 200

    scheduler = make_scheduler()
    assert scheduler.drain().stop_reason == "dispatch_paused"
    assert dispatcher.dispatched == []

    assert client.post(
        "/v1/admin/dispatch/resume", headers=auth_header("root"), json={}
    ).status_code == 200
    assert scheduler.drain().dispatched == 1


def test_admin_can_change_the_global_limit_without_a_redeploy(
    client, db, make_scheduler, dispatcher
):
    seed_tenant(db, "eng", max_active=50)
    for i in range(5):
        seed_task(db, task_id=f"task_{i}", tenant_id="eng")

    client.put("/v1/admin/limits/global", headers=auth_header("root"), json={"limit": 2})
    assert make_scheduler().drain().dispatched == 2

    client.put("/v1/admin/limits/global", headers=auth_header("root"), json={"limit": 5})
    assert make_scheduler().drain().dispatched == 3
    assert len(dispatcher.dispatched) == 5


def test_admin_drain_of_a_resource_class_stops_new_admissions(
    client, db, make_scheduler, dispatcher
):
    base_pools(db)
    seed_task(db, task_id="task_1", tenant_id="eng")

    drained = client.post(
        "/v1/admin/resources/standard/drain",
        headers=auth_header("root"),
        json={"drain": True, "reason": "node pool upgrade"},
    )
    assert drained.status_code == 200
    assert drained.json()["pool"]["enabled"] is False

    report = make_scheduler().drain()
    assert report.dispatched == 0
    assert db.docs["tasks/task_1"]["blocked_by"][0]["reason"] == "MANUAL_PAUSE"
    assert db.docs["tasks/task_1"]["state"] == "READY"

    client.post(
        "/v1/admin/resources/standard/drain",
        headers=auth_header("root"),
        json={"drain": False},
    )
    assert make_scheduler().drain().dispatched == 1


def test_admin_disable_of_a_provider_also_pins_the_quota_state(client, db, broker):
    broker.observe_success("anthropic", "eng")
    assert db.docs["pools/provider:anthropic:tenant:eng"]["quota_derived_limit"] > 0

    response = client.post(
        "/v1/admin/providers/anthropic/enabled",
        headers=auth_header("root"),
        json={"enabled": False, "reason": "billing hold"},
    )
    assert response.status_code == 200
    assert response.json()["quota_documents_updated"] == 1
    assert db.docs["pools/provider:anthropic"]["enabled"] is False
    assert db.docs["pools/provider:anthropic:tenant:eng"]["enabled"] is False
    assert db.docs["quota/anthropic:eng"]["state"] == "DISABLED"

    # And AIMD cannot raise it back up underneath the operator.
    for _ in range(100):
        broker.observe_success("anthropic", "eng")
    assert db.docs["quota/anthropic:eng"]["state"] == "DISABLED"
    assert db.docs["pools/provider:anthropic:tenant:eng"]["quota_derived_limit"] == 0


def test_admin_tenant_limits_move_the_tenant_pool_too(client, db):
    seed_tenant(db, "eng", max_active=20)
    response = client.put(
        "/v1/admin/limits/tenant/eng", headers=auth_header("root"), json={"limit": 3}
    )
    assert response.status_code == 200
    assert response.json()["tenant"]["max_active"] == 3
    # A limit that lives only on the tenant document is a limit that does not
    # exist: the scheduler enforces the pool.
    assert db.docs["pools/tenant:eng"]["hard_limit"] == 3


def test_admin_rejects_unknown_names(client):
    assert client.put(
        "/v1/admin/limits/provider/nope", headers=auth_header("root"), json={"limit": 1}
    ).status_code == 422
    assert client.put(
        "/v1/admin/limits/resource/nope", headers=auth_header("root"), json={"limit": 1}
    ).status_code == 422
    assert client.put(
        "/v1/admin/limits/runner/nope", headers=auth_header("root"), json={"limit": 1}
    ).status_code == 422


def test_disabled_tenant_cannot_submit(client, db):
    seed_tenant(db, "eng", max_active=5)
    client.put(
        "/v1/admin/tenants/eng/limits", headers=auth_header("root"), json={"enabled": False}
    )
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "mock"}
    )
    assert response.status_code == 403


# -- scheduler HTTP surface ------------------------------------------------

def test_pubsub_push_runs_one_drain(db, make_scheduler, dispatcher):
    import base64
    import json

    from fastapi.testclient import TestClient

    from scheduler.main import create_app

    base_pools(db)
    seed_task(db, task_id="task_1", tenant_id="eng")
    client = TestClient(create_app(make_scheduler()))

    payload = base64.b64encode(json.dumps({"reason": "task_submitted"}).encode()).decode()
    response = client.post(
        "/pubsub/push",
        json={"message": {"data": payload, "messageId": "1"}, "subscription": "s"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "task_submitted"
    assert body["report"]["dispatched"] == 1
    assert len(dispatcher.dispatched) == 1

    # A malformed envelope still acks and still drains; the queue is in
    # Firestore, not in the message.
    assert client.post("/pubsub/push", json={"message": {"data": "!!!not-base64"}}).status_code == 200
    assert client.get("/healthz").status_code == 200
    assert "swarm_scheduler_runs_total" in client.get("/metrics").text
