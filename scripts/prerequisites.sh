#!/usr/bin/env bash
# Verify this workstation (or CI runner) can operate the swarm.
#
# Exits non-zero if anything REQUIRED is missing. Version drift from the pins in
# docs/versions.md is a warning, not a failure -- except for kubectl, where an
# old client against a 1.35 control plane silently drops manifest fields it does
# not recognise, which is worse than a hard failure.
#
# Usage: scripts/prerequisites.sh [--no-cloud] [--quiet]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

CHECK_CLOUD=1
QUIET=0
for arg in "$@"; do
  case "${arg}" in
    --no-cloud) CHECK_CLOUD=0 ;;
    --quiet)    QUIET=1 ;;
    -h|--help)  sed -n '2,10p' "$0"; exit 0 ;;
    *) die "unknown argument: ${arg}" ;;
  esac
done

FAILURES=0
WARNINGS=0

fail() { err "$*"; FAILURES=$((FAILURES + 1)); }
soft() { warn "$*"; WARNINGS=$((WARNINGS + 1)); }
pass() { [[ "${QUIET}" -eq 1 ]] || ok "$*"; }

# check_tool NAME PINNED_VERSION BINARY [ARGS...]
# An empty PINNED_VERSION means "report what is installed, pin nothing".
check_tool() {
  local name="$1" pinned="$2" bin="$3"; shift 3
  if [[ -z "${bin}" ]]; then
    fail "${name} not found${pinned:+ (docs/versions.md pins ${pinned})}"
    return
  fi
  local raw found
  raw="$("${bin}" "$@" 2>&1 | head -n 3 || true)"
  found="$(printf '%s' "${raw}" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1 || true)"
  if [[ -z "${found}" ]]; then
    soft "${name} at ${bin} did not report a usable version (want ${pinned:-any})"
    return
  fi
  if [[ -z "${pinned}" || "${found}" == "${pinned}" ]]; then
    pass "${name} ${found} (${bin})"
  else
    soft "${name} ${found} at ${bin} (docs/versions.md pins ${pinned})"
  fi
}

step "Tooling"
check_tool terraform "1.16.2" "$(terraform_bin)" version
check_tool tflint    "0.64.0" "$(tflint_bin || true)" --version
check_tool checkov   "3.3.17" "$(checkov_bin || true)" --version
check_tool trivy     "0.74.0" "$(trivy_bin || true)" --version
check_tool uv        ""       "$(command -v uv || true)" --version
check_tool jq        ""       "$(command -v jq || true)" --version
check_tool gcloud    ""       "$(command -v gcloud || true)" version

# Same shadowing trap as kubectl: report it rather than letting it surprise
# someone whose interactive shell resolves a different binary than the scripts.
for shadowed in checkov tflint trivy terraform; do
  resolved="$(prefer_local_bin "${shadowed}" || true)"
  on_path="$(command -v "${shadowed}" 2>/dev/null || true)"
  if [[ -n "${resolved}" && -n "${on_path}" && "${resolved}" != "${on_path}" ]]; then
    soft "${shadowed}: scripts use ${resolved}, your shell resolves ${on_path}"
  fi
done

for required in curl python3 git; do
  if command -v "${required}" >/dev/null 2>&1; then
    pass "${required} present"
  else
    fail "${required} not found"
  fi
done

if command -v shellcheck >/dev/null 2>&1; then
  pass "shellcheck $(shellcheck --version | awk '/^version:/{print $2}')"
else
  soft "shellcheck not found; 'make lint' will skip shell linting"
fi

step "kubectl (hard requirement: >= 1.30 for the 1.35 control plane)"
if KUBECTL_PATH="$(kubectl_bin 2>/dev/null)"; then
  KUBECTL_VERSION="$("${KUBECTL_PATH}" version --client 2>/dev/null \
    | grep -oE 'v?[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)"
  pass "kubectl ${KUBECTL_VERSION} at ${KUBECTL_PATH}"
  SHADOW="$(command -v kubectl 2>/dev/null || true)"
  if [[ -n "${SHADOW}" && "${SHADOW}" != "${KUBECTL_PATH}" ]]; then
    soft "an older kubectl shadows it on \$PATH: ${SHADOW} -- scripts resolve ${KUBECTL_PATH} explicitly, but your own 'kubectl' does not"
  fi
else
  fail "no kubectl >= 1.30 on this machine (set SWARM_KUBECTL to one)"
fi

step "Python"
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
if [[ "${PY_VERSION}" == "3.11" ]]; then
  pass "python3 ${PY_VERSION}"
elif [[ -n "${PY_VERSION}" ]]; then
  soft "python3 ${PY_VERSION}; this repo targets 3.11 (uv reads .python-version and will fetch it)"
else
  fail "python3 not usable"
fi

if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node --version 2>/dev/null | grep -oE '[0-9]+' | head -n 1)"
  if [[ "${NODE_MAJOR}" == "20" ]]; then
    pass "node $(node --version)"
  else
    soft "node $(node --version); agent runtime images pin Node LTS 20"
  fi
else
  soft "node not found; only needed to exercise the agent runtime locally"
fi

step "Local development loop"
if java -version >/dev/null 2>&1; then
  pass "java $(java -version 2>&1 | head -n 1 | grep -oE '[0-9]+(\.[0-9]+)*' | head -n 1) (the Firestore emulator needs it)"
else
  soft "no Java runtime: 'make dev' cannot start the Firestore emulator. Install with: brew install --cask temurin"
fi
if gcloud components list --only-local-state --format='value(id)' 2>/dev/null | grep -q cloud-firestore-emulator; then
  pass "cloud-firestore-emulator component installed"
else
  soft "cloud-firestore-emulator not installed: gcloud components install cloud-firestore-emulator"
fi

step "Local container runtime (optional path)"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  pass "docker daemon reachable; docker-compose.yml is usable"
else
  soft "no working docker daemon -- expected on this machine. Images build with Cloud Build ('make build'), and the local loop is 'make dev' (gcloud emulators firestore)."
fi

if [[ "${CHECK_CLOUD}" -eq 0 ]]; then
  hr
  [[ "${FAILURES}" -eq 0 ]] || die "${FAILURES} required check(s) failed"
  ok "local prerequisites satisfied (${WARNINGS} warning(s)); cloud checks skipped"
  exit 0
fi

step "Google Cloud"
ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
if [[ -z "${ACCOUNT}" || "${ACCOUNT}" == "(unset)" ]]; then
  fail "no active gcloud account; run: gcloud auth login"
else
  pass "account ${ACCOUNT}"
  case "${ACCOUNT}" in
    *@saga.xyz) ;;
    *) soft "account ${ACCOUNT} is outside saga.xyz; the API only accepts saga.xyz identities" ;;
  esac
fi

if gcloud projects describe "${PROJECT_ID}" --format='value(projectId)' >/dev/null 2>&1; then
  pass "project ${PROJECT_ID} reachable"
else
  fail "cannot describe project ${PROJECT_ID}"
fi

if gcloud auth application-default print-access-token >/dev/null 2>&1; then
  pass "application default credentials present"
else
  soft "no ADC; terraform and the Python services need it: gcloud auth application-default login"
fi

step "Required APIs"
REQUIRED_APIS=(
  artifactregistry.googleapis.com
  cloudbuild.googleapis.com
  cloudidentity.googleapis.com
  cloudresourcemanager.googleapis.com
  cloudscheduler.googleapis.com
  compute.googleapis.com
  container.googleapis.com
  firestore.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  logging.googleapis.com
  monitoring.googleapis.com
  pubsub.googleapis.com
  run.googleapis.com
  secretmanager.googleapis.com
  serviceusage.googleapis.com
  storage.googleapis.com
  sts.googleapis.com
)
ENABLED_FILE="${BUILD_DIR}/enabled-apis.txt"
if gcloud services list --enabled --project "${PROJECT_ID}" \
     --format='value(config.name)' >"${ENABLED_FILE}" 2>/dev/null; then
  MISSING_APIS=()
  for api in "${REQUIRED_APIS[@]}"; do
    grep -qx "${api}" "${ENABLED_FILE}" || MISSING_APIS+=("${api}")
  done
  if [[ "${#MISSING_APIS[@]}" -eq 0 ]]; then
    pass "all ${#REQUIRED_APIS[@]} required APIs enabled"
  else
    fail "APIs not enabled: ${MISSING_APIS[*]}"
    dim "enable with: gcloud services enable ${MISSING_APIS[*]} --project ${PROJECT_ID}"
  fi
else
  soft "could not list enabled APIs (insufficient permission?)"
fi

step "Shared-project guard rails"
dim "saga-agents-staging is SHARED. These belong to other teams and nothing in this repo may touch them:"
dim "  GKE cluster agents-staging | VPC agents-staging-vpc | 12 service accounts | 3 pre-existing buckets"
if is_shared_resource "${GKE_CLUSTER}"; then
  fail "GKE_CLUSTER=${GKE_CLUSTER} is a deny-listed shared resource; the swarm needs its own cluster"
else
  pass "GKE_CLUSTER=${GKE_CLUSTER} is not a shared resource"
fi
if is_shared_resource "${ARTIFACT_BUCKET}"; then
  fail "ARTIFACT_BUCKET=${ARTIFACT_BUCKET} is a deny-listed shared bucket"
else
  pass "ARTIFACT_BUCKET=${ARTIFACT_BUCKET} is not a shared bucket"
fi
if [[ "${FIRESTORE_DATABASE}" == "(default)" ]]; then
  fail "FIRESTORE_DATABASE is (default); the swarm must use a named database in a shared project"
else
  pass "Firestore database '${FIRESTORE_DATABASE}' (not the shared (default))"
fi

hr
if [[ "${FAILURES}" -gt 0 ]]; then
  die "${FAILURES} required check(s) failed, ${WARNINGS} warning(s)"
fi
ok "prerequisites satisfied (${WARNINGS} warning(s))"
