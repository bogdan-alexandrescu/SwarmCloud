#!/usr/bin/env bash
# Shared harness for the bench-*.sh collectors. Sourced after lib/common.sh.
#
# WHAT A COLLECTOR MAY AND MAY NOT DO
# -----------------------------------
# A collector MEASURES. It appends one JSON line per sample and it never
# decides anything -- no thresholds, no percentiles, no pass, no fail. The
# verdict is `scripts/benchstat.py compare`, which is unit-tested offline
# against a baseline. This split is deliberate: a collector that could also
# report a pass is a collector that can report a pass having measured nothing,
# which is the failure this whole repository keeps producing.
#
# THE ONE RULE
# ------------
# A measurement that could not be taken is recorded with `bench_missing`, which
# writes `"value": null` and a REASON. It is never recorded as 0 and never
# silently skipped. `benchstat.py` fails a metric whose samples are all
# missing, so a collector that breaks turns the run red instead of setting a
# new record. `bench_sample` refuses a non-numeric value for the same reason --
# an unread shell variable arrives as the empty string, and letting "" through
# is how `[[ "" -le N ]]` became a green assertion in testlib.sh.
#
# bash 3.2.57 (what macOS ships): no associative arrays, no mapfile, no
# ${var,,}. Possibly-empty arrays expand as ${arr[@]+"${arr[@]}"}.

set -euo pipefail

#: Where samples land. One file per RUN, created empty by bench_init.
BENCH_SAMPLES="${BENCH_SAMPLES:-}"
BENCH_SUITE="${BENCH_SUITE:-bench}"
#: Counted so a suite can say how much it measured and how much it could not.
BENCH_TAKEN=0
BENCH_MISSED=0

#: Baselines and thresholds are checked into the repository, per environment,
#: because a baseline that lives only on the machine that recorded it is a
#: number nobody else can act on.
BENCH_DIR="${BENCH_DIR:-${REPO_ROOT}/benchmarks}"
BENCH_BASELINE="${BENCH_BASELINE:-${BENCH_DIR}/baselines/${ENVIRONMENT:-dev}.json}"
BENCH_THRESHOLDS="${BENCH_THRESHOLDS:-${BENCH_DIR}/thresholds.json}"

benchstat() {
  # python3 explicitly, not swarm_python: benchstat.py imports nothing outside
  # the standard library on purpose, so it runs in the verify image (which has
  # no uv, no venv and no swarm_common) exactly as it runs here.
  python3 "${REPO_ROOT}/scripts/benchstat.py" "$@"
}

# bench_init SUITE_NAME
bench_init() {
  BENCH_SUITE="$1"
  require_cmd jq curl python3
  mkdir -p "${BUILD_DIR}"
  # ONE FILE PER RUN, not per suite and environment. `: >` below truncates, and
  # this path used to be built from the suite and ENVIRONMENT alone, in the
  # checkout's shared build/ -- so a second run of the same suite started while
  # the first was still measuring erased the first one's samples. The first
  # then summarised whatever it wrote afterwards, plus the second run's
  # samples, and reported that as its own. That is the bench "flake" in
  # docs/audits/2026-09-23/two-flaky-gate-tests.md #1: a baseline holding only
  # the two /v1/stats metrics bench-api.sh writes LAST. It needed two runs in
  # one checkout, which is why CI -- one process, one case at a time -- never
  # saw it. `$$` is unique among running processes, which is the only
  # uniqueness this needs; a run that reuses a finished run's PID truncating
  # that file is correct.
  BENCH_SAMPLES="${BUILD_DIR}/bench-${BENCH_SUITE}-${ENVIRONMENT:-dev}.$$.jsonl"
  : >"${BENCH_SAMPLES}"
  BENCH_TAKEN=0
  BENCH_MISSED=0
  info "samples -> ${BENCH_SAMPLES}"
}

# bench_sample METRIC UNIT VALUE [LABELS_JSON]
bench_sample() {
  local metric="$1" unit="$2" value="$3" labels="${4:-{\}}"
  [[ -n "${BENCH_SAMPLES}" ]] || die "bench_sample before bench_init"
  # An unread value is not a measured one. This is the guard that makes the
  # honesty rule mechanical rather than a matter of every caller remembering.
  if [[ -z "${value}" || ! "${value}" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
    bench_missing "${metric}" "${unit}" \
      "the collector produced '${value}', which is not a number" "${labels}"
    return 0
  fi
  jq -nc --arg m "${metric}" --arg u "${unit}" \
    --argjson v "${value}" --argjson l "${labels}" \
    '{metric:$m, unit:$u, value:$v, labels:$l}' >>"${BENCH_SAMPLES}"
  BENCH_TAKEN=$(( BENCH_TAKEN + 1 ))
}

# bench_missing METRIC UNIT REASON [LABELS_JSON]
bench_missing() {
  local metric="$1" unit="$2" reason="$3" labels="${4:-{\}}"
  [[ -n "${BENCH_SAMPLES}" ]] || die "bench_missing before bench_init"
  jq -nc --arg m "${metric}" --arg u "${unit}" --arg r "${reason}" --argjson l "${labels}" \
    '{metric:$m, unit:$u, value:null, not_measured:$r, labels:$l}' >>"${BENCH_SAMPLES}"
  BENCH_MISSED=$(( BENCH_MISSED + 1 ))
  dim "  not measured: ${metric} -- ${reason}"
}

# bench_label KEY VALUE [KEY VALUE ...] -> a labels JSON object on stdout.
#
# bash 3.2 has no associative arrays, so labels are built as pairs rather than
# as a map. jq --args keeps every value out of the shell's own quoting.
bench_label() {
  local out='{}' k v
  while [[ $# -gt 1 ]]; do
    k="$1"; v="$2"; shift 2
    out="$(jq -nc --argjson o "${out}" --arg k "${k}" --arg v "${v}" '$o + {($k): $v}')"
  done
  printf '%s' "${out}"
}

# bench_request METHOD PATH BODY_OUT [REQUEST_BODY]
#   stdout:   "STATUS TOTAL_S TTFB_S"
#   BODY_OUT: a path THE CALLER chooses, where the response body is written.
#   returns:  non-zero if curl could not complete the request at all.
#
# NOT api_request: that one reports only a body and a status, and the timing is
# the whole point here. Same auth discipline though -- the token reaches curl
# through `-K -` on stdin and never through argv, for the reason auth_config
# documents.
#
# WHY THE BODY PATH IS AN ARGUMENT AND NOT A VARIABLE THIS FUNCTION SETS.
# Because callers read the timing as `fields="$(bench_request ...)"`, and a
# command substitution is a SUBSHELL: any variable this function assigned would
# die with it, and the caller would read whatever the variable held before the
# call. That is the trap CLAUDE.md documents and testlib.sh's submit_task
# carries a comment about; the first draft of this function fell into it
# anyway, and the symptom was `cat: : No such file or directory` rather than a
# wrong number -- which is the lucky version. The path being an argument makes
# the mistake unavailable.
bench_request() {
  bench_request_url "$1" "$(api_url)$2" "$3" "${4:-}"
}

# bench_request_url METHOD ABSOLUTE_URL BODY_OUT [REQUEST_BODY]
#
# The same thing against a service that is not swarm-api -- the reconciler, or
# a Cloud API. Separate because `api_url` is the control plane's address and
# quietly concatenating a full URL onto it produces a request to a host nobody
# meant to call. A relative fetch against the wrong origin has already cost
# this project a session.
bench_request_url() {
  local method="$1" url="$2" body_out="$3" body="${4:-}"
  local token out fields rc=0
  case "${url}" in
    https://*|http://*) ;;
    *) die "bench_request_url needs an absolute URL, got '${url}'" ;;
  esac
  token="$(id_token)"
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-bench-w.XXXXXX")"

  if [[ -n "${body}" ]]; then
    auth_config "${token}" | curl -sS -m "${HTTP_TIMEOUT}" -K - \
      -X "${method}" -H "Content-Type: application/json" --data-binary "${body}" \
      -o "${body_out}" -w '%{http_code} %{time_total} %{time_starttransfer}' \
      "${url}" >"${out}" 2>/dev/null || rc=$?
  else
    auth_config "${token}" | curl -sS -m "${HTTP_TIMEOUT}" -K - \
      -X "${method}" \
      -o "${body_out}" -w '%{http_code} %{time_total} %{time_starttransfer}' \
      "${url}" >"${out}" 2>/dev/null || rc=$?
  fi

  if [[ "${rc}" -ne 0 ]]; then
    rm -f "${out}"
    # A transport failure is not a slow request and not a fast one. The caller
    # records it with bench_missing; returning non-zero is what makes that the
    # only available branch.
    return 1
  fi
  fields="$(cat "${out}")"
  rm -f "${out}"
  printf '%s' "${fields}"
}

# bench_append_segments EVENTS_JSONL
#
# Turn one line per task -- `{"labels":{...},"events":[{"type","at"},...]}` --
# into segment samples, and append them to this suite's sample file.
#
# The decomposition lives in benchstat.py rather than in jq because it has to
# emit a NULL sample for a segment whose endpoint event never happened, and the
# natural jq for this (`map(select(.type==$t))|min`) emits nothing at all --
# which shrinks n and lets a task that never reached RUNNING improve the cold
# start figure it is absent from.
bench_append_segments() {
  local events="$1"
  [[ -n "${BENCH_SAMPLES}" ]] || die "bench_append_segments before bench_init"
  [[ -s "${events}" ]] || { warn "no event streams collected; nothing to decompose"; return 0; }
  benchstat segments --input "${events}" >>"${BENCH_SAMPLES}"
  bench_recount
}

# Re-derive the taken/missed counters from the sample file itself.
#
# Needed because bench_append_segments writes many samples in one go without
# going through bench_sample/bench_missing. Counting from the file rather than
# incrementing is also the version that cannot drift from what was actually
# written -- the counters are only ever a report, never an input to a verdict.
bench_recount() {
  BENCH_MISSED="$(jq -s '[.[] | select(.value == null)] | length' "${BENCH_SAMPLES}")"
  BENCH_TAKEN="$(jq -s '[.[] | select(.value != null)] | length' "${BENCH_SAMPLES}")"
}

# bench_ms SECONDS -> milliseconds, three decimals. curl reports seconds; every
# API threshold on this platform is quoted in milliseconds.
bench_ms() {
  printf '%s' "$(awk -v s="$1" 'BEGIN { printf "%.3f", s * 1000 }')"
}

# bench_now_ms -> wall clock in milliseconds.
#
# `date +%s%3N` is GNU-only and prints a literal "3N" on macOS's BSD date --
# which then parses as a number nobody notices. python3 is already required.
bench_now_ms() {
  python3 -c 'import time; print(int(time.time() * 1000))'
}

# bench_finish -- summarise, compare against the baseline, and set the exit code.
#
# The suite's exit code is benchstat's, not its own. A collector cannot pass a
# run it measured nothing in.
bench_finish() {
  local summary compare_rc=0
  [[ -n "${BENCH_SAMPLES}" ]] || die "bench_finish before bench_init"
  # Per run for the same reason as the samples: a summary another run can
  # overwrite between `summarize` and `compare` is a verdict on somebody
  # else's measurements.
  summary="${BENCH_SAMPLES%.jsonl}-summary.json"

  hr
  info "${BENCH_TAKEN} sample(s) measured, ${BENCH_MISSED} recorded as not measured"

  benchstat summarize --input "${BENCH_SAMPLES}" \
    --environment "${ENVIRONMENT:-dev}" --output "${summary}"
  info "summary -> ${summary}"

  if [[ "${BENCH_RECORD_BASELINE:-0}" == "1" ]]; then
    benchstat baseline --current "${summary}" --output "${BENCH_BASELINE}"
    ok "baseline recorded at ${BENCH_BASELINE}"
    return 0
  fi

  local args
  args=(compare --current "${summary}")
  # An ABSENT baseline is reported, not treated as a pass. benchstat marks
  # every metric `new` in that case, which is honest -- but the operator has to
  # be told that nothing was actually compared, or a first run looks like a
  # green gate.
  if [[ -f "${BENCH_BASELINE}" ]]; then
    args+=(--baseline "${BENCH_BASELINE}")
  else
    warn "no baseline at ${BENCH_BASELINE}: NOTHING WAS COMPARED."
    warn "record one with BENCH_RECORD_BASELINE=1 once you believe this run."
  fi
  [[ -f "${BENCH_THRESHOLDS}" ]] && args+=(--thresholds "${BENCH_THRESHOLDS}")

  benchstat "${args[@]}" || compare_rc=$?
  if [[ "${compare_rc}" -ne 0 ]]; then
    err "${BENCH_SUITE}: the gate failed -- see the verdicts above"
    return 1
  fi
  ok "${BENCH_SUITE}: every metric within threshold"
  return 0
}
