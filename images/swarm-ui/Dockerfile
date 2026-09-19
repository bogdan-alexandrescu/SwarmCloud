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

# --- build -----------------------------------------------------------------
# Digest pinned like every other base in this repository, resolved 2026-09-19.
ARG NODE_IMAGE=node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0
FROM ${NODE_IMAGE} AS build

WORKDIR /build

# Dependencies first, so a source-only change does not reinstall them.
COPY apps/swarm-ui/package.json apps/swarm-ui/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund

COPY apps/swarm-ui/ ./
# `npm run build` runs tsc first, so a type error FAILS THE IMAGE rather than
# shipping a bundle nobody typechecked.
RUN npm run build

# --- runtime ---------------------------------------------------------------
# nginx-unprivileged rather than nginx: it listens on 8080 and runs as a
# non-root uid out of the box, matching the posture of the other four images
# without a templated config or a chown pass.
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0
FROM ${NGINX_IMAGE}

COPY images/swarm-ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 8080
