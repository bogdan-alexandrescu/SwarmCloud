#!/usr/bin/env bash
# Delete EVERYTHING the platform holds for one tenant, prove it is gone, and
# only then release the tenant id.
#
# OWNER DECISION, 2026-09-24. When a tenant is offboarded its data is deleted
# at offboarding -- not kept, not left to expire. A tenant id is derived from a
# principal (swarm_common.identity), GCS access is granted by an IAM condition
# on the `tenants/<id>/` prefix, and the API's collision check compares against
# `tenants/<id>`. So anything that outlives the tenant document is handed to
# whoever next registers a principal that derives the same id -- the same group
# coming back, or a different one that slugs the same way. The decision is that
# such a registration can read NOTHING of the old tenant. Provider secrets are
# NOT this script's: terraform deletes them with the tenant's removal from
# tfvars, and the owner chose not to add deletion protection to them.
#
# WHAT IT DELETES, in this order, and why the order:
#
#   1. every object version under gs://<artifact bucket>/tenants/<id>/ --
#      artifacts, logs and CHECKPOINTS. All versions, because the bucket is
#      versioned and a noncurrent version is as readable as a live one.
#   2. Firestore, in batches, with a running count:
#        events, any task          tenant_id == <id>, one collection-group
#                                  listing across every task -- including a
#                                  task an earlier run already deleted
#        tasks/<t>/events/*        each listed task's events, whatever tenant_id
#                                  they carry, BEFORE the task itself
#        tasks                     tenant_id == <id>, each one only once its
#                                  events are recounted at ZERO
#        attempts, leases, workflows, quota,
#        outcome_days                                 tenant_id == <id>
#                                  (outcome_days: the GET /v1/outcomes rollup,
#                                  per tenant per UTC day; it holds task ids,
#                                  profiles and submitter emails, so it goes
#                                  with the tenant like everything else)
#        accounts, account_auth                       owner_tenant == <id>
#        credential_publications/<name>  listed from the ledger itself: see
#                                  ledger_of_tenant for how an entry is
#                                  attributed when its id is only a name
#        pools/tenant:<id>, pools/provider:<p>:tenant:<id>
#   3. VERIFICATION: every one of the above counted again, server side, by a
#      different call from the one that listed it, and printed as a table. Any
#      non-zero count fails the run, and the tenant document is kept.
#   4. tenants/<id>, LAST. While it exists the id is held and nobody else can
#      register it; deleting it before the data is proven gone would open the
#      exact window the owner decision closes. A failed run therefore leaves
#      the tenant registered, and a second run lists everything again from
#      the records themselves -- never through a parent document the first
#      run may have deleted -- so it finds what the first one left.
#
# WHY EVENTS ARE FOUND TWO WAYS. Firestore does not delete a document's
# subcollections with it. A task document deleted while an event is still
# under it leaves that event where no `tenant_id ==` listing of TASKS can ever
# reach it again, and a proof that counts only under the tasks it listed then
# prints 0 over it -- on the run after a failed proof, which lists no tasks at
# all. That is reachable: every event writer (the scheduler's cascade cancel,
# the API, the worker, the reconciler) appends with a blind set, outside any
# transaction this script could see, so an event can land between the events
# delete and the task delete. So:
#   * a task is deleted only after its events are recounted at zero -- a task
#     with one left is KEPT, the proof fails, and the next run finds it again;
#   * every event carrying this tenant's id is also listed and counted with a
#     collection-group query from the database root, which finds it whether
#     or not its task document still exists. That query MUST order by `at`
#     DESCENDING: measured read-only against the live `swarm` database on
#     2026-09-24, without it a runQuery and a count both answer 400
#     FAILED_PRECONDITION, because Firestore keeps single-field indexes for
#     COLLECTION scope only and the one index that serves it is terraform's
#     `events-tenant-at` (tenant_id ASC, at DESC). Every writer sets `at`.
#   * an event with NO tenant_id of this tenant (the reconciler writes
#     `tenant_id or ""`) is found only under its task, which is the reason
#     the per-task listing and the recount stay.
#
# WHAT IT LEAVES FOR TERRAFORM. A tenant document or pool document carrying
# `managed_by=swarm-terraform` is not deleted here. terraform/modules/firestore
# created it and holds it in state with `enabled = true`, so deleting it here
# would make the next release -- any push to main, before the tenant's tfvars
# removal merges -- recreate it ENABLED, reopening a tenant whose data this run
# just deleted. Such documents hold no tenant data (limits and counters), they
# keep the id held until terraform deletes them in the runbook's next step, and
# the proof table names each one as left for terraform.
#
# WHAT IT REFUSES, before printing anything else and again after the typed
# confirmation (which can sit at a prompt for as long as the operator likes):
#
#   * a tenant id that is not a tenant id, or is not registered (no
#     tenants/<id>) -- a typo must find nothing to delete, not a prefix;
#   * a tenant that is still ENABLED: while it is, the API accepts its work and
#     the scheduler can admit a task between the capacity check and the delete;
#   * a tenant holding ANY capacity -- a task in LEASED, DISPATCHED, STARTING or
#     RUNNING, an unreleased lease, a pool with active > 0, or an account with
#     an agent assigned. CONTRACT.md invariants 1 and 3: capacity is held from
#     LEASED, and nothing here may delete under a running worker or delete a
#     lease whose capacity would then never be given back;
#   * an account of ANOTHER tenant that still lends to this id: a lend names an
#     id, so it would lend to whoever registers the id next;
#   * the (default) Firestore database, a deny-listed bucket, and a bucket
#     without the platform prefix;
#   * production, without --allow-prod.
#
# NO BACKUP, ON PURPOSE. purge-data.sh exports the whole database into the
# artifact bucket before it deletes. Here that export would be a second copy of
# exactly the data the owner decision says must not survive.
#
# WHAT SURVIVES ANYWAY, and is printed rather than hidden: object versions this
# run deletes are SOFT-deleted, and the bucket keeps them for its soft-delete
# retention (7 days, measured 2026-09-24). roles/storage.objectUser includes
# storage.objects.restore (measured the same day), and that is the role a
# tenant's worker holds on its prefix -- so a re-registration of this id that
# gets a worker service account inside that window could restore them. And the
# `swarm` database has point-in-time recovery on with a 7-day version retention
# (measured the same day), so every document deleted here stays readable AS OF
# an earlier time for 7 days, by id, to any holder of datastore.entities.get --
# which every tenant worker's unconditioned role includes. Neither window is
# something this script can close; docs/runbooks/tenant-offboarding.md lists both.
#
# Dry run is the default: it checks, inventories and exits. --apply deletes,
# after the tenant id is TYPED at a terminal. SWARM_ASSUME_YES is ignored.
#
# Usage:
#   scripts/offboard-tenant.sh --tenant eng              # inventory only
#   scripts/offboard-tenant.sh --tenant eng --apply      # delete, after typing "eng"
#
# docs/runbooks/tenant-offboarding.md is where this runs, and in what order.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# common.sh's confirm() skips the prompt when SWARM_ASSUME_YES is set. That is
# right for a pause and wrong here: nothing about deleting a tenant's data may
# be unattended. Unset first, before anything can call confirm().
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: offboarding deletes a tenant's data and always needs the tenant id typed at a terminal"
  unset SWARM_ASSUME_YES
fi

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/offboard-tenant.sh --tenant <id> [--apply] [--allow-prod]

  --tenant <id>   the tenant to offboard (required)
  --apply         delete, after typing the tenant id; without it, a dry run
  --dry-run       the default, accepted so a command line can say so
  --allow-prod    required when ENVIRONMENT is prod

Refuses a tenant that is not registered, is still enabled, holds any capacity,
or is still lent an account. See docs/runbooks/tenant-offboarding.md.
USAGE
}

TENANT=""
APPLY=0
SAID_DRY_RUN=0
ALLOW_PROD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant|-t)
      [[ $# -ge 2 ]] || die "--tenant needs a value"
      TENANT="$2"; shift 2 ;;
    --tenant=*)   TENANT="${1#--tenant=}"; shift ;;
    --apply)      APPLY=1; shift ;;
    --dry-run|-n) SAID_DRY_RUN=1; shift ;;
    --allow-prod) ALLOW_PROD=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done

if [[ "${APPLY}" -eq 1 && "${SAID_DRY_RUN}" -eq 1 ]]; then
  die "--apply and --dry-run contradict each other; say which one you mean"
fi

# --- the tenant id ----------------------------------------------------------
#
# The rule terraform/modules/tenancy/variables.tf enforces and
# swarm_common.identity produces: lowercase letters, digits and `-`, at most 11
# characters (the worker service account swarm-agent-worker-<id> has a
# 30-character limit). Spelled as an explicit character list, not `[a-z]`: in
# bash 3.2 a range in a pattern follows the locale's collation, and `[a-z]`
# also matches `E` (measured 2026-09-24, docs/runbooks/tenant-offboarding.md
# step 1). An empty id is the case that matters most: `tenants//` and
# `tenants/` are the prefixes of every tenant at once.
case "${TENANT}" in
  '')
    usage; die "--tenant is required" ;;
  -*|*[!abcdefghijklmnopqrstuvwxyz0123456789-]*)
    die "'${TENANT}' is not a tenant id: lowercase letters, digits and '-', not starting with '-'" ;;
esac
[[ "${#TENANT}" -le 11 ]] || die "'${TENANT}' is not a tenant id: at most 11 characters"
if is_shared_resource "${TENANT}"; then
  die "'${TENANT}' is a name on the shared deny-list; refusing"
fi

# --- what this run may touch -------------------------------------------------
case "${FIRESTORE_DATABASE}" in
  ''|'(default)'|default)
    die "refusing to touch the (default) Firestore database; it belongs to the rest of this shared project" ;;
esac
if is_shared_resource "${ARTIFACT_BUCKET}"; then
  die "ARTIFACT_BUCKET=${ARTIFACT_BUCKET} is on the shared deny-list; refusing"
fi
case "${ARTIFACT_BUCKET}" in
  "$(guard_name_prefix)"*) ;;
  *) die "ARTIFACT_BUCKET=${ARTIFACT_BUCKET} does not carry the platform prefix '$(guard_name_prefix)'; refusing to delete from a bucket this platform did not name" ;;
esac
if is_production && [[ "${ALLOW_PROD}" -eq 0 ]]; then
  die "ENVIRONMENT is '${ENVIRONMENT}'; offboarding a production tenant requires --allow-prod"
fi

require_cmd gcloud jq curl

#: The one GCS prefix this run deletes under. The trailing `/` is what keeps
#: tenant `eng` from matching tenant `eng-x`.
GCS_PREFIX_URL="gs://${ARTIFACT_BUCKET}/tenants/${TENANT}/"
#: Every Firestore name this run deletes starts with this.
DOCS_ROOT="projects/${PROJECT_ID}/databases/${FIRESTORE_DATABASE}/documents"

#: Deletes per Firestore commit. The API accepts 500; 200 keeps a request cheap
#: to retry when the network drops half way, the same figure purge-data.sh uses.
BATCH=200
#: Documents per listing page. A listing is paginated on a cursor, so this is a
#: page size, not a cap: a tenant with 10,000 tasks is listed in full.
FS_PAGE=300

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-offboard.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

RECORD_DIR="${BUILD_DIR}/offboard-${TENANT}"
mkdir -p "${RECORD_DIR}"
RECORD="${RECORD_DIR}/offboard-tenant-$(date -u +%Y%m%dT%H%M%SZ).txt"

# Print a finished table, and keep it. Only ids and counts are ever in it; it
# goes through redact anyway, because it lands in a file someone pastes into a
# PR.
emit() {
  redact <"$1" | tee -a "${RECORD}" >&2
}

# The access token, minted in THIS shell. fs_request asks for it inside a
# command substitution, so common.sh's cache lives and dies in a subshell and
# every Firestore request starts a fresh `gcloud auth print-access-token` --
# about half a second each, and a tenant with a few hundred tasks makes over a
# thousand requests. Minted here, every subshell inherits it. Re-minted every
# 120 seconds: gcloud hands out a cached token until it is within minutes of
# expiry, so a token is only known to be good for a few minutes, and the
# typed confirmation can sit at its prompt for an hour.
TOKEN_MINTED_AT=-1
fresh_token() {
  if [[ "${TOKEN_MINTED_AT}" -lt 0 || $(( SECONDS - TOKEN_MINTED_AT )) -ge 120 ]]; then
    _ACCESS_TOKEN=""
    access_token >/dev/null
    TOKEN_MINTED_AT="${SECONDS}"
  fi
}

# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

eq_filter() {
  fs_field_filter "$1" EQUAL "$(jq -nc --arg v "$2" '{stringValue:$v}')"
}
and_filter() {
  jq -nc --argjson a "$1" --argjson b "$2" '{compositeFilter:{op:"AND",filters:[$a,$b]}}'
}

# fs_list_where PARENT COLLECTION WHERE_JSON FIELDS_CSV OUT_FILE
#
# Every document in COLLECTION (under PARENT; "" is the database root) that
# matches WHERE, one `{"name": ..., "fields": {...}}` per line, with only
# FIELDS projected. Paginated on __name__ with a cursor until a short page:
# fs_query in common.sh returns ONE page, and a silent cap on a deletion
# listing is a deletion that reports success over part of the tenant.
fs_list_where() {
  local parent="$1" collection="$2" where="$3" fields="$4" out="$5"
  local url query after="" page_file="${WORK}/page.jsonl" n
  url="$(fs_base)${parent:+/${parent}}:runQuery"
  : >"${out}"
  while :; do
    fresh_token
    query="$(jq -nc --arg c "${collection}" --argjson w "${where}" --arg f "${fields}" \
        --arg after "${after}" --argjson l "${FS_PAGE}" '
      {structuredQuery: (
         {from: [{collectionId: $c}],
          select: {fields: ([{fieldPath: "__name__"}]
                            + [$f | split(",")[] | select(length > 0) | {fieldPath: .}])},
          orderBy: [{field: {fieldPath: "__name__"}, direction: "ASCENDING"}],
          limit: $l}
         + (if $w == null then {} else {where: $w} end)
         + (if $after == "" then {} else {startAt: {values: [{referenceValue: $after}], before: false}} end))}')"
    fs_request POST "${url}" "${query}" >"${WORK}/page.json" || return 1
    jq -c '.[]? | select(.document != null) | .document | {name, fields: (.fields // {})}' \
      "${WORK}/page.json" >"${page_file}" || return 1
    n="$(grep -c . "${page_file}" || true)"
    cat "${page_file}" >>"${out}"
    [[ "${n}" -ge "${FS_PAGE}" ]] || break
    after="$(tail -n 1 "${page_file}" | jq -r '.name')"
  done
}

# fs_count_under PARENT COLLECTION -> integer. The server-side count of a
# SUBCOLLECTION. common.sh's fs_count_where addresses root collections only.
# Called inside `$(...)`, so the CALLER refreshes the token first.
fs_count_under() {
  local parent="$1" collection="$2" query result
  query="$(jq -nc --arg c "${collection}" '
    {structuredAggregationQuery:{structuredQuery:{from:[{collectionId:$c}]},
                                 aggregations:[{alias:"n",count:{}}]}}')"
  result="$(fs_request POST "$(fs_base)/${parent}:runAggregationQuery" "${query}")" || return 1
  printf '%s' "${result}" | jq -r '[.[]?|.result?.aggregateFields?.n?.integerValue//empty]|first // "0"'
}

# The collection-group query every event carrying this tenant's id answers:
# `events` at any depth under the database root, tenant_id == <id>, ordered
# by `at` DESCENDING. The order is not decoration. Without it the database
# refuses the query (FAILED_PRECONDITION, measured 2026-09-24; the header
# says why), and a refused count is a failed read, never a zero.
tenant_events_query() {
  jq -nc --argjson w "$(eq_filter tenant_id "${TENANT}")" '
    {from: [{collectionId: "events", allDescendants: true}],
     where: $w,
     orderBy: [{field: {fieldPath: "at"}, direction: "DESCENDING"}]}'
}

# fs_list_tenant_events OUT_FILE -- every event carrying this tenant's id, one
# `{"name": ..., "fields": {tenant_id, at}}` per line. Paginated like
# fs_list_where, on an (at, __name__) cursor: `at` alone is not unique, and a
# cursor on it would skip every event that shares a timestamp with the last
# one on a page.
fs_list_tenant_events() {
  local out="$1" query cursor="null" page_file="${WORK}/events-page.jsonl" n
  : >"${out}"
  while :; do
    fresh_token
    query="$(jq -nc --argjson q "$(tenant_events_query)" --argjson cursor "${cursor}" --argjson l "${FS_PAGE}" '
      {structuredQuery: (
         $q
         + {select: {fields: [{fieldPath: "__name__"}, {fieldPath: "tenant_id"}, {fieldPath: "at"}]},
            orderBy: ($q.orderBy + [{field: {fieldPath: "__name__"}, direction: "DESCENDING"}]),
            limit: $l}
         + (if $cursor == null then {} else {startAt: {values: $cursor, before: false}} end))}')"
    fs_request POST "$(fs_base):runQuery" "${query}" >"${WORK}/events-page.json" || return 1
    jq -c '.[]? | select(.document != null) | .document | {name, fields: (.fields // {})}' \
      "${WORK}/events-page.json" >"${page_file}" || return 1
    n="$(grep -c . "${page_file}" || true)"
    cat "${page_file}" >>"${out}"
    [[ "${n}" -ge "${FS_PAGE}" ]] || break
    cursor="$(tail -n 1 "${page_file}" | jq -c '[.fields.at, {referenceValue: .name}]')"
  done
}

# fs_count_tenant_events -> integer, server side. Called inside `$(...)`, so
# the CALLER refreshes the token first.
fs_count_tenant_events() {
  local query result
  query="$(jq -nc --argjson q "$(tenant_events_query)" \
    '{structuredAggregationQuery: {structuredQuery: $q, aggregations: [{alias: "n", count: {}}]}}')"
  result="$(fs_request POST "$(fs_base):runAggregationQuery" "${query}")" || return 1
  printf '%s' "${result}" | jq -r '[.[]?|.result?.aggregateFields?.n?.integerValue//empty]|first // "0"'
}

# assert_tenant_events LISTING -- the collection-group listing came from a
# server-side `tenant_id == <id>` filter. Checked again client side, with the
# shape: every name must be tasks/<task>/events/<event> in this database. An
# `events` collection anywhere else is not one this script knows how to own.
assert_tenant_events() {
  local listing="$1" stray
  stray="$(jq -r --arg t "${TENANT}" --arg root "${DOCS_ROOT}/" '
    select(((.fields.tenant_id.stringValue // "") != $t)
           or ((.name | startswith($root)) | not)
           or ((.name | ltrimstr($root) | split("/")) as $s
               | ($s | length) != 4 or $s[0] != "tasks" or $s[2] != "events"))
    | .name' "${listing}")" || die "could not read the events listing"
  if [[ -n "${stray}" ]]; then
    err "the events listing for tenant_id == ${TENANT} returned documents that are not ${TENANT}'s task events:"
    printf '%s\n' "${stray}" | head -n 5 | sed 's/^/     /' >&2
    die "refusing to delete anything from a listing that does not say what it was asked"
  fi
}

# assert_owned LISTING FIELD COLLECTION
#
# The listing came from a server-side `FIELD == <id>` filter. This checks every
# document's FIELD again, client side, and that its name is a direct child of
# COLLECTION in this database, because a filter that was dropped or mistyped
# would otherwise hand every tenant's documents to the delete below.
assert_owned() {
  local listing="$1" field="$2" collection="$3" stray
  stray="$(jq -r --arg f "${field}" --arg t "${TENANT}" \
      --arg root "${DOCS_ROOT}/${collection}/" '
    select(((.fields[$f].stringValue // "") != $t)
           or ((.name | startswith($root)) | not)
           or ((.name | ltrimstr($root)) | contains("/")))
    | .name' "${listing}")" || die "could not read the ${collection} listing"
  if [[ -n "${stray}" ]]; then
    err "the ${collection} listing for ${field} == ${TENANT} returned documents that are not ${TENANT}'s:"
    printf '%s\n' "${stray}" | head -n 5 | sed 's/^/     /' >&2
    die "refusing to delete anything from a listing that does not say what it was asked"
  fi
}

# commit_deletes NAMES_FILE LABEL [quiet] -- delete every name in the file,
# BATCH per Firestore commit, printing the running count unless `quiet` (the
# per-task events, which report once per 25 tasks instead). Quiet silences the
# progress lines only, never a failure.
commit_deletes() {
  local names="$1" label="$2" quiet="${3:-}" total deleted=0 start=1 end body n
  total="$(grep -c . "${names}" || true)"
  if [[ "${total}" -eq 0 ]]; then
    [[ -n "${quiet}" ]] || printf '    %-26s nothing to delete\n' "${label}" >&2
    return 0
  fi
  while [[ "${start}" -le "${total}" ]]; do
    fresh_token
    end=$(( start + BATCH - 1 ))
    sed -n "${start},${end}p" "${names}" >"${WORK}/batch.names"
    n="$(grep -c . "${WORK}/batch.names" || true)"
    body="$(jq -R . "${WORK}/batch.names" | jq -sc '{writes: map({delete: .})}')"
    if ! fs_request POST "$(fs_base):commit" "${body}" >/dev/null; then
      die "Firestore refused a delete batch in ${label} after ${deleted} of ${total}; nothing after it was attempted. The run is safe to repeat."
    fi
    deleted=$(( deleted + n ))
    [[ -n "${quiet}" ]] || printf '    %-26s deleted %s/%s\n' "${label}" "${deleted}" "${total}" >&2
    start=$(( end + 1 ))
  done
}

# names_of LISTING -> one document name per line
names_of() { jq -r '.name' "$1"; }

# ---------------------------------------------------------------------------
# GCS
# ---------------------------------------------------------------------------

GCS_OBJECTS=0
GCS_BYTES=0
#: In a variable, because bash 3.2 and 5 disagree about quoting inside `=~`.
GCS_TOTAL_RE='^TOTAL: ([0-9]+) objects?, ([0-9]+) bytes'

# gcs_total WHAT URL -- sets GCS_OBJECTS and GCS_BYTES from one listing.
# WHAT is `--all-versions` (live and noncurrent) or `--soft-deleted`.
#
# `gcloud storage ls --long` ends with `TOTAL: <n> objects, <b> bytes (...)`,
# and an empty match exits 1 with "One or more URLs matched no objects."
# (both measured against the live bucket on 2026-09-24). ONLY that sentence is
# an empty prefix. Anything else non-zero -- a dead session, a missing
# storage.objects.list, a wrong bucket -- is a failure to look, and a deletion
# script that reads it as "nothing there" reports a tenant gone whose
# checkpoints are all still readable.
gcs_total() {
  local what="$1" url="$2" out="${WORK}/gcs-ls.out" errf="${WORK}/gcs-ls.err" rc=0 total
  GCS_OBJECTS=0
  GCS_BYTES=0
  gcloud storage ls "${what}" --long "${url}**" --project "${PROJECT_ID}" \
    >"${out}" 2>"${errf}" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    total="$(grep '^TOTAL: ' "${out}" | tail -n 1 || true)"
    if [[ ! "${total}" =~ ${GCS_TOTAL_RE} ]]; then
      err "gcloud storage ls ${url} printed no TOTAL line this script can read: '${total}'"
      return 1
    fi
    GCS_OBJECTS="${BASH_REMATCH[1]}"
    GCS_BYTES="${BASH_REMATCH[2]}"
    return 0
  fi
  if grep -q 'matched no objects' "${errf}"; then
    return 0
  fi
  die_if_auth_failure "$(cat "${errf}")"
  err "could not list ${url} (${what}); that is a failure to LOOK, not an empty prefix:"
  redact <"${errf}" | head -n 3 | sed 's/^/     /' >&2
  return 1
}

# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

TENANT_IS_TERRAFORM=0

# The tenant must be registered, must be the document it claims to be, and
# must be disabled. Run before anything else and again after the confirmation.
refuse_unless_registered_and_disabled() {
  local doc
  fresh_token
  doc="$(fs_get "tenants/${TENANT}")" \
    || die "could not read tenants/${TENANT}; that is a failed read, not an unregistered tenant"
  if ! jq -e '.fields' <<<"${doc}" >/dev/null; then
    die "tenants/${TENANT} does not exist: '${TENANT}' is not a registered tenant, so there is nothing this script will delete. A tenant that is already gone is purge-data.sh's case, not this one's."
  fi
  if ! jq -e --arg t "${TENANT}" '.fields.tenant_id.stringValue == $t' <<<"${doc}" >/dev/null; then
    die "tenants/${TENANT} does not declare tenant_id '${TENANT}'; refusing to act on a document that is not what its name says"
  fi
  # `== false`, never `// true`: jq's alternative operator treats false as
  # absent, so a disabled tenant would read as enabled -- CLAUDE.md's trap.
  if ! jq -e '.fields.enabled.booleanValue == false' <<<"${doc}" >/dev/null; then
    die "tenants/${TENANT} is still ENABLED. Disable it first (runbook step 3): while it is enabled the API accepts its work and the scheduler can admit a task between this check and the delete."
  fi
  TENANT_IS_TERRAFORM=0
  if jq -e '.fields.managed_by.stringValue == "swarm-terraform"' <<<"${doc}" >/dev/null; then
    TENANT_IS_TERRAFORM=1
  fi
}

# The tenant's pools, decoded: `id<TAB>active<TAB>managed_by` per line. A pool
# is this tenant's when its id is exactly `tenant:<id>` or
# `provider:<p>:tenant:<id>` -- compared by segment, so `tenant:eng` is never
# `tenant:eng-x`.
list_tenant_pools() {
  local out="$1" docs
  docs="$(fs_list_docs pools)" || die "could not list pools; that is a failed read, not none"
  printf '%s\n' "${docs}" | jq -r --arg t "${TENANT}" '
    select(. != null)
    | (.id | split(":")) as $s
    | select($s == ["tenant", $t]
             or (($s | length) == 4 and $s[0] == "provider" and $s[2] == "tenant" and $s[3] == $t))
    | [.id, ((.active // 0) | tostring), (.managed_by // "")] | @tsv' >"${out}" \
    || die "could not decode the pools listing"
}

# Every way this tenant can hold capacity. Refuses if any is non-zero.
# Leaves the tenant's leases, pools and accounts listed in WORK for the
# inventory; the deletes list them again rather than trust this.
refuse_if_capacity_held() {
  local when="$1" table="${WORK}/capacity.txt" state n held=0 checks=0
  local leases_total unreleased pools_total pools_active accounts_total accounts_busy
  fresh_token

  printf '== Capacity held by %s (%s) ==\n' "${TENANT}" "${when}" >"${table}"

  for state in "${CONCURRENCY_STATES[@]}"; do
    n="$(fs_count_where tasks "$(and_filter "$(eq_filter tenant_id "${TENANT}")" "$(eq_filter state "${state}")")")" \
      || die "could not count ${state} tasks; that is a failed read, not zero"
    printf '    %-22s %s\n' "tasks ${state}" "${n}" >>"${table}"
    held=$(( held + n ))
    checks=$(( checks + 1 ))
  done
  # An empty loop prints nothing and looks like "nothing running".
  [[ "${checks}" -eq "${#CONCURRENCY_STATES[@]}" && "${checks}" -gt 0 ]] \
    || die "checked ${checks} capacity states, expected ${#CONCURRENCY_STATES[@]}"

  # Unreleased is decided client side, over every lease: released_at absent OR
  # null. A server-side IS_NULL matches only a field that exists and is null.
  fs_list_where "" leases "$(eq_filter tenant_id "${TENANT}")" "tenant_id,released_at" "${WORK}/leases.jsonl" \
    || die "could not list leases; that is a failed read, not none"
  leases_total="$(grep -c . "${WORK}/leases.jsonl" || true)"
  unreleased="$(jq -s '[.[] | select((.fields.released_at // {nullValue: null}) | has("nullValue"))] | length' \
    "${WORK}/leases.jsonl")"
  printf '    %-22s %s   (of %s lease(s))\n' "unreleased leases" "${unreleased}" "${leases_total}" >>"${table}"
  held=$(( held + unreleased ))
  checks=$(( checks + 1 ))

  list_tenant_pools "${WORK}/pools.tsv"
  pools_total="$(grep -c . "${WORK}/pools.tsv" || true)"
  pools_active="$(awk -F'\t' '$2 + 0 > 0' "${WORK}/pools.tsv" | grep -c . || true)"
  printf '    %-22s %s   (of %s pool(s))\n' "pools with active > 0" "${pools_active}" "${pools_total}" >>"${table}"
  held=$(( held + pools_active ))
  checks=$(( checks + 1 ))

  fs_list_where "" accounts "$(eq_filter owner_tenant "${TENANT}")" "owner_tenant,assigned" "${WORK}/accounts.jsonl" \
    || die "could not list accounts; that is a failed read, not none"
  accounts_total="$(grep -c . "${WORK}/accounts.jsonl" || true)"
  accounts_busy="$(jq -s '[.[] | select(((.fields.assigned.integerValue // "0") | tonumber) > 0)] | length' \
    "${WORK}/accounts.jsonl")"
  printf '    %-22s %s   (of %s account(s))\n' "accounts assigned" "${accounts_busy}" "${accounts_total}" >>"${table}"
  held=$(( held + accounts_busy ))
  checks=$(( checks + 1 ))

  printf '    (%s capacity checks)\n' "${checks}" >>"${table}"
  emit "${table}"

  if [[ "${held}" -ne 0 ]]; then
    die "refusing to offboard ${TENANT}: ${held} thing(s) above still hold capacity. Drain first (runbook step 4); nothing was deleted."
  fi
}

refuse_if_lent_to() {
  local n
  fresh_token
  n="$(fs_count_where accounts "$(jq -nc --arg v "${TENANT}" \
        '{fieldFilter:{field:{fieldPath:"lend_to"},op:"ARRAY_CONTAINS",value:{stringValue:$v}}}')")" \
    || die "could not count accounts lent to ${TENANT}; that is a failed read, not zero"
  if [[ "${n}" -ne 0 ]]; then
    die "refusing to offboard ${TENANT}: ${n} account(s) of other tenants still lend to it. A lend names an id, so it would lend to whoever registers '${TENANT}' next. Remove them first (runbook step 5)."
  fi
}

# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

#: Every task name this run has seen, so the events check after the deletes
#: can look under each one.
ALL_TASKS="${WORK}/all-tasks.names"
: >"${ALL_TASKS}"

# ledger_of_tenant OUT_FILE -- the full document names of this tenant's
# publication-ledger entries, listed FROM THE LEDGER ITSELF.
#
# `credential_publications/<secret name>` carries no tenant field, so an entry
# is attributed by its id. This used to start from the secrets labelled
# tenant=<id> and GET each name -- which finds nothing for an entry whose
# secret is already gone, and a proof built the same way then printed 0 over
# it. Now every entry is listed (one per base secret: five in the live
# database on 2026-09-24) and one is this tenant's when:
#
#   * its id is one of this tenant's secret-name forms, `swarm-tenant-<id>-*`
#     or `swarm-account-<id>--*` -- the shapes the runbook's deny-rule allows;
#   * and, because a name form is a PREFIX and prefixes collide
#     (`swarm-tenant-eng-` also starts every secret of tenant `eng-x`):
#       - if a secret of that name (or its `-refresh` twin) still carries a
#         `tenant` label, the label decides, exactly;
#       - if none does, the entry is this tenant's only when no LONGER
#         registered tenant id also names it. `eng-x`'s entry is `eng-x`'s
#         while `tenants/eng-x` exists, whether or not its secret does.
#
# What that still cannot tell apart: an entry of a tenant that is neither
# registered nor labelled any more, whose id also starts with this tenant's
# form. Such an entry belongs to nobody, holds a digest and no credential, and
# is deleted with this tenant's -- the one direction a wrong guess here costs
# nothing a live tenant needs.
ledger_of_tenant() {
  local out="$1" errf="${WORK}/secrets.err" ids labels
  fs_list_where "" credential_publications null "" "${WORK}/ledger.jsonl" \
    || die "could not list credential_publications; a failed read, not none"
  fs_list_where "" tenants null "" "${WORK}/tenants.jsonl" \
    || die "could not list tenants; a failed read, not none"
  if ! gcloud secrets list --project "${PROJECT_ID}" --filter='labels.tenant:*' \
       --format='value(name.basename(),labels.tenant)' >"${WORK}/secret-tenants.tsv" 2>"${errf}"; then
    die_if_auth_failure "$(cat "${errf}")"
    err "gcloud secrets list failed; that is NOT proof no secret is labelled for another tenant:"
    redact <"${errf}" | head -n 3 | sed 's/^/     /' >&2
    die "cannot attribute the publication ledger for ${TENANT}"
  fi
  ids="$(jq -c -s '[.[] | .name | split("/") | last]' "${WORK}/tenants.jsonl")" \
    || die "could not read the tenants listing"
  labels="$(jq -R -s -c '
    split("\n") | map(select(length > 0) | split("\t") | {key: .[0], value: (.[1] // "")})
    | from_entries' "${WORK}/secret-tenants.tsv")" \
    || die "could not read the secrets listing"
  jq -r --arg t "${TENANT}" --arg root "${DOCS_ROOT}/credential_publications/" \
      --argjson ids "${ids}" --argjson labels "${labels}" '
    def named_for($id; $x):
      ($id | startswith("swarm-tenant-" + $x + "-")) or ($id | startswith("swarm-account-" + $x + "--"));
    select(.name | startswith($root))
    | (.name | ltrimstr($root)) as $id
    | select(($id | contains("/")) | not)
    | select(named_for($id; $t))
    | (($labels[$id] // "") as $a | ($labels[$id + "-refresh"] // "") as $b
       | if $a != "" then $a else $b end) as $owner
    | select(if $owner != "" then $owner == $t
             else ([$ids[] | select(. != $t and length > ($t | length) and named_for($id; .))] | length) == 0
             end)
    | .name' "${WORK}/ledger.jsonl" >"${out}" \
    || die "could not attribute the publication ledger for ${TENANT}"
}

# The pools this run deletes: the tenant's, less any terraform holds.
pools_to_delete() {
  awk -F'\t' '$3 != "swarm-terraform" {print $1}' "$1"
}
pools_for_terraform() {
  awk -F'\t' '$3 == "swarm-terraform" {print $1}' "$1"
}

inventory() {
  local table="${WORK}/inventory.txt" c n events=0 visited=0 task tasks objects bytes
  local attempts workflows quota auth tenant_events outcome_days
  step "Inventory"
  fresh_token
  gcs_total --all-versions "${GCS_PREFIX_URL}" || die "cannot inventory ${GCS_PREFIX_URL}"
  objects="${GCS_OBJECTS}"
  bytes="${GCS_BYTES}"

  fs_list_where "" tasks "$(eq_filter tenant_id "${TENANT}")" "tenant_id" "${WORK}/tasks.jsonl" \
    || die "could not list tasks; that is a failed read, not none"
  assert_owned "${WORK}/tasks.jsonl" tenant_id tasks
  names_of "${WORK}/tasks.jsonl" >>"${ALL_TASKS}"
  tasks="$(grep -c . "${WORK}/tasks.jsonl" || true)"
  while IFS= read -r task; do
    [[ -n "${task}" ]] || continue
    fresh_token
    n="$(fs_count_under "tasks/${task##*/}" events)" \
      || die "could not count events of ${task##*/}; a failed read, not zero"
    events=$(( events + n ))
    visited=$(( visited + 1 ))
    if [[ $(( visited % 100 )) -eq 0 ]]; then
      dim "  counted events under ${visited}/${tasks} task(s)"
    fi
  done < <(names_of "${WORK}/tasks.jsonl")
  [[ "${visited}" -eq "${tasks}" ]] || die "counted events under ${visited} of ${tasks} tasks"
  fresh_token
  tenant_events="$(fs_count_tenant_events)" \
    || die "could not count events by tenant_id; a failed read, not zero"

  attempts="$(fs_count_where attempts "$(eq_filter tenant_id "${TENANT}")")" \
    || die "could not count attempts; a failed read, not zero"
  workflows="$(fs_count_where workflows "$(eq_filter tenant_id "${TENANT}")")" \
    || die "could not count workflows; a failed read, not zero"
  quota="$(fs_count_where quota "$(eq_filter tenant_id "${TENANT}")")" \
    || die "could not count quota; a failed read, not zero"
  outcome_days="$(fs_count_where outcome_days "$(eq_filter tenant_id "${TENANT}")")" \
    || die "could not count outcome_days; a failed read, not zero"
  auth="$(fs_count_where account_auth "$(eq_filter owner_tenant "${TENANT}")")" \
    || die "could not count account_auth; a failed read, not zero"

  ledger_of_tenant "${WORK}/ledger.present"

  {
    printf '== Inventory: everything tenant %s holds (%s / %s) ==\n' "${TENANT}" "${PROJECT_ID}" "${FIRESTORE_DATABASE}"
    printf '    %-26s %s object version(s), %s byte(s)\n' "${GCS_PREFIX_URL}" "${objects}" "${bytes}"
    printf '    %-26s %s\n' "tasks" "${tasks}"
    printf '    %-26s %s   (under %s task(s))\n' "task events" "${events}" "${visited}"
    printf '    %-26s %s   (any task, listed or not; overlaps the row above)\n' "events by tenant_id" "${tenant_events}"
    printf '    %-26s %s\n' "attempts" "${attempts}"
    printf '    %-26s %s   (all released)\n' "leases" "$(grep -c . "${WORK}/leases.jsonl" || true)"
    printf '    %-26s %s\n' "workflows" "${workflows}"
    printf '    %-26s %s\n' "quota" "${quota}"
    printf '    %-26s %s\n' "outcome_days" "${outcome_days}"
    printf '    %-26s %s\n' "accounts (owned)" "$(grep -c . "${WORK}/accounts.jsonl" || true)"
    printf '    %-26s %s\n' "account_auth" "${auth}"
    printf '    %-26s %s   (listed from the ledger, named for %s)\n' "credential_publications" \
      "$(grep -c . "${WORK}/ledger.present" || true)" "${TENANT}"
    printf '    %-26s %s\n' "pools" "$(pools_to_delete "${WORK}/pools.tsv" | grep -c . || true)"
    while IFS= read -r c; do
      [[ -n "${c}" ]] || continue
      printf '    %-26s left for terraform (managed_by=swarm-terraform)\n' "pools/${c}"
    done < <(pools_for_terraform "${WORK}/pools.tsv")
    if [[ "${TENANT_IS_TERRAFORM}" -eq 1 ]]; then
      printf '    %-26s left for terraform (managed_by=swarm-terraform)\n' "tenants/${TENANT}"
    else
      printf '    %-26s 1   (deleted LAST, after the proof)\n' "tenants/${TENANT}"
    fi
  } >"${table}"
  emit "${table}"
}

# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

delete_objects() {
  local log="${WORK}/gcs-rm.log" removed
  step "Deleting ${GCS_PREFIX_URL}"
  # Stated once more, next to the command that could do the most damage: the
  # URL is the bucket, `tenants/`, a validated non-empty id, and a slash.
  # `gcloud storage rm --recursive gs://<bucket>/` deletes the BUCKET.
  [[ "${GCS_PREFIX_URL}" == "gs://${ARTIFACT_BUCKET}/tenants/${TENANT}/" && -n "${TENANT}" ]] \
    || die "refusing to delete ${GCS_PREFIX_URL}: not gs://${ARTIFACT_BUCKET}/tenants/<id>/"
  if gcloud storage rm --recursive --all-versions "${GCS_PREFIX_URL}" --project "${PROJECT_ID}" \
       >"${log}" 2>&1; then
    # One `Removing gs://...` line per object version, after a `Removing
    # objects:` header that is not one.
    removed="$(grep -c '^Removing gs://' "${log}" || true)"
    ok "gcloud storage rm finished: ${removed} object version(s) removed"
    return 0
  fi
  if grep -q 'matched no objects' "${log}"; then
    ok "nothing under ${GCS_PREFIX_URL} (already empty)"
    return 0
  fi
  die_if_auth_failure "$(cat "${log}")"
  err "gcloud storage rm failed under ${GCS_PREFIX_URL}; this is NOT confirmation the objects are gone:"
  redact <"${log}" | tail -n 5 | sed 's/^/     /' >&2
  die "stopped before touching Firestore. The run is safe to repeat."
}

# delete_where COLLECTION FIELD LABEL -- list, check ownership, delete.
delete_where() {
  local collection="$1" field="$2" label="$3" listing="${WORK}/${1}.del.jsonl"
  fs_list_where "" "${collection}" "$(eq_filter "${field}" "${TENANT}")" "${field}" "${listing}" \
    || die "could not list ${collection}; a failed read, not none"
  assert_owned "${listing}" "${field}" "${collection}"
  names_of "${listing}" >"${WORK}/${collection}.del.names"
  commit_deletes "${WORK}/${collection}.del.names" "${label}"
}

delete_records() {
  local task n left stray tasks_listing="${WORK}/tasks.del.jsonl" events_listing="${WORK}/events.del.jsonl"
  local visited=0 total events_deleted=0 kept=0
  step "Deleting Firestore records"
  fresh_token

  # Events by tenant_id, across every task -- the ones under a task an earlier
  # run deleted included. First, so the per-task pass below mostly finds
  # nothing left to do.
  fs_list_tenant_events "${WORK}/tenant-events.del.jsonl" \
    || die "could not list events by tenant_id; a failed read, not none"
  assert_tenant_events "${WORK}/tenant-events.del.jsonl"
  names_of "${WORK}/tenant-events.del.jsonl" >"${WORK}/tenant-events.del.names"
  commit_deletes "${WORK}/tenant-events.del.names" "events by tenant_id"

  # Tasks: each task's events (whatever tenant_id they carry), then a recount,
  # and the task goes only if the recount is zero. A task deleted with an event
  # under it takes the only way back to that event with it.
  fs_list_where "" tasks "$(eq_filter tenant_id "${TENANT}")" "tenant_id" "${tasks_listing}" \
    || die "could not list tasks; a failed read, not none"
  assert_owned "${tasks_listing}" tenant_id tasks
  names_of "${tasks_listing}" >>"${ALL_TASKS}"
  total="$(grep -c . "${tasks_listing}" || true)"
  : >"${WORK}/tasks.del.names"
  : >"${WORK}/tasks.kept.names"
  while IFS= read -r task; do
    [[ -n "${task}" ]] || continue
    fs_list_where "tasks/${task##*/}" events null "" "${events_listing}" \
      || die "could not list events of ${task##*/}; a failed read, not none"
    stray="$(jq -r --arg p "${task}/events/" \
      'select(((.name | startswith($p)) | not) or ((.name | ltrimstr($p)) | contains("/"))) | .name' \
      "${events_listing}")" || die "could not read the events listing of ${task##*/}"
    [[ -z "${stray}" ]] || die "the events listing of ${task##*/} returned documents outside it; refusing"
    n="$(grep -c . "${events_listing}" || true)"
    if [[ "${n}" -gt 0 ]]; then
      names_of "${events_listing}" >"${WORK}/events.del.names"
      commit_deletes "${WORK}/events.del.names" "events of ${task##*/}" quiet
      events_deleted=$(( events_deleted + n ))
    fi
    fresh_token
    left="$(fs_count_under "tasks/${task##*/}" events)" \
      || die "could not recount the events of ${task##*/}; a failed read, not zero"
    if [[ "${left}" -eq 0 ]]; then
      printf '%s\n' "${task}" >>"${WORK}/tasks.del.names"
    else
      printf '%s\t%s\n' "${task##*/}" "${left}" >>"${WORK}/tasks.kept.names"
      kept=$(( kept + 1 ))
    fi
    visited=$(( visited + 1 ))
    if [[ $(( visited % 25 )) -eq 0 || "${visited}" -eq "${total}" ]]; then
      printf '    %-26s %s/%s task(s), %s event(s) deleted\n' "task events" "${visited}" "${total}" "${events_deleted}" >&2
    fi
  done < <(names_of "${tasks_listing}")
  [[ "${visited}" -eq "${total}" ]] || die "went through ${visited} of ${total} tasks"
  if [[ "${kept}" -gt 0 ]]; then
    warn "keeping ${kept} task document(s): each still has events under it after their delete, and deleting it would leave those events where no task listing reaches them. The proof below fails on them; run again."
    head -n 5 "${WORK}/tasks.kept.names" | awk -F'\t' '{printf "     tasks/%s  %s event(s) left\n", $1, $2}' >&2
  fi
  commit_deletes "${WORK}/tasks.del.names" "tasks"

  delete_where attempts tenant_id attempts

  # Leases: listed with released_at, and checked AGAIN at the delete. Deleting
  # an unreleased lease loses the capacity it holds in every pool it names.
  fs_list_where "" leases "$(eq_filter tenant_id "${TENANT}")" "tenant_id,released_at" "${WORK}/leases.del.jsonl" \
    || die "could not list leases; a failed read, not none"
  assert_owned "${WORK}/leases.del.jsonl" tenant_id leases
  n="$(jq -s '[.[] | select((.fields.released_at // {nullValue: null}) | has("nullValue"))] | length' \
    "${WORK}/leases.del.jsonl")"
  [[ "${n}" -eq 0 ]] || die "${n} lease(s) of ${TENANT} became unreleased since the check; stopping. Drain, then run again."
  names_of "${WORK}/leases.del.jsonl" >"${WORK}/leases.del.names"
  commit_deletes "${WORK}/leases.del.names" "leases"

  delete_where workflows tenant_id workflows
  delete_where quota tenant_id quota
  delete_where outcome_days tenant_id outcome_days
  delete_where account_auth owner_tenant account_auth

  # Accounts: owned by this tenant, id `<tenant>:<label>`, nothing assigned.
  fs_list_where "" accounts "$(eq_filter owner_tenant "${TENANT}")" "owner_tenant,assigned" "${WORK}/accounts.del.jsonl" \
    || die "could not list accounts; a failed read, not none"
  assert_owned "${WORK}/accounts.del.jsonl" owner_tenant accounts
  n="$(jq -r --arg p "${DOCS_ROOT}/accounts/${TENANT}:" \
      'select(((.name | startswith($p)) | not) or (((.fields.assigned.integerValue // "0") | tonumber) > 0)) | .name' \
      "${WORK}/accounts.del.jsonl" | grep -c . || true)"
  [[ "${n}" -eq 0 ]] || die "${n} account(s) of ${TENANT} are misnamed or have an agent assigned; stopping"
  names_of "${WORK}/accounts.del.jsonl" >"${WORK}/accounts.del.names"
  commit_deletes "${WORK}/accounts.del.names" "accounts"

  ledger_of_tenant "${WORK}/ledger.del.names"
  commit_deletes "${WORK}/ledger.del.names" "credential_publications"

  # Pools: the tenant's, with no capacity held, less the ones terraform holds.
  list_tenant_pools "${WORK}/pools.del.tsv"
  n="$(awk -F'\t' '$2 + 0 > 0' "${WORK}/pools.del.tsv" | grep -c . || true)"
  [[ "${n}" -eq 0 ]] || die "${n} pool(s) of ${TENANT} hold capacity again; stopping. Drain, then run again."
  pools_to_delete "${WORK}/pools.del.tsv" | sed "s#^#${DOCS_ROOT}/pools/#" >"${WORK}/pools.del.names"
  commit_deletes "${WORK}/pools.del.names" "pools"
}

# ---------------------------------------------------------------------------
# Proof
# ---------------------------------------------------------------------------

PROOF_FAILED=0
PROOF_CHECKS=0

proof_row() {
  local label="$1" found="$2" note="${3:-}"
  printf '    %-34s %s%s\n' "${label}" "${found}" "${note:+   ${note}}" >>"${WORK}/proof.txt"
  PROOF_CHECKS=$(( PROOF_CHECKS + 1 ))
  if [[ "${found}" != "0" ]]; then
    PROOF_FAILED=$(( PROOF_FAILED + 1 ))
  fi
}

# Every count taken again, server side, by a different call from the listing
# that fed the delete: an aggregation for a collection, a fresh listing of the
# ledger, a fresh `gcloud storage ls` for the prefix. Two rows cover events:
# one under each task any listing of this run saw, which catches an event
# without this tenant's id; and one by tenant_id across every task, which
# catches an event whose task is gone -- the one a rerun after a failed proof
# would otherwise count as zero, having no task left to look under.
prove_gone() {
  local c n events=0 visited=0 task tasks_seen
  step "Proof"
  fresh_token
  printf '== Proof: what is left of tenant %s ==\n' "${TENANT}" >"${WORK}/proof.txt"

  gcs_total --all-versions "${GCS_PREFIX_URL}" || die "cannot verify ${GCS_PREFIX_URL}; a failed look is not proof"
  proof_row "${GCS_PREFIX_URL}" "${GCS_OBJECTS}" "object version(s), live and noncurrent"

  for c in tasks attempts leases workflows quota outcome_days; do
    n="$(fs_count_where "${c}" "$(eq_filter tenant_id "${TENANT}")")" \
      || die "could not count ${c}; a failed read is not proof"
    proof_row "${c}" "${n}"
  done

  sort -u "${ALL_TASKS}" >"${WORK}/all-tasks.sorted"
  tasks_seen="$(grep -c . "${WORK}/all-tasks.sorted" || true)"
  while IFS= read -r task; do
    [[ -n "${task}" ]] || continue
    fresh_token
    n="$(fs_count_under "tasks/${task##*/}" events)" \
      || die "could not count events of ${task##*/}; a failed read is not proof"
    events=$(( events + n ))
    visited=$(( visited + 1 ))
  done <"${WORK}/all-tasks.sorted"
  [[ "${visited}" -eq "${tasks_seen}" ]] || die "checked events under ${visited} of ${tasks_seen} tasks"
  proof_row "task events" "${events}" "(under each of ${visited} task(s) this run listed)"
  # The row that does not depend on any task listing: a server-side count of
  # every event carrying this tenant's id, under any task, existing or not.
  fresh_token
  n="$(fs_count_tenant_events)" || die "could not count events by tenant_id; a failed read is not proof"
  proof_row "events by tenant_id" "${n}" "(collection group: any task, listed or not)"

  for c in accounts account_auth; do
    n="$(fs_count_where "${c}" "$(eq_filter owner_tenant "${TENANT}")")" \
      || die "could not count ${c}; a failed read is not proof"
    proof_row "${c}" "${n}"
  done

  # Listed afresh from the ledger, not re-read from the names the delete used.
  ledger_of_tenant "${WORK}/ledger.after"
  proof_row "credential_publications" "$(grep -c . "${WORK}/ledger.after" || true)" \
    "(listed from the ledger, named for ${TENANT})"

  list_tenant_pools "${WORK}/pools.after.tsv"
  proof_row "pools" "$(pools_to_delete "${WORK}/pools.after.tsv" | grep -c . || true)"
  while IFS= read -r c; do
    [[ -n "${c}" ]] || continue
    printf '    %-34s left for terraform (managed_by=swarm-terraform)\n' "pools/${c}" >>"${WORK}/proof.txt"
  done < <(pools_for_terraform "${WORK}/pools.after.tsv")

  printf '    (%s checks)\n' "${PROOF_CHECKS}" >>"${WORK}/proof.txt"
  emit "${WORK}/proof.txt"

  if [[ "${PROOF_FAILED}" -ne 0 ]]; then
    err "VERIFICATION FAILED: ${PROOF_FAILED} of ${PROOF_CHECKS} checks above found something left."
    die "tenants/${TENANT} was NOT deleted, so the id stays held and this run can be repeated. Read the non-zero rows; nothing about this tenant is finished."
  fi
}

# What the bucket still keeps, and for whom. Informational, and run LAST: no
# API deletes a soft-deleted object before the bucket's retention expires, so
# this is a fact to print rather than a check to fail. It runs in a subshell
# so that a lookup failure -- gcs_total can exit on a dead session -- cannot
# turn a finished, proven offboarding into a red exit.
report_soft_deleted() {
  local soft
  if soft="$(gcs_total --soft-deleted "${GCS_PREFIX_URL}" && printf '%s' "${GCS_OBJECTS}")"; then
    if [[ "${soft}" -gt 0 ]]; then
      warn "${soft} object version(s) under ${GCS_PREFIX_URL} are SOFT-deleted: the bucket keeps them for its soft-delete retention (7 days on 2026-09-24), restorable by anyone holding storage.objects.restore on the prefix -- and roles/storage.objectUser, the role a tenant worker holds there, includes it. A re-registration of '${TENANT}' that gets a worker service account inside that window could restore them." 2>&1 \
        | tee -a "${RECORD}" >&2
    else
      printf '    %-34s 0   (soft-deleted)\n' "${GCS_PREFIX_URL}" | tee -a "${RECORD}" >&2
    fi
  else
    warn "could not list soft-deleted objects under ${GCS_PREFIX_URL}; that says nothing either way"
  fi
}

# The Firestore half of the same fact: with point-in-time recovery on, every
# document deleted above can still be read AS OF an earlier time, by id, until
# the database's version retention passes. Read from the database itself on
# every run rather than asserted from a date, and run in a subshell for the
# reason report_soft_deleted gives.
report_point_in_time_recovery() {
  local db pitr retention
  fresh_token
  if db="$(fs_request GET "https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/${FIRESTORE_DATABASE}")" \
       && pitr="$(jq -r '.pointInTimeRecoveryEnablement // "UNKNOWN"' <<<"${db}")" \
       && retention="$(jq -r '.versionRetentionPeriod // "unknown"' <<<"${db}")"; then
    case "${pitr}" in
      POINT_IN_TIME_RECOVERY_ENABLED)
        warn "point-in-time recovery is ON for ${FIRESTORE_DATABASE} (version retention ${retention}): every document deleted above stays readable as of an earlier time until then, by id, to any holder of datastore.entities.get -- which every tenant worker's unconditioned role includes." 2>&1 \
          | tee -a "${RECORD}" >&2 ;;
      POINT_IN_TIME_RECOVERY_DISABLED)
        printf '    %-34s off   (point-in-time recovery)\n' "${FIRESTORE_DATABASE}" | tee -a "${RECORD}" >&2 ;;
      *)
        warn "could not tell whether point-in-time recovery is on for ${FIRESTORE_DATABASE} ('${pitr}'); that says nothing either way" ;;
    esac
  else
    warn "could not read ${FIRESTORE_DATABASE}'s point-in-time recovery setting; that says nothing either way"
  fi
}

release_tenant_id() {
  local doc
  fresh_token
  if [[ "${TENANT_IS_TERRAFORM}" -eq 1 ]]; then
    printf '    %-34s left for terraform (managed_by=swarm-terraform): the tfvars removal deletes it\n' \
      "tenants/${TENANT}" | tee -a "${RECORD}" >&2
    return 0
  fi
  fs_delete "tenants/${TENANT}" || die "could not delete tenants/${TENANT}; everything else is gone and proven, run again to finish"
  doc="$(fs_get "tenants/${TENANT}")" || die "could not read tenants/${TENANT} back; a failed read is not proof"
  if jq -e '.fields' <<<"${doc}" >/dev/null; then
    die "VERIFICATION FAILED: tenants/${TENANT} is still there after its delete"
  fi
  printf '    %-34s absent\n' "tenants/${TENANT}" | tee -a "${RECORD}" >&2
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

step "Offboard tenant ${TENANT}"
info "project      ${PROJECT_ID}"
info "environment  ${ENVIRONMENT}"
info "database     ${FIRESTORE_DATABASE}"
info "objects      ${GCS_PREFIX_URL}"
info "record       ${RECORD}"
if [[ "${APPLY}" -eq 1 ]]; then
  warn "APPLY: this run deletes, after you type the tenant id"
else
  info "DRY RUN (the default): nothing will be deleted; --apply deletes"
fi

fresh_token
require_fs_database offboard
refuse_unless_registered_and_disabled
refuse_if_capacity_held "before anything"
refuse_if_lent_to
inventory

if [[ "${APPLY}" -eq 0 ]]; then
  hr
  ok "dry run complete: nothing was deleted. The inventory above is what --apply deletes."
  exit 0
fi

step "Confirmation"
warn "about to PERMANENTLY delete everything in the inventory above, for tenant ${TENANT} in ${PROJECT_ID}"
dim "  There is no backup, by design: a copy would outlive the tenant, which the owner decision forbids."
confirm "This cannot be undone." "${TENANT}"
if is_production; then
  confirm "This is PRODUCTION. Confirm the project id." "${PROJECT_ID}"
fi

# The prompt could have waited for an hour. Everything that made the run safe
# is checked again now, not trusted from before it.
refuse_unless_registered_and_disabled
refuse_if_capacity_held "after the confirmation"
refuse_if_lent_to

delete_objects
delete_records
prove_gone
release_tenant_id
report_soft_deleted
( report_point_in_time_recovery ) || true

hr
ok "tenant ${TENANT}: every record and object this script owns is gone, and the proof is above (kept in ${RECORD})"
