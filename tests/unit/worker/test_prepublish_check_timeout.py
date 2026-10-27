"""A check the runner killed at its limit is a timeout, whichever check it is (#2503).

`_run_checks` marked `timed_out` only for the guard script. graph-pytest
killed at `CHECK_TIMEOUT_SECONDS` (run_command's `[timed out after Ns]`
marker, exit 124) counted as an ordinary failure: the agent got a fix turn
for a timeout no change of its own fixes, with the killed output to read.
Now any check whose output ends in the runner's kill, and shows no failure
of its own before it, is a timeout: left out of the fix prompt, named under
"Not yours to fix", and never a reason to split the guard script.

A real Gate on a scratch git repository; the checks' commands go to a fake
runner, git goes to the real one.
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
KILLED = "tests/unit/test_x.py ....\n[timed out after 600s]"
REMAINING = 1500.0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=_ENV).stdout.strip()


def _log() -> Any:
    return build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                        runner_profile="claude-code", stream=io.StringIO())


class Fake:
    """The checks' runner: `answers[name]` is a list of Ran, one per call."""

    def __init__(self, **answers: list[prepublish.Ran]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, argv, cwd, env, timeout) -> prepublish.Ran:
        name = Path(argv[0]).name
        if name in self.answers:
            self.calls.append(name)
            queue = self.answers[name]
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return prepublish.run_command(argv, cwd, env, timeout)


class Turns:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, text: str) -> bool:
        self.prompts.append(text)
        return True


def _gate(tmp_path: Path, monkeypatch, runner: Fake, names: list[str], turns: Turns
          ) -> tuple[prepublish.Gate, Path]:
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
    checks = [prepublish.Check(n, ("x.py",), f"{n} x.py", run=prepublish._simple([n, "x.py"]))
              for n in names]
    monkeypatch.setattr(prepublish, "select_checks", lambda *a, **k: list(checks))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           **{k: v for k, v in _ENV.items() if k.startswith("GIT_")},
           prepublish.CLONE_BASE_ENV: base}
    gate = prepublish.Gate(repo=repo, env=env, secrets=(), resume=turns,
                           remaining=lambda: REMAINING, log=_log(), artifacts_dir=artifacts,
                           runner=runner)
    return gate, artifacts


def test_a_graph_pytest_timeout_is_no_fix_turn(tmp_path, monkeypatch):
    runner = Fake(**{"graph-pytest": [prepublish.Ran(124, KILLED, timed_out=True)]})
    turns = Turns()
    gate, _ = _gate(tmp_path, monkeypatch, runner, ["graph-pytest"], turns)
    record = gate.run()
    first = {c["name"]: c for c in record["rounds"][0]}
    assert first["graph-pytest"].get("timed_out") is True, first
    assert first["graph-pytest"]["reason"] == "ran out of time"
    assert turns.prompts == [] and record["fix_turn"] is False
    # Not a guard timeout: no extra round of the guard script in parts.
    assert len(record["rounds"]) == 1
    assert record["failed"] == ["graph-pytest"]


def test_a_timed_out_check_beside_a_real_failure_is_not_yours_to_fix(tmp_path, monkeypatch):
    runner = Fake(**{"graph-pytest": [prepublish.Ran(124, KILLED, timed_out=True)],
                     "ruff": [prepublish.Ran(1, "x.py:1:1: F401 unused import"),
                              prepublish.Ran(0, "")]})
    turns = Turns()
    gate, _ = _gate(tmp_path, monkeypatch, runner, ["graph-pytest", "ruff"], turns)
    record = gate.run()
    assert record["fix_turn"] is True and len(turns.prompts) == 1
    prompt = turns.prompts[0]
    assert "## ruff" in prompt
    assert "## graph-pytest" not in prompt
    assert "Not yours to fix: graph-pytest" in prompt
    assert f"{int(REMAINING)} s" in prompt
    # Two rounds: the first and the one after the fix turn; no guard parts.
    assert len(record["rounds"]) == 2
    second = {c["name"]: c for c in record["rounds"][1]}
    assert second["ruff"]["status"] == "passed"


def test_a_failure_before_the_kill_still_reaches_the_agent(tmp_path, monkeypatch):
    mixed = ("tests/unit/test_x.py ..F.\nFAILED tests/unit/test_x.py::test_a - AssertionError"
             "\n[timed out after 600s]")
    runner = Fake(**{"graph-pytest": [prepublish.Ran(124, mixed, timed_out=True)]})
    turns = Turns()
    gate, _ = _gate(tmp_path, monkeypatch, runner, ["graph-pytest"], turns)
    record = gate.run()
    assert record["rounds"][0][0].get("timed_out") is None
    assert record["fix_turn"] is True
    assert "## graph-pytest" in turns.prompts[0]
