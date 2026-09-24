"""The reconciler must recognise what the dispatcher actually creates.

These tests exist because of a real failure mode rather than a hypothetical one.
The reconciler decides that a task has no execution behind it by NOT FINDING one,
and the consequence of that conclusion is severe: it releases the slot and puts
the task back on the queue, so the scheduler starts a second agent. If the
listing code cannot see a running execution -- because the dispatcher stamped
`managed-by=swarm-scheduler` and the listing filtered on `managed-by=swarm`, or
because the identifiers were read from a label whose value had been sanitised
from `task_9f3a` to `task-9f3a` -- then every healthy task on that backend looks
abandoned, and the service whose entire purpose is preventing duplicate
execution becomes the thing that causes it.

So each test here feeds the backend objects shaped exactly as
`scheduler/dispatch.py` creates them, and asserts the reconciler sees them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeBackend, FakeFirestore, FakeTransactionRunner
from test_reconciler_safety import TENANT, build, running_execution, seed_running_task

from reconciler.backends import (
    GC_MANAGED_VALUES,
    CloudRunBackend,
    GkeBackend,
    is_gc_eligible,
    is_swarm_managed,
    managed_label_selector,
)
from reconciler.config import ReconcilerConfig
from reconciler.detect import normalise_executions, sanitised
from reconciler.logs import build_logger
from reconciler.model import ControlSnapshot, JobResourceView, TaskView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore
from swarm_common.models import utcnow
from swarm_common.states import TaskState

REGION = "us-central1"
PROJECT = "saga-agents-staging"
JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-eng-mock"

#: DERIVED, never spelled again in this file.
#:
#: Every namespace literal below used to read `swarm-<tenant>`, which was the
#: reconciler's own (wrong) prefix written out by hand -- so the suite agreed
#: with the bug it existed to catch. `GkeBackend` both FILTERS on this prefix
#: and SLICES it off to recover a tenant id, so a fixture that restates it
#: passes whatever the prefix says and proves nothing about whether the
#: reconciler and the scheduler are looking at the same namespaces.
#: `scripts/lib/check-contract-parity.sh` section 6 is what holds the prefix
#: itself to the scheduler's.
NS = ReconcilerConfig.namespace_prefix
TENANT_NS = f"{NS}{TENANT}"


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
# Objects shaped like the ones the dispatcher creates
# ---------------------------------------------------------------------------


def env_entries(**values: str) -> list[Any]:
    return [SimpleNamespace(name=k, value=v) for k, v in values.items()]


def run_execution(
    *,
    name: str = f"{JOB}/executions/x1",
    labels: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    running: int = 1,
    cancelled: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    completed: bool = False,
    reconciling: bool = False,
    age_seconds: int = 600,
) -> Any:
    """A `run_v2.Execution` as the dispatcher's `run_job` override produces it.

    `completed=True` is the terminal shape, and the field values are the ones a
    real `get_execution` returned for swarm-job-eng-claude-code-sq2l6 after it
    was cancelled: `running_count=0`, `cancelled_count=1`, `reconciling=False`
    and `completion_time` set.
    """
    return SimpleNamespace(
        name=name,
        labels=labels or {},
        create_time=utcnow() - timedelta(seconds=age_seconds),
        running_count=running,
        cancelled_count=cancelled,
        succeeded_count=succeeded,
        failed_count=failed,
        completion_time=utcnow() if completed else None,
        reconciling=reconciling,
        template=SimpleNamespace(
            containers=[SimpleNamespace(name="worker", env=env_entries(**(env or {})))]
        ),
    )


def run_job(*, name: str = JOB, labels: dict[str, str] | None = None, age_days: int = 30) -> Any:
    return SimpleNamespace(
        name=name,
        labels=labels or {},
        create_time=utcnow() - timedelta(days=age_days),
    )


class FakeJobsClient:
    def __init__(self, jobs: list[Any]) -> None:
        self.jobs = jobs
        self.deleted: list[str] = []

    def list_jobs(self, parent: str) -> list[Any]:
        return list(self.jobs)

    def get_job(self, name: str) -> Any:
        for job in self.jobs:
            if job.name == name:
                return job
        raise KeyError(name)

    def delete_job(self, name: str) -> Any:
        self.deleted.append(name)
        return SimpleNamespace(result=lambda timeout=None: SimpleNamespace(name=name))


class FakeExecutionsClient:
    def __init__(self, executions: dict[str, list[Any]]) -> None:
        self.executions = executions
        #: Every name `get_execution` was asked for, so a test can assert that
        #: `terminate()` re-read the execution rather than trusting the ack.
        self.read: list[str] = []

    def list_executions(self, parent: str) -> list[Any]:
        return list(self.executions.get(parent, []))

    def get_execution(self, name: str) -> Any:
        """One execution by name, as `terminate()` re-reads it.

        An unknown name raises `NotFound`, which is what the real client does
        and is itself a confirmed stop.
        """
        from google.api_core import exceptions as gapi_exceptions

        self.read.append(name)
        for items in self.executions.values():
            for execution in items:
                if str(getattr(execution, "name", "")) == name:
                    return execution
        raise gapi_exceptions.NotFound(name)


def k8s_job(
    *,
    name: str = "swarm-9f3a-3",
    namespace: str = TENANT_NS,
    labels: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    active: int = 1,
) -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=namespace,
            labels=labels or {},
            creation_timestamp=utcnow() - timedelta(seconds=600),
        ),
        status=SimpleNamespace(active=active, succeeded=0, failed=0),
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[SimpleNamespace(name="worker", env=env_entries(**(env or {})))]
                )
            )
        ),
    )


class FakeBatchApi:
    def __init__(self, jobs: list[Any]) -> None:
        self.jobs = jobs
        self.selectors: list[str] = []

    def list_job_for_all_namespaces(self, label_selector: str) -> Any:
        self.selectors.append(label_selector)
        return SimpleNamespace(items=list(self.jobs))

    def list_namespaced_job(self, namespace: str) -> Any:
        return SimpleNamespace(
            items=[j for j in self.jobs if j.metadata.namespace == namespace]
        )


class FakeCoreApi:
    def __init__(self, namespaces: list[Any]) -> None:
        self.namespaces = namespaces
        self.deleted: list[str] = []

    def list_namespace(self, label_selector: str) -> Any:
        return SimpleNamespace(items=list(self.namespaces))

    def read_namespace(self, name: str) -> Any:
        for ns in self.namespaces:
            if ns.metadata.name == name:
                return ns
        raise KeyError(name)

    def delete_namespace(self, name: str) -> None:
        self.deleted.append(name)


def k8s_namespace(name: str, labels: dict[str, str], age_days: int = 30) -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=labels,
            creation_timestamp=utcnow() - timedelta(days=age_days),
        )
    )


# ---------------------------------------------------------------------------
# Managed-marker family
# ---------------------------------------------------------------------------


def test_every_marker_this_platform_stamps_is_visible():
    """Reads must not go blind because a component picked a different suffix."""
    for marker in ("swarm", "swarm-scheduler", "swarm-api", "swarm-terraform"):
        assert is_swarm_managed({"managed-by": marker}), marker
    assert not is_swarm_managed({"managed-by": "other-team-platform"})
    assert not is_swarm_managed({})


def test_terraform_owned_resources_are_visible_but_never_deleted():
    assert is_swarm_managed({"managed-by": "swarm-terraform"})
    assert not is_gc_eligible({"managed-by": "swarm-terraform"})
    assert is_gc_eligible({"managed-by": "swarm-scheduler"})
    assert "swarm-terraform" not in GC_MANAGED_VALUES


def test_the_kubernetes_selector_is_set_based():
    selector = managed_label_selector()
    assert selector.startswith("managed-by in (")
    assert "swarm-scheduler" in selector


# ---------------------------------------------------------------------------
# Cloud Run
# ---------------------------------------------------------------------------


def test_cloud_run_sees_executions_of_a_dispatcher_labelled_job():
    """The dispatcher stamps `managed-by: swarm-scheduler`. It must be visible."""
    job = run_job(labels={"managed-by": "swarm-scheduler", "swarm-tenant": TENANT})
    execution = run_execution(
        labels={"managed-by": "swarm-scheduler"},
        env={
            "TASK_ID": "task_9f3a",
            "ATTEMPT_ID": "att_7b21",
            "TENANT_ID": TENANT,
            "GENERATION": "3",
        },
    )
    backend = CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([job]),
        executions_client=FakeExecutionsClient({JOB: [execution]}),
    )

    views = backend.list_executions()
    assert len(views) == 1
    view = views[0]
    assert view.is_active
    # Verbatim, from the environment -- not the sanitised label.
    assert view.task_id == "task_9f3a"
    assert view.attempt_id == "att_7b21"
    assert view.tenant_id == TENANT
    assert view.generation == 3


def test_cloud_run_ignores_another_teams_job_in_this_shared_project():
    other = run_job(
        name=f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-lookalike",
        labels={"managed-by": "data-platform"},
    )
    backend = CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([other]),
        executions_client=FakeExecutionsClient({other.name: [run_execution()]}),
    )
    assert backend.list_executions() == []


def test_cloud_run_gc_eligibility_follows_the_marker():
    scheduler_job = run_job(labels={"managed-by": "swarm-scheduler", "swarm-tenant": "finance"})
    tf_job = run_job(
        name=f"projects/{PROJECT}/locations/{REGION}/jobs/swarm-tf-owned",
        labels={"managed-by": "swarm-terraform", "swarm-tenant": "finance"},
    )
    backend = CloudRunBackend(
        PROJECT,
        REGION,
        jobs_client=FakeJobsClient([scheduler_job, tf_job]),
        executions_client=FakeExecutionsClient({}),
    )
    by_name = {r.name: r for r in backend.list_job_resources()}
    assert by_name[JOB].managed is True
    assert by_name[tf_job.name].managed is False
    assert by_name[JOB].tenant_id == "finance"


def test_cloud_run_refuses_to_delete_a_terraform_owned_job():
    tf_job = run_job(labels={"managed-by": "swarm-terraform"})
    jobs = FakeJobsClient([tf_job])
    backend = CloudRunBackend(
        PROJECT, REGION, jobs_client=jobs, executions_client=FakeExecutionsClient({})
    )
    view = JobResourceView(
        name=JOB,
        tenant_id="finance",
        runner_profile="mock",
        created_at=utcnow() - timedelta(days=30),
        last_execution_at=utcnow() - timedelta(days=30),
        managed=True,          # a stale view; the re-read is what must catch it
        active_executions=0,
    )
    with pytest.raises(PermissionError):
        backend.delete_job_resource(view)
    assert jobs.deleted == []


# ---------------------------------------------------------------------------
# GKE
# ---------------------------------------------------------------------------


def test_gke_reads_identifiers_from_the_pod_environment():
    """The dispatcher puts no attempt id or generation in a k8s label at all.

    Both live only in the worker container's environment, so a reconciler that
    read labels alone would match no lease and call every browser job an orphan.
    """
    job = k8s_job(
        labels={
            "managed-by": "swarm-scheduler",
            "swarm-tenant": TENANT,
            "swarm-task": "task-9f3a",      # sanitised: underscores are illegal
        },
        env={
            "TASK_ID": "task_9f3a",
            "ATTEMPT_ID": "att_7b21",
            "TENANT_ID": TENANT,
            "GENERATION": "4",
        },
    )
    batch = FakeBatchApi([job])
    backend = GkeBackend(batch_api=batch, core_api=FakeCoreApi([]))

    views = backend.list_executions()
    assert len(views) == 1
    assert views[0].task_id == "task_9f3a"
    assert views[0].attempt_id == "att_7b21"
    assert views[0].generation == 4
    assert views[0].namespace == TENANT_NS
    assert batch.selectors == [managed_label_selector()]


def test_gke_skips_a_job_in_a_namespace_that_is_not_ours():
    job = k8s_job(namespace="finance-prod", labels={"managed-by": "swarm-scheduler"})
    backend = GkeBackend(batch_api=FakeBatchApi([job]), core_api=FakeCoreApi([]))
    assert backend.list_executions() == []


def test_gke_skips_a_job_in_a_namespace_that_only_looks_like_ours():
    """`swarm-eng` is NOT a tenant namespace, and it is not a hypothetical name.

    It is what `kubernetes/render.py` and `scripts/register-tenant.sh` both
    created while the scheduler dispatched into `swarm-tenant-eng` -- the
    2026-09-23 outage. With the reconciler's prefix left at `swarm-` this
    listing accepted both spellings and then recovered the tenant id by
    slicing, which turns `swarm-tenant-eng` into the tenant `tenant-eng`: a
    finding filed against a tenant that exists nowhere.

    THE MUTATION THIS CATCHES: set `ReconcilerConfig.namespace_prefix` back to
    `"swarm-"` and this job is listed instead of skipped.
    """
    job = k8s_job(
        namespace=f"swarm-{TENANT}",  # namespace-prefix-exempt: the wrong spelling, on purpose
        labels={"managed-by": "swarm-scheduler", "swarm-tenant": TENANT},
        env={"TASK_ID": "task_9f3a", "TENANT_ID": TENANT, "GENERATION": "4"},
    )
    backend = GkeBackend(batch_api=FakeBatchApi([job]), core_api=FakeCoreApi([]))
    assert backend.list_executions() == []


def test_a_tenant_namespace_is_collectable_whichever_marker_created_it():
    """`register-tenant.sh` labels namespaces `swarm-terraform` and relabels on
    every run, yet no terraform state holds a namespace -- there is no
    kubernetes provider in `terraform/`. So the namespace rule keys on the pair
    (recognised marker, tenant label) instead, which nothing outside this
    platform sets."""
    runtime = k8s_namespace(f"{NS}finance", {"managed-by": "swarm", "swarm-tenant": "finance"})
    scripted = k8s_namespace(TENANT_NS, {"managed-by": "swarm-terraform", "swarm-tenant": TENANT})
    backend = GkeBackend(
        batch_api=FakeBatchApi([]), core_api=FakeCoreApi([runtime, scripted])
    )
    by_name = {r.name: r for r in backend.list_job_resources()}
    assert by_name[f"{NS}finance"].managed is True
    assert by_name[TENANT_NS].managed is True


def test_a_namespace_without_a_tenant_label_is_never_collectable():
    """A shared project holds other teams' namespaces. A marker alone is not
    enough of a claim to delete one."""
    stray = k8s_namespace(f"{NS}shared-tools", {"managed-by": "swarm-terraform"})
    backend = GkeBackend(batch_api=FakeBatchApi([]), core_api=FakeCoreApi([stray]))
    resources = backend.list_job_resources()
    assert resources[0].managed is False
    # With no `swarm-tenant` label the tenant id is recovered by slicing the
    # prefix off the namespace name, which is why the prefix has to be the
    # scheduler's exactly. THE MUTATION THIS CATCHES: with
    # `namespace_prefix = "swarm-"` this reads `tenant-shared-tools`, and every
    # finding the reconciler files about an unlabelled namespace is attributed
    # to a tenant that does not exist.
    assert resources[0].tenant_id == "shared-tools"

    with pytest.raises(PermissionError):
        backend.delete_job_resource(replace(resources[0], managed=True))
    assert backend._core.deleted == []


# ---------------------------------------------------------------------------
# A cancel Cloud Run would not acknowledge, over an execution it reports as over
#
# End to end through the real `CloudRunBackend`, because the bool that decides
# whether the slot comes back is produced there and `FakeBackend` returns a
# scripted one. Wedged live on 2026-09-22: task_b5dc2568713a40158851 held its
# lease from 04:00:22 (the failed cancel) until 04:05:16, when the following
# pass reached it by another route. The route out must not be incidental.
# ---------------------------------------------------------------------------


def _wedged_clients(*, cancel_error: Exception, current: Any):
    """A dispatcher-shaped job whose one execution refuses to be cancelled."""
    job = run_job(labels={"managed-by": "swarm-scheduler", "swarm-tenant-id": TENANT})
    listed = run_execution(
        labels={"managed-by": "swarm-scheduler"},
        env={
            "TASK_ID": "task_1",
            "ATTEMPT_ID": "att_1",
            "TENANT_ID": TENANT,
            "GENERATION": "3",
        },
    )

    class _Client(FakeExecutionsClient):
        def __init__(self) -> None:
            super().__init__({JOB: [listed]})
            self.cancelled: list[str] = []

        def cancel_execution(self, request):
            self.cancelled.append(request.name)
            raise cancel_error

        def get_execution(self, name: str):
            # The cancel has landed by the time anything re-reads, so the GET
            # returns the post-cancel state rather than the listed one.
            self.read.append(name)
            return current

    return FakeJobsClient([job]), _Client()


def test_an_unacknowledged_cancel_releases_the_slot_once_the_execution_reads_as_over(db):
    """The sq2l6 shape: LRO error code 2, execution already terminal."""
    from google.api_core import exceptions as gapi_exceptions

    seed_running_task(db, pool_active=2)
    jobs, executions = _wedged_clients(
        cancel_error=gapi_exceptions.Unknown("Unspecified error."),
        current=run_execution(running=0, cancelled=1, completed=True),
    )
    backend = CloudRunBackend(
        PROJECT, REGION, job_name_prefix="swarm-",
        jobs_client=jobs, executions_client=executions, logger=build_logger(),
    )
    report = build_gke_reconciler(db, config_for_cloud_run(), backend).run_once()

    outcome = report.outcomes[0]
    assert outcome.kind == "dead_worker"
    assert executions.cancelled == [f"{JOB}/executions/x1"]
    assert outcome.terminated is True, outcome.as_dict()
    assert outcome.released is True, outcome.as_dict()
    # The generation was fenced first, the slot came back after, and the task is
    # queueable again -- all in the pass that found it.
    assert db.doc("tasks/task_1")["current_generation"] == 4
    assert db.doc("pools/global")["active"] == 1
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert db.doc("tasks/task_1")["state"] == TaskState.READY.value


def test_an_unacknowledged_cancel_holds_the_slot_while_the_execution_still_runs(db):
    """Genuinely unknown: Cloud Run still reports the execution running.

    A stuck slot is recoverable; two agents on one repository are not.
    """
    from google.api_core import exceptions as gapi_exceptions

    seed_running_task(db, pool_active=2)
    jobs, executions = _wedged_clients(
        cancel_error=gapi_exceptions.Unknown("Unspecified error."),
        current=run_execution(running=1),
    )
    backend = CloudRunBackend(
        PROJECT, REGION, job_name_prefix="swarm-",
        jobs_client=jobs, executions_client=executions, logger=build_logger(),
    )
    report = build_gke_reconciler(db, config_for_cloud_run(), backend).run_once()

    outcome = report.outcomes[0]
    assert outcome.terminated is False
    assert outcome.released is False
    assert outcome.skipped and "termination_failed" in outcome.skipped
    assert "Unspecified error" in outcome.skipped, "the real cause must survive"
    assert db.doc("pools/global")["active"] == 2
    assert db.doc("leases/lease_1")["released_at"] is None
    # The generation brake was still applied: it does not need the backend.
    assert db.doc("tasks/task_1")["current_generation"] == 4


# ---------------------------------------------------------------------------
# Identifier normalisation
# ---------------------------------------------------------------------------


def test_a_sanitised_label_resolves_back_to_the_real_document_id():
    snapshot = ControlSnapshot()
    snapshot.tasks["task_9f3a"] = TaskView(
        task_id="task_9f3a",
        tenant_id=TENANT,
        state=TaskState.RUNNING,
        generation=2,
        lease_id="lease_1",
        runner_profile="browser",
        resource_class="browser",
        updated_at=utcnow(),
    )
    execution = replace(running_execution(), task_id="task-9f3a", attempt_id="att-7b21")
    resolved = normalise_executions(snapshot, [execution])
    assert resolved[0].task_id == "task_9f3a"
    assert sanitised("task_9f3a") == "task-9f3a"


def test_a_label_identified_execution_is_not_mistaken_for_an_orphan(db, config):
    """End to end: only sanitised labels available, task healthy, hands off."""
    seed_running_task(db, silent_seconds=5, generation=3)
    execution = replace(
        running_execution(age_seconds=300),
        task_id="task-1",              # what a label would have carried
        attempt_id="att-1",
    )
    backend = FakeBackend(executions=[execution], journal=db.writes)
    report = build(db, config, backend).run_once()

    assert report.findings == 0, [o.as_dict() for o in report.outcomes]
    assert backend.terminated == []
    assert db.doc("leases/lease_1")["released_at"] is None


# ---------------------------------------------------------------------------
# Namespace garbage collection
# ---------------------------------------------------------------------------


def gke_config(**overrides: Any) -> ReconcilerConfig:
    base = dict(
        project_id=PROJECT,
        region=REGION,
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        empty_namespace_ttl_seconds=3600,
    )
    base.update(overrides)
    return ReconcilerConfig(**base)


def build_gke_reconciler(db: FakeFirestore, cfg: ReconcilerConfig, backend: Any):
    logger = build_logger(stream=__import__("io").StringIO())
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=[backend], config=cfg, logger=logger)


def idle_namespace(tenant: str) -> JobResourceView:
    old = utcnow() - timedelta(days=30)
    return JobResourceView(
        name=f"swarm-{tenant}",
        tenant_id=tenant,
        runner_profile=None,
        created_at=old,
        last_execution_at=old,
        managed=True,
        active_executions=0,
    )


def test_a_registered_tenants_namespace_survives_being_idle(db):
    """Nothing recreates a namespace, its KSA or its workload-identity binding.

    Collecting it because the tenant had a quiet day would turn their next
    submission into a dispatch failure, so registration alone protects it.
    """
    db.seed("tenants/finance", {"tenant_id": "finance", "kind": "group", "enabled": True})
    backend = FakeBackend(
        "GKE_AUTOPILOT", executions=[], resources=[idle_namespace("finance")], journal=db.writes
    )
    build_gke_reconciler(db, gke_config(), backend).run_once()
    assert backend.deleted == []


def test_a_deregistered_tenants_empty_namespace_is_collected(db):
    db.seed("tenants/eng", {"tenant_id": "eng", "kind": "group", "enabled": True})
    backend = FakeBackend(
        "GKE_AUTOPILOT", executions=[], resources=[idle_namespace("finance")], journal=db.writes
    )
    report = build_gke_reconciler(db, gke_config(), backend).run_once()
    assert backend.deleted == ["swarm-finance"]
    assert any(o.deleted == "swarm-finance" for o in report.outcomes)


def test_cloud_run_job_resources_are_collected_even_for_a_registered_tenant(db):
    """`ensure_job` recreates a Job on the next dispatch, so idling is enough."""
    db.seed("tenants/finance", {"tenant_id": "finance", "kind": "group", "enabled": True})
    old = utcnow() - timedelta(days=30)
    resource = JobResourceView(
        name="swarm-finance-mock",
        tenant_id="finance",
        runner_profile="mock",
        created_at=old,
        last_execution_at=old,
        managed=True,
        active_executions=0,
    )
    backend = FakeBackend(executions=[], resources=[resource], journal=db.writes)
    build_gke_reconciler(db, config_for_cloud_run(), backend).run_once()
    assert backend.deleted == ["swarm-finance-mock"]


def config_for_cloud_run() -> ReconcilerConfig:
    return ReconcilerConfig(
        project_id=PROJECT,
        region=REGION,
        firestore_database="swarm",
        heartbeat_grace_seconds=90,
        missing_execution_grace_seconds=300,
        orphan_execution_grace_seconds=120,
        enable_gke=False,
    )
