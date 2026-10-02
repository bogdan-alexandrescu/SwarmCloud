"""`python -m agent_worker.publish_scan`: the worker's publish scan, runnable (2026-10-02).

WHAT WENT WRONG. Three of six SwarmCloud lanes did all their work and were
refused at publish by the worker's credential scan, which nobody could run
before the worker did. The module runs the SAME scan -- `_DiffLeakScanner`
over the same `-U0` diff, asking `_credential_in` with each file's path, so
the same tiers -- on what the working tree adds against a base, and names
`path:line rule` for every hit, never the value.

Real git in a temporary repository; nothing touches the network. Every
credential-shaped value is assembled at runtime.
"""

from __future__ import annotations

import random
import shutil
import string
import subprocess
import sys
from pathlib import Path

import pytest

from agent_worker import publish_scan

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_PW = "pass" + "word"


def _password() -> str:
    """A generic secret `_looks_like_a_credential` accepts: 24 distinct characters."""
    return "".join(random.Random(11).sample(string.ascii_letters + string.digits, 24))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "scan@example.invalid")
    _git(root, "config", "user.name", "scan")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_worker.publish_scan", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_clean_diff_exits_zero(repo: Path):
    (repo / "src" / "app.py").write_text("def main():\n    return 2\n")
    (repo / "src" / "new.py").write_text("VALUE = 3\n")
    done = _run(repo)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


def test_a_planted_generic_secret_is_named_by_path_line_and_rule_and_never_by_value(repo: Path):
    value = _password()
    (repo / "src" / "app.py").write_text(f'def main():\n    {_PW} = "{value}"\n    return 1\n')
    done = _run(repo)
    assert done.returncode == 1
    assert done.stdout.splitlines() == ["src/app.py:2 key_value_assignment"]
    assert value not in done.stdout + done.stderr


def test_every_hit_is_listed_including_an_untracked_file(repo: Path):
    """Not only the first: the lane fixes them all in one pass. A file the
    agent has not `git add`ed yet is what the worker will commit, so it counts."""
    value = _password()
    (repo / "src" / "app.py").write_text(
        f'def main():\n    {_PW} = "{value}"\n    x = 1\n    {_PW} = "{value}"\n'
    )
    (repo / "deploy").mkdir()
    (repo / "deploy" / "settings.json").write_text(f'{{\n  "{_PW}": "{value}"\n}}\n')
    done = _run(repo)
    assert done.returncode == 1
    assert sorted(done.stdout.splitlines()) == [
        "deploy/settings.json:2 key_value_assignment",
        "src/app.py:2 key_value_assignment",
        "src/app.py:4 key_value_assignment",
    ]
    assert value not in done.stdout + done.stderr


def test_the_tiers_are_the_workers(repo: Path):
    """A low-entropy fixture under tests/ passes (tier 3); a reference passes
    outside tests (tier 2's reference rule); the same fixture in src is refused."""
    fixture = f'login({_PW}="hunter2")\n'
    (repo / "tests").mkdir()
    (repo / "tests" / "test_login.py").write_text(fixture)
    (repo / "infra.tf").write_text('locals {\n  ANTHROPIC_API_' + 'KEY = "anthropic"\n}\n')
    assert _run(repo).returncode == 0
    (repo / "src" / "login.py").write_text(fixture)
    done = _run(repo)
    assert done.returncode == 1
    assert done.stdout.splitlines() == ["src/login.py:1 key_value_assignment"]


def test_a_removed_line_is_not_a_hit(repo: Path):
    """Only what the diff ADDS: a line already in the base is not this change's."""
    value = _password()
    (repo / "src" / "old.py").write_text(f'{_PW} = "{value}"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "history")
    (repo / "src" / "old.py").write_text("VALUE = 1\n")
    assert _run(repo).returncode == 0


def test_base_defaults_to_the_merge_base_with_origin_main(repo: Path, tmp_path: Path):
    """A secret committed on the branch is still in what the push would add."""
    origin = tmp_path / "origin.git"
    _git(repo, "clone", "-q", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "fetch", "-q", "origin")
    (repo / "src" / "app.py").write_text(f'{_PW} = "{_password()}"\n')
    _git(repo, "commit", "-q", "-am", "branch work")
    done = _run(repo)
    assert done.returncode == 1
    assert done.stdout.splitlines() == ["src/app.py:1 key_value_assignment"]
    # Against HEAD the committed line is not added again.
    assert _run(repo, "--base", "HEAD").returncode == 0


def test_without_origin_the_base_is_head(repo: Path):
    (repo / "src" / "app.py").write_text(f'{_PW} = "{_password()}"\n')
    _git(repo, "commit", "-q", "-am", "committed")
    assert _run(repo).returncode == 0
    assert publish_scan.default_base(repo) == _git(repo, "rev-parse", "HEAD").strip()


def test_an_unknown_base_is_an_error_not_a_clean_scan(repo: Path):
    done = _run(repo, "--base", "no-such-rev")
    assert done.returncode == 2
    assert done.stdout == ""
