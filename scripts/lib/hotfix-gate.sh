#!/usr/bin/env bash
# Does this commit on main come from a pull request labelled `hotfix`?
#
# hotfix.yml runs on every push to main (and by dispatch on main), because
# GitHub evaluates a workflow's `concurrency:` before any job and the hotfix
# lane needs a group of its own: a label cannot move a run into another group,
# so the label is read HERE, by the lane's first job, and every later job runs
# only on its answer (owner decision 2026-10-08, observer proposal H;
# docs/ci.md, "Hotfix releases").
#
# It asks GitHub which pull requests the commit belongs to
# (GET /repos/{repo}/commits/{sha}/pulls) and proceeds only if one of them was
# MERGED into main and carries the label. An open pull request that merely
# contains the commit does not count, and neither does one merged elsewhere.
#
# Usage:
#   hotfix-gate.sh --sha SHA --repo OWNER/NAME [--label hotfix] [--base main]
#
# stdout: `proceed=true|false` and `pr=<number or empty>`, for $GITHUB_OUTPUT.
# Exit 0 with proceed=false is the ordinary answer for an ordinary commit:
# the lane ends green having done nothing. Exit 1 is a question GitHub did not
# answer (after retries); it fails the lane rather than guess either way --
# guessing "no" would quietly turn a hotfix into a 100-minute release.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

GATE_SHA=""
GATE_REPO=""
GATE_LABEL="hotfix"
GATE_BASE="main"
# Between two tries of the API. Tests set 0.
GATE_RETRY_DELAY="${SWARM_HOTFIX_GATE_RETRY_DELAY:-5}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha)   GATE_SHA="${2:-}"; shift 2 ;;
    --repo)  GATE_REPO="${2:-}"; shift 2 ;;
    --label) GATE_LABEL="${2:-}"; shift 2 ;;
    --base)  GATE_BASE="${2:-}"; shift 2 ;;
    *) die "hotfix-gate.sh: unknown argument '$1'" ;;
  esac
done

[[ "${GATE_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "hotfix-gate.sh: --sha must be a full commit SHA, got '${GATE_SHA}'"
[[ "${GATE_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "hotfix-gate.sh: --repo must be OWNER/NAME, got '${GATE_REPO}'"
require_cmd gh jq

pulls="$(mktemp "${TMPDIR:-/tmp}/swarm-hotfix-gate.XXXXXX")"
trap 'rm -f "${pulls}" "${pulls}.err"' EXIT

tries=0
until gh api -H "Accept: application/vnd.github+json" \
    "repos/${GATE_REPO}/commits/${GATE_SHA}/pulls?per_page=100" >"${pulls}" 2>"${pulls}.err"; do
  tries=$((tries + 1))
  if [[ "${tries}" -ge 3 ]]; then
    err "GitHub did not say which pull request ${GATE_SHA} came from:"
    redact <"${pulls}.err" | head -n 3 | sed 's/^/     /' >&2
    exit 1
  fi
  sleep "${GATE_RETRY_DELAY}"
done

jq -e 'type == "array"' "${pulls}" >/dev/null 2>&1 || die "GitHub answered with something other than a list of pull requests"

merged="$(jq -r --arg base "${GATE_BASE}" \
  '[.[] | select(.merged_at != null and .base.ref == $base)] | map(.number | tostring) | join(" ")' "${pulls}")"
# `$want`, not `$label`: `label` is a jq keyword.
labelled="$(jq -r --arg base "${GATE_BASE}" --arg want "${GATE_LABEL}" \
  '[.[] | select(.merged_at != null and .base.ref == $base)
        | select([.labels[]?.name] | index($want))] | first | .number // empty' "${pulls}")"

if [[ -n "${labelled}" ]]; then
  ok "${GATE_SHA} is pull request #${labelled}, merged into ${GATE_BASE} with the '${GATE_LABEL}' label: the hotfix lane proceeds"
  printf 'proceed=true\npr=%s\n' "${labelled}"
elif [[ -n "${merged}" ]]; then
  info "${GATE_SHA} is pull request #${merged// /, #}, merged into ${GATE_BASE} without the '${GATE_LABEL}' label: the normal release ships it"
  printf 'proceed=false\npr=%s\n' "${merged%% *}"
else
  info "${GATE_SHA} belongs to no pull request merged into ${GATE_BASE}: nothing for the hotfix lane"
  printf 'proceed=false\npr=\n'
fi
