#!/usr/bin/env bash
# Reconciliation pass duration, and what each pass actually examined.
#
# WHY THIS READS LOGS INSTEAD OF TRIGGERING A PASS
# ------------------------------------------------
# The obvious collector is `POST /reconcile` on swarm-reconciler, timed. It is
# the wrong one, for two reasons and the first is disqualifying:
#
#   1. A pass MUTATES. It terminates executions, releases leases and deletes
#      job resources. A benchmark that repairs production every time someone
#      wants a latency figure is not a benchmark, and this project's rule is
#      that nothing creates, modifies or deletes in the cloud beyond what a
#      test is explicitly designed to create and then clean up. There is no
#      dry-run switch on that route -- `dry_run` comes from the service's
#      environment, not from the request.
#   2. A triggered pass is not a representative pass. The scheduled ones run
#      against whatever state has accumulated; one fired by hand seconds after
#      the last tick examines almost nothing and reports a wonderful duration.
#
# `Reconciler.run_once` already logs its entire report as structured fields --
# `reconciliation pass complete` with duration_seconds, tasks_examined,
# leases_examined, executions_examined, findings and errors. So the numbers
# exist for every real pass. This reads them back. Cost: a handful of Cloud
# Logging list requests, no writes, nothing triggered. Under a cent, about ten
# seconds.
#
# THE MEASUREMENT THAT MATTERS MOST IS NOT THE DURATION
# -----------------------------------------------------
# When a backend cannot be listed -- the GKE 401 seen on 2026-09-22 -- the
# reconciler correctly SKIPS that backend's findings rather than repairing
# against a list it could not read, appends one string to `report.errors`, and
# returns a pass that looks entirely healthy and is FASTER than a complete one.
# Every counter goes down. A duration threshold rewards it.
#
# So `reconcile.backend_errors` is a metric with an absolute limit of zero, and
# `reconcile.executions_examined` is gated too: a pass that examined no
# executions on a platform with running work has not reconciled anything,
# however quickly it finished.
#
# Usage: scripts/bench-reconcile.sh [--freshness 6h] [--limit 50]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/benchlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/benchlib.sh"

load_env

FRESHNESS="6h"
LIMIT=50

while [[ $# -gt 0 ]]; do
  case "$1" in
    --freshness) FRESHNESS="$2"; shift 2 ;;
    --limit)     LIMIT="$2"; shift 2 ;;
    -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

case "${FRESHNESS}" in
  *h) WINDOW_SECONDS=$(( ${FRESHNESS%h} * 3600 )) ;;
  *m) WINDOW_SECONDS=$(( ${FRESHNESS%m} * 60 )) ;;
  *d) WINDOW_SECONDS=$(( ${FRESHNESS%d} * 86400 )) ;;
  *) die "--freshness takes a value like 30m, 6h or 2d; got '${FRESHNESS}'" ;;
esac

step "Reconciliation passes in the last ${FRESHNESS}"
bench_init reconcile

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-reconcile.XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT INT TERM

SINCE="$(python3 -c "
import datetime, sys
print((datetime.datetime.now(datetime.timezone.utc)
       - datetime.timedelta(seconds=${WINDOW_SECONDS})).isoformat().replace('+00:00', 'Z'))
")"
info "window starts ${SINCE}"

# The Logging REST API rather than `gcloud logging read`: the verify image runs
# these suites inside the VPC and carries no Cloud SDK on purpose, because the
# SDK base image ships unfixed HIGH/CRITICAL CVEs that `make push` refuses to
# promote. Read-only; entries:list creates nothing.
FILTER="$(printf 'resource.type="cloud_run_revision" AND resource.labels.service_name="%s" AND jsonPayload.message="reconciliation pass complete" AND timestamp>="%s"' \
  "${RECONCILER_SERVICE}" "${SINCE}")"
BODY="$(jq -nc --arg p "${PROJECT_ID}" --arg f "${FILTER}" --argjson n "${LIMIT}" \
  '{resourceNames:["projects/" + $p], filter:$f, orderBy:"timestamp desc", pageSize:$n}')"

RC=0
curl -sS -m "${HTTP_TIMEOUT}" \
  -H "Authorization: Bearer $(access_token)" \
  -H "Content-Type: application/json" \
  --data-binary "${BODY}" \
  "https://logging.googleapis.com/v2/entries:list" >"${WORK}/entries.json" 2>"${WORK}/err" || RC=$?

if [[ "${RC}" -ne 0 ]]; then
  redact <"${WORK}/err" >&2
  die "the Logging API could not be reached. That is a transport failure, not a platform with no reconciler passes."
fi
if jq -e '.error' "${WORK}/entries.json" >/dev/null 2>&1; then
  jq -r '.error.message // "unknown error"' "${WORK}/entries.json" | redact >&2
  die "the Logging API returned an error. An error is NOT an empty result: this says nothing about whether passes ran."
fi

PASSES="$(jq '[.entries // [] | .[] | .jsonPayload] | length' "${WORK}/entries.json")"
info "${PASSES} pass(es) found"

# Zero passes is the finding, not the absence of one. A reconciler that has not
# run in six hours is a platform whose stale leases are not being reclaimed,
# and reporting "no samples, all good" is the failure mode this whole file
# exists to refuse.
if [[ "${PASSES}" -eq 0 ]]; then
  for metric in reconcile.duration_seconds reconcile.tasks_examined \
                reconcile.leases_examined reconcile.executions_examined \
                reconcile.findings reconcile.backend_errors; do
    bench_missing "${metric}" "" \
      "no '${RECONCILER_SERVICE}' pass logged a complete report in the last ${FRESHNESS}" ""
  done
  bench_finish
  exit $?
fi

jq -c '.entries // [] | .[] | .jsonPayload' "${WORK}/entries.json" >"${WORK}/passes.jsonl"

while IFS= read -r pass; do
  [[ -n "${pass}" ]] || continue
  dry="$(printf '%s' "${pass}" | jq -r 'if .dry_run == true then "true" else "false" end')"
  labels="$(bench_label dry_run "${dry}")"

  # `// empty`, never `// 0`. A report missing a counter is a report from a
  # different version of the reconciler, and substituting zero would publish a
  # pass that examined nothing as a pass that examined nothing SUCCESSFULLY.
  for pair in duration_seconds:s tasks_examined:tasks leases_examined:leases \
              executions_examined:executions findings:findings; do
    field="${pair%%:*}"; unit="${pair##*:}"
    value="$(printf '%s' "${pass}" | jq -r --arg f "${field}" '.[$f] // empty')"
    if [[ -z "${value}" || "${value}" == "null" ]]; then
      bench_missing "reconcile.${field}" "${unit}" \
        "the pass report carried no '${field}'" "${labels}"
    else
      bench_sample "reconcile.${field}" "${unit}" "${value}" "${labels}"
    fi
  done

  # The one that turns a silently skipped backend into a red gate. `errors` is
  # an ARRAY of strings; its length is the metric, and thresholds.json holds it
  # at an absolute maximum of zero.
  errors="$(printf '%s' "${pass}" | jq -r '(.errors // []) | length')"
  bench_sample reconcile.backend_errors errors "${errors}" "${labels}"
  if [[ "${errors}" -gt 0 ]]; then
    printf '%s' "${pass}" | jq -r '(.errors // [])[]' | redact | sed 's/^/       error: /' >&2
  fi
done <"${WORK}/passes.jsonl"

bench_finish
