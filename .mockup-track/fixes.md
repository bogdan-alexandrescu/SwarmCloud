# fix-track: Activity timeline, fixes for the review's 11 findings

**STATUS: CODE FIXED, FRAMES NOT RE-RENDERED.** The live browser crashed at 22:16 UTC, partway through the first render (`track-ended` and `track-projects` came out blank). The gate then refused every call with "the live browser is gone for this attempt (Chromium could not be started again)". This bundle has the fixed `variants.patch` and only the 4 light frames whose code has not changed since they were rendered. Seven light frames and every dark frame are missing. The verdict's checks need a new render, in light and dark, before review.

**Re-render (one command per device and theme, browser up, dev server on fixtures):**
`swarm-handoff device desktop; swarm-handoff theme light; python3 render.py desktop light`, then `theme dark` / `dark`. For the phone: `device phone-390` / `phone-390 light|dark`. render.py writes frames/, frames/dark/, the HTML, assets/, and a measurement JSON per run (ctl-scroll overflow, scroller pins, row rects, tooltip rect, iframe min font-size, md5).

## The findings and what changed

The base is variants.patch from the track bundle. Fixes F1–F11 come from an earlier attempt at this step, which was interrupted before it rendered. That work is in the working tree; I kept it, re-rendered what I could, and measured it. The last two fixes below are mine.

| # | Finding | Fix | Measured (desktop, light, 22:15 UTC) |
|---|---|---|---|
| F1 | working-collapsed == handed | A later moment, at 0:38: the person asks for the order button near the top, the voice answers "Noted…", and the working card sits between that reply and the next one. Rows have moved on. | md5 7dc9a47e ≠ handed 0ce12369. ok |
| F2 | ended: newest line sliced | `.vtk-lines` is a `column-reverse` scroller, so it opens on its newest line, with no script and in a saved frame too | resumed: last caption bottom 704 = scroller bottom 704. ended: **not measured** (blank render) |
| F3 | 390: oldest line shown | Same pin | **not measured** (phone not rendered) |
| F4 | paused-390: workflow below the fold | Phone timeline tightened (gaps, row padding, lines capped at 5.2em) | **not measured** |
| F5 | handed-390: progress cut / wraps | Progress lines shortened ("Hero and order button · 1 of 4") and tightened | desktop: every `.vtk-row-doing` is 19px = one line. 390 **not measured** |
| F6 | tooltip hides its own row | Reason tooltip opens left of the timeline, level with its row | tip [725,649–965,733]; rows start at x=1015: neither row covered. It covers the end of the newest captions while open (noted in notes.md). ok |
| F7 | 1px page overflow with the Stage | Earlier attempt's change did not fix it: still **872/871** in resumed and failed-agent. **Mine:** `row-gap: 10px` (was 14px) on `.vt.vtk.has-frames` removes 8–16px above the talk row | after the change: **not measured** |
| F8 | dark: native grey scrollbar | `scrollbar-width: thin` and `scrollbar-color` in the theme's ink on the talk's scrollers | **not looked at** in dark |
| F9 | log vs row disagree | Log "0:19 Frame drawn: Warm & rustic, hero"; row at 0:38 "Menu strip · 2 of 4" (hero done) | ok (fixture text) |
| F10 | Planner 2:25 in resumed/ended | Planner ends at 0:11 (0:09 elapsed) in every state | resumed 0:09. ok |
| F11 | Stage thumbnail text unreadable | The iframes are no longer zoomed; their text is set at ≥12px | resumed and failed-agent: every iframe min 12px, scrollHeight = clientHeight (nothing clipped). ok |

**My second fix (a regression from the fixes above, not in the verdict):** F1 made the working-open log longer. The page then overflowed by **40px** (ctl-scroll 911/871), and the log opened on its **oldest** line, which hid the newest one, the point of the card. The log now scrolls in a `column-reverse` wrapper (`.vtk-steps-scroll`, max-height 10em), so it opens on its newest line, and the page fits. **Not re-rendered.**

## Verified
- Related tests, single fork, run while no dev server was up: `npx vitest run --pool=forks --poolOptions.forks.singleFork voice.section.talk voice.projects.preferences voice.talk voice.stage voice.cardstyles prose.app.pages prose.hygiene`. Result: 7 files, 131 tests passed. The prose ratchet did not go up. `scripts/related-tests.sh` lists 760 files for these paths, because Section and Projects are imported app-wide, so this is the voice and prose subset only.
- A throwaway jsdom check mounted all 8 `vstate`s, `ended` included, without an error: 8/8 passed. The file was deleted afterwards. So the blank ended frame was the browser crashing, not the code.
- `npx tsc --noEmit -p apps/swarm-ui`: clean.
- variants.patch applies cleanly to 217598f.

## Not verified
- Every dark frame, both 390 frames, and light working-open, resumed, failed-agent, ended and projects: none were rendered after the fixes.
- So F2 (ended), F3, F4, F5 at 390, F7, F8 and the working-open fix are fixed in code but unseen.
