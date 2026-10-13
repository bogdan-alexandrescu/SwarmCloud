#!/usr/bin/env bash
# startup-measure: where a GKE start-up's seconds go, cold vs warm, per
# runner_profile and resource class, and whether the browser pods would fit a
# 4 vCPU / 8 GiB class. Read-only. (START-MEASURE; owner request 2026-10-13.)
#
#   scripts/startup-measure.sh [--since 7d] [--cold-seconds 10]
#                              [--samples 4] [--interval 15] [--no-pods]
#                              [--class browser] [--candidate standard]
#                              [--max-tasks 2000] [--save DIR] [--json]
#
# WHY. docs/browser-startup-speed.md §9: the owner decided on 2026-10-13 that
# warm placeholder slots (decision 3, an amendment to invariant 1) and a
# smaller `browser` class (decision 6, a contract request) are decided only
# after they are MEASURED. §1 measured one start (38 s node wait, 42 s pull);
# this measures every start in the window.
#
# WHAT IT READS.
#   * Through the API only, never Firestore: GET /v1/tasks newest first until a
#     task is older than --since, then every page of each task's events. The
#     `startup_progress` steps there are the reconciler's (the pod's four) and
#     the worker's (the browser's three), contract request 74. The caller's
#     tenant, or SWARM_API_TENANT's.
#   * `kubectl top pods -A --containers -l swarm-resource-class=<class>`,
#     --samples times, --interval seconds apart, on the swarm's own cluster
#     (`swarm-autopilot`). Through `kc` (kubectl_bin: never an old client off
#     PATH), and only after assert_kube_context has refused any context that is
#     not ours -- agents-staging, the other team's cluster in the same project,
#     is on common.sh's deny-list and is never pointed at. `top` reads the
#     metrics API; nothing is created, patched or deleted. --no-pods skips it.
#
# WHAT IT PRINTS. Per runner_profile and class, for all, cold and warm starts:
# n, the cold share (node wait over --cold-seconds), and p50/p95 of
#   node wait  dispatched        -> pod_scheduled
#   pull       image_pulling     -> image_pulled
#   browser    container_started -> first_frame
#   total      dispatched        -> first_frame
# then a verdict: what a warm slot would have saved, and whether the pods'
# sampled peak fits the candidate class (`standard`, 4 vCPU / 8 GiB, read from
# the frozen catalogue) with 20 % headroom. Every number is made by
# scripts/startup_measure.py, which reads what this fetched and is unit-tested
# offline (tests/unit/scripts/test_startup_measure.py). This script only
# fetches.
#
# --save DIR keeps what was read (events.json, top.txt) so the numbers recorded
# in the doc can be re-derived: `scripts/startup_measure.py report --top
# DIR/top.txt <DIR/events.json`.
#
# EXIT. 0 measured; 1 pods were sampled and none was running (NOT_MEASURED);
# 2 no start-up was read, or a request failed. Reading nothing is never a
# result (CLAUDE.md, rule zero).
#
# Everything printed goes through `redact`.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SINCE="7d"
MAX_TASKS=2000
SAMPLES=4
INTERVAL=15
RC_CLASS="browser"
SAVE_DIR=""
REPORT_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)        SINCE="${2:?--since needs a duration, e.g. 7d}"; shift 2 ;;
    --cold-seconds) REPORT_ARGS+=(--cold-seconds "${2:?--cold-seconds needs a number}"); shift 2 ;;
    --samples)      SAMPLES="${2:?--samples needs a number}"; shift 2 ;;
    --interval)     INTERVAL="${2:?--interval needs seconds}"; shift 2 ;;
    --no-pods)      SAMPLES=0; shift ;;
    --class)        RC_CLASS="${2:?--class needs a resource class}"; shift 2 ;;
    --candidate)    REPORT_ARGS+=(--candidate "${2:?--candidate needs a resource class}"); shift 2 ;;
    --max-tasks)    MAX_TASKS="${2:?--max-tasks needs a number}"; shift 2 ;;
    --save)         SAVE_DIR="${2:?--save needs a directory}"; shift 2 ;;
    --json)         REPORT_ARGS+=(--json); shift ;;
    -h|--help)      sed -n '2,52p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "${MAX_TASKS}" =~ ^[0-9]+$ ]] || die "--max-tasks must be a number"
[[ "${SAMPLES}" =~ ^[0-9]+$ ]] || die "--samples must be a number"
[[ "${INTERVAL}" =~ ^[0-9]+$ ]] || die "--interval must be a number of seconds"
# A label value: it goes into a selector, never into a shell word unquoted.
[[ "${RC_CLASS}" =~ ^[a-z0-9-]+$ ]] || die "--class must be a resource class name"
REPORT_ARGS+=(--since "${SINCE}" --class "${RC_CLASS}")

load_env
require_cmd curl

# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a PY <<<"$(swarm_python)"
HELPER="${REPO_ROOT}/scripts/startup_measure.py"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/startup-measure.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM
PAGE="${WORK}/page.json"
IDS="${WORK}/ids.txt"
STREAM="${WORK}/events.json"
TOP="${WORK}/top.txt"
: >"${IDS}"
: >"${STREAM}"

# One read. Redirected to a file rather than captured with $(...): a command
# substitution is a SUBSHELL, so API_STATUS would never reach this shell and
# every response would read as a success.
fetch() {
  local path="$1"
  if ! api_request GET "${API_PREFIX}${path}" >"${PAGE}"; then
    err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}"
    redact <"${PAGE}" >&2 || true
    exit 2
  fi
}

# --- the tasks inside the window, newest first -----------------------------
step "Tasks created in the last ${SINCE}"
cursor=""
listed=0
while :; do
  query="/tasks?view=summary&limit=200"
  [[ -z "${cursor}" ]] || query="${query}&page_token=${cursor}"
  fetch "${query}"
  # The page itself goes into the stream: it is where each task's
  # runner_profile and resource_class come from.
  cat "${PAGE}" >>"${STREAM}"
  printf '\n' >>"${STREAM}"
  cursor=""
  done_listing=0
  while read -r kind value; do
    case "${kind}" in
      task)  printf '%s\n' "${value}" >>"${IDS}"; listed=$((listed + 1)) ;;
      next)  cursor="${value}" ;;
      older) done_listing=1 ;;
    esac
  done < <("${PY[@]}" "${HELPER}" window --since "${SINCE}" <"${PAGE}")
  if [[ "${done_listing}" -eq 1 || -z "${cursor}" ]]; then
    break
  fi
  if [[ "${listed}" -ge "${MAX_TASKS}" ]]; then
    warn "stopped listing at --max-tasks ${MAX_TASKS}; older tasks in the window were not read"
    break
  fi
done
info "${listed} task(s) listed"

# --- each task's events, every page ------------------------------------------
step "Reading startup_progress steps"
visited=0
with_steps=0
while read -r task_id; do
  [[ -n "${task_id}" ]] || continue
  [[ "${visited}" -lt "${MAX_TASKS}" ]] || break
  visited=$((visited + 1))
  marked=0
  events_cursor=""
  while :; do
    query="/tasks/${task_id}/events?limit=200"
    [[ -z "${events_cursor}" ]] || query="${query}&page_token=${events_cursor}"
    fetch "${query}"
    # Every page that holds a step or a dispatch: a step's segment starts at
    # `dispatched`, which can sit on an earlier page than the step.
    if grep -qE '"(startup_progress|dispatched)"' "${PAGE}"; then
      grep -q '"startup_progress"' "${PAGE}" && marked=1
      cat "${PAGE}" >>"${STREAM}"
      printf '\n' >>"${STREAM}"
    fi
    events_cursor="$("${PY[@]}" "${HELPER}" next-token <"${PAGE}")"
    [[ -n "${events_cursor}" ]] || break
  done
  [[ "${marked}" -eq 0 ]] || with_steps=$((with_steps + 1))
done <"${IDS}"
info "${visited} task(s) visited, ${with_steps} with a startup_progress step"

# --- the pods' real use --------------------------------------------------------
TOP_ARGS=()
if [[ "${SAMPLES}" -gt 0 ]]; then
  step "Sampling ${RC_CLASS} pods on ${GKE_CLUSTER}: ${SAMPLES} sample(s), ${INTERVAL} s apart"
  # Refuses any context that is not the swarm's own cluster, including one
  # renamed to look like it but ending in a deny-listed name (agents-staging).
  assert_kube_context
  CONTEXT="$(kube_current_context)"
  : >"${TOP}"
  read_samples=0
  i=0
  while [[ "${i}" -lt "${SAMPLES}" ]]; do
    i=$((i + 1))
    printf '# sample %s\n' "$(iso_now)" >>"${TOP}"
    if kc --context "${CONTEXT}" top pods --all-namespaces --containers --no-headers \
         -l "swarm-resource-class=${RC_CLASS}" >>"${TOP}" 2>"${WORK}/top.err"; then
      read_samples=$((read_samples + 1))
    else
      # No pod of the class running is "No resources found" on some clients
      # and an error on others; either way the sample holds no pod, which
      # the helper reports as NOT_MEASURED rather than as a fit.
      warn "sample ${i}: the metrics read did not answer: $(redact <"${WORK}/top.err" | tail -n 1)"
    fi
    if [[ "${i}" -lt "${SAMPLES}" ]]; then sleep "${INTERVAL}"; fi
  done
  info "${read_samples} of ${SAMPLES} sample(s) answered"
  TOP_ARGS=(--top "${TOP}")
fi

if [[ -n "${SAVE_DIR}" ]]; then
  mkdir -p "${SAVE_DIR}"
  cp "${STREAM}" "${SAVE_DIR}/events.json"
  [[ ! -f "${TOP}" ]] || cp "${TOP}" "${SAVE_DIR}/top.txt"
  info "kept what was read in ${SAVE_DIR}"
fi

# --- the numbers ---------------------------------------------------------------
hr
status=0
"${PY[@]}" "${HELPER}" report ${REPORT_ARGS[@]+"${REPORT_ARGS[@]}"} ${TOP_ARGS[@]+"${TOP_ARGS[@]}"} \
  <"${STREAM}" 2>&1 | redact || status=$?
exit "${status}"
