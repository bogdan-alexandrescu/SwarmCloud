#!/usr/bin/env bash
# Sign the stored `workspace_settings` documents with swarm-api's settings MAC
# (#1457; docs/design/tenant-settings-integrity.md §3.4 and §5 request B,
# contract request 86; lane ISO-SETTINGS-MAC).
#
# WHY. Every tenant worker can write any `workspace_settings/<tenant>`
# document, so swarm-api now stores a MAC beside each block it writes and,
# for a tenant listed in SETTINGS_MAC_TENANTS, acts only on a block whose MAC
# verifies. Every document written before that is unsigned, and a tenant cut
# over with unsigned settings reads them as unset (TypeSafe off, merge direct,
# Jev off). This pass signs what is stored, so the cutover changes nothing a
# person chose. Run it after `scripts/create-secrets.sh --settings-mac-key`
# and after swarm-api runs with SWARM_SETTINGS_KEY, and BEFORE a tenant is
# added to SETTINGS_MAC_TENANTS. Run it again after a key rotation: a block
# signed under the replaced key is re-signed under the new one.
#
# WHAT IT WRITES. Only each block's `mac`, computed over the fields
# swarm_common.specsign.SETTINGS_MAC_FIELDS lists for the block, as they are
# stored. No setting changes, and a field outside those lists is neither
# covered nor touched. The write is swarm_api.judgment.resign_workspace_settings,
# one transaction per document; the MAC's shape is stated there and nowhere
# else. A block that already verifies under the current key is left alone, so
# a second run writes nothing.
#
# WHAT IT REFUSES TO SIGN. A `code_intelligence` block carrying
# `judgment_backend` is `held`: no swarm-api path writes that field, so only a
# direct Firestore write -- #1457's forgery -- can have put it there, and
# signing it would make the forgery the signed value. Remove the field by hand
# (after reading whose it is) and run this again.
#
# WHAT A PERSON MUST READ. The pass signs whatever is stored, including a value
# a worker forged before today. The dry run prints every block's covered
# values for that reason: read them against what each workspace chose (its
# `admin_audit` entries) before --apply.
#
# THE KEY never passes through this shell. The python reads the latest version
# of `swarm-settings-key` from Secret Manager with the operator's own
# credentials, uses it, and prints only its id (a domain-separated hash).
#
# DRY RUN BY DEFAULT. Without --apply it prints the plan and writes nothing.
# With --apply it prints the same plan, then needs the word `sign` TYPED at a
# terminal (SWARM_ASSUME_YES is ignored), then re-reads each document in its
# own transaction and signs it.
#
# Usage:
#   scripts/resign-workspace-settings.sh                   # dry run, every workspace
#   scripts/resign-workspace-settings.sh --tenant eng      # dry run, one workspace
#   scripts/resign-workspace-settings.sh --apply           # sign, after typing `sign`
#
# Exit: 0 written, nothing to do, or a dry run; 1 refused or failed; 2 usage.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# common.sh's confirm() skips the prompt when SWARM_ASSUME_YES is set. Signing
# makes every stored value one the control plane acts on, so the word is
# always typed at a terminal.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: signing workspace settings always needs the word typed at a terminal"
  unset SWARM_ASSUME_YES
fi

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/resign-workspace-settings.sh [--tenant <id>] [--apply] [--allow-prod]

  --tenant <id>   one workspace (its document id); without it, every workspace
  --apply         sign, after typing `sign`; without it, a dry run that
                  prints the plan and writes nothing
  --dry-run       the default, accepted so a command line can say so
  --allow-prod    required when ENVIRONMENT is prod
USAGE
}

TENANT=""
APPLY=0
SAID_DRY_RUN=0
ALLOW_PROD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)      APPLY=1; shift ;;
    --dry-run|-n) SAID_DRY_RUN=1; shift ;;
    --allow-prod) ALLOW_PROD=1; shift ;;
    --tenant|-t)  [[ $# -ge 2 ]] || { usage; exit 2; }; TENANT="$2"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage; err "unknown argument: $1"; exit 2 ;;
  esac
done
if [[ "${APPLY}" -eq 1 && "${SAID_DRY_RUN}" -eq 1 ]]; then
  die "--apply and --dry-run contradict each other; say which one you mean"
fi
if [[ -n "${TENANT}" ]]; then
  case "${TENANT}" in
    *[!a-z0-9-]*|'') die "tenant id '${TENANT}' must be lowercase letters, digits and hyphens" ;;
  esac
fi

case "${FIRESTORE_DATABASE}" in
  ''|'(default)'|default)
    die "refusing the (default) Firestore database; it belongs to the rest of this shared project" ;;
esac
if is_production && [[ "${ALLOW_PROD}" -eq 0 ]]; then
  die "ENVIRONMENT is '${ENVIRONMENT}'; signing production workspace settings requires --allow-prod"
fi

require_cmd jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-resign.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# resign OUT APPLY: run resign_workspace_settings with the platform's python;
# its JSON result in OUT. Into a file, never a command substitution, so the
# status is the python's.
resign() {
  local out="$1" apply="$2" python
  python="$(swarm_python)"
  # shellcheck disable=SC2086  # swarm_python may print `uv run --project <root> python`
  SWARM_RESIGN_TENANT="${TENANT}" SWARM_RESIGN_APPLY="${apply}" \
    ${python} - >"${out}" 2>"${WORK}/resign.err" <<'PY'
import json
import os
import sys

from google.cloud import firestore, secretmanager

from swarm_api.judgment import SETTINGS_KEY_SECRET_ID, resign_workspace_settings

env = os.environ
name = f"projects/{env['PROJECT_ID']}/secrets/{SETTINGS_KEY_SECRET_ID}/versions/latest"
try:
    key = secretmanager.SecretManagerServiceClient().access_secret_version(
        name=name).payload.data.decode("utf-8").strip()
except Exception as exc:  # noqa: BLE001 - the class names the cause; the message may carry a path
    print(f"could not read {SETTINGS_KEY_SECRET_ID}'s latest version ({type(exc).__name__})",
          file=sys.stderr)
    sys.exit(3)
if not key:
    print(f"{SETTINGS_KEY_SECRET_ID} has an empty latest version", file=sys.stderr)
    sys.exit(3)
db = firestore.Client(project=env["PROJECT_ID"], database=env["FIRESTORE_DATABASE"])
result = resign_workspace_settings(db, key, apply=env["SWARM_RESIGN_APPLY"] == "1",
                                   tenant_id=env["SWARM_RESIGN_TENANT"] or None)
json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
PY
}

# show FILE: the plan, one line a block, redacted.
show() {
  jq -r '.blocks[] | "  \(.status)\t\(.tenant_id)\t\(.block)\t\(.covered | tojson)"' "$1" \
    | redact >&2
}

info "firestore      ${PROJECT_ID}/${FIRESTORE_DATABASE}"
info "workspaces     ${TENANT:-every workspace}"
if ! resign "${WORK}/plan.json" 0; then
  redact <"${WORK}/resign.err" >&2
  die "the plan could not be read; nothing was written"
fi
info "key id         $(jq -r '.key' "${WORK}/plan.json") (latest version of swarm-settings-key)"
info "documents      $(jq -r '.documents' "${WORK}/plan.json")"
printf '\n  status\ttenant\tblock\tcovered values (read them before --apply)\n' >&2
show "${WORK}/plan.json"
printf '\n' >&2

TO_SIGN="$(jq '[.blocks[] | select(.status == "unsigned" or .status == "stale")] | length' "${WORK}/plan.json")"
HELD="$(jq '[.blocks[] | select(.status == "held")] | length' "${WORK}/plan.json")"
if [[ "${HELD}" -gt 0 ]]; then
  warn "${HELD} block(s) held: code_intelligence.judgment_backend is written by no swarm-api path, so it is never signed here. Remove it by hand, then run this again"
fi
if [[ "${TO_SIGN}" -eq 0 ]]; then
  ok "every block that can be signed already verifies under the current key; nothing to write"
  exit 0
fi
info "plan: sign ${TO_SIGN} block(s); only their \`mac\` is written"

if [[ "${APPLY}" -eq 0 ]]; then
  dim "dry run: nothing was written. To sign: scripts/resign-workspace-settings.sh${TENANT:+ --tenant ${TENANT}} --apply"
  exit 0
fi

confirm "This writes the settings MAC of ${TO_SIGN} block(s) in ${PROJECT_ID}/${FIRESTORE_DATABASE}, over the values printed above." "sign"

if ! resign "${WORK}/result.json" 1; then
  redact <"${WORK}/resign.err" >&2
  die "the signing pass failed; a document whose transaction did not commit was not written. Run this again: it signs only what still does not verify"
fi
ok "signed $(jq -r '.written' "${WORK}/result.json") block(s) under key $(jq -r '.key' "${WORK}/result.json")"
dim "next: add a dev tenant to SETTINGS_MAC_TENANTS on swarm-api and the scheduler, then the rest"
