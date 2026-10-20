"""A GKE worker pod's CPU bursts to twice its request; memory never bursts
(SIZES-SCHED-1, docs/design/runner-sizes.md §4.1, §4.4, §6; part of #1689).

Contract request RS-1 (accepted by the owner 2026-10-20, merged as
SIZES-CONTRACT-1) amended invariant 7 for CPU only: on GKE Autopilot a
container's CPU limit may be up to `CPU_BURST_MAX_RATIO` x its request, while
memory and ephemeral-storage keep request == limit on every container, and a
Cloud Run Job renders the request as its single number. These hold:

  * a GKE worker requests its class's `cpu` and is limited to its
    `cpu_limit_cores`, both as millicores ("4000m", "8000m"), so a
    quarter-core request is exact; memory and ephemeral-storage are the same
    in both directions;
  * the live-browser split renders worker 2 / 4 and display 2 / 3 cores, with
    memory 3 and 5 GiB unchanged and equal in both directions, and the two
    REQUESTS still sum to the class, so admission's units and the bill do not
    move;
  * a Cloud Run Job's `limits.cpu` is the request ("4"), never the burst
    limit: Cloud Run bills its limit, so rendering 8 would double the bill;
  * a class rolled back to `cpu_limit=None` (RS-1's rollback) renders
    request == limit again, live split included;
  * no container anywhere has a memory limit different from its request: a
    sweep over every profile x class x live on/off, which prints and asserts
    the count it visited.

Each assertion carries a MUTATION comment naming the change in
apps/scheduler/scheduler/dispatch.py that turns it red.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import PROJECT, scheduler_settings  # noqa: E402
from control_plane.test_dispatch_manifests import NOW, make_lease, make_task  # noqa: E402

from swarm_common.models import Tenant  # noqa: E402
from swarm_common.profiles import (  # noqa: E402
    CPU_BURST_MAX_RATIO,
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    Backend,
    ResourceClass,
    resolve_backend,
)

from scheduler.dispatch import (  # noqa: E402
    CloudRunJobDispatcher,
    GkeJobDispatcher,
    GkeTarget,
)

LIVE_PROFILE = "claude-code-browser"


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


def _gke_job(tenant: Tenant, profile_name: str, *, live: bool = False,
             resource_class: str | None = None) -> dict[str, Any]:
    dispatcher = GkeJobDispatcher(scheduler_settings(live_browser_sidecar=live),
                                  target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    task = make_task(profile_name)
    if resource_class is not None:
        task = dataclasses.replace(task, resource_class=resource_class)
    return dispatcher._manifest(task=task, lease=make_lease(task),
                                profile=RUNNER_PROFILES[profile_name], tenant=tenant)


def _containers(job: dict[str, Any]) -> list[dict[str, Any]]:
    spec = job["spec"]["template"]["spec"]
    return spec["containers"] + spec.get("initContainers", [])


def _named(job: dict[str, Any], name: str) -> dict[str, Any]:
    (container,) = [c for c in _containers(job) if c["name"] == name]
    return container


def _cpu(quantity: str) -> float:
    q = str(quantity)
    return int(q[:-1]) / 1000 if q.endswith("m") else float(q)


def _profiles(backend: Backend) -> list[str]:
    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if not p.control_plane and resolve_backend(p) is backend)


# --- the plain GKE worker --------------------------------------------------------


def test_a_claude_code_pod_requests_4_cores_and_bursts_to_8(tenant):
    resources = _named(_gke_job(tenant, "claude-code"), "worker")["resources"]
    # MUTATION: render `"limits": resources` (one dict for both directions) in
    # GkeJobDispatcher._manifest and limits.cpu reads "4000m".
    assert resources["limits"]["cpu"] == "8000m"
    # MUTATION: render the request from `rc.cpu_limit_cores` and this reads "8000m".
    assert resources["requests"]["cpu"] == "4000m"
    # MUTATION: render the limit's memory from anything but `rc.memory_gib`
    # (a memory burst) and the two directions differ.
    assert resources["requests"]["memory"] == resources["limits"]["memory"] == "8Gi"
    # MUTATION: drop ephemeral-storage from the limits dict and this fails.
    assert resources["requests"]["ephemeral-storage"] == resources["limits"]["ephemeral-storage"]
    assert set(resources["requests"]) == set(resources["limits"]) == {
        "cpu", "memory", "ephemeral-storage"}


@pytest.mark.parametrize("profile_name", _profiles(Backend.GKE_AUTOPILOT))
def test_every_gke_profile_bursts_to_its_class_limit(tenant, profile_name):
    rc = RESOURCE_CLASSES[RUNNER_PROFILES[profile_name].resource_class]
    resources = _named(_gke_job(tenant, profile_name), "worker")["resources"]
    # MUTATION: hard-code `"8000m"` in _manifest instead of reading
    # `rc.cpu_limit_cores` and a class with another limit is wrong here.
    assert _cpu(resources["limits"]["cpu"]) == rc.cpu_limit_cores
    assert _cpu(resources["requests"]["cpu"]) == rc.cpu


def test_a_step_that_shrank_the_class_bursts_to_the_smaller_classs_limit(tenant):
    """Sizing comes from `task.resource_class` (dispatch.py's module
    docstring), for the limit as for the request."""
    resources = _named(_gke_job(tenant, "browser", resource_class="standard"),
                       "worker")["resources"]
    standard = RESOURCE_CLASSES["standard"]
    # MUTATION: read the limit from `profile.resource_class` and a shrunk
    # step bursts to the class it did not pay for.
    assert _cpu(resources["limits"]["cpu"]) == standard.cpu_limit_cores
    assert _cpu(resources["requests"]["cpu"]) == standard.cpu


def test_a_quarter_core_request_renders_exactly_in_millicores(tenant, monkeypatch):
    monkeypatch.setitem(RESOURCE_CLASSES, "standard", ResourceClass(
        "standard", cpu=2.25, memory_gib=8, disk_gib=4, units=1, cpu_limit=4.5))
    resources = _named(_gke_job(tenant, "claude-code"), "worker")["resources"]
    # MUTATION: render CPU as `str(int(cores))` and this reads "2" / "4".
    assert resources["requests"]["cpu"] == "2250m"
    assert resources["limits"]["cpu"] == "4500m"


# --- the live-browser split ------------------------------------------------------


def test_the_live_split_renders_worker_2_of_4_and_display_2_of_3(tenant):
    job = _gke_job(tenant, LIVE_PROFILE, live=True)
    worker, display = _named(job, "worker")["resources"], _named(job, "display")["resources"]
    # MUTATION: set the worker's `limits` to its requests in with_live_display
    # and this reads "2000m".
    assert (worker["requests"]["cpu"], worker["limits"]["cpu"]) == ("2000m", "4000m")
    # MUTATION: drop `cpu_limit=3` from LIVE_DISPLAY_SHARE["browser"] and the
    # display bursts to the class ratio, "4000m".
    assert (display["requests"]["cpu"], display["limits"]["cpu"]) == ("2000m", "3000m")
    # MUTATION: give either container a memory limit above its request.
    assert worker["requests"]["memory"] == worker["limits"]["memory"] == "3Gi"
    assert display["requests"]["memory"] == display["limits"]["memory"] == "5Gi"
    for r in (worker, display):
        assert r["requests"]["ephemeral-storage"] == r["limits"]["ephemeral-storage"]


@pytest.mark.parametrize("resource_class", sorted(RESOURCE_CLASSES))
def test_the_live_split_requests_still_sum_to_the_class(tenant, resource_class):
    """Admission charged the class's units and Autopilot bills requests, so
    the REQUESTS sum to the class; the limits are free and bounded by it."""
    rc = RESOURCE_CLASSES[resource_class]
    containers = _containers(_gke_job(tenant, LIVE_PROFILE, live=True,
                                      resource_class=resource_class))
    assert len(containers) == 2
    # MUTATION: compute the worker's request from `rc.cpu_limit_cores - share.cpu`
    # and the pod requests (and is billed) more than admission charged.
    assert sum(_cpu(c["resources"]["requests"]["cpu"]) for c in containers) == rc.cpu
    # MUTATION: give the display `share.cpu * CPU_BURST_MAX_RATIO` uncapped
    # for a class whose own ratio is lower, and the pod's limit passes the class's.
    assert sum(_cpu(c["resources"]["limits"]["cpu"]) for c in containers) <= rc.cpu_limit_cores


# --- Cloud Run renders the request ------------------------------------------------


@pytest.mark.parametrize("resource_class", sorted(RESOURCE_CLASSES))
@pytest.mark.parametrize("profile_name", _profiles(Backend.CLOUD_RUN_JOB))
def test_a_cloud_run_job_is_limited_to_the_request_never_the_burst(tenant, profile_name,
                                                                  resource_class):
    rc = RESOURCE_CLASSES[resource_class]
    job = CloudRunJobDispatcher(scheduler_settings(), client=object())._build_job(
        RUNNER_PROFILES[profile_name], tenant, resource_class)
    (container,) = job.template.template.containers
    # MUTATION: render `rc.cpu_limit_cores` in CloudRunJobDispatcher._build_job
    # and a standard Job reads "8": twice the bill, no burst.
    assert container.resources.limits["cpu"] == str(int(rc.cpu))
    assert container.resources.limits["cpu"] != str(int(rc.cpu_limit_cores))
    assert container.resources.limits["memory"] == f"{rc.memory_gib}Gi"


# --- RS-1's rollback ------------------------------------------------------------


def test_a_class_without_a_cpu_limit_renders_requests_equal_to_limits(tenant, monkeypatch):
    for name, rc in list(RESOURCE_CLASSES.items()):
        monkeypatch.setitem(RESOURCE_CLASSES, name, dataclasses.replace(rc, cpu_limit=None))
    plain = _named(_gke_job(tenant, "claude-code"), "worker")["resources"]
    # MUTATION: render the limit as `CPU_BURST_MAX_RATIO * rc.cpu` instead of
    # `rc.cpu_limit_cores` and a rolled-back class still bursts.
    assert plain["requests"] == plain["limits"]
    for container in _containers(_gke_job(tenant, LIVE_PROFILE, live=True)):
        r = container["resources"]
        # MUTATION: apply LIVE_DISPLAY_SHARE's `cpu_limit` without capping it
        # at the class's own ratio and the display still bursts to 3.
        assert r["requests"] == r["limits"], container["name"]


# --- the sweep ------------------------------------------------------------------


def test_no_container_anywhere_has_a_memory_limit_different_from_its_request(tenant, capsys):
    visited = 0
    expected = 0
    for profile_name in sorted(RUNNER_PROFILES):
        profile = RUNNER_PROFILES[profile_name]
        if profile.control_plane:
            continue
        for resource_class in sorted(RESOURCE_CLASSES):
            rc = RESOURCE_CLASSES[resource_class]
            if resolve_backend(profile) is Backend.CLOUD_RUN_JOB:
                expected += 1
                job = CloudRunJobDispatcher(scheduler_settings(), client=object())._build_job(
                    profile, tenant, resource_class)
                for container in job.template.template.containers:
                    visited += 1
                    # Cloud Run has one number per resource, so a request
                    # below it is impossible; the number is the class's.
                    assert dict(container.resources.limits) == {
                        "cpu": str(int(rc.cpu)), "memory": f"{rc.memory_gib}Gi"}, profile_name
                continue
            for live in (False, True):
                job = _gke_job(tenant, profile_name, live=live, resource_class=resource_class)
                containers = _containers(job)
                expected += 2 if live and profile.live_browser else 1
                for container in containers:
                    visited += 1
                    r = container["resources"]
                    where = f"{profile_name}/{resource_class}/live={live}/{container['name']}"
                    # MUTATION: render a memory limit above the request on any
                    # container (a memory burst is a kill) and this fails.
                    assert r["requests"]["memory"] == r["limits"]["memory"], where
                    assert (r["requests"]["ephemeral-storage"]
                            == r["limits"]["ephemeral-storage"]), where
                    cpu_request, cpu_limit = _cpu(r["requests"]["cpu"]), _cpu(r["limits"]["cpu"])
                    # MUTATION: render any CPU limit past CPU_BURST_MAX_RATIO x
                    # the request, or below it, and this fails.
                    assert cpu_request <= cpu_limit <= CPU_BURST_MAX_RATIO * cpu_request, where
    with capsys.disabled():
        print(f"\nmemory sweep: visited {visited} containers")
    # A sweep that ran over nothing is not a clean sweep (CLAUDE.md, rule zero).
    assert visited == expected
    assert visited >= 3 * len(RESOURCE_CLASSES)
