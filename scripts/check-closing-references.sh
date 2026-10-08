#!/usr/bin/env bash
# check-closing-references: refuse a pull request whose text would make GitHub
# close another OPEN pull request when it merges.
#
# WHY THIS EXISTS. On 2026-10-08 the text of the pull request merged as 857
# named the App-settings pull request, 840, with a closing keyword while 840
# was open and unmerged. Two seconds after the swarmcloud-merge App merged
# 857, GitHub's own keyword handling closed 840 (scripts/close-merged-issues.sh
# logged no references; it was not the cause). Owner decision, 2026-10-08:
# auto-merge.yml's enable job runs this BEFORE auto-merge is enabled, and
# refuses the pull request on its finding.
#
# WHAT IT READS. GraphQL `closingIssuesReferences` alone cannot see this: it
# is an IssueConnection and does not list a pull request at all -- the pull
# request merged as 817 says "fixes" before 777, a pull request, and its list
# is empty (read 2026-10-08). So the candidates are that list PLUS the closing
# keywords this script finds in the title, the body and every commit message
# (this repository's squash commit message is the commit messages, so a
# keyword in one lands on main and closes too). A keyword is close, closes,
# closed, fix, fixes, fixed, resolve, resolves or resolved, any case, an
# optional colon, then `#N`, `owner/name#N` or a github.com issues/pull URL.
# Only references into this repository count; the pull request's own number
# does not (merging closes it anyway). It reads more than GitHub acts on
# rather than less: a false refusal costs a reworded sentence, a miss costs a
# closed pull request.
#
# THE DECISION. For each candidate it asks REST `repos/<R>/issues/<n>`: an
# OPEN item with a `pull_request` field refuses. An issue passes (closing it is
# what the keyword is for); a closed or merged pull request passes (the
# keyword closes; it never reopens a closed one and cannot change a merged
# one); a number GitHub answers 404 for passes (nothing to close).
#
# EXIT. 0: nothing would be closed that must not be. 2: refused, and stdout is
# the comment to post. Anything else: could not tell -- a pull request number
# that is not one, an unreadable or erroring answer, a failed read other than
# a 404, more references or commits than one page. Empty is not success: the
# caller must treat every exit but 0 as "not merged".
#
# Usage:
#   scripts/check-closing-references.sh --pr <number> [--repo owner/name]
#
# Needs `gh` authenticated (GH_TOKEN) with pull-requests: read, and jq.
# --repo defaults to GH_REPO. A summary is appended to GITHUB_STEP_SUMMARY when
# it is set.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# One page each. A pull request with more references or commits than this
# fails closed rather than being judged on part of its text.
PAGE=100

QUERY="query(\$owner: String!, \$name: String!, \$number: Int!) {
  repository(owner: \$owner, name: \$name) {
    pullRequest(number: \$number) {
      number title body
      closingIssuesReferences(first: ${PAGE}) {
        totalCount
        nodes { number url repository { nameWithOwner } }
      }
      commits(first: ${PAGE}) {
        totalCount
        nodes { commit { message } }
      }
    }
  }
}"

usage() { sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'; }

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
  die "could not read #${pr}'s text and closing references from ${repo}"
fi

jq -e 'type == "object"' "${answer}" >/dev/null 2>&1 \
  || die "the answer for #${pr} is not JSON"
if jq -e '(.errors // []) | length > 0' "${answer}" >/dev/null; then
  die "GitHub answered #${pr} with an error: $(jq -r '[.errors[].message] | join("; ")' "${answer}")"
fi
jq -e '.data.repository.pullRequest | type == "object"' "${answer}" >/dev/null \
  || die "${repo} has no pull request #${pr} in the answer"
jq -e '.data.repository.pullRequest.closingIssuesReferences.nodes | type == "array"' "${answer}" >/dev/null \
  || die "the answer for #${pr} carries no closing-references list"
jq -e '.data.repository.pullRequest.commits.nodes | type == "array"' "${answer}" >/dev/null \
  || die "the answer for #${pr} carries no commit list"

references_total="$(jq -r '.data.repository.pullRequest.closingIssuesReferences.totalCount // 0' "${answer}")"
commits_total="$(jq -r '.data.repository.pullRequest.commits.totalCount // 0' "${answer}")"
[[ "${references_total}" =~ ^[0-9]+$ && "${commits_total}" =~ ^[0-9]+$ ]] \
  || die "the answer for #${pr} carries no readable counts"
if [[ "${references_total}" -gt "${PAGE}" ]]; then
  die "#${pr} has ${references_total} closing references and one page reads ${PAGE}; not judged on part of them"
fi
if [[ "${commits_total}" -gt "${PAGE}" ]]; then
  die "#${pr} has ${commits_total} commits and one page reads ${PAGE}; not judged on part of them"
fi

# Every number in this repository that GitHub lists or a closing keyword
# names, once each, never this pull request's own.
named="${work}/named.txt"
jq -r --arg repo "${repo}" --argjson self "${pr}" '
  .data.repository.pullRequest as $pr
  | ($repo | ascii_downcase) as $here
  | [ $pr.closingIssuesReferences.nodes[]
      | {where: (.repository.nameWithOwner // ""), n: .number} ]
    + ( [ $pr.title, $pr.body, ($pr.commits.nodes[] | .commit.message) ]
        | map(select(type == "string"))
        | map([ scan("(?i)(?<![A-Za-z0-9_])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\\s*(?:#([0-9]+)|([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)|https?://github\\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([0-9]+))(?![A-Za-z0-9_])") ])
        | add // []
        | map(if .[0] != null then {where: $repo, n: (.[0] | tonumber)}
              elif .[2] != null then {where: .[1], n: (.[2] | tonumber)}
              else {where: .[3], n: (.[4] | tonumber)} end) )
  | map(select((.n | type) == "number" and .n > 0 and .n != $self
               and ((.where | ascii_downcase) == $here)))
  | map(.n) | unique | .[]' "${answer}" >"${named}"

note "### Closing references of #${pr}"
note ""
visited=0
open_prs=()
while read -r number; do
  [[ -n "${number}" ]] || continue
  [[ "${number}" =~ ^[1-9][0-9]*$ ]] || die "not a number among the references: '${number}'"
  visited=$((visited + 1))
  item="${work}/item-${number}.json"
  item_err="${work}/item-${number}.err"
  if ! gh api "repos/${repo}/issues/${number}" >"${item}" 2>"${item_err}"; then
    if grep -q "HTTP 404" "${item_err}"; then
      info "#${number} does not exist in ${repo}: nothing to close"
      note "* #${number}: not found; nothing to close"
      continue
    fi
    cat "${item_err}" >&2
    die "could not read whether #${number} is an open pull request"
  fi
  jq -e --argjson n "${number}" '.number == $n and (.state | type) == "string"' "${item}" >/dev/null 2>&1 \
    || die "the answer for #${number} is not a readable issue or pull request"
  # Explicit comparisons: `false // x` is x in jq.
  if jq -e '(.pull_request | type) == "object" and .state == "open"' "${item}" >/dev/null; then
    err "#${number} is an OPEN pull request that a closing keyword names"
    note "* **#${number}: open pull request -- would be closed by this merge**"
    open_prs+=("${number}")
  elif jq -e '(.pull_request | type) == "object"' "${item}" >/dev/null; then
    info "#${number} is a pull request that is no longer open: a keyword cannot change it"
    note "* #${number}: pull request, $(jq -r .state "${item}"); left alone"
  else
    info "#${number} is an issue: closing it is what the keyword is for"
    note "* #${number}: issue"
  fi
done <"${named}"

info "read ${visited} closing reference(s) of #${pr}"
if [[ "${visited}" -eq 0 ]]; then
  note "#${pr} names nothing with a closing keyword."
fi

if [[ ${#open_prs[@]} -eq 0 ]]; then
  exit 0
fi

if [[ ${#open_prs[@]} -eq 1 ]]; then
  n="${open_prs[0]}"
  printf '%s\n' "**Not merged, and \`ready\` removed.** This pull request's text names open pull request #${n} with a closing keyword (for example \"fix #${n}\"), so GitHub would close #${n} when this merges. Reword it (for example \"PR ${n}\") or use \"part of #${n}\", then add \`ready\` again."
else
  listed=""
  for n in "${open_prs[@]}"; do
    listed="${listed:+${listed}, }#${n}"
  done
  n="${open_prs[0]}"
  printf '%s\n' "**Not merged, and \`ready\` removed.** This pull request's text names open pull requests ${listed} with a closing keyword (for example \"fix #${n}\"), so GitHub would close them when this merges. Reword each (for example \"PR ${n}\") or use \"part of #${n}\", then add \`ready\` again."
fi
exit 2
