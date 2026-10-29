#!/usr/bin/env bash
#
# refresh-unit-weights: rewrite tests/unit/scripts/unit_test_durations.json,
# the per-file CI-seconds the unit shards and changed-guards.sh split by, from
# the weights main's own unit shards measured.
#
#   scripts/refresh-unit-weights.sh [--run <id>] [--limit N] [--repo owner/name]
#
# WHY (#2364). The file was refreshed only by hand, from a recipe in
# docs/ci.md. New test files went unweighted between refreshes until a hard
# 80 % coverage floor in test_unit_shards.py turned main and every pull request
# red (1069 of 1337 weighted). The floor now only warns (unit_shards.py
# WEIGHT_COVERAGE_WARN); this is the refresh, in one command.
#
# WHERE THE FIGURES COME FROM. On main, every `unit tests (k/N)` shard uploads
# what it measured, per file, as the artifact `unit-durations-<k>`
# (application.yml, "record this shard's measured durations"). This downloads
# them with `gh run download` and hands them to scripts/lib/merge_unit_weights.py
# (a measured weight replaces the stored one, a deleted file is dropped, an
# unmeasured file keeps its weight). Like refresh-ui-weights.sh it changes no
# workflow: a workflow file can only be pushed by swarm-api.
#
#   --run    the application.yml run to read (default: the newest successful
#            push run on main that has unit-durations artifacts -- a push run
#            that skipped python-unit, because a run of the identical tree had
#            passed, uploads none, so this walks back past it)
#   --limit  how many successful main push runs to look through (default 20)
#   --repo   defaults to GH_REPO, then GITHUB_REPOSITORY, then gh's own
#            reading of the current checkout
#
# Exit 0 when the weights were merged (the merge says whether a byte changed),
# 1 when no run's weights could be read or merged (an unread download is never
# "nothing to refresh"). Review the diff and commit it.
set -euo pipefail

# This script touches no project: it reads CI timings from GitHub and writes
# the checkout. It says so, so common.sh (no default project, DAYONE-DESAGA-2)
# does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

MERGER="${REPO_ROOT}/scripts/lib/merge_unit_weights.py"
WEIGHTS="${REPO_ROOT}/tests/unit/scripts/unit_test_durations.json"
ARTIFACTS="unit-durations-*"

run_id=""
limit=20
repo="${GH_REPO:-${GITHUB_REPOSITORY:-}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run) [[ $# -ge 2 ]] || die "--run needs a value"; run_id="$2"; shift 2 ;;
    --limit) [[ $# -ge 2 ]] || die "--limit needs a value"; limit="$2"; shift 2 ;;
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ -z "${run_id}" || "${run_id}" =~ ^[0-9]+$ ]] || die "--run must be a run id, got '${run_id}'"
[[ "${limit}" =~ ^[1-9][0-9]*$ ]] || die "--limit must be a positive whole number, got '${limit}'"
[[ -z "${repo}" || "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
  || die "the repository must be owner/name, got '${repo}'"

require_cmd gh jq python3

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-unit-weights.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

repo_args=()
[[ -z "${repo}" ]] || repo_args=(--repo "${repo}")

# Download run $1's weight artifacts into ${WORK}/dl; fails when it has none.
download() {
  rm -rf "${WORK}/dl"
  mkdir -p "${WORK}/dl"
  gh run download "$1" ${repo_args[@]+"${repo_args[@]}"} -p "${ARTIFACTS}" -D "${WORK}/dl" \
    >/dev/null 2>"${WORK}/gh.err" || return 1
  [[ -n "$(find "${WORK}/dl" -name unit-durations.json -type f -print -quit)" ]]
}

if [[ -n "${run_id}" ]]; then
  if ! download "${run_id}"; then
    redact <"${WORK}/gh.err" >&2
    die "run ${run_id} has no '${ARTIFACTS}' artifact to read"
  fi
else
  if ! gh run list ${repo_args[@]+"${repo_args[@]}"} --workflow application.yml --branch main \
      --event push --status success --limit "${limit}" --json databaseId >"${WORK}/runs.json" 2>"${WORK}/gh.err"; then
    redact <"${WORK}/gh.err" >&2
    die "gh run list could not read application.yml's push runs on main"
  fi
  jq -e 'type == "array"' "${WORK}/runs.json" >/dev/null 2>&1 || die "gh run list did not answer a JSON array"
  jq -r '.[].databaseId' "${WORK}/runs.json" >"${WORK}/run-ids"
  read_runs=0
  while IFS= read -r candidate; do
    read_runs=$((read_runs + 1))
    if download "${candidate}"; then
      run_id="${candidate}"
      break
    fi
  done <"${WORK}/run-ids"
  [[ -n "${run_id}" ]] \
    || die "none of the ${read_runs} successful push run(s) of application.yml on main read has '${ARTIFACTS}' artifacts (raise --limit)"
fi

info "run ${run_id}: merging its '${ARTIFACTS}' artifacts into ${WEIGHTS#"${REPO_ROOT}"/}"
python3 "${MERGER}" --root "${REPO_ROOT}" --stored "${WEIGHTS}" --measured "${WORK}/dl" \
  || die "the weights of run ${run_id} could not be merged"
ok "merged the weights of run ${run_id}; review the diff and commit it"
