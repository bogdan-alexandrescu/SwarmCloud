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
  describe_err="$(mktemp "${TMPDIR:-/tmp}/swarm-smoke-describe.XXXXXX")"
  # REST rather than `gcloud run services describe`, so this suite runs in an
  # image with no Cloud SDK -- see cloud_run_service_uri in lib/common.sh for
  # why that matters. Same distinction preserved: a service that is ABSENT and
  # a lookup that could not be MADE are different failures.
  if ! url="$(cloud_run_service_uri "${service}" 2>"${describe_err}")"; then
    err_detail="$(cat "${describe_err}")"
    rm -f "${describe_err}"
    die_if_auth_failure "${err_detail}"
    if grep -qi 'NOT_FOUND\|could not be found\|does not exist\|not found' <<<"${err_detail}"; then
      t_fail "${service}: not deployed"
    else
      t_fail "${service}: could not check deployment status: $(printf '%s' "${err_detail}" | redact | head -n 1)"
    fi
    continue
  fi
  rm -f "${describe_err}"
  if [[ -z "${url}" ]]; then
    t_fail "${service}: describe succeeded but returned no status.url"
    continue
  fi
  # /readyz ONLY for the service this identity may invoke and holds a token
  # for. id_token() mints one token, for API_AUDIENCE; presenting it to
  # swarm-scheduler or swarm-quota-broker gets a 401 for the wrong audience
  # even before IAM is consulted -- which is exactly what this suite reported
  # on its first successful run, as two health failures against two healthy
  # services.
  #
  # The others are checked through the Admin API instead. See
  # cloud_run_service_ready for why that is not a weaker check but a
  # differently-scoped one: making the probe work would mean adding a test
  # identity to the invoker list of the service holding every tenant's
  # subscription credentials.
  if [[ "${service}" == "${API_SERVICE}" ]]; then
    # PROBED AT api_url, NOT at the *.run.app URL this loop just resolved.
    #
    # `cloud_run_service_uri` answers with status.url, and swarm-api's ingress
    # is internal-and-cloud-load-balancing: from outside the VPC that hostname
    # answers 404 to every path, including /readyz, before the request reaches
    # the container. This suite reported that as "swarm-api /readyz 404" -- a
    # health failure against a healthy service, for the second time in this
    # file. api_url resolves the address that actually serves, and
    # api_credential presents the kind of token that address accepts.
    readyz_err="$(mktemp "${TMPDIR:-/tmp}/swarm-smoke-readyz.XXXXXX")"
    if code="$(auth_config "$(api_credential)" \
        | curl -sS -m 15 -K - -o /dev/null -w '%{http_code}' \
        "$(api_url)/readyz" 2>"${readyz_err}")"; then
      rm -f "${readyz_err}"
      if [[ "${code}" == "200" ]]; then
        t_pass "${service} /readyz 200"
      else
        t_fail "${service} /readyz ${code}"
      fi
    else
      err_detail="$(cat "${readyz_err}")"
      rm -f "${readyz_err}"
      die_if_auth_failure "${err_detail}"
      t_fail "${service}: /readyz unreachable: $(printf '%s' "${err_detail}" | redact | head -n 1)"
    fi
    continue
  fi

  ready_err="$(mktemp "${TMPDIR:-/tmp}/swarm-smoke-ready.XXXXXX")"
  if ! ready="$(cloud_run_service_ready "${service}" 2>"${ready_err}")"; then
    err_detail="$(cat "${ready_err}")"
    rm -f "${ready_err}"
    die_if_auth_failure "${err_detail}"
    t_fail "${service}: could not read its serving condition: $(printf '%s' "${err_detail}" | redact | head -n 1)"
    continue
  fi
  rm -f "${ready_err}"
  case "${ready}" in
    True)  t_pass "${service} serving revision Ready" ;;
    False) t_fail "${service} serving revision is NOT Ready" ;;
    # Unknown is a deploy in flight or a condition this API version does not
    # report. Not a pass -- an unread condition is not a healthy one.
    *)     t_fail "${service} readiness is ${ready}; the condition was not reported" ;;
  esac
done

# ---------------------------------------------------------------------------
t_case "Caller is authenticated and resolves to a tenant"
me_body="$(mktemp "${TMPDIR:-/tmp}/swarm-smoke-me.XXXXXX")"
# api_get is called directly (redirected to a file), not inside "$(...)" --
# a command substitution forks a subshell, which would throw away the
# API_STATUS the function assigns and leave the `else` branch below reading a
# stale value from whatever earlier call last ran in this shell.
if api_get "/tenants/me" >"${me_body}"; then
  me="$(cat "${me_body}")"
  rm -f "${me_body}"
  # `.tenant.tenant_id` FIRST, because that is the shape GET /v1/tenants/me
  # actually returns: {"tenant": {...}, "principal": {...}} (routes/tenants.py
  # get_me). Reading `.tenant_id` at the top level found nothing and this
  # reported "no tenant in the response" against a response that named the
  # tenant on its second line. The flatter spellings are kept as fallbacks so
  # an older deployment still parses.
  tenant="$(jq -r '.tenant.tenant_id // .tenant_id // .id // empty' <<<"${me}")"
  email="$(jq -r '.principal.email // .email // .submitted_by // empty' <<<"${me}")"
  if [[ -n "${tenant}" ]]; then
    t_pass "tenant ${tenant}${email:+ (${email})}"
  else
    t_fail "no tenant in the response: $(printf '%s' "${me}" | redact | head -c 200)"
  fi
  # WHAT THIS STEP CAN HONESTLY ASSERT.
  #
  # It used to hardcode `*@saga.xyz`, which is a restatement of ALLOWED_DOMAINS
  # and was false by construction for the only identity that can run this
  # suite: swarm-verify is a service account, and a service account cannot be a
  # principal in a Workspace domain. ALLOWED_USERS exists precisely so that
  # identity is admitted, so the suite was failing the platform for doing what
  # it was configured to do.
  #
  # The property worth testing is different and stronger: the API must
  # attribute the caller to the identity the caller actually IS. A control
  # plane that resolved a caller to somebody else's identity would hand them
  # somebody else's tenant, secrets and GCS prefix -- and every other assertion
  # in this suite would still pass.
  actual_identity="$(metadata_identity)"
  if [[ -z "${email}" ]]; then
    t_fail "the API reported no identity for an authenticated caller"
  elif [[ -z "${actual_identity}" ]]; then
    # Running outside Cloud Run: there is nothing to compare against, and
    # inventing a comparison would be worse than declining one.
    t_pass "identity ${email} reported (not running on Cloud Run; no independent identity to compare)"
  elif [[ "${email}" == "${actual_identity}" ]]; then
    t_pass "the API attributed this caller to ${email}, which is who it runs as"
  else
    t_fail "the API attributed this caller to ${email} but it runs as ${actual_identity}"
  fi
else
  rm -f "${me_body}"
  t_fail "GET ${API_PREFIX}/tenants/me -> HTTP ${API_STATUS}"
  tenant=""
fi

# ---------------------------------------------------------------------------
t_case "An unknown runner profile is rejected (invariant 10)"
if api_post "/tasks" '{"runner_profile":"definitely-not-a-profile","input":{}}' >/dev/null 2>&1; then
  t_fail "the API accepted an unknown runner profile"
else
  # 401/403 sit inside the 4xx window but prove nothing about profile
  # validation -- they are an expired token or a missing IAM binding. Only
  # 400/422 demonstrate the profile catalogue actually rejected the name.
  case "${API_STATUS}" in
    400|422) t_pass "rejected with HTTP ${API_STATUS}" ;;
    401|403) t_fail "got HTTP ${API_STATUS} -- that is an auth/permission failure, not proof the unknown profile was validated" ;;
    *)       t_fail "expected HTTP 400 or 422, got HTTP ${API_STATUS}" ;;
  esac
fi

# ---------------------------------------------------------------------------
t_case "Caller-supplied image and command are not honoured (invariant 10)"
injected='{"runner_profile":"mock","input":{},"image":"attacker/evil:latest","command":["/bin/sh","-c","id"],"resource_class":"large"}'
evil_body="$(mktemp "${TMPDIR:-/tmp}/swarm-smoke-evil.XXXXXX")"
# api_post is called directly (redirected to a file) so API_STATUS is set in
# THIS shell, not a subshell -- and jq runs afterward, as its own command, so
# a curl/API failure can no longer be masked by jq's own exit status the way
# `api_post ... | jq ...` would mask it under pipefail (the pipeline's status
# is the rightmost command's).
if api_post "/tasks" "${injected}" >"${evil_body}"; then
  evil_id="$(jq -r '.id // .task_id // empty' <"${evil_body}")"
  rm -f "${evil_body}"
  if [[ -n "${evil_id}" ]]; then
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
    t_fail "the API accepted the request (HTTP ${API_STATUS}) but the response carried no task id"
  fi
else
  rm -f "${evil_body}"
  # Rejecting the whole request outright is equally correct -- but only a
  # clean 4xx is evidence of that. A 5xx or a transport failure (API_STATUS=0)
  # proves nothing about whether the injected fields were honoured; the API
  # may equally have accepted them and then crashed.
  if [[ "${API_STATUS}" -ge 400 && "${API_STATUS}" -lt 500 ]]; then
    t_pass "request carrying an image/command was rejected outright (HTTP ${API_STATUS})"
  else
    t_fail "request carrying an image/command failed with HTTP ${API_STATUS}, not a clean 4xx rejection"
  fi
fi

# ---------------------------------------------------------------------------
t_case "Baseline capacity before submitting"
BASE_HOLDING="$(holding_capacity)"
BASE_LEASES="$(active_leases)"
t_info "tasks holding capacity: ${BASE_HOLDING}, active leases: ${BASE_LEASES}"
t_pass "baseline captured"

# ---------------------------------------------------------------------------
# EVERY BACKEND, NOT EVERY PROFILE.
#
# This suite ran one `mock` task and called the platform proven. `mock`
# resolves to CLOUD_RUN_JOB, and so do claude-code, codex and generic --
# `browser` is the ONLY profile whose resolved backend is GKE_AUTOPILOT, a
# different API, a different permission and a different authorisation model.
# So a green smoke test said nothing whatsoever about half the dispatch paths.
#
# It said nothing for two days while GKE was totally broken: seven browser
# tasks, seven failures, 403 on every one. `make smoke` passed throughout.
#
# The profiles below are therefore chosen to cover the BACKENDS, and the list
# is derived from the frozen catalogue rather than typed out, so a profile that
# moves to a new backend is covered the day it moves instead of the day someone
# remembers this file exists.
backends_to_cover() {
  "${PYTHON_BIN:-python3}" - <<'PY'
import sys
sys.path.insert(0, "apps/common")
from swarm_common.profiles import RUNNER_PROFILES
seen = {}
for name, p in RUNNER_PROFILES.items():
    if not getattr(p, "available", True):
        continue
    seen.setdefault(getattr(p.backend, "value", str(p.backend)), name)
print(" ".join(f"{b}:{n}" for b, n in sorted(seen.items())))
PY
}

COVER="$(backends_to_cover 2>/dev/null || true)"
if [[ -z "${COVER}" ]]; then
  t_fail "could not read the runner-profile catalogue; this suite cannot know which backends exist"
  t_info "without it a green run proves only that SOME backend works, which is what let GKE fail unseen"
else
  t_info "backends to cover: ${COVER}"
fi

for pair in ${COVER}; do
  BACKEND="${pair%%:*}"
  BPROFILE="${pair##*:}"
  t_case "Backend ${BACKEND}: submit a ${BPROFILE} task and run it to completion"
  B_RUN_ID="$(test_run_id)"
  if ! B_TASK_ID="$(submit_task "${BPROFILE}" \
      "$(jq -nc --arg r "${B_RUN_ID}" '{message:"smoke", run_id:$r}')" \
      '{"priority":10,"metadata":{"source":"smoke-test-backend"}}')"; then
    t_fail "${BACKEND}: submission failed for profile ${BPROFILE}"
    continue
  fi
  t_info "${BACKEND}: task ${B_TASK_ID}"
  if b_final="$(wait_for_state "${B_TASK_ID}" "SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED" "${TIMEOUT}")"; then
    assert_eq "SUCCEEDED" "${b_final}" "${BACKEND}: terminal state"
    if [[ "${b_final}" != "SUCCEEDED" ]]; then
      # The dispatch error is the whole diagnosis for a backend that cannot
      # start work at all, and it is one field away. Printing it here is the
      # difference between "browser failed" and "403 on jobs.batch in
      # swarm-tenant-eng", which is what took two days to find by hand.
      t_info "${BACKEND}: last_error: $(task_field "${B_TASK_ID}" '.last_error // "none"')"
      t_info "${BACKEND}: attempts:   $(task_field "${B_TASK_ID}" '.attempt_count // 0 | tostring')"
    fi
  else
    t_fail "${BACKEND}: no terminal state within ${TIMEOUT}s (stuck at ${b_final})"
    t_info "${BACKEND}: last_error: $(task_field "${B_TASK_ID}" '.last_error // "none"')"
  fi
done

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
  OBJ_PREFIX="tenants/${TENANT_ID}/tasks/${TASK_ID}/"
  PREFIX="gs://${ARTIFACT_BUCKET}/${OBJ_PREFIX}"
  ls_err="$(mktemp "${TMPDIR:-/tmp}/swarm-smoke-ls.XXXXXX")"
  # REST rather than `gcloud storage ls`, for the same reason as above. The
  # distinction that mattered with gcloud still matters and is now simpler to
  # express: the JSON API returns 200 with `items` ABSENT for an empty prefix,
  # so zero objects is an ANSWER, while a denied listing or an expired session
  # is an error body that fails this helper.
  if COUNT="$(gcs_object_count "${ARTIFACT_BUCKET}" "${OBJ_PREFIX}" 2>"${ls_err}")"; then
    rm -f "${ls_err}"
    if [[ "${COUNT}" -eq 0 ]]; then
      t_info "no objects under ${PREFIX} (a mock run may legitimately produce none)"
    fi
    t_pass "${COUNT} object(s) under ${PREFIX}"
  else
    err_detail="$(cat "${ls_err}")"
    rm -f "${ls_err}"
    die_if_auth_failure "${err_detail}"
    t_fail "could not list ${PREFIX}: $(printf '%s' "${err_detail}" | redact | head -n 1)"
  fi
else
  t_fail "task has no tenant_id"
fi

# ---------------------------------------------------------------------------
t_case "No credential material in the API response"
detail_body="$(mktemp "${TMPDIR:-/tmp}/swarm-smoke-detail.XXXXXX")"
# `|| true` here used to swallow any fetch failure (401/403/500/timeout) into
# an empty DETAIL, which trivially fails to match the key pattern below and
# reads as a PASS for a security invariant that was never actually checked.
if api_get "/tasks/${TASK_ID}" >"${detail_body}"; then
  DETAIL="$(cat "${detail_body}")"
  rm -f "${detail_body}"
  if printf '%s' "${DETAIL}" | grep -Eq 'sk-ant-[A-Za-z0-9]|sk-[A-Za-z0-9]{20}|"ANTHROPIC_API_KEY"[[:space:]]*:[[:space:]]*"[^"]'; then
    t_fail "the task response contains something that looks like a key"
  else
    t_pass "no key-shaped strings in the task response"
  fi
else
  rm -f "${detail_body}"
  t_fail "GET ${API_PREFIX}/tasks/${TASK_ID} -> HTTP ${API_STATUS}; could not inspect the response for credential material"
fi

[[ "${KEEP}" -eq 1 ]] || cancel_all "${TASK_ID}"
t_summary
