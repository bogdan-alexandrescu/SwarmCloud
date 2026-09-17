#!/usr/bin/env bash
# Self-test for common.sh's expired-session classifier.
#
# The classifier decides whether a gcloud failure means "log in again" or
# something else entirely. Getting that wrong is not cosmetic: a misclassified
# expired session sent an operator to `make infra` to create an Artifact
# Registry repository that already existed, and to rebuild six images that were
# already built. Both pieces of advice fail the same way, at a different
# message, which is how one expiry costs an hour.
#
# The fixtures below are the REAL strings gcloud produced, not invented ones,
# plus the failures that must NOT be treated as auth so a genuine missing
# resource still reports itself accurately.
#
# Usage: scripts/lib/auth-guard.sh --self-test

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SELF_TEST=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --self-test) SELF_TEST=1; shift ;;
    -h|--help)   sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "${SELF_TEST}" -eq 1 ]] || die "this script only runs as --self-test"

PASS=0
FAIL=0

expect_auth() {
  local label="$1" text="$2"
  if gcloud_auth_failure "${text}"; then
    ok "${label}"; PASS=$((PASS + 1))
  else
    err "${label}: should have been read as an expired session, was not"; FAIL=$((FAIL + 1))
  fi
}

expect_not_auth() {
  local label="$1" text="$2"
  if gcloud_auth_failure "${text}"; then
    err "${label}: read as an expired session, which would hide the real cause"; FAIL=$((FAIL + 1))
  else
    ok "${label}"; PASS=$((PASS + 1))
  fi
}

step "Auth-guard self-test"

# --- what an expired Workspace session really produced --------------------

expect_auth "ACCESS_TOKEN_TYPE_UNSUPPORTED" \
  "ERROR: (gcloud.secrets.versions.list) UNAUTHENTICATED: Request had invalid authentication credentials. Expected OAuth 2 access token, login cookie or other valid authentication credential. reason: ACCESS_TOKEN_TYPE_UNSUPPORTED"

expect_auth "bare UNAUTHENTICATED" \
  "ERROR: (gcloud.artifacts.repositories.describe) UNAUTHENTICATED: Request is missing required authentication credential."

expect_auth "invalid authentication credentials" \
  "Request had invalid authentication credentials. Expected OAuth 2 access token."

expect_auth "reauthentication demanded" \
  "ERROR: (gcloud.projects.describe) There was a problem refreshing your current auth tokens: Reauthentication required"

expect_auth "no credentials at all" \
  "ERROR: (gcloud.secrets.describe) Your current active account [bogdan@saga.xyz] does not have any valid credentials"

# --- and what must keep reporting itself ----------------------------------
#
# These are the ones that matter most. If a permission gap or a genuinely
# missing resource were classified as auth, the operator would re-login, watch
# it fail again, and have learned nothing.

expect_not_auth "a genuinely missing repository" \
  "ERROR: (gcloud.artifacts.repositories.describe) NOT_FOUND: Repository \"swarm-images\" not found."

expect_not_auth "a missing permission" \
  "ERROR: (gcloud.artifacts.docker.images.describe) PERMISSION_DENIED: Permission 'containeranalysis.occurrences.list' denied on resource"

expect_not_auth "a disabled API" \
  "ERROR: (gcloud.run.services.list) FAILED_PRECONDITION: Cloud Run Admin API has not been used in project saga-agents-staging before or it is disabled."

expect_not_auth "a quota error" \
  "ERROR: (gcloud.builds.submit) RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'Build requests'"

# The real one, verbatim. It carries "is the active account specified by the
# [core/account] property" -- close enough to the phrasing an expired session
# uses that a looser pattern would swallow it, and the operator would re-login
# instead of being told which permission they are missing.
expect_not_auth "permission denied, mentioning the active account" \
  "ERROR: (gcloud.artifacts.docker.images.describe) [bogdan@saga.xyz] does not have permission to access projects instance [saga-agents-staging] (or it may not exist): Permission 'containeranalysis.occurrences.list' denied on resource 'projects/00000030aa1b3e3c' (or it may not exist). This command is authenticated as bogdan@saga.xyz which is the active account specified by the [core/account] property"

expect_not_auth "an empty string" ""

hr
if [[ "${FAIL}" -gt 0 ]]; then
  die "auth-guard self-test: ${PASS} passed, ${FAIL} failed"
fi
ok "auth-guard self-test: ${PASS}/${PASS} passed"
