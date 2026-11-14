#!/usr/bin/env bash
# Writes the values the tracked <env>.tfvars may not hold as one -var-file for
# the deploying plans: shared_deny_list from the reviewed
# config/<env>/deny-list.json (through deny_list_require, the guard's loader),
# and alert_emails from config/<env>/private.tfvars.json when it exists.
# Why: without them swarm-api's SWARM_SHARED_DENY_LIST_SET renders "0"
# (DENY_LIST_UNSET) and the alert policies notify nobody.
#
# Usage: scripts/lib/private-tfvars.sh --out FILE   (PROJECT_ID, ENVIRONMENT set)
#
# private.tfvars.json may set only PRIVATE_KEYS. Prints counts, never a value;
# stops, writing nothing, when the list or the private file cannot be used.

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
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (usage: private-tfvars.sh --out FILE)" ;;
  esac
done
[[ -n "${OUT}" ]] || die "usage: private-tfvars.sh --out FILE"

deny_list_require
mkdir -p "$(dirname -- "${OUT}")" || die "could not create the directory of ${OUT}"

# deny_list_require fetches deny-list.json alone, so private.tfvars.json is
# fetched here, the same way: from this run's configured source, never a copy a
# past run left in the cache (config_fetch_one drops it when the source holds
# none, and says why on anything but "missing").
private_file="$(config_cache_dir "${ENVIRONMENT}")/private.tfvars.json"
private_json='{}'
private_rc=0
config_fetch_one "${ENVIRONMENT}" private.tfvars.json || private_rc=$?
if (( private_rc == 1 )); then
  die "$(config_source "${ENVIRONMENT}")/private.tfvars.json could not be read"
elif (( private_rc == 0 )); then
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
# An empty list writes no line at all (printf with no arguments would write one
# empty line, an entry of ""), and bash 3.2 under `set -u` dies on an empty "${arr[@]}".
if ! {
  if [[ "${#SHARED_DENY_LIST[@]}" -gt 0 ]]; then
    printf '%s\n' "${SHARED_DENY_LIST[@]}"
  fi
} | jq -R . | jq -s --argjson private "${private_json}" '$private + {shared_deny_list: .}' >"${part}"; then
  rm -f -- "${part}"
  die "could not write ${OUT}"
fi
mv -f "${part}" "${OUT}"
ok "private values for ${PROJECT_ID} (${ENVIRONMENT}): ${#SHARED_DENY_LIST[@]} never-touch entries, $(jq '.alert_emails // [] | length' "${OUT}") alert address(es) -> ${OUT}"
