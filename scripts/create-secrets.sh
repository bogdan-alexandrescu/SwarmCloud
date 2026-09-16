#!/usr/bin/env bash
# Create or rotate a tenant's provider credential in Secret Manager.
#
# Secret MATERIAL is deliberately not managed by Terraform. A Terraform-managed
# secret version puts the plaintext key in the state file, and the state file
# lives in a bucket several teams can read. So Terraform creates nothing here:
# this script owns both the container and its versions, and destroy.sh will
# never see these resources in a destroy plan because they are not in state.
#
# The key is never passed as an argument -- argv is world-readable through ps --
# and is never echoed. It reaches gcloud through a 0600 file in a private temp
# directory that is removed on every exit path.
#
# Usage:
#   scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
#   scripts/create-secrets.sh --tenant eng --provider openai --from-file ./key.txt
#   scripts/create-secrets.sh --tenant eng --provider anthropic --stdin --disable-previous
#   scripts/create-secrets.sh --list [--tenant eng]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TENANT=""
PROVIDER=""
SECRET_NAME=""
FROM_FILE=""
READ_STDIN=0
DISABLE_PREVIOUS=0
LIST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant|-t)        TENANT="$2"; shift 2 ;;
    --provider|-p)      PROVIDER="$2"; shift 2 ;;
    --name)             SECRET_NAME="$2"; shift 2 ;;
    --from-file|-f)     FROM_FILE="$2"; shift 2 ;;
    --stdin)            READ_STDIN=1; shift ;;
    --disable-previous) DISABLE_PREVIOUS=1; shift ;;
    --list|-l)          LIST=1; shift ;;
    -h|--help)          sed -n '2,21p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud

if [[ "${LIST}" -eq 1 ]]; then
  step "Tenant credentials in ${PROJECT_ID}"
  filter="labels.component=tenant-credential"
  [[ -n "${TENANT}" ]] && filter="${filter} AND labels.tenant=${TENANT}"
  gcloud secrets list --project "${PROJECT_ID}" --filter "${filter}" \
    --format='table(name.basename():label=SECRET,labels.tenant:label=TENANT,labels.provider:label=PROVIDER,createTime.date("%Y-%m-%d"):label=CREATED)'
  dim "values are never printed by this tool"
  exit 0
fi

[[ -n "${TENANT}" ]]   || die "--tenant is required (e.g. eng, or u-alice for a personal tenant)"
[[ -n "${PROVIDER}" ]] || die "--provider is required (e.g. anthropic, openai)"

# Must match swarm_common.models.Tenant.secret_name(), which is what the worker
# asks Secret Manager for at runtime. A mismatch here parks every task of that
# tenant as CREDENTIAL_MISSING.
SECRET_NAME="${SECRET_NAME:-swarm-tenant-${TENANT}-${PROVIDER}}"

case "${TENANT}" in
  *[!a-z0-9-]*) die "tenant id ${TENANT} must be lowercase letters, digits and hyphens (see identity.tenant_id_for_group)" ;;
esac

TMPDIR_SECRET="$(mktemp -d "${TMPDIR:-/tmp}/swarm-secret.XXXXXX")"
chmod 0700 "${TMPDIR_SECRET}"
cleanup() {
  if [[ -d "${TMPDIR_SECRET}" ]]; then
    find "${TMPDIR_SECRET}" -type f -exec rm -f {} + 2>/dev/null || true
    rmdir "${TMPDIR_SECRET}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM
KEY_FILE="${TMPDIR_SECRET}/value"

if [[ -n "${FROM_FILE}" ]]; then
  [[ -f "${FROM_FILE}" ]] || die "no such file: ${FROM_FILE}"
  cat "${FROM_FILE}" >"${KEY_FILE}"
elif [[ "${READ_STDIN}" -eq 1 ]]; then
  if [[ -t 0 ]]; then
    printf 'Paste the %s API key for tenant %s (input hidden), then press Enter: ' \
      "${PROVIDER}" "${TENANT}" >&2
    read -rs SECRET_VALUE
    printf '\n' >&2
    printf '%s' "${SECRET_VALUE}" >"${KEY_FILE}"
    unset SECRET_VALUE
  else
    cat >"${KEY_FILE}"
  fi
else
  die "supply the key with --stdin or --from-file; it is never accepted as an argument"
fi
chmod 0600 "${KEY_FILE}"

# Strip a single trailing newline: a pasted key almost always has one, and a key
# with \n appended fails provider auth in a way that looks like a bad key.
if [[ -s "${KEY_FILE}" ]]; then
  printf '%s' "$(cat "${KEY_FILE}")" >"${KEY_FILE}.trimmed"
  mv "${KEY_FILE}.trimmed" "${KEY_FILE}"
  chmod 0600 "${KEY_FILE}"
fi

BYTES="$(wc -c <"${KEY_FILE}" | tr -d ' ')"
[[ "${BYTES}" -gt 0 ]] || die "refusing to store an empty secret"
[[ "${BYTES}" -lt 65536 ]] || die "value is ${BYTES} bytes; that is not an API key"

# Shape check only. Providers change prefixes, so this warns and never blocks.
PREFIX="$(cut -c1-7 <"${KEY_FILE}")"
case "${PROVIDER}:${PREFIX}" in
  anthropic:sk-ant-) ok "value looks like an Anthropic key (${BYTES} bytes)" ;;
  openai:sk-*)       ok "value looks like an OpenAI key (${BYTES} bytes)" ;;
  anthropic:*|openai:*) warn "value does not have the usual ${PROVIDER} prefix; storing it anyway (${BYTES} bytes)" ;;
  *)                 info "stored ${BYTES} bytes for provider ${PROVIDER}" ;;
esac

step "Secret ${SECRET_NAME}"
if gcloud secrets describe "${SECRET_NAME}" --project "${PROJECT_ID}" \
     --format='value(name)' >/dev/null 2>&1; then
  ok "secret exists; adding a new version"
else
  info "creating secret"
  gcloud secrets create "${SECRET_NAME}" \
    --project "${PROJECT_ID}" \
    --replication-policy=user-managed \
    --locations="${REGION}" \
    --labels="managed-by=swarm-secrets,component=tenant-credential,tenant=${TENANT},provider=${PROVIDER},environment=${ENVIRONMENT}" \
    >/dev/null
  ok "created ${SECRET_NAME} (replicated only to ${REGION})"
fi

PREVIOUS="$(gcloud secrets versions list "${SECRET_NAME}" --project "${PROJECT_ID}" \
  --filter='state=ENABLED' --format='value(name)' --limit=50 2>/dev/null || true)"

VERSION="$(gcloud secrets versions add "${SECRET_NAME}" \
  --project "${PROJECT_ID}" --data-file="${KEY_FILE}" --format='value(name)')"
ok "added version ${VERSION##*/}"

if [[ "${DISABLE_PREVIOUS}" -eq 1 && -n "${PREVIOUS}" ]]; then
  step "Disabling superseded versions"
  # Disabled, not destroyed: a rotation that turns out to have stored the wrong
  # key must be reversible, and an in-flight worker may still hold the old one.
  while IFS= read -r old; do
    [[ -n "${old}" ]] || continue
    if gcloud secrets versions disable "${old}" --secret "${SECRET_NAME}" \
         --project "${PROJECT_ID}" >/dev/null 2>&1; then
      ok "disabled version ${old}"
    else
      warn "could not disable version ${old}"
    fi
  done <<<"${PREVIOUS}"
  dim "re-enable with: gcloud secrets versions enable <N> --secret ${SECRET_NAME}"
fi

step "Access"
TENANT_SA="$(gcloud secrets get-iam-policy "${SECRET_NAME}" --project "${PROJECT_ID}" \
  --format='value(bindings.members)' 2>/dev/null | tr ';' '\n' | grep -c 'serviceAccount:' || true)"
if [[ "${TENANT_SA}" -gt 0 ]]; then
  ok "${TENANT_SA} service account binding(s) already present"
else
  warn "no service account can read ${SECRET_NAME} yet"
  dim "run: scripts/register-tenant.sh --tenant ${TENANT} --providers ${PROVIDER}"
fi

hr
ok "credential stored for tenant ${TENANT}, provider ${PROVIDER}"
dim "the plaintext existed only in ${TMPDIR_SECRET}, which is now removed"
