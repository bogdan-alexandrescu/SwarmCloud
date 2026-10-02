#!/usr/bin/env bash
# Cold start, measured from the tasks that already ran. (#363: measure first.)
#
# WHY THIS EXISTS BESIDE bench-dispatch.sh
# ----------------------------------------
# The first real `claude-code` run spent 3m09s between `dispatched` and
# `starting` against 18s of agent (docs/benchmarks.md). Before anything is
# changed to shorten that -- a smaller image, a warm pool, min instances --
# the owner's rule is to measure it, per runner profile and per backend, at
# p50/p95/p99 with n. `bench-dispatch.sh` measures it by SUBMITTING tasks,
# which on `claude-code` spends provider tokens and measures a handful of
# fresh runs. This reads the event log of the newest terminal tasks instead:
# no submission, no spend, and the cold starts real work actually paid.
#
# The decomposition is benchstat.py's (`segments`), the same one
# bench-dispatch.sh uses, so the two produce the same metrics --
# `dispatch.cold_start` (dispatched -> running) and its two halves -- and a
# task that never reached RUNNING is a NULL sample, never a fast one.
#
# LABELS. `runner_profile` comes from the task; `backend` from its first
# `lease_acquired` event's detail, which the scheduler writes. Cloud Run Jobs
# and GKE Autopilot never share a percentile. An event without a backend is
# labelled `unknown` rather than guessed from the profile catalogue, which is
# frozen contract data this script must not restate.
#
# Cost to run: read-only, one GET for the task list and one per terminal task.
#
# Usage: scripts/bench-coldstart.sh [--limit 200] [--profile claude-code]

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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)   LIMIT="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Cold start from history${PROFILE:+ on ${PROFILE}} (newest ${LIMIT} tasks)"
bench_init coldstart

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-coldstart.XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT INT TERM

if ! FIELDS="$(bench_request GET "/v1/tasks?limit=${LIMIT}" "${WORK}/tasks.json")"; then
  die "curl could not reach $(api_url)/v1/tasks. That is a transport failure, not a platform with no history."
fi
[[ "${FIELDS%% *}" == 2* ]] || die "GET /v1/tasks answered HTTP ${FIELDS%% *}. An error is not an empty task list."

# Tasks that were DISPATCHED at least once: a terminal state with an attempt.
# CANCELLED is left out. A task cancelled between DISPATCHED and RUNNING has
# an attempt but no cold start, and counting it would turn a user's
# cancellation into a missing sample that fails the run.
jq -r '[.tasks // [] | .[]
        | select(.state == "SUCCEEDED" or .state == "FAILED" or .state == "DEAD_LETTERED")
        | select(.attempt_count != null and .attempt_count > 0)]
       | .[] | [.id, (.runner_profile // "unknown")] | @tsv' \
  "${WORK}/tasks.json" >"${WORK}/candidates.tsv"

EVENTS="${WORK}/events.jsonl"
: >"${EVENTS}"
VISITED=0
while IFS=$'\t' read -r task_id profile; do
  [[ -n "${task_id}" ]] || continue
  if [[ -n "${PROFILE}" && "${profile}" != "${PROFILE}" ]]; then continue; fi
  VISITED=$(( VISITED + 1 ))
  if ! EF="$(bench_request GET "/v1/tasks/${task_id}/events?limit=200" "${WORK}/ev.json")" \
     || [[ "${EF%% *}" != 2* ]]; then
    # Not an empty stream: an unreadable one is a null sample for every
    # segment, which fails the gate instead of shrinking n.
    jq -nc --arg p "${profile}" '{labels: {runner_profile: $p, backend: "unknown"}, events: []}' >>"${EVENTS}"
    continue
  fi
  jq -c --arg p "${profile}" '
    (.events // []) as $ev
    | {labels: {runner_profile: $p,
                backend: ([$ev[] | select(.type == "lease_acquired") | .detail.backend? // empty]
                          | first // "unknown")},
       events: $ev}' "${WORK}/ev.json" >>"${EVENTS}"
done <"${WORK}/candidates.tsv"

info "${VISITED} task(s) visited"
if [[ "${VISITED}" -eq 0 ]]; then
  bench_missing dispatch.cold_start s \
    "no dispatched terminal task${PROFILE:+ on ${PROFILE}} in the newest ${LIMIT}" ""
fi
bench_append_segments "${EVENTS}"
bench_finish
