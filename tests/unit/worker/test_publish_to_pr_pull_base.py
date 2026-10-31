"""A `publish_to_pr` clone carries its pull request's base branch (#2901).

2026-10-31: two `publish_to_pr` tasks asked to "merge main and resolve
conflicts" found a one-commit clone of the pull request's head, a platform
fetch of `main` that answered `too_large` (its depth-1 haves hide every
ancestor, so git sends the base's whole history) and a `git fetch` with no
credential; both published nothing.

Pinned here, with real git against a `file://` forge whose pull request
branch is behind its base: a clone given `pull_base` holds `origin/<base>`
and a merge base with HEAD, deepened in rounds and still shallow (never the
whole history); a clone without it is one commit, as before; the byte budget
and the round bound stop a runaway deepen with a clear error, which the
clone records rather than failing on; unrelated histories say so.
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops
from agent_worker.logs import build_logger

#: Commits on the shared line, then on the pull request's branch, then on
#: the base after the branch left it. BEHIND > PULL_BASE_FIRST_DEPTH, so the
#: first round cannot reach the merge base and a second one must.
SHARED = 300
AHEAD = 3
BEHIND = 100


def _logger():
    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id="tenant-a",
                        generation=1, runner_profile="mock", stream=io.StringIO())


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                                          "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()


def _commit(stream: list[bytes], ref: str, mark: int, parent: int | None, path: str,
            content: bytes) -> None:
    message = f"commit {mark}".encode()
    stream += [f"commit {ref}\nmark :{mark}\n".encode(),
               f"committer T <t@example.com> {1_790_000_000 + mark} +0000\n".encode(),
               f"data {len(message)}\n".encode(), message, b"\n"]
    if parent is not None:
        stream.append(f"from :{parent}\n".encode())
    stream += [f"M 644 inline {path}\ndata {len(content)}\n".encode(), content, b"\n\n"]


def _forge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, blob_bytes: int = 0,
           unrelated: bool = False) -> tuple[str, str]:
    """A bare `file://` forge: `main`, and `feature` behind it. Returns the
    url and the merge base's sha (the last shared commit)."""
    seed = tmp_path / "seed.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(seed))
    stream: list[bytes] = []
    mark = 0
    for index in range(SHARED):
        mark += 1
        _commit(stream, "refs/heads/main", mark, mark - 1 or None, "shared.txt",
                f"shared {index}\n".encode())
    fork = mark
    parent: int | None = None if unrelated else fork
    for index in range(AHEAD):
        mark += 1
        _commit(stream, "refs/heads/feature", mark, parent, "feature.txt",
                f"feature {index}\n".encode())
        parent = mark
    parent = fork
    for index in range(BEHIND):
        mark += 1
        content = os.urandom(blob_bytes) if blob_bytes else f"main {index}\n".encode()
        _commit(stream, "refs/heads/main", mark, parent, f"main-{index}.bin", content)
        parent = mark
    stream.append(f"get-mark :{fork}\n".encode())
    done = subprocess.run(["git", "fast-import", "--quiet", "--cat-blob-fd=1"], cwd=str(seed),
                          input=b"".join(stream), check=True, capture_output=True)
    fork_sha = done.stdout.decode().split()[-1]
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(gitops, "validate_repository_url",
                        lambda url: url if url.startswith(allowed) else real(url))
    return f"file://{seed}", fork_sha


def _clone(tmp_path: Path, url: str, **kwargs) -> gitops.CloneResult:
    (tmp_path / "logs").mkdir(exist_ok=True)       # the workspace makes it
    return gitops.shallow_clone(
        url=url, ref="feature", destination=tmp_path / "repo", private_dir=tmp_path / "private",
        logs_dir=tmp_path / "logs", timeout_seconds=60, logger=_logger(), **kwargs,
    )


def test_a_publish_to_pr_clone_holds_its_base_and_a_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop the `pull_base` branch from `shallow_clone` and there
    is no origin/main; stop after one round and the merge base is missing."""
    url, fork = _forge(tmp_path, monkeypatch)
    result = _clone(tmp_path, url, pull_base="main")
    repo = tmp_path / "repo"

    assert result.base is not None
    assert result.base["merge_base"] == fork
    assert result.base["remote_ref"] == "origin/main"
    assert result.base["rounds"] == 2                 # 64 is short of 100; 128 reaches
    assert "origin/main" in result.base["note"] and fork in result.base["note"]
    assert _git(repo, "merge-base", "HEAD", "origin/main") == fork
    assert _git(repo, "rev-parse", "HEAD") == _git(Path(url[7:]), "rev-parse", "feature")
    # Deep enough, not the whole history: the clone is still shallow.
    assert (repo / ".git" / "shallow").exists()
    assert int(_git(repo, "rev-list", "--count", "origin/main")) < SHARED + BEHIND
    # And the merge is now possible: no conflict in this fixture.
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "merge", "--quiet",
         "--no-edit", "origin/main")
    # The credential file, had there been one, is gone with the clone's.
    assert not (tmp_path / "private" / ".git-credentials").exists()


def test_a_clone_without_a_pull_base_is_still_one_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _fork = _forge(tmp_path, monkeypatch)
    result = _clone(tmp_path, url)
    repo = tmp_path / "repo"

    assert result.base is None
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert subprocess.run(["git", "rev-parse", "--verify", "--quiet", "origin/main"],
                          cwd=str(repo), capture_output=True).returncode != 0


def test_the_byte_budget_stops_a_runaway_deepen_with_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop the budget check and the deepen runs on to the merge
    base, so `PullBaseError` is never raised."""
    url, _fork = _forge(tmp_path, monkeypatch, blob_bytes=16 * 1024)
    clone = _clone(tmp_path, url)
    env, config_args, _cred = gitops._clone_env(url, None, tmp_path / "private", _logger())

    with pytest.raises(gitops.PullBaseError) as raised:
        gitops.fetch_pull_base(
            clone.path, "main", git_binary="git", config_args=config_args, url=url,
            token=None, private_dir=tmp_path / "private", env=env, logs_dir=tmp_path / "logs",
            timeout_seconds=60, logger=_logger(), max_bytes=256 * 1024,
        )
    message = str(raised.value)
    assert "262144-byte budget" in message and "without reaching a merge base" in message
    assert "1 rounds" in message                     # stopped after the round that passed it


def test_a_clone_records_a_base_over_budget_and_still_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _fork = _forge(tmp_path, monkeypatch, blob_bytes=16 * 1024)
    monkeypatch.setattr(gitops, "PULL_BASE_MAX_BYTES", 256 * 1024)
    result = _clone(tmp_path, url, pull_base="main")

    assert result.commit is not None
    assert result.base is not None and result.base["merge_base"] is None
    assert "budget" in result.base["error"]
    assert result.base["note"].startswith("The pull request's base branch main could not")


def test_the_round_bound_stops_the_deepen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _fork = _forge(tmp_path, monkeypatch)
    monkeypatch.setattr(gitops, "PULL_BASE_MAX_ROUNDS", 1)
    result = _clone(tmp_path, url, pull_base="main")

    assert result.base is not None and result.base["merge_base"] is None
    assert "in 1 rounds without reaching a merge base" in result.base["error"]


def test_unrelated_histories_say_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url, _fork = _forge(tmp_path, monkeypatch, unrelated=True)
    result = _clone(tmp_path, url, pull_base="main")

    assert result.base is not None and result.base["merge_base"] is None
    assert "unrelated" in result.base["error"]


@pytest.mark.parametrize("base", ["refs/heads/main", "-x", "0123456789abcdef0123456789abcdef01234567"])
def test_a_base_that_is_not_a_branch_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    url, _fork = _forge(tmp_path, monkeypatch)
    result = _clone(tmp_path, url, pull_base=base)

    assert result.base is not None and result.base["merge_base"] is None
    assert _git(tmp_path / "repo", "rev-list", "--count", "HEAD") == "1"
