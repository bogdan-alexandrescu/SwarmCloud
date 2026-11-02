"""A conflict fix round publishes a REAL merge of the default branch (#1321).

WHAT WENT WRONG. A merge step's conflict fix round (`merge.py`
CONFLICT_FIX_PENDING/RUNNING, the claude-code continuation
`swarm_api.mergewake.conflict_fix_continuation` submits) told the agent to
run `git merge --no-ff origin/<base>`, and the agent did -- but the worker's
publish rewrote every commit on the agent's first-parent line as its own
(`lifecycle.replay_agent_commits`, #242) and "A merge the agent made keeps
its merged tree and loses its second parent". The fold
(`gitops.fold_agent_commits`) did the same. So the forge received
"Merge remote-tracking branch 'origin/main' into ..." commits with ONE
parent (PR 1316: 8e97bb04, a70e8e2a; PR 1304: edad021): GitHub kept the old
merge base, listed every file main had changed as the PR's, and the PR still
conflicted until the step failed after its two rounds.

WHAT IS PINNED.

* A merge parent that is on the default branch AS THE FORGE HOLDS IT is
  kept, by the replay and by the fold, so the pushed merge has two parents
  and the pull request's merge base becomes main's head. A side branch the
  agent made is still dropped (`test_agent_commits_are_kept.py`), since its
  commits would otherwise reach the forge unrewritten.
* A conflict fix round (`metadata.dispatch.conflict_round`, the signed mark
  swarm-api copies from the `metadata.merge_conflict_round` record
  `mergewake` submits the round with) checks, before it pushes, that `git merge-base <main on the forge> HEAD` is main's head --
  or a main commit the round itself merged, when main moved on during the
  round -- and refuses the publish with a reason naming the merge base
  otherwise, so a round that cannot clear the conflict fails loudly.

Real git against local `file://` remotes; the worker's clone is the real
`--depth 1` clone, so the shallow case is the one exercised. Only the HTTP
forge is faked, as in `test_strategy_end_to_end.py`.
"""

from __future__ import annotations

import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    _the_agent_titles_its_pull_request,
    assert_only_the_worker_wrote,
    forge,
    local_urls,
    origin,
    refs,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_RNG = random.Random(1321)

ROUND = {"merge_task": "task_" + "a" * 20, "round": 1, "head_sha": "0" * 40, "pull_request": 1316}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _as_claude(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )


def _credential_line() -> str:
    """A real-shaped AWS key id, built at run time so the source never holds one."""
    while True:
        tail = "".join(_RNG.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        if not lifecycle._PLACEHOLDER.search(tail):
            return "aws_key = '" + "AK" + "IA" + tail + "'\n"


def _diverge(origin: Path, tmp_path: Path) -> Path:
    """`lane` and `main` both change `shared.txt` from their fork point, so
    the lane's pull request conflicts. Returns the developer clone."""
    dev = tmp_path / "upstream-dev"
    _git(tmp_path, "clone", "--quiet", str(origin), str(dev))
    (dev / "shared.txt").write_text("the original line\n")
    _git(dev, "add", "-A")
    _git(dev, "commit", "--quiet", "-m", "fork point")
    _git(dev, "push", "--quiet", "origin", "main")
    _git(dev, "checkout", "--quiet", "-b", "lane")
    (dev / "shared.txt").write_text("the lane's line\n")
    (dev / "lane.txt").write_text("the lane's own file\n")
    _git(dev, "add", "-A")
    _git(dev, "commit", "--quiet", "-m", "the lane's change")
    _git(dev, "push", "--quiet", "origin", "lane")
    _git(dev, "checkout", "--quiet", "main")
    for n in range(3):
        (dev / f"main-{n}.txt").write_text(f"main moved {n}\n")
        _git(dev, "add", "-A")
        _git(dev, "commit", "--quiet", "-m", f"main moves {n}")
    (dev / "shared.txt").write_text("main's line\n")
    _git(dev, "add", "-A")
    _git(dev, "commit", "--quiet", "-m", "main changes the shared line")
    _git(dev, "push", "--quiet", "origin", "main")
    return dev


def _advance_main(dev: Path) -> str:
    """`main` moves on again, without touching what the lane changed."""
    (dev / "later.txt").write_text("after the round fetched main\n")
    _git(dev, "add", "-A")
    _git(dev, "commit", "--quiet", "-m", "main moves on during the round")
    _git(dev, "push", "--quiet", "origin", "main")
    return _git(dev, "rev-parse", "HEAD")


def _round(worker_factory, monkeypatch, origin: Path, *, task_id: str, edit,
           conflict_round: bool = True):
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    dispatch: dict = {"strategy": "direct-pr"}
    metadata: dict = {"dispatch": dispatch}
    if conflict_round:
        # Both, as swarm-api stores a round: the record mergewake submits it
        # with, and the signed mark the worker acts on.
        dispatch[lifecycle.CONFLICT_ROUND_FIELD] = True
        metadata["merge_conflict_round"] = dict(ROUND)
    task = {"task_id": task_id, "repository_ref": "lane", "metadata": metadata}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    assert _git(repo, "rev-parse", "--is-shallow-repository") == "true", (
        "the worker's clone is no longer shallow; this test exercises the shallow case"
    )
    base = cloned["commit"]
    edit(repo)
    return config, base, worker._harvest_git(publish=True)


def _fetch_main(repo: Path, origin: Path) -> None:
    """What the round's prompt says: unshallow, then fetch the base branch."""
    _git(repo, "fetch", "--quiet", "--unshallow", f"file://{origin}")
    _git(repo, "fetch", "--quiet", f"file://{origin}", "main:refs/remotes/origin/main")


def _resolve(repo: Path) -> None:
    (repo / "shared.txt").write_text("the lane's line, kept beside main's line\n")
    assert _as_claude(repo, "add", "-A").returncode == 0


def _merge_main(origin: Path):
    def edit(repo: Path) -> None:
        _fetch_main(repo, origin)
        merged = _as_claude(repo, "merge", "--no-ff", "origin/main")
        assert merged.returncode != 0, "the fixture was meant to conflict"
        _resolve(repo)
        done = _as_claude(repo, "commit", "--quiet", "--no-edit")
        assert done.returncode == 0, done.stderr

    return edit


def _parents(origin: Path, ref: str) -> list[str]:
    return _git(origin, "rev-list", "--parents", "-n1", ref).split()[1:]


def _first_parent_commits(origin: Path, base: str, branch: str) -> list[dict]:
    raw = _git(origin, "log", "--first-parent",
               "--format=%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e", f"{base}..{branch}")
    out = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if record:
            an, ae, cn, ce, message = record.split("\x1f", 4)
            out.append({"author": (an, ae), "committer": (cn, ce), "message": message})
    return out


def test_a_conflict_round_pushes_a_two_parent_merge_whose_base_is_mains_head(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _diverge(origin, tmp_path)
    main = refs(origin)["main"]
    config, base, out = _round(worker_factory, monkeypatch, origin,
                               task_id="t-conflict-merge", edit=_merge_main(origin))

    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}t-conflict-merge"
    tip = refs(origin)[branch]
    assert _parents(origin, tip) == [base, main], (
        "the conflict round's merge reached the forge without main as its second parent"
    )
    assert _git(origin, "merge-base", "main", branch) == main
    # GitHub's file list is the three-dot diff: only the lane's own change.
    changed = set(_git(origin, "diff", "--name-only", f"main...{branch}").split())
    assert changed == {"shared.txt", "lane.txt"}, changed
    assert _git(origin, "show", f"{branch}:shared.txt") == "the lane's line, kept beside main's line"
    assert_only_the_worker_wrote(_first_parent_commits(origin, base, branch), config)


def test_the_old_one_parent_merge_is_refused_with_its_merge_base_named(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The control: a "merge" with one parent -- a squash merge committed with
    git's merge message, the shape the forge received -- leaves the merge base
    where it was, so the round refuses rather than push it and burn a round."""
    _diverge(origin, tmp_path)

    def edit(repo: Path) -> None:
        _fetch_main(repo, origin)
        _as_claude(repo, "merge", "--squash", "origin/main")
        _resolve(repo)
        done = _as_claude(repo, "commit", "--quiet", "-m",
                          "Merge remote-tracking branch 'origin/main' into lane")
        assert done.returncode == 0, done.stderr
        assert len(_git(repo, "rev-list", "--parents", "-n1", "HEAD").split()) == 2

    config, _, out = _round(worker_factory, monkeypatch, origin,
                            task_id="t-conflict-squash", edit=edit)

    assert out["published"] is False, out
    assert out.get(lifecycle.PUBLISH_FAILED_FIELD) is True, out
    reason = out.get("publish_reason", "")
    assert "merge base" in reason and "conflict fix round" in reason, reason
    assert f"{config.git_branch_prefix}t-conflict-squash" not in refs(origin)


def test_the_same_one_parent_commit_outside_a_conflict_round_still_publishes(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The check is the conflict round's: an ordinary step is not refused for
    not having merged main."""
    _diverge(origin, tmp_path)

    def edit(repo: Path) -> None:
        (repo / "lane-more.txt").write_text("more lane work\n")
        _as_claude(repo, "add", "-A")
        assert _as_claude(repo, "commit", "--quiet", "-m", "more").returncode == 0

    config, _, out = _round(worker_factory, monkeypatch, origin, task_id="t-not-a-round",
                            edit=edit, conflict_round=False)

    assert out["published"] is True, out.get("publish_reason")


def test_main_moving_on_during_the_round_does_not_refuse_the_merge_it_made(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    dev = _diverge(origin, tmp_path)
    merged_main = refs(origin)["main"]
    merge = _merge_main(origin)

    def edit(repo: Path) -> None:
        merge(repo)
        _advance_main(dev)

    config, base, out = _round(worker_factory, monkeypatch, origin,
                               task_id="t-main-moved", edit=edit)

    assert out["published"] is True, out.get("publish_reason")
    branch = f"{config.git_branch_prefix}t-main-moved"
    assert _parents(origin, branch) == [base, merged_main]


def test_a_folded_conflict_round_keeps_main_as_the_folds_second_parent(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """A credential added in one commit and removed in the next folds the
    history (only the final tree may be pushed); the fold keeps the merge."""
    _diverge(origin, tmp_path)
    main = refs(origin)["main"]
    merge = _merge_main(origin)

    def edit(repo: Path) -> None:
        merge(repo)
        (repo / "settings.py").write_text(_credential_line())
        _as_claude(repo, "add", "-A")
        assert _as_claude(repo, "commit", "--quiet", "-m", "oops").returncode == 0
        (repo / "settings.py").unlink()
        _as_claude(repo, "add", "-A")
        assert _as_claude(repo, "commit", "--quiet", "-m", "remove it").returncode == 0

    config, base, out = _round(worker_factory, monkeypatch, origin,
                               task_id="t-conflict-fold", edit=edit)

    assert out["published"] is True, out.get("publish_reason")
    assert out.get("agent_commits_folded"), out
    branch = f"{config.git_branch_prefix}t-conflict-fold"
    assert _parents(origin, branch) == [base, main]
    assert _git(origin, "merge-base", "main", branch) == main
    assert_only_the_worker_wrote(_first_parent_commits(origin, base, branch), config)


def test_the_round_mark_is_the_one_swarm_api_signs():
    """The worker cannot import swarm_api, so it restates the field; this
    holds the restatement to the block swarm-api writes and signs."""
    from swarm_api.validation import CONFLICT_ROUND_FIELD, DispatchOptions

    assert lifecycle.CONFLICT_ROUND_FIELD == CONFLICT_ROUND_FIELD
    assert DispatchOptions(conflict_round=True).to_metadata()[CONFLICT_ROUND_FIELD] is True


def test_an_unsigned_round_record_alone_asks_for_no_check(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The worker acts only on what the step-spec signature covers: the
    record under `metadata.merge_conflict_round` without the signed mark is
    an ordinary step's metadata."""
    _diverge(origin, tmp_path)

    def edit(repo: Path) -> None:
        (repo / "lane-more.txt").write_text("more lane work\n")
        _as_claude(repo, "add", "-A")
        assert _as_claude(repo, "commit", "--quiet", "-m", "more").returncode == 0

    worker_metadata = {"dispatch": {"strategy": "direct-pr"}, "merge_conflict_round": dict(ROUND)}
    worker, config, _ = worker_factory(
        task_id="t-unsigned", attempt_id="att-t-unsigned", lease_id="lease-t-unsigned",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-unsigned", "repository_ref": "lane", "metadata": worker_metadata}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME)
    out = worker._harvest_git(publish=True)

    assert out["published"] is True, out.get("publish_reason")
