# Schedules: typed recurring jobs per repository and tenant, with gates and approvals

**Status: DESIGN, 2026-10-08 (lane SCHED0, part of #892). None of it is
built.** The owner asked on 2026-10-08:

> "we need a way to view and fully manage crons like this one above for self
> improvement or auto planning and executing and merging open github issues
> and more. This should be a section in the UI as well and maybe shown in new
> runs or maybe below runs in the main menu. Or come up with your preferred
> approach to manage these recurring jobs, the types of jobs we should support
> and the required gates and human approvals if needed. Make it so that its
> also supported per repo and fully multi tenant."

The "cron above" is the operator's session cron (job `bdb47d30`, 2026-10-08),
which sweeps open issues from one person's Claude session. It dies with that
session, expires after seven days, and nobody else can see it. This document
designs what replaces it. The screens are drawn, in two or three variants each,
in [web-ui/mockups/schedules.html](web-ui/mockups/schedules.html). The owner's
open decisions are §10, and the same questions are in this lane's
`questions.json` artifact.

What it settles, one line each:

* **A schedule is a tenant's Firestore document,** `schedules/{schedule_id}`.
  It names a **type by name** from a fixed catalogue, a repository scope, a cron
  expression with a timezone, the type's parameters, a gate, a budget and an
  owner. It is not a frozen-contract change (§1).
* **One platform tick fires every due schedule.** One Cloud Scheduler job runs
  every minute for all tenants, personal `u-*` tenants included. Each due slot
  is claimed **exactly once** by creating a firing document in a Firestore
  transaction. There is no GCP job per schedule (§2).
* **A firing creates ordinary work:** an issue run, a task or a workflow,
  submitted as the schedule's owner in the schedule's tenant, marked
  `metadata.schedule`, and admitted like anything else. A few types make only
  GitHub API calls and no agent work; they run inside swarm-api, use no
  capacity, and are recorded the same way (§2.7, SD8).
* **Twelve types**, from `issue-sweep` to `custom-prompt`. Each has
  parameters, a default gate, an output and a blast radius. The issue sweep
  that lane SWEEP is building **becomes** the `issue-sweep` type (§3, §8).
* **A gate is a risk tier plus an approval mode.** Each type sets a floor that a
  tenant can make stricter but never looser. Some **hard stops** apply whatever
  the mode: IAM and Terraform bootstrap changes, frozen-contract edits,
  `.github/workflows/`, security-class issues, budget exhaustion and repeated
  failure (§4).
* **One approvals inbox** for plan, run, merge and proposal approvals, across
  schedules and issue runs. Each approval is an approval of a **digest**, as
  issue runs approve plans (D3). A pending approval is a document and holds no
  capacity, and it expires (§4.4-§4.7).
* **Fully per tenant (§5).** A tenant sees and edits only its own schedules,
  and every firing uses that tenant's identity, secrets, prefix and ceilings.
  Admins get a cross-tenant view that can pause but cannot edit or approve.
* **Work › Schedules, after Runs**, with the inbox as its second pane and an
  admin view under Admin (§6). Also a REST API, MCP tools and `sc schedules`
  (§7).

`eng` is an example tenant and `example-org/example-api` an example repository.
`u-alice` is a personal workspace, as in [workspaces.md](workspaces.md).
Dollar figures given as defaults are proposals for the owner, not
measurements (§10, SD4).

---

## 0. Today, re-read 2026-10-08 against `main` (cbba982)

* **Recurring work runs from one person's session.** Job `bdb47d30` is a
  `CronCreate` job in the operator's Claude session. It is session-only, it
  expires after seven days, and nobody else can see, pause or audit it
  (#892).
* **Platform ticks are one Cloud Scheduler job per purpose, mostly one per
  tenant.** `terraform/modules/scheduler/jobs.tf` declares `safety_tick`,
  `reconciler`, `quota_refresh` and `forge_refresh` once each, and
  `workflow_rollup`, `issue_run_advance`, `repo_index_poll` and `merge_wake`
  once per tenant in `var.rollup_tenant_ids`. The per-tenant ones call a
  `/v1/admin/...?tenant_id=` route as the `swarm-rollup-sweeper` OIDC identity,
  which swarm-api admits to those routes only
  (`apps/swarm-api/swarm_api/auth.py::ROLLUP_SWEEPER_ROUTES`).
  **A personal `u-*` tenant is in none of the per-tenant ticks** because it is
  not in `var.tenants`. Its issue runs move only when someone reads them
  ([issue-runs.md](issue-runs.md#the-cloud-scheduler-tick-and-why-every-read-also-advances)).
* **The repository index already has a per-repository recurring job.**
  Each registration carries `index.interval_hours` (default 24),
  `index.on_change` (`poll`), `index.min_change_interval_minutes` (default 30),
  `index.full_every_days` and `index.paused`. The 5-minute `repo_index_poll`
  tick reads each head with an ETag and queues an index run when the head has
  moved or the interval has passed
  ([repo-index.md §3.3](repo-index.md#33-triggers-an-interval-and-a-change-on-the-default-branch)).
* **The issue run has the gate shapes this design reuses.**
  - `plan_approval: auto | required`.
  - `PLANNED` is a document and nothing else, so waiting holds no capacity.
  - Approval is of a plan **digest**, and a mismatch answers 409 `plan_changed`.
  - `fix_rounds` is 1-5, default 3.
  - `auto_merge` merges through one merge-only continuation, once CI is green
    and the review said MERGE (`apps/swarm-api/swarm_api/issueci.py::_merge`).
  - Any member of the run's tenant may approve.

  See `apps/swarm-api/swarm_api/issueruns.py` and [issue-runs.md](issue-runs.md).
* **Work submitted on someone's behalf is submitted as a stored member.**
  `apps/swarm-api/swarm_api/routes/runs.py::run_owner_auth` and
  `apps/swarm-api/swarm_api/routes/admin.py::registration_owner_auth` build the
  submitter from the stored creator, never from the tick's identity. They ask
  the directory again on every submission whether that person is still a
  member.
* **There are no dollar budgets** (owner decision 2026-10-01,
  [cost-control.md §2](cost-control.md#2-ceilings-you-actually-own)).
  - `monthly_budget_usd` is refused with a 422.
  - `PARKED(BUDGET_EXHAUSTED)` is never written.
  - Per-attempt `cost_usd` is recorded **when the attempt ends**, and only when
    the runner reported one.

  Concurrency ceilings are the only spend control applied before work runs.
* **Admins live in Firestore** (`admin_roles/`, `admin_audit/`, PR 860,
  `apps/swarm-api/swarm_api/admins.py`). `PLATFORM_OWNER` is the owner, and
  admin rights are platform-wide. **There is no tenant-admin role**: inside a
  tenant every member is equal.
* **A new refusal ships report-only** (PR 873,
  `apps/swarm-api/swarm_api/refusals.py`): it is logged under its own code and
  turned on later with `REFUSAL_<CODE>=on`.
* **The forge credential cannot push `.github/workflows/`.** Workflows write is
  not among the App's permissions ([onboarding.md D8](onboarding.md#d8-what-may-swarmcloud-do-as-the-user-beyond-clone-push-and-pull-request),
  option (a)), and the worker's pushes of workflow files were refused on
  2026-10-08.
* **Security-class issues need the owner's OK for their plans** (owner decision,
  2026-10-08).
* **Lane SWEEP is in flight and not on `main`** at cbba982 (`git grep -i
  issue_sweep` finds nothing in `apps/` or `terraform/`). Its brief:
  - a Cloud Scheduler `issue_sweep` job, shipped **off**;
  - the planner decides readiness, with a `NOT_READY` verdict;
  - at most **8 live runs**;
  - a **territory guard**, so two runs do not edit the same files.

  §8.1 is written against that brief. The build lane re-reads SWEEP's merged
  code before it starts.
* **The console's Work section has ten tabs:** Agents, Workflows, Runs,
  Timeline, Repositories, Setup, Access and the three Submit screens
  (`apps/swarm-ui/src/App.tsx` `SECTIONS`).
  `tests/unit/scripts/test_issue_forms.py` binds the issue forms' "Where" list
  to that array.
* **No cron library is a dependency** of any app.

---

## 1. The model

### 1.1 `schedules/{schedule_id}`

The collection is new, kept by its own module (`swarm_api/schedules.py`) and
read and written there only. It does not go through `store.py` or `codec.py`,
for the reason `issue_runs` gives: its shape is not the frozen contract's and
must not leak into it.

| field | type | written by | meaning |
|---|---|---|---|
| `schedule_id` | string | swarm-api | `sch_` and 12 hex digits from `secrets.token_hex(6)`. Random, so an id says nothing about its tenant. It equals the document id |
| `tenant_id` | string | swarm-api | The owner tenant, resolved from the caller exactly as a task's is. **Never a body field.** Every read compares it with the caller's tenant and answers a mismatch with the same 404 as a missing schedule, as `issue_runs` does |
| `name` | string | caller | 1-80 characters, unique within the tenant. It is what the console lists |
| `type` | string | caller | A name from the catalogue (§3), e.g. `issue-sweep`. **By name, never an image, command or profile** (invariant 10). An unknown or unavailable type is a 422 that names the available ones |
| `scope` | map | caller | `{mode: "repos" \| "all", repo_ids: [...]}`. `repos` lists 1-25 registrations of this tenant. `all` means every registration the tenant has **at firing time**. Platform-scope types (§3.13) take `{mode: "platform"}` and are admin-only |
| `cron` | string | caller | Five fields (§2.3), validated, with a per-type minimum interval |
| `timezone` | string | caller | An IANA name (`Europe/London`), default `UTC`. Validated against `zoneinfo` |
| `params` | map | caller | The type's parameters (§3), validated by that type's own Pydantic model. **Extra keys are refused by name**, as `PlanSpec` refuses them |
| `gate` | map | caller, within the type's floor | `{plan, run, merge, approvers, approval_ttl_hours}` (§4.2). It is stored resolved, with defaults filled in, so a later default change does not loosen an existing schedule |
| `budget` | map | caller, within platform caps | `{per_run_usd, per_day_usd, max_concurrent}` (§4.3, SD4) |
| `policy` | map | caller | `{overlap: skip \| queue_one, catch_up: skip \| run_once, jitter: bool, dry_run: bool}`. Defaults come from the type (§2.4-§2.6) |
| `state` | string | swarm-api | `enabled`, `paused`, `auto_paused` or `disabled` (§1.3) |
| `pause` | map or null | swarm-api | `{by, at, reason, code}`. `code` is set for an auto-pause (§4.3) |
| `owner` | string | swarm-api | The member whose identity firings submit as (§2.7). It starts as the creator and changes only by an explicit "take ownership", which is audited |
| `created_by`, `created_at`, `updated_by`, `updated_at` | string, timestamp | swarm-api | Taken from the verified token, never from the body |
| `next_run_at` | timestamp | swarm-api | The next slot in UTC, **after jitter**. It is null while not `enabled`. The tick's query reads it (§2.1) |
| `next_slot` | timestamp | swarm-api | The same slot before jitter. It is the firing's idempotency key (§2.2) |
| `last_firing` | map or null | swarm-api | `{firing_id, slot, outcome, work_ref, ended_at}`, a copy for the list screen |
| `consecutive_failures` | int | swarm-api | Reset by a success, and drives the auto-pause (§4.3) |
| `spend` | map | swarm-api | `{day, reported_usd, unreported_attempts, reserved_usd}` for the current day in `timezone` (§4.3) |
| `revision` | int | swarm-api | Incremented by every edit. An edit carries the revision it read, and a stale one is a 409 `schedule_changed`, the `plan_changed` pattern |

**Limits that bound one tenant (§5.6):** at most 25 schedules per tenant
(`SCHEDULES_PER_TENANT`, an admin may raise it per tenant), and each type has a
minimum interval (§3).

### 1.2 `schedule_firings/{schedule_id}:{slot}`

There is one document per due slot. `{slot}` is the slot's Unix minute,
**before jitter**. Its **creation is the claim**: the tick creates it inside the
transaction that advances `next_run_at` (§2.2), and a create that finds the
document already there is the duplicate that never happens twice.

| field | meaning |
|---|---|
| `schedule_id`, `tenant_id`, `type`, `slot`, `fired_at` | identity and when the tick claimed it |
| `trigger` | `cron`, `catch_up`, `run_now` or `queued`. A `run_now` firing is keyed `run_now:{uuid}` rather than by slot |
| `state` | §1.4 |
| `params_digest` | sha256 of the resolved `{type, scope (resolved repo list), params, gate, budget}` the firing ran with. A run approval approves this digest (§4.5) |
| `work` | `[{kind: issue_run \| task \| workflow \| api_action, id, repo_id}]`, what it created. A sweep creates several |
| `skip` | `{code, detail}` when nothing was created: `OVERLAP`, `BUDGET_EXHAUSTED`, `MISSED_SLOT`, `OWNER_NOT_MEMBER`, `REPOSITORY_NOT_GRANTED`, `WORKSPACE_NOT_READY`, `NO_CLAUDE_ACCOUNT`, `SCHEDULES_DISABLED` or `DRY_RUN` |
| `dry_run` | what a dry run would have created, as data (§2.8) |
| `approval_id` | set while a run approval is pending |
| `outcome` | `succeeded`, `failed`, `cancelled`, `refused`, `skipped`, `expired`, `rejected` or `partial`, derived from its work (§2.9) |
| `cost` | `{reported_usd, unreported_attempts}`, summed over the work's attempts (§4.3) |
| `history` | each move, with time and actor |

Firings are kept for 90 days, then deleted by a Firestore TTL policy on
`expire_at`. The schedule's audit (§4.8) keeps the decisions for longer.

### 1.3 Schedule states

| state | entered when | the tick | the person sees |
|---|---|---|---|
| `enabled` | created (unless created paused), resumed | fires due slots | the next run in words, e.g. "Mon 09:00 London" |
| `paused` | a member or admin paused it | reads nothing | "Paused by {who} {when}: {reason}" and Resume |
| `auto_paused` | a hard stop of §4.3 | reads nothing | the code's copy, e.g. "Paused after 3 failed runs in a row", and Resume, which is audited |
| `disabled` | an admin disabled it (§5.3), or its type was withdrawn from the catalogue | reads nothing | "Disabled by an admin". Only an admin may re-enable it |

Deleting a schedule is a hard delete of the document. It needs a typed
confirmation in the console. Its firings stay until their TTL, and its audit
stays. Work that a schedule already created is **not** cancelled by a pause or
a delete: it is ordinary work, cancelled the ordinary way. The console offers
"Pause and cancel live runs" as a separate, typed action.

### 1.4 Firing states

```
claimed ─► awaiting_approval ─► (approved) ─► created ─► done (outcome)
   │              │                               ▲
   │              ├─► expired / rejected          │
   │              ▼                               │
   ├─► skipped (code)                             │
   ├─► queued (overlap: queue_one) ───────────────┘
   └─► refused (a submission refusal: WORKSPACE_NOT_READY, …)
```

`awaiting_approval` and `queued` are Firestore documents and nothing else. No
task, lease or pending pod exists for them (invariant 1). The work is created
only at `created`.

### 1.5 Frozen contract

**None is needed.** The pieces are:

* `schedules/`, `schedule_firings/`, `approvals/` and `schedule_audit/` are new
  swarm-api collections.
* The link from work to its firing is a new **swarm-api reserved metadata
  key**, `schedule`, added to
  `apps/swarm-api/swarm_api/validation.py::RESERVED_METADATA_KEYS`. A caller
  cannot forge it. It sits on the task, the workflow and the issue run, so
  every surface can say "made by schedule *nightly sweep*".
* `submitted_by` stays the verified owner's email, as the frozen `Task`
  defines it.

  The owner asked for `created_by: schedule:<id>`. It is served by the API
  from `metadata.schedule`, as a derived `created_by` field on reads, and not
  stored in `submitted_by`. Two reasons. `submitted_by` is "verified email from
  the ID token" in `apps/common/swarm_common/models.py`. And repository grants,
  commit identity and membership checks all read it as a person.
* Every type compiles to work the platform already runs:
  - an issue run;
  - a `claude-code` or `indexer` task;
  - a workflow of `claude-code` steps;
  - a swarm-api API action.

  None needs a new `TaskState`, `ParkReason` or profile.

One **optional** request is written in §12. It is a declared input that would
let `observer` and `cost-report` hand an agent a data file rather than putting
the data in the prompt. The design works without it (§3.4).

---

## 2. Execution

### 2.1 One tick, not one GCP job per schedule

`google_cloud_scheduler_job.schedule_tick` runs every minute and calls
`POST /v1/admin/schedules/tick` with **no tenant parameter**, as the existing
`swarm-rollup-sweeper` identity. The route is added to `ROLLUP_SWEEPER_ROUTES`.

Why one tick:

* **A per-schedule GCP job would make every edit an infrastructure change.**
  It would mean Cloud Scheduler admin rights for swarm-api, or a Terraform
  apply per edit. It would raise the question of which identity each job
  carries. And it would put the list of tenants' schedules in GCP, outside the
  tenant check every read makes. With one tick, a schedule is data, and the
  tenant check is the one `issue_runs` already makes.
* **A per-tenant tick (the `issue_run_advance` pattern) misses `u-*`
  tenants,** which the owner wants included. A single query over
  `schedules where state == enabled and next_run_at <= now order by
  next_run_at` covers every tenant, personal ones included, with one composite
  index (§9, S4).
* **The identity is not widened in kind.** The sweeper already calls one route
  per registered tenant. This route reads due schedules across tenants, but it
  **submits nothing as itself**: every firing is submitted as the schedule's
  owner in the schedule's tenant (§2.7). Not reusing the sweeper is option (b)
  of SD10: a dedicated `swarm-schedule-tick` account, which needs a bootstrap
  apply by the owner.

The job's `retry_count` is 0. The next minute's tick is the retry, and every
claim is a transaction, so an overlapping retry repeats reads and never a
firing.

### 2.2 Exactly once per slot

For each due schedule, one Firestore transaction does the following:

1. **Re-read the schedule.** If it is not `enabled`, or `next_run_at` is in the
   future, stop. Another tick won.
2. **Compute the slot.** The slot is `next_slot`, the stored un-jittered time.
   Then compute the newest slot that is `<= now` (§2.4, catch-up).
3. **Create `schedule_firings/{id}:{slot}`.** Firestore's `create` fails if the
   document exists, and the transaction aborts with nothing written. A retried
   tick, a second tick and an overlapping run-now all meet the same document.
4. **Advance** `next_slot` and `next_run_at` to the first slot after `now`, and
   write `last_firing` as claimed.

Creating the work happens **after** the transaction, from the firing document
(§2.7). If that step fails (a 5xx, or a timeout of the route), the firing stays
`claimed`. The next tick finds claimed firings older than 2 minutes and
finishes them. Before creating anything, a finisher looks for work already
carrying `metadata.schedule.firing_id` equal to this firing's id (tasks,
workflows and issue runs, §2.7), and adopts what it finds. So a step that
created the work and then failed to record it cannot create it twice. The
submission paths have no idempotency key of their own today (child tasks have
`child_request_id`; top-level tasks and workflows have none), which is why the
lookup is by the firing's own mark.

### 2.3 Cron expressions, timezone and the clock

* **Five fields:** minute, hour, day of month, month and day of week. Allowed:
  lists, ranges, steps, `JAN`-`DEC` and `MON`-`SUN`, and the aliases
  `@hourly`, `@daily`, `@weekly` and `@monthly`.

  Refused: seconds, `@reboot`, and Quartz's `L`, `W`, `#` and `?`. The parser
  is **swarm-api's own**, about 150 lines over `zoneinfo`, not a dependency.
  The grammar is small. One implementation serves the tick, the validation,
  the console's "next five firings" and the words. A second parser in
  TypeScript would drift from it, so the console asks the API
  (`POST /v1/schedules:preview`, §7.1).
* **Day of month and day of week combine as Vixie cron does:** when both are
  restricted, either one matching fires. The words say "or", so
  `0 9 1 * MON` reads "09:00 on the 1st, or on Mondays".
* **Each type has a minimum interval** (§3). It is validated by computing the
  smallest gap between the next 50 firings, so `*/5 * * * *` is refused for a
  type whose minimum is 15 minutes, whatever the spelling.
* **Daylight saving time.** A local time that does not exist (spring forward)
  fires once, at the first instant after the gap. A local time that happens
  twice (fall back) fires once, at its first occurrence. The slot is the UTC
  instant, so the key cannot collide.
* **The clock.** The tick compares `next_run_at` with swarm-api's clock. That
  is Cloud Run's, synchronised by Google. **A late tick does not move a slot:**
  the slot comes from the stored cron and `next_slot`, and the tick time only
  decides whether a slot is due. A tick a minute late fires the 09:00 slot at
  09:01 with `slot = 09:00`. Lateness is recorded as `fired_at - slot`, and
  the admin view shows its 95th percentile.

### 2.4 Catch-up after an outage

A slot is **missed** when `now - slot` exceeds its grace. The grace is the
smaller of 15 minutes and half the schedule's interval. Missed slots are never
all run.

| policy | after an outage that missed several slots |
|---|---|
| `skip` | Each missed slot gets a `skipped / MISSED_SLOT` firing, so the history shows the gap. Then the schedule waits for its next slot |
| `run_once` | The same records, except that the **newest** missed slot fires once, with `trigger: catch_up` |

The default is per type (SD6): `run_once` for reports (`observer`,
`cost-report`, `release-health`, `docs-drift`) and `repo-index-refresh`, and
`skip` for everything that acts on repositories. Sweeping issues twice after
an outage would be harmless, because the sweep is idempotent per issue. But a
person reading "it ran at 14:07" after a 09:00 schedule should be told why, so
an acting type does not catch up unless asked to.

Writing a skip record per missed slot is bounded. A schedule paused or broken
for a month writes at most 50 records. Beyond that it writes one
`MISSED_SLOT` record whose detail gives the count.

### 2.5 Overlap

A firing is **live** while any of its work is in a non-terminal state.
For an issue run, `PLANNED` counts as live: the plan is waiting for a person,
and a second sweep would plan the same issues again.

| policy | a slot comes due while the previous firing is live |
|---|---|
| `skip` (default) | `skipped / OVERLAP`, naming the live firing |
| `queue_one` | one `queued` firing. It is created as the live one ends, by the tick that sees it end. A second overlap while one is queued is `skipped / OVERLAP`. **Never more than one queued** |

`max_concurrent` (§4.3) is a separate bound. It counts work items, such as the
sweep's live issue runs, not firings.

### 2.6 Jitter

When `policy.jitter` is on (the default), `next_run_at = next_slot +
offset`. The offset is `sha256(schedule_id) mod J` seconds, and `J` is the
smaller of 300 seconds and 10% of the interval.

* It is **deterministic**: a schedule fires at the same offset every time, and
  a person sees it ("09:03, spread to avoid a stampede").
* It **spreads** the 25 schedules a tenant writes as `0 9 * * 1-5` over five
  minutes rather than one tick.
* The firing key stays the un-jittered `slot`, so jitter cannot create or
  merge firings.

### 2.7 What a firing creates, and as whom

The firing creates work through the **same service methods a person's request
uses**:

* `apps/swarm-api/swarm_api/service.py::SubmissionService.submit_tasks`;
* `SubmissionService.submit_workflow`;
* the issue-run creation that `POST /v1/runs` makes.

So every validation, refusal, grant check, signature and admission rule applies
unchanged.

**As whom.** `schedule_owner_auth(ctx, schedule)` is a third copy of the
pattern in `run_owner_auth` and `registration_owner_auth`. The S2 lane folds
the three into one helper in its own new file, and does not change the other
two:

* the principal is the stored `owner`, in the stored `tenant_id`;
* `is_admin`, `member_scope` and `tenant_member` are an ordinary member's empty
  values;
* membership is asked of the directory **at every firing**
  (`Authenticator.is_tenant_member`).

An owner who has left the tenant makes the firing `skipped /
OWNER_NOT_MEMBER`, and the schedule moves to `auto_paused` with that code. Any
current member may "take ownership", which is audited. A directory lookup that
fails leaves the firing `claimed` for the next tick.

**Marked.** The work carries `metadata.schedule = {schedule_id, firing_id,
slot, type}`. An issue run carries the same map on its own document. Reads
derive `created_by: "schedule:<schedule_id>"` from it.

**Admitted like anything else.** A task the firing creates is `QUEUED` or
`READY` and costs nothing until admission takes a lease (invariants 1-3). A
firing never reserves capacity, never waits for it, and does not know whether
the tenant's pool is full. A full pool is the ordinary queue.

**Personal workspaces.** The submission gates of
[workspaces.md §5](workspaces.md#5-the-submission-gate) apply as they do to a
person: `WORKSPACE_NOT_READY` or `NO_CLAUDE_ACCOUNT` makes the firing
`refused` with that code. Three refusals in a row auto-pause the schedule
(§4.3).

### 2.8 Dry run

With `policy.dry_run`, or `POST …:run` with `{"dry_run": true}`, the firing
computes what it would create and stores it in `dry_run`, then ends `skipped /
DRY_RUN`. Nothing is submitted and nothing is written to GitHub.

* For `issue-sweep`, the output is the candidate issues and why each was taken
  or passed over.
* For `pr-shepherd`, it is the label, update-branch and comment actions.
* For the agent types, it is the compiled prompt's size and the resolved
  repository list. **The agent itself never runs dry.** A dry run that spends
  tokens is not dry.

### 2.9 Outcome, and advancing what a firing made

A firing's outcome is **derived** from its work, the way a workflow's state is
derived from its steps:

* all succeeded: `succeeded`;
* any failed: `failed`, or `partial` when the type does several independent
  things (a sweep that started five runs, of which one failed);
* a run that ended `NOT_READY` (§3.1) counts as succeeded, because "this issue
  is not ready" is an answer.

The tick advances live firings: up to one page, oldest first, after it has
fired due slots. For an issue run it calls
`apps/swarm-api/swarm_api/routes/runs.py::advance_run`. **That closes, for
scheduled runs, the gap of §0:** an `auto` issue run in a `u-*` tenant moves
even when nobody reads it, because the schedule tick reaches every tenant.

### 2.10 Tick bounds and fairness

* It reads at most 200 due schedules per tick, ordered by `next_run_at`.
* It fires at most **5 per tenant per tick**, round-robin by tenant. The rest
  stay due for the next minute. So one tenant's 25 schedules due at 09:00
  cannot delay another tenant's one schedule by more than a tick.
* It stops starting new work at 240 seconds, `repoindex.POLL_BUDGET_SECONDS`'s
  value and reason. It reports `{fired, skipped, advanced, truncated}`.
* A schedule whose firing raised an error is counted and the rest of the page
  goes on, as the issue-run tick does.

### 2.11 Kill switches

| switch | who | effect |
|---|---|---|
| Pause a schedule | any member of its tenant, or an admin | no new firings. Live work continues |
| Pause and cancel live runs | any member, typed confirmation | the above, plus an ordinary cancel of the firing's live work |
| Pause all of a tenant's schedules | any member, typed; an admin from §5.3 | every `enabled` schedule of the tenant becomes `paused` with one reason, audited per schedule |
| `SCHEDULES_ENABLED=false` | the owner, through `api_settings` | the tick returns `{disabled: true}` immediately. Each due slot is recorded as `skipped / SCHEDULES_DISABLED` when the switch is turned back on, under the catch-up policy |
| Cloud Scheduler `paused` | Terraform (`var.paused`) | the tick is not called at all. The same catch-up applies on resume |

---

## 3. The job-type catalogue

A caller picks a type **by name**. The catalogue is code
(`swarm_api/scheduletypes.py`), in the shape of the frozen `RUNNER_PROFILES`
but not part of it. Each entry declares:

* name and description;
* executor (`issue_runs`, `task`, `workflow` or `api`);
* the Pydantic parameter model;
* the minimum interval;
* the default and floor gates;
* the default budget and catch-up;
* the risk tier (§4.1) and whether it pushes (§5.4);
* who may create it (`member`, `admin` or `owner`);
* `available` and a `disabled_reason`, as profiles have.

No type accepts an image, a command, a profile, a resource class or a backend
parameter. A type that runs an agent names its profile in code. Invariant 10
holds for schedules exactly as for tasks.

| # | type | executor | risk | pushes | default gate | creates | min interval | phase |
|---|---|---|---|---|---|---|---|---|
| 1 | `issue-sweep` | issue runs | R3 with `merge: auto`, else R2 | yes | plan auto · **approve merge** | members | 15 min | 1 |
| 2 | `issue-plan-only` | issue runs | R1 | no, until approved | **approve plan** | members | 15 min | 1 |
| 3 | `repo-index-refresh` | `indexer` tasks | R0 | no | auto | members | 1 h | 1 |
| 4 | `observer` | one `claude-code` task | R0 | no | auto | members (tenant), owner (platform) | 1 h | 1 |
| 5 | `epic-triage` | `claude-code` task, then API | R1 | no (comments) | **approve plan** (the ticks) | members | 6 h | 2 |
| 6 | `pr-shepherd` | API, then continuations | R1 for API actions, R2 for conflict fixes | yes (fixes) | API auto · **approve run** for fixes | members | 30 min | 2 |
| 7 | `ci-flake-hunter` | API, then `claude-code` tasks | R1 | no (issues) | auto | members | 6 h | 2 |
| 8 | `dependency-cve-refresh` | workflow | R2 | yes | **approve merge** | members | 24 h | 2 |
| 9 | `release-health` | API | R1 | no (an issue) | auto | members; owner for the platform's own repository | 15 min | 2 |
| 10 | `docs-drift` | `claude-code` task | R1 report, R2 with `fix: true` | with `fix: true` | auto report · **approve merge** | members | 24 h | 2 |
| 11 | `cost-report` | API | R0 | no | auto | members (tenant), admin (platform) | 24 h | 2 |
| 12 | `custom-prompt` | workflow from a saved spec | R3 | yes | **approve run + approve merge**; never auto | members, with a one-time admin approval of the spec | 6 h | 3 |

**Platform-wide (admin) types** run with `scope: {mode: "platform"}`: the
platform variants of `observer` and `cost-report`, and `release-health` on the
platform's own repository. They are created by an admin (§3.13). **Owner-only**
are the `observer` platform variant, which reads every tenant's aggregates, and
any schedule whose resolved gate allows an **unattended merge** (`merge:
auto`) in the platform's own repository. Both put the platform's own code or
every tenant's data within one approval's reach.

### 3.1 `issue-sweep`

* **Does.** It reads the open issues of each repository in scope, with the
  tenant's forge token (`forge.read_open_work`).
  - It drops issues that match `labels_exclude`, issues with a live issue run,
    and issues answered `NOT_READY` within `cooldown_hours` whose body and
    labels have not changed since.
  - It creates an issue run for each of up to `max_new_per_firing` remaining
    issues, oldest first, subject to `max_live_runs`.
  - The planner decides readiness. Lane SWEEP's `NOT_READY` verdict ends the
    run with no workflow and a comment that says what is missing.
  - The **territory guard** holds an approved plan whose `files` overlap a live
    run's plan until that run ends. It is lane SWEEP's guard, kept as written.
    [lane-queue.md §4](lane-queue.md) designs the general lock this guard
    would later become.
* **Parameters.**

  | parameter | default | range |
  |---|---|---|
  | `labels_include` | `[]` (any) | — |
  | `labels_exclude` | `["security", "needs-owner", "wontfix", "question"]` | — |
  | `max_live_runs` | 8 | 1-8 |
  | `max_new_per_firing` | 3 | 1-8 |
  | `cooldown_hours` | 72 | 1-720 |
  | `plan_approval` | `auto` | `auto`, `required` |
  | `merge` | `approve` | `off`, `approve`, `auto` |
  | `fix_rounds` | 3 | 1-5 |
  | `territory_guard` | true | **not settable to false** by a member |

  The cap of 8 is lane SWEEP's, and it is the upper bound here: one firing can
  hold at most 8 tenant slots of work at once.
* **Produces.** Issue runs, each linked from the firing. Pull requests, opened
  by those runs. Merges, when the merge gate allows them.
* **Blast radius.** Code changes on branches. Merges into the default branch
  only through the run's existing merge-only continuation, at the reviewed and
  green sha, after the merge gate. A security-class issue is never swept
  (§4.4). Bounded by `max_live_runs`, the tenant's ceilings and the budget.

### 3.2 `issue-plan-only`

* **Does.** The same selection as `issue-sweep`, with `plan_approval:
  required` forced. Plans arrive in the inbox, and approving one is the
  issue run's existing approve. Nothing executes until a person approves.
* **Parameters.** The selection parameters of §3.1, `max_pending_plans`
  (default 5, range 1-20: a new plan is not made while that many are waiting),
  and `fix_rounds`.
* **Default gate.** Approve plan. It is the floor, so it cannot be lowered.
* **Produces.** `PLANNED` issue runs and plan comments on the issues.
* **Blast radius.** One planner task per issue, and plan comments on GitHub.
  No code changes without a person.

### 3.3 `repo-index-refresh`

* **Does.** For each repository in scope, it queues an index run through
  `RepoIndex`'s existing queueing, with that path's in-flight rule: a run in
  flight is not duplicated.
* **Parameters.** `kind` (`incremental` or `full`, default `full`) and
  `only_if_behind` (default false).
* **Default gate.** Auto. Floor: auto.
* **Produces.** `indexer` tasks. Their output is the index under the tenant's
  prefix, `repos/<repo_id>/`.
* **Blast radius.** It reads repositories and writes only the tenant's own
  index objects. §8.3 decides how it relates to the existing `index.*`
  settings.

### 3.4 `observer`

* **Does.** It is a read-only self-improvement report over the last
  `window_hours` of the tenant's work. swarm-api computes a **digest** from
  data it already holds. A `claude-code` task with **no repository** gets the
  digest as data, between delimiter lines carrying the firing id: the issue
  run's open-work pattern, bounded to 48 KiB (`MAX_PLANNER_PROMPT_BYTES` is 64
  KiB). The task writes `report.md` and `proposals.json`. The digest holds:
  - cost per run and per profile, with spend coverage;
  - the median and 95th-percentile start latency (`LEASED` to `RUNNING`);
  - the share of pull requests red on their first CI run;
  - fix steps that ran and changed nothing;
  - pull-request titles that state no fact, or carry a closing keyword the
    review did not confirm;
  - parks by reason;
  - refusals by code.
* **Parameters.**

  | parameter | default | range |
  |---|---|---|
  | `window_hours` | 24 | 1-168 |
  | `focus` | all | a subset of `cost`, `latency`, `ci`, `idle_fixes`, `titles`, `parks`, `refusals` |
  | `file_issues` | `false` | — |

* **Default gate.** Auto. Each proposal lands in the inbox as a **proposal**
  (§4.5). Turning one into a GitHub issue is a click by a person, unless
  `file_issues` is true, in which case they are filed as one epic per the
  CLAUDE.md "Issues" rules: one comment per finding.
* **Produces.** `report.md` (an artifact, shown on the firing), proposals, and
  optionally an epic.
* **Blast radius.** None on code. It reads the tenant's own records.
* **Platform variant (owner-only).** It reads aggregates across tenants: counts,
  durations, costs and codes, **never a prompt, an output or a title from
  another tenant**. It runs in the tenant the owner names when creating it
  (§5.3).

### 3.5 `epic-triage`

* **Does.** For each open issue labelled `epic` in scope, a `claude-code` task
  reads the epic's comments and the repository's default branch. It writes
  `ticks.json`: for each unticked box, the evidence (a merged PR or a commit,
  with the file and the call site) or "still open". swarm-api validates each
  proposed tick. **One without a PR or commit reference is refused**, because
  CLAUDE.md says to tick only with evidence. The rest become one **plan
  approval**: the list of ticks with their evidence. When approved, swarm-api,
  not the agent, edits the comments with the tenant's token.
* **Parameters.** `epic_label` (`epic`) and `max_epics` (5, range 1-20).
* **Default gate.** Approve plan. Floor: approve plan.
* **Produces.** Tick proposals, and after approval, edited comments with the
  evidence appended.
* **Blast radius.** Checkbox edits in the epics' comments. Reversible. It never
  closes an epic: an epic closes on its comments, by a person.

### 3.6 `pr-shepherd`

* **Does.** For each open, non-draft pull request in scope with no activity for
  `stale_hours`:
  - It applies the `labels` policy, e.g. adds `stale` or removes `ready` when
    CI is red at the head.
  - It calls GitHub's update-branch when the pull request is behind and
    mergeable.
  - For a **conflict**: if the pull request was opened by a SwarmCloud issue
    run, it submits a conflict-fix continuation of the run's integrator. That
    is the CI fix round's mechanism, the only way the worker can push to an
    existing pull request's branch today. Otherwise it posts one comment
    naming the conflicting files.
* **Parameters.** `stale_hours` (48), `labels` (a small map of conditions to
  add/remove), `update_branch` (true), `fix_conflicts` (false), and
  `max_prs_per_firing` (10).
* **Default gate.** Auto for the API actions. **Approve run** for conflict
  fixes, which push code.
* **Produces.** Labels, merged-base updates, comments and continuation tasks.
* **Blast radius.** A pull request's labels and branch. Update-branch makes a
  merge commit on the PR's branch, and GitHub may refuse it when the merge
  brings in `.github/workflows/` changes the credential cannot push (S0
  verifies this). A refusal is recorded and never retried in the same firing.

### 3.7 `ci-flake-hunter`

* **Does.** It reads the default branch's check runs over `window_days`. A
  **flake** is a check that failed and then passed at the **same sha**. For the
  top `max_flakes` by count, it files or updates one issue per flaky test
  (title as a fact, label `bug`), optionally with a `claude-code` diagnosis
  task whose `diagnosis.md` is attached.
* **Parameters.** `window_days` (7), `min_occurrences` (2), `max_flakes` (3)
  and `diagnose` (false).
* **Default gate.** Auto. Issues are reversible and say who filed them.
* **Produces.** Issues, and diagnosis artifacts.
* **Blast radius.** Issues only. Reading check runs needs `checks: read`, and
  reading workflow runs needs `actions: read`. S0 verifies which the App
  holds; if neither, the type stays `available: false`.

### 3.8 `dependency-cve-refresh`

* **Does.** It reads the latest vulnerability scan for the repository: the
  image scan of `security.yml`, read as a check-run summary. If fixable
  packages are listed, it submits a workflow whose one step bumps the base
  images' package pins (the libtiff fix of 2026-10-08 is the model) and opens
  a pull request through the ordinary review and integrate shape.
* **Parameters.** `paths` (default `["images/"]`), `severity_at_least`
  (`high`) and `max_packages` (10).
* **Default gate.** Approve merge. Floor: approve merge.
* **Produces.** One pull request per firing at most.
* **Blast radius.** Image definitions. The release builds and deploys them, so
  a bad bump reaches the platform at the next release, which is why the merge
  gate cannot be lowered.

### 3.9 `release-health`

* **Does.** It reads the latest runs of a named workflow (default
  `release.yml`) on the default branch. On a **failure** it opens one issue or
  updates the open one: title as a fact, the failing job and step, a log
  excerpt passed through `redact`, label `bug`. On the first success after a
  failure it comments "green again at {sha}". **It never closes the issue**;
  a person does.
* **Parameters.** `workflow` (`release.yml`), `branch` (the default branch) and
  `issue_label` (`bug`).
* **Default gate.** Auto.
* **Produces.** One issue per failure streak.
* **Blast radius.** One issue. Needs `actions: read` (S0).

### 3.10 `docs-drift`

* **Does.** A `claude-code` task checks, in the repository's `docs/`:
  - line citations against the files they name;
  - `path::symbol` citations against the code;
  - stated defaults against the values in code.

  It writes `drift.md`. With `fix: true` it also edits the docs (only paths
  under `paths`) and opens a pull request.
* **Parameters.** `paths` (`["docs/"]`) and `fix` (false).
* **Default gate.** Auto for the report. Approve merge with `fix`.
* **Produces.** A report, and optionally a docs-only pull request.
* **Blast radius.** Documentation. A diff outside `paths` is a hard stop at the
  merge gate (§4.4).

### 3.11 `cost-report`

* **Does.** It sums `cost_usd` over the window's attempts by schedule,
  repository, profile and person, **with coverage**: rows without a reported
  cost are counted and the total is marked a lower bound, the attempts list's
  rule. It stores the report on the firing and, optionally, posts it as a
  comment on a named issue.
* **Parameters.** `window` (`day` or `week`) and `post_to_issue` (optional
  `owner/repo#N`).
* **Default gate.** Auto.
* **Produces.** A stored report.
* **Blast radius.** None, or one comment.
* **Platform variant (admin).** It covers all tenants, and is visible to
  admins only.

### 3.12 `custom-prompt`

* **Does.** It runs a saved workflow spec, signed as every workflow spec is.
  The spec is stored on the schedule **with its digest**.
* **Parameters.** `spec` (a workflow spec, validated by the same
  `WorkflowCreate` model as `POST /v1/workflows`, so any profile it names is
  named, never an image) and `max_steps` (5).
* **Gate.**
  - **Approve run** and **approve merge** are the floor, and members cannot
    lower them.
  - **Activation:** the first time a spec digest is enabled, a platform admin
    approves the digest in the inbox, and editing the spec needs that approval
    again. A spec is a standing instruction to an agent with push rights, which
    the catalogue's other types are not.
  - `merge: auto` is never allowed.
* **Produces.** Whatever the spec's workflow produces.
* **Blast radius.** Bounded by the spec's repositories, the gate and the
  budget.

### 3.13 Platform-scope types

A platform-scope schedule is created by an admin and **owned by a tenant the
admin names**. That tenant's identity, ceilings and spend are the ones used.
The platform does not run work outside a tenant. The owner names a tenant for
platform automation when the first one is created (SD7).

What such a schedule reads across tenants is **aggregates built by swarm-api**
(counts, durations, costs, codes), never a tenant's prompts, outputs, titles or
repository contents. So no tenant's data enters another tenant's worker
(invariant 9).

---

## 4. Gates and approvals

### 4.1 Risk tiers

| tier | what the work may change | examples |
|---|---|---|
| R0 | SwarmCloud's own records and the tenant's own objects | `repo-index-refresh`, `observer`, `cost-report` |
| R1 | GitHub issues, comments and labels. No code | `issue-plan-only`, `epic-triage`, `release-health`, `ci-flake-hunter` |
| R2 | Code on branches, through pull requests. No merge | `pr-shepherd` fixes, `dependency-cve-refresh`, `docs-drift` with `fix` |
| R3 | A merge into the default branch, or a standing free-form instruction | `issue-sweep` with `merge: auto`, `custom-prompt` |

A schedule's tier is computed from its type and its resolved gate. The console
shows it as a chip, and the admin view sorts by it.

### 4.2 Approval modes

A gate has three independent points, plus who may approve:

| point | values | meaning |
|---|---|---|
| `run` | `auto`, `approve` | `approve`: each firing waits in the inbox, showing its resolved parameters and repositories, before it creates anything |
| `plan` | `auto`, `approve` | for types that plan (issue runs, epic ticks): each plan waits for approval of its digest. This is the issue run's `plan_approval: required` |
| `merge` | `off`, `approve`, `auto` | `approve`: a green, reviewed pull request waits in the inbox before the merge-only continuation is submitted. `auto` is the issue run's `auto_merge` |
| `approvers` | `members`, `owner_only`, or a list of members | §4.6 |

The owner's five modes map onto it like this:

| mode | `run` | `plan` | `merge` |
|---|---|---|---|
| auto | `auto` | `auto` | `auto` or `off` |
| approve the plan | `auto` | `approve` | any |
| approve each run | `approve` | any | any |
| approve before merge | any | any | `approve` |
| owner-only | any | any | any, with `approvers: owner_only` |

**Floors.**
- Each type declares a floor (§3). A member may set any gate **at or above**
  the floor.
- Lowering a gate below the type's **default** needs a platform admin, and is
  audited.
- Below the floor is impossible.
- Raising a gate is always allowed and takes effect at the next firing. A
  firing already awaiting approval keeps the gate it was claimed with.

### 4.3 Budgets, and what they can and cannot promise

`budget = {per_run_usd, per_day_usd, max_concurrent}`.

* **`max_concurrent` is enforced before work exists.** It is the number of
  live work items the schedule may have: a sweep's issue runs, a shepherd's
  continuations. A firing creates no more than the remainder.
* **`per_day_usd` is a firing gate, not an admission control.** A firing is
  skipped with `BUDGET_EXHAUSTED` when `reported_today + reserved >
  per_day_usd`.
  - `reserved` is `per_run_usd` for each live work item, and for each attempt
    whose cost was not reported. **An unknown cost counts as the cap, not as
    zero.**
  - The day is the schedule's timezone's.
  - Nothing parks: `PARKED(BUDGET_EXHAUSTED)` stays unwritten, as
    cost-control.md requires.
* **`per_run_usd` is a tripwire, not a ceiling.** `cost_usd` is written when
  an attempt ends, so nothing can stop a run at its cap mid-flight.
  - The real bound on one run is its time: the profile's `timeout_seconds` and
    the issue run's `fix_rounds`.
  - A run whose recorded cost exceeds `per_run_usd` ends its firing `failed`
    with `RUN_OVER_BUDGET`, and **pauses the schedule at once**.

  The worst case for a day is therefore `per_day_usd + max_concurrent ×
  (actual run cost − per_run_usd)`, and the console's budget card says so in
  words.

How this sits with the owner's decision of 2026-10-01 ("no per-tenant dollar
budgets"): nothing in admission reads these figures, no tenant-wide budget is
created, and the scheduler is unchanged. They bound what a **schedule** may
start. The owner confirms or overrides this in SD4.

### 4.4 Hard stops, whatever the mode

| stop | detected | what happens |
|---|---|---|
| **IAM, Terraform bootstrap, `terraform/**/iam*.tf`, any `google_*_iam_*` change** | at plan (the plan's `files`, and a scan of step prompts for the paths) **and** at merge (the pull request's changed files, read through the forge API before the merge continuation is submitted, `issueci._merge`'s point) | Held with `NEEDS_OWNER`. An approval inbox item with `approvers: owner_only`, whatever the schedule's approvers. The plan's files are advisory, which is why the merge check exists |
| **The frozen contract**, `apps/common/swarm_common/**`, in a repository whose registration marks it `platform: true` | the same two points | `NEEDS_OWNER`, as above, and the inbox item links [contract-change-requests.md](contract-change-requests.md) |
| **`.github/workflows/**`** | at plan | **Refused, not held.** The credential cannot push those files (§0), so the step would fail after spending. The plan is returned to the planner once with that constraint. A second plan touching them ends the run `NEEDS_HUMAN`, and its issue gets a comment |
| **A security-class issue**: label `security`, or the form's severity S0 | at selection and at plan | Excluded from `issue-sweep` by default. If a member removes `security` from `labels_exclude`, or a run is created on such an issue, its plan is **always** `approve` with `approvers: owner_only`, and its merge is never `auto` (owner decision 2026-10-08) |
| **Protected paths** a tenant declares on a registration (`hard_stop_paths`, default `[".github/workflows/**", "terraform/bootstrap/**", "**/iam*.tf", "CODEOWNERS"]`) | both points | `NEEDS_OWNER` for the platform's own repository. For another repository, a held approval for the tenant's members with the matched path named |
| **Budget exhausted** | at firing | `skipped / BUDGET_EXHAUSTED`. The schedule stays `enabled` and fires again the next day |
| **Run over budget** | at the work's end | the firing is `failed / RUN_OVER_BUDGET`. The schedule becomes `auto_paused` |
| **N consecutive failures** (`failed` or `refused`; default N = 3, range 1-10) | at outcome | `auto_paused / CONSECUTIVE_FAILURES`, naming the last N firings |
| **Owner left the tenant** | at firing | `auto_paused / OWNER_NOT_MEMBER` |
| **Repository no longer registered or granted** | at firing | that repository is skipped, `REPOSITORY_NOT_GRANTED`. If the scope resolves to none, the firing is `refused` and counts as a failure |

Every hard stop is **its own code**. Each new refusal among them ships
report-only behind a `refusals.SWITCHES` entry (PR 873), except the
`.github/workflows/` refusal and the security-class owner gate. Those two
record what the platform already cannot do and what the owner already decided,
so a report-only phase would only let through work that cannot succeed. The
owner confirms the exception in SD3.

### 4.5 Approvals: one record, one inbox

`approvals/{approval_id}` is a new collection, kept by `swarm_api/approvals.py`.

| field | meaning |
|---|---|
| `tenant_id` | as everywhere: a mismatch is a 404 |
| `kind` | `run` (a firing waiting to create work), `plan` (an issue-run plan or an epic-tick list), `merge` (a green PR waiting), `proposal` (an observer or flake proposal to file as an issue), `spec` (a `custom-prompt` digest, admins only), `owner` (any `NEEDS_OWNER` hold) |
| `subject` | `{schedule_id?, firing_id?, run_id?, pr?}` |
| `digest` | what is approved. For `run`, the firing's `params_digest`. For `plan`, the plan digest (D3). For `merge`, `{head_sha, verdict}`, so a push after the request makes the approval stale (409 `merge_changed`). For `spec`, the spec digest |
| `summary` | what the inbox shows, built by swarm-api from the subject, masked |
| `approvers` | resolved at creation (§4.6) |
| `state` | `pending`, `approved`, `rejected`, `expired` or `superseded` |
| `requested_at`, `expires_at`, `decided_by`, `decided_at`, `reason` | `reason` is required for a rejection |

**Issue runs join the inbox without moving.** A `PLANNED` issue run's existing
approve, edit and reject stay where they are, in `issueruns.py` and
`routes/runs.py`. The inbox **projects** them: the list route reads `PLANNED`
runs (the existing index `issue_runs where tenant_id == T and state ==
PLANNED`) beside `approvals/`, and an approve from the inbox calls the run's
existing approve with the digest shown. So a plan approved in Work › Runs, from
`sc plan approve` or from the inbox is one approval, and there is one source of
truth for it.

Approving is one transaction:

1. Check the state and the digest.
2. Write the decision and a `schedule_audit` entry.
3. Move the subject: create the firing's work, submit the merge continuation,
   or apply the epic ticks.

Two approvers racing produce one transition, and the loser gets 409
`already_decided`.

### 4.6 Who may approve

* **Default: any member of the schedule's tenant.** This is the issue runs'
  rule, and the only role a tenant has (§0: there is no tenant-admin role).
* **`approvers: owner_only`**: `PLATFORM_OWNER` only. It is forced for the
  `NEEDS_OWNER` holds of §4.4 and for security-class plans.
* **A named list.** Each name must be a member at creation **and** at approval,
  because membership is asked of the directory.
* **R3 needs a second person.** The member who last changed the schedule's
  gate, spec or scope may not approve its R3 runs or merges when the tenant
  has more than one member. In a personal `u-*` tenant there is one person, so
  the rule cannot apply, and the console says so on the gate card. (SD5.)
* **Platform admins cannot approve another tenant's work** by being admins
  (§5.3). An admin who is also a member approves as a member.

### 4.7 Expiry

`approval_ttl_hours` defaults to 72 (range 1-336). An expired approval:

* For `run`, the firing ends `expired`, and the next slot fires normally.
* For `plan`, the issue run is **rejected with reason "expired"**. It moves to
  the existing `REJECTED` state rather than gaining a state, and the issue's
  cooldown starts.
* For `merge`, the pull request stays open, green and unmerged, and the run
  stays `DONE`. Nothing merges after expiry without a new approval, which the
  console's "Request merge again" creates.

**A pending approval holds nothing**, before and after expiry. No task, lease
or slot exists for it, and a plan's planner task has already ended (invariant
1). Expiry is a tidy-up of the inbox, not a capacity release. Expired items
leave the inbox and remain in the schedule's history.

### 4.8 The audit

`schedule_audit/{id}` is **append-only**, like `admin_audit`. The module that
writes it has no update or delete path. An entry is written in the same
transaction as the change it records:

* create, edit (with the before and after of `gate`, `budget`, `scope`,
  `cron`, `params` and `state`), pause, resume, auto-pause, disable, delete,
  take-ownership and run-now;
* every approval decision;
* every hard stop.

Fields: `{schedule_id, tenant_id, action, by, at, detail}`. `by` is an email,
`schedule-tick` or `admin:<email>`. It is private (Firestore): the console
shows it to the tenant's members, and the admin view to admins.

### 4.9 Notifications

| event | console | plugin | GitHub | admins |
|---|---|---|---|---|
| an approval is waiting | the Schedules count badge, Overview's "Waiting on you" card | `sc` prints "2 approvals waiting" on its next command; `swarm_approvals` | the issue's existing plan comment, for plans | — |
| auto-pause | a banner on the schedule and the list | the same line | — | a log-based metric, `schedule_auto_paused`, on the existing alert channel (`alert_emails`), counted per tenant |
| a hard stop needing the owner | an inbox item marked owner | the same | — | the same alert, `schedule_needs_owner` |

The plugin's approval prompt is AskUserQuestion-shaped. When `swarm_approvals`
returns pending items, the operator's Claude session asks the person with the
item's summary and the choices Approve, Reject or Later. Approve calls
`swarm_schedule_approve` with the digest shown. **A GitHub comment
(`/swarmcloud approve <digest>`) is phase 2** (SD9). It needs the poll to read
comments, and a mapping from a GitHub login to a member, which onboarding's
connection holds but nothing checks yet. Until then a comment approves
nothing, and the plan comment says where to approve.

---

## 5. Multi-tenancy and isolation

### 5.1 A tenant sees only its own

Every route resolves the caller's tenant, honouring `X-Swarm-Tenant` as a
selector that never grants
([multi-tenancy.md](multi-tenancy.md#the-tenant-switcher-x-swarm-tenant-selects-it-never-grants)).
It compares the stored `tenant_id` and answers a mismatch with the 404 a
missing id gets. That covers `schedules/`, `schedule_firings/`, `approvals/`
and `schedule_audit/`. A schedule id, a firing id and an approval id are
therefore never an oracle for another tenant.

### 5.2 A firing runs as its tenant

The work a firing creates is ordinary work in `schedule.tenant_id`, submitted
as the stored owner (§2.7). So it runs with that tenant's worker identity,
secrets, GCS prefix `tenants/<id>/`, namespace and `tenant:<id>` pools, with no
schedule-specific path (invariant 9). The forge token is the one the tenant's
work already resolves (git-tokens.md's order). A type that calls GitHub from
swarm-api uses the same `SecretManagerForgeTokens` read as the issue preview,
for that tenant's secret only.

### 5.3 The admin view

`GET /v1/admin/schedules` (`is_admin`) lists every tenant's schedules. Each row
shows:

* the tenant, type, tier, cron in words, next run and last outcome;
* spend today with its coverage;
* pending approvals (a count only);
* state.

Admins may **pause**, **disable** or **re-enable** any schedule, and pause all
of a tenant's, each audited as `admin:<email>`. They may **not** edit another
tenant's parameters, gate, scope or spec, and may **not** approve its
approvals. Those are the tenant's decisions, and an admin's console session is
not that tenant's identity. They also create platform-scope schedules (§3.13).

### 5.4 Repository scope

* A schedule's `repo_ids` must each be a registration **of the schedule's
  tenant** (`repositories/{repo_id}.tenant_id`). It is checked at create, at
  edit and at every firing, and `all` resolves at firing time.
* For a type that **pushes** (§3, "pushes"), the owner's grant on each
  repository must be `write` (`swarm_api/access.py`, `MODES`) when
  `REPOSITORY_GRANTS_ENFORCED` is on. That is the same check a submission
  makes, made early so the form can say so. A grant revoked later skips that
  repository at firing (`REPOSITORY_NOT_GRANTED`).
* A repository registered by two tenants is two registrations. Each tenant's
  schedules see only their own, as the index does (repo-index.md §2.4).

### 5.5 Personal workspaces

A `u-*` tenant is a tenant: it gets its own schedules, scoped to its own
registrations, firing as its one member.

* It is in the tick because the tick queries across tenants (§2.1).
* The issue runs its schedules create are advanced by the schedule tick
  (§2.9), so an `auto` sweep in `u-alice` moves while Alice is away. Her
  unscheduled runs still advance only on read: that gap is the per-tenant
  tick's, and is not widened or closed here.
* Before the workspace is ready, firings are `refused / WORKSPACE_NOT_READY`.
  Creating a schedule is allowed, so a person can set one up during
  onboarding, but it is created **paused**, with the reason shown.

### 5.6 What a runaway schedule can and cannot do to another tenant

| it CAN | it CANNOT |
|---|---|
| Fill its own tenant's `tenant:<id>` pool, up to `max_active` and `capacity_units`. The tenant's other work then queues behind it | Exceed its tenant's ceilings. Admission checks every pool in one transaction (invariants 2-3) |
| Take `global` and `resource:<class>` slots up to its tenant ceiling, so other tenants' work queues longer when the platform is full. **This is the existing ceiling arithmetic**: the sum of tenant ceilings above `global` is the oversubscription an admin chose | Hold capacity while waiting. Approvals, queued firings and `PLANNED` runs are documents (invariant 1) |
| Use a shared pool Claude account's concurrency where its tenant borrows one. Per-key `provider:<p>:tenant:<id>` AIMD keeps its 429s to its own target | Exhaust another tenant's provider key: keys are per tenant (invariant 9) |
| Spend its own forge token's rate limit | Delay another tenant's firings by more than one tick: at most 5 firings per tenant per tick, round-robin (§2.10) |
| Fire as often as its type's minimum interval allows, across at most 25 schedules | Run without bound. `max_concurrent`, `per_day_usd`, the 8-run cap of the sweep and the consecutive-failure pause bound it, and an admin can pause it (§5.3) |

What an admin uses when one tenant's schedules crowd the platform is what they
use today: the tenant's ceiling (`PUT /admin/tenants/<t>/limits`), plus pause
in §5.3.

---

## 6. The console

### 6.1 Placement (SD1)

**Recommended: Work › Schedules, a tab directly after Runs** (mock-ups §2,
variant P1).

* Schedules create runs, and the person who asks "what ran overnight" is in
  Work.
* "Below runs in the main menu" is what the owner described.
* The approvals inbox is **the second pane of the Schedules screen**, at
  `/schedules/approvals`, with a count badge on the tab. Its items cover issue
  runs as well as schedules. It is also on Overview, as a "Waiting on you"
  card that appears only when something is waiting.

The alternatives, drawn:

* **P2, inside Runs**, as a segmented control: "Issue runs | Schedules |
  Approvals". It adds no tab, but Runs today means issue runs, and a schedule
  is not a run.
* **P3, a new spine section, Automate**, with Schedules and Approvals. That
  gives two tabs a spine slot. navigation.html kept the spine to four sections
  on purpose, and repositories.html rejected a section for one page for the
  same reason.

The admin cross-tenant view is **Admin › Schedules** (`admin: true`), beside
Tenants.

Adding a tab touches `SECTIONS`. So the build lane also updates the issue
forms' "Where" list, which `tests/unit/scripts/test_issue_forms.py` holds to
`SECTIONS`, and the nav tests (`nav.links.test.tsx`,
`test_nav_headings_agree.py`).

### 6.2 Screens

Each screen is drawn in 2-3 variants in the mock-ups, with a recommendation.

| screen | variants | recommended | why |
|---|---|---|---|
| Schedules list | A table · B cards grouped by repository · C split list and detail | **A** | It holds the eight columns the owner listed without wrapping at 1280px, and drops to two lines a row on a phone. B shows repository grouping, which the "Repository" filter on A also gives |
| Schedule detail | A tabs (History, Budget, Gate, Settings, Audit) · B one page, timeline first | **A** | Run history is the first tab and the reason to open the page. Audit and Settings are long and rarely read |
| Create and edit | A one page from a type template · B a four-step wizard · C a drawer over the list | **A** | Every field has a default from the type, so most schedules are a type, a repository and a time. One page with the cron editor and the next five firings beside it shows the whole decision at once |
| Approvals inbox | A one list, each item expanding in place · B split list and detail | **B** on desktop, A on a phone | A plan or a merge needs its diff or plan read before approval, which a split view gives room for |
| Admin cross-tenant | A one table with a tenant column · B per-tenant groups with rollups | **A** | Sorting by tier, spend or failures across tenants is the admin's question |

The docs/web-ui rules apply:

* theme tokens and the sky-blue brand (palette A);
* the 12px floor, no all-caps;
* phone widths: each screen has a 390px frame;
* **an unknown is a dash with its reason, never 0.** "Spend today" is
  `$3.10 · 2 attempts unreported` with a partial mark (`Mark kind="partial"`),
  not `$3.10`. A schedule that has never fired shows the `Absent` empty state,
  not "0 runs";
* no invented data: every figure on a frame is labelled example data and
  mapped to its field (mock-ups §10).

### 6.3 Cron in words, and the next five

The form offers presets (every hour, weekdays at 09:00, nightly, weekly on
Monday) and a raw field. Below the field it shows the expression in words and
the **next five firings in the schedule's timezone and in the viewer's**. All
of that comes from `POST /v1/schedules:preview {cron, timezone, type}`, which
answers `{words, next: [...], min_gap_minutes, refusal?}` from the tick's own
parser (§2.3), so the form and the tick cannot disagree.

---

## 7. The plugin and the API

### 7.1 Routes

All of these are tenant-scoped as in §5.1. The new refusals ship report-only
(PR 873) except as §4.4 says.

| route | does |
|---|---|
| `GET /v1/schedule-types` | the catalogue: name, description, parameter schema, default and floor gate, minimum interval, availability. Each tenant sees the types it may create |
| `GET /v1/schedules` | the tenant's schedules with `last_firing`, `next_run_at`, words, spend today with coverage, and pending approval count |
| `POST /v1/schedules` | create: `{name, type, scope, cron, timezone, params?, gate?, budget?, policy?, state?}`. Takes an optional `client_request_id`; a repeat within 24 h returns the schedule the first one created |
| `GET /v1/schedules/{id}` | one schedule, with its last 50 firings |
| `PATCH /v1/schedules/{id}` | edit, carrying `revision`. A gate below the default needs `is_admin` |
| `DELETE /v1/schedules/{id}` | delete (the console's typed confirmation is client-side; the route takes `confirm: <name>`) |
| `POST /v1/schedules/{id}:pause` · `:resume` · `:run` | `:run` takes `{dry_run?}` and makes a `run_now` firing, under the gate's `run` point |
| `POST /v1/schedules/{id}:take-ownership` | the caller becomes the owner. Audited |
| `GET /v1/schedules/{id}/firings` · `/{firing_id}` | the history. A firing shows its work links, cost and dry-run output |
| `GET /v1/schedules/{id}/audit` | the audit, newest first |
| `POST /v1/schedules:preview` | §6.3 |
| `GET /v1/approvals` | the inbox: `approvals/` plus projected `PLANNED` runs, filterable by kind |
| `POST /v1/approvals/{id}:approve` · `:reject` | `{digest}` / `{reason}`. A projected run's id is `run:<run_id>` and calls its existing approve |
| `POST /v1/admin/schedules/tick` | the tick (§2). Admits only the rollup sweeper |
| `GET /v1/admin/schedules` | §5.3 |
| `POST /v1/admin/schedules/{id}:pause` · `:disable` · `:enable` | §5.3 |

### 7.2 MCP tools

| tool | wraps |
|---|---|
| `swarm_schedule_types` | `GET /v1/schedule-types` |
| `swarm_schedules` | the list, or one schedule with `id` |
| `swarm_schedule_create` | create. The tool's description lists the available types by name and says that no image or command is accepted |
| `swarm_schedule_update` | `PATCH`, with revision |
| `swarm_schedule_pause` · `swarm_schedule_resume` | the two verbs |
| `swarm_schedule_run_now` | `:run`, with `dry_run` |
| `swarm_approvals` | the inbox |
| `swarm_schedule_approve` · `swarm_schedule_reject` | approve with the digest the tool returned, or reject with a reason. For a projected issue-run plan this is the same call as `swarm_plan_approve` |

### 7.3 `sc schedules`

```
sc schedules                         list, like the console's table
sc schedules show <name|id>          detail and the last 10 firings
sc schedules new <type> --repo owner/repo --cron "0 9 * * 1-5" [--tz Europe/London] [--param k=v ...] [--dry-run]
sc schedules pause|resume <name|id> [--reason ...]
sc schedules run <name|id> [--dry-run]
sc schedules preview "<cron>" [--tz ...]
sc approvals                         the inbox; then approve|reject <id>
```

`plugin/commands/sc.md` documents them. **The operator's session cron becomes
unnecessary:** the session no longer runs a loop. It reads `sc approvals` when
the person asks, or when `sc` prints the waiting count.

---

## 8. Migration

### 8.1 Lane SWEEP's `issue_sweep` becomes the `issue-sweep` type

SWEEP ships first, as briefed:

* a Cloud Scheduler job, `issue_sweep`, **off by default**;
* the sweep logic in a swarm-api module;
* the `NOT_READY` verdict, the cap of 8 and the territory guard.

The migration keeps SWEEP's logic and moves its trigger and configuration:

1. **S6 makes SWEEP's module the type's executor.** Its constants (cap, labels,
   guard) become the parameters of §3.1, with SWEEP's values as the defaults.
   Its readiness verdict and territory guard stay in its module, unchanged in
   behaviour. Its tests keep passing, which is the lane's acceptance.
2. **S9 creates one `issue-sweep` schedule** per tenant that had SWEEP enabled,
   with SWEEP's cadence and scope. It is written by an operator through the API
   and owned by a named member. Its first firing runs **dry** (§2.8), and its
   candidate list is compared with SWEEP's last real sweep.
3. **The SWEEP job is paused** (`paused = true` in Terraform) in the same
   release that enables the schedule. Two triggers of one sweep never run: the
   per-issue live-run check already dedupes, but a paused job makes it
   obvious.
4. **After 7 days of the schedule firing without an auto-pause**, the next
   S9 change deletes `google_cloud_scheduler_job.issue_sweep` and its route's
   entry in `ROLLUP_SWEEPER_ROUTES`, and points SWEEP's docs here.

If SWEEP's merged names differ from its brief, S6 follows SWEEP's code and
records the difference in this document. That is the rule for when another
lane's layout contradicts this one.

### 8.2 The operator's session cron

Job `bdb47d30` is deleted by the operator (`CronDelete`) **once the first
`issue-sweep` schedule has fired for real** and its firing shows created issue
runs. It is a session object, not code, so its deletion is a step in S9's
acceptance, evidenced by the firing id. It must not be deleted earlier,
because until then it is the only sweep.

### 8.3 The repository index stays where it is (SD7)

**Recommended: keep `index.*` on the registration and the 5-minute poll as
they are.**

* The index's main trigger is **a change on the default branch**, found by an
  ETag poll, with in-flight dedupe and a minimum change interval. A cron
  expression cannot say "when the head moves".
* Folding it in would make the schedule tick read every repository's head every
  five minutes, a second poll beside the first.
* `index.interval_hours` is a backstop, not a schedule anyone plans around.

Instead:

* The Schedules list shows each registration's index cadence as a **read-only
  "built-in" row** ("Index · example-org/example-api · on change, at most every
  30 min, and every 24 h"), linking to the repository's Settings. A person sees
  all recurring work in one place.
* `repo-index-refresh` exists for what the built-in settings cannot express: a
  full refresh at a chosen time across several repositories, e.g. Sunday
  03:00.

Folding in (option b) would mean migrating every registration's settings into
a schedule and moving the change trigger into the tick. The owner may prefer
that for one model. It is a later lane either way, and nothing here blocks it.

### 8.4 Other existing ticks

`workflow_rollup`, `issue_run_advance`, `merge_wake`, `forge_refresh`,
`quota_refresh`, `reconciler` and `safety_tick` are **platform machinery**.
They are not jobs a tenant manages, and they stay in Terraform. The admin view
lists them read-only, as "Platform ticks", from a static list in the S7 lane,
so an admin sees every recurring thing in one place.

---

## 9. Build plan

Within a phase no file is in two lanes, and a lane depends only on earlier
phases. New files are named without their root.

| lane | phase | builds | territory | needs |
|---|---|---|---|---|
| S0 | 0 | **Verification, no code.** Each result is dated in §0 of this document. It checks: which of `actions: read` and `checks: read` the GitHub App and tenant tokens hold (§3.7, §3.9); whether update-branch is refused when the base brings `.github/workflows/` changes (§3.6); SWEEP's merged names (§8.1); the composite index shape for the tick query; and the Cloud Scheduler job's identity reuse (SD10) | `docs/schedules.md` | SWEEP merged |
| S1 | 1 | The model: the `schedules/` document and its validation, the cron parser with words and preview, gate resolution with floors, budget arithmetic, and the type catalogue with all twelve entries and `available` from the executor modules' presence (so later type lanes add a file and edit nothing shared) | new `swarm_api/schedules.py`, new `swarm_api/cronexpr.py`, new `swarm_api/scheduletypes.py`, new `tests/unit/control_plane/test_schedules_model.py`, new `test_cronexpr.py` | S0 |
| S2 | 2 | The tick and firings: §2.1-§2.11, `schedule_owner_auth`, `metadata.schedule` reserved, the tick route, auto-pause and its codes and switches | new `swarm_api/schedulefire.py`, new `swarm_api/routes/schedule_tick.py`, `apps/swarm-api/swarm_api/auth.py` (`ROLLUP_SWEEPER_ROUTES`), `apps/swarm-api/swarm_api/validation.py` (`RESERVED_METADATA_KEYS`), `apps/swarm-api/swarm_api/refusals.py`, `apps/swarm-api/swarm_api/main.py`, new `tests/unit/control_plane/test_schedule_tick.py` | S1 |
| S4 | 2 | Terraform: `google_cloud_scheduler_job.schedule_tick` (every minute, `retry_count = 0`, the sweeper's OIDC), the composite indexes (`schedules`: `state`, `next_run_at`; `schedule_firings`: `schedule_id`, `slot` desc; `approvals`: `tenant_id`, `state`, `requested_at`), the TTL policy on `schedule_firings.expire_at`, the `schedule_auto_paused` and `schedule_needs_owner` log metrics and alerts, and `terraform test` assertions. **No new IAM member**, so it is an ordinary release with no `dev-iam` approval, under SD10 (a) | `terraform/modules/scheduler/jobs.tf`, `terraform/modules/scheduler/variables.tf`, `terraform/modules/firestore/indexes.tf`, `terraform/modules/monitoring/`, new `tests/terraform/schedule_tick.tftest.hcl` | S0 |
| S3 | 3 | The tenant routes of §7.1 except approvals, and the admin list and actions | new `swarm_api/routes/schedules.py`, `apps/swarm-api/swarm_api/main.py`, new `tests/unit/control_plane/test_schedule_routes.py` | S2 |
| S5 | 3 | Approvals: `approvals/`, the inbox with projected `PLANNED` runs, the run, merge and proposal gates, the hard stops of §4.4 at plan and at merge, expiry, `schedule_audit/`, and the issue run's `metadata.schedule` with its lookup by firing id | new `swarm_api/approvals.py`, new `swarm_api/routes/approvals.py`, new `swarm_api/schedaudit.py`, `apps/swarm-api/swarm_api/issueruns.py`, `apps/swarm-api/swarm_api/issueci.py`, `apps/swarm-api/swarm_api/routes/runs.py`, new `tests/unit/control_plane/test_approvals.py` | S2 |
| S6 | 4 | The first types (SD2): `issue-sweep` (adopting SWEEP's module), `issue-plan-only`, `repo-index-refresh`, `observer` | new `swarm_api/schedtypes/` (`issue_sweep.py`, `issue_plan_only.py`, `repo_index_refresh.py`, `observer.py`), SWEEP's module, new `tests/unit/control_plane/test_schedtypes_*.py` | S3, S5 |
| S7 | 4 | The console: Work › Schedules (list, detail, create/edit, approvals pane), the Overview card, Admin › Schedules; the issue forms' "Where" list | new `Schedules.tsx`, `ScheduleDetail.tsx`, `ScheduleEdit.tsx`, `Approvals.tsx` and `AdminSchedules.tsx` in apps/swarm-ui/src, `apps/swarm-ui/src/App.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/Overview.tsx`, `.github/ISSUE_TEMPLATE/`, tests under `apps/swarm-ui/src/__tests__/` | S3, S5 |
| S8 | 4 | The plugin: the MCP tools of §7.2, `sc schedules` and `sc approvals`, and the docs | `apps/swarm-mcp/swarm_mcp/server.py`, `apps/swarm-mcp/swarm_mcp/sc.py`, new `swarm_mcp/schedules.py`, `plugin/commands/sc.md`, `plugin/README.md`, new `tests/unit/mcp/test_schedules_tools.py` | S3, S5 |
| S9 | 5 | Migration (§8.1-§8.2): the schedules created, SWEEP's job paused and later deleted, the session cron deleted; docs: issue-runs.md, operations.md, cost-control.md (§4.3's relation to 2026-10-01), multi-tenancy.md (§5.6) | `terraform/modules/scheduler/jobs.tf`, `apps/swarm-api/swarm_api/auth.py`, `docs/issue-runs.md`, `docs/operations.md`, `docs/cost-control.md`, `docs/multi-tenancy.md` | S6, S7, S8 |
| S10a-h | 6+ | One lane per remaining type: `epic-triage`, `pr-shepherd`, `ci-flake-hunter`, `dependency-cve-refresh`, `release-health`, `docs-drift`, `cost-report`, `custom-prompt` (with its admin spec approval). Each adds **one file** in `swarm_api/schedtypes/` and its test. They can run in parallel because the catalogue reads availability from the file's presence | one new file each | S6; S0 for the GitHub permissions |

**Acceptance per lane, measurable:**

* **S1.** The parser's table test covers every field form, the DST gap and
  repeat in `Europe/London` and `America/New_York`, and the Vixie OR. Every
  catalogue entry validates its own defaults. A gate below its floor is
  refused.
* **S2.** Two concurrent ticks over one due schedule create **one** firing,
  tested against the Firestore emulator in the integration job. A retried
  create returns the first work. A schedule paused between the query and the
  transaction fires nothing. Catch-up `skip` and `run_once` after a simulated
  3-hour gap write the expected records. The per-tenant cap of 5 holds.
* **S4.** `terraform test` asserts one `schedule_tick` job, `retry_count = 0`,
  the sweeper's email, `managed-by=swarm-terraform` in its description, and
  the three indexes.
* **S3.** Another tenant's schedule id answers 404 on every route. An unknown
  type answers 422 naming the available ones. A `params` extra key is refused
  by name.
* **S5.** Approving a stale digest answers 409. Two approvals of one item make
  one transition. A merge approval after a new push answers 409
  `merge_changed`. A plan touching `.github/workflows/` is refused, and one
  touching `terraform/bootstrap/` is held for the owner. An expired plan ends
  `REJECTED` with reason "expired" and no task exists.
* **S6.** SWEEP's tests pass unchanged against the executor. A dry-run sweep
  lists candidates and creates nothing. `observer` produces `report.md` from a
  fixture digest, offline with the mock runner.
* **S7.** The vitest for each screen covers its empty state (`Absent`), a
  partial spend (`Mark kind="partial"`) and a 390px layout. The issue forms
  test passes with the new tab.
* **S8.** The MCP tool list test includes the new tools, and
  `swarm_schedule_create` refuses `image`.
* **S9.** On dev:
  - the issue sweep fires as a schedule and is visible in the console;
  - `repo-index-refresh` or `observer` fires from a second schedule;
  - a member of another tenant gets 404 for both;
  - a schedule with `run: approve` holds its firing until a member approves it
    in the inbox;
  - job `bdb47d30` is deleted, with the firing id in the PR.

  These are #892's own acceptance criteria.

**Owner applies.** Under SD10 (a), **none**. The tick reuses the sweeper's
identity, and indexes, metrics and alerts are ordinary release resources. Under
SD10 (b), a dedicated identity is a bootstrap apply by the owner and a release
whose IAM plan waits in `dev-iam`. If S0 finds that `actions: read` is
missing, adding it to the GitHub App is an org-admin approval, which is the
owner's, and only S10's `ci-flake-hunter` and `release-health` wait for it.

**One review** (credentials, tenant isolation, IAM) for S2 (the tick
submitting on behalf of members), S5 (approvals and hard stops) and S10's
`custom-prompt`.

---

## 10. Decisions for the owner

Each decision is also in this lane's `questions.json`, with the mock-up
variant it previews. Nothing below is decided. Each line gives the
recommendation and why.

### SD1. Where do schedules live in the console?

* **(a) Work › Schedules, after Runs, with the approvals inbox as its second
  pane** (P1).
* (b) Inside Runs, as a segmented control (P2).
* (c) A new spine section, Automate (P3).

**Recommendation: (a)**, for the reasons in §6.1.

### SD2. Which types are built first?

* **(a) `issue-sweep`, `issue-plan-only`, `repo-index-refresh`, `observer`.**
* (b) `issue-sweep`, `observer`, `cost-report`, `release-health`.
* (c) `issue-sweep`, `issue-plan-only`, `epic-triage`, `pr-shepherd`.

**Recommendation: (a).**

* It meets #892's acceptance: the sweep, plus the index or the observer.
* It needs no GitHub permission S0 has not confirmed.
* `issue-plan-only` is nearly free once `issue-sweep` exists.

### SD3. The default gate per type

* **(a) As in the §3 table:**
  - the sweep plans automatically and **asks before merge**;
  - plan-only, epic-triage and custom-prompt ask before plan or run;
  - index, observer, flake, release-health and cost-report are auto;
  - the `.github/workflows/` refusal and the security owner gate are enforced
    from day one, not report-only.
* (b) Stricter for the first two weeks: every acting type at `run: approve`,
  loosened per schedule once its firings look right.
* (c) Looser: the sweep merges automatically on a green, reviewed MERGE
  verdict (R3), as SWEEP's brief says "plan, execute and merge".

**Recommendation: (a).** An unattended merge is the one irreversible step,
and approving a green, reviewed pull request is one click in the inbox. (c)
can be chosen per schedule later by an admin.

### SD4. Budget defaults

* **(a) Per type, enforced as §4.3 says:**

  | type | per run | per day | concurrent |
  |---|---|---|---|
  | `issue-sweep` | $15 | $120 | 8 |
  | `issue-plan-only` | $3 | $30 | 5 |
  | `observer` | $5 | $10 | 1 |
  | `repo-index-refresh` | $5 | $40 | 4 |

  Others are set in their lanes. These are proposals; no measurement backs
  them.
* (b) No dollar figures at all, only `max_concurrent`. This is the 2026-10-01
  decision applied to schedules.
* (c) One per-tenant daily schedules budget shared by all its schedules.

**Recommendation: (a).** The owner's request names dollars per run and per
day. §4.3 makes them firing gates with the unknown counted at the cap, so
they never claim to stop a run they cannot stop.

### SD5. Who may approve?

* **(a) Any member of the tenant.** R3 needs a person other than the last
  editor of the gate, scope or spec when the tenant has more than one member.
  Owner holds and security plans are owner-only.
* (b) Only the schedule's owner, plus the owner holds.
* (c) Only platform admins.
* (d) Members for R0-R2, admins for R3.

**Recommendation: (a).** It is the issue runs' rule, plus a two-person check
on the one tier that merges.

### SD6. Catch-up after an outage

* (a) Always skip missed slots.
* (b) Always run the newest missed slot once.
* **(c) Per type:** `run_once` for reports and the index, `skip` for types
  that act on repositories. Overridable per schedule.

**Recommendation: (c)** (§2.4).

### SD7. The repository index and platform-scope schedules

* **(a) Keep `index.*` and the poll; show them as built-in rows;
  `repo-index-refresh` for timed full refreshes.** Platform-scope schedules
  run in a tenant the owner names.
* (b) Fold the index's interval into a `repo-index-refresh` schedule per
  registration, and move the change trigger into the tick.

**Recommendation: (a)** (§8.3).

### SD8. Types with no agent

* **(a) `cost-report`, `release-health` and `pr-shepherd`'s API actions run in
  swarm-api,** recorded as firings with `work.kind: api_action`, using no
  capacity.
* (b) Every firing is a task. These types run a `claude-code` task that makes
  the same calls.

**Recommendation: (a).** A deterministic API call does not need a model or a
worker. (b) spends tokens to call an endpoint, and gives an agent a write it
does not need.

### SD9. GitHub-comment approvals

* **(a) Phase 2.**
* (b) In the first build.

**Recommendation: (a)** (§4.9).

### SD10. The tick's identity

* **(a) Reuse `swarm-rollup-sweeper`** and add one route. No owner apply is
  needed.
* (b) A dedicated `swarm-schedule-tick` account. That means an owner bootstrap
  apply and `dev-iam`.

**Recommendation: (a)** (§2.1).

---

## 11. Invariants, each with how it holds

1. **Demand only from `LEASED`…`RUNNING`.** A schedule, a firing, a queued
   firing, a pending approval and a `PLANNED` run are Firestore documents.
   A firing's work is created `QUEUED` or `READY` and takes capacity only at
   admission. Nothing in this design creates a pod, a lease or a slot. The
   tick is a request to swarm-api, not a worker.
2. **All-or-nothing reservation.** Unchanged: firings submit through the
   ordinary paths, and admission is untouched.
3. **Concurrency from `LEASED`.** Unchanged. `max_concurrent` counts the
   schedule's live work items from creation, which is stricter than `LEASED`
   and is a firing bound, not a pool.
4. **Workers never sleep through a wait.** No worker waits for an approval:
   the approval is before the work exists (run), between a planner task that
   ended and a workflow not yet created (plan), or after a run that is `DONE`
   (merge). The tick never waits inside a request beyond its 240 s budget.
5. **Fencing.** Unchanged for attempts. The firing's own fence is the creation
   of `schedule_firings/{id}:{slot}` in a transaction (§2.2), and every
   approval moves its subject in the transaction that checks its digest.
6. **No Spot.** Nothing here declares any.
7. **`requests == limits`.** Unchanged. Types name profiles whose classes are
   fixed.
8. **Checkpointing.** Unchanged for the work a firing creates. A firing has
   nothing to checkpoint: its state is its document.
9. **Isolation.** Every collection is tenant-scoped, with the 404 rule. Every
   firing submits as a current member, in its tenant, with its tenant's
   credentials and prefix. Platform-scope types read aggregates only and run
   in a named tenant. An admin can pause but cannot edit or approve another
   tenant's schedules.
10. **Profiles by name.** A caller picks a schedule **type** by name, and a
    type names its profile in code. No route here accepts an image, a
    command, a resource spec or a backend parameter. `custom-prompt`'s spec is
    the ordinary `WorkflowCreate`, which already refuses them.

---

## 12. Contract change requests

**None is required.** One is optional. It is written here as a request, for
the owner to decide whether to file it in
[contract-change-requests.md](contract-change-requests.md). This lane's
territory does not include that file.

### Request S-A (optional). `profiles.py`: a declared `context` input on `claude-code`, a swarm-api-composed data file staged into the workspace

**What is true today.** `claude-code` declares one input, `issue`, which the
worker stages as `issue.md` (contract request 28). Data that swarm-api
composes, like the issue run's open work, goes into the prompt between
delimiter lines, inside `MAX_PLANNER_PROMPT_BYTES` (64 KiB), because the
claude-code runner passes the prompt as one argv string and Linux refuses a
single argument over 128 KiB.

**Why.** `observer`'s digest over 168 hours, and `cost-report` if it ever runs
an agent, can exceed 48 KiB for a busy tenant. Then the digest is truncated,
and the report says so.

**The requested change.** `_CLI_AGENT_INPUTS` gains `context`: an object
reference, `{uri, sha256, bytes}`, to an object under the task's own tenant
prefix, written by swarm-api. The worker stages it as `context.json` after
checking the digest. The bound is 1 MiB. A caller may not set it. Like
`input_from`, it is a swarm-api reserved key.

**What it would break.** Nothing existing: the input is optional. Workers
built before it refuse it, so the rollout order is worker, then API.

**If it is declined.** `observer` keeps the 48 KiB prompt digest, truncated
with a stated count. That is enough for the 24-hour default.

**Invariants.** 9: the object is under the task's tenant prefix, read by that
tenant's worker. 10: it is data, not an image, a command or a resource spec.
Others are untouched.
