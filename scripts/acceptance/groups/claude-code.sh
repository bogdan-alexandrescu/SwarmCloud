#!/usr/bin/env bash
# Acceptance group `claude-code`: a real agent, on a real repository, judged by
# what it produced -- the patch's lines, the pull request's title and body, the
# answer's content. Prompts are one or two sentences on purpose: every check
# here spends subscription quota.
#
# The fixture is tests/acceptance/fixtures/claude-code: calc.py's add()
# subtracts, and test_calc.py fails because of it.

set -euo pipefail

CC_FIXTURE="tests/acceptance/fixtures/claude-code/calc.py"
CC_BUG_LINE="    return a - b"
CC_FIX_LINE="    return a + b"
#: A known, closed issue of this repository, and a string its title carries
#: that an agent cannot produce without reading it. #77's title is
#: "bootstrap.sh reports "object versioning is OFF" on a state bucket that has
#: versioning on" (read 2026-09-29).
CC_ISSUE="${SWARM_ACCEPTANCE_ISSUE:-77}"
CC_ISSUE_EXPECT="${SWARM_ACCEPTANCE_ISSUE_EXPECT:-bootstrap.sh}"

claude_code_checks() {
  cat <<'EOF'
claude-code: collect fixes the one-line bug, and the patch applies and replaces exactly that line
claude-code: direct-pr opens a pull request titled from pr-title.txt, with the task id in its body
claude-code: the issue input puts the issue in issue.md and the answer uses it
EOF
}

_cc_fix_prompt() {
  printf 'In %s, add() returns a - b; make it return a + b. Change only that line and no other file. Do not run anything and do not commit. %s' \
    "${CC_FIXTURE}" "$1"
}

run_claude_code() {
  step "Acceptance: claude-code (repository ${ACC_REPOSITORY_URL} @ ${ACC_REF})"
  local collect="" direct="" issue="" issue_declared=0 answer

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
  answer="$(acc_door claude-code "$(jq -nc --argjson n "${CC_ISSUE}" '{prompt: "acceptance door probe", issue: $n}')")"
  case "${answer}" in
    "422 invalid_dispatch"|"400 invalid_dispatch") issue_declared=1 ;;
    "422 invalid_input") issue_declared=0 ;;
    *) issue_declared=2; t_info "issue declaration probe answered '${answer}'" ;;
  esac
  if [[ "${issue_declared}" == "1" ]]; then
    ACC_CHECK="claude-code: issue"
    acc_submit issue claude-code \
      "$(jq -nc --argjson n "${CC_ISSUE}" '{prompt: "Read issue.md in your working directory. Reply with only the file name of the script that issue is about, and nothing else. Do not edit any file.", issue: $n}')" \
      "$(acc_repo_extra '{"max_attempts": 1}')" || issue=""
  fi

  _cc_check_collect "${collect}"
  _cc_check_direct_pr "${direct}"
  _cc_check_issue "${issue}" "${issue_declared}"
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

  # Apply it to the file as it is at the ref the task cloned. Read from GitHub
  # directly: the swarm-verify image carries scripts/, not tests/.
  dir="${ACC_WORK}/cc-apply"
  mkdir -p "${dir}/$(dirname "${CC_FIXTURE}")"
  original="${dir}/${CC_FIXTURE}"
  if ! curl -sSf -m 30 -o "${original}" \
      "https://raw.githubusercontent.com/${ACC_GITHUB_REPO}/${ACC_REF}/${CC_FIXTURE}" 2>/dev/null; then
    acc_skip "not measured: could not fetch ${CC_FIXTURE} at ${ACC_REF} from raw.githubusercontent.com to apply the patch to" "${task}"
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
  if files="$(acc_github GET "/repos/${ACC_GITHUB_REPO}/pulls/${number}/files")"; then
    acc_assert_eq "${CC_FIXTURE}" "$(jq -r '[.[].filename] | join(" ")' <<<"${files}")" "the PR changes only the fixture" "${task}"
    if jq -r '.[].patch // ""' <<<"${files}" | grep -qxF -- "-${CC_BUG_LINE}"; then
      acc_pass "the PR's diff removes the bug line" "${task}"
    else
      acc_fail "the PR's diff does not remove the bug line" "${task}"
    fi
  else
    acc_fail "could not read PR #${number}'s files from GitHub" "${task}"
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
  elif grep -qF "${CC_ISSUE_EXPECT}" <<<"${content}"; then
    acc_pass "the answer names ${CC_ISSUE_EXPECT}, which only issue #${CC_ISSUE}'s text says" "${task}"
  else
    acc_fail "the answer does not name ${CC_ISSUE_EXPECT}: $(printf '%s' "${content}" | redact | head -c 200)" "${task}"
  fi
}
