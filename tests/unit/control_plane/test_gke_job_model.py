"""A GKE worker pod runs the model a Cloud Run Job runs (#226; contract request 53).

The owner accepted request 53 (claude-code on GKE Autopilot) on 2026-10-07 on
condition that MODEL reaches GKE pods first. Until then `GkeJobDispatcher`
set no `MODEL`: `gke_worker_env` is `worker_env` plus the metadata-server
address, and `worker_env` carries none on purpose (the model is the Job's,
never the task's). A claude-code pod on GKE would have run the CLI's default
model instead of the pinned `claude-opus-5-5`, silently.

These tests hold the GKE half to the Cloud Run half, profile by profile:

  * the same `WORKER_MODELS` gives the same `MODEL` on both backends, and a
    profile with no entry gets NO `MODEL` on either (the CLI default, on both);
  * a task's own `model` selects nothing on GKE either (invariant 10);
  * the per-task environment `gke_worker_env` still carries no `MODEL`: it is
    added by the manifest, from the scheduler's settings, as Cloud Run's is
    added by `_build_job`.

MUTATIONS: delete the `MODEL` append in `GkeJobDispatcher._manifest` and the
first test fails for claude-code; read `task.model` there and the third fails;
read a different settings field and the first fails for every pinned profile.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

from scheduler.dispatch import CloudRunJobDispatcher, GkeJobDispatcher, GkeTarget, gke_worker_env

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import FakeBatchApi, FakeJobsClient

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
PINNED_MODEL = "claude-opus-5-5"

#: terraform/infra/locals.tf `runner_models`, as WORKER_MODELS carries it to the
#: scheduler. Restated as test input only: the assertions compare the two
#: backends to EACH OTHER, so a value here can make neither of them right.
WORKER_MODELS = {
    "claude-code": PINNED_MODEL,
    "claude-code-review": PINNED_MODEL,
    "indexer": PINNED_MODEL,
}


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="u-alice",
        kind="user",
        principal="alice@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-u-alice@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/u-alice",
        namespace="swarm-tenant-u-alice",
    )


def _task(profile_name: str, *, model: str | None = None) -> Task:
    profile = RUNNER_PROFILES[profile_name]
    return Task(
        id="task_abc123",
        tenant_id="u-alice",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        model=model,
        timeout_seconds=1800,
    )


def _lease(task: Task) -> Lease:
    return Lease(
        lease_id="lease_1",
        task_id=task.id,
        attempt_id="att_1",
        tenant_id=task.tenant_id,
        generation=3,
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=8),
        expires_at=NOW + timedelta(minutes=2),
    )


def _gke_models(settings, tenant: Tenant, profile_name: str, *, task_model: str | None = None):
    """Every MODEL entry on the dispatched GKE pod's worker container (a list, to see duplicates)."""
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(
        settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api
    )
    task = _task(profile_name, model=task_model)
    dispatcher.dispatch(
        task=task, lease=_lease(task), profile=RUNNER_PROFILES[profile_name], tenant=tenant
    )
    container = api.created[0][1]["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "worker"
    return [e["value"] for e in container["env"] if e["name"] == "MODEL"], container["env"]


def _cloud_run_model(settings, tenant: Tenant, profile_name: str) -> str | None:
    job = CloudRunJobDispatcher(settings, client=FakeJobsClient())._build_job(
        RUNNER_PROFILES[profile_name], tenant
    )
    models = [e.value for e in job.template.template.containers[0].env if e.name == "MODEL"]
    assert len(models) <= 1, models
    return models[0] if models else None


@pytest.mark.parametrize("profile_name", sorted(RUNNER_PROFILES))
def test_a_gke_pod_carries_the_model_a_cloud_run_job_carries(tenant, profile_name):
    """Every profile, because any of them is one catalogue edit from GKE."""
    settings = scheduler_settings(worker_models=dict(WORKER_MODELS))

    gke, _ = _gke_models(settings, tenant, profile_name)
    cloud_run = _cloud_run_model(settings, tenant, profile_name)

    assert gke == ([cloud_run] if cloud_run else []), (
        f"{profile_name}: Cloud Run's Job runs MODEL={cloud_run!r}, the GKE pod {gke!r}"
    )


def test_a_claude_code_pod_on_gke_runs_the_pinned_model(tenant):
    """The case request 53 is about, stated as the value, not only as parity:
    parity alone would pass if both backends lost the model together."""
    settings = scheduler_settings(worker_models=dict(WORKER_MODELS))

    gke, _ = _gke_models(settings, tenant, "claude-code")

    assert gke == [PINNED_MODEL]
    assert _cloud_run_model(settings, tenant, "claude-code") == PINNED_MODEL


@pytest.mark.parametrize("profile_name", ["claude-code", "browser"])
def test_with_no_pinned_model_neither_backend_sets_one(tenant, profile_name):
    """No WORKER_MODELS entry: no MODEL on either backend, so the CLI's own
    default runs on both, rather than an empty MODEL on one of them."""
    settings = scheduler_settings(worker_models={})

    gke, env = _gke_models(settings, tenant, profile_name)

    assert gke == []
    assert _cloud_run_model(settings, tenant, profile_name) is None
    assert all(e["value"] != "" for e in env if e["name"] == "MODEL")


@pytest.mark.parametrize("worker_models", [dict(WORKER_MODELS), {}])
def test_a_tasks_own_model_selects_nothing_on_gke(tenant, worker_models):
    """Invariant 10 on the GKE path: the model is the Job's, never the caller's."""
    settings = scheduler_settings(worker_models=worker_models)

    gke, env = _gke_models(settings, tenant, "claude-code", task_model="claude-haiku-5")

    assert gke == ([PINNED_MODEL] if worker_models else [])
    assert "claude-haiku-5" not in [e["value"] for e in env]


def test_the_per_task_gke_environment_still_carries_no_model(tenant):
    """MODEL is added by the manifest from the scheduler's settings; the
    environment a task shapes carries none, on GKE as on Cloud Run."""
    settings = scheduler_settings(worker_models=dict(WORKER_MODELS))
    task = _task("claude-code", model="claude-haiku-5")

    env = gke_worker_env(task=task, lease=_lease(task), tenant=tenant, settings=settings)

    assert "MODEL" not in env
    assert "claude-haiku-5" not in env.values()
