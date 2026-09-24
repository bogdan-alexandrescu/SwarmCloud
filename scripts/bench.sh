#!/usr/bin/env bash
# The benchmark gate: run the collectors, compare against baselines, fail loudly.
#
# WHAT A BENCHMARK IS FOR HERE
# ----------------------------
# The correctness suites answer "did it work". They cannot see the run that
# spent three minutes nine seconds in Cloud Run cold start to do eighteen
# seconds of work, because that run succeeded. These answer "what did it cost",
# in time and in money, against a recorded baseline.
#
# SUITES, AND WHAT EACH COSTS TO RUN
# ----------------------------------
#   api         ~30s   read-only GETs. Cents at most. Safe anywhere.
#   reconcile   ~10s   reads Cloud Logging. Triggers NOTHING. Cents.
#   cost        ~15s   read-only. Needs an environment with history.
#   ui           ~1m   drives a browser at a UI you are already running. Free.
#   dispatch    ~2m    SUBMITS TASKS on --profile (mock by default), cancels
#                      them on exit. Cents on mock. On claude-code it spends
#                      real provider tokens.
#   admission   ~3m    SUBMITS a 150-task backlog on mock, cancels on exit.
#                      Cents. On claude-code it spends real provider tokens.
#
# `--suites api,reconcile,cost` is the read-only set: nothing submitted,
# nothing created, safe to run against production. That is the default for
# `--read-only`.
#
# A BASELINE IS PART OF THE BENCHMARK
# -----------------------------------
# A number with nothing to compare it to is a number nobody acts on. Baselines
# live in benchmarks/baselines/<environment>.json and are checked in, so the
# comparison is reproducible on someone else's machine. Record one with
# --record-baseline once you believe a run; until then every metric reports as
# `new` and the run says plainly that NOTHING WAS COMPARED.
#
# Usage:
#   scripts/bench.sh --suites api,ui
#   scripts/bench.sh --read-only
#   scripts/bench.sh --suites api --record-baseline
#   scripts/bench.sh --self-test          # offline; no cloud, no browser

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ALL_SUITES="api reconcile cost ui dispatch admission"
READ_ONLY_SUITES="api reconcile cost"
SUITES=""
RECORD=0
SELF_TEST=0
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --suites)          SUITES="$(printf '%s' "$2" | tr ',' ' ')"; shift 2 ;;
    --read-only)       SUITES="${READ_ONLY_SUITES}"; shift ;;
    --record-baseline) RECORD=1; shift ;;
    --self-test)       SELF_TEST=1; shift ;;
    --)                shift; EXTRA=("$@"); break ;;
    -h|--help)         sed -n '2,45p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# ---------------------------------------------------------------------------
# Self-test: the whole analysis pipeline, offline, with no cloud and no
# browser. Wired into `make test` so the engine cannot rot the way
# tests/integration did for 95 commits.
#
# It exercises the paths a collector cannot reach on a laptop: the percentile
# engine, the not-measured semantics, the baseline comparison, and the UI
# collector's capture-to-samples half (through --from-json, which exists for
# exactly this).
if [[ "${SELF_TEST}" -eq 1 ]]; then
  require_cmd jq python3
  step "Benchmark self-test (offline)"

  info "benchstat engine assertions"
  python3 "${REPO_ROOT}/scripts/benchstat.py" self-test

  WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-selftest.XXXXXX")"
  trap 'rm -rf "${WORK}"' EXIT INT TERM

  # --- A run whose every metric is within threshold passes. ----------------
  : >"${WORK}/good.jsonl"
  for i in 1 2 3 4 5 6 7 8 9 10; do
    jq -nc --argjson v "${i}" '{metric:"selftest.latency", unit:"ms", value:($v * 10), labels:{}}' \
      >>"${WORK}/good.jsonl"
  done
  python3 "${REPO_ROOT}/scripts/benchstat.py" summarize \
    --input "${WORK}/good.jsonl" --environment selftest --output "${WORK}/good.json"
  python3 "${REPO_ROOT}/scripts/benchstat.py" baseline \
    --current "${WORK}/good.json" --output "${WORK}/base.json" 2>/dev/null
  if ! python3 "${REPO_ROOT}/scripts/benchstat.py" compare \
        --current "${WORK}/good.json" --baseline "${WORK}/base.json" >/dev/null; then
    die "self-test: a run compared against itself must PASS, and did not"
  fi
  ok "an unchanged run passes"

  # --- The same run, ten times slower, must FAIL. --------------------------
  : >"${WORK}/slow.jsonl"
  for i in 1 2 3 4 5 6 7 8 9 10; do
    jq -nc --argjson v "${i}" '{metric:"selftest.latency", unit:"ms", value:($v * 100), labels:{}}' \
      >>"${WORK}/slow.jsonl"
  done
  python3 "${REPO_ROOT}/scripts/benchstat.py" summarize \
    --input "${WORK}/slow.jsonl" --environment selftest --output "${WORK}/slow.json"
  if python3 "${REPO_ROOT}/scripts/benchstat.py" compare \
       --current "${WORK}/slow.json" --baseline "${WORK}/base.json" >/dev/null; then
    die "self-test: a 10x regression must FAIL the gate, and did not"
  fi
  ok "a 10x regression fails"

  # --- A collector that measured NOTHING must FAIL, not pass fast. ---------
  jq -nc '{metric:"selftest.latency", unit:"ms", value:null, not_measured:"the collector could not read it", labels:{}}' \
    >"${WORK}/none.jsonl"
  python3 "${REPO_ROOT}/scripts/benchstat.py" summarize \
    --input "${WORK}/none.jsonl" --environment selftest --output "${WORK}/none.json"
  if python3 "${REPO_ROOT}/scripts/benchstat.py" compare \
       --current "${WORK}/none.json" --baseline "${WORK}/base.json" >/dev/null; then
    die "self-test: a run that measured nothing must FAIL the gate, and did not"
  fi
  ok "a run that measured nothing fails"

  # --- The event decomposition, on the real 2026-09-22 timings. ------------
  jq -nc '{labels:{runner_profile:"claude-code", backend:"cloud_run"},
           events:[{type:"submitted", at:"2026-09-22T03:46:00Z"},
                   {type:"dispatched", at:"2026-09-22T03:46:05Z"},
                   {type:"starting",  at:"2026-09-22T03:49:14Z"},
                   {type:"running",   at:"2026-09-22T03:49:14Z"},
                   {type:"succeeded", at:"2026-09-22T03:49:32Z"}]}' \
    >"${WORK}/events.jsonl"
  python3 "${REPO_ROOT}/scripts/benchstat.py" segments \
    --input "${WORK}/events.jsonl" --output "${WORK}/segments.jsonl"
  # Compared as a NUMBER. jq prints the float as "189.0" and a string
  # comparison against "189" fails -- which is a self-test that reports a
  # correct engine as broken, the opposite of the failure this file guards
  # against but just as useless.
  cold="$(jq -r 'select(.metric == "dispatch.cold_start") | .value' "${WORK}/segments.jsonl")"
  [[ "$(awk -v c="${cold}" 'BEGIN { print (c == 189) ? "yes" : "no" }')" == "yes" ]] \
    || die "self-test: cold start decomposed to '${cold}', expected 189"
  ok "the recorded 3m09s cold start decomposes to 189s"

  # --- The UI collector's capture-to-samples half, with no browser. --------
  #
  # A fixture run makes NO /v1 request. It must come out as not-measured with
  # that reason, never as a screen that fetched nothing very quickly.
  jq -nc '{screen:"#overview/now", ok:true, nonce:"selftest",
           nav:{ttfb_ms:4.2, dom_content_loaded_ms:35.9, load_ms:41.2},
           first_contentful_paint_ms:76, api:[]}' >"${WORK}/capture.jsonl"
  if SWARM_ENV_FILE=/dev/null ENVIRONMENT=selftest \
     BENCH_BASELINE="${WORK}/ui-base.json" BENCH_THRESHOLDS="${WORK}/none.thresholds" \
     "${REPO_ROOT}/scripts/bench-ui.sh" --from-json "${WORK}/capture.jsonl" \
     >"${WORK}/ui.log" 2>&1; then
    die "self-test: a fixture capture with no /v1 request must FAIL the gate, and did not"
  fi
  grep -q 'issued no same-origin /v1 request' "${WORK}/ui.log" \
    || { redact <"${WORK}/ui.log" >&2; die "self-test: the fixture capture failed for the wrong reason"; }
  ok "a fixture UI capture reports its API metrics as not measured"

  hr
  ok "benchmark self-test passed"
  exit 0
fi

# ---------------------------------------------------------------------------
load_env
[[ -n "${SUITES}" ]] || SUITES="${READ_ONLY_SUITES}"

for suite in ${SUITES}; do
  case " ${ALL_SUITES} " in
    *" ${suite} "*) ;;
    *) die "unknown suite '${suite}'; choose from: ${ALL_SUITES}" ;;
  esac
done

step "Benchmarks: ${SUITES} (${ENVIRONMENT} / ${PROJECT_ID})"
[[ "${RECORD}" -eq 0 ]] || warn "recording baselines: this run's numbers BECOME the reference"

FAILED=()
PASSED=()
for suite in ${SUITES}; do
  rc=0
  if [[ "${RECORD}" -eq 1 ]]; then
    BENCH_RECORD_BASELINE=1 "${REPO_ROOT}/scripts/bench-${suite}.sh" ${EXTRA[@]+"${EXTRA[@]}"} || rc=$?
  else
    "${REPO_ROOT}/scripts/bench-${suite}.sh" ${EXTRA[@]+"${EXTRA[@]}"} || rc=$?
  fi
  if [[ "${rc}" -eq 0 ]]; then PASSED+=("${suite}"); else FAILED+=("${suite}"); fi
done

hr
# Every suite runs even when an earlier one fails, and the tally is printed
# whatever happened. Stopping at the first failure would hide the rest, and a
# performance report that stops early is one nobody can act on.
[[ "${#PASSED[@]}" -eq 0 ]] || ok "within threshold: ${PASSED[*]}"
if [[ "${#FAILED[@]}" -gt 0 ]]; then
  die "${#FAILED[@]} suite(s) over threshold or not measured: ${FAILED[*]}"
fi
ok "every benchmark suite is within threshold"
