# SwarmCloud web UI — feature specification

Eight sections, one per area. Every screen names the Firestore collection, API
route, GCS path or GCP API behind each value it shows, and every screen that
**cannot** be built today says so and names the prerequisite.

## How this was produced, and how much to trust it

Three passes, all against the real code:

1. **Feasibility.** One agent per area established what data exists. Every
   "this exists" claim was then handed to a separate agent told to REFUTE it and
   to default to refuted when uncertain. Several claims did not survive — a
   model field that no live code path ever writes is the recurring shape, and a
   screen built on one renders permanent zeroes.
2. **Drafting.** One agent per area wrote its section against those verified
   findings, with the refuted claims supplied as a list of traps.
3. **Critique.** A second agent per section hunted for screens presented as
   available that the findings say are not. **141 issues were corrected**, so
   the drafts were meaningfully wrong before review — read this as evidence the
   review mattered, not that the result is perfect.

It is a specification, not a promise. Where it says a number exists, a path was
checked. Where it says something is impossible today, that was checked too.

## The shape of the answer

| | |
|---|---|
| screens specified | **80** |
| buildable with today's data | **26** |
| blocked on platform work | **54** |
| prerequisites identified | **105** |
| of those, needing a frozen-contract change | **10** |

**Roughly two thirds of the screens need platform work before they need UI
work.** That is the single most useful thing here. Four of the seven original
asks — live logs, browser visuals, account quotas, token spend — are platform
changes wearing a UI costume, and no amount of front-end effort reaches them.

## Read in this order

1. [Front door, auth, and the API surface](01-reachability.md) — **P0, gates
   everything.** No browser can reach swarm-api today. Not because the ingress
   setting forbids it (it does not — `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` is
   exactly what an external ALB needs), but because **no load balancer was ever
   built**.
2. [Cluster state, health and capacity](02-cluster-state.md)
3. [Agents, workflows, table and graph](03-agents-and-workflows.md) — the
   workflow graph is a real DAG and is servable today.
4. [Live agent output and browser visuals](04-live-logs.md) — the hardest
   section; read its redaction argument before promising anyone a live stream.
5. [Alerts, errors and the event timeline](05-alerts-errors.md) — reconciler
   findings are computed per pass and **discarded**.
6. [Claude account management](06-accounts.md) — the quota detail asked for
   reads fields nothing writes.
7. [Per-user and per-tenant activity](07-user-activity.md)
8. [Operator and admin affordances](08-operator-gaps.md) — grounded in the
   failures recorded in `docs/audits/`.

## Also in this directory

The eight numbered files above are the feature specification — what could be
built, and what blocks each screen. Four more files answer different questions
and are read on their own:

* [`agent-inspector-artifacts.md`](agent-inspector-artifacts.md) — the agent
  drawer's Artifacts pane (inputs, the answer, every file, logs and the
  transcript, live) and the CPU rows in Details (#184): where each figure comes
  from, why every byte goes through the API, and what the pane cannot show.

* [`redesign.md`](redesign.md) — the information architecture: what the sections
  are, why there are six of them, and the routing. `App.tsx` names this file as
  the place to argue with the nav, so it is the one that has to be current.
* [`design-system.md`](design-system.md) — the visual system: tokens,
  primitives, the absence vocabulary, and the record of each screen pass.
* [`ux-plan.md`](ux-plan.md) — the 2026-09-24 measured sweep of the running
  console: what is wrong in order of what it costs a user, what is already
  fixed with before/after figures, the sequence, and an explicit list of what
  was not checked.

`redesign-v2.md`, `ui-audit-and-build-prompt.md` and `prose-migration-table.md`
are working documents from earlier passes. Where one of them contradicts
`redesign.md` or `design-system.md`, those two win.

## A note on old route names in the evidence

**The nav was renamed on 2026-09-24 and the evidence files were not.** Two
section ids changed — `agents` → `work` and `pools` → `capacity` — along with
their labels (Agents → Work, Pools → Capacity) and one tab label (Capacity
holders → Holders). The argument is in
[`redesign.md`](redesign.md#no-section-may-be-named-after-one-of-its-own-tabs):
both sections were named after their own first tab, so the rail drew
`Agents > Agents` and `Pools > Pools`.

Everything under `evidence/` and under `docs/audits/` keeps the spelling it was
captured with, deliberately. Those files are dated records of what was true on
a date, and a measurement edited to match today is a measurement that has been
falsified — `audit-agents-running.json` recorded a screen reached at
`#agents/running`, and at the moment it was recorded that was the address. The
screenshots are the same: `02-agents-running.png`, `05-pools-pools.png` and
`07-pools-holders.png` are named for the routes that produced them.

To translate a filename or a route in a report into today's address:

| in the evidence and the audits | today |
|---|---|
| `#agents`, `#agents/running`, `#agents/workflows` | `#work`, `#work/running`, `#work/workflows` |
| `#agents/task/<id>` | `#work/task/<id>` |
| `#pools`, `#pools/pools`, `#pools/holders`, `#pools/accounts` | `#capacity`, `#capacity/pools`, `#capacity/holders`, `#capacity/accounts` |
| `#activity/timeline`, `#history/timeline` | `#work/timeline` |
| `#counts`, `#history/counts`, `#activity/counts` | `#admin/counts` |
| `#runtimes/catalogue` | `#capacity/catalogue` |
| `#settings/limits`, `#settings/accounts` | `#admin/limits`, `#capacity/accounts` |
| the "Agents" section | the **Work** section |
| the "Pools" section | the **Capacity** section |
| the "Runtimes" section | the **Runtimes** tab of **Capacity** |
| the "History" section | split: **Timeline** under Work, **Platform counts** under Admin |
| the "Capacity holders" tab | the **Holders** tab |
| the "Runner profiles" tab | the **Profile headroom** tab |

The last four rows are the 2026-09-24 collapse from six sections to three
(Work, Capacity, Admin, with Overview as the landing screen). No screen was
merged or removed by it — all fifteen keep their own route, their own read and
their own failure state; what went away is two rail entries. The argument is in
[redesign.md](redesign.md#changed-2026-09-24--six-sections-to-three), and the
measurement that prompted it is [ux-plan.md](ux-plan.md) §1.4.

Every old hash in that left column still resolves in the app, with its tail
intact, so a link pasted out of a two-day-old report still lands on the pane it
named. A *bare* retired section hash — `#runtimes`, `#history`, `#agents` —
lands on the new section's first pane rather than on any particular one, which
is what an unrecognised tail has always done; the app has never written a bare
section hash into the address bar (`canonical` always emits
`<section>/<tab>`), so those exist only where someone typed one by hand. What the app will not do is *write* one: an alias is for a hash someone
else saved, never a second name this product may emit, and
`nav.links.test.tsx` fails the build over any internal href that uses one.

## Buildable today


**[cluster-state](02-cluster-state.md)**
- Screen A sub-component — per-runner-profile headroom rows
- Screen A sub-component — data-source freshness strip
- Screen B — Capacity board (full pool table, PAUSED column, Set-by column, Over-ceiling column, admin drain/limit controls)

**[agents-and-workflows](03-agents-and-workflows.md)**
- Agents table (Live / Waiting / Recent tabs, 9 columns)
- Agent detail — Why / Timeline / Output / Placement / Input
- Agent detail — artifacts and logs
- Workflows list (cards with step-state strip and computed rollup)
- Workflow DAG view (layered graph, control + artifact edges, live node states)
- Workflow DAG — phone layered-list rendering

**[live-logs](04-live-logs.md)**
- Attempt Timeline
- Run Output (result, artifacts, log URIs)
- Liveness badge (shared component)

**[alerts-errors](05-alerts-errors.md)**
- Trouble board — platform state banner (dispatch pause + task counts)
- Trouble board — failures that need a human, tenant-scoped
- Trouble board — parked work by ParkReason
- Trouble board — why the queue is not moving (blocked_by joined to pools)
- Trouble board — provider health and quota (per provider, per tenant)
- Task timeline (per-task event stream, error banner, GCS log paths)

**[user-activity](07-user-activity.md)**
- A1 — Activity (tenant timeline: stacked outcome chart read out by its legend, three figures on the metric strip, runner-profile split)
- A1 — Token spend, summed from task results and marked partial (the coverage bar was replaced by its foot, TS-12)
- A2 — People (per-engineer table + engineer drawer, derived from A1's rows at zero extra reads)
- A4 — Tenants (admin roster: tenant_id, kind, principal, enabled, max_active, capacity_units, credential chips)
- A4 — Platform-wide state counts (admin, behind an explicit button, 5-minute cache)
- Window & provenance bar (covered span, timezone, row budget, bucket size, coverage ratio, refresh age)

**[operator-gaps](08-operator-gaps.md)**
- O1 — Control Room

**[reachability](01-reachability.md)**
- F3 — Fetch contract / provenance component (the empty-vs-broken rule)

## Blocked, with what blocks them

**Re-checked 2026-10-02 against main.** The tables after this one are the
specification as it was written, kept so the reasoning behind each blocker
survives. This table is what is true now. Every row of those tables is in it,
in the same order, and each verdict was read from the code it cites. **Still
blocked** means the blocker named below still holds as written. A route path
is under `/v1`.

| screen | 2026-10-02 | evidence |
|---|---|---|
| Screen A — Operations Home | partly shipped: `GET /v1/admin/leases` and `GET /v1/admin/quota` exist; the Overview reads leases, and only the quota screen reads quota | `apps/swarm-api/swarm_api/routes/admin.py:401`, `apps/swarm-api/swarm_api/routes/admin.py:524`, `apps/swarm-ui/src/Overview.tsx:151`, `apps/swarm-ui/src/QuotaDetail.tsx:53` |
| Screen C — Live capacity holders | shipped: `GET /v1/admin/leases`, rendered by Capacity ▸ Holders | `apps/swarm-api/swarm_api/routes/admin.py:401`, `apps/swarm-ui/src/Holders.tsx:131` |
| Screen D — Backend reality | still blocked: only the reconciler reads Cloud Run; swarm-api serves no backend inventory | `apps/reconciler/reconciler/backends.py:506` |
| Screen E — GKE Autopilot nodes | still blocked, and still recommended not to build | nothing reads nodes |
| Screen F — Throughput over time | partly shipped: a Firestore-backed throughput lane over `GET /v1/outcomes`; no Monitoring series and no utilisation history | `apps/swarm-api/swarm_api/routes/outcomes.py:47`, `apps/swarm-ui/src/Activity.tsx:137` |
| Screen G — Reconciler last pass | still blocked: the report is held in process memory and never written to Firestore | `apps/reconciler/reconciler/service.py:121` |
| Agents table — cost and token column | partly shipped: the attempt carries typed spend and the agent detail shows it; the Agents table has no cost column | `apps/swarm-api/swarm_api/codec.py:578`, `apps/swarm-ui/src/AgentDetail.tsx:935` |
| Agents table — fencing-generation indicator | partly shipped: `task_to_api` serves `current_generation` and `current_lease_id`; no screen reads them | `apps/swarm-api/swarm_api/codec.py:266` |
| Agent detail — attempt and pod panel | shipped: `GET /v1/tasks/{id}/attempts` and `GET /v1/attempts`; execution name, peak RSS and OOM near miss on the detail | `apps/swarm-api/swarm_api/routes/tasks.py:251`, `apps/swarm-api/swarm_api/routes/attempts.py:37`, `apps/swarm-ui/src/AgentDetail.tsx:1643` |
| Per-engineer agent grouping / owner filter | still blocked: `GET /v1/tasks` takes no `submitted_by` filter; the owner column is client-side | `apps/swarm-api/swarm_api/routes/tasks.py:106` |
| Cross-tenant 'all agents' admin table | still blocked: every task list is scoped to the caller's tenant | `apps/swarm-api/swarm_api/deps.py:287` |
| Agent-spawns-sub-agent social graph | still blocked: a worker still cannot create a task | — |
| Worker Console | partly shipped: `GET /v1/tasks/{id}/logs` serves a live tail the worker publishes to the bucket; still no Cloud Logging relay | `apps/swarm-api/swarm_api/routes/tasks.py:457`, `apps/swarm-ui/src/Artifacts.tsx:226` |
| Live Transcript | shipped: the worker runs stream-json and `GET /v1/tasks/{id}/transcript` serves it redacted | `apps/agent-worker/agent_worker/procman.py:67`, `apps/swarm-api/swarm_api/routes/tasks.py:524` |
| Browser Filmstrip | partly shipped: the artifact byte route exists and images render inline, one artifact at a time | `apps/swarm-api/swarm_api/routes/tasks.py:374`, `apps/swarm-ui/src/Artifacts.tsx:1175` |
| Live Browser View | still blocked, and still not to be promised | — |
| ACC-1 Account pool | shipped: `GET /v1/accounts`, rendered by Capacity ▸ Accounts | `apps/swarm-api/swarm_api/routes/accounts.py:165`, `apps/swarm-ui/src/Accounts.tsx:160` |
| ACC-2 Account detail drawer | partly shipped: the read and the drawer exist; refresh health is still not persisted | `apps/swarm-api/swarm_api/routes/accounts.py:165`, `apps/swarm-ui/src/Accounts.tsx:1067` |
| ACC-3 Add account | shipped: the browser sign-in, `POST /v1/accounts/authorize` then `POST /v1/accounts/exchange`; no CLI handoff is needed | `apps/swarm-api/swarm_api/routes/accounts.py:258`, `apps/swarm-api/swarm_api/routes/accounts.py:285` |
| ACC-4 State changes and danger zone | partly shipped: pause, resume, drain, remove and refresh routes are used by the UI; no `expected_state`, actor or audit trail | `apps/swarm-api/swarm_api/routes/accounts.py:359`, `apps/swarm-api/swarm_api/routes/accounts.py:377`, `apps/swarm-api/swarm_api/routes/accounts.py:328` |
| ACC-5 Quota panel | partly shipped: a usage poller now writes the windows; refresh TTL is not stored | `apps/quota-broker/quota_broker/usagepoll.py:140`, `apps/swarm-ui/src/Accounts.tsx:740` |
| ACC-6 Pool refresh-health banner | still blocked: `last_refresh_at` is still not persisted | — |
| ACC-7 Who is using this account | shipped: the broker records holds; `GET /v1/accounts/{id}/holders` and `/history` | `apps/quota-broker/quota_broker/accounts.py:295`, `apps/swarm-api/swarm_api/routes/accounts.py:441`, `apps/swarm-api/swarm_api/routes/accounts.py:548` |
| Trouble board — silent workers | shipped: `GET /v1/admin/leases`, counted by the Overview checks | `apps/swarm-api/swarm_api/routes/admin.py:401`, `apps/swarm-ui/src/checks.ts:205` |
| Trouble board — admitted but never dispatched | shipped: the same route, filtered on `dispatch_overdue` | `apps/swarm-api/swarm_api/routes/admin.py:401`, `apps/swarm-ui/src/checks.ts:233` |
| Trouble board — failures that need a human, platform-wide | partly shipped: a FAILED check over the caller's own tenant; no cross-tenant route | `apps/swarm-api/swarm_api/routes/tasks.py:106`, `apps/swarm-ui/src/checks.ts:438` |
| Task timeline — per-attempt facts strip | shipped: `GET /v1/tasks/{id}/attempts`, drawn with exit code, RSS and OOM | `apps/swarm-api/swarm_api/routes/tasks.py:251`, `apps/swarm-ui/src/AttemptTimeline.tsx:321` |
| Activity stream | still blocked: events are per task only | `apps/swarm-api/swarm_api/routes/tasks.py:190` |
| Signals — the eleven metric charts | still blocked: no Monitoring proxy, and swarm-api holds no `monitoring.viewer` | `terraform/modules/iam/bindings.tf:75` |
| Signals — alert policy register | still blocked, for the same reason | `terraform/modules/iam/bindings.tf:75` |
| Signals — what is firing right now | still refused for v1 | — |
| Attempt log viewer | partly shipped: `GET /v1/tasks/{id}/logs?attempt_id=` serves the worker's output from the bucket; no Cloud Logging lines | `apps/swarm-api/swarm_api/routes/tasks.py:457` |
| Reconciler passes / 'what is broken right now' | still blocked: the report is memory-only | `apps/reconciler/reconciler/service.py:121` |
| Admin action log | partly shipped: pool writes stamp the latest actor; no audit collection | `apps/swarm-api/swarm_api/routes/admin.py:134` |
| Dead-letter queue | still refused | — |
| Pub/Sub dead-lettered message list | still refused | — |
| A2 — Workflows per engineer by outcome | partly shipped: the rollup now writes `workflow.state`; no per-engineer workflow outcome view | `apps/swarm-api/swarm_api/rollup.py:626`, `apps/swarm-api/swarm_api/routes/admin.py:562` |
| A3 — Agents started (Cloud Monitoring) | still blocked: no `monitoring.viewer` and no proxy | `terraform/modules/iam/bindings.tf:75` |
| A3 — Agents started per engineer | partly shipped: `GET /v1/outcomes` groups by `submitted_by` from Firestore; the Monitoring path is still blocked | `apps/swarm-api/swarm_api/routes/outcomes.py:58` |
| A4 — Per-tenant budget column | still blocked, permanently: there are no budgets (owner, 2026-10-01) and the column is omitted | `apps/swarm-api/swarm_api/routes/admin.py:266` |
| O2 — Capacity Ledger (Leases tab) | shipped as Capacity ▸ Holders | `apps/swarm-api/swarm_api/routes/admin.py:401`, `apps/swarm-ui/src/Holders.tsx:131` |
| O2 — Capacity Ledger (Attempts & sizing tab) | partly shipped: `GET /v1/attempts` exists, tenant-scoped; no screen calls it | `apps/swarm-api/swarm_api/routes/attempts.py:37` |
| O2 — reconciler pass panel | still blocked: memory-only | `apps/reconciler/reconciler/service.py:121` |
| O3 — Config vs Reality (pool + tenant rows) | still blocked | — |
| O3 — Config vs Reality (service env + alert policy rows) | still blocked | — |
| O4 — Deploy Truth | still blocked | — |
| O5 — Control-Action Trail | partly shipped: the latest actor on pools; no audit collection | `apps/swarm-api/swarm_api/routes/admin.py:134` |
| O6 — Alerts panel | still blocked | `terraform/modules/iam/bindings.tf:75` |
| O6 — Platform error feed | still blocked: events are per task only | `apps/swarm-api/swarm_api/routes/tasks.py:190` |
| O7 — Account health (admin slice) | partly shipped: accounts, refresh and history routes and the Accounts screen exist, tenant-scoped; no admin slice | `apps/swarm-api/swarm_api/routes/accounts.py:165`, `apps/swarm-ui/src/Accounts.tsx:195` |
| F1 — The gate | partly shipped: the external ALB, IAP and assertion verification exist (where `enable_frontend` is set); no "authenticated but not admitted" page | `terraform/modules/frontend/main.tf:1`, `apps/swarm-api/swarm_api/auth.py:139` |
| F2 — Identity and tenant bar | shipped: `GET /v1/tenants/me`, in the shell | `apps/swarm-api/swarm_api/routes/tenants.py:24`, `apps/swarm-ui/src/Spine.tsx:351` |
| F4 — System status and reachability panel | partly shipped: per-route data-source cells; rate-limit headroom is still not exposed | `apps/swarm-ui/src/DataSources.tsx:90`, `apps/swarm-api/swarm_api/ratelimit.py:80` |
| F5 — Session expiry and degraded-mode overlay | shipped: a 401 renders "Session expired" with a reload | `apps/swarm-ui/src/Overview.tsx:169`, `apps/swarm-ui/src/Dock.tsx:231` |

**[cluster-state](02-cluster-state.md)**

| screen | blocked on |
|---|---|
| Screen A — Operations Home (pause banner, 4 tiles, per-profile headroom, Attention rollup, data-source strip) | P1 (GET /v1/admin/leases) and P2 (GET /v1/admin/quota) for 3 of the 6 attention checks; ships today in a reduced form that names the checks it could n |
| Screen C — Live capacity holders (lease list, in-flight vCPU/GiB, class mix, accounting-drift check) | P1 (GET /v1/admin/leases) |
| Screen D — Backend reality (Cloud Run executions vs GKE Jobs vs leases, the four reconciler disagreements) | P5 (IAM + a backend-inventory read endpoint) |
| Screen E — GKE Autopilot nodes (recommended NOT to build; pod detail instead) | P6 (container.nodes.list IAM, a list_node call, a new model field and endpoint) |
| Screen F — Throughput over time (attempts started / released / parked / fenced). NOT a utilisation chart; utilisation history does not exist at all | P8 (Cloud Monitoring read access) for throughput; P9 for any real utilisation history |
| Screen G — Reconciler last pass (findings by kind, slots released, errors) | P7 (persist ReconcileReport to reconciler_runs/{id} + a read endpoint) |

**[agents-and-workflows](03-agents-and-workflows.md)**

| screen | blocked on |
|---|---|
| Agents table — cost and token column | P8: commit the uncommitted _usage_summary() worker change and redeploy agent-runtime-base; then CR entry 2 for typed Attempt fields if the numbers mus |
| Agents table — fencing-generation indicator (column 7 glyph) | P1: task_to_api drops current_generation and current_lease_id even though Task carries them |
| Agent detail — attempt and pod panel (execution_name, exit_code, peak_rss_bytes, oom_near_miss, heartbeat_at) | P1: attempts and leases have no API read path at all |
| Per-engineer agent grouping / owner filter (server-side) | P5: no submitted_by query param and no composite index covering it |
| Cross-tenant 'all agents in the cluster' admin table | P9: every list method starts from an equality filter on tenant_id and no admin branch exists |
| Agent-spawns-sub-agent social graph | P10: no mechanism exists — the worker has no create-task method, the container gets no API URL or token, and the only create path requires a human's G |

**[live-logs](04-live-logs.md)**

| screen | blocked on |
|---|---|
| Worker Console (Cloud Logging status ticker) | a Cloud Logging relay route in swarm-api plus roles/logging.viewer on its service account. Also needs one gcloud logging read against a finished GKE a |
| Live Transcript (the agent's own words, as it works) | the full chain: CLAUDE_CODE_ARGS set to stream-json (without it there is nothing incremental to tee), streaming-safe redaction with a carry-over buffe |
| Browser Filmstrip (post-hoc screenshots) | the artifact byte-proxy route -- without it this is a list of filenames, not a filmstrip, and should not ship. Frames also only land at terminal state |
| Live Browser View (CDP / VNC / video) | does not exist and should not be promised. No screenshot stream, video, trace, CDP or VNC surface anywhere; three independent deliberate blocks (ingre |

**[accounts](06-accounts.md)**

| screen | blocked on |
|---|---|
| ACC-1 Account pool (list) | P1 — accounts read API (every registry value already exists in Firestore; nothing can reach them from a browser). Quota/token/agents columns additiona |
| ACC-2 Account detail drawer | P1 for the document read; P2 for refresh health; P6 for secret-version freshness. Identity and derived secret names are real today. |
| ACC-3 Add account (guided CLI handoff) | P1 (validate + poll for the document) and P9 (refuse a colliding derived secret name). Browser-side credential paste is blocked on an IAM grant swarm- |
| ACC-4 State changes and danger zone (pause/resume/drain/remove) | P1 + P7 (expected_state precondition, actor, audit trail). DRAINING is advisory until P5 because nothing assigns agents to accounts. |
| ACC-5 Quota panel (session 5h / week 7d / refresh TTL) | P4 for utilization and resets (no writer exists for windows/observed_at); P2 for refresh TTL. Ships as an explicit 'not measured' panel until then — n |
| ACC-6 Pool refresh-health banner (sweep liveness) | P2 — last_refresh_at is not persisted anywhere; the sweep result is returned to Cloud Scheduler and discarded. |
| ACC-7 Who is using this account (agents per account) | P5 — frozen-contract change request; nothing assigns an account to an agent and Lease has no account field. |

**[alerts-errors](05-alerts-errors.md)**

| screen | blocked on |
|---|---|
| Trouble board — silent workers (unreleased leases, heartbeat age vs the reconciler's own thresholds) | P5 — GET /v1/leases; there is no attempts or leases endpoint anywhere in swarm-api |
| Trouble board — admitted but never dispatched (LEASED past dispatch_deadline) | P5 — GET /v1/leases (the index leases-state-dispatch-deadline already exists) |
| Trouble board — failures that need a human, platform-wide | P6 — a new Firestore index (state ASC, created_at DESC) plus an admin-gated route; without the index Firestore returns FAILED_PRECONDITION, not a slow |
| Task timeline — per-attempt facts strip (exit_code, peak RSS, peak disk, OOM near miss, execution name) | P4 — GET /v1/attempts; the attempts collection is rich and no HTTP route reaches it |
| Activity stream (cross-task event feed, tenant-scoped and admin cross-tenant) | P1 (GET /v1/events collection-group route — no code calls collection_group() today) and P2 (index events tenant_id/type/at, which is also the cost con |
| Signals — the eleven metric charts reused verbatim from dashboard.tf | P9 — a Monitoring proxy in swarm-api that builds filters server-side from tile names, plus roles/monitoring.viewer on the swarm-api service account |
| Signals — alert policy register with an 'ever observed?' column | P10 — alertPolicies.list through the same proxy |
| Signals — what is firing right now | refused for v1: Monitoring v3 exposes no open-incident endpoint and only email notification channels exist; replaced by a clearly-labelled computed co |
| Attempt log viewer (worker structured lines, severity filter, live poll) | P11 — GET /v1/attempts/{id}/logs with a server-built filter and roles/logging.viewer; carries the project-wide log-read caveat and shows no agent outp |
| Reconciler passes / 'what is broken right now' | P7 — reconciler findings are computed per pass and discarded; nothing persists them, and detected-but-skipped findings leave no trace in Firestore at  |
| Admin action log (who paused dispatch, who lowered a limit) | P8 — set_dispatch_paused overwrites one document and pool limit writes record no actor at all |
| Dead-letter queue | refused: TaskState.DEAD_LETTERED is never written by any code path (exhausted retries go to FAILED), so the screen would be permanently empty and indi |
| Pub/Sub dead-lettered message list | refused: reading the DLQ either acks (destroying the messages) or leaves them redelivered, and the payload is a wake doorbell with no tenant or task i |

**[user-activity](07-user-activity.md)**

| screen | blocked on |
|---|---|
| A2 — Workflows per engineer by outcome | P10 — workflows.state is written once as QUEUED and never updated by anything; outcome must be derived from tasks.workflow_id until then |
| A3 — Agents started (Cloud Monitoring 'starting' series, per tenant, per runner_profile, calendar ranges) | P5 — roles/monitoring.viewer is not granted to swarm-api, google-cloud-monitoring is not a swarm-api dependency, and no API route proxies projects.tim |
| A3 — Agents started per engineer | P6 — submitted_by never reaches the worker (worker_env carries identifiers only) so no log-based metric can carry a user label; recommended NOT to bui |
| A4 — Per-tenant budget column | nothing will fix it — monthly_budget_usd is None for every tenant because the only write path 422s; the column must be omitted, not left blank |

**[operator-gaps](08-operator-gaps.md)**

| screen | blocked on |
|---|---|
| O2 — Capacity Ledger (Leases tab) | P1 — GET /v1/admin/leases; no route reads the leases collection at all |
| O2 — Capacity Ledger (Attempts & sizing tab) | P2 — GET /v1/admin/attempts; and P4/P5 for per-profile grouping, since attempt documents carry no runner_profile |
| O2 — reconciler pass panel | P9 — ReconcileReport lives in per-instance memory (service.py:100) and stdout only |
| O3 — Config vs Reality (pool + tenant rows) | P10 — a terraform-owned config/pools and config/tenants mirror the control plane can read; today the configured value exists only as a terraform outpu |
| O3 — Config vs Reality (service env + alert policy rows) | D1 then P12 — nothing compares either side to what Cloud Run is actually serving |
| O4 — Deploy Truth | P13 for the intended digest (the manifest is a local file written before push-images.sh's own failure check), and D1/P12 for the live digest |
| O5 — Control-Action Trail | P8 — there is no audit collection; pools have no updated_by, control/dispatch keeps only the latest actor, admin actions are an unlabelled Prometheus  |
| O6 — Alerts panel | D1 then P12 — alertPolicies.list is not reachable from swarm-api |
| O6 — Platform error feed | P3 — GET /v1/tasks/{id}/events is per-task; no cross-task route exists (the collection-group index does) |
| O7 — Account health (admin slice) | P6 must land first or the state column paints green over a dead account; then P7 for any route at all over the accounts collection |

**[reachability](01-reachability.md)**

| screen | blocked on |
|---|---|
| F1 — The gate: IAP sign-in and the "authenticated but not admitted" page | No load balancer and no IAP exist (P1, P2), and the app cannot verify an IAP assertion (P3). Until all three land there is no network path from a brow |
| F2 — Identity and tenant bar (app shell header) | Every field it shows exists today (GET /v1/tenants/me), but it is unreachable without P1/P2/P3. Two values are additionally degraded: is_admin is perm |
| F4 — System status and reachability panel | Unreachable without P1/P2/P3. Three of its intended values are also missing server-side: rate-limit headroom is computed but not exposed (ratelimit.py |
| F5 — Session expiry and degraded-mode overlay | Its trigger is IAP session expiry, which does not exist until P1/P2. The classification logic it depends on (F3) is buildable now. |

## Prerequisites touching the frozen contract

`apps/common/swarm_common/` is frozen. These are change REQUESTS, never edits — see `docs/contract-change-requests.md`.

| what | area | effort |
|---|---|---|
| NOT REQUESTED, and worth recording as declined: a denormalised active_by_resource_class field on SlotPool. The investigation raise | cluster-state | none — explicitly not requested |
| Commit and deploy the already-written worker usage extraction. _usage_summary() at apps/agent-worker/agent_worker/lifecycle.py:105 | agents-and-workflows | small to ship the worker half; the typed Attempt fields are  |
| Runtime agent-spawns-agent, if the owner genuinely wants the sub-agent tree rather than the workflow DAG. Three independent walls  | agents-and-workflows | large |
| A model-driven browser agent, without which 'watch a Claude agent do visual QA' describes nothing. The browser profile is a script | live-logs | large |
| Account-to-agent assignment: call choose() at admission, record the assignment on the lease, install the account credential in the | accounts | large |
| TaskEvent needs an `expires_at` field, or the events TTL must be withdrawn. indexes.tf configures a Firestore TTL on events using  | alerts-errors | small |
| Optional: a denormalised attempts_exhausted boolean on Task, so 'failures that need a human' becomes a server-side query instead o | alerts-errors | small |
| Apply contract-change request #2 (docs/contract-change-requests.md:100): add optional input_tokens, output_tokens, cache_read_inpu | user-activity | large |
| Contract change REQUEST (never an edit): type runner_profile and resource_class on Attempt, as optional fields defaulting to None. | operator-gaps | small |
| Accept a list of token audiences. Settings.api_audience is a single string and GoogleTokenVerifier takes audience: str, but a brow | reachability | small |
