# Direction E — Attention inbox (?wa=e)

**The idea.** Every Work screen opens on what needs Bogdan, never on a list to browse. One inbox card sits on top of
Agents, Workflows and one workflow — your turn in a browser (with its deadline), owner questions, merges waiting,
failed steps, stalled agents, approvals — one line per item in triage order, each with its action inline: **Answer**
(the box opens in the row), **Retry**, **Approve merge**, **Take your turn**, **Open**. Counter chips on the card filter
it by kind. Everything else follows below, quieter: live browsers as a strip of stills, then agents or workflows in
one-line rows **grouped by workflow** (Agents) or **by state** (Workflows) with a counter per group, and on a workflow
page the 40 steps as 4 stage rows of 10 with a counter per stage. Screens whose job is something else (one agent,
watching browsers) fold the inbox to a single line that stays on top.

## + What it gets right
+ What needs Bogdan is the first thing on every screen, with its number in a heading and per-kind chips (WA E·1, E·3, E·4).
+ Most items are dealt with without leaving the row: answer, retry, approve, take a turn (WA E·7).
+ The merge says what holds it ("held by 2 questions", #2401 #2402) and stays disabled until the answers are in.
+ The 40-step hard case reads at a glance: 4 stage rows with ✓/✕/●/⏸ counters, every cell a step with its state and pull request (WA E·4, E·8).
+ A step opens **in place under its own stage row**, so the step panel can never again be drawn under the stage cards (WA E·5).
+ Browsers are first-class: a strip on every list screen, a wall of ten with one large on demand (WA E·6), and one agent's browser large with logs beside it and its siblings one click away (WA E·2).
+ Context is kept: the inbox bar and breadcrumb on the agent screen go back to the workflow in one click.
+ Prose is out of the rows: numbers, chips, icons with words; explanations are tooltips (policy, dispatch, spend, approvals).
+ Honest states: approvals are "not known" (not 0), stalled shows 0 because it is computed and none qualify, a running cost says "+"/"so far".

## ! What it costs Bogdan
! The inbox always comes first, so on a phone it fills the whole first screen and the browsers and lists start below the fold (WA E·10, E·11). On a quiet day it is a small "Nothing waits on you here" line.
! Agents with 20 items: the inbox shows 2 per kind and folds the rest ("5 more question", "9 more failed"), one click away — the full list of failures is not visible at once (WA E·1).
! The inbox row is a one-line summary; a long question is cut with … until **Answer** opens it (WA E·4 vs E·7).
! At 1280 the inbox drops the pull request numbers from its rows and long step names shorten (onboardi…); a question mark on a cell sits at the cell's edge (WA E·9).
! Graph, Table, Steps over time and Changes are the console's existing views behind the view switch: the stage rows are this direction's own view; picking another draws the console's workflow page, with its own header, below (not shown in a frame).
! On the phone the inbox shows kinds as icons only (the chips above it name them), and stage cells drop their names, leaving state marks (WA E·11).
! The agent screen's large still is about 600px wide at 1440 because logs sit beside it; Theater gives the whole width (WA E·2).

Where the cost is visible: the phone frames (E·10, E·11) and the folded "N more" lines in E·1 are the clearest.

## Shared pieces changed
None. Direction E adds `src/workredesign/E/` (index.tsx, inbox.tsx, e.css, every file a MOCK-HOOK FILE) and two
`// MOCK-HOOK` lines in `WorkRedesign.tsx` registering `e: DIRECTION_E`. It reuses the scaffold's `parts.tsx` helpers
(`useWorkData`, `splitCounts`, `stagesOf`, `workflowSpend`, `ageWords`) and fixtures, and the console's own
components (`Chip`, `Button`, `Segmented`, `Breadcrumb`, `StatePill`, `LiveChip`, `AgentSplit`, `WorkflowsScreen`,
`ownerQuestionsHold`, `handoffOf`, `timeLeft`, `presetLabel`).

## Note on features.md
The staged `work-redesign-base.patch` did not contain `features.md` or `base-notes.md` (it holds the scaffold's code
only). The map below is this step's own inventory of the Work section, written from the brief's list and the
console's components the frames embed; it should be checked against the foundation step's features.md when the
bundles are compared.

## Where every feature lives
| Feature | Where in direction E |
|---|---|
| Search (name or id) | Agents: "Find by name or id" in the Everything-else toolbar (E·1); Workflows: "Find a workflow" (E·3) |
| Filter: profile | Agents toolbar, profile select (E·1) |
| Filter: browsers only | Agents toolbar chip "Browsers only" (E·1) |
| Filter: owner | Agents and Workflows toolbars, owner select (E·1, E·3) |
| Sort | Agents: newest/oldest/cost; Workflows: last change/name/most steps (E·1, E·3) |
| Live / Waiting / Recent split with counts | Top line chips and the segmented switch above the groups (E·1) |
| Recent by state (succeeded/failed/cancelled) | Agents toolbar on Recent: "ended" select (succeeded, failed, cancelled); each row's state mark (E·1) |
| Copy id | "copy id" on every agent row (E·1), the step panel (E·5), the workflow bar (E·4); AgentSplit header copy (E·2) |
| Copy link | Workflow bar "copy link" (E·4); AgentSplit "Copy link" (E·2) |
| Take control | Inbox "Take your turn" (E·1); AgentSplit "Take control" (E·2) |
| Stop an agent | AgentSplit "Stop" (E·2); "Stop step" in the opened step (E·5) |
| Live browser | Large still with live mark (E·2); strip (E·1, E·4); wall (E·6); opened step (E·5) |
| Device preset | "device" select above the large browser (E·2) |
| Theater mode | "Theater" button (E·2): the browser takes the full width, the side panel hides |
| Member control | "Members may control" toggle chip (E·2) |
| Jev / Playwright badges | Beside the browser's address (E·2) |
| Watch several browsers at once / one large | Strip "Watch all N" → wall (E·6); click a picture → large with the others beside it |
| Hand-off (your turn in a browser) | Inbox kind "Your turn" with time left and "Take your turn" (E·1); "your turn" chip on the still |
| Logs | AgentSplit Logs tab beside the browser (E·2); "Logs" in a failed row's Why (E·7) and in the opened step (E·5) |
| Changes | AgentSplit tabs (More); "Changes" in a failed row (E·7) and the opened step (E·5); workflow view "Changes" (E·4) |
| Attempts and their history | AgentSplit "Attempts" tab (E·2); "attempt 1 of 3" in the failed row and the opened step (E·5, E·7) |
| Budget and cost | Cost on every agent row and still; spend per workflow row; "spent $13.52" in the workflow header with the earlier attempts in its tooltip (E·1, E·3, E·4); AgentSplit details |
| Child agents | AgentSplit tabs (More: children) (E·2) |
| Artifacts, checkpoints | AgentSplit tabs (More) (E·2) |
| Startup progress, liveness | AgentSplit header (state and running time) (E·2); "Stalled" kind in the inbox for live agents with no word for 15 min |
| Owner questions and answers | Inbox "Question" rows with Answer → box + Send (E·4, E·7); "?" mark on the asking step's cell |
| Merge waiting / owner_questions hold | Inbox "Merge waiting: held by 2 questions #2401 #2402", Approve merge disabled until answered (E·4, E·7); "merge waits on" tag in the workflow bar |
| Approvals | Inbox chip "Approvals not known" (the dataset has none to read) |
| Failed steps with red check | Inbox "Failed" rows with the error, Retry, and Why → checks passed/failed by name (E·7); red cell in the stage rows (E·4) |
| Workflow Graph / Table / Steps over time / Changes | View switch above the steps; Stages is this direction's view, the others draw the console's views (E·4) |
| Dispatch | "a PR per step" tag in the workflow bar (E·4); AgentSplit "A PR per step" (E·2) |
| Pull requests | #n on every started step cell and in the inbox rows; "PRs 15" in the header (E·4) |
| Policy | "on failure: carry on" tag with the rule in its tooltip (E·4) |
| Stop a workflow | "Stop workflow…" with a confirm naming how many running and waiting steps it stops (E·4) |
| Step states with park reasons | Stage cell marks and the key under the rows; a waiting step's reason ("Waiting on 1 earlier step: …") in its tooltip and opened panel (E·4, E·5) |
| Step dependencies / inputs | Opened step: "after" and "reads" (E·5) |
| Workflow state counts | Header chips Running / Waiting / Failed and "4/40 done" (E·4); list group headers (E·3) |
| Workflows list: running / parked / failed / done | Groups with counters, Done folded (E·3) |
| Workflow row facts | Step bar, done/total, ● running ✕ failed, need-you count, live browsers, owner, age, spend (E·3) |
| Agent detail (profile, resources, workflow link, task id) | AgentSplit Details header (E·2) |
| Breadcrumbs and one-click moves | Agents › workflow › step breadcrumb; inbox bar back to the workflow; stills and rows open the agent (E·2) |
| Reads footer | The console's footer, unchanged, on every frame |
| Routes (/agents, /workflows, one agent, one workflow) | ?ws=agents/agent/workflows/workflow; the console's routes untouched |
| Phone | Every screen stacks; inbox rows go to two lines (E·10, E·11) |
| Light and dark | Console tokens only; frames rendered in both (E·12 and the -dark pngs) |
