#!/usr/bin/env bash
#
# Report one run of .github/workflows/accept.yml: the SHA it accepted in the
# run's summary, and -- when it went red -- ONE GitHub issue, labelled bug,
# naming that SHA and every failing group.
#
# WHY AN ISSUE (owner decision 2026-10-08, cut A of the release timing
# report; docs/ci.md, "The release timeline"). Acceptance used to be the last
# job of release.yml, so a red acceptance failed the release and held the
# `release-dev` lock while it ran. It now runs after the deploy, outside that
# lock, and blocks nothing; a red result that only lived on a run page would
# be a red result nobody reads. So it lands where findings live here: an
# issue (CLAUDE.md, "Issues").
#
# ONE ISSUE, NOT ONE PER RUN. Every push deploys and is accepted, and a
# defect that breaks acceptance breaks it on every run until it is fixed: an
# issue per run would be ten issues for one defect by lunchtime. The open
# issue carrying MARKER (in its body) is retitled to the latest red SHA and
# groups and gets a comment per red run; with none open, one is created in
# the bug form's sections. A green run changes nothing: closing is a
# person's call, made with the PR that fixed it (CLAUDE.md: `Closes #N` only
# when unconditionally true).
#
# NOT FILED WHEN THE DEPLOYMENT IS GONE. If dev runs another SHA by the time
# the report looks (ACCEPT_NOW_SHA, read again from applied.json), the hotfix
# lane applied a newer commit mid-run, and a red result judged a deployment
# that no longer exists. The summary says so; that commit's own acceptance
# reports for it.
#
# Inputs, from the workflow's environment:
#   ACCEPT_SHA      the SHA `what dev runs` read (empty if that job failed)
#   ACCEPT_NOW_SHA  what applied.json names now (empty if it could not be read)
#   ACCEPT_NEEDS    toJSON(needs) of the report job
#   GITHUB_REPOSITORY GITHUB_RUN_ID GITHUB_RUN_ATTEMPT GITHUB_SERVER_URL
#   GITHUB_STEP_SUMMARY (optional), GH_TOKEN (for gh)
#
# Exit 0 when the report was made (the run is already red through its failed
# jobs; this does not add a second failure). Exit 1 when a red result could
# not be filed: losing it silently is the one outcome this script exists to
# prevent.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_cmd gh jq

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
SERVER="${GITHUB_SERVER_URL:-https://github.com}"
RUN_URL="${SERVER}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
SHA="${ACCEPT_SHA:-}"
NOW_SHA="${ACCEPT_NOW_SHA:-}"
NEEDS="${ACCEPT_NEEDS:-}"
[[ -n "${NEEDS}" ]] || NEEDS='{}'

# The issue this script owns: found by this line in its body, never by title.
MARKER='<!-- swarm-accept: dev acceptance is red -->'

[[ -z "${SHA}" || "${SHA}" =~ ^[0-9a-f]{40}$ ]] || die "ACCEPT_SHA is not a commit SHA: '${SHA}'"
[[ -z "${NOW_SHA}" || "${NOW_SHA}" =~ ^[0-9a-f]{40}$ ]] || NOW_SHA=""
printf '%s' "${NEEDS}" | jq -e 'type == "object"' >/dev/null 2>&1 || die "ACCEPT_NEEDS is not a JSON object"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-accept-report.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# Red is decided from `needs`, which GitHub renders and nothing can fail to
# read. Every job this report needs, and how it ended.
failed_needs="$(printf '%s' "${NEEDS}" | jq -r '[to_entries[] | select(.value.result == "failure") | .key] | join(" ")')"

# The failing JOBS by name -- "acceptance (mock)" -- so a matrix says which
# group. From this run attempt's job list; the `needs` keys when that cannot
# be read (they name the wave, not the group, and the issue says so).
groups=""
if gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${ATTEMPT}/jobs?per_page=100" \
    --jq '.jobs[] | select(.conclusion == "failure") | .name' >"${WORK}/jobs" 2>"${WORK}/jobs.err"; then
  groups="$(sed '/^$/d' "${WORK}/jobs" | paste -sd ',' - | sed 's/,/, /g')"
else
  warn "could not read this run's job list; naming the failed jobs from needs instead:"
  redact <"${WORK}/jobs.err" | head -n 3 | sed 's/^/     /' >&2
fi
[[ -n "${groups}" ]] || groups="$(printf '%s' "${failed_needs}" | sed 's/ /, /g')"

short="${SHA:0:12}"
{
  if [[ -n "${SHA}" ]]; then
    echo "### Dev acceptance of \`${SHA}\`"
  else
    echo "### Dev acceptance: what dev runs could not be read"
  fi
  echo
  echo "| job | result |"
  echo "|---|---|"
  printf '%s' "${NEEDS}" | jq -r 'to_entries[] | "| \(.key) | \(.value.result) |"'
  echo
} >>"${SUMMARY}"

if [[ -z "${failed_needs}" ]]; then
  echo "Green: dev runs \`${SHA}\` and it passed acceptance." >>"${SUMMARY}"
  ok "acceptance of ${SHA} is green"
  exit 0
fi

if [[ -n "${SHA}" && -n "${NOW_SHA}" && "${NOW_SHA}" != "${SHA}" ]]; then
  echo "Red, but dev now runs \`${NOW_SHA}\`: a newer commit was applied while this ran, so this result judged a deployment that is gone and is not filed. That commit's acceptance reports for it." >>"${SUMMARY}"
  echo "::notice title=Superseded during acceptance::${SHA} failed ${groups}, but dev now runs ${NOW_SHA}; not filed."
  exit 0
fi

title="Dev acceptance failed on ${short:-an unknown commit}: ${groups}"
# A title is one line and GitHub caps it at 256 characters.
title="${title:0:250}"

# The run's own section: the body of a new issue's "Identifiers", and the
# comment on an open one.
{
  echo "**${title}**"
  echo
  if [[ -n "${SHA}" ]]; then
    echo "- Commit: \`${SHA}\`"
  else
    echo "- Commit: unknown -- \`what dev runs\` failed before it read releases/dev/applied.json"
  fi
  echo "- Failing: ${groups}"
  echo "- Run: ${RUN_URL} (attempt ${ATTEMPT})"
} >"${WORK}/section.md"

existing=""
if ! existing="$(gh issue list --repo "${GITHUB_REPOSITORY}" --label bug --state open --limit 200 \
    --json number,body --jq '[.[] | select(.body | contains("swarm-accept: dev acceptance is red"))][0].number // empty' 2>"${WORK}/list.err")"; then
  redact <"${WORK}/list.err" | head -n 3 | sed 's/^/     /' >&2
  die "could not list open bug issues, so the red acceptance of ${SHA:-an unknown commit} (${groups}) is NOT filed. Run: ${RUN_URL}"
fi

if [[ -n "${existing}" ]]; then
  [[ "${existing}" =~ ^[0-9]+$ ]] || die "gh named issue '${existing}', which is not a number"
  gh issue edit "${existing}" --repo "${GITHUB_REPOSITORY}" --title "${title}" >/dev/null \
    || die "could not retitle issue #${existing}"
  gh issue comment "${existing}" --repo "${GITHUB_REPOSITORY}" --body-file "${WORK}/section.md" >/dev/null \
    || die "could not comment on issue #${existing}"
  echo "Red: updated issue #${existing}." >>"${SUMMARY}"
  echo "::error title=Dev acceptance is red::${SHA:-unknown} failed ${groups}; issue #${existing} updated."
  exit 0
fi

# A new issue, in the bug form's sections and order (.github/ISSUE_TEMPLATE/
# bug_report.yml): gh issue create bypasses the form, so the body follows it.
{
  echo "${MARKER}"
  echo
  echo "### What happened"
  echo
  echo "Post-deploy acceptance of dev failed on \`${SHA:-an unknown commit}\`: ${groups}."
  echo
  echo "### Severity"
  echo
  echo "S2 — wrong data on screen or in the API (states, counts, capacity, costs, timings) — I can't trust what it says"
  echo
  echo "Filed by accept.yml, which cannot tell a broken feature from a broken check; triage sets the real severity."
  echo
  echo "### Where you saw it"
  echo
  echo "Release pipeline or CI (.github/workflows)"
  echo
  echo "### Environment"
  echo
  echo "dev"
  echo
  echo "### Steps to reproduce"
  echo
  echo "1. Open the run below and read the failing job's log; verify-remote.sh prints the swarm-verify execution's own transcript for a failed group."
  echo "2. Re-run it against what dev runs now: dispatch the \`accept\` workflow on main."
  echo
  echo "### What you expected instead"
  echo
  echo "Every acceptance group passes against the commit dev runs (docs/acceptance.md says what each check asserts and why)."
  echo
  echo "### Identifiers"
  echo
  cat "${WORK}/section.md"
  echo
  echo "### Screenshots, errors, logs"
  echo
  echo "In the run's failing jobs. Not copied here: a suite transcript is where a token turns up."
  echo
  echo "### Anything else"
  echo
  echo "Later red runs comment on this issue and retitle it, rather than opening another. A green run changes nothing here: close it with the change that fixed it."
} >"${WORK}/body.md"

if ! url="$(gh issue create --repo "${GITHUB_REPOSITORY}" --label bug --title "${title}" --body-file "${WORK}/body.md")"; then
  die "could not open an issue for the red acceptance of ${SHA:-an unknown commit} (${groups}). Run: ${RUN_URL}"
fi
echo "Red: opened ${url}." >>"${SUMMARY}"
echo "::error title=Dev acceptance is red::${SHA:-unknown} failed ${groups}; opened ${url}."
