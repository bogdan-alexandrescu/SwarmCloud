#!/usr/bin/env bash
# The frozen contract is unchanged by this pull request, or its change is
# recorded as accepted by the owner.
#
# apps/common/swarm_common is frozen by CONTRACT.md. Every other component was
# written against those exact types, so a change there breaks all of them at
# once and must be a deliberate, discussed act. CLAUDE.md rule 1 sends every
# contract change through docs/contract-change-requests.md, so a diff under
# swarm_common passes only when the pull request ADDS a line there containing
# "accepted by the owner". The line is printed, so the reviewer merging the PR
# reads the claim the change rests on.
#
# WHAT IS COMPARED: the merge commit's FIRST PARENT against the merge commit.
# A pull_request run checks out `Merge <head> into <main now>`, which is
# `git merge <head>` run on main as it stands. Its first parent is that main
# and its second is the PR head, so first-parent..HEAD is the PR's change as it
# would merge: nothing more, nothing less.
#
# NOT `github.event.pull_request.base.sha`. That is main when the PR was
# opened, and GitHub does not move it when main moves or when the PR gets new
# pushes. Until 2026-09-24 this step diffed base.sha..HEAD, so it covered
# everything that had landed on main since the PR was opened, plus the PR.
# Measured on PR #44's own run 36039462416 (python job 107767715382): the
# checkout was "Merge 9c56167 into d45b1d69" and the step compared it against
# 0fac6d6. Once #44 landed, a PR opened before it that edited swarm_common
# without acceptance would have passed on #44's acceptance lines, and every
# other open PR would have been told it changed profiles.py and states.py.
# --pr-base is still taken, only so the log can say when it is stale.
#
# FAILS CLOSED. Anything that is not the PR's merge commit (a single-parent
# HEAD, a merge of some other head), a first parent that cannot be fetched, and
# a failed diff each stop the step. Until 2026-09-24 a failed diff passed it:
# it was a three-dot diff in a depth-1 checkout, git answered "no merge base",
# and the step printed "frozen contract untouched" on every PR (run
# 36036034784, job 107756241218).
#
# Tested against a real repository built the way GitHub builds that checkout:
# tests/unit/scripts/test_frozen_contract_check.py.
#
# Usage: scripts/lib/check-frozen-contract.sh --pr-head SHA [--pr-base SHA] [--repo DIR]
#   --pr-head  github.event.pull_request.head.sha: must be the merge's second parent
#   --pr-base  github.event.pull_request.base.sha: reported, never compared against
#   --repo     the checkout to inspect (default: this repository)

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

FROZEN_DIR="apps/common/swarm_common"
REQUESTS="docs/contract-change-requests.md"
MARK="accepted by the owner"

REPO="${REPO_ROOT}"
PR_BASE=""
PR_HEAD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr-base|--pr-head|--repo)
      [[ $# -ge 2 ]] || die "$1 needs a value"
      case "$1" in
        --pr-base) PR_BASE="$2" ;;
        --pr-head) PR_HEAD="$2" ;;
        --repo)    REPO="$2" ;;
      esac
      shift 2
      ;;
    -h|--help) sed -n '2,43p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "${PR_HEAD}" ]] || die "--pr-head is required"

g() { git -C "${REPO}" "$@"; }

refuse() {
  local line
  for line in "$@"; do
    printf '::error::%s\n' "${line}"
  done
  exit 1
}

head="$(g rev-parse --verify 'HEAD^{commit}')"

# The parents as the commit object records them. Not `HEAD^1`: in a depth-1
# checkout git treats HEAD as having no parents (it is a shallow boundary), but
# the object still names them, and a name is all the fetch below needs. Header
# lines only: the headers end at the first blank line, and a message line that
# happens to start with "parent " is not one.
raw="$(g cat-file commit "${head}")"
count=0
first=""
second=""
while IFS= read -r line; do
  [[ -n "${line}" ]] || break
  case "${line}" in
    "parent "*)
      count=$((count + 1))
      if [[ "${count}" -eq 1 ]]; then
        first="${line#parent }"
      elif [[ "${count}" -eq 2 ]]; then
        second="${line#parent }"
      fi
      ;;
  esac
done <<<"${raw}"

if [[ "${count}" -ne 2 ]]; then
  refuse "HEAD ${head} has ${count} parent(s), so it is not a pull request's merge commit." \
         "A pull_request run checks out 'Merge <head> into <main>', whose first parent is main as it stands." \
         "Without it there is nothing that separates this PR's change from anything else, so nothing is passed."
fi
if [[ "${second}" != "${PR_HEAD}" ]]; then
  refuse "HEAD ${head} merges ${second}, but this pull request's head is ${PR_HEAD}." \
         "The checkout is not this PR merged into main, so its diff is not this PR's change."
fi

echo "comparing ${first} (main, as this run merged into it)"
echo "     with ${head} (that plus pull request head ${second})"
if [[ -n "${PR_BASE}" && "${PR_BASE}" != "${first}" ]]; then
  echo "the event's base.sha is ${PR_BASE}, main when the PR was opened. main has moved since, and"
  echo "base.sha is not compared against: a diff from it would include everything main gained since."
fi

if ! g cat-file -e "${first}^{commit}" 2>/dev/null; then
  echo "fetching ${first}: a depth-1 checkout does not include the merge commit's parents"
  g fetch --no-tags --depth=1 origin "${first}" \
    || refuse "could not fetch ${first}, the merge commit's first parent, so nothing can be compared."
fi

changed="$(g diff --name-only "${first}" "${head}" -- "${FROZEN_DIR}")"
if [[ -z "${changed}" ]]; then
  echo "frozen contract untouched"
  exit 0
fi
echo "changed under ${FROZEN_DIR}/ by this pull request:"
printf '%s\n' "${changed}" | sed 's/^/  /'

# The diff is taken first and searched second, so a failed diff stops the step
# here instead of reading as "no acceptance line" -- or, before, as nothing.
requests_diff="$(g diff "${first}" "${head}" -- "${REQUESTS}")"
accepted="$(printf '%s\n' "${requests_diff}" | grep -E '^\+[^+]' | grep -iF "${MARK}" || true)"
if [[ -z "${accepted}" ]]; then
  echo "::error::${FROZEN_DIR}/ is FROZEN (see CONTRACT.md)."
  echo "A change there needs the owner's acceptance, recorded in THIS PR as an added line in"
  echo "${REQUESTS} containing '${MARK}', next to the request."
  g diff --stat "${first}" "${head}" -- "${FROZEN_DIR}"
  exit 1
fi
echo "the change rests on this acceptance, which this pull request records:"
printf '%s\n' "${accepted}"
