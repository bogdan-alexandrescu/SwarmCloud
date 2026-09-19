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
# The kubeconfig is written 0600. It carries a bearer credential for a
# Kubernetes API server, and build/ is an ordinary directory in a repository
# checkout.
#
# --merge UNDOES safety property 1 -- it writes the swarm context into
# ~/.kube/config alongside whatever production contexts are already there, which
# is exactly the hazard this script exists to avoid. It exists because some
# tooling cannot be told to use a different kubeconfig, and it requires a typed
# confirmation for the same reason a destroy does. docs/security.md names it.
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
    -h|--help)     sed -n '2,25p' "$0"; exit 0 ;;
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
DESCRIBE_ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/swarm-gke-describe.XXXXXX")"
if ! CLUSTER_JSON="$(gcloud container clusters describe "${GKE_CLUSTER}" \
      --project "${PROJECT_ID}" --location "${GKE_LOCATION}" --format=json 2>"${DESCRIBE_ERR_FILE}")"; then
  DESCRIBE_ERR="$(cat "${DESCRIBE_ERR_FILE}")"
  rm -f "${DESCRIBE_ERR_FILE}"
  # A denied container.clusters.get, an expired session, the Kubernetes Engine
  # API being disabled, and a zonal/regional GKE_LOCATION mismatch all produce
  # this exact same empty result as a genuinely missing cluster. stderr is the
  # only thing that tells them apart, so it is captured, not discarded.
  die_if_auth_failure "${DESCRIBE_ERR}"
  err "cluster ${GKE_CLUSTER} not found in ${GKE_LOCATION} -- or the lookup itself failed:"
  printf '%s\n' "${DESCRIBE_ERR}" | redact | head -n 5 | sed 's/^/     /' >&2
  dim "clusters visible in this project:"
  LIST_ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/swarm-gke-list.XXXXXX")"
  if ! gcloud container clusters list --project "${PROJECT_ID}" \
      --format='table(name,location,status,autopilot.enabled:label=AUTOPILOT)' >&2 2>"${LIST_ERR_FILE}"; then
    LIST_ERR="$(cat "${LIST_ERR_FILE}")"
    rm -f "${LIST_ERR_FILE}"
    # If this listing failed too, the empty table an operator would otherwise
    # see is not "this project has no clusters" -- it is the same broken
    # session or permission set that just failed the describe above.
    die_if_auth_failure "${LIST_ERR}"
    warn "listing clusters also failed; that is not proof this project has none"
    printf '%s\n' "${LIST_ERR}" | redact | head -n 3 | sed 's/^/     /' >&2
  else
    rm -f "${LIST_ERR_FILE}"
  fi
  die "run 'make infra' to create the swarm cluster, or pass --cluster/--location -- but check the message above first: this may be an auth problem, not a missing cluster"
fi
rm -f "${DESCRIBE_ERR_FILE}"

SERVER_VERSION="$(printf '%s' "${CLUSTER_JSON}" | jq -r '.currentMasterVersion // ""')"
AUTOPILOT="$(printf '%s' "${CLUSTER_JSON}" | jq -r 'if .autopilot.enabled then "yes" else "no" end')"
STATUS="$(printf '%s' "${CLUSTER_JSON}" | jq -r '.status // "UNKNOWN"')"
ok "status ${STATUS}, autopilot ${AUTOPILOT}, control plane ${SERVER_VERSION}"
[[ "${STATUS}" == "RUNNING" ]] || warn "cluster is ${STATUS}, not RUNNING"
[[ "${AUTOPILOT}" == "yes" ]] || warn "this is not an Autopilot cluster; the swarm's GKE path assumes Autopilot extended run time"

step "Credentials"
KUBECONFIG_PATH="${BUILD_DIR}/kubeconfig-${ENVIRONMENT}.yaml"
if [[ "${MERGE}" -eq 1 ]]; then
  MERGE_TARGET="${KUBECONFIG:-${HOME}/.kube/config}"
  warn "--merge writes the swarm context into ${MERGE_TARGET}, alongside your other contexts."
  warn "That is the shared-kubeconfig hazard this script exists to avoid: on this workstation the"
  warn "ACTIVE context was another team's live agents-staging cluster, with agents-prod in the same"
  warn "file. Every later 'kubectl delete' is then one 'use-context' away from the wrong cluster."
  warn "The isolated file at ${KUBECONFIG_PATH} needs no flag and every script here picks it up."
  # A safety-reducing action gets the same typed confirmation as a destructive
  # one, and SWARM_ASSUME_YES does not skip it.
  SWARM_ASSUME_YES="" confirm "This weakens the isolation the script provides." "merge"
else
  export KUBECONFIG="${KUBECONFIG_PATH}"
  rm -f "${KUBECONFIG_PATH}"
  # Created 0600 BEFORE gcloud writes into it: a kubeconfig holds a credential
  # for a Kubernetes API server, and build/ is a normal directory in a checkout.
  # Doing it afterwards leaves a window in which the file exists world-readable.
  ( umask 077 && : >"${KUBECONFIG_PATH}" )
  chmod 0600 "${KUBECONFIG_PATH}"
fi

gcloud container clusters get-credentials "${GKE_CLUSTER}" \
  --project "${PROJECT_ID}" --location "${GKE_LOCATION}" 2>&1 | redact

if [[ "${MERGE}" -eq 0 ]]; then
  chmod 0600 "${KUBECONFIG_PATH}"
fi

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
READYZ_ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/swarm-readyz.XXXXXX")"
if "${KUBECTL_BIN}" get --raw='/readyz' >/dev/null 2>"${READYZ_ERR_FILE}"; then
  ok "API server reachable"
else
  READYZ_ERR="$(cat "${READYZ_ERR_FILE}")"
  rm -f "${READYZ_ERR_FILE}"
  # A private control plane needing an authorised network or a bastion is only
  # one of several things a silent /readyz failure means: a missing or failing
  # gke-gcloud-auth-plugin, an expired gcloud session, RBAC denying the
  # non-resource URL /readyz, or a cluster still PROVISIONING/REPAIRING all
  # look identical from a bare exit code. `2>&1` used to discard the one line
  # that tells them apart.
  die_if_auth_failure "${READYZ_ERR}"
  warn "API server did not answer /readyz. This is not necessarily a private control plane needing"
  warn "an authorised network or a bastion -- it also looks like a missing auth plugin, a denied RBAC"
  warn "binding, or a cluster still provisioning. The actual error:"
  printf '%s\n' "${READYZ_ERR}" | redact | head -n 5 | sed 's/^/     /' >&2
fi
rm -f "${READYZ_ERR_FILE}"

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
