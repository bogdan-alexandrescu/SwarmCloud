#!/usr/bin/env bash
# scorecard: what agent work costs and how fast it lands, per week. READ-ONLY.
#
#   scripts/scorecard.sh [--since ISO] [--until ISO] [--weeks N] [--json]
#                        [--from-file records.jsonl] [--no-github] [--no-verdicts]
#                        [--max-verdicts 200] [--max-gh-calls 40] [--max-tasks 20000]
#                        [--repo owner/name] [--acceptance-tenant ID]...
#
# Owner-approved improvement plan, 2026-11-23: every later change (cheaper
# model classes for reviews, a stricter pre-publish gate, a shorter merge-train
# wait) is judged on these numbers. docs/scorecard.md explains each measure.
#
# THE WINDOW. The last 7 days by default; --weeks N for the last N weeks as a
# trend, or --since/--until. It is cut into 7-day weeks ending at --until.
#
# WHAT IT READS, all read-only; nothing is written to the cluster, Firestore,
# the API or GitHub:
#
#   1. TASK RECORDS: one structured query on `tasks` (created_at in the window,
#      a field mask, paged by a created_at + name cursor), through
#      lib/common.sh's fs_request, whose token goes to curl on stdin and never
#      on a command line. --from-file reads a saved extract (JSON lines of the
#      same records) instead.
#   2. REVIEW VERDICTS: verdict.json of the newest --max-verdicts review and
#      re-review tasks, through the API's tenant-checked, redacted
#      /artifacts/content read. The report says how many were sampled of how
#      many. --no-verdicts skips it.
#   3. GITHUB, through gh: issue comments (merge-train ejections), merged pull
#      requests, release.yml and accept.yml runs, pull-request CI runs. Every
#      call spends the operator's hourly quota, so at most --max-gh-calls are
#      made and the report prints how many. --no-github skips it.
#
# Every number is made by scripts/lib/scorecard.py, which reads the files this
# script fetches and is unit-tested offline (tests/unit/scripts/test_scorecard.py).
# A source that could not be read prints `not read: <why>`, never 0. No
# workspace is named: tenants are counted only as main workspace / acceptance /
# personal. Everything printed goes through `redact`.
#
# EXIT. 0 the report was printed; 2 bad arguments or the task records could
# not be read (a report over records nobody read would be all "not read").

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SINCE=""
UNTIL=""
WEEKS=""
FROM_FILE=""
GITHUB=1
VERDICTS=1
#: verdict.json reads, at most: one API call each. 200 covers a busy week's
#: reviews; past it the newest are kept and the report says it sampled.
MAX_VERDICTS=200
#: gh calls, at most: each page is one call against the operator's 5,000 an
#: hour. 40 reads a few thousand comments and runs, enough for four weeks.
MAX_GH_CALLS=40
#: Task records, at most (300 per Firestore page): a guard against a window
#: typed wrong, not a sample size.
MAX_TASKS=20000
REPO="${GH_REPO:-}"
ACCEPTANCE=()
REPORT_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)        SINCE="${2:?--since needs a timestamp}"; shift 2 ;;
    --until)        UNTIL="${2:?--until needs a timestamp}"; shift 2 ;;
    --weeks)        WEEKS="${2:?--weeks needs a number}"; shift 2 ;;
    --from-file)    FROM_FILE="${2:?--from-file needs a path}"; shift 2 ;;
    --no-github)    GITHUB=0; shift ;;
    --no-verdicts)  VERDICTS=0; shift ;;
    --max-verdicts) MAX_VERDICTS="${2:?--max-verdicts needs a number}"; shift 2 ;;
    --max-gh-calls) MAX_GH_CALLS="${2:?--max-gh-calls needs a number}"; shift 2 ;;
    --max-tasks)    MAX_TASKS="${2:?--max-tasks needs a number}"; shift 2 ;;
    --repo)         REPO="${2:?--repo needs owner/name}"; shift 2 ;;
    --acceptance-tenant) ACCEPTANCE+=("${2:?--acceptance-tenant needs an id}"); shift 2 ;;
    --json)         REPORT_ARGS+=(--json); shift ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
for pair in "--weeks:${WEEKS:-1}" "--max-verdicts:${MAX_VERDICTS}" "--max-gh-calls:${MAX_GH_CALLS}" "--max-tasks:${MAX_TASKS}"; do
  [[ "${pair#*:}" =~ ^[0-9]+$ ]] || die "${pair%%:*} must be a whole number"
done
[[ -z "${WEEKS}" || "${WEEKS}" -gt 0 ]] || die "--weeks must be above 0"
[[ -z "${FROM_FILE}" || -f "${FROM_FILE}" ]] || die "--from-file ${FROM_FILE}: no such file"
# The acceptance suite's tenant (scripts/acceptance/config.sh ACC_TENANT).
[[ ${#ACCEPTANCE[@]} -gt 0 ]] || ACCEPTANCE=("${SWARM_ACCEPTANCE_TENANT:-smoke}")

require_cmd jq
# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a PY <<<"$(swarm_python)"
HELPER="${REPO_ROOT}/scripts/lib/scorecard.py"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/scorecard.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM
RECORDS="${WORK}/records.jsonl"
CURSOR="${WORK}/cursor.json"
VERDICT_DIR="${WORK}/verdicts"
GH_DIR="${WORK}/github"
mkdir -p "${VERDICT_DIR}" "${GH_DIR}"

WINDOW_ARGS=()
[[ -z "${SINCE}" ]] || WINDOW_ARGS+=(--since "${SINCE}")
[[ -z "${UNTIL}" ]] || WINDOW_ARGS+=(--until "${UNTIL}")
[[ -z "${WEEKS}" ]] || WINDOW_ARGS+=(--weeks "${WEEKS}")
# The window is fixed once, so every source reads the same one.
read -r W_SINCE W_UNTIL < <("${PY[@]}" "${HELPER}" window ${WINDOW_ARGS[@]+"${WINDOW_ARGS[@]}"})
[[ -n "${W_UNTIL:-}" ]] || die "could not resolve the window"
FIXED=(--since "${W_SINCE}" --until "${W_UNTIL}")
info "window ${W_SINCE} .. ${W_UNTIL}"

# --- 1. task records ----------------------------------------------------------
if [[ -n "${FROM_FILE}" ]]; then
  cp "${FROM_FILE}" "${RECORDS}"
  SOURCE="the extract given with --from-file"
else
  step "Task records from Firestore (${FIRESTORE_DATABASE})"
  require_cmd curl
  : >"${RECORDS}"
  read_rows=0
  pages=0
  while :; do
    body="$("${PY[@]}" "${HELPER}" fs-query "${FIXED[@]}" --cursor "${CURSOR}")"
    # Redirected, not captured: fs_request's failure message goes to stderr,
    # and its exit status must reach this shell.
    if ! fs_request POST "$(fs_base):runQuery" "${body}" >"${WORK}/page.json"; then
      die "the task query failed; nothing was reported (a report over unread records would read as empty)"
    fi
    pages=$((pages + 1))
    rows="$("${PY[@]}" "${HELPER}" fs-decode --page "${WORK}/page.json" --records "${RECORDS}" --cursor "${CURSOR}")"
    read_rows=$((read_rows + ${rows#rows }))
    [[ -f "${CURSOR}" ]] || break
    if [[ "${read_rows}" -ge "${MAX_TASKS}" ]]; then
      warn "stopped at --max-tasks ${MAX_TASKS}: later tasks in the window were not read"
      break
    fi
  done
  info "${read_rows} task record(s) in ${pages} page(s)"
  SOURCE="Firestore (${pages} page(s))"
fi

# --- 2. review verdicts -------------------------------------------------------
VERDICT_ARGS=()
if [[ "${VERDICTS}" -eq 1 ]]; then
  step "Review verdicts (at most ${MAX_VERDICTS})"
  require_cmd curl
  n_candidates=0
  sampled=0
  while read -r kind task_id tenant; do
    case "${kind}" in
      candidates) n_candidates="${task_id}"; continue ;;
      task) ;;
      *) continue ;;
    esac
    # One read per task, as that task's tenant. A 404 (the task lists no
    # verdict.json) leaves an empty file, counted as missing; any other
    # failure leaves its status, counted as unreadable -- neither is a verdict.
    if ! SWARM_API_TENANT="${tenant}" api_request GET \
        "${API_PREFIX}/tasks/${task_id}/artifacts/content?name=verdict.json&limit_bytes=65536" \
        >"${VERDICT_DIR}/${task_id}.json"; then
      if [[ "${API_STATUS}" == "404" ]]; then
        : >"${VERDICT_DIR}/${task_id}.json"
      else
        jq -nc --arg s "${API_STATUS}" '{read_failed: $s}' >"${VERDICT_DIR}/${task_id}.json"
      fi
    fi
    sampled=$((sampled + 1))
  done < <("${PY[@]}" "${HELPER}" reviews --records "${RECORDS}" --max "${MAX_VERDICTS}")
  jq -nc --argjson c "${n_candidates}" --argjson s "${sampled}" --argjson cap "${MAX_VERDICTS}" \
    '{candidates: $c, sampled: $s, cap: $cap}' >"${VERDICT_DIR}/meta.json"
  info "${sampled} verdict(s) read of ${n_candidates} review task(s)"
  VERDICT_ARGS=(--verdicts-dir "${VERDICT_DIR}")
fi

# --- 3. GitHub ----------------------------------------------------------------
GH_ARGS=()
if [[ "${GITHUB}" -eq 1 ]]; then
  step "GitHub (at most ${MAX_GH_CALLS} call(s))"
  require_cmd gh
  calls=0
  if [[ -z "${REPO}" ]]; then
    calls=1
    REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)" \
      || die "no --repo and gh resolves no repository here"
  fi
  since_day="${W_SINCE%%T*}"
  until_day="${W_UNTIL%%T*}"

  # gh_read SOURCE KEY ENDPOINT ARGS...: pages of 100 into SOURCE-<n>.json until
  # a page is short or the call cap is spent; one status line per source.
  gh_read() {
    local source="$1" key="$2" endpoint="$3" page=1 n
    shift 3
    while :; do
      if [[ "${calls}" -ge "${MAX_GH_CALLS}" ]]; then
        if [[ "${page}" -eq 1 ]]; then
          printf '%s\t%s\n' "${source}" "the --max-gh-calls cap (${MAX_GH_CALLS}) was spent" >>"${GH_DIR}/status.tsv"
        else
          warn "${source}: stopped at the --max-gh-calls cap; later pages were not read"
          printf '%s\tok\n' "${source}" >>"${GH_DIR}/status.tsv"
        fi
        return 0
      fi
      calls=$((calls + 1))
      if ! gh_api_read_retry "${GH_DIR}/${source}-${page}.json" "${GH_DIR}/${source}.err" \
          -X GET "${endpoint}" "$@" -f per_page=100 -f page="${page}"; then
        rm -f "${GH_DIR}/${source}"-*.json
        printf '%s\t%s\n' "${source}" "gh api ${endpoint} failed: $(redact <"${GH_DIR}/${source}.err" | head -n 1 | tr '\t' ' ')" \
          >>"${GH_DIR}/status.tsv"
        return 0
      fi
      if [[ -n "${key}" ]]; then
        n="$(jq -r --arg k "${key}" '.[$k] // [] | length' "${GH_DIR}/${source}-${page}.json")"
      else
        n="$(jq -r 'length' "${GH_DIR}/${source}-${page}.json")"
      fi
      if [[ "${n}" -lt 100 ]]; then
        printf '%s\tok\n' "${source}" >>"${GH_DIR}/status.tsv"
        return 0
      fi
      page=$((page + 1))
    done
  }
  # `since` filters on updated_at; the report keeps comments CREATED in the window.
  gh_read comments "" "repos/${REPO}/issues/comments" -f since="${W_SINCE}" -f sort=created -f direction=asc
  gh_read merged items "search/issues" -f q="repo:${REPO} is:pr is:merged merged:${since_day}..${until_day}"
  gh_read release workflow_runs "repos/${REPO}/actions/workflows/release.yml/runs" -f created="${since_day}..${until_day}"
  gh_read accept workflow_runs "repos/${REPO}/actions/workflows/accept.yml/runs" -f created="${since_day}..${until_day}"
  gh_read prci workflow_runs "repos/${REPO}/actions/runs" -f event=pull_request -f created="${since_day}..${until_day}"
  printf '%s\n' "${calls}" >"${GH_DIR}/calls"
  info "${calls} gh call(s) made (a retried call counts once)"
  GH_ARGS=(--github-dir "${GH_DIR}")
fi

# --- the numbers --------------------------------------------------------------
hr
ACC_ARGS=()
for tenant in "${ACCEPTANCE[@]}"; do ACC_ARGS+=(--acceptance-tenant "${tenant}"); done
"${PY[@]}" "${HELPER}" report "${FIXED[@]}" --records "${RECORDS}" --records-source "${SOURCE}" \
  ${VERDICT_ARGS[@]+"${VERDICT_ARGS[@]}"} ${GH_ARGS[@]+"${GH_ARGS[@]}"} "${ACC_ARGS[@]}" \
  ${REPORT_ARGS[@]+"${REPORT_ARGS[@]}"} | redact
