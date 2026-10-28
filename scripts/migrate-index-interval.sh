#!/usr/bin/env bash
# Move every repository registration still storing the old change-interval
# default, `index.min_change_interval_minutes = 30`, to the new one, 120.
# A ONE-TIME MIGRATION (docs/repo-index.md §3.3, "Moving the old default").
#
# WHY. #2524 (BGD, 2026-10-27) raised MIN_CHANGE_INTERVAL_DEFAULT in
# apps/swarm-api/swarm_api/repositories.py from 30 to 120 minutes: 845 index
# runs in the week 2026-10-20..27, mostly change-triggered. But
# `_index_defaults` WRITES the default into every registration it creates, so
# every registration made before #2524 holds a literal 30, and changing the
# constant moved none of them. BGD, 2026-10-28: "a ONE-TIME MIGRATION, not a
# read-time rule. Reads of min_change_interval_minutes stay literal." A rule
# that read a stored 30 as 120 was built and removed: it makes the document
# disagree with what the poll applies, and it cannot tell the old default from
# a person who chose 30. This script changes the data once instead.
#
# WHAT IT CANNOT TELL APART. A registration whose owner chose 30 looks exactly
# like one that took the old default, so it is moved too. It is listed by the
# dry run, and can be set back with PATCH /v1/repositories/{repo_id}
# `{"index": {"min_change_interval_minutes": 30}}`.
#
# DRY RUN BY DEFAULT. Without --apply it lists every registration in the
# `repositories` collection whose `index.min_change_interval_minutes` is
# exactly the integer 30 (repo_id, tenant, owner/repo), prints the count, and
# writes nothing. With --apply it prints the same list, then needs
# `migrate-<count>` TYPED at a terminal (SWARM_ASSUME_YES is ignored), and
# patches each listed document with an updateMask naming ONLY
# `index.min_change_interval_minutes` and a `currentDocument.updateTime`
# precondition: a registration edited between the listing and the write is
# refused by Firestore, reported, and left as it is; run again to pick it up.
# A re-run finds nothing left to move and writes nothing.
#
# THE OPERATOR RUN AGAINST A DEPLOYED ENVIRONMENT WAITS FOR BGD'S GO-AHEAD.
#
# Usage:
#   scripts/migrate-index-interval.sh                 # dry run: list and count
#   scripts/migrate-index-interval.sh --apply         # rewrite, after typing migrate-<count>
#   scripts/migrate-index-interval.sh --apply --allow-prod --environment prod
#
# Exit: 0 dry run, nothing to do, or every listed registration moved;
#       1 refused, failed, or a registration not moved (named on stderr); 2 usage.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# common.sh's confirm() skips the prompt when SWARM_ASSUME_YES is set. This
# rewrites other people's registrations, so the answer is always typed.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: rewriting registrations always needs a typed confirmation"
  unset SWARM_ASSUME_YES
fi

#: The value every registration stored at creation before #2524, and the one
#: it is moved to: repositories.MIN_CHANGE_INTERVAL_DEFAULT, then and now.
FROM_MINUTES=30
TO_MINUTES=120
#: repositories.COLLECTION.
COLLECTION="repositories"
FIELD="index.min_change_interval_minutes"

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/migrate-index-interval.sh [--apply] [--allow-prod] [--environment ENV]

  (no argument)   dry run: list each registration storing 30 and print the count
  --dry-run       the default, accepted so a command line can say so
  --apply         rewrite those 30s to 120, after typing migrate-<count>
  --allow-prod    required when ENVIRONMENT is prod
  --environment   the environment whose Firestore database is read
USAGE
}

APPLY=0
SAID_DRY_RUN=0
ALLOW_PROD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)          APPLY=1; shift ;;
    --dry-run|-n)     SAID_DRY_RUN=1; shift ;;
    --allow-prod)     ALLOW_PROD=1; shift ;;
    --environment|-e) [[ $# -ge 2 ]] || { usage; exit 2; }; ENVIRONMENT="$2"; export ENVIRONMENT; shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *)                usage; err "unknown argument: $1"; exit 2 ;;
  esac
done
if [[ "${APPLY}" -eq 1 && "${SAID_DRY_RUN}" -eq 1 ]]; then
  die "--apply and --dry-run contradict each other; say which one you mean"
fi

case "${FIRESTORE_DATABASE}" in
  ''|'(default)'|default)
    die "refusing the (default) Firestore database; it belongs to the rest of this shared project" ;;
esac
if is_production && [[ "${ALLOW_PROD}" -eq 0 ]]; then
  die "ENVIRONMENT is '${ENVIRONMENT}'; migrating production registrations requires --allow-prod"
fi

require_cmd jq curl

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-index-interval.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

step "Registrations storing ${FIELD} = ${FROM_MINUTES}"
info "project      ${PROJECT_ID}"
info "environment  ${ENVIRONMENT}"
info "database     ${FIRESTORE_DATABASE}"
[[ "${APPLY}" -eq 1 ]] || warn "DRY RUN: nothing will be written"

# Into a file, never a command substitution, so a failed read stops here
# instead of reading as an empty collection.
fs_list "${COLLECTION}" >"${WORK}/pages.jsonl" || die "could not list ${COLLECTION}; nothing was written"

# One TAB-separated row per document storing EXACTLY the integer 30:
#   <document name> <updateTime> <repo_id> <tenant_id> <owner/repo>
# A double 30.0, a string "30" or a missing value is not the old default as
# `_index_defaults` wrote it, so it is not touched.
jq -r --arg from "${FROM_MINUTES}" '
  .[]
  | select(.fields.index.mapValue.fields.min_change_interval_minutes.integerValue == $from)
  | [ .name,
      .updateTime,
      (.name | split("/") | last),
      (.fields.tenant_id.stringValue // "-"),
      ((.fields.owner.stringValue // "?") + "/" + (.fields.repo.stringValue // "?")) ]
  | @tsv' "${WORK}/pages.jsonl" >"${WORK}/rows.tsv"

COUNT=0
while IFS=$'\t' read -r _name _updated repo_id tenant repository; do
  printf '%s\t%s\t%s\n' "${repo_id}" "${tenant}" "${repository}"
  COUNT=$((COUNT + 1))
done <"${WORK}/rows.tsv"
printf '%s registration(s) store %s = %s\n' "${COUNT}" "${FIELD}" "${FROM_MINUTES}"

if [[ "${APPLY}" -eq 0 ]]; then
  info "dry run: nothing was written. --apply rewrites them to ${TO_MINUTES} (operator run waits for BGD's go-ahead)."
  exit 0
fi
if [[ "${COUNT}" -eq 0 ]]; then
  ok "nothing to migrate; nothing was written"
  exit 0
fi

step "Rewrite ${COUNT} registration(s) to ${TO_MINUTES}"
confirm "This rewrites ${FIELD} from ${FROM_MINUTES} to ${TO_MINUTES} on the ${COUNT} registration(s) above, in ${PROJECT_ID}/${FIRESTORE_DATABASE}." \
  "migrate-${COUNT}"
if is_production; then
  confirm "This is PRODUCTION. Confirm the project id." "${PROJECT_ID}"
fi

# Only the masked field is written: `index` carries only that key, and the
# updateMask names only its path, so every other index setting is kept.
FIELDS="$(jq -nc --arg to "${TO_MINUTES}" \
  '{index: {mapValue: {fields: {min_change_interval_minutes: {integerValue: $to}}}}}')"
BASE="$(fs_base)/"
MOVED=0
FAILED=0
while IFS=$'\t' read -r name updated repo_id tenant _repository; do
  doc="${name#*/documents/}"
  if [[ "${doc}" != "${COLLECTION}/"* || "${doc}" == "${name}" ]]; then
    err "${repo_id}: document name '${name}' is not under ${BASE}${COLLECTION}; not written"
    FAILED=$((FAILED + 1))
    continue
  fi
  if fs_patch "${doc}" "${FIELD}" "${FIELDS}" "${updated}" </dev/null; then
    ok "${repo_id} (${tenant}) ${FROM_MINUTES} -> ${TO_MINUTES}"
    MOVED=$((MOVED + 1))
  else
    err "${repo_id} (${tenant}) not moved: changed since it was listed, or the write failed (above). Run again to pick it up."
    FAILED=$((FAILED + 1))
  fi
done <"${WORK}/rows.tsv"

printf '%s of %s registration(s) moved to %s\n' "${MOVED}" "${COUNT}" "${TO_MINUTES}"
[[ "${FAILED}" -eq 0 ]] || die "${FAILED} registration(s) were not moved; nothing else was touched"
