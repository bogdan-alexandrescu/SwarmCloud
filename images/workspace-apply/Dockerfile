# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# workspace-apply -- the image of the personal-workspace job, and the code it
# runs (docs/workspaces.md §2.1-2.2; lane W4 of #847 made the toolbox, lane
# W6b put the code in it).
#
# Listed in ALL_TARGETS in scripts/build-images.sh and scripts/push-images.sh,
# so every push to main builds it, the release's trivy gate scans it, and
# push-images.sh promotes its digest with every other image.
#
# WHAT RUNS ON IT. The Cloud Run job `swarm-workspace-apply`
# (terraform/bootstrap/), as swarm-workspace-deployer, with the fixed command
# `python3 -I /opt/swarm/entry.py <w-id> <mode>` (WD2 re-decided 2026-10-10:
# a Cloud Run job replaces the Cloud Build trigger). entry.py is step 0, the
# scrub: it discards every environment variable the execution was handed and
# `execve`s scripts/workspace-apply.sh (steps 1 to 3: validate, install the
# call guard, run `scripts/register-tenant.sh --workspace`) with a fixed one.
# The job names this image BY DIGEST (workspace_apply_image in the bootstrap),
# so a new build of it reaches that identity only through the owner's
# bootstrap apply: the image is part of what bounds the identity (§2.4 R1),
# and .github/CODEOWNERS names the owner on this file for that reason.
#
# THE CODE IS COPIED IN. A Cloud Build run was handed a checkout of main; a
# Cloud Run execution is handed nothing but its image. So the image carries,
# read-only and root-owned under /opt/swarm, exactly what the build read from
# its checkout, in the repository's own layout (common.sh finds REPO_ROOT two
# directories above scripts/lib, render.py finds the packages under apps/):
#
#   entry.py                          step 0
#   scripts/workspace-apply.sh        steps 1 to 3
#   scripts/register-tenant.sh        the script (A1-A9)
#   scripts/lib/                      the guard, its rules, cases and shims,
#                                     common.sh
#   kubernetes/                       apply.sh and render.py (A7, A9)
#   apps/common/swarm_common          the frozen contract the script and
#                                     render.py import
#   apps/quota-broker/quota_broker    render.py's account-label rule
#   apps/scheduler/scheduler          render.py's Job deadline and disk size.
#                                     docs/workspaces.md §2.2 lists the first
#                                     two packages only; render.py imports
#                                     this one at module level too, so A7
#                                     cannot run without it (measured
#                                     2026-10-10: ModuleNotFoundError).
#   terraform/modules/service_account_ids/main.tf
#                                     where common.sh's platform_account_email
#                                     reads the scheduler's and the
#                                     reconciler's account ids, for the guard's
#                                     expectation (A4's act-as members). Also
#                                     missing from §2.2's list; without it the
#                                     guard refuses every run.
#
# That set is measured, not guessed: on 2026-10-10 the W6 test harness
# (tests/unit/scripts/test_register_tenant_workspace.py) ran a full --mode
# create and --mode limits from a read-only copy of exactly these paths, as a
# non-root user, and both reached `ready`.
#
# All three packages import only the standard library at module level, so the
# image's Debian python3 runs them with no install.
#
# WHY NOT THE STOCK IMAGE ALONE. W0 (2026-10-08) found that
# gcr.io/google.com/cloudsdktool/google-cloud-cli carries none of kubectl, jq,
# curl, uv or gke-gcloud-auth-plugin, and on amd64 not even a python3 on PATH:
# its gcloud runs on a bundled interpreter. The job needs every one of them:
# register-tenant.sh requires gcloud, jq, curl and python3; the namespace step
# (A7, kubernetes/apply.sh) needs kubectl and, to authenticate it to GKE, the
# auth plugin.
#
# THE BUILD CONTEXT. The repository's .dockerignore excludes scripts/,
# kubernetes/ and terraform/ on purpose (no application image carries operational tooling),
# so this Dockerfile is built from a context that holds them, assembled by
# images/workspace-apply/cloudbuild.yaml as swarm-verify's is
# (images/swarm-verify/cloudbuild.yaml), never by weakening the root rule. A
# COPY added below must be added to that assembly too; a test holds the two
# together (test_build_images_pr_check.py).

# google-cloud-cli 591.0.0, the -stable (Debian trixie) variant, by digest of
# its multi-arch index: `HEAD
# https://gcr.io/v2/google.com/cloudsdktool/google-cloud-cli/manifests/591.0.0-stable`,
# docker-content-digest, read 2026-10-31. 591.0.0 because it is the gcloud
# agent-runtime-base pins, so the auth plugin's .deb pin below is the one that
# image already verified against this exact gcloud. Raised from 587.0.0 on
# 2026-10-31 (lane IMAGE-SCAN-NOV): 591.0.0's gcloud-crc32c is go1.27.2, which
# fixes CVE-2026-78667, CVE-2026-78669 and CVE-2026-97031 that 587.0.0's
# (go1.26.6) carried; `trivy image` over both digests found 591.0.0's
# HIGH/CRITICAL fixable findings a subset of 587.0.0's.
ARG GCLOUD_IMAGE=gcr.io/google.com/cloudsdktool/google-cloud-cli:591.0.0-stable@sha256:d3f74bb21280869467e4e901c66280d4610e0bda81bab1cd7d9c112ccd1d2276
# The uv every other image copies in, for the same reason they give
# (images/swarm-api/Dockerfile): 0.12.15 is the release whose static Rust
# dependencies pass the promote scan.
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
ARG MIRROR_GHCR=

FROM ${MIRROR_GHCR}/${UV_IMAGE} AS uvbin

FROM ${GCLOUD_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# bash with pipefail: every `echo <sha> <file> | sha256sum -c -` below is a
# pipe, and a pipe whose first command failed must not pass on the last one's.
# EVERY DOWNLOAD HERE RETRIES (2026-10-14, PR 1281's flake table): `curl -fsSL
# --retry 5 --retry-all-errors --retry-connrefused`, 1+2+4+8+16 s of backoff at
# most. --retry-all-errors is what makes an HTTP error (curl's exit 22, which
# failed one main build of agent-runtime-base in run 38245452463) retry at all. A
# file that is really gone still fails, ~31 s later, and every download is
# still checked against its pinned checksum after it lands.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# jq, curl and python3 from Debian, with the security updates of the day.
#
# python3 is needed twice: register-tenant.sh asks the frozen contract for
# names with `python3 - <<PY`, and gcloud itself runs on it once the bundled
# interpreter is gone (below). Debian trixie's python3 is 3.13, inside the
# 3.9-3.14 range gcloud 591's launcher accepts.
#
# A FRESH LAYER, EVERY BUILD AND EVERY RELEASE (owner decision 2026-10-14).
# Releases 38535875750 and 38540997006 refused this image over curl and
# libcurl4t64 8.14.1-2+deb13u5 (CVE-2026-8286, HIGH, fixed in +deb13u7): this
# RUN had last run on 2026-10-13, and main's incremental build kept re-tagging
# that digest because none of its inputs changed. scripts/build-images.sh now
# rebuilds it in every main build and every release, and passes the build's
# name as APT_REFRESH, named here so no layer cache can serve an older upgrade.
#
# THE ASSERTION. After the upgrade and the installs, a simulated dist-upgrade
# must have nothing left to do. `apt-get upgrade` keeps back a package whose
# fix needs a new dependency, and that package would ship at its vulnerable
# version and fail the release's promote scan 45 minutes later; here it fails
# this build, naming the package. No version is pinned and nothing is ignored.
ARG APT_REFRESH=unset
RUN echo "apt layer of ${APT_REFRESH}" \
 && apt-get update -qq \
 && apt-get upgrade -y -qq \
 && apt-get install --no-install-recommends -y -qq \
      ca-certificates \
      curl \
      jq \
      python3 \
 && apt-get -s dist-upgrade >/tmp/apt-pending.txt \
 && if grep '^Inst ' /tmp/apt-pending.txt >&2; then \
      echo "workspace-apply: the packages above still have a newer Debian version after apt-get upgrade (APT_REFRESH=${APT_REFRESH}); the promote scan would refuse this image" >&2; \
      exit 1; \
    fi \
 && rm -f /tmp/apt-pending.txt \
 && dpkg-query -W curl libcurl4t64 \
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
# google-cloud-cli-gke-gcloud-auth-plugin 591.0.0-0 in
# https://packages.cloud.google.com/apt/dists/cloud-sdk/main/binary-amd64/Packages.
# The .deb declares no Depends, so it installs beside an SDK this image did not
# install through dpkg. Named GKE_PLUGIN_*, not GKE_AUTH_PLUGIN_*: Trivy DS-0031
# reads "AUTH" in an ARG name as a secret.
#
# kubectl 1.36.6: the version agent-runtime-base pins and docs/versions.md
# names for operators, one minor ahead of the 1.35 control plane as kubectl's
# skew policy allows. The static binary from dl.k8s.io; checksum from
# https://dl.k8s.io/release/v1.36.6/bin/linux/amd64/kubectl.sha256.
#
# amd64 only, like every image here: Cloud Build's default pool is amd64, and
# so is Cloud Run.
ARG GCLOUD_VERSION=591.0.0
ARG GKE_PLUGIN_POOL_ID=0f14e7e21ca8f8a0ba22a1f83c4b26b1
ARG GKE_PLUGIN_SHA256=b46d7490fd2b596e3b58b0d94aa8f0328c288fcfa517a6f6b8a01e704b5760c7
ARG KUBECTL_VERSION=1.36.6
ARG KUBECTL_SHA256=29818b6729dc9262b692307470d0558d027725e738be5dd47c760261868a2634
RUN mkdir -p /tmp/toolbox \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/gke-gcloud-auth-plugin.deb \
      "https://packages.cloud.google.com/apt/pool/cloud-sdk/google-cloud-cli-gke-gcloud-auth-plugin_${GCLOUD_VERSION}-0_amd64_${GKE_PLUGIN_POOL_ID}.deb" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/toolbox/kubectl \
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

# THE CODE, read-only and root-owned under /opt/swarm (§2.2). Copied with
# their repository modes, so the scripts keep their exec bits, then every write
# bit is taken away; the job runs as an unprivileged user (below), for whom
# that is real.
COPY images/workspace-apply/entry.py /opt/swarm/entry.py
COPY scripts/workspace-apply.sh /opt/swarm/scripts/workspace-apply.sh
COPY scripts/register-tenant.sh /opt/swarm/scripts/register-tenant.sh
COPY scripts/lib/ /opt/swarm/scripts/lib/
COPY kubernetes/ /opt/swarm/kubernetes/
COPY apps/common/swarm_common/ /opt/swarm/apps/common/swarm_common/
COPY apps/quota-broker/quota_broker/ /opt/swarm/apps/quota-broker/quota_broker/
COPY apps/scheduler/scheduler/ /opt/swarm/apps/scheduler/scheduler/
COPY terraform/modules/service_account_ids/main.tf /opt/swarm/terraform/modules/service_account_ids/main.tf

# Bytecode compiled now, so no run writes any beside the code; then root owns
# all of it and nobody may write it.
#
# /opt/swarm/build exists, empty, for one reason: load_env
# (scripts/lib/common.sh) runs `mkdir -p "${REPO_ROOT}/build"` every time, and
# on a read-only tree that only succeeds if the directory is already there.
# Nothing on the workspace path writes into it.
RUN find /opt/swarm -name __pycache__ -prune -exec rm -rf {} + \
 && mkdir -p /opt/swarm/build \
 && python3 -m compileall -q /opt/swarm \
 && chown -R root:root /opt/swarm \
 && chmod -R a-w /opt/swarm \
 && test -x /opt/swarm/scripts/workspace-apply.sh \
 && test -x /opt/swarm/scripts/register-tenant.sh \
 && test -x /opt/swarm/scripts/lib/workspace-guard.sh \
 && test -x /opt/swarm/scripts/lib/guard-bin/gcloud \
 && test -x /opt/swarm/scripts/lib/guard-bin/kubectl \
 && test -x /opt/swarm/scripts/lib/guard-bin/curl \
 && test -x /opt/swarm/kubernetes/apply.sh

# THE CODE'S SELF-TEST, on this image, so a layout or a tool that does not
# hold fails this build (and the pull request's build-check) rather than the
# first person's onboarding:
#   * every tool the job needs resolves on entry.py's FIXED PATH, read from
#     entry.py itself (runpy, so its main does not run), in an environment
#     with nothing else in it -- as the job will see it;
#   * the scripts parse;
#   * render.py imports its three packages from this layout and renders a
#     tenant (offline: it marks the render not-for-apply and says so);
#   * the call guard judges its own cases (scripts/lib/workspace-guard-cases.json)
#     on this image, as every execution does again before it runs.
RUN set -e \
 && safe_path="$(python3 -I -c 'import runpy; print(runpy.run_path("/opt/swarm/entry.py")["SAFE_PATH"])')" \
 && env -i PATH="${safe_path}" bash -c 'for tool in bash timeout gcloud gke-gcloud-auth-plugin kubectl jq curl python3 uv; do command -v "${tool}" >/dev/null || { echo "${tool}: not on entry.py SAFE_PATH" >&2; exit 1; }; done' \
 && bash -n /opt/swarm/scripts/workspace-apply.sh \
 && bash -n /opt/swarm/scripts/register-tenant.sh \
 && bash -n /opt/swarm/scripts/lib/workspace-guard.sh \
 && python3 -I /opt/swarm/kubernetes/render.py tenant --tenant u-selftest \
      --gsa swarm-agent-worker-u-selftest@example-project.iam.gserviceaccount.com >/dev/null \
 && env -i PATH="${safe_path}" HOME=/tmp TMPDIR=/tmp /opt/swarm/scripts/lib/workspace-guard.sh self-test >/dev/null

# NO ENTRYPOINT THE JOB RELIES ON. The job's `command` (W4b, terraform/bootstrap)
# is `python3 -I /opt/swarm/entry.py`, fixed in the job spec, where no run
# override reaches; an execution's two arguments are its `args`. The stock
# image's entrypoint configures gcloud for interactive use, so it is cleared,
# and CMD is empty so a job without arguments passes entry.py none (and is
# refused) rather than the stock image's default.
ENTRYPOINT []
CMD []

# An unprivileged user, so /opt/swarm is read-only to the run in fact and not
# only by its mode bits. Nothing the job does needs root: entry.py makes its
# private HOME and the guard's 0600 expectation file under /tmp, in the
# execution's in-memory filesystem, and the identity is the job's, from the
# metadata server. The image holds no credential.
USER 65534:65534
