#!/usr/bin/env bash
# Roll the live-browser relay (StatefulSet swarm-system/swarm-live on the swarm's
# Autopilot cluster) to the image this environment's terraform rendered, wait
# for the rollout, and prove the relay's pods run that digest.
#
#   scripts/roll-live-relay.sh                                     # dry run: apply.sh's preview
#   scripts/roll-live-relay.sh --confirm                           # apply, parity, rollout, digest
#   scripts/roll-live-relay.sh --tf-init --get-credentials --confirm   # what the release runs
#   scripts/roll-live-relay.sh --confirm --timeout 900             # a slower Autopilot scale-up
#
# WHY THIS EXISTS (lane OPS-RELAY, part of #1030). The relay's manifest is
# rendered by terraform (terraform/infra/live.tf, output `swarm_live_manifest`,
# with the release's swarm-live digest), but no release applied it: only
# `kubernetes/apply.sh --swarm-system --confirm`, by hand. Measured 2026-10-12
# (visual QA pass 5): twice that day the relay ran a pre-change image until the
# operator applied it, and the console's origin/device stayed "Not reported".
# The release now runs this after its digests are verified, so every release
# rolls the relay to the image it built.
#
# THE APPLY IS apply.sh's, NOT A SECOND COPY OF IT. `kubernetes/apply.sh
# --swarm-system` already reads the relay manifest from terraform, refuses one
# that reaches outside swarm-system, carries RBAC or an unfilled placeholder,
# holds terraform's swarm-api range equal to the range read from swarm-api's
# service, applies relay and policies in one stream, and then runs the network
# parity check (kubernetes/network_parity.py --live-ingress) against the live
# policies. Its --confirm path asks nothing, so it needs no non-interactive
# flag. What this script adds is what apply.sh never did: wait for the
# StatefulSet to roll, and read back what the pods actually run.
#
# A RELAY THAT IS OFF IS NOT A FAILURE. While enable_live_browser is false for
# an environment, terraform's `live_relay.enabled` is false and its manifest is
# null; there is nothing to roll, and the script says so (a GitHub notice) and
# exits 0. Enabled with no manifest -- no swarm-live digest in image_refs -- is
# a failure: that is a relay the release cannot roll.
#
# SAFETY, in apply.sh's terms and with common.sh's deny-list:
#   * kubectl from `kubectl_bin` (an old client silently drops fields);
#   * the cluster -- from --cluster, terraform's `gke_cluster_name` or
#     GKE_CLUSTER -- must start with `swarm-` and match nothing cluster-shaped
#     on SHARED_DENY_LIST, checked BEFORE get-credentials and before apply.sh;
#   * the context's kubeconfig cluster entry must be exactly
#     gke_<PROJECT_ID>_<GKE_LOCATION>_<cluster>, so the cluster apply.sh writes
#     to and the cluster this script waits on are one, and never agents-staging.
#     apply.sh repeats all of these checks itself.
#
# Exit: 0 the relay runs the rendered digest (or the relay is off, or a dry run
# previewed); 1 anything refused, failed, timed out or still on another image.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

APPLY="${REPO_ROOT}/kubernetes/apply.sh"
RELAY_NAMESPACE="swarm-system"
RELAY_STATEFULSET="swarm-live"
# The container in kubernetes/swarm-system/swarm-live.yaml that runs the image.
RELAY_CONTAINER="relay"
RELAY_SELECTOR="app.kubernetes.io/name=swarm-live"

CONFIRM=0
GET_CREDENTIALS=0
TF_INIT=0
CONTEXT=""
CLUSTER=""
# 600 s: one replica, so a roll is delete-then-create, and on Autopilot the new
# pod may wait for a node to be provisioned (minutes) before it pulls the
# image. Long enough for that, short enough that a crash-looping relay fails
# the release inside the job instead of at the job's own timeout.
TIMEOUT=600

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm)          CONFIRM=1; shift ;;
    --get-credentials)  GET_CREDENTIALS=1; shift ;;
    --tf-init)          TF_INIT=1; shift ;;
    --context)          CONTEXT="$2"; shift 2 ;;
    --context=*)        CONTEXT="${1#*=}"; shift ;;
    --cluster)          CLUSTER="$2"; shift 2 ;;
    --cluster=*)        CLUSTER="${1#*=}"; shift ;;
    --timeout)          TIMEOUT="$2"; shift 2 ;;
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
  for foreign in "${SHARED_DENY_LIST[@]}"; do
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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-live-roll.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
TF_DIR="$(tf_root)"

step "Live relay rollout (${ENVIRONMENT}, ${PROJECT_ID})"

# --- what terraform rendered ---------------------------------------------------
if [[ "${TF_INIT}" -eq 1 ]]; then
  # The state the release's apply just wrote: the same backend and prefix the
  # apply planned against (.github/actions/release-namespaces does the same).
  # Read-only from here on: only `output` is run.
  if ! tf -chdir="${TF_DIR}" init -input=false -reconfigure \
      -backend-config="bucket=${TF_STATE_BUCKET}" \
      -backend-config="prefix=${TF_STATE_PREFIX}" >"${WORK}/init.out" 2>&1; then
    redact <"${WORK}/init.out" | tail -n 20 | sed 's/^/     /' >&2
    die "terraform init against gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX} failed (above); nothing was applied."
  fi
  info "terraform  initialised against gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX}"
fi

if ! tf -chdir="${TF_DIR}" output -json live_relay >"${WORK}/live_relay.json" 2>"${WORK}/live_relay.err"; then
  redact <"${WORK}/live_relay.err" | sed -n '1,5p' | sed 's/^/     /' >&2
  die "could not read terraform's live_relay output (above); run with --tf-init, or 'make tf-init'."
fi
# Explicit comparisons: `.enabled // true` reads false as absent (CLAUDE.md).
if jq -e '.enabled == false' "${WORK}/live_relay.json" >/dev/null 2>&1; then
  notice "Live relay not rolled" "enable_live_browser is off for ${ENVIRONMENT}: there is no relay to roll."
  exit 0
fi
jq -e '.enabled == true' "${WORK}/live_relay.json" >/dev/null 2>&1 \
  || die "terraform's live_relay output says neither enabled nor disabled; nothing was applied."

if ! tf -chdir="${TF_DIR}" output -raw swarm_live_manifest >"${WORK}/relay.yaml" 2>"${WORK}/relay.err" \
    || [[ ! -s "${WORK}/relay.yaml" ]]; then
  redact <"${WORK}/relay.err" | sed -n '1,5p' | sed 's/^/     /' >&2
  die "the live relay is enabled but terraform rendered no swarm_live_manifest: image_refs has no
       swarm-live digest (terraform/infra/live.tf). This release cannot roll the relay."
fi
# The manifest's one image line (kubernetes/swarm-system/swarm-live.yaml).
IMAGE="$(sed -n 's/^[[:space:]]*image:[[:space:]]*//p' "${WORK}/relay.yaml" | head -n 1 | tr -d "\"'")"
[[ -n "${IMAGE}" ]] || die "the rendered relay manifest names no image; nothing was applied."
DIGEST=""
case "${IMAGE}" in
  *@sha256:*) DIGEST="${IMAGE##*@}" ;;
  *) warn "the relay image '${IMAGE}' is not pinned by digest; the pods are compared by image name only." ;;
esac
info "relay image  ${IMAGE}"

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

if "${KUBECTL}" "${KARGS[@]}" get statefulset "${RELAY_STATEFULSET}" --namespace "${RELAY_NAMESPACE}" -o json \
    >"${WORK}/before.json" 2>/dev/null; then
  BEFORE="$(jq -r --arg c "${RELAY_CONTAINER}" \
    '[.spec.template.spec.containers[]? | select(.name == $c) | .image] | first // "none"' "${WORK}/before.json")"
else
  BEFORE="none (no StatefulSet yet)"
fi
info "relay now    ${BEFORE}"

# --- apply: apply.sh's, with its checks and its parity comparison ---------------
# render.py and network_parity.py run on ${PYTHON_BIN:-python3}; the project's
# synced interpreter when there is one, never a bare python3 by accident.
if [[ -z "${PYTHON_BIN:-}" && -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
  export PYTHON_BIN
fi
APPLY_ARGS=(--swarm-system --context "${CONTEXT}" --cluster "${CLUSTER}")
[[ "${CONFIRM}" -eq 1 ]] && APPLY_ARGS+=(--confirm)
"${APPLY}" "${APPLY_ARGS[@]}" \
  || die "kubernetes/apply.sh --swarm-system failed (above); the relay was not rolled."

if [[ "${CONFIRM}" -ne 1 ]]; then
  ok "dry run only. Re-run with --confirm to apply, wait for the rollout and check the digest."
  exit 0
fi

# --- wait for the roll ---------------------------------------------------------
info "waiting up to ${TIMEOUT}s for statefulset/${RELAY_STATEFULSET} to roll"
if ! "${KUBECTL}" "${KARGS[@]}" rollout status "statefulset/${RELAY_STATEFULSET}" \
    --namespace "${RELAY_NAMESPACE}" --timeout="${TIMEOUT}s" 2>&1 | redact; then
  "${KUBECTL}" "${KARGS[@]}" get pods --namespace "${RELAY_NAMESPACE}" -l "${RELAY_SELECTOR}" -o wide 2>&1 \
    | redact | sed 's/^/     /' >&2 || true
  die "the relay's rollout did not finish within ${TIMEOUT}s (pods above); it may still run the old image."
fi

# --- what the relay now runs ---------------------------------------------------
"${KUBECTL}" "${KARGS[@]}" get statefulset "${RELAY_STATEFULSET}" --namespace "${RELAY_NAMESPACE}" -o json \
  >"${WORK}/after.json" || die "could not read statefulset/${RELAY_STATEFULSET} back after the rollout."
SPEC_IMAGE="$(jq -r --arg c "${RELAY_CONTAINER}" \
  '[.spec.template.spec.containers[]? | select(.name == $c) | .image] | first // ""' "${WORK}/after.json")"
[[ "${SPEC_IMAGE}" == "${IMAGE}" ]] \
  || die "statefulset/${RELAY_STATEFULSET} names '${SPEC_IMAGE:-nothing}', not the rendered '${IMAGE}'."

"${KUBECTL}" "${KARGS[@]}" get pods --namespace "${RELAY_NAMESPACE}" -l "${RELAY_SELECTOR}" -o json \
  >"${WORK}/pods.json" || die "could not list the relay's pods after the rollout."
jq -r --arg c "${RELAY_CONTAINER}" \
  '.items[] | [.metadata.name, ([.status.containerStatuses[]? | select(.name == $c) | .imageID] | first // "")] | @tsv' \
  "${WORK}/pods.json" >"${WORK}/pods.tsv" || die "kubectl's pod list is not JSON."

visited=0
STALE=()
TAB="$(printf '\t')"
while IFS="${TAB}" read -r pod image_id; do
  [[ -n "${pod}" ]] || continue
  visited=$((visited + 1))
  if [[ -n "${DIGEST}" && "${image_id}" != *"@${DIGEST}" ]]; then
    err "${pod} runs '${image_id:-nothing reported}', not ${DIGEST}"
    STALE+=("${pod}")
  fi
done <"${WORK}/pods.tsv"
[[ "${visited}" -gt 0 ]] || die "no relay pod (${RELAY_SELECTOR}) after the rollout; the relay is not running."
if [[ "${#STALE[@]}" -gt 0 ]]; then
  die "${#STALE[@]} of ${visited} relay pod(s) still run another image than ${IMAGE}: ${STALE[*]}"
fi
ok "the relay rolled: ${visited} pod(s) run ${DIGEST:-${IMAGE}} (was ${BEFORE})"
