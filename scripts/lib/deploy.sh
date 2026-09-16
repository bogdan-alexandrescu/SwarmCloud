#!/usr/bin/env bash
# Roll the promoted images out to the Cloud Run control plane. Entry point:
# `make deploy`. It lives in lib/ because it is one step of the pipeline rather
# than a tool an operator reaches for on its own.
#
# Two supported shapes, detected rather than assumed, because Terraform owns the
# service definitions and only Terraform knows whether it takes an image
# variable:
#
#   1. the environment declares `image_refs` or `image_tag` -> terraform apply,
#      so the deployed digest is recorded in state and cannot drift;
#   2. it declares neither -> `gcloud run services update --image <digest>`.
#
# Either way the deployed thing is an immutable sha256 digest from the promotion
# manifest, never a tag someone could move underneath us.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

WAIT_SECONDS="${DEPLOY_WAIT_SECONDS:-300}"
SKIP_HEALTH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-health) SKIP_HEALTH=1; shift ;;
    --wait)      WAIT_SECONDS="$2"; shift 2 ;;
    -h|--help)   sed -n '2,17p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq
MANIFEST="${BUILD_DIR}/deployed-images-${ENVIRONMENT}.json"
[[ -f "${MANIFEST}" ]] || die "no ${MANIFEST}; run 'make build push' first"

TAG="$(jq -r '.tag' "${MANIFEST}")"
step "Deploy ${TAG} to ${ENVIRONMENT}"
jq -r '.images[] | "    \(.name)  \(.digest)"' "${MANIFEST}" >&2

image_ref() {
  jq -r --arg n "$1" '.images[] | select(.name == $n) | .ref // empty' "${MANIFEST}"
}

TF_ROOT="${REPO_ROOT}/terraform/infra"
TF_VAR_NAME=""
if [[ -d "${TF_ROOT}/.terraform" ]]; then
  if grep -Rqs 'variable[[:space:]]*"image_refs"' "${TF_ROOT}"; then
    TF_VAR_NAME="image_refs"
  elif grep -Rqs 'variable[[:space:]]*"image_tag"' "${TF_ROOT}"; then
    TF_VAR_NAME="image_tag"
  fi
fi

if [[ -n "${TF_VAR_NAME}" ]]; then
  step "Terraform-managed services (var ${TF_VAR_NAME})"
  VAR_ARGS=()
  if [[ "${TF_VAR_NAME}" == "image_refs" ]]; then
    REFS_JSON="$(jq -c '[.images[] | {key:.name, value:.ref}] | from_entries' "${MANIFEST}")"
    VAR_ARGS+=(-var="image_refs=${REFS_JSON}")
  else
    VAR_ARGS+=(-var="image_tag=${TAG}")
  fi
  tf_var_args
  tf -chdir="${TF_ROOT}" apply -input=false -auto-approve -lock-timeout=120s \
    "${TF_VAR_ARGS[@]}" "${VAR_ARGS[@]}" 2>&1 | redact
  ok "terraform apply complete"
else
  step "Updating Cloud Run services directly"
  info "the ${ENVIRONMENT} environment declares no image variable, so services are updated in place"
  for pair in "${API_SERVICE}:swarm-api" "${SCHEDULER_SERVICE}:swarm-scheduler" \
              "${QUOTA_SERVICE}:swarm-quota-broker" "${RECONCILER_SERVICE}:swarm-reconciler"; do
    service="${pair%%:*}"
    image_name="${pair##*:}"
    ref="$(image_ref "${image_name}")"
    if [[ -z "${ref}" ]]; then
      warn "no image for ${image_name} in the manifest; skipping ${service}"
      continue
    fi
    if ! gcloud run services describe "${service}" --project "${PROJECT_ID}" \
         --region "${REGION}" --format='value(metadata.name)' >/dev/null 2>&1; then
      warn "Cloud Run service ${service} does not exist yet; run 'make infra' first"
      continue
    fi
    info "${service} -> ${ref##*@}"
    gcloud run services update "${service}" \
      --project "${PROJECT_ID}" --region "${REGION}" \
      --image "${ref}" \
      --update-labels="managed-by=swarm-terraform,swarm-image-tag=${TAG}" \
      --quiet 2>&1 | redact
    ok "${service} updated"
  done
fi

step "Waiting for revisions to become ready"
DEADLINE=$(( $(date -u +%s) + WAIT_SECONDS ))
for service in "${API_SERVICE}" "${SCHEDULER_SERVICE}" "${QUOTA_SERVICE}" "${RECONCILER_SERVICE}"; do
  while :; do
    ready="$(gcloud run services describe "${service}" --project "${PROJECT_ID}" \
      --region "${REGION}" --format='value(status.conditions.filter("type:Ready").status)' 2>/dev/null || true)"
    [[ "${ready}" == "True" ]] && { ok "${service} ready"; break; }
    if [[ "$(date -u +%s)" -ge "${DEADLINE}" ]]; then
      warn "${service} was not ready within ${WAIT_SECONDS}s (condition: ${ready:-unknown})"
      break
    fi
    sleep 5
  done
done

if [[ "${SKIP_HEALTH}" -eq 0 ]]; then
  step "Health"
  for service in "${API_SERVICE}" "${SCHEDULER_SERVICE}" "${QUOTA_SERVICE}" "${RECONCILER_SERVICE}"; do
    url="$(gcloud run services describe "${service}" --project "${PROJECT_ID}" \
      --region "${REGION}" --format='value(status.url)' 2>/dev/null || true)"
    [[ -n "${url}" ]] || continue
    code="$(curl -sS -m 15 -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer $(id_token)" "${url%/}/healthz" 2>/dev/null || echo 000)"
    if [[ "${code}" == "200" ]]; then
      ok "${service} ${url}"
    else
      warn "${service} /healthz returned ${code}"
    fi
  done
fi

hr
ok "deployed ${TAG}"
dim "verify end to end with: make smoke"
