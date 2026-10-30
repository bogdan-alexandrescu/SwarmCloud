# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# swarm-verify -- the verification targets, packaged to run INSIDE the VPC.
#
# Listed in ALL_TARGETS in scripts/build-images.sh.
#
# Build from the REPOSITORY ROOT:
#
#     docker build -f images/swarm-verify/Dockerfile \
#         -t us-central1-docker.pkg.dev/$PROJECT/swarm-images/swarm-verify:$TAG .
#
# WHY THIS IMAGE EXISTS
# ---------------------
# CLAUDE.md requires `make smoke concurrency-test race-test` before claiming a
# change to admission, dispatch or reconciliation is done. On 2026-09-20 all
# three were found to be unable to reach the API at all: swarm-api runs with
# ingress `internal-and-cloud-load-balancing`, which is correct and deliberate
# -- it is what puts the service behind the external ALB and IAP -- and a
# laptop therefore cannot reach the `*.run.app` URL the scripts resolve to.
# The gate had stopped being a gate. See
# docs/audits/2026-09-20/verification-targets-cannot-run.md.
#
# The alternative was to give the scripts an identity that can pass through
# IAP, which needs an OAuth client id and a service account holding
# roles/iap.httpsResourceAccessor. That works from a laptop and not from CI,
# and it means minting a credential whose only purpose is to walk past the
# front door. Running the tests from INSIDE the VPC needs no client, no
# secret, and works identically in CI -- the ingress setting already permits
# internal traffic.
#
# NO GCLOUD IN THIS IMAGE, and this is the second answer to that question.
#
# The first version argued for no gcloud on the strength of the ID token
# alone, without checking what else the scripts called -- they called
# `gcloud run services describe` and `gcloud storage ls`, and all three
# targets failed with "required command not found: gcloud". A container
# problem wearing the costume of a platform problem, which is the exact
# confusion this gate exists to remove.
#
# The second version added the Cloud SDK base, and `make push` refused to
# promote it: that image ships a python-cryptography with unfixed HIGH and
# CRITICAL CVEs. The scan gate was right, and allow-listing it would have
# traded a real security control for convenience in an image that exists to
# enforce correctness.
#
# So the three gcloud calls were replaced with REST, in
# cloud_run_service_uri, gcs_object_count and cloud_run_execution_count
# (scripts/lib/common.sh). Each preserves the distinction that mattered:
# "absent" and "could not look" stay different answers. access_token() and
# id_token() both take their credentials from the metadata server when
# K_SERVICE or CLOUD_RUN_JOB is set, so nothing here needs a key file either.

# Pulled through this project's Docker Hub mirror, never from Docker Hub
# directly (terraform/infra/build_mirror.tf, docs/runbooks/build-mirror.md),
# and by digest: this was the one base here named by tag alone. The digest is
# Docker Hub's alpine:3.20 index, read on 2026-10-12. A pull request's local
# build passes MIRROR_DOCKER_HUB=docker.io instead (scripts/lib/build-mirror.sh).
# No default, so this file names no project (DAYONE-DESAGA-1,
# docs/design/day-one-install.md section 7): scripts/build-images.sh passes
# the mirror of the project it builds for (lib/build-mirror.sh
# mirror_build_args), and the `# syntax=` frontend through the same mirror as
# BUILDKIT_SYNTAX. A `docker build` given neither fails on its first FROM.
ARG MIRROR_DOCKER_HUB=
ARG ALPINE_IMAGE=alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc

FROM ${MIRROR_DOCKER_HUB}/library/${ALPINE_IMAGE}

# bash because every script declares `#!/usr/bin/env bash` and relies on
# arrays and `set -o pipefail`; ash would silently mis-execute them.
# jq because the scripts parse every API response with it.
# curl because that is how they talk to the API and to the metadata server.
# coreutils for the GNU `date`/`timeout` semantics the scripts assume.
# python3 (standard library only, no pip) for scripts/acceptance/pngcheck.py,
# which decodes a browser task's screenshot and asserts it is not blank --
# the check that would have caught every "successful" browser task that
# screenshotted about:blank (owner decision on #358, 2026-09-29). Without it
# the acceptance suite's pixel checks SKIP in the release.
# git because scripts/acceptance/groups/claude-code.sh applies the collected
# patch with `git apply`, falling back to `patch -p1` only when git is
# missing. Without either binary that fallback itself fails closed with
# "patch: command not found", so the check never gets to say whether the
# agent's patch was right -- release run 36738557395 (31be425) caught this:
# the fixture patch was byte-correct and still reported FAIL. This is not the
# gcloud/Cloud-SDK tradeoff above: git alone, with no Python dependency chain,
# carries no comparable CVE surface.
# sed (GNU) because `redact` in scripts/lib/common.sh is one sed -E program
# with `#` delimiters and back-references that busybox sed refuses to compile
# ("sed: unmatched '#'"). Without it every acceptance reason piped through
# redact came out EMPTY: 13 of the 14 failed live-browser runs read on
# 2026-10-14 printed that error once per check (lane SPEED-C2). It fails
# closed -- nothing leaked, the reason was lost -- which is still a check
# that cannot say why it failed.
RUN apk add --no-cache bash curl jq coreutils ca-certificates python3 git sed

WORKDIR /swarm

# Only what the verification targets need. The application source is not here:
# this image tests a DEPLOYED platform over HTTP, it does not import it.
COPY scripts/ /swarm/scripts/
COPY Makefile /swarm/Makefile
# The acceptance fixtures (#628): the claude-code collect check applies the
# collected patch to calc.py as the release deployed it. The sandbox those
# tasks clone is private and this job holds no GitHub token, so the file
# cannot be read from there; sandbox-sync.sh puts this same commit's copy on
# the sandbox's main before the suite runs, so the two are the same bytes.
COPY tests/acceptance/fixtures/ /swarm/tests/acceptance/fixtures/
# The live-browser group's scripted viewer (scripts/acceptance/groups/
# live-browser.sh runs it with python3): standard library only, so it needs
# nothing the image does not already carry.
COPY tests/acceptance/test_live_browser.py /swarm/tests/acceptance/test_live_browser.py
# The git-tools group's prompts and judge (scripts/acceptance/groups/
# git-tools.sh runs it with python3 for every prompt and verdict): standard
# library only. Missing from the image from PR 1269 until 2026-10-15, every
# git-tools check failed "the judge wrote no verdict" with an empty prompt.
COPY tests/acceptance/test_runner_git_tools.py /swarm/tests/acceptance/test_runner_git_tools.py

# The mcp group's judge (lane MCP-ACCEPT, part of #1433; scripts/acceptance/
# groups/mcp.sh runs it to mint, build the request bodies and judge), and the
# echo MCP server the dev-only Cloud Run service swarm-mcp-echo runs FROM THIS
# IMAGE (terraform/infra/mcp_test_server.tf overrides the entrypoint with
# python3 and this path): standard library only, both.
COPY tests/acceptance/test_caller_mcp.py /swarm/tests/acceptance/test_caller_mcp.py
COPY tests/acceptance/mcp_echo_server/ /swarm/tests/acceptance/mcp_echo_server/

# The frozen identity module, and nothing else of the application: common.sh's
# personal_tenant_id imports its tenant_id_for_user to derive the caller's own
# tenant (u-sw-c90291 for swarm-verify), where the workflow group's await-input
# checks run (HW-ACCEPT-ID, owner decision 2026-10-19). Imported rather than
# restated in shell, because the slug appends a digest of the full principal
# and a copy would derive an id nobody resolves to. Standard library only.
COPY apps/common/swarm_common/identity.py /swarm/apps/common/swarm_common/identity.py

# The hourly doctor (lane DAYONE-DOCTOR-1): terraform/infra/doctor.tf runs
# `python -m swarm_install.scheduled_doctor` here as swarm-doctor. It needs the
# installer's package and the release's manifest (the probes it runs), and the
# manifest model's two libraries, from Alpine's own packages rather than pip
# (the scan gate above). Its probes call Google's REST APIs with the standard
# library and the metadata server's token: no Google client library, no gcloud.
# `python` because that is the job's command; Alpine ships only `python3`.
RUN apk add --no-cache py3-yaml py3-pydantic \
    && ln -sf /usr/bin/python3 /usr/bin/python
COPY apps/swarm-install/swarm_install/ /swarm/apps/swarm-install/swarm_install/
COPY install/ /swarm/install/
ENV PYTHONPATH=/swarm/apps/swarm-install

# Fail loudly at build time rather than at 3am if a script lost its bit.
RUN chmod +x /swarm/scripts/*.sh /swarm/scripts/lib/*.sh 2>/dev/null || true

# SWARM_ASSUME_YES is safe here and nowhere else: this image runs only the
# read-and-submit verification targets. Anything destructive requires a TYPED
# confirmation that deliberately ignores this variable (scripts/lib/common.sh),
# so setting it cannot arm a destroy.
ENV SWARM_ASSUME_YES=1

# Overridden per execution. The default runs the full gate.
ENTRYPOINT ["/bin/bash", "-lc"]
CMD ["scripts/smoke-test.sh"]
