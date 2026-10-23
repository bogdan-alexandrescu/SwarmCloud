# One visual-QA dispatch over an app's environments, discovering as it captures

**Status: DESIGN, not built.** Lane QA-ENVIRONMENTS-DESIGN, 2026-10-23, part
of #2136. Nothing here changes behaviour. The build lanes of §8 start only
after the owner answers §9; the pull request carrying this file is not
merged until then.

**The request (BGD, 2026-10-23 ~17:40Z, #2136):** dispatch one visual-QA
workflow from the console against an app such as `https://studio.saga.xyz`
and have it QA both legs of that app, production and staging, in the same
dispatch. It signs in to each. It runs discovery **while** it does QA, not
as a step that must finish first, because the UI changes all the time and a
run over a stored, stale route list misses new routes, sections and
features. BGD chose (~17:55Z) a design first, then his answers, then build
lanes.

**What this design builds on, and does not reopen.** Everything in
[visual-qa-any-app.md](visual-qa-any-app.md) stands unless a question in §9
asks the owner to change it:

* **§3, the gate's safety rules, are the owner's.** Read-only by default on
  every target; a write to another origin is refused always; only a
  `non_production` target may run a `mutating: true` flow, and only in that
  flow's step. This design adds **no** write path. A production leg is
  read-only (§6).
* **VQ-Q1:** any platform admin approves a host, audited.
* **VQ-Q2:** one QA secret per tenant, `swarm-tenant-<tenant>-qa`, a JSON map
  read and written by swarm-api alone. This design keeps one secret per
  tenant and adds **no** IAM binding and **no** secret slot (§1.4).
* **VQ-Q3:** an issue shows its screenshot by console link by default.
* **§2.4:** MFA and CAPTCHA are refused, never solved, never handed to a
  person. §2.5's owner-configured bypass is the only way past a challenge.
* **The frozen contract:** `Workflow.steps` is fixed at submission
  (`apps/common/swarm_common/models.py` `Workflow`). The recommended options
  below need **no** frozen-contract change; the one option that would is
  written as a request (§3.6), not filed.

---

## 0. What exists, read on main (`3fff96e`), 2026-10-23

| piece | today | where |
|---|---|---|
| a target's origin | **one**: one `base_url`, one `sign_in` (with `start_clicks` and an optional `extra_origin`), one `environment` mark (`production`, the default, or `non_production`), one `host_state` and `approved_origins` | `apps/swarm-api/swarm_api/qatargets.py` `SPEC_FIELDS`, `_sign_in`, `approve_host`; `qaspec.py` `_environment` |
| the credential | the tenant's one QA secret, keyed by **target id**: `values[target_id] = {username, password, challenge_bypass?}` | `qatargets.py` `set_credential`, `set_challenge_bypass`, `QaTargetIssuer._entry` |
| the mark | `metadata.qa_target = {id, revision}`, one value for every browser step of a submission; a caller's mark is refused, and `validation.qa_target_mark_allowed(mark)` admits exactly one value for the one submission inside it | `qaspec.qa_target_metadata`, `validation.py` `qa_target_mark_allowed`, `QA_TARGET_METADATA_KEY` |
| the run record | `qa_target_runs/{workflow_id}`: one `target_id`, one `revision`, the browser `task_ids`, `kind` (`run` or `discovery`) | `QaTargets.record_run` |
| the worker's route | serves the target to an agent only when the run record names that agent for that target in the lease's tenant, the host approved, the revision unchanged; `mutating` true only for a declared `flow-<id>` step on a `non_production` target | `QaTargetIssuer.issue`, `_run_names`, `_mutating` |
| the gate | holds **one target per attempt**: a second `qa_sign_in` naming another `target_id` is refused `target_changed`; writes to the target's origins refused unless signing in, `read_posts`, or a mutating flow; anything but `GET`/`HEAD` to any other origin refused always | `apps/agent-worker/agent_worker/livegate/qatarget.py` `QaTarget.handle`, `judge` |
| `:run` | refuses `qa_target_no_screens` until `screens` or a stored `discovered` list exists; renders each capture step's route list into its prompt | `QaTargets.runnable`, `qaspec.render`, `_capture_prompt` |
| discovery | a separate one-step workflow (`:discover`), one at a time per target; `swarm_qa discover` follows the rendered `a[href]` links only, breadth first, at most 60 pages and depth 3, and is **not** passed `--hash-routes` | `qaspec.render_discovery`, `images/agent-runtime-browser/swarm_qa.py` `discover`, `LINKS_JS` |
| capture | captures exactly the routes its prompt lists; adds none it finds | `swarm_qa.py` `capture` |
| the schedule | `visual-qa` takes one `target`; per-run $40, per-day $80, `max_concurrent` 1 (VQ7-CAPS) | `schedtypes/visual_qa.py` `BUDGET_CAPS`, `scheduletypes.py` `VisualQaParams` |
| the console | one Run per target card, "report only" and "Flows with" beside it | `apps/swarm-ui/src/QaTargets.tsx`, `qaTargetsApi.ts` `runQaTarget`, `cannotRunBecause` |
| AI Studio | two targets, `ai-studio-staging-routes` and `ai-studio-staging-crawl`, both staging; the production leg has **no** target and its sign-in host is **not recorded** | #2136 |

**One fact about AI Studio shapes the safety argument.** Its base URL opens
on a chooser with a **Staging | Production** toggle (visual-qa-any-app.md
§2.2, start clicks). If both legs are served from the same origin
(`https://studio.saga.xyz`) and differ only by the toggle, the gate cannot
tell the legs apart by origin. §1.5 and §6 make that case safe by rule
rather than by hope.

---

## 1. One app, several environments

### 1.1 The options

**(a) One target with an `environments[]` list (recommended).** What is the
app's stays on the target once; what is an environment's moves into an
entry:

| on the target (the app's, shared by every environment) | in each `environments[]` entry (one leg) |
|---|---|
| `name`, `repository`, `authorized_by`/`attested_at`, `screens`, `discover`, `flows`, `flows_browser_tool`, `widths`, `themes`, `exclusions`, `design_docs`, `file`, `clock` | `name` (a short label, `[a-z0-9-]{1,20}`, unique in the target: `staging`, `production`), `base_url` (and so `host`, `origin`), `environment` mark (`production` \| `non_production`), `sign_in` (`login_path`, selectors, `signed_in_selector`, `start_clicks`, `extra_origin`), `credential` status, `challenge_bypass` spec and `challenge_bypass_value` status, `host_state`/`approved_origins`/`approved_by`/`approved_at`, `read_posts`, `rate` (optional; slower only, the 1 s floor held), `discovered` and `discovering` (§5) |

Why this split: the routes, flows, exclusions and filing rules describe the
**app**, and a route found on staging is a route production will have once it
ships; keeping them twice is how two copies drift. The sign-in, credential,
host and write rule describe **where** the app is served and are exactly the
things that must never cross from one leg to the other, so they are per
entry and every rule that guards them today is applied per entry.

**(b) Two targets linked as one app.** Each leg stays a target as today; a new
`app` label (or a `qa_apps/{app_id}` document listing target ids) groups
them. Nothing the gate, the worker route, the secret or the run record
holds changes, because a leg *is* a target. The cost is everything shared
twice: `screens`, `flows`, `exclusions`, `file.label_rules` and promotions
must be edited on both, and a route discovered on one is not the other's.

**(c) No model change; "run both" over two picked targets.** The console and
`:run` take a list of target ids and submit one run each, linked as a group
(§2). Cheapest to build; there is no app, so nothing knows two targets are
one product, the schedule names a list, and the duplication of (b) remains.

### 1.2 Migrating today's stored targets

* **(a):** one lane rewrites every `qa_targets` document in a transaction:
  the per-leg fields move into `environments: [{name, ...}]` with `name`
  `production` when the old `environment` was `production` and `staging`
  otherwise, and `revision` is bumped (so a run started before the rewrite
  is refused `the QA target changed since this run started` by the worker
  route, as any edit is). Each rewrite appends an `admin_audit` row
  `qa_target_migrated`. Until the rewrite has run everywhere, the reader
  (`QaTargets.find`) presents an unmigrated document as a one-entry list, so
  nothing reads two shapes past the read. AI Studio's two staging targets
  each become a one-environment target; BGD then adds a `production` entry
  to the one he keeps (and may delete the other; its `screens` are a
  `screens` list to copy, not a different app).
* **(b):** none for existing targets; a person links them.
* **(c):** none.

### 1.3 The host approval, per environment

`:approve-host` gains a required `environment` (the entry's `name`) under
(a); the admin types that entry's host, and its `extra_origin` when declared,
exactly as today, and the audit row names the entry. Changing an entry's
host or `extra_origin` sets **that entry** back to `pending` and removes
**that entry's** bypass value (the rule of visual-qa-any-app.md §2.5, applied
per entry). Two entries on the same host are two approvals: the approval is
of a leg (its origins, its write rule), not of a hostname. Under (b) and (c)
nothing changes.

### 1.4 The credential secret: still one secret per tenant, no IAM change

Under (a) the tenant's one QA secret stays one secret, read and written by
swarm-api alone (VQ-Q2's V-1 and V-2, unchanged). Only the map's key
changes: an entry is `values["<target_id>/<environment name>"]`.

* **No secret migration.** The migration of §1.2 records
  on the one entry it creates that its map key is still the bare
  `<target_id>` (a `legacy_map_key` flag), and the reader
  uses that key for that entry until the next credential or bypass write for
  it, which writes the new key and deletes the old one in the same new
  version. Nothing rewrites a secret in bulk.
* **The one-time link binds the entry.** `POST .../credential-links` and
  `PUT .../credential` take the `environment` name; the link document
  records it and `consume_link` compares it with the email, tenant and
  target, so a link minted for staging can never set production's password.
* **Size.** A Secret Manager payload is at most 64 KiB. An entry at today's
  maxima (256-character username, 1,024-character password, 1,024-character
  bypass value) is about 2.4 KiB, so one tenant holds about 25 maximal legs;
  real test accounts are a small fraction of that. The registration refuses
  a new entry when the map would pass 56 KiB (an 8 KiB margin), with a
  sentence, rather than failing at write.
* **Delete.** Deleting an entry removes its key; deleting the target removes
  every `<target_id>/` key and the legacy key, as today's delete removes
  `values[target_id]`.

Under (b) and (c) the secret is unchanged: each leg is a target with its own
key.

### 1.5 What the gate holds per attempt

**One leg, always.** Under every option an attempt is one leg's: its mark
names the target **and the leg** (`{id, revision, environment}` under (a);
the target id alone under (b) and (c), where a target is a leg). The worker
route answers that leg's `base_url`, `origins`, `environment`, `sign_in`,
credential, bypass, `read_posts` and `rate`, and nothing of any other leg.
The gate's hand-off shape does not change: it already receives one origin
set and one credential. Under (a) the gate's `target_changed` refusal
compares `(target_id, environment name)`, so a second hand-off in one
attempt can never switch legs; the worker route never offers one anyway,
because the mark is read from the task document swarm-api wrote.

**Two legs on one origin.** When two entries of one target share any
approved origin (AI Studio's case if both are `https://studio.saga.xyz`),
the worker route treats **both** as production for the write rule:
`mutating` is false for every step of either leg, whatever the entry's mark
says. The page's toggle, not the origin, chooses which backend a write
would reach, and an agent on the staging leg that clicks "Production" on
the chooser must not be holding a write permission. Writes to another
origin (a separate staging API host) are refused always, as today. This is
a narrowing of §3, never a widening: it only ever turns a write permission
off.

---

## 2. One dispatch, both legs

### 2.1 The options

**(a) One route, one workflow per leg, linked by a run group (recommended).**
`POST /v1/qa-targets/{id}:run` takes `environments: ["staging",
"production"]` (names only; default: every entry). swarm-api checks every
named leg with `runnable` **before submitting any**, so a pending host or a
missing credential on one leg refuses the whole dispatch with that leg
named, and nothing spends. Then it renders and submits one workflow per
leg, each inside its own `qa_target_mark_allowed(mark)` with that leg's one
mark, and writes one `qa_target_runs/{workflow_id}` row per leg carrying
`environment` and a shared `group_id` (`qrg_` + 16 hex), plus one
`qa_run_groups/{group_id}` document: `{tenant_id, target_id, revision,
legs: [{environment, workflow_id}], by, at, file, browser_tool}`. The
answer is `{group_id, legs: [{environment, workflow_id, browser_steps}]}`.

What does not change, and why that is the point: the single-mark rule
(`validation.qa_target_mark_allowed`, one value per submission), the
worker route's `_run_names` check (it adds `run.environment ==
mark.environment`), the gate's one-target-per-attempt rule, `on_step_failure:
continue` within a leg, the report's reviewed-count check, and the filing
step. A leg that fails costs the other nothing; each leg is a workflow the
console already knows how to show.

A crash between two submissions leaves the first leg running and the second
never submitted. The group document is written **before** the first
submission with each leg `pending`; each leg's `workflow_id` is filled after
its submission; the run page shows a leg still `pending` after its
neighbour started as "not started: <why>", and the dispatch answers 207
when a later submission is refused. (The same crash window `:run` has today
between submission and `record_run`, visual_qa.py's "WHAT IS NOT CLOSED
HERE", is unchanged.)

**(b) One workflow whose steps each carry their own leg's mark.** One
submission holds both legs' steps (`capture-staging-g1`, `capture-production-g1`,
...), and one report could compare them. What changes:

* the single-mark rule: `qa_target_mark_allowed` takes a map `step_id ->
  mark`, and `validation` admits a step's mark only when it equals the value
  swarm-api rendered **for that step id**, so a step can carry neither no
  mark nor the other leg's;
* the run record names `task_ids` per leg, and `_run_names` admits an agent
  only under its own leg's list;
* the gate is unchanged (an attempt is still one agent, one leg);
* the report step stages both legs' tars and the filing step files for both.

How a step never reaches the other leg's credential or origin under (b): the
mark is per task document, written by swarm-api; the worker route reads the
agent's own mark and checks the run record's per-leg agent list; the answer
carries one leg; the gate refuses a change of leg. Those are the same three
locks (a) uses, so (b) is not less safe, but it changes a validation rule
that today has one exact value for a reason (VQ2-RESERVE), doubles one
workflow's step count (up to 2 x (8 capture + 8 review + 10 flow) + 3), and
puts both legs under one per-workflow cost tripwire.

### 2.2 Cost, and the caps

A leg costs what a run costs today plus its crawl (§3.1). The only
measured full run is the console's run 5b (`wf_025ae735511546d99eff`: 17
steps, 362 screenshots, **$23.62**, schedtypes/visual_qa.py); two legs of an
app that size are about **$47 before the crawl**. The crawl's own cost is
not measured: it is haiku time spent waiting on navigations
(`swarm_qa`'s loop, not the model, walks the pages), and the first build
lane measures it on AI Studio staging before the schedule's caps are
changed.

* **Started by hand** (`:run`): no dollar budget applies, as for all work
  started by hand (schedules.py, "BUDGETS"), so two legs are simply two
  workflows a member started.
* **The `visual-qa` schedule:** `per_run_usd` is a tripwire per **work
  item** and `max_concurrent` counts live work items
  (`schedules.reserved_usd`, `concurrency_room`). Under (a) each leg is a
  work item: $40 bounds each leg, and today's `max_concurrent: 1` would
  leave a firing room for one leg only. Under (b) one workflow holds both
  legs and $40 bounds both, which run 5b's figure says is too little for an
  app of the console's size. §9 Q9 asks which caps apply.

---

## 3. Discovery during QA, not before it

### 3.1 (a) Every capture step crawls, and owns a share of what it finds (recommended)

**What a capture step does.** It captures its stored routes exactly as
today, and **harvests the links on every page it loads while capturing**
(the page is already loaded; reading `a[href]` costs no navigation). Then it
continues a bounded crawl, breadth first, from the leg's base path and from
every stored route the run lists (all groups', not only its own, so every
step walks the same graph), and captures the **new** routes it **owns**:

* **New** is a same-origin path under the base, not excluded, not
  disallowed by `robots.txt`, not answering 4xx/5xx, and not in the run's
  stored list (which the prompt states in full, at most 60 paths).
* **The owner rule is deterministic and needs no coordination.** For a run
  of `G` capture steps, the new route `p` belongs to step
  `1 + (int(sha256(p)[:8], 16) mod G)`. Every step computes the same owner
  for every path, so **two steps never both capture one new route**. A
  step that finds a route it does not own writes it to its `found.json` and
  does not capture it.
* **The cap.** A step captures at most `found_per_step` owned new routes
  (default 4, at most 8), and the renderer bounds the run so stored plus
  `G x found_per_step` never passes `MAX_SCREENS` (60) per leg. Past the
  cap, owned routes are listed in `found.json` as `over_cap`, in
  breadth-first order, so the next run has them stored.
* **New route ids** are `route_id(path)` with a collision suffix taken from
  `sha256(path)[:6]` rather than `route_id`'s counter, so a path has the
  same id in every step and every run, and the filing step's de-duplication
  key (`visual-qa:<target_id>:<screen>:<title>`) stays stable.

**What this misses, stated plainly.** If the owner step never reaches a
route that another step saw (a page that rendered a link for one session
and not another, a crawl one step cut off at its time bound), nobody
captures it this run. The finder lists it; the report shows it as "found,
not reached by its owner" (§3.5); the merge of §5 stores it, so the next
run captures it as a stored route. Exact coverage needs coordination, which
is option (b).

**Wall-clock.** Stored routes are captured from minute zero, as today: no
step waits on any discovery. Each step's crawl is bounded by
`--crawl-seconds` (default 600) and the gate's navigation spacing (default
2 s, floor 1 s), so a step grows by at most its crawl plus `found_per_step`
captures; the capture timeout grows from 1,800 s to 2,700 s for a crawling
step. Against today's two dispatches (a discovery workflow of up to
1,800 s, then the run), a first run is one dispatch and no slower than a
run of the same size.

**Cost.** `G` crawls instead of one: the same pages walked by each step.
The model's share is small (haiku waits on the tool), the navigation's share
is the target's: `G` concurrent browsers at the leg's `navigation_ms` each,
which is exactly the load `G` parallel capture steps already put on a
target today (the rate is per attempt, §3 of visual-qa-any-app.md). No
review, report or filing step changes cost except by reviewing the new
routes it is given.

**A run with no stored screens** (BGD's "from nothing but the base URL").
`runnable` stops refusing `qa_target_no_screens` for a target with
`discover` set: the renderer makes `discover.slots` capture steps (default
4, at most 8) with no stored routes, each crawling from the base path and
owning its share. A target with neither `screens` nor `discover` is still
refused, with today's code and a sentence naming `discover`. The standalone
`:discover` stays, for a person who wants the list without a run.

**Security of the new path.** None of it is new authority:

* the crawl is the existing `swarm_qa discover` walk, run inside the capture
  step's attempt, on the attempt's one leg, through the same gate: the
  origin lock answers any off-origin top-frame navigation with the gate's
  page, writes stay refused (a capture step is never `mutating`: the worker
  route answers `mutating: true` only for `flow-<id>`), and the rate limit
  holds;
* what an agent found reaches nothing but its own capture and its
  `found.json`, an artifact; swarm-api reads it through the tenant-checked,
  redacted artifact read and holds every path to `qaspec.discovered_screens`
  (same origin, not excluded, well-formed ids, at most 60) before storing
  anything (§5);
* no prompt carries a found path into another step: the stored list in the
  prompt is swarm-api's, from the target.

### 3.2 (b) A discovery step beside the capture steps, feeding them through the API

A `discover` step starts with the capture steps. As it finds routes it
appends them to a per-run work queue, and each capture step, after its
stored routes, takes queued routes until the queue is empty and the
discovery has ended. Two ways to carry the queue:

* **(b1) A queue the steps read through the API.** `qa_run_queue/{workflow_id}`
  in swarm-api; the agent cannot sign an attempt request, so the worker
  relays (a new `swarm-handoff qa-queue put|take` verb over the worker's
  socket, as the child spool is relayed), and swarm-api proves the attempt,
  checks the run record names the agent, holds each path to
  `discovered_screens`, and hands each route to exactly one taker in a
  transaction. Exact coverage, balanced load; no frozen change. New:
  one worker route, one relay verb, one collection, and a capture step that
  ends only when the queue is drained **and** the discovery agent has
  ended, which is the polling CLAUDE.md asks agents not to do (a capture
  agent sleeping on an empty queue, while the discovery walks, is invariant
  4's "sleeping through a wait").
* **(b2) Marked child agents.** The discovery step submits one child capture
  per batch of found routes through the existing child spool
  (`POST /v1/tasks/{parent_task_id}/children`). A child carrying
  `metadata.qa_target` is **a security change**: today the reserved mark is
  admitted only inside `run_target` and a schedule firing, and a child's
  metadata is the worker's request. Exactly what would change: the children
  route would copy the **parent's** stored mark (never one in the request)
  onto a child whose profile is `claude-code-browser`; swarm-api would append
  the child's task id to the parent's `qa_target_runs` row in the same
  transaction, so `_run_names` serves the child; and the child's route list
  would be read by swarm-api from the parent's `found.json` artifact, held to
  `discovered_screens`, never from the request. Children are not workflow
  steps, so the report cannot `input_from` them: it would read their tars
  through the artifact API, a new path for a review step. `Workflow.steps`
  does not grow, so no frozen change; the security review of CLAUDE.md
  applies.

Wall-clock: the best of the options, capture of found routes starts as soon
as they are found. Cost: one crawl, not `G`. A run with no stored screens:
swarm-api seeds the queue with the base path, and capture steps start with
empty stored lists. Coverage is reported exactly: queued, taken, captured.

### 3.3 (c) A fresh discovery heads every run (the baseline, ruled out)

A `discover` step first; `G` capture steps each `input_from` its
`screens.json` and take their share by index. No new path, no coverage gap,
no frozen change; and every capture waits for the discovery, which is the
"prerequisite" BGD's wording rules out. Kept here only as the baseline the
others are measured against: its wall-clock is the discovery's plus the
run's.

### 3.4 Comparison

| | (a) crawl in every capture step | (b1) API work queue | (b2) marked children | (c) discovery first |
|---|---|---|---|---|
| capture waits for discovery | no | no | no | **yes** |
| a new route captured this run | when its owner reaches it, up to the cap | when any step takes it | when a child runs | always, up to 60 |
| crawls a run | `G` | 1 | 1 | 1 |
| new API surface | none (one artifact read at settle) | worker route, relay verb, collection | children route copies the mark; report reads child artifacts | none |
| security review | no new authority | new attempt-signed route | **yes**: the reserved mark on a child | no |
| frozen contract | none | none | none | none |
| first run from the base URL alone | `discover.slots` crawling steps | queue seeded with `/` | discovery seeds children | the discovery's list |

### 3.5 How coverage is reported

Every crawling capture step writes `found.json` beside its manifest:
`{format: 1, stored: [...], captured_new: [{id, path, depth}], not_owned:
[...], over_cap: [...], skipped: {off_origin, outside_base, excluded,
robots, http_error, failed, write_attempting}, crawl: {pages, depth,
seconds, stopped_by: "done" | "pages" | "depth" | "time"}}`. The capture's
manifest lists the new routes' images like any other, so the report's
**reviewed-count check is unchanged**: it compares each group's manifest
image count with the review's `reviewed`. Where a capture tar is missing,
the expected count is computed from the stored routes as today and the
report says the found routes' count is unknown.

The report gains one section per leg, "Coverage": stored routes captured,
new routes captured (with their paths), found but over the cap, found but
not reached by their owner, and why the crawl stopped. A crawl stopped by
`time` or `pages` is said plainly, because a missing route is then not
evidence the route is gone (§5).

### 3.6 Draft frozen-contract request (not filed)

Only an option this design does not recommend would need one, so it is
drafted here, in `docs/contract-change-requests.md` form, and **not** filed
there:

> **QE-1. `models.py`: a running workflow's steps may grow (QA-ENVIRONMENTS-DESIGN, part of #2136).**
> *What is true today:* `Workflow.steps` is fixed at submission; nothing
> appends a step to a running workflow. *The requested change:* an
> `append_steps` path, swarm-api only, admitting steps whose
> `runner_profile` is one of the workflow's existing steps' and whose
> `depends_on` names existing steps only, so a discovery step could add
> capture steps that a report step then stages. *What it would break:* every
> reader that treats `steps` as fixed (the workflow state derivation, the
> cancel cascade, the console's step list, `on_step_failure`) and the
> schedule's per-item accounting. *If it is declined:* option (a) or (b) of
> §3 gives the same result without it.

---

## 4. Discovery coverage: menus, buttons, tabs, hash routes

Today the walk reads only rendered `a[href]` (`LINKS_JS`) and the renderer
never passes `--hash-routes`. Sections reached by a menu, a button or a tab
are therefore invisible unless a link to them is in the DOM. The design
widens the walk in three steps, each bounded, none of them able to write:

1. **More link sources, no clicks.** `--hash-routes` on for every crawl
   (a `#/route` fragment is a route; `#section` stays not one). And besides
   `a[href]`: `area[href]`, `[role=link][href]`, `[routerlink]`,
   `[data-href]`, `link[rel=next|prefetch|prerender][href]`, read from the
   whole DOM, hidden elements included (a collapsed menu's links are usually
   already in it). Each value goes through the same `same_origin_path` and
   `under` checks as an `a[href]`.
2. **Disclosure clicks, and only disclosure clicks.** On each crawled page,
   the walk may click controls that **reveal** rather than act: an element
   with `aria-expanded="false"`, `aria-haspopup`, `role=tab`,
   `role=menuitem` with `aria-haspopup`, or a `<summary>`. It never clicks:
   a control inside a `<form>`, a `type=submit` or `type=reset`, an `<a>`
   whose target is off-origin, an `<input>`, anything whose text (trimmed,
   case-insensitive) contains any exclusion word of the target (the
   defaults: delete, remove, pay, buy, purchase, send, invite, unsubscribe,
   cancel subscription, and `logout`; plus the target's own
   `exclusions.controls`), or anything matching an exclusion selector. After
   each click it harvests links again (1) and, if the click changed the
   page's same-origin path (a router push), records that path as a route.
   Then it restores the page (`Escape`, or reloads the path when the click
   navigated).
3. **A section that has no URL** (a tab whose content changes but the path
   does not) is recorded as a route **state**: `{path, reveal: [{text,
   css?}]}`, the same shape and the same registration rules as
   `sign_in.start_clicks` (`qaspec.start_clicks`: plain CSS, exact visible
   text, 1 to 3 clicks, no exclusion word), which capture replays before it
   shoots. The route list format gains `reveal`, optional; `swarm_qa`'s
   `load_routes` and swarm-api's `discovered_screens` hold it to those rules.

**Why it cannot write.** A disclosure control does not submit by its role,
and the exclusion words and the form rule refuse the controls that do. On a
production leg, the gate's write refusal is the backstop that does not
depend on any of that: any `POST`, `PUT`, `PATCH` or `DELETE` a click
causes to the leg's origins is refused (`qa_write_refused`), and anything
but `GET` to another origin is refused on every leg. A capture step is
never `mutating` on any leg, staging included. The walk reads the gate's
refusal record (`GET /json/swarm/qa-refusals`, which capture already reads)
after each click; a click that caused a refusal marks that control
`write_attempting`, it is never clicked again in the run, and the coverage
section lists it, because a control that tried to write is a finding about
the app worth a person's look, not a route.

**Bounds.** Pages: `discover.max_pages` (at most 60). Depth:
`discover.max_depth` (at most 3), a disclosure click adding no depth.
Clicks: at most 20 a page and 200 a crawl. Time: `--crawl-seconds`, default
600, at most 900. Rate: every top-frame navigation, a click's router push
included, is spaced by the leg's `navigation_ms` (the gate holds it; floor
1 s, never faster). `robots.txt` `Disallow` for `*` is honoured for every
path the walk would load or record.

---

## 5. Where found routes go

**The options.** (a) merged into the leg's `discovered` automatically after
the run; (b) shown in the report and the console for a workspace admin to
promote into `screens`; (c) both.

**Recommended: (c).** When a leg's workflow ends, the `task_finished` push
swarm-api already answers (`QaTargets.settle_discovery`'s doorbell) settles
the run: it reads every capture step's `found.json` through the
tenant-checked, redacted artifact read, holds every path to
`qaspec.discovered_screens` (and `reveal` to the start-click rules), and
merges the union into **that leg's** `discovered`. Each route there carries
`first_seen`, `last_seen` and the `workflow_id` that last saw it. Storing
never bumps `revision`, exactly as discovery's write does today, so a run
already started keeps its mark. The next run's stored list includes them,
so a route found today is captured from minute zero tomorrow. `screens`, the
list an admin wrote, is never written by a run: the report and the target's
card list "new since the last run" with **Promote** (an admin's PATCH, as
any edit of `screens` is).

**Routes that disappear.** A route in a leg's `discovered` that a complete
crawl (`stopped_by: "done"`) did not see is marked `missing_since`; after
three consecutive complete crawls of that leg without it, it is dropped
from `discovered` and the report lists it under "gone". A crawl stopped by
a bound proves nothing about absence and changes nothing. A route in
`screens` is never dropped by a run: when it answers 4xx/5xx it is already
a failed route at `navigate`, a finding about the run, and the card offers
the admin **Remove**.

**Between legs.** Because `discovered` is per leg and `screens` is shared,
the run page shows "on staging, not on production" and the reverse. That is
information (a feature not shipped yet), never a filed finding.

---

## 6. Production legs

**What a production leg may do: read.** Captures, reviews, crawling (§4) and
flows declared `mutating: false`. Nothing else, enforced by the gate and
the worker route as today. A `mutating: true` flow is rendered only for
legs whose mark is `non_production` **and** that share no approved origin
with a production leg (§1.5); a production leg's run renders no mutating
flow at all. Allowing a mutating flow on production would change
visual-qa-any-app.md §3 and is the owner's (§9 Q6, recommended no).

**Production sign-in.** Today the gate refuses MFA and CAPTCHA
(`qa_sign_in_challenge`). For an MFA-protected production account there are
four choices, in §9 Q7:

1. **An MFA-free QA account in production** (recommended): a test account
   the app's owner exempts from MFA, with the least privilege that can see
   the screens. Nothing in the platform changes.
2. **The identity provider's own skip rule, carried by §2.5's bypass.** Some
   providers skip a second factor on a request header the owner configures
   (Keycloak's conditional OTP form has such a setting; whether AI Studio's
   production realm uses it is a fact for CLD or BGD). The bypass already
   applies a header on the leg's approved origins only, the IdP's
   `extra_origin` included. But §2.5 was decided for CAPTCHA and WAF
   challenges; using it to skip MFA widens that decision, so it is the
   owner's.
3. **Public pages only** for production: the production leg has `sign_in:
   {kind: none}` and captures what an anonymous visitor sees.
4. **A TOTP seed in the QA secret, the gate computing the code.** Reverses
   §2.4's "never solved", puts a second factor for a production account in a
   platform secret, and is not recommended.

**Facts this lane does not know, set per environment when known:**

* **F-1.** AI Studio's production sign-in: its `login_path`, its identity
  provider's origin (its `extra_origin`), and whether the production leg is
  `https://studio.saga.xyz` with the chooser's **Production** toggle or a
  different host. BGD or CLD supplies it; it is an entry's data, typed into
  the console, not code.
* **F-2.** Whether that production account has MFA, and of which kind.

The design never hard-codes them: `base_url`, `sign_in.start_clicks` (the
staging entry clicks `Staging` then `Sign in`, the production entry
`Production` then `Sign in`), `extra_origin` and the approval are per entry,
and the sign-in note each prompt carries (`qaspec._sign_in_note`) names
**that leg's** clicks only, so an agent on one leg is never told to click
the other leg's toggle. If it clicks it anyway, §1.5's shared-origin rule has
already turned any write permission off.

---

## 7. The console

**The dispatch form** lives on the target's card on Repositories › QA
targets, where Run is today; **Work › Submit a workflow** gains a "Visual QA
of a registered app" entry that opens the same form, so there is one form.
A member picks the app (the target), and sees one checkbox per environment,
each with its host state, its credential state and `cannotRunBecause` for
that leg (a leg that cannot run is drawn disabled with its reason; the
others stay runnable). Beside them, as today: "report only, file no issues"
and "Flows with". **Run** sends `:run` with the ticked names and nothing
else the caller picks (invariant 10: names only, never a URL, a sign-in or a
credential).

**The run page** (`/qa-runs/<group_id>`, from `GET /v1/qa-runs/{group_id}`,
tenant-scoped) shows one column per leg: the workflow and its state, the
steps, screens captured (stored and new), coverage (§3.5), findings by
severity, issues filed, and the gate's refusal count; above them, the routes
seen on one leg only (§5). Each column links its workflow's own pages, which
exist today.

**Findings per environment.** The options, §9 Q8: (a) one issue per finding
per environment: the de-duplication key gains the leg
(`visual-qa:<target_id>:<environment>:<screen>:<title>`), the title starts
`[staging]` or `[production]`, and the body links the other leg's run; the
two legs' file steps run concurrently and never race on one key
(recommended). (b) one issue naming both legs: the same defect on both is
one bug to fix, but the two file steps run in separate workflows at the
same time, and the broker's search-then-file is not atomic across them, so
(b) needs one filing step for the group (a third workflow swarm-api submits
when both reports end, staging both `findings.json` it reads through the
artifact API), which is a new path.

**The `visual-qa` schedule type** takes the same choice: `VisualQaParams`
gains `environments: list[str]` (at least one; default every entry), checked
against the target at create, edit and every firing; a firing is the same
`:run` with that list, and its caps are §9 Q9's.

---

## 8. Build plan

Each lane owns the files named; **two lanes that touch one file are one
lane** (CLAUDE.md, "Issues"), so lanes sharing `qatargets.py` are phases of
one lane, run one after another, never in parallel. Every lane pushes its
tests first to the PR branch and its fix to `<branch>-fix`, and runs
`scripts/changed-guards.sh` before finishing. The table assumes the
recommended answers; a different answer changes the lanes it names.

| phase | lane | territory | red-first tests (each fails before the change) | needs first |
|---|---|---|---|---|
| 1 | **QAENV-MODEL** | `apps/swarm-api/swarm_api/qatargets.py`, `qaspec.py`, `routes/qatargets.py`, `tests/unit/control_plane/test_qa_targets.py`, `test_qaspec.py` | an unmigrated target reads as one entry and the migration writes `environments` with one audit row and a new revision; approving one entry's host leaves the other `pending`; a credential link minted for `staging` is refused by `PUT` for `production`; the worker route serves only the mark's entry (another entry's origin, password or bypass is in no answer, a runtime-built fake value asserted absent); `mutating` is false on a `non_production` entry sharing an origin with a production entry; the legacy secret key is read until the entry's next write and gone after it; a map past 56 KiB is refused | §9 Q1 |
| 1 | **QAENV-CRAWL** | `images/agent-runtime-browser/swarm_qa.py`, its tests under `tests/unit/` | `capture --crawl` harvests links from captured pages; the owner of a path is the same in every step for a given `G`; a step never captures a route it does not own or past its cap; `found.json` lists `not_owned` and `over_cap`; `--hash-routes` keeps `#/x` and drops `#x`; a control inside a form, with an exclusion word or of `type=submit` is never clicked; a click that the gate refused marks the control `write_attempting`; `reveal` with a Playwright selector is refused; the crawl stops at its time bound and says so | §9 Q3, Q4 |
| 1 | **QAENV-GATE** | `apps/agent-worker/agent_worker/livegate/qatarget.py`, `apps/agent-worker/agent_worker/qa_target.py`, their tests | a second hand-off with the same target id and another environment name is refused `target_changed`; a hand-off without the name is still accepted (old marks) | §9 Q1 |
| 2 | **QAENV-RUN** (QAENV-MODEL's files, after it merges) | `qatargets.py`, `qaspec.py`, `routes/qatargets.py`, `schedtypes/visual_qa.py`, `scheduletypes.py` `VisualQaParams`, their tests | `:run` with two names submits two workflows, each with its one mark, and one group document; a refusal on either leg submits nothing; a target with `discover` and no screens renders `discover.slots` crawling steps instead of `qa_target_no_screens`; the stored list is in every crawling step's prompt and no found path is; the report prompt carries the coverage section; the schedule's `environments` refuses a name the target lacks; a firing creates one work item per leg within `max_concurrent` | QAENV-MODEL merged; §9 Q2, Q9 |
| 3 | **QAENV-SETTLE** (same files, after QAENV-RUN) | `qatargets.py`, `qaspec.py`, their tests | a leg's `found.json` paths off-origin, excluded or malformed are dropped or refuse the file as `discovered_screens` does; the merge writes `first_seen`/`last_seen` and never `screens` or `revision`; a route unseen in three complete crawls is dropped and one unseen in a crawl stopped by `time` is kept; the file prompt's dedupe key names the leg | QAENV-RUN merged; §9 Q5, Q8 |
| 3 | **QAENV-UI** | `apps/swarm-ui/src/QaTargets.tsx`, `qaTargetsApi.ts`, a new `QaRunGroup.tsx`, `App.tsx` route, `paths.ts`, the Submit-a-workflow entry, their vitest files; `.github/ISSUE_TEMPLATE/` if a nav tab changes (`test_issue_forms.py`) | a leg that cannot run is disabled with its reason while the other stays enabled; Run sends names only; the run page shows one column per leg and the one-leg-only routes; Promote is drawn to an admin only | QAENV-RUN merged |
| 3 | **QAENV-SC** | `apps/swarm-mcp`, `plugin/` (`swarm_qa_run`, `swarm_qa_targets`) | `swarm_qa_run` takes `environments` by name and no URL field; the targets tool lists entries without any credential field | QAENV-RUN merged |
| 4 | **QAENV-DOCS** | `docs/design/visual-qa-any-app.md`, `docs/schedules.md` §3.17, this file's status | `test_docs_describe_what_was_built` | each lane above |
| data | **AI Studio production entry** (a person, not a lane) | the console | — | F-1, F-2, §9 Q7; a platform admin approves the entry's host |

The review CLAUDE.md requires for credentials, isolation and redaction
applies to QAENV-MODEL (the secret key, the link binding, the worker
route's per-leg answer) and QAENV-GATE.

**Invariants.** 1: a leg not yet submitted, and a queued leg, are documents
and cost nothing; nothing here holds capacity. 4: no step waits on another
(option (a)); option (b1) is the one that would make an agent wait. 9: each
leg's credential is a key in its own tenant's secret, served only to that
tenant's agents the run record names. 10: a caller names a target and
environment names; every URL, sign-in and credential is the target's.

---

## 9. Questions for the owner

Each is also in this lane's `questions.json`, recommendation first.

**Q1. How does one app carry two environments?**
(a) **one target with an `environments[]` list**, the sign-in, credential,
host approval, write rule and discovered routes per entry, the rest shared
(recommended: one route list and one set of flows for one app, and every
per-leg rule still applied per leg; costs a migration and a secret key
change with no IAM); (b) two targets linked as one app (no migration, no
secret change; everything shared is kept twice and drifts); (c) no model
change, "run both" over two picked targets (cheapest; no app, a schedule
names a list).

**Q2. How does one dispatch run both legs?**
(a) **one route submits one workflow per leg, linked by a run group**
(recommended: the single-mark rule, the gate and the report stay as they
are; a failed leg costs the other nothing); (b) one workflow whose steps
carry their own leg's mark (one report could compare the legs; changes the
single-mark rule to a per-step map, doubles the workflow, and puts both legs
under one cost tripwire).

**Q3. How does discovery run during QA?**
(a) **every capture step crawls the same graph and captures the new routes
it owns by a hash of the path, up to a cap** (recommended: nothing waits,
no new API or authority, no frozen change; a route its owner did not reach
waits one run); (b1) a discovery step feeding capture steps through an API
work queue (exact coverage, one crawl; a new attempt-signed route and relay,
and capture agents that wait on the queue); (b2) the discovery step submits
marked child captures (exact coverage; the reserved mark on a child is a
security change and the report reads child artifacts by a new path); (c) a
discovery step heads every run (the baseline your wording rules out).

**Q4. How far may discovery go to find menu-, button- and tab-reached
sections?**
(a) **more link sources plus disclosure-only clicks** (`aria-expanded`,
`aria-haspopup`, tabs, `<summary>`; never in a form, never an exclusion
word), on every leg, the gate's write refusal as the backstop (recommended);
(b) more link sources and hash routes only, no clicks (no click risk at all;
misses sections behind menus that render their links on open); (c) clicks
on non-production legs only (production finds less than staging).

**Q5. Where do found routes go?**
(a) **both: merged into the leg's `discovered` after every run, and shown
for an admin to promote into `screens`** (recommended: the next run captures
them from minute zero; `screens` stays the admin's); (b) merged only;
(c) proposed only (nothing found is captured again until someone promotes
it).

**Q6. May a production leg run a mutating flow?**
(a) **no: production legs read only, §3 unchanged** (recommended);
(b) yes, for flows an admin declares, which changes visual-qa-any-app.md §3
and needs a production test account that may write.

**Q7. How does a production leg sign in when the account has MFA?**
(a) **an MFA-free QA account in production** (recommended: nothing in the
platform changes); (b) the identity provider's own skip-by-header rule,
carried by §2.5's challenge bypass on the IdP origin (no new code; widens
the CAPTCHA decision to MFA); (c) production legs capture public pages only;
(d) a TOTP seed in the QA secret, the gate computing the code (reverses
§2.4; not recommended).

**Q8. How are findings filed per environment?**
(a) **one issue per finding per environment, the leg in the dedupe key and
the title, each linking the other leg's run** (recommended: no race between
the legs' concurrent file steps); (b) one issue naming both legs, through a
group filing workflow swarm-api submits after both reports (one issue per
bug; a new cross-workflow artifact path).

**Q9. What do the `visual-qa` schedule's caps become for two legs?**
(a) **per leg: $40 a leg, `max_concurrent` 2, $160 a day** (recommended:
keeps "the daily firing plus one run-now" for both legs, as VQ7-CAPS set for
one); (b) $40 a leg, `max_concurrent` 2, $80 a day (one two-leg firing a
day, no run-now room); (c) unchanged until the crawl's cost is measured, a
schedule naming two legs refused until then.

**F-1, F-2 (facts, not choices).** AI Studio's production sign-in host,
`login_path`, IdP origin and whether it is the chooser's Production toggle on
`https://studio.saga.xyz`; and whether its production account has MFA.
