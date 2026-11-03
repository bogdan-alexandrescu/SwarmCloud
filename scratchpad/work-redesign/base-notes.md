# Work redesign: the foundation scaffold (`?wa=base`)

Mock-ups only. Nothing here is committed: `work-redesign-base.patch` applies with
`git apply work-redesign-base.patch` from the repository root (checked with `git apply --check` on a
clean checkout of 57392c6). Every added line is marked `// MOCK-HOOK` (CSS `/* MOCK-HOOK */`), and every
new file starts with a `// MOCK-HOOK FILE` line.

## Run it

```
cd apps/swarm-ui && npm ci && ./node_modules/.bin/vite --port 5173      # the dev server is the fixture build
open http://localhost:5173/agents?wa=base&ws=workflow&wst=rest
```
Stop the dev server before vitest or tsc (5 GiB container). `render.py` in the artifacts is the
brief's snapshot kit adapted to the task's live browser (`swarm_browser.live()`, one reused tab, fonts
fetched with urllib because the gate refuses `Storage.getCookies`).

## URL switches (read once at load, `src/workredesign/switches.ts`)

| switch | values | meaning |
|---|---|---|
| `?wa=` | `base`, `a` … `f` | direction. Absent: today's console, untouched. A letter with no screens registered draws `base` and says so. |
| `?ws=` | `agents` (default), `agent`, `workflows`, `workflow` | the screen |
| `?wst=` | `rest` (default), `open`, `watch`, `needs`, `logs` | the state the screen opens in |
| `?wid=` | an agent id or workflow id | optional: which agent (`ws=agent`) or workflow (`ws=workflow`). Defaults: `task_dpb_build_contacts`, `wf_design_picks_build` |
| `?wfx=off` | | serve the console's ordinary fixtures instead of this dataset |
| theme | console's Light / Dark / System switch (Profile, sidebar foot); frames also take `?theme=light|dark` | |

Use the path `/agents` for every mock-up address so the spine lights Work (`workMockHref(wa, ws, wst, wid)`
builds one). App renders `<WorkRedesign>` in place of the Work section's body when `?wa` is set
(App.tsx, one MOCK-HOOK line in `SectionBody`); the rest of the shell (spine, sidebar, reads footer) is the
real one. Moving between screens inside the mock-up is local state (`nav({screen, id})`), so one click
moves between a workflow, a step, an agent and its browser and the URL switch stays put.

What each `wst` does in base: `open` = agents: the hard case's agent open beside the list; workflow: step
`build-invoices` (the failed one) selected. `watch` = the browser wall opens `build-contacts` large;
agent: the Browser tab. `needs` = the needs-you strip expanded (on `ws=workflow` it is expanded at rest
too). `logs` = agent: the Logs tab.

## Adding a direction

1. Copy `src/workredesign/base.tsx` to `src/workredesign/<letter>.tsx`; export four screens taking
   `BaseProps` (`data, at, nav, wst, go, list, onList`).
2. Register them in `DIRECTIONS` in `WorkRedesign.tsx`: `a: { agents: AAgents, agent: AAgent, workflows: …, workflow: … },`.
3. Its CSS in its own file, classes prefixed (`.wa-a-…`), imported from its screens; tokens only from the
   console (`--text`, `--text-dim`, `--line`, `--surface`, `--surface-2`, `--s-live`, `--s-park`,
   `--s-bad`, `--info`, `--t-meta` 13px, `--t-body` 14px, `--ctl-s1..s5`, `--radius`, `--font`, `--mono`).

## Files

| file | what |
|---|---|
| `src/workredesign/fixtures.ts` | the dataset; `mockWorkOn()`; the browser stills (`mockStillUrl`) |
| `src/workredesign/switches.ts` | `WORK_SWITCHES {wa, ws, wst, wid}`, `workMockHref()` |
| `src/workredesign/parts.tsx` | shared building blocks (below) |
| `src/workredesign/base.tsx` | the four base screens: `BaseAgents`, `BaseAgent`, `BaseWorkflows`, `BaseWorkflow`; `DEFAULT_AGENT`; `InlineAgent` |
| `src/workredesign/WorkRedesign.tsx` | the entry; `DIRECTIONS` registry |
| `src/workredesign/workredesign.css` | the scaffold's plain layout (`.wr-*`), plus the still inside `.live-screen` and the agent drawer drawn inline |
| hooks: `api.ts` | `fixtureTaskRows()` and `fixtureWorkflowRows()` answer `mockTaskRows()` / `mockWorkflowRows()`; `loadLiveSession` opens a session for every RUNNING mock browser agent with its `mockDevice()` |
| hooks: `LiveBrowser.tsx` | `LiveScreen` draws `<img class="mock-still">` inside `.live-screen` (where noVNC's canvas goes) and suppresses the "No picture" empty state for it |
| hooks: `LiveThumb.tsx` | the workflow card's thumbnail canvas gets the still drawn into it and reads "live" |
| hooks: `App.tsx` | `SectionBody` renders `<WorkRedesign>` for the Work section when `?wa` is set |

## Building blocks (`parts.tsx`)

| export | props | what |
|---|---|---|
| `useWorkData()` | – | `{status:'ok', tasks, taskById, workflows}` via `loadTasks(500,{view:'summary'})` + `loadWorkflowBoard()` |
| `splitCounts(tasks)` | | `{live, waiting, recent, browsers}` with the console's state sets (live = LEASED/DISPATCHED/STARTING/RUNNING) |
| `needsYou(tasks, workflowId?)` → `NeedItem[]` | | failed workflow steps, owner questions (`ownerQuestionsHold` + AWAITING_INPUT parks), merges waiting, hand-offs (`handoffOf`). Approvals: not in this dataset; the strip says "not read here" rather than 0 |
| `NeedsYou` | `items, open, nav` | the strip: a chip per kind with its count; `open` lists one line per item, each a link to its agent and workflow |
| `BrowserStill` | `task, nav, size: 'thumb'|'large'` | the still image marked with the console's `LiveChip` ("live"); click opens the agent |
| `BrowserWall` | `tasks, nav, large?` | every RUNNING browser agent's still in a grid, "Watch large" per cell |
| `stagesOf(workflow)` | | steps grouped by dependency depth (the hard case: 4 stages of 10) |
| `StageGrid` | `workflow, taskById, nav, selected?, onSelect?` | one row per stage, one cell per step: name, state mark, PR number, `?` for an open owner question; failed cell red |
| `workflowSpend(w, taskById)` | | the hard case: $13.52; else the steps' summed cost, "so far" when partial, "not known" when none |
| `ageWords(iso)` | | "4 min", "2 h", "3 d" against the mock clock; "not known" for null |
| `Region` | `title, note?, children` | a plain labelled region |
| `useNeeds(data, workflowId?)` | | memoised `needsYou` |
| `NEED_WORD` | | the plain words for each need kind |

Console components the base screens use as they are: `AgentsScreen` (Agents.tsx; the whole list),
`AgentSplit` (AgentSplit.tsx; one agent with every pane, drawn in the page by `.wr-inline-agent`),
`WorkflowsScreen` (Workflows.tsx; list or, with `view="wf=<id>"`, one workflow's page with Graph / Table /
Steps over time / Changes), `Breadcrumb`, `Chip`, `Count`, `LiveChip`, `StatePill` (components/).
`components-reference.md` lists every component and helper of the 18 files with its props and
the api.ts reads it makes (from the inventory pass).

## Fixture keys (`fixtures.ts`)

| key | value |
|---|---|
| `HARD_WF` / `HARD_WF_TITLE` | `wf_design_picks_build` / `design-picks-build` |
| `HARD_STAGES` | `build`, `review`, `fix`, `re-review` |
| `HARD_FEATURES` | login, search, calendar, invoices, contacts, reports, settings, booking, export, onboarding |
| step id / task id | `hardStepId(stage, feature)` = `build-login`; `hardTaskId` = `task_dpb_build_login` (`re-review` → `rereview`) |
| `HARD_MERGE_TASK` | `task_dpb_merge`: profile `merge`, PARKED HUMAN_REQUIRED, `park_phase: owner_questions`, holding on the 2 questions |
| `HARD_SPENT_USD` | 13.52 |
| `PEOPLE` | bogdan@, ana.moreau@, kenji.sato@, lea.fontaine@, ravi.iyer@ (all example.com). The console's signed-in fixture identity stays alice@example.com in workspace u-bogdan |
| repositories | example-org/northwind-crm, example-org/roombook |
| `MOCK_NOW` | the page's clock at load, to the minute |
| `mockTaskRows()` / `mockWorkflowRows()` | everything below |
| `mockLiveTask(id)`, `mockDevice(id)`, `mockStillUrl(id, mobile?)`, `stillKind(id)` | the browser side |

The hard case, step by step (stage × feature):

| | login | search | calendar | invoices | contacts … onboarding (6) |
|---|---|---|---|---|---|
| build | succeeded #2400 | succeeded #2401, asks a question | succeeded #2402, asks a question | **failed**, red check "e2e / chromium", #2403 | running (browser) #2404–#2409 |
| review | succeeded #2410 | running #2411 | running #2412 | running #2413 (reviews the red check) | waiting on build |
| fix | running #2420 | waiting | waiting | waiting | waiting |
| re-review | waiting | waiting | waiting | waiting | waiting |

Counts: 10 running browser agents, 25 waiting on earlier steps, 4 succeeded + 1 failed = 5 finished, 2 owner
questions (asked by build-search and build-calendar, holding the merge task: the "merge waiting").
**The brief's figures (10 + 25 + 5 + 1) add to 41 for 40 steps**; this reading keeps 40 steps and counts
the failed one among the 5 finished. Workflow policy: `on_step_failure: CONTINUE` ("if a step fails: keep
going"). Every started step has a pull request in `result_summary.git.pull_request`.

Agents list: Live 34 (18 browsers: 17 RUNNING with a still + 1 STARTING), Waiting 86 (the hard case's 25,
its merge task, 30 small-workflow steps, 30 others across queue, capacity, quota, CI, retry, question),
Recent 202. One live browser agent (`task_daa7d85f`) asks for a person: a hand-off, "your turn".
Workflows: 40 (the hard case + 39 of three steps: 10 running, 10 parked on CI, 10 failed, 9 done).

Browser stills: an SVG per agent (login, table, dashboard, calendar, kanban, form), chosen from its
feature for the hard case, drawn 1440×900 (or 393×852 on a phone preset) with a host bar
(staging.northwind-crm.example.com / app.roombook.example.com).

Honesty notes: cost on a running agent is `cost_incomplete`; the workflow board's own Cost column reads
attempts and shows "$0.973, 5 of 15 reported" for the hard case while the base header shows the brief's
$13.52: the two figures come from different reads and a direction should show which it is. Approvals are
shown as not read, never as 0.

## Where each features.md section lives in `wa=base`

| features.md section | base screen and region |
|---|---|
| Agents.tsx (list, rows, tabs, search, filters, sort, counts) | `ws=agents` › region "Agents" (the console's `AgentsScreen`); counts repeated in the chips row |
| agentlist.ts (list address) | `ws=agents` › "Agents" (its tab/state addresses via `list`/`onList`) |
| AgentSplit.tsx (head, tabs, copy link, Take control, Stop, close) | `ws=agent` (inline `AgentSplit`), and `ws=agents&wst=open` beside the list |
| App.tsx routes | unchanged; the mock-up's own moves are `nav()`; App's routes still work for every other address |
| AgentDetail.tsx (questions, held questions, attempts, budget, PR, dispatch, artifacts, checkpoints …) | `ws=agent` › Details tab (and its other tabs) |
| AgentLogs.tsx | `ws=agent&wst=logs` |
| AgentBudget.tsx | `ws=agent` › Details |
| AgentChildren.tsx | `ws=agent` › Children (under More) |
| Handoff.tsx | `ws=agent` banner; `NeedsYou` "Your turn in a browser"; `BrowserStill` "your turn" chip |
| StartupProgress.tsx, Liveness.tsx | `ws=agent` › Details (and the list rows) |
| LiveBrowser.tsx, LiveToolMenu.tsx, BrowserBadges.tsx, Theater.tsx | `ws=agent` › Browser tab (`wst=watch`); the still drawn in `.live-screen`; Theater from its button |
| LiveThumb.tsx | `ws=workflow` › region "Workflow" (stage cards' thumbnails); `BrowserWall` / `BrowserStill` |
| WorkflowsScreen routing, list, filters, sort, rows, workflowlist.ts | `ws=workflows` › region "Workflows" |
| Board & WorkflowCard, step inspector, graph, header, policy, stop, copy | `ws=workflow` › region "Workflow"; plus `StageGrid` and the selected-step region |
| WorkflowViews.tsx (Table), TimelineLanes.tsx (Steps over time), WorkflowChanges.tsx (Changes) | `ws=workflow` › region "Workflow" › its tabs |
| reads footer | the shell's own footer on every screen |

## Frames (`frames/`, 1440×900, rest state)

`base-agents`, `base-agent`, `base-workflows`, `base-workflow` (`/agents?wa=base&ws=<screen>&wst=rest`) and
`today-agents` (`/agents`), `today-agent` (`/agents/live/task_dpb_build_contacts`, which the console opens on
the Browser tab), `today-workflows` (`/workflows`), `today-workflow` (`/workflows/wf_design_picks_build`).
Each has a `.png` and a self-contained `.html` (fonts in `assets/`, 15 files).

Two changes landed after the frames were taken, and the live browser crashed before they could be
re-rendered (the gate answered `gate_unavailable: crashed … carry on without it`):
1. `MOCK_NOW` moved from the fixture clock (2026-10-19 12:00Z) to the page's clock. So the frames show
   ages such as "13d 16h", where the patch now shows minutes.
2. `.wr-inline-agent` now draws the agent drawer inside the page. In `base-agent.png` it is still the
   console's fixed drawer on the right, overlapping the large still.
The patch type-checks; these two were not seen rendered.

## Appendix: components and reads of the 18 files

See `components-reference.md` (42 KB; from the inventory pass, per file).
