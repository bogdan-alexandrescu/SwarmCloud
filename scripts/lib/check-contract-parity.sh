#!/usr/bin/env bash
# Assert that every shell, jq or TypeScript restatement of the frozen contract
# still matches it.
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
# still matches. This is the equivalent test for the other two surfaces: the
# shell and jq copies in scripts/lib/common.sh (sections 1-4), and the copies
# apps/swarm-ui/src/types.ts has to keep because a browser cannot import
# Python (section 5). The TypeScript side reads the .ts as text and needs no
# node toolchain, here or in CI.
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

# --------------------------------------------------------------------------
# 5. The frozen catalogue and the task lifecycle, restated in TypeScript.
# --------------------------------------------------------------------------
# `apps/swarm-ui/src/types.ts` hand-copies the unit weights, the twelve task
# states, the eight park reasons, the six provider states and the pool families.
# It has to: the browser cannot import Python, and the alternative to a bundled
# copy of a three-entry frozen catalogue is a request per render to learn
# something that cannot change without a contract change.
#
# Until now nothing asserted any of it, and this file's own header records why
# that matters -- three shell restatements had already drifted before anyone
# looked. TypeScript was simply the side nobody had got to. Nothing here
# typechecks the UI; `make lint` runs no `tsc` and this must keep working on a
# machine with no node toolchain, so the literals are read out of the source the
# same way section 4 reads GSA_PREFIX out of register-tenant.sh.
#
# NO APOSTROPHES IN THE PYTHON BELOW. The probe is a heredoc inside a command
# substitution, and bash scans that body for quote characters while it looks for
# the closing paren -- so one unpaired `'` anywhere in it, including in a comment
# ("the file's own..."), swallows the rest of the script. The report is
#
#     check-contract-parity.sh: line NNN: unexpected EOF while looking for matching `''
#
# with NNN pointing at the closing `)"` and nothing at the apostrophe, which is
# why this is written down rather than left to be rediscovered. `bash -n` catches
# it; write "the partition the UI makes", not "the UI's partition".
#
# WHAT IS DELIBERATELY NOT CHECKED. `REASON_COPY` is a documented SUBSET of
# BlockedReason -- five members are never written as a blocker and the table
# omits them on purpose -- so asserting equality there would fail on correct
# code. `ResourceClassSpec` carries no numbers at all: cpu/memory/disk are
# served by `/v1/resource-classes` precisely so they cannot drift, and that is
# the pattern the rest of this file argues for.
TS_TYPES="${REPO_ROOT}/apps/swarm-ui/src/types.ts"

if [[ ! -f "${TS_TYPES}" ]]; then
  # Refuse rather than skip, for the reason section 4 gives: a parity check that
  # passes because it could not find the thing it compares reports an agreement
  # it never established.
  err "apps/swarm-ui/src/types.ts is missing; the TypeScript restatements cannot be checked"
  err "If the UI moved, point this section at it -- do not let it skip."
  FAILED=1
elif ! TS_PARITY="$(python3 - "${REPO_ROOT}" "${TS_TYPES}" 2>&1 <<'PY'
import dataclasses
import json
import re
import sys
from pathlib import Path

repo_root, types_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, str(Path(repo_root) / "apps" / "common"))

from swarm_common import states as S                      # noqa: E402
from swarm_common.models import ProviderState, WorkflowStep, pool_names_for  # noqa: E402
from swarm_common.profiles import RESOURCE_CLASSES        # noqa: E402

SRC = Path(types_path).read_text()
REPORT = []


def emit(status, name, detail):
    # One line per assertion, '|'-separated, because the shell reads it with
    # `IFS='|' read`. Nothing here may contain a '|' -- every value printed is a
    # sorted list of identifiers or a small integer.
    REPORT.append("%s|%s|%s" % (status, name, detail))


# -- readers ---------------------------------------------------------------
# Each returns None when the literal is not where it was, which becomes a
# MISSING (a failure), never a silent pass.

def ts_union(name):
    """`export type N = 'a' | 'b'`, one line or continued with leading `|`."""
    m = re.search(r"export type %s\s*=([^\n]*(?:\n\s*\|[^\n]*)*)" % name, SRC)
    return None if m is None else re.findall(r"'([^']+)'", m.group(1))


def ts_set(name):
    """`export const N: ReadonlySet<T> = new Set<T>([...])`."""
    m = re.search(
        r"export const %s\b[^=]*=\s*new Set<[^>]*>\(\[(.*?)\]\)" % name, SRC, re.S
    )
    return None if m is None else re.findall(r"'([^']+)'", m.group(1))


def ts_array(name):
    """`export const N: readonly T[] = [...]`."""
    m = re.search(r"export const %s\b[^=]*=\s*\[(.*?)\]" % name, SRC, re.S)
    return None if m is None else re.findall(r"'([^']+)'", m.group(1))


def ts_number_map(name):
    """`export const N: Readonly<Record<string, number>> = { k: 1, ... }`."""
    m = re.search(r"export const %s\b[^=]*=\s*\{(.*?)\n\}" % name, SRC, re.S)
    if m is None:
        return None
    return dict(
        (k, int(v)) for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", m.group(1))
    )


def ts_pool_kind_cases():
    """The `case 'x':` labels inside `poolKind`."""
    m = re.search(r"export function poolKind\(.*?\n\}", SRC, re.S)
    return None if m is None else re.findall(r"case '([^']+)':", m.group(0))


def compare_set(name, from_ts, from_py, shape):
    if from_ts is None:
        emit("MISSING", name, "no %s found" % shape)
        return
    got, want = sorted(set(from_ts)), sorted(set(from_py))
    if len(from_ts) != len(set(from_ts)):
        emit("DRIFT", name, "the TypeScript list repeats a member: %s" % " ".join(from_ts))
    elif got == want:
        emit("OK", name, "%d member(s) match the frozen contract" % len(want))
    else:
        emit(
            "DRIFT",
            name,
            "frozen %s  //  types.ts %s" % (" ".join(want), " ".join(got)),
        )


# -- 1. the unit weights ---------------------------------------------------
# The one place the UI copies a NUMBER out of the frozen catalogue. Admission
# counts weighted units, so a wrong weight here misreports how much of the
# budget a task will cost before anyone submits it.
units_ts = ts_number_map("RESOURCE_UNITS")
units_py = dict((name, rc.units) for name, rc in RESOURCE_CLASSES.items())
if units_ts is None:
    emit("MISSING", "RESOURCE_UNITS", "no `export const RESOURCE_UNITS = {...}` found")
elif units_ts == units_py:
    emit(
        "OK",
        "RESOURCE_UNITS",
        "%s match swarm_common.profiles"
        % " ".join("%s=%d" % kv for kv in sorted(units_py.items())),
    )
else:
    emit(
        "DRIFT",
        "RESOURCE_UNITS",
        "frozen %s  //  types.ts %s"
        % (json.dumps(units_py, sort_keys=True), json.dumps(units_ts, sort_keys=True)),
    )

# -- 2. the task lifecycle -------------------------------------------------
compare_set("TaskState", ts_union("TaskState"), [s.value for s in S.TaskState], "union")
compare_set(
    "CONCURRENCY_STATES",
    ts_set("CONCURRENCY_STATES"),
    [s.value for s in S.CONCURRENCY_STATES],
    "Set literal",
)
compare_set(
    "TERMINAL_STATES",
    ts_set("TERMINAL_STATES"),
    [s.value for s in S.TERMINAL_STATES],
    "Set literal",
)

# REAL_STATES is a partition the UI makes of TaskState -- the nine a task document
# can hold -- and NEVER_WRITTEN is the rest. It is not a restatement of the
# frozen enum, so it is checked against the partition instead: a state added to
# the enum and to neither list is a state whose tasks appear in no filter, no
# legend and no histogram, which reads as "there are none".
real_states = ts_array("REAL_STATES")
never_written = ts_set("NEVER_WRITTEN")
all_states = set(s.value for s in S.TaskState)
if real_states is None or never_written is None:
    emit("MISSING", "REAL_STATES/NEVER_WRITTEN", "one of the two literals is not there")
else:
    overlap = sorted(set(real_states) & set(never_written))
    missed = sorted(all_states - set(real_states) - set(never_written))
    unknown = sorted((set(real_states) | set(never_written)) - all_states)
    if overlap:
        emit("DRIFT", "REAL_STATES/NEVER_WRITTEN", "in both lists: %s" % " ".join(overlap))
    elif missed:
        emit(
            "DRIFT",
            "REAL_STATES/NEVER_WRITTEN",
            "in neither list, so no filter or legend can show them: %s" % " ".join(missed),
        )
    elif unknown:
        emit("DRIFT", "REAL_STATES/NEVER_WRITTEN", "not a TaskState: %s" % " ".join(unknown))
    else:
        emit(
            "OK",
            "REAL_STATES/NEVER_WRITTEN",
            "partition the %d frozen states exactly" % len(all_states),
        )

# -- 3. the reason and provider enums --------------------------------------
compare_set("ParkReason", ts_union("ParkReason"), [p.value for p in S.ParkReason], "union")
compare_set(
    "ProviderStateName",
    ts_union("ProviderStateName"),
    [p.value for p in ProviderState],
    "union",
)

# -- 4. the pool naming rules ----------------------------------------------
# Derived from `pool_names_for` rather than written down: it is the function
# that decides what a pool is called, so its output is the authority. Every
# name is `<family>:<...>` except `global`.
families_py = set()
for pool in pool_names_for(
    tenant_id="parity",
    provider="anthropic",
    resource_class="standard",
    runner_profile="claude-code",
    backend="CLOUD_RUN_JOB",
):
    families_py.add(pool.split(":")[0] if ":" in pool else pool)

compare_set("PoolKind", ts_union("PoolKind"), families_py, "union")
compare_set("POOL_FAMILY_ORDER", ts_array("POOL_FAMILY_ORDER"), families_py, "array literal")

# `poolKind` parses the family off the name with a switch whose `default` is
# `global`. A family added to `pool_names_for` and not to the switch is not a
# parse error -- it is silently reported as the global pool, and `poolScope`
# then calls a per-tenant number platform-wide. That is a scope error dressed
# as a comparison, which the neighbouring comment in types.ts calls Trap E.
cases = ts_pool_kind_cases()
compare_set("poolKind cases", cases, families_py - set(["global"]), "switch")

# -- 5. WorkflowStep.input_from, a SHAPE rather than a value ---------------
# `codec.workflow_to_api` serves this field exactly as the frozen dataclass
# holds it, so the TypeScript declaration is a restatement of the frozen
# annotation. It was `string | null` against a `dict[str, str]` that is never a
# string and never null, and the fixtures supplied a string too -- so the screen
# was right in development and would have been wrong against every real
# response, which is the signature defect of this repository. One field, so the
# mapping from a Python annotation to its TypeScript spelling is a literal.
PY_TO_TS = {"dict[str, str]": "Record<string, string>"}

step_field = re.search(
    r"export interface WorkflowStep \{(.*?)\n\}", SRC, re.S
)
if step_field is None:
    emit("MISSING", "WorkflowStep", "no `export interface WorkflowStep` found")
else:
    declared = re.search(r"\n  input_from(\?)?:\s*([^\n]+)", step_field.group(1))
    frozen = None
    for field in dataclasses.fields(WorkflowStep):
        if field.name == "input_from":
            frozen = str(field.type)
    if declared is None:
        emit("MISSING", "WorkflowStep.input_from", "the field is not in the interface")
    elif frozen is None:
        emit("MISSING", "WorkflowStep.input_from", "the frozen dataclass no longer has it")
    elif frozen not in PY_TO_TS:
        emit(
            "MISSING",
            "WorkflowStep.input_from",
            "the frozen annotation is now %r and this probe has no TypeScript "
            "spelling for it; add one" % frozen,
        )
    else:
        want, got = PY_TO_TS[frozen], declared.group(2).strip()
        if declared.group(1):
            emit(
                "DRIFT",
                "WorkflowStep.input_from",
                "declared optional; the frozen field has a default_factory and is "
                "always served, so it is never absent",
            )
        elif got == want:
            emit("OK", "WorkflowStep.input_from", "%s matches %s" % (got, frozen))
        else:
            emit(
                "DRIFT",
                "WorkflowStep.input_from",
                "frozen %s means %s  //  types.ts says %s" % (frozen, want, got),
            )

print("\n".join(REPORT))
PY
)"; then
  err "the TypeScript parity probe failed to run:"
  printf '%s\n' "${TS_PARITY}" | sed 's/^/     /' >&2
  FAILED=1
else
  while IFS='|' read -r TS_STATUS TS_NAME TS_DETAIL; do
    [[ -n "${TS_STATUS}" ]] || continue
    case "${TS_STATUS}" in
      OK)
        ok "types.ts ${TS_NAME}: ${TS_DETAIL}"
        ;;
      MISSING)
        err "could not read ${TS_NAME} out of apps/swarm-ui/src/types.ts: ${TS_DETAIL}"
        err "The restatement moved or changed shape; update the probe so this keeps checking it."
        FAILED=1
        ;;
      DRIFT)
        err "types.ts ${TS_NAME} has DRIFTED from the frozen contract:"
        err "    ${TS_DETAIL}"
        err "Fix apps/swarm-ui/src/types.ts; the frozen module is the authority."
        FAILED=1
        ;;
      *)
        err "unexpected TypeScript parity output: ${TS_STATUS}|${TS_NAME}|${TS_DETAIL}"
        FAILED=1
        ;;
    esac
  done <<<"${TS_PARITY}"
fi

# --------------------------------------------------------------------------
# 6. The tenant NAMESPACE, which is not in the frozen contract at all.
# --------------------------------------------------------------------------
# THIS SECTION EXISTS BECAUSE THE VALUE HAS NO FROZEN HOME. Every other section
# above compares a copy against `swarm_common`; there is no
# `swarm_common.namespace_for(tenant)` to compare against, so seven components
# each wrote the prefix down and two of them wrote it down differently.
#
# What that cost, on 2026-09-23: `kubernetes/render.py` spelled it `swarm-` and
# `apps/scheduler/scheduler/dispatch.py` spelled it `swarm-tenant-`, so the
# provisioner created `swarm-eng` and the dispatcher created Jobs in
# `swarm-tenant-eng`, which did not exist. Kubernetes AUTHORISES BEFORE IT
# RESOLVES: a Job created into a namespace that is not there is reported as
#
#     jobs.batch is forbidden: User "..." cannot create resource "jobs" in API
#     group "batch" in the namespace "swarm-tenant-eng"
#
# -- a 403 naming a permission, never a 404 naming the namespace. Every
# `browser` task this platform had ever accepted (seven, over two days) failed
# that way and three separate investigations went at IAM. docs/gke-dispatch-403.md.
#
# THE AUTHORITY IS THE DISPATCHER, not this file and not the renderer: the
# scheduler is the component that actually creates the object, so whatever it
# spells is what has to exist. It is read out of the source as text rather than
# imported, for the reason sections 4 and 5 give -- this check must keep working
# with nothing but python3 and jq, in the `shell` CI job, with no scheduler
# dependencies installed.
#
# Two halves, because they catch different things:
#
#   6a. THE DECLARED PREFIXES -- every `namespace_prefix`-shaped default in the
#       repository. This is where the bug lived.
#   6b. THE NAMESPACE LITERALS -- every place a namespace VALUE is written out,
#       including test fixtures. A fixture that restates the prefix is the worst
#       case of all: it agrees with the bug and makes the suite prove it.
#
# Both halves REFUSE TO SKIP. A scan that finds fewer sites than it did when it
# was written reports that the restatements moved, rather than passing because
# it could no longer see them -- the failure mode section 4 already guards.
step "Tenant namespace parity"

if ! NS_PARITY="$(python3 - "${REPO_ROOT}" "${TENANT_NAMESPACE_PREFIX}" 2>&1 <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
shell_prefix = sys.argv[2]
REPORT = []


def emit(status, name, detail):
    REPORT.append("%s|%s|%s" % (status, name, detail))


# Directories that hold no source, plus one that holds source this lane may not
# edit. See the swarm-ui note at 6b.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", ".mypy_cache",
             "__pycache__", ".pytest_cache", ".terraform", ".claude"}
SKIP_SUFFIXES = {".md", ".lock", ".png", ".svg", ".ico", ".woff", ".woff2"}


# This file describes the patterns it looks for, in prose and in regex
# literals, so scanning it finds itself: the sentence explaining what a
# namespace literal looks like IS a namespace literal. The value it would
# otherwise contribute -- the shell prefix -- arrives as argv[2] instead, so
# nothing is lost by leaving it out.
SELF = (root / "scripts" / "lib" / "check-contract-parity.sh").resolve()


def sources():
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.resolve() == SELF:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        try:
            yield path, path.read_text()
        except (UnicodeDecodeError, OSError):
            continue


FILES = list(sources())

# -- the authority ---------------------------------------------------------
# `namespace_template: str = "swarm-tenant-{tenant}"` in the dispatcher. The
# prefix is whatever precedes the interpolation.
dispatch = (root / "apps" / "scheduler" / "scheduler" / "dispatch.py").read_text()
templates = re.findall(r"namespace_template[^=\n]*=\s*\"([^\"]*\{tenant\})\"", dispatch)
# The fallback inside `namespace_for` is a second literal of the same template
# and is included deliberately: it is what is used when no GkeTarget was built.
templates += re.findall(r"else\s+\"([^\"]*\{tenant\})\"", dispatch)
prefixes = set(t.split("{tenant}")[0] for t in templates)

if not templates:
    emit("MISSING", "GkeTarget.namespace_template",
         "no `namespace_template = \"...{tenant}\"` in apps/scheduler/scheduler/dispatch.py")
    print("\n".join(REPORT))
    raise SystemExit(0)
if len(prefixes) != 1:
    emit("DRIFT", "GkeTarget.namespace_template",
         "the dispatcher itself spells the namespace %d ways: %s"
         % (len(prefixes), " ".join(sorted(prefixes))))
    print("\n".join(REPORT))
    raise SystemExit(0)

AUTHORITY = prefixes.pop()
emit("OK", "authority",
     "the scheduler dispatches into %s<tenant> (%d template literal(s) agree)"
     % (AUTHORITY, len(templates)))

if shell_prefix != AUTHORITY:
    emit("DRIFT", "TENANT_NAMESPACE_PREFIX in scripts/lib/common.sh",
         "shell says %s, the scheduler dispatches into %s" % (shell_prefix, AUTHORITY))
else:
    emit("OK", "TENANT_NAMESPACE_PREFIX in scripts/lib/common.sh",
         "%s, same as the scheduler" % shell_prefix)

# -- 6a. declared prefixes -------------------------------------------------
# Python defaults, the renderer constant and the terraform variable default.
# Each regex is anchored on the IDENTIFIER rather than on a file path, so a new
# component that names its setting the same way is picked up without anyone
# remembering that this file exists.
DECLARATIONS = (
    (r"^\s*(?:tenant_)?namespace_prefix\s*:\s*str\s*=\s*\"([^\"]*)\"", "python default"),
    (r"^\s*NAMESPACE_PREFIX\s*=\s*\"([^\"]*)\"", "renderer constant"),
    (r"os\.environ\.get\(\s*\n?\s*\"TENANT_NAMESPACE_PREFIX\",\s*\"([^\"]*)\"", "env default"),
    (r"variable\s+\"namespace_prefix\"\s*\{[^}]*?default\s*=\s*\"([^\"]*)\"", "terraform default"),
    (r"^\s*tenant_namespace_prefix\s*=\s*\"([^\"]*)\"", "test/tfvars literal"),
)

declared = []
for path, text in FILES:
    rel = path.relative_to(root)
    for pattern, kind in DECLARATIONS:
        for match in re.finditer(pattern, text, re.M | re.S):
            line = text.count("\n", 0, match.start()) + 1
            declared.append((str(rel), line, kind, match.group(1)))

# The count when this was written. Lower means a restatement moved or was
# renamed and this scan stopped seeing it, which is not the same as agreement.
MIN_DECLARATIONS = 7
if len(declared) < MIN_DECLARATIONS:
    emit("MISSING", "declared namespace prefixes",
         "found %d, expected at least %d; a restatement moved or was renamed -- "
         "update the DECLARATIONS patterns so this keeps checking it"
         % (len(declared), MIN_DECLARATIONS))
else:
    wrong = [d for d in declared if d[3] != AUTHORITY]
    if wrong:
        for rel, line, kind, value in wrong:
            emit("DRIFT", "declared namespace prefix",
                 "%s:%d (%s) says %s, the scheduler dispatches into %s"
                 % (rel, line, kind, value, AUTHORITY))
    else:
        emit("OK", "declared namespace prefixes",
             "%d declaration(s) all spell it %s" % (len(declared), AUTHORITY))

# -- 6b. namespace literals ------------------------------------------------
# Anything of the form `namespace = "swarm-..."`, `"namespace": f"swarm-..."`,
# `namespaces["eng"] == "swarm-..."`. The literal has to FOLLOW the identifier,
# so an unrelated `swarm-` string elsewhere on the line is not matched.
#
# `# namespace-prefix-exempt:` on the line opts one out, for the negative tests
# that feed the WRONG spelling in on purpose. The marker carries a reason, and
# it is a line comment in every language scanned here.
#
# apps/swarm-ui IS SCANNED AND ITS FAILURES ARE REPORTED, not excluded: as of
# 2026-09-24 two browser-side fixtures still carry `swarm-u-bogdan`
# (src/api.ts and src/__tests__/brand.test.tsx). They are display-only mock
# data and cannot dispatch anything, but an exclusion with a comment is how a
# deficit becomes permanent, so they are listed below as ADVISORY -- printed on
# every run, failing nothing, until the lane that owns that directory fixes
# them and the ADVISORY branch here is deleted.
ADVISORY_PREFIX = "apps/swarm-ui/"
# The apostrophe is BUILT, not written. This probe is a heredoc inside a
# command substitution and bash scans that body for quote characters while it
# looks for the closing paren, so a single one here truncates the script -- the
# note in section 5 above records the unhelpful error that produces.
_Q = chr(39)
LITERAL = re.compile(
    r"namespaces?[\"\]]*\s*(?:==|=|:)\s*f?[\"" + _Q + r"]([a-z0-9][a-z0-9.\-{}_]*)[\"" + _Q + r"]"
)

literals = []
for path, text in FILES:
    rel = str(path.relative_to(root))
    for number, line in enumerate(text.splitlines(), start=1):
        if "namespace-prefix-exempt" in line:
            continue
        for match in LITERAL.finditer(line):
            value = match.group(1)
            # Only namespaces this platform owns. `finance-prod` in a
            # negative test belongs to another team and must not match.
            if not value.startswith("swarm-"):
                continue
            literals.append((rel, number, value))

MIN_LITERALS = 8
if len(literals) < MIN_LITERALS:
    emit("MISSING", "namespace literals",
         "found %d, expected at least %d; the literals moved or changed shape -- "
         "update the LITERAL pattern so this keeps checking them"
         % (len(literals), MIN_LITERALS))
else:
    bad = [lit for lit in literals if not lit[2].startswith(AUTHORITY)]
    hard = [lit for lit in bad if not lit[0].startswith(ADVISORY_PREFIX)]
    soft = [lit for lit in bad if lit[0].startswith(ADVISORY_PREFIX)]
    for rel, number, value in hard:
        emit("DRIFT", "namespace literal",
             "%s:%d is %s; the scheduler dispatches into %s<tenant>, so this "
             "names a namespace nothing creates" % (rel, number, value, AUTHORITY))
    for rel, number, value in soft:
        emit("ADVISORY", "namespace literal",
             "%s:%d is %s -- browser-side mock data, owned by the UI lane, "
             "dispatches nothing" % (rel, number, value))
    if not hard:
        emit("OK", "namespace literals",
             "%d of %d literal(s) start with %s; %d advisory"
             % (len(literals) - len(bad), len(literals), AUTHORITY, len(soft)))

print("\n".join(REPORT))
PY
)"; then
  err "the tenant-namespace parity probe failed to run:"
  printf '%s\n' "${NS_PARITY}" | sed 's/^/     /' >&2
  FAILED=1
else
  # Counted, and the count is printed. A `while read` over an empty string
  # reports nothing and reads exactly like a clean sweep -- CLAUDE.md rule zero.
  NS_SEEN=0
  while IFS='|' read -r NS_STATUS NS_NAME NS_DETAIL; do
    [[ -n "${NS_STATUS}" ]] || continue
    NS_SEEN=$(( NS_SEEN + 1 ))
    case "${NS_STATUS}" in
      OK)       ok "namespace ${NS_NAME}: ${NS_DETAIL}" ;;
      ADVISORY) warn "namespace ${NS_NAME}: ${NS_DETAIL}" ;;
      MISSING)
        err "the tenant-namespace scan lost sight of what it compares: ${NS_DETAIL}"
        err "A scan that passes because it stopped looking reports an agreement it never established."
        FAILED=1
        ;;
      DRIFT)
        err "${NS_NAME} has DRIFTED: ${NS_DETAIL}"
        err "See docs/gke-dispatch-403.md -- the failure this produces is a 403, not a 404."
        FAILED=1
        ;;
      *)
        err "unexpected tenant-namespace parity output: ${NS_STATUS}|${NS_NAME}|${NS_DETAIL}"
        FAILED=1
        ;;
    esac
  done <<<"${NS_PARITY}"
  if [[ "${NS_SEEN}" -eq 0 ]]; then
    err "the tenant-namespace probe printed no assertions at all; nothing was checked"
    FAILED=1
  fi
fi

hr
[[ "${FAILED}" -eq 0 ]] || die "a restatement of the frozen contract has drifted"
ok "every restatement of the frozen contract still matches it"
