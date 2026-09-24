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
# The last-slot test narrows a NARROW pool (runner:mock), never the global
# pool, so the rest of the platform keeps working while it runs. The original
# limit is restored on every exit path.
#
# ---------------------------------------------------------------------------
# HOW THIS SUITE IS ALLOWED TO NARROW A POOL -- owner decision, 2026-09-24
# ---------------------------------------------------------------------------
#
# Through the admin API, and through nothing else:
#
#     PUT /v1/admin/limits/runner/mock   {"limit": N}
#
# `swarm-verify` -- the identity the in-VPC gate runs as, because swarm-api's
# ingress refuses a laptop -- is a platform admin in dev (`admin_users` in
# terraform/environments/dev/dev.tfvars). Until then it held
# roles/datastore.viewer alone, the narrowing Firestore PATCH was refused 403,
# and this was the one target of the gate that could not run. The three
# options weighed and why this one won are in
# docs/audits/2026-09-22/race-test-needs-a-write.md. The part worth keeping
# next to the code, so nobody drifts back to a direct write:
#
#   * A Firestore write role cannot be scoped below the DATABASE. It would be
#     create/update/delete over every tenant's tasks, leases and attempts and
#     over the `pools/*` documents admission counts against -- and it would keep
#     alive the one write this file must never make: `active` outside the
#     admission transaction (see "Narrow" below for what that did once).
#   * The admin route is shaped like the operation. It refuses a pool name
#     outside the frozen catalogue, bounds the value (LimitRequest: 0..100000),
#     has no parameter that reaches `active`, runs as the verified caller, and
#     stamps that caller on the pool document as `admin_changed_by`.
#
# The cost, stated rather than minimised: admin is ONE boolean, and it opens
# every /v1/admin route, not only this one. Read from routes/admin.py and
# store.py on 2026-09-24, the gate can also:
#
#   * pause dispatch platform-wide, and set ANY ceiling, global to 0 included;
#   * drain a provider or a resource class for every tenant, and disable a
#     provider, which also rewrites every tenant's quota document for it
#     (re-enabling resets each one to AVAILABLE and clears its cooldown and
#     quota-derived cap; it does not put back what was there);
#   * WRITE TENANT DOCUMENTS. PUT /v1/admin/tenants/{id}/limits sets
#     max_active, capacity_units and `enabled` on tenants/<id>, so it can
#     DISABLE any tenant, which stops every other route for that tenant
#     (routes/accounts.py); PUT /v1/admin/limits/tenant/{id} sets max_active;
#   * rewrite the stored state of any tenant's workflows
#     (POST /v1/admin/workflows/rollup?tenant_id=);
#   * read every tenant's leases, quota and tenant record (/v1/admin/leases,
#     /quota, /tenants), which roles/datastore.viewer already lets it read
#     from Firestore directly.
#
# Nothing records a previous value, so undoing any of those needs the old
# number from somewhere else. What admin CANNOT do: write `active` on an
# existing pool, touch a tenant's service account, GCS prefix or secret names
# (set_tenant_limits patches the three fields above and nothing else), create
# or delete a tenant, delete a lease, or read or write a secret. It is granted
# in dev only. The first record of this decision said it could not touch a
# tenant document; that was wrong, and the dated correction is in the audit.
#
# FOUR REFUSALS FOLLOW FROM USING THE API, all checked before the first write,
# because in each case the API could not undo what the suite would do:
#
#   * `--profile` must be `mock`. mock has no provider, so a narrowed runner:mock
#     holds up nothing but this suite's own tasks, and the verify tenant
#     (`providers = []`) cannot run anything else anyway. Narrowing
#     runner:claude-code to one slot would serialise every tenant's real agents
#     behind a single lease on a shared, live platform.
#   * The pool must EXIST. Store.upsert_pool creates a pool it cannot find, and
#     no admin route deletes one -- so a narrow would leave behind a ceiling on a
#     pool that admission used to treat as unlimited.
#   * The pool must be ENABLED. The runner route never touches `enabled`, there
#     is no undrain route for a runner pool, and a drained pool admits nothing,
#     so there would be nothing to race for. The old PATCH silently re-enabled
#     it, undoing an operator's drain.
#   * Its ceiling must be within LimitRequest's bound (API_LIMIT_MAX below), or
#     the narrow succeeds and the restoring PUT is a 422.
#
# The restore goes through the same route, runs from the EXIT trap on every path
# (INT and TERM re-raise), and reports what the pool READS afterwards rather
# than what the write returned.
#
# OBSERVATIONS still read Firestore directly, as in every suite
# (scripts/lib/testlib.sh): a broken API must not be able to report its own
# success, so the readback that decides whether the narrow landed is not the
# PUT's response body.
#
# Usage: scripts/race-test.sh [--parallel 12] [--limit 1] [--timeout 300]
#        --profile is still accepted, and anything but `mock` is refused.

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

# The largest ceiling PUT /v1/admin/limits/* accepts: `LimitRequest.limit` is
# `Field(ge=0, le=100_000)` in apps/swarm-api/swarm_api/schemas.py. A MIRRORED
# VALUE, and tests/unit/scripts/test_race_test_limit_bound.py fails the moment
# the two disagree -- a stale copy here would either refuse a restorable pool or,
# worse, narrow one whose restore the API then rejects.
API_LIMIT_MAX=100000

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)  PROFILE="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    --limit)    SLOT_LIMIT="$2"; shift 2 ;;
    --timeout)  TIMEOUT="$2"; shift 2 ;;
    # Through the permission section: an operator running --help needs to read
    # HOW this suite is allowed to change a live ceiling, not just its flags.
    # Up to `set -euo pipefail` rather than a line number: the header has grown
    # twice, and a fixed range silently cuts off whatever was added last.
    -h|--help)  sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d'; exit 0 ;;
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

# Before require_platform, so a refused profile costs no request at all. See the
# header for why this is a refusal and not a warning.
if [[ "${PROFILE}" != "mock" ]]; then
  die "race-test only runs against the mock profile, not '${PROFILE}': it narrows runner:${PROFILE} to ${SLOT_LIMIT} slot(s) on a live, shared platform, and every other profile is backed by a provider whose tenants' real agents would queue behind that slot for the whole run"
fi

step "Race conditions: ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform

POOL_NAME="runner:${PROFILE}"
# Relative to API_PREFIX, which api_send prepends -- and exactly what
# scripts/api.sh takes, so the fix printed below can be pasted as it stands.
LIMIT_ROUTE="/admin/limits/runner/${PROFILE}"
TASK_IDS=()

# ---------------------------------------------------------------------------
# What the pool is BEFORE anything changes, and whether the API could put it
# back. Every refusal here happens before the first write, so a refused run
# leaves the platform exactly as it found it.
# ---------------------------------------------------------------------------
if ! ORIGINAL_POOL="$(pool_doc "${POOL_NAME}")"; then
  die "could not read pools/${POOL_NAME}; the Firestore error is above. The ceiling this suite restores has to be known before it is changed."
fi
if [[ -z "${ORIGINAL_POOL}" || "${ORIGINAL_POOL}" == "null" ]]; then
  die "${POOL_NAME} has no pool document. Provisioning creates pools (terraform/modules/firestore); this suite does not. The admin API would CREATE it, and no admin route can delete it afterwards, so the narrow could not be undone."
fi
ORIGINAL_LIMIT="$(jq -r '.hard_limit // empty' <<<"${ORIGINAL_POOL}")"
[[ "${ORIGINAL_LIMIT}" =~ ^[0-9]+$ ]] \
  || die "${POOL_NAME} has no readable hard_limit ('${ORIGINAL_LIMIT}'), so there is nothing to restore it to"
[[ "${ORIGINAL_LIMIT}" -le "${API_LIMIT_MAX}" ]] \
  || die "${POOL_NAME} hard_limit is ${ORIGINAL_LIMIT}, above the ${API_LIMIT_MAX} the admin API accepts. The narrow would land and its restore would be refused 422, leaving the pool at ${SLOT_LIMIT} slot(s). Set a ceiling the API can express first."
# `== false`, not `// true`: jq's alternative operator reads false as absent.
if [[ "$(jq -r 'if .enabled == false then "drained" else "open" end' <<<"${ORIGINAL_POOL}")" == "drained" ]]; then
  die "${POOL_NAME} is drained (enabled = false): somebody stopped admissions into it, and a drained pool admits nothing, so there is nothing to race for. The runner-limit route does not touch enabled and this suite will not undo a drain. Re-enable it deliberately, then re-run."
fi

# `restored` USED TO BE PRINTED WHETHER OR NOT ANYTHING WAS RESTORED, and the
# in-VPC run on 2026-09-22 is the proof. The narrowing PATCH was refused 403
# (the verify identity held roles/datastore.viewer, so it could not write
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
# narrow SUCCEEDED and the restore then failed -- a 429, a dropped connection,
# admin revoked mid-run by a redeploy. Then runner:mock is left pinned at one
# slot on a live deployment, every mock task after this run serialises behind a
# single lease, and the only record of it says it was put back.
#
# So: say nothing when nothing was changed, verify the restore by READING the
# pool back rather than trusting the write's status, and when it did not take,
# print the exact commands that fix it: the same admin route, which is as
# bounded and as attributed as the change it undoes, and the operator fallback
# for when that route cannot be reached (see TWO REPAIRS in `restore`).
#
# AND A RESTORE THAT DID NOT TAKE FAILS THE RUN. It used to be reported and then
# forgotten: `restore` ran from `trap restore EXIT`, printed COULD NOT RESTORE
# and returned, and an EXIT trap that does not call `exit` leaves the status
# alone (the PR #21 review measured `trap f EXIT; true` exiting 0 on /bin/bash
# 3.2.57; tests/integration/test_race_test_refuses_an_unnarrowed_pool.py's
# complete-run cases prove the consequence in CI). The last
# command was a green t_summary, so a run with every race case passing and the
# restoring PUT refused exited 0 -- and `make verify-remote` printed
# `ok race-test` and ran e2e-test against a single mock slot, where the failure
# surfaced as someone else's timeout. A live pool left narrowed is the one thing
# this suite promised not to do; the run that does it is a failed run.
NARROWED=0
RESTORED=0
RESTORE_RC=0

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

# _set_limit N OUTFILE -- the one write this suite makes, in one place.
#
# api_send is called DIRECTLY, not inside "$(...)": a command substitution runs
# in a subshell, and the API_STATUS the caller reads afterwards would be
# whatever the previous request left behind (CLAUDE.md, "Writing scripts").
_set_limit() {
  api_send PUT "${LIMIT_ROUTE}" "$(jq -nc --argjson l "$1" '{limit:$l}')" "$2"
}

restore() {
  # Idempotent: the final case, the INT and TERM handlers call this, and every
  # one of them is followed by the EXIT trap, which would otherwise run the
  # whole thing a second time -- re-cancelling tasks and re-reporting a restore
  # that already happened. The SECOND call answers with the first call's
  # verdict, so the trap still learns that the pool was left narrowed.
  if [[ "${RESTORED}" -eq 1 ]]; then
    return "${RESTORE_RC}"
  fi
  RESTORED=1

  # THE VERDICT COMES FROM THE POOL, NOT FROM THE WRITE. Whether this suite is
  # finished with the platform is a question about what the pool document says
  # now; the status of the PUT meant to change it is at best evidence.
  # Reading first also keeps the common case silent: when the narrowing write
  # was itself refused there is nothing to undo, and a restoring write refused
  # for the same reason would print an error about a problem that does not
  # exist.
  local before="" after="" out="" answered=""
  if [[ "${NARROWED}" -eq 1 ]]; then
    before="$(_pool_hard_limit)"
    if [[ "${before}" != "${ORIGINAL_LIMIT}" ]]; then
      # /dev/null rather than no file: this runs in a condition (`if restore`,
      # `restore || ...`), where set -e is off, so a failed mktemp would
      # otherwise hand api_send an empty path and lose the write altogether.
      out="$(mktemp "${TMPDIR:-/tmp}/swarm-race-restore.XXXXXX")" || out=/dev/null
      # The status is KEPT, not discarded: it is the second line of the report
      # below when the readback disagrees. api_send has already printed the
      # redacted body of a refusal.
      if _set_limit "${ORIGINAL_LIMIT}" "${out}"; then
        answered="HTTP ${API_STATUS}"
      else
        answered="HTTP ${API_STATUS}, error above"
      fi
      [[ "${out}" == /dev/null ]] || rm -f "${out}"
      after="$(_pool_hard_limit)"
      if [[ "${after}" == "${ORIGINAL_LIMIT}" ]]; then
        info "restored ${POOL_NAME} hard_limit to ${ORIGINAL_LIMIT} (confirmed by readback)"
      else
        RESTORE_RC=1
        # TWO REPAIRS, because the one that matches this suite's own write
        # cannot be run with a person's credential. The front door refuses every
        # user credential (scripts/api.sh: SWARM_IMPERSONATE_SA is REQUIRED
        # there), so the admin route only works as an identity that is BOTH an
        # admin and admitted by IAP. In dev that is swarm-verify once both of
        # its grants are live: IAP through frontend_iap_members in
        # terraform/bootstrap/terraform.tfvars (#23, applied by the owner, not
        # the release) and admin through admin_users (applied by the release).
        # pool-limit.sh needs neither: it works as an operator, through a
        # Firestore updateMask that names hard_limit alone. Printing only the
        # first sent whoever read this to a 403 with nothing else on screen,
        # while every mock task serialised.
        err "COULD NOT RESTORE ${POOL_NAME}. It is still narrowed, and every ${PROFILE} task"
        err "on this deployment will serialise behind ${SLOT_LIMIT} slot(s) until it is put back."
        err "  the restoring PUT ${API_PREFIX}${LIMIT_ROUTE} answered ${answered}"
        err "  hard_limit now reads: ${after}    it should be: ${ORIGINAL_LIMIT}"
        err "  fix with either of these:"
        err "    scripts/pool-limit.sh --pool ${POOL_NAME} --limit ${ORIGINAL_LIMIT}"
        err "      as an operator who can write Firestore, from a workstation. It writes"
        err "      hard_limit alone (an updateMask), so it cannot touch active."
        err "    scripts/api.sh PUT ${LIMIT_ROUTE} '{\"limit\":${ORIGINAL_LIMIT}}'"
        err "      the admin route this suite used, which records who made the change."
        err "      Through the front door it needs SWARM_IMPERSONATE_SA naming an identity"
        err "      that is in admin_users AND admitted by IAP (frontend_iap_members in"
        err "      terraform/bootstrap/terraform.tfvars); a user credential is refused there."
      fi
    fi
  fi
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
  return "${RESTORE_RC}"
}

# INT and TERM RE-RAISE rather than returning into the suite.
#
# `trap restore EXIT INT TERM` ran the handler and then RESUMED at the next
# statement: a Ctrl-C during the sampling loop put the ceiling back to its
# original value, cancelled every task, and then carried on asserting "the
# narrowed pool is never over its limit" against a pool that was no longer
# narrowed and had no work left in it. An interrupted run could finish green.
# Exiting from the handler is what makes a mid-suite die a failure.
#
# The EXIT trap keeps a failing status and turns a passing one into a failure
# when the pool was left narrowed. It never turns 130/143 or a t_fatal's 1 into
# something else: the earlier cause is the better diagnosis. The signal
# handlers ignore restore's verdict for the same reason -- an interrupted run
# already exits non-zero -- and the EXIT trap after them re-reads it anyway.
_on_exit() {
  local rc=$?
  if ! restore && [[ "${rc}" -eq 0 ]]; then
    rc=1
  fi
  exit "${rc}"
}
trap _on_exit EXIT
trap 'restore || true; exit 130' INT
trap 'restore || true; exit 143' TERM

# ---------------------------------------------------------------------------
t_case "Narrow ${POOL_NAME} to ${SLOT_LIMIT} slot(s) through the admin API"
#
# `active` IS NEVER WRITTEN, and now CANNOT be.
#
# It used to be: this read `active`, then wrote it back in a plain Firestore
# PATCH, milliseconds before launching the concurrent submissions this test
# exists to race. Firestore's REST PATCH is not transactional, so an admission
# committing between the read and the write was silently clobbered -- the pool's
# `active` dropped below its true value and capacity was inflated on a live
# deployment.
#
# Worse, it broke the test's own purpose: the clobber lowers `active`, so the
# "narrowed pool is never over its limit" assertion below would PASS on a
# platform that was genuinely oversubscribed. The one test meant to catch
# oversubscription could mask it.
#
# swarm_common.models.SlotPool says `active` is mutated ONLY inside the admission
# and release transactions. The field mask was the first fix; the admin route is
# the second and the structural one: Store.upsert_pool has no parameter that
# reaches `active`, so there is no value of this request that can write it.
#
# ARMED BEFORE THE WRITE, not after. A PUT that times out may still have been
# applied by the server, so "the write failed" is not the same as "nothing
# changed"; only a read settles it, and `restore` above does exactly that. Set
# afterwards, an ambiguous write would leave the pool narrowed and the trap
# would skip it.
NARROWED=1
NARROW_OUT="$(mktemp "${TMPDIR:-/tmp}/swarm-race-narrow.XXXXXX")"
if ! _set_limit "${SLOT_LIMIT}" "${NARROW_OUT}"; then
  rm -f "${NARROW_OUT}"
  if [[ "${API_STATUS}" == "403" ]]; then
    # An IAP refusal has already been explained by api_request. This is the
    # other 403: swarm-api knew the caller and the caller is not an admin.
    t_info "A 403 from swarm-api here means the caller is not a platform admin."
    t_info "The gate's identity is made one through admin_users in"
    t_info "terraform/environments/${ENVIRONMENT}/${ENVIRONMENT}.tfvars (bare email, no"
    t_info "serviceAccount: prefix) and the release that applies it."
  fi
  t_fatal "could not narrow ${POOL_NAME}: PUT ${API_PREFIX}${LIMIT_ROUTE} answered HTTP ${API_STATUS}"
fi
rm -f "${NARROW_OUT}"

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
  t_fatal "${POOL_NAME} does not exist after the write that was supposed to narrow it"
fi
NARROW_EFFECTIVE="$(printf '%s' "${NARROW_DOC}" | jq -r "${FS_JQ} effective_limit")"
if [[ "${NARROW_EFFECTIVE}" != "${SLOT_LIMIT}" ]]; then
  t_info "hard_limit=$(printf '%s' "${NARROW_DOC}" | jq -r '.hard_limit // "unset"') adaptive_target=$(printf '%s' "${NARROW_DOC}" | jq -r '.adaptive_target // "unset"') quota_derived_limit=$(printf '%s' "${NARROW_DOC}" | jq -r '.quota_derived_limit // "unset"')"
  t_fatal "${POOL_NAME} effective_limit is ${NARROW_EFFECTIVE}, not ${SLOT_LIMIT}: there is nothing for the tasks below to race for"
fi
t_pass "${POOL_NAME} effective_limit: ${NARROW_EFFECTIVE}"
# Who the API says made the change. Reported, not asserted: a swarm-api older
# than the attribution on the limit routes writes no admin_changed_by, and that
# is a fact about the deployment rather than a failure of this race.
NARROWED_BY="$(printf '%s' "${NARROW_DOC}" | jq -r '.admin_changed_by // empty')"
if [[ -n "${NARROWED_BY}" ]]; then
  t_info "swarm-api recorded the narrow as made by ${NARROWED_BY}"
else
  t_info "the pool carries no admin_changed_by: this swarm-api does not yet attribute limit changes"
fi

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
# THE FLOOR, because 0 == 0 over zero attempts is a PASS for invariant 5 that
# read nothing. The sweep (13-swallowed-stderr-sweep.md, race-test.sh:159)
# found it reachable through a failed query, and that path now aborts; the same
# vacuous pass is still reachable through a query that simply matched nothing.
# The case above asserts the narrowed pool was held. When it was, at least one
# task was leased -- and the scheduler writes the attempt document in the same
# step it leases (apps/scheduler/scheduler/loop.py, `create_attempt`).
assert_ge "${CHECKED}" 1 "tasks whose attempts were read (a generation check over none proves nothing)"
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

# ---------------------------------------------------------------------------
t_case "${POOL_NAME} is back at its original ceiling"
#
# A CASE, NOT ONLY A TRAP. The EXIT trap now fails a run that left the pool
# narrowed, but by then t_summary has already printed its verdict, and a green
# `race: N/N passed` followed by COULD NOT RESTORE is two answers to one
# question. Restoring here puts the failure IN the summary -- the line people
# quote and the one `make verify-remote`'s 40-line tail keeps. The trap stays for
# every path that never gets this far; `restore` is idempotent, so on this path
# it only repeats this verdict.
if restore; then
  t_pass "${POOL_NAME} hard_limit reads ${ORIGINAL_LIMIT} again"
else
  t_fail "${POOL_NAME} was NOT restored to hard_limit ${ORIGINAL_LIMIT}: it is still narrowed, and the repair is printed above"
fi

t_summary
