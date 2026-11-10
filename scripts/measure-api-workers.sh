#!/usr/bin/env bash
#
# measure-api-workers: does swarm-api serve more with two uvicorn workers per
# instance than with one? (the owner's follow-up of 2026-11-06, after
# swarm-api moved to 2 vCPU instances; the image's count is
# SWARM_API_WORKERS, default 1, in images/swarm-api/Dockerfile).
#
# OFFLINE. The server is swarm-api's own create_app() over the unit-test fakes
# (tests/perf/swarm_api_workers_app.py): an in-memory Firestore, a static token
# verifier, no forge credential. No cloud, no credentials, no emulator. So the
# numbers are the API's own CPU per read -- the part a second worker can run
# in parallel -- and not Firestore's network round trip, which a second worker
# does not shorten.
#
# METHOD. For each round, each worker count and each concurrency: start
# `uvicorn --factory --workers N` pinned to --server-cpus (default 0,1: the
# instance's 2 vCPU), drive the console's hottest reads at it from client
# processes pinned to --client-cpus for --warmup + --duration seconds
# (tests/perf/drive_reads.py, closed loop), stop it, and record requests per
# second and p50/p95/p99 latency. Rounds alternate 1 and 2 workers so drift in
# the machine falls on both.
#
# Usage:
#   scripts/measure-api-workers.sh [--workers "1 2"] [--concurrency "8 32"]
#       [--duration 30] [--warmup 5] [--rounds 2] [--server-cpus 0,1]
#       [--client-cpus 2-5] [--out FILE]
#
# Writes one JSON line per run to --out (default
# $SWARM_ARTIFACTS_DIR/workers-measure.jsonl, else build/workers-measure.jsonl)
# and prints a Markdown table on stdout. The bearer token is drawn at run time
# and never printed.
set -euo pipefail

# This script touches no project: it runs swarm-api locally over fakes. It
# says so, so common.sh does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

worker_counts="1 2"
concurrencies="8 32"
duration=30
warmup=5
rounds=2
server_cpus="0,1"
client_cpus="2-5"
out="${SWARM_ARTIFACTS_DIR:-${BUILD_DIR}}/workers-measure.jsonl"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers) worker_counts="$2"; shift 2 ;;
    --concurrency) concurrencies="$2"; shift 2 ;;
    --duration) duration="$2"; shift 2 ;;
    --warmup) warmup="$2"; shift 2 ;;
    --rounds) rounds="$2"; shift 2 ;;
    --server-cpus) server_cpus="$2"; shift 2 ;;
    --client-cpus) client_cpus="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    -h|--help) sed -n '2,31p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

# Word lists from the flags, split explicitly (zsh callers quote them).
read -r -a WORKER_LIST <<< "${worker_counts}"
read -r -a CONC_LIST <<< "${concurrencies}"
[[ ${#WORKER_LIST[@]} -gt 0 ]] || die "--workers names no worker count"
[[ ${#CONC_LIST[@]} -gt 0 ]] || die "--concurrency names no concurrency"
for n in "${WORKER_LIST[@]}" "${CONC_LIST[@]}" "${duration}" "${rounds}"; do
  [[ "${n}" =~ ^[0-9]+$ && "${n}" -gt 0 ]] || die "not a positive whole number: ${n}"
done
[[ "${warmup}" =~ ^[0-9]+$ ]] || die "--warmup is not a whole number of seconds: ${warmup}"

require_cmd setsid
read -r -a PY <<< "$(swarm_python)"
PIN_SERVER=()
PIN_CLIENT=()
if command -v taskset >/dev/null 2>&1; then
  PIN_SERVER=(taskset -c "${server_cpus}")
  PIN_CLIENT=(taskset -c "${client_cpus}")
else
  warn "taskset not found: server and client share every CPU, so the numbers are not a 2 vCPU instance's"
fi

export PYTHONPATH="${REPO_ROOT}/tests/unit:${REPO_ROOT}/tests/perf${PYTHONPATH:+:${PYTHONPATH}}"
# Drawn here, at run time, never stored: read, not assigned, so the value
# exists only in this process's environment.
read -r SWARM_PERF_TOKEN < <("${PY[@]}" -c 'import secrets; print(secrets.token_hex(24))')
export SWARM_PERF_TOKEN
port="$("${PY[@]}" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
base="http://127.0.0.1:${port}"

mkdir -p "$(dirname -- "${out}")"
: > "${out}"
server_log="$(mktemp "${TMPDIR:-/tmp}/measure-api-workers.XXXXXX")"
server_pid=""

stop_server() {
  if [[ -n "${server_pid}" ]]; then
    kill -TERM -- "-${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
    server_pid=""
  fi
}
cleanup() { stop_server; rm -f "${server_log}"; }
trap cleanup EXIT INT TERM

runs=0
round=1
while [[ "${round}" -le "${rounds}" ]]; do
  order=("${WORKER_LIST[@]}")
  if (( round % 2 == 0 )); then
    order=()
    for (( i = ${#WORKER_LIST[@]} - 1; i >= 0; i-- )); do order+=("${WORKER_LIST[i]}"); done
  fi
  for workers in "${order[@]}"; do
    for conc in "${CONC_LIST[@]}"; do
      step "round ${round}: ${workers} worker(s), ${conc} in flight, ${duration}s"
      # setsid: the server leads its own process group, so stopping it stops
      # every worker uvicorn forked, not only the parent.
      setsid "${PIN_SERVER[@]+"${PIN_SERVER[@]}"}" "${PY[@]}" -m uvicorn --factory \
        swarm_api_workers_app:create_app --host 127.0.0.1 --port "${port}" \
        --workers "${workers}" --log-level warning --no-access-log >"${server_log}" 2>&1 &
      server_pid=$!
      if ! result="$("${PIN_CLIENT[@]+"${PIN_CLIENT[@]}"}" "${PY[@]}" "${REPO_ROOT}/tests/perf/drive_reads.py" \
          --base "${base}" --concurrency "${conc}" --duration "${duration}" --warmup "${warmup}")"; then
        err "the run failed; the server said:"
        tail -n 40 "${server_log}" | redact >&2
        exit 1
      fi
      stop_server
      printf '%s\n' "${result}" \
        | "${PY[@]}" -c 'import json, sys; r = json.loads(sys.stdin.read()); r.pop("mix"); r["workers"] = int(sys.argv[1]); r["round"] = int(sys.argv[2]); print(json.dumps(r))' \
          "${workers}" "${round}" >> "${out}"
      runs=$((runs + 1))
      ok "$(tail -n 1 "${out}")"
    done
  done
  round=$((round + 1))
done

info "${runs} run(s) recorded in ${out}"
"${PY[@]}" - "${out}" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
print("| round | workers | in flight | requests/s | p50 ms | p95 ms | p99 ms | non-200 |")
print("|---:|---:|---:|---:|---:|---:|---:|---:|")
for r in sorted(rows, key=lambda r: (r["concurrency"], r["workers"], r["round"])):
    print(f"| {r['round']} | {r['workers']} | {r['concurrency']} | {r['rps']} | {r['p50_ms']} | "
          f"{r['p95_ms']} | {r['p99_ms']} | {r['non_200']} |")
PY
