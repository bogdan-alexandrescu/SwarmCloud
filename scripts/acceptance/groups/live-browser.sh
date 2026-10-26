#!/usr/bin/env bash
# Acceptance group `live-browser`: a person takes control of an agent's
# browser, signs in, and gives it back (docs/design/live-browser.md section 9,
# lane LB-L13; docs/acceptance.md, "live-browser").
#
# ONE claude-code-browser TASK. The agent serves
# tests/acceptance/fixtures/login.html on 127.0.0.1 in its own pod, opens it
# in the platform's Chromium through $SWARM_CDP_ENDPOINT, and asks for help
# with `swarm-handoff`. A scripted viewer --
# tests/acceptance/test_live_browser.py, standard library only -- does what
# the console does: a viewer ticket, wss to swarm-api at
# /v1/live/<session>/stream with the ticket as its first message, Take
# control, a click and a password typed as RFB key events, Return control.
# The password is generated INSIDE that process at run time and never leaves
# it: not this shell, not an argv, not the environment, not a log.
#
# THIS FILE OWNS THE TASK; THE PYTHON OWNS THE PERSON. The task is submitted
# here and waited on with acc_settle, so a closed pool or a missing
# credential is the same SKIP as in every other group. The viewer runs
# beside that wait, in the background, and is told the settled state through
# a file in its control directory. Its verdicts come back one JSON line per
# check and are reported here, in this file's order.
#
# CREDENTIALS are handed over as files under ACC_WORK (mode 0700), each read
# once and deleted by the Python: the API credential common.sh resolves
# (api_credential: an ID token in the VPC, IAP's access token at the front
# door) and the GCS access token the sweep reads the artifact bucket with.
#
# ONE TASK AT A TIME, so this group's peak in the smoke tenant is 1.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

#: The verdict keys tests/acceptance/test_live_browser.py writes (its CHECKS),
#: in the order of live_browser_checks below. A unit test holds them equal.
LIVE_BROWSER_KEYS=(handoff isolation frames recording returned no_leak)

live_browser_checks() {
  cat <<'EOF'
live-browser: a claude-code-browser agent on the fixture sign-in page asks for help, and its live session shows the hand-off
live-browser: a member of another tenant gets 404 from every live route, and a ticket re-pointed at that tenant is refused on the socket
live-browser: a scripted viewer takes control and types a password made at run time; every frame it receives shows the field as drawn
live-browser: the recording's frames show the password field as drawn
live-browser: control returns, the agent reports the code only the typed password makes, and the task succeeds
live-browser: the password is in no transcript, log, checkpoint, output, recording or event
EOF
}

#: What the agent is asked. The steps are spelled out because the check
#: depends on each: the port, the page, never typing, asking, waiting again on
#: 75, and the refusal's code when there is no live path. $SWARM_ARTIFACTS_DIR
#: is the agent's to expand, so it stays literal here.
# shellcheck disable=SC2016
LIVE_BROWSER_PROMPT='Serve the fixture sign-in page and have a person sign in on it; never type into the page yourself. 1) Run: cd tests/acceptance/fixtures && nohup python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 & 2) In a Python script using Playwright, connect to the platform browser with p.chromium.connect_over_cdp(os.environ["SWARM_CDP_ENDPOINT"]), take browser.contexts[0] and its first page (or a new page), goto http://127.0.0.1:8765/login.html, and end the script without closing the page or the browser. 3) Run: swarm-handoff request --reason "Sign in on the acceptance fixture page". If it exits 5, reply with only HANDOFF-REFUSED followed by the "code" field of the JSON it printed, and stop. 4) Run: swarm-handoff wait, and run it again each time it exits 75. 5) When it exits 0, connect again the same way, read the inner text of #code on that page, write it alone to $SWARM_ARTIFACTS_DIR/session-code.txt, and reply with only that text.'

# live_browser_stuck DOC -> the state of a task acc_settle stopped waiting on,
# with why it was waiting: `PARKED:<park_reason>`, the shape acc_settle
# already prints for CREDENTIAL_MISSING, and `:eligible again at <when>` when
# the park names an instant. The viewer's verdict names both. A bare `PARKED`
# was all the scheduled runs of 2026-10-15..19 could say (#1333): a hand-off
# whose hold ran out unwatched parks on HUMAN_REQUIRED, which only a person
# promotes, and a provider park comes back on its own.
live_browser_stuck() {
  jq -j '(.state // "MISSING") as $s
    | [$s]
      + (if $s == "PARKED" and (.park_reason // "") != "" then [.park_reason] else [] end)
      + (if $s == "PARKED" and (.next_eligible_at // "") != ""
         then ["eligible again at \(.next_eligible_at)"] else [] end)
    | join(":")' <<<"$1"
}

run_live_browser() {
  step "Acceptance: live-browser"
  local task="" state="" doc dir pid rc=0 i key line verdict reason empty='{}'
  local names=()
  while IFS= read -r line; do
    names+=("${line}")
  done < <(live_browser_checks)
  [[ "${#names[@]}" -eq "${#LIVE_BROWSER_KEYS[@]}" ]] \
    || die "live-browser: ${#names[@]} checks listed, ${#LIVE_BROWSER_KEYS[@]} verdict keys"

  acc_check "${names[0]}"
  acc_submit task claude-code-browser "$(jq -nc --arg p "${LIVE_BROWSER_PROMPT}" '{prompt: $p}')" \
    "$(acc_repo_extra '{"max_attempts": 1}')" || task=""
  if [[ -z "${task}" ]]; then
    for (( i = 1; i < ${#names[@]}; i++ )); do
      acc_check "${names[i]}"
      acc_skip "not measured: the task was not created (see the first check)"
    done
    return 0
  fi

  dir="${ACC_WORK}/live-browser"
  mkdir -p "${dir}"
  chmod 700 "${dir}"
  ( umask 077; api_credential >"${dir}/credential" )
  if ! ( umask 077; access_token >"${dir}/gcs-token" ) 2>/dev/null; then
    rm -f "${dir}/gcs-token"
    warn "live-browser: no access token for ${ARTIFACT_BUCKET}; the sweep and the recording check will FAIL"
  fi

  local args=(--task "${task}" --tenant "${ACC_TENANT}" --api "$(api_url)"
              --bucket "${ARTIFACT_BUCKET}" --control-dir "${dir}" --results "${dir}/results.jsonl"
              --credential-file "${dir}/credential" --timeout "${ACC_TIMEOUT}")
  [[ ! -f "${dir}/gcs-token" ]] || args+=(--gcs-token-file "${dir}/gcs-token")
  [[ -z "${SWARM_ACCEPTANCE_OTHER_TENANT:-}" ]] || args+=(--other-tenant "${SWARM_ACCEPTANCE_OTHER_TENANT}")

  python3 "${REPO_ROOT}/tests/acceptance/test_live_browser.py" "${args[@]}" &
  pid=$!

  if ! state="$(acc_settle "${task}" "${ACC_TIMEOUT}")"; then
    # The wait ran out. Which park, if any, is the verdict's reason.
    if doc="$(task_doc "${task}")"; then
      state="$(live_browser_stuck "${doc}")"
    fi
  fi
  printf '%s\n' "${state}" >"${dir}/settled"
  wait "${pid}" || rc=$?
  [[ "${rc}" -eq 0 ]] || warn "live-browser: the viewer exited ${rc}"
  rm -f "${dir}/credential" "${dir}/gcs-token"

  for (( i = 0; i < ${#names[@]}; i++ )); do
    key="${LIVE_BROWSER_KEYS[i]}"
    acc_check "${names[i]}"
    line="$(jq -c --arg k "${key}" 'select(.check == $k)' "${dir}/results.jsonl" 2>/dev/null | head -n 1 || true)"
    verdict="$(jq -r '.verdict // empty' <<<"${line:-$empty}" 2>/dev/null || true)"
    reason="$(jq -r '.reason // empty' <<<"${line:-$empty}" 2>/dev/null | redact || true)"
    case "${verdict}" in
      PASS) acc_pass "${reason}" "${task}" ;;
      SKIP) acc_skip "${reason}" "${task}" ;;
      FAIL) acc_fail "${reason}" "${task}" ;;
      *) acc_fail "the viewer wrote no verdict for this check (it exited ${rc})" "${task}" ;;
    esac
  done
  cancel_all "${task}"
}
