#!/usr/bin/env bash
#
# refresh-weights-pr: the scheduled test-weights refresh. It runs
# scripts/refresh-unit-weights.sh and scripts/refresh-ui-weights.sh against
# main's CI runs and opens what they changed as ONE pull request from a fixed
# maintenance branch. It never pushes the default branch.
#
#   scripts/refresh-weights-pr.sh refresh
#       Run both refresh scripts in this checkout. Prints a diffstat of the
#       two weights files and, under GitHub Actions, writes `changed=true` or
#       `changed=false` to $GITHUB_OUTPUT. Exit 1 when either refresh could
#       not read its run (its own message says why); whatever the other one
#       changed is still left in the checkout, and still `changed=true`.
#
#   scripts/refresh-weights-pr.sh open
#       Commit the two weights files (nothing else) on top of this checkout's
#       HEAD and force-push that one commit to refs/heads/maint/test-weights
#       with the merge App's token (APP_TOKEN), then open the pull request
#       into main, or say which open one the push updated. GH_REPO names the
#       repository.
#
# WHY (CI scanner gates, the epic's weights item). Both weights files were
# refreshed only when someone remembered to run the scripts, and their own
# headers say "review the diff and commit it". docs/ci.md (the unit weights
# section) proposed this job: run them on a schedule and open the pull request
# with the merge App's token, because a pull request opened with the
# workflow's GITHUB_TOKEN starts no workflow and so could never go green.
#
# ONE BRANCH, FORCE-PUSHED. Every run rebuilds the branch from main's tip, so
# an open refresh pull request is updated in place (never a pile of them) and
# always carries one commit: the latest figures on the latest main. The branch
# is a constant below; `open` refuses any other, and the refspec names it in
# full, so no run can push main. It is not a `swarm/` branch on purpose: a
# lane's pull request runs no checks (docs/ci.md), and this one's must.
#
# NOT `ready`. The pull request is opened for a person to review, as the
# refresh scripts ask; nothing here labels it (questions.json on the pull
# request that added this holds the owner's choice).
#
# NOTHING SECRET IS PRINTED. The token is handed to git by a credential helper
# that reads APP_TOKEN from the environment when git asks, so it is in no
# argument, URL or remote (scripts/merge-train.sh's git_app_push), and gh's
# error output goes through `redact`.
#
#   exit 0  done
#   exit 1  a refresh could not read its run, or the push or the pull request failed
#   exit 2  usage
set -euo pipefail

# This script touches no project: it reads GitHub's CI runs and writes this
# checkout. It says so, so common.sh (no default project) does not stop on an
# unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

BRANCH="maint/test-weights"
BASE="main"
WEIGHTS_FILES=(tests/unit/scripts/unit_test_durations.json tests/unit/scripts/ui_test_durations.json)
TITLE="Test weights refreshed from main's CI timings"

usage() { sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }

changed() { ! git -C "${REPO_ROOT}" diff --quiet -- "${WEIGHTS_FILES[@]}"; }

refresh() {
  local status=0 name
  for name in refresh-unit-weights.sh refresh-ui-weights.sh; do
    info "running scripts/${name}"
    if ! "${REPO_ROOT}/scripts/${name}"; then
      err "scripts/${name} could not refresh its weights; the other file is still refreshed"
      status=1
    fi
  done
  local answer=false
  if changed; then
    answer=true
    git -C "${REPO_ROOT}" diff --stat -- "${WEIGHTS_FILES[@]}" >&2
  else
    info "the weights match main's latest CI runs: nothing to open"
  fi
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf 'changed=%s\n' "${answer}" >>"${GITHUB_OUTPUT}"
  fi
  return "${status}"
}

open_pr() {
  [[ -n "${APP_TOKEN:-}" ]] || die "APP_TOKEN is not set: the push and the pull request need the merge App's token"
  [[ "${GH_REPO:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "GH_REPO must be owner/name"
  # The one branch this pushes; never the default branch (constant above).
  [[ "${BRANCH}" != "${BASE}" && "${BRANCH}" == maint/* ]] || die "refusing to push ${BRANCH}"
  changed || { info "nothing changed in the weights files: no push, no pull request"; return 0; }
  require_cmd gh jq

  local work
  work="$(mktemp -d "${TMPDIR:-/tmp}/swarm-weights-pr.XXXXXX")"
  # shellcheck disable=SC2064  # expand now: ${work} is local
  trap "rm -rf '${work}'" RETURN

  # The weights files alone, never anything else the run left in the tree.
  git -C "${REPO_ROOT}" add -- "${WEIGHTS_FILES[@]}"
  git -C "${REPO_ROOT}" -c user.name=test-weights-refresh \
    -c user.email=test-weights-refresh@users.noreply.github.com \
    -c commit.gpgsign=false -c core.hooksPath=/dev/null \
    commit -q -m "${TITLE}" -m "From scripts/refresh-unit-weights.sh and scripts/refresh-ui-weights.sh, run by .github/workflows/refresh-weights.yml." \
    -- "${WEIGHTS_FILES[@]}"

  info "pushing one commit to ${BRANCH} (never ${BASE})"
  # shellcheck disable=SC2016  # the helper expands APP_TOKEN when git runs it
  if ! git -C "${REPO_ROOT}" -c credential.helper= \
      -c 'credential.helper=!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=${APP_TOKEN}"; }; f' \
      push -q --force "https://github.com/${GH_REPO}.git" "HEAD:refs/heads/${BRANCH}" 2>"${work}/push.err"; then
    redact <"${work}/push.err" >&2
    die "could not push ${BRANCH}"
  fi

  if ! GH_TOKEN="${APP_TOKEN}" gh pr list --repo "${GH_REPO}" --head "${BRANCH}" --base "${BASE}" \
      --state open --json number,url >"${work}/open.json" 2>"${work}/gh.err"; then
    redact <"${work}/gh.err" >&2
    die "could not list the open pull requests from ${BRANCH}"
  fi
  if [[ "$(jq 'length' "${work}/open.json")" -gt 0 ]]; then
    ok "updated the open pull request $(jq -r '.[0].url' "${work}/open.json")"
    return 0
  fi

  {
    cat <<'BODY'
The scheduled test-weights refresh (`.github/workflows/refresh-weights.yml`) ran
`scripts/refresh-unit-weights.sh` and `scripts/refresh-ui-weights.sh` against
main's latest green CI runs. What changed:

```
BODY
    git -C "${REPO_ROOT}" diff --stat HEAD~1 HEAD -- "${WEIGHTS_FILES[@]}"
    cat <<'BODY'
```

Review the figures, as the scripts ask. A later run force-pushes this branch
with fresher ones, one commit on main's tip, and updates this pull request.
BODY
  } >"${work}/body.md"
  if ! GH_TOKEN="${APP_TOKEN}" gh pr create --repo "${GH_REPO}" --base "${BASE}" --head "${BRANCH}" \
      --title "${TITLE}" --body-file "${work}/body.md" >"${work}/url" 2>"${work}/gh.err"; then
    redact <"${work}/gh.err" >&2
    die "could not open the pull request from ${BRANCH}"
  fi
  ok "opened $(cat "${work}/url")"
}

case "${1:-}" in
  refresh) [[ $# -eq 1 ]] || usage; refresh ;;
  open) [[ $# -eq 1 ]] || usage; open_pr ;;
  *) usage ;;
esac
