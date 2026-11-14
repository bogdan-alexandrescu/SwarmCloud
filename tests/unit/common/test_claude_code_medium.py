"""Contract request 147: the `medium` resource class and `claude-code-medium` (part of #2688).

Accepted by the owner on 6 November 2026, after the 7-day load
analysis: 771 large attempts (4.8% of 16,103) held 46% of all capacity
unit-hours and used about a third of the CPU and memory they reserved;
measured without page cache, 96% fit 6 CPU and 16 GiB and only 31 needed
`large`. What is held here:

  * `medium` is 6 CPU requested (12 limit), 16 GiB, and costs 2 units, the
    number `units_for` derives; its defaults pass every frozen bound, and it
    sits between `browser` and `large` in `CLASS_ORDER`;
  * `claude-code-medium` is `claude-code` in every field except its name and
    the `medium` class, on GKE Autopilot on-demand (invariant 6);
  * its GKE pod has memory and ephemeral storage `requests == limits`, CPU
    request <= limit <= CPU_BURST_MAX_RATIO x request (invariant 7);
  * a claude-code-medium task is admitted against `resource:medium` and
    `runner:claude-code-medium`, all or nothing, at 2 units per compute pool;
  * docs/contract-change-requests.md records request 147 as accepted by the
    owner, which is what scripts/lib/check-frozen-contract.sh admits on.

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

from control_plane.conftest import PROJECT, scheduler_settings  # noqa: E402
from control_plane.test_dispatch_manifests import NOW, make_lease, make_task  # noqa: E402

from swarm_common import profiles  # noqa: E402
from swarm_common.admission import AdmissionConfig, acquire_lease_in_transaction  # noqa: E402
from swarm_common.models import Task, Tenant, pool_names_for  # noqa: E402
from swarm_common.profiles import (  # noqa: E402
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    Backend,
    SpotStrategy,
    check_size,
    default_sizes,
    resolve_backend,
    units_for,
)
from swarm_common.states import TaskState  # noqa: E402

from scheduler import sizes  # noqa: E402
from scheduler.dispatch import GkeJobDispatcher, GkeTarget  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
REQUESTS = REPO / "docs" / "contract-change-requests.md"

NAME = "claude-code-medium"
TENANT = "eng"
BACKEND = "GKE_AUTOPILOT"


def test_the_medium_class_is_6_cpu_16_gib_at_2_units():
    # MUTATION: delete `medium` from RESOURCE_CLASSES and this is a KeyError.
    rc = RESOURCE_CLASSES["medium"]
    assert (rc.name, rc.cpu, rc.memory_gib) == ("medium", 6, 16)
    # MUTATION: `units=4` and the import-time check refuses the module;
    # `units=1` and admission undercharges a 6-CPU pod.
    assert rc.units == 2 == units_for(rc.cpu, rc.memory_gib)
    # Invariant 7: CPU request <= limit <= the burst ratio x the request.
    assert rc.cpu <= rc.cpu_limit_cores <= profiles.CPU_BURST_MAX_RATIO * rc.cpu
    assert rc.cpu_limit_cores == 12
    # The workspace tmpfs is a slice of memory, and leaves 2 GiB to the agent.
    assert 1 <= rc.disk_gib <= rc.memory_gib - 2


def test_medium_sits_between_browser_and_large_and_its_defaults_pass_every_bound():
    # MUTATION: append `medium` after `large` and check_size refuses the
    # defaults: large's 8 CPU and 32 GiB are above the "larger" medium's.
    assert profiles.CLASS_ORDER == ("standard", "browser", "medium", "builder", "large")
    # MUTATION: give medium 40 GiB, or a memory/CPU ratio Autopilot would
    # raise, and a refusal names it.
    assert check_size(default_sizes()) == []
    assert default_sizes()["medium"].units == 2


def _medium() -> Any:
    return RUNNER_PROFILES[NAME]


def test_the_profile_is_claude_code_at_the_medium_class():
    claude = RUNNER_PROFILES["claude-code"]
    # Every input but pull_request, the owner's pick on #3470 for claude-code,
    # codex and claude-code-review alone (as claude-code-large).
    expected = dataclasses.replace(
        claude,
        name=NAME,
        resource_class="medium",
        inputs={k: v for k, v in claude.inputs.items() if k != "pull_request"},
        # Contract request 135: only claude-code's build stages run `builder`;
        # a profile picked by name for its size keeps that size at every stage.
        build_resource_class=None,
    )
    # MUTATION: a different timeout, image, input or secret on the entry and it
    # is no longer claude-code made bigger.
    assert _medium() == expected


def test_it_runs_on_gke_autopilot_on_demand_and_checkpoints():
    profile = _medium()
    assert profile.backend is Backend.GKE_AUTOPILOT
    assert resolve_backend(profile) is Backend.GKE_AUTOPILOT
    assert profile.spot is SpotStrategy.ON_DEMAND_ONLY
    assert profile.supports_checkpoint is True


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


def test_its_gke_pod_requests_16_gib_with_requests_equal_to_limits(tenant):
    dispatcher = GkeJobDispatcher(scheduler_settings(),
                                  target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    task = dataclasses.replace(make_task(NAME), resource_class="medium")
    # MUTATION: no `medium` row in dispatch.GKE_DISK and the render refuses.
    job = dispatcher._manifest(task=task, lease=make_lease(task), profile=_medium(), tenant=tenant)
    spec = job["spec"]["template"]["spec"]
    for container in spec["containers"] + spec.get("initContainers", []):
        r = container["resources"]
        assert r["requests"]["memory"] == r["limits"]["memory"], container["name"]
        if "ephemeral-storage" in r["requests"]:
            assert r["requests"]["ephemeral-storage"] == r["limits"]["ephemeral-storage"]
    (worker,) = [c for c in spec["containers"] if c["name"] == "worker"]
    assert worker["resources"]["requests"]["memory"] == "16Gi"
    assert _cores(worker["resources"]["requests"]["cpu"]) == 6
    assert _cores(worker["resources"]["limits"]["cpu"]) == 12


# --- admission against the medium pool ---------------------------------------


class _Snap:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class _Ref:
    def __init__(self, path: str) -> None:
        self.path = path


class _Collection:
    def __init__(self, name: str) -> None:
        self._name = name

    def document(self, doc_id: str) -> _Ref:
        return _Ref(f"{self._name}/{doc_id}")


class _Db:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs

    def collection(self, name: str) -> _Collection:
        return _Collection(name)


class _Txn:
    def __init__(self, db: _Db) -> None:
        self._db = db
        self._written = False

    def get(self, ref: _Ref) -> _Snap:
        assert not self._written, "a read after a write: Firestore raises ReadAfterWriteError"
        return _Snap(self._db.docs.get(ref.path))

    def set(self, ref: _Ref, data: dict[str, Any]) -> None:
        self._written = True
        self._db.docs[ref.path] = dict(data)

    def update(self, ref: _Ref, data: dict[str, Any]) -> None:
        self._written = True
        self._db.docs.setdefault(ref.path, {}).update(data)


ADMIT_AT = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)


def _task() -> Task:
    profile = _medium()
    return Task(
        id="task_medium",
        tenant_id=TENANT,
        created_at=ADMIT_AT,
        updated_at=ADMIT_AT,
        state=TaskState.READY,
        runner_profile=NAME,
        resource_class=profile.resource_class,
        provider=profile.provider,
        input={},
        submitted_by="alice@example.com",
    )


def test_a_claude_code_medium_task_is_admitted_against_the_medium_pool():
    task = _task()
    required = pool_names_for(tenant_id=TENANT, provider=task.provider,
                              resource_class=task.resource_class,
                              runner_profile=task.runner_profile, backend=BACKEND)
    # MUTATION: leave the profile on `large` and it acquires resource:large.
    assert "resource:medium" in required and f"runner:{NAME}" in required
    assert "resource:large" not in required
    docs: dict[str, dict[str, Any]] = {
        f"pools/{name}": {"hard_limit": 10, "active": 0, "enabled": True} for name in required
    }
    docs[f"tasks/{task.id}"] = {"state": TaskState.READY.value, "current_generation": 0}
    db = _Db(docs)
    # The units admission charges come from the scheduler's sizes module.
    sized = sizes.for_task(sizes.DEFAULTS, task, backend=Backend.GKE_AUTOPILOT, live=False)
    assert (sized.resource_class, sized.size.units) == ("medium", 2)
    acquire_lease_in_transaction(_Txn(db), db=db, task=task, units=sized.size.units,
                                 backend=BACKEND, config=AdmissionConfig(), now=ADMIT_AT)
    for pool in ("global", f"tenant:{TENANT}", "resource:medium", f"runner:{NAME}", f"backend:{BACKEND}"):
        assert db.docs[f"pools/{pool}"]["active"] == 2, pool


def test_request_147_is_recorded_as_accepted_by_the_owner():
    text = REQUESTS.read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines() if line.startswith("| 147 |")]
    assert "claude-code-medium" in row and "`medium`" in row
    # MUTATION: drop "accepted by the owner" and check-frozen-contract.sh
    # refuses this pull request's swarm_common diff.
    assert "accepted by the owner" in row.lower()
