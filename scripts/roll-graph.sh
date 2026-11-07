#!/usr/bin/env bash
# Roll swarm-graph, the shared graph service (Deployment swarm-system/swarm-graph
# on the swarm's Autopilot cluster), to the image this environment's terraform
# rendered, wait for the rollout, and prove its pods run that digest
# (docs/design/graph-service.md §2.1, §8.2 step 2; lane GS-ROLL, part of #1384).
#
#   scripts/roll-graph.sh                                       # dry run: apply.sh's preview
#   scripts/roll-graph.sh --confirm                             # apply, rollout, digest
#   scripts/roll-graph.sh --tf-init --get-credentials --confirm # what the release runs
#   scripts/roll-graph.sh --confirm --timeout 900               # a slower Autopilot scale-up
#
# WHY. terraform/infra/graph_service.tf renders `swarm_graph_manifest` with the
# release's swarm-graph digest (GS-INFRA, PR 1399), but nothing applied it: the
# file's own step 2 was an operator piping `terraform output` into kubectl by
# hand. The release runs this after the swarm-egress roll, so every release
# rolls the graph service to the image it built, and a crash-looping or stale
# service fails the release instead of every agent's graph tools.
#
# OFF IS NOT A FAILURE. The output is null until graph_service.serve is set
# (every environment today); then this prints one notice and exits 0, having
# asked nothing of gcloud or the cluster. Terraform does not store a null
# output in state at all, so a named read of it -- `-raw` or `-json` -- fails
# with 'Output "swarm_graph_manifest" not found', exactly as a missing state
# would (release 38867328505, 2026-10-16, went red on that). A failed named
# read is told apart with ONE unnamed `output -json`: a state that reads as an
# object and has no non-null swarm_graph_manifest is off; a state terraform
# cannot read at all fails.
#
# IN THE SHAPE OF scripts/roll-egress.sh (roll-live-relay.sh), not a call to
# it: that implementation rolls StatefulSets through `apply.sh --swarm-system`,
# which needs the live relay's manifest, and swarm-graph is a Deployment that
# needs neither. The flags it shares mean the same here; it has no --force or
# --defer-*: the service is two replicas rolled one surge at a time
# (maxUnavailable 0) and holds no session, so there is nothing to defer for,
# and an unchanged manifest applies as "unchanged" and rolls nothing.
#
# THE APPLY IS apply.sh's (`kubernetes/apply.sh --swarm-graph`), which reads
# the same output again, refuses a manifest with a placeholder, RBAC, a Secret
# or an object outside swarm-system, and repeats every cluster check below.
#
# SAFETY, with common.sh's deny-list and kubectl resolution, as the relay's:
#   * kubectl from `kubectl_bin` (an old client silently drops fields);
#   * the cluster -- from --cluster, terraform's `gke_cluster_name` or
#     GKE_CLUSTER -- must start with `swarm-` and match nothing cluster-shaped
#     on SHARED_DENY_LIST, checked BEFORE get-credentials and before apply.sh;
#   * the context's kubeconfig cluster entry must be exactly
#     gke_<PROJECT_ID>_<GKE_LOCATION>_<cluster>, so the cluster apply.sh writes
#     to and the cluster this script waits on are one, and never agents-staging.
#
# Exit: 0 swarm-graph runs the rendered digest (or it is not served, or a dry
# run previewed); 1 anything refused, failed, timed out or on another image.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

APPLY="${REPO_ROOT}/kubernetes/apply.sh"
GRAPH_NAMESPACE="swarm-system"
GRAPH_DEPLOYMENT="swarm-graph"
GRAPH_CONTAINER="graph"
GRAPH_SELECTOR="app.kubernetes.io/name=swarm-graph"
MANIFEST_OUTPUT="swarm_graph_manifest"

CONFIRM=0
GET_CREDENTIALS=0
TF_INIT=0
CONTEXT=""
CLUSTER=""
# 600 s (10 min): a surge pod on Autopilot may wait minutes for a node before
# it pulls the image, twice over for two replicas. Long enough for that, short
# enough that a crash-looping service fails inside the release step's timeout.
TIMEOUT=600

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm)          CONFIRM=1; shift ;;
    --get-credentials)  GET_CREDENTIALS=1; shift ;;
    --tf-init)          TF_INIT=1; shift ;;
    --context)          [[ $# -ge 2 ]] || die "--context takes a kubectl context"
                        CONTEXT="$2"; shift 2 ;;
    --context=*)        CONTEXT="${1#*=}"; shift ;;
    --cluster)          [[ $# -ge 2 ]] || die "--cluster takes a cluster name"
                        CLUSTER="$2"; shift 2 ;;
    --cluster=*)        CLUSTER="${1#*=}"; shift ;;
    --timeout)          [[ $# -ge 2 ]] || die "--timeout takes whole seconds"
                        TIMEOUT="$2"; shift 2 ;;
    --timeout=*)        TIMEOUT="${1#*=}"; shift ;;
    -h|--help)          awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0"; exit 0 ;;
    *)                  die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "${TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "--timeout takes whole seconds, not '${TIMEOUT}'"

# notice TITLE MESSAGE -- a GitHub notice on a runner, an info line elsewhere.
notice() {
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    printf '::notice title=%s::%s\n' "$1" "$2"
  fi
  info "$2"
}

# refuse_cluster WHAT REF -- die unless the cluster REF names (the last `_`
# segment of gcloud's gke_<project>_<location>_<cluster>, else REF whole) is a
# `swarm-` cluster matching nothing cluster-shaped on the shared deny-list. The
# cluster segment, not the whole string: our project id contains the other
# team's cluster name (apply.sh's rule).
refuse_cluster() {
  local what="$1" ref="$2" cluster foreign
  cluster="${ref##*_}"
  for foreign in ${SHARED_DENY_LIST[@]+"${SHARED_DENY_LIST[@]}"}; do
    case "${foreign}" in *@*|default) continue ;; esac
    case "${cluster}" in
      *"${foreign}"*) die "${what} names cluster '${cluster}', which matches '${foreign}' on the shared
       deny-list: it belongs to another team. Refusing; nothing was applied." ;;
    esac
  done
  case "${cluster}" in
    swarm-*) ;;
    *) die "${what} names cluster '${cluster}', which does not start with 'swarm-'; refusing." ;;
  esac
}

# A flag is judged before terraform is asked for anything.
[[ -z "${CLUSTER}" ]] || refuse_cluster "--cluster '${CLUSTER}'" "${CLUSTER}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/${GRAPH_DEPLOYMENT}-roll.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
TF_DIR="$(tf_root)"

step "${GRAPH_DEPLOYMENT} rollout (${ENVIRONMENT}, ${PROJECT_ID})"

# --- what terraform rendered ---------------------------------------------------
if [[ "${TF_INIT}" -eq 1 ]]; then
  # The state the release's apply just wrote: the same backend and prefix the
  # apply planned against. Read-only from here on: only `output` is run.
  if ! tf -chdir="${TF_DIR}" init -input=false -reconfigure \
      -backend-config="bucket=${TF_STATE_BUCKET}" \
      -backend-config="prefix=${TF_STATE_PREFIX}" >"${WORK}/init.out" 2>&1; then
    redact <"${WORK}/init.out" | tail -n 20 | sed 's/^/     /' >&2
    die "terraform init against gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX} failed (above); nothing was applied."
  fi
  info "terraform  initialised against gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX}"
fi

if ! tf -chdir="${TF_DIR}" output -raw "${MANIFEST_OUTPUT}" >"${WORK}/graph.yaml" 2>"${WORK}/graph.err" \
    || [[ ! -s "${WORK}/graph.yaml" ]]; then
  # Null (not served) or a state terraform could not read: the unnamed
  # -json read tells them apart. Terraform leaves a null output out of state,
  # so "not served" is an object without the key, or with a null or empty
  # value; a read that fails, or is not an object, is not "off".
  if tf -chdir="${TF_DIR}" output -json >"${WORK}/outputs.json" 2>>"${WORK}/graph.err" \
      && jq -e --arg k "${MANIFEST_OUTPUT}" \
        'type == "object" and ((.[$k].value // null) == null or .[$k].value == "")' \
        "${WORK}/outputs.json" >/dev/null 2>&1; then
    notice "${GRAPH_DEPLOYMENT} not rolled" "graph_service.serve is off for ${ENVIRONMENT}: terraform rendered no ${MANIFEST_OUTPUT}, so there is no graph service to roll."
    exit 0
  fi
  redact <"${WORK}/graph.err" | sed -n '1,5p' | sed 's/^/     /' >&2
  die "could not read terraform's ${MANIFEST_OUTPUT} output (above); run with --tf-init, or 'make tf-init'."
fi
# The manifest's one image line (kubernetes/swarm-system/swarm-graph.yaml).
IMAGE="$(sed -n 's/^[[:space:]]*image:[[:space:]]*//p' "${WORK}/graph.yaml" | head -n 1 | tr -d "\"'")"
[[ -n "${IMAGE}" ]] || die "the rendered swarm-graph manifest names no image; nothing was applied."
case "${IMAGE}" in
  *@sha256:*) DIGEST="${IMAGE##*@}" ;;
  # terraform's image_refs validation only admits digests, so this is a
  # manifest from somewhere else; with no digest nothing could be proven.
  *) die "the swarm-graph image '${IMAGE}' is not pinned by digest; its pods could not be proven. Nothing was applied." ;;
esac
info "swarm-graph image  ${IMAGE}"

# --- the cluster, and only ours --------------------------------------------------
if [[ -z "${CLUSTER}" ]]; then
  CLUSTER="$(tf -chdir="${TF_DIR}" output -raw gke_cluster_name 2>/dev/null || true)"
  CLUSTER="${CLUSTER:-${GKE_CLUSTER}}"
fi
refuse_cluster "cluster '${CLUSTER}'" "${CLUSTER}"
EXPECTED_ENTRY="gke_${PROJECT_ID}_${GKE_LOCATION}_${CLUSTER}"

if [[ "${GET_CREDENTIALS}" -eq 1 ]]; then
  # The runner has gcloud but no kubeconfig entry. The cluster was checked
  # above; get-credentials writes gcloud's own context name, EXPECTED_ENTRY.
  if ! command -v gke-gcloud-auth-plugin >/dev/null 2>&1; then
    gcloud components install gke-gcloud-auth-plugin --quiet
  fi
  gcloud container clusters get-credentials "${CLUSTER}" \
    --project "${PROJECT_ID}" --location "${GKE_LOCATION}"
  [[ -n "${CONTEXT}" ]] || CONTEXT="${EXPECTED_ENTRY}"
fi

KUBECTL="$(kubectl_bin)"
info "kubectl  ${KUBECTL}"
if [[ -z "${CONTEXT}" ]]; then
  CONTEXT="$("${KUBECTL}" config current-context 2>/dev/null || true)"
fi
[[ -n "${CONTEXT}" ]] || die "no kubectl context is selected; run scripts/configure-kubectl.sh"
case "${CONTEXT}" in
  gke_*) refuse_cluster "context '${CONTEXT}'" "${CONTEXT}" ;;
esac
ENTRY="$("${KUBECTL}" config view -o \
  "jsonpath={.contexts[?(@.name==\"${CONTEXT}\")].context.cluster}" 2>/dev/null || true)"
[[ -n "${ENTRY}" ]] || die "could not resolve which cluster context '${CONTEXT}' points at; refusing."
refuse_cluster "context '${CONTEXT}' -> kubeconfig cluster '${ENTRY}'" "${ENTRY}"
[[ "${ENTRY}" == "${EXPECTED_ENTRY}" ]] \
  || die "context '${CONTEXT}' points at kubeconfig cluster '${ENTRY}', not '${EXPECTED_ENTRY}'; refusing."
info "context  ${CONTEXT} -> ${ENTRY}"
KARGS=(--context "${CONTEXT}")

if "${KUBECTL}" "${KARGS[@]}" get deployment "${GRAPH_DEPLOYMENT}" --namespace "${GRAPH_NAMESPACE}" -o json \
    >"${WORK}/before.json" 2>/dev/null; then
  BEFORE="$(jq -r --arg c "${GRAPH_CONTAINER}" \
    '[.spec.template.spec.containers[]? | select(.name == $c) | .image] | first // "none"' "${WORK}/before.json")"
else
  BEFORE="none (no Deployment yet)"
fi
info "swarm-graph now    ${BEFORE}"

# --- apply: apply.sh's, with its manifest and cluster checks -----------------
APPLY_ARGS=(--swarm-graph --context "${CONTEXT}" --cluster "${CLUSTER}")
[[ "${CONFIRM}" -eq 1 ]] && APPLY_ARGS+=(--confirm)
"${APPLY}" "${APPLY_ARGS[@]}" \
  || die "kubernetes/apply.sh --swarm-graph failed (above); swarm-graph was not rolled."

if [[ "${CONFIRM}" -ne 1 ]]; then
  ok "dry run only. Re-run with --confirm to apply, wait for the rollout and check the digest."
  exit 0
fi

# --- wait for the roll ---------------------------------------------------------
info "waiting up to ${TIMEOUT}s for deploy/${GRAPH_DEPLOYMENT} to roll"
if ! "${KUBECTL}" "${KARGS[@]}" rollout status "deploy/${GRAPH_DEPLOYMENT}" \
    -n "${GRAPH_NAMESPACE}" --timeout="${TIMEOUT}s" 2>&1 | redact; then
  "${KUBECTL}" "${KARGS[@]}" get pods --namespace "${GRAPH_NAMESPACE}" -l "${GRAPH_SELECTOR}" -o wide 2>&1 \
    | redact | sed 's/^/     /' >&2 || true
  die "swarm-graph's rollout did not finish within ${TIMEOUT}s (pods above); it may still run the old image."
fi

# --- what the Deployment now runs ----------------------------------------------
"${KUBECTL}" "${KARGS[@]}" get deployment "${GRAPH_DEPLOYMENT}" --namespace "${GRAPH_NAMESPACE}" -o json \
  >"${WORK}/after.json" || die "could not read deploy/${GRAPH_DEPLOYMENT} back after the rollout."
SPEC_IMAGE="$(jq -r --arg c "${GRAPH_CONTAINER}" \
  '[.spec.template.spec.containers[]? | select(.name == $c) | .image] | first // ""' "${WORK}/after.json")"
[[ "${SPEC_IMAGE}" == "${IMAGE}" ]] \
  || die "deploy/${GRAPH_DEPLOYMENT} names '${SPEC_IMAGE:-nothing}', not the rendered '${IMAGE}'."

# Pods already being deleted are the old ReplicaSet's, finishing their grace
# period after `rollout status` returned; they are not what the service runs.
"${KUBECTL}" "${KARGS[@]}" get pods --namespace "${GRAPH_NAMESPACE}" -l "${GRAPH_SELECTOR}" -o json \
  >"${WORK}/pods.json" || die "could not list swarm-graph's pods after the rollout."
jq -r --arg c "${GRAPH_CONTAINER}" \
  '.items[] | select(.metadata.deletionTimestamp == null)
   | [.metadata.name, ([.status.containerStatuses[]? | select(.name == $c) | .imageID] | first // "")] | @tsv' \
  "${WORK}/pods.json" >"${WORK}/pods.tsv" || die "kubectl's pod list is not JSON."

TAB="$(printf '\t')"
visited=0
STALE=()
while IFS="${TAB}" read -r pod image_id; do
  [[ -n "${pod}" ]] || continue
  visited=$((visited + 1))
  if [[ "${image_id}" != *"@${DIGEST}" ]]; then
    err "${pod} runs '${image_id:-nothing reported}', not ${DIGEST}"
    STALE+=("${pod}")
  fi
done <"${WORK}/pods.tsv"
[[ "${visited}" -gt 0 ]] || die "no swarm-graph pod (${GRAPH_SELECTOR}) after the rollout; the service is not running."
if [[ "${#STALE[@]}" -gt 0 ]]; then
  die "${#STALE[@]} of ${visited} swarm-graph pod(s) run another image than ${IMAGE}: ${STALE[*]}"
fi
ok "swarm-graph rolled: ${visited} pod(s) run ${DIGEST} (was ${BEFORE})"
