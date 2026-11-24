#!/usr/bin/env bash
# Read the acceptance suite's pull requests back from the private sandbox, and
# fail when one is not what the platform should have opened.
#
# Usage: SWARM_ACCEPTANCE_GITHUB_TOKEN=... scripts/acceptance/github-verify.sh [--since ISO8601]
#
# WHY HERE, NOT IN THE SUITE (archived issue 628). The suite runs in the swarm-verify job,
# which holds no GitHub credential on purpose (CLAUDE.md: a forge token lives
# only in Secret Manager for the worker). The sandbox is private, so the suite
# cannot read a pull request back and SKIPs those assertions, naming this
# script. accept.yml's read-back job holds the sandbox's own token (the
# repository secret SWARM_SANDBOX_GITHUB_TOKEN) on the GitHub runner, so it
# runs this after the suite and BEFORE github-cleanup.sh closes anything.
# Without it, every release would go green with the pull-request half of the
# direct-pr and integrate checks unexercised.
#
# WHICH PULL REQUESTS: every OPEN one on config.sh's ACC_GITHUB_REPO whose
# head is a `swarm/task_...` branch of the sandbox itself, created at or after
# --since (SWARM_ACCEPTANCE_SINCE; the release passes the time the suite
# started, so a pull request an earlier, unswept run left is not judged
# again). The task id is the head branch without `swarm/`: only the platform
# names branches that way.
#
# WHAT EACH MUST BE -- the assertions the suite made before the sandbox went
# private:
#
#   * its title is non-empty and carries no task id (the agent's
#     pr-title.txt, a fact-style title -- never the task id);
#   * its body carries the task id;
#   * every file it changes lies under tests/acceptance/fixtures/, and its
#     diff removes the fixture's bug line from calc.py;
#   * a direct-pr pull request (no "Integrates" block) changes calc.py and
#     nothing else;
#   * an integrate pull request lists at least one `- merged:` swarm/ branch
#     and no `- conflicted:` or `- missing:` one.
#   * the git-tools rebase lane's pull request (its body carries the marker
#     tests/acceptance/test_runner_git_tools.py's `pr_kind` reads) instead:
#     its head contains main (rebased, not merged around), it changes only
#     planted.txt and lane.txt under tests/acceptance/fixtures/git-tools/,
#     and planted.txt at its head is the resolution the lane was asked for
#     (`expected-planted`) -- the half of the rebase check only GitHub sees.
#   * the git-tools build step's integrator's pull request (`pr_kind`'s
#     git-tools-integrate: its body's marker, or an "Integrates" block over
#     files all under tests/acceptance/fixtures/git-tools/) instead: it
#     changes only that group's build.txt and its integrate block is whole --
#     never the calc.py assertion, which is the claude-code group's. That it
#     exists at all is a warning: the suite's push_fails check FAILs it.
#   * the workflow group's review-and-fix loop's pull request (its body
#     carries FIX_LOOP_MARKER, or it changes FIX_LOOP_FILE alone) instead:
#     it changes only FIX_LOOP_FILE, and its diff appends lines reading `fix`
#     and removes none -- what the loop's fix step and fix rounds are told to
#     write, and nothing else. How many lines is the suite's to judge (a loop
#     the workspace's quota parked leaves one); never the calc.py assertion.
#
# WHICH RULES: a pull request is judged by the check that opened it, in this
# order -- git-tools' (the judge's pr-kind), the fix loop's, then the
# claude-code group's (direct-pr and integrate, the workflow group's
# integrate chain included) when it changes a file under that group's
# fixture directory. Anything else FAILS as unclassified, naming its files:
# it is never judged by another check's rules. On 2026-11-23 the fix loop's
# pull request fell through to the claude-code rules and failed calc.py's.
#
# Nothing is written: this reads only. Pull requests are listed by the sweep
# that follows and closed there.
#
# FINDING NONE is not a failure -- a claude-code group whose provider
# credential is missing SKIPs without opening anything -- but it is never
# silent: the count of each kind is printed, and a GitHub Actions warning
# names a kind with none.

set -euo pipefail

# This script touches GitHub only, never the project: it says so, so sourcing
# common.sh does not load the never-touch list. The sandbox jobs that run it are
# self-hosted with no swarm-accept login, and a load there could only refuse.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=config.sh
source "${REPO_ROOT}/scripts/acceptance/config.sh"
acc_config_problem || die "refusing to verify: see above"

SINCE="${SWARM_ACCEPTANCE_SINCE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) [[ $# -ge 2 ]] || die "--since needs a value"; SINCE="$2"; shift 2 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
# ISO 8601 UTC, the shape GitHub's created_at has, so the two compare as
# strings. Empty means every open pull request.
[[ -z "${SINCE}" || "${SINCE}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
  || die "--since must be UTC ISO 8601 (YYYY-MM-DDTHH:MM:SSZ), got '${SINCE}'"

require_cmd jq curl
TOKEN="${SWARM_ACCEPTANCE_GITHUB_TOKEN:-}"
[[ -n "${TOKEN}" ]] || die "no GitHub token for ${ACC_GITHUB_REPO}: set SWARM_ACCEPTANCE_GITHUB_TOKEN (the release passes the SWARM_SANDBOX_GITHUB_TOKEN secret)"
REPO="${ACC_GITHUB_REPO}"
API="${ACC_GITHUB_API}"
FIXTURES="tests/acceptance/fixtures/"
# The claude-code fixture and its one deliberate bug: the same two values
# groups/claude-code.sh asserts on (CC_FIXTURE, CC_BUG_LINE).
CALC="tests/acceptance/fixtures/claude-code/calc.py"
BUG_LINE="    return a - b"
# The git-tools group's judge: which pull request is its rebase lane's, and
# what that lane's planted.txt must read. Written once, there.
GIT_TOOLS_JUDGE="${REPO_ROOT}/tests/acceptance/test_runner_git_tools.py"
GIT_TOOLS_DIR="tests/acceptance/fixtures/git-tools"
# What the git-tools build step commits, the one file its integrator's pull
# request may change (the judge's BUILD_FILE; a unit test holds them equal).
GIT_TOOLS_BUILD="${GIT_TOOLS_DIR}/build.txt"
# The claude-code group's fixture directory: a pull request that changes a
# file under it is that group's (or the workflow integrate chain's, which
# edits the same calc.py and is judged the same way).
CC_DIR="tests/acceptance/fixtures/claude-code/"
# The workflow group's fix-loop check: the one file its fix step and fix
# rounds append to, and the line its fix step writes into the pull request's
# body (groups/workflow.sh's WF_FIX_LOOP_FILE and WF_FIX_LOOP_MARKER; a unit
# test holds them equal).
FIX_LOOP_FILE="tests/acceptance/fixtures/workflow/fix-loop-rounds.txt"
FIX_LOOP_MARKER="acceptance-workflow: fix-loop"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-acc-verify.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# gh_api PATH -> body on stdout; non-zero on a non-2xx. GET only. The token
# reaches curl on stdin (-K -, common.sh's auth_config), never in argv.
gh_api() {
  local path="$1" out="${WORK}/resp" code
  code="$(auth_config "${TOKEN}" \
    | curl -K - -sS -m 30 -o "${out}" -w '%{http_code}' \
        -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
        "${API}${path}")" || return 1
  cat "${out}"
  [[ "${code}" =~ ^2 ]]
}

# gh_raw PATH OUTFILE -> a file's bytes through the contents API; non-zero on a non-2xx.
gh_raw() {
  local path="$1" out="$2" code
  code="$(auth_config "${TOKEN}" \
    | curl -K - -sS -m 30 -o "${out}" -w '%{http_code}' \
        -H "Accept: application/vnd.github.raw" -H "X-GitHub-Api-Version: 2022-11-28" \
        "${API}${path}")" || return 1
  [[ "${code}" =~ ^2 ]]
}

failures=0
# fail_pr NUMBER WHY -> records a failure against one pull request.
fail_pr() {
  err "PR #$1: $2"
  failures=$(( failures + 1 ))
}

step "Acceptance pull requests on ${REPO}${SINCE:+ opened since ${SINCE}}"
gh_api "/repos/${REPO}/pulls?state=open&per_page=100" >"${WORK}/pulls.json" \
  || die "could not list open pull requests: $(redact <"${WORK}/pulls.json" | head -c 200)"

# verify_git_tools_pr NUMBER HEAD -> the git-tools rebase lane's pull request,
# after its lane rebased it onto ACC_REF and resolved the planted conflict.
verify_git_tools_pr() {
  local number="$1" head="$2" behind files want
  if ! gh_api "/repos/${REPO}/compare/${ACC_REF}...${head}" >"${WORK}/compare.json"; then
    fail_pr "${number}" "could not compare ${head} with ${ACC_REF}"
    return 0
  fi
  behind="$(jq -r '.behind_by // "unknown"' "${WORK}/compare.json")"
  if [[ "${behind}" == "0" ]]; then
    ok "PR #${number}: its head contains ${ACC_REF} (the lane rebased it)"
  else
    fail_pr "${number}" "its head is ${behind} commit(s) behind ${ACC_REF}: the rebase lane did not land its rebase"
  fi
  files="$(jq -r '[.files[]?.filename] | sort | join(" ")' "${WORK}/compare.json")"
  want="${GIT_TOOLS_DIR}/lane.txt ${GIT_TOOLS_DIR}/planted.txt"
  if [[ "${files}" == "${want}" ]]; then
    ok "PR #${number}: against ${ACC_REF} it changes only ${want}"
  else
    fail_pr "${number}" "against ${ACC_REF} it must change exactly ${want}; it changes: ${files:-nothing}"
  fi
  python3 "${GIT_TOOLS_JUDGE}" expected-planted >"${WORK}/planted.want"
  if ! gh_raw "/repos/${REPO}/contents/${GIT_TOOLS_DIR}/planted.txt?ref=$(jq -r '.head.sha // ""' "${WORK}/pr.json")" "${WORK}/planted.got"; then
    fail_pr "${number}" "could not read ${GIT_TOOLS_DIR}/planted.txt at its head"
  elif cmp -s "${WORK}/planted.want" "${WORK}/planted.got"; then
    ok "PR #${number}: planted.txt at its head is the resolution: main's line, then the lane's"
  else
    fail_pr "${number}" "planted.txt at its head is not the resolution the lane was asked for: $(head -c 200 "${WORK}/planted.got" | redact | tr '\n' '|')"
  fi
}

# verify_integrate_block NUMBER -> an integrate pull request's body lists at
# least one merged swarm/ branch and leaves none out.
verify_integrate_block() {
  local number="$1" merged left_out
  # shellcheck disable=SC2016  # the backticks and \1 are sed's, not the shell's
  merged="$(sed -nE 's/^- merged: `(swarm\/[A-Za-z0-9_.-]+)`.*$/\1/p' "${WORK}/body.txt" | tr '\n' ' ')"
  left_out="$(grep -E '^- (conflicted|missing): ' "${WORK}/body.txt" | tr '\n' ' ' || true)"
  if [[ -z "${merged}" ]]; then
    fail_pr "${number}" "an integrate pull request whose body lists no merged swarm/ branch"
  else
    ok "PR #${number}: integrates ${merged}"
  fi
  [[ -z "${left_out}" ]] || fail_pr "${number}" "the integration left branches out: ${left_out}"
}

# verify_git_tools_integrate_pr NUMBER -> the git-tools build step's
# integrator's pull request (pr_kind's git-tools-integrate). It is judged by
# what that group's build step writes -- build.txt and nothing else -- never by
# the claude-code fixture's calc.py. It exists only when the build step wrongly
# SUCCEEDED (its final push is made to fail and on_step_failure stops the
# integrator), which the suite's push_fails check FAILs: here that is a
# warning naming the check, not a second failure of the same defect.
verify_git_tools_integrate_pr() {
  local number="$1" files
  files="$(jq -r '[.[].filename] | sort | join(" ")' "${WORK}/files.json")"
  if [[ "${files}" == "${GIT_TOOLS_BUILD}" ]]; then
    ok "PR #${number}: the git-tools integrator's pull request changes only ${GIT_TOOLS_BUILD}"
  else
    fail_pr "${number}" "the git-tools integrator's pull request must change only ${GIT_TOOLS_BUILD}; it changes: ${files:-nothing}"
  fi
  verify_integrate_block "${number}"
  warn "PR #${number}: the git-tools build step's integrator opened a pull request, so the build step's push did not fail as planted (the suite's push_fails check reports it)"
  [[ -z "${GITHUB_ACTIONS:-}" ]] || printf '::warning::PR #%s: the git-tools build step ended SUCCEEDED, so its integrator ran; see the suite'"'"'s push_fails check\n' "${number}"
}

# verify_fix_loop_pr NUMBER -> the workflow group's fix-loop pull request:
# FIX_LOOP_FILE alone, every added line reading `fix`, no line removed.
verify_fix_loop_pr() {
  local number="$1" files patch added bad
  files="$(jq -r '[.[].filename] | sort | join(" ")' "${WORK}/files.json")"
  if [[ "${files}" == "${FIX_LOOP_FILE}" ]]; then
    ok "PR #${number}: the fix-loop pull request changes only ${FIX_LOOP_FILE}"
  else
    fail_pr "${number}" "the fix-loop pull request must change only ${FIX_LOOP_FILE}; it changes: ${files:-nothing}"
    return 0
  fi
  patch="$(jq -r --arg f "${FIX_LOOP_FILE}" '.[] | select(.filename == $f) | .patch // ""' "${WORK}/files.json")"
  added="$(grep -c '^+' <<<"${patch}" || true)"
  bad="$(grep -E '^-|^\+' <<<"${patch}" | grep -vxF '+fix' | head -3 | redact | tr '\n' '|' || true)"
  if [[ -n "${bad}" ]]; then
    fail_pr "${number}" "the fix-loop pull request's diff must only append lines reading fix to ${FIX_LOOP_FILE}; it has: ${bad}"
  elif [[ "${added}" -eq 0 ]]; then
    fail_pr "${number}" "the fix-loop pull request's diff appends nothing to ${FIX_LOOP_FILE}"
  else
    ok "PR #${number}: the fix-loop pull request appends ${added} line(s) reading fix"
  fi
}

direct=0
integrate=0
fix_loop=0
unclassified=0
git_tools=0
git_tools_integrate=0
visited=0
while IFS=$'\t' read -r number head; do
  [[ -n "${number}" ]] || continue
  visited=$(( visited + 1 ))
  task="${head#swarm/}"
  jq --argjson n "${number}" '.[] | select(.number == $n)' "${WORK}/pulls.json" >"${WORK}/pr.json"
  title="$(jq -r '.title // ""' "${WORK}/pr.json")"
  jq -r '.body // ""' "${WORK}/pr.json" >"${WORK}/body.txt"

  if [[ -z "${title//[[:space:]]/}" ]]; then
    fail_pr "${number}" "its title is empty"
  elif grep -qE 'task_[0-9a-f]{8,}' <<<"${title}"; then
    fail_pr "${number}" "its title carries a task id, not the agent's pr-title.txt: ${title}"
  else
    ok "PR #${number}: the title names the work: ${title}"
  fi
  if grep -qF "${task}" "${WORK}/body.txt"; then
    ok "PR #${number}: the body carries ${task}"
  else
    fail_pr "${number}" "its body does not carry ${task}"
  fi

  if ! gh_api "/repos/${REPO}/pulls/${number}/files?per_page=100" >"${WORK}/files.json"; then
    fail_pr "${number}" "could not read its files"
    continue
  fi
  outside="$(jq -r --arg p "${FIXTURES}" '[.[].filename | select(startswith($p) | not)] | join(" ")' "${WORK}/files.json")"
  if [[ -n "${outside}" ]]; then
    fail_pr "${number}" "it changes files outside ${FIXTURES}: ${outside}"
  fi
  jq -r '.[].filename' "${WORK}/files.json" >"${WORK}/files.txt"
  kind="$(python3 "${GIT_TOOLS_JUDGE}" pr-kind --body-file "${WORK}/body.txt" --files-file "${WORK}/files.txt")"
  if [[ "${kind}" == "git-tools" ]]; then
    git_tools=$(( git_tools + 1 ))
    verify_git_tools_pr "${number}" "${head}"
    continue
  fi
  if [[ "${kind}" == "git-tools-integrate" ]]; then
    git_tools_integrate=$(( git_tools_integrate + 1 ))
    verify_git_tools_integrate_pr "${number}"
    continue
  fi
  if grep -qxF -- "${FIX_LOOP_MARKER}" "${WORK}/body.txt" \
      || [[ "$(jq -r '[.[].filename] | join(" ")' "${WORK}/files.json")" == "${FIX_LOOP_FILE}" ]]; then
    fix_loop=$(( fix_loop + 1 ))
    verify_fix_loop_pr "${number}"
    continue
  fi
  if ! jq -e --arg d "${CC_DIR}" 'any(.[]; .filename | startswith($d))' "${WORK}/files.json" >/dev/null; then
    unclassified=$(( unclassified + 1 ))
    files="$(jq -r '[.[].filename] | join(" ")' "${WORK}/files.json")"
    fail_pr "${number}" "unclassified: no acceptance check owns this pull request (no check's marker in its body, no file under ${CC_DIR}), so no check's rules were applied; it changes: ${files:-nothing}"
    continue
  fi
  if jq -r --arg f "${CALC}" '.[] | select(.filename == $f) | .patch // ""' "${WORK}/files.json" \
      | grep -qxF -- "-${BUG_LINE}"; then
    ok "PR #${number}: the diff removes '${BUG_LINE# *}' from ${CALC}"
  else
    fail_pr "${number}" "its diff does not remove '${BUG_LINE# *}' from ${CALC}"
  fi

  if grep -qE '^Integrates [0-9]+ contributor branch' "${WORK}/body.txt"; then
    integrate=$(( integrate + 1 ))
    verify_integrate_block "${number}"
  else
    direct=$(( direct + 1 ))
    files="$(jq -r '[.[].filename] | join(" ")' "${WORK}/files.json")"
    if [[ "${files}" == "${CALC}" ]]; then
      ok "PR #${number}: changes only ${CALC}"
    else
      fail_pr "${number}" "a direct-pr pull request must change only ${CALC}; it changes: ${files:-nothing}"
    fi
  fi
done < <(jq -r --arg since "${SINCE}" '
  .[]
  | select(.head.ref | test("^swarm/task_"))
  | select(.head.repo.full_name == .base.repo.full_name)
  | select($since == "" or (.created_at // "") >= $since)
  | [.number, .head.ref] | @tsv' "${WORK}/pulls.json")

info "verified ${visited} pull request(s): ${direct} direct-pr, ${integrate} integrate, ${git_tools} git-tools, ${git_tools_integrate} git-tools integrate, ${fix_loop} fix-loop, ${unclassified} unclassified"
for kind in direct-pr integrate git-tools fix-loop; do
  case "${kind}" in
    direct-pr) n="${direct}" ;;
    integrate) n="${integrate}" ;;
    fix-loop) n="${fix_loop}" ;;
    *) n="${git_tools}" ;;
  esac
  if [[ "${n}" -eq 0 ]]; then
    warn "no ${kind} pull request to read back: the suite opened none (see its SKIP or FAIL lines)"
    [[ -z "${GITHUB_ACTIONS:-}" ]] || printf '::warning::acceptance opened no %s pull request on the sandbox; nothing of it was read back\n' "${kind}"
  fi
done
[[ "${failures}" -eq 0 ]] || die "${failures} assertion(s) failed on the sandbox's acceptance pull requests"
ok "every acceptance pull request on ${REPO} is what the platform should have opened"
