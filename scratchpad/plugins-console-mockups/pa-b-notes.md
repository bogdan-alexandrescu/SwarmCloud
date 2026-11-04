# Plugins console, direction B: capability cards, guided picker, full-page interview

**The idea.** Direction B is built around the interview. Config › Plugins shows one capability card per plugin: its
pinned version, the profiles it runs on, a row saying which modes run today, then tabs per mode listing every skill as
a tile with what it does, plus a "Never offered" tab, and the workspace allow switch on the card. In the builder, a
step's plugin is chosen in a short guided sheet: 1 plugin, 2 mode, 3 skills, each choice explained. The sheet leaves
one segmented chip on the step (`gstack | Unattended | review · investigate · …`). The interview takes the whole
planning page. It asks one question at a time, with its options side by side and each option's rendered design in
the card. On the left a progress rail lists every answered question, with who answered and where. A decision bar stays
at the bottom: time left, "Use the recommended answer", "Answer A". The proposal gets its own page with the same bar
(Reject, Edit, Approve against the digest).

Code: `apps/swarm-ui/src/pluginsconsole/B/` (`ConfigB.tsx`, `BuilderB.tsx`, `PlanB.tsx`, `NeedsB.tsx`, `b.css`, every
rule scoped to `.pc-b`). Open with `?pa=b&ps=<config|builder|plan|needs>&pst=<rest|open|answered|proposal>`, plus
`?pv=viewer` and `?pb=today` as in base-notes.md.

## + What it gets right

+ **The question is the page.** At 1440 the four designs sit 2×2 above the fold, the recommended one picked and marked.
  Each is the real HTML, laid out at 820 px and scaled into its card, with Enlarge for full size (PA B·5).
+ **The answer is always one click away.** The decision bar stays at the bottom of the question column. It shows the
  expiry as a number (23 h 22 min left of 24 h) and "Never answered for anyone. The first answer stands." (PA B·5, B·9)
+ **History as a rail, not a table.** Each of the 12 answers shows the short question, the answer in bold, an avatar,
  the name and a "Console / Voice it / Claude Code" mark. The head counts them: Console 6, Voice it 5, Claude Code 1.
  The rail opens scrolled to the current question (PA B·5, B·6).
+ **One chip per step.** The chip names the plugin, mode and skills in one line. It is struck through and red-edged on
  a refused step, and the refusal uses check_step's own words. "Last run used" counts ×2 / ×1 / ×0, and a dash means
  the run was not counted; it never shows 0 for that (PA B·3).
+ **The picker teaches.** Every mode has its one-line meaning and every skill its note, so the person needs no
  catalogue page open. The live chip at the sheet's foot shows what the step will carry (PA B·4).
+ **The cost of a change is said before it is saved.** Switching gstack off names the 3 steps it would refuse, with the
  refusal's exact words, before "Save and sign" (PA B·2).
+ **Nothing typed.** No control takes a path, a command, a URL or a version. The version is a read-only chip ("pinned
  in the image, never chosen here"), and plugins, modes and skills are radios and ticks over catalogue names.

## ! What it costs Bogdan

! **Previews are pictures, not readable pages, until enlarged.** Four designs side by side at 1440 come out at about
  0.42 scale, so the text inside them is too small to read (PA B·5; worse at 1280, PA B·9). They are good for comparing
  where the timer sits. Reading a design takes a click on Enlarge.
! **The sticky decision bar covers the lower options while you scroll** (the bottoms of C and D in PA B·5 and B·9). This
  is the price of always having the answer in reach.
! **On a phone the rail drops below the answers** (PA B·10). The history is a scroll away, and the four previews stack
  into a long page.
! **The card is tall.** Twelve skill tiles push the page below the fold (PA B·1), and on a phone the mode tabs scroll
  sideways (PA B·11).
! **A full page per question means leaving the list.** Needs you keeps the short answer in the row. The designs need
  "Open the interview with its 4 designs" (PA B·8).
! **The proposal page shows the interview only as tallies**, for example "Kenji Sato · Console · 4". The served run has
  no per-question history for a planner run (PA B·7).

## Where each features.md item lives

| id | Where (frame) |
|---|---|
| C1 | Card head, plugin name (B·1) |
| C2 | Version chip with "pinned in the image, never chosen here" (B·1) |
| C3 | "Runs on" profile chips, with "other runner profiles cannot load plugins" (B·1) |
| C4 | Mode tabs with counts (Unattended 12, Planner 6, Designer 4); a skill tile per skill (B·1) |
| C5 | Each tile's line: what the skill does (B·1, B·4) |
| C6 | Mode row above the tabs, "Unattended: runs today …"; with `?pb=today` it reads "not yet" with the bridge reason, and so does the tab's lead line (B·1) |
| C7 | "Never offered 15" tab: each group struck through, with why (the tab is in B·1, its panel opens on click) |
| C8 | The allow switch on the card; side card "This workspace may use" (B·1, B·2) |
| C9 | Side card, "When nobody chose: No plugin is allowed" (B·1) |
| C10 | Under the switch, "You are an admin: you can change this"; Save and sign bar (B·2) |
| C11 | Side card, "Signed: only a signed setting allows anything; an unsigned one allows nothing"; "Save and sign" (B·1, B·2) |
| C12 | Side card "Changes": who, when, before → after, newest first (B·1) |
| C13 | `?pv=viewer`: the switch is disabled, with "Read only: only the workspace's owner or an admin changes this" (not one of the 11 frames) |
| C14 | The lead line; no input anywhere takes a path, command, URL or version (B·1) |
| B1 | Sheet step 1, Plugin (B·4); "+ Add a plugin" on a step (B·3) |
| B2 | Sheet step 2, Mode, with the meaning of each (B·4) |
| B3 | Sheet step 3, skills ticked from the mode's list, "2 of 12" (B·4) |
| B4 | Step 1 offers only allowed plugins and says so; a plugin not allowed shows disabled, with the refusal's words (B·4) |
| B5 | The codex step's refusal box (B·3, B·4); the sheet on a codex step says the profile refusal in its step 1 |
| B6 | The words in the config-open bar (B·2); a disabled plugin row in sheet step 1 |
| B7 | Sheet step 2 with `?pb=today`: Planner and Designer disabled, "Not yet: …" with the bridge reason |
| B8 | The segmented summary chip on each step (B·3), previewed in the sheet foot (B·4) |
| B9 | "Last run used gstack 1.92.55.0 · Unattended" line (B·3) |
| B10 | ×2 ×1 ×0 per skill; a dash plus "times each ran: not known (stream not read)" on plan-checkout (B·3) |
| B11 | "Refused at submission:" on the step it names (B·3) |
| I1 | Question title (B·5) |
| I2 | Option cards A–D with label and description (B·5) |
| I3 | "Recommended" chip, preselected (B·5) |
| I4 | The rendered HTML design in each card, plus Enlarge (B·5); the chosen design large (B·6); diagrams on the proposal (B·7) |
| I5 | "asked by design-html at 19 Oct, 11:22 UTC" (B·5) |
| I6 | Left rail, 12 answered (B·5) |
| I7 | Avatar and name per rail item (B·5) |
| I8 | Console / Voice it / Claude Code mark per item, with counts in the rail head (B·5) |
| I9 | "Use the recommended answer" in the bar (B·5) |
| I10 | "None of these fits? Answer in your own words" under the options; opens a box in the bar |
| I11 | Bar: "23 h 22 min left of 24 h · Never answered for anyone"; its tooltip gives when it ends (B·5) |
| I12 | Bar: "The first answer stands" (B·5); after answering, "A second answer to question 13 is refused" (B·6) |
| I13 | "Parked: holds no machine" chip in the facts row (B·5); "Answered: queued to resume" (B·6) |
| I14 | Facts row: step, repository, gstack 1.92.55.0 · Designer · skills (B·5, B·7) |
| I15 | Rail head: "12 answered · how many more: not known" (B·5) |
| I16 | Rail foot, "Then: the design proposal …" (B·5); the proposal page (B·7) |
| I17 | Proposal bar: Reject, Edit, Approve, with revision and digest (B·7) |
| I18 | Rail foot: "not set to approve automatically"; with `plan_approval: 'auto'` the bar shows "Approved automatically" (B·7) |
| N1 | The real NeedsYou: the designer step's question row (B·8) |
| N2 | The unattended review step's question row, and its card, "no recommended answer" (B·8) |
| N3 | Answer in the row; "Open the interview with its 4 designs" on the card (B·8) |
| N4 | Card: "23 h 22 min left · asked …"; the panel head: "Unanswered in 24 hours, it ends with the question open" (B·8) |
| N5 | Card chip: gstack · Designer · asked by design-html (B·8) |

## Shared pieces changed

Only `PluginsConsole.tsx`. It imports B's four screens and `B/b.css` (`// MOCK-HOOK`), and draws `screenB()` when `?pa=b`.
No other shared file was changed. `fixtures.ts` and `parts.tsx` are used as they are. `PlanInterview.tsx`'s `leftWords`
and `plainLabel` are imported, not copied.

## What this direction did not solve, and reviewer notes

- The nav still highlights **MCP servers**, and the phone's top bar reads "MCP servers". The scaffold sits under that
  path because Config has no Plugins entry yet (base-notes, shape 10). The real build adds the tab.
- The answer in PA B·6 is by Ana Moreau, as the fixture has it, not by the person viewing.
- On the proposal page the diagram stays on white in dark mode. It is the SVG picture the skill wrote, like the design
  previews.
- **The workspace setting allows plugins, not skills.** The brief's config-open frame says "which skills the workspace
  allows", but main's setting (`plugins.allowed`) holds plugin names only, and skills are chosen per step. PA B·2
  therefore shows the plugin switch mid-change. A per-skill allow-list is a product change: it is asked in
  questions.json.
- A hand-off row was left out of Needs you. The console measures its deadline against the real clock, not the fixture
  clock, so it would always read "time is up".
- The 1280×800 frame was drawn in a 1280×800 iframe on the 1440×900 browser, because the live browser has no 1280
  preset. The app's own media queries see 1280.
- The text inside the scaled previews is below 12 px by design: it is a picture of a design, and Enlarge shows it at
  full size.
