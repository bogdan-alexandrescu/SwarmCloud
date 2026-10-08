#!/usr/bin/env bash
# WHERE the acceptance suite works: the tenant it submits for and the
# repository its tasks clone and open pull requests on. Stated once, here, and
# read by run.sh (through lib.sh), github-cleanup.sh and sandbox-sync.sh.
#
# THE `smoke` TENANT, AND THE PRIVATE SANDBOX (owner decision 2026-10-05,
# #628). Until then the suite submitted as swarm-verify, which resolves to
# `eng`, against this platform's own public repository. In five days that
# opened and closed 58 fixture pull requests there, each running full CI, and
# made acceptance 62% of eng's tasks -- so eng's success rates and failure
# classes could no longer be read. The suite now:
#
#   * sends `X-Swarm-Tenant: ${ACC_TENANT}` on every API call (lib.sh exports
#     SWARM_API_TENANT, which common.sh's api_request turns into the header).
#     The header SELECTS among the caller's verified group memberships and
#     never grants, so swarm-verify must be a member of smoke's directory
#     group; run.sh refuses to start when the API resolves it to anything
#     else, rather than running in eng again (docs/ci.md, "Release
#     acceptance runs in the smoke tenant");
#   * clones, and opens its pull requests on, the private sandbox repository.
#     The smoke tenant's own forge token (Secret Manager,
#     swarm-tenant-smoke-git) is what the worker clones and pushes with.
#
# Not a tenants.<t>.service_accounts listing: a listed account is
# continuation-scoped (swarm_api.auth CONTINUATION_ROUTES) and may submit
# nothing but a continuation, so every suite swarm-verify runs would stop.
#
# Sourced, never run. Pure: no network, no credentials, no common.sh, so
# tests/unit/scripts/test_acceptance_target.py reads it exactly as the suite
# does.

# Sourced by lib.sh, github-cleanup.sh and sandbox-sync.sh, which read every
# variable set here; checked on its own, shellcheck sees them as unused.
# shellcheck disable=SC2034
set -euo pipefail

#: The repository acceptance clones by default: private, created by the owner
#: on 2026-10-05, default branch main. sandbox-sync.sh keeps its
#: tests/acceptance/fixtures/ equal to the released commit's.
ACC_SANDBOX_REPOSITORY_URL="https://github.com/bogdan-alexandrescu/swarmcloud-sandbox.git"

#: The tenant every submission is made for, as `X-Swarm-Tenant`.
ACC_TENANT="${SWARM_ACCEPTANCE_TENANT:-smoke}"
#: The repository every task that clones clones, and where its pull requests
#: open. An override is checked by acc_config_problem below, and run.sh asks
#: GitHub that it is not publicly readable before anything is submitted.
ACC_REPOSITORY_URL="${SWARM_ACCEPTANCE_REPOSITORY_URL:-${ACC_SANDBOX_REPOSITORY_URL}}"
#: The ref cloned. `main` because the release deploys main and sandbox-sync.sh
#: writes that commit's fixtures to the sandbox's main just before the suite
#: runs. verify-remote.sh cannot pass an environment variable into the
#: swarm-verify job, so a release run always reads main; set it by hand to
#: prove a branch you pushed to the sandbox yourself.
ACC_REF="${SWARM_ACCEPTANCE_REF:-main}"
#: owner/repo on GitHub, derived from the URL: where pull requests open, and
#: where the suite reads them back from.
ACC_GITHUB_REPO="$(printf '%s' "${ACC_REPOSITORY_URL}" | sed -E 's#^https://github\.com/##; s#\.git$##; s#/+$##')"
ACC_GITHUB_API="${SWARM_ACCEPTANCE_GITHUB_API:-https://api.github.com}"
#: The sandbox issue the claude-code `issue` check fetches, and a string only
#: its text carries. sandbox-sync.sh opens it on a sandbox that has none --
#: as #1, the first number a new repository hands out -- and refuses a sandbox
#: whose issue #ACC_ISSUE says something else.
ACC_ISSUE="${SWARM_ACCEPTANCE_ISSUE:-1}"
ACC_ISSUE_EXPECT="${SWARM_ACCEPTANCE_ISSUE_EXPECT:-sandbox-probe.sh}"

# _acc_owner_repo URL -> "owner/repo" in lower case, from an https or ssh
# GitHub remote; empty for anything else.
_acc_owner_repo() {
  printf '%s' "$1" \
    | sed -nE 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+)$#\2#p' \
    | sed -E 's#\.git$##; s#/+$##' \
    | tr '[:upper:]' '[:lower:]'
}

# acc_config_problem -> returns 0 and prints nothing when the target is one
# acceptance may work in; otherwise prints why to stderr and returns 1.
#
# THE TARGET MAY NEVER BE THE REPOSITORY THIS CODE CAME FROM. That is the
# repository a fixture pull request runs full CI on and its readers watch. It
# is recognised without being named: GITHUB_REPOSITORY in a CI run, and this
# checkout's own origin anywhere git can read it. (The swarm-verify image has
# neither; run.sh's acc_require_private_repository refuses a public target
# there, and anywhere else.)
acc_config_problem() {
  local here origin_repo target
  if [[ ! "${ACC_TENANT}" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    printf 'acceptance: refusing tenant %q: a tenant id is lower-case letters, digits and hyphens\n' "${ACC_TENANT}" >&2
    return 1
  fi
  if [[ ! "${ACC_REPOSITORY_URL}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    printf 'acceptance: refusing repository %q: expected https://github.com/<owner>/<repo>[.git]\n' "${ACC_REPOSITORY_URL}" >&2
    return 1
  fi
  target="$(_acc_owner_repo "${ACC_REPOSITORY_URL}")"
  if [[ -n "${GITHUB_REPOSITORY:-}" ]] \
      && [[ "${target}" == "$(printf '%s' "${GITHUB_REPOSITORY}" | tr '[:upper:]' '[:lower:]')" ]]; then
    printf 'acceptance: refusing %s: it is the repository this CI run is for (GITHUB_REPOSITORY). Acceptance runs against the private sandbox only (#628).\n' "${ACC_GITHUB_REPO}" >&2
    return 1
  fi
  here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
  if command -v git >/dev/null 2>&1 \
      && origin_repo="$(git -C "${here}" config --get remote.origin.url 2>/dev/null)" \
      && [[ -n "$(_acc_owner_repo "${origin_repo}")" ]] \
      && [[ "${target}" == "$(_acc_owner_repo "${origin_repo}")" ]]; then
    printf 'acceptance: refusing %s: it is the repository this checkout was cloned from. Acceptance runs against the private sandbox only (#628).\n' "${ACC_GITHUB_REPO}" >&2
    return 1
  fi
  return 0
}
