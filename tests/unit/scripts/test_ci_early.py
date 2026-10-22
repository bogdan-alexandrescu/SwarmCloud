"""CI starts when the implement step pushes, not when the fix step opens the pull request.

LANE CI-EARLY (part of #1694, owner decision 2026-10-20). Under the integrate
strategy the implement step (a `contributor`) pushes `swarm/<task id>` and
opens no pull request; the fix step (the `integrator`) opens it after review
(lifecycle.py, the contributor's early return). Every required workflow ran
only on `pull_request`, so nothing was tested until then, and the merge step
spent 40 % of a workflow waiting for CI (20.5 h in the sample).

The required workflows -- application.yml (ci-gate), security.yml (four
checks) and terraform.yml (which ci-gate waits for) -- now also run on a push
to `swarm/**`, the whole of each, while review and fix happen.

WHY PUSH TRIGGERS AND NOT AN EARLY DRAFT PULL REQUEST. The merge step reads
every run of a required check at the head commit and holds the check green
only when the LATEST run of EACH event that ran it is green (merge.py
`required_check_state`; BGD's decision of 2026-10-22, CHECK-RULE, part of
#1999). A push run at the head is one more event whose latest run must be
green: a red push run is never outvoted by a green pull_request run. A re-run
replaces only the earlier runs of its own event, so a flake that passed on
re-run no longer holds the check (#1999, #1778). A draft opened at implement would be a different
pull request from the one merged (the integrator pushes its own branch), the
merge step refuses drafts, and a red draft would start the CI fixer on a
branch the fix step is about to write.

What a lane push must NOT do is anything only main may: build and push
images, plan against the dev project, release. Those are asserted here too.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
LANE_BRANCHES = "swarm/**"
MAIN_TIP = "c" * 40


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _on(name: str) -> dict:
    # PyYAML reads the bare key `on` as True.
    data = _workflow(name)
    return data.get("on", data.get(True))


@pytest.mark.parametrize("name", ["application.yml", "security.yml", "terraform.yml"])
def test_a_push_to_a_lane_branch_runs_every_required_workflow(name):
    """MUTATION: drop `swarm/**` from any required workflow's push branches."""
    push = _on(name).get("push") or {}
    assert push.get("branches") == ["main", LANE_BRANCHES], push
    assert "paths" not in push and "paths-ignore" not in push, push


def test_a_lane_push_never_releases():
    """MUTATION: add `swarm/**` to release.yml's push branches."""
    assert (_on("release.yml").get("push") or {}).get("branches") == ["main"]


def test_a_lane_push_never_builds_or_pushes_images():
    """`build images` pushes to the registry with a cloud identity; it ran on
    every event but pull_request and merge_group, which would now include a
    lane push. MUTATION: keep the old `github.event_name != 'pull_request'` if."""
    build = _workflow("application.yml")["jobs"]["build"]
    assert build["if"] == (
        "${{ !cancelled() && !failure() && github.event_name != 'pull_request' && "
        "github.event_name != 'merge_group' && github.ref == 'refs/heads/main' || "
        "!cancelled() && !failure() && github.event_name == 'workflow_dispatch' }}"
    ), build["if"]


def test_a_lane_push_never_plans_and_says_so():
    """The plan authenticates to the dev project; on a lane push it is not
    run, and `plan-not-run` says so in the run, as it does on a pull request.
    MUTATION: keep the plan's `!= 'pull_request' && != 'merge_group'` if."""
    jobs = _workflow("terraform.yml")["jobs"]
    assert jobs["plan"]["if"] == (
        "github.event_name != 'pull_request' && github.event_name != 'merge_group' && "
        "github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch'"
    ), jobs["plan"]["if"]
    assert jobs["plan-not-run"]["if"] == (
        "github.event_name == 'pull_request' || github.event_name == 'merge_group' || "
        "(github.event_name == 'push' && github.ref != 'refs/heads/main')"
    ), jobs["plan-not-run"]["if"]


def test_the_ci_fixer_acts_only_on_a_pull_requests_run():
    """A lane push and the pull request at the same head both run
    application.yml; a red head would start two fixers. The fixer already
    refuses a branch with no pull request (ci-fix.sh). MUTATION: drop the
    event condition."""
    condition = _workflow("ci-fix.yml")["jobs"]["fix"]["if"]
    assert "github.event.workflow_run.event == 'pull_request'" in condition, condition


def test_a_lane_push_is_cancelled_by_the_next_push_to_its_branch():
    """Concurrency is the ref everywhere but main, so a newer push to a lane
    branch cancels the older run. MUTATION: give every push a per-commit group."""
    concurrency = _workflow("application.yml")["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.ref != 'refs/heads/main' }}"
    assert "github.ref == 'refs/heads/main' && format(" in concurrency["group"]
    assert _workflow("security.yml")["concurrency"]["group"] == "security-${{ github.ref }}"


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
        # ONE event, a flake re-run green: the re-run replaces it (BGD,
        # 2026-10-22). Under the every-run rule this read "failed" (#1999).
        ([_run(1, "push", "failure"), _run(2, "push", "success")], "green"),
        # ONE event, green and then red: the latest is red.
        ([_run(1, "push", "success"), _run(2, "push", "failure")], "failed"),
        ([_run(1, "push", "success"), _run(2, "pull_request", "success")], "green"),
    ],
)
def test_the_merge_step_holds_each_event_s_latest_run_at_the_head(runs, state):
    """The property the push triggers rely on to keep "every required check
    green at head" exact: CI-EARLY (owner, 2026-10-20) with BGD's latest run
    per event (2026-10-22). MUTATION (merge.py): let the newest run of a name
    win whatever its event -> the push-red, pull_request-green case reads
    green. MUTATION: keep the every-run rule -> the re-run case reads failed."""
    # Imported here, not at module top: test_collection_stays_cheap holds every
    # module under tests/unit/scripts to collecting without the service stack.
    from agent_worker import merge

    assert merge.required_check_state(runs) == state


# --- the trivy gate's base on a lane push ------------------------------------
def _gate(tmp_path: Path, *, event: str, ref: str, base: str, ls_remote: str = f"{MAIN_TIP}\trefs/heads/main\n",
          ls_status: int = 0) -> tuple[subprocess.CompletedProcess[str], str]:
    # security.yml's own `trivy (repo)` job: CI-FOLD (#1799) leaves it unfolded
    # so that only it holds security-events: write (owner, 2026-10-21).
    steps = yaml.safe_load((WORKFLOWS / "security.yml").read_text())["jobs"]["filesystem"]["steps"]
    (gate,) = [s for s in steps if "scripts/trivy-new-findings.sh gate" in str(s.get("run", ""))]
    env = gate.get("env") or {}
    assert env.get("EVENT") == "${{ github.event_name }}", env
    assert env.get("REF") == "${{ github.ref }}", env
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    calls = tmp_path / "calls"
    fake = root / "scripts" / "trivy-new-findings.sh"
    fake.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > {str(calls)!r}\n")
    fake.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    git = bindir / "git"
    answer = tmp_path / "ls-remote"
    answer.write_text(ls_remote)
    git.write_text(
        "#!/usr/bin/env bash\n"
        "[ \"$*\" = 'ls-remote origin refs/heads/main' ] || exit 97\n"
        f"cat {str(answer)!r}\n"
        f"exit {ls_status}\n"
    )
    git.chmod(0o755)
    body = tmp_path / "gate.sh"
    body.write_text(gate["run"])
    proc = subprocess.run(
        ["bash", "-e", str(body)], cwd=root, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
             "EVENT": event, "REF": ref, "BASE_SHA": base},
    )
    return proc, calls.read_text().strip() if calls.exists() else ""


def test_a_lane_push_compares_trivy_findings_with_mains_tip(tmp_path):
    """With no base, every finding main already has fails the gate: a lane
    push would be red where its pull request is green, and the merge step
    holds both. MUTATION: pass the empty pull-request base on a lane push."""
    proc, args = _gate(tmp_path, event="push", ref="refs/heads/swarm/task_0123456789abcdef0123", base="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert args == f"gate --base {MAIN_TIP}", args


def test_a_pull_request_and_main_keep_their_bases(tmp_path):
    """MUTATION: look main's tip up for every event."""
    pr_base = "a" * 40
    proc, args = _gate(tmp_path / "pr", event="pull_request", ref="refs/pull/7/merge", base=pr_base)
    assert proc.returncode == 0 and args == f"gate --base {pr_base}", (proc.stderr, args)
    proc, args = _gate(tmp_path / "main", event="push", ref="refs/heads/main", base="")
    assert proc.returncode == 0 and args == "gate --base", (proc.stderr, args)


@pytest.mark.parametrize("ls_remote, ls_status", [("", 0), ("garbage\n", 0), ("", 2)])
def test_a_lane_push_that_cannot_read_mains_tip_fails_rather_than_gating_on_nothing(tmp_path, ls_remote, ls_status):
    """MUTATION: fall through to `--base ""` (every finding new) or to no gate."""
    proc, args = _gate(tmp_path, event="push", ref="refs/heads/swarm/task_0123456789abcdef0123", base="",
                       ls_remote=ls_remote, ls_status=ls_status)
    assert proc.returncode != 0, proc.stdout
    assert args == "", args
