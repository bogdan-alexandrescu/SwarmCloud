#!/usr/bin/env bash
# Register a tenant: a Google group, or one person as a personal fallback tenant.
#
# A tenant is the unit of isolation in this platform, so registering one means
# creating every boundary at once -- there is no "half-registered" state worth
# having:
#
#   * a dedicated Google service account            (identity)
#   * a narrowed Firestore role -- no deletes, no queries -- and NO IAM
#     condition, because Firestore ignores conditions on the data plane and a
#     conditioned binding denies document access outright (docs/security.md)
#   * GCS access conditioned on the tenant's own object prefix, so tenant A's
#     credentials cannot read tenant B's artifacts even by guessing the path
#   * Secret Manager access to that tenant's provider keys only
#   * the full Kubernetes namespace -- NetworkPolicy, ResourceQuota, LimitRange,
#     Pod Security Admission labels, RBAC -- plus the workload-identity binding
#   * a tenants/<id> document and a tenant:<id> slot pool
#
# Re-running is safe for LIMITS and WIRING. It is not a way to re-point a tenant:
# if tenants/<id> already names a different principal or kind, this refuses,
# because one `--group contractors@saga.xyz --tenant eng` would otherwise hand
# every member of contractors@ eng's provider keys, artifacts and budget with no
# prompt and no warning.
#
# NOR IS A RE-RUN THE WAY TO ADD A PROVIDER. It writes the whole tenant record:
# `--providers` REPLACES `credentials`, and max_active, capacity_units and
# display_name go back to their defaults unless they are typed again. Use
# `--add-provider`, which makes the two changes adding one needs and nothing
# else -- see section A below.
#
# ORDER, FOR A TENANT TERRAFORM WILL MANAGE (#334). This script creates the
# worker account first; the owner then applies terraform/bootstrap's grant of
# roles/iam.serviceAccountAdmin on it to the release deployer -- FROM MAIN, with
# the pull request's dev.tfvars copied out by `git show` and passed as
# -var infra_tenants_tfvars, so the branch contributes data and never code;
# only then does the release that adds the tenant to
# terraform/environments/dev/dev.tfvars run. The release sets the account's IAM
# in the same apply, and the deployer holds that role per account, never on the
# project. Section 2b checks the grant and prints the commands.
#
# AN ACCOUNT THAT ALREADY EXISTED IS INSPECTED, AND REFUSED IF SOMEBODY ELSE
# COULD HOLD IT. terraform adopts an existing worker account
# (create_ignore_already_exists), so an account made first by anyone else would
# become the tenant's identity with their IAM policy and keys. Section 2b stops
# the registration when the existing account has an IAM binding the platform
# does not make, a conditioned binding, or a user-managed key.
#
# NAMING. The identity created here is `swarm-agent-worker-<tenant>`, which is
# what terraform/modules/tenancy creates, what terraform/modules/cloud_run_jobs
# binds to each (tenant, profile) Job, and what kubernetes/render.py defaults to.
# This script used to create `swarm-t-<tenant>` instead -- an identity nothing
# ever ran as -- so every boundary it granted was granted to an orphan while the
# real worker got none of them, and its `kubectl annotate` actively re-pointed
# the tenant's Kubernetes service account at that orphan.
#
# The tenant id is DERIVED from the principal by swarm_common.identity, which is
# the same function the API uses to resolve a caller. `--tenant` is an assertion,
# not a choice: it must equal the derived id or this refuses, because any other
# id registers boundaries under a name nothing ever resolves to.
#
# Usage:
#   scripts/register-tenant.sh --group eng@saga.xyz
#   scripts/register-tenant.sh --user alice@saga.xyz
#   scripts/register-tenant.sh --group eng@saga.xyz --providers anthropic,openai \
#                              --max-active 40 --capacity-units 80 --budget 2000
#   scripts/register-tenant.sh --group eng@saga.xyz --dry-run
#   scripts/register-tenant.sh --group eng@saga.xyz --skip-k8s   # Cloud Run only
#
# A person's workspace, from its approved record, under the call guard only --
# the Cloud Run job swarm-workspace-apply runs this, through
# scripts/workspace-apply.sh (section W below, docs/workspaces.md §2.2, §4):
#   scripts/register-tenant.sh --workspace w-3f9a2c                 # --mode create
#   scripts/register-tenant.sh --workspace w-3f9a2c --mode limits   # a ceiling change
#   scripts/register-tenant.sh --workspace w-3f9a2c --mode verify   # A9 only
#
# Add one provider to a tenant that is already registered, keeping the others:
#   scripts/register-tenant.sh --tenant eng --add-provider git
#   scripts/register-tenant.sh --group eng@saga.xyz --add-provider openai --dry-run
# `git` (here or in --providers) also lets swarm-api read that one -git secret,
# for the issue preview and an issue run's write-back to its issue (which needs
# the token to hold `issues: write` and `pull_requests: write`, plus
# `checks: read` and `actions: read`: docs/multi-tenancy.md, "What the forge
# credential must be allowed"); see FORGE_READER_ID below.
#
# `git-merge` and `git-review`, the #295 GitHub App keys, are RETIRED (owner
# decision MS0-Q4, 2026-10-06): the merge, post-verdict and review accounts
# that alone were to read them are gone, so nothing reads either key. Both are
# refused on every path, before anything is read.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/forge-slot.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/forge-slot.sh"

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
ADD_PROVIDER=""
ADD_PROVIDER_GIVEN=0
# The flags only a full registration reads, as typed. --add-provider refuses
# them rather than ignoring them: an operator who typed `--max-active 5` meant
# it, and a run that quietly did not apply it is worse than one that stops.
FULL_ONLY_FLAGS=""
# --workspace (section W): the opaque id of an approved personal workspace, and
# which of its three runs to make. Everything else comes from its record.
WORKSPACE_ID=""
WORKSPACE_GIVEN=0
WORKSPACE_MODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --group|-g)        GROUP="$2"; shift 2 ;;
    --user|-u)         USER_EMAIL="$2"; shift 2 ;;
    --tenant|-t)       TENANT_ID="$2"; shift 2 ;;
    --providers|-p)    PROVIDERS_CSV="$2"; FULL_ONLY_FLAGS+=" $1"; shift 2 ;;
    --max-active)      MAX_ACTIVE="$2"; FULL_ONLY_FLAGS+=" $1"; shift 2 ;;
    --capacity-units)  CAPACITY_UNITS="$2"; FULL_ONLY_FLAGS+=" $1"; shift 2 ;;
    --budget)          BUDGET="$2"; FULL_ONLY_FLAGS+=" $1"; shift 2 ;;
    --display-name)    DISPLAY_NAME="$2"; FULL_ONLY_FLAGS+=" $1"; shift 2 ;;
    --skip-k8s)        SKIP_K8S=1; FULL_ONLY_FLAGS+=" $1"; shift ;;
    --add-provider)    ADD_PROVIDER="$2"; ADD_PROVIDER_GIVEN=1; shift 2 ;;
    --dry-run|-n)      DRY_RUN=1; shift ;;
    --workspace)       WORKSPACE_ID="$2"; WORKSPACE_GIVEN=1; shift 2 ;;
    --mode)            WORKSPACE_MODE="$2"; shift 2 ;;
    # The header above `set -euo pipefail`, however long it grows.
    -h|--help)         awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# --- W. --workspace: one person's workspace, from its approved record --------
#
# docs/workspaces.md §4 (lane W6 of #847). A person's workspace -- their worker
# account, its own IAM, their artifact prefix, their forge slot, their
# namespace and their tenant and pool documents -- is made by THIS script, run
# by one Cloud Run job, swarm-workspace-apply (images/workspace-apply/entry.py
# scrubs its environment, scripts/workspace-apply.sh installs the guard), as
# swarm-workspace-deployer after an admin approves the request in People. That
# identity holds project-wide account-IAM power Google cannot narrow (§2.4), so
# this path differs from --group and --user in what it reads and what it may do:
#
#   * IT TAKES THE OPAQUE WORKSPACE ID AND NOTHING ELSE. Every name comes from
#     the approved record `workspaces/<tenant>` in Firestore, and the tenant id
#     is RE-DERIVED from the record's principal by the frozen
#     swarm_common.identity, so an edited record cannot point the job at
#     somebody else's resources. --group, --user, --tenant, --providers and
#     every other operator flag are refused beside it.
#   * IT RUNS ONLY UNDER THE CALL GUARD (§2.5). Every gcloud, kubectl and curl
#     it makes -- and every one kubernetes/apply.sh makes for it -- passes
#     through scripts/lib/guard-bin/ and scripts/lib/workspace-guard.sh, which
#     allows only the calls that create or bind THIS workspace's resources. It
#     refuses to start unless SWARM_CALL_GUARD names the job's expectation file
#     and the three tools resolve to the shims.
#   * A GUARD STOP IS needs_owner, NOT A FAILURE. The guard refuses the call
#     before it reaches Google or the cluster and writes a stop file; this
#     notices it after every step (including a refusal some read swallowed),
#     records needs_owner with the step and the rule, and ends. The owner reads
#     the refused call in the execution's private log (§2.5, "What the owner does
#     then").
#   * IT NEVER REMOVES ANYTHING. The operator path's repair of a stale
#     unconditioned bucket binding is a removal, which the guard refuses always
#     (C9); here a stale binding stops the run for the owner instead.
#   * IT MAKES THE ACT-AS GRANT. The scheduler and the reconciler get
#     roles/iam.serviceAccountUser on the new worker (A4), because the
#     dispatcher creates a tenant's Cloud Run jobs on demand
#     (scheduler/dispatch.py CloudRunJobDispatcher.ensure_job) and needs actAs
#     on the job's account. The --group and --user paths still do not make it
#     (docs/workspaces.md §0); that gap is reported on the pull request.
#   * ITS SQUAT INSPECTION IS NARROWED. An existing account may carry only the
#     four bindings this mode makes -- Workload Identity for the namespace's two
#     KSAs, act-as for the scheduler and the reconciler -- unconditioned, and
#     no user-managed key. The release deployer's grants are allowed only on a
#     record marked `migrated` (u-bogdan, §3.3), whose account Terraform made.
#
# THE STEPS (§2.2, §4.2), each recorded on the record as `steps.<id>` when it
# starts and ends, which is what the console's progress view reads:
#
#   A1 claim      read the record, check it, write the guard's expectation,
#                 and take the record in one Firestore transaction
#   A2 name free  inspect an existing worker account (read only)
#   A3 identity   create the worker account if absent
#   A4 its IAM    Workload Identity x2 and act-as x2, each read first
#   A5 access     the two tenants/<tenant>/ bucket grants and the .tenant marker
#   A6 forge slot the empty slot and its -refresh twin, the worker on the slot
#   A7 namespace  kubernetes/apply.sh with the record's quota (§8)
#   A8 limits     the tenant and pool documents with the record's limits
#   A9 verify     re-read every object; only then `ready`
#
# --mode create runs A1-A9; --mode limits runs A1, A7 and A8 (a ceiling change:
# the namespace quota and the documents, no IAM call); --mode verify runs A1
# and A9 (the u-bogdan migration, and an admin's check from People).
#
# WHAT A1 ADMITS. create: a record in `approved`; or `failed` / `needs_owner`
# with an admin's retry recorded AFTER the last run's claim; or `applying` whose
# run is dead (claimed over an hour ago and never finished). limits: `ready`.
# verify: `ready` or `failed`. Every mode needs `decision.verdict == approved`
# by an email that is an admin in admin_roles/ NOW (a revoked admin's approval
# no longer starts anything), except verify on a `migrated` record, which no
# admin approved because it predates approval. A retry is the record's
# `retry: {by, at}` map, written by swarm-api's admin retry route (lane W7) and
# checked here the same way. A live run of the same workspace (claimed within
# the hour and not finished) means this build exits 0 having written nothing;
# so does a create for a workspace already `ready`, which is what the dispatch
# sweep's late re-publish finds. Anything else refused at A1 writes nothing and
# exits 1: before A1 reads the record the guard allows no write at all, and a
# forged message for an unapproved record must not move it to `failed`.
#
# ON FAILURE a step's code (§4.3) goes on the record as
# `failure: {step, code, retryable, at}` -- never command output; the console
# serves the copy from the code -- and the state becomes `failed`, except in
# --mode limits: a ceiling change makes no IAM call and touches none of what
# A9 verified, so a failed one leaves the workspace `ready` with the failure
# recorded rather than refusing the person's work over a quota number.
#
# Exit codes: 0 done (or nothing to do), 1 refused or failed, 3 stopped for the
# owner (needs_owner). scripts/workspace-apply.sh reads 3 as "ended, not
# failed" (§2.2).

WS_ID_RE='^w-[0-9a-f]{6}$'
WS_TENANT_RE='^u-[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
# Lower-cased, as swarm-api stores a principal; narrow enough that an address
# is safe in a Firestore document path and a jq string.
WS_EMAIL_RE='^[a-z0-9._+-]+@[a-z0-9.-]+$'
# The run's id, BUILD_ID: in the job, the Cloud Run execution's name, which
# images/workspace-apply/entry.py checks against this same rule before it
# passes it on (tests/unit/scripts/test_workspace_apply_entry.py holds the two
# equal). Kept on one line, in single quotes, for that test.
WS_BUILD_RE='^[A-Za-z0-9._:-]{1,80}$'
# §8, decided by the owner 2026-10-08 (WD5): 8 agents and 8 capacity units (a
# pool of 8), and a namespace of 16 pods and 64 vCPU. 8 people at the ceiling
# hold 64 of claude-code's 80; with requests == limits, 16 pods / 64 vCPU is 8
# claude-code pods at 4 vCPU plus headroom for jobs finishing inside their TTL.
WS_DEFAULT_MAX_ACTIVE=8
WS_DEFAULT_CAPACITY_UNITS=8
# The quota scales with the ceiling, 2 pods and 8 vCPU per agent (§6.4); the
# other three quota lines keep §8's ratio to those two: 128Gi for 64 vCPU, 64
# jobs and 160Gi of ephemeral storage for 16 pods.
WS_PODS_PER_AGENT=2
WS_CPU_PER_AGENT=8
WS_MEMORY_GI_PER_CPU=2
WS_JOBS_PER_POD=4
WS_EPHEMERAL_GI_PER_POD=10
# A run claimed longer ago than this and never finished is dead, and its record
# may be claimed again. Twice the job's deadline (1800 seconds, entry.py's own,
# whatever timeout an execution was given), so no live execution is ever
# mistaken for a dead one.
WS_LIVE_RUN_SECONDS=3600
# §2.2: a transient failure is retried 3 times, after 4, 16 and 64 seconds.
# The unit tests set this to "0 0 0"; nothing else should.
WS_RETRY_DELAYS="${SWARM_WORKSPACE_RETRY_DELAYS:-4 16 64}"
WS_STOPPED_RC=3
WS_KSAS=(swarm-agent-worker swarm-worker)

WS_WORK=""
WS_KUBECTL=""
WS_CONTEXT=""
WS_LAST_ERR=""

# The record, as A1 read it.
WS_RECORD_ID=""
WS_REC_TENANT=""
WS_PRINCIPAL=""
WS_STATE=""
WS_VERDICT=""
WS_DECIDED_BY=""
WS_RETRY_BY=""
WS_RETRY_AT=""
WS_RUN_CLAIMED_AT=""
WS_RUN_FINISHED_AT=""
WS_RUN_ATTEMPT=0
WS_MIGRATED="false"
WS_REQUEST_ID=""
WS_MAX_ACTIVE=""
WS_CAPACITY_UNITS=""
WS_QUOTA_PODS=""
WS_QUOTA_CPU=""

# The names, from the guard's expectation once A1 has written it.
WS_TENANT=""
WS_DOC=""
WS_DOCS_PREFIX=""
WS_WORKER_ID=""
WS_WORKER_EMAIL=""
WS_NAMESPACE=""
WS_SCHEDULER=""
WS_RECONCILER=""
WS_BUCKET_URL=""
WS_MARKER_URL=""
WS_GCS_PREFIX=""
WS_READ_EXPR=""
WS_WRITE_EXPR=""
WS_FORGE_SLOT=""
WS_FORGE_TWIN=""
WS_FIRESTORE_ROLE=""
WS_META_ROLE=""
WS_FALLBACK="false"

# The object kinds and names a tenant render holds, one `Kind<TAB>name` line
# each, read from render.py's YAML without a YAML library (the job's image has
# none): a document's top-level `kind:` and its metadata's own `name:`.
WS_OBJECTS_PY='
import sys
kind = name = None
in_meta = False
def emit():
    if kind and name:
        print(kind + "\t" + name)
for raw in sys.stdin.read().splitlines() + ["---"]:
    line = raw.rstrip()
    if line == "---":
        emit()
        kind = name = None
        in_meta = False
        continue
    if not line or line.lstrip().startswith("#"):
        continue
    if not line.startswith(" "):
        in_meta = line == "metadata:"
        if line.startswith("kind:"):
            kind = line.split(":", 1)[1].strip().strip("\"\x27")
        continue
    if in_meta and line.startswith("  name:") and not line.startswith("   "):
        name = line.split(":", 1)[1].strip().strip("\"\x27")
'

ws_stopped() { [[ -n "${SWARM_CALL_GUARD:-}" && -e "${SWARM_CALL_GUARD}.stop" ]]; }

ws_expect() {
  jq -r --arg k "$1" '.[$k] // "" | if type == "string" then . else tojson end' "${SWARM_CALL_GUARD}"
}

# The job never runs unguarded: the expectation file must be there, private,
# outside the checkout and naming this workspace, and gcloud, kubectl and curl
# must all resolve to scripts/lib/guard-bin/.
ws_require_guard() {
  [[ -n "${SWARM_CALL_GUARD:-}" ]] \
    || die "--workspace runs only under the call guard (docs/workspaces.md §2.5): SWARM_CALL_GUARD is not set.
  The build's guard step sets it and writes the file with scripts/lib/workspace-guard.sh init."
  [[ -f "${SWARM_CALL_GUARD}" && -r "${SWARM_CALL_GUARD}" ]] \
    || die "SWARM_CALL_GUARD names ${SWARM_CALL_GUARD}, which is not a readable file; write it first with
  scripts/lib/workspace-guard.sh init --workspace-id ${WORKSPACE_ID}"
  case "${SWARM_CALL_GUARD}" in
    "${REPO_ROOT}"/*) die "the guard's expectation file must live outside the checkout, not at ${SWARM_CALL_GUARD}" ;;
  esac
  ! ws_stopped || die "${SWARM_CALL_GUARD}.stop already exists: a refused call has stopped this job already. Nothing was changed."
  local named
  named="$(jq -r '.workspace_id // ""' "${SWARM_CALL_GUARD}" 2>/dev/null || true)"
  [[ "${named}" == "${WORKSPACE_ID}" ]] \
    || die "the guard's expectation names workspace '${named}', not ${WORKSPACE_ID}; one job guards one workspace"
  local guard_dir tool resolved
  guard_dir="$(cd -- "${SWARM_LIB_DIR}/guard-bin" && pwd -P)"
  for tool in gcloud kubectl curl; do
    if [[ "${tool}" == "kubectl" ]]; then
      resolved="$(kubectl_bin)"
    else
      resolved="$(command -v "${tool}" 2>/dev/null || true)"
    fi
    [[ -n "${resolved}" && "${resolved}" == */* ]] \
      || die "${tool} does not resolve to a file; put scripts/lib/guard-bin first on PATH"
    [[ "$(cd -- "$(dirname -- "${resolved}")" && pwd -P)" == "${guard_dir}" ]] \
      || die "${tool} resolves to ${resolved}, not the guard's shim in scripts/lib/guard-bin/.
  Put that directory first on PATH; --workspace never runs a tool the guard does not see."
  done
}

ws_code() { printf '%s' "$1" >"${WS_WORK}/code"; }
ws_hold() { printf '%s' "$1" >"${WS_WORK}/hold"; err "stopping for the platform owner: $2"; }
ws_object() { printf '%s' "$1" >"${WS_WORK}/object"; err "verify: a ${1} is missing or not as specified"; }

ws_show_err() {
  [[ -s "${WS_WORK}/call.err" ]] || return 0
  redact <"${WS_WORK}/call.err" | sed -n '1,5p' | sed 's/^/     /' >&2
}

# A failure worth asking again (§2.2): a quota or rate refusal, a 5xx, an
# unavailable or timed-out backend, or IAM's concurrent policy change.
ws_transient() {
  local five='(HTTP|code=|[Ee]rror|status|returned)[ :=]*5[0-9][0-9]'
  case "$1" in
    *"HTTP 429"*|*"code=429"*|*RESOURCE_EXHAUSTED*|*"Too Many Requests"*) return 0 ;;
    *UNAVAILABLE*|*DEADLINE_EXCEEDED*|*"concurrent policy change"*) return 0 ;;
  esac
  [[ "$1" =~ ${five} ]]
}

# ws_call OUT COMMAND...: stdout to OUT, stderr kept in $WS_WORK/call.err, and
# a transient failure asked again after each of WS_RETRY_DELAYS. Never retried
# after a guard stop: the latch refuses every later call anyway.
ws_call() {
  local out="$1" attempt=0 rc delay
  shift
  local -a delays=()
  read -r -a delays <<<"${WS_RETRY_DELAYS}"
  while :; do
    rc=0
    "$@" >"${out}" 2>"${WS_WORK}/call.err" || rc=$?
    [[ "${rc}" -ne 0 ]] || return 0
    ws_stopped && break
    ws_transient "$(cat "${WS_WORK}/call.err")" || break
    [[ "${attempt}" -lt "${#delays[@]}" ]] || break
    delay="${delays[${attempt}]}"
    attempt=$((attempt + 1))
    warn "a transient failure; asking again (${attempt} of ${#delays[@]}) in ${delay}s"
    sleep "${delay}"
  done
  WS_LAST_ERR="$(cat "${WS_WORK}/call.err")"
  return "${rc}"
}

# ws_probe OUT COMMAND...: 0 present, 1 absent (the API said NOT_FOUND), 2 could
# not tell -- the reason printed. The tri-state of common.sh's _shared_probe,
# with the retry above.
ws_probe() {
  if ws_call "$@"; then return 0; fi
  ws_stopped && return 2
  if gcloud_not_found "${WS_LAST_ERR}"; then return 1; fi
  ws_show_err
  return 2
}

# --- the record ----------------------------------------------------------------

ws_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# ws_field FILE PATH -> one decoded field of a Firestore document, "" if absent.
ws_field() {
  jq -r "${FS_JQ} doc | (${2}) // \"\" | if type == \"string\" then . else tojson end" "$1"
}

ws_load_record() {
  local file="$1"
  WS_RECORD_ID="$(jq -r '(.name // "") | split("/") | last' "${file}")"
  WS_REC_TENANT="$(ws_field "${file}" '.tenant_id')"
  WS_PRINCIPAL="$(ws_lower "$(ws_field "${file}" '.principal')")"
  WS_STATE="$(ws_field "${file}" '.state')"
  WS_VERDICT="$(ws_field "${file}" '.decision.verdict')"
  WS_DECIDED_BY="$(ws_lower "$(ws_field "${file}" '.decision.by')")"
  WS_RETRY_BY="$(ws_lower "$(ws_field "${file}" '.retry.by')")"
  WS_RETRY_AT="$(ws_field "${file}" '.retry.at')"
  WS_RUN_CLAIMED_AT="$(ws_field "${file}" '.run.claimed_at')"
  WS_RUN_FINISHED_AT="$(ws_field "${file}" '.run.finished_at')"
  WS_RUN_ATTEMPT="$(ws_field "${file}" '.run.attempt')"
  [[ "${WS_RUN_ATTEMPT}" =~ ^[0-9]+$ ]] || WS_RUN_ATTEMPT=0
  WS_MIGRATED="$(ws_field "${file}" '.migrated')"
  [[ "${WS_MIGRATED}" == "true" ]] || WS_MIGRATED="false"
  WS_REQUEST_ID="$(ws_field "${file}" '.request_id')"
  WS_MAX_ACTIVE="$(ws_field "${file}" '.limits.max_active')"
  WS_CAPACITY_UNITS="$(ws_field "${file}" '.limits.capacity_units')"
  WS_QUOTA_PODS="$(ws_field "${file}" '.limits.quota_pods')"
  WS_QUOTA_CPU="$(ws_field "${file}" '.limits.quota_cpu')"
}

# Seconds since an RFC 3339 timestamp, or nothing when it does not parse.
ws_age() {
  jq -nr --arg t "$1" '($t | sub("\\.[0-9]+"; "") | fromdateiso8601) as $s | (now - $s) | floor' 2>/dev/null || true
}

# True when A is strictly later than B (B empty counts as "never").
ws_later() {
  [[ -n "$1" ]] || return 1
  [[ -n "$2" ]] || return 0
  jq -ne --arg a "$1" --arg b "$2" \
    '($a | sub("\\.[0-9]+"; "") | fromdateiso8601) > ($b | sub("\\.[0-9]+"; "") | fromdateiso8601)' >/dev/null 2>&1
}

# A run that was claimed and has not finished is live for WS_LIVE_RUN_SECONDS.
# An unreadable claim time counts as live: never take a record from under a
# build that may still be running.
ws_run_live() {
  [[ -n "${WS_RUN_CLAIMED_AT}" && -z "${WS_RUN_FINISHED_AT}" ]] || return 1
  local age
  age="$(ws_age "${WS_RUN_CLAIMED_AT}")"
  [[ "${age}" =~ ^-?[0-9]+$ ]] || return 0
  [[ "${age}" -lt "${WS_LIVE_RUN_SECONDS}" ]]
}

# Prints "" when this mode may claim the record as read, else "CODE why".
ws_admit() {
  if ws_run_live; then
    printf 'RUN_IN_PROGRESS another run claimed this workspace at %s and has not finished' "${WS_RUN_CLAIMED_AT}"
    return 0
  fi
  case "${WORKSPACE_MODE}" in
    create)
      case "${WS_STATE}" in
        approved|applying) ;;
        failed|needs_owner)
          if ! ws_later "${WS_RETRY_AT}" "${WS_RUN_CLAIMED_AT}"; then
            printf 'NOT_RETRIED the record is %s and no admin has asked for a retry since the last run' "${WS_STATE}"
            return 0
          fi ;;
        ready) printf 'ALREADY_READY the workspace is ready; there is nothing to create'; return 0 ;;
        *) printf 'WORKSPACE_NOT_APPROVED the record is %s, not approved' "${WS_STATE:-without a state}"; return 0 ;;
      esac ;;
    limits)
      [[ "${WS_STATE}" == "ready" ]] \
        || { printf 'NOT_READY a ceiling change needs a ready workspace; this one is %s' "${WS_STATE:-without a state}"; return 0; } ;;
    verify)
      case "${WS_STATE}" in
        ready|failed) ;;
        *) printf 'NOT_VERIFIABLE only a ready or failed workspace is verified; this one is %s' "${WS_STATE:-without a state}"; return 0 ;;
      esac ;;
  esac
  if [[ "${WORKSPACE_MODE}" == "verify" && "${WS_MIGRATED}" == "true" ]]; then
    return 0
  fi
  [[ "${WS_VERDICT}" == "approved" ]] \
    || { printf 'WORKSPACE_NOT_APPROVED no admin approved this workspace'; return 0; }
  return 0
}

# True when EMAIL is an owner or admin in admin_roles/ now.
ws_is_admin() {
  local email="$1" role
  [[ "${email}" =~ ${WS_EMAIL_RE} ]] || return 1
  FS_ALLOW_404=1 fs_request GET "$(fs_base)/admin_roles/${email}" >"${WS_WORK}/admin.json" || return 1
  jq -e '.fields' "${WS_WORK}/admin.json" >/dev/null 2>&1 || return 1
  role="$(ws_field "${WS_WORK}/admin.json" '.role')"
  [[ "${role}" == "owner" || "${role}" == "admin" ]]
}

ws_derive() {
  python3 - "${REPO_ROOT}" "$1" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common.identity import tenant_id_for_user
print(tenant_id_for_user(sys.argv[2]))
PY
}

# Whole numbers within bounds, or the default the record leaves out.
ws_whole() {
  local value="$1" default="$2" max="$3"
  [[ -n "${value}" ]] || value="${default}"
  [[ "${value}" =~ ^[0-9]+$ ]] || return 1
  value=$((10#${value}))
  [[ "${value}" -ge 1 && "${value}" -le "${max}" ]] || return 1
  printf '%s' "${value}"
}

ws_resolve_limits() {
  local max units pods cpu
  max="$(ws_whole "${WS_MAX_ACTIVE}" "${WS_DEFAULT_MAX_ACTIVE}" 1000)" \
    || die "the record's limits.max_active '${WS_MAX_ACTIVE}' is not a whole number from 1 to 1000. Nothing was changed."
  units="$(ws_whole "${WS_CAPACITY_UNITS}" "${WS_DEFAULT_CAPACITY_UNITS}" 4000)" \
    || die "the record's limits.capacity_units '${WS_CAPACITY_UNITS}' is not a whole number from 1 to 4000. Nothing was changed."
  pods="$(ws_whole "${WS_QUOTA_PODS}" "$((max * WS_PODS_PER_AGENT))" 2000)" \
    || die "the record's limits.quota_pods '${WS_QUOTA_PODS}' is not a whole number from 1 to 2000. Nothing was changed."
  cpu="$(ws_whole "${WS_QUOTA_CPU}" "$((max * WS_CPU_PER_AGENT))" 8000)" \
    || die "the record's limits.quota_cpu '${WS_QUOTA_CPU}' is not a whole number from 1 to 8000. Nothing was changed."
  WS_MAX_ACTIVE="${max}"
  WS_CAPACITY_UNITS="${units}"
  WS_QUOTA_PODS="${pods}"
  WS_QUOTA_CPU="${cpu}"
}

ws_load_names() {
  WS_TENANT="$(ws_expect tenant_id)"
  WS_DOC="$(ws_expect workspace_doc)"
  WS_DOCS_PREFIX="$(ws_expect firestore_docs_prefix)"
  WS_WORKER_ID="$(ws_expect worker_id)"
  WS_WORKER_EMAIL="$(ws_expect worker_email)"
  WS_NAMESPACE="$(ws_expect namespace)"
  WS_SCHEDULER="$(ws_expect scheduler_email)"
  WS_RECONCILER="$(ws_expect reconciler_email)"
  WS_BUCKET_URL="$(ws_expect bucket_url)"
  WS_MARKER_URL="$(ws_expect marker_url)"
  WS_GCS_PREFIX="$(ws_expect gcs_prefix)"
  WS_READ_EXPR="$(ws_expect read_expr)"
  WS_WRITE_EXPR="$(ws_expect write_expr)"
  WS_FORGE_SLOT="$(ws_expect forge_slot)"
  WS_FORGE_TWIN="$(ws_expect forge_slot_twin)"
  WS_FIRESTORE_ROLE="$(ws_expect firestore_role)"
  WS_META_ROLE="$(ws_expect bucket_metadata_role)"
  WS_CONTEXT="gke_${PROJECT_ID}_$(ws_expect gke_location)_$(ws_expect gke_cluster)"
  # One switch, read from the guard's own rules, so the script makes the WD9
  # fallback's grants exactly when the guard allows them (§2.3).
  WS_FALLBACK="$(jq -r '.wd9_fallback == true' "${SWARM_LIB_DIR}/workspace-calls.json")"
  local name
  for name in WS_TENANT WS_DOC WS_DOCS_PREFIX WS_WORKER_EMAIL WS_NAMESPACE WS_SCHEDULER \
              WS_RECONCILER WS_BUCKET_URL WS_READ_EXPR WS_WRITE_EXPR WS_FORGE_SLOT WS_FORGE_TWIN; do
    [[ -n "${!name}" ]] || die "the guard's expectation holds no ${name#WS_}; nothing past A1 can run"
  done
}

# --- progress on the record -------------------------------------------------------

# One step's progress. A progress write that fails is reported and the run goes
# on: the record's final state is written separately, and that one is fatal.
ws_mark() {
  local id="$1" state="$2" code="${3:-}" fields
  fields="$(jq -nc --arg id "${id}" --arg s "${state}" --arg c "${code}" --arg at "$(iso_now)" '
    {steps: {mapValue: {fields: {($id): {mapValue: {fields: (
      {state: {stringValue: $s}, at: {timestampValue: $at}}
      + (if $c == "" then {} else {code: {stringValue: $c}} end))}}}}}}')"
  fs_patch "${WS_DOC}" "steps.${id}" "${fields}" \
    || warn "could not record ${id} as ${state} on the record; the run goes on"
}

ws_retryable() {
  case "$1" in
    APPLY_FAILED|GRANT_FAILED|CONTROL_PLANE_WRITE_FAILED|CLUSTER_UNREACHABLE|NAMESPACE_APPLY_FAILED|VERIFY_FAILED) return 0 ;;
  esac
  return 1
}

ws_default_code() {
  case "$1" in
    A2|A3) printf 'APPLY_FAILED' ;;
    A4|A5|A6) printf 'GRANT_FAILED' ;;
    A7) printf 'NAMESPACE_APPLY_FAILED' ;;
    A8) printf 'CONTROL_PLANE_WRITE_FAILED' ;;
    *) printf 'VERIFY_FAILED' ;;
  esac
}

ws_label() {
  case "$1" in
    A1) printf 'claim' ;;
    A2) printf 'checking the name is free' ;;
    A3) printf 'identity' ;;
    A4) printf 'identity bindings' ;;
    A5) printf 'access' ;;
    A6) printf 'forge slot' ;;
    A7) printf 'namespace' ;;
    A8) printf 'limits' ;;
    A9) printf 'final check' ;;
  esac
}

# needs_owner: the step held, with the guard's rule (or the hold's reason) as its
# code. Not a failure code (§4.2). After a stop the guard still allows exactly
# this write to the workspace record.
ws_needs_owner() {
  local id="$1" code="$2" at fields
  at="$(iso_now)"
  fields="$(jq -nc --arg id "${id}" --arg c "${code}" --arg at "${at}" '
    {state: {stringValue: "needs_owner"},
     steps: {mapValue: {fields: {($id): {mapValue: {fields: {
       state: {stringValue: "held"}, at: {timestampValue: $at}, code: {stringValue: $c}}}}}}},
     run: {mapValue: {fields: {finished_at: {timestampValue: $at}}}}}')"
  fs_patch "${WS_DOC}" "state,steps.${id},run.finished_at" "${fields}" \
    || err "could not record needs_owner on the record; the build log above is the only account of this stop"
  err "${WORKSPACE_ID}: stopped at ${id} for the platform owner (${code}); nothing the refused call would have done has happened"
}

ws_failed() {
  local id="$1" code="$2" at retryable=false object="" fields mask
  at="$(iso_now)"
  ws_retryable "${code}" && retryable=true
  [[ -s "${WS_WORK}/object" ]] && object="$(cat "${WS_WORK}/object")"
  fields="$(jq -nc --arg id "${id}" --arg c "${code}" --arg at "${at}" --arg o "${object}" \
      --argjson r "${retryable}" --arg mode "${WORKSPACE_MODE}" '
    {steps: {mapValue: {fields: {($id): {mapValue: {fields: {
       state: {stringValue: "failed"}, at: {timestampValue: $at}, code: {stringValue: $c}}}}}}},
     failure: {mapValue: {fields: (
       {step: {stringValue: $id}, code: {stringValue: $c}, retryable: {booleanValue: $r}, at: {timestampValue: $at}}
       + (if $o == "" then {} else {object: {stringValue: $o}} end))}},
     run: {mapValue: {fields: {finished_at: {timestampValue: $at}}}}}
    + (if $mode == "limits" then {} else {state: {stringValue: "failed"}} end)')"
  mask="steps.${id},failure,run.finished_at"
  [[ "${WORKSPACE_MODE}" == "limits" ]] || mask="state,${mask}"
  fs_patch "${WS_DOC}" "${mask}" "${fields}" \
    || err "could not record the failure on the record; the build log above is the only account of it"
  err "${WORKSPACE_ID}: ${id} failed (${code}, retryable: ${retryable})"
}

# Runs one step in a subshell with errexit, so a `die` deep in common.sh ends
# the step and not the recording of it; then checks for a guard stop whatever
# the step returned, because a refusal some read swallowed is still a stop.
ws_run_step() {
  local id="$1" fn="$2" rc code
  step "${id} $(ws_label "${id}") (${WORKSPACE_ID})"
  rm -f "${WS_WORK}/code" "${WS_WORK}/hold" "${WS_WORK}/object"
  ws_mark "${id}" running
  set +e
  ( set -e; "${fn}" )
  rc=$?
  set -e
  if ws_stopped; then
    ws_needs_owner "${id}" "$(jq -r '.rule // "C0"' "${SWARM_CALL_GUARD}.stop" 2>/dev/null || printf 'C0')"
    exit "${WS_STOPPED_RC}"
  fi
  if [[ -s "${WS_WORK}/hold" ]]; then
    ws_needs_owner "${id}" "$(cat "${WS_WORK}/hold")"
    exit "${WS_STOPPED_RC}"
  fi
  if [[ "${rc}" -eq 0 ]]; then
    ws_mark "${id}" "done"
    return 0
  fi
  code="$(cat "${WS_WORK}/code" 2>/dev/null || true)"
  [[ -n "${code}" ]] || code="$(ws_default_code "${id}")"
  ws_failed "${id}" "${code}"
  exit 1
}

# --- A1 claim ---------------------------------------------------------------------

ws_rollback() {
  fs_request POST "$(fs_base):rollback" "$(jq -nc --arg t "$1" '{transaction: $t}')" >/dev/null 2>&1 || true
}

# The claim's write: the run, the mode's steps reset, the failure cleared, and
# for a create the state `applying`. One commit inside the transaction whose
# read admitted it, so two builds cannot both claim one workspace.
ws_claim_write() {
  local tx="$1" at attempt steps_json mask_json state_json
  at="$(iso_now)"
  attempt=$((WS_RUN_ATTEMPT + 1))
  local -a ids=()
  case "${WORKSPACE_MODE}" in
    create) ids=(A1 A2 A3 A4 A5 A6 A7 A8 A9) ;;
    limits) ids=(A1 A7 A8) ;;
    verify) ids=(A1 A9) ;;
  esac
  steps_json="$(printf '%s\n' "${ids[@]}" | jq -Rsc --arg at "${at}" '
    split("\n") | map(select(length > 0))
    | map({key: ., value: {mapValue: {fields: {
        state: {stringValue: (if . == "A1" then "done" else "todo" end)}, at: {timestampValue: $at}}}}})
    | from_entries')"
  if [[ "${WORKSPACE_MODE}" == "create" ]]; then
    state_json='{"state": {"stringValue": "applying"}}'
    mask_json='["state", "steps"]'
  else
    state_json='{}'
    mask_json="$(printf '%s\n' "${ids[@]}" | jq -Rsc 'split("\n") | map(select(length > 0) | "steps." + .)')"
  fi
  jq -nc --arg name "${WS_DOCS_PREFIX}/${WS_DOC}" --arg tx "${tx}" --arg at "${at}" \
      --arg build "${WS_BUILD_ID}" --arg mode "${WORKSPACE_MODE}" --argjson attempt "${attempt}" \
      --argjson steps "${steps_json}" --argjson state "${state_json}" --argjson mask "${mask_json}" '
    {writes: [{
       update: {name: $name, fields: ($state + {
         steps: {mapValue: {fields: $steps}},
         run: {mapValue: {fields: {
           build_id: {stringValue: $build}, attempt: {integerValue: ($attempt | tostring)},
           mode: {stringValue: $mode}, claimed_at: {timestampValue: $at}}}}})},
       updateMask: {fieldPaths: ($mask + ["failure", "run.build_id", "run.attempt", "run.mode",
                                         "run.claimed_at", "run.finished_at"])},
       currentDocument: {exists: true}}],
     transaction: $tx}'
}

ws_claim() {
  step "A1 claim (${WORKSPACE_ID})"
  local query count reason

  # 1. The record, by the opaque id: the only read the guard allows yet.
  query="$(jq -nc --arg w "${WORKSPACE_ID}" '{structuredQuery: {
      from: [{collectionId: "workspaces"}],
      where: {fieldFilter: {field: {fieldPath: "workspace_id"}, op: "EQUAL", value: {stringValue: $w}}},
      limit: 2}}')"
  fs_request POST "$(fs_base):runQuery" "${query}" >"${WS_WORK}/query.json" \
    || die "could not read the workspace record for ${WORKSPACE_ID} (above). Nothing was changed."
  count="$(jq '[.[]? | select(.document != null)] | length' "${WS_WORK}/query.json")"
  [[ "${count}" -ne 0 ]] || die "WORKSPACE_NOT_FOUND: no workspace record names ${WORKSPACE_ID}. Nothing was changed."
  [[ "${count}" -eq 1 ]] \
    || die "two workspace records name ${WORKSPACE_ID}; workspace ids are unique (workspace_ids/), so this needs a person. Nothing was changed."
  jq '[.[] | select(.document != null)][0].document' "${WS_WORK}/query.json" >"${WS_WORK}/record.json"
  ws_load_record "${WS_WORK}/record.json"

  # 2. The tenant id, re-derived from the principal by the frozen contract.
  [[ "${WS_PRINCIPAL}" =~ ${WS_EMAIL_RE} ]] \
    || die "${WORKSPACE_ID}'s record names no usable principal. Nothing was changed."
  local derived
  derived="$(ws_derive "${WS_PRINCIPAL}")" \
    || die "swarm_common.identity could not derive a tenant id from ${WORKSPACE_ID}'s principal. Nothing was changed."
  [[ "${derived}" =~ ${WS_TENANT_RE} ]] || die "the derived tenant id '${derived}' is not a personal tenant id. Nothing was changed."
  if [[ "${WS_REC_TENANT}" != "${derived}" || "${WS_RECORD_ID}" != "${derived}" ]]; then
    die "WORKSPACE_ID_TAKEN: ${WORKSPACE_ID}'s record is filed as '${WS_RECORD_ID}' and names tenant
  '${WS_REC_TENANT}', but its principal derives '${derived}' (swarm_common.identity). An edited
  record must not point this job at another tenant's resources. Nothing was changed."
  fi

  # 3. Is this mode admitted, on the record as read?
  reason="$(ws_admit)"
  case "${reason}" in
    "") ;;
    RUN_IN_PROGRESS*|ALREADY_READY*)
      ok "${WORKSPACE_ID}: ${reason#* }; nothing to do"
      exit 0 ;;
    *)
      err "${reason%% *}: ${WORKSPACE_ID}: ${reason#* }"
      die "refusing to run --mode ${WORKSPACE_MODE}. Nothing was changed." ;;
  esac

  # 4. Approved, and retried, by people who are admins now.
  if ! [[ "${WORKSPACE_MODE}" == "verify" && "${WS_MIGRATED}" == "true" ]]; then
    ws_is_admin "${WS_DECIDED_BY}" \
      || die "WORKSPACE_NOT_APPROVED: ${WORKSPACE_ID} was approved by someone who is not an admin in admin_roles/ now. Nothing was changed."
    if [[ "${WORKSPACE_MODE}" == "create" && ( "${WS_STATE}" == "failed" || "${WS_STATE}" == "needs_owner" ) ]]; then
      ws_is_admin "${WS_RETRY_BY}" \
        || die "WORKSPACE_NOT_APPROVED: ${WORKSPACE_ID}'s retry was asked for by someone who is not an admin now. Nothing was changed."
    fi
  fi
  ws_resolve_limits

  # 5. The guard's expectation, from the record. workspace-guard.sh re-derives
  # the tenant itself and refuses a record that disagrees.
  "${SWARM_LIB_DIR}/workspace-guard.sh" expect --record "${WS_WORK}/record.json" \
    || die "the call guard refused the record (above). Nothing was changed."
  ws_load_names
  [[ "${WS_TENANT}" == "${derived}" ]] \
    || die "the guard's expectation names tenant ${WS_TENANT}, not ${derived}. Nothing was changed."

  # 6. The claim: re-read inside a transaction, re-admit, commit.
  local tx enc
  fs_request POST "$(fs_base):beginTransaction" '{"options":{"readWrite":{}}}' >"${WS_WORK}/tx.json" \
    || die "could not begin the claim's transaction (above). Nothing was changed."
  tx="$(jq -r '.transaction // ""' "${WS_WORK}/tx.json")"
  [[ -n "${tx}" ]] || die "Firestore began no transaction. Nothing was changed."
  enc="$(jq -rn --arg t "${tx}" '$t | @uri')"
  if ! fs_request GET "$(fs_base)/${WS_DOC}?transaction=${enc}" >"${WS_WORK}/claim.json"; then
    ws_rollback "${tx}"
    die "could not re-read the record inside the claim (above). Nothing was changed."
  fi
  ws_load_record "${WS_WORK}/claim.json"
  if [[ "${WS_REC_TENANT}" != "${derived}" || ! "${WS_PRINCIPAL}" =~ ${WS_EMAIL_RE} ]] \
     || [[ "$(ws_derive "${WS_PRINCIPAL}")" != "${derived}" ]]; then
    ws_rollback "${tx}"
    die "the record changed its tenant or principal while it was being claimed. Nothing was changed."
  fi
  reason="$(ws_admit)"
  if [[ -n "${reason}" ]]; then
    ws_rollback "${tx}"
    case "${reason}" in
      RUN_IN_PROGRESS*|ALREADY_READY*) ok "${WORKSPACE_ID}: ${reason#* }; nothing to do"; exit 0 ;;
    esac
    die "${reason%% *}: ${WORKSPACE_ID}: ${reason#* }. Nothing was changed."
  fi
  ws_resolve_limits
  ws_claim_write "${tx}" >"${WS_WORK}/commit.json"
  fs_request POST "$(fs_base):commit" "$(cat "${WS_WORK}/commit.json")" >/dev/null \
    || die "the claim did not commit (above): another run may have claimed ${WORKSPACE_ID} first. Nothing else was changed."
  ok "claimed ${WORKSPACE_ID}"
  dim "  request ${WS_REQUEST_ID:-<none>}, attempt $((WS_RUN_ATTEMPT + 1)), build ${WS_BUILD_ID}"
  dim "  limits: ${WS_MAX_ACTIVE} agents, ${WS_CAPACITY_UNITS} units; quota ${WS_QUOTA_PODS} pods, ${WS_QUOTA_CPU} vCPU"
}

# --- A2-A9 ------------------------------------------------------------------------

# The bindings this mode makes on the worker's own policy, one "role member" a
# line: Workload Identity for the namespace's two KSAs and act-as for the
# scheduler and the reconciler.
ws_account_pairs() {
  local ksa mode="${1:-allowed}"
  for ksa in "${WS_KSAS[@]}"; do
    # "required" (A9) on a MIGRATED record skips the legacy swarm-worker: a
    # Terraform-made tenant binds swarm-agent-worker only, and swarm-worker is
    # rendered only where IAM binds it (kubernetes/README.md). It stays in the
    # "allowed" list, so the squat inspection never calls it foreign.
    # w-752763's A9 failed on exactly this, 2026-10-10 (#847).
    if [[ "${mode}" == "required" && "${WS_MIGRATED}" == "true" && "${ksa}" == "swarm-worker" ]]; then
      continue
    fi
    printf 'roles/iam.workloadIdentityUser serviceAccount:%s.svc.id.goog[%s/%s]\n' "${PROJECT_ID}" "${WS_NAMESPACE}" "${ksa}"
  done
  printf 'roles/iam.serviceAccountUser serviceAccount:%s\n' "${WS_SCHEDULER}" "${WS_RECONCILER}"
}

# The narrowed squat inspection: prints every binding on the worker's policy
# that this mode does not make (a conditioned one included), one per line.
# The release deployer's two grants are allowed on a migrated record only.
ws_foreign_bindings() {
  local policy="$1" allowed deployer
  deployer="${DEPLOYER_SERVICE_ACCOUNT:-swarm-tf-deployer@${PROJECT_ID}.iam.gserviceaccount.com}"
  allowed="$(ws_account_pairs | jq -Rsc 'split("\n") | map(select(length > 0))')"
  if [[ "${WS_MIGRATED}" == "true" ]]; then
    allowed="$(jq -c --arg d "serviceAccount:${deployer}" \
      '. + ["roles/iam.serviceAccountAdmin \($d)", "roles/iam.serviceAccountUser \($d)"]' <<<"${allowed}")"
  fi
  jq -r --argjson allowed "${allowed}" '
    .bindings[]? as $b | $b.members[]? as $m
    | select(($b.condition != null) or (($allowed | any(. == ($b.role + " " + $m))) | not))
    | "\($b.role) \($m)" + (if $b.condition != null then " (conditioned)" else "" end)' "${policy}"
}

# 0 when the account carries nothing foreign and no user-managed key; 1 when it
# does (IDENTITY_NOT_OURS); 2 when it could not be inspected.
ws_inspect_account() {
  local unexpected keys
  ws_call "${WS_WORK}/account-policy.json" gcloud iam service-accounts get-iam-policy "${WS_WORKER_EMAIL}" \
    --project "${PROJECT_ID}" --format=json || { ws_show_err; return 2; }
  unexpected="$(ws_foreign_bindings "${WS_WORK}/account-policy.json")" || return 2
  ws_call "${WS_WORK}/account-keys.txt" gcloud iam service-accounts keys list --iam-account "${WS_WORKER_EMAIL}" \
    --project "${PROJECT_ID}" --managed-by=user --format='value(name)' || { ws_show_err; return 2; }
  keys="$(grep -c . "${WS_WORK}/account-keys.txt" || true)"
  if [[ -n "${unexpected}" || "${keys}" -gt 0 ]]; then
    err "the worker account already exists and carries what this platform never grants it:"
    if [[ -n "${unexpected}" ]]; then
      while IFS= read -r line; do err "  binding  ${line}"; done <<<"${unexpected}"
    fi
    [[ "${keys}" -eq 0 ]] || err "  ${keys} user-managed key(s)"
    return 1
  fi
  return 0
}

ws_a2() {
  local rc=0
  ws_probe "${WS_WORK}/a2.out" gcloud iam service-accounts describe "${WS_WORKER_EMAIL}" \
    --project "${PROJECT_ID}" --format='value(email)' || rc=$?
  case "${rc}" in
    1) printf 'absent' >"${WS_WORK}/account"; ok "identity: absent"; return 0 ;;
    2) return 1 ;;
  esac
  printf 'present' >"${WS_WORK}/account"
  rc=0
  ws_inspect_account || rc=$?
  case "${rc}" in
    0) ok "identity: ours" ;;
    1) ws_code IDENTITY_NOT_OURS
       err "refusing to adopt an account somebody else may control (#334); nothing was changed"
       return 1 ;;
    *) err "the existing worker account could not be inspected, so it cannot be shown to be ours"
       return 1 ;;
  esac
}

ws_a3() {
  if [[ "$(cat "${WS_WORK}/account" 2>/dev/null || true)" == "present" ]]; then
    ok "identity: ours, already present"
    return 0
  fi
  # The display name and description name the workspace id, never the person:
  # this project's account list is readable by the other team.
  ws_call /dev/null gcloud iam service-accounts create "${WS_WORKER_ID}" --project "${PROJECT_ID}" \
    --display-name "swarm workspace ${WORKSPACE_ID}" \
    --description "SwarmCloud personal workspace ${WORKSPACE_ID}; made by register-tenant.sh --workspace" \
    || { ws_show_err; return 1; }
  # "Done" is the account readable, not the create returning: IAM is eventually
  # consistent, and A4 writes the new account's policy next.
  local rc attempt=0
  local -a delays=()
  read -r -a delays <<<"${WS_RETRY_DELAYS}"
  while :; do
    rc=0
    ws_probe "${WS_WORK}/a3.out" gcloud iam service-accounts describe "${WS_WORKER_EMAIL}" \
      --project "${PROJECT_ID}" --format='value(email)' || rc=$?
    [[ "${rc}" -ne 0 ]] || break
    [[ "${rc}" -eq 1 && "${attempt}" -lt "${#delays[@]}" ]] || return 1
    sleep "${delays[${attempt}]}"
    attempt=$((attempt + 1))
  done
  ok "identity: created"
}

ws_a4() {
  local made=0 role member
  ws_call "${WS_WORK}/a4-policy.json" gcloud iam service-accounts get-iam-policy "${WS_WORKER_EMAIL}" \
    --project "${PROJECT_ID}" --format=json || { ws_show_err; return 1; }
  while read -r role member; do
    [[ -n "${role}" ]] || continue
    if iam_policy_binds_member "${WS_WORK}/a4-policy.json" "${role}" "${member}"; then
      continue
    fi
    ws_call /dev/null gcloud iam service-accounts add-iam-policy-binding "${WS_WORKER_EMAIL}" \
      --project "${PROJECT_ID}" --role "${role}" --member "${member}" --quiet || { ws_show_err; return 1; }
    made=$((made + 1))
  done < <(ws_account_pairs)
  ok "account bindings: 4 (${made} added)"
}

ws_condition_file() {
  local file="$1" title="$2" description="$3" expression="$4"
  jq -n --arg t "${title}" --arg d "${description}" --arg e "${expression}" \
    '{title: $t, description: $d, expression: $e}' >"${file}"
}

ws_bucket_has() {
  jq -e --arg r "$2" --arg m "serviceAccount:${WS_WORKER_EMAIL}" --arg e "$3" \
    'any((.bindings? // [])[]; .role == $r and any(.members[]?; . == $m)
         and (if $e == "" then .condition == null else (.condition.expression? // "") == $e end))' \
    "$1" >/dev/null 2>&1
}

ws_put_marker() {
  printf 'workspace %s registered %s\n' "${WORKSPACE_ID}" "$(iso_now)" \
    | gcloud storage cp - "${WS_MARKER_URL}" --project "${PROJECT_ID}"
}

ws_a5() {
  local policy="${WS_WORK}/bucket-policy.json" stale role
  ws_call "${policy}" gcloud storage buckets get-iam-policy "${WS_BUCKET_URL}" \
    --project "${PROJECT_ID}" --format=json || { ws_show_err; return 1; }
  # Anything else this worker holds on the shared bucket goes to the owner: the
  # operator path would remove it, and a removal is never this job's (C9).
  stale="$(jq -r --arg m "serviceAccount:${WS_WORKER_EMAIL}" --arg re "${WS_READ_EXPR}" \
      --arg we "${WS_WRITE_EXPR}" --arg meta "${WS_META_ROLE}" --argjson fb "${WS_FALLBACK}" '
    (.bindings? // [])[] | select(any(.members[]?; . == $m))
    | select((.role == "roles/storage.objectViewer" and (.condition.expression? // "") == $re) | not)
    | select((.role == "roles/storage.objectUser" and (.condition.expression? // "") == $we) | not)
    | select(($fb and .role == $meta and .condition == null) | not)
    | .role + (if .condition != null then " (conditioned)" else "" end)' "${policy}")"
  if [[ -n "${stale}" ]]; then
    while IFS= read -r role; do err "  stale bucket binding  ${role}"; done <<<"${stale}"
    ws_hold STALE_BUCKET_BINDING "the worker already holds a bucket binding this job does not make, and removing it is the owner's call"
    return 1
  fi
  ws_condition_file "${WS_WORK}/read-condition.json" "swarm-tenant-prefix-read-${WS_TENANT}" \
    "Read and list objects under ${WS_GCS_PREFIX}/ only." "${WS_READ_EXPR}"
  ws_condition_file "${WS_WORK}/write-condition.json" "swarm-tenant-prefix-write-${WS_TENANT}" \
    "Write objects under ${WS_GCS_PREFIX}/, except ${WS_GCS_PREFIX}/verdicts/." "${WS_WRITE_EXPR}"
  if ! ws_bucket_has "${policy}" roles/storage.objectViewer "${WS_READ_EXPR}"; then
    ws_call /dev/null gcloud storage buckets add-iam-policy-binding "${WS_BUCKET_URL}" --project "${PROJECT_ID}" \
      --member "serviceAccount:${WS_WORKER_EMAIL}" --role roles/storage.objectViewer \
      --condition-from-file "${WS_WORK}/read-condition.json" || { ws_show_err; return 1; }
  fi
  if ! ws_bucket_has "${policy}" roles/storage.objectUser "${WS_WRITE_EXPR}"; then
    ws_call /dev/null gcloud storage buckets add-iam-policy-binding "${WS_BUCKET_URL}" --project "${PROJECT_ID}" \
      --member "serviceAccount:${WS_WORKER_EMAIL}" --role roles/storage.objectUser \
      --condition-from-file "${WS_WORK}/write-condition.json" || { ws_show_err; return 1; }
  fi
  if [[ "${WS_FALLBACK}" == "true" ]]; then
    # WD9's fallback (§2.3): no principal-set grant, so the person's worker gets
    # the bucket-metadata, database and telemetry roles one by one.
    if ! ws_bucket_has "${policy}" "${WS_META_ROLE}" ""; then
      ws_call /dev/null gcloud storage buckets add-iam-policy-binding "${WS_BUCKET_URL}" --project "${PROJECT_ID}" \
        --member "serviceAccount:${WS_WORKER_EMAIL}" --role "${WS_META_ROLE}" --condition None \
        || { ws_show_err; return 1; }
    fi
    ws_call "${WS_WORK}/project-policy.json" gcloud projects get-iam-policy "${PROJECT_ID}" --format=json \
      || { ws_show_err; return 1; }
    for role in "${WS_FIRESTORE_ROLE}" roles/logging.logWriter roles/monitoring.metricWriter; do
      ws_bucket_has "${WS_WORK}/project-policy.json" "${role}" "" && continue
      ws_call /dev/null gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member "serviceAccount:${WS_WORKER_EMAIL}" --role "${role}" --condition None --quiet \
        || { ws_show_err; return 1; }
    done
  fi
  ws_call /dev/null ws_put_marker || { ws_show_err; return 1; }
  ok "access: granted"
}

# The person's empty forge slot and its twin, and the worker's read of the slot
# (never the twin): scripts/lib/forge-slot.sh, the one path that makes them,
# which workspace-migrate-record.sh runs too. Through ws_call / ws_probe, so
# every call has the guard's retry.
ws_a6() {
  local rc=0
  FORGE_SLOT_CALL=ws_call FORGE_SLOT_PROBE=ws_probe FORGE_SLOT_SHOW_ERR=ws_show_err \
    FORGE_SLOT_WORK="${WS_WORK}" FORGE_SLOT_DRY_RUN=0 \
    forge_slot_ensure_pair "${WS_TENANT}" "${WS_FORGE_SLOT}" "${WS_FORGE_TWIN}" "${WS_WORKER_EMAIL}" || rc=$?
  case "${rc}" in
    0) ok "forge slot: bound" ;;
    3) ws_hold SLOT_NOT_OURS "the forge slot already exists labelled for '${FORGE_SLOT_LABELLED:-nobody}', not this tenant"
       return 1 ;;
    4) ws_hold TWIN_BOUND "the worker is bound to the forge slot's refresh twin, which no worker may read"
       return 1 ;;
    *) return 1 ;;
  esac
}

# Credentials for the swarm cluster in the job's own kubeconfig, and the API
# server answering. CLUSTER_UNREACHABLE otherwise.
ws_cluster_connect() {
  ws_call /dev/null gcloud container clusters get-credentials "$(ws_expect gke_cluster)" \
    --location "$(ws_expect gke_location)" --project "${PROJECT_ID}" \
    || { ws_show_err; ws_code CLUSTER_UNREACHABLE; return 1; }
  ws_call /dev/null "${WS_KUBECTL}" --context "${WS_CONTEXT}" get --raw=/readyz \
    || { ws_show_err; ws_code CLUSTER_UNREACHABLE; return 1; }
}

ws_quota_args() {
  printf '%s\n' --quota-pods "${WS_QUOTA_PODS}" --quota-cpu "${WS_QUOTA_CPU}" \
    --quota-memory "$((WS_QUOTA_CPU * WS_MEMORY_GI_PER_CPU))Gi" \
    --quota-jobs "$((WS_QUOTA_PODS * WS_JOBS_PER_POD))" \
    --quota-ephemeral "$((WS_QUOTA_PODS * WS_EPHEMERAL_GI_PER_POD))Gi"
}

ws_a7() {
  ws_cluster_connect || return 1
  local -a args=(--tenant "${WS_TENANT}" --gsa "${WS_WORKER_EMAIL}" --context "${WS_CONTEXT}"
    --cluster "$(ws_expect gke_cluster)" --confirm)
  local quota
  while IFS= read -r quota; do args+=("${quota}"); done < <(ws_quota_args)
  # A server dry run first, where there is a namespace for it to run in: on a
  # first apply nothing inside the namespace can be dry-run yet (apply.sh).
  if "${WS_KUBECTL}" --context "${WS_CONTEXT}" get namespace "${WS_NAMESPACE}" -o name >/dev/null 2>&1; then
    args+=(--server-dry-run)
  fi
  ws_stopped && return 1
  "${REPO_ROOT}/kubernetes/apply.sh" "${args[@]}" || return 1
  ok "namespace: applied"
}

# ws_doc_check FILE EXPRESSION [jq --arg ...]: true when the decoded document
# satisfies EXPRESSION. Values go in as jq arguments, never into the program.
ws_doc_check() {
  local file="$1" expression="$2"
  shift 2
  jq -e "$@" "${FS_JQ} doc | ${expression}" "${file}" >/dev/null 2>&1
}

# ws_doc_off FILE PROGRAM [jq --arg ...]: the names of the fields A9 finds not
# as specified, comma-separated; "document" when there is no document; nothing
# when all agree. PROGRAM yields an array of names, and may use `$mig` (the
# record is migrated) and `whole` (a limit as the record states it: on a
# migrated record also an integral double or a string of digits, see ws_a9).
# An unreadable answer counts as "document", never as agreement.
ws_doc_off() {
  local file="$1" expression="$2"
  shift 2
  [[ -s "${file}" ]] || { printf 'document'; return 0; }
  jq -r --argjson mig "${WS_MIGRATED}" "$@" "${FS_JQ}
    def whole: if type == \"number\" then .
      elif \$mig and type == \"string\" and test(\"^[0-9]+$\") then tonumber else null end;
    if .fields then (doc | ${expression} | join(\", \")) else \"document\" end" "${file}" 2>/dev/null \
    || printf 'document'
}

# The fields' NAMES only, never their values (§4.2): the log names what to
# look at, and the record's failure.object stays the document's type.
ws_off_err() {
  if [[ "$2" == "document" ]]; then
    err "verify: the ${1} document is missing"
  else
    err "verify: the ${1} document's fields ${2} are not as the record specifies$([[ "${WS_MIGRATED}" == "true" ]] && printf ' (migrated record)')"
  fi
}

ws_a8() {
  local pool_limit fields mask exists
  pool_limit=$(( WS_MAX_ACTIVE < WS_CAPACITY_UNITS ? WS_MAX_ACTIVE : WS_CAPACITY_UNITS ))
  ws_call "${WS_WORK}/tenant.json" fs_get "tenants/${WS_TENANT}" || { ws_show_err; return 1; }
  exists="$(jq -r 'if .fields then "yes" else "no" end' "${WS_WORK}/tenant.json")"
  fields="$(jq -nc --arg id "${WS_TENANT}" --arg p "${WS_PRINCIPAL}" --arg sa "${WS_WORKER_EMAIL}" \
      --arg prefix "${WS_GCS_PREFIX}" --arg ns "${WS_NAMESPACE}" --arg at "$(iso_now)" \
      --argjson max "${WS_MAX_ACTIVE}" --argjson units "${WS_CAPACITY_UNITS}" '
    {tenant_id: {stringValue: $id}, kind: {stringValue: "user"}, principal: {stringValue: $p},
     display_name: {stringValue: $p}, created_at: {timestampValue: $at},
     max_active: {integerValue: ($max | tostring)}, capacity_units: {integerValue: ($units | tostring)},
     enabled: {booleanValue: true}, credentials: {arrayValue: {values: []}},
     service_account: {stringValue: $sa}, gcs_prefix: {stringValue: $prefix}, namespace: {stringValue: $ns}}')"
  if [[ "${exists}" == "yes" ]]; then
    # Adopted, never re-pointed (§3.3): a document `ensure_tenant` wrote on
    # first sight is this person's only when its principal is theirs. Its
    # credentials, created_at, enabled and display_name are left as they are:
    # a migrated workspace keeps its provider, a disabled tenant stays disabled.
    # shellcheck disable=SC2016  # a jq program; $p is jq's
    if ! ws_doc_check "${WS_WORK}/tenant.json" \
         '((.principal // "") | ascii_downcase) == $p and ((.kind // "user") == "user")' --arg p "${WS_PRINCIPAL}"; then
      ws_code WORKSPACE_ID_TAKEN
      err "tenants/<tenant> already belongs to a different principal or kind; it is not adopted"
      return 1
    fi
    mask="tenant_id,kind,principal,max_active,capacity_units,service_account,gcs_prefix,namespace"
    fields="$(jq -c 'del(.created_at, .enabled, .credentials, .display_name)' <<<"${fields}")"
    ws_call /dev/null fs_patch "tenants/${WS_TENANT}" "${mask}" "${fields}" \
      "$(jq -r '.updateTime // ""' "${WS_WORK}/tenant.json")" || { ws_show_err; return 1; }
  else
    mask="tenant_id,kind,principal,display_name,created_at,max_active,capacity_units,enabled,credentials,service_account,gcs_prefix,namespace"
    ws_call /dev/null fs_patch "tenants/${WS_TENANT}" "${mask}" "${fields}" || { ws_show_err; return 1; }
  fi
  # The pool's limit, never its `active` (invariant 2): an existing pool keeps
  # the count of what it holds.
  ws_call "${WS_WORK}/pool.json" fs_get "pools/tenant:${WS_TENANT}" || { ws_show_err; return 1; }
  if jq -e '.fields' "${WS_WORK}/pool.json" >/dev/null 2>&1; then
    ws_call /dev/null fs_patch "pools/tenant:${WS_TENANT}" "hard_limit,updated_at" \
      "$(jq -nc --argjson l "${pool_limit}" --arg t "$(iso_now)" \
        '{hard_limit: {integerValue: ($l | tostring)}, updated_at: {timestampValue: $t}}')" \
      || { ws_show_err; return 1; }
  else
    ws_call /dev/null fs_patch "pools/tenant:${WS_TENANT}" "name,hard_limit,active,enabled,updated_at" \
      "$(jq -nc --arg n "tenant:${WS_TENANT}" --argjson l "${pool_limit}" --arg t "$(iso_now)" \
        '{name: {stringValue: $n}, hard_limit: {integerValue: ($l | tostring)},
          active: {integerValue: "0"}, enabled: {booleanValue: true}, updated_at: {timestampValue: $t}}')" \
      || { ws_show_err; return 1; }
  fi
  ok "limits: written (${WS_MAX_ACTIVE} agents, pool ${pool_limit})"
}

ws_kind_word() {
  case "$1" in
    ServiceAccount) printf 'serviceaccount' ;;
    ResourceQuota) printf 'resourcequota' ;;
    LimitRange) printf 'limitrange' ;;
    NetworkPolicy) printf 'networkpolicy' ;;
    Role) printf 'role' ;;
    RoleBinding) printf 'rolebinding' ;;
    *) return 1 ;;
  esac
}

# A9: every object read back, as specified. A failure names the object's type,
# never its name (§4.2), on the record as failure.object.
ws_a9() {
  local n=0 role member kind name word
  # The account and its own policy.
  ws_probe "${WS_WORK}/a9.out" gcloud iam service-accounts describe "${WS_WORKER_EMAIL}" \
    --project "${PROJECT_ID}" --format='value(email)' || { ws_object "service account"; return 1; }
  n=$((n + 1))
  local rc=0
  ws_inspect_account || rc=$?
  [[ "${rc}" -eq 0 ]] || { ws_object "service account binding"; return 1; }
  while read -r role member; do
    [[ -n "${role}" ]] || continue
    iam_policy_binds_member "${WS_WORK}/account-policy.json" "${role}" "${member}" \
      || { ws_object "service account binding"; return 1; }
    n=$((n + 1))
  done < <(ws_account_pairs required)
  # The bucket grants.
  ws_call "${WS_WORK}/bucket-policy.json" gcloud storage buckets get-iam-policy "${WS_BUCKET_URL}" \
    --project "${PROJECT_ID}" --format=json || { ws_show_err; ws_object "bucket grant"; return 1; }
  ws_bucket_has "${WS_WORK}/bucket-policy.json" roles/storage.objectViewer "${WS_READ_EXPR}" \
    || { ws_object "bucket grant"; return 1; }
  ws_bucket_has "${WS_WORK}/bucket-policy.json" roles/storage.objectUser "${WS_WRITE_EXPR}" \
    || { ws_object "bucket grant"; return 1; }
  n=$((n + 2))
  if [[ "${WS_FALLBACK}" == "true" ]]; then
    ws_bucket_has "${WS_WORK}/bucket-policy.json" "${WS_META_ROLE}" "" || { ws_object "bucket grant"; return 1; }
    ws_call "${WS_WORK}/project-policy.json" gcloud projects get-iam-policy "${PROJECT_ID}" --format=json \
      || { ws_show_err; ws_object "project grant"; return 1; }
    for role in "${WS_FIRESTORE_ROLE}" roles/logging.logWriter roles/monitoring.metricWriter; do
      ws_bucket_has "${WS_WORK}/project-policy.json" "${role}" "" || { ws_object "project grant"; return 1; }
    done
    n=$((n + 4))
  fi
  # The forge slot, its twin, and who reads them.
  for name in "${WS_FORGE_SLOT}" "${WS_FORGE_TWIN}"; do
    if ! ws_probe "${WS_WORK}/slot.json" gcloud secrets describe "${name}" --project "${PROJECT_ID}" --format=json \
      || [[ "$(jq -r '.labels.tenant // ""' "${WS_WORK}/slot.json")" != "${WS_TENANT}" ]]; then
      ws_object "forge slot"
      return 1
    fi
    n=$((n + 1))
  done
  ws_call "${WS_WORK}/slot-policy.json" gcloud secrets get-iam-policy "${WS_FORGE_SLOT}" \
    --project "${PROJECT_ID}" --format=json || { ws_show_err; ws_object "forge slot binding"; return 1; }
  iam_policy_binds_member "${WS_WORK}/slot-policy.json" roles/secretmanager.secretAccessor \
    "serviceAccount:${WS_WORKER_EMAIL}" || { ws_object "forge slot binding"; return 1; }
  ws_call "${WS_WORK}/twin-policy.json" gcloud secrets get-iam-policy "${WS_FORGE_TWIN}" \
    --project "${PROJECT_ID}" --format=json || { ws_show_err; ws_object "forge slot binding"; return 1; }
  if jq -e --arg m "serviceAccount:${WS_WORKER_EMAIL}" 'any((.bindings? // [])[]; any(.members[]?; . == $m))' \
       "${WS_WORK}/twin-policy.json" >/dev/null 2>&1; then
    ws_object "forge slot binding"
    return 1
  fi
  n=$((n + 1))
  # Every object the tenant render holds (kubernetes/render.py TENANT_FILES and,
  # since A4 binds swarm-worker, the older identity too). Not on a MIGRATED
  # record: a Terraform-made tenant binds swarm-agent-worker only, and the
  # legacy account is rendered only where IAM binds it (kubernetes/README.md),
  # so it has no swarm-worker objects to find (W9, measured 2026-10-10 on
  # w-752763). A record the job made still needs both.
  local -a bound=(--bound-ksa swarm-agent-worker)
  [[ "${WS_MIGRATED}" == "true" ]] || bound+=(--bound-ksa swarm-worker)
  ws_cluster_connect || return 1
  python3 "${REPO_ROOT}/kubernetes/render.py" tenant --tenant "${WS_TENANT}" --gsa "${WS_WORKER_EMAIL}" \
    "${bound[@]}" 2>/dev/null \
    | python3 -c "${WS_OBJECTS_PY}" >"${WS_WORK}/objects.tsv" \
    || { ws_object "namespace object"; return 1; }
  [[ -s "${WS_WORK}/objects.tsv" ]] || { ws_object "namespace object"; return 1; }
  while IFS="$(printf '\t')" read -r kind name; do
    [[ -n "${kind}" ]] || continue
    if [[ "${kind}" == "Namespace" ]]; then
      [[ "${name}" == "${WS_NAMESPACE}" ]] || { ws_object "Namespace"; return 1; }
      ws_call "${WS_WORK}/object.json" "${WS_KUBECTL}" --context "${WS_CONTEXT}" get namespace "${name}" -o json \
        || { ws_object "Namespace"; return 1; }
    else
      word="$(ws_kind_word "${kind}")" || { ws_object "${kind}"; return 1; }
      ws_call "${WS_WORK}/object.json" "${WS_KUBECTL}" --context "${WS_CONTEXT}" get "${word}" "${name}" \
        -n "${WS_NAMESPACE}" -o json || { ws_object "${kind}"; return 1; }
      if [[ "${kind}" == "ResourceQuota" ]]; then
        jq -e --arg p "${WS_QUOTA_PODS}" --arg c "${WS_QUOTA_CPU}" \
          '(.spec.hard.pods // "") == $p and (.spec.hard["requests.cpu"] // "") == $c' \
          "${WS_WORK}/object.json" >/dev/null 2>&1 || { ws_object "ResourceQuota"; return 1; }
      fi
    fi
    n=$((n + 1))
  done <"${WS_WORK}/objects.tsv"
  # Both documents, with the record's limits. Both must EXIST and the limits
  # must AGREE on every record. On a MIGRATED one the document is Terraform's
  # (terraform/modules/firestore/bootstrap.tf, written once and then under
  # ignore_changes), so two things it may legitimately do differently are
  # accepted there (W9, §3.3): a limit stored as an integral double or a
  # string of digits, which the Tenant and SlotPool models read as the same
  # whole number; and no `namespace`, which a document written before the
  # module named that field never gained -- the dispatcher derives the same
  # name when it is absent (GkeJobDispatcher.namespace_for). A namespace that
  # is present must still be the tenant's, and `service_account` is required
  # everywhere: dispatch refuses a tenant without one. A record the job made
  # keeps the strict check, since A8 wrote every field as checked here.
  ws_call "${WS_WORK}/tenant.json" fs_get "tenants/${WS_TENANT}" || { ws_show_err; ws_object "tenant document"; return 1; }
  local off
  # shellcheck disable=SC2016  # jq programs; their $names are jq's
  off="$(ws_doc_off "${WS_WORK}/tenant.json" \
      '[(if ((.principal // "") | tostring | ascii_downcase | if $mig then gsub("^\\s+|\\s+$"; "") else . end) == $p
         then empty else "principal" end),
        (if .service_account == $sa
            or ($mig and ((.service_account // "") | tostring | ascii_downcase) == ($sa | ascii_downcase))
         then empty else "service_account" end),
        (if .namespace == $ns or ($mig and (.namespace // "") == "") then empty else "namespace" end),
        (if (.max_active | whole) == $max then empty else "max_active" end),
        (if (.capacity_units | whole) == $units then empty else "capacity_units" end)]' \
      --arg p "${WS_PRINCIPAL}" --arg sa "${WS_WORKER_EMAIL}" --arg ns "${WS_NAMESPACE}" \
      --argjson max "${WS_MAX_ACTIVE}" --argjson units "${WS_CAPACITY_UNITS}")"
  [[ -z "${off}" ]] || { ws_off_err "tenant" "${off}"; ws_object "tenant document"; return 1; }
  ws_call "${WS_WORK}/pool.json" fs_get "pools/tenant:${WS_TENANT}" || { ws_show_err; ws_object "pool document"; return 1; }
  # shellcheck disable=SC2016
  off="$(ws_doc_off "${WS_WORK}/pool.json" '[if (.hard_limit | whole) == $l then empty else "hard_limit" end]' \
      --argjson l "$(( WS_MAX_ACTIVE < WS_CAPACITY_UNITS ? WS_MAX_ACTIVE : WS_CAPACITY_UNITS ))")"
  [[ -z "${off}" ]] || { ws_off_err "pool" "${off}"; ws_object "pool document"; return 1; }
  n=$((n + 2))
  printf '%s' "${n}" >"${WS_WORK}/verified"
  ok "verified: ${n} of ${n} objects"
}

# `ready` only after A9 (create, verify); a ceiling change only closes its run.
ws_finish() {
  local at fields mask
  at="$(iso_now)"
  if [[ "${WORKSPACE_MODE}" == "limits" ]]; then
    fields="$(jq -nc --arg at "${at}" '{run: {mapValue: {fields: {finished_at: {timestampValue: $at}}}}}')"
    mask="run.finished_at,failure"
  else
    fields="$(jq -nc --arg at "${at}" '{state: {stringValue: "ready"}, ready_at: {timestampValue: $at},
      run: {mapValue: {fields: {finished_at: {timestampValue: $at}}}}}')"
    mask="state,ready_at,run.finished_at,failure"
  fi
  fs_patch "${WS_DOC}" "${mask}" "${fields}" \
    || die "every step is done, but the record could not be closed (above); an admin's retry finds everything present"
  if [[ "${WORKSPACE_MODE}" == "limits" ]]; then
    ok "${WORKSPACE_ID}: limits applied"
  else
    ok "${WORKSPACE_ID}: ready"
  fi
}

workspace_main() {
  [[ "${WORKSPACE_ID}" =~ ${WS_ID_RE} ]] || die "--workspace needs an id of the form w-<6 hex digits>, got '${WORKSPACE_ID}'"
  case "${WORKSPACE_MODE}" in
    create|limits|verify) ;;
    *) die "--mode must be create, limits or verify, got '${WORKSPACE_MODE}'" ;;
  esac
  # Set by entry.py from CLOUD_RUN_EXECUTION; a local-<time> id is the owner's
  # own run (§2.5, "What the owner does then").
  WS_BUILD_ID="${BUILD_ID:-local-$(date -u +%Y%m%dT%H%M%SZ)}"
  [[ "${WS_BUILD_ID}" =~ ${WS_BUILD_RE} ]] || die "BUILD_ID '${WS_BUILD_ID}' is not a Cloud Run execution name"
  local delays_re='^[0-9]+( [0-9]+)*$'
  [[ "${WS_RETRY_DELAYS}" =~ ${delays_re} ]] \
    || die "SWARM_WORKSPACE_RETRY_DELAYS must be whole seconds separated by spaces, got '${WS_RETRY_DELAYS}'"
  require_cmd jq curl python3 gcloud
  ws_require_guard
  WS_WORK="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/swarm-workspace.XXXXXX")"
  # shellcheck disable=SC2064  # the path is fixed now, on purpose
  trap "rm -rf '${WS_WORK}'" EXIT
  # The job's own kubeconfig, private, so get-credentials writes the swarm
  # cluster's context there and nowhere an operator's contexts live.
  KUBECONFIG="${WS_WORK}/kubeconfig"
  export KUBECONFIG
  WS_KUBECTL="$(kubectl_bin)"
  info "workspace ${WORKSPACE_ID}, --mode ${WORKSPACE_MODE}"

  ws_claim
  case "${WORKSPACE_MODE}" in
    create)
      ws_run_step A2 ws_a2
      ws_run_step A3 ws_a3
      ws_run_step A4 ws_a4
      ws_run_step A5 ws_a5
      ws_run_step A6 ws_a6
      ws_run_step A7 ws_a7
      ws_run_step A8 ws_a8
      ws_run_step A9 ws_a9 ;;
    limits)
      ws_run_step A7 ws_a7
      ws_run_step A8 ws_a8 ;;
    verify)
      ws_run_step A9 ws_a9 ;;
  esac
  ws_finish
}

if [[ "${WORKSPACE_GIVEN}" -eq 1 ]]; then
  WS_REFUSED=""
  [[ -z "${GROUP}" ]] || WS_REFUSED+=" --group"
  [[ -z "${USER_EMAIL}" ]] || WS_REFUSED+=" --user"
  [[ -z "${TENANT_ID}" ]] || WS_REFUSED+=" --tenant"
  [[ "${ADD_PROVIDER_GIVEN}" -eq 0 ]] || WS_REFUSED+=" --add-provider"
  [[ "${DRY_RUN}" -eq 0 ]] || WS_REFUSED+=" --dry-run"
  WS_REFUSED+="${FULL_ONLY_FLAGS}"
  [[ -z "${WS_REFUSED}" ]] || die "--workspace takes its every value from the approved record, so it does not take${WS_REFUSED}.
  (A rehearsal is the call guard's report-only mode, docs/workspaces.md §2.5.)"
  [[ -n "${WORKSPACE_MODE}" ]] || WORKSPACE_MODE="create"
  workspace_main
  exit 0
fi
[[ -z "${WORKSPACE_MODE}" ]] || die "--mode belongs to --workspace"

require_cmd gcloud jq curl python3

# The providers anything on this platform reads a key for, one per line: what
# swarm_api.validation.known_providers() lets a tenant register through the API
# (every provider the runner catalogue references, disabled profiles included),
# plus the forge token the worker reads outside that catalogue
# (agent_worker.secrets.GIT_PROVIDER). Asked of the code rather than restated
# here, as derive_tenant_id below asks swarm_common.identity: a second copy of
# this list is the one that would drift.
known_providers() {
  python3 - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root / "apps" / d) for d in ("common", "swarm-api", "agent-worker")]
from swarm_api.validation import known_providers
from agent_worker.secrets import GIT_PROVIDER
print("\n".join(sorted(set(known_providers()) | {GIT_PROVIDER})))
PY
}

# The providers whose key is a GitHub App's, one per line:
# swarm_api.validation.APP_CREDENTIAL_PROVIDERS (git-merge, git-review), the
# retired #295 App keys. known_providers() above leaves them out, and this
# script refuses them everywhere: nothing reads either key any more, and the
# tenant's worker -- whose token any agent of the tenant can mint -- must
# never be granted one. Asked of the code, like the list above.
app_credential_providers() {
  python3 - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root / "apps" / d) for d in ("common", "swarm-api")]
from swarm_api.validation import APP_CREDENTIAL_PROVIDERS
print("\n".join(sorted(APP_CREDENTIAL_PROVIDERS)))
PY
}

APP_PROVIDERS="$(app_credential_providers)" \
  || die "could not read swarm_api.validation.APP_CREDENTIAL_PROVIDERS (python's error is above), so
  whether a provider's key is a GitHub App's -- which the worker must never read -- cannot be
  checked. Nothing was changed."
is_app_provider() {
  # A here-string, not `printf | grep -q`: bash line-buffers printf into a pipe,
  # grep -q exits at its first match, the next line's write takes SIGPIPE, and
  # pipefail turns a match into a miss -- intermittently, by scheduling.
  grep -Fqx -- "$1" <<<"${APP_PROVIDERS}"
}

# retired_app_provider_refusal PROVIDER  ->  the one message both paths die with.
retired_app_provider_refusal() {
  printf '%s' "provider '$1' is a retired #295 GitHub App key (owner decision MS0-Q4, 2026-10-06):
  the merge, post-verdict and review accounts that alone were to read it are gone, nothing on
  this platform reads it, and the tenant's worker -- whose token any agent of the tenant can
  mint -- must never be granted it. The merge step uses the tenant's forge token, 'git'.
  Nothing was changed."
}

if [[ "${ADD_PROVIDER_GIVEN}" -eq 1 ]]; then
  [[ -z "${FULL_ONLY_FLAGS}" ]] || die "--add-provider adds one provider and changes nothing else, so it does not take${FULL_ONLY_FLAGS}.
  Those belong to a full registration, which rewrites the whole tenant record. Drop them,
  or run the full registration without --add-provider."
  # The charset create-secrets.sh enforces on --provider, since the secret
  # named below is the one it made. `-refresh` is refused by name: with
  # --subscription, create-secrets.sh stores the LONG-LIVED half of a Claude
  # credential as swarm-tenant-<t>-<p>-refresh, and the tenant's worker is
  # absent from that secret's readers on purpose -- terraform/modules/secret_manager
  # `refresh_accessor` binds the quota broker alone, because a job holding the
  # refresh token could mint itself access for as long as it liked. Adding
  # provider `anthropic-refresh` would bind the worker to exactly that secret.
  case "${ADD_PROVIDER}" in
    "")                die "--add-provider needs a provider name (e.g. anthropic, openai, git)" ;;
    *[!a-z0-9-]*)      die "provider '${ADD_PROVIDER}' must be lowercase letters, digits and hyphens" ;;
    *-refresh)         die "provider '${ADD_PROVIDER}' names the -refresh half of a subscription credential,
  which only the quota broker may read -- never the tenant's worker. Add '${ADD_PROVIDER%-refresh}'
  instead: that binds the worker to the short-lived half the broker publishes." ;;
  esac
  # ONLY A PROVIDER SOMETHING READS. Listing any other name does nothing for the
  # tenant, and the only thing it can do is name a secret -- which, since
  # secret names do not split (see tenant_secret_state below), may be another
  # tenant's: `--tenant eng-anthropic --add-provider refresh` names
  # swarm-tenant-eng-anthropic-refresh, tenant eng's refresh half, and walks
  # past the `*-refresh` refusal above. The ownership check below refuses that
  # grant too; this refuses the name before anything is looked up.
  is_app_provider "${ADD_PROVIDER}" && die "$(retired_app_provider_refusal "${ADD_PROVIDER}")"
  KNOWN_PROVIDERS="$(known_providers)" \
    || die "could not read the provider list from swarm_api.validation and agent_worker.secrets
  (python's error is above), so '${ADD_PROVIDER}' cannot be checked against it. Nothing was changed."
  # Here-string for the reason given at is_app_provider: piped, a known provider
  # that is not the last line could intermittently be refused as unknown.
  if ! grep -Fqx -- "${ADD_PROVIDER}" <<<"${KNOWN_PROVIDERS}"; then
    die "provider '${ADD_PROVIDER}' is not one this platform reads a key for. Known providers:
  $(printf '%s\n' "${KNOWN_PROVIDERS}" | paste -sd, - | sed 's/,/, /g')
  (swarm_api.validation.known_providers(), plus agent_worker.secrets.GIT_PROVIDER). Nothing was changed."
  fi
fi

# --- limits ------------------------------------------------------------------
#
# WHOLE NUMBERS, CHECKED BEFORE ANYTHING USES THEM. The pool's ceiling below is
# computed in shell arithmetic, and `$(( ))` evaluates what it is given: a bare
# word is read as a variable name and `4 + 4` as a sum, so an unchecked limit
# becomes a ceiling nobody typed. jq's `--argjson` used to be the only check,
# and it accepted a negative number. `10#` stops a leading zero reading as
# octal, and writes the value back normalised so jq gets a JSON integer.
for limit_flag in max-active capacity-units; do
  case "${limit_flag}" in
    max-active) limit_value="${MAX_ACTIVE}" ;;
    *)          limit_value="${CAPACITY_UNITS}" ;;
  esac
  [[ "${limit_value}" =~ ^[0-9]+$ ]] \
    || die "--${limit_flag} must be a whole number of units, got '${limit_value}'"
done
MAX_ACTIVE=$((10#${MAX_ACTIVE}))
CAPACITY_UNITS=$((10#${CAPACITY_UNITS}))

# THE TENANT POOL'S CEILING IS THE SMALLER OF THE TWO. `max_active` and
# `capacity_units` bound ONE count -- the units the tenant's running work
# holds, where every task costs at least one -- so the smaller is the one that
# binds. swarm_api/store.py writes the pool that way in `ensure_tenant` and on
# every `set_tenant_limits`, terraform/infra/locals.tf `pool_tenants` does, and
# the console's Tenants screen prints this minimum as the ceiling admission
# enforces (AH-12 in #86).
#
# This used to write CAPACITY_UNITS: at the defaults above, a pool admitting 40
# units for a tenant whose record, the API and the console all say is capped
# at 20. tests/integration/test_register_tenant_grants.py holds it to min().
POOL_HARD_LIMIT=$(( MAX_ACTIVE < CAPACITY_UNITS ? MAX_ACTIVE : CAPACITY_UNITS ))

# --- identity -> tenant id ---------------------------------------------------
#
# Asked of swarm_common.identity rather than restated in shell. The shell copy
# that used to live here was a plain slugify, and a plain slugify is exactly the
# thing the frozen module documents as unsafe: `eng.team@` and `eng-team@` both
# reduce to `eng-team`, so two distinct Google groups would have been registered
# as ONE tenant sharing a namespace, a service account, provider keys and
# artifacts. The frozen module appends a digest of the full principal whenever
# the slug is lossy, so it cannot do that -- and the API derives the caller's
# tenant with the same function, so anything else registers a tenant nobody ever
# resolves to.
derive_tenant_id() {
  local kind="$1" principal="$2"
  python3 - "${REPO_ROOT}" "${kind}" "${principal}" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common.identity import tenant_id_for_group, tenant_id_for_user

kind, principal = sys.argv[2], sys.argv[3]
print(tenant_id_for_group(principal) if kind == "group" else tenant_id_for_user(principal))
PY
}

KIND=""
PRINCIPAL=""
if [[ -n "${GROUP}" ]]; then
  KIND="group"
  PRINCIPAL="${GROUP}"
elif [[ -n "${USER_EMAIL}" ]]; then
  KIND="user"
  PRINCIPAL="${USER_EMAIL}"
elif [[ "${ADD_PROVIDER_GIVEN}" -eq 1 && -n "${TENANT_ID}" ]]; then
  # --add-provider may name the tenant by id alone, because it creates nothing:
  # it changes a tenants/<id> document that must already exist and refuses in
  # section A if there is none. So a typed id cannot register boundaries under
  # a name nothing resolves to -- the reason `--tenant` is only an assertion
  # below -- and create-secrets.sh, which knows a tenant only by its id, can
  # print a command that works exactly as written. The principal is read off
  # the document instead.
  :
else
  die "give either --group <group@saga.xyz> or --user <person@saga.xyz>
  (or, to add one provider to a tenant already registered: --tenant <id> --add-provider <provider>)"
fi

if [[ -n "${PRINCIPAL}" ]]; then
  DERIVED_TENANT_ID="$(derive_tenant_id "${KIND}" "${PRINCIPAL}")" \
    || die "swarm_common.identity could not derive a tenant id from ${PRINCIPAL}"
  [[ -n "${DERIVED_TENANT_ID}" ]] || die "could not derive a tenant id from ${PRINCIPAL}"

  # `--tenant` is an override for reading a registration out loud, not a way to
  # choose an id: the API derives the caller's tenant from the token and never
  # looks at this. An id that disagrees registers boundaries under a name nothing
  # ever resolves to, so it is refused rather than honoured.
  if [[ -n "${TENANT_ID}" && "${TENANT_ID}" != "${DERIVED_TENANT_ID}" ]]; then
    die "--tenant '${TENANT_ID}' does not match the id the API will derive for ${PRINCIPAL}.
  swarm_common.identity resolves that principal to '${DERIVED_TENANT_ID}'; every request
  from a member of ${PRINCIPAL} would land in '${DERIVED_TENANT_ID}' and find nothing
  registered. Drop --tenant, or register the principal that really owns this tenant."
  fi
  TENANT_ID="${DERIVED_TENANT_ID}"
fi

case "${TENANT_ID}" in
  *[!a-z0-9-]*) die "tenant id '${TENANT_ID}' is not a valid slug" ;;
esac

if [[ -n "${PRINCIPAL}" ]]; then
  DOMAIN="${PRINCIPAL##*@}"
  ALLOWED_DOMAINS="${ALLOWED_DOMAINS:-saga.xyz}"
  case ",${ALLOWED_DOMAINS}," in
    *",${DOMAIN},"*) ;;
    *) die "${PRINCIPAL} is outside the allowed hosted domain(s) (${ALLOWED_DOMAINS}); authentication would reject it anyway" ;;
  esac
fi

# --- names, all of them owned by another track -------------------------------
#
# GSA: terraform/modules/tenancy/main.tf builds `swarm-agent-worker-<tenant>`,
# cloud_run_jobs binds it to each (tenant, profile) Job, and kubernetes/render.py
# defaults to it. It is the identity a worker actually runs as, so it is the one
# that has to receive every grant below.
GSA_PREFIX="swarm-agent-worker-"
GSA_ID="${GSA_PREFIX}${TENANT_ID}"

# A GCP service account id is capped at 30 characters, and this is NOT truncated
# to fit. Truncating is how two tenants end up sharing one workload identity:
# with a 30-character cut, every tenant id agreeing in its first 11 characters
# collapses onto one service account, and both tenants' secretAccessor bindings
# and both tenants' GCS prefix conditions then accumulate on it -- each tenant
# able to read the other's provider keys. terraform/modules/tenancy/variables.tf
# refuses the same ids for the same reason, so both provisioning paths agree on
# which tenants can exist.
#
# This is a RESTATEMENT of swarm_common.identity's `_MAX_TENANT_ID`, which is
# computed there the same way from the same prefix. scripts/lib/check-contract-parity.sh
# asserts the two still agree, so the prefix moving on either side fails `make test`
# rather than surfacing as a tenant the API resolves and nobody can provision.
MAX_TENANT_ID=$(( 30 - ${#GSA_PREFIX} ))
if [[ -z "${PRINCIPAL}" && "${#TENANT_ID}" -gt "${MAX_TENANT_ID}" ]]; then
  # A TYPED id (--tenant with --add-provider), so the drift diagnosis below does
  # not apply: no tenant can be registered under an id this long.
  die "tenant id '${TENANT_ID}' is ${#TENANT_ID} characters; no tenant id is longer than ${MAX_TENANT_ID}
  (the worker is '${GSA_PREFIX}<tenant>' and GCP caps a service account id at 30), so
  no tenant is registered under it. Check the id -- scripts/status.sh lists tenants."
elif [[ "${#TENANT_ID}" -gt "${MAX_TENANT_ID}" ]]; then
  die "tenant id '${TENANT_ID}' is ${#TENANT_ID} characters; the limit here is ${MAX_TENANT_ID}.

  The service account is '${GSA_PREFIX}<tenant>' and GCP caps a service account id at 30
  characters, so ${MAX_TENANT_ID} is all that is left. Truncating to fit would silently merge this
  tenant with any other whose id shares its first ${MAX_TENANT_ID} characters -- one shared workload
  identity, both tenants' secretAccessor bindings and both tenants' GCS prefix conditions
  accumulating on it. So this refuses, exactly as terraform/modules/tenancy does.

  THIS SHOULD BE UNREACHABLE, and that is the useful part of the message. This id was
  DERIVED by swarm_common.identity, not typed: '--tenant' is only ever checked for
  agreement with the derived value. The frozen module budgets its slugs against the same
  '${GSA_PREFIX}' prefix and the same 30-character cap, so it cannot mint an id longer
  than ${MAX_TENANT_ID} -- a principal whose slug would overrun gets a shortened slug plus a
  6-character digest instead. Reaching this line means the frozen module and this script
  have DRIFTED: its budget is now larger than the one this prefix leaves.

  So do not work around it per tenant. Compare swarm_common.identity's _GSA_PREFIX and
  _MAX_TENANT_ID against GSA_PREFIX here and in terraform/modules/tenancy, and make them
  agree again; scripts/lib/check-contract-parity.sh asserts exactly that pair and would
  normally have failed 'make test' before you got here.
  See docs/multi-tenancy.md section 6 -- note that that section still describes an
  earlier 22-character cap in the frozen module, which no longer exists."
fi

GSA_EMAIL="${GSA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

# NO APP KEY FOR THE WORKER. The full registration binds every listed
# provider's secret to the worker, and the worker's token is one any agent of
# the tenant can mint from the metadata server (docs/merge-step.md §0) -- so a
# retired #295 App key (git-merge, git-review) is refused before anything is
# created, rather than granted to it.
if [[ -n "${PROVIDERS_CSV}" ]]; then
  IFS=',' read -r -a REQUESTED_PROVIDERS <<<"${PROVIDERS_CSV}"
  for requested in ${REQUESTED_PROVIDERS[@]+"${REQUESTED_PROVIDERS[@]}"}; do
    is_app_provider "${requested}" || continue
    die "$(retired_app_provider_refusal "${requested}")"
  done
fi

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    dim "  would run: $*"
    return 0
  fi
  "$@"
}

# --- whose secret is it? -----------------------------------------------------
#
# SECRET NAMES DO NOT SPLIT. `swarm-tenant-<tenant>-<provider>` joins two
# values that may both contain hyphens with a hyphen, so one name is several
# pairs at once:
#
#   tenant eng-team,      provider git       -> swarm-tenant-eng-team-git
#   tenant eng,           provider team-git  -> swarm-tenant-eng-team-git
#   tenant eng-anthropic, provider refresh   -> swarm-tenant-eng-anthropic-refresh
#                                               = tenant eng's anthropic REFRESH half
#
# So a grant made on the name alone can bind one tenant's worker to another
# tenant's key -- invariant 9 -- and the last line walks straight past the
# `*-refresh` refusal, to the one secret no worker may read. The name cannot say
# whose a secret is; its labels can. scripts/create-secrets.sh and
# terraform/modules/secret_manager both label every tenant credential with
# `tenant` and `provider` (the module's comment gives this same reason), and a
# `-refresh` secret carries its BASE provider, so it never matches a worker's
# provider. A grant is made only on a secret whose labels name this tenant and
# this provider exactly. One with neither label is refused as well: nothing can
# say whose it is.
#
# tenant_secret_state SECRET PROVIDER   (the tenant is TENANT_ID)
#   0  this tenant's: labelled tenant=TENANT_ID provider=PROVIDER, with an
#      ENABLED version
#   1  absent -- gcloud answered NOT_FOUND
#   2  cannot tell -- a lookup failed; the reason is printed
#   3  labelled as another tenant's or provider's, or not labelled at all;
#      SECRET_LABEL_TENANT / SECRET_LABEL_PROVIDER say what the labels are
#   4  this tenant's, but with no ENABLED version: nothing for a worker to read
#
# Called as `tenant_secret_state ... || rc=$?`, never inside `$(...)`: the labels
# come back in globals, which a subshell would throw away.
SECRET_LABEL_TENANT=""
SECRET_LABEL_PROVIDER=""
tenant_secret_state() {
  local secret="$1" provider="$2" out errfile reason rc=0 enabled=""
  SECRET_LABEL_TENANT=""
  SECRET_LABEL_PROVIDER=""
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-secret.XXXXXX")"
  errfile="$(mktemp "${TMPDIR:-/tmp}/swarm-secret-err.XXXXXX")"

  gcloud secrets describe "${secret}" --project "${PROJECT_ID}" --format=json \
    >"${out}" 2>"${errfile}" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    reason="$(cat "${errfile}")"
    rm -f "${out}" "${errfile}"
    die_if_auth_failure "${reason}"
    # Only a recognisable NOT_FOUND is an absence; anything else is a failure
    # to look, and must not send the operator off to create a secret.
    gcloud_not_found "${reason}" && return 1
    err "could not establish whether ${secret} exists:"
    printf '%s\n' "${reason}" | redact | head -n 3 | sed 's/^/     /' >&2
    return 2
  fi
  if ! jq -e 'type == "object"' "${out}" >/dev/null 2>&1; then
    rm -f "${out}" "${errfile}"
    err "gcloud described ${secret} but did not answer with a JSON object, so its labels cannot be read"
    return 2
  fi
  SECRET_LABEL_TENANT="$(jq -r '.labels.tenant // ""' "${out}")"
  SECRET_LABEL_PROVIDER="$(jq -r '.labels.provider // ""' "${out}")"
  rm -f "${out}"
  if [[ "${SECRET_LABEL_TENANT}" != "${TENANT_ID}" || "${SECRET_LABEL_PROVIDER}" != "${provider}" ]]; then
    rm -f "${errfile}"
    return 3
  fi

  # A secret with no ENABLED version is a name with nothing behind it: listing
  # its provider admits the tenant's tasks for it and fails every one when the
  # worker asks for the key.
  if ! enabled="$(gcloud secrets versions list "${secret}" --project "${PROJECT_ID}" \
       --filter='state=ENABLED' --limit=1 --format='value(name)' 2>"${errfile}")"; then
    reason="$(cat "${errfile}")"
    rm -f "${errfile}"
    die_if_auth_failure "${reason}"
    err "could not list the enabled versions of ${secret}:"
    printf '%s\n' "${reason}" | redact | head -n 3 | sed 's/^/     /' >&2
    return 2
  fi
  rm -f "${errfile}"
  [[ -n "${enabled}" ]] || return 4
  return 0
}

# refuse_foreign_secret SECRET PROVIDER  -- after tenant_secret_state returned 3.
refuse_foreign_secret() {
  local secret="$1" provider="$2"
  if [[ -z "${SECRET_LABEL_TENANT}" && -z "${SECRET_LABEL_PROVIDER}" ]]; then
    # Only an UNLABELLED secret gets a relabel command. One labelled as someone
    # else's does not: pasting that command would make another tenant's key
    # look like this tenant's, which is the grant being refused.
    die "${secret} carries no tenant or provider label, so nothing says whose key it is.
  swarm-tenant-<tenant>-<provider> does not split -- tenant eng-team's git and tenant eng's
  team-git are the same name -- so ${GSA_ID} is granted only a secret labelled as this
  tenant's. If you have checked that ${secret} really is tenant ${TENANT_ID}'s ${provider} key,
  label it and run this again:
      gcloud secrets update ${secret} --project ${PROJECT_ID} --update-labels=tenant=${TENANT_ID},provider=${provider}
  Nothing was granted."
  fi
  die "${secret} is labelled tenant='${SECRET_LABEL_TENANT}' provider='${SECRET_LABEL_PROVIDER}',
  not tenant='${TENANT_ID}' provider='${provider}'. It is another tenant's or another provider's
  secret that happens to share this name: swarm-tenant-<tenant>-<provider> does not split, so
  tenant eng-team's git and tenant eng's team-git are one name, and tenant eng-anthropic's
  'refresh' is tenant eng's anthropic refresh half. Refusing to let ${GSA_ID} read it
  (invariant 9: a tenant's key is readable by that tenant's worker alone). Nothing was granted."
}

# require_grantable_secret SECRET PROVIDER READER
#
# --add-provider's check on a secret before READER (an account id, for the
# message) is bound to it: this tenant's, for this provider, with a key in it
# (tenant_secret_state above). Tri-state, as at the custom roles in section 3:
# a denied or expired lookup is not an absent secret, and must not send the
# operator to create one. Returns only for a secret that may be granted.
require_grantable_secret() {
  local sm_name="$1" provider="$2" reader="$3" rc=0
  tenant_secret_state "${sm_name}" "${provider}" || rc=$?
  case "${rc}" in
    0)
      ok "${sm_name} is labelled tenant=${TENANT_ID} provider=${provider} and has an enabled version"
      ;;
    1)
      die "${sm_name} does not exist, so there is no ${provider} key for ${reader}
  to read. Store it first, then run this again:
      scripts/create-secrets.sh --tenant ${TENANT_ID} --provider ${provider} --stdin
  Nothing was changed: listing ${provider} with no secret behind it would admit this
  tenant's ${provider} tasks and fail every one of them when the worker asks for the key."
      ;;
    3)
      refuse_foreign_secret "${sm_name}" "${provider}"
      ;;
    4)
      die "${sm_name} is tenant ${TENANT_ID}'s, but it has no ENABLED version, so there is no
  ${provider} key in it for ${reader} to read. Nothing was changed: listing
  ${provider} now would admit this tenant's ${provider} tasks and fail every one of them.
  Store a key, then run this again:
      scripts/create-secrets.sh --tenant ${TENANT_ID} --provider ${provider} --stdin
  (A subscription credential is different: its versions here are written by the quota
  broker from ${sm_name}-refresh on its next sweep. Run this again once one has been.)"
      ;;
    *)
      die "stopping: whether ${sm_name} exists, and whose it is, could not be established
  (gcloud's answer is above). That is a failure to LOOK, not a missing secret -- fix the
  session or the permission and run this again. Nothing was changed."
      ;;
  esac
}

# --- swarm-api, the second reader of -git ------------------------------------
#
# swarm-api's issue preview (apps/swarm-api/swarm_api/forge.py `preview`,
# routes/issues.py, #511) reads swarm-tenant-<tenant>-git, the forge token the
# worker clones and pushes with. The owner accepted swarm-api as that secret's
# second reader (2026-10-02). terraform/infra grants it through `forge_readers`,
# but only on a -git secret Terraform manages, and none is: every tenant's -git
# is registered here. So wherever this script lets the worker read -git, it
# lets swarm-api read that same ONE secret -- a binding on the secret, never on
# the project, where it would read every tenant's every key.
#
# EXACTLY `git`. Never git-merge or git-review, the retired #295 App keys,
# which this script refuses outright.
FORGE_PROVIDER="git"
FORGE_READER_ID="swarm-api"
FORGE_READER_EMAIL=""

# resolve_forge_reader  ->  sets FORGE_READER_EMAIL from
# terraform/modules/service_account_ids, or dies with nothing changed. Called
# before the first grant of the path that needs it.
resolve_forge_reader() {
  [[ -z "${FORGE_READER_EMAIL}" ]] || return 0
  FORGE_READER_EMAIL="$(platform_account_email "${FORGE_READER_ID}")" \
    || die "terraform/modules/service_account_ids lists no platform account ${FORGE_READER_ID} (or its
  platform map no longer reads the way common.sh tf_platform_account_ids expects), so there is no
  address to let the issue preview read swarm-tenant-${TENANT_ID}-${FORGE_PROVIDER} as. Nothing was changed."
}

# forge_reader_bound NAME  ->  0 when swarm-api already holds secretAccessor
# on the secret NAME, so a re-run adds nothing. A policy that cannot be read answers
# "not bound" (iam_policy_binds_member, common.sh): the add that follows is
# idempotent and fails loudly, where guessing "bound" is a silent missing grant.
forge_reader_bound() {
  local name="$1" policy_file errfile rc=0
  policy_file="$(mktemp "${TMPDIR:-/tmp}/swarm-git-policy.XXXXXX")"
  errfile="$(mktemp "${TMPDIR:-/tmp}/swarm-git-policy-err.XXXXXX")"
  if ! gcloud secrets get-iam-policy "${name}" --project "${PROJECT_ID}" \
         --format=json >"${policy_file}" 2>"${errfile}"; then
    local reason
    reason="$(cat "${errfile}")"
    rm -f "${policy_file}" "${errfile}"
    die_if_auth_failure "${reason}"
    return 1
  fi
  iam_policy_binds_member "${policy_file}" roles/secretmanager.secretAccessor \
    "serviceAccount:${FORGE_READER_EMAIL}" || rc=1
  rm -f "${policy_file}" "${errfile}"
  return "${rc}"
}

# --- A. add one provider to a registered tenant, and change nothing else -----
#
# WHY THIS IS ITS OWN PATH. Adding a provider to a tenant that already has some
# is the commonest change after registration, and both hints this platform
# printed for it were a re-run of the full registration: create-secrets.sh's
# `register-tenant.sh --tenant <t> --providers <p>`, and the closing "Next:"
# block of this script, `--<kind> <principal> --providers anthropic`. Followed
# as printed:
#
#   * the first died on its first line: the full registration needs --group or
#     --user, and create-secrets.sh knows the tenant only by id;
#   * the second REPLACED `credentials` with the one provider it named --
#     section 6's field mask includes the whole list -- so a tenant holding
#     openai came out holding anthropic alone, and every openai task parked as
#     CREDENTIAL_MISSING;
#   * both reset max_active and capacity_units to 20 and 40 and display_name to
#     the principal, and moved the tenant pool's ceiling with them, unless the
#     operator retyped values nothing had shown them.
#
# A hint that printed every current provider and every current limit could
# have been made correct, but only by reading the tenant document from
# create-secrets.sh, and it would still be a command that rewrites the whole
# record to change one field -- correct only until the record gains a field
# the hint does not know to carry. Adding a provider needs exactly two changes,
# and this path makes those two:
#
#   1. roles/secretmanager.secretAccessor for the tenant's worker on that ONE
#      secret -- the same additive grant section 4 makes;
#   2. the provider added to `credentials`, which admission and the worker
#      consult before they ask Secret Manager for anything.
#
# IN THAT ORDER. Listed-but-unreadable admits the provider's tasks and fails
# each one when the worker asks for its key; readable-but-unlisted is inert.
# So a run that stops between the two stops in the harmless state.
#
# THE LIST IS A UNION WRITTEN UNDER A PRECONDITION. `credentials` is read, the
# provider added, and the sorted result written with an update mask of
# `credentials` alone -- the same list swarm_api.store.register_credential
# writes when a tenant member stores a key through the API. The write carries
# the document's updateTime as read, so if anything changed tenants/<id> in
# between -- that API route, another operator -- Firestore refuses it
# (FAILED_PRECONDITION) instead of this silently dropping the provider they
# added. A precondition also means the PATCH cannot create the document, so a
# tenant removed in between does not come back as a record holding nothing but
# `credentials`.
if [[ "${ADD_PROVIDER_GIVEN}" -eq 1 ]]; then
  step "Add provider ${ADD_PROVIDER} to tenant ${TENANT_ID}"
  info "service acct   ${GSA_EMAIL}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    warn "DRY RUN: nothing will be changed"
  fi

  require_fs_database "add a provider to tenant ${TENANT_ID}"
  TENANT_RAW="$(fs_get "tenants/${TENANT_ID}")" \
    || die "could not read tenants/${TENANT_ID} (reason above). Nothing was changed."
  TENANT_DOC="$(jq -c "${FS_JQ} if .fields then doc else null end" <<<"${TENANT_RAW}")"
  if [[ -z "${TENANT_DOC}" || "${TENANT_DOC}" == "null" ]]; then
    die "tenants/${TENANT_ID} does not exist, so there is no tenant to add ${ADD_PROVIDER} to.
  --add-provider changes a registered tenant and creates nothing. Register it first --
  that is the path that creates its identity, grants and namespace:
      scripts/register-tenant.sh --group <group> --providers ${ADD_PROVIDER}
  (--user <person> for a personal tenant). Nothing was changed."
  fi

  DOC_PRINCIPAL="$(jq -r '.principal // ""' <<<"${TENANT_DOC}")"
  DOC_KIND="$(jq -r '.kind // ""' <<<"${TENANT_DOC}")"
  DOC_SA="$(jq -r '.service_account // ""' <<<"${TENANT_DOC}")"
  [[ -n "${DOC_PRINCIPAL}" ]] \
    || die "tenants/${TENANT_ID} exists but names no principal, so it is not a registered tenant.
  Run the full registration (--group or --user) instead. Nothing was changed."
  # Section 0's guard, for the same reason: a principal that does not own this
  # id is asking to hand its members this tenant's keys.
  if [[ -n "${PRINCIPAL}" ]] \
     && { [[ "${DOC_PRINCIPAL}" != "${PRINCIPAL}" ]] \
          || [[ -n "${DOC_KIND}" && "${DOC_KIND}" != "${KIND}" ]]; }; then
    die "tenant '${TENANT_ID}' belongs to ${DOC_KIND} ${DOC_PRINCIPAL}, not ${KIND} ${PRINCIPAL}.
  Refusing to change it on behalf of a principal that does not own it. Nothing was changed."
  fi
  # The document and the naming rule must agree on WHO reads the key.
  # swarm_api.credentials binds the document's service_account; this script,
  # terraform/modules/tenancy and kubernetes/render.py all name the worker
  # swarm-agent-worker-<tenant>. If they differ one of them is wrong, and
  # binding either would be a guess about which identity may read this
  # tenant's key.
  if [[ -n "${DOC_SA}" && "${DOC_SA}" != "${GSA_EMAIL}" ]]; then
    die "tenants/${TENANT_ID} records its service account as ${DOC_SA}, but this tenant's
  worker runs as ${GSA_EMAIL} (terraform/modules/tenancy, kubernetes/render.py and this
  script all use that name). Refusing to guess which of the two may read the ${ADD_PROVIDER}
  key. Nothing was changed."
  fi
  ok "tenants/${TENANT_ID} belongs to ${DOC_KIND} ${DOC_PRINCIPAL}"

  # 1. the grant, and only on a secret that is this tenant's, for this
  # provider, with a key in it (require_grantable_secret above).
  provider="${ADD_PROVIDER}"
  secret="swarm-tenant-${TENANT_ID}-${provider}"
  # (secret, member, account id) per grant, made in this order once every
  # check below has passed -- so a refusal anywhere changes nothing.
  GRANT_SECRETS=()
  GRANT_MEMBERS=()
  GRANT_NAMES=()
  require_grantable_secret "${secret}" "${provider}" "this tenant's worker"
  GRANT_SECRETS+=("${secret}")
  GRANT_MEMBERS+=("serviceAccount:${GSA_EMAIL}")
  GRANT_NAMES+=("${GSA_ID}")
  # After the worker's, so a run that stops between the two leaves the
  # worker able to read its key and only the preview without it.
  if [[ "${provider}" == "${FORGE_PROVIDER}" ]]; then
    resolve_forge_reader
    if forge_reader_bound "${secret}"; then
      ok "${secret}: ${FORGE_READER_ID} already reads it (the issue preview)"
    else
      GRANT_SECRETS+=("${secret}")
      GRANT_MEMBERS+=("serviceAccount:${FORGE_READER_EMAIL}")
      GRANT_NAMES+=("${FORGE_READER_ID}")
    fi
  fi

  for ((i = 0; i < ${#GRANT_SECRETS[@]}; i++)); do
    run gcloud secrets add-iam-policy-binding "${GRANT_SECRETS[$i]}" \
      --project "${PROJECT_ID}" \
      --member "${GRANT_MEMBERS[$i]}" \
      --role roles/secretmanager.secretAccessor --quiet >/dev/null
    # A dry run granted nothing, so it must not say it did.
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      dim "  would let ${GRANT_NAMES[$i]} read ${GRANT_SECRETS[$i]}"
    else
      ok "${GRANT_SECRETS[$i]}: ${GRANT_NAMES[$i]} may read it"
    fi
  done

  # 2. the list.
  CURRENT_CREDS="$(jq -c '[(.credentials // [])[] | tostring]' <<<"${TENANT_DOC}")"
  if jq -e --arg p "${provider}" 'any(.[]; . == $p)' <<<"${CURRENT_CREDS}" >/dev/null; then
    ok "tenants/${TENANT_ID} already lists ${provider}; credentials unchanged: $(jq -r 'join(", ")' <<<"${CURRENT_CREDS}")"
  else
    # `unique` sorts, as `sorted(set(...))` does in register_credential.
    NEW_CREDS="$(jq -c --arg p "${provider}" '. + [$p] | unique' <<<"${CURRENT_CREDS}")"
    UPDATE_TIME="$(jq -r '.updateTime // ""' <<<"${TENANT_RAW}")"
    [[ "${UPDATE_TIME}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$ ]] \
      || die "tenants/${TENANT_ID} came back without a usable updateTime ('${UPDATE_TIME}'), so the
  write cannot be made conditional on it. Nothing was listed; the grant above is inert alone."
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      dim "  would set tenants/${TENANT_ID} credentials ${CURRENT_CREDS} -> ${NEW_CREDS}"
      dim "  (update mask: credentials alone; applied only if the document is still at ${UPDATE_TIME})"
    else
      fs_patch "tenants/${TENANT_ID}" "credentials" \
        "$(jq -c '{credentials:{arrayValue:{values:map({stringValue:.})}}}' <<<"${NEW_CREDS}")" \
        "${UPDATE_TIME}" \
        || die "tenants/${TENANT_ID} was not updated (Firestore's answer is above). If it says
  FAILED_PRECONDITION, the document changed after it was read: run this again to add
  ${provider} to the list as it is now. The grant above is in place and inert on its own --
  a provider the tenant does not list is never asked for."
      ok "tenants/${TENANT_ID} credentials: $(jq -r 'join(", ")' <<<"${CURRENT_CREDS}") -> $(jq -r 'join(", ")' <<<"${NEW_CREDS}")"
    fi
  fi

  hr
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    warn "DRY RUN: nothing was changed. Without --dry-run, tenant ${TENANT_ID} would have ${provider}"
    dim "  and nothing else would be written: limits, display name, pool, namespace, other grants and the other providers would stay as they are"
  else
    ok "tenant ${TENANT_ID} has ${provider}"
    dim "  nothing else was written: limits, display name, pool, namespace, other grants and the other providers are as they were"
  fi
  exit 0
fi

# Namespace and KSAs. `kubernetes/render.py` is what CREATES these objects, so
# this script must name exactly what that renders -- it does not create them
# itself, it only issues the GCP-side Workload Identity bindings for them and
# records the namespace on the tenant document.
#
# THIS LINE WAS THE THIRD COPY OF THE 2026-09-23 OUTAGE. It read
# `NAMESPACE="swarm-${TENANT_ID}"` while the scheduler dispatched into
# `swarm-tenant-<id>`, and it is the worst of the three copies because of where
# the value goes: section 6 below writes it into the tenant document's
# `namespace` field, and `GkeJobDispatcher.namespace_for` PREFERS that field
# over its own template. So the Firestore record this script wrote was
# overriding the one spelling that was correct, and every `browser` task for
# such a tenant was dispatched into a namespace nothing had created -- reported
# by Kubernetes as `jobs.batch is forbidden`, never as a missing namespace,
# because it authorises before it resolves. See docs/gke-dispatch-403.md.
#
# `tenant_namespace` is the one shell copy of the prefix, in lib/common.sh;
# `scripts/lib/check-contract-parity.sh` section 6 asserts it against the
# scheduler, the renderer, the reconciler, the API and terraform.
NAMESPACE="$(tenant_namespace "${TENANT_ID}")"
# Both worker KSAs, because a binding is per (namespace, KSA) pair and a pod
# naming an unbound one authenticates as nothing at all. `swarm-agent-worker` is
# the name the dispatcher's pod spec actually uses and the name terraform binds;
# `swarm-worker` is the older one. kubernetes/apply.sh renders `swarm-worker`
# only where it finds this binding in IAM (kubernetes/render.py LEGACY_KSA_NAME),
# which is why section 5 binds before it applies. The `swarm-<tenant>`
# alias is gone -- see kubernetes/render.py DEFAULT_KSA_NAME for why it existed
# and why it no longer does.
KSAS=("swarm-worker" "swarm-agent-worker")
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
info "limits         max_active=${MAX_ACTIVE} capacity_units=${CAPACITY_UNITS} (pool ceiling ${POOL_HARD_LIMIT}, the smaller)"
[[ "${#PROVIDERS[@]}" -eq 0 ]] || info "providers      ${PROVIDERS[*]}"
[[ "${DRY_RUN}" -eq 1 ]] && warn "DRY RUN: nothing will be created"

# --- 0. never re-point an existing tenant ------------------------------------
#
# Re-running this script is advertised as harmless idempotency, and for limits
# and wiring it is. It also used to rewrite IDENTITY: `principal` and `kind` were
# both in the field mask, and nothing compared the incoming principal against the
# one already stored. So a single
#
#     register-tenant.sh --group contractors@saga.xyz --tenant eng
#
# made every member of contractors@ resolve to tenant eng and inherit eng's
# provider keys, artifact prefix and budget -- no warning, no confirmation, and
# no trace beyond one changed field.
#
# Checked before anything is created, so a mistyped principal costs nothing.
# A lookup that FAILED must not read as "no tenant document yet": that is the
# path that silently skips the collision guard below and lets a mistyped id
# take over another tenant's service account, secrets and GCS prefix. Only a
# confirmed-absent database (exit 1) is allowed to skip it.
PREFLIGHT_RC=0
fs_database_exists || PREFLIGHT_RC=$?
[[ "${PREFLIGHT_RC}" -le 1 ]] || die "cannot confirm Firestore database '${FIRESTORE_DATABASE}' exists (reason above), so the check that this tenant id is not already taken cannot run. Refusing to register."
if [[ "${PREFLIGHT_RC}" -eq 0 ]]; then
  EXISTING_DOC="$(fs_get "tenants/${TENANT_ID}" | jq -c "${FS_JQ} if .fields then doc else null end")"
  if [[ -n "${EXISTING_DOC}" && "${EXISTING_DOC}" != "null" ]]; then
    EXISTING_PRINCIPAL="$(jq -r '.principal // ""' <<<"${EXISTING_DOC}")"
    EXISTING_KIND="$(jq -r '.kind // ""' <<<"${EXISTING_DOC}")"
    if [[ -n "${EXISTING_PRINCIPAL}" && "${EXISTING_PRINCIPAL}" != "${PRINCIPAL}" ]] \
       || [[ -n "${EXISTING_KIND}" && "${EXISTING_KIND}" != "${KIND}" ]]; then
      die "tenant '${TENANT_ID}' already belongs to ${EXISTING_KIND} ${EXISTING_PRINCIPAL}.
  You asked to register ${KIND} ${PRINCIPAL} under the same id, which would hand every
  member of ${PRINCIPAL} that tenant's provider keys, artifacts and budget.
  Refusing. To retire the existing tenant deliberately:
      scripts/purge-data.sh --tenant ${TENANT_ID} --dry-run
  and then remove tenants/${TENANT_ID} before re-registering."
    fi
    ok "tenants/${TENANT_ID} already belongs to ${EXISTING_KIND} ${EXISTING_PRINCIPAL}; updating limits and wiring"
  fi
fi

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
SA_PREEXISTED=0
if gcloud iam service-accounts describe "${GSA_EMAIL}" \
     --project "${PROJECT_ID}" --format='value(email)' >/dev/null 2>&1; then
  SA_PREEXISTED=1
  ok "${GSA_EMAIL} exists"
else
  run gcloud iam service-accounts create "${GSA_ID}" \
    --project "${PROJECT_ID}" \
    --display-name "${DISPLAY_NAME:-swarm tenant ${TENANT_ID}}" \
    --description "Agent swarm workload identity for tenant ${TENANT_ID} (${PRINCIPAL})"
  ok "created ${GSA_EMAIL}"
fi

# --- 2b. the release deployer's grant on this account --------------------------
#
# THE BOOTSTRAP STEP COMES BEFORE THE RELEASE (#334, owner decision 2026-09-29).
# The release deployer holds roles/iam.serviceAccountAdmin on each account
# terraform/infra manages, granted one account at a time by
# terraform/bootstrap/deployer_service_accounts.tf -- no longer on the project.
# The release sets this account's IAM policy (actAs, Workload Identity) in the
# same apply that adds the tenant, and without that grant it 403s. The grant
# cannot be made before the account exists, which is why the account is created
# above, here, rather than by the release; terraform's
# create_ignore_already_exists adopts it.
#
# AN ACCOUNT THAT ALREADY EXISTED IS INSPECTED FIRST (#334 security review).
# Adoption is also squatting: whoever made `swarm-agent-worker-<tenant>` before
# this platform did chose its IAM policy and may hold a key to it, and the
# tenant's provider keys, artifacts and Firestore access would follow. So an
# existing account must carry nothing but what the platform itself grants --
# the deployer's serviceAccountAdmin and serviceAccountUser, the scheduler's and
# reconciler's serviceAccountUser, workloadIdentityUser for this tenant's two
# Kubernetes service accounts -- with no condition on any binding, and no
# user-managed key. Anything else stops the registration here, before a single
# grant is made to the account. A policy or key list that cannot be read stops
# it too: an account that cannot be inspected cannot be shown to be ours. These
# are reads, so they run under --dry-run as well.
#
# The deployer's grant is checked, not applied: bootstrap is the owner's root
# and runs as the owner, from main.
step "Release deployer grant"
DEPLOYER_SA="${DEPLOYER_SERVICE_ACCOUNT:-swarm-tf-deployer@${PROJECT_ID}.iam.gserviceaccount.com}"
BOOTSTRAP_TARGET="google_service_account_iam_member.deployer_admin[\"${GSA_ID}\"]"

if [[ "${SA_PREEXISTED}" -eq 1 ]]; then
  ALLOWED_PAIRS=(
    "roles/iam.serviceAccountAdmin serviceAccount:${DEPLOYER_SA}"
    "roles/iam.serviceAccountUser serviceAccount:${DEPLOYER_SA}"
    "roles/iam.serviceAccountUser serviceAccount:swarm-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
    "roles/iam.serviceAccountUser serviceAccount:swarm-reconciler@${PROJECT_ID}.iam.gserviceaccount.com"
  )
  for ksa in "${KSAS[@]}"; do
    ALLOWED_PAIRS+=("roles/iam.workloadIdentityUser serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa}]")
  done
  ALLOWED_JSON='[]'
  for pair in "${ALLOWED_PAIRS[@]}"; do
    ALLOWED_JSON="$(jq -c --arg p "${pair}" '. + [$p]' <<<"${ALLOWED_JSON}")"
  done

  SQUAT_POLICY_FILE="$(mktemp)"
  SQUAT_KEYS_FILE="$(mktemp)"
  if ! gcloud iam service-accounts get-iam-policy "${GSA_EMAIL}" \
         --project "${PROJECT_ID}" --format=json >"${SQUAT_POLICY_FILE}" 2>/dev/null; then
    rm -f "${SQUAT_POLICY_FILE}" "${SQUAT_KEYS_FILE}"
    die "${GSA_EMAIL} already existed and its IAM policy could not be read; refusing to adopt an account that cannot be inspected (#334)"
  fi
  if ! UNEXPECTED="$(jq -r --argjson allowed "${ALLOWED_JSON}" '
        .bindings[]? as $b | $b.members[]? as $m
        | select(($b.condition != null) or (($allowed | any(. == ($b.role + " " + $m))) | not))
        | "\($b.role) \($m)" + (if $b.condition != null then " (conditioned)" else "" end)
      ' "${SQUAT_POLICY_FILE}")"; then
    rm -f "${SQUAT_POLICY_FILE}" "${SQUAT_KEYS_FILE}"
    die "${GSA_EMAIL} already existed and its IAM policy is not readable JSON; refusing to adopt it (#334)"
  fi
  if ! gcloud iam service-accounts keys list --iam-account "${GSA_EMAIL}" \
         --project "${PROJECT_ID}" --managed-by=user --format='value(name)' >"${SQUAT_KEYS_FILE}" 2>/dev/null; then
    rm -f "${SQUAT_POLICY_FILE}" "${SQUAT_KEYS_FILE}"
    die "${GSA_EMAIL} already existed and its keys could not be listed; refusing to adopt an account that cannot be inspected (#334)"
  fi
  USER_KEYS="$(grep -c . "${SQUAT_KEYS_FILE}" || true)"
  rm -f "${SQUAT_POLICY_FILE}" "${SQUAT_KEYS_FILE}"

  if [[ -n "${UNEXPECTED}" || "${USER_KEYS}" -gt 0 ]]; then
    err "${GSA_EMAIL} ALREADY EXISTED and carries what this platform never grants it:"
    if [[ -n "${UNEXPECTED}" ]]; then
      while IFS= read -r line; do
        err "  binding  ${line}"
      done <<<"${UNEXPECTED}"
    fi
    [[ "${USER_KEYS}" -gt 0 ]] && err "  ${USER_KEYS} user-managed key(s)"
    err "terraform would ADOPT this account as tenant '${TENANT_ID}'s identity, so whoever holds those"
    err "bindings or keys would hold the tenant's provider keys, artifacts and Firestore access."
    die "refusing to register '${TENANT_ID}' onto an account somebody else may control (#334). Find out who made it; delete it, or remove the bindings and keys, and re-run."
  fi
  ok "${GSA_EMAIL} existed and carries only the platform's own grants and no user-managed key"
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  dim "  would check ${DEPLOYER_SA} holds roles/iam.serviceAccountAdmin on ${GSA_EMAIL}"
else
  GSA_POLICY_FILE="$(mktemp)"
  if gcloud iam service-accounts get-iam-policy "${GSA_EMAIL}" \
       --project "${PROJECT_ID}" --format=json >"${GSA_POLICY_FILE}" 2>/dev/null \
     && jq -e --arg m "serviceAccount:${DEPLOYER_SA}" \
          '[.bindings[]? | select(.role == "roles/iam.serviceAccountAdmin" and (.condition == null)) | .members[]?] | index($m) != null' \
          "${GSA_POLICY_FILE}" >/dev/null; then
    ok "${DEPLOYER_SA} holds roles/iam.serviceAccountAdmin on ${GSA_EMAIL}"
  else
    warn "the release deployer does not hold roles/iam.serviceAccountAdmin on ${GSA_EMAIL} yet"
    warn "a release that adds tenant '${TENANT_ID}' to terraform/environments/dev/dev.tfvars will 403 until it does. In this order:"
    warn "  1. add '${TENANT_ID}' to the tenants block of terraform/environments/dev/dev.tfvars on the pull request's branch, and push it"
    warn "  2. the owner, from an up-to-date MAIN checkout (never the branch), takes the branch's file as data only:"
    warn "       git show origin/<branch>:terraform/environments/dev/dev.tfvars > /tmp/pr-dev.tfvars"
    warn "       terraform -chdir=terraform/bootstrap init -reconfigure -backend-config=bucket=${TF_STATE_BUCKET}"
    warn "       terraform -chdir=terraform/bootstrap plan -var infra_tenants_tfvars=/tmp/pr-dev.tfvars -target='${BOOTSTRAP_TARGET}'"
    warn "     checks the plan (docs/ci.md: the deployer_admin_accounts output, no heredoc, '1 to add, 0 to change, 0 to destroy'),"
    warn "     and applies the same command"
    warn "  3. then merge; the release adopts this account and sets its IAM"
  fi
  rm -f "${GSA_POLICY_FILE}"
fi

# --- 3. IAM -------------------------------------------------------------------
step "IAM"

# Firestore, with NO IAM CONDITION. Verified live on 2026-09-16.
#
# This used to attach `resource.name.endsWith('/databases/swarm')` and announce
# the grant as "scoped to the swarm database". Firestore does not evaluate IAM
# Conditions on the DATA PLANE: conditions govern administrative operations --
# creating a database, managing indexes, backups -- and nothing else. A
# conditioned binding therefore does not narrow document access, it REMOVES it.
# Every identity carrying that condition reported
#
#     {"status":"not-ready","detail":"firestore unavailable: PermissionDenied"}
#
# so a tenant onboarded by this script got a worker that could not read its own
# task, its own lease or its own attempt, and parked on its first control-plane
# read. terraform/modules/tenancy learned this first: its
# `scope_firestore_to_database` variable defaults to FALSE for exactly this
# reason, so the two provisioning paths disagreed -- and this script's own rule
# is that they must grant identical authority.
#
# What the condition never bought is worth stating too, because it reads like a
# boundary: the smallest resource Firestore IAM can name is the DATABASE, so
# even when evaluated it scoped which database, never which tenant. The tenant
# boundary here is the SHAPE of the role plus application-level scoping
# (agent_worker.control._assert_tenant), which is what docs/security.md
# "Firestore database scoping does not exist" and docs/multi-tenancy.md's
# Firestore row now say.
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
#
# The ids are derived exactly as terraform/modules/custom_role_ids does, from
# that module's `local.custom_role_suffix` restated here: a CONSTANT there, a
# constant here, and never read from the environment. They used to come from a
# CUSTOM_ROLE_SUFFIX env var, left over from when the suffix was a terraform/infra
# variable; with the roles now defined in terraform/bootstrap from that constant
# (#79), an exported suffix made this script bind a role nobody made (#227).
# Change it only together with the module's value;
# tests/unit/scripts/test_register_tenant_role_ids.py fails when the two differ.
CUSTOM_ROLE_SUFFIX_MODULE=""
# The module's `role_suffix`: "" when the suffix is empty, else "_<suffix>".
ROLE_ID_SUFFIX="${CUSTOM_ROLE_SUFFIX_MODULE:+_${CUSTOM_ROLE_SUFFIX_MODULE}}"
FIRESTORE_ROLE_ID="swarmTenantWorkerFirestore${ROLE_ID_SUFFIX}"
FIRESTORE_ROLE="projects/${PROJECT_ID}/roles/${FIRESTORE_ROLE_ID}"

# THREE ANSWERS, NOT TWO. This was `describe >/dev/null 2>&1`, so a denied
# iam.roles.get or a dead session printed "does not exist. Run make infra" --
# a terraform apply against a shared project, to fix a permission. It is the
# first of the three findings the 2026-09-19 sweep fan-out never landed for
# this script (docs/audits/2026-09-18/13-swallowed-stderr-sweep.md).
# `shared_resource_present` is common.sh's tri-state describe: 0 present, 1 a
# genuine NOT_FOUND, 2 could not tell -- with gcloud's own error printed. It is
# named for the deny-list checks in destroy.sh; the question it answers is the
# same one this asks.
FS_ROLE_RC=0
shared_resource_present "custom role ${FIRESTORE_ROLE}" \
  gcloud iam roles describe "${FIRESTORE_ROLE_ID}" \
  --project "${PROJECT_ID}" --format='value(name)' || FS_ROLE_RC=$?
case "${FS_ROLE_RC}" in
  0) ;;
  1)
    die "custom role ${FIRESTORE_ROLE} does not exist. Run \`make infra\` first:
  terraform/modules/tenancy creates it, and granting roles/datastore.user instead
  would hand this tenant entities.delete and entities.list over every other
  tenant's control-plane documents."
    ;;
  *)
    die "stopping: this tenant's Firestore grant needs ${FIRESTORE_ROLE##*/}, and whether it
  is there could not be established (gcloud's answer is above). That is a failure
  to LOOK -- fix the session or the caller's iam.roles.get before anything else;
  \`make infra\` will not help. Falling back to roles/datastore.user is not an
  option: it hands entities.delete and entities.list over every tenant."
    ;;
esac

# `--condition None` is how gcloud spells "an unconditional binding", and it is
# not the same as omitting the flag: when the policy already holds a CONDITIONAL
# binding for this member and role -- which every tenant registered before this
# fix has -- gcloud asks which binding is meant, and under `--quiet` that failure
# is the whole command, not a prompt.
run gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${GSA_EMAIL}" \
  --role "${FIRESTORE_ROLE}" \
  --condition None \
  --quiet >/dev/null
ok "${FIRESTORE_ROLE##*/} (no deletes, no queries; unconditioned -- see docs/security.md)"
dim "  a condition here would DENY the data plane, not scope it; terraform adds none either"

# IAM bindings are additive, so the unconditional binding above is enough to make
# the worker work again -- but a tenant registered before this fix still carries
# the old conditional binding, which grants nothing and reads like a boundary.
# Say so rather than leave it for someone to find in the policy.
if [[ "${DRY_RUN}" -eq 0 ]]; then
  # Read the condition's ACTUAL title, expression and description off the policy
  # rather than assuming them. `remove-iam-policy-binding --condition` matches
  # EXACTLY, and these bindings are written by more than one thing: this script
  # once used title `swarm_db_only`, terraform/modules/tenancy writes
  # `swarm-database-only`, and terraform/modules/iam writes
  # `swarm-database-only-admin-ops`. A hardcoded title produces a command that
  # silently removes nothing for every tenant terraform provisioned, which is
  # the majority of them.
  #
  # Redirected to a file rather than `2>/dev/null`, and the exit status read.
  # An empty answer here means "no stale binding" and is printed as silence, so
  # discarding the failure made a policy read that never happened -- an expired
  # session, a missing resourcemanager.projects.getIamPolicy -- indistinguishable
  # from a clean tenant. That is the one reading this block must not produce:
  # the advisory exists precisely for tenants provisioned before the fix, and
  # they are the ones a failed read would silently clear.
  STALE_ERR="$(mktemp "${TMPDIR:-/tmp}/swarm-stale-binding-err.XXXXXX")"
  STALE=""
  STALE_READ=0
  if STALE_RAW="$(gcloud projects get-iam-policy "${PROJECT_ID}" \
    --flatten='bindings[].members' \
    --filter="bindings.role=${FIRESTORE_ROLE} AND bindings.members:${GSA_EMAIL} AND bindings.condition.expression:databases" \
    --format='csv[no-heading,separator="|"](bindings.condition.title,bindings.condition.expression,bindings.condition.description)' \
    2>"${STALE_ERR}")"; then
    STALE_READ=1
    STALE="$(printf '%s\n' "${STALE_RAW}" | head -1)"
  else
    warn "could NOT read the project IAM policy, so whether ${GSA_ID} still carries"
    warn "a stale conditional ${FIRESTORE_ROLE##*/} binding is UNKNOWN -- not 'no'."
    redact <"${STALE_ERR}" | head -n 3 | sed 's/^/     /' >&2
  fi
  rm -f "${STALE_ERR}"
  if [[ "${STALE_READ}" -eq 1 && -n "${STALE}" ]]; then
    STALE_TITLE="${STALE%%|*}"
    STALE_REST="${STALE#*|}"
    STALE_EXPR="${STALE_REST%%|*}"
    STALE_DESC="${STALE_REST#*|}"
    warn "${GSA_ID} still carries a CONDITIONAL binding for ${FIRESTORE_ROLE##*/} (title: ${STALE_TITLE})."
    warn "It grants nothing -- Firestore ignores IAM conditions on the data plane -- and should be removed:"
    dim  "  gcloud projects remove-iam-policy-binding ${PROJECT_ID} \\"
    dim  "    --member serviceAccount:${GSA_EMAIL} --role ${FIRESTORE_ROLE} \\"
    dim  "    --condition \"expression=${STALE_EXPR},title=${STALE_TITLE},description=${STALE_DESC}\""
  fi
fi

# GCS, conditioned on this tenant's own prefix. This is the boundary that stops
# a compromised worker from reading another tenant's artifacts by guessing a path.
#
# Three details are copied from terraform/modules/tenancy rather than improvised,
# because this script and that module must grant IDENTICAL authority -- a tenant
# onboarded here must not end up more privileged than one terraform created:
#
#   * TWO bindings, terraform/modules/tenancy's `worker_objects_read` and
#     `worker_objects_write` (#295; docs/merge-step.md §4.3, §10 item 5):
#     roles/storage.objectViewer on tenants/<t>/, and roles/storage.objectUser
#     on tenants/<t>/ EXCEPT tenants/<t>/verdicts/, which was to be the
#     retired #295 review account's alone; terraform keeps the pair as it is
#     (modules/tenancy says why), so this does too. GCS IAM is allow-only, so
#     the exclusion is the write binding's condition, not a deny rule. Never objectAdmin, which adds
#     storage.objects.setIamPolicy: a compromised worker could share its own
#     objects with anyone. Nothing in the worker path sets an object policy.
#   * the READ condition has TWO clauses. The first covers get on an object
#     path. The second covers LIST, which carries no object name at all -- the
#     only attribute that can scope a list request is objectListPrefix, and
#     without it the worker can enumerate every tenant's object names. The
#     write binding has no list clause: listing is the read binding's.
#   * a separate custom role for storage.buckets.get. Cloud Storage FUSE and the
#     client libraries both need the bucket's own metadata, and a prefix
#     condition can never match the bucket resource name. legacyBucketReader
#     would hand over objects.list across the whole bucket instead.
BUCKET_METADATA_ROLE_ID="swarmBucketMetadataReader${ROLE_ID_SUFFIX}"
# Tri-state, for the reason given at the Firestore role above: a denied
# storage.buckets.get used to print "does not exist yet; run 'make infra'".
BUCKET_RC=0
shared_resource_present "artifact bucket gs://${ARTIFACT_BUCKET}" \
  gcloud storage buckets describe "gs://${ARTIFACT_BUCKET}" \
  --project "${PROJECT_ID}" --format='value(name)' || BUCKET_RC=$?
if [[ "${BUCKET_RC}" -eq 0 ]]; then
  # --condition-from-file, NOT --condition. gcloud parses --condition as
  # comma-separated key=value pairs, and the read expression contains a comma
  # inside api.getAttribute(..., "") -- so gcloud split it mid-expression and
  # refused the fragment as an unknown key. Written as JSON by jq, so no
  # quote in an expression or a description needs escaping by hand.
  #
  # THE CONDITIONS ARE TERRAFORM'S, CHARACTER FOR CHARACTER: the title,
  # description and expression of worker_objects_read and worker_objects_write
  # in terraform/modules/tenancy/main.tf, built from its `object_prefix`,
  # `list_prefix` and `verdicts_prefix` locals.
  # tests/unit/scripts/test_register_tenant_merge_step.py renders those locals
  # and holds this to them, because the check below recognises the split by
  # its expressions: a tenant terraform applied must read as already split
  # here, not be granted a near-duplicate beside it.
  BUCKET_WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bucket.XXXXXX")"
  BUCKET_POLICY_FILE="${BUCKET_WORK_DIR}/policy.json"
  : >"${BUCKET_POLICY_FILE}"
  trap 'rm -rf "${BUCKET_WORK_DIR}"' EXIT INT TERM
  OBJECT_PREFIX_EXPR="resource.name.startsWith(\"projects/_/buckets/${ARTIFACT_BUCKET}/objects/${GCS_PREFIX}/\")"
  LIST_PREFIX_EXPR="api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"${GCS_PREFIX}/\")"
  VERDICTS_PREFIX_EXPR="resource.name.startsWith(\"projects/_/buckets/${ARTIFACT_BUCKET}/objects/${GCS_PREFIX}/verdicts/\")"
  READ_EXPR="${OBJECT_PREFIX_EXPR} || ${LIST_PREFIX_EXPR}"
  WRITE_EXPR="${OBJECT_PREFIX_EXPR} && !${VERDICTS_PREFIX_EXPR}"
  READ_CONDITION_FILE="${BUCKET_WORK_DIR}/read-condition.json"
  WRITE_CONDITION_FILE="${BUCKET_WORK_DIR}/write-condition.json"
  jq -n --arg t "swarm-tenant-prefix-read-${TENANT_ID}" \
        --arg d "Read and list objects under ${GCS_PREFIX}/ only." \
        --arg e "${READ_EXPR}" '{title:$t, description:$d, expression:$e}' >"${READ_CONDITION_FILE}"
  jq -n --arg t "swarm-tenant-prefix-write-${TENANT_ID}" \
        --arg d "Write objects under ${GCS_PREFIX}/, except ${GCS_PREFIX}/verdicts/." \
        --arg e "${WRITE_EXPR}" '{title:$t, description:$d, expression:$e}' >"${WRITE_CONDITION_FILE}"
  # Shown, not hidden. The condition IS the isolation -- it is the only thing
  # standing between this tenant's service account and every other tenant's
  # artifacts -- so an operator applying it should see what they are applying,
  # and a --dry-run that does not show it is not a rehearsal of anything.
  dim "  read condition:  objects/${GCS_PREFIX}/ and listing prefix ${GCS_PREFIX}/"
  dim "  write condition: objects/${GCS_PREFIX}/ except objects/${GCS_PREFIX}/verdicts/"
  # Read the bucket policy ONCE, here, and answer both "already granted?"
  # questions below from that one document. Two reads are two chances to decide
  # two grants against two different policies, and this one is long -- it is the
  # shared artifact bucket, so it holds every tenant's bindings.
  #
  # Redirected to a file rather than captured: `X="$(gcloud ...)"` runs the
  # command in a subshell, and this policy is also the input to a predicate that
  # must be able to say "I could not read it" separately from "it is not there".
  # An empty file is that first answer, and it makes both checks below fall
  # through to the add -- which either succeeds or fails out loud. The one
  # outcome this must never produce is a skipped grant reported as a present one.
  #
  # Its stderr used to go to /dev/null, so falling through was silent: the
  # operator saw two grants attempted and no word that the "already granted?"
  # check never happened, or why. When the add then failed -- a policy gcloud
  # cannot render at version 1 is refused with "Specified policy version (1)
  # must be at least 3" -- that error was the only one on screen, and it is not
  # the cause. The reason is shown now, before the grants.
  BUCKET_POLICY_ERR="${BUCKET_WORK_DIR}/policy.err"
  if ! gcloud storage buckets get-iam-policy "gs://${ARTIFACT_BUCKET}" \
       --project "${PROJECT_ID}" --format=json >"${BUCKET_POLICY_FILE}" 2>"${BUCKET_POLICY_ERR}"; then
    : >"${BUCKET_POLICY_FILE}"
    policy_err="$(cat "${BUCKET_POLICY_ERR}")"
    rm -f "${BUCKET_POLICY_ERR}"
    die_if_auth_failure "${policy_err}"
    warn "could NOT read the IAM policy on gs://${ARTIFACT_BUCKET}, so whether this tenant"
    warn "already holds its grants there is UNKNOWN -- attempting them; each lands or fails loudly."
    warn "Whether it still carries the OLD single objectUser binding is unknown too: re-run once the"
    warn "policy can be read, or that binding keeps its worker able to write under ${GCS_PREFIX}/verdicts/:"
    printf '%s\n' "${policy_err}" | redact | head -n 3 | sed 's/^/     /' >&2
  fi
  rm -f "${BUCKET_POLICY_ERR}"
  # Skip a binding that is already there. terraform/modules/tenancy grants
  # these same conditioned bindings for every tenant it manages, and adding one
  # twice is not merely redundant: gcloud reads the bucket policy at version 1,
  # the existing conditions make it version 3, and the write is refused with
  # "Specified policy version (1) must be at least 3" -- which aborts this
  # script before it reaches the tenant document, the thing it is actually
  # needed for.
  #
  # Role, member AND expression. This bucket's policy names every tenant, so
  # "is this member in the policy at all" is a different question; and since
  # the split, so is "does this member hold objectUser": the OLD single
  # binding holds objectUser too. Treating any objectUser as done is exactly
  # the check that would leave every tenant registered before #295 able to
  # write a verdict forever (merge-step.md §4.3, MAJOR 1c).
  bucket_binding_present() {
    local role="$1" expression="$2"
    [[ -s "${BUCKET_POLICY_FILE}" ]] || return 1
    jq -e --arg r "${role}" --arg m "serviceAccount:${GSA_EMAIL}" --arg e "${expression}" \
      'any((.bindings? // [])[]; .role == $r and any(.members[]?; . == $m)
           and (.condition.expression? // "") == $e)' \
      "${BUCKET_POLICY_FILE}" >/dev/null 2>&1
  }
  SPLIT_DONE=1
  if bucket_binding_present roles/storage.objectViewer "${READ_EXPR}"; then
    ok "storage.objectViewer already granted (read and list under gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/)"
  else
    SPLIT_DONE=0
    run gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
      --member "serviceAccount:${GSA_EMAIL}" \
      --role roles/storage.objectViewer \
      --condition-from-file "${READ_CONDITION_FILE}" \
      >/dev/null
    ok "storage.objectViewer (only gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/, listing included)"
  fi
  if bucket_binding_present roles/storage.objectUser "${WRITE_EXPR}"; then
    ok "storage.objectUser already granted (write under gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/, except verdicts/)"
  else
    SPLIT_DONE=0
    run gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
      --member "serviceAccount:${GSA_EMAIL}" \
      --role roles/storage.objectUser \
      --condition-from-file "${WRITE_CONDITION_FILE}" \
      >/dev/null
    ok "storage.objectUser (only gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/, except ${GCS_PREFIX}/verdicts/)"
  fi

  # THE OLD BINDING, REPLACED: any other objectUser binding this worker holds
  # on the bucket -- the pre-#295 single grant (`tenant_prefix_only`, or
  # terraform's retired `worker_objects`), or one with no condition at all.
  # Each overlaps the write binding without its verdicts/ exclusion, so while
  # it stands the split excludes nothing. Removed only AFTER both new bindings
  # exist (above; a failed add has already stopped the run under set -e), so
  # the worker is never without read or write for a moment, and only with a
  # typed confirmation: it is a removal from a policy every tenant shares.
  # `remove-iam-policy-binding` matches the (member, role, condition) triple
  # exactly, so each is removed with its condition as read off the policy.
  OLD_BINDINGS_FILE="${BUCKET_WORK_DIR}/old-bindings.jsonl"
  : >"${OLD_BINDINGS_FILE}"
  if [[ -s "${BUCKET_POLICY_FILE}" ]]; then
    jq -c --arg m "serviceAccount:${GSA_EMAIL}" --arg e "${WRITE_EXPR}" \
      '(.bindings? // [])[]
       | select(.role == "roles/storage.objectUser" and any(.members[]?; . == $m)
                and (.condition.expression? // null) != $e)
       | (.condition // null)' \
      "${BUCKET_POLICY_FILE}" >"${OLD_BINDINGS_FILE}" 2>/dev/null || : >"${OLD_BINDINGS_FILE}"
  fi
  OLD_COUNT="$(grep -c . "${OLD_BINDINGS_FILE}" || true)"
  if [[ "${OLD_COUNT}" -gt 0 ]]; then
    warn "${GSA_ID} still holds ${OLD_COUNT} storage.objectUser binding(s) WITHOUT the verdicts/ exclusion:"
    while IFS= read -r old; do
      if [[ "${old}" == "null" ]]; then
        warn "  (no condition: write anywhere in gs://${ARTIFACT_BUCKET})"
      else
        warn "  title: $(jq -r '.title // ""' <<<"${old}")"
        dim  "    expression: $(jq -r '.expression // ""' <<<"${old}")"
      fi
    done <"${OLD_BINDINGS_FILE}"
    warn "while it stands, this tenant's worker can write under ${GCS_PREFIX}/verdicts/, the prefix"
    warn "terraform/modules/tenancy keeps out of every worker's write grant (docs/merge-step.md §4.3)"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      dim "  would remove it once the two bindings above exist, after a typed confirmation:"
    else
      # SWARM_ASSUME_YES never answers this (CLAUDE.md: destructive steps take
      # a typed confirmation), and confirm() refuses without a terminal; this
      # says what is in place when it does.
      [[ -t 0 ]] || die "not removing the old binding without an interactive, typed confirmation (no terminal).
  The read and write-except-verdicts bindings above are in place; the old one is unchanged and
  still lets ${GSA_ID} write under ${GCS_PREFIX}/verdicts/. Run this again from a terminal."
      SWARM_ASSUME_YES="" confirm "Remove the old storage.objectUser binding(s) of ${GSA_EMAIL} from gs://${ARTIFACT_BUCKET}?" "${TENANT_ID}"
    fi
    old_index=0
    while IFS= read -r old; do
      old_index=$((old_index + 1))
      if [[ "${old}" == "null" ]]; then
        run gcloud storage buckets remove-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
          --member "serviceAccount:${GSA_EMAIL}" \
          --role roles/storage.objectUser \
          --condition None >/dev/null </dev/null
      else
        old_file="${BUCKET_WORK_DIR}/old-condition-${old_index}.json"
        jq '{title, description, expression} | with_entries(select(.value != null))' <<<"${old}" >"${old_file}"
        run gcloud storage buckets remove-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
          --member "serviceAccount:${GSA_EMAIL}" \
          --role roles/storage.objectUser \
          --condition-from-file "${old_file}" >/dev/null </dev/null
      fi
    done <"${OLD_BINDINGS_FILE}"
    if [[ "${DRY_RUN}" -eq 0 ]]; then
      ok "removed the old storage.objectUser binding(s); ${GSA_ID} can no longer write under ${GCS_PREFIX}/verdicts/"
    fi
  elif [[ "${SPLIT_DONE}" -eq 1 ]]; then
    ok "storage access already split (read + write-except-verdicts, as terraform/modules/tenancy grants it); nothing changed"
  fi

  # Tri-state, as at the Firestore role: a denied iam.roles.get used to print
  # "does not exist yet; run 'make infra'" below.
  META_ROLE_RC=0
  shared_resource_present "custom role ${BUCKET_METADATA_ROLE_ID}" \
    gcloud iam roles describe "${BUCKET_METADATA_ROLE_ID}" \
    --project "${PROJECT_ID}" --format='value(name)' || META_ROLE_RC=$?
  if [[ "${META_ROLE_RC}" -eq 0 ]]; then
    # This role is bound on ONE bucket for EVERY tenant
    # (terraform/modules/tenancy/main.tf, `for_each = var.tenants`), so asking
    # whether the policy mentions the role id at all is answered by the first
    # tenant ever bound and never becomes false again. A tenant this script
    # creates -- one that is not in var.tenants -- matched another tenant's
    # binding here and was told the grant existed. It did not, and without
    # storage.buckets.get its worker cannot mount the bucket at all.
    if iam_policy_binds_member "${BUCKET_POLICY_FILE}" \
         "projects/${PROJECT_ID}/roles/${BUCKET_METADATA_ROLE_ID}" \
         "serviceAccount:${GSA_EMAIL}"; then
      ok "${BUCKET_METADATA_ROLE_ID} already granted"
    else
      # `--condition None` for the same reason as the project bindings: this
      # policy contains conditions, so gcloud refuses an unconditioned addition
      # unless told that is deliberate.
      run gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
        --member "serviceAccount:${GSA_EMAIL}" \
        --role "projects/${PROJECT_ID}/roles/${BUCKET_METADATA_ROLE_ID}" \
        --condition None >/dev/null
      ok "${BUCKET_METADATA_ROLE_ID} (storage.buckets.get only -- enough to mount, not to enumerate)"
    fi
  elif [[ "${META_ROLE_RC}" -eq 1 ]]; then
    warn "custom role ${BUCKET_METADATA_ROLE_ID} does not exist yet; run 'make infra'"
    warn "without it the worker can reach its objects but cannot read the bucket's own metadata,"
    warn "which Cloud Storage FUSE needs in order to mount"
  else
    warn "the ${BUCKET_METADATA_ROLE_ID} grant was NOT made: whether the role is there could not"
    warn "be established (gcloud's answer is above). That is a failure to look, not a missing role --"
    warn "fix the session or iam.roles.get and re-run; until then this tenant's worker cannot mount the bucket"
  fi

  if [[ "${DRY_RUN}" -eq 0 ]]; then
    # stderr kept: "could not write" alone does not say whether it was a
    # permission, a session or the bucket.
    MARKER_ERR="$(mktemp "${TMPDIR:-/tmp}/swarm-prefix-marker.XXXXXX")"
    if ! printf 'tenant %s registered %s\n' "${TENANT_ID}" "$(iso_now)" \
      | gcloud storage cp - "gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/.tenant" \
        --project "${PROJECT_ID}" >/dev/null 2>"${MARKER_ERR}"; then
      warn "could not write the prefix marker object:"
      redact <"${MARKER_ERR}" | head -n 3 | sed 's/^/     /' >&2
    fi
    rm -f "${MARKER_ERR}"
  fi
elif [[ "${BUCKET_RC}" -eq 1 ]]; then
  warn "artifact bucket gs://${ARTIFACT_BUCKET} does not exist yet; run 'make infra' then re-run this"
else
  warn "this tenant's storage grants were NOT made: whether gs://${ARTIFACT_BUCKET} exists could not"
  warn "be established (gcloud's answer is above). That is a failure to look -- fix it and re-run;"
  warn "\`make infra\` will not help"
fi

# Logging and metrics cannot be conditioned per tenant; they are write-only and
# carry no cross-tenant read capability.
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  # `--condition None` is required, not optional. Once ANY binding in the
  # project policy carries a condition -- and several here do, deliberately --
  # gcloud refuses to add an unconditioned one without being told that is what
  # is meant, and under --quiet that refusal aborts the whole registration
  # partway. Which it did: the service account and the Firestore role were
  # created, the telemetry roles were not, and the tenant document was never
  # updated.
  run gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${GSA_EMAIL}" --role "${role}" \
    --condition None --quiet >/dev/null
  ok "${role}"
done

# --- 4. secrets ---------------------------------------------------------------
#
# EVERY SECRET IS CHECKED BEFORE ANY IS GRANTED, and granted only if its labels
# say it is this tenant's, for that provider (tenant_secret_state, above
# section A). This used to grant on the name alone, so `--providers team-git`
# for tenant eng bound eng's worker to tenant eng-team's git key, and
# `--providers anthropic-refresh` bound it to the refresh half only the quota
# broker may read. It also read ANY failed describe -- a denied or expired
# lookup included -- as "does not exist yet", and carried on.
#
# A missing secret, or one with no enabled version yet, is still a warning
# here and not a refusal, as it always was for the full registration: the
# grant on an empty secret is inert, and storing the key afterwards is the
# documented order.
if [[ "${#PROVIDERS[@]}" -gt 0 ]]; then
  step "Provider credentials"
  GRANTABLE=()
  for provider in "${PROVIDERS[@]}"; do
    secret="swarm-tenant-${TENANT_ID}-${provider}"
    SECRET_RC=0
    tenant_secret_state "${secret}" "${provider}" || SECRET_RC=$?
    case "${SECRET_RC}" in
      0) GRANTABLE+=("${provider}") ;;
      4)
        GRANTABLE+=("${provider}")
        warn "${secret} has no enabled version yet, so there is nothing in it to read"
        dim "  store one with: scripts/create-secrets.sh --tenant ${TENANT_ID} --provider ${provider} --stdin"
        ;;
      1)
        warn "${secret} does not exist yet"
        dim "  create it with: scripts/create-secrets.sh --tenant ${TENANT_ID} --provider ${provider} --stdin"
        ;;
      3) refuse_foreign_secret "${secret}" "${provider}" ;;
      *)
        die "stopping: whether ${secret} exists, and whose it is, could not be established
  (gcloud's answer is above). That is a failure to LOOK, not a missing secret. No secret
  was granted; fix the session or the permission and run this again."
        ;;
    esac
  done
  # swarm-api's address is settled before any secret is granted, so a module
  # it cannot be read off stops the run with no secret changed.
  for provider in ${GRANTABLE[@]+"${GRANTABLE[@]}"}; do
    [[ "${provider}" != "${FORGE_PROVIDER}" ]] || resolve_forge_reader
  done
  for provider in ${GRANTABLE[@]+"${GRANTABLE[@]}"}; do
    secret="swarm-tenant-${TENANT_ID}-${provider}"
    run gcloud secrets add-iam-policy-binding "${secret}" \
      --project "${PROJECT_ID}" \
      --member "serviceAccount:${GSA_EMAIL}" \
      --role roles/secretmanager.secretAccessor --quiet >/dev/null
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      dim "  would let ${GSA_ID} read ${secret}"
    else
      ok "${secret}: ${GSA_ID} may read it"
    fi
    # The issue preview's grant, on -git alone (resolve_forge_reader, above).
    [[ "${provider}" == "${FORGE_PROVIDER}" ]] || continue
    if forge_reader_bound "${secret}"; then
      ok "${secret}: ${FORGE_READER_ID} already reads it (the issue preview)"
      continue
    fi
    run gcloud secrets add-iam-policy-binding "${secret}" \
      --project "${PROJECT_ID}" \
      --member "serviceAccount:${FORGE_READER_EMAIL}" \
      --role roles/secretmanager.secretAccessor --quiet >/dev/null
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      dim "  would let ${FORGE_READER_ID} read ${secret}"
    else
      ok "${secret}: ${FORGE_READER_ID} may read it (the issue preview)"
    fi
  done
fi

# --- 5. Kubernetes ------------------------------------------------------------
#
# The whole namespace, applied by kubernetes/apply.sh, not assembled here.
#
# This used to create the namespace and a service account with two kubectl calls
# and stop there. A namespace with no NetworkPolicy runs under Kubernetes'
# DEFAULT allow-all pod networking, so tenant A's browser agent could open a
# socket straight to tenant B's running agent -- traffic that never touches a
# Google API, so no amount of IAM scoping sees it. It also had no ResourceQuota
# (one tenant could consume the cluster) and no pod-security.kubernetes.io/enforce
# label, so the cluster default applied instead of `baseline`.
#
# Those objects exist in kubernetes/ and are rendered by kubernetes/render.py,
# which validates every interpolated value before it reaches a manifest. Calling
# the renderer is the only way to get them all, in the right order, with the
# tenant id checked -- and it is what keeps this script from being a second,
# weaker definition of what a tenant namespace is.
if [[ "${SKIP_K8S}" -eq 0 ]]; then
  step "Kubernetes namespace (GKE path)"
  APPLY_SH="${REPO_ROOT}/kubernetes/apply.sh"
  KUBECTL_BIN="$(kubectl_bin 2>/dev/null || true)"
  # Never create objects through whatever context happens to be current: this
  # kubeconfig can reach other teams' clusters, production included. apply.sh
  # refuses on its own too; checking here keeps "not connected" a skip rather
  # than a failure, because the Cloud Run path does not need the cluster.
  # THREE DIFFERENT FAILURES, which used to be one: no usable kubectl, a context
  # that is not the swarm's, and a swarm context whose API server did not
  # answer. The last was `get --raw=/readyz >/dev/null 2>&1`, so an allowlist
  # miss or a dropped connection printed "not connected to the swarm cluster"
  # and sent the operator to configure-kubectl.sh -- which rewrites a
  # kubeconfig that was already right, and then this prints the same thing
  # again. status.sh separates the same three the same way.
  READYZ_ERR=""
  K8S_REACHABLE=0
  if [[ -n "${KUBECTL_BIN}" ]] && kube_context_is_swarm; then
    READYZ_ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/swarm-register-readyz.XXXXXX")"
    if "${KUBECTL_BIN}" get --raw='/readyz' >/dev/null 2>"${READYZ_ERR_FILE}"; then
      K8S_REACHABLE=1
    else
      # sed, not head: under pipefail a head that closes early can SIGPIPE the
      # writer, and a failing assignment here would end the script under set -e.
      READYZ_ERR="$(redact <"${READYZ_ERR_FILE}" | sed -n '1,3p')"
      # A readyz that failed with nothing on stderr still failed; say so
      # rather than printing an empty reason.
      [[ -n "${READYZ_ERR}" ]] || READYZ_ERR="kubectl exited non-zero and printed nothing"
    fi
    rm -f "${READYZ_ERR_FILE}"
  fi
  if [[ ! -f "${APPLY_SH}" ]]; then
    warn "kubernetes/apply.sh is missing; cannot create the tenant namespace"
  elif [[ "${K8S_REACHABLE}" -eq 1 ]]; then
    # Workload Identity is a GCP-side binding, so it stays here -- and it comes
    # BEFORE apply.sh, because apply.sh now reads these bindings to decide what
    # to render: the older `swarm-worker` account is rendered only where the
    # GSA binds it (kubernetes/render.py LEGACY_KSA_NAME). Bound after, a
    # tenant's first registration rendered its namespace without the account
    # this loop then bound. A binding names a (namespace, KSA) pair, not an
    # object, so it can precede both; and no tenant document exists until
    # section 6, so nothing dispatches into the namespace if apply.sh fails.
    # `swarm-agent-worker` is the one apps/scheduler/scheduler/dispatch.py
    # names in its pod spec; a pod naming an unbound KSA authenticates as
    # nothing at all. (In --dry-run `run` binds nothing, so the preview below
    # renders a brand-new tenant without `swarm-worker`.)
    for ksa in "${KSAS[@]}"; do
      run gcloud iam service-accounts add-iam-policy-binding "${GSA_EMAIL}" \
        --project "${PROJECT_ID}" \
        --role roles/iam.workloadIdentityUser \
        --member "serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa}]" \
        --quiet >/dev/null
      ok "workload identity: ${NAMESPACE}/${ksa} -> ${GSA_ID}"
    done

    # THE CLUSTER'S NETWORK comes from the cluster, through apply.sh. The
    # tenant's egress policy needs the pod range, the service range, the
    # kube-dns Service IP and the NodeLocal DNSCache address; apply.sh reads
    # all four (describe for the ranges, kube-system for the two DNS
    # addresses -- kubernetes/cluster-network.sh names each source) and
    # REFUSES them as arguments, so this script passes none. Restating the
    # read here would be a second copy of it, and a second copy is the defect
    # this fixes: until 2026-09-24 nothing on this path passed the network at
    # all, the policy was rendered from renderer defaults (10.0.0.0/8,
    # 34.118.224.0/20) and allowed DNS only to kube-dns pods this cluster's
    # DNS never uses, and a browser worker ran 390 s resolving nothing.
    #
    # What this script does pass is WHICH cluster: the context it just
    # checked (kube_context_is_swarm, /readyz) and the cluster name, so the
    # network apply.sh reads is the one of the cluster this tenant is being
    # registered on -- not whatever the current context becomes in between.
    APPLY_ARGS=(--tenant "${TENANT_ID}" --gsa "${GSA_EMAIL}"
      --context "$(kube_current_context)" --cluster "${GKE_CLUSTER}")
    [[ "${DRY_RUN}" -eq 1 ]] || APPLY_ARGS+=(--confirm)
    if "${APPLY_SH}" "${APPLY_ARGS[@]}"; then
      ok "namespace ${NAMESPACE}: quota, limits, RBAC, PSA labels and default-deny networking applied"
    else
      die "kubernetes/apply.sh failed; the tenant namespace is not isolated. Refusing to
  report this tenant as registered -- a namespace without its NetworkPolicy is
  reachable from every other tenant's pods."
    fi
  elif [[ -n "${READYZ_ERR}" ]]; then
    warn "context $(kube_current_context || echo none) IS the swarm cluster, but its API server did not answer /readyz:"
    printf '%s\n' "${READYZ_ERR}" | sed 's/^/     /' >&2
    dim "  most likely the master authorized-networks allowlist (your IP changed) or a dropped connection --"
    dim "  NOT a wrong context, so scripts/configure-kubectl.sh will not fix it. Re-run once the API server answers."
    warn "until then this tenant has NO GKE namespace: browser-class runners cannot be dispatched for it"
  else
    info "not connected to the swarm cluster (context: $(kube_current_context || echo none)); skipping the GKE namespace"
    dim "  run scripts/configure-kubectl.sh and re-run, or pass --skip-k8s if this tenant never needs browser/GPU runners"
    warn "until then this tenant has NO GKE namespace: browser-class runners cannot be dispatched for it"
  fi
fi

# --- 6. control-plane documents ----------------------------------------------
step "Control plane"
# require_fs_database is not used here: the cloud resources above were already
# created, so the operator needs to be told that BEFORE the exit code, and both
# non-zero cases end the same way.
FS_RC=0
fs_database_exists || FS_RC=$?
if [[ "${FS_RC}" -ne 0 ]]; then
  if [[ "${FS_RC}" -eq 1 ]]; then
    warn "Firestore database '${FIRESTORE_DATABASE}' does not exist; run 'make infra' first"
  else
    warn "could not confirm Firestore database '${FIRESTORE_DATABASE}' exists (reason above)"
    warn "that is a failure to look, not proof it is absent -- do not run 'make infra' on this alone"
  fi
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
  dim "  would write pools/${POOL} with hard_limit=${POOL_HARD_LIMIT}"
else
  POOL_EXISTING="$(fs_get "pools/${POOL}" | jq -c "${FS_JQ} if .fields then doc else null end")"
  if [[ "${POOL_EXISTING}" == "null" || -z "${POOL_EXISTING}" ]]; then
    # `active` starts at 0 and is only ever changed inside the admission and
    # release transactions -- never here, and never by any other background job.
    fs_patch "pools/${POOL}" "name,hard_limit,active,enabled,updated_at" \
      "$(jq -nc --arg n "${POOL}" --argjson l "${POOL_HARD_LIMIT}" --arg t "$(iso_now)" \
        '{name:{stringValue:$n},hard_limit:{integerValue:($l|tostring)},
          active:{integerValue:"0"},enabled:{booleanValue:true},updated_at:{timestampValue:$t}}')"
    ok "created pools/${POOL} (hard_limit ${POOL_HARD_LIMIT} units)"
  else
    CURRENT_ACTIVE="$(jq -r '.active // 0' <<<"${POOL_EXISTING}")"
    fs_patch "pools/${POOL}" "hard_limit,updated_at" \
      "$(jq -nc --argjson l "${POOL_HARD_LIMIT}" --arg t "$(iso_now)" \
        '{hard_limit:{integerValue:($l|tostring)},updated_at:{timestampValue:$t}}')"
    ok "updated pools/${POOL} hard_limit to ${POOL_HARD_LIMIT} (active ${CURRENT_ACTIVE} left untouched)"
  fi
fi

hr
ok "tenant ${TENANT_ID} registered"
# "then add it", NOT "then re-run this". This line used to print
# `--${KIND} ${PRINCIPAL} --providers anthropic`: a full re-run, whose
# --providers REPLACES `credentials` and whose defaults reset the limits, so
# following it dropped every other provider the tenant had. --add-provider is
# section A; tests/integration/test_register_tenant_add_provider.py runs this
# line as printed and reads what it wrote.
cat >&2 <<EOF

Next:
  terraform-managed?    add '${TENANT_ID}' to terraform/environments/dev/dev.tfvars on a branch; BEFORE merging,
                        the owner applies terraform/bootstrap FROM MAIN with that file copied out by git show:
                        -var infra_tenants_tfvars=<copy> -target='${BOOTSTRAP_TARGET}'
                        (the release 403s on this account's IAM without it -- docs/ci.md has the plan checks)
  store a provider key  scripts/create-secrets.sh --tenant ${TENANT_ID} --provider anthropic --stdin
  then add it           scripts/register-tenant.sh --tenant ${TENANT_ID} --add-provider anthropic
  check it              scripts/status.sh --tenant ${TENANT_ID}

Members of ${PRINCIPAL} now submit tasks with their own Google identity; the API
resolves them to tenant '${TENANT_ID}'. No shared token exists, by design.
EOF
