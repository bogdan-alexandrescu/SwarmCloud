#!/usr/bin/env bash
# API latency per route, at a fixed concurrency, as percentiles.
#
# WHY PER ROUTE AND NOT "THE API"
# -------------------------------
# The UI's own data-source strip measured /v1/stats at 1711ms and /v1/providers
# at 896ms while every other route answered under 300ms. A single "API p95"
# averages that difference away, and the difference IS the finding: those two
# routes do something the others do not.
#
# WHY /v1/stats GETS AN EXTRA MEASUREMENT
# ---------------------------------------
# `service.stats()` calls `count_tasks_by_state`, which issues one Firestore
# count() per task state. Firestore bills an aggregation per 1000 index entries
# SCANNED, not per row returned, so that route's latency and its cost both grow
# with the amount of HISTORY in the collection -- not with how much load the
# platform is under. A threshold on its raw latency therefore fires on a
# perfectly healthy platform that has simply been running for a month, and an
# operator learns to ignore it.
#
# So this records three things for that route and not one:
#
#   api.latency{route=/v1/stats}      the raw number, for the UI's sake
#   api.stats.history_tasks           how many task documents exist right now
#   api.stats.ms_per_1k_history       latency normalised by that history
#
# The normalised figure is the one to gate on: stable means the route is fine
# and the collection grew, rising means the route itself regressed.
#
# A NON-2xx RESPONSE IS NOT A FAST RESPONSE. A 403 answers in five
# milliseconds and would set a record every time. Any status outside 2xx is
# recorded as not-measured with the status in the reason, which fails the gate
# rather than improving the percentile.
#
# Cost to run: about 30 seconds and a few hundred read requests against
# Firestore -- under a cent at the default counts. Safe to run against a live
# environment: every request is a GET and nothing is created.
#
# Usage: scripts/bench-api.sh [--rounds 8] [--concurrency 4] [--route /v1/x]...

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/benchlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/benchlib.sh"

load_env

ROUNDS=8
CONCURRENCY=4
ROUTES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rounds)      ROUNDS="$2"; shift 2 ;;
    --concurrency) CONCURRENCY="$2"; shift 2 ;;
    --route)       ROUTES+=("$2"); shift 2 ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# The read surface the web UI actually drives, plus the readiness probe as a
# floor: /readyz touches Firestore and nothing else, so it separates "this
# route is slow" from "everything is slow".
if [[ "${#ROUTES[@]}" -eq 0 ]]; then
  ROUTES=(
    /readyz
    /v1/stats
    /v1/capacity
    /v1/providers
    /v1/resource-classes
    /v1/runtimes
    /v1/tenants/me
    "/v1/tasks?limit=200"
    "/v1/workflows?limit=100"
  )
fi

step "API latency: ${#ROUTES[@]} route(s), ${ROUNDS} round(s) at concurrency ${CONCURRENCY}"
bench_init api

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-api.XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT INT TERM

# The readiness gate, run before any timing. Benchmarking an API that is not up
# produces a page of not-measured samples and a confusing verdict; saying so
# once, here, is clearer. This is also the only thing this suite needs from the
# platform -- it reads no Firestore of its own.
READY_FIELDS=""
if ! READY_FIELDS="$(bench_request GET /readyz "${WORK}/readyz.json")"; then
  die "curl could not reach $(api_url)/readyz. That is a transport failure, not a slow API."
fi
READY_STATUS="${READY_FIELDS%% *}"
case "${READY_STATUS}" in
  2*) info "readyz: HTTP ${READY_STATUS}" ;;
  401|403) die "readyz answered HTTP ${READY_STATUS}: an auth/IAM problem, not a latency one." ;;
  *) die "readyz answered HTTP ${READY_STATUS}, not 2xx. Fix that before measuring latency." ;;
esac

# One concurrent burst against one route. Each worker writes its own line so
# nothing interleaves, and a worker whose curl failed writes the reason rather
# than nothing -- an absent file would shrink n and quietly improve the run.
burst() {
  local route="$1" round="$2" i fields
  for i in $(seq 1 "${CONCURRENCY}"); do
    (
      if fields="$(bench_request GET "${route}" "${WORK}/body-${round}-${i}")"; then
        printf '%s\n' "${fields}" >"${WORK}/r${round}-${i}"
      else
        printf 'TRANSPORT_FAILURE\n' >"${WORK}/r${round}-${i}"
      fi
      rm -f "${WORK}/body-${round}-${i}"
    ) &
  done
  wait
}

STATS_BODY=""
for route in "${ROUTES[@]}"; do
  labels="$(bench_label route "${route}")"
  info "${route}"
  for round in $(seq 1 "${ROUNDS}"); do
    burst "${route}" "${round}"
    for i in $(seq 1 "${CONCURRENCY}"); do
      line="$(cat "${WORK}/r${round}-${i}" 2>/dev/null || true)"
      rm -f "${WORK}/r${round}-${i}"
      if [[ "${line}" == "TRANSPORT_FAILURE" || -z "${line}" ]]; then
        bench_missing api.latency ms "curl could not complete the request" "${labels}"
        bench_missing api.ttfb ms "curl could not complete the request" "${labels}"
        continue
      fi
      status="$(printf '%s' "${line}" | awk '{print $1}')"
      total="$(printf '%s' "${line}" | awk '{print $2}')"
      ttfb="$(printf '%s' "${line}" | awk '{print $3}')"
      case "${status}" in
        2*)
          bench_sample api.latency ms "$(bench_ms "${total}")" "${labels}"
          bench_sample api.ttfb ms "$(bench_ms "${ttfb}")" "${labels}"
          ;;
        *)
          bench_missing api.latency ms "HTTP ${status}: an error answers fast and is not a measurement of this route" "${labels}"
          bench_missing api.ttfb ms "HTTP ${status}" "${labels}"
          ;;
      esac
    done
  done
done

# ---------------------------------------------------------------------------
# /v1/stats, normalised by history. See the header.
#
# Repeated ROUNDS times rather than sampled once. A single reading cannot be a
# percentile, and the engine says so (`insufficient_samples`) rather than
# quietly reporting p95 of one number -- which is how a benchmark ends up
# publishing a "p99" that is just whatever happened that one time. Sequential,
# not concurrent: this measures the route against a known history size, and
# three simultaneous count() aggregations measure contention instead.
if printf '%s\n' "${ROUTES[@]}" | grep -qx '/v1/stats'; then
  info "/v1/stats normalised by history (${ROUNDS} reading(s))"
  for round in $(seq 1 "${ROUNDS}"); do
    if ! FIELDS="$(bench_request GET /v1/stats "${WORK}/stats.json")"; then
      bench_missing api.stats.history_tasks tasks "curl could not complete the request" "$(bench_label route /v1/stats)"
      bench_missing api.stats.ms_per_1k_history ms "curl could not complete the request" "$(bench_label route /v1/stats)"
      continue
    fi
    status="$(printf '%s' "${FIELDS}" | awk '{print $1}')"
    total_ms="$(bench_ms "$(printf '%s' "${FIELDS}" | awk '{print $2}')")"
    if [[ "${status}" != 2* ]]; then
      bench_missing api.stats.history_tasks tasks "HTTP ${status}" "$(bench_label route /v1/stats)"
      bench_missing api.stats.ms_per_1k_history ms "HTTP ${status}" "$(bench_label route /v1/stats)"
      continue
    fi
    STATS_BODY="$(cat "${WORK}/stats.json")"
    # An ADMIN sees platform_tasks_by_state, which is the count the route really
    # scans. A non-admin sees only their own tenant's, which understates the
    # scan and would make the normalised figure look better than it is -- so
    # which one was used is a LABEL, not a silent choice. The two are different
    # metrics and must not share a baseline.
    scope="tenant"
    entries="$(printf '%s' "${STATS_BODY}" | jq -r '[.platform_tasks_by_state // {} | .[]] | add // empty')"
    if [[ -n "${entries}" && "${entries}" != "null" ]]; then
      scope="platform"
    else
      entries="$(printf '%s' "${STATS_BODY}" | jq -r '[.tasks_by_state // {} | .[]] | add // empty')"
    fi
    slabels="$(bench_label route /v1/stats scope "${scope}")"
    if [[ -z "${entries}" || "${entries}" == "null" ]]; then
      bench_missing api.stats.history_tasks tasks \
        "the response carried no tasks_by_state, so there is no history size to divide by" "${slabels}"
      bench_missing api.stats.ms_per_1k_history ms \
        "no history count to normalise by" "${slabels}"
      continue
    fi
    bench_sample api.stats.history_tasks tasks "${entries}" "${slabels}"
    # Guarded: a brand-new environment holds zero tasks, and dividing by it
    # would publish an infinity wearing a latency's units.
    if [[ "${entries}" -gt 0 ]]; then
      per1k="$(awk -v ms="${total_ms}" -v n="${entries}" 'BEGIN { printf "%.3f", ms / (n / 1000.0) }')"
      bench_sample api.stats.ms_per_1k_history ms "${per1k}" "${slabels}"
    else
      bench_missing api.stats.ms_per_1k_history ms \
        "the collection holds zero tasks, so latency per 1k of history is undefined" "${slabels}"
    fi
  done
fi

bench_finish
