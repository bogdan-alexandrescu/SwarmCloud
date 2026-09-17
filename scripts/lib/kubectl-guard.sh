#!/usr/bin/env bash
# Self-test for bin/kubectl, the wrapper that stops a bare `kubectl` reaching
# another team's cluster.
#
# The wrapper exists because the guard it wraps was optional and got skipped.
# A self-test exists because a wrapper nothing exercises is a wrapper that stops
# working silently -- and its failure mode is not an error message, it is a
# command that quietly succeeds against someone else's production.
#
# Usage: scripts/lib/kubectl-guard.sh --self-test
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SELF_TEST=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --self-test) SELF_TEST=1; shift ;;
    -h|--help)   sed -n '2,10p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "${SELF_TEST}" -eq 1 ]] || die "this script only runs as --self-test"

PASS=0
FAIL=0

allowed() {
  local label="$1" ctx="$2"
  local trailing="${ctx##*_}"
  if is_shared_resource "${trailing}" || ! kube_context_allowed "${ctx}"; then
    err "${label}: would be REFUSED, but this context is ours"; FAIL=$((FAIL + 1))
  else
    ok "${label}"; PASS=$((PASS + 1))
  fi
}

refused() {
  local label="$1" ctx="$2"
  local trailing="${ctx##*_}"
  if is_shared_resource "${trailing}" || ! kube_context_allowed "${ctx}"; then
    ok "${label}"; PASS=$((PASS + 1))
  else
    err "${label}: would be ALLOWED. A command through this context reaches another team."
    FAIL=$((FAIL + 1))
  fi
}

step "kubectl-guard self-test"

# --- ours ------------------------------------------------------------------
allowed "the isolated kubeconfig's context" "swarm-${ENVIRONMENT}"
allowed "the gcloud-generated name for our cluster" \
  "gke_${PROJECT_ID}_${GKE_LOCATION}_${GKE_CLUSTER}"

# --- the ones that matter --------------------------------------------------
# This is the context that was actually current on the reference workstation,
# and the one a bare kubectl reached on 2026-09-18.
refused "another team's staging cluster" \
  "gke_saga-agents-staging_us-central1-a_agents-staging"
refused "another team's PRODUCTION cluster" \
  "gke_saga-agents-prod_us-central1_agents-prod"

# A context renamed to look like ours is still that cluster underneath. The
# trailing segment is what gcloud derives from the cluster name, so it is what
# the deny-list checks -- a rename cannot launder it.
refused "a foreign cluster renamed to look like ours" \
  "swarm-dev_gke_saga-agents-staging_us-central1-a_agents-staging"

refused "an empty context" ""
refused "docker-desktop" "docker-desktop"
refused "a minikube context" "minikube"
refused "an EKS context" "arn:aws:eks:us-east-1:1234:cluster/prod"

hr
if [[ "${FAIL}" -gt 0 ]]; then
  die "kubectl-guard self-test: ${PASS} passed, ${FAIL} failed"
fi
ok "kubectl-guard self-test: ${PASS}/${PASS} passed"
