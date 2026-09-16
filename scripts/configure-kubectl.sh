#!/usr/bin/env bash
# Point kubectl at the swarm's own Autopilot cluster -- and at nothing else.
#
# Two safety properties this script exists to provide:
#
#   1. It writes an ISOLATED kubeconfig (build/kubeconfig-<env>.yaml) instead of
#      merging into ~/.kube/config. An operator's default kubeconfig usually has
#      a production context in it, and `kubectl delete` against the wrong current
#      context is the classic way to have a very bad afternoon.
#   2. It REFUSES to fetch credentials for any cluster on the shared deny-list.
#      agents-staging is a live cluster owned by another team in this same
#      project; this repo must never hold credentials for it.
#
# Usage: scripts/configure-kubectl.sh [--cluster NAME] [--location LOC] [--merge]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

MERGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster|-c)  GKE_CLUSTER="$2"; shift 2 ;;
    --location|-l) GKE_LOCATION="$2"; shift 2 ;;
    --merge)       MERGE=1; shift ;;
    -h|--help)     sed -n '2,15p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud
KUBECTL_BIN="$(kubectl_bin)"

step "Target"
info "cluster   ${GKE_CLUSTER}"
info "location  ${GKE_LOCATION}"
info "project   ${PROJECT_ID}"
info "kubectl   ${KUBECTL_BIN}"

if is_shared_resource "${GKE_CLUSTER}"; then
  err "${GKE_CLUSTER} belongs to another team in this shared project."
  err "This repo never configures credentials for it. Set GKE_CLUSTER in .env to the swarm's own cluster (default: swarm-autopilot)."
  exit 1
fi

if ! command -v gke-gcloud-auth-plugin >/dev/null 2>&1; then
  err "gke-gcloud-auth-plugin is missing; kubectl >= 1.26 cannot authenticate to GKE without it"
  die "install it with: gcloud components install gke-gcloud-auth-plugin"
fi
export USE_GKE_GCLOUD_AUTH_PLUGIN=True

step "Cluster lookup"
if ! CLUSTER_JSON="$(gcloud container clusters describe "${GKE_CLUSTER}" \
      --project "${PROJECT_ID}" --location "${GKE_LOCATION}" --format=json 2>/dev/null)"; then
  err "cluster ${GKE_CLUSTER} not found in ${GKE_LOCATION}"
  dim "clusters visible in this project:"
  gcloud container clusters list --project "${PROJECT_ID}" \
    --format='table(name,location,status,autopilot.enabled:label=AUTOPILOT)' >&2 || true
  die "run 'make infra' to create the swarm cluster, or pass --cluster/--location"
fi

SERVER_VERSION="$(printf '%s' "${CLUSTER_JSON}" | jq -r '.currentMasterVersion // ""')"
AUTOPILOT="$(printf '%s' "${CLUSTER_JSON}" | jq -r 'if .autopilot.enabled then "yes" else "no" end')"
STATUS="$(printf '%s' "${CLUSTER_JSON}" | jq -r '.status // "UNKNOWN"')"
ok "status ${STATUS}, autopilot ${AUTOPILOT}, control plane ${SERVER_VERSION}"
[[ "${STATUS}" == "RUNNING" ]] || warn "cluster is ${STATUS}, not RUNNING"
[[ "${AUTOPILOT}" == "yes" ]] || warn "this is not an Autopilot cluster; the swarm's GKE path assumes Autopilot extended run time"

step "Credentials"
KUBECONFIG_PATH="${BUILD_DIR}/kubeconfig-${ENVIRONMENT}.yaml"
if [[ "${MERGE}" -eq 1 ]]; then
  warn "--merge writes into ${KUBECONFIG:-${HOME}/.kube/config}, alongside your other contexts"
else
  export KUBECONFIG="${KUBECONFIG_PATH}"
  rm -f "${KUBECONFIG_PATH}"
fi

gcloud container clusters get-credentials "${GKE_CLUSTER}" \
  --project "${PROJECT_ID}" --location "${GKE_LOCATION}" 2>&1 | redact

GENERATED_CONTEXT="gke_${PROJECT_ID}_${GKE_LOCATION}_${GKE_CLUSTER}"
DESIRED_CONTEXT="swarm-${ENVIRONMENT}"
if "${KUBECTL_BIN}" config get-contexts "${GENERATED_CONTEXT}" >/dev/null 2>&1; then
  "${KUBECTL_BIN}" config rename-context "${GENERATED_CONTEXT}" "${DESIRED_CONTEXT}" >/dev/null 2>&1 || true
fi
"${KUBECTL_BIN}" config use-context "${DESIRED_CONTEXT}" >/dev/null 2>&1 \
  || "${KUBECTL_BIN}" config use-context "${GENERATED_CONTEXT}" >/dev/null 2>&1 || true
ok "context $("${KUBECTL_BIN}" config current-context)"

step "Version skew"
CLIENT_MINOR="$("${KUBECTL_BIN}" version --client 2>/dev/null \
  | grep -oE 'v?[0-9]+\.[0-9]+' | head -n 1 | tr -d 'v' | cut -d. -f2)"
SERVER_MINOR="$(printf '%s' "${SERVER_VERSION}" | cut -d. -f2)"
if [[ -n "${CLIENT_MINOR}" && -n "${SERVER_MINOR}" ]]; then
  SKEW=$(( CLIENT_MINOR - SERVER_MINOR ))
  [[ "${SKEW}" -lt 0 ]] && SKEW=$(( -SKEW ))
  if [[ "${SKEW}" -le 1 ]]; then
    ok "client 1.${CLIENT_MINOR} vs server 1.${SERVER_MINOR} (within one minor)"
  else
    warn "client 1.${CLIENT_MINOR} vs server 1.${SERVER_MINOR}: ${SKEW} minors apart"
    warn "an out-of-skew client can drop fields it does not understand when applying manifests"
  fi
fi

step "Connectivity"
if "${KUBECTL_BIN}" get --raw='/readyz' >/dev/null 2>&1; then
  ok "API server reachable"
else
  warn "API server did not answer /readyz; a private control plane needs an authorised network or a bastion"
fi

if NAMESPACES="$("${KUBECTL_BIN}" get namespaces -l managed-by=swarm-terraform \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)"; then
  if [[ -n "${NAMESPACES}" ]]; then
    ok "swarm namespaces:"
    printf '%s\n' "${NAMESPACES}" | sed 's/^/    /' >&2
  else
    info "no swarm-managed namespaces yet; register a tenant to create one"
  fi
fi

hr
ok "kubectl configured"
if [[ "${MERGE}" -eq 0 ]]; then
  cat >&2 <<EOF
This kubeconfig is isolated from your default one. To use it in your shell:

    export KUBECONFIG=${KUBECONFIG_PATH}
    ${KUBECTL_BIN} get pods -A

Scripts in this repo (status.sh, smoke-test.sh) pick it up automatically.
EOF
fi
