"""A round's fix lands on a continued branch that another writer moved, as a fast-forward (#1821).

MEASURED 2026-10-21 (system analysis, 48 h window): of 69 ci-fix tasks, 14
FAILED with `publish_failed: push failed ... Updates were rejected because
the remote contains work`. A ci-fix clones the pull request's branch;
another fix round, the merge step's branch update or a conflict fix moves
it before the fix pushes; `push_branch` never forces, and the only
recoveries were the task's own earlier attempt (#1438) and a branch moved
by merges of the default branch alone (#2179). Every other mover lost the
fix to `swarm-work.patch`.

DECIDED (BGD, 2026-10-27, recorded on #1821): worker rebase-and-retry. On
a rejected push the worker replays the attempt's commits onto the new tip,
re-runs the publish scan, the authorship check and the fence check, and
pushes once as a fast-forward, never forced; on conflict it ends
publish_failed with the reason.

The property, against a real bare repository on disk (only HTTP to the
forge is faked, as in test_strategy_end_to_end.py):

  * another round's ordinary commit on the branch between the clone and the
    push: the fix lands on top of it, one worker commit whose parent is the
    moved tip, pushed with no lease (a fast-forward);
  * a mover that already holds the fix: nothing is pushed, the round ends
    no_change;
  * a mover whose change conflicts with the fix: nothing is pushed and
    nothing is forced; publish_failed names the moved tip and the conflict,
    and the tip the mover left is untouched;
  * a mover that rewrote the branch (the clone base is no longer under it):
    nothing is replayed onto it; publish_failed as before;
  * an attempt fenced between the replay and the retry pushes nothing;
  * a replay that adds a leak fails the scan and pushes nothing.

WRITTEN TO FAIL BEFORE THE CHANGE: on main, every mover here other than a
merge of the default branch re-raised PushRejected, so the first test's
`assert out["published"] is True` failed with "Updates were rejected".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_worker import expected_outputs as expected_mod
from agent_worker import gitops, lifecycle
from agent_worker.errors import FencedError

from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_fence_holds,
    _the_agent_titles_its_pull_request,
    assert_only_the_worker_wrote,
    forge,
    local_urls,
    origin,
    pushed_commits,
    refs,
    tree_at,
)
from test_continued_branch_moved_by_base_merge import (  # noqa: F401 -- fixtures
    _commit,
    _fixer,
    _is_ancestor,
    _sh,
    _writer,
    pushes,
    red,
)

#: Another fix round's worker identity: the branch's mover is a worker too.
ANOTHER_ROUND = ("swarm-worker", "another-round@example.invalid")


def _another_round_commits(origin: Path, tmp: Path, branch: str, name: str, text: str) -> str:
    """Another writer's ordinary commit on `branch`, pushed as a fast-forward."""
    clone = _writer(origin, tmp)
    _sh(clone, "checkout", "--quiet", "-b", "pr", f"origin/{branch}")
    _commit(clone, name, text, "another round's fix", who=ANOTHER_ROUND)
    _sh(clone, "push", "--quiet", "origin", f"HEAD:{branch}")
    return refs(origin)[branch]


def test_a_fix_lands_on_top_of_another_rounds_commit_as_a_fast_forward(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    worker, config, repo = _fixer(worker_factory, monkeypatch, origin)
    cloned_at = refs(origin)[red]
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    moved = _another_round_commits(origin, tmp_path, red, "lint.cfg", "max-line = 100\n")

    out = worker._harvest_git(publish=True)

    assert out["published"] is True, out.get("publish_reason")
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD)
    landed = refs(origin)[red]
    assert out["branch"] == red and out["pushed_head"] == landed
    # On top of the mover, never over it.
    assert _is_ancestor(origin, moved, landed), "the other round's commit was replaced"
    assert _is_ancestor(origin, cloned_at, landed)
    added = pushed_commits(origin, red, since=moved)
    assert len(added) == 1 and added[0]["parents"] == [moved]
    assert_only_the_worker_wrote(added, config)
    assert {"lint.cfg", "feature.py"} <= tree_at(origin, red)
    assert "return 2" in _sh(origin, "show", f"{red}:feature.py")
    # Two plain pushes: the refused one and the fast-forward. Never a lease,
    # never a force.
    assert [call.get("lease") for call in pushes] == [None, None]
    assert out[lifecycle.MOVED_TIP_FIELD] == moved
    assert out["moved_tip_from"] == cloned_at
    assert [p["head"] for p in forge.pulls] == [red]


def test_a_mover_that_already_holds_the_fix_ends_no_change(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    fixed = "def broken():\n    return 2\n"
    (repo / "feature.py").write_text(fixed)
    moved = _another_round_commits(origin, tmp_path, red, "feature.py", fixed)

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert not out.get(lifecycle.PUBLISH_FAILED_FIELD), out.get("publish_reason")
    assert refs(origin)[red] == moved, "something was pushed over the moved tip"
    assert moved[:12] in out[lifecycle.BASE_MERGE_NO_CHANGE_FIELD]
    assert [call.get("lease") for call in pushes] == [None]
    assert forge.pulls == []
    summary: dict[str, Any] = {"git": out}
    assert worker._no_change_after_publish(summary) is True
    assert summary[expected_mod.NO_CHANGE_SUMMARY_KEY] is True
    assert worker._publish_failed(summary) is None


def test_a_conflicting_mover_fails_publish_and_is_never_forced(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    moved = _another_round_commits(origin, tmp_path, red, "feature.py",
                                   "def broken():\n    return 'theirs'\n")

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    reason = out["publish_reason"]
    assert moved[:12] in reason and "conflict" in reason, reason
    assert "nothing was pushed or forced" in reason, reason
    assert refs(origin)[red] == moved, "the branch another round moved was changed"
    assert [call.get("lease") for call in pushes] == [None]
    assert forge.pulls == []
    assert not out.get(lifecycle.BASE_MERGE_NO_CHANGE_FIELD)


def test_a_conflict_in_a_file_the_mover_changed_is_never_taken_from_the_tip(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    """Only a base merge's version may supersede the round's (#2367): a
    mover that is another round's commit is not main's published change, so
    its side of a conflict is not taken, and the round fails."""
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    (repo / "other.py").write_text("OTHER = 1\n")
    moved = _another_round_commits(origin, tmp_path, red, "feature.py",
                                   "def broken():\n    return 'theirs'\n")

    out = worker._harvest_git(publish=True)

    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert refs(origin)[red] == moved
    assert not out.get(lifecycle.BASE_MERGE_SUPERSEDED_FIELD)


def test_a_rewritten_branch_is_not_replayed_onto(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    clone = _writer(origin, tmp_path)
    _sh(clone, "checkout", "--quiet", "-b", "pr", f"origin/{red}")
    _sh(clone, "reset", "--quiet", "--hard", "origin/main")
    _commit(clone, "rewritten.txt", "history rewritten\n", "a rewrite", who=ANOTHER_ROUND)
    _sh(clone, "push", "--quiet", "--force", "origin", f"HEAD:{red}")
    rewritten = refs(origin)[red]

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert "rejected" in out["publish_reason"]
    assert refs(origin)[red] == rewritten
    assert [call.get("lease") for call in pushes] == [None]
    assert not out.get(lifecycle.MOVED_TIP_FIELD)


def test_an_attempt_fenced_before_the_retry_pushes_nothing(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    """The fence is read again after the replay, before the second push
    (invariant 5): a superseded attempt stands down with the branch as the
    mover left it."""
    worker, config, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    moved = _another_round_commits(origin, tmp_path, red, "lint.cfg", "max-line = 100\n")

    def fenced() -> Any:
        raise FencedError(config.generation, config.generation + 1, "superseded mid-publish")

    monkeypatch.setattr(worker.control, "validate_generation", fenced)

    with pytest.raises(FencedError):
        worker._harvest_git(publish=True)

    assert refs(origin)[red] == moved, "a fenced attempt pushed"
    assert [call.get("lease") for call in pushes] == [None]
    assert forge.pulls == []


def test_a_replay_that_adds_a_leak_is_not_pushed(
    worker_factory, monkeypatch, origin, red, local_urls, forge, pushes, tmp_path
):
    """The added-text scan runs again over the span the retry pushes."""
    worker, _, repo = _fixer(worker_factory, monkeypatch, origin)
    (repo / "feature.py").write_text("def broken():\n    return 2\n")
    moved = _another_round_commits(origin, tmp_path, red, "lint.cfg", "max-line = 100\n")
    real = lifecycle.final_tree_leak
    calls: list[dict[str, Any]] = []

    def leak_on_retry(**kwargs: Any) -> str | None:
        calls.append(kwargs)
        if kwargs.get("base") == moved:
            return "a planted leak in feature.py"
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "final_tree_leak", leak_on_retry)

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert out[lifecycle.PUBLISH_FAILED_FIELD] is True
    assert "leak" in out["publish_reason"], out["publish_reason"]
    assert any(call.get("base") == moved for call in calls), "the scan did not run again"
    assert refs(origin)[red] == moved
    assert [call.get("lease") for call in pushes] == [None]


def test_moved_branch_tip_never_fetches_outside_the_prefix(tmp_path):
    with pytest.raises(gitops.GitError, match="outside"):
        gitops.moved_branch_tip(
            repo=tmp_path / "no-such-repository", url="https://github.com/acme/widgets.git",
            branch="feature/x", token="unused", base="0" * 40,
            private_dir=tmp_path / "p", logs_dir=tmp_path / "l", timeout_seconds=5,
            logger=None,
        )
