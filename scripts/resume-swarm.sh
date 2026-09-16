#!/usr/bin/env bash
# Resume admission after pause-swarm.sh.
#
# By default this re-enables EXACTLY the pools that pause-swarm.sh disabled, read
# from build/pause-state-<env>.json. That matters: a pool may have been paused
# separately -- by an operator draining one bad tenant, or by the quota broker --
# and a blanket "enable everything" would silently undo that decision.
#
# Usage:
#   scripts/resume-swarm.sh                 # undo the recorded pause
#   scripts/resume-swarm.sh --tenant eng    # one pool, regardless of the record
#   scripts/resume-swarm.sh --all           # every pool (asks first)

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCOPE_ALL=0
EXPLICIT=()
WAKE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)   EXPLICIT+=("tenant:$2"); shift 2 ;;
    --provider) EXPLICIT+=("provider:$2"); shift 2 ;;
    --pool)     EXPLICIT+=("$2"); shift 2 ;;
    --global)   EXPLICIT+=("global"); shift ;;
    --all)      SCOPE_ALL=1; shift ;;
    --no-wake)  WAKE=0; shift ;;
    -h|--help)  sed -n '2,14p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq curl
fs_database_exists || die "Firestore database '${FIRESTORE_DATABASE}' does not exist; nothing to resume"

STATE_FILE="${BUILD_DIR}/pause-state-${ENVIRONMENT}.json"

TARGETS=()
RESUME_SCHEDULER=1
if [[ "${#EXPLICIT[@]}" -gt 0 ]]; then
  TARGETS=("${EXPLICIT[@]}")
elif [[ "${SCOPE_ALL}" -eq 1 ]]; then
  confirm "This enables EVERY pool, including any paused for reasons this run knows nothing about." "enable-all"
  while IFS= read -r pool_id; do
    [[ -n "${pool_id}" ]] && TARGETS+=("${pool_id}")
  done < <(fs_list_docs pools | jq -r '.id')
elif [[ -f "${STATE_FILE}" ]]; then
  while IFS= read -r pool_id; do
    [[ -n "${pool_id}" ]] && TARGETS+=("${pool_id}")
  done < <(jq -r '.pools[]?' "${STATE_FILE}")
  RESUME_SCHEDULER="$(jq -r 'if .scheduler_paused then 1 else 0 end' "${STATE_FILE}")"
  info "using $(basename "${STATE_FILE}") from $(jq -r .paused_at "${STATE_FILE}")"
  ALREADY="$(jq -r '[.already_paused[]?] | join(", ")' "${STATE_FILE}")"
  [[ -z "${ALREADY}" ]] || dim "left alone (paused before this run): ${ALREADY}"
else
  die "no ${STATE_FILE}; pass --global, --tenant, --provider or --all to say what to resume"
fi

[[ "${#TARGETS[@]}" -gt 0 ]] || { ok "nothing to resume"; exit 0; }

step "Resuming admission"
resumed=0
for pool in "${TARGETS[@]}"; do
  existing="$(fs_get "pools/${pool}" | jq -c "${FS_JQ} if .fields then doc else null end")"
  if [[ "${existing}" == "null" || -z "${existing}" ]]; then
    warn "pool ${pool} does not exist; skipping"
    continue
  fi
  hard="$(jq -r '.hard_limit // 0' <<<"${existing}")"
  if [[ "${hard}" -eq 0 ]]; then
    warn "pool ${pool} has hard_limit 0; enabling it admits nothing. Set a limit first."
  fi
  fs_patch "pools/${pool}" "enabled,updated_at" \
    "$(jq -nc --arg t "$(iso_now)" '{enabled:{booleanValue:true},updated_at:{timestampValue:$t}}')"
  ok "resumed ${pool} (hard_limit ${hard})"
  resumed=$(( resumed + 1 ))
done

if [[ "${RESUME_SCHEDULER}" -eq 1 ]]; then
  step "Cloud Scheduler safety tick"
  if gcloud scheduler jobs describe "${SCHEDULER_JOB}" \
       --project "${PROJECT_ID}" --location "${REGION}" --format='value(name)' >/dev/null 2>&1; then
    gcloud scheduler jobs resume "${SCHEDULER_JOB}" \
      --project "${PROJECT_ID}" --location "${REGION}" >/dev/null
    ok "resumed ${SCHEDULER_JOB}"
  else
    info "scheduler job ${SCHEDULER_JOB} not found"
  fi
fi

if [[ "${WAKE}" -eq 1 ]]; then
  step "Waking the scheduler"
  # The scheduler drains on a Pub/Sub wake and exits; the Cloud Scheduler tick is
  # only a safety net. Publishing here means a resumed swarm starts admitting in
  # seconds rather than at the next minute boundary.
  if gcloud pubsub topics describe "${PUBSUB_TOPIC}" \
       --project "${PROJECT_ID}" --format='value(name)' >/dev/null 2>&1; then
    gcloud pubsub topics publish "${PUBSUB_TOPIC}" \
      --project "${PROJECT_ID}" \
      --message='{"reason":"resume-swarm"}' \
      --attribute="source=resume-swarm,at=$(iso_now)" >/dev/null
    ok "published a wake message to ${PUBSUB_TOPIC}"
  else
    info "topic ${PUBSUB_TOPIC} not found; the scheduler will pick work up on its next tick"
  fi
fi

if [[ -f "${STATE_FILE}" && "${#EXPLICIT[@]}" -eq 0 ]]; then
  mv "${STATE_FILE}" "${STATE_FILE%.json}.$(date -u +%Y%m%d%H%M%S).json"
fi

hr
ok "resumed ${resumed} pool(s)"
dim "watch it pick up work: scripts/status.sh --watch"
