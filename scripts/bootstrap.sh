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
#   4. terraform/bootstrap apply, against its remote state in the bucket
#      above (gs://<bucket>/bootstrap). REFUSED when that state is empty while
#      the deployer service account already exists: see "Bootstrap layer".
#   5. terraform init for the selected environment
#
# Usage: scripts/bootstrap.sh [--environment dev] [--skip-prereq] [--yes]
#                             [--target ADDRESS]...
#        scripts/bootstrap.sh --migrate-state [--skip-prereq]
#
# --migrate-state is ONE-TIME (#827): it copies a local
# terraform/bootstrap/terraform.tfstate into gs://<bucket>/bootstrap with
# `terraform init -migrate-state`, after a typed confirmation that ignores
# --yes and SWARM_ASSUME_YES, and stops. It refuses when this checkout holds no
# local state file, or when the bucket already holds a bootstrap state. Run it
# from the checkout that holds the file; docs/operations.md has the command.
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
MIGRATE_STATE=0
# Passed to the bootstrap plan as-is; expanded with the bash 3.2 empty-array
# guard below. BOOTSTRAP_TARGET_NOTE is the same list for people to read.
BOOTSTRAP_TARGETS=()
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
    --migrate-state)  MIGRATE_STATE=1; shift ;;
    -h|--help)        sed -n '2,32p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
export ENVIRONMENT
TF_STATE_PREFIX="infra/${ENVIRONMENT}"

if [[ "${MIGRATE_STATE}" -eq 1 ]]; then
  # A targeted migration means nothing: state moves whole or not at all.
  [[ -z "${BOOTSTRAP_TARGET_NOTE}" ]] || die "--migrate-state moves the whole bootstrap state; it takes no --target"
  # Moving the one copy of the deployer's state is never pre-approved. --yes
  # set this a few lines up; an inherited value is refused the same way.
  if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
    warn "ignoring --yes / SWARM_ASSUME_YES: --migrate-state always requires a typed confirmation"
    unset SWARM_ASSUME_YES
  fi
fi

require_cmd gcloud jq curl

step "Environment"
info "project      ${PROJECT_ID}"
info "region       ${REGION}"
info "environment  ${ENVIRONMENT}"
info "state        gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX}"
info "bootstrap    gs://${TF_STATE_BUCKET}/${TF_BOOTSTRAP_STATE_PREFIX}"

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
#
# The field is `versioning_enabled`, not `versioning.enabled`. `gcloud storage`
# flattens it; the dotted path is the older `gsutil`/JSON-API shape and
# `gcloud storage` answers it with an empty string. Until 2026-09-25 this read
# the dotted path, fell into the catch-all, and told every bootstrap run
# "object versioning is OFF" about a bucket whose versioning_enabled is True.
# That is the same claim-without-a-read the paragraph above exists to prevent,
# so only an explicit False is reported as off; anything else is "could not
# tell". Measured the same day: `gcloud storage buckets list` prints True or
# False for every bucket this repository manages, never nothing.
VERSIONING_ERR="$(mktemp "${TMPDIR:-/tmp}/swarm-versioning-err.XXXXXX")"
if VERSIONING_ENABLED="$(gcloud storage buckets describe "gs://${TF_STATE_BUCKET}" \
     --project "${PROJECT_ID}" --format='value(versioning_enabled)' 2>"${VERSIONING_ERR}")"; then
  # `tr`, not `${var,,}`: bash 3.2 has no case modification.
  case "$(printf '%s' "${VERSIONING_ENABLED}" | tr '[:upper:]' '[:lower:]')" in
    true)
      ok "object versioning is on (state history is recoverable)"
      ;;
    false)
      warn "object versioning is OFF on gs://${TF_STATE_BUCKET}; a corrupted state file would be unrecoverable"
      ;;
    *)
      warn "gcloud read gs://${TF_STATE_BUCKET} but returned no versioning_enabled value (got '${VERSIONING_ENABLED}')."
      warn "This is not evidence that versioning is off and not evidence that it is on."
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
# The pre-#827 state: a local file, in whichever checkout last applied this
# root. terraform no longer reads it once the gcs backend is initialised; it is
# looked at only to migrate it, and to say so when a checkout still holds one.
BOOTSTRAP_LOCAL_STATE="${BOOTSTRAP_DIR}/terraform.tfstate"
# The object the gcs backend writes for the default workspace.
BOOTSTRAP_STATE_URL="gs://${TF_STATE_BUCKET}/${TF_BOOTSTRAP_STATE_PREFIX}/default.tfstate"
# Bucket from the environment, prefix from backend.tf -- the same split as
# terraform/infra, whose backend block names neither. -reconfigure for the same
# reason as the environment init below: a checkout's .terraform/ must never
# decide which state this root reads.
BOOTSTRAP_BACKEND_ARGS=(-backend-config="bucket=${TF_STATE_BUCKET}")
# google_service_account.deployer in terraform/bootstrap/wif.tf:
# "${var.name_prefix}-tf-deployer", name_prefix "swarm".
BOOTSTRAP_DEPLOYER="$(guard_name_prefix)tf-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

# How many resource instances a local state file holds; 0 for a file with none.
# `terraform state list` prints one line per instance, data sources included,
# and so does this, so the two counts are comparable after a migration.
bootstrap_local_instances() {
  jq '[.resources[]?.instances[]?] | length' "${BOOTSTRAP_LOCAL_STATE}" \
    || die "cannot read ${BOOTSTRAP_LOCAL_STATE} as a terraform state file"
}

# bootstrap_state_list FILE -- the remote state's addresses into FILE, one per
# line. An empty FILE is an empty state. terraform answers a backend with no
# object yet with "No state file was found!" and exit 1; that, and only that,
# is read as empty. Any other failure is fatal: a state that could not be read
# is not evidence that it is empty.
bootstrap_state_list() {
  local file="$1" errfile rc=0
  errfile="$(mktemp "${TMPDIR:-/tmp}/swarm-bootstrap-state-err.XXXXXX")"
  tf -chdir="${BOOTSTRAP_DIR}" state list >"${file}" 2>"${errfile}" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    if grep -q 'No state file was found' "${errfile}"; then
      : >"${file}"
    else
      err "could not list the bootstrap state at ${BOOTSTRAP_STATE_URL}:"
      redact <"${errfile}" | head -n 5 | sed 's/^/     /' >&2
      rm -f "${errfile}"
      die "refusing to plan the bootstrap layer against a state that could not be read"
    fi
  fi
  rm -f "${errfile}"
}

if [[ "${MIGRATE_STATE}" -eq 1 ]]; then
  # ONE-TIME (#827). Copies the local state into the bucket and stops; the next
  # `make bootstrap`, from any checkout, plans against the copy.
  [[ -f "${BOOTSTRAP_LOCAL_STATE}" ]] \
    || die "no ${BOOTSTRAP_LOCAL_STATE} in this checkout: run --migrate-state from the checkout that holds the bootstrap layer's local state"
  LOCAL_INSTANCES="$(bootstrap_local_instances)"
  LOCAL_SERIAL="$(jq -r '.serial // "unknown"' "${BOOTSTRAP_LOCAL_STATE}")"
  [[ "${LOCAL_INSTANCES}" -gt 0 ]] \
    || die "${BOOTSTRAP_LOCAL_STATE} holds no resources; there is nothing to migrate"
  info "local state   ${BOOTSTRAP_LOCAL_STATE}: serial ${LOCAL_SERIAL}, ${LOCAL_INSTANCES} resource instances"

  # Never over an existing remote state: -force-copy below would replace it,
  # and a checkout's local file is OLDER than a state the bucket already holds.
  REMOTE_RC=0
  shared_resource_present "the bootstrap state ${BOOTSTRAP_STATE_URL}" \
    gcloud storage objects describe "${BOOTSTRAP_STATE_URL}" --project "${PROJECT_ID}" \
    || REMOTE_RC=$?
  case "${REMOTE_RC}" in
    0) die "${BOOTSTRAP_STATE_URL} already exists, so the migration has been done. This checkout's local file is stale: move it aside and run scripts/bootstrap.sh without --migrate-state." ;;
    1) ok "${BOOTSTRAP_STATE_URL} does not exist yet" ;;
    *) die "refusing to migrate without knowing whether ${BOOTSTRAP_STATE_URL} already exists" ;;
  esac

  # A copy outside terraform's reach, before terraform touches the file.
  # build/ is gitignored; state holds resource ids and some sensitive values.
  BOOTSTRAP_BACKUP="${BUILD_DIR}/bootstrap-local-$(date -u +%Y%m%dT%H%M%SZ).tfstate"
  (umask 077 && cp "${BOOTSTRAP_LOCAL_STATE}" "${BOOTSTRAP_BACKUP}")
  ok "backed up the local state to ${BOOTSTRAP_BACKUP}"

  confirm "About to copy the bootstrap layer's local state (serial ${LOCAL_SERIAL}, ${LOCAL_INSTANCES} resource instances) into ${BOOTSTRAP_STATE_URL}. Every checkout plans against that copy from then on." "migrate"
  # -force-copy answers terraform's own "copy existing state?" prompt; the
  # typed "migrate" above and the existence check are the questions it asks.
  tf -chdir="${BOOTSTRAP_DIR}" init -input=false -migrate-state -force-copy \
    "${BOOTSTRAP_BACKEND_ARGS[@]}"

  MIGRATED_LIST="$(mktemp "${TMPDIR:-/tmp}/swarm-bootstrap-state.XXXXXX")"
  bootstrap_state_list "${MIGRATED_LIST}"
  REMOTE_INSTANCES="$(wc -l <"${MIGRATED_LIST}" | tr -d ' ')"
  rm -f "${MIGRATED_LIST}"
  if [[ "${REMOTE_INSTANCES}" -ne "${LOCAL_INSTANCES}" ]]; then
    die "${BOOTSTRAP_STATE_URL} lists ${REMOTE_INSTANCES} resource instances, the local state held ${LOCAL_INSTANCES}. Do NOT plan or apply; the original is backed up at ${BOOTSTRAP_BACKUP}."
  fi
  ok "migrated: ${BOOTSTRAP_STATE_URL} lists the same ${REMOTE_INSTANCES} resource instances"
  hr
  cat >&2 <<'NEXT'
Next:
  1. scripts/bootstrap.sh --environment dev    (from any checkout; expect no
                                                creates of the deployer)
  2. once that plan is clean, move terraform/bootstrap/terraform.tfstate and
     terraform.tfstate.backup out of this checkout. terraform no longer reads
     them; the copy in build/ is the rollback.
NEXT
  exit 0
fi

if compgen -G "${BOOTSTRAP_DIR}/*.tf" >/dev/null; then
  info "applying terraform/bootstrap (state ${BOOTSTRAP_STATE_URL})"
  if [[ -n "${BOOTSTRAP_TARGET_NOTE}" ]]; then
    # Said before the plan and again at the prompt: a targeted plan shows only
    # what was named, so the owner must know the rest of the root is pending.
    warn "TARGETED: this plan covers only${BOOTSTRAP_TARGET_NOTE}"
    warn "everything else pending in terraform/bootstrap is left for a later, untargeted run"
  fi
  tf -chdir="${BOOTSTRAP_DIR}" init -upgrade -input=false -reconfigure \
    "${BOOTSTRAP_BACKEND_ARGS[@]}"

  # THE EMPTY-STATE GUARD (#827). An empty bootstrap state plans every resource
  # in this root as a create -- on 2026-10-07, from a worktree, that was "9 to
  # import, 91 to add", the live deployer and all its grants. An empty state is
  # right only on a project that has never been bootstrapped, and the deployer
  # service account is the measurement of that: this root creates it, and
  # nothing else does.
  STATE_LIST="$(mktemp "${TMPDIR:-/tmp}/swarm-bootstrap-state.XXXXXX")"
  bootstrap_state_list "${STATE_LIST}"
  LOCAL_HELD=0
  if [[ -f "${BOOTSTRAP_LOCAL_STATE}" ]]; then
    LOCAL_HELD="$(bootstrap_local_instances)"
  fi
  if [[ -s "${STATE_LIST}" ]]; then
    ok "bootstrap state lists $(wc -l <"${STATE_LIST}" | tr -d ' ') resource instances"
    if [[ "${LOCAL_HELD}" -gt 0 ]]; then
      warn "${BOOTSTRAP_LOCAL_STATE} is a pre-migration copy; terraform does not read it. Move it out of this checkout."
    fi
  else
    if [[ "${LOCAL_HELD}" -gt 0 ]]; then
      rm -f "${STATE_LIST}"
      err "the bootstrap state at ${BOOTSTRAP_STATE_URL} is EMPTY, and this checkout holds a local one"
      err "(${BOOTSTRAP_LOCAL_STATE}, ${LOCAL_HELD} resource instances). Migrate it first, once:"
      err "    scripts/bootstrap.sh --migrate-state"
      die "refusing to plan the bootstrap layer against an empty state"
    fi
    DEPLOYER_RC=0
    shared_resource_present "${BOOTSTRAP_DEPLOYER}" \
      gcloud iam service-accounts describe "${BOOTSTRAP_DEPLOYER}" \
        --project "${PROJECT_ID}" --format='value(email)' \
      || DEPLOYER_RC=$?
    case "${DEPLOYER_RC}" in
      0)
        rm -f "${STATE_LIST}"
        err "the bootstrap state at ${BOOTSTRAP_STATE_URL} is EMPTY, but ${BOOTSTRAP_DEPLOYER} exists."
        err "This project has been bootstrapped; its state is somewhere else, most likely a local"
        err "terraform/bootstrap/terraform.tfstate in the checkout that last applied it. A plan from here"
        err "would offer to re-create the live deployer and every grant it holds."
        err "Migrate that state first, once, from that checkout:"
        err "    scripts/bootstrap.sh --migrate-state"
        err "(docs/operations.md, \"The bootstrap layer's state\")"
        die "refusing to plan the bootstrap layer against an empty state while the deployer exists"
        ;;
      1)
        info "empty bootstrap state and no ${BOOTSTRAP_DEPLOYER}: a project never bootstrapped, so everything is a create"
        ;;
      *)
        rm -f "${STATE_LIST}"
        die "refusing to plan against an empty bootstrap state without knowing whether ${BOOTSTRAP_DEPLOYER} exists"
        ;;
    esac
  fi
  rm -f "${STATE_LIST}"

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
