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
#   scripts/create-secrets.sh --child-key
#   scripts/create-secrets.sh --live-key
#   scripts/create-secrets.sh --github-app client-secret --stdin
#   scripts/create-secrets.sh --github-app private-key --stdin < app.private-key.pem
#   scripts/create-secrets.sh --search-api-key --stdin
#   scripts/create-secrets.sh --egress-key [--overwrite swarm-egress-key]
#   scripts/create-secrets.sh --egress-credential --stdin [--overwrite swarm-egress-credential] < credential.json
#   scripts/create-secrets.sh --tailscale-oauth --stdin
#   scripts/create-secrets.sh --typesafe-fwd-key [--overwrite swarm-typesafe-fwd-key]
#   scripts/create-secrets.sh --workspace-grant-key [--overwrite swarm-workspace-grant-key]
#   scripts/create-secrets.sh --graph-ticket-key [--overwrite swarm-graph-ticket-key]
#   scripts/create-secrets.sh --github-app-webhook-secret --stdin
#   scripts/create-secrets.sh --settings-mac-key [--overwrite swarm-settings-key]
#
# The forge token (`--provider git`, stored as swarm-tenant-<tenant>-git) is
# read by the tenant's workers AND by swarm-api, which writes an issue run's
# plan and status comments and its pull request's keyword with it (#454). On
# the repositories the tenant works in it needs contents and pull_requests
# write, issues write, checks read and actions read (a classic token's `repo`
# scope carries all of them): docs/multi-tenancy.md, "What the forge
# credential must be allowed". Store it with --stdin, so it never touches a file:
#   scripts/create-secrets.sh --tenant eng --provider git --stdin
#
# --child-key adds a version to `swarm-child-key`, the PLATFORM key the
# scheduler mints child-task registration nonces with and swarm-api verifies
# and attests them with (docs/design/child-tasks.md §3.2). Its value is
# GENERATED here -- 48 random bytes from python3's `secrets` -- written to the
# same private 0600 file, added as a version and removed; nobody ever sees it,
# so there is nothing to paste and nothing to leak from a clipboard. The
# secret itself and its two accessor bindings (swarm-scheduler, swarm-api,
# nobody else) are terraform's (terraform/infra/child_tasks.tf); this script
# never creates the container, so a typo cannot make an unbound twin.
#
# --live-key adds a version to `swarm-live-key`, the PLATFORM key swarm-api
# mints live-browser tunnel and viewer tickets with and the swarm-live relay
# verifies them with (docs/design/live-browser.md §3.3). Generated and added
# exactly as --child-key's is, never shown. The secret and its readers
# (swarm-api, and the relay's Workload Identity principal once
# enable_live_browser is applied) are terraform's (terraform/infra/live.tf).
# A rotation refuses every ticket minted under the old version, which ends
# every open live session; both ends reconnect with fresh tickets.
#
# --subscription stores a Claude SUBSCRIPTION credential (the JSON Claude Code
# keeps in the keychain: accessToken, refreshToken, expiresAt) in
# swarm-tenant-<tenant>-<provider>-refresh. The quota broker then refreshes it
# and republishes the short-lived access token to the secret the worker mounts.
# Without it, the value is written directly as a static credential -- an API key
# or a `claude setup-token` token.
#
# --github-app <slot> adds a version to one of the SwarmCloud GitHub App's two
# PLATFORM secrets (docs/runbooks/github-app.md): `client-secret` ->
# swarm-github-app-client-secret, `private-key` -> swarm-github-app-private-key.
# The value is read from stdin ONLY -- never a file argument, a tfvars value or
# an environment variable, because the repository is public and a path on a
# command line is one shell-history line from being committed. Terraform owns
# both slots and their one reader, swarm-api (modules/secret_manager); this
# never creates one, so a typo cannot make an unbound twin.
#
# --search-api-key adds a version to `swarm-search-api-key`, the PLATFORM key of
# the web search provider swarm-api calls for the agents' `search` tool
# (docs/search-tool.md; Brave Search API first). Read from stdin ONLY, for the
# reasons --github-app gives. Its one reader is swarm-api: this creates the
# secret if it is missing (replicated only to the region, like a tenant's) and
# grants swarm-api, and nobody else, secretAccessor on it. Terraform holds no
# copy of it, for the reason at the top of this file. swarm-api reads it only
# while SEARCH_TOOL_ENABLED is on, so storing it changes nothing by itself.
#
# --typesafe-fwd-key adds a version to `swarm-typesafe-fwd-key`, the PLATFORM
# key swarm-api mints attempt TypeSafe tickets with and the TypeSafe forwarder
# verifies them with (docs/design/workspace-secrets.md §3.5). Generated and
# added exactly as --egress-key's is, never read and never shown; terraform
# (terraform/infra/typesafe_fwd.tf) owns the container and its two readers,
# and an enabled version is replaced only after the same typed confirmation.
#
# --graph-ticket-key adds a version to `swarm-graph-ticket-key`, the PLATFORM
# key swarm-api mints attempt graph tickets with and the shared graph service
# (swarm-graph) verifies them with (docs/design/graph-service.md §3.2).
# Generated and added exactly as --typesafe-fwd-key's is, never read and never
# shown; terraform (terraform/infra/graph_service.tf, enable_graph_service)
# owns the container and its two readers, and an enabled version is replaced
# only after the same typed confirmation. Add it after that apply and before
# graph_service.serve: swarm-api's reference to a versionless secret would
# stop it starting.
#
# --github-app-webhook-secret adds a version to
# `swarm-github-app-webhook-secret`, the GitHub App's WEBHOOK secret: the HMAC
# key GitHub signs every delivery's X-Hub-Signature-256 with and the public
# receiver swarm-github-hooks verifies it with (docs/design/graph-service.md
# §6.2; docs/runbooks/github-app.md, "Step 9"). The value is GitHub's: the
# owner types it into the App's settings and here, so it is NEVER generated,
# and it is read from stdin ONLY, for the reasons --github-app gives. Inner
# characters are kept exactly (GitHub signs with the secret as typed); only
# the paste's trailing newline is dropped. Terraform owns the container and
# its one reader, the account the receiver runs as
# (terraform/infra/github_hooks.tf, enable_github_hooks); this never creates
# it, so a typo cannot make an unbound twin. The receiver re-reads `latest` on
# a signature mismatch at most once a minute, so a rotation is: change it at
# GitHub, store it here, and the next delivery after a minute verifies.
#
# --workspace-grant-key adds a version to `swarm-workspace-grant-key`, the
# PLATFORM key swarm-api MACs every workspace-secret grant with and verifies
# it with at release (docs/design/workspace-secrets.md, SEC-GRANT-ATTEST;
# docs/runbooks/workspace-grant-attestation.md). Generated and added exactly
# as --typesafe-fwd-key's is, never read and never shown; terraform
# (terraform/infra/workspace_grant_key.tf) owns the container and its one
# reader, swarm-api, and an enabled version is replaced only after the same
# typed confirmation. A rotation is the runbook's: the replaced version goes to
# workspace_grant_attestation.previous_version (the tfvars object
# terraform/infra/workspace_grant_key.tf declares) until the attest route has
# re-signed every grant under the new one.
#
# --egress-key adds a version to `swarm-egress-key`, the PLATFORM key swarm-api
# mints browser egress tickets with and swarm-egress verifies them with
# (docs/browser-egress.md §9). Generated and added exactly as --live-key's is,
# never read and never shown. --egress-credential adds a version to
# `swarm-egress-credential`, the Bright Data credential only swarm-egress reads:
# the JSON object apps/swarm-egress/swarm_egress/vendor.py parses (`vendor`,
# `host`, `port`, `tls`, `zones`), from --stdin ONLY -- never an argument, a
# file argument, a tfvars value or an environment variable, for the reasons
# --github-app gives. Terraform owns both containers and their readers
# (terraform/infra/egress.tf); this never creates either, so a typo cannot make
# an unbound twin.
#
# Both already hold a version (2026-10-14), and swarm-egress reads `latest`, so
# a new version REPLACES what it uses. Adding one to a secret that has an
# enabled version needs a TYPED confirmation: the secret's own name, given as
# `--overwrite <name>` or typed at the terminal (/dev/tty) when there is one.
# SWARM_ASSUME_YES does not stand in for it. Nothing is read from stdin until
# the confirmation is given. swarm-egress re-reads both on a timer
# (swarm_egress.main SECRET_POLL_SECONDS), so a rotation needs no restart of
# it; swarm-api resolves the key reference when an instance starts. Disable the
# old version by hand once both run on the new one.
# --tailscale-oauth adds a version to `swarm-tailscale-oauth`, the PLATFORM
# secret of the Tailscale OAuth client (tag:swarmcloud) the decision-api
# tailnet egress logs in with (docs/runbooks/tailscale-egress.md; lane
# JUDGE-NET). Read from stdin ONLY, for the reasons --github-app gives, and
# refused unless it is shaped like an OAuth client secret (`tskey-client-`),
# without printing what it was. Terraform owns the container and its one
# reader (terraform/infra/tailscale.tf); this never creates it, so a typo
# cannot make an unbound twin. `kubernetes/apply.sh --swarm-system --confirm`
# copies it into the cluster, so storing it changes nothing by itself.
#
# --settings-mac-key adds a version to `swarm-settings-key`, the PLATFORM key
# swarm-api MACs every `workspace_settings` block it writes with, and swarm-api
# and the scheduler verify it with before acting on one (#1457;
# docs/design/tenant-settings-integrity.md §5 request B). Generated and added
# exactly as --typesafe-fwd-key's is: written to a private 0600 file, added as
# a version and removed, never read back, never shown and never accepted on
# the command line. Its container and its two readers (swarm-api, the
# scheduler, and nobody a tenant runs as) are terraform's; this never creates
# it, so a typo cannot make an unbound twin. A NEW version invalidates every
# stored MAC until scripts/resign-workspace-settings.sh --apply re-signs them,
# so replacing an enabled one needs the same typed confirmation; keep the
# replaced version as SWARM_SETTINGS_KEY_PREVIOUS on both readers until the
# re-sign has run.
#
# There is no --git-wf. The `-git-wf` slot (a person's workflow-scoped token
# for the platform tenant) was removed on 2026-10-22 by BGD's decision (#2029,
# WF-APP-TOKEN): swarm-api now pushes a change under .github/workflows/ or
# .github/actions/ with a token the SwarmCloud GitHub App mints per push, for
# one repository whose registration has `workflow_files` switched on
# (docs/design/agent-autonomy.md §3.3, docs/runbooks/github-app.md). Nothing
# is stored for it, so there is nothing to store here: `--git-wf` is an
# unknown argument and `--provider git-wf` is refused.
#
# A tenant credential's NAME is never an input. It is always
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
CHILD_KEY=0
LIVE_KEY=0
GITHUB_APP_SLOT=""
SEARCH_API_KEY=0
EGRESS_KEY=0
EGRESS_CREDENTIAL=0
TYPESAFE_FWD_KEY=0
WORKSPACE_GRANT_KEY=0
GRAPH_TICKET_KEY=0
GITHUB_APP_WEBHOOK_SECRET=0
SETTINGS_MAC_KEY=0
OVERWRITE=""
TAILSCALE_OAUTH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant|-t)        TENANT="$2"; shift 2 ;;
    --provider|-p)      PROVIDER="$2"; shift 2 ;;
    --from-file|-f)     FROM_FILE="$2"; shift 2 ;;
    --stdin)            READ_STDIN=1; shift ;;
    --disable-previous) DISABLE_PREVIOUS=1; shift ;;
    --list|-l)          LIST=1; shift ;;
    --child-key)        CHILD_KEY=1; shift ;;
    --live-key)         LIVE_KEY=1; shift ;;
    --github-app)       [[ $# -ge 2 ]] || die "--github-app needs a slot: client-secret or private-key"
                        GITHUB_APP_SLOT="$2"; shift 2 ;;
    --search-api-key)   SEARCH_API_KEY=1; shift ;;
    --egress-key)       EGRESS_KEY=1; shift ;;
    --egress-credential) EGRESS_CREDENTIAL=1; shift ;;
    --typesafe-fwd-key) TYPESAFE_FWD_KEY=1; shift ;;
    --workspace-grant-key) WORKSPACE_GRANT_KEY=1; shift ;;
    --graph-ticket-key) GRAPH_TICKET_KEY=1; shift ;;
    --github-app-webhook-secret) GITHUB_APP_WEBHOOK_SECRET=1; shift ;;
    --settings-mac-key) SETTINGS_MAC_KEY=1; shift ;;
    --overwrite)        [[ $# -ge 2 ]] || die "--overwrite takes the name of the secret to add a version to"
                        OVERWRITE="$2"; shift 2 ;;
    # A value glued to a flag is a value on the command line, which argv
    # exposes through ps and shell history. Refused without repeating it.
    --egress-key=*|--egress-credential=*|--typesafe-fwd-key=*|--workspace-grant-key=*|--graph-ticket-key=*|--github-app-webhook-secret=*|--settings-mac-key=*)
      die "${1%%=*} takes no value on the command line: the key is generated, and the credential is read from --stdin only. Nothing was stored." ;;
    --tailscale-oauth)  TAILSCALE_OAUTH=1; shift ;;
    # The removed slot's flag: unknown, and a glued value is never repeated.
    --git-wf|--git-wf=*) die "unknown argument: --git-wf. The -git-wf slot no longer exists: a workflow-file push uses a token the SwarmCloud GitHub App mints per push, for a repository whose workflow_files switch is on (docs/runbooks/github-app.md). Nothing was stored." ;;
    # The header above `set -euo pipefail`, however long it grows; a fixed
    # line range stops mid-sentence the first time the header changes.
    -h|--help)          awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0"; exit 0 ;;
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
    -*) die "unknown argument: $1" ;;
    # Not a flag, so possibly a value someone meant to store: never repeated.
    *) die "unexpected value on the command line (not shown). Values are read from --stdin or --from-file, never from arguments; nothing was stored." ;;
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

# Adds a generated version to a PLATFORM key terraform created: 48 random bytes
# from python3's `secrets`, through a private 0600 file that is removed on every
# exit path, never printed. $1 the secret's name, $2 the terraform file that creates
# it and its readers, $3 this script's flag for it.
add_generated_platform_key() {
  local key_name="$1" tf_file="$2" flag="$3"
  require_cmd python3
  step "Secret ${key_name}"
  local describe_err=""
  if ! describe_err="$(gcloud secrets describe "${key_name}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${describe_err}"
    die "${key_name} does not exist in ${PROJECT_ID}. terraform creates it with its accessor bindings (${tf_file}): run 'make apply' first, then ${flag}"
  fi
  KEY_TMP="$(mktemp -d "${TMPDIR:-/tmp}/swarm-secret.XXXXXX")"
  chmod 0700 "${KEY_TMP}"
  # shellcheck disable=SC2064  # expand now: the path is fixed for this run
  trap "find '${KEY_TMP}' -type f -exec rm -f {} + 2>/dev/null || true; rmdir '${KEY_TMP}' 2>/dev/null || true" EXIT INT TERM
  local key_file="${KEY_TMP}/value"
  ( umask 077; python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(48))' >"${key_file}" )
  [[ -s "${key_file}" ]] || die "could not generate a key"
  local added
  added="$(gcloud secrets versions add "${key_name}" \
    --project "${PROJECT_ID}" --data-file="${key_file}" --format='value(name)')"
  ok "added version ${added##*/} of ${key_name}; its value was never printed"
}

# confirm_overwrite NAME -- return when NAME has no enabled version, or when the
# operator has typed NAME (--overwrite NAME, or at /dev/tty); die otherwise.
# Called before any value is read or generated. Not common.sh's confirm():
# that honours SWARM_ASSUME_YES and reads stdin, which here carries the value.
confirm_overwrite() {
  local name="$1" listed answer
  listed="$(mktemp "${TMPDIR:-/tmp}/swarm-versions.XXXXXX")"
  if ! gcloud secrets versions list "${name}" --project "${PROJECT_ID}" \
       --filter='state=ENABLED' --format='value(name)' --limit=50 >"${listed}" 2>"${listed}.err"; then
    die_if_auth_failure "$(cat "${listed}.err")"
    redact <"${listed}.err" | head -n 3 | sed 's/^/     /' >&2
    rm -f "${listed}" "${listed}.err"
    die "could not list ${name}'s versions, so whether this replaces one cannot be told; nothing was stored"
  fi
  if [[ ! -s "${listed}" ]]; then
    rm -f "${listed}" "${listed}.err"
    return 0
  fi
  rm -f "${listed}" "${listed}.err"
  if [[ -n "${OVERWRITE}" ]]; then
    [[ "${OVERWRITE}" == "${name}" ]] \
      || die "--overwrite names '${OVERWRITE}', not ${name}; nothing was stored"
    warn "${name} has an enabled version; adding a new one, which its reader takes as latest (--overwrite ${name})"
    return 0
  fi
  if { exec 3</dev/tty; } 2>/dev/null; then
    printf '%s already has an enabled version, and its reader takes the latest.\n' "${name}" >/dev/tty
    printf 'Type %s to add a new one: ' "${name}" >/dev/tty
    read -r answer <&3 || answer=""
    exec 3<&-
    [[ "${answer}" == "${name}" ]] || die "confirmation did not match; nothing was stored"
    return 0
  fi
  die "${name} already has an enabled version and its reader takes the latest. Confirm by typing its name:
     --overwrite ${name}   (SWARM_ASSUME_YES is not a confirmation). Nothing was stored."
}

if [[ "${SETTINGS_MAC_KEY}" -eq 1 ]]; then
  # The secret's name is fixed: swarm_api.judgment.SETTINGS_KEY_SECRET_ID reads
  # it; tests/unit/scripts/test_create_secrets_settings_mac_key.py holds the
  # two spellings equal.
  SETTINGS_KEY_NAME="swarm-settings-key"
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}${GITHUB_APP_SLOT}" || "${READ_STDIN}" -eq 1 || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 || "${EGRESS_KEY}" -eq 1 || "${EGRESS_CREDENTIAL}" -eq 1 || "${TAILSCALE_OAUTH}" -eq 1 || "${SEARCH_API_KEY}" -eq 1 || "${TYPESAFE_FWD_KEY}" -eq 1 || "${WORKSPACE_GRANT_KEY}" -eq 1 || "${GRAPH_TICKET_KEY}" -eq 1 || "${GITHUB_APP_WEBHOOK_SECRET}" -eq 1 || "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--settings-mac-key takes no --stdin, --tenant, --provider, --from-file, --subscription, other key or --disable-previous: the key is the platform's and is generated here, and an old version is disabled by hand once every stored MAC is re-signed under the new one"
  fi
  # Exists first: terraform creates it with its two readers.
  SETTINGS_KEY_ERR=""
  if ! SETTINGS_KEY_ERR="$(gcloud secrets describe "${SETTINGS_KEY_NAME}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${SETTINGS_KEY_ERR}"
    die "${SETTINGS_KEY_NAME} does not exist in ${PROJECT_ID}. terraform creates it with its two readers, swarm-api and the scheduler (the ISO-SETTINGS-MAC question on #1457): apply that first, then this"
  fi
  confirm_overwrite "${SETTINGS_KEY_NAME}"
  add_generated_platform_key "${SETTINGS_KEY_NAME}" "terraform (the ISO-SETTINGS-MAC question on #1457)" "--settings-mac-key"
  dim "swarm-api and the scheduler read it as SWARM_SETTINGS_KEY when an instance starts. On a rotation, give both the replaced version as SWARM_SETTINGS_KEY_PREVIOUS, then run scripts/resign-workspace-settings.sh --apply, then disable the old version by hand"
  exit 0
fi

if [[ "${GITHUB_APP_WEBHOOK_SECRET}" -eq 1 ]]; then
  # The secret's name is fixed: terraform/infra/github_hooks.tf creates it and
  # swarm_github_hooks.main.SECRET_ID reads it;
  # tests/unit/hooks/test_hooks_create_secret.py holds the three spellings equal.
  WEBHOOK_SECRET_NAME="swarm-github-app-webhook-secret"
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}${GITHUB_APP_SLOT}${OVERWRITE}" || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 || "${EGRESS_KEY}" -eq 1 || "${EGRESS_CREDENTIAL}" -eq 1 || "${TAILSCALE_OAUTH}" -eq 1 || "${SEARCH_API_KEY}" -eq 1 || "${TYPESAFE_FWD_KEY}" -eq 1 || "${WORKSPACE_GRANT_KEY}" -eq 1 || "${GRAPH_TICKET_KEY}" -eq 1 || "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--github-app-webhook-secret takes only --stdin: the secret is the platform's App's, not a tenant's, a file argument is a path to a plaintext copy, and an old version is disabled by hand once GitHub signs with the new one"
  fi
  [[ "${READ_STDIN}" -eq 1 ]] || die "--github-app-webhook-secret reads the value from --stdin and nothing else"

  step "Secret ${WEBHOOK_SECRET_NAME}"
  WH_DESCRIBE_ERR=""
  if ! WH_DESCRIBE_ERR="$(gcloud secrets describe "${WEBHOOK_SECRET_NAME}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${WH_DESCRIBE_ERR}"
    die "${WEBHOOK_SECRET_NAME} does not exist in ${PROJECT_ID}. terraform creates it with its one reader (terraform/infra/github_hooks.tf, enable_github_hooks = true): apply first, then --github-app-webhook-secret"
  fi

  WH_TMP="$(mktemp -d "${TMPDIR:-/tmp}/swarm-secret.XXXXXX")"
  chmod 0700 "${WH_TMP}"
  # shellcheck disable=SC2064  # expand now: the path is fixed for this run
  trap "find '${WH_TMP}' -type f -exec rm -f {} + 2>/dev/null || true; rmdir '${WH_TMP}' 2>/dev/null || true" EXIT INT TERM
  WH_FILE="${WH_TMP}/value"
  ( umask 077; : >"${WH_FILE}" )
  if [[ -t 0 ]]; then
    printf 'Paste the GitHub App webhook secret (input hidden), then press Enter: ' >&2
    IFS= read -rs WH_VALUE
    printf '\n' >&2
    printf '%s' "${WH_VALUE}" >"${WH_FILE}"
    unset WH_VALUE
  else
    cat >"${WH_FILE}"
  fi
  # The paste's trailing newline off, and nothing else: GitHub signs with the
  # secret exactly as typed, so a changed inner character fails every delivery.
  if [[ -s "${WH_FILE}" ]]; then
    printf '%s' "$(cat "${WH_FILE}")" >"${WH_FILE}.trimmed"
    mv "${WH_FILE}.trimmed" "${WH_FILE}"
    chmod 0600 "${WH_FILE}"
  fi
  WH_BYTES="$(wc -c <"${WH_FILE}" | tr -d ' ')"
  [[ "${WH_BYTES}" -gt 0 ]] || die "refusing to store an empty secret"
  [[ "${WH_BYTES}" -lt 1024 ]] || die "value is ${WH_BYTES} bytes; that is not a webhook secret. Nothing was stored"
  if grep -q -- '-----BEGIN' "${WH_FILE}"; then
    die "that is a PEM block, not a webhook secret; the App's private key goes to --github-app private-key. Nothing was stored"
  fi
  info "stored value is ${WH_BYTES} bytes"

  WH_VERSION="$(gcloud secrets versions add "${WEBHOOK_SECRET_NAME}" \
    --project "${PROJECT_ID}" --data-file="${WH_FILE}" --format='value(name)')"
  ok "added version ${WH_VERSION##*/} of ${WEBHOOK_SECRET_NAME}; its value was never printed"
  dim "its one reader is the account swarm-github-hooks runs as (terraform/infra/github_hooks.tf)"
  dim "the receiver reads it within a minute; next: tick Webhook -> Active in the App (docs/runbooks/github-app.md, Step 9)"
  dim "rotating: change it at GitHub, store it here, then disable the old version by hand"
  exit 0
fi

if [[ "${GRAPH_TICKET_KEY}" -eq 1 ]]; then
  GRAPH_KEY_NAME="swarm-graph-ticket-key"
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}${GITHUB_APP_SLOT}" || "${READ_STDIN}" -eq 1 || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 || "${EGRESS_KEY}" -eq 1 || "${EGRESS_CREDENTIAL}" -eq 1 || "${TAILSCALE_OAUTH}" -eq 1 || "${SEARCH_API_KEY}" -eq 1 || "${TYPESAFE_FWD_KEY}" -eq 1 || "${WORKSPACE_GRANT_KEY}" -eq 1 || "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--graph-ticket-key takes no --stdin, --tenant, --provider, --from-file, --subscription, other key or --disable-previous: the key is the platform's and is generated here, and an old version is disabled by hand once swarm-api and swarm-graph run on the new one"
  fi
  # Exists first: terraform/infra/graph_service.tf creates it with its readers.
  GRAPH_KEY_ERR=""
  if ! GRAPH_KEY_ERR="$(gcloud secrets describe "${GRAPH_KEY_NAME}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${GRAPH_KEY_ERR}"
    die "${GRAPH_KEY_NAME} does not exist in ${PROJECT_ID}. terraform creates it with its readers (terraform/infra/graph_service.tf, enable_graph_service): apply that first, then this"
  fi
  confirm_overwrite "${GRAPH_KEY_NAME}"
  add_generated_platform_key "${GRAPH_KEY_NAME}" "terraform/infra/graph_service.tf" "--graph-ticket-key"
  dim "swarm-api reads it when an instance starts; swarm-graph re-reads it on its own timer and keeps the previous key for a ticket's life. Disable the old version by hand once both use the new one"
  exit 0
fi

if [[ "${WORKSPACE_GRANT_KEY}" -eq 1 ]]; then
  GRANT_KEY_NAME="swarm-workspace-grant-key"
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}${GITHUB_APP_SLOT}" || "${READ_STDIN}" -eq 1 || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 || "${EGRESS_KEY}" -eq 1 || "${EGRESS_CREDENTIAL}" -eq 1 || "${TAILSCALE_OAUTH}" -eq 1 || "${SEARCH_API_KEY}" -eq 1 || "${TYPESAFE_FWD_KEY}" -eq 1 || "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--workspace-grant-key takes no --stdin, --tenant, --provider, --from-file, --subscription, other key or --disable-previous: the key is the platform's and is generated here, and an old version is disabled by hand once every grant is re-signed under the new one (docs/runbooks/workspace-grant-attestation.md)"
  fi
  # Exists first: terraform/infra/workspace_grant_key.tf creates it with its reader.
  GRANT_KEY_ERR=""
  if ! GRANT_KEY_ERR="$(gcloud secrets describe "${GRANT_KEY_NAME}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${GRANT_KEY_ERR}"
    die "${GRANT_KEY_NAME} does not exist in ${PROJECT_ID}. terraform creates it with its reader (terraform/infra/workspace_grant_key.tf): run 'make apply' first, then this"
  fi
  confirm_overwrite "${GRANT_KEY_NAME}"
  add_generated_platform_key "${GRANT_KEY_NAME}" "terraform/infra/workspace_grant_key.tf" "--workspace-grant-key"
  dim "swarm-api reads it when an instance starts. On a rotation, set workspace_grant_attestation.previous_version in terraform/environments/<env>/<env>.tfvars to the replaced version, apply, run the attest route, then clear it (docs/runbooks/workspace-grant-attestation.md)"
  exit 0
fi

if [[ "${TYPESAFE_FWD_KEY}" -eq 1 ]]; then
  TS_FWD_NAME="swarm-typesafe-fwd-key"
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}${GITHUB_APP_SLOT}" || "${READ_STDIN}" -eq 1 || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 || "${EGRESS_KEY}" -eq 1 || "${EGRESS_CREDENTIAL}" -eq 1 || "${TAILSCALE_OAUTH}" -eq 1 || "${SEARCH_API_KEY}" -eq 1 || "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--typesafe-fwd-key takes no --stdin, --tenant, --provider, --from-file, --subscription, other key or --disable-previous: the key is the platform's and is generated here, and an old version is disabled by hand once swarm-api and the forwarder run on the new one"
  fi
  # Exists first: terraform/infra/typesafe_fwd.tf creates it with its readers.
  TS_FWD_ERR=""
  if ! TS_FWD_ERR="$(gcloud secrets describe "${TS_FWD_NAME}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${TS_FWD_ERR}"
    die "${TS_FWD_NAME} does not exist in ${PROJECT_ID}. terraform creates it with its readers (terraform/infra/typesafe_fwd.tf, enable_typesafe_forwarder): apply that first, then this"
  fi
  confirm_overwrite "${TS_FWD_NAME}"
  add_generated_platform_key "${TS_FWD_NAME}" "terraform/infra/typesafe_fwd.tf" "--typesafe-fwd-key"
  dim "swarm-api reads it when an instance starts; the forwarder keeps the previous key for a ticket's life. Disable the old version by hand once both use the new one"
  exit 0
fi

if [[ "${EGRESS_KEY}" -eq 1 || "${EGRESS_CREDENTIAL}" -eq 1 ]]; then
  if [[ "${EGRESS_KEY}" -eq 1 && "${EGRESS_CREDENTIAL}" -eq 1 ]]; then
    die "--egress-key and --egress-credential are two secrets; add one per run"
  fi
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}${GITHUB_APP_SLOT}" || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 || "${TAILSCALE_OAUTH}" -eq 1 || "${SEARCH_API_KEY}" -eq 1 || "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--egress-key and --egress-credential take no --tenant, --provider, --from-file, --subscription, other key or --disable-previous: both are the platform's, a file argument is a path to a plaintext copy, and an old version is disabled by hand once swarm-egress runs on the new one"
  fi
  if [[ "${EGRESS_KEY}" -eq 1 ]]; then
    EGRESS_NAME="swarm-egress-key"
    [[ "${READ_STDIN}" -eq 0 ]] || die "--egress-key takes no --stdin: the key is generated here and never shown"
  else
    EGRESS_NAME="swarm-egress-credential"
    [[ "${READ_STDIN}" -eq 1 ]] || die "--egress-credential reads the value from --stdin and nothing else"
  fi
  # Exists first: terraform/infra/egress.tf creates both with their readers.
  EGRESS_ERR=""
  if ! EGRESS_ERR="$(gcloud secrets describe "${EGRESS_NAME}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${EGRESS_ERR}"
    die "${EGRESS_NAME} does not exist in ${PROJECT_ID}. terraform creates it with its readers (terraform/infra/egress.tf): run 'make apply' first, then this"
  fi
  confirm_overwrite "${EGRESS_NAME}"

  if [[ "${EGRESS_KEY}" -eq 1 ]]; then
    add_generated_platform_key "${EGRESS_NAME}" "terraform/infra/egress.tf" "--egress-key"
    dim "swarm-egress re-reads it on its own timer; swarm-api reads it when an instance starts. Disable the old version by hand once both use the new one"
    exit 0
  fi

  step "Secret ${EGRESS_NAME}"
  EGRESS_TMP="$(mktemp -d "${TMPDIR:-/tmp}/swarm-secret.XXXXXX")"
  chmod 0700 "${EGRESS_TMP}"
  # shellcheck disable=SC2064  # expand now: the path is fixed for this run
  trap "find '${EGRESS_TMP}' -type f -exec rm -f {} + 2>/dev/null || true; rmdir '${EGRESS_TMP}' 2>/dev/null || true" EXIT INT TERM
  EGRESS_FILE="${EGRESS_TMP}/value"
  ( umask 077; : >"${EGRESS_FILE}" )
  if [[ -t 0 ]]; then
    die "the credential is a JSON document: redirect it into stdin (--stdin < <file>), then delete the file"
  fi
  cat >"${EGRESS_FILE}"
  if [[ -s "${EGRESS_FILE}" ]]; then
    printf '%s' "$(cat "${EGRESS_FILE}")" >"${EGRESS_FILE}.trimmed"
    mv "${EGRESS_FILE}.trimmed" "${EGRESS_FILE}"
    chmod 0600 "${EGRESS_FILE}"
  fi
  EGRESS_BYTES="$(wc -c <"${EGRESS_FILE}" | tr -d ' ')"
  [[ "${EGRESS_BYTES}" -gt 0 ]] || die "refusing to store an empty secret"
  [[ "${EGRESS_BYTES}" -lt 65536 ]] || die "value is ${EGRESS_BYTES} bytes; that is not a vendor credential. Nothing was stored"
  # The shape BLOCKS: swarm-egress refuses a value vendor.parse_credential
  # cannot read and fails closed, which would show up only when the first
  # proxied attempt does. This is the outline of that parser (the top-level
  # fields and a non-empty `zones` object), not a copy of it; its answer names
  # the problem and never a value.
  require_cmd python3
  if ! python3 - "${EGRESS_FILE}" <<'PYEOF2'; then
import json, sys
try:
    body = json.load(open(sys.argv[1]))
except Exception:
    sys.exit("not JSON")
if not isinstance(body, dict):
    sys.exit("not a JSON object")
if set(body) - {"vendor", "host", "port", "tls", "zones"}:
    sys.exit("unknown top-level fields")
zones = body.get("zones")
if not isinstance(zones, dict) or not zones or not all(isinstance(z, dict) and z for z in zones.values()):
    sys.exit("no zone")
PYEOF2
    die "the value is not the credential object apps/swarm-egress/swarm_egress/vendor.py reads (vendor, host, port, tls, zones); nothing was stored"
  fi
  info "value is a vendor credential object (${EGRESS_BYTES} bytes)"
  EGRESS_VERSION="$(gcloud secrets versions add "${EGRESS_NAME}" \
    --project "${PROJECT_ID}" --data-file="${EGRESS_FILE}" --format='value(name)')"
  ok "added version ${EGRESS_VERSION##*/} of ${EGRESS_NAME}; its value was never printed"
  dim "swarm-egress is its one reader (terraform/infra/egress.tf) and re-reads it on its own timer; no restart is needed"
  dim "delete any file you redirected from; Secret Manager is the only place the value lives"
  exit 0
fi

if [[ -n "${OVERWRITE}" ]]; then
  die "--overwrite confirms a new version of swarm-egress-key, swarm-egress-credential, swarm-typesafe-fwd-key, swarm-graph-ticket-key or swarm-settings-key only"
fi

if [[ "${CHILD_KEY}" -eq 1 && "${LIVE_KEY}" -eq 1 ]]; then
  die "--child-key and --live-key are two keys; add one per run"
fi

if [[ "${CHILD_KEY}" -eq 1 ]]; then
  # Nothing about a tenant applies, and a value an operator supplies is a value
  # an operator has seen. Refused rather than ignored.
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}" || "${READ_STDIN}" -eq 1 || "${SUBSCRIPTION}" -eq 1 ]]; then
    die "--child-key takes no --tenant, --provider, --stdin, --from-file or --subscription: the value is generated here and never shown"
  fi
  # A rotation is TWO steps a dispatch window apart (§5 F13): swarm-api accepts
  # the previous version while workers dispatched under it register. Disabling
  # it in the same run would refuse every attempt dispatched just before.
  if [[ "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--disable-previous is refused with --child-key: pin the old version as child_key_previous_version, apply, wait one dispatch window (the lease's 300 s dispatch deadline), then disable it by hand"
  fi
  add_generated_platform_key "swarm-child-key" "terraform/infra/child_tasks.tf" "--child-key"
  dim "swarm-scheduler and swarm-api read it as SWARM_CHILD_KEY once enable_child_tasks = true is applied"
  dim "rotating: set child_key_previous_version to the version this one replaced and apply in the same change"
  exit 0
fi

if [[ "${LIVE_KEY}" -eq 1 ]]; then
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}" || "${READ_STDIN}" -eq 1 || "${SUBSCRIPTION}" -eq 1 || -n "${GITHUB_APP_SLOT}" ]]; then
    die "--live-key takes no --tenant, --provider, --stdin, --from-file, --subscription or --github-app: the value is generated here and never shown"
  fi
  # A ticket lives 60 seconds at most (§3.3), so nothing waits on the old
  # version beyond the sessions it ends; disabling it is still a separate,
  # deliberate step, after swarm-api and the relay have both restarted onto
  # the new one.
  if [[ "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--disable-previous is refused with --live-key: restart swarm-api and the swarm-live relay onto the new version first, then disable the old one by hand"
  fi
  add_generated_platform_key "swarm-live-key" "terraform/infra/live.tf" "--live-key"
  dim "swarm-api and the swarm-live relay read it at run time; the relay only once enable_live_browser = true is applied"
  exit 0
fi

if [[ -n "${GITHUB_APP_SLOT}" ]]; then
  # The slot word is the only input that names the secret, and it names one of
  # exactly two; tests/unit/scripts/test_github_app_runbook.py holds this list
  # to the ids terraform/modules/secret_manager declares.
  case "${GITHUB_APP_SLOT}" in
    client-secret|private-key) ;;
    *) die "--github-app takes client-secret or private-key, not '${GITHUB_APP_SLOT}'" ;;
  esac
  APP_SLOT_ID="swarm-github-app-${GITHUB_APP_SLOT}"
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}" || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 ]]; then
    die "--github-app takes only --stdin: no --tenant, --provider, --from-file, --subscription, --child-key or --live-key. The App's secrets are the platform's, not a tenant's, and a file argument is a path to a plaintext copy"
  fi
  # GitHub keeps both client secrets (and both private keys) valid until you
  # delete the old one there, so a rotation is: add here, let swarm-api read
  # the new version, delete the old one at GitHub, then disable the old
  # version by hand. Disabling first would refuse every exchange in between.
  if [[ "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--disable-previous is refused with --github-app: delete the old ${GITHUB_APP_SLOT} at GitHub first, then disable its version by hand (gcloud secrets versions disable <N> --secret ${APP_SLOT_ID}); docs/runbooks/github-app.md, 'Rotating'"
  fi
  [[ "${READ_STDIN}" -eq 1 ]] || die "--github-app reads the value from --stdin and nothing else"

  step "Secret ${APP_SLOT_ID}"
  APP_ERR=""
  if ! APP_ERR="$(gcloud secrets describe "${APP_SLOT_ID}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${APP_ERR}"
    die "${APP_SLOT_ID} does not exist in ${PROJECT_ID}. terraform creates it with swarm-api as its one reader (modules/secret_manager, enable_github_app = true): apply first, then this"
  fi

  APP_TMP="$(mktemp -d "${TMPDIR:-/tmp}/swarm-secret.XXXXXX")"
  chmod 0700 "${APP_TMP}"
  # shellcheck disable=SC2064  # expand now: the path is fixed for this run
  trap "find '${APP_TMP}' -type f -exec rm -f {} + 2>/dev/null || true; rmdir '${APP_TMP}' 2>/dev/null || true" EXIT INT TERM
  APP_FILE="${APP_TMP}/value"
  ( umask 077; : >"${APP_FILE}" )
  if [[ -t 0 ]]; then
    if [[ "${GITHUB_APP_SLOT}" == "private-key" ]]; then
      die "the private key is many lines: redirect the downloaded .pem into stdin (--stdin < <file>), then delete the file"
    fi
    printf 'Paste the GitHub App client secret (input hidden), then press Enter: ' >&2
    read -rs APP_VALUE
    printf '\n' >&2
    printf '%s' "${APP_VALUE}" >"${APP_FILE}"
    unset APP_VALUE
  else
    cat >"${APP_FILE}"
  fi
  # One trailing newline off, as for every other value.
  if [[ -s "${APP_FILE}" ]]; then
    printf '%s' "$(cat "${APP_FILE}")" >"${APP_FILE}.trimmed"
    mv "${APP_FILE}.trimmed" "${APP_FILE}"
    chmod 0600 "${APP_FILE}"
  fi
  APP_BYTES="$(wc -c <"${APP_FILE}" | tr -d ' ')"
  [[ "${APP_BYTES}" -gt 0 ]] || die "refusing to store an empty secret"
  [[ "${APP_BYTES}" -lt 65536 ]] || die "value is ${APP_BYTES} bytes; that is neither a client secret nor a private key"
  # The shape BLOCKS for the key: a pasted client secret or a public key in the
  # private-key slot is accepted by every byte count and fails only when
  # swarm-api first signs a JWT with it.
  if [[ "${GITHUB_APP_SLOT}" == "private-key" ]]; then
    if ! head -n 1 "${APP_FILE}" | grep -Eq '^-----BEGIN ([A-Z]+ )?PRIVATE KEY-----$'; then
      die "the private-key slot takes the PEM file GitHub downloads (its first line is the BEGIN ... PRIVATE KEY marker); nothing was stored"
    fi
    ok "value is a PEM private key (${APP_BYTES} bytes)"
  else
    if grep -q -- '-----BEGIN' "${APP_FILE}"; then
      die "that is a PEM block, not a client secret; store the key with --github-app private-key. Nothing was stored"
    fi
    info "stored value is ${APP_BYTES} bytes"
  fi

  APP_VERSION="$(gcloud secrets versions add "${APP_SLOT_ID}" \
    --project "${PROJECT_ID}" --data-file="${APP_FILE}" --format='value(name)')"
  ok "added version ${APP_VERSION##*/} of ${APP_SLOT_ID}; its value was never printed"
  dim "swarm-api is its one reader (terraform/modules/secret_manager github_app_accessor)"
  dim "delete any downloaded copy now; Secret Manager is the only place the value lives"
  exit 0
fi

if [[ "${TAILSCALE_OAUTH}" -eq 1 ]]; then
  # The secret's name is fixed: terraform/infra/tailscale.tf creates it and
  # kubernetes/apply.sh reads it; tests/unit/scripts/test_create_secrets_tailscale_oauth.py
  # holds the three spellings equal.
  TS_OAUTH_NAME="swarm-tailscale-oauth"
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}${GITHUB_APP_SLOT}" || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 || "${EGRESS_KEY}" -eq 1 || "${EGRESS_CREDENTIAL}" -eq 1 || "${SEARCH_API_KEY}" -eq 1 || "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--tailscale-oauth takes only --stdin: the client is the platform's, not a tenant's, a file argument is a path to a plaintext copy, and an old version is disabled by hand once the forwarder runs on the new one"
  fi
  [[ "${READ_STDIN}" -eq 1 ]] || die "--tailscale-oauth reads the value from --stdin and nothing else"

  step "Secret ${TS_OAUTH_NAME}"
  TS_DESCRIBE_ERR=""
  if ! TS_DESCRIBE_ERR="$(gcloud secrets describe "${TS_OAUTH_NAME}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    die_if_auth_failure "${TS_DESCRIBE_ERR}"
    die "${TS_OAUTH_NAME} does not exist in ${PROJECT_ID}. terraform creates it with its one reader (terraform/infra/tailscale.tf): run 'make apply' first, then --tailscale-oauth"
  fi

  TS_TMP="$(mktemp -d "${TMPDIR:-/tmp}/swarm-secret.XXXXXX")"
  chmod 0700 "${TS_TMP}"
  # shellcheck disable=SC2064  # expand now: the path is fixed for this run
  trap "find '${TS_TMP}' -type f -exec rm -f {} + 2>/dev/null || true; rmdir '${TS_TMP}' 2>/dev/null || true" EXIT INT TERM
  TS_FILE="${TS_TMP}/value"
  ( umask 077; : >"${TS_FILE}" )
  if [[ -t 0 ]]; then
    printf 'Paste the Tailscale OAuth client secret (input hidden), then press Enter: ' >&2
    read -rs TS_VALUE
    printf '\n' >&2
    printf '%s' "${TS_VALUE}" >"${TS_FILE}"
    unset TS_VALUE
  else
    cat >"${TS_FILE}"
  fi
  if [[ -s "${TS_FILE}" ]]; then
    printf '%s' "$(tr -d '[:space:]' <"${TS_FILE}")" >"${TS_FILE}.trimmed"
    mv "${TS_FILE}.trimmed" "${TS_FILE}"
    chmod 0600 "${TS_FILE}"
  fi
  TS_BYTES="$(wc -c <"${TS_FILE}" | tr -d ' ')"
  [[ "${TS_BYTES}" -gt 0 ]] || die "refusing to store an empty secret"
  [[ "${TS_BYTES}" -lt 1024 ]] || die "value is ${TS_BYTES} bytes; that is not a Tailscale OAuth client secret. Nothing was stored"
  # The shape, judged without echoing it: the client's SECRET, not its id, an
  # auth key (tskey-auth-) or an API token (tskey-api-).
  if [[ "$(head -c 13 "${TS_FILE}")" != "tskey-client-" ]]; then
    die "that is not a Tailscale OAuth client secret (it does not start tskey-client-); nothing was stored. Paste the client's secret, not its id, an auth key or an API token"
  fi
  info "stored value is ${TS_BYTES} bytes"

  TS_VERSION="$(gcloud secrets versions add "${TS_OAUTH_NAME}" \
    --project "${PROJECT_ID}" --data-file="${TS_FILE}" --format='value(name)')"
  ok "added version ${TS_VERSION##*/} of ${TS_OAUTH_NAME}; its value was never printed"
  dim "its one reader is the identity that syncs it into the cluster (terraform/infra/tailscale.tf)"
  dim "next: kubernetes/apply.sh --swarm-system --confirm copies it into swarm-system/operator-oauth"
  dim "rotating: store the new one, re-run that apply, restart deployment/decision-api-egress, revoke the old client secret in Tailscale"
  exit 0
fi

if [[ "${SEARCH_API_KEY}" -eq 1 ]]; then
  # The secret's name is fixed: it is swarm-api's SEARCH_API_KEY_SECRET default
  # (apps/swarm-api/swarm_api/settings.py), held equal by
  # tests/unit/scripts/test_create_secrets_search_key.py.
  SEARCH_SECRET="swarm-search-api-key"
  SEARCH_READER="serviceAccount:swarm-api@${PROJECT_ID}.iam.gserviceaccount.com"
  if [[ -n "${TENANT}${PROVIDER}${FROM_FILE}${GITHUB_APP_SLOT}" || "${SUBSCRIPTION}" -eq 1 || "${CHILD_KEY}" -eq 1 || "${LIVE_KEY}" -eq 1 || "${DISABLE_PREVIOUS}" -eq 1 ]]; then
    die "--search-api-key takes only --stdin: the key is the platform's, not a tenant's, a file argument is a path to a plaintext copy, and an old version is disabled by hand once swarm-api reads the new one"
  fi
  [[ "${READ_STDIN}" -eq 1 ]] || die "--search-api-key reads the value from --stdin and nothing else"

  step "Secret ${SEARCH_SECRET}"
  SEARCH_ERR=""
  if SEARCH_ERR="$(gcloud secrets describe "${SEARCH_SECRET}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    ok "secret exists; adding a new version"
  else
    die_if_auth_failure "${SEARCH_ERR}"
    info "creating secret"
    gcloud secrets create "${SEARCH_SECRET}" \
      --project "${PROJECT_ID}" \
      --replication-policy=user-managed \
      --locations="${REGION}" \
      --labels="managed-by=swarm-secrets,component=platform-search-key,environment=${ENVIRONMENT}" \
      >/dev/null
    ok "created ${SEARCH_SECRET} (replicated only to ${REGION})"
  fi

  SEARCH_TMP="$(mktemp -d "${TMPDIR:-/tmp}/swarm-secret.XXXXXX")"
  chmod 0700 "${SEARCH_TMP}"
  # shellcheck disable=SC2064  # expand now: the path is fixed for this run
  trap "find '${SEARCH_TMP}' -type f -exec rm -f {} + 2>/dev/null || true; rmdir '${SEARCH_TMP}' 2>/dev/null || true" EXIT INT TERM
  SEARCH_FILE="${SEARCH_TMP}/value"
  ( umask 077; : >"${SEARCH_FILE}" )
  if [[ -t 0 ]]; then
    printf 'Paste the search API key (input hidden), then press Enter: ' >&2
    read -rs SEARCH_VALUE
    printf '\n' >&2
    printf '%s' "${SEARCH_VALUE}" >"${SEARCH_FILE}"
    unset SEARCH_VALUE
  else
    cat >"${SEARCH_FILE}"
  fi
  if [[ -s "${SEARCH_FILE}" ]]; then
    printf '%s' "$(tr -d '[:space:]' <"${SEARCH_FILE}")" >"${SEARCH_FILE}.trimmed"
    mv "${SEARCH_FILE}.trimmed" "${SEARCH_FILE}"
    chmod 0600 "${SEARCH_FILE}"
  fi
  SEARCH_BYTES="$(wc -c <"${SEARCH_FILE}" | tr -d ' ')"
  [[ "${SEARCH_BYTES}" -gt 0 ]] || die "refusing to store an empty secret"
  [[ "${SEARCH_BYTES}" -lt 1024 ]] || die "value is ${SEARCH_BYTES} bytes; that is not a search API key. Nothing was stored"
  info "stored value is ${SEARCH_BYTES} bytes"

  SEARCH_VERSION="$(gcloud secrets versions add "${SEARCH_SECRET}" \
    --project "${PROJECT_ID}" --data-file="${SEARCH_FILE}" --format='value(name)')"
  ok "added version ${SEARCH_VERSION##*/} of ${SEARCH_SECRET}; its value was never printed"

  step "Access"
  search_iam_err="${SEARCH_TMP}/iam.err"
  if gcloud secrets add-iam-policy-binding "${SEARCH_SECRET}" --project "${PROJECT_ID}" \
       --member="${SEARCH_READER}" --role="roles/secretmanager.secretAccessor" \
       >/dev/null 2>"${search_iam_err}"; then
    ok "swarm-api may read ${SEARCH_SECRET}; nobody else was granted it"
  else
    die_if_auth_failure "$(cat "${search_iam_err}")"
    err "could not grant swarm-api secretAccessor on ${SEARCH_SECRET}:"
    redact <"${search_iam_err}" | head -n 3 | sed 's/^/     /' >&2
    die "the key is stored but swarm-api cannot read it: every search answers search_unconfigured until the binding exists"
  fi
  dim "swarm-api reads it per call, and only while SEARCH_TOOL_ENABLED is on (docs/search-tool.md)"
  dim "rotating: add the new key here, confirm a search succeeds, then disable the old version by hand"
  exit 0
fi

[[ -n "${TENANT}" ]]   || die "--tenant is required (e.g. eng, or u-alice for a personal tenant)"
[[ -n "${PROVIDER}" ]] || die "--provider is required (e.g. anthropic, openai)"
# The removed -git-wf slot is not a provider key either: this path would
# create it labelled tenant-credential, and nothing reads it any more.
if [[ "${PROVIDER}" == "git-wf" ]]; then
  die "the -git-wf slot no longer exists (#2029): a workflow-file push uses a token the SwarmCloud GitHub App mints per push, for a repository whose workflow_files switch is on (docs/runbooks/github-app.md). Nothing was stored."
fi

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

STILL_ENABLED=()
if [[ "${DISABLE_PREVIOUS}" -eq 1 && -n "${PREVIOUS}" ]]; then
  step "Disabling superseded versions"
  # Disabled, not destroyed: a rotation that turns out to have stored the wrong
  # key must be reversible, and an in-flight worker may still hold the old one.
  #
  # A failed disable used to be `>/dev/null 2>&1` and one warn line, and the
  # run still ended "ok credential stored". With --disable-previous the old
  # version is usually the LEAKED key this rotation exists to kill, so a version
  # left enabled is the whole job undone: the reason is shown, and the run ends
  # non-zero naming every version still live.
  disable_err="${TMPDIR_SECRET}/disable-version.err"
  while IFS= read -r old; do
    [[ -n "${old}" ]] || continue
    if gcloud secrets versions disable "${old}" --secret "${SECRET_NAME}" \
         --project "${PROJECT_ID}" >/dev/null 2>"${disable_err}"; then
      ok "disabled version ${old}"
    else
      die_if_auth_failure "$(cat "${disable_err}")"
      err "could not disable version ${old}; it is STILL ENABLED:"
      redact <"${disable_err}" | head -n 3 | sed 's/^/     /' >&2
      STILL_ENABLED+=("${old}")
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
  elif [[ "${SUBSCRIPTION}" -eq 1 ]]; then
    # The -refresh half is read by the quota broker and NEVER by the tenant's
    # worker: terraform/modules/secret_manager `refresh_accessor` binds the
    # broker alone, because a job holding the refresh token could mint itself
    # access for as long as it liked. So this must not send anyone to grant
    # the worker this secret. The worker reads the short-lived half the broker
    # publishes into swarm-tenant-<t>-<p>, and that is what --add-provider binds.
    warn "no service account can read ${SECRET_NAME} yet"
    dim "its one reader is the quota broker, never the tenant's worker: terraform/modules/secret_manager"
    dim "grants it (refresh_accessor) for each tenant/provider pair terraform declares, and to nothing else."
    dim "the worker reads swarm-tenant-${TENANT}-${PROVIDER}, which the broker publishes. To list ${PROVIDER}"
    dim "for the tenant and let its worker read that secret, keeping the tenant's other providers:"
    dim "run: scripts/register-tenant.sh --tenant ${TENANT} --add-provider ${PROVIDER}"
  else
    warn "no service account can read ${SECRET_NAME} yet"
    # --add-provider, NOT --providers. This printed
    # `register-tenant.sh --tenant <t> --providers <p>`, which died on its
    # first line -- the full registration needs --group or --user -- and,
    # given one, would have REPLACED the tenant's credentials with this one
    # provider and reset its limits. --add-provider grants the worker this one
    # secret and adds the provider to the list, and changes nothing else.
    dim "run: scripts/register-tenant.sh --tenant ${TENANT} --add-provider ${PROVIDER}"
  fi
fi

hr
ok "credential stored for tenant ${TENANT}, provider ${PROVIDER}"
dim "the plaintext existed only in ${TMPDIR_SECRET}, which is now removed"
if [[ "${#STILL_ENABLED[@]}" -gt 0 ]]; then
  err "the rotation is NOT complete: ${#STILL_ENABLED[@]} superseded version(s) of ${SECRET_NAME} are still enabled: ${STILL_ENABLED[*]}"
  die "disable them by hand (gcloud secrets versions disable <N> --secret ${SECRET_NAME}) before treating the old key as dead"
fi
