## Alerts, errors and the event timeline

This section owns the answer to one question: **when a user or an admin opens SwarmCloud because something feels wrong, what does the screen say, and is it true?**

Everything here was checked against the code. Where the data does not exist, the screen is refused and the prerequisite is named. Where the data exists but is reachable only through a Firestore document that no HTTP route returns, the route is listed as plumbing rather than pretended away.

---

### 0. The rule that outranks every layout decision in this section

This platform's defining bug is an error rendered as an empty success. It is not hypothetical — it is in this repository's own git history, commit `9c639af` ("An HTTP error is not an empty result"): `scripts/lib/common.sh` returned the response body whatever the status was, so `jq '.documents // []'` turned a 401 into `[]`, and `status.sh` — *the screen an operator reads during an incident* — rendered an expired session as an empty, healthy platform. Four findings, one root cause.

A web UI is the same bug with better typography. So the following is binding on every panel specified below, and a panel that does not implement it is not done.

**The panel state machine.** Every panel is in exactly one of six states, and each one looks different:

| State | What renders |
|---|---|
| `loading` | skeleton rows, never zeros, never an empty state |
| `ok` | rows + provenance line |
| `ok-empty` | a sentence that names the query in words, plus the provenance line |
| `ok-truncated` | rows + "showing the first N the API will return; there are more" — see §2, where this is not hypothetical |
| `stale` | the last good payload, dimmed, with a bar: "stale — last good 14:02:11, retrying" |
| `error` | the `code` and `message` from the API envelope, and a Retry button. **No number. Not 0, not "—" on its own.** |

**The error envelope is stable and must be branched on, not parsed.** `apps/swarm-api/swarm_api/errors.py` gives every failure `{code, message, detail?}` with a fixed code: `validation_failed` (422), `unauthenticated` (401), `forbidden` (403), `not_found` (404), `conflict` (409), `rate_limited` (429, `detail.retry_after_seconds`), `upstream_unavailable` (503). Two consequences:

* A `rate_limited` panel renders a countdown from `detail.retry_after_seconds`, not a Retry button that hammers.
* **A 401 is a page-level state, not a panel-level one.** An expired ID token fails all seven fetches at once, and that is precisely the 9c639af shape. The page renders "your session expired — sign in again" across the whole board; it never renders seven independently empty panels.

**The provenance line is mandatory and always visible**: `source · fetched 14:03:07 · 12 rows`. On a panel whose numbers are computed from a page rather than a total, it reads `· of the last 50 returned`, and the number is the row count actually received — never a literal `50` in the template. The API's page size is `default_page_size = 50`, capped by `max_page_size = 200` (`swarm_api/settings.py:81-82`, applied in `deps.py:194-199`), and both are environment-overridable, so a hardcoded "50" in the front end is a lie one `DEFAULT_PAGE_SIZE` change away.

**`ok-empty` must name the query.** Not "No results". "No unreleased leases — nothing is holding capacity right now." An operator has to be able to tell that apart from a blank div, and from an error, at a glance.

**Banned client-side patterns** (each is the `false // true` jq trap from `CLAUDE.md` wearing JavaScript):

* `x || 0` and `x ?? 0` on any value that came from a fetch — a failed fetch becomes a confident zero.
* `catch { return [] }` — this is literally `9c639af` again.
* `pool.enabled ?? true` — a **paused** pool renders as open, which is the one thing that column exists to show. Compare `pool.enabled === false` explicitly.
* `rows.length === 0 ? <Empty/> : <Rows/>` without first branching on request status.
* `rows.length === limit` treated as a complete answer. Every list route here is a page; two of them have no page token at all.
* `BlockedReason[row.reason]` or any enum lookup that drops what it does not recognise. `blocked_by` can legitimately carry `task_missing`, `not_ready` and `cancel_requested`, which are **not** `BlockedReason` members (`swarm_common/admission.py:148,155,157`). Unknown reason strings render verbatim.
* `if (event.detail.source)` as a test for "the reconciler wrote this". Presence is not the test; `detail.source === "reconciler"` is. See §2.

**Partial failure is visible at page level.** The Trouble board below is seven independent fetches (§1.1 through §1.7, the banner included). One failing must not blank the page — and the page header must read "2 of 7 panels failed", not look complete.

**A 403 on an admin panel is not an error.** Admin-only panels render "admin only — you are not in an admin group" (`require_admin`, `swarm_api/auth.py:248-250`), never a red failure. But note that `Forbidden` is also raised for the wrong organisation (`auth.py:196-199`) and carries the same `forbidden` code, so the disambiguation is positional, not textual: **if only the admin panels 403, render "admin only"; if every panel 403s, render the page-level "your account is not permitted here"** with the envelope's `message`.

**One structural invariant the UI can lean on:** `SwarmApiStore.create_tasks` (`apps/swarm-api/swarm_api/store.py:337-366`) writes the task document and its `submitted` event in the same batch, explicitly "so a partially written submission cannot leave a task document with no event trail". Therefore **a task that resolves with zero events is impossible.** An empty timeline is a failed query, and the UI must render it as one.

---

### 1. Screen: **Trouble** (the operational home)

One dense screen, seven panels, each independently fetched and independently truthful. This is what an admin opens at 3am and what a user opens when their task "isn't doing anything".

#### 1.1 Platform state banner

**What the user sees.** A full-width bar, only when something is off. Three possible contents, stacked:

* `DISPATCH PAUSED` — red. "Paused by alice@saga.xyz at 02:14 — 'draining for the quota incident'." Admin-only detail; non-admins see `DISPATCH PAUSED` with no actor.
* `YOUR TASKS: 4 LEASED · 11 RUNNING · 96 READY · 312 PARKED` — the counts, neutral. **The scope word is not decoration.** A non-admin can only be shown `tasks_by_state`, which is their tenant; an admin gets `platform_tasks_by_state` as well and the bar says `PLATFORM:` with a toggle back to `YOURS:`. An admin reading their own tenant's four running tasks as the platform total is a truth bug, not a layout preference.
* `2 of 7 panels failed` — when any panel on this board, this one included, is in `error`.

**Where every value comes from.**

* `dispatch_paused` → `GET /v1/stats` → `SubmissionService.stats` (`apps/swarm-api/swarm_api/service.py:271-288`) → `control/dispatch` document (`store.py:707-714`). `/v1/stats` is open to any authenticated caller; only `platform_tasks_by_state` is admin-gated (`service.py:286-287`).
* actor/reason/when → `GET /v1/admin/dispatch` (`routes/admin.py:70-75`, `Depends(admin_auth)`) → `{dispatch_paused, updated_at, updated_by, reason}`. **Current value only** — `set_dispatch_paused` uses `.set()` (`store.py:716-724`), so the previous pause and its reason are gone.
* counts → `GET /v1/stats` → `tasks_by_state` (tenant) and `platform_tasks_by_state` (admin only).

**What it cannot show yet.** The history of pauses. There is no audit trail of any admin action — see §7.

**Refresh and cost.** `/v1/stats` runs **12 Firestore `count()` aggregations for a tenant and 24 for an admin** (`store.py:524-533`, one per `TaskState`; `TaskState` has exactly 12 members, `states.py:17-29`). `count()` bills one read per 1000 index entries scanned, so on a platform with 500k historical tasks a single admin refresh is meaningfully expensive. **Do not auto-refresh this at 5s.** Refresh at 60s, and add a 60s cache in `swarm-api` (prerequisite P15) — note it is a *per-instance* cache: `swarm-api` runs on Cloud Run with `min_instance_count = 0` and a max above one (`terraform/modules/cloud_run/main.tf:37-41`), so six open tabs are one aggregation set per minute *per serving instance*, not globally one. At 100x task volume the aggregation cost grows with *history*, not with live work — this is the one panel that gets more expensive as the platform gets older, not busier.

**Phone layout.** The bar stays; the counts collapse to the two that matter — `RUNNING n` and `READY n` — with the full breakdown behind a tap. `DISPATCH PAUSED` and the scope word are never collapsed or truncated.

**Empty / loading / broken.** There is no empty state — a successful fetch always yields twelve numbers, because `count_tasks_by_state` iterates the whole enum and writes a key for each. On error the bar renders `PLATFORM STATE UNKNOWN — stats query failed (upstream_unavailable)` in the same red as a pause. This is the single most important place not to render zeros: "0 RUNNING" and "stats failed" are opposite facts.

#### 1.2 Silent workers — *the freshest failure signal on the platform*

**What the user sees.** A table of leases that have not been released, sorted by how long the worker has been quiet.

| Column | Example |
|---|---|
| Task | `task_9f3a…` + `claude-code` |
| Tenant | `eng` (admin only; a user sees only their own) |
| State | `RUNNING` |
| Silent for | `03:14` — **amber ≥ 90s, red ≥ 120s or past `expires_at`** |
| Lease expires | `in 47s` / `18s ago` |
| Gen | `2` |
| Attempt | `att_1c88…` |

Row classification, in words under the table — and the wording has to match what the reconciler actually does:

* *alive* (silent < 90s) — nothing will touch this.
* *silent* (90s ≤ silent) — **this is already the reconciler's trigger.** `detect.py:194` skips a lease only while `silent <= heartbeat_grace_seconds` and the lease is neither expired nor dispatch-overdue, so from 90s on, the next pass raises `STALE_LEASE` or `DEAD_WORKER` (`detect.py:230-250`) — "the reconciler will reclaim this on its next pass, which is up to 5 minutes away."
* *presumed dead* (past `expires_at`) — the lease TTL has run out too. `expires_at` is set to `now + lease_timeout_seconds` at admission (`swarm_common/admission.py:199`) and re-extended by the same 120s on every heartbeat (`agent_worker/control.py:509-516`, `__main__.py:68`), so in practice this lands at ~120s of silence. It is a second, independent signal, not a later stage of the first one.

**Where every value comes from.** Firestore `leases/{lease_id}`, the query the reconciler itself runs: `where released_at == null` (`apps/reconciler/reconciler/store.py:87-89`). Fields: `task_id`, `tenant_id`, `attempt_id`, `generation`, `state`, `created_at`, `heartbeat_at`, `expires_at`, `dispatch_deadline` (`apps/common/swarm_common/models.py:115-138`). `heartbeat_at` is written by the worker every 30s (`agent_worker/control.py:509-516`, interval `agent_worker/config.py:65`).

Computation: `silent_seconds = now − heartbeat_at`, falling back to `created_at` when `heartbeat_at` is null (a lease that has never beaten). Written out rather than as `heartbeat_at ?? created_at` on purpose: the fallback is a real timestamp, never `0` and never `now`.

**This requires a new route** — there is no attempts or leases endpoint anywhere in `swarm-api` (`routes/tasks.py` exposes create, batch, list, get, cancel, events, artifacts and nothing else) — prerequisite P5. `scripts/status.sh` works around this by hitting the Firestore REST API with gcloud credentials (`fs_query leases`, `status.sh:84`); a browser must not copy that, because it would need project-wide Firestore read and would cross the tenant boundary that invariant 9 keeps intact everywhere else.

**The thresholds must come from the API, not from JavaScript.** 90s and 120s are `ReconcilerConfig.heartbeat_grace_seconds` and `lease_timeout_seconds` (`apps/reconciler/reconciler/config.py:43-44`). A `const GRACE = 90` in the front end is exactly the restatement drift that `scripts/lib/check-contract-parity.sh` exists to catch in shell and jq. `GET /v1/leases` returns `{"leases": [...], "thresholds": {"heartbeat_grace_seconds": 90, "lease_timeout_seconds": 120}, "evaluated_at": ...}` and the UI colours from that payload. **P5 carries one extra obligation because of this:** `heartbeat_grace_seconds` is not a constant — the reconciler reads `HEARTBEAT_GRACE_SECONDS` and otherwise derives `max(90, heartbeat_interval_seconds * 3)` (`config.py:82-85`). `swarm-api` must resolve it the same way from the same environment, or the API becomes a third restatement of the number instead of the single source of it.

**What it cannot show yet.** *Why* the worker went quiet, and whether an execution is still running on the backend. Both require a backend `list_executions()` call, which only the reconciler makes — and its conclusions are discarded (§6). This panel therefore tells you *that* a slot is held by something silent, up to 5 minutes before the reconciler notices, and nothing about the cause. Say so on the screen: "fresher than the reconciler; blind to the backend."

**Refresh and cost.** One query, **bounded by live concurrency, not by history** — tens of documents. This is the cheapest live signal on the platform. Refresh at **10s**. At 100x it is still bounded by concurrency; the failure mode is leaked leases accumulating unreleased, which makes the query grow and is itself the thing worth seeing.

**Phone layout.** Three columns survive: Task (runner profile as a chip under the id), Silent-for (large, coloured), and a chevron. Tenant, gen, attempt id, lease id and the two deadlines move behind the tap. The sort stays "longest silent first" so the worst row is the top row on a 390pt screen without scrolling.

**Empty / loading / broken.** `ok-empty` = "No unreleased leases — nothing is holding capacity right now." That sentence is only legitimate after a 200. On error: "Lease query failed (`upstream_unavailable`)" and **no green tick anywhere on the card**.

#### 1.3 Admitted but never dispatched

**What the user sees.** Leases in state `LEASED` whose `dispatch_deadline` has passed: a slot reserved for a container that never started. Columns: Task, Tenant, Overdue by, `last_error`.

**Where every value comes from.** `leases` where `state == "LEASED"` and `dispatch_deadline < now` — index `leases-state-dispatch-deadline` already exists (`terraform/modules/firestore/indexes.tf:133-140`). **This is the same missing endpoint as §1.2 — prerequisite P5** — and it is the reason `GET /v1/leases` should accept a `state` filter rather than being two routes. `last_error` comes from `tasks/{id}.last_error`, which on a dispatch failure is a **stable code plus the attempt id**, e.g. `BACKEND_REJECTED (attempt att_1c88…)` — written by `return_to_ready_after_failed_dispatch` (`apps/scheduler/scheduler/store.py:250-290`, the write itself at `:286`, truncated to 1000 chars) and deliberately *not* the upstream message, because a Cloud Run or Kubernetes error echoes the tenant service account email, the job name and the secret names in the manifest (the function's own docstring says so at `:270-275`).

Joining the two: the lease rows carry `task_id`, and `last_error` needs the task document. Either P5 denormalises `last_error` onto the lease response or the panel issues one `GET /v1/tasks?state=LEASED` alongside. **Prefer the first**; the second is an N+1 waiting to be written as a loop.

**What it cannot show.** The upstream error text. It is only in the scheduler's Cloud Logging line. The UI must render the correlation id as a **copyable token** with a one-line explanation — "give this id to an operator; the full message is in the scheduler logs" — rather than a dead-end code. For admins, link it into the log viewer (§5) filtered on that attempt id.

**Refresh and cost.** One indexed query, bounded by concurrency. 30s.

**Phone layout.** Two columns: Task, Overdue by. `last_error` is a full-width second line in monospace, wrapped, never ellipsised — a truncated stable code is a useless stable code.

**Empty / loading / broken.** `ok-empty` = "Nothing admitted is overdue to start — every lease taken has reached its backend." On error: the lease query's own error code, and the §1.2 card must not be read as covering this one; they share a route but fail independently on screen only if fetched independently. If P5 serves both from one request, they share one error state and say so: "lease query failed — both lease panels are blind."

#### 1.4 Failures that need a human

**What the user sees.** The most recent failed tasks, with the ones that have burned every attempt called out at the top: **"3 of the last 50 failed tasks have exhausted their attempts."**

| Column | Source |
|---|---|
| Task + runner profile | `tasks/{id}` |
| Tenant | admin view only |
| Failed at | `completed_at` |
| Attempts | `attempt_count` / `max_attempts` — `3/3` in red |
| Error | `last_error` |

**Where every value comes from.** `GET /v1/tasks?state=FAILED` — tenant-scoped, served by index `tasks-tenant-state-created` (`indexes.tf:61-69`), returned through `task_to_api` (`codec.py:99-132`; `last_error` at `:129`, `attempt_count`/`max_attempts` at `:115-116`). The "exhausted" flag is computed client-side as `attempt_count >= max_attempts`, because **Firestore cannot compare two fields in a query**. The route also returns `next_page_token`, so "older failures" is a real button, not a redesign.

**This screen is NOT called a dead-letter queue, and there is no `DEAD_LETTERED` tab.** `TaskState.DEAD_LETTERED` exists in the frozen contract (`states.py:29`) and **is never written by any code path** — an exhaustive grep over `apps/` finds it only in the enum, in an event mapping the worker would use if `finish()` were ever called with it (`agent_worker/control.py:633`), and in two read-side guards (`scheduler/loop.py:56`, `swarm_api/store.py:429`). The retry-exhaustion path actually implemented goes to `FAILED`: `apps/reconciler/reconciler/store.py:213-216` promotes to `FAILED` when `attempt_count >= max_attempts`, and every worker failure path passes `TaskState.FAILED` (`agent_worker/lifecycle.py:181, 185, 482, 487, 493, 495, 517, 524`). A `DEAD_LETTERED` tab would be permanently empty and an operator could not tell that from "nothing is wrong" — the exact bug §0 exists to prevent. Two consequences fall out of this and are surfaced in §4: the `swarm/dead-lettered` log metric (`terraform/modules/monitoring/metrics.tf:76-81`) matches nothing, and the `swarm-<env>-tasks-dead-lettered` alert (`alerts.tf:328-374`) **can never fire** — it is a `COMPARISON_GT 0` threshold on a metric no line ever increments, so its silence is indistinguishable from health.

**What it cannot show yet.**

* **Platform-wide.** `GET /v1/tasks` is tenant-scoped at the store, and every task index leads with `tenant_id`. An admin query `where state == "FAILED" order by created_at DESC` has **no index**; Firestore answers `FAILED_PRECONDITION`, not a slow page (the index file says so itself at `indexes.tf:52-58`). Needs one new index + an admin route (P6).
* **`exit_code`, in this list.** The number itself is *not* unreachable: the worker's terminal event carries it — `finish()` emits `{"exit_code", "error"}` on the `failed` event (`agent_worker/control.py:635`) and `GET /v1/tasks/{id}/events` returns `detail` verbatim (`routes/tasks.py:24-34`). So a task *detail* page can show the exit code today. What does not exist is any way to get it **in a list**, or to get the rest of the per-attempt record — `peak_rss_bytes`, `oom_near_miss`, `peak_disk_bytes`, `execution_name`, and exit codes of *earlier* attempts — because `attempts/{attempt_id}` (`agent_worker/control.py:497-506`) is reachable by no API endpoint. That is P4. **Do not put an `exit_code` column in this table before P4 lands**: filling it would mean one events request per row.

**Honesty label, mandatory.** Because "exhausted" is a client-side filter over a page, the count is always phrased "**of the last N**", N being the rows actually returned. A bare "3 tasks need attention" would be a lie the moment there is an N+1-th.

**Refresh and cost.** One indexed query of ≤50 documents. 60s. At 100x it is unchanged — it is a page, not a scan. The platform-wide variant is the same shape once its index exists.

**Phone layout.** One card per failure: task id + runner chip on line 1, `3/3 attempts` + relative time on line 2, `last_error` in monospace on line 3 wrapped to three lines with "more" if longer. No table.

**Empty / loading / broken.** `ok-empty` = "No failed tasks in this tenant's last N — nothing has failed recently." Never "All good": the query is a page over one tenant and one state, and the sentence has to say so. On error, the exhausted-count headline is **suppressed entirely** rather than rendered as "0 of 0" — a zero there reads as reassurance.

#### 1.5 Parked work, by reason — *rendered in grey, never red*

**What the user sees.** A horizontal bar split by `ParkReason`, with counts, above a short list of the two reasons that actually need a person. Header text, fixed: **"Parked work costs nothing. A tall bar here is the platform declining to burn compute on a wait."**

Needs-a-human reasons, pulled out: `CREDENTIAL_MISSING` (the tenant has not registered a key for the provider this runner needs) and `BUDGET_EXHAUSTED`. The rest — `PROVIDER_QUOTA_EXHAUSTED`, `PROVIDER_COOLDOWN`, `PROVIDER_OUTAGE`, `SCHEDULED_RETRY`, `DEPENDENCY_INCOMPLETE`, `MANUAL_PAUSE` — are the platform working as designed (CONTRACT.md invariant 1).

**Where every value comes from.** `GET /v1/tasks?state=PARKED` → `park_reason` (`codec.py:119`) and `next_eligible_at` (`:118`). Aggregated over the returned page. The vocabulary is `ParkReason` (`swarm_common/states.py:125-137`) — render the enum values, do not invent friendlier labels that then drift, and render an unrecognised string verbatim rather than dropping it.

**Refresh and cost.** One page query, 60s. Aggregate is "of the last N" and labelled as such.

**Phone layout.** The stacked bar survives (it is one row of colour); the legend becomes a two-column list below it. Tapping a segment filters the task list.

**Empty / loading / broken.** `ok-empty` = "Nothing parked — no task is waiting on a quota window, a dependency or a credential." The bar renders as a single grey rule, not as a zero-height div that looks like a rendering failure. On error the bar is **not drawn at all** — an empty bar and a failed fetch are indistinguishable once the segments are gone — and the card shows the error code plus Retry.

#### 1.6 Why the queue is not moving

**What the user sees.** For `READY` tasks that were refused on the last scheduler pass, a count per reason, each row carrying the pool that caused it and the capacity numbers **as they were at the moment of refusal**:

`GLOBAL_CONCURRENCY_LIMIT — 23 tasks · pool "global" 40/40 at refusal`
`TENANT_LIMIT — 8 tasks · pool "tenant:eng" 20/20 at refusal`
`MANUAL_PAUSE — 4 tasks · pool "runner:browser" 0/6 at refusal · **DRAINED now**`

That third line is the case the panel exists for, and its reason code is the one thing a designer will get wrong from the outside: **a drained pool is recorded as `MANUAL_PAUSE`, not as `RUNNER_LIMIT`.** `evaluate_capacity` tests `if not pool.enabled` *before* it tests capacity (`swarm_common/admission.py:105-110`), so a paused pool with `active < limit` never reaches the limit branch. A panel that groups by `RUNNER_LIMIT` to find drained runner pools finds nothing.

**Where every value comes from.** `tasks/{id}.blocked_by` — written by `record_blockers` (`apps/scheduler/scheduler/store.py:192-201`), returned by the API (`codec.py:120`). **Each entry already carries its own numbers**: `{"pool": name, "reason": <BlockedReason>, "limit": pool.effective_limit, "active": pool.active}` (`admission.py:105-115`). Render those, not a live pool reading — the blocker is a fact about the pass that refused this task, and pairing it with a `GET /v1/capacity` number fetched twenty seconds later produces rows like "TENANT_LIMIT — 8 tasks · 14/20 active", which reads as a contradiction and is really two timestamps in one sentence.

`GET /v1/capacity` → `pool_to_api` (`codec.py:215-226`: `hard_limit`, `adaptive_target`, `quota_derived_limit`, `effective_limit`, `active`, `available`, `enabled`) is fetched for exactly one purpose: the **live** `enabled` flag, labelled `now` on screen. Note `effective_limit` is `min(hard_limit, adaptive_target, quota_derived_limit)` (`models.py:66-72`), so it, never `hard_limit`, is the denominator anywhere a ratio is drawn.

**Reasons that are not `BlockedReason`.** `record_blockers` stores whatever `AdmissionDenied` carried, and three of those are bare strings from the pre-flight checks: `task_missing`, `not_ready` (with `state`), `cancel_requested` (`admission.py:148,155,157`). They are rare and they mean something quite different from a capacity refusal — render them in their own group, "refused before capacity was evaluated", rather than letting an enum lookup silently swallow them.

**The `enabled` trap, restated for JavaScript.** `pool.enabled ?? true` renders a drained pool as open. Compare `pool.enabled === false`. This is the same defect the house rules record for jq (`.enabled // true`), in the one panel where it would be most misleading.

**What it cannot show.** A true total. `blocked_by` is an array of maps; Firestore cannot equality-filter on one field of an element, so there is no `count()` per reason. The counts are over the returned page and must be labelled "of the N most recently created READY tasks". The scheduler *does* compute a real aggregate — `DrainReport.blockers` (`apps/scheduler/scheduler/loop.py:78`) — and it is served by `GET /last-run` on `swarm-scheduler` (`main.py:249-252`), but that is **process memory** on a service with `min_instance_count = 0` (`terraform/modules/cloud_run/main.tf:37-41`): after an idle period the instance is gone and the answer is `null`, and under load two instances give two different answers. **Do not put `/last-run` in the UI.** It is a debugging aid, not an API.

**Refresh and cost.** One page query + one pool list. 30s. `list_pools` is bounded by pool count (a handful of shared pools plus one per tenant and per provider-tenant pair — `pool_names_for`, `models.py:94-107`) — at 100x tenants this becomes hundreds of documents per refresh and should move to a server-side 30s cache.

**Phone layout.** One row per reason: reason name, count, and a compact `40/40` capacity chip that turns into the word `DRAINED` when the live `enabled === false`. Pool internals (`hard_limit`, `adaptive_target`, `quota_derived_limit`, and which of the three is currently binding) behind the tap.

**Empty / loading / broken.** `ok-empty` = "No READY task was refused on the last pass — the queue is moving." Two failure modes that must look different: the `/v1/tasks?state=READY` page failing (no rows at all — show the error, no bars) and `/v1/capacity` failing while the task page succeeded (**rows render with their at-refusal numbers, and the `DRAINED now` chips are replaced by a grey "live pool state unavailable"** — never by an absent chip, which reads as "not drained").

#### 1.7 Provider health

**What the user sees.** One row per provider the platform has runner profiles for — today `anthropic` (`claude-code`, `browser`) and `openai` (`codex`), derived from `RUNNER_PROFILES` rather than hardcoded (`swarm_common/profiles.py:118-174`). State chip, "credential registered: yes/no", cooldown countdown, last 429, and the 429-vs-success ratio.

**The state chip has six values, not five**: `AVAILABLE`, `THROTTLED`, `EXHAUSTED`, `COOLDOWN`, `DISABLED`, `UNKNOWN` (`swarm_common/models.py:259-265`). `THROTTLED` is the one most easily missed and it is the common case during a squeeze — a chip switch that falls through to "unknown" for it would mislabel exactly the condition the panel is for.

**Where every value comes from.** `GET /v1/providers` → `SubmissionService.providers` (`service.py:334-350`): per provider, `credential_registered` (from `tenants/{id}.credentials` — provider **names** only, never key material), `runner_profiles`, and `quota` → `quota_to_api` (`codec.py:255-258`) over `quota/{provider}:{tenant_id}`: `state`, `cooldown_until`, `last_429_at`, `retry_after_seconds`, `requests_remaining`, `tokens_remaining`, `reset_at`, `success_count`, `rate_limit_count`, `effective_limit`.

**`effective_limit: 0` is a fact, not a missing value.** `QuotaState.effective_limit` deliberately returns 0 when the state is `EXHAUSTED`, `DISABLED` or `COOLDOWN` (`models.py:289-300`). It is the one zero on this board that must be rendered as a zero — with the state chip next to it doing the explaining.

**Tenancy note.** Quota is per provider **per tenant**, on purpose: tenants bring their own keys, so one tenant's 429 must not render as a platform outage. The row header says "your quota", not "provider status".

**What it cannot show.** Whether the provider is down globally. Nothing measures that. `ProviderState.UNKNOWN` means "no worker has reported on this provider recently", which is not the same as healthy, and the chip must use a distinct grey for it.

**Refresh and cost.** One small query per refresh. 60s.

**Phone layout.** One card per provider; state chip and countdown on the first line; counters behind the tap.

**Empty / loading / broken.** There is a per-provider empty state and it is not the panel's: `quota` is `null` when no quota document exists for that tenant yet (`service.py:347`). That row renders `UNKNOWN` with "no quota reported for your tenant yet", not blanks and not zeros. The panel's own `ok-empty` — no providers at all — is unreachable while any runner profile declares a provider; if it ever renders, it is a bug and should say "no providers configured", which an operator can act on.

> **Deeper Claude subscription quota** — per-session and per-week windows, refresh TTL, the claudeswitch-style view — is the accounts section's screen, not this one. This panel shows only what `quota/{provider}:{tenant_id}` already holds.

---

### 2. Screen: **Task timeline** (one task)

The single richest operator-facing data source that actually exists. Every lifecycle transition is appended by all four writers: scheduler (`apps/scheduler/scheduler/store.py:325-360`), worker (`agent_worker/control.py:345-375`), API (`swarm_api/store.py:459-489`), reconciler (`apps/reconciler/reconciler/store.py:243-279`).

**What the user sees.**

*Header:* task id (copyable), state chip, runner profile, resource class, provider, tenant, `submitted_by`, created / started / completed, elapsed, `attempt_count`/`max_attempts`, timeout, workflow link and `depends_on` chips, `cancel_requested` flag, and `latest_checkpoint` — all from `task_to_api` (`codec.py:99-132`). `latest_checkpoint` earns its place next to `attempt_count`: it is the difference between "a retry restarts this" and "a retry resumes this".

*Error banner,* when `last_error` is set — full text, monospace, never one-line-truncated. Three flavours, distinguished by prefix because each has a different meaning:
* `reconciled: …` — written by the reconciler (prefix at `reconciler/repair.py:342`, truncated to 2000 chars where it is stored, `reconciler/store.py:233`).
* `<STABLE_CODE> (attempt att_…)` — a dispatch failure (`scheduler/store.py:286`, 1000 chars). Show the "full message is in the operator logs" note.
* anything else — the worker's own error at finish (`agent_worker/control.py:610-636`, the field write at `:623`), or, for a task cancelled before it held capacity, the cancellation reason (`scheduler/store.py:319`).

*Output,* when `result_summary.logs` is present: `gs://…/logs/stdout.log` and `stderr.log` as copyable paths with a "copy gsutil command" button. **No download link** — `list_artifacts` never mints a signed URL, by design: the caller reads GCS with their own credentials, which keeps the tenant boundary in one place (`swarm_api/store.py:501-510`). **While the task is running this block is not empty, it is explicit:** the agent's stdout and stderr are captured to the tmpfs workspace and uploaded *once, at the end of the attempt* (`agent_worker/lifecycle.py:~896-905`), so the block reads "no output yet — the agent's output is uploaded when the attempt ends". A blank panel here during a 40-minute run is the §0 bug in miniature.

*Timeline,* newest last, one row per event: relative time, type chip, attempt short-id, generation, and the `detail` fields that matter for that type:

| Event type | Rendered from `detail` | Written by |
|---|---|---|
| `failed` / `succeeded` / `cancelled` (terminal) | `exit_code`, `error` | worker, `control.py:635` |
| `parked` | `reason` (ParkReason), `next_eligible_at` | worker `control.py:613-616`; scheduler `store.py:214` (**reason only** — no `next_eligible_at`) |
| `quota_exhausted` | `provider`, `provider_state`, `wait_seconds`, `next_eligible_at`, `park_phase` | worker, `lifecycle.py:756-762` (detail built at `quota.py:164-176`) |
| `generation_fenced` | `invalidated_generation` → `new_generation`, `finding`, `reason` | reconciler, `repair.py:281-289` |
| `lease_released` | `reason`, and `error_code` + `correlation_id` **only from the scheduler** | worker `control.py:578` (`{reason}` alone); scheduler `store.py:293-301`; reconciler `repair.py:329-333` (`reason` = a `FindingKind`, free text under `detail`) |
| `dispatched` | `execution_name`, `backend` | scheduler, `store.py:243-248` |
| `checkpoint_completed` | `seq`, `size_bytes`, `uri`, `checkpoint_id` | worker, `control.py:478-482` |
| `heartbeat` | `elapsed_seconds`, `peak_rss_bytes`, `checkpoints` | worker, `lifecycle.py:793-799` |
| `cancel_requested` (not terminal) | `requested_by`, `from_state`, `phase` (`cancel_requested`) | API, `Store.request_cancel`, for a task holding capacity: only the flag is set. Before 2026-09-24 this was written as `cancelled` with `phase: cancel_requested`; the API serves those stored rows as `cancel_requested` (contract request 17) |
| `cancelled` (API) | `requested_by`, `from_state`, `phase` (`cancelled`) | API, `Store.request_cancel`, for a task holding nothing: the API made the transition itself |
| `submitted` | `runner_profile`, `resource_class`, `state`, `workflow_id` | API, `store.py:351-363` |

`lease_released.reason` is the one field whose vocabulary spans three writers: `dispatch_failed` (scheduler), `parked:<ParkReason>` and `terminal:<TaskState>` (worker), or a bare `FindingKind` such as `stale_lease` (reconciler). Render it as a literal; do not map it through any one enum.

*Source attribution:* only the reconciler labels itself — it writes `detail.source = "reconciler"` on every event (`reconciler/store.py:263`). Rows carrying that **exact value** get a "reconciler" badge. **The test is the value, not the key:** the worker's `quota_exhausted` events also carry a `detail.source`, describing where the quota signal came from (`quota.py:172`), and a presence check badges them as reconciler work. **The UI must not guess a source for the others**; scheduler-, worker- and API-written events are indistinguishable by payload alone, and the honest label for those rows is the event type itself.

**The timeline is capped, and today it is capped at the wrong end.** `GET /v1/tasks/{task_id}/events` (`routes/tasks.py:123-131`) calls `list_events` (`store.py:490-499`), which orders `at` **ASCENDING** and applies `limit` = `paged_limit(...)` — `default_page_size` 50, `max_page_size` 200 — and the route returns **no page token**. So a task with more than 200 events hands back the *oldest* 200 and the newest are unreachable. This is not a corner case: the worker heartbeats every 30s and checkpoints every 120s (`agent_worker/config.py:65-66`), so a two-hour attempt writes ~240 heartbeat events before anything else, and the end of the timeline — the part an operator opened the page for — is exactly the part the API drops.

Until that is fixed, the screen must: request `limit=200`; and when 200 rows come back, render the `ok-truncated` state with the sentence "showing the oldest 200 events of this task — newer events are not reachable through this endpoint yet", plus a link to the log viewer (§5) filtered on the attempt id, which is not capped this way. **Prerequisite P16 (new): add `page_token`, or an `order=desc` parameter, to `GET /v1/tasks/{id}/events`.** It is a small Track A change over an existing subcollection query and it is the difference between this screen being the platform's best answer and its most confidently wrong one.

**History is unbounded, which is good news the UI should not rely on quietly.** `terraform/modules/firestore/indexes.tf:249-264` configures a Firestore TTL on `events` using field `var.event_ttl_field`, default `"expires_at"` (`modules/firestore/variables.tf:67-71`) — and **no event writer writes that field** (checked all four; `TaskEvent` has no such attribute, `models.py:219-228`). The TTL policy matches zero documents, so events never expire. The timeline can therefore promise full history; it should not promise it *because of* the TTL, and the mismatch belongs in §7 as a reported finding, not as a design assumption.

**Refresh and cost.** One indexed read per event returned; a short task has roughly 8–20 events, a long one hundreds. Poll at **10s while the task is in a non-terminal state, then stop** — a terminal task's timeline cannot change, and continuing to poll it is the most common way a task detail page becomes the platform's biggest Firestore reader. At 100x it is unchanged: this is a single-task query, and its cost is set by the task's own event count, not the platform's.

**Phone layout.** The header collapses to two lines: task id + state chip, then runner chip + elapsed; everything else (resource class, provider, tenant, submitter, timeout, attempts, workflow, depends_on) moves behind "details". The error banner never collapses and never truncates — it is the reason the page was opened. The timeline becomes a single left-rail column: time on the left at 11pt, type chip and the one or two `detail` fields that matter for that type stacked to its right, the rest of `detail` behind a tap on the row. Newest last, and the view opens scrolled to the bottom, so the phone shows the end of the story without a swipe.

**Empty / loading / broken.** There is **no legitimate empty state on this screen**, and that is the one place where this section's opening rule pays for itself: `create_tasks` writes the task and its `submitted` event in the same batch (`swarm_api/store.py:337-366`), so a task that exists has at least one event. `events: []` after a 200 therefore renders "timeline unavailable — this task must have at least a `submitted` event; the query returned none", styled as an error, never as a blank column. A 404 on the task means the id is wrong or belongs to another tenant, and says both. A 403 here is the tenant boundary, not an admin gate: "this task belongs to another tenant."
