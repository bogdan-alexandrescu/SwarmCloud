"""Reconciler settings.

Thresholds live here rather than in Firestore because they describe how long the
reconciler waits before calling something dead, and a reconciler that could be
reconfigured to act instantly would be a way to cause the exact duplicate
execution it exists to prevent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from swarm_common.config import Settings
from swarm_common.profiles import RUNNER_PROFILES


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


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


#: The per-profile stall threshold variables are this prefix plus the profile
#: name upper-cased with `-` as `_` (S27). The global `STUCK_AFTER_SECONDS` has
#: no trailing underscore, so it is never read as an override of anything.
STUCK_AFTER_PROFILE_PREFIX = "STUCK_AFTER_SECONDS_"


def stuck_after_env_name(runner_profile: str) -> str:
    """The variable that overrides one profile's stall threshold."""
    return STUCK_AFTER_PROFILE_PREFIX + runner_profile.upper().replace("-", "_")


def _stuck_after_overrides() -> tuple[dict[str, int], tuple[str, ...]]:
    """Per-profile stall thresholds from the environment, and the names ignored.

    The profile names are `swarm_common.profiles.RUNNER_PROFILES`, imported,
    never restated. A variable naming no profile is returned as ignored and
    applies to nothing: it must not fail startup (a typo should not take the
    reconciler down) and must not be guessed at. A KNOWN profile's value that
    is not a positive integer fails startup, as `_int` does for the global: a
    threshold of zero would fence every attempt the moment it was judged.
    """
    known = {stuck_after_env_name(name): name for name in RUNNER_PROFILES}
    overrides: dict[str, int] = {}
    ignored: list[str] = []
    for name in sorted(os.environ):
        if not name.startswith(STUCK_AFTER_PROFILE_PREFIX):
            continue
        profile = known.get(name)
        if profile is None:
            ignored.append(name)
            continue
        if not os.environ[name].strip():
            continue  # set but empty: unset, as `_int` reads it
        value = _int(name, 0)
        if value <= 0:
            raise ValueError(f"{name} must be a positive integer, got {value}")
        overrides[profile] = value
    return overrides, tuple(ignored)


#: Upper bound on `STARTUP_REFUND_LIMIT` (#67, security review, PR #290). The
#: refund exists so an attempt that never reached its runner does not spend
#: one of `max_attempts`; an operator override with no ceiling could set it
#: high enough that a task failing at startup every time never runs out of
#: refunds and so never reaches FAILED, holding capacity in a retry loop
#: `max_attempts` exists specifically to bound. Ten is well above the default
#: of three and still small next to any reasonable `max_attempts`.
STARTUP_REFUND_LIMIT_MAX = 10


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
    #: `from_env` defaults it to the platform's `dispatch_timeout_seconds`, and
    #: this default matches that one: 480 since contract request 37 (it was 300).
    missing_execution_grace_seconds: int = 480

    #: How long an execution may run with no matching live lease before it is
    #: treated as an orphan. Short, because an orphan is billed compute nobody
    #: is accounting for.
    orphan_execution_grace_seconds: int = 120

    #: A per-tenant-per-profile Cloud Run Job resource with no execution in this
    #: long is garbage-collected. Job resources are free, but thousands of them
    #: make every list call slower and the console unusable.
    unused_job_ttl_seconds: int = 7 * 24 * 3600
    empty_namespace_ttl_seconds: int = 24 * 3600

    # -- GKE browser eviction (docs/runbooks/browser-eviction.md) -----------
    #: Browser pods carry `cluster-autoscaler.kubernetes.io/safe-to-evict=false`
    #: (PR #31), so nothing in the cluster will ever move or reclaim one. These
    #: settings are how the platform gets that capacity back anyway: from a
    #: browser Job that stopped making progress, and from one still running
    #: after its task finished. Both rules act on GKE Jobs only -- today that
    #: is the `browser` profile, the one profile pinned to Autopilot.
    #:
    #: False is the way back to the reconciler as it was before these rules:
    #: it turns off the two rules AND their input -- the by-id read of tasks
    #: outside the concurrency states (`Reconciler._read_settled`) and the
    #: stand-asides that read makes possible (`detect.orphan_rule_defers`). A
    #: Job left running is then an orphan execution again, killed and only
    #: then released. What it does NOT turn off is the rule that no repair
    #: which only releases may return a lease whose own execution is still
    #: running (`detect.detect_orphan_leases`): that is not part of eviction,
    #: and turning it off would only bring back a release before the kill.
    enable_gke_eviction: bool = True

    #: How long a RUNNING GKE attempt may show no progress before it is fenced
    #: (and, from the next pass, terminated if still active, and released).
    #: Progress is defined in `progress.py`, and heartbeating is deliberately
    #: not part of it: a hung browser heartbeats.
    #:
    #: Thirty minutes. The window has to be far longer than any legitimate
    #: quiet stretch in a browser run -- one Playwright action waits at most
    #: its timeout (30s by default), `wait` is capped at 60s, a launch at 60s --
    #: and long enough that the verdict rests on a dozen consecutive CPU
    #: measurements (the worker emits one every fifth 30s heartbeat, so every
    #: 150s) rather than on one. It must also be well short of the profile's
    #: own 5400s timeout, or it saves nothing: a browser attempt costs 2
    #: capacity units and an 8 vCPU / 16 GiB pod that the autoscaler may not
    #: touch, and at 30 minutes an attempt that wedged early gives back at
    #: least an hour of it.
    stuck_after_seconds: int = 1800

    #: Per-runner-profile overrides of `stuck_after_seconds` (S27, owner
    #: decision 2026-10-01): a browser pod and a claude-code run that
    #: legitimately thinks for twenty minutes should not have to share one
    #: clock. Read from `STUCK_AFTER_SECONDS_<PROFILE>`, the profile name
    #: upper-cased with `-` as `_` (claude-code -> STUCK_AFTER_SECONDS_CLAUDE_CODE).
    #:
    #: EMPTY ON PURPOSE. No environment, tfvars or Terraform sets one, so every
    #: profile resolves to the global 1800 until the owner chooses otherwise.
    #: Always read through `stuck_after_for`, the one place detect.py and
    #: repair.py both ask, so the two can never judge an attempt by different
    #: clocks.
    stuck_after_seconds_by_profile: dict[str, int] = field(default_factory=dict)
    #: `STUCK_AFTER_SECONDS_<X>` variables whose <X> named no runner profile.
    #: Ignored -- never applied to anything -- and logged once when the
    #: `Reconciler` is built, so a typo is visible rather than silently inert.
    ignored_stuck_overrides: tuple[str, ...] = ()

    #: Run the no-progress rule on Cloud Run attempts too (D5; BUILD_PROMPT_V2
    #: 2.9 asks for the signal on every runner profile, and the four
    #: non-browser profiles run on Cloud Run Jobs). Same rule, same threshold,
    #: same action as on GKE: fence only.
    #:
    #: A SEPARATE SWITCH from `enable_gke_eviction` because that flag is more
    #: than the GKE half of this rule: it also turns off the left-running rule
    #: that DELETES GKE Jobs and the by-id task read behind it. An operator who
    #: needs to stop the Cloud Run fence must not have to take GKE Job deletion
    #: with it, nor the other way round. Default on: the rule acts only on
    #: positive evidence of quiet (`progress.py`), and only fences.
    enable_cloud_run_stall_guard: bool = True

    def stuck_after_for(self, runner_profile: str | None) -> int:
        """The no-progress threshold for one runner profile, in seconds.

        The profile's override when one is set, otherwise the global
        `stuck_after_seconds`. The ONE resolver: `detect._stuck_subject` and
        `repair.Reconciler._read_progress` both call it.
        """
        return self.stuck_after_seconds_by_profile.get(
            runner_profile or "", self.stuck_after_seconds
        )

    #: Mean CPU, in cores, below which a heartbeat interval counts as quiet.
    #:
    #: 0.05 cores -- 7.5 CPU-seconds across one 150s heartbeat-event interval.
    #: What sits under it: the worker's own overhead with the agent idle (a
    #: Firestore poll every 10s, a heartbeat every 30s, a checkpoint of the
    #: browser's small work tree every 120s), which is estimated at 0.003-0.02
    #: cores, and an idle Chromium tab. What sits over it: loading or
    #: rendering a page, which costs whole CPU-seconds per action.
    #:
    #: NOT MEASURED against a live browser pod. No browser attempt on this
    #: platform has reached RUNNING yet (every browser task in staging on
    #: 2026-09-24 failed at dispatch or never started), so there is no
    #: heartbeat series to calibrate from. The direction of error is chosen:
    #: set too LOW, a wedged browser whose container still spends CPU reads as
    #: progressing and is left for the worker's own timeout to end -- the
    #: behaviour before this rule existed. Only a value too HIGH evicts
    #: healthy work, which is why this is a small number.
    stuck_cpu_floor_cores: float = 0.05

    #: The longest gap allowed between two CPU measurements inside a quiet
    #: span. A span with a larger hole in it is not PROVEN quiet -- the agent
    #: may have worked through the part nobody measured -- and is not acted on.
    #:
    #: 600s: four heartbeat-event intervals. One or two missing events are an
    #: ordinary transient write failure; four in a row mean the evidence
    #: stopped arriving, and a reconciler must not treat "stopped hearing" as
    #: "heard nothing happening".
    stuck_evidence_max_gap_seconds: int = 600

    #: How long after its task reached a terminal state a GKE Job may still be
    #: active before it is terminated as left running.
    #:
    #: 300s. The worker writes the terminal state first and only then cleans
    #: up, exports its metrics and exits, after which the Job controller marks
    #: the Job Complete; that normally takes seconds. Five minutes is far past
    #: it, and far short of what a pod left open costs -- the node under it
    #: cannot scale down while it runs, because it may not be evicted.
    left_running_grace_seconds: int = 300

    #: How long before a pass read Firestore an execution must have ENDED for
    #: the ended-at-startup rule to act on it (#198, `detect_ended_at_startup`).
    #:
    #: The rule requeues a task that is still DISPATCHED or STARTING once its
    #: execution has ended. A worker makes every write before its container
    #: exits, and Cloud Run records the end after the exit, so a snapshot read
    #: after that instant has seen every write the worker made: a park (exit
    #: 75), a failure (exit 1), a cancel (exit 71) all move the task out of
    #: DISPATCHED and STARTING first. The snapshot is taken BEFORE the backends
    #: are listed, though, so an execution can be listed as over when the
    #: snapshot predates its worker's last write. Thirty seconds is far past
    #: the seconds between a worker's last write and its container's end, and
    #: short beside the 480 s dispatch deadline this rule is there to beat. The
    #: repair also refuses, inside its transactions, a task that has left
    #: DISPATCHED and STARTING, so this is the first of two guards.
    #:
    #: HOW MUCH SOONER IT IS, measured against 2026-09-25 and restated
    #: 2026-09-30 for #402 and contract request 37. It is not the grace that
    #: decides that; it is the reconciler's tick and how late the worker exits.
    #: A 69 from an unreachable control plane now comes after the generation
    #: check's ~180 s of attempts, 200 s at worst (`agent_worker.startup.
    #: CONTROL_PLANE_READ_SCHEDULE_SECONDS`, #402; it was ~90 s), so its
    #: execution ends about cold start + 185 s after dispatch and this rule may
    #: act 30 s after that. For the cold starts measured on 2026-09-25 (103 to
    #: 195 s, dispatch to worker) that is about 160 to 70 s before the 480 s
    #: dispatch deadline. A worker that dies in its first seconds (an exit 1, a
    #: 143) is eligible about 345 to 255 s before it. At the `*/1` tick (PR
    #: #202, the owner's decision) any of them is requeued 30 to 90 s after its
    #: execution ends, so this rule, not the deadline, is what requeues them.
    ended_execution_grace_seconds: int = 30

    #: How many attempts a task may have taken back because they ended before
    #: their runner started (#67, owner decision 2026-09-28). Such an attempt
    #: -- a 143 SIGTERM, or any exit but 78 while the task was DISPATCHED or
    #: STARTING -- did no work, so it does not use up one of `max_attempts`.
    #: Three, not unlimited: once they are used an early end counts as before,
    #: so a task killed at every start still reaches FAILED. 0 turns refunds off.
    #: Bounded at `STARTUP_REFUND_LIMIT_MAX` below: an operator override past
    #: that effectively disables `max_attempts` for a task stuck failing at
    #: startup, which would hold capacity in a retry loop that never reaches
    #: FAILED (security review, PR #290).
    startup_refund_limit: int = 3

    #: Minutes an unreleased lease may sit past its TTL -- held back for want
    #: of proof, or because its repair keeps failing -- before the pass says
    #: so at ERROR (once per lease per hour) and lists it in the persisted
    #: pass as `held_past_ttl`. Fifteen: a healthy reclaim of a dead worker
    #: completes within a few one-minute passes of the TTL, so a quarter of an
    #: hour past it is never routine, and it is half the thirty minutes the
    #: suppressed-lease alert waits. On 2026-10-03 a lease was held past its
    #: TTL for five hours with only a per-pass WARNING to show for it.
    held_lease_alert_minutes: int = 15

    #: How old a lease whose worker NEVER STARTED -- no heartbeat on the
    #: lease, no start on its attempt -- must be before that absence is itself
    #: enough to repair it, when nothing else can prove the execution is gone
    #: (its kill is not confirmed, or its backend cannot be read).
    #:
    #: Why a worker that never started may be repaired without that proof: the
    #: repair writes the fence in the same transaction as the release, and a
    #: worker checks its generation before it creates a workspace, reads a
    #: secret or starts a runner, so a container that does start later exits
    #: without running the agent (invariant 5). What it cannot do is hold the
    #: lease: an execution that cannot be read or cannot be cancelled held
    #: four leases for ten hours on 2026-10-04 (#560).
    #:
    #: Why this long: it is measured from the lease's creation, and the
    #: slowest dispatch-to-first-write measured on Cloud Run is 256 s against
    #: a 480 s `dispatch_timeout_seconds`. Twenty minutes is 2.5 times the
    #: deadline and nearly five times the slowest start ever seen, so no
    #: worker that is merely slow is in this window. `from_env` keeps it at
    #: least twice the platform's dispatch timeout if that is ever raised.
    never_started_release_seconds: int = 1200

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
    #: stale or wrong, not to bound storage: a pass runs every minute, so a
    #: deletion here means roughly ten thousand consecutive passes all agreed
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
    #: clock than the one-minute reconciliation tick. Hourly: a checkpoint that
    #: became collectable a moment ago costs pennies for another hour, whereas
    #: sixty full-bucket listings an hour is a bill.
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
        stuck_by_profile, ignored_stuck = _stuck_after_overrides()
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
            enable_gke_eviction=_bool("RECONCILER_ENABLE_GKE_EVICTION", True),
            stuck_after_seconds=_int("STUCK_AFTER_SECONDS", 1800),
            stuck_after_seconds_by_profile=stuck_by_profile,
            ignored_stuck_overrides=ignored_stuck,
            enable_cloud_run_stall_guard=_bool("RECONCILER_ENABLE_CLOUD_RUN_STALL_GUARD", True),
            stuck_cpu_floor_cores=_float("STUCK_CPU_FLOOR_CORES", 0.05),
            stuck_evidence_max_gap_seconds=_int("STUCK_EVIDENCE_MAX_GAP_SECONDS", 600),
            left_running_grace_seconds=_int("LEFT_RUNNING_GRACE_SECONDS", 300),
            ended_execution_grace_seconds=_int("ENDED_EXECUTION_GRACE_SECONDS", 30),
            startup_refund_limit=min(
                STARTUP_REFUND_LIMIT_MAX, max(0, _int("STARTUP_REFUND_LIMIT", 3))
            ),
            held_lease_alert_minutes=_int("HELD_LEASE_ALERT_MINUTES", 15),
            never_started_release_seconds=max(
                2 * settings.dispatch_timeout_seconds,
                _int("NEVER_STARTED_RELEASE_SECONDS", 1200),
            ),
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
