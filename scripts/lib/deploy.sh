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
# The channel the promote step actually tagged, not an assumption that it
# matches the environment name -- assert_tag_is_complete compares against it.
CHANNEL="$(jq -r '.channel // empty' "${MANIFEST}")"
[[ -n "${CHANNEL}" ]] || die "${MANIFEST} records no channel; re-run 'make push'"
step "Deploy ${TAG} to ${ENVIRONMENT}"
jq -r '.images[] | "    \(.name)  \(.digest)"' "${MANIFEST}" >&2

image_ref() {
  jq -r --arg n "$1" '.images[] | select(.name == $n) | .ref // empty' "${MANIFEST}"
}

# A uniform `image_tag` is applied to EVERY image the terraform root
# references -- not only the ones that happen to be in the manifest. Building a
# subset therefore mints a tag that most images do not carry, and terraform
# plans revisions pointing at images that do not exist. Cloud Run then refuses
# them one service at a time, minutes into an apply, after other resources have
# already changed.
#
# Observed 2026-09-19: `build-images.sh swarm-ui` alone produced tag
# 7f6a80cb32d5; the apply created six unservable revisions before failing.
# Traffic stayed on the previous revisions -- Cloud Run's doing, not ours -- so
# it cost an apply rather than an outage. It is not worth relying on that.
#
# The expected set is derived from the registry rather than restated here: any
# image already carrying the channel tag is part of this platform and must
# carry the new tag too. An unreadable registry is NOT an empty one, so a
# failed listing aborts instead of passing.
assert_tag_is_complete() {
  local tag="$1" channel="$2" tmp count

  tmp="$(mktemp -d)"
  if ! gcloud artifacts docker tags list "${IMAGE_REPO}" \
        --project="${PROJECT_ID}" --format='value(tag,image)' \
        >"${tmp}/all" 2>"${tmp}/err"; then
    err "could not list tags in ${IMAGE_REPO}"
    redact <"${tmp}/err" | head -n 5 | sed 's/^/     /' >&2
    rm -rf "${tmp}"
    die "refusing to deploy without confirming ${tag} covers every image -- a registry we cannot read is not an empty one"
  fi

  awk -v t="${channel}" '$1 == t { n = split($2, p, "/"); print p[n] }' "${tmp}/all" | sort -u >"${tmp}/onchannel"

  # ALSO every image the terraform root REFERENCES. Without this the guard has
  # a blind spot it was bitten by on 2026-09-20: a NEW image, declared in
  # terraform but never yet promoted, is not on the channel, so it is not in
  # the expected set, so the guard passes -- and the apply then fails on
  # "Image ... not found" twelve minutes in, having already changed other
  # resources. The original failure this guard was built for (a PARTIAL build
  # of images that already exist) is the other direction, and both are real.
  grep -rhoE '\$\{local\.image_base\}/[a-z0-9-]+' "${REPO_ROOT}/terraform/infra" 2>/dev/null \
    | sed 's|.*/||' | sort -u >"${tmp}/declared" || true
  cat "${tmp}/onchannel" "${tmp}/declared" 2>/dev/null | sort -u >"${tmp}/chan"
  awk -v t="${tag}"     '$1 == t { n = split($2, p, "/"); print p[n] }' "${tmp}/all" | sort -u >"${tmp}/have"

  if [[ ! -s "${tmp}/chan" ]]; then
    info "no image is on :${channel} or referenced by terraform; treating ${tag} as the first deploy"
    rm -rf "${tmp}"
    return 0
  fi

  comm -23 "${tmp}/chan" "${tmp}/have" >"${tmp}/missing"
  if [[ -s "${tmp}/missing" ]]; then
    err "tag ${tag} is incomplete -- on :${channel} but with no :${tag}:"
    sed 's/^/     /' "${tmp}/missing" >&2
    rm -rf "${tmp}"
    die "terraform applies ONE image_tag to every service and job, so this would point those at an image that does not exist. Build every target ('make build push' with no TARGET) before deploying."
  fi

  count="$(wc -l <"${tmp}/chan" | tr -d ' ')"
  rm -rf "${tmp}"
  ok "tag ${tag} covers all ${count} image(s) on :${channel}"
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
    assert_tag_is_complete "${TAG}" "${CHANNEL}"
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
# The UI is included here: a revision that fails to start is a broken deploy
# whether it serves JSON or JavaScript.
for service in "${API_SERVICE}" "${SCHEDULER_SERVICE}" "${QUOTA_SERVICE}" "${RECONCILER_SERVICE}" "${UI_SERVICE}"; do
  while :; do
    # NOT `status.conditions.filter("type:Ready").status`. That expression is
    # not valid gcloud format syntax and never was:
    #
    #   ERROR: Transform function expected
    #   [service value(status.conditions.filter("type:Ready").status *HERE* )]
    #
    # It sat behind `2>/dev/null || true`, so the error was discarded, the probe
    # returned empty, and empty was read as "the service does not exist". This
    # readiness wait has therefore NEVER checked readiness -- it reported either
    # a missing service or nothing at all, for every deploy this script has ever
    # run. It only became visible when the swallowed stderr was fixed.
    #
    # A service is ready when its newest revision is the one serving. Comparing
    # the two names says that directly, and catches the case a single "Ready"
    # condition misses: an old revision healthy while the new one failed to
    # start.
    if ! cloud_run_probe "${service}" 'value(status.latestReadyRevisionName,status.latestCreatedRevisionName)'; then
      warn "${service} does not exist; skipping readiness wait"
      SKIPPED+=("${service}: does not exist")
      break
    fi
    ready="$(printf '%s' "${CLOUD_RUN_VALUE}" | awk '{print $1}')"
    created="$(printf '%s' "${CLOUD_RUN_VALUE}" | awk '{print $2}')"
    if [[ -n "${ready}" && "${ready}" == "${created}" ]]; then
      ok "${service} ready (${ready})"
      break
    fi
    if [[ "$(date -u +%s)" -ge "${DEADLINE}" ]]; then
      warn "${service} was not ready within ${WAIT_SECONDS}s (serving ${ready:-none}, newest ${created:-unknown})"
      SKIPPED+=("${service}: not ready within ${WAIT_SECONDS}s (condition: ${ready:-unknown})")
      break
    fi
    sleep 5
  done
done

if [[ "${SKIP_HEALTH}" -eq 0 ]]; then
  step "Health"
  # ${UI_SERVICE} is deliberately absent. /readyz is the control plane's
  # contract -- "my dependencies answer" -- and a static file server has no
  # dependencies to speak for. Its liveness is the readiness wait above.
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
    elif [[ "${code}" == "404" ]]; then
      # A 404 on /readyz from OUTSIDE the VPC is Google's edge refusing the
      # request, not the service answering. Every control-plane service runs
      # with ingress internal-and-cloud-load-balancing, so a direct call to its
      # run.app URL from a workstation cannot reach the container -- the 404 is
      # produced before the request arrives, and the route exists and works.
      #
      # This check has therefore never been able to pass from a developer
      # machine. It only became visible when the swallowed stderr around it was
      # fixed, at which point every deploy reported four "unresolved issues"
      # that were the ingress setting doing its job.
      #
      # Reported, never counted as a failure: the readiness wait above already
      # confirmed the newest revision is serving, which is what a deploy needs
      # to know from here. Checking /readyz for real means asking from inside
      # the VPC or through the load balancer.
      dim "  ${service}: /readyz not reachable from here (404 at the edge; ingress is internal-and-cloud-load-balancing)"
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
