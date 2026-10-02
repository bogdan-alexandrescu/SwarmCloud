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

import os
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


def _run(repo: Path, *args: str, clone_base: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the module. `SWARM_CLONE_BASE` is removed from the environment the
    test itself runs in, so only a test that asks for one sets it."""
    env = {k: v for k, v in os.environ.items() if k != publish_scan.CLONE_BASE_ENV}
    if clone_base is not None:
        env[publish_scan.CLONE_BASE_ENV] = clone_base
    return subprocess.run(
        [sys.executable, "-m", "agent_worker.publish_scan", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
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


def test_with_no_known_base_a_committed_secret_is_never_reported_clean(repo: Path):
    """MAJOR 1 of #470's review: HEAD was the fallback base, so a secret the
    agent had COMMITTED was in no diff at all and the scan said clean. With
    no clone base, no shallow boundary and no origin/main, nothing can be
    scanned honestly: exit 2, never 0, and never a silent HEAD."""
    value = _password()
    (repo / "src" / "app.py").write_text(f'{_PW} = "{value}"\n')
    _git(repo, "commit", "-q", "-am", "committed")
    done = _run(repo)
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
    assert "could not determine the clone base; nothing was scanned" in done.stderr
    assert done.stdout == ""
    assert value not in done.stderr
    with pytest.raises(publish_scan.ScanError):
        publish_scan.default_base(repo, environ={})


def _two_commit_origin(tmp_path: Path) -> Path:
    """A repository with a `feature` branch two commits deep, to clone from."""
    origin = tmp_path / "upstream"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "scan@example.invalid")
    _git(origin, "config", "user.name", "scan")
    (origin / "app.py").write_text("X = 1\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "one")
    _git(origin, "checkout", "-q", "-b", "feature")
    (origin / "app.py").write_text("X = 2\n")
    _git(origin, "commit", "-q", "-am", "two")
    return origin


def _worker_clone(origin: Path, tmp_path: Path, branch: str) -> Path:
    """The worker's clone shape (gitops.shallow_clone): `--depth 1
    --single-branch` of one ref, so there is no origin/main to fall back on."""
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--single-branch", "--branch", branch,
         origin.resolve().as_uri(), str(clone)],
        check=True, capture_output=True,
    )
    _git(clone, "config", "user.email", "scan@example.invalid")
    _git(clone, "config", "user.name", "scan")
    return clone


def test_a_shallow_worker_clone_of_a_non_main_ref_scans_from_its_boundary(tmp_path: Path):
    """A non-main task's clone has only origin/<ref>. The base is the shallow
    boundary -- the commit the clone landed on -- so a committed secret is in
    the diff."""
    clone = _worker_clone(_two_commit_origin(tmp_path), tmp_path, "feature")
    landed = _git(clone, "rev-parse", "HEAD").strip()
    assert _git(clone, "rev-parse", "--is-shallow-repository").strip() == "true"
    value = _password()
    (clone / "app.py").write_text(f'{_PW} = "{value}"\n')
    _git(clone, "commit", "-q", "-am", "agent work")
    done = _run(clone)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert done.stdout.splitlines() == ["app.py:1 key_value_assignment"]
    assert landed[:12] in done.stderr
    assert value not in done.stdout + done.stderr


def test_the_workers_exported_clone_base_wins(tmp_path: Path):
    """The worker exports the base its publish diffs from (`SWARM_CLONE_BASE`);
    it is used before any guess, shallow or not."""
    clone = _worker_clone(_two_commit_origin(tmp_path), tmp_path, "feature")
    landed = _git(clone, "rev-parse", "HEAD").strip()
    _git(clone, "fetch", "-q", "--unshallow")  # no boundary left to find
    (clone / "app.py").write_text(f'{_PW} = "{_password()}"\n')
    _git(clone, "commit", "-q", "-am", "agent work")
    assert _run(clone).returncode == 2
    done = _run(clone, clone_base=landed)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert done.stdout.splitlines() == ["app.py:1 key_value_assignment"]


def test_an_exported_clone_base_that_names_no_commit_is_an_error(repo: Path):
    """Never a fallback to some other base: the worker said which one."""
    done = _run(repo, clone_base="0" * 40)
    assert done.returncode == 2
    assert done.stdout == ""


def test_an_empty_repository_clone_scans_against_the_empty_tree(tmp_path: Path):
    """`SWARM_CLONE_BASE=empty` (`gitops.EMPTY_CLONE_BASE`): the repository had
    no commit when cloned, so everything the agent committed is added."""
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "scan@example.invalid")
    _git(root, "config", "user.name", "scan")
    (root / "app.py").write_text(f'{_PW} = "{_password()}"\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    done = _run(root, clone_base="empty")
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert done.stdout.splitlines() == ["app.py:1 key_value_assignment"]


def test_the_worker_exports_its_clone_base_to_the_agent(db, worker_factory, tmp_path: Path):
    """The value the CLI prefers is set by the worker, from the base its own
    publish diffs from (`_publish_base`), in the agent's environment."""
    from conftest import seed_attempt

    from agent_worker import workspace as workspace_mod

    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    assert publish_scan.CLONE_BASE_ENV not in worker._build_child_env()
    worker._publish_base = "a" * 40
    worker._clone_base = "a" * 40
    assert worker._build_child_env()[publish_scan.CLONE_BASE_ENV] == "a" * 40


def test_an_unknown_base_is_an_error_not_a_clean_scan(repo: Path):
    done = _run(repo, "--base", "no-such-rev")
    assert done.returncode == 2
    assert done.stdout == ""
