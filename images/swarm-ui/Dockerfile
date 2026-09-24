# syntax=docker/dockerfile:1.7
#
# swarm-ui -- the static web UI, served behind the same load balancer as swarm-api.
#
# Listed in ALL_TARGETS in scripts/build-images.sh, which looks for exactly one
# of images/<target>/cloudbuild.yaml, images/<target>/Dockerfile,
# apps/<target>/Dockerfile or docker/<target>/Dockerfile. A missing recipe only
# WARNS, so a target with no Dockerfile builds nothing while terraform deploys
# the tag -- see the header of images/swarm-api/Dockerfile for the incident.
#
# Build from the REPOSITORY ROOT:
#
#     docker build -f images/swarm-ui/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-ui:$TAG .
#
# WHY A SEPARATE SERVICE AND NOT STATIC FILES INSIDE swarm-api
# ------------------------------------------------------------
# The UI ships far more often than the API, and coupling them means every button
# colour change redeploys the service that holds the tenant boundary. Separate
# also keeps the API image free of a node toolchain. The cost is one more Cloud
# Run service and a path rule on the URL map; the load balancer sends /v1/* to
# swarm-api and everything else here.

# BOTH ARGs LIVE HERE, ABOVE THE FIRST FROM, AND MUST STAY THERE. An ARG
# declared after a FROM is scoped to that build STAGE, so the nginx one sitting
# next to the runtime stage where it reads best was empty by the time the second
# FROM evaluated it: "base name (${NGINX_IMAGE}) should not be blank". The build
# ran all the way through npm first, so the failure arrived a minute in.
#
# Digests pinned like every other base here, resolved from the registry on
# 2026-09-19 rather than written from memory.
#
# nginx 1.31.6-alpine, not 1.27: trivy refused to promote the 1.27 build --
# "unfixed-excluded HIGH,CRITICAL vulnerabilities" -- over zlib 1.3.1-r2
# (CVE-2026-22184, arbitrary code execution via a buffer overflow in untgz),
# fixed in 1.3.2-r0. The security gate in scripts/push-images.sh caught it
# between build and promotion, which is exactly where it should be caught.
# Bumping the base is the fix; excluding the finding would not be.
#
# NODE 24 LTS, THE SAME DIGEST images/agent-runtime-base PINS. This was
# node:20-bookworm-slim until 2026-09-24, although Node 20 reached end of life in
# April 2026 and docs/versions.md had already moved the platform to 24. The
# digest is the base image's, re-resolved from Docker Hub on 2026-09-24: an OCI
# index whose linux/amd64 image carries NODE_VERSION=24.21.0 -- the same Node
# the current `24-bookworm-slim` tag points at -- so the platform has one Node
# build, not two.
#
# THIS LINE IS ALSO THE UI JOB'S NODE. .github/workflows/application.yml reads
# the major from here rather than pinning its own, so CI tests and builds the
# bundle on the line this image ships it from. tests/unit/scripts/
# test_ui_node_line.py holds both: one major across every Node image, pinned
# by digest, and the ui job reading it from this file.
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.31.6-alpine@sha256:b54ac358b83fc6c965793fd271839b4ea4cdb6e99895bb19618cbc2ca152d972

# --- build -----------------------------------------------------------------
FROM ${NODE_IMAGE} AS build

WORKDIR /build

# Dependencies first, so a source-only change does not reinstall them.
COPY apps/swarm-ui/package.json apps/swarm-ui/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund

COPY apps/swarm-ui/ ./
# `npm run build` runs tsc first, so a type error FAILS THE IMAGE rather than
# shipping a bundle nobody typechecked.
# THE ENVIRONMENT THE BADGE NAMES, baked in at build time.
#
# Vite inlines `import.meta.env.VITE_*` at build, so this cannot be supplied at
# run time by the Cloud Run service -- a static bundle has already decided.
# Without it `classifyEnvironment` falls through to `unknown` and the header
# draws a loud full-width "ENVIRONMENT UNKNOWN" bar on every screen.
#
# It is deliberately NOT defaulted to "dev". A wrong-but-quiet environment
# label is the exact failure the badge replaced: the old `<span class="env">`
# was hardcoded to "dev" and said so in production too. An unset build should
# say it does not know, loudly, rather than guess and be believed.
ARG VITE_SWARM_ENV=""
ENV VITE_SWARM_ENV=$VITE_SWARM_ENV
RUN npm run build

# --- runtime ---------------------------------------------------------------
# nginx-unprivileged rather than nginx: it listens on 8080 and runs as a
# non-root uid out of the box, matching the posture of the other four images
# without a templated config or a chown pass. Its ARG is at the top of the file.
FROM ${NGINX_IMAGE}

# THE BASE LAGS ALPINE, SO THE FIXED PACKAGE IS TAKEN FROM ALPINE DIRECTLY.
#
# Release 35969538707 refused to promote this image over libexpat 2.8.4-r0
# (CVE-2026-93990, HIGH, fixed in 2.8.5-r0). Docker Hub rebuilt 1.31.6-alpine
# on 2026-09-22 under a new digest, and that rebuild STILL ships 2.8.4-r0 --
# checked by listing its packages, not assumed -- so bumping the digest alone
# would have failed the same gate after another twenty minutes of release.
#
# Upgrading the one named package, rather than `apk upgrade` of everything,
# keeps the runtime otherwise identical to the pinned digest. nginx-unprivileged
# runs as uid 101; the USER line hands it back after the root-only apk step.
USER root
RUN apk upgrade --no-cache libexpat
USER 101

COPY images/swarm-ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 8080
