# Visual QA and UI testing with SwarmCloud's browser agent types

Which browser agent type to use for which kind of visual QA or exhaustive UI
testing, and how to run the one workflow that sweeps the whole console:
[`docs/qa/console-qa.workflow.json`](qa/console-qa.workflow.json).

This is the user's half of [the visual-QA design](design/visual-qa.md) (§1.3,
§1.4 and §4.6; owner decisions of 2026-10-19 in its §7). The design keeps the
measurements, the costs and the alternatives that were turned down; this page
says what to pick and why. Read on 2026-10-20 at `87896ff`.

---

## 1. The agent types, and what each one sees

There are eight ways an agent can have a browser. They differ most in **what the
model sees**, which is what decides the job each one is for.

| | agent type | who drives | sees | reaches the fixture build on `localhost` | dark mode |
|---|---|---|---|---|---|
| **F1** | the scripted `browser` profile | a fixed list of actions you send; no model | pixels and text, as files; no model looks at them | no: `localhost` and private addresses are refused | no |
| **F2** | `claude-code-browser`, `browser_tool: playwright` | the model, through the Playwright MCP tools | pixels, the accessibility tree, the DOM | yes | yes |
| **F3** | `claude-code-browser`, `browser_tool: dev-browser` | the model, writing short scripts that run many steps per turn | as F2: it is the same browser | yes | yes |
| **F4** | `claude-code-browser`, `browser_tool: jev` | a second model, Jev, picks each step towards a goal | **page text and element labels only, never pixels** | yes | yes |
| **F5** | `swarm_browser.live()` from Python, on any `claude-code-browser` agent | a fixed, committed script, such as `python -m swarm_qa` | everything Playwright's Python API gives | yes | yes |
| **F6** | `claude-code-browser` where the deployment has no live browser | a headless Playwright the agent launches | anything Playwright can, at any size | yes | yes |
| **F7** | plain `claude-code` | the model, with no browser | **image files** it opens with `Read` | no browser to reach it with | — |
| **F8** | `claude-code-lightpanda` | the model, through the `lightpanda` MCP tools, or Python Playwright over CDP | **the DOM and its text, never pixels**; iframes not at all | not verified (see F8 below) | no |

What each is, in one paragraph:

**F1, the scripted `browser` profile.** Eight actions run in order (`goto`,
`click`, `fill`, `press`, `wait_for`, `wait`, `screenshot`, `extract`), at
most 200, at a viewport you choose. No model is called: the result is
screenshots and text files. It is the most repeatable of the eight, and it
reaches public `http(s)` pages only, with no sign-in, no dark mode and no
judgment. See [browser-scripted-runner.md](browser-scripted-runner.md).

**F2, the live browser with Playwright MCP.** `claude-code-browser` runs the
agent beside a headed Chromium that a person can watch from the console. The
agent clicks, types, snapshots the accessibility tree and takes screenshots.
A `browser_take_screenshot` with no filename is saved under
`$SWARM_ARTIFACTS_DIR/screenshots` and comes back to the model as an image.
**The size is a preset, never a resize**: `swarm-handoff device <preset>`
switches between `desktop` (1440×900), `iphone-15` (393×852), `phone-390`
(390×844), `phone-380` (380×800), `pixel-8` (412×915), `iphone-se` (375×667)
and `ipad-7` (810×1080),
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

**F8, Lightpanda on `claude-code-lightpanda`** (contract request 127;
[the design](design/lightpanda-runtime.md)). A headless browser that runs a
page's JavaScript and builds its DOM but **draws no pixels**, beside the agent
in its own image, `agent-runtime-lightpanda`, on the `standard` class. The
runner (`apps/agent-worker/agent_worker/lightpanda.py` and its branch in
`runners/claude_code.py`) gives the agent two ways in: the `lightpanda` MCP
server (`goto`, `markdown`, `links`, `tree`, `interactiveElements`, `click`,
`fill`, `press`, `waitForSelector` and the rest of `lightpanda mcp`'s tools),
and a CDP endpoint in `$SWARM_LIGHTPANDA_CDP` (`http://127.0.0.1:9223`) for
`playwright.chromium.connect_over_cdp(...)` from Python, **one connection per
page** because Lightpanda allows one browser context per connection. Three of
its MCP tools are denied to the agent: `screenshot` (its picture is text in a
default stylesheet, not the page, and it crashed the process on heavy pages),
`search` (it would call Brave with no key; the CLI's WebSearch serves) and
`getEnv`. There is **no live view, no screenshot, no hand-off, no recording,
no device preset and no theme**, and the console shows no live-browser status
for it, because the runner never writes `browser.json`. Two things read
differently from Chromium: an iframe's content is not loaded at all, and a
page's text includes text the page hides with CSS. Whether it reaches a
fixture build on `localhost` was not measured: Lightpanda runs with
`--block-private-networks`, so use F5 for the fixture build. What it buys is
cost: on the design's 17 pages it used a median 48 MiB against Chromium's
454 MiB and 0.30 against 4.61 CPU-seconds, and it holds a `standard` pod and
a `runner:claude-code-lightpanda` place, never a `browser` one.

### Device and theme are the agent's commands, not the caller's

A caller picks `runner_profile` and, for `claude-code-browser`, the
`browser_tool` by name, and nothing else (invariant 10). Which device and
which theme a page is shown at are commands the agent runs inside the agent:
`swarm-handoff device`, `swarm-handoff theme`, or `page.emulate_media(...)`
from `swarm_browser.live()`. The template below puts them in each step's
prompt.

### A flow is recorded with `record start` and `record stop`

`swarm-handoff record start <name>` launches a recorder in the background that
turns screenshots of the live browser into `$SWARM_ARTIFACTS_DIR/flows/<name>.gif`;
`swarm-handoff record stop` stops it and prints what it wrote. A recorder
otherwise stops only at its frame or size cap or after 15 minutes, so a script
that crashes before its `record stop` leaves one running. The next
`record start` does not refuse for that: it stops the earlier recorder as
`record stop` would (the stop file, at most 30 s, then SIGTERM), keeps its
GIF, names it in its answer as `replaced` with what it wrote in
`replaced_result`, and starts the new one (#1981). One recorder runs at a time;
started again under the same name, the new GIF is written over the old.

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

| **Reading pages that need no eyes**: docs, articles, a page's text or its links, many pages at once | **F8**: `claude-code-lightpanda`, its `markdown` and `links` tools, or Python Playwright over `$SWARM_LIGHTPANDA_CDP` with one connection per page | the DOM is the input; no pixels are needed, so no `browser` pod is held; ten pages at once peaked at 183 MiB against Chromium's 1,831 MiB in the design's §3.2 | F2-F5 hold a `browser` pod (and its pool place) to read text; F1 has no model |
| **Scraping and link checks** on public pages | **F8** | the same reason, and its links matched headless Chromium's on every one of the design's 17 pages | anything inside an iframe is missing: send those pages to `claude-code-browser` |
| **A form flow you check by its result**, not by how it looks: a field filled, a button pressed, the next page's text | **F8** (`fill`, `click`, `press`, `waitForSelector`) | 4 of 4 of the design's form flows passed under Playwright over CDP | not when the form is in an iframe, behind a sign-in or a CAPTCHA, or when what it looked like matters: that is F2 |

**Lightpanda or the live browser.** Pick `claude-code-lightpanda` (F8) when
the job is the DOM: reading, scraping, link checks, form flows judged by
their result. Pick `claude-code-browser` (F2-F6) for **anything that needs
pixels, a live view, a hand-off, an iframe or a sign-in**: a screenshot as
evidence, a person watching or taking control, content in an iframe (an
embedded widget, most third-party sign-in), a sign-in, a CAPTCHA or a consent
wall. Nothing moves an agent between the two for you (invariant 10: the
caller names the profile, and the lease was taken for that profile's pools).
Instead, the runner's prompt note (`LIGHTPANDA_NOTE`) tells a Lightpanda
agent that when a page fails there (an error, an empty page, content in an
iframe, a sign-in, a CAPTCHA, a consent wall, or anything it must see) it
does not work around it: it submits a child agent on `claude-code-browser`
through `$SWARM_CHILDREN` with those URLs, waits for it, and says in its
answer which pages went there and why. So a mixed job can start on F8 and
pay for a `browser` pod only for the pages that need one.

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
| `flow-submit-form` | `claude-code-browser`, `playwright`, `sonnet` | the submit form at desktop and at `phone-390`, at most 12 screenshots per preset, and what the model says each screenshot showed. It stops at 35 minutes, or at its first browser tool timeout, and bundles what it reached |
| `report` | `claude-code`, `opus` | waits for the captures and the reviews only; merges every finding into `findings.json` and writes `report.md`, with each finding's crop and the full-size screenshot it cites, and a check of every review's `reviewed` count against its capture's `manifest.json` (a missing tar is a finding about the run) |
| `flow-report` | `claude-code`, `opus` | waits for the three flows only; writes `flows.md` and `flows.json`: each flow's GIFs, whether its goal was reached, and what the model said it saw in each `flow-submit-form` screenshot. A failed flow costs this report, never `report` (visual QA run 2 lost its whole report to one flow; [design §8](design/visual-qa.md#8-what-the-runs-taught)) |

The six groups are the route list's: `overview-automate`, `work-a`, `work-b`,
`capacity`, `admin` and `task`. Every tab of the console's navigation is in
one of them, and `tests/unit/scripts/test_qa_routes.py` fails when a tab is
added without a route. `tests/unit/mcp/test_qa_workflow_template.py` fails
when a group has no capture and review step here, when a report misses a
review or a flow or waits on a step that is not its own, or when the file stops being a spec the platform takes.

### What you get

The report step's artifacts, in the console's Artifacts pane: `report.md`,
`findings.json` and every finding's crop PNG; the flow-report step's:
`flows.md`, `flows.json`, every flow's GIF and the screenshots it cites, shown
inline.
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
* **No keyboard pass.** The `a11y-keyboard` step was dropped by the owner on
  2026-10-21: in visual QA run 4 it went quiet for about 33 minutes, was
  killed at 2700 s with nothing bundled, and `report`, which waited for it,
  was cancelled over six finished reviews. The axe-core scan stays, in
  `capture-task`. `tests/unit/mcp/test_qa_workflow_template.py` fails if the
  step comes back.

### Comparing two runs

Within one run, the report compares 1440 with 390 and light with dark. To
compare two runs, open both reports in the console, or download two capture
tarballs and run `python -m swarm_qa diff A.tar B.tar --out D` in a
`claude-code-browser` agent: a changed-pixel ratio per image and a diff PNG
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

---

## 5. Running a repository's own Playwright suite

A step on either Claude profile can run the repository's own headless suite
(`npx playwright test`), with the Chromium revision that repository's
`@playwright/test` pins. BGD chose this on 2026-10-26 (fix (c) on #2253) after
three steps of one Product Oracle run could not. Neither image carries the
repository's revision: the step installs it, and Chromium's system libraries
and fonts are already in both images, so **never pass `--with-deps`**. The agent
is not root, so the flag can only fail on `su` after a download.

**On `claude-code`** (agent-runtime-base), run the commands as they are:

```bash
npx playwright install chromium   # into $XDG_CACHE_HOME/ms-playwright
npx playwright test
```

The worker points `XDG_CACHE_HOME` at the attempt's cache
(`apps/agent-worker/agent_worker/cachedirs.py`), which is writable and is never
checkpointed, so the revision (~170 MB) stays out of the agent's diff and out
of the 2 GiB checkpoint. A resumed attempt downloads it again. The image
carries the libraries that Playwright's own `install-deps chromium` list names
for Debian 12, plus fontconfig, Liberation and Noto Color Emoji. It does not
carry a browser
(`images/agent-runtime-base/Dockerfile`). Before #2253 the launch failed on a
missing `libglib-2.0.so.0`, and a renderer with no font fontconfig could find
died with `Skia FontConfig FATAL`. CJK and Thai fonts are not in the base
image, so a screenshot assertion over such text renders it as boxes there.

**On `claude-code-browser`**, prefix each command with `swarm-test-browsers`:

```bash
swarm-test-browsers npx playwright install chromium
swarm-test-browsers npx playwright test
```

The agent's own `PLAYWRIGHT_BROWSERS_PATH` is the live-browser stub
(BROWSER-ENFORCE, #1030), so a bare `npx playwright test` fails to launch,
and that is deliberate: the agent's own browsing goes through the live browser
the person watching sees. `swarm-test-browsers` runs that one command with
`PLAYWRIGHT_BROWSERS_PATH="$SWARM_TEST_BROWSERS_PATH"`. That is a writable
directory in the attempt's cache, seeded with links to the image's own
revisions, so a suite that pins the image's revision downloads nothing.
Calling a test runner without the wrapper, with
`PLAYWRIGHT_BROWSERS_PATH="$SWARM_TEST_BROWSERS_PATH" npx playwright test`, does
the same thing. The wrapper is the form the agent's note names. The design, and
what the exception does and does not relax, are in
[live-browser.md, "The test-runner exception"](design/live-browser.md#the-test-runner-exception-pw-testrunner-2253).

---

## 6. Rendering your own branch in another repository's implement step

This section is for lane authors in other repositories. A UI lane's implement
step can look at its own branch in a real browser and compare each render with
the repository's frame PNGs before it finishes. BGD decided this on 2026-10-26
(IMPLEMENT-RENDER, part of #2442), at the AI Studio session's request, and it
applies to **any** repository.

**Why.** AI Studio's ~30 visual-conformance lanes checked their UI fixes only
with rendered-HTML tests. None of them looked at a real render. After merge, a
browser re-check on staging found 28 of 52 desktop rows and 12 of 34 phone rows
still wrong, plus 6 new regressions. A rendered-HTML test checks the markup
you expected to write. It cannot see a token that resolves to the wrong
colour, a flex child that wraps at 390, or a dark theme that was never wired
up. A render compared with the frame can.

### 6.1 Put the implement step on `claude-code-browser`

Run **only the implement step** on `claude-code-browser`, with
`browser_tool: playwright`. The other steps stay on `claude-code`. A
`claude-code-browser` step holds a `browser` pod on GKE Autopilot for its whole
length, including the time the model spends thinking, so a review or a plan
step there costs a browser slot and gets nothing for it.

```json
{"step_id": "implement",
 "runner_profile": "claude-code-browser",
 "model_class": "opus",
 "input": {
   "browser_tool": "playwright",
   "prompt": "Fix the rows in conformance.md. Before you finish, render your branch: start the app (README: `make dev-noauth`), and for every screen you changed take desktop light, desktop dark, phone-390 light and phone-390 dark, compare each with its frame in design/frames/ using `swarm-handoff compare`, fix what the diff shows, and compare again. In your summary, list every frame you compared with its score."}}
```

The prompt names the frames' directory, the command that starts the app
without sign-in, and the four renders. The agent cannot guess the first two,
and if the prompt does not ask for the renders, the step will not do them.

### 6.2 Start the app inside the step, on 127.0.0.1

The app runs in the step's own container, from the step's checkout: the branch
being implemented, not a deployment. The live Chromium runs in the pod's
display sidecar, which shares the pod's network namespace, so it reaches the
agent's `127.0.0.1` directly. Chromium also sends loopback traffic direct
when its other egress goes through a proxy. Use the repository's own dev
server, bound to `127.0.0.1`, started in the background, with its output in a
log file:

```bash
# a FastAPI backend, if the frontend needs one
(cd backend && uv sync && nohup uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 > /tmp/api.log 2>&1 &)
# a Next.js frontend
(cd web && npm ci && nohup npm run dev -- --hostname 127.0.0.1 --port 3000 > /tmp/web.log 2>&1 &)
# wait until a path that answers 200 does: curl retries itself, so no sleep loop is needed
curl -sf --retry 60 --retry-delay 2 --retry-connrefused http://127.0.0.1:3000/ > /dev/null
```

* **Point the frontend at the backend on loopback** with whatever the
  repository already reads, for example `NEXT_PUBLIC_API_URL=http://127.0.0.1:8000`
  or a dev rewrite. The backend's CORS settings must allow
  `http://127.0.0.1:3000`, or the frontend must proxy it.
* **Open the page in the LIVE browser**, never in a Chromium of your own. The
  runner refuses that launch: `chromium.launch()`, headless or not, fails by
  design, because the person watching on the console would see none
  of the work (live-browser.md §3.2). To drive the browser, use the
  `browser_*` tools or `from swarm_browser import live`. A repository's own
  headless suite is the one exception, and it runs under
  `swarm-test-browsers` (§5).
* **Next.js and `127.0.0.1`.** A Next.js dev server may treat
  `127.0.0.1` and `localhost` as different origins for its `/_next/*` dev
  resources. Newer versions warn about this, and versions that enforce
  `allowedDevOrigins` load the page without its scripts. If that happens, add
  `127.0.0.1` to `allowedDevOrigins` in the repository's `next.config`, or
  open the host the dev server printed.
* `swarm-handoff serve <dir>` is for a static directory, such as a mockup or
  the frames themselves. It is not for the app.

### Sign-in is never typed by an agent

An agent never types a password, a one-time code or a recovery key into a
sign-in page, and a lane cannot sign in to Keycloak, Auth0, Cloudflare Access
or any other identity provider on its own. **A repository whose app sits
behind Keycloak or something like it must offer a fixture or a no-auth dev
mode** before a lane can render it. Examples:

* an environment flag the dev server reads, such as `AUTH_MODE=fixture`,
  that swaps the auth provider for a fixed fake user and role;
* a mock-service-worker or fixture build, as this repository's console does
  with `npm run dev` (fixture data, no network);
* a backend dependency override that returns a fixed principal in dev.

Whatever form it takes, it must be off in every deployed build. The repository
owns it and documents it in its README, and the lane's prompt names it. A
hand-off to a person (`swarm-handoff request`) does not replace it: the lane
parks until someone comes, the session is not kept across a park, and on a
step that runs unattended nobody comes. The way through Cloudflare Access is a
service token that the platform adds and the agent never sees. That is lane
CF-ACCESS-TOKEN of #2442, not this one.

### 6.3 Render the four combinations

For each screen you changed, render four combinations: desktop light, desktop
dark, phone light and phone dark. The size is a preset and the theme is a
command. Both are the gate's, and both hold across navigation and new tabs:

| render | commands |
|---|---|
| desktop light | `swarm-handoff device desktop` · `swarm-handoff theme light` |
| desktop dark | `swarm-handoff device desktop` · `swarm-handoff theme dark` |
| phone light | `swarm-handoff device phone-390` · `swarm-handoff theme light` |
| phone dark | `swarm-handoff device phone-390` · `swarm-handoff theme dark` |

Use `phone-380` instead of `phone-390` when the repository's phone frames
are drawn at 380. It is 380×800, the frame AI Studio draws its phone mock-ups
in (PHONE-380), with `phone-390`'s scale and user agent. `desktop` is 1440×900. `browser_resize` is refused:
the gate owns the size.

Take each render to a known path, so that it can be compared:

```bash
mkdir -p "$SWARM_ARTIFACTS_DIR/render"
swarm-handoff device phone-390 && swarm-handoff theme dark
python - <<'PY'
import os
from swarm_browser import live
playwright, browser, context = live()
page = context.new_page()                      # a tab of its own, which the person sees
page.goto("http://127.0.0.1:3000/settings", wait_until="networkidle")
page.screenshot(path=os.environ["SWARM_ARTIFACTS_DIR"] + "/render/settings--phone-390--dark.png")
page.close()
playwright.stop()                              # never browser.close(): the live browser stays up
PY
```

A `browser_take_screenshot` with no filename is also saved, under
`$SWARM_ARTIFACTS_DIR/screenshots`, and the model sees it. The script above
puts the PNG at a name the compare step can use.

**Take the viewport, never the full page, at any preset.** Through the gate
a full-document shot is not the page as laid out, at any preset. On a phone,
`swarm_qa` measured a first shot at phone-390 of 980×2121 with the page drawn
small (`swarm_qa.py`, `_shot`). On the desktop too: a `full_page=True` shot at
1440 of a 300-item list (epic #1981, comment 7, the visual-QA run's
`flows/10c-long-300-fullpage--1440.png`) repeats the fixed banner and title
at its bottom. The likely cause, not traced: the gate refuses the metrics
override a full-page shot uses to grow the viewport to the document, so
Chromium draws past the viewport it has and draws fixed and sticky content
again where it does not belong.
For a long page, take segments: `swarm_qa capture`, which scrolls and shoots
viewport by viewport, or scroll and take one viewport shot per stop yourself.
Compare a viewport with `--top-only` (below). (The 1440 shot above is the
measurement; it was not re-taken through a live gate when this paragraph
changed.)

### 6.4 Compare each render with its frame

```bash
swarm-handoff compare "$SWARM_ARTIFACTS_DIR/render/settings--phone-390--dark.png" \
    design/frames/settings--phone-390--dark.png
```

It prints one JSON line and writes two files under
`$SWARM_ARTIFACTS_DIR/compare/`: `<name>.diff.png`, which is the render faded
with every differing cell in magenta, and `<name>.json`, the report. The name
defaults to the render's file name, and `--name` sets it. Exit codes:

* **0**: the score is within the threshold;
* **1**: the score is over the threshold;
* **5**: the input was refused (an unreadable image, an image too large to
  score, a bad name, or no `$SWARM_ARTIFACTS_DIR`).

How it scores, and why that way (the full reasoning is in the command's own
docstring, `images/agent-runtime-browser/swarm-handoff`):

* **The score is perceptual, not a pixel count.** A design tool and Chromium
  never draw a glyph's edge the same way, so a pixel-exact ratio would call
  every frame different. Both images are averaged into cells of 4×4 of the
  render's CSS pixels, and each pair of cells is compared in CIELAB. A cell
  differs when its colour difference (ΔE) is over 6, which is plainly visible.
  The score is the share of cells that differ: **0 for the same picture**.
  The default threshold is 0.02, about one misplaced line of body text across
  a desktop. `--threshold 0` asks for a match with no differing cell.
* **A frame exported at 2× or 3× is fine.** It is averaged over 2× or 3× the
  pixels, so it lands on the same grid as the render.
* **Transparency is seen over white.** A frame's empty canvas is transparent,
  and the browser paints white.
* **Heights must agree, or you must say they need not.** Rows that only one
  image has count as different, so a one-screen render cannot pass for a
  whole-page frame. `--top-only` scores only the rows both images have, and
  reports how many it left out.
* **Pillow does the decoding** when it is available. It is pinned in the
  image's venv, which also lets it read palette, 16-bit and JPEG frames.
  Without it, a standard-library decoder reads what Chromium writes.
* **Neither image is scored at more than 6 M pixels.** The scoring is pure
  Python, and this lane's first attempt to finish spent two hours of four
  cores before its runner timed out. Pillow shrinks a larger image by a whole
  factor first (a 2880×8000 frame is scored at 1440×4000); without Pillow it
  is refused as `too_large`, from its header, before anything is
  decompressed. A 2× export of a 1440×900 frame (5.2 M) is under the cap.
  Measured 2026-10-26: 1.3–1.6 s of CPU for a desktop render against its 2×
  frame, 0.3–1.2 s for a phone, and 5.3 s for a 1440×4000 page against its
  2× frame with Pillow.

### 6.5 Fix, then finish, and name the frames

Open each diff image with `Read`. The magenta cells are where the render and
the frame disagree. Fix them, render again at the same preset and theme, and
compare again. Finish when every frame you touched is within the threshold.
If a frame cannot be matched, because the frame is wrong or because of a font
the container lacks, say so for that frame.

The summary names every frame compared, with its preset, theme, score and
diff image:

```text
Rendered and compared (swarm-handoff compare, threshold 0.02):
- design/frames/settings--desktop--light.png    desktop   light  0.004  compare/settings--desktop--light.diff.png
- design/frames/settings--desktop--dark.png     desktop   dark   0.011  compare/settings--desktop--dark.diff.png
- design/frames/settings--phone-390--light.png  phone-390 light  0.000  compare/settings--phone-390--light.diff.png
- design/frames/settings--phone-390--dark.png   phone-390 dark   0.019  compare/settings--phone-390--dark.diff.png (--top-only, 120 rows left out)
```

The diff images and reports are in the step's artifacts, so whoever reviews
the PR, or the next visual-QA step, can check the claim without rendering
anything again.
