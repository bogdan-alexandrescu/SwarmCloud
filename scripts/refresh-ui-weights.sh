#!/usr/bin/env bash
#
# refresh-ui-weights: rewrite tests/unit/scripts/ui_test_durations.json, the
# per-file vitest CI-seconds related-tests.sh cuts its vitest runs by, from the
# logs of main's latest green application.yml run.
#
#   scripts/refresh-ui-weights.sh [--run <id>] [--limit N] [--threshold S] [--replace] [--repo owner/name]
#
# WHY (UI-WEIGHTS, part of #2362). scripts/lib/related_tests.py cut the vitest
# selection into runs of 50 files, assuming ~2.9 s a file. The nine
# spacing.*.test.tsx slices take 252-485 s each, landed in one run that could
# not finish inside its 540 s limit, and 40+ agents timed out in 2026-10-25/26.
# Runs are now cut by recorded weight, so the weights have to be refreshable.
#
# WHERE THE FIGURES COME FROM. vitest's default reporter prints one line a file
# in each 'swarm-ui component tests (k/4)' job's log:
#   ✓ src/__tests__/spacing.capacity.test.tsx (1 test) 259018ms
# This reads those jobs through `gh run view --job <id> --log` and hands the
# text to scripts/lib/ui_weights.py. It changes no workflow on purpose: a
# workflow file can only be pushed by swarm-api, and a lane that edited
# application.yml for this could not publish (wf_2e4899f9).
#
#   --run        the application.yml run to read (default: the newest
#                successful run on main whose component-test jobs all passed
#                -- a run that skipped them, because a run of the same tree
#                had passed, has no timings)
#   --limit      how many successful main runs to look through (default 20)
#   --threshold  record only files over this many seconds (default: the
#                parser's THRESHOLD_SECONDS, 20)
#   --replace    write the measured figures alone; by default a recorded
#                figure is never lowered (ui_weights.py says why)
#   --repo       defaults to GH_REPO, then GITHUB_REPOSITORY, then gh's own
#                reading of the current checkout
#
# Exit 0 when the file was rewritten, 1 when no run or no timing could be read
# (an unread log is never "every file is light"). Review the diff and commit it.
set -euo pipefail

# This script touches no project: it reads GitHub's test reports. It says so, so common.sh
# (no default project, DAYONE-DESAGA-2) does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

PARSER="${REPO_ROOT}/scripts/lib/ui_weights.py"
WEIGHTS="${REPO_ROOT}/tests/unit/scripts/ui_test_durations.json"
JOB_PREFIX="swarm-ui component tests"

run_id=""
limit=20
threshold=""
replace=0
repo="${GH_REPO:-${GITHUB_REPOSITORY:-}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run) [[ $# -ge 2 ]] || die "--run needs a value"; run_id="$2"; shift 2 ;;
    --limit) [[ $# -ge 2 ]] || die "--limit needs a value"; limit="$2"; shift 2 ;;
    --threshold) [[ $# -ge 2 ]] || die "--threshold needs a value"; threshold="$2"; shift 2 ;;
    --replace) replace=1; shift ;;
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ -z "${run_id}" || "${run_id}" =~ ^[0-9]+$ ]] || die "--run must be a run id, got '${run_id}'"
[[ "${limit}" =~ ^[1-9][0-9]*$ ]] || die "--limit must be a positive whole number, got '${limit}'"
[[ -z "${threshold}" || "${threshold}" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
  || die "--threshold must be a number of seconds, got '${threshold}'"
[[ -z "${repo}" || "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
  || die "the repository must be owner/name, got '${repo}'"

require_cmd gh jq python3

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-ui-weights.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

repo_args=()
[[ -z "${repo}" ]] || repo_args=(--repo "${repo}")

# The ids of a run's component-test jobs, one a line, when every one of them
# passed; nothing otherwise.
component_jobs() {
  gh run view "$1" ${repo_args[@]+"${repo_args[@]}"} --json jobs >"${WORK}/jobs.json" 2>"${WORK}/gh.err" \
    || return 0
  jq -r --arg p "${JOB_PREFIX}" '
    [.jobs[] | select(.name | startswith($p))] as $j
    | if ($j | length) > 0 and all($j[]; .conclusion == "success")
      then $j[].databaseId else empty end' "${WORK}/jobs.json"
}

: >"${WORK}/job-ids"
if [[ -n "${run_id}" ]]; then
  component_jobs "${run_id}" >"${WORK}/job-ids"
  [[ -s "${WORK}/job-ids" ]] || die "run ${run_id} has no passed '${JOB_PREFIX}' job to read"
else
  if ! gh run list ${repo_args[@]+"${repo_args[@]}"} --workflow application.yml --branch main \
      --status success --limit "${limit}" --json databaseId >"${WORK}/runs.json" 2>"${WORK}/gh.err"; then
    redact <"${WORK}/gh.err" >&2
    die "gh run list could not read application.yml's runs on main"
  fi
  jq -e 'type == "array"' "${WORK}/runs.json" >/dev/null 2>&1 || die "gh run list did not answer a JSON array"
  jq -r '.[].databaseId' "${WORK}/runs.json" >"${WORK}/run-ids"
  read_runs=0
  while IFS= read -r candidate; do
    read_runs=$((read_runs + 1))
    component_jobs "${candidate}" >"${WORK}/job-ids"
    if [[ -s "${WORK}/job-ids" ]]; then
      run_id="${candidate}"
      break
    fi
  done <"${WORK}/run-ids"
  [[ -n "${run_id}" ]] \
    || die "none of the ${read_runs} successful application.yml run(s) on main read ran '${JOB_PREFIX}' (raise --limit)"
fi

: >"${WORK}/vitest.log"
jobs_read=0
while IFS= read -r job; do
  if ! gh run view ${repo_args[@]+"${repo_args[@]}"} --job "${job}" --log >>"${WORK}/vitest.log" 2>"${WORK}/gh.err"; then
    redact <"${WORK}/gh.err" >&2
    die "gh could not read the log of job ${job} in run ${run_id}"
  fi
  jobs_read=$((jobs_read + 1))
done <"${WORK}/job-ids"
info "run ${run_id}: read ${jobs_read} '${JOB_PREFIX}' job log(s)"

parser_args=(--log "${WORK}/vitest.log" --out "${WEIGHTS}")
[[ -z "${threshold}" ]] || parser_args+=(--threshold "${threshold}")
[[ "${replace}" -eq 0 ]] || parser_args+=(--replace)
python3 "${PARSER}" "${parser_args[@]}" || die "no vitest timing could be read from run ${run_id}"
ok "rewrote ${WEIGHTS#"${REPO_ROOT}"/} from run ${run_id}; review the diff and commit it"
