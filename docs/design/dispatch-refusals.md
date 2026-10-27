# Dispatch refusals: when the platform says no to a Job (#1820)

A 403 on GKE Job create has three causes, and the code an agent ends with used
to name only one of them. This page records what each 403 is coded as today,
why, and how the `PLATFORM_REFUSED` park will use those codes once contract
request 123 ([contract-change-requests.md](../contract-change-requests.md)) is
accepted and applied.

## What happened

On 2026-10-20 19:24-20:06Z the admission policy
`swarm-worker-job-constraints` (`kubernetes/policies/pod-security.yaml`)
refused every CPU-burst Job with "requests must equal limits": the scheduler
shipped #1689's sizes before the policy allowed them (PR 1747 corrected the
policy). 54-56 dispatches failed, every one coded `gke_create_job_forbidden`,
and 11-13 agents ended FAILED `dispatch_failed` about two minutes after they
were submitted. Three merge agents used 10 of 10 attempts.

Two things were wrong at once:

* **the code.** `gke_create_job_forbidden` and its message say "this may be a
  missing namespace or a RoleBinding" ([gke-dispatch-403.md](../gke-dispatch-403.md)).
  An admission refusal is neither: Kubernetes authorises first, so a request
  that reached admission was already authorised, in a namespace that exists;
* **the retry.** `SchedulerStore.return_to_ready_after_failed_dispatch`
  returns a failed dispatch to READY after a fixed 30 s and ends it FAILED at
  `max_attempts`. A refusal that lasts as long as a release takes to correct
  spends every attempt in about `30 s × max_attempts`.

The same path applies to the Job quota. Each tenant namespace allows 200 Jobs
(`count/jobs.batch`, `kubernetes/namespaces/tenant-namespace.yaml`) and a
finished Job stays for an hour (`ttlSecondsAfterFinished`); `eng` peaked at
119 Jobs in a rolling hour, and the 201st would fail agents instead of waiting
for the oldest to expire.

## The codes (built, `apps/scheduler/scheduler/dispatch.py` `forbidden_code`)

The API server's own `Status.message` decides, read from the
`ApiException` body, else from the exception's text:

| the API server says | code | means |
|---|---|---|
| `ValidatingAdmissionPolicy '<p>' with binding '<b>' denied request: …` | `gke_admission_refused` | a cluster policy refused this manifest |
| `admission webhook "<w>" denied the request: …` | `gke_admission_refused` | a webhook (GKE Warden on Autopilot, for one) refused it |
| `exceeded quota: <quota>, requested: …, used: …, limited: …` | `gke_job_quota_exceeded` | the namespace's ResourceQuota is full |
| `User "<id>" cannot create resource "jobs" …`, and any 403 not recognised | `gke_create_job_forbidden` | RBAC, or a namespace that does not exist |

`PLATFORM_REFUSAL_CODES` is the first two. Admission is matched before quota,
so a webhook whose message happens to quote a quota is still the webhook.

**An unrecognised 403 stays `gke_create_job_forbidden`.** Being wrong toward
that code costs an operator the namespace diagnosis on a refusal; being wrong
away from it would, once the park exists, park an RBAC fault five times as
"the platform refused" before anyone looked at the RoleBinding. The texts
matched are the API server's wording, not this repository's, so a Kubernetes
release that rewords them degrades to today's behaviour, never to a wrong park.

A refusal's message drops the 403-means-404 paragraph and names the policy or
quota through the upstream text, which `loop.py` logs on its `dispatch failed`
line. The tenant sees the code and the attempt id only, as for every dispatch
failure.

Until request 123 is applied the two new codes are only the code the agent,
the attempt and the log carry. The store still retries them on its fixed
30 s and ends the agent at `max_attempts`, as it does every dispatch failure.
Nothing reads the codes to decide anything yet, and nothing in the repository
matched `gke_create_job_forbidden` to decide anything either (it is read by
operators, the runbooks and tests), so no reader is split by the new codes.

## The park, once request 123 is applied

`ParkReason.PLATFORM_REFUSED`, written by the drain's failed-dispatch branch
(`loop.py`, the `except DispatchError` after `self._router.dispatch`) through
`return_to_ready_after_failed_dispatch` when `exc.code in
PLATFORM_REFUSAL_CODES`:

1. **The same transaction, the same guards.** The agent is written only when it
   is still LEASED on this lease at this generation, and the lease is released
   in that transaction, exactly as the READY return is today. The target is
   PARKED/`PLATFORM_REFUSED` instead of READY.
2. **No attempt is spent.** The attempt is ended (`dispatch_refused`, carrying
   the code), and `attempt_count` is put back to its value before the lease.
   The platform refusing a manifest says nothing about the agent, so it must not
   move the agent toward `max_attempts`; the merge agents that used 10 of 10 on
   2026-10-20 never ran once.
3. **A bounded count, with exponential backoff.** `Task.platform_refusals`
   counts the parks. Park *n* (1-based) sets `next_eligible_at = now +
   2^(n-1) minutes`: 1, 2, 4, 8, 16. The sixth consecutive refusal ends the
   agent FAILED with the precise code in `last_error` (`gke_admission_refused`
   or `gke_job_quota_exceeded`, with the attempt id) and
   `end_cause` `dispatch_failed`. The whole ladder is 31 minutes: long enough
   for a release to roll a policy back or for an hour-old Job to expire out of
   the quota, short enough that a refusal that is not going away is reported
   the same afternoon. A dispatch that succeeds resets the count to 0.
4. **Promotion.** A sweep beside `_promote_scheduled_retries` returns a
   `PLATFORM_REFUSED` park to READY once `next_eligible_at` has passed, through
   `promote_to_ready`, guarded on the state and park reason it read. It writes
   the agent alone; only admission, later, takes capacity.
5. **An alert.** Each park increments
   `swarm_scheduler_platform_refusals_total{code}` and logs
   `platform refused dispatch` at ERROR with the upstream text. One refusal is
   a page-worthy fact: a policy or quota is refusing work the scheduler was
   allowed to admit.

### Why parking keeps invariant 1

Only LEASED, DISPATCHED, STARTING and RUNNING create demand (CONTRACT
invariant 1). A PARKED agent holds no lease and no pool count, and admission
does not consider it, so a tenant whose 300 agents are all refused holds no
capacity while they wait: the lease is released in the same transaction that
parks (invariant 2's accounting is not touched), and concurrency, counted from
LEASED (invariant 3), drops by one per park. A pending pod is never used as the
backlog: nothing is created in the cluster while the agent waits. This is the
difference from retrying faster: a READY agent that is re-leased every 30 s
takes a slot for the length of each failed create, and a quota-full namespace
would have its tenant's whole queue cycling through the pools.

### Who reads it

* **the scheduler** writes it (the failed-dispatch branch) and promotes it
  (the new sweep);
* **the reconciler** leaves it alone: a PARKED agent has no lease to reclaim;
* **swarm-api** returns it as the agent's `park_reason`, as it returns every
  park, and the console shows it beside `next_eligible_at`, so a tenant reads
  "refused by the platform, retrying at 14:08" rather than a FAILED agent;
* **operators** read the metric and the ERROR line, whose upstream text names
  the policy and its rule, or the quota and its usage.

### Not done here

A release preflight that server-side dry-runs one rendered Job per GKE profile
in each tenant namespace before the scheduler revision takes traffic, and a
shorter Job TTL (3600 s to about 600 s) to raise the per-tenant ceiling, were
proposed on #1820 and are not part of this step.
