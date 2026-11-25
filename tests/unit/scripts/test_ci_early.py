"""CI starts when the implement step pushes, not when the fix step opens the pull request.

LANE CI-EARLY (part of archived issue 1694, owner decision 2026-10-20). Under the integrate
strategy the implement step (a `contributor`) pushes `swarm/<task id>` and
opens no pull request; the fix step (the `integrator`) opens it after review
(lifecycle.py, the contributor's early return). Every required workflow ran
only on `pull_request`, so nothing was tested until then, and the merge step
spent 40 % of a workflow waiting for CI (20.5 h in the sample).

The required workflows -- application.yml (ci-gate), security.yml (four
checks) and terraform.yml (which ci-gate waits for) -- also ran on a push to
`swarm/**`, the whole of each, while review and fix happened.

SUPERSEDED FOR THE TRIGGERS (owner decision 2026-11-02, CI once per head,
part of archived issue 2688): a lane head ran every check twice, and merges queued behind
both runs, so a lane branch no longer runs them on push. What stays here
holds what a lane push would do if one ran (a workflow_dispatch on a lane
ref, a push run at a shared sha).

WHY PUSH TRIGGERS AND NOT AN EARLY DRAFT PULL REQUEST. The merge step reads
every run of a required check at the head commit and holds the check green
only when the LATEST run of EACH event that ran it is green (merge.py
`required_check_state`; the owner's decision of 2026-10-22, CHECK-RULE, part of
archived issue 1999). A push run at the head is one more event whose latest run must be
green: a red push run is never outvoted by a green pull_request run. A re-run
replaces only the earlier runs of its own event, so a flake that passed on
re-run no longer holds the check (archived issue 1999, archived issue 1778). A draft opened at implement would be a different
pull request from the one merged (the integrator pushes its own branch), the
merge step refuses drafts, and a red draft would start the CI fixer on a
branch the fix step is about to write.

What stays here: the merge step's per-event rule, and the trivy gate's base
(the true merge base, never a recorded commit), run from the step's
published shell (scripts/generated/ci-run-blocks.json).

FIVE TESTS READ THE WORKFLOWS: the gate step's environment (which event
fields become EVENT, REF, PR_BASE_REF and QUEUE_BASE_SHA), and that a lane
push never releases, never builds or pushes images, never plans (and says
so), and never starts the CI fixer. They hold CI security decisions the
owner kept on 2026-11-25 (no cloud identity and no release off a lane
branch), so this file is on scripts/check_test_reads.py's SECURITY_GUARDS
list for those tests alone.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from .ci_run_blocks import run_block

REPO = Path(__file__).resolve().parents[3]
SECURITY_YML = REPO / ".github" / "workflows" / "security.yml"
#: security.yml's `trivy (repo)` job (`filesystem`): CI-FOLD (archived issue
#: 1799) leaves it unfolded so that only it holds security-events: write
#: (owner, 2026-10-21).
TRIVY_GATE = ("security.yml", "filesystem", "fail on fixable HIGH/CRITICAL new against the base branch")


def _run(run_id: int, event: str, conclusion: str | None, status: str = "completed") -> dict:
    return {"status": status, "conclusion": conclusion, "run_id": run_id, "event": event}


@pytest.mark.parametrize(
    "runs, state",
    [
        # The lane push's run passed; the pull request's is still running.
        ([_run(1, "push", "success"), _run(2, "pull_request", None, "in_progress")], "pending"),
        # The pull request's run passed; the lane push's run at the same head
        # is red. CI-EARLY: the green pull_request run never outvotes it.
        ([_run(1, "push", "failure"), _run(2, "pull_request", "success")], "failed"),
        # The lane push's run passed; the pull request's run is red.
        ([_run(1, "push", "success"), _run(2, "pull_request", "failure")], "failed"),
        # ONE event, a flake re-run green: the re-run replaces it (the owner,
        # 2026-10-22). Under the every-run rule this read "failed" (archived issue 1999).
        ([_run(1, "push", "failure"), _run(2, "push", "success")], "green"),
        # ONE event, green and then red: the latest is red.
        ([_run(1, "push", "success"), _run(2, "push", "failure")], "failed"),
        ([_run(1, "push", "success"), _run(2, "pull_request", "success")], "green"),
    ],
)
def test_the_merge_step_holds_each_event_s_latest_run_at_the_head(runs, state):
    """The property the push triggers rely on to keep "every required check
    green at head" exact: CI-EARLY (owner, 2026-10-20) with the owner's latest run
    per event (2026-10-22). MUTATION (merge.py): let the newest run of a name
    win whatever its event -> the push-red, pull_request-green case reads
    green. MUTATION: keep the every-run rule -> the re-run case reads failed."""
    # Imported here, not at module top: test_collection_stays_cheap holds every
    # module under tests/unit/scripts to collecting without the service stack.
    from agent_worker import merge

    assert merge.required_check_state(runs) == state


# --- the trivy gate's base: the true merge base, never a recorded commit -----
def test_the_gate_step_maps_the_event_and_the_branch_a_pull_request_targets():
    """The scan gate's environment (exception 7 of the owner's 2026-11-25
    list): the branch the pull request targets now (a retarget changes it),
    never the base commit GitHub recorded on the event. MUTATION: map
    PR_BASE_REF from `github.event.pull_request.base.sha`."""
    steps = yaml.safe_load(SECURITY_YML.read_text())["jobs"][TRIVY_GATE[1]]["steps"]
    (gate,) = [s for s in steps if s.get("name") == TRIVY_GATE[2]]
    env = gate.get("env") or {}
    assert env.get("EVENT") == "${{ github.event_name }}", env
    assert env.get("REF") == "${{ github.ref }}", env
    assert env.get("PR_BASE_REF") == "${{ github.event.pull_request.base.ref }}", env
    assert env.get("QUEUE_BASE_SHA") == "${{ github.event.merge_group.base_sha }}", env
    assert "pull_request.base.sha" not in str(env), env


def _gate(tmp_path: Path, *, event: str, ref: str, pr_base_ref: str = "", queue_base: str = "",
          ) -> tuple[subprocess.CompletedProcess[str], str]:
    # The step's env maps EVENT, REF, PR_BASE_REF and QUEUE_BASE_SHA from the
    # event (held by the test above); the test sets them directly.
    script = run_block(*TRIVY_GATE)
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    calls = tmp_path / "calls"
    fake = root / "scripts" / "trivy-new-findings.sh"
    fake.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > {str(calls)!r}\n")
    fake.chmod(0o755)
    # No git at all: the step reads the base from the checkout through the
    # script, never from the network (its checkout holds no credentials).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    git = bindir / "git"
    git.write_text("#!/usr/bin/env bash\nexit 97\n")
    git.chmod(0o755)
    body = tmp_path / "gate.sh"
    body.write_text(script)
    proc = subprocess.run(
        ["bash", "-e", str(body)], cwd=root, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
             "EVENT": event, "REF": ref, "PR_BASE_REF": pr_base_ref, "QUEUE_BASE_SHA": queue_base},
    )
    return proc, calls.read_text().strip() if calls.exists() else ""


def test_a_lane_push_compares_trivy_findings_with_its_merge_base_with_main(tmp_path):
    """With no base, every finding main already has fails the gate: a lane
    push would be red where its pull request is green, and the merge step
    holds both. MUTATION: pass the empty pull-request base on a lane push."""
    proc, args = _gate(tmp_path, event="push", ref="refs/heads/swarm/task_0123456789abcdef0123")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert args == "gate --merge-base refs/remotes/origin/main", args


def test_a_pull_request_is_compared_with_its_merge_base_with_the_branch_it_targets_now(tmp_path):
    """A retargeted pull request: its recorded base commit is the old
    branch's, so the gate takes the merge base with the branch it targets.
    MUTATION: hand the script `--base <recorded base sha>`."""
    proc, args = _gate(tmp_path / "pr", event="pull_request", ref="refs/pull/7/merge", pr_base_ref="release-2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert args == "gate --merge-base refs/remotes/origin/release-2", args


def test_a_queue_entry_and_main_keep_their_bases(tmp_path):
    """MUTATION: give main's push a base, or drop the queue entry's."""
    queue_base = "a" * 40
    proc, args = _gate(tmp_path / "queue", event="merge_group", ref="refs/heads/gh-readonly-queue/main/x",
                       queue_base=queue_base)
    assert proc.returncode == 0 and args == f"gate --merge-base {queue_base}", (proc.stderr, args)
    proc, args = _gate(tmp_path / "main", event="push", ref="refs/heads/main")
    assert proc.returncode == 0 and args == "gate --base", (proc.stderr, args)


def test_a_pull_request_with_no_base_branch_fails_rather_than_gating_on_nothing(tmp_path):
    """MUTATION: fall through to `--base ""` (every finding new) or to no gate."""
    proc, args = _gate(tmp_path, event="pull_request", ref="refs/pull/7/merge", pr_base_ref="")
    assert proc.returncode != 0, proc.stdout
    assert args == "", args


WORKFLOWS = REPO / ".github" / "workflows"
#: The clause every job's `if:` in the three files ends with (`&& LANE_SKIP`),
#: as test_fast_checks_on_lane_prs.py defined it on origin/main (issue 71
#: deleted it there; the lane-push tests below need it).
LANE_SKIP = ("!(github.event_name == 'pull_request' && startsWith(github.head_ref, 'swarm/') "
             "&& github.event.pull_request.head.repo.full_name == github.repository)")


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _on(name: str) -> dict:
    # PyYAML reads the bare key `on` as True.
    data = _workflow(name)
    return data.get("on", data.get(True))


def test_a_lane_push_never_releases():
    """MUTATION: add `swarm/**` to release.yml's push branches."""
    assert (_on("release.yml").get("push") or {}).get("branches") == ["main"]


def test_a_lane_push_never_builds_or_pushes_images():
    """`build images` pushes to the registry with a cloud identity; it ran on
    every event but pull_request and merge_group, which would now include a
    lane push. MUTATION: keep the old `github.event_name != 'pull_request'` if."""
    build = _workflow("application.yml")["jobs"]["build"]
    # The lane clause (test_fast_checks_on_lane_prs.py) ends every job's `if:`.
    assert build["if"] == (
        "${{ (!cancelled() && !failure() && github.event_name != 'pull_request' && "
        "github.event_name != 'merge_group' && github.ref == 'refs/heads/main' || "
        "!cancelled() && !failure() && github.event_name == 'workflow_dispatch') && " + LANE_SKIP + " }}"
    ), build["if"]


def test_a_lane_push_never_plans_and_says_so():
    """The plan authenticates to the dev project; on a lane push it is not
    run, and `plan-not-run` says so in the run, as it does on a pull request.
    MUTATION: keep the plan's `!= 'pull_request' && != 'merge_group'` if."""
    jobs = _workflow("terraform.yml")["jobs"]
    assert jobs["plan"]["if"] == (
        "(github.event_name != 'pull_request' && github.event_name != 'merge_group' && "
        "github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch') && " + LANE_SKIP
    ), jobs["plan"]["if"]
    assert jobs["plan-not-run"]["if"] == (
        "(github.event_name == 'pull_request' || github.event_name == 'merge_group' || "
        "(github.event_name == 'push' && github.ref != 'refs/heads/main')) && " + LANE_SKIP
    ), jobs["plan-not-run"]["if"]


def test_the_ci_fixer_acts_only_on_a_pull_requests_run():
    """A lane push and the pull request at the same head both run
    application.yml; a red head would start two fixers. The fixer already
    refuses a branch with no pull request (ci-fix.sh). MUTATION: drop the
    event condition."""
    condition = _workflow("ci-fix.yml")["jobs"]["fix"]["if"]
    assert "github.event.workflow_run.event == 'pull_request'" in condition, condition
