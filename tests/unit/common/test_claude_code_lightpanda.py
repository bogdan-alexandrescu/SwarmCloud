"""Contract request 127: the `claude-code-lightpanda` runner profile (LIGHTPANDA-CONTRACT, part of #2688).

docs/design/lightpanda-runtime.md §5 B (2), §6.1 and §7 lane L1; accepted by
BGD on 2026-10-31 ("go with the recommended options"; accept contract request
127). Lightpanda is a headless browser that runs JavaScript and draws no
pixels: on 2026-10-31 it read the same links as headless Chromium on 16 of 17
real pages at a tenth of the memory. What is held here:

  * `claude-code-lightpanda` exists and is `claude-code` in every field except
    its name and the image `agent-runtime-lightpanda`;
  * it names the `standard` class (1 unit), so its lease takes
    `resource:standard`, never the scarce `resource:browser`;
  * it offers no `browser_tool` input and no live browser: one container, no
    display sidecar, no live view, no take-control;
  * it runs on GKE Autopilot, on-demand only (invariant 6), at the
    catalogue's default checkpoint interval (invariant 8);
  * its rendered GKE pod has memory and ephemeral storage `requests ==
    limits`, and CPU request <= limit <= CPU_BURST_MAX_RATIO x request
    (invariant 7);
  * docs/contract-change-requests.md records request 127 as applied, on a
    line carrying "accepted by the owner", which is what
    scripts/lib/check-frozen-contract.sh admits a `swarm_common` change on.

No cloud, no emulator: the catalogue is pure, and the manifest is rendered by
the scheduler's dispatcher with a stand-in batch API it never calls.
"""

from __future__ import annotations

import dataclasses
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
    CPU_BURST_MAX_RATIO,
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

NAME = "claude-code-lightpanda"
IMAGE = "agent-runtime-lightpanda"


def _lightpanda() -> RunnerProfile:
    # MUTATION: delete the `claude-code-lightpanda` entry from RUNNER_PROFILES
    # and every test that reads it fails here with a KeyError.
    return RUNNER_PROFILES[NAME]


def test_the_profile_is_in_the_catalogue_under_its_own_name():
    # MUTATION: give the entry `name="claude-code"` and the key and the name
    # disagree.
    assert _lightpanda().name == NAME


def test_it_runs_the_lightpanda_image():
    # MUTATION: `image="agent-runtime-browser"` and the agent gets Chromium;
    # `image="agent-runtime-base"` and it gets no browser at all.
    assert _lightpanda().image == IMAGE


def test_it_names_the_standard_class_one_unit():
    # MUTATION: `resource_class="browser"` and every DOM-only agent queues on
    # the 20 browser units the design moves it off.
    assert _lightpanda().resource_class == "standard"
    # MUTATION: a new, smaller class (which the request rules out) would
    # still be one unit; this pins that the standard class is.
    assert RESOURCE_CLASSES["standard"].units == 1


def test_it_offers_no_browser_tool_and_no_live_browser():
    profile = _lightpanda()
    # MUTATION: `inputs=_CLAUDE_CODE_BROWSER_INPUTS` and a caller may pick
    # playwright or dev-browser, which this image does not carry.
    assert "browser_tool" not in profile.inputs
    # MUTATION: `live_browser=True` and the pod renders a display sidecar and
    # the console offers a live view of pixels that are never drawn.
    assert profile.live_browser is False


def test_it_runs_on_gke_autopilot_on_demand():
    profile = _lightpanda()
    # MUTATION: `backend=Backend.CLOUD_RUN_JOB`.
    assert profile.backend is Backend.GKE_AUTOPILOT
    # MUTATION: `backend=Backend.AUTO` resolves to Cloud Run, so this fails.
    assert resolve_backend(profile) is Backend.GKE_AUTOPILOT
    # Invariant 6: no Spot.
    assert profile.spot is SpotStrategy.ON_DEMAND_ONLY


def test_it_checkpoints_at_the_catalogues_default_interval():
    default = RunnerProfile.__dataclass_fields__["checkpoint_interval_seconds"].default
    # MUTATION: `checkpoint_interval_seconds=600` on the entry.
    assert _lightpanda().checkpoint_interval_seconds == default
    # MUTATION: `supports_checkpoint=False` (invariant 8: mandatory).
    assert _lightpanda().supports_checkpoint is True


def test_it_is_claude_code_in_every_other_field():
    expected = dataclasses.replace(RUNNER_PROFILES["claude-code"], name=NAME, image=IMAGE)
    # MUTATION: give the entry its own `inputs`, `secrets`, `runner_argv`,
    # `provider`, `timeout_seconds`, `secret_slots` or model classes and it is
    # no longer claude-code on another image.
    assert _lightpanda() == expected


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


def _cpu(value: str) -> float:
    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


def test_its_gke_pod_holds_invariant_7_in_one_container(tenant):
    dispatcher = GkeJobDispatcher(scheduler_settings(),
                                  target=GkeTarget("https://k8s", "/ca.pem"),
                                  batch_api=object())
    profile = _lightpanda()
    task = dataclasses.replace(make_task(NAME), resource_class=profile.resource_class)
    job = dispatcher._manifest(task=task, lease=make_lease(task), profile=profile,
                               tenant=tenant)
    spec = job["spec"]["template"]["spec"]
    # MUTATION: `live_browser=True` and a `display` sidecar joins the worker.
    assert [c["name"] for c in spec["containers"]] == ["worker"]
    containers = spec["containers"] + spec.get("initContainers", [])
    for container in containers:
        r = container["resources"]
        # MUTATION: render a memory limit above the request and the pod may
        # burst into an OOM kill under node pressure (invariant 7).
        assert r["requests"]["memory"] == r["limits"]["memory"], container["name"]
        if "ephemeral-storage" in r["requests"]:
            assert (r["requests"]["ephemeral-storage"]
                    == r["limits"]["ephemeral-storage"]), container["name"]
        cpu_req, cpu_lim = _cpu(r["requests"]["cpu"]), _cpu(r["limits"]["cpu"])
        assert cpu_req <= cpu_lim <= CPU_BURST_MAX_RATIO * cpu_req, container["name"]
    (worker,) = spec["containers"]
    # MUTATION: `resource_class="browser"` would still read 8Gi; the image
    # check is what tells the two apart, and this pins the standard size.
    assert worker["resources"]["requests"]["memory"] == f"{RESOURCE_CLASSES['standard'].memory_gib}Gi"
    assert worker["image"].rsplit("/", 1)[-1].split("@")[0].split(":")[0] == IMAGE


def test_request_127_is_recorded_as_applied_with_the_owners_acceptance():
    text = REQUESTS.read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines() if line.startswith("| 127 |")]
    # MUTATION: leave row 127 reading "open" and the index says the catalogue
    # lacks a profile it has.
    assert "APPLIED" in row
    assert not row.rstrip().endswith("| open |")
    # MUTATION: drop "accepted by the owner" from the row and
    # check-frozen-contract.sh refuses this pull request's swarm_common diff.
    assert "accepted by the owner" in row.lower()
