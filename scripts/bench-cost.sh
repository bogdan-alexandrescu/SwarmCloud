#!/usr/bin/env bash
# Cost and token spend per task, from the fields the API now serves.
#
# WHY A LATENCY SUITE IS NOT ENOUGH
# ---------------------------------
# A change that doubles token usage -- a longer system prompt, a retry loop
# that re-sends context, a model swap, a checkpoint restore that replays
# history -- is invisible to every other benchmark here. Wall time can even
# IMPROVE while the bill doubles. This is the only measurement that sees it.
#
# WHY THIS IS ALSO A REGRESSION TEST FOR THE SPEND FIELDS
# -------------------------------------------------------
# `attempt_from_dict` dropped all five spend fields for a period, so every cost
# and token figure in the product was unreachable. The unit test that was
# supposed to cover it asserted they are None when ABSENT -- an assertion the
# bug satisfies -- and nothing asserted they are present when present.
#
# This collector cannot be satisfied that way. An attempt that ran an agent and
# carries no `cost_usd` is recorded as NOT MEASURED with that reason, and a
# metric whose samples are all not-measured fails the gate. If the fields stop
# arriving, this goes red on the next run against a real environment -- no
# assertion to get backwards, because the absence of a number is the failure.
#
# ATTEMPTS, NOT result_summary. `result_summary` is written once, at terminal
# state, so a task that failed twice and succeeded on the third attempt carries
# only the third attempt's numbers. Cost is the SUM over attempts: the two
# failed ones were paid for too, and a per-task cost that omits them
# understates every retried task on the platform.
#
# Cost to run: read-only, about 200 GETs against swarm-api, fifteen seconds,
# well under a cent. It creates no tasks -- it measures the ones that already
# ran, which is also why it needs an environment with history.
#
# Usage: scripts/bench-cost.sh [--limit 200] [--profile claude-code]

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
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

step "Spend per task${PROFILE:+ on ${PROFILE}} (newest ${LIMIT})"
bench_init cost

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-cost.XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT INT TERM

if ! FIELDS="$(bench_request GET "/v1/tasks?limit=${LIMIT}" "${WORK}/tasks.json")"; then
  die "curl could not reach $(api_url)/v1/tasks. That is a transport failure, not a platform with no spend."
fi
[[ "${FIELDS%% *}" == 2* ]] || die "GET /v1/tasks answered HTTP ${FIELDS%% *}. An error is not an empty task list."

# Only tasks that actually RAN an agent can have spend. A QUEUED task with no
# cost is correct, and folding it in as a zero would halve the reported mean
# per task on any platform with a backlog -- which is the shape of a cost
# metric that quietly stops being alarming.
jq -r '[.tasks // [] | .[]
        | select(.state == "SUCCEEDED" or .state == "FAILED")
        | select(.attempt_count != null and .attempt_count > 0)]
       | .[] | [.id, (.runner_profile // "unknown")] | @tsv' \
  "${WORK}/tasks.json" >"${WORK}/candidates.tsv"

TOTAL="$(grep -c . "${WORK}/candidates.tsv" || true)"
info "${TOTAL} terminal task(s) with at least one attempt"

if [[ "${TOTAL}" -eq 0 ]]; then
  # Not a pass. An environment with no completed work cannot tell you anything
  # about cost per task, and saying "0 samples, within threshold" would be the
  # lie this file is built to refuse.
  for metric in cost.usd_per_task cost.input_tokens cost.output_tokens \
                cost.cache_read_input_tokens cost.cache_creation_input_tokens; do
    bench_missing "${metric}" "" "no terminal task with an attempt was found in the newest ${LIMIT}" ""
  done
  bench_finish
  exit $?
fi

MEASURED=0
while IFS=$'\t' read -r task_id profile; do
  [[ -n "${task_id}" ]] || continue
  if [[ -n "${PROFILE}" && "${profile}" != "${PROFILE}" ]]; then continue; fi
  labels="$(bench_label runner_profile "${profile}")"

  if ! AF="$(bench_request GET "/v1/tasks/${task_id}/attempts" "${WORK}/att.json")" \
     || [[ "${AF%% *}" != 2* ]]; then
    for metric in cost.usd_per_task cost.input_tokens cost.output_tokens \
                  cost.cache_read_input_tokens cost.cache_creation_input_tokens; do
      bench_missing "${metric}" "" "the attempts for ${task_id} could not be read" "${labels}"
    done
    continue
  fi

  # SUMMED across attempts, and null-preserving: `add` over an array that is
  # empty after the nulls are filtered returns null, not 0. That is exactly the
  # distinction wanted -- "no attempt reported a cost" must not arrive here as
  # "this task cost nothing".
  for pair in cost_usd:usd_per_task:usd \
              input_tokens:input_tokens:tokens \
              output_tokens:output_tokens:tokens \
              cache_read_input_tokens:cache_read_input_tokens:tokens \
              cache_creation_input_tokens:cache_creation_input_tokens:tokens; do
    field="${pair%%:*}"; rest="${pair#*:}"; metric="${rest%%:*}"; unit="${rest##*:}"
    value="$(jq -r --arg f "${field}" \
      '[.attempts // [] | .[] | .[$f] | select(. != null)] | if length == 0 then "" else add end' \
      "${WORK}/att.json")"
    if [[ -z "${value}" || "${value}" == "null" ]]; then
      bench_missing "cost.${metric}" "${unit}" \
        "task ${task_id} ran an agent and no attempt reported ${field}" "${labels}"
    else
      bench_sample "cost.${metric}" "${unit}" "${value}" "${labels}"
      # `if`, not `[[ ... ]] && ...`: the && form returns 1 when the test is
      # false, and under `set -e` that ends the script silently on the first
      # non-cost field. Same trap as `$(( x++ ))` returning 1 at zero.
      if [[ "${field}" == "cost_usd" ]]; then MEASURED=$(( MEASURED + 1 )); fi
    fi
  done
done <"${WORK}/candidates.tsv"

info "${MEASURED} task(s) reported a cost"
bench_finish
