# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# agent-runtime-base -- the image every non-browser runner profile runs in.
#
# It is built twice. `--build-arg INSTALL_CODEX=0` makes
# agent-runtime-base-nocodex, the base agent-runtime-browser is built FROM
# (scripts/build-images.sh; BROWSER-NO-CODEX, owner decision 2026-10-13).
#
# Build from the REPOSITORY ROOT, because the worker and the frozen contract
# package are both part of the image:
#
#     docker build -f images/agent-runtime-base/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/agent-runtime-base:$TAG .
#
# Properties this image is required to have, and where each one is enforced:
#
#   pinned bases      every FROM carries a sha256 digest, so a rebuild in six
#                     months produces the same toolchain rather than whatever
#                     `slim-bookworm` points at that day
#   multi-stage       compilers and the wheel cache stay in the builder; the
#                     runtime carries the venv and the tools an agent uses
#   non-root          uid 10001, and /workspace is the only writable path it
#                     owns. The pod security policy also refuses root, so an
#                     image that ignored this would simply fail to schedule
#   no docker socket  nothing here talks to a container runtime. An agent that
#                     could reach the socket could escape every boundary the
#                     rest of this platform builds. The `docker` CLI is in the
#                     toolbox (BUILD_PROMPT_V2 §2.12) with no daemon and no
#                     socket behind it; image builds go to Cloud Build
#   tini as PID 1     an agent's helpers get orphaned when the worker kills a
#                     process group; tini reaps them instead of leaving zombies
#
# NOT HERE: the repository index's toolchain (the tree-sitter extractor, the
# graph shard writer, the Go toolchain, gopls, pyright and
# typescript-language-server). It is images/agent-runtime-indexer, built FROM
# this image by digest, and only the `indexer` runner profile runs it. Every
# claude-code and codex start pulls this image, and the toolchain was +188 MB
# compressed (726 -> 916 MB, the operator's registry bisect of 2026-10-05 with
# scripts/image-sizes.sh, #625) that only an index run uses. That bisect also
# showed the ~115 -> ~167 s start step of 10-04/05 happened with no change in
# this image's size (#667), so the move keeps the weight off every start; it
# is not the cause of that step. Adding a tool to this image is paid on every
# agent start: measure it with scripts/image-sizes.sh.
#
# Digests resolved from the registries on 2026-09-15.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553
# 0.12.15, not 0.11.7: the older uv statically links quinn-proto 0.11.14
# and rustls-webpki 0.103.10, both with fixed HIGH advisories
# (GHSA-4w2j-m93h-cj5j, GHSA-82j2-j2ch-gfr8) that failed the promote scan
# on /usr/local/bin/uv and /usr/local/bin/uvx. Nothing else in the image
# carries a Rust binary, which is why these two were the last to appear.
ARG UV_IMAGE=astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3
# Every base is pulled through this project's Artifact Registry mirrors,
# never from Docker Hub or ghcr.io directly: on 2026-10-12 three release
# builds failed inside Cloud Build on ghcr.io timeouts and a github.com 504.
# Remote repositories (terraform/infra/build_mirror.tf) serve the same
# digests. A pull request's local build, which has no Google identity,
# passes the upstream registries here instead (scripts/lib/build-mirror.sh).
# docs/runbooks/build-mirror.md.
# No default, so this file names no project (DAYONE-DESAGA-1,
# docs/design/day-one-install.md section 7): scripts/build-images.sh passes
# the mirror of the project it builds for (lib/build-mirror.sh
# mirror_build_args), and the `# syntax=` frontend through the same mirror as
# BUILDKIT_SYNTAX. A `docker build` given neither fails on its first FROM.
ARG MIRROR_DOCKER_HUB=
ARG MIRROR_GHCR=

# ---------------------------------------------------------------------------
# uv and Node arrive as pinned images rather than as curl | sh
# ---------------------------------------------------------------------------
FROM ${MIRROR_GHCR}/${UV_IMAGE} AS uvbin
FROM ${MIRROR_DOCKER_HUB}/library/${NODE_IMAGE} AS nodebin

# ---------------------------------------------------------------------------
# builder: compile wheels, produce /opt/venv
# ---------------------------------------------------------------------------
FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# APT_REFRESH: the build scripts/build-images.sh makes this layer in (every
# main build and release), so the upgrade below is never an older build's
# (owner decision 2026-10-14).
ARG APT_REFRESH=unset
RUN echo "apt layer of ${APT_REFRESH}" \
 && apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y \
      build-essential \
      ca-certificates \
      git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uvbin /uv /usr/local/bin/uv

WORKDIR /src
# The root project and its lock. `uv export` below reads the worker's
# third-party dependency set, at the versions and hashes uv.lock records, from
# them (#663). With --frozen it reads no other app's directory, so the builder
# still copies only the packages this image ships.
COPY pyproject.toml uv.lock /src/
# Only what the image needs to build: the frozen contract, the credential
# patterns the worker's per-commit secret scan matches (#259), and the worker.
COPY apps/common/ /src/apps/common/
COPY apps/redaction/ /src/apps/redaction/
# The workflow state derivation: a park, finish or retry on the direct
# Firestore path writes the workflow's derived state in its own transaction
# (#2838). `apps/agent-worker/pyproject.toml` resolves it through
# `{ path = "../workflow-rollup" }`.
COPY apps/workflow-rollup/ /src/apps/workflow-rollup/
COPY apps/agent-worker/ /src/apps/agent-worker/

# Build our own packages into a local wheelhouse. No index has ever heard of
# them, and the install below never asks one: it reads them from /wheels with
# `--no-index` (#663).
RUN uv build --wheel --out-dir /wheels /src/apps/common \
 && uv build --wheel --out-dir /wheels /src/apps/redaction \
 && uv build --wheel --out-dir /wheels /src/apps/workflow-rollup \
 && uv build --wheel --out-dir /wheels /src/apps/agent-worker

# Floors, not pins: both ship inside the stdlib venv at versions Debian's
# snapshot froze, and both had fixed HIGH advisories against those versions
# (CVE-2026-23949, CVE-2026-24049).
ARG SETUPTOOLS_VERSION=80.9.0
ARG WHEEL_VERSION=0.46.2
ARG PYTEST_VERSION=8.4.2

# `pytest` is installed into the SAME venv as the worker because the `pytest`
# entry in `agent_worker.runners.generic.GENERIC_COMMANDS` runs
# `python -m pytest` on this image's PATH, and that command is the first example
# the generic profile's docstring gives. Without it a tenant submitting the
# documented use case gets `No module named pytest` and a hard RunnerFailure.
# Pinned for the same reason as every other version here: a rebuild in six
# months must produce the same toolchain.
# pip STAYS here, unlike the control-plane images: an agent working in this
# container reasonably reaches for `pip install`, and taking it away would turn
# a normal step into a confusing failure inside somebody's task.
#
# setuptools and wheel are upgraded rather than removed for the same reason --
# both were flagged by the promote scan (jaraco.context path traversal vendored
# in setuptools, arbitrary code execution in wheel), and both are reachable here
# precisely BECAUSE an agent may build a package.
RUN python -m venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python \
      "setuptools>=${SETUPTOOLS_VERSION}" "wheel>=${WHEEL_VERSION}"

# Third-party dependencies, exported from uv.lock for the worker alone. The
# root project depends on every app, so the export prunes the internal packages
# swarm-agent-worker does not depend on (`swarm-api`, `swarm-install`,
# `swarm-mcp`, `swarm-quota-broker`, `swarm-reconciler`, `swarm-scheduler`),
# and the third-party packages only they need go with them. `--no-emit-local` leaves our own packages out of the file: they come
# from /wheels, below. The export carries a hash for every file; `--no-hashes`
# must never be added here.
RUN uv export --frozen --no-dev --no-emit-local --no-editable \
      --prune swarm-api --prune swarm-install --prune swarm-mcp \
      --prune swarm-quota-broker --prune swarm-reconciler \
      --prune swarm-scheduler \
      > /tmp/requirements.txt

# Installed at exactly the versions and hashes in uv.lock, with nothing
# resolved at build time (#663). This used to be one `uv pip install
# --find-links /wheels swarm-agent-worker` that resolved every third-party
# package unpinned against PyPI. `--require-hashes` refuses any file whose hash
# the lock does not record, so an index can fail this step but never substitute
# a package; `--no-deps` because the export is already the complete closure.
RUN uv pip install --python /opt/venv/bin/python --require-hashes --no-deps -r /tmp/requirements.txt

# Our own packages, from the wheelhouse ONLY. `--find-links` alone ADDS
# /wheels and keeps PyPI as an index, so every internal name was looked up on
# pypi.org -- the control-plane images' build on 3a0d488c failed on a 503 from
# pypi.org/simple/swarm-redaction/ -- and a public package published under one
# of these names at a higher version could have been installed instead of ours:
# dependency confusion into the image every tenant's agent runs in. `--no-index`
# means no index is ever asked for them; `--no-deps` because their third-party
# dependencies are already installed above, by hash.
RUN uv pip install --python /opt/venv/bin/python --no-index --find-links /wheels --no-deps \
      swarm-common swarm-redaction swarm-workflow-rollup swarm-agent-worker

# pytest for the generic runner's `python -m pytest` (see the venv step above).
RUN uv pip install --python /opt/venv/bin/python "pytest==${PYTEST_VERSION}"

# With `--no-deps` on both installs nothing above notices a dependency the
# export left out. `uv pip check` does, here, instead of an ImportError at the
# runtime stage's import check or inside a tenant's task.
RUN uv pip check --python /opt/venv/bin/python

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS runtime

# Re-declared: an ARG is scoped to the stage that declares it, so the builder's
# copies above are not visible here.
ARG SETUPTOOLS_VERSION=80.9.0
ARG WHEEL_VERSION=0.46.2
ARG INSTALL_AGENT_CLIS=1
# Pinned on 2026-09-15; CI bumps these deliberately rather than by rebuilding.
# Claude Code 2.1.283 (2026-09-27): 2.1.273 refused the pinned MODEL=claude-opus-5-5
# as unrecognized_model, so every claude-code task failed (task_2ad43c35a18448fca83b).
ARG CLAUDE_CODE_VERSION=2.1.283
ARG CODEX_VERSION=0.154.0
# 1 (the default, and every runner profile but the browser's): codex is
# installed beside Claude Code. 0: the same image without it -- the build
# `agent-runtime-base-nocodex` that agent-runtime-browser is built FROM
# (BROWSER-NO-CODEX, owner decision 2026-10-13, docs/browser-startup-speed.md
# §2.3). codex is 129.7 MB as a layer and `claude-code-browser` never runs it;
# a derived image cannot drop an inherited layer (deleting adds a whiteout and
# the bytes are still pulled), so it has to be left out HERE, by this argument.
ARG INSTALL_CODEX=1

# PYTHONDONTWRITEBYTECODE=1 stays on, deliberately, now that the build
# precompiles (the compileall step below): it is about RUNTIME writes. The
# agent runs as uid 10001 and cannot write the root-owned stdlib or venv
# anyway, so all it would ever write is a __pycache__ beside a tenant's own
# code in /workspace -- files that land in the agent's diff and its
# checkpoint. The bytecode every Python start actually needs is the stdlib's
# and the venv's, and the build writes that once, as root (#1334).
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NO_COLOR=1 \
    PATH="/opt/venv/bin:/usr/local/share/npm-global/bin:/usr/local/bin:/usr/bin:/bin" \
    NPM_CONFIG_PREFIX=/usr/local/share/npm-global \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    WORKSPACE_ROOT=/workspace \
    HOME=/home/swarm

# The agent's toolbox. `fd-find` installs the binary as `fdfind` on Debian, so
# it gets the name every agent and every README expects.
# `apt-get upgrade` is not belt-and-braces here. The base image is pinned to a
# digest, deliberately, so its packages stay at whatever the snapshot shipped
# even as Debian publishes fixes for them -- a pinned base is a FROZEN set of
# CVEs, not a safe one. `install` alone never touches a package that is already
# present, which is how libpcre2-8-0 sat at 10.42-1 with a fixed 10.42-1+deb12u1
# available and failed the promote scan on every image at once.
# APT_REFRESH: the build scripts/build-images.sh makes this layer in (every
# main build and release), so the upgrade below is never an older build's
# (owner decision 2026-10-14).
#
# PostgreSQL 16's server, for the test services a repository declares in
# .swarm/test-services.toml (docs/design/test-services.md §3.1, §4.1; owner
# decision 2026-10-23: option A, 16 only). The worker -- never the agent,
# never the repository -- runs `initdb` and `postgres` as its own child at
# runtime; THIS BUILD STARTS NOTHING AND CREATES NO DATA DIRECTORY:
# create_main_cluster is turned off before the server's postinst runs, and
# the build fails if a cluster appears anyway.
#
# From PGDG, the PostgreSQL project's own apt archive, because bookworm's
# carries only 15. Its signing key (ACCC4CF8) is fetched over HTTPS and must
# hold exactly one primary key, whose FULL fingerprint is
# PGDG_KEY_FINGERPRINT, before it becomes the keyring apt checks the archive
# against; any other key, a second one, or none fails the build before apt
# trusts anything. (`--show-keys`, not `--import`: an import starts
# gpg-agent, which `gpg` without its recommends does not carry.) The fingerprint
# is the one https://www.postgresql.org/download/linux/debian/ publishes, and
# https://www.postgresql.org/media/keys/ACCC4CF8.asc hashed to it on
# 2026-10-24. gpg is installed for that check alone and purged in this RUN.
#
# The server sits in THIS layer so that APT_REFRESH brings every 16.x minor
# on the next build, exactly as it brings Debian's fixes. The archive floats,
# so the build refuses anything older than POSTGRES_MIN_VERSION, the release
# measured when this went in (16.15-1.pgdg12+2, 2026-10-24). The PGDG source
# and keyring are removed once the server is in: the derived images run their
# own `apt-get update` and must not start depending on apt.postgresql.org.
#
# The scan (scripts/lib/image-scan.sh): trivy matches Debian packages against
# Debian's tracker, which does not track PGDG's postgresql-16, so findings on
# the server itself are under-reported, not over-reported (design §4.1). Its
# libraries (libllvm19, libicu72, libxml2, libxslt1.1, locales) are Debian's
# and are tracked. ssl-cert's postinst generates a snakeoil key pair, the same
# in every pull of the image; the service listens on loopback without TLS, so
# the pair is deleted.
#
# CHROMIUM'S SYSTEM LIBRARIES AND FONTS, AND NO CHROMIUM (BGD, 2026-10-26,
# fix (c) on #2253). A step on plain claude-code must be able to run a
# repository's own headless suite: `npx playwright install chromium`, then
# `npx playwright test`. Before this, the install worked and the launch did
# not: `libglib-2.0.so.0` was missing, and the agent is not root, so
# `install --with-deps` could not add it (task_69128d776a52467488e4).
#   * The libraries are exactly what Playwright's `install-deps chromium`
#     would add on this distro: playwright-core's own `debian12-x64.chromium`
#     list (the same 21 names in 1.63.0, the browser image's pin, and 1.64.1,
#     read 2026-10-26). Their dependencies follow with
#     `--no-install-recommends`, as everything here does.
#   * The fonts: fontconfig (and libfontconfig1/libfreetype6, which the
#     renderer loads), fonts-liberation (metric-compatible Arial, Times and
#     Courier, what a page's default sans/serif/mono resolve to) and
#     fonts-noto-color-emoji. Without a font fontconfig can find, the renderer
#     dies with "Skia FontConfig FATAL" (task_e97a6439de29456ba6ab got around
#     it by unpacking fonts under /tmp by hand). The build checks that
#     fontconfig finds both families and that the libraries Chromium loads
#     resolve, so a list that installs and still cannot launch fails here.
#   * Left out of Playwright's `tools` list on purpose: xvfb and
#     xfonts-scalable (a headed display; this image only runs headless, and
#     the browser image adds them), and the CJK, Thai, unifont and freefont
#     families (another ~48 MiB installed, by bookworm's package index).
#   * NO BROWSER BINARY: each repository pins its own @playwright/test and so
#     its own Chromium revision, which `npx playwright install chromium` puts
#     in the agent's writable cache (`$XDG_CACHE_HOME/ms-playwright`; the
#     worker sets XDG_CACHE_HOME, cachedirs.py). A baked revision would be the
#     wrong one for almost every repository, and ~170 MB on every start.
#   * The cost, by bookworm's index on 2026-10-26 (no daemon in the authoring
#     container to build the image): 51 packages, ~50 MiB installed, ~23 MiB
#     of .debs, paid on every claude-code and codex start. In this layer so
#     that APT_REFRESH upgrades them with everything else. agent-runtime-browser
#     is built FROM this image, so its `--with-deps` now finds them present.
ARG PGDG_KEY_FINGERPRINT=B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8
ARG POSTGRES_MAJOR=16
ARG POSTGRES_MIN_VERSION=16.15
ARG APT_REFRESH=unset
RUN echo "apt layer of ${APT_REFRESH}" \
 && apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y \
      bash \
      build-essential \
      ca-certificates \
      curl \
      fd-find \
      git \
      less \
      make \
      openssh-client \
      procps \
      ripgrep \
      tini \
      unzip \
      wget \
 && ln -sf "$(command -v fdfind)" /usr/local/bin/fd \
 && apt-get install --no-install-recommends -y \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 \
      libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 \
      libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
      libxfixes3 libxkbcommon0 libxrandr2 \
      fontconfig libfontconfig1 libfreetype6 fonts-liberation fonts-noto-color-emoji \
 && fc-cache -f \
 && fc-list : family | grep -q "Liberation Sans" \
 && fc-list : family | grep -q "Noto Color Emoji" \
 && n=0 && for lib in libglib-2.0.so.0 libnss3.so libnspr4.so libatk-1.0.so.0 libatk-bridge-2.0.so.0 libcups.so.2 libdrm.so.2 libgbm.so.1 libxkbcommon.so.0 libXcomposite.so.1 libXdamage.so.1 libXrandr.so.2 libasound.so.2 libpango-1.0.so.0 libcairo.so.2 libdbus-1.so.3 libfontconfig.so.1; do \
      if ! /sbin/ldconfig -p | grep -q "^[[:space:]]*${lib} "; then echo "headless Chromium: ${lib} does not resolve" >&2; exit 1; fi; \
      n=$((n + 1)); \
    done \
 && echo "headless Chromium: ${n} libraries resolve, fontconfig finds Liberation Sans and Noto Color Emoji" \
 && apt-get install --no-install-recommends -y gpg \
 && GNUPGHOME="$(mktemp -d)" && export GNUPGHOME \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/pgdg-key.asc \
      "https://www.postgresql.org/media/keys/ACCC4CF8.asc" \
 && test "$(gpg --batch --with-colons --show-keys /tmp/pgdg-key.asc \
      | awk -F: '$1 == "fpr" && prev == "pub" { print $10 } { prev = $1 }')" = "${PGDG_KEY_FINGERPRINT}" \
 && gpg --batch --dearmor --output /usr/share/keyrings/pgdg.gpg /tmp/pgdg-key.asc \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install --no-install-recommends -y postgresql-common \
 && sed -ri 's/^#?create_main_cluster = .*/create_main_cluster = false/' /etc/postgresql-common/createcluster.conf \
 && grep -qx 'create_main_cluster = false' /etc/postgresql-common/createcluster.conf \
 && apt-get install --no-install-recommends -y "postgresql-${POSTGRES_MAJOR}" \
 && if ! dpkg --compare-versions "$(dpkg-query -W -f='${Version}' "postgresql-${POSTGRES_MAJOR}")" ge "${POSTGRES_MIN_VERSION}"; then \
      echo "postgresql-${POSTGRES_MAJOR} is older than POSTGRES_MIN_VERSION=${POSTGRES_MIN_VERSION}" >&2; exit 1; \
    fi \
 && test ! -e "/var/lib/postgresql/${POSTGRES_MAJOR}" \
 && test ! -e "/etc/postgresql/${POSTGRES_MAJOR}" \
 && for t in postgres initdb pg_ctl pg_isready; do \
      ln -s "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/$t" "/usr/local/bin/$t"; \
    done \
 && rm -f /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem \
 && rm -rf "${GNUPGHOME}" /tmp/pgdg-key.asc \
 && apt-get purge -y --auto-remove gpg \
 && rm -f /etc/apt/sources.list.d/pgdg.list /usr/share/keyrings/pgdg.gpg \
 && rm -rf /var/lib/apt/lists/*

# Node LTS, copied from the pinned official image rather than from a script.
COPY --from=nodebin /usr/local/bin/node /usr/local/bin/node
COPY --from=nodebin /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# npm bundles its own copy of brace-expansion, hoisted to
# node_modules/npm/node_modules/brace-expansion -- not a Debian package, so
# `apt-get upgrade` above never touches it. The copy NODE_IMAGE carries is
# 5.0.7 (verified against the exact bundled npm: NODE_IMAGE resolves to Node
# 24.21.0 -- read from the image's OCI source annotation, which points at
# nodejs/docker-node.git 93a7bafc "24/bookworm-slim" -- and Node 24.21.0 bundles
# npm 11.19.0 per https://nodejs.org/dist/index.json; npm@11.19.0's own
# published tree carries brace-expansion 5.0.7, matching trivy exactly), which
# has CVE-2026-102276 (HIGH: stack exhaustion DoS from a crafted brace
# pattern, fixed upstream in 5.0.10) and CVE-2026-102278 (uncontrolled
# recursion in nested patterns, fixed in 5.0.11). No published npm release
# bundles the fix yet -- checked against npm's own `latest` (12.1.0) on
# 2026-09-29, which still ships 5.0.9, one patch short -- so this replaces the
# module in place rather than waiting on an npm bump.
#
# This stays on the 5.x line (5.0.12, which folds in the 5.0.11 fix for
# CVE-2026-102278 too) rather than dropping in the pre-rewrite 2.x/1.x lines
# trivy also lists as fixed. Read straight from npm@11.19.0's own bundled
# tree (https://unpkg.com/npm@11.19.0/node_modules/minimatch/package.json and
# its dist/commonjs/index.js): npm's bundled minimatch is 10.2.5, which
# depends on `brace-expansion@^5.0.5` and calls it as
# `(0, brace_expansion_1.expand)(pattern, {...})` -- a NAMED `expand` export.
# The 2.x/1.x lines only ever exported a default `module.exports = expandTop`
# function with no `.expand` property, so swapping in 2.1.6 (tried in an
# earlier revision of this change, before this was checked) would have left
# `brace_expansion_1.expand` undefined and broken every npm codepath that
# globs -- `npm pack`, `npm ci`'s `files` matching, `npm ls` -- silently past
# a build that only called brace-expansion directly. brace-expansion 5.0.7,
# 5.0.10 and 5.0.12 all define `exports.expand = expand` (checked against
# each version's dist/commonjs/index.js), so 5.0.12 is a same-API patch.
#
# brace-expansion 5.0.12 depends on `balanced-match@^4.0.2`. An earlier
# revision of this change also vendored a pinned balanced-match, having found
# that balanced-match@4.0.2 pulls in `jackspeak@^4.2.3` (a CLI-arg parser,
# no legitimate reason for a balanced-brace matcher to need it at runtime).
# That check was against the wrong version: npm@11.19.0 already bundles
# balanced-match at 4.0.4 (node_modules/npm/node_modules/balanced-match,
# https://registry.npmjs.org/balanced-match/4.0.4 -- "dependencies" is absent
# entirely), which satisfies brace-expansion 5.0.12's `^4.0.2` range and is
# exactly the balanced-match 5.0.7 already resolves against today, so nothing
# about the dependency graph changes -- only brace-expansion's own directory
# is replaced, and Node's module resolution finds the existing sibling
# `balanced-match` by walking up to npm's own node_modules, the same way it
# does for every other npm-bundled package today.
#
# The tarball is pinned by exact version and verified against an
# independently-recomputed sha512 before extraction -- the same
# download-then-verify shape UV_IMAGE and NODE_IMAGE get from a
# digest-addressed registry, just without one available for a single bundled
# module. The build then proves the swap two ways: a direct call through the
# same named `expand` export minimatch uses, and an end-to-end run of `npm
# pack`, which walks a `files` glob through npm's own bundled minimatch/glob
# and writes a real tarball -- the actual codepath a mismatched API would
# break -- so a bad swap fails the build rather than shipping quietly.
#
# undici, tar and ip-address are patched the same way, in this same RUN step
# rather than a second one, and for the same reason as brace-expansion: each
# is bundled inside npm's own node_modules, not installed via apt, so
# `apt-get upgrade` never reaches it, and each was checked against npm's
# actual bundled tree (npm@11.19.0, per the Node/npm version chain above),
# not assumed:
#
# undici 6.27.0 (node_modules/npm/node_modules/undici) has CVE-2026-19534
# (HIGH: DoS via unrequested WebSocket subprotocol, fixed in 6.28.1/7.29.1/
# 8.10.2) -- caught on 2026-09-30's dev release after brace-expansion's fix
# (#371) merged and cleared that finding. The sole consumer inside npm's tree
# is bundled node-gyp (checked by extracting the real npm@11.19.0 tarball and
# grepping every .js file for `require('undici')`): node-gyp's
# lib/download.js does `const { Agent, EnvHttpProxyAgent, RetryAgent, fetch }
# = require('undici')`, and node-gyp's own package.json pins
# `"undici": "^6.25.0"` -- so this stays on major 6, at 6.29.0 (the newest
# 6.x release, dist-tag `six`, which folds in 6.28.1's fix plus several
# npm-registry-published moderate/low undici advisories also fixed by
# <6.28.1/<6.29.0 that trivy hadn't flagged yet). undici has zero
# dependencies at both 6.27.0 and 6.29.0 (checked each version's
# package.json), and 6.29.0's index.js still exports Agent,
# EnvHttpProxyAgent, RetryAgent and fetch the same way -- same-API patch.
#
# tar and ip-address were not in trivy's failing set, but the npm registry's
# own bulk security-advisory endpoint
# (POST /-/npm/v1/security/advisories/bulk), queried against every
# package+version actually bundled in npm@11.19.0 (141 packages, walked from
# the real tarball's node_modules), returned two more HIGH advisories with a
# published fix:
#
#   - tar 7.5.19 (node_modules/npm/node_modules/tar): uncontrolled recursion
#     in mapHas/filesFilter, an uncatchable stack-overflow DoS from a crafted
#     long-path tar with member selection (GHSA-r292-9mhp-454m, fixed
#     >7.5.20). Bumped to 7.5.22 (latest). tar's own "dependencies" are
#     identical at 7.5.19 and 7.5.22 (chownr ^3.0.0, yallist ^5.0.0, minipass
#     ^7.1.2, minizlib ^3.1.0, @isaacs/fs-minipass ^4.0.0), so npm's
#     already-bundled copies of those satisfy the upgrade with nothing new
#     vendored.
#   - ip-address 10.2.0 (node_modules/npm/node_modules/ip-address): the worst
#     of eight advisories on this bundled version is HIGH
#     (GHSA-mwp4-54f8-5fhr, leading-zero octet decoding lets Address4 admit
#     addresses resolvers read as octal -- an SSRF/trust-boundary bypass,
#     fixed >10.3.0); the rest are moderate SSRF/DoS variants, all fixed by
#     <=10.7.2. Bumped to 10.7.2 (latest). ip-address has zero dependencies
#     at either version, and the sole consumer inside npm's tree, bundled
#     `socks` (required by @npmcli/agent's socks-proxy-agent, for SOCKS proxy
#     support), pins `"ip-address": "^10.1.1"`, which 10.7.2 satisfies; its
#     `require('ip-address')` call sites (socks/build/common/helpers.js,
#     socks/build/client/socksclient.js) use the same `Address4`/`Address6`
#     exports both versions carry.
#
# Neither trivy's own scan output nor this npm-registry query is assumed
# complete going forward -- this is what was true for npm@11.19.0 on
# 2026-09-30, checked directly against the registry, not carried over from
# the last release.
ARG BRACE_EXPANSION_VERSION=5.0.12
ARG BRACE_EXPANSION_SHA512=628bd0debce168b308ac38cd0cc90d4b4d6d79af77aa11211b9c72f1febe47497e770dca8bee6c0a822823de368ae2d94c75a881692d830543854d3d88d12299
# 6.29.1 (2026-10-24): trivy flagged CVE-2026-109869 (HIGH) in 6.29.0, fixed in
# 6.29.1 / 7.30.1 / 8.12.2; every release and image build failed on it from
# 2026-10-24 ~01:17Z. Still major 6 for node-gyp's "^6.25.0", still zero
# dependencies (registry package.json), same index.js exports.
ARG UNDICI_VERSION=6.29.1
ARG UNDICI_SHA512=12f46552dfd0a8012f5dd41e44c6147f0390b1fce90f3b7292b1b6a3018413fac1381a18bfdb85dfd673b0d0e1e254c51e14ebd973998ca91882e783d7666618
ARG TAR_VERSION=7.5.22
ARG TAR_SHA512=3053bf433bed00e9896e484e6824ef6c670537d2fd6fe26e9c8b03c1a2a58d239d70b31e6b7349d64f54b33feb8dd7d25d3ab875fcdf792ed6e29e1838000778
ARG IP_ADDRESS_VERSION=10.7.2
ARG IP_ADDRESS_SHA512=ec7ff68054888adc5cd211b79ce2358254bc40ba3f10704514b93cbc45235d8ff1bb401d2fc8d9f54d48cced8f526d1dd83fe87d070089f6f483a38192df14ef
RUN BE_DIR=/usr/local/lib/node_modules/npm/node_modules/brace-expansion \
 && UNDICI_DIR=/usr/local/lib/node_modules/npm/node_modules/undici \
 && TAR_DIR=/usr/local/lib/node_modules/npm/node_modules/tar \
 && IPADDR_DIR=/usr/local/lib/node_modules/npm/node_modules/ip-address \
 && BE_TGZ="/tmp/brace-expansion-${BRACE_EXPANSION_VERSION}.tgz" \
 && UNDICI_TGZ="/tmp/undici-${UNDICI_VERSION}.tgz" \
 && TAR_TGZ="/tmp/tar-${TAR_VERSION}.tgz" \
 && IPADDR_TGZ="/tmp/ip-address-${IP_ADDRESS_VERSION}.tgz" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o "${BE_TGZ}" \
      "https://registry.npmjs.org/brace-expansion/-/brace-expansion-${BRACE_EXPANSION_VERSION}.tgz" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o "${UNDICI_TGZ}" \
      "https://registry.npmjs.org/undici/-/undici-${UNDICI_VERSION}.tgz" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o "${TAR_TGZ}" \
      "https://registry.npmjs.org/tar/-/tar-${TAR_VERSION}.tgz" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o "${IPADDR_TGZ}" \
      "https://registry.npmjs.org/ip-address/-/ip-address-${IP_ADDRESS_VERSION}.tgz" \
 && echo "${BRACE_EXPANSION_SHA512}  ${BE_TGZ}" | sha512sum -c - \
 && echo "${UNDICI_SHA512}  ${UNDICI_TGZ}" | sha512sum -c - \
 && echo "${TAR_SHA512}  ${TAR_TGZ}" | sha512sum -c - \
 && echo "${IP_ADDRESS_SHA512}  ${IPADDR_TGZ}" | sha512sum -c - \
 && rm -rf "${BE_DIR}" "${UNDICI_DIR}" "${TAR_DIR}" "${IPADDR_DIR}" \
 && mkdir -p "${BE_DIR}" "${UNDICI_DIR}" "${TAR_DIR}" "${IPADDR_DIR}" \
 && tar -xzf "${BE_TGZ}" -C "${BE_DIR}" --strip-components=1 \
 && tar -xzf "${UNDICI_TGZ}" -C "${UNDICI_DIR}" --strip-components=1 \
 && tar -xzf "${TAR_TGZ}" -C "${TAR_DIR}" --strip-components=1 \
 && tar -xzf "${IPADDR_TGZ}" -C "${IPADDR_DIR}" --strip-components=1 \
 && rm -f "${BE_TGZ}" "${UNDICI_TGZ}" "${TAR_TGZ}" "${IPADDR_TGZ}" \
 && node -e "\
const assertVersion = (dir, want) => { \
  const pkg = require(dir + '/package.json'); \
  if (pkg.version !== want) { throw new Error(dir + ' landed ' + pkg.version + ', expected ' + want); } \
  return pkg.version; \
}; \
const beV = assertVersion('${BE_DIR}', '${BRACE_EXPANSION_VERSION}'); \
const { expand } = require('${BE_DIR}'); \
if (typeof expand !== 'function') { throw new Error('brace-expansion has no expand() export -- minimatch calls brace_expansion_1.expand()'); } \
const beOut = expand('a{{1,2},{3,4}}b'); \
const beWant = ['a1b','a2b','a3b','a4b']; \
if (JSON.stringify(beOut) !== JSON.stringify(beWant)) { throw new Error('brace-expansion regressed: ' + JSON.stringify(beOut)); } \
console.log('brace-expansion', beV, 'ok'); \
const undiciV = assertVersion('${UNDICI_DIR}', '${UNDICI_VERSION}'); \
const { Agent, EnvHttpProxyAgent, RetryAgent, fetch } = require('${UNDICI_DIR}'); \
for (const [name, sym] of [['Agent', Agent], ['EnvHttpProxyAgent', EnvHttpProxyAgent], ['RetryAgent', RetryAgent], ['fetch', fetch]]) { \
  if (typeof sym !== 'function') { throw new Error('undici has no ' + name + '() export -- node-gyp download.js destructures it'); } \
} \
const probeAgent = new Agent(); \
probeAgent.close().catch(() => {}); \
console.log('undici', undiciV, 'ok'); \
const tarV = assertVersion('${TAR_DIR}', '${TAR_VERSION}'); \
const tarLib = require('${TAR_DIR}'); \
for (const name of ['c', 'x', 'create', 'extract']) { \
  if (typeof tarLib[name] !== 'function') { throw new Error('tar has no ' + name + '() export'); } \
} \
console.log('tar', tarV, 'ok'); \
const ipV = assertVersion('${IPADDR_DIR}', '${IP_ADDRESS_VERSION}'); \
const { Address4, Address6, AddressError } = require('${IPADDR_DIR}'); \
const a4 = new Address4('192.168.1.1'); \
if (a4.correctForm() !== '192.168.1.1' || !a4.isPrivate()) { throw new Error('ip-address Address4 regressed on a valid literal'); } \
const a6 = new Address6('::1'); \
if (a6.correctForm() !== '::1' || !a6.isLoopback()) { throw new Error('ip-address Address6 regressed on a valid literal'); } \
let rejected = false; \
try { new Address4('999.999.999.999'); } catch (e) { rejected = e instanceof AddressError; } \
if (!rejected) { throw new Error('ip-address Address4 accepted an invalid literal'); } \
console.log('ip-address', ipV, 'ok');" \
 && export HOME=/tmp \
 && rm -rf /tmp/npm-patch-smoke \
 && mkdir -p /tmp/npm-patch-smoke \
 && cd /tmp/npm-patch-smoke \
 && npm --version \
 && npm init -y >/dev/null \
 && echo "console.log('ok')" > index.js \
 && npm pack >/dev/null \
 && test -f npm-patch-smoke-1.0.0.tgz \
 && node -e "\
const tarLib = require('${TAR_DIR}'); \
tarLib.x({ file: 'npm-patch-smoke-1.0.0.tgz', sync: true, C: '/tmp/npm-patch-smoke' }); \
console.log('npm pack + tar round-trip ok');" \
 && test -f package/index.js \
 && cd / \
 && rm -rf /tmp/npm-patch-smoke \
 && echo "npm pack (minimatch/glob/tar path) ok"

COPY --from=uvbin /uv /usr/local/bin/uv
COPY --from=uvbin /uvx /usr/local/bin/uvx

# The base image carries its OWN setuptools and wheel in /usr/local/lib, which
# is a different install from the one in /opt/venv -- upgrading the venv's copy
# left these untouched, and the promote scan kept flagging jaraco.context (path
# traversal, vendored inside setuptools) and wheel (arbitrary code execution).
#
# Upgraded here rather than removed, unlike the control-plane images. An agent
# working in this container may legitimately build a package, and the reason
# these are reachable at all is the reason they must stay.
RUN /usr/local/bin/python3 -m pip install --no-cache-dir --upgrade \
      "setuptools>=${SETUPTOOLS_VERSION}" "wheel>=${WHEEL_VERSION}"
COPY --from=builder /opt/venv /opt/venv

# Precompile the stdlib and the venv. The base image deletes every .pyc it
# ships, and PYTHONDONTWRITEBYTECODE=1 (above) stops one being cached, so
# without this every Python start in an agent container compiled the stdlib it
# imported, again: 0.21 s per call of a small Python tool against 0.08 s with
# a bytecode cache, measured by lane FIX-TESTS (PR 1415, #1334). CI runners
# never paid it; every lane in this image did.
#
# Compiled BY /opt/venv/bin/python, the first python on PATH -- the one the
# ENTRYPOINT, gcloud's launcher and every agent run -- because a .pyc carries
# its interpreter's magic number and a cache for another Python is never read.
# The paths are asked of that interpreter, not spelled out: its stdlib
# (/usr/local/lib/python3.11, with the base's own site-packages under it,
# where setuptools and wheel were just upgraded) and its purelib, the venv's
# site-packages. The builder installed the venv with UV_COMPILE_BYTECODE=1, so
# compileall finds those up to date and rewrites nothing the COPY already
# carries. After every install into either tree, and a failed compile fails
# the build. The last line proves the cache is there for the interpreter that
# runs, in this RUN (a check-only RUN can write a duplicate layer, PR 1239).
RUN py=/opt/venv/bin/python \
 && "$py" -m compileall -q -j 0 \
      "$("$py" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" \
      "$("$py" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
 && "$py" -c "import importlib.util, json, os; \
assert os.path.exists(importlib.util.cache_from_source(json.__file__)), 'no stdlib bytecode for ' + json.__file__; \
print('stdlib bytecode ok')"

# ---------------------------------------------------------------------------
# The infrastructure toolbox (docs/BUILD_PROMPT_V2.md §2.12)
# ---------------------------------------------------------------------------
# What an agent needs to work on this repository the way CLAUDE.md says work
# is checked: `gh` to open the pull request at all, `gcloud`/`kubectl` to read
# the infrastructure it is changing, the Terraform/lint/scan set `make lint`
# and `make security` call, `shellcheck` and `make` (apt, above), and the
# `docker` CLI. All of it is installed as root and owned by root; the agent
# runs it as uid 10001 and can write none of it.
#
# Every tool is the vendor's PREBUILT linux/amd64 release, never compiled here
# (an earlier attempt at this change built OpenTofu, TFLint and Trivy from
# source and starved its own heartbeat). Each is pinned to an exact version and
# checked against the sha256 written beside it BEFORE it is unpacked -- the
# same download-then-verify shape as the npm patches above. Where a vendor
# ships a .deb rather than a static binary, the .deb is fetched by its exact
# pool filename and verified the same way: its sha256 is copied from the
# vendor's GPG-signed apt index (the Packages file under dists/), so the trust
# root is the vendor's signing key without adding an apt source that would
# float with whatever the repository serves next week. One RUN per tool
# family, and each removes its own downloads, so no layer carries an archive.
#
# VERSIONS ARE CHOSEN AGAINST THE PROMOTE GATE, not by recency alone.
# scripts/push-images.sh refuses an image with any fixable HIGH or CRITICAL
# trivy finding. On 2026-10-01 every artifact below was downloaded, its sha256
# compared with the vendor's published value, and the extracted files scanned
# with trivy 0.74.0 (`trivy rootfs --scanners vuln --severity HIGH,CRITICAL
# --ignore-unfixed`, vulnerability DB updated 2026-10-01T07:23Z) -- the same
# filter the gate applies. Each pin below records what that scan found.
#
# bash with pipefail from here on: every `echo <sha> <file> | sha256sum -c -`
# below is a pipe, and so is the smoke step's output, and a pipe whose first
# command failed must not pass on the last one's exit status.
# EVERY DOWNLOAD HERE RETRIES (2026-10-14, PR 1281's flake table): `curl -fsSL
# --retry 5 --retry-all-errors --retry-connrefused`, 1+2+4+8+16 s of backoff at
# most. --retry-all-errors is what makes an HTTP error (curl's exit 22, which
# failed one main build of this image in run 38245452463) retry at all. A
# file that is really gone still fails, ~31 s later, and every download is
# still checked against its pinned checksum after it lands.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Google Cloud: gcloud, the GKE auth plugin, kubectl.
#
# gcloud 587.0.0 is the newest google-cloud-cli in the vendor's index on
# 2026-10-01. Its .deb carries a bundled CPython 3.14
# (platform/bundledpythonunix) whose site-packages had seven fixable HIGHs:
# cryptography 46.0.7 (CVE-2026-69247, CVE-2026-69249, GHSA-537c-gmf6-5ccf),
# urllib3 2.7.0 (CVE-2026-97687, CVE-2026-97689), msgpack 1.1.2
# (GHSA-6v7p-g79w-8964), setuptools 70.3.0 (CVE-2025-47273). The rest of the
# package scanned clean. So the bundled interpreter is removed between
# `dpkg --unpack` and `dpkg --configure`, and gcloud falls back to this
# image's own Python 3.11 -- its launcher accepts 3.9 through 3.14 and runs it
# with `-S`, so the worker venv's packages never load into it. Checked on
# 2026-10-01: `gcloud --version`, `gsutil version` and `bq version` from the
# unpacked 587.0.0 all run on Python 3.11 with the bundled copy deleted, and
# `gcloud components post-process` (the postinst) exits 0 on it. The
# postinst's .pyc recompile is skipped (CLOUDSDK_SKIP_PY_COMPILATION=1): it
# would compile for the interpreter that is about to be deleted.
# At runtime the launcher takes the first `python3` on PATH, which is
# /opt/venv/bin/python3. That is the same interpreter: the venv is made in the
# builder stage FROM the same PYTHON_IMAGE digest, and its python3 links to
# /usr/local/bin/python3.11. With `-S` the venv's site-packages never load.
# CLOUDSDK_PYTHON is NOT set as an ENV: the lifecycle builds the runner's
# environment rather than inheriting it, so an image ENV would not reach an
# agent's gcloud without a change to apps/agent-worker, and it would change
# nothing that runs.
#
# Source of GCLOUD_* and GKE_PLUGIN_*: the Filename and SHA256 fields of
# https://packages.cloud.google.com/apt/dists/cloud-sdk/main/binary-amd64/Packages
# (packages google-cloud-cli and google-cloud-cli-gke-gcloud-auth-plugin,
# Version 587.0.0-0), recomputed over the downloaded files. Neither .deb
# declares a Depends. The auth plugin is not on the §2.12 list, but kubectl
# cannot authenticate to a GKE cluster without it, so without it the kubectl
# below would only reach clusters nobody here runs.
ARG GCLOUD_VERSION=587.0.0
ARG GCLOUD_POOL_ID=997633f1dce2c650ae5bc92a1ec32bca
ARG GCLOUD_SHA256=117c7a96c43ecf649f91d7f775f54ff83134caeb42df4cfed8a7b92c2b701c14
# Named GKE_PLUGIN_*, not GKE_AUTH_PLUGIN_*: Trivy DS-0031 reads "AUTH" in an ARG name as a secret and fails the PR; these are public package pool ids and checksums.
ARG GKE_PLUGIN_POOL_ID=207adf5dccdc107f7d16bf1e01bd1398
ARG GKE_PLUGIN_SHA256=c572f6cf9b401820032e8459b2bade792442dfbbce30e53d3b1b0d7be1bf9e24
# kubectl 1.36.6: the newest 1.36 patch (https://dl.k8s.io/release/stable-1.36.txt
# on 2026-10-28), built on go1.26.10, which fixes CVE-2026-111943 and
# CVE-2026-56857 in kubectl (1.36.5 was go1.26.8), one minor ahead of the 1.35 control plane as kubectl's skew
# policy allows and the line docs/versions.md pins for operators. The static
# binary from dl.k8s.io rather than Google's `kubectl` .deb, which is 141 MB
# of several bundled versions. Checksum:
# https://dl.k8s.io/release/v1.36.6/bin/linux/amd64/kubectl.sha256.
ARG KUBECTL_VERSION=1.36.6
ARG KUBECTL_SHA256=29818b6729dc9262b692307470d0558d027725e738be5dd47c760261868a2634
# dpkg runs with /usr/sbin on PATH because it refuses to start without
# ldconfig and start-stop-daemon, which live there and which the runtime PATH
# deliberately omits; /usr/local/bin is there for the postinst's python3.
RUN mkdir -p /tmp/toolbox \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/google-cloud-cli.deb \
      "https://packages.cloud.google.com/apt/pool/cloud-sdk/google-cloud-cli_${GCLOUD_VERSION}-0_amd64_${GCLOUD_POOL_ID}.deb" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/gke-gcloud-auth-plugin.deb \
      "https://packages.cloud.google.com/apt/pool/cloud-sdk/google-cloud-cli-gke-gcloud-auth-plugin_${GCLOUD_VERSION}-0_amd64_${GKE_PLUGIN_POOL_ID}.deb" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/kubectl \
      "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && echo "${GCLOUD_SHA256}  /tmp/toolbox/google-cloud-cli.deb" | sha256sum -c - \
 && echo "${GKE_PLUGIN_SHA256}  /tmp/toolbox/gke-gcloud-auth-plugin.deb" | sha256sum -c - \
 && echo "${KUBECTL_SHA256}  /tmp/toolbox/kubectl" | sha256sum -c - \
 && PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
      dpkg --unpack /tmp/toolbox/google-cloud-cli.deb /tmp/toolbox/gke-gcloud-auth-plugin.deb \
 && rm -rf /usr/lib/google-cloud-sdk/platform/bundledpythonunix \
 && PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin CLOUDSDK_SKIP_PY_COMPILATION=1 \
      dpkg --configure -a \
 && test ! -e /usr/lib/google-cloud-sdk/platform/bundledpythonunix \
 && install -o root -g root -m 0755 /tmp/toolbox/kubectl /usr/local/bin/kubectl \
 && rm -rf /tmp/toolbox

# THE TOOL MIRROR. gh here, and terraform, tofu, tflint, shellcheck and trivy
# below, are read from build/tool-mirror, bind-mounted at /tmp/tool-mirror for
# the RUN that installs them and never copied into a layer. Each file sits at
# its upstream URL without `https://` and is checked against the same sha256
# as when it was downloaded from upstream. Cloud Build copies them in from
# gs://swarm-build-mirror-<project> before this build (scripts/lib/
# build-mirror.sh), and a missing object fails that step by name; a pull
# request's local build reads them from upstream against the same list,
# tool-mirror.sha256 beside this file, which a test holds equal to the pins
# here (tests/unit/scripts/test_build_mirror.py). Why: on 2026-10-12 a
# release build failed on a github.com 504 fetching shellcheck. Bumping a pin
# here means a new line there and `scripts/refresh-build-mirror.sh`
# (docs/runbooks/build-mirror.md).
#
# GitHub and Docker CLIs.
#
# gh 2.102.0: the newest release (https://api.github.com/repos/cli/cli/releases/latest,
# published 2026-09-30). Checksum from the vendor's own
# https://github.com/cli/cli/releases/download/v2.102.0/gh_2.102.0_checksums.txt
# (gh_2.102.0_linux_amd64.tar.gz). Scan: clean.
ARG GH_VERSION=2.102.0
ARG GH_SHA256=bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386
# docker CLI 29.8.2: Docker publishes no checksum beside its static tarballs,
# so this is the `docker-ce-cli` .deb, newest for bookworm in
# https://download.docker.com/linux/debian/dists/bookworm/stable/binary-amd64/Packages
# (Version 5:29.8.2-1~debian.12~bookworm), with that index's SHA256. It is the
# CLI alone -- Depends: libc6 only -- installed without its Recommends
# (buildx, compose), and with no daemon. Scan: clean.
ARG DOCKER_CLI_VERSION=29.8.2
ARG DOCKER_CLI_SHA256=ff99352d3b95ebaaf94bcdf65026714a1a72a44a999a9fc83f1386c3f30f33d6
RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror \
    mkdir -p /tmp/toolbox \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/gh.tar.gz \
      "file:///tmp/tool-mirror/github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/docker-ce-cli.deb \
      "https://download.docker.com/linux/debian/dists/bookworm/pool/stable/amd64/docker-ce-cli_${DOCKER_CLI_VERSION}-1~debian.12~bookworm_amd64.deb" \
 && echo "${GH_SHA256}  /tmp/toolbox/gh.tar.gz" | sha256sum -c - \
 && echo "${DOCKER_CLI_SHA256}  /tmp/toolbox/docker-ce-cli.deb" | sha256sum -c - \
 && tar -xzf /tmp/toolbox/gh.tar.gz -C /tmp/toolbox --strip-components=2 \
      "gh_${GH_VERSION}_linux_amd64/bin/gh" \
 && install -o root -g root -m 0755 /tmp/toolbox/gh /usr/local/bin/gh \
 && PATH=/usr/sbin:/usr/bin:/sbin:/bin dpkg -i /tmp/toolbox/docker-ce-cli.deb \
 && rm -rf /tmp/toolbox

# Terraform, and the two tools held back from the default build.
#
# terraform 1.16.4, NOT the 1.16.2 that scripts/prerequisites.sh and CI's
# terraform.yml/release.yml pin for operators: 1.16.2 is built with Go 1.26.4,
# and the scan found nine fixable HIGHs in its stdlib (CVE-2026-33818,
# CVE-2026-39821, CVE-2026-39822, CVE-2026-46600, CVE-2026-56853,
# CVE-2026-56858, CVE-2026-56859, CVE-2026-56860, CVE-2026-56862; fixed in Go
# 1.26.6). 1.16.4 (2026-09-23, newest) scanned clean. A patch release of the
# same minor reads and writes the same state and lock files. Checksum:
# https://releases.hashicorp.com/terraform/1.16.4/terraform_1.16.4_SHA256SUMS.
ARG TERRAFORM_VERSION=1.16.4
ARG TERRAFORM_SHA256=dc94af0eef1147718ad7c8daea792ed199e3e0492eec180d0adafa2a65a879df
# INSTALL_TOFU and INSTALL_TFLINT are each OFF by default because no published
# release of tofu, and none of tflint, passes the promote gate, and the gate
# refuses the whole image, not just the tool. Re-scanned on 2026-10-09 with
# trivy 0.75.0 (still the newest), the gate's filter (`trivy rootfs --scanners
# vuln --severity HIGH,CRITICAL --ignore-unfixed`, vulnerability DB updated
# 2026-10-08T19:05Z), at each newest release, on the executable binary after a
# sha256 match against the vendor's checksum file. The releases API, read
# 2026-10-09T00:01Z, lists no newer tofu or tflint release than the ones
# below; the findings are the same as the 2026-10-06 scan's:
#
#   tofu 1.13.1 (2026-10-01)   golang.org/x/mod 0.39.0 (CVE-2026-56864,
#                              CVE-2026-56865; fixed 0.40.0), google.golang.org/
#                              grpc 1.83.1 (CVE-2026-84445; fixed 1.83.2). The
#                              same two as 1.13.0 (2026-10-01 scan). The 1.12
#                              line's newest, 1.12.7 (2026-10-01), has eight:
#                              otel/sdk 1.42.0, x/mod 0.38.0, grpc 1.79.3,
#                              oras-go 2.6.1.
#   tflint 0.64.0 (2026-07-17) Go stdlib 1.26.3 (eleven CVEs, fixed 1.26.6),
#                              x/crypto 0.53.0 (CVE-2026-56854), x/mod 0.37.0,
#                              grpc 1.82.0 (CVE-2026-84304, CVE-2026-84445,
#                              GHSA-hrxh-6v49-42gf), seventeen in all. Still
#                              no newer release.
#
# Control, same scan and DB: the trivy 0.75.0 binary installed below reports
# its target and zero findings, so the filter can come out clean.
#
# trivy WAS held here too (0.74.0: grpc 1.82.1, CVE-2026-84304 and
# CVE-2026-84445). 0.75.0 (2026-10-01) carries grpc 1.83.2, x/mod 0.41.0,
# x/crypto 0.57.0 and Go 1.27.1, and that scan found nothing fixable at any
# severity, so it left the hold (#442) and is installed below by default.
#
# Building them from source with patched dependencies is ruled out (see the
# header of this section), and an ignore entry for another project's
# dependencies is the owner's decision, not this file's (#442: no waiver). So
# each install below is complete -- pinned, verified, and covered by the smoke
# step when its argument is 1. The two arguments are independent (#442,
# 2026-10-07): whichever vendor ships a release that scans clean first leaves
# the hold alone, as trivy did, without waiting for the other. Re-scan before
# flipping either default to 1, and record the release and the scan here.
#
# Checksums: https://github.com/opentofu/opentofu/releases/download/v1.13.1/tofu_1.13.1_SHA256SUMS
# (tofu_1.13.1_linux_amd64.tar.gz) and
# https://github.com/terraform-linters/tflint/releases/download/v0.64.0/checksums.txt
# (tflint_linux_amd64.zip).
ARG INSTALL_TOFU=0
ARG INSTALL_TFLINT=0
ARG TOFU_VERSION=1.13.1
ARG TOFU_SHA256=378ada19d4bc70c43732004e8159be771b23b9a5afdf059e5f8a2b3fa2c70a69
ARG TFLINT_VERSION=0.64.0
ARG TFLINT_SHA256=cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537
RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror \
    mkdir -p /tmp/toolbox \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/terraform.zip \
      "file:///tmp/tool-mirror/releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
 && echo "${TERRAFORM_SHA256}  /tmp/toolbox/terraform.zip" | sha256sum -c - \
 && unzip -q /tmp/toolbox/terraform.zip terraform -d /tmp/toolbox \
 && install -o root -g root -m 0755 /tmp/toolbox/terraform /usr/local/bin/terraform \
 && if [ "${INSTALL_TOFU}" = "1" ]; then \
      curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/tofu.tar.gz \
        "file:///tmp/tool-mirror/github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.tar.gz" \
      && echo "${TOFU_SHA256}  /tmp/toolbox/tofu.tar.gz" | sha256sum -c - \
      && tar -xzf /tmp/toolbox/tofu.tar.gz -C /tmp/toolbox tofu \
      && install -o root -g root -m 0755 /tmp/toolbox/tofu /usr/local/bin/tofu ; \
    fi \
 && if [ "${INSTALL_TFLINT}" = "1" ]; then \
      curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/tflint.zip \
        "file:///tmp/tool-mirror/github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/tflint_linux_amd64.zip" \
      && echo "${TFLINT_SHA256}  /tmp/toolbox/tflint.zip" | sha256sum -c - \
      && unzip -q /tmp/toolbox/tflint.zip tflint -d /tmp/toolbox \
      && install -o root -g root -m 0755 /tmp/toolbox/tflint /usr/local/bin/tflint ; \
    fi \
 && rm -rf /tmp/toolbox

# Linters and scanners: shellcheck and trivy.
#
# shellcheck 0.11.0, the newest release and the version docs/versions.md pins
# (Debian bookworm's apt package is 0.9.0).
# A static Haskell binary, which trivy has no analyzer for, so the gate
# cannot flag it either way. koalaman publishes no checksum
# file, so the sha256 is the asset digest GitHub records for
# shellcheck-v0.11.0.linux.x86_64.tar.gz in
# https://api.github.com/repos/koalaman/shellcheck/releases/latest, recomputed
# over the downloaded file.
#
# trivy 0.75.0 (2026-10-01), the newest release: it scanned clean on
# 2026-10-06 (see INSTALL_TOFU and INSTALL_TFLINT above for the scan and why
# 0.74.0 could not ship). It is the agent's scanner, not the gate's: CI and the release pin
# their own trivy in .github/workflows. Checksum from
# https://github.com/aquasecurity/trivy/releases/download/v0.75.0/trivy_0.75.0_checksums.txt
# (trivy_0.75.0_Linux-64bit.tar.gz), recomputed over the downloaded file.
ARG SHELLCHECK_VERSION=0.11.0
ARG SHELLCHECK_SHA256=b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6
ARG TRIVY_VERSION=0.75.0
ARG TRIVY_SHA256=c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f
RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror \
    mkdir -p /tmp/toolbox \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/shellcheck.tar.gz \
      "file:///tmp/tool-mirror/github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.x86_64.tar.gz" \
 && echo "${SHELLCHECK_SHA256}  /tmp/toolbox/shellcheck.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/toolbox/shellcheck.tar.gz -C /tmp/toolbox --strip-components=1 \
      "shellcheck-v${SHELLCHECK_VERSION}/shellcheck" \
 && install -o root -g root -m 0755 /tmp/toolbox/shellcheck /usr/local/bin/shellcheck \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/trivy.tar.gz \
      "file:///tmp/tool-mirror/github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
 && echo "${TRIVY_SHA256}  /tmp/toolbox/trivy.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/toolbox/trivy.tar.gz -C /tmp/toolbox trivy \
 && install -o root -g root -m 0755 /tmp/toolbox/trivy /usr/local/bin/trivy \
 && rm -rf /tmp/toolbox

# jq 1.7.1, the release's static linux/amd64 binary, in place of Debian
# bookworm's jq 1.6 (which apt installed above until #1334). Two reasons: an
# agent running this repository's scripts got 1.6's behaviour where CI's
# runners have 1.7, and 1.6 takes 27 ms to start (lane FIX-TESTS, PR 1415)
# on every one of the many jq calls a script makes: 50 calls of `jq .` took
# 1.39 s with Debian's 1.6 and 0.26 s with this binary, in a SwarmCloud agent
# container on 2026-10-17. /usr/local/bin precedes /usr/bin on PATH, and nothing
# installs Debian's jq any more, so there is one jq. Checksum from the
# vendor's own https://github.com/jqlang/jq/releases/download/jq-1.7.1/sha256sum.txt
# (jq-linux-amd64), recomputed over the downloaded file on 2026-10-17. A
# static C binary, which trivy has no analyzer for, as with shellcheck.
# Read from the tool mirror like the tools above; the build fails if the
# installed binary does not report the pinned version.
ARG JQ_VERSION=1.7.1
ARG JQ_SHA256=5942c9b0934e510ee61eb3e30273f1b3fe2590df93933a93d7c58b81d19c8ff5
RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror \
    mkdir -p /tmp/toolbox \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/jq \
      "file:///tmp/tool-mirror/github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-amd64" \
 && echo "${JQ_SHA256}  /tmp/toolbox/jq" | sha256sum -c - \
 && install -o root -g root -m 0755 /tmp/toolbox/jq /usr/local/bin/jq \
 && rm -rf /tmp/toolbox \
 && test "$(/usr/local/bin/jq --version)" = "jq-${JQ_VERSION}"

# The CodeRabbit CLI (CODERABBIT-IMAGE, part of #2442; owner decision
# 2026-10-26): a lane whose repository's rules ask for a local CodeRabbit
# review runs `coderabbit review` itself (docs/worker-images.md, "CodeRabbit").
#
# 0.9.1 is the release the vendor's installer binds Linux to
# (BUNDLED_LINUX_VERSION in https://cli.coderabbit.ai/install.sh, read
# 2026-10-26). Checksum: the coderabbit-linux-x64.zip line of
# https://cli.coderabbit.ai/releases/0.9.1/SHA256SUMS, recomputed over the
# downloaded file, and that SHA256SUMS's signature (SHA256SUMS.sig) verified
# with `openssl dgst -sha256 -verify` against the release public key the
# installer carries, on 2026-10-26. The installer itself is NOT run: it
# writes to ~/.local/bin, edits shell profiles and offers a login.
#
# FROM UPSTREAM, NOT THE TOOL MIRROR: the mirror's manifest admits only
# github.com and releases.hashicorp.com objects (scripts/lib/build-mirror.sh
# tool_mirror_rows), and the CLI is served from cli.coderabbit.ai, as gcloud
# is served from packages.cloud.google.com above. Moving it into the mirror
# is a change to that list and a bucket refresh, an operator's step.
#
# SIZE: the zip is 42.9 MB (what a node pulls, compressed); the binary it
# holds is 95,487,968 bytes, 91.1 MiB, on disk -- a single-file executable
# with its JavaScript runtime inside, paid on every claude-code and codex
# start. That is the price of the owner's decision to carry it here rather
# than have each lane download it.
# SCAN: trivy 0.75.0 `rootfs --scanners vuln --severity HIGH,CRITICAL
# --ignore-unfixed` over the extracted binary, 2026-10-26, finds no package
# it can analyse in it: nothing for the promote gate to refuse.
#
# ROOT-OWNED, outside the agent's reach: /opt/coderabbit/coderabbit, mode
# 0755, so the pin holds -- the CLI's self-update cannot replace it, and the
# launcher switches the attempt off anyway. What PATH finds is the launcher,
# images/agent-runtime-base/coderabbit, which hands the CLI the workspace's
# key when the worker released one: the CLI reads its key only from
# `--api-key`, never from the environment (its own --help, 0.9.1). The build
# runs the binary once, with a throwaway HOME, and fails unless it reports
# the pinned version.
ARG CODERABBIT_VERSION=0.9.1
ARG CODERABBIT_SHA256=9558e02f1b6570ade33d8191b33e10ffae2472ab2680d4518d03261bcebc80bc
RUN mkdir -p /tmp/toolbox /opt/coderabbit \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/coderabbit.zip \
      "https://cli.coderabbit.ai/releases/${CODERABBIT_VERSION}/coderabbit-linux-x64.zip" \
 && echo "${CODERABBIT_SHA256}  /tmp/toolbox/coderabbit.zip" | sha256sum -c - \
 && unzip -q /tmp/toolbox/coderabbit.zip coderabbit -d /tmp/toolbox \
 && install -o root -g root -m 0755 /tmp/toolbox/coderabbit /opt/coderabbit/coderabbit \
 && rm -rf /tmp/toolbox \
 && crhome="$(mktemp -d)" \
 && test "$(HOME="${crhome}" CODERABBIT_CLI_DISABLE_AUTO_UPDATE=true CR_CLI_TELEMETRY_DISABLED=true /opt/coderabbit/coderabbit --version)" = "${CODERABBIT_VERSION}" \
 && rm -rf "${crhome}"
COPY --chmod=0555 images/agent-runtime-base/coderabbit /usr/local/bin/coderabbit

# checkov is Python, so it gets its OWN environment (/opt/uv-tools/checkov,
# entry point in /usr/local/bin) and can never change the worker's /opt/venv
# or the agent's `pip install` target. 3.3.17 is the version CI's
# security.yml/terraform.yml and scripts/prerequisites.sh pin. A pinned
# checkov alone still resolves ~95 transitive dependencies to whatever PyPI
# serves on build day, so --exclude-newer pins the RESOLUTION to the date it
# was scanned: on 2026-10-01 `uv tool install --exclude-newer
# 2026-10-01T00:00:00Z checkov==3.3.17` on Python 3.11 with uv 0.12.15 (this
# image's) resolved 96 packages, identical to an unconstrained resolve that
# day, and the scan found no fixable HIGH or CRITICAL in them. Bump both
# together, and re-scan. Version source: https://pypi.org/project/checkov/3.3.17/
# MOVED 2026-10-19 to 2026-10-19T00:00:00Z, checkov unchanged: the 10-01 cutoff
# resolved aiohttp 3.14.3, and release 39309249439 refused both base images on
# CVE-2026-111064 and GHSA-2g87-pp6c-29x5 (fixed in 3.14.4, published
# 2026-10-05; 3.14.5 on 2026-10-11). The image scan in CI is the re-scan.
ARG CHECKOV_VERSION=3.3.17
# MOVED 2026-10-21 to 2026-10-21T00:00:00Z, checkov unchanged: the 10-19 cutoff
# resolved certifi 2026.10.18, and the image scan refused the base images on
# GHSA-6q48-6c7w-9gw2 (CRITICAL: bundled emSign root CAs with compromised keys),
# fixed in certifi 2026.10.19, published 2026-10-20T02:24Z.
ARG CHECKOV_EXCLUDE_NEWER=2026-10-21T00:00:00Z
RUN UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/uv-cache \
      uv tool install --python /usr/local/bin/python3.11 \
        --exclude-newer "${CHECKOV_EXCLUDE_NEWER}" "checkov==${CHECKOV_VERSION}" \
 && rm -rf /tmp/uv-cache

# The coding-agent CLIs the claude-code and codex runner profiles start. Both
# are optional at build time so the base image can be rebuilt in an environment
# with no npm registry access; the runners fail with a clear message if absent.
#
# codex is left out IN THIS RUN when INSTALL_CODEX=0, never removed by a later
# one: a later RUN would only white it out, and a check-only RUN can write a
# duplicate layer (PR 1239). With the default the command is the one it always
# was: one `npm install -g` of both CLIs.
RUN if [ "${INSTALL_AGENT_CLIS}" = "1" ]; then \
      codex="" \
      && if [ "${INSTALL_CODEX}" = "1" ]; then codex="@openai/codex@${CODEX_VERSION}"; fi \
      && mkdir -p "${NPM_CONFIG_PREFIX}" \
      && npm install -g --no-audit --no-fund \
           "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
           ${codex:+"${codex}"} \
      && npm cache clean --force ; \
    fi

# THE LANE QUALITY GATES (JEV-LANES, BGD 2026-10-31; #642: 25% of lane PRs were
# red on their first CI run). Two Claude Code hooks checked by TypeSafe's Jev,
# pinned, MIT both:
#   * jev-belay v0.2.0 (valentynkit/jev-belay at commit
#     ef719db7eaadc56aa4def86c4da4ffff5bcbca35, its v0.2.0 tag), a Stop hook
#     that blocks an unverified "done". It is not on the npm registry, and no
#     Dockerfile here fetches from GitHub (docs/runbooks/build-mirror.md), so
#     its three files are VENDORED beside the launcher, jev-gates/vendor/
#     jev-belay, byte for byte; tests/unit/worker/test_quality_gates.py holds
#     each file to the commit's sha256.
#   * jev-enforce v1.1.1 (erkamyaman/jev-enforce), the final reply and every
#     Edit/Write checked against the repository's CLAUDE.md rules: the npm
#     registry's tarball, checked against the sha512 below (the registry's
#     own `dist.integrity`; its `gitHead` is the v1.1.1 tag's commit
#     ed9c7d79bad2bfdea474df5e414b8b601c0e92cd, and its dist/src is that
#     commit's, file for file). Retried like the npm tarballs above.
# Neither has a runtime dependency, so only their own files are kept,
# root-owned and read-only, off PATH.
# They are NOT a plugin the CLI enables by itself: the claude-code runner
# registers them in its `--settings` hooks (`claude_code.write_headless_settings`)
# only when the worker handed it the gates' config, i.e. the workspace turned
# `quality_gates.jev` on and its TypeSafe key was readable, and every hook
# command is the launcher below (`swarm-jev-gate.mjs`), which holds the key in
# its own process, sends only to TypeSafe's API and scrubs the registered
# secrets (docs/jev-data-flow.md, "The lane hooks"). Without that config, or
# without TYPESAFE_API_KEY, both are inert. With INSTALL_AGENT_CLIS=0 nothing
# is fetched or installed but the launcher, and the runner, finding no tool,
# registers nothing.
ARG JEV_ENFORCE_VERSION=1.1.1
ARG JEV_ENFORCE_SHA512=9964c65068cb2e6b423d610c66b90b82f6fe391b3c3d85a9e8eac1e75fd1c160b573720ca0dce47e8fa104a7ff234299e7aa6360cafe7afef7ce04a019ea2ddf
RUN --mount=type=bind,source=images/agent-runtime-base/jev-gates,target=/tmp/jev-gates-build \
    install -d -m 0755 /opt/swarm/jev-gates /opt/swarm/jev-gates/bin \
 && install -o root -g root -m 0555 /tmp/jev-gates-build/bin/swarm-jev-gate.mjs /opt/swarm/jev-gates/bin/swarm-jev-gate.mjs \
 && if [ "${INSTALL_AGENT_CLIS}" = "1" ]; then \
      JE_TGZ="/tmp/jev-enforce-${JEV_ENFORCE_VERSION}.tgz" \
      && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o "${JE_TGZ}" \
           "https://registry.npmjs.org/jev-enforce/-/jev-enforce-${JEV_ENFORCE_VERSION}.tgz" \
      && echo "${JEV_ENFORCE_SHA512}  ${JE_TGZ}" | sha512sum -c - \
      && install -d -m 0755 /tmp/jev-enforce-src /opt/swarm/jev-gates/jev-belay /opt/swarm/jev-gates/jev-enforce/dist/src \
      && tar -xzf "${JE_TGZ}" -C /tmp/jev-enforce-src --strip-components=1 \
      && grep -q "\"version\": \"${JEV_ENFORCE_VERSION}\"" /tmp/jev-enforce-src/package.json \
      && install -o root -g root -m 0444 /tmp/jev-enforce-src/package.json /tmp/jev-enforce-src/LICENSE \
           /opt/swarm/jev-gates/jev-enforce/ \
      && install -o root -g root -m 0444 /tmp/jev-enforce-src/dist/src/*.js /opt/swarm/jev-gates/jev-enforce/dist/src/ \
      && install -o root -g root -m 0444 /tmp/jev-gates-build/vendor/jev-belay/belay.mjs \
           /tmp/jev-gates-build/vendor/jev-belay/package.json /tmp/jev-gates-build/vendor/jev-belay/LICENSE \
           /opt/swarm/jev-gates/jev-belay/ \
      && rm -rf /tmp/jev-enforce-src "${JE_TGZ}" ; \
    fi \
 && chmod -R a+rX,a-w /opt/swarm/jev-gates

# A warmed npm cache for this repository's UI (docs/design/agent-autonomy.md
# §3.10, lane AU-8). Built from apps/swarm-ui's lockfile with `npm ci --cache
# /opt/npm-cache`; the node_modules that install wrote is then thrown away and
# only the cache is kept. A copied node_modules is NOT baked in: it would be
# wrong for every other repository, and for this one the day the lockfile
# changes. The cache is content-addressed by each package's integrity hash, so
# a cache built from an older lockfile still serves every package the new one
# kept.
#
# A bind mount, not a COPY. BuildKit keys this RUN's cache on the mounted
# files, so a lockfile change rebuilds the layer, and every main build and
# release rebuilds this image anyway (APT_REFRESH above). A COPY would also make
# apps/swarm-ui/package-lock.json an image_inputs source in
# scripts/build-images.sh, so every pull request that touches the UI lockfile
# would build the four worker images too. The two files are kept at
# /opt/npm-cache-lockfile as the record of what the cache was built from.
# `--ignore-scripts`: nothing the lockfile names runs as root in this build,
# and no install script adds to the cache.
#
# `npm clean-install` is npm's own long name for `npm ci`, the same command.
# It is spelled out because `npm ci` in this file is what
# tests/unit/scripts/test_indexer_image_split.py reads as the repository
# index's language-server install (images/agent-runtime-indexer) leaking back
# into the base.
#
# READ-ONLY: root-owned, every write bit cleared, so no agent can plant a
# tarball in it for the next npm to install. npm's builtin config
# (/usr/local/lib/node_modules/npm/npmrc, read by every npm whatever HOME and
# the environment are) names it as the cache, with prefer-offline. Placed
# after the CLI install above, whose `npm cache clean` must not meet it.
#
# MEASURED 2026-10-22 with npm 11.19.0, this image's: an install whose every
# package is in a read-only cache succeeds offline (235 packages in 4 s); one
# that needs a package the cache does not hold fails with EACCES on
# _cacache/tmp rather than falling over to the network. And the worker sets
# npm_config_cache to the attempt's own writable cache (agent_worker.cachedirs),
# which overrides any npmrc, so an agent's npm reads this cache only once the
# worker points it here. Both are questions to the owner in lane AU-8's PR;
# nothing here changes what an agent's npm does today.
RUN --mount=type=bind,source=apps/swarm-ui,target=/tmp/swarm-ui \
    mkdir -p /tmp/npm-warm /opt/npm-cache-lockfile \
 && cp /tmp/swarm-ui/package.json /tmp/swarm-ui/package-lock.json /opt/npm-cache-lockfile/ \
 && cp /opt/npm-cache-lockfile/package.json /opt/npm-cache-lockfile/package-lock.json /tmp/npm-warm/ \
 && cd /tmp/npm-warm \
 && npm clean-install --cache /opt/npm-cache --ignore-scripts --no-audit --no-fund \
 && cd / \
 && rm -rf /tmp/npm-warm \
 && test -d /opt/npm-cache/_cacache \
 && printf '%s\n' 'cache=/opt/npm-cache' 'prefer-offline=true' \
      > /usr/local/lib/node_modules/npm/npmrc \
 && test "$(npm config get cache --logs-max=0)" = /opt/npm-cache \
 && test "$(npm config get prefer-offline --logs-max=0)" = true \
 && rm -rf /opt/npm-cache/_logs \
 && chown -R root:root /opt/npm-cache /opt/npm-cache-lockfile /usr/local/lib/node_modules/npm/npmrc \
 && chmod -R a-w /opt/npm-cache /opt/npm-cache-lockfile /usr/local/lib/node_modules/npm/npmrc \
 && echo "npm cache warmed from apps/swarm-ui/package-lock.json: $(du -sh /opt/npm-cache | cut -f1)"

# Non-root, with a fixed uid so the GKE pod spec and the Cloud Run Job agree.
# ABSOLUTE PATHS ON PURPOSE: the runtime PATH above deliberately omits
# /usr/sbin so the unprivileged agent cannot reach admin tooling, and
# groupadd/useradd live there. Calling them by name fails with 127.
RUN /usr/sbin/groupadd --gid 10001 swarm \
 && /usr/sbin/useradd --uid 10001 --gid 10001 --create-home --home-dir /home/swarm --shell /bin/bash swarm \
 && mkdir -p /workspace \
 && chown -R swarm:swarm /workspace /home/swarm

USER swarm:swarm
WORKDIR /workspace

# The checkpoint restore extracts through `tarfile.data_filter` (PR #288), which
# CPython has from 3.11.4. PYTHON_IMAGE is pinned by digest, and a digest says
# nothing about the patch level, so the build asserts it rather than a resume
# discovering it. `agent_worker.checkpoint` refuses to import without the
# filter too; this line names the cause in the build log.
#
# And a worker that cannot import its own runners is broken in a way that must
# not reach production, so the build proves it can.
#
# ONE RUN, not two. A check-only RUN leaves a ~100-byte layer holding only the
# etc/ directory entry, and two that finish in the same second write the SAME
# layer: 2 of the 30 newest digests of this image repeated one here (read
# 2026-10-13), and so did every browser and indexer image built on them. GKE
# does not stream an image with a duplicate layer (docs/browser-startup-
# speed.md §2.4), and the release refuses one (scripts/image-sizes.sh
# --duplicate-layers; tests/unit/scripts/test_image_streaming.py).
RUN python -c "import sys, tarfile; \
assert sys.version_info >= (3, 11, 4), 'Python %s: the checkpoint restore needs 3.11.4 or later' % sys.version.split()[0]; \
assert hasattr(tarfile, 'data_filter'), 'tarfile.data_filter is missing'; \
print('python', sys.version.split()[0])" \
 && python -c "import agent_worker, agent_worker.runners.mock, agent_worker.lifecycle; print('agent-worker', agent_worker.__version__)"

# Every program the generic runner's catalogue names must exist in this image,
# or the first tenant to submit the documented use case discovers it instead of
# the build doing. `_resolve_program` looks each one up on `generic.BASE_PATH`,
# so this proves exactly what the runner will do at execution time.
RUN python -c "\
from agent_worker.runners.generic import GENERIC_COMMANDS, _resolve_program; \
print({c.name: _resolve_program(c.argv[0]) for c in GENERIC_COMMANDS.values()})" \
 && python -m pytest --version

# The toolbox smoke: every tool above, run AS THE AGENT USER, the way an agent
# will run it. A missing or unrunnable tool fails the build and is named; the
# count is printed so a list that collapsed to one word cannot read as a pass.
# HOME is a throwaway so `gcloud --version` leaves no config in the image.
# The PostgreSQL binaries must also say POSTGRES_MAJOR: the test-service
# catalogue offers 16 alone (docs/design/test-services.md §3.1), and a server
# of another major on the PATH is a broken tool, not a working one.
RUN tools="gh gcloud gke-gcloud-auth-plugin kubectl terraform checkov trivy shellcheck make docker postgres initdb pg_ctl pg_isready coderabbit" \
 && if [ "${INSTALL_TOFU}" = "1" ]; then tools="$tools tofu"; fi \
 && if [ "${INSTALL_TFLINT}" = "1" ]; then tools="$tools tflint"; fi \
 && HOME="$(mktemp -d)" && export HOME \
 && n=0 && missing="" \
 && for t in $tools; do \
      case "$t" in kubectl) set -- version --client ;; *) set -- --version ;; esac; \
      if command -v "$t" >/dev/null 2>&1 && out="$("$t" "$@" 2>&1)"; then \
        case "$t" in postgres|initdb|pg_ctl|pg_isready) \
          case "$out" in *"(PostgreSQL) ${POSTGRES_MAJOR}."*) ;; \
            *) missing="$missing $t (not PostgreSQL ${POSTGRES_MAJOR})"; continue ;; \
          esac ;; \
        esac; \
        n=$((n + 1)); echo "$t: $(printf '%s\n' "$out" | head -n 1)"; \
      else \
        missing="$missing $t"; \
      fi; \
    done \
 && rm -rf "$HOME" \
 && if [ -n "$missing" ]; then echo "toolbox smoke: missing or broken:$missing" >&2; exit 1; fi \
 && echo "toolbox smoke: $n tools ok"

# tini reaps orphaned grandchildren; the worker still terminates its own process
# group itself, so signals reach exactly one direct child.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]
