"""Contract request 113: the `claude-code-large` runner profile (AU9-PROFILE-LARGE, part of #1933).

docs/design/agent-autonomy.md §3.11 and §6 row AU-9; accepted by the owner on
2026-10-22 (Q6: "Accept contract request 113: a claude-code-large profile").
On 2026-10-22 #1926 measured 8 GiB out-of-memory kills on `claude-code` and
three agents died at its 7,200 s timeout, and no profile could be named that
gave an agent more of either. What is held here:

  * `claude-code-large` exists and is `claude-code` in every field except its
    name, the `large` class (8 CPU requested, 32 GiB) and 14,400 s;
  * it runs on GKE Autopilot, on-demand only (invariant 6), at the catalogue's
    default checkpoint interval (invariant 8);
  * its rendered GKE pod has memory `requests == limits` at 32 GiB
    (invariant 7), and admission charges it the `large` class's 4 units;
  * every other profile is unchanged: the same names, and each one's class,
    backend, timeout, checkpoint interval and availability;
  * docs/contract-change-requests.md records request 113 as applied, on a line
    carrying "accepted by the owner", which is what
    scripts/lib/check-frozen-contract.sh admits a `swarm_common` change on.

No cloud, no emulator: the catalogue is pure, and the manifest is rendered by
the scheduler's dispatcher with a stand-in batch API it never calls.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import PROJECT, scheduler_settings  # noqa: E402
from control_plane.test_dispatch_manifests import NOW, make_lease, make_task  # noqa: E402

from swarm_common.models import Tenant  # noqa: E402
from swarm_common.profiles import (  # noqa: E402
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    Backend,
    RunnerProfile,
    SpotStrategy,
    resolve_backend,
)

from scheduler.dispatch import GkeJobDispatcher, GkeTarget  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
REQUESTS = REPO / "docs" / "contract-change-requests.md"
RUNNER_SIZES = REPO / "docs" / "design" / "runner-sizes.md"

NAME = "claude-code-large"

#: The catalogue as it stood on main before request 113 (ea899e1), per
#: profile: (class, backend, timeout_seconds, checkpoint_interval_seconds,
#: available). Request 113 adds one key and changes none of these.
BEFORE = {
    "mock": ("standard", Backend.CLOUD_RUN_JOB, 600, 30, True),
    "generic": ("standard", Backend.GKE_AUTOPILOT, 3600, 120, True),
    "claude-code": ("standard", Backend.GKE_AUTOPILOT, 7200, 120, True),
    "codex": ("standard", Backend.GKE_AUTOPILOT, 7200, 120, False),
    "browser": ("browser", Backend.GKE_AUTOPILOT, 5400, 120, True),
    "merge": ("standard", Backend.GKE_AUTOPILOT, 600, 120, True),
    "post-verdict": ("standard", Backend.CLOUD_RUN_JOB, 300, 120, False),
    "claude-code-review": ("standard", Backend.CLOUD_RUN_JOB, 7200, 120, False),
    "indexer": ("standard", Backend.GKE_AUTOPILOT, 7200, 120, True),
    "claude-code-browser": ("browser", Backend.GKE_AUTOPILOT, 7200, 120, True),
    "await-input": ("standard", Backend.CLOUD_RUN_JOB, 300, 120, True),
}


def _large() -> RunnerProfile:
    # MUTATION: delete the `claude-code-large` entry from RUNNER_PROFILES and
    # every test that reads it fails here with a KeyError.
    return RUNNER_PROFILES[NAME]


def test_the_profile_is_in_the_catalogue_under_its_own_name():
    # MUTATION: give the entry `name="claude-code"` and the key and the name
    # disagree.
    assert _large().name == NAME


def test_it_names_the_large_class():
    # MUTATION: `resource_class="standard"` and the agent keeps 8 GiB.
    assert _large().resource_class == "large"
    rc = RESOURCE_CLASSES["large"]
    # MUTATION: shrink the `large` class (a second contract change this lane
    # does not make) and the 32 GiB the owner accepted is gone.
    assert (rc.cpu, rc.memory_gib, rc.units) == (8, 32, 4)


def test_it_times_out_at_four_hours():
    # MUTATION: leave `timeout_seconds` at claude-code's 7200.
    assert _large().timeout_seconds == 14_400


def test_it_runs_on_gke_autopilot_on_demand():
    profile = _large()
    # MUTATION: `backend=Backend.CLOUD_RUN_JOB`.
    assert profile.backend is Backend.GKE_AUTOPILOT
    # MUTATION: `backend=Backend.AUTO` resolves to Cloud Run (every class fits
    # Cloud Run's ceiling), so this fails.
    assert resolve_backend(profile) is Backend.GKE_AUTOPILOT
    # MUTATION: any other SpotStrategy is refused by RunnerProfile itself;
    # this pins that the entry does not even try (invariant 6).
    assert profile.spot is SpotStrategy.ON_DEMAND_ONLY


def test_it_checkpoints_at_the_catalogues_default_interval():
    default = RunnerProfile.__dataclass_fields__["checkpoint_interval_seconds"].default
    # MUTATION: `checkpoint_interval_seconds=600` on the entry: a longer
    # interval loses more of a four-hour attempt to an interruption.
    assert _large().checkpoint_interval_seconds == default
    # MUTATION: `supports_checkpoint=False` (invariant 8: mandatory).
    assert _large().supports_checkpoint is True


def test_it_is_claude_code_in_every_other_field():
    expected = dataclasses.replace(
        RUNNER_PROFILES["claude-code"],
        name=NAME,
        resource_class="large",
        timeout_seconds=14_400,
    )
    # MUTATION: give the entry its own `inputs`, `secrets`, `runner_argv`,
    # `image`, `provider`, `secret_slots` or model classes and it is no
    # longer the same agent made bigger.
    assert _large() == expected


@pytest.mark.parametrize("name", sorted(BEFORE))
def test_every_other_profile_is_unchanged(name):
    p = RUNNER_PROFILES[name]
    # MUTATION: move `claude-code` to `large` instead of adding a profile and
    # every standard agent pays for 32 GiB.
    assert (p.resource_class, p.backend, p.timeout_seconds,
            p.checkpoint_interval_seconds, p.available) == BEFORE[name]


def test_the_catalogue_gains_exactly_one_profile():
    # MUTATION: add a second entry (or drop one) and the names differ.
    # fix-loop came later, by contract request 124 (FL-CONTRACT), and
    # claude-code-lightpanda by contract request 127 (LIGHTPANDA-CONTRACT).
    assert set(RUNNER_PROFILES) == set(BEFORE) | {NAME} | {"fix-loop", "claude-code-lightpanda"}


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


def test_its_gke_pod_holds_32_gib_with_memory_requests_equal_to_limits(tenant):
    dispatcher = GkeJobDispatcher(scheduler_settings(),
                                  target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    task = dataclasses.replace(make_task(NAME), resource_class=_large().resource_class)
    job = dispatcher._manifest(task=task, lease=make_lease(task), profile=_large(),
                               tenant=tenant)
    spec = job["spec"]["template"]["spec"]
    containers = spec["containers"] + spec.get("initContainers", [])
    assert containers, "the manifest rendered no container"
    for container in containers:
        r = container["resources"]
        # MUTATION: render a memory limit above the request in
        # GkeJobDispatcher._manifest and the pod may burst into an OOM kill
        # under node pressure (invariant 7).
        assert r["requests"]["memory"] == r["limits"]["memory"], container["name"]
    (worker,) = [c for c in spec["containers"] if c["name"] == "worker"]
    # MUTATION: `resource_class="standard"` on the entry and this reads 8Gi.
    assert worker["resources"]["requests"]["memory"] == "32Gi"
    # MUTATION: drop `safe-to-evict: "false"` and Autopilot may consolidate a
    # four-hour agent away mid-run.
    assert job["spec"]["template"]["metadata"]["annotations"][
        "cluster-autoscaler.kubernetes.io/safe-to-evict"] == "false"


def test_request_113_is_recorded_as_applied_with_the_owners_acceptance():
    text = REQUESTS.read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines() if line.startswith("| 113 |")]
    # MUTATION: leave row 113 reading "Not yet applied" and the index says the
    # catalogue lacks a profile it has.
    assert "APPLIED by the pull request that adds this line" in row
    assert "Not yet applied" not in row
    # MUTATION: drop "accepted by the owner" from the row and
    # check-frozen-contract.sh refuses this pull request's swarm_common diff.
    assert "accepted by the owner" in row.lower()


def test_runner_sizes_lists_the_profile_on_gke_at_the_large_class():
    text = RUNNER_SIZES.read_text(encoding="utf-8")
    section = text.split("### 1a.3 Which profiles run where", 1)[1].split("### 1a.4", 1)[0]
    (gke,) = [line for line in section.splitlines() if line.startswith("| GKE Autopilot |")]
    # MUTATION: leave §1a.3's GKE row without the profile and the doc that
    # says which profile runs where is wrong about one of them.
    assert re.search(r"`claude-code-large` \(large\b", gke), gke
