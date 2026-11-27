"""The gate runs the generated-types drift check and the test-pointer check
(three merge trains red on 2026-11-23).

A lane's pull request runs no CI of its own, so the merge train was the first
place two whole-tree checks ran: twice the console's generated API types were
stale after a lane changed a request model, and once a docstring under apps/
named a test file. Each red train cost every pull request aboard about half an
hour and a bisection. So the pre-publish gate, in the lane's own container:

* runs `scripts/gen_api_types.py --check` (`api-types`) when the change
  touches a file the generator reads -- the list the generator itself prints
  with `--inputs`, never one restated here -- or the generated file, and hands
  a stale file to the fix turn with the one command that regenerates it;
* runs `scripts/check_test_pointers.py` over the whole tree (`test-pointers`)
  when the change touches apps/ or tests/, and hands each pointer to the fix
  turn by file and line with "keep the reason, drop the pointer";
* leaves either one to CI, named and never passed, when it overruns its own
  budget -- the way a guard part stopped at the guard budget is left today;
* and both budgets together fit under the gate's floor without squeezing the
  guard script out (`test_the_budget_arithmetic_holds`).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import prepublish
from agent_worker.logs import build_logger

REPO = Path(__file__).resolve().parents[3]
_IDENT = {"GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
          "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}
STALE = ("apps/swarm-ui/src/generated/api-types.ts is stale; regenerate it: "
         "uv run python scripts/gen_api_types.py")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git builds the scratch repo")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_IDENT}).stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit(repo: Path, message: str = "change") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


#: A stand-in `uv`: `uv run ... python scripts/gen_api_types.py --check` fails
#: as the real generator does while the generated file does not say FRESH.
FAKE_UV = r'''
import pathlib, sys
out = pathlib.Path("apps/swarm-ui/src/generated/api-types.ts")
if "--check" in sys.argv and not (out.is_file() and "FRESH" in out.read_text()):
    print(%r, file=sys.stderr)
    sys.exit(1)
print("apps/swarm-ui/src/generated/api-types.ts is current")
''' % STALE


def _bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(f"#!{sys.executable}" + FAKE_UV)
    uv.chmod(0o755)
    return bin_dir


def _env(tmp_path: Path) -> dict[str, str]:
    git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    base = ":".join(p for p in ("/usr/bin", "/bin") if Path(p).is_dir())
    return {"PATH": f"{_bin(tmp_path)}:{git_dir}:{base}", "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path), **_IDENT}


@pytest.fixture
def lane(tmp_path: Path) -> dict[str, Any]:
    """A scratch repository holding the REAL generator and pointer check."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for script in (prepublish.API_TYPES_SCRIPT, prepublish.TEST_POINTERS_SCRIPT):
        _write(repo, script, (REPO / script).read_text())
    _write(repo, "apps/swarm-api/swarm_api/models.py", "X = 1\n")
    _write(repo, "apps/swarm-ui/src/generated/api-types.ts", "// FRESH\n")
    _write(repo, "apps/app/app/mod.py", "X = 1\n# a reason, and no pointer\n")
    _write(repo, "tests/unit/worker/test_widget.py",
           "def test_the_widget_is_held_equal():\n    pass\n")
    _write(repo, "docs/a.md", "a note\n")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base")
    return {"repo": repo, "base": _git(repo, "rev-parse", "HEAD"), "env": _env(tmp_path)}


def _chosen(lane: dict[str, Any], *files: str) -> dict[str, prepublish.Check]:
    checks = prepublish.select_checks(lane["repo"], list(files), base=lane["base"],
                                      env=lane["env"], python=sys.executable)
    return {c.name: c for c in checks}


# ---------------------------------------------------------------------------
# Each check runs when its trigger paths change, and is skipped otherwise
# ---------------------------------------------------------------------------


@needs_git
@pytest.mark.parametrize("path", [
    "apps/swarm-api/swarm_api/models.py",          # a package the generator imports
    "apps/common/swarm_common/config.py",          # the settings it builds the app with
    "apps/swarm-ui/src/generated/api-types.ts",    # the generated file itself
    "scripts/gen_api_types.py",                    # the generator
    "uv.lock",                                     # the FastAPI and pydantic it runs
])
def test_the_drift_check_runs_when_a_file_the_generator_reads_changes(lane, path):
    check = _chosen(lane, path)[prepublish.API_TYPES_CHECK]
    # MUTATION: a trigger list that misses the request models is the red train.
    assert check.run is not None, check.skipped
    assert check.budget == prepublish.API_TYPES_BUDGET_SECONDS


@needs_git
@pytest.mark.parametrize("path", ["docs/a.md", "apps/agent-worker/agent_worker/x.py",
                                  "terraform/infra/main.tf"])
def test_the_drift_check_is_skipped_when_nothing_the_generator_reads_changed(lane, path):
    check = _chosen(lane, path)[prepublish.API_TYPES_CHECK]
    # MUTATION: running it for every change spends ~12 s a round on nothing.
    assert check.run is None
    assert "generator" in (check.skipped or "")


@needs_git
def test_the_trigger_list_is_the_one_the_generator_prints(lane):
    # The generator names its own inputs; the gate restates none of them.
    gen = lane["repo"] / prepublish.API_TYPES_SCRIPT
    gen.write_text(gen.read_text().replace('"uv.lock",', '"uv.lock", "docs/a.md",'))
    assert _chosen(lane, "docs/a.md")[prepublish.API_TYPES_CHECK].run is not None


@needs_git
@pytest.mark.parametrize("path", ["apps/app/app/mod.py", "apps/swarm-ui/src/App.tsx",
                                  "tests/unit/worker/test_widget.py"])
def test_the_pointer_check_runs_when_apps_or_a_test_changes(lane, path):
    check = _chosen(lane, path)[prepublish.TEST_POINTERS_CHECK]
    # A new test's name can turn a comment nobody touched into a pointer, so
    # a changed test triggers it too.
    assert check.run is not None, check.skipped
    assert check.budget == prepublish.TEST_POINTERS_BUDGET_SECONDS


@needs_git
@pytest.mark.parametrize("path", ["docs/a.md", "scripts/x.sh", "terraform/infra/main.tf"])
def test_the_pointer_check_is_skipped_when_neither_apps_nor_tests_changed(lane, path):
    check = _chosen(lane, path)[prepublish.TEST_POINTERS_CHECK]
    assert check.run is None
    assert "apps/" in (check.skipped or "")


def test_a_repository_without_the_scripts_chooses_neither(tmp_path):
    names = [c.name for c in prepublish.select_checks(tmp_path, ["apps/a/b.py"], base="b",
                                                      env={})]
    assert prepublish.API_TYPES_CHECK not in names
    assert prepublish.TEST_POINTERS_CHECK not in names


def test_both_run_before_the_guard_script(tmp_path):
    for script in (prepublish.GUARDS_SCRIPT, prepublish.API_TYPES_SCRIPT,
                   prepublish.TEST_POINTERS_SCRIPT):
        _write(tmp_path, script, "")
    names = [c.name for c in prepublish.select_checks(
        tmp_path, ["apps/swarm-api/swarm_api/m.py"], base="b", env={})]
    # Ahead of the guard script, whose time-boxed parts can be left to CI.
    assert names.index(prepublish.API_TYPES_CHECK) < names.index(prepublish.GUARDS_CHECK)
    assert names.index(prepublish.TEST_POINTERS_CHECK) < names.index(prepublish.GUARDS_CHECK)


# ---------------------------------------------------------------------------
# A failure reaches the fix turn with its message
# ---------------------------------------------------------------------------


@needs_git
def test_a_stale_file_fails_with_the_one_command_that_fixes_it(lane):
    _write(lane["repo"], "apps/swarm-ui/src/generated/api-types.ts", "// old\n")
    check = _chosen(lane, "apps/swarm-ui/src/generated/api-types.ts")[prepublish.API_TYPES_CHECK]
    assert check.run is not None
    status, output = check.run(prepublish.run_command, lane["repo"], lane["env"], 60.0)
    assert status == prepublish.FAILED
    assert "is stale" in output
    assert f"`{prepublish.API_TYPES_FIX}`" in output and "commit" in output


@needs_git
def test_a_pointer_fails_with_its_file_and_line_and_the_advice(lane):
    _write(lane["repo"], "apps/app/app/mod.py",
           "X = 1\n# held equal by tests/unit/worker/test_widget.py\n")
    check = _chosen(lane, "apps/app/app/mod.py")[prepublish.TEST_POINTERS_CHECK]
    assert check.run is not None
    status, output = check.run(prepublish.run_command, lane["repo"], lane["env"], 60.0)
    assert status == prepublish.FAILED
    assert "apps/app/app/mod.py:2:" in output
    assert "keep the reason, drop the pointer" in output.lower()


def _gate(lane: dict[str, Any], tmp_path: Path, turns: "Turns") -> prepublish.Gate:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    log = build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                       runner_profile="claude-code", stream=io.StringIO())
    env = {**lane["env"], prepublish.CLONE_BASE_ENV: lane["base"]}
    return prepublish.Gate(repo=lane["repo"], env=env, secrets=(), resume=turns,
                           remaining=lambda: 3600.0, log=log, artifacts_dir=artifacts,
                           python=sys.executable)


class Turns:
    """The agent's session: each resume records its prompt and runs `act`."""

    def __init__(self, *acts: Any) -> None:
        self.prompts: list[str] = []
        self._acts = list(acts)

    def __call__(self, text: str) -> bool:
        self.prompts.append(text)
        if self._acts:
            self._acts.pop(0)()
        return True


def _statuses(round_: list[dict]) -> dict[str, str]:
    return {c["name"]: c["status"] for c in round_}


@needs_git
def test_the_gate_hands_a_stale_file_to_the_fix_turn_and_it_passes_after(lane, tmp_path):
    _write(lane["repo"], "apps/swarm-api/swarm_api/models.py", "X = 2\n")
    _write(lane["repo"], "apps/swarm-ui/src/generated/api-types.ts", "// old\n")
    _commit(lane["repo"])

    def regenerate() -> None:
        _write(lane["repo"], "apps/swarm-ui/src/generated/api-types.ts", "// FRESH again\n")
        _commit(lane["repo"], "regenerate")

    turns = Turns(regenerate)
    record = _gate(lane, tmp_path, turns).run()
    (prompt,) = turns.prompts
    assert f"## {prepublish.API_TYPES_CHECK}" in prompt
    assert prepublish.API_TYPES_FIX in prompt
    first, second = record["rounds"]
    assert _statuses(first)[prepublish.API_TYPES_CHECK] == prepublish.FAILED
    assert _statuses(second)[prepublish.API_TYPES_CHECK] == prepublish.PASSED


@needs_git
def test_the_gate_hands_a_pointer_to_the_fix_turn_by_file_and_line(lane, tmp_path):
    _write(lane["repo"], "apps/app/app/mod.py",
           'def f():\n    """Held equal by\n    tests/unit/worker/test_widget.py."""\n')
    _commit(lane["repo"])

    def drop_pointer() -> None:
        _write(lane["repo"], "apps/app/app/mod.py",
               'def f():\n    """Two callers read this; keep it pure."""\n')
        _commit(lane["repo"], "drop the pointer")

    turns = Turns(drop_pointer)
    record = _gate(lane, tmp_path, turns).run()
    (prompt,) = turns.prompts
    assert f"## {prepublish.TEST_POINTERS_CHECK}" in prompt
    assert "apps/app/app/mod.py:3:" in prompt
    assert "keep the reason, drop the pointer" in prompt.lower()
    first, second = record["rounds"]
    assert _statuses(first)[prepublish.TEST_POINTERS_CHECK] == prepublish.FAILED
    assert _statuses(second)[prepublish.TEST_POINTERS_CHECK] == prepublish.PASSED


# ---------------------------------------------------------------------------
# A check that overruns is left to CI, never passed and never sent to fix
# ---------------------------------------------------------------------------


def _overrunning(name: str, budget: float, limits: list[float]) -> prepublish.Check:
    def run(runner: Any, repo: Path, env: Any, timeout: float) -> tuple[str, str]:
        limits.append(timeout)
        return prepublish.FAILED, f"building the document...\n[timed out after {int(timeout)}s]"
    return prepublish.Check(name, ("apps/swarm-api/swarm_api/m.py",), f"run {name}", run=run,
                            budget=budget)


@pytest.mark.parametrize("name", ["api-types", "test-pointers"])
def test_an_overrun_is_left_to_ci_and_reported_as_left(tmp_path, name):
    budget = {"api-types": prepublish.API_TYPES_BUDGET_SECONDS,
              "test-pointers": prepublish.TEST_POINTERS_BUDGET_SECONDS}[name]
    limits: list[float] = []
    (outcome,) = prepublish.run_checks(tmp_path, [_overrunning(name, budget, limits)], env={},
                                       timeout=prepublish.CHECK_TIMEOUT_SECONDS)
    # Its own budget, not the 600 s every check may take.
    assert limits == [budget]
    # MUTATION: reporting the kill as a failure sends the agent to fix a
    # clock; reporting it as passed hides it from the reader.
    assert outcome.status == prepublish.SKIPPED and not outcome.timed_out
    assert outcome.left_to_ci == (f"run {name}",)
    assert "left to CI" in (outcome.reason or "")
    assert outcome.record()["left_to_ci"] == [f"run {name}"]
    assert prepublish.pr_section(None, [outcome], ()) is None
    advisory = prepublish.advisory_section(graph={}, outcomes=[outcome], reach=None,
                                           self_check=None, secrets=())
    assert f"`{name}`" in advisory and "left to CI" in advisory


def test_a_failure_printed_before_the_kill_still_reaches_the_fix_turn(tmp_path):
    def run(runner: Any, repo: Path, env: Any, timeout: float) -> tuple[str, str]:
        return prepublish.FAILED, "FAILED tests/x.py::test_y\n[timed out after 15s]"
    check = prepublish.Check("test-pointers", ("apps/a.py",), "run", run=run, budget=15.0)
    (outcome,) = prepublish.run_checks(tmp_path, [check], env={})
    assert outcome.status == prepublish.FAILED and not outcome.left_to_ci


def test_the_step_budget_still_bounds_a_budgeted_check(tmp_path):
    limits: list[float] = []
    now = [0.0]
    check = _overrunning("api-types", prepublish.API_TYPES_BUDGET_SECONDS, limits)
    deadline = prepublish.CHECK_MARGIN_SECONDS + 20.0
    prepublish.run_checks(tmp_path, [check], env={}, clock=lambda: now[0], deadline=deadline)
    assert limits == [20.0]


# ---------------------------------------------------------------------------
# The budget arithmetic, against the gate's own constants
# ---------------------------------------------------------------------------


def test_the_budget_arithmetic_holds():
    p = prepublish
    # The gate starts with as little as MIN_GATE_SECONDS left; every check
    # keeps CHECK_MARGIN_SECONDS back, and the guard script must still have
    # MIN_CHECK_SECONDS to start after everything that runs ahead of it: the
    # two ~1 s checks already there, and both new checks at their full budget.
    ahead = p.PRE_GUARD_MEASURED_SECONDS + p.API_TYPES_BUDGET_SECONDS + p.TEST_POINTERS_BUDGET_SECONDS
    assert ahead <= p.MIN_GATE_SECONDS - p.CHECK_MARGIN_SECONDS - p.MIN_CHECK_SECONDS
    # Each budget is at least three times its slowest measured run, so a
    # normal run is never cut, and each is under the any-check limit.
    for budget, measured in ((p.API_TYPES_BUDGET_SECONDS, p.API_TYPES_MEASURED_SECONDS),
                             (p.TEST_POINTERS_BUDGET_SECONDS, p.TEST_POINTERS_MEASURED_SECONDS)):
        assert budget >= 3 * measured
        assert budget <= p.CHECK_TIMEOUT_SECONDS
    # The guard script's own budget is wall time from its own start: what
    # runs ahead of it takes nothing from it.
    assert p.GUARDS_BUDGET_SECONDS == 600.0


def test_the_fix_command_is_the_generators_own():
    text = (REPO / prepublish.API_TYPES_SCRIPT).read_text()
    assert f'REGENERATE = "{prepublish.API_TYPES_FIX}"' in text
