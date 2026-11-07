#!/usr/bin/env bash
# Did a newer push to main cancel this commit's build? Asked by release.yml's
# and hotfix.yml's `superseded while it waited?` step after the images stage
# FAILED, to tell a build a newer push cancelled (green, superseded) from a
# real build or scan failure (red).
#
# Superseded only when all three hold:
#   1. main has moved past the commit (the compare API says `ahead`);
#   2. application.yml's push run of the commit on main concluded `cancelled`
#      (#2438: one concurrency group on main, newest wins);
#   3. its `build images` job neither succeeded nor failed in that run
#      (`cancelled`, `skipped`, or absent) -- or, since the build stopped
#      waiting for main's tests (owner pick, PR 3344), it succeeded and a
#      test job that gates the release (CI_RELEASE_TEST_JOBS, common.sh)
#      neither passed nor failed: cancelled, or never ran.
#
# WHY IT WAITS (#2664). The images stage fails the moment `build images` is
# cancelled, but GitHub takes seconds to finish cancelling the rest of the run:
# measured 2026-10-28, the run concluded `cancelled` up to 33 s after the
# release's stage failed. Read at once, the run has no conclusion yet and a
# superseded commit failed red with "no newer commit superseded it". So while
# main is ahead and the run has not concluded, this reads it again every
# --poll seconds, for at most --wait seconds. It stops waiting early when
# `build images` or a test that gates the release failed, which no later
# conclusion turns into a supersession. A run that is still unconcluded at the deadline, like
# any API answer it cannot read, is NOT superseded: the release stays red
# rather than guess green over a real failure.
#
# Usage:
#   build-superseded.sh --sha SHA --repo OWNER/NAME [--wait SECONDS] [--poll SECONDS]
#
# stdout: the cancelled application run's id, when superseded.
# Exit 0: superseded. Exit 1: not superseded (the reason on stderr).
# Needs `gh` with GH_TOKEN, and nothing else; it moves nothing.

set -euo pipefail
# This script touches no project: it reads GitHub only. It says so, so
# common.sh (no default project, DAYONE-DESAGA-2) does not stop on an unset
# PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SUP_SHA=""
SUP_REPO=""
# 90 s: the slowest measured cancel took 33 s (#2664); the bound keeps a real
# failure's red to at most a minute and a half later than before.
SUP_WAIT="${SWARM_SUPERSEDED_WAIT_SECONDS:-90}"
SUP_POLL="${SWARM_SUPERSEDED_POLL_SECONDS:-5}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SUP_SHA="${2:-}"; shift 2 ;;
    --repo) SUP_REPO="${2:-}"; shift 2 ;;
    --wait) SUP_WAIT="${2:-}"; shift 2 ;;
    --poll) SUP_POLL="${2:-}"; shift 2 ;;
    *) die "build-superseded.sh: unknown argument: $1" ;;
  esac
done
[[ -n "${SUP_SHA}" && -n "${SUP_REPO}" ]] || die "build-superseded.sh: --sha and --repo are required"
[[ "${SUP_WAIT}" =~ ^[0-9]+$ && "${SUP_POLL}" =~ ^[0-9]+$ ]] || die "build-superseded.sh: --wait and --poll take whole seconds"

not_superseded() { log "not superseded: $1"; exit 1; }

status="$(gh api "repos/${SUP_REPO}/compare/${SUP_SHA}...main" --jq '.status' 2>/dev/null)" || status=""
[[ "${status}" == ahead ]] || not_superseded "main is not ahead of ${SUP_SHA} (compare: ${status:-unreadable})"

# One read of the run's jobs answers both questions; see the loop. The names
# go in as a JSON array literal: `gh api --jq` takes no --arg.
TEST_JOBS_JSON="$(printf '%s\n' "${CI_RELEASE_TEST_JOBS[@]}" | jq -R . | jq -cs .)"
# shellcheck disable=SC2016  # jq's $names, not the shell's
JOBS_JQ='(.jobs // []) as $all
  | ([ $all[] | select(.name == "build images") ][0].conclusion // "none") as $b
  | [ '"${TEST_JOBS_JSON}"'[] as $n | [ $all[] | select(.name == $n) ] ] as $g
  | (if any($g[]; any(.[]; .conclusion != null and .conclusion != "success" and .conclusion != "skipped" and .conclusion != "cancelled")) then "failed"
     elif all($g[]; length > 0 and all(.[]; .conclusion == "success" or .conclusion == "skipped")) then "passed"
     else "unfinished" end) as $t
  | "\($b) \($t)"'
runs_path="repos/${SUP_REPO}/actions/workflows/application.yml/runs?head_sha=${SUP_SHA}&branch=main&event=push&per_page=1"
deadline=$((SECONDS + SUP_WAIT))
while :; do
  run="$(gh api "${runs_path}" --jq '.workflow_runs[0] // {} | "\(.id // "") \(.conclusion // "")"' 2>/dev/null)" || run=""
  run_id="${run%% *}"
  conclusion="${run#* }"
  [[ -n "${run_id}" ]] || not_superseded "no application push run of ${SUP_SHA} on main"
  # "<build images' conclusion> <the release's tests>": the tests are
  # `failed` when one of CI_RELEASE_TEST_JOBS (common.sh) concluded anything
  # but success, skipped or cancelled, `passed` when every one concluded
  # success or skipped, and `unfinished` otherwise (cancelled, running or
  # absent).
  jobs="$(gh api "repos/${SUP_REPO}/actions/runs/${run_id}/jobs?per_page=100" \
    --jq "${JOBS_JQ}" 2>/dev/null)" || jobs=""
  build="${jobs%% *}"
  tests="${jobs#* }"
  [[ "${jobs}" == *" "* ]] || tests=""
  if [[ -n "${conclusion}" ]]; then
    break
  fi
  # A failure is final whatever the run concludes, and so is a build that
  # succeeded with its tests passed. A build that succeeded alone is not,
  # since the owner's pick on PR 3344: the tests that gate the release run
  # beside it, and a newer push may yet cancel them.
  [[ "${build}" != failure ]] || not_superseded "build images in application run ${run_id} concluded failure"
  [[ "${tests}" != failed ]] || not_superseded "a test job that gates the release failed in application run ${run_id}"
  [[ "${build}" != success || "${tests}" != passed ]] \
    || not_superseded "build images and the tests that gate the release all passed in application run ${run_id}"
  if (( SECONDS >= deadline )); then
    not_superseded "application run ${run_id} had not concluded after ${SUP_WAIT}s"
  fi
  log "application run ${run_id} has not concluded yet; reading it again in ${SUP_POLL}s"
  sleep "${SUP_POLL}"
done

[[ "${conclusion}" == cancelled ]] || not_superseded "application run ${run_id} concluded ${conclusion}"
[[ "${tests}" != failed ]] || not_superseded "a test job that gates the release failed in application run ${run_id}"
case "${build}" in
  cancelled|skipped|none) printf '%s\n' "${run_id}" ;;
  # Built, and the newer push cancelled the tests that gate its release before
  # they passed: the stage refused it for that, and the newer commit ships it.
  # Tests that passed, or failed, leave the stage's failure its own.
  success)
    [[ "${tests}" == unfinished ]] \
      || not_superseded "build images in application run ${run_id} concluded success and the tests that gate the release ${tests:-could not be read}"
    printf '%s\n' "${run_id}" ;;
  *) not_superseded "build images in application run ${run_id} concluded ${build:-unreadable}" ;;
esac
