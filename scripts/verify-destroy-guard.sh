#!/usr/bin/env bash
# Prove the destroy guard against a REAL terraform plan, not a hand-written fixture.
#
# WHY THIS EXISTS. `make destroy` is the only thing standing between this
# repository and another team's production: saga-agents-staging holds their live
# GKE cluster `agents-staging`, their VPC, two subnets, three buckets, twelve
# service accounts and Firestore's shared `(default)` database. The guard that
# protects them had never been run against a real `terraform show -json` -- every
# test of it fed jq fixtures written by hand to match the code.
#
# That is not a theoretical gap. The fixtures had already drifted: the deny-list
# restatement in tests/integration/test_destroy_guard.py held 14 of the 20
# entries and held service accounts by SHORT NAME where production passes full
# emails, and a third copy of the deny-list transformation inside
# `destroy.sh --self-test` omitted the `(default)` entry, so the self-test of the
# guard protecting the shared Firestore database could never reach that branch.
# A fixture agrees with whoever wrote it. A real plan does not.
#
# WHAT IT DOES. It takes the destroy plan `scripts/destroy.sh --dry-run` already
# produced, and:
#
#   1. runs the real guard entry point over it unchanged and requires a PASS;
#   2. for EVERY entry of SHARED_DENY_LIST -- all 20 of them, placed by the
#      table in scripts/lib/destroy-guard-proof-cases.json rather than by hand,
#      with an unclassifiable entry a fatal error rather than a skip -- copies
#      the REAL resource_change of the type that entry belongs to, substitutes
#      the neighbour's identity into the fields the real provider populates, and
#      requires the guard to REFUSE and to name it;
#   3. mutates real labels the four ways that matter (managed-by removed, a
#      near-miss value, `labels: {}` with the truth in effective_labels, an
#      unknown resource type) and requires refuse / refuse / pass / refuse;
#   4. flips one deletion into an update and requires the destroy-mode refusal;
#   5. optionally RECORDS a redacted copy of the real plan for
#      tests/integration/test_destroy_guard_real_plan.py, and checks that the
#      recording still judges identically to the plan it came from.
#
# WHAT IT NEVER DOES. It never applies anything. It reads a plan file; the only
# terraform in this script is the absence of it. `terraform destroy` appears
# nowhere here, deliberately, and the plan it reads is produced by the read-only
# `--dry-run` path of destroy.sh.
#
# THE PLAN FILE IS HAZARDOUS. `build/destroy-<env>.tfplan` is an APPLYABLE
# destroy plan for a shared project. This script does not create it, does not
# move it and does not delete it -- but do not commit it and do not leave it
# lying around; `build/` is gitignored for that reason.
#
# Usage (or `make destroy-guard-proof`, which runs both steps):
#   scripts/destroy.sh --environment dev --dry-run          # produces the plan
#   scripts/verify-destroy-guard.sh --environment dev       # proves the guard on it
#   scripts/verify-destroy-guard.sh --plan <show-json>      # or any plan json
#   scripts/verify-destroy-guard.sh --record tests/integration/fixtures/destroy-plan-dev-real.json
#
# Exit 0 = every assertion held. Exit 1 = something could not be checked (which
# is a failure, not a skip). Exit 2 = the guard did not behave as required.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

PLAN=""
RECORD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    # Same spelling as every other script here, and it is load-bearing rather
    # than cosmetic: with no --plan, the plan judged is
    # build/destroy-<ENVIRONMENT>.plan.json, so naming the wrong environment
    # would judge a different environment's plan and call the guard proven.
    --environment|-e) ENVIRONMENT="$2"; export ENVIRONMENT; shift 2 ;;
    --plan|-p)        PLAN="$2"; shift 2 ;;
    --record|-r)      RECORD="$2"; shift 2 ;;
    -h|--help)        sed -n '2,56p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd jq

GUARD="${REPO_ROOT}/scripts/lib/plan-guard.sh"
[[ -x "${GUARD}" ]] || die "missing ${GUARD}"
GUARD_JQ="${SWARM_LIB_DIR}/destroy-guard.jq"
[[ -f "${GUARD_JQ}" ]] || die "missing ${GUARD_JQ}"
UNLABELABLE_TYPES="$(jq -c '.types' "${SWARM_LIB_DIR}/unlabelable-types.json")"

PLAN="${PLAN:-${BUILD_DIR}/destroy-${ENVIRONMENT}.plan.json}"
if [[ ! -f "${PLAN}" ]]; then
  err "no plan to judge: ${PLAN}"
  err "Produce one first -- it is read-only and applies nothing:"
  die  "    scripts/destroy.sh --environment ${ENVIRONMENT} --dry-run"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-guard-proof.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM

CHECKS=0
FAILED=0

# ---------------------------------------------------------------------------
# The plan has to be a DESTROY plan, and it has to contain something.
# ---------------------------------------------------------------------------
#
# "Empty output is not success" (CLAUDE.md). A plan with no resource_changes
# would pass every assertion below by having nothing to judge, and this script
# would print a wall of green having proved nothing at all.
step "The plan being judged"
DELETIONS="$(jq -r '[.resource_changes[]? | select((.change.actions // []) | index("delete"))] | length' "${PLAN}")"
TOTAL="$(jq -r '(.resource_changes // []) | length' "${PLAN}")"
info "${PLAN}"
info "${TOTAL} resource change(s), ${DELETIONS} deletion(s)"
[[ "${DELETIONS}" -gt 0 ]] || die "this plan deletes nothing; there is no guard behaviour to prove"
[[ "${TOTAL}" -eq "${DELETIONS}" ]] || warn "the plan is not purely a destroy plan (${TOTAL} changes, ${DELETIONS} deletions)"

# ---------------------------------------------------------------------------
# Assertion helpers. `plan-guard.sh` is the entry point the CI gates use and it
# shares its jq filter, its deny-list and its type allow-list with
# `scripts/destroy.sh`, so a verdict proven here is the verdict `make destroy`
# reaches. Exit 2 is "refused"; exit 1 is "could not judge", which this script
# treats as a failure of its own rather than as a refusal.
# ---------------------------------------------------------------------------

_run_guard() {
  local plan="$1" rc=0
  NO_COLOR=1 "${GUARD}" --mode destroy --plan "${plan}" >"${WORK}/guard.out" 2>&1 || rc=$?
  return "${rc}"
}

# expect_pass LABEL PLAN
expect_pass() {
  local label="$1" plan="$2" rc=0
  CHECKS=$((CHECKS + 1))
  _run_guard "${plan}" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    ok "${label}"
  else
    err "${label}: expected the guard to ALLOW this plan, got exit ${rc}"
    redact <"${WORK}/guard.out" | tail -n 12 >&2
    FAILED=$((FAILED + 1))
  fi
}

# expect_refusal LABEL PLAN NEEDLE [NEEDLE...]
# Every needle must appear in the guard's own output: a refusal that does not
# NAME the offending resource sends an operator looking through 264 addresses.
expect_refusal() {
  local label="$1" plan="$2"; shift 2
  local rc=0 needle
  CHECKS=$((CHECKS + 1))
  _run_guard "${plan}" || rc=$?
  if [[ "${rc}" -ne 2 ]]; then
    err "${label}: expected exit 2 (refused), got ${rc}"
    redact <"${WORK}/guard.out" | tail -n 12 >&2
    FAILED=$((FAILED + 1))
    return 0
  fi
  for needle in "$@"; do
    if ! grep -qF -- "${needle}" "${WORK}/guard.out"; then
      err "${label}: refused, but never named '${needle}'"
      redact <"${WORK}/guard.out" | tail -n 12 >&2
      FAILED=$((FAILED + 1))
      return 0
    fi
  done
  ok "${label}"
}

# THE TWO ENTRY POINTS DO NOT READ THE SAME KEY, and that is not a detail.
#
# `scripts/lib/plan-guard.sh` (the CI gates) refuses on `.denylist_touches`,
# which is built from `deny_hits_both` -- tokens of `before` AND `after`.
# `scripts/destroy.sh` (`make destroy`) refuses on `.denylist_hits`, built from
# `deny_hits` -- tokens of `before` only. They are different definitions in the
# same jq file, so a defect in one is invisible through the other.
#
# MEASURED on 2026-09-24: replacing `def tokens: tokens_of(before)` with a read
# of a field the real format does not have left every assertion in this script
# green, because plan-guard.sh never consults the key that broke. `make destroy`
# with that jq would have failed OPEN on a deny-listed resource. Every deny case
# below therefore asserts the verdict key `make destroy` actually reads, as well
# as the refusal the CI gate makes.
expect_denylist_hit() {
  local label="$1" plan="$2" addr="$3" hit
  CHECKS=$((CHECKS + 1))
  hit="$(jq -f "${GUARD_JQ}" \
           --argjson deny "${DENY_TOKENS}" \
           --argjson allow_types "${UNLABELABLE_TYPES}" \
           --arg project "${PROJECT_ID}" "${plan}" \
         | jq -r --arg a "${addr}" '[.denylist_hits[].address] | index($a) != null')"
  if [[ "${hit}" == "true" ]]; then
    ok "${label}"
  else
    err "${label}: the resource is not in .denylist_hits, the key scripts/destroy.sh reads"
    err "the CI plan guard reads .denylist_touches instead, so it can pass while make destroy fails open"
    FAILED=$((FAILED + 1))
  fi
}

# index_of_type TYPE -> the position of the first resource_change of that type,
# or "none". Used to pick a REAL resource to mutate rather than inventing one.
index_of_type() {
  jq -r --arg t "$1" '[.resource_changes[].type] | (index($t) // "none")' "${PLAN}"
}

address_at() {
  jq -r --argjson i "$1" '.resource_changes[$i].address' "${PLAN}"
}

# mutate IDX JQ_EXPR OUT -- rewrite one real resource's `before` and write the
# whole plan out. The expression runs with `.` bound to that resource's `before`,
# and with $e (the deny-list entry) and $p (the project) in scope.
mutate() {
  local idx="$1" expr="$2" out="$3" entry="${4:-}"
  jq --argjson i "${idx}" --arg e "${entry}" --arg p "${PROJECT_ID}" \
     ".resource_changes[\$i].change.before |= (${expr})" "${PLAN}" >"${out}"
}

# ---------------------------------------------------------------------------
# 1. The real plan, unmutated.
# ---------------------------------------------------------------------------
#
# On its own this proves little -- a guard that returned 0 unconditionally would
# also pass -- which is exactly why every case below it is a refusal case. It is
# here because the opposite failure is just as expensive: a guard that refuses a
# legitimate plan is a guard people learn to bypass.
step "1. The real plan is allowed"
expect_pass "the unmutated destroy plan passes" "${PLAN}"

# ---------------------------------------------------------------------------
# 2. Every deny-list entry, in real plan shape.
# ---------------------------------------------------------------------------
#
# The deny-list holds five KINDS of thing and they do not carry their identity
# in the same field: a service account is `.email` (and `.name` is
# `projects/-/serviceAccounts/<email>`), a bucket is `.name`, a cluster's `.id`
# is `projects/<p>/locations/<loc>/clusters/<name>`, a network's is
# `projects/<p>/global/networks/<name>`, and Firestore's database is
# `projects/<p>/databases/(default)`. Substituting the neighbour into a real
# resource of the matching type is what makes this a test of the guard rather
# than a test of the fixture writer's memory.
#
# The bare `default` is the one entry that CANNOT be matched as a token --
# `guard_deny_json` removes it, because the string appears inside too many
# unrelated ids -- so it is proved through the field the guard really uses for
# it: `before.network` ending in `/default`.
#
# AN UNCLASSIFIABLE ENTRY IS FATAL. A new deny-list entry that matches none of
# the rules in the table must stop this script, not be skipped with a warning:
# the defect this whole file exists to answer is a proof that silently covered
# 14 of the 20 entries. The count is asserted below, too, for the same reason.
step "2. Every deny-list entry is caught, substituted into a real resource"

# The mapping lives in scripts/lib/destroy-guard-proof-cases.json, read by this
# script and by tests/integration/test_destroy_guard_real_plan.py. See that
# file's own description for why it is not a case statement in each of them.
CASES_JSON="${SWARM_LIB_DIR}/destroy-guard-proof-cases.json"
[[ -f "${CASES_JSON}" ]] || die "missing ${CASES_JSON}"

# kind_of_entry ENTRY -> the first `classify` rule whose glob matches, or fail.
# The RHS of `==` is deliberately unquoted: that is what makes bash treat the
# pattern as a glob, which is the whole point of the table.
kind_of_entry() {
  local entry="$1" glob kind
  while IFS=$'\t' read -r glob kind; do
    [[ -n "${glob}" ]] || continue
    # shellcheck disable=SC2053  # glob matching is intended, not accidental
    if [[ "${entry}" == ${glob} ]]; then
      printf '%s' "${kind}"
      return 0
    fi
  done < <(jq -r '.classify[] | "\(.glob)\t\(.kind)"' "${CASES_JSON}")
  return 1
}

kind_field() {
  jq -er --arg k "$1" --arg f "$2" '.kinds[$k][$f]' "${CASES_JSON}"
}

# The token list the guard is really given, so the entry `default` (removed) and
# the entry `(default)` (added) are both accounted for here rather than assumed.
DENY_TOKENS="$(guard_deny_json)"
ENTRIES=()
for entry in "${SHARED_DENY_LIST[@]}"; do ENTRIES+=("${entry}"); done
ENTRIES+=("(default)")

COVERED=0
: >"${WORK}/kinds.txt"
for entry in ${ENTRIES[@]+"${ENTRIES[@]}"}; do
  kind="$(kind_of_entry "${entry}")" \
    || die "deny-list entry '${entry}' matches no rule in ${CASES_JSON}; classify it there rather than leaving it unproven"
  printf '%s\n' "${kind}" >>"${WORK}/kinds.txt"
  carrier="$(kind_field "${kind}" carrier_type)" || die "no carrier_type for kind '${kind}' in ${CASES_JSON}"
  patch="$(kind_field "${kind}" patch)"          || die "no patch for kind '${kind}' in ${CASES_JSON}"
  matched="$(kind_field "${kind}" expect_matched)" || die "no expect_matched for kind '${kind}'"
  [[ "${matched}" != "entry" ]] || matched="${entry}"

  idx="$(index_of_type "${carrier}")"
  if [[ "${idx}" == "none" ]]; then
    err "this plan contains no ${carrier}, so '${entry}' (${kind}) cannot be proved against it"
    FAILED=$((FAILED + 1))
    continue
  fi
  addr="$(address_at "${idx}")"
  mutate "${idx}" "${patch}" "${WORK}/deny.json" "${entry}"

  # Both the ADDRESS and the reason must appear: a refusal that does not say
  # which resource, and why, sends an operator through 264 addresses by hand.
  expect_refusal "deny: ${entry} (${kind}, via ${carrier})" "${WORK}/deny.json" "${addr}" "${matched}"
  expect_denylist_hit "deny: ${entry} is in .denylist_hits (the key make destroy reads)" \
    "${WORK}/deny.json" "${addr}"
  COVERED=$((COVERED + 1))
done

info "${COVERED} deny-list entr(ies) placed into a real resource and refused"
# The count is asserted, not printed and forgotten: a loop that iterated once --
# the zsh word-splitting trap CLAUDE.md names -- would otherwise report a clean
# sweep over a single entry.
EXPECTED_ENTRIES="$(( ${#SHARED_DENY_LIST[@]} + 1 ))"
if [[ "${COVERED}" -ne "${EXPECTED_ENTRIES}" ]]; then
  err "covered ${COVERED} of ${EXPECTED_ENTRIES} deny-list entries"
  FAILED=$((FAILED + 1))
fi
# And the list the guard judges by must be the same length, or the two halves of
# this proof are describing different lists.
TOKEN_COUNT="$(jq -r 'length' <<<"${DENY_TOKENS}")"
if [[ "${TOKEN_COUNT}" -ne "$(( ${#SHARED_DENY_LIST[@]} ))" ]]; then
  err "guard_deny_json produced ${TOKEN_COUNT} tokens from ${#SHARED_DENY_LIST[@]} deny-list entries"
  err "it removes the bare 'default' and adds '(default)', so the count must not change"
  FAILED=$((FAILED + 1))
fi

# THE INVENTORY FLOOR, and why a self-referential proof needs one.
#
# Everything above iterates SHARED_DENY_LIST, so it proves the guard agrees with
# whatever that list currently says -- and nothing at all about the list being
# complete. MEASURED on 2026-09-24: deleting one service account from
# SHARED_DENY_LIST and re-running this script produced 28 green assertions and
# exit 0. That is the same shape as the defect this exercise exists to answer, a
# proof covering 14 of 20 entries and reporting success.
#
# So the shared project's inventory is asserted against the numbers CLAUDE.md
# and CONTRACT.md state, read from destroy-guard-proof-cases.json, independently
# of the deny-list. Floors, not equalities: gaining a neighbour is normal,
# losing one silently is the thing that must be impossible.
step "2b. The deny-list still covers the whole shared inventory"
while IFS=$'\t' read -r floor_kind floor_n; do
  [[ -n "${floor_kind}" ]] || continue
  CHECKS=$((CHECKS + 1))
  if [[ "${floor_kind}" == "total" ]]; then
    got="${#SHARED_DENY_LIST[@]}"
  else
    got="$(grep -cx -- "${floor_kind}" "${WORK}/kinds.txt" || true)"
  fi
  if [[ "${got}" -ge "${floor_n}" ]]; then
    ok "${floor_kind}: ${got} (floor ${floor_n})"
  else
    err "${floor_kind}: ${got}, below the floor of ${floor_n}"
    err "an entry has been REMOVED from SHARED_DENY_LIST -- one of the other team's"
    err "resources is no longer protected, and every check above iterates that list,"
    err "so nothing else here would have noticed"
    FAILED=$((FAILED + 1))
  fi
done < <(jq -r '.inventory_floor | to_entries[] | "\(.key)\t\(.value)"' "${CASES_JSON}")

# ---------------------------------------------------------------------------
# 3. The label half, against real label maps.
# ---------------------------------------------------------------------------
step "3. managed-by=swarm-terraform, judged on real label maps"

LABELLED_IDX="$(jq -r '
  [ .resource_changes[] | select((.change.actions // []) | index("delete"))
    | select((.change.before.labels // {})["managed-by"] == "swarm-terraform") ]
  | if length == 0 then "none" else 0 end' "${PLAN}")"
if [[ "${LABELLED_IDX}" == "none" ]]; then
  err "no deletion in this plan carries labels['managed-by']=swarm-terraform, so the label half cannot be proved here"
  FAILED=$((FAILED + 1))
else
  # The position in the real array, not in the filtered one.
  LABELLED_IDX="$(jq -r '
    [ .resource_changes[] | ((.change.before.labels // {})["managed-by"] == "swarm-terraform") ]
    | index(true)' "${PLAN}")"
  LABELLED_ADDR="$(address_at "${LABELLED_IDX}")"
  info "using ${LABELLED_ADDR}"

  # 3a. The label removed everywhere it is spelled.
  mutate "${LABELLED_IDX}" \
    'del(.labels["managed-by"]) | del(.effective_labels["managed-by"]) | del(.terraform_labels["managed-by"])' \
    "${WORK}/unlabelled.json"
  expect_refusal "a real resource with managed-by removed is an offender" \
    "${WORK}/unlabelled.json" "${LABELLED_ADDR}" "do not carry managed-by=swarm-terraform"

  # 3b. A near-miss VALUE. A truthiness or substring check passes this.
  mutate "${LABELLED_IDX}" \
    '.labels["managed-by"] = "other-terraform" | .effective_labels["managed-by"] = "other-terraform" | .terraform_labels["managed-by"] = "other-terraform"' \
    "${WORK}/nearmiss.json"
  expect_refusal "managed-by=other-terraform is an offender" \
    "${WORK}/nearmiss.json" "${LABELLED_ADDR}" "not swarm-terraform"

  # 3c. `labels: {}` with the truth in effective_labels. This is NOT a refusal
  # case: the google provider reports an empty `labels` on an update to a
  # resource whose labels did not change, and an EMPTY OBJECT is truthy in jq,
  # so `{} // .effective_labels` stops at `{}`. That bug refused a legitimate
  # plan on 2026-09-19. Asserted here on a real label map, because a guard that
  # is wrong about ordinary work is one people learn to bypass.
  mutate "${LABELLED_IDX}" '.labels = {}' "${WORK}/emptylabels.json"
  expect_pass "empty .labels with managed-by in .effective_labels still passes" "${WORK}/emptylabels.json"

  # 3d. An unknown resource type fails CLOSED. The carve-out is a list, not a
  # guess: a type nobody has reviewed is an offender until someone adds it.
  jq --argjson i "${LABELLED_IDX}" \
     '.resource_changes[$i].type = "google_pretend_unreviewed_type"
      | .resource_changes[$i].change.before |= (del(.labels) | del(.effective_labels) | del(.terraform_labels))' \
     "${PLAN}" >"${WORK}/unknowntype.json"
  expect_refusal "an unlabelled resource of an unknown type is an offender" \
    "${WORK}/unknowntype.json" "${LABELLED_ADDR}" "google_pretend_unreviewed_type"
fi

# ---------------------------------------------------------------------------
# 4. The carve-out is applied where it should be and nowhere else.
# ---------------------------------------------------------------------------
#
# Read out of the real plan rather than asserted from the list: for every type
# present, "was it exempted" must agree with "does the provider give it a label
# field at all". The one documented exception is
# google_monitoring_alert_policy, whose `user_labels` is the monitoring
# product's own payload rather than a resource label -- and which, in this
# plan, carries managed-by there anyway.
step "4. The unlabelable carve-out matches the real provider output"

CARVE_REPORT="${WORK}/carve.json"
jq --argjson allow "${UNLABELABLE_TYPES}" '
  def label_fields: ["labels","effective_labels","terraform_labels","resource_labels","user_labels"];
  def is_exempt($t): (($allow | index($t)) != null) or ($t | test("_iam_(member|binding|policy)$"));
  [ .resource_changes[]
    | { type: .type,
        has_label_field: ( [ (.change.before // {}) | keys[] ] | any(. as $k | label_fields | index($k) != null) ) } ]
  | group_by(.type)
  | map({ type: .[0].type,
          has_label_field: (map(.has_label_field) | any),
          exempt: is_exempt(.[0].type) })
  | { exempt_but_labelable: [ .[] | select(.exempt and .has_label_field) | .type ],
      labelable_but_not_exempt: [ .[] | select((.exempt | not) and .has_label_field) | .type ],
      no_label_field_and_not_exempt: [ .[] | select((.exempt | not) and (.has_label_field | not)) | .type ],
      types: length }' "${PLAN}" >"${CARVE_REPORT}"

CHECKS=$((CHECKS + 1))
STRAY="$(jq -r '[.exempt_but_labelable[] | select(. != "google_monitoring_alert_policy")] | join(", ")' "${CARVE_REPORT}")"
if [[ -z "${STRAY}" ]]; then
  ok "no exempted type in this plan actually carries a resource label"
else
  err "these types are exempted from the label rule but DO carry a label field: ${STRAY}"
  err "an exemption for a labelable type hides a missing managed-by rather than describing the provider"
  FAILED=$((FAILED + 1))
fi

CHECKS=$((CHECKS + 1))
ORPHAN="$(jq -r '.no_label_field_and_not_exempt | join(", ")' "${CARVE_REPORT}")"
if [[ -z "${ORPHAN}" ]]; then
  ok "every type with no label field in the real plan is on the allow-list or is an IAM edge"
else
  err "these types carry no label field in the real plan and are NOT exempt: ${ORPHAN}"
  err "make destroy will abort on them forever -- a guard that can never pass is a guard that gets deleted"
  FAILED=$((FAILED + 1))
fi

info "$(jq -r '"\(.types) distinct resource type(s) classified"' "${CARVE_REPORT}")"

# ---------------------------------------------------------------------------
# 5. A destroy plan that is not a destroy plan.
# ---------------------------------------------------------------------------
step "5. A create or update inside a destroy plan is refused"
FLIP_IDX="$(jq -r '[.resource_changes[].type] | if length == 0 then "none" else 0 end' "${PLAN}")"
if [[ "${FLIP_IDX}" == "none" ]]; then
  err "no resource_changes to flip"
  FAILED=$((FAILED + 1))
else
  FLIP_ADDR="$(address_at 0)"
  jq '.resource_changes[0].change.actions = ["update"]' "${PLAN}" >"${WORK}/mutation.json"
  expect_refusal "a deletion turned into an update is refused in destroy mode" \
    "${WORK}/mutation.json" "${FLIP_ADDR}" "state and reality disagree"
fi

# ---------------------------------------------------------------------------
# 6. Record a redacted copy for the offline suite.
# ---------------------------------------------------------------------------
#
# WHAT SURVIVES A RECORDING, and why it is drawn this way. Values are kept for
# the fields the guard reads -- every identity token and every label map -- and
# for nothing else; every other key survives BY NAME ONLY, in
# `_recording.before_keys_by_type`, so the recording still states exactly which
# fields the real provider emitted without carrying their contents. That matters
# twice: a Cloud Run service's `template` holds its whole environment, a
# Firestore document's `fields` holds live data, and a monitoring dashboard holds
# 7 KB of JSON -- none of which belongs in a test fixture -- while the KEY NAMES
# are the evidence that the guard reads fields the real format actually has.
#
# The key census is per TYPE (the union over every instance of it), not per
# resource: per-resource it was 240 KB of the 300 KB file, which is how a
# recording stops being reviewable and starts being taken on trust.
#
# tests/integration/test_destroy_guard_real_plan.py reads `_recording.value_fields`
# and fails if the guard grew a read of a field this recording does not carry
# values for. That is the drift this whole exercise is about, turned into a CI
# failure instead of a discovery at teardown.
if [[ -n "${RECORD}" ]]; then
  step "6. Recording a redacted copy of this plan"

  VALUE_FIELDS='["name","id","email","account_id","bucket","cluster","cluster_id","network","subnetwork","repository","secret_id","service","job","database","topic","subscription","member","project","collection","document_id","labels","effective_labels","terraform_labels","resource_labels","user_labels","metadata"]'

  VERDICT_SUMMARY="$(jq -f "${GUARD_JQ}" \
      --argjson deny "${DENY_TOKENS}" \
      --argjson allow_types "${UNLABELABLE_TYPES}" \
      --arg project "${PROJECT_ID}" "${PLAN}" \
    | jq -c '{ deletions, offenders: (.offenders|length), denylist_hits: (.denylist_hits|length),
               wrong_project: (.wrong_project|length),
               unexpected_mutations: (.unexpected_mutations|length),
               unlabelable_allowed: (.unlabelable_allowed|length),
               labelled_ok: (.labelled_ok|length), data_bearing: (.data_bearing|length) }')"

  jq --argjson keep "${VALUE_FIELDS}" \
     --argjson verdict "${VERDICT_SUMMARY}" \
     --arg env "${ENVIRONMENT}" --arg project "${PROJECT_ID}" \
     --arg recorded "$(date -u +%Y-%m-%d)" '
    {
      _recording: {
        what: "terraform show -json of a REAL destroy plan, redacted. NOT a hand-written fixture.",
        generated_by: "scripts/verify-destroy-guard.sh --record <this file>",
        environment: $env, project: $project, recorded: $recorded,
        value_fields: $keep,
        redaction: "Values are kept only for value_fields. Every other before-key survives by NAME in before_keys_by_type; its value is dropped, because it can hold a service environment, live document data or a 7KB dashboard.",
        before_keys_by_type: (
          [ .resource_changes[] | { t: .type, k: ((.change.before // {}) | keys) } ]
          | group_by(.t)
          | map({ key: .[0].t, value: ([ .[].k[] ] | unique) })
          | from_entries ),
        verdict: $verdict
      },
      format_version: .format_version,
      terraform_version: .terraform_version,
      applyable: .applyable, complete: .complete, errored: .errored,
      resource_changes: [ .resource_changes[]
        | { address, mode, type, name, provider_name,
            change: {
              actions: .change.actions,
              after: .change.after,
              after_unknown: .change.after_unknown,
              before: ( (.change.before // {}) | with_entries(select(.key as $k | $keep | index($k) != null)) )
            } } ]
    }' "${PLAN}" >"${WORK}/recording.json"

  # The recording must reach the SAME verdict as the plan it came from, or the
  # redaction changed the judgement and the offline suite is testing a different
  # thing from the one that runs at teardown.
  RECORDED_VERDICT="$(jq -f "${GUARD_JQ}" \
      --argjson deny "${DENY_TOKENS}" \
      --argjson allow_types "${UNLABELABLE_TYPES}" \
      --arg project "${PROJECT_ID}" "${WORK}/recording.json" \
    | jq -c '{ deletions, offenders: (.offenders|length), denylist_hits: (.denylist_hits|length),
               wrong_project: (.wrong_project|length),
               unexpected_mutations: (.unexpected_mutations|length),
               unlabelable_allowed: (.unlabelable_allowed|length),
               labelled_ok: (.labelled_ok|length), data_bearing: (.data_bearing|length) }')"

  CHECKS=$((CHECKS + 1))
  if [[ "${RECORDED_VERDICT}" == "${VERDICT_SUMMARY}" ]]; then
    ok "the recording judges identically to the plan it came from"
  else
    err "redaction changed the verdict:"
    err "  plan:      ${VERDICT_SUMMARY}"
    err "  recording: ${RECORDED_VERDICT}"
    FAILED=$((FAILED + 1))
  fi

  mkdir -p "$(dirname "${RECORD}")"
  jq -S . "${WORK}/recording.json" >"${RECORD}"
  ok "recorded ${RECORD} ($(wc -c <"${RECORD}" | tr -d ' ') bytes)"
fi

# ---------------------------------------------------------------------------
hr
info "${CHECKS} assertion(s) run against a real terraform plan"
if [[ "${FAILED}" -gt 0 ]]; then
  die "${FAILED} of ${CHECKS} FAILED -- the destroy guard does not behave as required on a real plan"
fi
ok "all ${CHECKS} assertions held"
