#!/usr/bin/env bash
# Sustained-load test: submit many tasks and measure what the platform does.
#
# Reports admission latency (submitted -> LEASED) and end-to-end latency
# (submitted -> terminal) as p50/p90/p99, plus throughput. These are the numbers
# that tell you whether the scheduler's bounded drain loop keeps up, and they are
# the numbers docs/scaling.md is calibrated against.
#
# Usage:
#   scripts/load-test.sh --count 200 --rate 10
#   scripts/load-test.sh --count 50 --profile mock --timeout 900

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="load"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

COUNT=100
RATE=10
PROFILE="mock"
TIMEOUT=900
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)   COUNT="$2"; shift 2 ;;
    --rate)    RATE="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Load test: ${COUNT} tasks at ~${RATE}/s on ${PROFILE}"
require_platform
BASE_LEASES="$(active_leases)"
info "baseline unreleased leases: ${BASE_LEASES}"

RESULTS="${BUILD_DIR}/load-test-${ENVIRONMENT}.jsonl"
: >"${RESULTS}"
TASK_IDS=()
SUBMIT_FAILURES=0

cleanup() {
  if [[ "${KEEP}" -eq 0 && "${#TASK_IDS[@]}" -gt 0 ]]; then
    cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
  fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
t_case "Submit ${COUNT} tasks"
SUBMIT_START="$(date -u +%s)"
INTERVAL_US=$(( 1000000 / (RATE > 0 ? RATE : 1) ))
for i in $(seq 1 "${COUNT}"); do
  started_ns="$(date -u +%s)"
  if id="$(submit_task "${PROFILE}" \
        "$(jq -nc --argjson i "${i}" '{message:"load", index:$i}')" \
        '{"metadata":{"source":"load-test"}}' 2>/dev/null)"; then
    TASK_IDS+=("${id}")
    jq -nc --arg id "${id}" --argjson submitted "${started_ns}" \
      '{id:$id, submitted_at:$submitted}' >>"${RESULTS}"
  else
    SUBMIT_FAILURES=$(( SUBMIT_FAILURES + 1 ))
  fi
  # Pace submissions. perl is present on every macOS and every GitHub runner;
  # `sleep` with sub-second arguments is not portable to all /bin/sh.
  [[ "${RATE}" -ge 1000 ]] || perl -e "select(undef,undef,undef,${INTERVAL_US}/1000000)" 2>/dev/null || true
done
SUBMIT_ELAPSED=$(( $(date -u +%s) - SUBMIT_START ))
[[ "${SUBMIT_ELAPSED}" -gt 0 ]] || SUBMIT_ELAPSED=1
t_info "submitted ${#TASK_IDS[@]} in ${SUBMIT_ELAPSED}s (~$(( ${#TASK_IDS[@]} / SUBMIT_ELAPSED ))/s), ${SUBMIT_FAILURES} rejected"
assert_eq "0" "${SUBMIT_FAILURES}" "submission failures"

# ---------------------------------------------------------------------------
t_case "Drain the backlog within ${TIMEOUT}s"
DEADLINE=$(( $(date -u +%s) + TIMEOUT ))
COMPLETED=0
PEAK_HOLDING=0
while [[ "$(date -u +%s)" -lt "${DEADLINE}" ]]; do
  holding="$(holding_capacity)"
  [[ "${holding}" -gt "${PEAK_HOLDING}" ]] && PEAK_HOLDING="${holding}"
  queued="$(fs_count tasks state EQUAL QUEUED)"
  ready="$(fs_count tasks state EQUAL READY)"
  parked="$(fs_count tasks state EQUAL PARKED)"
  printf '\r  queued=%s ready=%s parked=%s holding=%s   ' \
    "${queued}" "${ready}" "${parked}" "${holding}" >&2
  if [[ "${queued}" -eq 0 && "${ready}" -eq 0 && "${holding}" -eq 0 ]]; then
    break
  fi
  sleep 5
done
printf '\n' >&2

# ---------------------------------------------------------------------------
t_case "Collect per-task timings"
MEASURED="${BUILD_DIR}/load-test-timings-${ENVIRONMENT}.jsonl"
: >"${MEASURED}"
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  doc="$(task_doc "${id}")"
  [[ "${doc}" == "null" || -z "${doc}" ]] && continue
  state="$(jq -r '.state // "?"' <<<"${doc}")"
  [[ "${state}" =~ ^(SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED)$ ]] && COMPLETED=$(( COMPLETED + 1 ))

  # Timings come from the event log, which is the only place that records when
  # each transition actually happened rather than when we happened to poll.
  #
  # `kind` reads the one stored shape whose type field is wrong: before
  # 2026-09-24 the API wrote a flag-only cancel -- a REQUEST, the task still
  # holding its lease -- as type `cancelled` with detail.phase
  # `cancel_requested` (contract request 17). Counted as `cancelled`, it ended
  # `done` when cancel was pressed rather than when the task ended.
  events="$(task_events "${id}" | jq -sc '.')"
  jq -nc --arg id "${id}" --arg state "${state}" --argjson ev "${events}" '
    def epoch: (. // "") | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601? // null;
    def kind: if .type == "cancelled" and ((.detail | type) == "object") and .detail.phase == "cancel_requested"
              then "cancel_requested" else .type end;
    def at($t): [ $ev[] | select(kind == $t) | (.at // "") | epoch ] | map(select(. != null)) | min;
    { id:$id, state:$state,
      submitted: at("submitted"), leased: at("lease_acquired"),
      running: at("running"), done: (at("succeeded") // at("failed") // at("cancelled")) }
    | . + { admission: (if .submitted and .leased then (.leased - .submitted) else null end),
            startup:   (if .leased and .running then (.running - .leased) else null end),
            total:     (if .submitted and .done then (.done - .submitted) else null end) }' \
    >>"${MEASURED}"
done
assert_ge "${COMPLETED}" "$(( ${#TASK_IDS[@]} * 90 / 100 ))" "tasks reaching a terminal state (90% of ${#TASK_IDS[@]})"

# ---------------------------------------------------------------------------
t_case "Latency distribution"
percentiles() {
  local field="$1" label="$2"
  local values p50 p90 p99 n
  values="$(jq -r --arg f "${field}" '.[$f] // empty' "${MEASURED}" | sort -n)"
  n="$(printf '%s\n' "${values}" | grep -c . || true)"
  if [[ "${n}" -eq 0 ]]; then
    t_info "${label}: no samples"
    return
  fi
  p50="$(printf '%s\n' "${values}" | sed -n "$(( (n * 50 + 99) / 100 ))p")"
  p90="$(printf '%s\n' "${values}" | sed -n "$(( (n * 90 + 99) / 100 ))p")"
  p99="$(printf '%s\n' "${values}" | sed -n "$(( (n * 99 + 99) / 100 ))p")"
  t_info "$(printf '%-28s n=%-4s p50=%ss p90=%ss p99=%ss' "${label}" "${n}" "${p50}" "${p90}" "${p99}")"
}
percentiles admission "submitted -> LEASED"
percentiles startup   "LEASED -> RUNNING"
percentiles total     "submitted -> terminal"
t_pass "timings written to ${MEASURED}"

# ---------------------------------------------------------------------------
t_case "No capacity leaked under load"
assert_le "$(pool_active global)" "$(pool_doc global | jq -r "${FS_JQ} effective_limit")" "global pool active vs limit"
if wait_until "leases to drain" 120 leases_at_most "${BASE_LEASES}"; then
  t_pass "every lease released"
else
  t_info "still holding: $(active_leases) lease(s)"
  t_fail "leases outstanding after the load test drained"
fi
t_info "peak tasks holding capacity: ${PEAK_HOLDING}"

t_summary
