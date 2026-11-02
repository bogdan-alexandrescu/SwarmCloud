# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# agent-runtime-indexer -- agent-runtime-base plus the repository index's
# toolchain: the tree-sitter extractor (`swarm-repo-index`), the graph shard
# writer (`swarm-repo-graph`), and the language servers of its LSP pass
# (pyright, typescript-language-server, gopls and terraform-ls) with the Go
# toolchain gopls runs `go list` with. docs/repo-index.md §3.5, lanes RI3, RI9 and RI10;
# docs/worker-images.md for why it is its own image.
#
# WHY NOT IN agent-runtime-base (#625). Every claude-code and codex start
# pulls the base, and this toolchain is +188 MB compressed that only an index
# run uses (726 -> 916 MB in the operator's registry bisect of 2026-10-05,
# scripts/image-sizes.sh). Only the `indexer` runner profile runs this image
# (contract request 48, accepted by the owner 2026-10-05).
#
# Build the base first, then:
#
#     docker build -f images/agent-runtime-indexer/Dockerfile \
#         --build-arg BASE_IMAGE=<registry>/agent-runtime-base@sha256:<digest> \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/agent-runtime-indexer:$TAG .
#
# BASE_IMAGE IS TAKEN BY DIGEST AND ONLY BY DIGEST. images/agent-runtime-indexer/
# cloudbuild.yaml pulls agent-runtime-base:<the same tag>, reads the digest
# back and passes it. There is no default -- a build handed no base fails at
# FROM -- and the first step below refuses a base named by tag, so every FROM
# in this file is pinned: BASE_IMAGE by the build, PYTHON_IMAGE here.
#
# Everything agent-runtime-base guarantees holds here because this IS that
# image plus three directories: non-root (uid 10001; root only while
# installing, and USER swarm:swarm before anything runs), no docker socket,
# tini as PID 1, the same ENTRYPOINT. Every download is verified: the Go
# tarball by sha256, gopls, terraform-ls and their raised golang.org/x
# modules through the module checksum database (and terraform-ls's module
# zip by a sha256 pinned here too), the npm servers by the lockfile's sha512 integrity, the
# tree-sitter wheels with --require-hashes.
#
# PYTHON_IMAGE IS agent-runtime-base's PYTHON_IMAGE, digest and all: the
# languageservers stage only downloads and compiles, but it must not drift to
# another Debian from the image its output lands in. A test holds the two
# equal (tests/unit/scripts/test_indexer_image_split.py).
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
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
ARG BASE_IMAGE

# ---------------------------------------------------------------------------
# languageservers: the Go toolchain, gopls and terraform-ls for the
# repository index's LSP pass (docs/repo-index.md §3.5, lane RI10). The two
# npm servers (pyright, typescript-language-server) are installed in the
# runtime stage below from images/agent-runtime-indexer/repo-index/lsp/package-lock.json.
# ---------------------------------------------------------------------------
FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS languageservers

# Go 1.27.3 (the newest stable on https://go.dev/dl/?mode=json on 2026-10-31,
# whose sha256 for go1.27.3.linux-amd64.tar.gz this is). Raised from 1.27.1,
# whose standard library carries CVE-2026-78667, CVE-2026-78669 and
# CVE-2026-97031 (fixed in 1.27.2) and CVE-2026-111943 (fixed in 1.27.3), in
# the toolchain this image ships and in the gopls and terraform-ls it links. gopls needs the
# toolchain at run time, not only to build: it loads packages through
# `go list`, so the runtime stage carries /usr/local/go too.
ARG GO_VERSION=1.27.3
ARG GO_SHA256=65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40
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
# terraform-ls was out of the image from 2026-10-05 (that day's source build
# answered 0 references in the LSP self-test, main a3621325) until 2026-10-20
# (lane INDEX-LANGS, part of #1693, owner decision): built as below it answers
# references across a module's files, and the self-test requires an HCL edge.
# Raising a pin: take the "fixed version" trivy prints, then read the pinned
# modules' OWN requirements on the proxy (curl proxy.golang.org/<mod>/@v/<v>.mod):
# the x/* modules require each other, so a pin below what another pin needs
# fails `go get` ("x/mod@v0.40.0 indirectly requires x/text@v0.41.0", main
# 3ed3305, 2026-10-05). Check every `go` line is not newer than GO_VERSION.
#
# gopls is v0.24.0-pre.1 (2026-09-30), not v0.23.0: x/mod >= 0.40 requires
# x/tools 0.49, and gopls is compiled against x/tools internals, so forcing
# 0.23.0 (x/tools 0.47.1 snapshot) onto a newer x/tools risks not compiling.
# 0.24.0-pre.1 already requires x/mod 0.41.0, x/text 0.42.0 and x/tools
# 0.50.1-pre, so its two pins below are no-ops kept for the linked-version
# check. Move to v0.24.0 when it is released. terraform-ls takes x/tools
# 0.48 -> 0.49 through the raised x/mod and x/text; it uses x/tools' public API.
ARG GOPLS_VERSION=0.24.0-pre.1
ARG X_MOD_VERSION=0.41.0
ARG X_TEXT_VERSION=0.42.0
# terraform-ls 0.39.0 (checkpoint-api.hashicorp.com's current_version and
# proxy.golang.org's @latest on 2026-10-20). Its go.mod requires x/crypto
# v0.54.0 and x/net v0.57.0; osv.dev (read 2026-10-20) has x/crypto fixed at
# 0.55.0 (CVE-2026-56854) and 0.56.0 (CVE-2026-78662, -56855), and x/net at
# 0.60.0 (CVE-2026-78659, -78660, -78663, -78669, -97032). x/net 0.60.0
# requires x/crypto 0.57.0 and x/text 0.42.0, so those are the pins: a higher
# x/net (0.61.0) would pull x/text 0.43.0 past X_TEXT_VERSION and fail the
# linked-version check. Every `go` line is 1.26.0 or older, under GO_VERSION.
# TERRAFORM_LS_SHA256 is the sha256 of the module's zip as proxy.golang.org
# serves it (`curl -sL https://proxy.golang.org/github.com/hashicorp/
# terraform-ls/@v/v<version>.zip | sha256sum`): the module checksum database
# already verifies those bytes, and this pin makes a raised version a reviewed
# change to this file, as GO_SHA256 does for the toolchain.
ARG TERRAFORM_LS_VERSION=0.39.0
ARG TERRAFORM_LS_SHA256=7ac775b112c025aa426f4ae107b7632e7553270e76f136a7b09cfeff4108dda5
ARG X_CRYPTO_VERSION=0.57.0
ARG X_NET_VERSION=0.60.0

# EVERY DOWNLOAD HERE RETRIES (2026-10-14, PR 1281's flake table): `curl -fsSL
# --retry 5 --retry-all-errors --retry-connrefused`, 1+2+4+8+16 s of backoff at
# most. --retry-all-errors is what makes an HTTP error (curl's exit 22, which
# failed one main build of agent-runtime-base in run 38245452463) retry at all. A
# file that is really gone still fails, ~31 s later, and every download is
# still checked against its pinned checksum after it lands.
# The Go module fetches retry too: `go mod download` and `go get` read
# proxy.golang.org, whose stream reset (INTERNAL_ERROR) failed one pull
# request's build of this image (run 38518513292). build_server runs up to
# three times, 15 and 30 s apart, from a clean source copy each time; a real
# failure -- a pin that did not land -- fails all three and is reported as
# itself.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update \
 && apt-get install --no-install-recommends -y ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/go.tar.gz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  /tmp/go.tar.gz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tar.gz \
 && rm -rf /tmp/go.tar.gz /usr/local/go/test
# check_module_zip MODULE VERSION SHA256: download the module (verified by the
# checksum database) and check its zip against the sha256 pinned above.
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
    check_module_zip() { \
      local zip; zip="$(go mod download -json "$1@v$2" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["Zip"])')" || return 1; \
      echo "$3  ${zip}" | sha256sum -c - || return 1; \
    }; \
    three_tries() { \
      local attempt; for attempt in 1 2 3; do \
        "$@" && return 0; \
        [ "${attempt}" -lt 3 ] || return 1; \
        echo "$1 $2: try ${attempt} of 3 failed; retrying in $((15 * attempt)) s" >&2; \
        sleep $((15 * attempt)); \
      done; \
    }; \
    three_tries build_server golang.org/x/tools/gopls "${GOPLS_VERSION}" /usr/local/bin/gopls \
      "golang.org/x/mod@v${X_MOD_VERSION}" "golang.org/x/text@v${X_TEXT_VERSION}" \
 && three_tries check_module_zip github.com/hashicorp/terraform-ls "${TERRAFORM_LS_VERSION}" "${TERRAFORM_LS_SHA256}" \
 && three_tries build_server github.com/hashicorp/terraform-ls "${TERRAFORM_LS_VERSION}" /usr/local/bin/terraform-ls \
      "golang.org/x/mod@v${X_MOD_VERSION}" "golang.org/x/text@v${X_TEXT_VERSION}" \
      "golang.org/x/crypto@v${X_CRYPTO_VERSION}" "golang.org/x/net@v${X_NET_VERSION}" \
 && rm -rf /tmp/src /tmp/gopath /tmp/gocache \
 && /usr/local/bin/gopls version \
 && /usr/local/bin/terraform-ls version

# ---------------------------------------------------------------------------
# runtime: agent-runtime-base, by digest, plus the toolchain
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

# Re-declared so the guard below can read it: an ARG before the first FROM is
# in scope for FROM lines only.
ARG BASE_IMAGE
# The commit this image was built from. cloudbuild.yaml passes it, and a
# --build-arg nothing declares is dropped with only a warning, so it is
# declared and recorded as the OCI revision label (#625).
ARG GIT_SHA=""
LABEL org.opencontainers.image.revision="${GIT_SHA}"

USER root

RUN case "${BASE_IMAGE}" in \
      *@sha256:*) echo "agent-runtime-indexer on ${BASE_IMAGE}" ;; \
      *) echo "BASE_IMAGE must name agent-runtime-base by digest (<ref>@sha256:<64 hex>), not '${BASE_IMAGE}'" >&2; exit 1 ;; \
    esac

# The repository-index extractor (docs/repo-index.md §3.5, lane RI3): one
# tree-sitter pass the indexer prompt runs first, as
# `swarm-repo-index --repo <checkout> --out "$SWARM_ARTIFACTS_DIR/repo-index.json"`
# (the 512 KiB index of §2.2; `--graph-out <file>` writes the graph, which
# §2.2 keeps out of the artifact, for the shard writer).
# Like the base image's checkov it gets its OWN environment, /opt/repo-index, so its grammars
# never change the worker's /opt/venv or the agent's `pip install` target.
# --require-hashes with --only-binary: every wheel is the exact bytes
# requirements.txt names, and nothing is compiled at build time. The script
# and its pins are read-only to the agent (root-owned, 0644/0755). `-I` keeps
# the agent's PYTHONPATH and user site out of the extractor's interpreter.
COPY images/agent-runtime-indexer/repo-index/requirements.txt images/agent-runtime-indexer/repo-index/repo_index_extract.py /opt/repo-index/
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
# repo_index_history.py beside it mines the clone for the history and rules
# layers (lane CTXQ-H); the writer loads it by path, and its self-test fails
# the build when it is missing.
COPY images/agent-runtime-indexer/repo-index/repo_graph_shards.py images/agent-runtime-indexer/repo-index/repo_index_history.py /opt/repo-index/
RUN chmod 0644 /opt/repo-index/repo_graph_shards.py /opt/repo-index/repo_index_history.py \
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
COPY --from=languageservers /usr/local/bin/gopls /opt/repo-index/servers/bin/
COPY --from=languageservers /usr/local/bin/terraform-ls /opt/repo-index/servers/bin/
COPY images/agent-runtime-indexer/repo-index/lsp/ /opt/repo-index/lsp/
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

USER swarm:swarm
# NO `WORKDIR /workspace` HERE, and ONE RUN for the three self-tests below.
# GKE does not stream an image with a duplicate layer (docs/browser-startup-
# speed.md §2.4), and both wrote one: restating the base's WORKDIR wrote
# BuildKit's empty layer a second time, and each check-only RUN leaves a
# ~100-byte layer holding only the etc/ and tmp/ directory entries -- the SAME
# layer when two of them finish in the same second, as 7 of the 30 newest
# digests of this image did (read 2026-10-13). The release refuses such an
# image (scripts/image-sizes.sh --duplicate-layers);
# tests/unit/scripts/test_image_streaming.py holds this file to it.
#
# The repository-index extractor must load all five grammars and parse one
# file with each, as the agent user, or the indexer step would find out
# instead of the build. And each installed language server must start, as the
# agent user, and resolve one call across two files -- not merely start. And
# the shard writer must shard, rewrite and sweep a graph in a scratch dir.
RUN swarm-repo-index --self-test \
 && swarm-repo-index --lsp-self-test \
 && swarm-repo-graph --self-test

# The base's, restated because a derived image that sets nothing inherits it
# but a reader of this file should not have to know that.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]
