#!/usr/bin/env bash
# Call the control-plane API as yourself, without putting a token in argv.
#
# This exists because the obvious one-liner is unsafe:
#
#   curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$API/v1/..."
#
# An ID token is not a session cookie. It is the ONLY input from which the API
# derives tenant identity, so whoever holds one for the next hour is that
# person's tenant: their provider keys, their GCS prefix, their budget. Putting
# it in curl's command line publishes it to every other process on the machine
# through /proc/<pid>/cmdline, and writes it into shell history -- the exact
# objection this repository raises to justify create-secrets.sh's design.
#
# So the header goes to curl on stdin (`curl -K -`), built by a shell builtin,
# and never appears in any argv. Audience and impersonation are handled by
# lib/common.sh's id_token(), so `SWARM_IMPERSONATE_SA` and `API_AUDIENCE` work
# here and a bare gcloud token is only the fallback.
#
# Usage:
#   scripts/api.sh GET  /tasks/tsk_123
#   scripts/api.sh GET  /tasks/tsk_123 | jq .blocked_by
#   scripts/api.sh POST /tasks '{"runner_profile":"mock","input":{}}'
#   scripts/api.sh POST /tasks @request.json
#   scripts/api.sh --raw GET /healthz          # no /v1 prefix
#
# Body on stdout, HTTP status on stderr. Exits non-zero on a non-2xx response so
# it composes with `set -e` in a runbook.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RAW=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --raw)     RAW=1; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) break ;;
  esac
done

[[ $# -ge 2 ]] || die "usage: scripts/api.sh [--raw] METHOD PATH [BODY|@FILE]"

METHOD="$1"
REQUEST_PATH="$2"
BODY="${3:-}"

require_cmd gcloud curl

# `@file` reads the body from a file, so a large or quoted JSON payload does not
# have to survive a shell quoting round trip.
if [[ "${BODY}" == @* ]]; then
  BODY_FILE="${BODY#@}"
  [[ -f "${BODY_FILE}" ]] || die "no such file: ${BODY_FILE}"
  BODY="$(cat "${BODY_FILE}")"
fi

case "${REQUEST_PATH}" in
  /*) ;;
  *) REQUEST_PATH="/${REQUEST_PATH}" ;;
esac

[[ "${RAW}" -eq 1 ]] || REQUEST_PATH="${API_PREFIX}${REQUEST_PATH}"

# Redirected to a file rather than captured with $(...): a command substitution
# runs in a SUBSHELL, so API_STATUS set inside it would never reach this shell
# and every response would look like a success.
RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/swarm-api.XXXXXX")"
trap 'rm -f "${RESPONSE_FILE}"' EXIT INT TERM

api_request "${METHOD}" "${REQUEST_PATH}" "${BODY}" >"${RESPONSE_FILE}" || true
cat "${RESPONSE_FILE}"
printf '\n'

if [[ "${API_STATUS}" -ge 200 && "${API_STATUS}" -lt 300 ]]; then
  exit 0
fi
err "HTTP ${API_STATUS} from ${METHOD} ${REQUEST_PATH}"
exit 1
