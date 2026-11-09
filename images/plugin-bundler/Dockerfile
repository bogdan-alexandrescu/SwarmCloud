# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# swarm-plugin-bundler -- the image a deployment's bundle build runs
# (docs/design/managed-plugins.md, section 1.4.2; owner decisions of
# 6 November 2026, Q13, and 9 November 2026: the bundle build runs in the
# deployment, on Cloud Build, from the deployment's copy of this signed release
# image, at any upstream pin a platform admin applied after the diff).
#
# gstack and security-audit are managed plugins whose versions are BUILT into
# an image version, not unpacked per attempt: gstack's compiled `browse`
# embeds its install path, and security-audit's validators must sit in a
# read-only root filesystem where the agent cannot edit them before the review
# runs them. This image holds what builds them, and nothing that reaches an
# agent:
#
#   recipes/gstack/build          gstack and Bun at their pins, `./setup`,
#                                 gstack-layers check and stage
#   recipes/security-audit/build  the skill folder and LICENSE, its own
#                                 validator tests run on the staged copy
#   swarm-bundle                  the pins check and the .swarm-bundle.json
#                                 writer both recipes end with
#
# plus the inputs the gstack recipe names: gstack-layers, the closure walker
# (scripts/gstack_packaging_measure.py) and both checked-in lists. The release
# build's agent-runtime-base and agent-runtime-browser stages bind-mount the
# same files from the repository and run the same recipes at the default pins
# (images/agent-runtime-base/bundled-plugins.json, copied here as the pins a
# fresh build starts from), so a recipe digest the bundler writes equals the
# release image's for the same recipe.
#
# A build runs a recipe with a pins file of bundled-plugins.json's shape and a
# mirror folder holding the pinned objects at their object paths (the source
# archive the intake job checked, and Bun's zip), and writes the tree to --out.
# Dependencies are fetched only through the deployment's mirror repositories,
# never straight from a public registry (owner, 9 November 2026, Q19): the
# pinned objects come from the mirror folder, and gstack's npm dependencies
# from the registry the bundle build passes as the recipe's --registry, the
# deployment's mirror repository, which lane 15 (terraform/infra) provides and
# lane 16 passes; until then this image ships the argument, not the
# repository. Without --registry, as in the release build's stages, Bun reads
# its default registry. Either way gstack installs only at the archive's
# bun.lock, frozen, or the build fails. The recipes install at
# /opt/swarm/plugins/<name> and Bun at /usr/local/bin/bun, which is why a build
# step runs this image as root: it is a Cloud Build step, never a pod, and it
# holds no credential (its service account's token is Cloud Build's own).
#
# Build from the REPOSITORY ROOT: the recipes' inputs live beside the agent
# images.
#
#     docker build -f images/plugin-bundler/Dockerfile -t swarm-plugin-bundler .
#
# Digests are agent-runtime-base's, so the bundle builds on the Python and Node
# the release image runs.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553
# Every base is pulled through the project's Artifact Registry mirror
# (scripts/lib/build-mirror.sh mirror_build_args); no default, so this file
# names no project.
ARG MIRROR_DOCKER_HUB=

FROM ${MIRROR_DOCKER_HUB}/library/${NODE_IMAGE} AS nodebin

FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE} AS runtime

# curl reads the mirror folder (file://), git checks an archive's commit in its
# pax header; both recipes need them, as the base image's stages do.
# `apt-get upgrade`: the release scans every image it promotes, and the pinned
# base's packages age between digest bumps (APT_REFRESH, as every image here).
ARG APT_REFRESH=unset
RUN echo "apt layer of ${APT_REFRESH}" \
 && apt-get update \
 && apt-get upgrade --no-install-recommends -y \
 && apt-get install --no-install-recommends -y ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# Node runs security-audit's own validator tests on the staged copy.
COPY --from=nodebin /usr/local/bin/node /usr/local/bin/node

COPY images/plugin-bundler/swarm-bundle /opt/swarm-bundler/swarm-bundle
COPY images/plugin-bundler/recipes/ /opt/swarm-bundler/recipes/
COPY images/agent-runtime-base/gstack-layers /opt/swarm-bundler/inputs/gstack-layers
COPY images/agent-runtime-base/gstack-base-files.txt /opt/swarm-bundler/inputs/gstack-base-files.txt
COPY images/agent-runtime-browser/gstack-browser-files.txt /opt/swarm-bundler/inputs/gstack-browser-files.txt
COPY scripts/gstack_packaging_measure.py /opt/swarm-bundler/inputs/gstack_packaging_measure.py
COPY images/agent-runtime-base/bundled-plugins.json /opt/swarm-bundler/bundled-plugins.json

RUN chmod -R go-w /opt/swarm-bundler \
 && /usr/local/bin/node --version \
 && python3 /opt/swarm-bundler/swarm-bundle pin --pins /opt/swarm-bundler/bundled-plugins.json \
      --plugin gstack source.commit

WORKDIR /workspace
