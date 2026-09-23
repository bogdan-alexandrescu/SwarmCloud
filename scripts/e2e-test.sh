#!/usr/bin/env bash
# The seams, end to end, against a deployed environment.
#
# The other suites here check PROPERTIES of the platform -- a pool is never over
# its limit, a failure leaks no capacity, the last free slot goes to one task.
# This one checks that the halves of the platform are actually JOINED, because
# that is what has been breaking: over three days every defect found by running
# something was a seam where both ends existed and nothing had ever executed the
# middle. An agent that could not be told where to write its output. Five spend
# fields written by the worker and never read by the API. A broker call with a
# keyword the callee does not take. A handler reading app state nothing set.
#
# Each check below is the one that would have caught one of those, and each is
# written so the FAILING version of the platform does not satisfy it:
#
#   1 handoff        a two-step workflow, asserted on the BYTES that reached
#                    the downstream workspace -- not on a SUCCEEDED state, which
#                    the broken platform also produced;
#   2 provenance     an artifact the WORKLOAD produced, named apart from the
#                    files the runner writes for itself -- `artifacts != []`
#                    was true throughout the outage;
#   3 spend          token counts and cost compared between Firestore and the
#                    HTTP API, present-when-present, on every attempt that has
#                    any -- the passing test that missed this asserted only
#                    absent-when-absent, which the bug satisfied;
#   4 sign-in        /v1/accounts/authorize answers with a usable URL and a
#                    state and leaks no verifier;
#   5 agreement      Firestore, the API and the Cloud Run execution say the same
#                    thing about a live task, or the disagreement is REPORTED;
#   6 negative       a step promised a file nobody wrote must NOT succeed. This
#                    is the suite's own proof that check 1 can fail.
#
# WHAT THIS SUITE DELIBERATELY CANNOT PROVE, stated here rather than left for a
# reader to assume: the `mock` profile's runner IS its own workload, so a mock
# run cannot distinguish a file written by an agent CHILD PROCESS from one
# written by the runner itself -- which is the precise distinction the
# SWARM_ARTIFACTS_DIR defect turned on. That distinction is proved offline, with
# a real agent child on the far side of run_child, in
# tests/unit/worker/test_agent_seam_end_to_end.py. Check 2 here proves the rest
# of the chain: harvest, manifest, GCS prefix and the HTTP read path.
#
# Nothing here writes to Firestore. Everything it creates is a task or a
# workflow submitted through the API, and every one of them is cancelled on the
# way out.
#
# Usage: scripts/e2e-test.sh [--timeout 900] [--keep] [--require-spend]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="e2e"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

PROFILE="mock"
TIMEOUT=900
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)       PROFILE="$2"; shift 2 ;;
    --timeout)       TIMEOUT="$2"; shift 2 ;;
    --keep)          KEEP=1; shift ;;
    # Turns every "not measured" into a failure. Right for a deployment where
    # the evidence is guaranteed to exist; wrong as a default, because a fresh
    # project has never run anything that reports spend and blaming it for that
    # teaches an operator to ignore the suite.
    --require-spend) SUITE_SKIPS_ARE_FAILURES=1; shift ;;
    -h|--help)       sed -n '2,46p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "End-to-end seams: ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform
info "api ${API_URL:-$(api_url)}"

NONCE="$(date -u +%Y%m%d%H%M%S)-$$"
HANDOFF_NAME="handoff-${NONCE}.txt"
# Byte-exact and deliberately plain ASCII. Two constraints on the content:
# a body mentioning a rate limit would be parked rather than completed, and a
# body carrying anything key-shaped would come back redacted and the byte
# comparison would then be asserting the wrong thing.
HANDOFF_TEXT="e2e handoff ${NONCE}"$'\n'"second line"$'\n'
HANDOFF_BYTES="$(printf '%s' "${HANDOFF_TEXT}" | wc -c | tr -d '[:space:]')"
MISSING_NAME="never-written-${NONCE}.txt"

TASK_IDS=()
cleanup() {
  [[ "${KEEP}" -eq 1 ]] && { info "--keep: leaving ${#TASK_IDS[@]} task(s) in place"; return 0; }
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
t_case "A two-step workflow with input_from is accepted"
# `input_from` is validated against the DAG, so the producer must also be a
# declared dependency. Both facts are sent, and the API's own validation is part
# of what this step exercises.
WF_BODY="$(jq -nc \
  --arg profile "${PROFILE}" \
  --arg name "${HANDOFF_NAME}" \
  --arg text "${HANDOFF_TEXT}" \
  --arg nonce "${NONCE}" '
  {
    steps: [
      { step_id: "produce",
        runner_profile: $profile,
        input: { prompt: "produce the handoff", steps: 1, sleep_seconds: 1,
                 artifact_name: $name, artifact_text: $text } },
      { step_id: "consume",
        runner_profile: $profile,
        depends_on: ["produce"],
        input_from: { produce: $name },
        input: { prompt: "consume the handoff", steps: 1, sleep_seconds: 1 } }
    ],
    metadata: { source: "e2e-test", run_id: $nonce }
  }')"
if ! WF="$(submit_workflow "${WF_BODY}")"; then
  die "the workflow was not accepted; every check below depends on it"
fi
WF_ID="$(jq -r '.workflow.workflow_id' <<<"${WF}")"
PRODUCE_ID="$(workflow_step_task "${WF}" produce)"
CONSUME_ID="$(workflow_step_task "${WF}" consume)"
[[ -n "${PRODUCE_ID}" && -n "${CONSUME_ID}" ]] \
  || die "the workflow response named no task for one of its steps: $(printf '%s' "${WF}" | redact | head -c 400)"
TASK_IDS+=("${PRODUCE_ID}" "${CONSUME_ID}")
t_info "workflow ${WF_ID}  produce=${PRODUCE_ID}  consume=${CONSUME_ID}"
t_pass "workflow accepted with both steps materialised as tasks"

# ---------------------------------------------------------------------------
t_case "Both steps run to completion"
TERMINAL="SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED"
if PRODUCE_STATE="$(wait_for_state "${PRODUCE_ID}" "${TERMINAL}" "${TIMEOUT}")"; then
  assert_eq "SUCCEEDED" "${PRODUCE_STATE}" "produce terminal state"
else
  t_fail "produce did not reach a terminal state within ${TIMEOUT}s (stuck at ${PRODUCE_STATE})"
fi
if CONSUME_STATE="$(wait_for_state "${CONSUME_ID}" "${TERMINAL}" "${TIMEOUT}")"; then
  assert_eq "SUCCEEDED" "${CONSUME_STATE}" "consume terminal state"
else
  t_fail "consume did not reach a terminal state within ${TIMEOUT}s (stuck at ${CONSUME_STATE})"
  t_info "blocked_by: $(task_field "${CONSUME_ID}" '.blocked_by // [] | tostring')"
  t_info "last_error: $(task_field "${CONSUME_ID}" '.last_error // "none"')"
fi

# ---------------------------------------------------------------------------
t_case "The producer's output is in the manifest, apart from the runner's own files"
#
# WHY THE SET DIFFERENCE. During the SWARM_ARTIFACTS_DIR outage every attempt's
# manifest held exactly three entries -- the runner's stdout, its stderr and its
# transcript -- so `artifacts | length > 0` was true on a platform where no
# workload could produce anything at all. The only assertion that distinguishes
# the two is one that names the file the WORKLOAD was asked for.
PRODUCE_SUMMARY="$(task_field "${PRODUCE_ID}" '.result_summary // {} | tostring')"
HANDOFF_ENTRY="$(jq -c --arg n "${HANDOFF_NAME}" \
  '[ (.artifacts // [])[] | select(.name == $n) ] | first // null' <<<"${PRODUCE_SUMMARY}")"
if [[ "${HANDOFF_ENTRY}" == "null" || -z "${HANDOFF_ENTRY}" ]]; then
  t_fail "the producer's manifest has no entry named ${HANDOFF_NAME}"
  t_info "manifest: $(jq -c '[ (.artifacts // [])[].name ]' <<<"${PRODUCE_SUMMARY}")"
else
  assert_eq "${HANDOFF_BYTES}" "$(jq -r '.bytes // -1' <<<"${HANDOFF_ENTRY}")" \
    "${HANDOFF_NAME} size in the manifest"
  # The URI must point inside the producer's OWN tenant/task/attempt prefix.
  # A manifest entry pointing anywhere else is how a handoff would cross a
  # tenant boundary, and `inputs.artifact_reference` refuses exactly that.
  URI="$(jq -r '.uri // ""' <<<"${HANDOFF_ENTRY}")"
  case "${URI}" in
    *"/tasks/${PRODUCE_ID}/attempts/"*"/artifacts/${HANDOFF_NAME}")
      t_pass "manifest URI is inside the producer's own attempt prefix" ;;
    *) t_fail "manifest URI is not the producer's own attempt prefix: ${URI}" ;;
  esac
fi
# Runner-written files are present too. Asserted so the check above is known to
# be a DIFFERENCE and not simply a manifest that happens to have one entry.
LOG_COUNT="$(jq -r '(.logs // {}) | length' <<<"${PRODUCE_SUMMARY}")"
assert_ge "${LOG_COUNT}" 1 "runner-written log streams recorded alongside the workload's artifact"
t_info "the mock runner IS its own workload, so this suite cannot tell a runner-written"
t_info "artifact from an agent-written one. tests/unit/worker/test_agent_seam_end_to_end.py"
t_info "makes that distinction, with a real agent child process."

# ---------------------------------------------------------------------------
t_case "The same artifact is reachable through the HTTP API"
ART_BODY="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-art.XXXXXX")"
if api_fetch "/tasks/${PRODUCE_ID}/artifacts" "${ART_BODY}"; then
  # `complete` is the field that stops an empty list reading as an answer.
  # Compared explicitly against true: `.complete // true` would report a
  # still-running task as finished, which is the jq alternative-operator trap.
  if [[ "$(jq -r '.complete' <"${ART_BODY}")" == "true" ]]; then
    t_pass "the artifact listing reports itself complete"
  else
    t_fail "the artifact listing is not complete for a task in ${PRODUCE_STATE}"
  fi
  API_ENTRY="$(jq -c --arg n "${HANDOFF_NAME}" \
    '[ (.artifacts // [])[] | select(.name == $n) ] | first // null' <"${ART_BODY}")"
  if [[ "${API_ENTRY}" == "null" ]]; then
    t_fail "GET ${API_PREFIX}/tasks/${PRODUCE_ID}/artifacts does not list ${HANDOFF_NAME}"
    t_info "it listed: $(jq -c '[ (.artifacts // [])[].name ]' <"${ART_BODY}")"
  else
    assert_eq "${HANDOFF_BYTES}" "$(jq -r '.bytes // -1' <<<"${API_ENTRY}")" \
      "${HANDOFF_NAME} size over HTTP"
  fi
  # No download URL is minted by this route on purpose: the caller reads GCS
  # with its own credentials, which keeps the tenant boundary in IAM.
  if jq -e '[ (.artifacts // [])[] | select(has("signed_url") or has("download_url")) ] | length > 0' \
      <"${ART_BODY}" >/dev/null; then
    t_fail "the artifact listing minted a download URL; that moves the tenant boundary out of IAM"
  else
    t_pass "no download URL minted in the artifact listing"
  fi
else
  t_fail "could not read the artifact listing over HTTP"
fi
rm -f "${ART_BODY}"

# ---------------------------------------------------------------------------
t_case "The downstream step received the CONTENT, not merely a green state"
#
# THE ASSERTION THAT MATTERS IN THIS WHOLE SUITE. A broken handoff still reaches
# SUCCEEDED at step 1, and a downstream step that received nothing can still
# reach SUCCEEDED too if the staging is allowed to warn and continue. What
# cannot be faked is the platform's own record of which bytes arrived: the size
# is re-measured against what actually landed on disk, not against what the
# upstream claimed, and the URI names the attempt they came from.
CONSUME_SUMMARY="$(task_field "${CONSUME_ID}" '.result_summary // {} | tostring')"
STAGED="$(jq -c --arg n "${HANDOFF_NAME}" \
  '[ (.staged_inputs // [])[] | select(.filename == $n) ] | first // null' <<<"${CONSUME_SUMMARY}")"
if [[ "${STAGED}" == "null" || -z "${STAGED}" ]]; then
  t_fail "the consumer recorded no staged input named ${HANDOFF_NAME}"
  t_info "staged_inputs: $(jq -c '.staged_inputs // []' <<<"${CONSUME_SUMMARY}")"
  t_info "this is the shape of the outage: a green upstream and an empty handoff"
else
  assert_eq "${PRODUCE_ID}" "$(jq -r '.task_id // ""' <<<"${STAGED}")" \
    "the staged file came from the producer"
  assert_eq "${HANDOFF_BYTES}" "$(jq -r '.bytes // -1' <<<"${STAGED}")" \
    "bytes that arrived in the consumer's workspace"
  assert_eq "${HANDOFF_NAME}" "$(jq -r '.path // ""' <<<"${STAGED}")" \
    "path in the consumer's working directory"
  STAGED_URI="$(jq -r '.uri // ""' <<<"${STAGED}")"
  case "${STAGED_URI}" in
    *"/tasks/${PRODUCE_ID}/attempts/"*"/artifacts/${HANDOFF_NAME}")
      t_pass "the bytes were fetched from the producer's own successful attempt" ;;
    *) t_fail "the staged input came from an unexpected location: ${STAGED_URI}" ;;
  esac
fi

# ---------------------------------------------------------------------------
t_case "NEGATIVE CONTROL: a step promised a file nobody wrote must not succeed"
#
# Without this, every assertion above is satisfied by a platform that stages
# nothing and reports success anyway -- which is precisely the failure mode the
# suite exists to catch, so the suite has to demonstrate it can catch it. The
# producer writes one name; the consumer declares another.
NEG_BODY="$(jq -nc \
  --arg profile "${PROFILE}" \
  --arg name "${HANDOFF_NAME}" \
  --arg missing "${MISSING_NAME}" \
  --arg text "${HANDOFF_TEXT}" \
  --arg nonce "${NONCE}" '
  {
    steps: [
      { step_id: "produce",
        runner_profile: $profile,
        input: { prompt: "produce", steps: 1, sleep_seconds: 1,
                 artifact_name: $name, artifact_text: $text } },
      { step_id: "consume",
        runner_profile: $profile,
        depends_on: ["produce"],
        input_from: { produce: $missing },
        input: { prompt: "consume", steps: 1, sleep_seconds: 1 } }
    ],
    metadata: { source: "e2e-test-negative", run_id: $nonce },
    on_step_failure: "continue"
  }')"
if NEG_WF="$(submit_workflow "${NEG_BODY}")"; then
  NEG_PRODUCE="$(workflow_step_task "${NEG_WF}" produce)"
  NEG_CONSUME="$(workflow_step_task "${NEG_WF}" consume)"
  TASK_IDS+=("${NEG_PRODUCE}" "${NEG_CONSUME}")
  if NEG_PRODUCE_STATE="$(wait_for_state "${NEG_PRODUCE}" "${TERMINAL}" "${TIMEOUT}")"; then
    assert_eq "SUCCEEDED" "${NEG_PRODUCE_STATE}" "the negative control's producer still succeeds"
  else
    t_fail "the negative control's producer never finished (stuck at ${NEG_PRODUCE_STATE})"
  fi
  # The consumer exhausts its attempts on an input that will never appear, so
  # this waits for a terminal state rather than for FAILED specifically -- and
  # then insists the terminal state is not SUCCEEDED.
  if NEG_CONSUME_STATE="$(wait_for_state "${NEG_CONSUME}" "${TERMINAL}" "${TIMEOUT}")"; then
    if [[ "${NEG_CONSUME_STATE}" == "SUCCEEDED" ]]; then
      t_fail "a step whose promised input was never written reported SUCCEEDED; the handoff checks above prove nothing"
    else
      t_pass "the promised-but-absent input failed the step (${NEG_CONSUME_STATE})"
      NEG_ERROR="$(task_field "${NEG_CONSUME}" '.last_error // ""')"
      case "${NEG_ERROR}" in
        *"${MISSING_NAME}"*) t_pass "the error names the file that was missing" ;;
        *) t_fail "the error does not name ${MISSING_NAME}: ${NEG_ERROR:0:200}" ;;
      esac
    fi
  else
    t_fail "the negative control's consumer never reached a terminal state (stuck at ${NEG_CONSUME_STATE})"
  fi
else
  t_fail "the negative-control workflow was not accepted, so the handoff checks are unproven"
fi

# ---------------------------------------------------------------------------
t_case "Spend reaches the HTTP API wherever Firestore carries it"
#
# PRESENT-WHEN-PRESENT, which is the half nobody asserted. `attempt_from_dict`
# dropped all five spend fields for months while a passing test checked they
# were None on an attempt that recorded none -- an assertion the bug satisfies.
# So this compares the two stores against each other, on every attempt that has
# a number to compare, and reports NOT MEASURED rather than PASS when the
# deployment has never run anything that reports usage.
SPEND_FIELDS="input_tokens output_tokens cache_read_input_tokens cache_creation_input_tokens cost_usd"

# Shape first: the keys must exist in the response at all. Checked on this run's
# own task, so it is answered even on a deployment that has never spent anything.
ATT_BODY="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-att.XXXXXX")"
if api_fetch "/tasks/${PRODUCE_ID}/attempts" "${ATT_BODY}"; then
  ATT_COUNT="$(jq -r '(.attempts // []) | length' <"${ATT_BODY}")"
  assert_ge "${ATT_COUNT}" 1 "attempts served for a task that ran"
  for field in ${SPEND_FIELDS}; do
    # `length > 0 and all(...)`, because jq's `all` over an EMPTY array is
    # TRUE. Without the length test this prints PASS for every field on a
    # response carrying no attempts at all -- a check that certifies a shape it
    # never saw, which is the same vacuity as `[[ "" -eq 0 ]]` one layer up.
    if jq -e --arg f "${field}" \
        '(.attempts // []) | (length > 0) and all(has($f))' <"${ATT_BODY}" >/dev/null; then
      t_pass "every served attempt carries the ${field} key"
    else
      t_fail "the API omits ${field} from its attempt shape entirely (or served no attempt to look at)"
    fi
  done
else
  t_fail "could not read the attempt list over HTTP"
fi
rm -f "${ATT_BODY}"

# Agreement: for every attempt Firestore says has spend, the API must serve the
# same number. `IS_NOT_NULL` is a unary filter, so an attempt that never had the
# field written is not returned -- which is the difference between "no spend"
# and "spend the decoder dropped".
COMPARED=0
DISAGREED=0
SPEND_ROWS="$(fs_query attempts "$(fs_null_filter input_tokens IS_NOT_NULL)" 25 \
  | jq -c "${FS_JQ} doc" || true)"
if [[ -z "${SPEND_ROWS//[[:space:]]/}" ]]; then
  t_skip "no attempt in this deployment has recorded token usage, so Firestore and the API could not be compared. The mock profile has provider=null and reports none; run a claude-code or codex task, or pass --require-spend to make this a failure."
else
  while IFS= read -r row; do
    [[ -n "${row}" ]] || continue
    ROW_TASK="$(jq -r '.task_id // ""' <<<"${row}")"
    ROW_ATTEMPT="$(jq -r '.attempt_id // .id // ""' <<<"${row}")"
    [[ -n "${ROW_TASK}" && -n "${ROW_ATTEMPT}" ]] || continue
    CMP_BODY="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-cmp.XXXXXX")"
    if ! api_fetch "/tasks/${ROW_TASK}/attempts" "${CMP_BODY}"; then
      # A 404 here is its own finding: Firestore holds an attempt for a task the
      # API will not serve. Reported, not skipped past.
      t_fail "Firestore has attempt ${ROW_ATTEMPT} for task ${ROW_TASK}, and the API would not serve that task's attempts (HTTP ${API_STATUS})"
      rm -f "${CMP_BODY}"
      continue
    fi
    SERVED="$(jq -c --arg a "${ROW_ATTEMPT}" \
      '[ (.attempts // [])[] | select(.attempt_id == $a) ] | first // null' <"${CMP_BODY}")"
    rm -f "${CMP_BODY}"
    if [[ "${SERVED}" == "null" ]]; then
      t_fail "the API does not serve attempt ${ROW_ATTEMPT} of task ${ROW_TASK}, which Firestore holds"
      DISAGREED=$(( DISAGREED + 1 ))
      continue
    fi
    for field in ${SPEND_FIELDS}; do
      # `has` and an explicit null test, not `// "absent"`. The alternative
      # operator treats `false` AND a real `null` as absent, and the whole point
      # of this comparison is to tell "not measured" apart from "measured".
      # A measured ZERO must reach the comparison: `0` is truthy in jq, so it
      # would survive `//` here, but relying on that is exactly the reasoning
      # that put `.enabled // true` in this repository.
      STORED="$(jq -r --arg f "${field}" \
        'if has($f) and .[$f] != null then (.[$f] | tostring) else "absent" end' <<<"${row}")"
      [[ "${STORED}" != "absent" ]] || continue
      GOT="$(jq -r --arg f "${field}" \
        'if has($f) and .[$f] != null then (.[$f] | tostring) else "absent" end' <<<"${SERVED}")"
      COMPARED=$(( COMPARED + 1 ))
      if [[ "${GOT}" != "${STORED}" ]]; then
        DISAGREED=$(( DISAGREED + 1 ))
        t_info "attempt ${ROW_ATTEMPT}: Firestore ${field}=${STORED}, API ${field}=${GOT}"
      fi
    done
  done <<<"${SPEND_ROWS}"

  if [[ "${COMPARED}" -eq 0 ]]; then
    t_skip "attempts with usage exist but none carried a comparable value"
  else
    t_info "compared ${COMPARED} recorded spend value(s) across attempts"
    assert_eq "0" "${DISAGREED}" "spend values where Firestore and the API disagree"
  fi
fi

# ---------------------------------------------------------------------------
t_case "Starting a browser sign-in returns a usable URL and a state, and leaks no verifier"
#
# WHAT THIS CAN AND CANNOT ASSERT, said plainly because the missing half is the
# interesting one. `/v1/accounts/authorize` is a proxy onto the broker, and it
# is exactly the shape that 500d for every caller when `BrokerClient._call` was
# invoked with a keyword it does not accept -- every test faked the account pool
# ABOVE that layer, so nothing executed the call. Driving it over HTTP is what
# proves the two services agree on a signature.
#
# The REDEEM step cannot be automated: it needs a human to sign in to Anthropic
# in a browser and paste back the code their callback page shows. So this checks
# everything up to that point and stops, rather than pretending.
SIGNIN_BODY="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-signin.XXXXXX")"
if api_send POST "/accounts/authorize" \
     "$(jq -nc --arg l "e2e-${NONCE}" '{label:$l, lend_to:[]}')" "${SIGNIN_BODY}"; then
  AUTH_URL="$(jq -r '.authorize_url // .url // ""' <"${SIGNIN_BODY}")"
  AUTH_STATE="$(jq -r '.state // ""' <"${SIGNIN_BODY}")"
  case "${AUTH_URL}" in
    https://*) t_pass "authorize_url is an https URL" ;;
    "")        t_fail "the response carried no authorize URL" ;;
    *)         t_fail "authorize_url is not https: ${AUTH_URL%%\?*}" ;;
  esac
  # A PKCE flow whose challenge is absent is a flow that proves nothing, and one
  # whose VERIFIER is returned to the client is the same thing wearing a
  # challenge. Both are checked, in opposite directions.
  case "${AUTH_URL}" in
    *code_challenge=*) t_pass "the authorize URL carries a PKCE challenge" ;;
    *) t_fail "the authorize URL carries no code_challenge" ;;
  esac
  if [[ "${#AUTH_STATE}" -ge 16 ]]; then
    t_pass "state is ${#AUTH_STATE} characters"
  else
    t_fail "state is ${#AUTH_STATE} characters; a guessable state is the only thing binding a redemption to the sign-in that started it"
  fi
  case "${AUTH_URL}" in
    *"state=${AUTH_STATE}"*) t_pass "the URL and the response name the same state" ;;
    *) t_fail "the authorize URL does not carry the state the response returned" ;;
  esac
  # The verifier stays in the broker. A verifier the client holds is a PKCE
  # exchange the client can complete on its own.
  if jq -e 'paths(scalars) as $p | $p | map(tostring) | any(test("verifier"; "i"))' \
      <"${SIGNIN_BODY}" >/dev/null 2>&1; then
    t_fail "the sign-in response contains a key matching /verifier/"
  else
    t_pass "no verifier in the sign-in response"
  fi
  if grep -Eq 'sk-ant-[A-Za-z0-9]|"refresh_token"|"access_token"' "${SIGNIN_BODY}"; then
    t_fail "the sign-in response contains credential-shaped material"
  else
    t_pass "no credential-shaped material in the sign-in response"
  fi
  t_info "NOT ASSERTED: redeeming the code. It needs a person to sign in to Anthropic"
  t_info "in a browser and paste back what the callback page shows, so this check stops"
  t_info "here rather than pretending. No account is created by this run."
  # THE ONE THING THIS SUITE LEAVES BEHIND, named so nobody has to discover it.
  # A pending sign-in record keyed by the state, in the broker's own collection.
  # It carries a PKCE verifier and no credential, it is unreachable without the
  # state, and it expires on its own -- there is no route that deletes one, so
  # cleaning it up is not available rather than skipped.
  #
  # The state itself is NOT printed, at any length. It is the only thing binding
  # a redemption to the sign-in that started it (see routes/accounts.py
  # finish_sign_in), and a verification transcript is copied into issues.
  t_info "left behind: one pending sign-in record, expiring in $(jq -r '.expires_in_seconds // "?"' <"${SIGNIN_BODY}")s."
else
  t_fail "POST ${API_PREFIX}/accounts/authorize failed (HTTP ${API_STATUS}); the browser sign-in path is unusable"
fi
rm -f "${SIGNIN_BODY}"

# ---------------------------------------------------------------------------
t_case "Firestore and the API agree about state, for this run's tasks"
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  [[ -n "${id}" ]] || continue
  FS_STATE="$(task_state "${id}")"
  ONE_BODY="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-one.XXXXXX")"
  if api_fetch "/tasks/${id}" "${ONE_BODY}"; then
    API_STATE="$(jq -r '.state // .task.state // "MISSING"' <"${ONE_BODY}")"
    if [[ "${API_STATE}" == "${FS_STATE}" ]]; then
      t_pass "${id}: both say ${FS_STATE}"
    else
      # Re-read once before calling it a disagreement: these are two reads of a
      # moving system, and a task that finished between them is not a defect.
      # The pause is the poll interval's, scaled: it exists to let a moving
      # system move, so against a fake that cannot move it need not be waited.
      sleep "${SWARM_REREAD_PAUSE_SECONDS:-3}"
      FS_STATE="$(task_state "${id}")"
      assert_eq "${FS_STATE}" "${API_STATE}" "${id}: Firestore vs API state"
    fi
  else
    t_fail "${id}: Firestore says ${FS_STATE} and the API would not serve the task"
  fi
  rm -f "${ONE_BODY}"
done

# ---------------------------------------------------------------------------
t_case "No task is holding capacity against an execution that is gone"
#
# THE TWENTY-MINUTE SILENCE. A task read DISPATCHED against a dead execution and
# nothing in the platform said so: the control plane believed it had dispatched,
# the backend had nothing running, and the slot stayed reserved. The reconciler
# is what repairs this; this check is what NOTICES it, which is a different job
# and the one that was missing.
#
# Deliberately platform-wide rather than limited to this run's tasks: a stuck
# task from an hour ago is exactly what nobody was told about.
EXEC_ERR="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-exec.XXXXXX")"
if EXECUTIONS="$(cloud_run_executions 2>"${EXEC_ERR}")"; then
  rm -f "${EXEC_ERR}"
  RUNNING_NAMES="$(jq -r '
    [ .[] | select((.completionTime // null) == null) | (.name | split("/") | last) ]
    | join(" ")' <<<"${EXECUTIONS}")"
  t_info "$(jq -r 'length' <<<"${EXECUTIONS}") execution(s) listed, $(printf '%s' "${RUNNING_NAMES}" | wc -w | tr -d '[:space:]') still running"
  MISMATCHED=0
  DISPATCHED_SEEN=0
  for state in DISPATCHED STARTING RUNNING; do
    ROWS="$(fs_query tasks "$(fs_field_filter state EQUAL "$(jq -nc --arg v "${state}" '{stringValue:$v}')")" 50 \
      | jq -c "${FS_JQ} doc" || true)"
    [[ -n "${ROWS//[[:space:]]/}" ]] || continue
    while IFS= read -r row; do
      [[ -n "${row}" ]] || continue
      TID="$(jq -r '.id // ""' <<<"${row}")"
      [[ -n "${TID}" ]] || continue
      DISPATCHED_SEEN=$(( DISPATCHED_SEEN + 1 ))
      # The execution name lives on the ATTEMPT, written by the dispatcher.
      # An attempt without one is a task the control plane never actually
      # handed to a backend -- which is the same finding by another route.
      EXEC_NAME="$(task_attempts "${TID}" \
        | jq -sr 'sort_by(.generation // 0) | last // {} | .execution_name // ""')"
      if [[ -z "${EXEC_NAME}" || "${EXEC_NAME}" == "null" ]]; then
        t_info "task ${TID} is ${state} and its newest attempt names no execution"
        MISMATCHED=$(( MISMATCHED + 1 ))
        continue
      fi
      SHORT="${EXEC_NAME##*/}"
      case " ${RUNNING_NAMES} " in
        *" ${SHORT} "*) ;;
        *)
          t_info "task ${TID} is ${state} against execution ${SHORT}, which is not running"
          MISMATCHED=$(( MISMATCHED + 1 ))
          ;;
      esac
    done <<<"${ROWS}"
  done
  if [[ "${DISPATCHED_SEEN}" -eq 0 ]]; then
    t_skip "no task was in DISPATCHED, STARTING or RUNNING while this check ran, so control plane and backend could not be compared"
  else
    t_info "checked ${DISPATCHED_SEEN} task(s) holding capacity"
    assert_eq "0" "${MISMATCHED}" "tasks holding capacity with no live execution behind them"
  fi
else
  ERR_DETAIL="$(cat "${EXEC_ERR}")"
  rm -f "${EXEC_ERR}"
  die_if_auth_failure "${ERR_DETAIL}"
  t_fail "could not list Cloud Run executions, so the control plane could not be compared with the backend: $(printf '%s' "${ERR_DETAIL}" | redact | head -n 1)"
fi

# ---------------------------------------------------------------------------
t_case "A workflow whose steps have all finished does not still read QUEUED"
#
# A workflow's `state` is written once, at creation, and nothing in this
# repository ever advances it: `Store.create_workflow` sets it and
# `Store.cancel_workflow` only sets `cancel_requested`. So a workflow whose
# every step has SUCCEEDED still serves QUEUED, for ever, through the same field
# a UI renders as progress.
#
# This is asserted rather than described because a check that agrees with the
# defect is how the defect survives.
WF_BODY_OUT="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-wf.XXXXXX")"
if api_fetch "/workflows/${WF_ID}" "${WF_BODY_OUT}"; then
  WF_STATE="$(jq -r '.workflow.state // "MISSING"' <"${WF_BODY_OUT}")"
  STEP_STATES="$(jq -r '[ (.tasks // [])[].state ] | unique | join(",")' <"${WF_BODY_OUT}")"
  t_info "workflow ${WF_ID} state=${WF_STATE}, step states=${STEP_STATES}"
  if [[ "${STEP_STATES}" == "SUCCEEDED" ]]; then
    if [[ "${WF_STATE}" == "SUCCEEDED" ]]; then
      t_pass "the workflow agrees with its steps"
    else
      t_fail "every step SUCCEEDED and the workflow still reads ${WF_STATE}; nothing in the platform advances a workflow's state"
    fi
  else
    t_skip "the steps are ${STEP_STATES}, so there is no settled workflow state to compare"
  fi
else
  t_fail "could not read the workflow over HTTP"
fi
rm -f "${WF_BODY_OUT}"

t_summary
