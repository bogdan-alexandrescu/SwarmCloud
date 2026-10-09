#!/usr/bin/env bash
# Read the acceptance suite's pull requests back from the private sandbox, and
# fail when one is not what the platform should have opened.
#
# Usage: SWARM_ACCEPTANCE_GITHUB_TOKEN=... scripts/acceptance/github-verify.sh [--since ISO8601]
#
# WHY HERE, NOT IN THE SUITE (#628). The suite runs in the swarm-verify job,
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
#
# Nothing is written: this reads only. Pull requests are listed by the sweep
# that follows and closed there.
#
# FINDING NONE is not a failure -- a claude-code group whose provider
# credential is missing SKIPs without opening anything -- but it is never
# silent: the count of each kind is printed, and a GitHub Actions warning
# names a kind with none.

set -euo pipefail
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

failures=0
# fail_pr NUMBER WHY -> records a failure against one pull request.
fail_pr() {
  err "PR #$1: $2"
  failures=$(( failures + 1 ))
}

step "Acceptance pull requests on ${REPO}${SINCE:+ opened since ${SINCE}}"
gh_api "/repos/${REPO}/pulls?state=open&per_page=100" >"${WORK}/pulls.json" \
  || die "could not list open pull requests: $(redact <"${WORK}/pulls.json" | head -c 200)"

direct=0
integrate=0
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
  if jq -r --arg f "${CALC}" '.[] | select(.filename == $f) | .patch // ""' "${WORK}/files.json" \
      | grep -qxF -- "-${BUG_LINE}"; then
    ok "PR #${number}: the diff removes '${BUG_LINE# *}' from ${CALC}"
  else
    fail_pr "${number}" "its diff does not remove '${BUG_LINE# *}' from ${CALC}"
  fi

  if grep -qE '^Integrates [0-9]+ contributor branch' "${WORK}/body.txt"; then
    integrate=$(( integrate + 1 ))
    # shellcheck disable=SC2016  # the backticks and \1 are sed's, not the shell's
    merged="$(sed -nE 's/^- merged: `(swarm\/[A-Za-z0-9_.-]+)`.*$/\1/p' "${WORK}/body.txt" | tr '\n' ' ')"
    left_out="$(grep -E '^- (conflicted|missing): ' "${WORK}/body.txt" | tr '\n' ' ' || true)"
    if [[ -z "${merged}" ]]; then
      fail_pr "${number}" "an integrate pull request whose body lists no merged swarm/ branch"
    else
      ok "PR #${number}: integrates ${merged}"
    fi
    [[ -z "${left_out}" ]] || fail_pr "${number}" "the integration left branches out: ${left_out}"
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

info "verified ${visited} pull request(s): ${direct} direct-pr, ${integrate} integrate"
for kind in direct-pr integrate; do
  n="${direct}"
  [[ "${kind}" == "direct-pr" ]] || n="${integrate}"
  if [[ "${n}" -eq 0 ]]; then
    warn "no ${kind} pull request to read back: the suite opened none (see its SKIP or FAIL lines)"
    [[ -z "${GITHUB_ACTIONS:-}" ]] || printf '::warning::acceptance opened no %s pull request on the sandbox; nothing of it was read back\n' "${kind}"
  fi
done
[[ "${failures}" -eq 0 ]] || die "${failures} assertion(s) failed on the sandbox's acceptance pull requests"
ok "every acceptance pull request on ${REPO} is what the platform should have opened"
