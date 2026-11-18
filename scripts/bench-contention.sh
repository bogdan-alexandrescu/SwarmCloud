#!/usr/bin/env bash
# Admission contention: the real frozen admission against a BENCH database.
#
# WHAT IS BEING MEASURED
# ----------------------
# Spec §8 row 6 (docs/BUILD_PROMPT_V2.md:740): "Firestore contention at 100
# admissions/min? Measure before designing the sharding." Every admission
# reads and writes `global` plus four to six narrower pools in ONE transaction
# (CONTRACT.md invariant 2), and Firestore shows contention on a document at
# roughly one write per second. Contention surfaces as Firestore aborting the
# commit and `firestore.transactional` re-running the body, and as latency.
# `SchedulerStore.acquire_lease` now logs both per admission; this suite drives
# that method at offered rates of 100, 200 and 400 admissions per minute and
# reports p50/p95/p99 admission latency, re-runs and aborts, with n.
#
# `bench-admission.sh` cannot answer this: it measures the deployed scheduler
# draining a backlog, deliberately changes no pools, and the scheduler's own
# drain loop (one instance admits serially) caps the rate it can offer.
#
# WHERE IT RUNS, AND WHY NOWHERE ELSE (owner decision OD-B17-1, 2026-10-02)
# -------------------------------------------------------------------------
# Only against a DEDICATED bench Firestore database (`swarm-bench` or
# `swarm-bench-<suffix>`). The harness writes pool documents named `global`,
# `tenant:...` and so on -- the names the frozen `pool_names_for()` returns --
# so against the live `swarm` database it would overwrite live limits and
# counts. It refuses the environment's FIRESTORE_DATABASE, `swarm`, `(default)`
# (another team's, in a shared project) and anything else not bench-named,
# HERE and again in Python, before any client is built.
#
# The bench database must have the SAME concurrency mode as the live one
# (terraform/modules/firestore `concurrency_mode`), or the result describes a
# different locking model. It is not created by this script: an unlabelled
# database made by hand is one nobody can safely delete later.
#
# COST TO RUN
# -----------
# Defaults: 60 s per rate, so 100 + 200 + 400 = 700 admissions and 700
# releases, about 3.5 minutes. Each admission is ~8 document reads and ~9
# writes; the whole run is a few thousand operations, well under a dollar.
# Touches no live pool, no task, no API. Needs Application Default
# Credentials allowed to read and write the bench database.
#
# Usage: scripts/bench-contention.sh [--database swarm-bench] [--rates 100,200,400]
#          [--duration 60] [--admitters 8] [--provider anthropic|none]
#          [--no-release] [--keep] [--break-lock]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/benchlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/benchlib.sh"

load_env

DATABASE="${BENCH_FIRESTORE_DATABASE:-swarm-bench}"
RATES="100,200,400"
DURATION=60
# One scheduler instance admits serially, so admitters stand in for scheduler
# instances. 8 is enough to OFFER 400/min at up to ~1 s per admission plus its
# release; a row the harness could not offer is flagged `rate_not_offered`.
ADMITTERS=8
PROVIDER="anthropic"
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --database)   DATABASE="$2"; shift 2 ;;
    --rates)      RATES="$2"; shift 2 ;;
    --duration)   DURATION="$2"; shift 2 ;;
    --admitters)  ADMITTERS="$2"; shift 2 ;;
    --provider)   PROVIDER="$2"; shift 2 ;;
    --no-release) EXTRA+=(--no-release); shift ;;
    --keep)       EXTRA+=(--keep); shift ;;
    --break-lock) EXTRA+=(--break-lock); shift ;;
    -h|--help)    sed -n '2,46p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# The guard, before anything that could reach Firestore.
if [[ "${DATABASE}" == "${FIRESTORE_DATABASE}" || "${DATABASE}" == "swarm" \
      || "${DATABASE}" == "(default)" ]] || is_shared_resource "${DATABASE}"; then
  die "refusing ${DATABASE}: it is a live database. This suite overwrites pool documents and runs only against a dedicated bench database (swarm-bench[-suffix])."
fi
if [[ ! "${DATABASE}" =~ ^swarm-bench(-[a-z0-9]+)*$ ]]; then
  die "refusing ${DATABASE}: not a bench database name (expected swarm-bench or swarm-bench-<suffix>)."
fi

step "Admission contention on ${PROJECT_ID}/${DATABASE}: ${RATES} per minute, ${DURATION}s each, ${ADMITTERS} admitters"
bench_init contention

# `swarm_python` prints a command line ("uv run --project <root> python" or a
# venv path), so it is split into an array rather than expanded unquoted.
read -r -a PYTHON <<<"$(swarm_python)"
REPORT="${BENCH_SAMPLES%.jsonl}-contention.json"

"${PYTHON[@]}" "${REPO_ROOT}/scripts/bench_contention.py" \
  --project "${PROJECT_ID}" \
  --database "${DATABASE}" \
  --live-database "${FIRESTORE_DATABASE}" \
  --rates "${RATES}" \
  --duration "${DURATION}" \
  --admitters "${ADMITTERS}" \
  --provider "${PROVIDER}" \
  --samples "${BENCH_SAMPLES}" \
  --report "${REPORT}" \
  ${EXTRA[@]+"${EXTRA[@]}"} 2>&1 | redact

bench_recount
info "table rows -> ${REPORT}"

# SUMMARISED, NOT GATED: no `bench_finish`. This is a one-off measurement that
# feeds a design decision, not a regression check, and benchstat's `compare`
# cannot judge it. `contention.reruns` and `contention.aborted` are 0 at p95
# on a healthy run, and a baseline of 0 makes every later ratio infinite --
# "regressed" at 0 again. `contention.achieved_rate_per_min` is one sample per
# rate, under every `min_samples`. The verdict is the table above, read
# against the rule in docs/scaling.md §5.
SUMMARY="${BENCH_SAMPLES%.jsonl}-summary.json"
hr
info "${BENCH_TAKEN} sample(s) measured, ${BENCH_MISSED} recorded as not measured"
benchstat summarize --input "${BENCH_SAMPLES}" --environment "${ENVIRONMENT:-dev}" --output "${SUMMARY}"
info "summary -> ${SUMMARY}"
if [[ "${BENCH_MISSED}" -gt 0 ]]; then
  err "${BENCH_MISSED} sample(s) were not measured; read their reasons in ${BENCH_SAMPLES} before quoting this run"
  exit 1
fi
