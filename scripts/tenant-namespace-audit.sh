#!/usr/bin/env bash
# List every tenant document whose `namespace` is not the canonical one.
#
# WHY THIS EXISTS
# ---------------
# On 2026-10-11 `tenants/u-bogdan` carried `namespace: swarm-u-bogdan`, the
# spelling used before 2026-09-23, while its real namespace is the derived
# one. The dispatcher preferred the stored value unchecked, so every GKE task
# for that tenant was created in a namespace that does not exist and came back
# 403 `jobs.batch is forbidden` (docs/gke-dispatch-403.md). The scheduler and
# the reconciler now verify the stored value before using it
# (`verified_namespace` in apps/scheduler/scheduler/dispatch.py and
# apps/reconciler/reconciler/backends.py) and log `tenant_namespace_mismatch`;
# this is the same comparison on demand, over every tenant, before anything
# dispatches -- the sweep in docs/runbooks/gke-dispatch-redispatch.md made
# runnable.
#
# THE CANONICAL FORM IS THE DISPATCHER'S, imported rather than restated:
# `sanitize_name(GkeTarget.namespace_template.format(tenant=id))` from
# scheduler.dispatch, through the project's python (`swarm_python`), so the
# 63-character truncation and hash agree with what is actually dispatched.
#
# Each tenant that records a namespace is one of:
#
#   ok        equal to the canonical form, or no namespace stored (the
#             dispatcher derives it);
#   IGNORED   outside the authority prefix. The services already use the
#             canonical name instead and log at ERROR -- the document is wrong;
#   KEPT      inside the prefix but not canonical (a `-canary` rename that
#             kubernetes/render.py --namespace allows). The services use it and
#             log at WARNING. Reported, because it is not the canonical form;
#             leave it only if that namespace is deliberate.
#
# READ-ONLY. One `GET /v1/admin/tenants` (an admin caller), nothing written.
# To repair a document, re-run scripts/register-tenant.sh for that tenant.
#
# Usage:
#   scripts/tenant-namespace-audit.sh          # report; exit 1 on any mismatch
#   scripts/tenant-namespace-audit.sh --json   # the same, as JSON on stdout
#
# Exit: 0 every tenant canonical, 1 at least one mismatch, 2 the audit could
# not run (the API refused, or it listed no tenants at all).

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_cmd jq curl

AS_JSON=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)    AS_JSON=1; shift ;;
    -h|--help) sed -n '2,42p' "$0"; exit 0 ;;
    *)         die "unknown argument: $1" ;;
  esac
done

work="$(mktemp -d "${TMPDIR:-/tmp}/swarm-ns-audit.XXXXXX")"
trap 'rm -rf "${work}"' EXIT

step "Tenant namespaces (${PROJECT_ID}, ${ENVIRONMENT})"

# Redirected to a file, not captured with "$(...)": a command substitution
# runs in a subshell and would throw away the API_STATUS api_request sets.
if ! api_get "/admin/tenants" >"${work}/tenants.json"; then
  err "GET ${API_PREFIX}/admin/tenants answered HTTP ${API_STATUS}; this needs an admin caller."
  redact <"${work}/tenants.json" | head -c 2000 >&2 || true
  printf '\n' >&2
  exit 2
fi

if ! jq -e '.tenants | type == "array"' "${work}/tenants.json" >/dev/null 2>&1; then
  err "the response has no .tenants array; refusing to report a clean audit over it"
  exit 2
fi

# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a PY <<<"$(swarm_python)"

status=0
PYTHONPATH="${REPO_ROOT}/apps/scheduler:${REPO_ROOT}/apps/common${PYTHONPATH:+:${PYTHONPATH}}" \
  "${PY[@]}" - "${work}/tenants.json" >"${work}/audit.json" <<'PY' || status=$?
import json
import sys

from scheduler.dispatch import DispatchError, GkeTarget, sanitize_name

template = GkeTarget.namespace_template
prefix = template.split("{tenant}", 1)[0]
tenants = json.load(open(sys.argv[1])).get("tenants") or []
rows = []
for tenant in tenants:
    tenant_id = str(tenant.get("tenant_id") or "")
    if not tenant_id:
        rows.append({"tenant_id": "", "stored": tenant.get("namespace"),
                     "canonical": None, "verdict": "NO_ID"})
        continue
    try:
        canonical = sanitize_name(template.format(tenant=tenant_id))
    except DispatchError:
        rows.append({"tenant_id": tenant_id, "stored": tenant.get("namespace"),
                     "canonical": None, "verdict": "NO_ID"})
        continue
    stored = tenant.get("namespace") or ""
    if not stored or stored == canonical:
        verdict = "ok"
    elif stored.startswith(prefix) and len(stored) > len(prefix):
        verdict = "KEPT"
    else:
        verdict = "IGNORED"
    rows.append({"tenant_id": tenant_id, "stored": stored or None,
                 "canonical": canonical, "verdict": verdict})
json.dump({"prefix": prefix, "visited": len(tenants), "tenants": rows}, sys.stdout)
PY
if [[ "${status}" -ne 0 ]]; then
  err "could not compute the canonical namespaces (the project python, exit ${status}); run: uv sync"
  exit 2
fi

visited="$(jq -r '.visited' "${work}/audit.json")"
mismatches="$(jq '[.tenants[] | select(.verdict != "ok")] | length' "${work}/audit.json")"

if [[ "${AS_JSON}" -eq 1 ]]; then
  jq '.' "${work}/audit.json"
else
  # Unit separator, not tab: tab is IFS whitespace, so an empty tenant_id
  # would collapse into the next field and shift every column after it.
  while IFS=$'\037' read -r verdict tenant_id stored canonical; do
    case "${verdict}" in
      ok)      ok "${tenant_id}: ${stored}" ;;
      KEPT)    warn "${tenant_id}: stored ${stored} is inside the prefix but not canonical (${canonical}); kept by the services" ;;
      IGNORED) err "${tenant_id}: stored ${stored} is outside the prefix; the services ignore it for ${canonical}" ;;
      *)       err "${tenant_id:-(empty)}: no usable tenant_id, so no namespace can be derived (stored ${stored})" ;;
    esac
  done < <(jq -r '.tenants[] | [.verdict, .tenant_id, (.stored // "(none, derived)"), (.canonical // "-")] | join("\u001f")' "${work}/audit.json")
fi

# The count actually visited, always: an audit over zero documents is not a
# clean audit (CLAUDE.md, "Empty output is not success").
hr
if [[ "${visited}" -eq 0 ]]; then
  err "the API listed 0 tenants; nothing was audited"
  exit 2
fi
if [[ "${mismatches}" -gt 0 ]]; then
  err "${mismatches} of ${visited} tenant(s) record a namespace that is not canonical."
  log "Repair: scripts/register-tenant.sh for each one (docs/runbooks/gke-dispatch-redispatch.md)."
  exit 1
fi
ok "all ${visited} tenant(s) record the canonical namespace, or none"
