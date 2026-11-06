# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# swarm-github-hooks -- the public GitHub push receiver (docs/design/graph-service.md
# §6.2; lane GS-HOOKS, part of #1384): it accepts only a signed push to a
# registered repository's default branch, dedups the delivery in Firestore,
# and forwards the repository's name to swarm-api's push route.
#
# Built by every release: GS-HOOKS put it in build-images.sh's and
# push-images.sh's ALL_TARGETS, and terraform/infra/github_hooks.tf deploys the
# promoted digest as the Cloud Run service swarm-github-hooks once
# enable_github_hooks is set. A pull request builds only the images its diff
# reaches, so this lane's build check builds and scans it before a release does.
#
# Beside the service, as apps/swarm-graph/Dockerfile is: `scripts/build-images.sh`
# `find_recipe()` accepts `apps/<target>/Dockerfile`. Build from the
# REPOSITORY ROOT -- the frozen contract and the redaction package are part of
# the image and are resolved through `{ path = "../common" }` and
# `{ path = "../redaction" }`:
#
#     docker build -f apps/swarm-github-hooks/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-github-hooks:$TAG .
#
# THE SAME POSTURE AS swarm-graph and the control-plane images: every FROM
# carries a digest, compilers stay in the builder, third-party packages are
# installed by hash with nothing resolved at build time (#663), our own
# packages come from the local wheelhouse only, and the runtime runs as uid
# 10001. Nothing below writes at run time (PYTHONDONTWRITEBYTECODE; the secret
# is held in memory).
#
# ITS OWN LOCK: it is not a member of the root uv project. It was seeded from
# apps/swarm-typesafe-fwd/uv.lock, whose third-party closure it shares exactly
# (google-auth and requests; no Firestore SDK -- Firestore is called over
# REST). To regenerate after apps/swarm-github-hooks/pyproject.toml changes:
#
#     cp apps/swarm-typesafe-fwd/uv.lock apps/swarm-github-hooks/uv.lock && uv lock --project apps/swarm-github-hooks
#
# NO CREDENTIAL IN THE IMAGE. The webhook secret is read from Secret Manager at
# run time with the service's own identity, into memory.
# Digest resolved from the registry on 2026-09-15; the same base every service
# image pins, so all of them move together.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
# The uv the other service images use (images/swarm-api/Dockerfile says why
# 0.12.15 and not older).
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

FROM ${MIRROR_GHCR}/${UV_IMAGE} AS uvbin

# ---------------------------------------------------------------------------
# builder: wheels for the frozen contract, redaction and the service, then /opt/venv
# ---------------------------------------------------------------------------
FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS builder

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
COPY apps/redaction/ /src/apps/redaction/
COPY apps/swarm-github-hooks/ /src/apps/swarm-github-hooks/

RUN uv build --wheel --out-dir /wheels /src/apps/common \
 && uv build --wheel --out-dir /wheels /src/apps/redaction \
 && uv build --wheel --out-dir /wheels /src/apps/swarm-github-hooks

# --without-pip: see images/swarm-reconciler/Dockerfile. Nothing at runtime
# imports pip, setuptools or wheel, and all three were flagged by the scan.
RUN python -m venv --without-pip /opt/venv

# Third-party dependencies, exported from apps/swarm-github-hooks/uv.lock with a hash
# for every file. `--no-emit-local` leaves our three packages out:
# they come from /wheels, below. `--no-hashes` must never be added here.
RUN uv export --frozen --no-dev --no-emit-local --no-editable \
      --project /src/apps/swarm-github-hooks > /tmp/requirements.txt

# At exactly the versions and hashes the lock records; `--no-deps` because the
# export is already the complete closure.
RUN uv pip install --python /opt/venv/bin/python --require-hashes --no-deps -r /tmp/requirements.txt

# Our own packages, from the wheelhouse ONLY: `--no-index`, so no index is ever
# asked for a name that is ours (dependency confusion, #663).
RUN uv pip install --python /opt/venv/bin/python --no-index --find-links /wheels --no-deps \
      swarm-common swarm-redaction swarm-github-hooks

RUN uv pip check --python /opt/venv/bin/python

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NO_COLOR=1 \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"

# ca-certificates for the TLS endpoints of Secret Manager, Firestore and
# swarm-api; tini so a SIGTERM reaches the receiver, which stops accepting and
# exits inside Cloud Run's grace. `apt-get upgrade` because a pinned base is a
# frozen set of CVEs, not a safe one (images/swarm-reconciler/Dockerfile).
RUN apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Build tooling the base ships outside /opt/venv; the service imports none of it.
RUN /usr/local/bin/python3 -m pip uninstall --yes setuptools wheel \
 && /usr/local/bin/python3 -m pip uninstall --yes pip \
 && ! /usr/local/bin/python3 -c "import setuptools" 2>/dev/null \
 && ! /usr/local/bin/python3 -c "import pip" 2>/dev/null

COPY --from=builder /opt/venv /opt/venv

# ABSOLUTE PATHS ON PURPOSE: the runtime PATH omits /usr/sbin.
RUN /usr/sbin/groupadd --gid 10001 swarm \
 && /usr/sbin/useradd --uid 10001 --gid 10001 --create-home --home-dir /home/swarm --shell /usr/sbin/nologin swarm

USER swarm:swarm
WORKDIR /home/swarm

# The build proves the receiver imports. Import only: it reads its secret
# from Secret Manager at run time, which needs an identity a build does not have.
RUN python -c "import swarm_github_hooks.main, swarm_github_hooks.server; print('swarm-github-hooks ok')"

ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME}

# Plain HTTP on 8080 (swarm_github_hooks.main.DEFAULT_PORT, Cloud Run's PORT);
# Cloud Run terminates TLS in front of it.
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_github_hooks.main"]
