"""The scheduler hands every worker the managed plugins bucket (managed-plugins.md §3).

The worker fetches each pinned plugin copy from SWARM_PLUGINS_BUCKET
(`agent_worker.pluginload.BUCKET_ENV`). Nothing set it in a worker's
environment, so no managed plugin could load. It is the scheduler's own
setting (terraform/infra/locals.tf renders `google_storage_bucket.plugins.name`
onto the swarm-scheduler service), passed through `worker_env` like the broker
URL -- a platform resource, never anything a caller sent (invariant 10).

WHAT IS PINNED
  * set: the Cloud Run execution override and the GKE pod both carry it;
  * empty or unset: OMITTED, never set empty -- a Cloud Run execution override
    MERGES into the Job's environment, and the worker's own fallback for an
    absent name is the bucket Terraform names by default;
  * a settings object without the field still renders, without the name;
  * `SchedulerSettings.from_env` reads it, stripped, default empty.
"""

from __future__ import annotations

from types import SimpleNamespace

from swarm_common.profiles import RUNNER_PROFILES

from scheduler.dispatch import (
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    gke_worker_env,
    worker_env,
)
from scheduler.settings import SchedulerSettings

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import FakeBatchApi, FakeJobsClient, make_lease, make_task
from .test_worker_env_console_links import _tenant

BUCKET = f"{PROJECT}-swarm-plugins"
NAME = "SWARM_PLUGINS_BUCKET"


def _env(settings, render=worker_env) -> dict[str, str]:
    task = make_task()
    return render(task=task, lease=make_lease(task), tenant=_tenant(), settings=settings)


def test_worker_env_carries_the_plugins_bucket():
    assert _env(scheduler_settings(plugins_bucket=BUCKET))[NAME] == BUCKET


def test_the_gke_worker_env_carries_the_plugins_bucket_too():
    assert _env(scheduler_settings(plugins_bucket=BUCKET), gke_worker_env)[NAME] == BUCKET


def test_the_cloud_run_execution_override_carries_the_plugins_bucket():
    client = FakeJobsClient()
    dispatcher = CloudRunJobDispatcher(scheduler_settings(plugins_bucket=BUCKET), client=client)
    task = make_task()
    dispatcher.dispatch(task=task, lease=make_lease(task),
                        profile=RUNNER_PROFILES[task.runner_profile], tenant=_tenant())
    override = client.runs[0]["overrides"].container_overrides[0]
    assert {e.name: e.value for e in override.env}[NAME] == BUCKET


def test_the_gke_pod_carries_the_plugins_bucket():
    dispatcher = GkeJobDispatcher(scheduler_settings(plugins_bucket=BUCKET),
                                  target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=FakeBatchApi())
    task = make_task()
    manifest = dispatcher._manifest(task=task, lease=make_lease(task),
                                    profile=RUNNER_PROFILES[task.runner_profile],
                                    tenant=_tenant())
    worker = manifest["spec"]["template"]["spec"]["containers"][0]
    assert {e["name"]: e["value"] for e in worker["env"]}[NAME] == BUCKET


def test_worker_env_omits_an_empty_plugins_bucket():
    assert NAME not in _env(scheduler_settings(plugins_bucket="  "))
    assert NAME not in _env(scheduler_settings())


def test_worker_env_tolerates_settings_without_the_field():
    base = scheduler_settings()
    bare = SimpleNamespace(project_id=base.project_id, region=base.region, core=base.core)
    assert NAME not in _env(bare)


def test_scheduler_settings_read_the_plugins_bucket(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv(NAME, f" {BUCKET} ")
    assert SchedulerSettings.from_env().plugins_bucket == BUCKET


def test_scheduler_settings_plugins_bucket_defaults_to_empty(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.delenv(NAME, raising=False)
    assert SchedulerSettings.from_env().plugins_bucket == ""
