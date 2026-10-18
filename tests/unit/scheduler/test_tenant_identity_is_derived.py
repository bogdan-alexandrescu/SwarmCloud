"""The dispatcher derives a tenant's identity, namespace and GCS prefix (lane
ISO-SCHED, phase 0 of docs/design/tenant-settings-integrity.md; part of #1457).

A tenant's worker can write its own `tenants/<id>` document. Read from there,
`service_account` ran tenant A's next Cloud Run attempt as tenant B's account
(the scheduler holds `actAs` on every worker account), and `namespace` ran A's
next GKE pod in `swarm-tenant-B`, as B's KSA and so as B's account, because
the 2026-10-11 rule kept any stored value inside the prefix. Each is now
derived from the tenant id; a stored value that differs is logged and ignored.
UNCONDITIONAL: there is no switch.

MUTATION: return `tenant.service_account` from `assert_tenant_identity`, keep
a stored namespace inside the prefix in `verified_namespace`, put
`tenant.gcs_prefix` back into `worker_env`, or drop the identity check from
`_refresh_image`, and the named case fails.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import PROJECT, scheduler_settings  # noqa: E402

from swarm_common.models import Lease, Task, Tenant  # noqa: E402
from swarm_common.profiles import RUNNER_PROFILES  # noqa: E402
from swarm_common.states import TaskState  # noqa: E402

from scheduler import dispatch as dispatch_module  # noqa: E402
from scheduler.dispatch import (  # noqa: E402
    TENANT_RECORD_MISMATCH,
    CloudRunJobDispatcher,
    DispatchError,
    GkeJobDispatcher,
    GkeTarget,
    assert_tenant_identity,
    job_id_for,
    worker_env,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
ENG_ACCOUNT = f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com"
OTHER_ACCOUNT = f"swarm-agent-worker-other@{PROJECT}.iam.gserviceaccount.com"


@pytest.fixture(autouse=True)
def fresh_logs():
    """The mismatch logs are once per tenant per hour, process-wide."""
    for registry in (dispatch_module._record_mismatch_due,
                     dispatch_module._namespace_mismatch_due):
        registry.clear()
    yield
    for registry in (dispatch_module._record_mismatch_due,
                     dispatch_module._namespace_mismatch_due):
        registry.clear()


def forged(**fields) -> Tenant:
    """Tenant `eng`, its record rewritten to name tenant `other`'s."""
    base = dict(
        tenant_id="eng", kind="group", principal="eng@saga.xyz", created_at=NOW,
        service_account=OTHER_ACCOUNT,
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/other",
        namespace="swarm-tenant-other",
    )
    base.update(fields)
    return Tenant(**base)


def make_task(profile_name: str, tenant_id: str = "eng") -> Task:
    profile = RUNNER_PROFILES[profile_name]
    return Task(
        id="task_iso1", tenant_id=tenant_id, created_at=NOW, updated_at=NOW,
        state=TaskState.LEASED, runner_profile=profile_name,
        resource_class=profile.resource_class, input={}, submitted_by="alice@saga.xyz",
        provider=profile.provider, timeout_seconds=1800,
    )


def make_lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_iso1", task_id=task.id, attempt_id="att_iso1",
        tenant_id=task.tenant_id, generation=1, pools=["global"], units=1,
        state=TaskState.LEASED, created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=2),
    )


class Operation:
    metadata = type("Meta", (), {"name": "projects/p/locations/l/jobs/j/executions/e-1"})()

    def result(self, timeout=None):
        return self.metadata


class JobsClient:
    """Cloud Run's JobsClient over real `run_v2.Job` messages."""

    def __init__(self, jobs: dict | None = None) -> None:
        self.jobs = dict(jobs or {})
        self.calls: list[str] = []

    def get_job(self, request):
        from google.api_core import exceptions as gexc

        self.calls.append("get_job")
        if request.name not in self.jobs:
            raise gexc.NotFound(request.name)
        return self.jobs[request.name]

    def create_job(self, request):
        self.calls.append("create_job")
        self.jobs[f"{request.parent}/jobs/{request.job_id}"] = request.job
        return Operation()

    def update_job(self, request):
        self.calls.append("update_job")
        self.jobs[request.job.name] = request.job
        return Operation()

    def run_job(self, request):
        self.calls.append("run_job")
        return Operation()


class BatchApi:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    def create_namespaced_job(self, namespace, body):
        self.created.append((namespace, body))
        return {"metadata": {"name": body["metadata"]["name"]}}


def _mismatches(caplog, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "scheduler.dispatch" and getattr(r, "event", None) == event]


# -- the service account ------------------------------------------------------

def test_a_tenant_whose_record_names_another_tenants_account_dispatches_as_its_own(caplog):
    caplog.set_level("DEBUG", logger="scheduler.dispatch")
    settings = scheduler_settings()
    client = JobsClient()
    dispatcher = CloudRunJobDispatcher(settings, client=client)
    task = make_task("mock")

    dispatcher.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES["mock"],
                        tenant=forged())

    [job] = client.jobs.values()
    assert job.template.template.service_account == ENG_ACCOUNT, (
        "the Job ran as the account the tenant document named"
    )
    assert assert_tenant_identity(forged(), settings) == ENG_ACCOUNT
    [entry] = [r for r in _mismatches(caplog, TENANT_RECORD_MISMATCH)
               if r.field == "service_account"]
    assert entry.levelname == "ERROR"
    assert (entry.tenant_id, entry.stored, entry.derived) == ("eng", OTHER_ACCOUNT, ENG_ACCOUNT)


def test_a_scheduler_job_built_as_another_tenants_account_is_rebuilt_before_it_runs():
    """A Job created while the record was forged keeps its account until rebuilt."""
    settings = scheduler_settings()
    profile = RUNNER_PROFILES["mock"]
    seeded = CloudRunJobDispatcher(settings, client=object())._build_job(profile, forged())
    seeded.template.template.service_account = OTHER_ACCOUNT
    seeded.name = CloudRunJobDispatcher(settings, client=object()).job_name(
        job_id_for("eng", "mock"))
    assert seeded.labels["managed-by"] == "swarm-scheduler"
    client = JobsClient(jobs={seeded.name: seeded})
    task = make_task("mock")

    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=make_lease(task), profile=profile, tenant=forged())

    assert client.calls == ["get_job", "update_job", "run_job"]
    assert client.jobs[seeded.name].template.template.service_account == ENG_ACCOUNT


def test_a_job_already_running_as_the_derived_account_is_not_rewritten():
    settings = scheduler_settings()
    profile = RUNNER_PROFILES["mock"]
    builder = CloudRunJobDispatcher(settings, client=object())
    seeded = builder._build_job(profile, forged())
    seeded.name = builder.job_name(job_id_for("eng", "mock"))
    client = JobsClient(jobs={seeded.name: seeded})
    task = make_task("mock")

    CloudRunJobDispatcher(settings, client=client).dispatch(
        task=task, lease=make_lease(task), profile=profile, tenant=forged())

    assert client.calls == ["get_job", "run_job"]


def test_an_empty_record_dispatches_as_the_derived_account():
    """Equality with the derived account is what is checked, not non-emptiness."""
    assert assert_tenant_identity(forged(service_account=None), scheduler_settings()) == (
        ENG_ACCOUNT)


def test_a_tenant_that_can_have_no_account_is_refused_on_both_backends():
    """`swarm-agent-worker-` plus twelve characters is over GCP's 30: no such
    account can exist, and a Job naming none runs as the default compute one."""
    settings = scheduler_settings()
    long_id = "a" * 12
    tenant = forged(tenant_id=long_id, service_account=OTHER_ACCOUNT)
    task = make_task("mock", long_id)
    with pytest.raises(DispatchError) as cloud_run:
        CloudRunJobDispatcher(settings, client=JobsClient()).dispatch(
            task=task, lease=make_lease(task), profile=RUNNER_PROFILES["mock"], tenant=tenant)
    assert cloud_run.value.code == "tenant_identity_missing"

    browser = make_task("browser", long_id)
    with pytest.raises(DispatchError) as gke:
        GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                         batch_api=BatchApi()).dispatch(
            task=browser, lease=make_lease(browser), profile=RUNNER_PROFILES["browser"],
            tenant=tenant)
    assert gke.value.code == "tenant_identity_missing"


# -- the namespace ------------------------------------------------------------

def test_a_record_naming_another_tenants_namespace_renders_the_derived_one(caplog):
    caplog.set_level("DEBUG", logger="scheduler.dispatch")
    api = BatchApi()
    dispatcher = GkeJobDispatcher(scheduler_settings(),
                                  target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api)
    task = make_task("browser")

    dispatcher.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES["browser"],
                        tenant=forged())

    [(namespace, body)] = api.created
    assert namespace == "swarm-tenant-eng", "the pod ran in the namespace the record named"
    assert body["metadata"]["namespace"] == "swarm-tenant-eng"
    [entry] = _mismatches(caplog, "tenant_namespace_mismatch")
    assert entry.levelname == "ERROR"
    assert (entry.tenant_id, entry.stored, entry.derived, entry.used) == (
        "eng", "swarm-tenant-other", "swarm-tenant-eng", "swarm-tenant-eng")


# -- the GCS prefix -----------------------------------------------------------

def test_gcs_prefix_is_derived_from_the_tenant_id(caplog):
    caplog.set_level("DEBUG", logger="scheduler.dispatch")
    settings = scheduler_settings()
    task = make_task("mock")

    env = worker_env(task=task, lease=make_lease(task), tenant=forged(), settings=settings)

    assert env["GCS_PREFIX"] == f"gs://{settings.core.artifact_bucket}/tenants/eng"
    [entry] = [r for r in _mismatches(caplog, TENANT_RECORD_MISMATCH)
               if r.field == "gcs_prefix"]
    assert entry.stored.endswith("/tenants/other")


def test_a_record_equal_to_the_derived_values_logs_nothing(caplog):
    caplog.set_level("DEBUG", logger="scheduler.dispatch")
    settings = scheduler_settings()
    tenant = forged(service_account=ENG_ACCOUNT,
                    gcs_prefix=f"gs://{settings.core.artifact_bucket}/tenants/eng",
                    namespace="swarm-tenant-eng")
    task = make_task("mock")
    assert assert_tenant_identity(tenant, settings) == ENG_ACCOUNT
    worker_env(task=task, lease=make_lease(task), tenant=tenant, settings=settings)
    GkeJobDispatcher(settings, target=None).namespace_for(tenant)
    assert _mismatches(caplog, TENANT_RECORD_MISMATCH) == []
    assert _mismatches(caplog, "tenant_namespace_mismatch") == []
