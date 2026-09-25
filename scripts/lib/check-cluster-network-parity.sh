#!/usr/bin/env bash
# Every applied tenant egress policy against the LIVE cluster's network.
#
#   scripts/lib/check-cluster-network-parity.sh                  # skips without credentials
#   scripts/lib/check-cluster-network-parity.sh --require-live   # a skip is a failure
#   scripts/lib/check-cluster-network-parity.sh --context swarm-dev
#
# THE MIRRORED COPY. `swarm-allow-worker-egress` carries four values that
# belong to the cluster: its pod range, its service range, the kube-dns Service
# IP and the NodeLocal DNSCache address. kubernetes/apply.sh now reads them from
# the cluster when it renders, but an applied policy is a COPY, and a copy goes
# stale the moment the cluster changes or someone applies a render by hand. On
# 2026-09-24 the applied copy agreed with none of the four -- it had been
# rendered from renderer defaults, and its only DNS rule reached kube-dns pods
# that a Cloud DNS + NodeLocal DNSCache cluster never sends a query to -- and a
# browser worker ran 390 s with every lookup silently dropped. Nothing compared
# the copy with the cluster. This does. docs/mirrored-values.md is the register
# of such copies (this one is not in it yet; see the PR that added this file).
#
# WHY NOT A SECTION OF check-contract-parity.sh. That script is part of
# `make test`, which is fully offline by rule, and it refuses to skip. This one
# needs the live cluster, so it cannot be offline, and in CI -- where there are
# no cloud credentials -- it cannot run at all. It therefore SKIPS WITH A
# NOTICE there (a GitHub `::notice`, so the run page says it did not compare
# anything) instead of passing as though it had. Run it with --require-live
# after an apply, where a skip would be a false green.
#
# READ-ONLY. One `gcloud container clusters describe`, and `kubectl get` on the
# kube-dns Service, the node-local-dns DaemonSet and the egress policies, all
# through the swarm cluster's context -- checked first, the same way every other
# script here checks it, because this kubeconfig reaches other teams' clusters.
#
# Exit status: 0 when every policy matches (or the check was skipped without
# --require-live); 1 when any differs, none was found, or the cluster could
# not be read.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
# The one reader of the cluster's network, shared with kubernetes/apply.sh, so
# the value checked is read exactly the way the value rendered was.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../../kubernetes/cluster-network.sh
source "${REPO_ROOT}/kubernetes/cluster-network.sh"

REQUIRE_LIVE=0
CONTEXT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --require-live) REQUIRE_LIVE=1; shift ;;
    --context)      CONTEXT="$2"; shift 2 ;;
    -h|--help)      sed -n '2,36p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

skip() {
  local reason="$1"
  if [[ "${REQUIRE_LIVE}" -eq 1 ]]; then
    die "cluster network parity was NOT checked: ${reason}. --require-live makes that a failure."
  fi
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    # stdout: GitHub reads workflow commands from it.
    printf '::notice title=cluster network parity skipped::%s. No applied egress policy was compared with the live cluster; run scripts/lib/check-cluster-network-parity.sh --require-live with credentials.\n' "${reason}"
  fi
  warn "cluster network parity SKIPPED: ${reason}"
  dim "  nothing was compared. With credentials: $0 --require-live"
  exit 0
}

step "Cluster network parity"

# --- credentials ------------------------------------------------------------
# Checked before anything touches kubectl, so a runner with no credentials
# never reaches for a cluster at all.
command -v gcloud >/dev/null 2>&1 || skip "gcloud is not installed"
ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | sed -n '1p' || true)"
[[ -n "${ACCOUNT}" ]] || skip "no active gcloud credentials"
info "account  ${ACCOUNT}"

# --- the swarm cluster, and only it -----------------------------------------
KUBECTL="$(kubectl_bin)"
KARGS=()
if [[ -n "${CONTEXT}" ]]; then
  if is_shared_resource "${CONTEXT##*_}" || ! kube_context_allowed "${CONTEXT}"; then
    die "context '${CONTEXT}' is not the swarm cluster's; refusing to read through it.
       Expected 'swarm-${ENVIRONMENT}' or 'gke_${PROJECT_ID}_${GKE_LOCATION}_${GKE_CLUSTER}'."
  fi
  KARGS+=(--context "${CONTEXT}")
else
  assert_kube_context
fi

resolve_cluster_network "${KUBECTL}" "${GKE_CLUSTER}" "${GKE_LOCATION}" "${PROJECT_ID}" \
  ${KARGS[@]+"${KARGS[@]}"} \
  || die "could not read the live network of ${GKE_CLUSTER} (above); nothing was compared"
info "live     pods=${CLUSTER_POD_CIDR} services=${CLUSTER_SERVICE_CIDR}"
info "live     dns node-local=${CLUSTER_NODE_LOCAL_DNS_IP} kube-dns=${CLUSTER_DNS_IP}  (${CLUSTER_NETWORK_SOURCE})"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-network-parity.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

jq -n \
  --arg pod "${CLUSTER_POD_CIDR}" \
  --arg svc "${CLUSTER_SERVICE_CIDR}" \
  --arg dns "${CLUSTER_DNS_IP}" \
  --arg nodelocal "${CLUSTER_NODE_LOCAL_DNS_IP}" \
  --arg source "${CLUSTER_NETWORK_SOURCE}" \
  '{pod_cidr: $pod, service_cidr: $svc, cluster_dns_ip: $dns, node_local_dns_ip: $nodelocal, source: $source}' \
  >"${WORK}/live.json"

# Every copy, in every namespace: a stale policy in a tenant nobody is looking
# at is still a tenant whose workers cannot resolve a name.
if ! "${KUBECTL}" ${KARGS[@]+"${KARGS[@]}"} get networkpolicies --all-namespaces \
      --field-selector metadata.name=swarm-allow-worker-egress -o json \
      >"${WORK}/policies.json" 2>"${WORK}/policies.err"; then
  err "could not list the egress policies:"
  redact <"${WORK}/policies.err" | sed -n '1,5p' | sed 's/^/     /' >&2
  die "nothing was compared"
fi

RC=0
"${PYTHON_BIN:-python3}" "${REPO_ROOT}/kubernetes/network_parity.py" \
  --live "${WORK}/live.json" \
  --policies "${WORK}/policies.json" \
  --namespace-prefix "${TENANT_NAMESPACE_PREFIX}" || RC=$?

if [[ "${RC}" -ne 0 ]]; then
  err "an applied egress policy differs from ${CLUSTER_NETWORK_SOURCE} (above)"
  dim "  re-render from the live cluster: kubernetes/apply.sh --tenant <id>, then --confirm"
  exit 1
fi
ok "every applied egress policy matches ${CLUSTER_NETWORK_SOURCE}"
