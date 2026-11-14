"""Contract request 135: the `builder` resource class, chosen by step stage (issue #3100).

Accepted by the owner on 13 November 2026 (8 CPU requested, 12 limit,
16 GiB, 2 units, chosen by step stage, GKE only); request 134 declined in
its favour. What is held here:

  * `builder` is 8 CPU requested, 12 limit, 16 GiB request == limit, 8 GiB
    workspace, 2 units (`units_for`), within CPU_BURST_MAX_RATIO (invariant
    7), between `medium` and `large` in `CLASS_ORDER`;
  * `resource_class_for_stage` gives `builder` to claude-code's implement,
    build and fix stages and the profile's own class to every other stage and
    every other profile, so reviews, checks and Cloud Run keep their class;
  * a builder GKE pod renders 8 / 12 CPU and 16 GiB, and a claude-code step
    at any other stage renders exactly what it rendered before;
  * a builder lease takes 2 units of every compute pool and 1 of each
    provider pool, all at once at lease time (invariants 2 and 3);
  * docs/contract-change-requests.md records 135 as accepted (DONE) and 134
    as declined.

No cloud, no emulator.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from common.test_claude_code_medium import _Db, _Txn  # noqa: E402
from control_plane.conftest import PROJECT, scheduler_settings  # noqa: E402
from control_plane.test_dispatch_manifests import NOW, make_lease, make_task  # noqa: E402

from swarm_common import profiles  # noqa: E402
from swarm_common.admission import AdmissionConfig, acquire_lease_in_transaction  # noqa: E402
from swarm_common.models import Task, Tenant, pool_names_for  # noqa: E402
from swarm_common.profiles import (  # noqa: E402
    BUILD_STAGES,
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    Backend,
    check_size,
    default_sizes,
    resolve_backend,
    resource_class_for_stage,
    units_for,
)
from swarm_common.states import TaskState  # noqa: E402

from scheduler import sizes  # noqa: E402
from scheduler.dispatch import GkeJobDispatcher, GkeTarget  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
REQUESTS = REPO / "docs" / "contract-change-requests.md"

TENANT = "eng"
BACKEND = "GKE_AUTOPILOT"
OTHER_STAGES = (None, "review", "requirements-check", "re-review", "merge", "plan", "")


def test_the_builder_class_is_8_cpu_requested_12_limit_16_gib_at_2_units():
    # MUTATION: delete `builder` from RESOURCE_CLASSES and this is a KeyError.
    rc = RESOURCE_CLASSES["builder"]
    assert (rc.name, rc.cpu, rc.cpu_limit_cores, rc.memory_gib, rc.disk_gib) == (
        "builder", 8, 12, 16, 8)
    # MUTATION: `units=3` and the import-time check refuses the module;
    # `units=1` and admission undercharges an 8-CPU pod.
    assert rc.units == 2 == units_for(rc.cpu, rc.memory_gib)
    # Invariant 7: CPU request <= limit <= the burst ratio x the request.
    assert rc.cpu <= rc.cpu_limit_cores <= profiles.CPU_BURST_MAX_RATIO * rc.cpu
    assert 1 <= rc.disk_gib <= rc.memory_gib - 2


def test_builder_sits_between_medium_and_large_and_its_defaults_pass_every_bound():
    assert profiles.CLASS_ORDER == ("standard", "browser", "medium", "builder", "large")
    assert check_size(default_sizes()) == []
    assert default_sizes()["builder"].units == 2


def test_no_profile_runs_builder_as_its_own_class():
    # A caller names a profile and never a size (invariant 10): `builder` is
    # reached only through the stage, never as a profile's default class.
    assert [p.name for p in RUNNER_PROFILES.values() if p.resource_class == "builder"] == []


def test_claude_code_alone_names_builder_and_it_is_on_gke():
    named = {p.name: p.build_resource_class for p in RUNNER_PROFILES.values()
             if p.build_resource_class is not None}
    # MUTATION: drop `build_resource_class="builder"` from claude-code and
    # this is {}; set it on another profile and that name appears.
    assert named == {"claude-code": "builder"}
    assert resolve_backend(RUNNER_PROFILES["claude-code"]) is Backend.GKE_AUTOPILOT


@pytest.mark.parametrize("stage", BUILD_STAGES)
def test_claude_codes_build_stages_run_builder(stage):
    assert BUILD_STAGES == ("implement", "build", "fix")
    assert resource_class_for_stage(RUNNER_PROFILES["claude-code"], stage) == "builder"


@pytest.mark.parametrize("stage", OTHER_STAGES)
def test_every_other_stage_keeps_claude_codes_own_class(stage):
    assert resource_class_for_stage(RUNNER_PROFILES["claude-code"], stage) == "standard"


@pytest.mark.parametrize("name", sorted(n for n in RUNNER_PROFILES if n != "claude-code"))
@pytest.mark.parametrize("stage", BUILD_STAGES + OTHER_STAGES)
def test_every_other_profile_keeps_its_class_at_every_stage(name, stage):
    profile = RUNNER_PROFILES[name]
    assert resource_class_for_stage(profile, stage) == profile.resource_class


def test_a_cloud_run_profile_cannot_name_a_build_class():
    mock = RUNNER_PROFILES["mock"]
    assert resolve_backend(mock) is Backend.CLOUD_RUN_JOB
    with pytest.raises(ValueError, match="build_resource_class"):
        dataclasses.replace(mock, build_resource_class="builder")


def test_an_unknown_build_class_is_refused():
    with pytest.raises(ValueError, match="build_resource_class"):
        dataclasses.replace(RUNNER_PROFILES["claude-code"], build_resource_class="huge")


# --- the GKE pod --------------------------------------------------------------


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id=TENANT,
        kind="group",
        principal="eng@example.com",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


def _cores(quantity: str) -> float:
    return float(quantity[:-1]) / 1000 if quantity.endswith("m") else float(quantity)


def _worker(tenant: Tenant, resource_class: str) -> dict[str, Any]:
    dispatcher = GkeJobDispatcher(scheduler_settings(),
                                  target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    task = dataclasses.replace(make_task("claude-code"), resource_class=resource_class)
    job = dispatcher._manifest(task=task, lease=make_lease(task),
                               profile=RUNNER_PROFILES["claude-code"], tenant=tenant)
    spec = job["spec"]["template"]["spec"]
    for container in spec["containers"] + spec.get("initContainers", []):
        r = container["resources"]
        # Invariant 7: memory and ephemeral storage request == limit.
        assert r["requests"]["memory"] == r["limits"]["memory"], container["name"]
        if "ephemeral-storage" in r["requests"]:
            assert r["requests"]["ephemeral-storage"] == r["limits"]["ephemeral-storage"]
        cpu, limit = _cores(r["requests"]["cpu"]), _cores(r["limits"]["cpu"])
        assert cpu <= limit <= profiles.CPU_BURST_MAX_RATIO * cpu, container["name"]
    (worker,) = [c for c in spec["containers"] if c["name"] == "worker"]
    return worker["resources"]


def test_a_builder_pod_requests_8_cpu_bursts_to_12_and_holds_16_gib(tenant):
    # MUTATION: no `builder` row in dispatch.GKE_DISK and the render refuses.
    r = _worker(tenant, "builder")
    assert _cores(r["requests"]["cpu"]) == 8
    assert _cores(r["limits"]["cpu"]) == 12
    assert r["requests"]["memory"] == r["limits"]["memory"] == "16Gi"


def test_a_claude_code_step_at_any_other_stage_renders_as_before(tenant):
    r = _worker(tenant, resource_class_for_stage(RUNNER_PROFILES["claude-code"], "review"))
    assert _cores(r["requests"]["cpu"]) == 4
    assert _cores(r["limits"]["cpu"]) == 8
    assert r["requests"]["memory"] == r["limits"]["memory"] == "8Gi"


# --- admission -----------------------------------------------------------------


ADMIT_AT = datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc)


def _task() -> Task:
    profile = RUNNER_PROFILES["claude-code"]
    return Task(
        id="task_builder",
        tenant_id=TENANT,
        created_at=ADMIT_AT,
        updated_at=ADMIT_AT,
        state=TaskState.READY,
        runner_profile=profile.name,
        resource_class=resource_class_for_stage(profile, "implement"),
        provider=profile.provider,
        input={},
        submitted_by="alice@example.com",
    )


def test_a_builder_lease_takes_2_units_of_every_compute_pool_at_lease_time():
    task = _task()
    required = pool_names_for(tenant_id=TENANT, provider=task.provider,
                              resource_class=task.resource_class,
                              runner_profile=task.runner_profile, backend=BACKEND)
    assert "resource:builder" in required and "resource:standard" not in required
    docs: dict[str, dict[str, Any]] = {
        f"pools/{name}": {"hard_limit": 10, "active": 0, "enabled": True} for name in required
    }
    docs[f"tasks/{task.id}"] = {"state": TaskState.READY.value, "current_generation": 0}
    db = _Db(docs)
    sized = sizes.for_task(sizes.DEFAULTS, task, backend=Backend.GKE_AUTOPILOT, live=False)
    assert (sized.resource_class, sized.size.units) == ("builder", 2)
    acquire_lease_in_transaction(_Txn(db), db=db, task=task, units=sized.size.units,
                                 backend=BACKEND, config=AdmissionConfig(), now=ADMIT_AT)
    # Invariant 3: counted from LEASED, the moment of this one transaction.
    assert db.docs[f"tasks/{task.id}"]["state"] == TaskState.LEASED.value
    compute = ("global", f"tenant:{TENANT}", "resource:builder", "runner:claude-code",
               f"backend:{BACKEND}")
    for pool in compute:
        assert db.docs[f"pools/{pool}"]["active"] == 2, pool
    for pool in required:
        if pool.startswith("provider:"):
            assert db.docs[f"pools/{pool}"]["active"] == 1, pool


def test_a_builder_lease_that_does_not_fit_one_pool_takes_nothing_anywhere():
    # Invariant 2: all or nothing. resource:builder has 1 unit left of 2.
    task = _task()
    required = pool_names_for(tenant_id=TENANT, provider=task.provider,
                              resource_class=task.resource_class,
                              runner_profile=task.runner_profile, backend=BACKEND)
    docs: dict[str, dict[str, Any]] = {
        f"pools/{name}": {"hard_limit": 10, "active": 0, "enabled": True} for name in required
    }
    docs["pools/resource:builder"]["hard_limit"] = 1
    docs[f"tasks/{task.id}"] = {"state": TaskState.READY.value, "current_generation": 0}
    db = _Db(docs)
    try:
        acquire_lease_in_transaction(_Txn(db), db=db, task=task, units=2, backend=BACKEND,
                                     config=AdmissionConfig(), now=ADMIT_AT)
    except Exception:  # noqa: BLE001 -- a refusal may raise or return; both take nothing
        pass
    for name in required:
        assert db.docs[f"pools/{name}"]["active"] == 0, name
    assert db.docs[f"tasks/{task.id}"]["state"] == TaskState.READY.value


# --- the register ----------------------------------------------------------------


def _row(number: int) -> str:
    text = REQUESTS.read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines() if line.startswith(f"| {number} |")]
    return row


def test_request_135_is_recorded_as_accepted_and_done():
    row = _row(135)
    # MUTATION: drop "accepted by the owner" and check-frozen-contract.sh
    # refuses this pull request's swarm_common diff.
    assert "accepted by the owner" in row.lower()
    assert "DONE" in row


def test_request_134_is_recorded_as_declined():
    row = _row(134)
    assert "DECLINED" in row and "135" in row
