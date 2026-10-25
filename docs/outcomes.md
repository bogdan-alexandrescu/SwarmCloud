# The outcome ledger: `GET /v1/outcomes`

What the platform's work in a span **ended as**, bucketed by **when it ended**.
It feeds Work › Timeline. The owner's decisions are in issue #185 (2026-09-25);
the code is `apps/swarm-api/swarm_api/outcomes.py`, the only implementation.

This page records why the route is shaped the way it is, so the shape is not
quietly reverted.

## Why it exists

The old Timeline stacked outcomes (placed by the day work ended) on top of
still-open work (placed by the day it was submitted) in one column, over the
newest 200 agents. So a column's height measured nothing. On dev, 16–25 Sep
2026, 416 cancels flattened 28 failures, and 22 Sep's 305 cancels hid that
day's 8 failures.

## One basis per figure

* Every outcome figure is placed by the agent's **`completed_at`**.
* Exactly one series is not: **`submitted`**, the throughput lane's arrivals,
  placed by **`created_at`**. The owner added that lane to answer "are we
  keeping up".
* The response names both under `basis`, so no client has to restate which is
  which.
* **Cost is placed by the agent's end**, not by when an attempt ran. The worker
  records spend before its terminal write (`lifecycle._upload_outputs` calls
  `_record_spend` first, and `_cleanup` is the crash backstop). So a finished
  agent's spend is complete, while an open agent's spend is not yet known.
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
* A bucket before a tenant's first agent holds sealed zeros. That is a real
  measurement: agents are never TTL'd, and the derive covers every agent.

## Derive, write, drift check

This is the shape the owner chose for workflow state (`rollup.py`), for the same
reason: a written value and a derived value are two records of one fact.

* **DERIVE.** `derive_day(tenant, utc_day)` does five reads, and each one leads
  with `tenant_id == T`:
  * the tenant's agents that ended that day;
  * the agents that arrived that day;
  * their attempts;
  * their workflow parents, read with `get_all` and checked against the tenant;
  * the ended agents' account events, for the pool account (#2149, below).

  It turns each agent into a small tuple. A parent that is missing or belongs to
  another tenant counts as **unread**. That agent's wait is excluded
  (`coverage.wait_excluded`, and `latency.wait_excluded` for the card that
  prints it), never guessed.
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
  ends. Every tuple is an agent that is already terminal, and the terminal
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

### The nightly seal: the workflow-rollup job (COUNTS-SEAL, #2149)

BGD's decision, 2026-10-23: `outcome_days` is "sealed nightly by the existing
rollup job", so a 12-month view on Admin › Platform counts reads sealed days
instead of spending a reader's derive budget on them. Before this, a day was
sealed only when somebody happened to read it after its grace.

* **Which job.** The existing `workflow-rollup-<tenant>` Cloud Scheduler job
  (`terraform/modules/scheduler/jobs.tf`, every 15 minutes) calls
  `POST /v1/admin/workflows/rollup?tenant_id=T&seal_outcomes=true`. The flag
  makes the route call `Outcomes.seal_recent` before its workflow sweep.
* **Why that job and not a new tick.** It already calls swarm-api as the
  `swarm-rollup-sweeper` identity, which swarm-api admits to that route
  (`auth.ROLLUP_SWEEPER_ROUTES`), and which already holds its one grant,
  `run.invoker` on swarm-api. So the seal needs no new job, no new route on
  the allow-list and **no new IAM binding**. A new tick on
  `POST /v1/admin/outcomes/rollup` would have needed that route added to the
  sweeper's allow-list, which widens what a leaked sweeper token reaches.
* **What one call does.** It looks at yesterday and the two days before it
  (`OUTCOMES_SEAL_LOOKBACK_DAYS = 3`), newest first. A day still inside its
  15-minute grace is `not_yet`. A day already sealed at this module's versions
  is `already_sealed` and costs its one stored-doc read. Any other day is
  sealed by a full derive, exactly what the next read would do. The two older
  days are there so a night the job missed is sealed by the next tick.
* **Never today.** The live day is not in the window, so it is never written
  or rewritten by the seal.
* **Idempotent.** A second call writes nothing: every day it looks at is
  `already_sealed`. Unlike the drift check, the seal never re-derives a sealed
  day, so a tick does not re-read history.
* **Bounded.** At most 3 days per call, inside the backfill's 31-day cap. No
  derive starts once the call has spent the derive budget (5,000 reads or
  20 s). A day it did not reach is `deferred` and is sealed by the next tick.
  It is never written as a zero.
* **Logged.** One line per tenant per call: `outcome seal tenant=T sealed=..
  already_sealed=.. not_yet=.. deferred=.. unknown=.. reads=..`. The route
  returns the same report under `outcomes`.
* **It never fails the sweep.** The seal runs first and never raises. A
  failure is `outcomes.error` in the response, and the workflow sweep still
  runs. An unregistered tenant is `skipped: unknown_tenant`, not sealed.
* **What it does not reach.**
  * A personal `u-*` tenant is not in `var.rollup_tenant_ids`, so no job
    seals its days. They seal on the next read past their grace, as before.
  * Days older than three days are not sealed by the job. A cold 12-month
    view still needs the admin backfill (`POST /v1/admin/outcomes/rollup`,
    31 days per call) once, after which the job keeps it sealed. That
    includes the one-off re-derive of every v8 day to v9 (below).

### The read budget

* One request may spend 5,000 reads and 20 s building missing days. It builds
  the newest days first.
* Past the budget, the remaining days are `unread: derive_budget`.
* An incomplete payload is **not** cached. A re-request therefore continues
  from where the last one stopped, because what that one built was written.
* A complete fold is cached for 60 s, keyed by the minute. A cache hit keeps
  its original `generated_at`, so the age a reader sees stays true.
* What is cached is the FOLD, not a payload (owner decision 2026-09-30, the
  review of #391). The key is the query WITHOUT its `section` list -- scope,
  the caller's own tenant in tenant scope, the resolved tenant set, span,
  bucket, tz, filters, kind, compare and the minute -- and every
  section is a projection of the entry. `group` is not in the key either
  (BGD, 2026-10-24 17:26Z, #2149): `groups` and `usage` are made from the
  same placed agents by the request's own group, so picking a breakdown
  never costs a read, and `filters.group` echoes the request, not the entry. The previous span, the terminal count
  and the workflow lookups are made once per entry, the first time a section
  needs them. So the Timeline's headline and seven cards, each asking for its
  own sections, cost one full read between them, plus what every request pays
  (authentication, the tenant check, and the tenant listing in platform
  scope). `reads` is what that request read; `cached` means it read nothing
  for its sections.
* `coverage.derived_now` counts the main span's days only. The previous span
  is read under a derive budget of its own, only when `previous` is asked for,
  so `coverage` is the same value whether or not `previous` came with it.
  #391 as first written counted both, and a `coverage`-only read on a cold
  store reported fewer days built than the full read did.

Read costs, measured on 2026-09-25 by the module's own meter over a read-only
snapshot of dev (4 tenants, 740 agents, 481 attempts, 17 workflows). The meter
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
  docs plus 143 step agents, read through `WorkflowRollups.for_workflows`.
* A first build of a sealed day reads every agent that ended or arrived that
  day, plus their attempts. A cold 14-day platform view spent 2,250 of its
  5,000 budget.

## Agents over time: the `usage` block (#2149)

Admin › Platform counts asks "how many agents ran per day, week or month, of
which type, for how long, on how many tokens, at what estimated cost". BGD's
decisions, 2026-10-23 (~19:30Z), and how the route keeps them:

* **The unit is agents, retries shown apart.** `usage=submitted` places each
  agent in the bucket of the day it was **submitted** (`created_at`) and
  counts it once (`agents`). Its attempts beyond the first are `retries` in
  the same bucket. `usage=ended` places agents by `completed_at` instead, the
  basis of every outcome figure. `usage.basis` names the one used.
* **Breakdowns** are `group`: `runner_profile` (agent type), `model`,
  `model_class`, `tenant_id` (workspace), and two the Timeline does not offer
  yet, `pool_account` and `provider` (`COUNT_GROUPS`). They are kept out of
  `GROUPS` because `test_outcomes_ui_field_contract.py` holds `GROUPS` to the
  Timeline's own picker; they move in when the Timeline draws them.
* **Each bucket, each group row and the totals carry:** `agents`,
  `agents_ended`, `retries`, `attempts`; `run_s {sum, measured, attempts}`;
  the four token counts, each `{sum, reporting, attempts}`; and `cost {sum_usd,
  estimated_usd, estimated, agents, reporting}` under `cost_label`, "Estimated cost (list
  price, not a bill)".
* **Every breakdown comes with every read.** `usage.breakdowns` carries
  `runner_profile`, `model`, `tenant_id` and `pool_account`
  (`USAGE_BREAKDOWNS`) from the one fold, whatever `group` asked for;
  `usage.groups` stays the requested group, which may also be one of the
  others (`submitted_by`, `model_class`, `provider`). BGD's answer 3,
  2026-10-24 17:26Z: picking a breakdown must never cost a read. Each
  grouping is `{by, rows_total, rows, series}`.
* **Without `usage` the response is unchanged**, byte for byte. The Timeline
  does not send it, and `test_outcomes_counts_rollup.py` holds that. `usage`
  is not a section. It is a projection of the same fold, so it costs no read
  of its own and is not part of the cache key.

### Per-bucket groups: `series` (#2149, BGD's answer 1)

The view stacks agents by group per bucket (mock-up A, frames F3 and F4), so
each grouping carries `series`, aligned with `usage.buckets`:
`{key, measured, other, keys, agents: [...], retries: [...]}`.

* **The same five in every bucket.** The 5 busiest measured keys over the
  WHOLE span (`USAGE_SERIES_NAMED`, by agents, ties by key) are named. A
  bucket's own top five would reshuffle the stack's colours bucket by bucket.
* **The rest is one series**, `"all others"` (`OTHER_KEYS`, `other: true`,
  `keys` says how many it folds), present only when there are more than
  five measured keys. Its key has a space, which no profile, model, tenant
  or account ID has.
* **An unknown is never stacked** (charts README). A key that says a value
  was not measured -- "not recorded", "not ended", and "unread" under
  `pool_account` -- is a series of its own whose `measured` is false, never
  named and never inside "all others". "not started", "no class", "no
  provider" and "holds no account" are facts, and stack (`measured: true`).
* **They add up.** Every agent is in exactly one series, so in a read bucket
  the series' `agents` sum to the bucket's `agents` and their `retries` to its
  `retries` (an agent's attempts beyond its first, as everywhere). An unread
  bucket is null in every series, as it is in the bucket row.

Payload, measured 2026-10-24 by `fold()` over a synthetic 26-week weekly read
of 3 workspaces (11,299 agents; 12 profiles, 5 model keys, 3 workspaces, 7
pool-account keys): the `usage` block grew from about 22 KB to 44 KB, and the
whole response from about 56 KB to 77 KB, uncompressed. The growth follows
the number of keys and buckets, not the number of agents: span rows are capped
at `GROUP_ROWS_MAX` per grouping and each series is two integers per bucket.

### Why null is not 0, again

* A token sum is null when no started attempt reported that count. 0 is a
  report: `mock` spends nothing on purpose. `reporting` of `attempts` is
  "1 of 2 attempts reported", which is what a reader needs in order to
  write "at least".
* An attempt with no `completed_at` is **not measured**, never 0 s. Its run
  time is not known (a worker that died mid-run never wrote one), and a zero
  would pull every average down. `run_s.measured` of `run_s.attempts` says
  how many attempts were measured.
* Cost is `sum_usd` over the agents that reported one, and `reporting` of
  `agents` is "k of n agents reported". When k < n, the UI writes "at least
  $X". An agent that reported nothing is never $0.
* `estimated_usd` is the part of the sum that came from the worker's
  list-price estimate for a run stopped before it reported (#627,
  `result_summary.cost_estimated`). It is **included** in `sum_usd` and also
  shown on its own, so a reader can see how much of the figure is an estimate.
* `cost.estimated` (BGD's answer 2, 2026-10-24) counts the agents behind
  `estimated_usd`, wherever it is served (span, bucket, group row), so the
  view can print "55 estimated from a stopped run ($56)". It counts only
  agents that reported a cost, the same agents `estimated_usd` sums, and in a
  read bucket it is a count: 0 means none, because every v9 tuple stores
  `cost_estimated`. An unread bucket's `cost` is null, as before.
* Under `usage=submitted`, an agent whose ended tuple is not in the span's
  read days counts in `agents` and in no measured figure. That agent is still
  open, or it ended after an explicit `until`. `agents_ended` says how many
  were measured. Its `model` and `pool_account` key is "not ended".

### Unused browser time: `browser` (#2322, BROWSER-USE-FLAG)

The 2026-10-25 audit of tenant eng found 49 browser agents that never touched
their browser, and found them only by reading agent logs by hand: the worker
counted screenshots and nothing else. So every usage row (span, bucket and
group row) carries `browser`, and the console reads unused browser hours off
the same `GET /v1/outcomes?usage=...` it already calls for the range.

* **Who decides "used".** The worker, on the attempt that ran the runner
  (`lifecycle.browser_use_record`). It writes `result_summary.browser_used`,
  which is true, false or null, and `browser_use`, the counts. A call is a
  browser MCP tool call (Playwright, jev) or a Bash command that drives the
  browser or the gate: `swarm-handoff serve|device|theme|record`,
  `dev-browser`, or a script through `swarm_browser`. An image under the
  agent's artifacts also counts, because some agents render through a script
  with no tool call at all. `GET /v1/tasks/{id}` and every list row serve both keys
  (`codec.browser_use_to_api`).
* **Null is not false.** Null means one of two things. Either the profile
  runs no live browser (`RunnerProfile.live_browser`; today only
  `claude-code-browser` does), or the counts could not be read. The second
  covers no report, an unread stream, and a cut stream that shows no call:
  a lower bound of 0 is not a measurement. Such an agent counts in
  `unknown_agents`, and in neither `unused_agents` nor `unused_s`.
* **The seconds are the run time.** `run_s` is the summed `run_s` of the
  browser-runtime agents, from their attempts, as the usage block's own
  `run_s`. `unused_s` is the part whose agent the worker said did not use the
  browser. It is 0.0 when every measured browser agent used it, and null
  only when no browser agent in the row has a measured run.
* **Version 10.** The tuple stores `browser_s` and `browser_used`, so every
  day stored under 9 is derived again once, at the cost set out under "What
  v9 costs to re-derive" below, with no extra read: the verdict is on the
  task document the derive already reads.

### The pool account comes from the agent's own events

The worker records the account it leased as an event on the agent
(`account_assigned`), never as a field of the agent's document. So the
derive reads it the way `task_accounts.py` reads it for the agent pages: the
same collection-group query over `events`, one per 30 ended agents, on the existing
`events-tenant-task-cause` index, with the tenant checked on every row. No
change to the frozen contract was needed.

* **The label** is the account the agent's final attempt held, or, when that
  attempt held none, the latest earlier attempt that held one
  (`task_accounts.last_account_id`). An agent whose events name no account
  is "not recorded" -- when it could have held one.
* **"holds no account"** (BGD's answer 4, 2026-10-24) is the key of an agent
  that never takes a pool account: a profile with no provider (mock), one
  that takes no subscription token (browser, codex, merge), or an agent never
  admitted. It is `task_accounts.could_hold_account` applied at READ time to
  the tuple's `profile` and `attempt_count`. `attempt_count` is raised in the
  same admission write that sets `current_generation`, so it is > 0 exactly
  when the generation is. Every v9 day already stores both columns, so no
  sealed day is re-derived and no agent is read again for it. An agent that has not
  ended is judged by its profile alone: an open mock agent "holds no
  account", an open claude-code agent is "not ended". The rule reads today's
  profile catalogue, which is in the frozen contract. A profile that changed
  whether it takes a token would re-key its past agents; none has.
* **An agent that cannot hold an account costs no read.** That is a profile
  with no provider, one that takes no subscription token, or an agent never
  admitted (`task_accounts.could_hold_account`, `_static`'s rule).
* **A failed events read fails the derive.** The day is then `read_failed`
  and is derived again on the next read. A list page shows such an account as
  `unread` and moves on, but a sealed day is never read again, so an account
  it missed would be missing for good. An agent past the reader's cap of 64
  events per agent is "unread", never a guess.
* **A sealed day never re-reads events.** The label is stored in the tuple.

### What v9 costs to re-derive

Every stored day was derived under `DERIVE_VERSION` 8, which has no usage
columns, so each one is derived again once: by the next read that needs it,
by the admin backfill, or, for the last three days, by the workflow-rollup
job's nightly seal. A re-derive costs what a first build costs (see the
measured table above): every agent that ended or arrived that day, their
attempts and their parents. On top of that come the events: one query per 30
pooled ended agents, billed per account event returned, about one or two per
attempt. On the 2026-09-25 snapshot (740 agents, 481 attempts), a cold 14-day
platform view was 2,250 reads. v9 adds one event query per tenant-day that
ended a pooled agent (one more per 30 such agents), each billed by the account
events it returns, about one per pooled attempt. So the estimate is at most
56 queries (14 days times 4 tenants) plus at most about 481 event rows, which
is under 550 more reads. That is an estimate, not a measurement. The platform
has grown since that snapshot, and today's figure was not measured.

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

## Why an agent ended: its typed cause first, its text only without one

Contract requests 23 and 24 were accepted by the owner on 2026-09-25 (#185,
decision 9) and applied in PR #217.

* **`Task.end_cause` is read first.** Every terminal writer records it beside
  `completed_at`: the worker, the reconciler, the scheduler and the API's
  cancel. An agent that carries one is classified by it and by nothing else --
  a cause the image does not know is `other`, never re-guessed from the text.
  The text classifier below is the FALLBACK, for agents that ended before the
  field existed (every agent on dev on the day it shipped) and for the one end
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
  stored day is derived again under the new rules. (`DERIVE_VERSION` is 10
  today; each step is recorded beside the constant in `outcomes.py`.)

### A cancel's cascade is not a failure's (decision 2)

The scheduler writes "an upstream workflow step did not succeed" when a parent
is FAILED, DEAD_LETTERED **or CANCELLED** (`scheduler/loop.py`,
`_FAILED_PARENT_STATES`), so the text alone counted a cancel somebody pressed
as a failure.

**A parent's own end decides, not its state.** The dependency rule is
transitive: a step cancelled after a failure is itself a "failed parent" to its
own dependants. So under `continue`, `a` FAILED -> `b` -> `c` -> `d` cancels
`b` after `a`, then `c` after `b` (CANCELLED), then `d` after `c`. Read by the
parent's state, `c` and `d` were "after a cancel" -- a cancel nobody made --
and left out of the workflow's failure cascade. The first cut of this change
did exactly that, one hop deep, and the review of #217 caught it before
release. Two halves now close it:

* the scheduler records `failed_parent` when any parent FAILED or was
  DEAD_LETTERED, or was CANCELLED **by a failure** (its own cause
  `failed_parent` or `workflow_sweep`); `cancelled_parent` only when every
  cancelled parent was ended by a requested cancel (`cancel_requested`,
  `cancelled_parent`, or the cancel flag on a parent from before causes). A
  parent cancelled before causes, with no flag, has an end the scheduler
  cannot name, and the cascade is written with **no cause**, for the ledger;
* an agent without a cause is split AT DERIVE TIME by what each `depends_on`
  parent's end sent down: a FAILED or DEAD_LETTERED parent, or one cancelled
  after a failure or by the sweep, sent a failure; one cancelled on request or
  after a cancel sent a cancel. A parent that is itself an untyped cascade is
  named by its own parents, and so on up: the derive reads the chain above
  such an agent (`_read_cascade_ancestors`), one batched, tenant-checked
  `get_all` per level, only for cascades that carry no cause -- every agent on
  dev when this shipped, none that ends after it. A failure anywhere wins; a
  parent whose end cannot be named (unreadable, or cancelled for no recorded
  reason) makes the step `other`, because it may have been the failure; only
  then does a cancel make it "after a cancel". Nothing read: `other`.

`workflows_failed.rows[].cascade_cancelled` counts only the failure's cascade
(after a failure, however far down, and the fail_workflow sweep). A step
"after a cancel" followed a cancel somebody asked for, at its parent or
further up -- not the failure.

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
  its own literal, and the worker ends the agent with the message as the whole
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
The pool account's events read (#2149) uses the existing
`events-tenant-task-cause`, the index `task_accounts.py` already needed.

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
  * A 35-agent day is read in both suites. Raising the chunk to 100 turned
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
  because every agent on dev was created after the span began (the first on
  16 Sep). So the agents that had ended by that time are exactly the agents
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

`outcome_days` holds a tenant's task ids, profiles, submitter emails and, since
v9, the pool account ids its agents ran on. So
`scripts/offboard-tenant.sh` counts it, deletes it by `tenant_id` (never by the
document-id prefix, which `eng` and `eng-x` share), and counts it again in the
proof. That follows the owner's 2026-09-24 decision that offboarding deletes
everything a tenant left.
