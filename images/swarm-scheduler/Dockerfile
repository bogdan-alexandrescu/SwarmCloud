# syntax=docker/dockerfile:1.7
#
# swarm-scheduler -- the admission controller: it acquires leases atomically and dispatches executions.
#
# This file exists because `scripts/build-images.sh` lists `swarm-scheduler` in
# ALL_TARGETS and `find_recipe()` looks for exactly one of
# `images/<target>/cloudbuild.yaml`, `images/<target>/Dockerfile`,
# `apps/<target>/Dockerfile` or `docker/<target>/Dockerfile`. With none of them
# present the script WARNS and continues, so `make build` reported success
# having built nothing, while `terraform/infra/main.tf` deploys
# `swarm-scheduler:${var.image_tag}` and the security workflow scans it. A
# missing recipe that does not fail the build is how an audited application ends
# up undeployable in silence.
#
# Build from the REPOSITORY ROOT -- the frozen contract package and the quota
# broker's package are both part of the image (this line said "the reconciler",
# copied from that image's Dockerfile; the reconciler was never copied in), and
# `apps/scheduler/pyproject.toml` resolves them through `{ path = "../common" }`
# and `{ path = "../quota-broker" }`, so all three must keep their relative
# positions inside the build context:
#
#     docker build -f images/swarm-scheduler/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-scheduler:$TAG .
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
# builder: wheels for the frozen contract, the quota broker's package and the
# scheduler, then /opt/venv
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
COPY apps/common/ /src/apps/common/
# The quota broker's PACKAGE, not its service: admission reads the account
# pool with the broker's own model and asks the broker's own function who an
# account serves (scheduler/credentials.py, #169). `apps/scheduler/pyproject.toml`
# resolves it through `{ path = "../quota-broker" }`, so it keeps the same
# relative position as the frozen contract does.
COPY apps/quota-broker/ /src/apps/quota-broker/
COPY apps/scheduler/ /src/apps/scheduler/

# Build `swarm-common` into a local wheelhouse first and resolve it from there:
# no index has ever heard of it, so `--find-links` is what makes this build work
# without a private registry. `swarm-quota-broker` the same way.
RUN uv build --wheel --out-dir /wheels /src/apps/common \
 && uv build --wheel --out-dir /wheels /src/apps/quota-broker \
 && uv build --wheel --out-dir /wheels /src/apps/scheduler

# `--without-pip`: the stdlib venv otherwise seeds pip, setuptools and wheel,
# and BUILD tooling is what those are. None of it is imported at runtime, and
# all three were flagged by the promote scan (jaraco.context path traversal,
# vendored inside setuptools; wheel privilege escalation). uv installs into the
# venv from outside it, so nothing here needs a pip inside it.
RUN python -m venv --without-pip /opt/venv \
 && uv pip install --python /opt/venv/bin/python --find-links /wheels swarm-scheduler

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
# PROJECT_ID is supplied only so Settings.from_env() can construct; this
# proves the module imports and wires up, and builds no Google client.
RUN PROJECT_ID=build-time-import-check \
    python -c "from scheduler.main import create_app; create_app(); print('swarm-scheduler ok')"

EXPOSE 8080
# These three expose create_app() and deliberately no module-level `app`, so
# uvicorn is given --factory. sh -c is needed because Cloud Run supplies the
# port through $PORT and the exec form performs no variable expansion; `exec`
# keeps uvicorn as the signal-receiving child of tini rather than a grandchild.
ENTRYPOINT ["/usr/bin/tini", "--", "sh", "-c", "exec uvicorn --factory scheduler.main:create_app --host 0.0.0.0 --port ${PORT:-8080}"]
