# Visual QA and UI testing with SwarmCloud's browser task types

Which browser task type to use for which kind of visual QA or exhaustive UI
testing, and how to run the one workflow that sweeps the whole console:
[`docs/qa/console-qa.workflow.json`](qa/console-qa.workflow.json).

This is the user's half of [the visual-QA design](design/visual-qa.md) (§1.3,
§1.4 and §4.6; owner decisions of 2026-10-19 in its §7). The design keeps the
measurements, the costs and the alternatives that were turned down; this page
says what to pick and why. Read on 2026-10-20 at `87896ff`.

---

## 1. The task types, and what each one sees

There are seven ways a task can have a browser. They differ most in **what the
model sees**, which is what decides the job each one is for.

| | task type | who drives | sees | reaches the fixture build on `localhost` | dark mode |
|---|---|---|---|---|---|
| **F1** | the scripted `browser` profile | a fixed list of actions you send; no model | pixels and text, as files; no model looks at them | no: `localhost` and private addresses are refused | no |
| **F2** | `claude-code-browser`, `browser_tool: playwright` | the model, through the Playwright MCP tools | pixels, the accessibility tree, the DOM | yes | yes |
| **F3** | `claude-code-browser`, `browser_tool: dev-browser` | the model, writing short scripts that run many steps per turn | as F2: it is the same browser | yes | yes |
| **F4** | `claude-code-browser`, `browser_tool: jev` | a second model, Jev, picks each step towards a goal | **page text and element labels only, never pixels** | yes | yes |
| **F5** | `swarm_browser.live()` from Python, on any `claude-code-browser` task | a fixed, committed script, such as `python -m swarm_qa` | everything Playwright's Python API gives | yes | yes |
| **F6** | `claude-code-browser` where the deployment has no live browser | a headless Playwright the agent launches | anything Playwright can, at any size | yes | yes |
| **F7** | plain `claude-code` | the model, with no browser | **image files** it opens with `Read` | no browser to reach it with | — |

What each is, in one paragraph:

**F1, the scripted `browser` profile.** Eight actions run in order (`goto`,
`click`, `fill`, `press`, `wait_for`, `wait`, `screenshot`, `extract`), at
most 200, at a viewport you choose. No model is called: the result is
screenshots and text files. It is the most repeatable of the seven, and it
reaches public `http(s)` pages only, with no sign-in, no dark mode and no
judgment. See [browser-scripted-runner.md](browser-scripted-runner.md).

**F2, the live browser with Playwright MCP.** `claude-code-browser` runs the
agent beside a headed Chromium that a person can watch from the console. The
agent clicks, types, snapshots the accessibility tree and takes screenshots.
A `browser_take_screenshot` with no filename is saved under
`$SWARM_ARTIFACTS_DIR/screenshots` and comes back to the model as an image.
**The size is a preset, never a resize**: `swarm-handoff device <preset>`
switches between `desktop` (1440×900), `iphone-15` (393×852), `phone-390`
(390×844), `pixel-8` (412×915), `iphone-se` (375×667) and `ipad-7` (810×1080),
and `browser_resize` is refused, so the person watching sees the device the
page is laid out for. `swarm-handoff theme dark|light|system` sets the colour
scheme every page is told to prefer, across navigations and new tabs. Every
attempt holds a `browser` pod for its whole length, including the time the
model spends thinking.

**F3, dev-browser.** The same live browser, driven by scripts the agent writes.
One script does what would otherwise be twenty tool calls, so it is the
cheaper choice for a long, repetitive flow. It always attaches to the live
browser and cannot start one of its own.

**F4, Jev.** A goal ("open the newest failed task's logs") is handed to Jev,
which decides each step by asking yes/no and pick-one questions about the
page's **text and element labels**. Screenshots are never sent to it. So Jev
can tell you whether a person can get from A to B and where a label misled
it, and **cannot** tell you that a button is clipped, a colour is wrong or a
column overflows at 390 px: it never sees the pixels that show those. Its
judge is the workspace's `judgment_backend`: the internal decision API (no
key) or TypeSafe (the workspace's TypeSafe key).

**F5, a fixed script on the live browser.** `from swarm_browser import live`
connects Python Playwright to the same browser. A committed script gives the
scripted runner's repeatability with the live browser's reach: `localhost`,
dark mode, the accessibility tree, axe-core. `python -m swarm_qa capture` is
this, for the console.

**F6, no live browser.** Only where a deployment does not run the live-browser
sidecar (prod today, never dev). The agent launches headless Chromium itself,
at any size. You cannot ask for it: it is a deployment setting.

**F7, plain `claude-code`.** No browser at all, which makes it the cheapest
place to **look at** screenshots: Claude Code's `Read` tool shows an image file
to the model, and a review step receives the PNGs from an earlier step with
`input_from`. It cannot start a browser reliably; the image carries none.

### Device and theme are the agent's commands, not the caller's

A caller picks `runner_profile` and, for `claude-code-browser`, the
`browser_tool` by name, and nothing else (invariant 10). Which device and
which theme a page is shown at are commands the agent runs inside the task:
`swarm-handoff device`, `swarm-handoff theme`, or `page.emulate_media(...)`
from `swarm_browser.live()`. The template below puts them in each step's
prompt.

---

## 2. Which one for which job

| job | use | why | not |
|---|---|---|---|
| **A pixel sweep**: every page at 1440 and 390, light and dark, the same way every time | **F5**: `python -m swarm_qa capture` on `claude-code-browser`, against the fixture build served in the same pod, on `haiku` | the only flavour on dev that reaches `localhost`, sets dark mode and takes the same images every run, so two runs can be compared | F1 cannot reach the fixture build or set a theme; F2-F4 put a model in charge of each click; F7 has no browser |
| **Looking at the screenshots**: layout, overflow, truncation, contrast | **F7**: plain `claude-code` on `opus`, reading the capture's PNGs | the model sees the same pixels without holding a `browser` pod while it thinks, and review is the long part | reviewing inside F2 holds a browser slot and a sidecar for the whole review |
| **Can a person get from A to B** (goal-driven flows) | **F4 (Jev)** for the goal, recorded as a GIF with `swarm-handoff record` | Jev is built for goals and judges on text, which is what a person reads to find their way | F1 has no judgment; F5 is a fixed script, the opposite of goal-driven |
| **A form, step by step**, with what the model saw at each step | **F2** (Playwright MCP) or **F3** | screenshots come back to the model as images, so it can say what each one showed | F4 never sees the screenshot |
| **Long pages and content** | **F4** `browser_read`, **F2** `browser_snapshot`, or **F1** `extract` for a public page | text is the right input, and reading it costs less than an image | a screenshot of a long page is downscaled until its text is unreadable |
| **Comparing with a mockup or another capture** | **F5** captures, then `python -m swarm_qa diff A.tar B.tar`, then F7 on the diff images | a comparison means something only when both sides were captured the same way. A mockup is served over `http` with `swarm-handoff serve <dir>` (`file://` is blocked) | a model-driven capture differs run to run for reasons that are not regressions |
| **Accessibility** | **F2** / **F5**: keyboard-only navigation, the accessibility tree, emulated vision deficiencies and reduced motion, and axe-core (`swarm_qa capture --axe`) | the live browser passes the accessibility, input and media-emulation commands | F1 sees no tree; F4 sees labels but cannot measure contrast or focus order |
| **Signed-in pages of a real console** | not in the template yet; see §4 | — | — |

**Jev runs only against the fixture build** (owner, 2026-10-19). On the fixture
build the page text is made-up data, so sending it to Jev's judge sends
nothing real. On a real console, use `browser_tool: playwright`.

---

## 3. Running the console QA workflow

[`docs/qa/console-qa.workflow.json`](qa/console-qa.workflow.json) is a
workflow spec: submit it with `swarm workflow docs/qa/console-qa.workflow.json`
or with `/sc:swarmcloud` and `swarm_workflow_spec`. It runs **on demand**
(owner, 2026-10-19): when you ask for it, or when a lane changed
`apps/swarm-ui` and wants a look. It is **report only**: it changes no file,
opens no pull request and files no issue.

To test a branch rather than `main`, change `repository_ref`; nothing else.

### What it runs

| steps | profile, browser tool, class | what each does |
|---|---|---|
| `capture-<group>`, one per group in [`apps/swarm-ui/qa/routes.json`](../apps/swarm-ui/qa/routes.json) | `claude-code-browser`, `playwright`, `haiku` | starts the fixture build (`npm run dev` in `apps/swarm-ui`, no network) and runs `python -m swarm_qa capture` for its group at `desktop` and `phone-390`, `light` and `dark`. Writes `capture-<group>.tar`: the PNGs, each page's text, ARIA snapshot and console errors, and `manifest.json`. `capture-task` also runs axe-core |
| `review-<group>` | `claude-code`, `opus` | opens every PNG in its group's capture and writes `findings-<group>.json` (one entry per defect, with a crop PNG) and `review-<group>.tar`. `reviewed` is the number of images it opened |
| `flow-find-failure`, `flow-workflow-graph` | `claude-code-browser`, `jev`, `sonnet` | a goal, reached as a person would, at desktop and then at `phone-390` or in dark mode; a GIF of each run under `flows/` |
| `flow-submit-form` | `claude-code-browser`, `playwright`, `sonnet` | the submit form at desktop and at `phone-390`, a screenshot at each step, and what the model says each screenshot showed |
| `a11y-keyboard` | `claude-code-browser`, `playwright`, `sonnet` | keyboard-only navigation of every section, unnamed controls, reduced motion and deuteranopia |
| `report` | `claude-code`, `opus` | merges every finding into `findings.json` and writes `report.md`, with each finding's crop, each flow's GIF, and a check of every review's `reviewed` count against what its capture should have produced |

The six groups are the route list's: `overview-automate`, `work-a`, `work-b`,
`capacity`, `admin` and `task`. Every tab of the console's navigation is in
one of them, and `tests/unit/scripts/test_qa_routes.py` fails when a tab is
added without a route. `tests/unit/mcp/test_qa_workflow_template.py` fails
when a group has no capture and review step here, when the report misses a
review or a flow, or when the file stops being a spec the platform takes.

### What you get

The report step's artifacts, in the console's Artifacts pane: `report.md`,
`findings.json`, every finding's crop PNG and every flow's GIF, shown inline.
Each capture's and review's tarball stays on its own step.

A finding is reported, not filed. To file them, follow CLAUDE.md's issue
rules: one `[epic]` issue for the run, one comment per finding.

**A clean report is not a sweep that saw nothing.** The report compares each
review's `reviewed` count with the images its capture produced, and a route
that failed to capture is itself a finding. A report with no findings and no
shortfall means every image was opened.

### What it costs and how long it takes

Estimates from the design (§4.7), not measurements: about **$16-26** a run,
most of it the six `opus` reviews, and about **40-55 minutes** end to end.
It uses at most 10 `browser` pods and 7 `standard` pods at once, which fits a
tenant's default pool.

### Why it is built this way

* **Capture and review are separate steps.** A capture is a script and takes
  minutes on a `browser` pod; a review is a model thinking for half an hour,
  which needs no browser. Splitting them keeps the browser pods short.
* **Captures are scripted, not driven by a model.** Two captures can only be
  compared if they were taken the same way: same device, same theme, same
  scroll positions, the clock frozen. A model screenshots whatever it thought
  to, whenever it got there.
* **`haiku` captures, `opus` reviews and reports, `sonnet` runs flows.** The
  capture agent runs one command; the review is the product; a flow is mostly
  tool calls whose judgment Jev or the screenshot carries.
* **`on_step_failure: continue`**, so one flow that fails does not cancel the
  captures. Every capture writes its tarball even when a route fails, because
  the review stages it by name and a missing file would fail the review.
* **No `resource_class`** anywhere: each profile's own is the right one.

### Comparing two runs

Within one run, the report compares 1440 with 390 and light with dark. To
compare two runs, open both reports in the console, or download two capture
tarballs and run `python -m swarm_qa diff A.tar B.tar --out D` in a
`claude-code-browser` task: a changed-pixel ratio per image and a diff PNG
where it is over the threshold. No step can read an earlier workflow's
artifacts yet, so the template does not do this itself (owner, 2026-10-19:
compare within one run for now).

---

## 4. What the template does not do yet

* **Signed-in pages of a real console.** The template stays on the fixture
  build. The owner chose a QA service account admitted through IAP
  ([qa-identity.md](design/qa-identity.md)); the catalogue declares
  `claude-code-browser`'s `qa_identity` input for it, but on 2026-10-20 nothing
  on main reads that input, so it signs nothing in. Once it does, the signed-in
  arm is one more group of steps against the dev console, with `playwright`
  for capture and for flows (never Jev on a real console), and never on an
  admin identity.
* **Prod.** Prod has no live browser, so every `claude-code-browser` step there
  would run without one, and `swarm_qa capture` refuses to run without it (exit
  2), rather than capture at another size.
* **A schedule.** It runs on demand. A weekly or nightly sweep is the owner's
  call (design §7 Q6).
