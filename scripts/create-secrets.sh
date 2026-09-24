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
#   scripts/create-secrets.sh --tenant eng --provider anthropic --subscription --stdin
#   scripts/create-secrets.sh --list [--tenant eng]
#
# --subscription stores a Claude SUBSCRIPTION credential (the JSON Claude Code
# keeps in the keychain: accessToken, refreshToken, expiresAt) in
# swarm-tenant-<tenant>-<provider>-refresh. The quota broker then refreshes it
# and republishes the short-lived access token to the secret the worker mounts.
# Without it, the value is written directly as a static credential -- an API key
# or a `claude setup-token` token.
#
# The secret's NAME is never an input. It is always
# swarm-tenant-<tenant>-<provider>, which is what swarm_common.models.Tenant's
# secret_name() returns and what the worker asks Secret Manager for.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TENANT=""
PROVIDER=""
SECRET_NAME=""
SUBSCRIPTION=0
FROM_FILE=""
READ_STDIN=0
DISABLE_PREVIOUS=0
LIST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant|-t)        TENANT="$2"; shift 2 ;;
    --provider|-p)      PROVIDER="$2"; shift 2 ;;
    --from-file|-f)     FROM_FILE="$2"; shift 2 ;;
    --stdin)            READ_STDIN=1; shift ;;
    --disable-previous) DISABLE_PREVIOUS=1; shift ;;
    --list|-l)          LIST=1; shift ;;
    -h|--help)          sed -n '2,22p' "$0"; exit 0 ;;
    # There is deliberately no --name. The secret's name is
    # swarm_common.models.Tenant.secret_name() and nothing else: docs call that
    # spelling the first of three independent mechanisms keeping one tenant's key
    # away from another, and an escape hatch that lets an operator create an
    # arbitrarily-named secret while LABELLING it with an unrelated tenant is a
    # hole in all three at once.
    --subscription) SUBSCRIPTION=1; shift ;;
    --name) die "--name was removed. A tenant's secret is always swarm-tenant-<tenant>-<provider>,
  because that is what swarm_common.models.Tenant.secret_name() returns and what the
  worker asks Secret Manager for. Any other name is a secret nothing will ever read." ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud

# Validated BEFORE anything uses it, --list included.
#
# The --list branch builds a gcloud --filter expression out of ${TENANT} and used
# to run before this check, so a crafted value such as `eng" OR labels.tenant!="`
# rewrote the filter and enumerated every tenant's secret names through a flag
# advertised as tenant-scoped. The charset below leaves nothing to quote with.
if [[ -n "${TENANT}" ]]; then
  case "${TENANT}" in
    *[!a-z0-9-]*) die "tenant id '${TENANT}' must be lowercase letters, digits and hyphens (see identity.tenant_id_for_group)" ;;
  esac
fi
if [[ -n "${PROVIDER}" ]]; then
  case "${PROVIDER}" in
    *[!a-z0-9-]*) die "provider '${PROVIDER}' must be lowercase letters, digits and hyphens" ;;
  esac
fi

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
# tenant as CREDENTIAL_MISSING. Asserted against the frozen model by
# scripts/lib/check-contract-parity.sh.
SECRET_NAME="swarm-tenant-${TENANT}-${PROVIDER}"
if [[ "${SUBSCRIPTION}" -eq 1 ]]; then
  # The LONG-LIVED half of a Claude subscription credential. The quota broker is
  # the only writer of the short-lived half (swarm-tenant-<t>-<p>), which it
  # republishes from this one as the access token nears expiry -- see
  # apps/quota-broker/quota_broker/credentials.py. Writing the access token here
  # by hand would be overwritten on the next sweep.
  #
  # The payload is the JSON Claude Code keeps in the macOS keychain under
  # `Claude Code-credentials`: accessToken, refreshToken, expiresAt.
  SECRET_NAME="${SECRET_NAME}-refresh"
fi

# Belt and braces: a tenant id is never a shared resource, but this script is one
# of the few that names a cloud resource from operator input, and the deny-list
# check costs nothing.
if is_shared_resource "${SECRET_NAME}" || is_shared_resource "${TENANT}"; then
  die "'${SECRET_NAME}' names a resource on the shared deny-list (scripts/lib/common.sh). Refusing."
fi

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

# A subscription credential is a JSON document, not a key, and the shape check
# for it BLOCKS rather than warns. The likeliest mistake is pasting the access
# token on its own: it is accepted by every check that only counts bytes, works
# for a few hours, and then the tenant stops -- with nothing connecting the
# outage to a paste made that morning. The refresh token is the part that
# matters, so its absence is fatal here, where the operator is still watching.
if [[ "${SUBSCRIPTION}" -eq 1 ]]; then
  if ! python3 - "${KEY_FILE}" <<'PYEOF'; then
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit("not JSON")
if isinstance(data, dict) and isinstance(data.get("claudeAiOauth"), dict):
    data = data["claudeAiOauth"]          # the keychain item's own wrapper
if not isinstance(data, dict) or not (data.get("refreshToken") or data.get("refresh_token")):
    sys.exit("no refreshToken")
PYEOF
    die "a --subscription value must be the credential JSON containing refreshToken, not an access token
     macOS: security find-generic-password -s 'Claude Code-credentials' -w"
  fi
  ok "value is a subscription credential with a refresh token (${BYTES} bytes)"
fi

# Shape check only. Providers change prefixes, so this warns and never blocks.
PREFIX="$(cut -c1-7 <"${KEY_FILE}")"
case "${SUBSCRIPTION}:${PROVIDER}:${PREFIX}" in
  1:*) : ;;  # already checked above, and a JSON blob has no key prefix
  *:anthropic:sk-ant-) ok "value looks like an Anthropic key (${BYTES} bytes)" ;;
  *:openai:sk-*)       ok "value looks like an OpenAI key (${BYTES} bytes)" ;;
  *:anthropic:*|*:openai:*) warn "value does not have the usual ${PROVIDER} prefix; storing it anyway (${BYTES} bytes)" ;;
  *)                   info "stored ${BYTES} bytes for provider ${PROVIDER}" ;;
esac

step "Secret ${SECRET_NAME}"
# The credential is already in a file on disk at this point, so a failure here
# that reads as anything other than "log in again" costs the operator the whole
# minting step as well. One expired session produced exactly that: the command
# died partway, stored nothing, and said nothing that pointed at the session.
SECRET_ERR=""
if SECRET_ERR="$(gcloud secrets describe "${SECRET_NAME}" --project "${PROJECT_ID}" \
     --format='value(name)' 2>&1 >/dev/null)"; then
  ok "secret exists; adding a new version"
else
  die_if_auth_failure "${SECRET_ERR}"
  info "creating secret"
  gcloud secrets create "${SECRET_NAME}" \
    --project "${PROJECT_ID}" \
    --replication-policy=user-managed \
    --locations="${REGION}" \
    --labels="managed-by=swarm-secrets,component=tenant-credential,tenant=${TENANT},provider=${PROVIDER},environment=${ENVIRONMENT}" \
    >/dev/null
  ok "created ${SECRET_NAME} (replicated only to ${REGION})"
fi

PREVIOUS=""
previous_err="${TMPDIR_SECRET}/previous-versions.err"
# A denied secretmanager.versions.list, an expired session or a wrong project
# must not read as "this secret has no enabled versions" -- with
# --disable-previous, that reading is exactly the difference between the
# leaked key this rotation exists to kill being disabled and it staying live
# while the operator is told the rotation completed.
if ! PREVIOUS="$(gcloud secrets versions list "${SECRET_NAME}" --project "${PROJECT_ID}" \
     --filter='state=ENABLED' --format='value(name)' --limit=50 2>"${previous_err}")"; then
  if [[ "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die_if_auth_failure "$(cat "${previous_err}")"
    err "gcloud secrets versions list failed for ${SECRET_NAME}; cannot tell which versions are enabled"
    redact <"${previous_err}" | head -n 5 | sed 's/^/     /' >&2
    die "--disable-previous requires knowing the current enabled versions; refusing to silently skip disabling them"
  fi
  PREVIOUS=""
fi

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
policy_err="${TMPDIR_SECRET}/iam-policy.err"
# secretmanager.secrets.getIamPolicy can be denied to an operator who holds
# only secretVersionAdder -- entirely plausible for someone whose job today is
# rotating a key, not managing IAM. That denial must not collapse into "zero
# bindings", or the operator is sent to re-run register-tenant.sh against a
# tenant that was already wired, on a shared project, to answer a question
# this step only failed to ask.
if ! POLICY="$(gcloud secrets get-iam-policy "${SECRET_NAME}" --project "${PROJECT_ID}" \
     --format='value(bindings.members)' 2>"${policy_err}")"; then
  die_if_auth_failure "$(cat "${policy_err}")"
  warn "could not check IAM bindings for ${SECRET_NAME}; this is NOT confirmation no service account can read it"
  redact <"${policy_err}" | head -n 5 | sed 's/^/     /' >&2
else
  TENANT_SA="$(printf '%s' "${POLICY}" | tr ';' '\n' | grep -c 'serviceAccount:' || true)"
  if [[ "${TENANT_SA}" -gt 0 ]]; then
    ok "${TENANT_SA} service account binding(s) already present"
  else
    warn "no service account can read ${SECRET_NAME} yet"
    dim "run: scripts/register-tenant.sh --tenant ${TENANT} --providers ${PROVIDER}"
  fi
fi

hr
ok "credential stored for tenant ${TENANT}, provider ${PROVIDER}"
dim "the plaintext existed only in ${TMPDIR_SECRET}, which is now removed"
