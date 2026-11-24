"""The `ready` label queues a pull request for GitHub's native auto-merge (archived issue 262).

`.github/workflows/auto-merge.yml` runs on `pull_request_target` [labeled]:
its `enable` job resolves the pull request (`pr`), gates it (`gate`) and
enables the merge with the merge App's token (`merge`); `disable` disarms a
stale auto-merge on a new head; `requeue` and `requeue-edited` dispatch a
re-evaluation of a `ready` pull request.

Those shells are RUN here, against a fake `gh` on PATH that records every
call, so the refusals are exercised rather than read. The shell is loaded
from scripts/generated/ci-run-blocks.json (scripts/gen_ci_run_blocks.py)
through `ci_run_blocks.run_block`, never from .github/ (issue 71). Nothing
here touches the network.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .ci_run_blocks import run_block

REPO = Path(__file__).resolve().parents[3]

#: GitHub Actions' own app id, the same on every repository (swarm_api.forgewrite.GITHUB_ACTIONS_APP_ID).
ACTIONS_APP = 15368

REFUSED_PREFIX = "[swarm] task_"

#: The enable job's check-run name, which the gate excludes from the checks it
#: reads (its own run is always still in progress while the gate runs).
ENABLE_JOB_NAME = "queue for auto-merge"

#: The step keys of the requeue jobs' one run step each (their `name:`).
REQUEUE_STEP = "dispatch a re-evaluation for each open ready pull request at the run's head"
REQUEUE_EDITED_STEP = "dispatch a re-evaluation of this pull request"


# ---------------------------------------------------------------------------
# The gate, run
# ---------------------------------------------------------------------------

PROTECTED = {"name": "main", "protected": True, "protection": {"enabled": True, "required_status_checks": {
    "enforcement_level": "non_admins",
    "contexts": ["shellcheck", "trivy (repo)", "fmt / validate / tflint"],
    "checks": [{"context": "shellcheck", "app_id": ACTIONS_APP}],
}}}
UNPROTECTED = {"name": "main", "protected": False, "protection": {"enabled": False, "required_status_checks": {
    "enforcement_level": "off", "contexts": [], "checks": [],
}}}
# What `GET repos/{repo}/branches/main` returned for this repository on
# 2026-10-01: `protected` is true because a RULESET protects main, while the
# classic `.protection` block is off and lists no check. Read alone, it is
# "main has no required status checks" -- the refusal every `ready` label got
# (run 36822836560, archived pull request 412; archived pull request 403).
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
_RULE_SOURCE = {"ruleset_source_type": "Repository", "ruleset_source": "example-org/example-repo",
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
    */actions/runs) cat "${FAKE_GH_WORKFLOW_RUNS}" ;;
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
# A re-evaluation's refusal drops `ready`; turning auto-merge off is the
# `disarm` step's, with the App token (#795). Any `pr merge` from the gate is
# a call it must not make.
if [[ "$*" == "pr edit ${PR_NUMBER} --remove-label ready" ]]; then
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
def run_gate(tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    script = run_block("auto-merge.yml", "enable", "gate")
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
        dependency_refusal: str = "",
        workflow_runs: list[dict] | None = None,
    ):
        branch_file = tmp_path / "branch.json"
        branch_file.write_text(json.dumps(branch))
        rules_file = tmp_path / "rules.json"
        # Default: no ruleset applies, so only classic protection can count.
        rules_file.write_text(json.dumps(rules if rules is not None else []))
        check_runs_file = tmp_path / "check-runs.json"
        # Default: nothing else has reported on this head, so item 5 passes.
        check_runs_file.write_text(json.dumps({"check_runs": check_runs if check_runs is not None else []}))
        # The Actions runs at the head: which event made each check suite.
        workflow_runs_file = tmp_path / "workflow-runs.json"
        workflow_runs_file.write_text(json.dumps({"workflow_runs": workflow_runs or []}))
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
            "GH_REPO": "example-org/example-repo",
            "EVENT_NAME": event_name,
            "PR_NUMBER": "4242",
            "PR_TITLE": title,
            "BASE_REF": base_ref,
            "HEAD_SHA": head_sha,
            "MERGE_APP_ID": app_id,
            "HAS_MERGE_APP_KEY": has_key,
            "DEPENDENCY_REFUSAL": dependency_refusal,
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_BRANCH": str(branch_file),
            "FAKE_GH_CHECK_RUNS": str(check_runs_file),
            "FAKE_GH_WORKFLOW_RUNS": str(workflow_runs_file),
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
    assert "api repos/example-org/example-repo/branches/main" in calls, calls
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
        "checks": [{"context": "trivy (repo)", "app_id": ACTIONS_APP}, {"context": "shellcheck", "app_id": ACTIONS_APP}],
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
    """archived pull request 697: `ready` landed while CI ran, the gate refused, and nothing ever
    looked again. Now it waits: no refusal, no merge, no armed auto-merge,
    and one comment saying what it waits for and when it looks again. The
    running check is a REQUIRED one (`shellcheck` in PROTECTED): only those
    hold the merge since archived issue 815, below.
    MUTATION: refuse on an incomplete `status` again, or let it fall
    through to `decision=merge`."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": "shellcheck", "status": status, "conclusion": None}],
    )
    assert proc.returncode == 0, proc.stderr + comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "wait"
    assert "Not queued" not in comments, comments
    assert "Queued for auto-merge" in comments and "shellcheck" in comments, comments
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


# archived issue 815: archived pull request 811 got `ready` while the code-scanning check `Trivy` -- not one
# ruleset 24160219 requires, and not reported by an application or security
# run -- was still running. The gate waited on it, nothing re-evaluated when it
# finished, and the merge queue stalled for five hours. Only a required check
# still running holds the merge now; native auto-merge waits for exactly those.


def _ruleset_checks_green() -> list[dict]:
    return [{"name": c, "status": "completed", "conclusion": "success"} for c in RULESET_CHECKS]


@pytest.mark.parametrize("event_name", ["pull_request_target", "workflow_dispatch"])
def test_a_pull_request_whose_only_unfinished_check_is_not_required_is_enabled(run_gate, tmp_path: Path,
                                                                               event_name: str):
    """archived issue 815. MUTATION: wait on every unfinished check again (the archived pull request 811 stall),
    or match the required names loosely enough that `Trivy` is one."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        RULESET_ONLY_BRANCH,
        rules=RULESET_RULES,
        event_name=event_name,
        check_runs=_ruleset_checks_green() + [{"name": "Trivy", "status": "in_progress", "conclusion": None}],
    )
    assert proc.returncode == 0, proc.stderr + comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "merge"
    assert comments == "" and "pr comment" not in calls, comments


def test_a_required_check_from_classic_protection_still_holds_the_merge(run_gate, tmp_path: Path):
    """The required set is gate 3's union: classic protection's names count too.
    MUTATION: read only the ruleset's names for the wait."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        rules=RULESET_RULES,
        check_runs=[{"name": "fmt / validate / tflint", "status": "queued", "conclusion": None}],
    )
    assert proc.returncode == 0, proc.stderr + comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "wait"


def test_the_wait_names_only_the_required_checks_it_waits_for(run_gate, tmp_path: Path):
    """A required check still running holds it; the comment names that check,
    not the non-required one running beside it, and says that a check suite
    finishing re-evaluates it. MUTATION: list every unfinished check."""
    runs = [r for r in _ruleset_checks_green() if r["name"] != "ci-gate"]
    proc, calls, comments = run_gate(
        "A fact-style headline",
        RULESET_ONLY_BRANCH,
        rules=RULESET_RULES,
        check_runs=runs + [
            {"name": "ci-gate", "status": "in_progress", "conclusion": None},
            {"name": "Trivy", "status": "in_progress", "conclusion": None},
        ],
    )
    assert proc.returncode == 0, proc.stderr + comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "wait"
    assert "ci-gate" in comments and "Trivy" not in comments, comments
    assert "check suite" in comments, comments
    assert "pr merge" not in calls, calls


def test_a_failing_check_that_is_not_required_still_refuses(run_gate, tmp_path: Path):
    """archived issue 815 narrows the WAIT, not gate 5: a check that ran and failed still
    holds the merge, required or not. MUTATION: filter item 5 by the
    required names too."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        RULESET_ONLY_BRANCH,
        rules=RULESET_RULES,
        check_runs=_ruleset_checks_green() + [{"name": "Trivy", "status": "completed", "conclusion": "failure"}],
    )
    assert proc.returncode != 0
    assert "Trivy" in comments, comments


def test_a_green_gate_says_merge(run_gate, tmp_path: Path):
    """The control for the two above: a gate that never says `merge` would pass them."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": "shellcheck", "status": "completed", "conclusion": "success"}],
    )
    assert proc.returncode == 0, comments
    assert _outputs(tmp_path / "output.txt").get("decision") == "merge"


MERGE_QUEUE_RULE = {"type": "merge_queue", **_RULE_SOURCE, "parameters": {
    "merge_method": "MERGE", "grouping_strategy": "ALLGREEN", "max_entries_to_build": 5,
    "min_entries_to_merge": 1, "min_entries_to_merge_wait_minutes": 0,
    "check_response_timeout_minutes": 60,
}}


@pytest.mark.parametrize(("rules", "queue"), [
    (RULESET_RULES, "false"),
    (RULESET_RULES + [MERGE_QUEUE_RULE], "true"),
], ids=["no-queue", "merge-queue"])
def test_a_green_gate_says_whether_the_base_requires_a_merge_queue(run_gate, tmp_path: Path,
                                                                   rules, queue):
    """Read from the same effective rules as gate 3 (main-protection, 24160219,
    gains a `merge_queue` rule when the operator applies docs/ci.md's change).
    MUTATION: hard-code `queue=false`, and a queued base gets `--squash`."""
    proc, _calls, comments = run_gate("A fact-style headline", RULESET_ONLY_BRANCH, rules=rules)
    assert proc.returncode == 0, (proc.stderr, comments)
    outputs = _outputs(tmp_path / "output.txt")
    assert outputs.get("decision") == "merge", outputs
    assert outputs.get("queue") == queue, outputs


@pytest.mark.parametrize("conclusion", ["timed_out", "action_required"])
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


def test_the_gate_excludes_its_own_still_running_check_run(run_gate):
    """MUTATION: drop the self-exclusion -- this job's own check run is always
    still `in_progress` while the gate script that reports it is running, so
    without it the gate would refuse every single run."""
    proc, _calls, comments = run_gate(
        "A fact-style headline",
        PROTECTED,
        check_runs=[{"name": ENABLE_JOB_NAME, "status": "in_progress", "conclusion": None}],
    )
    assert proc.returncode == 0, comments
    assert comments == "", comments


def test_the_check_runs_are_read_for_the_exact_head_commit(run_gate):
    proc, calls, _comments = run_gate("A fact-style headline", PROTECTED, head_sha="cafef00d")
    assert proc.returncode == 0, _comments
    assert "api repos/example-org/example-repo/commits/cafef00d/check-runs" in calls, calls


# ---------------------------------------------------------------------------
# The disable job: a push after `ready` must not leave a stale auto-merge armed
# ---------------------------------------------------------------------------


FAKE_GH_DISABLE = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s [as %s]\n' "$*" "${GH_TOKEN:-}" >> "${FAKE_GH_LOG}"
if [[ "${1:-} ${2:-}" == "pr view" ]]; then
  if [[ -f "${FAKE_GH_ARMED}" ]]; then
    echo '{"autoMergeRequest":{"mergeMethod":"SQUASH"}}'
  else
    echo '{"autoMergeRequest":null}'
  fi
  exit 0
fi
if [[ "${1:-} ${2:-}" == "pr merge" ]]; then
  rm -f "${FAKE_GH_ARMED}"
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


# Which identity made each call, in plain words: the fake logs them.
DISABLE_WORKFLOW_IDENTITY = "workflow"
DISABLE_APP_IDENTITY = "app"


@pytest.fixture
def run_disable(tmp_path: Path):
    """The disable job's `disarm` then `strip` steps, as they run on a push
    that is not a pure base merge (archived issue 795; the classifier is run in
    test_auto_merge_base_merge.py)."""
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH_DISABLE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "gh.log"
    comments = tmp_path / "comments.md"
    armed = tmp_path / "armed"
    for path in (log, comments, armed):
        path.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GH_REPO": "example-org/example-repo",
        "PR_NUMBER": "4242",
        "GH_TOKEN": DISABLE_WORKFLOW_IDENTITY,
        "APP_TOKEN": DISABLE_APP_IDENTITY,
        "DISARM_OUTCOME": "success",
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_COMMENTS": str(comments),
        "FAKE_GH_ARMED": str(armed),
    }
    procs = []
    for step_id in ("disarm", "strip"):
        procs.append(subprocess.run(
            ["bash", "-c", run_block("auto-merge.yml", "disable", step_id)],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        ))
    return procs, log.read_text(), comments.read_text()


def test_a_push_after_ready_disables_auto_merge_and_removes_the_label(run_disable):
    """RUN, against a fake `gh`: the disable job's own shells, not just its YAML.
    MUTATION: drop the `--disable-auto` call, or the `--remove-label ready` call."""
    procs, calls, comments = run_disable
    for proc in procs:
        assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "pr merge 4242 --disable-auto [as app]" in calls, calls
    assert "pr edit 4242 --remove-label ready" in calls, calls
    assert "pr comment 4242" in calls, calls
    assert "ready" in comments.lower(), comments


# ---------------------------------------------------------------------------
# Parity: the merge step restates gates 1, 3 and 5 (archived issue 295, merge-step.md §8)
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

class _Imported:
    """`<module>.<name>` imports the module on first use, not at collection:
    agent_worker is only needed by the parity tests below, and importing it at
    module top made every collection of tests/unit/scripts pay for it
    (test_collection_stays_cheap.py holds this)."""

    def __init__(self, module: str) -> None:
        self._module = module

    def __getattr__(self, name: str) -> object:
        return getattr(importlib.import_module(self._module), name)


worker_forge: Any = _Imported("agent_worker.forge")
worker_merge: Any = _Imported("agent_worker.merge")

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
    # archived issue 1778: a cancelled latest run is pending, not failed. Gate 5 no longer
    # refuses it; gate 6 waits only on a REQUIRED one (`CANCELLED_PARITY`).
    ("completed", "cancelled", False, True,
     "#1778: cancelled is pending; the gate waits only on a required check, the merge step on any"),
    # The check here (`terraform`) is not a required one. Since archived issue 815 the gate
    # waits only on required checks still running -- native auto-merge waits
    # for those -- while the worker's one-shot merge step holds on any.
    ("in_progress", None, False, True,
     "#815: the gate waits only on a required check still running; the merge step holds on any"),
    ("queued", None, False, True,
     "#815: the gate waits only on a required check still running; the merge step holds on any"),
    ("completed", "stale", False, True,
     "gate 5 holds only on three conclusions; the merge tolerates only success, skipped, neutral"),
    ("completed", "startup_failure", False, True,
     "gate 5 holds only on three conclusions; the merge tolerates only success, skipped, neutral"),
]


@pytest.mark.parametrize(("status", "conclusion", "gate_refuses", "merge_refuses", "why"),
                         CHECK_PARITY, ids=[f"{c[0]}-{c[1]}" for c in CHECK_PARITY])
def test_the_merge_steps_other_check_rule_matches_gate_5(
    run_gate, tmp_path, status, conclusion, gate_refuses, merge_refuses, why
):
    run = {"name": "terraform", "status": status, "conclusion": conclusion}
    proc, _calls, _comments = run_gate("A fact-style headline", PROTECTED, check_runs=[run])
    # "Refuses" here means HOLDS the merge: a refusal, or (for a check still
    # running, archived pull request 697) a wait for the re-evaluation. Either way, no merge now.
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


#: A check's runs at one head, by (run id, conclusion, event); the event of
#: each comes from its check suite's Actions run. The owner's decision of
#: 2026-10-22 (CHECK-RULE, part of archived issue 1999 and archived issue 1778): a check is decided by
#: the latest run of EACH event; a re-run replaces the earlier runs of its
#: own event, and a red push run is never outvoted by a green pull_request
#: run (CI-EARLY). (case, runs, gate refuses, merge step's state)
RERUN_PARITY = [
    ("one event, failed then re-run green",
     [(1, "failure", "push"), (2, "success", "push")], False, "green"),
    ("#1778: cancelled then green",
     [(1, "cancelled", "pull_request"), (2, "success", "pull_request")], False, "green"),
    ("one event, green then red",
     [(1, "success", "push"), (2, "failure", "push")], True, "failed"),
    ("push green, pull_request red",
     [(1, "success", "push"), (2, "failure", "pull_request")], True, "failed"),
    ("push red, pull_request green",
     [(1, "failure", "push"), (2, "success", "pull_request")], True, "failed"),
    ("listed newest first",
     [(2, "success", "push"), (1, "failure", "push")], False, "green"),
]


@pytest.mark.parametrize(("case", "runs", "gate_refuses", "merge_state"), RERUN_PARITY,
                         ids=[c[0] for c in RERUN_PARITY])
def test_gate_5_and_the_merge_step_judge_the_latest_run_of_each_event(
    run_gate, case, runs, gate_refuses, merge_state
):
    """MUTATION (auto-merge.yml): keep gate 5 over every run -> the re-run
    cases refuse. MUTATION: take the newest run of a name whatever its event
    -> "push red, pull_request green" passes. MUTATION (merge.py): drop the
    per-event grouping -> the merge step's state differs from the table."""
    suites = {event: 100 + i for i, event in enumerate(sorted({e for _, _, e in runs}))}
    check_runs = [{"id": run_id, "name": "ci-gate", "status": "completed", "conclusion": conclusion,
                   "check_suite": {"id": suites[event] + 10 * run_id}}
                  for run_id, conclusion, event in runs]
    workflow_runs = [{"id": 900 + run_id, "check_suite_id": suites[event] + 10 * run_id, "event": event}
                     for run_id, _conclusion, event in runs]
    proc, _calls, comments = run_gate("A fact-style headline", PROTECTED, check_runs=check_runs,
                                      workflow_runs=workflow_runs)
    assert (proc.returncode != 0) is gate_refuses, (case, proc.stderr, comments)
    facts = [worker_merge.CheckFacts(name="ci-gate", status="completed", conclusion=conclusion,
                                     run_id=run_id, event=event).as_run()
             for run_id, conclusion, event in runs]
    assert worker_merge.required_check_state(facts) == merge_state, case
    # §8: the merge may be stricter than the gate, never looser.
    if gate_refuses:
        assert merge_state == "failed", case


api_forgechecks: Any = _Imported("swarm_api.forgechecks")
MERGE_TRAIN = REPO / "scripts" / "merge-train.sh"

#: CHECK-RULE-2 (the owner, 2026-10-22, answering CHECK-RULE's two questions):
#: "Yes, a follow-up lane" -- the merge train's judge decides a check by the
#: latest run of each event too -- and "Yes, one rule for every check" -- a
#: check nothing requires follows the same rule. One table, through every
#: implementation: the worker's no-required path (`judge_other_checks`) or,
#: for a required check, `required_check_state`; swarm-api's
#: `forgechecks.evaluate` with and without a required set; `merge-train.sh
#: judge`; and gate 5 plus gate 6, run as the gate step's real shell.
#:
#: THE GATE IS READ FROM THE TREE'S auto-merge.yml, the one place the jq lives
#: (gate 5 writes each check's deciding runs once and gate 6 reads them). The
#: rows marked "gate 6" are red until the operator applies this lane's
#: workflow-lines.patch, which is the change they hold.
#:
#: A check's runs are (run id, event or None, status, conclusion); each run is
#: in its own check suite, 100 + its id, and the Actions runs say which event
#: made each suite. An event of None means no Actions run names the suite, so
#: each suite is its own group. (case, required, runs, holds)
EVERY_CHECK_PARITY = [
    ("not required, push failure then success",
     False, [(1, "push", "completed", "failure"), (2, "push", "completed", "success")], False),
    ("not required, push in_progress then success",
     False, [(1, "push", "in_progress", None), (2, "push", "completed", "success")], False),
    ("not required, push success, pull_request failure",
     False, [(1, "push", "completed", "success"), (2, "pull_request", "completed", "failure")], True),
    ("not required, push success then failure",
     False, [(1, "push", "completed", "success"), (2, "push", "completed", "failure")], True),
    ("not required, no event known, two suites",
     False, [(1, None, "completed", "failure"), (2, None, "completed", "success")], True),
    ("required ci-gate, push failure then success",
     True, [(1, "push", "completed", "failure"), (2, "push", "completed", "success")], False),
    ("required ci-gate, no event known, two suites",
     True, [(1, None, "completed", "failure"), (2, None, "completed", "success")], True),
    ("gate 6: required ci-gate, push in_progress then success",
     True, [(1, "push", "in_progress", None), (2, "push", "completed", "success")], False),
    ("gate 6 control: required ci-gate, push success then in_progress",
     True, [(1, "push", "completed", "success"), (2, "push", "in_progress", None)], True),
]


def _parity_runs(name: str, runs: list) -> tuple[list[dict], list[dict]]:
    """GitHub's check runs and the Actions runs at the head, for one row."""
    check_runs = [{"id": run_id, "name": name, "status": status, "conclusion": conclusion,
                   "app": {"id": ACTIONS_APP}, "check_suite": {"id": 100 + run_id},
                   "html_url": f"https://github.com/o/r/actions/runs/{run_id}"}
                  for run_id, _event, status, conclusion in runs]
    workflow_runs = [{"id": 900 + run_id, "check_suite_id": 100 + run_id, "event": event}
                     for run_id, event, _status, _conclusion in runs if event is not None]
    return check_runs, workflow_runs


def _train_judge(tmp_path: Path, check_runs: list[dict], workflow_runs: list[dict],
                 required: list[str]) -> str:
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    cr, st, req = tmp_path / "judge-cr.json", tmp_path / "judge-st.json", tmp_path / "judge-required.txt"
    cr.write_text(json.dumps({"total_count": len(check_runs), "check_runs": check_runs}))
    st.write_text(json.dumps({"state": "pending", "statuses": []}))
    req.write_text("".join(f"{name}\n" for name in required))
    args = ["judge", "--check-runs", str(cr), "--statuses", str(st), "--required", str(req)]
    if workflow_runs:
        # As the callers pass it: the Actions runs at the head, read only
        # when some check ran more than once; no Actions run, no file.
        wf = tmp_path / "judge-wf.json"
        wf.write_text(json.dumps({"total_count": len(workflow_runs), "workflow_runs": workflow_runs}))
        args += ["--workflow-runs", str(wf)]
    proc = subprocess.run(["bash", str(MERGE_TRAIN), *args], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    (verdict,) = [line.split("\t")[1] for line in proc.stdout.splitlines() if line.startswith("verdict\t")]
    return verdict


@pytest.mark.parametrize(("case", "required", "runs", "holds"), EVERY_CHECK_PARITY,
                         ids=[c[0] for c in EVERY_CHECK_PARITY])
def test_every_implementation_judges_every_check_by_the_latest_run_of_each_event(
    run_gate, tmp_path, case, required, runs, holds
):
    name = "ci-gate" if required else "terraform"
    check_runs, workflow_runs = _parity_runs(name, runs)

    # Gate 5 and gate 6, the gate step's shell. A required row runs against
    # main-protection's ruleset (ci-gate is one of its five); a row nothing
    # requires against classic protection, which names no `terraform`.
    rules, branch = (RULESET_RULES, RULESET_ONLY_BRANCH) if required else (None, PROTECTED)
    proc, _calls, comments = run_gate("A fact-style headline", branch, rules=rules,
                                      check_runs=check_runs, workflow_runs=workflow_runs)
    decision = _outputs(tmp_path / "output.txt").get("decision")
    # MUTATION (auto-merge.yml gate 5): judge every run instead of each
    # event's latest -> the "failure then success" rows refuse.
    # MUTATION (auto-merge.yml gate 6): read `unfinished` over every run ->
    # "gate 6: ... in_progress then success" waits.
    assert (proc.returncode != 0 or decision != "merge") is holds, (case, decision, proc.stderr, comments)

    # The worker's merge step.
    facts = [worker_merge.CheckFacts(name=name, status=status, conclusion=conclusion, app_id=ACTIONS_APP,
                                     run_id=run_id, event=event, check_suite_id=100 + run_id)
             for run_id, event, status, conclusion in runs]
    if required:
        worker_holds = worker_merge.required_check_state([f.as_run() for f in facts]) != "green"
    else:
        pending, failed = worker_merge.judge_other_checks(facts)
        worker_holds = bool(pending or failed)
    # MUTATION (merge.py, no-required path): judge every run instead of each
    # event's latest -> the "failure then success" and "in_progress then
    # success" rows hold.
    assert worker_holds is holds, case

    # swarm-api's reading.
    api_runs = [{**run, **({"event": event} if event is not None else {})}
                for run, (_id, event, _status, _conclusion) in zip(check_runs, runs)]
    required_set = [api_forgechecks.RequiredCheck(name, None)] if required else []
    reading = api_forgechecks.evaluate(required_set, api_runs, [])
    # MUTATION (forgechecks.py, `evaluate`'s no-required branch): judge every
    # run instead of each event's latest -> those rows read red or pending.
    assert (reading.state != api_forgechecks.GREEN) is holds, (case, reading)

    # The merge train's judge. A row nothing requires is judged beside one
    # green required check, so the verdict turns on the row's check alone.
    judged = check_runs if required else check_runs + [
        {"id": 50, "name": "ci-gate", "status": "completed", "conclusion": "success",
         "check_suite": {"id": 150}}]
    verdict = _train_judge(tmp_path, judged, workflow_runs, ["ci-gate"])
    # MUTATION (merge-train.sh `cmd_judge`): judge every run instead of each
    # event's latest -> the "failure then success" rows read red. MUTATION:
    # group by name alone when no Actions run is passed -> "no event known,
    # two suites" reads green.
    assert (verdict != "green") is holds, (case, verdict)


#: archived issue 1778's second expectation (the owner released bundle H09, 2026-10-27): "a check
#: whose LATEST run is cancelled is still not green and waits, rather than
#: failing the merge". Each judge's own answer, one row each, as STUCK_PARITY
#: below: gate 5 refuses only a red latest run, and gate 6 waits on a
#: required check whose latest run is cancelled, as on one still running; a
#: red latest run of another event still wins everywhere. A check nothing
#: requires is pending in the worker and swarm-api and, like one still
#: running (archived issue 815), holds neither the gate nor the train.
#:
#: The gate is read from the tree's auto-merge.yml: its rows are red until the
#: operator applies this lane's workflow-lines.patch.
#: (case, required, runs as (run id, event, status, conclusion),
#:  gate, worker, swarm-api, train)
CANCELLED_PARITY = [
    ("required, latest cancelled", True,
     [(1, "push", "completed", "cancelled")], "wait", "pending", "pending", "pending"),
    ("required, green then cancelled in one event", True,
     [(1, "push", "completed", "success"), (2, "push", "completed", "cancelled")],
     "wait", "pending", "pending", "pending"),
    ("required, cancelled push, red pull_request", True,
     [(1, "push", "completed", "cancelled"), (2, "pull_request", "completed", "failure")],
     "refuse", "failed", "red", "red"),
    ("required, cancelled then green (control)", True,
     [(1, "push", "completed", "cancelled"), (2, "push", "completed", "success")],
     "merge", "green", "green", "green"),
    ("not required, latest cancelled", False,
     [(1, "push", "completed", "cancelled")], "merge", "pending", "pending", "green"),
    ("not required, cancelled push, red pull_request", False,
     [(1, "push", "completed", "cancelled"), (2, "pull_request", "completed", "failure")],
     "refuse", "failed", "red", "red"),
]


@pytest.mark.parametrize(("case", "required", "runs", "gate", "worker", "api", "train"),
                         CANCELLED_PARITY, ids=[c[0] for c in CANCELLED_PARITY])
def test_every_judge_waits_on_a_cancelled_latest_run_and_red_still_wins(
    run_gate, tmp_path, case, required, runs, gate, worker, api, train
):
    name = "ci-gate" if required else "terraform"
    check_runs, workflow_runs = _parity_runs(name, runs)

    # Gate 5 and gate 6, the gate step's shell.
    rules, branch = (RULESET_RULES, RULESET_ONLY_BRANCH) if required else (None, PROTECTED)
    proc, _calls, comments = run_gate("A fact-style headline", branch, rules=rules,
                                      check_runs=check_runs, workflow_runs=workflow_runs)
    decision = "refuse" if proc.returncode != 0 else _outputs(tmp_path / "output.txt").get("decision")
    # MUTATION (auto-merge.yml gate 5): refuse a cancelled run -> the
    # "latest cancelled" rows refuse. MUTATION (gate 6): wait only on a run
    # with no conclusion -> the required cancelled rows merge.
    assert decision == gate, (case, decision, proc.stderr, comments)

    # The worker's merge step.
    facts = [worker_merge.CheckFacts(name=name, status=status, conclusion=conclusion, app_id=ACTIONS_APP,
                                     run_id=run_id, event=event, check_suite_id=100 + run_id)
             for run_id, event, status, conclusion in runs]
    if required:
        got = worker_merge.required_check_state([f.as_run() for f in facts])
    else:
        pending, failed = worker_merge.judge_other_checks(facts)
        got = "failed" if failed else "pending" if pending else "green"
    # MUTATION (merge.py): read cancelled as failed -> the cancelled rows "failed".
    assert got == worker, (case, got)

    # swarm-api's reading.
    api_runs = [{**run, "event": event} for run, (_id, event, _s, _c) in zip(check_runs, runs)]
    reading = api_forgechecks.evaluate(
        [api_forgechecks.RequiredCheck(name, None)] if required else [], api_runs, [])
    # MUTATION (forgechecks.py): read cancelled as red -> those rows "red".
    assert reading.state == api, (case, reading)

    # The merge train's judge, beside one green required check when the row
    # is a check nothing requires.
    judged = check_runs if required else check_runs + [
        {"id": 50, "name": "ci-gate", "status": "completed", "conclusion": "success",
         "check_suite": {"id": 150}}]
    verdict = _train_judge(tmp_path, judged, workflow_runs, ["ci-gate"])
    # MUTATION (merge-train.sh `cmd_judge`): keep cancelled in `bad` -> the
    # cancelled rows read "red".
    assert verdict == train, (case, verdict)

    # §8: the merge may be stricter than the gate, never looser.
    if gate != "merge":
        assert worker != "green", case


#: CHECK-STUCK (the owner, 2026-10-23, part of archived issue 2031): a check run that carries a
#: conclusion AND a completed_at is judged by its conclusion, whatever
#: `status` says. GitHub left PR 2006's `secret scan` at `in_progress` with
#: `conclusion: success` and a `completed_at`, and the worker's merge step
#: waited out its 6 h on it. A run with no conclusion stays pending; a run
#: `completed` with no conclusion is not green -- pending in the gate (gate 6
#: waits on a required check with no conclusion) and in the train's judge,
#: failed in the worker and red in swarm-api, as each already read it.
#:
#: Each judge's own answer, one row each. The gate is read from the tree's
#: auto-merge.yml: the rows its old rule answers differently are red until
#: the operator applies this lane's workflow-lines.patch.
#: (case, required, status, conclusion, completed_at,
#:  gate, worker, swarm-api, train)
STUCK_AT = "2026-10-22T19:05:52Z"
STUCK_PARITY = [
    ("required, in_progress, success, completed_at", True, "in_progress", "success", STUCK_AT,
     "merge", "green", "green", "green"),
    ("required, in_progress, failure, completed_at", True, "in_progress", "failure", STUCK_AT,
     "refuse", "failed", "red", "red"),
    ("required, in_progress, no conclusion", True, "in_progress", None, None,
     "wait", "pending", "pending", "pending"),
    ("required, in_progress, success, no completed_at", True, "in_progress", "success", None,
     "wait", "pending", "pending", "pending"),
    ("required, completed, no conclusion", True, "completed", None, STUCK_AT,
     "wait", "failed", "red", "pending"),
    ("not required, in_progress, success, completed_at", False, "in_progress", "success", STUCK_AT,
     "merge", "green", "green", "green"),
    ("not required, in_progress, failure, completed_at", False, "in_progress", "failure", STUCK_AT,
     "refuse", "failed", "red", "red"),
]


@pytest.mark.parametrize(("case", "required", "status", "conclusion", "completed_at",
                          "gate", "worker", "api", "train"),
                         STUCK_PARITY, ids=[c[0] for c in STUCK_PARITY])
def test_every_judge_reads_a_run_with_a_conclusion_and_completed_at_as_finished(
    run_gate, tmp_path, case, required, status, conclusion, completed_at, gate, worker, api, train
):
    name = "ci-gate" if required else "terraform"
    run = {"id": 1, "name": name, "status": status, "conclusion": conclusion,
           "completed_at": completed_at, "app": {"id": ACTIONS_APP}, "check_suite": {"id": 101},
           "html_url": "https://github.com/o/r/actions/runs/1"}

    # Gate 5 and gate 6, the gate step's shell.
    rules, branch = (RULESET_RULES, RULESET_ONLY_BRANCH) if required else (None, PROTECTED)
    proc, _calls, comments = run_gate("A fact-style headline", branch, rules=rules, check_runs=[run])
    decision = "refuse" if proc.returncode != 0 else _outputs(tmp_path / "output.txt").get("decision")
    # MUTATION (auto-merge.yml gate 5): read status alone -> the failure row
    # merges. MUTATION (gate 6): read status alone -> the success row waits,
    # PR 2006's wait; wait on `status` without the no-conclusion rule -> the
    # "completed, no conclusion" row merges.
    assert decision == gate, (case, decision, proc.stderr, comments)

    # The worker's merge step.
    facts = worker_merge.CheckFacts(name=name, status=status, conclusion=conclusion, app_id=ACTIONS_APP,
                                    run_id=1, check_suite_id=101, completed_at=completed_at)
    if required:
        got = worker_merge.required_check_state([facts.as_run()])
    else:
        pending, failed = worker_merge.judge_other_checks([facts])
        got = "failed" if failed else "pending" if pending else "green"
    # MUTATION (merge.py): read status alone -> the success rows read
    # "pending" and the failure rows "pending".
    assert got == worker, (case, got)

    # swarm-api's reading.
    reading = api_forgechecks.evaluate([api_forgechecks.RequiredCheck(name, None)] if required else [],
                                       [run], [])
    # MUTATION (forgechecks.py): read status alone -> the same rows pending.
    assert reading.state == api, (case, reading)

    # The merge train's judge, beside one green required check when the row
    # is a check nothing requires.
    judged = [run] if required else [run, {"id": 50, "name": "ci-gate", "status": "completed",
                                           "conclusion": "success", "check_suite": {"id": 150}}]
    verdict = _train_judge(tmp_path, judged, [], ["ci-gate"])
    # MUTATION (merge-train.sh `cmd_judge`): read status alone -> the
    # success rows read "pending" and the failure rows not "red".
    assert verdict == train, (case, verdict)

    # §8: the merge may be stricter than the gate, never looser.
    if gate != "merge":
        assert worker != "green", case


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


# ---------------------------------------------------------------------------
# A `ready` label that lands while checks still run is re-evaluated (archived pull request 697)
# ---------------------------------------------------------------------------
# Measured 2026-10-06 (observer P13): archived pull request 697 got `ready` while its CI ran, the
# gate refused it ("failing or still running"), and nothing looked again, so
# it sat unmerged after going green. Every SwarmCloud pull request opened
# with pr_label ready hits that. Owner decision, 2026-10-06: a pull request
# labelled early is merged once its checks are green at head. So the gate
# WAITS on a running check, and `requeue` -- on every `workflow_run:
# completed` of a workflow that runs on pull requests -- dispatches this
# workflow for each open `ready` pull request at that run's head, which runs
# the same gate and, when it passes, the same App-token merge.


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
def run_requeue(tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    script = run_block("auto-merge.yml", "requeue", REQUEUE_STEP)
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
            "GH_REPO": "example-org/example-repo",
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


# ---------------------------------------------------------------------------
# A body edit on a `ready` pull request is re-evaluated (archived issue 888 box 97)
# ---------------------------------------------------------------------------


def test_the_body_edit_re_evaluation_dispatches_exactly_that_pull_request(tmp_path: Path):
    """RUN, against the requeue job's fake `gh`. MUTATION: dispatch on the
    pull request's head ref, or for every ready pull request."""
    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH_REQUEUE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "gh.log"
    summary = tmp_path / "summary.md"
    for path in (log, summary):
        path.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GH_REPO": "example-org/example-repo",
        "PR_NUMBER": "4242",
        "DEFAULT_BRANCH": "main",
        "GITHUB_STEP_SUMMARY": str(summary),
        "FAKE_GH_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", "-c", run_block("auto-merge.yml", "requeue-edited", REQUEUE_EDITED_STEP)],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert log.read_text().splitlines() == ["workflow run auto-merge.yml --ref main -f pr=4242"], log.read_text()
    assert "#4242" in summary.read_text(), summary.read_text()


@pytest.mark.parametrize("number", ["", "0", "12; touch x"])
def test_the_body_edit_re_evaluation_refuses_a_number_that_is_not_one(tmp_path: Path, number: str):
    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    log = tmp_path / "gh.log"
    log.write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH_REQUEUE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PR_NUMBER": number,
        "DEFAULT_BRANCH": "main",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "FAKE_GH_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", "-c", run_block("auto-merge.yml", "requeue-edited", REQUEUE_EDITED_STEP)],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode != 0
    assert log.read_text() == "", log.read_text()


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
def run_resolve(tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    script = run_block("auto-merge.yml", "enable", "pr")
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
            "GH_REPO": "example-org/example-repo",
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
    outputs = _outputs(tmp_path / "output.txt")
    assert outputs.get("decision") == "refused"
    assert outputs.get("disarm") == "true", outputs
    assert "--disable-auto" not in calls, calls
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
    assert _outputs(tmp_path / "output.txt").get("disarm") != "true"
    assert "pr edit" not in calls and "pr comment" not in calls and comments == "", calls


def test_a_re_evaluation_still_waiting_is_silent(run_gate, tmp_path: Path):
    """The label run already said it waits; each finishing run must not say it again."""
    proc, calls, comments = run_gate(
        "A fact-style headline",
        RULESET_ONLY_BRANCH,
        rules=RULESET_RULES,
        event_name="workflow_dispatch",
        check_runs=[{"name": "ci-gate", "status": "in_progress", "conclusion": None}],
    )
    assert proc.returncode == 0, proc.stderr
    assert _outputs(tmp_path / "output.txt").get("decision") == "wait"
    assert comments == "" and "pr comment" not in calls, calls


def test_a_re_evaluation_with_every_check_green_says_merge(run_gate, tmp_path: Path):
    """The point of the whole path: archived pull request 697, once green, is merged."""
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


DEPENDENCY_REFUSAL = (
    "This pull request's body says it depends on something that has not landed: "
    "#840 is an open pull request that has not merged."
)


def test_a_label_time_dependency_refusal_removes_ready_then_and_says_so(run_gate, tmp_path: Path):
    """archived issue 888 box 99. The dependency refusal (item 0b) used to keep `ready` on
    the label, like the refusals above; the next CI-finish re-evaluation then
    removed it with a second comment, which read as the gate changing its
    mind. Now it comes off at the refusal, like item 0's, and one comment says
    so. MUTATION: route 0b back through `refuse` on the label path."""
    proc, calls, comments = run_gate("A fact-style headline", PROTECTED, dependency_refusal=DEPENDENCY_REFUSAL)
    assert proc.returncode != 0
    assert "pr edit 4242 --remove-label ready" in calls, calls
    assert "--disable-auto" not in calls and "api " not in calls, calls
    assert comments.count("**Not queued") == 1, comments
    assert "**Not queued for auto-merge, and `ready` removed.** " + DEPENDENCY_REFUSAL in comments, comments
    outputs = _outputs(tmp_path / "output.txt")
    assert outputs.get("decision") == "refused" and "disarm" not in outputs, outputs


def test_a_re_evaluation_dependency_refusal_still_drops_ready_and_disarms(run_gate, tmp_path: Path):
    """The control: on a re-evaluation an earlier run may have armed it, so
    the refusal also says `disarm`, as before."""
    proc, calls, comments = run_gate(
        "A fact-style headline", PROTECTED, event_name="workflow_dispatch", dependency_refusal=DEPENDENCY_REFUSAL
    )
    assert proc.returncode == 0, proc.stderr
    assert "pr edit 4242 --remove-label ready" in calls, calls
    assert DEPENDENCY_REFUSAL in comments, comments
    outputs = _outputs(tmp_path / "output.txt")
    assert outputs.get("decision") == "refused" and outputs.get("disarm") == "true", outputs


FAKE_GH_MERGE = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "$1 $2" == "pr merge" ]]; then
  for arg in "$@"; do
    if [[ "${arg}" == "--auto" ]]; then
      if [[ -z "${FAKE_GH_AUTO_ERROR}" ]]; then
        exit 0
      fi
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
# GraphQL: whether the pull request is in the merge queue, and the enqueue.
if [[ "$1 $2" == "api graphql" ]]; then
  case "$*" in
    *enqueuePullRequest*) printf '%s\n' '{"data":{"enqueuePullRequest":{"mergeQueueEntry":{"position":1}}}}'; exit 0 ;;
    *isInMergeQueue*) printf '%s\n' "${FAKE_GH_IN_QUEUE}"; exit 0 ;;
  esac
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


@pytest.fixture
def run_merge(tmp_path: Path):
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    script = run_block("auto-merge.yml", "enable", "merge")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH_MERGE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(view: dict, *, queue: str | None = None, in_queue: str = "false",
            auto_error: str = "auto-merge could not be enabled"):
        view_file = tmp_path / "view.json"
        # The head has not moved since the label (the move is held in
        # test_auto_merge_base_merge.py).
        view_file.write_text(json.dumps({"headRefOid": HEAD, **view}))
        log = tmp_path / "gh.log"
        summary = tmp_path / "summary.md"
        for path in (log, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "example-org/example-repo",
            "PR_NUMBER": "4242",
            "PR_TITLE": "A fact-style headline",
            "HEAD_SHA": HEAD,
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_VIEW": str(view_file),
            "FAKE_GH_AUTO_ERROR": auto_error,
            "FAKE_GH_IN_QUEUE": in_queue,
        }
        # Unset, as on every run before the gate said it: the no-queue path.
        if queue is not None:
            env["QUEUE"] = queue
        proc = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False
        )
        calls = log.read_text().splitlines()
        merges = [line for line in calls if line.startswith("pr merge")]
        run.calls = calls
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


@pytest.mark.parametrize("state", ["UNSTABLE", "HAS_HOOKS"])
def test_a_pull_request_mergeable_past_a_non_required_check_is_merged_directly(run_merge, state: str):
    """archived issue 815: with every required check green and a non-required one (Trivy)
    still running, GitHub reports UNSTABLE and refuses to ENABLE auto-merge,
    as it does on CLEAN. The ruleset still decides the direct merge.
    MUTATION: accept only CLEAN, and the gate's `merge` ends in a red run."""
    proc, merges, _summary = run_merge({"state": "OPEN", "mergeStateStatus": state, "autoMergeRequest": None})
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 2 and "--auto" not in merges[1], merges
    assert f"--match-head-commit {HEAD}" in merges[1] and "--squash" in merges[1], merges


def test_on_a_merge_queue_an_unstable_pull_request_is_enqueued(run_merge):
    proc, merges, _summary = run_merge({"id": "PR_kwDOexample", "state": "OPEN",
                                        "mergeStateStatus": "UNSTABLE", "autoMergeRequest": None},
                                       queue="true")
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 1, merges
    assert [c for c in run_merge.calls if "enqueuePullRequest" in c], run_merge.calls


def test_without_a_merge_queue_the_merge_is_the_squash_it_always_was(run_merge):
    """`QUEUE=false`, as the gate says it for main today: the same calls as
    with no QUEUE at all."""
    for queue in (None, "false"):
        proc, merges, _summary = run_merge({"state": "OPEN", "mergeStateStatus": "CLEAN",
                                            "autoMergeRequest": None}, queue=queue)
        assert proc.returncode == 0, proc.stderr
        assert len(merges) == 2 and all("--squash" in m for m in merges), merges
        assert not any(c.startswith("api graphql") for c in run_merge.calls), run_merge.calls


def test_on_a_merge_queue_auto_merge_enqueues_without_a_method(run_merge):
    """`gh pr merge --auto` on a base that requires a merge queue enqueues the
    pull request once its required checks pass (GitHub's documented behaviour)."""
    proc, merges, summary = run_merge({"state": "OPEN", "mergeStateStatus": "BLOCKED",
                                       "autoMergeRequest": None}, queue="true", auto_error="")
    assert proc.returncode == 0, proc.stderr
    (merge_call,) = merges
    assert "--auto" in merge_call and f"--match-head-commit {HEAD}" in merge_call, merge_call
    assert "--squash" not in merge_call and "--subject" not in merge_call, merge_call
    assert "merge queue" in summary, summary


def test_an_already_clean_pull_request_is_enqueued_through_graphql_not_merged(run_merge):
    """The queue's already-green path: never a direct merge, which would be a
    bypass of the queue. MUTATION: fall back to `gh pr merge` without --auto."""
    proc, merges, summary = run_merge({"id": "PR_kwDOexample", "state": "OPEN",
                                       "mergeStateStatus": "CLEAN", "autoMergeRequest": None},
                                      queue="true")
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 1, merges
    (enqueue,) = [c for c in run_merge.calls if "enqueuePullRequest" in c]
    assert "id=PR_kwDOexample" in enqueue and f"head={HEAD}" in enqueue, enqueue
    assert "merge queue" in summary, summary


def test_a_pull_request_already_in_the_queue_is_not_enqueued_again(run_merge):
    proc, merges, summary = run_merge({"id": "PR_kwDOexample", "state": "OPEN",
                                       "mergeStateStatus": "CLEAN", "autoMergeRequest": None},
                                      queue="true", in_queue="true")
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 1, merges
    assert not [c for c in run_merge.calls if "enqueuePullRequest" in c], run_merge.calls
    assert "already in" in summary, summary


def test_on_a_merge_queue_a_blocked_unarmed_pull_request_still_fails(run_merge):
    """The control: a queue does not turn a real failure into a success."""
    proc, merges, _summary = run_merge({"id": "PR_kwDOexample", "state": "OPEN",
                                        "mergeStateStatus": "BLOCKED", "autoMergeRequest": None},
                                       queue="true")
    assert proc.returncode != 0
    assert len(merges) == 1, merges
    assert not [c for c in run_merge.calls if "enqueuePullRequest" in c], run_merge.calls


