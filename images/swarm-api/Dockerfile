# syntax=docker/dockerfile:1.7
#
# swarm-api -- the FastAPI service that authenticates callers, resolves tenants and writes tasks.
#
# This file exists because `scripts/build-images.sh` lists `swarm-api` in
# ALL_TARGETS and `find_recipe()` looks for exactly one of
# `images/<target>/cloudbuild.yaml`, `images/<target>/Dockerfile`,
# `apps/<target>/Dockerfile` or `docker/<target>/Dockerfile`. With none of them
# present the script WARNS and continues, so `make build` reported success
# having built nothing, while `terraform/infra/main.tf` deploys
# `swarm-api:${var.image_tag}` and the security workflow scans it. A
# missing recipe that does not fail the build is how an audited application ends
# up undeployable in silence.
#
# Build from the REPOSITORY ROOT -- the frozen contract package and the
# reconciler are both part of the image, and `apps/swarm-api/pyproject.toml`
# resolves `swarm-common` through `{ path = "../common" }`, so the two must keep
# their relative positions inside the build context:
#
#     docker build -f images/swarm-api/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-api:$TAG .
#
# Same properties as agent-runtime-base, for the same reasons: every FROM
# carries a digest, compilers stay in the builder, and the runtime runs as a
# non-root uid. Generated from the swarm-reconciler image so all four control-plane
# services share one hardening posture; change them together.
#
# Digest resolved from the registry on 2026-09-15; the same base the worker
# image pins, so both move together.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
# 0.12.15, not 0.11.7: the older uv statically links quinn-proto 0.11.14
# and rustls-webpki 0.103.10, both with fixed HIGH advisories
# (GHSA-4w2j-m93h-cj5j, GHSA-82j2-j2ch-gfr8) that failed the promote scan
# on /usr/local/bin/uv and /usr/local/bin/uvx. Nothing else in the image
# carries a Rust binary, which is why these two were the last to appear.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3

FROM ${UV_IMAGE} AS uvbin

# ---------------------------------------------------------------------------
# builder: wheels for the frozen contract and the reconciler, then /opt/venv
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uvbin /uv /usr/local/bin/uv

WORKDIR /src
# The root project and its lock. `uv export` below reads this service's
# third-party dependency set, at the versions and hashes uv.lock records, from
# them (#663). With --frozen it reads no other app's directory, so the builder
# still copies only the packages this image ships.
COPY pyproject.toml uv.lock /src/
COPY apps/common/ /src/apps/common/
# The credential patterns swarm_api.redaction re-exports (#259).
COPY apps/redaction/ /src/apps/redaction/
# The workflow state derivation the API and the reconciler share (#616).
COPY apps/workflow-rollup/ /src/apps/workflow-rollup/
COPY apps/swarm-api/ /src/apps/swarm-api/

# Build our own packages into a local wheelhouse. No index has ever heard of
# them, and the install below never asks one: it reads them from /wheels with
# `--no-index` (#663).
RUN uv build --wheel --out-dir /wheels /src/apps/common \
 && uv build --wheel --out-dir /wheels /src/apps/redaction \
 && uv build --wheel --out-dir /wheels /src/apps/workflow-rollup \
 && uv build --wheel --out-dir /wheels /src/apps/swarm-api

# `--without-pip`: the stdlib venv otherwise seeds pip, setuptools and wheel,
# and BUILD tooling is what those are. None of it is imported at runtime, and
# all three were flagged by the promote scan (jaraco.context path traversal,
# vendored inside setuptools; wheel privilege escalation). uv installs into the
# venv from outside it, so nothing here needs a pip inside it.
RUN python -m venv --without-pip /opt/venv

# Third-party dependencies, exported from uv.lock for this service alone. The
# root project depends on every app, so the export prunes the internal packages
# swarm-api does not depend on (`swarm-agent-worker`, `swarm-mcp`,
# `swarm-quota-broker`, `swarm-reconciler`, `swarm-scheduler`), and the
# third-party packages only they need go with them. `--no-emit-local` leaves
# our own packages out of the file: they come from /wheels, below. The export
# carries a hash for every file; `--no-hashes` must never be added here.
RUN uv export --frozen --no-dev --no-emit-local --no-editable \
      --prune swarm-agent-worker --prune swarm-mcp \
      --prune swarm-quota-broker --prune swarm-reconciler \
      --prune swarm-scheduler \
      > /tmp/requirements.txt

# Installed at exactly the versions and hashes in uv.lock, with nothing
# resolved at build time (#663). This used to be one `uv pip install
# --find-links /wheels swarm-api` that resolved every third-party package
# unpinned against PyPI. `--require-hashes` refuses any file whose hash the lock
# does not record, so an index can fail this step but never substitute a
# package; `--no-deps` because the export is already the complete closure.
RUN uv pip install --python /opt/venv/bin/python --require-hashes --no-deps -r /tmp/requirements.txt

# Our own packages, from the wheelhouse ONLY. `--find-links` alone ADDS
# /wheels and keeps PyPI as an index, so every internal name was looked up on
# pypi.org: main's build on 3a0d488c failed on a 503 from
# pypi.org/simple/swarm-redaction/, and a public package published under one of
# these names at a higher version could have been installed instead of ours --
# dependency confusion into a control-plane image. `--no-index` means no index
# is ever asked for them; `--no-deps` because their third-party dependencies
# are already installed above, by hash.
RUN uv pip install --python /opt/venv/bin/python --no-index --find-links /wheels --no-deps \
      swarm-common swarm-redaction swarm-workflow-rollup swarm-api

# With `--no-deps` on both installs nothing above notices a dependency the
# export left out. `uv pip check` does, here, instead of an ImportError at the
# runtime stage's import check or in production.
RUN uv pip check --python /opt/venv/bin/python

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NO_COLOR=1 \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PORT=8080

# ca-certificates for the Google APIs and the cluster endpoint; tini so that a
# SIGTERM during a pass reaches the process rather than being swallowed by PID 1.
# `apt-get upgrade` is not belt-and-braces here. The base image is pinned to a
# digest, deliberately, so its packages stay at whatever the snapshot shipped
# even as Debian publishes fixes for them -- a pinned base is a FROZEN set of
# CVEs, not a safe one. `install` alone never touches a package that is already
# present, which is how libpcre2-8-0 sat at 10.42-1 with a fixed 10.42-1+deb12u1
# available and failed the promote scan on every image at once.
RUN apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# The python:3.11-slim base carries its own setuptools and wheel in
# /usr/local/lib, entirely separate from /opt/venv -- which is why building the
# venv --without-pip did not remove them, and why the promote scan still flagged
# jaraco.context (path traversal, vendored in setuptools) and wheel (arbitrary
# code execution) after that change.
#
# A control-plane service imports neither. They are build tooling that the base
# image happens to ship, so they are removed rather than upgraded: an upgrade
# only moves the next advisory further out, while nothing here will ever miss
# them. pip goes last, because it is what performs its own removal.
RUN /usr/local/bin/python3 -m pip uninstall --yes setuptools wheel \
 && /usr/local/bin/python3 -m pip uninstall --yes pip \
 && ! /usr/local/bin/python3 -c "import setuptools" 2>/dev/null \
 && ! /usr/local/bin/python3 -c "import pip" 2>/dev/null

COPY --from=builder /opt/venv /opt/venv

# ABSOLUTE PATHS ON PURPOSE: the runtime PATH above omits /usr/sbin so the
# service cannot reach admin tooling, and groupadd/useradd live there.
RUN /usr/sbin/groupadd --gid 10001 swarm \
 && /usr/sbin/useradd --uid 10001 --gid 10001 --create-home --home-dir /home/swarm --shell /usr/sbin/nologin swarm

USER swarm:swarm
WORKDIR /home/swarm

# A service that cannot import its own app is broken in a way that must not
# reach production, so the build proves it can. `create_app()` with no argument
# builds no Firestore client -- every Google import in this package is inside
# the function that needs it -- so this runs with no credentials.
# CORRECTION (2026-10-06): swarm-api's create_app() DOES construct a
# firestore.Client (deps.build_context -> build_firestore), which asks for
# Application Default Credentials at construction. Cloud Build has them from
# its metadata server, so this passed there; a pull request's build on the
# GitHub runner (PRB, #688) has none and failed. FIRESTORE_EMULATOR_HOST makes
# the client use anonymous credentials and connect nothing until first use, so
# the check still proves the app imports and wires up, and needs no identity.
# PROJECT_ID is supplied only so Settings.from_env() can construct; this
# proves the module imports and wires up, and builds no Google client.
RUN PROJECT_ID=build-time-import-check FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    python -c "from swarm_api.main import create_app; create_app(); print('swarm-api ok')"

# Which build this is, served by GET /v1/version (U25). scripts/build-images.sh
# passes both; a build that does not (a laptop `docker build`) leaves them
# empty, and the route serves an empty value as null rather than a guess.
# Declared LAST, after every expensive layer: a new SHA per build would
# otherwise invalidate the cache of everything below the ARG.
ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME}

EXPOSE 8080
# These three expose create_app() and deliberately no module-level `app`, so
# uvicorn is given --factory. sh -c is needed because Cloud Run supplies the
# port through $PORT and the exec form performs no variable expansion; `exec`
# keeps uvicorn as the signal-receiving child of tini rather than a grandchild.
ENTRYPOINT ["/usr/bin/tini", "--", "sh", "-c", "exec uvicorn --factory swarm_api.main:create_app --host 0.0.0.0 --port ${PORT:-8080}"]
