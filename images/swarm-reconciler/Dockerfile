# syntax=docker/dockerfile:1.7
#
# swarm-reconciler -- the Cloud Run service that compares control-plane state to
# real backend state and repairs the difference.
#
# This file exists because `scripts/build-images.sh` lists `swarm-reconciler` in
# ALL_TARGETS and `find_recipe()` looks for exactly one of
# `images/<target>/cloudbuild.yaml`, `images/<target>/Dockerfile`,
# `apps/<target>/Dockerfile` or `docker/<target>/Dockerfile`. With none of them
# present the script WARNS and continues, so `make build` reported success
# having built nothing, while `terraform/infra/main.tf` deploys
# `swarm-reconciler:${var.image_tag}` and the security workflow scans it. A
# missing recipe that does not fail the build is how an audited application ends
# up undeployable in silence.
#
# Build from the REPOSITORY ROOT -- the frozen contract package and the
# reconciler are both part of the image, and `apps/reconciler/pyproject.toml`
# resolves `swarm-common` through `{ path = "../common" }`, so the two must keep
# their relative positions inside the build context:
#
#     docker build -f images/swarm-reconciler/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-reconciler:$TAG .
#
# Same properties as agent-runtime-base, for the same reasons: every FROM
# carries a digest, compilers stay in the builder, and the runtime runs as a
# non-root uid. The reconciler is the one component with cluster-wide authority
# -- it terminates other people's executions -- so its image is the last place
# to be relaxed about any of that.
#
# Digest resolved from the registry on 2026-09-15; the same base the worker
# image pins, so both move together.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a

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
COPY apps/common/ /src/apps/common/
COPY apps/reconciler/ /src/apps/reconciler/

# Build `swarm-common` into a local wheelhouse first and resolve it from there:
# no index has ever heard of it, so `--find-links` is what makes this build work
# without a private registry.
RUN uv build --wheel --out-dir /wheels /src/apps/common \
 && uv build --wheel --out-dir /wheels /src/apps/reconciler

# `--without-pip`: the stdlib venv otherwise seeds pip, setuptools and wheel,
# and BUILD tooling is what those are. None of it is imported at runtime, and
# all three were flagged by the promote scan (jaraco.context path traversal,
# vendored inside setuptools; wheel privilege escalation). uv installs into the
# venv from outside it, so nothing here needs a pip inside it.
RUN python -m venv --without-pip /opt/venv \
 && uv pip install --python /opt/venv/bin/python --find-links /wheels swarm-reconciler

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
    python -c "from reconciler.service import create_app; create_app(); print('swarm-reconciler ok')"

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "reconciler"]
