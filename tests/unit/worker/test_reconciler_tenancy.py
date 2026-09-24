"""The reconciler must not be usable as a cross-tenant kill switch.

This service is the one component with authority over every tenant at once: it
invalidates generations, terminates executions, releases leases and rewrites task
state across all namespaces and all Cloud Run jobs. Until these tests existed it
authenticated its inputs by reading a field off the object it had just found --
`TASK_ID`, `TENANT_ID` and `GENERATION` came verbatim out of the worker
container's environment, which is written by whoever created the object.

A Job whose pod env claimed another tenant's task id and its current generation
would therefore have caused the reconciler to fence that tenant's live attempt,
release its slot and re-queue it. RBAC currently keeps tenants away from the Job
API, so it was one missing control away from exploitable rather than exploitable
-- which is exactly the kind of thing that stops being true quietly.

Two checks, in two places, because neither is sufficient alone:

* `backends.owning_tenant` takes the tenant from the object that ENCLOSES the
  container -- the namespace, or the per-tenant Cloud Run Job resource -- so a
  container cannot name a tenant it is not running as;
* `detect.scope_executions_to_their_tenant` then drops any claim on a TASK that
  belongs to a different tenant, so an execution can only ever cause repairs to
  its own tenant's work.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fakes import FakeFirestore, FakeTransactionRunner
from test_backend_identity import (
    JOB,
    PROJECT,
    REGION,
    TENANT_NS,
    FakeBatchApi,
    FakeCoreApi,
    FakeExecutionsClient,
    FakeJobsClient,
    k8s_job,
    run_execution,
    run_job,
)
from test_reconciler_safety import TENANT, seed_running_task

from reconciler.backends import CloudRunBackend, GkeBackend, owning_tenant
from reconciler.config import ReconcilerConfig
from reconciler.detect import scope_executions_to_their_tenant
from reconciler.logs import build_logger
from reconciler.model import ControlSnapshot, ExecutionPhase, ExecutionView, TaskView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import TaskState

VICTIM = "research"


@pytest.fixture
def config() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id=PROJECT,
        region=REGION,
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )


# ---------------------------------------------------------------------------
# owning_tenant: the enclosing object decides, not the container
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claimed,authority,expected",
    [
        # `owning_tenant` is deliberately tested on the STRING, not on a
        # namespace: its authority argument is whatever the caller sliced out
        # of an enclosing object, and `tenant-eng` is what that slice produced
        # for two days while the reconciler's prefix was `swarm-` instead of
        # `swarm-tenant-`. The prefix is fixed and asserted elsewhere now
        # (check-contract-parity.sh section 6); these cases stay because this
        # function must keep deferring to the authority it is handed even when
        # the authority is nonsense, which is the property that kept the
        # cross-tenant claim below from ever succeeding.
        ("eng", "eng", "eng"),
        ("eng", "tenant-eng", "eng"),
        # Underscores survive a Firestore id but not a label, so the comparison
        # has to be made on the sanitised form.
        ("u_alice", "u-alice", "u_alice"),
        # The attack: a container in tenant `eng`'s namespace claiming `research`.
        ("research", "eng", "eng"),
        ("research", "tenant-eng", "tenant-eng"),
        # A container that says nothing inherits the enclosing tenant.
        (None, "eng", "eng"),
        ("", "eng", "eng"),
        # Substring lookalikes must not pass: `eng` is not `engineering`.
        ("engineering", "eng", "eng"),
        ("eng", "engineering", "engineering"),
    ],
)
def test_the_enclosing_object_decides_which_tenant_an_execution_belongs_to(
    claimed, authority, expected
):
    assert owning_tenant(claimed, authority, resource="x") == expected


def test_with_no_enclosing_tenant_the_claim_is_returned_unchanged():
    """Nothing to check it against -- and `scope_executions_to_their_tenant`
    still refuses to let it act on another tenant's task."""
    assert owning_tenant("eng", None, resource="x") == "eng"
    assert owning_tenant("eng", "", resource="x") == "eng"


def test_a_gke_job_cannot_claim_a_tenant_its_namespace_does_not_name():
    """The namespace is per-tenant and a workload cannot move itself between
    namespaces, so it is the authority. The container environment is not."""
    batch = FakeBatchApi(
        [
            k8s_job(
                namespace=TENANT_NS,
                labels={"managed-by": "swarm-scheduler", "swarm-tenant": VICTIM},
                env={
                    "TASK_ID": "task_victim",
                    "ATTEMPT_ID": "att_victim",
                    "TENANT_ID": VICTIM,
                    "GENERATION": "9",
                },
            )
        ]
    )
    backend = GkeBackend(batch_api=batch, core_api=FakeCoreApi([]), logger=build_logger())

    view = backend.list_executions()[0]

    assert view.tenant_id == TENANT          # the namespace, not the env
    assert view.namespace == TENANT_NS
    # The task id is still taken verbatim -- it is the tenant check downstream
    # that refuses to act on it.
    assert view.task_id == "task_victim"


def test_a_cloud_run_execution_cannot_claim_a_tenant_its_job_resource_does_not_name():
    """Cloud Run pins the service account on the JOB resource, so the job's own
    tenant label is the identity its executions actually run as."""
    job = run_job(labels={"managed-by": "swarm-scheduler", "swarm-tenant": TENANT})
    execution = run_execution(
        labels={"managed-by": "swarm-scheduler"},
        env={"TASK_ID": "task_victim", "TENANT_ID": VICTIM, "GENERATION": "9"},
    )
    backend = CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([job]),
        executions_client=FakeExecutionsClient({JOB: [execution]}),
        logger=build_logger(),
    )

    view = backend.list_executions()[0]

    assert view.tenant_id == TENANT


# ---------------------------------------------------------------------------
# scope_executions_to_their_tenant: no acting on someone else's task
# ---------------------------------------------------------------------------


def _snapshot_with_victim_task() -> ControlSnapshot:
    snapshot = ControlSnapshot()
    snapshot.tasks["task_victim"] = TaskView(
        task_id="task_victim",
        tenant_id=VICTIM,
        state=TaskState.RUNNING,
        generation=9,
        lease_id="lease_victim",
        runner_profile="claude-code",
        resource_class="standard",
        updated_at=utcnow(),
    )
    return snapshot


def _execution(**overrides):
    base = dict(
        name="swarm-attacker-1",
        backend="GKE_AUTOPILOT",
        phase=ExecutionPhase.RUNNING,
        created_at=utcnow() - timedelta(seconds=600),
        task_id="task_victim",
        attempt_id="att_victim",
        tenant_id=TENANT,
        generation=9,
        namespace=TENANT_NS,
    )
    base.update(overrides)
    return ExecutionView(**base)


def test_an_execution_may_not_claim_a_task_belonging_to_another_tenant():
    scoped = scope_executions_to_their_tenant(
        _snapshot_with_victim_task(), [_execution()], build_logger()
    )
    assert scoped[0].task_id is None
    assert scoped[0].attempt_id is None
    assert scoped[0].generation is None
    # It is still the attacker's own execution; what it lost is the claim.
    assert scoped[0].tenant_id == TENANT
    assert scoped[0].name == "swarm-attacker-1"


def test_an_execution_claiming_its_own_tenants_task_is_untouched():
    scoped = scope_executions_to_their_tenant(
        _snapshot_with_victim_task(), [_execution(tenant_id=VICTIM)], build_logger()
    )
    assert scoped[0].task_id == "task_victim"
    assert scoped[0].generation == 9


def test_an_execution_naming_a_task_the_control_plane_does_not_know_is_untouched():
    """It is an orphan and the orphan rule handles it; rewriting it here would
    hide that."""
    scoped = scope_executions_to_their_tenant(
        _snapshot_with_victim_task(), [_execution(task_id="task_unknown")], build_logger()
    )
    assert scoped[0].task_id == "task_unknown"


def test_a_forged_execution_does_not_fence_the_victims_live_attempt(config):
    """The whole point, end to end through `run_once`: the victim's generation,
    lease and task state must all be exactly where they started."""
    db = FakeFirestore()
    seed_running_task(db, task_id="task_victim", lease_id="lease_victim",
                      attempt_id="att_victim", generation=9, silent_seconds=0)
    db.documents["tasks/task_victim"]["tenant_id"] = VICTIM
    db.documents["leases/lease_victim"]["tenant_id"] = VICTIM

    class _Backend:
        name = "CLOUD_RUN_JOB"

        def __init__(self) -> None:
            self.terminated: list[str] = []

        def list_executions(self):
            # Running in the ATTACKER's tenant, claiming the victim's task and
            # its current generation.
            return [_execution(backend="CLOUD_RUN_JOB", name="attacker-exec")]

        def terminate(self, execution):
            self.terminated.append(execution.name)
            return True

        def list_job_resources(self):
            return []

        def delete_job_resource(self, resource):
            return False

    backend = _Backend()
    store = ControlStore(db, logger=build_logger(), txn_runner=FakeTransactionRunner(db))
    report = Reconciler(
        store=store,
        backends=[backend],
        config=ReconcilerConfig(**{**config.__dict__, "enable_gc": False}),
        logger=build_logger(),
    ).run_once()

    task = db.doc("tasks/task_victim")
    lease = db.doc("leases/lease_victim")
    assert task["current_generation"] == 9, "the victim's generation was invalidated"
    assert task["state"] == TaskState.RUNNING.value
    assert lease["released_at"] is None, "the victim's slot was released"
    # The attacker's own execution is still an orphan and is still stopped.
    assert backend.terminated == ["attacker-exec"]
    assert all(o.task_id is None for o in report.outcomes)
