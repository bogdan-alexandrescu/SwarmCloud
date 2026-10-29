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
# Any change under apps/swarm-ui/ also runs the package's node:test runners,
# `(cd apps/swarm-ui && npm run test:node)`, once and after the vitest runs:
# test/*.test.mjs and tests/run.mjs read the tree from disk, so no import
# reaches them, and CI's ui-build runs them. Four lanes (PRs #2429, #2430,
# #2433, #2437) never ran them and went red there on no-company-name.test.mjs
# (#2455).
# A selection too big for one run comes
# back as a few runs of named files, printed to run ONE AFTER ANOTHER in the
# foreground: at most ~540 CI-seconds of pytest (unit_test_durations.json), and
# at most ~540 CI-seconds and 50 files of vitest, by
# tests/unit/scripts/ui_test_durations.json; a file it does not record weighs
# 2.9 s (the whole UI suite, 463 files in ~22 min on 4 cores), and a file
# heavier than 540 s runs alone. WEIGHTED-VITEST (part of #2362): cut by file
# count alone, the nine spacing.*.test.tsx slices (252-485 s each) shared one
# 50-file run that could not finish inside RUN_SECONDS, and 40+ agents timed
# out in 2026-10-25/26. scripts/refresh-ui-weights.sh rewrites the weights
# from the logs of main's latest green application.yml run.
#
# SPACING-NARROW (part of #2362): by import, every change under App.tsx reached
# every spacing slice, since spacingsweep.tsx imports {App, SECTIONS} from
# '../App'. A slice renders only its SPACING_GROUPS routes, so it is kept when
# the change reaches a module one of its routes' SectionBody cases renders, or
# reaches the slice other than through an App -> screen import (App.tsx, the
# shell, the sweep, spaceprobe). A changed sheet, or an App.tsx or sweep that
# does not parse, keeps every slice. docs/ci.md says how the routes are read.
#
# The diff is changed-guards.sh's: `git diff --name-only <base>...HEAD` (the
# merge base) plus uncommitted and untracked files. <base> is --base, else
# SWARM_CLONE_BASE, else origin/main; an unresolvable base exits 2 rather than
# reading as an empty diff. `--files` reads the paths from a file (or stdin).
#
# stdout: the related test files, repo-relative, one a line; then one command
# a line. `--run` also runs those commands in turn and stops at the first red.
#
# TEST-OUTPUT (TOKEN-A, part of #2524, BGD 2026-10-27). 97% of the pool's
# tokens in 2026-10-20..27 were cache reads, and a whole test transcript in a
# Bash result is re-read on every later turn. So with --run each run's full
# output goes to a log, redacted (common.sh's redact: the artifacts outlive
# the lane) -- under $SWARM_ARTIFACTS_DIR in a lane, else
# ${TMPDIR:-/tmp} -- and only its last TAIL_LINES lines are printed, after the
# failure blocks (pytest's FAILURES/ERRORS, vitest's Failed Tests/Suites, a
# node:test `not ok`) when it failed, at most BLOCK_LINES of them; the log is
# named. vitest runs with --reporter=dot: a dot a test, its failures in full.
# changed-guards.sh already keeps its runs' logs and prints their tails.
#
#   exit 0  nothing related, or the selection was printed (and with --run, passed)
#   exit 1  with --run, a run failed or ran out of time
#   exit 2  usage error, or the diff could not be read

set -euo pipefail

# This script touches no project: it reads the tree. It says so, so
# common.sh (no default project, DAYONE-DESAGA-2) does not stop on an unset
# PROJECT_ID. Not exported: the tests still run in the caller's environment.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SELECTOR="scripts/lib/related_tests.py"
UI_DIR="apps/swarm-ui"
# Each run's limit with --run: changed-guards.sh's PYTEST_SECONDS, under the
# 10 minutes an agent's command may take.
RUN_SECONDS=540
# What --run prints of each run's log (TEST-OUTPUT above).
TAIL_LINES=25
BLOCK_LINES=200
FAILURE_HEADER='^=+ (FAILURES|ERRORS) =+$|Failed (Tests|Suites) [0-9]+|^not ok '

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
    node)
      # rest is the package directory; the runner picks its own files.
      kinds+=("${kind}")
      runs+=("${rest}") ;;
  esac
done <"${work}/runs"

if [[ "${#runs[@]}" -eq 0 ]]; then
  info "${changed_count} changed path(s) in ${source_name}; no test file imports or names them: nothing to run here (scripts/changed-guards.sh and CI still run their guards)"
  exit 0
fi

command_of() {
  # $1 kind, $2 space-separated files (for node, the package directory).
  # Paths in this repository hold no spaces.
  if [[ "$1" == pytest ]]; then
    printf 'uv run pytest -q -n auto %s' "$2"
  elif [[ "$1" == node ]]; then
    printf '(cd %s && npm run test:node)' "$2"
  else
    printf '(cd %s && npx vitest run --reporter=dot %s)' "${UI_DIR}" "$2"
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
log_dir="${SWARM_ARTIFACTS_DIR:-${TMPDIR:-/tmp}}"
[[ -d "${log_dir}" && -w "${log_dir}" ]] || log_dir="${TMPDIR:-/tmp}"
log_stem="$(mktemp "${log_dir}/related-tests.XXXXXX")"
# Only its name is used: the logs are ${log_stem}.runN.log.
trap 'rm -rf "${work}"; rm -f "${log_stem}"' EXIT

show_log() {
  # $1 the run's log, $2 its exit status. The failure blocks first (failed
  # runs only, at most BLOCK_LINES), then the tail; a block that reaches the
  # tail is printed once, to the end.
  local total start
  total="$(wc -l <"$1" | tr -d ' ')"
  start=0
  if [[ "$2" -ne 0 ]]; then
    start="$(grep -n -m 1 -E "${FAILURE_HEADER}" "$1" | cut -d: -f1 || true)"
    start="${start:-0}"
  fi
  if [[ "${start}" -gt 0 && $((total - start + 1)) -le $((BLOCK_LINES + TAIL_LINES)) ]]; then
    tail -n "+${start}" "$1" | redact
  else
    if [[ "${start}" -gt 0 ]]; then
      tail -n "+${start}" "$1" | head -n "${BLOCK_LINES}" | redact
      printf '... (%s more line(s) in %s)\n' "$((total - start + 1 - BLOCK_LINES - TAIL_LINES))" "$1"
    fi
    tail -n "${TAIL_LINES}" "$1" | redact
  fi
}

for i in "${!runs[@]}"; do
  read -r -a run_files <<<"${runs[$i]}"
  status=0
  log="${log_stem}.run$((i + 1)).log"
  # The run writes to a private file under ${work} (removed on exit); only
  # its redacted copy is kept at ${log}, because ${log} sits in the lane's
  # artifacts, which outlive the lane and are read by others. Through
  # common.sh's redact, the same filter the printed tail goes through.
  raw="${work}/run$((i + 1)).raw"
  if [[ "${kinds[$i]}" == node ]]; then
    info "run $((i + 1)) of ${#runs[@]}: node, npm run test:node in ${runs[$i]}"
    (cd "${REPO_ROOT}/${runs[$i]}" && ${limit[@]+"${limit[@]}"} npm run test:node) >"${raw}" 2>&1 || status=$?
  elif [[ "${kinds[$i]}" == pytest ]]; then
    info "run $((i + 1)) of ${#runs[@]}: pytest, ${#run_files[@]} file(s)"
    (cd "${REPO_ROOT}" && ${limit[@]+"${limit[@]}"} uv run pytest -q -n auto "${run_files[@]}") >"${raw}" 2>&1 \
      || status=$?
  else
    info "run $((i + 1)) of ${#runs[@]}: vitest, ${#run_files[@]} file(s)"
    (cd "${REPO_ROOT}/${UI_DIR}" && ${limit[@]+"${limit[@]}"} npx vitest run --reporter=dot "${run_files[@]}") \
      >"${raw}" 2>&1 || status=$?
  fi
  redact <"${raw}" >"${log}"
  rm -f "${raw}"
  show_log "${log}" "${status}"
  if [[ "${status}" -eq 124 ]]; then
    err "run $((i + 1)) ran past ${RUN_SECONDS} s; it is not re-run as-is: narrow it to the files you changed; full output: ${log}"
    exit 1
  elif [[ "${status}" -ne 0 ]]; then
    err "run $((i + 1)) failed (exit ${status}); fix it before the next run; full output: ${log}"
    exit 1
  fi
  info "run $((i + 1)) passed; full output: ${log}"
done
ok "every related test passed (${#listed[@]} file(s), ${#runs[@]} run(s))"
