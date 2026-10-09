#!/usr/bin/env bash
# Put this commit's acceptance fixtures, and the fixture issue, on the private
# sandbox repository the acceptance suite's tasks clone.
#
# Usage: SWARM_ACCEPTANCE_GITHUB_TOKEN=... scripts/acceptance/sandbox-sync.sh
#
# WHY (#628). Acceptance used to clone this platform's own public repository,
# where tests/acceptance/fixtures/ already was. It now clones the private
# sandbox (scripts/acceptance/config.sh), which holds nothing of this
# repository unless something puts it there. So accept.yml, in a job of its own,
# runs this BEFORE the suite, with the sandbox's own token (the repository
# secret SWARM_SANDBOX_GITHUB_TOKEN), and the fixtures a release's tasks clone
# are the ones of the commit it deployed -- the same property cloning `main`
# here used to give.
#
# WHAT IT WRITES, and nothing else:
#
#   * tests/acceptance/fixtures/ on the sandbox's ACC_REF (main), replaced
#     wholesale by this checkout's copy, in one commit, pushed only when it
#     differs. No other path of the sandbox is read or written. An EMPTY
#     sandbox gets ACC_REF as its first commit.
#   * the fixture issue the claude-code `issue` check reads: #ACC_ISSUE must
#     carry ACC_ISSUE_EXPECT. A sandbox with no issue and no pull request at
#     all gets it opened -- as #1, the first number GitHub hands out, which is
#     config.sh's default. Any other mismatch is refused, naming what to
#     change: a renumbered issue would make that check read the wrong text.
#
# THE TOKEN never reaches argv, a URL or a log: git asks for it through
# GIT_ASKPASS, which reads it from the environment, and curl reads its header
# on stdin (-K -). Error text is passed through `redact`.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=config.sh
source "${REPO_ROOT}/scripts/acceptance/config.sh"
acc_config_problem || die "refusing to sync: see above"

case "${1:-}" in
  "") ;;
  -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
  *) die "unknown argument: $1" ;;
esac

require_cmd git jq curl
TOKEN="${SWARM_ACCEPTANCE_GITHUB_TOKEN:-}"
[[ -n "${TOKEN}" ]] || die "no token for ${ACC_GITHUB_REPO}: set SWARM_ACCEPTANCE_GITHUB_TOKEN (the release passes the SWARM_SANDBOX_GITHUB_TOKEN secret)"
FIXTURES="tests/acceptance/fixtures"
[[ -d "${REPO_ROOT}/${FIXTURES}" ]] || die "no ${FIXTURES}/ in this checkout to sync"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-acc-sync.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# gh_api METHOD PATH [BODY] -> body on stdout, HTTP status in ${WORK}/status;
# non-zero on a non-2xx. The status goes to a file, not a variable: callers
# redirect this function's output, and a status set in a $(...) subshell
# would be lost (CLAUDE.md).
gh_api() {
  local method="$1" path="$2" body="${3:-}" out="${WORK}/resp" code
  local args=(-sS -m 30 -o "${out}" -w '%{http_code}' -X "${method}"
    -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")
  [[ -z "${body}" ]] || args+=(-H "Content-Type: application/json" --data-binary "${body}")
  code="$(auth_config "${TOKEN}" | curl -K - "${args[@]}" "${ACC_GITHUB_API}${path}")" || code="000"
  printf '%s' "${code}" >"${WORK}/status"
  cat "${out}" 2>/dev/null || true
  [[ "${code}" =~ ^2 ]]
}

step "Acceptance sandbox: ${ACC_GITHUB_REPO} @ ${ACC_REF}"

# --- 1. The fixture issue -----------------------------------------------------
issue_title="${ACC_ISSUE_EXPECT} is the acceptance suite's fixture issue"
issue_body="$(printf '%s\n\n%s\n' \
  "The script this issue is about is ${ACC_ISSUE_EXPECT}. The claude-code acceptance check asks an agent which file this issue names, with this issue as its input, and expects that name: nothing else in this repository says it." \
  "Opened by scripts/acceptance/sandbox-sync.sh. Leave it open and do not edit it.")"

if gh_api GET "/repos/${ACC_GITHUB_REPO}/issues/${ACC_ISSUE}" >"${WORK}/issue.json"; then
  jq -e --arg x "${ACC_ISSUE_EXPECT}" \
      '(has("pull_request") | not) and (((.title // "") + "\n" + (.body // "")) | contains($x))' \
      "${WORK}/issue.json" >/dev/null \
    || die "#${ACC_ISSUE} on ${ACC_GITHUB_REPO} is a pull request or does not say '${ACC_ISSUE_EXPECT}': the claude-code issue check would read the wrong text. Point SWARM_ACCEPTANCE_ISSUE in scripts/acceptance/config.sh at the fixture issue."
  ok "fixture issue #${ACC_ISSUE} names ${ACC_ISSUE_EXPECT}"
elif [[ "$(cat "${WORK}/status")" == "404" ]]; then
  gh_api GET "/repos/${ACC_GITHUB_REPO}/issues?state=all&per_page=1" >"${WORK}/any.json" \
    || die "could not list ${ACC_GITHUB_REPO}'s issues (HTTP $(cat "${WORK}/status")): $(redact <"${WORK}/any.json" | head -c 200)"
  [[ "$(jq 'length' "${WORK}/any.json")" == "0" && "${ACC_ISSUE}" == "1" ]] \
    || die "${ACC_GITHUB_REPO} has no issue #${ACC_ISSUE}, and opening one now would not get that number. Open an issue whose title says '${ACC_ISSUE_EXPECT}' and set SWARM_ACCEPTANCE_ISSUE in scripts/acceptance/config.sh to its number."
  gh_api POST "/repos/${ACC_GITHUB_REPO}/issues" \
      "$(jq -nc --arg t "${issue_title}" --arg b "${issue_body}" '{title: $t, body: $b}')" >"${WORK}/created.json" \
    || die "could not open the fixture issue on ${ACC_GITHUB_REPO} (HTTP $(cat "${WORK}/status")): $(redact <"${WORK}/created.json" | head -c 200)"
  created="$(jq -r '.number // empty' "${WORK}/created.json")"
  [[ "${created}" == "${ACC_ISSUE}" ]] \
    || die "opened the fixture issue as #${created:-?}, not #${ACC_ISSUE}: set SWARM_ACCEPTANCE_ISSUE in scripts/acceptance/config.sh to ${created:-its number}"
  ok "opened fixture issue #${created}"
else
  die "could not read issue #${ACC_ISSUE} on ${ACC_GITHUB_REPO} (HTTP $(cat "${WORK}/status")): $(redact <"${WORK}/issue.json" | head -c 200)"
fi

# --- 2. The fixtures ------------------------------------------------------------
# git asks GIT_ASKPASS for the password; the URL names only the user, which
# is not a secret.
askpass="${WORK}/askpass.sh"
# shellcheck disable=SC2016  # expanded by the askpass process, from its environment
printf '#!/bin/sh\nprintf "%%s\\n" "${SWARM_ACCEPTANCE_GITHUB_TOKEN}"\n' >"${askpass}"
chmod 700 "${askpass}"
export GIT_ASKPASS="${askpass}" GIT_TERMINAL_PROMPT=0 SWARM_ACCEPTANCE_GITHUB_TOKEN="${TOKEN}"
url="https://x-access-token@github.com/${ACC_GITHUB_REPO}.git"
clone="${WORK}/sandbox"

if git clone --quiet --depth 1 --branch "${ACC_REF}" "${url}" "${clone}" 2>"${WORK}/git.err"; then
  :
elif rm -rf "${clone}" && git clone --quiet "${url}" "${clone}" 2>"${WORK}/git.err" \
    && [[ -z "$(git -C "${clone}" rev-parse --verify -q HEAD 2>/dev/null || true)" ]]; then
  # An empty repository: this commit creates ACC_REF.
  git -C "${clone}" checkout --quiet --orphan "${ACC_REF}"
  info "${ACC_GITHUB_REPO} is empty: creating ${ACC_REF}"
else
  die "could not clone ${ACC_GITHUB_REPO} at ${ACC_REF}: $(redact <"${WORK}/git.err" | head -n 3 | tr '\n' ' ')"
fi

rm -rf "${clone:?}/${FIXTURES}"
mkdir -p "${clone}/$(dirname "${FIXTURES}")"
cp -R "${REPO_ROOT}/${FIXTURES}" "${clone}/${FIXTURES}"
git -C "${clone}" add -A -- "${FIXTURES}"
if git -C "${clone}" diff --cached --quiet; then
  ok "${FIXTURES}/ on ${ACC_GITHUB_REPO}@${ACC_REF} already matches this commit"
  exit 0
fi
source_sha="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || printf 'unknown')"
git -C "${clone}" -c user.name="swarm-acceptance" -c user.email="swarm-acceptance@users.noreply.github.com" \
  commit --quiet -m "Acceptance fixtures as of ${source_sha}"
git -C "${clone}" push --quiet origin "HEAD:refs/heads/${ACC_REF}" 2>"${WORK}/git.err" \
  || die "could not push ${FIXTURES}/ to ${ACC_GITHUB_REPO}@${ACC_REF}: $(redact <"${WORK}/git.err" | head -n 3 | tr '\n' ' ')"
if git -C "${clone}" rev-parse -q --verify HEAD~1 >/dev/null; then
  pushed="$(git -C "${clone}" diff --stat HEAD~1 HEAD | tail -n 1)"
else
  pushed="the sandbox's first commit"
fi
ok "pushed ${FIXTURES}/ as of ${source_sha} to ${ACC_GITHUB_REPO}@${ACC_REF} (${pushed})"
