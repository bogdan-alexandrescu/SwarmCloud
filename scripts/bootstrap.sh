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

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SKIP_PREREQ=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment|-e) ENVIRONMENT="$2"; shift 2 ;;
    --skip-prereq)    SKIP_PREREQ=1; shift ;;
    --yes|-y)         export SWARM_ASSUME_YES=1; shift ;;
    -h|--help)        sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
export ENVIRONMENT
TF_STATE_PREFIX="swarm/${ENVIRONMENT}"

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
if gcloud storage buckets describe "gs://${TF_STATE_BUCKET}" \
     --project "${PROJECT_ID}" --format='value(versioning.enabled)' 2>/dev/null | grep -qi true; then
  ok "object versioning is on (state history is recoverable)"
else
  warn "object versioning is OFF on gs://${TF_STATE_BUCKET}; a corrupted state file would be unrecoverable"
fi

step "Bootstrap layer"
BOOTSTRAP_DIR="${REPO_ROOT}/terraform/bootstrap"
if compgen -G "${BOOTSTRAP_DIR}/*.tf" >/dev/null; then
  info "applying terraform/bootstrap"
  tf -chdir="${BOOTSTRAP_DIR}" init -upgrade -input=false
  tf -chdir="${BOOTSTRAP_DIR}" plan -input=false -out="${BUILD_DIR}/bootstrap.tfplan" \
    -var="project_id=${PROJECT_ID}" -var="region=${REGION}"
  confirm "About to apply the bootstrap layer to ${PROJECT_ID}." "apply"
  tf -chdir="${BOOTSTRAP_DIR}" apply -input=false "${BUILD_DIR}/bootstrap.tfplan"
  ok "bootstrap layer applied"
else
  info "terraform/bootstrap has no .tf files yet; the state bucket above is all the bootstrap this needs"
fi

step "Terraform init (${ENVIRONMENT})"
ENV_DIR="$(env_dir)"
if compgen -G "${ENV_DIR}/*.tf" >/dev/null; then
  tf -chdir="${ENV_DIR}" init -input=false -upgrade \
    -backend-config="bucket=${TF_STATE_BUCKET}" \
    -backend-config="prefix=${TF_STATE_PREFIX}"
  ok "terraform initialised in ${ENV_DIR}"
else
  warn "${ENV_DIR} has no .tf files yet; skipping init"
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
