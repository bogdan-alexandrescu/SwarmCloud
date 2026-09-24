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

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from swarm_common.config import Settings

#: `<registry path>@sha256:<64 hex>`, and nothing before the `@` that could be a
#: tag. The same shape terraform/infra/variables.tf validates `image_refs`
#: against: the runtime ignores a tag when a digest is present, so a tag there
#: is only a label that can disagree with what runs.
_DIGEST_REF = re.compile(r"^[^@:\s]+@sha256:[0-9a-f]{64}$")


def check_worker_image_refs(refs: object) -> None:
    """Refuse a worker image map that is not digest-pinned, naming the entry.

    A scheduler configured with a tag must fail when it STARTS, not at its
    first dispatch -- by then admission has taken a lease, and the failure
    surfaces as a task stuck behind a pull of the wrong thing.
    """
    if not isinstance(refs, dict):
        raise ValueError(
            f"WORKER_IMAGE_REFS must be a JSON object of image name -> digest ref, "
            f"got {type(refs).__name__}"
        )
    for name, ref in refs.items():
        if not isinstance(name, str) or not isinstance(ref, str) or not _DIGEST_REF.match(ref):
            raise ValueError(
                f"WORKER_IMAGE_REFS[{name!r}] = {ref!r} is not pinned by digest; every "
                "value must be `<registry>/<image>@sha256:<64 hex>` with no tag"
            )
        if not ref.split("@", 1)[0].endswith(f"/{name}"):
            raise ValueError(
                f"WORKER_IMAGE_REFS[{name!r}] = {ref!r} is the digest of a different "
                "image; each key must be the image's own name"
            )


def parse_worker_image_refs(raw: str) -> dict[str, str]:
    """WORKER_IMAGE_REFS -> {image name: digest ref}. Empty means none."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        refs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"WORKER_IMAGE_REFS is not valid JSON: {exc}") from exc
    check_worker_image_refs(refs)
    return dict(refs)


def parse_enforced_since(raw: str | None) -> datetime | None:
    """ON_STEP_FAILURE_ENFORCED_SINCE -> an aware datetime, or None when empty.

    An offset is REQUIRED. A naive time would be read in whatever zone the
    process runs in, and a cutoff that is hours off either cancels work it
    should have spared or spares work it should have cancelled. So a value
    without one refuses to start, as a malformed value does.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"ON_STEP_FAILURE_ENFORCED_SINCE must be an ISO-8601 timestamp with a "
            f"UTC offset, such as 2026-09-25T14:00:00Z; got {raw!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"ON_STEP_FAILURE_ENFORCED_SINCE={raw!r} has no UTC offset; write it as "
            "2026-09-25T14:00:00Z or with an explicit +HH:MM"
        )
    return parsed


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

    #: `on_step_failure: fail_workflow` applies only to workflows whose document
    #: `created_at` is at or after this instant. From ON_STEP_FAILURE_ENFORCED_SINCE,
    #: an ISO-8601 timestamp with a UTC offset.
    #:
    #: WHY IT EXISTS. Until the scheduler read the field, every workflow was
    #: stored with `fail_workflow` (the API default), and docs/workflows.md told
    #: the submitter the setting was not honoured and behaved like `continue`.
    #: Applying the rule to such a workflow cancels steps nobody was told would
    #: be cancelled. A cancel cannot be undone, and a cancelled step's checkpoint
    #: becomes reclaimable. Workflows created before the cutoff keep the rule
    #: they were submitted under: only the dependents of a failed step are
    #: cancelled.
    #:
    #: WHY THE DEFAULT IS None. None means every workflow, retroactively, which
    #: is the owner's 2026-09-24 decision as it was written. Whether in-flight
    #: workflows should be exempt was not part of that decision and is open.
    #: This setting is how the other answer is given without a code change.
    #: `python -m scheduler.on_step_failure_audit` lists, read-only, what the
    #: next drain would cancel under either answer.
    on_step_failure_enforced_since: datetime | None = None

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
    #: Tag for the agent runtime images, used ONLY when `worker_image_refs` is
    #: empty -- which a terraform-managed deployment never is. It remains for
    #: the local emulator loop, which has no promotion manifest to pin from.
    #: "latest" is not pushed by scripts/build-images.sh, so a deployed
    #: scheduler that somehow lost its digest map fails loudly on a 404 rather
    #: than quietly running whatever a tag points at.
    worker_image_tag: str = "latest"
    #: Runner image name -> `<registry>/<name>@sha256:<64 hex>`, from
    #: WORKER_IMAGE_REFS (terraform/infra/locals.tf writes it from the
    #: promotion manifest). When non-empty it is the ONLY source of a worker
    #: image: a tag is resolved when the image is pulled, so what an agent ran
    #: used to be whatever the tag pointed at on that node at that moment.
    #: Excluded from the hash: a dict is unhashable and the settings are frozen.
    worker_image_refs: dict[str, str] = field(default_factory=dict, hash=False)
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
            on_step_failure_enforced_since=parse_enforced_since(
                os.environ.get("ON_STEP_FAILURE_ENFORCED_SINCE")
            ),
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
            worker_image_refs=parse_worker_image_refs(os.environ.get("WORKER_IMAGE_REFS", "")),
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
        cutoff = self.on_step_failure_enforced_since
        if cutoff is not None and (cutoff.tzinfo is None or cutoff.utcoffset() is None):
            raise ValueError(
                "on_step_failure_enforced_since must be timezone-aware; a naive "
                "cutoff would be read in whatever zone the process runs in"
            )
        # Checked here as well as when parsed, so a settings object built
        # directly -- as every test does -- cannot carry a tag either.
        check_worker_image_refs(self.worker_image_refs)
