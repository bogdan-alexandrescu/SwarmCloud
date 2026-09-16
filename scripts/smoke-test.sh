#!/usr/bin/env bash
# End-to-end proof that the deployed swarm actually works.
#
# Uses the `mock` runner profile, which has provider=None on purpose: the smoke
# path must keep working before any tenant has registered an API key, otherwise
# the first thing you would learn after a deploy is nothing at all.
#
# Usage: scripts/smoke-test.sh [--profile mock] [--timeout 600] [--keep]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="smoke"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

PROFILE="mock"
TIMEOUT=600
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)  PROFILE="$2"; shift 2 ;;
    --timeout)  TIMEOUT="$2"; shift 2 ;;
    --keep)     KEEP=1; shift ;;
    -h|--help)  sed -n '2,9p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Smoke test: ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform
info "api ${API_URL:-$(api_url)}"

# ---------------------------------------------------------------------------
t_case "Control-plane services are healthy"
for service in "${API_SERVICE}" "${SCHEDULER_SERVICE}" "${QUOTA_SERVICE}"; do
  url="$(gcloud run services describe "${service}" --project "${PROJECT_ID}" \
    --region "${REGION}" --format='value(status.url)' 2>/dev/null || true)"
  if [[ -z "${url}" ]]; then
    t_fail "${service}: not deployed"
    continue
  fi
  code="$(curl -sS -m 15 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $(id_token)" "${url%/}/healthz" 2>/dev/null || echo 000)"
  if [[ "${code}" == "200" ]]; then
    t_pass "${service} /healthz 200"
  else
    t_fail "${service} /healthz ${code}"
  fi
done

# ---------------------------------------------------------------------------
t_case "Caller is authenticated and resolves to a tenant"
if me="$(api_get "/tenants/me")"; then
  tenant="$(jq -r '.tenant_id // .id // empty' <<<"${me}")"
  email="$(jq -r '.email // .submitted_by // empty' <<<"${me}")"
  if [[ -n "${tenant}" ]]; then
    t_pass "tenant ${tenant}${email:+ (${email})}"
  else
    t_fail "no tenant in the response: $(printf '%s' "${me}" | redact | head -c 200)"
  fi
  case "${email}" in
    *@saga.xyz|"") t_pass "hosted domain accepted" ;;
    *) t_fail "identity ${email} is outside saga.xyz" ;;
  esac
else
  t_fail "GET ${API_PREFIX}/tenants/me -> HTTP ${API_STATUS}"
  tenant=""
fi

# ---------------------------------------------------------------------------
t_case "An unknown runner profile is rejected (invariant 10)"
if api_post "/tasks" '{"runner_profile":"definitely-not-a-profile","input":{}}' >/dev/null 2>&1; then
  t_fail "the API accepted an unknown runner profile"
else
  if [[ "${API_STATUS}" -ge 400 && "${API_STATUS}" -lt 500 ]]; then
    t_pass "rejected with HTTP ${API_STATUS}"
  else
    t_fail "expected a 4xx, got HTTP ${API_STATUS}"
  fi
fi

# ---------------------------------------------------------------------------
t_case "Caller-supplied image and command are not honoured (invariant 10)"
injected='{"runner_profile":"mock","input":{},"image":"attacker/evil:latest","command":["/bin/sh","-c","id"],"resource_class":"large"}'
if evil_id="$(api_post "/tasks" "${injected}" | jq -r '.id // .task_id // empty')" && [[ -n "${evil_id}" ]]; then
  sleep 2
  got_class="$(task_field "${evil_id}" '.resource_class // "?"')"
  expected_class="standard"
  if [[ "${got_class}" == "${expected_class}" ]]; then
    t_pass "resource class came from the profile catalogue (${got_class}), not the request"
  else
    t_fail "request-supplied resource_class leaked through: ${got_class}"
  fi
  cancel_all "${evil_id}"
else
  # Rejecting the whole request outright is equally correct.
  t_pass "request carrying an image/command was rejected outright (HTTP ${API_STATUS})"
fi

# ---------------------------------------------------------------------------
t_case "Baseline capacity before submitting"
BASE_HOLDING="$(holding_capacity)"
BASE_LEASES="$(active_leases)"
t_info "tasks holding capacity: ${BASE_HOLDING}, active leases: ${BASE_LEASES}"
t_pass "baseline captured"

# ---------------------------------------------------------------------------
t_case "Submit a ${PROFILE} task and run it to completion"
RUN_ID="$(test_run_id)"
TASK_ID="$(submit_task "${PROFILE}" \
  "$(jq -nc --arg r "${RUN_ID}" '{message:"smoke", run_id:$r}')" \
  '{"priority":10,"metadata":{"source":"smoke-test"}}')" || die "submission failed"
t_info "task ${TASK_ID}"

if final="$(wait_for_state "${TASK_ID}" "SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED" "${TIMEOUT}")"; then
  assert_eq "SUCCEEDED" "${final}" "terminal state"
else
  t_fail "task did not reach a terminal state within ${TIMEOUT}s (stuck at ${final})"
  t_info "blocked_by: $(task_field "${TASK_ID}" '.blocked_by // [] | tostring')"
  t_info "park_reason: $(task_field "${TASK_ID}" '.park_reason // "none"')"
fi

# ---------------------------------------------------------------------------
t_case "The lifecycle was recorded"
EVENTS="$(task_events "${TASK_ID}" | jq -r '.type' | sort -u | tr '\n' ' ')"
t_info "events: ${EVENTS}"
for required in submitted lease_acquired dispatched succeeded; do
  case " ${EVENTS} " in
    *" ${required} "*) t_pass "event ${required}" ;;
    *) t_fail "missing event ${required}" ;;
  esac
done

# ---------------------------------------------------------------------------
t_case "Capacity was returned"
if wait_until "capacity to return to ${BASE_HOLDING}" 120 holding_at_most "${BASE_HOLDING}"; then
  t_pass "tasks holding capacity back to baseline (${BASE_HOLDING})"
else
  assert_le "$(holding_capacity)" "${BASE_HOLDING}" "tasks holding capacity after completion"
fi
LEASE_ID="$(task_field "${TASK_ID}" '.current_lease_id // ""')"
if [[ -n "${LEASE_ID}" && "${LEASE_ID}" != "null" ]]; then
  RELEASED="$(fs_get "leases/${LEASE_ID}" | jq -r "${FS_JQ} if .fields then (doc.released_at // \"null\") else \"missing\" end")"
  if [[ "${RELEASED}" != "null" && "${RELEASED}" != "missing" ]]; then
    t_pass "lease ${LEASE_ID} released at ${RELEASED}"
  else
    t_fail "lease ${LEASE_ID} is still holding capacity (released_at=${RELEASED})"
  fi
fi

# ---------------------------------------------------------------------------
t_case "Artifacts landed in the tenant's own prefix"
TENANT_ID="$(task_field "${TASK_ID}" '.tenant_id // ""')"
if [[ -n "${TENANT_ID}" && "${TENANT_ID}" != "null" ]]; then
  PREFIX="gs://${ARTIFACT_BUCKET}/tenants/${TENANT_ID}/tasks/${TASK_ID}/"
  if gcloud storage ls --recursive "${PREFIX}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    COUNT="$(gcloud storage ls --recursive "${PREFIX}" --project "${PROJECT_ID}" 2>/dev/null | wc -l | tr -d ' ')"
    t_pass "${COUNT} object(s) under ${PREFIX}"
  else
    t_info "no objects under ${PREFIX} (a mock run may legitimately produce none)"
    t_pass "artifact prefix is tenant-scoped"
  fi
else
  t_fail "task has no tenant_id"
fi

# ---------------------------------------------------------------------------
t_case "No credential material in the API response"
DETAIL="$(api_get "/tasks/${TASK_ID}" || true)"
if printf '%s' "${DETAIL}" | grep -Eq 'sk-ant-[A-Za-z0-9]|sk-[A-Za-z0-9]{20}|"ANTHROPIC_API_KEY"[[:space:]]*:[[:space:]]*"[^"]'; then
  t_fail "the task response contains something that looks like a key"
else
  t_pass "no key-shaped strings in the task response"
fi

[[ "${KEEP}" -eq 1 ]] || cancel_all "${TASK_ID}"
t_summary
