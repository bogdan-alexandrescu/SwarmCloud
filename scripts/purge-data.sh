#!/usr/bin/env bash
# Delete swarm RUNTIME DATA. Infrastructure is not touched -- that is destroy.sh.
#
# What this removes, and nothing else:
#   * documents in the '${FIRESTORE_DATABASE}' Firestore database (tasks and
#     their events subcollection, attempts, leases, workflows -- and pools/quota
#     /tenants only when --all is given, because deleting a pool document with an
#     active lease leaks that lease's capacity permanently);
#   * objects under the swarm artifact bucket prefix;
#   * tenant provider secrets, only with --secrets.
#
# Three hard guards:
#   1. it refuses to operate on the (default) Firestore database, which belongs
#      to the rest of this shared project;
#   2. it refuses to touch any bucket on the shared deny-list;
#   3. it exports Firestore to GCS before deleting anything, unless --no-backup.
#
# Usage:
#   scripts/purge-data.sh --dry-run
#   scripts/purge-data.sh --tenant eng
#   scripts/purge-data.sh --older-than-days 30
#   scripts/purge-data.sh --all --artifacts --secrets --environment dev

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: data deletion always requires a typed confirmation"
  unset SWARM_ASSUME_YES
fi

DRY_RUN=0
PURGE_ALL=0
WITH_ARTIFACTS=0
WITH_SECRETS=0
ALLOW_PROD=0
NO_BACKUP=0
TENANT=""
OLDER_THAN_DAYS=""
COLLECTIONS_CSV="tasks,attempts,leases,workflows"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment|-e)  ENVIRONMENT="$2"; export ENVIRONMENT; shift 2 ;;
    --tenant|-t)       TENANT="$2"; shift 2 ;;
    --older-than-days) OLDER_THAN_DAYS="$2"; shift 2 ;;
    --collections)     COLLECTIONS_CSV="$2"; shift 2 ;;
    --all)             PURGE_ALL=1; shift ;;
    --artifacts)       WITH_ARTIFACTS=1; shift ;;
    --secrets)         WITH_SECRETS=1; shift ;;
    --allow-prod)      ALLOW_PROD=1; shift ;;
    --no-backup)       NO_BACKUP=1; shift ;;
    --dry-run|-n)      DRY_RUN=1; shift ;;
    -h|--help)         sed -n '2,25p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq curl

step "Scope"
info "project      ${PROJECT_ID}"
info "environment  ${ENVIRONMENT}"
info "database     ${FIRESTORE_DATABASE}"
[[ -n "${TENANT}" ]] && info "tenant       ${TENANT}"
[[ -n "${OLDER_THAN_DAYS}" ]] && info "older than   ${OLDER_THAN_DAYS} day(s)"
[[ "${DRY_RUN}" -eq 1 ]] && warn "DRY RUN: nothing will be deleted"

# --- guard 1: never the shared default database -----------------------------
if [[ "${FIRESTORE_DATABASE}" == "(default)" || "${FIRESTORE_DATABASE}" == "default" ]]; then
  die "refusing to purge the (default) Firestore database; the swarm uses the named '${FIRESTORE_DATABASE}' database and (default) belongs to the rest of this shared project"
fi
fs_database_exists || die "Firestore database '${FIRESTORE_DATABASE}' does not exist"

# --- guard 2: never a shared bucket ------------------------------------------
if [[ "${WITH_ARTIFACTS}" -eq 1 ]] && is_shared_resource "${ARTIFACT_BUCKET}"; then
  die "ARTIFACT_BUCKET=${ARTIFACT_BUCKET} is a deny-listed shared bucket; refusing"
fi

if is_production && [[ "${ALLOW_PROD}" -eq 0 ]]; then
  die "environment is '${ENVIRONMENT}'; purging production data requires --allow-prod"
fi

COLLECTIONS=()
IFS=',' read -r -a COLLECTIONS <<<"${COLLECTIONS_CSV}"
if [[ "${PURGE_ALL}" -eq 1 ]]; then
  COLLECTIONS=(tasks attempts leases workflows pools quota tenants)
  warn "--all includes pools, quota and tenants: registered tenants will have to be re-registered"
fi

CUTOFF=""
if [[ -n "${OLDER_THAN_DAYS}" ]]; then
  # BSD date on macOS, GNU date elsewhere.
  CUTOFF="$(date -u -v-"${OLDER_THAN_DAYS}"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || date -u -d "${OLDER_THAN_DAYS} days ago" +%Y-%m-%dT%H:%M:%SZ)"
  info "cutoff       ${CUTOFF}"
fi

# ---------------------------------------------------------------------------
# Counting first, so the confirmation says a real number.
# ---------------------------------------------------------------------------
step "Counting"
TOTAL=0
declare_counts=""
for collection in "${COLLECTIONS[@]}"; do
  n="$(fs_count "${collection}")"
  TOTAL=$(( TOTAL + n ))
  printf '    %-12s %s\n' "${collection}" "${n}" >&2
  declare_counts="${declare_counts}${collection}=${n} "
done
info "total documents in scope before filtering: ${TOTAL}"

if [[ "${TOTAL}" -eq 0 && "${WITH_ARTIFACTS}" -eq 0 && "${WITH_SECRETS}" -eq 0 ]]; then
  ok "nothing to purge"
  exit 0
fi

# ---------------------------------------------------------------------------
# Backup before deletion.
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" -eq 0 && "${NO_BACKUP}" -eq 0 ]]; then
  step "Backup"
  BACKUP_URI="gs://${ARTIFACT_BUCKET}/backups/purge-$(date -u +%Y%m%dT%H%M%SZ)"
  if gcloud storage buckets describe "gs://${ARTIFACT_BUCKET}" \
       --project "${PROJECT_ID}" --format='value(name)' >/dev/null 2>&1; then
    info "exporting Firestore to ${BACKUP_URI} (this is synchronous and can take minutes)"
    gcloud firestore export "${BACKUP_URI}" \
      --project "${PROJECT_ID}" --database "${FIRESTORE_DATABASE}" 2>&1 | redact
    ok "backup written to ${BACKUP_URI}"
  else
    die "artifact bucket gs://${ARTIFACT_BUCKET} does not exist, so no backup can be taken; pass --no-backup to accept that"
  fi
fi

# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" -eq 0 ]]; then
  step "Confirmation"
  warn "about to permanently delete swarm data in ${PROJECT_ID} / ${FIRESTORE_DATABASE}"
  dim "  collections: ${COLLECTIONS[*]}"
  dim "  counts:      ${declare_counts}"
  [[ "${WITH_ARTIFACTS}" -eq 1 ]] && dim "  artifacts:   gs://${ARTIFACT_BUCKET}/tenants/${TENANT:-*}"
  [[ "${WITH_SECRETS}" -eq 1 ]]   && dim "  secrets:     tenant provider keys${TENANT:+ for ${TENANT}}"
  confirm "This cannot be undone except from the backup above." "purge-${ENVIRONMENT}"
  if is_production; then
    confirm "This is PRODUCTION data. Confirm the project id." "${PROJECT_ID}"
  fi
fi

# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

# Firestore commits accept up to 500 writes. 200 keeps each request small enough
# to retry cheaply when the network hiccups half way through a big purge.
BATCH=200

commit_deletes() {
  local names_file="$1" count=0 batch='[]' name
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    batch="$(jq -c --arg n "${name}" '. + [{delete:$n}]' <<<"${batch}")"
    count=$(( count + 1 ))
    if [[ $(( count % BATCH )) -eq 0 ]]; then
      fs_request POST "https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/${FIRESTORE_DATABASE}/documents:commit" \
        "$(jq -nc --argjson w "${batch}" '{writes:$w}')" >/dev/null
      batch='[]'
      printf '.' >&2
    fi
  done <"${names_file}"
  if [[ "$(jq -r 'length' <<<"${batch}")" -gt 0 ]]; then
    fs_request POST "https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/${FIRESTORE_DATABASE}/documents:commit" \
      "$(jq -nc --argjson w "${batch}" '{writes:$w}')" >/dev/null
  fi
  printf '\n' >&2
  printf '%s' "${count}"
}

# Which documents match the filters. Server-side filter on whichever single field
# is available (a tenant+time composite would need an index this script cannot
# create), the rest applied client-side.
select_docs() {
  local collection="$1" where='null'
  if [[ -n "${TENANT}" ]]; then
    where="$(fs_field_filter tenant_id EQUAL "$(jq -nc --arg v "${TENANT}" '{stringValue:$v}')")"
  elif [[ -n "${CUTOFF}" ]]; then
    where="$(fs_field_filter created_at LESS_THAN "$(jq -nc --arg v "${CUTOFF}" '{timestampValue:$v}')")"
  fi

  if [[ "${where}" == "null" ]]; then
    fs_list "${collection}" 300 | jq -r '.[]?.name'
  else
    fs_query "${collection}" "${where}" 1000 \
      | jq -r --arg cutoff "${CUTOFF}" '
          select($cutoff == "" or ((.fields.created_at.timestampValue // "9999") < $cutoff))
          | .name'
  fi
}

step "Deleting documents"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-purge.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM
DELETED=0

for collection in "${COLLECTIONS[@]}"; do
  names="${WORK}/${collection}.names"
  # select_docs's own pipeline already fails loudly (fs_list/fs_query -> fs_request
  # propagate a non-2xx as a non-zero exit under pipefail); do not paper over that
  # with `|| true` -- a denied or expired listing must not be read as an empty one.
  if ! select_docs "${collection}" >"${names}"; then
    die "could not list documents in '${collection}'; see the Firestore error above -- this is a failed listing, not an empty collection"
  fi
  n="$(wc -l <"${names}" | tr -d ' ')"

  if [[ "${collection}" == "tasks" && "${n}" -gt 0 ]]; then
    # Deleting a document does NOT delete its subcollections; orphaned events
    # would stay in the database forever and still be billed.
    events="${WORK}/task-events.names"
    : >"${events}"
    while IFS= read -r task_name; do
      [[ -n "${task_name}" ]] || continue
      task_id="${task_name##*/}"
      if ! fs_list "tasks/${task_id}/events" 300 | jq -r '.[]?.name' >>"${events}"; then
        die "could not list events for task '${task_id}'; see the Firestore error above -- this is a failed listing, not an empty subcollection"
      fi
    done <"${names}"
    e="$(wc -l <"${events}" | tr -d ' ')"
    if [[ "${e}" -gt 0 ]]; then
      if [[ "${DRY_RUN}" -eq 1 ]]; then
        info "would delete ${e} task event document(s)"
      else
        info "deleting ${e} task event document(s)"
        commit_deletes "${events}" >/dev/null
        DELETED=$(( DELETED + e ))
      fi
    fi
  fi

  if [[ "${n}" -eq 0 ]]; then
    dim "  ${collection}: nothing matched"
    continue
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "would delete ${n} document(s) from ${collection}"
    continue
  fi
  info "deleting ${n} document(s) from ${collection}"
  commit_deletes "${names}" >/dev/null
  DELETED=$(( DELETED + n ))
  ok "${collection} purged"
done

# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
if [[ "${WITH_ARTIFACTS}" -eq 1 ]]; then
  step "Artifacts"
  PREFIX="gs://${ARTIFACT_BUCKET}/tenants"
  [[ -n "${TENANT}" ]] && PREFIX="gs://${ARTIFACT_BUCKET}/tenants/${TENANT}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "would recursively delete ${PREFIX}/"
    gcloud storage ls "${PREFIX}/" --project "${PROJECT_ID}" 2>/dev/null | head -n 20 >&2 || true
  else
    info "deleting ${PREFIX}/"
    rm_out="${WORK}/artifacts-rm.log"
    if gcloud storage rm --recursive "${PREFIX}/" --project "${PROJECT_ID}" >"${rm_out}" 2>&1; then
      redact <"${rm_out}"
      ok "artifacts removed"
    else
      # A denied delete and an empty prefix both land here with a non-zero
      # exit; only the specific "matched no objects" message from gcloud
      # storage means the prefix was already empty. Anything else -- an
      # expired session, missing storage.objects.delete, a wrong project, a
      # retryable 503 -- must not be reported as a completed purge.
      die_if_auth_failure "$(cat "${rm_out}")"
      if grep -qi 'matched no objects\|no objects or files\|not found' "${rm_out}"; then
        warn "nothing to delete under ${PREFIX}/ (already empty)"
      else
        err "gcloud storage rm failed under ${PREFIX}/; this is NOT confirmation the artifacts are gone"
        redact <"${rm_out}" | head -n 5 | sed 's/^/     /' >&2
        die "cannot confirm artifacts were removed under ${PREFIX}/"
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
if [[ "${WITH_SECRETS}" -eq 1 ]]; then
  step "Tenant credentials"
  filter="labels.component=tenant-credential"
  [[ -n "${TENANT}" ]] && filter="${filter} AND labels.tenant=${TENANT}"
  secrets_err="${WORK}/secrets-list.err"
  # A denied secretmanager.secrets.list, an expired session or a malformed
  # filter must not read as "no matching secrets" -- this is a credential
  # purge, and reporting one destroyed when it was never even listed is worse
  # than the script refusing to run.
  if ! secrets="$(gcloud secrets list --project "${PROJECT_ID}" --filter "${filter}" \
       --format='value(name.basename())' 2>"${secrets_err}")"; then
    die_if_auth_failure "$(cat "${secrets_err}")"
    err "gcloud secrets list failed; this is NOT proof there are no matching tenant secrets"
    redact <"${secrets_err}" | head -n 5 | sed 's/^/     /' >&2
    die "cannot confirm tenant secrets are absent; aborting rather than reporting them purged"
  fi
  if [[ -z "${secrets}" ]]; then
    dim "  no matching secrets"
  else
    while IFS= read -r secret; do
      [[ -n "${secret}" ]] || continue
      if [[ "${DRY_RUN}" -eq 1 ]]; then
        info "would delete secret ${secret}"
      else
        gcloud secrets delete "${secret}" --project "${PROJECT_ID}" --quiet >/dev/null
        ok "deleted secret ${secret}"
      fi
    done <<<"${secrets}"
  fi
fi

hr
if [[ "${DRY_RUN}" -eq 1 ]]; then
  ok "dry run complete; nothing was deleted"
else
  ok "purged ${DELETED} document(s)"
  [[ "${NO_BACKUP}" -eq 1 ]] || info "backup: ${BACKUP_URI:-none}"
fi
