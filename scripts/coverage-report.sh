#!/usr/bin/env bash
#
# Report one nightly run of .github/workflows/coverage.yml (issue 64; docs/ci.md
# "Coverage floors"): each package's measured line coverage and floor in the
# run's summary, every night, and ONE GitHub issue, labelled bug, while a
# package is under its floor.
#
# WHY AN ISSUE. Coverage is measured once a day, off the merge path (owner
# decision 2026-11-23): nothing on a pull request or the merge train waits for
# it, so a red night blocks no merge, and a red result that lived only on a run
# page would be read by nobody. It lands where findings live here: an issue
# (CLAUDE.md, "Issues").
#
# ONE ISSUE, NOT ONE PER NIGHT. The open issue carrying MARKER in its body is
# retitled to the latest red night and gets a comment per red night; with none
# open, one is created in the bug form's sections. Each names the packages
# under their floor and the commits merged since the last green run of this
# workflow (`git log` between the two SHAs), where the drop came in. The next
# GREEN night comments on it and closes it: unlike acceptance, the check that
# opened it is the whole definition of fixed.
#
# The verdict, from the `judge` job (COVERAGE_VERDICT):
#   green       every floored package at or above its floor: close the issue
#   unset       no floor set yet: report, print the `fill` command, file nothing
#   below       a package BELOW its floor or MISSING: open or update the issue
#   incomplete  (or empty) no whole measurement: report it, file nothing, close
#               nothing -- a failed shard is not fallen coverage
#
# Inputs, from the workflow's environment:
#   COVERAGE_VERDICT  green | unset | below | incomplete (empty = incomplete)
#   COVERAGE_REPORTS  the `coverage-reports` artifact: verdict.txt (the
#                     coverage-floors.sh output), python.json, ui.json
#   COVERAGE_SHA      the commit measured
#   COVERAGE_WORKFLOW this workflow's file name (default coverage.yml)
#   COVERAGE_FLOORS   the floors file (default coverage-floors.json)
#   COVERAGE_GIT_DIR  the checkout `git log` reads (default the repository)
#   GITHUB_REPOSITORY GITHUB_RUN_ID GITHUB_SERVER_URL GITHUB_STEP_SUMMARY
#   GH_TOKEN (for gh); a checkout with full history (for git log)
#
# Exit 0 when the report was made (a red night already fails the run through
# the `judge` job). Exit 1 when a red night could not be filed or a green one
# could not close its issue: losing either silently is what this prevents.
set -euo pipefail

# This script touches no project: it reads GitHub and local files. It says
# so, so common.sh does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
: "${COVERAGE_REPORTS:?COVERAGE_REPORTS is required}"
SERVER="${GITHUB_SERVER_URL:-https://github.com}"
RUN_URL="${SERVER}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
SHA="${COVERAGE_SHA:-}"
WORKFLOW="${COVERAGE_WORKFLOW:-coverage.yml}"
VERDICT="${COVERAGE_VERDICT:-}"
FLOORS="${COVERAGE_FLOORS:-${REPO_ROOT}/coverage-floors.json}"
GIT_DIR_READ="${COVERAGE_GIT_DIR:-${REPO_ROOT}}"
LIB="${SWARM_LIB_DIR}/coverage_floors.py"
LINES="${COVERAGE_REPORTS}/verdict.txt"
# The issue this script owns: found by this line in its body, never by title.
MARKER='<!-- swarm-coverage: nightly line coverage is under a floor -->'
# At most this many commits are listed; the rest are counted.
MAX_COMMITS=100

[[ "${SHA}" =~ ^[0-9a-f]{40}$ ]] || die "COVERAGE_SHA is not a commit SHA: '${SHA}'"
case "${VERDICT}" in
  green | unset | below | incomplete) ;;
  "") VERDICT=incomplete ;;
  *) die "COVERAGE_VERDICT must be green, unset, below or incomplete, got '${VERDICT}'" ;;
esac
# A below verdict is only as good as the lines that name the packages.
if [[ "${VERDICT}" == "below" && ! -s "${LINES}" ]]; then
  VERDICT=incomplete
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-coverage-report.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# --- the summary, every night -------------------------------------------------
reports=()
for report in "${COVERAGE_REPORTS}/python.json" "${COVERAGE_REPORTS}/ui.json"; do
  if [[ -f "${report}" ]]; then
    reports+=(--report "${report}")
  fi
done

{
  echo "### Line coverage per package"
  echo
  echo "Commit \`${SHA}\`, run ${RUN_URL}: **${VERDICT}**."
  echo
  if [[ "${#reports[@]}" -gt 0 ]] && python3 "${LIB}" table --floors "${FLOORS}" ${reports[@]+"${reports[@]}"} >"${WORK}/table.md" 2>"${WORK}/table.err"; then
    cat "${WORK}/table.md"
  else
    echo "No per-package report: the measurement did not complete (see the \`coverage floors\` job's log)."
    redact <"${WORK}/table.err" 2>/dev/null | head -n 3 || true
  fi
  if [[ -s "${LINES}" ]]; then
    echo
    echo '```'
    redact <"${LINES}"
    echo '```'
  fi
} >"${WORK}/summary.md"
cat "${WORK}/summary.md" >>"${SUMMARY}"
cat "${WORK}/summary.md"

# A null floor or a package the file does not list: the command that sets it
# from THIS run's reports, ready to paste.
if [[ "${VERDICT}" != "incomplete" ]] && { [[ "${VERDICT}" == "unset" ]] || grep -qE '^(UNMEASURED|NO FLOOR)  ' "${LINES}" 2>/dev/null; }; then
  fill_reports=""
  [[ -f "${COVERAGE_REPORTS}/python.json" ]] && fill_reports="${fill_reports} --report \"\$TMPDIR/coverage-reports/python.json\""
  [[ -f "${COVERAGE_REPORTS}/ui.json" ]] && fill_reports="${fill_reports} --report \"\$TMPDIR/coverage-reports/ui.json\""
  {
    echo
    echo "#### Floors to set"
    echo
    echo "A package here has no floor yet. Set each from this run's measurement (less one point, rounded down), in a pull request:"
    echo
    echo '```bash'
    echo "gh run download ${GITHUB_RUN_ID} --repo ${GITHUB_REPOSITORY} -n coverage-reports -D \"\$TMPDIR/coverage-reports\""
    echo "python3 scripts/lib/coverage_floors.py fill --floors coverage-floors.json \\"
    echo "  --about \"Nightly coverage run ${RUN_URL}, $(date -u +%Y-%m-%d): whole-suite unit and component tests, line coverage\"${fill_reports}"
    echo '```'
  } >"${WORK}/fill.md"
  cat "${WORK}/fill.md" >>"${SUMMARY}"
  cat "${WORK}/fill.md"
fi

# --- the one issue --------------------------------------------------------------
if [[ "${VERDICT}" == "incomplete" ]]; then
  echo "Not a measurement (a shard's data is missing, or the floors file is malformed): no issue is opened, updated or closed." | tee -a "${SUMMARY}"
  exit 0
fi

existing=""
if ! existing="$(gh issue list --repo "${GITHUB_REPOSITORY}" --label bug --state open --limit 200 \
    --json number,body --jq "[.[] | select(.body | contains(\"${MARKER}\"))][0].number // empty" 2>"${WORK}/list.err")"; then
  redact <"${WORK}/list.err" | head -n 3 | sed 's/^/     /' >&2
  die "could not list open bug issues, so this night's coverage (${VERDICT}) is NOT filed or closed. Run: ${RUN_URL}"
fi
if [[ -n "${existing}" && ! "${existing}" =~ ^[0-9]+$ ]]; then
  die "gh named issue '${existing}', which is not a number"
fi

if [[ "${VERDICT}" == "unset" ]]; then
  echo "No floor is set yet: nothing is filed." | tee -a "${SUMMARY}"
  exit 0
fi

if [[ "${VERDICT}" == "green" ]]; then
  if [[ -z "${existing}" ]]; then
    echo "Green: every package at or above its floor; no issue open." | tee -a "${SUMMARY}"
    exit 0
  fi
  {
    echo "Green: every package with a floor is at or above it at \`${SHA}\`."
    echo
    echo "- Run: ${RUN_URL}"
    echo
    echo "Closed by the nightly coverage run (scripts/coverage-report.sh)."
  } >"${WORK}/green.md"
  gh issue comment "${existing}" --repo "${GITHUB_REPOSITORY}" --body-file "${WORK}/green.md" >/dev/null \
    || die "could not comment on issue #${existing}"
  gh issue close "${existing}" --repo "${GITHUB_REPOSITORY}" --reason completed >/dev/null \
    || die "could not close issue #${existing}"
  echo "Green: closed issue #${existing}." | tee -a "${SUMMARY}"
  exit 0
fi

# --- below: which packages, and what was merged since the last green night ----
grep -E '^(BELOW|MISSING)  ' "${LINES}" | redact >"${WORK}/below.txt" || true
count="$(grep -c . "${WORK}/below.txt" || true)"
[[ "${count}" -gt 0 ]] || die "verdict below, but ${LINES} names no BELOW or MISSING package"
if [[ "${count}" -eq 1 ]]; then
  package="$(sed -E 's/^(BELOW|MISSING)  ([^:]+):.*/\2/' "${WORK}/below.txt")"
  title="${package} line coverage is under its floor on main"
else
  title="${count} packages' line coverage is under their floors on main"
fi
title="${title:0:250}"

# The last run of this workflow on main that ended green. Every red night
# fails its run, so a successful run is a night at or above every floor (or
# one before any floor was set). This run is still in progress, never counted.
last_sha=""
last_url=""
if gh run list --repo "${GITHUB_REPOSITORY}" --workflow "${WORKFLOW}" --branch main --status success --limit 1 \
    --json headSha,url --jq '.[0] // empty | "\(.headSha) \(.url)"' >"${WORK}/last" 2>"${WORK}/last.err"; then
  read -r last_sha last_url <"${WORK}/last" || true
else
  redact <"${WORK}/last.err" | head -n 3 | sed 's/^/     /' >&2
fi
{
  if [[ -z "${last_sha}" ]]; then
    echo "No earlier green run of ${WORKFLOW} on main was found, so the commits are not listed."
  elif ! [[ "${last_sha}" =~ ^[0-9a-f]{40}$ ]] || ! git -C "${GIT_DIR_READ}" cat-file -e "${last_sha}^{commit}" 2>/dev/null; then
    echo "The last green run (${last_url}) measured \`${last_sha}\`, which this checkout does not have, so the commits are not listed."
  else
    total="$(git -C "${GIT_DIR_READ}" rev-list --count "${last_sha}..${SHA}")"
    echo "Merged since the last green run (${last_url}, \`${last_sha:0:12}\`): ${total} commit(s)."
    echo
    git -C "${GIT_DIR_READ}" log --format='- %h %s' "${last_sha}..${SHA}" | head -n "${MAX_COMMITS}" | redact
    if [[ "${total}" -gt "${MAX_COMMITS}" ]]; then
      echo "- ... and $((total - MAX_COMMITS)) more: \`git log ${last_sha:0:12}..${SHA:0:12}\`"
    fi
  fi
} >"${WORK}/commits.md"

# The night's own section: the "Identifiers" of a new issue, and the comment
# on an open one.
{
  echo "**${title}**"
  echo
  echo "- Commit: \`${SHA}\`"
  echo "- Run: ${RUN_URL}"
  echo
  echo "Under the floor in coverage-floors.json:"
  echo
  echo '```'
  cat "${WORK}/below.txt"
  echo '```'
  echo
  cat "${WORK}/commits.md"
} >"${WORK}/section.md"

if [[ -n "${existing}" ]]; then
  gh issue edit "${existing}" --repo "${GITHUB_REPOSITORY}" --title "${title}" >/dev/null \
    || die "could not retitle issue #${existing}"
  gh issue comment "${existing}" --repo "${GITHUB_REPOSITORY}" --body-file "${WORK}/section.md" >/dev/null \
    || die "could not comment on issue #${existing}"
  echo "Red: updated issue #${existing}." | tee -a "${SUMMARY}"
  echo "::error title=Coverage under a floor::${title}; issue #${existing} updated."
  exit 0
fi

# A new issue, in the bug form's sections and order (.github/ISSUE_TEMPLATE/
# bug_report.yml): gh issue create bypasses the form, so the body follows it.
{
  echo "${MARKER}"
  echo
  echo "### What happened"
  echo
  echo "The nightly coverage run (.github/workflows/coverage.yml) measured \`${SHA}\` on main, and ${count} package(s) fell under the line-coverage floor coverage-floors.json holds for them: their tests were deleted or weakened, or code was added that no test runs."
  echo
  echo "### Severity"
  echo
  echo "S3 — broken, but I found a workaround"
  echo
  echo "Filed by coverage.yml, which blocks no merge; triage sets the real severity."
  echo
  echo "### Where you saw it"
  echo
  echo "Release pipeline or CI (.github/workflows)"
  echo
  echo "### Environment"
  echo
  echo "CI — a workflow run"
  echo
  echo "### Steps to reproduce"
  echo
  echo "1. Open the run below: its summary has every package's measured coverage and floor."
  echo "2. Read the commits listed below for the change that removed tests or added untested code."
  echo "3. Re-measure after a fix: dispatch the \`coverage\` workflow on main."
  echo
  echo "### What you expected instead"
  echo
  echo "Every package's line coverage at or above its floor in coverage-floors.json (docs/ci.md, \"Coverage floors\")."
  echo
  echo "### Identifiers"
  echo
  cat "${WORK}/section.md"
  echo
  echo "### Screenshots, errors, logs"
  echo
  echo "In the run's \`coverage floors\` job and its \`coverage-reports\` artifact."
  echo
  echo "### Anything else"
  echo
  echo "Later red nights comment on this issue and retitle it, rather than opening another. The next green night comments and closes it. Add the missing tests; or, if coverage fell on purpose, lower the floor in coverage-floors.json and say why in the commit."
} >"${WORK}/body.md"

if ! url="$(gh issue create --repo "${GITHUB_REPOSITORY}" --label bug --title "${title}" --body-file "${WORK}/body.md")"; then
  die "could not open an issue for coverage under its floor at ${SHA}. Run: ${RUN_URL}"
fi
echo "Red: opened ${url}." | tee -a "${SUMMARY}"
echo "::error title=Coverage under a floor::${title}; opened ${url}."
