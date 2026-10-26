#!/usr/bin/env bash
# refresh-unit-weights: refresh the unit-test shard weights from main's newest
# green push run, and open (or update) one pull request with them.
#
#   scripts/refresh-unit-weights.sh                 (CI: commit, push, open the PR)
#   scripts/refresh-unit-weights.sh --write-only    (by hand: rewrite the file only)
#   ... [--run <application.yml run id>]            (that run, not the newest)
#
# WHY THIS EXISTS (#2364). tests/unit/scripts/unit_test_durations.json is what
# scripts/lib/unit_shards.py splits the unit suite by. It was refreshed only by
# a person following a recipe in docs/ci.md, new test files outran the
# refresh, and the 80 % coverage floor -- then a hard assertion -- turned main
# and every pull request red (1069 of 1337 weighted at 82638be8, until #2312).
# The floor is a warning now; this keeps it from firing.
# .github/workflows/unit-weights-refresh.yml runs it daily and on dispatch.
#
# WHICH RUN. The newest successful `push` run of application.yml on main that
# still has `unit-durations-*` artifacts (each python-unit shard on main
# uploads one, 30 days). A push run can skip python-unit when the identical
# tree was already tested, and uploads nothing, so the last
# UNIT_WEIGHTS_LOOKBACK (default 20) green push runs are walked back newest
# first until one has them; none of them having any is a failure, never a
# clean "nothing to do".
#
# THE MERGE is scripts/lib/merge_unit_weights.py: measured weights replace
# stored ones, deleted files drop, existing unmeasured files keep their
# weight, written in the shards' own format. The file unchanged, this exits 0
# and opens nothing.
#
# THE PULL REQUEST. One branch, chore/unit-weights-refresh, owned by this
# script: each run rebuilds it as main plus one commit and force-updates it
# (no other branch is ever forced). One pull request from it into main,
# opened or retitled in place. It is pushed and opened with the merge App's
# token (APP_TOKEN): a branch pushed or a pull request opened with the
# workflow's GITHUB_TOKEN starts no workflow, so the refresh would sit with no
# CI. It is NOT labelled for auto-merge: a person reads the before/after
# coverage and merges it.
#
# ENVIRONMENT. GH_TOKEN (reads runs and downloads artifacts: the workflow's
# token in CI, your gh login by hand), APP_TOKEN (the merge App's token; not
# needed with --write-only), GH_REPO (owner/name; defaults to the checkout's),
# REPO_DIR (defaults to this checkout). No token is printed: each reaches gh
# and git through the environment alone, git through a credential helper.
#
# tests/unit/scripts/test_unit_weights_refresh.py holds the merge, this
# script's shape and the workflow's.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

BRANCH="chore/unit-weights-refresh"
BASE="main"
WEIGHTS_REL="tests/unit/scripts/unit_test_durations.json"
LOOKBACK="${UNIT_WEIGHTS_LOOKBACK:-20}"

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; }

MODE="pr"
RUN_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --write-only) MODE="write" ;;
    --run) [[ $# -ge 2 ]] || die "--run needs a run id"; RUN_ID="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
  shift
done
[[ -z "${RUN_ID}" || "${RUN_ID}" =~ ^[0-9]+$ ]] || die "--run takes a numeric run id, not ${RUN_ID}"
[[ "${LOOKBACK}" =~ ^[0-9]+$ && "${LOOKBACK}" -gt 0 ]] || die "UNIT_WEIGHTS_LOOKBACK must be a positive number"

require_cmd gh git python3
REPO_DIR="${REPO_DIR:-${REPO_ROOT}}"
if [[ "${MODE}" == "pr" ]]; then
  : "${APP_TOKEN:?APP_TOKEN (the merge App token) is required to push and open the pull request; by hand, use --write-only}"
fi

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

if [[ -z "${GH_REPO:-}" ]]; then
  (cd "${REPO_DIR}" && gh repo view --json nameWithOwner --jq .nameWithOwner) >"${work}/repo"
  GH_REPO="$(cat "${work}/repo")"
fi
[[ -n "${GH_REPO}" ]] || die "could not tell which repository this is; set GH_REPO=owner/name"
export GH_REPO

summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$*" >>"${GITHUB_STEP_SUMMARY}"
  fi
  info "$*"
}

# The number of unexpired unit-durations-* artifacts a run has. `< /dev/null`:
# gh must not read the run list the loop below is reading on stdin.
durations_artifacts() {
  gh api --paginate "repos/${GH_REPO}/actions/runs/$1/artifacts?per_page=100" \
    --jq '.artifacts[] | select(.expired == false and (.name | startswith("unit-durations-"))) | .name' \
    </dev/null >"${work}/artifacts"
  wc -l <"${work}/artifacts" | tr -d ' '
}

# ---------------------------------------------------------------------------
# Which run
# ---------------------------------------------------------------------------

step "find main's newest green push run with unit-durations artifacts"
if [[ -n "${RUN_ID}" ]]; then
  printf '%s\n' "${RUN_ID}" >"${work}/runs"
else
  gh run list --repo "${GH_REPO}" --workflow application.yml --branch main --event push --status success \
    --limit "${LOOKBACK}" --json databaseId --jq '.[].databaseId' >"${work}/runs"
fi
run=""
visited=0
while IFS= read -r id; do
  [[ -n "${id}" ]] || continue
  visited=$((visited + 1))
  found="$(durations_artifacts "${id}")"
  if [[ "${found}" -gt 0 ]]; then
    run="${id}"
    info "run ${id}: ${found} unit-durations artifact(s)"
    break
  fi
  dim "run ${id}: no unit-durations artifact (python-unit skipped, or expired)"
done <"${work}/runs"
[[ -n "${run}" ]] || die "none of the ${visited} green push run(s) of application.yml on main looked at has an unexpired unit-durations-* artifact"
info "looked at ${visited} run(s); using ${run}"

gh api "repos/${GH_REPO}/actions/runs/${run}" --jq '.head_sha' </dev/null >"${work}/sha"
run_sha="$(cat "${work}/sha")"
run_url="https://github.com/${GH_REPO}/actions/runs/${run}"

# ---------------------------------------------------------------------------
# Download and merge
# ---------------------------------------------------------------------------

step "download and merge run ${run}'s measured durations"
gh run download "${run}" --repo "${GH_REPO}" -p 'unit-durations-*' -D "${work}/measured"
python3 "${SWARM_LIB_DIR}/merge_unit_weights.py" --root "${REPO_DIR}" \
  --stored "${REPO_DIR}/${WEIGHTS_REL}" --measured "${work}/measured" >"${work}/report"

report_value() { sed -n "s/^$1=//p" "${work}/report"; }
artifacts="$(report_value artifacts)"
measured="$(report_value measured)"
dropped="$(report_value dropped)"
test_files="$(report_value test_files)"
before="$(report_value weighted_before)"
after="$(report_value weighted_after)"
percent() { printf '%s' "$((100 * $1 / test_files))"; }

# Printed whatever happens next, so an empty merge is visible (CLAUDE.md, rule zero).
summary "Unit shard weights: merged ${measured} measured file(s) from ${artifacts} unit-durations artifact(s) of run ${run} (${run_sha:0:12}); dropped ${dropped}; weighted ${before} -> ${after} of ${test_files} test files ($(percent "${before}")% -> $(percent "${after}")%)"

if git -C "${REPO_DIR}" diff --quiet -- "${WEIGHTS_REL}"; then
  summary "Unit shard weights: ${WEIGHTS_REL} is unchanged; nothing to open"
  exit 0
fi
if [[ "${MODE}" == "write" ]]; then
  ok "rewrote ${WEIGHTS_REL}; commit it and open a pull request"
  exit 0
fi

# ---------------------------------------------------------------------------
# Commit, push, open or update the pull request
# ---------------------------------------------------------------------------

gh_app() { GH_TOKEN="${APP_TOKEN}" gh "$@"; }

git_app() {
  # A credential helper that reads APP_TOKEN from the environment when git
  # asks, so the token is in no argument, no URL and no remote.
  # shellcheck disable=SC2016
  git -C "${REPO_DIR}" -c credential.helper= \
    -c 'credential.helper=!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=${APP_TOKEN}"; }; f' \
    "$@"
}

remote="https://github.com/${GH_REPO}.git"
title="The unit shard weights cover ${after} of ${test_files} test files again, refreshed from main's green run ${run}"

step "commit to ${BRANCH}"
git -C "${REPO_DIR}" checkout -q -B "${BRANCH}"
git -C "${REPO_DIR}" add -- "${WEIGHTS_REL}"
git -C "${REPO_DIR}" -c user.name=unit-weights-refresh -c user.email=unit-weights-refresh@users.noreply.github.com \
  commit -q -m "${title}" -m "Merged from application.yml run ${run} (${run_url}) at ${run_sha} by scripts/refresh-unit-weights.sh (part of #2364)."

# The branch already carrying this exact tree is not pushed again: a push
# would re-run CI on the pull request for nothing.
same_tree=0
if git_app ls-remote --exit-code --heads "${remote}" "${BRANCH}" >/dev/null 2>&1; then
  git_app fetch -q --depth 1 "${remote}" "refs/heads/${BRANCH}"
  if [[ "$(git -C "${REPO_DIR}" rev-parse 'FETCH_HEAD^{tree}')" == "$(git -C "${REPO_DIR}" rev-parse 'HEAD^{tree}')" ]]; then
    same_tree=1
  fi
fi
if [[ "${same_tree}" -eq 1 ]]; then
  info "${BRANCH} already carries this tree; not pushed"
else
  git_app push -q --force "${remote}" "HEAD:refs/heads/${BRANCH}"
  ok "pushed ${BRANCH}"
fi

cat >"${work}/body.md" <<EOF
Refreshed by \`.github/workflows/unit-weights-refresh.yml\` (\`scripts/refresh-unit-weights.sh\`) from application.yml's green push run [${run}](${run_url}) on main, at \`${run_sha}\`.

| | test files with a weight |
|---|---|
| before | ${before} of ${test_files} ($(percent "${before}")%) |
| after | ${after} of ${test_files} ($(percent "${after}")%) |

- ${measured} file(s) measured across ${artifacts} \`unit-durations-*\` artifact(s) replace their stored weight;
- ${dropped} stored file(s) no longer under tests/unit are dropped;
- every other existing test file keeps its stored weight.

Stale weights only unbalance the unit shards (an unweighted file runs at the median weight, and \`unit_shards.py count\` warns below 80 %); they never drop a file. This pull request is not queued for auto-merge: a person reads the numbers above and merges it. Why it is automated: #2364, [docs/ci.md](docs/ci.md#refreshing-the-shard-weights).
EOF

step "open or update the pull request"
gh_app pr list --repo "${GH_REPO}" --head "${BRANCH}" --base "${BASE}" --state open \
  --json number --jq '.[0].number // empty' >"${work}/pr"
number="$(cat "${work}/pr")"
if [[ -n "${number}" ]]; then
  gh_app pr edit "${number}" --repo "${GH_REPO}" --title "${title}" --body-file "${work}/body.md" >/dev/null
  summary "Unit shard weights: updated pull request #${number}: ${title}"
else
  gh_app pr create --repo "${GH_REPO}" --base "${BASE}" --head "${BRANCH}" \
    --title "${title}" --body-file "${work}/body.md" >"${work}/url"
  summary "Unit shard weights: opened $(cat "${work}/url"): ${title}"
fi
