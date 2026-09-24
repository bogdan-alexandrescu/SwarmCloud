#!/usr/bin/env bash
# One-time (and safely repeatable) preparation of a project for the swarm.
#
# Everything here is idempotent and additive. Nothing in this script deletes or
# reconfigures anything that already exists, because saga-agents-staging is
# shared: another team's GKE cluster, VPC and service accounts live alongside us.
#
# Steps:
#   1. prerequisites
#   2. .env from .env.example, if absent
#   3. Terraform state bucket (created only if missing; versioning + UBLA)
#   4. terraform/bootstrap apply, when Track C has provided it
#   5. terraform init for the selected environment
#
# Usage: scripts/bootstrap.sh [--environment dev] [--skip-prereq] [--yes]
#                             [--target ADDRESS]...
#
# --target ADDRESS (repeatable) limits step 4's plan, and so its apply, to that
# resource in terraform/bootstrap. For applying one change while another pending
# change in the same root waits for its own window -- docs/ci.md names the case.
# It goes through the same pinned terraform, init and typed "apply" as the rest.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SKIP_PREREQ=0
# Passed to the bootstrap plan as-is; expanded with the bash 3.2 empty-array
# guard below. BOOTSTRAP_TARGET_NOTE is the same list for people to read.
BOOTSTRAP_TARGETS=("-target=google_logging_log_view.verify") # MUTATION
BOOTSTRAP_TARGET_NOTE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment|-e) ENVIRONMENT="$2"; shift 2 ;;
    --skip-prereq)    SKIP_PREREQ=1; shift ;;
    --yes|-y)         export SWARM_ASSUME_YES=1; shift ;;
    --target)
      [[ $# -ge 2 && -n "${2:-}" && "${2:-}" != -* ]] \
        || die "--target needs a resource address, e.g. --target google_logging_log_view.verify"
      BOOTSTRAP_TARGETS+=("-target=$2")
      BOOTSTRAP_TARGET_NOTE="${BOOTSTRAP_TARGET_NOTE} $2"
      shift 2 ;;
    -h|--help)        sed -n '2,22p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
export ENVIRONMENT
TF_STATE_PREFIX="infra/${ENVIRONMENT}"

require_cmd gcloud jq curl

step "Environment"
info "project      ${PROJECT_ID}"
info "region       ${REGION}"
info "environment  ${ENVIRONMENT}"
info "state        gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX}"

if [[ "${SKIP_PREREQ}" -eq 0 ]]; then
  step "Prerequisites"
  "${REPO_ROOT}/scripts/prerequisites.sh"
fi

step "Local configuration"
if [[ -f "${REPO_ROOT}/.env" ]]; then
  ok ".env already present (left untouched)"
else
  cp "${REPO_ROOT}/.env.example" "${REPO_ROOT}/.env"
  ok "created .env from .env.example -- review it before applying anything"
fi

step "Terraform state bucket"
if gcloud storage buckets describe "gs://${TF_STATE_BUCKET}" \
     --project "${PROJECT_ID}" --format='value(name)' >/dev/null 2>&1; then
  ok "gs://${TF_STATE_BUCKET} exists"
  if is_shared_resource "${TF_STATE_BUCKET}"; then
    warn "this bucket is shared with other teams; the swarm only writes under the '${TF_STATE_PREFIX}' prefix"
  fi
else
  info "creating gs://${TF_STATE_BUCKET}"
  gcloud storage buckets create "gs://${TF_STATE_BUCKET}" \
    --project "${PROJECT_ID}" \
    --location "${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
  gcloud storage buckets update "gs://${TF_STATE_BUCKET}" --versioning
  gcloud storage buckets update "gs://${TF_STATE_BUCKET}" \
    --update-labels="managed-by=swarm-bootstrap,component=tfstate,environment=${ENVIRONMENT}"
  ok "created gs://${TF_STATE_BUCKET} with versioning and uniform access"
fi

# State is the one thing whose loss is unrecoverable-by-replay: without it,
# terraform no longer knows which resources in a SHARED project are ours, and
# `make destroy`'s label assertion has nothing to assert against.
#
# THREE ANSWERS, for the same reason `_shared_probe` in lib/common.sh has three.
# This was `gcloud ... 2>/dev/null | grep -qi true`, which reports a bucket
# whose describe FAILED -- an expired session, a missing permission, the API not
# enabled -- as "object versioning is OFF ... a corrupted state file would be
# unrecoverable". That is a claim about the bucket made on the strength of never
# having read it, and TF_STATE_BUCKET is overridable: the branch above exists
# precisely because it can be pointed at a bucket this repository shares with
# another team, so the false sentence can be printed about theirs.
#
# Not routed through `_shared_probe`: that answers "does it exist", and the
# question here is the value of one field on a bucket that does exist. Kept
# local to its single call site rather than added to common.sh as a second
# almost-the-same helper.
VERSIONING_ERR="$(mktemp "${TMPDIR:-/tmp}/swarm-versioning-err.XXXXXX")"
if VERSIONING_ENABLED="$(gcloud storage buckets describe "gs://${TF_STATE_BUCKET}" \
     --project "${PROJECT_ID}" --format='value(versioning.enabled)' 2>"${VERSIONING_ERR}")"; then
  # `tr`, not `${var,,}`: bash 3.2 has no case modification.
  case "$(printf '%s' "${VERSIONING_ENABLED}" | tr '[:upper:]' '[:lower:]')" in
    true)
      ok "object versioning is on (state history is recoverable)"
      ;;
    *)
      warn "object versioning is OFF on gs://${TF_STATE_BUCKET}; a corrupted state file would be unrecoverable"
      ;;
  esac
else
  warn "could NOT read the versioning setting of gs://${TF_STATE_BUCKET}."
  warn "This is not evidence that versioning is off and not evidence that it is on."
  redact <"${VERSIONING_ERR}" | head -n 3 | sed 's/^/     /' >&2
fi
rm -f "${VERSIONING_ERR}"

step "Bootstrap layer"
BOOTSTRAP_DIR="${REPO_ROOT}/terraform/bootstrap"
if compgen -G "${BOOTSTRAP_DIR}/*.tf" >/dev/null; then
  info "applying terraform/bootstrap"
  if [[ -n "${BOOTSTRAP_TARGET_NOTE}" ]]; then
    # Said before the plan and again at the prompt: a targeted plan shows only
    # what was named, so the owner must know the rest of the root is pending.
    warn "TARGETED: this plan covers only${BOOTSTRAP_TARGET_NOTE}"
    warn "everything else pending in terraform/bootstrap is left for a later, untargeted run"
  fi
  tf -chdir="${BOOTSTRAP_DIR}" init -upgrade -input=false
  tf -chdir="${BOOTSTRAP_DIR}" plan -input=false -out="${BUILD_DIR}/bootstrap.tfplan" \
    -var="project_id=${PROJECT_ID}" -var="region=${REGION}" \
    ${BOOTSTRAP_TARGETS[@]+"${BOOTSTRAP_TARGETS[@]}"}
  if [[ -n "${BOOTSTRAP_TARGET_NOTE}" ]]; then
    confirm "About to apply ONLY${BOOTSTRAP_TARGET_NOTE} from the bootstrap layer to ${PROJECT_ID}." "apply"
  else
    confirm "About to apply the bootstrap layer to ${PROJECT_ID}." "apply"
  fi
  tf -chdir="${BOOTSTRAP_DIR}" apply -input=false "${BUILD_DIR}/bootstrap.tfplan"
  ok "bootstrap layer applied"
else
  info "terraform/bootstrap has no .tf files yet; the state bucket above is all the bootstrap this needs"
fi

step "Terraform init (${ENVIRONMENT})"
TF_ROOT="$(tf_root)"
tf_var_file >/dev/null   # fail now, loudly, if this environment has no inputs
if compgen -G "${TF_ROOT}/*.tf" >/dev/null; then
  # One root, one state prefix per environment. Re-initialising against a
  # different prefix is how two environments end up sharing state, so the
  # prefix is derived from ENVIRONMENT and never passed in by hand.
  tf -chdir="${TF_ROOT}" init -input=false -upgrade -reconfigure \
    -backend-config="bucket=${TF_STATE_BUCKET}" \
    -backend-config="prefix=${TF_STATE_PREFIX}"
  ok "terraform initialised: ${TF_ROOT} -> gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX}"
else
  warn "${TF_ROOT} has no .tf files yet; skipping init"
fi

hr
ok "bootstrap complete"
cat >&2 <<'NEXT'
Next:
  1. edit .env                       (tenants, budgets, API audience)
  2. make tf-plan                    (review every create against a shared project)
  3. make tf-apply
  4. make build push deploy
  5. make smoke
NEXT
