#!/usr/bin/env bash
# Acceptance group `claude-code`: a real agent, on a real repository, judged by
# what it produced -- the patch's lines, the pull request's title and body, the
# answer's content. Prompts are one or two sentences on purpose: every check
# here spends subscription quota.
#
# The fixture is tests/acceptance/fixtures/claude-code: calc.py's add()
# subtracts, and test_calc.py fails because of it. The tasks clone it from the
# private sandbox (config.sh), where sandbox-sync.sh put the released commit's
# copy, and run in the smoke tenant. The test-services pair below has a
# fixture of its own.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

CC_FIXTURE="tests/acceptance/fixtures/claude-code/calc.py"
CC_BUG_LINE="    return a - b"
CC_FIX_LINE="    return a + b"
# The issue check reads the sandbox's fixture issue, ACC_ISSUE, and expects
# ACC_ISSUE_EXPECT, a string only its text carries, so an agent cannot produce
# it without reading the issue. Both are config.sh's; sandbox-sync.sh opens the
# issue and refuses a sandbox whose issue says something else.

# THE MODEL-CLASS CANARY (docs/design/model-per-step.md §4.9, #1435). One step
# per class a claude-code step may name, each on a fixed prompt that ends at
# once. For each, the attempt's `model` must be the class's pin as
# /v1/runtimes serves it, and the runner summary's `usage.models` must hold
# that pin and no other class's. It is red until the CLI in
# agent-runtime-base accepts every pinned ID (§2.3): that is what it is for.
#
# The four names restate swarm_common.profiles.MODEL_CLASSES, in its order;
# tests/unit/scripts/test_model_class_canary.py holds them equal, so a class
# added to the frozen vocabulary fails that test until the canary runs it.
CC_MODEL_CLASSES=(opus fable sonnet haiku)
# The class whose pin the CLI may report BESIDE the one it was told to run:
# the small model it runs for its own housekeeping, which the worker's
# _note_model_mismatch (lifecycle.py) does not count as a substitution
# either. On a step of another class this pin in `usage.models` is noted,
# not failed; any other class's pin there is a substitution and fails.
CC_HOUSEKEEPING_CLASS="haiku"
CC_CANARY_PROMPT="Reply with the single word ok. Do not read, edit or run anything."

# THE DECLARED TEST SERVICE (TS-ACCEPT, part of #1627; docs/design/test-services.md
# §5.3 and §7). The fixture is tests/acceptance/fixtures/test-services: a
# .swarm/test-services.toml declaring Postgres 16, and test_store.py, which
# writes a row to it and reads it back, connecting in its pytest fixture. An
# agent breaks the assertion; the pre-publish gate runs the changed test
# (graph-pytest) before anything is published. With the declaration the
# worker has started Postgres after the clone, so the broken test FAILS and
# gets the fix turn; without it nothing listens, every test errors at setup
# on 127.0.0.1:5432, and the check is SKIPPED as PR 1606 made it. Both are
# collect steps: no pull request opens, so the record is read where the
# runner wrote it, before any publish (result_summary.runner.output.prepublish).
#
# The gate reads the declaration at the CLONE'S ROOT, and the sandbox's main
# holds the fixtures under tests/acceptance/fixtures/, so these tasks clone
# branches whose root IS the fixture: CC_TS_DECLARED_REF as it is,
# CC_TS_UNDECLARED_REF the same tree without .swarm/. sandbox-sync.sh puts
# them on the sandbox; a sandbox without them fails here at the clone, and
# the failure names the branch.
# tests/unit/scripts/test_acceptance_test_services.py holds the fixture to the
# catalogue's parser and the line below to the fixture's test.
CC_TS_DECLARED_REF="acceptance/test-services"
CC_TS_UNDECLARED_REF="acceptance/test-services-undeclared"
CC_TS_TEST="test_store.py"
CC_TS_ASSERT_LINE='    assert rows == [("kept",)]'
#: How prepublish._pytest_project's skip begins when nothing declared answers.
CC_TS_UNREACHABLE="a service the tests need is unreachable (127.0.0.1:5432)"

claude_code_checks() {
  cat <<'EOF'
claude-code: collect fixes the one-line bug, and the patch applies and replaces exactly that line
claude-code: direct-pr opens a pull request titled from pr-title.txt, with the task id in its body
claude-code: the issue input puts the issue in issue.md and the answer uses it
EOF
  local class
  for class in "${CC_MODEL_CLASSES[@]}"; do
    printf 'claude-code: a %s step runs its pin: the attempt records it and usage.models agrees\n' "${class}"
  done
  _cc_ts_check_name declared
  _cc_ts_check_name undeclared
}

# _cc_ts_check_name declared|undeclared -> the check's line.
_cc_ts_check_name() {
  case "$1" in
    declared) printf 'claude-code: a declared Postgres 16 runs the broken test before publish: graph-pytest failed, with a fix turn, and no pull request\n' ;;
    *) printf 'claude-code: the same fixture without .swarm/test-services.toml reports graph-pytest skipped, 127.0.0.1:5432 unreachable\n' ;;
  esac
}

_cc_ts_prompt() {
  printf 'In %s, change "kept" to "lost" on the line that reads: %s -- so that test fails. Change only that line and no other file. Do not run anything and do not commit. Reply with one word: done.' \
    "${CC_TS_TEST}" "${CC_TS_ASSERT_LINE#    }"
}

# _cc_ts_extra REF -> a one-attempt collect step on the sandbox at REF.
_cc_ts_extra() {
  acc_extra "$(jq -nc --arg u "${ACC_REPOSITORY_URL}" --arg r "$1" \
    '{repository_url: $u, repository_ref: $r, max_attempts: 1}')"
}

_cc_fix_prompt() {
  printf 'In %s, add() returns a - b; make it return a + b. Change only that line and no other file. Do not run anything and do not commit. %s' \
    "${CC_FIXTURE}" "$1"
}

run_claude_code() {
  step "Acceptance: claude-code (repository ${ACC_REPOSITORY_URL} @ ${ACC_REF})"
  local collect="" direct="" issue="" issue_declared=0 answer class step runtimes i
  local ts_declared="" ts_undeclared=""
  # One task id per class, in CC_MODEL_CLASSES' order; "" when not submitted.
  local canary=()

  ACC_CHECK="claude-code: collect"
  acc_submit collect claude-code \
    "$(jq -nc --arg p "$(_cc_fix_prompt "Reply with one word: done.")" '{prompt: $p}')" \
    "$(acc_repo_extra '{"max_attempts": 1}')" || collect=""

  ACC_CHECK="claude-code: direct-pr"
  acc_submit direct claude-code \
    "$(jq -nc --arg p "$(_cc_fix_prompt "Then write a one-line pull request title naming that fix, with no task id, to \$SWARM_ARTIFACTS_DIR/pr-title.txt, and write the single line acceptance-run:${ACC_RUN_ID} to \$SWARM_ARTIFACTS_DIR/pr-body.md.")" '{prompt: $p}')" \
    "$(acc_repo_extra '{"max_attempts": 1, "strategy": "direct-pr"}')" || direct=""

  # `issue` is contract request 28 (#265). Whether it is deployed is asked of
  # the door without creating anything (acc_door): a VALID issue number sent
  # with a reserved metadata key is refused for the metadata (invalid_dispatch)
  # where `issue` is declared, and for the input (invalid_input) where it is not.
  answer="$(acc_door claude-code "$(jq -nc --argjson n "${ACC_ISSUE}" '{prompt: "acceptance door probe", issue: $n}')")"
  case "${answer}" in
    "422 invalid_dispatch"|"400 invalid_dispatch") issue_declared=1 ;;
    "422 invalid_input") issue_declared=0 ;;
    *) issue_declared=2; t_info "issue declaration probe answered '${answer}'" ;;
  esac
  if [[ "${issue_declared}" == "1" ]]; then
    ACC_CHECK="claude-code: issue"
    acc_submit issue claude-code \
      "$(jq -nc --argjson n "${ACC_ISSUE}" '{prompt: "Read issue.md in your working directory. Reply with only the file name of the script that issue is about, and nothing else. Do not edit any file.", issue: $n}')" \
      "$(acc_repo_extra '{"max_attempts": 1}')" || issue=""
  fi

  # The class is named and nothing else that chooses a model: the API fills
  # TaskCreate.model from the class's pin (Q4) and refuses input.model.
  for class in "${CC_MODEL_CLASSES[@]}"; do
    ACC_CHECK="claude-code: model class ${class}"
    step=""
    acc_submit step claude-code \
      "$(jq -nc --arg p "${CC_CANARY_PROMPT}" '{prompt: $p}')" \
      "$(acc_repo_extra "$(jq -nc --arg c "${class}" '{max_attempts: 1, model_class: $c}')")" || step=""
    canary+=("${step}")
  done

  ACC_CHECK="claude-code: test-services declared"
  acc_submit ts_declared claude-code "$(jq -nc --arg p "$(_cc_ts_prompt)" '{prompt: $p}')" \
    "$(_cc_ts_extra "${CC_TS_DECLARED_REF}")" || ts_declared=""
  ACC_CHECK="claude-code: test-services undeclared"
  acc_submit ts_undeclared claude-code "$(jq -nc --arg p "$(_cc_ts_prompt)" '{prompt: $p}')" \
    "$(_cc_ts_extra "${CC_TS_UNDECLARED_REF}")" || ts_undeclared=""

  _cc_check_collect "${collect}"
  _cc_check_direct_pr "${direct}"
  _cc_check_issue "${issue}" "${issue_declared}"
  _cc_check_test_services declared "${ts_declared}"
  _cc_check_test_services undeclared "${ts_undeclared}"

  # Read once the other checks have settled, so the pins judged are the ones
  # the steps were dispatched with (R6: a class's pin at dispatch runs). An
  # unreadable catalogue leaves an empty file, which each check fails on.
  runtimes="${ACC_WORK}/cc-runtimes.json"
  api_fetch "/runtimes" "${runtimes}" || : >"${runtimes}"
  i=0
  for class in "${CC_MODEL_CLASSES[@]}"; do
    _cc_check_model_class "${class}" "${canary[${i}]}" "${runtimes}"
    i=$(( i + 1 ))
  done
}

# _cc_check_model_class CLASS TASK RUNTIMES_FILE -> one canary step's verdict.
_cc_check_model_class() {
  local class="$1" task="$2" runtimes="$3" state attempt_model usage_models
  acc_check "claude-code: a ${class} step runs its pin: the attempt records it and usage.models agrees"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  if [[ "${state}" != "SUCCEEDED" ]]; then
    acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error"' | redact | tr '\n' ' ' | head -c 300)" "${task}"
    return 0
  fi
  # The newest attempt: max_attempts is 1, so it is the only one.
  if ! attempt_model="$(task_attempts "${task}" | jq -rs 'sort_by(.created_at) | last | .model // ""')"; then
    acc_fail "its attempts could not be read" "${task}"
    return 0
  fi
  usage_models="$(task_field "${task}" '.result_summary.runner.usage.models // [] | tojson')" || usage_models="[]"
  _cc_judge_model_class "${class}" "${runtimes}" "${attempt_model}" "${usage_models}" "${task}"
}

# _cc_judge_model_class CLASS RUNTIMES_FILE ATTEMPT_MODEL USAGE_MODELS_JSON TASK
#
# The verdict alone, on what the deployment served and recorded, so it can be
# judged offline against the evidence a working platform leaves and the
# evidence each defect leaves (tests/unit/scripts/test_model_class_canary.py).
#
# The pins are claude-code's `models` in /v1/runtimes, which swarm-api serves
# from WORKER_CLASS_MODELS, the map the scheduler dispatches with. A `[...]`
# suffix on a model the CLI reports (`claude-opus-5-5[1m]`) names the same
# model, as in the worker's _note_model_mismatch.
_cc_judge_model_class() {
  local class="$1" runtimes="$2" attempt_model="$3" usage_models="$4" task="$5" pin others noted
  pin="$(jq -r --arg c "${class}" '.runtimes["claude-code"].models[$c] // empty' "${runtimes}" 2>/dev/null)" || pin=""
  if [[ -z "${pin}" ]]; then
    acc_fail "/v1/runtimes serves no pin for class ${class} on claude-code, so there is nothing to hold the attempt to" "${task}"
    return 0
  fi
  acc_assert_eq "${pin}" "${attempt_model}" "the attempt's model is the ${class} pin" "${task}"
  if ! jq -e 'type == "array"' >/dev/null 2>&1 <<<"${usage_models}"; then
    acc_fail "usage.models is not a list: $(printf '%s' "${usage_models}" | head -c 200)" "${task}"
    return 0
  fi
  if jq -e --arg p "${pin}" 'map(tostring | split("[")[0]) | index($p) != null' >/dev/null <<<"${usage_models}"; then
    acc_pass "usage.models holds the ${class} pin ${pin}" "${task}"
  else
    acc_fail "usage.models does not hold the ${class} pin ${pin}: $(jq -c . <<<"${usage_models}")" "${task}"
  fi
  # Another class's pin in the stream is a substitution, except the
  # housekeeping class's beside the pin on a step of another class.
  others="$(jq -r --arg c "${class}" --arg h "${CC_HOUSEKEEPING_CLASS}" --argjson u "${usage_models}" '
    (.runtimes["claude-code"].models // {}) as $m
    | ($u | map(tostring | split("[")[0])) as $ran
    | [$m | to_entries[] | select(.key != $c and .key != $h and (.value as $v | $ran | index($v) != null))
       | "\(.key)=\(.value)"]
    | join(" ")' "${runtimes}")"
  noted="$(jq -r --arg c "${class}" --arg h "${CC_HOUSEKEEPING_CLASS}" --argjson u "${usage_models}" '
    (.runtimes["claude-code"].models // {}) as $m
    | ($u | map(tostring | split("[")[0])) as $ran
    | if $c != $h and ($m[$h] // "") != "" and ($ran | index($m[$h]) != null) then $m[$h] else "" end' "${runtimes}")"
  if [[ -n "${others}" ]]; then
    acc_fail "usage.models holds another class's pin: ${others}" "${task}"
  else
    acc_pass "usage.models holds no other class's pin" "${task}"
  fi
  if [[ -n "${noted}" ]]; then
    t_info "usage.models also holds ${noted}, the ${CC_HOUSEKEEPING_CLASS} pin, which the CLI runs for its own housekeeping"
  fi
}

_cc_check_collect() {
  local task="$1" state patch files original dir applied
  acc_check "claude-code: collect fixes the one-line bug, and the patch applies and replaces exactly that line"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  if [[ "${state}" != "SUCCEEDED" ]]; then
    acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error"' | redact | tr '\n' ' ' | head -c 300)" "${task}"
    return 0
  fi
  patch="${ACC_WORK}/cc-collect.patch"
  if ! acc_raw_artifact "${task}" "swarm-work.patch" "${patch}"; then
    acc_fail "no swarm-work.patch to read (HTTP ${ACC_HTTP:-none}): collect harvested nothing" "${task}"
    return 0
  fi
  files="$(acc_diff_files <"${patch}" | tr '\n' ' ')"
  acc_assert_eq "${CC_FIXTURE} " "${files}" "the files the patch changes" "${task}"
  if acc_diff_replaces "${CC_FIXTURE}" "${CC_BUG_LINE}" <"${patch}"; then
    acc_pass "the patch removes 'return a - b' from ${CC_FIXTURE} and adds a line in its place" "${task}"
  else
    acc_fail "the patch does not replace the bug line in ${CC_FIXTURE}" "${task}"
  fi

  # Apply it to the file as it is at the ref the task cloned. With a token,
  # read it from the sandbox itself. Without one -- the release, whose
  # swarm-verify job holds none -- apply it to this build's own copy: the
  # swarm-verify image carries tests/acceptance/fixtures/ (#628), and
  # sandbox-sync.sh put that same commit's fixtures on the sandbox's main
  # before the suite started, so at main the two are the same bytes.
  dir="${ACC_WORK}/cc-apply"
  mkdir -p "${dir}/$(dirname "${CC_FIXTURE}")"
  original="${dir}/${CC_FIXTURE}"
  if acc_github_can_read; then
    if ! acc_github_raw "${CC_FIXTURE}" "${original}" 2>/dev/null; then
      acc_skip "not measured: could not fetch ${CC_FIXTURE} at ${ACC_REF} from ${ACC_GITHUB_REPO} to apply the patch to" "${task}"
      return 0
    fi
  elif [[ "${ACC_REF}" == "main" && -f "${REPO_ROOT}/${CC_FIXTURE}" ]]; then
    cp "${REPO_ROOT}/${CC_FIXTURE}" "${original}"
    t_info "applying to this build's ${CC_FIXTURE}, which sandbox-sync.sh put on ${ACC_GITHUB_REPO}@main"
  else
    acc_skip "applying the patch to ${CC_FIXTURE} at ${ACC_REF}: $(acc_github_skip_reason)" "${task}"
    return 0
  fi
  if command -v git >/dev/null 2>&1; then
    applied="$(cd "${dir}" && git apply --include="${CC_FIXTURE}" "${patch}" 2>&1)" || applied="FAILED: ${applied}"
  else
    applied="$(patch -p1 -d "${dir}" -i "${patch}" 2>&1)" || applied="FAILED: ${applied}"
  fi
  if [[ "${applied}" == FAILED:* ]]; then
    acc_fail "the patch does not apply to ${CC_FIXTURE} at ${ACC_REF}: $(printf '%s' "${applied}" | head -n 2 | tr '\n' ' ')" "${task}"
  elif grep -qxF "${CC_FIX_LINE}" "${original}" && ! grep -qxF "${CC_BUG_LINE}" "${original}"; then
    acc_pass "applied to ${CC_FIXTURE} at ${ACC_REF}: add() now reads 'return a + b'" "${task}"
  else
    acc_fail "applied, but add() does not read 'return a + b': $(grep -n 'return' "${original}" | head -n 2 | tr '\n' ' ')" "${task}"
  fi
}

_cc_check_direct_pr() {
  local task="$1" state git number branch reason title_file title pr pr_title pr_body pr_head files
  acc_check "claude-code: direct-pr opens a pull request titled from pr-title.txt, with the task id in its body"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  git="$(task_field "${task}" '.result_summary.git // {}')"
  number="$(jq -r '.pull_request.number // empty' <<<"${git}")"
  branch="$(jq -r '.branch // empty' <<<"${git}")"
  if [[ "${state}" != "SUCCEEDED" ]]; then
    acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error"' | redact | tr '\n' ' ' | head -c 300)" "${task}"
    acc_close_pr "${number}" "${branch}"
    return 0
  fi
  if [[ -z "${number}" ]]; then
    reason="$(jq -r '.publish_reason // "none"' <<<"${git}")"
    if grep -qiE 'credential|token|permission|403' <<<"${reason}"; then
      acc_skip "not measured: the tenant cannot push to ${ACC_GITHUB_REPO} (${reason})" "${task}"
    else
      acc_fail "no pull request was opened: ${reason}" "${task}"
    fi
    acc_close_pr "" "${branch}"
    return 0
  fi
  acc_pass "PR #${number} opened from ${branch}" "${task}"
  acc_assert_eq "agent" "$(jq -r '.pull_request_text.title // "none"' <<<"${git}")" "whose title is the agent's (pull_request_text.title)" "${task}"

  title_file="${ACC_WORK}/pr-title.txt"
  if ! acc_raw_artifact "${task}" "pr-title.txt" "${title_file}"; then
    acc_fail "pr-title.txt is not among the task's artifacts (HTTP ${ACC_HTTP:-none})" "${task}"
    acc_close_pr "${number}" "${branch}"
    return 0
  fi
  title="$(tr -d '\r' <"${title_file}" | sed -e 's/[[:space:]]*$//' | sed -n 1p)"
  # Everything below reads the pull request back from the private sandbox.
  if ! acc_github_can_read; then
    acc_skip "PR #${number}'s title, body, head and diff: $(acc_github_skip_reason)" "${task}"
    acc_close_pr "${number}" "${branch}"
    return 0
  fi
  if ! pr="$(acc_github GET "/repos/${ACC_GITHUB_REPO}/pulls/${number}")"; then
    acc_fail "could not read PR #${number} back from GitHub" "${task}"
    acc_close_pr "${number}" "${branch}"
    return 0
  fi
  pr_title="$(jq -r '.title // ""' <<<"${pr}")"
  pr_body="$(jq -r '.body // ""' <<<"${pr}")"
  pr_head="$(jq -r '.head.ref // ""' <<<"${pr}")"
  acc_assert_eq "${title}" "${pr_title}" "the PR title on GitHub is pr-title.txt's line" "${task}"
  if printf '%s' "${pr_title}" | acc_title_is_fact "${task}"; then
    acc_pass "the title names the work, not the task id: ${pr_title}" "${task}"
  else
    acc_fail "the title is not a fact-style title: ${pr_title}" "${task}"
  fi
  if grep -qF "${task}" <<<"${pr_body}"; then
    acc_pass "the PR body carries the task id" "${task}"
  else
    acc_fail "the PR body does not carry ${task}" "${task}"
  fi
  acc_assert_eq "${branch}" "${pr_head}" "the PR's head is the branch the task pushed" "${task}"
  # Measured against ACC_REF, not the PR's base: the platform opens direct-pr
  # against the repository's default branch regardless of ACC_REF, so a
  # /pulls/{n}/files diff run with a non-default ACC_REF would see every file
  # already on ACC_REF as an apparent addition (see acc_pr_files_vs_ref).
  if files="$(acc_pr_files_vs_ref "${pr_head}")"; then
    acc_assert_eq "${CC_FIXTURE}" "$(jq -r '[.[].filename] | join(" ")' <<<"${files}")" "the PR changes only the fixture" "${task}"
    if jq -r '.[].patch // ""' <<<"${files}" | grep -qxF -- "-${CC_BUG_LINE}"; then
      acc_pass "the PR's diff removes the bug line" "${task}"
    else
      acc_fail "the PR's diff does not remove the bug line" "${task}"
    fi
  else
    acc_fail "could not read PR #${number}'s diff against ${ACC_REF} from GitHub" "${task}"
  fi
  acc_close_pr "${number}" "${branch}"
}

_cc_check_issue() {
  local task="$1" declared="$2" state answer_file status content
  acc_check "claude-code: the issue input puts the issue in issue.md and the answer uses it"
  case "${declared}" in
    0) acc_skip "not measured: this deployment does not declare claude-code's issue input (contract request 28, #265)"; return 0 ;;
    2) acc_fail "could not tell whether the issue input is deployed; see the probe above"; return 0 ;;
  esac
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  if [[ "${state}" != "SUCCEEDED" ]]; then
    acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error"' | redact | tr '\n' ' ' | head -c 300)" "${task}"
    return 0
  fi
  answer_file="${ACC_WORK}/cc-answer.json"
  if ! api_fetch "/tasks/${task}/answer" "${answer_file}"; then
    acc_fail "GET /tasks/${task}/answer failed" "${task}"
    return 0
  fi
  status="$(jq -r '.status // "none"' "${answer_file}")"
  content="$(jq -r '.content // ""' "${answer_file}")"
  if [[ "${status}" != "ok" ]]; then
    acc_fail "the answer's status is ${status}, not ok" "${task}"
  elif grep -qF "${ACC_ISSUE_EXPECT}" <<<"${content}"; then
    acc_pass "the answer names ${ACC_ISSUE_EXPECT}, which only issue #${ACC_ISSUE}'s text says" "${task}"
  else
    acc_fail "the answer does not name ${ACC_ISSUE_EXPECT}: $(printf '%s' "${content}" | redact | head -c 200)" "${task}"
  fi
}

# _cc_check_test_services declared|undeclared TASK -> one half of the pair.
_cc_check_test_services() {
  local which="$1" task="$2" ref state summary
  ref="${CC_TS_DECLARED_REF}"
  [[ "${which}" == "declared" ]] || ref="${CC_TS_UNDECLARED_REF}"
  acc_check "$(_cc_ts_check_name "${which}")"
  [[ -n "${task}" ]] || { acc_fail "not submitted"; return 0; }
  acc_run_to_end state "${task}" || return 0
  if [[ "${state}" != "SUCCEEDED" ]]; then
    acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error"' | redact | tr '\n' ' ' | head -c 300) (it clones ${ACC_GITHUB_REPO}@${ref}, which scripts/acceptance/sandbox-sync.sh puts there)" "${task}"
    return 0
  fi
  summary="$(task_field "${task}" '.result_summary // {} | tojson')" || summary=""
  _cc_judge_test_services "${which}" "${summary}" "${task}"
}

# _cc_judge_test_services declared|undeclared SUMMARY_JSON TASK
#
# The verdict alone, on the task's result_summary, so it can be judged offline
# against the record a working gate leaves and the record each defect leaves
# (tests/unit/scripts/test_acceptance_test_services.py). Reads the FIRST
# round: the one that ran the agent's change as it left it, before any fix
# turn. Firestore holds a round, a list inside a list, as {"items": [...]}
# (agent_worker/storable.py); both shapes are read.
_cc_judge_test_services() {
  local which="$1" summary="$2" task="$3" record round check status reason services pr
  if ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"${summary}"; then
    acc_fail "its result_summary could not be read" "${task}"
    return 0
  fi
  record="$(jq -c '.runner.output.prepublish // empty | select(type == "object")' <<<"${summary}")"
  if [[ -z "${record}" ]]; then
    if jq -e '.runner.output.truncated == true' >/dev/null <<<"${summary}"; then
      # MUTATION: reading the preview as "no gate" would hide that the record exists
      # and was cut; the failure says which.
      acc_fail "the runner's output was cut to its 8000-character preview (lifecycle._truncate_json), so the pre-publish record cannot be read" "${task}"
    else
      # MUTATION: a runner with the gate off (CliAgentSpec.prepublish) records none.
      acc_fail "the runner recorded no pre-publish gate (result_summary.runner.output.prepublish)" "${task}"
    fi
    return 0
  fi
  round="$(jq -c '(.rounds // [])[0] // null | if type == "object" then (.items // null) else . end' <<<"${record}")"
  if [[ "${round}" == "null" ]]; then
    # MUTATION: a gate skipped for budget or an unknown base runs no round.
    acc_fail "the gate ran no round: $(jq -c '{skipped: (.skipped // null), base: (.base // null)}' <<<"${record}")" "${task}"
    return 0
  fi
  check="$(jq -c 'map(select(.name == "graph-pytest")) | first // null' <<<"${round}")"
  if [[ "${check}" == "null" ]]; then
    # MUTATION: a gate that does not select the changed test file runs no graph-pytest.
    acc_fail "the first round ran no graph-pytest: $(jq -r 'map("\(.name):\(.status)") | join(" ")' <<<"${round}")" "${task}"
    return 0
  fi
  status="$(jq -r '.status // "none"' <<<"${check}")"
  reason="$(jq -r '.reason // ""' <<<"${check}")"
  services="$(jq -r 'map(select(.name == "test-services") | "\(.status) (\(.reason // "no reason"))") | join("; ")' <<<"${round}")"
  pr="$(jq -r '.git.pull_request.number // empty' <<<"${summary}")"
  if [[ "${which}" == "declared" ]]; then
    # MUTATION: a worker that never starts the declared Postgres leaves the test
    # erroring at setup, and the gate reports `skipped`, the defect #1627 is about.
    acc_assert_eq "failed" "${status}" "the first round's graph-pytest, with Postgres declared${reason:+ (${reason})}" "${task}"
    # MUTATION: a declared service that did not start is `test-services: error`, and
    # the failure above would then not be the test's against a running Postgres.
    if [[ -z "${services}" ]]; then
      acc_pass "no test-services outcome: the declared Postgres answered" "${task}"
    else
      acc_fail "test-services: ${services}" "${task}"
    fi
    # MUTATION: a failed check with no fix turn is reported, never fixed (§5.3 row 2).
    acc_assert_eq "true" "$(jq -r '.fix_turn == true' <<<"${record}")" "the gate took a fix turn on it (fix_turn)" "${task}"
  else
    # MUTATION: a gate that judged an undeclared refusal as the tests' own fault
    # would report `failed` and spend a fix turn on a service nobody declared.
    acc_assert_eq "skipped" "${status}" "the first round's graph-pytest, with nothing declared" "${task}"
    # MUTATION: a skip for another reason (uv missing, no project) says nothing about
    # the service; only the unreachable address does.
    if [[ "${reason}" == "${CC_TS_UNREACHABLE}"* ]]; then
      acc_pass "skipped because ${CC_TS_UNREACHABLE#a service the tests need is }" "${task}"
    else
      acc_fail "skipped for another reason: '$(printf '%s' "${reason}" | head -c 200)'" "${task}"
    fi
    # MUTATION: with no declaration there is nothing to start or report.
    if [[ -z "${services}" ]]; then
      acc_pass "no test-services outcome: nothing is declared" "${task}"
    else
      acc_fail "test-services with nothing declared: ${services}" "${task}"
    fi
  fi
  # MUTATION: a collect step that opened a pull request: the record would no longer
  # show the gate ran before any could open.
  if [[ -z "${pr}" ]]; then
    acc_pass "no pull request opened: the gate's record precedes any publish" "${task}"
  else
    acc_fail "a collect step opened pull request #${pr}" "${task}"
  fi
}
