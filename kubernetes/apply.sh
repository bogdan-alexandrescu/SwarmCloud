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
# 2. The right cluster. This kubeconfig reaches three clusters that are not
#    ours: `agents-staging` (a live GKE Standard cluster owned by another team
#    in this same shared project), `agents-prod`, and an EKS cluster. Applying a
#    default-deny NetworkPolicy into the wrong one is a full outage for whoever
#    owns it. So the context must name the swarm's own Autopilot cluster, whose
#    name starts with `swarm-` -- the same rule terraform enforces on
#    `gke_autopilot.cluster_name` -- and every known-foreign cluster name is
#    refused outright, whatever flags are passed.
#
# 3. Dry run first, and offline by default. Without --confirm nothing is sent as
#    a write. Validation is client-side unless --server-dry-run is passed,
#    because a server dry run of a first-time tenant fails anyway (the namespace
#    it would create does not exist yet) and because the safest request to make
#    against a cluster you are not sure about is none.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# The swarm's own cluster, as `scripts/lib/common.sh` and terraform name it.
CLUSTER="${GKE_CLUSTER:-swarm-autopilot}"
#: Clusters in this kubeconfig that belong to other teams. Named, not inferred.
FOREIGN_CLUSTERS=(agents-staging agents-prod eks-cluster-name)
TENANT=""
MODE="tenant"
CONFIRM=0
SERVER_DRY_RUN=0
CONTEXT=""
RENDER_ARGS=()

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[2m%s\033[0m\n' "$*" >&2; }
ok() { printf '\033[32m%s\033[0m\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant|-t)  TENANT="$2"; shift 2 ;;
    --policies)   MODE="policies"; shift ;;
    --confirm|-y) CONFIRM=1; shift ;;
    --server-dry-run) SERVER_DRY_RUN=1; shift ;;
    --context)    CONTEXT="$2"; shift 2 ;;
    --cluster)    CLUSTER="$2"; shift 2 ;;
    -h|--help)    sed -n '2,28p' "$0"; exit 0 ;;
    # Everything else is passed straight to render.py, so --pss-enforce,
    # --pod-cidr, --quota-cpu and friends work without being restated here.
    *)            RENDER_ARGS+=("$1"); shift ;;
  esac
done

# --- 1. the right kubectl ----------------------------------------------------
KUBECTL=""
for candidate in "${KUBECTL_BIN:-}" /opt/homebrew/bin/kubectl "$(command -v kubectl || true)"; do
  [[ -n "${candidate}" && -x "${candidate}" ]] || continue
  version="$("${candidate}" version --client -o json 2>/dev/null \
    | sed -n 's/.*"minor": *"\([0-9]*\)".*/\1/p' | head -1)"
  if [[ -n "${version}" && "${version}" -ge 30 ]]; then
    KUBECTL="${candidate}"
    break
  fi
  info "skipping ${candidate}: client minor version '${version:-unknown}' is too old"
done
[[ -n "${KUBECTL}" ]] || die "no kubectl >= 1.30 found; /opt/homebrew/bin/kubectl is the expected one"
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

for foreign in "${FOREIGN_CLUSTERS[@]}"; do
  case "${CURRENT}" in
    *"${foreign}"*)
      die "context '${CURRENT}' names '${foreign}', which belongs to another team. Refusing." ;;
  esac
done
case "${CLUSTER}" in
  swarm-*) ;;
  *) die "cluster '${CLUSTER}' does not start with 'swarm-'; the swarm's Autopilot
       cluster does, and terraform refuses to manage any cluster that does not." ;;
esac
case "${CURRENT}" in
  *"${CLUSTER}"*) ;;
  *) die "context '${CURRENT}' does not name cluster '${CLUSTER}'; refusing to apply.
       Run scripts/configure-kubectl.sh, or pass --context/--cluster deliberately." ;;
esac
info "context  ${CURRENT}"

# --- render ------------------------------------------------------------------
PYTHON="${PYTHON_BIN:-python3}"
if [[ "${MODE}" == "policies" ]]; then
  MANIFEST="$("${PYTHON}" "${HERE}/render.py" policies)"
  info "rendering cluster-scoped admission policies"
else
  [[ -n "${TENANT}" ]] || die "--tenant is required (or pass --policies)"
  MANIFEST="$("${PYTHON}" "${HERE}/render.py" tenant --tenant "${TENANT}" "${RENDER_ARGS[@]+"${RENDER_ARGS[@]}"}")"
  info "rendering tenant ${TENANT}"
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
