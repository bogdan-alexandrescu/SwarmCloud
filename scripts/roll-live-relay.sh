#!/usr/bin/env bash
# Roll the live-browser relay (StatefulSet swarm-system/swarm-live on the swarm's
# Autopilot cluster) to the image this environment's terraform rendered, wait
# for the rollout, and prove the relay's pods run that digest.
#
#   scripts/roll-live-relay.sh                                     # dry run: apply.sh's preview
#   scripts/roll-live-relay.sh --confirm                           # apply, parity, rollout, digest
#   scripts/roll-live-relay.sh --tf-init --get-credentials --confirm   # what the release runs
#   scripts/roll-live-relay.sh --confirm --timeout 900             # a slower Autopilot scale-up
#   scripts/roll-live-relay.sh --confirm --force                   # roll even when the digest matches
#   scripts/roll-live-relay.sh --confirm --defer-timeout 0         # roll now, watched or not
#   scripts/roll-live-relay.sh --component egress ...              # swarm-egress (scripts/roll-egress.sh)
#
# ONLY A NEW IMAGE ROLLS IT, AND NEVER UNDER SOMEONE'S HANDS (lane RELAY-ROLL,
# part of #1030). MEASURED 2026-10-12/14: the relay restarted 40 times in
# ~48 h, because every release ran this, and the relay is ONE replica holding
# every session's rendezvous in memory: each roll dropped every live session
# and every person's control (tunnel close code 1012). So:
#   * the digest the release rendered is compared with the one the relay's
#     pods run, and both are printed; equal (and the StatefulSet already names
#     that image) is "not rolled", exit 0, with nothing applied. --force rolls
#     anyway. NOTE: kubernetes/apply.sh --swarm-system also applies
#     swarm-system's ingress policies; a release that does not roll the relay
#     does not re-apply them either -- run it by hand for a policy-only change;
#   * a roll waits while anyone is watching or in control: the relay's own
#     counts (`/healthz`, read inside the pod with `kubectl exec ... python -m
#     swarm_live.status`, so authenticated by the Kubernetes API and widening
#     no NetworkPolicy) are polled every --defer-poll seconds for up to
#     --defer-timeout (480 s), then the roll goes ahead anyway and says why.
#     480 s, not 600: with the 600 s rollout below it stays inside the
#     release step's 20-minute timeout. An unreadable count is said, and does
#     not block a roll;
#   * the relay itself, on SIGTERM, tells every gate and viewer to reconnect
#     now (close 1012) and both rejoin within about a second of the new pod.
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
# TWO COMPONENTS, ONE IMPLEMENTATION (lane EGRESS-BUILD, 2026-10-14).
# `--component relay` (the default) rolls swarm-live. `--component egress`
# rolls swarm-egress, the browser egress forwarder: StatefulSet
# swarm-system/swarm-egress, container `egress`, rendered by
# terraform/infra/egress.tf as `swarm_egress_manifest`, switched by
# enable_browser_egress and read back from the `browser_egress` output.
# scripts/roll-egress.sh is that second invocation by name. Both go through
# the same apply.sh --swarm-system, which applies the relay, swarm-system's
# policies and then swarm-egress in one stream, and through the same cluster
# checks below, so the shared-project deny-list is applied here once,
# whichever StatefulSet is rolled. The `case "${COMPONENT}"` table below is
# everything that differs.
#
# A COMPONENT THAT IS OFF IS NOT A FAILURE. While enable_live_browser is false
# for an environment, terraform's `live_relay.enabled` is false and its
# manifest is null; there is nothing to roll, and the script says so (a GitHub
# notice) and exits 0. Enabled with no manifest -- no swarm-live digest in
# image_refs -- is a failure: that is a relay the release cannot roll. The
# same holds for swarm-egress with enable_browser_egress, `browser_egress.enabled`
# and a swarm-egress digest.
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

COMPONENT="relay"
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
FORCE=0
DEFER_TIMEOUT=480
DEFER_POLL=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --component)        [[ $# -ge 2 ]] || die "--component takes relay or egress"
                        COMPONENT="$2"; shift 2 ;;
    --component=*)      COMPONENT="${1#*=}"; shift ;;
    --confirm)          CONFIRM=1; shift ;;
    --get-credentials)  GET_CREDENTIALS=1; shift ;;
    --tf-init)          TF_INIT=1; shift ;;
    --context)          CONTEXT="$2"; shift 2 ;;
    --context=*)        CONTEXT="${1#*=}"; shift ;;
    --cluster)          CLUSTER="$2"; shift 2 ;;
    --cluster=*)        CLUSTER="${1#*=}"; shift ;;
    --timeout)          TIMEOUT="$2"; shift 2 ;;
    --timeout=*)        TIMEOUT="${1#*=}"; shift ;;
    --force)            FORCE=1; shift ;;
    --defer-timeout)    DEFER_TIMEOUT="$2"; shift 2 ;;
    --defer-timeout=*)  DEFER_TIMEOUT="${1#*=}"; shift ;;
    --defer-poll)       DEFER_POLL="$2"; shift 2 ;;
    --defer-poll=*)     DEFER_POLL="${1#*=}"; shift ;;
    -h|--help)          awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0"; exit 0 ;;
    *)                  die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "${TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "--timeout takes whole seconds, not '${TIMEOUT}'"
[[ "${DEFER_TIMEOUT}" =~ ^[0-9]+$ ]] || die "--defer-timeout takes whole seconds, not '${DEFER_TIMEOUT}'"
[[ "${DEFER_POLL}" =~ ^[1-9][0-9]*$ ]] || die "--defer-poll takes whole seconds, not '${DEFER_POLL}'"

# Everything that differs between the two StatefulSets this rolls:
#   WHAT             how messages name it
#   SWITCH           the terraform variable that turns it on
#   STATUS_OUTPUT    the terraform output whose `.enabled` says it is on
#   MANIFEST_OUTPUT  the terraform output holding its rendered manifest
#   IMAGE_NAME       its image_refs key; TF_FILE the file that renders it
#   RELAY_STATEFULSET, RELAY_CONTAINER, RELAY_SELECTOR
#                    its objects, as kubernetes/swarm-system/<name>.yaml names them
case "${COMPONENT}" in
  relay)
    WHAT="relay"
    SWITCH="enable_live_browser"
    STATUS_OUTPUT="live_relay"
    MANIFEST_OUTPUT="swarm_live_manifest"
    IMAGE_NAME="swarm-live"
    TF_FILE="terraform/infra/live.tf"
    RELAY_STATEFULSET="swarm-live"
    RELAY_CONTAINER="relay"
    RELAY_SELECTOR="app.kubernetes.io/name=swarm-live"
    ;;
  egress)
    WHAT="swarm-egress"
    SWITCH="enable_browser_egress"
    STATUS_OUTPUT="browser_egress"
    MANIFEST_OUTPUT="swarm_egress_manifest"
    IMAGE_NAME="swarm-egress"
    TF_FILE="terraform/infra/egress.tf"
    RELAY_STATEFULSET="swarm-egress"
    RELAY_CONTAINER="egress"
    RELAY_SELECTOR="app.kubernetes.io/name=swarm-egress"
    ;;
  *) die "--component takes relay or egress, not '${COMPONENT}'" ;;
esac

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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/${RELAY_STATEFULSET}-roll.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
TF_DIR="$(tf_root)"

step "${RELAY_STATEFULSET} rollout (${ENVIRONMENT}, ${PROJECT_ID})"

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

if ! tf -chdir="${TF_DIR}" output -json "${STATUS_OUTPUT}" >"${WORK}/status.json" 2>"${WORK}/status.err"; then
  redact <"${WORK}/status.err" | sed -n '1,5p' | sed 's/^/     /' >&2
  die "could not read terraform's ${STATUS_OUTPUT} output (above); run with --tf-init, or 'make tf-init'."
fi
# Explicit comparisons: `.enabled // true` reads false as absent (CLAUDE.md).
if jq -e '.enabled == false' "${WORK}/status.json" >/dev/null 2>&1; then
  notice "${RELAY_STATEFULSET} not rolled" "${SWITCH} is off for ${ENVIRONMENT}: there is no ${WHAT} to roll."
  exit 0
fi
jq -e '.enabled == true' "${WORK}/status.json" >/dev/null 2>&1 \
  || die "terraform's ${STATUS_OUTPUT} output says neither enabled nor disabled; nothing was applied."

if ! tf -chdir="${TF_DIR}" output -raw "${MANIFEST_OUTPUT}" >"${WORK}/relay.yaml" 2>"${WORK}/relay.err" \
    || [[ ! -s "${WORK}/relay.yaml" ]]; then
  redact <"${WORK}/relay.err" | sed -n '1,5p' | sed 's/^/     /' >&2
  die "the ${WHAT} is enabled but terraform rendered no ${MANIFEST_OUTPUT}: image_refs has no
       ${IMAGE_NAME} digest (${TF_FILE}). This release cannot roll the ${WHAT}."
fi
# The manifest's one image line (kubernetes/swarm-system/<statefulset>.yaml).
IMAGE="$(sed -n 's/^[[:space:]]*image:[[:space:]]*//p' "${WORK}/relay.yaml" | head -n 1 | tr -d "\"'")"
[[ -n "${IMAGE}" ]] || die "the rendered ${WHAT} manifest names no image; nothing was applied."
DIGEST=""
case "${IMAGE}" in
  *@sha256:*) DIGEST="${IMAGE##*@}" ;;
  *) warn "the ${WHAT} image '${IMAGE}' is not pinned by digest; the pods are compared by image name only." ;;
esac
info "${WHAT} image  ${IMAGE}"

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
info "${WHAT} now    ${BEFORE}"

# relay_pods FILE -- the relay's pods as `name<TAB>imageID` lines in FILE.
relay_pods() {
  "${KUBECTL}" "${KARGS[@]}" get pods --namespace "${RELAY_NAMESPACE}" -l "${RELAY_SELECTOR}" -o json \
    >"${WORK}/pods-now.json" 2>/dev/null || return 1
  jq -r --arg c "${RELAY_CONTAINER}" \
    '.items[] | [.metadata.name, ([.status.containerStatuses[]? | select(.name == $c) | .imageID] | first // "")] | @tsv' \
    "${WORK}/pods-now.json" >"$1" 2>/dev/null
}

# --- only a new image rolls the relay -------------------------------------------
TAB="$(printf '\t')"
RUNNING_DIGESTS=()
RELAY_PODS=()
if [[ "${BEFORE}" != none* ]] && relay_pods "${WORK}/pods-before.tsv"; then
  while IFS="${TAB}" read -r pod image_id; do
    [[ -n "${pod}" ]] || continue
    RELAY_PODS+=("${pod}")
    running="none"
    case "${image_id}" in *@sha256:*) running="${image_id##*@}" ;; esac
    RUNNING_DIGESTS+=("${running}")
  done <"${WORK}/pods-before.tsv"
fi
info "rendered digest  ${DIGEST:-none (image not pinned)}"
if [[ "${#RUNNING_DIGESTS[@]}" -gt 0 ]]; then
  info "running digest   ${RUNNING_DIGESTS[*]}  (${#RELAY_PODS[@]} pod(s))"
else
  info "running digest   none (no relay pod)"
fi
SAME=0
if [[ -n "${DIGEST}" && "${BEFORE}" == "${IMAGE}" && "${#RELAY_PODS[@]}" -gt 0 ]]; then
  SAME=1
  for running in "${RUNNING_DIGESTS[@]}"; do
    [[ "${running}" == "${DIGEST}" ]] || SAME=0
  done
fi
if [[ "${SAME}" -eq 1 && "${FORCE}" -ne 1 ]]; then
  notice "${WHAT} not rolled" "not rolled: the ${WHAT} already runs ${DIGEST} on ${#RELAY_PODS[@]} pod(s), the digest this release rendered (--force rolls anyway)."
  exit 0
fi
[[ "${SAME}" -eq 1 ]] && warn "the relay already runs ${DIGEST}; rolling anyway (--force)."

# --- never while someone is watching or in control ------------------------------
# relay_counts -- "<viewers> <controlled>" from the first relay pod, or nothing.
relay_counts() {
  "${KUBECTL}" "${KARGS[@]}" exec "${RELAY_PODS[0]}" --namespace "${RELAY_NAMESPACE}" -c "${RELAY_CONTAINER}" \
    -- python -m swarm_live.status >"${WORK}/status.json" 2>"${WORK}/status.err" || return 1
  jq -r 'if (.viewers | type) == "number" and (.controlled | type) == "number"
         then "\(.viewers) \(.controlled)" else empty end' "${WORK}/status.json" 2>/dev/null
}
# Only the relay holds people's sessions; swarm-egress has no viewer counts
# (its pod has no swarm_live module), so it rolls without this wait.
if [[ "${COMPONENT}" == "relay" && "${CONFIRM}" -eq 1 && "${#RELAY_PODS[@]}" -gt 0 ]]; then
  waited=0
  while true; do
    counts="$(relay_counts || true)"
    if [[ -z "${counts}" ]]; then
      warn "could not read the relay's counts ($(redact <"${WORK}/status.err" | head -n 1)); rolling without waiting."
      break
    fi
    viewers="${counts% *}"
    controlled="${counts#* }"
    if [[ "${viewers}" -eq 0 && "${controlled}" -eq 0 ]]; then
      info "nobody is watching or in control; rolling"
      break
    fi
    if [[ "${waited}" -ge "${DEFER_TIMEOUT}" ]]; then
      notice "Live relay rolled while in use" "waited ${waited}s (--defer-timeout ${DEFER_TIMEOUT}) and ${viewers} viewer(s) are still connected, ${controlled} session(s) in a person's control; rolling anyway. They are told to reconnect (close 1012) and rejoin within about a second of the new relay."
      break
    fi
    info "deferring the roll: ${viewers} viewer(s) connected, ${controlled} session(s) in a person's control (waited ${waited}s of ${DEFER_TIMEOUT}s)"
    sleep "${DEFER_POLL}"
    waited=$((waited + DEFER_POLL))
  done
fi

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
  || die "kubernetes/apply.sh --swarm-system failed (above); the ${WHAT} was not rolled."

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
  die "the ${WHAT}'s rollout did not finish within ${TIMEOUT}s (pods above); it may still run the old image."
fi

# --- what the StatefulSet now runs ---------------------------------------------
"${KUBECTL}" "${KARGS[@]}" get statefulset "${RELAY_STATEFULSET}" --namespace "${RELAY_NAMESPACE}" -o json \
  >"${WORK}/after.json" || die "could not read statefulset/${RELAY_STATEFULSET} back after the rollout."
SPEC_IMAGE="$(jq -r --arg c "${RELAY_CONTAINER}" \
  '[.spec.template.spec.containers[]? | select(.name == $c) | .image] | first // ""' "${WORK}/after.json")"
[[ "${SPEC_IMAGE}" == "${IMAGE}" ]] \
  || die "statefulset/${RELAY_STATEFULSET} names '${SPEC_IMAGE:-nothing}', not the rendered '${IMAGE}'."

"${KUBECTL}" "${KARGS[@]}" get pods --namespace "${RELAY_NAMESPACE}" -l "${RELAY_SELECTOR}" -o json \
  >"${WORK}/pods.json" || die "could not list the ${WHAT}'s pods after the rollout."
jq -r --arg c "${RELAY_CONTAINER}" \
  '.items[] | [.metadata.name, ([.status.containerStatuses[]? | select(.name == $c) | .imageID] | first // "")] | @tsv' \
  "${WORK}/pods.json" >"${WORK}/pods.tsv" || die "kubectl's pod list is not JSON."

visited=0
STALE=()
while IFS="${TAB}" read -r pod image_id; do
  [[ -n "${pod}" ]] || continue
  visited=$((visited + 1))
  if [[ -n "${DIGEST}" && "${image_id}" != *"@${DIGEST}" ]]; then
    err "${pod} runs '${image_id:-nothing reported}', not ${DIGEST}"
    STALE+=("${pod}")
  fi
done <"${WORK}/pods.tsv"
[[ "${visited}" -gt 0 ]] || die "no ${WHAT} pod (${RELAY_SELECTOR}) after the rollout; the ${WHAT} is not running."
if [[ "${#STALE[@]}" -gt 0 ]]; then
  die "${#STALE[@]} of ${visited} ${WHAT} pod(s) still run another image than ${IMAGE}: ${STALE[*]}"
fi
ok "the ${WHAT} rolled: ${visited} pod(s) run ${DIGEST:-${IMAGE}} (was ${BEFORE})"
