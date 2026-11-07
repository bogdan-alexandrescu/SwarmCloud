#!/usr/bin/env bash
# ci-select: what application.yml's run of a change has to test.
#
#   scripts/ci-select.sh lane --changed <file> [--github-output <file>] [--summary <file>]
#   scripts/ci-select.sh ui-pick   (SHARD, SHARDS, UI_TESTS, DURATIONS, SHARD_FILES, GITHUB_OUTPUT)
#   scripts/ci-select.sh train-match --repo <owner/name> --sha <sha> [--github-output <file>] [--summary <file>]
#
# WHY (owner request, BGD 2026-11-06: "we really need to fix ci merges and
# streamline the ci part, its the biggest bottleneck we have right now"; part
# of #2688). One application.yml run was 27 jobs and 76 runner-minutes on a
# pull request, 34 jobs on a push to main, about 200 runs a day, and
# GitHub-hosted jobs waited up to 51 minutes. The owner picked two changes,
# so the full suite runs once per change, in the merge train (docs/ci.md
# "The full suite runs once per change, in the merge train"):
#
# `lane` -- A LANE'S PULL REQUEST (a `swarm/**` head) TESTS WHAT IT REACHES.
#   Lint, format, typecheck and the contract and lock checks still run whole;
#   the unit shards are narrowed by select_unit_tests.py as before, and this
#   names the swarm-ui component tests (related_tests.py's import graph plus
#   the stylesheet guards on a .css change) and the integration tests (a
#   changed one, one importing a changed module through any chain, or one
#   naming a changed path) the change reaches. It writes `lane=true` and the
#   two lists, or `lane=false` and why when the change touches shared ground
#   -- what changes how every test runs (select_unit_tests.py's fallback, the
#   swarm-ui runner's own files, tests/integration's shared helpers) -- or the
#   selection cannot be computed: then the lane runs the full suite.
#
# `train-match` -- MAIN DOES NOT RE-TEST WHAT ITS MERGE TRAIN TESTED. The
#   train (scripts/merge-train.sh) lands each pull request as its own squash,
#   so main's commit after the last one has the train's tested tree, but
#   names the last pull request, not the train: ci_reuse.py's lookup never
#   matched it, and main ran the whole suite again. This looks for a train
#   pull request (title `Merge train <id>: ...`, head `train/<id>` in this
#   repository) whose head has THIS commit's tree, whose newest `ci-gate` at
#   that head concluded success, and which the train closed with its
#   "Green, and landed" comment. Found, it writes `reused` (every job
#   ci_reuse.py may skip, read from REUSABLE there, never a copy) and
#   `reused_from` (the train's URL), which ci-gate's reuse check then cites.
#   The squash is pushed a few seconds before the train closes, so an open
#   train with this tree is waited for, at most TRAIN_WAIT_SECONDS. Any doubt
#   -- no train, an API error, a red or missing ci-gate, no landed comment --
#   writes `reused=[]`, and every job runs.
#
# tests/unit/scripts/test_ci_select.py runs both against fixtures.
set -euo pipefail

# This script touches no project: it reads the tree and GitHub only. It says
# so, so common.sh (no default project, DAYONE-DESAGA-2) does not stop on an
# unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# How long main's run waits for a train with its tree to close. The train
# closes seconds after its last squash (finish_train), so five minutes is a
# margin, not a wait; a train that never closes runs the full suite.
TRAIN_WAIT_SECONDS="${TRAIN_WAIT_SECONDS:-300}"
TRAIN_POLL_SECONDS="${TRAIN_POLL_SECONDS:-15}"
# The newest pull requests looked at: a train is closed seconds before main's
# run starts, so it is among the most recently updated.
TRAIN_LOOKBACK=50
# finish_train's sentence for a train that landed (scripts/merge-train.sh).
LANDED_PREFIX="Green, and landed"

usage() {
  sed -n '4,6p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2
}

github_output=""
summary_file=""

emit() {
  # key=value to stdout, and to the step's output file when given.
  printf '%s=%s\n' "$1" "$2"
  if [[ -n "${github_output}" ]]; then
    printf '%s=%s\n' "$1" "$2" >>"${github_output}"
  fi
}

say() {
  info "$*"
  if [[ -n "${summary_file}" ]]; then
    printf '%s\n\n' "$*" >>"${summary_file}"
  fi
}

# --------------------------------------------------------------------------
# lane
# --------------------------------------------------------------------------
lane() {
  local changed="" out
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --changed) [[ $# -ge 2 ]] || { usage; exit 2; }; changed="$2"; shift 2 ;;
      --github-output) [[ $# -ge 2 ]] || { usage; exit 2; }; github_output="$2"; shift 2 ;;
      --summary) [[ $# -ge 2 ]] || { usage; exit 2; }; summary_file="$2"; shift 2 ;;
      *) err "unknown option: $1"; usage; exit 2 ;;
    esac
  done
  [[ -n "${changed}" && -r "${changed}" ]] || { err "lane needs --changed <readable file>"; exit 2; }
  require_cmd python3
  out="${WORK}/lane"
  # A selector that fails is a selection that cannot be computed: the full
  # suite, never an empty one.
  if ! python3 - "${REPO_ROOT}" "${changed}" >"${out}" 2>"${out}.err" <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
os.environ.setdefault("DURATIONS", str(root / "tests/unit/scripts/ui_test_durations.json"))
sys.path.insert(0, str(root / "scripts" / "lib"))
import related_tests as rt  # noqa: E402
import select_unit_tests as sut  # noqa: E402

changed = sorted({c.strip() for c in Path(sys.argv[2]).read_text().splitlines() if c.strip()})
UI = rt.UI
INTEGRATION = "tests/integration/"
# Shared ground beyond select_unit_tests.py's fallback (workflows, actions,
# the frozen contract, conftest.py, pyproject.toml, uv.lock, the quarantine,
# more than MAX_CHANGED paths): every lockfile, the swarm-ui runner's own files
# (related_tests.py RUNNER_WIDE) and tests/integration's shared helpers.
LOCKFILES = {"uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock"}
RUNNER_CONFIG = {"pytest.ini", "setup.cfg", "tox.ini"}


def shared(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name in LOCKFILES or name == "package.json" or name in RUNNER_CONFIG:
        return f"{path} is a package manifest, lockfile or test runner config"
    if path.startswith(f"{UI}/") and path[len(UI) + 1:] in rt.RUNNER_WIDE:
        return f"{path} changes how every component test runs"
    if path.startswith(INTEGRATION) and not (name.startswith("test_") and name.endswith(".py")):
        return f"{path} is shared by the integration tests"
    return None


def full(why: str) -> None:
    print(f"lane=false\nui_tests=\nui_shards=\nintegration_tests=\nwhy={why}")


if not changed:
    full("no changed path could be read")
    sys.exit(0)
why = sut.fallback_reason(changed) or next((r for r in map(shared, changed) if r), None)
if why:
    full(why)
    sys.exit(0)

# swarm-ui component tests, relative to apps/swarm-ui as vitest reads them.
changed_ui = [c[len(UI) + 1:] for c in changed if c.startswith(f"{UI}/")]
ui: set[str] = set()
if changed_ui:
    ui.update(rt.ui_related(root / UI, changed_ui, None))
    if any(c.endswith(".css") for c in changed_ui):
        ui.update(rt.stylesheet_guards(set(rt._ui_files(root / UI))))

# Integration tests: changed, importing a changed module through any chain,
# or naming a changed path.
tests = sorted(str(p.relative_to(root)) for p in sut._python_files(root, "tests/integration")
               if p.name.startswith("test_"))
changed_py = [c for c in changed if c.endswith(".py") and c.startswith(("apps/", "scripts/", "tests/unit/"))]
reached = set(changed_py) | sut.import_closure(root, changed_py)
names: set[str] = set()
for rel in reached:
    names.update(sut._suffixes(root, rel))
integration = []
for rel in tests:
    text = (root / rel).read_text(errors="replace")
    if (rel in changed or names.intersection(sut.import_edges(rel, text))
            or any(sut.names_path(text, p) for p in changed)):
        integration.append(rel)

# The component-test shards, sized by ui_shards.py's own weights and rule
# over the selected files alone (`ui-pick` splits them the same way); one
# shard, which runs nothing, when none is selected.
import ui_shards  # noqa: E402

groups = ui_shards.units(sorted(ui))
total = sum(ui_shards.weight(u, ui_shards.weights()) for u in groups) if groups else 0
shards = ui_shards.shard_count(total, len(groups)) if groups else 1
print("lane=true")
print("ui_tests=" + " ".join(sorted(ui)))
print("ui_shards=" + json.dumps(list(range(1, shards + 1))))
print("integration_tests=" + " ".join(integration))
print(f"why={len(changed)} changed path(s) reach {len(ui)} component test file(s) and "
      f"{len(integration)} of {len(tests)} integration test file(s)")
PY
  then
    warn "the lane selection could not be computed; running the full suite"
    sed 's/^/  /' "${out}.err" >&2 || true
    printf 'lane=false\nui_tests=\nui_shards=\nintegration_tests=\nwhy=the lane selection could not be computed\n' >"${out}"
  fi
  local key value why="" narrowed=false
  while IFS='=' read -r key value; do
    emit "${key}" "${value}"
    case "${key}" in
      why) why="${value}" ;;
      lane) narrowed="${value}" ;;
    esac
  done <"${out}"
  if [[ "${narrowed}" == true ]]; then
    say "**A lane's pull request: related tests only.** ${why}. The merge train runs the full suite."
  else
    say "**The full suite:** ${why}."
  fi
}

# --------------------------------------------------------------------------
# ui-pick
# --------------------------------------------------------------------------
ui_pick() {
  # One `swarm-ui component tests (k/N)` shard of a lane's selection: the
  # environment ui_shards.py pick reads (SHARD, SHARDS, DURATIONS,
  # SHARD_FILES, GITHUB_OUTPUT) plus UI_TESTS, the lane's files. Writes the
  # shard's files to SHARD_FILES and `count`; an empty selection writes
  # count=0 and no file, and the job's later steps are skipped on it --
  # never an empty `vitest run`, which runs the whole suite.
  require_cmd python3
  python3 - "${REPO_ROOT}" <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1] + "/scripts/lib")
import ui_shards  # noqa: E402

files = sorted(set(os.environ.get("UI_TESTS", "").split()))
shard, shards = int(os.environ["SHARD"]), int(os.environ["SHARDS"])
out = Path(os.environ["SHARD_FILES"])
with open(os.environ["GITHUB_OUTPUT"], "a") as gh:
    if not files:
        out.write_text("")
        gh.write("count=0\n")
        print("the change reaches no component test: this shard runs nothing")
        sys.exit(0)
    groups = ui_shards.units(files)
    if not 1 <= shard <= shards or shards > len(groups):
        sys.exit(f"::error::shard {shard} of {shards} for {len(groups)} unit(s) of selected vitest files")
    parts, load = ui_shards.split(groups, shards, ui_shards.weights())
    mine = parts[shard - 1]
    out.write_text("".join(f"{f}\n" for f in mine))
    gh.write(f"count={len(mine)}\n")
print(f"{len(files)} selected vitest file(s) in {shards} shard(s); shard {shard} runs {len(mine)}, "
      f"~{load[shard - 1]:.0f} s of measured test time")
for f in mine:
    print(f"  {f}")
PY
}

# --------------------------------------------------------------------------
# train-match
# --------------------------------------------------------------------------
reusable_jobs() {
  # REUSABLE's keys in scripts/lib/ci_reuse.py, as a JSON list: the one list.
  python3 - "${REPO_ROOT}" <<'PY'
import json
import sys

sys.path.insert(0, sys.argv[1] + "/scripts/lib")
import ci_reuse  # noqa: E402

print(json.dumps(list(ci_reuse.REUSABLE), separators=(",", ":")))
PY
}

train_landed() {
  # $1 repo, $2 number. 0 when the train is closed and the train's own
  # "Green, and landed" comment (a bot's) is on it; waits while it is open.
  local repo="$1" number="$2" waited=0 state
  while :; do
    state="$(gh api "repos/${repo}/pulls/${number}" --jq '.state')" || return 1
    [[ "${state}" == open ]] || break
    if [[ "${waited}" -ge "${TRAIN_WAIT_SECONDS}" ]]; then
      warn "train #${number} has this tree but is still open after ${waited} s"
      return 1
    fi
    info "train #${number} has this tree and is still open; looking again in ${TRAIN_POLL_SECONDS} s"
    sleep "${TRAIN_POLL_SECONDS}"
    waited=$((waited + TRAIN_POLL_SECONDS))
  done
  [[ "${state}" == closed ]] || return 1
  # To a file, then grep: `gh ... | grep -q` under pipefail fails when grep
  # stops reading early.
  gh api "repos/${repo}/issues/${number}/comments?per_page=100" --paginate \
      --jq ".[] | select(.user.type == \"Bot\") | .body" >"${WORK}/comments" || return 1
  grep -q "^${LANDED_PREFIX}" "${WORK}/comments"
}

gate_green() {
  # $1 repo, $2 sha. 0 when the newest `ci-gate` check run at sha succeeded.
  local conclusion
  conclusion="$(gh api "repos/${1}/commits/${2}/check-runs?check_name=ci-gate&filter=latest" \
    --jq '[.check_runs[] | {c: .conclusion, at: (.completed_at // "")}] | sort_by(.at) | last | .c // ""')" \
    || return 1
  [[ "${conclusion}" == success ]]
}

train_match() {
  local repo="" sha="" tree list number head_sha head_tree url jobs
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) [[ $# -ge 2 ]] || { usage; exit 2; }; repo="$2"; shift 2 ;;
      --sha) [[ $# -ge 2 ]] || { usage; exit 2; }; sha="$2"; shift 2 ;;
      --github-output) [[ $# -ge 2 ]] || { usage; exit 2; }; github_output="$2"; shift 2 ;;
      --summary) [[ $# -ge 2 ]] || { usage; exit 2; }; summary_file="$2"; shift 2 ;;
      *) err "unknown option: $1"; usage; exit 2 ;;
    esac
  done
  [[ -n "${repo}" && -n "${sha}" ]] || { err "train-match needs --repo and --sha"; exit 2; }
  require_cmd gh
  require_cmd jq
  require_cmd python3

  none() {
    emit reused "[]"
    emit reused_from ""
    emit train ""
    say "No green, landed merge train tested this tree: ${1}. Every job runs."
  }

  if ! tree="$(git -C "${REPO_ROOT}" rev-parse --verify -q "${sha}^{tree}")"; then
    none "the tree of ${sha} cannot be read"
    return 0
  fi
  list="${WORK}/trains"
  if ! gh api "repos/${repo}/pulls?state=all&sort=updated&direction=desc&per_page=${TRAIN_LOOKBACK}" \
      --jq ".[] | select((.head.ref | startswith(\"train/\")) and .head.repo.full_name == \"${repo}\"
                         and (.title | startswith(\"Merge train \")))
            | \"\(.number)\t\(.head.sha)\t\(.html_url)\"" >"${list}"; then
    none "the pull requests could not be listed"
    return 0
  fi
  # The list on fd 3, so no `gh` in the loop can read it from stdin.
  while IFS=$'\t' read -r number head_sha url <&3; do
    [[ -n "${number}" ]] || continue
    head_tree="$(gh api "repos/${repo}/git/commits/${head_sha}" --jq '.tree.sha')" || continue
    [[ "${head_tree}" == "${tree}" ]] || continue
    info "train #${number} (${head_sha}) has this commit's tree ${tree}"
    if ! gate_green "${repo}" "${head_sha}"; then
      warn "train #${number}'s ci-gate at ${head_sha} did not pass"
      continue
    fi
    if ! train_landed "${repo}" "${number}"; then
      warn "train #${number} did not close with \"${LANDED_PREFIX}\""
      continue
    fi
    if ! jobs="$(reusable_jobs)"; then
      none "ci_reuse.py's REUSABLE could not be read"
      return 0
    fi
    emit reused "${jobs}"
    emit reused_from "${url}"
    emit train "${number}"
    say "**Main skips its test jobs:** merge train #${number} (${url}) tested this exact tree (${tree}) at ${head_sha}, its ci-gate passed and it landed."
    return 0
  done 3<"${list}"
  none "none of the ${TRAIN_LOOKBACK} newest pull requests is one"
}

[[ $# -ge 1 ]] || { usage; exit 2; }
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
command="$1"
shift
case "${command}" in
  lane) lane "$@" ;;
  ui-pick) ui_pick "$@" ;;
  train-match) train_match "$@" ;;
  -h|--help) usage ;;
  *) err "unknown command: ${command}"; usage; exit 2 ;;
esac
