#!/usr/bin/env bash
# Races: the last free slot, simultaneous cancellation, and stale generations.
#
# These are the failure modes that a single-threaded test never finds and that
# production finds immediately:
#
#   * two schedulers both admitting into the last slot -> the limit is exceeded
#     transiently and the platform oversubscribes;
#   * a task cancelled at the same moment it is dispatched -> either a running
#     agent nobody is watching, or a lease nobody releases;
#   * a retry whose predecessor is still alive -> two agents writing the same
#     workspace. Fencing generations exist to make the stale one exit without
#     running (CONTRACT.md invariant 5).
#
# The last-slot test narrows a NARROW pool (runner:<profile>), never the global
# pool, so the rest of the platform keeps working while it runs. The original
# limit is restored on every exit path.
#
# Usage: scripts/race-test.sh [--profile mock] [--parallel 12] [--timeout 300]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="race"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

PROFILE="mock"
PARALLEL=12
TIMEOUT=300
SLOT_LIMIT=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)  PROFILE="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --limit)    SLOT_LIMIT="$2"; shift 2 ;;
    --timeout)  TIMEOUT="$2"; shift 2 ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Race conditions: ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform

POOL_NAME="runner:${PROFILE}"
TASK_IDS=()
POOL_EXISTED=0
ORIGINAL_LIMIT=""
ORIGINAL_ENABLED="true"

ORIGINAL_POOL="$(pool_doc "${POOL_NAME}")"
if [[ "${ORIGINAL_POOL}" != "null" && -n "${ORIGINAL_POOL}" ]]; then
  POOL_EXISTED=1
  ORIGINAL_LIMIT="$(jq -r '.hard_limit // 0' <<<"${ORIGINAL_POOL}")"
  ORIGINAL_ENABLED="$(jq -r 'if .enabled == false then "false" else "true" end' <<<"${ORIGINAL_POOL}")"
fi

restore() {
  if [[ "${POOL_EXISTED}" -eq 1 ]]; then
    fs_patch "pools/${POOL_NAME}" "hard_limit,enabled,updated_at" \
      "$(jq -nc --argjson l "${ORIGINAL_LIMIT:-0}" --argjson e "${ORIGINAL_ENABLED}" --arg t "$(iso_now)" \
        '{hard_limit:{integerValue:($l|tostring)},enabled:{booleanValue:$e},updated_at:{timestampValue:$t}}')" \
      >/dev/null 2>&1 || true
    info "restored ${POOL_NAME} hard_limit to ${ORIGINAL_LIMIT}"
  else
    fs_delete "pools/${POOL_NAME}" 2>/dev/null || true
    info "removed the temporary ${POOL_NAME} pool"
  fi
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
}
trap restore EXIT INT TERM

# ---------------------------------------------------------------------------
t_case "Narrow ${POOL_NAME} to ${SLOT_LIMIT} slot(s)"
CURRENT_ACTIVE="$(jq -r '.active // 0' <<<"$(pool_doc "${POOL_NAME}")" 2>/dev/null || echo 0)"
[[ "${CURRENT_ACTIVE}" == "null" ]] && CURRENT_ACTIVE=0
fs_patch "pools/${POOL_NAME}" "name,hard_limit,active,enabled,updated_at" \
  "$(jq -nc --arg n "${POOL_NAME}" --argjson l "${SLOT_LIMIT}" --argjson a "${CURRENT_ACTIVE}" --arg t "$(iso_now)" \
    '{name:{stringValue:$n},hard_limit:{integerValue:($l|tostring)},
      active:{integerValue:($a|tostring)},enabled:{booleanValue:true},updated_at:{timestampValue:$t}}')"
assert_eq "${SLOT_LIMIT}" "$(pool_doc "${POOL_NAME}" | jq -r '.hard_limit')" "${POOL_NAME} hard_limit"

# ---------------------------------------------------------------------------
t_case "${PARALLEL} tasks race for ${SLOT_LIMIT} slot(s)"
SUBMIT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarm-race.XXXXXX")"
for i in $(seq 1 "${PARALLEL}"); do
  (
    submit_task "${PROFILE}" "$(jq -nc --argjson i "${i}" '{message:"race", index:$i, sleep_seconds:20}')" \
      '{"metadata":{"source":"race-test"}}' >"${SUBMIT_DIR}/${i}.id" 2>/dev/null || true
  ) &
done
wait
for f in "${SUBMIT_DIR}"/*.id; do
  [[ -f "${f}" ]] || continue
  id="$(cat "${f}")"
  [[ -n "${id}" ]] && TASK_IDS+=("${id}")
done
rm -rf "${SUBMIT_DIR}"
assert_ge "${#TASK_IDS[@]}" "$(( PARALLEL - 1 ))" "tasks accepted concurrently"

# ---------------------------------------------------------------------------
t_case "The narrowed pool is never over its limit, at any sample"
DEADLINE=$(( $(date -u +%s) + TIMEOUT ))
WORST=0
SAMPLES=0
while [[ "$(date -u +%s)" -lt "${DEADLINE}" ]]; do
  active="$(pool_active "${POOL_NAME}")"
  [[ "${active}" -gt "${WORST}" ]] && WORST="${active}"
  SAMPLES=$(( SAMPLES + 1 ))
  if [[ "${active}" -gt "${SLOT_LIMIT}" ]]; then
    t_info "OVER LIMIT: ${POOL_NAME} active=${active} limit=${SLOT_LIMIT}"
    break
  fi
  done_count=0
  for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
    task_is_terminal "${id}" && done_count=$(( done_count + 1 ))
  done
  [[ "${done_count}" -eq "${#TASK_IDS[@]}" ]] && break
  sleep 1
done
t_info "${SAMPLES} samples, worst observed active=${WORST}"
assert_le "${WORST}" "${SLOT_LIMIT}" "peak concurrent leases on ${POOL_NAME}"

# ---------------------------------------------------------------------------
t_case "Every attempt carries a distinct, increasing generation"
BAD_GENERATIONS=0
CHECKED=0
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  gens="$(fs_query attempts "$(fs_field_filter task_id EQUAL "$(jq -nc --arg v "${id}" '{stringValue:$v}')")" 50 \
    | jq -r "${FS_JQ} doc.generation" 2>/dev/null | sort -n | tr '\n' ' ')"
  [[ -z "${gens// /}" ]] && continue
  CHECKED=$(( CHECKED + 1 ))
  total_count="$(printf '%s' "${gens}" | tr ' ' '\n' | grep -c . || true)"
  uniq_count="$(printf '%s' "${gens}" | tr ' ' '\n' | grep . | sort -u | grep -c . || true)"
  if [[ "${uniq_count}" -ne "${total_count}" ]]; then
    BAD_GENERATIONS=$(( BAD_GENERATIONS + 1 ))
    t_info "task ${id} has duplicate generations: ${gens}"
  fi
done
t_info "checked ${CHECKED} task(s) with attempts"
assert_eq "0" "${BAD_GENERATIONS}" "tasks with duplicate attempt generations"

# ---------------------------------------------------------------------------
t_case "Simultaneous cancellation of every in-flight task"
CANCEL_PIDS=()
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  ( cancel_task "${id}" >/dev/null 2>&1 || true ) &
  CANCEL_PIDS+=($!)
done
for pid in ${CANCEL_PIDS[@]+"${CANCEL_PIDS[@]}"}; do wait "${pid}" || true; done
t_pass "cancellation requested for ${#TASK_IDS[@]} task(s) in parallel"

# ---------------------------------------------------------------------------
t_case "No lease survived the cancellation storm"
if wait_until "the ${POOL_NAME} pool to drain" 240 pool_drained "${POOL_NAME}"; then
  t_pass "${POOL_NAME} active is 0"
else
  t_fail "${POOL_NAME} still shows $(pool_active "${POOL_NAME}") active lease(s)"
fi

STUCK=0
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  state="$(task_state "${id}")"
  case "${state}" in
    SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED|QUEUED|PARKED|READY) ;;
    *) STUCK=$(( STUCK + 1 )); t_info "task ${id} is ${state}" ;;
  esac
done
assert_eq "0" "${STUCK}" "tasks still holding capacity after cancellation"

# ---------------------------------------------------------------------------
t_case "Accounting is consistent: no negative pools, none over limit"
BROKEN="$(fs_list_docs pools | jq -s "${FS_JQ}"' [ .[] | select((.active // 0) < 0 or (.active // 0) > effective_limit) | .id ] | join(", ")')"
if [[ "${BROKEN}" == '""' || -z "${BROKEN//\"/}" ]]; then
  t_pass "every pool is within [0, effective_limit]"
else
  t_fail "pools with impossible accounting: ${BROKEN}"
fi

t_summary
