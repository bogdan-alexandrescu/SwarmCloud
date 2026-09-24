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
    -h|--help)         sed -n '2,44p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq curl python3

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
else
  die "give either --group <group@saga.xyz> or --user <person@saga.xyz>"
fi

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

case "${TENANT_ID}" in
  *[!a-z0-9-]*) die "derived tenant id '${TENANT_ID}' is not a valid slug" ;;
esac

DOMAIN="${PRINCIPAL##*@}"
ALLOWED_DOMAINS="${ALLOWED_DOMAINS:-saga.xyz}"
case ",${ALLOWED_DOMAINS}," in
  *",${DOMAIN},"*) ;;
  *) die "${PRINCIPAL} is outside the allowed hosted domain(s) (${ALLOWED_DOMAINS}); authentication would reject it anyway" ;;
esac

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
if [[ "${#TENANT_ID}" -gt "${MAX_TENANT_ID}" ]]; then
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
# Both KSAs that kubernetes/service-accounts/worker-serviceaccount.yaml creates,
# because a binding is per (namespace, KSA) pair and a pod naming an unbound one
# authenticates as nothing at all. `swarm-agent-worker` is the name the
# dispatcher's pod spec actually uses and the name terraform binds; `swarm-worker`
# is the older one every already-registered tenant carries. The `swarm-<tenant>`
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
FIRESTORE_ROLE="projects/${PROJECT_ID}/roles/swarmTenantWorkerFirestore${CUSTOM_ROLE_SUFFIX:+_${CUSTOM_ROLE_SUFFIX}}"

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
  gcloud iam roles describe "swarmTenantWorkerFirestore${CUSTOM_ROLE_SUFFIX:+_${CUSTOM_ROLE_SUFFIX}}" \
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
#   * roles/storage.objectUser, NOT roles/storage.objectAdmin. objectAdmin adds
#     storage.objects.setIamPolicy, which lets a compromised worker grant its own
#     objects to anyone. Nothing in the worker path sets an object policy.
#   * the condition has TWO clauses. The first covers get/create/delete on an
#     object path. The second covers LIST, which carries no object name at all --
#     the only attribute that can scope a list request is objectListPrefix, and
#     without it the worker can enumerate every tenant's object names.
#   * a separate custom role for storage.buckets.get. Cloud Storage FUSE and the
#     client libraries both need the bucket's own metadata, and a prefix
#     condition can never match the bucket resource name. legacyBucketReader
#     would hand over objects.list across the whole bucket instead.
BUCKET_METADATA_ROLE_ID="swarmBucketMetadataReader${CUSTOM_ROLE_SUFFIX:+_${CUSTOM_ROLE_SUFFIX}}"
# Tri-state, for the reason given at the Firestore role above: a denied
# storage.buckets.get used to print "does not exist yet; run 'make infra'".
BUCKET_RC=0
shared_resource_present "artifact bucket gs://${ARTIFACT_BUCKET}" \
  gcloud storage buckets describe "gs://${ARTIFACT_BUCKET}" \
  --project "${PROJECT_ID}" --format='value(name)' || BUCKET_RC=$?
if [[ "${BUCKET_RC}" -eq 0 ]]; then
  # --condition-from-file, NOT --condition. gcloud parses --condition as
  # comma-separated key=value pairs, and this expression contains a comma
  # inside api.getAttribute(..., '') -- so gcloud split it mid-expression and
  # refused the fragment as an unknown key. The description contained an
  # apostrophe as well. This binding has therefore never been applied by this
  # script for any tenant, which means those tenants got their GCS access from
  # terraform or not at all.
  CONDITION_FILE="$(mktemp "${TMPDIR:-/tmp}/swarm-condition.XXXXXX")"
  BUCKET_POLICY_FILE="$(mktemp "${TMPDIR:-/tmp}/swarm-bucket-policy.XXXXXX")"
  trap 'rm -f "${CONDITION_FILE}" "${BUCKET_POLICY_FILE}"' EXIT INT TERM
  cat >"${CONDITION_FILE}" <<CONDEOF
title: tenant_prefix_only
description: Only this tenant's object prefix, listing included
expression: resource.name.startsWith('projects/_/buckets/${ARTIFACT_BUCKET}/objects/${GCS_PREFIX}/') || api.getAttribute('storage.googleapis.com/objectListPrefix', '').startsWith('${GCS_PREFIX}/')
CONDEOF
  # Shown, not hidden. The condition IS the isolation -- it is the only thing
  # standing between this tenant's service account and every other tenant's
  # artifacts -- so an operator applying it should see what they are applying,
  # and a --dry-run that does not show it is not a rehearsal of anything.
  dim "  condition: objects/${GCS_PREFIX}/ and listing prefix ${GCS_PREFIX}/"
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
  BUCKET_POLICY_ERR="$(mktemp "${TMPDIR:-/tmp}/swarm-bucket-policy-err.XXXXXX")"
  if ! gcloud storage buckets get-iam-policy "gs://${ARTIFACT_BUCKET}" \
       --project "${PROJECT_ID}" --format=json >"${BUCKET_POLICY_FILE}" 2>"${BUCKET_POLICY_ERR}"; then
    : >"${BUCKET_POLICY_FILE}"
    policy_err="$(cat "${BUCKET_POLICY_ERR}")"
    rm -f "${BUCKET_POLICY_ERR}"
    die_if_auth_failure "${policy_err}"
    warn "could NOT read the IAM policy on gs://${ARTIFACT_BUCKET}, so whether this tenant"
    warn "already holds its two grants there is UNKNOWN -- attempting both; each lands or fails loudly:"
    printf '%s\n' "${policy_err}" | redact | head -n 3 | sed 's/^/     /' >&2
  fi
  rm -f "${BUCKET_POLICY_ERR}"
  # Skip if the binding is already there. terraform/modules/tenancy grants this
  # same conditioned binding for every tenant it manages, and adding it twice is
  # not merely redundant: gcloud reads the bucket policy at version 1, the
  # existing conditions make it version 3, and the write is refused with
  # "Specified policy version (1) must be at least 3" -- which aborts this
  # script before it reaches the tenant document, the thing it is actually
  # needed for.
  #
  # Role AND member, not either alone. This bucket's policy names every tenant,
  # so "is this member in the policy at all" is a different question: a tenant
  # holding only the metadata role below would have its object grant skipped and
  # its worker left unable to read the artifacts it writes.
  if iam_policy_binds_member "${BUCKET_POLICY_FILE}" \
       roles/storage.objectUser "serviceAccount:${GSA_EMAIL}"; then
    ok "storage access already granted (terraform manages this tenant's binding)"
    rm -f "${CONDITION_FILE}"
  else
    run gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
      --member "serviceAccount:${GSA_EMAIL}" \
      --role roles/storage.objectUser \
      --condition-from-file "${CONDITION_FILE}" \
      >/dev/null
    rm -f "${CONDITION_FILE}"
    ok "storage.objectUser (only gs://${ARTIFACT_BUCKET}/${GCS_PREFIX}/, listing included)"
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
    APPLY_ARGS=(--tenant "${TENANT_ID}" --gsa "${GSA_EMAIL}")
    [[ "${DRY_RUN}" -eq 1 ]] || APPLY_ARGS+=(--confirm)
    if "${APPLY_SH}" "${APPLY_ARGS[@]}"; then
      ok "namespace ${NAMESPACE}: quota, limits, RBAC, PSA labels and default-deny networking applied"
    else
      die "kubernetes/apply.sh failed; the tenant namespace is not isolated. Refusing to
  report this tenant as registered -- a namespace without its NetworkPolicy is
  reachable from every other tenant's pods."
    fi

    # Workload Identity is a GCP-side binding, so it stays here. Both service
    # account names that kubernetes/service-accounts/worker-serviceaccount.yaml
    # creates are bound -- `swarm-worker` and `swarm-agent-worker`, the one
    # apps/scheduler/scheduler/dispatch.py actually names in its pod spec. A pod
    # naming the unbound one would authenticate as nothing at all.
    for ksa in "${KSAS[@]}"; do
      run gcloud iam service-accounts add-iam-policy-binding "${GSA_EMAIL}" \
        --project "${PROJECT_ID}" \
        --role roles/iam.workloadIdentityUser \
        --member "serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa}]" \
        --quiet >/dev/null
      ok "workload identity: ${NAMESPACE}/${ksa} -> ${GSA_ID}"
    done
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
