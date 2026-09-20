#!/usr/bin/env bash
#
# `terraform plan` for this environment, with the image tag that is ALREADY
# RUNNING.
#
# WHY THIS SCRIPT EXISTS, and it is not a convenience. `image_tag` defaults to
# "bootstrap" and its description says "used on the FIRST apply only; the deploy
# pipeline owns the image afterwards (see ignore_changes in the cloud_run
# modules)". That description is wrong about the mechanism, and the gap cost a
# live outage on 2026-09-20.
#
# Neither module ignores the image, deliberately and for good reasons stated in
# both: terraform IS the deploy mechanism for services, and for JOBS an ignored
# image is an image nothing ever compares, which would let a compromised
# dispatcher repoint a tenant's job at an attacker image unnoticed.
#
# So `image_tag` is applied on EVERY apply, and a plan that does not set it
# plans to move every service and every job to `agent-runtime-base:bootstrap`,
# which does not exist. `make tf-plan && make tf-apply` -- which is exactly what
# the documented `make infra` target runs -- therefore repointed all ten worker
# jobs at a missing image and broke every dispatch until the next real deploy.
#
# The fix is not a new default. It is to plan against what is actually running,
# so an infrastructure plan shows infrastructure changes and no image churn at
# all. A deploy still passes its own promoted tag; this only stops a plan that
# was never about images from silently becoming one.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
. "${SCRIPT_DIR}/lib/common.sh"

load_env
require_cmd jq curl

# The tag every service and job should keep. Read from the RUNNING service
# rather than from a build manifest, because the manifest records what was last
# built on this machine and the question here is what is deployed -- which may
# have come from CI, from someone else, or from a rollback.
current_image_tag() {
  local image=""
  image="$(cloud_run_image "${API_SERVICE}")" || true
  if [[ -n "${image}" && "${image}" == *:* ]]; then
    printf '%s' "${image##*:}"
    return 0
  fi

  # Nothing deployed yet. A build manifest from this machine is the next best
  # answer, and on a genuinely fresh project there is no answer at all -- which
  # is the one case where "bootstrap" is correct, because nothing exists to
  # break.
  local manifest="${REPO_ROOT}/build/deployed-images-${ENVIRONMENT}.json"
  if [[ -f "${manifest}" ]]; then
    local tag
    tag="$(jq -r '.tag // empty' "${manifest}" 2>/dev/null || true)"
    if [[ -n "${tag}" ]]; then
      printf '%s' "${tag}"
      return 0
    fi
  fi
  return 1
}

TAG=""
if TAG="$(current_image_tag)" && [[ -n "${TAG}" ]]; then
  ok "planning against the running image tag ${TAG}"
else
  TAG="bootstrap"
  warn "nothing is deployed yet; planning with image_tag=bootstrap"
  warn "that tag does not exist in Artifact Registry -- apply this plan only on"
  warn "a fresh project, and follow it with 'make build push deploy'"
fi

mkdir -p "${REPO_ROOT}/build"
tf_var_args
step "terraform plan (${ENVIRONMENT}, image_tag=${TAG})"
tf -chdir="$(tf_root)" plan \
  -input=false \
  -lock-timeout=120s \
  "${TF_VAR_ARGS[@]}" \
  -var="image_tag=${TAG}" \
  -out="${REPO_ROOT}/build/${ENVIRONMENT}.tfplan"
ok "plan written to build/${ENVIRONMENT}.tfplan"
