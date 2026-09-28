#!/usr/bin/env bash
# Prove the concurrency invariant: no pool is EVER over its effective limit.
#
# The interesting failure this catches is transient. A scheduler that checks
# capacity, then writes the lease in a second step, will briefly exceed the limit
# under load and then settle back -- an end-of-run check would see nothing wrong.
# So this samples every pool continuously while a deliberate overload runs, and
# fails on the worst sample, not the last one.
#
# It also checks two more invariants from CONTRACT.md, from the same samples
# (#175 -- until then both case titles below were asserted by re-checking the
# global limit, which a scheduler counting RUNNING instead of LEASED passes):
#
#   invariant 3  at every sample, every pool's `active` equals the units held by
#                the unreleased leases that name it, and at least one sample
#                caught a lease whose task had not reached RUNNING -- without
#                one the run could not tell LEASED counting from RUNNING
#                counting. A task in LEASED..RUNNING with no unreleased lease
#                behind it for longer than --grace fails too.
#   invariant 1  no unreleased lease -- no capacity -- is held for a QUEUED,
#                READY, PARKED or SUBMITTED task for longer than --grace.
#
# Each sample is ONE consistent Firestore read (every query at one readTime),
# and scripts/lib/concurrency-judge.jq says why the pool check is exact and why
# the other two carry a grace.
#
# NOT ASSERTED: that the backlog created no Cloud Run execution. An execution
# carries no task id (the Cloud Run job is per tenant and profile, and
# scheduler/dispatch.py labels it with tenant, profile and class only), so this
# suite cannot attribute one to a waiting task. It prints the count. What it
# asserts instead is the precondition dispatch depends on: a waiting task holds
# no lease, and the scheduler dispatches only on a lease.
#
# Usage: scripts/concurrency-test.sh [--count N] [--profile mock] [--duration 180]
#                                    [--interval 2] [--grace 30]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
SUITE_NAME="concurrency"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"

COUNT=0
PROFILE="mock"
DURATION=180
SAMPLE_INTERVAL=2
# Seconds a two-transaction gap (a park, then its release; a reconciler release,
# then its repair) may stay visible before it counts. Both close in well under a
# second in the code; 30 leaves room for a slow worker without letting a leak
# through, which stays open for ever.
GRACE=30
# Rows a sample query may return. At the limit the read is refused rather than
# judged, because a truncated lease list would read as a pool mismatch.
SAMPLE_QUERY_LIMIT=1000

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)    COUNT="$2"; shift 2 ;;
    --profile)  PROFILE="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --interval) SAMPLE_INTERVAL="$2"; shift 2 ;;
    --grace)    GRACE="$2"; shift 2 ;;
    -h|--help)  sed -n '2,35p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "${GRACE}" =~ ^[0-9]+$ ]] || die "--grace must be a whole number of seconds, got: ${GRACE}"

step "Concurrency invariant: ${PROJECT_ID} / ${ENVIRONMENT}"
require_platform

JUDGE="${SWARM_LIB_DIR}/concurrency-judge.jq"
[[ -f "${JUDGE}" ]] || die "missing ${JUDGE}"
CONCURRENCY_JSON="$(states_json "${CONCURRENCY_STATES[@]}")"
PENDING_JSON="$(states_json "${PENDING_STATES[@]}")"
PRE_RUNNING_JSON="$(jq -c 'map(select(. != "RUNNING"))' <<<"${CONCURRENCY_JSON}")"

# judge MODE [jq flags...] -- concurrency-judge.jq with every argument it reads.
judge() {
  local mode="$1"; shift
  jq -c "$@" -f "${JUDGE}" --arg mode "${mode}" \
    --argjson concurrency "${CONCURRENCY_JSON}" \
    --argjson pending "${PENDING_JSON}" \
    --argjson pre_running "${PRE_RUNNING_JSON}" \
    --argjson grace "${GRACE}"
}

# ---------------------------------------------------------------------------
# One sample, read consistently.
#
# The pools, the leases and the tasks used to be read by separate requests, so a
# lease created or released between two of them made the pool count and the
# lease count disagree with no defect behind it. Every query below passes the
# SAME `readTime`, and Firestore answers each from the snapshot committed at
# that instant. It is one second in the past so that it is never ahead of the
# server's clock.
# ---------------------------------------------------------------------------

# fs_query_at COLLECTION WHERE_JSON READ_TIME -> a JSON array of decoded docs
fs_query_at() {
  local collection="$1" where="$2" at="$3" query rows docs n
  query="$(jq -nc --arg c "${collection}" --argjson w "${where}" --arg t "${at}" \
             --argjson l "${SAMPLE_QUERY_LIMIT}" '
    { structuredQuery: ({ from: [{ collectionId: $c }], limit: $l }
                        + (if $w == null then {} else { where: $w } end)),
      readTime: $t }')"
  rows="$(fs_request POST "$(fs_base):runQuery" "${query}")" || return 1
  docs="$(printf '%s' "${rows}" | jq -c "${FS_JQ} [ .[]? | select(.document != null) | .document | doc ]")" || return 1
  n="$(jq -r 'length' <<<"${docs}")"
  if [[ "${n}" -ge "${SAMPLE_QUERY_LIMIT}" ]]; then
    err "${collection}: ${n} rows at the query limit; this sample would be judged on a truncated read"
    return 1
  fi
  printf '%s' "${docs}"
}

# fs_tasks_at IDS_JSON READ_TIME -> the tasks named, decoded, as of READ_TIME.
# batchGet, 100 names a request; a missing task is simply absent from the list.
fs_tasks_at() {
  local ids="$1" at="$2" total i names reply part all='[]'
  total="$(jq -r 'length' <<<"${ids}")"
  i=0
  while [[ "${i}" -lt "${total}" ]]; do
    names="$(jq -c --arg base "projects/${PROJECT_ID}/databases/${FIRESTORE_DATABASE}/documents/tasks/" \
               --argjson i "${i}" '.[$i:$i+100] | map($base + .)' <<<"${ids}")"
    reply="$(fs_request POST "$(fs_base):batchGet" \
               "$(jq -nc --argjson d "${names}" --arg t "${at}" '{documents: $d, readTime: $t}')")" || return 1
    part="$(printf '%s' "${reply}" | jq -c "${FS_JQ} [ .[]? | select(.found != null) | .found | doc ]")" || return 1
    all="$(jq -nc --argjson a "${all}" --argjson b "${part}" '$a + $b')"
    i=$(( i + 100 ))
  done
  printf '%s' "${all}"
}

# consistent_sample -> {at, pools, leases, tasks}, every part read at one instant.
consistent_sample() {
  local at pools leases holders lease_tasks in_concurrency
  at="$(jq -rn 'now - 1 | floor | todate')"
  in_concurrency="$(jq -nc --argjson s "${CONCURRENCY_JSON}" '
    { fieldFilter: { field: { fieldPath: "state" }, op: "IN",
                     value: { arrayValue: { values: [ $s[] | { stringValue: . } ] } } } }')"
  pools="$(fs_query_at pools null "${at}")" || return 1
  leases="$(fs_query_at leases "$(fs_null_filter released_at IS_NULL)" "${at}")" || return 1
  holders="$(fs_query_at tasks "${in_concurrency}" "${at}")" || return 1
  lease_tasks="$(fs_tasks_at "$(jq -c '[ .[].task_id | select(. != null) ] | unique' <<<"${leases}")" "${at}")" || return 1
  jq -nc --arg at "${at}" --argjson pools "${pools}" --argjson leases "${leases}" \
         --argjson holders "${holders}" --argjson lease_tasks "${lease_tasks}" \
    "${FS_JQ}"' { at: $at,
       pools: [ $pools[] | . + { effective: effective_limit } ],
       leases: $leases,
       tasks: ($holders + $lease_tasks | unique_by(.id)) }'
}

GLOBAL_LIMIT="$(pool_doc global | jq -r "${FS_JQ} effective_limit")" \
  || die "could not read the global pool from Firestore; see the Firestore error above -- this is a failed read, not proof the pool is unconfigured"
[[ "${GLOBAL_LIMIT}" -gt 0 ]] || die "the global pool has no limit configured; run 'make infra' first"
info "global effective limit: ${GLOBAL_LIMIT} units"

# Deliberately oversubscribe: three times the global limit guarantees that the
# limit is the binding constraint rather than the submission rate.
[[ "${COUNT}" -gt 0 ]] || COUNT=$(( GLOBAL_LIMIT * 3 ))
info "submitting ${COUNT} tasks on profile ${PROFILE}"

SAMPLES="${BUILD_DIR}/concurrency-samples-${ENVIRONMENT}.jsonl"
: >"${SAMPLES}"
TASK_IDS=()

cleanup() {
  [[ "${#TASK_IDS[@]}" -eq 0 ]] || cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
t_case "Submit ${COUNT} tasks (no infrastructure should appear yet)"
QUEUED_BEFORE="$(fs_count tasks state EQUAL QUEUED)"
for i in $(seq 1 "${COUNT}"); do
  if id="$(submit_task "${PROFILE}" "$(jq -nc --argjson i "${i}" '{prompt: ("concurrency " + ($i|tostring))}')" \
           '{"metadata":{"source":"concurrency-test"}}')"; then
    TASK_IDS+=("${id}")
  fi
done
assert_eq "${COUNT}" "${#TASK_IDS[@]}" "tasks accepted"
t_info "queued before: ${QUEUED_BEFORE}"

# ---------------------------------------------------------------------------
t_case "Sample every pool for ${DURATION}s while the backlog drains"
DEADLINE=$(( $(date -u +%s) + DURATION ))
VIOLATIONS=0
MAX_HOLDING=0
SAMPLE_COUNT=0

while [[ "$(date -u +%s)" -lt "${DEADLINE}" ]]; do
  # A failed read is a failed run, never a sample of zeros.
  SAMPLE="$(consistent_sample)" \
    || die "could not take a consistent sample of pools, leases and tasks; see the Firestore error above"
  JUDGED="$(judge sample <<<"${SAMPLE}")" || die "concurrency-judge.jq could not judge a sample"
  SNAPSHOT="$(jq -c '.pools' <<<"${SAMPLE}")"
  HOLDING="$(jq -r '.holding_tasks' <<<"${JUDGED}")"
  [[ "${HOLDING}" -gt "${MAX_HOLDING}" ]] && MAX_HOLDING="${HOLDING}"

  # Printed as it happens, so a long run shows the moment it went wrong.
  jq -r '.pool_mismatches[] | "    POOL/LEASE MISMATCH: \(.pool) active=\(.active) held-by-leases=\(.lease_units)"' \
    <<<"${JUDGED}" >&2

  OVER="$(jq -c '[ .[] | select(.active > .effective) ]' <<<"${SNAPSHOT}")"
  N_OVER="$(jq -r 'length' <<<"${OVER}")"
  if [[ "${N_OVER}" -gt 0 ]]; then
    VIOLATIONS=$(( VIOLATIONS + N_OVER ))
    jq -r '.[] | "    OVER LIMIT: \(.id) active=\(.active) effective=\(.effective)"' <<<"${OVER}" >&2
  fi

  # The judged fields, plus the pools the peak check below reads. `at` is the
  # sample's readTime, which is what the grace in concurrency-judge.jq measures.
  jq -nc --argjson pools "${SNAPSHOT}" --argjson judged "${JUDGED}" \
    '$judged + {holding: $judged.holding_tasks, pools: $pools}' >>"${SAMPLES}"
  SAMPLE_COUNT=$(( SAMPLE_COUNT + 1 ))

  REMAINING="$(fs_count tasks state EQUAL QUEUED)"
  READY="$(fs_count tasks state EQUAL READY)"
  if [[ "${REMAINING}" -eq 0 && "${READY}" -eq 0 && "${HOLDING}" -eq 0 && "${SAMPLE_COUNT}" -gt 3 ]]; then
    t_info "backlog drained after ${SAMPLE_COUNT} samples"
    break
  fi
  sleep "${SAMPLE_INTERVAL}"
done
t_info "${SAMPLE_COUNT} samples taken, peak tasks holding capacity: ${MAX_HOLDING}"
assert_eq "0" "${VIOLATIONS}" "pool-over-limit samples"

# ---------------------------------------------------------------------------
t_case "Global unit budget was never exceeded"
PEAK_GLOBAL="$(jq -s '[ .[].pools[] | select(.id == "global") | .active ] | max // 0' "${SAMPLES}")"
assert_le "${PEAK_GLOBAL}" "${GLOBAL_LIMIT}" "peak global pool usage"

# ---------------------------------------------------------------------------
t_case "Leases, not pods, are what counted (invariant 3)"
# Exact at every sample: a lease and its pool units are written in one
# transaction and each sample is one consistent read, so any difference is a
# defect. A scheduler that counted RUNNING instead of LEASED leaves the pools
# short by every lease whose task is still LEASED, DISPATCHED or STARTING.
MISMATCH_SAMPLES="$(jq -s '[ .[] | select((.pool_mismatches | length) > 0) ] | length' "${SAMPLES}")"
if [[ "${MISMATCH_SAMPLES}" -gt 0 ]]; then
  jq -rs '[ .[] | select((.pool_mismatches | length) > 0) ] | .[0:5][]
          | "    \(.at): " + ([ .pool_mismatches[] | "\(.pool) active=\(.active) leases=\(.lease_units)" ] | join(", "))' \
    "${SAMPLES}" >&2
fi
assert_eq "0" "${MISMATCH_SAMPLES}" "samples where a pool's active units differ from its unreleased leases' units"

# The boundary has to have been OBSERVED for the check above to mean anything:
# with every lease already RUNNING, LEASED counting and RUNNING counting agree.
PRE_RUNNING_SAMPLES="$(jq -s '[ .[] | select(.pre_running_leases > 0) ] | length' "${SAMPLES}")"
assert_ge "${PRE_RUNNING_SAMPLES}" 1 "samples that caught a lease before its task reached RUNNING"

# The lease count against LEASED..RUNNING: every task holding a concurrency
# state is behind an unreleased lease, allowing only the reconciler's
# release-then-repair gap (concurrency-judge.jq names it).
PERSISTED="$(judge persist -s <"${SAMPLES}")" || die "concurrency-judge.jq could not judge the run"
UNLEASED="$(jq -r '.unleased_holders | length' <<<"${PERSISTED}")"
jq -r '.unleased_holders[] | "    \(.task) \(.state) lease=\(.lease) for \(.seconds)s (\(.first_seen)..\(.last_seen))"' \
  <<<"${PERSISTED}" >&2
assert_eq "0" "${UNLEASED}" "tasks in LEASED..RUNNING with no unreleased lease for over ${GRACE}s"
t_info "peak unreleased leases: $(jq -s '[ .[].leases ] | max // 0' "${SAMPLES}"), peak tasks in LEASED..RUNNING: ${MAX_HOLDING}"

# ---------------------------------------------------------------------------
t_case "Backlog held no capacity while it waited (invariant 1)"
# QUEUED/PARKED/READY/SUBMITTED must hold no lease. Together with the exact
# pool check above -- every active unit belongs to an unreleased lease -- that
# is "the backlog held no capacity". The worker's park-then-release gap is
# allowed for --grace seconds and no longer.
BACKLOG="$(jq -r '.backlog_leases | length' <<<"${PERSISTED}")"
jq -r '.backlog_leases[] | "    \(.lease) held by \(.task) in \(.state) for \(.seconds)s (\(.first_seen)..\(.last_seen))"' \
  <<<"${PERSISTED}" >&2
assert_eq "0" "${BACKLOG}" "leases held by a waiting task for over ${GRACE}s"
t_info "samples that saw a waiting task holding a lease at all, closed within ${GRACE}s: $(jq -s '[ .[] | select((.backlog_leases | length) > 0) ] | length' "${SAMPLES}")"

# Reported, NOT asserted: an execution carries no task id, so it cannot be
# attributed to a waiting task from here (see the header).
EXECUTIONS_ERR="$(mktemp "${TMPDIR:-/tmp}/swarm-executions.XXXXXX")"
# REST rather than `gcloud run jobs executions list`, so this suite runs in an
# image with no Cloud SDK. Zero executions is an ANSWER and an unreadable
# listing is a FAILURE to list; neither is printed as the other.
if EXECUTIONS="$(cloud_run_execution_count 2>"${EXECUTIONS_ERR}")"; then
  rm -f "${EXECUTIONS_ERR}"
  t_info "cloud run executions visible (not attributable to tasks; not asserted): ${EXECUTIONS}"
else
  EXECUTIONS_ERR_DETAIL="$(cat "${EXECUTIONS_ERR}" 2>/dev/null || true)"
  rm -f "${EXECUTIONS_ERR}"
  die_if_auth_failure "${EXECUTIONS_ERR_DETAIL}"
  warn "could not list Cloud Run job executions; this is a failed listing, not proof that none exist"
  printf '%s\n' "${EXECUTIONS_ERR_DETAIL}" | redact | head -n 3 | sed 's/^/     /' >&2
fi

t_info "samples: ${SAMPLES}"
t_summary
