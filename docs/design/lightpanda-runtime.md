# Lightpanda as a runtime for browser agents

**Status:** analysis and design, 2026-10-31 (lane LIGHTPANDA-ANALYSIS, part of
#2688); partly built, as read on main on 2026-10-31. Its one contract change,
request 127 in
[`docs/contract-change-requests.md`](../contract-change-requests.md), is
accepted and applied: the frozen catalogue has `claude-code-lightpanda`.
**Landed:** the `agent-runtime-lightpanda` image, pinned, self-tested,
carrying its AGPL notices (L2); the build, push, warm and mirror scripts that
build it and stage its Corresponding Source into the release bundle (L3); the
claude-code runner's branch that starts and supervises `lightpanda serve` on
127.0.0.1:9223, registers the `lightpanda` MCP server instead of Playwright,
denies three of its tools, adds the prompt note and never sets
`SWARM_CDP_ENDPOINT` (L4); the stream tables and the `terraform/infra/locals.tf`
mirror with its `runner:` pool (L5); the console's Submit entry and fixtures
(part of L9); and the docs (L10: [worker-images.md](../worker-images.md),
[browser-qa.md](../browser-qa.md), [browser-egress.md](../browser-egress.md),
[execution-backends.md](../execution-backends.md),
[mirrored-values.md](../mirrored-values.md)). **Not landed:** the stated pool
limit in the environments' tfvars and the sizing proposal (L6; until then the
`runner:claude-code-lightpanda` pool takes `pool_limits.global`); the weekly
scan's image list in `.github/workflows/security.yml` (L7, owner-held); the
parity section of §6.5 (L8); the console's one-line profile hint and its
vitest (rest of L9); and the dev acceptance run (L11). Where the build
differs from this text, the build is described in the docs above: the image's
`/usr/share/doc/lightpanda/` also holds `LICENSING.md` and `FONTS-LICENSE`.

**Owner's ask (BGD, 2026-10-31):** "do a detailed analysis of this project and
determine if this would provide additional value or a new capability if we
build a runtime for browser agents using lightpanda-io browser, in parallel
with having playwright and jev-browser flavors. If there is additional value,
plan on designing and then implementing this additional capability in
swarmcloud." BGD is away and said to go with the recommended options, so every
decision below ends in one recommendation.

**Verdict, in one paragraph.** Build it, as a **new runner profile,
`claude-code-lightpanda`, on the existing `standard` class with no display
container**, chosen by the caller by name. On 17 real pages measured here,
Lightpanda 1.0.2 returned the same links as headless Chromium on every page.
It used a median **48 MiB against 454 MiB** of proportional memory (PSS),
**0.30 against 4.61 CPU-seconds**, and reached DOM ready in **253 against
470 ms**. Ten pages opened at once took **183 MiB, 3.0 CPU-s and 1.7 s**,
against 1,831 MiB, 37.3 CPU-s and 15.1 s for Chromium.

What is new is that browser work which needs no pixels can run **outside the
`browser` pool**, which is 20 units in prod against 100 for `standard`. It can
also run **dozens of pages in parallel inside one agent**, which the live
browser's display share cannot hold.

Lightpanda cannot replace Chromium for anything visual. It has no live view
and no take-control. In 1.0.2 it loaded **no iframe content** (3 of 3 pages),
and `Page.captureScreenshot` **crashed the whole process** on github.com (3 of
3 tries). The live-browser profile and its four `browser_tool` choices stay
exactly as they are.

---

## 1. What SwarmCloud has today

### 1.1 The one browser profile and its class

* **`claude-code-browser`** (`apps/common/swarm_common/profiles.py:2096-2117`,
  frozen) runs the claude-code runner on image `agent-runtime-browser`.
  * class `browser`, `backend=GKE_AUTOPILOT`, `live_browser=True`;
  * secret slots `files` and `cf-access`, timeout 7,200 s;
  * inputs: claude-code's, plus `browser_tool`.
* **The `browser` class** (`profiles.py:136-140`) is 4 CPU requested (limit 8),
  8 GiB memory (request = limit), 4 GiB disk and **1 unit**. That is the same
  size and the same unit count as `standard`.
  * The difference is the split. One live-browser pod is **two containers**:
    the worker (2 CPU request / 4 limit, 3 GiB) and the `display` sidecar
    (2 CPU / 3 limit, 5 GiB), from `_DEFAULT_LIVE_SPLIT`, `profiles.py:244-249`.
  * The display sidecar runs Xvfb, a headed Chromium on a CDP pipe, the live
    gate and its RFB capture (`apps/agent-worker/agent_worker/livegate/__main__.py:1-50`).
  * The image's 2 GiB `/dev/shm` tmpfs is charged to memory
    (`terraform/infra/locals.tf:236-239`).
* **Units:** `units_for(cpu, mem) = max(ceil(cpu/4), ceil(mem/8))`
  (`profiles.py:178-182`), so **no class can cost less than 1 unit**. A lease
  takes every pool at once (invariant 2): `global`, `tenant:<t>`,
  `resource:<class>`, `runner:<profile>` and `backend:<b>`
  (`models.py:95-120`).
* **The scarce pool is `resource:browser`.** Prod has `standard = 100`,
  `browser = 20`, `large = 8` (`terraform/environments/prod/prod.tfvars:72-74`).
  So at most 20 browser agents run at once in prod, whatever the rest of the
  platform is doing.

### 1.2 The four `browser_tool` choices

A caller picks `input.browser_tool`, one of `auto`, `playwright`,
`dev-browser` or `jev` (`profiles.py:1790-1799`; `browser_tools.py:56-60`).
Every choice reaches the **same** live Chromium through the gate's CDP proxy
on `127.0.0.1:9222`. Each tool carries a client tag in its endpoint path
(`browser_tools.tag_endpoint`, `browser_tools.py:138-148`;
`livegate/cdpproxy.py:185-196, 347-359`). Playwright MCP is registered on
every choice (`browser_tools.choose`, `browser_tools.py:106-135`).

| choice | what it is | what it is for | extra capacity |
|---|---|---|---|
| `playwright` | Playwright MCP (`playwright-mcp` 0.0.83, stdio, `--cdp-endpoint .../client/playwright`) | step-by-step: snapshot, click, type, screenshot | none beyond the pod |
| `dev-browser` | a CLI on PATH (`dev-browser` 1.0.0-rc.3, sha256-pinned) driving the same Chromium | fast scripted multi-step flows | none |
| `jev` | jev-browser MCP 0.2.0, judged by TypeSafe or an internal backend | goal-driven clicks and reading long pages | page text leaves for TypeSafe unless internal |
| `auto` | Playwright and dev-browser, plus jev when it is ready | the agent picks per step | none |

All four cost the same: **one `browser`-class pod, 1 unit in
`resource:browser`, plus the display sidecar's 2 CPU / 5 GiB.** The console's
picker (`apps/swarm-ui/src/BrowserToolPicker.tsx:54-66`) labels them "Auto",
"Playwright", "dev-browser" and "Jev", with these hints:

* Auto: "the agent picks per step";
* Playwright: "step-by-step browser tools";
* dev-browser: "fast scripted multi-step flows";
* Jev: "goal-driven clicks and reading long pages — sends page text to TypeSafe".

### 1.3 What the live browser gives that nothing else does

* the live view: framebuffer capture, RFB and the console's noVNC;
* take-control and the hand-off for a sign-in or a CAPTCHA (`swarm-handoff`);
* device presets and theme;
* flow GIFs and screenshots;
* the certificate rule and the security-state watch in the gate;
* QA targets' visual capture. `qaspec.py:267` runs `swarm_qa capture --crawl`
  on `claude-code-browser`, and goal-driven flows use `playwright` or `jev`
  (`qaspec.py:205-206`).

Every one of these needs pixels or a person at the screen.

---

## 2. What Lightpanda is

Source read at `lightpanda-io/browser` commit `d3f56c14` (main, 2026-10-31).
Binary measured: release **1.0.2** (2026-10-23).

* **Language and engine.** Zig, with these engines:
  * **V8** for JavaScript;
  * **html5ever** (Rust) for HTML;
  * **libcurl** for HTTP.

  It has its own DOM and Web API implementation (`src/browser/`). README
  "Status" lists: CORS, XHR, Fetch, cookies, custom headers, proxy, network
  interception, `robots.txt`, CDP and WebDriver BiDi, and an ad-blocker.
* **It does not render pixels.** There is no compositor or GPU raster and no
  image decoding into a frame.
  * `--dump png` and CDP `Page.captureScreenshot` produce what the README
    calls "a text-only rendering of the page". Measured here, it is the text
    and links in a default stylesheet, with no colours, images or page CSS
    layout (`src/browser/screenshot.zig`).
  * That rules out visual review, screenshots as evidence, the live view,
    take-control, flow recordings, device presets as a layout check, and
    canvas or WebGL output.
  * 1.0.2 resolves widths and `getBoundingClientRect` for block, inline,
    floats and basic flex (release notes). That is enough for scripts that
    read geometry, not for judging a layout.
* **CDP server.** `lightpanda serve --host 127.0.0.1 --port N` serves
  WebSocket CDP, and optionally WebDriver BiDi on the same port. Defaults are
  16 connections, a 1 MiB message cap, and `--unix-socket` as an option.
  * It implements 22 CDP domains (`src/server/cdp/domains/`): Accessibility,
    Audits, Browser, Console, CSS, Debugger, DOM, Emulation, Fetch, Input,
    Inspector, IO, Log, Network, Page, Performance, Runtime, Security,
    Storage and Target, plus its own `LP` and `WebMCP`.
* **Playwright `connectOverCDP`** works for navigation, `evaluate`, locators,
  `fill`, `click`, `press` and `check`. 4 of 4 form flows passed (§3).
  Known gaps, measured here:
  * **One browser context per CDP connection.** A second `newContext()` on
    one connection fails with `Target.createBrowserContext: Cannot have more
    than one browser context at a time`. Parallel work opens one connection
    per page.
  * **Frames.** `page.frames` is always 1. The DOM's
    `iframe.contentDocument` is `null`, even for a same-origin `child.html`
    served from `127.0.0.1`. `fetch --dump html` leaves the iframe empty.
  * **Screenshots** are text-only, and crashed the process on github.com (§3).
  * **`innerText` ignores CSS hiding.** Text is a superset of Chromium's:
    MDN 5,366 against 3,704 characters, react.dev 8,233 against 6,582.
* **Puppeteer** is supported upstream (README "Start a CDP server").
  Not measured here.
* **Native MCP server.** `lightpanda mcp` serves MCP over stdio or HTTP. It
  lists 32 tools, measured with `tools/list` on 1.0.2: `goto`, `search`,
  `markdown`, `html`, `screenshot`, `links`, `evaluate`, `extract`, `tree`,
  `nodeDetails`, `interactiveElements`, `structuredData`, `detectForms`,
  `click`, `fill`, `scroll`, `waitForSelector`, `waitForScript`,
  `waitForState`, `hover`, `press`, `selectOption`, `setChecked`,
  `findElement`, `consoleLogs`, `getUrl`, `getCookies`, `getEnv`, `save`,
  `session_new`, `session_list` and `session_close`. Three need care:
  * `search` calls the Brave search API (`src/browser/tools/search.zig`);
  * `getEnv` returns `LP_*` environment variables
    (`src/browser/tools/marshal.zig:379`);
  * `screenshot` is the crashing path.
* **Telemetry is ON by default.** Usage telemetry goes to
  `https://telemetry.lightpanda.io/v2` (`src/telemetry/lightpanda.zig:24`)
  unless `LIGHTPANDA_DISABLE_TELEMETRY=true` (README "Telemetry"). The update
  check (`src/Updater.zig`) runs only on `lightpanda version --check`, never
  on its own.
* **Maturity.**
  * 25 tagged releases since v0.1.0 (2026-01-07). 1.0.0 shipped 2026-10-02,
    then 1.0.1 (10-15) and 1.0.2 (10-23): **a release every one to three
    weeks**.
  * Main merges PRs daily (#4321 on 2026-10-31).
  * The 1.0.2 notes are mostly new Web APIs and behaviour fixes, so the API
    surface is still moving.
  * The open-issue count and the classes of open bugs were **not read**: the
    GitHub search calls were cut off in this session. Do not rely on any
    count from this document.
* **Platform builds.** Each release attaches `lightpanda-x86_64-linux` and
  `lightpanda-aarch64-linux`, plus `.deb` packages and macOS builds, with
  GitHub-published sha256 digests. For 1.0.2:

  | asset | bytes | sha256 |
  |---|---|---|
  | `lightpanda-x86_64-linux` | 76,408,136 | `ae7d01d261efb9e480e18ce3424533332883863bcf70b12a4321b5733f37e19e` (also computed here) |
  | `lightpanda-aarch64-linux` | 73,830,456 | `ac546dfc43c533430da92b75ab36f2a29da7d6324d5d38baf606ef2e0d849345` (from the release API; not downloaded here) |

### 2.1 The licence, quoted

`LICENSE` is the GNU Affero General Public License version 3. `LICENSING.md`
says: *"The default license for this project is AGPL-3.0-only."* SwarmCloud
itself is MIT (`LICENSE` at this repository's root). What each use obliges:

**Running it unmodified as a tool inside agent containers.** Running is not
propagation. §0: *"To 'propagate' a work means to do anything with it that,
without permission, would make you directly or secondarily liable for
infringement under applicable copyright law, except executing it on a
computer or modifying a private copy."* §9: *"You are not required to accept
this License in order to receive or run a copy of the Program."* §13's
network clause starts *"if you modify the Program"*, so it does not reach an
unmodified binary. **Running alone obliges nothing.**

**Shipping the binary in an image.** This is conveying, because SwarmCloud
publishes its runner images to Artifact Registry
`us-docker.pkg.dev/swarmcloud-prod/swarmcloud` and every install copies them
into the customer's project (`docs/design/day-one-install.md` D25). The image
therefore puts a copy of Lightpanda in another party's hands. §6:

> *"You may convey a covered work in object code form under the terms of
> sections 4 and 5, provided that you also convey the machine-readable
> Corresponding Source under the terms of this License, in one of these
> ways: ... d) Convey the object code by offering access from a designated
> place (gratis or for a charge), and offer equivalent access to the
> Corresponding Source in the same way through the same place at no further
> charge. ... If the place to copy the object code is a network server, the
> Corresponding Source may be on a different server (operated by you or a
> third party) that supports equivalent copying facilities, provided you
> maintain clear directions next to the object code saying where to find
> the Corresponding Source. Regardless of what server hosts the
> Corresponding Source, you remain obligated to ensure that it is available
> for as long as needed to satisfy these requirements."*

§4 adds that the copy must *"keep intact all notices stating that this License
... apply to the code; keep intact all notices of the absence of any warranty;
and give all recipients a copy of this License along with the Program."*

So shipping obliges SwarmCloud to do three things:

1. Put the AGPL text and a notice in the image, at
   `/usr/share/doc/lightpanda/LICENSE` and `/usr/share/doc/lightpanda/SOURCE`.
   The SOURCE file names the release tag, the upstream commit and the URL of
   the source archive.
2. Keep the Corresponding Source of that exact tag available for as long as
   the image is offered. Because "you remain obligated", **a link to GitHub
   alone is not enough**. SwarmCloud mirrors the tag's source archive next to
   the release bundle (§6.4).
3. Leave Lightpanda unmodified, so §13 never applies.

§5's aggregate clause keeps the AGPL off SwarmCloud's own code:

> *"A compilation of a covered work with other separate and independent
> works, which are not by their nature extensions of the covered work, and
> which are not combined with it such as to form a larger program, in or on
> a volume of a storage or distribution medium, is called an 'aggregate' ...
> Inclusion of a covered work in an aggregate does not cause this License to
> apply to the other parts of the aggregate."*

The worker talks to Lightpanda only over CDP and MCP, as a separate process,
which keeps it an aggregate. SwarmCloud never links, vendors or copies
Lightpanda's source into this repository.

**Modifying it.** Do not. A modified Lightpanda that the agent reaches over a
socket brings in §13:

> *"if you modify the Program, your modified version must prominently offer
> all users interacting with it remotely through a computer network (if your
> version supports such interaction) an opportunity to receive the
> Corresponding Source of your version by providing access to the
> Corresponding Source from a network server at no charge."*

It would also need SwarmCloud's patches published. If a fix is needed, it goes
upstream; the CLA is in `CLA.md`, and the image waits for the release that
carries it.

The binary statically embeds V8, libcurl and html5ever, each under its own
permissive licence. Their notices travel in the same `/usr/share/doc/lightpanda/`
directory, from the release's source tree.

This is a reading of the licence text for an engineering plan, not legal
advice. The obligations are the minimum the text states.

---

## 3. Measurements

**Where and when.** This container, 2026-10-31 at 04:25 UTC
(`results.json` `when_utc`): agent on `claude-code-browser`, x86_64,
kernel 6.12.110+, cgroup CPU limit 4 (`cpu.max 400000 100000`), memory limit
5 GiB.

**What ran.** Lightpanda 1.0.2 (the release binary, sha256 above) against
**Google Chrome for Testing 153.0.8010.12** with `--headless=new`, from this
image's `/opt/playwright/chromium-1243`. Both were driven by the **same Python
Playwright 1.63.0 script** over `connect_over_cdp`.

Chromium was started as a headless test process through `swarm-test-browsers`,
the sanctioned exception for a headless test process. The agent's live browser
was not used: it is in the display container, where its memory and CPU cannot
be read.

**Method.** The script and the raw numbers are in
[`lightpanda-runtime/bench.py`](lightpanda-runtime/bench.py) and
[`lightpanda-runtime/results.json`](lightpanda-runtime/results.json). The
script's docstring has the rerun commands. Its method:

* a fresh process per page and engine, 2 runs;
* 17 pages;
* one parallel run of 10 pages per engine;
* memory sampled every 25 ms over the browser's whole process tree;
* about one navigation a second on any one site.

**PSS, not RSS, is the memory figure to read.** Chromium is about ten
processes that share a 290 MB binary, so summed RSS counts the binary ten
times: 1.5 GiB RSS against 454 MiB PSS. Lightpanda is one process, so its RSS
and PSS agree.

**Chromium's CPU includes its start-up.** Full headless Chrome spends about
3.5 CPU-s before the first page. That is the real cost of the per-agent process
this runtime would replace, and it is why the parallel run below is the fairer
per-page figure.

### 3.1 Per page (median of 2 runs; LP = Lightpanda, Cr = Chromium)

| page | kind | start ms LP / Cr | DOM ready ms LP / Cr | PSS MiB LP / Cr | CPU s LP / Cr | links LP / Cr | text chars LP / Cr | iframes seen LP / Cr | result |
|---|---|---|---|---|---|---|---|---|---|
| example.com | static | 182 / 621 | 82 / 189 | 35 / 422 | 0.14 / 3.85 | 1 / 1 | 917 / 917 | – | same |
| docs.python.org json | static docs | 201 / 522 | 118 / 553 | 51 / 448 | 0.20 / 4.37 | 217 / 217 | 28,485 / 27,461 | – | same links |
| MDN Fetch API | docs, some JS | 250 / 575 | 232 / 611 | 72 / 491 | 0.48 / 5.65 | 187 / 187 | 5,366 / 3,704 | – | LP text includes hidden menus |
| Wikipedia article | large static | 256 / 628 | 102 / 414 | 60 / 464 | 0.35 / 4.76 | 260 / 261 | 9,294 / 8,196 | – | one link fewer in LP |
| Hacker News | server-rendered | 261 / 559 | 270 / 318 | 36 / 421 | 0.18 / 3.68 | 229 / 229 | 4,263 / 4,237 | – | same (Cr run 2 got an 85-char refusal page from HN: rate limit, not engine) |
| books.toscrape | scraping, static | 239 / 627 | 338 / 480 | 42 / 438 | 0.18 / 4.30 | 94 / 94 | 1,889 / 2,029 | – | same links |
| quotes.toscrape /js | content built by JS | 266 / 573 | 260 / 449 | 39 / 428 | 0.17 / 4.02 | 5 / 5 | 1,489 / 1,499 | – | same |
| github.com repo | heavy SSR + JS | 245 / 752 | 1,116 / 1,862 | 108 / 603 | 1.52 / 10.05 | 297 / 297 | 19,073 / 16,851 | – | **LP screenshot crashed the process (2/2)** |
| react.dev | Next.js | 250 / 556 | 378 / 684 | 84 / 497 | 0.82 / 5.81 | 98 / 98 | 8,233 / 6,582 | – | same links |
| TodoMVC React | client-only SPA | 208 / 662 | 194 / 319 | 54 / 454 | 0.41 / 4.75 | 14 / 14 | 657 / 657 | – | add-todo flow passed on both |
| Playwright TodoMVC | client-only SPA | 328 / 554 | 161 / 371 | 40 / 443 | 0.30 / 4.32 | 3 / 3 | 148 / 148 | – | add-todo flow passed on both |
| angular.dev | Angular SPA | 217 / 709 | 344 / 1,002 | 129 / 572 | 1.69 / 9.65 | 41 / 41 | 2,985 / 3,093 | – | same links |
| the-internet /login | login form | 233 / 631 | 359 / 585 | 50 / 464 | 0.37 / 5.15 | 3 / 3 | 261 / 262 | – | refused sign-in flash on both (values from `secrets.token_hex`) |
| httpbin /forms/post | form POST | 256 / 641 | 96 / 202 | 38 / 442 | 0.24 / 4.29 | 0 / 0 | 195 / 207 | – | POST echo carried the typed name on both |
| the-internet /iframe | TinyMCE iframe | 324 / 821 | 434 / 600 | 60 / 480 | 0.59 / 5.52 | 4 / 4 | 92 / 133 | **0 / 1** | **LP never loads the iframe** |
| the-internet /nested_frames | frameset | 217 / 624 | 106 / 225 | 31 / 433 | 0.14 / 4.37 | 0 / 0 | 0 / 0 | **0 / 5** | **LP never loads the frames** |
| the live deployment's console | IAP sign-in redirect | 278 / 674 | 246 / 478 | 43 / 473 | 0.21 / 4.85 | 8 / 8 | 989 / 987 | 0 / 1 | same text; LP misses Google's hidden iframe |

The "iframes seen" column counts frames other than the main one in Playwright's
frame tree. The DOM-side count (`iframe.contentDocument` text) was 0 on every
Lightpanda page. A same-origin local probe, `parent.html` with
`<iframe src=child.html>` on `127.0.0.1`, gave `contentDocument === null`, and
`frameLocator('#f')` timed out.

**Medians over all 34 page-runs per engine:**

| | Lightpanda | Chromium | ratio |
|---|---|---|---|
| start | 239 ms | 626 ms | 2.6× |
| DOM ready | 253 ms | 470 ms | 1.9× |
| PSS peak | 48 MiB (max 132) | 454 MiB (max 612) | 9.5× |
| CPU | 0.30 s | 4.61 s | 15× |

**Correctness, summed:**

* **Links:** equal on 16 of 17 pages; Wikipedia gave 260 against 261.
* **Markers:** every page's known string came out on both engines.
* **Forms:** 4 of 4 flows passed on both.
* **Text:** a superset on Lightpanda (hidden text included).
* **Iframes:** 0 of 3 on Lightpanda.
* **Screenshots:** 32 of 34 page-runs (16 of 17 pages) succeeded on
  Lightpanda, all text-only renderings. The 2 failures were both github.com
  runs, each a process crash (`TargetClosedError`, and
  `process_alive_at_end: false`). Chromium took 34 of 34, all real pixels.

### 3.2 Ten pages at once in one process

The ten were the first ten pages above, one CDP connection and context each.

| | Lightpanda | Chromium | ratio |
|---|---|---|---|
| wall time to all ten DOM-ready | 1.7 s | 15.1 s | 8.9× |
| PSS peak | 183 MiB | 1,831 MiB | 10× |
| CPU | 3.0 s | 37.3 s | 12× |
| pages with text | 10 / 10 | 10 / 10 | |

### 3.3 Not measured here

* arm64 (only the published digest is used);
* Puppeteer;
* the open-issue classes (§2);
* token size of Lightpanda's `markdown` tool against Playwright MCP's
  snapshot, so this document claims no saving in model context;
* pages behind a real sign-in;
* behaviour under the platform's browser egress policy
  (`docs/browser-egress.md`): the bench used this pod's ordinary egress;
* an agent end-to-end on a SwarmCloud profile, since none exists yet.

Upstream's own benchmark (README "Benchmarks": 100 pages on an m5.large,
123 MB against 2 GB, 5 s against 46 s) is **not measured here**. It agrees in
direction with §3.2.

---

## 4. The value question

**Lightpanda serves at least as well as Chromium:**

* DOM reading and scraping of server-rendered and JS-built pages (11 of 11
  such pages);
* link checks (identical links);
* form flows that need no visual judgement (4 of 4);
* API-backed page checks (`evaluate`, network interception);
* QA of server-rendered pages where the check is text, links or form
  behaviour;
* **many pages in parallel.**

**It cannot serve:**

* anything visual: screenshots as evidence, visual QA, layout and theme review;
* the live view, take-control, and any hand-off. Nobody can solve a CAPTCHA
  or sign in for it, so a site that needs a person is out of reach;
* device presets as a layout check, and flow GIFs;
* pages whose content is in an iframe (embedded editors, payment fields,
  most third-party sign-in widgets);
* `Page.captureScreenshot` on heavy pages (it crashes the process);
* any agent where hidden text must be told from visible text.

**What a lighter runtime saves, in SwarmCloud's own units:**

* **Units do not shrink.** `units_for` is at least 1 per agent, so a Lightpanda
  agent costs **1 unit**, like a browser agent. A smaller class (say 2 CPU /
  4 GiB) would still be 1 unit. It would halve the GKE request but squeeze
  the Claude Code CLI, which #1926 found OOM-killed at 8 GiB. **No new class.**
* **The pool moves.** On `standard`, a Lightpanda agent leases
  `resource:standard` (100 in prod), not `resource:browser` (20). DOM-only
  browser work stops queuing behind visual work, and 20 browser slots stay for
  agents that need a screen.
* **The display share goes to the agent.** The browser class gives Chromium,
  Xvfb and capture 2 CPU / 5 GiB of the pod. A `standard` pod gives the whole
  4 CPU / 8 GiB to the worker, and Lightpanda takes 48-132 MiB a page from it.
  * That 5 GiB display share would hold about **38 Lightpanda processes at
    the worst page measured (132 MiB), or about 100 at the median**.
  * Against that, 2 to 3 Chromium processes at 450-600 MiB fill it before
    their tabs are counted.
  * Ten parallel Chromium pages need 37 CPU-s, at least 12 s against the
    display's 3-CPU limit. Lightpanda does the same in 3 CPU-s.
* **Start.** No Xvfb, no gate and no Chromium to start in the pod: the
  measured start is 239 ms against 626 ms for a bare headless Chromium.
  The live gate's own start is not measured here; `docs/browser-startup-speed.md`
  has its numbers.

**What is new against what is only cheaper:**

* **New:**
  1. browser work that does not compete for the `browser` pool;
  2. one agent reading tens of pages concurrently: crawl a site, check every
     link, extract a catalogue, at 18 MiB and 0.3 CPU-s a page;
  3. Lightpanda's own MCP tools (`markdown`, `structuredData`, `detectForms`,
     `interactiveElements`), which none of the four existing tools offer as
     tools.
* **Only cheaper:** a single page read, which is 2× faster to DOM ready and
  15× less CPU. That alone would not justify a runtime.

---

## 5. Decisions

### A. Value: build it, or not

**Recommended: build it**, scoped to DOM-only work, beside the live browser
and never instead of it. The reason is the two new capabilities in §4, both
measured.

* *Not building* costs nothing now, and DOM-only work keeps queuing on the 20
  browser slots and paying for a display it never shows.
* The risk of building is a young dependency (1.0 is four weeks old) with
  measured gaps: iframes, the screenshot crash, and one context per
  connection. The design contains it:
  * the version is pinned, and any bump reruns `bench.py` as its acceptance;
  * the screenshot tool is not offered;
  * the agent hands any page it cannot read to the live browser (decision C).

### B. Shape

| option | capacity | live view | frozen contract | invariant 7 | invariant 10 | verdict |
|---|---|---|---|---|---|---|
| **(1)** a fifth `browser_tool`, `lightpanda`, in `claude-code-browser` | unchanged: still a `browser` unit and the 2 CPU / 5 GiB display, so nothing is saved | the pod keeps one, so the agent "has" a live view the tool cannot show | `browser_tool` choices, `RUNTIME_TOOLS`, parity §16, the API's `Literal`s, the gate's client tags | unchanged | holds (a name) | **not built.** It adds a choice that saves nothing, and its agents still hold Chromium, so "never receives Chromium" cannot be true of it |
| **(2)** a new profile, `claude-code-lightpanda`, on `standard`, one container, no display | 1 unit in `resource:standard`; frees the browser pool | none, and the profile says so (`live_browser=False`) | one catalogue entry (request 127), mirrored as `claude-code-large` was (request 113) | `standard` already holds it: memory request = limit, CPU 4 ≤ limit 8 ≤ 2×4 | holds: a caller names the profile; image and command come from the catalogue | **recommended** |
| **(3)** both | (2)'s, plus (1)'s cost | | both changes | | | not now: (1) has no measured value of its own |
| **(4)** a `browser` input on `claude-code` (Lightpanda in `agent-runtime-base`) | as (2) | none | changes claude-code's inputs | holds | holds | **not built.** It ships an AGPL 76 MB binary in every claude-code image, and every claude-code agent carries the licence notice and source obligation |
| **(5)** a smaller class, e.g. `lite` 2 CPU / 4 GiB | still 1 unit (`units_for` floor) | none | a new class, units, admin bounds | needs its own split | holds | **not built.** No unit saving, and it risks the CLI's memory (#1926) |

**Recommended: (2).** It is the only shape that saves real capacity
(`resource:browser`), and the only one where "this task never receives
Chromium and never claims a live view" is true by construction. It also costs
the least contract. The profile is claude-code's runner with a different image
and one runner branch. Backend is GKE Autopilot, like `claude-code`: Cloud
Run's start (p50 128 s, `profiles.py` request 53 note) is what claude-code
left.

### C. How it is chosen

**Recommended: by the caller only, by profile name.** The caller names the
profile at submission (invariant 10), or a workflow step names it, or a parent
agent names it for a child.

`auto` is not involved: it is a `browser_tool` value inside
`claude-code-browser` and resolves tools within the live browser. No platform
rule rewrites an agent's profile. The lease was taken for the named profile's
pools all-or-nothing (invariants 2 and 3), and silently moving an agent to
`browser` would spend the scarce pool without the caller's choice.

**The fallback when a page fails in Lightpanda** is the agent's, and the
runner's prompt states it. A page has failed when any of these holds:

* navigation errors;
* the Lightpanda process exits;
* the body is empty;
* the page's content is in an iframe;
* it shows a sign-in, a CAPTCHA, a bot check or a consent wall;
* the agent needs something visual.

The agent then **submits a child agent on `claude-code-browser`** through
`.swarm-children` (`runner_profile` named, the URLs and what to do with them
in the prompt). It waits for the child's result and says in its answer which
pages went to the live browser and why.

The children mechanism already exists and already takes a profile by name
(`.swarm-children/README.md`), so the fallback needs no new platform path.
The person sees the child in the console like any other.

**Alternative, not recommended:** have the platform re-submit a failed
Lightpanda agent on `claude-code-browser`. It repeats the whole agent for one
bad page, and it needs a new end cause and a new scheduler rule.

### D. Packaging

**Recommended: a new image, `agent-runtime-lightpanda`.** It is built `FROM`
`agent-runtime-base` (by digest, as `agent-runtime-browser` is built from the
base) plus one layer:

* `ARG LIGHTPANDA_VERSION=1.0.2`;
* `ARG LIGHTPANDA_SHA256_AMD64=ae7d01d2…37e19e` and
  `ARG LIGHTPANDA_SHA256_ARM64=ac546dfc…849345`, in full in the Dockerfile;
* the binary fetched through the build's tool mirror and
  `sha256sum -c`-checked, the pattern `DEV_BROWSER_SHA256` uses
  (`images/agent-runtime-browser/Dockerfile:221-229`), and listed in
  `images/agent-runtime-base/tool-mirror.sha256`;
* installed at `/opt/swarm/lightpanda/lightpanda`, mode 0755, owned by root;
* `ENV LIGHTPANDA_DISABLE_TELEMETRY=true` and `LIGHTPANDA_DISABLE_CORE_DUMP=1`.
  A core dump of a page process could hold page data in the workspace;
* `/usr/share/doc/lightpanda/{LICENSE,SOURCE,THIRD_PARTY}`. LICENSE is the
  AGPL text from the tag. SOURCE names the tag, the commit and the mirrored
  source archive's URL and sha256;
* a build-time self-test that runs `lightpanda version`, and checks the
  output equals `LIGHTPANDA_VERSION`;
* no Chromium. `agent-runtime-base` has none (its Dockerfile:283) and this
  image adds none.

*Why not the same image as `claude-code` or `claude-code-browser`:* either
would put an AGPL binary, its notices and its source obligation into every
agent of that profile, and grow the image by 76 MB for agents that never use it.

**Updating it.** A version bump is one PR that changes the two `ARG`s, the
mirror line and the mirrored source archive. Its acceptance is three things:

* `bench.py` rerun in a `claude-code-browser` agent, with the new
  `results.json` committed beside the old one;
* the image's self-test;
* the probe tests of §6.6.

Lightpanda's release cadence (one to three weeks) does not oblige SwarmCloud
to follow. Bump when a fix SwarmCloud needs ships: iframes and the screenshot
crash are the two to watch.

**Licence, met:** (1) notices in the image (§4); (2) the Corresponding Source
for the exact tag mirrored into the release bucket, with directions next to
the object code (§6(d)); (3) never modified (§13 never applies); (4) never
linked into SwarmCloud code (§5 aggregate).

---

## 6. Design

### 6.1 The profile (contract request 127)

The text is in `docs/contract-change-requests.md` request 127. In short:

* one `RunnerProfile` literal, `claude-code-lightpanda`, right after
  `claude-code-browser` in `RUNNER_PROFILES`;
* image `agent-runtime-lightpanda`, class `standard`, `GKE_AUTOPILOT`;
* claude-code's runner argv, provider, secrets (`secrets_any_of`), timeout
  (7,200 s), inputs (`_CLI_AGENT_MCP_INPUTS`), secret slots (`files`) and
  model classes (default `opus`);
* `live_browser` left at its default, `False`.

No new class, no new input, no new `browser_tool` choice, no new state or end
cause. **Every implementation lane after the contract lane needs request 127
applied first**, except the image lane (L2) and the benchmark rerun.

### 6.2 The runner (agent-worker)

`apps/agent-worker/agent_worker/runners/claude_code.py` gains one branch on
`ctx.profile == "claude-code-lightpanda"`. A new module,
`apps/agent-worker/agent_worker/lightpanda.py`, holds the constants and pure
helpers so they are testable without the CLI.

**Before the CLI starts:**

* The runner starts `lightpanda serve --host 127.0.0.1 --port 9223
  --cdp-max-connections 64 --block-private-networks --log-level warn`, under
  `env -i`. The child's only variables are `PATH`, `HOME` (the attempt's
  cache dir), `TMPDIR`, `LIGHTPANDA_DISABLE_TELEMETRY=true` and
  `LIGHTPANDA_DISABLE_CORE_DUMP=1`.
  * The scrubbed environment means no token or secret is reachable by
    Lightpanda or by a page.
  * `--block-private-networks` keeps a page's script off the metadata server
    and the pod network. The agent's own process can still reach them, as
    today, so this narrows only what a hostile page can reach.
* Port 9223, not 9222, so nothing written for the live gate's endpoint
  connects to Lightpanda by mistake.
* The runner supervises the process. When it exits it is restarted, at most
  20 times per attempt with a 1 s floor between starts, and each exit is one
  line in the runner's log (with no environment printed).
* The runner sets `SWARM_LIGHTPANDA_CDP=http://127.0.0.1:9223` for the CLI.
  It **does not set `SWARM_CDP_ENDPOINT`**.
* `PLAYWRIGHT_BROWSERS_PATH` stays as `agent-runtime-base` has it, which is
  empty of browsers, so `chromium.launch()` fails as it does on `claude-code`.

**The MCP config.** `write_mcp_config` gains a `lightpanda` stdio server and
**no `playwright` server**:

* command `/usr/bin/env`;
* args `-i PATH=… HOME=… LIGHTPANDA_DISABLE_TELEMETRY=true
  LIGHTPANDA_DISABLE_CORE_DUMP=1 /opt/swarm/lightpanda/lightpanda mcp
  --block-private-networks`.

The CLI's argv for this profile gains `--disallowedTools
mcp__lightpanda__screenshot,mcp__lightpanda__search,mcp__lightpanda__getEnv`:

* `screenshot` crashes on heavy pages and its picture is not the page;
* `search` would call Brave without a key; the CLI's WebSearch serves;
* `getEnv` has nothing to return under `env -i`, and is denied anyway.

**The prompt note** (`LIGHTPANDA_NOTE`, in `lightpanda.py`):

> "Browser for this task: Lightpanda, a headless browser that runs
> JavaScript but draws no pixels. Read and act on pages with the lightpanda
> tools (goto, markdown, links, tree, interactiveElements, click, fill,
> press, waitForSelector). For many pages at once, script Playwright from
> Python: `playwright.chromium.connect_over_cdp(os.environ["SWARM_LIGHTPANDA_CDP"])`,
> one connection per page (Lightpanda allows one browser context per
> connection). There is no live view, no screenshot and no hand-off here.
> Lightpanda does not load iframes, and a page's text includes text the page
> hides. For a web search, use the WebSearch tool. When a page fails here (an
> error, an empty page, content in an iframe, a sign-in, a CAPTCHA or a
> consent wall, or anything you must see), do not work around it: submit a
> child agent on runner_profile `claude-code-browser` through
> $SWARM_CHILDREN with those URLs and what to do there, wait for it, and say in your answer which pages went there and
> why. On any one site, keep to about one navigation a second."

**Status.** `browser_tools.write_state` is **not** called, so no
`browser.json` is written. The console's live-browser status is never
reported for this profile.

### 6.3 Control plane and console

* **`apps/agent-worker/agent_worker/runners/streams.py:73-74`** and
  **`apps/swarm-api/swarm_api/agent_streams.py:53`**: the new profile runs
  the claude-code CLI, so it joins the tuple of profiles whose stream files
  are claude-code's.
* **`terraform/infra/locals.tf`**: a `"claude-code-lightpanda"` entry in
  `runner_profiles`, copied from `claude-code-large`'s shape. That means
  image `agent-runtime-lightpanda`, class `standard`, `GKE_AUTOPILOT`, the
  `anthropic` secret map, 7,200 s and claude-code's model classes. The
  comment cites request 127. The entry also creates the
  `runner:claude-code-lightpanda` pool. It is not in
  `cloud_run_fallback_profiles`.
* **Pool limits.**
  * `terraform/environments/{dev,prod}/{dev,prod}.tfvars` `runner_profiles`
    gain `claude-code-lightpanda = 20`. The comment next to the value says:
    "DOM-only browser agents on `standard`; 20 matches `browser` so moving
    work here never lowers the browser ceiling".
  * The sizing proposal (`state.outputs["sizing"].pool_limits.runner_profiles`,
    lane SIZING-ASK's code) gains the same key with the same proportion as
    `claude-code-browser`.
* **`apps/swarm-ui/src/Submit.tsx`**: an entry
  `'claude-code-lightpanda': [{ name: 'prompt', kind: 'text', note: 'the whole instruction, passed as one argument' }]`
  with the comment "claude-code's runner on agent-runtime-lightpanda: a
  headless browser with no screen (contract request 127)".
* **The profile's one-line hint** wherever the console describes profiles:
  "reads and fills web pages without a screen — no live view, no screenshots,
  no iframes; pages it cannot read go to a live-browser child".
* **`BrowserToolPicker.tsx` does not change.** That picker belongs to
  `claude-code-browser`, whose choices do not change. Choosing the new
  profile shows no browser-tool picker.
* **`apps/swarm-ui/src/api.ts`**: the static input-contract and availability
  mirrors (around 4529-4595) gain the profile's entry, as `claude-code-large`'s.
* **`apps/swarm-mcp/swarm_mcp/profiles.py`**: if it enumerates profiles by
  name, the same entry. Lane L4 checks and says.

**No file under `apps/swarm-ui` names the company or the live deployment's
hostname** (guard test). The console text above names neither.

### 6.4 Image, catalogue and release

* `images/agent-runtime-lightpanda/{Dockerfile,cloudbuild.yaml,selftest.py,THIRD_PARTY}`
  (new).
* `images/agent-runtime-base/tool-mirror.sha256`: two lines, amd64 and
  arm64, held equal to the Dockerfile's pins by the existing test pattern
  (base Dockerfile 743-751).
* `scripts/build-images.sh:171` and `scripts/push-images.sh:77`
  `ALL_TARGETS`: add `agent-runtime-lightpanda`.
* `scripts/warm-jobs.sh:109` `RUNNER_IMAGES`: add it.
* `scripts/lib/build-mirror.sh`: fetch the release asset and **the tag's
  source archive**, `https://github.com/lightpanda-io/browser/archive/refs/tags/1.0.2.tar.gz`.
  The source archive's sha256 is recorded at the first mirror run, since it
  was not computed here.
* The release bundle in `gs://swarmcloud` carries
  `third-party/lightpanda/1.0.2/source.tar.gz` next to the image. That is the
  §6(d) "equivalent access" location named in the image's SOURCE file.
* **`terraform/infra/images.json`** lists platform and gated images. Runner
  images are derived from the profiles' `image` (`locals.tf:436-446`), so it
  likely needs no line. Lane L2 confirms.

### 6.5 The parity script

`scripts/lib/check-contract-parity.sh` gains a section after §16. It takes
`claude-code-lightpanda` from the frozen catalogue and fails unless:

1. the profile's `live_browser` is false and its `inputs` have no
   `browser_tool`;
2. `terraform/infra/locals.tf` has the entry with the same image, class and
   backend (the existing catalogue comparison at ~2015-2051 covers this once
   the entry exists; the new section asserts the profile is present);
3. `agent_worker/lightpanda.py` `CDP_PORT` is not 9222;
4. `agent_worker/lightpanda.py` `DISALLOWED_TOOLS` contains `screenshot`;
5. the image's Dockerfile pins `LIGHTPANDA_SHA256_AMD64`, and the same hash
   is in `tool-mirror.sha256`.

### 6.6 Tests (all offline: no cloud credentials, no emulator)

| test file | holds | red first because |
|---|---|---|
| `tests/unit/common/test_claude_code_lightpanda.py` | the profile exists; every field but `name`, `image` and `secret_slots` equals `claude-code`'s; class `standard`; `live_browser` false; no `browser_tool` input; units 1 | the profile does not exist |
| `tests/unit/worker/test_lightpanda_runner.py` | **a `claude-code-lightpanda` agent never receives Chromium and never claims a live view**: with a fake `RunnerContext` for the profile, the env handed to the CLI has no `SWARM_CDP_ENDPOINT`; the MCP config has `lightpanda` and no `playwright`; the argv has no `--cdp-endpoint` and no `9222`; `browser_tools.write_state` is never called (spy); the prompt note says "no live view"; a submitted `browser_tool` is refused at validation (the profile declares none) | the branch does not exist |
| same file | the `serve` child's env is exactly the allow-list (`PATH`, `HOME`, `TMPDIR`, the two `LIGHTPANDA_*`), and is given no variable from a parent env seeded with a fake token built by `secrets.token_hex`; `--block-private-networks` is on both commands; `--disallowedTools` names the three tools | as above |
| same file | the supervisor restarts a fake `serve` that exits, stops after 20, and logs one line per exit with no env | as above |
| `tests/unit/worker/test_lightpanda_image.py` | the Dockerfile pins version and both sha256s; the hashes equal `tool-mirror.sha256`; `ENV LIGHTPANDA_DISABLE_TELEMETRY=true`; the LICENSE and SOURCE files are copied; no `playwright install` and no `chromium` in the Dockerfile | the image does not exist |
| `tests/unit/control_plane/test_claude_code_lightpanda_streams.py` | both stream tuples include the profile | not yet added |
| `apps/swarm-ui/src/__tests__/submit.lightpanda.test.tsx` | choosing the profile shows the prompt field, shows no browser-tool picker, and shows the hint text | no entry |
| `tests/terraform/catalogue.tftest.hcl` (existing) | the mirror includes the profile | the comparison fails until the entry exists |

### 6.7 CI, IAM, docs

* **CI.** `.github/workflows/security.yml:569` lists the images it scans. The
  new image must join that `for image in …` list. That is a workflow change,
  **held for the owner as its own lane (L7)**: the lane writes
  `workflow-lines.patch` and does not push it. Nothing else in
  `.github/workflows` needs to change; the image build runs through
  `scripts/build-images.sh`.
* **IAM: none.** The profile uses the tenant's existing GSA and secrets,
  exactly as `claude-code` does, and Lightpanda calls no Google API.
* **Docs:**
  * `docs/worker-images.md`: the image, its pins and its licence duty;
  * `docs/browser-qa.md`: when to pick which profile;
  * `docs/execution-backends.md`: the profile in the GKE list;
  * `docs/browser-egress.md`: Lightpanda's egress is the pod's own, with
    `--block-private-networks`, and it does not use the live-browser egress
    proxy;
  * `docs/mirrored-values.md`: port 9223 and the profile name;
  * this document's Status line, when the lanes land.

### 6.8 What this does not change

The live browser, its four `browser_tool` choices, `auto`, the gate, the CDP
proxy and its client tags, the `browser` class and pool, and QA targets'
visual capture are unchanged.

QA targets do not gain a Lightpanda step in this plan. A link-check or DOM
step for `qaspec.py` is a product decision for later. Its measured case is
§3.2.

### 6.9 Invariants

* **1:** unchanged. The profile creates demand only when LEASED and after.
* **2 and 3:** the lease takes the new `runner:` pool with the rest, all or
  nothing, counted from LEASED.
* **4, 5 and 8:** the claude-code runner's checkpointing and fencing are
  reused unchanged. The `serve` child is started after the fencing check and
  killed with the attempt.
* **6:** no Spot (GKE on-demand, as `claude-code`).
* **7:** `standard` holds it already: memory and disk request = limit, CPU
  4 ≤ 8 ≤ 2 × 4. There is one container, so there is no split to sum.
* **9:** the tenant's own GSA, secrets, prefix and namespace, as `claude-code`.
* **10:** the caller names `claude-code-lightpanda`. Image, command, port and
  flags come from the catalogue and the runner, never the caller.

---

## 7. Implementation plan

Lanes in dependency order. Each is about three files at most, and each has a
red-first test.

| lane | needs | territory | red-first test |
|---|---|---|---|
| **L1 LP-CONTRACT** | owner accepts request 127 | `apps/common/swarm_common/profiles.py` (request 127 only), `docs/contract-change-requests.md` (status → APPLIED), `tests/unit/common/test_claude_code_lightpanda.py` | `test_claude_code_lightpanda.py` fails: no such profile |
| **L2 LP-IMAGE** | none (can run beside L1) | `images/agent-runtime-lightpanda/` (Dockerfile, cloudbuild.yaml, selftest.py, THIRD_PARTY), `images/agent-runtime-base/tool-mirror.sha256`, `tests/unit/worker/test_lightpanda_image.py` | `test_lightpanda_image.py` fails: no Dockerfile |
| **L3 LP-BUILD-SCRIPTS** | L2 | `scripts/build-images.sh`, `scripts/push-images.sh`, `scripts/warm-jobs.sh`, `scripts/lib/build-mirror.sh` (asset + source archive) | the existing target-list tests (`tests/unit/scripts/`, found with `scripts/related-tests.sh`) extended to expect the image: red until added |
| **L4 LP-RUNNER** | L1, L2 | `apps/agent-worker/agent_worker/lightpanda.py` (new), `apps/agent-worker/agent_worker/runners/claude_code.py`, `tests/unit/worker/test_lightpanda_runner.py` | **the "never receives Chromium, never claims a live view" test**, red: the branch does not exist |
| **L5 LP-STREAMS-TF** | L1 | `apps/agent-worker/agent_worker/runners/streams.py`, `apps/swarm-api/swarm_api/agent_streams.py`, `terraform/infra/locals.tf` (+ `tests/unit/control_plane/test_claude_code_lightpanda_streams.py`) | streams test red; `tests/terraform/catalogue.tftest.hcl` red until locals has the entry |
| **L6 LP-POOLS** | L1, L5 | `terraform/environments/dev/dev.tfvars`, `terraform/environments/prod/prod.tfvars`, the sizing proposal's runner-profile defaults (lane SIZING-ASK's file; cross the track and say so in the commit) | `tests/unit/scripts/test_pool_limit_units.py` extended to expect the key |
| **L7 LP-CI (owner-held)** | L2 | `.github/workflows/security.yml:569` as `$SWARM_ARTIFACTS_DIR/workflow-lines.patch`, pushed by the operator | none in-repo; the security run scans the new image |
| **L8 LP-PARITY** | L1, L4, L5 | `scripts/lib/check-contract-parity.sh` (§6.5), its test under `tests/unit/scripts/` | the parity test red against a fixture missing the profile |
| **L9 LP-UI** | L1 | `apps/swarm-ui/src/Submit.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/__tests__/submit.lightpanda.test.tsx` | the new vitest file red: no entry |
| **L10 LP-DOCS** | L4 | `docs/worker-images.md`, `docs/browser-qa.md`, `docs/browser-egress.md` (and `execution-backends.md`, `mirrored-values.md`) | the docs guards (`tests/unit/scripts/test_docs_*`) the selection names |
| **L11 LP-ACCEPT** | all, deployed to dev | none in-repo: one `claude-code-lightpanda` agent in dev reading three of §3's pages, one iframe page that it must hand to a `claude-code-browser` child, and ten pages in parallel; then `make smoke` | the agent's result and the child's existence, recorded on #2688 |

**Needs request 127 applied first:** L4, L5, L6, L8, L9, L10 and L11.
**Does not:** L2, L3 and L7.

L2 and L3 can merge before the contract. An image no profile names is built
but never dispatched.

---

## 8. For whoever acts on this at 3am

* **Measured:** this container, 2026-10-31 04:19-04:29 UTC. Lightpanda 1.0.2
  x86_64 against Chrome for Testing 153 headless, 17 public pages, 2 runs
  each, plus one 10-page parallel run per engine. Rerun:
  `docs/design/lightpanda-runtime/bench.py`, with commands in its docstring.
* **Read, not measured:** the licence (quoted), the CDP domain list, the MCP
  tool list (taken live from the binary), telemetry, the release list, and the
  arm64 digest.
* **Not done:**
  * the open-issue survey;
  * Puppeteer;
  * arm64 runs;
  * token-size comparison;
  * an end-to-end agent.
* **If Lightpanda misbehaves in production:** set the profile's pool limit
  to 0 (`runner_profiles.claude-code-lightpanda` in the environment's tfvars)
  and apply. No new lease is taken; agents already queued on it stay QUEUED,
  which costs nothing (invariant 1); the live browser is untouched, and no
  other profile depends on this one.
