# The outcome ledger: `GET /v1/outcomes`

What the platform's work in a span **ended as**, bucketed by **when it ended**.
It feeds Work › Timeline. The owner's decisions are in issue #185 (2026-09-25);
the code is `apps/swarm-api/swarm_api/outcomes.py`, the only implementation.

This page records why the route is shaped the way it is, so the shape is not
quietly reverted.

## Why it exists

The old Timeline stacked outcomes (placed by the day work ended) on top of
still-open work (placed by the day it was submitted) in one column, over the
newest 200 tasks. So a column's height measured nothing. On dev, 16–25 Sep
2026, 416 cancels flattened 28 failures, and 22 Sep's 305 cancels hid that
day's 8 failures.

## One basis per figure

* Every outcome figure is placed by the task's **`completed_at`**.
* Exactly one series is not: **`submitted`**, the throughput lane's arrivals,
  placed by **`created_at`**. The owner added that lane to answer "are we
  keeping up".
* The response names both under `basis`, so no client has to restate which is
  which.
* **Cost is placed by the task's end**, not by when an attempt ran. The worker
  records spend before its terminal write (`lifecycle._upload_outputs` calls
  `_record_spend` first, and `_cleanup` is the crash backstop). So a finished
  task's spend is complete, while an open task's spend is not yet known.
  Showing it would show a number that is still moving.

## The rate excludes cancels (owner decision)

* k = succeeded. n = succeeded + failed + dead_lettered.
* The interval is Wilson 95 %: z = 1.959964, clamped to [0, 1], 4 dp. Check
  value: 272 of 300 → 0.9067, 0.8684–0.9346.
* A bucket with nothing decided has `rate: null`, drawn as a gap. It is never
  0 %. A bucket of 305 cancels is not a bucket where everything failed.

## Null is not known, and 0 is measured

This rule runs through the whole response:

* `cost.sum_usd` is null when no attempt reported a cost. $0.00 is a report:
  `mock` spends nothing on purpose.
* A bucket that could not be read is `state: "unread"`, and **every** number on
  it is null. It is never a partial sum. `totals` are summed only from buckets
  that were read (TS-9), and `totals.complete` says whether any bucket was
  unread.
* In platform scope, ONE tenant's unread day empties the bucket for every
  tenant. Otherwise the bucket would show eng's successes without research's
  failures.
* `coverage.terminal_without_completed_at` is null when its count failed, never
  0.
* A bucket before a tenant's first task holds sealed zeros. That is a real
  measurement: tasks are never TTL'd, and the derive covers every task.

## Derive, write, drift check

This is the shape the owner chose for workflow state (`rollup.py`), for the same
reason: a written value and a derived value are two records of one fact.

* **DERIVE.** `derive_day(tenant, utc_day)` does four reads, and each one leads
  with `tenant_id == T`:
  * the tenant's tasks that ended that day;
  * the tasks that arrived that day;
  * their attempts;
  * their workflow parents, read with `get_all` and checked against the tenant.

  It turns each task into a small tuple. A parent that is missing or belongs to
  another tenant counts as **unread**. That task's wait is excluded
  (`coverage.wait_excluded`), never guessed.
* **WRITE.** The tuples are stored in `outcome_days/{tenant}_{YYYY-MM-DD}`,
  one JSON string field per kind, in columnar form. A day over 700 KB is sharded
  into `{id}_s{n}`, all in one batch (max 12 parts, because of Firestore's
  10 MiB request cap). A day that is bigger still is `unread: too_large`. Each
  write is an idempotent whole-document set, so there is no transaction.
* **DRIFT CHECK.** `POST /v1/admin/outcomes/rollup` re-derives the stored sealed
  days for one tenant (at most 31 days per call) and **reports** what
  disagrees. It repairs only when `repair=true`. A repaired day stays in the
  report with `repaired: true`. A day whose derive failed counts as `unknown`,
  never as "disagree". The same route backfills missing days, which is how a
  cold 90-day view gets filled.

### Sealing, and why a sealed day is complete

* A UTC day can be sealed 15 minutes (`OUTCOMES_SEAL_GRACE_S = 900`) after it
  ends. Every tuple is a task that is already terminal, and the terminal
  writers stamp `completed_at` with their own clock. So after the grace,
  nothing more can land in that day.
* The write that seals a day is always a **full** re-derive, never an
  increment. That write also corrects anything a live cut missed.
* A **live** day (today, or yesterday inside its grace) caches a full derive.
  Its cut is `built_at − 120 s`, so that a commit landing a little late is
  still caught. Each read adds the delta since the cut, deduped by task id.
  The doc is rewritten at most once a minute per tenant.
* A bucket is `open` rather than `sealed` for the same 15 minutes after it
  ends. `in_progress` separately marks "the current period, so far".

### The read budget

* One request may spend 5,000 reads and 20 s building missing days. It builds
  the newest days first.
* Past the budget, the remaining days are `unread: derive_budget`.
* An incomplete payload is **not** cached. A re-request therefore continues
  from where the last one stopped, because what that one built was written.
* A complete payload is cached for 60 s, keyed by the minute. A cache hit keeps
  its original `generated_at`, so the age a reader sees stays true.

Estimated costs, not measured:

* A warm 30-day hourly view in tenant scope is about 31 day docs, plus the live
  delta, plus 4 counts.
* The "Workflows that failed" rows add up to 20 workflow docs and their step
  tasks, read through `WorkflowRollups.for_workflows`.
* A first build of a sealed day reads every task that ended or arrived that
  day, plus their attempts.

## Bucket boundaries are wall-clock instants in the viewer's zone

The server owns alignment and echoes the result. The UI never re-derives a
boundary.

* A day bucket can be 23 h or 25 h long.
* Across a fall-back hour there are two hourly buckets with the same label and
  different offsets. Across a spring-forward hour, the skipped hour has no
  bucket.
* A midnight that does not exist starts the day at the first instant after the
  gap. Santiago skips 00:00–01:00 on 6 Sep 2026, so that day starts at
  01:00-03:00.
* A midnight that happens twice resolves to its first occurrence. Havana on
  1 Nov 2026 is the example.
* Weeks start on ISO Monday, the same rule as `types.ts bucketStart`.

## The failure classifier, and how it retires

`classify_failure` and `cancel_cause` read text that other components wrote.
None of those components ship in the API image.

* **Exit 78 is the only exit code trusted on its own.** It is the worker's
  CANNOT-START (`ExitCode.CONFIG`, `reconciler.detect.WORKER_EXIT_CANNOT_START`),
  and this module is the third reader that contract request 21 anticipated.
* **Exit 76 is deliberately not read as "timeout".** On a timeout the worker
  records the killed runner child's status. Nothing raises ChildTimeout, so no
  attempt ever carries 76. The worker's timeout text is matched instead.
* Every `last_error` pattern is pinned to its writer's source, or to the
  writer's real function, by
  `tests/unit/control_plane/test_outcomes_classifier_parity.py`. A reworded
  message therefore turns CI red instead of draining into `other`.
* `other` and `no_reason` are always counted. Nothing is dropped.
* The retirement path is a typed end cause written by each terminal writer.
  It is filed as contract request 23. `mock` is named as a declared-cost
  profile in one place until request 24 gives `RunnerProfile` a flag for it.

**Known overclaim, open with the owner.** The scheduler writes "an upstream
workflow step did not succeed" when a parent is FAILED, DEAD_LETTERED **or
CANCELLED** (`scheduler/loop.py`, `_FAILED_PARENT_STATES`). So some cancels
labelled "after a failure" really followed a cancel.

## Tenant isolation (invariant 9)

* Tenant scope reads only documents whose id is built from `tenant_scope`'s
  resolved id.
* A `tenant_id` in the query string is not a parameter at all.
* Platform scope, `tenant`, `exclude_tenant` and `group=tenant_id` each go
  through `require_admin` **before** any parameter is validated. A non-admin
  therefore learns nothing about tenant ids, not even whether the ones they
  named exist.
* A pool admin is refused, because the route is not in `POOL_ADMIN_ROUTES`.
* `exclude_tenant` exists so that "exclude verify" does not also silently drop
  a tenant created later, which an include list would do.

## Indexes

Both are in `terraform/modules/firestore/indexes.tf` and asserted in
`tests/terraform/firestore.tftest.hcl`:

* **`tasks-tenant-completed`** (tenant_id ASC, completed_at ASC): the
  ended-tasks query.
* **Index exemptions on `outcome_days.ended` and `outcome_days.arrived`.** These
  fields are only ever read by document id.

Arrivals use the existing `tasks-tenant-created`. Attempts use `tenant_id ==`
plus `task_id in`, with no ordering, which merged single-field indexes serve.

## What is not verified

* Whether the equality-only `count()` behind `terminal_without_completed_at`
  (tenant_id, state, completed_at == null) is served without a composite
  index. The emulator does not enforce indexes. The proof is the release smoke
  against real Firestore.
* The read-cost figures above are estimates, not measurements.
* The API image's time zone database. `python:3.11-slim` installs `tzdata`. If
  it were missing, every zone would fail, and the route answers 503 naming the
  image, not 422 blaming the caller.

## Offboarding

`outcome_days` holds a tenant's task ids, profiles and submitter emails. So
`scripts/offboard-tenant.sh` counts it, deletes it by `tenant_id` (never by the
document-id prefix, which `eng` and `eng-x` share), and counts it again in the
proof. That follows the owner's 2026-09-24 decision that offboarding deletes
everything a tenant left.
