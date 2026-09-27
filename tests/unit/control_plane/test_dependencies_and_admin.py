"""Dependency resolution, prewarm, and the admin control surface.

The dependency contract, end to end: a task whose `depends_on` are not all
SUCCEEDED parks as DEPENDENCY_INCOMPLETE, holds no capacity while it waits, and
returns to READY when the last parent succeeds. The promotion is a sweep at the
top of the drain rather than something the succeeding worker triggers, so a
worker that dies immediately after writing SUCCEEDED cannot strand its children.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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
    # READY at submission: the API no longer judges credentials (#169).
    # Admission parks it, and the credential sweep is what brings it back.
    assert task["state"] == "READY"

    scheduler = make_scheduler()
    scheduler.drain()
    assert db.docs[f"tasks/{task['id']}"]["state"] == "PARKED"
    assert db.docs[f"tasks/{task['id']}"]["park_reason"] == "CREDENTIAL_MISSING"

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


# -- the limits an operator sets are the limits that bind ------------------

def test_the_runbooks_spelling_of_a_limit_is_accepted(client, db):
    """docs/concurrency.md and docs/quota-management.md all send `hard_limit`.

    Under `extra="forbid"` every one of those copy-pasteable curls returned 422,
    naming `limit` as missing and `hard_limit` as forbidden. A limit an operator
    cannot set during an incident is not a limit.
    """
    seed_tenant(db, "eng", max_active=20)
    for path in (
        "/v1/admin/limits/global",
        "/v1/admin/limits/provider/anthropic",
        "/v1/admin/limits/resource/standard",
        "/v1/admin/limits/runner/mock",
    ):
        response = client.put(path, headers=auth_header("root"), json={"hard_limit": 150})
        assert response.status_code == 200, f"{path}: {response.text}"
        assert response.json()["pool"]["hard_limit"] == 150

    tenant_limit = client.put(
        "/v1/admin/limits/tenant/eng", headers=auth_header("root"), json={"hard_limit": 7}
    )
    assert tenant_limit.status_code == 200
    assert db.docs["pools/tenant:eng"]["hard_limit"] == 7

    # The original spelling still works; this adds an alias, it does not move.
    assert client.put(
        "/v1/admin/limits/global", headers=auth_header("root"), json={"limit": 11}
    ).status_code == 200
    assert db.docs["pools/global"]["hard_limit"] == 11


def test_capacity_units_really_bounds_the_tenant_pool(client, db):
    """It used to be written to the document and consulted by nothing.

    `acquire_lease_in_transaction` increments every pool by the task's weighted
    `units`, so `tenant:<id>.active` is a count of UNITS. Both knobs are ceilings
    on the same number, and the pool takes the smaller: correct read either way,
    and it can never raise a ceiling an operator set.
    """
    seed_tenant(db, "eng", max_active=20)
    response = client.put(
        "/v1/admin/tenants/eng/limits",
        headers=auth_header("root"),
        json={"capacity_units": 6},
    )
    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["capacity_units"] == 6
    assert response.json()["pool"]["hard_limit"] == 6
    assert db.docs["pools/tenant:eng"]["hard_limit"] == 6

    # And the smaller of the two is what binds.
    client.put(
        "/v1/admin/tenants/eng/limits",
        headers=auth_header("root"),
        json={"max_active": 3},
    )
    assert db.docs["pools/tenant:eng"]["hard_limit"] == 3


def test_a_capacity_units_ceiling_actually_refuses_admission(db, make_scheduler, dispatcher):
    """Through the real drain loop, in weighted units rather than task count."""
    seed_tenant(db, "eng", credentials=("anthropic",), max_active=100)
    seed_pool(db, "global", hard_limit=100)
    from swarm_api.store import Store

    Store(db).set_tenant_limits("eng", capacity_units=3)

    for index in range(4):
        seed_task(
            db,
            task_id=f"task_browser_{index}",
            tenant_id="eng",
            runner_profile="browser",
            resource_class="browser",         # 2 units each
            provider="anthropic",
        )

    report = make_scheduler().drain()

    assert report.leased == 1, "3 units of budget fits exactly one 2-unit task"
    assert db.docs["pools/tenant:eng"]["active"] == 2


def test_a_budget_that_cannot_be_enforced_is_refused_rather_than_stored(client, db):
    """An admin who sets a budget used to get a 200 and no spend control.

    There is no cost attribution anywhere in this control plane, and
    ParkReason.BUDGET_EXHAUSTED appears in no code path, so the number could only
    ever be stored and echoed back.
    """
    seed_tenant(db, "eng", max_active=20)
    response = client.put(
        "/v1/admin/tenants/eng/limits",
        headers=auth_header("root"),
        json={"monthly_budget_usd": 500},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert "monthly_budget_usd" in body["message"]
    assert body["detail"]["enforceable_limits"] == [
        "max_active",
        "capacity_units",
        "enabled",
    ]
    assert db.docs["tenants/eng"].get("monthly_budget_usd") is None


# -- a failed dispatch tells the tenant a code, not the backend's message ---

def test_a_failed_dispatch_does_not_leak_backend_detail_to_the_tenant(
    db, make_scheduler, dispatcher
):
    """`task.last_error` is returned to the caller by `codec.task_to_api`.

    A Cloud Run or Kubernetes error echoes the resource it was handed: the tenant
    service account email, the job name, the secret names in the manifest.
    """
    from scheduler.dispatch import DispatchError

    base_pools(db)
    seed_task(db, task_id="task_doomed", tenant_id="eng")

    def explode(*, task, lease, profile, tenant):
        raise DispatchError(
            "PermissionDenied: swarm-agent-worker-eng@saga-agents-staging.iam."
            "gserviceaccount.com cannot access secret swarm-tenant-eng-anthropic",
            code="cloud_run_run_job_failed",
        )

    dispatcher.dispatch = explode
    report = make_scheduler().drain()

    assert report.dispatch_failures == 1
    stored = db.docs["tasks/task_doomed"]
    assert stored["last_error"].startswith("cloud_run_run_job_failed (attempt att_")
    assert "gserviceaccount.com" not in stored["last_error"]
    assert "swarm-tenant-eng-anthropic" not in stored["last_error"]

    events = [
        doc
        for path, doc in db.docs.items()
        if path.startswith("tasks/task_doomed/events/")
        and doc["type"] == "lease_released"
    ]
    assert events[0]["detail"]["error_code"] == "cloud_run_run_job_failed"
    assert events[0]["detail"]["correlation_id"].startswith("att_")
    assert "gserviceaccount.com" not in repr(events[0]["detail"])


# -- report and metric agree ------------------------------------------------

def test_a_dependency_free_promotion_is_counted_in_the_metric_too(db, make_scheduler):
    """DrainReport.promoted_dependencies and
    `swarm_scheduler_promoted{kind="dependency"}` counted different things, so a
    dashboard built on the metric under-counted promotions."""
    base_pools(db)
    seed_task(
        db,
        task_id="task_orphan",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
    )
    seed_task(
        db,
        task_id="task_child",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=("task_parent",),
    )
    seed_task(db, task_id="task_parent", tenant_id="eng", state="SUCCEEDED")

    scheduler = make_scheduler()
    report = scheduler.drain()

    assert report.promoted_dependencies == 2
    rendered = scheduler.metrics.render()[0].decode("utf-8")
    assert 'swarm_scheduler_promoted_total{kind="dependency"} 2.0' in rendered


# -- the push verifier is a real control, or the process does not start -----

def test_the_scheduler_refuses_to_start_with_its_push_check_disabled(monkeypatch):
    """`PUSH_SERVICE_ACCOUNT` unset left the second layer silently off in every
    deployed environment, and the comment above it asserted a control a reviewer
    would then believe was present."""
    from scheduler.main import PushVerifier

    with_nothing = PushVerifier("", "")
    assert with_nothing.enabled is False, "still available for local development"

    with pytest.raises(ValueError) as exc:
        PushVerifier("", "", required=True)
    assert "PUSH_SERVICE_ACCOUNT" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        PushVerifier("tick@saga-agents-staging.iam.gserviceaccount.com", "", required=True)
    assert "PUSH_AUDIENCE" in str(exc.value)

    PushVerifier(
        "tick@saga-agents-staging.iam.gserviceaccount.com",
        "https://scheduler.example",
        required=True,
    )
