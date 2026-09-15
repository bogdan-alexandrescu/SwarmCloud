"""Platform settings. Everything tunable lives here or in Firestore, never in code.

Concurrency limits deliberately live in FIRESTORE, not here, because the spec
requires changing them without redeploying. This module holds only what must be
known at process start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    project_id: str
    region: str = "us-central1"
    environment: str = "dev"

    # Firestore uses a NAMED database so the project's `(default)` stays free for
    # other teams in this shared project.
    firestore_database: str = "swarm"

    artifact_bucket: str = ""
    artifact_registry: str = "swarm-images"

    # Identity. Only these hosted domains may authenticate.
    allowed_domains: tuple[str, ...] = ("saga.xyz",)
    api_audience: str = ""

    # Admission
    max_active_agents: int = 100
    global_capacity_units: int = 200
    dispatch_timeout_seconds: int = 300
    lease_timeout_seconds: int = 120
    heartbeat_interval_seconds: int = 30

    # Workers must never sleep through a long provider wait. Anything longer
    # than this checkpoints, parks and exits so the compute disappears.
    max_in_worker_retry_delay_seconds: int = 45
    checkpoint_interval_seconds: int = 120

    # Quota prewarm
    enable_quota_prewarm: bool = True
    prewarm_lead_seconds: int = 120
    prewarm_max_agents: int = 10

    # Backends
    enable_cloud_run_jobs: bool = True
    enable_gke_autopilot: bool = True

    # API admission guards
    max_batch_size: int = 100
    max_input_bytes: int = 256 * 1024
    max_workflow_steps: int = 50
    requests_per_second: int = 20

    # New tenants start small and an admin raises them.
    default_tenant_max_active: int = 20
    default_tenant_capacity_units: int = 40

    performance_profile: str = "balanced"

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("PROJECT_ID is required")
        if self.max_active_agents <= 0:
            raise ValueError("MAX_ACTIVE_AGENTS must be positive and finite")
        if self.dispatch_timeout_seconds <= 0 or self.lease_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if self.lease_timeout_seconds <= self.heartbeat_interval_seconds:
            raise ValueError(
                "lease_timeout must exceed heartbeat_interval, or every healthy "
                "worker would be reaped as dead between heartbeats"
            )
        if self.performance_profile not in {"economy", "balanced", "burst"}:
            raise ValueError(f"unknown performance profile {self.performance_profile}")
        if not self.allowed_domains:
            raise ValueError("at least one allowed domain is required; refusing open auth")

    @classmethod
    def from_env(cls) -> "Settings":
        project = os.environ.get("PROJECT_ID", "")
        domains = tuple(
            d.strip() for d in os.environ.get("ALLOWED_DOMAINS", "saga.xyz").split(",") if d.strip()
        )
        return cls(
            project_id=project,
            region=os.environ.get("REGION", "us-central1"),
            environment=os.environ.get("ENVIRONMENT", "dev"),
            firestore_database=os.environ.get("FIRESTORE_DATABASE", "swarm"),
            artifact_bucket=os.environ.get("ARTIFACT_BUCKET", f"{project}-swarm-artifacts"),
            allowed_domains=domains,
            api_audience=os.environ.get("API_AUDIENCE", ""),
            max_active_agents=_int("MAX_ACTIVE_AGENTS", 100),
            global_capacity_units=_int("GLOBAL_CAPACITY_UNITS", 200),
            dispatch_timeout_seconds=_int("DISPATCH_TIMEOUT_SECONDS", 300),
            lease_timeout_seconds=_int("LEASE_TIMEOUT_SECONDS", 120),
            heartbeat_interval_seconds=_int("HEARTBEAT_INTERVAL_SECONDS", 30),
            max_in_worker_retry_delay_seconds=_int("MAX_IN_WORKER_RETRY_DELAY_SECONDS", 45),
            checkpoint_interval_seconds=_int("CHECKPOINT_INTERVAL_SECONDS", 120),
            enable_quota_prewarm=_bool("ENABLE_QUOTA_PREWARM", True),
            prewarm_lead_seconds=_int("PREWARM_LEAD_SECONDS", 120),
            prewarm_max_agents=_int("PREWARM_MAX_AGENTS", 10),
            enable_cloud_run_jobs=_bool("ENABLE_CLOUD_RUN_JOBS", True),
            enable_gke_autopilot=_bool("ENABLE_GKE_AUTOPILOT", True),
            performance_profile=os.environ.get("PERFORMANCE_PROFILE", "balanced"),
        )
