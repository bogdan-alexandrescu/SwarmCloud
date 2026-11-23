#!/usr/bin/env bash
# THE COVERAGE RATCHET (issue 64): combine the nightly run's shards' coverage
# data and fail when a package's line coverage is under its floor in
# coverage-floors.json (docs/ci.md, "Coverage floors").
#
#   scripts/coverage-floors.sh [--python-data DIR] [--expect-python-shards N]
#                              [--ui-data DIR] [--expect-ui-shards N]
#                              [--floors FILE] [--keep-reports DIR]
#
# --python-data: a directory holding each unit shard's coverage.py data file
#   (`*.coverage`, written by pytest-cov with COVERAGE_FILE); they are
#   combined with `coverage combine` -- one run of the whole suite, never a
#   second -- and grouped per `apps/<dir>`.
# --ui-data: a directory holding each swarm-ui shard's istanbul
#   `coverage-final.json` (vitest's v8 provider), merged statement by statement.
# --expect-*-shards: how many shards ran; fewer data files than that fails
#   here, by name, instead of reading a partial run as fallen coverage.
# --floors: default coverage-floors.json at the repository root.
# --keep-reports: copy the per-package reports (python.json, ui.json) there,
#   for `coverage_floors.py fill` to set floors from (docs/ci.md, "Coverage
#   floors").
#
# A suite whose data is not given is not judged (its shards did not run the
# whole suite here). Exit 1 when a package is below its floor or missing from
# a report that ran; 2 when the floors file is malformed. The `judge` job of
# the nightly .github/workflows/coverage.yml runs this; locally it needs the
# same data files, so it is a CI step.
# It reads no project and no credential and prints only coverage numbers.

set -euo pipefail
# This script touches no project: it reads local coverage data. It says so, so
# common.sh does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

floors="${REPO_ROOT}/coverage-floors.json"
python_data=""
ui_data=""
expect_python=""
expect_ui=""
keep=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --python-data) python_data="${2:?--python-data needs a directory}"; shift 2 ;;
    --ui-data) ui_data="${2:?--ui-data needs a directory}"; shift 2 ;;
    --expect-python-shards) expect_python="${2:?--expect-python-shards needs a number}"; shift 2 ;;
    --expect-ui-shards) expect_ui="${2:?--expect-ui-shards needs a number}"; shift 2 ;;
    --floors) floors="${2:?--floors needs a file}"; shift 2 ;;
    --keep-reports) keep="${2:?--keep-reports needs a directory}"; shift 2 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) die "coverage-floors.sh: unknown argument $1 (see --help)" ;;
  esac
done

lib="${SWARM_LIB_DIR}/coverage_floors.py"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# Every data file named NAME under DIR, sorted, one a line, into FILE; then
# fail unless there are as many as the shards that ran.
collect() {
  local dir="$1" name="$2" expect="$3" out="$4" count
  [ -d "${dir}" ] || die "coverage-floors.sh: ${dir} is not a directory"
  find "${dir}" -type f -name "${name}" | LC_ALL=C sort > "${out}"
  count="$(grep -c . "${out}" || true)"
  echo "found ${count} data file(s) named ${name} under ${dir}"
  if [ "${count}" -eq 0 ]; then
    die "coverage-floors.sh: no ${name} under ${dir}"
  fi
  if [ -n "${expect}" ] && [ "${count}" -ne "${expect}" ]; then
    die "coverage-floors.sh: ${count} of ${expect} shards left coverage data under ${dir}; a partial run is not a measurement"
  fi
}

reports=()
if [ -n "${python_data}" ]; then
  collect "${python_data}" '*.coverage' "${expect_python}" "${work}/python-files"
  files=()
  while IFS= read -r file; do
    files+=("${file}")
  done < "${work}/python-files"
  # From the repository root: coverage reads its [tool.coverage] settings from
  # pyproject.toml there, and `coverage json` re-reads the measured sources.
  (
    cd "${REPO_ROOT}"
    uv run --frozen coverage combine --keep --quiet --data-file="${work}/python.coverage" "${files[@]}"
    uv run --frozen coverage json --quiet --data-file="${work}/python.coverage" -o "${work}/coverage.json"
  )
  python3 "${lib}" python-report --json "${work}/coverage.json" --out "${work}/python.json"
  reports+=(--report "${work}/python.json")
fi

if [ -n "${ui_data}" ]; then
  collect "${ui_data}" 'coverage-final.json' "${expect_ui}" "${work}/ui-files"
  files=()
  while IFS= read -r file; do
    files+=("${file}")
  done < "${work}/ui-files"
  python3 "${lib}" ui-report --out "${work}/ui.json" "${files[@]}"
  reports+=(--report "${work}/ui.json")
fi

[ "${#reports[@]}" -gt 0 ] || die "coverage-floors.sh: give --python-data, --ui-data or both"

if [ -n "${keep}" ]; then
  mkdir -p "${keep}"
  for report in "${work}/python.json" "${work}/ui.json"; do
    if [ -f "${report}" ]; then
      cp "${report}" "${keep}/"
    fi
  done
fi

python3 "${lib}" check --floors "${floors}" ${reports[@]+"${reports[@]}"}
