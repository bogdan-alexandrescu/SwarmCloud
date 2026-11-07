#!/usr/bin/env bash
#
# accept-live-browser-streak: has the scheduled live-browser acceptance group
# passed five runs in a row, so that the operator may move it back into the
# post-release waves of .github/workflows/accept.yml?
#
# WHY IT IS OFF THE RED PATH (owner decision 2026-10-14, lane SPEED-C2).
# accept.yml had run 159 times; 43 completed and 21 of those failed (49%).
# The `acceptance (live-browser)` job failed 18 of 34 runs, every other group
# 0-3, and each red run updated the one acceptance `bug` issue, so most of
# that issue's history was one group's noise. The group now runs as
# accept.yml's `live-browser` job, every three hours and on a dispatch with
# scope `live-browser`, and files its own issue (scripts/accept-report.sh,
# ACCEPT_KIND=live-browser). docs/ci.md, "live-browser runs on a schedule",
# has the failure causes found that day.
#
# WHAT IT COUNTS. Completed SCHEDULED runs of accept.yml on main, newest
# first (a scheduled run runs only the live-browser job and its report, so the
# run's conclusion is the group's). A success counts; a cancelled or skipped
# run neither counts nor breaks the streak (a post-release run's quiesce
# cancels a scheduled run in flight, which says nothing about the group);
# anything else -- failure, timed_out, startup_failure -- ends it. A dispatch
# is a probe and is not counted: the schedule is the measurement.
#
# Usage:
#   scripts/accept-live-browser-streak.sh [--need N] [--limit M] [--repo owner/name]
#
#   --need   the streak that promotes (default 5, the owner's number)
#   --limit  how many scheduled runs to read (default 30: ten days at three
#            hours would be 80, but a streak of five is decided long before)
#   --repo   defaults to GH_REPO, then GITHUB_REPOSITORY, then gh's own
#            reading of the current checkout
#
# Prints `streak=N`, `need=N`, `promote=true|false` on stdout and the runs it
# read on stderr. Exit 0 when the streak reached --need, 3 when it did not,
# 1 when the runs could not be read (an unreadable listing is never a streak
# of zero).
set -euo pipefail

# This script touches no project: it reads GitHub. It says so, so common.sh
# (no default project, DAYONE-DESAGA-2) does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

need=5
limit=30
repo="${GH_REPO:-${GITHUB_REPOSITORY:-}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --need) [[ $# -ge 2 ]] || die "--need needs a value"; need="$2"; shift 2 ;;
    --limit) [[ $# -ge 2 ]] || die "--limit needs a value"; limit="$2"; shift 2 ;;
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ "${need}" =~ ^[1-9][0-9]*$ ]] || die "--need must be a positive whole number, got '${need}'"
[[ "${limit}" =~ ^[1-9][0-9]*$ ]] || die "--limit must be a positive whole number, got '${limit}'"
[[ -z "${repo}" || "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
  || die "the repository must be owner/name, got '${repo}'"

require_cmd gh jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-lb-streak.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

repo_args=()
[[ -z "${repo}" ]] || repo_args=(--repo "${repo}")

# To a file, not a command substitution: the exit status is what tells an
# unreadable listing from an empty one.
if ! gh run list ${repo_args[@]+"${repo_args[@]}"} --workflow accept.yml --branch main --event schedule \
    --status completed --limit "${limit}" --json databaseId,conclusion,createdAt,url \
    >"${WORK}/runs.json" 2>"${WORK}/runs.err"; then
  redact <"${WORK}/runs.err" | head -n 3 | sed 's/^/     /' >&2
  die "could not list the scheduled accept.yml runs; no streak is reported"
fi
jq -e 'type == "array"' "${WORK}/runs.json" >/dev/null 2>&1 || die "gh run list did not answer a JSON array"

streak=0
read_runs=0
broke=""
# '|', not a tab: a tab is IFS whitespace, so `read` would fold an empty
# conclusion into the next field.
while IFS='|' read -r id conclusion created url; do
  [[ -n "${id}" ]] || continue
  read_runs=$((read_runs + 1))
  case "${conclusion}" in
    success)
      streak=$((streak + 1))
      info "run ${id} (${created}) passed -- ${url}"
      ;;
    cancelled|skipped)
      info "run ${id} (${created}) was ${conclusion}; not counted -- ${url}"
      ;;
    *)
      broke="run ${id} (${created}) ended ${conclusion:-without a conclusion} -- ${url}"
      break
      ;;
  esac
  [[ "${streak}" -lt "${need}" ]] || break
done < <(jq -r '.[] | [(.databaseId | tostring), (.conclusion // ""), (.createdAt // ""), (.url // "")] | join("|")' "${WORK}/runs.json")

info "read ${read_runs} scheduled run(s), newest first"
[[ -z "${broke}" ]] || warn "the streak ends at ${broke}"

printf 'streak=%s\nneed=%s\n' "${streak}" "${need}"
if [[ "${streak}" -ge "${need}" ]]; then
  printf 'promote=true\n'
  ok "live-browser passed ${streak} scheduled runs in a row: move it back into accept.yml's wave 2 (docs/ci.md, \"live-browser runs on a schedule\")"
  exit 0
fi
printf 'promote=false\n'
warn "live-browser has passed ${streak} of the ${need} scheduled runs in a row it needs; it stays on the schedule"
exit 3
