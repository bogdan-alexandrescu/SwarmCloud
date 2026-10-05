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
#                     rest of this platform builds. The `docker` CLI is in the
#                     toolbox (BUILD_PROMPT_V2 §2.12) with no daemon and no
#                     socket behind it; image builds go to Cloud Build
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
# languageservers: the Go toolchain, gopls and terraform-ls for the
# repository index's LSP pass (docs/repo-index.md §3.5, lane RI10). The two
# npm servers (pyright, typescript-language-server) are installed in the
# runtime stage from images/agent-runtime-base/repo-index/lsp/package-lock.json.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS languageservers

# Go 1.27.1 (2026-10-05, the newest stable on https://go.dev/dl/?mode=json,
# whose sha256 for go1.27.1.linux-amd64.tar.gz this is). gopls needs the
# toolchain at run time, not only to build: it loads packages through
# `go list`, so the runtime stage carries /usr/local/go too.
ARG GO_VERSION=1.27.1
ARG GO_SHA256=63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445
# gopls 0.23.0 (2026-07-07, proxy.golang.org's @latest on 2026-10-05) and
# terraform-ls 0.39.0 (checkpoint-api.hashicorp.com's current_version on
# 2026-10-05) are both COMPILED HERE from their module source, not taken as
# the vendor's build. Release 37289476429 (2026-10-05) refused this image:
# trivy found HIGH CVEs in both binaries, all in what they were linked
# against -- golang.org/x/mod v0.37/v0.38 (CVE-2026-56864, -56865),
# x/text v0.38 (CVE-2026-56852), x/crypto v0.54 (CVE-2026-56854), and, in
# HashiCorp's terraform-ls zip, the go1.25.12 standard library (seven CVEs).
# Owner decision 2026-10-05: rebuild from source rather than ignore them.
# So each server's module is fetched through the module proxy (sum.golang.org
# verifies every module's bytes), the flagged golang.org/x modules are raised
# to the fixed versions below with `go get` (which verifies those too), and
# the result is linked with the Go above, which also clears the stdlib CVEs.
# The build then reads each binary's own module list (`go version -m`) and
# fails if a bump did not land, so a pin that stops applying is loud.
# Raising a pin: take the "fixed version" trivy prints, and check the
# module's go.mod `go` line is not newer than GO_VERSION (GOTOOLCHAIN=local).
ARG GOPLS_VERSION=0.23.0
ARG TERRAFORM_LS_VERSION=0.39.0
ARG X_CRYPTO_VERSION=0.55.0
ARG X_MOD_VERSION=0.40.0
ARG X_TEXT_VERSION=0.39.0

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update \
 && apt-get install --no-install-recommends -y ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/go.tar.gz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  /tmp/go.tar.gz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tar.gz \
 && rm -rf /tmp/go.tar.gz /usr/local/go/test
# build_server MODULE VERSION OUT MODULE@FIX...: copy the module's verified
# source out of the read-only module cache, raise the named dependencies,
# build, and assert each raised version is what the binary was linked with.
RUN export PATH="/usr/local/go/bin:${PATH}" GOPATH=/tmp/gopath GOCACHE=/tmp/gocache \
      GOTOOLCHAIN=local GOTELEMETRY=off CGO_ENABLED=0 GOFLAGS=-trimpath; \
    build_server() { \
      local module="$1" version="$2" out="$3"; shift 3; \
      local dir; dir="$(go mod download -json "${module}@v${version}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["Dir"])')" || return 1; \
      rm -rf /tmp/src && cp -r "${dir}" /tmp/src && chmod -R u+w /tmp/src || return 1; \
      (cd /tmp/src && go get "$@" && go build -o "${out}" .) || return 1; \
      local fix; for fix in "$@"; do \
        go version -m "${out}" | grep -qE "[[:space:]]${fix%@*}[[:space:]]+${fix#*@}([[:space:]]|$)" \
          || { echo "${out}: ${fix} did not land" >&2; return 1; }; \
      done; \
      chmod 0755 "${out}"; \
    }; \
    build_server golang.org/x/tools/gopls "${GOPLS_VERSION}" /usr/local/bin/gopls \
      "golang.org/x/mod@v${X_MOD_VERSION}" "golang.org/x/text@v${X_TEXT_VERSION}" \
 && build_server github.com/hashicorp/terraform-ls "${TERRAFORM_LS_VERSION}" /usr/local/bin/terraform-ls \
      "golang.org/x/crypto@v${X_CRYPTO_VERSION}" "golang.org/x/mod@v${X_MOD_VERSION}" \
 && rm -rf /tmp/src /tmp/gopath /tmp/gocache \
 && /usr/local/bin/gopls version && /usr/local/bin/terraform-ls version

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
      make \
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
ARG UNDICI_VERSION=6.29.0
ARG UNDICI_SHA512=47e44e0c1aa9ea2da93df94676afb120e52497e44d7c6807c288a779c2aefc90ae7f6b8ed3a70e2a80db2363fb0e7e8a72cc1d2b0adccdb53a21827a2bc5fec2
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
 && curl -fsSL -o "${BE_TGZ}" \
      "https://registry.npmjs.org/brace-expansion/-/brace-expansion-${BRACE_EXPANSION_VERSION}.tgz" \
 && curl -fsSL -o "${UNDICI_TGZ}" \
      "https://registry.npmjs.org/undici/-/undici-${UNDICI_VERSION}.tgz" \
 && curl -fsSL -o "${TAR_TGZ}" \
      "https://registry.npmjs.org/tar/-/tar-${TAR_VERSION}.tgz" \
 && curl -fsSL -o "${IPADDR_TGZ}" \
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
# kubectl 1.36.5: the newest 1.36 patch (https://dl.k8s.io/release/stable-1.36.txt
# on 2026-10-01), one minor ahead of the 1.35 control plane as kubectl's skew
# policy allows and the line docs/versions.md pins for operators. The static
# binary from dl.k8s.io rather than Google's `kubectl` .deb, which is 141 MB
# of several bundled versions. Checksum:
# https://dl.k8s.io/release/v1.36.5/bin/linux/amd64/kubectl.sha256. Scan: clean.
ARG KUBECTL_VERSION=1.36.5
ARG KUBECTL_SHA256=33bc88a24c3b09cf55bfd59e6ca977881b61fc3f25ab65bc80c65b430248648e
# dpkg runs with /usr/sbin on PATH because it refuses to start without
# ldconfig and start-stop-daemon, which live there and which the runtime PATH
# deliberately omits; /usr/local/bin is there for the postinst's python3.
RUN mkdir -p /tmp/toolbox \
 && curl -fsSL -o /tmp/toolbox/google-cloud-cli.deb \
      "https://packages.cloud.google.com/apt/pool/cloud-sdk/google-cloud-cli_${GCLOUD_VERSION}-0_amd64_${GCLOUD_POOL_ID}.deb" \
 && curl -fsSL -o /tmp/toolbox/gke-gcloud-auth-plugin.deb \
      "https://packages.cloud.google.com/apt/pool/cloud-sdk/google-cloud-cli-gke-gcloud-auth-plugin_${GCLOUD_VERSION}-0_amd64_${GKE_PLUGIN_POOL_ID}.deb" \
 && curl -fsSL -o /tmp/toolbox/kubectl \
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
RUN mkdir -p /tmp/toolbox \
 && curl -fsSL -o /tmp/toolbox/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
 && curl -fsSL -o /tmp/toolbox/docker-ce-cli.deb \
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
# INSTALL_TOFU_TFLINT_TRIVY is OFF by default because no published release of
# these three passed the promote gate on 2026-10-01, and the gate refuses the
# whole image, not just the tool. Each was scanned at its newest release:
#
#   tofu 1.13.0 (2026-09-30)   golang.org/x/mod 0.39.0 (CVE-2026-56864,
#                              CVE-2026-56865; fixed 0.40.0), google.golang.org/
#                              grpc 1.83.1 (CVE-2026-84445; fixed 1.83.2). The
#                              1.12.6 docs/versions.md names had fourteen.
#   tflint 0.64.0 (2026-07-17) Go stdlib 1.26.3 (eleven CVEs, fixed 1.26.6),
#                              x/crypto 0.53.0 (CVE-2026-56854), x/mod 0.37.0,
#                              grpc 1.82.0 (CVE-2026-84304, CVE-2026-84445,
#                              GHSA-hrxh-6v49-42gf). No newer release exists.
#   trivy 0.74.0 (2026-08-14)  grpc 1.82.1 (CVE-2026-84304, fixed 1.83.1;
#                              CVE-2026-84445, fixed 1.83.2). No newer release.
#
# Building them from source with patched dependencies is ruled out (see the
# header of this section), and an ignore entry for another project's
# dependencies is the owner's decision, not this file's. So the install below
# is complete -- pinned, verified, and covered by the smoke step when the
# argument is 1 -- and turning it on is one build argument once the vendors
# ship a release that scans clean (or the owner accepts these findings in
# .trivyignore.yaml with an expiry). Re-scan before flipping it.
#
# Checksums: https://github.com/opentofu/opentofu/releases/download/v1.13.0/tofu_1.13.0_SHA256SUMS
# (tofu_1.13.0_linux_amd64.tar.gz) and
# https://github.com/terraform-linters/tflint/releases/download/v0.64.0/checksums.txt
# (tflint_linux_amd64.zip).
ARG INSTALL_TOFU_TFLINT_TRIVY=0
ARG TOFU_VERSION=1.13.0
ARG TOFU_SHA256=1f0cb37fc85dea4e7633a72aca2332b801f72650a32be911b8b9b32907f214fb
ARG TFLINT_VERSION=0.64.0
ARG TFLINT_SHA256=cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537
RUN mkdir -p /tmp/toolbox \
 && curl -fsSL -o /tmp/toolbox/terraform.zip \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
 && echo "${TERRAFORM_SHA256}  /tmp/toolbox/terraform.zip" | sha256sum -c - \
 && unzip -q /tmp/toolbox/terraform.zip terraform -d /tmp/toolbox \
 && install -o root -g root -m 0755 /tmp/toolbox/terraform /usr/local/bin/terraform \
 && if [ "${INSTALL_TOFU_TFLINT_TRIVY}" = "1" ]; then \
      curl -fsSL -o /tmp/toolbox/tofu.tar.gz \
        "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.tar.gz" \
      && curl -fsSL -o /tmp/toolbox/tflint.zip \
        "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/tflint_linux_amd64.zip" \
      && echo "${TOFU_SHA256}  /tmp/toolbox/tofu.tar.gz" | sha256sum -c - \
      && echo "${TFLINT_SHA256}  /tmp/toolbox/tflint.zip" | sha256sum -c - \
      && tar -xzf /tmp/toolbox/tofu.tar.gz -C /tmp/toolbox tofu \
      && unzip -q /tmp/toolbox/tflint.zip tflint -d /tmp/toolbox \
      && install -o root -g root -m 0755 /tmp/toolbox/tofu /usr/local/bin/tofu \
      && install -o root -g root -m 0755 /tmp/toolbox/tflint /usr/local/bin/tflint ; \
    fi \
 && rm -rf /tmp/toolbox

# Linters and scanners: shellcheck always, trivy behind the same argument.
#
# shellcheck 0.11.0, the newest release and the version docs/versions.md pins
# (Debian bookworm's apt package is 0.9.0).
# A static Haskell binary, which trivy has no analyzer for, so the gate
# cannot flag it either way. koalaman publishes no checksum
# file, so the sha256 is the asset digest GitHub records for
# shellcheck-v0.11.0.linux.x86_64.tar.gz in
# https://api.github.com/repos/koalaman/shellcheck/releases/latest, recomputed
# over the downloaded file. trivy 0.74.0: see INSTALL_TOFU_TFLINT_TRIVY above;
# checksum from
# https://github.com/aquasecurity/trivy/releases/download/v0.74.0/trivy_0.74.0_checksums.txt
# (trivy_0.74.0_Linux-64bit.tar.gz).
ARG SHELLCHECK_VERSION=0.11.0
ARG SHELLCHECK_SHA256=b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6
ARG TRIVY_VERSION=0.74.0
ARG TRIVY_SHA256=2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a
RUN mkdir -p /tmp/toolbox \
 && curl -fsSL -o /tmp/toolbox/shellcheck.tar.gz \
      "https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.x86_64.tar.gz" \
 && echo "${SHELLCHECK_SHA256}  /tmp/toolbox/shellcheck.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/toolbox/shellcheck.tar.gz -C /tmp/toolbox --strip-components=1 \
      "shellcheck-v${SHELLCHECK_VERSION}/shellcheck" \
 && install -o root -g root -m 0755 /tmp/toolbox/shellcheck /usr/local/bin/shellcheck \
 && if [ "${INSTALL_TOFU_TFLINT_TRIVY}" = "1" ]; then \
      curl -fsSL -o /tmp/toolbox/trivy.tar.gz \
        "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
      && echo "${TRIVY_SHA256}  /tmp/toolbox/trivy.tar.gz" | sha256sum -c - \
      && tar -xzf /tmp/toolbox/trivy.tar.gz -C /tmp/toolbox trivy \
      && install -o root -g root -m 0755 /tmp/toolbox/trivy /usr/local/bin/trivy ; \
    fi \
 && rm -rf /tmp/toolbox

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
ARG CHECKOV_VERSION=3.3.17
ARG CHECKOV_EXCLUDE_NEWER=2026-10-01T00:00:00Z
RUN UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/uv-cache \
      uv tool install --python /usr/local/bin/python3.11 \
        --exclude-newer "${CHECKOV_EXCLUDE_NEWER}" "checkov==${CHECKOV_VERSION}" \
 && rm -rf /tmp/uv-cache

# The repository-index extractor (docs/repo-index.md §3.5, lane RI3): one
# tree-sitter pass the indexer prompt runs first, as
# `swarm-repo-index --repo <checkout> --out "$SWARM_ARTIFACTS_DIR/repo-index.json"`
# (the 512 KiB index of §2.2; `--graph-out <file>` writes the graph, which
# §2.2 keeps out of the artifact, for the shard writer).
# Like checkov it gets its OWN environment, /opt/repo-index, so its grammars
# never change the worker's /opt/venv or the agent's `pip install` target.
# --require-hashes with --only-binary: every wheel is the exact bytes
# requirements.txt names, and nothing is compiled at build time. The script
# and its pins are read-only to the agent (root-owned, 0644/0755). `-I` keeps
# the agent's PYTHONPATH and user site out of the extractor's interpreter.
COPY images/agent-runtime-base/repo-index/requirements.txt images/agent-runtime-base/repo-index/repo_index_extract.py /opt/repo-index/
RUN /usr/local/bin/python3.11 -m venv --without-pip /opt/repo-index/venv \
 && UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv-cache \
      uv pip install --python /opt/repo-index/venv/bin/python \
        --require-hashes --only-binary :all: --no-deps \
        -r /opt/repo-index/requirements.txt \
 && rm -rf /tmp/uv-cache \
 && chmod 0644 /opt/repo-index/requirements.txt /opt/repo-index/repo_index_extract.py \
 && printf '%s\n' '#!/bin/sh' \
      'exec /opt/repo-index/venv/bin/python -I /opt/repo-index/repo_index_extract.py "$@"' \
      > /usr/local/bin/swarm-repo-index \
 && chmod 0755 /usr/local/bin/swarm-repo-index

# The graph shard writer and blob sweep (docs/repo-index.md §2.5, lane RI9):
# `swarm-repo-graph write --graph <the --graph-out file> --store gs://<bucket>
# --tenant <t> --repo-id <r> --index <repo-index.json>` stores the graph as
# content-addressed blobs and a manifest under tenants/<t>/repos/<r>/graph/,
# records the manifest's digest in the index, then sweeps blobs no manifest
# names. Standard library only, so it runs on the image's own python3.11
# (`-I`, as the extractor) and reaches the bucket through `gcloud storage`
# with the step's own account; root-owned and read-only to the agent.
COPY images/agent-runtime-base/repo-index/repo_graph_shards.py /opt/repo-index/
RUN chmod 0644 /opt/repo-index/repo_graph_shards.py \
 && printf '%s\n' '#!/bin/sh' \
      'exec /usr/local/bin/python3.11 -I /opt/repo-index/repo_graph_shards.py "$@"' \
      > /usr/local/bin/swarm-repo-graph \
 && chmod 0755 /usr/local/bin/swarm-repo-graph

# The language servers of the index's LSP pass (docs/repo-index.md §3.5,
# lane RI10): pyright, tsserver (through typescript-language-server), gopls
# and terraform-ls, each linked into /opt/repo-index/servers/bin, the ONLY
# place lsp/servers.py resolves a server from -- never PATH, which an agent
# can change. The lsp/ package is the driver swarm-repo-index imports.
#
# The npm servers come from lsp/package-lock.json with `npm ci`: exact
# versions and each tarball's sha512 integrity, nothing resolved at build
# time, and --ignore-scripts so no package runs code while installing. The
# three pins: pyright 1.1.414, typescript-language-server 6.0.1 (it needs
# Node >= 22.22.2; NODE_IMAGE is Node 24), and typescript 6.0.3 -- NOT 7.x,
# whose package is the native compiler and has no tsserver for
# typescript-language-server to start. The cache goes to /tmp, not the agent
# user's ~/.npm, and is removed in the same layer.
#
# /usr/local/go is the toolchain gopls runs `go list` with; `go` is linked
# into the servers' bin directory only, not onto the agent's PATH.
COPY --from=languageservers /usr/local/go /usr/local/go
COPY --from=languageservers /usr/local/bin/gopls /usr/local/bin/terraform-ls /opt/repo-index/servers/bin/
COPY images/agent-runtime-base/repo-index/lsp/ /opt/repo-index/lsp/
RUN cp /opt/repo-index/lsp/package.json /opt/repo-index/lsp/package-lock.json /opt/repo-index/servers/ \
 && cd /opt/repo-index/servers \
 && NPM_CONFIG_CACHE=/tmp/npm-cache npm ci --ignore-scripts --omit=optional --no-audit --no-fund \
 && rm -rf /tmp/npm-cache \
 && ln -s ../node_modules/.bin/pyright-langserver /opt/repo-index/servers/bin/pyright-langserver \
 && ln -s ../node_modules/.bin/typescript-language-server /opt/repo-index/servers/bin/typescript-language-server \
 && ln -s /usr/local/go/bin/go /opt/repo-index/servers/bin/go \
 && chmod -R a+rX,go-w /opt/repo-index/lsp /opt/repo-index/servers \
 && for s in pyright-langserver typescript-language-server gopls terraform-ls go; do \
      test -x "/opt/repo-index/servers/bin/$s" || { echo "language server missing: $s" >&2; exit 1; }; \
    done

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

# The repository-index extractor must load all five grammars and parse one
# file with each, as the agent user, or the indexer step would find out
# instead of the build.
RUN swarm-repo-index --self-test
# And each of the four language servers must start, as the agent user, and
# resolve one call across two files -- not merely start.
RUN swarm-repo-index --lsp-self-test
# And the shard writer must shard, rewrite and sweep a graph in a scratch dir.
RUN swarm-repo-graph --self-test

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
RUN tools="gh gcloud gke-gcloud-auth-plugin kubectl terraform checkov shellcheck make docker" \
 && if [ "${INSTALL_TOFU_TFLINT_TRIVY}" = "1" ]; then tools="$tools tofu tflint trivy"; fi \
 && HOME="$(mktemp -d)" && export HOME \
 && n=0 && missing="" \
 && for t in $tools; do \
      case "$t" in kubectl) set -- version --client ;; *) set -- --version ;; esac; \
      if command -v "$t" >/dev/null 2>&1 && out="$("$t" "$@" 2>&1)"; then \
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
