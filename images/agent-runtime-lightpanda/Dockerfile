# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# agent-runtime-lightpanda -- agent-runtime-base plus the Lightpanda headless
# browser, for the `claude-code-lightpanda` profile (docs/design/
# lightpanda-runtime.md sections 5.D and 6.4; contract request 127).
#
# WHY ITS OWN IMAGE. Lightpanda is AGPL-3.0-only. Shipping it conveys it, so
# the image carries the licence, a notice naming the Corresponding Source, and
# the binary unmodified (design section 2.1). Putting it in agent-runtime-base
# or agent-runtime-browser would put that binary, its notices and its source
# obligation into every agent of those profiles, and 76 MB into images whose
# agents never run it. An image no profile names is built but never dispatched.
#
# NO CHROMIUM. Lightpanda renders no pixels: no screenshots, no live view, no
# take-control. A task that needs any of them is a `claude-code-browser`
# task. The base has no Chromium and this image adds none; the self-test
# fails if one is found.
#
# Build the base first, then:
#
#     docker build -f images/agent-runtime-lightpanda/Dockerfile \
#         --build-arg BASE_IMAGE=<registry>/agent-runtime-base@sha256:<digest> \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/agent-runtime-lightpanda:$TAG .
#
# BASE_IMAGE IS TAKEN BY DIGEST AND ONLY BY DIGEST, as agent-runtime-indexer
# takes it. images/agent-runtime-lightpanda/cloudbuild.yaml pulls
# agent-runtime-base:<the same tag>, reads the digest back and passes it.
# There is no default -- a build handed no base fails at FROM -- and the first
# RUN below refuses a base named by tag. The base WITH codex, not the
# -nocodex one: the profile is claude-code's in every field but its name,
# image and secret slots.
ARG BASE_IMAGE

FROM ${BASE_IMAGE} AS runtime

# Re-declared so the guard below can read it: an ARG before the first FROM is
# in scope for FROM lines only.
ARG BASE_IMAGE
# The commit this image was built from, recorded as the OCI revision label.
ARG GIT_SHA=""
LABEL org.opencontainers.image.revision="${GIT_SHA}"
# Set by BuildKit from the build's --platform (the recipe builds linux/amd64).
ARG TARGETARCH

USER root

RUN case "${BASE_IMAGE}" in \
      *@sha256:*) echo "agent-runtime-lightpanda on ${BASE_IMAGE}" ;; \
      *) echo "BASE_IMAGE must name agent-runtime-base by digest (<ref>@sha256:<64 hex>), not '${BASE_IMAGE}'" >&2; exit 1 ;; \
    esac

# LIGHTPANDA 1.0.2, published 2026-10-23. The tag is lightweight and points
# at LIGHTPANDA_COMMIT. Each binary's sha256 is the release API's own `digest`
# for the asset (https://api.github.com/repos/lightpanda-io/browser/releases/
# tags/1.0.2, read 2026-10-31), equal to the design's section 2 table; the
# amd64 one was also recomputed from the download, 76,408,136 bytes, and that
# binary printed `1.0.2` for `lightpanda version`. LIGHTPANDA_SOURCE_SHA256 is
# `sha256sum` of the tag's source archive (github.com/lightpanda-io/browser/
# archive/refs/tags/1.0.2.tar.gz, 4,451,647 bytes) as downloaded 2026-10-31.
#
# All three are read from the tool mirror like the base's release files (see
# images/agent-runtime-base/Dockerfile, THE TOOL MIRROR); tool-mirror.sha256
# lists them at these sha256s, and tests/unit/worker/test_lightpanda_image.py
# and tests/unit/scripts/test_build_mirror_switch.py hold the two equal. A
# version bump is one PR: these ARGs, the three mirror lines and
# scripts/refresh-build-mirror.sh (design section 5.D, "Updating it").
ARG LIGHTPANDA_VERSION=1.0.2
ARG LIGHTPANDA_SHA256_AMD64=ae7d01d261efb9e480e18ce3424533332883863bcf70b12a4321b5733f37e19e
ARG LIGHTPANDA_SHA256_ARM64=ac546dfc43c533430da92b75ab36f2a29da7d6324d5d38baf606ef2e0d849345
ARG LIGHTPANDA_COMMIT=c5f7bb29b127040c899d4e181b5709f76fc71a6f
ARG LIGHTPANDA_SOURCE_SHA256=6a7a274acfdbf6d87b573d1a90029d2e17f71f718f78b03099d96d48f514d64e
# The Corresponding Source the image's SOURCE notice points at: the same
# archive, kept by the release bundle beside the images for as long as the
# image is offered (AGPL section 6(d); design sections 2.1 and 6.4). A link to
# GitHub alone is not enough, because the obligation stays with whoever conveys.
ARG LIGHTPANDA_SOURCE_URL=https://storage.googleapis.com/swarmcloud/third-party/lightpanda/1.0.2/source.tar.gz

# Telemetry off: it would report from inside a tenant's pod. Core dumps off: a
# dump of a page process could hold page data from the workspace. An ENV does
# not reach the agent's CLI (runners/cliagent.py builds that environment from
# scratch), so the runner sets both again on the process it starts; these
# cover anything that runs the binary directly, the self-test included.
ENV LIGHTPANDA_DISABLE_TELEMETRY=true \
    LIGHTPANDA_DISABLE_CORE_DUMP=1

# ONE RUN, one layer: the binary for the build's architecture, root-owned and
# 0755 so the agent's uid cannot replace it; the AGPL text and LICENSING.md
# from the tag's own source archive (nothing of Lightpanda's source is kept in
# this repository); the font licence the binary's text rendering embeds; the
# third-party notices beside this file; and SOURCE, naming the tag, the
# commit and the mirrored archive. An architecture with no pin fails here.
RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror \
    --mount=type=bind,source=images/agent-runtime-lightpanda,target=/tmp/swarm-lightpanda-build \
    mkdir -p /tmp/lightpanda \
 && case "${TARGETARCH}" in amd64) \
      curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/lightpanda/lightpanda-x86_64-linux \
        "file:///tmp/tool-mirror/github.com/lightpanda-io/browser/releases/download/${LIGHTPANDA_VERSION}/lightpanda-x86_64-linux" \
      && echo "${LIGHTPANDA_SHA256_AMD64}  /tmp/lightpanda/lightpanda-x86_64-linux" | sha256sum -c - \
      && mv /tmp/lightpanda/lightpanda-x86_64-linux /tmp/lightpanda/lightpanda ;; \
    arm64) \
      curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/lightpanda/lightpanda-aarch64-linux \
        "file:///tmp/tool-mirror/github.com/lightpanda-io/browser/releases/download/${LIGHTPANDA_VERSION}/lightpanda-aarch64-linux" \
      && echo "${LIGHTPANDA_SHA256_ARM64}  /tmp/lightpanda/lightpanda-aarch64-linux" | sha256sum -c - \
      && mv /tmp/lightpanda/lightpanda-aarch64-linux /tmp/lightpanda/lightpanda ;; \
    *) echo "no Lightpanda ${LIGHTPANDA_VERSION} pin for TARGETARCH '${TARGETARCH}'" >&2; exit 1 ;; \
    esac \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/lightpanda/source.tar.gz \
      "file:///tmp/tool-mirror/github.com/lightpanda-io/browser/archive/refs/tags/${LIGHTPANDA_VERSION}.tar.gz" \
 && echo "${LIGHTPANDA_SOURCE_SHA256}  /tmp/lightpanda/source.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/lightpanda/source.tar.gz -C /tmp/lightpanda \
      "browser-${LIGHTPANDA_VERSION}/LICENSE" \
      "browser-${LIGHTPANDA_VERSION}/LICENSING.md" \
      "browser-${LIGHTPANDA_VERSION}/src/rust/render/fonts/LICENSE" \
 && install -d -o root -g root -m 0755 /opt/swarm/lightpanda /usr/share/doc/lightpanda \
 && install -o root -g root -m 0755 /tmp/lightpanda/lightpanda /opt/swarm/lightpanda/lightpanda \
 && install -o root -g root -m 0644 "/tmp/lightpanda/browser-${LIGHTPANDA_VERSION}/LICENSE" /usr/share/doc/lightpanda/LICENSE \
 && install -o root -g root -m 0644 "/tmp/lightpanda/browser-${LIGHTPANDA_VERSION}/LICENSING.md" /usr/share/doc/lightpanda/LICENSING.md \
 && install -o root -g root -m 0644 "/tmp/lightpanda/browser-${LIGHTPANDA_VERSION}/src/rust/render/fonts/LICENSE" /usr/share/doc/lightpanda/FONTS-LICENSE \
 && install -o root -g root -m 0644 /tmp/swarm-lightpanda-build/THIRD_PARTY /usr/share/doc/lightpanda/THIRD_PARTY \
 && printf 'Lightpanda %s, unmodified, licensed AGPL-3.0-only (LICENSE beside this file).\nrelease tag: %s\nupstream commit: %s\nCorresponding Source: %s\nsha256: %s\nupstream repository: github.com/lightpanda-io/browser\n' \
      "${LIGHTPANDA_VERSION}" "${LIGHTPANDA_VERSION}" "${LIGHTPANDA_COMMIT}" "${LIGHTPANDA_SOURCE_URL}" "${LIGHTPANDA_SOURCE_SHA256}" > /usr/share/doc/lightpanda/SOURCE \
 && chmod 0644 /usr/share/doc/lightpanda/SOURCE \
 && rm -rf /tmp/lightpanda

USER swarm:swarm
# NO `WORKDIR /workspace` HERE: the base sets it and this image inherits it.
# Restating it writes BuildKit's empty layer a second time, and GKE does not
# stream an image with a duplicate layer (docs/browser-startup-speed.md §2.4;
# tests/unit/scripts/test_image_streaming.py).
#
# Prove, as the agent's uid and in ONE RUN (two check-only RUNs can write the
# same layer), that the binary is root-owned 0755 where the runner looks,
# reports LIGHTPANDA_VERSION, the notices are in place and no Chromium is
# anywhere in the image. selftest.py lists each check; it is bind-mounted,
# not shipped.
RUN --mount=type=bind,source=images/agent-runtime-lightpanda,target=/tmp/swarm-lightpanda-build \
    scratch="$(mktemp -d)" \
 && TMPDIR="${scratch}" LIGHTPANDA_VERSION="${LIGHTPANDA_VERSION}" LIGHTPANDA_COMMIT="${LIGHTPANDA_COMMIT}" \
      LIGHTPANDA_SOURCE_URL="${LIGHTPANDA_SOURCE_URL}" LIGHTPANDA_SOURCE_SHA256="${LIGHTPANDA_SOURCE_SHA256}" \
      python /tmp/swarm-lightpanda-build/selftest.py \
 && rm -rf "${scratch}"

# The base's, restated because a derived image that sets nothing inherits it
# but a reader of this file should not have to know that.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]
