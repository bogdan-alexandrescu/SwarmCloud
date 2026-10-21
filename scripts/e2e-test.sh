#!/usr/bin/env bash
# The seams, end to end, against a deployed environment.
#
# The other suites check PROPERTIES of the platform. This one checks that its
# halves are JOINED: every defect found by running something over three days
# was a seam where both ends existed and nothing had executed the middle. Each
# of its eleven cases is written so the FAILING platform does not satisfy it:
#
#   handoff (1-5)  a two-step `mock` workflow (`--profile` to change) whose
#                  producer's artifact must be in its manifest, under its own
#                  attempt prefix and beside the runner's log streams; listed,
#                  complete and with no download URL over HTTP; and recorded
#                  as staged in the consumer's workspace with the same bytes,
#                  path and source -- not merely SUCCEEDED, which the broken
#                  platform also reached.
#   negative (6)   a producer that never wrote the file its dependant declared
#                  FAILS naming it, and the dependant never runs: 1-5 can fail.
#   spend (7)      every attempt of the producer carries the 5 spend keys, up
#                  to 25 of the calling tenant's attempts with usage serve the
#                  same values over HTTP, and another tenant's task is refused
#                  404. Present-when-present, and isolation kept (#1754).
#   sign-in (8)    POST /accounts/authorize returns an https URL with a PKCE
#                  challenge and its state, and no verifier or credential.
#   agreement (9)  Firestore and the API give the same state for this run's
#                  tasks, after one re-read.
#   capacity (10)  platform-wide, no DISPATCHED/STARTING/RUNNING task names a
#                  dead Cloud Run execution or GKE Job (by backend), or none.
#   workflow (11)  a workflow whose steps all SUCCEEDED reads SUCCEEDED over
#                  HTTP, as the API's read-time derivation should make it.
# A case with nothing to compare is NOT MEASURED; --require-spend fails those.
#
# WHAT IT CANNOT PROVE: the `mock` runner IS its own workload, so it cannot tell
# a file an agent CHILD PROCESS wrote from one the runner wrote -- the exact
# distinction the SWARM_ARTIFACTS_DIR defect turned on. That is proved offline
# in tests/unit/worker/test_agent_seam_end_to_end.py.
#
# Nothing here writes to Firestore directly. Through the API it creates two
# workflows; on exit cleanup() asks to cancel their four step tasks, unless
# --keep (cancel_all ignores failures, and a workflow is never cancelled as a
# workflow). Case 8 leaves one pending sign-in record, carrying a PKCE
# verifier, which nothing deletes: see case 8 for why.
#
# --help prints lines 2-46, so this header ends at line 46.
#
# Usage: scripts/e2e-test.sh [--profile mock] [--timeout 900] [--keep]
#                            [--require-spend]

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
# GKE_* are case 10's: the cluster read, done once, and the CA it returned.
GKE_ACCESS=""
GKE_ENDPOINT=""
GKE_CA_FILE=""
cleanup() {
  [[ -z "${GKE_CA_FILE}" ]] || rm -f "${GKE_CA_FILE}"
  [[ "${KEEP}" -eq 1 ]] && { info "--keep: leaving ${#TASK_IDS[@]} task(s) in place"; return 0; }
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_submitted ${TASK_IDS[@]+"${TASK_IDS[@]}"}
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Case 6: the negative control.
#
# check_negative_control PRODUCER CONSUMER. The producer was asked to write one
# name and its dependant declared another, so the platform records the
# declared name as the producer's expected output (metadata.expected_outputs,
# #149) and FAILS the producer when it is missing: `expected outputs missing`,
# end_cause outputs_missing, retried to max_attempts. The dependant then never
# runs -- with on_step_failure `continue` it is CANCELLED as a transitive
# dependant. acceptance's workflow group asserts the same ("a missing one fails
# it before the child runs").
#
# This used to expect the producer to SUCCEED and the consumer to fail at
# staging; on 2026-10-20 (#1754) it failed the gate for the platform doing the
# newer, stricter thing. What makes the control a control is unchanged: the
# step that was promised a file nobody wrote does not succeed, and the error
# names the file.
check_negative_control() {
  local producer="$1" consumer="$2" pstate perror cstate leases
  # The dependant is cancelled when its producer fails; three minutes is the
  # wait acceptance gives the same transition.
  local consumer_wait=180
  if pstate="$(wait_for_state "${producer}" "${TERMINAL}" "${TIMEOUT}")"; then
    assert_eq "FAILED" "${pstate}" "the negative control's producer, which never wrote the file its dependant declared"
  else
    t_fail "the negative control's producer never finished (stuck at ${pstate})"
  fi
  perror="$(task_field "${producer}" '.last_error // ""')"
  case "${perror}" in
    *"expected outputs missing"*)
      t_pass "the producer failed for its missing expected output (end_cause $(task_field "${producer}" '.end_cause // "none"'))" ;;
    *) t_fail "the producer's error is not 'expected outputs missing': ${perror:0:200}" ;;
  esac
  case "${perror}" in
    *"${MISSING_NAME}"*) t_pass "the error names the file that was missing" ;;
    *) t_fail "the error does not name ${MISSING_NAME}: ${perror:0:200}" ;;
  esac
  cstate="$(wait_for_state "${consumer}" "${TERMINAL}" "${consumer_wait}")" || true
  if ! leases="$(task_lease_ids "${consumer}")"; then
    t_fail "could not read the consumer's events, so whether it ran cannot be shown (a failed read, not evidence either way)"
  elif [[ "${cstate}" == "SUCCEEDED" ]]; then
    t_fail "a step whose promised input was never written reported SUCCEEDED; the handoff checks above prove nothing"
  elif [[ -n "${leases}" ]]; then
    t_fail "the consumer was leased ($(printf '%s' "${leases}" | tr '\n' ' ')) although the step it stages from failed: it started on an input that was never written"
  else
    t_pass "the consumer never ran: ${cstate}, and it never held a lease"
  fi
}

# ---------------------------------------------------------------------------
# Case 7: the spend scan is the CALLING tenant's.
#
# The API serves a task's attempts only to its own tenant and answers 404 to
# everyone else. The scan used to read every tenant's attempts and fail on that
# 404 -- on 2026-10-20 (#1754) for three of the smoke tenant's tasks while the
# gate ran as eng, which is isolation working. So the scan keeps the calling
# tenant's rows, and foreign_tenant_refused asserts the 404 on purpose.

# own_tenant_rows TENANT MAX -- fs_query's rows (one Firestore document a line)
# on stdin -> the first MAX of TENANT's, decoded, one a line.
own_tenant_rows() {
  jq -sc --arg t "$1" --argjson n "$2" "${FS_JQ}"' [ .[] | doc | select(.tenant_id == $t) ] | .[0:$n][]'
}

# foreign_tenant_refused TENANT -- the control: one attempt of a tenant other
# than TENANT, and its task's attempts asked of the API as TENANT. 404 passes;
# a 2xx is broken isolation; anything else is reported as what it is. No other
# tenant in the deployment is NOT MEASURED, never a pass.
foreign_tenant_refused() {
  local tenant="$1" row task other out
  if ! row="$(fs_query attempts "$(fs_field_filter tenant_id NOT_EQUAL "$(jq -nc --arg v "${tenant}" '{stringValue:$v}')")" 1 \
        | jq -c "${FS_JQ} doc")"; then
    t_fail "could not read another tenant's attempt from Firestore, so the API's refusal of it was not exercised"
    return 0
  fi
  if [[ -z "${row}" ]]; then
    t_skip "no attempt of a tenant other than ${tenant} exists, so the API's refusal of another tenant's task could not be exercised"
    return 0
  fi
  task="$(jq -r '.task_id // ""' <<<"${row}")"
  other="$(jq -r '.tenant_id // ""' <<<"${row}")"
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-foreign.XXXXXX")"
  # api_get directly, not in $(): API_STATUS has to land in this shell.
  if api_get "/tasks/${task}/attempts" >"${out}"; then
    t_fail "the API served tenant ${other}'s task ${task} to a caller acting as ${tenant} (HTTP ${API_STATUS}): tenant isolation is broken"
  elif [[ "${API_STATUS}" == "404" ]]; then
    t_pass "tenant ${other}'s task ${task} is refused to ${tenant}: HTTP 404"
  else
    t_fail "tenant ${other}'s task ${task} answered HTTP ${API_STATUS} to ${tenant}, not the 404 isolation answers with"
  fi
  rm -f "${out}"
}

# ---------------------------------------------------------------------------
# Case 10: a holder is compared with the backend its attempt names.
#
# On 2026-10-20 (#1754) this listed Cloud Run executions only and flagged ten
# eng claude-code agents running on GKE Autopilot (e.g. task_cf154a98da3f42bfae36,
# dispatched as swarm-tenant-eng/swarm-cf154a98da3f42bfae36-1). The attempt's
# `backend` says which backend to ask; a GKE attempt's execution_name is
# `NAMESPACE/JOB` (GkeJobDispatcher), and a bare name is looked up in the
# tenant's namespace.
#
# GKE IS READ OVER REST, as Cloud Run is: the verify image has no kubectl and no
# Cloud SDK. Two reads -- container.clusters.get for the endpoint and CA, then
# the Job in its namespace -- and since VERIFY-GKE-READ (owner decision
# 2026-10-20) swarm-verify holds both, narrowly:
#   * roles/container.clusterViewer, conditioned to this cluster's resource
#     name (terraform/bootstrap/verify_cluster_read.tf
#     verify_views_swarm_cluster, which the owner applies -- never CI, since
#     VERIFY-GKE-READ-2);
#   * `get` and `list` on jobs.batch in the eng and smoke namespaces
#     (kubernetes/rbac/verify-rbac.yaml, by kubernetes/apply.sh --tenant eng
#     and --tenant smoke, which resolve swarm-verify's uniqueId themselves).
# So a REFUSED cluster read is the grant missing -- not applied, or removed --
# and fails this check: a gate that skipped it would pass [10] with the grant
# gone, which is the silence this check exists to end. An UNREACHABLE cluster,
# and a refused Job read, stay "not checked": every namespace but eng's and
# smoke's -- other tenants, the workspace deployer's swarm-tenant-u-* and group
# workspaces -- is deliberately not given the Job read (owner decision
# 2026-10-21; render.py verify_read_wanted), so a 403 there is expected.

# gke_cluster_access -> sets GKE_ACCESS to "ok" (with GKE_ENDPOINT and
# GKE_CA_FILE), "refused:<why>" (the grant is missing) or "no:<why>" (not
# measured), once. Called directly, never in $().
gke_cluster_access() {
  local out status rc=0
  [[ -z "${GKE_ACCESS}" ]] || return 0
  # The other team's cluster is never read, whatever GKE_CLUSTER says.
  if is_shared_resource "${GKE_CLUSTER}"; then
    GKE_ACCESS="no:${GKE_CLUSTER} is on the shared deny-list, and this suite never reads it"
    return 0
  fi
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-gke.XXXXXX")"
  status="$(auth_config "$(access_token)" | curl -sS -m "${HTTP_TIMEOUT:-30}" -K - -o "${out}" -w '%{http_code}' \
    "https://container.googleapis.com/v1/projects/${PROJECT_ID}/locations/${GKE_LOCATION}/clusters/${GKE_CLUSTER}")" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    GKE_ACCESS="no:the GKE API could not be reached (curl exit ${rc})"
  else
    case "${status}" in
      2*)
        GKE_ENDPOINT="$(jq -r '.endpoint // empty' <"${out}")"
        GKE_CA_FILE="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-gke-ca.XXXXXX")"
        if [[ -n "${GKE_ENDPOINT}" ]] \
          && jq -r '.masterAuth.clusterCaCertificate // "" | @base64d' <"${out}" >"${GKE_CA_FILE}" \
          && [[ -s "${GKE_CA_FILE}" ]]; then
          GKE_ACCESS="ok"
        else
          GKE_ACCESS="no:cluster ${GKE_CLUSTER} answered with no endpoint or CA"
        fi
        ;;
      401|403) GKE_ACCESS="refused:container.clusters.get on ${GKE_CLUSTER} answered HTTP ${status}; swarm-verify's conditioned clusterViewer (terraform/bootstrap/verify_cluster_read.tf, applied by the owner's make bootstrap) is not in effect" ;;
      *)       GKE_ACCESS="no:cluster ${GKE_CLUSTER} could not be read (HTTP ${status})" ;;
    esac
  fi
  rm -f "${out}"
}

# gke_job_status NAMESPACE JOB -> running | finished | gone, or
# "unreadable <why>". Needs GKE_ACCESS=ok.
gke_job_status() {
  local ns="$1" name="$2" out status rc=0
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-job.XXXXXX")"
  status="$(auth_config "$(access_token)" | curl -sS -m "${HTTP_TIMEOUT:-30}" -K - --cacert "${GKE_CA_FILE}" \
    -o "${out}" -w '%{http_code}' "https://${GKE_ENDPOINT}/apis/batch/v1/namespaces/${ns}/jobs/${name}")" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    printf 'unreadable the cluster endpoint could not be reached (curl exit %s)' "${rc}"
  else
    case "${status}" in
      2*)
        jq -r 'if ([ .status.conditions[]? | select((.type == "Complete" or .type == "Failed") and .status == "True") ]
                   | length) > 0 then "finished" else "running" end' <"${out}" | tr -d '\n' ;;
      404)     printf 'gone' ;;
      401|403) printf 'unreadable no RBAC read (jobs.get in %s answered HTTP %s; kubernetes/rbac/verify-rbac.yaml is applied by kubernetes/apply.sh --tenant %s, in the eng and smoke namespaces only)' \
                 "${ns}" "${status}" "${ns#"${TENANT_NAMESPACE_PREFIX}"}" ;;
      *)       printf 'unreadable the cluster answered HTTP %s' "${status}" ;;
    esac
  fi
  rm -f "${out}"
}

# capacity_holder_verdict NEWEST_ATTEMPT_JSON TENANT RUNNING_EXECUTIONS -> sets
# VERDICT to ok | gone | unchecked | refused and VERDICT_DETAIL to why.
# Directly, not $().
VERDICT=""
VERDICT_DETAIL=""
capacity_holder_verdict() {
  local attempt="$1" tenant="$2" running="$3" exec_name backend ns name answer task gen hex job
  exec_name="$(jq -r '.execution_name // ""' <<<"${attempt}")"
  backend="$(jq -r '.backend // ""' <<<"${attempt}")"
  task="$(jq -r '.task_id // ""' <<<"${attempt}")"
  gen="$(jq -r '.generation // "" | tostring' <<<"${attempt}")"
  # An attempt without one is a task the control plane never actually handed
  # to a backend -- the same finding by another route.
  if [[ -z "${exec_name}" || "${exec_name}" == "null" ]]; then
    VERDICT="gone"; VERDICT_DETAIL="its newest attempt names no execution"
    return 0
  fi
  if [[ "${backend}" == "GKE_AUTOPILOT" ]]; then
    if [[ "${exec_name}" == */* ]]; then
      # `<namespace>/<job>`: what the dispatcher records, and since #1804 what
      # the worker records too (SWARM_EXECUTION_NAME).
      ns="${exec_name%%/*}"; name="${exec_name##*/}"
    else
      # A bare name. Before #1804 a GKE worker recorded its POD's hostname,
      # `swarm-<task>-<gen>-<5 chars>`, and jobs/<pod> is a 404 for a live Job:
      # the 06:45Z 2026-10-21 gate called seven running agents gone that way.
      # The Job is computed FROM THE ATTEMPT, the dispatcher's way
      # (sanitize_name("swarm", <task id without task_>, <generation>), which
      # is that string unchanged for an id of [a-z0-9]), and the name must be
      # exactly that Job or that Job plus a 5-character pod suffix. Anything
      # else is not checked: a 404 for a name we cannot map proves nothing.
      hex="${task#task_}"; job="swarm-${hex}-${gen}"
      if [[ ! "${hex}" =~ ^[a-z0-9]+$ || ! "${gen}" =~ ^[0-9]+$ || "${#job}" -gt 63 ]] \
         || [[ "${exec_name}" != "${job}" && ! "${exec_name}" =~ ^${job}-[a-z0-9]{5}$ ]]; then
        VERDICT="unchecked"
        VERDICT_DETAIL="GKE execution ${exec_name} not checked: it names no namespace and is not the Job or pod of ${task:-its task} generation ${gen:-unknown}"
        return 0
      fi
      ns="$(tenant_namespace "${tenant}")"; name="${job}"
    fi
    gke_cluster_access
    case "${GKE_ACCESS}" in
      ok) ;;
      refused:*)
        VERDICT="refused"; VERDICT_DETAIL="GKE Job ${ns}/${name} could not be read: ${GKE_ACCESS#refused:}"
        return 0 ;;
      *)
        VERDICT="unchecked"; VERDICT_DETAIL="GKE Job ${ns}/${name} not checked: ${GKE_ACCESS#no:}"
        return 0 ;;
    esac
    answer="$(gke_job_status "${ns}" "${name}")"
    case "${answer}" in
      running)       VERDICT="ok";        VERDICT_DETAIL="GKE Job ${ns}/${name} is running" ;;
      gone|finished) VERDICT="gone";      VERDICT_DETAIL="GKE Job ${ns}/${name} is ${answer}" ;;
      *)             VERDICT="unchecked"; VERDICT_DETAIL="GKE Job ${ns}/${name} not checked: ${answer#unreadable }" ;;
    esac
    return 0
  fi
  case " ${running} " in
    *" ${exec_name##*/} "*) VERDICT="ok";   VERDICT_DETAIL="execution ${exec_name##*/} is running" ;;
    *)                      VERDICT="gone"; VERDICT_DETAIL="execution ${exec_name##*/}, which is not running" ;;
  esac
}

# judge_capacity_holders CHECKED MISMATCHED UNCHECKED REFUSED -> [10]'s verdict
# over what its loop counted. A refusal is a failure (the read is granted, so
# its absence is a deployment defect); unchecked is a skip; a holder against a
# dead execution or Job is a failure.
judge_capacity_holders() {
  local checked="$1" mismatched="$2" unchecked="$3" refused="$4"
  if [[ "${refused}" -gt 0 ]]; then
    t_fail "${refused} task(s) holding capacity on GKE could not be checked: ${GKE_ACCESS#refused:}"
  fi
  if [[ "${unchecked}" -gt 0 ]]; then
    # Not measured, and said so: an unreachable cluster, or a namespace the
    # Job read is not rendered in, is not a dead backend.
    if [[ "${GKE_ACCESS}" == "ok" ]]; then
      t_skip "${unchecked} task(s) holding capacity on GKE not checked: the cluster did not answer for their Jobs (each is named above)"
    else
      t_skip "${unchecked} task(s) holding capacity on GKE not checked: ${GKE_ACCESS#no:}"
    fi
  fi
  if [[ "$(( checked + unchecked + refused ))" -eq 0 ]]; then
    t_skip "no task was in DISPATCHED, STARTING or RUNNING while this check ran, so control plane and backend could not be compared"
  elif [[ "${checked}" -gt 0 ]]; then
    t_info "checked ${checked} task(s) holding capacity against their backend"
    assert_eq "0" "${mismatched}" "tasks holding capacity with no live execution behind them"
  fi
}

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
t_case "NEGATIVE CONTROL: a step promised a file nobody wrote fails, and its dependant never runs"
#
# Without this, every assertion above is satisfied by a platform that stages
# nothing and reports success anyway -- which is precisely the failure mode the
# suite exists to catch, so the suite has to demonstrate it can catch it. The
# producer writes one name; the consumer declares another. What the platform
# must do about it is in check_negative_control above.
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
  check_negative_control "${NEG_PRODUCE}" "${NEG_CONSUME}"
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
#
# THE CALLING TENANT'S ATTEMPTS ONLY (see own_tenant_rows above): the tenant
# this run's own producer was created in, which is the tenant its API calls act
# as. Filtered after the read rather than in the query, because an equality on
# tenant_id beside the IS_NOT_NULL inequality needs a composite index nobody
# has declared; the scan reads up to SPEND_SCAN_LIMIT usage rows to find 25.
SPEND_SCAN_LIMIT=500
COMPARED=0
DISAGREED=0
SPEND_ROWS=""
CALLER_TENANT="$(task_field "${PRODUCE_ID}" '.tenant_id // ""')" || CALLER_TENANT=""
if [[ -z "${CALLER_TENANT}" ]]; then
  t_fail "could not tell which tenant this run's API calls act as (task ${PRODUCE_ID} names none), so the spend scan cannot be scoped to it"
else
  t_info "comparing the spend of tenant ${CALLER_TENANT}, the tenant this run's tasks were created in"
  SPEND_ROWS="$(fs_query attempts "$(fs_null_filter input_tokens IS_NOT_NULL)" "${SPEND_SCAN_LIMIT}" \
    | own_tenant_rows "${CALLER_TENANT}" 25 || true)"
fi
if [[ -z "${SPEND_ROWS//[[:space:]]/}" ]]; then
  t_skip "no attempt of tenant ${CALLER_TENANT:-unknown} has recorded token usage, so Firestore and the API could not be compared. The mock profile has provider=null and reports none; run a claude-code or codex task, or pass --require-spend to make this a failure."
else
  while IFS= read -r row; do
    [[ -n "${row}" ]] || continue
    ROW_TASK="$(jq -r '.task_id // ""' <<<"${row}")"
    ROW_ATTEMPT="$(jq -r '.attempt_id // .id // ""' <<<"${row}")"
    [[ -n "${ROW_TASK}" && -n "${ROW_ATTEMPT}" ]] || continue
    CMP_BODY="$(mktemp "${TMPDIR:-/tmp}/swarm-e2e-cmp.XXXXXX")"
    if ! api_fetch "/tasks/${ROW_TASK}/attempts" "${CMP_BODY}"; then
      # A 404 here is its own finding: Firestore holds an attempt of the
      # caller's OWN tenant for a task the API will not serve to it.
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
# The control that keeps isolation tested now that the scan no longer trips
# over it: another tenant's task IS refused.
[[ -z "${CALLER_TENANT}" ]] || foreign_tenant_refused "${CALLER_TENANT}"

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
  # state, and it stops being redeemable after the broker's 15-minute
  # PENDING_TTL. It is NOT deleted, then or ever: PENDING_TTL is checked only
  # when someone redeems the state, and `account_auth` has no Firestore TTL
  # policy. There is no route that deletes one either, so cleaning it up is not
  # available rather than skipped, and every run leaves one such record.
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
  CHECKED=0
  UNCHECKED=0
  REFUSED=0
  for state in DISPATCHED STARTING RUNNING; do
    ROWS="$(fs_query tasks "$(fs_field_filter state EQUAL "$(jq -nc --arg v "${state}" '{stringValue:$v}')")" 50 \
      | jq -c "${FS_JQ} doc" || true)"
    [[ -n "${ROWS//[[:space:]]/}" ]] || continue
    while IFS= read -r row; do
      [[ -n "${row}" ]] || continue
      TID="$(jq -r '.id // ""' <<<"${row}")"
      [[ -n "${TID}" ]] || continue
      # The execution and its backend live on the ATTEMPT, written by the
      # dispatcher; the newest one is the one holding the capacity.
      NEWEST="$(task_attempts "${TID}" | jq -sc 'sort_by(.generation // 0) | last // {}')"
      capacity_holder_verdict "${NEWEST}" "$(jq -r '.tenant_id // ""' <<<"${row}")" "${RUNNING_NAMES}"
      case "${VERDICT}" in
        ok) CHECKED=$(( CHECKED + 1 )) ;;
        unchecked)
          UNCHECKED=$(( UNCHECKED + 1 ))
          t_info "task ${TID} is ${state}: ${VERDICT_DETAIL}"
          ;;
        refused)
          REFUSED=$(( REFUSED + 1 ))
          t_info "task ${TID} is ${state}: ${VERDICT_DETAIL}"
          ;;
        *)
          CHECKED=$(( CHECKED + 1 ))
          MISMATCHED=$(( MISMATCHED + 1 ))
          t_info "task ${TID} is ${state} against ${VERDICT_DETAIL}"
          ;;
      esac
    done <<<"${ROWS}"
  done
  judge_capacity_holders "${CHECKED}" "${MISMATCHED}" "${UNCHECKED}" "${REFUSED}"
else
  ERR_DETAIL="$(cat "${EXEC_ERR}")"
  rm -f "${EXEC_ERR}"
  die_if_auth_failure "${ERR_DETAIL}"
  t_fail "could not list Cloud Run executions, so the control plane could not be compared with the backend: $(printf '%s' "${ERR_DETAIL}" | redact | head -n 1)"
fi

# ---------------------------------------------------------------------------
t_case "A workflow whose steps have all finished does not still read QUEUED"
#
# A workflow's `state` was once written only at creation, so a workflow whose
# every step had SUCCEEDED served QUEUED for ever, through the same field a UI
# renders as progress. The API now derives it from the steps on every read
# (`swarm_api.rollup`, `routes/workflows.py`) and writes it back through
# `Store.set_workflow_state` when the stored copy disagrees.
#
# This is asserted rather than described because a check that agrees with a
# defect is how the defect survives, and the derivation could regress.
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
