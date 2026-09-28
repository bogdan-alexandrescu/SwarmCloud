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
# The three workflows the owner named as required, 2026-09-28.
REQUIRED_WORKFLOWS = ("application.yml", "security.yml", "terraform.yml")


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
    # The head SHA is read (HEAD_SHA) to PIN both merge calls to the reviewed
    # commit -- fencing against a push after the label, not a way to find or
    # run the fork's code. Reading it is safe only because there is no
    # checkout step (asserted above) and nothing here reads where the fork's
    # code actually lives.
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
    """Read the base branch's protection; comment on a refusal. Nothing else.
    MUTATION: set `contents: write`, add `id-token: write`, or `permissions: write-all`."""
    assert workflow.get("permissions") == {}, workflow.get("permissions")
    assert job.get("permissions") == {"contents": "read", "pull-requests": "write"}, job.get("permissions")


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
    assert inputs.get("permission-contents") == "write", inputs
    assert inputs.get("permission-pull-requests") == "write", inputs
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

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "${1:-}" == "api" ]]; then
  cat "${FAKE_GH_BRANCH}"
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
    ):
        branch_file = tmp_path / "branch.json"
        branch_file.write_text(json.dumps(branch))
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
            "MERGE_APP_ID": app_id,
            "HAS_MERGE_APP_KEY": has_key,
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_BRANCH": str(branch_file),
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


def test_docs_carry_the_branch_protection_command_for_the_three_required_workflows():
    """The owner applies it; the doc must name checks that exist, from all three
    workflows, pinned to GitHub Actions so another App cannot satisfy them.
    MUTATION: rename a job in security.yml without updating the command."""
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
    for name in REQUIRED_WORKFLOWS:
        reported = _job_check_names(WORKFLOWS / name)
        assert reported, name
        assert contexts & reported, f"no check from {name} is required: {sorted(reported)}"
    every_name = set().union(*(_job_check_names(WORKFLOWS / n) for n in REQUIRED_WORKFLOWS))
    unknown = contexts - every_name
    assert not unknown, f"required checks no job reports on a pull request: {sorted(unknown)}"
    for key in ("enforce_admins", "required_pull_request_reviews", "restrictions"):
        assert key in payload, f"the protection PUT rejects a body without {key!r}"


def test_docs_say_auto_merge_must_be_allowed_on_the_repository():
    text = CI_DOC.read_text()
    assert "allow_auto_merge=true" in text
    assert "auto-merge.yml" in text
