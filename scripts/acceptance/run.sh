#!/usr/bin/env bash
# The acceptance suite: REAL tasks against a deployed environment, judged by
# what they PRODUCED.
#
# Usage: scripts/acceptance/run.sh [--only <group>[,<group>...]] [--list]
#
#   groups   mock  generic  claude-code  workflow  browser
#   --only   run these groups only (repeatable, or comma-separated)
#   --list   print every group and check, touch nothing, exit 0
#
# WHY IT EXISTS (owner decision, 2026-09-29). The smoke and e2e suites prove a
# task reached SUCCEEDED. Every browser task this platform reported as a
# success screenshotted about:blank, and SUCCEEDED said nothing about that. So
# every check here reads an output -- a PNG's pixels, a patch's lines, a
# staged file's bytes, a pull request's title, a refusal's code -- and a
# terminal state is only its precondition. docs/acceptance.md says what each
# check asserts and why.
#
# EACH CHECK prints PASS or FAIL with a one-line reason and the task id, or
# SKIP with the reason it could not be measured (a profile input not yet
# deployed, a pool an operator closed, a credential the tenant lacks). A skip
# is never a pass: testlib's summary lists every one by name. The exit status
# is non-zero when any check FAILED.
#
# WHERE IT RUNS. Like the other suites, inside the VPC: in the release as
# `./scripts/verify-remote.sh acceptance/<group>`, one swarm-verify execution
# per group (scripts/acceptance/<group>.sh), because the job's 30-minute
# timeout would not hold them all. Anywhere that can reach the API also works,
# with the caller's own identity and tenant.
#
# WHAT IT SPENDS. The claude-code group and the workflow group's integrate
# chain run six claude-code tasks on one- or two-sentence prompts, against the
# caller's tenant's subscription. The direct-pr and integrate checks open real
# pull requests on this repository; each check closes its own when this run
# holds a GitHub token (SWARM_ACCEPTANCE_GITHUB_TOKEN, GH_TOKEN or
# GITHUB_TOKEN), and the release sweeps them afterwards otherwise
# (scripts/acceptance/github-cleanup.sh).
#
# Environment: SWARM_ACCEPTANCE_REF (default main), SWARM_ACCEPTANCE_TIMEOUT
# (900), SWARM_ACCEPTANCE_ADMIT_WAIT (300), SWARM_ACCEPTANCE_ISSUE (77) and
# SWARM_ACCEPTANCE_ISSUE_EXPECT (bootstrap.sh).

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../lib/common.sh"
SUITE_NAME="acceptance"
# shellcheck source=../lib/testlib.sh
source "${REPO_ROOT}/scripts/lib/testlib.sh"
# shellcheck source=lib.sh
source "${REPO_ROOT}/scripts/acceptance/lib.sh"
# shellcheck source=groups/mock.sh
source "${REPO_ROOT}/scripts/acceptance/groups/mock.sh"
# shellcheck source=groups/generic.sh
source "${REPO_ROOT}/scripts/acceptance/groups/generic.sh"
# shellcheck source=groups/claude-code.sh
source "${REPO_ROOT}/scripts/acceptance/groups/claude-code.sh"
# shellcheck source=groups/workflow.sh
source "${REPO_ROOT}/scripts/acceptance/groups/workflow.sh"
# shellcheck source=groups/browser.sh
source "${REPO_ROOT}/scripts/acceptance/groups/browser.sh"

ALL_GROUPS=(mock generic claude-code workflow browser)

#: A group `--only` accepts that is NEVER in ALL_GROUPS and never appears in
#: --list's output or a normal run's group list. It does no platform work and
#: exists only so a unit test can prove the exit-code and progress-reporting
#: behaviour below (a mid-run crash must still exit nonzero and say how far the
#: suite got) without a deployment to talk to.
SELFTEST_CRASH_GROUP="_selftest_crash"
#: Named to match fn_name(SELFTEST_CRASH_GROUP) + "_checks" / "run_" + fn_name(...),
#: the same convention every real group's <group>_checks / run_<group> follows
#: (fn_name only replaces "-" with "_", and this group's name has no dashes).
_selftest_crash_checks() {
  cat <<'EOF'
selftest: deliberately crashes to prove a mid-run failure still exits nonzero
EOF
}
run__selftest_crash() {
  step "Acceptance: ${SELFTEST_CRASH_GROUP}"
  acc_check "$(_selftest_crash_checks)"
  local selftest_required_var
  : "${selftest_required_var:?selftest: this crash is deliberate -- proving the run.sh exit trap reports it and exits nonzero}"
}

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }

is_group() {
  local g
  [[ "$1" == "${SELFTEST_CRASH_GROUP}" ]] && return 0
  for g in "${ALL_GROUPS[@]}"; do
    [[ "${g}" == "$1" ]] && return 0
  done
  return 1
}

SELECTED=()
LIST=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)
      [[ $# -ge 2 ]] || die "--only needs a group: ${ALL_GROUPS[*]}"
      IFS=',' read -r -a wanted <<<"$2"
      for g in ${wanted[@]+"${wanted[@]}"}; do
        is_group "${g}" || die "unknown group '${g}'; the groups are: ${ALL_GROUPS[*]}"
        SELECTED+=("${g}")
      done
      shift 2 ;;
    --list) LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "${#SELECTED[@]}" -gt 0 ]] || SELECTED=("${ALL_GROUPS[@]}")

# The function names a group's checks and runner are spelled with: claude-code
# becomes claude_code_checks / run_claude_code.
fn_name() { printf '%s' "${1//-/_}"; }

if [[ "${LIST}" -eq 1 ]]; then
  for g in "${SELECTED[@]}"; do
    printf '%s\n' "${g}"
    "$(fn_name "${g}")_checks" | sed 's/^/  /'
  done
  exit 0
fi

step "Acceptance: ${PROJECT_ID} / ${ENVIRONMENT} -- groups: ${SELECTED[*]}"
if [[ "${SELECTED[*]}" == "${SELFTEST_CRASH_GROUP}" ]]; then
  # The self-test proves the exit trap, not the platform: skip the readyz
  # probe so a unit test can run it with no deployment and no credentials.
  info "selftest: skipping require_platform -- this group talks to nothing"
else
  require_platform
  info "api ${API_URL:-$(api_url)}; fixtures ${ACC_REPOSITORY_URL} @ ${ACC_REF}"
fi
acc_init

#: How many checks this run is answerable for, so a crash can say how far it
#: got. Computed the same way --list enumerates them: one line per check, per
#: selected group's `_checks` function.
ACC_TOTAL_CHECKS=0
for g in "${SELECTED[@]}"; do
  ACC_TOTAL_CHECKS=$(( ACC_TOTAL_CHECKS + $("$(fn_name "${g}")_checks" | wc -l) ))
done

#: Set only right before the two intended exits below. If the trap fires
#: while this is still 0, the run ended some other way -- an unhandled error,
#: an unbound variable, a killed process -- and that is reported as a crash,
#: not silently exited 0.
ACC_FINISHED=0

_acc_exit_trap() {
  # $? here is whatever caused the shell to exit -- an explicit `exit 0`/`exit
  # 1` below, or a mid-run crash (an unbound variable, a command `set -e`
  # caught). Capturing it as the FIRST statement, before acc_cleanup runs, is
  # what makes the reported and final exit code the CRASH's, not
  # acc_cleanup's: acc_cleanup (cancel_all + rm -rf) almost always succeeds,
  # and without this capture the shell exits with ITS status -- 0 -- silently
  # turning a crashed run into a green one (PR #358's live run, finding 2).
  local ec=$?
  acc_cleanup
  if [[ "${ACC_FINISHED}" != "1" ]]; then
    [[ "${ec}" -ne 0 ]] || ec=1
    err "${SUITE_NAME}: the suite did not finish -- crashed after ${TESTS_RUN} of ${ACC_TOTAL_CHECKS} checks visited (exit ${ec})"
  fi
  exit "${ec}"
}
trap _acc_exit_trap EXIT

for g in "${SELECTED[@]}"; do
  "run_$(fn_name "${g}")"
done

ACC_FINISHED=1
if t_summary; then
  exit 0
fi
exit 1
