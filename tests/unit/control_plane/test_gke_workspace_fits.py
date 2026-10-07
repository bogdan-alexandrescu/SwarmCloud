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

MUTATIONS: point `resources["ephemeral-storage"]` in `GkeJobDispatcher._manifest`
back at `rc.disk_gib`, and the first test fails; raise GKE_TMP_GIB or
GKE_HOME_GIB without the total, and it fails too; raise the total past 10 and
the Autopilot test fails; shrink the workspace below Cloud Run's and the
workspace test fails.
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
    GKE_TMP_GIB,
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


def _pod(tenant: Tenant, profile_name: str) -> dict:
    profile = RUNNER_PROFILES[profile_name]
    task = Task(
        id="task_ws0001",
        tenant_id=tenant.tenant_id,
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
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
    """The archive is built under /tmp (`checkpoint.py`, TemporaryDirectory)."""
    from agent_worker.config import WorkerConfig

    defaults = {f.name: f.default for f in dataclasses.fields(WorkerConfig)}

    assert GKE_TMP_GIB * GIB >= defaults["max_checkpoint_bytes"]


@pytest.mark.parametrize("profile_name", ["claude-code"] + sorted(
    name for name, p in RUNNER_PROFILES.items() if resolve_backend(p) is Backend.GKE_AUTOPILOT
))
def test_no_gke_pod_asks_autopilot_for_more_disk_than_it_admits(tenant, profile_name):
    """Autopilot refuses a general-purpose pod requesting over 10 GiB of
    ephemeral storage, so a larger number would be a pod that never starts."""
    resources = _worker(_pod(tenant, profile_name))["resources"]

    assert _gib(resources["requests"]["ephemeral-storage"]) <= AUTOPILOT_MAX_EPHEMERAL_GIB
