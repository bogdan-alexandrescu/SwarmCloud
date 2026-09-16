#!/usr/bin/env bash
# Shared assertions and platform helpers for the *-test.sh scripts.
#
# Deliberate split of responsibilities:
#
#   * WRITES go through the API, because that is the only supported way to
#     create work and the tests should exercise the real admission path.
#   * OBSERVATIONS read Firestore directly, because Firestore is the
#     authoritative store. Reading state from the same API that is under test
#     would let a broken API report success, and it couples these tests to a
#     response shape rather than to the state machine in CONTRACT.md.

set -euo pipefail

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0
FAILED_NAMES=()
SUITE_NAME="${SUITE_NAME:-suite}"
SUITE_STARTED="$(date -u +%s)"

t_case() {
  TESTS_RUN=$((TESTS_RUN + 1))
  printf '\n%s[%d] %s%s\n' "${C_BOLD}" "${TESTS_RUN}" "$*" "${C_RESET}" >&2
}

t_pass() {
  TESTS_PASSED=$((TESTS_PASSED + 1))
  printf '%s  PASS%s %s\n' "${C_GREEN}" "${C_RESET}" "$*" >&2
}

t_fail() {
  TESTS_FAILED=$((TESTS_FAILED + 1))
  FAILED_NAMES+=("$*")
  printf '%s  FAIL%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2
}

t_info() { printf '       %s\n' "$*" >&2; }

assert_eq() {
  local want="$1" got="$2" what="$3"
  if [[ "${want}" == "${got}" ]]; then
    t_pass "${what}: ${got}"
  else
    t_fail "${what}: expected '${want}', got '${got}'"
  fi
}

assert_le() {
  local value="$1" limit="$2" what="$3"
  if [[ "${value}" -le "${limit}" ]]; then
    t_pass "${what}: ${value} <= ${limit}"
  else
    t_fail "${what}: ${value} > ${limit}"
  fi
}

assert_ge() {
  local value="$1" floor="$2" what="$3"
  if [[ "${value}" -ge "${floor}" ]]; then
    t_pass "${what}: ${value} >= ${floor}"
  else
    t_fail "${what}: ${value} < ${floor}"
  fi
}

assert_true() {
  if "$@"; then t_pass "$*"; else t_fail "$*"; fi
}

t_summary() {
  local elapsed=$(( $(date -u +%s) - SUITE_STARTED ))
  hr
  if [[ "${TESTS_FAILED}" -eq 0 ]]; then
    ok "${SUITE_NAME}: ${TESTS_PASSED}/${TESTS_RUN} passed in ${elapsed}s"
    return 0
  fi
  err "${SUITE_NAME}: ${TESTS_FAILED} of ${TESTS_RUN} failed in ${elapsed}s"
  local name
  for name in ${FAILED_NAMES[@]+"${FAILED_NAMES[@]}"}; do
    printf '    - %s\n' "${name}" >&2
  done
  return 1
}

# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

require_platform() {
  require_cmd gcloud jq curl
  if ! fs_database_exists; then
    die "Firestore database '${FIRESTORE_DATABASE}' does not exist. Run 'make infra' first."
  fi
  if ! api_reachable; then
    die "the API at $(api_url) did not answer /healthz. Run 'make deploy', or set API_URL."
  fi
}

# submit_task PROFILE [INPUT_JSON] [EXTRA_JSON] -> task id on stdout
submit_task() {
  local profile="$1" input="${2:-{\}}" extra="${3:-{\}}"
  local body response id
  body="$(jq -nc --arg p "${profile}" --argjson i "${input}" --argjson x "${extra}" \
    '{runner_profile:$p, input:$i} + $x')"
  if ! response="$(api_post "/tasks" "${body}")"; then
    err "POST ${API_PREFIX}/tasks returned HTTP ${API_STATUS}"
    printf '%s\n' "${response}" | redact >&2
    return 1
  fi
  id="$(jq -r '.id // .task_id // .task.id // empty' <<<"${response}")"
  [[ -n "${id}" ]] || { err "no task id in the API response"; printf '%s\n' "${response}" | redact >&2; return 1; }
  printf '%s' "${id}"
}

cancel_task() {
  api_post "/tasks/$1/cancel" '{}' >/dev/null
}

# Authoritative read, straight from Firestore.
task_doc() { fs_get "tasks/$1" | jq -c "${FS_JQ} if .fields then doc else null end"; }
task_state() { task_doc "$1" | jq -r '.state // "MISSING"'; }
task_field() { task_doc "$1" | jq -r "$2"; }

pool_doc() { fs_get "pools/$1" | jq -c "${FS_JQ} if .fields then doc else null end"; }
pool_active() { pool_doc "$1" | jq -r '.active // 0'; }
pool_limit() { pool_doc "$1" | jq -r "${FS_JQ} effective_limit" 2>/dev/null || pool_doc "$1" | jq -r '.hard_limit // 0'; }

# Number of tasks currently holding capacity, from the state machine's own
# definition of CONCURRENCY_STATES.
holding_capacity() {
  local total=0 state n
  for state in LEASED DISPATCHED STARTING RUNNING; do
    n="$(fs_count tasks state EQUAL "${state}")"
    total=$(( total + n ))
  done
  printf '%s' "${total}"
}

active_leases() {
  fs_count_where leases "$(fs_null_filter released_at IS_NULL)"
}

# Predicates, so wait_until can be given a condition rather than a shell string.
holding_at_most() { [[ "$(holding_capacity)" -le "$1" ]]; }
pool_drained()    { [[ "$(pool_active "$1")" -eq 0 ]]; }
leases_at_most()  { [[ "$(active_leases)" -le "$1" ]]; }
task_is_terminal() {
  case "$(task_state "$1")" in
    SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED) return 0 ;;
    *) return 1 ;;
  esac
}
task_in_state() {
  local id="$1" wanted="$2" state
  state="$(task_state "${id}")"
  [[ "${state}" =~ ^(${wanted})$ ]]
}

# wait_for_state TASK_ID "A|B|C" TIMEOUT_SECONDS
wait_for_state() {
  local task_id="$1" wanted="$2" timeout="${3:-300}"
  local deadline=$(( $(date -u +%s) + timeout )) state
  while :; do
    state="$(task_state "${task_id}")"
    if [[ "${state}" =~ ^(${wanted})$ ]]; then
      printf '%s' "${state}"
      return 0
    fi
    if [[ "$(date -u +%s)" -ge "${deadline}" ]]; then
      printf '%s' "${state}"
      return 1
    fi
    sleep 2
  done
}

# wait_until DESCRIPTION TIMEOUT COMMAND...
wait_until() {
  local what="$1" timeout="$2"; shift 2
  local deadline=$(( $(date -u +%s) + timeout ))
  while :; do
    if "$@"; then return 0; fi
    if [[ "$(date -u +%s)" -ge "${deadline}" ]]; then
      t_info "timed out after ${timeout}s waiting for: ${what}"
      return 1
    fi
    sleep 2
  done
}

task_events() {
  fs_list "tasks/$1/events" 200 | jq -c "${FS_JQ} .[] | doc"
}

# Everything a test creates is tagged so cleanup can find it again and so a
# human reading Firestore can tell test traffic from real work.
test_run_id() { printf 'test-%s-%s' "${SUITE_NAME}" "$(date -u +%Y%m%d%H%M%S)"; }

cancel_all() {
  local id
  for id in "$@"; do
    [[ -n "${id}" ]] || continue
    cancel_task "${id}" >/dev/null 2>&1 || true
  done
}
