"""The publish auto-commit keeps what the agent's commits removed from the index (#2536).

WHAT WENT WRONG. The fix agent of task_6e4f6aa4 removed a reviewed file from
the branch with `git rm --cached scratchpad/runbook-cf-step2/guards.log`,
which keeps the file on disk, and committed that (0ca3137). At publish,
`commit_dirty` ran `git add --all -- .` over the mirrored working tree, staged
the untracked file again and committed it: the worker's own commit undid the
agent's. The fix's net diff was empty, the pushed head stayed at the
implementer's commit, the step still ended SUCCEEDED, and the re-review found
the same blocker.

WHAT IS PINNED.

* `commit_dirty`, given the publish base, does not stage an untracked path
  that one of the agent's commits since that base deleted and HEAD does not
  hold. It reports the path as left unstaged; the publish lists it under
  `left_unstaged`.
* Everything else is staged as before: an untracked file no agent commit
  touched, and a deletion the agent left uncommitted.
* The backstop: a publish whose auto-commit returns the tree to the publish
  base's tree, when the agent's own commits had changed it, refuses to
  publish (`auto_commit_undid_agent_commits`, naming the paths) and the
  attempt ends FAILED with `publish_refused`, never SUCCEEDED.

Real git against a local `file://` remote; only the HTTP forge is faked.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from agent_worker.gitops import commit_dirty
from agent_worker.logs import build_logger
from swarm_common.states import TaskState

from worker_seeds import seed_attempt
from test_publish_commits_in_the_clean_repository import _attempt
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    forge,
    local_urls,
    origin,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

LOG = "scratchpad/x/guards.log"


def _git(repo: Path, *args: str) -> str:
    """git as the agent, with an identity of its own."""
    ident = ["-c", "user.name=Agent", "-c", "user.email=agent@example.invalid"]
    return subprocess.run(
        ["git", *ident, *args], cwd=str(repo), check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _logger():
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="u-test", generation=1,
        runner_profile="mock", stream=io.StringIO(),
    )


def _files(repo: Path, ref: str = "HEAD") -> set[str]:
    return set(_git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())


@pytest.fixture()
def clone(tmp_path: Path) -> tuple[Path, str]:
    """A repository whose base commit tracks the guards log, as main did."""
    path = tmp_path / "work" / "repo"
    path.mkdir(parents=True)
    _git(path, "init", "--quiet", "--initial-branch=main")
    (path / "README.md").write_text("one\n")
    (path / "scratchpad" / "x").mkdir(parents=True)
    (path / LOG).write_text("72 lines of guard output\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _commit_dirty(path: Path, tmp_path: Path, *, base: str | None, left: list[str]) -> str | None:
    private = tmp_path / "private"
    logs = tmp_path / "logs"
    private.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    return commit_dirty(
        repo=path, message="swarm: uncommitted changes", author_name="swarmcloud agent",
        author_email="a@example.com", private_dir=private, logs_dir=logs,
        timeout_seconds=60, logger=_logger(), base=base, left_unstaged=left,
    )


# -- gitops ---------------------------------------------------------------------


def test_a_file_the_agent_removed_with_rm_cached_is_not_republished(clone, tmp_path):
    """The incident, at the function that made it: the agent's commit removes
    the log from the index and leaves it on disk; the auto-commit must not put
    it back. MUTATION: drop the exclusion -- HEAD holds the log again."""
    path, base = clone
    _git(path, "rm", "--cached", "--quiet", LOG)
    _git(path, "commit", "--quiet", "-m", "Drop the scratchpad guards log")
    agent_head = _git(path, "rev-parse", "HEAD")
    assert (path / LOG).exists(), "the control: rm --cached keeps the file on disk"
    left: list[str] = []

    sha = _commit_dirty(path, tmp_path, base=base, left=left)

    assert sha is None, "nothing else was dirty, so the worker had nothing to commit"
    assert _git(path, "rev-parse", "HEAD") == agent_head
    assert LOG not in _files(path), "the auto-commit undid the agent's removal"
    assert left == [LOG]
    assert (path / LOG).exists(), "the worker deleted the agent's file from disk"


def test_an_untracked_file_no_agent_commit_touched_is_still_committed(clone, tmp_path):
    """The rest of the auto-commit is unchanged: a new file the agent never
    committed and a deletion it left uncommitted both reach HEAD, next to the
    removal it did commit, which stays out."""
    path, base = clone
    _git(path, "rm", "--cached", "--quiet", LOG)
    _git(path, "commit", "--quiet", "-m", "Drop the scratchpad guards log")
    (path / "new.txt").write_text("left uncommitted\n")
    (path / "README.md").unlink()
    left: list[str] = []

    sha = _commit_dirty(path, tmp_path, base=base, left=left)

    assert sha, "the uncommitted work was not committed"
    files = _files(path)
    assert "new.txt" in files
    assert "README.md" not in files, "an uncommitted deletion did not reach HEAD"
    assert LOG not in files
    assert left == [LOG]

    # The control: with no base, today's behaviour -- everything is staged.
    path2 = tmp_path / "again"
    _git(tmp_path, "clone", "--quiet", str(path), str(path2))
    (path2 / LOG).parent.mkdir(parents=True, exist_ok=True)
    (path2 / LOG).write_text("72 lines of guard output\n")
    none_left: list[str] = []
    assert _commit_dirty(path2, tmp_path, base=None, left=none_left)
    assert LOG in _files(path2) and none_left == []


# -- the publish ------------------------------------------------------------------


@pytest.fixture()
def origin_with_log(origin: Path, tmp_path: Path) -> Path:
    """`origin` with the guards log tracked on main, as main tracked it."""
    seed = tmp_path / "seed-log"
    _git(tmp_path, "clone", "--quiet", str(origin), str(seed))
    (seed / "scratchpad" / "x").mkdir(parents=True)
    (seed / LOG).write_text("72 lines of guard output\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "Track the guards log")
    _git(seed, "push", "--quiet", "origin", "HEAD:main")
    return origin


def test_the_leftover_path_is_reported_not_committed(
    worker_factory, monkeypatch, origin_with_log, local_urls, forge
):
    """Through the publish path: the clone, the mirror into the worker's
    repository, the auto-commit, the replay and the push. The pushed branch
    does not hold the log, and the publish says it left it unstaged."""
    def edit(repo: Path) -> None:
        _git(repo, "rm", "--cached", "--quiet", LOG)
        _git(repo, "commit", "--quiet", "-m", "Drop the scratchpad guards log")
        assert (repo / LOG).exists()

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin_with_log, task_id="t-rm-cached", edit=edit,
    )

    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}{config.task_id}"
    files = tree_at(origin_with_log, branch)
    assert LOG not in files, "the published branch holds the file the agent removed"
    assert "README.md" in files
    assert out["left_unstaged"] == [LOG], out


def test_a_publish_whose_auto_commit_undid_the_agents_commits_fails(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """The backstop, for whatever else could make the auto-commit cancel the
    agent's commits: here the agent commits a change and then restores the
    file in its working tree without committing. The tree the worker would
    push is the base's, though the agent's commits changed it: nothing is
    pushed, the reason names the path, and the attempt ends FAILED with
    `publish_refused`. MUTATION: drop the check -- the branch is pushed with
    an empty net diff and `published` is True."""
    def edit(repo: Path) -> None:
        original = (repo / "README.md").read_text()
        (repo / "README.md").write_text("the agent's change\n")
        _git(repo, "commit", "--quiet", "-am", "Change the README")
        (repo / "README.md").write_text(original)

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-undone", edit=edit,
    )

    assert out["published"] is False, out
    reason = out.get(lifecycle.AUTO_COMMIT_UNDID_FIELD)
    assert reason and "README.md" in reason, out
    assert "README.md" in out["publish_reason"], out
    branch = f"{config.git_branch_prefix}{config.task_id}"
    pushed = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=str(origin), check=True, capture_output=True, text=True,
    ).stdout.split()
    assert branch not in pushed, "a branch was pushed"

    # And the attempt that publishes it does not end SUCCEEDED.
    seed_attempt(db, task_input={"prompt": "fix the widget", "steps": 1, "sleep_seconds": 0.01},
                 attempt_count=1)
    db.doc("tasks/task_1")["metadata"] = {"dispatch": {"strategy": "direct-pr"}}
    monkeypatch.setattr(lifecycle.Worker, "_title_owed", lambda self, task: False)
    worker, _, _ = worker_factory()
    monkeypatch.setattr(worker, "_harvest_git", lambda **_kwargs: dict(out))

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task["state"]
    assert task["end_cause"] == "publish_refused", task["end_cause"]
    assert "README.md" in task["last_error"]
