"""A GKE worker pod's caches are on a network disk of their own; a Cloud Run worker keeps them small.

CACHE-SCRATCH (2026-10-15): task_41ae250f, task_c6a07695 (CI-fix rounds on PR
1338) and task_44c61c77 (BROWSER-CLASS implement) were each evicted twice with
`Usage of EmptyDir volume "workspace" exceeds the limit "5Gi"`. The agent's
HOME and TMPDIR were both on the workspace volume, so every tool cache was too.
The caches moved to a 2 GiB `cache` emptyDir of their own.

CACHE-PVC (owner decision 2026-10-15 ~04:00 PDT, "Network-disk cache"): that
2 GiB emptyDir evicted task_247c87a8714c4977921a (SEC-INJECT-TYPESAFE
implement) twice with `EmptyDir volume "cache" exceeds 2Gi`, and Autopilot
caps a pod's ephemeral storage at 10 GiB, so no local split has room for a
bigger one. The cache is now a generic ephemeral volume: a 20 GiB pd-balanced
PersistentVolumeClaim the ephemeral-volume controller creates with the pod and
deletes with it. A PVC is not ephemeral-storage.

What holds, for every profile the GKE dispatcher can send:

* the pod's `cache` volume is `ephemeral.volumeClaimTemplate`, on
  CACHE_STORAGE_CLASS, ReadWriteOnce, CACHE_VOLUME_GIB (20) GiB, NOT an
  emptyDir, mounted at CACHE_MOUNT -- outside the workspace mount -- and the
  worker is told where with SWARM_CACHE_ROOT, the name the worker reads
  (`agent_worker.cachedirs.CACHE_ROOT_ENV`);
* the pod's ephemeral-storage is the sum of the LOCAL disk volumes only
  (workspace, /tmp, HOME), with requests == limits (invariant 7), exactly
  Autopilot's 10 GiB;
* every worker template under kubernetes/worker-templates/ carries the same
  volume, mount and variable (the parity test in test_kubernetes_manifests.py
  compares volumes and mounts; the variable and the claim are held here);
* a Cloud Run worker is NOT given SWARM_CACHE_ROOT, on the execution override,
  the Job the scheduler creates or `worker_env`: there the worker falls back to
  a small cache inside its tmpfs, with uv's and pip's switched off.

MUTATIONS: put the `cache` volume back to an emptyDir, or drop it or its mount
from `_manifest` (the first test names it); change its class, size or access
mode (the same test); add the PVC's GiB to `GkeDisk.ephemeral_gib` (the sum
test); move SWARM_CACHE_ROOT into `worker_env` (the Cloud Run test names where
it leaked); put any one template back to an emptyDir (the template test names
the file).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agent_worker.cachedirs import CACHE_ROOT_ENV
from swarm_common.models import Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import (
    AUTOPILOT_MAX_EPHEMERAL_GIB,
    CACHE_MOUNT,
    CACHE_STORAGE_CLASS,
    CACHE_VOLUME_GIB,
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


def _claim_spec(volume: dict) -> dict:
    """The PVC spec of a generic ephemeral volume, or a failure naming what it is instead."""
    assert "emptyDir" not in volume, f"the cache is still an emptyDir: {volume}"
    ephemeral = volume.get("ephemeral")
    assert ephemeral is not None, f"the cache is not a generic ephemeral volume: {volume}"
    return ephemeral["volumeClaimTemplate"]["spec"]


def test_the_cache_mount_is_not_inside_the_workspace_mount():
    assert not Path(CACHE_MOUNT).is_relative_to(WORKSPACE_MOUNT), CACHE_MOUNT


def test_the_cache_volume_is_the_owners_20_gib():
    """The owner's number (CACHE-PVC). Ten times the emptyDir that evicted."""
    assert CACHE_VOLUME_GIB == 20


@pytest.mark.parametrize("profile_name", _gke_profiles())
def test_a_gke_pod_has_a_network_disk_cache_mounted_where_the_worker_is_told(
    settings, tenant, profile_name
):
    pod = _pod(settings, tenant, profile_name)
    worker = _worker(pod)

    volumes = {v["name"]: v for v in pod["volumes"]}
    assert "cache" in volumes, f"{profile_name}: the pod has no cache volume"
    claim = _claim_spec(volumes["cache"])
    assert claim["storageClassName"] == CACHE_STORAGE_CLASS, claim
    assert claim["accessModes"] == ["ReadWriteOnce"], claim
    assert claim["resources"] == {"requests": {"storage": f"{CACHE_VOLUME_GIB}Gi"}}, claim

    mounts = {m["name"]: m["mountPath"] for m in worker["volumeMounts"]}
    assert mounts.get("cache") == CACHE_MOUNT, mounts

    env = {e["name"]: e.get("value") for e in worker["env"]}
    assert env.get(CACHE_ROOT_ENV) == CACHE_MOUNT, (
        f"{profile_name}: the worker is not told where the cache volume is"
    )


@pytest.mark.parametrize("profile_name", _gke_profiles())
def test_the_pods_ephemeral_storage_is_its_local_volumes_alone_with_requests_equal_to_limits(
    settings, tenant, profile_name
):
    """The PVC is not ephemeral-storage: kubelet does not count it, so the
    pod's limit is workspace + /tmp + HOME, and it is all of Autopilot's 10."""
    pod = _pod(settings, tenant, profile_name)
    resources = _worker(pod)["resources"]
    disk = {
        v["name"]: _gib(v["emptyDir"]["sizeLimit"])
        for v in pod["volumes"]
        if "emptyDir" in v and v["emptyDir"].get("medium") != "Memory"
    }

    assert resources["requests"] == resources["limits"], "invariant 7"
    limit = _gib(resources["limits"]["ephemeral-storage"])
    assert set(disk) == {"workspace", "tmp", "home"}, disk
    assert sum(disk.values()) == limit, f"{disk} against a {limit} GiB pod limit"
    assert limit == AUTOPILOT_MAX_EPHEMERAL_GIB


@pytest.mark.parametrize("name", sorted(GKE_DISK))
def test_no_class_counts_the_cache_in_its_local_disk(name):
    disk = GKE_DISK[name]
    assert not hasattr(disk, "cache_gib"), f"{name}: the cache is a PVC, not a local GiB"
    assert disk.ephemeral_gib == disk.workspace_gib + disk.tmp_gib + disk.home_gib


@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES.glob("*.yaml")))
def test_every_worker_template_carries_the_cache_claim_mount_and_variable(template):
    text = (TEMPLATES / template).read_text()
    assert re.search(
        rf"- name: {CACHE_ROOT_ENV}\n\s+value: {re.escape(CACHE_MOUNT)}\n", text
    ), f"{template}: no {CACHE_ROOT_ENV}={CACHE_MOUNT} on the worker"
    assert re.search(
        rf"- name: cache\n\s+mountPath: {re.escape(CACHE_MOUNT)}\n", text
    ), f"{template}: the cache volume is not mounted at {CACHE_MOUNT}"
    pod = yaml.safe_load(text)["spec"]["template"]["spec"]
    volume = next((v for v in pod["volumes"] if v["name"] == "cache"), None)
    assert volume is not None, f"{template}: no cache volume"
    claim = _claim_spec(volume)
    assert claim["storageClassName"] == "__CACHE_STORAGE_CLASS__", f"{template}: {claim}"
    assert claim["accessModes"] == ["ReadWriteOnce"], f"{template}: {claim}"
    assert claim["resources"] == {"requests": {"storage": "__CACHE_DISK__"}}, f"{template}: {claim}"


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
