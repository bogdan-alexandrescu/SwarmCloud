"""The `browser` resource class is 4 vCPU / 8 GiB / 4 GiB workspace, 1 unit
(contract request 79, accepted by the owner 2026-10-14).

Measured first (docs/browser-startup-speed.md §11): four browser pods peaked
at 2.13 cores and 1.85 GiB over eight samples, under 80 % of 4 vCPU / 8 GiB.
The owner then dropped `units` from 2 to 1 ("Drop to 1", 2026-10-14 ~22:55
PDT), so a pool of N units admits N browser agents rather than N/2.

These hold:

  * the frozen catalogue says 4/8/4/1;
  * the GKE pod `claude-code-browser` renders asks for exactly 4 vCPU / 8 GiB,
    with requests == limits on every container (invariant 7) -- with the live
    display sidecar and without it, so the split still adds up to the class;
  * admission charges a browser agent 1 unit: a 3-unit pool admits three.

MUTATION: put `cpu=8, memory_gib=16, disk_gib=8, units=2` back in
profiles.py and every test here fails; leave LIVE_DISPLAY_SHARE["browser"] at
5 vCPU / 10 GiB and the live-sidecar test fails (the split no longer fits).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from swarm_common.models import Tenant
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES

from scheduler.dispatch import GkeJobDispatcher, GkeTarget

from .conftest import PROJECT, scheduler_settings, seed_pool, seed_task, seed_tenant
from .test_dispatch_manifests import NOW, make_lease, make_task

PROFILE = "claude-code-browser"


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


def _spec(tenant: Tenant, *, live: bool) -> dict[str, Any]:
    settings = scheduler_settings(live_browser_sidecar=live)
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    task = make_task(PROFILE)
    job = dispatcher._manifest(task=task, lease=make_lease(task),
                               profile=RUNNER_PROFILES[PROFILE], tenant=tenant)
    return job["spec"]["template"]["spec"]


def _gib(quantity: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Gi|Mi)", quantity)
    assert m, f"unexpected quantity {quantity!r}"
    return float(m.group(1)) * {"Gi": 1, "Mi": 1 / 1024}[m.group(2)]


def _cpu(quantity: str) -> float:
    q = str(quantity)
    return int(q[:-1]) / 1000 if q.endswith("m") else float(q)


def test_the_catalogue_browser_class_is_4_8_4_and_1_unit():
    rc = RESOURCE_CLASSES["browser"]
    assert (rc.cpu, rc.memory_gib, rc.disk_gib, rc.units) == (4, 8, 4, 1)


@pytest.mark.parametrize("live", [False, True], ids=["no-sidecar", "live-sidecar"])
def test_the_claude_code_browser_pod_requests_4_vcpu_8_gib_with_requests_equal_to_limits(
        tenant, live):
    spec = _spec(tenant, live=live)
    containers = spec["containers"] + spec.get("initContainers", [])
    assert len(containers) == (2 if live else 1)

    for c in containers:
        r = c["resources"]
        assert r["requests"] == r["limits"], f"{c['name']}: invariant 7, no bursting"
    assert sum(_cpu(c["resources"]["requests"]["cpu"]) for c in containers) == 4
    assert sum(_gib(c["resources"]["requests"]["memory"]) for c in containers) == 8


def test_admission_charges_a_browser_agent_one_unit(db, make_scheduler):
    """Through the real drain loop: a `resource:browser` pool of 3 admits 3 of 4."""
    seed_tenant(db, "eng", credentials=("anthropic",), max_active=100)
    seed_pool(db, "global", hard_limit=100)
    seed_pool(db, "resource:browser", hard_limit=3)
    for index in range(4):
        seed_task(db, task_id=f"task_browser_{index}", tenant_id="eng",
                  runner_profile=PROFILE, resource_class="browser", provider="anthropic")

    report = make_scheduler().drain()

    assert report.leased == 3, "a 3-unit pool admits three 1-unit browser agents"
    assert db.docs["pools/resource:browser"]["active"] == 3
    assert db.docs["pools/tenant:eng"]["active"] == 3
    leases = [d for k, d in db.docs.items() if k.startswith("leases/")]
    assert len(leases) == 3
    assert {lease["units"] for lease in leases} == {1}

