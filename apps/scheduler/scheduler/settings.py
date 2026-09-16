"""Scheduler process settings.

The frozen `Settings` holds the platform-wide values. What is here is specific
to one drain loop: how many tasks to look at per pass, when to stop, and how
starvation aging is shaped.

The drain loop is BOUNDED in three independent ways, because a scheduler that
runs forever on a Pub/Sub push is a scheduler that gets killed mid-transaction:
a wall-clock budget, a maximum number of admissions, and a maximum number of
passes. Whichever trips first ends the run, and the next push (or the 1-minute
safety tick) picks up where it left off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from swarm_common.config import Settings


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SchedulerSettings:
    core: Settings

    #: READY tasks read per pass. Large enough that round-robin sees several
    #: tenants, small enough that one Firestore query stays cheap.
    candidate_batch_size: int = 200

    #: Hard stops on the drain loop.
    max_leases_per_run: int = 200
    max_passes_per_run: int = 25
    max_run_seconds: float = 45.0

    #: Starvation aging. A task's effective priority rises by `aging_step` for
    #: every `aging_interval_seconds` it has waited, capped at `aging_max_bonus`.
    #: The cap is what stops aging from inverting the priority scheme entirely:
    #: an old low-priority task should overtake a fresh one, not outrank an
    #: urgent one forever.
    aging_interval_seconds: int = 60
    aging_step: int = 1
    aging_max_bonus: int = 50

    #: Dependency promotion sweep: PARKED(DEPENDENCY_INCOMPLETE) tasks examined
    #: per run. Bounded for the same reason as the candidate batch.
    dependency_sweep_size: int = 200

    #: Prewarm: PARKED tasks whose cooldown expires within the lead window are
    #: promoted to READY early, so they are queued the instant capacity appears.
    #: READY costs nothing (invariant 1), so this is a latency win with no spend.
    enable_prewarm: bool = True

    #: GKE dispatch target (browser/GPU profiles only).
    gke_cluster: str = ""
    gke_location: str = ""

    project_id: str = ""
    region: str = "us-central1"
    artifact_registry_host: str = ""
    worker_image_tag: str = "latest"
    dispatch_topic: str = ""

    @classmethod
    def from_env(cls) -> "SchedulerSettings":
        core = Settings.from_env()
        registry = os.environ.get(
            "ARTIFACT_REGISTRY_HOST",
            f"{core.region}-docker.pkg.dev/{core.project_id}/{core.artifact_registry}",
        )
        return cls(
            core=core,
            candidate_batch_size=_int("CANDIDATE_BATCH_SIZE", 200),
            max_leases_per_run=_int("MAX_LEASES_PER_RUN", 200),
            max_passes_per_run=_int("MAX_PASSES_PER_RUN", 25),
            max_run_seconds=_float("MAX_RUN_SECONDS", 45.0),
            aging_interval_seconds=_int("AGING_INTERVAL_SECONDS", 60),
            aging_step=_int("AGING_STEP", 1),
            aging_max_bonus=_int("AGING_MAX_BONUS", 50),
            dependency_sweep_size=_int("DEPENDENCY_SWEEP_SIZE", 200),
            enable_prewarm=_bool("ENABLE_QUOTA_PREWARM", core.enable_quota_prewarm),
            gke_cluster=os.environ.get("GKE_CLUSTER", ""),
            gke_location=os.environ.get("GKE_LOCATION", core.region),
            project_id=core.project_id,
            region=core.region,
            artifact_registry_host=registry,
            worker_image_tag=os.environ.get("WORKER_IMAGE_TAG", "latest"),
            dispatch_topic=os.environ.get("DISPATCH_TOPIC", ""),
        )

    def __post_init__(self) -> None:
        if self.candidate_batch_size <= 0:
            raise ValueError("candidate_batch_size must be positive")
        if self.max_leases_per_run <= 0 or self.max_passes_per_run <= 0:
            raise ValueError("the drain loop must be bounded by a positive number of passes")
        if self.max_run_seconds <= 0:
            raise ValueError("max_run_seconds must be positive")
        if self.aging_interval_seconds <= 0:
            raise ValueError("aging_interval_seconds must be positive")
        if self.aging_max_bonus < 0:
            raise ValueError("aging_max_bonus cannot be negative")
