#!/usr/bin/env bash
# Acceptance group `generic`: every command in the generic runner's catalogue
# (GENERIC_COMMANDS, apps/agent-worker/agent_worker/runners/generic.py), run
# for real against tests/acceptance/fixtures/generic in a clone of this
# repository, and judged by its OUTPUT: the command the runner recorded, its
# exit code, and the marker the fixture prints. Then the door: arguments the
# runner would refuse must be refused by swarm-api before any task exists.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

#: Where the fixture lands inside the task's workspace: the worker clones the
#: task's repository into work/repo, and working_directory is relative to work/.
GENERIC_FIXTURE_DIR="repo/tests/acceptance/fixtures/generic"

generic_checks() {
  cat <<'EOF'
generic: pytest runs the fixture's three tests and reports 3 passed
generic: pytest with paths runs only the named test and reports 1 passed
generic: npm-ci installs from package-lock.json and exits 0
generic: npm-test runs the fixture's test script and prints its marker
generic: npm-build runs the fixture's build script and prints its marker
generic: make runs the named target and prints its marker
generic: uv-sync installs from uv.lock and exits 0
generic: bad arguments are refused at the door with a 422
EOF
}

# One row per command: CHECK-SUFFIX COMMAND EXTRA-INPUT-JSON EXPECTED-STDOUT
# (EXPECTED "-" means "exit 0 is the whole output": npm ci and uv sync print
# their progress to stderr, and what they print varies by version).
_generic_rows() {
  cat <<EOF
pytest|pytest|{}|3 passed
pytest with paths|pytest|{"paths":["${GENERIC_FIXTURE_DIR}/test_single.py"]}|1 passed
npm-ci|npm-ci|{}|-
npm-test|npm-test|{}|ACCEPTANCE-NPM-TEST-OK
npm-build|npm-build|{}|ACCEPTANCE-NPM-BUILD-OK
make|make|{"target":"acceptance"}|ACCEPTANCE-MAKE-OK
uv-sync|uv-sync|{}|-
EOF
}

run_generic() {
  step "Acceptance: generic (fixture ${ACC_REPOSITORY_URL} @ ${ACC_REF})"
  local rows=() ids=() line name command extra expect task input i
  while IFS= read -r line; do
    [[ -n "${line}" ]] && rows+=("${line}")
  done < <(_generic_rows)

  # Submit every command first, then judge each: the group costs one task's
  # wall clock, not seven.
  for line in "${rows[@]}"; do
    IFS='|' read -r name command extra expect <<<"${line}"
    ACC_CHECK="generic: ${name}"
    input="$(jq -nc --arg p "acceptance ${ACC_RUN_ID} ${name}" --arg c "${command}" \
      --arg w "${GENERIC_FIXTURE_DIR}" --argjson x "${extra}" \
      '{prompt: $p, command: $c, working_directory: $w} + $x')"
    task=""
    acc_submit task generic "${input}" "$(acc_repo_extra '{"max_attempts": 1}')" || task=""
    ids+=("${task}")
  done

  i=0
  for line in "${rows[@]}"; do
    IFS='|' read -r name command extra expect <<<"${line}"
    _generic_check "${name}" "${command}" "${expect}" "${ids[i]}"
    i=$(( i + 1 ))
  done

  _generic_door
}

_generic_title() {
  generic_checks | awk -v n="generic: $1 " 'index($0, n) == 1 { print; exit }'
}

_generic_check() {
  local name="$1" command="$2" expect="$3" task="$4" state tail summary
  acc_check "$(_generic_title "${name}")"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  if [[ "${state}" != "SUCCEEDED" ]]; then
    acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error"' | redact | tr '\n' ' ' | head -c 400)" "${task}"
    return 0
  fi
  acc_assert_eq "${command} 0" "$(acc_output "${task}" '"\(.command // "none") \(.exit_code // "none")"')" "the command the runner recorded, and its exit code" "${task}"
  [[ "${expect}" != "-" ]] || return 0
  tail="$(acc_output "${task}" '.stdout_tail // ""')"
  case "${command}" in
    pytest)
      if ! summary="$(printf '%s\n' "${tail}" | acc_pytest_summary)"; then
        acc_fail "no pytest summary line in the runner's stdout: $(printf '%s' "${tail}" | tail -n 3 | redact | tr '\n' ' ')" "${task}"
        return 0
      fi
      acc_assert_eq "passed=${expect%% *} failed=0 errors=0" "${summary}" "pytest summary" "${task}"
      ;;
    *)
      if printf '%s' "${tail}" | grep -qF "${expect}"; then
        acc_pass "stdout carries ${expect}" "${task}"
      else
        acc_fail "stdout does not carry ${expect}: $(printf '%s' "${tail}" | tail -n 3 | redact | tr '\n' ' ')" "${task}"
      fi
      ;;
  esac
}

_generic_door() {
  local probe answer case_input label
  acc_check "generic: bad arguments are refused at the door with a 422"
  # Whether this deployment declares generic's inputs at all (contract request
  # 32, #218 / PR #345). Before it, swarm-api checks a generic input's SIZE
  # only, and a bad argument becomes a task the runner refuses -- so there is
  # no door to test yet, and the honest answer is a skip naming why.
  local rc=0
  probe='{"prompt":"acceptance door probe","command":"not-a-catalogue-command"}'
  acc_declares generic "${probe}" || rc=$?
  case "${rc}" in
    0) ;;
    1)
      acc_skip "not measured: this deployment does not declare generic's inputs yet (contract request 32, #218/PR #345), so a bad argument is not checked at the door"
      return 0 ;;
    *)
      acc_fail "the declaration probe answered neither invalid_input nor invalid_dispatch; see above"
      return 0 ;;
  esac
  while IFS='|' read -r label case_input; do
    [[ -n "${label}" ]] || continue
    answer="$(acc_door generic "${case_input}")"
    acc_assert_eq "422 invalid_input" "${answer}" "${label}" ""
  done <<EOF
a command not in the catalogue|{"prompt":"x","command":"rm"}
no command at all|{"prompt":"x"}
a path that is a flag|{"prompt":"x","command":"pytest","paths":["-rf"]}
a path that escapes the workspace|{"prompt":"x","command":"pytest","paths":["../../etc/passwd"]}
a make target that is a flag|{"prompt":"x","command":"make","target":"--eval=all:;id"}
a working_directory that escapes|{"prompt":"x","command":"pytest","working_directory":"../.."}
an argv supplied with the task|{"prompt":"x","command":"pytest","argv":["bash","-c","id"]}
a timeout above the platform's|{"prompt":"x","command":"pytest","timeout_seconds":999999}
EOF
}
