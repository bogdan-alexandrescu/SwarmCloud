#!/usr/bin/env bash
# The frozen contract is unchanged by this pull request, or its change is
# recorded as accepted by the owner.
#
# apps/common/swarm_common is frozen by CONTRACT.md. Every other component was
# written against those exact types, so a change there breaks all of them at
# once and must be a deliberate, discussed act. CLAUDE.md rule 1 sends every
# contract change through docs/contract-change-requests.md, so a diff under
# swarm_common passes only when the pull request ADDS a line there containing
# "accepted by the owner". The line is printed, so the reviewer merging the PR
# reads the claim the change rests on.
#
# This is application.yml's step, moved here unchanged so that what it
# compares against can be tested: tests/unit/scripts/test_frozen_contract_check.py.
#
# Usage: scripts/lib/check-frozen-contract.sh --pr-base SHA --pr-head SHA [--repo DIR]
#   --pr-base  github.event.pull_request.base.sha
#   --pr-head  github.event.pull_request.head.sha
#   --repo     the checkout to inspect (default: this repository)

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

FROZEN_DIR="apps/common/swarm_common"
REQUESTS="docs/contract-change-requests.md"
MARK="accepted by the owner"

REPO="${REPO_ROOT}"
PR_BASE=""
PR_HEAD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr-base|--pr-head|--repo)
      [[ $# -ge 2 ]] || die "$1 needs a value"
      case "$1" in
        --pr-base) PR_BASE="$2" ;;
        --pr-head) PR_HEAD="$2" ;;
        --repo)    REPO="$2" ;;
      esac
      shift 2
      ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "${PR_BASE}" ]] || die "--pr-base is required"

g() { git -C "${REPO}" "$@"; }

echo "pull request head: ${PR_HEAD:-unknown}"
if ! g cat-file -e "${PR_BASE}^{commit}" 2>/dev/null; then
  g fetch --no-tags --depth=1 origin "${PR_BASE}"
fi
changed="$(g diff --name-only "${PR_BASE}" HEAD -- "${FROZEN_DIR}")"
if [[ -z "${changed}" ]]; then
  echo "frozen contract untouched"
  exit 0
fi
echo "changed under ${FROZEN_DIR}/:"
printf '%s\n' "${changed}" | sed 's/^/  /'
requests_diff="$(g diff "${PR_BASE}" HEAD -- "${REQUESTS}")"
accepted="$(printf '%s\n' "${requests_diff}" | grep -E '^\+[^+]' | grep -iF "${MARK}" || true)"
if [[ -z "${accepted}" ]]; then
  echo "::error::${FROZEN_DIR}/ is FROZEN (see CONTRACT.md)."
  echo "A change there needs the owner's acceptance, recorded in THIS PR as an added line in"
  echo "${REQUESTS} containing '${MARK}', next to the request."
  g diff --stat "${PR_BASE}" HEAD -- "${FROZEN_DIR}"
  exit 1
fi
echo "the change rests on this recorded acceptance:"
printf '%s\n' "${accepted}"
