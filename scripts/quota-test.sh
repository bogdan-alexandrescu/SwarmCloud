#!/usr/bin/env bash
# Provider-quota behaviour: exhaustion must cost nothing.
#
# The property under test is the expensive one to get wrong. When a provider is
# rate-limited, the wrong design keeps workers alive sleeping through the window
# -- paying for CPU and memory to wait. This platform requires the opposite:
# checkpoint, PARK, release the lease, exit, and let the parked task cost nothing
# until the window reopens (CONTRACT.md invariant 4).
#
# The test drives quota state directly in Firestore, which is the same document
# the quota broker owns, and restores the original state on every exit path.
#
# Usage: scripts/quota-test.sh [--provider anthropic] [--profile claude-code] [--timeout 300]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="quota"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

PROVIDER="anthropic"
PROFILE=""
TIMEOUT=300
TENANT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider) PROVIDER="$2"; shift 2 ;;
    --profile)  PROFILE="$2"; shift 2 ;;
    --tenant)   TENANT="$2"; shift 2 ;;
    --timeout)  TIMEOUT="$2"; shift 2 ;;
    -h|--help)  sed -n '2,14p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Quota behaviour: provider ${PROVIDER}"
require_platform

# Which runner profile consumes this provider. Names come from the frozen
# catalogue in swarm_common.profiles, never from a caller.
if [[ -z "${PROFILE}" ]]; then
  case "${PROVIDER}" in
    anthropic) PROFILE="claude-code" ;;
    openai)    PROFILE="codex" ;;
    *)         die "no default runner profile for provider ${PROVIDER}; pass --profile" ;;
  esac
fi

if [[ -z "${TENANT}" ]]; then
  TENANT="$(api_get "/tenants/me" 2>/dev/null | jq -r '.tenant_id // .id // empty')"
  [[ -n "${TENANT}" ]] || die "could not determine your tenant; pass --tenant"
fi
QUOTA_DOC="quota/${PROVIDER}:${TENANT}"
POOL_NAME="provider:${PROVIDER}:tenant:${TENANT}"
info "tenant ${TENANT}, quota document ${QUOTA_DOC}"

ORIGINAL="$(fs_get "${QUOTA_DOC}" | jq -c "${FS_JQ} if .fields then doc else null end")"
if [[ "${ORIGINAL}" == "null" || -z "${ORIGINAL}" ]]; then
  die "no quota document at ${QUOTA_DOC}; register the tenant and store a ${PROVIDER} key first"
fi
ORIGINAL_STATE="$(jq -r '.state // "AVAILABLE"' <<<"${ORIGINAL}")"
info "current provider state: ${ORIGINAL_STATE}"

TASK_IDS=()
restore() {
  # Always put the provider back, even on Ctrl-C: leaving a provider EXHAUSTED
  # would silently park every task of this tenant.
  fs_patch "${QUOTA_DOC}" "state,cooldown_until,updated_at" \
    "$(jq -nc --arg s "${ORIGINAL_STATE}" --arg t "$(iso_now)" \
      '{state:{stringValue:$s},cooldown_until:{nullValue:null},updated_at:{timestampValue:$t}}')" \
    >/dev/null 2>&1 || true
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
  info "restored ${PROVIDER} to ${ORIGINAL_STATE}"
}
trap restore EXIT INT TERM

# ---------------------------------------------------------------------------
t_case "Exhausting the provider drops its effective limit to zero"
COOLDOWN_UNTIL="$(date -u -v+10M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '10 minutes' +%Y-%m-%dT%H:%M:%SZ)"
fs_patch "${QUOTA_DOC}" "state,cooldown_until,last_429_at,updated_at" \
  "$(jq -nc --arg c "${COOLDOWN_UNTIL}" --arg t "$(iso_now)" \
    '{state:{stringValue:"EXHAUSTED"},cooldown_until:{timestampValue:$c},
      last_429_at:{timestampValue:$t},updated_at:{timestampValue:$t}}')"
NEW_STATE="$(fs_get "${QUOTA_DOC}" | jq -r "${FS_JQ} doc.state")"
assert_eq "EXHAUSTED" "${NEW_STATE}" "provider state"

# QuotaState.effective_limit returns 0 for EXHAUSTED and COOLDOWN, and the broker
# mirrors that into the per-tenant provider pool as quota_derived_limit.
pool_quota_derived_zero() {
  local doc
  doc="$(pool_doc "$1")"
  [[ "${doc}" != "null" && -n "${doc}" ]] || return 1
  [[ "$(jq -r '.quota_derived_limit // -1' <<<"${doc}")" == "0" ]]
}

if [[ "$(pool_doc "${POOL_NAME}")" == "null" ]]; then
  t_info "pool ${POOL_NAME} does not exist; an unconfigured pool is unlimited by construction"
  t_info "the admission block therefore has to come from the quota check, which the next case exercises"
  t_pass "no stale per-tenant provider pool to mislead the test"
elif wait_until "quota_derived_limit on ${POOL_NAME} to reach 0" 90 pool_quota_derived_zero "${POOL_NAME}"; then
  t_pass "quota broker propagated the exhaustion into ${POOL_NAME} (quota_derived_limit=0)"
else
  t_fail "quota_derived_limit on ${POOL_NAME} is $(pool_doc "${POOL_NAME}" | jq -r '.quota_derived_limit // "unset"') after 90s"
fi

# ---------------------------------------------------------------------------
t_case "A task for an exhausted provider parks instead of running"
BASE_HOLDING="$(holding_capacity)"
if ! id="$(submit_task "${PROFILE}" '{"prompt":"quota probe"}' \
      '{"metadata":{"source":"quota-test"}}')"; then
  t_fail "could not submit a ${PROFILE} task"
else
  TASK_IDS+=("${id}")
  t_info "task ${id}"
  DEADLINE=$(( $(date -u +%s) + TIMEOUT ))
  OBSERVED_RUNNING=0
  FINAL_STATE=""
  while [[ "$(date -u +%s)" -lt "${DEADLINE}" ]]; do
    FINAL_STATE="$(task_state "${id}")"
    case "${FINAL_STATE}" in
      RUNNING|STARTING|DISPATCHED) OBSERVED_RUNNING=1; break ;;
      PARKED) break ;;
      SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED) break ;;
    esac
    sleep 3
  done
  t_info "task settled at ${FINAL_STATE}"
  assert_eq "0" "${OBSERVED_RUNNING}" "task never reached a compute-consuming state"
  case "${FINAL_STATE}" in
    PARKED)
      REASON="$(task_field "${id}" '.park_reason // "none"')"
      case "${REASON}" in
        PROVIDER_QUOTA_EXHAUSTED|PROVIDER_COOLDOWN|CREDENTIAL_MISSING)
          t_pass "parked with reason ${REASON}" ;;
        *) t_fail "parked with unexpected reason ${REASON}" ;;
      esac ;;
    QUEUED|READY)
      BLOCKED="$(task_field "${id}" '.blocked_by // [] | tostring')"
      t_pass "held in ${FINAL_STATE}, blocked_by=${BLOCKED}" ;;
    *) t_fail "expected PARKED or a queued state, got ${FINAL_STATE}" ;;
  esac
fi

# ---------------------------------------------------------------------------
t_case "Parking consumed no capacity"
assert_le "$(holding_capacity)" "${BASE_HOLDING}" "tasks holding capacity while parked"
if [[ -n "${id:-}" ]]; then
  ATTEMPTS="$(task_field "${id}" '.attempt_count // 0')"
  assert_eq "0" "${ATTEMPTS}" "attempts started for the parked task"
fi

# ---------------------------------------------------------------------------
t_case "Restoring the provider releases the parked work"
fs_patch "${QUOTA_DOC}" "state,cooldown_until,updated_at" \
  "$(jq -nc --arg t "$(iso_now)" \
    '{state:{stringValue:"AVAILABLE"},cooldown_until:{nullValue:null},updated_at:{timestampValue:$t}}')"
if gcloud pubsub topics describe "${PUBSUB_TOPIC}" --project "${PROJECT_ID}" \
     --format='value(name)' >/dev/null 2>&1; then
  gcloud pubsub topics publish "${PUBSUB_TOPIC}" --project "${PROJECT_ID}" \
    --message='{"reason":"quota-test-restore"}' >/dev/null
  t_info "woke the scheduler"
fi
assert_eq "AVAILABLE" "$(fs_get "${QUOTA_DOC}" | jq -r "${FS_JQ} doc.state")" "provider state after restore"

if [[ -n "${id:-}" ]]; then
  if wait_until "the parked task to become eligible again" 120 \
       task_in_state "${id}" "READY|LEASED|DISPATCHED|STARTING|RUNNING|SUCCEEDED"; then
    t_pass "task moved to $(task_state "${id}") once quota returned"
  else
    t_info "task is at $(task_state "${id}")"
    t_fail "task did not become eligible within 120s of quota returning"
  fi
fi

t_summary
