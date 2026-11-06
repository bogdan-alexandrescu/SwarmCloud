"""An index run's history deepen starts no background git maintenance (#2922).

`gitops.deepen_history` runs two shallow fetches back to back over one clone:
`--shallow-since=<window>`, then `--deepen=1`. git 2.56 (CI's runner) ends
every fetch with a detached `git maintenance run --auto`, whose repack can
hold or rewrite `.git/shallow` while the next fetch deepens it ("shallow file
has changed since we read it", "Unable to create '.git/shallow.lock'"), the
race #2922 measured in `fetch_pull_base`. Pinned here, with real git against
a `file://` forge: both of the deepen's fetches run, and GIT_TRACE shows
neither starting `maintenance run --auto` nor `gc --auto`.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops
from agent_worker.logs import build_logger

DAY = 86_400
#: The head's date; the forge's commits are one a day before it.
HEAD_TIME = 1_790_000_000
COMMITS = 30
#: Inside the forge's history, so the window's fetch leaves the clone
#: shallow and the parent fetch runs too.
WINDOW_DAYS = 10

#: A git that starts automatic maintenance after a fetch, as `GIT_TRACE`
#: writes it: `maintenance run --auto` (git 2.29 on) or `gc --auto` before.
_AUTO_MAINTENANCE = re.compile(r"run_command: git (maintenance run|gc) --auto")


def _logger():
    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id="tenant-a",
                        generation=1, runner_profile="indexer", stream=io.StringIO())


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                                          "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()


def _forge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A bare `file://` forge whose `main` holds a commit a day for COMMITS days."""
    seed = tmp_path / "seed.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(seed))
    stream: list[bytes] = []
    for mark in range(1, COMMITS + 1):
        message = f"commit {mark}".encode()
        content = f"line {mark}\n".encode()
        when = HEAD_TIME - (COMMITS - mark) * DAY
        stream += [f"commit refs/heads/main\nmark :{mark}\n".encode(),
                   f"committer T <t@example.com> {when} +0000\n".encode(),
                   f"data {len(message)}\n".encode(), message, b"\n"]
        if mark > 1:
            stream.append(f"from :{mark - 1}\n".encode())
        stream += [f"M 644 inline file.txt\ndata {len(content)}\n".encode(), content, b"\n\n"]
    subprocess.run(["git", "fast-import", "--quiet"], cwd=str(seed), input=b"".join(stream),
                   check=True, capture_output=True)
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(gitops, "validate_repository_url",
                        lambda url: url if url.startswith(allowed) else real(url))
    return f"file://{seed}"


def test_the_history_deepen_starts_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop `maintenance.auto=false` from `history_fetch_argv` or
    `history_parent_argv` and the trace shows `git maintenance run --auto`
    after that fetch."""
    url = _forge(tmp_path, monkeypatch)
    (tmp_path / "logs").mkdir()
    clone = gitops.shallow_clone(
        url=url, ref="main", destination=tmp_path / "repo", private_dir=tmp_path / "private",
        logs_dir=tmp_path / "logs", timeout_seconds=60, logger=_logger(),
    )
    repo = clone.path
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"

    env, config_args, _cred = gitops._clone_env(url, None, tmp_path / "private", _logger())
    trace = tmp_path / "git-trace.log"
    labels: list[str] = []
    run_steps = gitops._run_git_steps

    def traced(steps, **kwargs):
        labels.append(str(kwargs.get("label", "")))
        kwargs["env"] = {**kwargs["env"], "GIT_TRACE": str(trace)}
        return run_steps(steps, **kwargs)

    monkeypatch.setattr(gitops, "_run_git_steps", traced)
    gitops.deepen_history(
        repo, WINDOW_DAYS, git_binary="git", config_args=config_args, url=url, token=None,
        private_dir=tmp_path / "private", env=env, logs_dir=tmp_path / "logs",
        timeout_seconds=60, logger=_logger(),
    )

    # Both fetches ran: the window's, then one parent past it.
    assert labels == ["git-history", "git-history-parent"]
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == WINDOW_DAYS + 2
    started = trace.read_text()
    assert started.count("built-in: git fetch") >= 2  # the trace saw both fetches
    assert not _AUTO_MAINTENANCE.findall(started)


def test_both_history_fetches_turn_automatic_maintenance_off() -> None:
    """The argv itself, for a git too old to trace a detached maintenance."""
    destination = Path("/tmp/repo")
    for argv in (
        gitops.history_fetch_argv("git", ["-c", "x=y"], destination, "a" * 40, HEAD_TIME, 90),
        gitops.history_parent_argv("git", ["-c", "x=y"], destination, "a" * 40),
    ):
        settings = argv[:argv.index("-C")]
        assert settings[:3] == ["git", "-c", "x=y"]           # the clone's config first
        assert "maintenance.auto=false" in settings and "gc.auto=0" in settings
        assert argv[argv.index("-C") + 2] == "fetch"
