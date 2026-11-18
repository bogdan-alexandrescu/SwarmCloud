#!/usr/bin/env bash
# Stop admitting new work. Running tasks are left alone.
#
# Pausing flips `enabled=false` on slot pools and pauses the Cloud Scheduler
# safety tick. It does NOT kill anything that is already running: a running agent
# holds a lease, has a workspace, and is mid-way through work that checkpointing
# can resume but that killing would waste. Use scripts/status.sh to watch the
# RUNNING count drain, or --drain to wait for it here.
#
# Every change is recorded in build/pause-state-<env>.json so that resume-swarm.sh
# re-enables exactly what this paused -- and never re-enables a pool that was
# already deliberately paused by someone else.
#
# Usage:
#   scripts/pause-swarm.sh                       # global pool + scheduler tick
#   scripts/pause-swarm.sh --tenant eng          # one tenant only
#   scripts/pause-swarm.sh --provider anthropic  # one provider only
#   scripts/pause-swarm.sh --all --drain         # everything, then wait

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCOPE_GLOBAL=1
SCOPE_ALL=0
TENANTS=()
PROVIDERS=()
DRAIN=0
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-1800}"
PAUSE_SCHEDULER=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)        TENANTS+=("$2"); SCOPE_GLOBAL=0; shift 2 ;;
    --provider)      PROVIDERS+=("$2"); SCOPE_GLOBAL=0; shift 2 ;;
    --all)           SCOPE_ALL=1; shift ;;
    --drain)         DRAIN=1; shift ;;
    --drain-timeout) DRAIN_TIMEOUT="$2"; shift 2 ;;
    --keep-scheduler) PAUSE_SCHEDULER=0; shift ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq curl
require_fs_database pause

STATE_FILE="${BUILD_DIR}/pause-state-${ENVIRONMENT}.json"

# Which pools to touch.
TARGETS=()
if [[ "${SCOPE_ALL}" -eq 1 ]]; then
  # A plain `done < <(...)` never checks the process substitution's exit
  # status -- a denied or expired pools listing would just look like EOF, and
  # the loop would exhaust TARGETS with no line above ever saying why. Capture
  # it as a variable first so a real failure hits `|| die` instead.
  pool_ids="$(fs_list_docs pools | jq -r '.id')" \
    || die "could not list pools; see the Firestore error above -- this is a failed listing, not an empty pool set"
  while IFS= read -r pool_id; do
    [[ -n "${pool_id}" ]] && TARGETS+=("${pool_id}")
  done <<<"${pool_ids}"
else
  [[ "${SCOPE_GLOBAL}" -eq 1 ]] && TARGETS+=("global")
  for t in ${TENANTS[@]+"${TENANTS[@]}"}; do TARGETS+=("tenant:${t}"); done
  for p in ${PROVIDERS[@]+"${PROVIDERS[@]}"}; do TARGETS+=("provider:${p}"); done
fi
[[ "${#TARGETS[@]}" -gt 0 ]] || die "nothing selected to pause"

step "Pausing admission"
info "pools: ${TARGETS[*]}"

paused='[]'
skipped='[]'
for pool in "${TARGETS[@]}"; do
  # Firestore PATCH creates a document that does not exist. A pool document
  # created here would have no hard_limit, which reads as limit 0 -- i.e. a
  # permanent block that resume could not undo. So: never patch what is absent.
  existing="$(fs_get "pools/${pool}" | jq -c "${FS_JQ} if .fields then doc else null end")"
  if [[ "${existing}" == "null" || -z "${existing}" ]]; then
    warn "pool ${pool} does not exist; skipping (refusing to create a pool with no limit)"
    skipped="$(jq -c --arg p "${pool}" '. + [$p]' <<<"${skipped}")"
    continue
  fi

  was_enabled="$(jq -r 'if (.enabled == false) then "false" else "true" end' <<<"${existing}")"
  if [[ "${was_enabled}" == "false" ]]; then
    info "pool ${pool} was already paused; leaving it (resume will not touch it)"
    skipped="$(jq -c --arg p "${pool}" '. + [$p]' <<<"${skipped}")"
    continue
  fi

  fs_patch "pools/${pool}" "enabled,updated_at" \
    "$(jq -nc --arg t "$(iso_now)" '{enabled:{booleanValue:false},updated_at:{timestampValue:$t}}')"
  active="$(jq -r '.active // 0' <<<"${existing}")"
  ok "paused ${pool} (${active} lease(s) still holding capacity)"
  paused="$(jq -c --arg p "${pool}" '. + [$p]' <<<"${paused}")"
done

if [[ "${PAUSE_SCHEDULER}" -eq 1 ]]; then
  step "Cloud Scheduler safety tick"
  # `if cmd; then ... else ...` suppresses set -e for the condition itself, so
  # this cannot rely on the script dying -- it must tell a real NOT_FOUND apart
  # from an expired session, a missing cloudscheduler.jobs.get, a disabled API
  # or a job that exists in some other location, all of which describe fails
  # on identically. Capture stderr instead of throwing it at /dev/null.
  scheduler_err_file="$(mktemp "${TMPDIR:-/tmp}/swarm-scheduler.XXXXXX")"
  if gcloud scheduler jobs describe "${SCHEDULER_JOB}" \
       --project "${PROJECT_ID}" --location "${REGION}" --format='value(name)' \
       >/dev/null 2>"${scheduler_err_file}"; then
    rm -f "${scheduler_err_file}"
    gcloud scheduler jobs pause "${SCHEDULER_JOB}" \
      --project "${PROJECT_ID}" --location "${REGION}" >/dev/null
    ok "paused ${SCHEDULER_JOB}"
  else
    scheduler_err="$(cat "${scheduler_err_file}")"
    rm -f "${scheduler_err_file}"
    # Exits here, naming it, if the session itself is the problem.
    die_if_auth_failure "${scheduler_err}"
    if grep -qi 'not_found\|not found' <<<"${scheduler_err}"; then
      info "scheduler job ${SCHEDULER_JOB} not found; nothing to pause"
      PAUSE_SCHEDULER=0
    else
      err "could not look up scheduler job ${SCHEDULER_JOB}; this is NOT confirmation it is absent"
      printf '%s\n' "${scheduler_err}" | redact | head -n 5 | sed 's/^/     /' >&2
      die "check the account, region (${REGION}) and project (${PROJECT_ID}) before assuming the safety tick was already paused"
    fi
  fi
fi

jq -n --arg at "$(iso_now)" --arg env "${ENVIRONMENT}" \
      --argjson pools "${paused}" --argjson skipped "${skipped}" \
      --argjson scheduler "$([[ "${PAUSE_SCHEDULER}" -eq 1 ]] && echo true || echo false)" \
      --arg job "${SCHEDULER_JOB}" \
   '{paused_at:$at, environment:$env, pools:$pools, already_paused:$skipped,
     scheduler_paused:$scheduler, scheduler_job:$job}' >"${STATE_FILE}"
ok "recorded ${STATE_FILE}"

if [[ "${DRAIN}" -eq 1 ]]; then
  step "Draining"
  deadline=$(( $(date -u +%s) + DRAIN_TIMEOUT ))
  while :; do
    holding=0
    for state in LEASED DISPATCHED STARTING RUNNING; do
      n="$(fs_count tasks state EQUAL "${state}")"
      holding=$(( holding + n ))
    done
    if [[ "${holding}" -eq 0 ]]; then
      ok "all tasks have released capacity"
      break
    fi
    if [[ "$(date -u +%s)" -ge "${deadline}" ]]; then
      warn "still ${holding} task(s) holding capacity after ${DRAIN_TIMEOUT}s; not waiting further"
      break
    fi
    info "${holding} task(s) still holding capacity..."
    sleep 10
  done
fi

hr
ok "admission paused"
dim "running work continues; resume with: scripts/resume-swarm.sh"
