#!/usr/bin/env bash
# Read and change the concurrency ceiling of a live pool.
#
# WHY THIS EXISTS
# ---------------
# `pool_limits` in an environment's tfvars is the ceiling a NEW environment is
# born with, and nothing else. terraform/modules/firestore/bootstrap.tf carries
# `ignore_changes = [fields]` on every pool document, deliberately: `active` is
# mutated by the admission transaction on every lease, so an apply that rewrote
# those documents would reset live concurrency counters to zero and instantly
# oversubscribe every pool. Terraform creates them once and then stops having an
# opinion.
#
# The consequence was found the hard way on 2026-09-19: pool_limits was raised
# from 20/10/5 to 40/20/15, committed twice with a carefully argued comment
# beside the values, applied -- and the running platform stayed at 20/10/5. The
# apply moved a terraform OUTPUT and nothing else. Nothing anywhere reported a
# difference, because until now nothing compared the two.
#
# So there are two jobs here and the second matters more than the first:
#
#   --check   compare every live pool against the terraform output and report
#             the difference. This is the part that stops the two drifting
#             silently again.
#   --pool/--sync
#             change a live ceiling, through a Firestore updateMask that names
#             `hard_limit` and nothing else, so `active` is never written.
#
# WHY NOT THE ADMIN API. PUT /v1/admin/limits/... is the supported path and does
# exactly this. The API's ingress is internal-and-cloud-load-balancing, so it is
# unreachable from a workstation -- the same reason scripts/swarm.py talks to
# Firestore in-process. When there is an internal load balancer with IAP in
# front of the API, this script should call it instead, and the updateMask below
# is the behaviour to preserve.
#
# A POOL THAT DOES NOT EXIST IS NOT CREATED HERE. swarm_common.admission treats a
# missing pool document as unlimited, so inventing one with a guessed ceiling is
# worse than refusing. Provisioning creates them; this only ever edits.
#
# Usage:
#   scripts/pool-limit.sh --check
#   scripts/pool-limit.sh --pool global --limit 40
#   scripts/pool-limit.sh --sync

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_cmd jq curl

MODE=""
POOL=""
LIMIT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)  MODE="check"; shift ;;
    --sync)   MODE="sync"; shift ;;
    --pool)   MODE="set"; POOL="$2"; shift 2 ;;
    --limit)  LIMIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,44p' "$0"; exit 0 ;;
    *)        die "unknown argument: $1" ;;
  esac
done
[[ -n "${MODE}" ]] || die "nothing to do: pass --check, --sync, or --pool NAME --limit N"

# --- what the configuration says -------------------------------------------
# From the terraform OUTPUT rather than by parsing tfvars: the output is
# `{ for name, p in local.pools : name => p.hard_limit }`, which is the same
# expression that built the documents at provisioning. Re-deriving pool names in
# shell would be a second restatement of the catalogue, and CLAUDE.md is explicit
# about where those end up.
expected_json() {
  local out
  if ! out="$(tf -chdir="${REPO_ROOT}/terraform/infra" output -json pool_limits 2>&1)"; then
    die_if_auth_failure "${out}"
    die "cannot read the pool_limits terraform output; run 'make tf-plan' once to initialise the root: ${out}"
  fi
  printf '%s' "${out}"
}

# --- what the platform is actually running ---------------------------------
live_json() {
  fs_list_docs pools | jq -sc 'map({key: .name, value: .hard_limit}) | from_entries'
}

report_drift() {
  local expected live
  expected="$(expected_json)"
  live="$(live_json)"

  jq -rn --argjson e "${expected}" --argjson l "${live}" '
    ($e | keys) as $names
    | [ $names[]
        | { name: .,
            expected: $e[.],
            live: ($l[.] // null) }
        | select(.live != .expected) ]
    | if length == 0 then "IN SYNC"
      else (.[] | "  \(.name)  live=\(.live // "MISSING")  tfvars=\(.expected)")
      end'
}

case "${MODE}" in
  check)
    step "Pool ceilings: live vs terraform"
    drift="$(report_drift)"
    if [[ "${drift}" == "IN SYNC" ]]; then
      ok "every live pool matches the terraform output"
      exit 0
    fi
    printf '%s\n' "${drift}" >&2
    hr
    # Deliberately non-zero. A difference here is not cosmetic: the numbers in
    # tfvars are what a reader believes production is running, and they are not.
    err "live pool ceilings differ from the configuration"
    dim "these are not applied by terraform -- see the header of this script"
    dim "run: scripts/pool-limit.sh --sync"
    exit 1
    ;;

  set)
    [[ -n "${LIMIT}" ]] || die "--pool needs --limit"
    [[ "${LIMIT}" =~ ^[0-9]+$ ]] || die "--limit must be a non-negative integer, got: ${LIMIT}"
    current="$(live_json | jq -r --arg p "${POOL}" '.[$p] // "MISSING"')"
    [[ "${current}" != "MISSING" ]] || die "pool ${POOL} does not exist; provisioning creates pools, this only edits them (a missing pool is treated as UNLIMITED by the admission transaction, so inventing one here would be a guess with teeth)"
    step "Pool ${POOL}: ${current} -> ${LIMIT}"
    # updateMask names hard_limit alone, so `active` -- which the admission
    # transaction owns -- is never part of the write.
    fs_patch "pools/${POOL}" "hard_limit" "{\"hard_limit\":{\"integerValue\":\"${LIMIT}\"}}"
    ok "${POOL} hard_limit=${LIMIT}"
    ;;

  sync)
    step "Pool ceilings: sync live to terraform"
    drift="$(report_drift)"
    if [[ "${drift}" == "IN SYNC" ]]; then
      ok "already in sync; nothing to do"
      exit 0
    fi
    printf '%s\n' "${drift}" >&2
    hr
    confirm "This changes the concurrency ceiling of the pools above on the RUNNING platform." "sync-pools"
    expected="$(expected_json)"
    live="$(live_json)"
    # A pool in the configuration but not in Firestore is REPORTED, never
    # created: see the note on missing pools in the header.
    while IFS=$'\t' read -r name limit; do
      [[ -n "${name}" ]] || continue
      if [[ "$(jq -rn --argjson l "${live}" --arg n "${name}" '$l[$n] // "MISSING"')" == "MISSING" ]]; then
        warn "${name}: in the configuration but not in Firestore; not created here"
        continue
      fi
      fs_patch "pools/${name}" "hard_limit" "{\"hard_limit\":{\"integerValue\":\"${limit}\"}}"
      printf '  %-40s -> %s\n' "${name}" "${limit}" >&2
    done < <(jq -rn --argjson e "${expected}" --argjson l "${live}" '
      $e | to_entries[] | select(.value != ($l[.key] // null)) | "\(.key)\t\(.value)"')
    hr
    ok "pools synced to the terraform output"
    ;;
esac
