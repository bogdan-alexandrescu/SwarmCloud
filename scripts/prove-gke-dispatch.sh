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
#      the leak invariant 1 exists to prevent. Every lease it held is named
#      from its EVENTS, because the worker clears `current_lease_id` as the
#      task ends (see task_lease_ids in lib/testlib.sh). The check waits up to
#      --lease-wait seconds, because the worker writes the terminal state
#      before it releases the lease.
#
# A PARKED TASK PROVES NOTHING, and says so. The 30-node redispatch in the
# incident parked six browser steps before any reached a backend. Parked on
# CREDENTIAL_MISSING -- the caller's tenant has no anthropic key, and no pool
# account can run the profile -- this fails at once rather than waiting out its
# timeout, naming the fix from what the account pool answered. For `browser`
# the pool's answer is always `profile_takes_no_subscription`: an account is a
# Claude subscription and this profile takes an API key only, so the identity
# this runs as needs a tenant with a key of its own (#169).
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
# Usage: scripts/prove-gke-dispatch.sh [--timeout 900] [--lease-wait 60] [--url https://example.com] [--keep]

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
# How long a lease may still read as held after the task reads terminal. The
# real gap is one transaction after three writes (control.finish), so well
# under a second. 60s allows for a worker that is slow to reach its release,
# and is still short enough that a lease which never comes back fails the
# release promptly rather than after the whole task timeout.
LEASE_WAIT=60
URL=""
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout)    TIMEOUT="$2"; shift 2 ;;
    --lease-wait) LEASE_WAIT="$2"; shift 2 ;;
    --url)        URL="$2"; shift 2 ;;
    --keep)       KEEP=1; shift ;;
    -h|--help)    sed -n '2,45p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "${TIMEOUT}" =~ ^[0-9]+$ ]] || die "--timeout must be a number of seconds, got ${TIMEOUT}"
[[ "${LEASE_WAIT}" =~ ^[0-9]+$ ]] || die "--lease-wait must be a number of seconds, got ${LEASE_WAIT}"

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
  # WHAT THE ACCOUNT POOL ANSWERED, read from the park's own event rather than
  # guessed here (#169). Admission records it as `account_pool`
  # (apps/scheduler/scheduler/credentials.py); a worker that parks on the pool
  # records `account_pool_reason`. This hint used to offer "or a pool account"
  # to every profile, and no pool account can run `browser` at all.
  PARK_TENANT="$(printf '%s' "${DOC}" | jq -r '.tenant_id // "<tenant>"')"
  PARK_PROVIDER="anthropic"
  POOL_ANSWER=""
  PARK_EVENTS="${TMPDIR:-/tmp}/swarm-gke-proof-park.$$"
  if task_events "${TASK_ID}" >"${PARK_EVENTS}" 2>/dev/null; then
    POOL_ANSWER="$(jq -r 'select(.type == "parked") | .detail.account_pool // .detail.account_pool_reason // empty' "${PARK_EVENTS}" | tail -n 1)"
    PARK_PROVIDER="$(jq -r 'select(.type == "parked") | .detail.provider // empty' "${PARK_EVENTS}" | tail -n 1)"
    PARK_PROVIDER="${PARK_PROVIDER:-anthropic}"
  fi
  rm -f "${PARK_EVENTS}"
  KEY_FIX="scripts/create-secrets.sh --tenant ${PARK_TENANT} --provider ${PARK_PROVIDER} --stdin"
  t_fail "PARKED on CREDENTIAL_MISSING: tenant ${PARK_TENANT} has no ${PARK_PROVIDER} key and no pool account that can run the ${PROFILE} profile (the account pool answered: ${POOL_ANSWER:-nothing recorded})"
  t_info "a parked task never reaches a backend, so this proves NOTHING about GKE dispatch either way"
  case "${POOL_ANSWER}" in
    profile_takes_no_subscription)
      t_info "no pool account can run the ${PROFILE} profile: an account is a Claude subscription, and this profile takes no subscription token"
      t_info "the fix is a key of the tenant's own: ${KEY_FIX}, or run this as an identity whose tenant already has one"
      ;;
    no_accounts_registered)
      t_info "no ${PARK_PROVIDER} account in the pool is owned by or lent to tenant ${PARK_TENANT}"
      t_info "the fix is a key of the tenant's own (${KEY_FIX}), or lending it an account: scripts/api.sh PUT /v1/accounts/<account_id>/lending '{\"lend_to\": [\"${PARK_TENANT}\"]}'"
      ;;
    no_broker_configured)
      t_info "this deployment has no account pool, so a key is the only credential: ${KEY_FIX}"
      ;;
    *)
      t_info "the park names no account-pool answer${POOL_ANSWER:+ it recognises (${POOL_ANSWER})}: run this as an identity whose tenant has a key (${KEY_FIX})"
      ;;
  esac
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
if [[ -z "${FINAL}" ]]; then
  t_fail "the task is not finished, so its lease cannot have been returned yet"
elif ! t_check_leases_released "${TASK_ID}" "${LEASE_WAIT}" fail; then
  t_info "see the Firestore error above: the check is red because the read failed, not because a lease is held"
fi

# A proof task that is still in flight -- parked, or cut off by the timeout --
# is cancelled, so it does not sit holding a place in the tenant's queue.
if [[ -z "${FINAL}" && "${KEEP}" -eq 0 ]]; then
  cancel_all "${TASK_ID}"
  t_info "cancelled ${TASK_ID}, which had not finished"
fi

t_summary
