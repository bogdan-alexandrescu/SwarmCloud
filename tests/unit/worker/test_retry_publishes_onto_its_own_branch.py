"""A retry publishes onto its task's branch when only its earlier attempt wrote it (#1438).

MEASURED 2026-10-16 (R10, `task_ddfff9b902a44333b294`): attempt 1 pushed
`swarm/task_<id>` and crashed before its terminal write; the reclaimed task's
attempts 2 and 3 published from a fresh publish repository at the same clone
base, and each push was refused -- "Updates were rejected because the remote
contains work that you do not have locally" -- by attempt 1's own commit. The
task failed with its work on the forge and no pull request.

The property, against a real bare repository on disk (only HTTP to the forge
is faked, as in test_strategy_end_to_end.py):

  * attempt 2 of the same task replaces the branch with its own history,
    by `--force-with-lease` to the tip it verified, and opens the pull
    request from it exactly as a first-attempt push would have;
  * a branch holding a commit someone else wrote, or one the worker made for
    ANOTHER task, is left exactly as it was and the attempt fails as before;
  * a branch that moved after the check is refused by the lease;
  * a branch that is not `<prefix><this task id>` is never read or written.

WRITTEN TO FAIL BEFORE THE CHANGE: there, attempt 2's push was refused and
`published` was False.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops, lifecycle

from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_fence_holds,
    _attempt,
    _the_agent_titles_its_pull_request,
    assert_only_the_worker_wrote,
    forge,
    local_urls,
    origin,
    pushed_commits,
    refs,
    tree_at,
)

DIRECT = {"strategy": "direct-pr", "carrier": "checkpoints"}
TASK = "task_retry0001"


def _publish(worker_factory, monkeypatch, origin: Path, *, attempt: int, files: dict[str, str]):
    worker, config, task = _attempt(
        worker_factory, monkeypatch, origin, task_id=TASK, attempt=attempt, dispatch=DIRECT
    )
    assert worker._maybe_clone(task)["commit"], "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    for name, text in files.items():
        (repo / name).write_text(text)
    return worker, config, worker._harvest_git(publish=True)


def _push_onto(origin: Path, tmp: Path, branch: str, *, name: str, email: str, message: str) -> str:
    """Add one commit to `branch` on the remote, as `name <email>`: a writer
    other than this attempt."""
    clone = tmp / f"writer-{len(list(tmp.glob('writer-*')))}"
    subprocess.run(["git", "clone", "--quiet", "--branch", branch, str(origin), str(clone)],
                   check=True, capture_output=True)
    (clone / "other.txt").write_text("someone else's work\n")
    # In the environment, which outranks any `user.*` an ambient config or
    # environment of the machine running the test would otherwise supply.
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    subprocess.run(["git", "add", "other.txt"], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=clone, check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "push", "--quiet", "origin", f"HEAD:{branch}"], cwd=clone, check=True,
                   capture_output=True)
    return refs(origin)[branch]


@pytest.fixture
def pushes(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    real = lifecycle.push_branch

    def spy(**kwargs: Any) -> str:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "push_branch", spy)
    return calls


def test_a_retry_replaces_its_own_first_attempts_branch_and_opens_the_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, log_stream
):
    _, config, first = _publish(worker_factory, monkeypatch, origin, attempt=1,
                                files={"first.txt": "attempt 1\n"})
    branch = f"{config.git_branch_prefix}{TASK}"
    assert first["published"] is True, first.get("publish_reason")
    first_tip = refs(origin)[branch]

    second_worker, config, second = _publish(worker_factory, monkeypatch, origin, attempt=2,
                                             files={"second.txt": "attempt 2\n"})

    assert second["published"] is True, second.get("publish_reason")
    assert not second.get(lifecycle.PUBLISH_FAILED_FIELD)
    landed = refs(origin)[branch]
    assert landed != first_tip
    assert second["pushed_head"] == landed
    # The branch a first-attempt push of THIS attempt would have left: its
    # own work over the clone base, and nothing of attempt 1's tree.
    assert "second.txt" in tree_at(origin, branch)
    assert "first.txt" not in tree_at(origin, branch)
    commits = pushed_commits(origin, branch)
    assert len(commits) == 1 and commits[0]["parents"] == [refs(origin)["main"]]
    assert_only_the_worker_wrote(commits, config)
    assert second_worker._pushed_branch["head"] == landed
    # The pull request step saw the branch.
    assert forge.pulls[-1]["head"] == branch and second["pull_request"]["number"] == 2
    # Never a bare force: the first push of each attempt carries no lease, the
    # retry's one push carries exactly the tip it verified.
    assert [call.get("lease") for call in pushes] == [None, None, first_tip]
    assert "force-with-lease" in log_stream.getvalue()


def test_a_commit_someone_else_wrote_on_the_branch_fails_the_retry_and_is_kept(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path
):
    _, config, first = _publish(worker_factory, monkeypatch, origin, attempt=1,
                                files={"first.txt": "attempt 1\n"})
    branch = f"{config.git_branch_prefix}{TASK}"
    foreign = _push_onto(origin, tmp_path, branch, name="a person",
                         email="person@example.invalid", message="my own change")

    _, _, second = _publish(worker_factory, monkeypatch, origin, attempt=2,
                            files={"second.txt": "attempt 2\n"})

    assert second["published"] is False
    assert second[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert "rejected" in second["publish_reason"]
    assert refs(origin)[branch] == foreign, "the branch someone else wrote was changed"
    assert len(forge.pulls) == 1, "a pull request was opened from a refused push"
    assert [call.get("lease") for call in pushes] == [None, None]


def test_a_worker_commit_made_for_another_task_fails_the_retry(
    worker_factory, monkeypatch, origin, local_urls, forge, pushes, tmp_path
):
    """The worker's identity alone is not this task's: the bot writes for
    every task, and a fix step continues another task's branch (#263)."""
    _, config, _ = _publish(worker_factory, monkeypatch, origin, attempt=1,
                            files={"first.txt": "attempt 1\n"})
    branch = f"{config.git_branch_prefix}{TASK}"
    other = _push_onto(
        origin, tmp_path, branch, name=config.git_author_name, email=config.git_author_email,
        message="a fix\n\nMade by the worker for task task_someone_else.",
    )

    _, _, second = _publish(worker_factory, monkeypatch, origin, attempt=2,
                            files={"second.txt": "attempt 2\n"})

    assert second["published"] is False
    assert refs(origin)[branch] == other
    assert [call.get("lease") for call in pushes] == [None, None]


def test_a_branch_that_moved_after_the_check_is_refused_by_the_lease(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, config, _ = _publish(worker_factory, monkeypatch, origin, attempt=1,
                            files={"first.txt": "attempt 1\n"})
    branch = f"{config.git_branch_prefix}{TASK}"
    first_tip = refs(origin)[branch]
    # The check passed against a tip the branch no longer holds.
    stale = refs(origin)["main"]
    monkeypatch.setattr(lifecycle, "own_branch_tip", lambda **_kwargs: (stale, ""))

    _, _, second = _publish(worker_factory, monkeypatch, origin, attempt=2,
                            files={"second.txt": "attempt 2\n"})

    assert second["published"] is False
    assert second[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert refs(origin)[branch] == first_tip


def test_a_branch_that_is_not_this_tasks_own_is_never_read(tmp_path):
    tip, why = gitops.own_branch_tip(
        repo=tmp_path / "no-such-repository", url="https://github.com/acme/widgets.git",
        branch="swarm/task_other", token="unused", task_id=TASK, base="0" * 40,
        author_name="w", author_email="w@example.invalid", private_dir=tmp_path / "p",
        logs_dir=tmp_path / "l", timeout_seconds=5, logger=None,
    )
    assert tip is None and "not this task's own branch" in why


def test_a_leased_push_needs_the_full_sha_it_replaces(tmp_path):
    with pytest.raises(gitops.GitError, match="full sha"):
        gitops.push_branch(
            repo=tmp_path, url="https://github.com/acme/widgets.git", branch="swarm/task_x",
            token="unused", private_dir=tmp_path / "p", logs_dir=tmp_path / "l",
            timeout_seconds=5, logger=None, lease="abc123",
        )
