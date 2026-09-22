#!/usr/bin/env bash
# Admission throughput: how fast the scheduler can take a backlog apart.
#
# WHAT IS BEING MEASURED
# ----------------------
# Admission is the all-or-nothing multi-pool reservation in CONTRACT.md
# invariant 2: every pool a task needs is reserved in ONE Firestore
# transaction, or none is. The interesting performance question about that
# design is whether it degrades as the number of pools involved grows -- a
# transaction touching six documents contends differently from one touching
# four, and contention under a backlog is where it would show.
#
# The rate is computed from `lease_acquired` timestamps in the event log, not
# from how long this script took to notice. Polling rate is this script's
# property; the timestamps are the platform's.
#
# HOW THE POOL-COUNT QUESTION IS ANSWERED WITHOUT CHANGING ANYTHING
# -----------------------------------------------------------------
# Not by reconfiguring pools -- that is an admin mutation on a shared project
# and this suite makes none. Different runner profiles already reserve
# different numbers of pools: a profile with no provider (`mock`) skips the
# provider pool that `claude-code` must take. So running this on two profiles
# and comparing is the experiment, and the profile is a LABEL so the two never
# share a baseline.
#
# `admission.pools_visible` records the platform's pool cardinality alongside,
# because a throughput figure from an environment with 6 pools is not
# comparable to one from an environment with 40. It is read from
# /v1/capacity -- the count is NOT restated here from the profile catalogue,
# which is frozen contract data and would drift the moment a pool is added.
#
# COST TO RUN
# -----------
# --profile mock (default): submits `--count` tasks, all of which are cancelled
# on exit. Two to four minutes; a few hundred Firestore writes and a handful of
# Cloud Run executions; cents. --profile claude-code SPENDS REAL PROVIDER
# TOKENS and should be run deliberately with a small --count.
#
# Usage: scripts/bench-admission.sh [--count 150] [--profile mock] [--timeout 600]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/benchlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/benchlib.sh"
# submit_task and cancel_all only; the verdict is benchstat's. See
# bench-dispatch.sh for why this borrows rather than restates.
SUITE_NAME="bench-admission"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/testlib.sh"

load_env

# 150 is concurrency-test.sh's shape, deliberately: the same backlog size makes
# the two runs comparable, and that suite has already proved 150 is enough to
# make the pool limit the binding constraint rather than the submission rate.
COUNT=150
PROFILE="mock"
TIMEOUT=600
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)   COUNT="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Admission throughput: ${COUNT} task backlog on ${PROFILE}"
bench_init admission

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-admission.XXXXXX")"
TASK_IDS=()
cleanup() {
  if [[ "${KEEP}" -eq 0 && "${#TASK_IDS[@]}" -gt 0 ]]; then
    info "cancelling ${#TASK_IDS[@]} task(s)"
    cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
  fi
  rm -rf "${WORK}"
}
trap cleanup EXIT INT TERM

if ! READY="$(bench_request GET /readyz "${WORK}/readyz.json")" || [[ "${READY%% *}" != 2* ]]; then
  die "the API is not ready (${READY:-no response}); that is not a slow admission path."
fi

BACKEND="unknown"
POOLS=""
if CF="$(bench_request GET /v1/capacity "${WORK}/capacity.json")" && [[ "${CF%% *}" == 2* ]]; then
  BACKEND="$(jq -r --arg p "${PROFILE}" '.runner_profiles[$p].backend // "unknown"' "${WORK}/capacity.json")"
  # `pools_complete` is served precisely so a truncated listing is not read as
  # a small platform. A truncated one cannot answer "how many pools", so it is
  # recorded as not-measured rather than as a floor dressed up as a count.
  if [[ "$(jq -r '.pools_complete == true' "${WORK}/capacity.json")" == "true" ]]; then
    POOLS="$(jq -r '(.pools // []) | length' "${WORK}/capacity.json")"
  fi
fi
LABELS="$(bench_label runner_profile "${PROFILE}" backend "${BACKEND}")"
if [[ -n "${POOLS}" ]]; then
  bench_sample admission.pools_visible pools "${POOLS}" "${LABELS}"
  info "profile ${PROFILE} on ${BACKEND}; ${POOLS} pool(s) visible"
else
  bench_missing admission.pools_visible pools \
    "the pool listing was truncated or unreadable, so its cardinality is unknown" "${LABELS}"
fi

# ---------------------------------------------------------------------------
# The backlog. Submitted as fast as the API accepts, on purpose: this measures
# how fast the SCHEDULER drains, and a paced submission would measure the pace.
info "submitting ${COUNT} task(s) as fast as the API accepts"
SUBMIT_START="$(bench_now_ms)"
REJECTED=0
for i in $(seq 1 "${COUNT}"); do
  if id="$(submit_task "${PROFILE}" \
        "$(jq -nc --argjson i "${i}" '{message:"bench-admission", index:$i}')" \
        '{"metadata":{"source":"bench-admission"}}' 2>/dev/null)"; then
    TASK_IDS+=("${id}")
  else
    REJECTED=$(( REJECTED + 1 ))
  fi
done
SUBMIT_MS=$(( $(bench_now_ms) - SUBMIT_START ))
[[ "${SUBMIT_MS}" -gt 0 ]] || SUBMIT_MS=1
info "submitted ${#TASK_IDS[@]} in ${SUBMIT_MS}ms, ${REJECTED} rejected"
[[ "${#TASK_IDS[@]}" -gt 0 ]] || die "every submission was rejected; there is no backlog to drain"

bench_sample admission.submit_rate_per_s tasks_per_s \
  "$(awk -v n="${#TASK_IDS[@]}" -v ms="${SUBMIT_MS}" 'BEGIN { printf "%.3f", n / (ms / 1000.0) }')" \
  "${LABELS}"

# ---------------------------------------------------------------------------
# Wait for the backlog to be ADMITTED, which is not the same as finished. The
# question is how fast leases are granted; what happens after is dispatch's.
info "waiting up to ${TIMEOUT}s for the backlog to be admitted"
DEADLINE=$(( $(date -u +%s) + TIMEOUT ))
while [[ "$(date -u +%s)" -lt "${DEADLINE}" ]]; do
  waiting=0
  for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
    if ! TF="$(bench_request GET "/v1/tasks/${id}" "${WORK}/t.json")" || [[ "${TF%% *}" != 2* ]]; then
      waiting=$(( waiting + 1 )); continue
    fi
    case "$(jq -r '.state // "?"' "${WORK}/t.json")" in
      SUBMITTED|QUEUED|READY|PARKED) waiting=$(( waiting + 1 )) ;;
      *) ;;
    esac
  done
  printf '\r  %s of %s not yet admitted   ' "${waiting}" "${#TASK_IDS[@]}" >&2
  [[ "${waiting}" -eq 0 ]] && break
  sleep 5
done
printf '\n' >&2

# ---------------------------------------------------------------------------
info "reading lease_acquired timestamps"
LEASES="${WORK}/leases.jsonl"
: >"${LEASES}"
UNREADABLE=0
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  if ! EF="$(bench_request GET "/v1/tasks/${id}/events?limit=200" "${WORK}/ev.json")" \
     || [[ "${EF%% *}" != 2* ]]; then
    UNREADABLE=$(( UNREADABLE + 1 ))
    continue
  fi
  jq -r '[.events // [] | .[] | select(.type == "lease_acquired") | .at] | first // empty' \
    "${WORK}/ev.json" >>"${LEASES}"
done

ADMITTED="$(grep -c . "${LEASES}" || true)"
info "${ADMITTED} of ${#TASK_IDS[@]} admitted; ${UNREADABLE} event stream(s) unreadable"

# Throughput over the admission WINDOW -- first lease to last -- because that
# is the interval the scheduler was actually working in. Dividing by wall time
# since submission would fold in however long the first tick took to fire and
# report a slower scheduler than the one that ran.
if [[ "${ADMITTED}" -ge 2 ]]; then
  WINDOW="$(python3 -c "
import sys
from datetime import datetime, timezone
ts = []
for line in open('${LEASES}'):
    line = line.strip()
    if not line:
        continue
    try:
        d = datetime.fromisoformat(line.replace('Z', '+00:00'))
    except ValueError:
        continue
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    ts.append(d.timestamp())
if len(ts) < 2:
    sys.exit(3)
print('%.6f' % (max(ts) - min(ts)))
")" || WINDOW=""
  if [[ -z "${WINDOW}" ]]; then
    bench_missing admission.throughput_per_s tasks_per_s \
      "the lease timestamps could not be parsed" "${LABELS}"
  elif [[ "$(awk -v w="${WINDOW}" 'BEGIN { print (w > 0) ? 1 : 0 }')" == "1" ]]; then
    bench_sample admission.throughput_per_s tasks_per_s \
      "$(awk -v n="${ADMITTED}" -v w="${WINDOW}" 'BEGIN { printf "%.4f", n / w }')" "${LABELS}"
    bench_sample admission.window_seconds s "${WINDOW}" "${LABELS}"
  else
    # Every lease carries the same timestamp to the second. That is a real
    # answer about clock granularity, not an infinite throughput, and
    # publishing n/0 as a record-breaking rate is precisely the dishonesty
    # this suite is built to avoid.
    bench_missing admission.throughput_per_s tasks_per_s \
      "every lease shares one timestamp, so the admission window is zero and the rate is not defined" "${LABELS}"
    bench_sample admission.window_seconds s "${WINDOW}" "${LABELS}"
  fi
else
  bench_missing admission.throughput_per_s tasks_per_s \
    "only ${ADMITTED} task(s) were admitted; a rate needs at least two leases" "${LABELS}"
  bench_missing admission.window_seconds s \
    "only ${ADMITTED} task(s) were admitted" "${LABELS}"
fi

# The tasks that never got a lease are the finding. Left out, the run would
# report the throughput of the subset that happened to work.
NEVER=$(( ${#TASK_IDS[@]} - ADMITTED ))
if [[ "${NEVER}" -gt 0 ]]; then
  for i in $(seq 1 "${NEVER}"); do
    bench_missing admission.admitted_fraction fraction \
      "this task was never admitted within ${TIMEOUT}s" "${LABELS}"
  done
fi
for i in $(seq 1 "${ADMITTED}"); do
  bench_sample admission.admitted_fraction fraction 1 "${LABELS}"
done

bench_finish
