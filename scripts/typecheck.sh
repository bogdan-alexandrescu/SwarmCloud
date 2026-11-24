#!/usr/bin/env bash
# typecheck: run pyright (basic mode) over the Python apps and fail only on the
# errors a change adds over the per-file baseline.
#
#   scripts/typecheck.sh [<file>...]
#       Check the tree [tool.pyright] in pyproject.toml includes, or only the
#       named files, and compare with scripts/lib/pyright-baseline.json.
#
#   scripts/typecheck.sh --write-baseline
#       Rewrite the baseline from a full run. Only to SHRINK it after fixing
#       errors, or after a deliberate PYRIGHT_VERSION bump: review shows any
#       file or count it adds, and tests/unit/scripts/test_typecheck.py holds
#       the total under its ceiling.
#
# WHY (audit "add a type checker (pyright basic) with a baseline", part of
# #55). The apps had no type checker; measured 2026-11-22, pyright 1.1.414 in
# basic mode reports 1,245 errors in 251 of the 718 files under apps/, mostly
# reportArgumentType and reportOptionalMemberAccess. Fixing them all first
# would conflict with every open lane, so the baseline records today's errors
# per (file, rule) and only an error beyond a file's count for that rule fails
# (ruff's BLE001 baseline had this per-file shape until issue 185 moved it to
# an inline marker per site). A file the baseline does not name must be clean. scripts/lib/pyright_baseline.py
# does the comparison and says why the identity is (file, rule), not the line.
#
# The configuration is [tool.pyright] in the root pyproject.toml (one place).
# pyright is pinned to PYRIGHT_VERSION exactly and runs as a uv tool
# (`uv tool run`, with the [nodejs] extra so it brings its own Node), not as a
# dev dependency: it adds nothing to uv.lock, which CI installs from. It
# resolves imports through the project venv, synced as CI's Python jobs sync it
# (`--frozen --all-extras --dev`); a different venv would resolve a different
# set of imports and report a different set of errors than the baseline holds.
# A full run takes about 3 minutes on 4 cores; named files take seconds.
#
# stdout: one line per error of a (file, rule) over its allowance,
# `path:line:col: rule: message`, redacted (common.sh's redact). stderr: the
# counts compared, so an empty stdout is visibly a comparison.
#
#   exit 0  no error beyond the baseline
#   exit 1  at least one (file, rule) over the baseline (listed)
#   exit 2  usage error, or pyright could not run or checked nothing

set -euo pipefail

# This script touches no project: it type-checks the checkout. It says so, so
# common.sh does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# Exact pin; tests/unit/scripts/test_typecheck.py holds it equal to the
# baseline's "pyright", so a bump regenerates the baseline in the same change.
PYRIGHT_VERSION="1.1.414"
BASELINE="${REPO_ROOT}/scripts/lib/pyright-baseline.json"
COMPARE="${REPO_ROOT}/scripts/lib/pyright_baseline.py"

usage() { awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0" >&2; exit 2; }

write=0
files=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --write-baseline) write=1 ;;
    -h|--help) usage ;;
    --) shift; files+=("$@"); break ;;
    -*) err "unknown option: $1"; usage ;;
    *) files+=("$1") ;;
  esac
  shift
done
if [[ "${write}" -eq 1 && "${#files[@]}" -gt 0 ]]; then
  err "--write-baseline takes no files: the baseline is written from a full run"
  exit 2
fi

require_cmd uv python3

work="$(mktemp -d "${TMPDIR:-/tmp}/typecheck.XXXXXX")"
trap 'rm -rf "${work}"' EXIT

cd "${REPO_ROOT}"
# The venv pyright resolves imports through, synced from uv.lock as CI does.
uv run --frozen --all-extras --dev --quiet \
  python -c 'import sys; print(sys.executable)' >"${work}/python" \
  || { err "could not sync the project venv from uv.lock"; exit 2; }
python_bin="$(cat "${work}/python")"

info "pyright ${PYRIGHT_VERSION} (basic, [tool.pyright] in pyproject.toml) on ${#files[@]} named file(s), 0 meaning the configured tree"
rc=0
uv tool run --quiet --from "pyright[nodejs]==${PYRIGHT_VERSION}" \
  pyright --outputjson --pythonpath "${python_bin}" ${files[@]+"${files[@]}"} \
  >"${work}/report.json" 2>"${work}/pyright.err" || rc=$?
# pyright: 0 no errors, 1 errors reported; anything else is a failure to run.
if [[ "${rc}" -gt 1 ]]; then
  err "pyright exited ${rc}:"
  redact <"${work}/pyright.err" >&2
  exit 2
fi

if [[ "${write}" -eq 1 ]]; then
  python3 "${COMPARE}" write --root "${REPO_ROOT}" --report "${work}/report.json" \
    --baseline "${BASELINE}" --version "${PYRIGHT_VERSION}"
  exit 0
fi

partial=()
[[ "${#files[@]}" -gt 0 ]] && partial=(--partial)
rc=0
python3 "${COMPARE}" compare --root "${REPO_ROOT}" --report "${work}/report.json" \
  --baseline "${BASELINE}" ${partial[@]+"${partial[@]}"} >"${work}/new" || rc=$?
redact <"${work}/new"
exit "${rc}"
