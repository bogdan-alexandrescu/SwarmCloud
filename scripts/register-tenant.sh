#!/usr/bin/env bash
# Register a tenant: a Google group, or one person as a personal fallback tenant.
#
# A tenant is the unit of isolation in this platform, so registering one means
# creating every boundary at once -- there is no "half-registered" state worth
# having:
#
#   * a dedicated Google service account            (identity)
#   * Firestore access scoped to the swarm database (control-plane data)
#   * GCS access conditioned on the tenant's own object prefix, so tenant A's
#     credentials cannot read tenant B's artifacts even by guessing the path
#   * Secret Manager access to that tenant's provider keys only
#   * a Kubernetes namespace + workload-identity binding for the GKE path
#   * a tenants/<id> document and a tenant:<id> slot pool
#
# Everything is idempotent: re-running updates limits and fills in whatever is
# missing.
#
# Usage:
#   scripts/register-tenant.sh --group eng@saga.xyz
#   scripts/register-tenant.sh --user alice@saga.xyz
#   scripts/register-tenant.sh --group eng@saga.xyz --providers anthropic,openai \
#                              --max-active 40 --capacity-units 80 --budget 2000
#   scripts/register-tenant.sh --group eng@saga.xyz --dry-run

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

GROUP=""
USER_EMAIL=""
TENANT_ID=""
PROVIDERS_CSV=""
MAX_ACTIVE="${DEFAULT_TENANT_MAX_ACTIVE:-20}"
CAPACITY_UNITS="${DEFAULT_TENANT_CAPACITY_UNITS:-40}"
BUDGET=""
DISPLAY_NAME=""
DRY_RUN=0
SKIP_K8S=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --group|-g)        GROUP="$2"; shift 2 ;;
    --user|-u)         USER_EMAIL="$2"; shift 2 ;;
    --tenant|-t)       TENANT_ID="$2"; shift 2 ;;
    --providers|-p)    PROVIDERS_CSV="$2"; shift 2 ;;
    --max-active)      MAX_ACTIVE="$2"; shift 2 ;;
    --capacity-units)  CAPACITY_UNITS="$2"; shift 2 ;;
    --budget)          BUDGET="$2"; shift 2 ;;
    --display-name)    DISPLAY_NAME="$2"; shift 2 ;;
    --skip-k8s)        SKIP_K8S=1; shift ;;
    --dry-run|-n)      DRY_RUN=1; shift ;;
    -h|--help)         sed -n '2,28p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq curl

# --- identity -> tenant id, exactly as swarm_common.identity does it ---------
slugify() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//'; }

KIND=""
PRINCIPAL=""
if [[ -n "${GROUP}" ]]; then
  KIND="group"
  PRINCIPAL="${GROUP}"
  TENANT_ID="${TENANT_ID:-$(slugify "${GROUP%%@*}")}"
elif [[ -n "${USER_EMAIL}" ]]; then
  KIND="user"
  PRINCIPAL="${USER_EMAIL}"
  # Personal tenants are namespaced so they can never collide with a group.
  TENANT_ID="${TENANT_ID:-u-$(slugify "${USER_EMAIL%%@*}")}"
else
  die "give either --group <group@saga.xyz> or --user <person@saga.xyz>"
fi

[[ -n "${TENANT_ID}" ]] || die "could not derive a tenant id from ${PRINCIPAL}"
case "${TENANT_ID}" in
  *[!a-z0-9-]*) die "derived tenant id '${TENANT_ID}' is not a valid slug" ;;
esac

DOMAIN="${PRINCIPAL##*@}"
ALLOWED_DOMAINS="${ALLOWED_DOMAINS:-saga.xyz}"
case ",${ALLOWED_DOMAINS}," in
  *",${DOMAIN},"*) ;;
  *) die "${PRINCIPAL} is outside the allowed hosted domain(s) (${ALLOWED_DOMAINS}); authentication would reject it anyway" ;;
esac

GSA_ID="swarm-t-${TENANT_ID}"
GSA_ID="${GSA_ID:0:30}"
GSA_EMAIL="${GSA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
NAMESPACE="swarm-${TENANT_ID}"
KSA="swarm-worker"
GCS_PREFIX="tenants/${TENANT_ID}"

PROVIDERS=()
if [[ -n "${PROVIDERS_CSV}" ]]; then
  IFS=',' read -r -a PROVIDERS <<<"${PROVIDERS_CSV}"
fi

step "Tenant"
info "id             ${TENANT_ID}"
info "kind           ${KIND}"
info "principal      ${PRINCIPAL}"
info "service acct   ${GSA_EMAIL}"
info "namespace      ${NAMESPACE}"
info "gcs prefix     gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/"
info "limits         max_active=${MAX_ACTIVE} capacity_units=${CAPACITY_UNITS}"
[[ "${#PROVIDERS[@]}" -eq 0 ]] || info "providers      ${PROVIDERS[*]}"
[[ "${DRY_RUN}" -eq 1 ]] && warn "DRY RUN: nothing will be created"

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    dim "  would run: $*"
    return 0
  fi
  "$@"
}

# --- 1. the group must actually exist ---------------------------------------
if [[ "${KIND}" == "group" ]]; then
  step "Cloud Identity"
  if gcloud identity groups describe "${GROUP}" --format='value(name)' >/dev/null 2>&1; then
    ok "group ${GROUP} exists"
  else
    warn "could not verify ${GROUP} via Cloud Identity (it may not exist, or you may lack groups.read)"
    warn "if the group does not exist, every member falls back to a personal tenant and this registration goes unused"
  fi
fi

# --- 2. service account -------------------------------------------------------
step "Service account"
if gcloud iam service-accounts describe "${GSA_EMAIL}" \
     --project "${PROJECT_ID}" --format='value(email)' >/dev/null 2>&1; then
  ok "${GSA_EMAIL} exists"
else
  run gcloud iam service-accounts create "${GSA_ID}" \
    --project "${PROJECT_ID}" \
    --display-name "${DISPLAY_NAME:-swarm tenant ${TENANT_ID}}" \
    --description "Agent swarm workload identity for tenant ${TENANT_ID} (${PRINCIPAL})"
  ok "created ${GSA_EMAIL}"
fi

# --- 3. IAM -------------------------------------------------------------------
step "IAM"

# Firestore, conditioned on the swarm database. Without the condition this would
# also grant access to the (default) database, which belongs to other teams in
# this shared project.
#
# The role is the NARROWED CUSTOM ROLE terraform builds, never roles/datastore.user.
# This script and terraform/modules/tenancy must grant identical authority, or a
# tenant onboarded here ends up more privileged than one terraform created.
#
# roles/datastore.user carries two permissions the custom role deliberately drops,
# and Firestore IAM cannot scope below the database, so both apply to EVERY
# tenant's documents, not just this tenant's:
#
#   datastore.entities.delete -- a hostile worker could delete another tenant's
#   tasks, leases and attempts, and the pool documents the whole platform admits
#   against. Nothing in the worker path deletes a document.
#
#   datastore.entities.list -- queries. Without it a document can only be fetched
#   by an id already known, and ids are `<prefix>_<20 hex>`.
FIRESTORE_ROLE="projects/${PROJECT_ID}/roles/swarmTenantWorkerFirestore${CUSTOM_ROLE_SUFFIX:+_${CUSTOM_ROLE_SUFFIX}}"

if ! gcloud iam roles describe "swarmTenantWorkerFirestore${CUSTOM_ROLE_SUFFIX:+_${CUSTOM_ROLE_SUFFIX}}" \
     --project "${PROJECT_ID}" --format='value(name)' >/dev/null 2>&1; then
  die "custom role ${FIRESTORE_ROLE} does not exist. Run \`make infra\` first:
  terraform/modules/tenancy creates it, and granting roles/datastore.user instead
  would hand this tenant entities.delete and entities.list over every other
  tenant's control-plane documents."
fi

run gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${GSA_EMAIL}" \
  --role "${FIRESTORE_ROLE}" \
  --condition "expression=resource.name.endsWith('/databases/${FIRESTORE_DATABASE}'),title=swarm_db_only,description=Only the swarm Firestore database" \
  --quiet >/dev/null
ok "${FIRESTORE_ROLE##*/} (no deletes, no queries; scoped to the ${FIRESTORE_DATABASE} database)"

# GCS, conditioned on this tenant's own prefix. This is the boundary that stops
# a compromised worker from reading another tenant's artifacts by guessing a path.
if gcloud storage buckets describe "gs://${ARTIFACT_BUCKET}" \
     --project "${PROJECT_ID}" --format='value(name)' >/dev/null 2>&1; then
  run gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
    --member "serviceAccount:${GSA_EMAIL}" \
    --role roles/storage.objectAdmin \
    --condition "expression=resource.name.startsWith('projects/_/buckets/${ARTIFACT_BUCKET}/objects/${GCS_PREFIX}/'),title=tenant_prefix_only,description=Only this tenant's object prefix" \
    >/dev/null
  ok "storage.objectAdmin (only gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/)"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    printf 'tenant %s registered %s\n' "${TENANT_ID}" "$(iso_now)" \
      | gcloud storage cp - "gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/.tenant" \
        --project "${PROJECT_ID}" >/dev/null 2>&1 || warn "could not write the prefix marker object"
  fi
else
  warn "artifact bucket gs://${ARTIFACT_BUCKET} does not exist yet; run 'make infra' then re-run this"
fi

# Logging and metrics cannot be conditioned per tenant; they are write-only and
# carry no cross-tenant read capability.
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  run gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${GSA_EMAIL}" --role "${role}" --quiet >/dev/null
  ok "${role}"
done

# --- 4. secrets ---------------------------------------------------------------
if [[ "${#PROVIDERS[@]}" -gt 0 ]]; then
  step "Provider credentials"
  for provider in "${PROVIDERS[@]}"; do
    secret="swarm-tenant-${TENANT_ID}-${provider}"
    if gcloud secrets describe "${secret}" --project "${PROJECT_ID}" \
         --format='value(name)' >/dev/null 2>&1; then
      run gcloud secrets add-iam-policy-binding "${secret}" \
        --project "${PROJECT_ID}" \
        --member "serviceAccount:${GSA_EMAIL}" \
        --role roles/secretmanager.secretAccessor --quiet >/dev/null
      ok "${secret}: ${GSA_ID} may read it"
    else
      warn "${secret} does not exist yet"
      dim "  create it with: scripts/create-secrets.sh --tenant ${TENANT_ID} --provider ${provider} --stdin"
    fi
  done
fi

# --- 5. Kubernetes ------------------------------------------------------------
if [[ "${SKIP_K8S}" -eq 0 ]]; then
  step "Kubernetes namespace (GKE path)"
  KUBECTL_BIN="$(kubectl_bin 2>/dev/null || true)"
  # Never create objects through whatever context happens to be current: this
  # kubeconfig can reach other teams' clusters, production included.
  if [[ -n "${KUBECTL_BIN}" ]] && kube_context_is_swarm \
     && "${KUBECTL_BIN}" get --raw='/readyz' >/dev/null 2>&1; then
    if "${KUBECTL_BIN}" get namespace "${NAMESPACE}" >/dev/null 2>&1; then
      ok "namespace ${NAMESPACE} exists"
    else
      run "${KUBECTL_BIN}" create namespace "${NAMESPACE}"
      run "${KUBECTL_BIN}" label namespace "${NAMESPACE}" \
        managed-by=swarm-terraform "swarm-tenant=${TENANT_ID}" --overwrite
      ok "created namespace ${NAMESPACE}"
    fi

    if "${KUBECTL_BIN}" -n "${NAMESPACE}" get serviceaccount "${KSA}" >/dev/null 2>&1; then
      ok "service account ${NAMESPACE}/${KSA} exists"
    else
      run "${KUBECTL_BIN}" -n "${NAMESPACE}" create serviceaccount "${KSA}"
      ok "created ${NAMESPACE}/${KSA}"
    fi
    run "${KUBECTL_BIN}" -n "${NAMESPACE}" annotate serviceaccount "${KSA}" \
      "iam.gke.io/gcp-service-account=${GSA_EMAIL}" --overwrite >/dev/null

    run gcloud iam service-accounts add-iam-policy-binding "${GSA_EMAIL}" \
      --project "${PROJECT_ID}" \
      --role roles/iam.workloadIdentityUser \
      --member "serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${KSA}]" \
      --quiet >/dev/null
    ok "workload identity: ${NAMESPACE}/${KSA} -> ${GSA_ID}"
  else
    info "not connected to the swarm cluster (context: $(kube_current_context || echo none)); skipping the GKE namespace"
    dim "  run scripts/configure-kubectl.sh and re-run, or pass --skip-k8s if this tenant never needs browser/GPU runners"
  fi
fi

# --- 6. control-plane documents ----------------------------------------------
step "Control plane"
if ! fs_database_exists; then
  warn "Firestore database '${FIRESTORE_DATABASE}' does not exist; run 'make infra' first"
  warn "the cloud resources above were created, but the tenant is not yet known to the scheduler"
  exit 1
fi

CREDENTIALS_JSON='[]'
for provider in ${PROVIDERS[@]+"${PROVIDERS[@]}"}; do
  CREDENTIALS_JSON="$(jq -c --arg p "${provider}" '. + [{stringValue:$p}]' <<<"${CREDENTIALS_JSON}")"
done

TENANT_FIELDS="$(jq -nc \
  --arg id "${TENANT_ID}" --arg kind "${KIND}" --arg principal "${PRINCIPAL}" \
  --arg display "${DISPLAY_NAME:-${PRINCIPAL}}" --arg sa "${GSA_EMAIL}" \
  --arg prefix "${GCS_PREFIX}" --arg ns "${NAMESPACE}" --arg at "$(iso_now)" \
  --argjson max "${MAX_ACTIVE}" --argjson units "${CAPACITY_UNITS}" \
  --argjson creds "${CREDENTIALS_JSON}" \
  --arg budget "${BUDGET}" '
  {
    tenant_id:{stringValue:$id},
    kind:{stringValue:$kind},
    principal:{stringValue:$principal},
    display_name:{stringValue:$display},
    created_at:{timestampValue:$at},
    max_active:{integerValue:($max|tostring)},
    capacity_units:{integerValue:($units|tostring)},
    enabled:{booleanValue:true},
    credentials:{arrayValue:{values:$creds}},
    service_account:{stringValue:$sa},
    gcs_prefix:{stringValue:$prefix},
    namespace:{stringValue:$ns}
  }
  + (if $budget == "" then {} else {monthly_budget_usd:{doubleValue:($budget|tonumber)}} end)')"

MASK="tenant_id,kind,principal,display_name,created_at,max_active,capacity_units,enabled,credentials,service_account,gcs_prefix,namespace"
[[ -n "${BUDGET}" ]] && MASK="${MASK},monthly_budget_usd"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  dim "  would write tenants/${TENANT_ID}"
else
  EXISTING="$(fs_get "tenants/${TENANT_ID}" | jq -r 'if .fields then "yes" else "no" end')"
  if [[ "${EXISTING}" == "yes" ]]; then
    # Preserve created_at on an update; only limits and wiring change.
    MASK="${MASK//created_at,/}"
    TENANT_FIELDS="$(jq -c 'del(.created_at)' <<<"${TENANT_FIELDS}")"
    fs_patch "tenants/${TENANT_ID}" "${MASK}" "${TENANT_FIELDS}"
    ok "updated tenants/${TENANT_ID}"
  else
    fs_patch "tenants/${TENANT_ID}" "${MASK}" "${TENANT_FIELDS}"
    ok "created tenants/${TENANT_ID}"
  fi
fi

POOL="tenant:${TENANT_ID}"
if [[ "${DRY_RUN}" -eq 1 ]]; then
  dim "  would write pools/${POOL} with hard_limit=${CAPACITY_UNITS}"
else
  POOL_EXISTING="$(fs_get "pools/${POOL}" | jq -c "${FS_JQ} if .fields then doc else null end")"
  if [[ "${POOL_EXISTING}" == "null" || -z "${POOL_EXISTING}" ]]; then
    # `active` starts at 0 and is only ever changed inside the admission and
    # release transactions -- never here, and never by any other background job.
    fs_patch "pools/${POOL}" "name,hard_limit,active,enabled,updated_at" \
      "$(jq -nc --arg n "${POOL}" --argjson l "${CAPACITY_UNITS}" --arg t "$(iso_now)" \
        '{name:{stringValue:$n},hard_limit:{integerValue:($l|tostring)},
          active:{integerValue:"0"},enabled:{booleanValue:true},updated_at:{timestampValue:$t}}')"
    ok "created pools/${POOL} (hard_limit ${CAPACITY_UNITS} units)"
  else
    CURRENT_ACTIVE="$(jq -r '.active // 0' <<<"${POOL_EXISTING}")"
    fs_patch "pools/${POOL}" "hard_limit,updated_at" \
      "$(jq -nc --argjson l "${CAPACITY_UNITS}" --arg t "$(iso_now)" \
        '{hard_limit:{integerValue:($l|tostring)},updated_at:{timestampValue:$t}}')"
    ok "updated pools/${POOL} hard_limit to ${CAPACITY_UNITS} (active ${CURRENT_ACTIVE} left untouched)"
  fi
fi

hr
ok "tenant ${TENANT_ID} registered"
cat >&2 <<EOF

Next:
  store provider keys   scripts/create-secrets.sh --tenant ${TENANT_ID} --provider anthropic --stdin
  then re-run this      scripts/register-tenant.sh --${KIND} ${PRINCIPAL} --providers anthropic
  check it              scripts/status.sh --tenant ${TENANT_ID}

Members of ${PRINCIPAL} now submit tasks with their own Google identity; the API
resolves them to tenant '${TENANT_ID}'. No shared token exists, by design.
EOF
