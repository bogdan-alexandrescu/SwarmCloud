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
#      (`cancelled`, `skipped`, or absent).
#
# WHY IT WAITS (#2664). The images stage fails the moment `build images` is
# cancelled, but GitHub takes seconds to finish cancelling the rest of the run:
# measured 2026-10-28, the run concluded `cancelled` up to 33 s after the
# release's stage failed. Read at once, the run has no conclusion yet and a
# superseded commit failed red with "no newer commit superseded it". So while
# main is ahead and the run has not concluded, this reads it again every
# --poll seconds, for at most --wait seconds. It stops waiting early when
# `build images` itself succeeded or failed, which no later conclusion turns
# into a supersession. A run that is still unconcluded at the deadline, like
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

runs_path="repos/${SUP_REPO}/actions/workflows/application.yml/runs?head_sha=${SUP_SHA}&branch=main&event=push&per_page=1"
deadline=$((SECONDS + SUP_WAIT))
while :; do
  run="$(gh api "${runs_path}" --jq '.workflow_runs[0] // {} | "\(.id // "") \(.conclusion // "")"' 2>/dev/null)" || run=""
  run_id="${run%% *}"
  conclusion="${run#* }"
  [[ -n "${run_id}" ]] || not_superseded "no application push run of ${SUP_SHA} on main"
  build="$(gh api "repos/${SUP_REPO}/actions/runs/${run_id}/jobs?per_page=100" \
    --jq '[.jobs[] | select(.name == "build images")][0].conclusion // "none"' 2>/dev/null)" || build=""
  if [[ -n "${conclusion}" ]]; then
    break
  fi
  case "${build}" in
    success|failure) not_superseded "build images in application run ${run_id} concluded ${build}" ;;
  esac
  if (( SECONDS >= deadline )); then
    not_superseded "application run ${run_id} had not concluded after ${SUP_WAIT}s"
  fi
  log "application run ${run_id} has not concluded yet; reading it again in ${SUP_POLL}s"
  sleep "${SUP_POLL}"
done

[[ "${conclusion}" == cancelled ]] || not_superseded "application run ${run_id} concluded ${conclusion}"
case "${build}" in
  cancelled|skipped|none) printf '%s\n' "${run_id}" ;;
  *) not_superseded "build images in application run ${run_id} concluded ${build:-unreadable}" ;;
esac
