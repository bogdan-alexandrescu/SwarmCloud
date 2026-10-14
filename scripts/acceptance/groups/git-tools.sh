#!/usr/bin/env bash
# Acceptance group `git-tools`: the runner git tools (#1225), judged on a
# deployed environment by what swarm-api audited and what the tasks produced
# (docs/runner-git-tools.md section 7, lane G7; docs/acceptance.md, "git-tools").
#
# FOUR claude-code TASKS AND A TWO-STEP WORKFLOW, in two phases:
#
#   phase 1  lane   direct-pr, cloning acceptance/git-tools-base: a commit
#                   from BEFORE main added tests/acceptance/fixtures/git-tools/
#                   planted.txt (sandbox-sync.sh keeps that branch). It adds
#                   its own planted.txt, so its pull request conflicts with main.
#            build  an `integrate` contributor (+ its integrator, which runs
#                   only if the build step wrongly succeeds): pushes its branch
#                   mid-task, then commits off it, so its final push cannot
#                   fast-forward and is refused, never forced.
#   phase 2  rebase a direct-pr continuation of the lane: fetches the branch,
#                   git_rebase_onto main, resolves the planted conflict, pushes
#                   (a lease-guarded rewrite, Q1 = A) and reads pr_checks; it
#                   reports its environment by name (env_report.py).
#            reader collect with carrier: branches, so it MAY push (its own
#                   branch) and a refusal is the branch rule's, not
#                   `strategy_pushes_nothing`: pushes main and the lane's
#                   branch, fetching each before and after, and reads an
#                   allowlisted repository at a branch and then at a sha.
#
# Phase 2 waits for the lane because both need its branch to exist. Its peak
# in the smoke tenant is therefore five: lane, build and integrate, then the
# build pair still running beside rebase and reader.
#
# THIS FILE OWNS THE TASKS; THE PYTHON OWNS THE JUDGEMENT. Each task's state
# (acc_settle's, so a closed pool or a missing credential is the usual SKIP),
# document, git-calls audit and two artifacts are saved under one directory,
# and tests/acceptance/test_runner_git_tools.py judges it, one JSON line per
# check, reported here in this file's order. The prompts are the Python's
# too, so what a task is asked and what is read back are written once.
#
# THE PULL REQUEST'S CONTENT ON GITHUB -- its head contains main, and
# planted.txt holds the resolution -- is read where a sandbox token is:
# scripts/acceptance/github-verify.sh, after the suite.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

#: The verdict keys tests/acceptance/test_runner_git_tools.py writes (its
#: CHECKS), in the order of git_tools_checks below. A unit test holds them equal.
GIT_TOOLS_KEYS=(rebase read_private push_main push_other push_fails)
#: The roles the Python reads (its ROLES), one directory each.
GIT_TOOLS_ROLES=(lane rebase reader build)
GIT_TOOLS_JUDGE="tests/acceptance/test_runner_git_tools.py"
#: The sandbox branch sandbox-sync.sh points before main's planted.txt.
GIT_TOOLS_BASE="acceptance/git-tools-base"
GIT_TOOLS_PLANTED="tests/acceptance/fixtures/git-tools/planted.txt"

git_tools_checks() {
  cat <<'EOF'
git-tools: a lane rebases its pull request's branch onto main through a planted conflict, pushes, and reads pr_checks, with no forge credential in its environment
git-tools: read_private reads an allowlisted repository at a sha, and a repository on no list is refused
git-tools: a push to main is refused, and main does not move
git-tools: a push to another task's branch is refused, and that branch does not move
git-tools: a build step whose push is made to fail ends FAILED, not SUCCEEDED
EOF
}

# _gt_prompt NAME [ARGS...] -> the Python's prompt for one task.
_gt_prompt() {
  local name="$1"
  shift
  python3 "${REPO_ROOT}/${GIT_TOOLS_JUDGE}" prompt "${name}" --run-id "${ACC_RUN_ID}" "$@"
}

# _gt_choose_read DIR -> writes DIR/read.json: the allowlisted repository the
# reader reads, or why there is none. The sandbox when the smoke tenant
# lists it, because then the file read is known: this build's planted.txt,
# which sandbox-sync.sh put on its main. A platform admin sets the list
# (PATCH /v1/repositories/{id} readable_by_tasks, docs/runner-git-tools.md
# section 2.3); the suite only reads it.
_gt_choose_read() {
  local dir="$1" listed="${1}/repositories.json" pick sum
  if ! api_fetch "/repositories?limit=100" "${listed}" 2>/dev/null; then
    jq -nc --arg s "${API_STATUS:-?}" '{skip: ("GET /v1/repositories answered HTTP " + $s + ", so no allowlisted repository could be chosen")}' >"${dir}/read.json"
    return 0
  fi
  pick="$(jq -c --arg s "${ACC_GITHUB_REPO}" '
    [.repositories[]? | select(.readable_by_tasks == true)]
    | (map(select((.repository // "" | ascii_downcase) == ($s | ascii_downcase))) + .)[0] // empty' "${listed}")"
  if [[ -z "${pick}" ]]; then
    jq -nc --arg t "${ACC_TENANT}" '{skip: ("no repository registered in tenant " + $t + " is readable_by_tasks; a platform admin sets it with PATCH /v1/repositories/{id} (docs/runner-git-tools.md 2.3)")}' >"${dir}/read.json"
    return 0
  fi
  if [[ "$(jq -r '.repository | ascii_downcase' <<<"${pick}")" == "$(printf '%s' "${ACC_GITHUB_REPO}" | tr '[:upper:]' '[:lower:]')" ]]; then
    sum="$(sha256sum "${REPO_ROOT}/${GIT_TOOLS_PLANTED}" | awk '{print $1}')"
    jq -nc --arg r "$(jq -r '.repository' <<<"${pick}")" --arg p "${GIT_TOOLS_PLANTED}" --arg ref "${ACC_REF}" --arg h "${sum}" \
      '{repo: $r, path: $p, ref: $ref, expected_sha256: $h}' >"${dir}/read.json"
  else
    jq -nc --argjson x "${pick}" '{repo: $x.repository, path: "", ref: ($x.default_branch // "main")}' >"${dir}/read.json"
  fi
}

# _gt_save DIR ROLE TASK STATE -> that task's evidence under DIR/ROLE. A read
# that fails leaves its file absent; the Python judges an absent audit or
# artifact as unread, never as clean.
_gt_save() {
  local dir="$1/$2" task="$3" state="$4" name
  mkdir -p "${dir}"
  printf '%s\n' "${task}" >"${dir}/task"
  printf '%s\n' "${state}" >"${dir}/state"
  [[ -n "${task}" ]] || return 0
  task_doc "${task}" >"${dir}/doc.json" 2>/dev/null || rm -f "${dir}/doc.json"
  api_fetch "/tasks/${task}/git-calls" "${dir}/calls.json" 2>/dev/null || rm -f "${dir}/calls.json"
  for name in git-answers.jsonl env-report.json; do
    acc_raw_artifact "${task}" "${name}" "${dir}/${name}" 2>/dev/null || rm -f "${dir}/${name}"
  done
}

# _gt_settle VAR TASK -> VAR holds acc_settle's state, or "" with no task.
_gt_settle() {
  local __gt_var="$1" __gt_task="$2" __gt_state=""
  if [[ -n "${__gt_task}" ]]; then
    __gt_state="$(acc_settle "${__gt_task}" "${ACC_TIMEOUT}")" || true
  fi
  printf -v "${__gt_var}" '%s' "${__gt_state}"
}

run_git_tools() {
  step "Acceptance: git-tools (repository ${ACC_REPOSITORY_URL}; base ${GIT_TOOLS_BASE})"
  local dir lane="" build="" rebase="" reader="" response="" lane_state="" build_state=""
  local rebase_state="" reader_state="" number="" read_args=() names=() i line key verdict reason
  local empty='{}'
  dir="${ACC_WORK}/git-tools"
  mkdir -p "${dir}"
  while IFS= read -r line; do
    names+=("${line}")
  done < <(git_tools_checks)
  [[ "${#names[@]}" -eq "${#GIT_TOOLS_KEYS[@]}" ]] \
    || die "git-tools: ${#names[@]} checks listed, ${#GIT_TOOLS_KEYS[@]} verdict keys"

  # Phase 1: the lane that opens the pull request, and the build step.
  ACC_CHECK="git-tools: lane"
  acc_submit lane claude-code "$(jq -nc --arg p "$(_gt_prompt lane)" '{prompt: $p}')" \
    "$(acc_extra "$(jq -nc --arg u "${ACC_REPOSITORY_URL}" --arg r "${GIT_TOOLS_BASE}" \
        '{repository_url: $u, repository_ref: $r, max_attempts: 1, strategy: "direct-pr"}')")" || lane=""

  ACC_CHECK="git-tools: build"
  if acc_workflow response "$(jq -nc --argjson m "$(acc_metadata)" --arg u "${ACC_REPOSITORY_URL}" --arg r "${ACC_REF}" \
      --arg b "$(_gt_prompt build)" --arg i "$(_gt_prompt integrate)" '{
        priority: 10, metadata: $m, on_step_failure: "fail_workflow",
        strategy: "integrate", repository_url: $u, repository_ref: $r,
        steps: [
          {step_id: "build", runner_profile: "claude-code", input: {prompt: $b}},
          {step_id: "integrate", runner_profile: "claude-code", depends_on: ["build"], input: {prompt: $i}}
        ]}')"; then
    build="$(workflow_step_task "${response}" build)"
  fi

  _gt_settle lane_state "${lane}"
  number="$(task_field "${lane}" '.result_summary.git.pull_request.number // empty' 2>/dev/null || true)"

  # Phase 2: both need the lane's branch. The reader runs whatever the lane
  # ended (an absent branch is still one it must not create); the rebase
  # lane needs the lane's pull request.
  if [[ -n "${lane}" ]]; then
    _gt_choose_read "${dir}"
    read_args=(--lane-branch "swarm/${lane}")
    if [[ -n "$(jq -r '.repo // empty' "${dir}/read.json")" ]]; then
      read_args+=(--read-repo "$(jq -r '.repo' "${dir}/read.json")" --read-path "$(jq -r '.path' "${dir}/read.json")"
                  --read-ref "$(jq -r '.ref' "${dir}/read.json")")
    fi
    ACC_CHECK="git-tools: reader"
    acc_submit reader claude-code "$(jq -nc --arg p "$(_gt_prompt reader "${read_args[@]}")" '{prompt: $p}')" \
      "$(acc_repo_extra '{"max_attempts": 1, "strategy": "collect", "carrier": "branches"}')" || reader=""
  else
    printf '%s\n' "${empty}" >"${dir}/read.json"
  fi
  if [[ "${lane_state}" == "SUCCEEDED" && -n "${number}" ]]; then
    ACC_CHECK="git-tools: rebase"
    if acc_workflow response "$(jq -nc --argjson m "$(acc_metadata)" --arg t "${lane}" \
        --arg p "$(_gt_prompt rebase --lane-branch "swarm/${lane}")" '{
          priority: 10, metadata: $m, strategy: "direct-pr", continues_task: $t,
          steps: [{step_id: "rebase", runner_profile: "claude-code", input: {prompt: $p}}]}')"; then
      rebase="$(workflow_step_task "${response}" rebase)"
    fi
  else
    t_info "git-tools: the lane ended ${lane_state:-unsubmitted} with pull request '${number:-none}'; the rebase lane is not submitted"
  fi

  _gt_settle build_state "${build}"
  _gt_settle rebase_state "${rebase}"
  _gt_settle reader_state "${reader}"

  _gt_save "${dir}" lane "${lane}" "${lane_state}"
  _gt_save "${dir}" rebase "${rebase}" "${rebase_state}"
  _gt_save "${dir}" reader "${reader}" "${reader_state}"
  _gt_save "${dir}" build "${build}" "${build_state}"

  rm -f "${dir}/results.jsonl"
  python3 "${REPO_ROOT}/${GIT_TOOLS_JUDGE}" judge --dir "${dir}" --results "${dir}/results.jsonl" \
    || warn "git-tools: the judge exited non-zero"

  for (( i = 0; i < ${#names[@]}; i++ )); do
    key="${GIT_TOOLS_KEYS[i]}"
    acc_check "${names[i]}"
    line="$(jq -c --arg k "${key}" 'select(.check == $k)' "${dir}/results.jsonl" 2>/dev/null | head -n 1 || true)"
    verdict="$(jq -r '.verdict // empty' <<<"${line:-$empty}" 2>/dev/null || true)"
    reason="$(jq -r '.reason // empty' <<<"${line:-$empty}" 2>/dev/null | redact || true)"
    case "${verdict}" in
      PASS) acc_pass "${reason}" ;;
      SKIP) acc_skip "${reason}" ;;
      FAIL) acc_fail "${reason}" ;;
      *) acc_fail "the judge wrote no verdict for this check" ;;
    esac
  done
  t_info "git-tools tasks: lane ${lane:-none} rebase ${rebase:-none} reader ${reader:-none} build ${build:-none}"
  # The lane's pull request stays open for github-verify.sh to read back;
  # github-cleanup.sh closes it. With a token here, it is closed now.
  [[ -z "${number}" ]] || acc_close_pr "${number}" "swarm/${lane}"
  cancel_all ${lane:+"${lane}"} ${build:+"${build}"} ${rebase:+"${rebase}"} ${reader:+"${reader}"}
}
