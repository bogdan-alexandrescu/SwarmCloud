#!/usr/bin/env bash
# The deployment's private Terraform values, as one variables file for the plans
# that deploy.
#
# The release and main's plan read the tracked <env>.tfvars, and this
# repository names no real resource and no person, so two values a deployment
# needs can never live there:
#
#   * shared_deny_list. terraform/infra renders swarm-api's
#     SWARM_SHARED_DENY_LIST and SWARM_SHARED_DENY_LIST_SET from it. It was null
#     on every release, so the marker rendered "0" and swarm-api refused every
#     issue run that touches infrastructure (DENY_LIST_UNSET). Its one source is
#     the reviewed config/<env>/deny-list.json, the object the shared-project
#     guard already reads (deny_list_require in common.sh): its entries are
#     written unchanged, so swarm-api and the guard can never disagree.
#   * alert_emails. The alert policies notify nobody while it is empty, and an
#     address names a person. It comes from config/<env>/private.tfvars.json
#     when that file exists.
#
# private.tfvars.json may set ONLY the keys in PRIVATE_KEYS: anything else
# refuses, so the private file can never quietly override a tracked setting.
#
# Usage: scripts/lib/private-tfvars.sh --out FILE
#   with PROJECT_ID and ENVIRONMENT set, as the release job sets them.
#
# Stops, writing nothing, when the never-touch list cannot be read or checked
# (a plan with no list would turn the marker back to "0"), or when
# private.tfvars.json is malformed. Prints counts only, never a value.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The values a deployment keeps out of the repository, beside shared_deny_list.
PRIVATE_KEYS='["alert_emails"]'

OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      [[ $# -ge 2 && -n "$2" ]] || die "--out needs a file"
      OUT="$2"
      shift 2
      ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (usage: private-tfvars.sh --out FILE)" ;;
  esac
done
[[ -n "${OUT}" ]] || die "usage: private-tfvars.sh --out FILE"

deny_list_require
mkdir -p "$(dirname -- "${OUT}")" || die "could not create the directory of ${OUT}"

# deny_list_require fetched config/<env>/ into this cache, private.tfvars.json
# with it when it exists.
private_file="$(config_cache_dir "${ENVIRONMENT}")/private.tfvars.json"
private_json='{}'
if [[ -f "${private_file}" ]]; then
  # shellcheck disable=SC2016  # a jq program: its $names are jq's, not the shell's
  private_json="$(jq -ce --argjson keys "${PRIVATE_KEYS}" '
    if type != "object" then error("it is not a JSON object")
    elif (keys - $keys) != [] then error("it sets keys it may not: \(keys - $keys | join(", "))")
    elif has("alert_emails") and ((.alert_emails | type) != "array"
         or any(.alert_emails[]; type != "string" or length == 0))
      then error("alert_emails is not a list of addresses")
    else . end' "${private_file}" 2>&1)" \
    || die "$(config_source "${ENVIRONMENT}")/private.tfvars.json refused: ${private_json#jq: error (at *): }"
fi

# One part file renamed into place: a reader sees the whole file or none.
part="$(mktemp "${OUT}.part.XXXXXX")" || die "could not create a file beside ${OUT}"
if ! { if [[ "${#SHARED_DENY_LIST[@]}" -gt 0 ]]; then printf '%s\n' "${SHARED_DENY_LIST[@]}"; fi; } \
    | jq -R . | jq -s --argjson private "${private_json}" '$private + {shared_deny_list: .}' >"${part}"; then
  rm -f -- "${part}"
  die "could not write ${OUT}"
fi
mv -f "${part}" "${OUT}"
ok "private values for ${PROJECT_ID} (${ENVIRONMENT}): ${#SHARED_DENY_LIST[@]} never-touch entries, $(jq '.alert_emails // [] | length' "${OUT}") alert address(es) -> ${OUT}"
