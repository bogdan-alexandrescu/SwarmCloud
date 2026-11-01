# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# swarm-voice -- Voice it's GPT-Live session holder (docs/design/voice-it.md
# §3.2, §3.4, section 10 phase one): it answers the browser's WebRTC offer
# through OpenAI and holds each live session's sideband WebSocket for the
# length of the conversation. A one-replica StatefulSet in swarm-system on the
# GKE cluster, kubernetes/swarm-system/swarm-voice.yaml.
#
# Here, not under images/, beside its app as swarm-live's recipe is (the
# nearest precedent: a swarm-system service holding long-lived sockets);
# `scripts/build-images.sh` `find_recipe()` accepts `apps/<target>/Dockerfile`
# as it accepts `images/<target>/Dockerfile`.
#
# Build from the REPOSITORY ROOT -- the frozen contract package is part of the
# image, and `apps/swarm-voice/pyproject.toml` resolves `swarm-common` through
# `{ path = "../common" }`, so the two must keep their relative positions:
#
#     docker build -f apps/swarm-voice/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-voice:$TAG .
#
# THE SAME POSTURE AS THE FOUR CONTROL-PLANE IMAGES (images/swarm-api and its
# siblings), for the same reasons: every FROM carries a digest, compilers stay
# in the builder, third-party packages are installed by hash with nothing
# resolved at build time (#663), our own packages come from the local wheelhouse
# only, and the runtime runs as uid 10001 -- the uid
# kubernetes/swarm-system/swarm-voice.yaml runs it as, under the `restricted`
# pod security standard.
#
# ITS OWN LOCK. swarm-voice is not a member of the root uv project (its tests
# import it by path), so the root uv.lock has no entry for it and the root
# export the other images use cannot name its closure. apps/swarm-voice/uv.lock
# is that closure: fastapi, uvicorn, websockets, httpx and google-auth. To
# regenerate it after apps/swarm-voice/pyproject.toml changes, seed it from the
# root lock so the versions every other service ships are kept:
#
#     cp uv.lock apps/swarm-voice/uv.lock && uv lock --project apps/swarm-voice
#
# NO FFMPEG. Unlike swarm-live it encodes nothing: the audio goes between the
# browser and OpenAI over WebRTC, and swarm-voice holds only the sideband.
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
# builder: wheels for the frozen contract and swarm-voice, then /opt/venv
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
COPY apps/swarm-voice/ /src/apps/swarm-voice/

RUN uv build --wheel --out-dir /wheels /src/apps/common \
 && uv build --wheel --out-dir /wheels /src/apps/swarm-voice

# --without-pip: see images/swarm-reconciler/Dockerfile. Nothing at runtime
# imports pip, setuptools or wheel, and all three were flagged by the scan.
RUN python -m venv --without-pip /opt/venv

# Third-party dependencies, exported from apps/swarm-voice/uv.lock with a hash
# for every file. `--no-emit-local` leaves swarm-common and swarm-voice out:
# they come from /wheels, below. `--no-hashes` must never be added here.
RUN uv export --frozen --no-dev --no-emit-local --no-editable \
      --project /src/apps/swarm-voice > /tmp/requirements.txt

# At exactly the versions and hashes the lock records; `--no-deps` because the
# export is already the complete closure.
RUN uv pip install --python /opt/venv/bin/python --require-hashes --no-deps -r /tmp/requirements.txt

# Our own packages, from the wheelhouse ONLY: `--no-index`, so no index is ever
# asked for a name that is ours (dependency confusion, #663).
RUN uv pip install --python /opt/venv/bin/python --no-index --find-links /wheels --no-deps \
      swarm-common swarm-voice

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

# ca-certificates for Secret Manager, OpenAI and swarm-api; tini so a SIGTERM
# reaches the service, which closes its sidebands inside the pod's grace. `apt-get upgrade` because a pinned base is a frozen
# set of CVEs, not a safe one (images/swarm-reconciler/Dockerfile).
# APT_REFRESH: the build scripts/build-images.sh makes this layer in (every
# main build and release), so the upgrade below is never an older build's
# (owner decision 2026-10-14).
ARG APT_REFRESH=unset
RUN echo "apt layer of ${APT_REFRESH}" \
 && apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Build tooling the base ships outside /opt/venv; swarm-voice imports none of it.
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

# The build proves the service imports. Import only: it reads the person's
# credential from Secret Manager per session, which needs an identity a build
# does not have.
RUN python -c "import swarm_voice.main, swarm_voice.credential, swarm_voice.live; print('swarm-voice ok')"

ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME}

# Plain HTTP on 8080 (swarm_voice.main reads PORT; the manifest sets 8080). No
# TLS and no certificate here: swarm-api reaches it inside the VPC through the
# internal load balancer, with a Google ID token checked by the service, and
# the NetworkPolicy admits only swarm-api's egress range.
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_voice.main"]
