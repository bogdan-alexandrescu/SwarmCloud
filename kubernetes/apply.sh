#!/usr/bin/env bash
# Render and apply the tenant manifests in this directory.
#
#   kubernetes/apply.sh --tenant eng                 # dry run, prints a diff
#   kubernetes/apply.sh --tenant eng --confirm       # actually applies
#   kubernetes/apply.sh --policies --confirm         # cluster-scoped policies
#
# Three safety properties, in the order they matter.
#
# 1. The right kubectl. An old EKS kubectl 1.22 shadows the current one on PATH
#    on the reference workstation, and 1.22 silently does not understand
#    ValidatingAdmissionPolicy -- it would report success having applied
#    nothing. The binary is resolved explicitly, and its version is checked.
#
# 2. The right cluster. This kubeconfig reaches clusters that are not ours:
#    `agents-staging` (a live GKE Standard cluster owned by another team in this
#    same shared project), `agents-prod`, and an EKS cluster. Applying a
#    default-deny NetworkPolicy into the wrong one is a full outage for whoever
#    owns it. Three independent refusals, because each catches what the others
#    cannot:
#      * the context must not name anything on SHARED_DENY_LIST, which lives
#        once in scripts/lib/common.sh and is sourced rather than restated;
#      * the context must not look like an EKS one. The EKS cluster's NAME
#        appears nowhere in this repository and inventing a placeholder for it
#        was worse than useless -- a literal `eks-cluster-name` matches no real
#        context, so the entry read as a handled case while handling nothing.
#        The shape is what is checkable without the name: an EKS context is its
#        cluster ARN, or a host under `eks.amazonaws.com`;
#      * the cluster must start with `swarm-`, the same rule terraform enforces
#        on `gke_autopilot.cluster_name`, and the context must name it.
#
# 3. Dry run first, and offline by default. Without --confirm nothing is sent as
#    a write. Validation is client-side unless --server-dry-run is passed,
#    because a server dry run of a first-time tenant fails anyway (the namespace
#    it would create does not exist yet) and because the safest request to make
#    against a cluster you are not sure about is none.
#
# And one property of what is rendered: the tenant egress policy carries the
# cluster's OWN network -- pod range, service range, kube-dns Service IP,
# NodeLocal DNSCache address -- read from the cluster this script just checked
# (kubernetes/cluster-network.sh), never typed and never taken from the command
# line. Afterwards, scripts/lib/check-cluster-network-parity.sh compares every
# applied copy with the live cluster.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "${HERE}/.." && pwd)"

# The deny-list of other teams' resources lives ONCE, in scripts/lib/common.sh
# (CLAUDE.md rule 2). Restating it here is how the two copies drift and how a
# resource added to one list stays unprotected in the other. common.sh only
# defines functions and arrays when sourced -- it reads no .env and contacts
# nothing -- so sourcing it costs nothing and buys `kubectl_bin` as well.
COMMON="${REPO}/scripts/lib/common.sh"
[[ -f "${COMMON}" ]] || { printf 'error: %s is missing; the shared deny-list lives there\n' "${COMMON}" >&2; exit 1; }
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../scripts/lib/common.sh
source "${COMMON}"

# The swarm's own cluster, as `scripts/lib/common.sh` and terraform name it.
CLUSTER="${GKE_CLUSTER:-swarm-autopilot}"
TENANT=""
MODE="tenant"
CONFIRM=0
SERVER_DRY_RUN=0
CONTEXT=""
RENDER_ARGS=()

# THE CLUSTER'S NETWORK IS READ, NOT PASSED. These five render.py flags used to
# be forwarded like everything else, and render.py defaulted them, and nothing
# ever passed the real ones: the 2026-09-24 policy was rendered for pods
# 10.0.0.0/8 and services 34.118.224.0/20 on a cluster that uses neither, with
# no DNS rule this cluster's DNS could match. A value typed here is the value
# that goes stale when the cluster changes, so it is refused, and the value this
# script reads from the cluster is the only one rendered.
NETWORK_FLAGS=(--pod-cidr --service-cidr --cluster-dns-ip --node-local-dns-ip --network-source)

# network_flag_for ARG -- print the network flag ARG spells, and succeed, when
# ARG (the part before any `=`) is one of NETWORK_FLAGS or ANY PREFIX of one.
#
# Prefixes, not just the full names, because that is what the renderer would
# have accepted: argparse expands an unambiguous prefix of a long option, and
# the arguments forwarded below come AFTER the four values this script read, so
# the last occurrence won and `--pod-cid 10.200.0.0/14` replaced the cluster's
# pod range. render.py now refuses abbreviations itself (allow_abbrev=False);
# this refusal stays because it names the reason, and because it does not
# depend on every future caller of render.py remembering to be as strict.
#
# Only for what is forwarded. apply.sh's own `--cluster` is a prefix of
# `--cluster-dns-ip`, which is why this is not applied to every argument.
network_flag_for() {
  local name="${1%%=*}" flag
  [[ "${name}" == --?* ]] || return 1
  for flag in "${NETWORK_FLAGS[@]}"; do
    case "${flag}" in
      "${name}"*) printf '%s' "${flag}"; return 0 ;;
    esac
  done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant|-t)  TENANT="$2"; shift 2 ;;
    --policies)   MODE="policies"; shift ;;
    --confirm|-y) CONFIRM=1; shift ;;
    --server-dry-run) SERVER_DRY_RUN=1; shift ;;
    --context)    CONTEXT="$2"; shift 2 ;;
    --cluster)    CLUSTER="$2"; shift 2 ;;
    # The `=` spellings of the three above. Without them `--cluster=NAME` was
    # forwarded, and the renderer read `--cluster` as an abbreviation of
    # `--cluster-dns-ip` and tried to render the cluster's NAME as its DNS IP.
    --tenant=*)   TENANT="${1#*=}"; shift ;;
    --context=*)  CONTEXT="${1#*=}"; shift ;;
    --cluster=*)  CLUSTER="${1#*=}"; shift ;;
    -h|--help)    sed -n '2,43p' "$0"; exit 0 ;;
    # Everything else is passed straight to render.py, so --pss-enforce,
    # --quota-cpu and friends work without being restated here -- except the
    # network, in any spelling that could reach it.
    *)
      if NETWORK_FLAG="$(network_flag_for "$1")"; then
        die "'${1%%=*}' would reach the renderer as ${NETWORK_FLAG}, which is read from the cluster
       by this script (kubernetes/cluster-network.sh); it cannot be passed, in full or
       abbreviated. To render for another cluster, point --context/--cluster at it."
      fi
      RENDER_ARGS+=("$1"); shift ;;
  esac
done

# --- 1. the right kubectl ----------------------------------------------------
# `kubectl_bin` from common.sh, not a second copy of the same search: it already
# knows that 1.22 (EKS) and 1.25 (Docker Desktop) win $PATH on this machine and
# that a 1.22 client silently DROPS fields it does not understand -- including
# the whole of ValidatingAdmissionPolicy -- while reporting success.
KUBECTL="$(kubectl_bin)"
info "kubectl  ${KUBECTL}"

# --- 2. the right cluster ----------------------------------------------------
KUBECTL_ARGS=()
if [[ -n "${CONTEXT}" ]]; then
  KUBECTL_ARGS+=(--context "${CONTEXT}")
  CURRENT="${CONTEXT}"
else
  CURRENT="$("${KUBECTL}" config current-context 2>/dev/null || true)"
fi
[[ -n "${CURRENT}" ]] || die "no kubectl context is selected; run scripts/configure-kubectl.sh"

for foreign in "${SHARED_DENY_LIST[@]}"; do
  # Service accounts and the project's `default` VPC are on the shared list but
  # can never name a kube context, and substring-matching `default` would refuse
  # legitimate contexts that merely contain the word. Cluster-shaped entries
  # only.
  case "${foreign}" in
    *@*|default) continue ;;
  esac
  case "${CURRENT}" in
    *"${foreign}"*)
      die "context '${CURRENT}' names '${foreign}', which belongs to another team. Refusing." ;;
  esac
done

# EKS by SHAPE, because its name is not in this repository. A kubeconfig entry
# for EKS is the cluster ARN, or a host under eks.amazonaws.com.
case "${CURRENT}" in
  arn:aws:eks:*|*.eks.amazonaws.com*|*eks.amazonaws.com*)
    die "context '${CURRENT}' looks like an EKS cluster. This script applies GKE
       tenant isolation objects; refusing." ;;
esac
case "${CLUSTER}" in
  swarm-*) ;;
  *) die "cluster '${CLUSTER}' does not start with 'swarm-'; the swarm's Autopilot
       cluster does, and terraform refuses to manage any cluster that does not." ;;
esac
# THE CONTEXT'S CLUSTER, NOT THE CONTEXT'S NAME.
#
# This used to match the context LABEL against the cluster name, which meant
# the repository's own setup script produced a kubeconfig this script refused:
# scripts/configure-kubectl.sh:132 deliberately renames the generated context
# `gke_<project>_<region>_swarm-autopilot` to the friendlier `swarm-${ENV}`, so
# the label is `swarm-dev` and the comparison failed every time. The documented
# path -- "Run scripts/configure-kubectl.sh" -- produced the exact state the
# error told you to fix by running it.
#
# That is not a cosmetic bug. Tenant namespaces are created by this script, and
# with it refusing, `scripts/register-tenant.sh` takes its "not connected to the
# swarm cluster" branch, logs an INFO, skips the whole GKE half and still
# reports the tenant registered. On 2026-09-23 `swarm-tenant-eng` did not exist
# on the cluster at all and every `browser` task the platform had ever accepted
# -- seven of them -- had failed.
#
# A label is a nickname anyone may change; the cluster a context points AT is
# the thing that decides which API server is about to be written to. Resolve it
# and compare that, so the guard keeps refusing another team's cluster while
# accepting any honest name for our own.
CURRENT_CLUSTER="$("${KUBECTL}" config view -o \
  "jsonpath={.contexts[?(@.name==\"${CURRENT}\")].context.cluster}" 2>/dev/null || true)"
if [[ -z "${CURRENT_CLUSTER}" ]]; then
  die "could not resolve which cluster context '${CURRENT}' points at; refusing to apply.
       An unresolvable context is not a safe one -- it is one nothing checked."
fi
case "${CURRENT_CLUSTER}" in
  *"${CLUSTER}"*) ;;
  *) die "context '${CURRENT}' points at cluster '${CURRENT_CLUSTER}', not '${CLUSTER}'; refusing to apply.
       Run scripts/configure-kubectl.sh, or pass --context/--cluster deliberately." ;;
esac
info "context  ${CURRENT} -> ${CURRENT_CLUSTER}"

# --- render ------------------------------------------------------------------
PYTHON="${PYTHON_BIN:-python3}"
if [[ "${MODE}" == "policies" ]]; then
  MANIFEST="$("${PYTHON}" "${HERE}/render.py" policies)"
  info "rendering cluster-scoped admission policies"
else
  [[ -n "${TENANT}" ]] || die "--tenant is required (or pass --policies)"

  # THE CONTROL PLANE'S NUMERIC IDENTITIES, RESOLVED HERE BECAUSE THEY CANNOT BE
  # DERIVED.
  #
  # GKE resolves a Google service account reaching the Kubernetes API with an
  # OAuth access token to its numeric uniqueId, not its email -- and that is how
  # the scheduler reaches it. A RoleBinding naming only the email applies
  # cleanly and authorises nobody, which is the failure that made every GKE
  # dispatch this platform ever attempted return
  #
  #     jobs.batch is forbidden: User "117405034245659033603" cannot create ...
  #
  # for eight months of browser tasks. render.py takes the uniqueIds as flags
  # and falls back to the email when they are absent, so a resolution failure
  # here would silently re-create that exact bug. It is therefore FATAL rather
  # than skipped: an apply that cannot name the identities it is authorising has
  # nothing useful to do.
  #
  # Resolved by uniqueId lookup rather than read from terraform state, because
  # this script must work against a cluster whose state file it cannot read.
  uid_of() {
    local account="$1" uid
    uid="$(gcloud iam service-accounts describe "${account}" \
      --project="${PROJECT_ID}" --format='value(uniqueId)' 2>/dev/null || true)"
    [[ "${uid}" =~ ^[0-9]{15,25}$ ]] \
      || die "could not resolve the uniqueId of ${account} (got '${uid}').
       Without it the RoleBinding authorises nobody -- see kubernetes/rbac/dispatcher-rbac.yaml."
    printf '%s' "${uid}"
  }
  SCHEDULER_UID="$(uid_of "swarm-scheduler@${PROJECT_ID}.iam.gserviceaccount.com")"
  RECONCILER_UID="$(uid_of "swarm-reconciler@${PROJECT_ID}.iam.gserviceaccount.com")"
  info "rbac subjects  scheduler=${SCHEDULER_UID} reconciler=${RECONCILER_UID}"

  # THE CLUSTER'S NETWORK, READ FROM THE CLUSTER THE CHECKS ABOVE APPROVED.
  #
  # The egress policy needs four values that belong to the cluster: the pod and
  # service ranges it carves out of the internet, and the two addresses a pod's
  # DNS actually goes to on a Cloud DNS + NodeLocal DNSCache cluster -- the
  # node-local address and the kube-dns Service IP. cluster-network.sh says
  # which field or object each one is read from. Called directly, NOT inside
  # $(...): it sets its results as variables, and a command substitution would
  # run it in a subshell and throw them away.
  #
  # FATAL, like the uniqueIds above: rendering without them is what produced
  # the 2026-09-24 policy, and there is no default that is right for a cluster
  # this script has not read.
  # shellcheck source-path=SCRIPTDIR
  # shellcheck source=cluster-network.sh
  source "${HERE}/cluster-network.sh"
  resolve_cluster_network "${KUBECTL}" "${CLUSTER}" "${GKE_LOCATION}" "${PROJECT_ID}" \
    ${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"} \
    || die "could not read ${CLUSTER}'s network (above); nothing was rendered or applied.
       The egress policy is rendered from the live cluster or not at all."
  info "network  pods=${CLUSTER_POD_CIDR} services=${CLUSTER_SERVICE_CIDR}"
  info "dns      node-local=${CLUSTER_NODE_LOCAL_DNS_IP} kube-dns=${CLUSTER_DNS_IP}  (${CLUSTER_NETWORK_SOURCE})"

  MANIFEST="$("${PYTHON}" "${HERE}/render.py" tenant --tenant "${TENANT}" \
    --scheduler-uid "${SCHEDULER_UID}" \
    --reconciler-uid "${RECONCILER_UID}" \
    --pod-cidr "${CLUSTER_POD_CIDR}" \
    --service-cidr "${CLUSTER_SERVICE_CIDR}" \
    --cluster-dns-ip "${CLUSTER_DNS_IP}" \
    --node-local-dns-ip "${CLUSTER_NODE_LOCAL_DNS_IP}" \
    --network-source "${CLUSTER_NETWORK_SOURCE}" \
    "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}")"
  info "rendering tenant ${TENANT}"

  # Belt and braces on the property above: a render marked offline carries
  # documentation addresses, and must never reach a cluster. Matched on the
  # annotation itself -- the templates' comments are rendered too, and they
  # name the marker.
  case "${MANIFEST}" in
    *"swarm.saga.xyz/network-source: offline-render-not-for-apply"*)
      die "the render is marked offline-render-not-for-apply -- it carries no real
       cluster network. Refusing; nothing was applied." ;;
  esac
fi

# --- 3. dry run, then apply --------------------------------------------------
# Client-side first, always: it needs no cluster and catches a malformed render.
printf '%s\n' "${MANIFEST}" | "${KUBECTL}" apply --dry-run=client --validate=false -f - >/dev/null \
  || die "the rendered manifest is not valid YAML; nothing was applied"

if [[ "${SERVER_DRY_RUN}" -eq 1 ]]; then
  # Only useful once the namespace exists: a server dry run cannot see objects
  # inside a namespace the same manifest is still proposing to create.
  printf '%s\n' "${MANIFEST}" | "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" \
    apply --dry-run=server -f - \
    || die "server-side validation failed; nothing was applied"
fi

if [[ "${CONFIRM}" -ne 1 ]]; then
  printf '%s\n' "${MANIFEST}" | "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" \
    diff -f - || true      # `diff` exits 1 when there IS a difference
  ok "dry run only. Re-run with --confirm to apply."
  exit 0
fi

printf '%s\n' "${MANIFEST}" | "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" apply -f -
ok "applied"
if [[ "${MODE}" == "tenant" ]]; then
  # Not run from here: it checks EVERY tenant namespace, and a stale neighbour
  # must not turn this tenant's successful apply into a failure that
  # register-tenant.sh would then report as "not isolated".
  dim "  compare every applied egress policy with the live cluster:"
  dim "    scripts/lib/check-cluster-network-parity.sh --require-live${CONTEXT:+ --context ${CONTEXT}}"
fi
