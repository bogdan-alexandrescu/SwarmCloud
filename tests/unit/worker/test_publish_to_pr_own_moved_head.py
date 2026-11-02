"""A pull request head moved only by this task's own earlier generation is not refused (#2693).

MEASURED: task_ffc37f64's generation 1 was fenced `dead_worker` and still
pushed onto PR #2613 (a273bb75 -> 7eb64482); generation 5, holding the same
work as 831e2bef on a273bb75, then refused its own publish `head_moved`. A
head whose new commits are all this task's worker's -- the worker's identity
and the task's own "Made by the worker for task <id>." line -- was written by
nobody else, so:

* the moved head already carries this attempt's tree: nothing is pushed, and
  the publish records the pull request and its current head;
* it carries something else: this attempt's commits are replayed onto it and
  pushed as a fast-forward leased on that head, never forced;
* any commit by another writer -- another author, or the worker writing for
  another task -- is refused `head_moved` exactly as before, nothing pushed.

Real git against a bare repository on disk; only HTTP is faked
(test_publish_to_pr.py's fixtures).

MUTATIONS: drop the own-commit proof -- (c) and (d) publish over another
writer. Accept a worker commit naming another task -- (c)'s second case
publishes. Push the tree-equal case -- origin moves. Push the replay without
the lease -- the recorded lease is None.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle
from agent_worker.config import WorkerConfig

from test_publish_to_pr import (  # noqa: F401 -- fixtures
    DIRECT,
    NUMBER,
    PR_BRANCH,
    _edit,
    _no_agent_text,
    _push_commit,
    _titled,
    pull,
)
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_fence_holds,
    forge,
    local_urls,
    origin,
    refs,
    run_attempt,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TASK = "t-follow"
WORKER = (WorkerConfig.git_author_name, WorkerConfig.git_author_email)


def _commit_as(origin: Path, tmp: Path, name: str, files: dict[str, str], *,
               who: tuple[str, str], task: str | None) -> str:
    """A commit on the pull request's branch, as `who`, in the worker's message shape."""
    work = tmp / f"gen-{name}"
    subprocess.run(["git", "clone", "--quiet", "--branch", PR_BRANCH, str(origin), str(work)],
                   check=True, capture_output=True)
    for path, text in files.items():
        (work / path).write_text(text)
    message = f"{name}\n\nAn earlier generation's publish."
    if task is not None:
        message += f"\n\nMade by the worker for task {task}."
    ident = ["-c", f"user.name={who[0]}", "-c", f"user.email={who[1]}",
             "-c", "commit.gpgSign=false"]
    subprocess.run(["git", *ident, "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", *ident, "commit", "--quiet", "-m", message], cwd=work, check=True,
                   capture_output=True)
    subprocess.run(["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{PR_BRANCH}"],
                   cwd=work, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _the_replays_fence_holds(monkeypatch):
    """The replay reads the fence with `validate_generation` before its push,
    and these harnesses write no task document: it holds here, as
    `_the_fence_holds` makes the publish's other reads hold."""
    monkeypatch.setattr(lifecycle.ControlPlane, "validate_generation", lambda self: None)


@pytest.fixture
def leases(monkeypatch) -> list:
    seen: list = []
    real = lifecycle.push_branch

    def recording(**kwargs):
        seen.append(kwargs.get("lease"))
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "push_branch", recording)
    return seen


def test_a_head_moved_by_this_tasks_own_earlier_generation_with_this_tree_pushes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path, leases
):
    _no_agent_text(monkeypatch)
    moved: list[str] = []

    def edit_then_gen1_publishes_the_same_work(repo: Path) -> None:
        _edit("followup")(repo)
        moved.append(_commit_as(origin, tmp_path, "gen1", {"followup.txt": "work from followup\n"},
                                who=WORKER, task=TASK))

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id=TASK,
                            dispatch=DIRECT, edit=edit_then_gen1_publishes_the_same_work)
    assert out["published"] is True, out
    assert lifecycle.PUBLISH_FAILED_FIELD not in out, out
    assert refs(origin)[PR_BRANCH] == moved[0], "the tree-equal publish pushed"
    assert leases == [], "a push was attempted over a head that already holds the work"
    assert out["pushed_head"] == moved[0]
    assert out["pull_request"]["number"] == NUMBER
    assert forge.pulls == []


def test_a_head_moved_by_this_tasks_own_other_work_is_replayed_onto_and_leased(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path, leases
):
    _no_agent_text(monkeypatch)
    moved: list[str] = []

    def edit_then_gen1_publishes_partial_work(repo: Path) -> None:
        _edit("followup")(repo)
        moved.append(_commit_as(origin, tmp_path, "gen1", {"partial.txt": "gen 1's part\n"},
                                who=WORKER, task=TASK))

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id=TASK,
                            dispatch=DIRECT, edit=edit_then_gen1_publishes_partial_work)
    assert out["published"] is True, out
    tip = refs(origin)[PR_BRANCH]
    assert tip != moved[0] and out["pushed_head"] == tip
    subprocess.run(["git", "merge-base", "--is-ancestor", moved[0], tip], cwd=origin,
                   check=True)  # a fast-forward of the moved head, never forced
    assert {"mockup.txt", "partial.txt", "followup.txt"} <= tree_at(origin, tip)
    assert leases == [moved[0]], "the replay's push was not leased on the moved head"
    assert forge.pulls == []


@pytest.mark.parametrize(
    ("who", "task"),
    [(("someone", "someone@example.invalid"), TASK), (WORKER, "t-another")],
    ids=["another-author", "the-worker-for-another-task"],
)
def test_a_head_moved_by_another_writer_is_still_refused(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path, who, task
):
    _no_agent_text(monkeypatch)

    def edit_then_another_writer_pushes(repo: Path) -> None:
        _edit("followup")(repo)
        _commit_as(origin, tmp_path, "other", {"other.txt": "theirs\n"}, who=who, task=task)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id=TASK,
                            dispatch=DIRECT, edit=edit_then_another_writer_pushes)
    moved = refs(origin)[PR_BRANCH]
    assert out["published"] is False, out
    assert "head moved" in out["publish_reason"]
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert "followup.txt" not in tree_at(origin, moved), "the lane's push landed"
    assert "other.txt" in tree_at(origin, moved), "another writer's push was replaced"
    assert forge.pulls == [] and pull.updates == []


def test_a_head_moved_by_own_commits_then_another_writer_is_refused(
    worker_factory, monkeypatch, origin, local_urls, forge, pull, tmp_path
):
    _no_agent_text(monkeypatch)

    def edit_then_gen1_then_someone(repo: Path) -> None:
        _edit("followup")(repo)
        _commit_as(origin, tmp_path, "gen1", {"partial.txt": "gen 1's part\n"},
                   who=WORKER, task=TASK)
        _push_commit(origin, tmp_path, PR_BRANCH, "by-hand", start=PR_BRANCH)

    _, _, out = run_attempt(worker_factory, monkeypatch, origin, task_id=TASK,
                            dispatch=DIRECT, edit=edit_then_gen1_then_someone)
    moved = refs(origin)[PR_BRANCH]
    assert out["published"] is False, out
    assert "head moved" in out["publish_reason"]
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert "followup.txt" not in tree_at(origin, moved)
    assert "by-hand.txt" in tree_at(origin, moved)
    assert forge.pulls == [] and pull.updates == []
