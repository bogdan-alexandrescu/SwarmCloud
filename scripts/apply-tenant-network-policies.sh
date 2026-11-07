#!/usr/bin/env bash
# Re-apply the tenant NetworkPolicy set to EVERY tenant namespace on the swarm
# cluster: each namespace labelled `swarm-tenant`, whoever created it.
#
#   scripts/apply-tenant-network-policies.sh                     # dry run: diff + server verdicts
#   scripts/apply-tenant-network-policies.sh --confirm           # apply, read back, parity sweep
#   scripts/apply-tenant-network-policies.sh --get-credentials --confirm   # what the release runs
#   scripts/apply-tenant-network-policies.sh --list-files        # the policy files applied; offline
#
# WHY THIS EXISTS (lane H1-NETPOL). On 2026-10-11 the live browser needed
# `swarm-allow-live-egress` (kubernetes/network-policies/allow-live-egress.yaml)
# in each tenant namespace, and no release applied it: the operator ran
# `kubernetes/apply.sh --tenant` by hand for eng, smoke and u-sw-c90291. Two
# gaps, both closed here:
#
#   * the release's tenant apply (.github/actions/release-namespaces) is off
#     unless vars.APPLY_TENANT_NAMESPACES is 'true', and
#   * it walks `terraform output tenant_namespaces`, which never names a
#     workspace tenant (`swarm-tenant-u-*`, made by register-tenant.sh
#     --workspace). The cluster's own label does: every tenant namespace render
#     carries `swarm-tenant: <id>` (kubernetes/namespaces/tenant-namespace.yaml).
#
# ONLY THE NETWORK POLICIES. A full `apply.sh --tenant` also re-renders the
# namespace's quota, accounts and RBAC from defaults, which would overwrite a
# workspace tenant's own quota and GSA. The policies depend on nothing but the
# namespace, the tenant id and the cluster's network, so they are safe to
# re-apply everywhere. `scripts/tenant_netpol.py` renders them through
# kubernetes/render.py; the set is render.py's TENANT_FILES `network-policies/`
# entries, and tests/unit/scripts/test_tenant_netpol_apply.py holds that equal
# to the tenant policy files on disk, so a new one cannot be forgotten.
#
# IDEMPOTENT. `kubectl apply` of the same render reports `unchanged`. Nothing is
# deleted: a policy removed from the repository stays on the cluster until
# someone deletes it deliberately.
#
# SAFETY, in apply.sh's terms and with common.sh's deny-list:
#   * kubectl from `kubectl_bin` (an old client silently drops fields);
#   * the cluster must start with `swarm-` and match nothing on SHARED_DENY_LIST,
#     and the context's kubeconfig cluster entry must be exactly
#     gke_<PROJECT_ID>_<GKE_LOCATION>_<cluster> -- the cluster whose network is
#     read is the cluster written to, never `agents-staging`;
#   * a labelled namespace outside `swarm-tenant-` is refused, not applied;
#   * the cluster's network is READ (kubernetes/cluster-network.sh), never typed;
#   * under --confirm, every rendered policy is read back by name in every
#     namespace, then scripts/lib/check-cluster-network-parity.sh --require-live
#     sweeps every tenant egress policy against the live network.
#
# Exit: 0 every tenant namespace carries the full set; 1 anything refused,
# failed, or no tenant namespace was found (an empty sweep is not a success).
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# The one reader of the cluster's network, shared with kubernetes/apply.sh and
# the parity check.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../kubernetes/cluster-network.sh
source "${REPO_ROOT}/kubernetes/cluster-network.sh"

PYTHON="${PYTHON_BIN:-python3}"
HELPER="${REPO_ROOT}/scripts/tenant_netpol.py"
PARITY="${REPO_ROOT}/scripts/lib/check-cluster-network-parity.sh"
TENANT_LABEL="swarm-tenant"

CONFIRM=0
GET_CREDENTIALS=0
CONTEXT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm)          CONFIRM=1; shift ;;
    --get-credentials)  GET_CREDENTIALS=1; shift ;;
    --context)          CONTEXT="$2"; shift 2 ;;
    --context=*)        CONTEXT="${1#*=}"; shift ;;
    --cluster)          GKE_CLUSTER="$2"; shift 2 ;;
    --cluster=*)        GKE_CLUSTER="${1#*=}"; shift ;;
    --list-files)       exec "${PYTHON}" "${HELPER}" --list ;;
    -h|--help)          awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0"; exit 0 ;;
    *)                  die "unknown argument: $1 (see --help)" ;;
  esac
done

# --- the cluster, and only ours ----------------------------------------------
# refuse_foreign WHAT REF -- die when the cluster REF names matches a
# cluster-shaped entry of the shared deny-list (apply.sh's rule: the cluster
# segment, not the whole string, since our project id contains the other
# team's cluster name).
refuse_foreign() {
  local what="$1" ref="$2" cluster foreign
  cluster="${ref##*_}"
  for foreign in ${SHARED_DENY_LIST[@]+"${SHARED_DENY_LIST[@]}"}; do
    case "${foreign}" in *@*|default) continue ;; esac
    case "${cluster}" in
      *"${foreign}"*) die "${what} names cluster '${cluster}', which matches '${foreign}' on the shared
       deny-list: it belongs to another team. Refusing." ;;
    esac
  done
}

case "${GKE_CLUSTER}" in
  swarm-*) ;;
  *) die "cluster '${GKE_CLUSTER}' does not start with 'swarm-'; refusing." ;;
esac
refuse_foreign "--cluster '${GKE_CLUSTER}'" "${GKE_CLUSTER}"
EXPECTED_ENTRY="gke_${PROJECT_ID}_${GKE_LOCATION}_${GKE_CLUSTER}"

step "Tenant network policies (${GKE_CLUSTER}, ${PROJECT_ID}/${GKE_LOCATION})"

if [[ "${GET_CREDENTIALS}" -eq 1 ]]; then
  # The release's runner has gcloud but no kubeconfig entry for the cluster.
  # The cluster name was checked above; get-credentials writes gcloud's own
  # context name, which is exactly EXPECTED_ENTRY.
  if ! command -v gke-gcloud-auth-plugin >/dev/null 2>&1; then
    gcloud components install gke-gcloud-auth-plugin --quiet
  fi
  gcloud container clusters get-credentials "${GKE_CLUSTER}" \
    --project "${PROJECT_ID}" --location "${GKE_LOCATION}"
  [[ -n "${CONTEXT}" ]] || CONTEXT="${EXPECTED_ENTRY}"
fi

KUBECTL="$(kubectl_bin)"
info "kubectl  ${KUBECTL}"
if [[ -z "${CONTEXT}" ]]; then
  CONTEXT="$("${KUBECTL}" config current-context 2>/dev/null || true)"
fi
[[ -n "${CONTEXT}" ]] || die "no kubectl context is selected; run scripts/configure-kubectl.sh"
refuse_foreign "context '${CONTEXT}'" "${CONTEXT}"
ENTRY="$("${KUBECTL}" config view -o \
  "jsonpath={.contexts[?(@.name==\"${CONTEXT}\")].context.cluster}" 2>/dev/null || true)"
[[ -n "${ENTRY}" ]] || die "could not resolve which cluster context '${CONTEXT}' points at; refusing."
refuse_foreign "context '${CONTEXT}' -> kubeconfig cluster '${ENTRY}'" "${ENTRY}"
if [[ "${ENTRY}" != "${EXPECTED_ENTRY}" ]]; then
  die "context '${CONTEXT}' points at kubeconfig cluster '${ENTRY}', not '${EXPECTED_ENTRY}';
       refusing. The network read and the policies written must be the same cluster's."
fi
info "context  ${CONTEXT} -> ${ENTRY}"
KARGS=(--context "${CONTEXT}")

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-tenant-netpol.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# Called directly, not in $(...): it sets its results as variables.
resolve_cluster_network "${KUBECTL}" "${GKE_CLUSTER}" "${GKE_LOCATION}" "${PROJECT_ID}" "${KARGS[@]}" \
  || die "could not read ${GKE_CLUSTER}'s network (above); nothing was rendered or applied."
info "network  pods=${CLUSTER_POD_CIDR} services=${CLUSTER_SERVICE_CIDR}"
info "dns      node-local=${CLUSTER_NODE_LOCAL_DNS_IP} kube-dns=${CLUSTER_DNS_IP}  (${CLUSTER_NETWORK_SOURCE})"

"${PYTHON}" "${HELPER}" --list >"${WORK}/files" || die "could not list the tenant policy files"
info "policy set  $(tr '\n' ' ' <"${WORK}/files")"

# --- every tenant namespace, by the cluster's own label ----------------------
if ! "${KUBECTL}" "${KARGS[@]}" get namespaces -l "${TENANT_LABEL}" -o json \
    >"${WORK}/namespaces.json" 2>"${WORK}/namespaces.err"; then
  redact <"${WORK}/namespaces.err" | sed -n '1,5p' | sed 's/^/     /' >&2
  die "could not list the namespaces labelled ${TENANT_LABEL}; nothing was applied."
fi
jq -r --arg l "${TENANT_LABEL}" \
  '.items[] | [.metadata.name, (.metadata.labels[$l] // "")] | @tsv' \
  "${WORK}/namespaces.json" >"${WORK}/namespaces.tsv" \
  || die "kubectl's namespace list is not JSON; nothing was applied."

visited=0
applied=0
FAILED=()
TAB="$(printf '\t')"
while IFS="${TAB}" read -r ns tenant; do
  [[ -n "${ns}" ]] || continue
  visited=$((visited + 1))
  case "${ns}" in
    "${TENANT_NAMESPACE_PREFIX}"?*) ;;
    *) err "${ns} is labelled ${TENANT_LABEL}=${tenant} but is not a ${TENANT_NAMESPACE_PREFIX}* namespace; refused"
       FAILED+=("${ns}"); continue ;;
  esac
  if is_shared_resource "${ns}" || [[ -z "${tenant}" ]]; then
    err "${ns}: shared-list name or empty ${TENANT_LABEL} label; refused"
    FAILED+=("${ns}"); continue
  fi
  manifest="${WORK}/${ns}.yaml"
  objects="${WORK}/${ns}.tsv"
  # render.py validates the tenant id and refuses a namespace outside the prefix.
  if ! "${PYTHON}" "${HELPER}" render --tenant "${tenant}" --namespace "${ns}" \
      --project "${PROJECT_ID}" --region "${REGION}" \
      --pod-cidr "${CLUSTER_POD_CIDR}" \
      --service-cidr "${CLUSTER_SERVICE_CIDR}" \
      --cluster-dns-ip "${CLUSTER_DNS_IP}" \
      --node-local-dns-ip "${CLUSTER_NODE_LOCAL_DNS_IP}" \
      --network-source "${CLUSTER_NETWORK_SOURCE}" \
      --summary "${objects}" >"${manifest}"; then
    err "${ns}: could not render the tenant policies (above)"
    FAILED+=("${ns}"); continue
  fi
  if [[ "${CONFIRM}" -ne 1 ]]; then
    info "${ns} (tenant ${tenant}): what --confirm would change"
    "${KUBECTL}" "${KARGS[@]}" diff -f "${manifest}" | redact || true   # diff exits 1 on a difference
    if ! "${KUBECTL}" "${KARGS[@]}" apply --dry-run=server -f "${manifest}" 2>&1 | redact; then
      err "${ns}: the server dry run failed (above)"
      FAILED+=("${ns}")
    fi
    continue
  fi
  info "${ns} (tenant ${tenant})"
  if ! "${KUBECTL}" "${KARGS[@]}" apply -f "${manifest}" 2>&1 | redact; then
    err "${ns}: kubectl apply failed (above)"
    FAILED+=("${ns}"); continue
  fi
  # READ BACK, BY NAME: the apply's own report is not the cluster's state.
  # Only a NotFound answer means missing. Any other failed get is retried, 3
  # tries 2 s apart, and one that never succeeds fails the namespace as
  # UNREADABLE, with its error. Release 40034319802 (2026-10-23) failed its
  # deploy on swarm-allow-typesafe-fwd in swarm-tenant-u-devops, a policy the
  # apply had just reported "unchanged": one get failed, and any failed get was
  # then counted as "not found", with its stderr thrown away (#2185).
  missing=""
  unreadable=""
  readerr="${WORK}/${ns}.readback.err"
  while IFS="${TAB}" read -r _kind name _namespace; do
    [[ -n "${name}" ]] || continue
    tries=0
    while :; do
      tries=$((tries + 1))
      if "${KUBECTL}" "${KARGS[@]}" get networkpolicies "${name}" --namespace "${ns}" -o name \
          >/dev/null 2>"${readerr}"; then
        break
      fi
      if grep -q 'NotFound' "${readerr}"; then
        missing="${missing} ${name}"; break
      fi
      if [[ "${tries}" -ge 3 ]]; then
        unreadable="${unreadable} ${name}"
        cp "${readerr}" "${readerr}.last"
        break
      fi
      sleep 2
    done
  done <"${objects}"
  if [[ -n "${missing}" ]]; then
    err "${ns}: applied, but not found on read-back:${missing}"
  fi
  if [[ -n "${unreadable}" ]]; then
    err "${ns}: applied, but could not be read back:${unreadable}"
    redact <"${readerr}.last" | sed -n '1,5p' | sed 's/^/     /' >&2
  fi
  if [[ -n "${missing}" || -n "${unreadable}" ]]; then
    FAILED+=("${ns}"); continue
  fi
  applied=$((applied + 1))
done <"${WORK}/namespaces.tsv"

info "visited ${visited} namespace(s) labelled ${TENANT_LABEL}"
[[ "${visited}" -gt 0 ]] || die "no namespace is labelled ${TENANT_LABEL} on ${GKE_CLUSTER}; nothing was applied.
       A swarm cluster with no tenant namespace is not one this sweep can call clean."
if [[ "${#FAILED[@]}" -gt 0 ]]; then
  die "${#FAILED[@]} of ${visited} tenant namespace(s) failed: ${FAILED[*]}"
fi
if [[ "${CONFIRM}" -ne 1 ]]; then
  ok "dry run over ${visited} namespace(s). Re-run with --confirm to apply."
  exit 0
fi
ok "applied the tenant policy set to ${applied} of ${visited} namespace(s); every policy read back"

# The sweep, not one namespace: every tenant egress policy against the network.
"${PARITY}" --require-live --context "${CONTEXT}" --cluster "${GKE_CLUSTER}" \
  || die "applied, but a tenant egress policy does not match the live cluster's network (above)"
