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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from swarm_common.config import Settings
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, RunnerProfile, resolve_backend

from . import artifact_manifest, standalone_outputs
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



# ---------------------------------------------------------------------------
# THE LEASE'S SILENCE GRACE AND THE BEAT THAT HAS TO FIT INSIDE IT (#426)
# ---------------------------------------------------------------------------
#
# Stated together here, because the one is only meaningful against the other.
# The reconciler reclaims a lease that has been silent for longer than
# `heartbeat_grace_seconds` (`reconciler.config.ReconcilerConfig`), as
# `lost_worker`. At the platform's defaults that was a 90 s grace against a
# 30 s beat: the THIRD beat was due on the grace itself, so two missed beats
# and any latency on the third were a reclaim. On 2026-10-01 a claude-code
# worker compiling on every core went 98 s silent across a checkpoint and was
# reclaimed alive.
#
# So the worker derives its beat from the grace: two consecutive beats may be
# lost, and the third still has a whole interval to land before the grace runs
# out -- `(MISSED_BEATS_TOLERATED + 2) x beat <= grace`, 4 x 22 = 88 <= 90.
# A grace with room to spare keeps the platform's interval: a beat is a
# Firestore transaction, and beating more often than the grace needs costs
# writes for nothing.
#
# `heartbeat_grace_seconds` restates the reconciler's default formula, and
# reads the same `HEARTBEAT_GRACE_SECONDS` override it does; the parity is held
# by tests/unit/worker/test_heartbeat_outlives_a_busy_checkpoint.py. The
# worker cannot import the reconciler, and the platform `Settings` that both
# read is frozen: moving the grace there is a contract change request, not a
# change made here.

#: The reconciler's floor under the grace: `max(90, 3 x interval)`.
HEARTBEAT_GRACE_FLOOR_SECONDS = 90
#: How many consecutive beats may be lost -- a slow transaction, a scheduling
#: stall under a saturated CPU -- with the lease still inside its grace.
MISSED_BEATS_TOLERATED = 2


def heartbeat_grace_seconds(platform_interval_seconds: int) -> int:
    """The silence the reconciler tolerates before it reclaims a lease.

    The reconciler's own derivation (`ReconcilerConfig.from_env`): the
    `HEARTBEAT_GRACE_SECONDS` override, else `max(90, 3 x interval)` of the
    platform's `HEARTBEAT_INTERVAL_SECONDS`.
    """
    return _int_env(
        "HEARTBEAT_GRACE_SECONDS",
        max(HEARTBEAT_GRACE_FLOOR_SECONDS, 3 * platform_interval_seconds),
    )


def heartbeat_beat_seconds(grace_seconds: int, platform_interval_seconds: int) -> int:
    """The worker's beat: the platform's interval, shortened until it fits the grace.

    `(MISSED_BEATS_TOLERATED + 2) x beat <= grace`: the beats after the last
    one that landed may be lost `MISSED_BEATS_TOLERATED` times, and the next
    one still has a whole interval to land in.
    """
    beat = min(platform_interval_seconds, grace_seconds // (MISSED_BEATS_TOLERATED + 2))
    if beat < 1:
        raise ConfigError(
            f"a {grace_seconds} s heartbeat grace leaves no room for "
            f"{MISSED_BEATS_TOLERATED} missed beats; raise HEARTBEAT_GRACE_SECONDS"
        )
    return beat


#: How far below the worker the runner child runs (`nice`, 0-19), and every
#: process it forks with it: the agent, its compilers, its test runners. A
#: build with `-p` equal to the core count competed with the heartbeat at the
#: SAME priority (#426). Linux lets a process lower its own priority, or its
#: child's, without a capability, but not raise it, so the heartbeat is put
#: above the agent by lowering the agent. 10 gives the worker about nine times
#: an agent process's CPU weight under the kernel's fair scheduler (1024
#: against 110) when both want the CPU, and costs the agent nothing when the
#: worker is idle, which is nearly always.
RUNNER_NICENESS_DEFAULT = 10

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
    #: The worker's beat, `heartbeat_beat_seconds` of the grace -- not the
    #: platform's `HEARTBEAT_INTERVAL_SECONDS` as such (#426). 22 at the
    #: platform's defaults (a 90 s grace).
    heartbeat_interval_seconds: int = 22
    #: `RUNNER_NICENESS_DEFAULT`: the runner child runs this far below the worker.
    runner_niceness: int = RUNNER_NICENESS_DEFAULT
    checkpoint_interval_seconds: int = 120
    max_in_worker_retry_delay_seconds: int = 45
    #: How many times one forge call the worker makes before or after the
    #: agent (the issue fetch, the push-scope probe, the publish's probe and
    #: pull request, the merge and post-verdict reads) is tried when it fails
    #: TRANSIENTLY -- a timeout, a reset, DNS, 429, 5xx, a rate-limit 403
    #: (`forge.retry_transient`). Four, because one blip lost a whole run on
    #: 2026-10-04 (run_51e2e460eef54d208986) and a second try clears a blip.
    #: The tries share ONE wall-clock budget, the smaller of
    #: `max_in_worker_retry_delay_seconds` and the step's remaining deadline,
    #: so this never becomes the long in-worker wait invariant 4 forbids;
    #: past it the attempt fails retryably and its capacity is released.
    forge_read_attempts: int = 4
    #: Hard wall clock for the runner child.
    timeout_seconds: int = 3600
    #: SIGTERM -> (grace) -> SIGKILL.
    termination_grace_seconds: int = 20
    #: How often the worker re-reads control-plane state for cancellation,
    #: backpressure and a generation change that happened mid-run.
    control_poll_seconds: int = 10
    #: How long the worker heartbeats the lease from a thread while a
    #: checkpoint is written (`lifecycle._heartbeat_meanwhile`, #286): three
    #: lease timeouts, 360 s at the default 120 (`from_env` derives it from
    #: `LEASE_TIMEOUT_SECONDS`). Meant to be long enough for an honest
    #: checkpoint of a large tree now that the tool caches are left out (not
    #: measured against the 2 GiB cap), and bounded so that a checkpoint
    #: running past it is treated as wedged. An unbounded thread would keep a wedged attempt's lease, and the
    #: capacity reserved behind it, alive for ever while the supervision loop
    #: is not running to notice; bounded, the lease lapses one lease timeout
    #: later and the reconciler reclaims it as it would a silent worker.
    heartbeat_meanwhile_max_seconds: int = 360

    # --- safety caps ---------------------------------------------------------
    max_stdout_bytes: int = 32 * 1024 * 1024
    max_stderr_bytes: int = 8 * 1024 * 1024
    max_artifact_bytes: int = 512 * 1024 * 1024
    #: At most this many files are uploaded from `$SWARM_ARTIFACTS_DIR` per
    #: attempt: 500, the owner's number (#228, 2026-09-26). Each is an entry in
    #: `result_summary.artifacts`, on the task's Firestore document, which holds
    #: 1 MiB; about 6,000 small files took it past that. The rest are counted
    #: and named in the log. The order they are taken in, and the bound on a
    #: name's length that keeps 500 entries small, are in `artifact_manifest`.
    max_artifact_files: int = artifact_manifest.MAX_FILES
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

    # --- the finish wake (#636) ----------------------------------------------
    #: The scheduler's wake topic (DISPATCH_TOPIC), passed through by the
    #: scheduler's `worker_env` from its own settings. The worker publishes a
    #: `task_finished` wake on it after ending its task, so the task's
    #: dependants are released at once instead of on the next safety tick.
    #: None means no wake; the tick still releases them.
    wake_topic: str | None = None

    # --- pull request console links -------------------------------------------
    #: The console's origin (SWARM_CONSOLE_URL), passed through by the
    #: scheduler's `worker_env` from its own settings -- never from a caller.
    #: None means no link is ever written.
    console_url: str | None = None
    #: Whether a pull request this worker opens carries the workflow and agent
    #: console links (SWARM_PR_CONSOLE_LINKS). Default False: off until the
    #: owner has seen it on a real PR (owner decision, 2026-10-01). The
    #: scheduler omits the variable when the switch is off, so the default is
    #: what an unswitched deployment gets.
    pr_console_links: bool = False

    # --- child tasks (docs/design/child-tasks.md) ---------------------------
    #: The one-use registration nonce the scheduler minted for THIS attempt
    #: (SWARM_CHILD_NONCE, §3.2 step 1). Spent registering the attempt key
    #: before the agent exists; worthless after. Never in the repr, a log line
    #: or the agent's environment. None means no child path for this attempt.
    child_nonce: str | None = field(default=None, repr=False, compare=False, hash=False)
    #: swarm-api's address and ID-token audience (SWARM_API_URL,
    #: SWARM_API_AUDIENCE), from the scheduler's own settings.
    swarm_api_url: str | None = None
    swarm_api_audience: str | None = None
    #: The design's limits (§7). Requests answered per control poll: four per
    #: 10 s keeps one worker far below the API's per-caller rate.
    max_child_requests_per_tick: int = 4
    #: How long answering one request may retry: below
    #: max_in_worker_retry_delay_seconds (45), so it is never the long
    #: in-worker wait invariant 4 forbids.
    child_submit_retry_seconds: int = 30
    #: Equal to max_input_bytes: the worker never reads an unbounded file an
    #: agent wrote into its own memory.
    max_child_request_bytes: int = 256 * 1024
    #: Awaits whose attempt is refunded; the fifth counts like any attempt.
    max_child_await_resumes: int = 4

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

    # --- the step-spec signature (contract request 34) ----------------------
    # PLATFORM configuration, never a task's: terraform renders these onto
    # every worker Job (and the swarm-spec-verify-keys ConfigMap on GKE), and
    # `scheduler.dispatch.worker_env` is held by a test to never set them.
    #
    #: `enforce` (THE DEFAULT, decision 5) or `legacy`. In legacy an unsigned
    #: task created before `spec_legacy_cutover` runs, until
    #: `specverify.SPEC_LEGACY_UNTIL`. The FIRST release that verifies ships
    #: `legacy` from terraform, because every task already parked then is
    #: unsigned; the default is what the platform returns to after the window.
    spec_signature_mode: str = "enforce"
    #: The moment the signing swarm-api revision took all traffic (RFC 3339).
    spec_legacy_cutover: datetime | None = None
    #: The crypto key whose versions the worker trusts: projects/.../cryptoKeys/<k>.
    spec_signing_key: str = ""
    #: {full version name: PEM public key}, every ENABLED version of that key.
    spec_verify_keys: dict[str, str] = field(default_factory=dict, hash=False)
    #: TASK_TIMEOUT_SECONDS as the execution carried it, or None when unset.
    #: Compared with the signed `timeout_seconds` (check 5).
    task_timeout_env: int | None = None
    #: CLOUD_RUN_JOB, set by Cloud Run itself, not by the scheduler.
    cloud_run_job: str | None = None
    #: RUNNER_JOB_NAME, set by the GKE dispatcher's render (self-consistency only).
    runner_job_name: str | None = None
    #: The clock the legacy window is read against. None is the wall clock;
    #: a test injects one to stand past SPEC_LEGACY_UNTIL.
    spec_clock: Callable[[], datetime] | None = field(default=None, compare=False, hash=False)

    # ------------------------------------------------------------------------
    @property
    def profile(self) -> RunnerProfile:
        try:
            return RUNNER_PROFILES[self.runner_profile]
        except KeyError as exc:
            raise ConfigError(f"unknown runner profile {self.runner_profile!r}") from exc

    @property
    def profile_resource_class(self) -> str:
        """The PROFILE's class. Not necessarily the one the container was sized with.

        The dispatcher sizes the container from the TASK's class when the
        catalogue still has it (`scheduler.dispatch.resource_class_for`), and a
        workflow step may name a smaller class than its profile's. Named for
        what it is, because as `resource_class` it was read as the container's
        size, and the OOM near-miss was judged against memory the container
        never had (#205). `sized_resource_class` is the container's.
        """
        return self.profile.resource_class

    def sized_resource_class(self, task_class: Any) -> str:
        """The class the container was sized with, given the task's `resource_class`.

        `resource_class_for(task, profile)` restated (the worker image does not
        install the scheduler; `tests/unit/worker/test_cpu_sampler.py` compares
        the two): the task's own class when the catalogue still has it, else
        the profile's.
        """
        if isinstance(task_class, str) and task_class in RESOURCE_CLASSES:
            return task_class
        return self.profile_resource_class

    @property
    def backend(self) -> str:
        return resolve_backend(self.profile).value

    def memory_limit_bytes_of(self, resource_class: str) -> int:
        """`resource_class`'s memory, which is request AND limit (invariant 7).

        A method taking the class, not a property of this config: the config
        knows only the profile's class, and the limit that matters is the
        container's (#205). There is no disk counterpart; nothing read one.
        """
        return RESOURCE_CLASSES[resource_class].memory_gib * 1024 * 1024 * 1024

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
        if not 0 <= self.runner_niceness <= 19:
            raise ConfigError(
                f"runner niceness must be 0-19 (a lower priority than the worker), "
                f"got {self.runner_niceness}"
            )
        if self.heartbeat_meanwhile_max_seconds <= 0:
            raise ConfigError("the checkpoint heartbeat's bound must be positive")
        if self.checkpoint_interval_seconds <= 0:
            raise ConfigError(
                "checkpointing is mandatory; a non-positive interval would disable it"
            )
        if self.timeout_seconds <= 0:
            raise ConfigError("timeout must be positive")
        if self.termination_grace_seconds < 0:
            raise ConfigError("termination grace must not be negative")
        if self.spec_signature_mode not in ("enforce", "legacy"):
            raise ConfigError(
                f"SPEC_SIGNATURE_MODE must be enforce or legacy, got {self.spec_signature_mode!r}"
            )

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> "WorkerConfig":
        settings = settings or Settings.from_env()
        profile_name = _require("RUNNER_PROFILE")
        profile = RUNNER_PROFILES.get(profile_name)
        if profile is None:
            raise ConfigError(f"unknown runner profile {profile_name!r}")

        repo = os.environ.get("REPOSITORY_URL", "").strip() or None
        ref = os.environ.get("REPOSITORY_REF", "").strip() or None

        # The step-spec verification settings (contract request 34). The Job's
        # environment on Cloud Run; the read-only ConfigMap mount on GKE, read
        # only when the environment carries no keys. On GKE the mount supplies
        # ALL FOUR, the mode and the cutover included (owner decision
        # 2026-09-29), so a GKE worker follows the legacy window exactly as a
        # Cloud Run one does; the environment still wins wherever it is set.
        from . import specverify

        spec = {name: os.environ.get(name, "") for name in specverify.SETTING_NAMES}
        if not spec["SPEC_VERIFY_KEYS"].strip():
            mounted = specverify.read_mount()
            spec = {
                name: value if value.strip() else mounted[name] for name, value in spec.items()
            }
        raw_keys = spec["SPEC_VERIFY_KEYS"]
        signing_key = spec["SPEC_SIGNING_KEY"].strip()
        timeout_env = os.environ.get("TASK_TIMEOUT_SECONDS", "").strip()

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
            heartbeat_interval_seconds=heartbeat_beat_seconds(
                heartbeat_grace_seconds(settings.heartbeat_interval_seconds),
                settings.heartbeat_interval_seconds,
            ),
            runner_niceness=_int_env("RUNNER_NICENESS", RUNNER_NICENESS_DEFAULT),
            checkpoint_interval_seconds=_int_env(
                "CHECKPOINT_INTERVAL_SECONDS", profile.checkpoint_interval_seconds
            ),
            max_in_worker_retry_delay_seconds=settings.max_in_worker_retry_delay_seconds,
            forge_read_attempts=max(1, _int_env("FORGE_READ_ATTEMPTS", 4)),
            timeout_seconds=_int_env("TASK_TIMEOUT_SECONDS", profile.timeout_seconds),
            termination_grace_seconds=_int_env("TERMINATION_GRACE_SECONDS", 20),
            control_poll_seconds=_int_env("CONTROL_POLL_SECONDS", 10),
            heartbeat_meanwhile_max_seconds=3 * settings.lease_timeout_seconds,
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
            wake_topic=os.environ.get("DISPATCH_TOPIC", "").strip() or None,
            console_url=os.environ.get("SWARM_CONSOLE_URL", "").strip() or None,
            pr_console_links=_bool_env("SWARM_PR_CONSOLE_LINKS", False),
            child_nonce=os.environ.get("SWARM_CHILD_NONCE", "").strip() or None,
            swarm_api_url=os.environ.get("SWARM_API_URL", "").strip() or None,
            swarm_api_audience=os.environ.get("SWARM_API_AUDIENCE", "").strip() or None,
            provider=profile.provider,
            model=os.environ.get("MODEL", "").strip() or None,
            spec_signature_mode=specverify.parse_mode(spec["SPEC_SIGNATURE_MODE"]),
            spec_legacy_cutover=specverify.parse_cutover(spec["SPEC_LEGACY_CUTOVER"]),
            spec_signing_key=signing_key,
            spec_verify_keys=specverify.parse_verify_keys(raw_keys),
            task_timeout_env=_int_env("TASK_TIMEOUT_SECONDS", 0) if timeout_env else None,
            cloud_run_job=os.environ.get("CLOUD_RUN_JOB", "").strip() or None,
            runner_job_name=os.environ.get("RUNNER_JOB_NAME", "").strip() or None,
        )
