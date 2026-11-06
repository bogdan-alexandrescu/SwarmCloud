# fixes.md: direction brief (one-page brief), verdict NOT_YET, 13 findings

I applied the bundle's `variants.patch` to a clean checkout, fixed every finding and nothing else, and re-rendered all frames in light and dark. Nothing is committed and no pull request is open. The new `variants.patch` applies cleanly to HEAD (b6f1248).
The fixes are in `ApprovalBrief.tsx`, `ApprovalBriefData.ts` and `styles/approval-brief.css`; `App.tsx`, `Approvals.tsx` and `paths.ts` are as the bundle had them.

| # | Finding | Fix | Frames |
|---|---|---|---|
| 1 | Text under 12px in the map, file chips, test files, overlaps, slugs and the legend badge | Edge labels, node sub-labels, area names, step badges and the legend badge are now 12px. Step chips, test file names, overlap file names and step slugs are also 12px; `.mono` inside the page is now `max(12px, 0.92em)`. | all |
| 2 | Phone captions under the decision buttons were 11px | Removed the phone-only 11px rule, so they use `--t-micro` (12px) as on desktop | brief-top-390, brief-decided-390 |
| 3 | brief-risk = brief-validation | Re-captured. brief-risk is the Risk card top-aligned. brief-validation is scrolled to the end of Validation with *Done when* opened (tests, QA, requirements). Now 0 identical PNGs in either theme. | brief-risk, brief-validation |
| 4 | brief-steps = brief-overlaps | Each overlap row now has a *Compare* button that opens the shared files, with what this plan does to each and what the PR does (`theirs`, MOCK-HOOK, invented). brief-overlaps shows #3496 opened. | brief-steps, brief-overlaps(-390) |
| 5 | Edit promises "Change the plan first" but only opens the run | Caption is now "Opens the run" | brief-top(-390), brief-high-risk |
| 6 | Prose on the surface next to Tests and Raw step briefs | Both moved behind an (i) | brief-validation, brief-raw |
| 7 | QA drawn as live checkboxes | Now a numbered list (Q1…) with who and where. The (i) on *QA after deploy* says the checks are ticked on the run page after deploy, not here. | brief-validation(-390) |
| 8 | Phone map: callers sat under area labels; the pinned strip covered the map | The phone layout now goes area by area: callers above, a box holding only the area's changed files, then tests below. The picked file's strip opens inside the map, right under the file, and the map makes room for it. It is no longer sticky, so it covers nothing. | brief-graph-390 |
| 9 | High-risk map: arcs and test edges crossed in the right gutter | Each arc between changed files has its own lane right of the Changes column, drawn square, with its label rotated along the lane. The Tests column moves right to make room. Arcs attach below a file's middle and test edges above it. Test edges run straight across the lanes before bending, so any crossing is square and each label sits by its own test. | brief-high-risk-map |
| 10 | Dimmed nodes unreadable in light | Dimmed nodes go to 0.62 opacity (was 0.32), dimmed edges to 0.45 (was 0.22) and dimmed labels to 0.6 (was 0.25) | brief-graph |
| 11 | Stray hover captured | The mouse moves to (0,0) and focus is cleared before every shot | brief-graph, brief-multi-step |
| 12 | No phone frames below the map | Added brief-risk-390, -validation-390, -steps-390, -overlaps-390, -raw-390 and -decided-390 | new |
| 13 | Light/dark clocks differ | The page clock is pinned to 2026-11-06T10:09:00Z (Playwright `clock.set_fixed_time`) for both themes | all |

## Verified
- `npx tsc -b --noEmit` in apps/swarm-ui: clean.
- `npx vitest run --pool=forks --poolOptions.forks.singleFork` on approvals.test, approvals.qa1, approvals.qa2, approvals.qa2fix13 and approvals.move: 5 files, 48 tests passed.
- 20 frames × 2 themes rendered: `frames/<name>.png` (light), `frames/dark/<name>.png`, `frames/<name>.html` (light DOM, CSS inlined), and 15 fonts in `assets/`. `md5sum` finds no identical pair.
- I looked at the changed frames myself. Two Sonnet reviewers then read all 40 PNGs against the 13 findings (desktop and phone, light and dark) and found them fixed. One new defect was found: brief-raw-390 had been captured with the fold closed, because the click hit the (i) in the summary. I re-captured it, opened, and checked it in both themes.
- `frames.json` is updated (20 entries). `notes.md` is updated for the changed behaviour (Edit caption, QA list, Compare, phone map, arc lanes, pinned clock).

## Not verified
- Frames come from an iframe of the frame's size in the live browser, as in the original bundle; the gate's device and theme switches were not used.
- The live run data still lacks `theirs` on an overlap, so the Compare row's PR line is invented (MOCK-HOOK).
