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
* Under `kind=standalone`, `workflows_failed` does not apply (`applicable:
  false`), and its three counts -- `with_ended_steps`, `with_failed_steps`,
  `rows_total` -- are null: nothing was counted, so no count is a zero. The
  review of #196 found them served as 0, which reads as "no workflow failed".
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
  `test_a_day_is_open_for_fifteen_minutes_after_it_ends_and_sealed_at_the_fifteenth`
  pins both edges: at 00:14:59 the day before is `open`, not in progress, and
  its stored day still live; at 00:15:00 it is `sealed`, by a full derive.
* A stored day is used only when BOTH its `derive_version` and its
  `classifier_version` are this module's. Each tuple carries the class its
  classifier gave it, so a day on another classifier holds classes this code
  would not assign; it is derived again on the next read that needs it. (Until
  2026-09-25 only `derive_version` was read, so the classifier's version was
  written and never acted on.)

### The read budget

* One request may spend 5,000 reads and 20 s building missing days. It builds
  the newest days first.
* Past the budget, the remaining days are `unread: derive_budget`.
* An incomplete payload is **not** cached. A re-request therefore continues
  from where the last one stopped, because what that one built was written.
* A complete payload is cached for 60 s, keyed by the minute. A cache hit keeps
  its original `generated_at`, so the age a reader sees stays true.

Read costs, measured on 2026-09-25 by the module's own meter over a read-only
snapshot of dev (4 tenants, 740 tasks, 481 attempts, 17 workflows). The meter
counts the way Firestore bills: a query costs its rows or 1 if it returns none,
a `get_all` costs one read per id asked for (a missing document included), and
a small `count()` costs 1. It is not a Cloud Billing figure.

| view | first read (derives) | warm (stored days) |
|---|---|---|
| one tenant, 30 days hourly, no failed workflow in the span | 68 | **36** |
| eng, 30 days hourly, 6 failed workflows in the span | 217 | **185** |
| platform, 14 days daily, 4 tenants | 2,250 | 221 |

* So the 35–50 estimate holds for a tenant view with no failed workflow: 31
  day docs, the live delta and 4 counts.
* The "Workflows that failed" rows are the rest. On dev they cost 6 workflow
  docs plus 143 step tasks, read through `WorkflowRollups.for_workflows`.
* A first build of a sealed day reads every task that ended or arrived that
  day, plus their attempts. A cold 14-day platform view spent 2,250 of its
  5,000 budget.

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

## Why a task ended: its typed cause first, its text only without one

Contract requests 23 and 24 were accepted by the owner on 2026-09-25 (#185,
decision 9) and applied in PR #217.

* **`Task.end_cause` is read first.** Every terminal writer records it beside
  `completed_at`: the worker, the reconciler, the scheduler and the API's
  cancel. A task that carries one is classified by it and by nothing else --
  a cause the image does not know is `other`, never re-guessed from the text.
  The text classifier below is the FALLBACK, for tasks that ended before the
  field existed (every task on dev on the day it shipped) and for the one end
  no cause names (the runner stopped on SIGTERM with no cancel requested).
* **The failure classes**, in their fixed order: runner error, timeout, lost
  worker, could not start, **inputs unavailable**, outputs missing, dispatch
  failed, other, no reason recorded. "Inputs unavailable" is decision 4: the
  worker refusing to stage a declared `input_from` artifact before the agent
  starts is not the runner's error. It sits before "outputs missing" because
  an attempt meets them in that order.
* **The cancel causes**: requested, after a failure, **after a cancel**,
  workflow sweep, other. "After a cancel" is decision 2 (below).
* **Declared cost is the catalogue's.** `DECLARED_COST_PROFILES` is every
  profile whose `RunnerProfile.cost_declared` is set (request 24); the module
  names none.
* `DERIVE_VERSION` and `CLASSIFIER_VERSION` went 1 -> 2 with this, so every
  stored day is derived again under the new rules.

### A cancel's cascade is not a failure's (decision 2)

The scheduler writes "an upstream workflow step did not succeed" when a parent
is FAILED, DEAD_LETTERED **or CANCELLED** (`scheduler/loop.py`,
`_FAILED_PARENT_STATES`), so the text alone counted a cancel somebody pressed
as a failure. Two halves close it:

* the scheduler now records `failed_parent` or `cancelled_parent` from the
  parents it read when it cancelled (a failure wins when a step had both);
* a task without that cause is split AT DERIVE TIME by the states of its
  `depends_on` parents -- the documents the wait figure already reads, so it
  costs no read. A FAILED or DEAD_LETTERED parent makes it "after a failure",
  else a CANCELLED one "after a cancel". With no parent readable in either
  state the text cannot say which, and it is `other`: never a guessed failure.

`workflows_failed.rows[].cascade_cancelled` counts only the failure's cascade
(after a failure and the fail_workflow sweep). A step that followed a CANCELLED
parent was stopped by a person, not by the failure.

### The text classifier (the fallback)

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
* Every `InputUnavailable` message `agent_worker/inputs.py` raises opens with
  its own literal, and the worker ends the task with the message as the whole
  `last_error`; the parity test renders the opening of EVERY raise there and
  holds each to "inputs unavailable".
* `other` and `no_reason` are always counted. Nothing is dropped.

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

## Measured against real Firestore

The emulator does not enforce composite indexes, so each query shape was run
once against dev (`saga-agents-staging`, database `swarm`) on 2026-09-25,
**read-only**, through the module's own methods and the real client. Every
write RPC was refused in the client before anything was sent.

* **The ended-tasks query cannot run without `tasks-tenant-completed`.** It
  failed with `FailedPrecondition: The query requires an index`, and the index
  Firestore asked for is (tenant_id ASC, completed_at ASC, `__name__` ASC):
  the Terraform index, plus the `__name__` field Firestore adds itself. Until
  that index is built, every derive fails. Each day is then `unread:
  read_failed` and every bucket carries no numbers, which is the honest
  failure, never a row of zeros.
  * In `release.yml`, `deploy` needs `infrastructure`. So on the release path
    the index exists before the route ships.
  * A deploy by hand that skips Terraform serves every bucket unread until
    the index is applied.
* **Everything else runs on indexes that already exist:**
  * the arrivals query on `tasks-tenant-created`;
  * `tenant_id ==` plus `task_id in` on attempts;
  * the tenant-checked `get_all` of parents and of `outcome_days`, where a
    missing document comes back with `exists` False;
  * `count()` over (tenant_id, state, completed_at == null), both
    tenant-scoped and over the whole platform, with no composite index.
* **An `in` takes at most 30 values.** 30 streamed; 31 failed with
  `InvalidArgument: 'IN' supports up to 30 comparison values.` That limit is
  why attempts are read in chunks of 30.
  * `tests/unit/control_plane/fakes.py` now refuses a larger `in` the same
    way. The emulator already did.
  * A 35-task day is read in both suites. Raising the chunk to 100 turned
    exactly those two tests red (run 36196177221, reverted).
* `tests/integration/test_outcomes_emulator.py` runs the route through the
  real client against the emulator on every PR. It covers the queries, the
  IS_NULL count, the rollup batch, the Timestamps read back, and the drift
  route.

Still not verified:

* **The index build itself**, which is a Terraform apply.
* **A single batch near the 10 MiB request cap.** That is a write, and the
  read-only probe could not make one.
* **The API image's time zone database.** `python:3.11-slim` installs
  `tzdata`. If it were missing, every zone would fail, and the route answers
  503 naming the image, not 422 blaming the caller.

## What dev showed against #185's checks (2026-09-25)

The mockup's figures were read at 19:21 UTC by **submission** day. This route
places work by **completed_at**. Both give the same headline on dev:

* At the mockup's read time, completed_at gives 272 of 300, 90.67 %
  (0.8684–0.9346). That is the mockup's figure exactly. The two bases agree
  because every task on dev was created after the span began (the first on
  16 Sep). So the tasks that had ended by that time are exactly the tasks
  whose `completed_at` falls before it. At 22:03 UTC the same span read
  278 of 306, because six more had succeeded since.
* **The time zone moves 22 Sep's failures, as it should.** In UTC, 22 Sep ended
  25 succeeded, 305 cancelled and 8 failed, which is the mockup's day, and 338
  were submitted. In Europe/Bucharest, 7 of those 8 failures ended between
  00:00 and 02:00 local on 23 Sep. So Bucharest's 22 Sep reads 14 / 305 / 1,
  and its 23 Sep has 13 failures. The cancels have their own lane either way,
  so they no longer hide a failure.
* Every figure the route served over that snapshot matched a direct count
  written without the module: each bucket, the rate and its interval, cancel
  causes, retries, cost, per-profile latency, groups, and the first failed
  step of each workflow.
* **The failure split is not the mockup's.** On dev: runner error 13,
  dispatch failed 7, could not start 4, lost worker 3, other 1, and no
  timeout at all. So the mockup's "other" was not hiding timeouts.
  * The mockup's "outputs missing 6" are the downstream half of #149: 10 of
    the 13 runner errors are the worker refusing to stage an input
    (`inputs.InputUnavailable`: "upstream task … did not produce an artifact
    named …", and "upstream tasks … all stage …").
  * The contract put any worker-written end with an exit code under
    "runner error". The owner decided on 2026-09-25 that input staging is its
    own class (#185, decision 4); `CLASSIFIER_VERSION` and `DERIVE_VERSION`
    went to 2 with it, so dev's stored days are derived again and these 10
    read "inputs unavailable".

## Offboarding

`outcome_days` holds a tenant's task ids, profiles and submitter emails. So
`scripts/offboard-tenant.sh` counts it, deletes it by `tenant_id` (never by the
document-id prefix, which `eng` and `eng-x` share), and counts it again in the
proof. That follows the owner's 2026-09-24 decision that offboarding deletes
everything a tenant left.
