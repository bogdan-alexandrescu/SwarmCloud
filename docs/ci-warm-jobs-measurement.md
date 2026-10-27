# Does a Cloud Run image import stay cached across a long idle gap? (#888, box 94)

`scripts/warm-jobs.sh` skips a listed worker job that already has a successful
execution on the digest it runs, whether that was the previous release's warm
run or a tenant agent, and however long ago it ran (`already_started()`). The
skip assumes that Cloud Run pays the image import once per digest and does
not evict it after some idle time. The 2026-10-09 lane sent to check this
could not: a SwarmCloud worker is denied `run.jobs.list` and every log view.
[docs/ci.md](ci.md#the-release-warms-each-cloud-run-worker-job-once-on-its-new-digest)
called the premise "still unmeasured" and gave the read-only command that
would settle it. This page records the measurement and what was decided from
it. It replaces that paragraph of docs/ci.md.

**Measured 2026-10-27 between 05:52 and 06:12 UTC**, read-only, by the
operator (CLD) from the laptop, at BGD's decision of the same day, against
`saga-agents-staging`, region `us-central1`. origin/main at read time was
`17d6c5abd` (#2533). Nothing was created, changed, executed or deleted, and no
secret value was read. The raw field-reduced data (execution name, times,
image, counts, conditions) stays on the operator's machine and is not
committed. The numbers below are taken from it as reported. None were
re-measured or estimated here.

## What there is to measure

* **Only the release's warm self-test has run on the Cloud Run worker jobs
  since 2026-10-08.** `swarm-job-eng-claude-code` retains 1,128 executions
  (2026-09-20 18:43Z to 2026-10-27 04:03Z): 892 agent runs and 236
  `--self-test` warm runs, and its last agent run was 2026-10-08 11:09Z.
  `swarm-job-eng-indexer` has 152 agent runs and 235 warm runs, and its last
  agent run was 2026-10-10 17:25Z.
* **A runner digest almost never outlives its commit.** eng-claude-code saw
  294 distinct digests in 36 days, and the median digest ran once on that
  job. Of the newest 40 digests of each of `agent-runtime-base`,
  `-browser` and `-indexer` in Artifact Registry, none carries more than one
  commit tag. So in practice no worker job ever starts a digest after a day
  of idleness.
* **No Cloud Run browser job exists.** `claude-code-browser` runs on GKE
  Autopilot, so the browser runner was read from kubelet image-pull events
  instead (below). `warm-jobs.sh` does not warm it.

## The results

Across all 14 worker jobs and 8,481 retained executions (2026-09-20 to
2026-10-27), grouped by the hours since the same digest last ran on **any**
job:

| gap since the digest last ran | executions | timed | <= 5 s | >= 20 s | median (s) |
|---|---|---|---|---|---|
| first in project | 583 | 519 | 16 | 458 | 39.29 |
| < 5 min | 7237 | 1417 | 1005 | 360 | 1.73 |
| 5 min - 1 h | 642 | 182 | 179 | 0 | 1.13 |
| 1 - 6 h | 13 | 7 | 7 | 0 | 1.15 |
| 6 - 24 h | 6 | 4 | 4 | 0 | 1.04 |
| >= 24 h | 0 | - | - | - | - |

"Timed" means the `ContainerReady` message carries a duration. The rest read
`Imported container image.` with none, including 64 first-in-project
executions, so a missing duration does not simply mean the image was cached.
The 360 runs of 20 s or more inside "< 5 min" started alongside the first run
of their digest, before its import had finished.

* **Long gaps.** 25 worker-job executions ran a digest an hour or more after
  it last ran. The 13 that are timed read **1.00-2.49 s**, and none read
  30-59 s. The longest same-digest gap on a runner is **13.7 h**
  (`eng-claude-code-xltj5`, 2026-10-03 20:35Z, 1.00 s).
* **The cache is shared across jobs on the same digest.** A job's first run
  of a digest that another job had run 2 minutes to 1 hour earlier had a
  median of 1.22 s (127 of 132 timed runs <= 5 s). Started together with that
  other job, it paid the import as well (median 32.26 s).
* **Recent runs.** Each of the last 40 executions of eng-claude-code and of
  eng-indexer was a warm run, and each was that job's first run of its
  digest. Those that were also the first run anywhere read 21.5-59.7 s, and
  one on each job read about 123 s.
* **Control: a gap longer than a day, on a different image.**
  `swarm-workspace-apply` (not a worker job, and a much smaller image) ran
  digest `7a03edc768e6` after 27.9 h idle and imported it in 2.26 s, against
  6.98 s when the digest was fresh. That is one sample. It suggests the cache
  outlasts a day, but it does not prove it for a runner image.
* **Browser runner on GKE, for contrast.** In 151 kubelet pulls of
  `agent-runtime-browser` (2026-10-25 10:58Z to 2026-10-27 06:04Z), the first
  pull of each digest had a median of 45.8 s and later pulls a median of
  3.2 s. Autopilot caches per node, which is a different mechanism from the
  Cloud Run import.

The doc's own command (docs/ci.md), run as written against eng-claude-code,
printed 54 rows with a largest gap of 3 h, all dated 2026-10-08 or earlier.
It undercounts for two reasons. It drops every execution with no duration
(715 of 1,128), and it groups by job, although the cache is per digest
across jobs.

## Decision: the skip rule is kept as it is

**After a same-digest idle gap of 1 to 13.7 h, every timed import on a
worker job read 1.0-2.5 s.** None read the 30-59 s of a fresh digest. Under
the triage rule for box 94 ("if imports stay 1-3 s after long gaps, keep the
code"), `already_started()` keeps counting any successful execution on the
digest, a tenant agent included, with no age bound.

**A gap of 24 h or more was not measured on a runner image, because none has
ever happened.** No age bound such as `WARM_SKIP_MAX_AGE_HOURS` was added. A
default for it could not be justified from data that does not exist. And
because a new commit almost always brings a new runner digest, the skip
fires in practice only when the same release's digest is warmed a second
time. The longest gap that skip could cover is the time between two
runs of one release. If runner digests start to survive across days (for
example, if builds of an unchanged runner become reproducible), read the
table above again, grouped by the hours since the digest last ran on any job.
If a gap of 24 h or more shows a fresh 30-59 s import, bound the skip below
that gap.

`tests/unit/scripts/test_warm_jobs.py`
(`test_an_old_tenant_task_on_the_same_digest_still_skips_the_job`) holds the
decision. The test serves an old, successful agent execution on the
same digest and asserts that the job is skipped.
