#!/usr/bin/env bash
# The recording-runner spike ("lane 0" of docs/design/recording-runner.md §10):
# measures §5.2 option C -- a Docker daemon inside a GKE Sandbox pod in a tenant
# namespace -- with the three checks the design names (§5.2 C, risks 1-3):
#
#   1. ADMISSION: the cluster admits the sandboxed pod (runtime class gvisor,
#      root and the daemon's capabilities inside the sandbox, a per-pod disk
#      claim, a Guaranteed 16-core / 64 GiB shape) and runs it;
#   2. DAEMON: `dockerd` starts inside the sandbox and runs a container;
#   3. SPEED: a fixed workload's wall time in the sandbox against the same
#      workload outside it, same image and shape. The pass bar is the design's:
#      the sandboxed run keeps a margin under the suites' 600 s limit.
#
# The runbook is docs/runbooks/recording-runner-spike.md: what each check
# proves, the impact inventory, the preconditions, rollback.
#
# MODES
#   --preflight (the default)  reads only, plus a server-side dry run of both
#                              pods, which the API server persists nothing for.
#   --apply                    the preflight, then, after the namespace is TYPED
#                              at a terminal, creates the pods and runs the
#                              checks; at the end deletes what it created after
#                              the run id is typed.
#   --cleanup --run-id ID      deletes one earlier run's pods and claim, after
#                              the run id is typed, and proves nothing is left.
#
# WHAT IT TOUCHES: only the namespace given, which must be a tenant namespace
# (swarm-tenant-*) not on the deployment's never-touch list, through a kubectl
# context common.sh accepts as the swarm's own. It creates two pods (and, through
# the sandboxed pod, one disk claim), each labelled
# swarm-spike=recording-runner,swarm-spike-run=<run id>, and deletes by exactly
# that selector in exactly that namespace: never by name, never --all, never
# across namespaces. It changes nothing else: not the namespace, its quota, its
# LimitRange, its Pod Security level or its policies.
#
# Nothing it prints or writes can hold a credential: every line from kubectl
# goes through common.sh's `redact`, and the result table is built from parsed
# numbers and this script's own words. The pods mount no API token and run as
# the namespace's disarmed `default` account, which has no Google identity.
#
# SWARM_ASSUME_YES is IGNORED: both confirmations are typed at a terminal.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# common.sh's confirm() skips its prompt when SWARM_ASSUME_YES is set; creating
# 16-core pods and deleting a disk claim may not be unattended.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: the spike creates and deletes only after a typed confirmation"
  unset SWARM_ASSUME_YES
fi

usage() {
  cat >&2 <<'USAGE'
Usage:
  scripts/recording-runner-spike.sh --namespace swarm-tenant-<id> \
      --daemon-image <registry/path>@sha256:<digest> --storage-class <class> \
      [--preflight | --apply] [--run-id rrs-<id>] [--margin-pct <n>] [--out <file>] [--allow-prod]
  scripts/recording-runner-spike.sh --cleanup --namespace swarm-tenant-<id> --run-id rrs-<id>

  --namespace      the tenant namespace to run in (required)
  --daemon-image   an image with dockerd, the docker CLI and busybox, BY DIGEST
  --storage-class  the class of the image-store claim: the one the tenant quota
                   allows (dispatch.py's CLAIMED_CACHE_STORAGE_CLASS)
  --preflight      the default: read-only checks and a server-side dry run
  --apply          create the pods and run the three checks (typed confirmation)
  --cleanup        delete one run's pods and claim (typed confirmation)
  --run-id         the run's label value; generated for --apply, required for --cleanup
  --margin-pct     the margin under the 600 s suite limit (default 20: 480 s)
  --out            the result file (default build/recording-runner-spike/<run id>.md)
  --allow-prod     required when ENVIRONMENT is prod
See docs/runbooks/recording-runner-spike.md.
USAGE
}

MODE="preflight"
NAMESPACE=""
DAEMON_IMAGE=""
STORAGE_CLASS=""
RUN_ID=""
MARGIN_PCT=20
OUT=""
ALLOW_PROD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)     [[ $# -ge 2 ]] || die "--namespace needs a value"; NAMESPACE="$2"; shift 2 ;;
    --namespace=*)   NAMESPACE="${1#--namespace=}"; shift ;;
    --daemon-image)  [[ $# -ge 2 ]] || die "--daemon-image needs a value"; DAEMON_IMAGE="$2"; shift 2 ;;
    --daemon-image=*) DAEMON_IMAGE="${1#--daemon-image=}"; shift ;;
    --storage-class) [[ $# -ge 2 ]] || die "--storage-class needs a value"; STORAGE_CLASS="$2"; shift 2 ;;
    --storage-class=*) STORAGE_CLASS="${1#--storage-class=}"; shift ;;
    --run-id)        [[ $# -ge 2 ]] || die "--run-id needs a value"; RUN_ID="$2"; shift 2 ;;
    --run-id=*)      RUN_ID="${1#--run-id=}"; shift ;;
    --margin-pct)    [[ $# -ge 2 ]] || die "--margin-pct needs a value"; MARGIN_PCT="$2"; shift 2 ;;
    --margin-pct=*)  MARGIN_PCT="${1#--margin-pct=}"; shift ;;
    --out)           [[ $# -ge 2 ]] || die "--out needs a value"; OUT="$2"; shift 2 ;;
    --out=*)         OUT="${1#--out=}"; shift ;;
    --preflight)     MODE="preflight"; shift ;;
    --apply)         MODE="apply"; shift ;;
    --cleanup)       MODE="cleanup"; shift ;;
    --allow-prod)    ALLOW_PROD=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done

# --- what this run may touch: refused before any kubectl call ---------------

# The suites' per-suite limit the design's pass bar is stated against (§5.2 C,
# risk 3, from the issue: "each under a fixed 600-second limit").
SUITE_LIMIT_SECONDS=600
SPIKE_KEY="swarm-spike"
SPIKE_VALUE="recording-runner"
RUN_KEY="swarm-spike-run"
KDIR="${REPO_ROOT}/kubernetes/recording-spike"

# A tenant namespace, spelled as an explicit character list (in bash 3.2 a range
# follows the locale's collation, as scripts/offboard-tenant.sh says).
case "${NAMESPACE}" in
  '') usage; die "--namespace is required" ;;
  swarm-tenant-*) ;;
  *) die "'${NAMESPACE}' is not a tenant namespace (swarm-tenant-<id>); the spike runs only in one" ;;
esac
case "${NAMESPACE#swarm-tenant-}" in
  ''|-*|*[!abcdefghijklmnopqrstuvwxyz0123456789-]*)
    die "'${NAMESPACE}' is not a tenant namespace: lowercase letters, digits and '-' after swarm-tenant-" ;;
esac
[[ "${#NAMESPACE}" -le 63 ]] || die "'${NAMESPACE}' is longer than a namespace name can be"
if is_shared_resource "${NAMESPACE}" || is_shared_resource "${NAMESPACE#swarm-tenant-}"; then
  die "'${NAMESPACE}' is on the never-touch list; refusing"
fi

if [[ "${MODE}" == "apply" && -z "${RUN_ID}" ]]; then
  RUN_ID="rrs-$(date -u +%Y%m%d%H%M%S)"
fi
if [[ "${MODE}" == "preflight" && -z "${RUN_ID}" ]]; then
  RUN_ID="rrs-preflight"
fi
case "${RUN_ID}" in
  '') usage; die "--run-id is required with --cleanup: the run whose objects are deleted" ;;
  rrs-*) ;;
  *) die "'${RUN_ID}' is not a spike run id (rrs-<lowercase letters and digits>)" ;;
esac
case "${RUN_ID#rrs-}" in
  ''|*[!abcdefghijklmnopqrstuvwxyz0123456789-]*)
    die "'${RUN_ID}' is not a spike run id (rrs-<lowercase letters and digits>)" ;;
esac
[[ "${#RUN_ID}" -le 32 ]] || die "'${RUN_ID}' is longer than 32 characters"
SELECTOR="${SPIKE_KEY}=${SPIKE_VALUE},${RUN_KEY}=${RUN_ID}"

if is_production && [[ "${ALLOW_PROD}" -ne 1 ]]; then
  die "ENVIRONMENT is ${ENVIRONMENT}: the design runs the spike on a dev cluster; pass --allow-prod to run it here"
fi

if [[ "${MODE}" != "cleanup" ]]; then
  [[ "${DAEMON_IMAGE}" =~ ^[a-z0-9][a-z0-9./_:-]*@sha256:[0-9a-f]{64}$ ]] \
    || die "--daemon-image must name an image by digest (<registry/path>@sha256:<64 hex>), so both pods run the same bytes"
  [[ "${STORAGE_CLASS}" =~ ^[a-z0-9][a-z0-9.-]{0,252}$ ]] \
    || die "--storage-class must name a StorageClass (lowercase letters, digits, '.' and '-')"
  if is_shared_resource "${STORAGE_CLASS}"; then
    die "storage class '${STORAGE_CLASS}' is on the never-touch list; refusing"
  fi
  [[ "${MARGIN_PCT}" =~ ^[0-9]{1,2}$ ]] || die "--margin-pct is a whole number from 0 to 99"
else
  MARGIN_PCT=20
fi
PASS_BAR_SECONDS=$(( SUITE_LIMIT_SECONDS * (100 - MARGIN_PCT) / 100 ))

if [[ -z "${OUT}" ]]; then
  OUT="${BUILD_DIR}/recording-runner-spike/${RUN_ID}.md"
fi

# --- kubectl, only through the swarm's own context and only in NAMESPACE ------

KUBE="$(kubectl_bin)"
assert_kube_context
# Pin the asserted context: every later call passes --context, so a
# use-context in another terminal cannot redirect this run to another cluster.
CONTEXT="$(kube_current_context)"
[[ -n "${CONTEXT}" ]] || die "kubectl has no current context"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/recording-spike.XXXXXX")"
CREATED=0
DELETED=0
# shellcheck disable=SC2317,SC2329  # invoked by the EXIT trap below
on_exit() {
  rm -rf -- "${WORK}"
  if [[ "${CREATED}" -eq 1 && "${DELETED}" -ne 1 ]]; then
    warn "objects of run ${RUN_ID} may be left in ${NAMESPACE}. Delete them with:"
    warn "  scripts/recording-runner-spike.sh --cleanup --namespace ${NAMESPACE} --run-id ${RUN_ID}"
  fi
}
trap on_exit EXIT

# k ARGS...: kubectl in NAMESPACE, stdout to WORK/out, stderr to WORK/err.
# Returns kubectl's status. Never prints either stream itself.
k() {
  "${KUBE}" --context "${CONTEXT}" --namespace "${NAMESPACE}" "$@" >"${WORK}/out" 2>"${WORK}/err"
}

# A short, redacted excerpt of the last kubectl error, on one line.
last_error() {
  redact <"${WORK}/err" | tr '\n' ' ' | sed 's/[|]/\//g' | cut -c1-300
}

ROWS="${WORK}/rows"
: >"${ROWS}"
# row CHECK RESULT DETAIL: one line of the result table. RESULT is PASS, FAIL,
# INFO or NOT RUN; DETAIL is this script's own words and numbers, or a
# redacted excerpt.
row() {
  local detail
  detail="$(printf '%s' "$3" | tr '\n' ' ' | sed 's/[|]/\//g')"
  printf '| %s | %s | %s |\n' "$1" "$2" "${detail}" >>"${ROWS}"
  info "$1: $2 -- ${detail}"
}

write_results() {
  mkdir -p "$(dirname -- "${OUT}")"
  {
    printf '# Recording-runner spike, run %s\n\n' "${RUN_ID}"
    printf -- '- mode: %s\n- when (UTC): %s\n- environment: %s\n- namespace: %s\n' \
      "${MODE}" "$(iso_now)" "${ENVIRONMENT:-}" "${NAMESPACE}"
    printf -- '- daemon image: %s\n- storage class: %s\n' "${DAEMON_IMAGE:-n/a}" "${STORAGE_CLASS:-n/a}"
    printf -- '- pass bar (check 3): sandboxed wall time <= %s s (%s s suite limit, %s%% margin)\n\n' \
      "${PASS_BAR_SECONDS}" "${SUITE_LIMIT_SECONDS}" "${MARGIN_PCT}"
    printf '| Check | Result | Detail |\n|---|---|---|\n'
    cat "${ROWS}"
  } | redact >"${OUT}"
  step "Result"
  cat "${OUT}" >&2
  info "written to ${OUT}"
}

# render TEMPLATE TENANT -> WORK/<name>, every placeholder filled, or stop.
render() {
  local src="$1" tenant="$2" dst
  dst="${WORK}/$(basename -- "${src}")"
  sed -e "s|__NAMESPACE__|${NAMESPACE}|g" \
      -e "s|__TENANT_ID__|${tenant}|g" \
      -e "s|__RUN_ID__|${RUN_ID}|g" \
      -e "s|__DAEMON_IMAGE__|${DAEMON_IMAGE}|g" \
      -e "s|__STORAGE_CLASS__|${STORAGE_CLASS}|g" \
      "${src}" >"${dst}"
  if grep -n '__[A-Z][A-Z_]*__' "${dst}" >/dev/null; then
    die "$(basename -- "${src}") has a placeholder this script does not fill; refusing to create it"
  fi
  printf '%s' "${dst}"
}

# --- the delete: one run's objects, by its label, in NAMESPACE --------------

delete_run() {
  # In a subshell, so a confirmation that does not match (or no terminal) is a
  # result row and the table is still written, rather than an exit here.
  if ! ( confirm "Delete run ${RUN_ID}'s pods and disk claim (label ${SELECTOR}) in ${NAMESPACE}?" "${RUN_ID}" ); then
    row "cleanup: delete" "NOT RUN" "the run id was not typed; nothing was deleted"
    return 0
  fi
  # By the run's label and nothing else: never a name, never --all, never
  # --all-namespaces. The claim of a generic ephemeral volume is also deleted
  # with its pod; naming pvc here as well means a claim left by a pod deleted
  # some other way is found too.
  if k delete pod,pvc --selector "${SELECTOR}" --ignore-not-found --wait=true --timeout=10m; then
    row "cleanup: delete" "PASS" "deleted by ${SELECTOR}: $(redact <"${WORK}/out" | wc -l | tr -d ' ') object(s)"
  else
    row "cleanup: delete" "FAIL" "$(last_error)"
  fi
  if k get pod,pvc --selector "${SELECTOR}" --output name; then
    if [[ -s "${WORK}/out" ]]; then
      row "cleanup: nothing left" "FAIL" "still present: $(redact <"${WORK}/out" | tr '\n' ' ')"
    else
      DELETED=1
      row "cleanup: nothing left" "PASS" "no pod or claim carries ${SELECTOR}"
    fi
  else
    row "cleanup: nothing left" "FAIL" "could not list: $(last_error)"
  fi
}

if [[ "${MODE}" == "cleanup" ]]; then
  step "Cleanup of run ${RUN_ID} in ${NAMESPACE}"
  if k get pod,pvc --selector "${SELECTOR}" --output name; then
    info "objects of this run: $(redact <"${WORK}/out" | tr '\n' ' ')"
  fi
  CREATED=1
  delete_run
  write_results
  [[ "${DELETED}" -eq 1 ]] || exit 1
  exit 0
fi

# --- preflight: reads, and a dry run that persists nothing --------------------

step "Preflight (read-only) in ${NAMESPACE}"
row "context" "PASS" "kubectl context is the swarm cluster (common.sh assert_kube_context)"

if ! k get namespace "${NAMESPACE}" --output json; then
  row "namespace exists" "FAIL" "$(last_error)"
  write_results
  exit 1
fi
TENANT_ID="$(jq -r '.metadata.labels["swarm-tenant"] // ""' "${WORK}/out")"
PSA_ENFORCE="$(jq -r '.metadata.labels["pod-security.kubernetes.io/enforce"] // "none"' "${WORK}/out")"
case "${TENANT_ID}" in
  ''|-*|*[!abcdefghijklmnopqrstuvwxyz0123456789-]*)
    row "namespace exists" "FAIL" "the namespace carries no valid swarm-tenant label: not a tenant namespace"
    write_results
    exit 1 ;;
esac
row "namespace exists" "PASS" "tenant label present; Pod Security enforce level: ${PSA_ENFORCE}"

if k get networkpolicy swarm-default-deny swarm-allow-worker-egress --output name; then
  row "tenant network policies" "PASS" "swarm-default-deny and swarm-allow-worker-egress are applied"
else
  row "tenant network policies" "FAIL" "the namespace lacks a tenant policy: $(last_error)"
fi

if k get runtimeclass gvisor --output name; then
  row "runtime class gvisor" "PASS" "the cluster offers it"
else
  row "runtime class gvisor" "FAIL" "$(last_error)"
fi

if k get storageclass "${STORAGE_CLASS}" --output name; then
  row "storage class" "PASS" "${STORAGE_CLASS} exists"
else
  row "storage class" "FAIL" "$(last_error)"
fi

if k get limitrange --output json; then
  row "namespace LimitRange" "INFO" "$(jq -r '[.items[].spec.limits[] | select(.type == "Pod" or .type == "Container") | "\(.type) max cpu \(.max.cpu // "-") memory \(.max.memory // "-")"] | join("; ")' "${WORK}/out")"
fi
if k get resourcequota --output json; then
  row "namespace quota" "INFO" "$(jq -r '[.items[] | "requests.cpu used \(.status.used["requests.cpu"] // "-") of \(.status.hard["requests.cpu"] // "-"), requests.memory used \(.status.used["requests.memory"] // "-") of \(.status.hard["requests.memory"] // "-"), requests.storage used \(.status.used["requests.storage"] // "-") of \(.status.hard["requests.storage"] // "-")"] | join("; ")' "${WORK}/out")"
fi

if k get pod,pvc --selector "${SPIKE_KEY}=${SPIKE_VALUE}" --output name && [[ -s "${WORK}/out" ]]; then
  row "earlier spike objects" "INFO" "present, from an earlier run: $(redact <"${WORK}/out" | tr '\n' ' ')"
fi

SANDBOXED="$(render "${KDIR}/sandboxed-pod.yaml" "${TENANT_ID}")"
CONTROL="$(render "${KDIR}/control-pod.yaml" "${TENANT_ID}")"
SANDBOXED_POD="recording-spike-${RUN_ID}-sandboxed"
CONTROL_POD="recording-spike-${RUN_ID}-control"

# Check 1, first half: would the API server admit the pod? A server-side dry
# run passes through every admission step (Pod Security, LimitRange, quota,
# the platform's ValidatingAdmissionPolicies, Autopilot's own) and persists
# nothing.
ADMITTED=0
if k create --dry-run=server --filename "${SANDBOXED}" --output name; then
  ADMITTED=1
  row "1. admission (dry run)" "PASS" "the API server admits the sandboxed pod"
else
  row "1. admission (dry run)" "FAIL" "refused: $(last_error)"
fi
CONTROL_ADMITTED=0
if k create --dry-run=server --filename "${CONTROL}" --output name; then
  CONTROL_ADMITTED=1
  row "control pod (dry run)" "PASS" "the API server admits the control pod"
else
  row "control pod (dry run)" "FAIL" "refused: $(last_error)"
fi

if [[ "${MODE}" == "preflight" ]]; then
  row "2. daemon" "NOT RUN" "preflight creates nothing; run with --apply"
  row "3. speed" "NOT RUN" "preflight creates nothing; run with --apply"
  write_results
  exit 0
fi

if [[ "${ADMITTED}" -ne 1 ]]; then
  row "2. daemon" "NOT RUN" "check 1 refused the pod, so option C is closed in this namespace as it stands"
  row "3. speed" "NOT RUN" "check 1 refused the pod"
  write_results
  exit 1
fi

# --- apply: the typed confirmation, then the only creates ---------------------

step "Apply"
warn "This creates two 16-core / 64 GiB pods and one 50 GiB disk claim in ${NAMESPACE} (run ${RUN_ID})."
confirm "Create the spike's objects in ${NAMESPACE}?" "${NAMESPACE}"

# wait_ready POD -> 0 when Ready within 15 minutes (Autopilot provisions a node
# for a 16-core pod), else 1 with WORK/err holding why.
wait_ready() {
  k wait --for=condition=Ready "pod/$1" --timeout=900s
}

# exec_in POD ARGS...: run ARGS in the pod's spike container.
exec_in() {
  local pod="$1"; shift
  k exec "${pod}" --container spike -- "$@"
}

# run_workload POD -> WORK/<pod>.workload; prints "<wall seconds> <rc>".
run_workload() {
  local pod="$1" log="${WORK}/$1.workload" wall rc
  "${KUBE}" --context "${CONTEXT}" --namespace "${NAMESPACE}" exec --stdin "${pod}" --container spike -- sh -s \
    <"${KDIR}/workload.sh" >"${log}" 2>"${WORK}/err" || true
  wall="$(sed -n 's/^SPIKE_WALL_SECONDS=\([0-9][0-9]*\)$/\1/p' "${log}" | tail -n 1)"
  rc="$(sed -n 's/^SPIKE_WORKLOAD_RC=\([0-9][0-9]*\)$/\1/p' "${log}" | tail -n 1)"
  printf '%s %s' "${wall:-none}" "${rc:-none}"
}

phases_of() {
  sed -n 's/^SPIKE_PHASE name=\([a-z_]*\) seconds=\([0-9]*\) rc=\([0-9]*\)$/\1 \2s rc \3/p' "${WORK}/$1.workload" \
    | tr '\n' ',' | sed 's/,$//'
}

# Check 1, second half: created, scheduled, Ready, sandboxed, Guaranteed.
CREATED=1
if ! k create --filename "${SANDBOXED}" --output name; then
  row "1. admission (create)" "FAIL" "refused at create: $(last_error)"
  row "2. daemon" "NOT RUN" "the pod was not created"
  row "3. speed" "NOT RUN" "the pod was not created"
  delete_run
  write_results
  exit 1
fi
if ! wait_ready "${SANDBOXED_POD}"; then
  k get events --field-selector "involvedObject.name=${SANDBOXED_POD}" --output custom-columns=REASON:.reason,MESSAGE:.message || true
  row "1. admission (create)" "FAIL" "created but not Ready in 15 min: $(redact <"${WORK}/out" | tail -n 5 | tr '\n' ' ' | cut -c1-400)"
  row "2. daemon" "NOT RUN" "the pod did not start"
  row "3. speed" "NOT RUN" "the pod did not start"
  delete_run
  write_results
  exit 1
fi
k get pod "${SANDBOXED_POD}" --output json
RUNTIME="$(jq -r '.spec.runtimeClassName // "none"' "${WORK}/out")"
QOS="$(jq -r '.status.qosClass // "unknown"' "${WORK}/out")"
KERNEL="unknown"
if exec_in "${SANDBOXED_POD}" uname -r; then
  KERNEL="$(redact <"${WORK}/out" | head -n 1 | cut -c1-80)"
fi
if [[ "${RUNTIME}" == "gvisor" && "${QOS}" == "Guaranteed" ]]; then
  row "1. admission (create)" "PASS" "Ready; runtime class ${RUNTIME}; QoS ${QOS}; kernel ${KERNEL}"
else
  row "1. admission (create)" "FAIL" "Ready, but runtime class ${RUNTIME} and QoS ${QOS} (wanted gvisor, Guaranteed); kernel ${KERNEL}"
fi

# Check 2: the daemon answers within two minutes, then runs a container.
DOCKER_UP=0
tries=0
while [[ "${tries}" -lt 24 ]]; do
  if exec_in "${SANDBOXED_POD}" docker info --format '{{.ServerVersion}} {{.Driver}}'; then
    DOCKER_UP=1
    break
  fi
  tries=$((tries + 1))
  sleep 5
done
if [[ "${DOCKER_UP}" -eq 1 ]]; then
  DAEMON_INFO="$(redact <"${WORK}/out" | head -n 1 | cut -c1-80)"
  # The container's image is made in the sandbox from the pod's own /bin and
  # /lib, not pulled: the inner daemon holds no registry credential (the pod
  # has no Google identity), and a pull would test the registry, not the
  # daemon. It still writes the image store on the disk claim and starts a
  # container through the daemon's runtime, which is what check 2 asks.
  # shellcheck disable=SC2016  # expanded by the pod's shell, not this one
  if exec_in "${SANDBOXED_POD}" sh -c 'tar -C / -cf - bin lib | docker import - recording-spike-probe:1 >/dev/null && docker run --rm --network none recording-spike-probe:1 /bin/sh -c "echo recording-spike-ok"' \
      && grep -qx 'recording-spike-ok' "${WORK}/out"; then
    row "2. daemon" "PASS" "dockerd up (version and storage driver: ${DAEMON_INFO}); imported an image and ran a container that printed its marker"
  else
    row "2. daemon" "FAIL" "dockerd up (${DAEMON_INFO}) but docker run failed: $(last_error)"
  fi
else
  exec_in "${SANDBOXED_POD}" tail -n 15 /run/dockerd.log || true
  row "2. daemon" "FAIL" "dockerd did not answer in 2 min; its log ends: $(redact <"${WORK}/out" | tr '\n' ' ' | cut -c1-600)"
fi

# Check 3: the same fixed workload in the sandbox and outside it.
read -r S_WALL S_RC <<<"$(run_workload "${SANDBOXED_POD}")"
row "3a. workload, sandboxed" "INFO" "wall ${S_WALL} s, exit ${S_RC}; $(phases_of "${SANDBOXED_POD}")"

C_WALL="none"
C_RC="none"
if [[ "${CONTROL_ADMITTED}" -eq 1 ]] && k create --filename "${CONTROL}" --output name && wait_ready "${CONTROL_POD}"; then
  read -r C_WALL C_RC <<<"$(run_workload "${CONTROL_POD}")"
  row "3b. workload, control" "INFO" "wall ${C_WALL} s, exit ${C_RC}; $(phases_of "${CONTROL_POD}")"
else
  row "3b. workload, control" "FAIL" "the control pod did not run: $(last_error)"
fi

if [[ "${S_WALL}" =~ ^[0-9]+$ && "${S_RC}" == "0" ]]; then
  RATIO="n/a"
  if [[ "${C_WALL}" =~ ^[0-9]+$ && "${C_RC}" == "0" && "${C_WALL}" -gt 0 ]]; then
    RATIO="$(awk -v s="${S_WALL}" -v c="${C_WALL}" 'BEGIN { printf "%.2f", s / c }')"
  fi
  if [[ "${S_WALL}" -le "${PASS_BAR_SECONDS}" && "${RATIO}" != "n/a" ]]; then
    row "3. speed" "PASS" "sandboxed ${S_WALL} s <= ${PASS_BAR_SECONDS} s; control ${C_WALL} s; sandboxed/control ${RATIO}"
  elif [[ "${RATIO}" == "n/a" ]]; then
    row "3. speed" "FAIL" "sandboxed ${S_WALL} s, but no control time to compare it with"
  else
    row "3. speed" "FAIL" "sandboxed ${S_WALL} s > ${PASS_BAR_SECONDS} s; control ${C_WALL} s; sandboxed/control ${RATIO}"
  fi
else
  row "3. speed" "FAIL" "the sandboxed workload did not complete (wall ${S_WALL}, exit ${S_RC})"
fi

# --- cleanup: only this run's objects, after the run id is typed --------------

step "Cleanup"
delete_run
write_results
grep -q '| FAIL |' "${OUT}" && exit 1
exit 0
