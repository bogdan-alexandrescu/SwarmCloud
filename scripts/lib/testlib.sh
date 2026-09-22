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

# A numeric assertion whose operand was never measured is not a pass.
#
# Both helpers below are reached as `assert_le "$(pool_active global)" N ...`
# (failure-test.sh:162, load-test.sh:150). A command substitution used as an
# ARGUMENT swallows its own exit status -- `set -e` cannot fire there -- so a
# failed read arrived as the empty string, and `[[ "" -le N ]]` is TRUE in
# bash. The assertion passed green having measured nothing.
#
# Guarding the callers is not enough, because the substitution is evaluated
# before this function is entered; the check has to be here, where the value
# actually lands.
_assert_numeric() {
  local value="$1" what="$2" role="$3"
  if [[ -z "${value}" ]]; then
    t_fail "${what}: the ${role} could not be read (empty). Refusing to compare -- an unread value is not a passing one."
    return 1
  fi
  if [[ ! "${value}" =~ ^-?[0-9]+$ ]]; then
    t_fail "${what}: the ${role} is not a number ('${value}'). Refusing to compare."
    return 1
  fi
  return 0
}

assert_le() {
  local value="$1" limit="$2" what="$3"
  _assert_numeric "${value}" "${what}" "measured value" || return 0
  _assert_numeric "${limit}" "${what}" "limit" || return 0
  if [[ "${value}" -le "${limit}" ]]; then
    t_pass "${what}: ${value} <= ${limit}"
  else
    t_fail "${what}: ${value} > ${limit}"
  fi
}

assert_ge() {
  local value="$1" floor="$2" what="$3"
  _assert_numeric "${value}" "${what}" "measured value" || return 0
  _assert_numeric "${floor}" "${what}" "floor" || return 0
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
  # NOT gcloud. The suites reach Cloud Run and GCS over REST via
  # cloud_run_service_uri and gcs_object_count, so they run in an image with
  # no Cloud SDK -- whose base ships unfixed HIGH/CRITICAL CVEs that `make
  # push` rightly refuses to promote.
  require_cmd jq curl
  # Three answers, not two. This guard used to run `gcloud firestore databases
  # describe` with its stderr discarded, in an image that carries no Cloud SDK
  # on purpose -- so every in-VPC run of this gate died here with "does not
  # exist. Run 'make infra' first" against a database holding live tenants.
  # fs_database_exists is REST now and separates absent from unreadable.
  require_fs_database "run ${SUITE_NAME:-the verification suites}"

  # Not api_reachable(): it runs curl with stderr sent to /dev/null and reads
  # grep's exit status rather than curl's, so a 403 (missing roles/run.invoker),
  # a 401 (expired session) and a genuinely undeployed service all collapse to
  # the same "did not answer" verdict below. Probe directly, the same way
  # api_url() does above it in common.sh, so the die message names the real
  # cause instead of sending every one of those cases at 'make deploy'.
  #
  # api_credential, not id_token: the front door is behind IAP and refuses a
  # Google ID token whatever its audience. And through `-K -`, not argv --
  # this line used to interpolate the token straight into curl's command line,
  # where every process on the box can read it out of ps.
  local ready_err ready_status detail
  ready_err="$(mktemp "${TMPDIR:-/tmp}/swarm-readyz.XXXXXX")"
  if ! ready_status="$(auth_config "$(api_credential)" \
        | curl -sS -m 10 -K - -o /dev/null -w '%{http_code}' \
        "$(api_url)/readyz" 2>"${ready_err}")"; then
    detail="$(cat "${ready_err}")"
    rm -f "${ready_err}"
    die_if_auth_failure "${detail}"
    err "curl could not reach $(api_url)/readyz"
    printf '%s\n' "${detail}" | redact | head -n 3 | sed 's/^/     /' >&2
    die "that is a transport failure, not proof the API is undeployed. Check network access and API_URL before running 'make deploy'."
  fi
  rm -f "${ready_err}"

  case "${ready_status}" in
    2*) ;;
    401|403)
      die "the API at $(api_url)/readyz answered HTTP ${ready_status}. That is an auth/IAM problem -- an expired session, the caller missing roles/run.invoker, or IAP refusing the credential -- not a missing deployment. Do not run 'make deploy' for this."
      ;;
    404)
      # 404 HAS ITS OWN CASE BECAUSE IT IS THE ONE THAT LIES.
      #
      # This gate used to fold it into the catch-all below and print "rule out
      # auth/IAM before running 'make deploy'", which is advice that cannot
      # help: the request never reached the application. A Cloud Run service
      # whose ingress is internal-and-cloud-load-balancing answers every
      # external request 404 from Google's frontend -- /readyz, /v1/anything
      # and a route that does not exist all return the identical 272-byte HTML
      # page. Measured against the live deployment on 2026-09-22, while the
      # same routes served through the load balancer.
      #
      # common.sh's api_url refuses that address now, so reaching here means
      # something set API_URL to it by hand, or the front door itself is
      # serving a 404.
      err "the API at $(api_url)/readyz answered HTTP 404."
      err "A 404 here is almost never a missing route. Cloud Run's frontend answers 404 to"
      err "every external request for a service whose ingress is not 'all' -- so this is the"
      err "wrong ADDRESS, not a broken deployment and not an IAM problem."
      err "Check: is API_URL pinned to the *.run.app hostname? From outside the VPC it cannot"
      err "serve. Unset it and let api_url resolve the load balancer, or set API_HOST."
      die "do not run 'make deploy' for this; nothing about the deployment is wrong."
      ;;
    *)
      die "the API at $(api_url)/readyz answered HTTP ${ready_status}, not 2xx. Rule out auth/IAM before running 'make deploy' -- this was not a transport failure, not a 401/403 and not a 404."
      ;;
  esac
}

# submit_task PROFILE [INPUT_JSON] [EXTRA_JSON] -> task id on stdout
submit_task() {
  local profile="$1" input="${2:-{\}}" extra="${3:-{\}}"
  local body response id out
  body="$(jq -nc --arg p "${profile}" --argjson i "${input}" --argjson x "${extra}" \
    '{runner_profile:$p, input:$i} + $x')"
  # api_post is called directly here, NOT as `response="$(api_post ...)"`: that
  # form runs api_post in a subshell, so the API_STATUS it sets dies with the
  # subshell and this function would print whatever API_STATUS held before this
  # call ran -- the exact command-substitution trap CLAUDE.md documents.
  # Redirect to a file instead, as fs_request/api_url already do.
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-submit.XXXXXX")"
  if ! api_post "/tasks" "${body}" >"${out}"; then
    response="$(cat "${out}")"
    rm -f "${out}"
    err "POST ${API_PREFIX}/tasks returned HTTP ${API_STATUS}"
    printf '%s\n' "${response}" | redact >&2
    return 1
  fi
  response="$(cat "${out}")"
  rm -f "${out}"
  id="$(jq -r '.id // .task_id // .task.id // empty' <<<"${response}")"
  [[ -n "${id}" ]] || { err "no task id in the API response"; printf '%s\n' "${response}" | redact >&2; return 1; }
  printf '%s' "${id}"
}

cancel_task() {
  api_post "/tasks/$1/cancel" '{}' >/dev/null
}

# Authoritative read, straight from Firestore.
#
# `|| return 1`, not bare `set -e`: wait_for_state below, and wait_until's
# predicates, are called as the condition of an `if` in the *-test.sh scripts
# (`if final="$(wait_for_state ...)"`, `if wait_until ... task_in_state ...`),
# and set -e is suppressed for the whole call tree evaluated as that condition
# -- see common.sh's fs_list for the same note. Without an explicit check here,
# a real Firestore error (403, 500, a dropped connection) is silently read as
# "no matching state yet" and the caller polls it for the full timeout instead
# of failing on the first read.
task_doc() { fs_get "tasks/$1" | jq -c "${FS_JQ} if .fields then doc else null end" || return 1; }
task_state() {
  local doc
  doc="$(task_doc "$1")" || return 1
  printf '%s' "${doc}" | jq -r '.state // "MISSING"'
}
task_field() {
  local doc
  doc="$(task_doc "$1")" || return 1
  printf '%s' "${doc}" | jq -r "$2"
}

# `|| return 1` on every read, for the reason task_doc above already carries
# it -- these three were the ones that did not, and the omission reached the
# assertions.
#
# A failed read makes these print the EMPTY STRING, and `[[ "" -eq 0 ]]` and
# `[[ "" -le N ]]` are both TRUE in bash (confirmed on 3.2.57 and on the
# Alpine bash the verify image ships). The predicates below then answer "the
# pool is drained" and "capacity is back at baseline" having never read
# anything. wait_until calls them as `if "$@"`, which discards the status, so
# nothing downstream could notice either.
#
# The reachable consequence was in race-test.sh:203, polled the instant after
# every cancellation is fired in parallel -- exactly when Firestore is most
# likely to answer 429. One failed read there printed
# `PASS  runner:mock active is 0` and exited 0, certifying that no lease
# survived the cancellation storm without having looked at the pool.
#
# A 401 is no safer despite fs_request routing it through die: the die runs in
# the left-hand side of the pipeline, which is a subshell, so only the subshell
# exits and the false PASS still happens.
pool_doc() { fs_get "pools/$1" | jq -c "${FS_JQ} if .fields then doc else null end" || return 1; }
pool_active() {
  local doc
  doc="$(pool_doc "$1")" || return 1
  # A pool document that does not exist is NOT a drained pool. fs_get opts into
  # 404, and `null | .active // 0` is 0, so an absent pool used to read as
  # empty -- the same conflation, one level down.
  [[ -n "${doc}" && "${doc}" != "null" ]] || return 1
  printf '%s' "${doc}" | jq -r '.active // 0'
}
pool_limit() { pool_doc "$1" | jq -r "${FS_JQ} effective_limit" 2>/dev/null || pool_doc "$1" | jq -r '.hard_limit // 0'; }

# Number of tasks currently holding capacity, from the state machine's own
# definition of CONCURRENCY_STATES.
holding_capacity() {
  local total=0 state n
  for state in LEASED DISPATCHED STARTING RUNNING; do
    # Unchecked, a failed count folded in as 0 and UNDERSTATED the total --
    # the direction that turns a real leak into a pass.
    n="$(fs_count tasks state EQUAL "${state}")" || return 1
    [[ -n "${n}" ]] || return 1
    total=$(( total + n ))
  done
  printf '%s' "${total}"
}

active_leases() {
  local n
  n="$(fs_count_where leases "$(fs_null_filter released_at IS_NULL)")" || return 1
  [[ -n "${n}" ]] || return 1
  printf '%s' "${n}"
}

# Predicates, so wait_until can be given a condition rather than a shell string.
#
# Each captures the read FIRST and fails the predicate when the read failed,
# rather than letting the empty string fall into a numeric comparison that is
# true for every bound. A failed read now means "not satisfied, keep polling",
# and if it persists the wait times out loudly -- which is the honest outcome.
holding_at_most() {
  local n; n="$(holding_capacity)" || return 1
  [[ -n "${n}" ]] || return 1
  [[ "${n}" -le "$1" ]]
}
pool_drained() {
  local n; n="$(pool_active "$1")" || return 1
  [[ -n "${n}" ]] || return 1
  [[ "${n}" -eq 0 ]]
}
leases_at_most() {
  local n; n="$(active_leases)" || return 1
  [[ -n "${n}" ]] || return 1
  [[ "${n}" -le "$1" ]]
}
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
    # Checked explicitly, not `state="$(task_state ...)"` on its own: callers
    # invoke wait_for_state as an `if` condition
    # (`if final="$(wait_for_state ...)"`), which suppresses set -e for
    # everything evaluated underneath it, including this loop. Without this
    # check a real Firestore error here reads as "not in the wanted state yet"
    # and burns the full timeout before reporting a misleading state.
    if ! state="$(task_state "${task_id}")"; then
      die "could not read the state of task ${task_id}; see the Firestore error above -- this is a failed read, not evidence the task never reached ${wanted}"
    fi
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
