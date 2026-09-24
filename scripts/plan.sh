#!/usr/bin/env bash
#
# `terraform plan` for this environment, with the images that are ALREADY
# DEPLOYED -- by digest.
#
# WHY THIS SCRIPT EXISTS, and it is not a convenience. terraform/infra applies
# its image input on EVERY apply: neither cloud_run module ignores the image,
# deliberately (terraform IS the deploy mechanism for services, and for JOBS an
# ignored image is one nothing ever compares, which would let a compromised
# dispatcher repoint a tenant's job unnoticed). So a plan that does not say
# which images to keep plans to move all of them.
#
# That cost a live outage on 2026-09-20, when the input was a tag defaulting to
# "bootstrap": `make tf-plan && make tf-apply` repointed all ten worker jobs at
# an image that did not exist and broke every dispatch until the next deploy.
#
# The input is now `image_refs`, one digest per image, and terraform refuses to
# plan while any image has none -- so the outage cannot recur as a quiet image
# change; it would be a plan that fails. This script supplies the digests
# terraform last applied (scripts/lib/image-refs.sh --applied), so an
# infrastructure plan shows infrastructure changes and no image churn at all.
# A deploy passes the promotion manifest's digests instead, through
# scripts/lib/deploy.sh.
#
# ON A FRESH PROJECT there is nothing to pin: no image has been built, because
# the registry images are pushed to is itself created by this terraform root.
# Then, and only then, this plans the registry alone (`-target`), so that
# `make tf-apply` creates it and `make build push deploy` can follow. The
# services and jobs are planned by that deploy, with real digests.
#
# "Fresh" is terraform STATE's answer (image-refs.sh exit 3): no registry in
# state, or a registry with nothing pushed to it yet. It used to be the
# registry's answer. On a fresh project the registry does not exist, so the
# tag listing failed NOT_FOUND, which reads as an unreadable registry (exit 1),
# and this script refused to plan the bootstrap described above.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
. "${SCRIPT_DIR}/lib/common.sh"

load_env
require_cmd jq curl

TF_DIR="$(tf_root)"
REFS="${REPO_ROOT}/build/image-refs-${ENVIRONMENT}.tfvars.json"
mkdir -p "${REPO_ROOT}/build"

refs_rc=0
"${SCRIPT_DIR}/lib/image-refs.sh" --applied --out "${REFS}" || refs_rc=$?

TARGET_ARGS=()
case "${refs_rc}" in
  0)
    ok "planning against the digests terraform last applied"
    # What is SERVING, compared with what terraform last applied. They differ
    # only if something deployed out of band -- a `gcloud run services update`
    # rollback, say -- and this plan would quietly undo it. Said, not refused:
    # the operator reading the plan decides.
    serving="$(cloud_run_image "${API_SERVICE}" 2>/dev/null || true)"
    pinned="$(jq -r '.image_refs["swarm-api"] // empty' "${REFS}")"
    if [[ -n "${serving}" && -n "${pinned}" && "${serving}" != "${pinned}" ]]; then
      warn "${API_SERVICE} is serving ${serving}"
      warn "terraform last applied ${pinned}"
      warn "something deployed out of band; this plan returns ${API_SERVICE} to what terraform applied."
      warn "To keep what is serving, deploy it through scripts/lib/deploy.sh --manifest <its manifest>."
    fi
    ;;
  3)
    warn "terraform has applied no image for ${ENVIRONMENT} and there is none to pin: a fresh project."
    warn "planning the Artifact Registry repository ALONE, so the first images have somewhere to go."
    warn "apply this, then: make build push deploy -- which plans everything else, by digest."
    TARGET_ARGS=(-target=module.project_services -target=module.artifact_registry)
    ;;
  *)
    die "could not work out which images are deployed (see above); refusing to plan without them"
    ;;
esac

tf_var_args
step "terraform plan (${ENVIRONMENT})"
if [[ "${refs_rc}" -eq 0 ]]; then
  tf -chdir="${TF_DIR}" plan \
    -input=false \
    -lock-timeout=120s \
    "${TF_VAR_ARGS[@]}" \
    -var-file="${REFS}" \
    -out="${REPO_ROOT}/build/${ENVIRONMENT}.tfplan"
else
  tf -chdir="${TF_DIR}" plan \
    -input=false \
    -lock-timeout=120s \
    "${TF_VAR_ARGS[@]}" \
    "${TARGET_ARGS[@]}" \
    -out="${REPO_ROOT}/build/${ENVIRONMENT}.tfplan"
fi
ok "plan written to build/${ENVIRONMENT}.tfplan"
