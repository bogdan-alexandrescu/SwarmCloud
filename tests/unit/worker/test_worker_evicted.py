"""A GKE pod kubelet evicts is named as evicted, and a disk eviction is retried once (#893).

WHAT WAS WRONG. On 2026-10-08 task_22e763906edd4edcb630's pod was evicted three
times -- `Usage of EmptyDir volume "tmp" exceeds the limit "2Gi"` -- and the
task ended FAILED reading "reconciled: task is RUNNING but no backend execution
exists 539s after dispatch". Two defects:

  * `GkeBackend._job_view` never set `ExecutionView.ended`, so a Failed Job
    was listed and counted for nothing, and `detect_missing_executions` called
    the attempt's execution absent;
  * nothing read the pod's own `status.reason: Evicted`, so every retry ran
    the same step on the same disk until `max_attempts` was spent.

WHAT EACH TEST PINS:

  * a Failed (or Complete) Job is `ended`, a running one is not;
  * the pod's eviction message reaches `Termination.eviction`, with or without
    a terminated container beside it, from the `pods list` already granted;
  * the eviction is the attempt's and the task's error, as
    `evicted: EmptyDir volume "tmp" exceeds 2Gi`, never "no backend execution";
  * the FIRST disk eviction requeues and is counted in
    `metadata.disk_evictions`; the SECOND fails the task with the eviction as
    its cause, attempts left or not; spent attempts fail it on the first;
  * a node-pressure eviction is named but retried as any lost worker is;
  * the counter key is the one swarm-api reserves.

Against the real `Reconciler` and `ControlStore` over the in-memory Firestore;
only the backend is scripted, reporting what kubelet and the Job controller do.
"""

from __future__ import annotations

import io
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner

from reconciler.config import ReconcilerConfig
from reconciler.detect import eviction_cause
from reconciler.logs import build_logger as build_reconciler_logger
from reconciler.model import (
    DISK_EVICTION_RETRIES,
    DISK_EVICTIONS_KEY,
    ExecutionPhase,
    ExecutionView,
    Termination,
    count_disk_eviction,
    disk_evictions_used,
)
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import EndCause, pool_names_for, utcnow
from swarm_common.states import EventType, TaskState

from worker_seeds import TENANT, seed_tenant

GKE = "GKE_AUTOPILOT"
NAMESPACE = "swarm-tenant-eng"
GENERATION = 1
TASK, LEASE, ATTEMPT = "task_22e763906edd4edcb630", "lease_ev1", "att_ev1"
JOB_NAME = "swarm-22e763906edd4edcb630-1"
POOLS = pool_names_for(
    tenant_id=TENANT,
    provider="anthropic",
    resource_class="standard",
    runner_profile="claude-code",
    backend=GKE,
)

#: kubelet's own words, as the GKE events read on 2026-10-08.
TMP_EVICTION = 'Usage of EmptyDir volume "tmp" exceeds the limit "2Gi". '
NODE_PRESSURE = (
    "The node was low on resource: ephemeral-storage. Threshold quantity: 10Gi, "
    "available: 9Gi. "
)


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


def seed_a_running_attempt(
    db: FakeFirestore,
    *,
    attempt_count: int = 1,
    max_attempts: int = 3,
    metadata: dict[str, Any] | None = None,
    heartbeat_seconds_ago: float = 60,
) -> None:
    """A RUNNING claude-code attempt on GKE whose worker heartbeated, then vanished."""
    now = utcnow()
    admitted = now - timedelta(minutes=9)
    db.seed(
        f"tasks/{TASK}",
        {
            "id": TASK, "tenant_id": TENANT, "state": TaskState.RUNNING.value,
            "runner_profile": "claude-code", "resource_class": "standard",
            "current_generation": GENERATION, "current_lease_id": LEASE,
            "attempt_count": attempt_count, "max_attempts": max_attempts,
            "updated_at": admitted, "cancel_requested": False,
            "last_error": None, "metadata": dict(metadata or {}),
        },
    )
    db.seed(
        f"leases/{LEASE}",
        {
            "lease_id": LEASE, "task_id": TASK, "attempt_id": ATTEMPT,
            "tenant_id": TENANT, "generation": GENERATION,
            "pools": POOLS, "units": 1, "state": TaskState.RUNNING.value,
            "created_at": admitted,
            "dispatch_deadline": admitted + timedelta(seconds=480),
            "expires_at": now + timedelta(seconds=60),
            "heartbeat_at": now - timedelta(seconds=heartbeat_seconds_ago),
            "released_at": None,
        },
    )
    db.seed(
        f"attempts/{ATTEMPT}",
        {
            "attempt_id": ATTEMPT, "task_id": TASK, "tenant_id": TENANT,
            "generation": GENERATION, "lease_id": LEASE, "backend": GKE,
            "created_at": admitted, "execution_name": f"{NAMESPACE}/{JOB_NAME}",
        },
    )
    for pool in POOLS:
        db.seed(f"pools/{pool}", {"name": pool, "hard_limit": 10, "active": 1,
                                  "enabled": True, "updated_at": now})
    seed_tenant(db)


def failed_job(*, ended_seconds_ago: float = 60) -> ExecutionView:
    """The Job, as `GkeBackend._job_view` lists it once it is Failed."""
    return ExecutionView(
        name=JOB_NAME,
        backend=GKE,
        phase=ExecutionPhase.FAILED,
        created_at=utcnow() - timedelta(minutes=9),
        task_id=TASK,
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        generation=GENERATION,
        namespace=NAMESPACE,
        completed_at=utcnow() - timedelta(seconds=ended_seconds_ago),
        ended=True,
    )


def evicted(message: str) -> Termination:
    """What `GkeBackend.termination` reads off an evicted pod."""
    return Termination(
        exit_code=137,
        message=None,
        detail="container worker: ContainerStatusUnknown",
        eviction=message.strip(),
    )


def reconcile(
    db: FakeFirestore, config: ReconcilerConfig, termination: Any
) -> tuple[Any, FakeBackend]:
    logger = build_reconciler_logger(stream=io.StringIO())
    backend = FakeBackend(
        name=GKE,
        executions=[failed_job()],
        journal=db.writes,
        terminations={JOB_NAME: termination},
    )
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(store=store, backends=[backend], config=config, logger=logger).run_once()
    return report, backend


# ---------------------------------------------------------------------------
# The reconciler: named, retried once, then failed
# ---------------------------------------------------------------------------


def test_the_first_disk_eviction_is_named_and_retried_once(db, reconciler_config):
    seed_a_running_attempt(db, attempt_count=1, max_attempts=3)

    report, backend = reconcile(db, reconciler_config, evicted(TMP_EVICTION))

    assert backend.termination_reads == [JOB_NAME], "the pod was never read"
    assert [o.kind for o in report.outcomes] == ["worker_evicted"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.READY.value, task
    assert task["last_error"].startswith(
        'reconciled: evicted: EmptyDir volume "tmp" exceeds 2Gi'
    ), task["last_error"]
    assert "no backend execution" not in task["last_error"]
    assert "retrying once" in task["last_error"]
    assert task["metadata"][DISK_EVICTIONS_KEY] == 1
    assert task["current_lease_id"] is None
    assert db.doc(f"leases/{LEASE}")["released_at"] is not None
    # The attempt carries the eviction too, not a silence.
    attempt = db.doc(f"attempts/{ATTEMPT}")
    assert 'evicted: EmptyDir volume "tmp" exceeds 2Gi' in str(attempt.get("error")), attempt


def test_the_second_disk_eviction_fails_the_task_with_attempts_left(db, reconciler_config):
    """What task_22e763906edd4edcb630 needed: two attempts, not three identical ones."""
    seed_a_running_attempt(
        db, attempt_count=2, max_attempts=3, metadata={DISK_EVICTIONS_KEY: 1}
    )

    report, _ = reconcile(db, reconciler_config, evicted(TMP_EVICTION))

    assert [o.kind for o in report.outcomes] == ["worker_evicted"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value, task
    assert task["attempt_count"] < task["max_attempts"]
    assert task["end_cause"] == EndCause.LOST_WORKER.value
    assert 'evicted: EmptyDir volume "tmp" exceeds 2Gi' in task["last_error"]
    assert "not retried again" in task["last_error"]
    assert task["metadata"][DISK_EVICTIONS_KEY] == 2
    assert EventType.READY.value not in db.event_types(TASK), db.event_types(TASK)


def test_spent_attempts_fail_on_the_first_disk_eviction(db, reconciler_config):
    """The frozen `retries_exhausted` still applies: the rule only ever fails sooner."""
    seed_a_running_attempt(db, attempt_count=3, max_attempts=3)

    reconcile(db, reconciler_config, evicted(TMP_EVICTION))

    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.FAILED.value, task
    assert "no attempts left" in task["last_error"]


def test_a_node_pressure_eviction_is_named_but_not_counted_as_the_steps_size(
    db, reconciler_config
):
    seed_a_running_attempt(db, attempt_count=1, metadata={DISK_EVICTIONS_KEY: 1})

    report, _ = reconcile(db, reconciler_config, evicted(NODE_PRESSURE))

    assert [o.kind for o in report.outcomes] == ["worker_evicted"], report.as_dict()
    task = db.doc(f"tasks/{TASK}")
    assert task["state"] == TaskState.READY.value, task
    assert task["last_error"].startswith(
        "reconciled: evicted: The node was low on resource: ephemeral-storage"
    ), task["last_error"]
    assert task["metadata"][DISK_EVICTIONS_KEY] == 1, "a node's pressure is not this step's"


def test_a_failed_job_with_no_eviction_is_a_lost_worker_not_a_missing_execution(
    db, reconciler_config
):
    """`ended` on a Failed Job: the stale-lease rule's positive proof, never an absence."""
    seed_a_running_attempt(db, heartbeat_seconds_ago=200)

    report, _ = reconcile(
        db, reconciler_config, Termination(exit_code=137, detail="container worker: Error")
    )

    kinds = [o.kind for o in report.outcomes]
    assert "missing_execution" not in kinds, report.as_dict()
    assert kinds == ["stale_lease"], report.as_dict()
    assert "its execution has ended (failed)" in db.doc(f"tasks/{TASK}")["last_error"]


# ---------------------------------------------------------------------------
# The rule, pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "cause", "disk_limit"),
    [
        (TMP_EVICTION, 'evicted: EmptyDir volume "tmp" exceeds 2Gi', True),
        (
            'Usage of EmptyDir volume "workspace" exceeds the limit "4Gi". ',
            'evicted: EmptyDir volume "workspace" exceeds 4Gi',
            True,
        ),
        (
            "Pod ephemeral local storage usage exceeds the total limit of containers 10Gi. ",
            "evicted: the pod's ephemeral storage exceeds 10Gi",
            True,
        ),
        (
            'Container worker exceeded its local ephemeral storage limit "10Gi". ',
            "evicted: container worker exceeds its ephemeral storage limit 10Gi",
            True,
        ),
        (NODE_PRESSURE.strip(), "evicted: " + " ".join(NODE_PRESSURE.split()), False),
    ],
)
def test_the_cause_names_the_volume_and_its_limit(message, cause, disk_limit):
    assert eviction_cause(message) == (cause, disk_limit)


def test_the_retry_rule_retries_once_and_only_ever_fails_sooner():
    assert DISK_EVICTION_RETRIES == 1
    first = count_disk_eviction(1, 3, 0)
    assert (first.evictions, first.exhausted) == (1, False)
    second = count_disk_eviction(2, 3, 1)
    assert (second.evictions, second.exhausted) == (2, True)
    assert count_disk_eviction(3, 3, 0).exhausted, "spent attempts still end the task"


@pytest.mark.parametrize("value", [float("inf"), "3", True, -1, 2**70, None, 1.0])
def test_an_unreadable_counter_reads_as_none_used(value):
    assert disk_evictions_used({DISK_EVICTIONS_KEY: value}) == 0


def test_the_counter_is_the_key_swarm_api_reserves():
    from swarm_api.validation import DISK_EVICTIONS_METADATA_KEY, RESERVED_METADATA_KEYS

    assert DISK_EVICTIONS_KEY == DISK_EVICTIONS_METADATA_KEY
    assert DISK_EVICTIONS_METADATA_KEY in RESERVED_METADATA_KEYS


# ---------------------------------------------------------------------------
# The GKE backend: `ended` from the Job, the eviction from the pod
# ---------------------------------------------------------------------------


def _job(conditions: list[tuple[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=JOB_NAME,
            namespace=NAMESPACE,
            labels={"managed-by": "swarm-scheduler", "swarm-task": TASK},
            creation_timestamp=utcnow() - timedelta(minutes=9),
        ),
        status=SimpleNamespace(
            completion_time=None,
            conditions=[
                SimpleNamespace(type=kind, status=state, last_transition_time=utcnow())
                for kind, state in conditions
            ],
        ),
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[]))),
    )


@pytest.mark.parametrize(
    ("conditions", "ended"),
    [
        ([("Failed", "True")], True),
        ([("Complete", "True")], True),
        ([("FailureTarget", "True")], False),
        ([("Failed", "False")], False),
        ([], False),
    ],
)
def test_a_jobs_ended_comes_from_its_complete_or_failed_condition(conditions, ended):
    from reconciler.backends import GkeBackend

    backend = GkeBackend(namespace_prefix="swarm-tenant-", batch_api=object(), core_api=object())
    view = backend._job_view(_job(conditions), NAMESPACE)

    assert view is not None
    assert view.ended is ended


class _CoreApi:
    def __init__(self, pods: list[Any]) -> None:
        self.pods = pods

    def list_namespaced_pod(self, *, namespace: str, label_selector: str) -> Any:
        return SimpleNamespace(items=list(self.pods))


def _evicted_pod(*, with_container: bool) -> SimpleNamespace:
    statuses = (
        [
            SimpleNamespace(
                name="worker",
                state=SimpleNamespace(
                    terminated=SimpleNamespace(
                        exit_code=137, message=None, reason="ContainerStatusUnknown"
                    )
                ),
                last_state=SimpleNamespace(terminated=None),
            )
        ]
        if with_container
        else []
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(name=f"{JOB_NAME}-abcde", namespace=NAMESPACE),
        status=SimpleNamespace(
            reason="Evicted", message=TMP_EVICTION, container_statuses=statuses
        ),
    )


@pytest.mark.parametrize("with_container", [True, False])
def test_gke_reads_the_eviction_off_the_pod(with_container):
    from reconciler.backends import GkeBackend

    backend = GkeBackend(
        namespace_prefix="swarm-tenant-",
        batch_api=object(),
        core_api=_CoreApi([_evicted_pod(with_container=with_container)]),
    )

    found = backend.termination(failed_job())

    assert found is not None
    assert found.eviction == TMP_EVICTION.strip()
    assert found.exit_code == (137 if with_container else None)


def test_a_pod_that_was_not_evicted_carries_no_eviction():
    from reconciler.backends import GkeBackend

    pod = _evicted_pod(with_container=True)
    pod.status.reason, pod.status.message = None, None
    backend = GkeBackend(
        namespace_prefix="swarm-tenant-", batch_api=object(), core_api=_CoreApi([pod])
    )

    found = backend.termination(failed_job())

    assert found is not None and found.eviction is None
