"""A step whose clone started DETACHED publishes only what it made (#2094).

PR 2126 (HARVEST-START-SHA-2) left one gap, which its implement agent asked
about: a step cloned by sha (a by-sha `repository_ref`, or a resumed index
run) starts on a detached HEAD, so detaching onto a fetched branch is no
branch change at all, and a review that ran `git checkout --detach
origin/<reviewed>` -- a line that descends from its start -- still published
the reviewed commits as its own. BGD decided on 2026-10-23 (~18:39Z, recorded
on task_bf208a4d46f04764869d q1): "Yes, close it."

So a detached end HEAD standing on any `refs/remotes/origin/*` tip is a move,
whatever the start was, unless it is the start sha itself. Against a real
bare repository on disk (only HTTP to the forge is faked):

  * a review cloned by sha that detaches onto the fetched reviewed branch
    publishes nothing, and says why;
  * a by-sha implement step that commits on top of its start (detached, on no
    origin tip) publishes its commits;
  * a detached end HEAD on the tracking tip of the step's own branch is not
    a move;
  * a detached end HEAD equal to the start sha publishes its uncommitted
    edits, even with an origin tip on that same commit -- from a by-sha start
    or from a branch start;
  * a carrier checkpoint push after that move is skipped with the reason.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_harvest_start_sha import REVIEW, REVIEWED, IMPLEMENT, _commits, _git, _kept, _reviewed_branch
from test_carrier_branches import BRANCHES, tree_at
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    _the_agent_titles_its_pull_request,
    assert_only_the_worker_wrote,
    forge,
    local_urls,
    origin,
    pushed_commits,
    refs,
    run_attempt,
)
from worker_seeds import seed_attempt

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _cloned_by_sha(worker_factory, monkeypatch, origin: Path, *, task_id: str, dispatch: dict):
    """A worker whose `repository_ref` is main's sha: its clone starts detached."""
    start = refs(origin)["main"]
    worker, config, _ = worker_factory(
        task_id=task_id, attempt_id=f"att-{task_id}", lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}", repository_ref=start,
    )
    if f"tasks/{task_id}" not in worker.db.documents:
        # The carrier push re-checks that the attempt owns its task (#453).
        seed_attempt(worker.db, task_id=task_id, attempt_id=f"att-{task_id}",
                     lease_id=f"lease-{task_id}")
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": task_id, "metadata": {"dispatch": dispatch}}
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    cloned = worker._maybe_clone(worker._task)
    assert cloned is not None and cloned["commit"] == start, "the clone did not land on the sha"
    # The scenario: the clone itself started detached, so no branch changes.
    assert worker._start_branch == lifecycle.DETACHED_HEAD, worker._start_branch
    return worker, config, worker.ws.work / lifecycle.REPO_DIR_NAME, start


def _detaches_onto_the_reviewed_branch(repo: Path) -> None:
    _git(repo, "fetch", "--quiet", "origin", REVIEWED)
    _git(repo, "checkout", "--quiet", "--detach", f"origin/{REVIEWED}")


def test_a_review_cloned_by_sha_that_detaches_onto_the_reviewed_branch_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    reviewed_tip = _reviewed_branch(origin, tmp_path)
    worker, config, repo, _ = _cloned_by_sha(
        worker_factory, monkeypatch, origin, task_id="t-sha-review-detached", dispatch=REVIEW
    )
    _detaches_onto_the_reviewed_branch(repo)

    out = worker._harvest_git(publish=True)

    # MUTATION: check origin tips only when the start was a branch (PR 2126)
    # and the reviewed branch, which descends from the start sha, is
    # published as this review's pull request: #456 again.
    assert out["published"] is False, out
    assert forge.pulls == [], "a review cloned by sha opened a pull request"
    assert f"swarm/{config.task_id}" not in refs(origin), "the reviewed branch was republished"
    assert refs(origin)[REVIEWED] == reviewed_tip
    # MUTATION: drop the reason and nothing says why the review published nothing.
    assert out["head_moved"] == reviewed_tip[:12], out
    assert out["publish_reason"] == (
        f"HEAD moved off this task's branch (to {reviewed_tip[:12]}); "
        "nothing of this task's was published"
    ), out
    # MUTATION: count the moved HEAD's commits and the reviewed two are this step's.
    assert out["commit_count"] == 0 and out["dirty_count"] == 0, out
    # MUTATION: mark the publish failed and the review's verdict is lost.
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD), out
    assert worker._no_change({"git": out}) is True


def test_a_by_sha_implement_step_that_commits_on_its_start_publishes_its_commits(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control: detached, descending from the start, on no origin tip."""
    worker, config, repo, _ = _cloned_by_sha(
        worker_factory, monkeypatch, origin, task_id="t-sha-own-commits", dispatch=IMPLEMENT
    )
    _commits(repo, "tests", "fix")

    out = worker._harvest_git(publish=True)

    branch = f"swarm/{config.task_id}"
    # MUTATION: call every detached HEAD of a by-sha clone a move and the
    # step's own commits are lost.
    assert out["published"] is True, out.get("publish_reason")
    assert "head_moved" not in out, out
    assert out["commit_count"] == 2, out
    commits = pushed_commits(origin, branch)
    assert [c["message"].strip() for c in commits] == _kept(config.task_id, "Add the fix", "Add the tests")
    assert_only_the_worker_wrote(commits, config)
    assert [pull["head"] for pull in forge.pulls] == [branch]


def test_a_by_sha_step_detached_on_its_own_branchs_tracking_tip_publishes_its_commits(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A `git_fetch` of the step's own branch writes its tracking ref onto the
    step's own commits; that branch is never a fetched line (PR 2126)."""
    worker, config, repo, _ = _cloned_by_sha(
        worker_factory, monkeypatch, origin, task_id="t-sha-own-tip", dispatch=IMPLEMENT
    )
    _commits(repo, "tests")
    _git(repo, "update-ref", f"refs/remotes/origin/swarm/{config.task_id}", "HEAD")

    out = worker._harvest_git(publish=True)

    # MUTATION: count the step's own branch among the fetched tips and its
    # own commit reads as a move onto someone else's line.
    assert out["published"] is True, out.get("publish_reason")
    assert "head_moved" not in out, out
    assert "tests.py" in tree_at(origin, f"swarm/{config.task_id}")


def test_a_by_sha_step_on_its_start_sha_with_edits_publishes_them(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """HEAD is still the start sha, and `origin/main` is fetched onto that
    same commit: the step stayed put, so its edits are its own."""
    worker, config, repo, start = _cloned_by_sha(
        worker_factory, monkeypatch, origin, task_id="t-sha-stayed-edits", dispatch=IMPLEMENT
    )
    _git(repo, "fetch", "--quiet", "origin", "main")
    assert _git(repo, "rev-parse", "refs/remotes/origin/main") == start, "the scenario needs a tip"
    (repo / "edit.py").write_text("an uncommitted edit\n")

    out = worker._harvest_git(publish=True)

    # MUTATION: drop the start-sha exception and this step, which never left
    # its start, reads as moved onto origin/main and its edit is lost.
    assert out["published"] is True, out.get("publish_reason")
    assert "head_moved" not in out, out
    assert "edit.py" in tree_at(origin, f"swarm/{config.task_id}")


def test_a_branch_start_detached_on_its_own_start_sha_publishes_its_edits(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """From a branch start: `git checkout --detach` where the clone landed.
    The end HEAD is the start sha, which is also `origin/main`'s tip; it is
    the start, so it is not a move (BGD's rule: "except the start sha")."""
    def edit(repo: Path) -> None:
        _git(repo, "checkout", "--quiet", "--detach")
        (repo / "edit.py").write_text("an uncommitted edit\n")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-branch-detached-start",
        dispatch=IMPLEMENT, edit=edit,
    )

    # MUTATION: test the origin tips before the start sha and this reads as a
    # move onto origin/main, publishing nothing of the step's own edit.
    assert out["published"] is True, out.get("publish_reason")
    assert "head_moved" not in out, out
    assert "edit.py" in tree_at(origin, f"swarm/{config.task_id}")


def test_a_carrier_push_after_a_by_sha_detached_move_is_skipped_with_the_reason(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    reviewed_tip = _reviewed_branch(origin, tmp_path)
    worker, config, repo, _ = _cloned_by_sha(
        worker_factory, monkeypatch, origin, task_id="t-sha-carrier-moved", dispatch=BRANCHES
    )
    _detaches_onto_the_reviewed_branch(repo)
    said: list[tuple[str, dict]] = []
    real_info = worker.log.info

    def info(event: str, **fields: object) -> None:
        said.append((event, fields))
        real_info(event, **fields)

    monkeypatch.setattr(worker.log, "info", info)

    worker._push_carrier_branch("final")

    # MUTATION: let the carrier ask only about branch changes and the reviewed
    # branch's tree is pushed to swarm/t-sha-carrier-moved as this checkpoint.
    assert f"swarm/{config.task_id}" not in refs(origin), "a moved HEAD was carried"
    assert refs(origin)[REVIEWED] == reviewed_tip
    assert worker._carrier_pushed is None
    # MUTATION: skip silently and nothing says why the branch was not pushed.
    reasons = [f.get("reason") for e, f in said if e == "carrier: the step's branch was not pushed"]
    assert reasons == [
        f"HEAD moved off this task's branch (to {reviewed_tip[:12]}); "
        "nothing of this task's was published"
    ], said
