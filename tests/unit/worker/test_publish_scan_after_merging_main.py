"""A lane that merges the default branch is scanned on its own lines, not main's (#453).

WHAT WENT WRONG. The publish scans -- the per-commit pass that decides between
keeping and folding, and the final-tree pass that refuses -- diffed what the
push adds against the CLONE BASE. A lane that merged a newer `main` into its
work (to resolve a conflict, or because a fix step continues a branch that
fell behind) carried every line `main` gained since the clone into that diff,
and a credential-shaped fixture on `main` refused the lane's publish. Those
lines are already published: they are on the repository's default branch.

WHAT IS PINNED. When the agent's first-parent line holds a merge, the worker
fetches the default branch's tip into its publish repository and scans from
the newest commit both share -- the point the lane merged, as the forge's
own pull-request diff does. Proven against the forge, never taken from the
agent's clone, so:

* a lane that merged `main` publishes, with `main`'s file on the branch;
* a credential the lane adds itself after the merge is still refused;
* a merge of a side branch the agent made -- not on the default branch --
  is still scanned from the clone base, and its credential still refused;
* a lane with no merge fetches nothing.
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
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_RNG = random.Random(453248)


def _credential_line() -> str:
    """A real-shaped AWS key id in a quoted assignment, built at run time so
    the source never holds one."""
    while True:
        tail = "".join(_RNG.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        if not lifecycle._PLACEHOLDER.search(tail):
            return "aws_key = '" + "AK" + "IA" + tail + "'\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _advance_main(origin: Path, tmp_path: Path) -> str:
    """`main` gains a file carrying a credential-shaped fixture after the clone."""
    other = tmp_path / "upstream-dev"
    _git(tmp_path, "clone", "--quiet", str(origin), str(other))
    (other / "src").mkdir(exist_ok=True)
    (other / "src" / "fixture_settings.py").write_text(_credential_line())
    _git(other, "add", "-A")
    _git(other, "commit", "--quiet", "-m", "main gains a fixture")
    _git(other, "push", "--quiet", "origin", "main")
    return _git(other, "rev-parse", "HEAD")


def _run(worker_factory, monkeypatch, origin: Path, tmp_path: Path, *, task_id: str, edit):
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    _advance_main(origin, tmp_path)
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME, origin)
    return config, worker._harvest_git(publish=True)


def _merge_main(repo: Path, origin: Path) -> None:
    _git(repo, "fetch", "--quiet", f"file://{origin}", "main")
    _git(repo, "merge", "--quiet", "--no-ff", "--no-edit", "FETCH_HEAD")


def _lane_work(repo: Path) -> None:
    (repo / "lane.txt").write_text("the lane's own work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "the lane's own work")


def test_a_lane_that_merged_main_is_not_refused_on_mains_lines(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    def edit(repo: Path, origin: Path) -> None:
        _lane_work(repo)
        _merge_main(repo, origin)
        (repo / "after.txt").write_text("more of the lane's work\n")

    config, out = _run(worker_factory, monkeypatch, origin, tmp_path,
                       task_id="t-merged-main", edit=edit)

    assert out["published"] is True, out.get("publish_reason")
    assert "final_tree_leak" not in out, out
    branch = f"{config.git_branch_prefix}t-merged-main"
    assert {"lane.txt", "after.txt", "src/fixture_settings.py"} <= tree_at(origin, branch)


def test_a_credential_the_lane_adds_after_merging_main_is_still_refused(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    def edit(repo: Path, origin: Path) -> None:
        _merge_main(repo, origin)
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "lane_settings.py").write_text(_credential_line())

    config, out = _run(worker_factory, monkeypatch, origin, tmp_path,
                       task_id="t-merged-own-key", edit=edit)

    assert out["published"] is False, out
    assert "src/lane_settings.py" in out.get("final_tree_leak", ""), out
    assert f"{config.git_branch_prefix}t-merged-own-key" not in refs(origin)


def test_a_merged_side_branch_the_agent_made_is_scanned_from_the_clone_base(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """A merge proves nothing by itself: the floor is a commit the default
    branch on the forge holds, so a side branch the agent made and merged --
    carrying a credential -- is refused exactly as an ordinary commit is."""

    def edit(repo: Path, origin: Path) -> None:
        _git(repo, "checkout", "--quiet", "-b", "side")
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "side_settings.py").write_text(_credential_line())
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", "side")
        _git(repo, "checkout", "--quiet", "-")
        _lane_work(repo)
        _git(repo, "merge", "--quiet", "--no-ff", "--no-edit", "side")

    config, out = _run(worker_factory, monkeypatch, origin, tmp_path,
                       task_id="t-merged-side", edit=edit)

    assert out["published"] is False, out
    assert "src/side_settings.py" in out.get("final_tree_leak", ""), out
    assert f"{config.git_branch_prefix}t-merged-side" not in refs(origin)


def test_a_lane_with_no_merge_fetches_nothing_more(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    fetched: list[str] = []
    real = lifecycle.fetch_branch_tip

    def spy(**kwargs):  # type: ignore[no-untyped-def]
        fetched.append(kwargs["branch"])
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "fetch_branch_tip", spy)

    def edit(repo: Path, origin: Path) -> None:
        _lane_work(repo)

    _, out = _run(worker_factory, monkeypatch, origin, tmp_path,
                  task_id="t-no-merge", edit=edit)

    assert out["published"] is True, out.get("publish_reason")
    assert fetched == [], fetched
