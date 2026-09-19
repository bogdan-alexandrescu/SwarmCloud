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

# Services this run could not update, wait for, or verify -- as opposed to
# services it genuinely updated. Kept so the closing line at the bottom of
# this script can tell the truth instead of printing "ok deployed" whenever
# execution merely reaches the end.
SKIPPED=()

# cloud_run_probe SERVICE FORMAT -> value in CLOUD_RUN_VALUE, empty and
# return 1 only when gcloud's own error says the service was not found. Any
# other failure -- an expired session, a missing run.services.get, a
# disabled Cloud Run API, the wrong region or project -- dies naming the
# real cause instead of being read as absence.
#
# Call this bare, as `if ! cloud_run_probe ...; then`. Never as
# `x="$(cloud_run_probe ...)"`: a command substitution forks a subshell, and
# `die`'s `exit` would then only close that subshell, leaving this script to
# read a dead session as a missing service.
CLOUD_RUN_VALUE=""
cloud_run_probe() {
  local service="$1" format="$2"
  local err_file msg rc=0
  CLOUD_RUN_VALUE=""
  err_file="$(mktemp "${TMPDIR:-/tmp}/swarm-run-describe.XXXXXX")"
  # `|| rc=$?`, not `if ...; then ...; fi`: when an if's condition is false
  # and it has no else, the if construct itself exits 0, so a bare `rc=$?`
  # placed after `fi` reads back 0 no matter how gcloud actually failed.
  CLOUD_RUN_VALUE="$(gcloud run services describe "${service}" --project "${PROJECT_ID}" \
       --region "${REGION}" --format="${format}" 2>"${err_file}")" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    rm -f "${err_file}"
    return 0
  fi
  msg="$(cat "${err_file}" 2>/dev/null || true)"
  rm -f "${err_file}"
  CLOUD_RUN_VALUE=""
  die_if_auth_failure "${msg}"
  case "${msg}" in
    *NOT_FOUND*|*"was not found"*|*"could not be found"*) return 1 ;;
  esac
  err "gcloud run services describe ${service} failed (exit ${rc}); this is NOT confirmation the service is absent"
  printf '%s\n' "${msg}" | redact | head -n 5 | sed 's/^/     /' >&2
  die "cannot confirm whether ${service} exists in ${REGION}/${PROJECT_ID} -- check the account, region and project before treating this as absent"
}

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
      SKIPPED+=("${service}: no image for ${image_name} in ${MANIFEST}")
      continue
    fi
    if ! cloud_run_probe "${service}" 'value(metadata.name)'; then
      warn "Cloud Run service ${service} does not exist yet; run 'make infra' first"
      SKIPPED+=("${service}: does not exist yet")
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
    if ! cloud_run_probe "${service}" 'value(status.conditions.filter("type:Ready").status)'; then
      warn "${service} does not exist; skipping readiness wait"
      SKIPPED+=("${service}: does not exist")
      break
    fi
    ready="${CLOUD_RUN_VALUE}"
    [[ "${ready}" == "True" ]] && { ok "${service} ready"; break; }
    if [[ "$(date -u +%s)" -ge "${DEADLINE}" ]]; then
      warn "${service} was not ready within ${WAIT_SECONDS}s (condition: ${ready:-unknown})"
      SKIPPED+=("${service}: not ready within ${WAIT_SECONDS}s (condition: ${ready:-unknown})")
      break
    fi
    sleep 5
  done
done

if [[ "${SKIP_HEALTH}" -eq 0 ]]; then
  step "Health"
  for service in "${API_SERVICE}" "${SCHEDULER_SERVICE}" "${QUOTA_SERVICE}" "${RECONCILER_SERVICE}"; do
    if ! cloud_run_probe "${service}" 'value(status.url)'; then
      warn "${service} does not exist; skipping /readyz check"
      SKIPPED+=("${service}: does not exist, /readyz not checked")
      continue
    fi
    url="${CLOUD_RUN_VALUE}"
    if [[ -z "${url}" ]]; then
      warn "${service} has no status.url yet; skipping /readyz check"
      SKIPPED+=("${service}: no status.url yet, /readyz not checked")
      continue
    fi
    # id_token resolved as its own statement, not inline in curl's argument
    # list: nested inside another command substitution, id_token's own `die`
    # on a mint failure would only close that inner subshell, so curl would
    # run with an empty bearer and a 401 would be misread as a bad /readyz.
    token="$(id_token)"
    curl_err="$(mktemp "${TMPDIR:-/tmp}/swarm-readyz.XXXXXX")"
    curl_rc=0
    # `|| curl_rc=$?`, not `if ! code=$(...); then`: `!` itself always
    # succeeds once its negated command has failed, so `$?` inside that
    # then-branch is 0 regardless of what curl actually returned.
    code="$(auth_config "${token}" | curl -sS -m 15 -o /dev/null -w '%{http_code}' -K - \
         "${url%/}/readyz" 2>"${curl_err}")" || curl_rc=$?
    if [[ "${curl_rc}" -ne 0 ]]; then
      curl_msg="$(cat "${curl_err}" 2>/dev/null || true)"
      rm -f "${curl_err}"
      # curl still writes its -w status (usually 000) even when it fails to
      # connect, so appending a second "000" on failure produced the
      # malformed six-digit code this used to print; a transport failure is
      # reported as exactly that, not as an HTTP response.
      warn "${service} /readyz: curl could not complete (exit ${curl_rc}); this is a transport failure, not an HTTP response"
      printf '%s\n' "${curl_msg}" | redact | head -n 3 | sed 's/^/     /' >&2
      SKIPPED+=("${service}: /readyz check failed to connect (curl exit ${curl_rc})")
      continue
    fi
    rm -f "${curl_err}"
    if [[ "${code}" == "200" ]]; then
      ok "${service} ${url}"
    else
      warn "${service} /readyz returned ${code}"
      SKIPPED+=("${service}: /readyz returned ${code}")
    fi
  done
fi

hr
if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  err "deploy of ${TAG} to ${ENVIRONMENT} finished with ${#SKIPPED[@]} unresolved issue(s):"
  for issue in "${SKIPPED[@]}"; do
    printf '     %s\n' "${issue}" >&2
  done
  die "not every service is confirmed on ${TAG}; see above before trusting this deploy"
fi
ok "deployed ${TAG}"
dim "verify end to end with: make smoke"
