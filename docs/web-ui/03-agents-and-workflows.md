## Agents, workflows, table and graph views

Everything below was read from source in `/Users/bogdan/claudespace/agent-swarm-infra` at HEAD `2d06f26`. Nothing was run against the live deployment, with one exception noted at §2.4(a), where I executed the function in question because the claim turned on it. Where I say a value exists, there is a file:line. Where I say it does not, I looked and say what I grepped for.

**Three commits landed after the `456b426` this section was first written against, and two of them change what is in it:** `ef1a9c5` committed the worker token/cost extraction and `docs/contract-change-requests.md` (§2.4(a) previously described both as uncommitted working-tree changes — they are committed), and `9c639af` / `2d06f26` are both fixes for the same class of bug this section had no answer for: a failed read rendered as an empty result. §1.7 is new and exists because of that.

---

### 0. The correction that changes the brief

The owner asked for "agents grouped by workflow … a graph view (workflow -> sub-agents, like a social graph)". Half of that is better than expected and half of it does not exist.

**The good half.** A workflow is a real, validated, acyclic DAG, not a star. `Task.depends_on` (`apps/common/swarm_common/models.py:181`) holds **parent task ids**, not step names — resolved at submission in `apps/swarm-api/swarm_api/service.py:209` (`parent_task_ids = [step_task_id[dep] for dep in source.depends_on]`) and passed at `:223`. Edges are already materialised on the child document: no join, no reverse lookup. Cycles are impossible (`apps/swarm-api/swarm_api/validation.py:158-218` rejects them at submission with the exact cycle named). And `GET /v1/workflows/{id}` (`apps/swarm-api/swarm_api/routes/workflows.py:44-57`) returns the workflow, every step, and up to 200 live task documents **in one request**. The graph view needs zero backend work.

**The half that does not exist.** An agent cannot spawn a sub-agent. Three independent walls, any one fatal:

1. The worker's control plane has no create-task method. `apps/agent-worker/agent_worker/control.py:149-636` talks to Firestore directly and its whole vocabulary is fetch / transition / emit / heartbeat / checkpoint / park / finish.
2. The container is never told where the API is. `worker_env` at `apps/scheduler/scheduler/dispatch.py:194-220` passes identifiers only — no API URL, no token — and that restriction is documented there as invariant 10 enforced at the last moment.
3. The only create path is `POST /v1/tasks`, which requires a Google ID token for a human in an allowed hosted domain with a pinned audience (`apps/swarm-api/swarm_api/auth.py:62-107` for verification, `:196` for the domain check). A pod has a GSA, not a user identity.

So there is no parent/child runtime edge, no mechanism to produce one, and nothing recorded. **The UI must say "workflow DAG", never "agent tree" or "social graph".** Section 6 says what the nearest honest thing is and what it would take to build the real one.

---

### 0.5 The second correction: nothing in this repository can serve a browser yet

This outranks every screen below, because every screen below is a browser polling `GET /v1/tasks`. Four separate facts, each verified:

1. **There is no CORS middleware.** `grep -rn -i cors apps/` returns nothing. `create_app` (`apps/swarm-api/swarm_api/main.py:54-149`) installs exactly one `@app.middleware("http")`, and it is the latency/counter observer. A page served from any origin other than the API's own cannot read a single response.
2. **Ingress forbids being publicly reachable.** `terraform/modules/cloud_run/variables.tf:30-44` pins `ingress` to `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` and has a validation block that *refuses* `INGRESS_TRAFFIC_ALL` ("would expose the control plane to the internet").
3. **No load balancer exists to be the "and Cloud Load Balancing" half.** `grep -rli "forwarding_rule\|network_endpoint_group\|url_map\|backend_service" terraform/` returns nothing. There is no IAP config either. So the declared front door has no door.
4. **The token a browser can get is the wrong token.** The API pins the ID token audience (`auth.py:62-107`; `API_AUDIENCE` is mandatory outside local dev, `deps.py:92-98`), and `terraform/infra/variables.tf:278-308` records that edge IAM *consumes* the caller's `Authorization` header — measured on a correlated request on 2026-09-16 — which is why `api_invokers` is expected to be `allUsers` with internal ingress rather than a real IAM gate. A Google Sign-In token minted in a browser carries the web OAuth client id as its audience, not the service URL.

**Consequence, and it is a build-order consequence, not a caveat.** A pure SPA calling `GET /v1/tasks` directly is not buildable against what exists. The honest v1 is a small server-side component — a BFF inside the ingress boundary that holds the ID token, adds CORS for its own origin, and forwards to `swarm-api` — plus the load balancer that gives it an address. That is Track C (ingress, LB) and Track A or a new track (the BFF); it is not UI work and it does not shrink by writing more of this spec. Every request count, payload size and poll interval below is measured at the API and applies unchanged at the BFF, which is why the numbers are still worth having now.

I have not verified how the deployed environment is reached today — `docs/DEPLOY_STATE.md` records the services live, and `scripts/lib/common.sh:428-451` mints an ID token for `scripts/api.sh`, but nothing in the repository declares the network path that makes that call arrive.

---

### 1. Vocabulary and colour, decided once and reused by every screen

Six things every screen in this section shares. Getting them wrong once produces a UI that lies consistently.

**1.1 — Only nine of the twelve task states are real.** `TaskState` has 12 members (`apps/common/swarm_common/states.py:17-29`). Repo-wide, **nothing ever writes `SUBMITTED`, `QUEUED` or `DEAD_LETTERED` to a task document.** `service.py:122` says so in a comment — "Walk the real state machine even though only the end state is stored" — and tasks are created directly at `READY` or `PARKED` (`service.py:127/131/135`). `QUEUED` is only the creation state of a *workflow* (`service.py:258`). `DEAD_LETTERED` appears only as an enum member, two membership tests and an unreachable dict branch (`agent_worker/control.py:633`; `finish()` is only ever called with SUCCEEDED / FAILED / CANCELLED).

  *Consequence:* those three must not appear in a state filter dropdown, a state legend, or a state histogram. They would be permanent zeroes, and a permanent zero reads as "nothing is broken" rather than "this bucket cannot fill".

  *One exception, and it is a decoding exception only:* `DEAD_LETTERED` is in the scheduler's `_FAILED_PARENT_STATES` (`apps/scheduler/scheduler/loop.py:55-57`), so §2.3's parent check must still recognise the string. Recognising it is not the same as offering it as a filter.

**1.2 — `FAILED` is a sink in practice, whatever the transition map says.** `_ALLOWED` permits `FAILED -> READY` (`states.py:93`), but nothing performs it. The reconciler is the only retry engine and it refuses terminal states outright (`apps/reconciler/reconciler/store.py:209-210`: `if current in TERMINAL_STATES: return None`). Retries re-enter from `LEASED/DISPATCHED/STARTING/RUNNING -> READY`, and exhaustion is decided *before* FAILED is written (`reconciler/store.py:212-216` flips a READY repair to FAILED once `attempt_count >= max_attempts`). There is no retry route — `routes/tasks.py` has create, batch, list, get, cancel, events, artifacts and nothing else.

  *Consequence:* no "Retry" button anywhere in this section. A button that POSTs nowhere is worse than no button.

**1.3 — Three colour groups, taken from the frozen sets, not invented.**

| Group | Members | Meaning | Colour intent |
|---|---|---|---|
| Live | `LEASED, DISPATCHED, STARTING, RUNNING` (`CONCURRENCY_STATES`, `states.py:35-42`) | **This is the honest definition of "a running agent"** — these and only these hold a pool slot and cost money | accent / saturated |
| Waiting | `READY, PARKED` | durable, costs nothing (invariant 1) | neutral / muted |
| Done | `SUCCEEDED, FAILED, CANCELLED` | terminal | success / danger / grey |

`CONCURRENCY_STATES` is genuinely load-bearing, not decorative: `reconciler/store.py:80-82` builds its Firestore `in` query straight from it, `reconciler/model.py:59-60` defines `holds_capacity` from it, and `agent_worker/control.py:255` gates on it. Colour by it and the UI's "live" count means the same thing as the platform's.

Never use colour alone — each chip carries a glyph and the state word. `PARKED` amber and `FAILED` red are indistinguishable to ~8% of male viewers.

**1.4 — `blocked_by` has two different shapes and the UI must not assume either.** This is the "why is this agent not running" field and it is the most valuable column on the table, but it is written by two unrelated code paths:

* **Admission denial** (`apps/common/swarm_common/admission.py:97-114`, written by `scheduler/store.py:192-202`): `{"pool": "provider:anthropic", "reason": "PROVIDER_CONCURRENCY_LIMIT", "limit": 10, "active": 10}`. All four keys are written unconditionally on both branches (the disabled-pool branch is `admission.py:105-109`), so in practice an admission blocker always carries `limit` and `active`.
* **Worker park** (`agent_worker/control.py:595-602`): `[{"reason": "<ParkReason>", **detail}]` where `detail` is arbitrary. On the interrupted path (`lifecycle.py:444-448`) `detail` is `{"cause": "worker_interrupted", **summary}` — and `summary` is the **entire artifact/log/checkpoint dict**. A `blocked_by` entry can legitimately contain an `artifacts` array.

  *Consequence:* the renderer keys off `entry["reason"]` and treats `pool`, `limit`, `active` as optional enrichment. Rendering `entry` as JSON, or assuming `limit` exists, produces either a crash or a paragraph of GCS URIs inside a table cell.

  *Also:* `blocked_by` is cleared on admission (`admission.py:237`), on promotion to READY (`scheduler/store.py:216-227`) and on cancel (`scheduler/store.py:313-322`, `swarm_api/store.py:441-443`). A stale blocker cannot survive a state change, so the UI never needs to age one out itself.

**1.5 — The reason vocabulary the UI must have copy for.** Only these are ever written:

* From admission (`_blocked_reason_for_pool`, `admission.py:71-84`, plus the disabled-pool branch at `:105-109`): `GLOBAL_CONCURRENCY_LIMIT`, `TENANT_LIMIT`, `PROVIDER_CONCURRENCY_LIMIT`, `RESOURCE_CLASS_LIMIT`, `RUNNER_LIMIT`, `BACKEND_LIMIT`, `MANUAL_PAUSE`.
* From `ParkReason` (`states.py:125-137`), which lands in `park_reason` **and** in `blocked_by[].reason`: `PROVIDER_QUOTA_EXHAUSTED`, `PROVIDER_COOLDOWN`, `PROVIDER_OUTAGE`, `SCHEDULED_RETRY`, `DEPENDENCY_INCOMPLETE`, `MANUAL_PAUSE`, `BUDGET_EXHAUSTED`, `CREDENTIAL_MISSING`.

`BlockedReason.BUDGET_LIMIT`, `QUOTA_EXHAUSTED`, `COOLDOWN`, `DEPENDENCY` and `SCHEDULED_RETRY` are **never written as blockers** — do not build a legend that promises them. `codec.blocked_reason_values()` (`codec.py:370-371`) exists and is wired to no route, so the UI ships the copy table itself, and ships an unknown-reason fallback that prints the raw string rather than blanking the cell.

The one distinction worth designing around: `TENANT_LIMIT` means *you* are at your limit and only an admin can help; `GLOBAL_CONCURRENCY_LIMIT` / `RESOURCE_CLASS_LIMIT` mean the platform is busy and waiting is the answer. That difference is exactly why the field was surfaced verbatim (`states.py:140-145`), and the copy should preserve it.

**1.6 — `attempt_count` and `current_generation` are different numbers and the gap is meaningful.** Both are on the task. `attempt_count` increments only on admission (`admission.py:236`). `current_generation` increments on admission (`:235`) **and** on a reconciler fence (`reconciler/store.py:164-172`). So `current_generation > attempt_count` means "a stale worker was fenced out". Show `attempt_count / max_attempts` as the primary figure and surface generation only in the detail view, labelled "fencing generation", with the gap called out when it exists. See §2.2 for why it cannot be rendered at all today.

**1.7 — Empty, failed, partial and stale are four different things, and only one of them is an empty table.** This is decided here because every screen in this section polls, and because the repository's last two commits (`9c639af` "An HTTP error is not an empty result", `2d06f26` "a failed listing is not an empty collection") are both fixes for having got this wrong on the server side. Shipping it wrong in the UI would re-import the bug one layer up.

Five states per list, and they must be visually distinct:

| Condition | What the UI shows |
|---|---|
| Request in flight, nothing cached | skeleton rows — never the empty-state copy |
| `200`, `tasks: []` | the empty state, worded for the tab: "No agents are running right now." / "Nothing is waiting." Only this case may say "none" |
| `4xx`/`5xx` | an error state that keeps the last good rows visible, dimmed, with "couldn't refresh — showing the list from 14:02" and a Retry control. **Never** an empty table |
| Some of the Live tab's queries failed | the partial state: the rows that arrived, plus "2 of 4 states could not be loaded — this list is incomplete" and a **suppressed count badge**. A badge computed from three of four states is a wrong number presented as a right one |
| Poll suspended (`document.hidden`, or repeated failure) | "paused — updated 6m ago", and an immediate fetch on resume |

Two response shapes, because the API has two:

* The application's own errors are `{"code", "message", "detail"?}` (`apps/swarm-api/swarm_api/errors.py:15-28`) with codes `bad_request` 400, `validation_failed` 422, `unauthenticated` 401, `forbidden` 403, `not_found` 404, `conflict` 409, `rate_limited` 429, `upstream_unavailable` 503. `message` is written for a human and is safe to show. `429` carries `Retry-After` (`main.py:99-108`) and the UI must honour it rather than retry into the same wall.
* **Anything else is a bare FastAPI 500** — `create_app` registers handlers for `ApiError`, `AuthError`, `InvalidTransition` and `RequestValidationError` and nothing else (`main.py:99-147`), so an uncaught exception returns `{"detail": "Internal Server Error"}`, a different shape with no `code`. The UI must not key off `code` existing.

**The specific 500 to expect, and it is the one that looks most like "empty".** Firestore fails a query with `FAILED_PRECONDITION` when a composite index is missing *or still building* — the reasoning is written out at `swarm_api/store.py:8-31` — and nothing catches it. Every tab below is a state-filtered or workflow-filtered query that depends on a composite index. The findings note the indexes are declared in terraform but were not confirmed built in the live project. So the single most likely first-run failure of this screen is a 500 on `?state=RUNNING` that an unwary UI renders as "no agents are running". The error state must name the status code and offer the raw `message`, because that is what tells an operator to go look at index build state.

---

### 2. Screen A — Agents

The primary screen of this section. One table, three tabs, no sub-pages.

#### 2.1 What the user sees

**Tabs (a segmented control, not a dropdown):** `Live` · `Waiting` · `Recent`. Each carries a count badge from the rows it holds — not from `/v1/stats`, so the badge and the table can never disagree. The badge is suppressed, not zeroed, whenever the tab is in the partial or error state of §1.7.

**Columns, desktop, in this order:**

| # | Header | Content | Source |
|---|---|---|---|
| 1 | *(state)* | glyph + state word chip, coloured per §1.3 | `task.state` |
| 2 | Agent | `runner_profile` in bold, `model` beneath in muted type, `id` last 8 chars in mono as a tertiary line | `task.runner_profile`, `task.model`, `task.id` |
| 3 | Why | the derived one-liner (§2.3) — empty for `RUNNING` and `SUCCEEDED` | derived from `park_reason`, `blocked_by`, `last_error`, `cancel_requested` |
| 4 | Owner | `submitted_by` local part, full address on hover | `task.submitted_by` |
| 5 | Workflow | `step_id` as a pill linking to the DAG view; em-dash when standalone | `task.workflow_id`, `task.step_id` |
| 6 | Elapsed | live ticking duration (§2.3) | derived from `created_at` / `started_at` / `completed_at` |
| 7 | Try | `attempt_count`/`max_attempts`, with a small fence glyph when `current_generation > attempt_count` | `task.attempt_count`, `task.max_attempts`, `task.current_generation` |
| 8 | Class | `resource_class` + units, e.g. `browser · 2u` | `task.resource_class`, units from `RESOURCE_CLASSES` (`profiles.py:77-81`, frozen catalogue, bundled client-side) |
| 9 | *(actions)* | Cancel, enabled only when the state is non-terminal | `POST /v1/tasks/{id}/cancel` |

Nine columns. Not fourteen. The things deliberately *not* columns: `tenant_id` (every row is the caller's own tenant — it goes in the page header once), `priority` (nobody sorts by it because the server will not sort by it), `provider` (implied by `runner_profile`; the 5-profile catalogue makes it redundant), `repository_url`, `timeout_seconds`, `input` — all of those live in the detail view.

**Cancel is not a state change, and the UI must not pretend it is.** `POST /v1/tasks/{id}/cancel` → `store.request_cancel` (`swarm_api/store.py:414-456`) behaves three ways:

* Terminal task → **409 `conflict`**, `detail.state`. So the control is disabled on terminal rows, and a 409 arriving anyway (the row went terminal between render and click) is shown as "already finished", not as an error.
* `READY` / `PARKED` → straight to `CANCELLED`, and the response carries `released_immediately: true` (`routes/tasks.py:107-120`).
* `LEASED` / `DISPATCHED` / `STARTING` / `RUNNING` → only `cancel_requested: true` is written; the state does not change until the worker or the reconciler releases the lease, because releasing it from the API would decrement a pool a live container still occupies. `released_immediately` is `false`.

  So an optimistic flip to CANCELLED is a lie for exactly the rows a user most wants to cancel. Render a persistent "cancelling…" chip on the row, driven by `cancel_requested == true && !isTerminal(state)`, and let the poll resolve it. That chip is also the honest render for a workflow-level cancel, which fans out through the same method (`store.py:578-601`).

**Filters above the table:** state (folded into the tabs), `runner_profile` (5 known values from the frozen catalogue, so a select, not a text input), owner (see §2.4(c) — client-side, and labelled as such), and a "standalone only" toggle (client-side, §2.4(d)).

**Grouping, which the owner asked for and the first draft of this section dropped.** "Agents grouped by workflow" is half a list feature and half the graph. The list half: a `Group by workflow` toggle on the `Waiting` and `Recent` tabs that collapses rows under a workflow header showing the workflow id, the step count present in the loaded page, and the **computed** rollup (any member LEASED/DISPATCHED/STARTING/RUNNING → running; all SUCCEEDED → succeeded; any FAILED/CANCELLED → failed, respecting `on_step_failure`). Standalone agents collect under one "No workflow" group, last.

Two honesty requirements on it, both load-bearing:
* The grouping is **client-side over the loaded page**, labelled exactly as §2.4(c) labels the owner filter. A group header that says "3 steps" when the workflow has 9 and 6 fell off page one is the same lie in a new place. Each group header therefore links to `GET /v1/tasks?workflow_id=<id>` — that filter *is* server-side (`store.py:381-399`, index `tasks-tenant-workflow-created`, `indexes.tf:72-81`) — and to the DAG view, which fetches the whole workflow in one request.
* The rollup is computed, **never** `workflow.state`. That field is written once as `QUEUED` at `service.py:258` and no component ever updates it: nothing in `apps/scheduler`, `apps/reconciler` or `apps/agent-worker` writes the `workflows` collection at all. Rendering it would label every finished workflow "queued" forever.

**Row click** opens the detail view (§3) as a right-hand drawer on desktop, a full-screen push on phone.

**Phone, which the column table above does not answer.** Below 640px the table becomes a card list; nine columns do not fold, they have to be re-authored:

* **Card line 1:** state chip (glyph + word) · `runner_profile` · elapsed, right-aligned.
* **Card line 2:** the "Why" one-liner, clamped to two lines. This is the reason the screen exists on a phone at all — someone is checking why their agent has not moved — so it outranks every identifier and is never the thing that gets dropped.
* **Card line 3, muted, one line:** `step_id` pill (or nothing when standalone) · `attempt_count`/`max_attempts` · `resource_class`. Owner is omitted: on a phone the viewer is almost always looking at their own work, and it is one tap away in the drawer.
* **Hidden entirely:** the id fragment, `model`, units, the fence glyph. All present in the full-screen detail push.
* **Tabs** stay a segmented control, full width, with counts. **Filters** collapse behind one "Filter" button opening a sheet; the client-side-only labelling from §2.4 must survive into the sheet, not be dropped for space. **Sort controls: none** — see §2.4(e). **Cancel** is not on the card; it lives in the detail push, behind the same non-terminal rule, because a destructive control inside a scrolling list on a touch target is a mis-tap waiting to happen.
* **Page size is 50, not 200** (§2.5), so the phone build must show "showing the 50 most recent" above the list rather than implying completeness.

#### 2.2 Where every value comes from

Route: `GET /v1/tasks` (`apps/swarm-api/swarm_api/routes/tasks.py:64-95`), backed by `Store.list_tasks` (`apps/swarm-api/swarm_api/store.py:381-412`). Response: `{tasks: [...], next_page_token, tenant_id}`. Each row is `codec.task_to_api` (`codec.py:99-132`), which returns exactly: `id, tenant_id, state, runner_profile, resource_class, provider, model, priority, created_at, updated_at, started_at, completed_at, submitted_by, attempt_count, max_attempts, timeout_seconds, next_eligible_at, park_reason, blocked_by, workflow_id, step_id, depends_on, cancel_requested, metadata, repository_url, repository_ref, input, last_error, result_summary, latest_checkpoint`.

`limit` is clamped by `paged_limit` (`deps.py:194-199`): omitted → 50 (`settings.py:82`), anything above 200 → 200 (`settings.py:81`), below 1 → 422. An unknown `state` string is a 422 whose `detail.known_states` lists all 12 enum members — including the three from §1.1 that can never occur, so do not build the filter from that list.

Note what is **not** in `task_to_api`: `current_generation` and `current_lease_id`. Column 7's fence glyph therefore **cannot be rendered today** — `task_to_api` drops both fields even though the Task dataclass carries them (`models.py:176-177`). Either add them to `task_to_api` (one line, unfrozen, Track A) or drop the glyph. I recommend adding them; `current_generation` is a small integer and it is the only cheap signal that a worker was fenced. Until it lands, column 7 is `attempt_count`/`max_attempts` alone and §1.6's guidance applies only to the detail view.

**Tab queries:**

* `Live` → four requests, one per concurrency state: `?state=LEASED`, `?state=DISPATCHED`, `?state=STARTING`, `?state=RUNNING`, each `&limit=200`. Merged and sorted client-side. Index `tasks-tenant-state-created` (`terraform/modules/firestore/indexes.tf:61-70`) covers all four. If any one fails, §1.7's partial state applies — a merge of three successes is not a Live list.
* `Waiting` → two requests: `?state=READY`, `?state=PARKED`.
* `Recent` → one request, no state filter, `&limit=200`. Index `tasks-tenant-created` (`indexes.tf:103-110`).

Four requests for the Live tab is ugly, and it is also what makes the partial-failure state necessary. §8/P1 replaces it with one `state=A,B,C,D` list parameter served by a Firestore `in` query — the pattern already exists at `reconciler/store.py:80-82` and needs **no new index**, because Firestore fans an `in` out into one sub-query per value against the same composite index. It collapses four failure surfaces into one as well as four requests into one.

**Why not just `?limit=200` unfiltered for the Live tab:** ordering is `created_at DESC` and nothing else (`store.py:403`). A `claude-code` agent with a 7200s timeout, submitted three days ago and still running, sits below every task submitted since. On a busy tenant it falls off page one and the "live agents" view silently omits the longest-running agent on the platform. State-filtered queries are the only correct construction today.

#### 2.3 Derived values, with their computations

**Column 3, "Why":** first match wins.

```
state == PARKED            -> parkReasonCopy[task.park_reason]
                              + relative time to task.next_eligible_at when set
state == READY && blocked_by.length
                           -> blockedCopy[blocked_by[0].reason]
                              + " (" + blocked_by[0].active + "/" + blocked_by[0].limit + ")"
                              when BOTH keys are present  <-- see 1.4
state == FAILED            -> task.last_error, single line, clamped to 2 lines with a tooltip
state == CANCELLED && !task.cancel_requested && task.depends_on.length
                           -> "an upstream step did not succeed"
state == CANCELLED         -> "cancelled by request"
otherwise                  -> ""
```

The cascade case deserves care, and the first draft of this rule was wrong twice.

When a workflow parent fails, the scheduler's dependency sweep **cancels the child** (`apps/scheduler/scheduler/loop.py:438-442`) with `last_error = "an upstream workflow step did not succeed"`. Do **not** string-match that sentence — compute it. But compute it correctly:

* The trigger is **any** parent in `_FAILED_PARENT_STATES = {FAILED, CANCELLED, DEAD_LETTERED}` (`loop.py:55-57`), not *every* parent. A child of two parents where one succeeded and one failed is cancelled. A rule requiring all parents to have failed would miss the common case and fall through to the wrong copy.
* In the **table**, the parents' states are not loaded — only `depends_on` ids are. The discriminator that works with what the row actually has is `cancel_requested`: the scheduler's cascade writes `state`, `park_reason: null`, `blocked_by: []`, `completed_at` and `last_error` and **never sets `cancel_requested`** (`scheduler/store.py:303-323`), while every human cancel sets it, including a whole-workflow cancel, which fans out through `request_cancel` per step (`swarm_api/store.py:578-601`). That is why the rule above keys off `!cancel_requested`, and it survives an upstream copy change.
* In the **workflow/DAG view** (§5–6), where the parents' states *are* in hand from the single `GET /v1/workflows/{id}`, upgrade the copy to name the parent: "blocked: `<step_id>` failed". Use the any-parent rule there too.

**Column 6, "Elapsed":**

```
completed_at != null -> completed_at - (started_at ?? created_at)   [static]
started_at   != null -> now - started_at                            [ticking]
otherwise            -> now - created_at, prefixed "queued "        [ticking]
```

`started_at` is written on `DISPATCHED -> STARTING` (`agent_worker/control.py:410-413`), so a `LEASED` or `DISPATCHED` task legitimately has none. Render "queued 4m", not "0s", and never `Invalid Date`. Every timestamp in `task_to_api` is passed through as the Firestore value, so the parser must also survive `null` on `completed_at` for a row that went terminal between two polls.

#### 2.4 What this screen cannot show yet

**a) No token, cost or spend column — and the prerequisite is closer than it looks but not finished.** `task.model` is recorded "for attribution and cost reporting" (`swarm_api/schemas.py:37-39`) and nothing consumes it. `TenantLimitsRequest.monthly_budget_usd` is accepted only so the refusal can explain itself, with the reason written beside it (`schemas.py:126-131`): *"This control plane has no cost attribution source — no billing export, no per-attempt spend."*

  The worker-side extraction the brief was waiting on **is committed** (commit `ef1a9c5`, not an uncommitted working-tree change as the first draft of this section said). `_usage_summary()` at `apps/agent-worker/agent_worker/lifecycle.py:1055-1103` pulls `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `thinking_tokens`, `total_cost_usd`, `num_turns`, `duration_ms`, `duration_api_ms` and a model list, and it is called at `lifecycle.py:474` — deliberately before `_truncate_json(..., 8000)` at `:470` discards the raw result. It lands at `task.result_summary.runner.usage`, which `task_to_api` already returns. The contract-change request for typed fields is entry 2 in `docs/contract-change-requests.md`, also committed.

  **It does not produce numbers yet, and I verified that by running it.** The call passes `runner_result.get("output")`, but for a CLI agent that dict is the runner's own envelope — `{provider, model, exit_code, structured_output, limits}` (`runners/cliagent.py:342-357`, written through `runners/base.py:227-236` and `:129-145`) — and the CLI's own JSON, which is where `usage`, `total_cost_usd`, `num_turns` and `modelUsage` live, is nested one level down under `structured_output`. `_usage_summary` looks for those keys at the top level, finds none, and returns `{}`:

  ```
  _usage_summary(<raw claude-code JSON>) -> {'input_tokens': 8, 'output_tokens': 402,
                                             'total_cost_usd': 0.0642028, 'num_turns': 4, ...}
  _usage_summary(<the dict production actually passes>) -> {}
  ```

  The unit test (`tests/unit/worker/test_usage_summary.py`) feeds it the raw CLI shape, so it passes while the production path yields nothing. This is a one-line Track B fix (fall back to `output.get("structured_output")`) plus a test at the real call site, and it belongs in a report to Track B rather than in this spec — but until it lands, **the cost column has no data source at all**, not merely an undeployed one.

  So the honest status: **not buildable today; buildable with no API change once (i) that one-line worker fix lands, (ii) a new `agent-runtime-base` image ships, and (iii) attempts run on it.** Three caveats that must be in the UI, not just in this spec:
  * only `claude-code` and `codex` produce it — both go through `cliagent.run_cli_agent` (`runners/cliagent.py:342-357`). `mock`, `generic` and `browser` report nothing, and **nothing is not zero**. Render `—`, never `$0.00`.
  * `result_summary` is only written by `finish()` (`control.py:610-636`). A `RUNNING` agent has no usage figure, and a `PARKED` one never will for the attempt it lost — the park path puts the summary in the event detail and in `blocked_by` (`lifecycle.py:444-448`), not in `result_summary`.
  * until the typed `Attempt` fields land (CR entry 2), these numbers live in an untyped dict. Nothing can query or index them, so "top spenders this week" is a client-side sum over one page — not a real aggregate.

**b) No pod, execution name, exit code, peak RSS or heartbeat.** Every one of those lives on the `attempts` or `leases` collection, and **neither has any API read path**. `Store` defines `LEASES = "leases"` (`store.py:80`) and `codec` has `attempt_from_dict` / `lease_from_dict` (`codec.py:159-196`), but no Store method and no route reads either — confirmed by grep across `apps/swarm-api/`. The documents are written (`agent_worker/control.py:425-505`), the indexes exist (`attempts-task-created`, `attempts-tenant-created`, `leases-tenant-created`, `indexes.tf:152-178`), the decoders exist. Only the routes are missing. See §8/P1 — this is the single highest-value unlock in this section.

  The workaround that works today is the events subcollection: the `DISPATCHED` event's `detail` carries `{execution_name, backend}` (`scheduler/store.py:240-247`). But that is one subcollection query **per row** — 200 rows is 200 queries per refresh. **Do not put an execution name in the table.** It belongs in the detail view, where it costs one query for one task.

**c) No filter or grouping by owner.** `submitted_by` is populated from the verified ID token (`service.py:146`) and returned, but `list_tasks` accepts only `state`, `workflow_id` and `runner_profile` (`store.py:381-399`) and **no index covers `submitted_by`** (`indexes.tf:17-225`). Client-side filtering over the loaded page is the only option today, and the control must say so — "filtering the 200 loaded agents" — because a silent client-side filter over a paged list is exactly the lie this platform keeps shipping. §8/P3.

**d) No "standalone only" server-side filter.** `task_to_firestore` writes `workflow_id` explicitly as `null` (`models.py:189-193`) and the API's `workflow_id` filter is a plain string query param — there is no way to express `== null`. Client-side over the loaded page, labelled the same way.

**e) Sorting is `created_at DESC` and nothing else.** Any column header that sorts, sorts only within the loaded page. Either label every sort control "sorts the loaded page" or offer no sort controls. I recommend the latter on phone and the former on desktop.

**f) No workflow rollup from the server, and `GET /v1/workflows` is worse than useless for status.** Covered in §2.1 under grouping: `workflow.state` is frozen at `QUEUED` from `service.py:258` forever, so the workflow list endpoint (`routes/workflows.py:27-41`) returns a list of workflows every one of which claims to be queued. The rollup is computed client-side from member tasks. The `workflows-tenant-state-created` index (`indexes.tf:218-226`) invites a state filter that would be wrong; do not build one.

#### 2.5 Refresh and cost

**Per refresh:** Live tab = 4 indexed queries, each capped at 200 documents. Recent tab = 1. All are `<equality filters> ORDER BY created_at DESC` served entirely from composite indexes — no scan, no aggregation.

**Interval:** 5s while the Live tab holds any row, 30s otherwise, and **stop entirely when `document.hidden`**. Resume with an immediate fetch plus a visible "updated just now". A background tab polling every 5s for eight hours is 5,760 requests against a 20 rps per-principal budget for nothing. On repeated failure, back off rather than hammer, and show the paused state from §1.7 rather than a spinner that never resolves.

**Rate limit:** `TokenBucketLimiter`, 20 rps sustained (`config.py:74`) with a burst of 40 (`settings.py:85`), keyed on the authenticated principal, per instance — deliberately in-process, so with N instances the effective ceiling is N × rps (`apps/swarm-api/swarm_api/ratelimit.py:1-11`, wired at `deps.py:110-113`). The Live tab's 4 requests per 5s is 0.8 rps — comfortable. **An N+1 pattern is not:** 50 parallel per-row requests exhausts the burst and earns a 429 with `Retry-After` (`main.py:99-108`). Every design in this section is built to avoid N+1 for that reason as much as for cost.

**The payload is the real problem, and it is serious.** `task_to_api` returns `input`, `metadata` and `result_summary` on **every row**. `input` is bounded only by `max_input_bytes = 256 KiB` (`config.py:72`), `metadata` by 16 KiB (`service.py:118`), and `result_summary` carries `runner.summary` (≤4000 chars), `runner.output` (≤8000 chars) and the full artifact list (`lifecycle.py:459-476`, `:895-927`). A realistic `claude-code` row is 10–20 KiB; 200 rows is **2–4 MB per refresh**. On a 5s poll that is roughly 600 KB/s to an iPhone, for a table that displays perhaps 300 bytes per row.

  There is no cheaper endpoint. `GET /v1/stats` returns counts only, and it is not cheap either (§4.4). So: **P2 in §8 is a `view=summary` query parameter on `GET /v1/tasks` that omits `input`, `metadata` and `result_summary`.** One conditional in `task_to_api`, no new index, no contract change, ~40x smaller payload. Until it exists, the phone build must cap at `limit=50` and say "showing the 50 most recent" rather than ship a 4 MB poll.

**At 100x volume:** query cost does not change — the indexes are equality-plus-ordered-field and the limit is hard. What breaks is *coverage*: a tenant with more than 200 simultaneously-live agents overflows page one of the Live tab, and because the four state queries page independently by `created_at`, merging their second pages correctly is not possible client-side. At that point the `state=in` route (P1) plus a proper compound cursor (P4) are not optimisations, they are required.

**The pagination defect that must be understood before this table ships.** `list_tasks` paginates with a strict inequality on `created_at` (`store.py:400-402`) and a token that is only a base64 ISO timestamp (`store.py:133-134`). The docstring at `store.py:26-31` dismisses ties as theoretical: *"ids are generated with microsecond-resolution timestamps and random suffixes, so that is a theoretical rather than an operational concern, and the `id` tiebreak below makes it deterministic anyway."*

  That reasoning holds for organically submitted work and does not hold for the one call this platform offers for submitting work in bulk. `POST /v1/tasks/batch` (`routes/tasks.py:50-61`) builds up to `max_batch_size = 100` tasks (`config.py:71`) in a single `submit_tasks` pass that stamps every one of them with the **same** `now`, and a workflow does the same for up to 50 steps (`service.py:200-231`). So a whole batch shares one `created_at` to the microsecond. The `id` tiebreak is applied only to the rows already in hand (`store.py:407`) and never reaches the query: the next page asks for `created_at < <that instant>` and therefore skips **every remaining row of that batch**. A 100-task batch straddling a page boundary loses the tail of the batch silently, and on the Recent tab that is the most visible list on the screen.

  Two consequences for this section, one now and one later:
  * **Now:** do not present the list as complete and do not build infinite scroll on this token. Page explicitly, and when `next_page_token` is present say "more agents — newest 200 shown". The `Live` tab is capped at 200 per state anyway.
  * **Later (P4):** the fix is a compound cursor of `(created_at, id)` with the matching `order_by("created_at").order_by("id")`, which the existing composite indexes do **not** serve — it needs `id` appended, so it is an index change as well as a Store change, and Firestore fails a near-miss index rather than degrading (`store.py:8-24`). That is why it is P4 and not a patch: it is Track A plus Track C, coordinated.

  This is a platform defect, not a UI one. It goes to Track A as a report, with the batch reasoning above, rather than being worked around in the browser.

#### 2.6 The five things this screen must never claim

Short, because each is already argued above, and each has been shipped wrong somewhere before:

1. **Never render `workflow.state`.** Frozen at `QUEUED` since `service.py:258`; compute the rollup (§2.1, §2.4(f)).
2. **Never show an empty table for a failed request.** §1.7. The likeliest first failure is a 500 from a composite index that is still building, and it looks exactly like "nothing is running".
3. **Never offer `SUBMITTED`, `QUEUED` or `DEAD_LETTERED` as a filter or a legend entry**, and never offer a Retry button. §1.1, §1.2.
4. **Never render `$0.00` for an agent with no usage data**, and never build the cost column on `GET /v1/tasks/{id}/artifacts` either — that endpoint reads a subcollection nothing writes (`store.py:501-522`) and returns `[]` for every task forever. The real artifact list is `task.result_summary.artifacts`. §2.4(a).
5. **Never let a client-side filter, sort or grouping look server-side.** Owner filter, standalone toggle, column sort and workflow grouping all operate on the loaded page only, and each says so in its own control. §2.1, §2.4(c)(d)(e).
