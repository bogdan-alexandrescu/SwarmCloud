#!/usr/bin/env bash
# Assert that every shell/jq restatement of the frozen contract still matches it.
#
# CONTRACT.md says "Import from it; do not redefine its types". A bash script
# cannot import Python, so a handful of properties are necessarily restated in
# jq -- and a restatement with nothing asserting it is just a copy waiting to
# drift. It already had: `scripts/status.sh` carried its own inline copy of
# SlotPool.effective_limit that had lost the `max(0, ...)` floor the model and
# the common.sh copy both apply, so a pool with a negative cap would have been
# printed with a negative limit while the scheduler computed zero.
#
# docs/architecture.md permits exactly one other restatement -- Terraform's copy
# of the runner catalogue -- and permits it *because* a test asserts the copy
# still matches. This is the equivalent test for the shell side.
#
# Run by `make test` and by the `shell` job in .github/workflows/application.yml.
# It needs no cloud credentials and no emulator.
#
# Usage: scripts/lib/check-contract-parity.sh

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_cmd jq python3

FAILED=0

step "Frozen-contract parity"

# --------------------------------------------------------------------------
# 1. SlotPool.effective_limit, restated as `effective_limit` in FS_JQ.
# --------------------------------------------------------------------------
# The matrix deliberately includes the cases that separate a correct copy from a
# plausible one: an absent adaptive target, an absent quota cap, a quota cap
# below the hard limit, and the negative inputs that need the max(0, ...) floor.
CASES='[
  {"hard_limit": 10, "adaptive_target": null, "quota_derived_limit": null},
  {"hard_limit": 10, "adaptive_target": 4,    "quota_derived_limit": null},
  {"hard_limit": 10, "adaptive_target": null, "quota_derived_limit": 3},
  {"hard_limit": 10, "adaptive_target": 4,    "quota_derived_limit": 7},
  {"hard_limit": 10, "adaptive_target": 7,    "quota_derived_limit": 4},
  {"hard_limit": 0,  "adaptive_target": 0,    "quota_derived_limit": 0},
  {"hard_limit": 10, "adaptive_target": 0,    "quota_derived_limit": null},
  {"hard_limit": -1, "adaptive_target": null, "quota_derived_limit": null},
  {"hard_limit": 10, "adaptive_target": -5,   "quota_derived_limit": null},
  {"hard_limit": 10, "adaptive_target": null, "quota_derived_limit": -3}
]'

FROM_JQ="$(jq -c "${FS_JQ} [ .[] | effective_limit ]" <<<"${CASES}")"
FROM_PY="$(python3 - "${REPO_ROOT}" "${CASES}" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common.models import SlotPool

cases = json.loads(sys.argv[2])
print(json.dumps([
    SlotPool(
        name="parity",
        hard_limit=c["hard_limit"],
        adaptive_target=c["adaptive_target"],
        quota_derived_limit=c["quota_derived_limit"],
    ).effective_limit
    for c in cases
], separators=(",", ":")))
PY
)"

if [[ "${FROM_JQ}" == "${FROM_PY}" ]]; then
  ok "SlotPool.effective_limit: jq restatement matches the frozen model on $(jq 'length' <<<"${CASES}") cases"
else
  err "SlotPool.effective_limit has DRIFTED from swarm_common.models:"
  err "    frozen model : ${FROM_PY}"
  err "    FS_JQ in common.sh : ${FROM_JQ}"
  err "Fix the jq in scripts/lib/common.sh; the frozen module is the authority."
  FAILED=1
fi

# --------------------------------------------------------------------------
# 2. Tenant.secret_name(), restated in create-secrets.sh and register-tenant.sh.
# --------------------------------------------------------------------------
# A mismatch here is silent and total: the worker asks Secret Manager for the
# frozen spelling, finds nothing, and every task of that tenant parks as
# CREDENTIAL_MISSING while the operator can see the secret they just created.
SHELL_SECRET_NAME="swarm-tenant-eng-anthropic"
FROZEN_SECRET_NAME="$(python3 - "${REPO_ROOT}" <<'PY'
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common.models import Tenant
print(Tenant(tenant_id="eng", kind="group", principal="eng@saga.xyz",
             created_at=datetime.now(timezone.utc)).secret_name("anthropic"))
PY
)"

if [[ "${SHELL_SECRET_NAME}" == "${FROZEN_SECRET_NAME}" ]]; then
  ok "Tenant.secret_name(): scripts spell it '${FROZEN_SECRET_NAME}', same as the frozen model"
else
  err "the scripts build '${SHELL_SECRET_NAME}' but Tenant.secret_name() returns '${FROZEN_SECRET_NAME}'"
  err "Every task of that tenant would park as CREDENTIAL_MISSING. Fix the scripts."
  FAILED=1
fi

# Both scripts must build that name the same way; a fixed-string grep for the
# exact expression is enough to catch one of them being edited without the other.
# shellcheck disable=SC2016  # the ${...} here is the literal text being searched for
check_secret_shape() {
  local file="$1" literal="$2"
  if grep -qF "${literal}" "${file}"; then
    ok "$(basename "${file}") uses the frozen secret-name shape"
  else
    err "$(basename "${file}") no longer builds swarm-tenant-<tenant>-<provider>"
    FAILED=1
  fi
}
# shellcheck disable=SC2016  # literal, not an expansion
check_secret_shape "${REPO_ROOT}/scripts/create-secrets.sh"   'swarm-tenant-${TENANT}-${PROVIDER}'
# shellcheck disable=SC2016  # literal, not an expansion
check_secret_shape "${REPO_ROOT}/scripts/register-tenant.sh"  'swarm-tenant-${TENANT_ID}-${provider}'

hr
[[ "${FAILED}" -eq 0 ]] || die "a restatement of the frozen contract has drifted"
ok "every restatement of the frozen contract still matches it"
