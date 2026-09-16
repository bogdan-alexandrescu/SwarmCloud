#!/usr/bin/env bash
# Judge a terraform plan against the shared-project rules. ONE implementation.
#
# `saga-agents-staging` holds another team's live GKE cluster, their VPC, their
# buckets and twelve of their service accounts, so every plan -- apply or destroy
# -- has to satisfy two assertions:
#
#   1. no change may touch anything on SHARED_DENY_LIST;
#   2. everything this repository creates must carry managed-by=swarm-terraform,
#      and everything it deletes must already carry it (or be of a type that
#      physically cannot, from scripts/lib/unlabelable-types.json).
#
# Assertion 2's forward half is the one that gets skipped, and it is the one that
# matters most later: `make destroy` REFUSES to delete an unlabelled resource, so
# an unlabelled resource that merges is one nobody can ever clean up.
#
# This script exists because that rule was previously restated three times -- in
# destroy.sh, in an awk regex in .github/workflows/release.yml, and in a jq
# filter in .github/workflows/terraform.yml -- and the copies disagreed. The awk
# copy exempted only `google_project_iam*`, so a plan deleting a
# `google_storage_bucket_iam_member` (which happens whenever a tenant is removed
# or a tenant's provider list changes) was blocked from production by a rule
# nobody had decided on. The terraform.yml copy checked the deny-list and simply
# never implemented the label half its own comment promised.
#
# Usage:
#   scripts/lib/plan-guard.sh --plan terraform/infra/plan.json            # apply
#   scripts/lib/plan-guard.sh --plan build/dev.plan.json --mode destroy
#   scripts/lib/plan-guard.sh --self-test
#
# Exit 0 = the plan is allowed. Exit 2 = it is not. Exit 1 = it could not be
# judged, which is also a refusal: this fails closed.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

PLAN=""
MODE="apply"
SELF_TEST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan|-p)   PLAN="$2"; shift 2 ;;
    --mode|-m)   MODE="$2"; shift 2 ;;
    --self-test) SELF_TEST=1; shift ;;
    -h|--help)   sed -n '2,33p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd jq
GUARD_JQ="${SWARM_LIB_DIR}/destroy-guard.jq"
TYPES_JSON="${SWARM_LIB_DIR}/unlabelable-types.json"
[[ -f "${GUARD_JQ}" ]]   || die "missing ${GUARD_JQ}"
[[ -f "${TYPES_JSON}" ]] || die "missing ${TYPES_JSON}"

# The allow-list of types that cannot carry a label. Read from the file so that
# destroy.sh and both workflows are judging by the same list.
unlabelable_types() { jq -c '.types' "${TYPES_JSON}"; }

# The deny-list, minus the bare "default" token -- that string appears inside too
# many unrelated ids to compare blindly. The shared default network is matched on
# the network/subnetwork fields instead, inside destroy-guard.jq.
deny_json() {
  printf '%s\n' "${SHARED_DENY_LIST[@]}" | grep -vx 'default' | jq -R . | jq -sc '. + ["(default)"]'
}

# judge PLAN_JSON VERDICT_OUT
judge() {
  local plan="$1" out="$2"
  jq -f "${GUARD_JQ}" \
    --argjson deny "$(deny_json)" \
    --argjson allow_types "$(unlabelable_types)" \
    --arg project "${PROJECT_ID}" \
    "${plan}" >"${out}"
}

# report VERDICT_JSON MODE -> 0 allowed, 2 refused
report() {
  local verdict="$1" mode="$2" abort=0 n

  n="$(jq -r '.denylist_touches | length' "${verdict}")"
  if [[ "${n}" -gt 0 ]]; then
    hr
    err "STOP. ${n} change(s) target resources that belong to another team:"
    jq -r '.denylist_touches[] | "    \(.address)  (\(.type))  \(.actions|join("+"))  matched: \(.matched|join(", "))"' \
      "${verdict}" >&2
    abort=1
  fi

  n="$(jq -r '.offenders | length' "${verdict}")"
  if [[ "${n}" -gt 0 ]]; then
    hr
    err "STOP. ${n} resource(s) marked for DELETION do not carry managed-by=swarm-terraform:"
    jq -r '.offenders[] | "    \(.address)\n        type:   \(.type)\n        reason: \(.reason)"' \
      "${verdict}" >&2
    err "Either they are not ours, or Terraform is missing the label. Fix the labels; do not bypass this."
    abort=1
  fi

  n="$(jq -r '.wrong_project | length' "${verdict}")"
  if [[ "${n}" -gt 0 ]]; then
    hr
    err "STOP. Resource(s) in the plan live in a different project than ${PROJECT_ID}:"
    jq -r '.wrong_project[] | "    \(.address) -> \(.project)"' "${verdict}" >&2
    abort=1
  fi

  if [[ "${mode}" == "apply" ]]; then
    n="$(jq -r '.unlabelled_creations | length' "${verdict}")"
    if [[ "${n}" -gt 0 ]]; then
      hr
      err "STOP. ${n} resource(s) this plan CREATES or UPDATES carry no managed-by=swarm-terraform:"
      jq -r '.unlabelled_creations[] | "    \(.address)\n        type:   \(.type)\n        reason: \(.reason)"' \
        "${verdict}" >&2
      err "make destroy refuses to delete an unlabelled resource, so merging this creates"
      err "something nobody can clean up later -- in a project holding another team's production."
      abort=1
    fi
  fi

  if [[ "${mode}" == "destroy" ]]; then
    n="$(jq -r '.unexpected_mutations | length' "${verdict}")"
    if [[ "${n}" -gt 0 ]]; then
      hr
      err "STOP. A destroy plan that also creates or updates resources means state and reality disagree:"
      jq -r '.unexpected_mutations[] | "    \(.address)  \(.actions | join("+"))"' "${verdict}" >&2
      abort=1
    fi
  fi

  [[ "${abort}" -eq 0 ]] || return 2
  return 0
}

# ---------------------------------------------------------------------------
# Self-test. The guard decides whether a plan may touch a shared project, so it
# is verified from fixtures rather than trusted -- and the fixtures are the cases
# that were actually got wrong before.
# ---------------------------------------------------------------------------
if [[ "${SELF_TEST}" -eq 1 ]]; then
  step "Plan-guard self-test"
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-plan-guard.XXXXXX")"
  trap 'rm -rf "${WORK}"' EXIT INT TERM

  cat >"${WORK}/plan.json" <<'FIXTURE_JSON'
{"resource_changes":[
 {"address":"labelled.bucket","type":"google_storage_bucket",
  "change":{"actions":["create"],"before":null,
    "after":{"name":"saga-agents-staging-swarm-artifacts","project":"saga-agents-staging",
             "labels":{"managed-by":"swarm-terraform"}}}},
 {"address":"unlabelled.topic","type":"google_pubsub_topic",
  "change":{"actions":["create"],"before":null,
    "after":{"name":"swarm-scheduler-wake","project":"saga-agents-staging",
             "labels":{"component":"scheduler"}}}},
 {"address":"iam.edge","type":"google_storage_bucket_iam_member",
  "change":{"actions":["create"],"before":null,
    "after":{"bucket":"saga-agents-staging-swarm-artifacts","project":"saga-agents-staging",
             "member":"serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"}}},
 {"address":"unknown.labels","type":"google_cloud_run_v2_service",
  "change":{"actions":["create"],"before":null,
    "after":{"name":"swarm-api","project":"saga-agents-staging"},
    "after_unknown":{"labels":true}}},
 {"address":"iam.edge.delete","type":"google_secret_manager_secret_iam_binding",
  "change":{"actions":["delete"],
    "before":{"secret_id":"swarm-tenant-eng-anthropic","project":"saga-agents-staging"}}},
 {"address":"firestore.doc.delete","type":"google_firestore_document",
  "change":{"actions":["delete"],
    "before":{"collection":"pools","document_id":"global","database":"swarm",
              "project":"saga-agents-staging"}}},
 {"address":"theirs.binding","type":"google_storage_bucket_iam_member",
  "change":{"actions":["create"],"before":null,
    "after":{"bucket":"saga-agents-files-staging","project":"saga-agents-staging",
             "member":"serviceAccount:swarm-agent-worker-eng@saga-agents-staging.iam.gserviceaccount.com"}}}
]}
FIXTURE_JSON

  judge "${WORK}/plan.json" "${WORK}/verdict.json"

  FAILED=0
  check() {
    local label="$1" expr="$2" want="$3" got
    got="$(jq -r "${expr}" <"${WORK}/verdict.json")"
    if [[ "${got}" == "${want}" ]]; then ok "${label}: ${got}"; else err "${label}: expected ${want}, got ${got}"; FAILED=1; fi
  }

  check "a labelled creation passes" \
    '[.unlabelled_creations[].address] | index("labelled.bucket") == null' true
  check "an unlabelled creation is caught" \
    '[.unlabelled_creations[].address] | index("unlabelled.topic") != null' true
  check "an IAM edge needs no label (create)" \
    '[.unlabelled_creations[].address] | index("iam.edge") == null' true
  check "labels unknown at plan time are not a finding" \
    '[.unlabelled_creations[].address] | index("unknown.labels") == null' true
  # The four cases below are the ones release.yml's awk regex got wrong.
  check "a non-project IAM edge deletion is exempt (release.yml regression)" \
    '[.offenders[].address] | index("iam.edge.delete") == null' true
  check "google_firestore_document deletion is exempt (make destroy regression)" \
    '[.offenders[].address] | index("firestore.doc.delete") == null' true
  check "a binding CREATED on another team's bucket is caught" \
    '[.denylist_touches[].address] | index("theirs.binding") != null' true
  check "our own bucket is not a deny-list hit" \
    '[.denylist_touches[].address] | index("labelled.bucket") == null' true

  hr
  [[ "${FAILED}" -eq 0 ]] || die "PLAN-GUARD SELF-TEST FAILED -- do not trust the workflow gates until this passes"
  ok "plan-guard self-test passed"
  exit 0
fi

# ---------------------------------------------------------------------------
[[ -n "${PLAN}" ]] || die "--plan <terraform show -json output> is required"
[[ -f "${PLAN}" ]] || die "no such plan file: ${PLAN}"
case "${MODE}" in
  apply|destroy) ;;
  *) die "--mode must be 'apply' or 'destroy', got '${MODE}'" ;;
esac

VERDICT="${BUILD_DIR}/plan-guard-verdict.json"
judge "${PLAN}" "${VERDICT}"

step "Shared-project guard (${MODE})"
info "$(jq -r '"\(.total_changes) change(s), \(.deletions) deletion(s)"' "${VERDICT}")"

if report "${VERDICT}" "${MODE}"; then
  ok "no deny-listed resource is touched"
  ok "every deletion is ours, and every creation carries managed-by=swarm-terraform"
  exit 0
fi

hr
err "REFUSED. Full verdict: ${VERDICT}"
exit 2
