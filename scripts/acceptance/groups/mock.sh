#!/usr/bin/env bash
# Acceptance group `mock`: every behaviour the mock runner declares, each run
# for real and each judged by what it PRODUCED.
#
# The mock is the one profile with no provider (apps/agent-worker/agent_worker/
# runners/mock.py), so these checks need nothing but a deployment. What each
# one asserts, and why, is in docs/acceptance.md. The tasks are submitted
# together first and judged one by one after, so the group costs roughly its
# slowest task (the 150 s checkpoint sleep, the 90 s park) rather than the sum.

set -euo pipefail

mock_checks() {
  cat <<'EOF'
mock: success produces the runner's own record of the work
mock: fail with fail_message ends FAILED, runner_error, carrying the message
mock: every exit_code class is recorded as the runner's, and the reserved ones are refused at the door
mock: a short rate limit is retried in place, then the task succeeds
mock: quota_exhausted parks with its retry_after, holds no capacity, then resumes
mock: the resumed attempt restores the checkpoint the parked one recorded
mock: cancel while running ends CANCELLED before the work is done, lease released
mock: cancel while queued ends CANCELLED having never held a lease
mock: a long sleep writes periodic checkpoints whose ids increase
mock: an artifact's downloaded bytes equal artifact_text exactly
mock: steps runs exactly that many steps
mock: cpu_burn runs within the resource class's limits
EOF
}

_mock_input() {
  jq -nc --arg p "acceptance ${ACC_RUN_ID} ${ACC_CHECK}" --argjson x "${1:-{\}}" '{prompt: $p} + $x'
}

# _mock_submit VAR CHECK INPUT_EXTRA [TASK_EXTRA]
_mock_submit() {
  local __ms_var="$1" __ms_input="$3" __ms_extra="${4:-{\}}"
  ACC_CHECK="$2"
  acc_submit "${__ms_var}" mock "$(_mock_input "${__ms_input}")" "$(acc_extra "${__ms_extra}")" \
    || printf -v "${__ms_var}" '%s' ""
}

run_mock() {
  step "Acceptance: mock"
  local ok fail ex2 ex76 ex255 retry park cancel_run ckpt art steps cpu wf="" queued_up queued_down
  local art_name art_text

  art_name="acceptance-${ACC_RUN_ID}.txt"
  # A newline inside, a trailing newline, and a non-ASCII character: the three
  # things a lossy download (a shell variable, a JSON round trip, a charset
  # guess) changes.
  art_text="$(printf 'line one\nline two \xe2\x9c\x93 %s\n\n' "${ACC_RUN_ID}")"$'\n'

  _mock_submit ok "mock: success" '{"steps": 2, "sleep_seconds": 2}'
  _mock_submit fail "mock: fail" "$(jq -nc --arg m "acceptance fail_message ${ACC_RUN_ID}" '{fail: true, fail_message: $m}')" '{"max_attempts": 1}'
  _mock_submit ex2 "mock: exit 2" "$(jq -nc --arg m "exit 2 ${ACC_RUN_ID}" '{fail: true, fail_message: $m, exit_code: 2}')" '{"max_attempts": 1}'
  _mock_submit ex76 "mock: exit 76" "$(jq -nc --arg m "exit 76 ${ACC_RUN_ID}" '{fail: true, fail_message: $m, exit_code: 76}')" '{"max_attempts": 1}'
  _mock_submit ex255 "mock: exit 255" "$(jq -nc --arg m "exit 255 ${ACC_RUN_ID}" '{fail: true, fail_message: $m, exit_code: 255}')" '{"max_attempts": 1}'
  _mock_submit retry "mock: retry" '{"quota_exhausted": true, "retry_after_seconds": 1, "steps": 2, "sleep_seconds": 2}'
  _mock_submit park "mock: park" '{"quota_exhausted": true, "retry_after_seconds": 90, "steps": 2, "sleep_seconds": 2}'
  _mock_submit cancel_run "mock: cancel running" '{"steps": 20, "sleep_seconds": 600}'
  _mock_submit ckpt "mock: checkpoints" '{"steps": 5, "sleep_seconds": 150}'
  _mock_submit art "mock: artifact" "$(jq -nc --arg n "${art_name}" --arg t "${art_text}" '{artifact_name: $n, artifact_text: $t, steps: 1, sleep_seconds: 0}')"
  _mock_submit steps "mock: steps" '{"steps": 7, "sleep_seconds": 3.5}'
  _mock_submit cpu "mock: cpu_burn" '{"cpu_burn_seconds": 20, "steps": 2, "sleep_seconds": 0}'

  # A task that cannot be admitted while its parent runs: a workflow step
  # whose one dependency sleeps. Cancelling it is "cancel while queued" with
  # no race against the scheduler.
  ACC_CHECK="mock: cancel queued"
  queued_up=""
  queued_down=""
  if acc_workflow wf "$(jq -nc --argjson m "$(acc_metadata)" --arg p "acceptance ${ACC_RUN_ID} queued" '{
        priority: 10, metadata: $m, on_step_failure: "fail_workflow",
        steps: [
          {step_id: "upstream", runner_profile: "mock", input: {prompt: $p, steps: 20, sleep_seconds: 600}},
          {step_id: "queued", runner_profile: "mock", depends_on: ["upstream"], input: {prompt: $p}}
        ]}')"; then
    queued_up="$(workflow_step_task "${wf}" upstream)"
    queued_down="$(workflow_step_task "${wf}" queued)"
  fi

  _mock_check_success "${ok}"
  _mock_check_fail "${fail}"
  _mock_check_exit_codes "${ex2}" "${ex76}" "${ex255}"
  _mock_check_cancel_queued "${queued_down}" "${queued_up}"
  _mock_check_cancel_running "${cancel_run}"
  _mock_check_retry "${retry}"
  _mock_check_park_and_restore "${park}"
  _mock_check_artifact "${art}" "${art_name}" "${art_text}"
  _mock_check_steps "${steps}"
  _mock_check_cpu "${cpu}"
  _mock_check_checkpoints "${ckpt}"
}

# ---------------------------------------------------------------------------

_mock_check_success() {
  local task="$1" state
  acc_check "mock: success produces the runner's own record of the work"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  if [[ "${state}" != "SUCCEEDED" ]]; then
    acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error recorded"' | redact | head -c 300)" "${task}"
    return 0
  fi
  acc_assert_eq "2/2" "$(acc_output "${task}" '"\(.completed_steps)/\(.requested_steps)"')" "completed/requested steps" "${task}"
  acc_assert_eq "mock runner completed 2/2 steps" "$(acc_output "${task}" '.summary // empty')" "runner summary" "${task}"
  local released
  if released="$(acc_leases_released "${task}")" && [[ "${released}" != 0* ]]; then
    acc_pass "${released}" "${task}"
  else
    acc_fail "capacity: ${released}" "${task}"
  fi
}

_mock_check_fail() {
  local task="$1" state msg
  acc_check "mock: fail with fail_message ends FAILED, runner_error, carrying the message"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  acc_assert_eq "FAILED" "${state}" "state" "${task}"
  acc_assert_eq "runner_error" "$(task_field "${task}" '.end_cause // "none"')" "end_cause" "${task}"
  msg="acceptance fail_message ${ACC_RUN_ID}"
  if task_field "${task}" '.last_error // ""' | grep -qF "${msg}"; then
    acc_pass "last_error carries the fail_message" "${task}"
  else
    acc_fail "last_error does not carry '${msg}': $(task_field "${task}" '.last_error // "none"' | redact | head -c 200)" "${task}"
  fi
  acc_assert_eq "1" "$(task_field "${task}" '.attempt_count // 0')" "attempts (max_attempts 1: a failure is not retried)" "${task}"
}

_mock_check_exit_codes() {
  local code task state got answer
  acc_check "mock: every exit_code class is recorded as the runner's, and the reserved ones are refused at the door"
  # Accepted: 2 (an ordinary non-1 code), 76 (the WORKER's own timeout code --
  # a runner exiting with it must still read as a runner failure, not a
  # timeout), 255 (the ceiling). Each FAILED, runner_error, its own code.
  for code in 2 76 255; do
    case "${code}" in 2) task="$1" ;; 76) task="$2" ;; 255) task="$3" ;; esac
    [[ -n "${task}" ]] || { acc_fail "exit_code ${code}: not submitted"; continue; }
    acc_run_to_end state "${task}" || continue
    got="$(task_field "${task}" '"\(.state) \(.end_cause // "none") \(.result_summary.exit_code // "none")"')"
    acc_assert_eq "FAILED runner_error ${code}" "${got}" "exit_code ${code}: state, end_cause, recorded exit code" "${task}"
    if task_field "${task}" '.last_error // ""' | grep -qF "exit ${code} ${ACC_RUN_ID}"; then
      acc_pass "exit_code ${code}: last_error is the runner's message, not a generic one" "${task}"
    else
      acc_fail "exit_code ${code}: last_error lost the runner's message" "${task}"
    fi
  done
  # Refused: 0 (success beside a result.json), 77 (rate limit), 78 (refused
  # credential), 143 (SIGTERM), 256 (out of range). Each is a 422 at the door.
  for code in 0 77 78 143 256; do
    answer="$(acc_door mock "$(jq -nc --argjson c "${code}" '{prompt: "acceptance door", fail: true, exit_code: $c}')")"
    acc_assert_eq "422 invalid_input" "${answer}" "exit_code ${code} at the door"
  done
}

_mock_check_retry() {
  local task="$1" state retries attempts resumed
  acc_check "mock: a short rate limit is retried in place, then the task succeeds"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "state after the retries" "${task}"
  retries="$(acc_events "${task}" | jq -s '[.[] | select(.type == "retrying")] | length')"
  if [[ "${retries}" -ge 1 ]]; then
    acc_pass "${retries} in-place retry event(s) before the park" "${task}"
  else
    acc_fail "no 'retrying' event: a 1 s retry-after was not retried in place" "${task}"
  fi
  attempts="$(task_field "${task}" '.attempt_count // 0')"
  resumed="$(acc_output "${task}" '.was_resumed // false')"
  acc_assert_eq "true" "$([[ "${attempts}" -ge 2 && "${resumed}" == "true" ]] && echo true || echo "attempts=${attempts} was_resumed=${resumed}")" "a second attempt ran and resumed" "${task}"
}

_mock_check_park_and_restore() {
  local task="$1" state doc reason held parked_at eligible_at waited latest restored last_before released
  acc_check "mock: quota_exhausted parks with its retry_after, holds no capacity, then resumes"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  if ! state="$(wait_for_state "${task}" "PARKED|SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED" "${ACC_TIMEOUT}")"; then
    acc_fail "never parked (last state ${state})" "${task}"
    return 0
  fi
  if [[ "${state}" != "PARKED" ]]; then
    acc_fail "ended ${state} without parking" "${task}"
    return 0
  fi
  doc="$(task_doc "${task}")"
  reason="$(jq -r '.park_reason // "none"' <<<"${doc}")"
  acc_assert_eq "QUOTA_EXHAUSTED" "${reason}" "park_reason" "${task}"
  latest="$(jq -r '.latest_checkpoint // ""' <<<"${doc}")"
  # Invariant 1: a PARKED task costs nothing. Its pointer to a lease is gone
  # and every lease it held is released.
  held="$(jq -r '.current_lease_id // "null"' <<<"${doc}")"
  if released="$(acc_leases_released "${task}")" && [[ "${held}" == "null" ]]; then
    acc_pass "while PARKED: current_lease_id null, ${released} (invariant 1)" "${task}"
  else
    acc_fail "while PARKED it still holds capacity: current_lease_id=${held}; ${released}" "${task}"
  fi
  parked_at="$(acc_events "${task}" | jq -sr '[.[] | select(.type == "parked")] | sort_by(.at) | last | .at // empty')"
  eligible_at="$(jq -r '.next_eligible_at // empty' <<<"${doc}")"
  if [[ -n "${parked_at}" && -n "${eligible_at}" ]]; then
    waited=$(( $(acc_iso_epoch "${eligible_at}") - $(acc_iso_epoch "${parked_at}") ))
    if [[ "${waited}" -ge 85 ]]; then
      acc_pass "next_eligible_at is ${waited}s after the park (retry_after 90)" "${task}"
    else
      acc_fail "next_eligible_at is only ${waited}s after the park; retry_after was 90" "${task}"
    fi
  else
    acc_fail "the park left no parked event or no next_eligible_at to compare" "${task}"
  fi
  if [[ "$(acc_events "${task}" | jq -s '[.[] | select(.type == "retrying")] | length')" == "0" ]]; then
    acc_pass "parked at once: a 90 s wait is not retried in place" "${task}"
  else
    acc_fail "a 90 s retry-after was retried in place instead of parked" "${task}"
  fi

  acc_run_to_end state "${task}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "state after the park" "${task}"

  acc_check "mock: the resumed attempt restores the checkpoint the parked one recorded"
  restored="$(acc_events "${task}" | jq -sr '[.[] | select(.type == "checkpoint_restored")] | sort_by(.at) | first | .detail.checkpoint_id // empty')"
  last_before="$(acc_events "${task}" | jq -sr '
      ([.[] | select(.type == "checkpoint_restored")] | sort_by(.at) | first | .at) as $r
      | [.[] | select(.type == "checkpoint_completed" and ($r == null or .at < $r))]
      | sort_by(.at) | last | .detail.checkpoint_id // empty')"
  if [[ -z "${restored}" ]]; then
    acc_fail "no checkpoint_restored event: the second attempt started from nothing" "${task}"
  elif [[ "${restored}" == "${last_before}" ]]; then
    acc_pass "restored ${restored}, the last checkpoint written before the resume" "${task}"
  else
    acc_fail "restored ${restored}, but the last checkpoint before the resume was ${last_before:-none}" "${task}"
  fi
  if [[ -n "${latest}" && -n "${restored}" && "${latest}" == *"${restored}"* ]]; then
    acc_pass "the task's latest_checkpoint while PARKED named ${restored}" "${task}"
  else
    acc_fail "latest_checkpoint while PARKED was '${latest:-empty}', which does not name the restored ${restored:-none}" "${task}"
  fi
  acc_assert_eq "true" "$(acc_output "${task}" '.was_resumed // false')" "the runner saw its own saved state (was_resumed)" "${task}"
}

_mock_check_cancel_running() {
  local task="$1" state steps released
  acc_check "mock: cancel while running ends CANCELLED before the work is done, lease released"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  if ! state="$(wait_for_state "${task}" "RUNNING|SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED" "${ACC_TIMEOUT}")" || [[ "${state}" != "RUNNING" ]]; then
    acc_fail "never observed RUNNING (last state ${state})" "${task}"
    return 0
  fi
  if ! cancel_task "${task}"; then
    acc_fail "the cancel was refused (HTTP ${API_STATUS})" "${task}"
    return 0
  fi
  acc_run_to_end state "${task}" || return 0
  acc_assert_eq "CANCELLED" "${state}" "state" "${task}"
  steps="$(acc_output "${task}" '.completed_steps // "none"')"
  if [[ "${steps}" == "none" || "${steps}" -lt 20 ]]; then
    acc_pass "stopped early: ${steps} of 20 steps" "${task}"
  else
    acc_fail "all 20 steps completed: the cancel did not stop the work" "${task}"
  fi
  if released="$(acc_leases_released "${task}")" && [[ "${released}" != 0* ]]; then
    acc_pass "${released}" "${task}"
  else
    acc_fail "capacity: ${released}" "${task}"
  fi
}

_mock_check_cancel_queued() {
  local task="$1" upstream="$2" state leases
  acc_check "mock: cancel while queued ends CANCELLED having never held a lease"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; cancel_all "${upstream}"; return 0; }
  state="$(task_state "${task}")"
  case "${state}" in
    QUEUED|READY|PARKED) ;;
    *) acc_fail "was ${state} before the cancel, not waiting on its parent" "${task}"; cancel_all "${task}" "${upstream}"; return 0 ;;
  esac
  # Invariant 1, where it is observable: a task waiting costs nothing.
  if [[ "$(task_field "${task}" '.current_lease_id // "null"')" == "null" ]] && [[ -z "$(task_lease_ids "${task}")" ]]; then
    acc_pass "while ${state}: no lease, no current_lease_id (invariant 1)" "${task}"
  else
    acc_fail "while ${state} it names a lease" "${task}"
  fi
  if ! cancel_task "${task}"; then
    acc_fail "the cancel was refused (HTTP ${API_STATUS})" "${task}"
    cancel_all "${upstream}"
    return 0
  fi
  if ! state="$(wait_for_state "${task}" "CANCELLED|SUCCEEDED|FAILED|DEAD_LETTERED" 120)"; then
    acc_fail "not CANCELLED 120s after the cancel (state ${state})" "${task}"
  else
    acc_assert_eq "CANCELLED" "${state}" "state (was waiting on its parent)" "${task}"
  fi
  leases="$(task_lease_ids "${task}")"
  if [[ -z "${leases}" ]]; then
    acc_pass "its events name no lease: it never held capacity" "${task}"
  else
    acc_fail "its events name lease(s) ${leases//$'\n'/ }: it was admitted after all" "${task}"
  fi
  cancel_all "${upstream}"
}

_mock_check_checkpoints() {
  local task="$1" state ids count
  acc_check "mock: a long sleep writes periodic checkpoints whose ids increase"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "state" "${task}"
  ids="$(acc_events "${task}" | acc_checkpoint_ids)"
  count="$(printf '%s' "${ids}" | awk 'NF' | wc -l | tr -d ' ')"
  # 150 s at the mock profile's 30 s interval (checkpoint_interval_seconds)
  # is four periodic checkpoints and a final one. Three is the floor that
  # still proves "periodic": the final checkpoint alone is one.
  if [[ "${count}" -ge 3 ]]; then
    acc_pass "${count} checkpoints written: ${ids//$'\n'/ }" "${task}"
  else
    acc_fail "only ${count} checkpoint(s) in 150 s at a 30 s interval: ${ids//$'\n'/ }" "${task}"
  fi
  if printf '%s\n' "${ids}" | acc_strictly_increasing; then
    acc_pass "checkpoint ids increase in the order they were written" "${task}"
  else
    acc_fail "checkpoint ids do not increase in write order: ${ids//$'\n'/ }" "${task}"
  fi
}

_mock_check_artifact() {
  local task="$1" name="$2" text="$3" state file want
  acc_check "mock: an artifact's downloaded bytes equal artifact_text exactly"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "state" "${task}"
  file="${ACC_WORK}/artifact-got"
  want="${ACC_WORK}/artifact-want"
  printf '%s' "${text}" >"${want}"
  if ! acc_raw_artifact "${task}" "${name}" "${file}"; then
    acc_fail "GET artifacts/raw?name=${name} answered HTTP ${ACC_HTTP:-none}" "${task}"
    return 0
  fi
  if cmp -s "${want}" "${file}"; then
    acc_pass "${name}: $(wc -c <"${file}" | tr -d ' ') bytes, identical to artifact_text" "${task}"
  else
    acc_fail "${name}: $(wc -c <"${file}" | tr -d ' ') bytes downloaded, $(wc -c <"${want}" | tr -d ' ') sent; first difference: $(cmp "${want}" "${file}" 2>&1 | head -n 1)" "${task}"
  fi
}

_mock_check_steps() {
  local task="$1" state
  acc_check "mock: steps runs exactly that many steps"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "state" "${task}"
  acc_assert_eq "7 7" "$(acc_output "${task}" '"\(.completed_steps) \(.requested_steps)"')" "completed and requested steps" "${task}"
}

_mock_check_cpu() {
  local task="$1" state runtimes memory_gib attempt rss near duration
  acc_check "mock: cpu_burn runs within the resource class's limits"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "state" "${task}"
  acc_assert_eq "20" "$(acc_output "${task}" '.metrics.cpu_burn_seconds // "none" | tostring')" "cpu_burn_seconds the runner reports" "${task}"
  duration="$(task_field "${task}" '.result_summary.duration_seconds // 0 | floor')"
  if [[ "${duration}" -ge 20 ]]; then
    acc_pass "the runner ran ${duration}s, so the burn happened" "${task}"
  else
    acc_fail "the runner ran ${duration}s, less than the 20 s it was asked to burn" "${task}"
  fi
  runtimes="${ACC_WORK}/runtimes.json"
  if ! api_fetch "/runtimes" "${runtimes}"; then
    acc_fail "could not read the catalogue to know the class's memory" "${task}"
    return 0
  fi
  memory_gib="$(jq -r '.runtimes.mock.resources.memory_gib // empty' "${runtimes}")"
  attempt="$(task_attempts "${task}" | jq -s 'sort_by(.created_at) | last')"
  rss="$(jq -r '.peak_rss_bytes // empty' <<<"${attempt}")"
  near="$(jq -r '.oom_near_miss // false' <<<"${attempt}")"
  if [[ -z "${rss}" || -z "${memory_gib}" ]]; then
    acc_fail "peak_rss_bytes (${rss:-unrecorded}) or the class memory (${memory_gib:-unknown}) was not available" "${task}"
  elif [[ "${rss}" -lt $(( memory_gib * 1024 * 1024 * 1024 )) && "${near}" != "true" ]]; then
    acc_pass "peak RSS ${rss} bytes under the ${memory_gib} GiB limit, no OOM near miss" "${task}"
  else
    acc_fail "peak RSS ${rss} bytes against ${memory_gib} GiB, oom_near_miss=${near}" "${task}"
  fi
}
