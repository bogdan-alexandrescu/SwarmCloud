#!/usr/bin/env bash
#
# Run ONE no-op execution of each per-tenant Cloud Run worker job, so the
# image import a new digest costs is paid by the release and not by the first
# tenant task (#363).
#
#   scripts/warm-jobs.sh                 every per-tenant worker job listed
#   scripts/warm-jobs.sh JOB [JOB...]    only these (each still checked)
#   scripts/warm-jobs.sh --previous FILE [--manifest FILE]
#                                        nothing at all when the runner
#                                        images' digests did not change
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
# NOTHING TO WARM WHEN THE RUNNER DIGESTS DID NOT CHANGE (owner decision
# 2026-10-08, observer proposal H). application.yml now rebuilds only the
# images a commit changed and reuses the previous digests of the rest
# (docs/ci.md), so most releases ship the worker images unchanged, and a
# warm run on a digest every job has already started pays no import -- it
# only occupies a worker shape. Two checks, the first where it can be made:
#
#   * --previous FILE (or WARM_PREVIOUS_MANIFEST): the deployed-images manifest
#     of the release before this one. When agent-runtime-base, -browser and
#     -indexer carry the same digests there as in --manifest (default
#     build/deployed-images-<env>.json), nothing is warmed and the output says
#     so. release.yml does not pass it today -- its deploy job holds no
#     `actions: read` to fetch that artifact, and another lane is
#     restructuring release.yml -- so without it:
#   * per listed job: a job whose image is pinned by digest and which already
#     has a successful execution on that same image is skipped, by name, in
#     the output. That is the per-digest import already paid, by the previous
#     release's warm run or by a tenant task -- a premise still UNMEASURED on
#     2026-10-09 (box 94, #888): docs/ci.md gives the one read-only command
#     that settles whether an import survives a long idle gap, and what to
#     change here if it does not. A job new in this release (a new
#     tenant or profile) has none, and is warmed even when no digest changed,
#     which a comparison of manifests alone would miss. A listing of
#     executions that fails warms the job, as before. A job named on the
#     command line is warmed as asked, without this check.
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

PREVIOUS="${WARM_PREVIOUS_MANIFEST:-}"
CURRENT="${BUILD_DIR}/deployed-images-${ENVIRONMENT}.json"
NAMED=()
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --previous) [[ "$#" -ge 2 ]] || die "--previous needs a manifest file"; PREVIOUS="$2"; shift 2 ;;
    --manifest) [[ "$#" -ge 2 ]] || die "--manifest needs a manifest file"; CURRENT="$2"; shift 2 ;;
    -*)         die "unknown flag: $1" ;;
    *)          NAMED+=("$1"); shift ;;
  esac
done
# The images every worker job runs. Stated here once; deploy.sh pins the
# same three into WORKER_IMAGE_REFS.
RUNNER_IMAGES=(agent-runtime-base agent-runtime-browser agent-runtime-indexer)

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-warm-jobs.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

step "Warm each Cloud Run worker job once (${REGION}/${PROJECT_ID})"

# The digest manifest $1 records for image $2, or nothing.
digest_in() {
  jq -r --arg n "$2" '[(.images // [])[] | select(.name == $n) | .digest // empty] | if length == 1 then .[0] else "" end' "$1" 2>/dev/null || true
}

if [[ -n "${PREVIOUS}" ]]; then
  if [[ ! -f "${PREVIOUS}" || ! -f "${CURRENT}" ]]; then
    warn "cannot compare runner digests (${PREVIOUS} or ${CURRENT} is missing), so every job is warmed"
  else
    same=0
    for image in "${RUNNER_IMAGES[@]}"; do
      now="$(digest_in "${CURRENT}" "${image}")"
      before="$(digest_in "${PREVIOUS}" "${image}")"
      if [[ -n "${now}" && "${now}" == "${before}" ]]; then same=$((same + 1)); fi
    done
    if [[ "${same}" -eq "${#RUNNER_IMAGES[@]}" ]]; then
      ok "skipped warming: ${RUNNER_IMAGES[*]} carry the same digests as the previous release (${PREVIOUS}), so every job has already paid their import"
      exit 0
    fi
    info "${same} of ${#RUNNER_IMAGES[@]} runner image digest(s) unchanged since the previous release; warming"
  fi
fi

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

# Whether job $1 already has a successful execution on image $2, which must
# be pinned by digest: a tag names whatever it points at now, not what ran.
already_started() {
  local name="$1" image="$2"
  [[ "${image}" == *@sha256:* ]] || return 1
  gcloud run jobs executions list --job "${name}" --project "${PROJECT_ID}" --region "${REGION}" \
    --limit 20 --format=json </dev/null >"${WORK}/executions.json" 2>/dev/null || return 1
  jq -e --arg img "${image}" '
      any(.[]?; ((.status.succeededCount // 0) > 0)
                and any([(.spec // {}) | .. | objects | .image? // empty][]; . == $img))' \
    "${WORK}/executions.json" >/dev/null 2>&1
}

JOBS=()
SKIPPED=()
if [[ "${#NAMED[@]}" -gt 0 ]]; then
  for name in "${NAMED[@]}"; do
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
  # The job's container image beside its name: "-" when it has none.
  jq -r '.[]
         | select((.metadata.labels // {})["managed-by"] == "swarm-terraform")
         | select(((.metadata.labels // {})["swarm-tenant"] // "") != "")
         | [.metadata.name, ([(.spec.template // {}) | .. | objects | .image? // empty][0] // "-")]
         | @tsv' "${WORK}/jobs.json" >"${WORK}/jobs.txt"
  while IFS=$'\t' read -r name image; do
    [[ -n "${name}" ]] || continue
    # A listed name that fails the checks is not the caller's doing: say so
    # and skip it, without failing.
    reason="$(refusal "${name}")"
    if [[ -n "${reason}" ]]; then
      warn "skipped ${name}: it ${reason}"
      continue
    fi
    if already_started "${name}" "${image}"; then
      info "skipped ${name}: it has already run ${image##*@} successfully, so that digest's import is paid"
      SKIPPED+=("${name}")
      continue
    fi
    JOBS+=("${name}")
  done <"${WORK}/jobs.txt"
fi

TOTAL="${#JOBS[@]}"
if [[ "${TOTAL}" -eq 0 && "${#SKIPPED[@]}" -gt 0 ]]; then
  ok "skipped warming: all ${#SKIPPED[@]} worker job(s) have already run their current digest"
  exit 0
fi
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
if [[ "${#SKIPPED[@]}" -gt 0 ]]; then
  info "skipped ${#SKIPPED[@]} already started on their current digest: ${SKIPPED[*]}"
fi
if [[ "${#FAILED[@]}" -gt 0 ]]; then
  warn "${#FAILED[@]} warm execution(s) failed: ${FAILED[*]}"
fi
if [[ "${#REFUSED[@]}" -gt 0 ]]; then
  die "refused ${#REFUSED[@]} name(s) no warm run may execute: ${REFUSED[*]}"
fi
