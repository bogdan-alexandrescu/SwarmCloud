#!/usr/bin/env bash
# Prove the GKE path works: submit ONE browser task and follow it to the end.
#
# WHY. docs/incidents/2026-09-24-gke-dispatch.md: every `browser` task this
# platform ever accepted failed, from the day GKE dispatch shipped. Seven causes
# were stacked, every one produced the same `jobs.batch is forbidden`, and the
# incident's section 5 ends on the point this script exists to act on: whether
# the last cause is the last one cannot be known from the repository -- "the
# cheapest proof is one task". The release runs this after every deploy, so
# that proof is taken every time rather than remembered.
#
# WHAT IT ASSERTS, each separately, because each was a different failure:
#
#   1. the task was DISPATCHED, and to GKE_AUTOPILOT -- causes 1, 3 and 6 all
#      stopped it before a Job existed, and a task that went to Cloud Run
#      proves nothing about GKE however green it ends;
#   2. it SUCCEEDED -- cause 7 created the Job and died on a read-only root
#      filesystem, which only running it can show;
#   3. it left artifacts under the tenant's OWN prefix -- the browser runner
#      writes a screenshot, so none means the worker never got that far;
#   4. its lease was released -- a finished task that still holds capacity is
#      the leak invariant 1 exists to prevent.
#
# A PARKED TASK PROVES NOTHING, and says so. The 30-node redispatch in the
# incident parked six browser steps before any reached a backend. Parked on
# CREDENTIAL_MISSING -- the caller's tenant holds no anthropic credential --
# this fails at once rather than waiting out its timeout, naming the fix.
#
# WHAT IT SUBMITS. The browser runner refuses an input with neither `url` nor
# `actions`, so a generic `{message: ...}` input fails at the runner with
# dispatch working perfectly -- which is what the smoke suite's backend matrix
# used to submit. This takes one screenshot of about:blank: Chromium starts,
# /dev/shm is big enough, the workspace is writable and an artifact uploads,
# all without depending on any site outside the platform. `--url` adds a page
# load, which also proves the worker's egress.
#
# It creates one task, as the calling identity's tenant, and nothing else. It
# never touches the cluster directly; everything goes through the API, and
# what is observed is read from Firestore and GCS, as every suite here does.
#
# Usage: scripts/prove-gke-dispatch.sh [--timeout 900] [--url https://example.com] [--keep]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="gke-proof"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

PROFILE="browser"
BACKEND="GKE_AUTOPILOT"
TIMEOUT=900
URL=""
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --url)     URL="$2"; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) sed -n '2,42p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "${TIMEOUT}" =~ ^[0-9]+$ ]] || die "--timeout must be a number of seconds, got ${TIMEOUT}"

step "GKE dispatch proof: one ${PROFILE} task on ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform
info "api ${API_URL:-$(api_url)}"

# ---------------------------------------------------------------------------
t_case "Submit one ${PROFILE} task"
RUN_ID="$(test_run_id)"
# The shared per-profile input (testlib.sh): one screenshot of about:blank. A
# --url is merged in as the page to load first, which also proves egress.
EXTRA_INPUT="$(jq -nc --arg u "${URL}" 'if $u == "" then {} else {url: $u} end')"
if ! TASK_ID="$(submit_task "${PROFILE}" "$(profile_input "${PROFILE}" "${RUN_ID}" | jq -c --argjson x "${EXTRA_INPUT}" '. + $x')" \
    '{"priority":10,"metadata":{"source":"prove-gke-dispatch"}}')"; then
  t_fail "the ${PROFILE} task could not be submitted; see the API response above"
  t_summary
  exit 1
fi
t_pass "task ${TASK_ID}"

# ---------------------------------------------------------------------------
# Follow it. Terminal states end the wait; so does a park that cannot resolve
# by itself. Polled with the same knob testlib's wait_for_state reads.
t_case "It reaches a terminal state"
DEADLINE=$(( $(date -u +%s) + TIMEOUT ))
FINAL=""
PARKED_ON=""
while :; do
  if ! DOC="$(task_doc "${TASK_ID}")"; then
    die "could not read task ${TASK_ID}; see the Firestore error above -- a failed read is not evidence of anything about dispatch"
  fi
  STATE="$(printf '%s' "${DOC}" | jq -r '.state // "MISSING"')"
  case "${STATE}" in
    SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED)
      FINAL="${STATE}"
      break
      ;;
    PARKED)
      PARKED_ON="$(printf '%s' "${DOC}" | jq -r '.park_reason // "unknown"')"
      if [[ "${PARKED_ON}" == "CREDENTIAL_MISSING" ]]; then
        break
      fi
      ;;
  esac
  if [[ "$(date -u +%s)" -ge "${DEADLINE}" ]]; then
    break
  fi
  sleep "${SWARM_POLL_INTERVAL_SECONDS:-5}"
done

if [[ -n "${FINAL}" ]]; then
  t_pass "reached ${FINAL}"
elif [[ "${PARKED_ON}" == "CREDENTIAL_MISSING" ]]; then
  t_fail "PARKED on CREDENTIAL_MISSING: the calling identity's tenant holds no anthropic credential, which the ${PROFILE} profile requires"
  t_info "a parked task never reaches a backend, so this proves NOTHING about GKE dispatch either way"
  t_info "run this as an identity whose tenant has a key (scripts/create-secrets.sh --provider anthropic) or a pool account"
else
  t_fail "no terminal state within ${TIMEOUT}s (stuck at ${STATE}${PARKED_ON:+, parked on ${PARKED_ON}})"
fi

# ---------------------------------------------------------------------------
t_case "It was dispatched to ${BACKEND}"
EVENTS="${TMPDIR:-/tmp}/swarm-gke-proof-events.$$"
if ! task_events "${TASK_ID}" >"${EVENTS}"; then
  rm -f "${EVENTS}"
  die "could not read the events of ${TASK_ID}; a failed read is not evidence the task was never dispatched"
fi
DISPATCHED_TO="$(jq -r 'select(.type == "dispatched") | .detail.backend // "unrecorded"' "${EVENTS}" | tail -n 1)"
EXECUTION="$(jq -r 'select(.type == "dispatched") | .detail.execution_name // empty' "${EVENTS}" | tail -n 1)"
rm -f "${EVENTS}"
LAST_ERROR="$(printf '%s' "${DOC}" | jq -r '.last_error // empty')"
if [[ -z "${DISPATCHED_TO}" ]]; then
  t_fail "never dispatched: no Job was created for it${LAST_ERROR:+ (last_error: ${LAST_ERROR})}"
  t_info "a 'jobs.batch is forbidden' here is the 403-that-means-404: check the namespace, then the"
  t_info "RoleBinding's two subject spellings, in the order docs/incidents/2026-09-24-gke-dispatch.md section 4 gives"
elif [[ "${DISPATCHED_TO}" != "${BACKEND}" ]]; then
  t_fail "dispatched to ${DISPATCHED_TO}, not ${BACKEND}: this run proves nothing about GKE, however it ended"
else
  t_pass "dispatched to ${BACKEND}${EXECUTION:+ as ${EXECUTION}}"
fi

# ---------------------------------------------------------------------------
t_case "It succeeded"
if [[ "${FINAL}" == "SUCCEEDED" ]]; then
  t_pass "SUCCEEDED"
else
  t_fail "ended ${FINAL:-${STATE}}, not SUCCEEDED"
  [[ -z "${LAST_ERROR}" ]] || t_info "last_error: ${LAST_ERROR}"
  # The attempt's own error is the worker's words, not the scheduler's code:
  # it is where cause 7's `Read-only file system` would appear.
  if ATTEMPTS="$(task_attempts "${TASK_ID}")"; then
    printf '%s\n' "${ATTEMPTS}" \
      | jq -r 'select(. != null) | "attempt \(.attempt_id // "?") on \(.backend // "?"): exit \(.exit_code // "none"), error \(.error // "none")"' \
      | redact | while IFS= read -r line; do t_info "${line}"; done
  fi
fi

# ---------------------------------------------------------------------------
t_case "Its artifacts landed under the tenant's own prefix"
TENANT_ID="$(printf '%s' "${DOC}" | jq -r '.tenant_id // empty')"
if [[ "${FINAL}" != "SUCCEEDED" ]]; then
  t_fail "no artifacts to look for: the task did not succeed"
elif [[ -z "${TENANT_ID}" ]]; then
  t_fail "the task records no tenant_id, so its prefix cannot be named"
else
  PREFIX="tenants/${TENANT_ID}/tasks/${TASK_ID}/"
  ls_err="${TMPDIR:-/tmp}/swarm-gke-proof-ls.$$"
  if COUNT="$(gcs_object_count "${ARTIFACT_BUCKET}" "${PREFIX}" 2>"${ls_err}")"; then
    rm -f "${ls_err}"
    if [[ "${COUNT}" -gt 0 ]]; then
      t_pass "${COUNT} object(s) under gs://${ARTIFACT_BUCKET}/${PREFIX}"
    else
      t_fail "no artifacts under gs://${ARTIFACT_BUCKET}/${PREFIX}: the runner takes a screenshot, so none means it never got that far"
    fi
  else
    err_detail="$(cat "${ls_err}" 2>/dev/null || true)"
    rm -f "${ls_err}"
    die_if_auth_failure "${err_detail}"
    t_fail "could not list gs://${ARTIFACT_BUCKET}/${PREFIX}: $(printf '%s' "${err_detail}" | redact | head -n 1)"
  fi
fi

# ---------------------------------------------------------------------------
t_case "Its capacity was returned"
LEASE_ID="$(printf '%s' "${DOC}" | jq -r '.current_lease_id // empty')"
if [[ -z "${FINAL}" ]]; then
  t_fail "the task is not finished, so its lease cannot have been returned yet"
elif [[ -z "${LEASE_ID}" ]]; then
  t_fail "the task records no lease, so whether capacity was returned cannot be checked"
elif ! LEASE="$(fs_get "leases/${LEASE_ID}")"; then
  die "could not read lease ${LEASE_ID}; see the Firestore error above"
else
  RELEASED="$(printf '%s' "${LEASE}" | jq -r "${FS_JQ} if .fields then (doc.released_at // \"null\") else \"missing\" end")"
  if [[ "${RELEASED}" != "null" && "${RELEASED}" != "missing" ]]; then
    t_pass "lease ${LEASE_ID} released at ${RELEASED}"
  else
    t_fail "lease ${LEASE_ID} is still holding capacity (released_at=${RELEASED})"
  fi
fi

# A proof task that is still in flight -- parked, or cut off by the timeout --
# is cancelled, so it does not sit holding a place in the tenant's queue.
if [[ -z "${FINAL}" && "${KEEP}" -eq 0 ]]; then
  cancel_all "${TASK_ID}"
  t_info "cancelled ${TASK_ID}, which had not finished"
fi

t_summary
