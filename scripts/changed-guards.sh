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
#   * the fixed guard set (GUARD_TESTS below), always -- about a minute (a
#     docs-only diff: DOC_GUARD_TESTS, see below). Pull-request CI reads the
#     same array and runs it on every pull request;
#   * every changed tests/unit/**/test_*.py that still exists;
#   * every tests/unit/**/test_*.py whose text names a changed path, by its
#     repo-relative path or its basename. A basename in GENERIC_BASENAMES
#     (`__init__.py`, `package.json`, ...) selects by full path only: by name
#     alone it would select half the suite;
#   * every tests/unit/**/test_*.py whose text names a docs/**/*.md that cites
#     a changed non-doc path by its FULL path, one level deep (#2299): a test
#     that resolves a doc's citation names only the doc;
# then runs them in ONE `pytest -q -n auto -p no:warnings` call, followed by
# scripts/lib/check-contract-parity.sh. An empty diff runs nothing.
#
# A change to a swarm-ui stylesheet (apps/swarm-ui/**.css) also runs the UI's
# stylesheet guards in one vitest call (RELATED-CSS-GUARDS, part of #1914).
# They read every sheet from disk, so neither a test's text nor an import
# names the changed sheet; measured 2026-10-22 on PR 1905, whose duplicate
# selector in styles/intake.css CI refused after every agent of the lane had
# passed this script. The list is scripts/lib/related_tests.py's
# STYLESHEET_GUARDS (`--stylesheet-guards`), the one related-tests.sh selects
# from; they run in part 1 only, like the parity check, from the package's
# own node_modules/.bin/vitest. Not installed is a failure, not a pass.
#
# A DOCS-ONLY diff -- every changed path under docs/ or ending in .md --
# selects only the doc guards (DOC_GUARD_TESTS) and the tests whose text
# names a changed doc, and runs no parity check (GUARDS-DEDUP, part of #1694:
# docs-only lanes spent 44-50 min in guards on 2026-10-19/20). The UI route
# seam and test_specsign_covers read source, not docs, and the parity check
# skips `.md` files outright, so none of them can turn red on a doc. CI's
# docs-only pick still runs every test that builds a path through docs/
# (docs/ci.md), and main's push runs everything.
#
# THE RECORD (GUARDS-DEDUP). A run that reads its diff from git (not --files,
# not --list) writes how it ended -- passed, failed or timed_out -- with the
# tree it ran on and its base commit, to <git dir>/swarm-changed-guards/
# all.json (shard-<k>-of-<n>.json with --shard). The tree is the one the
# worker would commit: HEAD plus every uncommitted and untracked file, as
# `git add -A` into a scratch index writes it. The worker's pre-publish gate
# computes that tree itself and skips its own run only when a record says
# passed on the same tree from the same base (agent_worker.prepublish): in 73
# of 74 lanes it re-ran this script on code the agent had just passed. The
# record sits in the git directory, so the publish never commits it.
#
# `--shard k/n` runs the k-th of n parts of that selection, so a selection too
# heavy for one command's limit runs as n commands, each under it (#1334). The
# parts are cut the way CI cuts its unit shards (docs/ci.md): longest file
# first onto the lightest part, weighted by tests/unit/scripts/
# unit_test_durations.json, a file with no weight at the median. The cut is
# deterministic, so the n commands together run every selected file once; the
# contract-parity check runs in part 1 only. Without --shard, a selection that
# heavy runs its parts in turn (see SHARD_HINT_WEIGHT).
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

# This script touches no project: it reads the tree. It says so, so
# common.sh (no default project, DAYONE-DESAGA-2) does not stop on an unset
# PROJECT_ID. Not exported: the tests still run in the caller's environment.
SWARM_NO_PROJECT=1

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# The guards #642 measured lanes going red on, and the whole-tree tests (they
# walk apps/, tests/ or docs/ and assert over every file, so no changed path
# names them) that turned main red because no pull request ran them:
# test_collection_stays_cheap for 19 commits on 2026-10-20, test_awaiting_
# input_readers for 6 on 2026-10-19. Each reads files across the repository,
# so no area-narrowed run reaches it. THE ONE LIST: pull-request CI's
# selection (scripts/lib/select_unit_tests.py guard_tests) reads this array
# from this file and always runs it, so keep it one path a line, no quoting.
# Only whole-tree tests of at most 10 CI-seconds a file are here: every pull
# request and every lane pays for this set (CI-GUARDS-ALWAYS, part of #1822).
# After test_docs_agent_vocabulary: the walkers of scripts/, terraform/ and
# kubernetes/, the same defect class one directory over (a new .tf, .yaml or
# .sh breaks them and names none), each under 5 CI-seconds, about 11 s
# together (CI-GUARDS-MORE, part of #1822). After test_swarm_verify_image_
# helpers: the walkers of .github/ and images/ -- every workflow, every
# Dockerfile, or every file of one image directory -- each under 5
# CI-seconds, about 12 s together (CI-GUARDS-DOCS, part of #1822). After
# test_repo_index_extract: three more of those walkers, test_auto_merge_base_
# merge (2.6 s), test_issue_forms (0.6 s) and test_verify_deployer_trust
# (4.0 s), +7.2 CI-seconds a pull request; they also read CLAUDE.md or
# docs/runbooks/merge-app.md by name, so a docs-only diff of those selects
# them as readers (CI-GUARDS-THREE, part of #1822).
GUARD_TESTS=(
  tests/unit/scripts/test_docs_describe_what_was_built.py
  tests/unit/scripts/test_docs_spec_amendments.py
  tests/unit/control_plane/test_runtimes_screen.py
  tests/unit/common/test_specsign_covers.py
  tests/unit/scripts/test_collection_stays_cheap.py
  tests/unit/common/test_awaiting_input_readers.py
  tests/unit/control_plane/test_artifacts_read_path.py
  tests/unit/control_plane/test_declared_dependencies.py
  tests/unit/control_plane/test_spec_signing_every_submitter.py
  tests/unit/scheduler/test_mcp_admission.py
  tests/unit/worker/test_helpers_import_by_unique_name.py
  tests/unit/scripts/test_docs_agent_vocabulary.py
  tests/unit/common/test_startup_progress_contract.py
  tests/unit/control_plane/test_reconciler_cloud_run_stall_guard.py
  tests/unit/scripts/test_cloudbuild_connection_retired.py
  tests/unit/worker/test_spec_verify_keys_configmap.py
  tests/unit/scripts/test_workspace_apply_approval.py
  tests/unit/worker/test_kubernetes_manifests.py
  tests/unit/control_plane/test_dispatch_judgment.py
  tests/unit/control_plane/test_gke_cache_volume.py
  tests/unit/control_plane/test_gke_worker_metadata_env.py
  tests/unit/control_plane/test_schedules_migration_seed.py
  tests/unit/control_plane/test_tfvars_refusal_switches.py
  tests/unit/control_plane/test_workspace_gate_dev.py
  tests/unit/scripts/test_create_secrets_workspace_grant_key.py
  tests/unit/scripts/test_deployer_role_count_prose.py
  tests/unit/scripts/test_github_app_runbook.py
  tests/unit/scripts/test_owner_pinned_jobs.py
  tests/unit/scripts/test_tenancy_retired_295_identities.py
  tests/unit/worker/test_checkpoint_retention.py
  tests/unit/worker/test_workspace_provisioner_scope.py
  tests/unit/scripts/test_swarm_verify_image_helpers.py
  tests/unit/scripts/test_fork_prs_off_self_hosted.py
  tests/unit/scripts/test_workflow_step_reachability.py
  tests/unit/scripts/test_image_streaming.py
  tests/unit/scripts/test_pr_image_scan.py
  tests/unit/scripts/test_ui_node_line.py
  tests/unit/worker/test_jev_browser_image.py
  tests/unit/worker/test_repo_index_extract.py
  tests/unit/scripts/test_auto_merge_base_merge.py
  tests/unit/scripts/test_issue_forms.py
  tests/unit/scripts/test_verify_deployer_trust.py
)
# The guards that read docs: all a docs-only diff selects of GUARD_TESTS.
# Pull-request CI runs the whole GUARD_TESTS even for a docs-only change.
DOC_GUARD_TESTS=(
  tests/unit/scripts/test_docs_describe_what_was_built.py
  tests/unit/scripts/test_docs_spec_amendments.py
  tests/unit/scripts/test_docs_agent_vocabulary.py
)
PARITY_CHECK="scripts/lib/check-contract-parity.sh"
# The stylesheet guards' list and the package they run in (see above).
STYLESHEET_SELECTOR="scripts/lib/related_tests.py"
UI_DIR="apps/swarm-ui"
# Where a run's result is recorded, under the git directory (THE RECORD
# above). agent_worker.prepublish.GUARDS_RECORD_DIR restates the name.
RECORD_DIR_NAME="swarm-changed-guards"

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

# Docs-only: every changed path is under docs/ or a .md file.
docs_only=1
while IFS= read -r path; do
  case "${path}" in
    docs/*|*.md) ;;
    *) docs_only=0; break ;;
  esac
done <"${work}/changed"
guards=("${GUARD_TESTS[@]}")
run_parity=1
if [[ "${docs_only}" -eq 1 ]]; then
  guards=("${DOC_GUARD_TESTS[@]}")
  run_parity=0
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

# A doc that cites a changed non-doc path by its full path counts as changed,
# one level deep (#2299; select_unit_tests.py's doc_closure says why). A
# changed doc cites nothing in, so a docs-only diff selects what it did.
grep -vE '^docs/|\.md$' "${work}/changed" >"${work}/cited" || true
: >"${work}/citing-docs"
if [[ -s "${work}/cited" && -d "${REPO_ROOT}/docs" ]]; then
  status=0
  (cd "${REPO_ROOT}" && grep -rlF -f "${work}/cited" --include='*.md' \
    --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=__pycache__ docs) \
    >"${work}/citing-docs" || status=$?
  if [[ "${status}" -gt 1 ]]; then
    err "grep could not search docs/"
    exit 2
  fi
fi
while IFS= read -r doc; do
  printf '%s\n' "${doc}" >>"${work}/patterns"
  name="${doc##*/}"
  case "${GENERIC_BASENAMES}" in
    *" ${name} "*) ;;
    *) printf '%s\n' "${name}" >>"${work}/patterns" ;;
  esac
done <"${work}/citing-docs"

status=0
(cd "${REPO_ROOT}" && grep -rlF -f "${work}/patterns" --include='test_*.py' tests/unit) \
  >"${work}/readers" || status=$?
if [[ "${status}" -gt 1 ]]; then
  err "grep could not search tests/unit"
  exit 2
fi

: >"${work}/selected"
for guard in "${guards[@]}"; do
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

# The stylesheet guards, for a swarm-ui stylesheet change; part 1 runs them.
style_guards=()
if [[ "${shard_k}" -le 1 ]] && grep -qE "^${UI_DIR}/.*\.css\$" "${work}/changed"; then
  require_cmd python3
  python3 "${REPO_ROOT}/${STYLESHEET_SELECTOR}" --root "${REPO_ROOT}" --changed "${work}/changed" \
    --stylesheet-guards >"${work}/style-guards" \
    || { err "${STYLESHEET_SELECTOR} could not name the stylesheet guards"; exit 2; }
  while IFS= read -r guard; do
    if [[ -n "${guard}" ]]; then
      style_guards+=("${guard}")
    fi
  done <"${work}/style-guards"
fi

# The parts a selection this heavy should run as (SHARD_HINT_WEIGHT).
parts=$(( (weight + SHARD_HINT_WEIGHT - 1) / SHARD_HINT_WEIGHT ))
[[ "${parts}" -ge 2 ]] || parts=2
split_hint="run it as ${parts} commands: scripts/changed-guards.sh --shard 1/${parts} ... --shard ${parts}/${parts}"

info "${changed_count} changed path(s) in ${source_name}; ${total_count} test file(s) read them or guard the repository:"
if [[ "${docs_only}" -eq 1 ]]; then
  info "docs-only change: the doc guards and the tests that read the changed docs; no contract-parity check"
fi
if [[ "${shard_n}" -gt 0 ]]; then
  info "part ${shard_k} of ${shard_n}: ${#selected[@]} of the ${total_count} file(s)"
elif [[ "${weight}" -gt "${SHARD_HINT_WEIGHT}" ]]; then
  warn "the selection weighs ${weight} CI-seconds (${DURATIONS}), more than one ${PYTEST_SECONDS} s run may hold: it runs as ${parts} parts in turn; under a 10-minute command limit, ${split_hint}"
fi
printf '%s\n' ${selected[@]+"${selected[@]}"}
if [[ ${#style_guards[@]} -gt 0 ]]; then
  info "a swarm-ui stylesheet changed: ${#style_guards[@]} stylesheet guard(s) run in one vitest call"
  for guard in "${style_guards[@]}"; do
    printf '%s/%s\n' "${UI_DIR}" "${guard}"
  done
fi
if [[ "${shard_k}" -le 1 && "${run_parity}" -eq 1 ]]; then
  printf 'bash %s\n' "${PARITY_CHECK}"
fi

if [[ "${list_only}" -eq 1 ]]; then
  exit 0
fi

# THE RECORD: the tree this run checks, taken before any test runs. Only a
# run that read its diff from git has a tree to name.
record_file=""
record_tree=""
record_base=""
if [[ -z "${files_from}" ]]; then
  git_dir="$(git -C "${REPO_ROOT}" rev-parse --absolute-git-dir)"
  scratch_index="${work}/index"
  if [[ -f "${git_dir}/index" ]]; then
    cp "${git_dir}/index" "${scratch_index}"
  else
    GIT_INDEX_FILE="${scratch_index}" git -C "${REPO_ROOT}" read-tree HEAD 2>/dev/null \
      || GIT_INDEX_FILE="${scratch_index}" git -C "${REPO_ROOT}" read-tree --empty
  fi
  if GIT_INDEX_FILE="${scratch_index}" git -C "${REPO_ROOT}" add -A >/dev/null 2>&1 \
      && record_tree="$(GIT_INDEX_FILE="${scratch_index}" git -C "${REPO_ROOT}" write-tree)"; then
    record_base="$(git -C "${REPO_ROOT}" rev-parse --verify -q "${base}^{commit}")"
    mkdir -p "${git_dir}/${RECORD_DIR_NAME}"
    if [[ "${shard_n}" -gt 0 ]]; then
      record_file="${git_dir}/${RECORD_DIR_NAME}/shard-${shard_k}-of-${shard_n}.json"
    else
      record_file="${git_dir}/${RECORD_DIR_NAME}/all.json"
    fi
    # A stale record of this part never outlives a run that did not finish.
    rm -f "${record_file}"
  else
    warn "git could not write this checkout's tree; the run is not recorded"
  fi
fi

# How the run ended -- passed, failed or timed_out -- in the record, and to
# the parent run's status file when this is one of its parts.
finish() {
  local result="$1"
  if [[ -n "${_CHANGED_GUARDS_RESULT:-}" ]]; then
    printf '%s\n' "${result}" >"${_CHANGED_GUARDS_RESULT}"
  fi
  if [[ -n "${record_file}" ]]; then
    local part="all"
    [[ "${shard_n}" -eq 0 ]] || part="${shard_k}/${shard_n}"
    jq -n --arg tree "${record_tree}" --arg base "${record_base}" --arg part "${part}" \
      --arg status "${result}" --argjson selected "${#selected[@]}" \
      --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{version: 1, tree: $tree, base: $base, part: $part, status: $status,
        selected: $selected, finished_at: $at}' >"${record_file}.tmp" \
      && mv "${record_file}.tmp" "${record_file}"
  fi
}

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
# A selection over SHARD_HINT_WEIGHT, run without --shard, runs as its parts,
# one after another: each its own pytest call under the PYTEST_SECONDS limit,
# so a hang still ends, and a passing selection completes however many files
# it names (#1334: this lane's own 21 files ran past 540 s as one call). The
# whole run is then up to parts x 540 s; an agent whose command limit is 10
# minutes runs the parts as separate commands instead (`--shard`).
if [[ "${shard_n}" -eq 0 && "${weight}" -gt "${SHARD_HINT_WEIGHT}" ]]; then
  info "running the selection as ${parts} parts, each under ${PYTEST_SECONDS} s"
  part_failed=0
  part_timed_out=0
  for ((k = 1; k <= parts; k++)); do
    "${caller_only[@]}" "_CHANGED_GUARDS_RESULT=${work}/part-${k}" \
      bash "${BASH_SOURCE[0]}" --files "${work}/changed" --shard "${k}/${parts}" || part_failed=1
    if [[ -r "${work}/part-${k}" ]] && grep -qx timed_out "${work}/part-${k}"; then
      part_timed_out=1
    fi
  done
  if [[ "${part_failed}" -ne 0 ]]; then
    if [[ "${part_timed_out}" -ne 0 ]]; then
      finish timed_out
    else
      finish failed
    fi
    err "a part failed; its output is above"
    exit 1
  fi
  finish passed
  ok "all ${parts} parts: ${total_count} guard and source-reading test file(s) and contract parity pass"
  exit 0
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

vitest_status=0
if [[ ${#style_guards[@]} -gt 0 ]]; then
  vitest_bin="${REPO_ROOT}/${UI_DIR}/node_modules/.bin/vitest"
  if [[ ! -x "${vitest_bin}" ]]; then
    err "the stylesheet guards did not run: ${UI_DIR}/node_modules/.bin/vitest is not installed; install it with (cd ${UI_DIR} && npm ci) and run this again"
    failed=1
  else
    step "vitest: ${#style_guards[@]} stylesheet guard(s)"
    (cd "${REPO_ROOT}/${UI_DIR}" && "${caller_only[@]}" ${deadline[@]+"${deadline[@]}"} "${vitest_bin}" run \
      "${style_guards[@]}") >"${log}.vitest" 2>&1 || vitest_status=$?
    tail -n 25 "${log}.vitest" | redact
    if [[ "${vitest_status}" -eq 124 ]]; then
      err "the stylesheet guards ran past ${PYTEST_SECONDS} s"
    fi
    if [[ "${vitest_status}" -ne 0 ]]; then
      err "a stylesheet guard failed (exit ${vitest_status}); full output: ${log}.vitest"
      failed=1
    fi
  fi
fi

if [[ ${#deadline[@]} -gt 0 ]]; then
  deadline=(timeout "${PARITY_SECONDS}")
fi
parity_status=0
if [[ "${shard_k}" -le 1 && "${run_parity}" -eq 1 ]]; then
  (cd "${REPO_ROOT}" && "${caller_only[@]}" ${deadline[@]+"${deadline[@]}"} bash "${PARITY_CHECK}") >"${log}.parity" 2>&1 \
    || parity_status=$?
fi
if [[ "${parity_status}" -ne 0 ]]; then
  tail -n 25 "${log}.parity" | redact
  err "contract parity failed (exit ${parity_status}); full output: ${log}.parity"
  failed=1
fi

if [[ "${failed}" -ne 0 ]]; then
  if [[ "${pytest_status}" -eq 124 || "${vitest_status}" -eq 124 || "${parity_status}" -eq 124 ]]; then
    finish timed_out
  else
    finish failed
  fi
  exit 1
fi
finish passed
rm -f "${log}" "${log}.vitest" "${log}.parity"
if [[ ${#style_guards[@]} -gt 0 ]]; then
  ok "${#style_guards[@]} stylesheet guard(s) pass"
fi
if [[ "${run_parity}" -eq 0 ]]; then
  ok "${#selected[@]} doc guard and doc-reading test file(s) pass"
elif [[ "${shard_n}" -eq 0 ]]; then
  ok "${#selected[@]} guard and source-reading test file(s) and contract parity pass"
elif [[ "${shard_k}" -eq 1 ]]; then
  ok "part 1 of ${shard_n}: ${#selected[@]} of ${total_count} test file(s) and contract parity pass"
else
  ok "part ${shard_k} of ${shard_n}: ${#selected[@]} of ${total_count} test file(s) pass"
fi
