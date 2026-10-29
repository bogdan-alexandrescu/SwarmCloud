#!/usr/bin/env bash
# Render and apply the tenant manifests in this directory.
#
#   kubernetes/apply.sh --tenant eng                 # dry run, prints a diff
#   kubernetes/apply.sh --tenant eng --confirm       # actually applies
#   kubernetes/apply.sh --tenant research --workspace-id w-1a2b3c --confirm
#                         # a group or service workspace's namespace, as the
#                         # workspace job's A7 applies it: the Namespace carries
#                         # swarm.saga.xyz/workspace, which the deployer's scope
#                         # policy admits it by (docs/design/workspace-kinds.md)
#   kubernetes/apply.sh --policies --confirm         # cluster-scoped policies;
#                         # refuses this environment's Terraform tenants'
#                         # namespaces to the deployer, read from its tfvars
#   kubernetes/apply.sh --swarm-system --confirm     # the live relay, then
#                         # swarm-system's ingress: the relay's manifest from
#                         # `terraform output -raw swarm_live_manifest` (which
#                         # creates the namespace), then its TCP 8080 from
#                         # labelled tenant pods and swarm-api's Direct VPC
#                         # egress range, read from the service
#                         # (docs/design/live-browser.md §3.3, §8.3), then
#                         # swarm-egress from `swarm_egress_manifest` when
#                         # terraform renders one (docs/browser-egress.md),
#                         # then the decision-api tailnet egress from
#                         # `tailscale_egress_manifest` when terraform renders
#                         # one, and after the apply copies its Tailscale
#                         # OAuth secret from Secret Manager into
#                         # swarm-system/operator-oauth, never printing it
#                         # (docs/runbooks/tailscale-egress.md)
#   kubernetes/apply.sh --swarm-graph --confirm      # the shared graph service:
#                         # `terraform output -raw swarm_graph_manifest`
#                         # (terraform/infra/graph_service.tf), alone; null
#                         # until graph_service.serve, and then nothing is
#                         # applied (docs/design/graph-service.md §2.1, §8.2).
#                         # scripts/roll-graph.sh drives it and waits for the
#                         # rollout. After applying, swarm-api's egress range
#                         # is read from its service and swarm-graph-ingress
#                         # is compared with it (network_parity.py
#                         # --graph-ingress); a difference fails the apply
#   kubernetes/apply.sh --swarm-typesafe-fwd --confirm   # the TypeSafe forwarder:
#                         # `terraform output -raw swarm_typesafe_fwd_manifest`
#                         # (terraform/infra/typesafe_fwd.tf), alone; null
#                         # until typesafe_forwarder.serve, and then nothing
#                         # is applied (docs/design/workspace-secrets.md §3.5).
#                         # scripts/roll-typesafe-fwd.sh drives it and waits
#                         # for the rollout
#   kubernetes/apply.sh --tenant eng --spec-verify-keys keys.json --confirm
#                         # ... with the step-spec public keys ConfigMap, from
#                         # `terraform output -json spec_verify_keys_configmap`
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
# line. Afterwards, scripts/lib/check-cluster-network-parity.sh compares the
# applied copy with the live cluster: a tenant `--confirm` RUNS it, with
# --require-live, the same context and --cluster, scoped to the namespace just
# applied, and exits non-zero when it fails -- the apply happened, but the
# tenant's egress does not match the cluster's network (#76). Scoped, so a stale
# NEIGHBOUR cannot fail this tenant's apply; the whole-cluster sweep is printed.
# A dry run only prints the command. Likewise the Kubernetes service accounts
# the tenant GSA binds for Workload Identity are read from the GSA's IAM policy
# and passed as --bound-ksa: the older `swarm-worker` account is rendered only
# where it is bound (kubernetes/render.py LEGACY_KSA_NAME).
#
# --swarm-system follows the same rule for its one cluster value. The relay's
# ingress admits swarm-api's Direct VPC egress range, and that range is READ:
# the subnetwork swarm-api's own service names (`gcloud run services describe`,
# the run.googleapis.com/network-interfaces annotation), then that subnetwork's
# ipCidrRange. Never typed, never forwarded. Before those policies it applies
# the relay itself -- `terraform output -raw swarm_live_manifest`
# (terraform/infra/live.tf), which creates the swarm-system namespace the
# policies go into -- in the same apply, first. --confirm refuses when there is
# no manifest to apply (enable_live_browser off), when it carries an object
# outside swarm-system, an RBAC object or an unrendered placeholder, and when
# the range terraform rendered into the relay's own NetworkPolicy is not the
# range read from swarm-api. Afterwards it compares the applied policies with
# that range (kubernetes/network_parity.py --live-ingress), exiting non-zero
# on a difference. The relay is applied through the same context checks as
# everything here: never to `agents-staging`.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "${HERE}/.." && pwd)"

# The deny-list of other teams' resources lives ONCE, in scripts/lib/common.sh
# (CLAUDE.md rule 2). Restating it here is how the two copies drift and how a
# resource added to one list stays unprotected in the other. common.sh only
# defines functions and arrays when sourced -- it reads no .env and contacts
# nothing -- so sourcing it costs nothing and buys `kubectl_bin` as well.
COMMON="${REPO}/scripts/lib/common.sh"
# THE PROJECT IS THE CALLER'S, NEVER A DEFAULT (DAYONE-K8S-1, design §7).
# common.sh's load_env fills an unset PROJECT_ID with the live project, so a
# caller in any other project that forgot it would read that project's cluster
# context check, uniqueIds and network, and render its identities. Marked here,
# before load_env runs: the environment or the env file replaces the mark, and
# a mark that survives is a project nobody gave, refused below once --help has
# had its chance. "(none given)" is no project id, so load_env's `:-` keeps it.
PROJECT_NOT_GIVEN="(none given)"
[[ -n "${PROJECT_ID:-}" ]] || PROJECT_ID="${PROJECT_NOT_GIVEN}"
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
SPEC_VERIFY_KEYS_FILE=""
# The workspace a group or service namespace is built for (the job's step A7,
# docs/design/workspace-kinds.md §4.3): rendered as swarm.saga.xyz/workspace on
# the Namespace, which is what the deployer's scope policy admits a namespace
# outside swarm-tenant-u-* by. Tenant mode only; empty for every other render.
WORKSPACE_ID=""
RENDER_ARGS=()
# --swarm-system's tailnet egress manifest, when terraform renders one; read
# in that mode, tested after the apply in every mode. The two secrets' names
# are fixed: terraform/infra/tailscale.tf creates the first, and
# kubernetes/swarm-system/tailscale-egress.yaml mounts the second.
TAILSCALE_MANIFEST=""
TAILSCALE_OAUTH_GSM_NAME="swarm-tailscale-oauth"
TAILSCALE_K8S_OBJECT="operator-oauth"

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

# SWARM-API'S EGRESS RANGE IS READ, NOT PASSED, for the same reason as the
# cluster network: it is admitted to the live relay, and a typed value is the
# one that goes stale when the subnet changes.
LIVE_FLAGS=(--swarm-api-egress-cidr --swarm-api-egress-source)

# SWARM-VERIFY'S uniqueId IS RESOLVED, NOT PASSED (VERIFY-GKE-READ-2), as the
# scheduler's and the reconciler's are: forwarded after the resolved value, a
# typed `--verify-uid` would win and bind whatever number it named into the
# eng and smoke namespaces' Job read.
VERIFY_FLAGS=(--verify-uid)

# THE PROJECT IS PROJECT_ID, NOT PASSED: every render below is given
# `--project "${PROJECT_ID}"`, the project whose cluster, uniqueIds and network
# this script reads. A forwarded `--project` would come after it and win, and
# render another project's identities into this cluster.
PROJECT_FLAGS=(--project)

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
    --swarm-system) MODE="swarm-system"; shift ;;
    --swarm-graph) MODE="swarm-graph"; shift ;;
    --swarm-typesafe-fwd) MODE="swarm-typesafe-fwd"; shift ;;
    --confirm|-y) CONFIRM=1; shift ;;
    --server-dry-run) SERVER_DRY_RUN=1; shift ;;
    --context)    CONTEXT="$2"; shift 2 ;;
    --cluster)    CLUSTER="$2"; shift 2 ;;
    # The `=` spellings of the three above. Without them `--cluster=NAME` was
    # forwarded, and the renderer read `--cluster` as an abbreviation of
    # `--cluster-dns-ip` and tried to render the cluster's NAME as its DNS IP.
    # The step-spec public keys (contract request 34): a file holding
    # `terraform -chdir=terraform/infra output -json spec_verify_keys_configmap`.
    # Rendered as the tenant namespace's swarm-spec-verify-keys ConfigMap and
    # applied with the rest of it. The release passes it for every tenant;
    # without it no ConfigMap is rendered and an existing one is left alone.
    --spec-verify-keys)   SPEC_VERIFY_KEYS_FILE="$2"; shift 2 ;;
    --spec-verify-keys=*) SPEC_VERIFY_KEYS_FILE="${1#*=}"; shift ;;
    --workspace-id)   [[ $# -ge 2 ]] || die "--workspace-id needs a value (w- and six hex digits)"
                      WORKSPACE_ID="$2"; shift 2 ;;
    --workspace-id=*) WORKSPACE_ID="${1#*=}"; shift ;;
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
      if LIVE_FLAG="$(derived_flag_for "$1" "${LIVE_FLAGS[@]}")"; then
        die "'${1%%=*}' would reach the renderer as ${LIVE_FLAG}, which this script reads from
       swarm-api's Cloud Run service and the subnetwork it names; it cannot be passed, in
       full or abbreviated."
      fi
      if IDENTITY_FLAG="$(derived_flag_for "$1" "${IDENTITY_FLAGS[@]}")"; then
        die "'${1%%=*}' would reach the renderer as ${IDENTITY_FLAG}, which this script reads from
       the tenant GSA's IAM policy (its roles/iam.workloadIdentityUser members); it cannot
       be passed, in full or abbreviated. Bind the KSA in IAM and re-run."
      fi
      if PROJECT_FLAG="$(derived_flag_for "$1" "${PROJECT_FLAGS[@]}")"; then
        die "'${1%%=*}' would reach the renderer as ${PROJECT_FLAG}; the project is PROJECT_ID
       (${PROJECT_ID}), the one whose cluster and identities this script reads. Set
       PROJECT_ID instead."
      fi
      if VERIFY_FLAG="$(derived_flag_for "$1" "${VERIFY_FLAGS[@]}")"; then
        die "'${1%%=*}' would reach the renderer as ${VERIFY_FLAG}, which this script resolves
       itself (swarm-verify's uniqueId, by uid_of, for the eng and smoke renders); it
       cannot be passed, in full or abbreviated."
      fi
      RENDER_ARGS+=("$1"); shift ;;
  esac
done

# --- 0. the project, before anything is contacted ------------------------------
[[ "${PROJECT_ID}" != "${PROJECT_NOT_GIVEN}" ]] \
  || die "PROJECT_ID is not set (in the environment or ${SWARM_ENV_FILE:-.env}); refusing to render.
       Every object here names its project's identities, and there is no default project.
       Run: PROJECT_ID=<your project> $0 ..."

# --- 0. the workspace label, before anything is contacted ---------------------
# Checked here as well as in render.py so a refused id costs no cluster read,
# and so it can only ever reach the one render that labels a Namespace: never
# the cluster-scoped policies or swarm-system, and never `render.py identity`.
WORKSPACE_ARGS=()
if [[ -n "${WORKSPACE_ID}" ]]; then
  [[ "${MODE}" == "tenant" ]] \
    || die "--workspace-id labels a tenant's Namespace (--tenant); --${MODE} renders none"
  [[ "${WORKSPACE_ID}" =~ ^w-[0-9a-f]{6}$ ]] \
    || die "--workspace-id '${WORKSPACE_ID}' is not a workspace id (w- and six hex digits)"
  WORKSPACE_ARGS=(--workspace-id "${WORKSPACE_ID}")
fi

# --- 0. --swarm-system's preconditions, before anything is contacted -----------
# Measured 2026-10-12: from a checkout whose terraform/infra was never
# initialised, --swarm-system got through kubectl and gcloud and then failed on
# terraform's own error -- or, from a checkout older than --swarm-system, with
# "--tenant is required". Each precondition this mode has is checked here, on
# the filesystem and the command line alone, and named in one line with what to
# do. It never asks for a tenant. (Its terraform OUTPUT is read below, after the
# context checks: nothing is asked of terraform for a refused cluster.)
if [[ "${MODE}" == "swarm-system" ]]; then
  [[ -z "${TENANT}" ]] || die "--swarm-system renders swarm-system's policies, not a tenant's; drop --tenant"
  [[ -z "${SPEC_VERIFY_KEYS_FILE}" ]] || die "--spec-verify-keys belongs to a tenant's namespace, not to swarm-system"
  [[ -z "${RENDER_ARGS[*]+x}" ]] \
    || die "--swarm-system takes no render arguments (got: ${RENDER_ARGS[*]}); its one value is read"
  tf_initialised \
    || die "terraform/infra is not initialised for ${ENVIRONMENT} (no $(tf_data_dir)); run: $(tf_init_command)"
fi
# --swarm-graph has the same preconditions, for the same reason: its one input
# is a terraform output, read after the context checks below.
if [[ "${MODE}" == "swarm-graph" ]]; then
  [[ -z "${TENANT}" ]] || die "--swarm-graph applies swarm-system's graph service, not a tenant's; drop --tenant"
  [[ -z "${SPEC_VERIFY_KEYS_FILE}" ]] || die "--spec-verify-keys belongs to a tenant's namespace, not to swarm-system"
  [[ -z "${RENDER_ARGS[*]+x}" ]] \
    || die "--swarm-graph takes no render arguments (got: ${RENDER_ARGS[*]}); terraform renders its manifest"
  tf_initialised \
    || die "terraform/infra is not initialised for ${ENVIRONMENT} (no $(tf_data_dir)); run: $(tf_init_command)"
fi
# --swarm-typesafe-fwd likewise.
if [[ "${MODE}" == "swarm-typesafe-fwd" ]]; then
  [[ -z "${TENANT}" ]] || die "--swarm-typesafe-fwd applies swarm-system's TypeSafe forwarder, not a tenant's; drop --tenant"
  [[ -z "${SPEC_VERIFY_KEYS_FILE}" ]] || die "--spec-verify-keys belongs to a tenant's namespace, not to swarm-system"
  [[ -z "${RENDER_ARGS[*]+x}" ]] \
    || die "--swarm-typesafe-fwd takes no render arguments (got: ${RENDER_ARGS[*]}); terraform renders its manifest"
  tf_initialised \
    || die "terraform/infra is not initialised for ${ENVIRONMENT} (no $(tf_data_dir)); run: $(tf_init_command)"
fi

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

# SWARM-API'S DIRECT VPC EGRESS RANGE, READ FROM ITS SERVICE, NEVER TYPED.
# Sets SWARM_API_EGRESS_CIDR and SWARM_API_EGRESS_SOURCE, or dies. Called, not
# $(...)'d: what it assigns is what the caller reads. --swarm-system reads it
# before rendering (the relay and its policies admit it); --swarm-graph reads
# it after applying, for the parity check of swarm-graph-ingress, whose stats
# rule terraform rendered from its own copy of the subnet (CACHE-FOLLOWUPS).
# EGRESS_UNREAD is what a failure says happened to the run: nothing rendered
# before an apply, a comparison not made after one.
EGRESS_UNREAD="nothing was rendered."
read_swarm_api_egress() {
  local ref
  # To files and read in THIS shell. The v1 (Knative) shape is what `gcloud run services
  # describe` returns -- the annotation holds a JSON list -- and the v2 shape
  # is read as well, as scripts/lib/deploy.sh does for the image, so a gcloud
  # that switches surfaces does not turn this into "no subnet".
  LIVE_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-live-netpol.XXXXXX")"
  if ! gcloud run services describe "${API_SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
      --format=json >"${LIVE_WORK}/service.json" 2>"${LIVE_WORK}/service.err"; then
    LIVE_ERR="$(redact <"${LIVE_WORK}/service.err")"
    rm -rf "${LIVE_WORK}"
    die_if_auth_failure "${LIVE_ERR}"
    err "could not describe Cloud Run service ${API_SERVICE} in ${PROJECT_ID}/${REGION}:"
    printf '%s\n' "${LIVE_ERR}" | sed -n '1,5p' | sed 's/^/     /' >&2
    die "swarm-system's ingress admits swarm-api's egress range, read from the service or not at all; ${EGRESS_UNREAD}"
  fi
  if ! jq -r '
      [ ((.spec.template.metadata.annotations["run.googleapis.com/network-interfaces"] // "[]")
          | fromjson | .[]? | .subnetwork // empty),
        (.template.vpcAccess.networkInterfaces[]? | .subnetwork // empty) ]
      | unique | .[]' "${LIVE_WORK}/service.json" >"${LIVE_WORK}/subnets" 2>"${LIVE_WORK}/jq.err"; then
    rm -rf "${LIVE_WORK}"
    die "could not read ${API_SERVICE}'s Direct VPC egress from its description; ${EGRESS_UNREAD}"
  fi
  # Counted, not `${#array[@]}`: an empty array is an unbound variable under
  # bash 3.2's `set -u` (CLAUDE.md), and none is exactly the case to report.
  SUBNET_REF=""
  SUBNET_COUNT=0
  while IFS= read -r ref; do
    [[ -n "${ref}" ]] || continue
    SUBNET_REF="${ref}"
    SUBNET_COUNT=$((SUBNET_COUNT + 1))
  done <"${LIVE_WORK}/subnets"
  if [[ "${SUBNET_COUNT}" -ne 1 ]]; then
    rm -rf "${LIVE_WORK}"
    die "${API_SERVICE} names ${SUBNET_COUNT} Direct VPC egress subnetworks, not exactly one; swarm-system
       admits one range and this script will not choose. (terraform/modules/cloud_run sets
       network_interfaces.subnetwork on every service.) ${EGRESS_UNREAD}"
  fi
  # A name or a path (projects/<p>/regions/<r>/subnetworks/<name>); the
  # region in a path wins over ${REGION}.
  SUBNET="${SUBNET_REF##*/}"
  SUBNET_REGION="${REGION}"
  case "${SUBNET_REF}" in
    */regions/*/subnetworks/*) SUBNET_REGION="${SUBNET_REF#*/regions/}"; SUBNET_REGION="${SUBNET_REGION%%/*}" ;;
  esac
  [[ "${SUBNET}" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || { rm -rf "${LIVE_WORK}"; die "${API_SERVICE}'s subnetwork '${SUBNET_REF}' is not a subnetwork name; ${EGRESS_UNREAD}"; }
  if ! gcloud compute networks subnets describe "${SUBNET}" --project "${PROJECT_ID}" \
      --region "${SUBNET_REGION}" --format=json >"${LIVE_WORK}/subnet.json" 2>"${LIVE_WORK}/subnet.err"; then
    LIVE_ERR="$(redact <"${LIVE_WORK}/subnet.err")"
    rm -rf "${LIVE_WORK}"
    die_if_auth_failure "${LIVE_ERR}"
    die "could not describe subnetwork ${SUBNET} in ${PROJECT_ID}/${SUBNET_REGION}; ${EGRESS_UNREAD}"
  fi
  SWARM_API_EGRESS_CIDR="$(jq -r '.ipCidrRange // empty' "${LIVE_WORK}/subnet.json")"
  rm -rf "${LIVE_WORK}"
  [[ -n "${SWARM_API_EGRESS_CIDR}" ]] || die "subnetwork ${SUBNET} reports no ipCidrRange; ${EGRESS_UNREAD}"
  SWARM_API_EGRESS_SOURCE="run/${PROJECT_ID}/${SUBNET_REGION}/${API_SERVICE}/${SUBNET}"
  info "swarm-api egress  ${SWARM_API_EGRESS_CIDR}  (${SWARM_API_EGRESS_SOURCE})"
}

# --- render ------------------------------------------------------------------
PYTHON="${PYTHON_BIN:-python3}"

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
#
# Both modes need them. A tenant's RoleBindings name them; the cluster-scoped
# render's workspace-deployer RoleBinding policy allows the dispatcher and
# reaper Roles to be bound to them and nobody else, so a policy rendered
# without them would refuse the tenant render's own uniqueId subjects.
uid_of() {
  local account="$1" uid
  uid="$(gcloud iam service-accounts describe "${account}" \
    --project="${PROJECT_ID}" --format='value(uniqueId)' 2>/dev/null || true)"
  [[ "${uid}" =~ ^[0-9]{15,25}$ ]] \
    || die "could not resolve the uniqueId of ${account} (got '${uid}').
       Without it the RoleBinding authorises nobody -- see kubernetes/rbac/dispatcher-rbac.yaml."
  printf '%s' "${uid}"
}

if [[ "${MODE}" == "policies" ]]; then
  [[ -z "${SPEC_VERIFY_KEYS_FILE}" ]] || die "--spec-verify-keys belongs to a tenant's namespace, not to the cluster-scoped policies"
  SCHEDULER_UID="$(uid_of "swarm-scheduler@${PROJECT_ID}.iam.gserviceaccount.com")"
  RECONCILER_UID="$(uid_of "swarm-reconciler@${PROJECT_ID}.iam.gserviceaccount.com")"

  # THE WORKSPACE DEPLOYER'S uniqueId (docs/workspaces.md §2.3) is the one
  # lookup allowed to come back empty, because the account is created by the
  # owner's bootstrap apply and these policies may be applied before it. Then
  # render.py binds and matches the email alone -- in the ClusterRoleBinding
  # AND in the scope policy, the same value in both, so the deployer is either
  # bound and scoped or not bound at all; never bound and unscoped. Re-run this
  # once the account exists, or a deployer that presents its uniqueId is
  # refused by RBAC.
  DEPLOYER="swarm-workspace-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
  DEPLOYER_UID="$(gcloud iam service-accounts describe "${DEPLOYER}" \
    --project="${PROJECT_ID}" --format='value(uniqueId)' 2>/dev/null || true)"
  if [[ ! "${DEPLOYER_UID}" =~ ^[0-9]{15,25}$ ]]; then
    warn "could not resolve the uniqueId of ${DEPLOYER}; binding and scoping it by email only."
    warn "Re-run kubernetes/apply.sh --policies --confirm after the bootstrap apply creates it."
    DEPLOYER_UID=""
  fi
  info "rbac subjects  scheduler=${SCHEDULER_UID} reconciler=${RECONCILER_UID} workspace-deployer=${DEPLOYER_UID:-${DEPLOYER}}"

  # THE TERRAFORM TENANTS' NAMESPACES, which the deployer's scope policy
  # refuses by name (docs/design/workspace-kinds.md §3.1): read from this
  # environment's tfvars, the file that declares var.tenants. Not from
  # terraform state, which this mode never needed; and not every
  # environment's, which would refuse a name another environment's Terraform
  # owns (prod's `research`) and this environment's job may need. tf_var_file
  # dies when the file is missing, and render.py refuses one naming no tenant.
  TF_TFVARS="$(tf_var_file)"
  info "terraform tenants  refused by name, from ${TF_TFVARS#"${REPO}/"}"

  MANIFEST="$("${PYTHON}" "${HERE}/render.py" policies --project "${PROJECT_ID}" \
    --scheduler-uid "${SCHEDULER_UID}" --reconciler-uid "${RECONCILER_UID}" \
    --workspace-deployer-uid "${DEPLOYER_UID}" --terraform-tfvars "${TF_TFVARS}")"
  info "rendering cluster-scoped admission policies and the workspace deployer's RBAC"
elif [[ "${MODE}" == "swarm-system" ]]; then
  # --tenant, --spec-verify-keys and render arguments were refused at 0.

  # THE RELAY, FROM TERRAFORM, APPLIED FIRST (lane LB-L8e). Its manifest is
  # terraform/infra/live.tf's `swarm_live_manifest`: kubernetes/swarm-system/
  # swarm-live.yaml with this root's image digest, bucket, key reference,
  # reserved address and swarm-api's subnet range filled in. It creates the
  # swarm-system namespace, so it goes ahead of the policies in ONE apply
  # (kubectl applies a stream in order): a NetworkPolicy applied into a
  # namespace that does not exist fails mid-apply.
  #
  # Read here, AFTER the context checks above, and applied only through them:
  # nothing below can send it to agents-staging. Read FIRST in this mode,
  # before swarm-api is described, so a missing output is a precondition
  # reported up front rather than a late failure. To files, not $(...): the
  # exit status and the error are both kept (CLAUDE.md).
  RELAY_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-live-relay.XXXXXX")"
  # Held across swarm-api's reads below, any of which may die.
  trap 'rm -rf "${RELAY_WORK}"' EXIT
  RELAY_MANIFEST=""
  TF_DIR="$(tf_root)"
  if tf -chdir="${TF_DIR}" output -raw swarm_live_manifest >"${RELAY_WORK}/relay.yaml" 2>"${RELAY_WORK}/relay.err" \
      && [[ -s "${RELAY_WORK}/relay.yaml" ]]; then
    RELAY_MANIFEST="$(cat "${RELAY_WORK}/relay.yaml")"
  else
    # Null while enable_live_browser is off or swarm-live has no digest, and
    # `output -raw` refuses a null. (An uninitialised root was refused at 0.)
    # One actionable line, then terraform's own words, before swarm-api is
    # described: a run that cannot apply the relay stops here.
    MISSING="terraform output swarm_live_manifest is missing for ${ENVIRONMENT}: set enable_live_browser and a swarm-live digest in image_refs (terraform/infra/live.tf), then make tf-plan tf-apply"
    redact <"${RELAY_WORK}/relay.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    if [[ "${CONFIRM}" -eq 1 ]]; then
      rm -rf "${RELAY_WORK}"
      err "${MISSING}"
      die "no relay manifest: swarm-system's policies would select a relay that does not exist,
       in a namespace nothing created. Nothing was applied."
    fi
    warn "${MISSING}"
    warn "previewing swarm-system's policies alone; --confirm will refuse."
  fi

  read_swarm_api_egress


  if [[ -n "${RELAY_MANIFEST}" ]]; then
    # What the relay manifest may hold, checked before anything sees a
    # cluster: every object in swarm-system (the namespace itself aside),
    # no Kubernetes RBAC -- the relay calls no Kubernetes API -- and no
    # placeholder terraform left unfilled.
    if grep -Eo '__[A-Z0-9_]+__' "${RELAY_WORK}/relay.yaml" >"${RELAY_WORK}/placeholders"; then
      PLACEHOLDER="$(head -n 1 "${RELAY_WORK}/placeholders")"
      rm -rf "${RELAY_WORK}"
      die "the relay manifest carries an unrendered placeholder (${PLACEHOLDER}); nothing was applied."
    fi
    if grep -Eq '^kind:[[:space:]]*(Cluster)?Role(Binding)?[[:space:]]*$' "${RELAY_WORK}/relay.yaml"; then
      rm -rf "${RELAY_WORK}"
      die "the relay manifest carries Kubernetes RBAC; the relay holds none. Nothing was applied."
    fi
    if grep -E '^[[:space:]]+namespace:[[:space:]]' "${RELAY_WORK}/relay.yaml" \
        | grep -Ev '^[[:space:]]+namespace:[[:space:]]+swarm-system[[:space:]]*$' >"${RELAY_WORK}/foreign"; then
      redact <"${RELAY_WORK}/foreign" | sed -n '1,5p' | sed 's/^/     /' >&2
      rm -rf "${RELAY_WORK}"
      die "the relay manifest names a namespace other than swarm-system (above); nothing was applied."
    fi

    # TERRAFORM'S RANGE MUST BE SWARM-API'S. The relay's own NetworkPolicy
    # admits var.subnet_cidr, the range terraform gives every Cloud Run
    # service's Direct VPC egress; the policies below admit the range just
    # READ from swarm-api's service. Two values for one fact are held equal
    # here, before either reaches the cluster.
    if ! tf -chdir="${TF_DIR}" output -json live_relay >"${RELAY_WORK}/live_relay.json" 2>"${RELAY_WORK}/live_relay.err"; then
      redact <"${RELAY_WORK}/live_relay.err" | sed -n '1,5p' | sed 's/^/     /' >&2
      rm -rf "${RELAY_WORK}"
      die "could not read terraform's live_relay output (above); nothing was applied."
    fi
    TF_EGRESS_CIDR="$(jq -r '.swarm_api_egress_cidr // empty' "${RELAY_WORK}/live_relay.json")"
    if [[ "${TF_EGRESS_CIDR}" != "${SWARM_API_EGRESS_CIDR}" ]]; then
      rm -rf "${RELAY_WORK}"
      die "the relay manifest admits swarm-api's range as '${TF_EGRESS_CIDR:-nothing}' (terraform's var.subnet_cidr),
       but ${API_SERVICE} egresses from ${SWARM_API_EGRESS_CIDR} (${SWARM_API_EGRESS_SOURCE}).
       One of them is stale; nothing was applied."
    fi
    info "relay manifest  terraform output swarm_live_manifest (admits ${TF_EGRESS_CIDR})"
  fi

  # SWARM-EGRESS, FROM TERRAFORM, AFTER THE RELAY (docs/browser-egress.md;
  # lane EGRESS-SVC). terraform/infra/egress.tf's `swarm_egress_manifest`:
  # kubernetes/swarm-system/swarm-egress.yaml with the image digest, the two
  # secrets' version references and the browser egress setting filled in.
  # OPTIONAL, unlike the relay's: null while enable_browser_egress is off,
  # and then nothing about egress is applied. Present, it gets the relay's
  # three checks before anything sees a cluster.
  EGRESS_MANIFEST=""
  if tf -chdir="${TF_DIR}" output -raw swarm_egress_manifest >"${RELAY_WORK}/egress.yaml" 2>"${RELAY_WORK}/egress.err" \
      && [[ -s "${RELAY_WORK}/egress.yaml" ]]; then
    if grep -Eo '__[A-Z0-9_]+__' "${RELAY_WORK}/egress.yaml" >"${RELAY_WORK}/egress-placeholders"; then
      PLACEHOLDER="$(head -n 1 "${RELAY_WORK}/egress-placeholders")"
      rm -rf "${RELAY_WORK}"
      die "the swarm-egress manifest carries an unrendered placeholder (${PLACEHOLDER}); nothing was applied."
    fi
    if grep -Eq '^kind:[[:space:]]*(Cluster)?Role(Binding)?[[:space:]]*$' "${RELAY_WORK}/egress.yaml"; then
      rm -rf "${RELAY_WORK}"
      die "the swarm-egress manifest carries Kubernetes RBAC; swarm-egress holds none. Nothing was applied."
    fi
    if grep -E '^[[:space:]]+namespace:[[:space:]]' "${RELAY_WORK}/egress.yaml" \
        | grep -Ev '^[[:space:]]+namespace:[[:space:]]+swarm-system[[:space:]]*$' >"${RELAY_WORK}/egress-foreign"; then
      redact <"${RELAY_WORK}/egress-foreign" | sed -n '1,5p' | sed 's/^/     /' >&2
      rm -rf "${RELAY_WORK}"
      die "the swarm-egress manifest names a namespace other than swarm-system (above); nothing was applied."
    fi
    EGRESS_MANIFEST="$(cat "${RELAY_WORK}/egress.yaml")"
    info "egress manifest terraform output swarm_egress_manifest"
  else
    info "no swarm-egress manifest (enable_browser_egress is off); nothing about browser egress is applied"
  fi

  # THE TAILNET EGRESS, FROM TERRAFORM, AFTER SWARM-EGRESS (lane JUDGE-NET;
  # docs/runbooks/tailscale-egress.md). terraform/infra/tailscale.tf's
  # `tailscale_egress_manifest`: kubernetes/swarm-system/tailscale-egress.yaml
  # with the tailnet name filled in. OPTIONAL, like swarm-egress's: null while
  # enable_tailscale_egress is off, and then nothing about the tailnet is
  # applied and no secret is read. Present, it gets the same three checks,
  # plus one of its own: it must not carry a Secret. The OAuth client secret
  # reaches the cluster only through the sync step after the apply, never
  # inside a manifest that is previewed, diffed and dry-run.
  TAILSCALE_MANIFEST=""
  if tf -chdir="${TF_DIR}" output -raw tailscale_egress_manifest >"${RELAY_WORK}/tailscale.yaml" 2>"${RELAY_WORK}/tailscale.err" \
      && [[ -s "${RELAY_WORK}/tailscale.yaml" ]]; then
    if grep -Eo '__[A-Z0-9_]+__' "${RELAY_WORK}/tailscale.yaml" >"${RELAY_WORK}/tailscale-placeholders"; then
      PLACEHOLDER="$(head -n 1 "${RELAY_WORK}/tailscale-placeholders")"
      rm -rf "${RELAY_WORK}"
      die "the tailscale egress manifest carries an unrendered placeholder (${PLACEHOLDER}); nothing was applied."
    fi
    if grep -Eq '^kind:[[:space:]]*(Cluster)?Role(Binding)?[[:space:]]*$' "${RELAY_WORK}/tailscale.yaml"; then
      rm -rf "${RELAY_WORK}"
      die "the tailscale egress manifest carries Kubernetes RBAC; the forwarder holds none. Nothing was applied."
    fi
    if grep -Eq '^kind:[[:space:]]*Secret[[:space:]]*$' "${RELAY_WORK}/tailscale.yaml"; then
      rm -rf "${RELAY_WORK}"
      die "the tailscale egress manifest carries a Secret; the OAuth secret is synced from Secret Manager, never shipped in a manifest. Nothing was applied."
    fi
    if grep -E '^[[:space:]]+namespace:[[:space:]]' "${RELAY_WORK}/tailscale.yaml" \
        | grep -Ev '^[[:space:]]+namespace:[[:space:]]+swarm-system[[:space:]]*$' >"${RELAY_WORK}/tailscale-foreign"; then
      redact <"${RELAY_WORK}/tailscale-foreign" | sed -n '1,5p' | sed 's/^/     /' >&2
      rm -rf "${RELAY_WORK}"
      die "the tailscale egress manifest names a namespace other than swarm-system (above); nothing was applied."
    fi
    TAILSCALE_MANIFEST="$(cat "${RELAY_WORK}/tailscale.yaml")"
    info "tailnet egress  terraform output tailscale_egress_manifest"
  else
    info "no tailscale egress manifest (enable_tailscale_egress is off); nothing about the tailnet is applied"
  fi
  rm -rf "${RELAY_WORK}"

  if ! "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" get namespace swarm-system -o name >/dev/null 2>&1; then
    if [[ -n "${RELAY_MANIFEST}" ]]; then
      info "namespace swarm-system does not exist yet; the relay manifest creates it, ahead of the policies."
    else
      warn "namespace swarm-system does not exist yet, and there is no relay manifest to create it."
    fi
  fi

  POLICIES="$("${PYTHON}" "${HERE}/render.py" swarm-system \
    --swarm-api-egress-cidr "${SWARM_API_EGRESS_CIDR}" \
    --swarm-api-egress-source "${SWARM_API_EGRESS_SOURCE}")"
  if [[ -n "${RELAY_MANIFEST}" ]]; then
    MANIFEST="${RELAY_MANIFEST}"$'\n---\n'"${POLICIES}"
    info "rendering the live relay, then swarm-system's ingress policies"
    if [[ -n "${EGRESS_MANIFEST}" ]]; then
      MANIFEST="${MANIFEST}"$'\n---\n'"${EGRESS_MANIFEST}"
      info "and swarm-egress after them"
    fi
    if [[ -n "${TAILSCALE_MANIFEST}" ]]; then
      MANIFEST="${MANIFEST}"$'\n---\n'"${TAILSCALE_MANIFEST}"
      info "and the decision-api tailnet egress last"
    fi
  else
    MANIFEST="${POLICIES}"
    info "rendering swarm-system's ingress policies"
  fi
elif [[ "${MODE}" == "swarm-graph" ]]; then
  # --tenant, --spec-verify-keys and render arguments were refused at 0.

  # THE SHARED GRAPH SERVICE, FROM TERRAFORM, ALONE (lane GS-ROLL, part of
  # #1384; docs/design/graph-service.md §2.1, §8.2). terraform/infra/
  # graph_service.tf's `swarm_graph_manifest`: kubernetes/swarm-system/
  # swarm-graph.yaml with the image digest, the ticket key's version
  # reference, the bucket and the kill switch filled in. It carries its own
  # Namespace object, so it needs nothing --swarm-system applies, and it is
  # its own mode rather than a fourth manifest in --swarm-system's stream:
  # that stream refuses to apply without the live relay's manifest, and the
  # graph service does not depend on the live browser.
  #
  # NULL UNTIL graph_service.serve (every environment today), and then
  # nothing is applied and this exits 0: off is not a failure. Read here,
  # AFTER the context checks above, so nothing below can send it to
  # agents-staging. To files, not $(...): the status and the error are kept.
  GRAPH_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-graph-apply.XXXXXX")"
  trap 'rm -rf "${GRAPH_WORK}"' EXIT
  TF_DIR="$(tf_root)"
  if ! tf -chdir="${TF_DIR}" output -raw swarm_graph_manifest >"${GRAPH_WORK}/graph.yaml" 2>"${GRAPH_WORK}/graph.err" \
      || [[ ! -s "${GRAPH_WORK}/graph.yaml" ]]; then
    redact <"${GRAPH_WORK}/graph.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    info "no swarm-graph manifest (graph_service.serve is off, or no swarm-graph digest); nothing about the graph service is applied"
    exit 0
  fi
  # The checks every swarm-system manifest from terraform gets, plus the
  # tailnet's: no placeholder left unfilled, no Kubernetes RBAC (the service
  # calls no Kubernetes API), no Secret (the ticket key is read from Secret
  # Manager at run time by the pod's own identity, never shipped in a
  # manifest that is previewed and diffed), and nothing outside swarm-system.
  if grep -Eo '__[A-Z0-9_]+__' "${GRAPH_WORK}/graph.yaml" >"${GRAPH_WORK}/graph-placeholders"; then
    PLACEHOLDER="$(head -n 1 "${GRAPH_WORK}/graph-placeholders")"
    die "the swarm-graph manifest carries an unrendered placeholder (${PLACEHOLDER}); nothing was applied."
  fi
  if grep -Eq '^kind:[[:space:]]*(Cluster)?Role(Binding)?[[:space:]]*$' "${GRAPH_WORK}/graph.yaml"; then
    die "the swarm-graph manifest carries Kubernetes RBAC; swarm-graph holds none. Nothing was applied."
  fi
  if grep -Eq '^kind:[[:space:]]*Secret[[:space:]]*$' "${GRAPH_WORK}/graph.yaml"; then
    die "the swarm-graph manifest carries a Secret; the ticket key is read from Secret Manager, never shipped in a manifest. Nothing was applied."
  fi
  if grep -E '^[[:space:]]+namespace:[[:space:]]' "${GRAPH_WORK}/graph.yaml" \
      | grep -Ev '^[[:space:]]+namespace:[[:space:]]+swarm-system[[:space:]]*$' >"${GRAPH_WORK}/graph-foreign"; then
    redact <"${GRAPH_WORK}/graph-foreign" | sed -n '1,5p' | sed 's/^/     /' >&2
    die "the swarm-graph manifest names a namespace other than swarm-system (above); nothing was applied."
  fi
  MANIFEST="$(cat "${GRAPH_WORK}/graph.yaml")"
  rm -rf "${GRAPH_WORK}"
  info "rendering the graph service  terraform output swarm_graph_manifest"
elif [[ "${MODE}" == "swarm-typesafe-fwd" ]]; then
  # --tenant, --spec-verify-keys and render arguments were refused at 0.

  # THE TYPESAFE FORWARDER, FROM TERRAFORM, ALONE (lane TS-ROLL, part of
  # #1347; docs/design/workspace-secrets.md §3.5). terraform/infra/
  # typesafe_fwd.tf's `swarm_typesafe_fwd_manifest`: kubernetes/swarm-system/
  # swarm-typesafe-fwd.yaml with the image digest, the key's version
  # reference, the project and the setting filled in. Its own mode, as
  # --swarm-graph is, rather than a fifth manifest in --swarm-system's stream
  # (where design §3.5 first placed it): that stream refuses to apply without
  # the live relay's manifest, and the forwarder does not depend on the live
  # browser. It carries its own Namespace object, so it needs nothing
  # --swarm-system applies.
  #
  # NULL UNTIL typesafe_forwarder.serve, and then nothing is applied and this
  # exits 0. Terraform leaves a null output OUT of state, so the named read
  # fails 'Output "..." not found' exactly as an unreadable state does (PR
  # 1402); one unnamed `output -json` tells them apart: an object without a
  # non-null value is off, a state terraform cannot read fails. Read here,
  # AFTER the context checks above, so nothing below can send it to
  # agents-staging. To files, not $(...): the status and the error are kept.
  TSF_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-typesafe-fwd-apply.XXXXXX")"
  trap 'rm -rf "${TSF_WORK}"' EXIT
  TF_DIR="$(tf_root)"
  if ! tf -chdir="${TF_DIR}" output -raw swarm_typesafe_fwd_manifest >"${TSF_WORK}/fwd.yaml" 2>"${TSF_WORK}/fwd.err" \
      || [[ ! -s "${TSF_WORK}/fwd.yaml" ]]; then
    if tf -chdir="${TF_DIR}" output -json >"${TSF_WORK}/outputs.json" 2>>"${TSF_WORK}/fwd.err" \
        && jq -e --arg k swarm_typesafe_fwd_manifest \
          'type == "object" and ((.[$k].value // null) == null or .[$k].value == "")' \
          "${TSF_WORK}/outputs.json" >/dev/null 2>&1; then
      info "no swarm-typesafe-fwd manifest (typesafe_forwarder.serve is off, or no swarm-typesafe-fwd digest); nothing about the TypeSafe forwarder is applied"
      exit 0
    fi
    redact <"${TSF_WORK}/fwd.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    die "could not read terraform's swarm_typesafe_fwd_manifest output (above); nothing was applied."
  fi
  # swarm-egress's checks, and the graph service's: no placeholder left
  # unfilled, no Kubernetes RBAC (the forwarder calls no Kubernetes API), no
  # Secret (the key is read from Secret Manager by the pod's own identity,
  # never shipped in a manifest that is previewed and diffed), and nothing
  # outside swarm-system.
  if grep -Eo '__[A-Z0-9_]+__' "${TSF_WORK}/fwd.yaml" >"${TSF_WORK}/fwd-placeholders"; then
    PLACEHOLDER="$(head -n 1 "${TSF_WORK}/fwd-placeholders")"
    die "the swarm-typesafe-fwd manifest carries an unrendered placeholder (${PLACEHOLDER}); nothing was applied."
  fi
  if grep -Eq '^kind:[[:space:]]*(Cluster)?Role(Binding)?[[:space:]]*$' "${TSF_WORK}/fwd.yaml"; then
    die "the swarm-typesafe-fwd manifest carries Kubernetes RBAC; the forwarder holds none. Nothing was applied."
  fi
  if grep -Eq '^kind:[[:space:]]*Secret[[:space:]]*$' "${TSF_WORK}/fwd.yaml"; then
    die "the swarm-typesafe-fwd manifest carries a Secret; the key is read from Secret Manager, never shipped in a manifest. Nothing was applied."
  fi
  if grep -E '^[[:space:]]+namespace:[[:space:]]' "${TSF_WORK}/fwd.yaml" \
      | grep -Ev '^[[:space:]]+namespace:[[:space:]]+swarm-system[[:space:]]*$' >"${TSF_WORK}/fwd-foreign"; then
    redact <"${TSF_WORK}/fwd-foreign" | sed -n '1,5p' | sed 's/^/     /' >&2
    die "the swarm-typesafe-fwd manifest names a namespace other than swarm-system (above); nothing was applied."
  fi
  MANIFEST="$(cat "${TSF_WORK}/fwd.yaml")"
  rm -rf "${TSF_WORK}"
  info "rendering the TypeSafe forwarder  terraform output swarm_typesafe_fwd_manifest"
else
  [[ -n "${TENANT}" ]] || die "--tenant is required (or pass --policies, --swarm-system, --swarm-graph or --swarm-typesafe-fwd)"

  # A WORKSPACE TENANT'S QUOTA IS ITS RECORD'S (QUOTA-ONE, #2039; BGD's
  # decision, 2026-10-23). The workspace job's A7 renders the namespace with
  # five --quota-* flags from the record's limits, and its A9 fails the
  # workspace when the live quota has other numbers. This script, run by hand
  # with no quota flag, used to render render.py's flat defaults (pods 100,
  # cpu 400) for every tenant, and on 2026-10-23 that failed u-bogdan's
  # workspace, whose 100 agents want pods 200, cpu 800. So:
  #   * all five --quota-* flags given (the job's A7, under a call guard that
  #     admits no call to swarm-api): rendered as given, nothing is read;
  #   * a Terraform tenant -- named in this environment's tfvars, eng or smoke
  #     -- has no workspace record: render.py's defaults, nothing is read, and
  #     the release, whose account is no swarm-api admin, applies it as before;
  #   * any other tenant: its record is read from swarm-api with the
  #     operator's own credentials (--workspace-id names it; else the admin
  #     list, matched by the tenant its principal derives), and render.py's
  #     `workspace-quota` turns its limits into the five flags with the job's
  #     formula. No record names it: the defaults, said so. An unreadable
  #     answer renders nothing -- falling back to the defaults is the defect.
  # A --quota-* flag on the command line still wins over the record's: the
  # record's flags are passed first, and render.py takes the last of a flag.
  QUOTA_ARGS=()
  # render.py's tenant render has no quota default (DAYONE-K8S-1): where no
  # record exists, the Terraform tenant quota is passed by name, as flags, from
  # `render.py terraform-quota`, before any flag given on the command line.
  terraform_quota_args() {
    local line
    QUOTA_ARGS=()
    while IFS= read -r line; do
      if [[ -n "${line}" ]]; then QUOTA_ARGS+=("${line}"); fi
    done < <("${PYTHON}" "${HERE}/render.py" terraform-quota)
    [[ "${#QUOTA_ARGS[@]}" -eq 10 ]] \
      || die "render.py terraform-quota printed ${#QUOTA_ARGS[@]} values, not five flags and their numbers"
  }
  QUOTA_SEEN=" "
  for arg in ${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}; do
    case "${arg%%=*}" in
      --quota-pods|--quota-cpu|--quota-memory|--quota-jobs|--quota-ephemeral)
        [[ "${QUOTA_SEEN}" == *" ${arg%%=*} "* ]] || QUOTA_SEEN+="${arg%%=*} " ;;
    esac
  done
  read -r -a QUOTA_GIVEN <<<"${QUOTA_SEEN}"
  TF_TENANTS=""
  if [[ "${#QUOTA_GIVEN[@]}" -lt 5 ]]; then
    TF_TFVARS="$(tf_var_file)"
    TF_TENANTS="$("${PYTHON}" "${HERE}/render.py" terraform-tenants --terraform-tfvars "${TF_TFVARS}")" \
      || die "could not read the Terraform tenants from ${TF_TFVARS} (above); nothing was rendered or applied."
  fi
  if [[ "${#QUOTA_GIVEN[@]}" -eq 5 ]]; then
    info "quota  as given: all five --quota-* flags are on the command line; no workspace record is read"
  elif [[ $'\n'"${TF_TENANTS}"$'\n' == *$'\n'"${TENANT}"$'\n'* ]]; then
    terraform_quota_args
    info "quota  render.py's Terraform tenant quota: ${TENANT} is a Terraform tenant (${ENVIRONMENT}.tfvars), which has no workspace record"
  else
    QUOTA_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-quota.XXXXXX")"
    QUOTA_PATH="/admin/workspaces${WORKSPACE_ID:+/${WORKSPACE_ID}}"
    # To a file, in THIS shell: api_request sets API_STATUS, which a command
    # substitution would throw away (CLAUDE.md).
    if ! api_get "${QUOTA_PATH}" >"${QUOTA_WORK}/record.json"; then
      rm -rf "${QUOTA_WORK}"
      die "could not read ${TENANT}'s workspace record from swarm-api (GET ${API_PREFIX}${QUOTA_PATH}: HTTP ${API_STATUS}).
       A workspace tenant's namespace quota is its record's; nothing was rendered or applied.
       Read it as a swarm-api admin, or pass all five --quota-* flags (scripts/register-tenant.sh
       ws_quota_args computes them from the record's limits)."
    fi
    if ! "${PYTHON}" "${HERE}/render.py" workspace-quota --tenant "${TENANT}" \
        --record "${QUOTA_WORK}/record.json" --with-id >"${QUOTA_WORK}/args"; then
      rm -rf "${QUOTA_WORK}"
      die "could not turn ${TENANT}'s workspace record into its quota (above); nothing was rendered or applied."
    fi
    QUOTA_ID=""
    while IFS= read -r line; do
      [[ -n "${line}" ]] || continue
      if [[ -z "${QUOTA_ID}" ]]; then QUOTA_ID="${line}"; else QUOTA_ARGS+=("${line}"); fi
    done <"${QUOTA_WORK}/args"
    rm -rf "${QUOTA_WORK}"
    if [[ -z "${QUOTA_ID}" ]]; then
      terraform_quota_args
      info "quota  render.py's Terraform tenant quota: no workspace record names ${TENANT}"
    else
      [[ "${QUOTA_ID}" =~ ^w-[0-9a-f]{6}$ ]] || QUOTA_ID="(an unrecognised id)"
      [[ "${#QUOTA_ARGS[@]}" -eq 10 ]] \
        || die "render.py workspace-quota printed ${#QUOTA_ARGS[@]} values, not five flags and their numbers"
      info "quota  pods ${QUOTA_ARGS[1]}, cpu ${QUOTA_ARGS[3]}, memory ${QUOTA_ARGS[5]}, jobs ${QUOTA_ARGS[7]}, ephemeral ${QUOTA_ARGS[9]}  (workspace record ${QUOTA_ID}${QUOTA_GIVEN[0]+; ${QUOTA_GIVEN[*]} as given})"
    fi
  fi

  SCHEDULER_UID="$(uid_of "swarm-scheduler@${PROJECT_ID}.iam.gserviceaccount.com")"
  RECONCILER_UID="$(uid_of "swarm-reconciler@${PROJECT_ID}.iam.gserviceaccount.com")"

  # THE VERIFICATION GATE'S uniqueId (VERIFY-GKE-READ-2, owner decision
  # 2026-10-21), for the Job read kubernetes/rbac/verify-rbac.yaml binds in the
  # eng and smoke namespaces only. render.py says whether this render carries
  # it (`verify-read`, with the same forwarded arguments) so the namespaces are
  # named once, there; elsewhere -- every other tenant, a person's namespace, a
  # group workspace -- the account is not looked up at all, and the workspace
  # deployer, which runs this script for those, gains no lookup. Where the read
  # is rendered, a missing account is as FATAL as a missing scheduler: an eng
  # render without it applies cleanly and leaves e2e [10] failing on a 403.
  VERIFY_GSA="$("${PYTHON}" "${HERE}/render.py" verify-read --tenant "${TENANT}" --project "${PROJECT_ID}" \
    ${WORKSPACE_ARGS[@]+"${WORKSPACE_ARGS[@]}"} \
    "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}")" \
    || die "could not ask render.py whether tenant ${TENANT}'s render binds swarm-verify (above);
       nothing was rendered or applied."
  VERIFY_UID=""
  VERIFY_ARGS=()
  if [[ -n "${VERIFY_GSA}" ]]; then
    VERIFY_UID="$(uid_of "${VERIFY_GSA}")"
    VERIFY_ARGS=(--verify-uid "${VERIFY_UID}")
  fi
  info "rbac subjects  scheduler=${SCHEDULER_UID} reconciler=${RECONCILER_UID} verify=${VERIFY_UID:-not bound in this namespace}"

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
  IDENTITY="$("${PYTHON}" "${HERE}/render.py" identity --tenant "${TENANT}" --project "${PROJECT_ID}" \
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

  SPEC_ARGS=()
  if [[ -n "${SPEC_VERIFY_KEYS_FILE}" ]]; then
    [[ -r "${SPEC_VERIFY_KEYS_FILE}" ]] || die "--spec-verify-keys ${SPEC_VERIFY_KEYS_FILE}: no such readable file"
    # The environment the keys must belong to: render.py accepts only
    # swarm-${ENVIRONMENT}-specs/step-spec (#346), so another environment's key,
    # or any other key in the shared project, is refused before anything is
    # applied. ${ENVIRONMENT} is common.sh's (load_env), which the release sets.
    SPEC_ARGS+=(--spec-verify-keys-file "${SPEC_VERIFY_KEYS_FILE}" --environment "${ENVIRONMENT}")
    info "step-spec keys   rendering swarm-spec-verify-keys from ${SPEC_VERIFY_KEYS_FILE} (environment ${ENVIRONMENT})"
  else
    dim "  no --spec-verify-keys: swarm-spec-verify-keys is not rendered; an existing one is left as it is"
  fi

  MANIFEST="$("${PYTHON}" "${HERE}/render.py" tenant --tenant "${TENANT}" --project "${PROJECT_ID}" \
    ${SPEC_ARGS[@]+"${SPEC_ARGS[@]}"} \
    --scheduler-uid "${SCHEDULER_UID}" \
    --reconciler-uid "${RECONCILER_UID}" \
    ${VERIFY_ARGS[@]+"${VERIFY_ARGS[@]}"} \
    --pod-cidr "${CLUSTER_POD_CIDR}" \
    --service-cidr "${CLUSTER_SERVICE_CIDR}" \
    --cluster-dns-ip "${CLUSTER_DNS_IP}" \
    --node-local-dns-ip "${CLUSTER_NODE_LOCAL_DNS_IP}" \
    --network-source "${CLUSTER_NETWORK_SOURCE}" \
    ${BOUND_KSA_ARGS[@]+"${BOUND_KSA_ARGS[@]}"} \
    ${WORKSPACE_ARGS[@]+"${WORKSPACE_ARGS[@]}"} \
    ${QUOTA_ARGS[@]+"${QUOTA_ARGS[@]}"} \
    "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}")"
  info "rendering tenant ${TENANT}${WORKSPACE_ID:+ (namespace labelled swarm.saga.xyz/workspace=${WORKSPACE_ID})}"
fi

# Belt and braces on the property above: a render marked offline carries
# documentation addresses, and must never reach a cluster. Matched on the
# annotation itself -- the templates' comments are rendered too, and they
# name the marker. Every mode: the tenant egress policy and swarm-system's
# relay ingress both carry it.
case "${MANIFEST}" in
  *"swarm.saga.xyz/network-source: offline-render-not-for-apply"*)
    die "the render is marked offline-render-not-for-apply -- it carries no real
       cluster network. Refusing; nothing was applied." ;;
esac

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

# THE NETWORK PARITY CHECK, after a tenant apply. The context is the one this
# script resolved and checked above (`CURRENT`), passed explicitly so a
# current-context changed in between cannot redirect the read, and --cluster is
# this run's. Scoped to the tenant's namespace (`render.py identity`).
PARITY="${REPO}/scripts/lib/check-cluster-network-parity.sh"
PARITY_ARGS=()
if [[ "${MODE}" == "tenant" ]]; then
  PARITY_ARGS=(--require-live --context "${CURRENT}" --cluster "${CLUSTER}" --namespace "${TENANT_NAMESPACE}")
fi

if [[ "${CONFIRM}" -ne 1 ]]; then
  printf '%s\n' "${MANIFEST}" | "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" \
    diff -f - || true      # `diff` exits 1 when there IS a difference
  preview_verdicts
  if [[ "${MODE}" == "tenant" ]]; then
    dim "  --confirm then compares ${TENANT_NAMESPACE}'s egress policy with the live cluster:"
    dim "    scripts/lib/check-cluster-network-parity.sh ${PARITY_ARGS[*]}"
  fi
  if [[ "${MODE}" == "swarm-system" ]]; then
    dim "  --confirm then compares swarm-system's policies with ${SWARM_API_EGRESS_CIDR}"
    dim "    (kubernetes/network_parity.py --live-ingress)"
    if [[ -n "${TAILSCALE_MANIFEST}" ]]; then
      dim "  --confirm then copies Secret Manager ${TAILSCALE_OAUTH_GSM_NAME} into swarm-system/${TAILSCALE_K8S_OBJECT}"
      dim "    (never printed; a dry run does not read it)"
    fi
  fi
  if [[ "${MODE}" == "swarm-graph" ]]; then
    dim "  --confirm applies it, then reads swarm-api's egress range from its service and compares"
    dim "    swarm-system/swarm-graph-ingress with it (kubernetes/network_parity.py --graph-ingress);"
    dim "    scripts/roll-graph.sh --confirm also waits for deploy/swarm-graph and checks its pods"
    dim "    run the manifest's digest"
  fi
  if [[ "${MODE}" == "swarm-typesafe-fwd" ]]; then
    dim "  --confirm applies it; scripts/roll-typesafe-fwd.sh --confirm also waits for deploy/swarm-typesafe-fwd"
    dim "    and checks its pods run the manifest's digest"
  fi
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
if [[ "${MODE}" == "swarm-system" && -n "${TAILSCALE_MANIFEST}" ]]; then
  # THE TAILSCALE OAUTH SECRET, SYNCED (lane JUDGE-NET). terraform/infra/
  # tailscale.tf says why here and not a Job or the pod itself: this run
  # already passed the shared-cluster refusals, already writes swarm-system,
  # and gcloud verifies the TLS it reads over. Only under --confirm (a dry run
  # exited above without reading it), only through this context, and only
  # once the namespace the apply just made exists.
  #
  # The value goes Secret Manager -> a 0600 file in a 0700 directory ->
  # `kubectl create secret --dry-run=client` -> `kubectl apply`, and is
  # removed on every exit path. It is never an argument, a variable, a pipe
  # into something that echoes, or a line of output: what kubectl prints is
  # the object's name and verdict, and that goes through `redact` as well.
  TS_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-tailscale-oauth.XXXXXX")"
  chmod 0700 "${TS_WORK}"
  trap 'rm -rf "${TS_WORK}"' EXIT
  info "syncing Secret Manager ${TAILSCALE_OAUTH_GSM_NAME} into swarm-system/${TAILSCALE_K8S_OBJECT} (the value is never printed)"
  ( umask 077; : >"${TS_WORK}/client_secret" )
  if ! gcloud secrets versions access latest --secret "${TAILSCALE_OAUTH_GSM_NAME}" --project "${PROJECT_ID}" \
      --out-file="${TS_WORK}/client_secret" >/dev/null 2>"${TS_WORK}/access.err"; then
    TS_ERR="$(redact <"${TS_WORK}/access.err")"
    rm -rf "${TS_WORK}"
    die_if_auth_failure "${TS_ERR}"
    err "could not read ${TAILSCALE_OAUTH_GSM_NAME} from Secret Manager in ${PROJECT_ID}:"
    printf '%s\n' "${TS_ERR}" | sed -n '1,5p' | sed 's/^/     /' >&2
    die "applied, but swarm-system/${TAILSCALE_K8S_OBJECT} was not written, so the decision-api forwarder
       waits for it. Store the OAuth client secret (scripts/create-secrets.sh --tailscale-oauth --stdin)
       and re-run; docs/runbooks/tailscale-egress.md."
  fi
  # The SHAPE, checked without printing a byte of it: a Tailscale OAuth client
  # secret starts `tskey-client-`, and anything else (an auth key, an API
  # token, a paste of the wrong field) would log the node in as something
  # other than tag:swarmcloud's OAuth client, or not at all.
  if [[ "$(head -c 13 "${TS_WORK}/client_secret")" != "tskey-client-" ]]; then
    rm -rf "${TS_WORK}"
    die "applied, but the latest version of ${TAILSCALE_OAUTH_GSM_NAME} is not a Tailscale OAuth client secret
       (it does not start tskey-client-); swarm-system/${TAILSCALE_K8S_OBJECT} was not written. Store the
       right value with scripts/create-secrets.sh --tailscale-oauth --stdin."
  fi
  if ! ( umask 077
         "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" create secret generic "${TAILSCALE_K8S_OBJECT}" \
           --namespace swarm-system --from-file=client_secret="${TS_WORK}/client_secret" \
           --dry-run=client -o yaml >"${TS_WORK}/secret.yaml" 2>"${TS_WORK}/create.err" \
         && "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" label --local -f "${TS_WORK}/secret.yaml" \
           managed-by=swarm-terraform app.kubernetes.io/name=decision-api-egress \
           -o yaml >"${TS_WORK}/labelled.yaml" 2>>"${TS_WORK}/create.err" ); then
    redact <"${TS_WORK}/create.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    rm -rf "${TS_WORK}"
    die "applied, but swarm-system/${TAILSCALE_K8S_OBJECT} could not be built (above); it was not written."
  fi
  if ! "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" apply -f "${TS_WORK}/labelled.yaml" \
      >"${TS_WORK}/apply.out" 2>&1; then
    redact <"${TS_WORK}/apply.out" | sed -n '1,5p' | sed 's/^/     /' >&2
    rm -rf "${TS_WORK}"
    die "applied, but swarm-system/${TAILSCALE_K8S_OBJECT} could not be written (above)."
  fi
  redact <"${TS_WORK}/apply.out" | sed 's/^/  /'
  rm -rf "${TS_WORK}"
  ok "swarm-system/${TAILSCALE_K8S_OBJECT} holds the current ${TAILSCALE_OAUTH_GSM_NAME}; its value was never printed"
  dim "  after a rotation the running node keeps its login; restart it onto the new secret with"
  dim "    kubectl -n swarm-system rollout restart deployment/decision-api-egress"
fi
if [[ "${MODE}" == "tenant" ]]; then
  # RUN, not printed (#76): printed, the comparison happened only when someone
  # remembered to. Scoped to this tenant's namespace, so a stale neighbour
  # cannot turn this apply into a failure register-tenant.sh would report as
  # "not isolated" -- and a mismatch in THIS namespace is exactly that: its
  # egress policy does not match the network its workers resolve names on.
  info "comparing ${TENANT_NAMESPACE}'s egress policy with the live cluster"
  if ! "${PARITY}" "${PARITY_ARGS[@]}"; then
    die "applied, but ${TENANT_NAMESPACE}'s egress policy does not match the live cluster's network
       (scripts/lib/check-cluster-network-parity.sh, above). Its workers may not resolve names."
  fi
  dim "  every tenant namespace, not just this one:"
  dim "    scripts/lib/check-cluster-network-parity.sh --require-live --context ${CURRENT} --cluster ${CLUSTER}"
fi
if [[ "${MODE}" == "swarm-system" ]]; then
  # RUN, like the tenant parity check: the policies as the API server now
  # holds them, against the range read above. An allow without the deny, a
  # second port or another range is a failure here, not a pass.
  info "comparing swarm-system's ingress policies with swarm-api's ${SWARM_API_EGRESS_CIDR}"
  LIVE_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-live-parity.XXXXXX")"
  if ! "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" get networkpolicies -n swarm-system -o json \
      >"${LIVE_WORK}/policies.json" 2>"${LIVE_WORK}/policies.err"; then
    redact <"${LIVE_WORK}/policies.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    rm -rf "${LIVE_WORK}"
    die "applied, but swarm-system's NetworkPolicies could not be read back; nothing was compared."
  fi
  jq -n --arg cidr "${SWARM_API_EGRESS_CIDR}" --arg source "${SWARM_API_EGRESS_SOURCE}" \
    '{swarm_api_egress_cidr: $cidr, source: $source}' >"${LIVE_WORK}/live.json"
  if ! "${PYTHON}" "${HERE}/network_parity.py" --live-ingress \
      --live "${LIVE_WORK}/live.json" --policies "${LIVE_WORK}/policies.json"; then
    rm -rf "${LIVE_WORK}"
    die "applied, but swarm-system's ingress does not match what was rendered (above)."
  fi
  rm -rf "${LIVE_WORK}"
fi
if [[ "${MODE}" == "swarm-graph" ]]; then
  # RUN, like --swarm-system's (CACHE-FOLLOWUPS, part of #1835; BGD,
  # 2026-10-28; docs/design/graph-answer-cache.md §5.3). swarm-graph-ingress
  # admits swarm-api's range on the stats port, and terraform rendered that
  # range from var.subnet_cidr, its own copy of the subnet; the range swarm-api
  # actually egresses from is READ here, from its service, exactly as
  # --swarm-system reads it, and the policy as the API server now holds it is
  # judged against it by check_graph_ingress: three rules, one peer and one
  # port each -- tenants on 8080, swarm-api's range and the swarm-graph pods
  # on 8081 -- and nothing more. A stale subnet, a missing policy or any
  # widening fails the apply with the checker's reason.
  EGRESS_UNREAD="swarm-graph was applied, but its ingress was not compared."
  read_swarm_api_egress
  info "comparing swarm-system/swarm-graph-ingress with swarm-api's ${SWARM_API_EGRESS_CIDR}"
  GRAPH_PARITY_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-graph-parity.XXXXXX")"
  trap 'rm -rf "${GRAPH_PARITY_WORK}"' EXIT
  if ! "${KUBECTL}" "${KUBECTL_ARGS[@]+"${KUBECTL_ARGS[@]}"}" get networkpolicies -n swarm-system -o json \
      >"${GRAPH_PARITY_WORK}/policies.json" 2>"${GRAPH_PARITY_WORK}/policies.err"; then
    redact <"${GRAPH_PARITY_WORK}/policies.err" | sed -n '1,5p' | sed 's/^/     /' >&2
    die "applied, but swarm-system's NetworkPolicies could not be read back; swarm-graph-ingress was not compared."
  fi
  jq -n --arg cidr "${SWARM_API_EGRESS_CIDR}" --arg source "${SWARM_API_EGRESS_SOURCE}" \
    '{swarm_api_egress_cidr: $cidr, source: $source}' >"${GRAPH_PARITY_WORK}/live.json"
  if ! "${PYTHON}" "${HERE}/network_parity.py" --graph-ingress \
      --live "${GRAPH_PARITY_WORK}/live.json" --policies "${GRAPH_PARITY_WORK}/policies.json"; then
    die "applied, but swarm-system/swarm-graph-ingress does not match swarm-api's ${SWARM_API_EGRESS_CIDR} (above)."
  fi
  rm -rf "${GRAPH_PARITY_WORK}"
fi
