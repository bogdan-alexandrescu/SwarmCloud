# Direction D — Graph with a docked inspector

**The idea.** An evolution of today's console, not a new one. Every Work screen becomes two panes: on the left a
dense, one-line-per-row surface (the Agents table, the Workflows table, or the workflow's stage grid), on the right a
resizable **inspector** (drag handle; folds to a rail; a sheet on the phone). The workflow graph is redrawn as a
grid, a row per stage and a column per feature, each step a chip with its state icon, its time and its pull request,
so 40 steps read as 4 × 10 squares with nothing overlapping. Whatever you click — a row, a chip, a still — opens in
the inspector, and the left side never moves, so context is kept. The inspector of one agent is the console's own
agent view (Details, Browser, Logs, Changes, Attempts, Artifacts, Checkpoints, Children), unchanged in behaviour.
A **Needs you** bar with counted chips (Failed, Question, Merge waiting, Your turn in a browser, Approvals) sits on
every screen; clicking it puts the needs list in the inspector and dims everything else in the grid.

## + What it gets right
+ What needs Bogdan is one glance on every screen: the Needs-you bar with counts, red failed chips, purple "?"
  marks on the grid cells that ask a question, the Merge strip under the grid with "Answer 2 questions" (D·4, D·7).
+ A 40-step workflow fits one screen at 1440 and 1512 with the overview beside it (D·4, D·8); each stage row
  carries its own tally (✓3 ✗1 ▶6).
+ Running browsers are first-class: a wall of live stills in the Agents inspector when nothing is picked (D·1),
  ten stills under the grid (D·4), the same ten in the inspector with one large (D·6), Theater one click further.
+ One click between workflow → step → agent → browser, the grid kept in view; ↑/↓ in the step inspector walks
  stages ("↓ review") without going back (D·5).
+ Far less prose: rows are state pill + name + numbers; reasons and policy sit behind a details toggle or tooltip
  (Policy, Workflow id and reads, wait reasons as tooltips).
+ Lowest risk to build: reuses AgentSplit, WorkflowsScreen views and every console component; nothing shared changed.

## ! What it costs Bogdan
! Two panes split the width: at 1280 the inspector starts **folded** at rest so the chips keep their time and PR
  number; "Workflow details" brings it back (D·8b). With the inspector open at 1440 the chips drop the PR number
  (D·4) and feature names truncate ("calend…", "conta…"); the full name is in the tooltip and the step inspector.
! With the inspector wide (watching, D·5/D·6), the grid narrows to state marks only and its column labels turn
  vertical; times move into the tooltip.
! In the Agents table beside an open agent, only state and name remain (D·2); owner, attempts, cost and PR come
  back when the inspector closes or is dragged narrower.
! On the phone the grid is marks only (D·9b), the Needs-you chips wrap to two lines (D·9), and the inspector is a
  full sheet — one thing at a time.
! Where the cost is visible: the left column's width — every frame with the inspector open shows columns or labels
  dropped by container queries; the clearest are D·2, D·6 and D·8b.

## Shared pieces
None changed by this direction. Direction D lives in `apps/swarm-ui/src/workredesign/D/` (DirectionD.tsx, d.css)
and one registration line in `WorkRedesign.tsx`. The scaffold's still-image hooks in LiveBrowser/LiveThumb are used
as they are. Note: the scaffold patch did not carry features.md; this step rebuilt it from the code (features.md
in the bundle, ids A/G/B/L/W/R below).

## Map: every features.md item → where it lives in D
| id | feature | where in D |
|---|---|---|
| A1 | Live/Waiting/Recent with counts | segmented control at the top of Agents (D·1), sidebar counts |
| A2 | search; a whole task id opens it | search box; a pasted whole task id opens that agent in the inspector |
| A3 | filter profile | "Any profile" select |
| A4 | browsers only | "Browsers only" toggle chip |
| A5 | filter owner | "Anyone" select |
| A6 | sort | sort select: newest, oldest, most spent; on Waiting also "grouped by why it waits" |
| A7 | state pill | first text column |
| A8 | name / workflow › step | Agent column ("design-picks-build › build-contacts") |
| A9 | running / waiting / ended span | "Running" / "Waiting on" / "Ended" column |
| A10 | attempts n/max | Attempt column (hidden when the inspector is wide) |
| A11 | cost, unknown as unknown | Cost column, "+" marks an incomplete sum, "—"/not known never 0 |
| A12 | live thumbnail | still in the first column of every browser row |
| A13 | waiting grouped by why | Waiting tab: "Waiting on" column with the reason words, tooltip with the pool/blocker sentence; sort "grouped by why it waits" |
| A14 | silent-worker line | red "silent" mark on the row, the full line as its tooltip |
| A15 | failed rows marked | failed state pill; Failed chip in Needs you |
| A16 | cancelled note | "· cancelled by request" after the name |
| A17 | paging | "n of m shown · show 60 more" at the table's foot |
| A18 | polling cadence | unchanged data layer (the console's own hooks) |
| A19 | your turn in a browser | "your turn" chip on the row; "Your turn in a browser" chip in Needs you; first in the browser wall |
| A20 | split / fold the list | the inspector is the split; drag handle resizes, × folds it to a rail |
| G1 | header | inspector header: state, workflow · step, elapsed, attempt n of max (D·2) |
| G2–G6 | copy link, copy id, take control, stop, close | inspector header buttons; copy id beside the task id (D·2) |
| G7 | meta line | inspector meta line: profile, browser used, class, vCPU, memory, workflow link, account, "A PR per step" |
| G8 | tabs with counts + More | inspector tabs (Details, Browser, Logs, Changes, Attempts, Artifacts, Checkpoints, More n) |
| G9 | details / why | Details tab |
| G10 | budget and cost | Details tab (AgentBudget); workflow cost in the workflow Overview |
| G11 | children | Children tab (under More when narrow) |
| G12 | hand-off | banner in the inspector and on the Browser tab; "Your turn" in Needs you |
| G13 | owner questions/answers | Needs-you list in the inspector with Answer (D·7); question chip on rows; "?" on grid cells |
| G14 | startup progress | Browser tab before the first picture; "+1 starting, no picture yet" in the browser wall (D·1) |
| G15 | liveness | inspector header (heartbeat age), "silent" mark on rows |
| G16 | logs | Logs tab (D·2b), and "◫ logs" beside the browser (D·2) |
| G17 | changes | Changes tab; workflow Changes view |
| G18 | attempts with history | Attempts tab with count |
| G19 | artifacts | Artifacts tab ("not yet" when none) |
| G20 | checkpoints | Checkpoints tab with count |
| G21 | retry / resubmit | inspector header of a finished agent (console's own control) |
| B1 | live picture, marked live | Browser tab, stills marked "live" everywhere |
| B2 | address bar + who drives | strip above the picture ("Nobody is in control: the agent is driving.") |
| B3 | device preset | "Device: iPad ▾" (D·2) |
| B4 | member control | "Let any member take control" checkbox |
| B5 | take / give back control | Take control in the header |
| B6 | theater | "Theater" button |
| B7 | logs beside | "◫ logs" |
| B8 | tool / Jev / Playwright badges | status strip of the Browser tab (console's LiveToolMenu) |
| B9 | reconnecting / failed | Browser tab states (console's own) |
| B10 | thumbnails | Agents table, browser wall, live stills under the grid, the watch wall |
| L1 | buckets with counts | All / Running / Parked / Failed / Done segmented (D·3) |
| L2–L5 | search, owner, profile, sort | tools row of Workflows: search, Anyone, Any profile, "Needs you first" / newest / most failed |
| L6 | row: state, name, steps, owner, changed | table row; step bar + "5/40"; owner and changed columns (owner hidden, changed in the inspector when it is open) |
| L7 | why / failed count | "Needs you" column: ✗ failed, ? questions, ⇢ merge |
| L8 | phone summary | phone rows: state + name + steps + needs |
| L9 | submit (empty) | console's empty state and the sidebar Submit |
| W1 | back keeping filters | breadcrumb "Workflows / design-picks-build" |
| W2 | header | state, name, 40 steps, $13.52, 52m old; tally line ▶10 ◷25 ✓4 ✗1, 10 browsers (D·4) |
| W3 | Graph/Table/Steps over time/Changes | segmented control under Needs you; the console's views embedded without their duplicate header |
| W4 | stages, state per node, stage counts | the grid: row per stage with ✓ ✗ ▶ ◷ counts (a stage count opens the Table view) |
| W5 | park reasons | grid cell tooltip and the step inspector; legend above the grid |
| W6 | step's agent beside | step inspector (D·5) with ↑/↓ to walk stages |
| W7 | table | Table view |
| W8 | steps over time | Steps over time view |
| W9 | changes | Changes view |
| W10 | dispatch, PR per step | "#2403" on each chip (1280 / folded), PR column in Agents, Overview "Pull requests 15 · 4 merged · 11 open" |
| W11 | merge held by questions | Merge strip under the grid with "Answer 2 questions"; Overview "Merge" row; Needs-you list (D·7) |
| W12 | policy | Overview: "On a failed step", and the "Policy" details toggle |
| W13 | stop workflow | Overview: "Stop workflow" (typed confirmation as today) |
| W14 | spend | header $13.52; Overview "Spent … $3.36 on earlier attempts"; Workflows "Spent" column |
| W15 | live browser agents | "Live browsers 10" under the grid; Browsers tab of the inspector (D·6) |
| W16 | rollup / drift | Overview "Workflow id and reads" toggle |
| W17 | reads footer | console footer, unchanged ("Reads 10 routes · p95 400 ms …") |
| R1–R3 | routes | unchanged; the mock-ups use ?wa=d&ws=&wst= on top of them |
