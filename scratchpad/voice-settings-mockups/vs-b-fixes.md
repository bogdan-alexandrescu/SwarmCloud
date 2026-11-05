# Direction B (Cards and a timeline): fixes for the review (verdict NOT_YET, 14 findings)

Applied `variants.patch` from vs-b-bundle.tar.gz to a clean checkout of a4b0de3, then the bundle's unrendered `vs-b-fixes.patch`, then the fixes below. The new `variants.patch` in this bundle is the complete diff (App.tsx, Spine.tsx, paths.ts unchanged from the bundle; `src/voice/mock/VsB.tsx`, `vsb-css.ts`, `fixtures.ts` changed). Nothing committed, no pull request opened. Every frame was re-rendered, in light (`frames/`) and dark (`frames/dark/`).

| # | Finding | Fix | See |
|---|---|---|---|
| 1 | Machine spec clipped on every row, worst in -pending | The spec goes under the machine name (from vs-b-fixes.patch), and the 'now'/'next start' chips are gone (see 4). Swept the DOM in all 19 desktop states: no text is clipped or cut short with an ellipsis anywhere in `.vsb`. | all settings frames |
| 2 | Read-only draws a radio dot and a selected border | In read only the row is a plain `div.vsb-mach`: no dot, no `is-on` border | vs-b-settings-readonly |
| 3 | Read-only persona is Platform admin | Rendered with the `member` fixture persona: the sidebar reads "alice · Workspace admin" and the spine has no Admin entry | vs-b-settings-readonly |
| 4 | Pending drawn twice; chips 11.5px | Chips removed, and their CSS with them. The band is the only place the pending change appears. | vs-b-settings-pending |
| 5 | Heat-strip date labels 10.5px | `var(--t-micro)` (12px) | every telemetry frame with the strip |
| 6 | Latency axis and p90 labels 11px (SVG text scaled down) | The axis and p90 labels are now HTML over the chart at `--t-micro`; the SVG draws only bars and rules (`preserveAspectRatio="none"`) | vs-b-telemetry-live-session, -390 |
| 7 | -sessions is the same image as -live-idle | -sessions is now its own state: the 90-day range with 25 sessions (11 older fixture sessions added, MOCK-HOOK), a three-row strip, and the page scrolled to the list | vs-b-telemetry-sessions |
| 8 | Lowest heat level is close to an empty day in dark | 36/58/80% steps (from vs-b-fixes.patch), now rendered. In dark, l-1 against l-0 has a contrast of 1.67:1. | frames/dark/vs-b-telemetry-days |
| 9 | Missing states | New frames: vs-b-settings-key-unset, -applied, -stockout (no free L4 at the last start), -error (failed read, no cards) and vs-b-telemetry-error (failed read, no strip or rows) | captions in frames.json |
| 10 | Phone frames never rendered | vs-b-settings-admin-390 and vs-b-telemetry-live-session-390, light and dark. I also scrolled the phone Settings page down to Models and Behaviour to check them (not a frame): nothing overflows. | phone frames |
| 11 | One-time link text clipped | The link field takes the full row and wraps (`overflow-wrap: anywhere`) | vs-b-settings-key-link |
| 12 | Always-visible price explanation in the Machine card's footer | Footer removed. A "per hour" column label over the prices carries the (i) with the source text. | settings frames |
| 13 | Conflict head shows v14 while the band reports v15 | The head shows the newest version read: "v15 · alex, today 06:41" | vs-b-settings-conflict |
| 14 | Strip and row count talking minutes; cost counts machine minutes | Sessions get `machine_minutes`. The strip's metric is now "Machine minutes", the totals read "… machine min", and the row column is "Machine" (talking minutes in its tooltip). The detail still lists both. | telemetry frames |

## Verified
- `npx tsc --noEmit -p apps/swarm-ui`: exit 0, after the last edit.
- A computed-style sweep over every desktop state in the live browser: no text under 12px in `.vsb`, and nothing clipped. The one exception to the size rule is the italic "i" glyph inside the (i) button, at 10px: it is an icon, unchanged from before, and the review did not raise it.
- Two Sonnet reviewers read every PNG (Settings: 24 images; Telemetry: 18). Settings: no defects. Telemetry raised two points:
  - The p50/p90/max header in the session detail looked small. Checked: it is `--t-micro` (12px) and the caption is `--t-meta` (13px). No change.
  - In dark, an EMPTY day (l-0, the `--surface-2` slot) is only 1.1:1 against the panel. That was not one of the review's findings, which is about l-1 against l-0 (fixed, 1.67:1), so I left it. If wanted: an inset `--line` stroke on l-0.
- There are no unit tests for `src/voice/mock/`: no test file imports it. `scripts/related-tests.sh` lists most of the UI suite only because the bundle's hooks touch App.tsx and Spine.tsx, so I did not run it. CI was not run (no commit).

## Not done, and why
- The phone frames are 392x849, not 390x844. The live browser's `phone-390` preset reports that size, and the gate refuses setting the viewport directly. At that width the host app's document is 2px wider than its client area, on Talk and Preferences too, so the thin horizontal scrollbar at the bottom of the phone frames is not this direction's.
