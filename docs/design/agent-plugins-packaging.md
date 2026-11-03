# Agent plugins packaging spike: gstack v1.92.55.0 measured, and the image build it supports

**Status: measured 3 November 2026; this is a spike. No shipped image changes here.** The owner's agent plugins plan
(`docs/design/agent-plugins.md`, on the open branch `agent-plugins-plan`) bakes gstack into the agent images at a
pinned version (BGD, 3 November 2026). Before any image changes, the plan asks how big gstack is, what its offered
skills need, and what breaks when it runs headless and unattended from a read-only install with `HOME` in the
attempt's workspace. This page records what was measured, how it was measured, what was not measured, and the build
it supports.

Everything below was run in a SwarmCloud agent container (Linux x86_64, glibc, Python 3.11, git, jq; not the agent
image) on 3 November 2026. The arithmetic is `scripts/gstack_packaging_measure.py`
(tests: `tests/unit/scripts/test_gstack_packaging_measure.py`). The raw reports were uploaded as the step's artifacts
(`measure.json`, `smoke-*.json`, the setup logs, and the base and browser layer file lists).

## What was measured, and how

| what | value |
|---|---|
| gstack | latest stable release tag **v1.92.55.0** (GitHub `releases/latest`, published 2026-11-02), commit `2512dd7624a474fee036326989ea0c6bb8470e69` |
| gstack source archive | `github.com/garrytan/gstack/archive/refs/tags/v1.92.55.0.tar.gz`, 23,135,451 bytes, sha256 `63c118f13ccf0d72d06d7bff56231dc08ce3da29ce405ed567224dd303cf5bd2` |
| Bun | **1.4.2**, also gstack's own tested pin (`GSTACK_BUN_TESTED` in `bin/gstack-bun-version.sh`; the security floor is 1.3.3). `bun-linux-x64.zip` from `github.com/oven-sh/bun/releases/download/bun-v1.4.2/`, sha256 `36368faef7527875d5ffa52e53cd48021741f2a83eb6208a8dd64068d422a913`, which matches the release's `SHASUMS256.txt` |
| setup | `./setup --host claude --no-browser` with `GSTACK_SKIP_FONTS=1`, `GSTACK_SETUP_SKIP_CSO_BUILD=1`, `GSTACK_SECURITY_OFF=1`, a throwaway `HOME`, and Bun's install cache in a new, empty folder |

**How the skills were run.** For each offered skill, `smoke()` in the script runs every bash block of its `SKILL.md`
that calls a gstack helper and needs nothing from a model. The run uses `bash -c`, a 60 s limit per block, and the
environment a headless agent would have:

* `HOME` is a workspace folder holding `.claude/skills/gstack` (a link to the install) and one `SKILL.md` link for
  each skill.
* `GSTACK_SESSION_KIND=unattended`.
* `GSTACK_HOME` is a temporary folder. Its `config.yaml` turns off the update check, telemetry, outside reviews and
  the design detector.
* The working directory is a new git repository with an `origin`, and the git identity comes from the environment, as
  the worker sets it.

A block is **skipped, never passed**, in two cases: it holds something a model fills in (`<base>`, `SESSION_ID`,
`[exact command]`), or it reads a variable that an earlier turn sets (`$B`, `$NEW_VERSION`). A skill with no
runnable block has the launcher named in its `allowed-tools` probed with `--help`.

**Read-only means proved.** `--read-only` writes a probe file into the install first and refuses the run if the write
succeeds. A writable run snapshots the install before and after each skill instead, and names every file a helper
wrote there. In this container the install was made read-only with mode bits (`chmod -R a-w`, as a non-root user),
so a write fails with `EACCES`. The agent image would make it root-owned on a read-only root filesystem (`EROFS`).
Both are write failures, but the `EROFS` form itself was not exercised.

## The numbers

**Setup time** (wall clock, one run each):

| how | time |
|---|---|
| a git checkout of the tag, empty Bun cache | **22.6 s** |
| the release archive (no `.git`), empty Bun cache | **23.0 s** |
| a git checkout with the CSO helper built (`GSTACK_SETUP_SKIP_CSO_BUILD` unset), Bun cache warm | **29.3 s** |

`bun install` took about 1.5 s of that and installed 148 packages from `bun.lock`. The rest is rendering the skills
and compiling the binaries.

**The whole install** is **1,064.2 MB** without `.git` (433 MB as one gzip -1 stream):

| top-level entry | MB |
|---|---|
| `node_modules` (148 packages; `onnxruntime-node` 288 MB and `onnxruntime-web` 141 MB, for the security classifier that `GSTACK_SECURITY_OFF=1` leaves off) | 572.4 |
| `browse` (two compiled binaries) | 166.9 |
| `make-pdf` | 84.4 |
| `bin` (186 helpers, one compiled) | 83.8 |
| `design` | 82.0 |
| `test` | 42.2 |
| `lib` | 13.2 |
| `scripts` | 4.1 |

**Each compiled binary** is a Bun single-file executable that embeds the Bun runtime, so each is about 81 MB:

| binary | bytes | gzip -6 | what it is | needed by an offered skill? |
|---|---|---|---|---|
| `make-pdf/dist/pdf` | 83,633,632 | 37,128,554 | PDF rendering | no (`make-pdf` is not offered) |
| `design/dist/design` | 81,532,384 | 36,723,105 | "AI-powered UI mockup generation" through an outside image model | named by the design skills, but every command it has calls an outside AI service, which is off at first; left out (see below) |
| `browse/dist/browse` | 81,417,696 | 36,693,689 | the headless browser CLI | yes: `qa`, `qa-only`, `design-review`, `design-consultation` (browser image only) |
| `bin/gstack-global-discover` | 81,335,776 | 36,669,883 | cross-project discovery | no: reached only through `gstack-doctor` and docs |
| `browse/dist/find-browse` | 81,315,296 | — | locates a browse install | no |
| `bin/gstack-cso-core` (only when CSO is built) | 82,097,632 | — | `/cso`'s trusted core | `/cso` (see below) |
| `bin/gstack-cso-launcher` (only when CSO is built) | 775,944 | — | static C launcher, created mode **0700** | `/cso` |

**The minimal set the offered skills need** has two parts, both verified by running them, not only derived:

* **Base:** 406 files, **6.74 MB** as tar (**2.13 MB** gzip). This is the 19 skills' rendered pages and sections, 100+
  helper scripts under `bin/`, and the `lib/` and `scripts/` TypeScript they run. It needs **no `node_modules` and no
  compiled binary**. Every helper that runs without a model is either bash or `bun run` on TypeScript that imports only
  Node built-ins and other gstack files. The file list is the artifact `base-layer-files.txt`.
* **Browser:** a further 594 files, **123.0 MB** as tar (**49.8 MB** gzip). These are `browse/dist/browse`, the 77
  files of `browse/src`, three `lib/` files, and these `node_modules` packages: `playwright`, `playwright-core`,
  `diff`, `socks`, `ip-address`, `smart-buffer`, `sharp` with `@img/sharp-linux-x64`, `@img/sharp-libvips-linux-x64`
  and `@img/colour`, `detect-libc` and `semver`. The compiled `browse` starts its server as
  `bun run <install>/browse/src/server.ts`, so the binary alone is not enough. The file list is the artifact
  `browser-layer-files.txt`.

How the base set was derived: `skill_closure()` starts from each skill's own directory. From there it follows every
reference that names a file in the install: the literal `~/.claude/skills/gstack/<path>` the rendered skills use,
`$VAR/<path>` in helpers, bare `gstack-*` helper names, and relative TypeScript imports. Comment lines are dropped
before matching. Markdown outside the offered skills (README, CLAUDE.md, docs) is kept as a leaf but not followed.
The walk never follows:

* `.tmpl` sources;
* installers and upgraders: `setup`, `gstack-upgrade`, `gstack-relink`, the skill renderer, Capy's installer and
  `gstack-doctor`, which launches every binary;
* pages of skills that are not offered.

Every cut edge is listed in the report.

Without those cuts, the walk pulled in all four binaries through `gstack-upgrade/SKILL.md` and `gstack-doctor`. That
is why the result was then checked by running it:

* a read-only install holding **only** the base set runs the same 77 blocks with the same results as the full
  install;
* a read-only install holding base plus browser drives Chromium (below).

**What each image would gain:**

| image | added, uncompressed | added, gzip | what |
|---|---|---|---|
| `agent-runtime-base` (about 730 MB today) | **≈ 86.2 MB** | **≈ 38.8 MB** | Bun 1.4.2 (79.5 MB, 36.7 MB gzip) plus the base set (6.7 MB, 2.1 MB gzip) |
| `agent-runtime-browser` (about 1.25 GB today; built `FROM` the base) | ≈ 86.2 + **123.0 MB** | ≈ 38.8 + **49.8 MB** | the base's share plus the browser set; Chromium is reused, so none is added |
| `agent-runtime-lightpanda` (built `FROM` the base) | ≈ 86.2 MB | ≈ 38.8 MB | the base's share; no browser set |
| for comparison: the whole install | 1,064 MB | 433 MB | what copying the install as is would cost every agent start |

Building `/cso` would add another ≈ 82.9 MB to the base (its core binary and launcher). That is not proposed; see
the findings.

**Start time before and after: not measured.** Measuring it needs an image built and dispatched; this container
builds no image, and the brief changes no shipped image. The number belongs to the build lane, measured the way
`scripts/startup_measure.py` measures `pull` (`image_pulling` to `image_pulled`) on the deployment. Until then it is
unknown, not zero. The size above is the input to it: about 39 MB more to pull for every base-image start, and about
89 MB more for every browser start.

## The smoke run: what ran, and what broke

77 of the 271 bash blocks in the 19 offered skills ran without a model, plus `/cso`'s launcher probe. The other 194
were skipped: 58 call no gstack helper (plain git and shell steps) and 136 need a model's input or an earlier turn's
variable. The
results were identical, apart from paths and temporary names, in all three runs: a writable full install, a read-only
full install, and a read-only install of only the base set.

| skill | blocks | ran | skipped | failed |
|---|---|---|---|---|
| review | 19 | 5 | 14 | 0 |
| investigate | 15 | 3 | 12 | 0 |
| health | 10 | 2 | 8 | 0 |
| cso | 0 | 1 (launcher probe) | 0 | **1** |
| qa-only | 5 | 1 | 4 | 0 |
| qa | 13 | 4 | 9 | 0 |
| design-review | 30 | 9 | 21 | **1** (the design detector's install block, refused by the config: intended) |
| document-generate | 11 | 2 | 9 | 0 |
| document-release | 9 | 1 | 8 | 0 |
| ship | 19 | 5 | 14 | 0 |
| plan-eng-review | 11 | 4 | 7 | 0 |
| plan-design-review | 22 | 6 | 16 | 0 |
| autoplan | 17 | 6 | 11 | 0 |
| office-hours | 18 | 6 | 12 | 0 |
| plan-ceo-review | 16 | 8 | 8 | 0 |
| plan-devex-review | 13 | 5 | 8 | 0 |
| spec | 7 | 1 | 6 | 0 |
| design-consultation | 18 | 6 | 12 | 0 |
| design-html | 18 | 3 | 15 | 0 |

The preamble (`gstack-skill-start`) ran for every skill and printed `UNATTENDED_SESSION: true` with gstack's
unattended instruction block. That block says: take the recommended option, leave consents, unrecommended questions
and final gates pending, and never push or publish. **The skipped blocks are not verified.** Most are the
question-log, review-log, learnings and skill-end calls whose arguments a model writes. They run the same helpers with
other arguments, but whether those arguments work under a read-only install was not tested here. The catalogue
lane's smoke check (plan: "CI renders every offered skill and runs each one's smoke check") has to run them with
realistic arguments.

### What breaks, and what the image or the runner does about it

1. **Skills call a literal `~/.claude/skills/gstack/...` path.** The committed render hard-codes it. An install
   anywhere else is re-rendered into gstack's state root instead (`_render_install` in `setup`), and that render
   would land in the build's throwaway `HOME`. With `HOME` at the attempt's `work/`, the runner creates
   `work/.claude/skills/gstack` as a link to `/opt/swarm/plugins/gstack`, plus `work/.claude/skills/<skill>/SKILL.md`
   for **only the step's skills**. That is exactly the layout the smoke used, and it worked. The links are a few
   bytes in the checkpoint and are recreated on every start.
2. **`setup` writes into `HOME`.** It registered a `Stop` hook (`timeline-stop-hook`) in `~/.claude/settings.json`
   and wrote `~/.claude.json` and `~/.gstack/`. The build stage runs setup with a throwaway `HOME` and copies none of
   it. The runner keeps writing its own settings and registers no gstack hook, as the plan says.
3. **`gstack-lease` writes into the install's own `.git`.** Every `gstack-skill-start` writes a lease into
   `<install>/.git/gstack-leases/` so that `gstack-upgrade` can see live sessions. In a read-only install that keeps
   `.git`, this prints `gstack-lease: the lease directory stayed locked by a live process ...` on **every skill start**
   (18 of 18 starts in the read-only full run). Without `.git` it exits silently (fail-open; 0 of 18). **The image
   ships the install without `.git`.** The release archive has none, and nothing upgrades in place.
4. **The design detector downloads a binary and records a consent nobody gave.** With gstack's default
   `design_detector: auto`, running design-review's install block
   (`gstack-design-detect.ts install`) downloaded the 16 MB `impeccable` engine from github.com into
   `~/.impeccable/` at run time. It then wrote to `security/egress.jsonl`: "user accepted the install offer for
   impeccable engine 0.1.3". The helper trusts its caller. An unattended agent that runs that block downloads a
   binary, and the record claims a person agreed. **The worker's gstack config sets `design_detector: off`**: the
   install block then answers `IMPECCABLE_INSTALL_REFUSED`, and nothing is fetched (verified). This is the one
   outbound fetch the smoke found. The update check and telemetry were off in every run.
5. **`/cso` is not usable as packaged.** With `GSTACK_SETUP_SKIP_CSO_BUILD=1` (the brief's flag), its launcher does
   not exist, and the skill reports "not assessed". Building it works on Linux (setup took 29.3 s with a warm Bun cache, against 22.6 s without it;
   `gstack-cso-launcher --help` answers `gstack-cso 3.0.0 (helper ABI 3)`), but three things stand in the way:
   * the launcher is created mode 0700, so a root-owned copy cannot be run by the agent's uid;
   * `doctor` reports the static snapshot as missing ("Private state has a group- or world-writable ancestor").
     `/workspace` in this container is world-writable, and both the workspace and the cache volume failed. The agent
     pod's `work/` ancestors were not checked;
   * all six scanners (gitleaks, osv, semgrep, zizmor, trivy, schemathesis) need container images that agent pods
     cannot run.

   This is an owner question (below).
6. **The design skills report `DESIGN_NOT_AVAILABLE` without the `design` binary.** That is intended while outside
   AI services are off. Every `design` command (generate, variants, iterate, check, evolve, extract, ...) calls an
   outside image model. The four blocks that probe it ran either way; what the skills do next, after the probe, was
   not verified.
7. **The compiled `browse` needs Bun at run time and embeds its build path.** It spawns
   `bun run <install>/browse/src/server.ts`, which fails with `Executable not found in $PATH: "bun"` until Bun is on
   `PATH`. Its bundle also carries the build-time `__dirname` of `browse/src`. Two builds at different paths differed
   (`cmp`: first difference at byte 81,260,545), and the embedded path held the build folder. **Build at the final
   path, `/opt/swarm/plugins/gstack`**, so the embedded and the installed path are the same. The binaries are
   therefore not byte-reproducible across build paths: pin the source and Bun, not the binaries' hashes.
8. **Bun writes a transpiler cache into `HOME`**: 17 to 19 files under `~/.bun/install/cache/@t@/` per run of the 19 skills, which
   in a pod would be the checkpointed workspace. `BUN_RUNTIME_TRANSPILER_CACHE_PATH=0` stopped it: no file under
   `HOME` (verified on review and ship).
9. **Chromium writes into `HOME`**: `.config/google-chrome-for-testing`, `.cache/fontconfig` and `.local/share`.
   The runner should point the browser's XDG folders at the cache volume, as it does for other caches
   (`docs/worker-images.md`, "Where an agent's caches go"). Not verified here.
10. **gstack writes into the project.** It writes `.gstack/` (browse's audit, daemon and network logs, its state file,
    temporary briefs and prompts), adds `/.gstack/tmp/` to `.git/info/exclude`, and gives `.gstack/` its own
    `.gitignore`. After every run `git status --ignored` showed `!! .gstack/` and nothing untracked. The runner still
    adds `.gstack/` to `.git/info/exclude`, as the plan says, so the diff never depends on gstack's own file.
11. **Linux itself broke nothing that ran.** The macOS-only Aside probe prints `NEEDS_ASIDE: Linux` and the skills
    fall back to gstack's own browser. One difference between runs was a backgrounded `gstack-brain-cache refresh`
    (gbrain, never offered) whose output raced the block's exit.

### The browser image's Chromium, reused

The browser image installs Chromium with `playwright install --with-deps --no-shell chromium` from Python Playwright
1.63.0 into `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright`. The same Playwright version's Chromium (`chromium-1243`,
"Google Chrome for Testing 153.0.8010.12", `chrome-linux64/chrome`) was fetched into a temporary folder here.
gstack's `browse` drove it through `GSTACK_CHROMIUM_PATH=<that chrome>` and `GSTACK_CHROMIUM_NO_SANDBOX=1`. gstack
bundles Node Playwright 1.62.1, one minor version behind the browser it drove; it worked. This ran from a read-only
install of only the base and browser sets, with `HOME` in a workspace folder:

* `goto file://.../index.html`: `200`, 2.0 s including the server's start;
* `text`: the page text, wrapped in gstack's untrusted-content markers;
* `screenshot`: a 9,991-byte PNG, with `sharp` present (without `sharp` the screenshot size guard failed with
  `Cannot find package 'sharp'`);
* `accessibility`: the heading;
* `js navigator.userAgent`: `HeadlessChrome/153.0.0.0`;
* `stop`.

**So the browser image adds no Chromium.** The runner sets `GSTACK_CHROMIUM_PATH` to the `chrome` that
`livegate`'s `find_chromium` already finds under `/opt/playwright`. Chromium's own sandbox stays off, as the browser
image's Dockerfile decides for every agent browser. Not verified: the managed Chromium policy
(`/etc/chromium/policies/managed/swarm.json`) under gstack's launch flags, and gstack's browser against the image's
own Chromium binary rather than the same version fetched here.

## The image build proposal

**The source, through the build mirror.** Add two rows to `images/agent-runtime-base/tool-mirror.sha256`, each at its
upstream URL without `https://` (the same scheme as gh, terraform and shellcheck):

```
63c118f13ccf0d72d06d7bff56231dc08ce3da29ce405ed567224dd303cf5bd2  github.com/garrytan/gstack/archive/refs/tags/v1.92.55.0.tar.gz
36368faef7527875d5ffa52e53cd48021741f2a83eb6208a8dd64068d422a913  github.com/oven-sh/bun/releases/download/bun-v1.4.2/bun-linux-x64.zip
```

The Dockerfile pins the matching `ARG GSTACK_VERSION=1.92.55.0`, `GSTACK_SHA256`, `GSTACK_COMMIT=2512dd76...`,
`BUN_VERSION=1.4.2` and `BUN_SHA256`. A test holds those ARGs equal to the mirror rows, as
`tests/unit/scripts/test_build_mirror.py` does for the others. The npm packages come from `bun install
--frozen-lockfile` against gstack's own `bun.lock`, whose integrity hashes Bun checks. They are fetched only in the
build stage. Whether the registry fetch goes through a mirror the way the base's pinned npm packages do is a question
for the build lane.

**The layers.**

1. **A build stage**, `gstack-build`, `FROM` the same pinned Python base the builder uses (it has bash, git and
   curl). It:
   * unpacks Bun from the mirror after `sha256sum -c`;
   * unpacks the gstack archive **at `/opt/swarm/plugins/gstack`**, the final path (finding 7);
   * runs `./setup --host claude --no-browser` with `HOME` and `GSTACK_STATE_ROOT` in a throwaway folder,
     `GSTACK_SKIP_FONTS=1`, `GSTACK_SETUP_SKIP_CSO_BUILD=1` and `GSTACK_SECURITY_OFF=1` (no Chromium download, no
     font step, no scanner images, no 112 MB model);
   * runs `scripts/gstack_packaging_measure.py` on the result and fails if its closure differs from the checked-in
     base and browser lists.

   Everything else stays in the stage.
2. **The base image** copies `/usr/local/bin/bun`, root-owned 0755. Bun goes in `/usr/local/bin`, not under the
   plugin, because gstack's helpers run `bun` from `PATH`, and so does the compiled `browse`. The base image also
   copies the base list into `/opt/swarm/plugins/gstack`, root-owned, directories 0755, files 0644, helpers 0755,
   and **no `.git`** (finding 3).
3. **The browser image** copies the browser list into the same folder, which is about 123 MB more. It reuses the
   Chromium it already has.

An image `ENV` does not reach the agent's CLI, because the runner builds its environment from scratch (the Lightpanda
precedent, `docs/worker-images.md`). So the runner sets, on every claude-code agent that uses gstack:

* `GSTACK_SESSION_KIND=unattended` (or the planner mode's value);
* `GSTACK_HOME` in the attempt's workspace, so a resumed step keeps gstack's state;
* `GSTACK_EPHEMERAL=1`, as the plan says (not exercised here);
* `BUN_RUNTIME_TRANSPILER_CACHE_PATH=0` (finding 8);
* on browser profiles, `GSTACK_CHROMIUM_PATH` and `GSTACK_CHROMIUM_NO_SANDBOX=1`.

It also writes `GSTACK_HOME/config.yaml` with `update_check: false`, `telemetry: off`, `codex_reviews: disabled` and
`design_detector: off` (finding 4; `GSTACK_CONFIG` in the script is that file), and creates the skill links
(finding 1).

**Bumping the pin** is one PR: the ARGs, the two mirror rows, and the regenerated base and browser lists. CI runs this
script's smoke on the new tag, read-only, and refuses a list that changed without the PR saying why.

## What this spike did not verify

* The start time before and after (no image built).
* An `EROFS` read-only root filesystem; the install was read-only by mode bits.
* The 194 skipped blocks: their helpers with model-written arguments.
* Anything a model does in a skill. This spike runs no model and asks no question.
* gstack's question capture, park and resume (the question bridge spike's work).
* `/cso` in an agent pod: whether `work/` has a group- or world-writable ancestor there.
* gstack's browser under the browser image's managed Chromium policy, and against that image's own binary.
* arm64: every number here is x86_64.

## How the contract's invariants hold

This change adds a measurement script, its tests and this page. It changes no image, no runner, no scheduler, no
Firestore shape and no API.

* Infrastructure demand (1–3), fencing (5), checkpointing (8): untouched; nothing here runs in a dispatch path.
* Resources (7): no request or limit changes. The image sizes above are measurements, not limits.
* Tenant isolation (9): nothing new. The proposal keeps gstack's state in the attempt's own workspace and the install
  read-only and shared. The design detector's outbound fetch is turned off in the worker's config (finding 4).
* Caller input (10): a step will name a plugin and skills **by name** from the catalogue, never a path, command,
  version or image (the plan's catalogue). This proposal adds no field a caller sets.
* `apps/common/swarm_common/` is untouched. The plan's contract request (per-step plugins in the work contract's
  capabilities) is the catalogue lane's, not this spike's.
