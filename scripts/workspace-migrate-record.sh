#!/usr/bin/env bash
# Write the `ready`, `migrated` workspace record of a personal tenant whose
# resources Terraform made (docs/workspaces.md §3.3; lane W9 of #847).
#
# WHY. u-bogdan's account, grants, secrets, jobs and documents were made by
# Terraform and are forgotten from its state by terraform/{infra,bootstrap}/
# removed.tf, without being destroyed. From then on nothing knows the tenant is
# a workspace unless `workspaces/u-bogdan` says so, and the submission gate
# (§5) refuses every tenant whose record is not `ready`. The workspace job
# cannot write it: it would try to CREATE what already exists. So this writes
# it, once, and `register-tenant.sh --workspace <id> --mode verify` then reads
# every object back.
#
# A REQUEST MAY ALREADY EXIST, AND ITS ID IS KEPT. On 2026-10-09 the owner
# asked for a workspace in the console before the migration ran, so
# `workspaces/u-bogdan` was `requested` with an id and a `workspace_ids/`
# entry. A fresh id would orphan both. A `requested`, `failed` or `denied`
# record is therefore completed IN PLACE; only a missing one is created, with a
# fresh id; a record already `ready` and `migrated` is left alone (a re-run is
# a no-op); anything else (approved, applying, needs_owner, a ready record the
# job made) is refused, because a build may be making the same resources.
#
# THE WRITE IS swarm_api.workspaces.Workspaces.migrate, run with the
# platform's python, in one Firestore transaction with its `admin_audit`
# entry. The record's shape is stated there and nowhere else: this script
# reads the namespace's live quota, prints the plan, asks, and calls it.
#
# WHAT IT READS. The record, and `tenants/<tenant>` for the principal, the live
# `max_active` and `capacity_units` and the provider keys (inside migrate).
# The namespace's ResourceQuota (`swarm-tenant-quota`) for `quota_pods` and
# `quota_cpu`, through the swarm cluster's own context only, unless both are
# given as flags: the verify run compares the record with all four, so the
# record must say what is live, not what a default says.
#
# DRY RUN BY DEFAULT. Without --apply it prints the record before and after,
# redacted, and writes nothing. With --apply it prints the same plan, then
# needs the tenant id TYPED at a terminal (SWARM_ASSUME_YES is ignored), and
# writes only if the record is still in the state the plan was read from.
#
# Usage:
#   scripts/workspace-migrate-record.sh u-bogdan              # dry run
#   scripts/workspace-migrate-record.sh u-bogdan --apply      # write, after typing u-bogdan
#   scripts/workspace-migrate-record.sh u-bogdan --quota-pods 100 --quota-cpu 400
#
# Exit: 0 written, nothing to do, or a dry run; 1 refused or failed (nothing
# written); 2 usage.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# common.sh's confirm() skips the prompt when SWARM_ASSUME_YES is set. This
# makes a person's workspace `ready` without the job's evidence, so the tenant
# id is always typed at a terminal.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: migrating a workspace record always needs the tenant id typed at a terminal"
  unset SWARM_ASSUME_YES
fi

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/workspace-migrate-record.sh <tenant> [--apply] [--quota-pods N --quota-cpu N] [--allow-prod]

  <tenant>        the personal tenant, u-<name> (for example u-bogdan)
  --apply         write the record, after typing the tenant id; without it, a
                  dry run that prints the plan and writes nothing
  --dry-run       the default, accepted so a command line can say so
  --quota-pods N  the namespace ResourceQuota's pods, instead of reading it
  --quota-cpu N   the namespace ResourceQuota's requests.cpu, instead of reading it
  --allow-prod    required when ENVIRONMENT is prod
USAGE
}

TENANT=""
APPLY=0
SAID_DRY_RUN=0
ALLOW_PROD=0
QUOTA_PODS=""
QUOTA_CPU=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)      APPLY=1; shift ;;
    --dry-run|-n) SAID_DRY_RUN=1; shift ;;
    --allow-prod) ALLOW_PROD=1; shift ;;
    --quota-pods) [[ $# -ge 2 ]] || { usage; exit 2; }; QUOTA_PODS="$2"; shift 2 ;;
    --quota-cpu)  [[ $# -ge 2 ]] || { usage; exit 2; }; QUOTA_CPU="$2"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    -*)           usage; err "unknown argument: $1"; exit 2 ;;
    *)
      [[ -z "${TENANT}" ]] || { usage; err "one tenant at a time; got '${TENANT}' and '$1'"; exit 2; }
      TENANT="$1"; shift ;;
  esac
done
[[ -n "${TENANT}" ]] || { usage; err "name the tenant, for example u-bogdan"; exit 2; }
if [[ "${APPLY}" -eq 1 && "${SAID_DRY_RUN}" -eq 1 ]]; then
  die "--apply and --dry-run contradict each other; say which one you mean"
fi

# A personal tenant id as swarm_common.identity.tenant_id_for_user makes one;
# migrate() checks the principal derives to it.
[[ "${TENANT}" =~ ^u-[a-z0-9][a-z0-9-]{0,40}$ ]] \
  || die "'${TENANT}' is not a personal tenant id (u-<name>); only those are migrated. Nothing was read or written."
is_shared_resource "${TENANT}" && die "'${TENANT}' is on the shared deny-list; refusing"

case "${FIRESTORE_DATABASE}" in
  ''|'(default)'|default)
    die "refusing the (default) Firestore database; it belongs to the rest of this shared project" ;;
esac
if is_production && [[ "${ALLOW_PROD}" -eq 0 ]]; then
  die "ENVIRONMENT is '${ENVIRONMENT}'; migrating a production workspace record requires --allow-prod"
fi

require_cmd jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-ws-migrate.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

whole() { [[ "$1" =~ ^[1-9][0-9]{0,5}$ ]]; }

# --- the namespace's live quota -------------------------------------------------

if [[ -n "${QUOTA_PODS}" || -n "${QUOTA_CPU}" ]]; then
  [[ -n "${QUOTA_PODS}" && -n "${QUOTA_CPU}" ]] \
    || die "give both --quota-pods and --quota-cpu, or neither (then both are read from the namespace)"
  whole "${QUOTA_PODS}" || die "--quota-pods '${QUOTA_PODS}' is not a whole number above 0"
  whole "${QUOTA_CPU}" || die "--quota-cpu '${QUOTA_CPU}' is not a whole number above 0"
  info "quota          pods ${QUOTA_PODS}, cpu ${QUOTA_CPU} (as given)"
else
  # The namespace as a render names it (kubernetes/render.py identity), never
  # derived a second time here; and only through the swarm cluster's context.
  read -r NAMESPACE _ _ < <(python3 "${REPO_ROOT}/kubernetes/render.py" identity --tenant "${TENANT}") \
    || die "could not ask kubernetes/render.py which namespace ${TENANT} has"
  [[ "${NAMESPACE}" == swarm-tenant-* ]] || die "render named namespace '${NAMESPACE}', not a swarm tenant's"
  assert_kube_context
  if ! kc get resourcequota swarm-tenant-quota -n "${NAMESPACE}" -o json >"${WORK}/quota.json" 2>"${WORK}/quota.err"; then
    redact <"${WORK}/quota.err" >&2
    die "could not read ResourceQuota swarm-tenant-quota in ${NAMESPACE}. Nothing was written. Give --quota-pods and --quota-cpu if you have read it another way."
  fi
  QUOTA_PODS="$(jq -r '.spec.hard.pods // ""' "${WORK}/quota.json")"
  QUOTA_CPU="$(jq -r '.spec.hard["requests.cpu"] // ""' "${WORK}/quota.json")"
  # Whole numbers only: the verify run compares them with the record as
  # strings, so "400" reads back and "400000m" would not.
  whole "${QUOTA_PODS}" || die "the live quota's pods is '${QUOTA_PODS}', not a whole number; nothing was written"
  whole "${QUOTA_CPU}" || die "the live quota's requests.cpu is '${QUOTA_CPU}', not a whole number of vCPU; nothing was written"
  info "quota          pods ${QUOTA_PODS}, cpu ${QUOTA_CPU} (read from ${NAMESPACE})"
fi

# --- the write path: Workspaces.migrate ----------------------------------------

# migrate OUT APPLY EXPECT: run Workspaces.migrate with the platform's python;
# its JSON result in OUT. Into a file, never a command substitution, so the
# status is the python's.
migrate() {
  local out="$1" apply="$2" expect="$3" python
  python="$(swarm_python)"
  # shellcheck disable=SC2086  # swarm_python may print `uv run --project <root> python`
  SWARM_MIGRATE_TENANT="${TENANT}" SWARM_MIGRATE_QUOTA_PODS="${QUOTA_PODS}" \
  SWARM_MIGRATE_QUOTA_CPU="${QUOTA_CPU}" SWARM_MIGRATE_APPLY="${apply}" \
  SWARM_MIGRATE_EXPECT="${expect}" \
    ${python} - >"${out}" 2>"${WORK}/migrate.err" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

from google.cloud import firestore

from swarm_api.errors import ApiError
from swarm_api.workspaces import Workspaces

env = os.environ
db = firestore.Client(project=env["PROJECT_ID"], database=env["FIRESTORE_DATABASE"])
try:
    result = Workspaces(db, now=lambda: datetime.now(timezone.utc), gate=False).migrate(
        env["SWARM_MIGRATE_TENANT"],
        quota_pods=int(env["SWARM_MIGRATE_QUOTA_PODS"]),
        quota_cpu=int(env["SWARM_MIGRATE_QUOTA_CPU"]),
        apply=env["SWARM_MIGRATE_APPLY"] == "1",
        expect=env["SWARM_MIGRATE_EXPECT"] or None,
    )
except (ApiError, ValueError) as exc:
    print(f"refused: {exc}", file=sys.stderr)
    sys.exit(3)
json.dump(result, sys.stdout, indent=2, sort_keys=True,
          default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v))
PY
}

# show TITLE FILE JQ_PATH: one document of the result, redacted.
show() {
  printf '\n%s\n' "$1" >&2
  jq "$3" "$2" | redact >&2
}

info "tenant         ${TENANT}"
info "firestore      ${PROJECT_ID}/${FIRESTORE_DATABASE}"
if ! migrate "${WORK}/plan.json" 0 ""; then
  redact <"${WORK}/migrate.err" >&2
  die "the plan was refused or failed; nothing was written"
fi
ACTION="$(jq -r '.action' "${WORK}/plan.json")"
WORKSPACE_ID="$(jq -r '.workspace_id' "${WORK}/plan.json")"
show "before (workspaces/${TENANT}):" "${WORK}/plan.json" '.before'
show "after:" "${WORK}/plan.json" '.after'
printf '\n' >&2

case "${ACTION}" in
  nothing)
    ok "workspaces/${TENANT} is already ready and migrated (${WORKSPACE_ID}); nothing to write"
    exit 0 ;;
  update)
    info "plan: complete the existing record IN PLACE, keeping workspace id ${WORKSPACE_ID}" ;;
  create)
    info "plan: create the record and workspace_ids/ entry with a fresh workspace id (${WORKSPACE_ID} here; drawn again when written)" ;;
  *) die "Workspaces.migrate answered an action this script does not know: '${ACTION}'" ;;
esac

if [[ "${APPLY}" -eq 0 ]]; then
  dim "dry run: nothing was written. To write it: scripts/workspace-migrate-record.sh ${TENANT} --apply"
  exit 0
fi

confirm "This writes workspaces/${TENANT} as ready and migrated (${ACTION}), and one admin_audit entry, in ${PROJECT_ID}/${FIRESTORE_DATABASE}." "${TENANT}"

if ! migrate "${WORK}/result.json" 1 "${ACTION}"; then
  redact <"${WORK}/migrate.err" >&2
  die "the write was refused or failed; the transaction wrote nothing"
fi
WORKSPACE_ID="$(jq -r '.workspace_id' "${WORK}/result.json")"
show "written:" "${WORK}/result.json" '.after'
ok "workspaces/${TENANT} is ready and migrated: ${WORKSPACE_ID} (${ACTION})"
dim "Next (docs/workspaces.md §3.3, step 4): scripts/register-tenant.sh --workspace ${WORKSPACE_ID} --mode verify, under the call guard"
