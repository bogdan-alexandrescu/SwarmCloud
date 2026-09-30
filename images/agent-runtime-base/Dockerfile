# syntax=docker/dockerfile:1.7
#
# agent-runtime-base -- the image every non-browser runner profile runs in.
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
#                     rest of this platform builds
#   tini as PID 1     an agent's helpers get orphaned when the worker kills a
#                     process group; tini reaps them instead of leaving zombies
#
# Digests resolved from the registries on 2026-09-15.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553
# 0.12.15, not 0.11.7: the older uv statically links quinn-proto 0.11.14
# and rustls-webpki 0.103.10, both with fixed HIGH advisories
# (GHSA-4w2j-m93h-cj5j, GHSA-82j2-j2ch-gfr8) that failed the promote scan
# on /usr/local/bin/uv and /usr/local/bin/uvx. Nothing else in the image
# carries a Rust binary, which is why these two were the last to appear.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3

# ---------------------------------------------------------------------------
# uv and Node arrive as pinned images rather than as curl | sh
# ---------------------------------------------------------------------------
FROM ${UV_IMAGE} AS uvbin
FROM ${NODE_IMAGE} AS nodebin

# ---------------------------------------------------------------------------
# builder: compile wheels, produce /opt/venv
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y \
      build-essential \
      ca-certificates \
      git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uvbin /uv /usr/local/bin/uv

WORKDIR /src
# Only what the image needs to build: the frozen contract, the credential
# patterns the worker's per-commit secret scan matches (#259), and the worker.
COPY apps/common/ /src/apps/common/
COPY apps/redaction/ /src/apps/redaction/
COPY apps/agent-worker/ /src/apps/agent-worker/

# Wheels first, then install from the local wheelhouse. Resolving
# `swarm-common` from a wheel we just built -- rather than from an index that
# has never heard of it -- is what makes this build reproducible offline.
RUN uv build --wheel --out-dir /wheels /src/apps/common \
 && uv build --wheel --out-dir /wheels /src/apps/redaction \
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
      "setuptools>=${SETUPTOOLS_VERSION}" "wheel>=${WHEEL_VERSION}" \
 && uv pip install --python /opt/venv/bin/python --find-links /wheels swarm-agent-worker \
 && uv pip install --python /opt/venv/bin/python "pytest==${PYTEST_VERSION}"

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

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
RUN apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y \
      bash \
      build-essential \
      ca-certificates \
      curl \
      fd-find \
      git \
      jq \
      less \
      openssh-client \
      procps \
      ripgrep \
      tini \
      unzip \
      wget \
 && ln -sf "$(command -v fdfind)" /usr/local/bin/fd \
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
# pack --dry-run`, which walks a `files` glob through npm's own bundled
# minimatch/glob -- the actual codepath a mismatched API would break -- so a
# bad swap fails the build rather than shipping quietly.
ARG BRACE_EXPANSION_VERSION=5.0.12
ARG BRACE_EXPANSION_SHA512=628bd0debce168b308ac38cd0cc90d4b4d6d79af77aa11211b9c72f1febe47497e770dca8bee6c0a822823de368ae2d94c75a881692d830543854d3d88d12299
RUN BE_DIR=/usr/local/lib/node_modules/npm/node_modules/brace-expansion \
 && BE_TGZ="/tmp/brace-expansion-${BRACE_EXPANSION_VERSION}.tgz" \
 && curl -fsSL -o "${BE_TGZ}" \
      "https://registry.npmjs.org/brace-expansion/-/brace-expansion-${BRACE_EXPANSION_VERSION}.tgz" \
 && echo "${BRACE_EXPANSION_SHA512}  ${BE_TGZ}" | sha512sum -c - \
 && rm -rf "${BE_DIR}" \
 && mkdir -p "${BE_DIR}" \
 && tar -xzf "${BE_TGZ}" -C "${BE_DIR}" --strip-components=1 \
 && rm -f "${BE_TGZ}" \
 && node -e "\
const pkg = require('${BE_DIR}/package.json'); \
if (pkg.version !== '${BRACE_EXPANSION_VERSION}') { throw new Error('brace-expansion landed ' + pkg.version + ', expected ${BRACE_EXPANSION_VERSION}'); } \
const { expand } = require('${BE_DIR}'); \
if (typeof expand !== 'function') { throw new Error('brace-expansion has no expand() export -- minimatch calls brace_expansion_1.expand()'); } \
const out = expand('a{{1,2},{3,4}}b'); \
const want = ['a1b','a2b','a3b','a4b']; \
if (JSON.stringify(out) !== JSON.stringify(want)) { throw new Error('brace-expansion regressed: ' + JSON.stringify(out)); } \
console.log('brace-expansion', pkg.version, 'ok');" \
 && export HOME=/tmp \
 && rm -rf /tmp/npm-brace-expansion-smoke \
 && mkdir -p /tmp/npm-brace-expansion-smoke \
 && cd /tmp/npm-brace-expansion-smoke \
 && npm --version \
 && npm init -y >/dev/null \
 && npm pack --dry-run >/dev/null \
 && cd / \
 && rm -rf /tmp/npm-brace-expansion-smoke \
 && echo "npm pack (minimatch/glob brace-expansion path) ok"

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

# The coding-agent CLIs the claude-code and codex runner profiles start. Both
# are optional at build time so the base image can be rebuilt in an environment
# with no npm registry access; the runners fail with a clear message if absent.
RUN if [ "${INSTALL_AGENT_CLIS}" = "1" ]; then \
      mkdir -p "${NPM_CONFIG_PREFIX}" \
      && npm install -g --no-audit --no-fund \
           "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
           "@openai/codex@${CODEX_VERSION}" \
      && npm cache clean --force ; \
    fi

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
RUN python -c "import sys, tarfile; \
assert sys.version_info >= (3, 11, 4), 'Python %s: the checkpoint restore needs 3.11.4 or later' % sys.version.split()[0]; \
assert hasattr(tarfile, 'data_filter'), 'tarfile.data_filter is missing'; \
print('python', sys.version.split()[0])"

# A worker that cannot import its own runners is broken in a way that must not
# reach production, so the build proves it can.
RUN python -c "import agent_worker, agent_worker.runners.mock, agent_worker.lifecycle; print('agent-worker', agent_worker.__version__)"

# Every program the generic runner's catalogue names must exist in this image,
# or the first tenant to submit the documented use case discovers it instead of
# the build doing. `_resolve_program` looks each one up on `generic.BASE_PATH`,
# so this proves exactly what the runner will do at execution time.
RUN python -c "\
from agent_worker.runners.generic import GENERIC_COMMANDS, _resolve_program; \
print({c.name: _resolve_program(c.argv[0]) for c in GENERIC_COMMANDS.values()})" \
 && python -m pytest --version

# tini reaps orphaned grandchildren; the worker still terminates its own process
# group itself, so signals reach exactly one direct child.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]
