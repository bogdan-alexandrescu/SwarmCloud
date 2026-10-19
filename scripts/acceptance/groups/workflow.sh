#!/usr/bin/env bash
# Acceptance group `workflow`: the DAG features, judged by what crossed each
# edge -- the bytes a step staged, the names a parent was told to write, the
# one pull request an integrate chain opens, and the cancel a failed parent
# sends down -- and a file written before a quota park, carried to the
# attempt that succeeds and staged from there (#166).
#
# The staging, carry and failure checks use `mock`, which costs no quota. The
# implement -> review -> fix chain uses claude-code, because `integrate` is
# about agents' git work; its prompts are one sentence each.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

WF_PAYLOAD_NAME="payload.txt"
WF_CARRY_NAME="notes.md"

# The await-input checks (#1436, docs/design/human-steps.md §6 row HW-ACCEPT).
# `draft` writes WF_DRAFT_NAME, `review` waits for a person and is answered
# with a verdict form saved as WF_REVIEW_NAME plus one text file, which the
# worker stages into `revise` under WF_REVIEW_FILES_DIR (the artifact's stem
# plus `.files/`, apps/agent-worker/agent_worker/inputs.py attachments_dir).
WF_DRAFT_NAME="draft.md"
WF_REVIEW_NAME="review.json"
WF_REVIEW_FILES_DIR="review.files"
WF_ATTACHMENT_NAME="reviewer-notes.md"
# An hour: the answered wait must outlive everything this group runs before
# _wf_check_await posts the answer (the integrate chain is the long one, and
# the group takes about 13 minutes), so it is open when answered. Far inside
# the 24-hour bound (validation.py AWAIT_INPUT_MAX_SECONDS).
WF_AWAIT_EXPIRES_SECONDS=3600
# The expiring wait: 300 s, the smallest `expires_after_seconds` the API takes,
# so the default lands about six minutes after the park -- the 300 s plus at
# most one schedule tick (every minute) -- while the other checks run.
WF_EXPIRE_SECONDS=300
# THE AWAIT-INPUT CHECKS RUN IN THE CALLER'S OWN SERVICE TENANT (owner
# decision 2026-10-19, HW-ACCEPT-ID; docs/design/human-steps.md). swarm-api
# takes an answer from a person who is a member, or from a service tenant's
# own principal acting in that tenant (awaitinput.py `service_principal`); a
# service account in the GROUP tenant ACC_TENANT is 403 `person_required`, and
# that is not relaxed. So both workflows are submitted, read and answered with
# X-Swarm-Tenant set to WF_OWN_TENANT -- swarm-verify's u-sw-c90291, the path
# oracle-api's u-or-9a0792 takes too -- which a service account may name
# (auth.py `_with_own_service_tenant`) without its default leaving the group.
# Derived at run time from the caller's verified email (GET /tenants/me) with
# common.sh's personal_tenant_id, never typed in. Every other check stays in
# ACC_TENANT. Their mock steps run on that tenant's own mock job (dev.tfvars
# declares u-sw-c90291; await-input is control_plane and needs no job).
WF_OWN_TENANT=""
WF_OWN_PROBLEM=""

# ONE DRAIN, as seconds: the scheduler's safety tick is one minute (a lost
# task_finished wake costs at most that, apps/scheduler/scheduler/main.py),
# and the answer's own wake promotes and admits the dependant in the same
# drain. A revise leased later than this after the answer waited for
# something other than the drain.
WF_DRAIN_SECONDS=60

workflow_checks() {
  cat <<'EOF'
workflow: input_from stages the parent's artifact into the child byte for byte
workflow: expected_outputs tells the parent what to write, and a missing one fails it before the child runs
workflow: on_step_failure fail_workflow cancels every dependant, none of which ever held a lease
workflow: implement -> review -> fix with integrate opens one pull request, with the review's verdict.json staged into fix
workflow: carry: a file written before a quota park reaches the attempt that succeeds by reference, and the child stages it
workflow: await-input: a step waits for a member's answer holding no lease, and revise runs within one drain holding the posted form and file
workflow: await-input expiry: a step nobody answers ends at expires_after with its declared default, and the when-gated steps take that branch
EOF
}

run_workflow() {
  step "Acceptance: workflow"
  local stage="" missing="" cascade="" chain="" carry="" await="" expire="" payload notes expire_default

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

  # #166's carry, live: docs/workflows.md's recipe, as the owner asked for on
  # 2026-10-08. `a` writes notes.md and then parks on quota
  # (artifact_before_park), and the attempt after the park writes nothing
  # under that name, so the notes.md the task ends with can only be the
  # parked attempt's, carried by reference. 60 s is over the worker's 45 s
  # in-place retry ceiling (max_in_worker_retry_delay_seconds), so it parks.
  notes="$(printf 'written before the park\nrun %s\n' "${ACC_RUN_ID}")"$'\n'
  ACC_CHECK="workflow: carry"
  acc_workflow carry "$(jq -nc --argjson m "$(acc_metadata)" --arg p "acceptance ${ACC_RUN_ID} carry" \
      --arg n "${WF_CARRY_NAME}" --arg t "${notes}" '{
        priority: 10, metadata: $m,
        steps: [
          {step_id: "a", runner_profile: "mock", input: {prompt: $p, quota_exhausted: true, retry_after_seconds: 60, artifact_before_park: true, artifact_name: $n, artifact_text: $t}},
          {step_id: "b", runner_profile: "mock", depends_on: ["a"], input_from: {a: $n}, input: {prompt: $p, steps: 1, sleep_seconds: 0}}
        ]}')" || carry=""

  # The caller's own tenant, for the two await-input workflows below.
  _wf_own_tenant || true

  # #1436's loop, live: draft -> review (a person) -> revise. `review` is
  # answered by _wf_check_await, which runs after the long checks, so it has
  # sat parked for minutes by then -- long enough for a lease to show if the
  # scheduler ever took one for it. max_files 1: the answer carries one file.
  ACC_CHECK="workflow: await-input"
  [[ -n "${WF_OWN_TENANT}" ]] && _wf_in_own_tenant acc_workflow await "$(jq -nc --argjson m "$(acc_metadata)" --arg p "acceptance ${ACC_RUN_ID} await" \
      --arg d "${WF_DRAFT_NAME}" --arg t "$(printf 'draft to review\nrun %s\n' "${ACC_RUN_ID}")" \
      --arg r "${WF_REVIEW_NAME}" --argjson e "${WF_AWAIT_EXPIRES_SECONDS}" '{
        priority: 10, metadata: $m, on_step_failure: "fail_workflow",
        steps: [
          {step_id: "draft", runner_profile: "mock", input: {prompt: $p, artifact_name: $d, artifact_text: $t, steps: 1, sleep_seconds: 0}},
          {step_id: "review", runner_profile: "await-input", depends_on: ["draft"], input_from: {draft: $d},
           input: {prompt: ("Read " + $d + ". Approve it, or list what must change.")},
           await_input: {form: "verdict", artifact: $r, max_files: 1, expires_after_seconds: $e, on_expire: "fail"}},
          {step_id: "revise", runner_profile: "mock", depends_on: ["review"], input_from: {review: $r}, input: {prompt: $p, steps: 1, sleep_seconds: 0}}
        ]}')" || await=""

  # The wait nobody answers: `ask` expires after 300 s with on_expire
  # "default", and the declared default says NOT_YET. Two steps are gated on
  # it, one on each verdict, so the branch taken is read against its
  # opposite: `revise` runs its agent and `ship` does not. Were the default
  # never applied, or applied as MERGE, the two would come out the other way.
  ACC_CHECK="workflow: await-input expiry"
  expire_default="$(jq -nc --arg run "${ACC_RUN_ID}" '{verdict: "NOT_YET", findings: [("acceptance " + $run + " default")]}')"
  [[ -n "${WF_OWN_TENANT}" ]] && _wf_in_own_tenant acc_workflow expire "$(jq -nc --argjson m "$(acc_metadata)" --arg p "acceptance ${ACC_RUN_ID} expire" \
      --argjson dflt "${expire_default}" --argjson e "${WF_EXPIRE_SECONDS}" '{
        priority: 10, metadata: $m, on_step_failure: "fail_workflow",
        steps: [
          {step_id: "ask", runner_profile: "await-input",
           input: {prompt: "Nobody answers this step: it ends with its declared default."},
           await_input: {form: "verdict", artifact: "verdict.json", max_files: 0, expires_after_seconds: $e,
                         on_expire: "default", default: $dflt}},
          {step_id: "revise", runner_profile: "mock", depends_on: ["ask"], input_from: {ask: "verdict.json"},
           when: {step: "ask", verdict_in: ["NOT_YET"]}, input: {prompt: $p, steps: 1, sleep_seconds: 0}},
          {step_id: "ship", runner_profile: "mock", depends_on: ["ask"], input_from: {ask: "verdict.json"},
           when: {step: "ask", verdict_in: ["MERGE"]}, input: {prompt: $p, steps: 1, sleep_seconds: 0}}
        ]}')" || expire=""

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
  _wf_check_carry "${carry}" "${notes}"
  _wf_in_own_tenant _wf_check_await "${await}"
  _wf_in_own_tenant _wf_check_expire "${expire}" "${expire_default}"
}

# _wf_own_tenant -> sets WF_OWN_TENANT to the id of this caller's own tenant,
# derived from the email GET /tenants/me verified, or leaves it empty and says
# why in WF_OWN_PROBLEM. Called directly, never in $(), so both assignments
# land in this shell.
_wf_own_tenant() {
  local out="${ACC_WORK}/wf-me.json" email own
  WF_OWN_TENANT=""
  WF_OWN_PROBLEM=""
  if ! api_fetch "/tenants/me" "${out}"; then
    WF_OWN_PROBLEM="GET /tenants/me answered HTTP ${API_STATUS}, so this caller's own tenant is not known"
    return 1
  fi
  email="$(jq -r '.principal.email // empty' "${out}")"
  if [[ -z "${email}" ]] || ! own="$(personal_tenant_id "${email}")"; then
    WF_OWN_PROBLEM="this caller's own tenant could not be derived from GET /tenants/me's principal.email '${email:-none}' (common.sh personal_tenant_id)"
    return 1
  fi
  if [[ ! "${own}" =~ ^u-[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
    WF_OWN_PROBLEM="derived '${own}' for this caller's own tenant, which is not a personal tenant id"
    return 1
  fi
  WF_OWN_TENANT="${own}"
  t_info "await-input checks run in this caller's own tenant ${WF_OWN_TENANT} (X-Swarm-Tenant)"
}

# _wf_in_own_tenant COMMAND... -> COMMAND with every API call it makes
# carrying X-Swarm-Tenant: WF_OWN_TENANT instead of ACC_TENANT: api_request,
# acc_raw_artifact and _wf_post_answer all send SWARM_API_TENANT, and the
# local shadows it for COMMAND and everything it calls, and no longer. A
# function, not a subshell, so what COMMAND assigns (acc_workflow's VAR,
# ACC_TASKS, ACC_OTHER_TASKS, the recorded results) stays in this shell.
_wf_in_own_tenant() {
  local SWARM_API_TENANT="${WF_OWN_TENANT:-${SWARM_API_TENANT}}"
  "$@"
}

# _wf_final_state WORKFLOW_ID -> the derived workflow state.
_wf_final_state() {
  local out="${ACC_WORK}/wf-state.json"
  api_fetch "/workflows/$1" "${out}" || { printf 'unreadable'; return 0; }
  jq -r '.workflow.state // "none"' "${out}"
}

_wf_check_staging() {
  local wf="$1" payload="$2" a b state
  acc_check "workflow: input_from stages the parent's artifact into the child byte for byte"
  [[ -n "${wf}" ]] || { acc_fail "not submitted"; return 0; }
  a="$(workflow_step_task "${wf}" a)"
  b="$(workflow_step_task "${wf}" b)"
  acc_run_to_end state "${b}" || return 0
  acc_assert_eq "SUCCEEDED SUCCEEDED" "$(task_state "${a}") ${state}" "parent and child" "${b}"
  _wf_check_child_copy "${b}" "${WF_PAYLOAD_NAME}" "${payload}"
}

# _wf_check_child_copy CHILD NAME EXPECTED -> PASS when the staged file NAME
# in CHILD's last checkpoint is EXPECTED byte for byte.
_wf_check_child_copy() {
  local b="$1" name="$2" payload="$3" content
  content="${ACC_WORK}/wf-member.txt"
  _wf_workspace_file "${b}" "${name}" "${content}" || return 0
  if cmp -s <(printf '%s' "${payload}") "${content}"; then
    acc_pass "${WF_MEMBER} in the child's workspace is the parent's ${name}, $(wc -c <"${content}" | tr -d ' ') bytes, identical" "${b}"
  else
    acc_fail "${WF_MEMBER} differs from what the parent wrote: $(cmp <(printf '%s' "${payload}") "${content}" 2>&1 | head -n 1)" "${b}"
  fi
}

# _wf_workspace_file CHILD NAME OUT -> 0 with the bytes of the staged file
# NAME, as CHILD's last checkpoint holds it, in OUT, and its path in the
# checkpoint in WF_MEMBER; otherwise records a FAIL and returns 1. The child's
# last checkpoint is its workspace (work/) at the end, so a staged file is in
# it exactly as it landed. NAME may carry a directory (review.files/x.md).
WF_MEMBER=""
_wf_workspace_file() {
  local b="$1" name="$2" out="$3" ckpt listing file
  WF_MEMBER=""
  ckpt="$(acc_events "${b}" | acc_checkpoint_ids | tail -n 1)"
  if [[ -z "${ckpt}" ]]; then
    acc_fail "the child wrote no checkpoint to read its workspace from" "${b}"
    return 1
  fi
  listing="${ACC_WORK}/wf-listing.json"
  if ! api_fetch "/tasks/${b}/checkpoints/${ckpt}/files" "${listing}"; then
    acc_fail "could not list ${ckpt}'s files" "${b}"
    return 1
  fi
  WF_MEMBER="$(jq -r --arg n "${name}" '[.files[]? | (.path // .name) | select(. == $n or endswith("/" + $n))] | first // empty' "${listing}")"
  if [[ -z "${WF_MEMBER}" ]]; then
    acc_fail "${name} is not in the child's workspace (${ckpt})" "${b}"
    return 1
  fi
  file="${ACC_WORK}/wf-member.json"
  if ! api_fetch "/tasks/${b}/checkpoints/${ckpt}/files/${WF_MEMBER}" "${file}"; then
    acc_fail "could not read ${WF_MEMBER} from ${ckpt}" "${b}"
    return 1
  fi
  if [[ "$(jq -r '.truncated // false' "${file}")" == "true" ]]; then
    acc_fail "the read of ${WF_MEMBER} was truncated, so its bytes cannot be compared whole" "${b}"
    return 1
  fi
  # jq -j writes the string exactly, adding no newline.
  jq -j '.content // ""' "${file}" >"${out}"
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

# _wf_check_carry WORKFLOW_ID ARTIFACT_TEXT: #166, live. A's parked attempt
# wrote notes.md; the attempt that succeeded wrote nothing under that name,
# so A's notes.md can only be the parked attempt's object, listed by reference
# with `carried_from` (Worker._carry_parked_uploads, read back by id through
# ControlPlane.parked_uploads), and B stages that object. The dev 403 that
# reopened #166 was a live measurement; this is the opposite one.
#
# acc_run_to_end's ACC_TIMEOUT (900 s by default) covers A's 60 s park plus
# two mock attempts and B's one, with room for admission.
_wf_check_carry() {
  local wf="$1" notes="$2" a b state attempts events parked final artifacts entry count carried uri staged object
  acc_check "workflow: carry: a file written before a quota park reaches the attempt that succeeds by reference, and the child stages it"
  [[ -n "${wf}" ]] || { acc_fail "not submitted"; return 0; }
  a="$(workflow_step_task "${wf}" a)"
  b="$(workflow_step_task "${wf}" b)"
  acc_run_to_end state "${b}" || return 0
  acc_assert_eq "SUCCEEDED SUCCEEDED" "$(task_state "${a}") ${state}" "parent and child" "${b}"

  attempts="$(task_field "${a}" '.attempt_count // 0')"
  if [[ "${attempts}" -ge 2 ]]; then
    acc_pass "the parent ran ${attempts} attempts: it parked and resumed" "${a}"
  else
    acc_fail "the parent ran ${attempts} attempt(s); the recipe parks its first, so it needs at least 2" "${a}"
  fi

  # The parked attempt is the one the park's own events name (each event
  # carries its attempt_id; control.py `_event_write`), and the finishing
  # attempt is the one that wrote `succeeded`.
  events="${ACC_WORK}/wf-carry-events.jsonl"
  if ! acc_events "${a}" >"${events}"; then
    acc_fail "could not read the parent's events" "${a}"
    return 0
  fi
  parked="$(jq -sr '[.[] | select(.type == "parked" or .type == "quota_exhausted") | select(.attempt_id != null)] | sort_by(.at) | first | .attempt_id // empty' "${events}")"
  final="$(jq -sr '[.[] | select(.type == "succeeded") | select(.attempt_id != null)] | sort_by(.at) | last | .attempt_id // empty' "${events}")"
  if [[ -z "${parked}" ]]; then
    acc_fail "the parent has no parked or quota_exhausted event naming an attempt: it never parked, so nothing was carried" "${a}"
    return 0
  fi
  if [[ -n "${final}" && "${final}" != "${parked}" ]]; then
    acc_pass "parked on attempt ${parked}, succeeded on attempt ${final}" "${a}"
  else
    acc_fail "the parent's succeeded event names attempt '${final:-none}', not one after the parked ${parked}" "${a}"
  fi

  # A's notes.md: one entry, carried from the parked attempt, pointing at its
  # object -- and none under the finishing attempt's prefix, which the mock
  # never wrote.
  artifacts="$(task_field "${a}" '.result_summary.artifacts // []')"
  count="$(jq --arg n "${WF_CARRY_NAME}" '[.[]? | select(.name == $n)] | length' <<<"${artifacts}")"
  entry="$(jq -c --arg n "${WF_CARRY_NAME}" '[.[]? | select(.name == $n)] | first // {}' <<<"${artifacts}")"
  carried="$(jq -r '.carried_from // "none"' <<<"${entry}")"
  uri="$(jq -r '.uri // ""' <<<"${entry}")"
  object="/tasks/${a}/attempts/${parked}/artifacts/${WF_CARRY_NAME}"
  acc_assert_eq "1 ${parked}" "${count} ${carried}" "${WF_CARRY_NAME} entries in result_summary.artifacts, and its carried_from" "${a}"
  if [[ "${uri}" == *"${object}" ]]; then
    acc_pass "${WF_CARRY_NAME}'s uri is the parked attempt's object (${object})" "${a}"
  else
    acc_fail "${WF_CARRY_NAME}'s uri is '${uri:-none}', not the parked attempt's ${object}" "${a}"
  fi
  if [[ -n "${final}" ]] && jq -e --arg p "/attempts/${final}/artifacts/${WF_CARRY_NAME}" \
      'any(.[]?; (.uri // "") | endswith($p))' <<<"${artifacts}" >/dev/null 2>&1; then
    acc_fail "result_summary.artifacts lists a ${WF_CARRY_NAME} under the finishing attempt ${final}: the file was rewritten, not carried" "${a}"
  else
    acc_pass "nothing under the finishing attempt's prefix is named ${WF_CARRY_NAME}" "${a}"
  fi

  # B staged the parked attempt's object, and it arrived whole.
  staged="$(task_field "${b}" '.result_summary.staged_inputs // []')"
  if jq -e --arg a "${a}" --arg n "${WF_CARRY_NAME}" --arg o "${object}" \
      'any(.[]?; (.filename // "") == $n and (.task_id // "") == $a and ((.uri // "") | endswith($o)))' \
      <<<"${staged}" >/dev/null 2>&1; then
    acc_pass "the child's result_summary.staged_inputs names ${WF_CARRY_NAME} from the parked attempt's object" "${b}"
  else
    acc_fail "the child's staged_inputs does not name ${WF_CARRY_NAME} at ${object}: ${staged}" "${b}"
  fi
  _wf_check_child_copy "${b}" "${WF_CARRY_NAME}" "${notes}"
}

# A jq prelude: `epoch` turns an ISO-8601 timestamp, as Firestore's REST
# encoding and swarm-api's `_iso` both write one (fractional seconds, `Z` or
# `+00:00`), into epoch seconds; anything else into null.
WF_JQ_EPOCH='def epoch: if type == "string" then (try (sub("\\.[0-9]+"; "") | sub("(Z|[+]00:00)$"; "") + "Z" | fromdateiso8601) catch null) else null end;'

# _wf_awaiting TASK -> true while TASK is PARKED(AWAITING_INPUT), the wait.
# A predicate for wait_until: a failed read is "not yet", never a pass.
_wf_awaiting() {
  local doc
  doc="$(task_doc "$1")" || return 1
  [[ "$(jq -r '"\(.state // "MISSING") \(.park_reason // "none")"' <<<"${doc}")" == "PARKED AWAITING_INPUT" ]]
}

# _wf_lease_docs TASK -> how many lease documents name TASK, read from the
# leases collection itself rather than the task's events, so a lease the
# scheduler wrote without its event is still counted. Fails on a read that did
# not answer.
_wf_lease_docs() {
  local docs
  docs="$(fs_query leases "$(fs_field_filter task_id EQUAL "$(jq -nc --arg v "$1" '{stringValue:$v}')")" 50 \
    | jq -c "${FS_JQ} doc")" || return 1
  printf '%s' "${docs}" | awk 'NF' | wc -l | tr -d ' '
}

# _wf_post_answer WORKFLOW_ID STEP_ID ANSWER_FILE ATTACHMENT OUT -> 0 when
# swarm-api took the answer (200), with its response in OUT; the HTTP status
# is in WF_ANSWER_STATUS either way ("000" when curl itself failed).
#
# multipart/form-data, as the console sends an answer with files: the
# `answer` part is the JSON body ({answer_id, payload}) and the `file` part
# the attachment, named WF_ATTACHMENT_NAME. curl sends the Content-Length the
# route requires (411 without it) because both parts are read from files. Not
# api_request, which sends JSON only; the header goes in on stdin, as
# acc_raw_artifact sends it, and X-Swarm-Tenant with it -- SWARM_API_TENANT,
# as api_request sends it, which _wf_in_own_tenant points at WF_OWN_TENANT --
# or the step is another tenant's and answers 404.
WF_ANSWER_STATUS=""
_wf_post_answer() {
  local wid="$1" step_id="$2" answer="$3" attachment="$4" out="$5" code
  WF_ANSWER_STATUS="000"
  if ! code="$(auth_config "$(api_credential)" | curl -sS -m 120 -K - -o "${out}" -w '%{http_code}' \
      -H "X-Swarm-Tenant: ${SWARM_API_TENANT}" \
      -F "answer=<${answer};type=application/json" \
      -F "file=@${attachment};filename=${WF_ATTACHMENT_NAME};type=text/markdown" \
      "$(api_url)${API_PREFIX}/workflows/${wid}/steps/${step_id}/input")"; then
    return 1
  fi
  WF_ANSWER_STATUS="${code}"
  [[ "${code}" == "200" ]]
}

# _wf_check_await WORKFLOW_JSON: #1436, live (docs/design/human-steps.md §6,
# row HW-ACCEPT). draft -> review -> revise, with `mock` for draft and revise:
#
#   * while `review` waits it is PARKED(AWAITING_INPUT) with zero leases --
#     none in its events, none in the leases collection -- and no attempt
#     (invariant 1: a wait costs nothing);
#   * the answer, a verdict form and one text file posted to the step's
#     route, ends `review` SUCCEEDED, and `revise` is leased within one drain
#     of it (WF_DRAIN_SECONDS) and runs;
#   * `revise`'s workspace holds review.json and review.files/<the file>,
#     each with the sha256 the answer recorded, and the file's equal to the
#     bytes sent.
#
# THE ANSWERING IDENTITY is this suite's own caller, swarm-verify, in its OWN
# service tenant (owner decision 2026-10-19, HW-ACCEPT-ID; see WF_OWN_TENANT
# above): answered as that tenant's principal, `answered_as: service`, which
# is the path oracle-api takes. In the group tenant ACC_TENANT the same answer
# is 403 `person_required`, deliberately: no exception to it was made there.
_wf_check_await() {
  local wf="$1" wid draft review revise state doc window leases docs attempts
  local answer_id answer attachment out reason answered_at sent recorded form_sha
  local events lease_at running_at delay content
  acc_check "workflow: await-input: a step waits for a member's answer holding no lease, and revise runs within one drain holding the posted form and file"
  [[ -n "${wf}" ]] || { acc_fail "not submitted${WF_OWN_PROBLEM:+: ${WF_OWN_PROBLEM}}"; return 0; }
  wid="$(jq -r '.workflow.workflow_id' <<<"${wf}")"
  draft="$(workflow_step_task "${wf}" draft)"
  review="$(workflow_step_task "${wf}" review)"
  revise="$(workflow_step_task "${wf}" revise)"
  acc_run_to_end state "${draft}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "draft, the step the wait depends on" "${draft}"

  # The park. draft's task_finished promotes review, and the scheduler parks
  # an await-input step in the same call that makes it READY; three drains
  # is room for a lost wake, not a tolerance on the drain.
  if ! wait_until "review PARKED(AWAITING_INPUT)" $(( 3 * WF_DRAIN_SECONDS )) _wf_awaiting "${review}"; then
    acc_fail "review is $(task_field "${review}" '"\(.state // "MISSING") \(.park_reason // "none")"'), not PARKED AWAITING_INPUT, $(( 3 * WF_DRAIN_SECONDS )) s after draft succeeded" "${review}"
    return 0
  fi
  if ! doc="$(task_doc "${review}")"; then
    acc_fail "could not read the waiting step" "${review}"
    return 0
  fi
  window="$(jq -r "${WF_JQ_EPOCH}"' .metadata.await_input // {} | ((.expires_at | epoch) // 0) - ((.opened_at | epoch) // 0)' <<<"${doc}")"
  acc_assert_eq "${WF_AWAIT_EXPIRES_SECONDS}" "${window}" "the wait's window, metadata.await_input expires_at - opened_at, in seconds" "${review}"

  # Zero leases while parked, read three ways: the task's events, the leases
  # collection, and the attempt count admission increments with each lease.
  if ! leases="$(task_lease_ids "${review}")" || ! docs="$(_wf_lease_docs "${review}")"; then
    acc_fail "could not read the waiting step's leases" "${review}"
    return 0
  fi
  attempts="$(jq -r '.attempt_count // 0' <<<"${doc}")"
  acc_assert_eq "0 0 0" "$(printf '%s' "${leases}" | awk 'NF' | wc -l | tr -d ' ') ${docs} ${attempts}" \
    "while it waits: lease ids in its events, lease documents naming it, attempts" "${review}"

  # The answer: a verdict form and one text file, multipart.
  answer_id="$(printf 'acc-%s' "${ACC_RUN_ID}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-64)"
  answer="${ACC_WORK}/wf-answer.json"
  attachment="${ACC_WORK}/wf-attachment.md"
  out="${ACC_WORK}/wf-answer-response.json"
  jq -nc --arg id "${answer_id}" --arg f "acceptance ${ACC_RUN_ID}: tighten the second paragraph" \
    '{answer_id: $id, payload: {verdict: "NOT_YET", findings: [$f]}}' >"${answer}"
  printf 'notes attached to the review\nrun %s\n' "${ACC_RUN_ID}" >"${attachment}"
  if ! _wf_post_answer "${wid}" review "${answer}" "${attachment}" "${out}"; then
    reason="$(jq -r 'if type == "object" then (.detail.reason // .code // "") else "" end' "${out}" 2>/dev/null || true)"
    if [[ "${WF_ANSWER_STATUS}" == "403" && "${reason}" == "person_required" ]]; then
      acc_fail "swarm-api refused the answer, 403 person_required, in tenant ${SWARM_API_TENANT}: it did not take this caller as that service tenant's own principal (awaitinput.py service_principal; the tenant document must be kind user with exactly this caller's email as principal)" "${review}"
    else
      acc_fail "swarm-api did not take the answer: HTTP ${WF_ANSWER_STATUS} ${reason:-}" "${review}"
      [[ ! -s "${out}" ]] || _api_error_body "${out}"
    fi
    return 0
  fi
  sent="$(sha256sum "${attachment}" | awk '{print $1}')"
  recorded="$(jq -r '"\(.state // "none") \([.files[]? | .sha256] | join(","))"' "${out}")"
  acc_assert_eq "SUCCEEDED ${sent}" "${recorded}" "the answer's response: the step's state and the digest recorded for the one file" "${review}"
  form_sha="$(jq -r '.artifact.sha256 // empty' "${out}")"
  answered_at="$(jq -r '.answered_at // empty' "${out}")"
  acc_assert_eq "person service" "$(task_field "${review}" '"\(.result_summary.input.source // "none") \(.result_summary.input.answered_as // "none")"')" \
    "result_summary.input source and answered_as of the answered step: answered by the service tenant's own principal" "${review}"

  # revise: leased within one drain of the answer, and running.
  acc_run_to_end state "${revise}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "revise" "${revise}"
  events="${ACC_WORK}/wf-await-events.jsonl"
  if ! acc_events "${revise}" >"${events}"; then
    acc_fail "could not read revise's events" "${revise}"
    return 0
  fi
  lease_at="$(jq -sr "${WF_JQ_EPOCH}"' [.[] | select(.type == "lease_acquired") | (.at | epoch) | select(. != null)] | min // empty' "${events}")"
  running_at="$(jq -sr "${WF_JQ_EPOCH}"' [.[] | select(.type == "running") | (.at | epoch) | select(. != null)] | min // empty' "${events}")"
  answered_at="$(jq -rn --arg a "${answered_at}" "${WF_JQ_EPOCH}"' $a | epoch // empty')"
  if [[ -z "${answered_at}" || -z "${lease_at}" ]]; then
    acc_fail "cannot time the resume: answered_at '${answered_at:-none}', revise's first lease_acquired '${lease_at:-none}'" "${revise}"
  else
    delay=$(( ${lease_at%.*} - ${answered_at%.*} ))
    if [[ "${delay}" -le "${WF_DRAIN_SECONDS}" ]]; then
      acc_pass "revise was leased ${delay} s after the answer, within one drain (${WF_DRAIN_SECONDS} s)" "${revise}"
    else
      acc_fail "revise was leased ${delay} s after the answer, more than one drain (${WF_DRAIN_SECONDS} s)" "${revise}"
    fi
  fi
  # RUNNING is held to the same drain, as the brief states it: the lease
  # bound says the scheduler admitted revise on the answer's own wake, and
  # this one that the job it dispatched started inside that drain too.
  if [[ -z "${running_at}" ]]; then
    acc_fail "revise has no running event: it never reached RUNNING" "${revise}"
  elif [[ -z "${answered_at}" ]]; then
    acc_fail "cannot time revise's RUNNING: the answer recorded no answered_at" "${revise}"
  else
    delay=$(( ${running_at%.*} - ${answered_at%.*} ))
    if [[ "${delay}" -le "${WF_DRAIN_SECONDS}" ]]; then
      acc_pass "revise was RUNNING ${delay} s after the answer, within one drain (${WF_DRAIN_SECONDS} s)" "${revise}"
    else
      acc_fail "revise was RUNNING ${delay} s after the answer, more than one drain (${WF_DRAIN_SECONDS} s)" "${revise}"
    fi
  fi

  # The wait never took a lease, answered as well as parked.
  if leases="$(task_lease_ids "${review}")"; then
    acc_assert_eq "none" "${leases:-none}" "lease ids in the answered step's events" "${review}"
  else
    acc_fail "could not read the answered step's events" "${review}"
  fi

  # revise's workspace: the form and the file, as the answer recorded them.
  content="${ACC_WORK}/wf-await-form.json"
  if _wf_workspace_file "${revise}" "${WF_REVIEW_NAME}" "${content}"; then
    acc_assert_eq "${form_sha:-none recorded}" "$(sha256sum "${content}" | awk '{print $1}')" \
      "sha256 of ${WF_MEMBER} in revise's workspace, against the answer's recorded artifact digest" "${revise}"
    acc_assert_eq "$(jq -cS '.payload' "${answer}")" "$(jq -cS '.' "${content}" 2>/dev/null || printf 'not JSON')" \
      "${WF_REVIEW_NAME} in revise's workspace, against the form posted" "${revise}"
  fi
  content="${ACC_WORK}/wf-await-file.md"
  if _wf_workspace_file "${revise}" "${WF_REVIEW_FILES_DIR}/${WF_ATTACHMENT_NAME}" "${content}"; then
    acc_assert_eq "${sent}" "$(sha256sum "${content}" | awk '{print $1}')" \
      "sha256 of ${WF_MEMBER} in revise's workspace, against the file posted" "${revise}"
  fi
  acc_assert_eq "SUCCEEDED" "$(_wf_final_state "${wid}")" "the workflow's derived state" "${wid}"
}

# _wf_check_expire WORKFLOW_JSON DEFAULT_JSON: the wait nobody answers. `ask`
# declares expires_after_seconds 300 and on_expire "default"; the schedule
# tick (every minute) ends it after expires_at with the signed default as its
# artifact (swarm_api/awaitinput.py `_default`), and the two steps gated on it
# take the branch that default names: `revise` (NOT_YET) runs its agent,
# `ship` (MERGE) does not, and both SUCCEED, because `when` gates the agent and
# not the step (docs/workflows.md).
#
# acc_run_to_end's ACC_TIMEOUT (900 s) covers the 300 s, a tick, and two mock
# steps; the clock started when run_workflow submitted it, before the
# integrate chain.
_wf_check_expire() {
  local wf="$1" default="$2" wid ask revise ship state doc times ended leases stored
  acc_check "workflow: await-input expiry: a step nobody answers ends at expires_after with its declared default, and the when-gated steps take that branch"
  [[ -n "${wf}" ]] || { acc_fail "not submitted${WF_OWN_PROBLEM:+: ${WF_OWN_PROBLEM}}"; return 0; }
  wid="$(jq -r '.workflow.workflow_id' <<<"${wf}")"
  ask="$(workflow_step_task "${wf}" ask)"
  revise="$(workflow_step_task "${wf}" revise)"
  ship="$(workflow_step_task "${wf}" ship)"
  acc_run_to_end state "${ask}" || return 0
  acc_assert_eq "SUCCEEDED" "${state}" "the expired step, declared on_expire default" "${ask}"
  if ! doc="$(task_doc "${ask}")"; then
    acc_fail "could not read the expired step" "${ask}"
    return 0
  fi
  acc_assert_eq "default none" "$(jq -r '"\(.result_summary.input.source // "missing") \(.result_summary.input.answered_by // "none")"' <<<"${doc}")" \
    "result_summary.input source and answered_by" "${ask}"

  # It waited its window: expires_at is opened_at + 300, and it ended no
  # earlier than expires_at, by the step's own succeeded event.
  times="$(jq -r "${WF_JQ_EPOCH}"' .metadata.await_input // {} | "\((.opened_at | epoch) // 0) \((.expires_at | epoch) // 0)"' <<<"${doc}")"
  # A missing or unreadable block reads as "0 0": say that, rather than let
  # the window report it as a wrong window of 0 s. The checks after the
  # timing still run; they do not read it.
  if [[ "${times% *}" == "0" || "${times#* }" == "0" ]]; then
    acc_fail "metadata.await_input has no readable opened_at and expires_at, so the window cannot be timed: $(jq -c '.metadata.await_input // "missing"' <<<"${doc}")" "${ask}"
  else
    acc_assert_eq "${WF_EXPIRE_SECONDS}" "$(( ${times#* } - ${times% *} ))" "the wait's window, expires_at - opened_at, in seconds" "${ask}"
    ended="$(acc_events "${ask}" | jq -sr "${WF_JQ_EPOCH}"' [.[] | select(.type == "succeeded") | (.at | epoch) | select(. != null)] | min // empty')"
    if [[ -n "${ended}" && "${ended%.*}" -ge "${times#* }" ]]; then
      acc_pass "ended $(( ${ended%.*} - ${times% *} )) s after the park, not before expires_at" "${ask}"
    else
      acc_fail "its succeeded event (${ended:-none}) is before expires_at (${times#* }): the default landed before the wait ran out" "${ask}"
    fi
  fi
  if leases="$(task_lease_ids "${ask}")"; then
    acc_assert_eq "none" "${leases:-none}" "lease ids in the expired step's events" "${ask}"
  else
    acc_fail "could not read the expired step's events" "${ask}"
  fi

  # The artifact is the declared default.
  stored="$(acc_artifact_text "${ask}" "verdict.json" 2>/dev/null | jq -cS '.' 2>/dev/null || true)"
  acc_assert_eq "$(jq -cS '.' <<<"${default}")" "${stored:-none}" "verdict.json, against the declared default" "${ask}"

  # The branch: revise ran its agent on NOT_YET; ship, gated on MERGE, did not.
  acc_run_to_end state "${revise}" || return 0
  acc_assert_eq "SUCCEEDED NOT_YET true" "${state} $(task_field "${revise}" '"\(.result_summary.verdict_gate.verdict // "none") \(.result_summary.verdict_gate.agent_ran | tostring)"')" \
    "revise, gated on NOT_YET: state, the verdict it read, whether its agent ran" "${revise}"
  acc_run_to_end state "${ship}" || return 0
  acc_assert_eq "SUCCEEDED NOT_YET false" "${state} $(task_field "${ship}" '"\(.result_summary.verdict_gate.verdict // "none") \(.result_summary.verdict_gate.agent_ran | tostring)"')" \
    "ship, gated on MERGE: state, the verdict it read, whether its agent ran" "${ship}"
  acc_assert_eq "SUCCEEDED" "$(_wf_final_state "${wid}")" "the workflow's derived state" "${wid}"
}
