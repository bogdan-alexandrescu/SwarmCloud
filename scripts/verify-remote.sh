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
# VPC. Its ID token comes from the metadata server rather than from a key file.
#
# The job's identity holds four grants, all read-only at the project level:
# run.invoker on swarm-api, roles/datastore.viewer, roles/run.viewer and
# storage.objectViewer on the artifact bucket (terraform/infra/verify.tf). The
# header here used to say "run.invoker and nothing else", which stopped being
# true when the suites were found to read Firestore and Cloud Run directly --
# and an out-of-date statement of what an identity can do is how the next
# permission gets granted without anyone weighing it.
#
# ONE TARGET NEEDS MORE THAN THOSE GRANTS. race-test narrows a pool to force
# contention, which is a WRITE, and roles/datastore.viewer cannot make it. The
# owner decided on 2026-09-24 that it goes through the admin API rather than a
# Firestore write role: in dev the job's identity is also in `admin_users`, and
# race-test narrows runner:mock with PUT /v1/admin/limits/runner/mock. Of the
# default targets below, that is the only write made outside the job's own
# tenant. Why, and what admin costs, is at the top of scripts/race-test.sh.
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
# `e2e-test` is in the DEFAULT list, not merely available to be named.
#
# A suite nothing invokes rots, and this repository has the receipts:
# tests/integration errored in its fixture for 95 commits because `make test`
# did not run it, and tests/terraform's 86 assertions were wired into no target
# at all. The seam checks are the ones that would have caught the last three
# days of defects, so they belong in what runs by default rather than in what
# somebody remembers to type.
[[ "${#TARGETS[@]}" -gt 0 ]] || TARGETS=(smoke-test concurrency-test race-test e2e-test)

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
