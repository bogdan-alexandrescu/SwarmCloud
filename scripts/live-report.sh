#!/usr/bin/env bash
# live-report: the live browser, measured, per browser task over the last N
# hours (LIVE-MEASURE, owner decision 2026-10-13; part of #1030). Read-only.
#
#   scripts/live-report.sh [--hours 24] [--max-tasks 200] [--json]
#
# Phase 1 of the live-browser plan is "measure": whether to keep warm pods is
# decided from these numbers. Per task it prints
#
#   * start-up: dispatched -> first frame, split cold/warm, with node
#     allocation (dispatched -> pod_scheduled) and the image pull, from
#     GET /v1/tasks/{id}/startup;
#   * the viewer's stream: fps while the page changed, the longest a change
#     waited, blank (#555/#222) time, input-to-frame p95 and keys delivered,
#     from the display sidecar's `live_viewer_metrics` lines in Cloud Logging;
#   * the hand-off funnel and "I'm ready" -> first frame, from swarm-api's
#     `live_handoff_funnel` and `live_ready_first_frame` lines;
#   * Google /sorry hits, from the relay's action log
#     (GET /v1/tasks/{id}/live/actions);
#
# and then the Phase-2 targets, each PASS, FAIL or NO DATA: >= 10 fps while
# the page changes, input_to_frame p95 < 200 ms, 100% of keys, "I'm ready" ->
# visible p90 < 20 s warm and < 60 s cold, hand-off expiry < 30% when a person
# is present. Every number is made by scripts/live_report.py, which reads the
# files this script fetches and is unit-tested offline
# (tests/unit/scripts/test_live_report.py). This script only fetches.
#
# OUR CLUSTER ONLY. The gate lines are read with a filter naming
# resource.labels.cluster_name="${GKE_CLUSTER}" and the display container; the
# script refuses to run when GKE_CLUSTER is on the shared deny-list, so it
# never reads agents-staging (CLAUDE.md, rule 2). Nothing is written anywhere:
# `gcloud logging read` and API GETs only.
#
# EXIT. 0 every target PASS, 1 any FAIL, 3 no FAIL but a target had no data,
# 2 nothing read or a request failed. Reading nothing is never a pass
# (CLAUDE.md, rule zero).
#
# The API credential is lib/common.sh's api_credential(), as scripts/api.sh
# uses it. Everything printed goes through `redact`.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

HOURS=24
MAX_TASKS=200
#: Cloud Logging entries read per query, at most: a busy day is a few hundred
#: connections and hand-offs; the cap is what keeps a bad filter cheap.
LOG_LIMIT=5000
REPORT_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hours)     HOURS="${2:?--hours needs a number}"; shift 2 ;;
    --max-tasks) MAX_TASKS="${2:?--max-tasks needs a number}"; shift 2 ;;
    --json)      REPORT_ARGS+=(--json); shift ;;
    -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "${HOURS}" =~ ^[0-9]+$ && "${HOURS}" -gt 0 ]] || die "--hours must be a whole number above 0"
[[ "${MAX_TASKS}" =~ ^[0-9]+$ ]] || die "--max-tasks must be a number"

if is_shared_resource "${GKE_CLUSTER}"; then
  die "GKE_CLUSTER=${GKE_CLUSTER} is another team's cluster (the shared deny-list); this report reads ours only"
fi

require_cmd gcloud curl jq

# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a PY <<<"$(swarm_python)"
HELPER="${REPO_ROOT}/scripts/live_report.py"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/live-report.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM
GATE="${WORK}/gate.json"
API_LOG="${WORK}/api.json"
TASKS_DIR="${WORK}/tasks"
LISTED="${WORK}/listed.txt"
IDS="${WORK}/ids.txt"
PAGE="${WORK}/page.json"
mkdir -p "${TASKS_DIR}"
: >"${LISTED}"

# One Cloud Logging read into a file. A failed read is exit 2: a report over
# what could not be read would say "no data" where the truth is "not looked".
read_log() {
  local filter="$1" out="$2" rc=0
  gcloud logging read "${filter}" \
    --project "${PROJECT_ID}" \
    --freshness "${HOURS}h" \
    --order desc \
    --limit "${LOG_LIMIT}" \
    --format json >"${out}" 2>"${WORK}/log.err" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    err "gcloud logging read failed (exit ${rc}); reading needs roles/logging.viewer on ${PROJECT_ID}:"
    redact <"${WORK}/log.err" | head -n 3 | sed 's/^/     /' >&2
    exit 2
  fi
  local n
  n="$(jq -r 'if type == "array" then length else 0 end' <"${out}" 2>/dev/null || printf '0')"
  [[ "${n}" -lt "${LOG_LIMIT}" ]] || warn "read the cap of ${LOG_LIMIT} entries: older lines in the window were not read"
  info "${n} entr(ies)"
}

# One API read. Redirected to a file rather than captured with $(...): a
# command substitution is a SUBSHELL, so API_STATUS would never reach this
# shell and every response would read as a success.
fetch() {
  local path="$1" out="$2"
  if ! api_request GET "${API_PREFIX}${path}" >"${out}"; then
    err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}"
    redact <"${out}" >&2 || true
    exit 2
  fi
}

# Read the API's 404 for one task's optional document (no live actions yet)
# as "none", anything else as a failure.
fetch_optional() {
  local path="$1" out="$2"
  if ! api_request GET "${API_PREFIX}${path}" >"${out}"; then
    if [[ "${API_STATUS}" == "404" ]]; then
      : >"${out}"
      return 0
    fi
    err "GET ${API_PREFIX}${path} answered HTTP ${API_STATUS}"
    redact <"${out}" >&2 || true
    exit 2
  fi
}

# --- the display sidecar's metrics lines, our cluster only -------------------
step "Viewer metrics from the display sidecar (cluster ${GKE_CLUSTER})"
read_log "resource.type=\"k8s_container\" AND resource.labels.project_id=\"${PROJECT_ID}\" AND resource.labels.cluster_name=\"${GKE_CLUSTER}\" AND resource.labels.container_name=\"display\" AND jsonPayload.event=\"live_viewer_metrics\"" "${GATE}"

# --- swarm-api's hand-off funnel and ready->frame lines ----------------------
step "Hand-off funnel from ${API_SERVICE}"
read_log "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${API_SERVICE}\" AND (jsonPayload.event=\"live_handoff_funnel\" OR jsonPayload.event=\"live_ready_first_frame\")" "${API_LOG}"

# --- browser tasks the API lists in the window -------------------------------
step "Browser tasks created in the last ${HOURS} h"
listed=0
while read -r profile; do
  [[ -n "${profile}" ]] || continue
  cursor=""
  while :; do
    query="/tasks?view=summary&limit=200&runner_profile=${profile}"
    [[ -z "${cursor}" ]] || query="${query}&page_token=${cursor}"
    fetch "${query}" "${PAGE}"
    cursor=""
    done_listing=0
    while read -r kind value; do
      case "${kind}" in
        task)  printf '%s\n' "${value}" >>"${LISTED}"; listed=$((listed + 1)) ;;
        next)  cursor="${value}" ;;
        older) done_listing=1 ;;
      esac
    done < <("${PY[@]}" "${HELPER}" window --hours "${HOURS}" <"${PAGE}")
    if [[ "${done_listing}" -eq 1 || -z "${cursor}" || "${listed}" -ge "${MAX_TASKS}" ]]; then
      break
    fi
  done
done < <("${PY[@]}" "${HELPER}" profiles)
info "${listed} browser task(s) listed"

# --- each task's start-up and action log -------------------------------------
step "Start-up and action log per task"
{
  "${PY[@]}" "${HELPER}" tasks --gate "${GATE}" --api "${API_LOG}" --hours "${HOURS}"
  cat "${LISTED}"
} | awk 'NF && !seen[$0]++' >"${IDS}"
visited=0
while read -r task_id; do
  [[ "${task_id}" =~ ^task_[a-z0-9]+$ ]] || continue
  [[ "${visited}" -lt "${MAX_TASKS}" ]] || { warn "stopped at --max-tasks ${MAX_TASKS}"; break; }
  visited=$((visited + 1))
  fetch_optional "/tasks/${task_id}/startup" "${TASKS_DIR}/${task_id}.startup.json"
  fetch_optional "/tasks/${task_id}/live/actions" "${TASKS_DIR}/${task_id}.actions.json"
done <"${IDS}"
info "${visited} task(s) visited"

# --- the numbers -------------------------------------------------------------
hr
status=0
"${PY[@]}" "${HELPER}" report --gate "${GATE}" --api "${API_LOG}" --tasks-dir "${TASKS_DIR}" \
  --listed "${LISTED}" --hours "${HOURS}" ${REPORT_ARGS[@]+"${REPORT_ARGS[@]}"} 2>&1 | redact || status=$?
exit "${status}"
