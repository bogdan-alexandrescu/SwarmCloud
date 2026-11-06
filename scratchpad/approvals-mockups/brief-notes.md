# Direction: brief, the plan as a one-page brief (`?ap=brief`)

**The idea.** One scrolling page, read top to bottom in the order a decider asks
the questions. The header card answers *what, why, how big, how risky, and what
do I press* in the first screen. Under it, the change map shows *where*: the
changed files grouped by area, their callers to the left and their tests to the
right, with every edge labelled. Next come Risk and Validation side by side, then
the steps in order, then the overlaps. The raw briefs stay folded at the bottom.
Nothing is behind a tab: scrolling is the only interaction the page needs.

Built on the fixture run `run_9b8af0113faa4165ab8e` (issue #3507: one step, five
files, 2.5 h, three overlaps, four requirements). The sections the parallel lane
adds to the planner are invented where the run does not serve them; each is
marked MOCK-HOOK in `ApprovalBriefData.ts`. The plans in `brief-high-risk` and
`brief-multi-step` are invented outright.

## + What it gets right
+ **Ten-second header** (brief-top, brief-top-390): the purpose sentence as the
  headline, *Why* in one line with its evidence behind (i), and four fact tiles
  (Size, Estimate, Risk, Overlaps). Under each of Approve, Edit and Reject sits a
  three-word caption of what it does; Edit's says *Opens the run*, which is all it does in this mock. The card's top edge takes the risk colour.
+ **The map is a real graph, drawn by hand in SVG** (brief-graph): it uses no
  diagram library. Changed files sit in area boxes, callers and tests flank
  them, and edges are labelled calls, imports or tests. Picking a file lights its
  neighbours, dims the rest, and opens a strip with its one-line change, *called
  by*, *uses* and *tested by*. The per-file change list under the map works the
  same way, so the map also has a keyboard and screen-reader route.
+ **High risk cannot be missed** (brief-high-risk, brief-high-risk-map): a red
  top edge, the Risk tile filled red, and a *Touches* strip in the header naming
  Invariant 2, Invariant 3 and *Frozen contract · contract.py*. In the map the
  frozen file sits in a red dashed area. The Risk card turns red too, with the
  invariants in red rows.
+ **Validation ties tests to requirements** (brief-validation): each red-first
  test (T1–T3) says what it proves and carries the requirement ids it covers. The
  QA checks follow as a numbered list (Q1–Q3), each with who and where; they are
  ticked on the run page after deploy, as their (i) says, not here. The four
  requirements fold under *Done when*.
+ **Overlaps open to compare** (brief-overlaps): *Compare* on a row shows each
  shared file with what this plan does to it and what the PR does to it.
+ **Order is visible twice** (brief-multi-step, brief-multi-step-order): step
  badges ①②③ on the changed files in the map, and the steps grouped by stage
  (*Stage 2 · in parallel*, *after step 1*).
+ **Decided in place** (brief-decided): the decision row becomes a green receipt
  showing who approved, when, and the workflow it started, linked.
+ **Phone** (brief-graph-390): the map changes layout instead of shrinking.
  Each changed file sits with its callers above it and its tests below. Callers sit above each
  area box and tests below it; only changed files are inside the box. Edges run
  down a lane gutter, and no two overlapping edges share a lane. The picked
  file's strip opens in the map right under the file, so it covers nothing.

## ! What it costs
! **Length**: on the fixture the page is about four 1440×900 screens. Risk and
  Validation start under the fold (brief-top), so a reader who only reads the
  header has to trust the Risk tile until they scroll (brief-risk).
! **The map does not scale far**: three columns suit up to about 12 nodes. A
  plan with 30 files would need area folding or a "show callers" toggle, which
  this mock does not have (brief-graph).
! **Arcs between changed files take room from the map** (brief-high-risk-map):
  each rides its own lane right of the Changes column, with its label along it,
  and the Tests column moves right to make room; test edges cross the lanes
  squarely. Past three or four arcs the Changes column gets narrow.
! **The phone map is long** (brief-graph-390): five files with their callers
  and tests run past one screen, so the edges become a lane diagram, not a
  picture of the structure.
! **Edit** only opens the run page, and its caption says so: an inline editor is
  out of scope for this mock (brief-top).
! The Why evidence is behind (i), so on a phone it costs a tap (brief-top-390).

## How the frames were made
The dev server ran on its fixtures. The frames were captured in the live
browser. From 09:45 UTC the gate refused every `swarm-handoff device` and
`theme` change (`unresponsive`), so the frames come from a same-origin page.
That page holds the app in an iframe of exactly the frame's size (1440×900 or
390×844), and the light or dark theme is set through the console's own
`swarm.theme` choice. The theme switch in the sidebar therefore shows Light or
Dark, not System. The page clock is pinned to 2026-11-06 10:09 UTC, so every
light and dark pair shows the same times, and the mouse is moved off the page
before each shot. Every section has a phone frame too (`*-390`). Each frame is in `frames/<name>.png` (light) and
`frames/dark/<name>.png`; `frames/<name>.html` is the light DOM with its CSS
inlined.

Mock URLs (`?ap=brief`):
- `/automate/approvals/run%3Arun_9b8af0113faa4165ab8e?ap=brief`: the fixture
- `/automate/approvals/run%3Arun_mock_admission_multipool?ap=brief`: high risk, invented
- `/automate/approvals/run%3Arun_mock_plan_brief_three?ap=brief`: three steps, invented
