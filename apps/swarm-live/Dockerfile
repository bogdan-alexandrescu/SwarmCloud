# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# swarm-live -- the live browser's relay (docs/design/live-browser.md §3.3, §7;
# lane LB-L8e, part of #1030): where a tenant pod's live gate and swarm-api's
# proxied viewers meet, and the recorder of masked frames.
#
# Here, not under images/, because the brief for this lane put it beside the
# relay; `scripts/build-images.sh` `find_recipe()` accepts
# `apps/<target>/Dockerfile` as it accepts `images/<target>/Dockerfile`.
#
# Build from the REPOSITORY ROOT -- the frozen contract package is part of the
# image, and `apps/swarm-live/pyproject.toml` resolves `swarm-common` through
# `{ path = "../common" }`, so the two must keep their relative positions:
#
#     docker build -f apps/swarm-live/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-live:$TAG .
#
# THE SAME POSTURE AS THE FOUR CONTROL-PLANE IMAGES (images/swarm-api and its
# siblings), for the same reasons: every FROM carries a digest, compilers stay
# in the builder, third-party packages are installed by hash with nothing
# resolved at build time (#663), our own packages come from the local wheelhouse
# only, and the runtime runs as uid 10001 -- the uid
# kubernetes/swarm-system/swarm-live.yaml runs it as, under the `restricted`
# pod security standard.
#
# ITS OWN LOCK. swarm-live is not a member of the root uv project (its tests
# import it by path), so the root uv.lock has no entry for it and the root
# export the other images use cannot name its closure. apps/swarm-live/uv.lock
# is that closure, resolved at the root lock's versions so the relay ships the
# fastapi, uvicorn and google-cloud-storage every other service ships. To
# regenerate it after apps/swarm-live/pyproject.toml changes, seed it from the
# root lock so those versions are kept:
#
#     cp uv.lock apps/swarm-live/uv.lock && uv lock --project apps/swarm-live
#
# FFMPEG. The recorder encodes VP8 in WebM through an `ffmpeg` binary
# (swarm_live/recorder.py, FfmpegVp8Encoder: `-c:v libvpx`). Debian's ffmpeg is
# built with libvpx; the build proves the encoder is there rather than trusting
# that, because a relay without it serves every session and records none.
#
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
# builder: wheels for the frozen contract and the relay, then /opt/venv
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
 && apt-get install --no-install-recommends -y build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uvbin /uv /usr/local/bin/uv

WORKDIR /src
COPY apps/common/ /src/apps/common/
COPY apps/swarm-live/ /src/apps/swarm-live/

RUN uv build --wheel --out-dir /wheels /src/apps/common \
 && uv build --wheel --out-dir /wheels /src/apps/swarm-live

# --without-pip: see images/swarm-reconciler/Dockerfile. Nothing at runtime
# imports pip, setuptools or wheel, and all three were flagged by the scan.
RUN python -m venv --without-pip /opt/venv

# Third-party dependencies, exported from apps/swarm-live/uv.lock with a hash
# for every file. `--no-emit-local` leaves swarm-common and swarm-live out:
# they come from /wheels, below. `--no-hashes` must never be added here.
RUN uv export --frozen --no-dev --no-emit-local --no-editable \
      --project /src/apps/swarm-live > /tmp/requirements.txt

# At exactly the versions and hashes the lock records; `--no-deps` because the
# export is already the complete closure.
RUN uv pip install --python /opt/venv/bin/python --require-hashes --no-deps -r /tmp/requirements.txt

# Our own packages, from the wheelhouse ONLY: `--no-index`, so no index is ever
# asked for a name that is ours (dependency confusion, #663).
RUN uv pip install --python /opt/venv/bin/python --no-index --find-links /wheels --no-deps \
      swarm-common swarm-live

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

# ca-certificates for Cloud Storage and Secret Manager; tini so a SIGTERM
# reaches the relay, which closes its sockets inside the pod's 30 s grace;
# ffmpeg for the recorder. `apt-get upgrade` because a pinned base is a frozen
# set of CVEs, not a safe one (images/swarm-reconciler/Dockerfile).
# APT_REFRESH: the build scripts/build-images.sh makes this layer in (every
# main build and release), so the upgrade below is never an older build's
# (owner decision 2026-10-14).
ARG APT_REFRESH=unset
RUN echo "apt layer of ${APT_REFRESH}" \
 && apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y ca-certificates tini ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Build tooling the base ships outside /opt/venv; the relay imports none of it.
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

# The build proves the relay imports and the recorder's encoder exists. Import
# only: the relay reads swarm-live-key from Secret Manager when it starts,
# which needs an identity a build does not have.
RUN python -c "import swarm_live.main, swarm_live.recorder, swarm_live.rendezvous; print('swarm-live ok')" \
 && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q ' libvpx ' \
 && echo 'ffmpeg has libvpx'

ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME}

# Plain ws on 8080, fixed for both integration lanes (owner decision
# 2026-10-11 (b); swarm_live.main.PORT). No TLS and no certificate: TLS ends at
# the load balancer in front of swarm-api, and this port is reachable only from
# swarm-api's egress range and the tenant gates (the NetworkPolicies).
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_live.main"]
