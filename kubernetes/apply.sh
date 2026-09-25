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
#      * the CLUSTER the context names -- in the context label, in the cluster
#        entry it resolves to, and in --cluster -- must not be on
#        SHARED_DENY_LIST, which lives once in scripts/lib/common.sh and is
#        sourced rather than restated. The cluster, not the whole string: our
#        project id contains the other team's cluster name;
#      * the context must not look like an EKS one. The EKS cluster's NAME
#        appears nowhere in this repository and inventing a placeholder for it
#        was worse than useless -- a literal `eks-cluster-name` matches no real
#        context, so the entry read as a handled case while handling nothing.
#        The shape is what is checkable without the name: an EKS context is its
#        cluster ARN, or a host under `eks.amazonaws.com`;
#      * the cluster must start with `swarm-`, the same rule terraform enforces
#        on `gke_autopilot.cluster_name`, and the context must point at exactly
#        it -- the kubeconfig cluster entry must be gcloud's name for that
#        cluster in ${PROJECT_ID} at ${GKE_LOCATION}, the one whose network this
#        script reads.
#
# 3. Dry run first. Without --confirm nothing is written. The preview is two
#    server dry runs of the one render, and prints both, because each shows
#    what the other cannot:
#      * `kubectl diff` -- WHICH FIELDS change;
#      * `kubectl apply --dry-run=server` -- the verdict --confirm will print for
#        each object (created / configured / unchanged). kubectl diff builds its
#        patch without kubectl's last-applied annotation and apply builds it
#        with it, so an object whose only change is that annotation has no hunk
#        in the diff and still applies as "configured". On 2026-09-25 that was
#        the Role swarm-worker, and a preview of the diff alone did not show it.
#    A server dry run that fails is reported, not fatal: a first-time tenant's
#    namespace does not exist yet, so nothing inside it can be dry-run on the
#    server. --server-dry-run makes it fatal, and under --confirm runs it before
#    the apply.
#
# And two properties of what is rendered. The tenant egress policy carries the
# cluster's OWN network -- pod range, service range, kube-dns Service IP,
# NodeLocal DNSCache address -- read from the cluster this script just checked
# (kubernetes/cluster-network.sh), never typed and never taken from the command
# line. Afterwards, scripts/lib/check-cluster-network-parity.sh compares every
# applied copy with the live cluster. Likewise the Kubernetes service accounts
# the tenant GSA binds for Workload Identity are read from the GSA's IAM policy
# and passed as --bound-ksa: the older `swarm-worker` account is rendered only
# where it is bound (kubernetes/render.py LEGACY_KSA_NAME).
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

# THE WORKLOAD IDENTITY BINDINGS ARE READ, NOT PASSED, for the same reason.
# `--bound-ksa` tells render.py which KSAs the tenant GSA binds, and that
# decides whether the older `swarm-worker` account is rendered. Typed by hand it
# would render an account as bound where IAM binds nothing -- the exact state
# (an annotation claiming an identity IAM does not grant) it exists to remove.
IDENTITY_FLAGS=(--bound-ksa)

# derived_flag_for ARG FLAG... -- print the FLAG that ARG spells, and succeed,
# when ARG (the part before any `=`) is one of the FLAGs or ANY PREFIX of one.
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
derived_flag_for() {
  local name="${1%%=*}" flag
  shift
  [[ "${name}" == --?* ]] || return 1
  for flag in "$@"; do
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
    # The header above `set -euo pipefail`, however long it grows; a fixed line
    # range stopped mid-sentence the first time the header changed.
    -h|--help)    awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0"; exit 0 ;;
    # Everything else is passed straight to render.py, so --pss-enforce,
    # --quota-cpu and friends work without being restated here -- except the
    # network, in any spelling that could reach it.
    *)
      if NETWORK_FLAG="$(derived_flag_for "$1" "${NETWORK_FLAGS[@]}")"; then
        die "'${1%%=*}' would reach the renderer as ${NETWORK_FLAG}, which is read from the cluster
       by this script (kubernetes/cluster-network.sh); it cannot be passed, in full or
       abbreviated. To render for another cluster, point --context/--cluster at it."
      fi
      if IDENTITY_FLAG="$(derived_flag_for "$1" "${IDENTITY_FLAGS[@]}")"; then
        die "'${1%%=*}' would reach the renderer as ${IDENTITY_FLAG}, which this script reads from
       the tenant GSA's IAM policy (its roles/iam.workloadIdentityUser members); it cannot
       be passed, in full or abbreviated. Bind the KSA in IAM and re-run."
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

# cluster_of REF -- the CLUSTER a context or kubeconfig cluster entry names.
#
# gcloud names both `gke_<project>_<location>_<cluster>`. None of the three
# segments can contain `_` (GCP project ids, locations and GKE cluster names are
# lowercase letters, digits and hyphens), so the shape is unambiguous and the
# cluster is the last segment. Anything else -- `swarm-dev`, a hand-renamed
# entry -- is judged whole, because there is no way to tell which part of it is
# the cluster.
cluster_of() {
  local ref="$1"
  local gke_ref='^gke_[a-z][a-z0-9-]*_[a-z0-9-]+_([a-z0-9-]+)$'
  if [[ "${ref}" =~ ${gke_ref} ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  else
    printf '%s' "${ref}"
  fi
}

# refuse_foreign WHAT REF -- die when the cluster REF names is on the shared
# deny-list. WHAT is how the refusal describes where REF came from.
#
# THE CLUSTER, NOT THE WHOLE STRING. This used to be `*"${foreign}"*` over the
# whole context name, which refused gcloud's own name for OUR cluster:
# gke_saga-agents-staging_us-central1_swarm-autopilot contains `agents-staging`
# -- the other team's cluster -- because our PROJECT id does. So the default
# context `gcloud container clusters get-credentials` writes was refused, and
# scripts/register-tenant.sh, whose own guard (common.sh kube_context_allowed)
# accepts that name and hands it to this script as --context, would then die
# with "the tenant namespace is not isolated" (a consequence read from the two
# scripts, not a run observed). Only the cluster segment says whose cluster
# it is; the project is shared by definition. The other team's context,
# gke_saga-agents-staging_us-central1-a_agents-staging, still ends in
# `agents-staging` and is still refused -- as is our label pointing at it,
# because the resolved cluster entry is judged too.
#
# Still a substring match WITHIN the cluster name, so `agents-staging-2` is
# refused as well. Service accounts and the project's `default` VPC are on the
# shared list but can never name a cluster, and `default` would match too much;
# cluster-shaped entries only.
refuse_foreign() {
  local what="$1" ref="$2" cluster foreign
  cluster="$(cluster_of "${ref}")"
  for foreign in "${SHARED_DENY_LIST[@]}"; do
    case "${foreign}" in
      *@*|default) continue ;;
    esac
    case "${cluster}" in
      *"${foreign}"*)
        die "${what} names cluster '${cluster}', which matches '${foreign}' on the shared
       deny-list: it belongs to another team. Refusing." ;;
    esac
  done
}

refuse_foreign "context '${CURRENT}'" "${CURRENT}"
refuse_foreign "--cluster '${CLUSTER}'" "${CLUSTER}"

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
# The label is a nickname; this is what gets written to. A `swarm-dev` label
# pointing at the other team's cluster is refused here, by the deny-list, with
# the deny-list's reason.
refuse_foreign "context '${CURRENT}' -> kubeconfig cluster '${CURRENT_CLUSTER}'" "${CURRENT_CLUSTER}"
# EXACTLY THE CLUSTER WHOSE NETWORK IS READ: ${CLUSTER} in ${PROJECT_ID} at
# ${GKE_LOCATION}. Everything below reads that cluster by those three names --
# `gcloud container clusters describe ${CLUSTER} --project ${PROJECT_ID}
# --location ${GKE_LOCATION}` for the egress policy's network, ${PROJECT_ID}'s
# pool for Workload Identity -- so the cluster entry the context resolves to
# must be gcloud's name for exactly that one. That is the name
# `gcloud container clusters get-credentials` writes; scripts/configure-kubectl.sh
# renames only the CONTEXT (to swarm-${ENVIRONMENT}) and keeps it, and
# common.sh's kube_context_allowed accepts the same string as a context name.
#
# Two narrower versions of this check each let a wrong cluster through:
#   * `*"${CLUSTER}"*`, a substring, passed `swarm-autopilot-old`;
#   * comparing the cluster SEGMENT alone passed our cluster's name in another
#     project or location -- gke_other-project_us-central1_swarm-autopilot, or
#     ..._us-central1-a_swarm-autopilot beside the other team's zonal cluster.
# Either way the policy is rendered from one cluster's network and written to
# another: the RC3 shape, from the guard's side. An entry not in gcloud's shape
# names no project or location at all, so nothing shows it is ours; it is
# refused too, with the name that would be accepted.
EXPECTED_CLUSTER="gke_${PROJECT_ID}_${GKE_LOCATION}_${CLUSTER}"
if [[ "${CURRENT_CLUSTER}" != "${EXPECTED_CLUSTER}" ]]; then
  die "context '${CURRENT}' points at kubeconfig cluster '${CURRENT_CLUSTER}', not '${EXPECTED_CLUSTER}';
       refusing to apply. This script reads the network of cluster ${CLUSTER} in project
       ${PROJECT_ID} at ${GKE_LOCATION}, and writes only to that cluster. Run
       scripts/configure-kubectl.sh, or pass --context/--cluster deliberately."
fi
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

  # WHICH KSAs THE TENANT GSA BINDS, READ FROM ITS IAM POLICY.
  #
  # render.py renders the older `swarm-worker` account only where it is bound
  # (LEGACY_KSA_NAME). It used to be rendered everywhere, and on eng -- a
  # terraform tenant -- it was bound nowhere: measured 2026-09-25, the only
  # roles/iam.workloadIdentityUser member on swarm-agent-worker-eng@ is
  # `[swarm-tenant-eng/swarm-agent-worker]`. Only IAM knows, so IAM is read.
  #
  # The namespace, GSA and KSA come from the renderer (`render.py identity`,
  # with the same forwarded arguments -- --gsa from register-tenant.sh
  # included), so the policy read is the policy of the GSA the render
  # annotates, not of a name derived a second time here.
  #
  # A member counts only if it is this project's pool AND this namespace: the
  # namespace is part of the principal, so a binding for
  # `[swarm-tenant-other/swarm-worker]` binds nothing here.
  #
  # An unreadable policy is FATAL, like the uniqueIds: guessing "nothing bound"
  # would drop a bound account's RoleBinding subject. A GSA that does not exist
  # yet (register-tenant.sh --dry-run previews a new tenant whose GSA it has not
  # created) binds nothing, and says so.
  IDENTITY="$("${PYTHON}" "${HERE}/render.py" identity --tenant "${TENANT}" \
    "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}")" \
    || die "could not resolve tenant ${TENANT}'s namespace and service account (above);
       nothing was rendered or applied."
  read -r TENANT_NAMESPACE TENANT_GSA TENANT_KSA <<<"${IDENTITY}"
  [[ -n "${TENANT_NAMESPACE}" && -n "${TENANT_GSA}" && -n "${TENANT_KSA}" ]] \
    || die "render.py identity printed '${IDENTITY}', not '<namespace> <gsa> <ksa>'"

  WI_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-wi-bindings.XXXXXX")"
  BOUND_KSA_ARGS=()
  BOUND_KSAS=""
  if gcloud iam service-accounts get-iam-policy "${TENANT_GSA}" \
      --project "${PROJECT_ID}" --format=json \
      >"${WI_WORK}/policy.json" 2>"${WI_WORK}/policy.err"; then
    # To a file, then read in THIS shell: a pipe into `while` would run the loop
    # in a subshell and lose BOUND_KSA_ARGS (CLAUDE.md, the command-substitution
    # trap's sibling).
    if ! jq -r --arg prefix "serviceAccount:${PROJECT_ID}.svc.id.goog[${TENANT_NAMESPACE}/" '
        [ .bindings[]? | select(.role == "roles/iam.workloadIdentityUser") | .members[]?
          | select(startswith($prefix) and endswith("]"))
          | ltrimstr($prefix) | rtrimstr("]") ]
        | unique | .[]' "${WI_WORK}/policy.json" >"${WI_WORK}/bound" 2>"${WI_WORK}/jq.err"; then
      err "could not read ${TENANT_GSA}'s IAM policy as JSON:"
      sed -n '1,3p' "${WI_WORK}/jq.err" | sed 's/^/     /' >&2
      rm -rf "${WI_WORK}"
      die "nothing was rendered or applied."
    fi
    while IFS= read -r ksa; do
      [[ -n "${ksa}" ]] || continue
      BOUND_KSA_ARGS+=(--bound-ksa "${ksa}")
      BOUND_KSAS="${BOUND_KSAS:+${BOUND_KSAS} }${ksa}"
    done <"${WI_WORK}/bound"
  else
    WI_ERR="$(redact <"${WI_WORK}/policy.err")"
    rm -rf "${WI_WORK}"
    die_if_auth_failure "${WI_ERR}"
    case "${WI_ERR}" in
      *NOT_FOUND*)
        warn "${TENANT_GSA} does not exist yet, so no KSA is bound to it; rendering without"
        dim "  the older swarm-worker account. Re-run once the GSA exists (register-tenant.sh creates it)." ;;
      *)
        err "could not read the IAM policy of ${TENANT_GSA}:"
        printf '%s\n' "${WI_ERR}" | sed -n '1,5p' | sed 's/^/     /' >&2
        die "without it this script cannot tell which KSAs are Workload Identity-bound, and
       renders nothing rather than guess. Needs iam.serviceAccounts.getIamPolicy on that account." ;;
    esac
  fi
  rm -rf "${WI_WORK}"
  info "workload identity  ${TENANT_GSA} binds in ${TENANT_NAMESPACE}: ${BOUND_KSAS:-nothing}"
  # The same read answers whether the pods can authenticate at all. The
  # dispatcher runs them as ${TENANT_KSA}; unbound, every one starts and 403s on
  # its first Google API call, which reads as a permissions bug in the worker.
  case " ${BOUND_KSAS} " in
    *" ${TENANT_KSA} "*) ;;
    *) warn "${TENANT_KSA} -- the account the dispatcher runs worker pods as -- has no Workload Identity binding
       on ${TENANT_GSA}; those pods will authenticate as nothing. terraform/modules/tenancy (or
       scripts/register-tenant.sh) issues it." ;;
  esac

  MANIFEST="$("${PYTHON}" "${HERE}/render.py" tenant --tenant "${TENANT}" \
    --scheduler-uid "${SCHEDULER_UID}" \
    --reconciler-uid "${RECONCILER_UID}" \
    --pod-cidr "${CLUSTER_POD_CIDR}" \
    --service-cidr "${CLUSTER_SERVICE_CIDR}" \
    --cluster-dns-ip "${CLUSTER_DNS_IP}" \
    --node-local-dns-ip "${CLUSTER_NODE_LOCAL_DNS_IP}" \
    --network-source "${CLUSTER_NETWORK_SOURCE}" \
    ${BOUND_KSA_ARGS[@]+"${BOUND_KSA_ARGS[@]}"} \
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

# preview_verdicts -- print, object by object, what `--confirm` will report,
# from `kubectl apply --dry-run=server` of the same render through the same
# context. Nothing is written.
#
# WHY THIS AND NOT THE DIFF ALONE. On 2026-09-25 `--confirm` printed
# `role.rbac.authorization.k8s.io/swarm-worker configured` after a preview whose
# diff listed only two NetworkPolicies. kubectl diff builds its patch WITHOUT
# kubectl's last-applied-configuration annotation; apply builds it WITH it. So
# an object whose only change is that annotation -- or whose patch the server
# normalises away, as it did the Role's `rules: []` -- has no hunk in the diff
# and applies as "configured". This asks kubectl the question apply answers,
# with apply's own patch, rather than restating when kubectl says "configured".
#
# To files, so the verdicts (stdout) and what failed (stderr) stay apart and the
# exit status is kept. A failure is not fatal here unless --server-dry-run was
# passed: kubectl still prints a verdict for every object it could judge, and
# for a first-time tenant it cannot judge anything inside the namespace.
preview_verdicts() {
  local work status=0 configured created unchanged
  work="$(mktemp -d "${TMPDIR:-/tmp}/swarm-apply-preview.XXXXXX")"
  printf '%s\n' "${MANIFEST}" | "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" \
    apply --dry-run=server -f - >"${work}/out" 2>"${work}/err" || status=$?
  info "what --confirm will report, object by object (kubectl apply --dry-run=server; nothing written):"
  redact <"${work}/out"
  # grep -c prints 0 and exits 1 when nothing matches; the count is what is kept.
  configured="$(grep -c ' configured (server dry run)$' "${work}/out" || true)"
  created="$(grep -c ' created (server dry run)$' "${work}/out" || true)"
  unchanged="$(grep -c ' unchanged (server dry run)$' "${work}/out" || true)"
  if [[ "${status}" -ne 0 ]]; then
    err "the server could not dry-run every object; kubectl said (exit ${status}):"
    redact <"${work}/err" | sed -n '1,20p' | sed 's/^/     /' >&2
    rm -rf "${work}"
    if [[ "${SERVER_DRY_RUN}" -eq 1 ]]; then
      die "server-side validation failed (--server-dry-run); nothing was applied"
    fi
    warn "the objects kubectl names there are not in the list above: nothing about them was judged."
    dim "  For a first-time tenant that is expected -- its namespace does not exist yet, so nothing inside"
    dim "  it can be dry-run on the server, and --confirm creates the namespace first. Any other error is"
    dim "  one --confirm is likely to meet as well. --server-dry-run makes this fatal."
  else
    rm -rf "${work}"
  fi
  if [[ $((configured + created + unchanged)) -eq 0 ]]; then
    warn "the server dry run reported no object at all -- that is nothing judged, not nothing changed;"
    dim "  the diff above is the only preview this run has."
    return 0
  fi
  info "--confirm will report ${configured} configured, ${created} created, ${unchanged} unchanged"
  if [[ "${configured}" -gt 0 ]]; then
    dim "  An object listed as configured with no hunk in the diff above changes nothing the diff"
    dim "  compares: only kubectl's last-applied-configuration annotation (which kubectl diff leaves"
    dim "  out of its patch and apply writes), or a patch the server normalises away."
  fi
}

if [[ "${CONFIRM}" -ne 1 ]]; then
  printf '%s\n' "${MANIFEST}" | "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" \
    diff -f - || true      # `diff` exits 1 when there IS a difference
  preview_verdicts
  ok "dry run only. Re-run with --confirm to apply."
  exit 0
fi

if [[ "${SERVER_DRY_RUN}" -eq 1 ]]; then
  # Only useful once the namespace exists: a server dry run cannot see objects
  # inside a namespace the same manifest is still proposing to create.
  printf '%s\n' "${MANIFEST}" | "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" \
    apply --dry-run=server -f - \
    || die "server-side validation failed; nothing was applied"
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
