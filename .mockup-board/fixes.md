# Board by state — fixes from review (verdict NOT_YET, 10 findings)

Mock-up only: no commit, no pull request. `variants.patch` is the bundle's patch with these fixes on top, against main 217598f.
Files: `apps/swarm-ui/src/voice/ProjectsBoard.tsx`, `apps/swarm-ui/src/styles/projects-board.css` (Section.tsx unchanged from the bundle).

| # | Finding | Fix | Frames re-rendered |
|---|---|---|---|
| 1 | board-states: the Waiting tooltip covered the Newsletter card and its "Your pick" | A column-head tooltip now opens in the flow, as a full-width row under the column head (`.pb-col-h` wraps, the tip host is `display: contents`, the tip is `position: static`), so it pushes the cards down and covers nothing. The cost is added to notes.md: hovering an (i) moves that column's cards. | states |
| 2 | switched / new-mid / aside: the narrating pill at the end of the strip | Removed: the `note` prop, `.pb-strip-note` and its phone rule are gone. The captions still say what happened. | switched, new-mid, aside |
| 3 | switched: Signup card had both a "Pick" tag and a "Your pick" chip; new-mid: "New" chip plus the pill | A Talk card that asks shows only its ask; the kind tag shows only on cards that ask nothing. The strip's "New" word becomes a small info-coloured dot, with "Started this session" in its title and aria-label (no word on the surface). The pill went with fix 2. | switched, new-mid |
| 4 | new-mid: the new brief already had dates while Voice it asks for them | Card body: "Goal: a banner with the holiday hours, on every page. Dates not set yet." | new-mid |
| 5 | mark: the scrim did not dim the brief drawer; the outcome named #231, which appears nowhere | The drawer is portalled to `document.body`, like the dialog, so the dialog's scrim (z 50) now sits over the drawer (z 30). The state tokens moved onto `.pb-panel` as well, since the drawer is no longer inside `.pb`. Prefilled outcome is now "Home page live, merged #227", matching the timeline's 8 Nov merge. | mark, brief |
| 6 | catchup-390: the sticky mic bar floated over the timeline with no backing | On a phone, the board's Talk `.vt-mic` sticks at `bottom: 0` with a full-width `--surface` backing and a top hairline, bleeding to the pane's edges. Nothing scrolls past beside it any more. | catchup-390 (and the other 390 Talk states share the rule) |
| 7 | rest-390: the caption said the filter bar scrolls sideways, but it wraps | Caption and notes.md now say the chips wrap to two rows. The bar is unchanged. | (caption only) |
| 8 | states: Archived column and cards had heavy dashed outlines | The Archived column gets the same solid soft border as the others. Archived cards get one 1 px dashed `--line-soft` outline, including the left edge (no 3 px tone edge). | states |
| 9 | the bakery's "Opening hours" card had no Open button | Added "Open", matching its siblings. | two-open, aside, catchup |
| 10 | aside listed an "Order page build" running before the review the brief says comes first | That row is removed. The aside lists the invoice reminder draft (running) and the rota calendar sync (Approve). Voice it's caption now says "Two: the invoice reminder draft is running, and the rota calendar waits on your approval. Still on the bakery website." | aside |

notes.md was updated wherever it described what changed: the archived look, the tooltip's cost, the "New" dot, the aside no longer noted in the strip, and the 390 filter bar and mic bar.
frames.json: new captions for rest-390 (wrap) and states (where the tooltip opens).

## Stylesheet guards (pre-publish gate)
The sheet failed four stylesheet guards. Some failures were already in the bundle and one came from fix 1. Fixed in `projects-board.css`:
- **Duplicate selectors.** The three `.pb-talk` rules are now one. The two `.pb-col-h` rules (one of them added by fix 1) are now one.
- **Raw line-heights.** The five raw line-heights (18 and 20 px) are now `var(--lh-micro)`. The two 20 px pills get 1 px of vertical padding to keep their height.
- **Uppercase labels.** The timeline's kind label (`.pb-tl-k`) used uppercase with tracking. It now uses the console's label rank: mono and `--text-faint`, no capitals, no letter-spacing.

The four guards pass, run single-fork on named files: `stylesheet.gate`, `sections.stylesheet`, `letterspacing`, `typescale`, 701/701.
