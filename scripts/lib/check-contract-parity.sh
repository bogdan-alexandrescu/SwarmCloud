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

# --------------------------------------------------------------------------
# 3. The task lifecycle, restated as shell arrays in common.sh.
# --------------------------------------------------------------------------
# CONTRACT.md invariant 1 is a PARTITION of `TaskState`: CONCURRENCY_STATES hold
# capacity, PENDING_STATES cost nothing, TERMINAL_STATES have released. A state
# added to the enum and not to the arrays is counted in none of them, so
# `make status` prints a busy platform as idle -- during exactly the incident
# where that number is the one being read. The full enum is asserted too,
# because `status.sh` counts tasks by iterating it: a state missing from
# TASK_STATES is a state whose tasks are never queried at all.
set_matches() {
  local name="$1"; shift
  local from_shell from_py
  from_shell="$(printf '%s\n' "$@" | LC_ALL=C sort | tr '\n' ' ')"
  from_py="$(python3 - "${REPO_ROOT}" "${name}" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common import states as S

name = sys.argv[2]
if name == "TASK_STATES":
    values = [s.value for s in S.TaskState]
else:
    values = [s.value for s in getattr(S, name)]
print(" ".join(sorted(values)), end=" ")
PY
)"
  if [[ "${from_shell}" == "${from_py}" ]]; then
    ok "${name}: the shell array matches swarm_common.states ($# state(s))"
  else
    err "${name} has DRIFTED from swarm_common.states:"
    err "    frozen enum     : ${from_py}"
    err "    common.sh array : ${from_shell}"
    err "Fix the array in scripts/lib/common.sh; the frozen module is the authority."
    FAILED=1
  fi
}

set_matches TASK_STATES        ${TASK_STATES[@]+"${TASK_STATES[@]}"}
set_matches CONCURRENCY_STATES ${CONCURRENCY_STATES[@]+"${CONCURRENCY_STATES[@]}"}
set_matches PENDING_STATES     ${PENDING_STATES[@]+"${PENDING_STATES[@]}"}
set_matches TERMINAL_STATES    ${TERMINAL_STATES[@]+"${TERMINAL_STATES[@]}"}

# --------------------------------------------------------------------------
# 4. The tenant-id length budget, restated in register-tenant.sh.
# --------------------------------------------------------------------------
# `swarm_common.identity` sizes its slugs so `<_GSA_PREFIX><id>` always fits
# GCP's 30-character service account id; register-tenant.sh refuses an id that
# does not. While both derive the same number the script's check is unreachable,
# which is the correct state. If the frozen module's budget ever grows past what
# the script's prefix leaves, the resolver starts minting ids the script and
# terraform both refuse -- tenants the API resolves and nobody can provision.
# That gap was real here once, and the script's error text went on describing it
# long after it was closed, which is why the number is asserted and not just
# written down.
#
# Read out of the source rather than by running the script: it takes a
# principal, talks to GCP and creates things.
REGISTER_TENANT="${REPO_ROOT}/scripts/register-tenant.sh"
SHELL_GSA_PREFIX="$(sed -n 's/^GSA_PREFIX="\(.*\)"$/\1/p' "${REGISTER_TENANT}" | head -1)"
# shellcheck disable=SC2016  # the $(( is the literal shell arithmetic being matched in the file
SHELL_ID_MAX="$(sed -n 's/^MAX_TENANT_ID=\$(( *\([0-9][0-9]*\) *-.*$/\1/p' "${REGISTER_TENANT}" | head -1)"

if [[ -z "${SHELL_GSA_PREFIX}" || -z "${SHELL_ID_MAX}" ]]; then
  # Refuse rather than skip. A parity check that passes because it could no
  # longer find the thing it compares is worse than no check at all: it reports
  # an agreement it never established.
  err "could not read GSA_PREFIX / MAX_TENANT_ID out of scripts/register-tenant.sh"
  err "The restatement moved or changed shape; update the sed above so this keeps checking it."
  FAILED=1
else
  SHELL_TENANT_BUDGET=$(( SHELL_ID_MAX - ${#SHELL_GSA_PREFIX} ))
  FROZEN_IDENTITY="$(python3 - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common import identity

print(identity._GSA_PREFIX, identity._GSA_ACCOUNT_ID_MAX, identity._MAX_TENANT_ID)
PY
)"
  read -r FROZEN_PREFIX FROZEN_ID_MAX FROZEN_BUDGET <<<"${FROZEN_IDENTITY}"

  if [[ "${SHELL_GSA_PREFIX}" == "${FROZEN_PREFIX}" ]] \
     && [[ "${SHELL_ID_MAX}" == "${FROZEN_ID_MAX}" ]] \
     && [[ "${SHELL_TENANT_BUDGET}" == "${FROZEN_BUDGET}" ]]; then
    ok "tenant-id budget: register-tenant.sh and swarm_common.identity both allow ${FROZEN_BUDGET} characters"
  else
    err "the tenant-id budget has DRIFTED from swarm_common.identity:"
    err "    frozen module      : prefix '${FROZEN_PREFIX}' cap ${FROZEN_ID_MAX} -> ${FROZEN_BUDGET} characters"
    err "    register-tenant.sh : prefix '${SHELL_GSA_PREFIX}' cap ${SHELL_ID_MAX} -> ${SHELL_TENANT_BUDGET} characters"
    err "Ids the resolver mints but the script refuses are tenants nobody can provision."
    FAILED=1
  fi
fi

hr
[[ "${FAILED}" -eq 0 ]] || die "a restatement of the frozen contract has drifted"
ok "every restatement of the frozen contract still matches it"
