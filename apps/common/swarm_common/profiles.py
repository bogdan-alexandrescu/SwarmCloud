"""Resource classes and runner profiles -- the admin-defined execution catalogue.

API callers choose a `runner_profile` by NAME and nothing else. They never supply
an image, a command, a resource spec or a backend. That is the whole point: it is
what stops an authenticated caller from turning the swarm into arbitrary compute.

Sizing note. These numbers are not guesses. They were measured on the reference
workstation on 2026-09-15: one working Claude Code lane was `claude` 1,532 MB +
`pytest` 769 MB + node/tsx guards 207 MB = ~2.5 GiB resident. The classes below
are roughly 2x that, and the worker exports peak RSS and peak disk per profile so
the numbers get corrected from production rather than from arithmetic.

Two hard rules follow from the stability requirement:
  * requests == limits. Bursting past a request is precisely what gets a
    container OOM-killed under node pressure, so we never do it.
  * Spot is disabled. Verified against GKE docs: Spot Pods cannot use Autopilot
    extended run time, so Spot and "no preemption" are mutually exclusive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Backend(str, Enum):
    CLOUD_RUN_JOB = "CLOUD_RUN_JOB"
    GKE_AUTOPILOT = "GKE_AUTOPILOT"
    AUTO = "AUTO"


class SpotStrategy(str, Enum):
    ON_DEMAND_ONLY = "on_demand_only"
    SPOT_PREFERRED = "spot_preferred"
    SPOT_ONLY = "spot_only"


@dataclass(frozen=True)
class ResourceClass:
    """A sizing envelope. `cpu` and `memory_gib` are BOTH request and limit."""

    name: str
    cpu: float
    memory_gib: int
    disk_gib: int
    #: Capacity units consumed from the weighted global budget. Heavier classes
    #: cost more scheduling budget than light ones.
    units: int
    #: Cloud Run ephemeral disk is a Preview feature and, per Google's docs,
    #: disables live migration -- which is why mandatory checkpointing exists.
    requires_preview_disk: bool = False

    def __post_init__(self) -> None:
        if self.cpu <= 0 or self.memory_gib <= 0:
            raise ValueError(f"resource class {self.name}: cpu and memory must be positive")
        if self.cpu > 8 or self.memory_gib > 32:
            # Cloud Run's hard ceiling. Anything above must be GKE-only.
            raise ValueError(f"resource class {self.name} exceeds the Cloud Run Jobs ceiling")


# WORKSPACE SIZES ARE MEMORY, NOT DISK.
#
# These originally claimed 20/40/100 GiB of disk-backed ephemeral storage. That
# is a Cloud Run Preview feature the Terraform google provider cannot express:
# `empty_dir.medium` accepts only "MEMORY". The workspace is therefore a tmpfs
# carved out of the container's memory, so `disk_gib` is a slice OF `memory_gib`
# and not additional capacity.
#
# The measured reference workload -- Claude Code plus a Node/Python toolchain
# running a test suite -- peaks near 2.5 GiB, which leaves standard roughly
# 1.5 GiB of real workspace headroom. A large monorepo with node_modules will
# not fit on `standard`; use `browser` or `large`, or route to GKE.
#
# Upside: this path is fully GA and supports live migration, which the Preview
# disk explicitly does not -- so the reliability requirement that drove the
# Cloud Run choice is better served here than by the feature we set out to use.
RESOURCE_CLASSES: dict[str, ResourceClass] = {
    "standard": ResourceClass("standard", cpu=4, memory_gib=8, disk_gib=4, units=1),
    "browser": ResourceClass("browser", cpu=8, memory_gib=16, disk_gib=8, units=2),
    "large": ResourceClass("large", cpu=8, memory_gib=32, disk_gib=16, units=4),
}


@dataclass(frozen=True)
class RunnerProfile:
    name: str
    image: str
    resource_class: str
    backend: Backend
    command: tuple[str, ...]
    #: Provider whose quota and credentials this runner consumes. None means the
    #: runner needs no external provider, so it works before a tenant registers
    #: any key -- which is what keeps the mock smoke path always available.
    provider: str | None = None
    secrets: tuple[str, ...] = ()
    supports_checkpoint: bool = True
    spot: SpotStrategy = SpotStrategy.ON_DEMAND_ONLY
    timeout_seconds: int = 3600
    #: Seconds between mandatory workspace checkpoints. This is what replaces
    #: live migration on the Cloud Run ephemeral-disk path.
    checkpoint_interval_seconds: int = 120

    def __post_init__(self) -> None:
        if self.resource_class not in RESOURCE_CLASSES:
            raise ValueError(f"runner {self.name}: unknown resource class {self.resource_class}")
        if self.spot is not SpotStrategy.ON_DEMAND_ONLY:
            raise ValueError(
                f"runner {self.name}: Spot is disabled platform-wide; Spot Pods cannot "
                "use extended run time and would violate the no-preemption requirement"
            )


RUNNER_PROFILES: dict[str, RunnerProfile] = {
    "mock": RunnerProfile(
        name="mock",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        command=("python", "-m", "agent_worker.runners.mock"),
        provider=None,          # no key required -- smoke tests must always work
        timeout_seconds=600,
        checkpoint_interval_seconds=30,
    ),
    "generic": RunnerProfile(
        name="generic",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        command=("python", "-m", "agent_worker.runners.generic"),
        provider=None,
    ),
    "claude-code": RunnerProfile(
        name="claude-code",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        command=("python", "-m", "agent_worker.runners.claude_code"),
        provider="anthropic",
        # BOTH are accepted, and a tenant supplies exactly one. Claude Code runs
        # on either metered API access (ANTHROPIC_API_KEY) or a Claude
        # subscription token (CLAUDE_CODE_OAUTH_TOKEN, from
        # `claude setup-token`). Requiring the API key would make a tenant who
        # already pays for a subscription buy metered access on top of it.
        secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        timeout_seconds=7200,
    ),
    "codex": RunnerProfile(
        name="codex",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        command=("python", "-m", "agent_worker.runners.codex"),
        provider="openai",
        secrets=("OPENAI_API_KEY",),
        timeout_seconds=7200,
    ),
    "browser": RunnerProfile(
        name="browser",
        image="agent-runtime-browser",
        resource_class="browser",
        # Chromium under Cloud Run needs a large /dev/shm; GKE gives us direct
        # control over that, so browser work stays on Autopilot.
        backend=Backend.GKE_AUTOPILOT,
        command=("python", "-m", "agent_worker.runners.browser"),
        provider="anthropic",
        secrets=("ANTHROPIC_API_KEY",),
        timeout_seconds=5400,
    ),
}


def resolve_backend(profile: RunnerProfile) -> Backend:
    """Resolve AUTO to a concrete backend.

    Cloud Run Jobs is preferred for everything it can hold, because it has no
    nodes, no autoscaler and no node upgrades -- far fewer ways for the platform
    to kill a task. GKE Autopilot takes what does not fit.
    """
    if profile.backend is not Backend.AUTO:
        return profile.backend
    rc = RESOURCE_CLASSES[profile.resource_class]
    if rc.cpu <= 8 and rc.memory_gib <= 32:
        return Backend.CLOUD_RUN_JOB
    return Backend.GKE_AUTOPILOT
