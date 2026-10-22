"""The pre-publish checks run uv outside the clone and leave the tree as they found it (#1964).

Lane PREPUB-UV-OUTSIDE. Measured 2026-10-22 on sagaxyz/ai-studio
(task_2df71fc1d5d34997957a, task_65d0f0328cc6464f8358): the gate's
`uv run pytest`, run from `runner/` inside the agent's clone with no
`UV_PROJECT_ENVIRONMENT`, created `runner/.venv` and an untracked
`runner/uv.lock`; the publish's `git add --all` then committed the 1,659-line
lock into both lanes' pull requests, and the venv turned that repository's own
pin guards red during the check.

So:

* every `uv` a check runs is given `UV_PROJECT_ENVIRONMENT` in a directory
  under a temporary root outside the checkout, one per project directory,
  removed after the checks; the gate's own environment never carries it;
* the working tree is recorded before a round of checks and, after it, every
  path a check created or changed is restored: a new untracked path removed, a
  changed path given back its recorded content. The agent's own changes are
  untouched, and the advisory section names what was restored.

The `uv` here is a stand-in on PATH that records its cwd and environment and
then misbehaves the way uv did in the clone.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_worker import prepublish
from agent_worker.logs import build_logger
from agent_worker.prepublish import Ran

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_IDENT = {"GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
          "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}

#: A stand-in `uv`: records its cwd and UV_PROJECT_ENVIRONMENT in $UV_LOG and,
#: unless $UV_QUIET is set, does what uv did inside the clone -- writes
#: `uv.lock` and `.venv/` into its cwd -- and then also overwrites a clean
#: tracked file, the agent's modified tracked file and the agent's untracked
#: file, which no check may leave changed.
FAKE_UV = r'''
import json, os, pathlib, sys
cwd = pathlib.Path.cwd()
with open(os.environ["UV_LOG"], "a") as f:
    f.write(json.dumps({"cwd": str(cwd), "argv": sys.argv[1:],
                        "project_env": os.environ.get("UV_PROJECT_ENVIRONMENT")}) + "\n")
if not os.environ.get("UV_QUIET"):
    (cwd / "uv.lock").write_text("version = 1\n" * 50)
    (cwd / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (cwd / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    for name in ("tracked.txt", "README.md", "notes/agent.py"):
        (cwd / name).write_text("a check was here\n")
    (cwd / "gone.txt").unlink(missing_ok=True)
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_IDENT}).stdout


def _log() -> Any:
    return build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                        runner_profile="claude-code", stream=io.StringIO())


def _lane(tmp_path: Path) -> tuple[Path, str]:
    """A root project configuring ruff, with no uv.lock; the agent committed
    one change, modified README.md without committing, and left an untracked
    file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for path, text in {"README.md": "hello\n", "tracked.txt": "as committed\n",
                       "gone.txt": "a tracked file a check deletes\n",
                       "pyproject.toml": '[project]\nname = "x"\n\n[tool.ruff]\n',
                       "app.py": "x = 1\n"}.items():
        (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "app.py").write_text("x = 2\n")
    _git(repo, "commit", "-q", "-am", "the lane's change")
    (repo / "README.md").write_text("the agent's README\n")
    (repo / "notes").mkdir()
    (repo / "notes" / "agent.py").write_text("y = 'the agent wrote this'\n")
    return repo, base


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(f"#!{sys.executable}" + FAKE_UV)
    uv.chmod(0o755)
    git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    base = ":".join(p for p in ("/usr/bin", "/bin") if Path(p).is_dir())
    # No ruff on this PATH: the gate runs the repository's ruff through `uv run`.
    return {"PATH": f"{bin_dir}:{git_dir}:{base}", "HOME": str(tmp_path),
            "UV_LOG": str(tmp_path / "uv.jsonl"), **_IDENT, **extra}


def _uv_calls(tmp_path: Path) -> list[dict]:
    path = tmp_path / "uv.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _gate(repo: Path, tmp_path: Path, env: dict[str, str]) -> prepublish.Gate:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return prepublish.Gate(repo=repo, env=env, secrets=(), resume=lambda _text: True,
                           remaining=lambda: 3600.0, log=_log(), artifacts_dir=artifacts)


def _advisory(tmp_path: Path) -> str:
    return (tmp_path / "artifacts" / prepublish.ADVISORY_SECTION_NAME).read_text()


def _status(repo: Path) -> list[str]:
    return sorted(line for line in _git(repo, "status", "--porcelain", "--untracked-files=all")
                  .splitlines())


def test_a_check_that_writes_uv_lock_and_a_venv_into_the_clone_leaves_neither(tmp_path):
    repo, base = _lane(tmp_path)
    before = _status(repo)
    env = {**_env(tmp_path), prepublish.CLONE_BASE_ENV: base}
    record = _gate(repo, tmp_path, env).run()
    calls = _uv_calls(tmp_path)
    # The checks did run uv, from the clone: the control that the stand-in
    # had its chance to write.
    assert [Path(c["cwd"]).resolve() for c in calls] == [repo.resolve()] * 2, calls
    # MUTATION: dropping the restore after the checks (or recording the tree
    # AFTER them) turns this red: uv.lock and .venv/ are left for the
    # publish's `git add --all`.
    assert not (repo / "uv.lock").exists()
    assert not (repo / ".venv").exists()
    # MUTATION: restoring only untracked paths turns this red: tracked.txt
    # still says "a check was here", and gone.txt stays deleted.
    assert (repo / "tracked.txt").read_text() == "as committed\n"
    assert (repo / "gone.txt").read_text() == "a tracked file a check deletes\n"
    # MUTATION: restoring every changed path to HEAD (`git checkout .` /
    # `git clean`) instead of to the RECORDED tree turns this red: the agent's
    # README edit and untracked file would be lost.
    assert (repo / "README.md").read_text() == "the agent's README\n"
    assert (repo / "notes" / "agent.py").read_text() == "y = 'the agent wrote this'\n"
    assert _status(repo) == before
    # MUTATION: not recording what was restored turns this red.
    assert record["restored"] == [".venv/bin/python", "README.md", "gone.txt",
                                  "notes/agent.py", "tracked.txt", "uv.lock"]
    advisory = _advisory(tmp_path)
    # MUTATION: dropping the advisory line turns this red.
    assert "### Side effects of the checks" in advisory, advisory
    line = next(x for x in advisory.splitlines() if x.startswith("The checks changed"))
    for name in ("uv.lock", ".venv/bin/python", "tracked.txt", "README.md"):
        assert f"`{name}`" in line, line


def test_every_uv_a_check_runs_gets_a_project_environment_outside_the_clone(tmp_path):
    repo, base = _lane(tmp_path)
    env = {**_env(tmp_path, UV_QUIET="1"), prepublish.CLONE_BASE_ENV: base}
    gate_env = dict(env)
    _gate(repo, tmp_path, env).run()
    calls = _uv_calls(tmp_path)
    assert len(calls) == 2, calls
    for call in calls:
        where = call["project_env"]
        # MUTATION: not setting UV_PROJECT_ENVIRONMENT for uv turns this red.
        assert where, call
        # MUTATION: pointing it inside the checkout (`<project>/.venv`, or a
        # directory under the repository) turns this red.
        assert not Path(where).resolve().is_relative_to(repo.resolve()), where
        # MUTATION: keeping the temporary root after the checks turns this red.
        assert not Path(where).exists(), where
    # MUTATION: writing UV_PROJECT_ENVIRONMENT into the gate's own environment
    # (the agent's, continued after the gate) turns this red.
    assert env == gate_env and "UV_PROJECT_ENVIRONMENT" not in env
    # Nothing to restore: no record of side effects, no advisory line (the
    # control for the test above).
    assert "### Side effects of the checks" not in _advisory(tmp_path)


class Recorder:
    """A `prepublish.Runner` for ai-studio's shape: records each call's argv,
    cwd and the environment it was given; every run passes."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str],
                 timeout: float) -> Ran:
        self.calls.append((list(argv), Path(cwd), dict(env)))
        if argv[:2] == ["bash", "scripts/changed-guards.sh"]:
            return Ran(0, "")
        return Ran(0, "1 passed in 0.01s\n")


def test_each_project_directory_gets_its_own_environment_and_other_tools_get_none(tmp_path):
    repo = tmp_path / "repo"
    for path in ("api/pyproject.toml", "api/tests/test_a.py", "runner/pyproject.toml",
                 "runner/tests/test_b.py", "scripts/changed-guards.sh"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("\n")
    tests = ["api/tests/test_a.py", "runner/tests/test_b.py"]
    checks = [prepublish.Check("graph-pytest", tuple(tests), "uv run pytest",
                               run=prepublish._graph_pytest(tests, "base"))]
    runner = Recorder()
    env = {"PATH": "/usr/bin"}
    (outcome,) = prepublish.run_checks(repo, checks, env=env, runner=runner)
    assert outcome.status == prepublish.PASSED
    uv = {cwd.name: e for argv, cwd, e in runner.calls if argv[0] == "uv"}
    assert sorted(uv) == ["api", "runner"]
    homes = {name: e.get("UV_PROJECT_ENVIRONMENT") for name, e in uv.items()}
    # MUTATION: one environment shared by every project turns this red: uv
    # would re-sync it between api's dependencies and runner's.
    assert homes["api"] and homes["runner"] and homes["api"] != homes["runner"], homes
    for home in homes.values():
        # MUTATION: `<project>/.venv` (uv's default) turns this red.
        assert not Path(home).resolve().is_relative_to(repo.resolve()), home
    # MUTATION: setting it for every command (the guards' bash, git) turns
    # this red: only uv is pointed at the project environment.
    others = [e for argv, _cwd, e in runner.calls if argv[0] != "uv"]
    assert others and all("UV_PROJECT_ENVIRONMENT" not in e for e in others)
    assert env == {"PATH": "/usr/bin"}


_HOST = {**os.environ, **_IDENT}


def test_a_check_restoring_nothing_reports_nothing_and_an_unwritten_tree_is_left_alone(
        tmp_path):
    repo, _base = _lane(tmp_path)
    state = prepublish.record_tree(repo, runner=prepublish.run_command, env=_HOST)
    assert state is not None
    # MUTATION: treating every recorded (agent-changed) path as a side effect
    # turns this red: README.md and notes/agent.py would be "restored".
    assert prepublish.restore_tree(repo, state, runner=prepublish.run_command,
                                   env=_HOST) == ([], [])
    # The agent deleting a tracked file is its change, not a check's: a later
    # check recreating it is undone.
    _git(repo, "rm", "-q", "tracked.txt")
    state = prepublish.record_tree(repo, runner=prepublish.run_command, env=_HOST)
    (repo / "tracked.txt").write_text("recreated by a check\n")
    (repo / "pkg" / "__pycache__").mkdir(parents=True)
    (repo / "pkg" / "__pycache__" / "m.pyc").write_bytes(b"\0\1")
    restored, failed = prepublish.restore_tree(repo, state, runner=prepublish.run_command,
                                               env=_HOST)
    # MUTATION: restoring a recorded-as-deleted path to HEAD turns this red.
    assert not (repo / "tracked.txt").exists()
    # MUTATION: leaving the emptied directories a removed file sat in turns this red.
    assert not (repo / "pkg").exists()
    assert (restored, failed) == (["pkg/__pycache__/m.pyc", "tracked.txt"], [])
