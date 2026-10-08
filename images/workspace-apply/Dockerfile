# syntax=docker/dockerfile:1.7
#
# workspace-apply -- the builder image of the personal-workspace job
# (docs/workspaces.md §2.2, lane W4 of #847).
#
# Listed in ALL_TARGETS in scripts/build-images.sh and scripts/push-images.sh,
# so every push to main builds it, the release's trivy gate scans it, and
# push-images.sh promotes its digest with every other image.
#
# WHAT RUNS ON IT. The Cloud Build trigger `swarm-workspace-apply`
# (terraform/bootstrap/workspace_deployer.tf) runs
# scripts/cloudbuild/workspace-apply.yaml, whose three steps -- validate,
# install the call guard, run `scripts/register-tenant.sh --workspace` -- all
# run in this one image, as swarm-workspace-deployer. The trigger names this
# image BY DIGEST (var.workspace_apply_builder_image there), so a new build of
# it reaches that identity only through the owner's bootstrap apply: the image
# is part of what bounds the identity (§2.4 R1), like the guard and the script.
#
# WHY NOT THE STOCK IMAGE ALONE. W0 (2026-10-08) found that
# gcr.io/google.com/cloudsdktool/google-cloud-cli carries none of kubectl, jq,
# curl, uv or gke-gcloud-auth-plugin, and on amd64 not even a python3 on PATH:
# its gcloud runs on a bundled interpreter. The job needs every one of them:
# register-tenant.sh requires gcloud, jq, curl and python3; the namespace step
# (A7, kubernetes/apply.sh) needs kubectl and, to authenticate it to GKE, the
# auth plugin.
#
# Build from the REPOSITORY ROOT, as every image here is:
#
#     docker build -f images/workspace-apply/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/workspace-apply:$TAG .
#
# It copies nothing from the repository. The job's code (the guard, the script,
# kubernetes/) is the checkout of main Cloud Build hands every step in
# /workspace, never a copy baked in here, so the code that runs is always the
# code the trigger built.

# google-cloud-cli 587.0.0, the -stable (Debian trixie) variant, by digest of
# its multi-arch index: `HEAD
# https://gcr.io/v2/google.com/cloudsdktool/google-cloud-cli/manifests/587.0.0-stable`,
# docker-content-digest, read 2026-10-08. 587.0.0 because it is the gcloud
# agent-runtime-base pins, so the auth plugin's .deb pin below is the one that
# image already verified against this exact gcloud.
ARG GCLOUD_IMAGE=gcr.io/google.com/cloudsdktool/google-cloud-cli:587.0.0-stable@sha256:73a97b855d386c74bee3facd59ccc261006c55a9812b7913fa6b93816010a1bc
# The uv every other image copies in, for the same reason they give
# (images/swarm-api/Dockerfile): 0.12.15 is the release whose static Rust
# dependencies pass the promote scan.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3

FROM ${UV_IMAGE} AS uvbin

FROM ${GCLOUD_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# bash with pipefail: every `echo <sha> <file> | sha256sum -c -` below is a
# pipe, and a pipe whose first command failed must not pass on the last one's.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# jq, curl and python3 from Debian, with the security updates of the day.
#
# python3 is needed twice: register-tenant.sh asks the frozen contract for
# names with `python3 - <<PY`, and gcloud itself runs on it once the bundled
# interpreter is gone (below). Debian trixie's python3 is 3.13, inside the
# 3.9-3.14 range gcloud 587's launcher accepts.
RUN apt-get update -qq \
 && apt-get upgrade -y -qq \
 && apt-get install --no-install-recommends -y -qq \
      ca-certificates \
      curl \
      jq \
      python3 \
 && rm -rf /var/lib/apt/lists/*

# THE BUNDLED INTERPRETER COMES OUT, as it does in agent-runtime-base. gcloud
# 587.0.0's platform/bundledpythonunix carried seven fixable HIGHs in its
# site-packages (cryptography, urllib3, msgpack, setuptools; the list and the
# scan are in images/agent-runtime-base/Dockerfile), which the release's promote
# scan refuses -- and a refused image would hold every release, not just this
# job. With it gone gcloud takes the interpreter CLOUDSDK_PYTHON names, and
# runs it with -S, so nothing on that interpreter's path loads into it.
ENV CLOUDSDK_PYTHON=/usr/bin/python3
RUN rm -rf /usr/lib/google-cloud-sdk/platform/bundledpythonunix \
 && test ! -e /usr/lib/google-cloud-sdk/platform/bundledpythonunix

# The GKE auth plugin and kubectl, at the pins agent-runtime-base verified.
#
# GKE_PLUGIN_*: the Filename and SHA256 of
# google-cloud-cli-gke-gcloud-auth-plugin 587.0.0-0 in
# https://packages.cloud.google.com/apt/dists/cloud-sdk/main/binary-amd64/Packages.
# The .deb declares no Depends, so it installs beside an SDK this image did not
# install through dpkg. Named GKE_PLUGIN_*, not GKE_AUTH_PLUGIN_*: Trivy DS-0031
# reads "AUTH" in an ARG name as a secret.
#
# kubectl 1.36.5: the version agent-runtime-base pins and docs/versions.md
# names for operators, one minor ahead of the 1.35 control plane as kubectl's
# skew policy allows. The static binary from dl.k8s.io; checksum from
# https://dl.k8s.io/release/v1.36.5/bin/linux/amd64/kubectl.sha256.
#
# amd64 only, like every image here: Cloud Build's default pool is amd64.
ARG GCLOUD_VERSION=587.0.0
ARG GKE_PLUGIN_POOL_ID=207adf5dccdc107f7d16bf1e01bd1398
ARG GKE_PLUGIN_SHA256=c572f6cf9b401820032e8459b2bade792442dfbbce30e53d3b1b0d7be1bf9e24
ARG KUBECTL_VERSION=1.36.5
ARG KUBECTL_SHA256=33bc88a24c3b09cf55bfd59e6ca977881b61fc3f25ab65bc80c65b430248648e
RUN mkdir -p /tmp/toolbox \
 && curl -fsSL -o /tmp/toolbox/gke-gcloud-auth-plugin.deb \
      "https://packages.cloud.google.com/apt/pool/cloud-sdk/google-cloud-cli-gke-gcloud-auth-plugin_${GCLOUD_VERSION}-0_amd64_${GKE_PLUGIN_POOL_ID}.deb" \
 && curl -fsSL -o /tmp/toolbox/kubectl \
      "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && echo "${GKE_PLUGIN_SHA256}  /tmp/toolbox/gke-gcloud-auth-plugin.deb" | sha256sum -c - \
 && echo "${KUBECTL_SHA256}  /tmp/toolbox/kubectl" | sha256sum -c - \
 && dpkg -i /tmp/toolbox/gke-gcloud-auth-plugin.deb \
 && install -o root -g root -m 0755 /tmp/toolbox/kubectl /usr/local/bin/kubectl \
 && rm -rf /tmp/toolbox

COPY --from=uvbin /uv /usr/local/bin/uv

# THE SELF-TEST. No `| grep -q` or `| head` here: under pipefail the writer
# dies of SIGPIPE when the reader stops early, and the build fails for nothing.
# Every tool the job needs answers, from PATH, on the
# interpreter that will run it -- so a pin that stops resolving, or a gcloud
# that cannot start without its bundled Python, fails this build (and the pull
# request's build-check) rather than the first person's onboarding.
RUN set -e \
 && bash --version \
 && gcloud --version \
 && gcloud info >/tmp/gcloud-info.txt \
 && grep -F 'Python Location: [/usr/bin/python3' /tmp/gcloud-info.txt \
 && rm -f /tmp/gcloud-info.txt \
 && gke-gcloud-auth-plugin --version \
 && kubectl version --client \
 && jq --version \
 && curl --version \
 && python3 --version \
 && uv --version

# The stock image's entrypoint configures gcloud for interactive use; a build
# step runs its own script, so nothing should run first.
ENTRYPOINT []
CMD ["bash"]

# Root, deliberately, as every Cloud Build builder runs: Cloud Build mounts
# /workspace (the checkout) and the build's volumes root-owned, and the steps
# write the guard's 0600 expectation file into one of them. The image holds no
# credential; the identity is the build's, from the metadata server.
USER root
