#!/usr/bin/env bash
# What the swarm is doing right now, in one screen.
#
# Reads Firestore DIRECTLY rather than through the API, on purpose: the moment
# you most need this is the moment the API is unhealthy, and Firestore is the
# authoritative store anyway -- the API only reads the same documents.
#
# Prints no secrets, ever. No environment variables, no secret payloads, no
# tokens; every subprocess's output goes through redact().
#
# Usage: scripts/status.sh [--json] [--watch [SECONDS]] [--tenant ID] [--no-gke] [--no-run]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

AS_JSON=0
WATCH=0
WATCH_SECONDS=5
TENANT_FILTER=""
WITH_GKE=1
WITH_RUN=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)   AS_JSON=1; shift ;;
    --watch)  WATCH=1
              if [[ "${2:-}" =~ ^[0-9]+$ ]]; then WATCH_SECONDS="$2"; shift 2; else shift; fi ;;
    --tenant) TENANT_FILTER="$2"; shift 2 ;;
    --no-gke) WITH_GKE=0; shift ;;
    --no-run) WITH_RUN=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq curl

TASK_STATES=(SUBMITTED QUEUED PARKED READY LEASED DISPATCHED STARTING RUNNING
             SUCCEEDED FAILED CANCELLED DEAD_LETTERED)

gcloud_json() {
  local out
  if out="$("$@" --format=json 2>/dev/null)"; then
    printf '%s' "${out:-[]}"
  else
    printf '[]'
  fi
}

collect() {
  local db_ok=false
  fs_database_exists && db_ok=true

  # ---- tasks by state -----------------------------------------------------
  local states='{}' state count
  if [[ "${db_ok}" == true ]]; then
    for state in "${TASK_STATES[@]}"; do
      count="$(fs_count tasks state EQUAL "${state}")"
      states="$(jq -c --arg s "${state}" --argjson n "${count:-0}" '. + {($s): $n}' <<<"${states}")"
    done
  fi

  # ---- leases -------------------------------------------------------------
  local leases='[]'
  if [[ "${db_ok}" == true ]]; then
    local where
    where="$(fs_null_filter released_at IS_NULL)"
    leases="$(fs_query leases "${where}" 500 | jq -sc "${FS_JQ} [ .[] | doc ]" || printf '[]')"
  fi

  # ---- pools, quota, tenants ---------------------------------------------
  local pools='[]' quota='[]' tenants='[]'
  if [[ "${db_ok}" == true ]]; then
    pools="$(fs_list_docs pools | jq -sc '.' || printf '[]')"
    quota="$(fs_list_docs quota | jq -sc '.' || printf '[]')"
    tenants="$(fs_list_docs tenants | jq -sc '.' || printf '[]')"
  fi

  # ---- Cloud Run ----------------------------------------------------------
  local services='[]' executions='[]' jobs='[]'
  if [[ "${WITH_RUN}" -eq 1 ]]; then
    services="$(gcloud_json gcloud run services list \
      --project "${PROJECT_ID}" --region "${REGION}" \
      --filter='metadata.labels.managed-by=swarm-terraform OR metadata.name~^swarm-')"
    jobs="$(gcloud_json gcloud run jobs list \
      --project "${PROJECT_ID}" --region "${REGION}" \
      --filter='metadata.name~^swarm-')"
    executions="$(gcloud_json gcloud run jobs executions list \
      --project "${PROJECT_ID}" --region "${REGION}" --limit 200)"
  fi

  # ---- GKE ----------------------------------------------------------------
  local gke='{"reachable":false,"pods":[]}'
  if [[ "${WITH_GKE}" -eq 1 ]]; then
    local kb pods
    kb="$(kubectl_bin 2>/dev/null || true)"
    if [[ -n "${kb}" ]] && kube_context_is_swarm \
       && "${kb}" get --raw='/readyz' >/dev/null 2>&1; then
      pods="$("${kb}" get pods -A -l managed-by=swarm-terraform \
        -o json 2>/dev/null | jq -c '[.items[] | {
          namespace: .metadata.namespace,
          name: .metadata.name,
          phase: .status.phase,
          task: (.metadata.labels["swarm-task-id"] // null),
          tenant: (.metadata.labels["swarm-tenant"] // null),
          restarts: ([.status.containerStatuses[]?.restartCount] | add // 0)
        }]' || printf '[]')"
      gke="$(jq -nc --argjson pods "${pods:-[]}" '{reachable:true, pods:$pods}')"
    fi
  fi

  jq -nc \
    --arg at "$(iso_now)" \
    --arg project "${PROJECT_ID}" \
    --arg region "${REGION}" \
    --arg environment "${ENVIRONMENT}" \
    --arg database "${FIRESTORE_DATABASE}" \
    --arg cluster "${GKE_CLUSTER}" \
    --argjson db_ok "${db_ok}" \
    --argjson states "${states}" \
    --argjson leases "${leases}" \
    --argjson pools "${pools}" \
    --argjson quota "${quota}" \
    --argjson tenants "${tenants}" \
    --argjson services "${services}" \
    --argjson jobs "${jobs}" \
    --argjson executions "${executions}" \
    --argjson gke "${gke}" \
    '{
      at:$at, project:$project, region:$region, environment:$environment,
      database:{name:$database, exists:$db_ok},
      cluster:$cluster,
      tasks:$states, leases:$leases, pools:$pools, quota:$quota, tenants:$tenants,
      cloud_run:{services:$services, jobs:$jobs, executions:$executions},
      gke:$gke
    }'
}

render() {
  local snapshot="$1"
  local filter="${TENANT_FILTER}"

  printf '\n%sAgent swarm%s  %s / %s / %s   %s\n' \
    "${C_BOLD}" "${C_RESET}" \
    "$(jq -r .project <<<"${snapshot}")" \
    "$(jq -r .environment <<<"${snapshot}")" \
    "$(jq -r .region <<<"${snapshot}")" \
    "$(jq -r .at <<<"${snapshot}")"

  if [[ "$(jq -r '.database.exists' <<<"${snapshot}")" != "true" ]]; then
    warn "Firestore database '$(jq -r .database.name <<<"${snapshot}")' does not exist yet -- run 'make infra'"
  fi

  step "Tasks"
  jq -r '
    .tasks as $t
    | [ "  demand-free : QUEUED \($t.QUEUED // 0)   PARKED \($t.PARKED // 0)   READY \($t.READY // 0)   SUBMITTED \($t.SUBMITTED // 0)",
        "  holding cap : LEASED \($t.LEASED // 0)   DISPATCHED \($t.DISPATCHED // 0)   STARTING \($t.STARTING // 0)   RUNNING \($t.RUNNING // 0)",
        "  terminal    : SUCCEEDED \($t.SUCCEEDED // 0)   FAILED \($t.FAILED // 0)   CANCELLED \($t.CANCELLED // 0)   DEAD_LETTERED \($t.DEAD_LETTERED // 0)" ]
    | .[]' <<<"${snapshot}" >&2
  local holding
  holding="$(jq -r '[.tasks.LEASED, .tasks.DISPATCHED, .tasks.STARTING, .tasks.RUNNING] | map(. // 0) | add' <<<"${snapshot}")"
  dim "  ${holding} task(s) hold capacity; QUEUED/PARKED/READY cost nothing"

  step "Leases"
  local now_epoch
  now_epoch="$(date -u +%s)"
  jq -r --arg tenant "${filter}" --argjson now "${now_epoch}" '
    def epoch: (. // "1970-01-01T00:00:00Z") | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601? // 0;
    .leases
    | map(select($tenant == "" or .tenant_id == $tenant))
    | if length == 0 then ["  none active"]
      else
        [ "  active: \(length)" ]
        + [ "  expired (reconciler will reclaim): \([ .[] | select((.expires_at|epoch) < $now) ] | length)" ]
        + [ "  dispatch overdue: \([ .[] | select(.state == "LEASED" and ((.dispatch_deadline|epoch) < $now)) ] | length)" ]
        + ( [ .[] | "    \(.lease_id // .id)  task=\(.task_id)  tenant=\(.tenant_id)  gen=\(.generation)  units=\(.units)  \(.state)" ] | .[0:10] )
      end
    | .[]' <<<"${snapshot}" >&2

  step "Concurrency pools"
  jq -r --arg tenant "${filter}" '
    def bar: (if . > 20 then 20 else . end) as $n | ("#" * $n);
    .pools
    | map(select($tenant == "" or (.id | startswith("tenant:" + $tenant) or (contains(":tenant:" + $tenant)) or (startswith("tenant:") | not))))
    | sort_by(.id)
    | if length == 0 then ["  no pools configured yet"]
      else
        [ "  " + ("POOL" | . + " " * (34 - length)) + "ACTIVE  LIMIT  HARD  STATE" ]
        + [ .[]
            | . as $p
            | ( [ ($p.hard_limit // 0) ]
                + (if ($p.adaptive_target // null) == null then [] else [$p.adaptive_target] end)
                + (if ($p.quota_derived_limit // null) == null then [] else [$p.quota_derived_limit] end)
                | min ) as $eff
            | "  " + (($p.id) | .[0:33] | . + " " * (34 - length))
              + (($p.active // 0)|tostring | . + " " * (8 - length))
              + (($eff|tostring) | . + " " * (7 - length))
              + ((($p.hard_limit // 0)|tostring) | . + " " * (6 - length))
              + (if ($p.enabled // true) then "open" else "PAUSED" end)
          ]
      end
    | .[]' <<<"${snapshot}" >&2

  step "Provider quota"
  jq -r --arg tenant "${filter}" '
    .quota
    | map(select($tenant == "" or .tenant_id == $tenant))
    | sort_by(.id)
    | if length == 0 then ["  no provider state recorded yet"]
      else [ .[] | "  \(.provider // "?"):\(.tenant_id // "?")  \(.state // "UNKNOWN")"
                   + "  target=\(.adaptive_target // "-")  hard=\(.configured_hard_max // "-")"
                   + "  429s=\(.rate_limit_count // 0)"
                   + (if (.cooldown_until // null) != null then "  cooldown_until=\(.cooldown_until)" else "" end) ]
      end
    | .[]' <<<"${snapshot}" >&2

  step "Cloud Run"
  jq -r '
    .cloud_run.services
    | if length == 0 then ["  no swarm services deployed"]
      else [ .[] | "  \(.metadata.name)  "
             + ( [ .status.conditions[]? | select(.type=="Ready") | .status ] | first // "?" | if . == "True" then "ready" else "NOT READY" end )
             + "  rev=\(.status.latestReadyRevisionName // "-")" ]
      end
    | .[]' <<<"${snapshot}" >&2
  jq -r '
    .cloud_run.executions as $e
    | ( [ $e[] | select((.status.completionTime // null) == null) ] | length ) as $running
    | ( [ $e[] | select((.status.completionTime // null) != null) ] | length ) as $done
    | "  job executions: \($running) active, \($done) completed (last \($e|length) listed)"' <<<"${snapshot}" >&2
  jq -r '
    .cloud_run.jobs
    | "  per-tenant job resources: \(length)"' <<<"${snapshot}" >&2

  step "GKE (${GKE_CLUSTER})"
  if [[ "$(jq -r '.gke.reachable' <<<"${snapshot}")" == "true" ]]; then
    jq -r --arg tenant "${filter}" '
      .gke.pods
      | map(select($tenant == "" or .tenant == $tenant))
      | if length == 0 then ["  no swarm workloads"]
        else
          ( group_by(.phase) | map("  \(.[0].phase): \(length)") )
          + ( [ .[] | select(.restarts > 0) | "  RESTARTED \(.restarts)x  \(.namespace)/\(.name)" ] )
        end
      | .[]' <<<"${snapshot}" >&2
  else
    dim "  not connected to the swarm cluster (context: $(kube_current_context || echo none))"
    dim "  run scripts/configure-kubectl.sh, or pass --no-gke"
  fi

  step "Attention"
  local attention
  attention="$(jq -r --argjson now "${now_epoch}" '
    def epoch: (. // "1970-01-01T00:00:00Z") | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601? // 0;
    [ (if (.tasks.PARKED // 0) > 0 then "  \(.tasks.PARKED) task(s) parked -- see scripts/status.sh --json | jq .tasks, and docs/quota-management.md" else empty end),
      (if ([.pools[] | select((.enabled // true) | not)] | length) > 0 then "  \([.pools[] | select((.enabled // true) | not) | .id] | join(", ")) PAUSED -- resume with scripts/resume-swarm.sh" else empty end),
      (if ([.leases[] | select((.expires_at|epoch) < $now)] | length) > 0 then "  \([.leases[] | select((.expires_at|epoch) < $now)] | length) expired lease(s) still holding capacity" else empty end),
      (if ([.quota[] | select(.state == "EXHAUSTED" or .state == "COOLDOWN")] | length) > 0 then "  provider(s) throttled: \([.quota[] | select(.state == "EXHAUSTED" or .state == "COOLDOWN") | .id] | join(", "))" else empty end),
      (if ([.gke.pods[] | select(.restarts > 0)] | length) > 0 then "  pod restarts observed -- the platform promises no restarts; investigate before dismissing" else empty end)
    ] | .[]' <<<"${snapshot}")"
  if [[ -z "${attention}" ]]; then
    ok "nothing needs attention"
  else
    printf '%s\n' "${attention}" >&2
  fi
  printf '\n' >&2
}

run_once() {
  local snapshot
  snapshot="$(collect)"
  if [[ "${AS_JSON}" -eq 1 ]]; then
    printf '%s\n' "${snapshot}" | jq .
  else
    render "${snapshot}"
  fi
}

if [[ "${WATCH}" -eq 1 ]]; then
  trap 'printf "\n" >&2; exit 0' INT
  while :; do
    [[ "${AS_JSON}" -eq 1 ]] || printf '\033[2J\033[H'
    run_once
    sleep "${WATCH_SECONDS}"
  done
else
  run_once
fi
