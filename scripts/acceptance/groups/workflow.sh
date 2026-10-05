#!/usr/bin/env bash
# Acceptance group `workflow`: the DAG features, judged by what crossed each
# edge -- the bytes a step staged, the names a parent was told to write, the
# one pull request an integrate chain opens, and the cancel a failed parent
# sends down.
#
# The staging and failure checks use `mock`, which costs no quota. The
# implement -> review -> fix chain uses claude-code, because `integrate` is
# about agents' git work; its prompts are one sentence each.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

WF_PAYLOAD_NAME="payload.txt"

workflow_checks() {
  cat <<'EOF'
workflow: input_from stages the parent's artifact into the child byte for byte
workflow: expected_outputs tells the parent what to write, and a missing one fails it before the child runs
workflow: on_step_failure fail_workflow cancels every dependant, none of which ever held a lease
workflow: implement -> review -> fix with integrate opens one pull request, with the review's verdict.json staged into fix
EOF
}

run_workflow() {
  step "Acceptance: workflow"
  local stage="" missing="" cascade="" chain="" payload

  # Staging: `a` writes a known file; `b` stages it. The bytes are checked in
  # b's own checkpoint, which is b's workspace as b saw it.
  payload="$(printf 'staged across an edge\nrun %s \xc2\xb6\n' "${ACC_RUN_ID}")"$'\n'
  ACC_CHECK="workflow: input_from"
  acc_workflow stage "$(jq -nc --argjson m "$(acc_metadata)" --arg p "acceptance ${ACC_RUN_ID} stage" \
      --arg n "${WF_PAYLOAD_NAME}" --arg t "${payload}" '{
        priority: 10, metadata: $m,
        steps: [
          {step_id: "a", runner_profile: "mock", input: {prompt: $p, artifact_name: $n, artifact_text: $t, steps: 1, sleep_seconds: 0}},
          {step_id: "b", runner_profile: "mock", depends_on: ["a"], input_from: {a: $n}, input: {prompt: $p, steps: 1, sleep_seconds: 0}}
        ]}')" || stage=""

  # A parent that writes the wrong file: `x` writes other.txt, `y` stages
  # wanted.txt. x's clean exit must FAIL for the missing name, retryably, and
  # y must never start.
  ACC_CHECK="workflow: expected_outputs"
  acc_workflow missing "$(jq -nc --argjson m "$(acc_metadata)" --arg p "acceptance ${ACC_RUN_ID} missing" '{
        priority: 10, metadata: $m, on_step_failure: "fail_workflow",
        steps: [
          {step_id: "x", runner_profile: "mock", input: {prompt: $p, artifact_name: "other.txt", steps: 1, sleep_seconds: 0}},
          {step_id: "y", runner_profile: "mock", depends_on: ["x"], input_from: {x: "wanted.txt"}, input: {prompt: $p}}
        ]}')" || missing=""

  # A parent that fails outright, two generations of dependants below it.
  ACC_CHECK="workflow: on_step_failure"
  acc_workflow cascade "$(jq -nc --argjson m "$(acc_metadata)" --arg p "acceptance ${ACC_RUN_ID} cascade" '{
        priority: 10, metadata: $m, on_step_failure: "fail_workflow",
        steps: [
          {step_id: "root", runner_profile: "mock", input: {prompt: $p, fail: true, fail_message: "acceptance cascade root"}},
          {step_id: "child", runner_profile: "mock", depends_on: ["root"], input: {prompt: $p}},
          {step_id: "grandchild", runner_profile: "mock", depends_on: ["child"], input: {prompt: $p}}
        ]}')" || cascade=""

  # Under strategy integrate, only the integrator step (here, fix) ever
  # brings another step's tree in, at PR-open time -- "strategy 'integrate'
  # makes one workflow step apply every other step's work and open ONE pull
  # request" (apps/swarm-mcp/swarm_mcp/server.py's `_dispatch_strategy`).
  # Every other step, review included, clones repository_ref like any
  # standalone task (docs/workflows.md's `input_from` section: staging is the
  # only channel a step's own artifact crosses to a dependant; there is no
  # implicit branch inheritance outside the single-pr strategy's documented
  # chain). So for review to judge implement's actual change rather than
  # whatever repository_ref alone contains, implement stages that change as
  # an artifact and review applies it itself before judging. implement runs
  # only the two git plumbing commands needed to produce that artifact, and
  # review reverses the patch once it has read the file, so review still
  # pushes nothing -- it stays a reader, not a second contributor for fix to
  # merge.
  ACC_CHECK="workflow: integrate chain"
  acc_workflow chain "$(jq -nc --argjson m "$(acc_metadata)" --arg u "${ACC_REPOSITORY_URL}" --arg r "${ACC_REF}" \
      --arg fx "${CC_FIXTURE}" --arg run "${ACC_RUN_ID}" '{
        priority: 10, metadata: $m, on_step_failure: "fail_workflow",
        strategy: "integrate", repository_url: $u, repository_ref: $r,
        steps: [
          {step_id: "implement", runner_profile: "claude-code",
           input: {prompt: ("In " + $fx + ", add() returns a - b; make it return a + b. Change only that line and no other file. Then run: git add -A && git diff --cached --binary HEAD -- and save the output of that command verbatim to $SWARM_ARTIFACTS_DIR/change.diff. Do not run anything else, and do not commit.")}},
          {step_id: "review", runner_profile: "claude-code", depends_on: ["implement"], input_from: {implement: "change.diff"},
           input: {prompt: ("First apply the staged change.diff to this checkout: git apply --index (its path was named above). Then read add() in " + $fx + " and write verdict.json to $SWARM_ARTIFACTS_DIR containing {\"verdict\": \"MERGE\"} if it now returns a + b, otherwise {\"verdict\": \"CHANGES\"}. Finally reverse that same patch (git apply --reverse --index) so the checkout is left exactly as it was cloned. Do not commit.")}},
          {step_id: "fix", runner_profile: "claude-code", depends_on: ["review"], input_from: {review: "verdict.json"},
           input: {prompt: ("Read verdict.json. Write its verdict value alone to $SWARM_ARTIFACTS_DIR/verdict-seen.txt. Write a one-line pull request title naming the add() fix, with no task id, to $SWARM_ARTIFACTS_DIR/pr-title.txt, and the single line acceptance-run:" + $run + " to $SWARM_ARTIFACTS_DIR/pr-body.md. Change no repository file.")}}
        ]}')" || chain=""

  _wf_check_staging "${stage}" "${payload}"
  _wf_check_expected_outputs "${stage}" "${missing}"
  _wf_check_cascade "${cascade}"
  _wf_check_integrate "${chain}"
}

# _wf_final_state WORKFLOW_ID -> the derived workflow state.
_wf_final_state() {
  local out="${ACC_WORK}/wf-state.json"
  api_fetch "/workflows/$1" "${out}" || { printf 'unreadable'; return 0; }
  jq -r '.workflow.state // "none"' "${out}"
}

_wf_check_staging() {
  local wf="$1" payload="$2" a b state ckpt listing member file content
  acc_check "workflow: input_from stages the parent's artifact into the child byte for byte"
  [[ -n "${wf}" ]] || { acc_fail "not submitted"; return 0; }
  a="$(workflow_step_task "${wf}" a)"
  b="$(workflow_step_task "${wf}" b)"
  acc_run_to_end state "${b}" || return 0
  acc_assert_eq "SUCCEEDED SUCCEEDED" "$(task_state "${a}") ${state}" "parent and child" "${b}"
  # b's last checkpoint is its workspace (work/) at the end, so the staged
  # file is in it exactly as it landed.
  ckpt="$(acc_events "${b}" | acc_checkpoint_ids | tail -n 1)"
  if [[ -z "${ckpt}" ]]; then
    acc_fail "the child wrote no checkpoint to read its workspace from" "${b}"
    return 0
  fi
  listing="${ACC_WORK}/wf-listing.json"
  if ! api_fetch "/tasks/${b}/checkpoints/${ckpt}/files" "${listing}"; then
    acc_fail "could not list ${ckpt}'s files" "${b}"
    return 0
  fi
  member="$(jq -r --arg n "${WF_PAYLOAD_NAME}" '[.files[]? | (.path // .name) | select(. == $n or endswith("/" + $n))] | first // empty' "${listing}")"
  if [[ -z "${member}" ]]; then
    acc_fail "${WF_PAYLOAD_NAME} is not in the child's workspace (${ckpt})" "${b}"
    return 0
  fi
  file="${ACC_WORK}/wf-member.json"
  if ! api_fetch "/tasks/${b}/checkpoints/${ckpt}/files/${member}" "${file}"; then
    acc_fail "could not read ${member} from ${ckpt}" "${b}"
    return 0
  fi
  if [[ "$(jq -r '.truncated // false' "${file}")" == "true" ]]; then
    acc_fail "the read of ${member} was truncated, so its bytes cannot be compared whole" "${b}"
    return 0
  fi
  # jq -j writes the string exactly, adding no newline.
  content="${ACC_WORK}/wf-member.txt"
  jq -j '.content // ""' "${file}" >"${content}"
  if cmp -s <(printf '%s' "${payload}") "${content}"; then
    acc_pass "${member} in the child's workspace is the parent's ${WF_PAYLOAD_NAME}, $(wc -c <"${content}" | tr -d ' ') bytes, identical" "${b}"
  else
    acc_fail "${member} differs from what the parent wrote: $(cmp <(printf '%s' "${payload}") "${content}" 2>&1 | head -n 1)" "${b}"
  fi
}

_wf_check_expected_outputs() {
  local stage="$1" wf="$2" a x y state declared attempts max missing_names leases
  acc_check "workflow: expected_outputs tells the parent what to write, and a missing one fails it before the child runs"
  if [[ -n "${stage}" ]]; then
    a="$(workflow_step_task "${stage}" a)"
    declared="$(task_field "${a}" '.metadata.expected_outputs // [] | join(",")')"
    acc_assert_eq "${WF_PAYLOAD_NAME}" "${declared}" "the parent's metadata.expected_outputs names what its child stages" "${a}"
  fi
  [[ -n "${wf}" ]] || { acc_fail "the missing-output workflow was not submitted"; return 0; }
  x="$(workflow_step_task "${wf}" x)"
  y="$(workflow_step_task "${wf}" y)"
  acc_run_to_end state "${x}" || return 0
  acc_assert_eq "FAILED outputs_missing" "${state} $(task_field "${x}" '.end_cause // "none"')" "the parent that wrote other.txt" "${x}"
  missing_names="$(task_field "${x}" '.result_summary.expected_outputs_missing // [] | map(if type == "object" then (.name // tostring) else tostring end) | join(",")')"
  if [[ "${missing_names}" == *wanted.txt* ]]; then
    acc_pass "result_summary.expected_outputs_missing names wanted.txt" "${x}"
  else
    acc_fail "result_summary.expected_outputs_missing is '${missing_names}', not wanted.txt" "${x}"
  fi
  attempts="$(task_field "${x}" '.attempt_count // 0')"
  max="$(task_field "${x}" '.max_attempts // 0')"
  if [[ "${attempts}" -ge 2 && "${attempts}" == "${max}" ]]; then
    acc_pass "retried to max_attempts (${attempts}/${max}): a missing output fails the attempt retryably" "${x}"
  else
    acc_fail "attempts ${attempts} of ${max}: a missing output should be retried until max_attempts" "${x}"
  fi
  if ! state="$(wait_for_state "${y}" "CANCELLED|FAILED|SUCCEEDED|DEAD_LETTERED" 180)"; then
    acc_fail "the child is ${state}, 180 s after its parent failed" "${y}"
    return 0
  fi
  acc_assert_eq "CANCELLED" "${state}" "the child of the failed parent" "${y}"
  leases="$(task_lease_ids "${y}")"
  if [[ -z "${leases}" ]]; then
    acc_pass "the child never held a lease: it did not start on a parent that did not write what it promised" "${y}"
  else
    acc_fail "the child held lease(s) ${leases//$'\n'/ }" "${y}"
  fi
}

_wf_check_cascade() {
  local wf="$1" root child grandchild state id leases wid
  acc_check "workflow: on_step_failure fail_workflow cancels every dependant, none of which ever held a lease"
  [[ -n "${wf}" ]] || { acc_fail "not submitted"; return 0; }
  root="$(workflow_step_task "${wf}" root)"
  child="$(workflow_step_task "${wf}" child)"
  grandchild="$(workflow_step_task "${wf}" grandchild)"
  acc_run_to_end state "${root}" || return 0
  acc_assert_eq "FAILED" "${state}" "the root" "${root}"
  for id in "${child}" "${grandchild}"; do
    if ! state="$(wait_for_state "${id}" "CANCELLED|FAILED|SUCCEEDED|DEAD_LETTERED" 180)"; then
      acc_fail "still ${state} 180 s after the root failed" "${id}"
      continue
    fi
    # end_cause is workflow_sweep, not failed_parent: `_stop_for_failed_workflow`
    # runs ahead of the depends_on cascade in `_admit_one`
    # (apps/scheduler/scheduler/loop.py `_sweep_failed_workflow`, which always
    # passes `end_cause=EndCause.WORKFLOW_SWEEP`) and catches every not-started
    # step of a fail_workflow workflow -- dependent or not -- before the
    # per-parent `failed_parent` check is ever reached. Confirmed on dev by the
    # manual acceptance pass, row 14 (PR #358 comment, 2026-09-30).
    acc_assert_eq "CANCELLED workflow_sweep" "${state} $(task_field "${id}" '.end_cause // "none"')" "a dependant of the failed root" "${id}"
    leases="$(task_lease_ids "${id}")"
    if [[ -z "${leases}" ]]; then
      acc_pass "never held a lease" "${id}"
    else
      acc_fail "held lease(s) ${leases//$'\n'/ }" "${id}"
    fi
  done
  wid="$(jq -r '.workflow.workflow_id' <<<"${wf}")"
  acc_assert_eq "FAILED" "$(_wf_final_state "${wid}")" "the workflow's derived state" "${wid}"
}

_wf_check_integrate() {
  local wf="$1" implement review fix state id git prs number branch verdict seen pr files merged staged
  acc_check "workflow: implement -> review -> fix with integrate opens one pull request, with the review's verdict.json staged into fix"
  [[ -n "${wf}" ]] || { acc_fail "not submitted"; return 0; }
  implement="$(workflow_step_task "${wf}" implement)"
  review="$(workflow_step_task "${wf}" review)"
  fix="$(workflow_step_task "${wf}" fix)"
  acc_run_to_end state "${fix}" || { acc_close_pr "" "swarm/${implement}" "swarm/${review}" "swarm/${fix}"; return 0; }
  acc_assert_eq "SUCCEEDED SUCCEEDED SUCCEEDED" "$(task_state "${implement}") $(task_state "${review}") ${state}" "implement, review, fix" "${fix}"

  # ONE pull request: the integrator's. A contributor pushes and stops.
  prs=""
  for id in "${implement}" "${review}" "${fix}"; do
    number="$(task_field "${id}" '.result_summary.git.pull_request.number // empty')"
    [[ -z "${number}" ]] || prs="${prs} ${id}:#${number}"
  done
  acc_assert_eq " ${fix}:#$(task_field "${fix}" '.result_summary.git.pull_request.number // "none"')" "${prs}" "exactly one pull request, opened by fix" "${fix}"
  git="$(task_field "${fix}" '.result_summary.git // {}')"
  number="$(jq -r '.pull_request.number // empty' <<<"${git}")"
  branch="$(jq -r '.branch // empty' <<<"${git}")"

  # The review's verdict reached fix: what review wrote is what fix read.
  verdict="$(acc_artifact_text "${review}" "verdict.json" | jq -r '.verdict // empty' 2>/dev/null || true)"
  seen="$(acc_artifact_text "${fix}" "verdict-seen.txt" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ -z "${verdict}" ]]; then
    acc_fail "review wrote no readable verdict.json" "${review}"
  elif [[ "${verdict}" == "${seen}" ]]; then
    acc_pass "review's verdict.json says ${verdict}, and fix read ${seen} from its staged copy" "${fix}"
  else
    acc_fail "review's verdict.json says '${verdict}', fix read '${seen:-nothing}'" "${fix}"
  fi
  # review's checkout, under strategy integrate, clones repository_ref like
  # every non-integrator step (docs/workflows.md's input_from section, and
  # swarm_mcp/server.py's `_dispatch_strategy`: "strategy 'integrate' makes
  # one workflow step apply every other step's work" -- singular, the
  # integrator, at PR-open time; nothing else in the chain ever sees another
  # step's tree). So review reads implement's change only if the spec stages
  # it: "implement" writes change.diff and review applies it before judging
  # (see the workflow spec above). This is the precondition the MERGE
  # assertion below depends on -- without it, review silently judges whatever
  # repository_ref alone contains (the bug), and every run would report
  # CHANGES regardless of what implement did.
  staged="$(task_field "${review}" '.result_summary.staged_inputs // []')"
  if jq -e --arg imp "${implement}" \
      'any(.[]?; (.filename // "") == "change.diff" and (.task_id // "") == $imp)' \
      <<<"${staged}" >/dev/null 2>&1; then
    acc_pass "review's result_summary.staged_inputs lists implement's change.diff" "${review}"
  else
    acc_fail "review's staged_inputs does not list change.diff from ${implement}: ${staged}" "${review}"
  fi

  # With that staged, MERGE is what a correct fix should get: this asserts
  # the value, not merely its shape, on purpose -- relaxing it to "any
  # declared verdict" would hide exactly the defect this check exists to
  # catch (round 2 of #358's triage: review judged the UNFIXED fixture
  # because its checkout never contained implement's change, confirmed via
  # `uv run swarm artifact task_feccd6c41e1b498b89a7 verdict.json` and its
  # answer text, which named the still-unfixed line by number).
  acc_assert_eq "MERGE" "${verdict}" "the review judged the implement step's fix" "${review}"

  if [[ -z "${number}" ]]; then
    acc_fail "no pull request to read back (number 'none')" "${fix}"
  elif ! acc_github_can_read; then
    # The pull request's body and diff live on the private sandbox.
    acc_skip "PR #${number}'s merged-branch list and diff: $(acc_github_skip_reason)" "${fix}"
  elif pr="$(acc_github GET "/repos/${ACC_GITHUB_REPO}/pulls/${number}")"; then
    merged="$(jq -r '.body // ""' <<<"${pr}" | acc_merged_branches | tr '\n' ' ')"
    if [[ "${merged}" == *"swarm/${implement}"* ]]; then
      acc_pass "PR #${number} integrates the implement step's branch" "${fix}"
    else
      acc_fail "PR #${number}'s body does not list swarm/${implement} as merged (lists: ${merged:-none})" "${fix}"
    fi
    # Measured against ACC_REF, not the PR's base -- see acc_pr_files_vs_ref.
    if files="$(acc_pr_files_vs_ref "${branch}")" \
        && jq -r '.[].patch // ""' <<<"${files}" | grep -qxF -- "-${CC_BUG_LINE}"; then
      acc_pass "PR #${number}'s diff removes the bug line" "${fix}"
    else
      acc_fail "PR #${number}'s diff does not remove the bug line" "${fix}"
    fi
  else
    acc_fail "could not read PR #${number} back from ${ACC_GITHUB_REPO}" "${fix}"
  fi
  acc_close_pr "${number}" "${branch}" "swarm/${implement}" "swarm/${review}"
}
