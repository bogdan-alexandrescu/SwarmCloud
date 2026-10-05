#!/usr/bin/env bash
# Close the pull requests and delete the branches the acceptance suite left on
# the sandbox repository.
#
# Usage: SWARM_ACCEPTANCE_GITHUB_TOKEN=... scripts/acceptance/github-cleanup.sh [--dry-run]
#
# WHY A SWEEP. The suite's direct-pr and integrate checks open real pull
# requests on the private sandbox (scripts/acceptance/config.sh,
# docs/acceptance.md). A check closes its own when its run holds a GitHub
# token, but in the release it runs in the swarm-verify job, which holds none
# -- on purpose: that job's identity is read-only everywhere else, and a forge
# token lives only in Secret Manager for the worker to read (CLAUDE.md). So
# the release's acceptance job runs this afterwards with the sandbox's own
# token, the repository secret SWARM_SANDBOX_GITHUB_TOKEN. The job's
# GITHUB_TOKEN reaches only the repository the workflow runs in, which is
# exactly the one acceptance must never touch (#628).
#
# WHICH REPOSITORY. config.sh's ACC_GITHUB_REPO, and nothing else: not
# GITHUB_REPOSITORY, which is the repository this CI run is for, and which
# config.sh refuses as a target.
#
# WHAT COUNTS AS THE SUITE'S, and nothing else is touched:
#
#   * an OPEN pull request whose head branch is `swarm/task_...` (only the
#     platform names branches that way) AND every file it changes lies under
#     tests/acceptance/fixtures/ (only the suite's prompts edit those) -- it is
#     closed, its head branch deleted, and so is every contributor branch its
#     body lists as merged (`- merged: \`swarm/...\``, the integrator's block);
#   * a `swarm/task_...` branch with no open pull request whose diff against
#     the default branch is non-empty and lies entirely under
#     tests/acceptance/fixtures/ -- a contributor branch, or a direct-pr branch
#     whose pull request was never opened -- is deleted.
#
# A pull request or branch that touches ANY other path is left alone, whoever
# opened it. Nothing is force-pushed, nothing is merged, and the default
# branch is never named.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=config.sh
source "${REPO_ROOT}/scripts/acceptance/config.sh"
acc_config_problem || die "refusing to sweep: see above"

DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  "") ;;
  -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
  *) die "unknown argument: $1" ;;
esac

TOKEN="${SWARM_ACCEPTANCE_GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"
[[ -n "${TOKEN}" ]] || die "no GitHub token for ${ACC_GITHUB_REPO}: set SWARM_ACCEPTANCE_GITHUB_TOKEN (the release passes the SWARM_SANDBOX_GITHUB_TOKEN secret)"
REPO="${ACC_GITHUB_REPO}"
API="${ACC_GITHUB_API}"
FIXTURES="tests/acceptance/fixtures/"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-acc-cleanup.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# gh_api METHOD PATH [BODY] -> body on stdout; non-zero on a non-2xx. The
# token reaches curl on stdin (-K -), never in argv.
gh_api() {
  local method="$1" path="$2" body="${3:-}" out="${WORK}/resp" code
  local args=(-sS -m 30 -o "${out}" -w '%{http_code}' -X "${method}"
    -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")
  [[ -z "${body}" ]] || args+=(-H "Content-Type: application/json" --data-binary "${body}")
  code="$(printf 'header = "Authorization: Bearer %s"\n' "${TOKEN}" | curl -K - "${args[@]}" "${API}${path}")" || return 1
  cat "${out}"
  [[ "${code}" =~ ^2 ]]
}

# only_fixtures FILES_JSON -> 0 when the list is non-empty and every filename
# lies under the fixtures directory.
only_fixtures() {
  jq -e --arg p "${FIXTURES}" 'length > 0 and all(.[]; (.filename // "") | startswith($p))' >/dev/null
}

delete_branch() {
  local branch="$1"
  [[ "${branch}" =~ ^swarm/task_[A-Za-z0-9_.-]+$ ]] || { warn "not deleting '${branch}': not a swarm/task_ branch"; return 0; }
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "would delete ${branch}"
    return 0
  fi
  if gh_api DELETE "/repos/${REPO}/git/refs/heads/${branch}" >/dev/null; then
    ok "deleted ${branch}"
  else
    warn "could not delete ${branch} (already gone?)"
  fi
}

step "Acceptance cleanup on ${REPO}"
closed=0
deleted_heads=" "

# 1. Open pull requests from swarm/task_ branches that change only fixtures.
gh_api GET "/repos/${REPO}/pulls?state=open&per_page=100" >"${WORK}/pulls.json" \
  || die "could not list open pull requests: $(redact <"${WORK}/pulls.json" | head -c 200)"
while IFS=$'\t' read -r number head; do
  [[ -n "${number}" ]] || continue
  gh_api GET "/repos/${REPO}/pulls/${number}/files?per_page=100" >"${WORK}/files.json" || { warn "could not read PR #${number}'s files; leaving it"; continue; }
  only_fixtures <"${WORK}/files.json" || continue
  info "PR #${number} (${head}) changes only ${FIXTURES}: the acceptance suite's"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    gh_api PATCH "/repos/${REPO}/pulls/${number}" '{"state":"closed"}' >/dev/null || { warn "could not close PR #${number}"; continue; }
    ok "closed PR #${number}"
  fi
  closed=$(( closed + 1 ))
  delete_branch "${head}"
  deleted_heads="${deleted_heads}${head} "
  # shellcheck disable=SC2016  # sed pattern below: the backtick and \1 are sed's, not the shell's
  while IFS= read -r merged; do
    [[ -n "${merged}" ]] || continue
    delete_branch "${merged}"
    deleted_heads="${deleted_heads}${merged} "
  done < <(jq -r --argjson n "${number}" '.[] | select(.number == $n) | .body // ""' "${WORK}/pulls.json" \
            | sed -nE 's/^- merged: `(swarm\/[A-Za-z0-9_.-]+)`.*$/\1/p')
done < <(jq -r '.[] | select(.head.ref | test("^swarm/task_")) | select(.head.repo.full_name == .base.repo.full_name) | [.number, .head.ref] | @tsv' "${WORK}/pulls.json")

# 2. Leftover swarm/task_ branches whose only changes are fixtures.
default_branch="$(gh_api GET "/repos/${REPO}" | jq -r '.default_branch // "main"')"
gh_api GET "/repos/${REPO}/git/matching-refs/heads/swarm/task_" >"${WORK}/refs.json" \
  || die "could not list swarm/task_ branches"
open_heads="$(jq -r '.[].head.ref' "${WORK}/pulls.json")"
swept=0
while IFS= read -r branch; do
  [[ -n "${branch}" ]] || continue
  case "${deleted_heads}" in *" ${branch} "*) continue ;; esac
  grep -qxF "${branch}" <<<"${open_heads}" && continue
  gh_api GET "/repos/${REPO}/compare/${default_branch}...${branch}" >"${WORK}/compare.json" || continue
  jq '.files // []' "${WORK}/compare.json" | only_fixtures || continue
  delete_branch "${branch}"
  swept=$(( swept + 1 ))
done < <(jq -r '.[].ref | sub("^refs/heads/"; "")' "${WORK}/refs.json")

ok "closed ${closed} pull request(s); swept ${swept} leftover branch(es)"
