#!/usr/bin/env bash
# flaky-report: print flaky-test candidates -- a CI job that failed and then
# passed at the SAME commit -- over the last N runs of a workflow.
#
# WHY THIS EXISTS. Owner decision, 2026-10-14: "Fix the known flakes and
# quarantine new ones". A flake is invisible in a pull request's final state:
# the rerun went green, and only the run's earlier attempt remembers the red
# job. `gh run list` shows the LATEST attempt only, so this asks for every
# earlier attempt's jobs, and also pairs separate runs at one commit where one
# failed and another passed. Measured with it on 2026-10-14: 8 of the 1,000
# application.yml runs since 2026-10-10 failed and then passed at the same commit.
#
# WHAT IT PRINTS. One line per failed job of a run that later passed at the
# same commit, then, for a pytest or vitest job, the failing test ids its log
# names. It changes nothing: it never edits tests/flaky.txt. The operator
# reads the candidates, files an issue and adds the line (docs/ci.md, "Flaky
# tests and the quarantine"). A job that fails for a network download is a
# candidate too; the fix for those is a pin, a mirror or a retry, not the list.
#
# Usage:
#   scripts/flaky-report.sh [--limit N] [--workflow FILE] [--repo owner/name] [--no-logs]
#
#   --limit N      runs to read, newest first (default 300)
#   --workflow F   the workflow file (default application.yml)
#   --repo R       default: GH_REPO, else the repository gh resolves here
#   --no-logs      list the failed jobs only; do not fetch logs for test ids
#
# Needs `gh` (authenticated, actions: read) and `jq`. Read-only.

set -euo pipefail
# This script touches no project: it reads GitHub's test reports. It says so, so common.sh
# (no default project, DAYONE-DESAGA-2) does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

LIMIT=300
WORKFLOW=application.yml
REPO="${GH_REPO:-}"
LOGS=1
ESC=$'\033'

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) [[ $# -ge 2 ]] || die "--limit needs a number"; LIMIT="$2"; shift 2 ;;
    --workflow) [[ $# -ge 2 ]] || die "--workflow needs a file name"; WORKFLOW="$2"; shift 2 ;;
    --repo) [[ $# -ge 2 ]] || die "--repo needs owner/name"; REPO="$2"; shift 2 ;;
    --no-logs) LOGS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ "${LIMIT}" =~ ^[1-9][0-9]*$ ]] || die "--limit must be a positive integer, got: ${LIMIT}"
require_cmd gh jq

if [[ -z "${REPO}" ]]; then
  REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
    || die "no --repo and gh resolves no repository here"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

step "reading the last ${LIMIT} runs of ${WORKFLOW} in ${REPO}"
gh run list --repo "${REPO}" --workflow "${WORKFLOW}" --limit "${LIMIT}" \
  --json databaseId,headSha,conclusion,attempt,createdAt,displayTitle > "${WORK}/runs.json" < /dev/null
RUNS="$(jq 'length' "${WORK}/runs.json")"
[[ "${RUNS}" -gt 0 ]] || die "gh returned no runs of ${WORKFLOW}: nothing was read, so nothing can be reported"
info "${RUNS} run(s), $(jq -r 'map(.createdAt) | min' "${WORK}/runs.json") to $(jq -r 'map(.createdAt) | max' "${WORK}/runs.json")"

# Every (run, attempt) whose jobs to read, one per line: "<run> <attempt> <sha>".
#  1. a run rerun green: each attempt before its last;
#  2. a run that failed at a commit where another run of this workflow passed.
jq -r '
  (.[] | select(.attempt > 1 and .conclusion == "success")
       | . as $r | range(1; $r.attempt) | "\($r.databaseId) \(.) \($r.headSha)"),
  (group_by(.headSha)[]
       | select(any(.conclusion == "success") and any(.conclusion == "failure"))
       | .[] | select(.conclusion == "failure") | "\(.databaseId) \(.attempt) \(.headSha)")
' "${WORK}/runs.json" | sort -u > "${WORK}/attempts.txt"

ATTEMPTS="$(wc -l < "${WORK}/attempts.txt" | tr -d ' ')"
info "${ATTEMPTS} failed attempt(s) at a commit that later passed"
if [[ "${ATTEMPTS}" -eq 0 ]]; then
  ok "no fail-then-pass at one commit in ${RUNS} run(s)"
  exit 0
fi

CANDIDATES=0
while read -r run attempt sha; do
  gh api "repos/${REPO}/actions/runs/${run}/attempts/${attempt}/jobs?per_page=100" \
    --jq '.jobs[] | select(.conclusion == "failure") | "\(.id)\t\(.name)"' > "${WORK}/jobs.tsv" < /dev/null \
    || { warn "run ${run} attempt ${attempt}: its jobs could not be read"; continue; }
  while IFS=$'\t' read -r job name; do
    # ci-gate and the `format / unit tests` roll-up only repeat a job above.
    case "${name}" in ci-gate|"format / unit tests"|"swarm-ui typecheck / component tests") continue ;; esac
    CANDIDATES=$((CANDIDATES + 1))
    printf '%s\t%s\trun %s attempt %s\thttps://github.com/%s/actions/runs/%s/job/%s\n' \
      "${sha:0:12}" "${name}" "${run}" "${attempt}" "${REPO}" "${run}" "${job}"
    [[ "${LOGS}" -eq 1 ]] || continue
    if ! gh api "repos/${REPO}/actions/jobs/${job}/logs" > "${WORK}/job.log" 2>/dev/null < /dev/null; then
      printf '\t  (log expired or unreadable)\n'
      continue
    fi
    # pytest's short summary (`FAILED <node id> - ...`) and vitest's
    # (`FAIL  <file> > <describe> > <name>`), colour codes and timestamps off.
    sed -e "s/${ESC}\[[0-9;]*m//g" -e 's/^[0-9TZ:.-]* //' "${WORK}/job.log" \
      | grep -E '^ *(FAILED|ERROR) tests/|^ *FAIL +(src|test)/' \
      | sed -e 's/ - .*$//' -e 's/^ *//' | sort -u | sed 's/^/    /' || true
  done < "${WORK}/jobs.tsv"
done < "${WORK}/attempts.txt"

hr
info "${CANDIDATES} candidate job failure(s) in ${ATTEMPTS} attempt(s) over ${RUNS} run(s)"
dim "Fix the cause where it is known. A test whose cause is not known yet goes in tests/flaky.txt"
dim "with its issue and today's date (docs/ci.md, \"Flaky tests and the quarantine\")."
