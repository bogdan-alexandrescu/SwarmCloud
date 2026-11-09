#!/usr/bin/env bash
# Apply a release's saved plan, and when that apply fails, still ship the
# Cloud Run image updates it carried. The release stays red either way.
#
# WHY THIS EXISTS. A release's apply IS its deploy: the new Cloud Run revisions
# of swarm-api, swarm-ui and the worker jobs are image updates inside the same
# saved plan as everything else. On 2026-11-09 dev's release applied a held
# plan in one apply, one unrelated resource in it (a GPU machine) could not be
# created for want of capacity in its zone, the apply failed, and no service
# update shipped: the console stayed on the previous night's build for about
# ten hours. One failing resource must not block a release (owner, 2026-11-09).
#
# WHAT IT DOES.
#
#   1. Applies the saved plan, exactly as the workflow did before. Success
#      ends here: exit 0, outcome=applied.
#   2. On failure, names the resource(s) Terraform reported, and re-plans with
#      `-target` on the Cloud Run services and jobs that the FAILED plan
#      updated in place -- the image updates, and nothing else.
#   3. Judges that targeted plan before applying any of it. It is applied only
#      when every change in it is an in-place update of one of those services
#      or jobs, every value the failed plan set for it is the value the
#      targeted plan sets, the shared-project guard passes and the IAM
#      classification answers `false`. `-target` also plans whatever the
#      targets depend on, so a service that references the failed resource, a
#      new service account or an IAM binding brings it into the plan -- and the
#      plan is refused, nothing applied.
#   4. Reports both outcomes: the services updated (or why not) and the
#      resource that failed. Exit 1 whenever step 1 failed, so the release is
#      still red and the deploy job after it does not run.
#
# NOTHING THE APPROVAL DID NOT COVER. The targeted plan is planned with the
# failed plan's OWN input variables (read from `terraform show -json` of the
# saved plan, written to a 0600 file in a private temporary directory that is
# removed on exit and never printed), at the same commit, so it pins the same
# image digests. Step 3 then refuses any change the failed plan did not
# propose in the same form. So on dev-iam, where the owner approved the held
# plan, and on prod, behind `approval (prod)`, what is applied is a subset of
# what was approved; and a targeted apply never adds, changes or removes IAM.
# The held plan itself is never re-planned or edited: step 1 applies it as the
# owner approved it, checked by sha256 in the workflow before this runs.
#
# Usage (from the workflow, under the release lock it already holds):
#   scripts/lib/release-apply.sh --chdir "${TF_ROOT}" --plan plan.tfplan \
#       --lock-timeout 180s [--outputs "$GITHUB_OUTPUT"] [--summary "$GITHUB_STEP_SUMMARY"]
#
# --plan is relative to --chdir, as Terraform takes it. --outputs gets
# `outcome=applied|services|failed` and `failed_resources=<addresses>`;
# `services` means the full apply failed and the Cloud Run updates are live
# (or were already live). Exit 0 only for `applied`.

set -euo pipefail
# The guards it reaches (plan-guard.sh) name and read the project themselves.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The resource types a targeted apply may change: what carries an image.
SERVICE_TYPES_JSON='["google_cloud_run_v2_service","google_cloud_run_v2_job"]'

CHDIR="${TF_ROOT:-terraform/infra}"
PLAN="plan.tfplan"
LOCK_TIMEOUT="180s"
OUTPUTS=""
SUMMARY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --chdir)        CHDIR="$2"; shift 2 ;;
    --plan)         PLAN="$2"; shift 2 ;;
    --lock-timeout) LOCK_TIMEOUT="$2"; shift 2 ;;
    --outputs)      OUTPUTS="$2"; shift 2 ;;
    --summary)      SUMMARY="$2"; shift 2 ;;
    -h|--help)      sed -n '2,50p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd jq
require_cmd terraform
[[ -d "${CHDIR}" ]] || die "no Terraform root at ${CHDIR}"
[[ -f "${CHDIR}/${PLAN}" ]] || die "no saved plan at ${CHDIR}/${PLAN}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/release-apply.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
chmod 700 "${WORK}"

output() { [[ -z "${OUTPUTS}" ]] || printf '%s=%s\n' "$1" "$2" >>"${OUTPUTS}"; }
summary() { [[ -z "${SUMMARY}" ]] || printf '%s\n' "$@" >>"${SUMMARY}"; }

# ---------------------------------------------------------------------------
# 1. The full apply.
# ---------------------------------------------------------------------------
set +e
terraform -chdir="${CHDIR}" apply -input=false -lock-timeout="${LOCK_TIMEOUT}" -no-color "${PLAN}" 2>&1 \
  | tee "${WORK}/apply.log"
full_status="${PIPESTATUS[0]}"
set -e
if [[ "${full_status}" -eq 0 ]]; then
  output outcome applied
  exit 0
fi

# The resources Terraform blamed: each error block carries `with ADDRESS,`.
# Read from the log file, never through $(...), so nothing here is lost.
sed -nE 's/^(│|\|)?[[:space:]]+with ([^[:space:]].*),[[:space:]]*$/\2/p' "${WORK}/apply.log" \
  | sort -u >"${WORK}/failed.txt"
if [[ -s "${WORK}/failed.txt" ]]; then
  failed="$(paste -sd ' ' "${WORK}/failed.txt")"
else
  failed="(Terraform named no resource; the apply log above has the error)"
fi
output failed_resources "${failed}"

# finish OUTCOME HEADLINE DETAIL... -- the report, and the release stays red.
finish() {
  local outcome="$1" headline="$2"
  shift 2
  output outcome "${outcome}"
  hr
  err "the full apply failed on: ${failed}"
  err "${headline}"
  local line
  for line in "$@"; do err "  ${line}"; done
  summary "### The full apply failed: the release is red" "" \
    "Failed: \`${failed}\`" "" "${headline}" ""
  for line in "$@"; do summary "- ${line}"; done
  summary ""
  echo "::error title=The full apply failed::failed on ${failed}. ${headline}"
  exit 1
}

# ---------------------------------------------------------------------------
# 2. The targeted re-plan: the failed plan's Cloud Run updates, its variables.
# ---------------------------------------------------------------------------
if ! terraform -chdir="${CHDIR}" show -json "${PLAN}" >"${WORK}/full.json" \
    || ! jq -e 'type == "object" and has("format_version")' "${WORK}/full.json" >/dev/null; then
  finish failed "the failed plan could not be read, so no service was retried"
fi

jq -r --argjson types "${SERVICE_TYPES_JSON}" '
  (.resource_changes // [])[]
  | select((.type as $t | $types | index($t)) != null and .change.actions == ["update"])
  | .address' "${WORK}/full.json" >"${WORK}/targets.txt"
if [[ ! -s "${WORK}/targets.txt" ]]; then
  finish failed "the failed plan updated no Cloud Run service or job, so there is nothing to retry"
fi

( umask 077; jq '(.variables // {}) | map_values(.value)' "${WORK}/full.json" >"${WORK}/vars.tfvars.json" )

targets=()
while IFS= read -r address; do
  targets+=("-target=${address}")
done <"${WORK}/targets.txt"
log "the full apply failed; re-planning only its ${#targets[@]} Cloud Run update(s)"

if ! terraform -chdir="${CHDIR}" plan -input=false -lock-timeout="${LOCK_TIMEOUT}" -no-color \
      -var-file="${WORK}/vars.tfvars.json" "${targets[@]}" -out="${WORK}/services.tfplan"; then
  finish failed "the targeted plan of the Cloud Run services and jobs failed, so no service was updated"
fi
if ! terraform -chdir="${CHDIR}" show -json "${WORK}/services.tfplan" >"${WORK}/services.json"; then
  finish failed "the targeted plan could not be read, so it was not applied"
fi

# ---------------------------------------------------------------------------
# 3. Judge it. Every changed row must be an in-place update of a Cloud Run
#    service or job that the failed plan updated, to the values it set.
# ---------------------------------------------------------------------------
if ! jq -r --argjson types "${SERVICE_TYPES_JSON}" --slurpfile full "${WORK}/full.json" '
  def changed: (.change.actions | type) != "array" or (.change.actions != ["no-op"] and .change.actions != ["read"]);
  # A value the failed plan knew (not null, so not unknown) that differs here.
  def drifts($want; $got):
    [$want | paths(scalars)] as $ps
    | any($ps[]; . as $p | ($want | getpath($p)) as $w
        | $w != null and ((try ($got | getpath($p)) catch null) != $w));
  if (type != "object") or (has("format_version") | not) then error("the targeted plan is not a plan") else . end
  | (($full[0].resource_changes // []) | map({key: .address, value: .}) | from_entries) as $was
  | [(.resource_changes // [])[] | select(changed)
     | . as $c | ($was[$c.address] // null) as $w
     | if ($types | index($c.type)) == null then "\($c.address): a \($c.type) is not a Cloud Run service or job"
       elif $c.change.actions != ["update"] then "\($c.address): \($c.change.actions | join("+")) is not an in-place update"
       elif $w == null or $w.change.actions != ["update"] then "\($c.address): the failed plan did not update it"
       elif drifts($w.change.after; $c.change.after) then "\($c.address): it sets values the failed plan did not"
       else empty end]
  | .[]' "${WORK}/services.json" >"${WORK}/refusals.txt"; then
  finish failed "the targeted plan could not be judged, so it was not applied"
fi
if [[ -s "${WORK}/refusals.txt" ]]; then
  refusals=()
  while IFS= read -r line; do refusals+=("${line}"); done <"${WORK}/refusals.txt"
  finish failed "the targeted plan changes more than the Cloud Run image updates, so it was not applied:" \
    "${refusals[@]}"
fi
if ! "${SWARM_LIB_DIR}/plan-guard.sh" --plan "${WORK}/services.json" --mode apply; then
  finish failed "the shared-project guard refused the targeted plan, so it was not applied"
fi
iam="$("${SWARM_LIB_DIR}/plan-guard.sh" --plan "${WORK}/services.json" --classify-iam --out "${WORK}/iam.json" || true)"
if [[ "${iam}" != "false" ]]; then
  finish failed "the targeted plan changes IAM (or could not be classified), so it was not applied"
fi

jq -r '[(.resource_changes // [])[] | select(.change.actions == ["update"]) | .address] | .[]' \
  "${WORK}/services.json" >"${WORK}/updating.txt"
if [[ ! -s "${WORK}/updating.txt" ]]; then
  finish services "every Cloud Run service and job was already on this release's images: the full apply updated them before it failed"
fi

# ---------------------------------------------------------------------------
# 4. Apply it, and report.
# ---------------------------------------------------------------------------
updated=()
while IFS= read -r line; do updated+=("${line}"); done <"${WORK}/updating.txt"
if ! terraform -chdir="${CHDIR}" apply -input=false -lock-timeout="${LOCK_TIMEOUT}" -no-color "${WORK}/services.tfplan"; then
  finish failed "the targeted apply of the Cloud Run services and jobs failed too"
fi
finish services "the Cloud Run services and jobs were updated to this release's images (${#updated[@]}):" \
  "${updated[@]}"
