"""Child tasks, the scheduler's half (docs/design/child-tasks.md §3.2, §3.3, §3.4).

The nonce the dispatcher passes, the await sweep that promotes a parent once
its children are done (and cancels the outstanding ones past the deadline),
and the cascade sweep that makes "cancelling a parent cancels its children"
certain. Driven through `Scheduler.drain()` over the in-memory Firestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import ParkReason, TaskState

from swarm_api.childkey import AttemptTuple, nonce
from swarm_api.validation import CHILD_CASCADE_METADATA_KEY as API_CASCADE_KEY

from scheduler import children as children_mod
from scheduler.dispatch import worker_env

from .conftest import PROJECT, scheduler_settings, seed_pool, seed_task, seed_tenant

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
PARENT = "task_parent"


def at_now() -> datetime:
    return NOW


def world(db) -> None:
    """A tenant, and a global pool at 0 so admission leases nothing: what is
    measured is the sweeps alone."""
    seed_pool(db, "global", hard_limit=0)
    seed_tenant(db, "eng")


def parked_parent(db, *, parked_at: datetime = NOW, **extra) -> dict:
    doc = seed_task(
        db, task_id=PARENT, tenant_id="eng", state="PARKED",
        park_reason=ParkReason.CHILDREN_INCOMPLETE.value, next_eligible_at=parked_at,
    )
    doc.update({"attempt_count": 1, **extra})
    return doc


def child(db, task_id: str, state: str, *, tenant: str = "eng", parent: str = PARENT,
          summary: dict | None = None) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state=state)
    doc["parent_task_id"] = parent
    doc["parent_attempt_id"] = "att_1"
    if summary is not None:
        doc["result_summary"] = summary
    return doc


# --------------------------------------------------------------------------
# §3.2 step 1: the registration nonce
# --------------------------------------------------------------------------


def test_the_scheduler_mints_the_nonce_swarm_api_verifies():
    attempt = AttemptTuple("eng", "task_1", "att_1", "lease_1", 3)
    assert children_mod.registration_nonce(
        "k", tenant_id="eng", task_id="task_1", attempt_id="att_1", lease_id="lease_1",
        generation=3,
    ) == nonce("k", attempt)
    assert children_mod.CHILD_CASCADE_METADATA_KEY == API_CASCADE_KEY


def _task(*, parent: str | None = None) -> Task:
    profile = RUNNER_PROFILES["mock"]
    return Task(
        id="task_abc", tenant_id="eng", created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile="mock", resource_class=profile.resource_class,
        input={}, submitted_by="alice@saga.xyz", parent_task_id=parent,
    )


def _lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_1", task_id=task.id, attempt_id="att_1", tenant_id="eng",
        generation=2, pools=["global"], units=1, state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5), expires_at=NOW + timedelta(minutes=2),
    )


def _tenant() -> Tenant:
    return Tenant(
        tenant_id="eng", kind="group", principal="eng@saga.xyz", created_at=NOW,
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
    )


def test_an_attempt_gets_a_nonce_bound_to_its_own_tuple():
    settings = scheduler_settings(
        child_key="k", swarm_api_url="https://api.example", swarm_api_audience="aud"
    )
    task = _task()
    env = worker_env(task=task, lease=_lease(task), tenant=_tenant(), settings=settings)
    assert env["SWARM_CHILD_NONCE"] == nonce("k", AttemptTuple("eng", "task_abc", "att_1", "lease_1", 2))
    assert env["SWARM_API_URL"] == "https://api.example"
    assert env["SWARM_API_AUDIENCE"] == "aud"
    assert "k" not in {v for name, v in env.items() if name != "SWARM_CHILD_NONCE"}


def test_a_child_and_an_unkeyed_deployment_get_no_child_path():
    keyed = scheduler_settings(child_key="k", swarm_api_url="https://api.example")
    as_child = _task(parent="task_parent")
    env = worker_env(task=as_child, lease=_lease(as_child), tenant=_tenant(), settings=keyed)
    assert "SWARM_CHILD_NONCE" not in env and "SWARM_API_URL" not in env
    plain = _task()
    env = worker_env(task=plain, lease=_lease(plain), tenant=_tenant(), settings=scheduler_settings())
    assert "SWARM_CHILD_NONCE" not in env
    # A nonce with nowhere to spend it would sit unspent where the agent reads it.
    no_api = scheduler_settings(child_key="k")
    env = worker_env(task=plain, lease=_lease(plain), tenant=_tenant(), settings=no_api)
    assert "SWARM_CHILD_NONCE" not in env


# --------------------------------------------------------------------------
# §3.3 step 6: the await sweep
# --------------------------------------------------------------------------


def test_a_parent_is_promoted_when_every_child_is_terminal_with_outputs(db, make_scheduler):
    world(db)
    parked_parent(db)
    child(db, "task_c1", "SUCCEEDED", summary={"artifacts": []})
    child(db, "task_c2", "FAILED", summary={"exit_code": 1})
    child(db, "task_c3", "CANCELLED")
    report = make_scheduler(now=at_now).drain()
    assert report.promoted_child_awaits == 1
    parent = db.docs[f"tasks/{PARENT}"]
    assert parent["state"] == "READY" and parent["park_reason"] is None


def test_a_parent_waits_for_a_running_child_and_an_unwritten_manifest(db, make_scheduler):
    world(db)
    parked_parent(db)
    child(db, "task_c1", "RUNNING")
    make_scheduler(now=at_now).drain()
    assert db.docs[f"tasks/{PARENT}"]["state"] == "PARKED"

    db.docs["tasks/task_c1"]["state"] = "SUCCEEDED"  # result_summary not written yet
    make_scheduler(now=at_now).drain()
    assert db.docs[f"tasks/{PARENT}"]["state"] == "PARKED"

    db.docs["tasks/task_c1"]["result_summary"] = {"artifacts": []}
    make_scheduler(now=at_now).drain()
    assert db.docs[f"tasks/{PARENT}"]["state"] == "READY"


def test_a_childs_end_in_another_tenant_does_not_count(db, make_scheduler):
    world(db)
    seed_tenant(db, "research")
    parked_parent(db)
    child(db, "task_mine", "RUNNING")
    child(db, "task_foreign", "SUCCEEDED", tenant="research", summary={"x": 1})
    make_scheduler(now=at_now).drain()
    assert db.docs[f"tasks/{PARENT}"]["state"] == "PARKED"


def test_past_the_deadline_the_outstanding_children_are_cancelled_then_it_is_promoted(
    db, make_scheduler
):
    """§5 F7: a child nobody will unblock does not hold its parent for ever."""
    world(db)
    parked_parent(db, parked_at=NOW - timedelta(days=2))
    stuck = child(db, "task_stuck", "PARKED")
    stuck["park_reason"] = ParkReason.CREDENTIAL_MISSING.value
    running = child(db, "task_running", "RUNNING")
    done = child(db, "task_done", "SUCCEEDED", summary={"artifacts": []})
    report = make_scheduler(now=at_now).drain()
    assert stuck["state"] == "CANCELLED" and stuck["end_cause"] == "child_cascade"
    assert stuck["metadata"]["child_cascade"]["why"] == "await_expired"
    # Invariant 1: a child that holds a lease is flagged, never released here.
    assert running["state"] == "RUNNING" and running["cancel_requested"] is True
    assert done["state"] == "SUCCEEDED"
    assert report.children_cascaded == 2
    assert db.docs[f"tasks/{PARENT}"]["state"] == "PARKED"

    running.update({"state": "CANCELLED", "end_cause": "child_cascade"})
    make_scheduler(now=at_now).drain()
    assert db.docs[f"tasks/{PARENT}"]["state"] == "READY"


def test_a_parent_that_awaited_on_its_last_attempt_is_dead_lettered(db, make_scheduler):
    """§5 F14: admission does not check the cap, so READY would lease one too many."""
    world(db)
    parked_parent(db, attempt_count=3, max_attempts=3)
    child(db, "task_c1", "SUCCEEDED", summary={"artifacts": []})
    report = make_scheduler(now=at_now).drain()
    assert report.dead_lettered == 1
    assert db.docs[f"tasks/{PARENT}"]["state"] == "DEAD_LETTERED"


def test_an_exhausted_awaiting_parent_is_dead_lettered_at_once_and_its_children_cascade(
    db, make_scheduler
):
    """§5 F14 then F6: no resume is coming, so the parent ends on the sweep
    that finds it, with its children still running, and they are cancelled as
    `parent_ended` rather than run for a consumer that is gone."""
    world(db)
    parked_parent(db, attempt_count=3, max_attempts=3)
    idle = child(db, "task_idle", "READY")
    running = child(db, "task_running", "RUNNING")
    make_scheduler(now=at_now).drain()
    assert db.docs[f"tasks/{PARENT}"]["state"] == "DEAD_LETTERED"
    make_scheduler(now=at_now).drain()
    assert idle["state"] == "CANCELLED" and idle["end_cause"] == "child_cascade"
    assert idle["metadata"]["child_cascade"]["why"] == "parent_ended"
    assert running["cancel_requested"] is True
    assert running["metadata"]["child_cascade"]["why"] == "parent_ended"


def test_the_await_sweep_reads_its_own_park_reason(db, make_scheduler):
    world(db)
    scheduler = make_scheduler(now=at_now)
    asked = []
    real_page = scheduler.store.parked_page

    def parked_page(reason, limit, after):
        asked.append(reason)
        return real_page(reason, limit, after)

    scheduler.store.parked_page = parked_page
    scheduler.drain()
    assert ParkReason.CHILDREN_INCOMPLETE in asked


def test_dependency_promotion_does_not_touch_an_awaiting_parent(db, make_scheduler):
    """Request 40's reason: DEPENDENCY_INCOMPLETE with no depends_on is promoted at once."""
    world(db)
    parked_parent(db)
    child(db, "task_c1", "RUNNING")
    report = make_scheduler(now=at_now).drain()
    assert report.promoted_dependencies == 0
    assert db.docs[f"tasks/{PARENT}"]["state"] == "PARKED"


# --------------------------------------------------------------------------
# §3.4 step 2: the cascade sweep
# --------------------------------------------------------------------------


def test_a_failed_parents_live_children_are_cancelled(db, make_scheduler):
    """§5 F6: nothing is left to consume their outputs."""
    world(db)
    seed_task(db, task_id=PARENT, tenant_id="eng", state="FAILED")
    idle = child(db, "task_idle", "READY")
    running = child(db, "task_running", "RUNNING")
    report = make_scheduler(now=at_now).drain()
    assert idle["state"] == "CANCELLED" and idle["end_cause"] == "child_cascade"
    assert idle["metadata"]["child_cascade"] == {"why": "parent_ended", "parent_task_id": PARENT}
    assert running["state"] == "RUNNING" and running["cancel_requested"] is True
    assert report.children_cascaded == 2 and report.cancelled >= 1
    # Idempotent: a second drain writes nothing more.
    events_before = len([p for p in db.docs if p.startswith("tasks/task_running/events/")])
    make_scheduler(now=at_now).drain()
    events_after = len([p for p in db.docs if p.startswith("tasks/task_running/events/")])
    assert events_after == events_before


def test_a_cancel_requested_parent_cascades_as_parent_cancelled(db, make_scheduler):
    """§3.4: the API's cascade died part-way; the sweep finishes it."""
    world(db)
    seed_task(db, task_id=PARENT, tenant_id="eng", state="RUNNING", cancel_requested=True)
    idle = child(db, "task_idle", "PARKED")
    make_scheduler(now=at_now).drain()
    assert idle["state"] == "CANCELLED"
    assert idle["metadata"]["child_cascade"]["why"] == "parent_cancelled"


def test_a_healthy_parent_and_a_retried_one_keep_their_children(db, make_scheduler):
    """§3.4: a retried or fenced parent keeps its children; they belong to the task."""
    world(db)
    seed_task(db, task_id=PARENT, tenant_id="eng", state="READY")
    kept = child(db, "task_kept", "RUNNING")
    make_scheduler(now=at_now).drain()
    assert kept["state"] == "RUNNING" and not kept.get("cancel_requested")


def test_a_child_naming_another_tenants_parent_is_left_alone(db, make_scheduler):
    """§5 F11: tenant first, before anything is written."""
    world(db)
    seed_tenant(db, "research")
    seed_task(db, task_id=PARENT, tenant_id="eng", state="CANCELLED")
    foreign = child(db, "task_foreign", "PARKED", tenant="research")
    make_scheduler(now=at_now).drain()
    assert foreign["state"] == "PARKED" and not foreign.get("cancel_requested")


def test_the_cascade_window_reaches_every_child(db, make_scheduler):
    """A page of healthy parents' children does not hide the ones after it."""
    world(db)
    settings = scheduler_settings(dependency_sweep_size=3)
    for i in range(4):
        seed_task(db, task_id=f"task_p{i}", tenant_id="eng", state="RUNNING")
        child(db, f"task_c{i}", "PARKED", parent=f"task_p{i}")
    seed_task(db, task_id="task_pz", tenant_id="eng", state="CANCELLED")
    late = child(db, "task_cz", "PARKED", parent="task_pz")
    scheduler = make_scheduler(now=at_now, settings=settings)
    scheduler.drain()
    scheduler.drain()
    assert late["state"] == "CANCELLED"


# --------------------------------------------------------------------------
# Contract request 41: every scheduler writer that ends a flagged child
# --------------------------------------------------------------------------


def _flagged(doc: dict) -> dict:
    """A child the cascade flagged while it held capacity: the flag and the
    marker are on it, and it is still to be ended by whoever reaches it."""
    doc["cancel_requested"] = True
    doc["metadata"] = {API_CASCADE_KEY: {"why": "parent_cancelled", "parent_task_id": PARENT}}
    return doc



def test_admission_ends_a_flagged_child_child_cascade(db, make_scheduler):
    """`_admit_one`'s cancel of a READY task with `cancel_requested`. Its parent
    is healthy, so the cascade sweep leaves it to admission."""
    world(db)
    seed_task(db, task_id=PARENT, tenant_id="eng", state="RUNNING")
    flagged = _flagged(child(db, "task_flagged", "READY"))
    make_scheduler(now=at_now).drain()
    assert flagged["state"] == "CANCELLED"
    assert flagged["end_cause"] == "child_cascade"


def test_admission_still_ends_an_ordinary_cancel_cancel_requested(db, make_scheduler):
    world(db)
    plain = seed_task(db, task_id="task_plain", tenant_id="eng", state="READY", cancel_requested=True)
    make_scheduler(now=at_now).drain()
    assert plain["state"] == "CANCELLED" and plain["end_cause"] == "cancel_requested"


def test_a_failed_dispatch_ends_a_flagged_child_child_cascade(db):
    """The dispatch-failure path's CANCELLED end (`return_to_ready_after_failed_dispatch`)."""
    from scheduler.store import SchedulerStore

    snapshot = Task(
        id="task_x", tenant_id="eng", runner_profile="mock", resource_class="standard",
        state=TaskState.READY, created_at=NOW, updated_at=NOW, input={},
        submitted_by="alice@saga.xyz", attempt_count=0, max_attempts=3, parent_task_id=PARENT,
    )
    lease = Lease(
        lease_id="lease_x", task_id="task_x", attempt_id="att_x", tenant_id="eng",
        generation=1, units=1, pools=["global"], state=TaskState.LEASED,
        created_at=NOW, dispatch_deadline=NOW + timedelta(seconds=300),
        expires_at=NOW + timedelta(seconds=120),
    )
    stored = snapshot.to_firestore()
    stored.update(
        {
            "state": TaskState.LEASED.value,
            "attempt_count": 1,
            "current_lease_id": lease.lease_id,
            "current_generation": 1,
        }
    )
    _flagged(stored)
    db.collection("tasks").document("task_x").set(stored)
    SchedulerStore(db, now=lambda: NOW).return_to_ready_after_failed_dispatch(
        snapshot, lease, "gke_create_job_failed"
    )
    doc = db.docs["tasks/task_x"]
    assert doc["state"] == "CANCELLED" and doc["end_cause"] == "child_cascade"


def test_the_scheduler_rule_is_the_workers():
    from agent_worker.control import cancel_end_cause as worker_rule

    for metadata in ({}, None, {API_CASCADE_KEY: {"why": "parent_ended"}}, {API_CASCADE_KEY: None}):
        assert children_mod.cancel_end_cause(metadata) is worker_rule({"metadata": metadata})
