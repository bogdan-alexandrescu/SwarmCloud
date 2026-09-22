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
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# --parallel IS THE NUMBER OF CONTENDERS, so one of them is not a race and zero
# is not a number this script can act on. Refused here rather than discovered
# later, and there is a second reason to refuse zero specifically:
#
#     $ seq 1 0        # GNU coreutils (the swarm-verify image)
#     $ seq 1 0        # BSD (macOS, which CLAUDE.md names as a target)
#     1
#     0
#
# BSD seq counts DOWN when the first argument exceeds the last, so the
# submission loop below runs TWICE for `--parallel 0` on a laptop and not at
# all in CI. Measured on this workstation on 2026-09-22. Nothing downstream
# could tell the difference, because the loop's body does not mention `i`
# except as a label.
[[ "${PARALLEL}" =~ ^[0-9]+$ ]] || die "--parallel must be a whole number, not '${PARALLEL}'"
[[ "${PARALLEL}" -ge 2 ]] || die "--parallel must be at least 2: ${PARALLEL} task(s) cannot race for a slot"
[[ "${SLOT_LIMIT}" =~ ^[0-9]+$ ]] || die "--limit must be a whole number, not '${SLOT_LIMIT}'"
[[ "${SLOT_LIMIT}" -lt "${PARALLEL}" ]] || die "--limit ${SLOT_LIMIT} is not below --parallel ${PARALLEL}: every task would be admitted and nothing would contend"

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

# `restored` USED TO BE PRINTED WHETHER OR NOT ANYTHING WAS RESTORED, and the
# in-VPC run on 2026-09-22 is the proof. The narrowing PATCH was refused 403
# (the verify identity holds roles/datastore.viewer, so it cannot write
# Firestore at all), the script died on it under `set -e`, this trap fired, the
# restoring PATCH was refused 403 by exactly the same IAM -- and the log said
#
#     ==> restored runner:mock hard_limit to 20
#
# Two separate lies in one line. Nothing had been narrowed, so nothing needed
# restoring; and the write that would have restored it failed too. `>/dev/null
# 2>&1 || true` discarded both the error and the status, which is the defect
# class this repository keeps producing: a guard whose failure is
# indistinguishable from the condition it checks.
#
# It matters beyond the log. The case this line exists for is the one where the
# narrow SUCCEEDED and the restore then failed -- a 429, a dropped connection, a
# role revoked mid-run. Then runner:mock is left pinned at one slot on a live
# deployment, every mock task after this run serialises behind a single lease,
# and the only record of it says it was put back.
#
# So: say nothing when nothing was changed, verify the restore by READING the
# pool back rather than trusting the write's status, and when it did not take,
# print the exact command that fixes it.
NARROWED=0
RESTORED=0

# THREE ANSWERS, NOT TWO: the current ceiling, `absent`, or `unreadable`.
#
# Collapsing the last two is how a restore reports success over a pool it never
# looked at -- `pool_doc` prints the empty string when the GET is refused, and
# comparing that against an unset ORIGINAL_LIMIT is true. Keeping them apart is
# what lets the caller say "the pool is fine" and "I could not tell" as
# different sentences.
_pool_hard_limit() {
  local doc
  doc="$(pool_doc "${POOL_NAME}" 2>/dev/null)" || { printf 'unreadable'; return 0; }
  if [[ -z "${doc}" ]]; then
    printf 'unreadable'
  elif [[ "${doc}" == "null" ]]; then
    printf 'absent'
  else
    jq -r '.hard_limit // "absent"' <<<"${doc}" | tr -d '\n'
  fi
}

restore() {
  # Idempotent: the INT and TERM handlers below call this and then exit, which
  # fires the EXIT trap and would otherwise run the whole thing a second time --
  # re-cancelling tasks and re-reporting a restore that already happened.
  [[ "${RESTORED}" -eq 1 ]] && return 0
  RESTORED=1

  # THE VERDICT COMES FROM THE POOL, NOT FROM THE WRITE. Whether this suite is
  # finished with the platform is a question about what the pool document says
  # now; the status of the PATCH meant to change it is at best evidence.
  # Reading first also keeps the common case silent: when the narrowing write
  # was itself refused there is nothing to undo, and a restoring write refused
  # by the same IAM would print an error about a problem that does not exist.
  local before="" after=""
  if [[ "${NARROWED}" -eq 1 ]]; then
    before="$(_pool_hard_limit)"
    if [[ "${POOL_EXISTED}" -eq 1 ]]; then
      if [[ "${before}" == "${ORIGINAL_LIMIT}" ]]; then
        : # Already where it belongs -- the narrow never landed. Say nothing.
      else
        fs_patch "pools/${POOL_NAME}" "hard_limit,enabled,updated_at" \
          "$(jq -nc --argjson l "${ORIGINAL_LIMIT:-0}" --argjson e "${ORIGINAL_ENABLED}" --arg t "$(iso_now)" \
            '{hard_limit:{integerValue:($l|tostring)},enabled:{booleanValue:$e},updated_at:{timestampValue:$t}}')" \
          >/dev/null 2>&1 || true
        after="$(_pool_hard_limit)"
        if [[ "${after}" == "${ORIGINAL_LIMIT}" ]]; then
          info "restored ${POOL_NAME} hard_limit to ${ORIGINAL_LIMIT} (confirmed by readback)"
        else
          err "COULD NOT RESTORE ${POOL_NAME}. It is still narrowed, and every ${PROFILE} task"
          err "on this deployment will serialise behind ${SLOT_LIMIT} slot(s) until it is put back."
          err "  hard_limit now reads: ${after}    it should be: ${ORIGINAL_LIMIT}"
          err "  fix with: scripts/pool-limit.sh --pool ${POOL_NAME} --limit ${ORIGINAL_LIMIT}"
        fi
      fi
    else
      if [[ "${before}" == "absent" ]]; then
        : # The pool this run would have created is not there. Nothing to remove.
      else
        fs_delete "pools/${POOL_NAME}" >/dev/null 2>&1 || true
        after="$(_pool_hard_limit)"
        if [[ "${after}" == "absent" ]]; then
          info "removed the temporary ${POOL_NAME} pool"
        else
          err "COULD NOT REMOVE the temporary ${POOL_NAME} pool (hard_limit ${after})."
          err "  An unconfigured pool is unlimited; this one is not, so it now caps ${PROFILE}."
          err "  fix by deleting the document pools/${POOL_NAME}"
        fi
      fi
    fi
  fi
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
}

# INT and TERM RE-RAISE rather than returning into the suite.
#
# `trap restore EXIT INT TERM` ran the handler and then RESUMED at the next
# statement: a Ctrl-C during the sampling loop put the ceiling back to its
# original value, cancelled every task, and then carried on asserting "the
# narrowed pool is never over its limit" against a pool that was no longer
# narrowed and had no work left in it. An interrupted run could finish green.
# Exiting from the handler is what makes a mid-suite die a failure.
trap restore EXIT
trap 'restore; exit 130' INT
trap 'restore; exit 143' TERM

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
#
# ARMED BEFORE THE WRITE, not after. A PATCH that times out may still have been
# applied by the server, so "the write failed" is not the same as "nothing
# changed"; only a read settles it, and `restore` above does exactly that. Set
# afterwards, an ambiguous write would leave the pool narrowed and the trap
# would skip it.
NARROWED=1
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

# THE READBACK IS THE TEST, AND IT HAS TO BE FATAL.
#
# This was `assert_eq "${SLOT_LIMIT}" "$(pool_doc ... | jq -r '.hard_limit')"`,
# which is wrong twice over.
#
# 1. `assert_eq` RECORDS a failure and returns. The suite then submitted its
#    tasks anyway and case 3 sampled a pool still sitting at its original
#    ceiling, where `assert_le "${WORST}" 1` is satisfied by any quiet moment --
#    so the one assertion in this repository that exists to catch
#    oversubscription reported PASS having created no contention at all. A red
#    summary line does not retract a green assertion; the assertion is the part
#    a person quotes.
# 2. It compared `hard_limit`, and admission does not. `effective_limit` is
#    min(hard_limit, adaptive_target, quota_derived_limit) -- the definition is
#    swarm_common.models.SlotPool.effective_limit, restated once in FS_JQ. A
#    pool whose quota_derived_limit is 0 because the provider is exhausted has
#    an effective ceiling of 0: nothing is ever admitted, `WORST` stays 0, and
#    the hard_limit check is perfectly happy. Zero contention, full marks.
#
# The command substitution is also captured and CHECKED here rather than passed
# as an argument. As an argument its exit status is discarded -- set -e cannot
# fire there -- so a 403, a 429 or a dropped connection on the read arrived as
# the empty string and was compared as though it were a measurement. testlib.sh
# carries the same note on assert_le/assert_ge; this is that trap one function
# over.
NARROW_DOC=""
if ! NARROW_DOC="$(pool_doc "${POOL_NAME}")"; then
  t_fatal "could not read ${POOL_NAME} back after narrowing it; the error is above. An unread ceiling is not a narrowed one."
fi
if [[ -z "${NARROW_DOC}" || "${NARROW_DOC}" == "null" ]]; then
  t_fatal "${POOL_NAME} does not exist after the write that was supposed to create it"
fi
NARROW_EFFECTIVE="$(printf '%s' "${NARROW_DOC}" | jq -r "${FS_JQ} effective_limit")"
if [[ "${NARROW_EFFECTIVE}" != "${SLOT_LIMIT}" ]]; then
  t_info "hard_limit=$(printf '%s' "${NARROW_DOC}" | jq -r '.hard_limit // "unset"') adaptive_target=$(printf '%s' "${NARROW_DOC}" | jq -r '.adaptive_target // "unset"') quota_derived_limit=$(printf '%s' "${NARROW_DOC}" | jq -r '.quota_derived_limit // "unset"')"
  t_fatal "${POOL_NAME} effective_limit is ${NARROW_EFFECTIVE}, not ${SLOT_LIMIT}: there is nothing for the tasks below to race for"
fi
t_pass "${POOL_NAME} effective_limit: ${NARROW_EFFECTIVE}"

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
# Separate from the assertion above, and fatal rather than advisory. That one is
# a quality bar -- one rejected submission out of twelve is worth reporting and
# is not a reason to stop. This is the floor below which the word "race" stops
# meaning anything: with fewer than two tasks in flight there is no last slot to
# contend for, and every case below would report a property of an idle platform
# while calling it a race.
if [[ "${#TASK_IDS[@]}" -lt 2 ]]; then
  t_fatal "only ${#TASK_IDS[@]} task(s) were accepted; two contenders are the minimum for a race"
fi

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
# The assertion above is satisfied by a platform that admitted nothing at all,
# and a race test that never contended is worse than one that fails: it
# certifies the property it was unable to test. `WORST` is the peak `active`
# this run actually observed on the narrowed pool, so requiring at least one is
# requiring that some task really did hold the slot the others were queued
# behind.
#
# One, not SLOT_LIMIT: every sample is taken from outside the admission
# transaction, so demanding that a 1Hz sampler catch the pool exactly full would
# be a race of its own. Holding the slot at all is what proves the contention
# was real.
assert_ge "${WORST}" 1 "the narrowed pool was actually contended (peak active)"

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
