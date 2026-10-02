#!/usr/bin/env bash
# Prompt-cache TTL: what the cache cost on the 5-minute TTL, and what the
# 1-hour TTL would have cost, from the attempts that already ran. (#323)
#
# WHY A TABLE AND NOT A SWITCH
# ----------------------------
# A 1-hour cache write costs 2x the base input price against 1.25x for the
# default 5 minutes, so the longer TTL pays only when it turns enough writes
# into reads: at a 0.1x read, 39.5% of them (benchstat.py
# `cache_ttl_break_even`). Whether this platform's agents clear that bar
# depends on gaps between requests that share a prefix, and the platform
# records only each attempt's TOTAL cache writes and reads. So this prints
# what can be read -- the cost as billed, the cost at 2x with no re-use, and
# the share of writes made by attempts that resumed 5-60 minutes after the
# previous one ended -- and says plainly that gaps inside an attempt are not
# visible. It does not change any setting and is not a gate.
#
# THE READ PRICE IS PER MODEL. 0.1x on most models, 0.05x on Claude Opus 5.5,
# 0.025x on Claude Fable 5.1. Pass --read-multiplier for the model the profile
# runs. Dollars appear only with --base-usd-per-mtok; no price is kept here.
#
# Cost to run: read-only, one GET for the task list and one per terminal task,
# against swarm-api. Creates nothing, spends no provider tokens.
#
# Usage: scripts/bench-cache-ttl.sh [--limit 200] [--profile claude-code]
#          [--read-multiplier 0.1] [--base-usd-per-mtok N] [--json]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/benchlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/benchlib.sh"

load_env

LIMIT=200
PROFILE=""
TABLE_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)             LIMIT="$2"; shift 2 ;;
    --profile)           PROFILE="$2"; shift 2 ;;
    --read-multiplier)   TABLE_ARGS+=(--read-multiplier "$2"); shift 2 ;;
    --base-usd-per-mtok) TABLE_ARGS+=(--base-usd-per-mtok "$2"); shift 2 ;;
    --json)              TABLE_ARGS+=(--json); shift ;;
    -h|--help)           sed -n '2,27p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd jq curl python3
step "Prompt-cache TTL cost table${PROFILE:+ for ${PROFILE}} (newest ${LIMIT} tasks)"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-cache-ttl.XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT INT TERM

if ! FIELDS="$(bench_request GET "/v1/tasks?limit=${LIMIT}" "${WORK}/tasks.json")"; then
  die "curl could not reach $(api_url)/v1/tasks. That is a transport failure, not a platform with no cache spend."
fi
[[ "${FIELDS%% *}" == 2* ]] || die "GET /v1/tasks answered HTTP ${FIELDS%% *}. An error is not an empty task list."

# Terminal tasks with at least one attempt: the ones whose spend is final.
jq -r '[.tasks // [] | .[]
        | select(.state == "SUCCEEDED" or .state == "FAILED" or .state == "DEAD_LETTERED")
        | select(.attempt_count != null and .attempt_count > 0)]
       | .[] | [.id, (.runner_profile // "unknown")] | @tsv' \
  "${WORK}/tasks.json" >"${WORK}/candidates.tsv"

ATTEMPTS="${WORK}/attempts.jsonl"
: >"${ATTEMPTS}"
VISITED=0
UNREADABLE=0
while IFS=$'\t' read -r task_id profile; do
  [[ -n "${task_id}" ]] || continue
  if [[ -n "${PROFILE}" && "${profile}" != "${PROFILE}" ]]; then continue; fi
  VISITED=$(( VISITED + 1 ))
  if ! AF="$(bench_request GET "/v1/tasks/${task_id}/attempts" "${WORK}/att.json")" \
     || [[ "${AF%% *}" != 2* ]]; then
    UNREADABLE=$(( UNREADABLE + 1 ))
    continue
  fi
  jq -c --arg t "${task_id}" --arg p "${profile}" \
    '{task_id: $t, runner_profile: $p, attempts: (.attempts // [])}' \
    "${WORK}/att.json" >>"${ATTEMPTS}"
done <"${WORK}/candidates.tsv"

info "${VISITED} terminal task(s) visited; ${UNREADABLE} whose attempts could not be read"
if [[ "${UNREADABLE}" -gt 0 ]]; then
  warn "${UNREADABLE} task(s) are missing from the table below; it is not the whole history"
fi
[[ -s "${ATTEMPTS}" ]] || die "no terminal task's attempts were read; there is nothing to price (that is not a zero cost)"

benchstat cache-ttl --input "${ATTEMPTS}" ${TABLE_ARGS[@]+"${TABLE_ARGS[@]}"}
