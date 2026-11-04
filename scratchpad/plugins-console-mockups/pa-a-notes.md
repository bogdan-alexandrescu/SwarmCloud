# Direction A: Settings page, inline picker and interview drawer

**The idea.** Stay as close to today's console as possible, so nothing new has to be learnt. Config › Plugins is a
dense table (plugin, pinned version, runner profiles, skills on per mode, allowed here) whose row expands into three
mode columns of skill switches, each skill with what it does, the never-offered skills with why, and the audit line.
In the workflow builder each step card gains one **Plugins** row: compact chips at rest (plugin, mode, skills, and on a
step that ran, what it used and how many times each skill ran), and the picker opens in place on the card (plugin by
name, a mode segmented control, skill chips), with refusals said inline under it. On the run's page the interview is a
**drawer docked on the right**: the answered questions as a conversation, the pending question pinned at the bottom
with its options as rows, and the chosen option's design drawn large in the page's main column. When the interview
ends the drawer's body becomes the proposal with Approve, Edit and Reject.

Frames: `frames/<name>.png` (light), `frames/dark/<name>.png` (dark), `frames/<name>.html` (opens from disk;
`?theme=dark|light`). Switches: `?pa=a&ps=config|builder|plan|needs&pst=rest|open|answered|proposal`, plus
`pv=viewer` (Config read only, C13, framed as PA A·12) and `pb=today` (planner and designer not running yet, C6/B7,
framed as PA A·13).

## + What it gets right

- **Nothing new to learn**: the table, the facts strip, the save bar, the step card and the Needs-you rows are the
  console's own patterns; an admin who knows Secrets and MCP servers reads Plugins at once (PA A·1, A·2).
- **Every skill says what it does where you switch it**, and the never-offered list sits in the same view with its
  reason, so "why can't I pick cso" is answered on the page (PA A·1).
- **The change is visible before it is signed**: switched-off skills are marked "not saved" and the save bar names
  each change as a chip, saying that saving signs and records who and when (PA A·2).
- **Refusals sit on the step they concern**: the codex step's "cannot load plugins" with a one-click by-name fix,
  the workspace-switched-off `ship` chip, the submit bar naming the refused step (PA A·3, A·4).
- **What a step used is on the same chips it chose** (`review ×2`, `health ×0` as a measured zero; the planner step's
  counts shown as *not known*, never 0) (PA A·3).
- **The design is large while you decide**: the main column shows the chosen option's rendered HTML, laid out at its own
  860 px width and scaled down whole to the column (it says the scale it drew at), so it is never reflowed into a broken
  layout; the drawer keeps the earlier answers (who, where: the console, Voice it, Claude Code) in view (PA A·5, A·6).
- **One place for the decision**: the proposal replaces the drawer's body, so Approve / Edit / Reject sit where the
  answers were, with the digest and the automatic-approval line (PA A·7).

## ! What it costs Bogdan

- **The drawer is narrow**: at 1280×800 the four options' descriptions and the earlier questions are cut to one line
  (full text on hover), the conversation shows about two answers, and the preview gets the leftover width, drawn at about
  half size (51%) (PA A·9). At 1440 the descriptions and questions wrap in full.
- **Only one option's design is large at a time**: comparing designs means clicking 1–4 (tabs above the preview, or
  the option rows); the drawer's thumbnails are too small to judge (PA A·5).
- **The phone loses the side-by-side**: the drawer stacks under the run's facts and the design opens inside the chosen
  option's card, scaled to a third of its width; reaching it means scrolling past the question (PA A·10 is that scrolled
  view); a long page.
- **Config is long**: 12 unattended skills make the expanded row taller than one screen; the never-offered list and
  the change history sit below the fold at 1440×900 (PA A·1, A·2).
- **The builder only fits six steps by compacting the stage rows** (stage name on the left, no flow arrows); a longer
  workflow scrolls (PA A·3).

## Shared pieces changed (MOCK-HOOK)

- `App.tsx`: a `Plugins` entry in the Config section's tabs. On its own it did not route: `paths.ts` now knows
  `config/plugins` → `/config/plugins`, `help.ts` titles it "Plugins" (document title, the phone's top bar and the route
  announcement read Plugins, no longer "Not found"), and `Spine.tsx` lists Plugins in the Config side nav, highlighted
  on the page. Direction B gets the same route for free.
- `work/NeedsYou.tsx`: an optional `detail` line under a question row (plugin, mode, skill that asked, time left);
  NeedsA fills it. An approval row says its kind in words ("Plan approval", not `issue_run_plan`).
- `fixtures.ts`: the diagram's labels drawn at 17 in its 760 viewBox (12 px or more down to a 537 px drawing); the design
  doc's sections, so the proposal shows the doc itself; the cso line no longer cites a date after the fixture clock.

## Every features.md item, and where it lives in A

| id | Where |
|---|---|
| C1 | Catalogue table, Plugin column (`gstack`) — A·1 |
| C2 | Pinned version column, "built into the images"; no field takes a version — A·1 |
| C3 | Runner profiles column, a chip per profile — A·1 |
| C4 | Expanded row: three mode columns (Unattended, Planner, Designer), a switch per skill — A·1, A·2 |
| C5 | Under each skill's chip, one line of what it does — A·1 |
| C6 | Each mode column's head: "Runs today", or why not (`pb=today`) — A·1 |
| C7 | "Never offered · 13 skills and 2 families of skills gstack ships, no switch", each group with why — A·1, A·2 |
| C8 | Facts strip "This workspace may use" and the Allowed here switch on the row — A·1 |
| C9 | Facts strip "When nobody has chosen: No plugin allowed"; a not-allowed row says steps naming it are refused — A·1 |
| C10 | Facts strip "You: Admin · can change it"; switches live only for an admin — A·1, A·2 |
| C11 | Facts strip "Setting: Signed · unsigned allows nothing"; save bar "Save and sign" — A·1, A·2 |
| C12 | Facts strip "Last change: Bogdan, 19 Oct"; Changes history line under the row — A·1, A·2 |
| C13 | You: "Member · read only" and a line saying who can change it, switches inert (`pv=viewer`) — A·12 |
| C14 | The page's lead paragraph; picker footnote "By name only: the version comes with the image" — A·1, A·4 |
| B1 | Picker on the step: Plugin, chosen by name from the catalogue — A·4 |
| B2 | Mode segmented control (Unattended, Planner, Designer) — A·4 |
| B3 | Skill chips, "3 of 11", only what the mode offers — A·4 |
| B4 | Picker's plugin line: "1 of 1 catalogue plugin allowed here" — A·4 |
| B5 | second-look on codex: "codex cannot load plugins", with Use claude-code-review; merge: "this runner cannot load plugins" — A·3, A·4 |
| B6 | `ship` chip struck through: "not allowed in this workspace (switched off by its admin in Config › Plugins)" — A·4 |
| B7 | Under the mode control, a refusal when the mode does not run yet (`pb=today`, Planner chosen) — A·13 |
| B8 | Step card Plugins row: gstack, mode chip, skill chips — A·3; in the open picker, changes against the saved set marked "not saved yet" — A·4 |
| B9 | "Last run used gstack 1.92.55.0 · Planner mode" under the chips — A·3 |
| B10 | `×n` on each chosen skill chip (`×0` measured; planner step: "not known: its stream was not read") — A·3 |
| B11 | Submit bar "Submit refused at second-look · nothing was started", the reason on that step — A·3, A·4 |
| I1 | Pinned question's text in the drawer — A·5, A·9, A·10 |
| I2 | Option rows: label and description — A·5 |
| I3 | "Recommended" chip on option 1 and on the large preview's head — A·5 |
| I4 | Thumbnails in each option row; the chosen one large in the main column with tabs 1–4 (inline in the card on a phone) — A·5, A·10 |
| I5 | "Asked by `design-html`" on the pinned question — A·5 |
| I6 | The drawer's conversation of answered questions — A·5, A·6 |
| I7 | Each answer's person (Ana Moreau, Kenji Sato) — A·5, A·6 |
| I8 | Each answer's place: the console, Voice it, Claude Code — A·5, A·6 |
| I9 | "Use the recommended answer" button — A·5 |
| I10 | "In my own words" — A·5 |
| I11 | Expiry bar "23 h 22 min left" and the line "Unanswered by …, the step ends with the question open: nobody's answer is guessed" — A·5 |
| I12 | "The first answer stands" line — A·5, A·6 |
| I13 | Drawer status "Parked, waiting for an answer · holds no lease, slot or container" — A·5 |
| I14 | Drawer head: gstack 1.92.55.0, mode chip, the skills it may use — A·5, A·7 |
| I15 | Drawer head "question 13 · 12 answered" and "how many more questions: not known" — A·5, A·6 |
| I16 | Proposal: summary, plan steps, requirements; the design doc itself large on the left (lead, diagram, four sections) — A·7 |
| I17 | Approve, Edit, Reject against revision 1 and its digest — A·7 |
| I18 | The line under the buttons: started with automatic approval it would go ahead on its own — A·7 |
| N1 | Needs you: "Question · roombook-checkout-redesign" row — A·8 |
| N2 | The unattended `review` step's question, "had no recommended answer, so it waits for a person" — A·8 |
| N3 | Answer in the row, or "Open the interview, with its 4 designs" — A·8 |
| N4 | "23 h 22 min left" per row and the footnote on 24 hours, nobody's answer guessed — A·8 |
| N5 | gstack, mode chip, "asked by design-html" / "asked by review" — A·8 |

## Verified, and not

- fix-a (2026-11-04): the review's 17 findings fixed (fixes.md). `npx tsc --noEmit -p .` in apps/swarm-ui: no errors.
- Frames (2026-11-04, live browser): all 13 rendered again after the fixes, in light and dark, and read by four
  reviewers, one per screen. Three PNGs are not right yet because the live browser crashed before they could be taken
  again: `dark/pa-a-builder-open.png` is a blank white capture (taken before the page painted), and both
  `pa-a-plan-rest-390.png` (light, dark) show the chosen card's room for its design but not the design itself (the
  iframe was laid out, 330x298 at 38%, but not painted when the shot was taken). Their HTML frames are right.
- Seen in review, outside the verdict and not changed: in A·2 the save bar covers the document-release row, so its
  not-saved mark shows only in the save bar's summary, not in place.
- No unit tests were run (mock-ups only).
- Owner decision (2026-11-04, answer to the parked question): those three PNGs (`dark/pa-a-builder-open.png`,
  `pa-a-plan-rest-390.png` light and dark) are **accepted as shipped**; their HTML frames are the reference.
