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
#
# CONCURRENCY-TEST RUNS LAST, because it is the one target that leaves work
# behind. It submits three times the global limit of mock tasks and cancels
# them on exit, and cancelled leases come back over the following seconds to
# minutes. Run second, as it was until 2026-10-20 (#1754), its backlog was still
# leased when race-test narrowed runner:mock (`active=40`, none of race-test's
# own tasks admitted) and when e2e read every task holding capacity. race-test
# now refuses a pool held by other work and counts only its own leases, and
# every target cancels what it submitted on exit; this order is what keeps the
# gate from leaning on that refusal to sequence its own targets.
[[ "${#TARGETS[@]}" -gt 0 ]] || TARGETS=(smoke-test race-test e2e-test concurrency-test)

# ---------------------------------------------------------------------------
# EVERY EXECUTION SAYS WHO STARTED IT (#1822 comment 31).
#
# accept.yml's quiesce used to cancel every running swarm-verify execution,
# an operator's `make verify-remote` in flight among them, whose run then read
# "FAILED" like a regression. So each execution carries SWARM_VERIFY_ORIGIN:
# `operator` unless the caller says otherwise, and accept.yml sets
# `accept-<run id>` for its whole workflow. Its quiesce and cleanup steps
# cancel only executions whose origin names an accept run, and print the rest
# as skipped.
#
# An environment override, not a label: `gcloud run jobs execute` takes
# `--args` and `--update-env-vars` and no label flag, and the execution
# records the override in its own template, where `executions list --format
# json` reads it back. The value is one of two shapes and nothing else: a
# comma would add a variable of the caller's choosing to the execution.
SWARM_VERIFY_ORIGIN="${SWARM_VERIFY_ORIGIN:-operator}"
[[ "${SWARM_VERIFY_ORIGIN}" =~ ^(operator|accept-[0-9]+)$ ]] \
  || die "SWARM_VERIFY_ORIGIN must be 'operator' or 'accept-<GitHub run id>', not '${SWARM_VERIFY_ORIGIN}'"

# THE ROLES PROBE'S SETTINGS (ROLES-ACCEPT, scripts/acceptance/groups/roles.sh)
# reach the execution the same way, each only when set: the job's own
# environment is its Terraform template's, and these four are the repository's
# variables, which accept.yml's smoke job passes in. None is a secret (a
# tenant id, two service-account emails, a workspace id), and each is checked
# for the characters those use before it goes near the comma-separated flag.
FORWARDED_SETTINGS=(
  SWARM_ACCEPTANCE_ROLES_TENANT
  SWARM_ACCEPTANCE_ROLES_VIEWER_SA
  SWARM_ACCEPTANCE_ROLES_ADMIN_SA
  SWARM_ACCEPTANCE_ROLES_OTHER_WORKSPACE
)
EXECUTION_ENV="SWARM_VERIFY_ORIGIN=${SWARM_VERIFY_ORIGIN}"
for setting in "${FORWARDED_SETTINGS[@]}"; do
  value="${!setting:-}"
  [[ -n "${value}" ]] || continue
  [[ "${value}" =~ ^[A-Za-z0-9@._-]+$ ]] \
    || die "${setting} may hold letters, digits, '@', '.', '_' and '-' only"
  EXECUTION_ENV+=",${setting}=${value}"
done

step "Verification gate, inside the VPC"
info "job     ${JOB} (${REGION}/${PROJECT_ID})"
info "targets ${TARGETS[*]}"
info "origin  ${SWARM_VERIFY_ORIGIN}"

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

# execution_cancelled EXECUTION -> prints why the execution was cancelled and
# returns 0 when Cloud Run says it was; returns 1 for anything else, an
# unreadable execution included, so a failure is never excused as a cancel.
#
# A cancel is not a regression in the target: something stopped it -- an
# accept run's quiesce (which now cancels accept runs' executions only), its
# cleanup after a GitHub cancel, or a person -- so it is reported apart from
# FAILED. Cloud Run does not record who cancelled; the audit log does.
execution_cancelled() {
  local execution="$1" described why
  described="$(mktemp "${TMPDIR:-/tmp}/swarm-verify-describe.XXXXXX")"
  if ! gcloud run jobs executions describe "${execution}" \
      --project "${PROJECT_ID}" --region "${REGION}" --format json >"${described}" 2>/dev/null; then
    rm -f "${described}"
    return 1
  fi
  why="$(jq -r '
      (.status // {}) as $s
      | [($s.conditions // [])[] | select((.reason // "") | test("cancel"; "i"))] as $c
      | if (($s.cancelledCount // 0) > 0) or ($c | length) > 0
        then ($c[0].message // "Cloud Run counts a cancelled task") else empty end' \
    <"${described}" 2>/dev/null | head -n 1 | redact)" || why=""
  rm -f "${described}"
  [[ -n "${why}" ]] || return 1
  printf '%s' "${why}"
}

FAILED=()
CANCELLED=()
for target in "${TARGETS[@]}"; do
  step "${target}"

  # `--wait` blocks until the execution finishes and propagates its exit code,
  # so the shell's own status is the test's status.
  #
  # `target` names an acceptance group as "acceptance/mock", so it is never
  # safe to splice into a path directly -- "swarm-verify-acceptance/mock.$$"
  # points INSIDE a directory ("swarm-verify-acceptance") nothing creates,
  # and the redirection below fails before gcloud ever runs (release
  # 36678195824: "line 212: /tmp/swarm-verify-acceptance/mock.2283: No such
  # file or directory"). mktemp names the file itself, with no target text
  # in the path at all.
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-verify.XXXXXX")"
  rc=0
  gcloud run jobs execute "${JOB}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --args "scripts/${target}.sh" \
    --update-env-vars "${EXECUTION_ENV}" \
    --wait \
    > "${out}" 2>&1 || rc=$?

  redact < "${out}" | tail -n 40

  if [[ "${rc}" -ne 0 ]]; then
    execution="$(execution_name "${out}")"
    if [[ -n "${execution}" ]] && why="$(execution_cancelled "${execution}")"; then
      warn "${target} CANCELLED (by a cancel of execution ${execution}: ${why}); not a failure of the target, run it again"
      CANCELLED+=("${target}")
      rm -f "${out}"
      continue
    fi
    err "${target} FAILED (exit ${rc})"
    FAILED+=("${target}")
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
if [[ "${#FAILED[@]}" -gt 0 || "${#CANCELLED[@]}" -gt 0 ]]; then
  [[ "${#CANCELLED[@]}" -eq 0 ]] || err "${#CANCELLED[@]} of ${#TARGETS[@]} targets cancelled: ${CANCELLED[*]}"
  [[ "${#FAILED[@]}" -eq 0 ]] || err "${#FAILED[@]} of ${#TARGETS[@]} targets failed: ${FAILED[*]}"
  exit 1
fi
ok "all ${#TARGETS[@]} verification targets passed"
