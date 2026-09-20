#!/usr/bin/env bash
#
# Run the verification gate from inside the VPC.
#
# CLAUDE.md requires `make smoke concurrency-test race-test` against a deployed
# environment before a change to admission, dispatch or reconciliation can be
# called done. Those targets cannot reach the API from a laptop: swarm-api runs
# with ingress `internal-and-cloud-load-balancing`, so the `*.run.app` address
# they resolve to refuses anything that did not arrive through the load
# balancer or the VPC. See docs/audits/2026-09-20/verification-targets-cannot-run.md.
#
# This executes the same scripts, unchanged, in a Cloud Run job that IS in the
# VPC. The job's identity holds run.invoker on swarm-api and nothing else, and
# its ID token comes from the metadata server rather than from a key file.
#
# Exit code is the job's exit code. A gate that swallows the failure it was
# built to catch is worse than no gate, so nothing here uses `|| true` and the
# execution's status is read back explicitly rather than inferred from the
# absence of an error message.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

load_env
require_cmd gcloud

JOB="swarm-verify"
TARGETS=("$@")
[[ "${#TARGETS[@]}" -gt 0 ]] || TARGETS=(smoke-test concurrency-test race-test)

step "Verification gate, inside the VPC"
info "job     ${JOB} (${REGION}/${PROJECT_ID})"
info "targets ${TARGETS[*]}"

FAILED=()
for target in "${TARGETS[@]}"; do
  step "${target}"

  # `--wait` blocks until the execution finishes and propagates its exit code,
  # so the shell's own status is the test's status.
  out="${TMPDIR:-/tmp}/swarm-verify-${target}.$$"
  rc=0
  gcloud run jobs execute "${JOB}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --args "scripts/${target}.sh" \
    --wait \
    > "${out}" 2>&1 || rc=$?

  redact < "${out}" | tail -n 40
  rm -f "${out}"

  if [[ "${rc}" -ne 0 ]]; then
    err "${target} FAILED (exit ${rc})"
    FAILED+=("${target}")
  else
    ok "${target}"
  fi
done

hr
if [[ "${#FAILED[@]}" -gt 0 ]]; then
  die "${#FAILED[@]} of ${#TARGETS[@]} targets failed: ${FAILED[*]}"
fi
ok "all ${#TARGETS[@]} verification targets passed"
