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

from . import standalone_outputs
from .errors import ConfigError


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required in the worker environment")
    return value


def _bool_env(name: str, default: bool) -> bool:
    """Read a boolean the way an operator expects it to read.

    `0`, `false`, `no` and `off` are all false, case-insensitively. Anything
    else non-empty is true. An UNSET variable falls back to the default rather
    than to false, so adding a switch cannot quietly turn off a behaviour that
    every existing deployment already has.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


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
    #: What a CLI agent with no repository may have uploaded from its working
    #: folder, per attempt: 50 files and 25 MiB in total. The owner's numbers
    #: (#184, 2026-09-26), kept in `standalone_outputs` beside the rules they
    #: bound. Much lower than `max_artifact_bytes` on purpose: a file the agent
    #: put in `$SWARM_ARTIFACTS_DIR` was meant to be kept, while the working
    #: folder also holds whatever the agent generated on the way.
    max_workdir_output_files: int = standalone_outputs.MAX_FILES
    max_workdir_output_bytes: int = standalone_outputs.MAX_BYTES

    # --- live logs -----------------------------------------------------------
    # The complete streams are uploaded once, at exit. That is correct for the
    # record and useless for watching: a twenty-minute task is a twenty-minute
    # blind spot. A bounded TAIL is published on a short timer instead.
    #
    # THE COST IS WORTH SEEING BEFORE TUNING THIS. One object write per stream
    # per interval per running agent. At 5s and 40 concurrent agents that is
    # 16 writes/second for the runner's two streams, ~1.4M class-A operations
    # a day, which is real money at GCS list prices. A CLI or generic runner
    # publishes its agent's two streams as well (#184), so up to 32
    # writes/second (~2.8M a day) when every stream is non-empty; a zero-byte
    # stream is skipped, and claude-code's stderr is usually empty. Raise the
    # interval before raising the concurrency.
    #
    # It is a TAIL and not the whole file on purpose: GCS has no append, so
    # publishing the full stream would rewrite up to `max_stdout_bytes` every
    # interval. A window keeps the cost flat in the length of the run.
    live_logs_enabled: bool = True
    live_log_interval_seconds: int = 5
    live_log_tail_bytes: int = 256 * 1024

    # --- git -----------------------------------------------------------------
    repository_url: str | None = None
    repository_ref: str | None = None
    git_clone_timeout_seconds: int = 300

    # Harvest and publish. The DEFAULTS here encode the split the whole feature
    # rests on: harvesting is on because it is read-only and costs one git
    # invocation, while publishing is on but cannot act -- it is gated at
    # runtime on the forge confirming the token carries the push bit, so a
    # platform whose tenants hold clone-only tokens (the state today) gets the
    # harvest path and an explicit reason, not a failure and not a silent skip.
    git_harvest_enabled: bool = True
    git_publish_enabled: bool = True
    git_harvest_timeout_seconds: int = 120
    #: A patch larger than this is DISCARDED rather than truncated: a truncated
    #: patch applies cleanly and silently drops the rest of the change, which is
    #: a worse outcome than having no patch at all.
    max_patch_bytes: int = 16 * 1024 * 1024
    #: The worker derives the branch from the task id and re-checks this prefix
    #: inside `push_branch`. The agent never supplies a branch name.
    git_branch_prefix: str = "swarm/"
    git_author_name: str = "swarmcloud agent"
    git_author_email: str = "swarmcloud-agent@users.noreply.github.com"

    # --- the account pool ----------------------------------------------------
    # Base URL of the quota broker, which is the platform's single writer of
    # subscription credentials and the only thing that may hand out an account.
    #
    # UNSET MEANS "THIS DEPLOYMENT HAS NO POOL", and that is the backwards
    # compatibility guarantee written down as a default. Without it the worker
    # resolves its tenant's own `swarm-tenant-<tenant>-<provider>` secret
    # exactly as it always has -- no call, no new failure mode, no behaviour
    # change for any deployment that has not registered an account. This is
    # deliberately NOT "not configured means try localhost": "not configured"
    # must mean refuse, and here refusing means declining to use the pool
    # rather than guessing at where it lives.
    quota_broker_url: str | None = None
    #: OIDC audience for the broker. Cloud Run checks a token's `aud` against
    #: the service URL unless the service declares a custom audience -- which
    #: this deployment's does (`custom_audiences` in terraform) -- so it is set
    #: explicitly rather than inferred, and falls back to the URL.
    quota_broker_audience: str | None = None

    # --- misc ----------------------------------------------------------------
    provider: str | None = None
    #: The model the agent CLI runs, from the Job's `MODEL` (#226). Set per
    #: runner profile in Terraform (`local.runner_models`, today
    #: `claude-code = "claude-opus-5-5"`), and on a Job the scheduler creates
    #: from the same value (`WORKER_MODELS`). The lifecycle hands it to the
    #: runner as `MODEL` and the runner passes it as `--model`. Never read from
    #: a task: a caller's `input.model` is refused at the API and dropped by
    #: the lifecycle (invariant 10). None leaves the CLI on its own default.
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
            git_harvest_enabled=_bool_env("GIT_HARVEST_ENABLED", True),
            git_publish_enabled=_bool_env("GIT_PUBLISH_ENABLED", True),
            git_harvest_timeout_seconds=_int_env("GIT_HARVEST_TIMEOUT_SECONDS", 120),
            max_patch_bytes=_int_env("MAX_PATCH_BYTES", 16 * 1024 * 1024),
            git_branch_prefix=os.environ.get("GIT_BRANCH_PREFIX", "").strip() or "swarm/",
            live_logs_enabled=_bool_env("LIVE_LOGS_ENABLED", True),
            live_log_interval_seconds=_int_env("LIVE_LOG_INTERVAL_SECONDS", 5),
            live_log_tail_bytes=_int_env("LIVE_LOG_TAIL_BYTES", 256 * 1024),
            quota_broker_url=os.environ.get("QUOTA_BROKER_URL", "").strip() or None,
            quota_broker_audience=(
                os.environ.get("QUOTA_BROKER_AUDIENCE", "").strip() or None
            ),
            provider=profile.provider,
            model=os.environ.get("MODEL", "").strip() or None,
        )
