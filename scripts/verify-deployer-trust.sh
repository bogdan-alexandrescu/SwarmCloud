#!/usr/bin/env bash
# Does the LIVE deployer trust policy match terraform/bootstrap/wif.tf? (#457)
#
# Read-only. It reads two terraform outputs and two gcloud resources and writes
# nothing to IAM, so it takes no confirmation.
#
# WHY THIS EXISTS. The deployer service account used to trust
# `attribute.repo_ref/<repo>@<ref>` -- a principalSet EVERY workflow on main
# presents -- so a merged test or conftest in any workflow that granted itself
# id-token could become the deployer (#457, docs/merge-step.md M4/R4). #465
# moved the grants to job level and pinned the binding to
# `attribute.job_workflow_ref` for the files in wif.tf `deployer_workflows`.
# The pin is IAM in the bootstrap root, which only the owner applies, and
# nothing in the code can say whether that apply has happened. This reads it.
#
# It fails, naming each member, when the deployer's roles/iam.workloadIdentityUser
# members hold:
#   * a repo_ref, repository or subject member -- the pre-apply binding;
#   * a job_workflow_ref (or anything else) that github_principals does not
#     render -- a workflow file the deployer should refuse;
# or lack one github_principals renders -- that file's auth step fails on main
# with iam.serviceAccounts.getAccessToken denied. It also fails when there are
# NO members, or terraform renders none: an empty comparison is not a pass. And
# it reads the provider's attribute_condition and fails if it admits
# `refs/pull/`, the boundary tests/terraform/bootstrap.tftest.hcl asserts on
# the rendered condition, or is empty.
#
# THE EXPECTED SET IS TERRAFORM'S, never a list here: github_principals, read
# from the bootstrap root's remote state. A second list in shell is how the two
# drift (tests/unit/scripts/test_verify_deployer_trust.py holds this file to
# naming no workflow file).
#
# Usage: scripts/verify-deployer-trust.sh
# Runbook: docs/runbooks/deployer-trust-pin.md

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

case "${1:-}" in
  -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
  "") ;;
  *) die "unknown argument: $1" ;;
esac

require_cmd gcloud jq

ROLE="roles/iam.workloadIdentityUser"
BOOTSTRAP_DIR="${REPO_ROOT}/terraform/bootstrap"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-verify-trust.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# run_quiet LABEL CMD... -- CMD's stdout to ${WORK}/out, its stderr redacted on
# failure. Not a command substitution: the caller reads the file.
run_quiet() {
  local label="$1" rc=0
  shift
  "$@" >"${WORK}/out" 2>"${WORK}/err" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    err "${label} failed (exit ${rc}):"
    redact <"${WORK}/err" | head -n 20 | sed 's/^/     /' >&2
    die "nothing was compared; this is not a verdict on the trust policy"
  fi
}

step "Expected (terraform/bootstrap, state gs://${TF_STATE_BUCKET}/${TF_BOOTSTRAP_STATE_PREFIX})"
# The same pinned terraform and backend arguments scripts/bootstrap.sh uses,
# minus -upgrade: a read must not rewrite the committed .terraform.lock.hcl.
run_quiet "terraform init (terraform/bootstrap)" \
  tf -chdir="${BOOTSTRAP_DIR}" init -input=false -reconfigure \
  -backend-config="bucket=${TF_STATE_BUCKET}"

run_quiet "terraform output github_deployer_service_account" \
  tf -chdir="${BOOTSTRAP_DIR}" output -raw github_deployer_service_account
DEPLOYER="$(cat "${WORK}/out")"
[[ -n "${DEPLOYER}" ]] || die "github_deployer_service_account is empty: enable_github_wif is off, or this is not the bootstrap state"

run_quiet "terraform output github_workload_identity_provider" \
  tf -chdir="${BOOTSTRAP_DIR}" output -raw github_workload_identity_provider
PROVIDER="$(cat "${WORK}/out")"

run_quiet "terraform output github_principals" \
  tf -chdir="${BOOTSTRAP_DIR}" output -json github_principals
jq -r '.[]' "${WORK}/out" | LC_ALL=C sort -u >"${WORK}/expected"
EXPECTED_COUNT="$(wc -l <"${WORK}/expected" | tr -d ' ')"
[[ "${EXPECTED_COUNT}" -gt 0 ]] \
  || die "terraform renders no github_principals: there is nothing to compare the live policy against"
info "deployer   ${DEPLOYER}"
info "expected   ${EXPECTED_COUNT} members from github_principals"

step "Live trust policy"
run_quiet "gcloud iam service-accounts get-iam-policy" \
  gcloud iam service-accounts get-iam-policy "${DEPLOYER}" --format=json
# Every binding on the role, conditioned or not: a conditioned grant still
# admits whoever its condition lets through.
jq -r --arg role "${ROLE}" \
  '[.bindings[]? | select(.role == $role) | .members[]?] | .[]' "${WORK}/out" \
  | LC_ALL=C sort -u >"${WORK}/live"
LIVE_COUNT="$(wc -l <"${WORK}/live" | tr -d ' ')"
info "live       ${LIVE_COUNT} ${ROLE} members"

FAILED=0
PRE_APPLY=0

while IFS= read -r member; do
  [[ -n "${member}" ]] || continue
  if grep -qxF -- "${member}" "${WORK}/expected"; then
    ok "${member}"
    continue
  fi
  FAILED=1
  case "${member}" in
    */attribute.repo_ref/*)
      PRE_APPLY=1
      err "repo_ref member: ${member}"
      err "  every workflow on that ref presents it, whatever file it runs" ;;
    */attribute.repository/*)
      PRE_APPLY=1
      err "repository member: ${member}"
      err "  every workflow on every ref of the repository presents it" ;;
    principal://*/subject/*)
      PRE_APPLY=1
      err "subject member: ${member}"
      err "  a sub names a ref or an environment, not a workflow file" ;;
    */attribute.job_workflow_ref/*)
      err "unexpected workflow: ${member}"
      err "  a workflow file or ref not in github_principals; the deployer must refuse it" ;;
    *)
      err "unexpected member: ${member}"
      err "  github_principals does not render it" ;;
  esac
done <"${WORK}/live"

while IFS= read -r member; do
  [[ -n "${member}" ]] || continue
  if ! grep -qxF -- "${member}" "${WORK}/live"; then
    FAILED=1
    err "missing: ${member}"
    err "  that workflow's auth step fails on its ref with iam.serviceAccounts.getAccessToken denied"
  fi
done <"${WORK}/expected"

if [[ "${LIVE_COUNT}" -eq 0 ]]; then
  FAILED=1
  err "the deployer holds no ${ROLE} members at all: nothing authenticates as it, and nothing was checked"
fi

step "Provider attribute condition"
if [[ -z "${PROVIDER}" ]]; then
  FAILED=1
  err "github_workload_identity_provider is empty; the provider's condition was not read"
else
  # projects/<number>/locations/<loc>/workloadIdentityPools/<pool>/providers/<id>
  # The resource name only ever carries the project NUMBER, and describe
  # refuses a number ("set it to PROJECT ID instead", #980). The pool lives in
  # var.project_id (terraform/bootstrap/wif.tf), which is common.sh's
  # PROJECT_ID, so the number is discarded and PROJECT_ID is passed.
  IFS=/ read -r _ _ _ P_LOCATION _ P_POOL _ P_ID <<<"${PROVIDER}"
  [[ -n "${P_ID:-}" ]] || die "cannot parse the provider name '${PROVIDER}'"
  run_quiet "gcloud iam workload-identity-pools providers describe" \
    gcloud iam workload-identity-pools providers describe "${P_ID}" \
    --workload-identity-pool="${P_POOL}" --location="${P_LOCATION}" \
    --project="${PROJECT_ID}" --format=json
  CONDITION="$(jq -r '.attributeCondition // ""' "${WORK}/out")"
  if [[ -z "${CONDITION}" ]]; then
    FAILED=1
    err "the provider has NO attribute condition: the pool trusts every repository on GitHub"
  elif [[ "${CONDITION}" == *refs/pull/* ]]; then
    FAILED=1
    err "the provider's condition admits refs/pull/: a pull request can mint a token"
    err "  ${CONDITION}"
  else
    ok "attribute condition admits no refs/pull/"
  fi
fi

if [[ "${FAILED}" -ne 0 ]]; then
  if [[ "${PRE_APPLY}" -ne 0 ]]; then
    err "the bootstrap apply has not happened: the live binding still trusts more than the listed workflow files."
    err "Apply it: scripts/bootstrap.sh --target 'google_service_account_iam_member.deployer_wif' (docs/runbooks/deployer-trust-pin.md)"
  fi
  die "the live deployer trust policy does not match terraform/bootstrap (${LIVE_COUNT} live, ${EXPECTED_COUNT} expected)"
fi

ok "checked ${LIVE_COUNT} members, all pinned to workflow files"
printf 'checked %s members, all pinned to workflow files\n' "${LIVE_COUNT}"
