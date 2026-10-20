#!/usr/bin/env bash
# Acceptance group `tenant-isolation` (owner decision 2026-10-20, lane
# ISO-ACCEPT-WIRE, part of #1457): from inside a smoke worker, with that
# worker's OWN ambient credential, Firestore refuses another tenant's
# documents. The probe is
# tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py;
# its docstring says what it asks, why it writes nothing, and why the other
# tenant is one that does not exist.
#
# One `generic` pytest agent, started in the probe's own directory and naming
# the probe (the opt-in: the runner passes no input to an environment
# variable). Each of the probe's three checks writes one
# `ISO-ACCEPT-RESULT {json}` line to stdout and stderr; on exit 0 the runner keeps
# stdout's tail, on any other exit only stderr's tail, in last_error. A check
# passes only on its own PASS line AND an exit of 0. Anything else is a FAIL,
# a held agent included: an isolation proof that did not run is not a skip
# (accept.yml makes the group report-only until ISO_PROBE_REQUIRED is true).

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

#: Where the probe lands inside the agent's workspace (relative to work/, as
#: generic.sh's GENERIC_FIXTURE_DIR).
TENANT_ISOLATION_DIR="repo/tests/acceptance/fixtures/tenant-isolation"
TENANT_ISOLATION_PROBE="${TENANT_ISOLATION_DIR}/test_cross_tenant_firestore.py"

tenant_isolation_checks() {
  cat <<'EOF2'
tenant-isolation: settings: a smoke worker's update of another tenant's workspace_settings is denied
tenant-isolation: tenant: a smoke worker's get of another tenant's tenants document is denied
tenant-isolation: lease: a smoke worker's create under leases is denied
EOF2
}

_tenant_isolation_title() {
  tenant_isolation_checks | awk -v n="tenant-isolation: $1:" 'index($0, n) == 1 { print; exit }'
}

run_tenant_isolation() {
  step "Acceptance: tenant-isolation (fixture ${ACC_REPOSITORY_URL} @ ${ACC_REF})"
  local input task="" state="" text="" check verdicts line verdict reason
  ACC_CHECK="tenant-isolation"
  input="$(jq -nc --arg p "acceptance ${ACC_RUN_ID} tenant-isolation" \
    --arg w "${TENANT_ISOLATION_DIR}" --arg f "${TENANT_ISOLATION_PROBE}" \
    '{prompt: $p, command: "pytest", working_directory: $w, paths: [$f]}')"
  acc_submit task generic "${input}" "$(acc_repo_extra '{"max_attempts": 1}')" || task=""

  if [[ -n "${task}" ]]; then
    state="$(acc_settle "${task}" "${ACC_TIMEOUT}")" || true
    case "${state}" in
      SUCCEEDED) text="$(acc_output "${task}" '.stdout_tail // ""')" ;;
      FAILED|DEAD_LETTERED) text="$(task_field "${task}" '.last_error // ""')" ;;
      *) cancel_all "${task}" ;;
    esac
  fi
  # The probe's own lines, as check<TAB>verdict<TAB>reason. Reasons are
  # Firestore's status and message: no token reaches them (the probe's
  # docstring), and they still go through redact.
  verdicts="$(printf '%s\n' "${text}" | sed -n 's/.*ISO-ACCEPT-RESULT //p' \
    | jq -Rr 'fromjson? | select(type == "object") | [.check, .verdict, (.reason // "" | gsub("[\t\n]"; " "))] | @tsv' \
    | redact)"

  for check in settings tenant lease; do
    acc_check "$(_tenant_isolation_title "${check}")"
    if [[ -z "${task}" ]]; then
      acc_fail "not submitted"
      continue
    fi
    case "${state}" in
      SUCCEEDED|FAILED|DEAD_LETTERED) ;;
      *)
        acc_fail "not measured: the probe agent did not run to an end (${state:-no state}); an isolation proof that did not run is a FAIL, not a skip" "${task}"
        continue ;;
    esac
    line="$(printf '%s\n' "${verdicts}" | awk -F '\t' -v c="${check}" '$1 == c { print; exit }')"
    if [[ -z "${line}" ]]; then
      acc_fail "the probe wrote no result line for ${check} (ended ${state}): $(printf '%s' "${text}" | tail -n 3 | redact | tr '\n' ' ' | head -c 400)" "${task}"
      continue
    fi
    IFS=$'\t' read -r _ verdict reason <<<"${line}"
    if [[ "${verdict}" == "PASS" && "${state}" == "SUCCEEDED" ]]; then
      acc_pass "${reason}" "${task}"
    else
      acc_fail "${verdict} (agent ended ${state}): ${reason}" "${task}"
    fi
  done
}
