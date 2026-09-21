#!/usr/bin/env bash
# Tear down the swarm's own infrastructure -- and nothing else.
#
# saga-agents-staging is a SHARED project. It holds a live GKE cluster
# (agents-staging), a VPC (agents-staging-vpc), three buckets and twelve service
# accounts belonging to promptlab, crawler, aipipeline, external-secrets and
# tournament-digest. A `terraform destroy` that touched any of those would take
# down other teams' production work.
#
# So this script never runs a bare destroy. It:
#
#   1. produces a destroy plan with `terraform plan -destroy -json`;
#   2. asserts EVERY resource marked for deletion carries managed-by=swarm-terraform,
#      or is of a type that physically cannot carry a label (IAM edges, API
#      enablements, Firestore indexes) -- unknown unlabelable types are treated
#      as offenders, i.e. it fails closed;
#   3. asserts no resource in the plan names anything on the shared deny-list;
#   4. aborts loudly, printing every offender, if either assertion fails;
#   5. refuses in prod without --allow-prod;
#   6. requires a typed confirmation -- SWARM_ASSUME_YES is explicitly ignored;
#   7. after applying, re-checks that every shared resource is still there --
#      and distinguishes "gcloud says it is gone" from "gcloud did not
#      answer", which are different emergencies.
#
# Usage:
#   scripts/destroy.sh --dry-run              # plan + assertions, change nothing
#   scripts/destroy.sh                        # destroy dev, keeping data stores
#   scripts/destroy.sh --include-data         # also delete Firestore and buckets
#   scripts/destroy.sh --environment prod --allow-prod
#   scripts/destroy.sh --self-test            # verify the guard still catches
#   scripts/destroy.sh --verify-shared        # re-check the neighbours only

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# Destroy is never non-interactive. If an automation sets this, it does not get
# to skip the confirmation for this script.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: destroy always requires a typed confirmation"
  unset SWARM_ASSUME_YES
fi

DRY_RUN=0
SELF_TEST=0
VERIFY_ONLY=0
ALLOW_PROD=0
INCLUDE_DATA=0
TARGETS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment|-e) ENVIRONMENT="$2"; export ENVIRONMENT; shift 2 ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --allow-prod)     ALLOW_PROD=1; shift ;;
    --include-data)   INCLUDE_DATA=1; shift ;;
    --target)         TARGETS+=("$2"); shift 2 ;;
    --self-test)      SELF_TEST=1; shift ;;
    --verify-shared)  VERIFY_ONLY=1; shift ;;
    -h|--help)        sed -n '2,31p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud jq
GUARD_JQ="${REPO_ROOT}/scripts/lib/destroy-guard.jq"
[[ -f "${GUARD_JQ}" ]] || die "missing ${GUARD_JQ}"

# Types that cannot carry a label in the google provider. Anything not on this
# list and not labelled managed-by=swarm-terraform is an offender. Adding to this
# list is a deliberate, reviewable act -- which is the point.
#
# The list lives in scripts/lib/unlabelable-types.json, not here, because the CI
# plan guards need exactly the same list and a second copy is how they drift. It
# already had: `google_firestore_document` was missing from this script's copy
# while release.yml's copy exempted the whole `google_firestore_` family, so
# `make destroy` aborted on every environment with bootstrapped pool documents
# (terraform/infra defaults bootstrap_firestore_documents = true) while CI
# thought the same plan was fine.
TYPES_JSON="${REPO_ROOT}/scripts/lib/unlabelable-types.json"
[[ -f "${TYPES_JSON}" ]] || die "missing ${TYPES_JSON}"
UNLABELABLE_TYPES="$(jq -c '.types' "${TYPES_JSON}")"

# ---------------------------------------------------------------------------
# Self-test. The guard is the only thing standing between `make destroy` and
# another team's production cluster, so it is verifiable without a live plan.
# ---------------------------------------------------------------------------
if [[ "${SELF_TEST}" -eq 1 ]]; then
  step "Guard self-test"
  FIXTURE="${BUILD_DIR}/destroy-guard-fixture.json"
  cat >"${FIXTURE}" <<'FIXTURE_JSON'
{"resource_changes":[
 {"address":"ours.bucket","type":"google_storage_bucket",
  "change":{"actions":["delete"],"before":{"name":"swarm-artifacts-saga-agents-staging",
    "project":"saga-agents-staging","labels":{"managed-by":"swarm-terraform"}}}},
 {"address":"theirs.cluster","type":"google_container_cluster",
  "change":{"actions":["delete"],"before":{"name":"agents-staging","project":"saga-agents-staging",
    "resource_labels":{}}}},
 {"address":"theirs.sa","type":"google_service_account",
  "change":{"actions":["delete"],"before":{"email":"crawler@saga-agents-staging.iam.gserviceaccount.com",
    "project":"saga-agents-staging"}}},
 {"address":"unlabelled.topic","type":"google_pubsub_topic",
  "change":{"actions":["delete"],"before":{"name":"swarm-scheduler-wake","project":"saga-agents-staging",
    "labels":{"component":"scheduler"}}}},
 {"address":"ours.iam","type":"google_project_iam_member",
  "change":{"actions":["delete"],"before":{"project":"saga-agents-staging",
    "member":"serviceAccount:swarm-worker@saga-agents-staging.iam.gserviceaccount.com"}}},
 {"address":"theirs.subnet","type":"google_compute_subnetwork",
  "change":{"actions":["delete"],"before":{"name":"agents-staging-subnet","project":"saga-agents-staging",
    "network":"projects/saga-agents-staging/global/networks/agents-staging-vpc"}}},
 {"address":"elsewhere.bucket","type":"google_storage_bucket",
  "change":{"actions":["delete"],"before":{"name":"x","project":"some-other-project",
    "labels":{"managed-by":"swarm-terraform"}}}},
 {"address":"ours.pool_doc","type":"google_firestore_document",
  "change":{"actions":["delete"],"before":{"collection":"pools","document_id":"global",
    "database":"swarm","project":"saga-agents-staging"}}},
 {"address":"ours.bucket_iam","type":"google_storage_bucket_iam_member",
  "change":{"actions":["delete"],"before":{"bucket":"swarm-artifacts-saga-agents-staging",
    "project":"saga-agents-staging",
    "member":"serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"}}}
]}
FIXTURE_JSON

  SELF_DENY="$(printf '%s\n' "${SHARED_DENY_LIST[@]}" | grep -vx 'default' | jq -R . | jq -sc '.')"
  SELF_VERDICT="$(jq -f "${GUARD_JQ}" --argjson deny "${SELF_DENY}" \
    --argjson allow_types "${UNLABELABLE_TYPES}" --arg project "${PROJECT_ID}" "${FIXTURE}")"

  expect() {
    local label="$1" expr="$2" want="$3" got
    got="$(jq -r "${expr}" <<<"${SELF_VERDICT}")"
    if [[ "${got}" == "${want}" ]]; then
      ok "${label}: ${got}"
    else
      err "${label}: expected ${want}, got ${got}"
      return 1
    fi
  }

  FAILED=0
  expect "deny-listed cluster caught"  '[.denylist_hits[].address] | index("theirs.cluster") != null' true || FAILED=1
  expect "deny-listed SA caught"       '[.denylist_hits[].address] | index("theirs.sa") != null' true || FAILED=1
  expect "deny-listed subnet caught"   '[.denylist_hits[].address] | index("theirs.subnet") != null' true || FAILED=1
  expect "unlabelled topic is an offender" '[.offenders[].address] | index("unlabelled.topic") != null' true || FAILED=1
  expect "labelled bucket is allowed"  '[.labelled_ok[].address] | index("ours.bucket") != null' true || FAILED=1
  expect "IAM edge allowed by type"    '[.unlabelable_allowed[].address] | index("ours.iam") != null' true || FAILED=1
  expect "foreign project caught"      '[.wrong_project[].address] | index("elsewhere.bucket") != null' true || FAILED=1
  expect "data-bearing bucket flagged" '[.data_bearing[].address] | index("ours.bucket") != null' true || FAILED=1
  expect "our bucket is not an offender" '[.offenders[].address] | index("ours.bucket") == null' true || FAILED=1
  # terraform/modules/firestore creates one document per pool and per tenant, and
  # google_firestore_document carries no labels at all. When it was missing from
  # the allow-list, every `make destroy` on a bootstrapped environment aborted
  # with "no labels at all" -- a guard that can never pass is a guard that gets
  # deleted, so it is asserted here rather than discovered at teardown.
  expect "firestore document allowed by type" '[.unlabelable_allowed[].address] | index("ours.pool_doc") != null' true || FAILED=1
  expect "firestore document is not an offender" '[.offenders[].address] | index("ours.pool_doc") == null' true || FAILED=1
  # Matched by SHAPE (`_iam_(member|binding|policy)$`), not by a prefix list, so
  # a bucket/topic/secret/service IAM edge is exempt the day it is written.
  expect "bucket IAM edge allowed by shape" '[.unlabelable_allowed[].address] | index("ours.bucket_iam") != null' true || FAILED=1

  hr
  if [[ "${FAILED}" -eq 1 ]]; then
    die "GUARD SELF-TEST FAILED -- do not run destroy until this passes"
  fi
  ok "guard self-test passed; destroy.sh will abort on shared or unlabelled resources"
  exit 0
fi


# ---------------------------------------------------------------------------
# The proof that the neighbours survived -- also runnable on its own.
# ---------------------------------------------------------------------------
#
# Read-only, and deliberately not welded to the end of a destroy: the one check
# in this repository that makes a claim about ANOTHER TEAM'S resources has to be
# runnable, and testable, without destroying anything first.
# tests/integration/test_shared_resource_verification.py drives it with a fake
# gcloud on PATH.
#
# THREE outcomes, because there are three:
#
#   0  every shared resource answered "present"
#   3  gcloud answered NOT_FOUND for something of theirs   -> escalate
#   4  at least one lookup did not answer at all           -> nothing is proven
#
# 4 is not a milder 3. Until this existed, an expired session -- the single most
# likely reason a lookup fails -- printed "SHARED RESOURCES ARE MISSING AFTER
# DESTROY ... Escalate immediately" and exited 3, naming EVERY ONE of another
# team's resources as deleted on no evidence whatsoever, with the real reason
# discarded into /dev/null by `2>&1`. 4 says the verification did not run, which
# after a destroy is its own emergency and has its own fix.
VERIFY_SURVIVED=0
VERIFY_MISSING=()
VERIFY_UNVERIFIED=()

# _verify MODE LABEL COMMAND... -- record one answer. MODE picks the probe
# shape; see shared_resource_present / shared_resource_listed in lib/common.sh.
_verify() {
  local mode="$1" label="$2"
  shift 2
  local rc=0
  if [[ "${mode}" == "list" ]]; then
    shared_resource_listed "${label}" "$@" || rc=$?
  else
    shared_resource_present "${label}" "$@" || rc=$?
  fi
  case "${rc}" in
    0) ok "${label} still present"; VERIFY_SURVIVED=$((VERIFY_SURVIVED + 1)) ;;
    1) VERIFY_MISSING+=("${label}") ;;
    *) VERIFY_UNVERIFIED+=("${label}") ;;
  esac
}

verify_shared_resources() {
  step "Verifying shared resources are untouched"
  VERIFY_SURVIVED=0
  VERIFY_MISSING=()
  VERIFY_UNVERIFIED=()

  # `clusters list --filter`, never `clusters describe --location`: a location
  # guessed wrong answers NOT_FOUND for a cluster that is running, and this
  # script must not be able to say a live cluster is deleted. The zone was
  # hard-coded to us-central1-a here while CONTRACT.md only pins the region.
  _verify list "gke/agents-staging" \
    gcloud container clusters list --project "${PROJECT_ID}" \
      --filter="name=agents-staging" --format='value(name)'

  local net bucket
  for net in agents-staging-vpc default; do
    _verify describe "network/${net}" \
      gcloud compute networks describe "${net}" --project "${PROJECT_ID}" \
        --format='value(name)'
  done

  for bucket in saga-agents-crawled-media-staging saga-agents-files-staging \
                saga-agents-terraform-state-staging; do
    _verify describe "bucket/${bucket}" \
      gcloud storage buckets describe "gs://${bucket}" --project "${PROJECT_ID}" \
        --format='value(name)'
  done

  # Named one by one rather than counted. The count came from
  # `SA_COUNT="$(gcloud iam service-accounts list ... 2>/dev/null | wc -l)"`,
  # which reports 0 when the call FAILS -- a command substitution swallowing its
  # own failure, printed as "0 service account(s) remain in the project".
  #
  # Read OUT OF the deny-list, not restated beside it. The restatement had
  # already drifted: it listed eleven of the twelve, and the one it dropped was
  # 209012342332-compute@developer.gserviceaccount.com -- the only address on
  # the list that is not <name>@<project>.iam, because it is the project's
  # DEFAULT COMPUTE service account. That is the identity Cloud Run falls back
  # to when a Job names none (scheduler.dispatch.assert_tenant_identity refuses
  # to dispatch rather than use it), so it is the shared account whose loss
  # would be felt furthest and the one nobody was looking at.
  local entry
  for entry in "${SHARED_DENY_LIST[@]}"; do
    case "${entry}" in
      *@*gserviceaccount.com)
        _verify describe "serviceAccount/${entry%%@*}" \
          gcloud iam service-accounts describe "${entry}" \
            --project "${PROJECT_ID}" --format='value(email)'
        ;;
    esac
  done

  hr
  if [[ "${#VERIFY_MISSING[@]}" -gt 0 ]]; then
    err "SHARED RESOURCES ARE GONE: ${VERIFY_MISSING[*]}"
    err "gcloud ANSWERED, and the answer was NOT_FOUND -- this is not a failed lookup."
    err "The plan assertions passed, so this should be impossible. Escalate immediately."
    return 3
  fi
  if [[ "${#VERIFY_UNVERIFIED[@]}" -gt 0 ]]; then
    err "COULD NOT VERIFY ${#VERIFY_UNVERIFIED[@]} shared resource(s): ${VERIFY_UNVERIFIED[*]}"
    err "The reasons are printed above. This is NOT evidence that they are gone,"
    err "and it is NOT evidence that they are fine: the check did not run."
    err "Fix the lookup -- most often 'gcloud auth login' -- and re-run:"
    err "    scripts/destroy.sh --environment ${ENVIRONMENT} --verify-shared"
    return 4
  fi
  ok "all ${VERIFY_SURVIVED} shared resource checks passed; other teams are unaffected"
  return 0
}

if [[ "${VERIFY_ONLY}" -eq 1 ]]; then
  VERIFY_RC=0
  verify_shared_resources || VERIFY_RC=$?
  exit "${VERIFY_RC}"
fi

TF_ROOT="$(tf_root)"
tf_var_args

step "Target"
info "project      ${PROJECT_ID}"
info "environment  ${ENVIRONMENT}"
info "root         ${TF_ROOT}"
info "variables    $(tf_var_file)"
info "state        gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX}"

if is_production; then
  if [[ "${ALLOW_PROD}" -eq 0 ]]; then
    err "environment is '${ENVIRONMENT}'."
    die "production teardown requires --allow-prod, on purpose."
  fi
  warn "PRODUCTION teardown requested. Two confirmations will be required."
fi

[[ -d "${TF_ROOT}/.terraform" ]] || die "terraform is not initialised in ${TF_ROOT}; run: make bootstrap"

# ---------------------------------------------------------------------------
# 1. Plan
# ---------------------------------------------------------------------------
step "Destroy plan"
PLAN_BIN="${BUILD_DIR}/destroy-${ENVIRONMENT}.tfplan"
PLAN_LOG="${BUILD_DIR}/destroy-${ENVIRONMENT}.plan.jsonl"
PLAN_JSON="${BUILD_DIR}/destroy-${ENVIRONMENT}.plan.json"
VERDICT_JSON="${BUILD_DIR}/destroy-${ENVIRONMENT}.verdict.json"

PLAN_ARGS=(-destroy -json -input=false -lock-timeout=120s -out="${PLAN_BIN}" "${TF_VAR_ARGS[@]}")
for t in ${TARGETS[@]+"${TARGETS[@]}"}; do PLAN_ARGS+=(-target="${t}"); done

info "terraform plan -destroy -json (log: ${PLAN_LOG})"
if ! tf -chdir="${TF_ROOT}" plan "${PLAN_ARGS[@]}" >"${PLAN_LOG}" 2>&1; then
  err "terraform plan -destroy failed:"
  jq -r 'select(.["@level"]=="error") | .["@message"]' <"${PLAN_LOG}" 2>/dev/null | redact >&2 \
    || tail -n 40 "${PLAN_LOG}" | redact >&2
  die "cannot continue without a plan"
fi
tf -chdir="${TF_ROOT}" show -json "${PLAN_BIN}" >"${PLAN_JSON}"
ok "plan written"

# ---------------------------------------------------------------------------
# 2. Assertions
# ---------------------------------------------------------------------------
step "Safety assertions"


# The deny-list, minus the bare "default" token -- that string appears inside too
# many unrelated ids to compare blindly. The shared default network is matched on
# the network/subnetwork fields instead, inside destroy-guard.jq.
DENY_JSON="$(printf '%s\n' "${SHARED_DENY_LIST[@]}" \
  | grep -vx 'default' | jq -R . | jq -sc '. + ["(default)"]')"

jq -f "${GUARD_JQ}" \
   --argjson deny "${DENY_JSON}" \
   --argjson allow_types "${UNLABELABLE_TYPES}" \
   --arg project "${PROJECT_ID}" \
   "${PLAN_JSON}" >"${VERDICT_JSON}"

DELETIONS="$(jq -r '.deletions' "${VERDICT_JSON}")"
N_OFFENDERS="$(jq -r '.offenders | length' "${VERDICT_JSON}")"
N_DENY="$(jq -r '.denylist_hits | length' "${VERDICT_JSON}")"
N_MUTATIONS="$(jq -r '.unexpected_mutations | length' "${VERDICT_JSON}")"
N_WRONG_PROJECT="$(jq -r '.wrong_project | length' "${VERDICT_JSON}")"
N_DATA="$(jq -r '.data_bearing | length' "${VERDICT_JSON}")"

if [[ "${DELETIONS}" -eq 0 ]]; then
  ok "the plan deletes nothing; there is nothing to destroy"
  exit 0
fi

info "${DELETIONS} resource(s) marked for deletion:"
jq -r '.by_type[] | "    \(.count)x \(.type)"' "${VERDICT_JSON}" >&2

ABORT=0

if [[ "${N_DENY}" -gt 0 ]]; then
  hr
  err "STOP. The plan would delete resources that belong to other teams."
  jq -r '.denylist_hits[] | "    \(.address)  (\(.type))  matched: \(.matched | join(", "))"' \
    "${VERDICT_JSON}" >&2
  err "These are deny-listed shared resources in ${PROJECT_ID}."
  ABORT=1
fi

if [[ "${N_OFFENDERS}" -gt 0 ]]; then
  hr
  err "STOP. ${N_OFFENDERS} resource(s) marked for deletion do not carry managed-by=swarm-terraform:"
  jq -r '.offenders[] | "    \(.address)\n        type:   \(.type)\n        reason: \(.reason)"' \
    "${VERDICT_JSON}" >&2
  err "Either they are not ours, or Terraform is missing the label. Fix the labels; do not bypass this."
  ABORT=1
fi

if [[ "${N_WRONG_PROJECT}" -gt 0 ]]; then
  hr
  err "STOP. Resource(s) in the plan live in a different project than ${PROJECT_ID}:"
  jq -r '.wrong_project[] | "    \(.address) -> \(.project)"' "${VERDICT_JSON}" >&2
  ABORT=1
fi

if [[ "${N_MUTATIONS}" -gt 0 ]]; then
  hr
  err "STOP. A destroy plan that also creates or updates resources means state and reality disagree:"
  jq -r '.unexpected_mutations[] | "    \(.address)  \(.actions | join("+"))"' "${VERDICT_JSON}" >&2
  err "Run 'make tf-plan' and reconcile first."
  ABORT=1
fi

if [[ "${ABORT}" -eq 1 ]]; then
  hr
  err "ABORTED. Nothing was destroyed. Full verdict: ${VERDICT_JSON}"
  exit 2
fi

ok "every deletion carries managed-by=swarm-terraform or is an unlabelable type on the allow-list"
ok "no deny-listed shared resource appears in the plan"
UNLABELABLE_COUNT="$(jq -r '.unlabelable_allowed | length' "${VERDICT_JSON}")"
if [[ "${UNLABELABLE_COUNT}" -gt 0 ]]; then
  info "${UNLABELABLE_COUNT} unlabelable resource(s) allowed by type:"
  jq -r '.unlabelable_allowed[] | "    \(.address)  (\(.type))"' "${VERDICT_JSON}" >&2
fi

if [[ "${N_DATA}" -gt 0 ]]; then
  hr
  warn "${N_DATA} data-bearing resource(s) are in the plan:"
  jq -r '.data_bearing[] | "    \(.address)  \(.name)"' "${VERDICT_JSON}" >&2
  if [[ "${INCLUDE_DATA}" -eq 0 ]]; then
    err "Destroying these deletes task history, artifacts and checkpoints permanently."
    err "Re-run with --include-data if that is what you want."
    err "To keep them, remove them from the plan first: terraform state rm <address>"
    die "ABORTED to protect data. Nothing was destroyed."
  fi
  warn "--include-data given; these WILL be deleted"
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  hr
  ok "dry run complete; all assertions passed. Nothing was destroyed."
  info "verdict: ${VERDICT_JSON}"
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. Confirmation
# ---------------------------------------------------------------------------
step "Confirmation"
hr
tf -chdir="${TF_ROOT}" show -no-color "${PLAN_BIN}" 2>/dev/null | head -n 60 | redact >&2
dim "(plan truncated; full plan: ${PLAN_JSON})"
hr

confirm "About to DESTROY ${DELETIONS} swarm resource(s) in ${PROJECT_ID} (${ENVIRONMENT})." "destroy-${ENVIRONMENT}"
if is_production; then
  confirm "This is PRODUCTION. Confirm the project id." "${PROJECT_ID}"
fi

# ---------------------------------------------------------------------------
# 4. Apply
# ---------------------------------------------------------------------------
step "Destroying"
if ! tf -chdir="${TF_ROOT}" apply -input=false -lock-timeout=120s "${PLAN_BIN}" 2>&1 | redact; then
  err "terraform apply of the destroy plan failed part-way"
  err "re-run this script: the plan is regenerated from live state each time"
  exit 1
fi
ok "terraform destroy applied"

# ---------------------------------------------------------------------------
# 5. Prove the shared resources survived
# ---------------------------------------------------------------------------
VERIFY_RC=0
verify_shared_resources || VERIFY_RC=$?
[[ "${VERIFY_RC}" -eq 0 ]] || exit "${VERIFY_RC}"
ok "swarm infrastructure destroyed (${ENVIRONMENT})"
dim "runtime data is separate: scripts/purge-data.sh removes Firestore documents, artifacts and secrets"
