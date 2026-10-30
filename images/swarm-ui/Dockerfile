# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
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

# --- build -----------------------------------------------------------------
FROM ${MIRROR_DOCKER_HUB}/library/${NODE_IMAGE} AS build

WORKDIR /build

# Dependencies first, so a source-only change does not reinstall them.
COPY apps/swarm-ui/package.json apps/swarm-ui/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund

COPY apps/swarm-ui/ ./
# NO ENVIRONMENT IS COMPILED IN (DAYONE-UIENV-1). This stage used to take
# `ARG VITE_SWARM_ENV` and promote it to an ENV before the build, because Vite
# inlines `import.meta.env.VITE_*` into the bundle -- which made a dev image and
# a prod image two different images, and a release could not ship one bundle
# to every deployment. The bundle now reads its environment at run time from
# `/config.json`, which the entrypoint script below writes from the container's
# own environment (apps/swarm-ui/src/runtimeConfig.ts), and
# apps/swarm-ui/src/__tests__/runtimeConfig.test.ts refuses any VITE_ ARG or
# ENV in this file.
#
# `npm run build` runs tsc first, so a type error FAILS THE IMAGE rather than
# shipping a bundle nobody typechecked.
RUN npm run build

# --- runtime ---------------------------------------------------------------
# nginx-unprivileged rather than nginx: it listens on 8080 and runs as a
# non-root uid out of the box, matching the posture of the other four images
# without a templated config or a chown pass. Its ARG is at the top of the file.
FROM ${MIRROR_DOCKER_HUB}/${NGINX_IMAGE}

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
# Release 36970395891 (2026-10-02) refused the image again, this time over
# pcre2 10.48-r0 (CVE-2026-103111, HIGH, fixed in 10.49-r0); same reason, same fix.
# Releases 37812320090 and 37816339298 (2026-10-08) refused it over tiff 4.7.1-r0
# (CVE-2026-4775, HIGH, fixed in 4.7.2-r0), holding every merge since 09:37Z.
# Releases 39158785934, 39161799433 and 39162254559 (2026-10-18) refused it over
# libx11 1.8.13-r0 (CVE-2026-88806, HIGH, fixed in 1.8.14-r0), holding every merge
# since 17:33Z.
# PR #2130's build-check (run 40024164828, 2026-10-23) refused it over brotli-libs
# 1.2.0-r1 (CVE-2026-111497, HIGH, fixed in 1.2.1-r0); same reason, same fix.
# PR #2390's and PR #2415's build-checks (runs 40260439312 and 40263732191,
# 2026-10-26) refused it over curl and
# libcurl 8.22.0-r0 (CVE-2026-92392, HIGH, fixed in 8.23.0-r0); same reason, same fix.
USER root
# APT_REFRESH: the build scripts/build-images.sh makes this layer in (every
# main build and release), so the upgrade below is never an older build's
# (owner decision 2026-10-14).
ARG APT_REFRESH=unset
RUN echo "apk layer of ${APT_REFRESH}" \
 && apk upgrade --no-cache libexpat pcre2 tiff libx11 brotli-libs curl libcurl

# THE ENVIRONMENT, WRITTEN AT START, NOT AT BUILD. nginx's own entrypoint runs
# every executable script in /docker-entrypoint.d before it starts nginx, as
# uid 101, so the file goes under /tmp (the bundle's directory is root's) and
# nginx.conf serves it at /config.json. The service declares its environment
# with the same ENVIRONMENT variable swarm-api reads. Unset, or anything but a
# plain environment name, writes `{}`: the console then says ENVIRONMENT
# UNKNOWN, loudly, rather than draw a name nobody checked. The value is never
# echoed to the log.
#
# A FILE IN THE REPOSITORY, COPIED, NEVER A HEREDOC. Main's Cloud Build builds
# this image with the classic builder (scripts/build-images.sh generate_config
# sets DOCKER_BUILDKIT=1 only for a Dockerfile that mounts the tool mirror),
# and a `RUN cat > ... <<"EOF"` heredoc is BuildKit's: written that way by
# DAYONE-UIENV-1 (#2744), it passed the pull request's buildx build-check and
# failed every main `build images` run from 16:01Z on 2026-10-29
# (SWARM-UI-IMAGE-FIX). tests/unit/scripts/test_classic_builder_dockerfiles.py
# refuses BuildKit-only syntax in every image the classic builder builds.
#
# NO `RUN chmod` AFTER THE COPY. The script is committed executable (git mode
# 100755) and COPY keeps the mode, so a chmod changes nothing, and its layer is
# byte-identical to the COPY's. The release's layer check refuses an image that
# repeats a layer, because GKE image streaming will not stream it (docs/
# browser-startup-speed.md section 2.4); that refusal stopped the release of
# c08dc1e75 on 2026-10-29. `COPY --chmod` is BuildKit's, so it is no way out
# either. tests/unit/scripts/test_ui_build_declares_its_environment.py holds both
# the executable bit and the absence of the chmod.
COPY images/swarm-ui/40-swarm-ui-config.sh /docker-entrypoint.d/40-swarm-ui-config.sh
USER 101

COPY images/swarm-ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 8080
