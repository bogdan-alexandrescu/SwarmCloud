#!/usr/bin/env bash
# Dispatch latency, decomposed -- and cold start, which nothing else measures.
#
# THE MEASUREMENT THIS EXISTS FOR
# -------------------------------
# The first real claude-code run on this platform, 2026-09-22:
#
#     dispatched 03:46:05 -> starting 03:49:14 (+3m09s) -> running +0.1s
#     -> succeeded +18s
#
# Three minutes nine seconds of Cloud Run cold start against eighteen seconds
# of agent. The correctness suites call that run a pass, because it IS a pass:
# the task succeeded, no capacity leaked, no invariant was violated. The UI
# renders the whole thing as the single word "dispatched". Nothing anywhere
# turns 189 seconds into a number an operator can compare against last week.
#
# WHY EACH SEGMENT IS REPORTED SEPARATELY
# ---------------------------------------
# "Dispatch got slower" is at least four different problems:
#
#   ready -> lease_acquired        admission: the scheduler's transaction
#   lease_acquired -> dispatched   the control plane creating the execution
#   dispatched -> starting         the BACKEND: scheduling and image pull
#   starting -> running            the container reaching the agent
#
# They have different owners and different fixes. A single end-to-end p95 says
# only that one of them moved. So the decomposition is the product here, and
# `dispatch.cold_start` (dispatched -> running) is called out as its own metric
# because it dominates and because percentiles cannot be added -- p95 of two
# halves is not p95 of the whole.
#
# WHY THIS READS THE API AND NOT FIRESTORE
# ----------------------------------------
# The other suites read Firestore directly and say why: observing through the
# API under test would let a broken API report success. That argument does not
# transfer here. These timestamps are written by the control plane into the
# event log; whoever fetches them, the numbers are the same, and a broken API
# produces a transport failure or a non-2xx, both of which this records as
# not-measured rather than as a fast dispatch. What reading the API buys is
# that this collector needs no roles/datastore.viewer -- the exact permission
# the verify service account is missing today, which is why race-test.sh
# cannot run.
#
# COST TO RUN
# -----------
#   --profile mock (default)  ~2 minutes, a handful of Cloud Run executions.
#                             Fractions of a cent. Safe on any environment.
#   --profile claude-code     ~5 minutes AND REAL PROVIDER SPEND -- it runs an
#                             actual agent. Use --count 3 and expect tokens on
#                             the bill. That is the run that measures the cold
#                             start that matters, so it is worth doing
#                             deliberately and not on every commit.
#
# Usage: scripts/bench-dispatch.sh [--count 10] [--profile mock] [--timeout 900]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/benchlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/benchlib.sh"
# For submit_task and cancel_all ONLY. This suite reports no t_pass and no
# t_fail: the verdict is benchstat's, compared against a baseline. Restating
# submit_task here to avoid the import is how the two copies drift.
SUITE_NAME="bench-dispatch"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/testlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/testlib.sh"

load_env

COUNT=10
PROFILE="mock"
TIMEOUT=900
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)   COUNT="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Dispatch latency: ${COUNT} task(s) on ${PROFILE}"
bench_init dispatch

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-dispatch.XXXXXX")"
TASK_IDS=()
cleanup() {
  if [[ "${KEEP}" -eq 0 && "${#TASK_IDS[@]}" -gt 0 ]]; then
    cancel_all ${TASK_IDS[@]+"${TASK_IDS[@]}"}
  fi
  rm -rf "${WORK}"
}
trap cleanup EXIT INT TERM

# Readiness first, for the same reason bench-api.sh probes it: a page of
# not-measured samples is a worse diagnosis than one sentence naming the cause.
if ! READY="$(bench_request GET /readyz "${WORK}/readyz.json")"; then
  die "curl could not reach $(api_url)/readyz. That is a transport failure, not a slow platform."
fi
case "${READY%% *}" in
  2*) ;;
  401|403) die "readyz answered HTTP ${READY%% *}: an auth/IAM problem, not a latency one." ;;
  *) die "readyz answered HTTP ${READY%% *}, not 2xx. Fix that before measuring dispatch." ;;
esac

# The BACKEND is a label, not an assumption. Cold start on Cloud Run Jobs and
# cold start on GKE Autopilot are different numbers with different owners, and
# a baseline that merges them hides a regression in whichever is faster. The
# value is read from /v1/runtimes -- which serves `resolved_backend` from the
# frozen catalogue -- rather than restated here, so it cannot drift.
BACKEND="unknown"
if RT="$(bench_request GET /v1/runtimes "${WORK}/runtimes.json")" && [[ "${RT%% *}" == 2* ]]; then
  BACKEND="$(jq -r --arg p "${PROFILE}" '.runtimes[$p].resolved_backend // "unknown"' "${WORK}/runtimes.json")"
fi
[[ "${BACKEND}" != "unknown" ]] || warn "the backend for profile ${PROFILE} could not be read; samples will be labelled backend=unknown"
info "profile ${PROFILE} resolves to backend ${BACKEND}"
LABELS="$(bench_label runner_profile "${PROFILE}" backend "${BACKEND}")"

# ---------------------------------------------------------------------------
info "submitting ${COUNT} task(s)"
SUBMIT_FAILURES=0
for i in $(seq 1 "${COUNT}"); do
  if id="$(submit_task "${PROFILE}" \
        "$(jq -nc --argjson i "${i}" '{prompt: ("bench-dispatch " + ($i|tostring))}')" \
        '{"metadata":{"source":"bench-dispatch"}}' 2>/dev/null)"; then
    TASK_IDS+=("${id}")
  else
    SUBMIT_FAILURES=$(( SUBMIT_FAILURES + 1 ))
  fi
done
info "submitted ${#TASK_IDS[@]}, ${SUBMIT_FAILURES} rejected"
[[ "${#TASK_IDS[@]}" -gt 0 ]] || die "every submission was rejected; there is nothing to measure"

# A rejected submission is a hole in the sample set, and a hole that says
# nothing looks like a smaller run rather than a broken one.
for i in $(seq 1 "${SUBMIT_FAILURES}"); do
  bench_missing dispatch.total s "the submission was rejected by the API" "${LABELS}"
done

# ---------------------------------------------------------------------------
info "waiting up to ${TIMEOUT}s for terminal states"
DEADLINE=$(( $(date -u +%s) + TIMEOUT ))
while [[ "$(date -u +%s)" -lt "${DEADLINE}" ]]; do
  pending=0
  for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
    if ! FIELDS="$(bench_request GET "/v1/tasks/${id}" "${WORK}/task.json")"; then
      pending=$(( pending + 1 )); continue
    fi
    [[ "${FIELDS%% *}" == 2* ]] || { pending=$(( pending + 1 )); continue; }
    state="$(jq -r '.state // "?"' "${WORK}/task.json")"
    case "${state}" in
      SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED) ;;
      *) pending=$(( pending + 1 )) ;;
    esac
  done
  printf '\r  %s of %s still running   ' "${pending}" "${#TASK_IDS[@]}" >&2
  [[ "${pending}" -eq 0 ]] && break
  sleep 5
done
printf '\n' >&2

# ---------------------------------------------------------------------------
info "collecting event streams"
EVENTS="${WORK}/events.jsonl"
: >"${EVENTS}"
for id in ${TASK_IDS[@]+"${TASK_IDS[@]}"}; do
  if ! FIELDS="$(bench_request GET "/v1/tasks/${id}/events?limit=200" "${WORK}/ev.json")"; then
    # Not an empty stream. A task whose events could not be read contributes a
    # null sample for every segment, which fails the gate -- rather than
    # vanishing and leaving a smaller, faster-looking run.
    jq -nc --argjson l "${LABELS}" '{labels:$l, events:[]}' >>"${EVENTS}"
    continue
  fi
  if [[ "${FIELDS%% *}" != 2* ]]; then
    jq -nc --argjson l "${LABELS}" '{labels:$l, events:[]}' >>"${EVENTS}"
    continue
  fi
  jq -c --argjson l "${LABELS}" '{labels:$l, events:(.events // [])}' "${WORK}/ev.json" >>"${EVENTS}"
done

bench_append_segments "${EVENTS}"
bench_finish
