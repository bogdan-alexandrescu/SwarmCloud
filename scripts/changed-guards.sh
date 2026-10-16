#!/usr/bin/env bash
# changed-guards: run the repo-wide guard tests, and every unit test file that
# reads a path this checkout changes, in one quiet pass.
#
#   scripts/changed-guards.sh [--base <rev>] [--files <file>|-] [--list] [--shard <k>/<n>]
#
# WHY (#642). 25% of lane PRs (33 of 131, measured 2026-10-05) were red on
# their first CI run, and the reds were mostly repo-wide guard tests that a
# lane's narrowed `pytest tests/unit/<area>` never reaches: the docs guards
# (test_docs_describe_what_was_built, test_docs_spec_amendments), the UI route
# seam in test_runtimes_screen.py, test_specsign_covers, and the
# contract-parity script. Those tests read source and docs as TEXT, so the only
# reliable way to find the ones a change can break is to look for the changed
# path's name in the tests -- which is what this does. It is the one command a
# SwarmCloud agent runs before it finishes (CLAUDE.md, docs/ci.md). A laptop
# lane does not run it: CI is the gate there.
#
# What it selects, for a non-empty diff:
#   * the fixed guard set (GUARD_TESTS below), always -- about a minute;
#   * every changed tests/unit/**/test_*.py that still exists;
#   * every tests/unit/**/test_*.py whose text names a changed path, by its
#     repo-relative path or its basename. A basename in GENERIC_BASENAMES
#     (`__init__.py`, `package.json`, ...) selects by full path only: by name
#     alone it would select half the suite;
# then runs them in ONE `pytest -q -n auto -p no:warnings` call, followed by
# scripts/lib/check-contract-parity.sh. An empty diff runs nothing.
#
# `--shard k/n` runs the k-th of n parts of that selection, so a selection too
# heavy for one command's limit runs as n commands, each under it (#1334). The
# parts are cut the way CI cuts its unit shards (docs/ci.md): longest file
# first onto the lightest part, weighted by tests/unit/scripts/
# unit_test_durations.json, a file with no weight at the median. The cut is
# deterministic, so the n commands together run every selected file once; the
# contract-parity check runs in part 1 only.
#
# The diff is `git diff --name-only <base>...HEAD` plus uncommitted and
# untracked files, since the worker commits those too. <base> is --base, else
# SWARM_CLONE_BASE, else origin/main; an unresolvable base exits 2 rather than
# reading as an empty diff. `--files` reads the changed paths from a file (or
# stdin with `-`) instead of git, which is how its tests feed a fixture diff.
#
#   exit 0  nothing changed, or every selected test and the parity check passed
#           (with --list: the selection was printed, nothing ran)
#   exit 1  a selected test or the parity check failed, or ran out of time
#   exit 2  usage error, or the diff could not be read

set -euo pipefail

# common.sh exports the deployment's settings (PROJECT_ID, IMAGE_REPO,
# ENVIRONMENT, ...). A test that inherits them reads a configured deployment
# where CI has none, and fails here alone -- measured 2026-10-06: 20 worker and
# push-images tests red under this script, green in a plain pytest. So the
# tests run in the caller's environment: every name sourcing added is unset.
caller_env="$(compgen -e)"

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# The guards #642 measured lanes going red on. Each reads files across the
# repository, so no area-narrowed run reaches it.
GUARD_TESTS=(
  tests/unit/scripts/test_docs_describe_what_was_built.py
  tests/unit/scripts/test_docs_spec_amendments.py
  tests/unit/control_plane/test_runtimes_screen.py
  tests/unit/common/test_specsign_covers.py
)
PARITY_CHECK="scripts/lib/check-contract-parity.sh"

# Names shared by many files. Matched by full path only.
GENERIC_BASENAMES=" __init__.py conftest.py README.md main.py app.py config.py models.py utils.py index.ts index.tsx types.ts package.json pyproject.toml Makefile Dockerfile "

# The whole run stays under the 10 minutes a lane allows a command: the pytest
# pass gets 540 s and the parity check 60 s. NOT RAISED for a selection that
# needs longer (#1334: GS-HOOKS, 79 files, 2,603 tests, all passing in 595 s on
# 6 cores): past 600 s the agent's own command limit kills the run, with no
# `ran past` line and no faulthandler frames to name what held it. A heavier
# selection is split instead (`--shard`, SHARD_HINT_WEIGHT), and every part
# keeps this limit, so a hang still ends at 540 s.
PYTEST_SECONDS=540
PARITY_SECONDS=60
DURATIONS="tests/unit/scripts/unit_test_durations.json"
# The CI-seconds of selected files past which a run is told to split, and the
# weight of each part it is told to split into. CI's weights are a file's
# summed test seconds on a CI runner; a SwarmCloud container on 6 cores ran
# test_register_tenant_workspace.py (583 CI-seconds) in 428 s of wall clock
# (2026-10-16), about 0.73 s a CI-second. At that rate 540 CI-seconds is ~400 s
# here, the 540 s limit less a quarter for a busier container.
SHARD_HINT_WEIGHT=540

# NAMING THE SLOW TEST (box 102, #888). Lanes hit the 540 s cap with nothing
# saying which test held it. `--durations=10` lists the ten slowest on any run
# that ends by itself; it does NOT print when `timeout` kills pytest (measured
# 2026-10-09 under -n 2: SIGTERM leaves no report, SIGINT leaves an xdist
# teardown traceback). So pytest's faulthandler also dumps the stack of any
# test still running after FAULTHANDLER_SECONDS, which names its file, line and
# function, and an overrun prints those frames. 60 s because the whole fixed
# guard set runs in about 50 s: a single test past a minute is the suspect.
FAULTHANDLER_SECONDS=60

usage() {
  sed -n '4p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2
}

base="${SWARM_CLONE_BASE:-origin/main}"
files_from=""
list_only=0
shard=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      base="$2"; shift 2 ;;
    --files)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      files_from="$2"; shift 2 ;;
    --list) list_only=1; shift ;;
    --shard)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      shard="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown option: $1"; usage; exit 2 ;;
  esac
done

shard_k=0
shard_n=0
if [[ -n "${shard}" ]]; then
  if [[ ! "${shard}" =~ ^([1-9][0-9]*)/([1-9][0-9]*)$ ]] \
      || [[ "${BASH_REMATCH[1]}" -gt "${BASH_REMATCH[2]}" ]]; then
    err "--shard takes k/n with 1 <= k <= n, not ${shard}"
    exit 2
  fi
  shard_k="${BASH_REMATCH[1]}"
  shard_n="${BASH_REMATCH[2]}"
fi

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
  info "nothing changed in ${source_name}: no guard to run"
  exit 0
fi

# One fixed-string pattern per full path, and one per distinctive basename.
: >"${work}/patterns"
: >"${work}/own-tests"
while IFS= read -r path; do
  printf '%s\n' "${path}" >>"${work}/patterns"
  name="${path##*/}"
  case "${GENERIC_BASENAMES}" in
    *" ${name} "*) ;;
    *) printf '%s\n' "${name}" >>"${work}/patterns" ;;
  esac
  case "${path}" in
    tests/unit/*/test_*.py|tests/unit/test_*.py)
      if [[ -f "${REPO_ROOT}/${path}" ]]; then
        printf '%s\n' "${path}" >>"${work}/own-tests"
      fi ;;
  esac
done <"${work}/changed"

status=0
(cd "${REPO_ROOT}" && grep -rlF -f "${work}/patterns" --include='test_*.py' tests/unit) \
  >"${work}/readers" || status=$?
if [[ "${status}" -gt 1 ]]; then
  err "grep could not search tests/unit"
  exit 2
fi

: >"${work}/selected"
for guard in "${GUARD_TESTS[@]}"; do
  if [[ -f "${REPO_ROOT}/${guard}" ]]; then
    printf '%s\n' "${guard}" >>"${work}/selected"
  else
    warn "guard ${guard} is gone; update GUARD_TESTS in ${BASH_SOURCE[0]}"
  fi
done
sort -u "${work}/own-tests" "${work}/readers" | while IFS= read -r test_file; do
  grep -qxF -- "${test_file}" "${work}/selected" || printf '%s\n' "${test_file}" >>"${work}/selected"
done

# The selection's weight in CI-seconds, and with --shard the part to run.
weights="${REPO_ROOT}/${DURATIONS}"
[[ -r "${weights}" ]] || weights=/dev/null
require_cmd jq
weight="$(jq -R . "${work}/selected" | jq -rs --slurpfile d "${weights}" '
  ($d[0] // {}) as $w | ([$w[]] | sort) as $all
  | (if ($all | length) > 0 then $all[($all | length) / 2 | floor] else 1 end) as $median
  | map($w[.] // $median) | add // 0 | ceil')"
total_count="$(wc -l <"${work}/selected" | tr -d ' ')"
if [[ "${shard_n}" -gt 0 ]]; then
  jq -R . "${work}/selected" | jq -rs --slurpfile d "${weights}" --argjson k "${shard_k}" --argjson n "${shard_n}" '
    ($d[0] // {}) as $w | ([$w[]] | sort) as $all
    | (if ($all | length) > 0 then $all[($all | length) / 2 | floor] else 1 end) as $median
    | map({f: ., w: ($w[.] // $median)}) | sort_by(-.w, .f)
    | reduce .[] as $x ([range($n) | {t: 0, f: []}];
        (to_entries | min_by(.value.t) | .key) as $i | .[$i].t += $x.w | .[$i].f += [$x.f])
    | .[$k - 1].f | sort[]' >"${work}/part"
  mv "${work}/part" "${work}/selected"
fi

selected=()
while IFS= read -r test_file; do
  selected+=("${test_file}")
done <"${work}/selected"

# The parts a selection this heavy should run as (SHARD_HINT_WEIGHT).
parts=$(( (weight + SHARD_HINT_WEIGHT - 1) / SHARD_HINT_WEIGHT ))
[[ "${parts}" -ge 2 ]] || parts=2
split_hint="run it as ${parts} commands: scripts/changed-guards.sh --shard 1/${parts} ... --shard ${parts}/${parts}"

info "${changed_count} changed path(s) in ${source_name}; ${total_count} test file(s) read them or guard the repository:"
if [[ "${shard_n}" -gt 0 ]]; then
  info "part ${shard_k} of ${shard_n}: ${#selected[@]} of the ${total_count} file(s)"
elif [[ "${weight}" -gt "${SHARD_HINT_WEIGHT}" ]]; then
  warn "the selection weighs ${weight} CI-seconds (${DURATIONS}), more than one ${PYTEST_SECONDS} s run may hold; ${split_hint}"
fi
printf '%s\n' ${selected[@]+"${selected[@]}"}
if [[ "${shard_k}" -le 1 ]]; then
  printf 'bash %s\n' "${PARITY_CHECK}"
fi

if [[ "${list_only}" -eq 1 ]]; then
  exit 0
fi

caller_only=(env)
while IFS= read -r name; do
  case $'\n'"${caller_env}"$'\n' in
    *$'\n'"${name}"$'\n'*) ;;
    *) caller_only+=(-u "${name}") ;;
  esac
done < <(compgen -e)

# `swarm_python` prints either one path or `uv run --project <root> python`.
# The bare venv interpreter is run as an activated venv, the way `uv run`
# runs it: worker tests start the venv's own commands from PATH, and 9 of
# test_no_planted_restore.py fail without it (measured 2026-10-06).
read -r -a py <<<"$(swarm_python)"
if [[ "${py[0]}" == */bin/python ]]; then
  venv_bin="${py[0]%/python}"
  caller_only+=("PATH=${venv_bin}:${PATH}" "VIRTUAL_ENV=${venv_bin%/bin}")
fi
deadline=()
if command -v timeout >/dev/null 2>&1; then
  deadline=(timeout "${PYTEST_SECONDS}")
fi

log="$(mktemp "${TMPDIR:-/tmp}/changed-guards.XXXXXX")"
failed=0

pytest_status=0
if [[ ${#selected[@]} -eq 0 ]]; then
  # More parts than files: pytest given no file would run all of tests/unit.
  info "part ${shard_k} of ${shard_n} holds no test file; pytest has nothing to run"
else
  step "pytest: ${#selected[@]} file(s), -n auto"
  (cd "${REPO_ROOT}" && "${caller_only[@]}" ${deadline[@]+"${deadline[@]}"} "${py[@]}" -m pytest -q -n auto -p no:warnings \
    --durations=10 -o "faulthandler_timeout=${FAULTHANDLER_SECONDS}" \
    ${selected[@]+"${selected[@]}"}) >"${log}" 2>&1 || pytest_status=$?
  tail -n 25 "${log}" | redact
  if [[ "${pytest_status}" -eq 124 ]]; then
    err "pytest ran past ${PYTEST_SECONDS} s"
    # The dumps sit far above the tail; print only their frames in a test file.
    if grep -E '^ *File ".*/test_[^/"]*\.py", line [0-9]+ in ' "${log}" | sort -u >"${work}/stuck" \
        && [[ -s "${work}/stuck" ]]; then
      err "still running after ${FAULTHANDLER_SECONDS} s (faulthandler):"
      redact <"${work}/stuck" >&2
    else
      err "no test ran past ${FAULTHANDLER_SECONDS} s: many moderate tests filled the cap; read the log's durations"
    fi
    if [[ "${shard_n}" -eq 0 ]]; then
      err "the selection weighs ${weight} CI-seconds; ${split_hint}"
    fi
  fi
  if [[ "${pytest_status}" -ne 0 ]]; then
    err "pytest failed (exit ${pytest_status}); full output: ${log}"
    failed=1
  fi
fi

if [[ ${#deadline[@]} -gt 0 ]]; then
  deadline=(timeout "${PARITY_SECONDS}")
fi
parity_status=0
if [[ "${shard_k}" -le 1 ]]; then
  (cd "${REPO_ROOT}" && "${caller_only[@]}" ${deadline[@]+"${deadline[@]}"} bash "${PARITY_CHECK}") >"${log}.parity" 2>&1 \
    || parity_status=$?
fi
if [[ "${parity_status}" -ne 0 ]]; then
  tail -n 25 "${log}.parity" | redact
  err "contract parity failed (exit ${parity_status}); full output: ${log}.parity"
  failed=1
fi

if [[ "${failed}" -ne 0 ]]; then
  exit 1
fi
rm -f "${log}" "${log}.parity"
if [[ "${shard_n}" -eq 0 ]]; then
  ok "${#selected[@]} guard and source-reading test file(s) and contract parity pass"
elif [[ "${shard_k}" -eq 1 ]]; then
  ok "part 1 of ${shard_n}: ${#selected[@]} of ${total_count} test file(s) and contract parity pass"
else
  ok "part ${shard_k} of ${shard_n}: ${#selected[@]} of ${total_count} test file(s) pass"
fi
