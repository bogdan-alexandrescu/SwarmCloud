#!/usr/bin/env bash
# browser-bench: the same real browser prompts with Playwright MCP and with
# dev-browser, and what each cost (DEVB-BENCH, owner decision 2026-10-14;
# part of #1030).
#
#   scripts/browser-bench.sh [--batch NAME] [--repeats 3] [--prompts a,b]
#                            [--tools playwright-mcp,dev-browser]
#                            [--usd-per-task 0.66] [--dry-run]
#   scripts/browser-bench.sh --collect NAME
#
# dev-browser is built opt-in beside the Playwright MCP, which stays the
# default; this benchmark is what decides any flip. docs/browser-bench.md has
# the ten prompts, each one's machine-checkable success criterion, the pass
# bar, what a batch costs and why egress must not change between the arms.
#
# SUBMIT (the default). One `claude-code-browser` task per run -- 10 prompts x
# 2 tools x 3 repeats = 60 by default, arms interleaved -- through the same
# api_request the other scripts use, each labelled
# bench-<tool>-<prompt>-<rep> in its free-form `metadata.bench`. The tool is
# chosen ONLY by the sentence in front of the prompt; no API input selects it
# (invariant 10). The task ids go to
# ${SWARM_BROWSER_BENCH_DIR:-~/.swarm/browser-bench}/<batch>/runs.tsv, and a
# label already in that file is not submitted twice, so a submit that failed
# half-way is re-run with the same --batch. Spending money asks for a typed
# confirmation (SWARM_ASSUME_YES skips it: submitting is not destructive).
#
# --dry-run prints every run it would submit and the estimated cost, and calls
# nothing.
#
# --collect NAME reads each task of the batch (the task, its attempts, events,
# live action log, answer and transcript, GETs only) into <batch>/tasks/, and
# scripts/browser_bench.py writes results.tsv, results-<tool>.tsv and
# summary.md there and prints the summary. Every number is made by the helper,
# which is unit-tested offline (tests/unit/scripts/test_browser_bench.py).
#
# EXIT. 0 submitted or collected; 1 a request failed or was refused; 2 nothing
# to read or a usage error. Reading nothing is never a result (CLAUDE.md, rule
# zero). Everything printed from the API goes through `redact`.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

BATCH=""
COLLECT=""
DRY_RUN=0
PLAN_ARGS=()
USD_PER_TASK=""
#: Transcript windows read per task, at most: a browser run's stdout is a few
#: MiB; the cap is what keeps a runaway transcript from being read forever.
MAX_WINDOWS=50
#: Event pages read per task, at most (a page is the API's default size).
MAX_EVENT_PAGES=20
while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch)        BATCH="${2:?--batch needs a name}"; shift 2 ;;
    --repeats)      PLAN_ARGS+=(--repeats "${2:?--repeats needs a number}"); shift 2 ;;
    --prompts)      PLAN_ARGS+=(--prompts "${2:?--prompts needs a list}"); shift 2 ;;
    --tools)        PLAN_ARGS+=(--tools "${2:?--tools needs a list}"); shift 2 ;;
    --usd-per-task) USD_PER_TASK="${2:?--usd-per-task needs a number}"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --collect)      COLLECT="${2:?--collect needs a batch name}"; shift 2 ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) err "unknown argument: $1 (see --help)"; exit 2 ;;
  esac
done
[[ -z "${USD_PER_TASK}" ]] || PLAN_ARGS+=(--usd-per-task "${USD_PER_TASK}")

NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
BENCH_ROOT="${SWARM_BROWSER_BENCH_DIR:-${HOME}/.swarm/browser-bench}"

# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a PY <<<"$(swarm_python)"
HELPER="${REPO_ROOT}/scripts/browser_bench.py"

# --- collect -----------------------------------------------------------------
if [[ -n "${COLLECT}" ]]; then
  [[ "${COLLECT}" =~ ${NAME_RE} ]] || { err "--collect: not a batch name: ${COLLECT}"; exit 2; }
  DIR="${BENCH_ROOT}/${COLLECT}"
  RUNS="${DIR}/runs.tsv"
  [[ -s "${RUNS}" ]] || { err "no runs recorded for batch ${COLLECT} (${RUNS})"; exit 2; }
  require_cmd gcloud curl jq
  TASKS_DIR="${DIR}/tasks"
  mkdir -p "${TASKS_DIR}"
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/browser-bench.XXXXXX")"
  trap 'rm -rf "${WORK}"' EXIT INT TERM

  # One API read into a file. Redirected rather than captured with $(...): a
  # command substitution is a SUBSHELL, so API_STATUS would never reach this
  # shell and every response would read as a success.
  fetch() {
    local path="$1" out="$2"
    if ! api_request GET "${API_PREFIX}${path}" >"${out}"; then
      err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}"
      redact <"${out}" >&2 || true
      exit 1
    fi
  }
  # A document a task may not have (no live session, no answer yet): a 404 is
  # "none", which the helper reports as not measured, never as zero.
  fetch_optional() {
    local path="$1" out="$2"
    if ! api_request GET "${API_PREFIX}${path}" >"${out}"; then
      if [[ "${API_STATUS}" == "404" ]]; then
        rm -f "${out}"
        return 0
      fi
      err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}"
      redact <"${out}" >&2 || true
      exit 1
    fi
  }

  step "Reading batch ${COLLECT}"
  visited=0
  unfinished=0
  while IFS=$'\t' read -r run_label _tool _prompt _rep task_id; do
    [[ "${task_id:-}" =~ ^task_[a-z0-9]+$ ]] || continue
    visited=$((visited + 1))
    base="${TASKS_DIR}/${task_id}"
    fetch "/tasks/${task_id}" "${base}.task.json"
    state="$(jq -r '.state // ""' "${base}.task.json")"
    case "${state}" in
      SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED) ;;
      *) unfinished=$((unfinished + 1)); dim "  ${run_label}: ${state:-unknown}, not finished" ;;
    esac
    fetch "/tasks/${task_id}/attempts" "${base}.attempts.json"

    # Every page of events, merged into one {events: [...]}.
    pages=()
    token=""
    page=0
    while [[ "${page}" -lt "${MAX_EVENT_PAGES}" ]]; do
      query="/tasks/${task_id}/events"
      [[ -z "${token}" ]] || query="${query}?page_token=${token}"
      fetch "${query}" "${WORK}/events.${page}.json"
      pages+=("${WORK}/events.${page}.json")
      token="$(jq -r '.next_page_token // "" | @uri' "${WORK}/events.${page}.json")"
      page=$((page + 1))
      [[ -n "${token}" ]] || break
    done
    [[ -z "${token}" ]] || warn "${run_label}: stopped at ${MAX_EVENT_PAGES} event pages"
    jq -s '{events: (map(.events // []) | add)}' "${pages[@]}" >"${base}.events.json"

    fetch_optional "/tasks/${task_id}/live/actions" "${base}.actions.json"
    fetch_optional "/tasks/${task_id}/answer" "${base}.answer.json"

    # The transcript, window by window, until the stream says there is no more.
    rm -f "${base}".transcript.*.json
    offset=0
    window=0
    while [[ "${window}" -lt "${MAX_WINDOWS}" ]]; do
      fetch_optional "/tasks/${task_id}/transcript?offset=${offset}" "${base}.transcript.${window}.json"
      [[ -f "${base}.transcript.${window}.json" ]] || break
      next="$(jq -r '.stream.next_offset // ""' "${base}.transcript.${window}.json")"
      window=$((window + 1))
      [[ -n "${next}" && "${next}" != "${offset}" ]] || break
      offset="${next}"
    done
    [[ "${window}" -lt "${MAX_WINDOWS}" ]] || warn "${run_label}: stopped at ${MAX_WINDOWS} transcript windows"
  done <"${RUNS}"
  info "${visited} run(s) visited"
  [[ "${unfinished}" -eq 0 ]] || warn "${unfinished} run(s) not finished yet: they count neither way; collect again later"
  [[ "${visited}" -gt 0 ]] || { err "runs.tsv named no task"; exit 2; }

  hr
  status=0
  "${PY[@]}" "${HELPER}" collect --dir "${DIR}" 2>&1 | redact || status=$?
  info "wrote ${DIR}/results.tsv, results-<tool>.tsv and summary.md"
  exit "${status}"
fi

# --- plan --------------------------------------------------------------------
[[ -n "${BATCH}" ]] || BATCH="$(date -u +%Y%m%dT%H%M%SZ)"
[[ "${BATCH}" =~ ${NAME_RE} ]] || { err "--batch: not a batch name: ${BATCH}"; exit 2; }

if [[ "${DRY_RUN}" -eq 1 ]]; then
  step "Dry run: batch ${BATCH}, nothing is submitted"
  "${PY[@]}" "${HELPER}" plan --batch "${BATCH}" ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"}
  exit $?
fi

require_cmd gcloud curl jq
DIR="${BENCH_ROOT}/${BATCH}"
RUNS="${DIR}/runs.tsv"
mkdir -p "${DIR}/bodies"
touch "${RUNS}"
PLAN="${DIR}/plan.tsv"
"${PY[@]}" "${HELPER}" plan --batch "${BATCH}" --bodies "${DIR}/bodies" \
  ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"} >"${PLAN}" 2>"${DIR}/plan.err" || {
  cat "${DIR}/plan.err" >&2; exit 2; }
planned="$(grep -c . "${PLAN}" || true)"
already="$(grep -c . "${RUNS}" || true)"
cat "${DIR}/plan.err" >&2

step "Submitting batch ${BATCH} to $(api_url) as claude-code-browser"
info "${planned} run(s) planned, ${already} already submitted in this batch"
confirm "This submits up to ${planned} real claude-code-browser tasks, which spend money (estimate above)." \
  "submit"

RESPONSE="$(mktemp "${TMPDIR:-/tmp}/browser-bench.XXXXXX")"
trap 'rm -f "${RESPONSE}"' EXIT INT TERM
visited=0
submitted=0
while IFS=$'\t' read -r run_label tool prompt rep body; do
  [[ -n "${body:-}" ]] || continue
  visited=$((visited + 1))
  if awk -F '\t' -v l="${run_label}" '$1 == l { found = 1 } END { exit !found }' "${RUNS}"; then
    dim "  ${run_label}: already submitted"
    continue
  fi
  if ! api_request POST "${API_PREFIX}/tasks" "$(cat "${body}")" >"${RESPONSE}"; then
    err "${run_label}: POST ${API_PREFIX}/tasks answered HTTP ${API_STATUS}"
    redact <"${RESPONSE}" >&2 || true
    info "${visited} run(s) visited, ${submitted} submitted; re-run with --batch ${BATCH} to continue"
    exit 1
  fi
  task_id="$(jq -r '.task.id // ""' "${RESPONSE}")"
  [[ "${task_id}" =~ ^task_[a-z0-9]+$ ]] || die "${run_label}: the API answered without a task id"
  printf '%s\t%s\t%s\t%s\t%s\n' "${run_label}" "${tool}" "${prompt}" "${rep}" "${task_id}" >>"${RUNS}"
  submitted=$((submitted + 1))
  ok "${run_label} -> ${task_id}"
done <"${PLAN}"
info "${visited} run(s) visited, ${submitted} submitted"
[[ "${visited}" -gt 0 ]] || { err "the plan named no run"; exit 2; }
info "when they finish: scripts/browser-bench.sh --collect ${BATCH}"
