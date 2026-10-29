# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# agent-runtime-browser -- agent-runtime-base, built WITHOUT codex, plus
# Playwright and Chromium.
#
# The base is `agent-runtime-base-nocodex`: images/agent-runtime-base/Dockerfile
# built with `--build-arg INSTALL_CODEX=0` (scripts/build-images.sh;
# BROWSER-NO-CODEX, owner decision 2026-10-13, docs/browser-startup-speed.md
# §2.3). codex is 129.7 MB of the base's npm layer and `claude-code-browser`
# never runs it; deleting it here would only add a whiteout over an inherited
# layer, so the base this is built FROM never has it. The self-test below
# fails if it is on PATH.
#
# Build that base first, then:
#
#     docker build -f images/agent-runtime-browser/Dockerfile \
#         --build-arg BASE_IMAGE=<base image ref, ideally by digest> \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/agent-runtime-browser:$TAG .
#
# BASE_IMAGE is supplied by digest by the checked-in
# `images/agent-runtime-browser/cloudbuild.yaml`, which derives the base ref
# from the image being built, pulls it, and reads the digest back. The default
# below is a LOCAL convenience only -- in a Cloud Build worker there is no local
# `agent-runtime-base-nocodex:latest`, so a build that falls through to it fails with a
# manifest-unknown pull error rather than silently using something else.
#
# This profile is the only one that does not run on Cloud Run Jobs. Chromium
# needs a large /dev/shm and Cloud Run does not let us size it, so the frozen
# catalogue pins `browser` to GKE Autopilot, where the Job template mounts 2Gi
# of tmpfs at /dev/shm -- which is also why the `browser` resource class is
# 16 GiB rather than 8: that tmpfs counts against the container's memory limit
# before Chromium has allocated a single tab.
#
# Chromium's own sandbox stays OFF: it needs user namespaces and CAP_SYS_ADMIN,
# which the pod security policy refuses. The isolation here is the pod --
# non-root, no privilege escalation, seccomp RuntimeDefault, every capability
# dropped, a per-tenant namespace with default-deny egress -- and handing back a
# capability to gain Chromium's inner sandbox would be a bad trade.
ARG BASE_IMAGE=agent-runtime-base-nocodex:latest
FROM ${BASE_IMAGE} AS runtime

# Pinned on 2026-09-15 against PyPI.
ARG PLAYWRIGHT_VERSION=1.63.0

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PLAYWRIGHT_SKIP_BROWSER_GC=1

USER root

# Chromium and its system libraries. `--with-deps` is what installs the ~100
# shared objects Chromium needs; doing it here, once, keeps it out of the
# attempt's critical path.
#
# SLIMMED (owner decision 2026-10-13, docs/browser-startup-speed.md §2.3/§9;
# BROWSER-ENFORCE, part of #1030). Everything removed is removed IN THIS RUN:
# deleting it in a later one only hides it under a whiteout, and the bytes are
# still pulled.
#
#   * `--no-shell`: no chromium-headless-shell (108.5 MB as a layer). Since
#     Playwright 1.49 `install chromium` adds it unless told not to, and it is
#     only what `headless=True` launches by default. The `browser` runner
#     launches the full build headless with `channel="chromium"`
#     (runners/browser.py::_launch); the live gate never ran the shell
#     (livegate/__main__.py::find_chromium globs `chromium-*/chrome-linux*/chrome`,
#     which this keeps).
#   * no second Node (44.6 MB): the wheel's `driver/node` is replaced by a link
#     to the base's /usr/local/bin/node (Node 24). A link at the same name and
#     not PLAYWRIGHT_NODEJS_PATH, because the runner's environment is built by
#     exact name (lifecycle.py) and would not carry a new variable. The link is
#     made after `playwright install`, which runs on the bundled one.
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
 && mkdir -p "${PLAYWRIGHT_BROWSERS_PATH}" \
 && playwright install --with-deps --no-shell chromium \
 && driver="$(python -c 'import os, playwright; print(os.path.join(os.path.dirname(playwright.__file__), "driver"))')" \
 && rm -f "${driver}/node" \
 && ln -s /usr/local/bin/node "${driver}/node" \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

# THE LIVE BROWSER'S DISPLAY SIDECAR (docs/design/live-browser.md §3.1, §5.6;
# lane L5, part of #1030). A `claude-code-browser` pod runs this same image
# twice: once as the worker (uid 10001, the ENTRYPOINT below) and once as the
# `display` native sidecar, which the dispatcher starts with its own command
# (`python -m agent_worker.livegate`) under uid 10002. It needs:
#
#   * Xvfb -- the virtual display Chromium draws on. The sidecar starts it with
#     `-nolisten tcp`, so its only socket is in the sidecar's own /tmp.
#   * the `swarm-display` user, uid/gid 10002, DISPLAY_UID in
#     apps/scheduler/scheduler/dispatch.py. The pod spec names the number so
#     the cluster enforces it; the image names it so the process has a passwd
#     entry (Chromium and Xvfb both look themselves up). No home and no login
#     shell: HOME is the sidecar's memory /tmp.
#   * the enterprise policy of §5.6, read by Chromium at start. Installed in
#     BOTH managed-policy directories: /etc/opt/chrome/ is Google Chrome's,
#     the one the design names, and /etc/chromium/ is Chromium's -- which is
#     what Playwright's bundled build is. A policy in the wrong one of the two
#     is silently ignored, so it goes in both. Root-owned and read-only to
#     every uid: a person in control, or the agent, must not edit it.
#     Two entries of the design's list are written in Chromium's URL-filter
#     syntax: `*.svc.cluster.local` is `svc.cluster.local` (a bare host
#     already covers its subdomains; a leading `*.` is not a valid filter),
#     and `cluster.local` is added for the rest of the cluster's names.
#     `chrome://newtab` is allowed back by URLAllowlist, which wins over the
#     blocklist.
RUN apt-get update \
 && apt-get install --no-install-recommends -y xvfb \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/sbin/groupadd --gid 10002 swarm-display \
 && /usr/sbin/useradd --uid 10002 --gid 10002 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin swarm-display \
 && install -d -m 0755 /etc/opt/chrome/policies/managed/ /etc/chromium/policies/managed/
COPY --chmod=0444 images/agent-runtime-browser/chromium-policy.json /etc/opt/chrome/policies/managed/swarm.json
COPY --chmod=0444 images/agent-runtime-browser/chromium-policy.json /etc/chromium/policies/managed/swarm.json

# The agent's `swarm-handoff` (docs/design/live-browser.md §4.1; lane L4, part
# of #1030): it writes a request into the hand-off spool and waits for the
# worker's answer. Files only, no socket. Root-owned, executable by every uid,
# writable by none: the agent runs it and cannot change what it does.
COPY --chmod=0555 images/agent-runtime-browser/swarm-handoff /usr/local/bin/swarm-handoff

# The agent's `swarm-test-browsers` (PW-TESTRUNNER, BGD 2026-10-24, part of
# #2253): runs ONE command -- a repository's `npx playwright test` -- with
# PLAYWRIGHT_BROWSERS_PATH at the writable directory the claude-code runner
# seeds in the attempt's cache, so a test process may launch its own headless
# Chromium while the agent's own default stays the live-browser stub below.
# Root-owned and read-only, as swarm-handoff is.
COPY --chmod=0555 images/agent-runtime-browser/swarm-test-browsers /usr/local/bin/swarm-test-browsers

# THE AGENT'S BROWSER IS THE LIVE ONE (BROWSER-ENFORCE, part of #1030;
# docs/design/live-browser.md §3.2). task_7cda0d5335f049b4b442's agent launched
# its own hidden headless Chromium, and the person watching saw none of its
# work. Three pieces, all root-owned and read-only to every uid:
#
#   * the Playwright MCP server, pinned. The claude-code runner registers it
#     with `--cdp-endpoint $SWARM_CDP_ENDPOINT` when the live browser exists
#     (`claude_code.PLAYWRIGHT_MCP_BIN`), so the agent's browser_navigate and
#     browser_tabs act on the browser the console shows. It connects and
#     launches nothing, so it needs no browser of its own. Pinned 2026-10-13
#     against npm's `latest`.
#   * the stub browsers directory, /opt/swarm/live-browser-only. The runner
#     points the AGENT's PLAYWRIGHT_BROWSERS_PATH at it -- only the agent's:
#     the image ENV above, which the gate and the `browser` runner read, stays
#     /opt/playwright -- so `chromium.launch()` and
#     `launch_persistent_context()`, headless or not, start `live-browser-only`
#     and fail with a message that names the CDP endpoint. Chosen over a
#     sitecustomize wrapping BrowserType.launch: it also holds for Node's
#     Playwright and for any Python, and it cannot reach the gate, whose
#     environment never names this directory. install-browser-stub.py asks
#     Playwright where each launch looks, so a version bump moves the stubs
#     with it.
#   * `swarm_browser.live()`, in the venv's site-packages: the CDP connection
#     in one line. The agent's PATH has /opt/venv/bin first on this image
#     (claude_code.VENV_BIN), so its `python` imports it.
#
# npm's cache and Playwright's scratch go to a directory removed in the same
# RUN: as root, npm would otherwise leave a root-owned ~/.npm in /home/swarm.
#
# THE VISUAL-QA CAPTURE TOOL (QA-CAPTURE, owner decision 2026-10-19, part of
# #1649; docs/design/visual-qa.md §4.2). `swarm_qa.py` sits beside
# `swarm_browser.py`, so the agent's `python -m swarm_qa capture` sweeps a
# group of apps/swarm-ui/qa/routes.json through the live browser, and
# `python -m swarm_qa diff` compares two captures. Nothing in the worker calls
# it. Its two pins ride in the RUN below rather than a RUN of their own (each
# RUN is a layer every cold pod pulls):
#   * axe-core, the accessibility engine `capture --axe` runs in the page,
#     read from /opt/swarm/axe-core/axe.min.js. Pinned 2026-10-20 against
#     npm's `latest`.
#   * Pillow, which `diff` decodes with (its standard-library decoder is the
#     fallback the unit tests use, and is slow on a 3x phone capture) and which
#     a review step crops a finding's evidence with, and which
#     `swarm-handoff compare` decodes a frame PNG with (IMPLEMENT-RENDER,
#     part of #2442; a palette or 16-bit frame needs it). Pinned 2026-10-20 against
#     PyPI's latest; 13.0.0 has a cp311 manylinux wheel, so nothing compiles.
ARG PLAYWRIGHT_MCP_VERSION=0.0.83
ARG AXE_CORE_VERSION=4.14.0
ARG PILLOW_VERSION=13.0.0
COPY --chmod=0444 images/agent-runtime-browser/swarm_browser.py /opt/venv/lib/python3.11/site-packages/swarm_browser.py
COPY --chmod=0444 images/agent-runtime-browser/swarm_qa.py /opt/venv/lib/python3.11/site-packages/swarm_qa.py
RUN --mount=type=bind,source=images/agent-runtime-browser,target=/tmp/swarm-browser-build \
    scratch="$(mktemp -d)" \
 && npm_config_cache="${scratch}/npm" npm install -g --no-audit --no-fund "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" "axe-core@${AXE_CORE_VERSION}" \
 && install -d -m 0755 /opt/swarm/axe-core \
 && install -o root -g root -m 0444 "$(npm root -g)/axe-core/axe.min.js" /opt/swarm/axe-core/axe.min.js \
 && pip install --no-cache-dir "pillow==${PILLOW_VERSION}" \
 && TMPDIR="${scratch}" python /tmp/swarm-browser-build/install-browser-stub.py \
      /opt/swarm/live-browser-only /tmp/swarm-browser-build/live-browser-only \
 && chmod -R a+rX,a-w /opt/swarm \
 && rm -rf "${scratch}"

# DEV-BROWSER, OPT-IN, ATTACH ONLY (DEVB-INSTALL, owner decision 2026-10-14,
# part of #1030; docs/design/live-browser.md "dev-browser (opt-in)"). Beside
# the Playwright MCP server, which stays the default, the agent may script
# multi-step browsing with SawyerHood/dev-browser v1 -- and only against the
# live browser:
#
#   * the REAL binary is /opt/swarm/dev-browser/dev-browser-bin, off PATH. It
#     is the upstream release's standalone Bun build (puppeteer-core 25.8.0
#     inside; libc only, so no Node and no npm package). The npm package
#     `dev-browser@1.0.0-rc.3` (dist-tag `next`) is only a shim whose
#     postinstall downloads this same file, which is why it is not used, and
#     why DEV_BROWSER_SKIP_DOWNLOAD=1 is exported to every run anyway;
#   * `dev-browser` on PATH is images/agent-runtime-browser/dev-browser, the
#     wrapper that always passes `--connect "$SWARM_CDP_ENDPOINT"` and refuses
#     `install`, `chrome`, `--headless` and any other `--connect`;
#   * /opt/swarm/dev-browser/live-browser-only is the stub
#     install-browser-stub.py installs for Playwright, copied to one fixed
#     path. dev-browser never reads PLAYWRIGHT_BROWSERS_PATH (no such string
#     in the binary, checked 2026-10-14), so the stub directory above does not
#     cover it; DEV_BROWSER_CHROME, which it reads before any other lookup, is
#     pointed here instead -- by the wrapper, and by the claude-code runner for
#     the agent's whole environment (`claude_code.DEV_BROWSER_CHROME`), not by
#     an ENV, which never reaches the agent's CLI (runners/cliagent.py builds
#     its environment from scratch). So a launch, even by the real binary,
#     fails with the stub's message rather than starting a hidden Chromium.
#
# The binary is read from the tool mirror like the base's release files (see
# images/agent-runtime-base/Dockerfile, THE TOOL MIRROR; tool-mirror.sha256
# lists it). Obtained 2026-10-14: DEV_BROWSER_SHA256 is `sha256sum` of
# https://github.com/SawyerHood/dev-browser/releases/download/v1.0.0-rc.3/dev-browser-linux-x64
# as downloaded, equal to that file's line in the release's own SHA256SUMS.
# 96,442,496 bytes; `dev-browser-bin --version` printed `dev-browser 1.0.0-rc.3`.
ARG DEV_BROWSER_VERSION=1.0.0-rc.3
ARG DEV_BROWSER_SHA256=390dd08f8321807bca2e1e060ec031511adb0af002e871dfde3b1f6c8feac914
COPY --chmod=0555 images/agent-runtime-browser/dev-browser /usr/local/bin/dev-browser
RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror \
    --mount=type=bind,source=images/agent-runtime-browser,target=/tmp/swarm-browser-build \
    mkdir -p /tmp/dev-browser \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/dev-browser/dev-browser-linux-x64 \
      "file:///tmp/tool-mirror/github.com/SawyerHood/dev-browser/releases/download/v${DEV_BROWSER_VERSION}/dev-browser-linux-x64" \
 && echo "${DEV_BROWSER_SHA256}  /tmp/dev-browser/dev-browser-linux-x64" | sha256sum -c - \
 && install -d -m 0755 /opt/swarm/dev-browser \
 && install -o root -g root -m 0555 /tmp/dev-browser/dev-browser-linux-x64 /opt/swarm/dev-browser/dev-browser-bin \
 && install -o root -g root -m 0555 /tmp/swarm-browser-build/live-browser-only /opt/swarm/dev-browser/live-browser-only \
 && rm -rf /tmp/dev-browser

# JEV-BROWSER, ATTACH ONLY (JEV-IMAGE, owner decision 2026-10-14, part of
# #1030). TypeSafe's jev-browser -- an MCP server over Playwright where the
# model "Jev" picks each step -- beside the Playwright MCP server and
# dev-browser. The owner accepted that it sends page text and element labels
# to TypeSafe (api.typesafe.ai). Upstream it only ever launches a browser of
# its own, so:
#
#   * npm ci from the committed images/agent-runtime-browser/jev-browser/
#     package.json and package-lock.json into /opt/swarm/jev-browser, off PATH.
#     Every package resolves from registry.npmjs.org with its sha512, as
#     @playwright/mcp above does (the build mirror is for images and GitHub
#     release files; the npm registry is read directly). `--ignore-scripts`:
#     none of the 98 packages has an install script, and none may run one.
#     Playwright is held to 1.63.0 by an override, the PLAYWRIGHT_VERSION
#     above, so its Chromium revision is the one /opt/playwright already
#     holds; PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 besides, so no second Chromium
#     arrives.
#   * jev-browser 0.2.0 is pinned exactly. It was released 2026-10-13, the day
#     before this; the repository has no minimum-age rule for a dependency
#     (none in docs/, scripts/ or the workflows, checked 2026-10-14), and 0.2.0
#     is the release the owner named. 0.1.2 (2026-10-08) is no older than two
#     weeks either.
#   * patches/jev-browser+0.2.0.patch (patch-package's format, applied by
#     `git apply`, which needs no extra package) adds the attach:
#     JEV_BROWSER_CDP_ENDPOINT -> `chromium.connectOverCDP`, the browser's
#     existing default context (never `newContext`: the gate refuses
#     Emulation.setDeviceMetricsOverride, which a context with a viewport
#     sends), no viewport, the WebMCP init script off (it rewrites
#     `modelContext` in every page the browser loads, the person's included),
#     and close() disconnecting only. Without the endpoint every launch, and
#     the MCP server's Chromium download, is refused. A version bump fails
#     here, at `git apply`, until the patch is redone for it.
#   * bin/jev-browser-mcp is the wrapper the runner registers: it refuses to
#     start without SWARM_CDP_ENDPOINT and makes that the attach endpoint.
#
# SIZE: +47 MiB on disk (`du -sh node_modules` after this `npm ci`, measured
# 2026-10-14; playwright-core 14 MiB of it), 7.4 MiB as `tar | gzip -6`.
ARG JEV_BROWSER_VERSION=0.2.0
RUN --mount=type=bind,source=images/agent-runtime-browser/jev-browser,target=/tmp/jev-browser-build \
    scratch="$(mktemp -d)" \
 && install -d -m 0755 /opt/swarm/jev-browser /opt/swarm/jev-browser/bin \
 && install -o root -g root -m 0444 /tmp/jev-browser-build/package.json /tmp/jev-browser-build/package-lock.json /opt/swarm/jev-browser/ \
 && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm_config_cache="${scratch}/npm" \
      npm ci --prefix /opt/swarm/jev-browser --omit=dev --ignore-scripts --no-audit --no-fund \
 && git -C /opt/swarm/jev-browser apply --verbose "/tmp/jev-browser-build/patches/jev-browser+${JEV_BROWSER_VERSION}.patch" \
 && install -o root -g root -m 0555 /tmp/jev-browser-build/bin/jev-browser-mcp /opt/swarm/jev-browser/bin/jev-browser-mcp \
 && chmod -R a+rX,a-w /opt/swarm/jev-browser \
 && rm -rf "${scratch}"

USER swarm:swarm
# NO `WORKDIR /workspace` HERE: the base sets it and this image inherits it.
# Restating it wrote BuildKit's empty layer (sha256:4f4fb700ef54...) a second
# time, and GKE does not stream an image with a duplicate layer, so every cold
# browser pod pulled all 1.25 GB first: 42 s of an 88 s start, measured
# 2026-10-13 (docs/browser-startup-speed.md §2.4). The release refuses such an
# image (scripts/image-sizes.sh --duplicate-layers);
# tests/unit/scripts/test_image_streaming.py holds this file to it.

# Prove, as the agent's uid and in ONE RUN (two check-only RUNs can write the
# same layer -- the duplicate above), that the full Chromium launches headless,
# that the agent's own launch is refused toward the live browser, that the
# helper and the MCP server are there, that codex is not and every other tool
# of the base is, and that the display sidecar's pieces
# are where the dispatcher expects them, and that `dev-browser` is the wrapper
# over the pinned binary and refuses a browser of its own, and that
# jev-browser attaches to a running Chromium over CDP and launches nothing,
# rather than discovering any of it at 3am in a tenant's attempt. selftest.py
# lists each check; it is bind-mounted, not shipped. DEV_BROWSER_VERSION and
# JEV_BROWSER_VERSION are the ARGs above, which every RUN of this stage sees:
# the versions the installed tools must report.
RUN --mount=type=bind,source=images/agent-runtime-browser,target=/tmp/swarm-browser-build \
    scratch="$(mktemp -d)" \
 && TMPDIR="${scratch}" DEV_BROWSER_VERSION="${DEV_BROWSER_VERSION}" JEV_BROWSER_VERSION="${JEV_BROWSER_VERSION}" \
      python /tmp/swarm-browser-build/selftest.py /opt/swarm/live-browser-only \
 && rm -rf "${scratch}"

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]
