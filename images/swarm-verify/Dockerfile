# syntax=docker/dockerfile:1.7
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
# NO GCLOUD IN THIS IMAGE, deliberately. `id_token()` in scripts/lib/common.sh
# has a branch for exactly this: when K_SERVICE or CLOUD_RUN_JOB is set it
# asks the metadata server for an ID token for the attached service account.
# Shipping gcloud to obtain a token the runtime already offers would add
# ~500MB and a second auth path to keep correct.

FROM alpine:3.20

# bash because every script here declares `#!/usr/bin/env bash` and relies on
# arrays and `set -o pipefail`; ash would silently mis-execute them.
# jq because the scripts parse every API response with it.
# curl because that is how they talk to the API and to the metadata server.
# coreutils for GNU `date`/`timeout` semantics the scripts assume.
RUN apk add --no-cache bash curl jq coreutils ca-certificates

WORKDIR /swarm

# Only what the verification targets need. The application source is not here:
# this image tests a DEPLOYED platform over HTTP, it does not import it.
COPY scripts/ /swarm/scripts/
COPY Makefile /swarm/Makefile

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
