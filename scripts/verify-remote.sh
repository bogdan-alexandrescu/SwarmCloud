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
# Firestore write role: in dev the job's identity is on `admin_pool_users`, which
# reaches PUT /v1/admin/limits/runner/{profile} and no other admin route, and
# race-test narrows runner:mock with it. It is NOT a platform admin (the first
# form of the decision made it one; the owner reversed that the same day). Of
# the default targets below, that is the only write made outside the job's own
# tenant. Why, and what it can still reach, is at the top of scripts/race-test.sh.
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

# ---------------------------------------------------------------------------
# A FAILED TARGET PRINTS ITS OWN TRANSCRIPT, NOT ONLY GCLOUD'S VERDICT.
#
# `gcloud run jobs execute --wait` streams progress dots and, on failure, one
# sentence: "Executing job failed ... gcloud run jobs executions describe
# swarm-verify-m9prt". The suite's own output -- which case failed, and why --
# goes to Cloud Logging, not to this terminal. So release 36038727721 printed
# "smoke-test FAILED (exit 1)" and nothing else, and finding out that the two
# failures were "could not read the runner-profile catalogue" and "the backend
# matrix visited 0 backends" took a second person, a second tool and the
# execution name copied out of the log by hand.
#
# So on a failed target the execution is named from gcloud's own output and
# its stdout/stderr (plus the platform's "Container called exit(1)." line) are
# read back from Cloud Logging, oldest first, through `redact`: they are a test
# suite's transcript, and a transcript is exactly where a token turns up.
#
# The job's audit events share the execution label and are excluded -- they
# are a JSON blob per state change and say nothing about which case failed.
#
# IT IS READ THROUGH ONE LOG VIEW, NOT FROM THE PROJECT. The release runs this
# script as swarm-tf-deployer, whose only logging role (configWriter) cannot
# read an entry, and this project is SHARED, so the answer is not a
# project-wide log read. The owner decided on 2026-09-24 that the deployer may
# read this job's logs ONLY: terraform/bootstrap/verify_logs.tf puts a view on
# the project's _Default bucket that selects the swarm-verify job and nothing
# else, and grants the deployer roles/logging.viewAccessor conditioned on that
# view. LOG_BUCKET, LOG_LOCATION and LOG_VIEW are that view; `--bucket
# --location --view` make gcloud send it to entries.list as the resource
# read, which is what the condition names.
# tests/integration/test_verify_remote_prints_the_job_log.py reads the grant out
# of the terraform and lets its fake release identity read that one view, so a
# rename on either side fails there.
#
# The grant is in the bootstrap root, which the OWNER applies (`make
# bootstrap`), never the release. Until it is applied the read is refused.
# What is printed then says so, names what grants the read, and gives the
# project-wide command that reads the same lines for anyone holding
# roles/logging.viewer. The target's verdict is unchanged: a missing transcript
# must never turn a failed gate into anything else.
#
# The filter below still names the job, the execution and the log. Through the
# view the job clauses are redundant, but the execution clause picks this run,
# the logName clause keeps out anything the job writes that is not its
# stdout/stderr, and the same filter is the by-hand command's, which reads the
# whole project.
LOG_BUCKET="_Default"
LOG_LOCATION="global"
LOG_VIEW="swarm-verify"

# LOG_TAIL_ENTRIES is how many lines are shown. The smoke suite's whole
# transcript was 41 lines on 2026-09-24 (swarm-verify-m9prt: 40 stderr, 1
# varlog/system); 80 holds that with room, and the failure summary is at the
# END of every suite's transcript, which a tail always includes.
LOG_TAIL_ENTRIES=80
# Cloud Logging ingests with a delay of a few seconds, and the job can finish
# before its last lines are queryable. LOG_READ_ATTEMPTS is the number of READS
# in all -- the first and then two retries, LOG_READ_WAIT_SECONDS apart -- before
# an empty answer is reported as empty. The wait is injectable so the offline
# test does not sleep; the default is what a real deployment needs.
LOG_READ_ATTEMPTS=3
LOG_READ_WAIT_SECONDS="${SWARM_VERIFY_LOG_WAIT_SECONDS:-5}"

# execution_name GCLOUD_OUTPUT_FILE -> the execution this run created, or
# nothing. Read from gcloud's own words ("executions describe <name>", and the
# console URL ".../executions/details/<region>/<name>") rather than from "the
# latest execution of the job", which another release running at the same time
# would make somebody else's.
#
# sed, not grep: "no match" is an answer here, and grep reports it as exit 1,
# which under pipefail is a failure this function would then have to swallow.
execution_name() {
  local file="$1" name
  name="$(sed -nE "s/.*executions describe (${JOB}-[a-z0-9]+).*/\\1/p" "${file}" | sed -n 1p)"
  if [[ -z "${name}" ]]; then
    name="$(sed -nE "s|.*/executions/details/${REGION}/(${JOB}-[a-z0-9]+).*|\\1|p" "${file}" | sed -n 1p)"
  fi
  printf '%s' "${name}"
}

# print_execution_log EXECUTION -> the execution's own last lines, or why they
# could not be read. Never fails: it explains a failure, it does not decide one.
print_execution_log() {
  local execution="$1" filter entries read_err attempt=1 n=0 rc
  filter="resource.type=\"cloud_run_job\""
  filter+=" AND resource.labels.job_name=\"${JOB}\""
  filter+=" AND labels.\"run.googleapis.com/execution_name\"=\"${execution}\""
  filter+=" AND logName:\"run.googleapis.com%2F\""
  entries="$(mktemp "${TMPDIR:-/tmp}/swarm-verify-log.XXXXXX")"
  read_err="$(mktemp "${TMPDIR:-/tmp}/swarm-verify-logerr.XXXXXX")"
  while :; do
    rc=0
    gcloud logging read "${filter}" \
      --project "${PROJECT_ID}" \
      --bucket "${LOG_BUCKET}" \
      --location "${LOG_LOCATION}" \
      --view "${LOG_VIEW}" \
      --order desc \
      --limit "${LOG_TAIL_ENTRIES}" \
      --freshness 1d \
      --format json \
      >"${entries}" 2>"${read_err}" || rc=$?
    if [[ "${rc}" -ne 0 ]]; then
      err "could not read the log of execution ${execution} (gcloud logging read exit ${rc}):"
      redact <"${read_err}" | head -n 3 | sed 's/^/     /' >&2
      err "reading it needs roles/logging.viewAccessor on projects/${PROJECT_ID}/locations/${LOG_LOCATION}/buckets/${LOG_BUCKET}/views/${LOG_VIEW}"
      err "(terraform/bootstrap/verify_logs.tf grants it to the release's deployer; the owner applies it with make bootstrap)"
      info "the same lines, by hand, with roles/logging.viewer: gcloud logging read '${filter}' --project ${PROJECT_ID} --order asc --limit ${LOG_TAIL_ENTRIES}"
      rm -f "${entries}" "${read_err}"
      return 0
    fi
    # `length` over an array. Anything else -- gcloud printing nothing, or an
    # object -- is not an answer and counts as empty rather than as a crash.
    n="$(jq -r 'if type == "array" then length else 0 end' <"${entries}" 2>/dev/null || printf '0')"
    if [[ "${n}" -gt 0 || "${attempt}" -ge "${LOG_READ_ATTEMPTS}" ]]; then
      break
    fi
    attempt=$(( attempt + 1 ))
    sleep "${LOG_READ_WAIT_SECONDS}"
  done
  rm -f "${read_err}"
  if [[ "${n}" -eq 0 ]]; then
    warn "execution ${execution} has no stdout/stderr in Cloud Logging after ${attempt} read(s)"
    info "it may not be ingested yet, or be older than the ${LOG_BUCKET} bucket keeps logs: gcloud logging read '${filter}' --project ${PROJECT_ID} --order asc"
    rm -f "${entries}"
    return 0
  fi
  info "execution ${execution}: its last ${n} log line(s), oldest first"
  # `--order desc --limit N` is the NEWEST N; reversed here so the transcript
  # reads the way it was written, ending on the suite's summary.
  # Inside `if !` so that a rendering failure is reported rather than killing
  # this script under set -e before the remaining targets have run.
  if ! jq -r 'reverse | .[] | objects
         | if .textPayload != null then .textPayload
           elif (.jsonPayload | type) == "object" then (.jsonPayload.message // (.jsonPayload | tojson))
           else "" end' \
       <"${entries}" | redact | sed 's/^/   | /' >&2; then
    warn "the log of execution ${execution} was read but could not be rendered"
  fi
  rm -f "${entries}"
}

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

  if [[ "${rc}" -ne 0 ]]; then
    err "${target} FAILED (exit ${rc})"
    FAILED+=("${target}")
    execution="$(execution_name "${out}")"
    if [[ -n "${execution}" ]]; then
      print_execution_log "${execution}"
    else
      # No execution named: gcloud refused before creating one (permission,
      # a missing job), and its own output above is the whole story.
      warn "gcloud named no execution for ${target}, so there is no job log to read; its output above is all there is"
    fi
  else
    ok "${target}"
  fi
  rm -f "${out}"
done

hr
if [[ "${#FAILED[@]}" -gt 0 ]]; then
  die "${#FAILED[@]} of ${#TARGETS[@]} targets failed: ${FAILED[*]}"
fi
ok "all ${#TARGETS[@]} verification targets passed"
