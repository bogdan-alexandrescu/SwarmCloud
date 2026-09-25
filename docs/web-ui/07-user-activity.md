## Per-user and per-tenant activity over time

This section covers requirement 6: *"for each engineer using SwarmCloud multi-tenantly, how many agents, tokens and jobs they have used over time, filterable by hour, day, week, month."*

Of the four nouns, **one** is fully real today. One is real but unreachable from this repo, one is not recorded at all (the capture that was supposed to record it does not fire — proven below), and the word "filterable" is not free. Read the ground-truth table before designing anything here.

---

### 0. Ground truth

Every row was checked by opening the file, and the token row was checked by **executing the code**, not by reading it.

| The owner asked for | Answerable today? | Source of truth |
|---|---|---|
| **Jobs** per engineer | Yes, **within a bounded row window** | `tasks/{id}.submitted_by` (`apps/common/swarm_common/models.py:163`), returned by `task_to_api` (`apps/swarm-api/swarm_api/codec.py:114`) |
| **Jobs** per engineer, calendar range ("all of August") | **No** | `list_tasks` has no `since`/`until` and no index covers `submitted_by` — see P1 and P3 |
| **Agents** (attempts that actually ran) per tenant, per hour/day/week/month | Yes, **but nothing in this repo can read it yet** | Cloud Monitoring `logging.googleapis.com/user/swarm/starting`, labelled `tenant_id` + `runner_profile` (`terraform/modules/monitoring/metrics.tf:42-101`, `:103-136`) |
| **Agents** per *engineer* | **No, and not cheaply fixable** | `submitted_by` never reaches the worker — `worker_env` carries identifiers only (`apps/scheduler/scheduler/dispatch.py:194-220`), and `build_logger` binds no user label (`apps/agent-worker/agent_worker/logs.py:183-201`) |
| **Tokens / provider cost** per task | **No. The capture exists and never fires.** | `_usage_summary` (`apps/agent-worker/agent_worker/lifecycle.py:1055-1103`) is correct, but it is called on the **wrong object** at `lifecycle.py:474`. See the box below — `result_summary.runner.usage` is `{}` on every task in the database |
| **Tokens** per engineer, aggregated | **No**, twice over | Nothing to aggregate (above), and no index/`sum()` target if there were — see P4 |
| **Cost / spend**, compute | **No** | No billing export anywhere in `terraform/` — no `google_logging_sink`, no BigQuery dataset |
| **Budget per tenant** | **No — this is a trap** | `Tenant.monthly_budget_usd` exists in the model (`models.py:246`) and is returned by `tenant_to_api` (`codec.py:297`), but `PUT /v1/admin/tenants/{id}/limits` **422s** on it (`routes/admin.py:178-195`). It is `None` for every tenant. Rendering it is rendering a permanent blank that looks like "no budget set" |
| **Per-attempt** anything (runtime per try, peak RSS, exit code) | **No API** | The `attempts` collection has an index (`indexes.tf:171-178`) and is written by the worker, but **no route reads it**. `/v1/tasks/{id}` sub-routes are `events` and `artifacts` only (`routes/tasks.py:123`, `:134`) |

#### The token capture does not work. Verified by running it.

`ef1a9c5` ("Keep the token numbers…") added `_usage_summary` to lift `input_tokens`, `output_tokens`, `cache_*`, `thinking_tokens`, `total_cost_usd`, `num_turns`, `duration_ms`, `duration_api_ms` and `models[]` out of a CLI agent's result before `_truncate_json` destroys it. The function is right. **The call site is one level too shallow.**

* A runner's `body()` returns `{summary, provider, model, exit_code, structured_output, limits, metrics}` (`apps/agent-worker/agent_worker/runners/cliagent.py:342-357`). The Claude Code JSON — the object that actually holds `usage` and `total_cost_usd` — is nested under **`structured_output`**.
* `run_runner` writes `result.json` with `output` = that dict minus `status`/`summary`/`metrics` (`apps/agent-worker/agent_worker/runners/base.py:228-239`).
* `lifecycle.py:474` then calls `_usage_summary(runner_result.get("output"))` — the envelope, whose top-level keys are `exit_code, limits, model, provider, structured_output`. `output.get("usage")` is `None`. `output.get("total_cost_usd")` is `None`.

Executed against the real functions with the real result shape:

```
keys of result.json['output'] : ['exit_code', 'limits', 'model', 'provider', 'structured_output']
what lifecycle.py:474 computes : {}
what the unit test computes    : {"input_tokens": 8, "output_tokens": 402, ... "total_cost_usd": 0.0642028}
```

`tests/unit/worker/test_usage_summary.py` passes `_usage_summary` the **inner** CLI object directly, so the suite proves the function and never exercises the wiring. Nothing else calls `_usage_summary` — one call site, repo-wide.

**Consequence for this section:** `result_summary.runner.usage` is present and empty on every task. Any tile, column or sum built on it renders `0`, `~$0.00`, or `0% coverage` forever. That is worse than omitting it, because a zero reads as "this engineer spent nothing". **Every token and cost surface below is therefore specified as a blocked state with its reason on screen, not as a number.** The unblock is a one-line change in Track B (`_usage_summary(… .get("structured_output"))`, plus a test at the `result.json` level) — carried below as **P4a**, and it must be picked up by the prerequisites section.

#### Two further traps found while writing this

* **`workflows/{id}.state` is written once, as `QUEUED`, and never updated by anything.** The only writers of the `workflows` collection are `create_workflow` (`apps/swarm-api/swarm_api/store.py:537-544`, seeded `state=TaskState.QUEUED` at `service.py:258`) and `cancel_workflow` (`store.py:577-583`), and the latter sets only `cancel_requested` and `updated_at`. No scheduler or reconciler code touches the collection at all. The index `workflows-tenant-state-created` (`terraform/modules/firestore/indexes.tf:219-226`) covers a field that never changes. **Any "workflows succeeded / failed" chart renders 100% QUEUED forever.** Workflow outcome must be derived from its member tasks (`tasks.workflow_id`), never from `workflows.state`.
* **`GET /v1/tasks` returns the whole task document per row**, including `input` and `result_summary` (`codec.py:101-131`). `input` is admitted up to 256 KiB (`apps/common/swarm_common/config.py:72`), so a 200-row page (`apps/swarm-api/swarm_api/settings.py:81`) is bounded only at ~51 MB. The typical figure is **unmeasured** — do not quote one until someone measures a real tenant. This is the single biggest reason the phone layout needs P7.

#### One engineer belongs to exactly one tenant

The requirement says "multi-tenantly", so this has to be stated rather than assumed. `resolve_tenant` walks the admin-ordered `tenant_groups` list and takes the **first** group the caller is a member of, falling back to a personal `u-<user>` tenant (`apps/common/swarm_common/identity.py:101-113`); `_tenant_principal` mirrors that rule exactly (`apps/swarm-api/swarm_api/auth.py:234-245`). So a person in three mapped groups still lands deterministically in one tenant, and "one engineer's activity split across tenants" cannot arise today.

The caveat that can: **reordering `tenant_groups` re-homes a user.** Their new tasks get the new `tenant_id`; their old tasks keep the old one and disappear from the view. If that list is ever reordered, every screen here silently loses history for the affected people, with no marker. Say so in the docs for that setting.

---

### 1. The one design rule for this section

> **Superseded 2026-09-25 (#185).** The rule below held while the task list
> could not filter by time, and the Timeline was built on it. `GET /v1/outcomes`
> removes that premise: the Timeline now reads a real span (24h to 90d, 14d by
> default, or a range), the server buckets it in the viewer's zone and derives
> every figure on one `completed_at` basis from a per-tenant, per-day rollup,
> and the Rows control is retired. What the screen draws, and why each part is
> drawn the way it is, is [design-system.md §16](design-system.md#16-the-timeline-as-an-outcome-ledger-185-owner-decisions-2026-09-25).
> This section is kept as the record of the constraint the old design answered.

> **Bound by rows. Label by the span those rows actually covered.**

The platform can serve "the most recent N tasks for this tenant" cheaply and exactly (`tasks-tenant-created` index, `tenant_id ASC, created_at DESC`, `terraform/modules/firestore/indexes.tf:103-110`). It cannot serve "everything in August" at all.

So the window control does **not** say "Last 7 days". It says **"Last 500 tasks"**, and the header then reports the span those 500 tasks turned out to cover: *"14 Sep 09:12 → 19 Sep 08:44 (4d 23h)"*. If the tenant is busy, that span is 6 hours; if quiet, 3 months. Both are true statements. "Last 7 days" would be a lie the moment the window truncates, and it is exactly the class of lie this platform keeps shipping.

This also has a useful cost property: the read cost is fixed at N regardless of volume. At 100x volume the screen costs the same and covers less time — which is visible in the header rather than silent.

Hour/day/week/month bucketing then happens **client-side over rows already fetched**, in the **viewer's IANA timezone**, with the timezone printed. A "day" boundary silently in UTC moves 7 hours of a Pacific engineer's work into the wrong day.

**The bucket control groups; it does not filter.** The owner's word was "filterable". Selecting `Day` re-buckets the rows already in the window — it does not fetch a different range, and it cannot, until P1. Label the control *Group by*, not *Filter*, so nobody reports a bug against a control that is doing what it says.

**Never let a clamp read as an end of data.** `paged_limit` silently takes `min(requested, 200)` (`apps/swarm-api/swarm_api/deps.py:194-199`) — a client that asks for `limit=500` gets 200 rows and a `next_page_token`, with no error and no warning. The row budget is therefore always a client-side fan-out of 200-row pages, and "no `next_page_token`" is the only honest end-of-data signal.

---

### 2. Screen A1 — Activity

> **Superseded 2026-09-25 (#185).** Screen A1 is now the Timeline's outcome
> ledger: a success-rate headline, four aligned lanes (rate with its Wilson
> interval, decided work, cancels on their own scale, and throughput as the one
> submission-time series), a Table twin and eight cards. The stat strip, the
> Token spend tile, the runner-profile split and the People table below are
> replaced by the ledger's readout, "Retries and attempts", "Reported cost · not
> a bill" and "Reliability by runner profile, tenant or person"
> ([design-system.md §16](design-system.md#16-the-timeline-as-an-outcome-ledger-185-owner-decisions-2026-09-25)).

The tenant's timeline. One screen, dense.

#### What the user sees

**Window bar (sticky, always visible).** Left: `eng` · `eng@saga.xyz`. Centre: `Last 500 tasks · 14 Sep 09:12 → 19 Sep 08:44 · Europe/Bucharest`. Right: a *Group by* selector `[ Hour | Day | Week | Month ]`, a row-budget selector `[ 200 | 500 | 1000 | 2000 ]`, and a refresh control showing `updated 41s ago`.

**Chart (primary).** Stacked columns, one per bucket, newest right.
Series and stack order, bottom to top: `SUCCEEDED` (green), `FAILED` (red), `DEAD_LETTERED` (dark red), `CANCELLED` (grey), `still open` (hatched — any task in the window whose `completed_at` is null).
Bucketed on **`completed_at`** for the four terminal series and on **`created_at`** for a thin overlaid line labelled *submitted*. Both are labelled on the axis legend; they are genuinely different questions and the chart must not merge them.

`completed_at` is safe to bucket on: **every** writer that moves a task to a terminal state sets it — the worker (`control.py:621`), the API's immediate cancel (`store.py:440`), the scheduler's cancel (`scheduler/store.py:318`) and the reconciler's reclaim (`reconciler/store.py:237`). A terminal row with a null `completed_at` is a data bug, and the chart should count it in *still open* **and** surface it in the state strip rather than dropping it.

**Stat strip, four tiles.** Each tile carries its own state dot (see §6):
- `Submitted` — count of rows in window. Sub-label: the span.
- `Completed` — count of rows with `completed_at != null`, and the success rate as `SUCCEEDED / completed`.
- `Attempts consumed` — `sum(attempt_count)` over rows. Sub-label: *"admissions, not runs — a lease reclaimed before dispatch counts here"* (see below).
- `Tokens & spend` — **permanently in the blocked state until P4a.** It renders the words *"not recorded"*, never a number, never `~$0.00`, and taps through to the explainer below. It stays on the screen rather than being removed, because the owner asked for tokens and an absent tile answers nothing.

> **Superseded 2026-09-25 (epic #84, TS-11 and TS-12).** The strip is the shared boxless `.ctl-metrics` strip with **three** figures: `Submitted` is gone, because the window bar above prints the same count beside the same span. `Tokens & spend` is now `Token spend`: the sum of `total_cost_usd` over the rows whose result carries one, with the foot `{k} of {n} tasks · from result` and the `partial` mark whenever `k < n` or a summed task ran more than once (a result is its last attempt). With no cost in any result it still says *"not recorded"* beside the absent mark, never `$0.00`. The coverage bar the explainer below was replaced by is gone too: it drew a row count where the figure goes, and the foot now states the coverage in words.

**The "not recorded" explainer (mandatory, not optional chrome).** Plain language, naming the cause: *"Token and cost numbers are produced by the agent CLI on every run, but the platform does not currently store them — the capture added on 19 Sep reads the wrong level of the result object, so every task records an empty usage block. The full numbers still exist in each attempt's transcript in GCS for `artifact_retention_days` (14 days in dev, 180 in prod), one file per attempt."* Once P4a lands, this tile becomes a real number and this explainer is replaced by the coverage bar specified in §3's *what it cannot show yet*.

**Secondary: runner-profile split.** A small horizontal bar list — `claude-code 411`, `codex 58`, `browser 31` — from `task.runner_profile`. The five profiles that exist are `mock`, `generic`, `claude-code`, `codex`, `browser` (`apps/common/swarm_common/profiles.py:118-175`); the UI must render whatever string it receives rather than a hardcoded list, because the catalogue is frozen contract data and can gain entries. This is the honest proxy for "what kind of agent" and it costs nothing extra.

#### Where every value comes from

| Value | Source | Computation |
|---|---|---|
| rows | `GET /v1/tasks?limit=200[&page_token=…]` → `apps/swarm-api/swarm_api/routes/tasks.py:64-94` → `Store.list_tasks` (`store.py:381-411`) | N rows = `ceil(N/200)` **sequential** calls; `page_token` chains from the prior response; stop on a null `next_page_token`, never on a short page |
| bucket key | `task.created_at` / `task.completed_at` | Floor to local boundary. Weeks ISO-8601 (Mon start), months calendar months, in the browser's IANA zone |
| outcome series | `task.state` | Only the four values in `TERMINAL_STATES` (`apps/common/swarm_common/states.py:46-53`) go in the stacks; everything else is *still open* |
| attempts consumed | `task.attempt_count` | Incremented once per lease acquisition inside the admission transaction (`apps/common/swarm_common/admission.py:236`). **This counts admissions.** A lease reclaimed by the reconciler before dispatch still increments it, so `attempt_count ≥` the number of agents that actually started. Those are two real, different numbers and the UI must never present them as one |
| tokens / spend | *(blocked)* `task.result_summary.runner.usage` | The key is always present and always `{}` today (§0). The client must test **present and non-empty** and render the blocked state otherwise — never `?? 0` |
| runner split | `task.runner_profile` | group + count |
| tenant identity | `GET /v1/tenants/me` → `{tenant, principal{email, domain, groups, is_admin}}` (`routes/tenants.py:24-38`) | one document read |

**This screen deliberately does not call `GET /v1/stats`.** See §7.

#### What it cannot show yet

- **A calendar range.** "August" is not expressible. `list_tasks` applies exactly one inequality, `created_at < before`, decoded from the page token (`store.py:400-402`). Blocked on **P1**.
- **Tokens, cost, or anything derived from them.** Blocked on **P4a** (make the capture fire) and then **P4** (aggregate it without reading every document).
- **An honest hourly chart over a long span.** Hour buckets over 500 rows of a busy tenant cover a few hours. The bucket selector must *disable* Month when the covered span is under 60 days, with the reason in the tooltip — not render one lonely column.
- **"Agents started"** as distinct from "tasks". The real per-attempt throughput signal is the Cloud Monitoring `starting` metric, and nothing in this repo can read it (**P5**). Until then the screen shows `attempt_count` and is explicit that it is admissions. Per-attempt documents cannot fill the gap either: the `attempts` collection has no route (§0).
- **Total compute spend.** No billing export exists in `terraform/` — no `google_logging_sink`, no BigQuery dataset. `PUT /v1/admin/tenants/{id}/limits` refuses `monthly_budget_usd` for exactly this reason (`routes/admin.py:178-195`), and the UI must not quietly disagree with the API.
- **Per-attempt history for a retried task.** `result_summary` is written only by `ControlPlane.finish()` (`apps/agent-worker/agent_worker/control.py:610-635`), once, at terminal state, and only when the worker reaches it. `park()` (`control.py:582-607`) writes none; a reconciler reclaim (`reconciler/store.py:225-241`) and an API or scheduler cancel write none; a fenced or crashed worker never reaches `finish()` at all. So even after P4a, a task that failed twice and succeeded on the third attempt will carry **attempt 3's numbers only**. Every row with `attempt_count > 1` must carry a marker: *"last attempt only — earlier attempts were not recorded."*

#### Refresh and cost

- **One refresh = N document reads** (N = the row budget), plus 1 for `/v1/tenants/me`. 500 rows ≈ 501 reads ≈ **$0.00015** at ~$0.03/100k reads.
- **Latency is the real constraint, not money.** 500 rows is 3 sequential round trips; 2000 rows is 10. Budget ~150 ms each. Show incremental progress (*"page 3 of 10"*) and render the chart from the pages already in hand rather than blocking on the last one — with the partial state marked (§6), because a chart drawn from 3 of 10 pages is a chart of a shorter span.
- **Auto-refresh: 60 s, pause when the tab is hidden, and stop after 15 minutes of no interaction.** A forgotten tab at 5 s with a 2000-row budget is 1.4 M reads/day for one idle browser.
- **Rate limit.** `requests_per_second` is 20 per instance (`apps/common/swarm_common/config.py:74`, echoed as `requests_per_second_per_instance` by `/v1/stats`); `RateLimited` returns 429 with a `Retry-After` header (`apps/swarm-api/swarm_api/main.py:102-103`). The client honours it with exponential backoff. Never retry-storm a paginated fan-out.
- **At 100x volume:** read cost is unchanged (bounded by rows); the *covered span* collapses — 500 tasks might be 40 minutes. The chart stays correct because the header states the span. The degradation is honest and visible, which is the point. The thing that genuinely breaks is the **payload**: 500 rows × up to 256 KiB of `input` each. Fix with **P7**.

#### Phone layout (390 pt)

- Window bar sticks at 52 pt: `eng · Last 500 · 14–19 Sep`. Tap opens a sheet with grouping, row budget and timezone.
- Bucket selector on phone offers **Day / Week / Month only**. Hour is available only after the window narrows to ≤ 48 h, because 120 bars at 3 pt are a texture, not a chart.
- Chart: full-bleed minus 16 pt gutters (358 pt), 180 pt tall, horizontally scrollable with snap, newest anchored right, the y-axis label pinned. Bars ≥ 10 pt wide.
- Stat tiles: 2×2 grid, 171 × 84 pt each. **The blocked `Tokens & spend` tile keeps its "not recorded" label on phone** — it is not allowed to degrade to a dash or to be dropped from the grid.
- Runner-profile split collapses to a single line: `claude-code 411 · codex 58 · +1`, tap to expand.
- Always visible on phone, at any size: the covered span, the blocked/partial markers, and any non-`ok` state.

---

### 3. Screen A2 — People

Per-engineer breakdown. This is the requirement's core, and it is the part standing on the thinnest ice.

#### What the user sees

A single scrollable table, derived from the **same rows already fetched for A1** — zero additional reads.

Desktop columns: `Engineer` · `Tasks` · `Succeeded` · `Failed` · `Dead-lettered` · `Cancelled` · `Open` · `Attempts` · `Last-attempt runtime p50` · `p95` · `Last active`.

There is **no cost column and no token column** in v1. They are specified in *what it cannot show yet* so they can be switched on the day P4a lands, and until then the table carries a single footer line: *"Token and cost columns are not shown because the platform records no usage — see Activity."*

Above the table, a persistent, non-dismissible caption in the same type size as the numbers, not smaller:

> **Based on the last 500 tasks for `eng` (14 Sep 09:12 → 19 Sep 08:44). This is not an all-time total.**

Sort defaults to `Tasks` descending.

**Engineer drawer** (tap a row): that engineer's rows from the window as a compact ledger — `created_at` · `runner_profile` · `state` · `attempt_count` · runtime · task id — plus their runner-profile mix and a sparkline in the current bucket size. A task id opens the task detail (owned by another section); the ledger links to it rather than reimplementing it.

#### Where every value comes from

| Value | Source | Computation |
|---|---|---|
| Engineer | `task.submitted_by` (`codec.py:114`) — the verified email from the ID token, never client-supplied | group rows by this exact string; no normalisation, no display-name lookup (there is no directory read in this platform) |
| Tasks / per-state counts | `task.state` | group + count |
| Attempts | `task.attempt_count` | sum; same admissions caveat as A1 |
| Runtime p50 / p95 | `task.completed_at - task.started_at` | **Only where both are non-null.** `started_at` is written at `DISPATCHED → STARTING` (`control.py:410-414`) and is **overwritten on every retry** — so this is the *last attempt's* wall time, not total compute. Column header reads **"Last-attempt runtime"**, and the tooltip says why. Per-attempt runtimes exist in the `attempts` collection and are unreachable: no route reads it |
| Last active | `max(task.updated_at)` | — |

#### What it cannot show yet

- **A per-engineer query.** `Store.list_tasks` accepts `state`, `workflow_id` and `runner_profile` and nothing else (`store.py:381-389`), and no index in `terraform/modules/firestore/indexes.tf` contains `submitted_by` in any position. The needed index is `tenant_id ASC, submitted_by ASC, created_at DESC` — Firestore serves `<equalities> ORDER BY created_at DESC` only from an index whose ordered field follows the equalities immediately, and the store's own header (`store.py:8-25`) names this as the reason four endpoints previously 500'd. Without the index the query fails `FAILED_PRECONDITION`; it does **not** degrade. Blocked on **P3**.
- **Tokens and provider cost per engineer.** Blocked on **P4a**, then **P4**. When they arrive the columns are `~Cost` (tilde always rendered, since it is provider cost only and a lower bound) and `Coverage` (a two-tone micro-bar: *rows with a non-empty usage object / rows for this engineer*, never a percentage buried in a tooltip). Ship both together or neither: a cost sum without its coverage bar is a fabrication with a dollar sign on it.
- **Anyone who submitted nothing in the window.** An engineer on holiday simply is not in the table. The caption above is what stops that reading as "they have never used the platform". Do not pad the table with zero rows for a roster you do not have — there is no roster; the tenant document holds a `principal`, not a member list.
- **Cross-tenant "everyone".** Every list path is hard-scoped to `auth.tenant_id` at the route (`routes/tasks.py:84`), deliberately (`store.py:5`). `is_admin` unlocks a good deal — dispatch pause/resume, every pool-limit `PUT`, the provider and resource drains, plus `GET /v1/admin/pools` and `GET /v1/admin/tenants` (`routes/admin.py:70-310`) and the platform-wide state counts (`service.py:286-287`) — but **no admin route returns another tenant's tasks, attempts or events**. A platform-wide per-user page is a **security boundary change**, not a parameter — out of scope for this section, and it must stay that way under CONTRACT.md invariant 9.
- **Workflows per engineer, by outcome.** `Workflow.submitted_by` exists (`models.py:324`) and `GET /v1/workflows` is tenant-scoped, but `workflows.state` is frozen at `QUEUED` forever (see §0). If the owner wants workflow counts, derive them from `tasks.workflow_id` — a "workflow succeeded" is *all its member tasks terminal and none failed*, computed from rows already in hand, and honest only when the whole workflow fits inside the row window. Blocked on **P10** for anything better.

#### Refresh and cost

- **Zero marginal reads.** A2 is a second view over A1's rows. This is deliberate and is the main argument for the bounded-row design: one fetch, two screens.
- The drawer is also free — it filters rows already in memory.
- **At 100x:** unchanged cost, shrinking covered span, honestly labelled. Once **P3** lands, a per-engineer query becomes ~1 aggregation read per bucket via `count()` (already proven working at `store.py:167-172`), which is the cheap class.

#### Phone layout

An eleven-column table is not a phone screen. On phone, A2 is a **list of cards**, one per engineer, 72 pt tall:

```
Line 1   alice@saga.xyz                      47
Line 2   38 ok · 6 failed · 3 open       p50 4m
Line 3   last active 2h ago
```

Numbers right-aligned in tabular figures. One metric on line 2 is owner-selectable (attempts / runtime p50 / dead-lettered) and the choice persists; `cost` joins that list only when P4a has landed. The caption ("Based on the last 500 tasks…") is pinned above the list and does not scroll away. Tapping a card pushes the drawer as a full screen, not a bottom sheet — the ledger needs the height.

---

### 4. Panel A3 — Agents started (Cloud Monitoring)

**This panel is not buildable today.** It is specified because it is the only source that answers "how many agents, per hour/day/week/month" over a long span at near-zero cost, and it is already deployed and collecting.

#### What the user sees

A second chart on A1, tab-switched with the task chart: `[ Tasks | Agents started ]`. Columns per bucket, stacked by `runner_profile`, over a range control that is genuinely calendar-based (`24h / 7d / 30d`) because this source really can serve it. A footnote states the metric's first data point, and any bucket before it renders as **outside the axis**, never as zero.

#### Where every value comes from

- Metric: `logging.googleapis.com/user/swarm/starting`, DELTA INT64, labels `tenant_id` and `runner_profile` (`terraform/modules/monitoring/metrics.tf:48-54`; name assembled at `:107` as `${var.name_prefix}/${replace(key,"_","-")}`). `name_prefix` defaults to `swarm` at the root (`terraform/infra/variables.tf:24-28`), is passed straight through to the module (`terraform/infra/main.tf:397`), and no `.tfvars` under `terraform/environments/` overrides it — so the type is `logging.googleapis.com/user/swarm/starting`. **Read the deployed metric's name rather than hardcoding it** if that default ever becomes environment-specific.
- Emitted once per attempt that actually began running, by `ControlPlane.emit(EventType.STARTING)` (`apps/agent-worker/agent_worker/control.py:345-376`, called from `advance_to_running`, `control.py:410-414`). This is genuinely "an agent started", not "a task was admitted".
- Read via `projects.timeSeries.list`, filter `metric.type="…/starting" AND metric.labels.tenant_id="<tenant>"`, `perSeriesAligner=ALIGN_DELTA`, `crossSeriesReducer=REDUCE_SUM`, `groupByFields=metric.label.runner_profile`, `alignmentPeriod` 3600s / 86400s / 604800s.

#### What has to be built first (P5)

1. `roles/monitoring.viewer` on the `swarm-api` service account. It is **not** granted today — `terraform/modules/iam/bindings.tf:73-102` gives it to `swarm-quota-broker` and `swarm-reconciler` only, and `swarm-api` gets `telemetry_roles` plus `roles/serviceusage.serviceUsageConsumer`. One line, Track C.
2. `google-cloud-monitoring` added to `apps/swarm-api/pyproject.toml` (it is in `apps/agent-worker` only today; `swarm-api`'s dependency list is firestore / secret-manager / pubsub / auth / prometheus).
3. A new route `GET /v1/activity/series` that applies `metric.labels.tenant_id = auth.tenant_id` **server-side**. The tenant filter must never be a client parameter — that is invariant 9 in a query string.

**The browser must never call Cloud Monitoring directly.** It would need credentials and would put the tenant filter on the client.

#### Refresh and cost

- Cloud Monitoring read API calls are not billed per data point, but they are quota-limited per project. One call per panel render. **Refresh: 5 minutes.** Log-based metrics lag ingestion by tens of seconds; a 10-second refresh buys nothing and burns quota.
- **At 100x:** unchanged — this is aggregated server-side by Google. It is the cheapest source in the whole section, which is precisely why P5 is the highest-value prerequisite per unit of work.
- **Retention:** governed by Google, not this repo. **Do not hardcode a range cap.** Read it from the current Google documentation at build time, put it in config, and until it is verified cap the picker at 30 days with the cap labelled.

#### What it can never show

Per-engineer anything. `submitted_by` does not reach the worker (`dispatch.py:194-220`), is not a bound log label (`logs.py:183-201`), and is not in the event detail written by `create_tasks` (`store.py:351-363` writes `runner_profile`, `resource_class`, `state`, `workflow_id` — and no submitter). Making it possible is **P6**, which I recommend against: it widens what a worker knows about a submitter, and Cloud Monitoring label cardinality would then grow with headcount, on a metric written once per attempt.

#### Empty and error states

Distinct and mandatory, because three different conditions look identical as a flat line at zero:
1. **403 / permission missing** (P5 step 1 not applied) — *"this deployment has not granted the API read access to metrics"*, with no chart drawn.
2. **No data points in range** — the axis is drawn, the columns are absent, and the footnote names the metric's first data point.
3. **Genuinely zero attempts in a bucket** — a zero-height column with a baseline tick.

#### Phone

Same chart geometry as A1. The range picker is a segmented control, not a date picker — `24h / 7d / 30d` only. The runner-profile legend becomes a wrapped chip row below the chart, with a tap to isolate a series.

---

### 5. Screen A4 — Tenants (admin only)

Rendered only when `GET /v1/tenants/me` returns `principal.is_admin == true`. Hidden entirely otherwise — not disabled, not a 403 page.

#### What the user sees

One table, one row per tenant, from `GET /v1/admin/tenants` (`routes/admin.py:305-310`): `tenant_id` · `kind` (group|user) · `principal` · `enabled` · `max_active` · `capacity_units` · `credentials` (provider-name chips) · `namespace` · `created_at`.

A disabled tenant renders with a struck-through id and a plain-language consequence rather than a status word: *"members of this tenant receive 403 on every call"* — which is what `tenant_for` actually does (`service.py:97-99`). It is not a soft flag.

Three rules this screen must not break:

- **`monthly_budget_usd` is not rendered.** It is in the payload (`codec.py:297`) and it is `None` for every tenant, because the only route that could set it 422s on it (`routes/admin.py:178-195`). A blank money column reads as "no budget set" — i.e. as a feature that exists and is unused. In its place the table shows `max_active` and `capacity_units`, which are the limits the scheduler really does enforce on every admission, and the column group is titled **"Enforced limits"**.
- **`service_account` and `gcs_prefix` are behind a row expander**, not columns. They are infrastructure identifiers, they are long, and they push the enforced limits off a laptop screen.
- **No activity numbers on this screen.** There is no cross-tenant task read (§3), so a `Tasks` column here could only be fabricated or assembled by impersonating each tenant. Each row links to nothing per-tenant; A1 and A2 always show *the caller's own* tenant.

**The list is capped and unpaginated.** `list_tenants` orders by `tenant_id` and takes the first 200 with no page token and no `limit` parameter on the route (`store.py:186-188`). Past 200 registered tenants the table is silently incomplete, so the header must read *"showing the first 200 tenants, ordered by id"* whenever exactly 200 rows come back, rather than letting the operator believe they are looking at everything.

#### Refresh and cost

One request, one small collection, ≤ 200 document reads. **No auto-refresh** — tenant registration is an operator action measured in weeks, and a manual refresh control is enough. This screen is the cheapest in the section and should not be put on a timer out of habit.

#### Empty and error states

- **403** (a non-admin who reached the URL directly): *"tenant administration requires membership of an admin group"*; the nav entry should not have been visible.
- **Zero rows**: only possible before the first tenant is registered — *"no tenants are registered yet; `scripts/register-tenant.sh` seeds the first one"*. Never the same visual as a failed request.
- **Request failed**: the error banner in §6, with the `code` from the payload; the table keeps the last good rows, greyed, with their age.

#### Phone layout

Cards, one per tenant, 88 pt:

```
Line 1   eng                                group
Line 2   eng@saga.xyz
Line 3   max 20 · units 40 · anthropic openai
```

A disabled tenant's card is struck through on line 1 with the 403 consequence on line 3, replacing the limits. Tap expands to the full field list including `service_account`, `gcs_prefix` and `namespace`. The "first 200" notice pins above the list, exactly like A2's caption.

---

### 6. States every screen must distinguish

The failure this section exists to prevent is a screen where **a broken query and an honest zero look the same**. Every surface above carries one of these six states explicitly — a dot on each stat tile, a banner for the screen:

| State | What triggers it | What the user sees |
|---|---|---|
| `ok` | All pages fetched, `next_page_token` null or the row budget reached | The numbers, plus the covered span |
| `partial` | Some pages in hand, fan-out still running or abandoned after a 429 | *"3 of 10 pages — covering 16 Sep 04:10 → now"*. The chart draws, the span shrinks to what was actually fetched, and **the stat tiles say `partial`** rather than reporting sums over a fraction of the window |
| `empty` | Every page fetched, zero rows, HTTP 200 | *"No tasks for this tenant yet."* For A2: *"No tasks in the window, so no one appears here."* Never the same treatment as `error` |
| `error` | Any non-2xx. Payload is `{code, message, detail?}` (`errors.py:24-28`) | The banner names the condition, not the status number: 401 → *"sign-in expired"*; 403 → *"this tenant is disabled"* (`service.py:97-99`); 429 → *"rate-limited, retrying in Ns"* from `Retry-After` (`main.py:102-103`); 422 → the `message` verbatim; 5xx / network → *"could not reach the control plane"*. The last good data stays on screen, greyed, labelled with its age |
| `blocked` | A value that no deployed code can produce: tokens, spend, agents-started, per-engineer filtering | The reason in one sentence, on the tile, plus the prerequisite. **Never a zero, a dash or an empty chart.** |
| `stale` | Auto-refresh paused (hidden tab, or 15-minute idle stop) | *"paused — updated 8m ago"*, with a tap to resume |

Two specific confusions to design out, both of which have a wrong-but-plausible rendering:

- **`FAILED_PRECONDITION` is not "no results".** Once P3 adds a `submitted_by` filter, a missing index makes Firestore refuse the query outright — the API surfaces a 5xx, and the UI must say *"this query needs an index that is not deployed"*, not *"this engineer has no tasks"*.
- **An empty `usage` object is not zero spend.** It means nothing was recorded (§0). `0` and `~$0.00` are both forbidden renderings for a missing measurement.

---

### 7. Why this section does not use `GET /v1/stats`

`/v1/stats` (`routes/platform.py:13-18` → `service.stats`, `service.py:271-288`) is the obvious-looking source and the wrong one here, for three reasons:

1. **It has no time predicate.** `count_tasks_by_state` (`store.py:524-535`) runs one `count()` per `TaskState` over the whole collection, filtered only by `tenant_id`. It answers "how many tasks are in each state right now", which is a cluster-health question, not an activity-over-time question.
2. **Its cost grows forever.** Firestore bills an aggregation at roughly one read per 1000 index entries matched, so a tenant with 500k lifetime `SUCCEEDED` tasks pays ~500 reads for that single state, on every call, and more every month. For an admin it runs the whole loop a second time platform-wide (`service.py:286-287`). The bounded-row design in §1 has the opposite property: fixed cost, shrinking coverage.
3. **Its live counts belong to the cluster-health section**, which owns state, pools and dispatch pause. Rendering them here would give two screens that disagree by a refresh interval.

Once **P1** lands, the cheap path for a calendar-ranged chart is a `count()` *with* a `created_at` range over the existing `tasks-tenant-created` index — ~1 read per bucket, ~30 reads for a 30-day chart, no new index. That is the aggregation this section should be rebuilt on, and it is the reason to decide the read shape (live scan vs. rollup collection) **before** the UI is written.

---

### Prerequisites this section depends on

Carried here so the prerequisites section picks them up; **P4a is new and is the cheapest high-value item in the list.**

| | What | Track | Size |
|---|---|---|---|
| **P4a** | `lifecycle.py:474` must call `_usage_summary(runner_result["output"].get("structured_output"))` — today it passes the runner envelope and records `{}` on every task. Add a test at the `result.json` level, not the CLI-object level | B | one line + one test |
| **P1** | `since`/`until` on `list_tasks`, mapping onto `tasks-tenant-created`; plus a `count()` variant per bucket | A | small |
| **P3** | Index `tenant_id ASC, submitted_by ASC, created_at DESC`, and a `submitted_by` parameter on `GET /v1/tasks` | C + A | small (index build time is the real cost) |
| **P4** | Somewhere typed to aggregate usage from — per-attempt fields plus a `sum()` target, which is a **frozen-contract change request** against `swarm_common.models.Attempt`, not an edit | contract request | medium |
| **P5** | `roles/monitoring.viewer` for `swarm-api`, the monitoring client dependency, and `GET /v1/activity/series` with a server-side tenant filter | C + A | small |
| **P7** | A projection or a light list shape for `GET /v1/tasks`, so a page is not up to 51 MB of `input` blobs | A | medium |
| **P10** | A real `workflows.state` writer, or an explicit decision that workflow outcome is always derived from member tasks | A | medium |
