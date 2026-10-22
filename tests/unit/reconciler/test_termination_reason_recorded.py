"""Why an execution ended reaches the attempt and the task, not only a log line (#1822).

WHAT WAS WRONG. In the 48 h to 2026-10-21, 9 claude-code attempts across 7
tasks were OOMKilled at their fixed 8 GiB, 3 more were evicted for node
memory and 1 for `/tmp`. The kill reason appeared only in the reconciler's
"read how an execution ended" log line. task_664c16eb was OOM-killed on
attempts 1 and 3 and ended FAILED lost_worker with a `last_error` that never
said OOM: a person reading the task could not tell a memory problem from a
dead node.

WHAT EACH TEST PINS:

  * the GKE read names an OOMKilled worker container with its memory limit,
    from the pod spec the same `pods list` returns; Cloud Run's memory-limit
    message reads as OOMKilled too;
  * an execution that ended OOMKilled leaves "OOMKilled" and the limit in the
    attempt's `error` and the task's `last_error`, on a stale-lease reclaim
    and on an ended-at-startup end (which, since #1926, fails the task);
  * an evicted execution carries kubelet's message, even when the eviction
    rule's grace has not run and the stale-lease rule reclaims it first;
  * an execution with no reason keeps today's words;
  * the end cause's category, the slot release and the generation fence are
    exactly what they are for the same reclaim with no reason read.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore,
with the scripted GKE backend of the eviction suite.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner
from test_worker_evicted import (
    ATTEMPT,
    GKE,
    JOB_NAME,
    LEASE,
    NAMESPACE,
    NODE_PRESSURE,
    POOLS,
    TASK,
    failed_job,
    seed_a_running_attempt,
)

from reconciler.backends import CloudRunBackend, GkeBackend
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger as build_reconciler_logger
from reconciler.model import ExecutionPhase, ExecutionView, Termination
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import EndCause, utcnow
from swarm_common.states import TaskState


@pytest.fixture
def reconciler_config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id="saga-agents-staging",
        region="us-central1",
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


# ---------------------------------------------------------------------------
# The backends' reads
# ---------------------------------------------------------------------------


class _CoreApi:
    def __init__(self, pods: list[Any]) -> None:
        self.pods = pods

    def list_namespaced_pod(self, *, namespace: str, label_selector: str) -> Any:
        return SimpleNamespace(items=list(self.pods))


def _oom_pod(*, memory: str | None = "8Gi") -> SimpleNamespace:
    """A pod whose worker container the kernel OOM-killed, as the kubernetes client reads it."""
    limits = {"cpu": "2", "memory": memory} if memory else {"cpu": "2"}
    return SimpleNamespace(
        metadata=SimpleNamespace(name=f"{JOB_NAME}-abcde", namespace=NAMESPACE),
        spec=SimpleNamespace(
            containers=[
                SimpleNamespace(
                    name="worker",
                    resources=SimpleNamespace(limits=limits, requests=dict(limits)),
                )
            ]
        ),
        status=SimpleNamespace(
            reason=None,
            message=None,
            container_statuses=[
                SimpleNamespace(
                    name="worker",
                    state=SimpleNamespace(
                        terminated=SimpleNamespace(
                            exit_code=137, message=None, reason="OOMKilled", finished_at=None
                        )
                    ),
                    last_state=SimpleNamespace(terminated=None),
                )
            ],
        ),
    )


def _gke_read(pod: Any) -> Termination:
    backend = GkeBackend(
        namespace_prefix="swarm-tenant-", batch_api=object(), core_api=_CoreApi([pod])
    )
    found = backend.termination(failed_job())
    assert found is not None
    return found


def test_gke_names_an_oom_kill_with_the_containers_memory_limit():
    found = _gke_read(_oom_pod())

    # MUTATION: drop the OOMKilled branch in GkeBackend.termination -> detail
    # reads "container worker: OOMKilled" with no limit, and this fails.
    assert "OOMKilled: the container passed its 8 GiB memory limit" in found.detail, found
    assert found.exit_code == 137
    assert found.eviction is None


def test_an_oom_kill_with_no_readable_limit_still_says_oomkilled():
    found = _gke_read(_oom_pod(memory=None))

    # MUTATION: require a limit before naming the kill -> "OOMKilled" vanishes.
    assert "OOMKilled: the container passed its memory limit" in found.detail, found


class _Tasks:
    def __init__(self, code: int, message: str) -> None:
        self._task = SimpleNamespace(
            last_attempt_result=SimpleNamespace(
                exit_code=code, status=SimpleNamespace(message=message)
            )
        )

    def list_tasks(self, *, parent: str) -> list[Any]:
        return [self._task]


def _cloud_run_read(code: int, message: str) -> Termination | None:
    backend = CloudRunBackend("p", "us-central1", tasks_client=_Tasks(code, message))
    execution = ExecutionView(
        name="projects/p/locations/us-central1/jobs/swarm-x/executions/swarm-x-abc",
        backend="CLOUD_RUN_JOB",
        phase=ExecutionPhase.FAILED,
        created_at=utcnow(),
    )
    return backend.termination(execution)


def test_cloud_runs_memory_limit_message_reads_as_an_oom_kill():
    said = "Memory limit of 8192 MiB exceeded with 8243 MiB used. Consider increasing the limit."
    found = _cloud_run_read(137, said)

    assert found is not None
    # MUTATION: drop the memory-limit match in CloudRunBackend.termination ->
    # the detail is Cloud Run's sentence alone, with no "OOMKilled".
    assert found.detail.startswith("OOMKilled: "), found
    assert "8192 MiB" in found.detail, found


def test_cloud_runs_other_exit_messages_are_left_as_they_were():
    said = "Task swarm-x-abc-task0 failed with exit code: 1"
    found = _cloud_run_read(1, said)

    assert found is not None
    # MUTATION: prefix every Cloud Run detail with "OOMKilled" -> fails.
    assert found.detail == said, found


# ---------------------------------------------------------------------------
# The reconciler: the reason on the attempt and the task
# ---------------------------------------------------------------------------


def _reconcile(
    db: FakeFirestore,
    config: ReconcilerConfig,
    termination: Any,
    *,
    ended_seconds_ago: float = 60,
) -> Any:
    logger = build_reconciler_logger(stream=io.StringIO())
    backend = FakeBackend(
        name=GKE,
        executions=[failed_job(ended_seconds_ago=ended_seconds_ago)],
        journal=db.writes,
        terminations={JOB_NAME: termination},
    )
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()


def _oom() -> Termination:
    """What `GkeBackend.termination` reads off the OOM-killed pod above."""
    return _gke_read(_oom_pod())


def _attempt_error(db: FakeFirestore) -> str:
    return str(db.doc(f"attempts/{ATTEMPT}").get("error") or "")


def test_an_oom_killed_reclaim_names_the_kill_and_its_limit(db, reconciler_config):
    """task_664c16eb's last attempt: RUNNING, attempts spent, OOM-killed."""
    seed_a_running_attempt(db, attempt_count=3, max_attempts=3, heartbeat_seconds_ago=200)

    report = _reconcile(db, reconciler_config, _oom())

    assert [o.kind for o in report.outcomes] == ["stale_lease"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    # MUTATION: leave the termination's words off the repair's error -> the
    # task reads "exit code 137 (container worker: OOMKilled)" with no limit.
    assert "OOMKilled" in task["last_error"], task["last_error"]
    assert "8 GiB memory limit" in task["last_error"], task["last_error"]
    # MUTATION: pass finding.reason, not the enriched words, as attempt_reason.
    assert "OOMKilled" in _attempt_error(db), _attempt_error(db)
    assert "8 GiB memory limit" in _attempt_error(db), _attempt_error(db)
    # The category is the reclaim's, as it was: only its words say why.
    # MUTATION: map an OOM to another EndCause -> fails.
    assert task["state"] == TaskState.FAILED.value, task
    assert task["end_cause"] == EndCause.LOST_WORKER.value, task


def test_an_oom_kill_before_the_runner_started_is_named_and_not_requeued(db, reconciler_config):
    """The ended-at-startup rule said only "exited 137 before its runner started".

    Since #1926 an OOM kill is not retried at the same size, at startup or
    later: the task FAILS, with the kill and its limit in its words.
    """
    seed_a_running_attempt(db, heartbeat_seconds_ago=30)
    db.doc(f"tasks/{TASK}")["state"] = TaskState.STARTING.value
    db.doc(f"leases/{LEASE}")["state"] = TaskState.STARTING.value

    report = _reconcile(db, reconciler_config, _oom())

    assert [o.kind for o in report.outcomes] == ["worker_ended_at_startup"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    # MUTATION: skip the ended-at-startup branch's `what_ended` error -> the
    # task reads "exited 137 before its runner started" alone.
    assert "OOMKilled: the container passed its 8 GiB memory limit" in task["last_error"], task
    assert "OOMKilled" in _attempt_error(db), _attempt_error(db)
    # MUTATION: leave the ended-at-startup rule out of the OOM fail (#1926) ->
    # the task is refunded and requeued READY.
    assert task["state"] == TaskState.FAILED.value, task


def test_an_eviction_reclaimed_before_its_grace_carries_kubelets_message(db, reconciler_config):
    """Inside the eviction rule's grace the stale-lease rule wins, and said "lost"."""
    seed_a_running_attempt(db, heartbeat_seconds_ago=200)
    evicted = Termination(
        exit_code=137,
        detail="container worker: ContainerStatusUnknown",
        eviction=NODE_PRESSURE.strip(),
    )

    report = _reconcile(db, reconciler_config, evicted, ended_seconds_ago=5)

    assert [o.kind for o in report.outcomes] == ["stale_lease"], report.as_dict()
    error = db.doc(f"tasks/{TASK}")["last_error"]
    # MUTATION: ignore `Termination.eviction` in the repair's words -> only
    # "the worker is lost" remains, and this fails.
    assert "Evicted: The node was low on resource: ephemeral-storage" in error, error
    assert "Evicted: The node was low on resource" in _attempt_error(db), _attempt_error(db)


def test_an_execution_with_no_reason_keeps_todays_words(db, reconciler_config):
    seed_a_running_attempt(db, heartbeat_seconds_ago=200)

    _reconcile(
        db,
        reconciler_config,
        Termination(exit_code=None, detail="the backend recorded no exit code for this execution"),
    )

    error = db.doc(f"tasks/{TASK}")["last_error"]
    # MUTATION: append the backend's detail whatever it says -> the error no
    # longer ends where today's does.
    assert error.endswith("its execution has ended (failed): the worker is lost"), error
    assert "OOMKilled" not in error and "Evicted" not in error, error
    assert _attempt_error(db).endswith("the worker is lost"), _attempt_error(db)


def test_a_crash_already_named_by_its_detail_is_not_named_twice(db, reconciler_config):
    seed_a_running_attempt(db, heartbeat_seconds_ago=200)

    _reconcile(db, reconciler_config, Termination(exit_code=1, detail="container worker: Error"))

    error = db.doc(f"tasks/{TASK}")["last_error"]
    # MUTATION: drop the already-said check -> the detail appears twice.
    assert error.count("container worker: Error") == 1, error


def _slots_and_fence(db: FakeFirestore) -> dict[str, Any]:
    lease = db.doc(f"leases/{LEASE}")
    task = db.doc(f"tasks/{TASK}")
    return {
        "released": lease.get("released_at") is not None,
        "pools": {pool: db.doc(f"pools/{pool}")["active"] for pool in POOLS},
        "generation": task["current_generation"],
        "lease_id": task.get("current_lease_id"),
        # Not the state: an OOM kill FAILS the task where the reclaim with no
        # reason requeues it (#1926, `test_out_of_memory_named`). What it
        # holds and what fences it are the same either way.
        "attempt_count": task["attempt_count"],
    }


@pytest.mark.parametrize(
    "termination",
    [
        pytest.param(lambda: _oom(), id="oom"),
        pytest.param(
            lambda: Termination(
                exit_code=137,
                detail="container worker: ContainerStatusUnknown",
                eviction=NODE_PRESSURE.strip(),
            ),
            id="evicted",
        ),
    ],
)
def test_the_reason_changes_no_slot_and_no_fence(reconciler_config, termination):
    """Read and recorded, nothing more: invariants 3 and 5 hold exactly as without it."""
    with_reason, without = FakeFirestore(), FakeFirestore()
    seed_a_running_attempt(with_reason, heartbeat_seconds_ago=200)
    seed_a_running_attempt(without, heartbeat_seconds_ago=200)

    _reconcile(with_reason, reconciler_config, termination(), ended_seconds_ago=5)
    _reconcile(
        without,
        reconciler_config,
        Termination(exit_code=None, detail="the backend recorded no exit code"),
        ended_seconds_ago=5,
    )

    # MUTATION: let the reason skip the release or the fence -> the two differ.
    assert _slots_and_fence(with_reason) == _slots_and_fence(without)
    assert _slots_and_fence(with_reason)["released"] is True
    assert set(_slots_and_fence(with_reason)["pools"].values()) == {0}
    assert _slots_and_fence(with_reason)["generation"] == 2
