"""A step publishes only what it made on top of where it started (#2094).

MEASURED 2026-10-23 (product-oracle, strategy `direct-pr`): a read-only
re-review (`task_aeba00993037460dbe42`) and a NOT_YET review
(`task_8418c1951306435fbd9b`) each checked out the branch they reviewed and
committed nothing. The end-of-task harvest diffed HEAD against the clone
base, counted the reviewed branch's commits as the step's own work, and
published them as new pull requests: #456 (+3,555 lines) and #465 (+1,619
lines), each the same tree as the pull request under review -- one of them
code the review had just rejected.

The property, against a real bare repository on disk (only HTTP to the forge
is faked, as in test_strategy_end_to_end.py):

  * a step whose agent checks out another branch and commits nothing
    publishes nothing, says HEAD moved and where to, and still ends
    `no_change` when it allows an empty diff (a review's verdict stands);
  * a step whose agent checks out another branch AND commits on it publishes
    nothing either: none of that line is this task's to publish;
  * a HEAD that stays on the task's branch but no longer descends from the
    start sha publishes nothing;
  * a step that commits on top of its start publishes exactly its own
    commits, and a step allowed an empty diff that did nothing opens no pull
    request (the controls).

RELAXED FOR A NEW LOCAL BRANCH (BGD, 2026-10-23 16:47Z, task_306e3aa8dee141a5a390
q0). All five implement agents sampled that day ran `git checkout -b <name>`
from their start and committed there; the strict rule above refused every one.
A branch the agent CREATED -- one with no `refs/remotes/origin/<name>` in the
clone -- whose HEAD descends from the start sha is not a move:

  * an implement agent on its own new branch publishes exactly its commits;
  * a new local branch that does not descend from the start publishes nothing;
  * a detached HEAD standing on an origin tip (the reviewed branch fetched and
    detached onto) publishes nothing;
  * a resumed attempt reads the start branch back from the manifest the
    worker wrote; a manifest without it skips the branch check only;
  * a `carrier: branches` checkpoint push obeys the same rule.

The PR 2078 tool-pushed-tip case is pinned in test_publish_after_tool_push.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle

from test_carrier_branches import BRANCHES, _worker as _carrier_worker
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    _attempt,
    _the_agent_titles_its_pull_request,
    agent_does_nothing,
    assert_only_the_worker_wrote,
    forge,
    local_urls,
    origin,
    pushed_commits,
    refs,
    run_attempt,
)
from worker_seeds import record_as_earlier_attempt

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

REVIEWED = "swarm/task_reviewed"
REVIEW = {"strategy": "direct-pr", "allow_empty_diff": True}
IMPLEMENT = {"strategy": "direct-pr"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _reviewed_branch(origin: Path, tmp: Path) -> str:
    """Another step's pull request branch on the remote: two commits over main."""
    clone = tmp / "author"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "checkout", "--quiet", "-b", REVIEWED)
    for name in ("one", "two"):
        (clone / f"{name}.py").write_text(f"the reviewed change, part {name}\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "--quiet", "-m", f"Reviewed change {name}")
    _git(clone, "push", "--quiet", "origin", f"HEAD:refs/heads/{REVIEWED}")
    return refs(origin)[REVIEWED]


def _checks_out_the_reviewed_branch(repo: Path) -> None:
    """What both reviews did: fetch the branch under review and switch to it."""
    _git(repo, "fetch", "--quiet", "origin", REVIEWED)
    _git(repo, "checkout", "--quiet", REVIEWED)


def test_a_review_that_checks_out_the_reviewed_branch_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    reviewed_tip = _reviewed_branch(origin, tmp_path)

    worker, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-review-moved",
        dispatch=REVIEW, edit=_checks_out_the_reviewed_branch,
    )

    # MUTATION: diff HEAD against the clone base again and the reviewed
    # branch's two commits are counted as this step's, pushed, and opened as
    # a pull request: #456 and #465.
    assert out["published"] is False, out
    assert forge.pulls == [], "a review that changed nothing opened a pull request"
    assert f"swarm/{config.task_id}" not in refs(origin), "the reviewed branch was republished"
    assert refs(origin)[REVIEWED] == reviewed_tip
    # MUTATION: drop the reason and the step publishes nothing without saying
    # why, which reads exactly like "the agent changed nothing".
    assert out["publish_reason"] == (
        f"HEAD moved off this task's branch (to {REVIEWED}); "
        "nothing of this task's was published"
    ), out
    assert out["head_moved"] == REVIEWED
    # MUTATION: count the moved HEAD's commits and the summary claims two
    # commits of the reviewed branch as this step's work.
    assert out["commit_count"] == 0 and out["dirty_count"] == 0, out
    assert not out.get("patch"), "the reviewed branch's diff was harvested as this step's"
    # MUTATION: mark the publish failed and the review's verdict is replaced by
    # a publish failure; it is a no_change review, as if it had stayed put.
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD), out
    assert worker._no_change({"git": out}) is True


def test_a_step_that_commits_on_the_branch_it_checked_out_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    reviewed_tip = _reviewed_branch(origin, tmp_path)

    def edit(repo: Path) -> None:
        _checks_out_the_reviewed_branch(repo)
        (repo / "fixup.py").write_text("a commit on someone else's branch\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", "Fix the reviewed change")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-moved-and-committed",
        dispatch=IMPLEMENT, edit=edit,
    )

    # MUTATION: decide "moved" only for a step with no commit of its own and
    # this publishes the reviewed branch's commits with the fixup on top.
    assert out["published"] is False, out
    assert out["head_moved"] == REVIEWED
    assert "HEAD moved off this task's branch (to swarm/task_reviewed)" in out["publish_reason"]
    assert forge.pulls == []
    assert f"swarm/{config.task_id}" not in refs(origin)
    assert refs(origin)[REVIEWED] == reviewed_tip, "someone else's branch was pushed to"


def test_a_head_on_the_same_branch_that_does_not_descend_from_the_start_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The branch name is unchanged; its line is not the one the step started
    on, so nothing on it tells this step's commits from anyone else's."""
    def edit(repo: Path) -> None:
        _git(repo, "checkout", "--quiet", "--orphan", "fresh")
        (repo / "fresh.txt").write_text("a new start\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", "Start over")
        _git(repo, "branch", "--quiet", "-M", "fresh", "main")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-orphan-same-branch",
        dispatch=IMPLEMENT, edit=edit,
    )

    head = out["head_moved"]
    # MUTATION: check only the branch name and this line is folded and pushed.
    assert out["published"] is False, out
    assert len(head) == 12 and head != (out["base"] or "")[:12], out
    assert out["publish_reason"] == (
        f"HEAD moved off this task's branch (to {head}); nothing of this task's was published"
    )
    assert forge.pulls == [] and f"swarm/{config.task_id}" not in refs(origin)


def test_a_step_that_commits_on_its_start_publishes_exactly_its_own_commits(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control: the guard costs a step that stayed put nothing."""
    def edit(repo: Path) -> None:
        for name in ("tests", "fix"):
            (repo / f"{name}.py").write_text(f"{name}\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "--quiet", "-m", f"Add the {name}")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-own-commits",
        dispatch=IMPLEMENT, edit=edit,
    )

    branch = f"swarm/{config.task_id}"
    # MUTATION: treat any HEAD as moved and this step's own work is lost.
    assert out["published"] is True, out.get("publish_reason")
    assert "head_moved" not in out
    assert out["commit_count"] == 2 and out.get("agent_commits_kept") == 2, out
    commits = pushed_commits(origin, branch)
    assert [c["message"].strip() for c in commits] == _kept(config.task_id, "Add the fix", "Add the tests")
    assert_only_the_worker_wrote(commits, config)
    assert [pull["head"] for pull in forge.pulls] == [branch]


def test_a_step_allowed_an_empty_diff_that_did_nothing_opens_no_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control for a read-only step that stayed where it started."""
    worker, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-review-stayed",
        dispatch=REVIEW, edit=agent_does_nothing,
    )

    # MUTATION: publish a read-only step's clone and this opens an empty PR.
    assert out["published"] is False and forge.pulls == [], out
    assert "head_moved" not in out
    assert out["publish_reason"] == "the agent changed nothing in the repository"
    assert worker._no_change({"git": out}) is True
    assert f"swarm/{config.task_id}" not in refs(origin)


# -- a branch the agent created from its start is its own (BGD, 2026-10-23) --


def _kept(task_id: str, *subjects: str) -> list[str]:
    """Kept agent commits' messages: each ends with the worker's line (#2505
    review), which the merge step reads to tell a platform head from a
    person's push."""
    return [f"{subject}\n\nMade by the worker for task {task_id}." for subject in subjects]


def _commits(repo: Path, *names: str) -> None:
    for name in names:
        (repo / f"{name}.py").write_text(f"{name}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", f"Add the {name}")


def test_an_implement_agent_on_its_own_new_branch_publishes_exactly_its_commits(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """What all five implement agents sampled on 2026-10-23 did."""
    def edit(repo: Path) -> None:
        _git(repo, "checkout", "--quiet", "-b", "feature")
        _commits(repo, "tests", "fix")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-own-new-branch",
        dispatch=IMPLEMENT, edit=edit,
    )

    branch = f"swarm/{config.task_id}"
    # MUTATION: call every branch change a move (PR 2107's strict rule) and
    # this publishes nothing, "HEAD moved off this task's branch (to feature)".
    assert out["published"] is True, out.get("publish_reason")
    assert "head_moved" not in out, out
    # MUTATION: count anything but the agent's own line and this is not 2.
    assert out["commit_count"] == 2 and out.get("agent_commits_kept") == 2, out
    commits = pushed_commits(origin, branch)
    assert [c["message"].strip() for c in commits] == _kept(config.task_id, "Add the fix", "Add the tests")
    assert_only_the_worker_wrote(commits, config)
    assert [pull["head"] for pull in forge.pulls] == [branch]
    assert "feature" not in refs(origin), "the agent's local name was pushed"


def test_a_review_that_fetches_a_branch_with_an_origin_counterpart_still_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The relaxation does not reach a branch the agent fetched: the clone
    tracks every branch, so the fetch wrote `refs/remotes/origin/<name>`."""
    _reviewed_branch(origin, tmp_path)

    def edit(repo: Path) -> None:
        _checks_out_the_reviewed_branch(repo)
        assert (repo / ".git" / "refs" / "remotes" / "origin" / REVIEWED).is_file() or (
            REVIEWED in (repo / ".git" / "packed-refs").read_text()
        ), "the scenario needs an origin counterpart"

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-review-counterpart",
        dispatch=REVIEW, edit=edit,
    )

    # MUTATION: skip the origin-counterpart check (descent alone) and the
    # reviewed branch, which descends from main, is published again: #456.
    assert out["published"] is False and forge.pulls == [], out
    assert out["head_moved"] == REVIEWED
    assert "HEAD moved off this task's branch" in out["publish_reason"]
    assert f"swarm/{config.task_id}" not in refs(origin)


def test_a_new_local_branch_that_does_not_descend_from_the_start_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    def edit(repo: Path) -> None:
        _git(repo, "checkout", "--quiet", "--orphan", "elsewhere")
        _git(repo, "rm", "-rf", "--quiet", ".")
        _commits(repo, "unrelated")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-new-branch-unrelated",
        dispatch=IMPLEMENT, edit=edit,
    )

    # MUTATION: relax on "no origin counterpart" alone, without the descent
    # check, and this unrelated line is published as the step's work.
    assert out["published"] is False, out
    assert out["head_moved"] == "elsewhere", out
    assert out["publish_reason"] == (
        "HEAD moved off this task's branch (to elsewhere); nothing of this task's was published"
    )
    assert forge.pulls == [] and f"swarm/{config.task_id}" not in refs(origin)


def test_a_head_detached_onto_an_origin_tip_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """Detached on the fetched reviewed branch: it descends from the start,
    and it is exactly a tip of `refs/remotes/origin/*`, so it is a move."""
    reviewed_tip = _reviewed_branch(origin, tmp_path)

    def edit(repo: Path) -> None:
        _git(repo, "fetch", "--quiet", "origin", REVIEWED)
        _git(repo, "checkout", "--quiet", "--detach", f"origin/{REVIEWED}")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin, task_id="t-detached-on-origin",
        dispatch=REVIEW, edit=edit,
    )

    # MUTATION: let every descending detached HEAD publish and this republishes
    # the reviewed branch's two commits.
    assert out["published"] is False and forge.pulls == [], out
    assert out["head_moved"] == reviewed_tip[:12], out
    assert f"swarm/{config.task_id}" not in refs(origin)


def _resumed(db, worker_factory, monkeypatch, origin, *, task_id: str, keep_start_branch: bool):
    """Attempt 1 clones and checkpoints; attempt 2 restores that checkpoint.
    `keep_start_branch=False` writes the manifest an older worker wrote."""
    first, _, task = _attempt(
        worker_factory, monkeypatch, origin, task_id=task_id, attempt=1, dispatch=IMPLEMENT
    )
    assert first._maybe_clone(task)["commit"], "the clone did not land"
    if not keep_start_branch:
        first.checkpoints.start_branch = None
    record = first.checkpoints.create(first.ws, label="park")
    second, config, _ = _attempt(
        worker_factory, monkeypatch, origin, task_id=task_id, attempt=2, dispatch=IMPLEMENT
    )
    record_as_earlier_attempt(db, record)
    second._restore_checkpoint({"attempt_count": 2, "latest_checkpoint": record.uri})
    assert second._maybe_clone(task)["from_checkpoint"] is True
    return second, config, second.ws.work / lifecycle.REPO_DIR_NAME


def test_a_resumed_attempt_reads_its_start_branch_from_the_manifest_and_refuses_a_move(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _reviewed_branch(origin, tmp_path)
    worker, config, repo = _resumed(
        db, worker_factory, monkeypatch, origin, task_id="t-resumed-moved", keep_start_branch=True
    )
    # MUTATION: leave `start_branch` out of the manifest, or do not read it back,
    # and the resumed attempt knows no start branch: the reviewed branch
    # descends from the start sha, so it is published as this step's work.
    assert worker._start_branch == "refs/heads/main"
    _checks_out_the_reviewed_branch(repo)

    out = worker._harvest_git(publish=True)

    assert out["published"] is False, out
    assert out["head_moved"] == REVIEWED
    assert forge.pulls == [] and f"swarm/{config.task_id}" not in refs(origin)


def test_a_resumed_attempt_whose_manifest_has_no_start_branch_keeps_the_descent_check_only(
    db, worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """An older checkpoint: the branch check is skipped, as before this
    change, and the descent check still refuses an unrelated line."""
    worker, config, repo = _resumed(
        db, worker_factory, monkeypatch, origin, task_id="t-resumed-legacy", keep_start_branch=False
    )
    # MUTATION: invent a start branch for a manifest that recorded none and
    # this reads "refs/heads/main" or "HEAD".
    assert worker._start_branch is None
    _git(repo, "checkout", "--quiet", "--orphan", "elsewhere")
    _git(repo, "rm", "-rf", "--quiet", ".")
    _commits(repo, "unrelated")

    out = worker._harvest_git(publish=True)

    # MUTATION: skip the descent check too when the start branch is unknown
    # and this unrelated line is published.
    assert out["published"] is False, out
    assert len(out["head_moved"]) == 12, out
    assert forge.pulls == [] and f"swarm/{config.task_id}" not in refs(origin)


def test_a_carrier_checkpoint_push_after_a_move_is_skipped_with_the_reason(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    worker, repo, _ = _carrier_worker(
        worker_factory, monkeypatch, origin, task_id="t-carrier-moved", dispatch=BRANCHES
    )
    reviewed_tip = _reviewed_branch(origin, tmp_path)
    _checks_out_the_reviewed_branch(repo)
    said: list[tuple[str, dict]] = []
    real_info = worker.log.info

    def info(event: str, **fields: object) -> None:
        said.append((event, fields))
        real_info(event, **fields)

    monkeypatch.setattr(worker.log, "info", info)

    worker._push_carrier_branch("final")

    # MUTATION: fold onto the tip without asking `_head_moved` and the
    # reviewed branch's tree is pushed to swarm/t-carrier-moved.
    assert "swarm/t-carrier-moved" not in refs(origin), "a moved HEAD was carried"
    assert refs(origin)[REVIEWED] == reviewed_tip
    assert worker._carrier_pushed is None
    # MUTATION: skip silently and nothing says why the branch was not pushed.
    reasons = [f.get("reason") for e, f in said if e == "carrier: the step's branch was not pushed"]
    assert reasons == [
        f"HEAD moved off this task's branch (to {REVIEWED}); nothing of this task's was published"
    ], said


def test_a_carrier_checkpoint_push_from_a_new_local_branch_still_pushes(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control for the carrier: the agent's own new branch is carried."""
    worker, repo, _ = _carrier_worker(
        worker_factory, monkeypatch, origin, task_id="t-carrier-own", dispatch=BRANCHES
    )
    _git(repo, "checkout", "--quiet", "-b", "feature")
    _commits(repo, "tests")

    worker._push_carrier_branch("final")

    # MUTATION: treat a new local branch as moved in the carrier and nothing is pushed.
    assert "tests.py" in _tree(origin, "swarm/t-carrier-own")


def _tree(bare: Path, ref: str) -> set[str]:
    return set(_git(bare, "ls-tree", "--name-only", ref).split())


@pytest.mark.parametrize("packed", [False, True])
def test_origin_tracking_refs_are_read_as_data_loose_or_packed(origin, tmp_path, packed):
    """The two readers `_head_moved` asks, against a clone whose tracking refs
    are loose files or lines of `packed-refs` (what `git gc` leaves)."""
    from agent_worker import gitops

    reviewed_tip = _reviewed_branch(origin, tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    main_tip = _git(clone, "rev-parse", "refs/remotes/origin/main")
    loose = clone / ".git" / "refs" / "remotes" / "origin"
    if packed:
        _git(clone, "pack-refs", "--all", "--prune")
        assert not (loose / "main").exists() and not (loose / REVIEWED).exists()
    else:
        # A clone packs what it fetched; a later fetch writes loose files.
        (clone / ".git" / "packed-refs").unlink()
        _git(clone, "update-ref", "refs/remotes/origin/main", main_tip)
        _git(clone, "update-ref", f"refs/remotes/origin/{REVIEWED}", reviewed_tip)
        assert (loose / "main").is_file() and (loose / REVIEWED).is_file()

    # MUTATION: read only loose files (or only packed-refs) and one of the two
    # parametrized cases answers None, and a fetched branch reads as the agent's own.
    assert gitops.read_origin_ref_as_data(clone, f"refs/heads/{REVIEWED}") == reviewed_tip
    assert gitops.read_origin_ref_as_data(clone, "refs/heads/feature") is None
    # MUTATION: follow the symbolic `origin/HEAD` or drop a packed line and
    # this set changes.
    assert gitops.origin_tips_as_data(clone) == {reviewed_tip, main_tip}
