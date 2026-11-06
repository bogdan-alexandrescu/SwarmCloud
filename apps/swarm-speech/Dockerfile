# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# swarm-speech -- Voice it's own speech server (docs/design/voice-it-own-speech.md
# §5.2): Qwen3-ASR-0.6B's streaming path through vLLM and Kokoro-82M, behind
# one WebSocket, on the speech machine's one NVIDIA L4. Not a Kubernetes or
# Cloud Run workload: the machine's systemd unit runs it with `docker run
# --gpus all` (apps/swarm-speech/vm/swarm-speech-launch.sh), pinned by digest.
#
# Here, beside its app as swarm-voice's recipe is; `scripts/build-images.sh`
# `find_recipe()` accepts `apps/<target>/Dockerfile`.
#
# Build from the REPOSITORY ROOT, as every service image is, with BuildKit (a
# RUN mounts uv from a stage rather than shipping it):
#
#     DOCKER_BUILDKIT=1 docker build -f apps/swarm-speech/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-speech:$TAG .
#
# THE SAME POSTURE AS swarm-voice: every FROM carries a digest, compilers stay
# in the builder, third-party packages are installed by hash with nothing
# resolved at build time (#663), our own package comes from the local
# wheelhouse only, and the runtime runs as uid 10001.
#
# ITS OWN LOCK. swarm-speech is not a member of the root uv project (its tests
# import it by path); apps/swarm-speech/uv.lock is its closure, resolved for
# Linux x86_64 only (vLLM ships nothing else). To regenerate it after
# apps/swarm-speech/pyproject.toml changes:
#
#     uv lock --project apps/swarm-speech
#
# NO CUDA BASE. torch and vLLM's wheels carry their CUDA libraries; the
# driver's libcuda comes from the host through the NVIDIA container toolkit
# (`--gpus all`), and the host's driver is the Deep Learning VM image's (§5.1).
#
# NO WEIGHTS. The models change with Settings (§11), so the server fetches
# them by pinned commit and checks every file's SHA-256 into the host's
# /var/lib/swarm-speech/models, mounted at the same path (swarm_speech/models.py).
#
# Digest resolved from the registry on 2026-09-15; the same base every service
# image pins, so all of them move together.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
# The uv the other service images use (images/swarm-api/Dockerfile says why
# 0.12.15 and not older).
ARG UV_IMAGE=astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3
# Every base is pulled through this project's Artifact Registry mirrors
# (terraform/infra/build_mirror.tf; docs/runbooks/build-mirror.md). No default,
# so this file names no project: scripts/build-images.sh passes the mirror of
# the project it builds for (lib/build-mirror.sh mirror_build_args).
ARG MIRROR_DOCKER_HUB=
ARG MIRROR_GHCR=

FROM ${MIRROR_GHCR}/${UV_IMAGE} AS uvbin

# ---------------------------------------------------------------------------
# deps: the third-party closure, /opt/venv, from the lock alone
# ---------------------------------------------------------------------------
# LAYER CACHE (scripts/build-images.sh LAYER_CACHED, 2026-11-06). This stage
# reads pyproject.toml and uv.lock and nothing else, and names no
# APT_REFRESH, so BuildKit's inline cache from the previous image serves it --
# and the runtime's copy of it, which comes before that stage's apt layer --
# whenever the lock is unchanged. Rebuilt, it took most of the 1,717 s build.
# Nothing here ships but /opt/venv: its compilers stay in this stage.
FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS deps

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uvbin /uv /usr/local/bin/uv

WORKDIR /src
COPY apps/swarm-speech/pyproject.toml apps/swarm-speech/uv.lock /src/apps/swarm-speech/

# --without-pip: see images/swarm-reconciler/Dockerfile.
RUN python -m venv --without-pip /opt/venv

# Third-party dependencies, exported from apps/swarm-speech/uv.lock with a hash
# for every file (en-core-web-sm included, by its URL). `--no-emit-local`
# leaves swarm-speech out: it comes from /wheels, in the runtime. `--no-hashes`
# must never be added here.
RUN uv export --frozen --no-dev --no-emit-local --no-editable \
      --project /src/apps/swarm-speech > /tmp/requirements.txt

RUN uv pip install --python /opt/venv/bin/python --require-hashes --no-deps -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# builder: the swarm-speech wheel, from the source
# ---------------------------------------------------------------------------
FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

COPY --from=uvbin /uv /usr/local/bin/uv

WORKDIR /src
COPY apps/swarm-speech/ /src/apps/swarm-speech/

RUN uv build --wheel --out-dir /wheels /src/apps/swarm-speech

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS runtime

# HF_HOME, VLLM_CACHE_ROOT, TRITON_CACHE_DIR: on the host's kept disk (the
# launcher mounts /var/lib/swarm-speech), so a restarted container or a
# restarted machine reuses vLLM's and Triton's compiled kernels instead of
# compiling them again at every start (start.load, §4.2).
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NO_COLOR=1 \
    HF_HOME=/var/lib/swarm-speech/cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    VLLM_CACHE_ROOT=/var/lib/swarm-speech/cache/vllm \
    TRITON_CACHE_DIR=/var/lib/swarm-speech/cache/triton \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"

# ca-certificates for Google's token certificates and Hugging Face; tini so a
# SIGTERM (`docker stop`) reaches the server. gcc and libc6-dev: Triton, under
# vLLM, compiles its CUDA launcher with the system C compiler at the first
# model load, and fails with "Failed to find C compiler" without one.
# `apt-get upgrade` because a pinned base is a frozen set of CVEs, not a safe
# one (images/swarm-reconciler/Dockerfile).
# The third-party closure FIRST, before the apt layer and before any source:
# the layer the inline cache serves while the lock is unchanged (deps, above).
COPY --from=deps /opt/venv /opt/venv

ARG APT_REFRESH=unset
RUN echo "apt layer of ${APT_REFRESH}" \
 && apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y ca-certificates tini gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# Build tooling the base ships outside /opt/venv; swarm-speech imports none of it.
RUN /usr/local/bin/python3 -m pip uninstall --yes setuptools wheel \
 && /usr/local/bin/python3 -m pip uninstall --yes pip \
 && ! /usr/local/bin/python3 -c "import setuptools" 2>/dev/null \
 && ! /usr/local/bin/python3 -c "import pip" 2>/dev/null

# Our own package, from the wheelhouse ONLY: `--no-index`, so no index is ever
# asked for a name that is ours (dependency confusion, #663). uv is mounted
# for this one RUN, not copied in, so the runtime does not ship it.
RUN --mount=type=bind,from=uvbin,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    UV_NO_CACHE=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
      /usr/local/bin/uv pip install --python /opt/venv/bin/python --no-index --find-links /wheels --no-deps \
      swarm-speech \
 && UV_NO_CACHE=1 /usr/local/bin/uv pip check --python /opt/venv/bin/python

# ABSOLUTE PATHS ON PURPOSE: the runtime PATH omits /usr/sbin. The uid is the
# one the launcher gives /var/lib/swarm-speech to.
RUN /usr/sbin/groupadd --gid 10001 swarm \
 && /usr/sbin/useradd --uid 10001 --gid 10001 --create-home --home-dir /home/swarm --shell /usr/sbin/nologin swarm

USER swarm:swarm
WORKDIR /home/swarm

# The build proves the server, both model libraries, NVML's binding and the
# spaCy model import. Import only: there is no GPU and no weight here.
RUN python -c "import swarm_speech.main, swarm_speech.engines, qwen_asr, kokoro, pynvml, en_core_web_sm; print('swarm-speech ok')"

ARG GIT_SHA=""
ARG BUILD_TIME=""
ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME}

# Plain HTTP on 8080: the firewall admits only the subnet to it, and every
# route checks swarm-api's Google ID token (§3.2).
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_speech.main"]
