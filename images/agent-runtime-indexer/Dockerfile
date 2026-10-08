# syntax=docker/dockerfile:1.7
#
# agent-runtime-indexer -- agent-runtime-base plus the repository index's
# toolchain: the tree-sitter extractor (`swarm-repo-index`), the graph shard
# writer (`swarm-repo-graph`), and the language servers of its LSP pass
# (pyright, typescript-language-server and gopls; terraform-ls is specified
# but not installed, see the languageservers stage) with the Go toolchain
# gopls runs `go list` with. docs/repo-index.md §3.5, lanes RI3, RI9 and RI10;
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
# tarball by sha256, gopls and its raised golang.org/x modules through the
# module checksum database, the npm servers by the lockfile's sha512 integrity, the
# tree-sitter wheels with --require-hashes.
#
# PYTHON_IMAGE IS agent-runtime-base's PYTHON_IMAGE, digest and all: the
# languageservers stage only downloads and compiles, but it must not drift to
# another Debian from the image its output lands in. A test holds the two
# equal (tests/unit/scripts/test_indexer_image_split.py).
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
ARG BASE_IMAGE

# ---------------------------------------------------------------------------
# languageservers: the Go toolchain and gopls for the
# repository index's LSP pass (docs/repo-index.md §3.5, lane RI10). The two
# npm servers (pyright, typescript-language-server) are installed in the
# runtime stage below from images/agent-runtime-indexer/repo-index/lsp/package-lock.json.
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
# terraform-ls is NOT built (owner decision 2026-10-05): its source build
# answered 0 references in the LSP self-test (main a3621325), so it is out of
# the image until it resolves (lsp/servers.py DISABLED_SERVERS, lane IDX).
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
 && rm -rf /tmp/src /tmp/gopath /tmp/gocache \
 && /usr/local/bin/gopls version

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
COPY images/agent-runtime-indexer/repo-index/repo_graph_shards.py /opt/repo-index/
RUN chmod 0644 /opt/repo-index/repo_graph_shards.py \
 && printf '%s\n' '#!/bin/sh' \
      'exec /usr/local/bin/python3.11 -I /opt/repo-index/repo_graph_shards.py "$@"' \
      > /usr/local/bin/swarm-repo-graph \
 && chmod 0755 /usr/local/bin/swarm-repo-graph

# The language servers of the index's LSP pass (docs/repo-index.md §3.5,
# lane RI10): pyright, tsserver (through typescript-language-server) and
# gopls, each linked into /opt/repo-index/servers/bin, the ONLY
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
COPY images/agent-runtime-indexer/repo-index/lsp/ /opt/repo-index/lsp/
RUN cp /opt/repo-index/lsp/package.json /opt/repo-index/lsp/package-lock.json /opt/repo-index/servers/ \
 && cd /opt/repo-index/servers \
 && NPM_CONFIG_CACHE=/tmp/npm-cache npm ci --ignore-scripts --omit=optional --no-audit --no-fund \
 && rm -rf /tmp/npm-cache \
 && ln -s ../node_modules/.bin/pyright-langserver /opt/repo-index/servers/bin/pyright-langserver \
 && ln -s ../node_modules/.bin/typescript-language-server /opt/repo-index/servers/bin/typescript-language-server \
 && ln -s /usr/local/go/bin/go /opt/repo-index/servers/bin/go \
 && chmod -R a+rX,go-w /opt/repo-index/lsp /opt/repo-index/servers \
 && for s in pyright-langserver typescript-language-server gopls go; do \
      test -x "/opt/repo-index/servers/bin/$s" || { echo "language server missing: $s" >&2; exit 1; }; \
    done

USER swarm:swarm
WORKDIR /workspace

# The repository-index extractor must load all five grammars and parse one
# file with each, as the agent user, or the indexer step would find out
# instead of the build.
RUN swarm-repo-index --self-test
# And each installed language server must start, as the agent user, and
# resolve one call across two files -- not merely start.
RUN swarm-repo-index --lsp-self-test
# And the shard writer must shard, rewrite and sweep a graph in a scratch dir.
RUN swarm-repo-graph --self-test

# The base's, restated because a derived image that sets nothing inherits it
# but a reader of this file should not have to know that.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]
