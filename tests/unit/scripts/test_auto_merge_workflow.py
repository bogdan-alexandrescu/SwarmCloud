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


@pytest.fixture(scope="module")
def close_job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    assert "close-issues" in jobs, f"expected a job keyed 'close-issues', found {sorted(jobs)}"
    return jobs["close-issues"]


@pytest.fixture(scope="module")
def requeue_job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    assert "requeue" in jobs, f"expected a job keyed 'requeue', found {sorted(jobs)}"
    return jobs["requeue"]


def test_the_workflow_has_exactly_the_enable_requeue_disable_and_close_issues_jobs(workflow: dict):
    jobs = workflow.get("jobs") or {}
    assert set(jobs) == {"enable", "requeue", "disable", "close-issues"}, sorted(jobs)


def _step(job: dict, step_id: str) -> dict:
    matches = [step for step in job.get("steps") or [] if step.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}, found {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Trigger and trust boundary
# ---------------------------------------------------------------------------


def test_it_runs_on_labeling_and_on_events_that_can_invalidate_the_label(workflow: dict):
    """MUTATION: add `pull_request` or `push`, or drop one of the five types --
    each of `synchronize`/`reopened`/`edited` is what lets the `disable` job
    catch a new head, and dropping one leaves that path unguarded; `closed`
    is what lets `close-issues` see the merge (#621). `workflow_run` and
    `workflow_dispatch` are the re-evaluation of a `ready` label that landed
    while checks were still running (#697), held below."""
    on = workflow["on"]
    assert set(on) == {"pull_request_target", "workflow_run", "workflow_dispatch"}, on
    assert set(on["pull_request_target"]["types"]) == {
        "labeled",
        "synchronize",
        "reopened",
        "edited",
        "closed",
    }, on["pull_request_target"]


def test_the_job_is_gated_on_the_ready_label(job: dict):
    condition = str(job.get("if") or "")
    assert re.search(r"github\.event\.label\.name\s*==\s*'ready'", condition), condition
    # The label path is only the label event itself; the other way in is the
    # re-evaluation dispatch, whose `resolve` step requires `ready` on the PR.
    assert re.search(r"github\.event\.action\s*==\s*'labeled'", condition), condition
    assert re.search(r"github\.event_name\s*==\s*'workflow_dispatch'", condition), condition
    assert "workflow_run" not in condition, "a workflow_run event carries no pull request; requeue dispatches"


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
    """Through the `pr` step's output, which is the event's head on a label
    and the API's head on a re-evaluation."""
    resolve_env = _step(job, "pr").get("env") or {}
    assert resolve_env.get("EVENT_HEAD_SHA") == "${{ github.event.pull_request.head.sha }}", resolve_env
    for step_id in ("gate", "merge"):
        env = _step(job, step_id).get("env") or {}
        assert env.get("HEAD_SHA") == "${{ steps.pr.outputs.head_sha }}", (step_id, env)
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
    """On a label they are the event's; on a re-evaluation the number is the
    dispatch input and the rest is read back from the API by the `pr` step."""
    env = job.get("env") or {}
    assert env.get("PR_NUMBER") == "${{ github.event.pull_request.number || inputs.pr }}", env
    assert env.get("EVENT_NAME") == "${{ github.event_name }}", env
    resolve_env = _step(job, "pr").get("env") or {}
    assert resolve_env.get("EVENT_TITLE") == "${{ github.event.pull_request.title }}", resolve_env
    assert resolve_env.get("EVENT_BASE_REF") == "${{ github.event.pull_request.base.ref }}", resolve_env
    gate_env = _step(job, "gate").get("env") or {}
    assert gate_env.get("PR_TITLE") == "${{ steps.pr.outputs.title }}", gate_env
    assert gate_env.get("BASE_REF") == "${{ steps.pr.outputs.base_ref }}", gate_env
    merge_env = _step(job, "merge").get("env") or {}
    assert merge_env.get("PR_TITLE") == "${{ steps.pr.outputs.title }}", merge_env


def test_the_merge_runs_only_after_the_gate(job: dict):
    steps = job.get("steps") or []
    ids = [step.get("id") for step in steps]
    assert ids.index("pr") < ids.index("gate") < ids.index("app-token") < ids.index("merge"), ids
    for step in steps[ids.index("gate") + 1 :]:
        assert "always()" not in str(step.get("if") or ""), step


def test_the_token_is_minted_and_the_merge_made_only_when_the_gate_says_merge(job: dict):
    """A gate that WAITS exits 0, so "the gate step succeeded" is no longer
    "the gate passed". MUTATION: drop either `if:`, and a pull request whose
    checks are still running is merged (or auto-merge is armed on it) at once."""
    assert _step(job, "gate").get("if") == "steps.pr.outputs.evaluate == 'true'"
    for step_id in ("app-token", "merge"):
        assert _step(job, step_id).get("if") == "steps.gate.outputs.decision == 'merge'", step_id


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
# A re-evaluation's refusal turns auto-merge off and drops `ready`. Only
# exactly those: any other `pr merge` from the gate is a merge it must not make.
if [[ "$*" == "pr merge ${PR_NUMBER} --disable-auto" || "$*" == "pr edit ${PR_NUMBER} --remove-label ready" ]]; then
  exit 0
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


def _outputs(path: Path) -> dict[str, str]:
    """Parse a $GITHUB_OUTPUT file the way the runner does: `k=v` lines, and
    `k<<DELIM` ... `DELIM` blocks for multi-line values."""
    outputs: dict[str, str] = {}
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            end = lines.index(delimiter, i + 1)
            outputs[key] = "\n".join(lines[i + 1 : end])
            i = end + 1
            continue
        key, _, value = line.partition("=")
        outputs[key] = value
        i += 1
    return outputs


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
        event_name: str = "pull_request_target",
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
        # The test reads the gate's `decision` from here (_outputs).
        output = tmp_path / "output.txt"
        for path in (log, comments, summary, output):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
            "EVENT_NAME": event_name,
            "PR_NUMBER": "4242",
            "PR_TITLE": title,
            "BASE_REF": base_ref,
            "HEAD_SHA": head_sha,
            "MERGE_APP_ID": app_id,
            "HAS_MERGE_APP_KEY": has_key,
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
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


@pytest.mark.parametrize("status", ["in_progress", "queued"])
def test_a_still_running_check_on_the_head_is_queued_not_refused(run_gate, tmp_path: Path, status: str):
    """#697: `ready` landed while CI ran, the gate refused, and nothing ever
    looked again. Now it waits: no refusal, no merge, no armed auto-merge,
    and one comment saying what it waits for and when it looks again.
    MUTATION: refuse on an incomplete `status` again, or let it fall
    through to `decision=merge`."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": "kubernetes manifests", "status": status, "conclusion": None}],
    )
    assert proc.returncode == 0, proc.stderr + comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "wait"
    assert "Not queued" not in comments, comments
    assert "Queued for auto-merge" in comments and "kubernetes manifests" in comments, comments
    assert "re-evaluated" in comments, comments
    assert "pr merge" not in calls and "pr edit" not in calls, calls


def test_a_failing_check_beside_a_running_one_is_refused_not_queued(run_gate, tmp_path: Path):
    """Red at the head already decides it: waiting for the rest cannot turn it green.
    MUTATION: test for running checks before failing ones."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[
            {"name": "kubernetes manifests", "status": "in_progress", "conclusion": None},
            {"name": "shellcheck", "status": "completed", "conclusion": "failure"},
        ],
    )
    assert proc.returncode != 0
    assert "Not queued for auto-merge" in comments and "shellcheck" in comments, comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "refused"


def test_a_green_gate_says_merge(run_gate, tmp_path: Path):
    """The control for the two above: a gate that never says `merge` would pass them."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": "shellcheck", "status": "completed", "conclusion": "success"}],
    )
    assert proc.returncode == 0, comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "merge"


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


def test_the_disable_job_never_runs_on_the_close(disable_job: dict):
    """A merged pull request still carries `ready`; without this guard its
    `closed` event would try to disable auto-merge and comment "this pull
    request's head changed" on a merged pull request.
    MUTATION: drop the `action != 'closed'` guard."""
    condition = str(disable_job.get("if") or "")
    assert re.search(r"github\.event\.action\s*!=\s*'closed'", condition), condition


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


# ---------------------------------------------------------------------------
# Parity: the merge step restates gates 1, 3 and 5 (#295, merge-step.md §8)
# ---------------------------------------------------------------------------
#
# ONE RULE, TWO STATEMENTS. The `merge` worker action
# (apps/agent-worker/agent_worker/merge.py) restates this gate's title rule
# (1), its required-checks-exist rule (3) and its no-failing-or-running-check
# rule (5). Every rule restated in a second place here has drifted, so the
# table below runs each case through BOTH -- the gate's real shell, against
# the fake `gh` above, and the merge's own functions -- and fails when they
# answer differently, except where the table says they DELIBERATELY differ.
# Where they differ the merge is always the stricter: it is the unattended
# path, and §8 forbids it ever being looser than the human one.

from agent_worker import forge as worker_forge  # noqa: E402
from agent_worker import merge as worker_merge  # noqa: E402

#: (title, gate refuses, merge refuses, why they differ or None)
TITLE_PARITY = [
    ("[swarm] task_01J9ZK3Q8R", True, True, None),
    ("[Swarm] task_x", True, True, None),
    ("   [swarm] task_x", True, True, None),
    ("\t[SWARM] TASK_abc and more", True, True, None),
    ("A fact-style headline", False, False, None),
    ("s] task_x", False, False, None),
    ("[swarm] output truncated marker is counted", False, False, None),
    ("Fix [swarm] task_ handling in the integrator", False, True,
     "the worker's own pr-title.txt rule (#214) refuses the retired shape anywhere"),
    ("[swarm]task_x", False, True,
     "the worker's own pr-title.txt rule (#214) allows any spacing"),
]


@pytest.mark.parametrize(("title", "gate_refuses", "merge_refuses", "why"), TITLE_PARITY,
                         ids=[t[0] for t in TITLE_PARITY])
def test_the_merge_steps_title_rule_matches_gate_1(run_gate, title, gate_refuses, merge_refuses, why):
    proc, _calls, _comments = run_gate(title, PROTECTED)
    assert (proc.returncode != 0) is gate_refuses, (title, proc.stderr)
    assert worker_merge.title_is_placeholder(title) is merge_refuses, title
    if gate_refuses:
        assert merge_refuses, f"the merge step is looser than gate 1 on {title!r}"
    if gate_refuses != merge_refuses:
        assert why, f"{title!r}: the two disagree and the table does not say why"


#: (status, conclusion, gate refuses, merge refuses, why they differ or None)
CHECK_PARITY = [
    ("completed", "success", False, False, None),
    ("completed", "skipped", False, False, None),
    ("completed", "neutral", False, False, None),
    ("completed", "failure", True, True, None),
    ("completed", "timed_out", True, True, None),
    ("completed", "action_required", True, True, None),
    ("completed", "cancelled", True, True, None),
    ("in_progress", None, True, True, None),
    ("queued", None, True, True, None),
    ("completed", "stale", False, True,
     "gate 5 holds only on four conclusions; the merge tolerates only success, skipped, neutral"),
    ("completed", "startup_failure", False, True,
     "gate 5 holds only on four conclusions; the merge tolerates only success, skipped, neutral"),
]


@pytest.mark.parametrize(("status", "conclusion", "gate_refuses", "merge_refuses", "why"),
                         CHECK_PARITY, ids=[f"{c[0]}-{c[1]}" for c in CHECK_PARITY])
def test_the_merge_steps_other_check_rule_matches_gate_5(
    run_gate, tmp_path, status, conclusion, gate_refuses, merge_refuses, why
):
    run = {"name": "terraform", "status": status, "conclusion": conclusion}
    proc, _calls, _comments = run_gate("A fact-style headline", PROTECTED, check_runs=[run])
    # "Refuses" here means HOLDS the merge: a refusal, or (for a check still
    # running, #697) a wait for the re-evaluation. Either way, no merge now.
    decision = _outputs(tmp_path / "output.txt").get("decision")
    holds = proc.returncode != 0 or decision != "merge"
    assert holds is gate_refuses, (run, proc.stderr, decision)
    assert worker_merge.other_check_blocks(run) is merge_refuses, run
    if gate_refuses:
        assert merge_refuses, f"the merge step is looser than gate 5 on {run}"
    if gate_refuses != merge_refuses:
        assert why


def test_a_required_check_must_be_success_or_skipped_where_gate_5_tolerates_neutral(run_gate):
    """Deliberate (§5.2 3, the owner's rule): `neutral` on a REQUIRED check
    holds the merge step; the gate lets it through."""
    run = {"name": "ci-gate", "status": "completed", "conclusion": "neutral"}
    proc, _calls, _comments = run_gate("A fact-style headline", PROTECTED, check_runs=[run])
    assert proc.returncode == 0
    assert worker_merge.required_check_state([run]) == "failed"
    assert worker_merge.required_check_state([{**run, "conclusion": "success"}]) == "green"
    assert worker_merge.required_check_state([]) == "pending"


@pytest.mark.parametrize(
    ("rules", "branch", "gate_refuses"),
    [([], UNPROTECTED, True), (RULESET_RULES, RULESET_ONLY_BRANCH, False)],
    ids=["no-required-checks", "main-protection"],
)
def test_the_merge_steps_required_set_matches_gate_3(run_gate, rules, branch, gate_refuses):
    """Gate 3 and the merge read the same endpoint, `rules/branches/{base}`,
    and agree on whether a required set exists. The merge then refuses every
    required check with no `integration_id` (M5, `required_check_unpinned`) --
    which today is all five of main-protection's: deliberate, since a legacy
    status from any token could satisfy an unpinned check."""
    proc, _calls, _comments = run_gate("A fact-style headline", branch, rules=rules)
    assert (proc.returncode != 0) is gate_refuses, proc.stderr
    required = worker_forge.required_status_checks(rules)
    assert (not required) is gate_refuses
    if required:
        assert {c.context for c in required} == set(RULESET_CHECKS)
        assert all(c.app_id is None for c in required)


def test_the_merge_squashes_under_the_same_subject_as_the_gate(job: dict):
    """`--subject "${PR_TITLE} (#${PR_NUMBER})"` there, `commit_title` here."""
    script = _step(job, "merge")["run"]
    assert '--subject "${PR_TITLE} (#${PR_NUMBER})"' in script
    source = (REPO / "apps" / "agent-worker" / "agent_worker" / "merge.py").read_text()
    assert '"commit_title": f"{title} (#{number})"' in source
    assert '"merge_method": "squash"' in source


# ---------------------------------------------------------------------------
# The close-issues job: a merge closes the issues its keywords name (#621)
# ---------------------------------------------------------------------------
# GitHub parsed the keywords of the App's merges and closed 5 of 33 of their
# issues (history analysis, 2026-10-05). Owner decision: close them here,
# after the merge, with no change to the App's permissions. The script's own
# decisions are run in test_close_merged_issues.py.


def test_the_close_job_runs_only_on_a_merge_into_the_default_branch(close_job: dict):
    """MUTATION: drop `merged == true` (a pull request closed unmerged would
    close its issues), or the base-branch comparison."""
    condition = str(close_job.get("if") or "")
    assert re.search(r"github\.event\.action\s*==\s*'closed'", condition), condition
    assert re.search(r"github\.event\.pull_request\.merged\s*==\s*true", condition), condition
    assert re.search(
        r"github\.event\.pull_request\.base\.ref\s*==\s*github\.event\.repository\.default_branch",
        condition,
    ), condition


def test_the_enable_job_never_runs_on_the_close(job: dict):
    """`enable` keys on the label just added; a `closed` event carries none."""
    condition = str(job.get("if") or "")
    assert "github.event.label.name == 'ready'" in condition, condition


def test_issues_write_is_granted_to_the_close_job_and_nowhere_else(workflow: dict, close_job: dict):
    """Least privilege: closing an issue needs issues: write; reading the
    closing references needs pull-requests: read; the checkout of the default
    branch's scripts needs contents: read. Nothing else, and no App token.
    MUTATION: grant issues: write at the workflow level or to another job, or
    widen pull-requests to write."""
    assert close_job.get("permissions") == {
        "contents": "read",
        "pull-requests": "read",
        "issues": "write",
    }, close_job.get("permissions")
    for job_id, other in (workflow.get("jobs") or {}).items():
        if job_id != "close-issues":
            assert "issues" not in (other.get("permissions") or {}), (job_id, other.get("permissions"))
    text = json.dumps(close_job)
    assert "app-token" not in text and "MERGE_APP" not in text, "the close job needs no App token"


def test_the_close_job_checks_out_the_default_branch_never_the_pull_request(close_job: dict):
    """pull_request_target with a write token: the code it runs is main's,
    which the merge has just made -- never the pull request's head or fork.
    MUTATION: check out `github.event.pull_request.head.sha`."""
    steps = close_job.get("steps") or []
    checkouts = [step for step in steps if "checkout" in str(step.get("uses") or "")]
    assert len(checkouts) == 1, checkouts
    inputs = checkouts[0].get("with") or {}
    assert inputs.get("ref") == "${{ github.event.repository.default_branch }}", inputs
    assert inputs.get("persist-credentials") is False, inputs
    assert "head" not in json.dumps(close_job), "the close job reads the pull request's head"


def test_the_close_job_runs_the_script_with_the_number_through_env(close_job: dict):
    steps = [step for step in close_job.get("steps") or [] if step.get("run")]
    assert len(steps) == 1, steps
    step = steps[0]
    assert "github.event" not in step["run"], step["run"]
    assert "scripts/close-merged-issues.sh" in step["run"], step["run"]
    assert '--pr "${PR_NUMBER}"' in step["run"], step["run"]
    env = {**(close_job.get("env") or {}), **(step.get("env") or {})}
    assert env.get("PR_NUMBER") == "${{ github.event.pull_request.number }}", env
    assert env.get("GH_TOKEN") == "${{ github.token }}", env


def test_docs_describe_closing_the_merged_pull_requests_issues():
    """MUTATION: drop the docs/ci.md section."""
    text = CI_DOC.read_text()
    assert "close-merged-issues.sh" in text
    assert "closingIssuesReferences" in text
    assert "part of #N" in text
    assert "#621" in text


# ---------------------------------------------------------------------------
# A `ready` label that lands while checks still run is re-evaluated (#697)
# ---------------------------------------------------------------------------
# Measured 2026-10-06 (observer P13): #697 got `ready` while its CI ran, the
# gate refused it ("failing or still running"), and nothing looked again, so
# it sat unmerged after going green. Every SwarmCloud pull request opened
# with pr_label ready hits that. Owner decision, 2026-10-06: a pull request
# labelled early is merged once its checks are green at head. So the gate
# WAITS on a running check, and `requeue` -- on every `workflow_run:
# completed` of a workflow that runs on pull requests -- dispatches this
# workflow for each open `ready` pull request at that run's head, which runs
# the same gate and, when it passes, the same App-token merge.


def _pull_request_workflow_names() -> set[str]:
    """The `name:` of every workflow that runs on `pull_request`."""
    names = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        data = _workflow(path)
        on = data.get("on") or {}
        events = set(on) if isinstance(on, (dict, list)) else {on}
        if "pull_request" in events:
            names.add(data["name"])
    return names


def test_it_re_evaluates_when_any_pull_request_workflow_completes(workflow: dict):
    """A check left out of the list is one whose completion never re-evaluates:
    if it is the last to finish, the pull request sits, which is #697 again.
    MUTATION: drop `ci-gate` (or any name) from `workflows:`, or listen to
    `requested` instead of `completed`."""
    names = _pull_request_workflow_names()
    assert {"application", "terraform", "security", "ci-gate"} <= names, names  # the control
    workflow_run = workflow["on"]["workflow_run"]
    assert workflow_run["types"] == ["completed"], workflow_run
    assert set(workflow_run["workflows"]) == names, workflow_run


def test_check_suite_is_not_the_completion_event(workflow: dict):
    """GitHub does not run a workflow on `check_suite` for a suite GitHub
    Actions created -- which is every suite here -- so it would never fire.
    MUTATION: listen to `check_suite: completed` instead of `workflow_run`."""
    assert "check_suite" not in workflow["on"], workflow["on"]


def test_the_dispatch_takes_one_required_pull_request_number(workflow: dict):
    inputs = (workflow["on"]["workflow_dispatch"] or {}).get("inputs") or {}
    assert set(inputs) == {"pr"}, inputs
    assert inputs["pr"].get("required") is True, inputs


def test_the_requeue_job_runs_only_on_a_pull_request_run_completing(requeue_job: dict):
    """MUTATION: drop the `workflow_run.event == 'pull_request'` guard (every
    push to main would list ready pull requests for nothing), or the
    MERGE_APP_ID guard (with no App every completion would re-refuse)."""
    condition = str(requeue_job.get("if") or "")
    assert re.search(r"github\.event_name\s*==\s*'workflow_run'", condition), condition
    assert re.search(r"github\.event\.workflow_run\.event\s*==\s*'pull_request'", condition), condition
    assert "vars.MERGE_APP_ID != ''" in condition, condition


def test_the_requeue_job_holds_no_app_token_and_least_privilege(requeue_job: dict):
    """It merges nothing: it dispatches (actions: write, which the
    GITHUB_TOKEN may do -- a dispatch is one of the two events that token
    still starts) and lists pull requests (pull-requests: read).
    MUTATION: mint the App token here, check out code, or widen permissions."""
    assert requeue_job.get("permissions") == {"actions": "write", "pull-requests": "read"}, requeue_job.get(
        "permissions"
    )
    text = json.dumps(requeue_job)
    assert "app-token" not in text and "MERGE_APP_PRIVATE_KEY" not in text, text
    steps = requeue_job.get("steps") or []
    assert steps, "the requeue job has no steps, so this checked nothing"
    for step in steps:
        assert "checkout" not in str(step.get("uses") or "")
        assert "github.event" not in str(step.get("run") or ""), step


def test_the_enable_and_disable_jobs_never_run_on_a_workflow_run(workflow: dict, job: dict, disable_job: dict):
    """A `workflow_run` event carries no pull request. MUTATION: drop the
    disable job's event_name guard."""
    disable = str(disable_job.get("if") or "")
    assert re.search(r"github\.event_name\s*==\s*'pull_request_target'", disable), disable


def test_a_re_evaluation_queues_separately_from_the_label_events(workflow: dict):
    """A queued run in a concurrency group cancels the one queued before it.
    Sharing the label events' group, a burst of re-evaluations could cancel a
    queued `synchronize` run, and a stale auto-merge would stay armed.
    MUTATION: drop the `requeue-` prefix."""
    group = workflow["concurrency"]["group"]
    assert "github.event_name == 'workflow_dispatch' && 'requeue-'" in group, group
    assert "github.event.pull_request.number || inputs.pr" in group, group


FAKE_GH_REQUEUE = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
case "$1 $2" in
  "pr list") cat "${FAKE_GH_PRS}" ;;
  "workflow run") ;;
  *) echo "fake gh: unexpected call: $*" >&2; exit 3 ;;
esac
"""


@pytest.fixture
def run_requeue(requeue_job: dict, tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    steps = requeue_job.get("steps") or []
    assert len(steps) == 1, f"expected one step in the requeue job, found {len(steps)}"
    script = steps[0]["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH_REQUEUE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(prs: list[dict], run_head_sha: str):
        prs_file = tmp_path / "prs.json"
        prs_file.write_text(json.dumps(prs))
        log = tmp_path / "gh.log"
        summary = tmp_path / "summary.md"
        for path in (log, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
            "RUN_HEAD_SHA": run_head_sha,
            "DEFAULT_BRANCH": "main",
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_PRS": str(prs_file),
        }
        proc = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False
        )
        return proc, log.read_text()

    return run


def test_requeue_dispatches_each_ready_pull_request_at_the_runs_head(run_requeue):
    """MUTATION: dispatch every ready pull request (not only this head's), or
    stop after the first."""
    head = "ab" * 20
    proc, calls = run_requeue(
        [
            {"number": 11, "headRefOid": head},
            {"number": 12, "headRefOid": "cd" * 20},
            {"number": 13, "headRefOid": head},
        ],
        head,
    )
    assert proc.returncode == 0, proc.stderr
    dispatches = [line for line in calls.splitlines() if line.startswith("workflow run")]
    assert dispatches == [
        "workflow run auto-merge.yml --ref main -f pr=11",
        "workflow run auto-merge.yml --ref main -f pr=13",
    ], calls
    assert "pr list --state open --label ready" in calls, calls
    assert "dispatched 2" in proc.stdout, proc.stdout


def test_requeue_dispatches_nothing_when_no_ready_pull_request_is_at_the_head(run_requeue):
    """A run for a superseded head (or a pull request without `ready`) re-evaluates nothing."""
    proc, calls = run_requeue([{"number": 12, "headRefOid": "cd" * 20}], "ab" * 20)
    assert proc.returncode == 0, proc.stderr
    assert "workflow run" not in calls, calls
    assert "dispatched 0" in proc.stdout, proc.stdout


FAKE_GH_VIEW = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "$1 $2" == "pr view" ]]; then
  cat "${FAKE_GH_VIEW}"
  exit 0
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


@pytest.fixture
def run_resolve(job: dict, tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    script = _step(job, "pr")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH_VIEW)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(event_name: str, *, view: dict | None = None, number: str = "4242", event: dict | None = None):
        view_file = tmp_path / "view.json"
        view_file.write_text(json.dumps(view or {}))
        log = tmp_path / "gh.log"
        summary = tmp_path / "summary.md"
        output = tmp_path / "output.txt"
        for path in (log, summary, output):
            path.write_text("")
        event = event or {}
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
            "EVENT_NAME": event_name,
            "PR_NUMBER": number,
            "EVENT_TITLE": event.get("title", ""),
            "EVENT_BASE_REF": event.get("base", ""),
            "EVENT_HEAD_SHA": event.get("head", ""),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_VIEW": str(view_file),
        }
        proc = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False
        )
        return proc, log.read_text(), _outputs(output)

    return run


HEAD = "0123456789abcdef0123456789abcdef01234567"


def test_on_a_label_the_pull_request_is_the_events_and_no_api_is_read(run_resolve):
    proc, calls, outputs = run_resolve(
        "pull_request_target", event={"title": "A fact-style headline", "base": "main", "head": HEAD}
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == "", calls
    assert outputs == {"evaluate": "true", "title": "A fact-style headline", "base_ref": "main", "head_sha": HEAD}


def test_on_a_re_evaluation_the_pull_request_is_read_back_from_the_api(run_resolve):
    """MUTATION: read the title or head from anywhere but the pull request itself."""
    view = {"state": "OPEN", "title": "A fact-style headline", "baseRefName": "main", "headRefOid": HEAD,
            "labels": [{"name": "enhancement"}, {"name": "ready"}]}
    proc, calls, outputs = run_resolve("workflow_dispatch", view=view)
    assert proc.returncode == 0, proc.stderr
    assert "pr view 4242 --json" in calls, calls
    assert outputs == {"evaluate": "true", "title": "A fact-style headline", "base_ref": "main", "head_sha": HEAD}


@pytest.mark.parametrize(
    "view",
    [
        {"state": "OPEN", "title": "t", "baseRefName": "main", "headRefOid": HEAD, "labels": []},
        {"state": "MERGED", "title": "t", "baseRefName": "main", "headRefOid": HEAD, "labels": [{"name": "ready"}]},
        {"state": "CLOSED", "title": "t", "baseRefName": "main", "headRefOid": HEAD, "labels": [{"name": "ready"}]},
    ],
    ids=["ready-removed", "merged", "closed"],
)
def test_a_re_evaluation_of_a_pull_request_no_longer_open_and_ready_does_nothing(run_resolve, view):
    """The dispatch is a request to look again, not a second `ready`: a pull
    request whose label was dropped (a push, a refusal) or that merged is
    left alone. MUTATION: drop the label or state check."""
    proc, _calls, outputs = run_resolve("workflow_dispatch", view=view)
    assert proc.returncode == 0, proc.stderr
    assert outputs == {"evaluate": "false"}, outputs


@pytest.mark.parametrize("number", ["", "0", "12; touch x", "-1"])
def test_a_dispatch_input_that_is_not_a_pull_request_number_fails(run_resolve, number):
    proc, calls, _outputs = run_resolve("workflow_dispatch", number=number)
    assert proc.returncode != 0
    assert calls == "", calls


def test_a_title_cannot_forge_another_output(run_resolve):
    """The title is the pull request author's text and is written to
    $GITHUB_OUTPUT. MUTATION: write it as `title=${title}`, and this one
    sets head_sha."""
    title = f"A headline\nhead_sha={'f' * 40}\nevaluate=true"
    view = {"state": "OPEN", "title": title, "baseRefName": "main", "headRefOid": HEAD, "labels": [{"name": "ready"}]}
    proc, _calls, outputs = run_resolve("workflow_dispatch", view=view)
    assert proc.returncode == 0, proc.stderr
    assert outputs["head_sha"] == HEAD, outputs
    assert outputs["title"] == title, outputs


def test_a_re_evaluation_that_finds_a_failing_check_drops_ready_and_says_so(run_gate, tmp_path: Path):
    """The refusal still happens, once: it turns auto-merge off and removes
    `ready`, so the next completion does not comment again, and it does not
    fail a run that sits on main's head commit.
    MUTATION: keep the label (one comment per finishing run), or exit 1."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        event_name="workflow_dispatch",
        check_runs=[{"name": "format / unit tests", "status": "completed", "conclusion": "failure"}],
    )
    assert proc.returncode == 0, proc.stderr
    assert _outputs(tmp_path / "output.txt").get("decision") == "refused"
    assert "pr merge 4242 --disable-auto" in calls, calls
    assert "pr edit 4242 --remove-label ready" in calls, calls
    assert "format / unit tests" in comments and "ready" in comments, comments


def test_a_re_evaluation_still_refuses_a_swarm_task_title(run_gate, tmp_path: Path):
    """Refusals are unchanged by the way in: a re-evaluation runs the same gate."""
    proc, calls, comments = run_gate("[swarm] task_01J9ZK3Q8R", PROTECTED, event_name="workflow_dispatch")
    assert proc.returncode == 0, proc.stderr
    assert _outputs(tmp_path / "output.txt").get("decision") == "refused"
    assert REFUSED_PREFIX in comments, comments
    assert "pr edit 4242 --remove-label ready" in calls, calls


def test_a_re_evaluation_without_the_merge_app_keeps_ready_for_the_merge_watcher(run_gate, tmp_path: Path):
    """Without the App, `ready` is what the operator's merge watcher reads;
    the label run already said why, and the pull request cannot fix it.
    MUTATION: drop the label on this refusal too."""
    proc, calls, comments = run_gate("A fact-style headline", PROTECTED, app_id="", event_name="workflow_dispatch")
    assert proc.returncode == 0, proc.stderr
    assert _outputs(tmp_path / "output.txt").get("decision") == "refused"
    assert "pr edit" not in calls and "pr comment" not in calls and comments == "", calls


def test_a_re_evaluation_still_waiting_is_silent(run_gate, tmp_path: Path):
    """The label run already said it waits; each finishing run must not say it again."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        event_name="workflow_dispatch",
        check_runs=[{"name": "ci-gate", "status": "in_progress", "conclusion": None}],
    )
    assert proc.returncode == 0, proc.stderr
    assert _outputs(tmp_path / "output.txt").get("decision") == "wait"
    assert comments == "" and "pr comment" not in calls, calls


def test_a_re_evaluation_with_every_check_green_says_merge(run_gate, tmp_path: Path):
    """The point of the whole path: #697, once green, is merged."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        RULESET_ONLY_BRANCH,
        rules=RULESET_RULES,
        event_name="workflow_dispatch",
        check_runs=[{"name": c, "status": "completed", "conclusion": "success"} for c in RULESET_CHECKS],
    )
    assert proc.returncode == 0, proc.stderr + comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "merge"
    assert "pr edit" not in calls and comments == "", calls


def test_a_label_time_refusal_is_unchanged_and_keeps_the_label(run_gate, tmp_path: Path):
    """The label path refuses exactly as before: comment, red, label left on."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": "shellcheck", "status": "completed", "conclusion": "failure"}],
    )
    assert proc.returncode != 0
    assert "Not queued for auto-merge" in comments, comments
    assert "pr edit" not in calls and "--disable-auto" not in calls, calls


FAKE_GH_MERGE = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "$1 $2" == "pr merge" ]]; then
  for arg in "$@"; do
    if [[ "${arg}" == "--auto" ]]; then
      echo "fake gh: ${FAKE_GH_AUTO_ERROR}" >&2
      exit 1
    fi
  done
  exit 0
fi
if [[ "$1 $2" == "pr view" ]]; then
  cat "${FAKE_GH_VIEW}"
  exit 0
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


@pytest.fixture
def run_merge(job: dict, tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    script = _step(job, "merge")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH_MERGE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(view: dict):
        view_file = tmp_path / "view.json"
        view_file.write_text(json.dumps(view))
        log = tmp_path / "gh.log"
        summary = tmp_path / "summary.md"
        for path in (log, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
            "PR_NUMBER": "4242",
            "PR_TITLE": "A fact-style headline",
            "HEAD_SHA": HEAD,
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_VIEW": str(view_file),
            "FAKE_GH_AUTO_ERROR": "auto-merge could not be enabled",
        }
        proc = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False
        )
        merges = [line for line in log.read_text().splitlines() if line.startswith("pr merge")]
        return proc, merges, summary.read_text()

    return run


def test_a_re_evaluation_of_an_already_armed_auto_merge_succeeds_without_merging(run_merge):
    """A label that landed before any check existed armed native auto-merge
    at once; its re-evaluation then finds it armed. That is queued, not an
    error. MUTATION: drop the autoMergeRequest branch (a red run), or merge
    directly there (bypassing what the armed auto-merge waits for)."""
    proc, merges, summary = run_merge({"state": "OPEN", "mergeStateStatus": "BLOCKED",
                                       "autoMergeRequest": {"mergeMethod": "SQUASH"}})
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 1 and "--auto" in merges[0], merges
    assert "Queued for auto-merge" in summary, summary


def test_a_pull_request_merged_meanwhile_succeeds_without_merging(run_merge):
    proc, merges, _summary = run_merge({"state": "MERGED", "mergeStateStatus": "UNKNOWN", "autoMergeRequest": None})
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 1, merges


def test_an_auto_merge_error_on_a_blocked_unarmed_pull_request_still_fails(run_merge):
    """The control: the new branches do not swallow a real failure."""
    proc, merges, _summary = run_merge({"state": "OPEN", "mergeStateStatus": "BLOCKED", "autoMergeRequest": None})
    assert proc.returncode != 0
    assert len(merges) == 1, merges


def test_an_already_clean_pull_request_is_merged_directly(run_merge):
    proc, merges, _summary = run_merge({"state": "OPEN", "mergeStateStatus": "CLEAN", "autoMergeRequest": None})
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 2 and "--auto" not in merges[1], merges
    assert f"--match-head-commit {HEAD}" in merges[1], merges


def test_docs_describe_the_re_evaluation_of_an_early_ready_label():
    """MUTATION: drop the docs/ci.md paragraph."""
    text = CI_DOC.read_text()
    assert "#697" in text
    assert "workflow_run" in text and "workflow_dispatch" in text
    assert "re-evaluat" in text
