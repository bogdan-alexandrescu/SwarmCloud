"""A GKE worker pod has a cache volume of its own; a Cloud Run worker keeps caches small (CACHE-SCRATCH).

Measured 2026-10-15: task_41ae250f, task_c6a07695 (CI-fix rounds on PR 1338)
and task_44c61c77 (BROWSER-CLASS implement) were each evicted twice with
`Usage of EmptyDir volume "workspace" exceeds the limit "5Gi"`. The agent's
HOME and TMPDIR were both on the workspace volume, so every tool cache was too.

What holds, for every profile the GKE dispatcher can send:

* the pod has a `cache` emptyDir, DISK-backed (no medium) with an explicit
  sizeLimit, GKE_DISK's for the class, mounted at CACHE_MOUNT -- outside the
  workspace mount -- and the worker is told where with SWARM_CACHE_ROOT, the
  name the worker reads (`agent_worker.cachedirs.CACHE_ROOT_ENV`);
* the pod's ephemeral-storage is the sum of every disk volume, the cache
  included, with requests == limits (invariant 7), within Autopilot's 10 GiB;
* every worker template under kubernetes/worker-templates/ carries the same
  volume, mount and variable (the parity test in test_kubernetes_manifests.py
  compares volumes and mounts; the variable is held here);
* a Cloud Run worker is NOT given SWARM_CACHE_ROOT, on the execution override,
  the Job the scheduler creates or `worker_env`: there the worker falls back to
  a small cache inside its tmpfs, with uv's and pip's switched off.

MUTATIONS: drop the `cache` volume or its mount from `_manifest` (the first
test names it); give it `medium: Memory` (it is no longer disk); leave its
sizeLimit out of `GkeDisk.ephemeral_gib` (the sum test); move SWARM_CACHE_ROOT
into `worker_env` (the Cloud Run test names where it leaked); drop it from any
one template (the template test names the file).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_worker.cachedirs import CACHE_ROOT_ENV
from swarm_common.models import Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import (
    AUTOPILOT_MAX_EPHEMERAL_GIB,
    CACHE_MOUNT,
    GKE_DISK,
    WORKSPACE_MOUNT,
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    worker_env,
)

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeBatchApi, FakeJobsClient, make_lease, make_task

TEMPLATES = Path(__file__).resolve().parents[3] / "kubernetes" / "worker-templates"


@pytest.fixture
def settings():
    return scheduler_settings()


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _gke_profiles() -> list[str]:
    """Every GKE profile, plus claude-code (on GKE since contract request 53)."""
    return sorted({"claude-code"} | {
        name for name, p in RUNNER_PROFILES.items() if resolve_backend(p) is Backend.GKE_AUTOPILOT
    })


def _pod(settings, tenant, profile_name: str) -> dict:
    task = make_task(profile_name)
    api = FakeBatchApi()
    GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api).dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES[profile_name], tenant=tenant
    )
    return api.created[0][1]["spec"]["template"]["spec"]


def _worker(pod: dict) -> dict:
    return next(c for c in pod["containers"] if c["name"] == "worker")


def _gib(quantity: str) -> int:
    assert quantity.endswith("Gi"), quantity
    return int(quantity[:-2])


def test_the_cache_mount_is_not_inside_the_workspace_mount():
    assert not Path(CACHE_MOUNT).is_relative_to(WORKSPACE_MOUNT), CACHE_MOUNT


@pytest.mark.parametrize("profile_name", _gke_profiles())
def test_a_gke_pod_has_a_disk_cache_volume_mounted_where_the_worker_is_told(
    settings, tenant, profile_name
):
    pod = _pod(settings, tenant, profile_name)
    worker = _worker(pod)
    rc = RESOURCE_CLASSES[make_task(profile_name).resource_class]

    volumes = {v["name"]: v for v in pod["volumes"]}
    assert "cache" in volumes, f"{profile_name}: the pod has no cache volume"
    empty_dir = volumes["cache"].get("emptyDir")
    assert empty_dir is not None, volumes["cache"]
    assert empty_dir.get("medium") in (None, ""), "the cache must be disk, not memory"
    assert empty_dir.get("sizeLimit") == f"{GKE_DISK[rc.name].cache_gib}Gi"

    mounts = {m["name"]: m["mountPath"] for m in worker["volumeMounts"]}
    assert mounts.get("cache") == CACHE_MOUNT, mounts

    env = {e["name"]: e.get("value") for e in worker["env"]}
    assert env.get(CACHE_ROOT_ENV) == CACHE_MOUNT, (
        f"{profile_name}: the worker is not told where the cache volume is"
    )


@pytest.mark.parametrize("profile_name", _gke_profiles())
def test_the_pods_ephemeral_storage_counts_the_cache_with_requests_equal_to_limits(
    settings, tenant, profile_name
):
    pod = _pod(settings, tenant, profile_name)
    resources = _worker(pod)["resources"]
    disk = {
        v["name"]: _gib(v["emptyDir"]["sizeLimit"])
        for v in pod["volumes"]
        if "emptyDir" in v and v["emptyDir"].get("medium") != "Memory"
    }

    assert resources["requests"] == resources["limits"], "invariant 7"
    limit = _gib(resources["limits"]["ephemeral-storage"])
    assert {"workspace", "cache", "tmp", "home"} <= set(disk), disk
    assert sum(disk.values()) == limit, f"{disk} against a {limit} GiB pod limit"
    assert limit <= AUTOPILOT_MAX_EPHEMERAL_GIB


@pytest.mark.parametrize("name", sorted(GKE_DISK))
def test_every_class_has_a_cache_and_its_sum_includes_it(name):
    disk = GKE_DISK[name]
    assert disk.cache_gib >= 1, name
    assert disk.ephemeral_gib == disk.workspace_gib + disk.cache_gib + disk.tmp_gib + disk.home_gib


@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES.glob("*.yaml")))
def test_every_worker_template_carries_the_cache_volume_mount_and_variable(template):
    text = (TEMPLATES / template).read_text()
    assert re.search(
        rf"- name: {CACHE_ROOT_ENV}\n\s+value: {re.escape(CACHE_MOUNT)}\n", text
    ), f"{template}: no {CACHE_ROOT_ENV}={CACHE_MOUNT} on the worker"
    assert re.search(
        rf"- name: cache\n\s+mountPath: {re.escape(CACHE_MOUNT)}\n", text
    ), f"{template}: the cache volume is not mounted at {CACHE_MOUNT}"
    assert re.search(
        r"- name: cache\n\s+emptyDir:\n\s+sizeLimit: __CACHE_DISK__\n", text
    ), f"{template}: no disk-backed cache volume sized __CACHE_DISK__"


def test_cloud_run_workers_are_not_given_a_cache_root(settings, tenant):
    """Cloud Run has no second disk; its worker keeps caches in its tmpfs."""
    profiles = sorted(
        name for name, p in RUNNER_PROFILES.items() if resolve_backend(p) is Backend.CLOUD_RUN_JOB
    )
    assert profiles, "no Cloud Run profile; this would check nothing"
    leaked: dict[str, str] = {}
    for profile_name in profiles:
        client = FakeJobsClient()
        task = make_task(profile_name)
        lease = make_lease(task)
        CloudRunJobDispatcher(settings, client=client).dispatch(
            task=task, lease=lease, profile=RUNNER_PROFILES[profile_name], tenant=tenant
        )
        overrides = client.runs[0]["overrides"].container_overrides
        override_names = {e.name for o in overrides for e in o.env}
        assert "TASK_ID" in override_names, f"{profile_name}: no worker env in the override"
        job = client.created[0]["job"]
        job_names = {e.name for c in job.template.template.containers for e in c.env}
        shared = set(worker_env(task=task, lease=lease, tenant=tenant, settings=settings))
        for where, names in (
            ("execution override", override_names),
            ("Cloud Run Job", job_names),
            ("worker_env", shared),
        ):
            if CACHE_ROOT_ENV in names:
                leaked[f"{profile_name}: {where}"] = CACHE_ROOT_ENV
    assert not leaked, f"a Cloud Run worker was told of a cache volume it does not have: {leaked}"
