#!/usr/bin/env bash
#
# Run ONE no-op execution of each per-tenant Cloud Run worker job, so the
# image import a new digest costs is paid by the release and not by the first
# tenant task (#363).
#
#   scripts/warm-jobs.sh                 every per-tenant worker job listed
#   scripts/warm-jobs.sh JOB [JOB...]    only these (each still checked)
#
# WHY. Measured 2026-10-07 on #363 (874 claude-code attempts, 497 with Cloud
# Run execution conditions): the first execution of each Cloud Run job after a
# new image digest spends 30-59 s importing the image (ContainerReady
# "Imported container image in Xs"); every later start pays 1-3 s. Owner
# decision the same day: a post-deploy warm run per Cloud Run job absorbs the
# per-digest import. release.yml runs this after the deploy has verified the
# new digests and before acceptance.
#
# WHAT AN EXECUTION DOES. `--args=--self-test` replaces the job's arguments;
# the image's ENTRYPOINT stays `python -m agent_worker`, which exits 0 on that
# flag before it reads its configuration (agent_worker.__main__.self_test). So
# no lease, no Firestore client, no task state. The execution carries no
# TASK_ID or ATTEMPT_ID, and the reconciler leaves such an execution alone
# (reconciler.backends.CloudRunBackend.list_executions).
#
# This is NOT warm capacity, which the owner declined on 2026-09-30
# (invariant 1): nothing stays running. Each execution lasts as long as Cloud
# Run takes to start it, and then exits.
#
# WHICH JOBS. Terraform names them `<prefix>job-<tenant>-<profile>`
# (terraform/infra/locals.tf, job_matrix) and labels them
# managed-by=swarm-terraform and swarm-tenant=<tenant>
# (terraform/modules/cloud_run_jobs). Only a job carrying all three is warmed:
# not swarm-verify, not a job something else created. A deny-listed name
# (SHARED_DENY_LIST, scripts/lib/common.sh) is refused and never executed,
# whether listed or named on the command line, and so is any name outside the
# prefix.
#
# A FAILED WARM RUN IS A WARNING. A cold start is a slower first task, not a
# broken release, so a failed execution or a listing that failed is reported
# and the script exits 0. Only a refused name exits non-zero: that is a caller
# asking for something this script must never do.
#
# WARM_PARALLELISM (default 4) executions run at once. Every one occupies a
# whole worker shape (up to 4 CPU / 8 GiB) in the region while Cloud Run
# starts it, beside whatever tenant tasks are starting at the same time, so a
# release does not start every job in one burst.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

load_env
require_cmd gcloud
require_cmd jq

WARM_PARALLELISM="${WARM_PARALLELISM:-4}"
if [[ ! "${WARM_PARALLELISM}" =~ ^[1-9][0-9]*$ ]]; then
  die "WARM_PARALLELISM must be a positive integer, not '${WARM_PARALLELISM}'"
fi

JOB_PREFIX="$(guard_name_prefix)job-"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-warm-jobs.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

step "Warm each Cloud Run worker job once (${REGION}/${PROJECT_ID})"

# Names refused, by reason. Checked again right before each execution, so no
# path into `gcloud run jobs execute` skips it.
REFUSED=()
refusal() {
  local name="$1"
  if is_shared_resource "${name}"; then
    printf 'is on the shared deny-list (scripts/lib/common.sh); it belongs to another team'
  elif [[ "${name}" != "${JOB_PREFIX}"* ]]; then
    printf 'is not a worker job (not named %s*)' "${JOB_PREFIX}"
  fi
}

JOBS=()
if [[ "$#" -gt 0 ]]; then
  for name in "$@"; do
    JOBS+=("${name}")
  done
else
  list_rc=0
  gcloud run jobs list --project "${PROJECT_ID}" --region "${REGION}" --format=json \
    >"${WORK}/jobs.json" 2>"${WORK}/jobs.err" || list_rc=$?
  if [[ "${list_rc}" -ne 0 ]]; then
    warn "could not list Cloud Run jobs, so no job was warmed; the first task on each new digest pays the image import"
    redact <"${WORK}/jobs.err" | head -n 5 | sed 's/^/     /' >&2
    exit 0
  fi
  # Filtered here rather than with --filter on the label, for deploy.sh's
  # reason: the key has a hyphen, and a filter gcloud parses differently from
  # how it reads would list nothing.
  jq -r '.[]
         | select((.metadata.labels // {})["managed-by"] == "swarm-terraform")
         | select(((.metadata.labels // {})["swarm-tenant"] // "") != "")
         | .metadata.name' "${WORK}/jobs.json" >"${WORK}/jobs.txt"
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    # A listed name that fails the checks is not the caller's doing: say so
    # and skip it, without failing.
    reason="$(refusal "${name}")"
    if [[ -n "${reason}" ]]; then
      warn "skipped ${name}: it ${reason}"
      continue
    fi
    # The claude-code jobs are idle Cloud Run FALLBACKS since claude-code moved
    # to GKE (contract request 53, applied 2026-10-08); terraform keeps them
    # until 2026-10-15 (cloud_run_fallback_profiles in terraform/infra/locals.tf).
    # Owner 2026-10-08: do not warm them. Remove this with that list.
    case "${name}" in
      *-claude-code) info "skipped ${name}: an idle Cloud Run fallback (claude-code runs on GKE)"; continue ;;
    esac
    JOBS+=("${name}")
  done <"${WORK}/jobs.txt"
fi

TOTAL="${#JOBS[@]}"
if [[ "${TOTAL}" -eq 0 ]]; then
  warn "no per-tenant worker job (${JOB_PREFIX}*, managed-by=swarm-terraform, swarm-tenant) was listed; nothing was warmed"
  exit 0
fi

WARMED=()
FAILED=()

# Runs one batch: every name in BATCH at once, then waits for each.
BATCH=()
PIDS=()
run_batch() {
  local i name rc
  # Output files by position, never by name: a name from the command line is
  # not a safe path.
  PIDS=()
  i=0
  for name in ${BATCH[@]+"${BATCH[@]}"}; do
    info "warming ${name}"
    gcloud run jobs execute "${name}" \
      --project "${PROJECT_ID}" \
      --region "${REGION}" \
      --args=--self-test \
      --wait \
      >"${WORK}/execute.${i}.out" 2>&1 &
    PIDS+=("$!")
    i=$((i + 1))
  done
  i=0
  for name in ${BATCH[@]+"${BATCH[@]}"}; do
    rc=0
    wait "${PIDS[${i}]}" || rc=$?
    if [[ "${rc}" -eq 0 ]]; then
      ok "${name} warmed"
      WARMED+=("${name}")
    else
      warn "${name}: the warm execution failed (exit ${rc}); its first task pays the image import"
      redact <"${WORK}/execute.${i}.out" | tail -n 5 | sed 's/^/     /' >&2
      FAILED+=("${name}")
    fi
    i=$((i + 1))
  done
  BATCH=()
}

for name in "${JOBS[@]}"; do
  reason="$(refusal "${name}")"
  if [[ -n "${reason}" ]]; then
    err "refused ${name}: it ${reason}; never executed"
    REFUSED+=("${name}")
    continue
  fi
  BATCH+=("${name}")
  if [[ "${#BATCH[@]}" -ge "${WARM_PARALLELISM}" ]]; then
    run_batch
  fi
done
[[ "${#BATCH[@]}" -eq 0 ]] || run_batch

hr
info "warmed ${#WARMED[@]} of ${TOTAL} worker job(s)"
if [[ "${#FAILED[@]}" -gt 0 ]]; then
  warn "${#FAILED[@]} warm execution(s) failed: ${FAILED[*]}"
fi
if [[ "${#REFUSED[@]}" -gt 0 ]]; then
  die "refused ${#REFUSED[@]} name(s) no warm run may execute: ${REFUSED[*]}"
fi
