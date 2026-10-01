"""The `ready` label queues a pull request for GitHub's native auto-merge (#262).

`.github/workflows/auto-merge.yml` runs on `pull_request_target` [labeled]. It
is the one workflow here that runs with a write token on an event a pull
request's author triggers, so its shape is held rather than trusted:

* it never checks out, and never runs, the pull request's code;
* the pull request's title reaches a shell only through `env:`, never through
  a `${{ }}` expansion inside `run:` -- a title is text anyone opening a pull
  request chooses;
* its GITHUB_TOKEN asks for the least it needs;
* the merge is enabled with the GitHub App's token, NOT the GITHUB_TOKEN. A
  merge the GITHUB_TOKEN performs starts no workflow, so `application.yml`
  would not build the commit on main and `release.yml` would not fire -- with
  nothing red anywhere to say so;
* the squash subject is the pull request's title and number. A one-commit
  squash otherwise takes the commit's own message, which is how #238 put
  `swarm: work from task_...` on main as a headline.

The gate step's shell is also RUN, against a fake `gh` on PATH that records
every call, so the refusals are exercised rather than read. Nothing here
touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
AUTO_MERGE = WORKFLOWS / "auto-merge.yml"
CI_DOC = REPO / "docs" / "ci.md"

REFUSED_PREFIX = "[swarm] task_"
# Owner decision, 2026-09-28: branch protection requires only the checks that
# run on EVERY pull request. security.yml has no `pull_request` path filter;
# application.yml and terraform.yml both do, so none of their jobs -- shellcheck
# and the actionlint job included -- are guaranteed to report.
ALWAYS_RUN_WORKFLOW = "security.yml"
PATH_FILTERED_WORKFLOWS = ("application.yml", "terraform.yml")


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert AUTO_MERGE.is_file(), f"{AUTO_MERGE.relative_to(REPO)} does not exist"
    return _workflow(AUTO_MERGE)


@pytest.fixture(scope="module")
def job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    assert "enable" in jobs, f"expected a job keyed 'enable', found {sorted(jobs)}"
    return jobs["enable"]


@pytest.fixture(scope="module")
def disable_job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    assert "disable" in jobs, f"expected a job keyed 'disable', found {sorted(jobs)}"
    return jobs["disable"]


def test_the_workflow_has_exactly_the_enable_and_disable_jobs(workflow: dict):
    jobs = workflow.get("jobs") or {}
    assert set(jobs) == {"enable", "disable"}, sorted(jobs)


def _step(job: dict, step_id: str) -> dict:
    matches = [step for step in job.get("steps") or [] if step.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}, found {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Trigger and trust boundary
# ---------------------------------------------------------------------------


def test_it_runs_on_labeling_and_on_events_that_can_invalidate_the_label(workflow: dict):
    """MUTATION: add `pull_request` or `push`, or drop one of the four types --
    each of `synchronize`/`reopened`/`edited` is what lets the `disable` job
    catch a new head, and dropping one leaves that path unguarded."""
    on = workflow["on"]
    assert set(on) == {"pull_request_target"}, on
    assert set(on["pull_request_target"]["types"]) == {
        "labeled",
        "synchronize",
        "reopened",
        "edited",
    }, on["pull_request_target"]


def test_the_job_is_gated_on_the_ready_label(job: dict):
    condition = str(job.get("if") or "")
    assert re.search(r"github\.event\.label\.name\s*==\s*'ready'", condition), condition


def test_it_never_checks_out_or_runs_the_pull_requests_code(workflow: dict, job: dict):
    """MUTATION: add `- uses: actions/checkout@v5` with `ref: ${{ github.event.pull_request.head.sha }}`."""
    text = AUTO_MERGE.read_text()
    steps = job.get("steps") or []
    assert steps, "the job has no steps, so this checked nothing"
    for step in steps:
        uses = str(step.get("uses") or "")
        assert "checkout" not in uses, f"a pull_request_target job checks out code: {uses}"
    # The head SHA is read (HEAD_SHA) to PIN the check-runs read and both merge
    # calls to the reviewed commit -- fencing against a push after the label,
    # not a way to find or run the fork's code. Reading it is safe only
    # because there is no checkout step (asserted above) and nothing here
    # reads where the fork's code actually lives.
    assert "github.head_ref" not in text
    assert "pull_request.head.repo" not in text


def test_attacker_controlled_text_reaches_a_shell_only_through_env(job: dict):
    """A `${{ github.event.pull_request.title }}` inside `run:` is script injection.
    MUTATION: write `--subject "${{ github.event.pull_request.title }} (#...)"`."""
    visited = 0
    for step in job.get("steps") or []:
        run = step.get("run")
        if run is None:
            continue
        visited += 1
        assert "github.event" not in run, f"step {step.get('name')!r} expands event data inside run:"
    assert visited >= 2, f"read only {visited} run: steps"


def test_the_github_token_asks_for_the_least_it_needs(workflow: dict, job: dict):
    """Read the base branch's protection and the head commit's check runs;
    comment on a refusal. Nothing else.
    MUTATION: set `contents: write`, add `id-token: write`, or `permissions: write-all`."""
    assert workflow.get("permissions") == {}, workflow.get("permissions")
    assert job.get("permissions") == {
        "contents": "read",
        "checks": "read",
        "pull-requests": "write",
    }, job.get("permissions")


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


def test_the_merge_is_enabled_with_the_app_token_so_releases_still_fire(job: dict):
    """MUTATION: set the merge step's GH_TOKEN to `${{ github.token }}` or
    `${{ secrets.GITHUB_TOKEN }}` -- the merge then starts no push workflow."""
    token_step = _step(job, "app-token")
    assert str(token_step.get("uses", "")).startswith("actions/create-github-app-token@"), token_step
    inputs = token_step.get("with") or {}
    # Scoped to what enabling auto-merge needs, not the App's whole grant.
    # workflows: write, so a pull request touching .github/workflows also
    # auto-merges (owner decision, 2026-09-28) -- requesting it here grants
    # nothing until the owner creates the App with that permission.
    assert inputs.get("permission-contents") == "write", inputs
    assert inputs.get("permission-pull-requests") == "write", inputs
    assert inputs.get("permission-workflows") == "write", inputs
    assert "secrets." in str(inputs.get("private-key")), inputs

    merge = _step(job, "merge")
    token = str((merge.get("env") or {}).get("GH_TOKEN"))
    assert token == "${{ steps.app-token.outputs.token }}", token
    assert "github.token" not in json.dumps(merge) and "GITHUB_TOKEN" not in json.dumps(merge)


def test_the_merge_is_native_auto_merge_squashed_under_the_pr_title(job: dict):
    """MUTATION: drop `--auto` (merges now, green or not), drop `--subject`
    (a one-commit squash takes the commit message, #238), or use `--merge`."""
    run = _step(job, "merge")["run"]
    # Join continuation lines, so each `gh pr merge` is one logical command.
    joined = re.sub(r"\\\n\s*", " ", run)
    merges = [line.strip() for line in joined.splitlines() if "gh pr merge" in line]
    assert merges, run
    # The first attempt is always native auto-merge. The only other merge is
    # the already-green fallback, and it must say the same things.
    assert "--auto" in merges[0], merges
    for command in merges:
        args = command.split("gh pr merge", 1)[1]
        assert '"${PR_NUMBER}"' in args, args
        assert "--squash" in args, args
        assert "--merge " not in args and "--rebase" not in args, args
        assert '--subject "${PR_TITLE} (#${PR_NUMBER})"' in args, args
        assert "--admin" not in args, "--admin bypasses the required checks the auto-merge waits for"


def test_both_merge_calls_pin_to_the_reviewed_head_commit(job: dict):
    """A push after the `ready` label -- including the SwarmCloud worker's own
    push, or a fork push in the seconds before this job starts -- must not let
    either merge call squash a commit nobody reviewed.
    MUTATION: drop `--match-head-commit` from either `gh pr merge` call, or
    interpolate the sha through `${{ }}` instead of the `HEAD_SHA` env var."""
    run = _step(job, "merge")["run"]
    joined = re.sub(r"\\\n\s*", " ", run)
    merges = [line.strip() for line in joined.splitlines() if "gh pr merge" in line]
    assert len(merges) == 2, merges
    for command in merges:
        assert '--match-head-commit "${HEAD_SHA}"' in command, command


def test_the_head_sha_reaches_the_merge_step_only_through_env(job: dict):
    env = job.get("env") or {}
    assert env.get("HEAD_SHA") == "${{ github.event.pull_request.head.sha }}", env
    run = _step(job, "merge")["run"]
    assert "github.event" not in run


def test_a_direct_merge_happens_only_when_github_says_the_pr_is_already_clean(job: dict):
    """GitHub will not enable auto-merge on a CLEAN pull request; anything else
    that stops `--auto` must fail the job, not fall through to a merge.
    MUTATION: drop the CLEAN check, so any auto-merge error merges at once."""
    run = _step(job, "merge")["run"]
    auto = run.index("--auto")
    clean = run.index('!= "CLEAN"')
    direct = [m.start() for m in re.finditer(r"gh pr merge", run)]
    assert len(direct) == 2, direct
    assert auto < clean < direct[1], "the direct merge must sit behind the CLEAN check"
    assert "exit 1" in run[clean : direct[1]], "a non-CLEAN failure must fail the job"


def test_the_title_and_number_come_from_the_event(job: dict):
    env = job.get("env") or {}
    assert env.get("PR_TITLE") == "${{ github.event.pull_request.title }}", env
    assert env.get("PR_NUMBER") == "${{ github.event.pull_request.number }}", env
    assert env.get("BASE_REF") == "${{ github.event.pull_request.base.ref }}", env


def test_the_merge_runs_only_after_the_gate(job: dict):
    steps = job.get("steps") or []
    ids = [step.get("id") for step in steps]
    assert ids.index("gate") < ids.index("app-token") < ids.index("merge"), ids
    for step in steps[ids.index("gate") + 1 :]:
        assert "always()" not in str(step.get("if") or ""), step


# ---------------------------------------------------------------------------
# The gate, run
# ---------------------------------------------------------------------------

PROTECTED = {"name": "main", "protected": True, "protection": {"enabled": True, "required_status_checks": {
    "enforcement_level": "non_admins",
    "contexts": ["shellcheck", "trivy (repo)", "fmt / validate / tflint"],
    "checks": [{"context": "shellcheck", "app_id": 15368}],
}}}
UNPROTECTED = {"name": "main", "protected": False, "protection": {"enabled": False, "required_status_checks": {
    "enforcement_level": "off", "contexts": [], "checks": [],
}}}
# What `GET repos/{repo}/branches/main` returned for this repository on
# 2026-10-01: `protected` is true because a RULESET protects main, while the
# classic `.protection` block is off and lists no check. Read alone, it is
# "main has no required status checks" -- the refusal every `ready` label got
# (run 36822836560, #412; #403).
RULESET_ONLY_BRANCH = {"name": "main", "protected": True, "protection": {"enabled": False, "required_status_checks": {
    "enforcement_level": "off", "contexts": [], "checks": [],
}}}
# `GET repos/{repo}/rules/branches/main` on 2026-10-01: the effective rules of
# ruleset 24160219 (`main-protection`), five required checks.
RULESET_CHECKS = [
    "secret scan",
    "trivy (repo)",
    "checkov (terraform + kubernetes)",
    "platform policy assertions",
    "ci-gate",
]
_RULE_SOURCE = {"ruleset_source_type": "Repository", "ruleset_source": "bogdan-alexandrescu/SwarmCloud",
                "ruleset_id": 24160219}
RULESET_RULES = [
    {"type": "deletion", **_RULE_SOURCE},
    {"type": "non_fast_forward", **_RULE_SOURCE},
    {"type": "pull_request", **_RULE_SOURCE, "parameters": {"required_approving_review_count": 0}},
    {"type": "required_status_checks", **_RULE_SOURCE, "parameters": {
        "strict_required_status_checks_policy": False,
        "do_not_enforce_on_create": False,
        "required_status_checks": [{"context": c} for c in RULESET_CHECKS],
    }},
]

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "${1:-}" == "api" ]]; then
  # The endpoint is the argument starting `repos/`, wherever flags put it.
  path=""
  for arg in "$@"; do
    case "${arg}" in repos/*) path="${arg}" ;; esac
  done
  case "${path%%\?*}" in
    */check-runs) cat "${FAKE_GH_CHECK_RUNS}" ;;
    */rules/branches/*) cat "${FAKE_GH_RULES}" ;;
    */branches/*) cat "${FAKE_GH_BRANCH}" ;;
    *) echo "fake gh: unexpected endpoint: $*" >&2; exit 3 ;;
  esac
  exit 0
fi
if [[ "${1:-} ${2:-}" == "pr comment" ]]; then
  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --body-file) cat "$2" >> "${FAKE_GH_COMMENTS}"; shift 2 ;;
      --body) printf '%s\n' "$2" >> "${FAKE_GH_COMMENTS}"; shift 2 ;;
      *) shift ;;
    esac
  done
  exit 0
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


@pytest.fixture
def run_gate(job: dict, tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    script = _step(job, "gate")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(
        title: str,
        branch: dict,
        *,
        app_id: str = "123456",
        has_key: str = "true",
        base_ref: str = "main",
        head_sha: str = "0000000000000000000000000000000000abcd",
        check_runs: list[dict] | None = None,
        rules: list[dict] | None = None,
    ):
        branch_file = tmp_path / "branch.json"
        branch_file.write_text(json.dumps(branch))
        rules_file = tmp_path / "rules.json"
        # Default: no ruleset applies, so only classic protection can count.
        rules_file.write_text(json.dumps(rules if rules is not None else []))
        check_runs_file = tmp_path / "check-runs.json"
        # Default: nothing else has reported on this head, so item 5 passes.
        check_runs_file.write_text(json.dumps({"check_runs": check_runs if check_runs is not None else []}))
        log = tmp_path / "gh.log"
        comments = tmp_path / "comments.md"
        summary = tmp_path / "summary.md"
        for path in (log, comments, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
            "PR_NUMBER": "4242",
            "PR_TITLE": title,
            "BASE_REF": base_ref,
            "HEAD_SHA": head_sha,
            "MERGE_APP_ID": app_id,
            "HAS_MERGE_APP_KEY": has_key,
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_BRANCH": str(branch_file),
            "FAKE_GH_CHECK_RUNS": str(check_runs_file),
            "FAKE_GH_RULES": str(rules_file),
            "FAKE_GH_COMMENTS": str(comments),
        }
        proc = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False
        )
        return proc, log.read_text(), comments.read_text()

    return run


def test_a_fact_style_title_on_a_protected_branch_passes_the_gate(run_gate):
    """The control: without it, every refusal below could be a gate that refuses everything."""
    proc, calls, comments = run_gate("The release fires after a label merge", PROTECTED)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "api repos/bogdan-alexandrescu/SwarmCloud/branches/main" in calls, calls
    assert "pr comment" not in calls and comments == "", comments


@pytest.mark.parametrize(
    "title",
    [
        "[swarm] task_01J9ZK3Q8R",
        "[swarm] task_",
        "[swarm] task_abc and a description after it",
    ],
)
def test_a_swarm_task_title_is_refused_with_a_comment_saying_why(run_gate, title: str):
    """MUTATION: drop the prefix check, or match `[swarm]` as a glob bracket expression."""
    proc, calls, comments = run_gate(title, PROTECTED)
    assert proc.returncode != 0, "a [swarm] task_ title passed the gate"
    assert "pr comment 4242" in calls, calls
    assert REFUSED_PREFIX in comments, comments
    assert "title" in comments.lower() and "ready" in comments, comments
    # Refused before anything else is asked: no protection read needed to say no.
    assert "api " not in calls, calls


@pytest.mark.parametrize(
    "title",
    [
        "Fix [swarm] task_ handling in the integrator",
        "[swarm] output truncated marker is counted",
        "s] task_x",
        "[swarm]task_x",
    ],
)
def test_only_the_prefix_is_refused(run_gate, title: str):
    """A glob `[swarm]` is a bracket expression matching ONE character, so an
    unquoted pattern would refuse `s] task_x` and pass the real prefix."""
    proc, calls, comments = run_gate(title, PROTECTED)
    assert proc.returncode == 0, (title, proc.stderr, comments)
    assert comments == "", comments


@pytest.mark.parametrize(
    "title",
    [
        "[Swarm] task_01J9ZK3Q8R",
        "[SWARM] TASK_x",
        "   [swarm] task_x",
        "\t[swarm] task_abc and a description after it",
    ],
)
def test_the_swarm_task_title_check_ignores_case_and_leading_whitespace(run_gate, title: str):
    """MUTATION: compare `${PR_TITLE}` directly instead of a trimmed, lower-cased copy."""
    proc, calls, comments = run_gate(title, PROTECTED)
    assert proc.returncode != 0, (title, proc.stderr, comments)
    assert "pr comment 4242" in calls, calls
    assert REFUSED_PREFIX in comments.lower(), comments


def test_a_base_branch_other_than_main_is_refused(run_gate):
    """Only `main` is protected and only `main`'s push starts the release, so a
    pull request based on anything else has nothing for auto-merge to gate on.
    MUTATION: drop this check, or compare against a variable instead of the
    literal `main`."""
    proc, calls, comments = run_gate("A fact-style headline", PROTECTED, base_ref="release/1.0")
    assert proc.returncode != 0, "a non-main base branch passed the gate"
    assert "pr comment 4242" in calls, calls
    assert "main" in comments.lower(), comments
    # Refused before the branch protection is even read.
    assert "api " not in calls, calls


def test_a_base_branch_without_required_checks_is_refused(run_gate):
    """With no required check, `--auto` has nothing to wait for and the pull
    request merges at once, red or not. MUTATION: remove the protection read."""
    proc, calls, comments = run_gate("A fact-style headline", UNPROTECTED)
    assert proc.returncode != 0, "an unprotected base branch passed the gate"
    assert "pr comment 4242" in calls, calls
    assert "required" in comments.lower() and "docs/ci.md" in comments, comments


def test_protection_without_checks_is_refused(run_gate):
    """`protected: true` alone is not enough: the checks list is what auto-merge waits on."""
    branch = {"name": "main", "protected": True, "protection": {"enabled": True, "required_status_checks": {
        "enforcement_level": "off", "contexts": [], "checks": []}}}
    proc, _calls, comments = run_gate("A fact-style headline", branch)
    assert proc.returncode != 0
    assert "required" in comments.lower(), comments


# Owner decision, 2026-10-01: main is protected by a repository RULESET
# (24160219), not classic branch protection, so the gate counts the effective
# rules for the branch and unions classic protection's checks into them.


def test_a_ruleset_s_required_checks_count_when_classic_protection_has_none(run_gate):
    """The defect of 2026-10-01: every `ready` label was refused with "main has
    no required status checks" while ruleset 24160219 required five.
    MUTATION: read only `branches/main` `.protection`, as before."""
    proc, calls, comments = run_gate("A fact-style headline", RULESET_ONLY_BRANCH, rules=RULESET_RULES)
    assert proc.returncode == 0, proc.stderr + proc.stdout + comments
    assert comments == "", comments
    assert "rules/branches/main" in calls, calls
    assert "gate passed: 5 required check(s) on main" in proc.stdout, proc.stdout


def test_classic_and_ruleset_checks_are_counted_once_each(run_gate):
    """A repository that keeps classic protection, or moves back to it, still
    counts; a check required by both is one check, not two.
    MUTATION: drop the classic read, drop the ruleset read, or drop `unique`."""
    branch = {"name": "main", "protected": True, "protection": {"enabled": True, "required_status_checks": {
        "enforcement_level": "non_admins",
        "contexts": ["trivy (repo)", "shellcheck"],
        "checks": [{"context": "trivy (repo)", "app_id": 15368}, {"context": "shellcheck", "app_id": 15368}],
    }}}
    proc, _calls, comments = run_gate("A fact-style headline", branch, rules=RULESET_RULES)
    assert proc.returncode == 0, proc.stderr + proc.stdout + comments
    # Five from the ruleset, plus shellcheck; trivy (repo) is in both.
    assert "gate passed: 6 required check(s) on main" in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [rule for rule in RULESET_RULES if rule["type"] != "required_status_checks"],
        [rule if rule["type"] != "required_status_checks" else {**rule, "parameters": {
            **rule["parameters"], "required_status_checks": []}} for rule in RULESET_RULES],
    ],
    ids=["no-rules", "ruleset-without-status-checks", "status-check-rule-with-no-checks"],
)
def test_no_required_check_in_either_ruleset_or_classic_protection_is_refused(run_gate, rules: list[dict]):
    """A ruleset that protects main without requiring a check gives auto-merge
    nothing to wait for. MUTATION: count the rules instead of their checks."""
    proc, calls, comments = run_gate("A fact-style headline", RULESET_ONLY_BRANCH, rules=rules)
    assert proc.returncode != 0, "a base branch requiring no check passed the gate"
    assert "rules/branches/main" in calls, calls
    assert "pr comment 4242" in calls, calls
    assert "required" in comments.lower() and "docs/ci.md" in comments, comments
    assert "ruleset" in comments.lower(), comments


def test_the_refusal_names_the_ruleset_not_classic_branch_protection(run_gate):
    """The comment sends the owner to the mechanism this repository uses.
    MUTATION: keep the old "applies branch protection" wording."""
    proc, _calls, comments = run_gate("A fact-style headline", UNPROTECTED)
    assert proc.returncode != 0
    assert "ruleset" in comments.lower(), comments


@pytest.mark.parametrize("app_id,has_key", [("", "true"), ("123456", "false"), ("", "false")])
def test_a_missing_merge_app_is_refused_rather_than_merged_with_the_github_token(run_gate, app_id, has_key):
    """The fallback that must NOT exist: merging with the GITHUB_TOKEN would
    land the commit and start no build and no release."""
    proc, calls, comments = run_gate("A fact-style headline", PROTECTED, app_id=app_id, has_key=has_key)
    assert proc.returncode != 0
    assert "pr comment 4242" in calls, calls
    assert "MERGE_APP" in comments and "release" in comments, comments


def test_a_title_with_shell_metacharacters_is_inert(run_gate, tmp_path: Path):
    """The title reaches bash as data. MUTATION: `eval` it, or interpolate it into a command."""
    marker = tmp_path / "pwned"
    title = f'$(touch {marker}) `touch {marker}` "; touch {marker}; echo "'
    proc, _calls, _comments = run_gate(title, PROTECTED)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), "the title was executed"


# ---------------------------------------------------------------------------
# Gate item 5: a check that already ran on the head and is failing or still
# running holds the merge too, even when it is not one of the required four.
# ---------------------------------------------------------------------------


def test_a_failing_check_on_the_head_is_refused(run_gate):
    """MUTATION: drop item 5, or only look at required checks."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": "swarm-ui typecheck / component tests", "status": "completed", "conclusion": "failure"}],
    )
    assert proc.returncode != 0, "a failing, non-required check on the head passed the gate"
    assert "pr comment 4242" in calls, calls
    assert "swarm-ui typecheck / component tests" in comments, comments
    assert "check-runs" in calls, calls


def test_a_still_running_check_on_the_head_is_refused(run_gate):
    """MUTATION: only refuse on `conclusion == "failure"`, ignoring an
    incomplete `status`."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": "kubernetes manifests", "status": "in_progress", "conclusion": None}],
    )
    assert proc.returncode != 0, "a still-running check on the head passed the gate"
    assert "kubernetes manifests" in comments, comments


@pytest.mark.parametrize("conclusion", ["timed_out", "action_required", "cancelled"])
def test_other_failing_conclusions_are_also_refused(run_gate, conclusion: str):
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": "shellcheck", "status": "completed", "conclusion": conclusion}],
    )
    assert proc.returncode != 0, conclusion
    assert "shellcheck" in comments, comments


def test_a_successful_or_skipped_check_does_not_block(run_gate):
    """The control for item 5: green or skipped checks are not signal to hold on."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[
            {"name": "shellcheck", "status": "completed", "conclusion": "success"},
            {"name": "release workflow wiring (actionlint)", "status": "completed", "conclusion": "skipped"},
            {"name": "format / unit tests", "status": "completed", "conclusion": "neutral"},
        ],
    )
    assert proc.returncode == 0, comments
    assert comments == "", comments


def test_the_gate_excludes_its_own_still_running_check_run(run_gate, job: dict):
    """MUTATION: drop the self-exclusion -- this job's own check run is always
    still `in_progress` while the gate script that reports it is running, so
    without it the gate would refuse every single run."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": job["name"], "status": "in_progress", "conclusion": None}],
    )
    assert proc.returncode == 0, comments
    assert comments == "", comments


def test_the_self_exclusion_name_matches_the_jobs_actual_name(job: dict):
    """The literal the gate script excludes by must be this job's real `name:`,
    not a copy that can drift when the job is renamed."""
    script = _step(job, "gate")["run"]
    assert f'!= "{job["name"]}"' in script, script


def test_the_check_runs_are_read_for_the_exact_head_commit(run_gate):
    proc, calls, _comments = run_gate("A fact-style headline", PROTECTED, head_sha="cafef00d")
    assert proc.returncode == 0, _comments
    assert "api repos/bogdan-alexandrescu/SwarmCloud/commits/cafef00d/check-runs" in calls, calls


# ---------------------------------------------------------------------------
# The disable job: a push after `ready` must not leave a stale auto-merge armed
# ---------------------------------------------------------------------------


def test_the_disable_job_runs_on_a_push_or_reopen_but_never_on_the_labeling_itself(disable_job: dict):
    """MUTATION: drop the `action != 'labeled'` guard -- the label-add event
    would then immediately disable the auto-merge the other job just enabled."""
    condition = str(disable_job.get("if") or "")
    assert re.search(r"github\.event\.action\s*!=\s*'labeled'", condition), condition
    assert re.search(
        r"contains\(\s*github\.event\.pull_request\.labels\.\*\.name\s*,\s*'ready'\s*\)", condition
    ), condition
    assert "edited" in condition and "changes.base" in condition, condition


def test_the_disable_job_never_checks_out_pr_code_and_uses_least_privilege(disable_job: dict):
    """MUTATION: add a checkout step, or widen permissions past pull-requests: write."""
    assert disable_job.get("permissions") == {"pull-requests": "write"}, disable_job.get("permissions")
    steps = disable_job.get("steps") or []
    assert steps, "the disable job has no steps, so this checked nothing"
    for step in steps:
        assert "checkout" not in str(step.get("uses") or "")


FAKE_GH_DISABLE = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "${1:-} ${2:-}" == "pr merge" ]]; then
  exit 0
fi
if [[ "${1:-} ${2:-}" == "pr edit" ]]; then
  exit 0
fi
if [[ "${1:-} ${2:-}" == "pr comment" ]]; then
  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --body-file) cat "$2" >> "${FAKE_GH_COMMENTS}"; shift 2 ;;
      --body) printf '%s\n' "$2" >> "${FAKE_GH_COMMENTS}"; shift 2 ;;
      *) shift ;;
    esac
  done
  exit 0
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


@pytest.fixture
def run_disable(disable_job: dict, tmp_path: Path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    steps = disable_job.get("steps") or []
    assert len(steps) == 1, f"expected one step in the disable job, found {len(steps)}"
    script = steps[0]["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH_DISABLE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "gh.log"
    comments = tmp_path / "comments.md"
    log.write_text("")
    comments.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
        "PR_NUMBER": "4242",
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_COMMENTS": str(comments),
    }
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    return proc, log.read_text(), comments.read_text()


def test_a_push_after_ready_disables_auto_merge_and_removes_the_label(run_disable):
    """RUN, against a fake `gh`: the disable job's own shell, not just its YAML.
    MUTATION: drop the `--disable-auto` call, or the `--remove-label ready` call."""
    proc, calls, comments = run_disable
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "pr merge 4242 --disable-auto" in calls, calls
    assert "pr edit 4242 --remove-label ready" in calls, calls
    assert "pr comment 4242" in calls, calls
    assert "ready" in comments.lower(), comments


# ---------------------------------------------------------------------------
# Wiring: linted on a pull request, and documented
# ---------------------------------------------------------------------------


def test_the_workflow_is_linted_on_every_pull_request_that_touches_it():
    """It runs only on pull_request_target, from main, so a broken expression
    would first show on the first `ready` label after merge."""
    application = _workflow(WORKFLOWS / "application.yml")
    paths = application["on"]["pull_request"]["paths"]
    assert ".github/workflows/auto-merge.yml" in paths, paths
    lint = [
        step.get("run") or ""
        for step in application["jobs"]["workflows"]["steps"]
        if "actionlint" in (step.get("run") or "")
    ]
    assert any(".github/workflows/auto-merge.yml" in run for run in lint), lint


def _job_check_names(path: Path) -> set[str]:
    """The check-run names a workflow's jobs report on a pull request."""
    workflow = _workflow(path)
    names = set()
    for job_id, job in (workflow.get("jobs") or {}).items():
        condition = str(job.get("if") or "")
        if "!= 'pull_request'" in condition or ("pull_request" not in condition and "schedule" in condition):
            continue
        names.add(str(job.get("name") or job_id))
    return names


def test_docs_carry_the_branch_protection_command_for_the_checks_that_always_run():
    """The owner applies it; the doc must name exactly the checks that run on
    every pull request -- security.yml's jobs -- pinned to GitHub Actions so
    another App cannot satisfy them, and nothing from a path-filtered
    workflow, which would hold every pull request that skipped it forever.
    MUTATION: rename a job in security.yml without updating the command, or
    add a job from application.yml/terraform.yml to the required list."""
    text = CI_DOC.read_text()
    match = re.search(
        r"```bash\n(gh api --method PUT \\\n\s+repos/bogdan-alexandrescu/SwarmCloud/branches/main/protection.*?)```",
        text,
        re.DOTALL,
    )
    assert match, "docs/ci.md does not carry the branch-protection `gh api` command"
    block = match.group(1)
    payload_match = re.search(r"<<'JSON'\n(.*?)\nJSON", block, re.DOTALL)
    assert payload_match, block
    payload = json.loads(payload_match.group(1))
    required = payload["required_status_checks"]
    # Not strict: auto-merge does not update a branch, so a strict rule would
    # hold every pull request that fell behind main forever.
    assert required["strict"] is False, required
    checks = required["checks"]
    assert checks and all(check.get("app_id") == 15368 for check in checks), checks
    contexts = {check["context"] for check in checks}

    always_run = _job_check_names(WORKFLOWS / ALWAYS_RUN_WORKFLOW)
    assert always_run, ALWAYS_RUN_WORKFLOW
    missing = always_run - contexts
    assert not missing, f"a check that always runs is not required: {sorted(missing)}"
    extra = contexts - always_run
    assert not extra, f"a required check is not one of {ALWAYS_RUN_WORKFLOW}'s always-run jobs: {sorted(extra)}"

    for name in PATH_FILTERED_WORKFLOWS:
        reported = _job_check_names(WORKFLOWS / name)
        assert reported, name  # the control: these workflows do have jobs
        leaked = contexts & reported
        assert not leaked, f"a required check comes from path-filtered {name}: {sorted(leaked)}"

    for key in ("enforce_admins", "required_pull_request_reviews", "restrictions"):
        assert key in payload, f"the protection PUT rejects a body without {key!r}"


def test_docs_say_auto_merge_must_be_allowed_on_the_repository():
    text = CI_DOC.read_text()
    assert "allow_auto_merge=true" in text
    assert "auto-merge.yml" in text
