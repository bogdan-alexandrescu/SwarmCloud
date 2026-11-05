# vt-a fixes: direction A (Stage), round fix-a

Verdict was NOT_YET with ten findings. The bundle's variants.patch was applied to a clean checkout
(a4b0de3) and every finding below was fixed; nothing else was changed. All 20 frames were re-rendered
(light and dark), because the bar and the visuals' type touch every one of them. No commit, no pull request.

| # | Finding | Fix | Frames |
|---|---|---|---|
| 1 | §9.2 states not drawn: thinking, reconnecting, engine down, read-back refused and expired, budget at the cap, the phases `updating speech server` / `fetching model` | Seven new scenes and frames. **vt-a-thinking**: the sentence is final, the ring pulses quietly, status `thinking`. **vt-a-reconnecting**: the ring spins in the warning colour, status `reconnecting` in warning ink, Mute and Push to talk held (disabled, drawn faint), Stop live. **vt-a-engine-down**: the session goes on in text mode mid-session (field, Send, Stop; engine `this browser`; its tooltip names the cause). **vt-a-readback-refused**: a spoken "no" marks the card `Not that` and quotes it; the agent says "Left it running." **vt-a-readback-expired**: the card greys, its clock shows 0s and `expired` (tooltip: nothing ran), and it stays. **vt-a-budget-cap**: the budget line red at $100.00 / $100.00, the ring back to Start, status `budget cap` with the reason (session ended, machine stopped) in its tooltip. **vt-a-starting-fetch**: `starting machine 0:38` and `updating speech server 1:12` ticked, `fetching model qwen3-asr-0.6b` in progress | new: thinking, reconnecting, engine-down, readback-refused, readback-expired, budget-cap, starting-fetch |
| 2–3 | vt-a-no-l4 (light, dark): a live Stop ring and live Mute and Push to talk in text mode, and nowhere to type | Text mode draws no voice ring and no Mute or Push to talk: a "Say to Voice it" field, Send and Stop stand at the bottom centre where the ring was (Send really adds the words to the captions). The status word and the `this browser` engine tick stay beside it | no-l4, engine-down |
| 4 | vt-a-no-l4: shows $14.20 spent, a mock-up with pins and an earlier spoken caption, though the stock-out happens at Start | The scene is now a fresh session at Start: $0.00 / $100.00, no visual, no captions, only the one line "No L4 free in us-central1-a." with [Text] (which only dismisses, §4.7) and the text-mode bar | no-l4 |
| 5–6 | vt-a-speaking: diagram text about 10–11 px | Diagram labels redrawn larger (node titles, sub-labels and the zone label all `--t-title`, 18 px in the sheet; nodes widened to 240) and the sheet now refuses to scale its smallest text under 12 px. Measured on render: smallest diagram text 12.58 px at k 0.699 | speaking, gptlive, budget-80, reconnecting, engine-down, budget-cap |
| 7–8 | vt-a-hearing-390: mock-up text about 4–5 px, no zoom | The sheet scales to fit only down to the 12 px text floor; below that it keeps the floor and the stage pans (both scrollbars draw) in a square window on the phone, opened at the start of the region being talked about (the filter row). Measured: 12.0 px at k 0.857 | hearing-390 |
| 9 | 13 px mock-up chrome and chip, 13 px code header, 16 px log head land at 11–12 px | The mock-up's chrome title and chip and the code header move to `--t-body` (14 px); the log head to `--t-title` and its step name to `--t-figure`; the log body to 1.3 em. Measured: mock-up 12.04 px (k 0.86), code 12.65 px (k 0.903), log 12.58 px (k 0.699) | hearing, thinking, visual-mockup, visual-code, note-adding, readback-* |
| 10 | vt-a-starting: the engine tick reads as a third phase | The engine tick is now beside the status words, not under them: to the right of the phases or status, set off by a thin rule, and aligned to the last line (on the phone, where the column is narrow, it stays under the status without the rule) | every desktop frame |

## Verified

* Text floor, measured in the browser for every frame and theme (the smallest text node in the sheet times its
  scale): minimum 12.0 px (hearing-390), every desktop frame 12.04 px or more; only hearing-390 pans.
* `npx tsc --noEmit -p .` in apps/swarm-ui: exit 0.
* `npx vitest run --pool=forks --poolOptions.forks.singleFork` on voice.section.talk, voice.talk,
  voice.talk.text, voice.cardstyles, voice.preferences, letterspacing, sections.stylesheet and
  phone.chrome: first run 1 failure, `sections.stylesheet.test.ts` refused my raw `18px`/`20px`
  font sizes; I moved them to the `--t-title` token and re-ran it: 301 passed. The other seven files: 384 passed.
  `scripts/related-tests.sh --base HEAD` lists 639 files (Section.tsx and a new stylesheet reach the whole
  suite); those were not run.
* Every frame looked at in light and dark: four Sonnet reviewers read all 40 PNGs against the direction,
  the honesty rules and these findings. They flagged two things, both fixed and re-checked by eye: the held
  Mute and Push to talk in reconnecting looked live (now drawn faint), and the phone's pan cut the filter
  row on both sides (now opens at the row's start). Every other frame was ok.
* `variants.patch` applies to a clean a4b0de3 (`git apply --check`).

## Not verified

* The phone frame pans: pins on the right half of the mock-up are a pan away (the tab still counts them).
  No zoom control was added, as no finding asked for one.
* eslint could not run here (no flat config resolves for a single-file run).
