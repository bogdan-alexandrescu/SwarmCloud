"""Each pre-publish check gets the step's budget left when it starts (#2274).

The gate computed one timeout per round -- `min(CHECK_TIMEOUT_SECONDS,
remaining() - 30)` -- and handed it to every check in sequence. With 640 s
left after the agent's end_turn, three checks that each ran to their limit
took ~1,800 s, the runner was SIGKILLed at the step deadline (exit -9), no
prepublish-checks.md was written, and the workflow's review, fix and merge
were cancelled. Now each check's limit is what is left when it starts, and a
check the budget cannot cover is skipped for budget, never started.

The clock is fake: each check advances it by the whole timeout it was given,
the worst case, so the total is exact and nothing sleeps.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import prepublish
from agent_worker.logs import build_logger

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
        "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=_ENV).stdout.strip()


def _log() -> Any:
    return build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                        runner_profile="claude-code", stream=io.StringIO())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _greedy(clock: Clock, given: list[tuple[str, float]], name: str) -> prepublish.Check:
    """A check that uses the whole timeout it is given, then passes."""

    def run(runner: Any, repo: Path, env: Any, timeout: float) -> tuple[str, str]:
        given.append((name, timeout))
        clock.now += timeout
        return prepublish.PASSED, ""

    return prepublish.Check(name, ("x.py",), f"run {name}", run=run)


def test_the_gate_stays_within_the_step_budget_and_skips_the_third_check(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "x.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")

    clock = Clock()
    start = clock.now
    given: list[tuple[str, float]] = []
    checks = [_greedy(clock, given, n) for n in ("changed-guards", "ruff", "graph-pytest")]
    monkeypatch.setattr(prepublish, "select_checks", lambda *a, **k: list(checks))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           **{k: v for k, v in _ENV.items() if k.startswith("GIT_")},
           prepublish.CLONE_BASE_ENV: base}
    gate = prepublish.Gate(repo=repo, env=env, secrets=(), resume=lambda text: True,
                           remaining=lambda: 640.0 - (clock.now - start), log=_log(),
                           artifacts_dir=artifacts, clock=clock)
    record = gate.run()

    assert clock.now - start <= 640.0, (
        f"the gate ran {clock.now - start:.0f} s on a 640 s budget: past the step's deadline")
    assert [name for name, _ in given] == ["changed-guards", "ruff"]
    statuses = {c["name"]: c["status"] for c in record["rounds"][0]}
    assert statuses == {"changed-guards": "passed", "ruff": "passed",
                        "graph-pytest": "skipped"}
    reason = next(c.get("reason") for c in record["rounds"][0] if c["name"] == "graph-pytest")
    assert reason and "step's budget" in reason
    # Advisory, like every skipped check: no fix turn, not a failure, no PR section.
    assert record["failed"] == [] and record["fix_turn"] is False
    assert record["pr_section"] is False
    advisory = (artifacts / prepublish.ADVISORY_SECTION_NAME).read_text()
    assert "graph-pytest skipped (the step's budget ran out" in advisory


def test_each_check_gets_the_budget_left_when_it_starts(tmp_path):
    clock = Clock()
    given: list[tuple[str, float]] = []
    checks = [_greedy(clock, given, n) for n in ("a", "b", "c", "d")]
    margin = prepublish.CHECK_MARGIN_SECONDS
    deadline = clock.now + 2 * prepublish.CHECK_TIMEOUT_SECONDS + 300.0 + margin
    outcomes = prepublish._run_checks(tmp_path, checks, env={}, runner=prepublish.run_command,
                                      timeout=prepublish.CHECK_TIMEOUT_SECONDS, clock=clock,
                                      deadline=deadline)

    # Plenty of time left: the full per-check limit, no regression.
    assert given[0] == ("a", prepublish.CHECK_TIMEOUT_SECONDS)
    assert given[1] == ("b", prepublish.CHECK_TIMEOUT_SECONDS)
    # Then what is left when it starts: deadline - now - margin.
    assert given[2] == ("c", pytest.approx(300.0))
    assert [name for name, _ in given] == ["a", "b", "c"]
    assert [o.status for o in outcomes] == ["passed"] * 3 + ["skipped"]
    assert "step's budget" in (outcomes[3].reason or "")
    assert clock.now <= deadline

    # No deadline: every check gets the cap, as before.
    given.clear()
    prepublish._run_checks(tmp_path, checks[:2], env={}, runner=prepublish.run_command,
                           timeout=prepublish.CHECK_TIMEOUT_SECONDS, clock=clock)
    assert [t for _, t in given] == [prepublish.CHECK_TIMEOUT_SECONDS] * 2


def test_a_check_already_skipped_keeps_its_own_reason(tmp_path):
    clock = Clock()
    given: list[tuple[str, float]] = []
    checks = [
        prepublish.Check("tflint", ("main.tf",), "tflint", skipped="tflint is not installed"),
        _greedy(clock, given, "ruff"),
        prepublish.Check("shellcheck", ("a.sh",), "shellcheck",
                         skipped="shellcheck is not installed"),
        _greedy(clock, given, "pytest"),
    ]
    deadline = clock.now + 10.0  # under the minimum from the start
    outcomes = prepublish._run_checks(tmp_path, checks, env={}, runner=prepublish.run_command,
                                      timeout=prepublish.CHECK_TIMEOUT_SECONDS, clock=clock,
                                      deadline=deadline)
    assert given == []
    reasons = {o.name: (o.status, o.reason) for o in outcomes}
    assert reasons["tflint"] == ("skipped", "tflint is not installed")
    assert reasons["shellcheck"] == ("skipped", "shellcheck is not installed")
    assert reasons["ruff"][0] == "skipped" and "step's budget" in (reasons["ruff"][1] or "")
    assert reasons["pytest"][0] == "skipped" and "step's budget" in (reasons["pytest"][1] or "")
