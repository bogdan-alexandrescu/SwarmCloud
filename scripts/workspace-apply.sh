#!/usr/bin/env bash
# Steps 1 to 3 of the personal-workspace job (docs/workspaces.md §2.2, lane W6b
# of #847): validate, install the call guard, run register-tenant.sh.
#
# WHO RUNS THIS. Only images/workspace-apply/entry.py, step 0, by `execve`, in
# the Cloud Run job `swarm-workspace-apply` (terraform/bootstrap/), as
# swarm-workspace-deployer: the identity with project-wide account-IAM power
# that only this job may use (§2.4). It replaces
# scripts/cloudbuild/workspace-apply.yaml line for line, now that WD2 is
# re-decided (2026-10-10) and the apply is a Cloud Run job rather than a Cloud
# Build trigger. The image carries this file, read-only under /opt/swarm, and
# the job runs that image by digest, so a merged change here reaches the
# identity only when the owner's bootstrap apply moves the digest (§2.4 R1).
# .github/CODEOWNERS names the owner on it for the same reason.
#
# WHAT IT TRUSTS. Its environment is the FIXED one entry.py wrote (PATH, HOME,
# PROJECT_ID, REGION, SWARM_ENV_FILE, SWARM_CALL_GUARD,
# SWARM_CALL_GUARD_ENFORCE=1, SWARM_KUBECTL, CLOUDSDK_PYTHON, BUILD_ID,
# NO_COLOR): every variable the execution was handed was discarded before this
# started. Its arguments are the execution's two, re-checked below: neither
# this check nor entry.py's (nor, under option (ii), the workflow's) is
# load-bearing alone.
#
# THE THREE STEPS:
#   1. validate  -- exactly two arguments, the id and the mode, before anything
#                   else runs;
#   2. guard     -- check the guard against its own cases on this image, then
#                   write the expectation file that admits A1's reads only,
#                   then put the shims first on PATH and check that they took;
#   3. apply     -- scripts/register-tenant.sh --workspace, with every gcloud,
#                   kubectl and curl it makes passing through the guard.
#
# MODES. `create` or `limits` (§2.2). register-tenant.sh also has `--mode
# verify` (A9 only, the migration check); no approval asks for it, so it is not
# run through the job.
#
# ITS EXIT. 0 is done or nothing to do. register-tenant.sh's 3 is "stopped for
# the platform owner": not a failure of the execution, since the record says
# needs_owner, the refused call is in this execution's log, and the owner
# decides (§2.5), so it ends the execution as a success. Anything else fails
# the execution, and the record says why in a code, never in command output.
#
# THE LOGS. Lines here name the workspace id only (§2.6); the bootstrap's sink
# routes the job's log to the restricted bucket swarm-workspace-apply.
#
# Usage (from entry.py only):
#   scripts/workspace-apply.sh w-3f9a2c create
#   scripts/workspace-apply.sh w-3f9a2c limits

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCRIPTS_DIR="${REPO_ROOT}/scripts"
GUARD="${SCRIPTS_DIR}/lib/workspace-guard.sh"
GUARD_BIN="${SCRIPTS_DIR}/lib/guard-bin"

# --- 1. validate --------------------------------------------------------------
# A malformed start ends the execution here and writes nothing (§2.2 step 1).
[[ $# -eq 2 ]] || die "refused: the job takes exactly two arguments, <w-id> <mode>; it was given $#"
WORKSPACE_ID="$1"
MODE="$2"
[[ "${WORKSPACE_ID}" =~ ^w-[0-9a-f]{6}$ ]] || die "refused: the first argument is not a workspace id of the form w-<6 hex digits>"
case "${MODE}" in
  create|limits) ;;
  *) die "refused: the second argument is neither create nor limits" ;;
esac
[[ -n "${SWARM_CALL_GUARD:-}" ]] || die "refused: SWARM_CALL_GUARD is not set; entry.py sets it, and nothing else runs this"
[[ "${SWARM_CALL_GUARD_ENFORCE:-}" == "1" ]] || die "refused: SWARM_CALL_GUARD_ENFORCE is not 1; the deployer identity never runs unguarded"
echo "workspace ${WORKSPACE_ID}: mode ${MODE}, execution ${BUILD_ID:-<none>}"

# --- 2. guard -----------------------------------------------------------------
for tool in gcloud kubectl curl; do
  [[ -x "${GUARD_BIN}/${tool}" ]] || die "refused: the guard's ${tool} shim is missing from the image"
done
# A guard that cannot judge its own cases on this image must not judge this
# run: every rule against a call built to trip it, and one allowed call per
# rule (scripts/lib/workspace-guard-cases.json). Offline.
"${GUARD}" self-test
umask 077
rm -f "${SWARM_CALL_GUARD}" "${SWARM_CALL_GUARD}.stop"
# Before A1 reads the approved record, only A1's own reads can match.
"${GUARD}" init --workspace-id "${WORKSPACE_ID}"
export PATH="${GUARD_BIN}:${PATH}"
# register-tenant.sh --workspace makes the same check and refuses to start
# without it (§2.5); made here too so a run whose PATH did not take fails at
# this line, naming the tool.
for tool in gcloud kubectl curl; do
  resolved="$(command -v "${tool}" || true)"
  [[ "${resolved}" == "${GUARD_BIN}/${tool}" ]] \
    || die "refused: ${tool} resolves to '${resolved}', not the guard's shim"
done
[[ -r "${SWARM_CALL_GUARD}" ]] || die "refused: the guard's expectation file is missing"
echo "workspace ${WORKSPACE_ID}: guard installed"

# --- 3. apply -----------------------------------------------------------------
rc=0
"${SCRIPTS_DIR}/register-tenant.sh" --workspace "${WORKSPACE_ID}" --mode "${MODE}" || rc=$?
if [[ "${rc}" -eq 3 ]]; then
  echo "workspace ${WORKSPACE_ID}: stopped for the platform owner (needs_owner); the refused call is above" >&2
  exit 0
fi
exit "${rc}"
