#!/usr/bin/env bash
# Failure handling: what happens when tasks fail, are cancelled, or are malformed.
#
# The assertion that matters in every case is the same one: capacity comes back.
# A lease that is not released on a failure path is worse than the failure --
# it permanently shrinks the platform, and nothing notices until the swarm
# mysteriously stops admitting work.
#
# Usage: scripts/failure-test.sh [--timeout 600]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="failure"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

TIMEOUT=600
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Failure handling: ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform

BASE_HOLDING="$(holding_capacity)"
BASE_LEASES="$(active_leases)"
BASE_GLOBAL="$(pool_active global)"
info "baseline: holding=${BASE_HOLDING} leases=${BASE_LEASES} global_active=${BASE_GLOBAL}"

TASK_IDS=()
cleanup() { [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}; }
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
t_case "A failing task is retried and then dead-lettered, not retried forever"
FAIL_ID="$(submit_task mock '{"fail":true,"message":"deliberate failure"}' \
  '{"max_attempts":2,"metadata":{"source":"failure-test"}}')" || t_fail "submission failed"
if [[ -n "${FAIL_ID:-}" ]]; then
  TASK_IDS+=("${FAIL_ID}")
  t_info "task ${FAIL_ID}"
  if final="$(wait_for_state "${FAIL_ID}" "FAILED|DEAD_LETTERED|SUCCEEDED" "${TIMEOUT}")"; then
    case "${final}" in
      FAILED|DEAD_LETTERED) t_pass "ended ${final}" ;;
      SUCCEEDED) t_fail "the mock runner ignored the failure input and succeeded" ;;
    esac
    ATTEMPTS="$(task_field "${FAIL_ID}" '.attempt_count // 0')"
    assert_le "${ATTEMPTS}" "2" "attempts used (max_attempts=2)"
    assert_ge "${ATTEMPTS}" "1" "at least one attempt was made"
  else
    t_fail "no terminal state within ${TIMEOUT}s (at ${final})"
  fi
fi

# ---------------------------------------------------------------------------
t_case "The failed task released its lease"
if [[ -n "${FAIL_ID:-}" ]]; then
  LEASE_ID="$(task_field "${FAIL_ID}" '.current_lease_id // ""')"
  if [[ -n "${LEASE_ID}" && "${LEASE_ID}" != "null" ]]; then
    RELEASED="$(fs_get "leases/${LEASE_ID}" | jq -r "${FS_JQ} if .fields then (doc.released_at // \"null\") else \"missing\" end")"
    if [[ "${RELEASED}" != "null" && "${RELEASED}" != "missing" ]]; then
      t_pass "lease released (${RELEASED})"
    else
      t_fail "lease ${LEASE_ID} still holds capacity after the task failed"
    fi
  else
    t_info "no lease recorded (the task may have failed before admission)"
    t_pass "nothing to release"
  fi
fi

# ---------------------------------------------------------------------------
t_case "Cancelling a task in flight releases its capacity"
CANCEL_ID="$(submit_task mock '{"sleep_seconds":120,"message":"cancel me"}' \
  '{"metadata":{"source":"failure-test"}}')" || t_fail "submission failed"
if [[ -n "${CANCEL_ID:-}" ]]; then
  TASK_IDS+=("${CANCEL_ID}")
  if wait_until "the task to be admitted" 300 \
       task_in_state "${CANCEL_ID}" "LEASED|DISPATCHED|STARTING|RUNNING"; then
    t_info "admitted at $(task_state "${CANCEL_ID}")"
    cancel_task "${CANCEL_ID}"
    if final="$(wait_for_state "${CANCEL_ID}" "CANCELLED|SUCCEEDED|FAILED" 300)"; then
      assert_eq "CANCELLED" "${final}" "state after cancellation"
    else
      t_fail "task did not settle after cancellation (at ${final})"
    fi
  else
    t_info "task never got admitted (platform may be at its limit); cancelling anyway"
    cancel_task "${CANCEL_ID}"
    assert_eq "CANCELLED" "$(wait_for_state "${CANCEL_ID}" "CANCELLED" 60 || task_state "${CANCEL_ID}")" "state after cancellation"
  fi
fi

# ---------------------------------------------------------------------------
t_case "Malformed submissions are rejected, not queued"
declare -a BAD_BODIES=(
  '{"input":{}}'
  '{"runner_profile":"","input":{}}'
  '{"runner_profile":"mock"}'
  '{"runner_profile":"mock","input":"not-an-object"}'
  '{"runner_profile":"../../etc/passwd","input":{}}'
)
REJECTED=0
# A non-2xx from api_post is not proof the body was validated and rejected --
# an expired session (401), a missing run.invoker binding (403), the wrong
# address (404), a cold-start 503 or a curl-level timeout/DNS failure land in
# the same branch as a genuine 422 unless the status is actually inspected.
# This used to accept any 4xx, which let the first three through; only what
# `validation_rejected` accepts means the request reached validation.
BAD_BODY_OUT="$(mktemp "${TMPDIR:-/tmp}/swarm-failtest.XXXXXX")"
for body in "${BAD_BODIES[@]}"; do
  if api_post "/tasks" "${body}" >"${BAD_BODY_OUT}" 2>&1; then
    t_fail "accepted a malformed body: ${body}"
  elif validation_rejected "${API_STATUS}"; then
    REJECTED=$(( REJECTED + 1 ))
  else
    detail="$(cat "${BAD_BODY_OUT}")"
    rm -f "${BAD_BODY_OUT}"
    die_if_auth_failure "${detail}"
    t_fail "malformed body not evaluated -- HTTP ${API_STATUS} is not a validation rejection: ${body}"
    printf '%s\n' "${detail}" | redact | head -n 3 | sed 's/^/     /' >&2
  fi
done
rm -f "${BAD_BODY_OUT}"
assert_eq "${#BAD_BODIES[@]}" "${REJECTED}" "malformed bodies rejected"

# ---------------------------------------------------------------------------
t_case "An oversized input is refused rather than stored"
BIG="$(jq -nc --arg s "$(head -c 400000 /dev/zero | tr '\0' 'x')" '{blob:$s}')"
BIG_OUT="$(mktemp "${TMPDIR:-/tmp}/swarm-failtest.XXXXXX")"
# API_STATUS=0 means curl itself never got an answer -- a ~400 KB body against
# HTTP_TIMEOUT is exactly the request most likely to time out or have its
# connection reset. That is not evidence max_input_bytes was enforced; only the
# validator's own refusal is -- `validate_input_size` raises ValidationFailed,
# a 422. A 413 would be an edge refusing the body before the API saw it.
if api_post "/tasks" "$(jq -nc --argjson i "${BIG}" '{runner_profile:"mock", input:$i}')" \
     >"${BIG_OUT}" 2>&1; then
  rm -f "${BIG_OUT}"
  t_fail "accepted an input larger than max_input_bytes (256 KiB)"
elif validation_rejected "${API_STATUS}"; then
  rm -f "${BIG_OUT}"
  t_pass "oversized input rejected with HTTP ${API_STATUS}"
else
  detail="$(cat "${BIG_OUT}")"
  rm -f "${BIG_OUT}"
  die_if_auth_failure "${detail}"
  t_fail "oversized input not evaluated -- HTTP ${API_STATUS} is not a validation rejection"
  printf '%s\n' "${detail}" | redact | head -n 3 | sed 's/^/     /' >&2
fi

# ---------------------------------------------------------------------------
t_case "No capacity leaked across any failure path"
if wait_until "capacity to return to baseline" 180 holding_at_most "${BASE_HOLDING}"; then
  t_pass "tasks holding capacity back to ${BASE_HOLDING}"
else
  assert_le "$(holding_capacity)" "${BASE_HOLDING}" "tasks holding capacity"
fi
assert_le "$(pool_active global)" "$(( BASE_GLOBAL + 1 ))" "global pool active vs baseline"

# A pool whose active count went NEGATIVE means a double release; a pool above
# its limit means a missing release. Both are silent until they are not.
NEGATIVE="$(fs_list_docs pools | jq -s '[ .[] | select((.active // 0) < 0) | .id ] | join(", ")')"
if [[ "${NEGATIVE}" == '""' || "${NEGATIVE}" == '' ]]; then
  t_pass "no pool has a negative active count"
else
  t_fail "pools with negative active counts (double release): ${NEGATIVE}"
fi

t_summary
