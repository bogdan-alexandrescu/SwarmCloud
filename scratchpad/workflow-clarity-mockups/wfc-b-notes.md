# Direction B: a Facts card beside the description (?wc=b)

**The idea.** The head keeps the title and today's chips row: state, pull request, steps, cost and age. Under them, one card answers Bogdan's question:
*what is this, and where did it come from?* On the left is the description, the two or three sentences the submitter wrote, in body type. On the right is a
column of five labelled facts: **Repository, Feature, Started by, From, When**. The labels use the console's label rank (13px mono, faint), so the
column reads as a definition list. On a phone (and in any pane under 520px) the facts stack under the description, with a rule between them. In the Workflows
list the repository is its own narrow column after the name: its name, with its owner under it in faint ink. On a phone that column gives way, and the
whole `owner/repo` goes under the title. Titles wrap and read whole in the list rather than being cut.
Every surface names a workflow by `metadata.title` and falls back to the label (`metadata.unit`) only when there is no title. That covers the page heading, the
breadcrumb, the list rows, the Needs-you strip and the sidebar's Recent list. The fixture labels (`vqa-rev`, `vqa-mod`, `sg-click`, `deps-n`, `pl-rn`,
`ckpt-mv`) appear on no frame. **The legs of one quality check share a title**, so the name adds the leg (`metadata.leg`):
'Visual QA: ai-studio-staging · reviewer leg' and '· moderator leg', on the heading, the breadcrumb, the list rows and Recent. Recent wraps to two lines,
so the leg is not cut off the shared title (wfc-b-head-leg, wfc-b-list).

## + What it gets right
+ **Each fact has a fixed place.** The five labels sit in the same spot on every workflow, so a reader learns where Repository is once
  (wfc-b-head-rest, wfc-b-head-lane, wfc-b-head-generated).
+ **A missing value keeps its slot and its label.** No feature reads as a `none` mark (`.ctl-mark.is-zero`: it was asked, and there is none), never as a
  row that is missing (wfc-b-head-nofeature). A workflow submitted before these fields existed reads `not recorded` (`.ctl-mark.is-absent`). Those are two
  different kinds of nothing, as design-system §6.10 asks.
+ **A generated description is marked three ways**: a `generated` chip beside the label, the text in the dim ink rather than full ink, and a visible
  line under it, "A model wrote this from the steps' prompts; no description was submitted." A touch screen reads that line without a hover
  (wfc-b-head-generated). The marks cannot be confused even in greyscale.
+ **The description is read whole.** It is clamped at five lines, not two, so a two-or-three-sentence description is never cut (wfc-b-head-rest). Show
  all appears only when the clamp measurably cut text (wfc-b-head-long → wfc-b-head-long-open), not by a character count.
+ **Nothing is drawn twice.** The page's count note lost "by … · started …", and the Overview inspector lost Owner and Started (it keeps
  Changed), because the card now holds them. When gives the UTC instant and leaves the age to the chips row's "16d 11h old".
+ **From is a chip** (Claude Code / Console / Schedule / Issue run / Quality check) with its reference beside it as a link where there is one
  (issue #3201, schedule `nightly-deps`). It is not a sentence.
+ The repository breaks only after the slash, never mid-word (wfc-b-head-lane: `bogdan-alexandrescu/` then `SwarmCloud`).
+ At 1280 the inspector folds to its rail, the card gets the width, and a two-sentence description runs to three lines (wfc-b-head-rest-1280).

## ! What it costs
! **At 1440 the card shares a ~580px pane with the docked inspector.** The facts column takes ~250px, so the description runs at ~300px:
  five lines for two sentences (wfc-b-head-rest), and a long repository or feature wraps to two lines (`example-org/` / `agent-swarm-infra`,
  "Per-tenant isolation / hardening" in wfc-b-head-long).
! **The card is tall.** It is 200–250px before the Needs-you row and the grid, so at 1440x900 the stage grid starts around y=540–620 rather
  than y=330 (compare wfc-b-head-rest with today's page). Expanded, the long description pushes the grid almost off the first screen
  (wfc-b-head-long-open).
! **List rows are two lines tall.** The name wraps instead of being cut, and the Repository cell holds the name over its owner. At 1440 the
  list shows 9 rows in about 480px where today's one-line rows take about 300px (wfc-b-list). Spent's "3 of 4 reported" moves under its figure to
  give the name that width.
! **On a phone, Steps is a count, not a bar** (`4/6`). The bar's width went to the title, which now wraps to two or three lines and reads whole.
  The state glyph keeps the colour (wfc-b-list-390).
! **Legs are told apart by a word in the name, not by a different title.** The submitter could title each leg instead. Until then the console
  adds `· <leg> leg` from `metadata.leg`.

## What the server must serve (not served today)
The frozen `Workflow` (codec `workflow_to_api`) has no `metadata` and no `repository_url`. The mock-up adds both to the fixture workflow as optional
fields: `metadata.{title, unit, leg (a quality-check leg: 'reviewer'|'moderator'|null), description, description_source: 'submitter'|'generated', feature: {title, url|null}|null,
origin: {kind, ref: {label, url|null}|null}}` and `repository_url`. Who and when come from `submitted_by`, `submitted_by_name` and `created_at`, which are
served today. Serving these fields is a contract change and needs its own request (docs/contract-change-requests.md). This step does not make it.

## Found while drawing, outside this direction (left as they are, except the first)
- The list's step-bar count was a `<small>` at 11.7px, under the 12px floor. wc.css raises it to `--t-micro` (12px) for every view, not only ?wc=b.
- Code lane: PR #3244 appears three times: in the chips row, in the Overview's Pull requests and in its Dispatch line (wfc-b-head-lane).
- At 390 the Needs-you row drops its words and keeps bare glyphs beside "not known" (wfc-b-head-rest-390).
- "cost not reported" is drawn in italics with an underline, so it reads like a link (wfc-b-head-nofeature).
- The Overview reads "1 steps read" and "0 opened by 1 steps read" (wfc-b-head-generated, wfc-b-head-nofeature).
- The Needs-you strip over the list still cuts its titles to one line ("Check the five contract invari…", wfc-b-list). It is the redesign's strip,
  not this direction's row.

## How the frames were made
The apps/swarm-ui dev server ran on fixtures with `?wc=b`. The live browser's size belongs to the gate, which offers no 1280x800 preset, so each frame
is rendered in a same-origin iframe sized to its viewport (tools/wc-frame.html) and the screenshot is clipped to it (tools/render.py). Light
and dark are set with `data-theme`. In the first round two Sonnet reviewers read all 20 PNGs (light and dark) against the checklist. They found two problems in this
direction: the rule ran past the facts when the description was open, and the repository was cut short on the phone row. Both are fixed, and the
frames were rendered again.
After the review (verdict NOT_YET) the fixes in fixes.md were made, and all 11 frames were rendered again in light and dark (22 PNGs). Two Sonnet
reviewers, one for each screen, read all 22. They found nothing in this direction. The one thing they flagged is the Needs-you strip's cut titles, which
come from the redesign (above).
