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


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


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
    #: short beside the 300 s dispatch deadline this rule is there to beat. The
    #: repair also refuses, inside its transactions, a task that has left
    #: DISPATCHED and STARTING, so this is the first of two guards.
    #:
    #: HOW MUCH SOONER IT IS, measured against 2026-09-25. It is not the grace
    #: that decides that; it is the reconciler's tick and how late the worker
    #: exits. A 69 from an unreachable control plane now comes after the
    #: generation check's ~90 s of attempts (`agent_worker.startup.
    #: CONTROL_PLANE_READ_SCHEDULE_SECONDS`), so its execution ends about
    #: cold start + 95 s after dispatch and this rule may act 30 s after that.
    #: For the cold starts measured that day (103 to 195 s, dispatch to worker)
    #: that is 70 s before to 20 s after the 300 s deadline. At the `*/5`
    #: tick a pass falls between the two at most a quarter of the time, so for
    #: that exit this rule mostly changes `last_error`, not when the task is
    #: requeued. A worker that dies in its first seconds (an exit 1, a 143) is
    #: eligible 65 to 155 s before the deadline, so a pass falls between more
    #: often, and then it is requeued five minutes sooner. At a `*/1` tick (PR
    #: #202, the owner's decision) any of them is requeued 30 to 90 s after its
    #: execution ends.
    ended_execution_grace_seconds: int = 30

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
            stuck_cpu_floor_cores=_float("STUCK_CPU_FLOOR_CORES", 0.05),
            stuck_evidence_max_gap_seconds=_int("STUCK_EVIDENCE_MAX_GAP_SECONDS", 600),
            left_running_grace_seconds=_int("LEFT_RUNNING_GRACE_SECONDS", 300),
            ended_execution_grace_seconds=_int("ENDED_EXECUTION_GRACE_SECONDS", 30),
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
