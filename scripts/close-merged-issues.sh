#!/usr/bin/env bash
# close-merged-issues: close every still-open issue a merged pull request's
# closing keywords name, with a comment naming the pull request (#621).
#
# WHY THIS EXISTS. GitHub parses a pull request's closing keywords whoever
# merges it -- its GraphQL `closingIssuesReferences` lists them -- but it did
# not act on them for the merges the swarmcloud-merge App made: of 33 issues
# named by App-merged pull requests since 2026-10-01, 28 stayed open, while
# owner merges closed 77 of 77 (history analysis, 2026-10-05). Owner decision,
# 2026-10-05: close them explicitly after the merge, the way the workflow
# `merge` step does (#581), and change nothing about the App's permissions.
# auto-merge.yml's `close-issues` job runs this on every pull request merged
# into the default branch; docs/ci.md says why it is not limited to the App.
#
# WHAT IT CLOSES. Exactly GitHub's own closing references -- the issues a
# closing keyword (`Closes #N`, `Fixes #N`, `Resolves #N`, ...) or a manual
# Development link names -- that are OPEN and in this repository. It never
# reads the pull request's text itself, so `part of #N`, which GitHub does not
# list, is never closed. An issue already closed is left alone (no second
# comment); one in another repository is recorded and left alone, because the
# job's token reaches this repository only.
#
# WHAT IT REFUSES, non-zero and closing nothing: a pull request number that is
# not a positive integer, an answer that is not a readable pull request (empty
# is not success: an error must not read as "nothing to close"), and a pull
# request that is not merged. A merge into a branch other than the default one
# closes nothing and succeeds: GitHub's keywords act only on the default
# branch. A refused close does not stop the others; the run fails at the end
# naming it, as it does when there are more references than one page.
#
# Usage:
#   scripts/close-merged-issues.sh --pr <number> [--repo owner/name]
#
# Needs `gh` authenticated (GH_TOKEN) with issues: write and pull-requests:
# read, and jq. --repo defaults to GH_REPO. A summary is appended to
# GITHUB_STEP_SUMMARY when it is set.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# One page. GitHub links far fewer than this to one pull request; more than a
# page fails the run after closing the page, rather than closing part quietly.
PAGE=100

QUERY="query(\$owner: String!, \$name: String!, \$number: Int!) {
  repository(owner: \$owner, name: \$name) {
    defaultBranchRef { name }
    pullRequest(number: \$number) {
      number merged baseRefName
      closingIssuesReferences(first: ${PAGE}) {
        totalCount
        nodes { number state repository { nameWithOwner } }
      }
    }
  }
}"

usage() { sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; }

pr=""
repo="${GH_REPO:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr) [[ $# -ge 2 ]] || die "--pr needs a value"; pr="$2"; shift 2 ;;
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ "${pr}" =~ ^[1-9][0-9]*$ ]] || die "--pr must be a pull request number, got '${pr}'"
[[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
  || die "the repository must be owner/name (--repo or GH_REPO), got '${repo}'"
require_cmd gh
require_cmd jq

summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
note() { printf '%s\n' "$*" >>"${summary}"; }

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
answer="${work}/answer.json"

if ! gh api graphql -f query="${QUERY}" -f owner="${repo%%/*}" -f name="${repo#*/}" \
    -F number="${pr}" >"${answer}"; then
  die "could not read #${pr}'s closing references from ${repo}"
fi

jq -e 'type == "object"' "${answer}" >/dev/null 2>&1 \
  || die "the closing-references answer for #${pr} is not JSON"
if jq -e '(.errors // []) | length > 0' "${answer}" >/dev/null; then
  die "GitHub answered #${pr}'s closing references with an error: $(jq -r '[.errors[].message] | join("; ")' "${answer}")"
fi
jq -e '.data.repository.pullRequest | type == "object"' "${answer}" >/dev/null \
  || die "${repo} has no pull request #${pr} in the answer"
jq -e '.data.repository.pullRequest.closingIssuesReferences.nodes | type == "array"' "${answer}" >/dev/null \
  || die "the answer for #${pr} carries no closing-references list"
jq -e '.data.repository.pullRequest.merged == true' "${answer}" >/dev/null \
  || die "#${pr} is not merged: its closing keywords close nothing"

base="$(jq -r '.data.repository.pullRequest.baseRefName // ""' "${answer}")"
default="$(jq -r '.data.repository.defaultBranchRef.name // ""' "${answer}")"
note "### Issues closed by #${pr}"
note ""
if [[ -z "${default}" || "${base}" != "${default}" ]]; then
  info "#${pr} merged into '${base}', not the default branch '${default}': closing keywords act only on the default branch"
  note "#${pr} merged into \`${base}\`, not the default branch \`${default}\`; nothing closed."
  exit 0
fi

# One line per reference: `close N`, `already N` or `elsewhere owner/name#N`.
decisions="${work}/decisions.txt"
jq -r --arg repo "${repo}" '
  .data.repository.pullRequest.closingIssuesReferences.nodes[]
  | select((.number | type) == "number" and .number > 0)
  | (.repository.nameWithOwner // "") as $where
  | if ($where | ascii_downcase) != ($repo | ascii_downcase) then "elsewhere \($where)#\(.number)"
    elif (.state // "") == "OPEN" then "close \(.number)"
    else "already \(.number)"
    end' "${answer}" >"${decisions}"

comment="Closed by #${pr}, merged into ${default}: its closing keyword names this issue. (auto-merge.yml closes these after a merge, because GitHub did not for the merge App's merges, #621.)"
visited=0
failed=()
while read -r verdict ref; do
  visited=$((visited + 1))
  case "${verdict}" in
    close)
      if gh issue close "${ref}" --repo "${repo}" --reason completed --comment "${comment}"; then
        ok "closed #${ref}"
        note "* closed #${ref}"
      else
        err "could not close #${ref}"
        note "* **could not close #${ref}**"
        failed+=("#${ref}")
      fi
      ;;
    already)
      info "#${ref} is already closed"
      note "* #${ref} already closed; left alone"
      ;;
    elsewhere)
      info "${ref} is in another repository; left alone"
      note "* ${ref} is in another repository; left alone"
      ;;
  esac
done <"${decisions}"

if [[ "${visited}" -eq 0 ]]; then
  info "#${pr} has no closing references"
  note "#${pr} has no closing references (a \`part of #N\` is not one); nothing closed."
fi

total="$(jq -r '.data.repository.pullRequest.closingIssuesReferences.totalCount // 0' "${answer}")"
listed="$(jq -r '.data.repository.pullRequest.closingIssuesReferences.nodes | length' "${answer}")"
status=0
if [[ "${total}" -gt "${listed}" ]]; then
  err "#${pr} has ${total} closing references and one page lists ${listed}; the rest were not closed"
  note "**#${pr} has ${total} closing references; only the first ${listed} were read.**"
  status=1
fi
if [[ ${#failed[@]} -gt 0 ]]; then
  err "not closed: ${failed[*]}"
  status=1
fi
info "read ${visited} closing reference(s) of #${pr}"
exit "${status}"
