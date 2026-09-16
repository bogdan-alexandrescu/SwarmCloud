#!/usr/bin/env bash
# Validate every Kubernetes manifest this repository can produce.
#
# The manifests in kubernetes/ are TEMPLATES with `__TOKEN__` placeholders, not
# applyable YAML -- `__SECRET_ENV__` is a structural block that is several lines
# or nothing at all, so a raw template does not even parse as YAML. Running
# `kubectl apply --dry-run` over the files on disk therefore reports errors that
# are not errors, which is worse than not checking at all: it trains people to
# ignore the output.
#
# So this renders them with kubernetes/render.py -- the same renderer an operator
# uses, reading sizing from the frozen catalogue -- and validates the result
# against a pinned kubectl (old clients silently DROP fields they do not
# understand, so a manifest can "validate" while losing its security context).
#
# NOTE ON SHAPE: every check writes its render to a file and is invoked as a
# plain command, never `render | check`. A function on the right of a pipe runs
# in a SUBSHELL, so a failure count incremented there is discarded and the
# script reports success while printing errors. That bug was real here once.
#
# Usage: scripts/lib/validate-manifests.sh [--tenant lint]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

TENANT="lint"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)  TENANT="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

RENDER="${REPO_ROOT}/kubernetes/render.py"
[[ -f "${RENDER}" ]] || die "no ${RENDER}"

require_cmd python3
KB="$(kubectl_bin)"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-manifests.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM

FAILED=0

# check DESCRIPTION -- COMMAND...
# Renders with COMMAND, then validates. Runs in THIS shell, so FAILED sticks.
check() {
  local description="$1"; shift
  local out="${WORK}/render.yaml"
  local errfile="${WORK}/render.err"

  if ! "$@" >"${out}" 2>"${errfile}"; then
    err "${description}: render failed"
    sed 's/^/    /' <"${errfile}" >&2
    FAILED=1
    return 0
  fi

  # render.py refuses to emit an unrendered placeholder; this is a second,
  # independent check on the same property, because a placeholder that survives
  # into a cluster is a pod running with a literal "__TENANT_ID__".
  if grep -qE '__[A-Z0-9_]+__' "${out}"; then
    err "${description}: unrendered placeholder(s):"
    grep -oE '__[A-Z0-9_]+__' "${out}" | sort -u | sed 's/^/    /' >&2
    FAILED=1
    return 0
  fi

  if ! "${KB}" apply --dry-run=client -f "${out}" -o name >"${WORK}/names" 2>&1; then
    err "${description}:"
    sed 's/^/    /' <"${WORK}/names" >&2
    FAILED=1
    return 0
  fi

  ok "${description} ($(grep -c . <"${WORK}/names") objects)"
}

check "cluster policies"            python3 "${RENDER}" policies
check "tenant namespace (${TENANT})" python3 "${RENDER}" tenant --tenant "${TENANT}"

# One Job per runner profile, because the profiles differ in exactly the fields
# that are easy to get wrong: image, resource class, secret block and backend.
PROFILES="$(python3 - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common.profiles import RUNNER_PROFILES
print(" ".join(sorted(RUNNER_PROFILES)))
PY
)"
[[ -n "${PROFILES}" ]] || die "could not read RUNNER_PROFILES from the frozen catalogue"

for profile in ${PROFILES}; do
  check "worker job (${profile})" \
    python3 "${RENDER}" job \
      --tenant "${TENANT}" \
      --profile "${profile}" \
      --task "tsk_lint" \
      --attempt "att_lint" \
      --lease "lease_lint" \
      --generation 1
done

if [[ "${FAILED}" -ne 0 ]]; then
  die "kubernetes manifest validation failed"
fi
ok "every rendered manifest is valid"
