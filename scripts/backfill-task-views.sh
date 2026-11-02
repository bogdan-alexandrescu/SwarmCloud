#!/usr/bin/env bash
# Fill every task's stored attempt totals and current account (contract
# request 132, part of #2688). One-off, idempotent, dry run by default.
#
# WHY. `Task.attempt_totals` and `Task.current_account` are READ-SIDE VIEWS:
# the writers of an attempt's end and of an account event keep them on the
# task document, so `GET /v1/tasks` serves a 200-row page in about three round
# trips instead of reading every row's attempts and account events. A task
# written before those writers has neither field, and the list derives both
# for it on every page, as it did before. This fills them once.
#
# WHAT IT WRITES. Exactly what the writers would have written, computed by
# swarm-api's own functions (`task_accounts.backfill_task_views`): the totals
# of the task's attempt documents as of its current attempt, and the account
# its current attempt's events leave it on. Only an ABSENT field is written,
# in a transaction that re-reads the task and writes nothing when a writer
# filled it or a newer attempt moved `current_generation` since the plan.
# A task with no attempt document or no account event at its current
# generation is left: the list derives it as before. Attempts and events are
# read in the task's own workspace only.
#
# NOTHING in admission, leasing, the scheduler or capacity reads either
# field: capacity is reserved all-or-nothing from the pool documents and
# concurrency counts from LEASED (CONTRACT.md invariants 2 and 3).
#
# Usage:
#   scripts/backfill-task-views.sh                    # dry run: counts, writes nothing
#   scripts/backfill-task-views.sh --tenant eng       # one workspace
#   scripts/backfill-task-views.sh --apply            # write, after typing the project id
#
# A second run finds nothing left to write and exits 0 without asking.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# common.sh's confirm() skips the prompt when SWARM_ASSUME_YES is set. This
# rewrites production records, so the project id is always typed at a terminal.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: the backfill always needs the project id typed at a terminal"
  unset SWARM_ASSUME_YES
fi

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/backfill-task-views.sh [--tenant <id>] [--apply] [--allow-prod]

  --tenant <id>   one workspace (its document id); without it, every workspace
  --apply         write the missing fields, after typing the project id;
                  without it, a dry run that counts and writes nothing
  --dry-run       the default, accepted so a command line can say so
  --allow-prod    required when ENVIRONMENT is prod
USAGE
}

TENANT=""
APPLY=0
SAID_DRY_RUN=0
ALLOW_PROD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)      APPLY=1; shift ;;
    --dry-run|-n) SAID_DRY_RUN=1; shift ;;
    --allow-prod) ALLOW_PROD=1; shift ;;
    --tenant|-t)  [[ $# -ge 2 ]] || { usage; exit 2; }; TENANT="$2"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage; err "unknown argument: $1"; exit 2 ;;
  esac
done
if [[ "${APPLY}" -eq 1 && "${SAID_DRY_RUN}" -eq 1 ]]; then
  die "--apply and --dry-run contradict each other; say which one you mean"
fi
if [[ -n "${TENANT}" ]]; then
  case "${TENANT}" in
    *[!a-z0-9-]*|'') die "tenant id '${TENANT}' must be lowercase letters, digits and hyphens" ;;
  esac
fi

case "${FIRESTORE_DATABASE}" in
  ''|'(default)'|default)
    die "refusing the (default) Firestore database; it belongs to the rest of this shared project" ;;
esac
if is_production && [[ "${ALLOW_PROD}" -eq 0 ]]; then
  die "ENVIRONMENT is '${ENVIRONMENT}'; backfilling production tasks requires --allow-prod"
fi

require_cmd jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-task-views.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# backfill OUT_FILE APPLY -- run swarm-api's backfill; its JSON report to OUT_FILE.
backfill() {
  local out="$1" apply="$2" python
  python="$(swarm_python)"
  # shellcheck disable=SC2086  # swarm_python may print `uv run --project <root> python`
  SWARM_BACKFILL_TENANT="${TENANT}" SWARM_BACKFILL_APPLY="${apply}" \
    ${python} - >"${out}" 2>"${WORK}/backfill.err" <<'PY'
import json
import os
import sys

from google.cloud import firestore

from swarm_api.task_accounts import backfill_task_views

env = os.environ
db = firestore.Client(project=env["PROJECT_ID"], database=env["FIRESTORE_DATABASE"])
report = backfill_task_views(db, apply=env["SWARM_BACKFILL_APPLY"] == "1",
                             tenant_id=env["SWARM_BACKFILL_TENANT"] or None)
json.dump(report, sys.stdout, indent=2, sort_keys=True, default=str)
PY
}

show() {
  jq -r '
    "  tasks read                        \(.tasks_read)",
    "  attempt totals to write           \(.totals_to_write)",
    "  current accounts to write         \(.accounts_to_write)",
    "  left: no attempt at current gen   \(.left_no_current_attempt)",
    "  left: no account event            \(.left_no_account_event)",
    "  left: could not be read whole     \(.left_unread)",
    "  written                           \(.written)",
    "  refused (changed since read)      \(.refused_changed)"' "$1" | redact >&2
}

info "firestore      ${PROJECT_ID}/${FIRESTORE_DATABASE}"
info "workspaces     ${TENANT:-every workspace}"
if ! backfill "${WORK}/plan.json" 0; then
  redact <"${WORK}/backfill.err" >&2
  die "the plan could not be read; nothing was written"
fi
show "${WORK}/plan.json"

TO_WRITE="$(jq '.totals_to_write + .accounts_to_write' "${WORK}/plan.json")"
if [[ "${TO_WRITE}" -eq 0 ]]; then
  ok "every task that can carry them already has both views; nothing to write"
  exit 0
fi

if [[ "${APPLY}" -eq 0 ]]; then
  jq -r '.sample[] | "  \(.tenant_id)\t\(.task_id)\tgeneration \(.generation)\t\(.fields | join(","))"' \
    "${WORK}/plan.json" | redact >&2
  dim "dry run: nothing was written. To write: scripts/backfill-task-views.sh${TENANT:+ --tenant ${TENANT}} --apply"
  exit 0
fi

confirm "This writes ${TO_WRITE} stored view(s) on task documents in ${PROJECT_ID}/${FIRESTORE_DATABASE}." "${PROJECT_ID}"

if ! backfill "${WORK}/applied.json" 1; then
  redact <"${WORK}/backfill.err" >&2
  die "the backfill stopped part way; run it again: it writes only what is still absent"
fi
show "${WORK}/applied.json"

# The proof is a second plan, not the writes' own answers.
if ! backfill "${WORK}/after.json" 0; then
  redact <"${WORK}/backfill.err" >&2
  die "the check after the writes could not be read; run the dry run again"
fi
LEFT="$(jq '.totals_to_write + .accounts_to_write' "${WORK}/after.json")"
REFUSED="$(jq '.refused_changed' "${WORK}/applied.json")"
if [[ "${LEFT}" -gt "${REFUSED}" ]]; then
  die "${LEFT} view(s) are still absent after the writes; run it again to see which"
fi
ok "wrote $(jq '.written' "${WORK}/applied.json") task document(s); ${LEFT} left to a second run"
