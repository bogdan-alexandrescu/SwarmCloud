"""Any pre-publish check that timed out is not the agent's to fix (#2503).

In task_f5abe2567f79434f9ed4 graph-pytest ran into its per-check timeout.
`run_command` killed it and wrote `[timed out after Ns]`, but only the guard
script's timeout was read as one: graph-pytest counted as an ordinary
failure, the gate opened a fix turn whose prompt named no time budget, and
the agent re-ran the same 59-file pytest selection until the step's 5400 s
runner deadline killed it. So:

* `_run_checks` marks ANY failed check whose output ends in the runner's
  timeout marker as timed out, `ran out of time`;
* a check that only timed out opens no fix turn, and only a timed-out
  guard script is re-run in parts;
* when a fix turn runs for something else, the timed-out check is listed
  as not the agent's to fix, and the prompt states the budget left.

The gate is real, on a scratch git repository; the runner is a fake that
answers the pytest and ruff commands and runs git for real.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from agent_worker import prepublish

from test_prepublish_gate import Turns, _change, _env, _git, _log, _tools

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PYPROJECT = '[project]\nname = "fixture"\nversion = "0"\n\n[tool.ruff]\nline-length = 99\n'
TEST_FILE = "def test_x():\n    assert 1 == 1\n"
TIMED_OUT = "....\n[timed out after 600s]"
LEFT = 1234.0


class FakeRunner:
    """graph-pytest's `uv run pytest` times out; ruff fails while `ruff_fails`
    is set; anything else (git) runs for real."""

    def __init__(self, *, ruff_fails: bool = False) -> None:
        self.ruff_fails = ruff_fails
        self.pytest_calls = 0

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str],
                 timeout: float) -> prepublish.Ran:
        argv = list(argv)
        if argv[:4] == ["uv", "run", "--quiet", "pytest"]:
            self.pytest_calls += 1
            return prepublish.Ran(124, TIMED_OUT, timed_out=True)
        if argv[:4] == ["uv", "run", "--quiet", "ruff"]:
            if self.ruff_fails and argv[4] == "check":
                return prepublish.Ran(1, "tests/test_x.py:1:1: F401 ruff says no")
            return prepublish.Ran(0, "")
        return prepublish.run_command(argv, cwd, env, timeout)


def _lane(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _change(repo, "pyproject.toml", PYPROJECT, commit=False)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _change(repo, "tests/test_x.py", TEST_FILE)
    # uv on PATH (graph-pytest and ruff run through it); no ruff of its own.
    env = {**_env(tmp_path, _tools(tmp_path, ("uv",))), prepublish.CLONE_BASE_ENV: base}
    return repo, env


def _gate(repo: Path, tmp_path: Path, env: dict[str, str], turns: Any,
          runner: FakeRunner) -> prepublish.Gate:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return prepublish.Gate(repo=repo, env=env, secrets=(), resume=turns,
                           remaining=lambda: LEFT, log=_log(), artifacts_dir=artifacts,
                           runner=runner)


def _named(round_: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in round_}


def test_a_timed_out_graph_pytest_opens_no_fix_turn(tmp_path):
    repo, env = _lane(tmp_path)
    runner = FakeRunner()
    turns = Turns()
    record = _gate(repo, tmp_path, env, turns, runner).run()

    first = _named(record["rounds"][0])
    assert first["graph-pytest"]["status"] == "failed"
    # MUTATION: reading a timeout only on the guard script turns these red.
    assert first["graph-pytest"].get("timed_out") is True
    assert first["graph-pytest"].get("reason") == "ran out of time"
    # MUTATION: a fix turn for a timeout re-runs the selection until the
    # step's runner deadline (task_f5abe2567f79434f9ed4).
    assert turns.prompts == [] and record["fix_turn"] is False
    # Not re-run at all, and no guard re-run in parts started by it.
    assert len(record["rounds"]) == 1, record["rounds"]
    assert runner.pytest_calls == 1
    assert record["guards"] == [] or not any("shard" in g for g in record["guards"])
    # Still published as a timeout for the operator.
    assert record["failed"] == ["graph-pytest"]
    section = (tmp_path / "artifacts" / prepublish.PR_SECTION_NAME).read_text()
    assert "graph-pytest timed out" in section


def test_a_timed_out_check_beside_a_real_failure_is_not_yours_to_fix(tmp_path):
    repo, env = _lane(tmp_path)
    runner = FakeRunner(ruff_fails=True)

    def fix() -> None:
        runner.ruff_fails = False

    turns = Turns(fix)
    record = _gate(repo, tmp_path, env, turns, runner).run()

    assert len(turns.prompts) == 1 and record["fix_turn"] is True
    prompt = turns.prompts[0]
    assert "## ruff-check" in prompt
    # MUTATION: sending the timeout as a failure to fix turns this red.
    assert "## graph-pytest" not in prompt
    not_yours = [line for line in prompt.splitlines() if line.startswith("Not yours to fix")]
    assert not_yours and "graph-pytest" in not_yours[0]
    # The budget left, in seconds, and an instruction not to re-run it.
    assert str(int(LEFT)) in prompt
    assert "do not run it" in prompt.lower()
    # Only the guard script is re-run in parts; graph-pytest is not.
    assert "in parts" not in not_yours[0]
    second = _named(record["rounds"][1])
    assert second["ruff-check"]["status"] == "passed"
    assert second["graph-pytest"].get("timed_out") is True
    assert len(record["rounds"]) == 2


def _check(status: str, output: str, name: str = "graph-pytest") -> prepublish.Check:
    return prepublish.Check(name, ("tests/test_x.py",), name,
                            run=lambda runner, repo, env, timeout: (status, output))


def _timed_out(check: prepublish.Check) -> bool:
    (outcome,) = prepublish._run_checks(Path("."), [check], env={}, runner=FakeRunner(),
                                        timeout=30.0, clock=lambda: 0.0)
    return outcome.timed_out


def test_run_checks_marks_any_check_that_timed_out():
    assert _timed_out(_check(prepublish.FAILED, "x\n[timed out after 30s]"))
    assert _timed_out(_check(prepublish.FAILED, "x\n[timed out after 30s]", name="vitest"))
    assert not _timed_out(_check(prepublish.FAILED, "assert 1 == 2"))
    assert not _timed_out(_check(prepublish.PASSED, "x\n[timed out after 30s]"))
    # The guard script's own deadline line still counts for the guards.
    assert _timed_out(_check(prepublish.FAILED, "fail pytest ran past 540 s",
                             name=prepublish.GUARDS_CHECK))


def test_a_project_that_failed_beside_one_that_timed_out_still_reaches_the_agent():
    # graph-pytest over two Python projects: one timed out, the other failed
    # on an assertion. The assertion is the agent's to fix, so the check is
    # not a timeout.
    mixed = ("a: failed\nb: failed\n\n--- a (failed) ---\n....\n[timed out after 300s]\n"
             "--- b (failed) ---\nFAILED tests/test_b.py::test_b - assert 1 == 2")
    assert not _timed_out(_check(prepublish.FAILED, mixed))
    both = ("a: failed\nb: failed\n\n--- a (failed) ---\n....\n[timed out after 300s]\n"
            "--- b (failed) ---\n....\n[timed out after 1s]")
    assert _timed_out(_check(prepublish.FAILED, both))
    one = ("a: failed\nb: passed\n\n--- a (failed) ---\n....\n[timed out after 300s]\n"
           "--- b (passed) ---\n1 passed")
    assert _timed_out(_check(prepublish.FAILED, one))
