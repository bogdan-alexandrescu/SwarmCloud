"""Worker configuration.

Everything that decides WHAT runs comes from the frozen catalogue in
`swarm_common.profiles`, keyed by the runner profile NAME. The environment only
carries identifiers -- which task, which attempt, which generation, which
tenant. That is invariant 10 enforced at the last possible moment: even a worker
launched with a doctored environment cannot be told to run an arbitrary image or
an arbitrary command, because neither is read from the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from swarm_common.config import Settings
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, RunnerProfile, resolve_backend

from .errors import ConfigError


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required in the worker environment")
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class WorkerConfig:
    # --- identity of this attempt -------------------------------------------
    task_id: str
    attempt_id: str
    lease_id: str
    tenant_id: str
    generation: int

    # --- catalogue selection (BY NAME ONLY) ---------------------------------
    runner_profile: str

    # --- platform ------------------------------------------------------------
    project_id: str
    region: str
    firestore_database: str
    artifact_bucket: str

    # --- filesystem ----------------------------------------------------------
    #: Root under which the isolated per-attempt workspace is created. On Cloud
    #: Run this is the Preview ephemeral disk mount; it is NOT shared with any
    #: other attempt and is assumed empty on every start.
    workspace_root: Path = Path("/workspace")

    # --- timing --------------------------------------------------------------
    heartbeat_interval_seconds: int = 30
    checkpoint_interval_seconds: int = 120
    max_in_worker_retry_delay_seconds: int = 45
    #: Hard wall clock for the runner child.
    timeout_seconds: int = 3600
    #: SIGTERM -> (grace) -> SIGKILL.
    termination_grace_seconds: int = 20
    #: How often the worker re-reads control-plane state for cancellation,
    #: backpressure and a generation change that happened mid-run.
    control_poll_seconds: int = 10

    # --- safety caps ---------------------------------------------------------
    max_stdout_bytes: int = 32 * 1024 * 1024
    max_stderr_bytes: int = 8 * 1024 * 1024
    max_artifact_bytes: int = 512 * 1024 * 1024
    max_checkpoint_bytes: int = 2 * 1024 * 1024 * 1024

    # --- git -----------------------------------------------------------------
    repository_url: str | None = None
    repository_ref: str | None = None
    git_clone_timeout_seconds: int = 300

    # --- misc ----------------------------------------------------------------
    provider: str | None = None
    model: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------------
    @property
    def profile(self) -> RunnerProfile:
        try:
            return RUNNER_PROFILES[self.runner_profile]
        except KeyError as exc:
            raise ConfigError(f"unknown runner profile {self.runner_profile!r}") from exc

    @property
    def resource_class(self) -> str:
        return self.profile.resource_class

    @property
    def backend(self) -> str:
        return resolve_backend(self.profile).value

    @property
    def memory_limit_bytes(self) -> int:
        return RESOURCE_CLASSES[self.resource_class].memory_gib * 1024 * 1024 * 1024

    @property
    def disk_limit_bytes(self) -> int:
        return RESOURCE_CLASSES[self.resource_class].disk_gib * 1024 * 1024 * 1024

    @property
    def gcs_prefix(self) -> str:
        """`tenants/<tenant>/tasks/<task>/attempts/<attempt>` -- deterministic."""
        return (
            f"tenants/{self.tenant_id}"
            f"/tasks/{self.task_id}"
            f"/attempts/{self.attempt_id}"
        )

    def checkpoint_prefix(self, checkpoint_id: str) -> str:
        return f"{self.gcs_prefix}/checkpoints/{checkpoint_id}"

    @property
    def artifact_prefix(self) -> str:
        return f"{self.gcs_prefix}/artifacts"

    @property
    def log_prefix(self) -> str:
        return f"{self.gcs_prefix}/logs"

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ConfigError("fencing generation must be >= 1")
        # Touch the profile so an unknown name fails at construction rather than
        # after a workspace has been created.
        _ = self.profile
        if self.heartbeat_interval_seconds <= 0:
            raise ConfigError("heartbeat interval must be positive")
        if self.checkpoint_interval_seconds <= 0:
            raise ConfigError(
                "checkpointing is mandatory; a non-positive interval would disable it"
            )
        if self.timeout_seconds <= 0:
            raise ConfigError("timeout must be positive")
        if self.termination_grace_seconds < 0:
            raise ConfigError("termination grace must not be negative")

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> "WorkerConfig":
        settings = settings or Settings.from_env()
        profile_name = _require("RUNNER_PROFILE")
        profile = RUNNER_PROFILES.get(profile_name)
        if profile is None:
            raise ConfigError(f"unknown runner profile {profile_name!r}")

        repo = os.environ.get("REPOSITORY_URL", "").strip() or None
        ref = os.environ.get("REPOSITORY_REF", "").strip() or None

        return cls(
            task_id=_require("TASK_ID"),
            attempt_id=_require("ATTEMPT_ID"),
            lease_id=_require("LEASE_ID"),
            tenant_id=_require("TENANT_ID"),
            generation=_int_env("GENERATION", 0),
            runner_profile=profile_name,
            project_id=settings.project_id,
            region=settings.region,
            firestore_database=settings.firestore_database,
            artifact_bucket=os.environ.get("ARTIFACT_BUCKET", settings.artifact_bucket),
            workspace_root=Path(os.environ.get("WORKSPACE_ROOT", "/workspace")),
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
            checkpoint_interval_seconds=_int_env(
                "CHECKPOINT_INTERVAL_SECONDS", profile.checkpoint_interval_seconds
            ),
            max_in_worker_retry_delay_seconds=settings.max_in_worker_retry_delay_seconds,
            timeout_seconds=_int_env("TASK_TIMEOUT_SECONDS", profile.timeout_seconds),
            termination_grace_seconds=_int_env("TERMINATION_GRACE_SECONDS", 20),
            control_poll_seconds=_int_env("CONTROL_POLL_SECONDS", 10),
            repository_url=repo,
            repository_ref=ref,
            provider=profile.provider,
            model=os.environ.get("MODEL", "").strip() or None,
        )
