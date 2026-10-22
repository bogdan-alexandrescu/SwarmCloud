#!/usr/bin/env bash
# related-tests: name the test files a change reaches, and the runs of named
# files that test it -- never a whole suite, a directory or a shard.
#
#   scripts/related-tests.sh [--base <rev>] [--files <file>|-] [--run]
#
# WHY (RELATED-TESTS, part of #1694, owner decision 2026-10-20). Agents ran
# whole suites and lost hours. Measured 2026-10-19/20: 36 vitest runs with no
# file filter, about 22 min each on 4 cores (2.05 h); 8 pytest runs over whole
# tests/unit directories (0.73 h); and both out-of-memory kills came from
# `for i in 1 2 3 4; do npx vitest run --shard=$i/4 & done`. So a SwarmCloud
# agent runs the tests related to what it changed through this script while it
# works, and scripts/changed-guards.sh once at the end (CLAUDE.md, the
# claude-code runner's RELATED_TESTS_NOTE). CI is the gate for the rest.
#
# What is related is decided by scripts/lib/related_tests.py: the pytest files
# that import a changed Python module (transitively), name a changed path, or
# are themselves changed -- select_unit_tests.py's rules, which CI uses -- and
# the vitest files that reach a changed swarm-ui module through its imports,
# the graph `vitest related` follows. A change to any apps/swarm-ui/**.css also
# selects the stylesheet guards, once (STYLESHEET_GUARDS there, the one list):
# they read every sheet from disk, so no import reaches them, and PR 1905's
# lane passed this without running them (RELATED-CSS-GUARDS, part of #1914).
# A selection too big for one run comes
# back as a few runs of named files (at most ~540 CI-seconds of pytest, or 50
# vitest files, each), printed to run ONE AFTER ANOTHER in the foreground.
#
# The diff is changed-guards.sh's: `git diff --name-only <base>...HEAD` (the
# merge base) plus uncommitted and untracked files. <base> is --base, else
# SWARM_CLONE_BASE, else origin/main; an unresolvable base exits 2 rather than
# reading as an empty diff. `--files` reads the paths from a file (or stdin).
#
# stdout: the related test files, repo-relative, one a line; then one command
# a line. `--run` also runs those commands in turn and stops at the first red.
#
#   exit 0  nothing related, or the selection was printed (and with --run, passed)
#   exit 1  with --run, a run failed or ran out of time
#   exit 2  usage error, or the diff could not be read

set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SELECTOR="scripts/lib/related_tests.py"
UI_DIR="apps/swarm-ui"
# Each run's limit with --run: changed-guards.sh's PYTEST_SECONDS, under the
# 10 minutes an agent's command may take.
RUN_SECONDS=540

usage() {
  sed -n '5p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2
}

base="${SWARM_CLONE_BASE:-origin/main}"
files_from=""
run=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      base="$2"; shift 2 ;;
    --files)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      files_from="$2"; shift 2 ;;
    --run) run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown option: $1"; usage; exit 2 ;;
  esac
done

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

if [[ -n "${files_from}" ]]; then
  source_name="${files_from}"
  if [[ "${files_from}" == "-" ]]; then
    cat >"${work}/raw"
  elif [[ -r "${files_from}" ]]; then
    cat -- "${files_from}" >"${work}/raw"
  else
    err "cannot read the changed-path list ${files_from}"
    exit 2
  fi
else
  source_name="${base}...HEAD and the work tree"
  if ! git -C "${REPO_ROOT}" rev-parse --verify -q "${base}^{commit}" >/dev/null; then
    err "base ${base} does not resolve to a commit; pass --base <rev> (refusing to read it as an empty diff)"
    exit 2
  fi
  {
    git -C "${REPO_ROOT}" diff --name-only "${base}...HEAD"
    git -C "${REPO_ROOT}" diff --name-only HEAD
    git -C "${REPO_ROOT}" ls-files --others --exclude-standard
  } >"${work}/raw" || { err "git could not list the changed paths"; exit 2; }
fi

sed -e 's/\r$//' -e '/^[[:space:]]*$/d' "${work}/raw" | sort -u >"${work}/changed"
changed_count="$(wc -l <"${work}/changed" | tr -d ' ')"
if [[ "${changed_count}" -eq 0 ]]; then
  info "nothing changed in ${source_name}: no related test to run"
  exit 0
fi

# Standard library only (like select_unit_tests.py, which CI runs before any
# sync), so the system python3 is enough.
require_cmd python3
python3 "${REPO_ROOT}/${SELECTOR}" --root "${REPO_ROOT}" --changed "${work}/changed" >"${work}/runs" \
  || { err "${SELECTOR} could not select the related tests"; exit 2; }

kinds=()
runs=()
listed=()
while IFS=$'\t' read -r kind rest; do
  case "${kind}" in
    note) warn "${rest}" ;;
    pytest|vitest)
      kinds+=("${kind}")
      runs+=("${rest}")
      read -r -a run_files <<<"${rest}"
      for f in ${run_files[@]+"${run_files[@]}"}; do
        if [[ "${kind}" == vitest ]]; then
          listed+=("${UI_DIR}/${f}")
        else
          listed+=("${f}")
        fi
      done ;;
  esac
done <"${work}/runs"

if [[ "${#runs[@]}" -eq 0 ]]; then
  info "${changed_count} changed path(s) in ${source_name}; no test file imports or names them: nothing to run here (scripts/changed-guards.sh and CI still run their guards)"
  exit 0
fi

command_of() {
  # $1 kind, $2 space-separated files. Paths in this repository hold no spaces.
  if [[ "$1" == pytest ]]; then
    printf 'uv run pytest -q -n auto %s' "$2"
  else
    printf '(cd %s && npx vitest run %s)' "${UI_DIR}" "$2"
  fi
}

info "${changed_count} changed path(s) in ${source_name}; ${#listed[@]} related test file(s) in ${#runs[@]} run(s), to run one after another:"
printf '%s\n' "${listed[@]}"
for i in "${!runs[@]}"; do
  command_of "${kinds[$i]}" "${runs[$i]}"
  printf '\n'
done

[[ "${run}" -eq 1 ]] || exit 0

limit=()
if command -v timeout >/dev/null 2>&1; then
  limit=(timeout "${RUN_SECONDS}")
fi
for i in "${!runs[@]}"; do
  read -r -a run_files <<<"${runs[$i]}"
  info "run $((i + 1)) of ${#runs[@]}: ${kinds[$i]}, ${#run_files[@]} file(s)"
  status=0
  if [[ "${kinds[$i]}" == pytest ]]; then
    (cd "${REPO_ROOT}" && ${limit[@]+"${limit[@]}"} uv run pytest -q -n auto "${run_files[@]}") || status=$?
  else
    (cd "${REPO_ROOT}/${UI_DIR}" && ${limit[@]+"${limit[@]}"} npx vitest run "${run_files[@]}") || status=$?
  fi
  if [[ "${status}" -eq 124 ]]; then
    err "run $((i + 1)) ran past ${RUN_SECONDS} s; it is not re-run as-is: narrow it to the files you changed"
    exit 1
  elif [[ "${status}" -ne 0 ]]; then
    err "run $((i + 1)) failed (exit ${status}); fix it before the next run"
    exit 1
  fi
done
ok "every related test passed (${#listed[@]} file(s), ${#runs[@]} run(s))"
