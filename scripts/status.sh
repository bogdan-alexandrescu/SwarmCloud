#!/usr/bin/env bash
# What the swarm is doing right now, in one screen.
#
# Reads Firestore DIRECTLY rather than through the API, on purpose: the moment
# you most need this is the moment the API is unhealthy, and Firestore is the
# authoritative store anyway -- the API only reads the same documents.
#
# Prints no secrets, ever. It reads only control-plane documents and resource
# listings, never a secret payload and never an environment variable, and every
# gcloud / kubectl subprocess it runs has its output passed through redact()
# before it is parsed -- this is the command an operator runs while sharing a
# screen. redact() is a pattern filter and is best-effort; see docs/security.md.
#
# --tenant scopes the SNAPSHOT, not just the printout. It used to be applied only
# while rendering, so `status.sh --json --tenant eng` returned every tenant's
# documents -- a flag that advertised scoping and did not apply it, on output
# that gets pasted into tickets and captured in CI logs.
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
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq curl

TASK_STATES=(SUBMITTED QUEUED PARKED READY LEASED DISPATCHED STARTING RUNNING
             SUCCEEDED FAILED CANCELLED DEAD_LETTERED)

# Every gcloud listing goes through redact() before it is parsed. A Cloud Run
# service or Job description carries its whole environment block, and a
# misconfigured deployment that put a key in a plain env var would otherwise
# print it on the screen an operator is sharing. redact() is best-effort by
# construction (docs/security.md says so), which is why it is applied here and
# not relied on there.
gcloud_json() {
  local out
  if out="$("$@" --format=json 2>/dev/null | redact)"; then
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

  # ---- admin dispatch pause ----------------------------------------------
  # `control/dispatch` is the flag POST /v1/admin/dispatch/pause writes, and
  # scheduler/loop.py checks it BEFORE anything else -- so it is the first cause
  # of "work sits in READY while pools are idle", and it was the one cause this
  # screen could not show. An operator would check paused pools, then the Cloud
  # Scheduler tick, then blocked_by, and never find it.
  local control='{"dispatch_paused":false}'
  if [[ "${db_ok}" == true ]]; then
    control="$(fs_get "control/dispatch" \
      | jq -c "${FS_JQ}"' if .fields then doc else {dispatch_paused:false} end' \
      2>/dev/null || printf '{"dispatch_paused":false}')"
    [[ -n "${control}" && "${control}" != "null" ]] || control='{"dispatch_paused":false}'
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
        -o json 2>/dev/null | redact | jq -c '[.items[] | {
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
    --argjson control "${control}" \
    '{
      at:$at, project:$project, region:$region, environment:$environment,
      database:{name:$database, exists:$db_ok},
      cluster:$cluster,
      control:$control,
      tasks:$states, leases:$leases, pools:$pools, quota:$quota, tenants:$tenants,
      cloud_run:{services:$services, jobs:$jobs, executions:$executions},
      gke:$gke
    }'
}

# Narrow a snapshot to one tenant. Applied to the SNAPSHOT, before anything is
# printed or serialised, so `--json --tenant eng` and the human rendering scope
# identically -- the JSON is what ends up in a ticket or a CI log.
#
# The per-state task counts stay platform-wide and are labelled as such: they
# come from Firestore aggregation queries with no tenant predicate, and quietly
# presenting a platform-wide number as a tenant's own would be worse than saying
# which it is.
scope_to_tenant() {
  local snapshot="$1" tenant="$2"
  [[ -n "${tenant}" ]] || { printf '%s' "${snapshot}"; return 0; }
  jq -c --arg t "${tenant}" '
    .scope = {tenant: $t, task_counts_are_platform_wide: true}
    | .leases  |= map(select(.tenant_id == $t))
    | .quota   |= map(select(.tenant_id == $t))
    | .tenants |= map(select((.tenant_id // .id) == $t))
    # A tenant sees its own pools plus the shared ones it admits against --
    # global, resource:, runner:, backend: and provider: are the pools that can
    # block this tenant, so hiding them would make a blocked task unexplainable.
    | .pools   |= map(select(
        (((.id | startswith("tenant:")) or (.id | contains(":tenant:"))) | not)
        or (.id == ("tenant:" + $t))
        or (.id | endswith(":tenant:" + $t))))
    | .gke.pods |= map(select(.tenant == $t))
    | .cloud_run.jobs |= map(select((.metadata.name // "") | contains("-" + $t + "-") or endswith("-" + $t)))
  ' <<<"${snapshot}"
}

render() {
  local snapshot="$1"

  # No per-section tenant filtering here: scope_to_tenant() already narrowed the
  # snapshot, so this function renders exactly what --json would print. Two
  # filters, one of which only ran on the human path, is how --json came to
  # ignore --tenant in the first place.
  printf '\n%sAgent swarm%s  %s / %s / %s   %s\n' \
    "${C_BOLD}" "${C_RESET}" \
    "$(jq -r .project <<<"${snapshot}")" \
    "$(jq -r .environment <<<"${snapshot}")" \
    "$(jq -r .region <<<"${snapshot}")" \
    "$(jq -r .at <<<"${snapshot}")"

  if [[ "$(jq -r '.database.exists' <<<"${snapshot}")" != "true" ]]; then
    warn "Firestore database '$(jq -r .database.name <<<"${snapshot}")' does not exist yet -- run 'make infra'"
  fi

  if [[ -n "${TENANT_FILTER}" ]]; then
    dim "  scoped to tenant '${TENANT_FILTER}'; task counts below are platform-wide"
  fi

  if [[ "$(jq -r 'if .control.dispatch_paused == true then "true" else "false" end' <<<"${snapshot}")" == "true" ]]; then
    warn "DISPATCH IS PAUSED platform-wide (control/dispatch), by $(jq -r '.control.updated_by // "?"' <<<"${snapshot}")"
    warn "the scheduler checks this before anything else, so nothing will be admitted at all"
    dim  "  resume with: scripts/api.sh POST /admin/dispatch/resume '{}'"
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
  jq -r --argjson now "${now_epoch}" '
    def epoch: (. // "1970-01-01T00:00:00Z") | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601? // 0;
    .leases
    | if length == 0 then ["  none active"]
      else
        [ "  active: \(length)" ]
        + [ "  expired (reconciler will reclaim): \([ .[] | select((.expires_at|epoch) < $now) ] | length)" ]
        + [ "  dispatch overdue: \([ .[] | select(.state == "LEASED" and ((.dispatch_deadline|epoch) < $now)) ] | length)" ]
        + ( [ .[] | "    \(.lease_id // .id)  task=\(.task_id)  tenant=\(.tenant_id)  gen=\(.generation)  units=\(.units)  \(.state)" ] | .[0:10] )
      end
    | .[]' <<<"${snapshot}" >&2

  step "Concurrency pools"
  # `effective_limit` comes from the FS_JQ prelude in lib/common.sh, which is the
  # one jq restatement of swarm_common.models.SlotPool.effective_limit and is
  # asserted against the frozen model by scripts/lib/check-contract-parity.sh.
  # This used to carry a second inline copy that had lost the max(0, ...) floor,
  # so a pool with a negative cap printed a negative limit while the scheduler
  # computed zero -- two different answers to the question this screen exists to
  # answer.
  jq -r "${FS_JQ}"'
    .pools
    | sort_by(.id)
    | if length == 0 then ["  no pools configured yet"]
      else
        [ "  " + ("POOL" | . + " " * (34 - length)) + "ACTIVE  LIMIT  HARD  STATE" ]
        + [ .[]
            | . as $p
            | ($p | effective_limit) as $eff
            | "  " + (($p.id) | .[0:33] | . + " " * (34 - length))
              + (($p.active // 0)|tostring | . + " " * (8 - length))
              + (($eff|tostring) | . + " " * (7 - length))
              + ((($p.hard_limit // 0)|tostring) | . + " " * (6 - length))
              # `.enabled == false`, NOT `.enabled // true`. jq'"'"'s alternative
              # operator treats FALSE as absent, so `false // true` is `true` --
              # a paused pool rendered as "open", which is the one thing this
              # column exists to show and the first cause in the "nothing is
              # running" runbook.
              + (if ($p.enabled == false) then "PAUSED" else "open" end)
          ]
      end
    | .[]' <<<"${snapshot}" >&2

  step "Provider quota"
  jq -r '
    .quota
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
    jq -r '
      .gke.pods
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
    [ (if (.control.dispatch_paused == true) then "  dispatch is PAUSED by an admin (control/dispatch) -- the scheduler admits nothing at all" else empty end),
      (if (.tasks.PARKED // 0) > 0 then "  \(.tasks.PARKED) task(s) parked -- see scripts/status.sh --json | jq .tasks, and docs/quota-management.md" else empty end),
      (if ([.pools[] | select(.enabled == false)] | length) > 0 then "  \([.pools[] | select(.enabled == false) | .id] | join(", ")) PAUSED -- resume with scripts/resume-swarm.sh" else empty end),
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
  snapshot="$(scope_to_tenant "${snapshot}" "${TENANT_FILTER}")"
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
