## Live agent output and browser-agent visuals

### Verdict, up front

The owner asked for "live logs from each pod / agent runtime AS THE AGENT WORKS", and "for agents that drive a browser, this must work visually too."

Four sentences, all unhappy:

1. **The agent's own output never leaves the pod until the attempt is over.** `procman.StreamCapture.pump` (`apps/agent-worker/agent_worker/procman.py:46-69`) drains the runner's stdout/stderr straight into files; those files reach GCS only through `_upload_outputs`, whose six call sites (`lifecycle.py:399, 442, 459, 720, 738, 993`) are all terminal. `kubectl logs` and a Cloud Logging tail show the worker's own JSON status lines and nothing else.
2. **Worse: for `claude-code` there is currently nothing to stream even if we tee'd it.** The CLI runs with `--print --output-format json` (`runners/claude_code.py:32-38`) — a single JSON document emitted at the very end. A perfect live tee of that pipe would show a blank screen for 40 minutes and then 300 KB at once. Live output requires changing the platform-set `CLAUDE_CODE_ARGS` to `stream-json` first.
3. **There is no browser-agent to watch.** The `browser` profile is a fixed Playwright action list of eight verbs (`runners/browser.py:112-158`), capped at `MAX_ACTIONS = 200` (`browser.py:36`), that never calls a model — despite the frozen catalogue declaring `provider="anthropic"` and `secrets=("ANTHROPIC_API_KEY",)` for it (`apps/common/swarm_common/profiles.py:163-173`). And `claude-code` runs on `agent-runtime-base` (`profiles.py:137-141`), which has no Chromium. "Watch a Claude agent do visual QA" describes a capability that does not exist in either profile. Any UI copy promising it is a lie regardless of how good the player component is.
4. **Nothing in this section is "zero backend work", because the API cannot be called from a browser at all today.** `create_app` (`apps/swarm-api/swarm_api/main.py:54-149`) installs exactly one middleware — `observe`, at `:75` — and four exception handlers. There is no `CORSMiddleware`; a repo-wide grep for `CORS` across `apps/` returns nothing. Auth is a Google ID token bearer against a pinned audience (`auth.py`, `GoogleTokenVerifier`, which refuses to start outside local dev without `API_AUDIENCE`). So every screen below is gated on two pieces of backend work before a single pixel renders: a CORS middleware, and a browser-obtainable ID token for that audience. This is small work and it is not optional; see prerequisite 0.

What **is** buildable on top of prerequisite 0, with no further backend work, is a genuinely good **"what is this attempt doing right now, at ~150-second resolution, and what did it produce"** screen. That is Screen 1 and Screen 2 below, and they should ship first because they are honest.

---

### Prerequisite 0 — the API is not browser-reachable yet

Both items are in `swarm-api` (Track A). Neither is a design question; both just have to exist.

| | What | Where | Why the screens die without it |
|---|---|---|---|
| 0a | `CORSMiddleware` with an explicit origin allow-list | `main.py:54-149`, next to the `observe` middleware | A cross-origin `fetch` from the UI never reaches a route handler. The failure surfaces as a network error with no HTTP status, which under the five-state contract below is `error`, not `ok-empty` — so at least it fails loudly. |
| 0b | A browser-obtainable ID token for the pinned audience | `auth.py` `GoogleTokenVerifier`; audience is this service's Cloud Run URL | Without it every call is `401`. Do **not** solve this by turning the audience check off: the docstring explains that with no audience pinned, any Google ID token from an allowed domain authenticates, including one a third-party SaaS obtained when an employee signed in with Google. |

Until 0a and 0b land, the honest status of this whole section is "designed, not buildable". Say that in the plan rather than discovering it on day one.

---

### The output topology — read this before designing any of it

Four different things get called "the logs" in this repo and they are not the same bytes. Getting these confused is how you build a screen that renders an empty box forever.

| Name | Where the bytes are | When they appear | Contains |
|---|---|---|---|
| **Worker status lines** | Container stdout → Cloud Logging, `jsonPayload.*` | Live, flushed per line (`logs.py:144-158`) | `{severity, time, component:"agent-worker", message, labels:{task_id, attempt_id, tenant_id, generation, runner_profile}}` (`logs.py:183-201`). Tens of lines per attempt-hour. **Zero bytes of agent output.** |
| **Task events** | Firestore `tasks/{task_id}/events/{event_id}` | Live, one write per event (`control.py:345-376`) | 20 typed event kinds (`swarm_common/states.py:161-181`) with a free-form `detail`. Heartbeat carries `{elapsed_seconds, peak_rss_bytes, checkpoints}`. |
| **Runner-wrapper streams** | `gs://<bucket>/tenants/<t>/tasks/<task>/attempts/<att>/logs/{stdout,stderr}.log` (`config.py:133-134`) | **Terminal only** | `stdout.log` is near-empty for `claude-code`. `stderr.log` holds the runner process's own JSON lines — **including a `"child started"` line whose `argv` contains the verbatim prompt** (`procman.py:155`; the runner's logger is `sys.stderr` per `cliagent.py:238`). |
| **The agent's actual words** | `.../artifacts/claude-code.stdout.log` and `.../artifacts/claude-transcript.json` | **Terminal only** | The CLI's own stdout (`cliagent.py:252`) and the parsed, scrubbed transcript (`cliagent.py:326-330`). `scripts/swarm.py:249-277` confirms this is where the answer lives — it reads `claude-code.stdout.log` and pulls `.result` out of it. |

Plus one summary that is reachable through the existing API **today**: `task.result_summary` (`swarm-api/codec.py:130`), built at `lifecycle.py:905-920` + `:466-477`.

#### Three traps inside `result_summary` that the UI must handle

**Trap 1 — `runner.output` is usually not a dict.** It is `_truncate_json(..., 8000)` (`lifecycle.py:470`, implementation at `:1106-1113`). For any real Claude Code run the encoded result exceeds 8000 chars, so this field is `{"truncated": true, "preview": "<first 8000 chars of the JSON encoding>"}`. A UI that reads `result_summary.runner.output.structured_output.result` gets `undefined` on every non-trivial run. Read `result_summary.runner.summary` instead (capped at 2000 chars by `cliagent._summarise` at `:379-392`, then at 4000 by `lifecycle.py:469` — 2000 is the binding cap).

**Trap 2 — `runner.usage` is empty in production. Do not build a cost panel on it.** `lifecycle.py:474` calls `_usage_summary(runner_result.get("output"))`, and `_usage_summary` (`:1055-1103`) looks for `usage`, `total_cost_usd`, `num_turns` and `modelUsage` at the top level of what it is given. But `run_runner` (`runners/base.py:229-236`) pops `status`, `summary` and `metrics` off the runner's return value and writes **the remainder** as `output`, so for `claude-code` that is `{provider, model, exit_code, structured_output, limits}` — the CLI's token numbers live one level down, inside `structured_output`. The only test (`tests/unit/worker/test_usage_summary.py`) feeds `_usage_summary` the CLI document directly, so the real wiring is untested. `docs/contract-change-requests.md:121-123` promises these numbers are kept; on the current code path `result_summary.runner.usage` is `{}`. **Report this as a worker defect (Track B) rather than designing around it, and do not ship a token/cost tile until it is fixed** — a cost panel reading `{}` and rendering `$0.00` is precisely the empty-success failure this section exists to stop.

**Trap 3 — `artifacts_skipped` is itself capped.** `lifecycle.py:916` stores `skipped[:50]`. So a run that skipped 200 artifacts shows 50 entries. The count in the UI copy must come from the phrase "at least N", not from `artifacts_skipped.length` presented as a total.

---

### Cross-cutting contract: "empty" is never the same pixel as "broken"

This platform's defining bug is an error rendered as an empty success. Every component in this section obeys the following, and it is not optional styling:

Every data-bearing region is in exactly one of **five** states, and each has a distinct, non-overlapping rendering:

| State | Rendering | Never |
|---|---|---|
| `loading` | skeleton rows at the shape of the real content, plus a live elapsed counter after 1.5s | a spinner with no shape |
| `ok-empty` | the region's own sentence, naming what was asked: *"No events yet. The task is QUEUED; nothing has been dispatched."* + the timestamp of the successful fetch | a blank div, a dash, or `0` |
| `ok-populated` | the content, plus `fetched <n>s ago` | — |
| `error` | **red-bordered** region, the HTTP status, the API's `code` and `message` fields verbatim, the failing route, and a **Retry** button | falling back to `ok-empty` |
| `stale` | last good content dimmed to 60% opacity, an amber bar pinned to the region top: *"Live updates stopped 47s ago — showing the last good read"*, and a Retry | silently continuing to show old data as if fresh |

Implementation rules that make this real rather than aspirational:

- The fetch layer returns a discriminated union `{status:'ok', data, fetchedAt} | {status:'error', httpStatus, code, message, detail?, route}`. **There is no code path that turns an error into `[]`.** A reducer that does `data?.events ?? []` is the bug; lint for `?? []` and `|| []` on API results.
- **`code` and `message` are guaranteed; `detail` is not.** `ApiError.to_payload` (`swarm-api/errors.py:24-28`) adds `detail` **only when it is truthy**, and the `AuthError` handler (`main.py:110-118`) returns `{code, message}` with no `detail` at all. Only the `ApiError` handler (`main.py:99-109`) and the validation handler (`:124-147`) can carry one. A UI that prints `error.detail` unconditionally renders `undefined` inside a red box, which teaches operators to distrust the error panel. Render `detail` only when present.
- A `200` carrying `{"events": []}` is `ok-empty`. A `403`, a `500`, a network failure (including the CORS failure in prerequisite 0a, which arrives with no status at all), a JSON parse failure and a timeout are all `error`. They are distinguishable in code, so they are distinguishable on screen.
- **The 200-event ceiling is a third state.** `GET /v1/tasks/{id}/events` orders `ASCENDING` with no cursor (`swarm-api/store.py:490-499`); omitting `limit` gives 50 and `max_page_size` is 200 (`settings.py:81-82`, clamped by `deps.paged_limit`). If `events.length === limit`, the newest events are **unreachable**, and the timeline must say so in a persistent amber banner: *"Showing the oldest 200 events. Newer events exist and cannot be fetched — this endpoint has no cursor. The header above is unaffected; it comes from the task document."* That last clause matters: without it an operator concludes the whole screen is stale. Rendering a truncated timeline as if complete is exactly the class of bug this section is meant to stop. A 7200s `claude-code` attempt (`profiles.py:151`) produces ~48 heartbeats (one per 5×30s, `lifecycle.py:93` + `config.py:65`) plus ~60 `checkpoint_started`/`checkpoint_completed` pairs = ~120 rows (`config.py:66`) plus ~8 lifecycle events ≈ **176 events for one attempt** — and `max_attempts` is 3 (`models.py:171`). This ceiling is hit in production, not in theory.
- Every region carries a `data-provenance` attribute naming its source (`firestore:tasks/{id}`, `api:/v1/tasks/{id}/events`, `gcs:.../artifacts/final.png`). A debug toggle renders it as a footnote. When a number is wrong, the first question is always "where did it come from", and the screen should answer it.
- **Never parse an id prefix.** Task ids are `task_<20 hex>` (`service.py:138` → `models.new_id`), attempts `att_`, leases `lease_`, and event ids are **`evt_` or `ev_` depending on which component wrote them** (`control.py:353` and `reconciler/store.py:255` use `evt`; `scheduler/store.py:336` and `swarm-api/store.py:352, :471` use `ev`). `docs/operations.md:161` uses `tsk_…` in its example, which is not a real id shape — do not copy it into UI placeholder text.

---

### Shared component — the Liveness badge

Referenced by both screens, so it is defined once, here.

There is **no exposed liveness signal finer than the event stream.** The lease document (`leases/{lease_id}`) is refreshed every 30s (`control.py:509-517`) and would give 30s resolution, but no route reads it. So the badge is **derived client-side from the newest event's `at`**, and it must say so on hover.

| Badge | Condition | Copy |
|---|---|---|
| `live` (green dot, pulsing) | state ∈ `LEASED/DISPATCHED/STARTING/RUNNING` and newest event < 180s old | *"Last event 42s ago"* |
| `quiet` (amber dot) | same states, newest event 180–420s old | *"No event for 4m. Heartbeat events are only every ~150s, so this is not yet alarming."* |
| `silent` (red dot) | same states, newest event > 420s old | *"No event for 11m. The worker may be gone; the reconciler reclaims a stale lease."* |
| `finished` (grey) | terminal state | *"Finished at 14:31:09"* |
| `not started` (hollow) | `SUBMITTED / QUEUED / PARKED / READY` | *"Not dispatched"* |

Hover/tap caption, always: *"Derived from the newest task event, so resolution is ~150s. This is not the lease heartbeat (30s) — that is not exposed by any route."*

**Phone:** the badge collapses to a coloured dot plus one word (`live` / `quiet` / `silent` / `done` / `queued`). The caption moves to a tap-to-reveal sheet. The dot is never the only carrier of meaning — the word is always present, because a colour-only badge fails for colour-blind operators and in a screenshot pasted into an incident channel.

---

### Screen 1 — Attempt Timeline  *(buildable once prerequisite 0 lands; ship first)*

The honest answer to "watch it work". Not a transcript; a high-fidelity status feed with real timestamps.

#### What the user sees

Header strip:
- Task id (monospace, tap to copy — the real shape is `task_…`), `runner_profile`, `resource_class`, `provider`, `model`
- **State chip**: the exact `TaskState` value — `SUBMITTED / QUEUED / PARKED / READY / LEASED / DISPATCHED / STARTING / RUNNING / SUCCEEDED / FAILED / CANCELLED / DEAD_LETTERED`. All **twelve** (`states.py:17-29`). Never a friendly synonym; operators grep for these strings. A chip map missing `SUBMITTED` renders blank for a task that was created one second ago — which is the empty-vs-broken bug in its purest form. Add a loud fallback chip (`UNKNOWN: <raw value>`, red) so a future enum member never renders as nothing.
- **Liveness badge** (shared component, above)
- `attempt_count / max_attempts` as `2 / 3`
- Elapsed: `started_at → now` while running, `started_at → completed_at` when finished
- When `park_reason` is set: a full-width amber bar with the `ParkReason` enum value (`states.py:125-137`: `PROVIDER_QUOTA_EXHAUSTED`, `PROVIDER_COOLDOWN`, `PROVIDER_OUTAGE`, `SCHEDULED_RETRY`, `DEPENDENCY_INCOMPLETE`, `MANUAL_PAUSE`, `BUDGET_EXHAUSTED`, `CREDENTIAL_MISSING`) plus `next_eligible_at` when set.
- **When `blocked_by` is non-empty: a separate full-width amber bar, and it is NOT the same thing as `park_reason`.** `record_blockers` (`scheduler/store.py:192-201`) writes `blocked_by` while deliberately leaving the task **READY** — *"The task stays READY. Parking it would be wrong — the platform being busy is not a durable condition."* It is cleared to `[]` on admission (`scheduler/store.py:223, :317`; `admission.py:237`). So **READY + blockers and no park reason is the commonest "why is nothing happening"** case, and a header that only renders `park_reason` shows that task as a bare `READY` chip with no explanation. `blocked_by` is a **list of dicts**, each `{pool, reason, limit, active}` (`admission.py:106-115`), with twelve possible `BlockedReason` values (`states.py:140-158`). Render one row per blocker: `` `tenant:eng` · TENANT_LIMIT · 4 active / 4 limit ``. Caption, from `docs/operations.md:168-170`: *"`TENANT_LIMIT` is yours to raise; `GLOBAL_CONCURRENCY_LIMIT` is the platform being full."* `MANUAL_PAUSE` gets its own copy — *"this pool was paused by an operator"* — because it is a decision, not congestion.
- `cancel_requested: true` → a red bar: *"Cancellation requested. The lease is released by the worker or the reconciler, not by the API"* — because `POST /v1/tasks/{id}/cancel` (`routes/tasks.py:107-121`) returns `released_immediately` (`:120`) and it is usually `false`.

Body: a **vertical event timeline**, newest at top, one row per event:

```
14:22:07  ●  heartbeat            elapsed 1847s · peak RSS 2.1 GiB · 15 checkpoints
14:20:11  ◆  checkpoint_completed ckpt-00015 · 84.2 MiB · seq 15
14:20:09  ◇  checkpoint_started   periodic
14:19:34  ●  heartbeat            elapsed 1694s · peak RSS 2.1 GiB · 14 checkpoints
13:51:20  ▶  running
13:51:14  ▶  starting
13:51:02  ▸  dispatched           cloud_run_job · projects/…/executions/swarm-claude-code-x7k2p
13:51:01  ▸  lease_acquired       gen 1 · lease_9f2a…
```

Each row: absolute local time + `Δ` from the previous event, a glyph keyed to event class, the `EventType` value in monospace, and a one-line rendering of `detail`. Tapping a row expands the raw `detail` JSON (the API returns it unfiltered — `routes/tasks.py:24-34`).

Rows that get special treatment because they are what people are actually looking for:
- `quota_exhausted` — amber, shows `retry_after_seconds` / `reset_at` from detail
- `generation_fenced` — red, shows `expected_generation` vs `observed_generation`; caption: *"another attempt took ownership; this worker exited without touching the lease"*
- `retrying` — shows `wait_seconds` and `attempt`
- `dead_lettered` — red; *"attempts exhausted — this one needs a human"*
- `failed` — red, with `task.last_error` inlined (scrubbed at `lifecycle.py:521`)
- `parked` — amber, shows `detail.reason` (the `ParkReason` value, written at `scheduler/store.py:214`)

Right rail (desktop) / collapsed card (phone): **Checkpoint ladder** — every `checkpoint_completed` as `seq · size · age`, and `task.latest_checkpoint` (the GCS URI) highlighted. Caption: *"Checkpoints contain `work/` only — not logs, not artifacts"* (`checkpoint.py:180`, inside `create` at `:165-211`, which archives `ws.work` and nothing else). This is the single most misunderstood thing on the platform and one line of UI copy prevents a bad 3am decision.

#### Where every value comes from

| Value | Source |
|---|---|
| everything in the header | `GET /v1/tasks/{task_id}` → `task_to_api` (`codec.py:99-131`). Firestore: one document read of `tasks/{task_id}`. |
| the event rows | `GET /v1/tasks/{task_id}/events?limit=200` → `store.list_events` (`store.py:490-499`) on `tasks/{task_id}/events`, `order_by("at", ASCENDING)` |
| `execution_name` on the `dispatched` row | that event's `detail.execution_name`, written by `apps/scheduler/scheduler/store.py:241-248`. **Use this copy, not the attempt document's.** The dispatcher writes it in two places, and `control.record_attempt_start` (`control.py:425-457`) then does a non-merge `.set()` that clobbers the attempt document's copy with an env-derived one from `_execution_name()` (`lifecycle.py:1037-1042`, called at `:198-199`). That env value is whichever of `CLOUD_RUN_EXECUTION / K_REVISION / JOB_NAME / HOSTNAME` is set first: on Cloud Run it is the bare execution name rather than the dispatcher's full `projects/…/executions/…` (`dispatch.py:431-434`), and on GKE it falls all the way through to `HOSTNAME`, i.e. the pod name, because the GKE container env is `worker_env` (`dispatch.py:194-220`, applied at `:525-526`) which sets no `JOB_NAME`. The event's copy is stable because nothing overwrites it. |
| | Also render `…/executions/pending-<attempt_id>` as-is when it appears — the dispatcher writes that placeholder when the operation metadata carries no name (`dispatch.py:432-433`). It is a real value, not a bug, and hiding it would make the row look empty. |
| `Δ` between rows | client-side from `event.at` |
| peak RSS | `heartbeat.detail.peak_rss_bytes` (`lifecycle.py:787-796`) |
| elapsed | `heartbeat.detail.elapsed_seconds`, or `now − task.started_at` between heartbeats |

#### What it cannot show yet

- **Nothing the agent said.** Not one byte. The densest live signal available is `elapsed_seconds` ticking and the checkpoint counter incrementing. Label the panel honestly: **"Attempt activity"**, never "Live log".
- **Liveness finer than ~150 seconds.** See the Liveness badge above. Prerequisite: an attempt/lease read route.
- **Per-attempt separation.** Events carry `attempt_id`, so the UI can group them — but there is no `/attempts` route (the full route set is `health`, `platform`, `admin`, `tasks`, `tenants`, `workflows`, and `codec.attempt_from_dict` at `swarm-api/codec.py:159` is defined and never called), so `exit_code`, `peak_disk_bytes` and `oom_near_miss` for attempt 1 of 3 are unreachable. `task.result_summary` only ever reflects the **last** attempt.
- **Anything past event #200.** See the ceiling banner above.
- **A working "copy the gcloud command" affordance.** If you add one, do **not** copy the command in `docs/operations.md:179-183`: it filters on `labels.swarm_task_id`, a log-entry label nothing in this repository sets (the worker writes `jsonPayload.labels.task_id`, `logs.py:151-152` + `:194`), so it returns nothing. The working shape is `resource.type=("cloud_run_job" OR "k8s_container") AND jsonPayload.labels.attempt_id="att_…"` plus a timestamp window from `started_at`. That doc line is Track D; report it rather than editing it.

#### Refresh and cost

- Two reads per poll minimum: `get_task` (1 document) then the events query (N documents) — `list_events` calls `get_task` first for the tenant check (`store.py:491`), so a header + timeline refresh is `1 + 1 + N`, never `1 + N`.
- **Poll every 10s while the task is in `LEASED/DISPATCHED/STARTING/RUNNING`. Stop polling entirely at a terminal state** and swap to a single "refresh" affordance. A 5s poll buys nothing: events arrive at 120–150s intervals.
- Cost arithmetic, with the assumptions stated so they can be re-checked: one open tab, 10s polling = 360 polls/hour. Early in an attempt (N≈50) that is ~18,700 document reads/hour; late in a 7200s attempt (N≈176) it is ~64,000/hour. **At roughly $0.03 per 100k reads that is $0.006–$0.019 per hour per open tab — verify current Firestore pricing before quoting either number to anyone.**
- **What breaks at 100×:** the whole-page re-read. Today the UI re-fetches every event it already has, every 10 seconds, forever. With 100 engineers each watching a late-stage task, that is ~6.4M reads/hour ≈ **$1.9/hour, ~$1,400/month, to display data that has not changed**. The `since` cursor (prerequisite 1) turns steady state into `1 + 1 + (0–2 new)` reads and cuts this by well over 95%. **Build the cursor before the screen ships to more than a handful of people.** Do not reach for a Firestore `onSnapshot` listener from the browser as the fix: Firestore has no document-level IAM (stated at `checkpoint.py:25-27`) and the tenant boundary is enforced in code by `store.get_task`, so a direct browser listener bypasses invariant 9 outright.

#### Phone layout (390pt)

- Header collapses to two lines: line 1 = state chip + liveness badge + elapsed; line 2 = profile · attempt `2/3`. Task id moves behind a tap on the header. `park_reason`, `blocked_by` and `cancel_requested` bars stay full-width and visible — they are the reason someone opened the page on a phone. When there are more than two blockers, show the first two and `+3 more` as a tap target; never a horizontal scroll.
- The timeline is a single column. Each row is two lines: `HH:MM:SS  event_type` / `detail summary, one line, ellipsised`. **No horizontal scroll, ever.** The `Δ` column is dropped; it reappears as a subtitle on tap.
- The checkpoint ladder becomes a collapsed accordion showing only `15 checkpoints · latest 84 MiB · 2m ago`.
- Filter chips (`all / lifecycle / heartbeats / checkpoints / problems`) pinned under the header. **Default on phone: `lifecycle + problems`**, which hides the ~168 heartbeat and checkpoint rows (48 heartbeats + ~120 checkpoint rows) that otherwise make the screen unreadable. Each chip shows its hidden count: `heartbeats (48)`, `checkpoints (120)`.
- The 200-event ceiling banner is not dismissible on phone. Truncation that the user cannot see is the bug.

#### Empty / loading / broken

- **Loading**: eight skeleton rows.
- **`ok-empty`, task exists, no events**: *"No events recorded. Task state is QUEUED — nothing has been dispatched yet."* Not a blank list.
- **404 on the task**: render the API's `message` verbatim, then the caption *"No task `task_…` is visible to tenant `eng`."* — and say the tenant, because the most common cause is looking at someone else's task id, which is invariant 9 working correctly, not a bug.
- **403**: the auth error surfaced verbatim; do not retry-loop a 403.
- **Network error with no status** (the CORS case, and the offline case): `error`, with the copy *"The API could not be reached — no HTTP response. If this is every route, it is CORS or the token, not this task."* Never `ok-empty`.
- **Task fetch OK but events fetch fails**: header renders `ok`, timeline renders `error`. **They are separate regions with separate states.** A partial failure must not blank the half that worked, and must not let the half that worked imply the other half is empty.

---

### Screen 2 — Run Output  *(metadata pane real today; file panes dark until a read route exists)*

#### What the user sees

Three panes, and the split is deliberate: what we can prove today, versus what needs a new endpoint.

**Pane A — Result (real today, once the attempt is terminal).** From `task.result_summary`:
- `exit_code` (large, colour-keyed). The values are defined in two places and both matter: the runner's own exits at `runners/base.py:38-44` — `0` green, `1` red, `77` amber *"provider quota exhausted"*, `143` grey *"terminated on SIGTERM"* — and the worker's at `errors.py:11-44`: `69` *"a dependency was unavailable before the runner — retried"*, `70` *"generation fenced — the agent was NOT run"*, `71` *"cancelled"*, `75` *"parked on a long provider wait"*, `76` *"timeout, child killed"*, `78` *"worker could not start — not retried"*, `79` *"tenant mismatch"*. Anything unmapped renders red with the raw number, never blank.
- `duration_seconds`
- **Agent's reply**: `result_summary.runner.summary` — up to 2000 characters, already scrubbed. Rendered as prose in a bordered block headed *"Agent's final reply (first 2000 characters)"*. This is the single most valuable thing on the screen and it is available right now. When the value is the literal `"agent produced no textual output"` (`cliagent.py:392`), render that as an `ok-empty` sentence, not as the agent's words.
- `result_summary.runner.status` and `result_summary.runner.metrics` (`duration_seconds`, `stdout_bytes`, `stderr_bytes` — `cliagent.py:352-356`)
- **Raw result preview**: `result_summary.runner.output`. If `output.truncated === true`, render `output.preview` in a monospace block headed *"Raw runner result, truncated at 8000 characters"* — with a note that the full document is in `artifacts/claude-transcript.json`. If it is a dict (short runs only), render it as a tree.
- **No token/cost tile.** See Trap 2. `result_summary.runner.usage` is `{}` on the current code path. Add the tile in the same change that fixes the worker, not before.
- **Artifact table**: one row per `result_summary.artifacts[]` entry — `name`, `bytes` (humanised), and the `gs://` URI with a copy button. Plus `artifact_bytes` total against the 512 MiB cap (`config.py:79`), and, when `artifacts_skipped` is non-empty, a **red** row: *"At least N artifacts were not uploaded — the 512 MiB cap was reached"* (`lifecycle.py:906-909`, `:916`), with "at least" because the list is itself capped at 50. Silently omitting skipped artifacts is exactly the empty-success failure.
- **Log URIs**: `result_summary.logs.stdout` / `.stderr` as `gs://` strings with copy buttons.
- **Checkpoint**: `result_summary.checkpoint.{checkpoint_id, uri, bytes}` and, when the attempt resumed, `restored_from.{checkpoint_id, attempt_id}` rendered as *"resumed from attempt att_… checkpoint ckpt-00009"*.

**Pane B — Files (dark today).** Rows are clickable only once a byte-proxy route exists. Until then each row shows the `gs://` URI, a copy button, and a single explanatory line: *"Opening files in the browser needs an artifact read route in swarm-api. `swarm-api` holds no GCS credentials today — read on `tenants/<tenant>/` is granted by IAM condition to the tenant's own worker service account (`terraform/modules/tenancy/main.tf:194-214`), not to a person and not to the API."* Do not render a disabled download button with no explanation; an operator will file a bug against the wrong component.

**Pane C — Browser evidence (dark today, and it is the closest thing to what the owner asked for).** The `browser` profile is not live-watchable, but it does leave real visual evidence behind, and the section would be dropping the owner's request if it did not name it:

| Artifact | Written at | What it is |
|---|---|---|
| `screenshot-NNN.png` / `<custom>.png` | `browser.py:145-149` | one per explicit `screenshot` action |
| `final.png` | `browser.py:164-167` | always, unless `input.screenshot === false` |
| `page.txt` | `browser.py:161-163` | final `body` inner text, unless `input.extract_text === false` |
| `console-errors.json` | `browser.py:173-174` | `msg.type === "error"` console messages only, each truncated to 500 chars (`browser.py:105-110`) |

These arrive in `result_summary.artifacts[]` like everything else, so the **table** is real today; only the **pixels** are dark. Design Pane C as a thumbnail grid with a per-tile dark state and one sentence — *"Screenshots exist in GCS and cannot be rendered here until the artifact read route exists"* — plus the action index each shot came from, taken from `runner.output.actions[].artifact` when the result was small enough not to be truncated. A post-run screenshot strip is the honest, shippable subset of "this must work visually too", and it should be named as such rather than left to be inferred from a generic artifact list.

Pane C copy must also carry the caveat in the read-route section below: **a PNG is never redacted.**

#### Where every value comes from

- Pane A: entirely `GET /v1/tasks/{task_id}` → `task.result_summary` (`codec.py:130`). **One document read.** Nothing else.
- Pane B and Pane C rows: the same `result_summary.artifacts[]` and `result_summary.logs`.
- **Do not use `GET /v1/tasks/{id}/artifacts`.** It queries `tasks/{task_id}/artifacts` (`store.py:501-522`), a subcollection **no component writes** — the worker puts the index in `result_summary.artifacts` instead (`lifecycle.py:905-907`). The endpoint is live, documented at `docs/operations.md:165`, and returns `[]` for every task that has ever run. A screen built on it renders a permanent empty state that looks like a successful "no artifacts". This is the trap list working as intended: **build on `result_summary`.**

#### What it cannot show yet, exactly

- **Any file's contents.** No signed-URL code exists anywhere in `apps/`, and `store.py:502-508` states the design in as many words: *"this endpoint never mints a download URL — the caller reads GCS with their own credentials, which keeps the tenant boundary in one place."*
- **Anything at all, while the attempt is still running.** `result_summary` is written only on the terminal paths, so a `RUNNING` task has `result_summary: null`. Every pane on this screen therefore has a *running* `ok-empty` state distinct from its *finished-but-empty* state: *"This attempt is still running. Results, logs and artifacts are written when it ends."* A UI that renders `null` as an empty artifact table tells an operator that a working run produced nothing.
- **A "just open it in the Cloud Console" link is not a workaround.** Terraform grants no human or group read on the artifact bucket (the only `google_storage_bucket_iam_member` on it is the per-tenant worker GSA condition at `terraform/modules/tenancy/main.tf:194-214`, plus a Google-owned log writer on the *access-log* bucket at `terraform/modules/storage/main.tf:156-162`). A console deep link 403s for anyone without project-wide storage read — i.e. for every ordinary engineer. Offer the link labelled *"opens in Cloud Console — requires project storage read"*, and do not present it as the answer.
- **Per-attempt files.** `result_summary` is the last attempt's. Attempt 1's stdout exists in GCS at a deterministic path but nothing enumerates it.
- **Anything older than 90 days**, and anything older than 14 days costs a Nearline retrieval fee (`terraform/modules/storage/variables.tf:25-33` `artifact_retention_days` default 90, `:49-53` `nearline_after_days` default 14). A UI that offers a file list for a 6-month-old task must say *"artifacts expire after 90 days"* rather than 404ing into an empty state.

#### Empty / loading / broken

Screen 2 gets the same five states as Screen 1, per-pane, because a partial failure here is common: the task document can load while the render of any given pane has nothing to show.

- **Loading**: Pane A as three skeleton blocks (exit code, reply, table); Panes B and C as skeleton rows.
- **`ok-empty`, running**: the sentence above, in every pane.
- **`ok-empty`, terminal, genuinely nothing**: *"This attempt finished with exit code 78 and produced no artifacts."* — name the exit code in the sentence, because "no artifacts" plus a non-zero exit is a different story from "no artifacts" plus a zero.
- **`error`**: one red-bordered pane, not a blank screen. Pane A failing does not blank Panes B and C — they read the same document, so in practice they fail together, and the copy says so once at the top rather than three times.
- **404 / 403**: as Screen 1.

#### Refresh and cost

One document read, on demand. Poll only while running (10s, shared with Screen 1 — same document, so fold them into one fetch). Once the byte proxy exists: one GCS class-B read plus egress per file opened, on click only. **Never prefetch file contents for a list** — a 14-row artifact table that prefetches is 14 GCS reads per render, and at 100× that is the line item someone asks about. Thumbnails in Pane C are the obvious temptation here: a grid of 20 screenshots that auto-loads is 20 proxied GCS reads per page view. Load thumbnails on click, or behind an explicit "load previews" toggle that names the cost.

#### Phone layout (390pt)

- Pane A first, then Pane C, then Pane B. `exit_code` + duration as one line of large type at the top, with the exit-code caption on the line beneath it — the number alone means nothing on a phone.
- The agent's reply block is the hero: full width, generous line-height, `max-height: 60vh` with an expand toggle.
- The raw-result preview is collapsed by default behind `Raw runner result (8 KB, truncated)`. It is monospace and it is the one block allowed to scroll horizontally *within itself*, never the page.
- The artifact table becomes cards: `name` on line 1, `bytes · [copy gs:// URI]` on line 2. **Drop the URI text itself from the card** — it is 120 characters of unwrappable `gs://` path that will either overflow the viewport or shrink the type to nothing. The copy button carries it, and a tap on the card reveals it in a full-width sheet with a select-all affordance.
- Pane C becomes a horizontally swipeable strip of fixed-size tiles (the one horizontal scroll on the page, and it is a deliberate carousel, not an overflow), each tile captioned with its artifact name. In the dark state each tile is a grey rectangle with the file name and a single shared explanatory line beneath the strip, not repeated per tile.
- `restored_from` and the checkpoint block collapse into one line: `resumed from ckpt-00009 · 84 MiB` with the attempt id behind a tap.
- The log URIs become two buttons, `copy stdout URI` / `copy stderr URI`, with no path text.
- Error and stale bars are full-width and pinned above Pane A, identical to Screen 1.

---

### The read route, when it is built — and its two real consequences

Recommended shape: `GET /v1/tasks/{task_id}/attempts/{attempt_id}/files/{path}`, tenant-checked by the same `store.get_task` call every other read uses, streaming bytes through `swarm-api` with `Content-Disposition`, a hard response cap (suggest 8 MiB inline, larger files refused with a message naming the `gs://` URI), and an access log line per read.

**Consequence 1 — it moves the tenant boundary.** Name it out loud in the design doc and in the PR: this gives `swarm-api`'s service account read across `tenants/*`, an IAM surface that by design no single identity holds today. It relocates the tenant boundary from a GCS IAM condition (`terraform/modules/tenancy/main.tf:194-214`) into `swarm-api`'s request path. That is a deliberate reversal of a stated design decision, not a plumbing detail, and it is the owner's call (see Decisions, D).

**Consequence 2 — it changes who sees unredacted bytes, and the redaction net is much smaller than it looks.** Today these files are read only by someone who already holds GCS read on the tenant prefix. Put them behind a browser and that changes. What actually gets scrubbed:

- Redaction is **literal-value substring replacement only** (`redact.py:42-52`), with a floor of `MIN_SECRET_LENGTH = 8`. There is **no pattern matching** in the platform.
- The registered set is tiny and closed: the tenant's own provider credential (`secrets.py:189-190`), the git clone token (`secrets.py:237`), and the credential env plus `HTTPS_PROXY`/`HTTP_PROXY` re-registered inside the runner process (`cliagent.py:243-245`). A credential the agent read out of the cloned repo, or a metadata-server token, or a password typed into a browser `fill` action, is registered by nothing and scrubbed by nothing.
- **A PNG is never scrubbed at all.** `scrub_file` (`redact.py:55-80`) returns `False` for anything it cannot `read_text()` as UTF-8, by design — *"corrupting an artifact to protect a key that is probably not in it is the wrong trade"* (`logs.py:115-122`). So a screenshot of a page showing a token is stored verbatim and would be served verbatim by Pane C.
- The pattern net that operators rely on — `sk-*`, `ya29.*`, JWTs, `AIza*`, `gh[pousr]_*`, `AKIA*`, PEM blocks, `Bearer` headers — lives in `scripts/lib/redact-stream.sh` / `scripts/lib/common.sh:816-829` and is a **terminal pipe, not a platform control**. A web UI gets none of it for free.

So the read route's PR must state which of these it accepts and which it fixes. Shipping Pane C without deciding is shipping an unredacted screenshot viewer.

**Prefer the byte proxy over a signed URL**: a signed URL leaves the audit trail, cannot be revoked once issued, and survives being pasted into Slack. The proxy costs egress and a few hundred ms; it keeps every read attributable to a verified principal.

---

### Prerequisites, in the order they unblock things

| # | Prerequisite | Unblocks | Size |
|---|---|---|---|
| 0a | `CORSMiddleware` on `swarm-api` | literally every screen here | small |
| 0b | browser-obtainable ID token for the pinned audience | same | small–medium; the token story is the real work |
| 1 | `since` cursor (or `DESC` ordering) on `GET /v1/tasks/{id}/events` | the 200-event ceiling, and the 100× cost cliff | small |
| 2 | an attempt/lease read route | per-attempt `exit_code`/`peak_disk_bytes`/`oom_near_miss`; 30s liveness | small |
| 3 | the artifact byte proxy | Pane B and Pane C pixels | medium — see the two consequences above |
| 4 | worker fix: `_usage_summary` reading `output["structured_output"]` | the token/cost tile | small, Track B |
| 5 | `CLAUDE_CODE_ARGS` → `stream-json`, plus a streaming tee and a streaming-safe scrubber | any live transcript at all | large, and item 5's scrubber gates it |

Items 4 and 5 and the `docs/operations.md` filter defect are other tracks' code. They are reported here, not changed here.

---

### Decisions this section cannot make

These are the owner's, and each one changes what gets built:

- **A.** Do prerequisites 0a/0b ship as part of this UI work, or as a separate API change first? Nothing here renders until they do.
- **B.** Does Pane C ship dark (rows + explanation, no pixels), or does the artifact byte proxy come first so it ships with images?
- **C.** Is the unredacted-PNG exposure acceptable for Pane C behind a verified principal and an access log, or does a screenshot scrubber gate it?
- **D.** Is relocating the tenant boundary from a GCS IAM condition into `swarm-api`'s request path acceptable at all? If not, Panes B and C never get pixels and should be designed as permanent copy-a-URI panes rather than as temporarily dark ones.
- **E.** Is "watch a Claude agent drive a browser" a product commitment? If yes it is a new runner (a model with browser tools), not a UI task — and the UI should not carry copy implying it until that exists.
