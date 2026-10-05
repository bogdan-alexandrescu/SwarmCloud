#!/usr/bin/env bash
# The acceptance suite's platform helpers: submit, wait, fetch an output.
#
# Sourced by scripts/acceptance/run.sh AFTER scripts/lib/common.sh and
# scripts/lib/testlib.sh, and built on them: submission is testlib's
# submit_task, state is testlib's task_doc / wait_for_state, leases are
# testlib's task_lease_ids / lease_released_at, and every API call goes through
# common.sh's credential and address resolution. What is here is only what the
# acceptance suite needs and the other suites do not: a PASS/FAIL line that
# names its check and task, a wait that notices a task the platform will never
# run, raw artifact bytes, and GitHub reads and cleanup.
#
# THE ONE RULE: an assertion reads an OUTPUT. The other suites prove a task
# reached SUCCEEDED. That is how every browser task that screenshotted
# about:blank was reported as a success (2026-09-29). Here SUCCEEDED is a
# precondition, never the verdict.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=parsers.sh
source "${REPO_ROOT}/scripts/acceptance/parsers.sh"

ACC_DIR="${REPO_ROOT}/scripts/acceptance"

# WHERE the suite works -- ACC_TENANT, ACC_REPOSITORY_URL, ACC_REF,
# ACC_GITHUB_REPO, ACC_GITHUB_API, ACC_ISSUE -- is config.sh's, stated once
# for this file, github-cleanup.sh and sandbox-sync.sh. A target it refuses
# stops the suite here, before anything is submitted.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=config.sh
source "${REPO_ROOT}/scripts/acceptance/config.sh"
acc_config_problem || exit 1

#: Every API call this run makes carries `X-Swarm-Tenant: ${ACC_TENANT}`:
#: common.sh's api_request sends the header whenever this is set. Exported, so
#: a helper run in a child process selects the same tenant.
export SWARM_API_TENANT="${ACC_TENANT}"

#: How long one task may take, end to end. Cloud Run Jobs start in about a
#: minute; the slowest mock check sleeps 150 s; a claude-code task on a tiny
#: prompt takes a few minutes.
ACC_TIMEOUT="${SWARM_ACCEPTANCE_TIMEOUT:-900}"
#: How long a task may wait for admission before the suite concludes the
#: platform is holding it on purpose (a closed pool, a missing credential) and
#: skips instead of burning its whole timeout.
ACC_ADMIT_WAIT="${SWARM_ACCEPTANCE_ADMIT_WAIT:-300}"

ACC_CHECK=""
ACC_TASKS=()
ACC_WORK=""

acc_init() {
  ACC_RUN_ID="$(test_run_id)"
  ACC_WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-acceptance.XXXXXX")"
}

acc_cleanup() {
  cancel_all ${ACC_TASKS[@]+"${ACC_TASKS[@]}"}
  [[ -z "${ACC_WORK}" ]] || rm -rf "${ACC_WORK}"
}

# ---------------------------------------------------------------------------
# The target: checked against the platform and GitHub before anything is
# submitted (#628). Each dies rather than reporting a FAIL: a suite run in the
# wrong tenant or on a public repository has already done the damage the
# check exists to stop, whatever it then reports.
# ---------------------------------------------------------------------------

# acc_require_tenant -> returns when the API resolves this caller, with
# X-Swarm-Tenant: ACC_TENANT, to ACC_TENANT; dies otherwise.
#
# A refusal is a 403 `tenant_not_member`: the caller is not a CONFIRMED member
# of the tenant's registered directory group. Never retried without the
# header -- that is how acceptance came to fill eng.
acc_require_tenant() {
  local out got
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-acc-me.XXXXXX")"
  if ! api_get "/tenants/me" >"${out}"; then
    got="$(acc_error_code <"${out}" 2>/dev/null || true)"
    rm -f "${out}"
    die "GET /tenants/me with X-Swarm-Tenant: ${ACC_TENANT} answered HTTP ${API_STATUS} ${got:-}: this caller cannot select tenant '${ACC_TENANT}'. It must be a member of ${ACC_TENANT}'s directory group and the group registered in dev.tfvars (docs/ci.md, \"Release acceptance runs in the smoke tenant\"). Not running in the caller's default tenant instead."
  fi
  got="$(jq -r '.tenant.tenant_id // empty' <"${out}" 2>/dev/null || true)"
  rm -f "${out}"
  [[ "${got}" == "${ACC_TENANT}" ]] \
    || die "the API resolved this caller to tenant '${got:-none}', not '${ACC_TENANT}': refusing to run acceptance there"
  info "tenant ${got} (X-Swarm-Tenant)"
}

# acc_require_private_repository -> returns when an ANONYMOUS read of
# ACC_GITHUB_REPO is a 404; dies when anybody can read it, or when GitHub's
# answer cannot be had.
#
# Anonymous on purpose: a token reads a private repository too. A 404 is
# private or absent, and the clone tells those apart. A 200 is public, which
# the sandbox is not -- and every public repository is one whose readers a
# fixture pull request reaches. Anything else is asked once more, then is a
# refusal: "could not tell" is not "private".
acc_require_private_repository() {
  local code attempt
  for attempt in 1 2; do
    code="$(curl -sS -m 30 -o /dev/null -w '%{http_code}' \
      -H "Accept: application/vnd.github+json" \
      "${ACC_GITHUB_API}/repos/${ACC_GITHUB_REPO}" 2>/dev/null)" || code="000"
    case "${code}" in
      404) info "repository ${ACC_GITHUB_REPO} is not publicly readable"; return 0 ;;
      200) die "${ACC_GITHUB_REPO} is publicly readable: acceptance opens real pull requests and runs only against a private sandbox (#628)" ;;
    esac
    [[ "${attempt}" -eq 2 ]] || sleep 5
  done
  # 403 and 429 are what GitHub answers an anonymous caller over its 60
  # requests an hour, counted per source IP -- the swarm-verify job's NAT
  # address. Not a platform failure, and not a public repository: say so.
  case "${code}" in
    403|429) die "could not confirm ${ACC_GITHUB_REPO} is private: GitHub answered HTTP ${code} twice to an anonymous read, which is its unauthenticated rate limit (60 an hour per source IP), not an answer about the repository; re-run the job once the hour has passed" ;;
  esac
  die "could not confirm ${ACC_GITHUB_REPO} is private: GitHub answered HTTP ${code} twice to an anonymous read"
}

# ---------------------------------------------------------------------------
# Reporting: one line per verdict, naming the check, the reason and the task.
# ---------------------------------------------------------------------------

acc_check() {
  ACC_CHECK="$1"
  t_case "$1"
}

_acc_line() {
  local reason="$1" task="${2:-}"
  printf '%s: %s%s' "${ACC_CHECK}" "${reason}" "${task:+ [${task}]}"
}

acc_pass() { t_pass "$(_acc_line "$@")"; }
acc_fail() { t_fail "$(_acc_line "$@")"; }
# A skip must say WHY, and is counted apart from passes (testlib's t_skip).
acc_skip() { t_skip "$(_acc_line "$@")"; }

# acc_assert_eq WANT GOT WHAT [TASK]
#
# TASK is optional -- a check made "at the door" (a submission refused before
# a task ever existed) has none to name. `"${4:-}"` is required, not
# decoration: a caller that omits it leaves $4 unset, and under this file's
# `set -u` referencing "$4" directly is a fatal "unbound variable" that kills
# the whole suite mid-run (PR #358's first live run, at the door checks in
# mock.sh, browser.sh and generic.sh that pass only WANT GOT WHAT).
acc_assert_eq() {
  if [[ "$1" == "$2" ]]; then
    acc_pass "$3: $2" "${4:-}"
  else
    acc_fail "$3: expected '$1', got '$2'" "${4:-}"
  fi
}

# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

# acc_metadata [EXTRA_JSON] -> the metadata every submission carries, so a
# human reading Firestore can tell acceptance traffic from real work.
acc_metadata() {
  local empty='{}'
  jq -nc --arg r "${ACC_RUN_ID}" --arg c "${ACC_CHECK}" --argjson x "${1:-$empty}" \
    '{source: "acceptance", acceptance_run: $r, check: $c} + $x'
}

# acc_extra [EXTRA_JSON] -> submit_task's EXTRA: priority and metadata, plus
# whatever the check adds (a repository, a strategy, max_attempts).
acc_extra() {
  local empty='{}'
  jq -nc --argjson m "$(acc_metadata)" --argjson x "${1:-$empty}" '{priority: 10, metadata: $m} + $x'
}

# acc_repo_extra [EXTRA_JSON] -> acc_extra plus the sandbox (ACC_REPOSITORY_URL) at ACC_REF.
acc_repo_extra() {
  local empty='{}'
  acc_extra "$(jq -nc --arg u "${ACC_REPOSITORY_URL}" --arg r "${ACC_REF}" --argjson x "${1:-$empty}" \
    '{repository_url: $u, repository_ref: $r} + $x')"
}

# acc_submit VAR PROFILE INPUT_JSON EXTRA_JSON
#
# Sets VAR to the new task id and records the task for cleanup. Through
# testlib's submit_task, redirected to a file rather than captured with $(),
# so the id lands in THIS shell's ACC_TASKS (the command-substitution trap
# CLAUDE.md names). On a refused submission it records a FAIL and returns 1.
#
# The locals are prefixed `__acc_` because VAR is assigned by name with
# printf -v, which writes the NEAREST variable of that name -- a local here
# called `id` or `state` would swallow a caller's `acc_submit id ...`.
acc_submit() {
  local __acc_var="$1" __acc_out __acc_id
  shift
  __acc_out="${ACC_WORK}/submit.$$"
  if ! submit_task "$@" >"${__acc_out}"; then
    rm -f "${__acc_out}"
    acc_fail "the submission was refused (HTTP ${API_STATUS:-?}); see the response above"
    return 1
  fi
  __acc_id="$(cat "${__acc_out}")"
  rm -f "${__acc_out}"
  ACC_TASKS+=("${__acc_id}")
  t_info "task ${__acc_id}"
  printf -v "${__acc_var}" '%s' "${__acc_id}"
}

# acc_workflow VAR BODY_JSON -> VAR holds the creation response; every step's
# task is recorded for cleanup.
acc_workflow() {
  local __acc_var="$1" __acc_body="$2" __acc_out __acc_response __acc_id
  __acc_out="${ACC_WORK}/workflow.$$"
  if ! submit_workflow "${__acc_body}" >"${__acc_out}"; then
    rm -f "${__acc_out}"
    acc_fail "the workflow submission was refused (HTTP ${API_STATUS:-?}); see the response above"
    return 1
  fi
  __acc_response="$(cat "${__acc_out}")"
  rm -f "${__acc_out}"
  while IFS= read -r __acc_id; do
    if [[ -n "${__acc_id}" ]]; then ACC_TASKS+=("${__acc_id}"); fi
  done < <(jq -r '.workflow.steps[].task_id // empty' <<<"${__acc_response}")
  t_info "workflow $(jq -r '.workflow.workflow_id' <<<"${__acc_response}")"
  printf -v "${__acc_var}" '%s' "${__acc_response}"
}

# acc_door PROFILE INPUT_JSON -> the refusal's "STATUS CODE", e.g. "422 invalid_input".
#
# A door check must never CREATE a task when the door fails to refuse. So
# every probe also carries `metadata.expected_outputs`, a key only the service
# may write: swarm-api checks a runner's input (validate_runner_input) BEFORE
# it checks the reserved metadata keys (reject_reserved_metadata, both in
# SubmissionService._build_task). A door that refuses the input answers
# `invalid_input`; a door that lets the input through is still refused, for
# the metadata, with `invalid_dispatch` -- and nothing is created either way.
# The caller asserts `invalid_input`, so a door that let the input through is
# a FAIL, never a task.
acc_door() {
  local profile="$1" input="$2" body out code
  body="$(jq -nc --arg p "${profile}" --argjson i "${input}" --argjson m "$(acc_metadata '{"expected_outputs":["acceptance-door-probe.txt"]}')" \
    '{runner_profile: $p, input: $i, metadata: $m}')"
  out="${ACC_WORK}/door.$$"
  if api_post "/tasks" "${body}" >"${out}"; then
    # A 2xx here means a task WAS created despite the reserved key. Cancelled
    # at once, HERE: this function runs inside $(...), so recording it in
    # ACC_TASKS for the EXIT trap would be lost with the subshell.
    local id
    id="$(jq -r '.id // .task_id // .task.id // empty' <"${out}")"
    [[ -z "${id}" ]] || cancel_all "${id}"
    rm -f "${out}"
    printf '%s created:%s' "${API_STATUS}" "${id:-?}"
    return 0
  fi
  code="$(acc_error_code <"${out}")"
  rm -f "${out}"
  printf '%s %s' "${API_STATUS}" "${code:-none}"
}

# acc_declares PROFILE PROBE_INPUT -> 0 when the deployed API validates
# PROBE_INPUT against a declaration for PROFILE, 1 when it does not look.
#
# The same zero-side-effect probe as acc_door: PROBE_INPUT is one the
# declaration refuses, sent with a reserved metadata key. `invalid_input`
# means the input was checked and refused; `invalid_dispatch` means it was let
# through to the metadata check, i.e. nothing is declared for that key yet.
# Anything else (auth, a 5xx) is neither, and returns 2.
acc_declares() {
  local answer
  answer="$(acc_door "$1" "$2")"
  case "${answer}" in
    "422 invalid_input") return 0 ;;
    "422 invalid_dispatch"|"400 invalid_dispatch") return 1 ;;
    *) t_info "declaration probe for $1 answered '${answer}'"; return 2 ;;
  esac
}

# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------

# acc_settle TASK TIMEOUT -> prints the state it settled in.
#
# Terminal states end the wait, as in testlib's wait_for_state. Two more do,
# because the platform will not run the task however long the suite waits:
#
#   * PARKED on CREDENTIAL_MISSING -- the caller's tenant has no credential for
#     the profile's provider (printed as PARKED:CREDENTIAL_MISSING);
#   * still not admitted after ACC_ADMIT_WAIT, and held by a pool that is
#     CLOSED -- paused, or at limit 0 -- or parked on a manual pause or an
#     exhausted budget (printed as HELD:<state>:<why>). A pool that is merely
#     full is waited on for the whole timeout.
#
# The caller turns either into a SKIP naming the reason. A PARKED task that
# HAS held a lease (a quota park) is not "held": it ran, and the wait goes on.
acc_settle() {
  local task="$1" timeout="$2" started deadline doc state reason
  started="$(date -u +%s)"
  deadline=$(( started + timeout ))
  while :; do
    if ! doc="$(task_doc "${task}")"; then
      die "could not read task ${task}; see the Firestore error above -- a failed read is not a result"
    fi
    state="$(jq -r '.state // "MISSING"' <<<"${doc}")"
    case "${state}" in
      SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED)
        printf '%s' "${state}"; return 0 ;;
      PARKED)
        reason="$(jq -r '.park_reason // ""' <<<"${doc}")"
        if [[ "${reason}" == "CREDENTIAL_MISSING" ]]; then
          printf 'PARKED:CREDENTIAL_MISSING'; return 0
        fi
        ;;
    esac
    case "${state}" in
      QUEUED|READY|PARKED)
        if [[ $(( $(date -u +%s) - started )) -ge "${ACC_ADMIT_WAIT}" ]] \
            && [[ "$(jq -r '.attempt_count // 0' <<<"${doc}")" == "0" ]]; then
          # CLOSED, not merely full. A pool at its limit because this suite's
          # own tasks fill it will open, and waiting is right; a pool an
          # operator paused (MANUAL_PAUSE) or set to 0 will not. The
          # scheduler's own blockers say which (swarm_common.admission writes
          # {pool, reason, limit, active}), so nothing is re-derived here.
          reason="$(jq -r '
              [ (.blocked_by // [])[]
                | select(.reason == "MANUAL_PAUSE" or .limit == 0)
                | "\(.pool // "?") \(.reason // "?") limit \(.limit // "?")" ]
              + (if (.park_reason // "") == "MANUAL_PAUSE" or (.park_reason // "") == "BUDGET_EXHAUSTED"
                 then [.park_reason] else [] end)
              | join(", ")' <<<"${doc}")"
          if [[ -n "${reason}" ]]; then
            printf 'HELD:%s:%s' "${state}" "${reason}"; return 0
          fi
        fi
        ;;
    esac
    if [[ "$(date -u +%s)" -ge "${deadline}" ]]; then
      printf '%s' "${state}"; return 1
    fi
    sleep "${SWARM_POLL_INTERVAL_SECONDS:-3}"
  done
}

# acc_settled_or_skip TASK STATE -> 0 when STATE is a state the task RAN to;
# otherwise records a SKIP (the platform holding it on purpose) or a FAIL (the
# wait timed out) and returns 1.
acc_settled_or_skip() {
  local task="$1" state="$2"
  case "${state}" in
    SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED) return 0 ;;
    PARKED:CREDENTIAL_MISSING)
      acc_skip "not measured: the caller's tenant holds no credential for this profile's provider (PARKED on CREDENTIAL_MISSING)" "${task}" ;;
    HELD:*)
      acc_skip "not measured: admission held the task for ${ACC_ADMIT_WAIT}s without leasing it (${state#HELD:}) -- a closed pool or a limit, not a result" "${task}" ;;
    *)
      acc_fail "did not finish within ${ACC_TIMEOUT}s (last state ${state})" "${task}" ;;
  esac
  cancel_all "${task}"
  return 1
}

# acc_run_to_end VAR TASK -> VAR holds the settled state; returns 1 (having
# recorded the SKIP or FAIL) unless the task ran to a terminal state.
acc_run_to_end() {
  local __acc_var="$1" __acc_task="$2" __acc_state
  __acc_state="$(acc_settle "${__acc_task}" "${ACC_TIMEOUT}")" || true
  printf -v "${__acc_var}" '%s' "${__acc_state}"
  acc_settled_or_skip "${__acc_task}" "${__acc_state}"
}

# acc_leases_released TASK -> 0 when every lease the task's events name is
# released; prints "N lease(s) released" or the first held one. Waits up to 60 s,
# because the worker writes the terminal state before it releases the lease.
acc_leases_released() {
  local task="$1" ids id at deadline held
  ids="$(task_lease_ids "${task}")" || { printf 'its events could not be read'; return 1; }
  deadline=$(( $(date -u +%s) + 60 ))
  while :; do
    held=""
    while IFS= read -r id; do
      [[ -n "${id}" ]] || continue
      at="$(lease_released_at "${id}")" || { printf 'lease %s could not be read' "${id}"; return 1; }
      [[ "${at}" != "null" && "${at}" != "missing" ]] || { held="${id} (${at})"; break; }
    done <<<"${ids}"
    [[ -n "${held}" ]] || break
    [[ "$(date -u +%s)" -lt "${deadline}" ]] || { printf 'lease %s still holds capacity' "${held}"; return 1; }
    sleep 3
  done
  printf '%s lease(s) released' "$(printf '%s' "${ids}" | awk 'NF' | wc -l | tr -d ' ')"
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

# acc_raw_artifact TASK NAME OUTFILE -> the artifact's stored BYTES in OUTFILE.
#
# Not api_get: api_request holds the body in a shell variable, which drops NUL
# bytes and the trailing newline -- so "the downloaded bytes match exactly"
# could never be true of a PNG, and could be false of a text file for a reason
# that is the suite's own. curl writes straight to the file, the header on
# stdin as common.sh's auth_config does everywhere -- and X-Swarm-Tenant, as
# api_request sends it, or the task is another tenant's and answers 404.
acc_raw_artifact() {
  local task="$1" name="$2" out="$3" q code
  q="$(jq -rn --arg n "${name}" '$n | @uri')"
  if ! code="$(auth_config "$(api_credential)" | curl -sS -m 120 -K - -o "${out}" -w '%{http_code}' \
      -H "X-Swarm-Tenant: ${ACC_TENANT}" \
      "$(api_url)${API_PREFIX}/tasks/${task}/artifacts/raw?name=${q}&disposition=attachment")"; then
    return 1
  fi
  ACC_HTTP="${code}"
  [[ "${code}" == "200" ]]
}

# acc_artifact_text TASK NAME -> the artifact's content as text, or fails.
acc_artifact_text() {
  local file="${ACC_WORK}/artifact.$$"
  acc_raw_artifact "$1" "$2" "${file}" || { rm -f "${file}"; return 1; }
  cat "${file}"
  rm -f "${file}"
}

# acc_output TASK JQ -> a field of the runner's own OUTPUT
# (result_summary.runner.output), the free-form dict the runner returned.
#
# `summary` and `metrics` are NOT inside it, though every runner's own return
# value has them alongside its output fields: the shared harness
# (run_runner, apps/agent-worker/agent_worker/runners/base.py) pops both keys
# off that dict before it becomes result.json, so lifecycle._finalise reads
# them back at result_summary.runner.summary and result_summary.runner.metrics
# -- siblings of .output, one level up. Use acc_runner_field for either (found
# wrong in PR #358's first live run: mock.sh read the summary at
# .result_summary.runner.output.summary, which the worker never writes).
acc_output() { task_field "$1" ".result_summary.runner.output | $2"; }

# acc_runner_field TASK JQ -> a field of the runner's own RECORD
# (result_summary.runner), the level `status`, `summary`, `output`, `usage`
# and `metrics` all sit at. See acc_output's note above.
acc_runner_field() { task_field "$1" ".result_summary.runner | $2"; }

acc_events() { task_events "$1"; }

# ---------------------------------------------------------------------------
# GitHub: read a pull request back, and clean up after one.
# ---------------------------------------------------------------------------

#: A token that can read and write the sandbox, when this run has one. The
#: swarm-verify job has none, on purpose: its identity holds no secret, and a
#: forge token lives only in Secret Manager for the worker (CLAUDE.md). The
#: release's acceptance job sweeps afterwards with the sandbox's own token
#: (scripts/acceptance/github-cleanup.sh, secret SWARM_SANDBOX_GITHUB_TOKEN).
ACC_GITHUB_TOKEN="${SWARM_ACCEPTANCE_GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"

# acc_github_can_read -> 0 when this run can read ACC_GITHUB_REPO back.
#
# The sandbox is PRIVATE (run.sh's acc_require_private_repository refuses
# anything else), so without a token every read is a 404. A check asks this
# first and SKIPs what it cannot look at, naming why -- never a FAIL for a
# read it could not make, and never a PASS for one it did not. In the release
# that is every pull-request read-back: the platform-side assertions (the
# pull request was opened, its title is the agent's, the artifacts exist) run
# here, and the release's acceptance job then makes the read-back ones on the
# GitHub runner, with the sandbox token, before anything is closed
# (scripts/acceptance/github-verify.sh). An operator run with
# SWARM_ACCEPTANCE_GITHUB_TOKEN measures them here too.
acc_github_can_read() { [[ -n "${ACC_GITHUB_TOKEN}" ]]; }

# acc_github_skip_reason -> the one sentence every such SKIP gives.
acc_github_skip_reason() {
  printf 'not measured here: %s is private and this run holds no token to read it (the swarm-verify job carries none). The release measures it after the suite with scripts/acceptance/github-verify.sh; set SWARM_ACCEPTANCE_GITHUB_TOKEN to measure it here' "${ACC_GITHUB_REPO}"
}

# acc_github METHOD PATH [BODY] -> the response body; fails on non-2xx.
acc_github() {
  local method="$1" path="$2" body="${3:-}" out code
  out="${ACC_WORK}/gh.$$"
  local args=(-sS -m 30 -o "${out}" -w '%{http_code}' -X "${method}"
    -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")
  [[ -z "${body}" ]] || args+=(-H "Content-Type: application/json" --data-binary "${body}")
  if [[ -n "${ACC_GITHUB_TOKEN}" ]]; then
    code="$(printf 'header = "Authorization: Bearer %s"\n' "${ACC_GITHUB_TOKEN}" \
      | curl -K - "${args[@]}" "${ACC_GITHUB_API}${path}")" || { rm -f "${out}"; return 1; }
  else
    code="$(curl "${args[@]}" "${ACC_GITHUB_API}${path}")" || { rm -f "${out}"; return 1; }
  fi
  cat "${out}"
  rm -f "${out}"
  [[ "${code}" =~ ^2 ]]
}

# acc_github_raw PATH_IN_REPO OUTFILE -> the file's bytes at ACC_REF, through
# the contents API with this run's token. Not raw.githubusercontent.com: it
# serves a private repository's files to nobody without one.
acc_github_raw() {
  local path="$1" out="$2" code q
  q="$(jq -rn --arg r "${ACC_REF}" '$r | @uri')"
  code="$(auth_config "${ACC_GITHUB_TOKEN}" \
    | curl -K - -sS -m 30 -o "${out}" -w '%{http_code}' \
      -H "Accept: application/vnd.github.raw" -H "X-GitHub-Api-Version: 2022-11-28" \
      "${ACC_GITHUB_API}/repos/${ACC_GITHUB_REPO}/contents/${path}?ref=${q}")" || return 1
  [[ "${code}" == "200" ]]
}

# acc_pr_files_vs_ref HEAD_BRANCH -> the files GitHub's compare API says
# differ between ACC_REF (the ref the task's repository_ref pointed the clone
# at) and HEAD_BRANCH, as a JSON array shaped like /pulls/{n}/files (filename,
# patch, ...).
#
# NOT /repos/.../pulls/{n}/files: a direct-pr or integrate task opens its pull
# request against the repository's default branch, whatever ACC_REF is, so a
# run against a non-default ACC_REF (as this suite runs against its own
# fixture branch) sees every file already on ACC_REF but still absent from
# the default branch as an apparent addition to the PR -- the whole unmerged
# branch, not the one line the task actually touched. Comparing against
# ACC_REF measures what the task changed relative to what it cloned, which
# reads the same whether or not ACC_REF has since merged to default.
acc_pr_files_vs_ref() {
  local head="$1"
  acc_github GET "/repos/${ACC_GITHUB_REPO}/compare/${ACC_REF}...${head}" | jq -c '.files // []'
}

# acc_close_pr NUMBER BRANCH... -> closes the pull request and deletes each
# branch, when this run holds a token; otherwise says what it left behind for
# the release's sweep. Never fails the check it is called from: cleanup is
# reported, not asserted.
acc_close_pr() {
  local number="$1" branch
  shift
  if [[ -z "${ACC_GITHUB_TOKEN}" ]]; then
    t_info "left for scripts/acceptance/github-cleanup.sh (no GitHub token in this run): PR #${number:-none} and branch(es) $*"
    return 0
  fi
  if [[ -n "${number}" ]]; then
    if acc_github PATCH "/repos/${ACC_GITHUB_REPO}/pulls/${number}" '{"state":"closed"}' >/dev/null; then
      t_info "closed PR #${number}"
    else
      warn "could not close PR #${number}"
    fi
  fi
  for branch in "$@"; do
    [[ -n "${branch}" ]] || continue
    if [[ ! "${branch}" =~ ^swarm/[A-Za-z0-9_.-]+$ ]]; then
      warn "not deleting '${branch}': not a swarm/ branch"
      continue
    fi
    if acc_github DELETE "/repos/${ACC_GITHUB_REPO}/git/refs/heads/${branch}" >/dev/null; then
      t_info "deleted branch ${branch}"
    else
      warn "could not delete branch ${branch}"
    fi
  done
}
