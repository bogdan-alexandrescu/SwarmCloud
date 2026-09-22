#!/usr/bin/env bash
# Races: the last free slot, simultaneous cancellation, and stale generations.
#
# These are the failure modes that a single-threaded test never finds and that
# production finds immediately:
#
#   * two schedulers both admitting into the last slot -> the limit is exceeded
#     transiently and the platform oversubscribes;
#   * a task cancelled at the same moment it is dispatched -> either a running
#     agent nobody is watching, or a lease nobody releases;
#   * a retry whose predecessor is still alive -> two agents writing the same
#     workspace. Fencing generations exist to make the stale one exit without
#     running (CONTRACT.md invariant 5).
#
# The last-slot test narrows a NARROW pool (runner:<profile>), never the global
# pool, so the rest of the platform keeps working while it runs. The original
# limit is restored on every exit path.
#
# ---------------------------------------------------------------------------
# THIS SUITE CANNOT RUN IN-VPC TODAY, AND THE REASON IS A DECISION NOBODY HAS
# TAKEN YET.
# ---------------------------------------------------------------------------
#
# Narrowing the pool is a WRITE. `swarm-verify` -- the identity that runs the
# in-VPC gate, because swarm-api's ingress refuses a laptop -- holds
# roles/datastore.viewer (terraform/infra/verify.tf), so `fs_patch` below is
# refused and this is the one target of the four that fails. It is not broken;
# it is unauthorised, and which authority to grant is a security question with
# three real answers. They are written out here, next to the code that needs the
# permission, rather than in a document somebody has to find.
#
# terraform/ is TRACK C. Whichever option is chosen, the grant is Track C's to
# make; this comment is a request, not a change.
#
# OPTION A -- grant swarm-verify a Firestore write role.
#   Cost: Firestore IAM cannot scope below the DATABASE. This repository already
#   established that and pays for it elsewhere: Firestore does not evaluate IAM
#   Conditions on the data plane (tests/integration/test_register_tenant_grants.py),
#   so there is no such thing as a grant scoped to `pools/*`. roles/datastore.user
#   therefore means create, update and DELETE on every document in the `swarm`
#   database -- every tenant's tasks, leases, attempts and secrets metadata, and
#   the `pools/*` documents the whole platform admits against. It is also
#   unattributed: a direct REST PATCH writes no event, increments no metric and
#   names no actor, so nothing afterwards can say the test did it.
#   And it keeps alive the exact capability this file's own comment (below, at
#   "Narrow ${POOL_NAME}") says must never be exercised: writing `active`
#   outside the admission transaction, which silently inflates capacity on a
#   live deployment and makes the oversubscription assertion pass on a platform
#   that is oversubscribed.
#
# OPTION B (RECOMMENDED) -- make swarm-verify an admin and narrow through
#   `PUT /v1/admin/limits/runner/{profile}`.
#   The capability is shaped like the operation, in four ways that A is not:
#     * BOUNDED. `LimitRequest.limit` is `ge=0, le=100000` and `_check_runner`
#       refuses any profile not in the frozen catalogue, so the request cannot
#       name a pool that does not exist.
#     * INCAPABLE OF THE DANGEROUS WRITE. `Store.upsert_pool` sets `hard_limit`
#       and `enabled` and touches nothing else -- there is no value of this
#       request that writes `active`. The field this file warns about becomes
#       unreachable rather than merely unwritten.
#     * ATTRIBUTED AND AUDITED. The route runs under `admin_auth`, the actor is
#       the verified identity from the token, and it increments
#       `admin_actions{action="limit_runner"}`.
#     * ALREADY THE SUPPORTED PATH. It is what an operator uses during an
#       incident, so the test exercises the interface the platform actually
#       offers instead of reaching behind it.
#   Cost, stated rather than minimised: `admin_auth` is ONE boolean. Admin is
#   not scoped to one pool, so this identity would also gain pause/resume
#   dispatch, provider and resource drains, tenant limits, and the admin read
#   surfaces. That is a genuine widening. It is still far narrower than A --
#   every one of those operations is bounded, validated and recorded, none of
#   them reads a secret or another tenant's artifact, and all of them are
#   reversible through the same API. The grant is one line:
#   `admin_users` in terraform/environments/<env>/<env>.tfvars gaining
#   `swarm-verify@<project>.iam.gserviceaccount.com`, which
#   terraform/infra/locals.tf already wires to ADMIN_USERS. Grant it in dev and
#   staging; there is no reason for the gate to be an admin in prod.
#
# OPTION C -- add no permission and race the pool at its real limit.
#   Submit `effective_limit + N` tasks and race those. Needs no grant at all,
#   and is the wrong trade here: the limit on a real deployment is large, so
#   forcing contention means saturating a pool with test work in a project
#   SHARED with another team, and the contention window stops being
#   deterministic -- which is the whole property this suite exists to pin.
#
# Until one is chosen, `make verify-remote` reports race-test as FAILED. That is
# the correct reading: the property is unverified. Do not make it skip.
#
# Usage: scripts/race-test.sh [--profile mock] [--parallel 12] [--timeout 300]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="race"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

PROFILE="mock"
PARALLEL=12
TIMEOUT=300
SLOT_LIMIT=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)  PROFILE="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --limit)    SLOT_LIMIT="$2"; shift 2 ;;
    --timeout)  TIMEOUT="$2"; shift 2 ;;
    # Through the permission section: an operator running --help on a target
    # that fails needs to read WHY it fails, not just what its flags are.
    -h|--help)  sed -n '2,89p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Race conditions: ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform

POOL_NAME="runner:${PROFILE}"
TASK_IDS=()
POOL_EXISTED=0
ORIGINAL_LIMIT=""
ORIGINAL_ENABLED="true"

ORIGINAL_POOL="$(pool_doc "${POOL_NAME}")"
if [[ "${ORIGINAL_POOL}" != "null" && -n "${ORIGINAL_POOL}" ]]; then
  POOL_EXISTED=1
  ORIGINAL_LIMIT="$(jq -r '.hard_limit // 0' <<<"${ORIGINAL_POOL}")"
  ORIGINAL_ENABLED="$(jq -r 'if .enabled == false then "false" else "true" end' <<<"${ORIGINAL_POOL}")"
fi

restore() {
  if [[ "${POOL_EXISTED}" -eq 1 ]]; then
    fs_patch "pools/${POOL_NAME}" "hard_limit,enabled,updated_at" \
      "$(jq -nc --argjson l "${ORIGINAL_LIMIT:-0}" --argjson e "${ORIGINAL_ENABLED}" --arg t "$(iso_now)" \
        '{hard_limit:{integerValue:($l|tostring)},enabled:{booleanValue:$e},updated_at:{timestampValue:$t}}')" \
      >/dev/null 2>&1 || true
    info "restored ${POOL_NAME} hard_limit to ${ORIGINAL_LIMIT}"
  else
    fs_delete "pools/${POOL_NAME}" 2>/dev/null || true
    info "removed the temporary ${POOL_NAME} pool"
  fi
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
}
trap restore EXIT INT TERM

# ---------------------------------------------------------------------------
t_case "Narrow ${POOL_NAME} to ${SLOT_LIMIT} slot(s)"
#
# `active` IS NOT IN THE FIELD MASK, and must never be.
#
# It used to be: this read `active`, then wrote it back in a plain PATCH,
# milliseconds before launching the concurrent submissions this test exists to
# race. Firestore's REST PATCH is not transactional, so an admission committing
# between the read and the write was silently clobbered -- the pool's `active`
# dropped below its true value and capacity was inflated on a live deployment.
#
# Worse, it broke the test's own purpose: the clobber lowers `active`, so the
# "narrowed pool is never over its limit" assertion below would PASS on a
# platform that was genuinely oversubscribed. The one test meant to catch
# oversubscription could mask it.
#
# swarm_common.models.SlotPool says `active` is mutated ONLY inside the admission
# and release transactions. Every sibling script honours that -- register-tenant
# patches only hard_limit, pause-swarm only enabled -- and so does this now. A
# pool that does not exist yet is created with active=0, which is safe because
# no lease can be holding a pool document that has never existed.
if [[ "${POOL_EXISTED}" -eq 1 ]]; then
  fs_patch "pools/${POOL_NAME}" "hard_limit,enabled,updated_at" \
    "$(jq -nc --argjson l "${SLOT_LIMIT}" --arg t "$(iso_now)" \
      '{hard_limit:{integerValue:($l|tostring)},enabled:{booleanValue:true},
        updated_at:{timestampValue:$t}}')"
else
  fs_patch "pools/${POOL_NAME}" "name,hard_limit,active,enabled,updated_at" \
    "$(jq -nc --arg n "${POOL_NAME}" --argjson l "${SLOT_LIMIT}" --arg t "$(iso_now)" \
      '{name:{stringValue:$n},hard_limit:{integerValue:($l|tostring)},
        active:{integerValue:"0"},enabled:{booleanValue:true},updated_at:{timestampValue:$t}}')"
fi
assert_eq "${SLOT_LIMIT}" "$(pool_doc "${POOL_NAME}" | jq -r '.hard_limit')" "${POOL_NAME} hard_limit"

# ---------------------------------------------------------------------------
t_case "${PARALLEL} tasks race for ${SLOT_LIMIT} slot(s)"
SUBMIT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarm-race.XXXXXX")"
for i in $(seq 1 "${PARALLEL}"); do
  (
    # submit_task already prints its own diagnosis (the HTTP status and the
    # redacted response body) to stderr on failure. Capture it per-submission
    # instead of routing it to /dev/null: `|| true` here only keeps one failed
    # background submission from aborting the others, it must not also erase
    # the one place the cause is recorded.
    submit_task "${PROFILE}" "$(jq -nc --argjson i "${i}" '{message:"race", index:$i, sleep_seconds:20}')" \
      '{"metadata":{"source":"race-test"}}' >"${SUBMIT_DIR}/${i}.id" 2>"${SUBMIT_DIR}/${i}.err" || true
  ) &
done
wait
for f in "${SUBMIT_DIR}"/*.id; do
  [[ -f "${f}" ]] || continue
  id="$(cat "${f}")"
  [[ -n "${id}" ]] && TASK_IDS+=("${id}")
done
FAILED_SUBMISSIONS=$(( PARALLEL - ${#TASK_IDS[@]} ))
if [[ "${FAILED_SUBMISSIONS}" -gt 0 ]]; then
  t_info "${FAILED_SUBMISSIONS} of ${PARALLEL} submission(s) failed; first failure below"
  for f in "${SUBMIT_DIR}"/*.err; do
    [[ -s "${f}" ]] || continue
    redact <"${f}" | head -n 5 | sed 's/^/       /' >&2
    break
  done
fi
rm -rf "${SUBMIT_DIR}"
assert_ge "${#TASK_IDS[@]}" "$(( PARALLEL - 1 ))" "tasks accepted concurrently"

# ---------------------------------------------------------------------------
t_case "The narrowed pool is never over its limit, at any sample"
DEADLINE=$(( $(date -u +%s) + TIMEOUT ))
WORST=0
SAMPLES=0
while [[ "$(date -u +%s)" -lt "${DEADLINE}" ]]; do
  active="$(pool_active "${POOL_NAME}")"
  [[ "${active}" -gt "${WORST}" ]] && WORST="${active}"
  SAMPLES=$(( SAMPLES + 1 ))
  if [[ "${active}" -gt "${SLOT_LIMIT}" ]]; then
    t_info "OVER LIMIT: ${POOL_NAME} active=${active} limit=${SLOT_LIMIT}"
    break
  fi
  done_count=0
  for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
    task_is_terminal "${id}" && done_count=$(( done_count + 1 ))
  done
  [[ "${done_count}" -eq "${#TASK_IDS[@]}" ]] && break
  sleep 1
done
t_info "${SAMPLES} samples, worst observed active=${WORST}"
assert_le "${WORST}" "${SLOT_LIMIT}" "peak concurrent leases on ${POOL_NAME}"

# ---------------------------------------------------------------------------
t_case "Every attempt carries a distinct, increasing generation"
BAD_GENERATIONS=0
CHECKED=0
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  # fs_query fails loudly on its own (fs_request -> fs_query propagate a
  # non-2xx as a non-zero exit, and this is a bare statement under
  # `set -o pipefail`, not an `if`/`while` condition, so that status is not
  # suppressed): a denied or expired query aborts the script here with the
  # real diagnosis already printed, rather than reading as zero attempts.
  # Only jq's own stderr was ever routed to /dev/null; keep it visible too.
  gens="$(fs_query attempts "$(fs_field_filter task_id EQUAL "$(jq -nc --arg v "${id}" '{stringValue:$v}')")" 50 \
    | jq -r "${FS_JQ} doc.generation" | sort -n | tr '\n' ' ')"
  [[ -z "${gens// /}" ]] && continue
  CHECKED=$(( CHECKED + 1 ))
  total_count="$(printf '%s' "${gens}" | tr ' ' '\n' | grep -c . || true)"
  uniq_count="$(printf '%s' "${gens}" | tr ' ' '\n' | grep . | sort -u | grep -c . || true)"
  if [[ "${uniq_count}" -ne "${total_count}" ]]; then
    BAD_GENERATIONS=$(( BAD_GENERATIONS + 1 ))
    t_info "task ${id} has duplicate generations: ${gens}"
  fi
done
t_info "checked ${CHECKED} task(s) with attempts"
assert_eq "0" "${BAD_GENERATIONS}" "tasks with duplicate attempt generations"

# ---------------------------------------------------------------------------
t_case "Simultaneous cancellation of every in-flight task"
CANCEL_PIDS=()
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  ( cancel_task "${id}" >/dev/null 2>&1 || true ) &
  CANCEL_PIDS+=($!)
done
for pid in ${CANCEL_PIDS[@]+"${CANCEL_PIDS[@]}"}; do wait "${pid}" || true; done
t_pass "cancellation requested for ${#TASK_IDS[@]} task(s) in parallel"

# ---------------------------------------------------------------------------
t_case "No lease survived the cancellation storm"
if wait_until "the ${POOL_NAME} pool to drain" 240 pool_drained "${POOL_NAME}"; then
  t_pass "${POOL_NAME} active is 0"
else
  t_fail "${POOL_NAME} still shows $(pool_active "${POOL_NAME}") active lease(s)"
fi

STUCK=0
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  state="$(task_state "${id}")"
  case "${state}" in
    SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED|QUEUED|PARKED|READY) ;;
    *) STUCK=$(( STUCK + 1 )); t_info "task ${id} is ${state}" ;;
  esac
done
assert_eq "0" "${STUCK}" "tasks still holding capacity after cancellation"

# ---------------------------------------------------------------------------
t_case "Accounting is consistent: no negative pools, none over limit"
BROKEN="$(fs_list_docs pools | jq -s "${FS_JQ}"' [ .[] | select((.active // 0) < 0 or (.active // 0) > effective_limit) | .id ] | join(", ")')"
if [[ "${BROKEN}" == '""' || -z "${BROKEN//\"/}" ]]; then
  t_pass "every pool is within [0, effective_limit]"
else
  t_fail "pools with impossible accounting: ${BROKEN}"
fi

t_summary
