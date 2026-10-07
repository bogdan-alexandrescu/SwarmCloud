"""Worker git prints no graft-deprecation hint, and grafts stay off (#808).

WHAT WENT WRONG. `_git_env` and `_UPLOAD_PACK_ENV` set
`GIT_GRAFT_FILE=/dev/null` so that `rev-list` and `merge-base` never follow a
`.git/info/grafts` the agent wrote (#259 re-review). git opens the graft file
-- which succeeds for /dev/null -- and prints its eight-line "Support for
<GIT_DIR>/info/grafts is deprecated" advice before it parses anything, so
every hardened worker git wrote that hint to stderr.

WHAT IS PINNED. Grafts stay off; `GIT_NO_REPLACE_OBJECTS` does not cover them.
In `_git_env` the graft file stays /dev/null and the hint is switched off with
`advice.graftFileDeprecated=false`, the key git's own hint names. An advice key
only decides whether a message prints, so a graft the agent wrote is still
ignored -- `test_grafts_are_still_ignored`. upload-pack never loads `advice.*`
(its config callback does not chain to git's default one), so no advice key
can reach it. `_UPLOAD_PACK_ENV` names a graft file that cannot open
(`/dev/null/no-grafts`, ENOTDIR), which git skips without a word. The upload
test plants a graft in the source to show the upload side still ignores it.
The control shows the `_git_env` command without the key does print the hint,
and main's `/dev/null` upload env printed it too, so these tests can fail.

Real git, in tmp_path; nothing leaves the machine.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_IDENT = ["-c", "user.name=Agent", "-c", "user.email=agent@example.invalid"]
_ADVICE_KEY = "advice.graftFileDeprecated"


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """A repository with two commits; returns it and the commit shas, oldest first."""
    path = tmp_path / "repo"
    path.mkdir()
    env = gitops._git_env(tmp_path / "setup-home")
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    shas = []
    for n in range(2):
        (path / f"file{n}.txt").write_text(f"content {n}\n")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
        subprocess.run(
            ["git", *_IDENT, "commit", "-q", "-m", f"commit {n}"], cwd=path, check=True, env=env
        )
        shas.append(
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=path, check=True, env=env,
                capture_output=True, text=True,
            ).stdout.strip()
        )
    return path, shas


def _private(tmp_path: Path) -> Path:
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    return private


def test_worker_git_env_prints_no_graft_hint(repo: tuple[Path, list[str]], tmp_path: Path) -> None:
    path, _ = repo
    result = _run(["git", "rev-list", "HEAD"], path, gitops._git_env(_private(tmp_path)))
    assert result.returncode == 0, result.stderr
    assert "graft" not in result.stderr
    assert "hint:" not in result.stderr


def test_hardened_upload_pack_prints_no_graft_hint(
    repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    path, shas = repo
    # A graft the upload side must not follow: it would leave the first
    # commit out of the pack, and the clone's connectivity check would fail.
    (path / ".git" / "info").mkdir(exist_ok=True)
    (path / ".git" / "info" / "grafts").write_text(f"{shas[1]}\n")
    # Built as `prepare_publish_repo` builds it, less the shallow file.
    upload_pack = " ".join(
        shlex.quote(part)
        for part in ["env", *gitops._UPLOAD_PACK_ENV, "git", *gitops._UPLOAD_PACK_CONFIG, "upload-pack"]
    )
    dest = tmp_path / "clone"
    result = _run(
        ["git", "clone", "--no-local", "--upload-pack", upload_pack, str(path), str(dest)],
        tmp_path,
        gitops._git_env(_private(tmp_path)),
    )
    assert result.returncode == 0, result.stderr
    assert "graft" not in result.stderr
    assert "hint:" not in result.stderr
    # The clone really went through upload-pack and arrived whole.
    head = _run(["git", "rev-parse", "HEAD"], dest, gitops._git_env(_private(tmp_path)))
    assert head.stdout.strip() == shas[1]
    count = _run(["git", "rev-list", "--count", "HEAD"], dest, gitops._git_env(_private(tmp_path)))
    assert count.stdout.strip() == "2"


def test_grafts_are_still_ignored(repo: tuple[Path, list[str]], tmp_path: Path) -> None:
    path, shas = repo
    # The agent grafts the second commit as a root.
    (path / ".git" / "info").mkdir(exist_ok=True)
    (path / ".git" / "info" / "grafts").write_text(f"{shas[1]}\n")

    result = _run(["git", "rev-list", "--count", "HEAD"], path, gitops._git_env(_private(tmp_path)))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"
    assert "graft" not in result.stderr
    assert "hint:" not in result.stderr

    # Control: without the worker's graft file, git does follow the graft,
    # so the '2' above is the hardening at work and not an unread file.
    followed = gitops._git_env(_private(tmp_path))
    del followed["GIT_GRAFT_FILE"]
    unhardened = _run(["git", "rev-list", "--count", "HEAD"], path, followed)
    assert unhardened.stdout.strip() == "1"


def test_without_the_advice_key_the_hint_prints(
    repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    env = gitops._git_env(_private(tmp_path))
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = [
        (env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"])
        for n in range(count)
        if env[f"GIT_CONFIG_KEY_{n}"] != _ADVICE_KEY
    ]
    assert len(pairs) == count - 1, "the worker env no longer carries the advice key"
    for n in range(count):
        del env[f"GIT_CONFIG_KEY_{n}"]
        del env[f"GIT_CONFIG_VALUE_{n}"]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for n, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{n}"] = key
        env[f"GIT_CONFIG_VALUE_{n}"] = value

    path, _ = repo
    result = _run(["git", "rev-list", "HEAD"], path, env)
    assert result.returncode == 0, result.stderr
    assert "graftFileDeprecated" in result.stderr
