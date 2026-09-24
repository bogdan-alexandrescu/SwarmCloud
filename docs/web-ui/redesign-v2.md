# SwarmCloud UI — redesign v2

## Status, 2026-09-24

**Most of this is built. This block is the status; the body below it is the
design record.** The body is kept as written at `f0154b4`: what was believed,
measured and proposed then, including claims that are now false. It is not
edited to match what shipped. That form (a dated status block, and no
"Superseded" rewrite of the body) is the owner's answer of 2026-09-24 to
`ui-audit-and-build-prompt.md` §B13 **D6**.

Every row below was checked against the code at `b0fff1b`, and the file:line
references are to that commit. Other lanes were changing
`AgentDetail.tsx`, `AttemptTimeline.tsx`, `RunFiles.tsx`, `charts/`, `api.ts`
and `Workflows.tsx` while this was written, so some of those line numbers will
move.

**Shipped**

| Proposed in | What | Where it is at `b0fff1b` |
|---|---|---|
| §2.2 (1) | **Rail** | `App.tsx:849`, the `nav.ctl-rail` fed from `SECTIONS` |
| §2.2 (2) | **Full-bleed work area** | `.app` is capped by `--app-max` (≥ 1600px, held by `brand.test.tsx` B19) instead of 1100px; prose alone is clamped to `--measure` |
| §2.2 (3) | **Inspector** | `App.tsx:730` and `:761` (`has-inspector`), `panes.ts:36` `INSPECTOR`, the resize grip at `App.tsx:1323` |
| §2.2 (4), §2.4 | **Dock with provenance** | `Dock.tsx:63`: the DataSources strip collapsed to one line that expands on click. The dock carries provenance only. Logs render in the inspector (below), not in the dock |
| §1.0, §6 F0 | **F0 decoder** | `apps/swarm-api/swarm_api/codec.py:278-282`: `attempt_from_dict` now passes all five spend fields |
| §6 S2, dock | **Logs route** | `GET /v1/tasks/{id}/logs` at `apps/swarm-api/swarm_api/routes/tasks.py:291`, redacted at read time. UI: `RunFiles.tsx:284` through `api.ts:444` `loadTaskLogs` |
| §6 S3 (artifact half), Panel 3 | **Artifact content route** | `GET /v1/tasks/{id}/artifacts/content` at `routes/tasks.py:200`. It resolves an artifact by manifest name, never by path. UI: `api.ts:648` `loadArtifactContent` |
| Panel 4 | **Checkpoint list** | `GET /v1/tasks/{id}/checkpoints` at `routes/tasks.py:246`, across attempts and read-only. UI: `RunFiles.tsx:96` through `api.ts:408` `loadCheckpoints` |
| Panel 1 | **Fencing fields** | `task_to_api` serves `current_generation` and `current_lease_id` (`codec.py:168-199`), typed at `types.ts:406-427`. `gen N` is drawn per attempt (`AgentDetail.tsx:1187`) and per event (`AgentDetail.tsx:2962`, `AttemptTimeline.tsx:324`). No screen reads `current_generation` itself yet |
| §4, §5.8, Panel 7 | **visx charts and TokenSpend** | `charts/TimeSeries.tsx:94` is the only module that imports visx. It uses four `@visx/*` 4.0.0 packages from `package.json`. `charts/TokenSpend.tsx:86` is mounted at `AgentDetail.tsx:1058` |
| §4 #5, Panel 6 | **Requested vs peak memory** | a `Util` bar with the resource class's `memory_gib` as its ceiling, at `AgentDetail.tsx:1430-1438` |
| §9 | **Help section and `?` cards** | `HelpSection.tsx:33` is mounted at `App.tsx:772`; `HelpCard.tsx:813`; the topics are in `help.ts` |
| (not in the body) | **StopRun** | `StopRun.tsx:101`, on an agent (`AgentDetail.tsx:575`) and on a workflow (`Workflows.tsx:1990`) |
| Panel 3 | **ArtifactViewer** | `ArtifactViewer.tsx:42` (`Markdown` at `:361`, `Transcript` at `:563`), opened from `AgentDetail.tsx:2000` |
| (not in the body) | **Brand** | `Brand.tsx` `ProductHeader`, mounted at `App.tsx:760`: the mark, the wordmark, an environment badge that is measured rather than hardcoded, and the identity |
| (not in the body) | **Submit form inputs** | `Submit.tsx:543` `InputFields` and `:340` `buildInput` replace the JSON textarea; `SubmitWorkflow.tsx:530` reuses them |
| §2.4 Overview | **Overview workflow and parked checks** | `checks.ts:566` `workflowCheck` and `checks.ts:706` `parkedCheck`, both run by `deriveChecks` (`checks.ts:133`) |
| §4 #1 | **Semantic zoom on the workflow graph** | `dag.ts:372` onward sets which fields a node drops at each tier; the control is at `Workflows.tsx:1060-1106` |
| §2.1, §2.4 | **Three-section nav** | `App.tsx:214-404` `SECTIONS`: Overview as the landing screen, then Work · Capacity · Admin. Runtimes and History became panes, not sections. The fifteen screens keep their own routes. This replaces §2.4's six sections |

**Still open at `b0fff1b`**

- **The workflow timeline and table views** (§2.3; §4 #2). The board offers
  Rows and Graph only (`Workflows.tsx:350-365`). `Work ▸ Timeline`
  (`App.tsx:275`) is the history of past runs, not §2.3's timeline mode.
- **The duration bar** (§4 #3; §9's measured example). No screen has the
  queued → leased → dispatched → starting → running segmented bar. Durations
  are shown as figures.
- **Peak RSS over time** (§4 #4). No step line has been built. Peak RSS is shown as a
  figure per attempt (`AttemptTimeline.tsx:286`) and as the requested-vs-peak
  bar listed above.
- **The checkpoint strip** (§4 #6). Checkpoints are a list in `RunFiles.tsx`,
  not a dot strip on the attempt timeline.
- **The diffstat** (§4 #7). Per-commit `+N −M` is printed as table figures
  (`AgentDetail.tsx:2413`), not as diverging bars.
- **`input_from` edges** (§4 #1, #9). The DAG draws edges from `depends_on`
  only (`dag.ts:1290`).
- **Scrubbers** (§2.3). Not built.
- **Events paging** (§6 S1). `GET /v1/tasks/{id}/events` takes `limit` and
  no cursor (`routes/tasks.py:136-144`), so the tail-truncation risk in §4
  still stands.
- **Cross-task attempts** (§6 S4). No route aggregates attempts across tasks.
- **Checkpoint content** (§6 S3, the checkpoint half). The route serves a
  listing and no bytes. The decision on its shape is recorded below.

**The six questions in §8 are all answered**

- **Q4, the chart library: visx.** The owner decided it on 2026-09-22 (the
  decision is recorded in `apps/swarm-ui/src/charts/README.md`). Four
  `@visx/*` packages are used, and only `charts/TimeSeries.tsx` imports them.
  dagre was not taken: the DAG is hand-rolled (design-system.md §0).
- **Q1, Q2 and Q3 were answered by what got built, not by a recorded choice.**
  Q1: the shell (rail, full-bleed, inspector, dock) landed around the existing
  screens. Q2: F0 shipped as its own fix (`codec.py:278-282`). Q3: both routes
  were built, logs and artifact contents.
- **Q5 was answered by the §9 owner directive** (2026-09-22). Prose comes off the
  data surfaces and the explanation moves into Help and the `?` cards. The
  fact itself stays on the surface.
- **Q6: both themes survive** (design-system.md §1.1).
- **S3 checkpoint content: the owner decided on 2026-09-24 that it is a
  listing, per-file content, and a whole-checkpoint download.** The listing is
  built. Per-file content and the whole download are not yet.

---

*The design record, unchanged from here down.*
**Base commit:** `f0154b4`.
**Supersedes:** nothing. `docs/web-ui/redesign.md` decided the *navigation* and the
owner amended it on 2026-09-21; that decision stands and is defended, not reopened,
in §2.

This document answers a brief with eight asks: look at Lens but more futuristic;
centre on agents and workflows of sub-agents; tooling to inspect an agent's work,
checkpoints, logs and stats; a way to visualise the work produced; resource
consumption, cost, duration, inputs and outputs, with a way to inspect the outputs;
graphs and diagrams everywhere; the cluster's state, visually.

Three of those eight are blocked on something other than design, and one of the
three is blocked on a five-line bug rather than on missing infrastructure. That is
the most important sentence in this file, so it is §1.0.

---

## 1. What is wrong with the current UI

"Very hard to read" is not a matter of taste here. It decomposes into one data bug,
four rendering bugs, and four structural problems. Every one is cited to a file and
a line, and every one was checked at `f0154b4` rather than taken from a report.

### 1.0 Every cost figure in the product is unreachable, and the code blames the wrong thing

`apps/swarm-api/swarm_api/codec.py:211-229` — `attempt_from_dict` builds an
`Attempt` from a Firestore document and never passes `input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` or
`cost_usd`. The dataclass defaults (`None`) therefore apply on every read, and
`attempt_to_api` (`codec.py:333-337`) faithfully serves those `None`s.

Verified by execution, not by reading — `attempt_to_api(attempt_from_dict(doc))`
against a document carrying real values:

```
input_tokens                       firestore=12345          api=None
output_tokens                      firestore=678            api=None
cache_read_input_tokens            firestore=900            api=None
cache_creation_input_tokens        firestore=100            api=None
cost_usd                           firestore=0.0642028      api=None
peak_rss_bytes                     firestore=123456789      api=123456789
```

`peak_rss_bytes` is the control: the harness is correct, the round-trip works, and
exactly the five spend fields are dropped.

The write path is fine. `lifecycle.py:688-694` extracts usage and calls
`control.record_spend`, which merge-sets those five keys with a
`not isinstance(v, bool)` guard (`control.py:497-530`). The numbers reach Firestore
and die at the decoder.

Two things make this worse than an ordinary bug:

- **The comment above the serialiser blames the worker.** `codec.py:330-332` says
  "Null until the worker fix ships in an agent-runtime-base image and attempts run
  on it." That is now false, and it is the first thing anyone debugging this will
  read.
- **A passing test protects it.** `tests/unit/control_plane/test_leases_and_attempts_read_path.py:151-156`
  (`test_absent_usage_is_null_not_zero`) asserts the five fields are `None` on an
  attempt that never recorded spend. That is the only test touching them. Nothing
  asserts they are present when the document carries them, so the fix has no
  failing test to turn green.

**Consequence for this redesign.** Build any cost visualisation today and it renders
em dashes forever — correctly, per the honesty rules — and the conclusion drawn will
be "the worker isn't capturing tokens yet." That conclusion was already reached once:
`docs/web-ui/redesign.md` §6.2 records it, and `AgentDetail.tsx` omits the cost
columns deliberately on those grounds. The UI is currently hiding a working feature
because a decoder is silently lossy.

This is not a missing seam. It is five lines. It is ranked as seam **F0** in §6 and
it should land before any of this proposal.

### 1.1 Four rendering bugs, all from duplicate selectors in one stylesheet

`apps/swarm-ui/src/styles.css` is 1,877 lines with no layering, and four selectors
are declared twice. Equal specificity, so the later declaration wins the conflicts
and the earlier one survives where there is no conflict — which is the worst of both.

**(a) `.bar` is two different components.** Declared at `styles.css:120` as a 5px
meter track (`height: 5px; border-radius: 999px; background: var(--surface-2);
overflow: hidden`) and again at `styles.css:903` as a padded banner
(`padding: 9px 12px; border: 1px solid var(--line); border-radius: var(--radius)`).
With `* { box-sizing: border-box }` (`styles.css:53`), a declared `height: 5px`
against 18px of vertical padding plus 2px of border cannot be honoured: the used
border box becomes 20px and **the content box is 0px**.

There are exactly four `.bar` call sites and they are the four worst places for this
to happen:

| Site | What it is | Result |
|---|---|---|
| `Capacity.tsx:314` | pool saturation meter | `.bar > i { height: 100% }` of a 0px content box → **the fill never renders**. The comment directly above it at `styles.css:119` reads "The bar is the whole point of the card: saturation at a glance." |
| `AgentDetail.tsx:576` | `park_reason` banner | 20px box, ~37px of content → **clipped** |
| `AgentDetail.tsx:592` | `blocked_by` banner | 20px box → **clipped** |
| `AgentDetail.tsx:616` | cancellation-requested banner | 20px box, ~56px of content → **two of three lines gone** |

Three of the four are the UI's explanation of *why an agent is not running*. The
fourth is the only saturation meter on the Pools screen. The 999px pill radius is
also overridden to `var(--radius)`, so even the track reads as a box.

*(I confirmed the two declarations, the `box-sizing` rule, the four call sites and
the box-model arithmetic by reading. Lane 3's exact pixel measurements — borderBoxH
20, scrollHeight 37/56 — came from headless Chrome and I did not re-run that browser
myself.)*

**(b) `@keyframes pulse` is declared twice** — `styles.css:160`
(`0%,100% { opacity: .5 } 50% { opacity: .85 }`) and `styles.css:900`
(`0%,100% { opacity: 1 } 50% { opacity: .35 }`). The second wins globally, so
`.skeleton`, authored as a subtle 0.5→0.85 breathe, now flashes 1→0.35 — roughly
three times the amplitude — on every loading placeholder in the product.

**(c) `.filters` is declared twice** — `styles.css:202` and `styles.css:514` — with
different `gap` and `font-size`.

**(d) `.panel` has no box.** The only rule matching it in 1,877 lines is
`.panel h2 { display: flex; align-items: center; gap: 10px }` (`styles.css:739`).
Every `<section className="section panel">` — the structure `PlatformCounts.tsx` and
several other screens are built from — has no border, no background, no padding and
no boundary. Combined with `.section { margin-bottom: 28px }` (`styles.css:90`), a
"panel" is a 28px gap between runs of text.

That is the wall of text, precisely located. The UI believes it has panels and does
not have them.

### 1.2 The whole application is a 1100px centred column

`styles.css:69` — `.app { max-width: 1100px; margin: 0 auto; padding: 24px var(--gutter) 64px; }`.

There is no rail, no second pane and no dock. Every screen is a one-dimensional
vertical stack, so comparison is done by scrolling. On a 27" monitor most of the
glass is empty while `table.pools` and `.ctl-table` scroll horizontally — the worst
possible outcome for a product whose job is putting numbers beside each other.

Nothing in the sheet constrains a *prose* measure either. The only `max-width`
declarations are `.acct-facts` 560px, `.acct-action > *` 620px, `.acct-wide` 420px,
`.dsp-repo` 520px and `.node-dep` 190px — five rules across two screens. So the
column is too narrow for tables and far too wide for sentences at the same time.
Lane 3 measured 156–165 characters per line on `.muted`, `.ctl-section-q`,
`.acct-instructions p` and `.conjunction` at a 1440px viewport, against a
comfortable range of 45–90. I did not re-measure those figures; the absence of any
measure constraint in the sheet I did verify.

This falls hardest on exactly the prose that carries the honesty rules: the
partial-read warnings, the section questions, the "what this number excludes"
captions.

### 1.3 There is not one SVG in the product

```
grep -rl '<svg\|viewBox\|<path \|<polyline\|<circle' apps/swarm-ui/src/   →  (nothing)
```

Across all 21,040 lines. `Activity.tsx`'s `Chart` is a `<div className="chart"
role="img">` with CSS bars; `Workflows.tsx:25` says "Deliberately SVG-free."
`package.json` has exactly two dependencies, `react` and `react-dom`.

Against a brief that says "graphs and diagrams everywhere possible", the current
count is zero, and there are seven independently implemented bar primitives doing
the work a chart layer would do: `.bar` (5px), `.stack` (12px), `.stackcol` (120px),
`.coverage` (4px), `.sr-bar` (9px), `.acct-bar` (10px cells), `.ctl-util-track`
(10px). Seven implementations of one idea is why the same fact has three different
visual weights on three screens.

### 1.4 Meaning is carried by prose, and prose is what you read when you cannot see

This is the honest version of the complaint, and it needs care, because the prose is
the best thing about this UI.

`redesign.md` §3 specifies that `.ctl-metric.is-absent` renders "the value is a
**sentence**, not a figure" — and the implementation is there at `styles.css:1319-1323`.
That rule is *right*. But its consequence today is that `Overview.tsx` (2,418 lines)
and `AgentDetail.tsx` (2,548 lines) are dense with explanatory text the eye has no
way to skip, at 156–165 characters per line, in a 12–12.5px face.

The fix is not less honesty. **The fix is moving the sentence one layer back** — into
a tooltip, a hover card or an inspector — so a glyph reads in 200ms and the sentence
is still one gesture away. §5 and §"Honesty rules" make that concrete.

### 1.5 The type, contrast and colour systems have each run out of room

- **Type.** 15 distinct font sizes (9.5, 10, 10.5, 11, 11.5, 12, 12.5, 13, 13.5, 14,
  14.5, 15, 20, 22, 24), twelve of them inside a 5.5px band, five at half-pixel
  steps. Lane 3 counted 112 of 153 sized declarations (73%) at or below 12.5px.
  `body` is 15px and almost nothing inherits it. Hierarchy has been spent on
  distinctions too small to perceive.
- **Inverted hierarchy.** `.section > h2` (`styles.css:91`) is 12px/600 in
  `--text-faint`, the faintest token in the system, over body text at 12.5–13px in
  `--text-dim`, which is *more* prominent. The heading is smaller and fainter than
  what it heads.
- **Contrast.** `--line` is `#262d36` (`styles.css:8`). I computed its WCAG contrast
  independently: **1.28:1** against `--surface` `#14181d` and **1.18:1** against
  `--surface-2` `#1b2027`. The non-text UI-boundary floor is 3:1. That one token
  carries every card edge, every table row separator, the drawer border and the
  section rules — so the tables have no visible grid and the cards have no visible
  edge. Raising it is the cheapest legibility win available and it fixes dozens of
  rules at once.
- **Colour dies in greyscale.** Computing relative luminance from the tokens at
  `styles.css:20-24`: `--ok` Y=0.3633, `--warn` Y=0.3660, `--info` Y=0.3657 — **one
  shade**. Six state colours resolve to two greys in dark mode. Where a word
  accompanies the colour (`.ctl-chip`, `.liveness`) the design survives; where
  colour alone carries state it does not — `.stackcol > i.failed` versus
  `.cancelled` is indistinguishable, so the Activity outcome chart renders failures
  and cancellations identically.
- **Colour means too many things.** `--info` currently means running/live, selected,
  clickable, focused, generic quantity, informational-not-broken, and the admin
  gate — seven meanings on one hue, so a selected tab and a live agent read alike.
  `--ctl-absent` is literally `var(--text-faint)` (`styles.css:1207`), which also
  means cancelled, unknown, idle, section heading and small print. **"Cancelled" (an
  outcome) and "not measured" (an absence) are rendered in the identical colour** —
  the exact collapse the rest of this codebase exists to prevent.

### 1.6 Detail costs you your place

`AgentDetail.tsx:70` is `role="dialog"` — a full-screen drawer. Reading one agent
takes the list of the others off the screen. Every reference product in this class
(Lens, Temporal, Weave) inspects in an overlay or a third pane precisely so that
does not happen.

### 1.7 One stale comment that will mislead the next reader

`apps/swarm-ui/src/api.ts:103-107` says `GET /v1/tasks/{id}/artifacts` "is
deliberately NOT called: it reads a Firestore subcollection nothing writes, so it
returns [] for every task forever."

That route was repaired. `store.list_artifacts` (`store.py:540-584`) now reads
`task.result_summary`, and its docstring cites *this very comment* as the workaround
that got written instead of the fix. Nothing is broken — the UI gets the same data
off `result_summary` — but the comment now describes a live route as dead. **Track
conflict, reported not patched:** `api.ts` is Track A's file.

---

## 2. The proposed information architecture

### 2.1 What does not change: the six nouns

`App.tsx:32-60` carries the argument and `App.tsx:101-190` carries the structure:
**Overview · Agents · Runtimes · Pools · History · Admin**, each with a `question`
field that is the membership test for any new screen. Eleven items went to five,
then six; the owner renamed Capacity→Pools and Activity→History on 2026-09-21 on
the grounds that Temporal and Nomad name sections after objects.

**I am not proposing new section names.** That would re-litigate a decision the owner
already made, and none of the three measurable causes of "hard to read" is the
section list. The membership test also correctly rejects the obvious Lens-shaped
mistake: a rail of Tasks / Attempts / Leases / Events / Artifacts / Checkpoints is
the Firestore schema, not the work, and nobody opens this product asking "show me
leases."

### 2.2 What changes: the shell

The one idea worth taking from Lens is not its tree — it is its **Dock**: a
persistent, resizable bottom region for output that does not take the list away.
Temporal arrives at the same idea independently (a child workflow's timeline opens
inside the parent's summary, without navigating). Weave arrives at it a third time
(a three-pane list / tree / detail layout).

```
┌────────┬──────────────────────────────────────────┬──────────────────┐
│        │  WORK AREA (full-bleed)                  │  INSPECTOR       │
│  RAIL  │                                          │  (right pane,    │
│        │  ┌────────────────────────────────────┐  │   resizable,     │
│ Over…  │  │ view-mode toggle: DAG │ Time │ Tbl │  │   dismissible)   │
│ Agents │  ├────────────────────────────────────┤  │                  │
│ Runti… │  │                                    │  │  the selected    │
│ Pools  │  │   one object, several view modes   │  │  node, in full   │
│ Histo… │  │                                    │  │                  │
│ Admin  │  └────────────────────────────────────┘  │                  │
│        ├──────────────────────────────────────────┴──────────────────┤
│        │  DOCK  ── logs · events · artifact preview ──  [collapse]   │
└────────┴─────────────────────────────────────────────────────────────┘
```

Four decisions, each with a reason:

1. **A persistent left rail replaces the top nav, at a fixed position.** The rail
   never reorders, so after a week you go to a position rather than reading a label.
   The current tab strips move with the section, so there is no position to learn.
2. **`max-width: 1100px` is removed from `.app`.** Tables, charts and graphs take
   the viewport. A new `--measure: 68ch` is applied to prose blocks *only*. A
   control plane is not a document and should not be set like one.
3. **The full-screen `role="dialog"` becomes a right-hand inspector.** Selecting an
   agent never removes the list of agents.
4. **A dock is added.** Logs, the event stream and artifact previews render there,
   and it survives navigation within a session. This is where the two currently
   unbuildable asks (§6) will land when their routes exist, so the layout should
   make room for them now rather than be retrofitted.

### 2.3 The centre of the redesign: one object, several view modes

The single highest-leverage import from the prior art is Weave's: **the view mode is
a property of the pane, not a route.** The same workflow becomes a DAG, a wall-clock
timeline and a table without navigating, which answers "graphs and diagrams
everywhere" without multiplying a 23-screen app into a 40-screen one.

Applied here, at two levels:

**A workflow** (`Agents ▸ Workflows`) gets three modes over one object:

- **Graph** — the agent/sub-agent DAG, laid out from `WorkflowStep.depends_on` and
  `input_from`. This is the screen the brief is really describing when it says
  "workflows of agents and sub-agents".
- **Timeline** — the same nodes on a wall-clock axis, so the question becomes "where
  did the four hours go" rather than "what depended on what". Temporal's distinction
  between its Compact and Timeline views, and it is the right one.
- **Table** — the same nodes as rows, sortable by duration, cost, attempts, state.
  The mode you want when there are sixty steps and you are looking for the outlier.

**An attempt history** (inside an agent) gets the same treatment at a smaller scale:
Compact (what happened), Timeline (where the time went), Full (every event, for the
3am question "what exactly did the reconciler do at 03:12").

Selecting any node in any mode fills the inspector. Nothing navigates.

**Scrubbers**, also from Weave, and cheap: *next attempt of this task*, *the same
step across the last ten workflows*, *the next agent on this runtime*. Keyboard
movement between comparable objects is how you avoid building a comparison screen
for every pair.

### 2.4 What each screen answers, and what it deliberately does not show

The `question` fields are quoted verbatim from `App.tsx` where they exist.

| Section | Answers | Deliberately does not show |
|---|---|---|
| **Overview** | "Is the platform healthy right now, and if not, what is the first thing to look at?" | No history, no trend lines — there is no time-series store (§7). No alert list; there is no alerting engine. |
| **Agents** | "What is running, what is waiting, what did it produce — and why has mine not moved?" | No platform-wide spend total (that is History). No infrastructure objects — a lease is shown as a reason a row is where it is, never as a row of its own. |
| **Runtimes** | "What kinds of agent can this platform run, where does each one run, and how big is one?" | No live utilisation — a runtime is a *spec*. Utilisation belongs to an attempt (Agents) or a pool (Pools). |
| **Pools** | "Is there room to run more, which ceiling is the binding one, and what is holding what there is?" | No per-agent detail. No pending-pod backlog — pending pods are never a queue here (CONTRACT invariant 1). |
| **History** | "What has this platform done over time, who used it, and what did it cost?" | No live state. Nothing auto-refreshing: `/v1/stats` bills per index entry scanned and gets more expensive as the platform ages (`PlatformCounts.tsx:9-17`). |
| **Admin** | "Change a ceiling, or see who is registered to use this platform." | No read-only data at all. If it does not change something, it is not here. |

Two screens do change section membership under this proposal, and both are
consequences of the shell rather than of renaming:

- **The attempt timeline** (`AttemptTimeline.tsx`, 249 lines, currently a screen)
  stops being a screen and becomes a *view mode* inside the agent inspector. It
  answers no question that "what is running / what did it produce" does not already
  own.
- **DataSources** (`DataSources.tsx`, 95 lines) moves into the dock as a permanent
  provenance strip, because "which of these seven reads succeeded" is context for
  everything on screen, not a destination.

---

## 3. The agent detail view

This is the screen the brief is really asking for. It is the inspector from §2.2,
opened on one task, and it is organised as **six panels plus the dock**.

For each panel: where the data comes from, and whether it can be built today.

Legend: **BUILDABLE** · **BLOCKED ON F0** (the five-line decoder fix, §1.0) ·
**NEEDS A ROUTE** (§6).

### Panel 1 — Identity and state — BUILDABLE

`GET /v1/tasks/{id}`. Name, runner profile, resource class, tenant, state, attempt
counter, `blocked_by`, `park_reason`, `next_eligible_at`, `latest_checkpoint`.

The three banners currently clipped by the `.bar` collision (§1.1) live here and are
the highest-value text on the screen: they are the answer to "why has mine not
moved."

### Panel 2 — The work produced — BUILDABLE, and richer than expected

Source: `task.result_summary.git`, served today through `task_to_api` and already
fully typed at `types.ts:522-580`. It carries `base`, `head`,
`commits[{sha, subject, author, committed_at, files_changed, insertions, deletions, binary_files}]`,
`commit_count`, `insertions`, `deletions`, `dirty[]`, `patch`, `patch_bytes`,
`patch_omitted`, `published`, `publish_reason`,
`pull_request{number, url, state, created}` and
`integrated{merged, conflicted, missing, complete}`.

This is the single richest "visualise the work an agent did" source in the platform
and it needs **no new route**. A commit-by-commit diffstat, a dirty-file list, a PR
badge carrying its reason, and a merge outcome for an integrator are all drawable
now.

Three rules this panel must not break:

- A missing pull request is **not** "none". `types.ts:494-500` names six distinct
  causes, and `publish_reason` is present on success as well as failure. Collapsing
  six operator responses into one word is a truth bug.
- `binary_files` must not fold into a 0/0 line count. Git prints `-` for both; the
  field exists so a binary change does not read as "changed nothing".
- `patch_omitted` is **not** "no patch". It means a patch existed and was discarded
  at the cap — because a truncated patch applies cleanly and silently drops the rest.

### Panel 3 — Inputs and outputs — MANIFEST BUILDABLE, CONTENTS NEED A ROUTE

**Inputs — buildable.** `result_summary.staged_inputs[{task_id, filename, path,
bytes, uri, from_checkpoint}]` (`inputs.py:94-105`) is what actually landed in the
workspace, including which upstream step each file came from. That is a direct edge
back into the workflow DAG and should be drawn as one.

**Output manifest — buildable.** `GET /v1/tasks/{id}/artifacts` returns
`{artifacts: [{name, bytes, uri}], artifacts_skipped: [name], artifact_bytes,
complete}`. `complete` is false until the task is terminal, which is what stops an
empty list reading as "produced nothing". `artifacts_skipped` names files dropped at
`max_artifact_bytes`, so a short list carries its own reason.

**Output contents — NEEDS A ROUTE.** The `uri` is `gs://`. Verified end to end:

- the artifact bucket sets `public_access_prevention = "enforced"` and
  `uniform_bucket_level_access = true` with no CORS
  (`terraform/modules/storage/main.tf:25-26, 69-70`);
- the load balancer routes only `/v1`, `/v1/*`, `/healthz`, `/readyz`, `/metrics`,
  `/docs`, `/openapi.json` to the API, with no `backend_bucket`
  (`terraform/modules/frontend/main.tf:267`);
- nothing in the repository mints a signed URL — `generate_signed_url` and
  `signBlob` appear nowhere in `apps/` or `terraform/`;
- `apps/swarm-api/pyproject.toml` has no `google-cloud-storage` dependency.

So a browser cannot read an artifact byte today. **The IAM already exists:**
`terraform/modules/iam/bindings.tf:228-233` grants swarm-api
`roles/storage.objectViewer` on the artifact bucket, unconditioned, with the comment
"The API serves result artifacts back to callers, so it reads objects" — a behaviour
it does not have. The seam is built at both ends with nothing in the middle. See
seam **S3**.

### Panel 4 — Checkpoints — LIST BUILDABLE, CONTENTS NEED A ROUTE

The brief says there is no checkpoint route, and that is true — but the checkpoint
*list* is already fully modelled. `types.ts:884-906` documents the assembly: it is a
join of two records, because neither is complete alone.

- `attempt.checkpoints` is the authoritative list of ids (`list[str]`, `models.py:215`),
  served by `attempt_to_api` today.
- the `checkpoint_completed` **event** for an id carries `{checkpoint_id, uri,
  size_bytes, seq}` — the only place the size and location exist per checkpoint.

So a checkpoint strip with id, sequence, size and time is buildable now from
`/v1/tasks/{id}/attempts` + `/v1/tasks/{id}/events`, with `eventOnly` and `uriKnown`
already defined to distinguish a real inconsistency from a paging artefact.

Two caveats the panel must state rather than smooth over:

- **`types.ts:894-900` names the truncation trap itself:** the events route orders
  oldest-first, caps the page and returns no token, so "the later checkpoints of a
  long attempt are exactly the ones whose events fall off the end." A row with an id
  and no uri means the event is not on this page — not that the checkpoint is broken.
  See seam **S1**.
- **"CONTENTS ARE NOT RECORDED ANYWHERE"** (`types.ts:902-903`). Only the id, size
  and uri. A file listing inside a checkpoint needs the same byte-serving route as
  artifacts — seam **S3**.

### Panel 5 — Duration — BUILDABLE, with one trap

Per attempt, exact: `created_at` (admission), `started_at` (worker start),
`completed_at`. For CLI runners, `result_summary.runner.usage.duration_ms` and
`duration_api_ms` separate wall time from API time.

A **full phase breakdown** is derivable from `/v1/tasks/{id}/events` with no new
route: `submitted → ready → lease_acquired → dispatched → starting → running →`
terminal, plus `parked`, `retrying`, `quota_exhausted`, `generation_fenced`,
`lease_released`, `checkpoint_*`, `heartbeat`. That yields queue wait,
admission→dispatch, dispatch→start (container cold start), start→running, and
running duration as separate segments.

**The trap: `task.started_at` is overwritten on every attempt.** `control.py:410-413`
sets it in the `DISPATCHED → STARTING` transition, which runs once per attempt. On a
retried task it is the *last* attempt's start. So `completed_at - started_at` is the
last attempt's runtime, and `completed_at - created_at` is total wall time including
queue and every park. Neither is "how long the agent worked." Only summing
per-attempt intervals gives that, and this panel must do the summing.

### Panel 6 — Resource requested vs used — BUILDABLE, but memory and disk only

**Measured:** memory and disk. `ResourceSampler` (`agent_worker/metrics.py:147-216`)
reads cgroup v2 `memory.peak`, falls back to `memory.current`, falls back to summing
`/proc/*/statm`; disk is an `os.walk` sum (`workspace.py:108-119`).

**Not measured anywhere: CPU.** `grep -n 'cpu' apps/agent-worker/agent_worker/metrics.py`
returns nothing. There is no CPU sampler and no `cpu.stat` read.
`/v1/resource-classes` serves `cpu` (4 / 8 / 8) as the *requested* side with no
"used" side existing in any store, log or metric. **A requested-vs-used chart must
simply not have a CPU row** — an empty row would imply a measurement that was taken
and came back zero.

**What reaches Firestore is high-water marks only.** `control.record_resource_usage`
(`control.py:484-495`) writes `peak_rss_bytes`, `peak_disk_bytes` and `oom_near_miss`
once at teardown. `ResourceUsage.samples` (`metrics.py:46`) is a *count*; the samples
themselves are discarded, and `merge_rss`/`merge_disk` are `max()`. Cloud Monitoring
gets three GAUGE points per attempt, also written once at the end, and swarm-api
holds no `roles/monitoring.viewer`, so that path is closed to the UI regardless.

Three rules:

- **Do not stack memory and disk.** `apps/common/swarm_common/profiles.py:61-67` says
  it in the frozen contract's own words: the workspace is memory-backed tmpfs, so
  "`disk_gib` is a slice OF `memory_gib` and not additional capacity."
  `peak_disk_bytes` is *inside* `peak_rss_bytes`. A stacked bar double-counts.
- **Mark the hazard band.** `oom_near_miss` fires at `NEAR_MISS_FRACTION = 0.90`, and
  `requests == limits` platform-wide (CONTRACT invariant 7), so 90–100% of the class
  ceiling is the entire safety margin. A memory gauge that does not mark that band is
  hiding the only thing the number is for.
- **`peak_rss_bytes: null` is not 0.** It is null on every attempt that never started
  a sampler.

### Panel 7 — Cost and tokens — BLOCKED ON F0

Every field this panel needs exists in Firestore and is dropped by the decoder
(§1.0). After F0 it is buildable with no new route.

`Attempt` carries `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `cost_usd` (`models.py:232-236`). Split three ways —
input, output, cache — and rolled up the workflow, this is LangSmith's model and the
data supports it directly.

Three coverage gaps the panel must **state**, not smooth over:

- **`record_spend` is called from the clean-exit path only.** It has exactly one
  caller, `lifecycle.py:694`, and the comment fourteen lines above it reads: "The
  ONLY call that passes `publish=True`. The agent exited on its own here; the other
  five call sites are parks and crashes." A quota park, a cancel, a SIGTERM, a crash
  and a generation fence all record **no spend** — and those are precisely the
  expensive attempts. A run parked at hour two burned two hours of tokens and reports
  nothing.
- **Only `claude-code` and `codex` emit a usage block at all.** `mock`, `generic` and
  `browser` report nothing. `types.ts:592-593` already states the rule: "NOTHING IS
  NOT ZERO. Render an em dash, never $0.00."
- **`result_summary` is written once, at terminal state.** A RUNNING agent has no
  figure, and a PARKED one never will for the attempt it lost.

`record_spend` also **drops the model list**: `control.py:510-514` writes only the
four token fields plus `cost_usd`, while `_usage_summary` (`lifecycle.py:2344-2375`)
also produces `models`, `num_turns`, `duration_ms`, `duration_api_ms` and
`thinking_tokens`. Those survive only inside `task.result_summary["runner"]["usage"]`,
an untyped dict no index can reach — readable on one agent, not aggregatable. The
docstring at `control.py:501-504` says that is deliberate. Cross-task spend needs
seam **S4**.

### The dock — logs — NEEDS A ROUTE, but less of one than expected

The brief says live logs "are written to GCS by the worker and nothing serves them."
Both halves are true, and the writer is much better than "written to GCS" suggests.

`lifecycle.py:1552-1601` publishes a bounded, scrubbed **tail** of each stream to
`{log_prefix}/live/{stdout,stderr}.tail.log`, and its docstring records three
decisions worth preserving in any reader:

- a tail and not the file, because GCS has no append and publishing the whole stream
  would make the cost of watching a run grow with the run's length;
- scrubbed on **every flush**, not only on the way out, because "it is gone five
  seconds later" is not a property anyone should rely on for a credential;
- it never raises — a failed flush costs the watcher five seconds of staleness.

Each object is prefixed `#swarm-tail offset=<n> size=<n>`, deliberately, so a reader
can distinguish a **gap** (it polled too slowly and the window moved) from a
**continuation**. Any log viewer must read that header and say "output is missing
here" rather than silently stitching two non-adjacent pieces together.

And there is already a reader: `apps/swarm-mcp/swarm_mcp/cli.py:62-69` constructs
`gs://{bucket}/tenants/{tenant}/tasks/{task}/attempts/{attempt}/logs/live/{stream}.tail.log`
and tails it with the caller's own credentials. The browser cannot do that. Seam
**S2** is the same route as S3 with a different prefix.

---

## Honesty rules — what must survive this redesign unchanged

This is the best property of the current UI and the easiest thing to lose in a
visual redesign, because every rule here costs pixels and a chart library will
happily violate all of them by default.

The rules are **structurally enforced**, not merely documented.
`apps/swarm-ui/src/fetch.ts:1-17` states the reason: a sweep found 56 places in this
platform's operational scripts where a probe failure rendered as an absence —
`status.sh` printing "no swarm services deployed" when a session had expired; a build
writing a manifest of zero images and reporting "ok built 6 image(s)". Commit
`9c639af` is titled "An HTTP error is not an empty result."

1. **A failed read is never rendered as a zero.** Every value resolves through
   `Result<T>`, which has five states and **no member that is both a failure and an
   array** (`fetch.ts:54-60`). `empty` is separate from `ok` on purpose: a component
   that wants rows must say what it does when there are none and cannot reach `data`
   in that case.
2. **An em dash means "not measured"; `0` means a measured zero.** Enforced at the
   serialiser (`codec.py:330-332`), restated in the types (`types.ts:592-593`) and
   given its own visual treatment (`.ctl-metric.is-absent`, `styles.css:1319-1323`:
   dashed border, no shadow, the value rendered as a sentence).
3. **A partial read says so, and says what it could not see.** `Overview.tsx` fans
   out seven independent reads and counts what landed, what is in flight, what failed
   and what was refused for want of admin. Each panel fails alone.
4. **A total is never shown over a partial response.** `PlatformCounts.tsx:18-22`:
   tenant counts and platform counts are separate blocks with the scope named, "never
   side by side in one row — an admin reading their own four running tasks as the
   platform total is a truth bug, not a layout preference."
5. **A clamped list says it is a window.** `api.ts:31` records that the server clamps
   at 200. A list that has been cut must say so, not imply it is complete.

**The redesign changes where these sentences live, never whether they exist.** The
move is: glyph in the data region, sentence in a tooltip, hover card or inspector —
one gesture away, never removed, and never replaced by a colour alone. §5 adds the
testable form of the same idea: *if removing the colour removes information, the
treatment is incomplete.*

One addition the redesign forces, because charts make it possible to lie in a new
way:

6. **A chart must not interpolate across an absence.** A gap in a series is drawn as
   a gap. A line that connects the point before a park to the point after it claims a
   measurement that was never taken.

---

## 4. Visualisations

Everything in this table is sourced from data that exists at `f0154b4`. Desirable
things that cannot be drawn are in §6, not here. The one exception is marked
**[F0]** — the data exists and is written correctly to Firestore; only the decoder
drops it, and the fix is five lines.

| # | What is drawn | Route → field | Chart type | What it must not imply |
|---|---|---|---|---|
| 1 | **Agent / sub-agent DAG** | `/v1/workflows/{id}` → `WorkflowStep.depends_on`, `input_from` | Layered directed graph (dagre), node = step, edge = dependency; edge style distinguishes `depends_on` (ordering) from `input_from` (data) | That an unconnected node is orphaned — it is independent. That edge direction is execution order beyond what is declared. |
| 2 | **Workflow wall-clock timeline** | same nodes → per-task `created_at` / `started_at` / `completed_at` | Gantt on a time axis; in-flight bars dashed and animating | That a bar's length is agent work — it includes queue and parks. That a task still running has an end. |
| 3 | **Attempt phase breakdown** | `/v1/tasks/{id}/events` → `submitted`, `ready`, `lease_acquired`, `dispatched`, `starting`, `running`, terminal | Horizontal stacked bar, one segment per phase | That the segments sum to task wall time when the 200-event page truncated. The panel must say the page is a window (§6 S1). |
| 4 | **Peak memory reached by T+n** | `/v1/tasks/{id}/events` → `HEARTBEAT.detail.peak_rss_bytes`, `.elapsed_seconds` (~1 point / 150s) | Monotonic **step** line, never smoothed | That it is "memory now". `peak_rss_bytes` is a running maximum that never resets; labelled as current usage it claims a flat footprint for an agent that spiked once and released. Label it *"peak reached by T+n"* and it is exactly true. |
| 5 | **Requested vs peak memory** | `/v1/resource-classes` → `memory_gib`; `/v1/tasks/{id}/attempts` → `peak_rss_bytes`, `oom_near_miss` | Bullet bar, requested as the track, peak as the fill, **90–100% band hatched** | That there is a CPU row — CPU is not measured anywhere. That disk is additional — `disk_gib` is a slice **of** `memory_gib` (`profiles.py:61-67`), so memory and disk must never be stacked. That `null` is 0. |
| 6 | **Checkpoint cadence** | `/v1/tasks/{id}/attempts` → `checkpoints[]`; `/v1/tasks/{id}/events` → `checkpoint_completed.detail{uri,size_bytes,seq}` | Dot strip along the attempt timeline; dot area = `size_bytes`; hollow dot = `uriKnown === false` | That a hollow dot is a broken checkpoint — it means the describing event is off this page (`types.ts:894-900`). That the strip shows what is *inside* a checkpoint; contents are recorded nowhere. |
| 7 | **Work produced, per commit** | `task.result_summary.git.commits[]` → `insertions`, `deletions`, `files_changed`, `binary_files` | Diverging diffstat bars, one row per commit | That a binary change is 0/0 — `binary_files` exists precisely so it does not read as "changed nothing". That `patch_omitted` means no patch; it means a patch existed and was discarded at the cap. |
| 8 | **Publish / integrate outcome** | `result_summary.git` → `published`, `publish_reason`, `pull_request{}`, `integrated{merged, conflicted, missing, complete}` | Status chip with the reason on the chip, not behind it | That a missing PR is "none" — `types.ts:494-500` names six distinct causes and each is a different operator response. |
| 9 | **Input provenance** | `result_summary.staged_inputs[]` → `task_id`, `filename`, `bytes`, `from_checkpoint` | Edges drawn back into viz #1, one per staged file | That an input with no `task_id` came from nowhere — it came from the submission. |
| 10 | **Pool saturation** | `/v1/pools` → `in_use`, `hard_limit`, `adaptive_target`, `enabled` | Bullet bar, two ceiling markers (adaptive and hard), **hatched when `enabled == false`** | That a paused pool is at its ceiling. Compare `.enabled == false` explicitly — `false // true` is `true` in jq and has already broken this exact column once (CLAUDE.md). |
| 11 | **Concurrency by pool** | `/v1/leases` → `pools[]`, `units`, `state` | Stacked bar by pool | That it counts RUNNING. Concurrency counts from **LEASED** (CONTRACT invariant 3). |
| 12 | **Account quota windows** | `/v1/accounts` → `utilization` (0.0–1.0), `resets_at`, `observed_at`, `stale`, `state` | Radial gauge — the **only** genuinely 0..1 quantity in the platform — with an explicit staleness marker | That a stale reading is live. `usage.py:72-81` distinguishes `UsageUnavailable` from a zero on purpose and `usagepoll.py` refuses to record a failed poll; rendering a stale 12% as a live 12% undoes all of it. Also: never join an account to an agent — `Lease` and `Attempt` carry no account field, so "this account's quota is being burned by that agent" is unanswerable. |
| 13 | **Task state distribution** | `/v1/stats` → `tasks_by_state`, `platform_tasks_by_state` | Horizontal bars, tenant and platform in **separate blocks** | That the two scopes are comparable in one row (`PlatformCounts.tsx:18-22`). That `QUEUED`/`PARKED`/`READY` are demand — only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create infrastructure demand (CONTRACT invariant 1). |
| 14 | **Outcome mix over history** | `/v1/tasks` → `state` | Stacked bar by day | That failed and cancelled are the same thing. They currently render identically in greyscale (§1.5) and must not. |
| 15 | **Duration across retries** | `/v1/tasks/{id}/attempts` → per-attempt `started_at`, `completed_at` | Lollipop, one per attempt, with the summed total stated separately | That `task.started_at` is the task's start — it is overwritten per attempt (`control.py:410-413`). Only the sum of per-attempt intervals is agent work. |
| 16 | **Cost and tokens, rolled up** **[F0]** | `/v1/tasks/{id}/attempts` → `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cost_usd` | Stacked bar per attempt (input / output / cache-read / cache-create), summed to the task and to the workflow | That `null` is `$0.00`. That a parked or crashed attempt was free — `record_spend` runs on the clean-exit path only, so the most expensive attempts report nothing and the chart must say *"not recorded for N of M attempts"*. That a `mock` or `generic` runner spent nothing — it reports nothing. |

Two rows of that table (#4, #6) depend on the event page not truncating, and one
(#3) is actively dangerous without seam S1: a two-hour run emits roughly 6 lifecycle
events + ~48 heartbeats (30s × `HEARTBEAT_EVENT_EVERY = 5` = one per 150s) + ~120
checkpoint events (`checkpoint_interval_seconds = 120`) ≈ 175, and past 200 the
route drops the **tail** — the terminal event, the last checkpoints, the last
heartbeats. **A timeline built on `/events` today shows a long agent's beginning and
implies it never finished.** That is the single most dangerous truncation in the
current API for a timeline-heavy redesign.

---

## 5. The visual language

"Futuristic" cashed out as decisions with values. The through-line is: **the data
region carries no decoration, so every mark in it means something.**

### 5.1 Fix the collisions first

Nothing below matters while §1.1 stands.

- Namespace `.bar`. It is a two-character class doing two jobs. Proposal:
  `.meter` (the 5px track) and `.notice` (the banner).
- Delete one `@keyframes pulse` and one `.filters` block.
- Give `.panel` an actual box: `background: var(--surface); border: 1px solid
  var(--line); border-radius: var(--radius); padding: 16px`.
- Add a lint gate that fails on a duplicate top-level selector in `styles.css`. Four
  collisions in one sheet is a process gap, not four mistakes.

### 5.2 Type — six integer steps, nothing between

| Step | Size | Use |
|---|---|---|
| micro | **11px** | provenance, ages, raw ids. A hard floor. |
| meta | **12px** | labels, captions, table headers |
| body | **13px** | body and table cell — the workhorse |
| title | **16px** | panel and section titles |
| screen | **20px** | screen title |
| figure | **28px** | the one number a metric tile exists for |

Delete 9.5px and 10px entirely; nothing in an operations UI belongs below 11px.
Delete every half-pixel step — they are not perceivable as hierarchy but are real as
rendering inconsistency across DPI.

**Hierarchy when everything is data**, in priority order, because size has already
been spent:

1. **Weight and case on labels** — uppercase 11px/600 at 0.06em tracking for the
   label, the value at 13–28px. This is already the house pattern and the best thing
   in the sheet.
2. **Rules and surfaces rather than gaps.** One visible 1px hairline does more work
   than 28px of margin and costs 28px less.
3. **Position as a contract** — identity always left, figure always right,
   `font-variant-numeric: tabular-nums` globally rather than per rule.
4. **Colour reserved exclusively for state**, so the eye can use it as a scan
   channel instead of learning to ignore it.

Fix the inversion: `.section > h2` becomes 16px/600 in `--text`, above body text in
`--text-dim`.

### 5.3 Measure and width

- Remove `max-width: 1100px` from `.app`. Full bleed.
- Add `--measure: 68ch`, applied to prose blocks only: `.muted`, `.ctl-section-q`,
  `.conjunction`, `.acct-instructions p`, every honesty caption.
- **Tables, charts and DAGs take the full width; sentences never do.**

### 5.4 Contrast

- `--line`: `#262d36` → **`#39424e`** (dark). Currently 1.28:1 / 1.18:1 against the
  two surfaces; the non-text floor is 3:1. One token, dozens of rules, the cheapest
  win available.
- Light mode: `--warn` `#9a6700` measures ~4.37:1 against `--surface-2` and is the
  colour on `.row .why`, the blocked-reason line the CSS itself calls the whole
  point. It needs darkening; the earlier audit fixed `--text-faint` and did not
  re-check `--warn`.
- Delete `--ctl-shadow` in dark mode: `0 1px 2px rgb(0 0 0 / .30)` on `--bg`
  `#0b0d10` is ~1.02:1 — invisible, and it costs a paint layer on every
  `.ctl-metric`.

*(I recomputed the `--line` ratios and the ok/warn/info luminances myself. The
remaining chip-level contrast figures are Lane 3's and I did not re-derive them
individually.)*

### 5.5 Colour in three tiers

**Tier 1 — state. The only hues permitted inside the data region.** Exactly six:
`ok`, `live`, `warn`, `bad`, `paused`, `unknown`. Every one carries a **second,
non-colour channel** so it survives greyscale and deuteranopia without relying on an
adjacent word:

| State | Colour | Second channel |
|---|---|---|
| ok | `--ok` | solid fill, filled dot |
| live | `--info` | solid fill + the only animation on the screen |
| warn | `--warn` | 45° hatch (`--ctl-hatch` already exists and is already used correctly for unknown ceilings — extend it) |
| bad | `--bad` | solid fill + a 2px left rule |
| paused | `--paused` | **horizontal** hatch, deliberately distinct from warn's 45° |
| unknown / not-measured | `--ctl-absent` | dotted outline, hollow dot, **no fill at all** |

Then retune luminance so the six occupy at least **three** distinct greyscale steps
instead of two: lift `ok`, darken `bad`, pull `paused` toward blue-grey.

Split `--ctl-absent` off `--text-faint`. "Cancelled" is an outcome and "not
measured" is an absence; they must not share a colour.

**Tier 2 — interaction, which must not be any state colour.** Today "running" and
"selected" are the same blue, so a selected tab and a live agent read alike. Make
interaction **hueless**: selection is a 2px rule plus a surface lift, focus is a
near-white 2px outline, links are underline-on-hover. Cheaper to keep honest, and
more futuristic than a second accent.

**Tier 3 — surfaces.** Three steps, not five, plus one hairline at ≥3:1.

**The rule to write down, and the testable form of what this codebase already
believes:**

> If removing the colour removes information, the treatment is incomplete.

### 5.6 Density

A task `.row` is ~68px tall because `.row .agent` (`styles.css:535-538`) stacks
name, model and id vertically. A 900px viewport shows about ten rows. This is an
instrument: a screen showing forty rows lets you find the outlier by scanning; a
screen showing ten makes you page.

Target **32–36px**:

- one line per row, secondary identifiers moved into a hover card or an expand — the
  `@media (max-width: 640px)` rules already prove the row survives losing them;
- replace `.section { margin-bottom: 28px }` with hairline-separated regions at
  16px, now that the hairline is visible;
- collapse the seven bar primitives (§1.3) into **one**, with one height, one radius
  convention and one colour rule.

### 5.7 Motion

The current sheet is already right here and the redesign must not undo it: five
motion declarations in 1,877 lines, and `@media (prefers-reduced-motion: reduce)`
neutralises all of them with `!important`, so ordering cannot defeat it.

Keep exactly three permitted animations: the live-state pulse (and only while the
thing is genuinely live), a meter's width transit, and the skeleton — at the
original 0.5→0.85 amplitude, not the 1→0.35 the collision produced.

Add one, from Temporal: **in-flight edges and bars are dashed and the dashes
animate**; retrying is dashed red, pending is dashed purple. It is the cheapest way
to make a static screenshot distinguishable from a live one, and it costs one
`stroke-dashoffset` animation.

### 5.8 The chart layer

Recommendation: **visx + `@dagrejs/dagre`**, measured by Lane 3 at **+44 KB gzip** on
this repo's Vite 5.4 / React 18.3 toolchain, against Recharts at +109 KB and ECharts
at +194 KB tree-shaken. *(I did not re-measure these bundle sizes.)*

The reason is not size. SVG keeps chart text as real text — selectable, searchable,
accessible — and visx gives scales and axes **without a chart component that decides
for you what "absent" looks like**. That is the one decision this UI cannot delegate:
every row of §4's table has a "must not imply" column, and a component that renders
`null` as a zero-height bar violates rule 2 of the honesty section before you have
written a line.

---

## 6. Missing server-side seams, ranked by cost

Ranked cheapest first. Cost is engineering effort, not importance.

### F0 — the attempt decoder drops five fields · ~5 lines · **do this first**

Not a seam; a bug (§1.0). Add the five fields to `attempt_from_dict`
(`codec.py:211-229`) and add a test asserting they are **present** when the document
carries them — the existing test only asserts they are null when it does not.

Unblocks viz #16 and Panel 7 entirely. Also delete the now-false comment at
`codec.py:330-332` blaming the worker image, and the `AgentDetail.tsx` omission that
was justified by it. **Track A's file — this is a request, not a change.**

### S1 — page the events route · small · unblocks every timeline

`store.list_events` (`store.py:529-538`) is `.order_by("at", ASCENDING).limit(limit)`
with `paged_limit` capping at `max_page_size = 200` and **no page token**, while the
tasks list route already has `page_token` / `next_page_token` (`tasks.py:79,98,102`)
— so the pattern exists in the same file.

Two options, and they are not exclusive:

- add `page_token` / `next_page_token` to `/v1/tasks/{id}/events`, matching the tasks
  route;
- add `order=desc`, so a caller who wants the *end* of a long run can get it in one
  page.

Without this, §4 rows #3, #4 and #6 are built on a page that silently drops the tail,
and a long agent renders as one that never finished. `types.ts:894-900` already
documents the consequence, which means the UI has been working around it rather than
it being fixed.

### S2 — serve the live log tail · medium · one of the two explicit asks

`GET /v1/tasks/{id}/attempts/{attempt_id}/logs/{stream}` returning the object at
`{log_prefix}/live/{stream}.tail.log`.

Most of the work is done: the writer exists and is careful (`lifecycle.py:1552-1601`
— bounded, scrubbed on every flush, `#swarm-tail offset= size=` header), the IAM
exists (`bindings.tf:228-233`, `roles/storage.objectViewer`), and a reader already
exists outside the browser (`swarm_mcp/cli.py:62-69`).

What it needs:

- `google-cloud-storage` added to `apps/swarm-api/pyproject.toml` — it is not there;
- a tenant check in application code, because the IAM grant is **unconditioned**. The
  pattern this repository already uses for exactly that is
  `checkpoint.CheckpointManager._owns` (`checkpoint.py:244-257`);
- the response to pass the `#swarm-tail` header through, so the viewer can render a
  **gap** rather than stitching non-adjacent output;
- a size cap, since the object is already bounded by `live_log_tail_bytes`.

### S3 — serve artifact and checkpoint bytes · medium · same machinery as S2

`GET /v1/tasks/{id}/artifacts/{name}/content` and the checkpoint equivalent.

Identical shape to S2 with a different prefix, and the same unconditioned IAM grant
already in place — with a Terraform comment (`bindings.tf:225-227`) asserting a
behaviour the API does not have: "The API serves result artifacts back to callers."
The seam is built at both ends with nothing in the middle, exactly as the
reconciler's checkpoint-retention grant was before that module landed.

Needs additionally: a content-type allowlist and an inline-vs-download decision, and
a decision on whether a checkpoint gets a *listing* (a manifest read out of GCS) or
only whole-object download — `types.ts:902-903` records that contents are recorded
nowhere, so a listing means reading the tarball.

Unblocks the "way to inspect the outputs" ask, which is currently impossible from a
browser at four independent layers (bucket policy, load balancer routing, no signed
URLs, no GCS client).

### S4 — cross-task attempt aggregation · medium · unblocks "what did it cost"

There is no route that sums cost across tasks. `Store.list_attempts` already accepts
`task_id=None` and the `attempts-tenant-created` index already exists
(`store.py:770-794`) — the only route that calls it passes a task id.

So "spend this week" today means one `/v1/tasks` page plus one
`/v1/tasks/{id}/attempts` call per row. **That N+1 will be written as a loop in the
browser**, and it will be written by whoever builds the History screen, so the route
is cheaper than the alternative.

Should also decide whether `models`, `num_turns` and `thinking_tokens` become
queryable. `control.py:501-504` deliberately leaves them in the untyped
`result_summary["runner"]["usage"]` dict; that is defensible per-agent and
indefensible per-fleet.

### S5 — a real resource time series · large · the only way to get a utilisation chart

Today only high-water marks are retained: `ResourceUsage.samples` is a count and the
samples are discarded (`metrics.py:40-52`), and Cloud Monitoring gets three GAUGE
points written once at teardown. swarm-api holds no `roles/monitoring.viewer`, so
that path is closed regardless.

The `HEARTBEAT` event series (`{elapsed_seconds, peak_rss_bytes, checkpoints}`, one
per 150s) is the *only* time series the platform has, and it is a monotonic peak, not
usage. Row #4 of §4 is honest about that by labelling it "peak reached by T+n"; a
genuine utilisation curve needs sample retention.

**CPU is a separate and larger ask:** there is no CPU sampler at all, so
requested-vs-used has no CPU row and cannot have one without worker changes.

### S6 — account ↔ attempt attribution · needs a frozen-contract change · **request, not proposal**

Nothing assigns an account to an attempt. `Lease` and `Attempt` carry no account
field, so "which agent burned this account's quota" is unanswerable, and no amount of
UI work changes that.

Adding a field to `Attempt` means editing `apps/common/swarm_common/models.py`, which
is **frozen**. Per CLAUDE.md this is raised here as a request. It belongs in
`docs/contract-change-requests.md` if the owner wants it pursued.

---

## 7. What I am not proposing, and why

1. **Not renaming the six sections.** `App.tsx:32-60` argues them, and the owner
   amended them on 2026-09-21. None of the measurable causes of "hard to read" is the
   section list. Re-litigating a decided question is not a redesign.
2. **Not a rail grouped by object kind.** Lens's tree is the Kubernetes *schema* and
   the direct translation here would be Tasks / Attempts / Leases / Events /
   Artifacts / Checkpoints — the Firestore collections. Nobody opens this product
   asking "show me leases." `App.tsx`'s `question` test already rejects it.
3. **Not a line chart of cluster utilisation over time.** There is no time-series
   store. `/v1/metrics` is an instantaneous Prometheus exposition rendered by
   `ctx.metrics.render()` (`routes/health.py:55-61`) and admin-gated. Sampling in the
   browser and drawing a line would manufacture history the platform never recorded —
   the exact failure class `fetch.ts` exists to prevent. This is what Grafana would
   tempt you into and it is the wrong import.
4. **Not an Alerts, Trouble or Incidents section.** No alerting engine, no incident
   model. A section named for machinery that does not exist is a promise the product
   cannot keep. `App.tsx:59-60` already decided this and it is still right.
5. **Not a quality score, eval or LLM-judge column.** Braintrust and LangSmith both
   offer one; this platform has no eval model, so a score would be invented. Taking
   the *diff between two attempts of the same task* from Braintrust is worth doing;
   taking its scoring is not.
6. **Not a fourth restatement of the frozen contract.** `types.ts` already
   hand-copies values from `swarm_common` and `check-contract-parity.sh` does not
   cover TypeScript. Nothing here adds a fifth place for those values to drift; the
   chart layer reads the same `types.ts` the screens do.
7. **Not a heavyweight chart framework.** Recharts (+109 KB) and ECharts (+194 KB)
   both ship components that decide what `null` looks like. §4 has sixteen rows with
   a "must not imply" column; that decision cannot be delegated.
8. **Not live streaming.** No WebSocket, no SSE. The platform is poll-shaped —
   heartbeats every 30s, a log tail flushed on an interval, `result_summary` written
   once at terminal state. A streaming transport would imply a freshness the data
   does not have.
9. **Not softening the honesty rules into glyph-only.** The sentences move one layer
   back; they do not disappear. A glyph with no reachable explanation is how
   "cancelled" and "not measured" ended up the same colour in the first place.
10. **Not editing another track's files.** Everything in §6 and §1.0 is raised as a
    request. `apps/swarm-api/` and `apps/swarm-ui/` are Tracks A and B;
    `apps/common/swarm_common/` is frozen.
11. **Not dropping the 200-row clamp language.** It is tempting to hide "showing the
    first 200" behind a nicer paginator. The clamp is real and a list that does not
    say it is a window is rule 5 broken.

---

## 8. Open questions for the owner

Ordered by how much the answer changes the work.

### Q1 — How much of the shell changes at once?

**What depends on it:** everything in §2.2, and whether this is one large change or a
sequence of small ones.

- **(a) Full shell replacement** — rail + full-bleed work area + inspector + dock,
  landed together. Every screen gets re-laid out once. Biggest change, shortest total
  time, one period where the product is in flux.
- **(b) Shell first, screens incrementally** — land the rail, full-bleed and dock as
  a frame that renders the *existing* screens unchanged inside it, then convert
  screens one at a time. Slower, reversible at every step, and the 1100px column dies
  on day one, which is the single biggest legibility win.
- **(c) Screens first, shell later** — fix type, contrast, colour and density inside
  the current column, then change the shell. Gets the most readable result soonest for
  the least risk, but the tables keep scrolling horizontally until the shell moves.

*My read:* **(b)**. The column is the structural problem and removing it is cheap;
converting 23 screens is not, and does not have to happen in one go.

### Q2 — Does the bug-fix batch ship before the redesign, or as part of it?

**What depends on it:** whether cost, pool saturation and three "why is my agent
stuck" banners work in the *current* UI, or only in the new one.

F0 (the decoder, ~5 lines) plus the four CSS collisions (§1.1) are small, independent
and make the deployed UI measurably less wrong today. They are also in Track A and
Track B files, so they are requests either way.

- **(a) Hotfix now, redesign after** — the pool meter renders, the banners stop
  clipping, cost stops being invisible, and the redesign starts from a truthful
  baseline.
- **(b) Fold into the redesign** — one change, one review.

*My read:* **(a)**. The `.bar` collision is currently blanking the one meter on the
Pools screen and clipping two of three lines of a cancellation notice. That is worth
fixing whether or not the redesign ever lands.

### Q3 — Which missing seam gets built first: logs, or output contents?

**What depends on it:** which of the two explicit asks the redesign can actually
deliver in its first version. They are the same machinery (S2 and S3), so the order
is a priority call, not an engineering one.

- **(a) Logs (S2)** — "what is it doing right now". The writer is already careful and
  a reader already exists in the MCP CLI; the browser is the only thing that cannot
  see them.
- **(b) Output contents (S3)** — "what did it produce". Currently impossible from a
  browser at four layers, and it is the ask phrased most concretely in the brief ("a
  way to inspect the outputs").
- **(c) Both, since the route is nearly identical** — one GCS client, one `_owns`
  check, two prefixes.

*My read:* **(c)**, sequenced logs-first. The second route is mostly a copy of the
first, and splitting them means writing the tenant-boundary code twice.

### Q4 — Chart library, or hand-rolled SVG?

**What depends on it:** ~44 KB of bundle, and how much control the "must not imply"
column in §4 actually has.

- **(a) visx + dagre (+44 KB)** — scales, axes and layout provided; every mark still
  drawn by us, so `null` never renders as zero by default.
- **(b) Hand-rolled SVG, zero dependencies** — keeps `package.json` at two entries,
  which is currently a real property of this repo. Costs writing scales, axes, ticks
  and a DAG layout, and dagre in particular is not worth reimplementing.
- **(c) Recharts (+109 KB)** — fastest to a chart, and the one most likely to render
  an absence as a zero somewhere nobody notices.

*My read:* **(a)**, and I would take **(b)** for everything except the DAG if the
dependency count matters more than the calendar. Worth saying: (c) is genuinely
tenable if the priority is seeing charts this week, and the honesty rules can be
enforced by review rather than by the library.

### Q5 — How far does the sentence move back?

**What depends on it:** row density, and how much of the current prose stays on
screen. This is the taste question underneath "very hard to read."

- **(a) Aggressive** — 32px rows, glyph-only in the data region, every sentence in a
  hover card or the inspector. Forty rows on screen. Reads like an instrument.
  Risk: on a touch device or a screenshot, the sentence is gone.
- **(b) Moderate** — 36px rows, glyph plus a short label in the data region, the full
  sentence one gesture away. About thirty rows.
- **(c) Conservative** — keep the sentence inline, fix only the measure (68ch), type
  scale and contrast. Roughly fifteen rows, but nothing is ever hidden.

*My read:* **(b)**. (a) is what Lens does and it is the reason people keep a terminal
open beside it.

### Q6 — Does light mode survive?

**What depends on it:** roughly half the colour work in §5.5. Every state colour
needs retuning twice, and light mode is currently the failing case for most of the
chip contrast (`--warn` `#9a6700` at ~4.37:1 is the worst, and it carries the
blocked-reason line).

- **(a) Keep both** — correct, and doubles the tuning.
- **(b) Dark only** — `styles.css:1-3` already says "Dark-first, because this is an
  operations screen that gets opened at 3am." Halves the work and makes the six-state
  palette much easier to separate in greyscale.
- **(c) Keep both, but fix light mode in a later pass** — ship dark correct and light
  no worse than today.

*My read:* **(c)**. Dropping light mode outright is a product decision I should not
make, and (c) costs nothing now.

---

## What I did not verify

Stated plainly, because an unverified claim here becomes a runbook step someone
follows at 3am.

- **Lane 3's headless-Chrome pixel measurements.** I confirmed the four duplicate
  selectors by reading, confirmed `* { box-sizing: border-box }`, confirmed the four
  `.bar` call sites, and derived the 20px border box / 0px content box from the box
  model. I did not re-run the browser, so the exact 37px and 56px scrollHeights are
  Lane 3's.
- **The 156–165 characters-per-line figures.** I verified that no rule in
  `styles.css` constrains a prose measure and that `.app` is 1100px wide. The
  character counts are Lane 3's measurement.
- **The bundle sizes** for visx, dagre, Recharts and ECharts. Lane 3's, measured on
  this toolchain; I did not build.
- **Most of the chip-level contrast table.** I recomputed `--line` (1.28:1 / 1.18:1)
  and the ok/warn/info greyscale luminances (0.3633 / 0.3660 / 0.3657) myself. The
  per-chip AA failures are Lane 3's.
- **All external prior art** — Lens's dock and sidebar, Temporal's three history
  views, Weave's scrubbers and view modes, LangSmith's three-way token split,
  Langfuse, Braintrust, Phoenix. These are Lane 1's readings of third-party
  documentation. I did not fetch any of it. Lane 1 explicitly flagged that it could
  **not** verify OpenAI's or Anthropic's own trace viewers and declined to describe
  them; nothing in this document rests on those.
- **Anything against a deployed environment.** This is a static analysis of the
  repository at `f0154b4`. No `make smoke`, no running cluster, no live API.
- **The exact event count for a two-hour run** (~175). That is arithmetic from
  `HEARTBEAT_EVENT_EVERY = 5`, `heartbeat_interval_seconds = 30` and
  `checkpoint_interval_seconds = 120`, all of which I read. It is not an observation
  of a real run.

## Track conflicts found

Reported, not patched, per CLAUDE.md.

1. **`apps/swarm-ui/src/api.ts:103-107`** describes `GET /v1/tasks/{id}/artifacts` as
   a dead route reading an unwritten subcollection. It was repaired;
   `store.list_artifacts` now reads `task.result_summary` and its docstring cites
   this comment by name. Nothing is broken; the next reader will be misled.
2. **`apps/swarm-api/swarm_api/codec.py:330-332`** attributes null cost fields to a
   worker image that has not shipped. The worker ships them correctly; the decoder
   twenty lines above drops them.
3. **`terraform/modules/iam/bindings.tf:225-227`** states "The API serves result
   artifacts back to callers, so it reads objects." The API has no route that serves
   an artifact and no GCS client dependency. The grant is correct for the intended
   design and currently unused.

---

## 9. OWNER DIRECTIVE, 2026-09-22 — prose out of the app

Stated directly by the owner:

> we should try to remove all of the prose content from the app. Help should
> sit in a dedicated help section but we could put everywhere we need to a
> helper hover over question mark tooltip and house info there too

This supersedes any part of this document, or of `redesign.md`, that puts
explanatory sentences on a data surface. Three places to apply it:

1. **A dedicated Help section**, carrying the long-form material that is
   currently inlined: what a pool is, why capacity is the minimum across pools,
   what each state means, why a paused pool is not a full one, what a
   checkpoint is for.
2. **A `?` affordance beside anything that needs a why**, hovering to a card.
   The card is where the sentence lives now.
3. **Data surfaces carry data**, not paragraphs.

### The part that must NOT be lost, and why it is subtle

The current UI's best property is implemented AS prose, so a naive removal
deletes it. `redesign.md` §3 specifies that an absent metric renders "the value
is a **sentence**, not a figure", and `PlatformCountsScreen` writes things like
"2 states did not come back (LEASED, RUNNING). No total is shown, because a sum
over a partial response would look like a complete one."

The distinction to hold:

* **The FACT stays on the surface, always, and stays impossible to miss.** That
  a read failed, that a response was partial, that a figure is unmeasured, that
  a total is being withheld — each keeps a visible marker: an em dash, a
  hatched segment, a count of what is missing, a struck-through total.
* **The EXPLANATION moves into the `?` card.** "Why is this an em dash" is a
  hover. "Why is there no total" is a hover.

A silent icon where a sentence used to be is NOT this directive satisfied; it
is the honesty rule deleted. The test for any panel after the change: can a
reader tell, WITHOUT hovering anything, that a number is missing rather than
zero? If not, the marker is too quiet.

### Measured example this directive should fix

From the first real `claude-code` run, `task_b208fc8542724268b5f4`:

```
03:46:05  dispatched
03:49:14  starting      +3m 09s
03:49:14  running       +0.1s
03:49:32  succeeded     +18s
```

3m09s of Cloud Run cold start, 18s of agent. `DISPATCHED` is where nearly all
the wall clock goes and it is the state a poller almost always sees — and no
screen distinguishes "waiting for a container" from "the agent is working".

That is not fixed by prose OR by a tooltip. It needs a duration breakdown
derived from the event stream (`queued → leased → dispatched → starting →
running`), drawn as a segmented bar on the agent row. The fact is the bar; the
`?` explains what each segment means. It is the clearest example in this
document of data replacing sentences rather than being explained by them.
