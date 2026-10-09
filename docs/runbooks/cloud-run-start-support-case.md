# Runbook: the Cloud Run Jobs start-time support case (#625)

**When to use this.** You are the owner, filing the Google Cloud support case
you decided on 2026-10-07 (decision (3) on #625): Cloud Run Job executions
spend most of their start in `ResourcesAvailable -> Started`, and that time
doubled and recovered with nothing on our side changing. Or a Cloud Run
profile's DISPATCHED -> STARTING has jumped again, and you want the evidence
Google needs in one place.

**What it changes.** Nothing. The case text below is a draft, ready to paste
into the Google Cloud console's support form. The only commands read.

---

## Why this exists

The measurement of 2026-10-07 (#625's second comment, #667, recorded in
[`docs/worker-images.md`](../worker-images.md#where-dispatched---starting-goes-measured-2026-10-07))
found that 90-95 % of a Cloud Run worker's start is Cloud Run's own instance
provisioning, that nothing of ours changed when it doubled, and that a 27.6 MB
control job moved the same way. Confidence is high that the time is in Cloud
Run provisioning and medium that the cause is Google-side: that is reached by
elimination, and Direct VPC egress cannot be excluded because every control
uses it. Only Google can see behind `ResourcesAvailable`, so the owner decided
to ask.

`claude-code` no longer starts on Cloud Run (contract request 53, applied
2026-10-08), but `mock`, `generic`, `merge`, `indexer` and `codex` still do
([where each profile starts](../worker-images.md#where-each-profile-starts-today)),
so the answer still matters.

## Before you file: fill in the marked fields

Every `<FILL IN: ...>` in the draft is yours to replace. Nothing else in it
names a project, an account or a credential.

1. **The project number.** Read it yourself; it is not written in this
   repository:

   ```bash
   gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)'
   ```

2. **Execution names to quote: two slow, two fast, read live.** Execution
   history is retained for a limited time, so pick recent ones rather than
   the 10-04 ones, and read their conditions before you quote them. List a
   worker job's executions, then describe one:

   ```bash
   gcloud run jobs executions list --job=<JOB> --region=us-central1 --limit=20
   gcloud run jobs executions describe <EXECUTION> --region=us-central1 \
     --format='yaml(metadata.name,metadata.creationTimestamp,status.conditions)'
   ```

   `<JOB>` is a per-tenant worker job, `swarm-job-<tenant>-<profile>` (for
   example a `mock` or `merge` job), or `swarm-verify`. From the conditions,
   take the `ResourcesAvailable` and `Started` `lastTransitionTime`: a slow
   execution has a gap of 150 s or more between them, a fast one under 60 s.
   If you can, include two executions created 10-60 s apart that reached
   `Started` in the same second.

3. **Severity.** The draft says S3 / degraded: no task fails, every start is
   slower. Change it if your support plan words severities differently.

Do not paste a token, a key, a log line with an `Authorization` header or a
secret's name into the case.

---

## The case (paste from here)

**Product:** Cloud Run (Jobs) · **Region:** us-central1 ·
**Project number:** `<FILL IN: project number>` · **Severity:** S3 / degraded

### Summary

Cloud Run Job executions spend 2-4 minutes between the `ResourcesAvailable`
and `Started` conditions. The time roughly doubled on 2026-10-04 and recovered
on 2026-10-05 with no change to our jobs, images or configuration, and returned
on 2026-10-06. A 27.6 MB, 1 CPU control job moved the same way.

### The symptom

We run short-lived Cloud Run Job executions, one per agent task. We measured
874 executions of one job class between 2026-09-30 and 2026-10-07 (497 of them
with readable execution conditions), against controls of 811, 446 and 313
executions of other workloads.

* Our own dispatch-to-started time: p50 128 s / p90 212 s.
* Of that, **90-95 % is `ResourcesAvailable -> Started`**: p50 138 s / p90
  227 s across the 497 executions with conditions, on every day and every job
  we read.
* Everything else is small and flat: our API call -> execution created
  -0.2 s, created -> `ResourcesAvailable` 1-2 s, `Started` -> the
  container's first log line 3 s.
* **The slow windows** (hourly median of execution created -> `Started`):
  * 2026-10-04 ~01h UTC to 2026-10-05 ~12h UTC: 160-227 s;
  * recovered to ~100 s from 2026-10-05 ~14h UTC, with no deployment on our
    side;
  * slow again from 2026-10-06 23h UTC: ~180 s;
  * swings of the same size on 2026-09-20..22.
* **Executions created 10-60 s apart often reach `Started` in the same
  second.** It looks like a periodic start gate rather than per-execution
  work. Concurrent creation adds up to ~60 s more in bursts.
* The first execution of a job after a new image digest also spends 30-59 s
  importing the image (`ContainerReady`, "Imported container image"); that is
  about 3 % of starts, we already absorb it with a warm-up execution after
  each deployment, and it is **not** what this case is about.

### The controls

* **Nothing of ours changed.** Every execution we read had the same shape:
  4 CPU / 8 GiB, second-generation execution environment (gen2), Direct VPC
  egress, the same region (us-central1), the same image size. No change to
  our infrastructure, images or dispatcher landed between 2026-10-02 20:37Z
  and 2026-10-04 05:14Z, a span that covers the start of the first slow window.
* **A small job moved the same way.** Our 27.6 MB, 1 CPU control job
  (swarm-verify) had daily p50 76 -> 153 -> 97 -> 191 s over the same days.
  So neither image size nor CPU/memory shape explains it.
* **The same work on GKE Autopilot, same region, does not show it.** Our
  browser workload on GKE Autopilot reaches its started state in p50 15 s /
  p90 83 s over 313 runs in the same period.
* **What we cannot exclude:** every control above also uses Direct VPC
  egress. If attaching the network interface is where the time goes, our
  controls would not show the difference.

### Execution names

Slow (gap of 150 s or more between `ResourcesAvailable` and `Started`):

* `<FILL IN: slow execution name 1, from gcloud run jobs executions describe>`
* `<FILL IN: slow execution name 2, from gcloud run jobs executions describe>`

Fast (gap under 60 s), same job and shape:

* `<FILL IN: fast execution name 1, from gcloud run jobs executions describe>`
* `<FILL IN: fast execution name 2, from gcloud run jobs executions describe>`

Created 10-60 s apart, `Started` in the same second (if you found a pair):

* `<FILL IN: execution name pair, or delete this line>`

### What we ask Google

1. What happens between `ResourcesAvailable` and `Started` for a Cloud Run Job
   execution, and which part of it took 2-4 minutes for the executions above?
2. Was there a regional event or a change in us-central1 Cloud Run Jobs
   provisioning on 2026-10-04 ~01h UTC to 2026-10-05 ~12h UTC, and again from
   2026-10-06 23h UTC?
3. Is execution start gated or batched (the same-second `Started` times of
   executions created up to a minute apart)? If it is, at what interval, and
   can a project influence it?
4. Does Direct VPC egress add to `ResourcesAvailable -> Started`, and by how
   much? Would Serverless VPC Access, or no VPC egress, start faster?
5. Is there a configuration, quota or capacity setting on our side that would
   bring a 4 CPU / 8 GiB gen2 execution's provisioning back to the ~60 s we
   saw on 2026-09-24?

(End of the case.)

---

## After you file

Put the case number on #625 or #667 as a comment, not in this file: a case
number is a fact about a moving target, and the issue is where its answer will
be read. If Google names a cause on our side (Direct VPC egress, for example),
that is a change to `terraform/` and a decision of yours, filed as its own
issue.
