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

    #: Fairness top-up. The global candidate query returns the highest-priority
    #: slice, so a tenant with more than `candidate_batch_size` high-priority
    #: tasks queued can fill it entirely and every other tenant disappears from
    #: the rotation -- round-robin cannot interleave what it never saw. When the
    #: slice comes back FULL (and only then, because a short slice is already
    #: the whole queue), the scheduler asks each unrepresented tenant for a few
    #: of its own READY tasks and adds them to the rotation.
    tenant_topup_candidates: int = 8
    #: Ceiling on how many tenants that top-up may query in one pass, so the
    #: cost of a pass stays bounded however many tenants exist.
    max_topup_tenants: int = 50

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

    #: Kubernetes service account the worker pod runs as, inside the tenant's own
    #: namespace. It must match the KSA the Workload Identity binding was issued
    #: for, or the pod gets no Google identity: terraform's tenancy module binds
    #: `<pool>[<namespace>/<ksa_name>]` with `ksa_name` defaulting to
    #: `swarm-agent-worker`. The namespace is what makes the binding per-tenant,
    #: so one shared spelling here is correct and is not a cross-tenant hole.
    worker_ksa_name: str = "swarm-agent-worker"

    #: Base URL of the quota broker, passed THROUGH to every worker as
    #: QUOTA_BROKER_URL. The scheduler never calls it; it is the only component
    #: that writes a worker's execution environment, so it is the only place
    #: this can be handed over.
    #:
    #: Without it the account pool is inert in production and nothing says so:
    #: `WorkerConfig.quota_broker_url` stays None, `Worker.__init__` builds no
    #: AccountBroker, and `_lease_account` returns None on its first branch for
    #: every task ever dispatched. The feature was fully implemented, fully
    #: tested -- every worker test injects `WorkerDeps.account_broker`, which is
    #: exactly what hid it -- and reached zero agents.
    #:
    #: Empty means "this deployment has no pool", which is the documented
    #: backwards-compatible default and not a guess at a URL.
    quota_broker_url: str = ""
    #: OIDC audience the worker asks the metadata server for. Cloud Run checks
    #: `aud` against the service URL unless the service declares a custom
    #: audience -- and this one does (`custom_audiences` in terraform, which is
    #: how the broker's own `BROKER_AUDIENCE` check is satisfied), so the URL
    #: alone would mint a token the broker rejects. Empty falls back to the URL,
    #: which is correct for a deployment with no custom audience.
    quota_broker_audience: str = ""

    project_id: str = ""
    region: str = "us-central1"
    artifact_registry_host: str = ""
    #: Tag for the agent runtime images. Defaults to the same immutable tag the
    #: control plane itself was deployed with, because "latest" is not pushed by
    #: scripts/build-images.sh -- every image carries a git SHA. Dispatch failed
    #: with `404 Image '...agent-runtime-base:latest' not found` on every task
    #: until this was wired, and the spec requires immutable tags in prod anyway.
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
            tenant_topup_candidates=_int("TENANT_TOPUP_CANDIDATES", 8),
            max_topup_tenants=_int("MAX_TOPUP_TENANTS", 50),
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
            worker_ksa_name=os.environ.get("WORKER_KSA_NAME", "").strip()
            or "swarm-agent-worker",
            quota_broker_url=os.environ.get("QUOTA_BROKER_URL", "").strip(),
            quota_broker_audience=os.environ.get("QUOTA_BROKER_AUDIENCE", "").strip(),
            project_id=core.project_id,
            region=core.region,
            artifact_registry_host=registry,
            worker_image_tag=(
                os.environ.get("WORKER_IMAGE_TAG")
                or os.environ.get("IMAGE_TAG")
                or "latest"
            ),
            dispatch_topic=os.environ.get("DISPATCH_TOPIC", ""),
        )

    def __post_init__(self) -> None:
        if self.candidate_batch_size <= 0:
            raise ValueError("candidate_batch_size must be positive")
        if self.tenant_topup_candidates < 0:
            raise ValueError("tenant_topup_candidates cannot be negative")
        if self.max_topup_tenants < 0:
            raise ValueError("max_topup_tenants cannot be negative")
        if self.max_leases_per_run <= 0 or self.max_passes_per_run <= 0:
            raise ValueError("the drain loop must be bounded by a positive number of passes")
        if self.max_run_seconds <= 0:
            raise ValueError("max_run_seconds must be positive")
        if self.aging_interval_seconds <= 0:
            raise ValueError("aging_interval_seconds must be positive")
        if self.aging_max_bonus < 0:
            raise ValueError("aging_max_bonus cannot be negative")
