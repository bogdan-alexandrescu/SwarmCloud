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
  # BSD stat (macOS) and GNU stat (Linux, and every CI runner) spell this
  # differently, and the ORDER matters for a reason that cost this check its
  # entire purpose on Linux for as long as it has existed.
  #
  # `-f` is not an unknown option to GNU stat. It means --file-system, it takes
  # a format, and it SUCCEEDS -- printing filesystem fields and a `  File:
  # "..."` line rather than the mode or the uid. So `stat -f ... || stat -c
  # ...` never reaches the fallback on Linux: the first branch exits 0, `owner`
  # is filled with that text, it does not equal EUID, and the check refuses
  # every env file it is handed. CI showed it as twenty-odd integration
  # failures all reading `is owned by uid   File: "..."` -- the uid missing
  # because there never was one.
  #
  # GNU FIRST, because `stat -c` on BSD is a genuine unknown option and exits
  # non-zero, so that fallback is the one that actually falls back. Then both
  # values are required to look like what they are: a stat that succeeds and
  # answers something else must read as "could not tell", which is the
  # permissive branch, rather than as a mode of `?p` that some future
  # comparison treats as meaningful.
  perms="$(stat -c '%a' "${env_file}" 2>/dev/null || stat -f '%Lp' "${env_file}" 2>/dev/null || echo "")"
  owner="$(stat -c '%u' "${env_file}" 2>/dev/null || stat -f '%u' "${env_file}" 2>/dev/null || echo "")"
  [[ "${perms}" =~ ^[0-7]+$ ]] || perms=""
  [[ "${owner}" =~ ^[0-9]+$ ]] || owner=""
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
  # The static web UI. Deployed like the others, but it serves files rather than
  # answering the control plane's health contract -- it has /healthz from nginx
  # and no /readyz, because there is no downstream for it to be ready FOR.
  UI_SERVICE="${UI_SERVICE:-swarm-ui}"

  API_PREFIX="${API_PREFIX:-/v1}"
  HTTP_TIMEOUT="${HTTP_TIMEOUT:-30}"

  # The FRONT DOOR: the hostname of the external load balancer in front of
  # swarm-api. Empty means "resolve it" (see front_door_host below), not "there
  # isn't one" -- swarm-api's ingress is internal-and-cloud-load-balancing, so
  # from outside the VPC the load balancer is the ONLY address that serves it.
  API_HOST="${API_HOST:-}"

  IMAGE_HOST="${IMAGE_HOST:-${REGION}-docker.pkg.dev}"
  IMAGE_REPO="${IMAGE_REPO:-${IMAGE_HOST}/${PROJECT_ID}/${ARTIFACT_REGISTRY}}"

  export PROJECT_ID REGION ZONE ENVIRONMENT FIRESTORE_DATABASE ARTIFACT_BUCKET
  export ARTIFACT_REGISTRY TF_STATE_BUCKET TF_STATE_PREFIX GKE_CLUSTER GKE_LOCATION
  export PUBSUB_TOPIC SCHEDULER_JOB API_SERVICE SCHEDULER_SERVICE QUOTA_SERVICE RECONCILER_SERVICE
  export API_PREFIX HTTP_TIMEOUT API_HOST IMAGE_HOST IMAGE_REPO

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

# The deny-list in the exact form `destroy-guard.jq` takes as `--argjson deny`.
#
# WHY THIS IS A FUNCTION HERE RATHER THAN FIVE LINES AT EACH CALL SITE. It WAS
# five lines at each call site -- `scripts/destroy.sh` and
# `scripts/lib/plan-guard.sh` each carried the same pipeline with the same
# comment copy-pasted above it -- and the list a guard judges by is not a place
# for two implementations. CLAUDE.md says a script must source this file "rather
# than re-deriving project, region, paths, the deny-list, the redaction filter,
# the unlabelable-type list, or the plan guard"; the deny-list itself obeyed
# that, its TRANSFORMATION did not. The two consumers are `make destroy` and the
# plan guard in CI, so a divergence means CI passes a plan that `make destroy`
# then refuses, or -- the direction that costs something -- CI passes a plan that
# touches another team's resource because its copy of the list lost an entry the
# other copy kept.
#
# Two transformations, and both are load-bearing:
#
#   * the bare `default` is REMOVED. That string appears inside too many
#     unrelated resource ids to compare blindly against every token, so
#     `destroy-guard.jq` matches the shared VPC's default network on the
#     `network`/`subnetwork` FIELD instead (`default_network_hit`). Leaving
#     `default` in the token list would flag ordinary resources of ours.
#   * `(default)` is ADDED. That is Firestore's default database -- the one
#     CONTRACT.md says must stay free for the other teams in this shared
#     project -- and it is a name no token-splitting would produce.
guard_deny_json() {
  printf '%s\n' "${SHARED_DENY_LIST[@]}" \
    | grep -vx 'default' | jq -R . | jq -sc '. + ["(default)"]'
}

# ---------------------------------------------------------------------------
# Is a shared resource still there?
# ---------------------------------------------------------------------------
#
# THREE answers, never two. `gcloud ... >/dev/null 2>&1` collapses "the API said
# NOT_FOUND" into the same `false` as "the session expired", "the API is not
# enabled", "I have no permission to look" and "the network is down" -- and the
# caller of that shape was destroy.sh's post-destroy check, which renders the
# false as "SHARED RESOURCES ARE MISSING AFTER DESTROY ... Escalate
# immediately". In a project holding another team's live cluster, "their cluster
# is gone" said on the strength of an expired token is the worst sentence this
# repository can print: a page, raised at the one moment nobody can tell whether
# it is real, about resources an operator has just run a destroy next to.
#
# So the reasons are separated here the way fs_database_exists separates them:
#
#   0  present       -- gcloud answered, and the resource is there
#   1  ABSENT        -- gcloud answered, and the answer was NOT_FOUND
#   2  cannot tell   -- gcloud did not answer; the reason is printed
#
# Return 2 is the DEFAULT. Only stderr that is recognisably a not-found earns a
# 1, because claiming a deletion without evidence is the whole defect; an
# unfamiliar error stays "cannot tell" and the operator reads the real text.

# True when this captured gcloud stderr is the API saying the resource is not
# there, rather than the SDK saying it could not ask.
#
# Deliberately narrow, and matched on the shapes the resource kinds checked in
# destroy.sh actually produce:
#   container clusters  -> "ResponseError: code=404, message=Not found: ..."
#   storage buckets     -> "ERROR: ... not found: 404"
#   iam service-accounts-> "NOT_FOUND: Unknown service account" / "does not exist"
gcloud_not_found() {
  case "$1" in
    *NOT_FOUND*|*"Not found"*|*"not found"*) return 0 ;;
    *"does not exist"*)                      return 0 ;;
    *"code=404"*|*"HTTPError 404"*)          return 0 ;;
  esac
  return 1
}

# _shared_probe MODE LABEL COMMAND...
#
# MODE is `describe` (the exit status alone decides presence) or `list` (a
# listing exits 0 whether or not it matched, so an EMPTY stdout is the absence).
# The list shape exists for the GKE cluster: `clusters describe` needs a
# --location, and a location guessed wrong returns a real NOT_FOUND for a
# cluster that is alive one zone over -- a false "it is gone" of exactly the
# kind this function exists to make impossible.
_shared_probe() {
  local mode="$1" label="$2"; shift 2
  local out errfile rc=0
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-probe-out.XXXXXX")"
  errfile="$(mktemp "${TMPDIR:-/tmp}/swarm-probe-err.XXXXXX")"
  "$@" >"${out}" 2>"${errfile}" || rc=$?

  if [[ "${rc}" -eq 0 ]]; then
    if [[ "${mode}" == "list" && ! -s "${out}" ]]; then
      rm -f "${out}" "${errfile}"
      return 1
    fi
    rm -f "${out}" "${errfile}"
    return 0
  fi

  if gcloud_not_found "$(cat "${errfile}")"; then
    rm -f "${out}" "${errfile}"
    return 1
  fi

  err "could not determine whether ${label} still exists:"
  redact <"${errfile}" | head -n 3 | sed 's/^/     /' >&2
  rm -f "${out}" "${errfile}"
  return 2
}

# shared_resource_present LABEL COMMAND...  -- a `describe`-shaped lookup.
shared_resource_present() { _shared_probe describe "$@"; }

# shared_resource_listed LABEL COMMAND...   -- a `list --filter`-shaped lookup.
shared_resource_listed() { _shared_probe list "$@"; }

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
    if [[ -n "${K_SERVICE:-}${CLOUD_RUN_JOB:-}" ]]; then
      # Inside Cloud Run. The metadata server issues an access token for the
      # attached service account, so no gcloud and no key file.
      # `service-accounts`, HYPHENATED. The underscore spelling below it for two
      # months returns "404 page not found" -- the metadata server's answer for
      # an unknown path, not for a refused one -- so `curl -sf` failed and this
      # died with "the metadata server refused an access token". Measured from
      # inside the job on 2026-09-22: service_accounts -> 404,
      # service-accounts -> 200. It is the whole reason the in-VPC gate could
      # never authenticate.
      local meta="http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
      _ACCESS_TOKEN="$(curl -sf -H 'Metadata-Flavor: Google' "${meta}" 2>/dev/null \
        | jq -r '.access_token // empty')" \
        || die "the metadata server refused an access token"
      [[ -n "${_ACCESS_TOKEN}" ]] || die "the metadata server returned no access token"
    elif [[ -n "${SWARM_IMPERSONATE_SA:-}" ]]; then
      # IAP is the reason this branch exists. The front door accepts an OAuth
      # ACCESS token (see api_credential below) and authorises the principal it
      # names, so reaching the API from a laptop means presenting a service
      # account's access token rather than the operator's own. gcloud's
      # `--impersonate-service-account` is the one way to get one without a key
      # file, which this repository does not issue.
      _ACCESS_TOKEN="$(gcloud auth print-access-token \
        --impersonate-service-account="${SWARM_IMPERSONATE_SA}" 2>/dev/null)" \
        || die "could not mint an access token by impersonating ${SWARM_IMPERSONATE_SA}; you need roles/iam.serviceAccountTokenCreator on it"
    else
      _ACCESS_TOKEN="$(gcloud auth print-access-token 2>/dev/null)" \
        || die "no gcloud credentials; run: gcloud auth login"
    fi
  fi
  printf '%s' "${_ACCESS_TOKEN}"
}

# --------------------------------------------------------------------------
# REST equivalents of two gcloud calls, so the verification image needs no SDK
# --------------------------------------------------------------------------
# The Cloud SDK base image ships a python-cryptography with unfixed HIGH and
# CRITICAL CVEs, and `make push` refuses to promote it -- correctly. Rather
# than allow-list the scan or pin a package the base will overwrite, the two
# things the tests actually needed gcloud for are done over REST with the
# token above. Both fail CLOSED and distinguish "absent" from "could not
# look", which is the whole reason these helpers are worth their lines.

#: The https URI of a Cloud Run service, or empty with a reason on stderr.
cloud_run_service_uri() {
  local service="$1" out rc=0
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-runuri.XXXXXX")"
  curl -sS --max-time "${HTTP_TIMEOUT:-30}" \
    -H "Authorization: Bearer $(access_token)" \
    "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/services/${service}" \
    >"${out}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    redact <"${out}" >&2
    rm -f "${out}"
    return 1
  fi
  # A 404 arrives as a JSON error body with status 200 from curl's point of
  # view, so the body is what decides -- not curl's exit code.
  if jq -e '.error' <"${out}" >/dev/null 2>&1; then
    jq -r '.error.message // "unknown error"' <"${out}" | redact >&2
    rm -f "${out}"
    return 1
  fi
  jq -r '.uri // empty' <"${out}"
  rm -f "${out}"
}

#: The identity this process runs as, straight from the metadata server.
#: Empty outside Cloud Run, which callers must treat as "unknown", never as a
#: mismatch.
metadata_identity() {
  [[ -n "${K_SERVICE:-}${CLOUD_RUN_JOB:-}" ]] || return 0
  local meta="http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
  curl -sf -m 10 -H 'Metadata-Flavor: Google' "${meta}" 2>/dev/null || true
}

#: Whether a Cloud Run service's latest revision is READY. Prints "True",
#: "False" or "Unknown", or fails with a reason on stderr.
#:
#: WHY THIS EXISTS RATHER THAN A /readyz PROBE. The verification suites check
#: that the control plane is healthy, and /readyz is the obvious way -- but a
#: probe needs run.invoker on the service AND an ID token minted for THAT
#: service's URL, and the gate holds one token for one audience. Getting there
#: for swarm-quota-broker would mean adding a test identity to the invoker list
#: of the service that holds every tenant's subscription credentials, whose
#: membership is deliberately workers + api + scheduler + tick and nothing
#: else. A health check is not worth that.
#:
#: The Admin API answers the same question -- is the serving revision up --
#: from a role the gate already holds (run.viewer), and it distinguishes "not
#: deployed" from "could not look", which a 401 from a probe does not.
cloud_run_service_ready() {
  local service="$1" out rc=0
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-runready.XXXXXX")"
  curl -sS --max-time "${HTTP_TIMEOUT:-30}" \
    -H "Authorization: Bearer $(access_token)" \
    "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/services/${service}" \
    >"${out}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    redact <"${out}" >&2
    rm -f "${out}"
    return 1
  fi
  if jq -e '.error' <"${out}" >/dev/null 2>&1; then
    jq -r '.error.message // "unknown error"' <"${out}" | redact >&2
    rm -f "${out}"
    return 1
  fi
  # `terminalCondition` is the v2 shape; `Ready` in `conditions` is the older
  # one. Neither present is "Unknown", never "False" -- an absent condition is
  # not a failing one.
  jq -r '(.terminalCondition.state // (.conditions[]? | select(.type=="Ready") | .state) // "UNKNOWN")
         | if . == "CONDITION_SUCCEEDED" then "True"
           elif . == "CONDITION_FAILED" then "False"
           else . end' <"${out}" | head -1
  rm -f "${out}"
}

#: The container image a Cloud Run service is CURRENTLY serving. Prints it, or
#: fails if the service cannot be read.
#:
#: Needed because `image_tag` is applied on every terraform apply -- neither
#: cloud_run module ignores the image, deliberately -- so any plan that does not
#: set it plans to move every service and job to the variable's default. Asking
#: the running service is the only answer that is true after a deploy from CI,
#: from another machine, or after a rollback.
cloud_run_image() {
  local service="$1" out rc=0
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-runimg.XXXXXX")"
  curl -sS --max-time "${HTTP_TIMEOUT:-30}" \
    -H "Authorization: Bearer $(access_token)" \
    "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/services/${service}" \
    >"${out}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    redact <"${out}" >&2
    rm -f "${out}"
    return 1
  fi
  if jq -e '.error' <"${out}" >/dev/null 2>&1; then
    jq -r '.error.message // "unknown error"' <"${out}" | redact >&2
    rm -f "${out}"
    return 1
  fi
  jq -r '.template.containers[0].image // empty' <"${out}"
  rm -f "${out}"
}

#: Every Cloud Run job execution in the project, as a JSON array. Fails loudly.
#:
#: An empty list is an ANSWER (`executions` is ABSENT, not empty, when there are
#: none) and an unreadable listing is a failure -- the same distinction
#: gcs_object_count draws, for the same reason: a denied or expired listing read
#: as "nothing is running" turns the invariant-1 check green on a platform that
#: is running work it cannot see.
cloud_run_executions() {
  local out rc=0
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-execs.XXXXXX")"
  curl -sS --max-time "${HTTP_TIMEOUT:-30}" \
    -H "Authorization: Bearer $(access_token)" \
    "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/-/executions?pageSize=500" \
    >"${out}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    redact <"${out}" >&2
    rm -f "${out}"
    return 1
  fi
  if jq -e '.error' <"${out}" >/dev/null 2>&1; then
    jq -r '.error.message // "unknown error"' <"${out}" | redact >&2
    rm -f "${out}"
    return 1
  fi
  jq -c '.executions // []' <"${out}"
  rm -f "${out}"
}

#: Count Cloud Run job executions in the project. Prints the count, or fails.
#: Zero is an ANSWER here; an unreadable listing is a failure, because that
#: number is the whole point of the invariant-1 check that calls it.
#:
#: Assigned first and checked, NOT `cloud_run_executions | jq length`: under
#: pipefail a pipeline's status is the rightmost command's, so jq succeeding on
#: empty input would mask the listing having failed -- and this function is
#: called as an `if` condition, where set -e is suppressed anyway.
cloud_run_execution_count() {
  local rows
  rows="$(cloud_run_executions)" || return 1
  [[ -n "${rows}" ]] || return 1
  printf '%s' "${rows}" | jq -r 'length'
}

#: Count objects under a GCS prefix. Prints the count, or fails with a reason.
#: An empty listing is a SUCCESS with count 0; only an unreadable one fails.
gcs_object_count() {
  local bucket="$1" prefix="$2" out rc=0
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-gcsls.XXXXXX")"
  curl -sS --max-time "${HTTP_TIMEOUT:-30}" \
    -H "Authorization: Bearer $(access_token)" \
    --get --data-urlencode "prefix=${prefix}" \
    "https://storage.googleapis.com/storage/v1/b/${bucket}/o" \
    >"${out}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    redact <"${out}" >&2
    rm -f "${out}"
    return 1
  fi
  if jq -e '.error' <"${out}" >/dev/null 2>&1; then
    jq -r '.error.message // "unknown error"' <"${out}" | redact >&2
    rm -f "${out}"
    return 1
  fi
  # `items` is ABSENT, not empty, when nothing matches -- so `// []` here is
  # correct and is not the `?? 0` trap: the request succeeded and the answer
  # is genuinely zero.
  jq -r '(.items // []) | length' <"${out}"
  rm -f "${out}"
}

# A Google ID token for the API.
#
# THIS IS THE CLOUD RUN CREDENTIAL, not the front door's. Going through the load
# balancer, an ID token is refused by IAP whatever its audience -- see
# api_credential below for the measurements. Callers ask for api_credential and
# let it choose; this stays the way to get an ID token specifically.
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
  elif [[ -n "${K_SERVICE:-}${CLOUD_RUN_JOB:-}" ]]; then
    # Running INSIDE Cloud Run. The metadata server mints an ID token for the
    # attached service account directly, so the container needs no gcloud and
    # no key file -- which is the whole reason the verification targets can run
    # from in here at all.
    #
    # K_SERVICE is set on a service, CLOUD_RUN_JOB on a job. Testing both means
    # this works whether the tests run as a job or as a probe endpoint.
    local aud="${API_AUDIENCE:-$(api_url)}"
    # Hyphenated, for the same reason as access_token above.
    local meta="http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
    _ID_TOKEN="$(curl -sf -H 'Metadata-Flavor: Google' \
      "${meta}?audience=${aud}&format=full" 2>/dev/null)" \
      || die "the metadata server refused an ID token for audience ${aud}"
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
#
# THE *.run.app URL IS NOT THE ADDRESS OF THIS API. Measured 2026-09-22:
#
#   curl https://swarm-api-tonstldhta-uc.a.run.app/readyz              -> 404
#   curl -H 'Authorization: Bearer <id token>' .../readyz              -> 404
#   curl .../v1/workflows  and  .../definitely-not-a-route             -> 404
#
# All four answers are byte-identical: the same 272-byte Google HTML page.
# swarm-api's own 404 is FastAPI JSON, so nothing in those requests ever
# reached the container. `gcloud run services describe swarm-api` explains it:
#
#   run.googleapis.com/ingress: internal-and-cloud-load-balancing
#
# which is the setting the external load balancer in front of Cloud Run
# REQUIRES (terraform/modules/frontend/main.tf says so at length), and which
# makes the run.app hostname unreachable from outside the VPC by design.
# Google's frontend refuses the request there and renders the refusal as 404 --
# the same status a missing route produces, which is why scripts/e2e-test.sh
# read a healthy control plane as a broken one and told its operator to
# rule out IAM and then redeploy.
#
# So a 404 here is not a routing bug and not an IAM problem. It is the wrong
# ADDRESS, and it was this library handing it out.
#
# The front door is the load balancer. See front_door_host and api_credential.

# The load balancer's hostname, or empty when there is no front door.
#
# THREE SOURCES, IN ORDER, AND NONE OF THEM RESTATES THE VALUE:
#
#   1. API_HOST          -- the operator, or a test.
#   2. `terraform output frontend_url` -- does not exist yet. Track C's root
#      outputs (terraform/infra/outputs.tf) export frontend_iap_audiences and
#      quota_broker_url but NOT the URL itself, and `tf_output api_url`, which
#      this function used to consult first, has never existed either -- which is
#      why the Cloud Run fallback below was in fact the only branch that ever
#      ran. Asked for as a cross-track request; the moment it lands it wins here
#      without another change.
#   3. terraform/environments/<env>/<env>.tfvars -- Track C's INPUT, read rather
#      than copied. `frontend_hostname` is where the hostname is decided, so
#      reading it is the "change your side to match theirs" move rather than a
#      second place for the name to drift.
_FRONT_DOOR_HOST=""
_FRONT_DOOR_RESOLVED=0
front_door_host() {
  if [[ "${_FRONT_DOOR_RESOLVED}" -eq 1 ]]; then
    printf '%s' "${_FRONT_DOOR_HOST}"
    return 0
  fi
  _FRONT_DOOR_RESOLVED=1

  if [[ -n "${API_HOST:-}" ]]; then
    _FRONT_DOOR_HOST="${API_HOST#https://}"
    _FRONT_DOOR_HOST="${_FRONT_DOOR_HOST#http://}"
    _FRONT_DOOR_HOST="${_FRONT_DOOR_HOST%/}"
    printf '%s' "${_FRONT_DOOR_HOST}"
    return 0
  fi

  local from_tf
  from_tf="$(tf_output frontend_url)"
  if [[ -n "${from_tf}" ]]; then
    _FRONT_DOOR_HOST="${from_tf#https://}"
    _FRONT_DOOR_HOST="${_FRONT_DOOR_HOST%/}"
    printf '%s' "${_FRONT_DOOR_HOST}"
    return 0
  fi

  local tfvars="${REPO_ROOT}/terraform/environments/${ENVIRONMENT}/${ENVIRONMENT}.tfvars"
  [[ -f "${tfvars}" ]] || { printf '%s' ""; return 0; }
  # `enable_frontend = false` means the load balancer is not deployed. The
  # hostname is usually still written down next to it, and using it then would
  # send every script at a name that resolves to nothing.
  if grep -Eq '^[[:space:]]*enable_frontend[[:space:]]*=[[:space:]]*false' "${tfvars}"; then
    printf '%s' ""
    return 0
  fi
  _FRONT_DOOR_HOST="$(sed -n 's/^[[:space:]]*frontend_hostname[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    "${tfvars}" | head -n 1)"
  printf '%s' "${_FRONT_DOOR_HOST}"
}

# Is the resolved API URL the IAP-protected front door rather than Cloud Run?
#
# It decides which CREDENTIAL to present, so it asks about the address that is
# actually in use rather than about configuration: an operator who sets API_URL
# to the load balancer by hand gets the IAP path too.
api_is_front_door() {
  local host
  host="$(front_door_host)"
  [[ -n "${host}" ]] || return 1
  case "$(api_url)" in
    "https://${host}"|"https://${host}/"*) return 0 ;;
    *) return 1 ;;
  esac
}

_API_URL=""
api_url() {
  if [[ -n "${_API_URL}" ]]; then printf '%s' "${_API_URL}"; return 0; fi
  if [[ -n "${API_URL:-}" ]]; then
    # An explicit override wins, and it is not only an escape hatch:
    # terraform/infra/verify.tf sets API_URL to the *.run.app address for the
    # in-VPC verification job, where that address IS reachable and there is no
    # gcloud in the image to look anything up with.
    _API_URL="${API_URL%/}"
  else
    local front_door
    front_door="$(front_door_host)"
    if [[ -n "${front_door}" ]]; then
      _API_URL="https://${front_door}"
    elif [[ -n "${K_SERVICE:-}${CLOUD_RUN_JOB:-}" ]]; then
      # Inside Cloud Run with no API_URL and no gcloud. Nothing below can run
      # here, and guessing an address is worse than saying so.
      die "no API URL: API_URL is unset and this is running inside Cloud Run, where there is no gcloud to look one up with. terraform/infra/verify.tf sets API_URL on the verification job; a job that reaches this line is missing that env var."
    else
      # stderr captured, not discarded. An expired session, a missing
      # run.services.get, a disabled Cloud Run Admin API, a wrong REGION and a
      # wrong PROJECT_ID all produce the same empty value here, and the message
      # below used to blame the last one on the list -- sending an operator to
      # redeploy a healthy control plane in the middle of the incident they were
      # trying to diagnose.
      # THE INGRESS IS READ IN THE SAME CALL AS THE URL, and it is not a nicety:
      # a Cloud Run URL whose service refuses external traffic is an address
      # that answers 404 to everything, and 404 is the one status an operator
      # reads as "wrong path" rather than "wrong host".
      local describe_err="" describe_out=""
      if ! describe_out="$(gcloud run services describe "${API_SERVICE}" \
           --project "${PROJECT_ID}" --region "${REGION}" \
           --format='value[separator="|"](status.url,metadata.annotations."run.googleapis.com/ingress")' \
           2>"${TMPDIR:-/tmp}/swarm-apiurl.$$")"; then
        describe_err="$(cat "${TMPDIR:-/tmp}/swarm-apiurl.$$" 2>/dev/null || true)"
        describe_out=""
      fi
      rm -f "${TMPDIR:-/tmp}/swarm-apiurl.$$"
      # Exits here if the session is dead, so nothing below misreports it.
      [[ -z "${describe_err}" ]] || die_if_auth_failure "${describe_err}"
      if [[ -z "${describe_out}" && -n "${describe_err}" ]]; then
        err "cannot resolve the API URL, and this is why:"
        printf '%s\n' "${describe_err}" | redact | head -n 3 | sed 's/^/     /' >&2
        die "that is a failure to LOOK UP ${API_SERVICE}, not proof it is absent. Check the account, the region (${REGION}) and the project (${PROJECT_ID}) before redeploying anything."
      fi
      local ingress=""
      case "${describe_out}" in
        *"|"*) ingress="${describe_out##*|}"; _API_URL="${describe_out%%|*}" ;;
        *)     _API_URL="${describe_out}" ;;
      esac
      _API_URL="${_API_URL%/}"
      # `all` is the only ingress that serves the run.app hostname publicly.
      # Anything else (internal, internal-and-cloud-load-balancing) admits only
      # VPC and load-balancer traffic, and a caller out here is neither.
      if [[ -n "${_API_URL}" && -n "${ingress}" && "${ingress}" != "all" ]]; then
        err "${API_SERVICE} resolves to ${_API_URL}, and that address cannot serve you."
        err "Its ingress is '${ingress}', so Google's frontend refuses external requests"
        err "before they reach the container -- and renders the refusal as HTTP 404, which"
        err "reads exactly like a missing route on a broken deployment. The service is fine."
        err "Reach it through the load balancer instead. Set the hostname:"
        err "    API_HOST=<the frontend_hostname in terraform/environments/${ENVIRONMENT}/${ENVIRONMENT}.tfvars>"
        die "or set API_URL explicitly if you are inside the VPC, where the run.app address does work."
      fi
    fi
  fi
  [[ -n "${_API_URL}" ]] || die "no API URL: API_URL and API_HOST are unset, terraform has no frontend_url output, no frontend_hostname is set for environment '${ENVIRONMENT}', and ${API_SERVICE} was not found in ${REGION}. If the lookup itself failed you would have seen why above; this is the case where it genuinely returned nothing."
  printf '%s' "${_API_URL}"
}

# The bearer THIS address expects. Two doors, two credentials.
#
# CLOUD RUN wants a Google ID token: the service verifies it itself and derives
# the tenant from it. That is id_token(), and it is what every script here has
# always sent.
#
# THE LOAD BALANCER WANTS AN OAUTH ACCESS TOKEN, and this is the opposite of
# what the notes in this repository assumed. Measured against the live front
# door on 2026-09-22:
#
#   gcloud user ID token                     -> 401 Invalid IAP credentials:
#                                               Invalid bearer token.
#                                               Invalid JWT audience.
#   impersonated SA ID token, --audiences=
#     <the client id in IAP's sign-in
#      redirect>                             -> 401, the SAME message
#   gcloud user ACCESS token                 -> 401, IAP error code 900
#   impersonated SA ACCESS token             -> 403 "Access denied. For user
#                                               swarm-verify@..."
#
# Only the last one got past authentication -- a 403 that NAMES the caller is
# IAP saying "I know who you are and you are not on the list", which is an IAM
# grant away from working. The ID-token route cannot be made to work here at
# all: `gcloud compute backend-services list --format='value(iap.oauth2ClientId)'`
# is EMPTY for swarm-ui-backend, so IAP is using a Google-managed OAuth client
# and there is no client id to mint an audience for.
#
# Nothing is lost by sending an access token: IAP sends the backend NO
# Authorization header of its own and swarm-api authenticates the caller from
# `X-Goog-IAP-JWT-Assertion` instead (apps/swarm-api/swarm_api/deps.py:198).
api_credential() {
  if api_is_front_door; then
    access_token
  else
    id_token
  fi
}

# Turn IAP's refusal into the sentence that names the fix.
#
# These bodies are short, generic and arrive with a 401 or 403 that every caller
# here already renders as "the API rejected you" -- which sends an operator to
# check their tenant, their groups and their ALLOWED_DOMAINS when the request
# never reached the application at all.
_api_explain_iap() {
  local status="$1" body="$2"
  case "${body}" in
    *"Invalid IAP credentials"*|*"iap/docs/faq#error_codes"*)
      err "that ${status} came from IAP, not from swarm-api: the request never reached the application."
      err "The front door takes an OAuth ACCESS token for a principal granted"
      err "roles/iap.httpsResourceAccessor, not a Google ID token. Set SWARM_IMPERSONATE_SA"
      err "to a service account that holds it."
      ;;
    *"Access denied. For user"*)
      err "that ${status} came from IAP: the credential was accepted and the principal is not authorised."
      err "It needs roles/iap.httpsResourceAccessor on the backend service -- which Track C"
      err "sets through frontend_iap_members in terraform/environments/${ENVIRONMENT}/${ENVIRONMENT}.tfvars."
      ;;
  esac
}

# api_request METHOD PATH [BODY] -> body on stdout, HTTP status in API_STATUS
API_STATUS=0
api_request() {
  local method="$1" path="$2" body="${3:-}"
  local url token response
  url="$(api_url)${path}"
  token="$(api_credential)"

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
  local payload="${response%$'\n'*}"
  if [[ "${API_STATUS}" == "401" || "${API_STATUS}" == "403" ]]; then
    _api_explain_iap "${API_STATUS}" "${payload}"
  fi
  printf '%s' "${payload}"
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
  # The service requires a credential: an unauthenticated request is answered
  # 403 by Cloud Run IAM, or 302/401 by IAP, and none of those is a 2xx -- so
  # a healthy API reads as unreachable without one. api_credential picks the
  # kind THIS address accepts.
  #
  # The header goes in through `-K -`, not argv, for the reason auth_config
  # explains: this line used to interpolate the token straight into curl's
  # command line, where every process on the box can read it out of ps.
  auth_config "$(api_credential)" | curl -sS -m 10 -K - -o /dev/null -w '%{http_code}' \
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

# --- the task lifecycle, restated once for the shell -------------------------
#
# `swarm_common.states` is the authority. A bash script cannot import it, so
# these four arrays are the ONE shell copy, kept here rather than in the scripts
# that read them -- `scripts/status.sh` used to carry its own inline copy of the
# capacity-holding set, which is the number an operator reads during an incident
# to decide whether the platform is actually busy.
#
# The split is not cosmetic. CONTRACT.md invariant 1 is exactly this partition:
# only CONCURRENCY_STATES create infrastructure demand, PENDING_STATES cost
# nothing, TERMINAL_STATES have released everything. A state added to the enum
# and not added here would be counted as neither, so a real backlog or a real
# load would read as zero on the one screen meant to show it.
#
# scripts/lib/check-contract-parity.sh asserts all four against the frozen enum,
# so adding a state to swarm_common without adding it here fails `make test`.
# shellcheck disable=SC2034  # consumed by the scripts that source this library
TASK_STATES=(SUBMITTED QUEUED PARKED READY LEASED DISPATCHED STARTING RUNNING
             SUCCEEDED FAILED CANCELLED DEAD_LETTERED)
# shellcheck disable=SC2034  # consumed by the scripts that source this library
CONCURRENCY_STATES=(LEASED DISPATCHED STARTING RUNNING)
# shellcheck disable=SC2034  # consumed by the scripts that source this library
PENDING_STATES=(SUBMITTED QUEUED PARKED READY)
# shellcheck disable=SC2034  # consumed by the scripts that source this library
TERMINAL_STATES=(SUCCEEDED FAILED CANCELLED DEAD_LETTERED)

# --- the tenant namespace, restated once for the shell -----------------------
#
# THE PREFIX THAT COST SEVEN TASKS. On 2026-09-23 every `browser` task this
# platform had ever accepted -- seven of them, over two days -- failed with
#
#     jobs.batch is forbidden ... in the namespace "swarm-tenant-eng"
#
# and three separate investigations went looking at IAM. The cause was not IAM.
# `kubernetes/render.py` spelled the prefix `swarm-` and
# `apps/scheduler/scheduler/dispatch.py` spelled it `swarm-tenant-`, so the
# provisioner created `swarm-eng` and the dispatcher wrote into
# `swarm-tenant-eng`, which did not exist. Kubernetes AUTHORISES BEFORE IT
# RESOLVES, so a Job created into a namespace that is not there comes back 403
# `forbidden`, never 404 `not found` -- the error names a permission whatever
# the real cause was. See docs/gke-dispatch-403.md.
#
# `scripts/register-tenant.sh` carried a THIRD copy of the wrong spelling
# (`NAMESPACE="swarm-${TENANT_ID}"`), which decided both the Workload Identity
# binding it issues and the `namespace` field it writes onto the tenant
# document -- and `GkeJobDispatcher.namespace_for` prefers that field over its
# own template, so the Firestore record was overriding the one spelling that
# was right.
#
# So the shell gets ONE copy, here, and every script derives from it.
# `scripts/lib/check-contract-parity.sh` section 6 asserts this value against
# the scheduler's `GkeTarget.namespace_template`, the renderer's
# `NAMESPACE_PREFIX`, the reconciler's two defaults, the API's settings and
# terraform's `namespace_prefix` default, and SWEEPS the repository for a
# seventh restatement nobody registered. Change it here and that check tells
# you every other place that has to move with it.
# shellcheck disable=SC2034  # consumed by the scripts that source this library
TENANT_NAMESPACE_PREFIX="swarm-tenant-"

# tenant_namespace TENANT_ID  ->  the Kubernetes namespace that tenant's pods
# run in. A function rather than a bare interpolation at each call site so that
# the sweep in check-contract-parity.sh has exactly one assignment to find.
tenant_namespace() {
  printf '%s%s' "${TENANT_NAMESPACE_PREFIX}" "$1"
}

# states_json ARRAY_ELEMENTS...  ->  ["A","B",...]
# Used to hand one of the sets above to jq as --argjson, so a jq expression can
# iterate the set instead of naming its members. bash 3.2 has no way to pass an
# array to a function, so callers expand it: states_json "${CONCURRENCY_STATES[@]}".
states_json() {
  printf '%s\n' "$@" | jq -Rsc 'split("\n") | map(select(length > 0))'
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

#: Whether the Firestore database exists.
#:
#: THREE ANSWERS, NOT TWO. The exit code is the answer:
#:
#:     0  present
#:     1  absent -- a definite 404 from the Firestore Admin API
#:     2  could not tell -- the reason is already on stderr
#:
#: The two-valued version of this was a lie with a long reach. It ran
#: `gcloud firestore databases describe` with `>/dev/null 2>&1`, so an expired
#: session, a missing datastore.databases.get, a disabled API, a wrong REGION
#: and a genuinely absent database all returned the same `false` -- and every
#: caller rendered that one false as "does not exist. Run 'make infra' first".
#: scripts/status.sh had already diagnosed this and repeated the call locally
#: with stderr captured rather than fixing it here, which is the restatement
#: this file exists to prevent.
#:
#: REST, not gcloud, for the second reason: this is the ONLY gcloud call left
#: on the verification suites' path, and images/swarm-verify carries no Cloud
#: SDK on purpose (the SDK base ships unfixed HIGH/CRITICAL CVEs and `make
#: push` refuses to promote it). `require_platform` called this first, so the
#: in-VPC gate died on its opening guard with "Firestore database 'swarm' does
#: not exist" against a database holding live tenants -- a missing binary
#: wearing the costume of a missing platform, which is precisely the confusion
#: the gate was built to remove.
fs_database_exists() {
  local out rc=0 code
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-fsdb.XXXXXX")"
  # The token is captured FIRST, as a plain assignment. Inline as
  # `-H "Authorization: Bearer $(access_token)"` it runs in a subshell, so
  # access_token's `die` kills only that subshell and curl goes out with an
  # empty bearer -- which is exactly what happened on 2026-09-22: the metadata
  # failure was real, and this function then asked Firestore anyway and
  # reported the 401 as its own reason. The command-substitution trap CLAUDE.md
  # names, in code written to fix a different instance of it.
  local token
  token="$(access_token)" || return 2
  curl -sS --max-time "${HTTP_TIMEOUT:-30}" \
    -H "Authorization: Bearer ${token}" \
    "https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/${FIRESTORE_DATABASE}" \
    >"${out}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    err "could not ask whether Firestore database ${FIRESTORE_DATABASE} exists:"
    redact <"${out}" | head -n 3 | sed 's/^/     /' >&2
    rm -f "${out}"
    return 2
  fi
  # A 404 arrives as a JSON error body with a 200 from curl's point of view, so
  # the body decides -- not curl's exit code.
  if jq -e '.error' <"${out}" >/dev/null 2>&1; then
    code="$(jq -r '.error.code // 0' <"${out}")"
    if [[ "${code}" == "404" ]]; then
      rm -f "${out}"
      return 1
    fi
    err "could not ask whether Firestore database ${FIRESTORE_DATABASE} exists:"
    jq -r '.error.message // "unknown error"' <"${out}" | redact | head -n 3 | sed 's/^/     /' >&2
    rm -f "${out}"
    return 2
  fi
  rm -f "${out}"
  return 0
}

#: Die unless the Firestore database is present, naming the REAL cause.
#:
#: ACTION is what the caller was about to do ("pause", "purge"), so the absent
#: case reads as a sentence. Exists so that the difference between "absent" and
#: "could not look" is stated once rather than in each of the five scripts that
#: used `fs_database_exists || die "... does not exist"` and so flattened it.
require_fs_database() {
  local action="${1:-continue}" rc=0
  fs_database_exists || rc=$?
  case "${rc}" in
    0) return 0 ;;
    1) die "Firestore database ${FIRESTORE_DATABASE} does not exist in ${PROJECT_ID}; nothing to ${action}. Run 'make infra' first." ;;
    *) die "cannot confirm Firestore database ${FIRESTORE_DATABASE} exists, so refusing to ${action}. The reason is above; that is a failure to LOOK, not proof the database is absent -- check the account and the project (${PROJECT_ID}) before running 'make infra'." ;;
  esac
}

# ---------------------------------------------------------------------------
# IAM policies
# ---------------------------------------------------------------------------

# iam_policy_binds_member POLICY_JSON_FILE ROLE MEMBER
#
# True only when MEMBER is listed in a binding whose role is EXACTLY ROLE.
#
# This exists because grepping a policy document for one half of that pair is
# not a near-miss, it is a check that can never fail. The artifact bucket is a
# single bucket shared by every tenant, with ONE policy:
#
#   * `grep -q "${ROLE_ID}"` is true the moment ANY tenant holds the role --
#     and terraform/modules/tenancy grants swarmBucketMetadataReader to every
#     tenant in var.tenants -- so the first tenant's binding makes the check
#     true forever, for every tenant after it.
#   * `grep -q "serviceAccount:${GSA}"` is true the moment that member holds
#     ANY role on the bucket, so one grant present makes a different, missing
#     grant read as present.
#
# Either way the caller reports "already granted" and skips a binding it never
# made: a worker that cannot reach its own artifacts, with a transcript that
# says it can.
#
# An unreadable, empty or unexpected policy answers "not bound" (non-zero)
# rather than failing. Callers use this to decide whether to ADD a binding, and
# `add-iam-policy-binding` is idempotent, so guessing "not bound" costs a
# redundant write that fails loudly, while guessing "bound" is the silent
# missing grant above.
iam_policy_binds_member() {
  local policy_file="$1" role="$2" member="$3"
  [[ -s "${policy_file}" ]] || return 1
  jq -e --arg role "${role}" --arg member "${member}" \
    'any((.bindings? // [])[]; .role == $role and any(.members[]?; . == $member))' \
    "${policy_file}" >/dev/null 2>&1
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
