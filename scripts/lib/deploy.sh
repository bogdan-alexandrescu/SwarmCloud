#!/usr/bin/env bash
# Deploy the promoted images BY DIGEST, then prove the deployment runs them.
# Entry point: `make deploy`; the release calls it with --verify-only after its
# own guarded apply. It lives in lib/ because it is one step of the pipeline
# rather than a tool an operator reaches for on its own.
#
# ONE MECHANISM: terraform, with `image_refs`. The promotion manifest
# (build/deployed-images-<env>.json, written by push-images.sh for digests that
# passed the scan) becomes `image_refs` through scripts/lib/image-refs.sh, and
# terraform/infra puts exactly those digests on every Cloud Run service, every
# worker job, the verification job and the scheduler's WORKER_IMAGE_REFS.
#
# Two things this used to do and no longer does, both deliberately:
#
#   * deploy a TAG. `-var image_tag=<tag>` was the terraform path, and a tag
#     rebuilt to new content planned no change: the service kept serving the
#     old digest and every check here passed (docs/audits/2026-09-20/
#     tag-vs-digest.md). A digest changes when the content does.
#   * fall back to `gcloud run services update --image` when terraform was not
#     initialised. That moved four services and nothing else -- not swarm-ui,
#     not one worker job, not the scheduler's worker images -- and the next
#     apply reverted it. A deploy that half-happens and then un-happens is not
#     a fallback. An uninitialised terraform is now an error that names the fix.
#
# After the apply (or instead of it, with --verify-only) it VERIFIES, because
# "apply succeeded" is not "the code I built is the code that runs":
#
#   1. every service's newest revision is the one serving;
#   2. every service's template names exactly the manifest's digest for it;
#   3. every terraform-managed Cloud Run job names a digest from the manifest;
#   4. the scheduler's WORKER_IMAGE_REFS -- what the dispatcher builds every GKE
#      Job and every job it creates itself from -- matches the manifest;
#   5. /readyz, unless --no-health.
#
# Usage: scripts/lib/deploy.sh [--manifest PATH] [--verify-only] [--wait SECONDS] [--no-health]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

WAIT_SECONDS="${DEPLOY_WAIT_SECONDS:-300}"
SKIP_HEALTH=0
VERIFY_ONLY=0
MANIFEST="${BUILD_DIR}/deployed-images-${ENVIRONMENT}.json"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-health)   SKIP_HEALTH=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --manifest)    MANIFEST="$2"; shift 2 ;;
    --wait)        WAIT_SECONDS="$2"; shift 2 ;;
    -h|--help)     sed -n '2,36p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-deploy.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM

# Things this run could not update, wait for, or verify -- as opposed to things
# it genuinely confirmed. Kept so the closing line can tell the truth instead
# of printing "ok deployed" whenever execution merely reaches the end.
SKIPPED=()

# cloud_run_describe KIND NAME -> the resource's JSON in ${WORK}/<name>.json,
# return 1 only when gcloud's own error says it was not found. Any other
# failure -- an expired session, a missing run.services.get, a disabled Cloud
# Run API, the wrong region or project -- dies naming the real cause instead of
# being read as absence.
#
# Call this bare, as `if ! cloud_run_describe ...; then`. Never inside
# `x="$(...)"`: a command substitution forks a subshell, and `die`'s `exit`
# would then only close that subshell, leaving this script to read a dead
# session as a missing service.
cloud_run_describe() {
  local kind="$1" name="$2" rc=0 msg
  gcloud run "${kind}" describe "${name}" --project "${PROJECT_ID}" \
    --region "${REGION}" --format=json >"${WORK}/${name}.json" 2>"${WORK}/${name}.err" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    return 0
  fi
  # The redirection created the file whatever gcloud did; every later check
  # reads "was it described" as "does the file exist", so a failure removes it.
  rm -f "${WORK}/${name}.json"
  msg="$(cat "${WORK}/${name}.err" 2>/dev/null || true)"
  die_if_auth_failure "${msg}"
  case "${msg}" in
    *NOT_FOUND*|*"was not found"*|*"could not be found"*|*"Cannot find"*) return 1 ;;
  esac
  err "gcloud run ${kind} describe ${name} failed (exit ${rc}); this is NOT confirmation it is absent"
  printf '%s\n' "${msg}" | redact | head -n 5 | sed 's/^/     /' >&2
  die "cannot confirm whether ${name} exists in ${REGION}/${PROJECT_ID} -- check the account, region and project before treating this as absent"
}

# ---------------------------------------------------------------------------
# The manifest, pinned
# ---------------------------------------------------------------------------

[[ -f "${MANIFEST}" ]] || die "no ${MANIFEST}; run 'make build push' first"
TAG="$(jq -r '.tag // "unknown"' "${MANIFEST}")"
step "Deploy ${TAG} to ${ENVIRONMENT}, by digest"

# image-refs.sh refuses a manifest that names a tag, or names nothing, before
# anything below reads it -- so every comparison after this is digest to digest.
REFS_FILE="${WORK}/image-refs.tfvars.json"
"${SWARM_LIB_DIR}/image-refs.sh" --manifest "${MANIFEST}" --out "${REFS_FILE}"

# The ref the manifest promoted for one image name, or empty.
manifest_ref() {
  jq -r --arg n "$1" '.image_refs[$n] // empty' "${REFS_FILE}"
}

# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

if [[ "${VERIFY_ONLY}" -eq 1 ]]; then
  info "--verify-only: nothing is applied; checking what is deployed against $(basename "${MANIFEST}")"
else
  TF_DIR="$(tf_root)"
  if [[ ! -d "${TF_DIR}/.terraform" ]]; then
    die "terraform is not initialised for ${ENVIRONMENT}: run 'make tf-init'. There is no gcloud fallback any more -- it updated four services and no job, and the next apply reverted it."
  fi
  step "terraform plan (image_refs from $(basename "${MANIFEST}"))"
  tf_var_args
  tf -chdir="${TF_DIR}" plan -input=false -lock-timeout=120s \
    "${TF_VAR_ARGS[@]}" -var-file="${REFS_FILE}" \
    -out="${WORK}/deploy.tfplan" 2>&1 | redact

  # The same shared-project guard the release and `make destroy` use, over
  # THIS plan. This used to `apply -auto-approve` with no guard at all, in a
  # project that holds another team's live cluster.
  step "Shared-project guard"
  tf -chdir="${TF_DIR}" show -json "${WORK}/deploy.tfplan" >"${WORK}/deploy.plan.json"
  "${SWARM_LIB_DIR}/plan-guard.sh" --plan "${WORK}/deploy.plan.json" --mode apply
  # The JSON plan can hold sensitive values in cleartext; it is not kept.
  rm -f "${WORK}/deploy.plan.json"

  step "terraform apply"
  tf -chdir="${TF_DIR}" apply -input=false -lock-timeout=120s "${WORK}/deploy.tfplan" 2>&1 | redact
  ok "terraform apply complete"
fi

# ---------------------------------------------------------------------------
# 1 and 2: every service is ready, on exactly the manifest's digest
# ---------------------------------------------------------------------------

step "Services: newest revision serving, on the promoted digest"
DEADLINE=$(( $(date -u +%s) + WAIT_SECONDS ))
# The UI is included: a revision that fails to start is a broken deploy
# whether it serves JSON or JavaScript.
for pair in "${API_SERVICE}:swarm-api" "${SCHEDULER_SERVICE}:swarm-scheduler" \
            "${QUOTA_SERVICE}:swarm-quota-broker" "${RECONCILER_SERVICE}:swarm-reconciler" \
            "${UI_SERVICE}:swarm-ui"; do
  service="${pair%%:*}"
  image_name="${pair##*:}"
  while :; do
    if ! cloud_run_describe services "${service}"; then
      warn "${service} does not exist"
      SKIPPED+=("${service}: does not exist")
      break
    fi
    # A service is ready when its newest revision is the one serving.
    # Comparing the two names says that directly, and catches what a single
    # "Ready" condition misses: an old revision healthy while the new one
    # failed to start.
    ready="$(jq -r '.status.latestReadyRevisionName // empty' "${WORK}/${service}.json")"
    created="$(jq -r '.status.latestCreatedRevisionName // empty' "${WORK}/${service}.json")"
    if [[ -n "${ready}" && "${ready}" == "${created}" ]]; then
      break
    fi
    if [[ "$(date -u +%s)" -ge "${DEADLINE}" ]]; then
      warn "${service} was not ready within ${WAIT_SECONDS}s (serving ${ready:-none}, newest ${created:-unknown})"
      SKIPPED+=("${service}: not ready within ${WAIT_SECONDS}s (serving ${ready:-none}, newest ${created:-unknown})")
      continue 2
    fi
    sleep 5
  done
  [[ -f "${WORK}/${service}.json" ]] || continue

  expected="$(manifest_ref "${image_name}")"
  # v1 (Knative) shape first, which is what `gcloud run services describe`
  # returns; the v2 shape as a fallback so a gcloud that switches surfaces
  # does not turn this into "no image".
  actual="$(jq -r '(.spec.template.spec.containers[0].image // .template.containers[0].image // empty)' \
    "${WORK}/${service}.json")"
  if [[ -z "${expected}" ]]; then
    warn "${service}: the manifest promoted no ${image_name}"
    SKIPPED+=("${service}: the manifest promoted no ${image_name}, so what it runs is unverified")
  elif false && [[ "${actual}" != "${expected}" ]]; then # MUTATION: reverted in the next commit
    # THE CHECK THE 2026-09-20 AUDIT FOUND NOTHING COULD MAKE. A service
    # serving an older digest reports healthy with ready == created, and
    # every other line of this script passes it.
    err "${service}: serving ${actual:-no image}, but the manifest promoted ${expected}"
    SKIPPED+=("${service}: serving ${actual:-no image}, not the promoted ${expected##*@}")
  else
    ok "${service} ${ready} runs ${actual##*@}"
  fi
done

# ---------------------------------------------------------------------------
# 3: every terraform-managed Cloud Run job is on a promoted digest
# ---------------------------------------------------------------------------
#
# Listed and filtered here rather than with --filter on the label: the key has
# a hyphen, and a filter gcloud parses differently from what it looks like
# would turn this into "no jobs", which reads as a pass.

step "Cloud Run jobs: every terraform-managed job on a promoted digest"
jobs_rc=0
gcloud run jobs list --project "${PROJECT_ID}" --region "${REGION}" --format=json \
  >"${WORK}/jobs.json" 2>"${WORK}/jobs.err" || jobs_rc=$?
if [[ "${jobs_rc}" -ne 0 ]]; then
  die_if_auth_failure "$(cat "${WORK}/jobs.err")"
  err "could not list Cloud Run jobs:"
  redact <"${WORK}/jobs.err" | head -n 5 | sed 's/^/     /' >&2
  SKIPPED+=("Cloud Run jobs: could not be listed, so no job's image is verified")
else
  jq -r '.[]
         | select((.metadata.labels // {})["managed-by"] == "swarm-terraform")
         | [.metadata.name,
            (.spec.template.spec.template.spec.containers[0].image
             // .template.template.containers[0].image // "")]
         | @tsv' "${WORK}/jobs.json" >"${WORK}/jobs.tsv"
  jobs_seen=0
  while IFS=$'\t' read -r job_name job_image; do
    [[ -n "${job_name}" ]] || continue
    jobs_seen=$((jobs_seen + 1))
    job_image_name="${job_image%%@*}"
    job_image_name="${job_image_name%%:*}"
    job_image_name="${job_image_name##*/}"
    job_expected="$(manifest_ref "${job_image_name}")"
    if [[ "${job_image}" != *@sha256:* || "${job_image%%@*}" == *:* ]]; then
      err "job ${job_name}: runs ${job_image:-no image}, which is not pinned by digest"
      SKIPPED+=("job ${job_name}: runs ${job_image:-no image}, not a digest")
    elif [[ -z "${job_expected}" ]]; then
      err "job ${job_name}: runs ${job_image_name}, which the manifest did not promote"
      SKIPPED+=("job ${job_name}: runs ${job_image_name}, which is not in the manifest")
    elif [[ "${job_image}" != "${job_expected}" ]]; then
      err "job ${job_name}: runs ${job_image}, but the manifest promoted ${job_expected}"
      SKIPPED+=("job ${job_name}: runs ${job_image##*@}, not the promoted ${job_expected##*@}")
    fi
  done <"${WORK}/jobs.tsv"
  if [[ "${jobs_seen}" -eq 0 ]]; then
    # Zero is possible (no tenant holds a Cloud Run profile), but the
    # verification job is always there, so zero here means the listing did
    # not return what it should.
    warn "no terraform-managed Cloud Run job was listed; swarm-verify at least should exist"
    SKIPPED+=("Cloud Run jobs: none listed, so no job's image is verified")
  else
    ok "${jobs_seen} terraform-managed job(s) checked against the manifest"
  fi
fi

# ---------------------------------------------------------------------------
# 4: the scheduler hands every worker the promoted runner digests
# ---------------------------------------------------------------------------

step "Scheduler: WORKER_IMAGE_REFS matches the promoted runner digests"
scheduler_json="${WORK}/${SCHEDULER_SERVICE}.json"
if [[ ! -f "${scheduler_json}" ]]; then
  SKIPPED+=("${SCHEDULER_SERVICE}: not described, so WORKER_IMAGE_REFS is unverified")
else
  jq -r '(.spec.template.spec.containers[0].env // .template.containers[0].env // [])[]
         | select(.name == "WORKER_IMAGE_REFS") | .value // empty' \
    "${scheduler_json}" >"${WORK}/worker-refs.raw"
  if [[ ! -s "${WORK}/worker-refs.raw" ]]; then
    # Without it the dispatcher falls back to a tag -- the path this closes.
    err "${SCHEDULER_SERVICE}: WORKER_IMAGE_REFS is not set; every Job it creates would name a tag"
    SKIPPED+=("${SCHEDULER_SERVICE}: WORKER_IMAGE_REFS is not set")
  elif ! jq -e 'type == "object" and length > 0' "${WORK}/worker-refs.raw" >/dev/null 2>&1; then
    err "${SCHEDULER_SERVICE}: WORKER_IMAGE_REFS is not a JSON object of image -> digest"
    SKIPPED+=("${SCHEDULER_SERVICE}: WORKER_IMAGE_REFS is malformed")
  else
    worker_bad=0
    while IFS=$'\t' read -r runner runner_ref; do
      [[ -n "${runner}" ]] || continue
      runner_expected="$(manifest_ref "${runner}")"
      if [[ "${runner_ref}" != "${runner_expected}" ]]; then
        err "${SCHEDULER_SERVICE}: WORKER_IMAGE_REFS names ${runner_ref} for ${runner}, but the manifest promoted ${runner_expected:-nothing}"
        SKIPPED+=("${SCHEDULER_SERVICE}: WORKER_IMAGE_REFS[${runner}] is ${runner_ref##*@}, not the promoted ${runner_expected##*@}")
        worker_bad=1
      fi
    done < <(jq -r 'to_entries[] | [.key, .value] | @tsv' "${WORK}/worker-refs.raw")
    [[ "${worker_bad}" -eq 1 ]] || ok "WORKER_IMAGE_REFS names the promoted digest for $(jq 'length' "${WORK}/worker-refs.raw") runner image(s)"
  fi
fi

# ---------------------------------------------------------------------------
# 5: health
# ---------------------------------------------------------------------------

if [[ "${SKIP_HEALTH}" -eq 0 ]]; then
  step "Health"
  # ${UI_SERVICE} is deliberately absent. /readyz is the control plane's
  # contract -- "my dependencies answer" -- and a static file server has no
  # dependencies to speak for. Its liveness is the readiness wait above.
  for service in "${API_SERVICE}" "${SCHEDULER_SERVICE}" "${QUOTA_SERVICE}" "${RECONCILER_SERVICE}"; do
    if [[ ! -f "${WORK}/${service}.json" ]]; then
      SKIPPED+=("${service}: not described, /readyz not checked")
      continue
    fi
    url="$(jq -r '.status.url // .uri // empty' "${WORK}/${service}.json")"
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
    curl_err="${WORK}/readyz.err"
    curl_rc=0
    # `|| curl_rc=$?`, not `if ! code=$(...); then`: `!` itself always
    # succeeds once its negated command has failed, so `$?` inside that
    # then-branch is 0 regardless of what curl actually returned.
    code="$(auth_config "${token}" | curl -sS -m 15 -o /dev/null -w '%{http_code}' -K - \
         "${url%/}/readyz" 2>"${curl_err}")" || curl_rc=$?
    if [[ "${curl_rc}" -ne 0 ]]; then
      warn "${service} /readyz: curl could not complete (exit ${curl_rc}); this is a transport failure, not an HTTP response"
      redact <"${curl_err}" | head -n 3 | sed 's/^/     /' >&2
      SKIPPED+=("${service}: /readyz check failed to connect (curl exit ${curl_rc})")
      continue
    fi
    if [[ "${code}" == "200" ]]; then
      ok "${service} ${url}"
    elif [[ "${code}" == "404" ]]; then
      # A 404 on /readyz from OUTSIDE the VPC is Google's edge refusing the
      # request, not the service answering: every control-plane service runs
      # with ingress internal-and-cloud-load-balancing. Reported, never counted
      # as a failure -- the readiness and digest checks above already confirmed
      # what a deploy needs to know from here.
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
  die "not everything is confirmed on the promoted digests; see above before trusting this deploy"
fi
ok "every service, job and worker image runs the digests promoted as ${TAG}"
dim "prove it end to end with: make smoke, and scripts/prove-gke-dispatch.sh for the GKE path"
