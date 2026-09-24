"""Reconciler settings.

Thresholds live here rather than in Firestore because they describe how long the
reconciler waits before calling something dead, and a reconciler that could be
reconfigured to act instantly would be a way to cause the exact duplicate
execution it exists to prevent.
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
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ReconcilerConfig:
    project_id: str
    region: str
    firestore_database: str

    #: A lease whose heartbeat is older than this is presumed dead. It must be
    #: comfortably longer than the worker's heartbeat interval, or a healthy
    #: worker gets reaped between two beats.
    lease_timeout_seconds: int = 120
    heartbeat_grace_seconds: int = 90
    #: How long a persisted reconciliation pass is kept. A pass runs every
    #: minute, so this is 10,080 documents a week at the default; the Firestore
    #: TTL policy on `expires_at` removes them. Long enough to investigate an
    #: incident from last week, short enough not to become a data set.
    pass_retention_hours: int = 168

    #: How long a task may sit in DISPATCHED/STARTING with no backend execution
    #: before the execution is presumed never to have been created. Image pulls
    #: on a cold Cloud Run Job are the slow case this must accommodate.
    missing_execution_grace_seconds: int = 300

    #: How long an execution may run with no matching live lease before it is
    #: treated as an orphan. Short, because an orphan is billed compute nobody
    #: is accounting for.
    orphan_execution_grace_seconds: int = 120

    #: A per-tenant-per-profile Cloud Run Job resource with no execution in this
    #: long is garbage-collected. Job resources are free, but thousands of them
    #: make every list call slower and the console unusable.
    unused_job_ttl_seconds: int = 7 * 24 * 3600
    empty_namespace_ttl_seconds: int = 24 * 3600

    max_findings_per_pass: int = 200
    dry_run: bool = False
    enable_gke: bool = True
    enable_cloud_run: bool = True
    enable_gc: bool = True

    # -- checkpoint retention ------------------------------------------------
    #: The artifact bucket. Empty disables checkpoint collection entirely,
    #: which is what happens in any environment that has not configured one --
    #: a collector that cannot see the bucket must do nothing, not assume.
    artifact_bucket: str = ""
    enable_checkpoint_gc: bool = True

    #: How long an object whose referent cannot be found is kept before the
    #: backstop takes it. This window is NOT a retention policy -- a checkpoint
    #: with a live reference is kept for ever, however old. It applies only to
    #: an object whose task or attempt document is gone, which nothing in this
    #: platform can resume from.
    #:
    #: Seven days. The window exists to absorb a control-plane read that is
    #: stale or wrong, not to bound storage: a pass runs every five minutes, so
    #: a deletion here means roughly two thousand consecutive passes all agreed
    #: the referent no longer exists. It is also deliberately shorter than
    #: `artifact_retention_days` (14 in dev.tfvars, 90 by default) so that this
    #: collector, and not the bucket's blind lifecycle rule, is what usually
    #: removes an orphan.
    checkpoint_orphan_backstop_seconds: int = 7 * 24 * 3600

    #: Objects examined per sweep. A full-bucket listing is the expensive part
    #: of this pass, and an unbounded one would grow with the platform until it
    #: outran the request timeout -- at which point the sweep stops happening at
    #: all, silently, which is worse than sweeping slowly.
    checkpoint_scan_limit: int = 20000

    #: The sweep lists the whole tenants/ prefix, so it runs on its own, slower
    #: clock than the five-minute reconciliation tick. Hourly: a checkpoint that
    #: became collectable a moment ago costs pennies for another hour, whereas
    #: twelve full-bucket listings an hour is a bill.
    checkpoint_sweep_interval_seconds: int = 3600

    #: Label every resource this platform owns; GC refuses to touch anything
    #: without it, which is what keeps a shared project safe.
    managed_label_key: str = "managed-by"
    managed_label_value: str = "swarm"
    #: Cloud Run Job RESOURCE names, which the dispatcher builds as
    #: `sanitize_name("swarm", ...)`. Unrelated to the namespace below despite
    #: the identical spelling -- do not collapse the two.
    job_name_prefix: str = "swarm-"
    #: THE KUBERNETES NAMESPACE PREFIX, and it must equal the scheduler's.
    #:
    #: This read `"swarm-"` while `apps/scheduler/scheduler/dispatch.py` has
    #: dispatched into `swarm-tenant-<id>` all along, which is the 2026-09-23
    #: outage (kubernetes/render.py's NAMESPACE_PREFIX carries the full
    #: diagnosis) surviving in a second service after the first was fixed.
    #:
    #: It is NOT harmless here just because `"swarm-tenant-eng".startswith(
    #: "swarm-")` is true. `GkeBackend` slices the prefix off to recover the
    #: tenant id whenever the `swarm-tenant` label is absent --
    #: `namespace[len(self._prefix):]` in backends.py -- so with the short
    #: prefix an orphaned Job in `swarm-tenant-eng` was attributed to a tenant
    #: called `tenant-eng`, which exists nowhere. A finding filed against a
    #: tenant that does not exist is a finding nobody acts on, and the
    #: attribution guard in `backends.py` is built to refuse exactly that kind
    #: of claim.
    #:
    #: `scripts/lib/check-contract-parity.sh` section 6 asserts this against
    #: every other restatement in the repository.
    namespace_prefix: str = "swarm-tenant-"

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> "ReconcilerConfig":
        settings = settings or Settings.from_env()
        return cls(
            project_id=settings.project_id,
            region=settings.region,
            firestore_database=settings.firestore_database,
            lease_timeout_seconds=settings.lease_timeout_seconds,
            heartbeat_grace_seconds=_int(
                "HEARTBEAT_GRACE_SECONDS", max(90, settings.heartbeat_interval_seconds * 3)
            ),
            pass_retention_hours=_int("PASS_RETENTION_HOURS", 168),
            missing_execution_grace_seconds=_int(
                "MISSING_EXECUTION_GRACE_SECONDS", settings.dispatch_timeout_seconds
            ),
            orphan_execution_grace_seconds=_int("ORPHAN_EXECUTION_GRACE_SECONDS", 120),
            unused_job_ttl_seconds=_int("UNUSED_JOB_TTL_SECONDS", 7 * 24 * 3600),
            empty_namespace_ttl_seconds=_int("EMPTY_NAMESPACE_TTL_SECONDS", 24 * 3600),
            max_findings_per_pass=_int("MAX_FINDINGS_PER_PASS", 200),
            dry_run=_bool("RECONCILER_DRY_RUN", False),
            enable_gke=_bool("ENABLE_GKE_AUTOPILOT", settings.enable_gke_autopilot),
            enable_cloud_run=_bool("ENABLE_CLOUD_RUN_JOBS", settings.enable_cloud_run_jobs),
            enable_gc=_bool("RECONCILER_ENABLE_GC", True),
            # ARTIFACT_BUCKET is already in every control-plane service's
            # environment (terraform/infra/locals.tf, common_env), and the
            # reconciler already holds roles/storage.objectAdmin on that bucket.
            artifact_bucket=settings.artifact_bucket,
            enable_checkpoint_gc=_bool("RECONCILER_ENABLE_CHECKPOINT_GC", True),
            checkpoint_orphan_backstop_seconds=_int(
                "CHECKPOINT_ORPHAN_BACKSTOP_SECONDS", 7 * 24 * 3600
            ),
            checkpoint_scan_limit=_int("CHECKPOINT_SCAN_LIMIT", 20000),
            checkpoint_sweep_interval_seconds=_int("CHECKPOINT_SWEEP_INTERVAL_SECONDS", 3600),
        )
