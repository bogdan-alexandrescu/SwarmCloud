# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# swarm-plugin-bundler -- the image of the Cloud Run job swarm-plugin-bundle,
# a deployment's bundle build (docs/design/managed-plugins.md, section 1.4.2;
# owner decisions of 6 November 2026, Q13, 9 November 2026: the bundle build
# runs in the deployment, at any upstream pin a platform admin applied after
# the diff, and 11 November 2026: as one job execution under the builder
# account). The job's command is `python3 -I /opt/swarm-bundler/swarm-bundle
# job ...` (terraform/infra/plugin-bundles.tf): it checks the execution's
# arguments, discards its environment, runs the recipes below and appends one
# layer per plugin to the deployment's copy of each release image through the
# registry API, with no container daemon, Dockerfile or BuildKit.
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
#                                 writer both recipes end with, and `job`,
#                                 the job's entrypoint
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
# bun.lock, frozen, or the build fails.
#
# The job runs as swarm-bundler (uid 10001), never root: it runs an upstream's
# setup and its dependencies' install scripts, which have no business owning
# the image. (That code can still ask the metadata server for the builder's
# token, which is why the builder holds exactly five grants, section 1.4.2.)
# That user owns exactly the folders a build writes: /opt/swarm/plugins, where
# the recipes install at /opt/swarm/plugins/<name> (the install path the
# release stages use, since gstack's compiled `browse` embeds it);
# /opt/swarm-bundler/bun, where the gstack recipe installs Bun when the step
# passes `--bun-path /opt/swarm-bundler/bun/bun` (the release stages, as root,
# keep the recipe's /usr/local/bin/bun); and /workspace, the working folder
# whose children are the --out folders. Each recipe's scratch folder is
# mktemp's, under /tmp. The recipe digests are unchanged by the user: they hash
# the recipe files, which are the release stages' bytes.
#
# Build from the REPOSITORY ROOT: the recipes' inputs live beside the agent
# images.
#
#     docker build -f images/plugin-bundler/Dockerfile -t swarm-plugin-bundler .
#
# Digests are agent-runtime-base's, so the bundle builds on the Python and Node
# the release image runs.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54
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

# The python:3.11-slim base carries its own setuptools and wheel in
# /usr/local/lib, and the release scan refuses both (jaraco.context, vendored
# in setuptools, and wheel itself). A bundle build builds plugins, never a
# Python package: the recipes' Python is swarm-bundle, gstack-layers, the
# closure walker and `python3 -m zipfile`, standard library only. So they are
# removed rather than upgraded, as in images/swarm-scheduler: an upgrade only
# moves the next advisory further out.
RUN /usr/local/bin/python3 -m pip uninstall --yes setuptools wheel \
 && ! /usr/local/bin/python3 -c "import setuptools" 2>/dev/null \
 && ! /usr/local/bin/python3 -c "import wheel" 2>/dev/null

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

# The unprivileged build user and the folders it owns (the header says why
# each). Everything else, the recipes and their inputs included, stays root's
# and not writable by it.
RUN /usr/sbin/groupadd --gid 10001 swarm-bundler \
 && /usr/sbin/useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin swarm-bundler \
 && install -d -o 10001 -g 10001 -m 0755 /opt/swarm/plugins /opt/swarm-bundler/bun /workspace

ENV PATH=/opt/swarm-bundler/bun:${PATH}

WORKDIR /workspace
USER 10001:10001
