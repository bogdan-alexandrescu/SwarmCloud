# Visual QA: which browser task type serves which job, and one workflow that sweeps the console

**Status: design, 2026-10-19 (lane QA-GUIDE, part of #1649). Nothing here is
built.** The owner reads it and picks the answers in §7 before any build lane
starts. Its frozen-contract half (§5) is empty for the recommended design and
holds two requests for the alternative, written as requests here and not in
[`docs/contract-change-requests.md`](../contract-change-requests.md); the
operator files them there only once they are accepted (CLAUDE.md rule 1).

Owner request, 2026-10-19, verbatim:

> "figure out how the different types of browser tasks can be used for visual
> QA runs and exhaustive testing. For example what would jev browser be used
> for vs the other flavors we currently support."

Every `file:line` below was read on 2026-10-19 at `2af9bed`. Lines move; the
symbol named beside each one is what to search for when they have. Nothing was
run to write this: every behaviour is read from the code, and where the code
cannot say (an upstream default, a time nobody measured) the text says
**unverified** or **estimate**, with the arithmetic.

What the design settles, in one line each:

* **There are seven browser flavours, not three**, and they see different things:
  the scripted `browser` runner sees pixels and text and no model looks at
  either; the live browser's Playwright MCP and `swarm_browser.live()` see
  pixels, DOM and the accessibility tree; dev-browser drives the same browser
  from scripts; **jev sees page TEXT and element labels only, never pixels**
  (§1.3).
* **Jev is for goal-driven flows and reading, not visual QA.** "Can a person
  find the failed task and open its logs" is a jev job; "is this button
  clipped at 390 px in dark mode" is not, because jev's judge never receives
  the pixels that show it (§1.3 F4, §1.4).
* **The recommended QA run splits capture from review** (§3.2): short,
  scripted captures on the live browser against the console's fixture build,
  served inside the same pod; long model reviews on the cheap `claude-code`
  pool reading the PNGs; flows on the live browser with jev or Playwright; one
  report step. It needs **no frozen-contract change** (§5).
* **The scripted `browser` runner cannot test the console today**: it refuses
  `localhost` and private addresses, the deployed console is behind IAP, and it
  has no dark mode. It stays the right tool for public pages, and becomes the
  cheapest console capture only if a fixture build is hosted and a
  `color_scheme` input is added (§3.3, §5).
* **The workflow template (§4.6) waits on QA-KIT, QA-FLOWS and BROWSER-START,
  and on one new lane, QA-CAPTURE**; QA-IDENTITY gates only the authenticated
  arm, which the template leaves out (§4.7).

---

## 1. The problem, as measured

### 1.1 What #1649 measured

The issue's survey of `origin/main` `7c2e148` (2026-10-19) found seven gaps.
Each is restated with what this lane read at `2af9bed`:

1. **The live browser refuses arbitrary sizes.** The device is the gate's, set
   from five named presets (`apps/agent-worker/agent_worker/livegate/presets.py:137-151`,
   `PRESETS`): `desktop` 1440×900 @1, `iphone-15` 393×852 @3, `pixel-8`
   412×915 @2.625, `iphone-se` 375×667 @2, `ipad-7` 810×1080 @2. None is 390.
   The agent's own `Emulation.setDeviceMetricsOverride`, `setVisibleSize`,
   user-agent and touch overrides are refused
   (`livegate/cdppolicy.py:152-158`, "the device is the gate's, by preset
   name"), so Playwright's `page.set_viewport_size()` fails through the gate.
   The agent can switch preset with `swarm-handoff device PRESET`
   (`images/agent-runtime-browser/swarm-handoff:198-226`, `cmd_device`), but the
   note it is given (`runners/claude_code.py:320-346`, `LIVE_BROWSER_NOTE`)
   never says so.
2. **Dark mode is possible and undocumented.** `Emulation.setEmulatedMedia` and
   `Emulation.setAutoDarkModeOverride` pass the gate
   (`cdppolicy.py:285`, `:293`), so `page.emulate_media(color_scheme="dark")`
   works from `swarm_browser.live()`. No note, preset, test or console control
   mentions it. The scripted runner has no theme input at all
   (`apps/common/swarm_common/profiles.py:1278-1306`, `_BROWSER_INPUTS`).
3. **An agent has no identity for an IAP app.** The console is behind IAP
   (`apps/swarm-ui/README.md`, "IAP sits in front"). A sign-in is a hand-off: a
   person types it, and a hold nobody watches ends after
   `human_unwatched_seconds = 60` (`apps/swarm-api/swarm_api/settings.py:585`).
   Saved logins are personal-workspace only
   (`swarm_api/browser_logins.py:110`, `BROWSER_LOGIN_PERSONAL_ONLY`) and
   untested against Google. `terraform/environments/prod/prod.tfvars` never
   sets `enable_live_browser`, so prod has no live browser.
4. **That the model sees its screenshots is upstream's default.** Playwright
   MCP is `@playwright/mcp@0.0.83` (`images/agent-runtime-browser/Dockerfile:148-152`),
   started with `--cdp-endpoint` and nothing else
   (`runners/claude_code.py:730`). No flag in the repository sets image
   responses, and no test checks that `browser_take_screenshot` reaches the
   model as an image. Nothing saves screenshots to artifacts automatically: the
   note only asks the agent to (`claude_code.py:344-346`).
5. **No GIF or flow capture for a report.** The live view is recorded as WebM
   segments for the console (`docs/acceptance.md:353`), not as a per-flow file
   an agent can attach. `Page.startScreencast` is allowed through the gate
   (`cdppolicy.py:237`); nothing turns it into a GIF.
6. **`file://` is blocked**, so a mockup cannot be opened beside the app:
   `images/agent-runtime-browser/chromium-policy.json:2-9` (`URLBlocklist`).
   The console's mockups are HTML files in the repository
   (`docs/web-ui/mockups/*.html`, nine of them).
7. **13% of attempts started without the live browser** (#1366): 8 of 60 on
   benchmark devb-1015 (`docs/design/live-browser.md:696-735`, "NOLIVE-GUARD").
   NOLIVE-GUARD now fails such an attempt retryably (`live_gate_unavailable`)
   rather than letting half of them fall back to a headless browser; the rest
   of #1366 is lane BROWSER-START.

And the record of how the console's own visual QA ran: #1038 produced at
least 111 numbered findings (`V001`..`V111` in
`apps/swarm-ui/src/__tests__/vqa.l11.workflow.test.tsx:1-20`) and #1427
re-ran it on 2026-10-16 ("measured in headless Chromium",
`people.vqa2r4.test.tsx:1-8`). 27 test files under
`apps/swarm-ui/src/__tests__/` pin those findings (`*vqa*`). The issue says
both runs used the plain `claude-code` profile against the fixture build. This
lane could not confirm how Chromium got there: `agent-runtime-base` has no
Chromium (`profiles.py:1701`; its apt list,
`images/agent-runtime-base/Dockerfile:242-256`, has no browser libraries), so
the agent must have downloaded one at run time. The PR bodies that would say
are not in this checkout (one squashed commit).

### 1.2 Done means

Today an operator runs visual QA on a laptop with Claude in Chrome or gstack.
When this design is built, they can submit one workflow and get back:

* every console page at 1440×900 and 390 wide, light and dark, captured the
  same way every run;
* a model's review of each page, each finding with its screenshot attached;
* a GIF per flow, with what the model saw at each step;
* an accessibility pass;
* one report listing findings, in the console's Artifacts pane, which already
  draws `.png`, `.jpg`, `.gif` and `.webp` inline up to 20 MiB
  (`apps/swarm-ui/src/Artifacts.tsx:909`; `swarm_api/checkpoint_content.py:255-261`,
  `IMAGE_EXTENSIONS`).

And it signs in without a person, by whichever QA-IDENTITY option the owner
picks. Until then it runs against the fixture build, which needs no sign-in.

### 1.3 The survey: every flavour as built

Seven ways to put a browser in front of a task exist on main (F1-F7). "Units" are
capacity units from the frozen `RESOURCE_CLASSES` (`profiles.py:100-102`):
scheduling weight, not money. Both `standard` and `browser` are 4 vCPU, 8 GiB,
1 unit. The only compute price in the repository is "`standard` at roughly
$0.20/hour" (`docs/cost-control.md:147`). Model prices are per million tokens
from `swarm_api/modelprices.py:127-134` (`SEED`): `claude-opus-5-5` $4 in,
$20 out, $0.20 cache read; `claude-sonnet-5` $2/$10/$0.20;
`claude-haiku-4-5` $1/$5/$0.10. The pinned `sonnet` and `haiku` class models
are `claude-sonnet-5-5` and `claude-haiku-5-5` (`terraform/infra/locals.tf:316-317`),
which `SEED` does not list, so the two older rows stand in for them in every
estimate below.

#### F1. The scripted `browser` profile (no model)

* **Where.** `profiles.py:1554-1567` (`RUNNER_PROFILES["browser"]`): image
  `agent-runtime-browser`, class `browser`, GKE Autopilot (Cloud Run cannot size
  `/dev/shm`), timeout 5,400 s. Runner `agent_worker/runners/browser.py`.
  Documented in `docs/browser-scripted-runner.md`.
* **Control.** Eight actions, run in order: `goto`, `click`, `fill`, `press`,
  `wait_for`, `wait` (≤60 s), `screenshot` (`name`, `full_page`, default
  full-page), `extract` (`inner_text`, default `body`)
  (`browser.py:273-344`; schema `profiles.py:1211-1273`, `_BROWSER_ACTION`).
  At most 200 (`browser.py:39`, `MAX_ACTIONS`). No `evaluate`, on purpose
  (`browser.py:19-22`). Viewport 320×240 to 3840×2160, default 1280×900
  (`profiles.py:1294-1295`, enforced by the catalogue, not the runner); a
  `user_agent` header. **No theme, no device scale factor, no locale, no
  reduced motion.**
* **Sees.** Pixels (screenshots), rendered text (`page.txt`, `extract-NNN.txt`),
  title and URL. Not the DOM, not the accessibility tree. **No model sees
  anything**: the result is files.
* **Writes.** `final.png`, `screenshot-NNN.png`, `page.txt`, `extract-NNN.txt`,
  and `console-errors.json` when there were console errors of type `error`
  (`browser.py:257-262`, `:376`). Page errors and failed requests are not
  captured. No HAR, trace or video.
* **Reaches.** Public `http(s)` hosts only: `goto` URLs go through
  `url_refusal`, and `localhost` and private addresses are refused
  (`docs/workflows.md:243-259`). Navigation caused by a click is not checked;
  the NetworkPolicy is the real control (`profiles.py:353-371`). No cookie,
  storage state or credential input.
* **Cost.** One `browser` pod, 1 unit, from the `resource:browser` pool (20 in
  dev, `dev.tfvars:145`). No model tokens: the profile declares
  `ANTHROPIC_API_KEY` (`profiles.py:1562-1563`) but the runner never calls a
  model. Cold start: one reading, 38.1 s node wait
  (`docs/browser-startup-speed.md:644`); an older one, STARTING at p50 15 s,
  p90 83 s (`:35`).
* **Determinism.** The best of the seven: the same actions on the same image
  give the same sequence. Screenshots disable animations (`browser.py:155`),
  but the page's own animations run; `goto` waits for `domcontentloaded`, not
  `load` (`browser.py:45`; the schema's text says "default load",
  `profiles.py:1220`, which is wrong). Fonts are whatever
  `playwright install --with-deps` brought (`Dockerfile:71-78`), fixed by the
  image digest.
* **Cannot.** Open the console: not `localhost` (refused), not the dev
  console (IAP), not any page that needs a sign-in. Dark mode. Judge anything.

#### F2. `claude-code-browser`, live, with Playwright MCP over the CDP gate

* **Where.** `profiles.py:1708-1727`: image `agent-runtime-browser`, class
  `browser`, GKE, 7,200 s, `live_browser=True`, model classes opus (default),
  fable, sonnet, haiku. The live browser exists when the deployment renders the
  display sidecar, `LIVE_BROWSER_SIDECAR=true`, from `enable_live_browser`
  (`terraform/infra/live.tf:87-105`); dev sets it (`dev.tfvars:534`), prod does
  not. The sidecar is headed Chromium on Xvfb, behind the gate's CDP proxy on
  `127.0.0.1:9222` (`docs/design/live-browser.md:107-126`).
* **The tool choice.** `input.browser_tool`, one of `auto`, `playwright`,
  `dev-browser`, `jev` (`profiles.py:1439-1458`; contract request 77), chosen by
  the caller per task by name; absent means `auto`. `browser_tools.choose`
  (`agent_worker/browser_tools.py:106-135`) maps it: Playwright MCP is
  registered on every choice; `auto` adds dev-browser and, if it can run, jev.
* **Sees.** Pixels (`browser_take_screenshot`, returned to the model as an
  image by upstream's default: **unverified here**, gap 4), the accessibility
  tree (`browser_snapshot`; `Accessibility.getFullAXTree` passes the gate,
  `cdppolicy.py:297-299`), DOM through `Runtime.evaluate` (`:248`).
* **Controls.** Input (click, type, keys), tabs, navigation; **theme** through
  emulated media; **vision deficiencies** (`Emulation.setEmulatedVisionDeficiency`,
  `:286`), locale and timezone. **Size only by preset**, through
  `swarm-handoff device`, answered on the worker's next poll (10 s,
  `swarm-handoff:52`), and no 390 preset (gap 1).
* **Cost.** One `browser` pod, 1 unit; with the sidecar it is split display
  3 vCPU / 5 GiB and worker 1 vCPU / 3 GiB (`scheduler/dispatch.py:694-698`).
  Cold start p50 125.2 s, p95 129.4 s (node wait 38.6 s, pull 54 s); warm p50
  6.2 s (`docs/browser-startup-speed.md:633-645`, n=4 each, before CACHE-PVC).
  Model tokens: the measured median browser task cost **$0.66**
  (`docs/browser-bench.md:161`), with a long tail the bench budgets at twice
  that.
* **Determinism.** Low as an agent drives it: the model picks clicks and
  waits. High when the agent runs a fixed script through `swarm_browser.live()`
  (F5): same preset, same Chromium, same page.
* **Cannot.** Resize freely. Launch its own Chromium (BROWSER-ENFORCE: the
  agent's `PLAYWRIGHT_BROWSERS_PATH` is a stub directory,
  `claude_code.py:961`, `docs/design/live-browser.md:177-215`). Open `file://`
  (gap 6). Read or write cookies (`cdppolicy.py:61-70`). Sign in without a
  person (gap 3). Capture while a person holds control
  (`cdppolicy.py:356-366`).

#### F3. `claude-code-browser`, live, dev-browser (attach only)

* **What.** SawyerHood dev-browser 1.0.0-rc.3 (`Dockerfile:158-202`), behind a
  wrapper that always adds `--connect $SWARM_CDP_ENDPOINT/client/dev-browser`
  and refuses `install`, `chrome`, `--headless` and any other `--connect`
  (`images/agent-runtime-browser/dev-browser:71-121`). The agent writes small
  scripts that run many steps per turn.
* **Sees and controls.** As F2, through the same gate, because it is the same
  browser. Its scripts can screenshot to files the agent then reads.
* **Cost.** As F2 for the pod. Fewer model turns than Playwright MCP for a
  long, repetitive flow, because one script does what would be twenty tool
  calls; no measurement of that saving is in the repository
  (`docs/browser-bench.md` compares the tools on success, not tokens).
* **Cannot.** Run without the live browser (the wrapper refuses without an
  endpoint, `:56-58`). It is guidance, not a sandbox: the real binary is on
  disk (`dev-browser:25-28`). The agent is told to `dev-browser stop` after
  every hand-off (`claude_code.py:363-369`). The design's claim that the gate
  refuses `Performance.enable` (`live-browser.md:278-282`,
  `browser-bench.md:14-19`) is out of date: the policy now allows it
  (`cdppolicy.py:72-97`).

#### F4. `claude-code-browser`, live, jev-browser MCP

* **What.** TypeSafe's `jev-browser` 0.2.0, patched to attach to the live
  browser's existing context with no viewport of its own
  (`Dockerfile:204-261`; `jev-browser/patches/jev-browser+0.2.0.patch`). Its
  tools are `browser_do` and `browser_read` (`agent_worker/jevkey.py:4`). A
  model, "Jev", picks each step of a goal ("open the newest failed task's
  logs").
* **How it decides.** It asks two kinds of question, `noul` (yes/no with a
  probability) and `choice` (one of several), and what it sends is **page text
  and element labels**, each element as `"<role> <name>"` cut at 80 characters
  (`live-browser.md:361-401`). Screenshots are not sent
  (`claude_code.py:426-430`). The judge is the workspace's `judgment_backend`
  (`live-browser.md:284-348`): `internal-jev` (default) or `internal-laya`, the
  internal decision API over Tailscale with no key, or `typesafe`, which needs
  the workspace's TypeSafe key. The wrapper never falls back from internal to
  TypeSafe (`jev-browser/bin/jev-browser-mcp`).
* **Sees.** Text and labels: no pixels, no layout, no colour, no overflow.
* **Cost.** As F2 for the pod and the outer agent. One internal probe answered
  in 0.34 s (`/jev`, confidence 0.98) and 2.9 s (`/laya`, 0.04)
  (`live-browser.md:377-383`). TypeSafe runs "spend TypeSafe credits"
  (`browser-bench.md:30-33`); no per-call figure is recorded.
* **Determinism.** Lowest: two models decide each step.
* **Cannot.** Run without the live browser; run on an internal backend with no
  route (`no_route`, `browser_tools.py:62-67`); see anything a screenshot would
  show. Its end-to-end acceptance from a pod against the reshaped internal API
  is unmeasured (`live-browser.md:356-359`).

#### F5. `swarm_browser.live()` from Python (any `browser_tool`)

* **What.** `images/agent-runtime-browser/swarm_browser.py:53` (`live`), in the
  venv's site-packages (`Dockerfile:149`): returns `(playwright, browser,
  context)` connected over CDP to the live browser's existing context;
  `handoff_page` (`:100`) finds the tab a person left.
* **Sees and controls.** Everything Playwright's Python API gives through the
  gate: screenshots to files, `page.accessibility`, `emulate_media`, page
  text, console messages, network events. Same size and cookie limits as F2.
* **Why it matters here.** It is how a **fixed, committed script** drives the
  live browser. A script gives the scripted runner's determinism with the live
  browser's reach (it can open `localhost`, below) and dark mode.

#### F6. `claude-code-browser` without the sidecar (headless Python Playwright)

* **When.** Only where the deployment does not render the sidecar
  (`LIVE_BROWSER_SIDECAR` false): prod today, never dev. The runner then keeps
  the real browsers path and appends `BROWSER_NOTE` (`claude_code.py:291-299`):
  launch `headless=True, channel="chromium"`. No MCP browser tool is registered
  (`write_mcp_config` adds them only with an endpoint, `:725`).
* **Sees and controls.** Everything Playwright can: **any viewport, any device
  scale factor, emulated media, video recording, `localhost`**.
* **Cost.** As F2 without the sidecar split; model tokens as any agent.
* **Determinism.** High for a scripted capture: nobody else drives the page.
* **Cannot.** Be chosen. A caller cannot ask for "no sidecar": it is a
  deployment setting, and on dev every `claude-code-browser` attempt has one.

#### F7. Plain `claude-code` with headless Playwright against the fixture build

* **What #1038 and #1427 did** (the issue's account). `claude-code` is class
  `standard` on GKE, 7,200 s (`profiles.py:1491-1520`), image
  `agent-runtime-base` with uv and node, **no Chromium and no browser system
  libraries** (`profiles.py:1701`). The fixture build is `npm run dev` in
  `apps/swarm-ui`: `USE_FIXTURES = import.meta.env.DEV && !import.meta.env.VITE_LIVE`
  (`apps/swarm-ui/src/api.ts:58`), served on `localhost:5173`, no network.
* **Sees and controls.** Whatever a Chromium the agent downloads at run time
  can, if it starts: any size, any theme, `localhost`.
* **Cost.** `standard` pool (80 in dev, `dev.tfvars:144`), a smaller image
  than the browser one. Plus a run-time Chromium download per attempt
  (`playwright install chromium`), which needs egress to Playwright's CDN and
  libraries the base image does not carry.
* **Determinism.** Low: the browser build is whatever the download returned.
* **Cannot, reliably.** Start a browser: it is unsupported by the image. But it
  is the **cheapest place to look at PNGs**: Claude Code's `Read` tool shows an
  image file to the model, so a review of captured screenshots needs no
  browser at all.

### 1.4 The map: which job, which flavour

| job | flavour | why this one | why not the others |
|---|---|---|---|
| **Pixel baseline sweep** (pages × 1440 / 390 × light / dark, deterministic) | **F5**: a fixed capture script on the live browser, against the fixture build served in the same pod, on a cheap model class | the only flavour on dev that reaches `localhost`, sets dark mode and runs a fixed sequence. The live browser shares the pod's network namespace with the worker (`livegate/egress.py:39`), and the Chromium policy blocks cluster names but not loopback (`chromium-policy.json`) | F1 cannot reach the fixture build or set a theme; F6 does not exist on dev; F7 has no browser; F2-F4 put a model in charge of each click |
| **Exploratory visual review** (a model looks at screenshots) | **F7** reading F5's PNGs (plain `claude-code`, `Read` on files staged with `input_from`) | the model sees the same pixels without holding a `browser` pod while it thinks. Review is the long part; the `standard` pool has four times the `browser` pool's ceiling in dev | doing it in F2 holds a `browser` slot and a sidecar for the whole review |
| **Goal-driven flow testing** | **F4 (jev)** for "can a person get from A to B", with **F2** (Playwright MCP) or **F3** (dev-browser) driving the GIF capture QA-FLOWS adds | jev is built for goals and judges on text; it is cheap per step on the internal backend and does not need the outer model to plan each click | F1 has no judgment; F5 is a fixed script, the opposite of goal-driven |
| **Long-page reading and content checks** | **F4** `browser_read` or **F2** `browser_snapshot`; **F1** `extract` for a public page | text is the right input, and jev and the snapshot read text without the cost of an image | a screenshot of a long page is downscaled until the text is unreadable to the model |
| **Regression against an earlier run or a mockup** | **F5** captures, compared by a pixel diff and then by a model on the diff images (F7) | the comparison is only meaningful when both sides were captured the same way. Mockups are served over `http` from the checkout (QA-FLOWS), captured at the same preset | any model-driven capture differs run to run for reasons that are not regressions |
| **Accessibility** | **F2** / **F5**: the accessibility tree, keyboard-only navigation, emulated vision deficiencies and reduced motion, plus axe-core injected by the capture tool (§4.2) | the gate passes `Accessibility.*`, `Input.*` and the media and vision emulations | F1 sees no tree; F4 sees labels but cannot measure contrast or focus order |
| **Authenticated flows** | F2 / F4 on the dev console, **by whichever QA-IDENTITY option the owner picks**: a QA Google account with a saved login, a QA service account through IAP with an injected token, or no sign-in at all by staying on the fixture build | this design does not choose (§2, invariant 9, says what the choice must answer) | F1 has no credential input; F7 has no browser |

---

## 2. Constraints that bind the design

### 2.1 The invariants it touches

**1. Only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create demand.** The
workflow's steps that wait on their dependencies are `QUEUED`/`READY` and hold
nothing. The design splits capture from review **so that a `browser` slot is
held only while a browser is in use**: a capture step exits when its tarball is
written, and the review that follows holds a `standard` slot instead. No step
requests a hand-off (the fixture build needs no sign-in), so none parks. If
QA-IDENTITY later picks a saved login, a hand-off that nobody answers ends after
60 s and the task parks holding nothing, as today
(`live-browser.md` §4.3).

**2. All-or-nothing reservation.** Unchanged. Each step's attempt reserves its
runner, class, backend and tenant pools in one transaction, as any step does.
The design adds no pool and no unit.

**5. Fencing.** Unchanged. A stale worker exits before its agent runs, so a
stale capture writes nothing. The capture tool writes only under
`$SWARM_ARTIFACTS_DIR`, which the worker uploads under the attempt it belongs
to.

**8. Checkpoints.** Unchanged, and the design leans on the steps being short
rather than on resuming them: a capture step is a few minutes and writes its
captures as it goes; a retried attempt captures again from the start, which
costs minutes. Periodic checkpointing stays on for every step, review included.

**9. Tenant isolation.** The workflow runs in the submitting tenant's
namespace, with its GSA, and its artifacts land under its GCS prefix. Against
the fixture build there is no tenant data on any page. **Two things change the
moment the run signs in to a real console**, and QA-IDENTITY's design must
answer both:

* a QA identity with a platform-admin role screenshots pages that list other
  tenants (Admin › Tenants, People, Counts). Those PNGs would be stored under
  the QA tenant's prefix, a copy of other tenants' data that tenant
  isolation exists to prevent;
* jev sends page text to the decision API or to TypeSafe (F4). On the fixture
  build that text is fixture data; on a real console it is whatever the QA
  identity can read.

**10. Callers pick a `runner_profile` by name, never an image, command,
resources or backend parameters.** Preserved without a new caller input. The
template names `claude-code-browser` and `claude-code`, a `browser_tool` by
name (contract request 77) and a `model_class` by name (contract request 101).
The device and theme are **agent-side commands inside the profile**
(`swarm-handoff device <preset>`, `page.emulate_media`), as #1649's constraint
check asks. The prompt telling an agent to run the capture tool is data to the
agent, the same as a prompt that says "run pytest": the platform executes only
the profile's `runner_argv`.

Invariants 3, 4, 6 and 7 are not touched: concurrency counts as before, no step
waits on a provider, no Spot, and the classes keep `requests == limits`.

### 2.2 The frozen contract

**The recommended design needs nothing changed under `apps/common/swarm_common/`.**
The presets live in the worker and swarm-api (`livegate/presets.py`,
`swarm_api/live.py:799-803`), not in the contract; the template uses only
fields the contract already has. The alternative in §3.3 needs two changes, as
requests in §5.

### 2.3 Platform facts the workflow has to live with

* **No fan-out primitive.** A workflow is a list of up to 50 steps
  (`swarm_common/config.py:80`, `max_workflow_steps`); a fan-out is N written
  steps.
* **`input_from` stages ONE file per parent**, and the names must differ across
  parents (`swarm_api/validation.py` `validate_dag`). A spec file cannot set
  `input_layout: by_parent` (`swarm_mcp/workflows.py:80-106`, `_STEP_KEYS`). So
  each capture step bundles its PNGs into one uniquely named tarball.
* **A file named in `input_from` is an expected output**: the upstream attempt
  fails if it is missing (`docs/workflows.md:533-594`). So a capture or review
  step writes its tarball **even when a page failed**, recording the failure
  inside it.
* **`on_step_failure: "continue"` cancels the failed step's transitive
  dependants** (`docs/workflows.md:1783-1785`). The report depends on
  everything, so one step that never starts (`CANNOT_START`) cancels the
  report. The template keeps every step's own failures inside its output so
  that only a platform failure does this; a "run whatever happened upstream"
  report would be a separate platform request, not needed for a first version.
* **No step can read an earlier workflow's artifacts.** `input_from` is within
  one workflow. This is the regression-baseline gap (§7 Q3).
* **No pixel-diff library is in any image** (no Pillow or imageio in
  `images/*`).

---

## 3. Designs that could work

### 3.1 Design A: one exploratory agent per page, all on the live browser

One `claude-code-browser` step per console screen (about 31: the 23 tabs of
`SECTIONS`, `apps/swarm-ui/src/App.tsx:259`, plus the eight task panes), each
told to start the fixture build, look at its page at both sizes and themes,
and report.

* **Code.** QA-KIT only (the 390 preset, theme instructions, auto-saved
  screenshots).
* **Latency.** 31 `browser` pods against a ceiling of 20 in dev: two waves,
  each cold start about 125 s, each step starting its own `npm ci`.
* **Money.** 31 model-driven browser tasks at the measured median $0.66, long
  tail ×2: **$20-41** of model, and every one holds a `browser` slot while
  its model thinks.
* **Breaks.** Determinism: each agent captures differently, so two runs cannot
  be compared. Coverage: the model decides what "looked at" means.

### 3.2 Design B: scripted capture, cheap review, jev for flows (recommended)

Four kinds of step:

1. **Capture** (6 steps, `claude-code-browser`, `browser_tool: playwright`,
   `model_class: haiku`). The agent builds the fixture console from the
   checkout, serves it on `localhost`, and runs the capture tool (§4.2): a
   fixed script on `swarm_browser.live()` that walks a route list at each
   preset and theme and writes PNGs, a manifest and `console-errors.json`. It
   then exits.
2. **Review** (6 steps, `claude-code`, `model_class: opus`). Reads one
   capture tarball, looks at every PNG, writes findings with a crop per
   finding.
3. **Flows and accessibility** (4 steps, `claude-code-browser`): goal-driven
   flows with jev, one GIF-recorded flow with Playwright, one accessibility
   pass.
4. **Report** (1 step, `claude-code`, `opus`). Merges everything into one
   report with the screenshots and GIFs attached.

* **Code.** QA-KIT, QA-FLOWS, BROWSER-START (all in #1649) and one new lane,
  QA-CAPTURE: a ~400-line Python tool in the browser image and a route list
  held to `SECTIONS` by a test (§6).
* **Latency.** Critical path capture → review → report, about 40-55 minutes
  (§4.7).
* **Money.** About **$15-25** of model per run, most of it the review steps'
  image tokens; compute about $1 (§4.7).
* **Breaks.** Nothing that exists. It relies on two things this lane could not
  run: that the live browser loads the worker's `localhost` (same network
  namespace, not blocked by policy; **unverified**, and QA-FLOWS's
  served-mockup test proves exactly this), and that a viewport screenshot
  through the gate is pixel-identical run to run on the same image
  (**unverified**; QA-CAPTURE's acceptance run measures it).

### 3.3 Design C: the scripted `browser` runner against a hosted fixture build

Capture with F1, no model at all: one `browser` step per page and
configuration, against a fixture build deployed at a URL the runner may open.

* **Code.** A frozen-contract change: `color_scheme` and `reduced_motion`
  inputs on `_BROWSER_INPUTS` (§5 requests A and B), read by `browser.py`. A
  production-built fixture console: `USE_FIXTURES` is tied to Vite's dev mode
  (`api.ts:58`), so a static fixture build needs a build flag. A place to host
  it that the runner can reach: a public origin (the runner refuses private
  addresses), deployed **per ref under test**, because QA tests a branch, not
  main.
* **Latency.** Fastest capture: no `npm ci` per step, no model turns.
* **Money.** Least: no capture tokens. Reviews cost what they cost in B.
* **Breaks.** It puts a public copy of the admin console on the internet (with
  fixture data only, but every screen and route). It adds a deploy per branch.
  It still cannot do flows, accessibility or anything signed in, so B's other
  steps are needed anyway.

### 3.4 Recommendation: B

B is the only design that runs on dev once #1649's own lanes land, needs no
frozen change and no new public surface, and captures deterministically. A
costs more and cannot be compared run to run. C's saving is only the capture
steps' few cents of haiku and their `npm ci`, bought with a contract change, a
public deployment and a per-branch pipeline. C becomes worth it only for a
**nightly sweep of main**, where the hosted build is deployed once; §7 Q4 asks
whether to plan for that.

---

## 4. The recommended design

### 4.1 What #1649's lanes provide, and what this design assumes of each

| lane | what the workflow uses | assumed name or shape |
|---|---|---|
| **QA-KIT** | a 390×844 preset; the device and theme commands in `LIVE_BROWSER_NOTE`; dark mode tested; screenshots saved to artifacts automatically; a test that the model receives a screenshot as an image | the preset is written here as **`phone-390`**; QA-KIT names it, and the template follows QA-KIT's name |
| **QA-FLOWS** | a GIF per flow; a mockup page served over `http` | a command or Python call the agent runs to start and stop a flow recording, writing `<name>.gif` under `$SWARM_ARTIFACTS_DIR`; written here as `swarm_qa flow start/stop` only if QA-FLOWS puts it in the same module, otherwise whatever it ships |
| **BROWSER-START** | capture steps start with the live browser | a start without it fails retryably (NOLIVE-GUARD already does), so a capture never silently runs headless at another size |
| **QA-IDENTITY** | nothing in the template; the authenticated arm (§4.6, last paragraph) waits for its decision | — |

### 4.2 The capture tool: `swarm_qa` (new, lane QA-CAPTURE)

A module beside `swarm_browser.py` in the browser image's venv:
`images/agent-runtime-browser/swarm_qa.py`, installed by one `COPY` line in
`images/agent-runtime-browser/Dockerfile` next to `:149`. It is a tool the
agent runs, not a platform behaviour: nothing in the worker calls it.

```
python -m swarm_qa capture \
    --routes apps/swarm-ui/qa/routes.json --group work-a \
    --base http://localhost:5173 \
    --presets desktop,phone-390 --themes light,dark \
    --out "$SWARM_ARTIFACTS_DIR/capture-work-a" \
    --bundle "$SWARM_ARTIFACTS_DIR/capture-work-a.tar"
```

What it does, in order, and why each:

1. **Refuses without the live browser.** `SWARM_CDP_ENDPOINT` unset exits 2
   with "no live browser: this capture would not match its baseline". A
   headless fallback would capture at another size with other fonts, which is
   noise in every comparison.
2. **Connects with `swarm_browser.live()`** and opens one tab of its own.
3. **For each preset**: `swarm-handoff device <preset>`, waits for the answer
   (the command already blocks up to 30 s, `swarm-handoff:53`). It never calls
   `set_viewport_size`, which the gate refuses.
4. **For each theme**: `page.emulate_media(color_scheme=..., reduced_motion="reduce")`.
5. **For each route in the group**: navigates; waits for `networkidle`, then
   `document.fonts.ready`, then the route's `ready` selector from the route
   list; freezes the clock with Playwright's `page.clock` at the route list's
   fixed instant, so "3 minutes ago" reads the same every run; captures
   **viewport-sized segments**, scrolling one viewport at a time up to the
   route's `segments` (default 4), with `animations="disabled"` and
   `caret="hide"`. Segments rather than one full-page image, because a model
   receives a tall image downscaled until its text is unreadable, and because
   whether a full-page capture through the gate needs a metrics override is
   **unverified**.
6. **Records what the page said**: console errors and page errors per route,
   failed requests, the route's text (`inner_text("body")`) and its
   accessibility tree (`page.accessibility.snapshot()`), each as a file beside
   the PNGs.
7. **With `--axe`**, injects axe-core from the image (pinned in the same
   Dockerfile line) and writes `axe.json` per route and theme.
8. **Writes `manifest.json`** (§4.4) and then the tarball, **always**, with any
   route that failed listed as failed. A failed route is a finding, not a
   failed step (§2.3).
9. **`python -m swarm_qa diff A.tar B.tar --out D`** compares two captures
   segment by segment: a changed-pixel ratio per image and a diff PNG where it
   is over the threshold. It needs Pillow, pinned in the image venv by the
   same lane. What `B.tar` can be is §7 Q3.

File names: `<group>/<route-id>--<preset>--<theme>--s<N>.png`, so the review
and the report can name every image without opening the manifest.

#### 4.2.1 Amendment, 2026-10-20: what the capture does after the first run (QA-TOOLS-FIX, epic #1680)

Steps 2 and 5 above are the design as written before the first run. That
run (epic #1680) showed four places where it did not hold, and QA-TOOLS-FIX
(PR 1681) changed the tool. What `images/agent-runtime-browser/swarm_qa.py`
and `images/agent-runtime-browser/swarm-handoff` do now:

* **It scrolls the page's own scroller, not the document.** The console's
  document does not scroll: a route's content scrolls inside `div.ctl-scroll`
  (`Spine.tsx`). The first run scrolled only the document, `main` and
  `[role=dialog]`, so it took one segment of every route (QA-003). The capture
  now picks, in this order: the nearest ancestor of the route's `ready`
  element that scrolls and has more to show; else the document, if it has
  more to show; else the largest scrolling element on the page by the area it
  shows (the 64 px rail scrolls too, and is not the page). It names no class,
  so a console that moves its scroller still segments. Each segment scrolls
  that element by its own visible height, the count is the route's `segments`
  or the pages it actually holds, whichever is fewer, and the element chosen
  is written in the route's manifest entry as `scroller`
  (`swarm_qa.py` (`MEASURE_JS`)).
* **It waits for the page to stop reading, and says so when it never does.**
  `networkidle`, `document.fonts.ready` and the `ready` selector are all met
  in the middle of a read: the fixture build reads on timers with no network
  traffic (QA-027). So after `load` the capture waits up to 10 s for the
  network to go quiet (the console polls, so not reaching it is recorded as
  `network_idle: false`, not a failure), then for the fonts, then up to 30 s
  for the `ready` selector, and then until the page has shown nothing pending
  on two polls in a row, 150 ms apart, for up to 15 s. Pending means a
  skeleton bar, a region marked `aria-busy`, the page head's refresh while it
  reads, or the word "reading…" anywhere on the page, and only if it is drawn
  (`swarm_qa.py` (`PENDING_JS`), `SETTLE_TIMEOUT_MS`). A page still reading
  after 15 s is shot as it stands, never dropped: its entry says
  `status: "unsettled"` and `settled: false`, `pending` lists up to five
  things it still showed, and `failed` carries "did not settle in 15000 ms:
  still showing … (shot as it stood)". That is a finding for the review, not
  a failed step.
* **Flows are recorded through the connection the recorder already holds.**
  `swarm-handoff record start <name>` and `swarm-handoff record stop` write
  `$SWARM_ARTIFACTS_DIR/flows/<name>.gif`. The recorder connects to the live
  browser through the gate's CDP proxy, as the agent does, and takes each
  frame with the page's own screenshot on the session that connection
  already has for the page. It never opens a CDP session of its own: that is
  a second browser-debugging session, which the gate refuses, and the first
  run's recorder asked for one and skipped every frame (QA-002). Frames are
  taken at CSS size, so a 1440×900 desktop frame is decoded here and fitted
  to 720×450 (`swarm-handoff` (`LiveFrames`)).
* **Each route starts from a clean browser state.** The capture's tab lives
  in the live browser's one profile, so whatever a route left in storage drew
  the next route: the sidebar's Recent workflows appeared on every route after
  `/workflows` and on none before it (QA-077). Each route is its own
  navigation, so the page's memory starts empty; now a script that runs
  before the console's own scripts, on every load of the console's origin,
  also empties its local and session storage, so every route draws what a
  first visit draws (`swarm_qa.py` (`RESET_STATE_JS`)). Other origins are
  left alone, and cookies are not cleared. A fresh browser context per route would also do it, but opens a
  window the gate has never been measured fitting to a preset.

### 4.3 The route list: `apps/swarm-ui/qa/routes.json` (new, lane QA-CAPTURE)

```json
{
  "format": 1,
  "clock": "2026-10-19T12:00:00Z",
  "groups": {
    "overview-automate": [
      {"id": "now", "hash": "#overview/now", "ready": "main h1", "segments": 3},
      {"id": "schedules", "hash": "#automate/schedules", "ready": "main h1"},
      {"id": "approvals", "hash": "#automate/approvals", "ready": "main h1"}
    ]
  },
  "mockups": [
    {"id": "overview-mockup", "path": "docs/web-ui/mockups/overview.html", "compare_with": "now"}
  ]
}
```

Six groups, sized so each capture step takes about the same time: 
`overview-automate` (3 tabs), `work-a` (`running`, `workflows`, `runs`,
`timeline`, `repositories`), `work-b` (`setup`, `access`, `new`,
`new-workflow`, `new-issue`), `capacity` (`pools`, `catalogue`, `profiles`,
`holders`, `accounts`, `quota`), `admin` (`limits`, `tenants`, `counts`,
`people`) and `task` (the eight task panes, `detail` to `browser`, on one
fixture task). The `settings/*` pages join whichever group QA-CAPTURE finds
lightest. The hashes and `ready` selectors are QA-CAPTURE's to read from
`App.tsx`; the shape above is illustrative.

**A test holds it to the nav**, the way `tests/unit/scripts/test_issue_forms.py`
holds the issue forms: `tests/unit/scripts/test_qa_routes.py` reads `SECTIONS`
in `App.tsx` and fails when a tab is in one and not the other. A sweep that
silently skips a new tab is the defect CLAUDE.md's "empty output is not
success" names.

### 4.4 What each step writes

**`manifest.json`, in every capture tarball:**

```json
{
  "format": 1,
  "group": "work-a",
  "commit": "<the checkout's HEAD>",
  "image": "<agent-runtime-browser digest, from the worker's environment>",
  "presets": ["desktop", "phone-390"],
  "themes": ["light", "dark"],
  "routes": [
    {"id": "runs", "status": "captured",
     "images": ["work-a/runs--desktop--light--s1.png"],
     "console_errors": 0, "page_errors": 0, "failed_requests": 0,
     "text": "work-a/runs--desktop--light.txt",
     "a11y": "work-a/runs--desktop--light.a11y.json"}
  ],
  "failed": [{"id": "timeline", "preset": "phone-390", "theme": "dark", "error": "ready selector not found in 30 s"}]
}
```

**`findings-<group>.json`, in every review tarball** (beside one crop PNG per
finding):

```json
{
  "format": 1,
  "group": "work-a",
  "findings": [
    {"id": "work-a-007", "severity": "major",
     "route": "runs", "preset": "phone-390", "theme": "dark",
     "what": "the Checks column overflows its card by 64 px; the last two check names are under the next card",
     "expected": "the column wraps or the table scrolls inside its card, as at 1440",
     "evidence": ["work-a-007.png"],
     "source": "work-a/runs--phone-390--dark--s2.png"}
  ],
  "reviewed": 40,
  "not_reviewed": []
}
```

`reviewed` is the count of images the reviewer opened, and the report checks
it against the manifest: a review that looked at nothing is not a clean
review.

**The report step's artifacts:** `report.md` (findings grouped by page,
severity first, each with its image inline as a relative link), `findings.json`
(every finding, with its group prefix), every finding's crop PNG, every flow's
GIF, and the manifests. The Artifacts pane shows the PNGs and GIFs inline
(`Artifacts.tsx:909`).

### 4.5 The changes, by component

| component | change | files | lane |
|---|---|---|---|
| browser image | `swarm_qa` module; axe-core and Pillow pinned | `images/agent-runtime-browser/swarm_qa.py` (new), `images/agent-runtime-browser/Dockerfile` | QA-CAPTURE |
| console | the route list, naming the fixture task the task panes are captured on | `apps/swarm-ui/qa/routes.json` (new) | QA-CAPTURE |
| worker | none beyond QA-KIT's note text | `apps/agent-worker/agent_worker/runners/claude_code.py` (QA-KIT's) | QA-KIT |
| scheduler, swarm-api, plugin | **none** | — | — |
| docs | the template and the user-facing guide | `docs/qa/console-qa.workflow.json` (new), `docs/browser-qa.md` (new), one link from `docs/workflows.md` | QA-TEMPLATE |
| tests | tool, route list, template | `tests/unit/worker/test_swarm_qa.py`, `tests/unit/scripts/test_qa_routes.py`, `tests/unit/mcp/test_qa_workflow_template.py` (all new) | QA-CAPTURE, QA-TEMPLATE |

No Firestore document changes: the run is an ordinary workflow, and its
findings live in artifacts, not in a collection. No API field is added: every
field the template sends exists (`WorkflowStepCreate`,
`swarm_api/schemas.py:207-270`).

### 4.6 The workflow spec template

Valid against the spec reader `swarm_mcp.workflows.read_spec` (the format
`swarm_workflow` and `swarm workflow` take), and so against
`WorkflowCreate`: 17 steps of the 50 allowed; every `input_from` source is a
`depends_on`; every staged name is unique; every key is in `_STEP_KEYS` or
`SPEC_KEYS`. `browser_tool` is a declared input of `claude-code-browser`;
`model_class` is a named class. This lane checked the JSON below on
2026-10-19 the way `swarm_workflow` reads it
(`swarm_mcp/server.py:4675-4682`): `profiles.check_spec_model_classes`, then
`workflows.read_spec` on the spec without its classes, then
`profiles.with_model_classes`; and then as swarm-api does:
`schemas.WorkflowCreate`, `validation.validate_dag(max_steps=50)`, and per step
`validate_runner_profile`, `validate_model_class` and `validate_runner_input`.
All 17 steps passed. QA-TEMPLATE commits it as a file under a test that
repeats that check (§6).

```json
{
  "title": "Console visual QA: every page at 1440 and 390, light and dark",
  "label": "console-qa",
  "repository_url": "https://github.com/bogdan-alexandrescu/SwarmCloud",
  "repository_ref": "main",
  "strategy": "collect",
  "merge": "off",
  "on_step_failure": "continue",
  "steps": [
    {"step_id": "capture-overview-automate", "runner_profile": "claude-code-browser", "model_class": "haiku",
     "inputs": {"browser_tool": "playwright"}, "allow_empty_diff": true, "timeout_seconds": 1800,
     "prompt": "Capture, do not review. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background (the fixture build, no network). When http://localhost:5173 answers, run `python -m swarm_qa capture --routes apps/swarm-ui/qa/routes.json --group overview-automate --base http://localhost:5173 --presets desktop,phone-390 --themes light,dark --out $SWARM_ARTIFACTS_DIR/capture-overview-automate --bundle $SWARM_ARTIFACTS_DIR/capture-overview-automate.tar`. If it exits non-zero, put its last 40 lines in your answer and still make sure capture-overview-automate.tar exists. Change no file in the repository."},
    {"step_id": "capture-work-a", "runner_profile": "claude-code-browser", "model_class": "haiku",
     "inputs": {"browser_tool": "playwright"}, "allow_empty_diff": true, "timeout_seconds": 1800,
     "prompt": "Capture, do not review. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background (the fixture build, no network). When http://localhost:5173 answers, run `python -m swarm_qa capture --routes apps/swarm-ui/qa/routes.json --group work-a --base http://localhost:5173 --presets desktop,phone-390 --themes light,dark --out $SWARM_ARTIFACTS_DIR/capture-work-a --bundle $SWARM_ARTIFACTS_DIR/capture-work-a.tar`. If it exits non-zero, put its last 40 lines in your answer and still make sure capture-work-a.tar exists. Change no file in the repository."},
    {"step_id": "capture-work-b", "runner_profile": "claude-code-browser", "model_class": "haiku",
     "inputs": {"browser_tool": "playwright"}, "allow_empty_diff": true, "timeout_seconds": 1800,
     "prompt": "Capture, do not review. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background (the fixture build, no network). When http://localhost:5173 answers, run `python -m swarm_qa capture --routes apps/swarm-ui/qa/routes.json --group work-b --base http://localhost:5173 --presets desktop,phone-390 --themes light,dark --out $SWARM_ARTIFACTS_DIR/capture-work-b --bundle $SWARM_ARTIFACTS_DIR/capture-work-b.tar`. If it exits non-zero, put its last 40 lines in your answer and still make sure capture-work-b.tar exists. Change no file in the repository."},
    {"step_id": "capture-capacity", "runner_profile": "claude-code-browser", "model_class": "haiku",
     "inputs": {"browser_tool": "playwright"}, "allow_empty_diff": true, "timeout_seconds": 1800,
     "prompt": "Capture, do not review. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background (the fixture build, no network). When http://localhost:5173 answers, run `python -m swarm_qa capture --routes apps/swarm-ui/qa/routes.json --group capacity --base http://localhost:5173 --presets desktop,phone-390 --themes light,dark --out $SWARM_ARTIFACTS_DIR/capture-capacity --bundle $SWARM_ARTIFACTS_DIR/capture-capacity.tar`. If it exits non-zero, put its last 40 lines in your answer and still make sure capture-capacity.tar exists. Change no file in the repository."},
    {"step_id": "capture-admin", "runner_profile": "claude-code-browser", "model_class": "haiku",
     "inputs": {"browser_tool": "playwright"}, "allow_empty_diff": true, "timeout_seconds": 1800,
     "prompt": "Capture, do not review. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background (the fixture build, no network). When http://localhost:5173 answers, run `python -m swarm_qa capture --routes apps/swarm-ui/qa/routes.json --group admin --base http://localhost:5173 --presets desktop,phone-390 --themes light,dark --out $SWARM_ARTIFACTS_DIR/capture-admin --bundle $SWARM_ARTIFACTS_DIR/capture-admin.tar`. If it exits non-zero, put its last 40 lines in your answer and still make sure capture-admin.tar exists. Change no file in the repository."},
    {"step_id": "capture-task", "runner_profile": "claude-code-browser", "model_class": "haiku",
     "inputs": {"browser_tool": "playwright"}, "allow_empty_diff": true, "timeout_seconds": 1800,
     "prompt": "Capture, do not review. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background (the fixture build, no network). When http://localhost:5173 answers, run `python -m swarm_qa capture --routes apps/swarm-ui/qa/routes.json --group task --base http://localhost:5173 --presets desktop,phone-390 --themes light,dark --axe --out $SWARM_ARTIFACTS_DIR/capture-task --bundle $SWARM_ARTIFACTS_DIR/capture-task.tar`. If it exits non-zero, put its last 40 lines in your answer and still make sure capture-task.tar exists. Change no file in the repository."},

    {"step_id": "review-overview-automate", "runner_profile": "claude-code", "model_class": "opus",
     "depends_on": ["capture-overview-automate"], "input_from": {"capture-overview-automate": "capture-overview-automate.tar"},
     "allow_empty_diff": true, "timeout_seconds": 3600,
     "prompt": "Visual QA review of the console's fixture build. Unpack capture-overview-automate.tar. For every PNG its manifest.json lists, open the image with the Read tool and look at it: layout, overflow and clipping, alignment, spacing, truncation, contrast in dark mode, anything that differs between 1440 and phone-390 that should not, and anything that contradicts the page's own text file. Each route that failed to capture is itself a finding. For each finding write a crop PNG named <id>.png showing it, and an entry in findings-overview-automate.json in the shape of docs/design/visual-qa.md section 4.4, with reviewed set to the number of images you opened. Bundle findings-overview-automate.json and the crops into review-overview-automate.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},
    {"step_id": "review-work-a", "runner_profile": "claude-code", "model_class": "opus",
     "depends_on": ["capture-work-a"], "input_from": {"capture-work-a": "capture-work-a.tar"},
     "allow_empty_diff": true, "timeout_seconds": 3600,
     "prompt": "Visual QA review of the console's fixture build. Unpack capture-work-a.tar. For every PNG its manifest.json lists, open the image with the Read tool and look at it: layout, overflow and clipping, alignment, spacing, truncation, contrast in dark mode, anything that differs between 1440 and phone-390 that should not, and anything that contradicts the page's own text file. Each route that failed to capture is itself a finding. For each finding write a crop PNG named <id>.png showing it, and an entry in findings-work-a.json in the shape of docs/design/visual-qa.md section 4.4, with reviewed set to the number of images you opened. Bundle findings-work-a.json and the crops into review-work-a.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},
    {"step_id": "review-work-b", "runner_profile": "claude-code", "model_class": "opus",
     "depends_on": ["capture-work-b"], "input_from": {"capture-work-b": "capture-work-b.tar"},
     "allow_empty_diff": true, "timeout_seconds": 3600,
     "prompt": "Visual QA review of the console's fixture build. Unpack capture-work-b.tar. For every PNG its manifest.json lists, open the image with the Read tool and look at it: layout, overflow and clipping, alignment, spacing, truncation, contrast in dark mode, anything that differs between 1440 and phone-390 that should not, and anything that contradicts the page's own text file. Each route that failed to capture is itself a finding. For each finding write a crop PNG named <id>.png showing it, and an entry in findings-work-b.json in the shape of docs/design/visual-qa.md section 4.4, with reviewed set to the number of images you opened. Bundle findings-work-b.json and the crops into review-work-b.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},
    {"step_id": "review-capacity", "runner_profile": "claude-code", "model_class": "opus",
     "depends_on": ["capture-capacity"], "input_from": {"capture-capacity": "capture-capacity.tar"},
     "allow_empty_diff": true, "timeout_seconds": 3600,
     "prompt": "Visual QA review of the console's fixture build. Unpack capture-capacity.tar. For every PNG its manifest.json lists, open the image with the Read tool and look at it: layout, overflow and clipping, alignment, spacing, truncation, contrast in dark mode, anything that differs between 1440 and phone-390 that should not, and anything that contradicts the page's own text file. Each route that failed to capture is itself a finding. For each finding write a crop PNG named <id>.png showing it, and an entry in findings-capacity.json in the shape of docs/design/visual-qa.md section 4.4, with reviewed set to the number of images you opened. Bundle findings-capacity.json and the crops into review-capacity.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},
    {"step_id": "review-admin", "runner_profile": "claude-code", "model_class": "opus",
     "depends_on": ["capture-admin"], "input_from": {"capture-admin": "capture-admin.tar"},
     "allow_empty_diff": true, "timeout_seconds": 3600,
     "prompt": "Visual QA review of the console's fixture build. Unpack capture-admin.tar. For every PNG its manifest.json lists, open the image with the Read tool and look at it: layout, overflow and clipping, alignment, spacing, truncation, contrast in dark mode, anything that differs between 1440 and phone-390 that should not, and anything that contradicts the page's own text file. Each route that failed to capture is itself a finding. For each finding write a crop PNG named <id>.png showing it, and an entry in findings-admin.json in the shape of docs/design/visual-qa.md section 4.4, with reviewed set to the number of images you opened. Bundle findings-admin.json and the crops into review-admin.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},
    {"step_id": "review-task", "runner_profile": "claude-code", "model_class": "opus",
     "depends_on": ["capture-task"], "input_from": {"capture-task": "capture-task.tar"},
     "allow_empty_diff": true, "timeout_seconds": 3600,
     "prompt": "Visual and accessibility QA review of the console's task panes (fixture build). Unpack capture-task.tar. For every PNG its manifest.json lists, open the image with the Read tool and look at it: layout, overflow and clipping, alignment, spacing, truncation, contrast in dark mode, anything that differs between 1440 and phone-390 that should not, and anything that contradicts the page's own text file. Also read every axe.json and accessibility tree file: each axe violation of impact serious or critical is a finding. Each route that failed to capture is itself a finding. For each finding write a crop PNG named <id>.png showing it, and an entry in findings-task.json in the shape of docs/design/visual-qa.md section 4.4, with reviewed set to the number of images you opened. Bundle findings-task.json and the crops into review-task.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},

    {"step_id": "flow-find-failure", "runner_profile": "claude-code-browser", "model_class": "sonnet",
     "inputs": {"browser_tool": "jev"}, "allow_empty_diff": true, "timeout_seconds": 2700,
     "prompt": "Goal-driven flow test of the console's fixture build. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background; open http://localhost:5173 in the live browser at the desktop preset. Start a flow recording named flow-find-failure. Using the jev browser tools, reach this goal as a person would, without typing addresses: find the most recently failed task and open its logs, then its attempts. Stop the recording. Then switch to the phone-390 preset (swarm-handoff device phone-390) and do it again as flow-find-failure-phone. For each run write in flow-find-failure.json: whether the goal was reached, the steps taken, and every point where a label was missing, ambiguous or misleading, each with a screenshot saved under $SWARM_ARTIFACTS_DIR. Bundle flow-find-failure.json, both GIFs and the screenshots into flow-find-failure.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},
    {"step_id": "flow-workflow-graph", "runner_profile": "claude-code-browser", "model_class": "sonnet",
     "inputs": {"browser_tool": "jev"}, "allow_empty_diff": true, "timeout_seconds": 2700,
     "prompt": "Goal-driven flow test of the console's fixture build. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background; open http://localhost:5173 in the live browser at the desktop preset. Start a flow recording named flow-workflow-graph. Using the jev browser tools, reach this goal as a person would, without typing addresses: open a workflow that has a failed step, find which step failed and why, and open that step's changes. Stop the recording. Then do it again in dark mode (page.emulate_media(color_scheme='dark') through swarm_browser.live()) as flow-workflow-graph-dark. For each run write in flow-workflow-graph.json: whether the goal was reached, the steps taken, and every point where a label was missing, ambiguous or misleading, each with a screenshot saved under $SWARM_ARTIFACTS_DIR. Bundle flow-workflow-graph.json, both GIFs and the screenshots into flow-workflow-graph.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},
    {"step_id": "flow-submit-form", "runner_profile": "claude-code-browser", "model_class": "sonnet",
     "inputs": {"browser_tool": "playwright"}, "allow_empty_diff": true, "timeout_seconds": 2700,
     "prompt": "Scripted flow test of the console's fixture build with the Playwright browser tools. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background; open http://localhost:5173 in the live browser. At the desktop preset and then at phone-390 (swarm-handoff device phone-390), record a flow named flow-submit-form-<preset>: open the task form at `/submit/task` (route `new` in apps/swarm-ui/qa/routes.json), the way a person reaches it: the rail's Submit item, then Start a task; try to submit it empty, read every validation message, fill each field, choose each runner profile in turn and note what the form shows for it, and stop before the final submit. Take a browser_take_screenshot at each step and say in flow-submit-form.json what each screenshot showed you, so the report can tell whether you saw the image. Note every message that is missing, wrong or clipped, with its screenshot under $SWARM_ARTIFACTS_DIR. Bundle flow-submit-form.json, the GIFs and the screenshots into flow-submit-form.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},
    {"step_id": "a11y-keyboard", "runner_profile": "claude-code-browser", "model_class": "sonnet",
     "inputs": {"browser_tool": "playwright"}, "allow_empty_diff": true, "timeout_seconds": 2700,
     "prompt": "Accessibility pass of the console's fixture build. In apps/swarm-ui run `npm ci` and start `npm run dev` in the background; open http://localhost:5173 in the live browser at the desktop preset. For each top-level section, using only the keyboard (Tab, Shift+Tab, Enter, Escape, arrows): reach every control, open and close every dialog and inspector, and note where focus is lost, invisible or trapped. Then take browser_snapshot of each page and note controls with no accessible name. Then, through swarm_browser.live(), repeat one page per section with emulate_media(reduced_motion='reduce') and with Emulation.setEmulatedVisionDeficiency set to deuteranopia, and screenshot each. Write a11y-keyboard.json in the findings shape of docs/design/visual-qa.md section 4.4, with a screenshot per finding under $SWARM_ARTIFACTS_DIR, and bundle it with the screenshots into a11y-keyboard.tar under $SWARM_ARTIFACTS_DIR. Change no file in the repository."},

    {"step_id": "report", "runner_profile": "claude-code", "model_class": "opus",
     "depends_on": ["review-overview-automate", "review-work-a", "review-work-b", "review-capacity", "review-admin", "review-task",
                    "flow-find-failure", "flow-workflow-graph", "flow-submit-form", "a11y-keyboard"],
     "input_from": {"review-overview-automate": "review-overview-automate.tar", "review-work-a": "review-work-a.tar",
                    "review-work-b": "review-work-b.tar", "review-capacity": "review-capacity.tar",
                    "review-admin": "review-admin.tar", "review-task": "review-task.tar",
                    "flow-find-failure": "flow-find-failure.tar", "flow-workflow-graph": "flow-workflow-graph.tar",
                    "flow-submit-form": "flow-submit-form.tar", "a11y-keyboard": "a11y-keyboard.tar"},
     "allow_empty_diff": true, "timeout_seconds": 3600,
     "prompt": "Write the console visual QA report. Unpack every staged tar. Check each review's reviewed count against the images its group's routes should have produced (two presets, two themes, each route's segments) and report any shortfall as a finding about the run itself. Merge every finding into findings.json, de-duplicating the same defect seen at several sizes into one finding that lists each. Write report.md: a summary table (findings by severity and by page), then each finding, severity first, with its crop PNG embedded by relative link, then one section per flow with its GIF embedded and whether its goal was reached, then what the model said it saw in each flow-submit-form screenshot. Copy every crop PNG and every GIF to $SWARM_ARTIFACTS_DIR next to report.md and findings.json so the console shows them. Change no file in the repository."}
  ]
}
```

Why the choices in it:

* **`repository_ref: "main"`** is the ref under test; a run on a branch
  changes this one field. `merge: "off"` and `allow_empty_diff: true` on every
  step, because the run changes nothing and publishes nothing.
* **`on_step_failure: "continue"`**, so one flow that fails does not cancel
  the captures (§2.3 says what it still cancels).
* **`haiku` for capture** because the agent runs one command; **`opus` for
  review and report** because the judgment is the product (§7 Q1); **`sonnet`
  for flows**, which are mostly tool calls whose judgment jev or the
  screenshot carries.
* **`flow-submit-form` asks the agent to say what each screenshot showed.**
  That is the run-time half of QA-KIT's "the model receives the screenshot as
  an image": the report quotes it, so a run where the model got no image says
  so in the report.
* **No `resource_class`**: each profile's own class is the right one.
* **The authenticated arm is not in the template.** Once QA-IDENTITY's option
  is built, it is one more group of steps on `claude-code-browser` against the
  dev console's address, signed in as that option says, with `browser_tool`
  `playwright` for capture and `jev` for flows, and subject to §2.1's
  invariant-9 answer about what a QA identity may screenshot.

### 4.7 Cost, duration, and what it waits on

**Estimates, not measurements.** Inputs: the cold-start and bench figures in
§1.3; prices from `SEED`; an image read by the model at about 1,600 tokens
(a 1440×900 PNG is about 1.3 MP, which Claude downscales to about 1.15 MP);
`npm ci` and Vite start of apps/swarm-ui at 1-3 minutes, **unmeasured** on a
pod.

| steps | per step | arithmetic | total |
|---|---|---|---|
| 6 capture, `claude-code-browser`, haiku | about $0.10 | ~20 turns, context ≤ 20k, mostly cache reads; no image is read by the model | **~$0.60** |
| 6 review, `claude-code`, opus | about $1.50-2.50 | ~24-40 images × 1,600 tokens enter the context and are re-read each turn from cache: ~40 turns × ~80k average context = 3.2 M cache-read tokens × $0.20/M = $0.64; ~150k cache writes × $5/M = $0.75; ~30k output × $20/M = $0.60 | **~$9-15** |
| 3 flow + 1 accessibility, `claude-code-browser`, sonnet | about $0.70-1.30 | the browser bench's median $0.66 per task, ×2 for the long tail, at sonnet's lower price | **~$3-5** |
| 1 report, opus | about $2-4 | reads ten tarballs' JSON and some images | **~$2-4** |
| compute | 10 browser pods × ~10 min + 7 standard pods × ~30 min at ~$0.20/hour | | **~$1** |
| **run** | | | **~$16-26** |

**Duration.** Captures: cold start about 2 minutes, `npm ci` and Vite 1-3
minutes, then 4 configurations × about 6 routes × about 3 segments, each a
navigation and settle of about 3 s plus two preset switches of up to 10 s:
about 5 minutes. So **8-12 minutes**. Reviews: **20-30 minutes** (40 turns at
30-45 s). Report: **10-15 minutes**. Critical path **about 40-55 minutes**;
the flows run beside the captures and finish inside it.

**Capacity.** At most 10 `browser` pods at once (6 captures, 3 flows, 1
accessibility) of dev's 20, and 7 `standard` pods. The template fits inside a
tenant's default pool of 40 (`pool_limits.default_tenant`, `dev.tfvars:140`).

**What it waits on.**

| gap | without it | the template's step that needs it |
|---|---|---|
| **QA-KIT** | no 390 preset, so `--presets desktop,phone-390` fails; no theme note; screenshots saved only when an agent remembers; nothing proves the model sees images | every capture; `flow-*`; `a11y-keyboard` |
| **QA-FLOWS** | no GIF; no `http` mockup, so the route list's `mockups` cannot be captured beside the app (and nothing has proved the live browser loads the pod's `localhost`, which every step here assumes) | `flow-*`, the mockup comparison |
| **BROWSER-START** (part of #1366) | a capture step can land without its live browser; NOLIVE-GUARD fails it retryably, which costs a retry, and a step out of attempts cancels the report | every capture and flow |
| **QA-CAPTURE** (new, §6) | no `swarm_qa`, no route list; an agent writes its own loop each run, and two runs cannot be compared | every capture and review |
| **QA-IDENTITY** | the run stays on the fixture build, which is the template as written | only the authenticated arm, not in the template |

---

## 5. Frozen contract

**The recommended design (B) needs no change under
`apps/common/swarm_common/`.** Every field it uses exists: `RUNNER_PROFILES`
entries `claude-code-browser` and `claude-code`, the declared input
`browser_tool` (request 77), `MODEL_CLASSES` (request 101), and
`max_workflow_steps = 50`.

The two requests below are needed **only if the owner chooses design C**
(§3.3, §7 Q4). They are written in the shape of
`docs/contract-change-requests.md`; their numbers are letters until the
operator files them there, at the next free numbers (106 and 107 at this
reading).

### Request A. `profiles.py`: the scripted `browser` runner takes a `color_scheme`

**Status:** proposed by docs/design/visual-qa.md §5 (QA-GUIDE, part of #1649);
not accepted.

#### Why

The scripted runner is the only flavour with no model and any viewport, and
the only one that cannot capture a dark theme. A pixel baseline sweep is
pages × sizes × themes; without a theme input it covers half.

#### The change

```python
_BROWSER_INPUTS: dict[str, RunnerInput] = {
    ...
    # Request A (visual-qa.md §5): the page's prefers-color-scheme, by name.
    # A choice, never a CSS or a script (invariant 10).
    "color_scheme": RunnerInput(
        "string", choices=("light", "dark", "no-preference"),
        means="the page's prefers-color-scheme; default light",
    ),
}
```

`agent_worker/runners/browser.py` passes it to `browser.new_context(color_scheme=...)`.

**Tests:** `tests/unit/mcp/test_runner_inputs.py` (the schema is held to the
runner); a runner test that `dark` reaches `new_context`; a catalogue test that
`"sepia"` is refused at submission.

#### Rollback

Remove the entry. A stored task that names it is refused at its next
validation as an undeclared input; a queued one runs without it at the default.

#### What it would break

Nothing: an absent input is today's behaviour. Terraform's mirror of the entry
(`terraform/infra/locals.tf:116-124`) carries no inputs.

#### Invariants

**10**: a named choice from a fixed list. The rest are untouched.

### Request B. `profiles.py`: the scripted `browser` runner takes `reduced_motion`

**Status:** proposed by docs/design/visual-qa.md §5; not accepted.

#### Why

A screenshot disables CSS animations (`browser.py:155`) but not a page's own
JavaScript motion; `prefers-reduced-motion: reduce` makes the console's
transitions stop, so two captures of the same page match.

#### The change

```python
    "reduced_motion": RunnerInput(
        "string", choices=("reduce", "no-preference"),
        means="the page's prefers-reduced-motion; default no-preference",
    ),
```

passed to `new_context(reduced_motion=...)`.

**Tests, rollback, what it would break, invariants:** as request A.

### Requests not made

* **The 390 preset** is not a contract change (`livegate/presets.py`,
  `swarm_api/live.py`): QA-KIT's.
* **A caller-chosen viewport for the live browser** is not requested. The
  presets exist so a caller names a device and never sends metrics
  (`presets.py:3-9`); the design keeps that.
* **Staging an earlier workflow's artifact into a step** (§7 Q3, option b)
  would touch `WorkflowStepCreate` in swarm-api and the step document in
  `models.py`. If the owner picks it, it gets its own design and its own
  request; it is not drafted here.

---

## 6. Build plan

Lanes split by territory: no two lanes edit the same file. #1649's own lanes
(QA-KIT, QA-FLOWS, QA-IDENTITY, BROWSER-START) are briefed by the issue and
listed here only for their order and the files that bound this design.

| order | lane | files (territory) | red-first tests | depends on |
|---|---|---|---|---|
| 1 | **QA-KIT** (#1649) | `livegate/presets.py`, `swarm_api/live.py`, `apps/swarm-ui/src/LiveBrowser.tsx`, `Theater.tsx`, `TheaterTimeline.tsx` (preset labels), `runners/claude_code.py` (`LIVE_BROWSER_NOTE`), the screenshot auto-save, and their tests | the issue's: a 390×844 preset resolves; the note names `swarm-handoff device` and `emulate_media`; dark emulation passes the gate; a screenshot reaches the model as an image | — |
| 1 | **BROWSER-START** (#1366, #1649) | its own brief | its own | — |
| 1 | **QA-IDENTITY** (design, #1649) | its design doc | — | — |
| 2 | **QA-FLOWS** (#1649) | its own brief; if it edits `images/agent-runtime-browser/Dockerfile`, QA-CAPTURE waits for it | the issue's, plus: the live browser loads a page served on the worker's `localhost` | — |
| 3 | **QA-CAPTURE** (new) | `images/agent-runtime-browser/swarm_qa.py` (new); one `COPY` line, axe-core and Pillow pins in `images/agent-runtime-browser/Dockerfile`; `apps/swarm-ui/qa/routes.json` (new); `tests/unit/worker/test_swarm_qa.py` (new); `tests/unit/scripts/test_qa_routes.py` (new) | **`test_capture_refuses_without_the_live_browser`** (no endpoint → exit 2, nothing written); **`test_capture_switches_device_by_preset_and_never_resizes`** (a fake `swarm-handoff` records `device phone-390`; a fake page asserts `set_viewport_size` is never called); **`test_capture_sets_theme_by_emulated_media`**; **`test_capture_writes_the_tarball_when_a_route_fails`** (one route's ready selector times out: the tar exists and `failed` names it); **`test_file_names_carry_route_preset_theme_segment`**; **`test_diff_reports_ratio_and_writes_a_diff_png`**; **`test_every_sections_tab_is_in_a_route_group`** (reads `SECTIONS` in `App.tsx`; fails on a tab added without a route) | QA-KIT (the preset name), QA-FLOWS (the Dockerfile) |
| 4 | **QA-TEMPLATE** (new) | `docs/qa/console-qa.workflow.json` (new); `docs/browser-qa.md` (new: §1.3 and §1.4 rewritten for users); one link line in `docs/workflows.md`; `tests/unit/mcp/test_qa_workflow_template.py` (new) | **`test_template_reads_as_a_spec`** (`read_spec` and the API's `WorkflowCreate` accept it); **`test_template_captures_every_route_group`** (one capture and one review step per group in `routes.json`); **`test_template_report_reads_every_review_and_flow`**; **`test_template_names_only_profiles_inputs_and_classes`** (no `resource_class`, every `browser_tool` in the declared choices, every `model_class` in `MODEL_CLASSES`) | QA-CAPTURE |
| 5 | **QA-PROOF** (operator, on dev) | none: submit the template, read the report | the run's own report: each group's `reviewed` equals its manifest's image count; one GIF per flow; `flow-submit-form`'s screenshot descriptions match its PNGs. A second run on the same commit: `swarm_qa diff` of the two captures reports a changed-pixel ratio of 0 for every image, which is the measurement that the capture is deterministic | everything above; for the signed-in arm, the QA-IDENTITY build |

The red-first shape is CLAUDE.md's: each new lane pushes its tests alone to the
PR branch, lets CI run them red, then pushes the fix to `<branch>-fix`.

---

## 7. Owner questions

**Answered by the owner, 2026-10-19 ~22:30Z (Claude Code session f050e91d):** design B, with scripted capture and Opus review, and QA-CAPTURE is built. Each question is answered with its recommendation:

- Q1: Opus for review and report.
- Q2: build QA-CAPTURE.
- Q3: compare within one run for now.
- Q4: design C not now, so requests A and B are not filed.
- Q5: Jev only on the fixture build, and a QA identity without platform-admin.
- Q6: on demand.
- Q7: report only, with findings filed as one epic per run.

Each has a recommendation, first. They are also in the task's
`questions.json`.

**Q1. Which model class reviews the screenshots?**
1. **`opus` for review and report (recommended).** The review is the product:
   #1038's 111 findings came from a model looking at renders. About $9-15 of
   the run's $16-26.
2. `sonnet`: about 40-50% less on those steps; weaker on fine layout.
3. `fable`: 2.5 times `opus` per token (`docs/workflows.md:434`); only if
   `opus` misses defects a person then finds.

**Q2. Build QA-CAPTURE (the `swarm_qa` tool and the route list), or let each
run's agent write its own capture loop?**
1. **Build it (recommended).** Determinism is what makes a regression
   comparison mean anything, and the route list's test is what makes a new tab
   impossible to skip.
2. Do not: the template's capture prompts describe the loop and the agent
   writes it. No new lane; two runs cannot be compared, and coverage is
   whatever the agent did.

**Q3. Where does the baseline for a regression comparison come from?** No step
can read an earlier workflow's artifacts today (§2.3).
1. **Not yet; compare inside one run (recommended for now).** The run
   compares the app with the mockups (served over `http`, QA-FLOWS) and its
   1440 renders with its 390 ones. A person compares two runs' reports in the
   console. Then decide 2 or 3 after QA-PROOF.
2. A platform feature: a step names an earlier workflow's step and file in
   the same tenant, staged like `input_from`. Clean and tenant-scoped; a
   swarm-api change and a frozen-contract request, with its own design.
3. Baselines in git: the report step's capture tarballs committed to a
   dedicated branch of the repository, read by a compare step. No platform
   change; about 20 MB of PNGs per refresh, forever, in a public repository's
   history.

**Q4. Plan for design C (the scripted runner against a hosted fixture build)
for a nightly sweep of main?**
1. **Not now (recommended).** Revisit if the QA run becomes nightly and its
   capture cost matters; it saves cents of haiku per run today.
2. Yes: accept §5 requests A and B, and add a lane for a fixture build flag and
   a hosted origin. It publishes the console's screens (with fixture data) at
   a public address.

**Q5. When the run signs in to a real console, may page text go to jev's judge
and may an admin identity screenshot other tenants' pages?**
1. **Fixture build only for jev, and a QA identity without platform-admin
   (recommended).** Real-console runs use `browser_tool: playwright`, and
   Admin pages are captured only on the fixture build. Keeps invariant 9 as
   it is.
2. Jev on the real console with the internal decision API only (never
   TypeSafe), still no admin identity.
3. Both allowed, with the run in a dedicated QA workspace whose artifacts only
   admins can read.

**Q6. How often does the QA run run?**
1. **On demand (recommended)**, by the operator or a lane that changed
   `apps/swarm-ui`: about $16-26 a run.
2. Weekly on main, as a schedule: about $70-115 a month.
3. Nightly on main: about $490-790 a month.

**Q7. What does the run do with its findings?**
1. **Report only (recommended).** The operator files them as one epic, one
   comment per finding, the way CLAUDE.md's issue rules say; the agent has no
   forge write to file them itself.
2. The report step also opens a pull request adding `findings.json` under
   `docs/qa/runs/`, so findings are in git; one PR per run.
