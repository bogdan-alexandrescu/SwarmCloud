#!/usr/bin/env bash
# egress-ready-report: how long new workers waited for their path to the
# forge, per backend, over recent steps (#939). Read-only.
#
#   scripts/egress-ready-report.sh [--since 7d] [--min-n 30]
#                                  [--backend cloud-run|gke|unknown]
#                                  [--max-tasks 2000] [--json]
#
# WHAT IT READS. Every attempt that clones writes an `egress_ready` startup
# mark (agent_worker.lifecycle._mark_egress_ready: detail.egress is
# EgressProbe.result(), whose `egress_ready_seconds` is None when nothing
# connected within the cap) and a `clone_timed` mark (_mark_clone_timed). This
# walks GET /v1/tasks newest first until a task is older than --since, reads
# each task's events, and, for a task that has a mark, its attempts -- the
# task document carries no backend, the attempt does (Attempt.backend in
# swarm_common.models). Only through the API: never Firestore directly.
#
# WHAT IT PRINTS. One row per backend: n, n_none, p50, p90 and max of
# egress_ready_seconds, and the clone's p50 beside it; the number of tasks
# and marks actually visited; and a verdict against #939's bar, Cloud Run p50
# under 5 s with n >= --min-n. Every number is made by
# scripts/egress_ready_report.py, which reads the API's responses on stdin and
# is unit-tested offline (tests/unit/scripts/test_egress_ready_report.py).
# This script only fetches.
#
# EXIT. 0 PASS (or NOT_JUDGED when --backend leaves Cloud Run out), 1 FAIL --
# `insufficient n` included, 2 when no egress_ready mark was read or a request
# failed. Reading nothing is never reported as a pass (CLAUDE.md, rule zero).
#
# The credential is lib/common.sh's api_credential(), as scripts/api.sh uses
# it: it reaches curl on stdin, never argv. SWARM_API_TENANT picks the tenant.
# Everything printed goes through `redact`.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SINCE="7d"
MAX_TASKS=2000
REPORT_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)     SINCE="${2:?--since needs a duration, e.g. 7d}"; shift 2 ;;
    --min-n)     REPORT_ARGS+=(--min-n "${2:?--min-n needs a number}"); shift 2 ;;
    --backend)   REPORT_ARGS+=(--backend "${2:?--backend needs a name}"); shift 2 ;;
    --max-tasks) MAX_TASKS="${2:?--max-tasks needs a number}"; shift 2 ;;
    --json)      REPORT_ARGS+=(--json); shift ;;
    -h|--help)   sed -n '2,32p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "${MAX_TASKS}" =~ ^[0-9]+$ ]] || die "--max-tasks must be a number"

require_cmd gcloud curl

# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a PY <<<"$(swarm_python)"
HELPER="${REPO_ROOT}/scripts/egress_ready_report.py"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/egress-ready.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM
PAGE="${WORK}/page.json"
IDS="${WORK}/ids.txt"
STREAM="${WORK}/stream.json"
: >"${IDS}"
: >"${STREAM}"

# One read. Redirected to a file rather than captured with $(...): a command
# substitution is a SUBSHELL, so API_STATUS would never reach this shell and
# every response would read as a success.
fetch() {
  local path="$1"
  if ! api_request GET "${API_PREFIX}${path}" >"${PAGE}"; then
    err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}"
    redact <"${PAGE}" >&2 || true
    exit 2
  fi
}

# --- the tasks inside the window, newest first -----------------------------
step "Tasks created in the last ${SINCE}"
cursor=""
listed=0
while :; do
  query="/tasks?view=summary&limit=200"
  [[ -z "${cursor}" ]] || query="${query}&page_token=${cursor}"
  fetch "${query}"
  cursor=""
  done_listing=0
  while read -r kind value; do
    case "${kind}" in
      task)  printf '%s\n' "${value}" >>"${IDS}"; listed=$((listed + 1)) ;;
      next)  cursor="${value}" ;;
      older) done_listing=1 ;;
    esac
  done < <("${PY[@]}" "${HELPER}" window --since "${SINCE}" <"${PAGE}")
  if [[ "${done_listing}" -eq 1 || -z "${cursor}" ]]; then
    break
  fi
  if [[ "${listed}" -ge "${MAX_TASKS}" ]]; then
    warn "stopped listing at --max-tasks ${MAX_TASKS}; older tasks in the window were not read"
    break
  fi
done
info "${listed} task(s) listed"

# --- each task's events, and its attempts when it carries a mark -----------
step "Reading startup marks"
visited=0
with_marks=0
while read -r task_id; do
  [[ -n "${task_id}" ]] || continue
  [[ "${visited}" -lt "${MAX_TASKS}" ]] || break
  visited=$((visited + 1))
  marked=0
  events_cursor=""
  while :; do
    query="/tasks/${task_id}/events?limit=200"
    [[ -z "${events_cursor}" ]] || query="${query}&page_token=${events_cursor}"
    fetch "${query}"
    if grep -qE 'egress_ready|clone_timed' "${PAGE}"; then
      marked=1
      cat "${PAGE}" >>"${STREAM}"
      printf '\n' >>"${STREAM}"
    fi
    events_cursor="$("${PY[@]}" "${HELPER}" next-token <"${PAGE}")"
    [[ -n "${events_cursor}" ]] || break
  done
  if [[ "${marked}" -eq 1 ]]; then
    with_marks=$((with_marks + 1))
    fetch "/tasks/${task_id}/attempts"
    cat "${PAGE}" >>"${STREAM}"
    printf '\n' >>"${STREAM}"
  fi
done <"${IDS}"
info "${visited} task(s) visited, ${with_marks} with a startup mark"

# --- the numbers -------------------------------------------------------------
hr
status=0
"${PY[@]}" "${HELPER}" report --since "${SINCE}" ${REPORT_ARGS[@]+"${REPORT_ARGS[@]}"} \
  <"${STREAM}" 2>&1 | redact || status=$?
exit "${status}"
