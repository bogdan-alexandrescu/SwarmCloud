"""A claude-code workspace that fits on Cloud Run fits on GKE (contract request 53).

On Cloud Run the `standard` class's workspace is a 4 GiB tmpfs inside the
8 GiB memory limit, and /tmp and HOME sit OUTSIDE it, in the container's
in-memory writable layer. On GKE all three are disk `emptyDir`s, and kubelet
sums every one of them against the pod's single ephemeral-storage limit,
evicting the pod -- no checkpoint, no park -- when the sum passes it. That
limit was `disk_gib`, 4 GiB: a full workspace left no room for the checkpoint
archive the worker builds in /tmp (up to `max_checkpoint_bytes`, 2 GiB), so a
run that fits on Cloud Run would have been evicted on GKE at its first
checkpoint.

The same arithmetic held nowhere else until 2026-10-07: the browser class
mounted workspace 8 + /tmp 2 + HOME 4 = 14 GiB of disk under an 8 GiB pod limit,
so a browser pod could be evicted before any one volume was full (owner
decision, contract request 53). So the sum is now checked for EVERY resource
class the GKE manifest renders, not only claude-code's.

MUTATIONS: point `resources["ephemeral-storage"]` in `GkeJobDispatcher._manifest`
back at `rc.disk_gib`, and the claude-code test fails; put browser's `GkeDisk`
back to 8/2/4, or point the workspace volume back at `rc.disk_gib`, and the
every-class test fails for browser; raise any class's total past 10 and it
fails too; shrink claude-code's workspace below Cloud Run's and the workspace
test fails.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend, resolve_backend
from swarm_common.states import TaskState

from scheduler.dispatch import (
    AUTOPILOT_MAX_EPHEMERAL_GIB,
    GKE_DISK,
    GkeJobDispatcher,
    GkeTarget,
    workspace_size_gib,
)

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import FakeBatchApi

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
GIB = 1024**3


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


def _pod(tenant: Tenant, profile_name: str, resource_class: str | None = None) -> dict:
    profile = RUNNER_PROFILES[profile_name]
    task = Task(
        id="task_ws0001",
        tenant_id=tenant.tenant_id,
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=resource_class or profile.resource_class,
        input={},
        submitted_by="eng@saga.xyz",
        provider=profile.provider,
        timeout_seconds=7200,
    )
    lease = Lease(
        lease_id="lease_ws0001",
        task_id=task.id,
        attempt_id="att_ws0001",
        tenant_id=tenant.tenant_id,
        generation=1,
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=8),
        expires_at=NOW + timedelta(minutes=2),
    )
    api = FakeBatchApi()
    GkeJobDispatcher(
        scheduler_settings(), target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api
    ).dispatch(task=task, lease=lease, profile=profile, tenant=tenant)
    return api.created[0][1]["spec"]["template"]["spec"]


def _gib(quantity: str) -> int:
    assert quantity.endswith("Gi"), quantity
    return int(quantity[:-2])


def _worker(pod: dict) -> dict:
    return next(c for c in pod["containers"] if c["name"] == "worker")


def _disk_volumes(pod: dict) -> dict[str, int]:
    """Every disk-backed emptyDir the worker mounts, and its sizeLimit in GiB."""
    mounted = {m["name"] for m in _worker(pod)["volumeMounts"]}
    return {
        v["name"]: _gib(v["emptyDir"]["sizeLimit"])
        for v in pod["volumes"]
        if "emptyDir" in v and v["emptyDir"].get("medium") != "Memory" and v["name"] in mounted
    }


def test_a_claude_code_pod_has_disk_for_its_workspace_tmp_and_home_together(tenant):
    """Each volume's own sizeLimit must bind before the pod's total does, as the
    workspace limit binds on Cloud Run: a full workspace is then the workspace
    filling up, never the whole pod evicted for a checkpoint archive in /tmp."""
    pod = _pod(tenant, "claude-code")
    resources = _worker(pod)["resources"]
    volumes = _disk_volumes(pod)

    assert set(volumes) == {"workspace", "tmp", "home"}, volumes
    assert resources["requests"] == resources["limits"], "invariant 7"
    assert _gib(resources["limits"]["ephemeral-storage"]) >= sum(volumes.values()), (
        f"the pod may write {sum(volumes.values())} GiB across {volumes} but is evicted "
        f"at {resources['limits']['ephemeral-storage']}"
    )
    assert resources["limits"]["ephemeral-storage"] == "10Gi"


def test_the_gke_workspace_is_no_smaller_than_cloud_runs(tenant):
    rc = RESOURCE_CLASSES[RUNNER_PROFILES["claude-code"].resource_class]

    assert _disk_volumes(_pod(tenant, "claude-code"))["workspace"] >= workspace_size_gib(rc)


def test_tmp_holds_the_largest_checkpoint_archive_the_worker_builds():
    """The archive is built under /tmp (`checkpoint.py`, TemporaryDirectory).

    claude-code's class only. Browser's /tmp is 1 GiB by the owner's decision of
    2026-10-07 (see GKE_DISK): it fits the pod's 8 GiB, not the 2 GiB cap.
    """
    from agent_worker.config import WorkerConfig

    defaults = {f.name: f.default for f in dataclasses.fields(WorkerConfig)}

    standard = GKE_DISK[RUNNER_PROFILES["claude-code"].resource_class]
    assert standard.tmp_gib * GIB >= defaults["max_checkpoint_bytes"]


@pytest.mark.parametrize("profile_name", ["claude-code"] + sorted(
    name for name, p in RUNNER_PROFILES.items() if resolve_backend(p) is Backend.GKE_AUTOPILOT
))
def test_no_gke_pod_asks_autopilot_for_more_disk_than_it_admits(tenant, profile_name):
    """Autopilot refuses a general-purpose pod requesting over 10 GiB of
    ephemeral storage, so a larger number would be a pod that never starts."""
    resources = _worker(_pod(tenant, profile_name))["resources"]

    assert _gib(resources["requests"]["ephemeral-storage"]) <= AUTOPILOT_MAX_EPHEMERAL_GIB


def _gke_pods() -> list[tuple[str, str]]:
    """(profile, resource class) for every class the GKE manifest can render.

    The profiles are every GKE_AUTOPILOT one plus claude-code (contract request
    53 moves it to GKE), and each with its own class and every class a step may
    override it to -- swarm-api's own rule, `validate_resource_class_override`,
    not a restatement of it. Then every class in the catalogue on top, under
    claude-code, because `_manifest` renders whatever class it is handed and
    a class that is one catalogue edit from GKE should already fit.
    """
    from swarm_api.validation import ValidationFailed, validate_resource_class_override

    profiles = {"claude-code"} | {
        name for name, p in RUNNER_PROFILES.items() if resolve_backend(p) is Backend.GKE_AUTOPILOT
    }
    pairs: set[tuple[str, str]] = {("claude-code", rc) for rc in RESOURCE_CLASSES}
    for name in profiles:
        for rc in RESOURCE_CLASSES:
            try:
                pairs.add((name, validate_resource_class_override(RUNNER_PROFILES[name], rc)))
            except ValidationFailed:
                pass
    return sorted(pairs)


def test_every_resource_class_is_rendered_below():
    """The parametrisation below must not silently shrink to nothing."""
    assert {rc for _, rc in _gke_pods()} == set(RESOURCE_CLASSES)
    assert ("browser", "browser") in _gke_pods()
    assert ("claude-code", RUNNER_PROFILES["claude-code"].resource_class) in _gke_pods()


@pytest.mark.parametrize(("profile_name", "resource_class"), _gke_pods())
def test_a_gke_pods_scratch_volumes_never_add_up_past_its_disk_limit(
    tenant, profile_name, resource_class
):
    """sum(disk emptyDir sizeLimits) <= ephemeral-storage <= Autopilot's 10 GiB.

    kubelet evicts the pod when the volumes together pass its ephemeral-storage
    limit, so a sum above it is a pod evicted -- no checkpoint, no park --
    before any one volume is full. The memory-medium /dev/shm is charged to the
    memory limit, not to ephemeral-storage, and is left out of the sum.
    """
    pod = _pod(tenant, profile_name, resource_class)
    resources = _worker(pod)["resources"]
    volumes = _disk_volumes(pod)
    limit = _gib(resources["limits"]["ephemeral-storage"])

    assert resources["requests"] == resources["limits"], "invariant 7"
    assert {"workspace", "tmp", "home"} <= set(volumes), volumes
    assert sum(volumes.values()) <= limit, (
        f"{profile_name}/{resource_class}: the pod may write {sum(volumes.values())} GiB "
        f"across {volumes} but is evicted at {limit} GiB"
    )
    assert limit <= AUTOPILOT_MAX_EPHEMERAL_GIB, (
        f"{profile_name}/{resource_class}: {limit} GiB is more than Autopilot admits"
    )


def test_a_browser_pod_has_the_owners_disk_layout(tenant):
    """Owner decision 2026-10-07: workspace 5 + /tmp 1 + HOME 2 = the 8 GiB limit."""
    pod = _pod(tenant, "browser")

    assert _disk_volumes(pod) == {"workspace": 5, "tmp": 1, "home": 2}
    assert _worker(pod)["resources"]["limits"]["ephemeral-storage"] == "8Gi"
