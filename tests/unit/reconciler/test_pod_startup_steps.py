"""The pod's start-up steps become `startup_progress` events, once each, fenced (STARTUP-API).

The owner, 2026-10-13: a progress bar while a browser agent starts, with
"real pod steps". The reconciler already reads a starting GKE attempt's pod
for `start_problem` (F1-POD); the same read now yields `pod_scheduled`,
`image_pulling`, `image_pulled` and `container_started`, each dated by the
pod's own status (the reconciler's Role cannot read Kubernetes Events).

WHAT EACH TEST PINS:

  * the steps and their times are read off the pod status: the PodScheduled
    and PodReadyToStartContainers conditions, the first container to start
    (init and native sidecar included) and the first regular container;
  * a pod not yet scheduled has no step, and one still pulling has two;
  * `GkeBackend.start_read` reads the pod ONCE for the problem and the steps;
  * each step is written once per attempt, with the shared shape
    (`detail = {step, attempt_id, generation}`), at the pod's time -- and a
    second pass over the same pod writes nothing new;
  * a step read under a generation the task has moved past is not written
    (invariant 5), and nothing else moves: not the state, not the lease, not
    a pool (invariants 1-3);
  * a RUNNING task is still read within the start-up window, and not after.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore;
only the backend is scripted.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeBackend, FakeTransactionRunner

from reconciler.backends import GkeBackend
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.repair import Reconciler
from reconciler.startsteps import (
    POD_STARTUP_STEPS,
    StartRead,
    pod_start_steps,
    pods_start_steps,
    startup_event_id,
)
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import EventType, TaskState
from worker_seeds import TENANT

from test_pod_start_problem import (
    ATTEMPT,
    GKE,
    LEASE,
    NAMESPACE,
    POOLS,
    TASK,
    _CoreApi,
    dispatched_job,
    seed_attempt,
)

T0 = datetime(2026, 10, 13, 9, 0, 0, tzinfo=timezone.utc)
SCHEDULED = T0 + timedelta(seconds=2)
SANDBOX = T0 + timedelta(seconds=4)
SIDECAR_STARTED = T0 + timedelta(seconds=41)
WORKER_STARTED = T0 + timedelta(seconds=47)


def _condition(kind: str, at: datetime, status: str = "True") -> SimpleNamespace:
    return SimpleNamespace(type=kind, status=status, last_transition_time=at)


def _container(name: str, *, started: datetime | None = None, waiting: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        restart_count=0,
        state=SimpleNamespace(
            running=SimpleNamespace(started_at=started) if started else None,
            waiting=SimpleNamespace(reason=waiting, message=None) if waiting else None,
            terminated=None,
        ),
        last_state=SimpleNamespace(terminated=None, waiting=None, running=None),
    )


def _pod(
    *,
    scheduled: bool = True,
    sandbox: bool = True,
    sidecar: datetime | None = SIDECAR_STARTED,
    worker: datetime | None = WORKER_STARTED,
) -> SimpleNamespace:
    conditions = []
    if scheduled:
        conditions.append(_condition("PodScheduled", SCHEDULED))
    else:
        conditions.append(_condition("PodScheduled", T0, status="False"))
    if sandbox:
        conditions.append(_condition("PodReadyToStartContainers", SANDBOX))
    return SimpleNamespace(
        metadata=SimpleNamespace(name="swarm-pod", namespace=NAMESPACE, creation_timestamp=T0),
        status=SimpleNamespace(
            phase="Running" if worker else "Pending",
            reason=None,
            message=None,
            start_time=(T0 + timedelta(seconds=3)) if scheduled else None,
            conditions=conditions,
            init_container_statuses=[
                _container("display", started=sidecar, waiting=None if sidecar else "PodInitializing")
            ] if scheduled else [],
            container_statuses=[
                _container("worker", started=worker, waiting=None if worker else "PodInitializing")
            ] if scheduled else [],
        ),
    )


# ---------------------------------------------------------------------------
# The steps, off the pod status
# ---------------------------------------------------------------------------


def test_a_started_pod_has_all_four_steps_at_its_own_times():
    assert pod_start_steps(_pod()) == {
        "pod_scheduled": SCHEDULED,
        "image_pulling": SANDBOX,
        "image_pulled": SIDECAR_STARTED,
        "container_started": WORKER_STARTED,
    }


def test_a_pod_not_yet_scheduled_has_no_step():
    assert pod_start_steps(_pod(scheduled=False)) == {}


def test_a_pod_still_pulling_has_two_steps():
    steps = pod_start_steps(_pod(sidecar=None, worker=None))
    assert steps == {"pod_scheduled": SCHEDULED, "image_pulling": SANDBOX}


def test_without_the_sandbox_condition_kubelets_start_time_dates_the_pull():
    steps = pod_start_steps(_pod(sandbox=False, sidecar=None, worker=None))
    assert steps["image_pulling"] == T0 + timedelta(seconds=3)


def test_a_pod_read_as_plain_dicts_reads_the_same():
    pod = {
        "metadata": {"creation_timestamp": T0},
        "status": {
            "conditions": [{"type": "PodScheduled", "status": "True", "last_transition_time": SCHEDULED}],
            "start_time": SANDBOX,
            "init_container_statuses": [],
            "container_statuses": [
                {"name": "worker", "state": {"running": {"started_at": WORKER_STARTED}}}
            ],
        },
    }
    assert pod_start_steps(pod) == {
        "pod_scheduled": SCHEDULED,
        "image_pulling": SANDBOX,
        "image_pulled": WORKER_STARTED,
        "container_started": WORKER_STARTED,
    }


def test_a_pod_placed_on_a_warm_node_with_its_image_cached_still_reports_every_step():
    """LIVE-UNUSED (#1030), measured on task_84cb2038aa704c829d48 (2026-10-13
    13:3x UTC): the node was already up and the image already on it, and the
    console's bar waited on the pod's steps. Such a pod reads like any other:
    the scheduler still sets `PodScheduled` (onto the node that exists), and
    kubelet still dates each container's start -- `image_pulled` is the first
    start, though nothing was pulled. All within a second or two."""
    warm_scheduled = T0 + timedelta(milliseconds=300)
    pod = _pod(sidecar=T0 + timedelta(seconds=1), worker=T0 + timedelta(seconds=2))
    pod.status.conditions = [
        _condition("PodScheduled", warm_scheduled),
        _condition("Initialized", T0 + timedelta(seconds=1)),
        _condition("PodReadyToStartContainers", T0 + timedelta(milliseconds=800)),
        _condition("ContainersReady", T0 + timedelta(seconds=2)),
        _condition("Ready", T0 + timedelta(seconds=2)),
    ]
    assert pod_start_steps(pod) == {
        "pod_scheduled": warm_scheduled,
        "image_pulling": T0 + timedelta(milliseconds=800),
        "image_pulled": T0 + timedelta(seconds=1),
        "container_started": T0 + timedelta(seconds=2),
    }


def test_an_undated_pod_scheduled_leaves_out_that_step_only():
    """A step the status cannot date is left out -- that one, not every later
    one: the pod started its containers, and those times are the pod's own."""
    pod = _pod()
    pod.status.conditions[0].last_transition_time = None
    assert pod_start_steps(pod) == {
        "image_pulling": SANDBOX,
        "image_pulled": SIDECAR_STARTED,
        "container_started": WORKER_STARTED,
    }


def test_of_several_pods_the_newest_is_read():
    old = _pod(sidecar=None, worker=None)
    old.metadata.creation_timestamp = T0 - timedelta(minutes=5)
    assert pods_start_steps([old, _pod()])["container_started"] == WORKER_STARTED
    assert pods_start_steps([]) == {}


def test_gke_reads_the_pod_once_for_the_problem_and_the_steps():
    core = _CoreApi([_pod()])
    backend = GkeBackend(namespace_prefix="swarm-tenant-", batch_api=object(), core_api=core)

    found = backend.start_read(dispatched_job())

    assert len(core.calls) == 1
    assert found is not None and found.problem is None
    assert set(found.steps) == set(POD_STARTUP_STEPS)


def test_gke_reads_no_steps_outside_the_tenant_prefix():
    core = _CoreApi([_pod()])
    backend = GkeBackend(namespace_prefix="swarm-tenant-", batch_api=object(), core_api=core)

    assert backend.start_read(dispatched_job(namespace="agents-staging")) is None
    assert core.calls == []


# ---------------------------------------------------------------------------
# The reconciler writes them, once, fenced, and nothing else
# ---------------------------------------------------------------------------


class _StepBackend(FakeBackend):
    """A GKE backend whose one pod read is scripted."""

    def __init__(self, *args: Any, steps: dict[str, datetime] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.steps = dict(steps or {})
        self.reads: list[str] = []

    def start_read(self, execution: Any) -> StartRead:
        self.reads.append(execution.name)
        return StartRead(problem=None, steps=dict(self.steps))


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


def reconcile(db: Any, config: ReconcilerConfig, steps: dict[str, datetime]) -> tuple[Any, _StepBackend]:
    logger = build_logger(stream=io.StringIO())
    backend = _StepBackend(name=GKE, executions=[dispatched_job()], journal=db.writes, steps=steps)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()
    return report, backend


def step_events(db: Any) -> list[dict[str, Any]]:
    return [e for e in db.events(TASK) if e.get("type") == EventType.STARTUP_PROGRESS.value]


ALL_FOUR = pod_start_steps(_pod())


@pytest.mark.parametrize("state", [TaskState.DISPATCHED, TaskState.STARTING])
def test_each_step_is_written_once_with_the_shared_shape_and_nothing_else_moves(db, config, state):
    seed_attempt(db, state=state)
    before_task = dict(db.doc(f"tasks/{TASK}"))
    before_lease = dict(db.doc(f"leases/{LEASE}"))
    before_pools = {p: dict(db.doc(f"pools/{p}")) for p in POOLS}

    report, backend = reconcile(db, config, ALL_FOUR)

    assert backend.reads, "the pod was never read"
    events = step_events(db)
    assert sorted(e["detail"]["step"] for e in events) == sorted(POD_STARTUP_STEPS)
    for event in events:
        step = event["detail"]["step"]
        assert event["event_id"] == startup_event_id(ATTEMPT, step)
        assert event["detail"] == {"step": step, "attempt_id": ATTEMPT, "generation": 1}
        assert event["attempt_id"] == ATTEMPT and event["lease_id"] == LEASE
        assert event["generation"] == 1
        assert event["at"] == ALL_FOUR[step], "the pod's time, not the pass's"
    # Invariants 1-3: a record; no state, lease or capacity moved.
    assert db.doc(f"tasks/{TASK}") == before_task
    assert db.doc(f"leases/{LEASE}") == before_lease
    assert {p: db.doc(f"pools/{p}") for p in POOLS} == before_pools
    assert report.outcomes == []


def test_a_second_pass_over_the_same_pod_writes_nothing_new(db, config):
    seed_attempt(db, state=TaskState.DISPATCHED)
    reconcile(db, config, ALL_FOUR)
    first = step_events(db)

    reconcile(db, config, ALL_FOUR)

    assert step_events(db) == first
    assert len(first) == 4


def test_steps_seen_across_passes_are_each_written_once(db, config):
    seed_attempt(db, state=TaskState.DISPATCHED)
    early = {k: v for k, v in ALL_FOUR.items() if k in ("pod_scheduled", "image_pulling")}

    reconcile(db, config, early)
    assert sorted(e["detail"]["step"] for e in step_events(db)) == ["image_pulling", "pod_scheduled"]

    reconcile(db, config, ALL_FOUR)
    assert sorted(e["detail"]["step"] for e in step_events(db)) == sorted(POD_STARTUP_STEPS)


def test_a_step_read_under_a_stale_generation_is_not_written(db, config):
    """The task moved to generation 2; the pod was read under the lease's generation 1."""
    seed_attempt(db, state=TaskState.DISPATCHED)
    db.doc(f"tasks/{TASK}")["current_generation"] = 2

    reconcile(db, config, ALL_FOUR)

    assert step_events(db) == []


def test_an_execution_stamped_with_another_generation_is_not_read(db, config):
    seed_attempt(db, state=TaskState.DISPATCHED)
    logger = build_logger(stream=io.StringIO())
    stale = dispatched_job()
    object.__setattr__(stale, "generation", 7)
    backend = _StepBackend(name=GKE, executions=[stale], journal=db.writes, steps=ALL_FOUR)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()

    assert backend.reads == []
    assert step_events(db) == []


def test_the_store_writes_nothing_for_another_lease(db, config):
    seed_attempt(db, state=TaskState.DISPATCHED)
    store = ControlStore(db, logger=build_logger(stream=io.StringIO()), txn_runner=FakeTransactionRunner(db))

    written = store.record_startup_step(
        TASK, tenant_id=TENANT, lease_id="lease_other", attempt_id=ATTEMPT,
        generation=1, step="pod_scheduled", at=SCHEDULED,
    )

    assert written is False
    assert step_events(db) == []


def test_a_running_task_is_read_within_the_window(db, config):
    seed_attempt(db, state=TaskState.RUNNING)

    _, backend = reconcile(db, config, ALL_FOUR)

    assert backend.reads, "a RUNNING task within the window was not read"
    assert len(step_events(db)) == 4


def test_a_running_task_past_the_window_is_not_read(db, config):
    seed_attempt(db, state=TaskState.RUNNING)
    db.doc(f"leases/{LEASE}")["created_at"] = utcnow() - timedelta(minutes=30)

    _, backend = reconcile(db, config, ALL_FOUR)

    assert backend.reads == []
    assert step_events(db) == []


def test_a_dry_run_writes_no_step(db, config):
    seed_attempt(db, state=TaskState.DISPATCHED)
    dry = replace(config, dry_run=True)

    reconcile(db, dry, ALL_FOUR)

    assert step_events(db) == []


def test_a_warm_start_that_is_already_running_at_the_first_pass_still_writes_pod_scheduled(db, config):
    """LIVE-UNUSED (#1030): on a warm node with the image cached the worker is
    RUNNING within seconds, before any pass sees it DISPATCHED. The pass that
    first sees it RUNNING still reads the pod and writes every step it dates,
    `pod_scheduled` and `image_pulled` included."""
    seed_attempt(db, state=TaskState.RUNNING)
    warm = _pod(sidecar=SCHEDULED + timedelta(seconds=1), worker=SCHEDULED + timedelta(seconds=2))

    _, backend = reconcile(db, config, pod_start_steps(warm))

    assert backend.reads, "a warm start's pod was never read"
    written = {e["detail"]["step"]: e["at"] for e in step_events(db)}
    assert written["pod_scheduled"] == SCHEDULED
    assert written["image_pulled"] == SCHEDULED + timedelta(seconds=1)
    assert set(written) == set(POD_STARTUP_STEPS)
