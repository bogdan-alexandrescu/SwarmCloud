# Runbook: browser pods the reconciler evicts

**When to use this.** A browser task was re-queued or failed with
`stuck_no_progress`, a task's timeline shows a `generation_fenced` event with
`finding: left_running`, or you want to know whether the reconciler is evicting
browser pods, and why.

**What it changes.** Nothing. Every command here only reads.

---

## Why this exists

Browser pods carry `cluster-autoscaler.kubernetes.io/safe-to-evict=false`
(PR #31). That annotation stops the autoscaler moving a browser run in the
middle of its work. It also means nothing in the cluster will ever reclaim one:
a browser pod holds its node for as long as it runs. A browser attempt holds
2 capacity units and an 8 vCPU / 16 GiB pod.

The owner's requirement, recorded on 2026-09-24 next to that annotation:

> we should have a way to evict browser pods that are stuck for some time
> without progress or that were left open and running after the agent work
> was finished.

The reconciler does that with two rules. Both act only on GKE Jobs, and today
the `browser` profile is the only profile that runs on GKE.

| Rule | Finding | What the reconciler does |
|---|---|---|
| Stuck | `stuck_no_progress` | Fences the generation and records why, in that pass and nothing more. The worker stops the agent at its next control poll, about 10s later, and exits. On a later pass, the lease is superseded and silent, and the existing rules release it: they delete the Job first if it is still active, then set the task to READY. The task goes to FAILED instead once its attempts are spent, or to CANCELLED if a cancel was requested. |
| Left running | `left_running` | Once `LEFT_RUNNING_GRACE_SECONDS` have passed since the task finished, deletes the Job. Releases a lease only if the Job's own lease (same attempt, same generation) was never released, and only after the delete succeeded. The task stays in the terminal state it had. |

**A lease is never released while its own Job is still running.** This holds
inside the grace too, before any rule has tried to delete the Job. The worker
writes the terminal state first and releases its lease in a separate write
(`ControlPlane.finish`). A worker that wedges between the two leaves a finished
task, an unreleased lease and a pod that is still up. The reconciler runs every
minute, so its first several passes after that land inside the 300s grace. On
those passes, nothing is deleted and nothing is released. The
lease's 2 units on each of the seven pools stay held, because the pod is still
using them. On the first pass after the grace, the Job is deleted, and only then
is the lease released. If the delete is refused, the lease stays held and the
next pass tries again.

The same hold applies when the Job's task could not be read this pass, and when
the task was re-admitted between the reconciler's snapshot and its read. Both
Job and lease are left for the next pass.

**Why the stuck rule does not delete the Job in the pass that fences it.** The
worker under a stuck browser is alive, and deleting its Job sends it SIGTERM.
The worker's SIGTERM path (`lifecycle._handle_interruption`) checkpoints and
parks the task `SCHEDULED_RETRY` without checking whether it was fenced.
Nothing in the platform promotes that park. Racing the reconciler's own READY,
it would leave the task parked forever, or FAILED if the scheduler had already
leased it again. The fence has no such race: the worker's fenced exit writes
only its own attempt document. This comes from reading the code. It has not
been observed, because no browser attempt has run here yet.

The outer bound on every browser pod is unchanged: the Job's
`activeDeadlineSeconds`, which is the task timeout plus the dispatch timeout
plus the finalise budget (`backend_deadline_seconds` in
`apps/scheduler/scheduler/dispatch.py`), counted from Job creation. These rules
exist to end a pod long before that.

The code is in `apps/reconciler/reconciler/`: `progress.py` (what progress
means), `detect.py` (`detect_stuck_executions`, `detect_left_running`) and
`repair.py` (the repair order).

---

## What "no progress" means, and what it does not

**A heartbeat is not progress.** The worker heartbeats from its own supervision
loop, which keeps running whatever the agent is doing. A Chromium stuck on a
page that never answers heartbeats just as well as one that is working. So a
stuck browser task looks healthy to every older rule: its lease is fresh, its
Job is active, and its generation is current.

Here is what the worker writes while an attempt runs, and whether it counts as
progress:

| Written | Where | Progress? |
|---|---|---|
| `heartbeat_at`, every 30s | the lease | No. It only shows the worker is alive. |
| `heartbeat` event, every 150s, carrying cumulative `cpu_seconds` | the task's events | **Yes, if CPU moved.** An interval that averages at least `STUCK_CPU_FLOOR_CORES` counts. |
| `checkpoint_completed` event, every 120s, carrying `size_bytes` | the task's events | **Yes, if the size changed.** A checkpoint runs on a timer, so the event alone proves nothing. A different archive size means `work/` changed. |
| `task.updated_at`, `task.latest_checkpoint` | the task | No. Every checkpoint updates these. |
| anything per browser action | nowhere | The browser runner reports its actions only in `result.json`, when it exits. |
| stdout and stderr | GCS live tail only | Not read. The browser runner prints nothing until it exits. |

CPU is the signal that matters for browser work. The browser runner writes its
screenshots and extracts to `artifacts/`, which is not checkpointed, so its work
tree barely changes however hard it works. If browser work were judged by the
work tree alone, every healthy browser run longer than the threshold would be
evicted.

**The reconciler acts only when the quiet is proven.** An attempt counts as
stuck only when both of these hold:

- CPU samples and checkpoints were each recorded across the whole quiet span,
  with no gap longer than `STUCK_EVIDENCE_MAX_GAP_SECONDS`.
- Every one of those measurements showed nothing.

If CPU could not be measured (`cpu_seconds: null`), or the attempt's events
stopped arriving, the reconciler does not judge the attempt at all. It logs this
line and leaves the attempt alone:

```
not judging an attempt's progress: the evidence is incomplete
```

The reconciler judges an attempt only by that attempt's own events at its own
generation. A new attempt does not inherit the quiet history of the attempt it
replaced.

**What this does not catch.** A browser spinning at full CPU on a page that
never finishes looks like it is progressing. The worker's own `timeout_seconds`
still ends it: 5400s for `browser`, the same bound every attempt had before this
rule existed.

---

## Settings

These are environment variables on the `swarm-reconciler` Cloud Run service.
Each default and the reason for it is written next to the value in
`apps/reconciler/reconciler/config.py`. None of them is set in terraform today,
so the defaults apply.

| Variable | Default | Meaning |
|---|---|---|
| `RECONCILER_ENABLE_GKE_EVICTION` | `true` | Turns both rules on or off, together with their input: the by-id read of tasks outside the concurrency states. With it off, no task is read by id, and a Job left running falls back to the orphan-execution rule, as before this change: it is deleted at once, and its lease is released only after the delete succeeds. It does not turn off the lease hold described above. That hold is not part of eviction, and turning it off would only bring back a release before the delete. |
| `STUCK_AFTER_SECONDS` | `1800` | How long an attempt may go without progress before the reconciler evicts it. |
| `STUCK_CPU_FLOOR_CORES` | `0.05` | The mean CPU below which a heartbeat interval counts as quiet. |
| `STUCK_EVIDENCE_MAX_GAP_SECONDS` | `600` | The longest gap allowed between measurements inside a quiet span. |
| `LEFT_RUNNING_GRACE_SECONDS` | `300` | How long after the task finished its Job may still be active. |

`RECONCILER_DRY_RUN=true` still does what it always did. Both rules report what
they would evict, under `skipped: dry_run`, and write nothing.

To change a value permanently, add it to the `"swarm-reconciler"` env map in
`terraform/infra/locals.tf`. A `gcloud run services update --update-env-vars`
takes effect sooner, but the next terraform apply reverts it.

---

## Finding every eviction

Each eviction writes one log line, `evicted a GKE job`, which carries `kind`,
`task_id`, `execution`, `namespace` and `reason`:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND resource.labels.service_name="swarm-reconciler"
   AND jsonPayload.component="reconciler"
   AND jsonPayload.message="evicted a GKE job"' \
  --project saga-agents-staging --freshness=1d --format=json
```

The same outcome is stored on the persisted pass. Each `reconciler_passes`
document lists it under `outcomes`, with `kind` set to `stuck_no_progress` or
`left_running`.

The task's own events also say what happened:

- **Stuck:**
  - From the reconciler: `generation_fenced` with `detail.finding:
    stuck_no_progress`. Its `detail.reason` gives the quiet span, the mean CPU,
    how many checkpoints were unchanged, and the last progress seen.
    `detail.then` says what happens next.
  - From the worker, seconds later: `generation_fenced` with `phase: running`.
  - On a later pass, from the rule that finishes the job: `lease_released`,
    then `ready`, `failed` or `cancelled`. Their `detail.reason` is
    `orphan_lease` or `missing_execution` if the worker stopped itself, or
    `obsolete_generation` if the Job had to be deleted.
- **Left running:** `generation_fenced` with `detail.finding: left_running` and
  `detail.phase: left_running`. There is no `EventType` for "execution evicted"
  yet: [contract request 19](../contract-change-requests.md) asks for one.
  `generation_fenced` is the nearest existing type.

---

## Things that look like this but are not

- **An attempt that ran for less than `STUCK_AFTER_SECONDS`.** It is never read,
  let alone judged.
- **A lease that stopped heartbeating.** That is `dead_worker` or `stale_lease`,
  and those rules act much sooner. When both rules match, the stuck rule gives
  way.
- **A finished task whose lease is still unreleased.** If its Job is still
  active, this is the lease hold described above, not a leak. The lease is
  released once the Job is gone. If a pass tried to delete the Job and failed,
  every other finding about that lease in that pass is recorded with
  `skipped: execution_not_stopped`.
- **A Job of an older generation.** That is `obsolete_generation`.
- **A Job that claims a generation above its task's.** No admission ever issued
  that generation. Admission raises `current_generation` in the same transaction
  that writes the lease, before any dispatch. So this is an `orphan_execution`,
  and the reason says so.
- **A Job in a namespace the reconciler did not read.** The rules act only on
  what they see. The reconciler reads the namespace of every registered tenant
  and every live attempt. A Job in the namespace of a tenant that is no longer
  registered is not seen.
- **GKE unreadable this pass.** If the Job list for a namespace fails, neither
  rule acts there. A by-name probe can find a Job, but it cannot tell whether
  that Job has been quiet.

---

## Not verified against a live cluster

The unit tests in `tests/unit/control_plane/test_browser_eviction.py` run the
real reconciler, store and `GkeBackend` against a fake Kubernetes API and an
in-memory Firestore. The following have **not** been observed on a real cluster:

- **The CPU floor.** No browser attempt on this platform has reached RUNNING
  yet. None of the 21 browser tasks in staging on 2026-09-24 has a
  `started_at`, so there is no real heartbeat series to calibrate against. The
  floor was chosen so that an error in it fails safe. Too low, and a wedged
  browser is left for the worker's timeout. Only a value that is too high
  evicts healthy work.
- **Chromium's CPU when idle** on an Autopilot pod, and the worker's own
  overhead in that cgroup.
- **The delete itself** (`delete_namespaced_job` with background propagation)
  against the `swarm-reaper` Role, for these two rules. It is the same
  `GkeBackend.terminate` call every GKE repair makes.
