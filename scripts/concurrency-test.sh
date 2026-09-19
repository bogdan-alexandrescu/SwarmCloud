#!/usr/bin/env bash
# Prove the concurrency invariant: no pool is EVER over its effective limit.
#
# The interesting failure this catches is transient. A scheduler that checks
# capacity, then writes the lease in a second step, will briefly exceed the limit
# under load and then settle back -- an end-of-run check would see nothing wrong.
# So this samples every pool continuously while a deliberate overload runs, and
# fails on the worst sample, not the last one.
#
# It also checks the accounting boundary from CONTRACT.md invariant 3: capacity
# is counted from LEASED, not RUNNING, so a slow container start still occupies
# its slot.
#
# Usage: scripts/concurrency-test.sh [--count N] [--profile mock] [--duration 180]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="concurrency"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

COUNT=0
PROFILE="mock"
DURATION=180
SAMPLE_INTERVAL=2

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)    COUNT="$2"; shift 2 ;;
    --profile)  PROFILE="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --interval) SAMPLE_INTERVAL="$2"; shift 2 ;;
    -h|--help)  sed -n '2,14p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Concurrency invariant: ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform

GLOBAL_LIMIT="$(pool_doc global | jq -r "${FS_JQ} effective_limit")" \
  || die "could not read the global pool from Firestore; see the Firestore error above -- this is a failed read, not proof the pool is unconfigured"
[[ "${GLOBAL_LIMIT}" -gt 0 ]] || die "the global pool has no limit configured; run 'make infra' first"
info "global effective limit: ${GLOBAL_LIMIT} units"

# Deliberately oversubscribe: three times the global limit guarantees that the
# limit is the binding constraint rather than the submission rate.
[[ "${COUNT}" -gt 0 ]] || COUNT=$(( GLOBAL_LIMIT * 3 ))
info "submitting ${COUNT} tasks on profile ${PROFILE}"

SAMPLES="${BUILD_DIR}/concurrency-samples-${ENVIRONMENT}.jsonl"
: >"${SAMPLES}"
TASK_IDS=()

cleanup() {
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
t_case "Submit ${COUNT} tasks (no infrastructure should appear yet)"
QUEUED_BEFORE="$(fs_count tasks state EQUAL QUEUED)"
for i in $(seq 1 "${COUNT}"); do
  if id="$(submit_task "${PROFILE}" "$(jq -nc --argjson i "${i}" '{message:"concurrency", index:$i}')" \
           '{"metadata":{"source":"concurrency-test"}}')"; then
    TASK_IDS+=("${id}")
  fi
done
assert_eq "${COUNT}" "${#TASK_IDS[@]}" "tasks accepted"
t_info "queued before: ${QUEUED_BEFORE}"

# ---------------------------------------------------------------------------
t_case "Sample every pool for ${DURATION}s while the backlog drains"
DEADLINE=$(( $(date -u +%s) + DURATION ))
VIOLATIONS=0
MAX_HOLDING=0
SAMPLE_COUNT=0

while [[ "$(date -u +%s)" -lt "${DEADLINE}" ]]; do
  SNAPSHOT="$(fs_list_docs pools | jq -sc "${FS_JQ} [ .[] | . + {effective: effective_limit} ]")"
  HOLDING="$(holding_capacity)"
  [[ "${HOLDING}" -gt "${MAX_HOLDING}" ]] && MAX_HOLDING="${HOLDING}"

  OVER="$(jq -c '[ .[] | select(.active > .effective) ]' <<<"${SNAPSHOT}")"
  N_OVER="$(jq -r 'length' <<<"${OVER}")"
  if [[ "${N_OVER}" -gt 0 ]]; then
    VIOLATIONS=$(( VIOLATIONS + N_OVER ))
    jq -r '.[] | "    OVER LIMIT: \(.id) active=\(.active) effective=\(.effective)"' <<<"${OVER}" >&2
  fi

  jq -nc --arg at "$(iso_now)" --argjson pools "${SNAPSHOT}" --argjson holding "${HOLDING}" \
    '{at:$at, holding:$holding, pools:$pools}' >>"${SAMPLES}"
  SAMPLE_COUNT=$(( SAMPLE_COUNT + 1 ))

  REMAINING="$(fs_count tasks state EQUAL QUEUED)"
  READY="$(fs_count tasks state EQUAL READY)"
  if [[ "${REMAINING}" -eq 0 && "${READY}" -eq 0 && "${HOLDING}" -eq 0 && "${SAMPLE_COUNT}" -gt 3 ]]; then
    t_info "backlog drained after ${SAMPLE_COUNT} samples"
    break
  fi
  sleep "${SAMPLE_INTERVAL}"
done
t_info "${SAMPLE_COUNT} samples taken, peak tasks holding capacity: ${MAX_HOLDING}"
assert_eq "0" "${VIOLATIONS}" "pool-over-limit samples"

# ---------------------------------------------------------------------------
t_case "Global unit budget was never exceeded"
PEAK_GLOBAL="$(jq -s '[ .[].pools[] | select(.id == "global") | .active ] | max // 0' "${SAMPLES}")"
assert_le "${PEAK_GLOBAL}" "${GLOBAL_LIMIT}" "peak global pool usage"

# ---------------------------------------------------------------------------
t_case "Leases, not pods, are what counted"
# active pool units must equal the units held by unreleased leases at every
# sample; if the scheduler ever counted RUNNING instead of LEASED these diverge.
PEAK_LEASES="$(active_leases)"
t_info "unreleased leases now: ${PEAK_LEASES}"
FINAL_GLOBAL="$(pool_active global)"
assert_le "${FINAL_GLOBAL}" "${GLOBAL_LIMIT}" "global pool active at end"

# ---------------------------------------------------------------------------
t_case "Backlog cost nothing while it waited"
# QUEUED/PARKED/READY must never create infrastructure demand (invariant 1).
EXECUTIONS_ERR="$(mktemp "${TMPDIR:-/tmp}/swarm-executions.XXXXXX")"
if EXECUTIONS_RAW="$(gcloud run jobs executions list --project "${PROJECT_ID}" --region "${REGION}" \
     --format='value(metadata.name)' --limit 500 2>"${EXECUTIONS_ERR}")"; then
  rm -f "${EXECUTIONS_ERR}"
  if [[ -z "${EXECUTIONS_RAW}" ]]; then
    EXECUTIONS=0
  else
    EXECUTIONS="$(printf '%s\n' "${EXECUTIONS_RAW}" | wc -l | tr -d ' ')"
  fi
  t_info "cloud run executions visible: ${EXECUTIONS}"
else
  EXECUTIONS_ERR_DETAIL="$(cat "${EXECUTIONS_ERR}" 2>/dev/null || true)"
  rm -f "${EXECUTIONS_ERR}"
  # A denied run.jobs.executions.list or an expired session must not be read as
  # "zero executions" -- that is the one number this test case exists to check.
  die_if_auth_failure "${EXECUTIONS_ERR_DETAIL}"
  warn "could not list Cloud Run job executions; this is a failed listing, not proof that none exist"
  printf '%s\n' "${EXECUTIONS_ERR_DETAIL}" | redact | head -n 3 | sed 's/^/     /' >&2
fi
PEAK_HOLDING_SAMPLE="$(jq -s '[ .[].holding ] | max // 0' "${SAMPLES}")"
assert_le "${PEAK_HOLDING_SAMPLE}" "${GLOBAL_LIMIT}" "peak tasks holding capacity vs global limit"

t_info "samples: ${SAMPLES}"
t_summary
