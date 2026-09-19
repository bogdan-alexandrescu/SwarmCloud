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
- A1 — Activity (tenant timeline: stacked outcome chart, submitted line, 4 stat tiles, coverage bar, runner-profile split)
- A1 — Recorded spend tile + coverage bar
- A2 — People (per-engineer table + engineer drawer, derived from A1's rows at zero extra reads)
- A4 — Tenants (admin roster: tenant_id, kind, principal, enabled, max_active, capacity_units, credential chips)
- A4 — Platform-wide state counts (admin, behind an explicit button, 5-minute cache)
- Window & provenance bar (covered span, timezone, row budget, bucket size, coverage ratio, refresh age)

**[operator-gaps](08-operator-gaps.md)**
- O1 — Control Room

**[reachability](01-reachability.md)**
- F3 — Fetch contract / provenance component (the empty-vs-broken rule)

## Blocked, with what blocks them


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
