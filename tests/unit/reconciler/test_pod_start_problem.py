"""A task whose GKE pod cannot start says why, on the task (F1-POD).

WHAT WAS WRONG. Measured on 2026-10-11 20:58-21:05 PDT: every
claude-code-browser pod's sidecar crash-looped (`CrashLoopBackOff`, "No module
named agent_worker.livegate.__main__") for about three minutes, while the task
page read only "dispatched" / "account not assigned yet" -- and after the
cancel, "never ran", although the pod had started. Nothing on the platform
read the pod's container statuses until the Job had ENDED
(`GkeBackend.termination`), and a crash-looping sidecar never ends a Job.

WHAT EACH TEST PINS:

  * a container Waiting in a start-failure reason -- init containers and
    native sidecars included, which kubelet reports under
    `init_container_statuses` -- or one that has restarted, is a start problem;
    a pod whose containers all run cleanly, or that is still pulling, is not;
  * the record is bounded: one printable line of at most 300 characters;
  * `GkeBackend.start_problem` reads the pod from the `pods list` the
    `swarm-reaper` Role already grants, and never outside the tenant prefix;
  * the reconciler writes it to `metadata.start_problem` of a DISPATCHED or
    STARTING task, and changes nothing else: not the state, not the lease,
    not a pool (invariants 1-3);
  * it is cleared when the pod reads clean, and when the task reaches RUNNING;
  * a dry run writes nothing; the key is the one swarm-api reserves.

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
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.backends import GkeBackend
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView, TaskView
from reconciler.repair import Reconciler
from reconciler.startproblem import (
    MAX_START_MESSAGE_CHARS,
    START_PROBLEM_KEY,
    START_PROBLEM_REASONS,
    StartProblem,
    pod_start_problem,
    start_problem_of,
)
from reconciler.store import ControlStore
from swarm_common.models import pool_names_for, utcnow
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_tenant

GKE = "GKE_AUTOPILOT"
NAMESPACE = "swarm-tenant-eng"
TASK, LEASE, ATTEMPT = "task_f1pod0000000000001", "lease_f1pod", "att_f1pod"
JOB_NAME = "swarm-f1pod0000000000001-1"
POOLS = pool_names_for(
    tenant_id=TENANT,
    provider="anthropic",
    resource_class="standard",
    runner_profile="claude-code-browser",
    backend=GKE,
)

#: kubelet's words for the 2026-10-11 sidecar, as `kubectl describe` read them.
LIVEGATE_BACKOFF = (
    "back-off 40s restarting failed container=livegate "
    "pod=swarm-f1pod0000000000001-1-abcde_swarm-tenant-eng(0f1e2d3c)"
)


# ---------------------------------------------------------------------------
# Pod statuses, as the kubernetes client returns them
# ---------------------------------------------------------------------------


def _status(
    name: str,
    *,
    waiting: str | None = None,
    message: str | None = None,
    running: bool = False,
    restarts: int = 0,
    last_exit: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        restart_count=restarts,
        state=SimpleNamespace(
            waiting=SimpleNamespace(reason=waiting, message=message) if waiting else None,
            running=SimpleNamespace(started_at=utcnow()) if running else None,
            terminated=None,
        ),
        last_state=SimpleNamespace(
            terminated=(
                SimpleNamespace(exit_code=last_exit, reason="Error", message=None)
                if last_exit is not None
                else None
            ),
            waiting=None,
            running=None,
        ),
    )


def _pod(
    *,
    init: list[Any] | None = None,
    containers: list[Any] | None = None,
    started: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=f"{JOB_NAME}-abcde", namespace=NAMESPACE, creation_timestamp=utcnow()
        ),
        status=SimpleNamespace(
            phase="Pending",
            reason=None,
            message=None,
            start_time=utcnow() if started else None,
            init_container_statuses=list(init or []),
            container_statuses=list(containers or []),
        ),
    )


def crash_looping_sidecar() -> SimpleNamespace:
    """The 2026-10-11 shape: the native sidecar crash-loops, the worker waits on it."""
    return _pod(
        init=[
            _status(
                "livegate",
                waiting="CrashLoopBackOff",
                message=LIVEGATE_BACKOFF,
                restarts=4,
                last_exit=1,
            )
        ],
        containers=[_status("worker", waiting="PodInitializing")],
    )


def test_a_crash_looping_native_sidecar_is_a_start_problem():
    problem = pod_start_problem(crash_looping_sidecar())

    assert problem is not None
    assert problem.container == "livegate"
    assert problem.reason == "CrashLoopBackOff"
    assert problem.restarts == 4
    assert "restarting failed container=livegate" in problem.message
    assert problem.pod_started is True


@pytest.mark.parametrize("reason", sorted(START_PROBLEM_REASONS))
def test_every_start_failure_reason_is_a_start_problem(reason):
    pod = _pod(containers=[_status("worker", waiting=reason, message="it said why")])

    problem = pod_start_problem(pod)

    assert problem is not None and problem.reason == reason
    assert problem.container == "worker"


def test_the_reasons_are_the_six_kubelet_start_failures():
    assert START_PROBLEM_REASONS == {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "CreateContainerError",
        "RunContainerError",
    }


def test_a_container_that_restarted_is_a_start_problem_even_while_it_runs():
    pod = _pod(containers=[_status("worker", running=True, restarts=2, last_exit=137)])

    problem = pod_start_problem(pod)

    assert problem is not None
    assert problem.container == "worker" and problem.restarts == 2
    assert problem.reason == "Error"


@pytest.mark.parametrize(
    "pod",
    [
        _pod(init=[_status("livegate", running=True)], containers=[_status("worker", running=True)]),
        _pod(containers=[_status("worker", waiting="ContainerCreating")], started=False),
        _pod(containers=[_status("worker", waiting="PodInitializing")]),
        _pod(),
    ],
    ids=["all-running", "creating", "initializing", "no-statuses"],
)
def test_a_pod_starting_normally_has_no_start_problem(pod):
    assert pod_start_problem(pod) is None


def test_the_worst_container_is_named():
    pod = _pod(
        init=[_status("livegate", running=True, restarts=1, last_exit=1)],
        containers=[_status("worker", waiting="ImagePullBackOff", message="pull failed")],
    )

    problem = pod_start_problem(pod)

    # A container stuck in a start failure outranks one that restarted and runs.
    assert problem is not None and problem.container == "worker"


def test_the_message_is_one_bounded_printable_line():
    long = "line one\nline\ttwo \x07" + "x" * 1000
    pod = _pod(containers=[_status("worker", waiting="CreateContainerError", message=long)])

    problem = pod_start_problem(pod)

    assert problem is not None
    assert len(problem.message) <= MAX_START_MESSAGE_CHARS
    assert "\n" not in problem.message and "\t" not in problem.message
    assert problem.message.isprintable()


def test_a_pod_read_as_plain_dicts_reads_the_same():
    pod = {
        "status": {
            "start_time": "2026-10-11T20:58:00Z",
            "init_container_statuses": [
                {
                    "name": "livegate",
                    "restart_count": 3,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "m"}},
                }
            ],
        }
    }

    problem = pod_start_problem(pod)

    assert problem is not None and problem.container == "livegate" and problem.restarts == 3


# ---------------------------------------------------------------------------
# GkeBackend reads the pod from the grant it already holds
# ---------------------------------------------------------------------------


class _CoreApi:
    def __init__(self, pods: list[Any]) -> None:
        self.pods = pods
        self.calls: list[tuple[str, str]] = []

    def list_namespaced_pod(self, *, namespace: str, label_selector: str) -> Any:
        self.calls.append((namespace, label_selector))
        return SimpleNamespace(items=list(self.pods))


def dispatched_job(namespace: str = NAMESPACE) -> ExecutionView:
    return ExecutionView(
        name=JOB_NAME,
        backend=GKE,
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(minutes=2),
        task_id=TASK,
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        generation=1,
        namespace=namespace,
    )


def test_gke_reads_the_start_problem_off_the_jobs_pod():
    core = _CoreApi([crash_looping_sidecar()])
    backend = GkeBackend(namespace_prefix="swarm-tenant-", batch_api=object(), core_api=core)

    problem = backend.start_problem(dispatched_job())

    assert problem is not None and problem.container == "livegate"
    assert core.calls and core.calls[0][0] == NAMESPACE
    assert core.calls[0][1].endswith(f"={JOB_NAME}")


def test_gke_reads_nothing_outside_the_tenant_prefix():
    core = _CoreApi([crash_looping_sidecar()])
    backend = GkeBackend(namespace_prefix="swarm-tenant-", batch_api=object(), core_api=core)

    assert backend.start_problem(dispatched_job(namespace="agents-staging")) is None
    assert core.calls == []


# ---------------------------------------------------------------------------
# The reconciler writes it on the task, and nothing else
# ---------------------------------------------------------------------------


class _PodBackend(FakeBackend):
    """A GKE backend whose pod start reading is scripted."""

    def __init__(self, *args: Any, problem: StartProblem | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.problem = problem
        self.start_reads: list[str] = []

    def start_problem(self, execution: Any) -> StartProblem | None:
        self.start_reads.append(execution.name)
        return self.problem


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


def seed_attempt(
    db: FakeFirestore, *, state: TaskState, metadata: dict[str, Any] | None = None
) -> None:
    now = utcnow()
    admitted = now - timedelta(minutes=2)
    db.seed(
        f"tasks/{TASK}",
        {
            "id": TASK, "tenant_id": TENANT, "state": state.value,
            "runner_profile": "claude-code-browser", "resource_class": "standard",
            "current_generation": 1, "current_lease_id": LEASE,
            "attempt_count": 1, "max_attempts": 3,
            "updated_at": admitted, "cancel_requested": False,
            "started_at": admitted if state is TaskState.RUNNING else None,
            "last_error": None, "metadata": dict(metadata or {}),
        },
    )
    db.seed(
        f"leases/{LEASE}",
        {
            "lease_id": LEASE, "task_id": TASK, "attempt_id": ATTEMPT,
            "tenant_id": TENANT, "generation": 1,
            "pools": POOLS, "units": 1, "state": state.value,
            "created_at": admitted,
            "dispatch_deadline": admitted + timedelta(seconds=900),
            "expires_at": now + timedelta(seconds=300),
            "heartbeat_at": now - timedelta(seconds=5),
            "released_at": None,
        },
    )
    db.seed(
        f"attempts/{ATTEMPT}",
        {
            "attempt_id": ATTEMPT, "task_id": TASK, "tenant_id": TENANT,
            "generation": 1, "lease_id": LEASE, "backend": GKE,
            "created_at": admitted, "execution_name": f"{NAMESPACE}/{JOB_NAME}",
        },
    )
    for pool in POOLS:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 1,
                                  "enabled": True, "updated_at": now})
    seed_tenant(db)


def reconcile(
    db: FakeFirestore, config: ReconcilerConfig, problem: StartProblem | None
) -> tuple[Any, _PodBackend]:
    logger = build_logger(stream=io.StringIO())
    backend = _PodBackend(
        name=GKE, executions=[dispatched_job()], journal=db.writes, problem=problem
    )
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()
    return report, backend


SIDECAR = StartProblem(
    container="livegate",
    reason="CrashLoopBackOff",
    restarts=4,
    message=LIVEGATE_BACKOFF,
    pod_started=True,
)


@pytest.mark.parametrize("state", [TaskState.DISPATCHED, TaskState.STARTING])
def test_the_start_problem_is_written_on_the_task_and_nothing_else_moves(db, config, state):
    seed_attempt(db, state=state)
    before_lease = dict(db.doc(f"leases/{LEASE}"))
    before_pools = {p: dict(db.doc(f"pools/{p}")) for p in POOLS}

    report, backend = reconcile(db, config, SIDECAR)

    assert backend.start_reads == [JOB_NAME], "the pod was never read"
    task = db.doc(f"tasks/{TASK}")
    recorded = task["metadata"][START_PROBLEM_KEY]
    assert recorded["container"] == "livegate"
    assert recorded["reason"] == "CrashLoopBackOff"
    assert recorded["restarts"] == 4
    assert recorded["message"] == LIVEGATE_BACKOFF
    assert recorded["pod_started"] is True
    assert recorded["attempt_id"] == ATTEMPT
    assert isinstance(recorded["seen_at"], datetime)
    # Invariants 1-3: no state transition, no lease or capacity movement.
    assert task["state"] == state.value
    assert db.doc(f"leases/{LEASE}") == before_lease
    assert {p: db.doc(f"pools/{p}") for p in POOLS} == before_pools
    assert not backend.terminated
    assert report.outcomes == []


def test_the_start_problem_is_cleared_when_the_pod_reads_clean(db, config):
    seed_attempt(
        db,
        state=TaskState.STARTING,
        metadata={START_PROBLEM_KEY: {"container": "livegate", "reason": "CrashLoopBackOff",
                                      "restarts": 4, "message": "m", "pod_started": True,
                                      "attempt_id": ATTEMPT}, "keep": "me"},
    )

    reconcile(db, config, None)

    metadata = db.doc(f"tasks/{TASK}")["metadata"]
    assert START_PROBLEM_KEY not in metadata
    assert metadata["keep"] == "me", "the caller's metadata must survive the clear"


def test_the_start_problem_is_cleared_when_the_task_runs(db, config):
    seed_attempt(
        db,
        state=TaskState.RUNNING,
        metadata={START_PROBLEM_KEY: {"container": "livegate", "reason": "CrashLoopBackOff",
                                      "restarts": 4, "message": "m", "pod_started": True,
                                      "attempt_id": ATTEMPT}},
    )

    _, backend = reconcile(db, config, SIDECAR)

    assert backend.start_reads == [], "a RUNNING task's pod is not read"
    assert START_PROBLEM_KEY not in db.doc(f"tasks/{TASK}")["metadata"]


def test_a_clean_pod_on_a_task_with_nothing_recorded_writes_nothing(db, config):
    seed_attempt(db, state=TaskState.DISPATCHED)
    writes_before = len(db.writes)

    reconcile(db, config, None)

    task_writes = [w for w in db.writes[writes_before:] if w[1] == f"tasks/{TASK}"]
    assert task_writes == []


def test_a_dry_run_writes_no_start_problem(db, config):
    seed_attempt(db, state=TaskState.DISPATCHED)
    dry = replace(config, dry_run=True)

    reconcile(db, dry, SIDECAR)

    assert START_PROBLEM_KEY not in db.doc(f"tasks/{TASK}")["metadata"]


def test_a_failed_pod_read_concludes_nothing(db, config):
    seed_attempt(
        db,
        state=TaskState.DISPATCHED,
        metadata={START_PROBLEM_KEY: {"container": "c", "reason": "r", "restarts": 1,
                                      "message": "m", "pod_started": True,
                                      "attempt_id": ATTEMPT}},
    )

    class _Broken(_PodBackend):
        def start_problem(self, execution: Any) -> StartProblem | None:
            raise RuntimeError("403 pods is forbidden")

    logger = build_logger(stream=io.StringIO())
    backend = _Broken(name=GKE, executions=[dispatched_job()], journal=db.writes)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()

    assert START_PROBLEM_KEY in db.doc(f"tasks/{TASK}")["metadata"], "a failed read cleared it"
    assert any("start_problem" in e for e in report.errors)


def test_the_task_view_reads_the_recorded_problem():
    recorded = {"container": "livegate", "reason": "CrashLoopBackOff"}
    view = TaskView.from_doc(
        {"id": TASK, "state": "DISPATCHED", "metadata": {START_PROBLEM_KEY: recorded}}
    )

    assert view.start_problem == recorded
    assert start_problem_of({START_PROBLEM_KEY: "not a map"}) is None
    assert start_problem_of(None) is None


def test_the_key_is_the_one_swarm_api_reserves():
    from swarm_api.validation import RESERVED_METADATA_KEYS, START_PROBLEM_METADATA_KEY

    assert START_PROBLEM_KEY == START_PROBLEM_METADATA_KEY == "start_problem"
    assert START_PROBLEM_KEY in RESERVED_METADATA_KEYS


def test_seen_at_is_utc():
    record = SIDECAR.record(attempt_id=ATTEMPT, seen_at=datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc))
    assert record["seen_at"].tzinfo is not None
    assert set(record) == {
        "container", "reason", "restarts", "message", "pod_started", "attempt_id", "seen_at",
    }


def test_an_earlier_attempts_record_is_cleared(db, config):
    seed_attempt(
        db,
        state=TaskState.DISPATCHED,
        metadata={START_PROBLEM_KEY: {"container": "livegate", "reason": "CrashLoopBackOff",
                                      "restarts": 4, "message": "m", "pod_started": True,
                                      "attempt_id": "att_earlier"}},
    )

    reconcile(db, config, None)

    assert START_PROBLEM_KEY not in db.doc(f"tasks/{TASK}")["metadata"]
