"""A GKE worker is told its local scratch volumes and their limits (#2436).

kubelet evicts a GKE worker pod, with no grace period, the moment one of its
disk-backed emptyDirs passes its `sizeLimit`, and says only
`Usage of EmptyDir volume "tmp" exceeds the limit "3Gi"`. To report what filled
the volume, and to warn before it does, the worker has to know which of its
paths are such volumes and how big each limit is. The dispatcher tells it, in
SWARM_SCRATCH_VOLUMES: `<emptyDir name>=<mountPath>:<sizeLimit GiB>`, comma
separated, built from the same constants the manifest is.

What holds, for every resource class in GKE_DISK:

* the worker container's SWARM_SCRATCH_VOLUMES names EXACTLY the manifest's own
  disk-backed emptyDirs (no `medium`) that the worker container mounts, each with
  that mount's path and that volume's sizeLimit;
* the cache volume (a network disk, never an eviction) and dshm (a memory tmpfs,
  charged to the memory limit) are not in it;
* a Cloud Run worker is not given it: it has no such volumes.

MUTATIONS: drop a volume from `scratch_volumes_value`, or give it a hand-written
size (the first test names the class and the difference); add the cache or dshm
(the second); move the variable into `worker_env` (the third names where it
leaked).
"""

from __future__ import annotations

import dataclasses

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import (
    GKE_DISK,
    SCRATCH_VOLUMES_ENV,
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
    gke_disk,
    scratch_volumes_value,
    worker_env,
)

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeBatchApi, FakeJobsClient, make_lease, make_task


@pytest.fixture
def settings():
    return scheduler_settings()


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@example.com",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _pod(settings, tenant, resource_class: str) -> dict:
    """The pod the GKE dispatcher sends for a claude-code task of this class."""
    task = dataclasses.replace(make_task("claude-code"), resource_class=resource_class)
    api = FakeBatchApi()
    GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api).dispatch(
        task=task, lease=make_lease(task), profile=RUNNER_PROFILES["claude-code"], tenant=tenant
    )
    return api.created[0][1]["spec"]["template"]["spec"]


def _worker(pod: dict) -> dict:
    return next(c for c in pod["containers"] if c["name"] == "worker")


def parse(value: str) -> dict[str, tuple[str, int]]:
    """`name=/mount:gib,...` as {name: (mount, gib)}, refusing a repeated name."""
    entries: dict[str, tuple[str, int]] = {}
    for item in value.split(","):
        name, _, rest = item.partition("=")
        mount, _, gib = rest.rpartition(":")
        assert name and mount and gib.isdigit(), f"malformed entry {item!r} in {value!r}"
        assert name not in entries, f"{name!r} named twice in {value!r}"
        entries[name] = (mount, int(gib))
    return entries


def local_disk_volumes(pod: dict) -> dict[str, tuple[str, int]]:
    """What the manifest itself says: disk emptyDirs mounted in the worker container."""
    mounts = {m["name"]: m["mountPath"] for m in _worker(pod)["volumeMounts"]}
    found: dict[str, tuple[str, int]] = {}
    for volume in pod["volumes"]:
        empty = volume.get("emptyDir")
        if empty is None or "medium" in empty or volume["name"] not in mounts:
            continue
        limit = empty["sizeLimit"]
        assert limit.endswith("Gi"), f"{volume['name']}: sizeLimit {limit!r} is not in GiB"
        found[volume["name"]] = (mounts[volume["name"]], int(limit[:-2]))
    return found


@pytest.mark.parametrize("resource_class", sorted(GKE_DISK))
def test_every_gke_class_names_its_disk_emptydirs_and_their_limits(settings, tenant, resource_class):
    pod = _pod(settings, tenant, resource_class)
    env = {e["name"]: e["value"] for e in _worker(pod)["env"]}
    assert SCRATCH_VOLUMES_ENV == "SWARM_SCRATCH_VOLUMES"
    assert SCRATCH_VOLUMES_ENV in env, f"{resource_class}: the worker is not told its scratch volumes"
    told = parse(env[SCRATCH_VOLUMES_ENV])
    expected = local_disk_volumes(pod)
    assert expected, f"{resource_class}: the manifest has no disk emptyDir; this would check nothing"
    assert told == expected, (
        f"{resource_class}: SWARM_SCRATCH_VOLUMES says {told}, the manifest's own disk "
        f"emptyDirs are {expected}"
    )
    # The value is the pure function's, for this class's disk.
    assert env[SCRATCH_VOLUMES_ENV] == scratch_volumes_value(gke_disk(RESOURCE_CLASSES[resource_class]))


def test_cache_and_dshm_are_not_scratch_volumes(settings, tenant):
    for resource_class in sorted(GKE_DISK):
        pod = _pod(settings, tenant, resource_class)
        names = {v["name"] for v in pod["volumes"]}
        # Both exist on the pod, so their absence below is a choice, not a vacancy.
        assert {"cache", "dshm"} <= names, f"{resource_class}: {sorted(names)}"
        env = {e["name"]: e["value"] for e in _worker(pod)["env"]}
        told = parse(env[SCRATCH_VOLUMES_ENV])
        assert "cache" not in told, f"{resource_class}: the network-disk cache is never an eviction"
        assert "dshm" not in told, f"{resource_class}: dshm is memory, not ephemeral-storage"
        assert all(mount not in ("/cache", "/dev/shm") for mount, _ in told.values()), told


def test_cloud_run_carries_no_scratch_volumes(settings, tenant):
    profiles = sorted(
        name for name, p in RUNNER_PROFILES.items() if resolve_backend(p) is Backend.CLOUD_RUN_JOB
    )
    assert profiles, "no Cloud Run profile; this would check nothing"
    leaked: list[str] = []
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
            if SCRATCH_VOLUMES_ENV in names:
                leaked.append(f"{profile_name}: {where}")
    assert not leaked, f"a Cloud Run worker was told of GKE scratch volumes: {leaked}"
