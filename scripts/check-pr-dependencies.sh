#!/usr/bin/env bash
# check-pr-dependencies: refuse a pull request whose body says it depends on
# another pull request or issue that has not landed yet.
#
# WHY THIS EXISTS. Lanes run in parallel, and a lane's pull request is often
# written against another lane's that has not merged: without it, main breaks
# or the change is meaningless. Nothing held that order -- `ready` merged
# whichever went green first. Owner decision, 2026-10-08 (observer proposal I,
# part 2): auto-merge.yml's enable job runs this BEFORE auto-merge is enabled,
# and refuses the pull request, with a comment, while a dependency is open.
#
# THE PHRASE. In the pull request's BODY (not the title, not the commits):
#
#   depends on PR <N>     depends on PR #<N>     depends on #<N>
#   depends on <owner>/<repo>#<N>
#   depends on https://github.com/<owner>/<repo>/pull/<N>
#
# any case, any whitespace between the words, `PR` optional before the last
# two. A bare number is this repository's; `<owner>/<repo>#<N>` or a URL is
# read in that repository (#888 box 96: a bare #N always named this one, and
# a cross-repository dependency was silently ignored). The pull request's own
# number is ignored. Anything else ("see #N", "part of #N", "after #N") is
# not a dependency.
#
# NOT IN CODE OR A QUOTE (#888 box 95). A fenced block (``` or ~~~, to its
# closing fence or the end of the body, as GitHub renders it), an inline code
# span and a `>` quoted line are dropped before the scan: a pull request that
# explains this rule, or quotes another's body, is showing the phrase, not
# saying it.
#
# THE DECISION. For each number it asks REST `repos/<R>/issues/<n>`, <R> being
# the repository the line names:
#   * a pull request passes only when it is MERGED (`pull_request.merged_at`).
#     Open refuses; closed without merging refuses too -- what this one
#     depends on will never land, so the line must be removed or reworded;
#   * an issue passes only when it is CLOSED;
#   * a number GitHub answers 404 for refuses: the author named nothing, and
#     a dependency that cannot be read is not one that landed. In another
#     repository a private one reads as a 404 too, and refuses the same way.
#
# EXIT. 0: every dependency has landed (or none is named). 2: refused, and
# stdout is the sentence for the refusal comment. Anything else: could not tell
# -- a pull request number that is not one, an unreadable answer, a failed read
# other than a 404. The caller treats every exit but 0 and 2 as "not merged":
# fail closed.
#
# Usage:
#   scripts/check-pr-dependencies.sh --pr <number> [--repo owner/name]
#
# Needs `gh` authenticated (GH_TOKEN) with pull-requests: read, and jq.
# --repo defaults to GH_REPO. A summary is appended to GITHUB_STEP_SUMMARY when
# it is set.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# The phrase, as a jq (Oniguruma) pattern. tests/unit/scripts/
# test_check_pr_dependencies.py holds docs/ci.md's examples against it.
# Captures: 1 the repository of a URL, 2 the repository of `owner/repo#N`
# (neither for a bare number), 3 the number. A repository name is never only
# dots, so `..` cannot walk the API path.
REPO_NAME='[A-Za-z0-9-]+/[A-Za-z0-9_.-]*[A-Za-z0-9_-][A-Za-z0-9_.-]*'
DEPENDS_ON="(?i)(?<![A-Za-z0-9_])depends\\s+on\\s+(?:PR\\s*#?|#|(?:PR\\s+)?https?://github\\.com/(${REPO_NAME})/(?:pull|issues)/|(?:PR\\s+)?(${REPO_NAME})#)([0-9]+)(?![A-Za-z0-9_])"

# The header, to its first line that is not a comment: no line range to keep
# in step with it (#888 box 92 names the hand-kept form).
usage() { awk 'NR == 1 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"; }

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
answer="${work}/pull.json"

if ! gh api "repos/${repo}/pulls/${pr}" >"${answer}"; then
  die "could not read #${pr}'s body from ${repo}"
fi
# Slurped, so an EMPTY answer is an empty array and fails: jq 1.6's `-e` exits
# 0 when it reads no input at all, which would pass an empty body unread.
jq -se --argjson n "${pr}" 'length == 1 and (.[0] | type == "object" and .number == $n)' \
  "${answer}" >/dev/null 2>&1 || die "the answer for #${pr} is not that pull request"
# A pull request with no description has `"body": null`: nothing named. Any
# other non-string is an answer this cannot read.
jq -e '.body == null or (.body | type) == "string"' "${answer}" >/dev/null \
  || die "the answer for #${pr} carries no readable body"

# The body as prose: fenced blocks (to the closing fence, or the end of the
# body if none closes it, as GitHub renders it), `>` quoted lines and inline
# code spans removed. A closing fence is the opening one's character, at
# least as long, alone on its line (CommonMark).
#
# One `<owner>/<repo><TAB><n>` line per dependency. This repository named in
# full is this repository (GitHub's names are case-insensitive), so its own
# number is ignored however it is written.
named="${work}/named.txt"
jq -r --arg re "${DEPENDS_ON}" --argjson self "${pr}" --arg this "${repo}" '
  def prose:
    gsub("\r"; "")
    | split("\n")
    | reduce .[] as $line ({fence: null, kept: []};
        if .fence != null then
          ((($line | capture("^ {0,3}(?<f>`{3,}|~{3,})[ \t]*$")) // null) as $c
           | if $c != null and ($c.f[0:1] == .fence[0:1]) and ($c.f | length) >= (.fence | length)
             then .fence = null else . end)
        else
          ((($line | capture("^ {0,3}(?<f>`{3,}|~{3,})")) // null) as $o
           | if $o != null then .fence = $o.f
             elif ($line | test("^\\s*>")) then .
             else .kept += [$line] end)
        end)
    | .kept | join("\n")
    | gsub("(`+).*?\\1"; " ");
  [ (.body // "") | prose | scan($re)
    | { repo: (.[0] // .[1] // $this), n: (.[2] | tonumber) }
    | if (.repo | ascii_downcase) == ($this | ascii_downcase) then .repo = $this else . end ]
  | map(select(.n > 0 and (.repo != $this or .n != $self))) | unique
  | .[] | "\(.repo)\t\(.n)"' "${answer}" >"${named}"

note "### Dependencies of #${pr}"
note ""
visited=0
pending=()
reasons=()
while IFS=$'\t' read -r dep_repo number; do
  [[ -n "${dep_repo}${number}" ]] || continue
  [[ "${number}" =~ ^[1-9][0-9]*$ ]] || die "not a number among the dependencies: '${number}'"
  [[ "${dep_repo}" =~ ^${REPO_NAME}$ ]] || die "not a repository among the dependencies: '${dep_repo}'"
  visited=$((visited + 1))
  # `#N` in this repository, `owner/repo#N` in any other.
  ref="#${number}"
  where="this repository"
  if [[ "${dep_repo}" != "${repo}" ]]; then
    ref="${dep_repo}#${number}"
    where="${dep_repo}"
  fi
  item="${work}/item-${visited}.json"
  item_err="${work}/item-${visited}.err"
  if ! gh api "repos/${dep_repo}/issues/${number}" >"${item}" 2>"${item_err}"; then
    if grep -q "HTTP 404" "${item_err}"; then
      note "* **${ref}: not found**"
      pending+=("${ref}")
      reasons+=("${ref} does not exist in ${where}")
      continue
    fi
    cat "${item_err}" >&2
    die "could not read whether ${ref} has landed"
  fi
  jq -se --argjson n "${number}" 'length == 1 and (.[0] | .number == $n and (.state | type) == "string")' "${item}" >/dev/null 2>&1 \
    || die "the answer for ${ref} is not a readable issue or pull request"
  # Explicit comparisons throughout: `false // x` is x in jq.
  if jq -e '(.pull_request | type) == "object"' "${item}" >/dev/null; then
    if jq -e '(.pull_request.merged_at | type) == "string"' "${item}" >/dev/null; then
      note "* ${ref}: pull request, merged"
    elif jq -e '.state == "open"' "${item}" >/dev/null; then
      note "* **${ref}: pull request, open -- not merged yet**"
      pending+=("${ref}")
      reasons+=("${ref} is an open pull request that has not merged")
    else
      note "* **${ref}: pull request, closed without merging**"
      pending+=("${ref}")
      reasons+=("${ref} is a pull request that was closed without merging, so it will never land")
    fi
  elif jq -e '.state == "closed"' "${item}" >/dev/null; then
    note "* ${ref}: issue, closed"
  else
    note "* **${ref}: issue, open**"
    pending+=("${ref}")
    reasons+=("${ref} is an open issue")
  fi
done <"${named}"

info "read ${visited} dependenc(ies) of #${pr}"
if [[ "${visited}" -eq 0 ]]; then
  note "#${pr} names no dependency (\`depends on PR <N>\`, \`depends on #<N>\` or \`depends on <owner>/<repo>#<N>\`, outside code and quotes)."
fi

if [[ ${#pending[@]} -eq 0 ]]; then
  exit 0
fi

listed=""
for reason in "${reasons[@]}"; do
  listed="${listed:+${listed}; }${reason}"
done
printf '%s\n' "This pull request's body says it depends on something that has not landed: ${listed}. It merges only after each one named with \`depends on PR <N>\`, \`depends on #<N>\` or \`depends on <owner>/<repo>#<N>\` is merged (a pull request) or closed (an issue). Wait for that, or remove the line if it no longer holds."
exit 2
