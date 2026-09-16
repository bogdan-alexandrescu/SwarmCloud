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

    #: Label every resource this platform owns; GC refuses to touch anything
    #: without it, which is what keeps a shared project safe.
    managed_label_key: str = "managed-by"
    managed_label_value: str = "swarm"
    job_name_prefix: str = "swarm-"
    namespace_prefix: str = "swarm-"

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
        )
