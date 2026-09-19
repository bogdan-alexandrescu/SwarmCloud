#!/usr/bin/env bash
# Shared library for every script in scripts/. Sourced, never executed directly.
#
# Two things in here exist because of this specific machine and this specific
# project, and removing them will break operators in ways that are hard to see:
#
#   1. kubectl is resolved EXPLICITLY. Three kubectl binaries are on this Mac and
#      the two that win $PATH lookup are 1.22 (EKS) and 1.25 (Docker Desktop),
#      both far outside the supported skew for a 1.35 control plane. A 1.22
#      client against a 1.35 server does not fail loudly -- it silently drops
#      fields it does not understand from manifests it applies.
#   2. Every shared resource in saga-agents-staging that belongs to another team
#      is named here, once, in SHARED_DENY_LIST. destroy.sh, purge-data.sh and
#      configure-kubectl.sh all read it. One list, one place to keep correct.

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SWARM_LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SWARM_LIB_DIR}/../.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build"
export REPO_ROOT BUILD_DIR

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_DIM=""; C_BOLD=""
fi

log()  { printf '%s\n' "$*" >&2; }
info() { printf '%s==>%s %s\n' "${C_BLUE}" "${C_RESET}" "$*" >&2; }
step() { printf '\n%s== %s ==%s\n' "${C_BOLD}" "$*" "${C_RESET}" >&2; }
ok()   { printf '%s  ok%s %s\n' "${C_GREEN}" "${C_RESET}" "$*" >&2; }
warn() { printf '%swarn%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
err()  { printf '%s fail%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; }
dim()  { printf '%s%s%s\n' "${C_DIM}" "$*" "${C_RESET}" >&2; }
die()  { err "$*"; exit 1; }

hr() { printf '%s\n' "------------------------------------------------------------" >&2; }

# ---------------------------------------------------------------------------
# Telling an expired session apart from a missing resource
# ---------------------------------------------------------------------------
#
# A Workspace session policy expires the operator's gcloud credential on a
# timer. When it does, `gcloud auth print-access-token` still SUCCEEDS -- it
# returns a ya29. token with the right scopes -- and every API call then fails
# with UNAUTHENTICATED / ACCESS_TOKEN_TYPE_UNSUPPORTED. So checking that a token
# exists proves nothing; only a real call does.
#
# The cost of not distinguishing this was measured, not imagined. One expiry
# produced, in the same minute:
#
#   build-images.sh   "Artifact Registry repository swarm-images does not exist
#                      in us-central1. Run 'make infra' first"   (it existed)
#   push-images.sh    "not found in Artifact Registry -- build it first"
#                                                            (they were built)
#   create-secrets.sh died partway, storing nothing, saying nothing useful
#
# Each message sends someone at a different wrong problem, and `make infra`
# would have failed too, with a fourth unrelated message. Callers capture
# stderr and pass it here instead of guessing from an exit code.

gcloud_auth_failure() {
  case "$1" in
    *ACCESS_TOKEN_TYPE_UNSUPPORTED*|*UNAUTHENTICATED*) return 0 ;;
    *"invalid authentication credentials"*)            return 0 ;;
    *"Reauthentication required"*|*"reauth"*)          return 0 ;;
    *"credentials were not found"*)                    return 0 ;;
    *"Your current active account"*)                   return 0 ;;
  esac
  return 1
}

# Exit with the real diagnosis if the captured stderr is an auth failure;
# otherwise return so the caller can report its own, accurate, error.
die_if_auth_failure() {
  gcloud_auth_failure "$1" || return 0
  err "the gcloud session is not usable -- this is authentication, not a missing resource"
  printf '%s\n' "$1" | head -n 2 | sed 's/^/     /' >&2
  dim "a token still exists and still has the right scopes; the session behind it has expired"
  die "run: gcloud auth login && gcloud auth application-default login"
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# `.env` is SOURCED, not parsed. Sourcing it means every line in it is shell
# executed by every script here -- including destroy.sh and purge-data.sh, which
# read PROJECT_ID, GKE_CLUSTER and ENVIRONMENT from it before any deny-list
# check runs. So the file is treated as code, and code this repository runs must
# not be writable by anyone but its owner.
#
# This refuses rather than warns: a warning printed before `source` has already
# lost, because the next line runs the file anyway.
assert_env_file_is_trustworthy() {
  local env_file="$1" perms owner
  [[ -f "${env_file}" ]] || return 0
  # BSD stat (macOS) and GNU stat (CI) spell this differently.
  perms="$(stat -f '%Lp' "${env_file}" 2>/dev/null || stat -c '%a' "${env_file}" 2>/dev/null || echo "")"
  owner="$(stat -f '%u' "${env_file}" 2>/dev/null || stat -c '%u' "${env_file}" 2>/dev/null || echo "")"
  if [[ -n "${owner}" && -n "${EUID:-}" && "${owner}" != "${EUID}" ]]; then
    printf 'error: %s is owned by uid %s, not by you (%s). It is sourced as shell; refusing.\n' \
      "${env_file}" "${owner}" "${EUID}" >&2
    exit 1
  fi
  if [[ -n "${perms}" && "${perms}" =~ ^[0-7]?([0-7])([0-7])$ ]]; then
    local group="${BASH_REMATCH[1]}" other="${BASH_REMATCH[2]}"
    if (( (group & 2) != 0 || (other & 2) != 0 )); then
      printf 'error: %s is group- or world-writable (mode %s). It is sourced as shell by every\n' \
        "${env_file}" "${perms}" >&2
      printf '       script in scripts/, so anyone who can write it can run commands as you.\n' >&2
      printf '       Fix with: chmod 600 %s\n' "${env_file}" >&2
      exit 1
    fi
  fi
}

load_env() {
  local env_file="${SWARM_ENV_FILE:-${REPO_ROOT}/.env}"
  if [[ -f "${env_file}" ]]; then
    assert_env_file_is_trustworthy "${env_file}"
    set -a
    # shellcheck disable=SC1090  # path is operator-chosen by design
    source "${env_file}"
    set +a
  fi

  PROJECT_ID="${PROJECT_ID:-saga-agents-staging}"
  REGION="${REGION:-us-central1}"
  ZONE="${ZONE:-us-central1-a}"
  ENVIRONMENT="${ENVIRONMENT:-dev}"

  # Named database. NEVER (default): this project is shared and (default)
  # belongs to whoever gets there first.
  FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-swarm}"

  # `swarm-artifacts-<project>`, matching terraform/modules/storage:
  #   artifact_bucket_name = "${var.name_prefix}-artifacts-${var.bucket_suffix}"
  # This read `${PROJECT_ID}-swarm-artifacts` -- the same words in the wrong
  # order -- so every script that touched the bucket looked for one that has
  # never existed. register-tenant.sh reported "artifact bucket does not exist
  # yet; run make infra", which is advice that cannot help: `make infra` creates
  # the bucket under its real name, and the next run says the same thing.
  ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-swarm-artifacts-${PROJECT_ID}}"
  ARTIFACT_REGISTRY="${ARTIFACT_REGISTRY:-swarm-images}"
  # The swarm's OWN state bucket, created by terraform/bootstrap. Deliberately
  # not saga-agents-terraform-state-staging: that bucket belongs to another team
  # (it is on the deny-list below) and putting our state in it would make our
  # teardown depend on their retention policy.
  TF_STATE_BUCKET="${TF_STATE_BUCKET:-swarm-tfstate-${PROJECT_ID}}"
  TF_STATE_PREFIX="${TF_STATE_PREFIX:-infra/${ENVIRONMENT}}"

  # The swarm's own Autopilot cluster. agents-staging belongs to another team and
  # is deny-listed below; nothing here may ever target it.
  GKE_CLUSTER="${GKE_CLUSTER:-swarm-autopilot}"
  GKE_LOCATION="${GKE_LOCATION:-${REGION}}"

  PUBSUB_TOPIC="${PUBSUB_TOPIC:-swarm-scheduler-wake}"
  SCHEDULER_JOB="${SCHEDULER_JOB:-swarm-scheduler-tick}"

  API_SERVICE="${API_SERVICE:-swarm-api}"
  SCHEDULER_SERVICE="${SCHEDULER_SERVICE:-swarm-scheduler}"
  QUOTA_SERVICE="${QUOTA_SERVICE:-swarm-quota-broker}"
  RECONCILER_SERVICE="${RECONCILER_SERVICE:-swarm-reconciler}"

  API_PREFIX="${API_PREFIX:-/v1}"
  HTTP_TIMEOUT="${HTTP_TIMEOUT:-30}"

  IMAGE_HOST="${IMAGE_HOST:-${REGION}-docker.pkg.dev}"
  IMAGE_REPO="${IMAGE_REPO:-${IMAGE_HOST}/${PROJECT_ID}/${ARTIFACT_REGISTRY}}"

  export PROJECT_ID REGION ZONE ENVIRONMENT FIRESTORE_DATABASE ARTIFACT_BUCKET
  export ARTIFACT_REGISTRY TF_STATE_BUCKET TF_STATE_PREFIX GKE_CLUSTER GKE_LOCATION
  export PUBSUB_TOPIC SCHEDULER_JOB API_SERVICE SCHEDULER_SERVICE QUOTA_SERVICE RECONCILER_SERVICE
  export API_PREFIX HTTP_TIMEOUT IMAGE_HOST IMAGE_REPO

  mkdir -p "${BUILD_DIR}"

  # configure-kubectl.sh writes an isolated kubeconfig rather than merging into
  # the operator's default one. Pick it up here so every other script targets the
  # swarm cluster without the operator having to remember an export -- and
  # without any script ever inheriting a stray production context.
  if [[ -z "${KUBECONFIG:-}" && -f "${BUILD_DIR}/kubeconfig-${ENVIRONMENT}.yaml" ]]; then
    KUBECONFIG="${BUILD_DIR}/kubeconfig-${ENVIRONMENT}.yaml"
    export KUBECONFIG
  fi
}

# Resources owned by other teams in this shared project. Verified against the
# live project on 2026-09-15. Nothing this repo runs may delete, modify or even
# point kubectl at anything in this list.
SHARED_DENY_LIST=(
  "agents-staging"
  "agents-staging-vpc"
  "agents-staging-subnet"
  "gke-agents-staging-88280d02-pe-subnet"
  "default"
  "saga-agents-crawled-media-staging"
  "saga-agents-files-staging"
  "saga-agents-terraform-state-staging"
  "api-service@saga-agents-staging.iam.gserviceaccount.com"
  "publisher@saga-agents-staging.iam.gserviceaccount.com"
  "209012342332-compute@developer.gserviceaccount.com"
  "promptlab-deployer@saga-agents-staging.iam.gserviceaccount.com"
  "promptlab-runner@saga-agents-staging.iam.gserviceaccount.com"
  "aipipeline@saga-agents-staging.iam.gserviceaccount.com"
  "saga-storage-ro@saga-agents-staging.iam.gserviceaccount.com"
  "saga-storage-rw@saga-agents-staging.iam.gserviceaccount.com"
  "external-secrets@saga-agents-staging.iam.gserviceaccount.com"
  "staging-gke-nodes@saga-agents-staging.iam.gserviceaccount.com"
  "crawler@saga-agents-staging.iam.gserviceaccount.com"
  "tournament-digest@saga-agents-staging.iam.gserviceaccount.com"
)

is_shared_resource() {
  local candidate="$1" protected
  for protected in "${SHARED_DENY_LIST[@]}"; do
    [[ "${candidate}" == "${protected}" ]] && return 0
  done
  return 1
}

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

require_cmd() {
  local missing=0 cmd
  for cmd in "$@"; do
    command -v "${cmd}" >/dev/null 2>&1 || { err "required command not found: ${cmd}"; missing=1; }
  done
  [[ "${missing}" -eq 0 ]] || die "install the missing tools, or run: make prerequisites"
}

# First two components of a `vX.Y.Z` anywhere in the given string.
_semver_minor() {
  local raw="$1"
  if [[ "${raw}" =~ v?([0-9]+)\.([0-9]+) ]]; then
    printf '%s %s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

KUBECTL=""
# Resolve a kubectl new enough for a 1.35 control plane. The old binaries on this
# machine win $PATH, so PATH order is deliberately consulted LAST.
kubectl_bin() {
  if [[ -n "${KUBECTL}" ]]; then printf '%s' "${KUBECTL}"; return 0; fi

  local candidates=() c parsed major minor
  [[ -n "${SWARM_KUBECTL:-}" ]] && candidates+=("${SWARM_KUBECTL}")
  candidates+=("/opt/homebrew/bin/kubectl" "/usr/local/opt/kubernetes-cli/bin/kubectl")
  if c="$(command -v kubectl 2>/dev/null)"; then candidates+=("${c}"); fi

  for c in "${candidates[@]}"; do
    [[ -x "${c}" ]] || continue
    local ver
    ver="$("${c}" version --client 2>/dev/null | head -n 2 | tr '\n' ' ')" || continue
    parsed="$(_semver_minor "${ver}")" || continue
    read -r major minor <<<"${parsed}"
    if [[ "${major}" -gt 1 || ( "${major}" -eq 1 && "${minor}" -ge 30 ) ]]; then
      KUBECTL="${c}"
      printf '%s' "${KUBECTL}"
      return 0
    fi
  done

  err "no kubectl >= 1.30 found. Checked: ${candidates[*]}"
  err "this machine has kubectl 1.22 (EKS) and 1.25 (Docker Desktop) ahead of 1.36 on \$PATH."
  die "set SWARM_KUBECTL=/opt/homebrew/bin/kubectl, or install kubernetes-cli"
}

kc() {
  local bin
  bin="$(kubectl_bin)"
  "${bin}" "$@"
}

# Resolve a tool, preferring the pinned copy in ~/.local/bin over whatever wins
# $PATH. This is not cosmetic: on this workstation Homebrew's checkov 3.3.10 is
# broken (it raises on import) and shadows the working 3.3.17 in ~/.local/bin,
# exactly as three old kubectl binaries shadow 1.36.3.
prefer_local_bin() {
  local name="$1" override="${2:-}"
  if [[ -n "${override}" ]]; then printf '%s' "${override}"; return 0; fi
  if [[ -x "${HOME}/.local/bin/${name}" ]]; then printf '%s' "${HOME}/.local/bin/${name}"; return 0; fi
  command -v "${name}" 2>/dev/null || return 1
}

# The swarm's own contexts, and the only ones any script here may act through.
# This guard is not theoretical: on the reference workstation the ACTIVE context
# was gke_saga-agents-staging_us-central1-a_agents-staging -- another team's live
# cluster -- with gke_saga-agents-prod_us-central1_agents-prod sitting in the same
# kubeconfig. Creating a namespace through whatever context happened to be
# current would have put swarm objects in someone else's production.
kube_context_allowed() {
  local ctx="$1"
  [[ "${ctx}" == "swarm-${ENVIRONMENT}" ]] && return 0
  [[ "${ctx}" == "gke_${PROJECT_ID}_${GKE_LOCATION}_${GKE_CLUSTER}" ]] && return 0
  return 1
}

kube_current_context() {
  local bin
  bin="$(kubectl_bin 2>/dev/null)" || return 1
  "${bin}" config current-context 2>/dev/null
}

# True only when kubectl is pointed at the swarm's own cluster.
kube_context_is_swarm() {
  local ctx
  ctx="$(kube_current_context)" || return 1
  [[ -n "${ctx}" ]] || return 1
  # A context whose name ends in a deny-listed cluster is never acceptable, even
  # if someone renamed it to look like ours.
  local trailing="${ctx##*_}"
  is_shared_resource "${trailing}" && return 1
  kube_context_allowed "${ctx}"
}

assert_kube_context() {
  local ctx
  ctx="$(kube_current_context || true)"
  if kube_context_is_swarm; then
    return 0
  fi
  err "kubectl is pointed at '''${ctx:-<none>}''', which is not the swarm cluster."
  err "Expected 'swarm-${ENVIRONMENT}' or 'gke_${PROJECT_ID}_${GKE_LOCATION}_${GKE_CLUSTER}'."
  err "Other teams''' clusters -- including production -- are reachable from this kubeconfig."
  die "run scripts/configure-kubectl.sh first; it writes an isolated kubeconfig for the swarm cluster"
}

# A python that has the platform's own dependencies.
#
# Bare `python3` is the system interpreter and does not have google-cloud-*
# installed, so a script reaching for it gets ModuleNotFoundError halfway
# through -- after the side effects it already performed. Measured: the account
# onboarding stored a credential in Secret Manager and then failed to register
# the account, leaving a secret with nothing pointing at it.
swarm_python() {
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    printf '%s' "${REPO_ROOT}/.venv/bin/python"
    return 0
  fi
  if command -v uv >/dev/null 2>&1; then
    printf 'uv run --project %s python' "${REPO_ROOT}"
    return 0
  fi
  die "no project python found. Run: uv sync"
}

terraform_bin() {
  prefer_local_bin terraform "${SWARM_TERRAFORM:-}" \
    || die "terraform not found; run: make prerequisites"
}

tflint_bin()  { prefer_local_bin tflint  "${SWARM_TFLINT:-}"; }
checkov_bin() { prefer_local_bin checkov "${SWARM_CHECKOV:-}"; }
trivy_bin()   { prefer_local_bin trivy   "${SWARM_TRIVY:-}"; }

tf() {
  local bin
  bin="$(terraform_bin)"
  "${bin}" "$@"
}

# There is ONE terraform root. Environments are tfvars files against it, not
# roots of their own -- so `terraform plan` for dev and prod run identical
# configuration and differ only in inputs and state prefix. A root per
# environment is how prod quietly drifts from dev.
tf_root() {
  local dir="${REPO_ROOT}/terraform/infra"
  [[ -d "${dir}" ]] || die "no terraform root: ${dir}"
  printf '%s' "${dir}"
}

# The variables for this environment. Absent is fatal rather than defaulted:
# planning prod with dev's inputs is the mistake this refuses to make possible.
tf_var_file() {
  local file="${REPO_ROOT}/terraform/environments/${ENVIRONMENT}/${ENVIRONMENT}.tfvars"
  [[ -f "${file}" ]] || die "no tfvars for environment '${ENVIRONMENT}': ${file}"
  printf '%s' "${file}"
}

# Usage: tf_var_args; tf -chdir="$(tf_root)" plan "${TF_VAR_ARGS[@]}"
# shellcheck disable=SC2034  # consumed by the scripts that source this library
TF_VAR_ARGS=()
tf_var_args() {
  # shellcheck disable=SC2034  # read by the scripts that source this library
  TF_VAR_ARGS=(-var-file="$(tf_var_file)")
}

# Read one terraform output, empty string if the output or the state is absent.
# Never fails the caller: scripts must degrade to env defaults, because an
# operator running `make status` before the first apply is a normal thing to do.
tf_output() {
  local name="$1" dir value
  dir="${REPO_ROOT}/terraform/infra"
  [[ -d "${dir}/.terraform" ]] || return 0
  value="$(tf -chdir="${dir}" output -raw "${name}" 2>/dev/null)" || return 0
  printf '%s' "${value}"
}

# ---------------------------------------------------------------------------
# Credentials. Nothing below ever prints a token.
# ---------------------------------------------------------------------------

_ACCESS_TOKEN=""
access_token() {
  if [[ -z "${_ACCESS_TOKEN}" ]]; then
    _ACCESS_TOKEN="$(gcloud auth print-access-token 2>/dev/null)" \
      || die "no gcloud credentials; run: gcloud auth login"
  fi
  printf '%s' "${_ACCESS_TOKEN}"
}

# A Google ID token for the API.
#
# The API verifies the token itself and checks the `hd` claim, so the useful
# source depends on who is calling:
#   SWARM_ID_TOKEN        -- CI supplies its own, already minted by WIF
#   SWARM_IMPERSONATE_SA  -- mint one for an exact audience via impersonation
#   otherwise             -- the operator's own gcloud user token, whose audience
#                            is the gcloud OAuth client; API_AUDIENCE must list it
_ID_TOKEN=""
id_token() {
  if [[ -n "${_ID_TOKEN}" ]]; then printf '%s' "${_ID_TOKEN}"; return 0; fi
  if [[ -n "${SWARM_ID_TOKEN:-}" ]]; then
    _ID_TOKEN="${SWARM_ID_TOKEN}"
  elif [[ -n "${SWARM_IMPERSONATE_SA:-}" ]]; then
    _ID_TOKEN="$(gcloud auth print-identity-token \
      --impersonate-service-account="${SWARM_IMPERSONATE_SA}" \
      --audiences="${API_AUDIENCE:-$(api_url)}" --include-email 2>/dev/null)" \
      || die "could not mint an ID token by impersonating ${SWARM_IMPERSONATE_SA}"
  else
    _ID_TOKEN="$(gcloud auth print-identity-token 2>/dev/null)" \
      || die "no ID token available; run: gcloud auth login"
  fi
  printf '%s' "${_ID_TOKEN}"
}

# A curl config carrying one Authorization header, written to curl's STDIN.
#
# The token never reaches argv. `curl -H "Authorization: Bearer ${token}"` puts
# it in curl's command line, which is world-readable through /proc on Linux and
# lands in shell history when a human copies the pattern -- and an ID token here
# is not a session cookie, it is full impersonation of that caller's tenant:
# their provider keys, their GCS prefix, their budget.
#
# `printf` is a bash BUILTIN, so building this string forks no process and
# creates no /proc entry of its own; the value exists only in this shell's
# memory and in the pipe to curl.
auth_config() { printf 'header = "Authorization: Bearer %s"\n' "$1"; }

# ---------------------------------------------------------------------------
# Control-plane API
# ---------------------------------------------------------------------------

_API_URL=""
api_url() {
  if [[ -n "${_API_URL}" ]]; then printf '%s' "${_API_URL}"; return 0; fi
  if [[ -n "${API_URL:-}" ]]; then
    _API_URL="${API_URL%/}"
  else
    local from_tf
    from_tf="$(tf_output api_url)"
    if [[ -n "${from_tf}" ]]; then
      _API_URL="${from_tf%/}"
    else
      # stderr captured, not discarded. An expired session, a missing
      # run.services.get, a disabled Cloud Run Admin API, a wrong REGION and a
      # wrong PROJECT_ID all produce the same empty value here, and the message
      # below used to blame the last one on the list -- sending an operator to
      # redeploy a healthy control plane in the middle of the incident they were
      # trying to diagnose.
      local describe_err=""
      if ! _API_URL="$(gcloud run services describe "${API_SERVICE}" \
           --project "${PROJECT_ID}" --region "${REGION}" \
           --format='value(status.url)' 2>"${TMPDIR:-/tmp}/swarm-apiurl.$$")"; then
        describe_err="$(cat "${TMPDIR:-/tmp}/swarm-apiurl.$$" 2>/dev/null || true)"
        _API_URL=""
      fi
      rm -f "${TMPDIR:-/tmp}/swarm-apiurl.$$"
      # Exits here if the session is dead, so nothing below misreports it.
      [[ -z "${describe_err}" ]] || die_if_auth_failure "${describe_err}"
      if [[ -z "${_API_URL}" && -n "${describe_err}" ]]; then
        err "cannot resolve the API URL, and this is why:"
        printf '%s\n' "${describe_err}" | redact | head -n 3 | sed 's/^/     /' >&2
        die "that is a failure to LOOK UP ${API_SERVICE}, not proof it is absent. Check the account, the region (${REGION}) and the project (${PROJECT_ID}) before redeploying anything."
      fi
      _API_URL="${_API_URL%/}"
    fi
  fi
  [[ -n "${_API_URL}" ]] || die "no API URL: API_URL is unset, terraform has no api_url output, and ${API_SERVICE} was not found in ${REGION}. If the lookup itself failed you would have seen why above; this is the case where it genuinely returned nothing."
  printf '%s' "${_API_URL}"
}

# api_request METHOD PATH [BODY] -> body on stdout, HTTP status in API_STATUS
API_STATUS=0
api_request() {
  local method="$1" path="$2" body="${3:-}"
  local url token response
  url="$(api_url)${path}"
  token="$(id_token)"

  # -K - : the Authorization header arrives on stdin, never in argv. See
  # auth_config above for why that distinction matters for an ID token.
  if [[ -n "${body}" ]]; then
    response="$(auth_config "${token}" | curl -sS -m "${HTTP_TIMEOUT}" -K - \
      -w $'\n%{http_code}' -X "${method}" \
      -H "Content-Type: application/json" \
      --data-binary "${body}" "${url}")" || { API_STATUS=0; return 1; }
  else
    response="$(auth_config "${token}" | curl -sS -m "${HTTP_TIMEOUT}" -K - \
      -w $'\n%{http_code}' -X "${method}" "${url}")" || { API_STATUS=0; return 1; }
  fi

  API_STATUS="${response##*$'\n'}"
  printf '%s' "${response%$'\n'*}"
  [[ "${API_STATUS}" -ge 200 && "${API_STATUS}" -lt 300 ]]
}

api_get()  { api_request GET  "${API_PREFIX}$1"; }
api_post() { api_request POST "${API_PREFIX}$1" "$2"; }

api_reachable() {
# Cloud Run reserves the path configured as the container's livenessProbe
# (/healthz here): external requests to it are answered 404 by the frontend
# before reaching the container, while the internal prober gets 200. Verified
# 2026-09-16 -- /healthz returned 404 with no `server: Google Frontend` header
# while /readyz on the same router returned 200 with one. /readyz is the better
# gate regardless: it proves Firestore is reachable, not just that a process is up.
  # The service requires an ID token: Cloud Run IAM answers an unauthenticated
  # request 403, which is not a 2xx and so read as "unreachable" even when the
  # API is perfectly healthy.
  curl -sS -m 10 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $(id_token)" \
    "$(api_url)/readyz" 2>/dev/null | grep -q '^2'
}

# ---------------------------------------------------------------------------
# Firestore REST. Used instead of a Python client so that ops scripts have no
# dependency beyond curl + jq, and so they work identically from CI.
# ---------------------------------------------------------------------------

fs_base() {
  printf 'https://firestore.googleapis.com/v1/projects/%s/databases/%s/documents' \
    "${PROJECT_ID}" "${FIRESTORE_DATABASE}"
}

# This USED TO return the response body whatever the status was. curl had no -f
# and nothing looked at the code, so a 401, 403, 429 or 500 exited 0 and its
# `{"error": ...}` body was handed back as if it were data. Every reader built on
# top then turned that into an absence:
#
#   fs_list    jq '.documents // []'   -> []      "no pools configured yet"
#   fs_count   the aggregate branch    -> 0       "0 task(s) hold capacity"
#   fs_query   select(.document)       -> nothing "none active"
#
# So an expired session read as an empty, healthy platform on the one screen an
# operator trusts during an incident, and turned two concurrency assertions
# green in the test suite by giving them nothing to count.
#
# 404 IS AN ANSWER ONLY WHERE IT MEANS SOMETHING. Callers using fs_get to ask
# whether ONE document exists read the reply as `if .fields then ... else absent`
# (register-tenant.sh:234,601,618, failure-test.sh:66, quota-test.sh:61), and for
# them a 404 is a legitimate "no". They opt in with FS_ALLOW_404.
#
# For a COLLECTION it means the opposite: an empty collection answers 200 with no
# documents, so a 404 on fs_list/fs_query/fs_count means the database or the path
# is wrong. Passing it through was this fix'"'"'s own first mistake -- a request
# against a database that does not exist still came back as `[]`, i.e. exactly
# the bug being fixed, one layer further down.
#
# Everything else non-2xx is a failure, says so, and returns non-zero so `set -e`
# stops the caller instead of letting it read an error body as data.
fs_request() {
  local method="$1" url="$2" body="${3:-}"
  local token status out rc=0
  token="$(access_token)"
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-fs.XXXXXX")"
  if [[ -n "${body}" ]]; then
    status="$(auth_config "${token}" | curl -sS -m "${HTTP_TIMEOUT}" -K - -X "${method}" \
      -H "Content-Type: application/json" \
      --data-binary "${body}" -o "${out}" -w '%{http_code}' "${url}")" || rc=$?
  else
    status="$(auth_config "${token}" | curl -sS -m "${HTTP_TIMEOUT}" -K - -X "${method}" \
      -o "${out}" -w '%{http_code}' "${url}")" || rc=$?
  fi

  if [[ "${rc}" -ne 0 ]]; then
    rm -f "${out}"
    err "Firestore ${method} could not complete (curl exit ${rc}). This is a transport failure, NOT an empty result."
    return 1
  fi

  case "${status}" in
    2*)
      cat "${out}"
      rm -f "${out}"
      ;;
    404)
      if [[ -n "${FS_ALLOW_404:-}" ]]; then
        cat "${out}"
        rm -f "${out}"
      else
        rm -f "${out}"
        err "Firestore ${method} returned HTTP 404. An empty collection answers 200, so this is a wrong database or path -- NOT an empty result."
        dim "  database: ${FIRESTORE_DATABASE}  project: ${PROJECT_ID}"
        return 1
      fi
      ;;
    *)
      local detail
      detail="$(head -c 600 "${out}" 2>/dev/null || true)"
      rm -f "${out}"
      # Exits here on a dead session, naming it as one.
      die_if_auth_failure "${detail}"
      err "Firestore ${method} returned HTTP ${status}. This is NOT an empty result."
      printf '%s\n' "${detail}" | redact | head -n 3 | sed 's/^/     /' >&2
      return 1
      ;;
  esac
}

# FS_ALLOW_404: a missing document is a legitimate answer to "does this exist?".
fs_get()    { FS_ALLOW_404=1 fs_request GET "$(fs_base)/$1"; }
fs_delete() { fs_request DELETE "$(fs_base)/$1" >/dev/null; }

# fs_list COLLECTION [PAGE_SIZE] -> concatenated `documents` arrays, paginated.
fs_list() {
  local collection="$1" page_size="${2:-300}"
  local token page url out
  token=""
  while :; do
    url="$(fs_base)/${collection}?pageSize=${page_size}"
    [[ -n "${token}" ]] && url="${url}&pageToken=${token}"
    # `|| return 1`, not bare `set -e`: set -e is suppressed throughout an
    # `if` condition, and status.sh and the test suites call these from
    # inside one. Without this the caller reads an error as an empty page.
    page="$(fs_request GET "${url}")" || return 1
    out="$(printf '%s' "${page}" | jq -c '.documents // []')"
    printf '%s\n' "${out}"
    token="$(printf '%s' "${page}" | jq -r '.nextPageToken // ""')"
    [[ -n "${token}" ]] || break
  done
}

# One decoded document per line.
# The pipeline's status is jq's, so a failing fs_list would be invisible here --
# the same trap the sweep found in status.sh's gcloud_json. Run fs_list first and
# check it, then decode.
fs_list_docs() {
  local raw
  raw="$(fs_list "$1" "${2:-300}")" || return 1
  printf '%s' "${raw}" | jq -c "${FS_JQ} .[] | doc"
}

# fs_patch DOC_PATH FIELD_MASK_CSV JSON_FIELDS
fs_patch() {
  local doc="$1" mask="$2" fields="$3"
  local url="" part
  url="$(fs_base)/${doc}?"
  IFS=',' read -r -a _mask_parts <<<"${mask}"
  for part in "${_mask_parts[@]}"; do
    url+="updateMask.fieldPaths=${part}&"
  done
  fs_request PATCH "${url%&}" "{\"fields\":${fields}}" >/dev/null
}

# fs_count_where COLLECTION WHERE_JSON -> integer, via a server-side aggregation,
# so a hundred thousand tasks cost one request and zero document reads. Pass
# 'null' for an unfiltered count.
fs_count_where() {
  local collection="$1" where="${2:-null}"
  local query result
  query="$(jq -nc --arg c "${collection}" --argjson w "${where}" '
    {structuredAggregationQuery:{
       structuredQuery: ({from:[{collectionId:$c}]} + (if $w == null then {} else {where:$w} end)),
       aggregations:[{alias:"n",count:{}}]}}')"
  result="$(fs_request POST "$(fs_base):runAggregationQuery" "${query}")" || return 1
  printf '%s' "$(printf '%s' "${result}" \
    | jq -r '[.[]?|.result?.aggregateFields?.n?.integerValue//empty]|first // "0"')"
}

# fs_count COLLECTION [FIELD OP STRING_VALUE]
fs_count() {
  local collection="$1" field="${2:-}" op="${3:-}" value="${4:-}"
  local where='null'
  if [[ -n "${field}" ]]; then
    where="$(jq -nc --arg f "${field}" --arg o "${op}" --arg v "${value}" \
      '{fieldFilter:{field:{fieldPath:$f},op:$o,value:{stringValue:$v}}}')"
  fi
  fs_count_where "${collection}" "${where}"
}

fs_null_filter() {
  jq -nc --arg f "$1" --arg o "$2" '{unaryFilter:{field:{fieldPath:$f},op:$o}}'
}

# Firestore's REST encoding is typed ({"integerValue":"3"}), which is unreadable
# in a terminal and awkward in jq. This prelude decodes a document into plain
# JSON: `jq "${FS_JQ} .[] | doc"`.
FS_JQ='
def fv:
  if type != "object" then .
  elif has("integerValue")   then (.integerValue|tonumber)
  elif has("doubleValue")    then .doubleValue
  elif has("booleanValue")   then .booleanValue
  elif has("stringValue")    then .stringValue
  elif has("timestampValue") then .timestampValue
  elif has("nullValue")      then null
  elif has("bytesValue")     then "<bytes>"
  elif has("arrayValue")     then [ (.arrayValue.values // [])[] | fv ]
  elif has("mapValue")       then ( (.mapValue.fields // {}) | with_entries(.value |= fv) )
  else . end;
def doc: { id: (.name | split("/") | last) }
         + ( (.fields // {}) | with_entries(.value |= fv) );
# Mirrors swarm_common.models.SlotPool.effective_limit exactly: the minimum of
# the hard limit and any adaptive or quota-derived cap, floored at zero.
def effective_limit:
  [ (.hard_limit // 0) ]
  + (if (.adaptive_target // null) == null then [] else [.adaptive_target] end)
  + (if (.quota_derived_limit // null) == null then [] else [.quota_derived_limit] end)
  | min | if . < 0 then 0 else . end;
'

# fs_query COLLECTION WHERE_JSON [LIMIT] -> one document JSON per line
fs_query() {
  local collection="$1" where="${2:-null}" limit="${3:-500}"
  local query
  query="$(jq -nc --arg c "${collection}" --argjson w "${where}" --argjson l "${limit}" '
    {structuredQuery: ({from:[{collectionId:$c}], limit:$l}
       + (if $w == null then {} else {where:$w} end))}')"
  local rows
  rows="$(fs_request POST "$(fs_base):runQuery" "${query}")" || return 1
  printf '%s' "${rows}" \
    | jq -c '.[]? | select(.document != null) | .document'
}

fs_field_filter() {
  jq -nc --arg f "$1" --arg o "$2" --argjson v "$3" \
    '{fieldFilter:{field:{fieldPath:$f},op:$o,value:$v}}'
}

fs_database_exists() {
  gcloud firestore databases describe --database="${FIRESTORE_DATABASE}" \
    --project="${PROJECT_ID}" --format='value(name)' >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

is_production() {
  [[ "${ENVIRONMENT}" == "prod" || "${ENVIRONMENT}" == "production" ]]
}

# confirm PROMPT EXPECTED_ANSWER -- typed confirmation, never a bare y/n for
# anything destructive. Honours SWARM_ASSUME_YES only for non-destructive flows.
confirm() {
  local prompt="$1" expected="$2" answer
  if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
    warn "SWARM_ASSUME_YES set; skipping confirmation for: ${prompt}"
    return 0
  fi
  [[ -t 0 ]] || die "refusing to proceed without an interactive confirmation (not a TTY)"
  printf '%s\n' "${prompt}" >&2
  printf 'Type %s to continue: ' "${expected}" >&2
  read -r answer
  [[ "${answer}" == "${expected}" ]] || die "confirmation did not match; aborted"
}

git_sha() {
  if git -C "${REPO_ROOT}" rev-parse --short=12 HEAD >/dev/null 2>&1; then
    git -C "${REPO_ROOT}" rev-parse --short=12 HEAD
  else
    printf 'nogit-%s' "$(date -u +%Y%m%d%H%M%S)"
  fi
}

git_dirty() {
  git -C "${REPO_ROOT}" diff --quiet 2>/dev/null && \
  git -C "${REPO_ROOT}" diff --cached --quiet 2>/dev/null && return 1
  return 0
}

iso_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Mask anything that looks like a credential before it reaches a terminal or a
# CI log.
#
# THIS IS BEST-EFFORT AND IS NOT A BOUNDARY. It is a pattern list, so it masks
# the shapes it knows and nothing else: an opaque, high-entropy key from a
# provider whose prefix is not below (Azure, Bedrock, a self-hosted gateway)
# passes through in cleartext. The controls that actually stop a key reaching a
# log are the ones with no pattern matching in them -- the worker registering
# every secret value with its logger the moment it reads it, and the API having
# no code path that returns payload bytes at all. Treat this filter as the last
# of three layers, never as the first.
#
# The `Bearer` rule is separate from the assignment rule on purpose: the
# assignment rule's terminator stops at whitespace, so `Authorization: Bearer X`
# would otherwise mask only the literal word `Bearer` and print X.
redact() {
  sed -E \
    -e 's/(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+/\1********/g' \
    -e 's/(ya29\.)[A-Za-z0-9._-]+/\1********/g' \
    -e 's/(ey[A-Za-z0-9_-]{8})[A-Za-z0-9._-]+/\1********/g' \
    -e 's/(AIza)[A-Za-z0-9_-]{20,}/\1********/g' \
    -e 's/(gh[pousr]_)[A-Za-z0-9]{8,}/\1********/g' \
    -e 's/(github_pat_)[A-Za-z0-9_]{8,}/\1********/g' \
    -e 's/(xox[abprs]-)[A-Za-z0-9-]{8,}/\1********/g' \
    -e 's/((AKIA|ASIA)[A-Z0-9]{4})[A-Z0-9]+/\1********/g' \
    -e 's/(-----BEGIN [A-Z ]*PRIVATE KEY-----).*/\1********/g' \
    -e 's/(([Bb]earer|[Bb]asic)[[:space:]]+)[A-Za-z0-9._~+\/-]{12,}=*/\1********/g' \
    -e 's/("?(api_?key|apikey|password|passwd|secret|token|credential|authorization)"?[[:space:]]*[:=][[:space:]]*"?)[^",[:space:]]+/\1********/Ig'
}

load_env
