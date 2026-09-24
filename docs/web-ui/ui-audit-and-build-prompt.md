# SwarmCloud web UI — audit, and the prompt to build the replacement

**Base commit:** `8abc9c7` ("A claude-code agent was never told where to put its
work"). The three lanes this document compiles reported against `2d7b72a`; the
branch moved two commits while they were writing, and §A0 lists what moved.

**Status:** the audit half is an observation record. The build half is an
executable brief — it is written so that an engineer or an agent can start at
§B10 step 1 and work down without asking a question. Where a choice had to be
made to keep it executable, the choice is stated with its cost and repeated in
§B13 so the owner can reverse it cheaply.

**What it compiles.** Three audits, plus my own verification pass:

| Lane | What it looked at |
|---|---|
| 1 | The product as the user who dispatched `wf_bcdc9180e4fb4a209f31`, from the screenshots |
| 2 | The layout, type, colour and density systems, from the stylesheet and the screenshots |
| 3 | Charting stack, the duration breakdown, the visualisation table, the server gaps |

I re-derived every figure I repeat below that could be re-derived from the
repository or from a PNG. Where a lane's number and mine disagree, mine is
printed and the disagreement is named. Where I could not re-derive something —
bundle sizes, anything against a deployed cluster — it is attributed and marked.

**What it deliberately does not do.** It does not re-litigate the navigation.
`docs/web-ui/redesign.md`'s "Superseded, 2026-09-21" block records the owner's
decisions — eleven nav items to five (now six), Capacity renamed Pools, Activity
renamed History, no Trouble page because "trouble is a bad word for a page" —
and they stand. §B2 keeps all six nouns.

---
---

# HALF ONE — THE AUDIT

---

## A0. What moved since `redesign-v2.md` was written, and what that invalidates

`redesign-v2.md` is 1,184 lines against `f0154b4`. Four of its load-bearing
claims are now false. Read this section before citing it.

### A0.1 F0 is FIXED. Every cost figure in the product is now reachable.

`redesign-v2.md` §1.0 is its self-declared most important sentence: the attempt
decoder drops the five spend fields, so every cost and token figure renders as
an em dash forever. That was true. Commit `e15bffd` ("Every cost figure in the
product was unreachable, and the comment blamed the worker") fixed it.

Verified at `8abc9c7`, `apps/swarm-api/swarm_api/codec.py`, inside
`attempt_from_dict`:

```
        # THE FIVE SPEND FIELDS, which this decoder used to drop.
        ...
        # NOTE the `is None` checks rather than `or`: 0 tokens and $0.00 are
        # measurements, and `data.get(k) or None` would turn a real zero back
        # into "not measured" -- the same conflation this codebase has spent
        # three days removing, reintroduced in the line that fixes it.
        input_tokens=data.get("input_tokens"),
```

And it is visible on screen. `docs/web-ui/evidence/01-overview-now.png` shows
`TOKEN SPEND $0.0540`, and the SPEND panel below it reads `COST $0.0540 ·
INPUT 6 · OUTPUT 214 · CACHE READ 73.2k · CACHE WRITE 9.1k`.
`09-history-timeline.png` shows `TOKENS & SPEND 1 of 7 — 14% of rows carry
usage; the rest predate the capture, so any total would understate.`

**Consequence for the build.** Visualisation #16 (cost and tokens) is no longer
blocked. It moves into the first buildable batch. The coverage caveats in
`redesign-v2.md` §Panel 7 all still hold and are restated in §B6.4 — in
particular that `record_spend` runs on the clean-exit path only, so a parked or
crashed attempt records nothing, and those are the expensive ones.

### A0.2 The log and checkpoint routes LANDED, and no screen calls them.

`redesign-v2.md` ranks S2 (serve the live log tail) and S3 (serve artifact and
checkpoint bytes) as unbuilt seams. Commit `96164c3` ("Checkpoints and logs can
be read back now, and the logs are redacted on the way out") built two of the
three.

Verified at `apps/swarm-api/swarm_api/routes/tasks.py`:

```
136:@router.get("/{task_id}/events")
147:@router.get("/{task_id}/attempts")
177:@router.get("/{task_id}/artifacts")
200:@router.get("/{task_id}/checkpoints")     <- new
245:@router.get("/{task_id}/logs")            <- new
```

`/checkpoints` takes `attempt_id`, `limit` and **`page_token`** — it is the one
list route in this file that pages. `/logs` takes `attempt_id`, `stream`,
`source` (`auto`/`final`/`live`), `offset` and `limit_bytes`, applies
`swarm_api.redaction` unconditionally on the way out, and reports each stream as
`ok`, `absent` or `unreadable` independently with `content` null rather than
`""` in the latter two.

The TypeScript client is written too — `loadCheckpoints` at
`apps/swarm-ui/src/api.ts:377` and `loadTaskLogs` at `api.ts:412`, both fully
documented, both handling the status trichotomy correctly.

**And nothing imports either of them.** Verified:

```
$ grep -rn "loadTaskLogs\|loadCheckpoints" apps/swarm-ui/src/
apps/swarm-ui/src/api.ts:377:export async function loadCheckpoints(
apps/swarm-ui/src/api.ts:412:export async function loadTaskLogs(
```

Two matches, both the declarations. No screen, no drawer, no tab.

This is the exact defect class `tests/unit/control_plane/test_runtimes_screen.py`
was written for, one link further along the chain. Its docstring:

> the route is read by the client, the loader is used by a screen, and the
> router reaches that screen — three links, and a chain that is missing any of
> them renders nothing while every component looks finished.

`/v1/runtimes` was missing link two. Logs and checkpoints have link two and are
missing link three. That test pins the chain for `/v1/runtimes` only; it is not
generalised, which is why this went unnoticed. §B11 makes generalising it step
one of the proof work.

**Consequence for the build.** "A way to inspect the outputs" and "live logs"
are no longer platform asks. They are UI work against routes that already
answer. They move from §B9 (seams) into §B6.12 (the dock).

### A0.3 The stylesheet grew; all four collisions survived.

`apps/swarm-ui/src/styles.css` is **1,976 lines**, not the 1,877 `redesign-v2.md`
measured. Every collision it named is still there, at the same shape:

```
120:.bar { height: 5px; border-radius: 999px; background: var(--surface-2); overflow: hidden; }
903:.bar {  margin-bottom: 10px; padding: 9px 12px; border-radius: var(--radius); border: 1px solid var(--line); ... }

160:@keyframes pulse { 0%, 100% { opacity: .5 } 50% { opacity: .85 } }
900:@keyframes pulse { 0%, 100% { opacity: 1 } 50% { opacity: .35 } }

202:.filters { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
514:.filters { display: flex; align-items: center; gap: 14px; ... font-size: 12.5px; ... }

739:.panel h2 { display: flex; align-items: center; gap: 10px; }   <- the only .panel rule in 1,976 lines
```

`.app { max-width: 1100px }` is still at line 69.

### A0.4 `task_to_api` still does not serve the fencing state.

`apps/swarm-api/swarm_api/codec.py:87-88` decodes `current_lease_id` and
`current_generation` off the Firestore document into the `Task`. `task_to_api`
(`codec.py:146-183`) serves 28 fields and neither of those two. The UI's own
type file already documents the absence, at `apps/swarm-ui/src/types.ts:372`:

> NOT on the task: `current_generation`. … so `current_generation > attempt_count`
> is exactly [the fence]

Marked IN FLIGHT in §A4 — a lane is reported to be fixing it. I did not verify
that lane's work.

---

## A1. The journey, as the user who dispatched the workflow

Compiled from Lane 1, checked by me against the PNGs named. Every claim here is
something visible in a file in `docs/web-ui/evidence/`.

### A1.1 Overview answers "is anything of mine running?" with "no", wrongly

`01-overview-now.png`, captured while `wf_bcdc9180e4fb4a209f31` had `research`
READY and `draft`/`review` PARKED.

The word "workflow" does not appear anywhere on it. Not in the five tiles, not
in NEEDS ATTENTION, not in RUNNING NOW. What it says instead:

```
RUNNING NOW        0 agents
                   LEASED, DISPATCHED, STARTING, RUNNING — the states that reserve capacity

RUNNING NOW panel  Nothing is running
                   No task on the 7 most recently created is in LEASED, DISPATCHED,
                   STARTING or RUNNING. The state counts agree: zero.
```

Every sentence is true. The conclusion a reader takes away — the platform is
idle, nothing of mine exists — is false.

This is not a wording problem, because the panel that exists to catch it cannot.
The NEEDS ATTENTION tile prints its own scope on the card: "derived from
dispatch, leases, provider quota, accounts, pools and failures · 6 of 6 checks
ran". `deriveChecks` at `apps/swarm-ui/src/Overview.tsx:1807-1815` is exactly
those six and no more:

```python
  return [
    dispatchCheck(s.stats),
    leaseCheck(s.leases),
    quotaCheck(s.providers),
    accountCheck(s.accounts),
    poolCheck(s.capacity),
    failureCheck(s.tasks),
  ]
```

There is no parked check and no workflow check. A three-step workflow stalled on
`DEPENDENCY_INCOMPLETE` raises nothing, while the "clear:" line reassures about
20 pools and 1 account. The only thing the panel surfaced was an unrelated
failed task from a different run.

### A1.2 Agents opens on the one tab that cannot contain a stalled step

`02-agents-running.png`. The page lands on **Live**, Live's badge is **0**, and
the body is:

```
Nothing in this tab
No agent is holding a pool slot right now. This is a real zero from a successful read.
```

The three steps are behind **Waiting 3**, one tab over. The default is set at
`apps/swarm-ui/src/Agents.tsx:39` — `useState<Tab>('live')`. The section's
printed question, visible two lines above, is "What is running, what is waiting,
what did it produce — and why has mine not moved?" The default tab is the one
tab that structurally cannot hold a step that has not moved.

**The truncated id is worse than short.** `Agents.tsx:295` is
`{task.id.slice(-8)}` — the *last* eight characters. `40158851` is not a prefix
of `task_b5dc2568713a40158851`. It cannot be pasted into a search, matched
against the `task_b5dc25…` strings the same page prints in its own receipt
footer, or matched against the drawer title. The full id exists only as a native
`title=` tooltip.

**What does work is the step chip.** `22-detail-research.png` shows the row as
`• DISPATCHED  claude-code / 40158851   bogdan   (RESEARCH)`. The
RESEARCH / DRAFT / REVIEW chips are the one usable handle on the list and are a
good decision. But the chip carries only `step_id`; the workflow id is again
hover-only (`Agents.tsx:305`, `title={`workflow ${task.workflow_id}`}`), so with
two workflows in flight, three rows reading RESEARCH/DRAFT/REVIEW cannot be
assigned to either without hovering each one.

### A1.3 Workflows draws the shape correctly and answers nothing else

`21-workflows-list.png`, `03-agents-workflows.png`, `40-workflows-*.png`.

**Shape: right.** `research → THEN → draft → THEN → review`, each node carrying
state, runner profile and a `← research` / `← draft` dependency line. The `THEN`
divider with a hairline either side, and the dependency printed *on* the node
rather than drawn as a connector, are better than a decorative line would be.
`Workflows.tsx:169-176` records why: a single vertical stalk used to sit there
and "it LIED: one line between levels reads as a linear chain, and this is a
DAG". That reasoning is correct and must survive.

**What each step produced: nothing.** No output, no duration, no cost, no
artifact, no task id on any node. And it is a dead end. `StepNode`
(`Workflows.tsx:266-298`) renders:

```tsx
    <div className={`node ${p.tone}`} title={p.title}>
```

No `<a href>`, no `onClick`. The task id is inside `p.title`, a native tooltip.
From "draft is parked" there is no click that reaches draft. The route is
Agents → Waiting → find the row → open the drawer, re-identifying by the step
chip a step you were already looking at.

**The heading states something false.** On `21-workflows-list.png` the heading
reads `WF_BCDC9180E4FB4A209F31 · QUEUED · UPDATED 2M AGO` while the research
node directly below reads `● dispatched`. Two independent bugs:

- `QUEUED` is `workflow.state`, which `docs/web-ui/README.md` records as written
  once at creation and never updated. A permanently-dead field is printed in the
  heading, in the same typeface, directly beside the live computed rollup
  `0/3 done`, with nothing distinguishing which of the two is maintained.
- `styles.css:91-95` uppercases it, and the id with it:
  `.section > h2 { font-size: 12px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.07em; color: var(--text-faint) }`. `Workflows.tsx:154`
  deliberately writes `{workflow.workflow_id} · {workflow.state.toLowerCase()}`;
  the stylesheet re-uppercases the state and mangles the id. The only handle the
  user has for their workflow is rendered at 12px, in the faintest token in the
  palette, in a form that does not match `wf_bcdc9180e4fb4a209f31`.

Two screens away, `QuotaDetail.tsx:94-96` refuses to do this:

```tsx
      {/* No text-transform here. Tenant ids are opaque and a displayed id that
          differs from the real one is unusable — `.filters button` elsewhere
          capitalises, which is why this is a plain cell. */}
```

**The layout wastes the screen.** `40-workflows-1024x768.png`: the DAG is a
~132px column with roughly 430px of blank page to its left. The cause is two
declarations, `styles.css:239-259`: `.level { align-items: center }` and
`.level-steps { justify-content: center }`, with `.node { min-width: 132px }`.
It is a layout change, not a rewrite.

**One thing it answers well:** `collect · No pull request. Nothing is pushed.`
That is the honest answer to "what will this produce", stated once, at the top.
Keep it.

### A1.4 The agent drawer: present, absent, and present-but-unreadable

`22-detail-research.png`, `23-attempts-research.png`,
`24-detail-draft-PARKED.png`, `25-detail-review-PARKED.png`.

**Present, and the best work in the product** (see §A2):

- **Duration** — "ELAPSED / queued 2m 30s / Time spent waiting. Nothing has
  started." On PARKED: "queued 2m 41s / Parked — wall time, not work. Nothing is
  executing and no capacity is held."
- **Tokens** — "not reported / No attempt reported a token count. Not the same
  as a run that used none."
- **Cost** — "not reported / No attempt reported a cost. This is an absent
  measurement, not $0.00."
- **Checkpoints** — `0`, as a figure, caption "No attempt document lists one."
- **Attempts** — "1 / 3 allowed"; on the parked task "0 / 3 allowed" with
  "Nothing has been admitted yet — The query succeeded and returned nothing. An
  attempt document is written when capacity is reserved, so a task that is
  PARKED genuinely has none. This is a real zero, not a failed read."
- **REQUESTED VS UTILISED** — `memory peak RSS  [hatched]  — / 8.00 GiB  never ran`
  and `workspace peak … / 4.00 GiB never ran`, as two rows, not stacked.
  Correct: `apps/common/swarm_common/profiles.py:61-67` says the workspace is a
  slice *of* memory, so stacking them would double-count.
- **Attempt facts** — GEN 1, `● NEVER STARTED`, `LATEST`, backend
  `CLOUD_RUN_JOB`, attempt id, lease id.

**Absent entirely:** inputs, outputs, logs. No tab, no link, no filename — even
though §A0.2 shows both routes answer today.

**Present but unreadable — three, and the first is the worst bug in the product:**

**(a) The `DEPENDENCY_INCOMPLETE` banner is sliced through its own glyphs.**
Visible at y≈319–334 on both `24-detail-draft-PARKED.png` and
`25-detail-review-PARKED.png`: the top ~40% of each letter renders and the rest
is gone. The cause is the `.bar` collision — see §A3.1 for the box arithmetic.
The sentence being destroyed is the answer to the section's own question.

**(b) REQUESTED VS UTILISED starts at y≈940 in a 1000px viewport.** The drawer
scrolls (`.drawer { overflow-y: auto }`, `styles.css:582`), so this is not a
clip — but the panel is below the fold on every laptop, behind six metric tiles
and an attempt block. `22-detail-research.png` cuts off mid-row on the
`workspace peak` line.

**(c) `Execution` eats three of the drawer's ~20 fact lines** with
`projects/saga-agents-staging/locations/us-central1/jobs/swarm-job-eng-claude-code/executions/swarm-job-eng-claude-code-sq2l6`.
The longest string in the drawer is the one a user has no use for.

**The Attempts tab cannot answer the timing question.** `23-attempts-research.png`
prints event times through `timeAgo`: `submitted 3m ago`, `lease_acquired 2m ago`,
`dispatched 2m ago`. Minute granularity on events seconds apart. The
03:46:05 → 03:49:14 dispatch-to-starting gap measured on this platform (§A4.1)
is arithmetically underivable from this screen, though the events carrying the
absolute timestamps are on it. Each payload is a raw pretty-printed JSON block —
the `lease_acquired` blob is 11 lines of pool-name array — spending roughly
800px on three events.

**Credit:** the Attempts tab states its own truncation, on screen, today:
"3 events on this page, the oldest 3 this task recorded · no page token, so
newer events may exist and are unreachable."

### A1.5 PARKED gives half an answer, and the machine-readable half is the half lost

The drawer renders `park_reason` twice: once in the clipped banner, once as a
legible `WHY / Waiting on an earlier step in its workflow.` in amber lower down.
So the prose survives. The enum string `DEPENDENCY_INCOMPLETE` — the token you
would grep a log for or paste into a thread — appears **only** in the clipped
banner, so it is exactly the part that is destroyed.

Actionable: no. `review`'s WHY and `draft`'s WHY are byte-identical and neither
names which step. The true chain is three hops — review waits on draft, draft
waits on research, research is DISPATCHED with an attempt marked NEVER STARTED —
and assembling it takes the Workflows DAG for the edges plus three separate
drawers. The DAG knows the edge and cannot show the state detail; the drawer
knows the state detail and does not show the edge.

**The machine audit missed this entirely.**
`audit-agents-task-task_b5dc2568713a40158851.json` is the only per-task audit in
the evidence set, and `task_b5dc…` is `research` — DISPATCHED, no `park_reason`,
therefore no banner. The audit sampled the one step of three that cannot exhibit
the bug. All ten `audit-*.json` files detect `clipped-x` and `text-truncated`
only; there is no vertical-clip predicate, which is why the worst rendering bug
in the product is absent from every one of them. §B11 makes adding that
predicate part of the proof.

### A1.6 What is on screen that nobody needs, ranked

**1. The API request-receipt footer (`DataSourceStrip`).** Already measured: 16
clipped and 16 ellipsised `span.s-path` per view, URLs of ~300px natural width
rendered into 158px. Three things about it were not measured:

- **It is the same cells on every page, because it is a session log, not a page
  report.** `fetch.ts:202` is `const probes = new Map<string, ProbeRecord>()`,
  keyed by full path and never evicted; `probeSnapshot()` (`fetch.ts:261-266`)
  sorts alphabetically. `02-agents-running.png` shows `/v1/admin/tenants … 10m
  ago` and `/v1/runtimes … 10m ago` on a page that requested neither.
- **It is unbounded in cell count**, because every per-task path gets its own
  permanent key. Seven tasks produced 16 cells. Opening twenty drawers adds
  sixty more, to the footer of every page, for the rest of the session.
- **The 158px clip destroys its own identifiers.** On `03-agents-workflows.png`
  three separate cards read the identical string `/v1/tasks/task_b208fc…` with
  three different ages. They are `/v1/tasks/{id}`, `/{id}/attempts` and
  `/{id}/events`. An instrument whose entire purpose is "which route is stale"
  cannot name the route.

On `02-agents-running.png` the footer occupies roughly 195px of vertical against
the page's only content at roughly 85px. At 1024×768 (`40-workflows-1024x768.png`)
it starts at y≈600 and runs past the fold, so the last thing the page shows is a
grid of truncated URLs.

It is not worthless. `DataSources.tsx:6-17` argues it correctly — "A panel
showing a figure from four minutes ago while its route has been failing for
three of them is indistinguishable from a healthy panel unless something says
so" — and a 403 reading as information rather than breakage is a real decision.
It belongs behind one affordance, not under every page.

**2. The raw JSON event payloads** on the Attempts tab. ~800px for three events,
of which the useful content is four field names.

**3. `Execution`** in the drawer. Three lines for a Cloud Run resource path.

**4. `DEV` badges** beside every h1 and beside the drawer title, four per screen.
Nothing in this app reads an environment — `Overview.tsx:179-183` says so and
omits its own badge for exactly that reason: "on the one screen whose whole
claim is that each figure declares where it came from, a word nobody measured is
the loudest thing on it."

**5. The repeated section question.** Printed under the tab strip on every pane
of every section, so "What is running, what is waiting, what did it produce —
and why has mine not moved?" appears on all four Agents panes. It is a good
membership test for an engineer adding a screen (`App.tsx:97` and the argument
at `:49-51`); it is the same sentence four times to a user.

Nothing else on these screens is waste. The prose in the tiles is doing work.

---

## A2. What works, and what a redesign that loses it has regressed

This is not a courtesy section. The items here are the reason the product is
worth redesigning rather than replacing, and each is easy to lose to a chart
library's defaults.

1. **The absent-value copy in the drawer.** "not reported — No attempt reported
   a cost. This is an absent measurement, not $0.00." / "not reported — No
   attempt reported a token count. Not the same as a run that used none." /
   "never ran". These are the single best thing in the product. A chart library
   renders `null` as a zero-height bar by default, which is this rule broken
   before the first line of chart code.

2. **`0` as a digit beside absences as sentences.** CHECKPOINTS renders `0` as a
   figure with "No attempt document lists one."; its five neighbours render
   sentences. The one measured zero on the panel looks different from the five
   absences. That distinction is the product's best idea and it must be a
   type-level invariant in the replacement, not a convention (§B4.3).

3. **REQUESTED VS UTILISED**, with memory and workspace as separate rows, the
   track hatched, and `never ran` printed. Correct against
   `profiles.py:61-67`, correct about the hatch meaning "not a measurement", and
   correct to have no CPU row — nothing in `agent_worker/metrics.py` samples CPU,
   so an empty CPU row would imply a measurement that came back zero.

4. **"The query succeeded and returned nothing … This is a real zero, not a
   failed read."** The empty/error distinction stated in the empty state itself.
   `fetch.ts:54-60` enforces it structurally: `Result<T>` has five states and no
   member that is both a failure and an array.

5. **The self-reported truncation** on the Attempts tab: "no page token, so
   newer events may exist and are unreachable." The UI telling you what it
   cannot see.

6. **`collect · No pull request. Nothing is pushed.`** on the workflow card.

7. **The `THEN` level divider**, and the decision at `Workflows.tsx:169-176` to
   delete the vertical stalk because it rendered a fork as a chain.

8. **The tenant/platform separation** in `PlatformCounts.tsx:18-22` — never side
   by side in one row, because an admin reading their own four running tasks as
   the platform total is a truth bug.

9. **`--ctl-hatch`** (`styles.css:1214-1220`) and its comment: "Diagonal stripes
   read as 'no data here' at a glance and survive a greyscale screenshot, which
   a colour alone does not." The replacement's whole colour system (§B4.2) is
   this token generalised.

10. **The motion budget.** Five motion declarations in 1,976 lines, with
    `@media (prefers-reduced-motion: reduce)` neutralising all of them using
    `!important` so ordering cannot defeat it.

11. **`.source.info`** — a 403 for a non-admin styled as information rather than
    as an error, with the comment saying why: "Styling this as an error is what
    makes a working page look broken."

---

## A3. The system defects

Lane 2's audit, with every figure I could re-derive re-derived. Disagreements
are marked.

### A3.1 `.bar` is two components, and it blanks or slices four call sites

Declared twice, equal specificity:

```
120: .bar { height: 5px; border-radius: 999px; background: var(--surface-2); overflow: hidden; }
903: .bar { margin-bottom: 10px; padding: 9px 12px; border-radius: var(--radius);
            border: 1px solid var(--line); font-size: 12.5px; line-height: 1.5; }
```

The later wins the conflicts (`border-radius`), the earlier survives where there
is no conflict (`height: 5px`, `overflow: hidden`). With
`* { box-sizing: border-box }` (`styles.css:53`), a declared 5px height against
18px of vertical padding plus 2px of border gives a 20px border box and a **0px
content box**.

Four call sites, verified:

| Site | What it is | Result |
|---|---|---|
| `Capacity.tsx:411` | the only pool saturation meter | `.bar > i { height: 100% }` of a 0px content box — the fill never renders. `styles.css:119`'s own comment: "The bar is the whole point of the card: saturation at a glance." |
| `AgentDetail.tsx:576` | `park_reason` banner | clipped |
| `AgentDetail.tsx:592` | `blocked_by` banner | clipped |
| `AgentDetail.tsx:616` | cancellation-requested banner | clipped |

Three of the four are the UI's entire answer to "why has my agent not moved".
`redesign-v2.md` §1.1(a) derived this from the box model and said explicitly it
had not re-run a browser. `24-detail-draft-PARKED.png` and
`25-detail-review-PARKED.png` are that derivation rendered, at y≈319–334.

### A3.2 The other three collisions

- **`@keyframes pulse`** at `:160` (`.5 → .85`) and `:900` (`1 → .35`). The
  second wins globally, so `.skeleton`, authored as a subtle breathe, flashes at
  roughly three times the intended amplitude on every loading placeholder.
- **`.filters`** at `:202` (gap 6px) and `:514` (gap 14px, font-size 12.5px).
  A real duplicate with different values.
- **`.panel` has no box.** The only rule matching it in 1,976 lines is
  `.panel h2` at `:739`. Every `<section className="section panel">` has no
  border, no background, no padding. Combined with `.section { margin-bottom:
  28px }`, a "panel" is a 28px gap between runs of text. That is the wall of
  text, precisely located: the UI believes it has panels and does not have them.

**A lint gate is the right fix, and a naive one gets switched off in a week.**
`.nav button` (`:185`/`:186`) and `.row .st` (`:220`/`:543`) are deliberate
adjacent splits. Key the gate on *non-adjacent* duplicate top-level selectors,
or require an explicit `/* intentional split */` marker — otherwise it fails on
four things of which two are intentional.

### A3.3 Colour collapses to two greys in dark and one in light

I computed these myself from the tokens at `styles.css:20-24` and `:44-48`, WCAG
relative luminance converted to CIE L*. The script is reproducible from the hex
values in the stylesheet.

```
DARK                                    LIGHT
  ok          #3fb950  L* 66.77           ok          #1a7f37  L* 46.54
  warn        #d29922  L* 66.98           warn        #9a6700  L* 47.71
  info        #58a6ff  L* 66.95           bad         #cf222e  L* 45.09
  bad         #f85149  L* 58.34           info        #0969da  L* 45.94
  paused      #a371f7  L* 58.33           paused      #8250df  L* 46.73
  text-faint  #828d9b  L* 58.19           text-faint  #606a77  L* 44.41

  span across the six: 8.78 L*            span across the six: 3.30 L*
  6 of 15 pairs under the 2.3 L* JND      12 of 15 pairs under the 2.3 L* JND
```

**Disagreement with Lane 2, recorded:** Lane 2 reported "14 of 15 pairs under
2.3 L*" in light mode. I measure **12 of 15**. The conclusion is unchanged and
the count should be the measured one.

The consequence on a real screen. `styles.css:840-842`:

```css
.stackcol > i.succeeded { background: var(--ok); }
.stackcol > i.failed    { background: var(--bad); }
.stackcol > i.cancelled { background: var(--text-faint); }
```

In light mode, succeeded-vs-failed is ΔL* 1.45 and failed-vs-cancelled is
ΔL* 0.68. In dark mode failed-vs-cancelled is ΔL* 0.15.
`09-history-timeline.png` is that chart, in light mode. Both themes render a
failure and a cancellation identically in greyscale — which is the one collapse
this codebase exists to prevent.

**And every screenshot in the evidence set is light mode**, where the collapse is
total. `redesign-v2.md` §1.5 found the ok/warn/info cluster in dark and stopped
there; it missed the bad/paused/text-faint cluster — the one that makes failed
and cancelled identical — and did not compute light mode at all.

`--ctl-absent: var(--text-faint)` (`styles.css:1207`) makes them literally the
same value, so "cancelled" (an outcome), "not measured" (an absence), "idle",
"unknown", every section heading and all small print are one colour.

`--info` currently carries seven jobs: running (`.row .st.live`, `:219`),
selected tab (`.nav button.on` `:189`, `.tabs button.on` `:503`), generic
quantity (`.sr-bar > i` `:875`), informational-not-broken (`.source.info` `:688`),
the admin gate (`.state.admin-gate` `:952`) and every link (`.sub button` `:83`).
A selected tab and a live agent read alike.

### A3.4 Contrast: one hairline token, everywhere, at a third of the floor

Computed by me:

```
--line dark  #262d36  vs --surface  #14181d   1.28:1
--line dark  #262d36  vs --surface-2 #1b2027  1.18:1
--line light #d8dee4  vs --surface  #ffffff   1.36:1
--line light #d8dee4  vs --surface-2 #f0f3f6  1.22:1
```

The WCAG non-text boundary floor is 3:1. One token carries every card edge,
every table rule, the drawer border and every section rule, which is why the
tables on `10-admin-limits.png` have no visible grid.

`--warn` in light mode (`#9a6700`) measures 4.87:1 against `--surface` and
**4.37:1** against `--surface-2`, and it is the colour on `.row .why`, the
blocked-reason line. It clears AA for normal text by a margin of 0.

### A3.5 Type: hierarchy is spent

My census over `styles.css`, counting both `font-size:` and the size inside
shorthand `font:`:

```
  32  12px       17  10.5px      1  9.5px
  28  12.5px      6  13.5px      1  24px
  22  13px        6  10px        1  22px
  21  11px        5  14px        1  14.5px
  18  11.5px      3  15px
                  2  20px
  total sized declarations: 164      distinct sizes: 15
```

Twelve of the fifteen sit inside the 9.5–15px band. **71 of 164 (43%)** are at a
half-pixel step. **123 of 164 (75%)** are at or below 12.5px. A sixteenth size
(19px) is declared inside `Overview.tsx`, invisible to any lint over the sheet.

This agrees with Lane 2 exactly, and corrects `redesign-v2.md`'s "112 of 153
(73%)", which was measured on the smaller sheet.

**The hierarchy is inverted.** `.section > h2` (`styles.css:91-95`) is 12px/600
in `--text-faint`, the faintest token in the system, sitting over body text at
12.5–13px in `--text-dim`, which is more prominent. On
`04-runtimes-catalogue.png`, "SIZING 3 CLASSES" at y≈470 is quieter than the row
"standard 4 8 GiB" at y≈533 that it heads.

### A3.6 Layout: one column, no measure, uncapped bars

- `.app { max-width: 1100px; margin: 0 auto }` (`:69`). Content runs
  x=266..1334 in every screenshot at 1600px — 250px dead on each side.
- Navigation is two stacked horizontal strips, pushing the section question to
  y≈114 on all ten views.
- Nothing constrains a prose measure. The only `max-width` declarations in the
  sheet are `.node-dep` 190px, `.acct-facts` 560px, `.acct-action > *` 620px,
  `.acct-wide` 420px, `.dsp-repo` 520px and one textarea at 620px — six rules
  across three screens. Lane 2 counted lines of 157–192 characters directly off
  the screenshots (`09-history` y≈886 → 180; `08-pools` y≈613 → 187, y≈527 →
  157; `05-pools` y≈521 → 192; `01-overview` y≈409 → 159), against a comfortable
  45–90. I did not re-count characters; I verified there is no measure rule.
- **The bars are uncapped.** `.chart .col { flex: 1 }` (`:837`) makes a
  two-bucket chart draw two ~534px-wide, 120px-tall bars for seven tasks —
  `09-history-timeline.png`, y≈335..455. Same defect at `.split-row` (`:872`):
  "claude-code 5" gets a bar the width of the content column.
- **The tabs are uncapped.** `.tabs button { flex: 1 }` (`:494`) makes
  Live/Waiting/Recent three ~356px buttons for four-character labels —
  `02-agents-running.png`, y≈236.
- `.ov-cols` (`Overview.tsx:2295-2300`) is
  `repeat(auto-fit, minmax(min(400px, 100%), 1fr))`, which at a 1068px content
  area yields exactly two columns. At a full-bleed 1400px work area it yields
  three. That is the point of removing the max-width.
- The primitive is already fighting the column. `Overview.tsx:2365-2378` narrows
  `.ctl-util`'s four columns inside its cards, with a comment explaining that
  the full-width sizing "leaves the name nothing" in a half-width panel. A pane
  system sized once removes that whole class of per-screen override.

  *(Correction to Lane 2: it described `.ctl-util` as "four columns totalling
  366px fixed + 1fr" narrowed to "314px fixed". The actual declaration at
  `styles.css:1363` is `minmax(0, 1fr) minmax(90px, 160px) 78px 92px`, narrowed
  at `Overview.tsx:2374` to `minmax(0, 1fr) minmax(56px, 96px) 54px 104px`. The
  point stands; the arithmetic did not.)*

### A3.7 Density: ten rows where twenty-eight fit

Measured off `22-detail-research.png`: the Agents row runs y≈305..363 — ~58px
with two identifier lines, more with three, because `.row .agent`
(`styles.css:535-538`) stacks name, model and id vertically. At a 1000px
viewport that is about ten rows. This is an instrument: a screen showing
twenty-eight rows lets you find the outlier by scanning; a screen showing ten
makes you page.

The row already survives losing its secondary identifiers — the
`@media (max-width: 640px)` block at `styles.css:559-573` drops them and
promotes the "why" line, with a comment saying why: "someone on a phone is
checking why their agent has not moved, and that outranks every id."

**Seven independent track primitives** do one job: `.bar` 5px (`:120`),
`.coverage` 4px (`:869`), `.sr-bar` 9px (`:874`), `.acct-bar` 10px cells
(`:1046`), `.ctl-util-track` 10px (`:1383`), `.stack` 12px (`:758`), `.stackcol`
120px (`:838`). Seven implementations of one idea is why the same fact has three
visual weights on three screens.

### A3.8 The nav says one word and the page says another

The rename reached the nav and not the headings:

| Nav label (`App.tsx`) | `<h1>` (the screen) |
|---|---|
| Pools | `Capacity` (`Capacity.tsx:47`) |
| History | `Activity` (`Activity.tsx:37`) |
| Holders | `Capacity holders` (`Holders.tsx:34`) |

Visible on `05-pools-pools.png`, `07-pools-holders.png` and
`09-history-timeline.png`.

---

## A4. The infrastructure observations, end to end

These came from watching a real three-step workflow run on the live platform.
Each is marked **FIXED**, **IN FLIGHT** or **OPEN**. Three lanes are reported to
be working on the GKE 401, the wedged task and the API fields as this is
written; I have not seen their branches and cannot confirm the state of any of
the three, so IN FLIGHT here means "reported in progress", not "verified".

### A4.1 Cold start is 90% of the wall clock and is displayed as one word — OPEN

First real `claude-code` run, `task_b208fc8542724268b5f4`:

```
03:46:05  dispatched
03:49:14  starting      +3m 09s
03:49:14  running       +0.1s
03:49:32  succeeded     +18s
```

3m09s of Cloud Run cold start against 18s of agent. All of it is displayed as
the single word `dispatched`, which is the state a poller almost always
observes. No screen distinguishes "waiting for a container" from "the agent is
working".

This is the one span in the platform whose two ends are written by two different
processes: `dispatched` by the scheduler when it asks the backend
(`scheduler/store.py:243`, detail `{execution_name, backend}`), `starting` by
the **worker** as the first thing the container writes
(`apps/agent-worker/.../control.py:414`). It is therefore the only segment that
is not the control plane describing its own bookkeeping, and the only one that
measures the infrastructure rather than the application.

**Fix: §B6.5, the `AttemptDuration` component.** Not prose, not a tooltip.

### A4.2 A task read DISPATCHED for 20 minutes against a dead execution — IN FLIGHT

The UI matched the control plane exactly. The control plane did not match
reality. This is not a UI bug and cannot be fixed in the UI — but §A4.3 is the
part the UI could have shown and could not.

### A4.3 `task_to_api` serves neither `current_generation` nor `current_lease_id` — IN FLIGHT

`codec.py:87-88` decodes both. `task_to_api` (`codec.py:146-183`) serves
neither. So during A4.2 the UI **could not** have shown the
generation-2-versus-lease-generation-1 mismatch that explained the stall, even
though the fencing state was in the document the API had already read.

`types.ts:372-375` documents the absence and the arithmetic
(`current_generation > attempt_count` is exactly the fence). Route shape in
§B9.S7.

### A4.4 The GKE 401 — IN FLIGHT

Reported from the live run. I did not reproduce it and there is no artefact of
it in the repository at `8abc9c7`. Recorded so it is not lost; owned by the lane
working on it.

Adjacent and verified: `docs/contract-change-requests.md` §5 records that
`scheduler/dispatch.py:507-549` and `reconciler/backends.py:459-500` are
byte-identical copies of the GKE host normaliser, that they *did* disagree once,
and that the direction of the disagreement meant every GKE dispatch failed
**after admission had taken the lease** — so invariant 3 counted a slot held by
a task committed to a backend that would never start it. That is the failure
shape a 401 on the same path produces.

### A4.5 Termination could not be confirmed — OPEN

Reported from the live run. Not reproducible from the repository, and the UI has
no surface for it today. The route that would answer it is the backend-inventory
read (`docs/web-ui/02-cluster-state.md` Screen D, blocked on P5). Out of scope
for this build; recorded so it is not lost.

### A4.6 The events route drops the tail of any long run — OPEN, and the most dangerous

`routes/tasks.py:136-144`:

```python
def list_events(task_id, limit=None, tenant_id=..., ctx=...):
    events = ctx.store.list_events(tenant_id, task_id, limit=paged_limit(ctx, limit))
    return {"task_id": task_id, "events": [_event_to_api(e) for e in events]}
```

`order_by("at", ASCENDING).limit()`, no page token, no `order` parameter. So a
long attempt loses its **tail**: the terminal event, the last checkpoints, the
last heartbeats. And `api.ts:123` calls `${path}/events` with no limit, so it
receives `default_page_size` (50), not the 200 cap.

At roughly 1.4 events/minute once running (two checkpoint events per 120s plus
one heartbeat event per 150s) plus about six lifecycle events, 50 is reached
after roughly **31 minutes** of running. A timeline built on `/events` today
shows a long agent's beginning and implies it never finished.

Note the neighbouring route already pages: `/v1/tasks/{id}/checkpoints` takes
`page_token`. The pattern exists in the same file. Route shape in §B9.S1.

### A4.7 The five spend fields — FIXED

§A0.1. `e15bffd`.

### A4.8 Logs and checkpoints — FIXED server-side, OPEN in the UI

§A0.2. Routes and TypeScript client both exist; no screen calls either.

---

## A5. Where this document disagrees

Stated rather than silently corrected, so a reader who follows a citation knows
what moved.

**With `redesign-v2.md`:**

1. **§1.0 (F0) is fixed.** Cost is buildable now and belongs in the first batch,
   not behind a blocker.
2. **§6 S2 and half of S3 are built.** Logs and the checkpoint list are UI work,
   not platform work. The seam ranking in §B9 is re-ordered accordingly.
3. **§5.8's chart recommendation (visx + dagre) is replaced** by d3-scale +
   dagre — see §B5. The reason is not size: visx's `<BarStack>` renders an
   absent key as `height="0"` with its neighbours abutting, so the bar silently
   shortens and the gap is not drawn. That is honesty rule 6 broken by a
   component, and it is the single decision this UI cannot delegate. A library
   that ships no marks cannot make it.
4. **§8 Q6(c) ("fix light mode in a later pass") should be rejected.** Every
   screenshot anyone will read is light mode, and light mode is where the
   collapse is total (12 of 15 pairs inside the JND, versus 6 of 15 in dark).
   It is one table of six values, twice — not half the colour work.
5. **§1.5's "six state colours resolve to two greys in dark mode" understates
   it.** It found the ok/warn/info cluster; the bad/paused/text-faint cluster is
   the one that makes a failure and a cancellation identical.

**With the lanes:**

6. **Lane 2's "14 of 15 pairs under 2.3 L*" in light mode is 12 of 15** (§A3.3).
7. **Lane 2's `.ctl-util` column arithmetic is wrong**; the declaration is
   `minmax(0,1fr) minmax(90px,160px) 78px 92px` (§A3.6). The conclusion holds.
8. **Lane 2's "the footer is roughly four times the pixel area of the page's
   only content" on `02-agents-running.png` is closer to 2.3×** by my reading of
   the PNG (footer ~195px vertical against content ~85px). The footer is still
   the largest element on the page.
9. **Lane 1's "Live 0 / Waiting 3" is the state on `02-agents-running.png`
   only.** By `22-detail-research.png` the same screen reads Live 1 / Waiting 2.
   The finding — that the default tab is the one that cannot hold a stalled step
   — does not depend on which capture you take.

---
---

# HALF TWO — THE BUILD PROMPT

Everything below is instruction. It is written to be executed top to bottom from
§B10 without a clarifying question. Constraints first, because they invalidate
work rather than shape it.

---

## B1. The rules this work is constrained by

### B1.1 The frozen contract

`apps/common/swarm_common/` is **frozen**. Import from it; never redefine its
types; never edit the files. If something there needs to change, write the
request into `docs/contract-change-requests.md` and continue without it. That
file already holds five requests in the house format; match it.

Nothing in this build requires a contract change. §B9.S6 is the one item that
would, and it is explicitly out of scope.

### B1.2 The honesty rules, as testable properties

These are non-negotiable and survive any redesign. Stated here as **properties
with a test and a mutation**, because "documented" is how the last four got
broken by a stylesheet.

Each property gets a test that fails before the work and passes after, and a
mutation that the test must catch. A test that passes against the mutation has
not proved the property.

| | Property | Test | Mutation it must catch |
|---|---|---|---|
| **H1** | A failed read is never rendered as a zero. | For every screen module, no numeric render is reachable from a `Result` whose status is `error` or `stale`. | Change any screen's `status === 'ok' ? data.rows.length : null` to `: 0`. |
| **H2** | An em dash means "not measured"; `0` means a measured zero. | Every metric component, given `null`, renders the absent treatment; given `0`, renders a digit. Two fixtures, one component, both assertions. | Replace `value === null ? <Absent/> : <Figure v={value}/>` with `<Figure v={value ?? 0}/>`. |
| **H3** | A partial read says so, and names what it could not see. | Given four landed reads, one refused and two broken, the provenance line contains the count and the route name of each failure. | Drop `refused` from the provenance string; drop the route names and keep the count. |
| **H4** | A total is never shown over a partial response. | Given a state-count map with two states missing, no sum is rendered, and the withheld total carries a visible marker. | Sum the present states and render it. |
| **H5** | A clamped list says it is a window. | Given exactly `limit` rows, the window marker renders. Given `limit - 1`, it does not. | Change `rows.length >= limit` to `> limit`. |
| **H6** | A chart never interpolates across an absence. | Given a series with a null point, the generated path contains more than one `M` command (or the absence is a hatched span of real extent). | Filter nulls before building the path. |
| **H7** | An open interval has no right edge and no total. | `AttemptDuration` with no terminal event: the scale domain max equals the last measured boundary, the bar carries `data-open="true"`, and the label ends in "so far". | Pad the domain to `timeout_seconds`. |
| **H8** | A displayed id is byte-identical to the real id. | The rendered text of every element carrying an id equals the API's string; no selector that can contain an id declares `text-transform`. | Re-add `text-transform: uppercase` to `.section > h2`. |
| **H9** | A duplicate top-level selector in `styles.css` fails the build. | The gate flags a non-adjacent duplicate; it does not flag `.nav button` (`:185`/`:186`) or `.row .st` (`:220`/`:543`). | Re-introduce the `.bar` collision. |
| **H10** | A route the client calls is served, a loader the client exports is used by a screen, and the router reaches that screen. | Generalise `test_runtimes_screen.py::test_the_runtimes_route_is_read_by_a_screen_the_router_reaches` over every exported loader in `api.ts`. | Add a loader nothing calls — it must fail. (Today `loadTaskLogs` and `loadCheckpoints` are exactly this; the generalised test must be **red** at `8abc9c7`.) |

H10 is the test that would have caught §A0.2, and it must go in first (§B10
step 0) precisely because it starts red.

### B1.3 The TypeScript restatement trap

`apps/swarm-ui/src/types.ts` already hand-copies values from `swarm_common`, and
`scripts/lib/check-contract-parity.sh` reads shell and jq and **no `.ts` file at
all**. Do not add a second copy.

Concretely, and these are hard rules:

- No resource class, runner profile, backend, state enum, park reason or pool
  name may be written as a literal in any new `.ts` or `.tsx` file. Read them
  from the route.
- No chart configuration may enumerate states. Derive the set from the data.
- A development fixture may not be a copy of the frozen catalogue.
  `test_runtimes_screen.py::test_the_development_fixture_is_not_a_copy_of_the_frozen_catalogue`
  is the existing guard; extend it to every new fixture.

The one exception already in the tree is
`test_the_typescript_type_is_field_for_field_what_the_route_sends`, which pins
`types.ts` to the route's actual response. Add one of those per new type. That
is a *comparison*, not a restatement.

### B1.4 The offline gate

`tests/integration` is **offline**: it drives the real scripts end to end with a
fake `gcloud` and a fake `curl` on `PATH` under `--dry-run`, creating nothing
and needing no credentials. `make test` runs it with no directory guard,
deliberately — the Makefile's own comment says a guard that turns "I could not
find the tests" into a green run is a gate that passes hardest when it is most
broken.

Every test this build adds must run with **no cloud credentials and no
emulator**.

### B1.5 `make lint` and `make test` are their own step

Before any commit, in the repository root:

```bash
make lint        # shellcheck + doc links + terraform fmt/validate + tflint + manifests
make test        # unit + integration + terraform tests + guard and parity self-tests
```

Both must be clean. This is a step in §B10, not a formality at the end — a step
whose only work is running them and fixing what they say.

`make test` does not currently run anything in `apps/swarm-ui/`. §B10 step 0
wires the UI checks into it; after that, `make test` is the single gate.

### B1.6 Track ownership

`apps/` is Tracks A and B. `docs/` is Track D. If a change to
`apps/swarm-api/` is needed, it is §B9 and it is a **request** — write the route
shape into the seam list and build the UI against the route that exists today,
with the honest absence rendered. Do not patch another track's files.

### B1.7 Contract invariants this build must not misrepresent

From `CONTRACT.md`. Any panel showing demand, concurrency or capacity restates
these, and getting one wrong is a truth bug:

- Only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create infrastructure demand.
  `QUEUED`, `PARKED`, `READY` cost nothing and must never be drawn as a backlog.
- Concurrency counts from `LEASED`, not `RUNNING`.
- Capacity is all-or-nothing across every pool, so a task's ceiling is the
  **minimum** across its pools. Raising a non-binding pool changes nothing.
- Spot is disabled platform-wide; `requests == limits`, so there is no burst
  headroom to draw.
- A paused pool is not a full one. Compare `enabled === false` explicitly —
  `.enabled // true` reports a paused pool as open, and it has broken this exact
  column once already.

---

## B2. The target information architecture

**Six nouns, unchanged.** Overview · Agents · Runtimes · Pools · History ·
Admin. `App.tsx:90-190` carries the structure and the `question` field that is
the membership test for any new screen. The owner decided this on 2026-09-21; it
is not reopened.

Two changes, both consequences of the shell rather than renames:

- **`AttemptTimeline.tsx` stops being a screen** and becomes a view mode inside
  the agent inspector. It answers no question that "what is running, what did it
  produce" does not already own.
- **`DataSources.tsx` moves into the dock** as a provenance strip behind one
  affordance. "Which of these reads succeeded" is context for everything on
  screen, not a destination — and §A1.6 is what it costs as a footer.

**The headings are brought into line with the nav** (§A3.8): `Capacity` → Pools,
`Activity` → History, `Capacity holders` → Holders. Six words; the rename was
half-applied.

**The section question moves.** It is printed once, in the `?` card for the
section (§B7), not under the tab strip on every pane.

---

## B3. Layout: regions, with widths

A CSS grid on `.app`, replacing `max-width: 1100px`.

```
grid-template-columns: [rail] 200px [work] minmax(640px, 1fr) [inspector] 480px;
grid-template-rows:    [head] 44px  [body] 1fr                [dock] 28px;
```

### RAIL — 200px fixed

Never reorders, so after a week you go to a position rather than reading a
label. Carries the six sections and, indented 12px, each section's tabs as a
second level. This deletes both horizontal strips (`.ctl-nav` `:1637`,
`.ctl-subnav` `:1700`) and recovers 64px of vertical on every screen.

200px fits "Runner profiles" at 13px. Collapses to 56px icon-only below 1280px;
becomes the current top bar below 900px.

### WORK AREA — fluid, `min-width: 640px`, **no max-width**

A 12-column grid, 16px gutter. `--measure: 72ch` applied to **prose blocks
only**. Tables, charts and DAGs are full-bleed; sentences never are.

A control plane is not a document. The evidence is `.ov-cols` yielding two
columns at 1068px and three at 1400px (§A3.6), and `Overview.tsx:2365-2378`
already overriding a shared primitive because the column starves it.

### INSPECTOR — right pane, 480px default, resizable 400–720px

A **grid column**, not `position: fixed`. The work area reflows to
1600 − 200 − 480 − 2 = 918px and the list stays on screen. Width persisted per
viewer in `localStorage`, read inside `try/catch`, with 480px on any failure.

Reverts to today's overlay (`role="dialog"`) only below 1100px total width,
where two panes genuinely do not fit.

This is the fix for "reading one agent takes the others off the screen", and it
is the largest single change in the build. It is sequenced late (§B10) for that
reason.

### DOCK — bottom, 28px collapsed, expands to 240px, resizable 120px–70vh

Owns, in order of arrival: the provenance strip (one line collapsed —
`16 reads · newest 9s · 0 failed · 1 admin-only`), then logs, then artifact and
checkpoint previews. It survives navigation within a session.

### HEAD — 44px, spanning work + inspector

Breadcrumb (section ▸ tab ▸ object), refresh control with the read age, and the
screen's single `?`. No environment badge until the API reports its own
environment (§B9.S8) — `Overview.tsx:179-183` already made that decision and it
is right.

---

## B4. The visual language, with values

### B4.1 Type — six integer steps

```css
--t-micro:  11px;  line-height 1.45   /* ages, raw ids, provenance, table caption. Hard floor. */
--t-meta:   12px;  line-height 1.45   /* column headers, chip text, labels: 600, uppercase, 0.06em */
--t-body:   13px;  line-height 1.50   /* table cell, body, inspector kv. The workhorse. */
--t-lead:   15px;  line-height 1.55   /* the one prose paragraph a screen is allowed */
--t-title:  18px;  line-height 1.30   /* panel title and screen h1 */
--t-figure: 28px;  line-height 1.10   /* the one number a metric tile exists for */
```

Delete 9.5, 10, 10.5, 11.5, 12.5, 13.5, 14, 14.5, 19, 20, 22 and 24. The
half-pixel steps go because at 12px versus 12.5px on a 1× display the cap
heights round to the same value: the distinction costs a token and buys nothing,
while being real as rendering inconsistency across DPI.

**Fix the inversion.** `.section > h2` becomes `--t-title` / 600 in `--text`.
The uppercase small-caps treatment drops one level, to panel labels only.

**Hierarchy channels, in priority order**, because size is already spent:

1. **Weight + case + tracking on the label, full size on the value.** Already
   the house pattern and the best thing in the sheet (`.ctl-metric-label`
   `:1275`, 600/uppercase/0.05em, over `.ctl-metric-value` at 24px/600 tabular).
   Keep it; move the label to `--t-meta`.
2. **A visible hairline and a surface step, not a margin.** With `--line` at 3:1
   (§B4.3), one rule does the work 28px of margin was doing badly. On
   `04-runtimes-catalogue.png` the "This is the dispatch topology…" paragraph
   sits 19px above the BACKENDS heading and reads as its lead-in while belonging
   to the section above; that is whitespace being asked to carry structure.
3. **Position as a contract.** Identity left, figure right,
   `font-variant-numeric: tabular-nums` declared once on `:root` instead of the
   nine places it appears now.
4. **Colour, reserved exclusively for state**, so the eye can use it as a scan
   channel instead of learning to ignore it.

### B4.2 Colour — three tiers, and a testable rule

**The rule, and it is the acceptance test:**

> Render the page in greyscale. If any two states become the same mark, the
> treatment is incomplete.

Greyscale rather than a colour-blindness simulation, because it also covers the
screenshot pasted into an incident channel, which is how these screens are
actually read.

**Tier 1 — STATE.** Six values. The only hues permitted inside a data region.
Each carries a second, non-colour channel **and** a mandatory word:

| State | Second channel |
|---|---|
| `ok` | solid fill, filled dot |
| `live` | solid fill + the only animation on the screen |
| `warn` | 45° hatch — `--ctl-hatch` (`styles.css:1214`) already does this correctly for unknown ceilings; extend it |
| `bad` | solid fill + a 2px left rule |
| `paused` | **horizontal** hatch, deliberately distinct from `warn`'s 45° |
| `absent` | dotted outline, hollow dot, **no fill at all** |

Retune luminance so the six occupy at least four distinct L* steps, each ≥6 L*
apart. Dark targets: `bad` 52, `paused` 44, `absent` 58, `ok` 62, `live` 70,
`warn` 76. Invert the ordering in light. That is measurable, and §B11 makes it a
test.

**Split `--ctl-absent` off `--text-faint`.** `styles.css:1207` makes them
literally the same value, so "cancelled" (an outcome), "not measured" (an
absence), "idle", "unknown", every section heading and all small print are one
colour. They are different facts.

**Tier 2 — INTERACTION, and it must be hueless.** `--info` carries seven jobs
today (§A3.3), so a selected tab and a live agent read alike. Selection becomes
a 2px rule plus one surface step; focus a near-white 2px outline; links
underline-on-hover. Cheaper to keep honest than a second accent.

**Tier 3 — SURFACES.** Three steps plus one hairline at ≥3:1.
`--line` → `#39424e` dark (3.06:1 against `--surface`) / `#b9c0c8` light. Delete
`--ctl-shadow` in dark: `0 1px 2px rgb(0 0 0 / .30)` on `#0b0d10` is invisible
and costs a paint layer on every `.ctl-metric`.

**Light mode is fixed in the same pass.** Not a later one. Every piece of
captured evidence is light mode and light mode is where the collapse is total
(§A3.3, §A5.4).

### B4.3 Density and spacing

Target **36px rows, one line**. At a 1000px viewport that is about 24 rows
against today's ten. Secondary identifiers move to the row's hover card and the
inspector; the `@media (max-width: 640px)` block at `styles.css:559-573` already
proves the row survives losing them.

36px rather than 32px, because 32px requires the identifiers to be *only* in the
hover card, and a hover card is unavailable on a touch device and invisible in a
screenshot. 36px keeps a glyph plus a short label inline.

Six moves, each with its citation:

1. Replace `.section { margin-bottom: 28px }` (`:90`) with hairline-separated
   regions at 12px.
2. **Collapse the seven track primitives into one** (§A3.7): one `--track-h:
   8px`, one radius convention, one colour rule. This is the same work as
   building a chart layer for the eight proportional panels (§B5), and it
   arrives with the hatch semantics already argued in the stylesheet.
3. **Cap the bars.** `.chart .col { flex: 1 }` (`:837`) → `max-width: 72px`,
   left-aligned. Same at `.split-row` (`:872`).
4. **Stop spending 1068px on four-character labels.**
   `.tabs button { flex: 1 }` (`:494`) → `flex: 0 0 auto`.
5. Remove `.app`'s max-width so `.ov-cols` reaches three columns.
6. Spacing floor: 36px row, 12px inter-region, 8px intra-panel, 4px intra-row.
   Four steps. `--ctl-s1..s5` (`:1190`) declares five; drop `--ctl-s4: 18px` —
   it is 1.5× `s3` and nobody can tell.

### B4.4 Motion

Keep the current budget, which is right: `@media (prefers-reduced-motion:
reduce)` neutralises everything with `!important` so ordering cannot defeat it.

Exactly four permitted animations:

1. the live-state pulse, and **only while the thing is genuinely live**;
2. a meter's width transit;
3. the skeleton, at the original 0.5 → 0.85 amplitude (the `:900` keyframe block
   that overrides it is deleted with the collision);
4. **the open segment of an in-flight duration bar** — one
   `stroke-dashoffset` animation, applied only while the segment is genuinely
   open. It is the cheapest way to make a static screenshot distinguishable from
   a live one, and it is the visual form of H7.

---

## B5. The charting decision

**Dependencies, exact:**

```
d3-scale        4.0.2   ISC    +12.6 KB gzip
@dagrejs/dagre  3.1.1   MIT    +16.0 KB gzip
                               +29.1 KB gzip measured together
```

Measured by Lane 3 as real Vite 5.4.11 / React 18.3.1 / esbuild-minify bundles,
gzip level 9, against a react + react-dom baseline of 44.6 KB. **I did not
re-run those builds.** Licences and publish dates came from the npm packuments.

Install the **scoped** dagre. The unscoped `dagre@0.8.5` last published
2019-12-03 and is dead.

### What `d3-scale` buys, and what it deliberately does not

It buys `scaleLinear`, `scaleUtc`, `scaleBand`, and `.ticks()` / `.tickFormat()`.
Tick-interval selection over a timestamp or duration domain is the one genuinely
hard piece of arithmetic in every axis-bearing panel, and the one worth not
hand-rolling: "nice" intervals that step seconds → minutes → hours without
producing 7-second gridlines is a solved problem you will get subtly wrong, and
a duration axis with wrong tick intervals misleads about magnitudes — the exact
failure class this codebase exists to prevent.

It buys **not one mark**. Every rect, path, pattern and label is ours, so no
component anywhere in the product can decide what `null` looks like.

That is the decision, and it is why this replaces `redesign-v2.md` §5.8's visx
recommendation. Rendered to static markup, visx 4.0.0's `<BarStack>` with a null
key emits:

```
<rect y="166.667" height="33.333" fill="#a11">
<rect y="166.667" height="0"      fill="url(#vhatch)">   <- the unknown
<rect y="66.667"  height="100"    fill="#11a">
```

The `<pattern>` is emitted and referenced, so the hatch works. What fails is
that the unknown gets height 0 and its neighbours **abut** — segment 1 ends at
y=166.667 and segment 3 begins at y=166.667. Nothing renormalised; the absence
was simply not drawn, and the bar silently shortened. Recharts' default
`stackOffset` is `"none"`, so it does not renormalise either. **This is not a
library-capability question.** No stacking function can give an absence extent,
because an absence has no value to hand it.

The requirement is therefore satisfied in the **data layer**, by one rule:

> **The total comes from the attempt document. The split comes from the events.
> The residual is the hatch. When there is no total, the bar has no right edge.**

`/v1/tasks/{id}/attempts` gives `created_at` and `completed_at` independently of
the event stream, so when `completed_at` is non-null the unknown's extent is
`total − sum(measured segments)` and a hatched span of real height can be drawn.
When `completed_at` is null there is no total, and the only honest rendering is a
bar with no terminus and no axis maximum past it — which no charting library
offers at any price, because every stacked bar has a fixed domain.

### What `@dagrejs/dagre` buys

`Workflows.tsx:80` (`levelsOf`) already computes rank. Dagre is not for that. It
is for what `levelsOf` does not do: in-rank ordering and edge routing, which is
what makes a graph legible once `input_from` edges cross ranks. Today
`.level-steps { justify-content: center }` orders siblings by array index, so
crossing edges are unavoidable at four nodes.

**Rejected, with the measured reason:**

- `elkjs` +428.5 KB — 27× dagre — and **EPL-2.0 OR GPL-3.0-or-later**, the only
  non-permissive licence surveyed. Its layered algorithm genuinely beats
  dagre's, for hundreds of nodes. A workflow here has three.
- `@xyflow/react` +61.0 KB, plus a pan/zoom canvas, its own stylesheet, node
  components that own their rendering (the one decision the honesty rules cannot
  delegate), and `zustand` as a second state store into an app with none.
- `recharts` +105.2 KB, 11 runtime dependencies including `@reduxjs/toolkit`,
  `react-redux` and `immer`.
- `echarts` +163.2 KB tree-shaken, +369.8 KB as a barrel import.

### The eight panels that need no library at all

Eight of the sixteen visualisations need no scale, no axis and no SVG, and the
repository already ships an honest primitive for them: `.ctl-util-track` /
`.ctl-util-fill` / `.ctl-util-over` with `--ctl-hatch` and `--ctl-hatch-bad`
(`styles.css:1214-1227`, `:1378-1400`). Its own comment states the rule:

> `.is-unknown` no ceiling, or it could not be read. Hatched, NO fill. An
> unfilled plain track reads as "0% used", which is a claim.

It is what draws REQUESTED VS UTILISED in `22-detail-research.png`.

Those eight: attempt phase breakdown, requested-vs-peak memory, pool saturation,
concurrency by pool, task-state distribution, per-commit diffstat, outcome mix,
cost/tokens per attempt.

**So the real shape:** one CSS meter component for the eight proportional
panels; `d3-scale` plus own marks for the five that need a continuous axis
(workflow Gantt, peak-memory step line, checkpoint strip, per-attempt lollipop,
account quota gauge); `@dagrejs/dagre` for the one graph. Collapsing the seven
existing bar primitives (§B4.3 move 2) into that one meter is the same work as
building a chart layer for them.

### If the dependency count is fixed at zero

Keep hand-rolling for the DAG, refuse it for the axes.

- **DAG:** keep `levelsOf` and add one barycentre ordering pass — order each
  rank by the mean rank-index of its parents, two or three iterations. About 40
  lines, no dependency, and for a handful of steps it gets most of dagre's
  readability. Straight-line edges, no spline routing.
- **Axes:** do not hand-roll `ticks()`. Ship the eight proportional panels on
  the existing CSS primitive, which needs no scale, and **hold** the five
  axis-bearing panels rather than approximate them.

---

## B6. The screens, in dependency order

Each carries: what it answers, its data sources by route and field, its panels,
what it must **not** imply, and how it renders each honesty case.

Legend for data: all routes exist at `8abc9c7` unless marked **[seam]**.

---

### B6.0 — The defect batch (no new screen)

**What it answers:** nothing new. It makes the current answers legible.

The eleven items in §B8, applied to `styles.css` and four `.tsx` files. Nothing
in this batch changes an information architecture, so it can land first and
ship on its own.

**Why first:** three of the four `.bar` call sites are the UI's only answer to
"why has my agent not moved", and the fourth is the only saturation meter on the
Pools screen. That is worth fixing whether or not the rest of this lands.

---

### B6.1 — The shell (rail, work area, dock; inspector deferred)

**What it answers:** where am I, what did this screen read, and is it fresh.

**Data:** `probeSnapshot()` from `fetch.ts:261`; `App.tsx:90-190` for the
section and tab structure.

**Panels:** rail, head (breadcrumb + refresh + age + `?`), dock collapsed to one
line.

**Must not imply:** that the provenance line covers this page's reads. It is a
session log (§A1.6). The collapsed line must say so — `16 reads this session ·
newest 9s` — and the expanded dock groups by *this view's* routes first, with
the rest under "other reads this session".

**Honesty cases:**
- A route that has never succeeded shows no age, not "just now".
- A 403 renders as information, with the word "admin only", not as an error.
  `DataSources.tsx:15-17` is right and must survive.
- A failure must not erase the age of the last good payload — `fetch.ts:219-222`
  already handles this and the new strip must keep it.

**Deferred:** the inspector column. It lands in B6.4 so that exactly one screen
is being converted at a time.

---

### B6.2 — Help, and the `?` affordance

Specified in full in §B7. It is built second because every screen after this
one moves a sentence into it, and building it late means the sentences get
deleted instead of moved.

---

### B6.3 — Agents (the list)

**What it answers:** "What is running, what is waiting, what did it produce —
and why has mine not moved?" (`App.tsx:116`, verbatim).

**Data:** `GET /v1/tasks?limit=200` → `id`, `state`, `runner_profile`,
`resource_class`, `submitted_by`, `created_at`, `updated_at`, `attempt_count`,
`max_attempts`, `park_reason`, `blocked_by`, `workflow_id`, `step_id`,
`next_eligible_at`, `cancel_requested`.
`GET /v1/admin/leases?active_only=true&limit=200` for the holding-a-slot count.

**Panels:**

1. **Tab strip** — Live / Waiting / Recent, `flex: 0 0 auto`, badge counts from
   the **rows**, never from `/v1/stats` (that is already the rule at
   `Agents.tsx:35`, and it is why the badge and the list can never disagree).
   **The landing tab is the first non-empty one**, left to right. This is the
   fix for §A1.2 and it is four lines.
2. **The row, 36px, one line:** state glyph + word · profile · step chip ·
   **id prefix** · owner · elapsed · duration bar (B6.5) · why.
3. **Filters** — profile, and a "mine" toggle defaulting on.

**Must not imply:**
- That an empty Live tab means nothing exists. The empty state names which tabs
  have rows: "Nothing is holding a slot. 3 waiting, 4 recent."
- That the list is complete. `api.ts:31` records the server clamps at 200 (H5).
- That `QUEUED`/`PARKED`/`READY` are a backlog (B1.7).

**Honesty cases:**
- Read failed → the row area renders the error, never zero rows (H1).
- Read empty → "This is a real zero from a successful read", which the current
  screen already says correctly.
- 200 rows returned → the window marker (H5).

**The id fix (H8).** `task.id.slice(-8)` becomes the **prefix after the
underscore**: `task_b5dc2568713a40158851` → `b5dc2568`. A prefix is what a search
matches and what the receipt footer prints. Pair it with click-to-copy on the
full id.

**The workflow handle.** The step chip carries `step_id` today and the workflow
id only in a `title`. Put both on the row: `RESEARCH · wf_bcdc9180`. Three rows
reading RESEARCH/DRAFT/REVIEW from two different workflows must be assignable
without hovering.

---

### B6.4 — The agent inspector (the drawer becomes a pane)

**What it answers:** everything about one attempt of one task.

**Data:**
`GET /v1/tasks/{id}` (identity, state, `park_reason`, `blocked_by`,
`next_eligible_at`, `latest_checkpoint`, `result_summary`);
`GET /v1/tasks/{id}/attempts` (per-attempt `created_at`, `started_at`,
`completed_at`, `exit_code`, `error`, `peak_rss_bytes`, `peak_disk_bytes`,
`oom_near_miss`, `checkpoints[]`, `generation`, `lease_id`, `backend`,
`execution_name`, and — since `e15bffd` — `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`, `cost_usd`);
`GET /v1/tasks/{id}/events`;
`GET /v1/resource-classes` (the requested side).

**Panels, in this order:**

1. **Identity and state.** Full id, copyable. Profile, class, tenant, owner,
   workflow + step, attempt counter.
2. **The three banners** — `park_reason`, `blocked_by`, cancellation-requested —
   as `.notice`, not `.bar` (§B8.1). The enum string is rendered **beside** the
   sentence, not instead of it: `DEPENDENCY_INCOMPLETE — Waiting on an earlier
   step in its workflow.` The enum is what gets grepped; the sentence is what
   gets read.
3. **`blocked_by`, resolved.** The banner names the step it waits on **and that
   step's state**: "Waiting on **draft**, which is PARKED on DEPENDENCY_INCOMPLETE."
   The data is on the workflow the list already loaded. This is the fix for the
   three-hops problem in §A1.5.
4. **`AttemptDuration`** (§B6.5), the first thing above the fold after the
   banners.
5. **REQUESTED VS UTILISED**, promoted **above** the metric tiles. It is the
   panel the brief asked for and it is currently at y≈940 in a 1000px viewport
   (§A1.4b).
6. **Metric tiles** — elapsed, attempts, peak memory, tokens, cost, checkpoints.
   Copy unchanged (§A2.1, §A2.2).
7. **Attempts**, one block per attempt, newest first.
8. **Work produced** — `result_summary.git`.
9. **Inputs** — `result_summary.staged_inputs[]`.
10. **Outputs** — `GET /v1/tasks/{id}/artifacts`.

**Must not imply:**
- That `completed_at − started_at` is the agent's work. `control.py:410-413`
  overwrites `task.started_at` on every attempt, so on a retried task it is the
  *last* attempt's start. Only the sum of per-attempt intervals is work, and the
  panel does the summing and states it separately.
- That memory and disk add. `profiles.py:61-67`: the workspace is a slice **of**
  memory. Two rows, never stacked.
- That there is a CPU row. Nothing in `agent_worker/metrics.py` samples CPU.
- That `peak_rss_bytes: null` is 0.
- That a missing pull request is "none" — `types.ts:494-500` names six distinct
  causes and each is a different operator response.
- That `patch_omitted` means no patch. It means a patch existed and was
  discarded at the cap.
- That `binary_files` is a 0/0 line count. Git prints `-` for both; the field
  exists precisely so a binary change does not read as "changed nothing".
- That an empty artifact list means none were produced. `complete` is false
  until the task is terminal.
- That a task with no spend figure was free (see the coverage rule below).

**Honesty cases:**
- `null` cost/tokens → the existing sentence treatment, unchanged.
- `0` checkpoints → a digit, unchanged.
- **Spend coverage.** `record_spend` has one caller, `lifecycle.py:694`, and the
  comment fourteen lines above it says it is "the ONLY call that passes
  `publish=True`. The agent exited on its own here; the other five call sites
  are parks and crashes." A quota park, a cancel, a SIGTERM, a crash and a
  generation fence all record **nothing** — and those are the expensive
  attempts. Any rolled-up cost figure must carry "not recorded for N of M
  attempts". `09-history-timeline.png` already does this correctly at the
  history scale ("1 of 7 · 14% of rows carry usage") and the inspector must
  match.
- Only `claude-code` and `codex` emit a usage block at all; `mock`, `generic`
  and `browser` report nothing. That is an absence, not a zero.

**Truncation:** `Execution` collapses to the execution's last segment
(`swarm-job-eng-claude-code-sq2l6`) with the full path on click-to-copy. Three
lines of a Cloud Run resource path is the longest string in the drawer and the
one nobody uses.

---

### B6.5 — `AttemptDuration`

The component that answers §A4.1. One horizontal segmented bar **per attempt**,
stacked as rows when a task has several. Never per task, for the
`task.started_at` reason above.

**Inputs:** the attempt row from `/v1/tasks/{id}/attempts`, and the events for
that `attempt_id` from `/v1/tasks/{id}/events`. Every event carries
`attempt_id`, `lease_id` and `generation` (`_event_to_api`,
`routes/tasks.py:33-44`), so the grouping is the API's, not a guess.

**The segments, and who writes each boundary:**

| # | Name | Left | Right | Written by |
|---|---|---|---|---|
| 0 | blocked | `submitted` | `ready` | swarm-api → scheduler |
| 1 | waiting for a slot | `ready` \| `submitted` | `lease_acquired` | scheduler |
| 2 | asking the backend | `lease_acquired` | `dispatched` | scheduler (both) |
| 3 | **cold start** | `dispatched` | `starting` | scheduler → **worker** |
| 4 | starting up | `starting` | `running` | worker (both) |
| 5 | running | `running` | terminal | worker |

Segment 3 is the finding. It is the only span whose two ends are written by two
different processes, and it is exactly "how long the backend took to give us a
container". In the measured run it was 3m09s against 18s of agent.

**Overlays, not segments** (they must not consume width in the stack):

- `heartbeat` — tick marks inside segment 5. Detail
  `{elapsed_seconds, peak_rss_bytes, checkpoints}` (`lifecycle.py:1471-1479`),
  one per 150s (`heartbeat_interval_seconds` 30 × `HEARTBEAT_EVENT_EVERY` 5).
- `checkpoint_started` → `checkpoint_completed` — paired sub-spans inside
  segment 5. Completion detail `{checkpoint_id, uri, size_bytes, seq}`
  (`control.py:479-482`). **An unpaired `checkpoint_started` is normal, not a
  stall:** a failed checkpoint must never end the attempt, so it retries at the
  next interval. Draw it as an open sub-span with its own marker, never as a
  hang.
- `quota_exhausted`, `parked`, `retrying`, `generation_fenced`,
  `lease_released` — boundary markers that terminate the attempt.

**The four missing-event cases, which are four different facts:**

**(a) OPEN AND LIVE.** Task non-terminal, next boundary not yet written. Extent
is `now − left`. The segment runs to the bar's right edge with **no right
edge** — a serrated or fading terminus, no axis maximum past it, no tick beyond
the last measured boundary. The label reads "3m 09s so far", never "3m 09s".
*This is the 20-minute DISPATCHED case.*

**(b) OPEN AND DEAD — a known total with an unknown split.** A terminal event
exists but an intermediate one was never written (a worker killed before
`starting`, so `dispatched → failed` with nothing between). The interval's
extent **is** known from the attempt document; only its division is not. Draw
**one hatched span covering the whole interval**, labelled with the two events
it lies between: "between dispatched and failed · 18m 22s · not broken down."
**Do not distribute the interval across the segments that should have been
there** — that invents measurements.

**(c) ABSENT BY CONSTRUCTION — draw nothing.** Segment 0 does not exist for a
task submitted straight to READY. `swarm_api/service.py:5` says so plainly: "a
submitted task is written straight to READY (or PARKED), never to a QUEUED
state", and `promote_to_ready` (`scheduler/store.py:227`) — one of only two
`ready` emitters — fires on a park release. So no `ready` event means "there was
nothing to be blocked on", not "the event is missing". **The discriminator is
served:** `submitted.detail.state` carries the initial state, visible as
`"state": "READY"` in `23-attempts-research.png`. Read it, and case (c) never
falls into case (b). A segment that does not exist is drawn as nothing — not as
zero, not as hatched.

**(d) TRUNCATED PAGE — degrade to unknown, never to finished.** If
`events.length === limit`, the page is a window and the last segment is drawn as
case (a) open, with the marker the drawer already prints today. Until §B9.S1
lands this is the common case for any run over ~31 minutes (§A4.6), and it is
what stops a long agent rendering as one that never finished.

**Three structural rules for an in-progress segment:**

1. The scale domain is `[0, max over the drawn segments]`, recomputed on every
   poll. **Never** `[0, timeout_seconds]` — padding to the timeout draws a total
   nobody measured, and `timeout_seconds` is a ceiling, not an expectation.
2. **No axis tick is drawn to the right of the last measured boundary.** A tick
   past the data is a claim about how much is left.
3. The open segment's right edge is the bar's right edge, rendered as a terminus
   glyph, so an open bar and a closed bar are distinguishable in a static
   screenshot with no colour and no hover.

**Scale: linear, always.** 3m09s against 18s is 10.5:1, so on a 200px bar the
agent's work is about 5px. That ratio **is** the finding. A log axis makes two
bars incomparable, which is the whole point of drawing them.

**Do not use `scaleTime` for it.** The domain is elapsed milliseconds, not wall
clock, and `scaleTime` places ticks on wall-clock boundaries. Use `scaleLinear`
over ms with a hand-written `tickFormat` (`s` / `m s` / `h m`). Use **`scaleUtc`**
— not `scaleTime` — for the workflow Gantt, since every timestamp in this
platform is `utcnow()`.

---

### B6.6 — Workflows

**What it answers:** the shape of the work, where each step is, what each one
produced, and which step is blocking the rest.

**Data:** `GET /v1/workflows?limit=100` → `workflow_id`, `steps[]` with
`step_id`, `depends_on`, `input_from`, `runner_profile`;
`GET /v1/tasks?limit=200` joined by `workflow_id` for live state.

**Panels:**

1. **Header.** The workflow id at `--t-title`, **not uppercased**, in `--text`,
   beside the **computed rollup** (`0/3 done`). `workflow.state` is either
   dropped or struck the way `.rollup.untrusted` already strikes an
   untrustworthy count — it is written once at creation and never updated
   (§A1.3), and printing a dead field beside a live one in the same typeface is
   a truth bug.
2. **The DAG.** `dagre` for in-rank ordering; keep `levelsOf`'s ranking and keep
   the `THEN` level divider and the `← dependency` line on the node
   (`Workflows.tsx:169-176` is right).
3. **Each node is a link.** `StepNode`'s `<div>` becomes an `<a>` whose href is
   the task route. The id is already in scope at `Workflows.tsx:279` inside
   `p.title`. This is the cheapest change in the build and it converts the
   workflow view from a picture into the product's entry point.
4. **The node carries state detail:** state word, elapsed, and the park reason
   when parked. Left-align the DAG; remove `align-items: center` and
   `justify-content: center` (`styles.css:239-259`) — that is 430px of empty
   page at 1024px (§A1.3).
5. **`collect · No pull request. Nothing is pushed.`** unchanged.
6. **View modes**, once B6.5 exists: Graph / Timeline / Table. The mode is a
   property of the pane, not a route — the same nodes on a UTC Gantt answer
   "where did the four hours go" instead of "what depended on what".

**Must not imply:**
- That an unconnected node is orphaned. It is independent.
- That edge direction is execution order beyond what `depends_on` declares.
- That a `depends_on` edge and an `input_from` edge are the same thing. One is
  ordering, one is data. Distinguish them by stroke.
- That a Gantt bar's length is agent work. It includes queue and parks.
- That a step still running has an end.

**Honesty cases:**
- A step whose task could not be read renders as `unknown`, hollow, and the
  rollup is **suppressed** — `Workflows.tsx:111-115` already does this
  ("Counts are SUPPRESSED when any step state is unknown") and it must survive.
- A workflow with no matching tasks renders the shape with every node
  `unknown`, not with every node `queued`.

---

### B6.7 — Overview

**What it answers:** "Is the platform healthy right now, and if not, what is the
first thing to look at?" (`App.tsx:105`).

**Data:** the seven reads it fans out today, plus nothing new.

**The change: a seventh check.** `deriveChecks` (`Overview.tsx:1807-1815`) gains
`parkedCheck(s.tasks)` — "work of yours is parked, by reason". The data is on
the task page Overview already loads; no new route. This is the fix for §A1.1,
and the tile's own self-report becomes "7 of 7 checks ran".

RUNNING NOW gains a companion line when the check fires: "Nothing is holding a
slot. **3 of your steps are parked** — 2 on DEPENDENCY_INCOMPLETE." The current
sentence is true and must stay; what it needs is the sentence beside it.

**Must not imply:**
- That "nothing is running" means nothing exists (the whole finding).
- That the six-becomes-seven checks are exhaustive. The tile already prints its
  own scope; keep that and update it.
- That `QUEUED`/`PARKED`/`READY` create demand.

**Honesty cases:** `Overview.tsx` already fans out seven independent reads and
counts what landed, what is in flight, what failed and what was refused, with
each panel failing alone. That is H3 implemented; do not rewrite it, move it
into the head's provenance control.

---

### B6.8 — Pools

**What it answers:** "Is there room to run more, which ceiling is the binding
one, and what is holding what there is?" (`App.tsx:155`).

**Data:** `GET /v1/capacity` → `pools[]` with `in_use`, `hard_limit`,
`adaptive_target`, `enabled`; `GET /v1/admin/leases?active_only=true`.

**Panels:** saturation meter per pool (the one that currently does not render at
all, §A3.1), two ceiling markers, holders list.

**Must not imply:**
- **That a paused pool is at its ceiling.** Compare `enabled === false`
  explicitly. Hatch a paused pool; do not fill it.
- That concurrency counts from RUNNING. It counts from LEASED.
- That pending pods are a backlog.
- That raising a non-binding pool changes anything — a task clears **every**
  pool at once, so its ceiling is the **minimum**. The current screen says this
  well and must keep saying it.

**Honesty case:** a pool whose limit could not be read renders `not read` —
"It is not uncapped and it is not empty: nothing is known about it, and it may
be the one refusing." `Profiles.tsx:214` already has this string; it is the
model for the whole build.

**Heading:** `Capacity` → `Pools`; `Capacity holders` → `Holders`.

---

### B6.9 — Runtimes

**What it answers:** "What kinds of agent can this platform run, where does each
one run, and how big is one?" (`App.tsx:147`).

**Data:** `GET /v1/runtimes`, `GET /v1/resource-classes`, `GET /v1/capacity`.

**Must not imply:** live utilisation. A runtime is a *spec*; utilisation belongs
to an attempt or a pool. The three reads must not be pooled into one failure —
`api.ts:455-465` already argues this and the existing screen is correct.

**Do not touch the catalogue-parity property.**
`test_runtimes_screen.py::test_the_screen_restates_no_value_from_the_frozen_catalogue`
and `::test_a_catalogue_that_gains_entries_needs_no_edit_here` are the guards
that make this screen honest; the re-layout must leave both green.

---

### B6.10 — History

**What it answers:** "What has this platform done over time, who used it, and
what did it cost?" (`App.tsx:176`).

**Data:** `GET /v1/tasks` paged; `GET /v1/stats` (admin).

**Panels:** outcome mix by day (bars capped at 72px, left-aligned), stat tiles,
runner-profile split, people table, spend.

**Must not imply:**
- That failed and cancelled are the same thing — they render identically in
  greyscale today (§A3.3) and B4.2 fixes it.
- That the two scopes are comparable in one row. `PlatformCounts.tsx:18-22`: an
  admin reading their own four running tasks as the platform total is a truth
  bug.
- That the window is everything. "Grouped client-side over the 7 rows in the
  window. There is no server-side filter or index on `submitted_by`, so this
  cannot be a per-engineer query — and an engineer whose work fell outside the
  window is absent rather than shown as zero." That sentence, visible on
  `09-history-timeline.png`, is exactly right and must survive.
- That the spend total is complete. "1 of 7 · 14% of rows carry usage" stays.

**Nothing on this screen auto-refreshes.** `/v1/stats` bills per index entry
scanned and gets more expensive as the platform ages
(`PlatformCounts.tsx:9-17`).

---

### B6.11 — Admin

**What it answers:** "Change a ceiling, or see who is registered to use this
platform." (`App.tsx:185`).

**Must not imply:** anything read-only belongs here. If it does not change
something, it is not on this screen.

The admin tabs stay visible to non-admins with the `admin` marker, per
`redesign.md`: a tab hidden from a non-admin is indistinguishable from a tab
that does not exist, and the person then asks in Slack whether the feature was
built.

---

### B6.12 — The dock: logs, checkpoints, artifacts

This is the ask that `redesign-v2.md` ranked as two unbuilt platform seams. Two
of the three are built (§A0.2) and this is the screen work.

**Logs — buildable today.** `GET /v1/tasks/{id}/logs?attempt_id=&stream=&source=&offset=&limit_bytes=`,
client at `api.ts:412`.

- Each stream reports `ok` / `absent` / `unreadable` **independently**, and
  `content` is `null` rather than `""` in the latter two. **Nothing may render
  `content` without reading `status` first** — the client's own docstring says
  so and it is H1 at the byte level. `""` means an object that exists and is
  empty; `null` means it could not be read or is not there. Those are three
  different screens.
- Paging is by **raw byte offset** (`next_offset`), not by line and not by the
  length of the text returned, because redaction makes the text shorter than the
  bytes it came from. Windows are aligned to whitespace by the server so a
  credential cannot be split across two.
- `source=auto` prefers the final record and falls back to the tail **only when
  the record is ABSENT**, never when it is unreadable. The viewer must print
  which object it is showing.
- The live tail object is prefixed `#swarm-tail offset=<n> size=<n>`
  deliberately, so a reader can distinguish a **gap** (it polled too slowly and
  the window moved) from a continuation. **The viewer must say "output is
  missing here"** rather than silently stitching two non-adjacent pieces
  together.
- **Never undo the server's redaction.** It is applied at read time,
  unconditionally, because the worker's own pass replaces registered literal
  values only and is skipped entirely for a run that registered none — so a
  token the agent minted, an `Authorization:` header a tool echoed, or an `.env`
  printed out of a cloned repository reaches the bucket in the clear.

**Checkpoints — buildable today.** `GET /v1/tasks/{id}/checkpoints?attempt_id=&limit=&page_token=`,
client at `api.ts:377`. It pages, which the events route does not.

- A failed *listing* is a 503, never `{"checkpoints": []}` with a 200.
- A checkpoint whose manifest is **missing** is `absent` with
  `resumable: false` — the manifest is the commit marker, so there is nothing to
  resume from.
- A checkpoint whose manifest **could not be read** is `unreadable` with
  `resumable: null`, because "cannot resume" and "cannot tell" are different
  facts. **A row with `resumable: null` must not be drawn as one that cannot be
  resumed.**
- Listing is across attempts, deliberately: a resume is by definition a new
  attempt, so the checkpoint that matters after a crash was written by the
  attempt that died.
- Contents are recorded nowhere. Only id, size and uri (`types.ts:902-903`).

**Artifacts — manifest today, bytes need a seam.** `GET /v1/tasks/{id}/artifacts`
returns `{artifacts: [{name, bytes, uri}], artifacts_skipped, artifact_bytes,
complete}`. The `uri` is `gs://` and no download URL is minted, deliberately, to
keep the tenant boundary where IAM already enforces it. `complete` is false until
the task is terminal, which is what stops an empty list reading as "produced
nothing"; `artifacts_skipped` names files dropped at `max_artifact_bytes`, so a
short list carries its own reason.

Byte-serving is §B9.S3. Until it lands, the manifest renders with the `gs://`
uri and a copy control — **not** a dead link.

---

## B7. The `?` affordance and the Help section

The owner directive (`redesign-v2.md` §9) is:

> we should try to remove all of the prose content from the app. Help should sit
> in a dedicated help section but we could put everywhere we need to a helper
> hover over question mark tooltip and house info there too

### B7.1 The distinction that makes this safe

The current UI's best property is implemented **as prose**, so a naive removal
deletes it.

- **The FACT stays on the surface, always, and stays impossible to miss.** That
  a read failed, that a response was partial, that a figure is unmeasured, that
  a total is being withheld — each keeps a visible marker: an em dash, a hatched
  segment, a count of what is missing, a struck total, a dotted outline.
- **The EXPLANATION moves into the `?` card.** "Why is this an em dash" is a
  hover. "Why is there no total" is a hover.

**The acceptance test for any panel after this change:** can a reader tell,
*without hovering anything*, that a number is missing rather than zero? If not,
the marker is too quiet and the honesty rule has been deleted rather than moved.

A silent icon where a sentence used to be is not this directive satisfied.

### B7.2 `<HelpCard>` — the component

```
<HelpCard topic="park-reason" />   renders a 14px ? glyph
```

- **Trigger:** a 14px `?` in `--text-faint`, inline, after the label it explains.
  Never after a value.
- **Opens on hover with a 120ms delay, on focus, and on click.** Click pins it;
  Escape and outside-click dismiss. Hover alone is not enough — it is
  unavailable on a touch device and invisible in a screenshot, which is the
  failure mode the honesty rules exist to prevent.
- **Content:** 240–380px wide, `--t-body`, `--measure: 52ch`, at most 60 words,
  ending in a link to the Help section anchor for the long form.
- **Source of truth:** one `help.ts` module exporting
  `Record<TopicId, {short: string; anchor: string}>`. The Help section renders
  the long form from the same module, so a topic cannot exist in one and not the
  other. **It restates no frozen value** (B1.3) — where a topic needs a number,
  it reads it from the route.
- **Accessibility:** `aria-describedby` on the element it explains; the card is
  `role="tooltip"` when hovered, `role="dialog"` when pinned.

### B7.3 The Help section

A seventh destination, reached from the head's `?` and from every card's
footer link — **not** a seventh rail item, because it is a thing you look up,
not a thing you work in. The same argument `redesign.md` made for the API
reference at `#reference`.

It carries the long-form material currently inlined:

| Anchor | What it explains |
|---|---|
| `#help/states` | Every task state, and which four reserve capacity |
| `#help/park-reasons` | Every `ParkReason`, what causes it, and what clears it |
| `#help/pools` | What a pool is; why a ceiling is the minimum across pools; why raising a non-binding pool changes nothing |
| `#help/paused` | Why a paused pool is not a full one |
| `#help/capacity` | All-or-nothing reservation, and why concurrency counts from LEASED |
| `#help/attempts-and-fencing` | Generations, stale workers, and what a fence means |
| `#help/checkpoints` | What a checkpoint is for, why it is mandatory and periodic, and what `resumable: null` means |
| `#help/duration` | Every segment of the duration bar, and why cold start is its own segment |
| `#help/absent-vs-zero` | Em dash versus `0`, and the hatch |
| `#help/coverage` | Why a spend total says "1 of 7", and which exits record no spend |
| `#help/windows` | The 200-row clamp, the event page with no token, and what "unreachable" means |
| `#help/provenance` | What the dock's read strip is, and why a 403 is information |

Each section's `question` field from `App.tsx` lives here too, so it is stated
once instead of on all four panes of a section (§A1.6 item 5).

---

## B8. The defect list, with a fix per item

| # | Defect | Where | Fix |
|---|---|---|---|
| 1 | `.bar` declared twice: 0px content box slices three "why is my agent stuck" banners and blanks the only pool meter | `styles.css:120` / `:903`; `Capacity.tsx:411`, `AgentDetail.tsx:576,592,616` | Namespace: `.meter` for the 5px track, `.notice` for the banner. Update the four call sites. |
| 2 | `@keyframes pulse` declared twice; the skeleton flashes at ~3× the intended amplitude | `styles.css:160` / `:900` | Delete the `:900` block; keep 0.5 → 0.85. |
| 3 | `.filters` declared twice with different gap and font-size | `styles.css:202` / `:514` | Merge into one rule; the second's values win where they differ. |
| 4 | `.panel` has no box, so a "panel" is a 28px gap between runs of text | `styles.css:739` is the only `.panel` rule | Add `background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px`. |
| 5 | No gate on duplicate top-level selectors | — | Add one, keyed on **non-adjacent** duplicates (or requiring an explicit marker), so `.nav button` `:185`/`:186` and `.row .st` `:220`/`:543` do not fail it. H9. |
| 6 | Six state colours collapse to two greys in dark and one in light; failed and cancelled are the same mark | `styles.css:20-24`, `:44-48`, `:840-842` | §B4.2. Retune to ≥4 L* steps ≥6 apart; second non-colour channel on every state; split `--ctl-absent` off `--text-faint`. |
| 7 | `--line` at 1.18–1.36:1 against a 3:1 floor, carrying every edge in the product | `styles.css:8`, `:37` | `#39424e` dark / `#b9c0c8` light. |
| 8 | Heading hierarchy inverted: `h2` fainter and smaller than the body it heads | `styles.css:91-95` | `--t-title` / 600 / `--text`. Removing `text-transform` also fixes the uppercased workflow id (H8). |
| 9 | Bars and tabs expand to fill the content column | `styles.css:837` `.chart .col`, `:872` `.split-row`, `:494` `.tabs button` | `max-width: 72px` and left-align for bars; `flex: 0 0 auto` for tabs. |
| 10 | The receipt footer is a session-wide, unbounded, alphabetically-sorted log rendered under every page, whose 158px clip destroys the identifiers it exists to publish | `fetch.ts:202`, `:261`; `styles.css:670-690` | Move to the dock (§B3), one line collapsed. Inside it, truncate the **middle** of the path, not the tail, so `/v1/tasks/…/attempts` and `/v1/tasks/…/events` stay distinguishable. |
| 11 | Nav labels disagree with page headings | `Capacity.tsx:47`, `Activity.tsx:37`, `Holders.tsx:34` | `Pools`, `History`, `Holders`. |
| 12 | Agents lands on the tab that cannot contain a stalled step | `Agents.tsx:39` | Land on the first non-empty tab. |
| 13 | The row id is the last 8 characters, so it is a prefix of nothing | `Agents.tsx:295` | Use the prefix after the underscore; click-to-copy the full id. |
| 14 | DAG nodes are `<div>`s with the task id in a `title` | `Workflows.tsx:279` | Make the node an `<a>`; add state detail and elapsed to it. |
| 15 | The DAG is centred in a full-width container, leaving ~430px empty at 1024px | `styles.css:239-259` | Left-align: drop `align-items: center` and `justify-content: center`. |
| 16 | A dead `workflow.state` is printed beside a live rollup in the same typeface | `Workflows.tsx:154` | Drop it, or strike it the way `.rollup.untrusted` already strikes an untrustworthy count. |
| 17 | Events render at minute granularity, so a 3m09s gap between two events is underivable | `AgentDetail.tsx` / `Shell.tsx timeAgo` | Absolute times with seconds on the Attempts tab; `timeAgo` stays for list rows. |
| 18 | Raw JSON event payloads spend ~800px on three events | `AgentDetail.tsx` Attempts tab | Render the four useful fields; the raw block behind a disclosure. |
| 19 | `Execution` spends three lines on a Cloud Run resource path | `AgentDetail.tsx` | Last segment, with click-to-copy. |
| 20 | Four `DEV` badges per screen, from a hardcoded string | every screen except Overview | Remove until §B9.S8 lands. `Overview.tsx:179-183` already made this call. |
| 21 | The machine audit has no vertical-clip predicate, which is why the worst rendering bug is in none of the ten reports | `docs/web-ui/evidence/audit-*.json` | Add `scrollHeight > clientHeight + 1` on any element with non-visible overflow or a declared height. |
| 22 | The per-task audit sampled the one step of three that cannot exhibit the banner bug | `audit-agents-task-task_b5dc…json` | Capture a per-task audit for each of a DISPATCHED, a PARKED and a terminal task. |

---

## B9. Server-side seams still needed

Re-ranked against `8abc9c7`. Cost is engineering effort, not importance. Each is
a **request** to Track A, not a change (§B1.6). Build the UI against what exists
and render the absence honestly until each lands.

### S1 — page the events route · small · **unblocks every timeline**

`routes/tasks.py:136-144` is `order_by("at", ASCENDING).limit()` with no page
token and no `order` parameter, and `api.ts:123` requests it with no limit, so
it receives `default_page_size` (50). A run over ~31 minutes loses its tail
(§A4.6).

```
GET /v1/tasks/{task_id}/events
    ?limit=<int>
    &page_token=<opaque>       <- new
    &order=asc|desc            <- new, default asc
 -> {"task_id": str, "events": [...], "next_page_token": str | null}
```

The tasks list route already has `page_token`/`next_page_token`
(`tasks.py:79,98,102`) and `/v1/tasks/{id}/checkpoints` already takes a
`page_token` — the pattern exists twice in the same file.

`order=desc` matters independently: a caller who wants the *end* of a long run
gets it in one page.

**Until it lands:** every timeline draws its last segment as case (a) open
(§B6.5d) whenever `events.length === limit`.

### S2 — artifact and checkpoint **bytes** · medium

The listings exist. The contents do not.

```
GET /v1/tasks/{task_id}/artifacts/{name}/content
GET /v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content
 -> the object, with a content-type allowlist and an inline-vs-download decision
```

The machinery is the same as `/logs`, which is built: one GCS client, the
`_owns` tenant check pattern from `checkpoint.CheckpointManager._owns`
(`checkpoint.py:244-257`), a size cap.

Also needs a decision on whether a checkpoint gets a *listing* (a manifest read
out of GCS) or only whole-object download — `types.ts:902-903` records that
contents are recorded nowhere, so a listing means reading the tarball.

**Until it lands:** the manifest renders with the `gs://` uri and a copy
control, not a dead link.

### S3 — cross-task attempt aggregation · medium · unblocks "what did it cost"

No route sums cost across tasks. `Store.list_attempts` already accepts
`task_id=None` and the `attempts-tenant-created` index already exists
(`store.py:770-794`); the only route that calls it passes a task id.

```
GET /v1/attempts?since=<iso8601>&until=<iso8601>&limit=&page_token=
 -> {"attempts": [...], "next_page_token": str | null,
     "coverage": {"attempts": int, "with_spend": int}}
```

`coverage` is not decoration. It is what lets the caller render "1 of 7" without
a second pass, and without it the N+1 loop gets written in the browser by
whoever builds History.

Should also decide whether `models`, `num_turns` and `thinking_tokens` become
queryable. `control.py:501-504` deliberately leaves them in the untyped
`result_summary["runner"]["usage"]` dict — defensible per-agent, indefensible
per-fleet.

### S4 — `current_generation` and `current_lease_id` on the task · ~2 lines · IN FLIGHT

§A4.3. Both are already decoded at `codec.py:87-88`; `task_to_api` does not
serve them.

```
GET /v1/tasks/{task_id}
 -> { ..., "current_generation": int, "current_lease_id": str | null }
```

Unblocks: showing the generation-2-versus-lease-generation-1 mismatch that
explained a 20-minute stall. `types.ts:372-375` already documents the arithmetic.

Reported IN FLIGHT. If it has landed by the time this is executed, B6.4 panel 1
gains a fencing row; if not, that row is absent, not zero.

### S5 — the API reports its own environment · small

Nothing in the UI reads an environment; four `DEV` badges per screen are a
hardcoded string. `Overview.tsx:179-183` omits its own badge for that reason.

```
GET /v1/tenants/me  ->  { ..., "environment": "dev" | "staging" | "prod" }
```

**Until it lands:** no badge (§B8.20).

### S6 — a real resource time series · large

Only high-water marks are retained: `ResourceUsage.samples` (`metrics.py:46`) is
a *count* and the samples are discarded; `merge_rss`/`merge_disk` are `max()`.
Cloud Monitoring gets three GAUGE points written once at teardown, and swarm-api
holds no `roles/monitoring.viewer`, so that path is closed regardless.

The `HEARTBEAT` event series (`{elapsed_seconds, peak_rss_bytes, checkpoints}`,
one per 150s) is the **only** time series the platform has, and it is a
monotonic peak, not usage. §B6.5's step line is honest about that by labelling
it "peak reached by T+n"; a genuine utilisation curve needs sample retention.

**CPU is a separate and larger ask:** there is no CPU sampler at all
(`grep -n cpu apps/agent-worker/agent_worker/metrics.py` returns nothing), so
requested-vs-used has no CPU row and cannot have one without worker changes.

### S7 — account ↔ attempt attribution · needs a frozen-contract change · **out of scope**

Nothing assigns an account to an attempt. `Lease` and `Attempt` carry no account
field, so "which agent burned this account's quota" is unanswerable and no UI
work reaches it. Adding a field means editing `apps/common/swarm_common/models.py`,
which is frozen.

**Raise it in `docs/contract-change-requests.md`; do not build against it.** The
quota gauge must never join an account to an agent.

---

## B10. The order of work, and why

Each step is a landable change. Nothing below depends on a step after it.

| # | Step | Why here |
|---|---|---|
| **0** | **Wire the UI into the gate, and generalise H10.** Add `vitest` as a devDependency, a `make ui-test` target, and wire it into `make test`. Generalise `test_the_runtimes_route_is_read_by_a_screen_the_router_reaches` over every exported loader in `api.ts`. | It starts **red** — `loadTaskLogs` and `loadCheckpoints` have no screen (§A0.2). A gate that starts green proves nothing. Everything after this is measured by it. |
| **1** | **The defect batch** (§B6.0, §B8 items 1–4, 8, 9, 11). CSS and four `.tsx` files. No IA change. | Three of the four `.bar` sites are the only answer to "why has my agent not moved" and the fourth blanks the only pool meter. It is worth fixing whether or not the rest lands, and everything after it is measured on a truthful baseline. |
| **2** | **The duplicate-selector gate** (§B8.5, H9). | Four collisions in one sheet is a process gap, not four mistakes. Land it immediately after fixing them, before the sheet is rewritten, so the rewrite cannot reintroduce one. |
| **3** | **Colour, type and contrast tokens** (§B4.1, §B4.2, §B8.6, §B8.7). Both themes. | One block of tokens; changes forty rules. Largest legibility gain per line in the build, and it must precede any chart work or the charts inherit the collapse. |
| **4** | **Help and `<HelpCard>`** (§B6.2, §B7). | Every step after this moves a sentence into it. Build it late and the sentences get deleted instead of moved. |
| **5** | **The shell: rail, full-bleed work area, dock** (§B6.1, §B3 minus the inspector). Existing screens render unchanged inside it. | The 1100px column is the structural problem and removing it is cheap. Converting 23 screens is not, and does not have to happen at once. Reversible at every step. |
| **6** | **Agents list** (§B6.3) + **Workflows node links and left-align** (§B6.6 panels 1–5, §B8 items 12–16). | Both are small, both are the user's entry point, and item 14 (make the node an anchor) is the cheapest change in the document: the id is already in scope. |
| **7** | **The meter primitive**: collapse the seven track implementations into one (§B4.3 move 2). | Eight of the sixteen panels are drawn on it and it carries the hatch semantics. Doing it before the chart layer means the chart layer has eight fewer panels to draw. |
| **8** | **`AttemptDuration`** (§B6.5) + `d3-scale` (§B5). | The answer to §A4.1, and the first component that needs a scale. Landing it alone proves the honesty rules against a real axis before anything else depends on them. |
| **9** | **The inspector becomes a pane** (§B3 INSPECTOR, §B6.4). | The largest single change. Sequenced after the content panels so it is a layout move, not a rewrite. |
| **10** | **The dock gains logs and checkpoints** (§B6.12). | Turns step 0's red test green. Two routes that already answer; no platform work. |
| **11** | **`@dagrejs/dagre`, and the workflow view modes** (§B6.6 panel 6). | The only step that needs the second dependency, and the only one that is pure improvement rather than a fix. Last, so that dropping it costs nothing already built. |
| **12** | **Overview's seventh check** (§B6.7). | Small, independent, and reads data Overview already loads. Late because it is the least broken of the three screens in §A1. |

**`make lint` and `make test` run at the end of every numbered step, and a step
is not finished until both are clean.** Not once at the end.

---

## B11. How to prove each step

The owner standard is *prove each path before calling it done*. Concretely, for
every step: a test that **fails before the change and passes after**, plus a
named mutation the test must catch. A test that passes against its mutation has
proved nothing.

### B11.1 The two test mechanisms, and which to use for what

**Mechanism A — Python tests that read the shipped TypeScript and CSS as text.**
Already the house pattern: `tests/unit/control_plane/test_runtimes_screen.py`
does exactly this, and its docstring says why — "Everything here reads the
shipped client source deliberately … A mock would agree with whatever the screen
happens to do." It runs under `make test`, offline, with no new dependency.

Use it for: seam properties (H10), the duplicate-selector gate (H9), the
no-text-transform-on-ids half of H8, the token-scale census, the no-restatement
rule (B1.3), and every property that is about *source structure*.

**Mechanism B — `vitest`, as a devDependency.**

Use it for: H2, H5, H6, H7 and the render half of H8, which are about what comes
out of a component and cannot be seen by scanning source.

**This adds one devDependency to a repository that currently has two runtime and
five dev.** It ships zero runtime bytes. The alternative — extracting every
honesty-bearing computation into pure modules and testing them from Node without
a DOM — covers H6 and H7 but not H2, H5 or H8, because those are about what is
*rendered*. The choice is stated here so it is executable and repeated in §B13
so it is reversible.

Keep all honesty-bearing computation in pure `.ts` modules with no React anyway
(`duration.ts`, `scale.ts`, `coverage.ts`), so most assertions need no DOM even
with vitest available.

### B11.2 Per step

| Step | Failing-then-passing test | The mutation it must catch |
|---|---|---|
| **0** | `test_every_exported_loader_is_used_by_a_screen_the_router_reaches` over `api.ts`. **Red at `8abc9c7`** on `loadTaskLogs` and `loadCheckpoints`. | Add an exported loader nothing calls. |
| **1** | `test_no_selector_is_declared_twice_non_adjacently` (red on `.bar`, `pulse`, `.filters`). Plus a render test: `AgentDetail` with `park_reason` set produces a notice whose `scrollHeight <= clientHeight`. | Re-add `height: 5px` to the banner rule. |
| **1** | `test_panel_has_a_box`: `.panel` matches a rule declaring `border` and `background`. | Delete the new `.panel` rule. |
| **1** | `test_no_id_bearing_selector_declares_text_transform` (H8, red on `.section > h2`). Plus a render test: the rendered workflow id string `===` the API's. | Re-add `text-transform: uppercase` to `.section > h2`. |
| **2** | The gate itself, run against a fixture sheet containing one intentional adjacent split and one accidental non-adjacent duplicate. It must flag exactly one. | Make the gate flag adjacent duplicates: it then fails on `.nav button` and gets switched off. |
| **3** | `test_state_tokens_occupy_four_luminance_steps`: computes L* from the tokens in `styles.css` for **both** themes and asserts ≥4 distinct steps ≥6 L* apart, and no pair under 2.3 L*. **Red at `8abc9c7` in both themes.** | Revert one token to its current value. |
| **3** | `test_line_token_meets_the_non_text_floor`: `--line` ≥3:1 against both surfaces, both themes. Red today at 1.18–1.36:1. | Revert `--line`. |
| **3** | `test_the_type_scale_has_six_steps_and_no_half_pixels`: census over `styles.css` (both `font-size:` and shorthand `font:`), asserting ≤6 distinct sizes and zero half-pixel values. Red today at 15 and 71. | Add a 12.5px declaration. |
| **4** | `test_every_help_topic_has_a_short_form_and_an_anchor`, and `test_no_help_string_restates_a_frozen_value` (B1.3). | Inline a resource-class name into a help string. |
| **5** | `test_the_app_declares_no_max_width`; plus a render test asserting the rail, work area and dock are present at 1600px and the rail collapses at 1200px. | Re-add `max-width: 1100px`. |
| **6** | `test_the_landing_tab_is_the_first_non_empty_one`: given rows where Live is empty and Waiting has 3, the mounted component's selected tab is Waiting. | Hardcode `'live'`. |
| **6** | `test_the_displayed_id_is_a_prefix_of_the_real_id`: `realId.includes(displayed)` **and** `realId.indexOf(displayed) < 8`. | Revert to `slice(-8)` — the first assertion passes, the second fails. That is why both are needed. |
| **6** | `test_every_dag_node_is_a_link_to_its_task`: rendered node has an `href` containing the step's task id. | Revert `<a>` to `<div>`. |
| **7** | `test_one_track_primitive`: exactly one selector in `styles.css` declares a track height. Red today at seven. | Re-add `.sr-bar`. |
| **8** | **H6** — `buildSeries` with a null point yields a path with >1 `M` command. | `points.filter(p => p !== null)` before building. |
| **8** | **H7** — `AttemptDuration` with no terminal event: `domain[1] === lastMeasuredBoundary`, the open segment carries `data-open="true"`, the label ends in "so far". | Pad the domain to `timeout_seconds`. |
| **8** | **Case (b)** — a terminal event with a missing intermediate yields **one** hatched span covering the interval, labelled with its two bounding events, and **not** N segments. | Distribute the interval across the segments that should have been there. |
| **8** | **Case (c)** — `submitted.detail.state === "READY"` yields **zero** segment-0 elements, not a zero-width one and not a hatched one. | Treat a missing `ready` event as case (b). |
| **8** | **Case (d)** — `events.length === limit` yields an open last segment and the window marker. | `events.length > limit`. |
| **9** | `test_selecting_an_agent_keeps_the_list_mounted`: at 1600px, select a row; the list element is still in the document. | Revert the inspector to `position: fixed` with `role="dialog"`. |
| **10** | Step 0's test turns **green**. Plus: `content === null` renders the unreadable state and `content === ''` renders the empty state, and the two differ. | `content || ''` anywhere in the viewer. |
| **10** | `test_a_tail_gap_is_reported`: two windows whose `#swarm-tail offset=` values are non-adjacent render a gap marker between them. | Concatenate the two windows. |
| **10** | `test_resumable_null_is_not_drawn_as_false`: a checkpoint row with `manifest: 'unreadable'`, `resumable: null` renders differently from one with `resumable: false`. | `resumable ?? false`. |
| **11** | `test_the_dag_orders_siblings_without_crossing_edges` on a four-node fork/join fixture. | Revert to array-index ordering. |
| **12** | `test_a_parked_step_raises_a_check`: `deriveChecks` with a PARKED task returns a check in `found` state naming the park reason. Plus: the tile's self-report reads "7 of 7". | Remove `parkedCheck` — the count silently becomes 6 of 6 and the test must catch the *count*, not just the check. |

### B11.3 The audit harness

Regenerate `docs/web-ui/evidence/audit-*.json` after step 1 and after step 5,
with the vertical-clip predicate added (§B8.21) and with a per-task audit for a
DISPATCHED, a PARKED and a terminal task (§B8.22). The PARKED capture is the one
that proves §A3.1 fixed, and no existing audit could have.

Also regenerate the screenshots at 1600×1000, 1440×900, 1280×800 and 1024×768,
in **both** themes. Every capture in the current evidence set is light mode, and
light mode is where the colour collapse is total.

---

## B12. What this prompt does not cover

Stated plainly, because an unverified claim here ends up as a runbook step
someone follows at 3am.

1. **Anything against a deployed environment.** Every claim in Half One is from
   the repository at `8abc9c7` or from a PNG in `docs/web-ui/evidence/`. No
   `make smoke`, no running cluster, no live API. §A4.2, §A4.4 and §A4.5 are
   reported observations I could not reproduce and did not verify.
2. **The three in-flight lanes.** I have not seen their branches. §A4.2, §A4.3
   and §A4.4 are marked IN FLIGHT on the strength of the brief alone. If §B9.S4
   has landed, B6.4 gains a fencing row; if it has not, the row is absent.
3. **The bundle sizes.** Lane 3's, measured on this toolchain. I did not build.
   The licences and publish dates are from the npm packuments, also Lane 3's.
4. **The character-per-line figures** (157–192). I verified that no rule in
   `styles.css` constrains a prose measure and that `.app` is 1100px. The counts
   are Lane 2's, read off the screenshots.
5. **The visx `<BarStack>` markup** in §B5. Lane 3 rendered it; I did not.
6. **Any external prior art.** Nothing in this document rests on a claim about
   Lens, Temporal, Weave, LangSmith, Langfuse, Braintrust or Phoenix.
7. **Accessibility beyond contrast and greyscale.** No screen-reader pass, no
   keyboard-trap audit, no focus-order review. The `role="dialog"` → pane change
   in step 9 needs one and this prompt does not specify it.
8. **Performance.** No render-budget target, no measurement of what a 200-row
   list at 36px costs. The 200-row clamp is a server fact, not a UI budget.
9. **Mobile below 900px** beyond "the rail becomes the current top bar". The
   existing `@media (max-width: 640px)` blocks encode real decisions
   (`styles.css:559-573` promotes the "why" line and drops identifiers) and this
   prompt does not re-specify them.
10. **Anything that needs a frozen-contract change.** §B9.S7 is named and
    excluded.
11. **The `docs/web-ui/*.md` specification set.** Its eight files specify 80
    screens, 26 buildable. This prompt covers the six sections and the inspector;
    it does not reconcile itself against all 80.

---

## B13. What remains the owner's decision

Each of these has a default in the prompt so the work is executable. Each
default is cheap to reverse, and none is a decision I should be making.

**D1 — The receipt strip: move it, or only fix its truncation?**
The prompt moves it to the dock (§B8.10). The alternative is one line of CSS —
truncate the middle of `.s-path` instead of the tail — which removes the
indistinguishable-pair bug today and keeps the footer. Both are defensible;
`DataSources.tsx:6-17` argues correctly for the instrument existing, and the
argument does not say it must be under every page.

**D2 — Row density: 36px, or 32px, or leave the rows alone?**
The prompt says 36px (§B4.3), which keeps a glyph plus a short label inline.
32px puts every identifier in a hover card, which is unavailable on touch and
invisible in a screenshot. Leaving rows alone and fixing only measure, type and
contrast is the lowest-risk option and gets about 15 rows instead of 24.

**D3 — Does `vitest` go in?**
The prompt adds it (§B11.1), because five of the ten honesty properties are
about what is rendered and a source-scanning test cannot see a render. It is one
devDependency and zero runtime bytes. Declining it means H2, H5, H6, H7 and half
of H8 are enforced by review rather than by a gate — which is how the four CSS
collisions got in.

**D4 — Does the inspector become a real pane now (step 9), or does the overlay
stay until later?**
The prompt schedules it at step 9. It is the fix for "reading one agent takes
the others off screen" and it is the largest single change in the build.
Deferring it costs nothing already built.

**D5 — Is the environment badge removed now, or kept until §B9.S5 lands?**
The prompt removes it (§B8.20), following `Overview.tsx:179-183`. Keeping a word
nobody measured on nine screens is the alternative.

**D6 — Does `docs/web-ui/redesign-v2.md` get a "Superseded" block?**
Four of its load-bearing claims are now false (§A0, §A5). The house pattern for
this is `redesign.md`'s "Superseded, 2026-09-21" block, which records what moved
rather than silently editing. I did not add one — that is an edit to a document
the owner may want left as the record of what was believed at `f0154b4`.

> **Answered by the owner, 2026-09-24.** The answer is a dated status block, not a
> "Superseded" rewrite. `redesign-v2.md`'s old header line, "Status: proposal.
> Nothing here is implemented", is replaced by a block titled "Status,
> 2026-09-24". It lists what shipped (checked against the code at `b0fff1b`,
> with file references) and what is still open. It records Q1 and Q3–Q6 as
> answered, and Q2 as half answered: F0 and §1.1(a) shipped, while the
> duplicate `@keyframes pulse` and `.filters` are still in the sheet and are on
> its open list. The body stays as the design record of what was believed at
> `f0154b4`.

**D7 — Which seam is built first: S1 (events paging) or S3 (cross-task
attempts)?**
The prompt ranks S1 first because every timeline is built on a page that
silently drops its tail, and a long agent currently renders as one that never
finished. S3 is the one that stops an N+1 loop being written in the browser by
whoever builds History. They are independent and the order is a priority call,
not an engineering one.

---

# HALF THREE — what the screenshots show (2026-09-22, signed in, live data)

Half one was written against screenshots taken while a workflow was mid-flight.
This half was captured after it finished, against `https://swarm.saga.xyz`, all
15 routes, with the probe in `scratchpad/uiaudit/probe.js` running in-page on
each. It supersedes nothing; it adds what only became visible once there was
completed work to render.

**Method.** Each route: navigate, `wait --networkidle`, run the probe
(error-boundary detection, clipped-x/y, past-viewport, ellipsis truncation,
pointer-without-target), screenshot. 15/15 captured.

**No route crashed.** The `ErrorBoundary` ("The interface crashed",
ErrorBoundary.tsx:38) did not fire on any of the 15. Every defect below is a
layout or truth defect in a page that rendered successfully.

## V1 — The workflow graph is narrow, and it is not a graph (severity: high)

**CORRECTED 2026-09-22, after running a six-step workflow.** The first version
of this finding said the DAG was "jammed against the right edge". That was
wrong: I measured the blank space to the LEFT of the nodes and did not measure
the right. It is centred, with roughly equal margins either side. The claim of
misalignment is withdrawn.

What is actually wrong is worse, and only became visible with a workflow wide
enough to show it. Rendering `wf_5e5ad3b6f7da4299a839` — five independent steps
and one step joining all five — the screen draws:

```
[cold-start] [fencing] [allornothing] [checkpoints] [absentzero]
                    ———— THEN ————
                     [synthesis]
```

One `THEN` bar. The same `THEN` bar a purely linear three-step workflow draws
between `research` and `draft`. **So the view cannot express the difference
between "these five ran in parallel and the sixth joined them" and "these ran
one after another."** There are no edges: siblings are laid out in a row and
dependency is implied by vertical order plus one separator. That is a list with
a decoration, not a directed graph, and the one question this screen exists to
answer — what depends on what — is the question it cannot answer.

The truncated dependency line under `synthesis` makes it concrete: it reads
`← cold-start, fencing, allorn…` and ellipses away two of the five parents. The
only complete statement of the graph on the page is cut off mid-word.

Secondary, and the part the original finding got right: the nodes occupy about
700px of a 1070px content area and the page is centred in ~1100px of a 1600px
viewport, so the most important diagram in the product uses well under half the
glass. It should be the widest, most deliberate thing in the app.

## V2 — The header contradicts the steps, on one screen, at one glance (high)

Also `agents/workflows`, and this is the sharpest instance of the dead-field
problem half one predicted:

```
WF_BCDC9180E4FB4A209F31 · QUEUED · UPDATED 2H AGO
3 steps · not counted                      <- struck through, amber
  research  ✓ succeeded
  draft     ✗ failed
  review    ø cancelled
```

The header says **QUEUED**. The steps say succeeded, failed and cancelled. Both
are on screen simultaneously, in the same card, and nothing says which one the
reader should believe. `QUEUED` is the field nothing ever updated; the step
states are live. A reader's eye goes to the header first because it is the
heading.

Fix: the header must render the DERIVED rollup, never the stored `state` field.
`apps/swarm-api/swarm_api/rollup.py` already computes it.

## V3 — "3 steps · not counted" renders with a STRIKETHROUGH (high)

Same card. Amber, struck through. Strikethrough universally means *retracted* or
*superseded*. Here it is the only place the step COUNT appears, so the one
number telling you how big the workflow is looks like it has been invalidated.
Whatever the intent, no reader recovers it.

If the count is known (it is — three nodes are drawn from it), print it plainly.
"not counted" belongs to the rollup, not to the step count.

## V4 — The workflow id is uppercased by CSS and cannot be pasted (medium)

`WF_BCDC9180E4FB4A209F31` is displayed; the real id is lowercase. This is the
exact mistake `QuotaDetail.tsx:94-96` refuses to make two screens away, with the
reason written beside it: "a displayed id that differs from the real one is
unusable". The workflow screen does not honour its own codebase's rule.

## V5 — No step carries a duration, a cost, or a token figure (high)

The step nodes show: name, state, runner profile, dependency. That is all. The
brief for this redesign asks for "resource consumption and costs, duration,
inputs and outputs" — and the one screen about a multi-agent run reports none of
the four, even though `attempt_from_dict` now decodes all five spend fields and
the attempt rows carry them.

## V6 — The API telemetry strip occupies the bottom of EVERY page (medium)

Sixteen green cards, roughly 200px tall, on all 15 routes, below the content:

```
/v1/accounts  200 · 382ms   /v1/admin/leases?acti… 200 · 275ms
/v1/capacity  200 · 274ms   /v1/providers          200 · 187ms   ...
```

It is genuinely useful and should not be deleted, but it currently outranks the
page's own content for vertical space on the shorter screens and it is identical
everywhere. It belongs behind a disclosure, or in the Help/diagnostics section,
not stapled to every view.

## V7 — Vast dead space in the right column (medium)

`overview/now`: the left column runs to roughly y=900; the right column stops at
y=600 after "Nothing is running", leaving a blank half-page. `agents/workflows`
ends at y≈820 on a 1150px-tall viewport. The content is centred in a ~1100px
column on a 2000px screen, so on a wide display the app uses under half the
glass. For a console whose brief is "graphs and diagrams everywhere possible",
empty space is the most expensive thing on screen.

## V8 — The capacity bars are invisible at zero (medium)

`overview/now`, "Capacity, by what binds it": five rows, each with a light-grey
track and a fill of zero width, reading `0 / 10`. A zero-width fill on a
near-white track is indistinguishable from "this widget failed to render". The
same page is careful in prose about measured-zero vs absent; its bars are not.

## V9 — Confirmed still live: Overview never says "workflow" (high, from half one)

Re-verified on 2026-09-22 with a completed 3-step workflow in the tenant. The
word "workflow" appears nowhere on `overview/now`. The panel reads:

> Nothing is running — No task on the 7 most recently created is in LEASED,
> DISPATCHED, STARTING or RUNNING. The state counts agree: zero.

Every sentence true; the conclusion a reader draws is false. `deriveChecks` now
runs 6 of 6 and surfaces "2 failed tasks among the 7 most recent", which is an
improvement — but there is still no workflow check and no parked check.

## V10 — The tenants table clips the IDENTITY column (low)

`admin/tenants`: `swarm-agent-worker-eng@sag…`, `swarm-agent-worker-u-bogda…`
truncated at the table's right boundary. This is the only route where the probe
reported `past-viewport` (×2), so it is the one genuine horizontal overflow in
the app. Service-account emails are the thing an operator copies.

## Probe findings across all 15 routes

`clipped-x` ×2 and `ellipsis-truncated` ×2 on every route — these are the shell
chrome, not per-page defects, and should be fixed once in the nav.
`past-viewport` ×2 on `admin/tenants` only (V10).

## Build-prompt additions

Append to the B-series, in dependency order:

* **B14 — Draw the actual graph, at full width.** Real edges from each parent
  to each child, so a fan-in of five is visibly different from a chain of five.
  A single shared `THEN` bar cannot encode that and must go. Full content width;
  never ellipse the dependency list — it is the only complete statement of the
  graph on the page. Blocks V1.
* **B15 — One state, derived, in the header.** Delete every render of the stored
  `state` field on a workflow. The header shows the rollup from `rollup.py` and
  the step chips agree with it by construction. Blocks V2.
* **B16 — Step nodes carry the four numbers.** Duration, cost, tokens in/out,
  and a link to inputs/outputs, on every node, with the absent-value discipline
  the agent drawer already gets right ("not reported" ≠ "$0.00"). Blocks V5.
* **B17 — Never restyle an identifier.** Remove `text-transform` from every id.
  One rule, applied globally, matching QuotaDetail's stated reason. Blocks V4.
* **B18 — Telemetry behind a disclosure.** Collapse the request strip to a
  single line with a count and p95; expand on click; full detail in Help. Blocks
  V6.
* **B19 — Layout fills the viewport.** Wide-screen breakpoints; no page may end
  with a half-empty right column while the left column continues. Blocks V7.
* **B20 — A zero bar must look measured.** A visible baseline tick or a
  hairline at zero, distinct from an unrendered track. Blocks V8.
* **B21 — Overview gains a workflow check and a parked check.** `deriveChecks`
  must be able to say "a workflow has not advanced in N minutes" and "N steps
  are parked". Blocks V9 and is the highest-value single change in this
  document.

## What was not verified

* No route was tested below 1554px content width, so these are desktop
  findings only; the mobile/narrow story is unaudited.
* Task-detail (`agents/task/<id>`) and its attempts pane were NOT captured —
  the link-harvest returned nothing on the workflows route and I did not reach
  them. They remain the deepest unaudited surface and are the most likely home
  of a genuine `ErrorBoundary` crash.
* The `ui-test.sh` lane reported `fail — Treat this as a harness failure, never
  as a pass`, so its assertions did not run; that failure is unexplained here.
* `e2e-test.sh` reported `swarm-api .../readyz answered HTTP 404`, which is a
  platform finding, not a UI one, and is unresolved.

---

# HALF FOUR — a six-agent workflow, watched (2026-09-22)

Submitted `wf_5e5ad3b6f7da4299a839`: five independent `claude-code` steps and a
sixth joining all five via `input_from`. Walked every route at 1600x1000 while
it ran. **No route crashed** — `ErrorBoundary` did not fire on any of the 19
surfaces visited, including task detail and the attempts pane, which half three
listed as unaudited.

## W1 — The New Workflow form cannot submit a workflow that runs (BLOCKER)

`SubmitWorkflow.tsx:186-189` builds each step as:

```js
steps: steps.map((s) => ({
  step_id: s.stepId.trim(),
  runner_profile: s.profile,
  depends_on: s.dependsOn.filter((d) => ids.includes(d)),
})),
```

There is **no `input`** and **no `input_from`**. `WorkflowStepCreate.input` is
`Field(default_factory=dict)` (schemas.py:74), so every step arrives with
`input == {}`. And `cliagent.py:263-265`:

```python
prompt = payload.get("prompt")
if not isinstance(prompt, str) or not prompt.strip():
    raise RunnerFailure(f"{spec.name} requires a non-empty string input.prompt")
```

So **every workflow submitted from this screen fails at every agent step, by
construction.** Not intermittently — the failure is total and deterministic for
any profile backed by the CLI agent runner. The screen renders, validates step
ids, offers dependency checkboxes, explains three merge strategies at length,
and produces a workflow that cannot execute.

The single-agent form does not have this bug: `Submit.tsx:220` renders a
`<textarea>` for `input`. So the platform's own UI is inconsistent with itself,
and the working half is the one that was built first.

`input_from` being absent is the second half: it is the mechanism by which one
step's artifact reaches the next, it is the reason `SWARM_ARTIFACTS_DIR` was
fixed, and there is no way to express it from the UI at all. A workflow built
here cannot pass work between steps even if the prompts were supplied.

**This outranks every layout finding in this document.** A console whose
"create" screen cannot create a working thing is not a formatting problem.

## W2 — The graph cannot distinguish a fan-in from a chain (high)

See the corrected V1. With five parallel steps joining into one, the page draws
the five in a row, then a single `THEN` bar, then the join — the same `THEN` bar
a strictly linear workflow draws between two sequential steps. There are no
edges. The dependency line that would disambiguate is ellipsed:
`← cold-start, fencing, allorn…`, hiding two of the five parents.

## W3 — The PARKED reason banner is vertically clipped (high)

Task detail for the join step, `DEPENDENCY_INCOMPLETE`. The banner renders with
its text sliced horizontally through the middle — the top half of the letters is
visible and the bottom half is cut off by the container. The probe reports it as
`clipped-y`. This is the one banner that explains why the task is not running,
on the one screen a user opens to ask exactly that.

## W4 — The task drawer overlays the list with no scrim (medium)

Opening a task slides a drawer over the right side of the Agents table. There is
no dimming layer and no shadow, so the drawer's left edge cuts the underlying
rows and slices the telemetry cards mid-card. It reads as a broken layout rather
than a panel above a page.

## W5 — `admin/tenants` overflow is worse on a wider screen (medium)

`past-viewport` was ×2 at 1554px content width and ×4 at 1600px. The IDENTITY
column holds service-account emails — the thing an operator copies — and they
are clipped at both widths.

## W6 — The workflow read did not report `state_source` (medium)

`routes/workflows.py`'s module docstring says "EVERY READ DERIVES" and that the
response reports whether the derived state agrees with the stored copy. A live
`GET /v1/workflows/wf_5e5ad3b6f7da4299a839` returned `state: "QUEUED"` while
five of its six steps were `dispatched`, and carried no `state_source` field in
the payload this probe could see. Either the field is nested where the probe did
not look, or the derive path is not running on this route. Worth confirming
before B15 is built on top of it.

## What still holds, and should survive the redesign

The absent-value discipline on task detail is the best writing in the product
and got better, not worse, under load:

* `PEAK MEMORY — not recorded. Written when an attempt ends. A running attempt
  has none.`
* `TOKEN COST — not reported. No attempt reported a cost. This is an absent
  measurement, not $0.00.`
* `CHECKPOINTS — 0` rendered as a DIGIT, with "No attempt document lists one."
* `ELAPSED — queued 36s. Parked — wall time, not work. Nothing is executing and
  no capacity is held.`
* `Nothing has been admitted yet ... a task that is PARKED genuinely has none.
  This is a real zero, not a failed read.`

## Build-prompt additions

* **B22 — The New Workflow form collects per-step input.** A prompt field per
  step, and an `input_from` control naming an upstream step and the artifact
  filename to stage. Until this exists the screen should refuse to submit rather
  than create work that cannot run. **Blocks W1 and outranks B14-B21.**
* **B23 — The parked reason must be legible.** Fix the clipped banner and give
  every blocked/parked reason a container that grows with its text. Blocks W3.
* **B24 — The drawer is a layer.** Scrim, shadow, and a hard left edge; the page
  beneath must read as covered, not as cut. Blocks W4.
* **B25 — Tables own their overflow.** No identifier column may clip at any
  width; service-account emails and tenant ids wrap or scroll within the table.
  Blocks W5.

## What was not verified

* The workflow was still running when these were written; no step had reached
  `succeeded`, so the artifact-staging path (`input_from` at runtime) is
  unproven in this pass.
* W1 is proven by reading the source and the two contracts it violates, not by
  submitting a workflow from the form and watching it fail. That experiment was
  not run, because it would create work that is guaranteed to fail and spend
  agent time to prove something the code already states.
* W6 may be a probe artefact rather than a defect; it is written as a question.

---

# HALF FIVE — the original brief, item by item, against what exists

Asked for on 2026-09-22: re-read the original request and find what is still
missing. Every line below is a measurement, not an impression.

The brief was: *"more centered around Agents and Workflows of agents and
sub-agents, with great tooling to inspect the work an agent has done,
checkpoints, logs and stats and a way to visualize the resulting work ...
details about resource consumption and costs, duration, inputs and outputs and
a way to inspect the outputs ... graphs and diagrams everywhere possible"*,
styled after Lens for Kubernetes but more futuristic, with Nomad, Run:ai,
OpenShift Console and Rancher as references — and, separately, *"remove all of
the prose content from the app. Help should sit in a dedicated help section but
we could put everywhere we need to a helper hover over question mark tooltip"*.

| Brief item | State | Evidence |
|---|---|---|
| Agents & workflows as the centre | partial | Nav leads with Overview; Agents is second |
| **Sub-agents** | **does not exist** | No `parent_task`/`child_task` in the UI **or in the frozen contract** |
| Inspect an agent's work | partial | `AgentDetail` is good; it is the only one |
| **Checkpoints** | **unreachable** | `loadCheckpoints` (api.ts:379) — **no screen calls it** |
| **Logs** | **unreachable** | `loadTaskLogs` (api.ts:414) — **no screen calls it** |
| Stats | partial | `PlatformCounts` exists, behind a button |
| **Visualize the resulting work** | **absent** | Artifacts are LISTED; nothing renders their content |
| **Inspect the outputs** | **absent** | No viewer, no download, no preview anywhere |
| Resource consumption | partial | On task detail only; absent from every graph node |
| Costs | partial | Same |
| Duration | partial | Same |
| Inputs and outputs | **absent** | No input shown; no output viewer |
| **Graphs and diagrams everywhere** | **effectively absent** | **0** charting deps; **3** CSS bar rows; `svg` in exactly **1** file |
| Futuristic / Lens-like | not started | Plain document layout, system fonts, no visual language |
| **Remove prose to a Help section** | **not started** | **108** `className="muted"` prose blocks; **6** legend/`<dl>` blocks |
| **`?` hover affordance** | **not started** | **0** instances. 94 native `title=` attrs, which are not the same thing |
| **Dedicated Help section** | **does not exist** | No `Help*.tsx` |
| **Logo / branding** | **does not exist** | No logo in any component or in index.html |

## The three that are not UI work

**S1 — Sub-agents do not exist in the model.** The brief says "workflows of
agents **and sub-agents**". A workflow step is a task; there is no parent/child
task relationship anywhere, including in `apps/common/swarm_common/models.py`,
which is FROZEN. This cannot be built in the UI. It is a **contract change
request** and belongs in `docs/contract-change-requests.md` with the shape the
UI would need — at minimum a nullable parent task id and a way to list children
— so the owner can decide. Nobody should fake it by inferring hierarchy from
`depends_on`: a dependency is not a parent.

**S2 — Nothing can show an output.** `GET /v1/tasks/{id}/artifacts` lists
artifacts and the run really does produce them — `wf_5e5ad3b6f7da4299a839`
yielded `synthesis.md`, five `<step>.md` files and `claude-transcript.json`.
There is no route that returns an artifact's CONTENT and no screen that renders
one. "A way to visualize the resulting work" is the headline ask of the brief
and it is the single largest unbuilt thing. This needs a server route first.

**S3 — There is no stop control.** `POST /v1/tasks/{task_id}/cancel` exists
(routes/tasks.py:116) and works — it cancelled six tasks cleanly when used at
the workflow level on 2026-09-22 — but **no UI calls it**. An operator watching
an agent burn tokens on the wrong thing cannot stop it from the console.

## New build items

* **B26 — The node is the way in.** `StepNode` (Workflows.tsx:317) is a plain
  component with no href and no onClick. Clicking a node must open that agent
  run: its runtime environment, live and final logs, checkpoints, artifacts,
  outputs, spend and duration. `loadCheckpoints` and `loadTaskLogs` already
  exist and have never been called — this is their caller. Blocks the brief's
  "great tooling to inspect the work an agent has done".
* **B27 — Two graph modes.** A collapsed summary for scanning many workflows,
  and a full visual DAG canvas for one. The collapsed mode must still convey
  topology, not just a count.
* **B28 — Stop a run from the console.** A cancel control on a running node and
  on the run detail, wired to the existing route. It is destructive and
  irreversible for that attempt, so it confirms first and names what it will
  stop. Report whether an attempt can be stopped without failing the workflow
  when `on_step_failure: continue`.
* **B29 — An artifact viewer, and the route under it.** Render text artifacts
  and transcripts in-page; offer download for the rest. The server route that
  returns content does not exist yet — specify it before building the UI, and
  note that artifacts are tenant-scoped GCS objects, so the route must enforce
  the same tenant boundary the artifact list does and must never proxy a path
  the caller supplies.
* **B30 — Identity: logo and header.** A real product header with a logo, the
  environment, the tenant and the signed-in principal. Today `index.html` and
  every component carry no branding at all.
* **B31 — Sub-agents, as a contract request.** Write the request; do not build a
  fake hierarchy from `depends_on`. **Filed 2026-09-24** as
  [request #14](../contract-change-requests.md#14-modelspy-a-sub-agent-has-nowhere-to-name-its-parent),
  open.

## Correction to half one

Half one said the checkpoint and log routes "already exist with no screen
calling them". That was right about the routes and understated the situation:
the **client loaders exist too** (`api.ts:379`, `api.ts:414`), fully written,
and are dead code. The work is further along and more wasted than reported.
