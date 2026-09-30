"""The worker's own commits are titled like the change, never by task id (#361).

The commit the worker makes of what the agent left uncommitted was titled
`swarm: uncommitted changes from <task_id>`, and a fold's
`swarm: work from <task_id>`: the line `git log --oneline` and a reviewer read
named an internal id and said nothing about the change, on a branch whose pull
request title may not carry the id at all (owner rule, 2026-09-28). The subject
is the agent's `pr-title.txt` when it wrote a usable one -- the file and the
checks the pull request title goes through -- and otherwise a neutral line with
no task id. The id is in the commit BODY.

Real git against a local `file://` remote; only the HTTP forge is faked, as in
test_agent_commits_are_kept.py. WRITTEN TO FAIL ON THE CODE BEFORE THE CHANGE,
on its assertions.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_agent_commits_are_kept import _commit
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    assert_only_the_worker_wrote,
    forge,
    local_urls,
    origin,
    pushed_commits,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TITLE = "The widget refuses a negative size"


def _attempt(worker_factory, monkeypatch, remote: Path, *, task_id: str, edit, title=None):
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{remote}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME)
    if title is not None:
        (worker.ws.artifacts / lifecycle.PR_TITLE_FILE).write_text(title)
    out = worker._harvest_git(publish=True)
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = list(reversed(pushed_commits(remote, branch)))  # oldest first
    assert_only_the_worker_wrote(commits, config)
    return commits, out


def _leave_uncommitted(repo: Path) -> None:
    (repo / "notes.txt").write_text("left uncommitted\n")


def _move_off_the_base(repo: Path) -> None:
    subprocess.run(["git", "checkout", "-q", "--orphan", "fresh"], cwd=str(repo), check=True)
    (repo / "fresh.txt").write_text("a new start\n")
    _commit(repo, "Start over")


def _subject_and_body(commit: dict) -> tuple[str, str]:
    subject, _, body = commit["message"].strip().partition("\n")
    return subject, body


def test_the_uncommitted_work_commit_takes_the_agents_title(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    commits, out = _attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-titled", edit=_leave_uncommitted, title=TITLE + "\n",
    )
    assert out["auto_committed"] is True
    subject, body = _subject_and_body(commits[-1])
    assert subject == TITLE, commits[-1]["message"]
    assert "t-titled" in body, "the task id belongs in the commit body"


def test_without_a_title_the_uncommitted_work_commit_names_no_task(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    commits, _ = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-untitled", edit=_leave_uncommitted,
    )
    subject, body = _subject_and_body(commits[-1])
    assert "t-untitled" not in subject, f"the commit subject names the task: {subject!r}"
    assert "swarm:" not in subject, subject
    assert subject == lifecycle.UNCOMMITTED_COMMIT_SUBJECT
    assert "t-untitled" in body, "the task id belongs in the commit body"


def test_a_refused_title_is_not_the_commit_subject(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The checks the pull request title goes through: a title naming the task
    id is refused there, and so here."""
    commits, _ = _attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-echo", edit=_leave_uncommitted, title="Fix the widget for t-echo",
    )
    subject, _ = _subject_and_body(commits[-1])
    assert subject == lifecycle.UNCOMMITTED_COMMIT_SUBJECT, subject


def test_a_fold_commit_takes_the_agents_title_and_names_no_task_in_its_subject(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    commits, out = _attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-fold", edit=_move_off_the_base, title=TITLE,
    )
    assert out.get("agent_commits_folded") == 1, out
    assert len(commits) == 1, commits
    subject, body = _subject_and_body(commits[0])
    assert subject == TITLE, commits[0]["message"]
    assert "t-fold" in body


def test_an_untitled_fold_commit_names_no_task_in_its_subject(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    commits, _ = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-fold-plain", edit=_move_off_the_base,
    )
    subject, body = _subject_and_body(commits[0])
    assert subject == lifecycle.FOLDED_COMMIT_SUBJECT, subject
    assert "t-fold-plain" in body
