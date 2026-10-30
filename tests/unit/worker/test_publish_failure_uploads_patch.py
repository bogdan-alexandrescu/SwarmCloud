"""A publish that fails after the agent ran still uploads swarm-work.patch (#2544).

MEASURED 2026-10-27: two integrate bundles (TOKEN-A, U01) lost their
integrator's whole run. The fix step's agent ran, then the publish-fetch into
the worker's clean repository failed (the #2559 shape: a merge of a branch
the agent fetched `--depth=N`), and the attempt ended with "No
swarm-work.patch was uploaded for this attempt". The harvest took its patch
from the clean repository, so a repository that could not be built meant no
patch at all.

Owner decision 2026-10-28: whenever the publish fails after the agent ran,
the worker uploads swarm-work.patch, so `swarm_apply` recovers the work
without re-running the step. The patch is the agent's TREE against the clone
base, so it needs none of the history the clone lacks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.gitops import GitError

from test_agent_commits_are_kept import _rev
from test_publish_scans_a_clean_repository import (
    _fetch_sibling_with_depth_and_merge,
    _origin_with_a_sibling,
)
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    forge,
    local_urls,
    origin,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: Spelled out rather than read from `lifecycle`, so the tests do not follow a rename.
PATCH_NAME = "swarm-work.patch"
DIRECT_PR = {"strategy": "direct-pr"}


def _cloned(worker_factory, monkeypatch, origin_repo: Path, *, task_id: str, **overrides):
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin_repo}",
        **overrides,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": dict(DIRECT_PR)}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    return worker, repo, _rev(repo, "HEAD")


def _finish(worker) -> tuple[dict[str, Any], str | None]:
    """The clean finish's order: harvest and upload, then the deferred
    publish, then the error the step ends with."""
    summary = worker._upload_outputs(defer_publish=True)
    worker._publish_checked(summary, None)
    return summary, worker._publish_failed(summary)


def _patch_uri(summary: dict[str, Any]) -> str | None:
    for entry in summary.get("artifacts") or []:
        if isinstance(entry, dict) and entry.get("name") == PATCH_NAME and entry.get("uri"):
            return entry["uri"]
    return None


def _agent_adds_binary_untracked_and_deletes(repo: Path) -> None:
    """Committed and uncommitted work together: a commit, an edit left
    uncommitted, an untracked binary file and a deleted file."""
    (repo / "committed.txt").write_text("committed by the agent\n")
    for args in (["add", "committed.txt"], ["commit", "-q", "-m", "agent commit"]):
        subprocess.run(["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
                        *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "README.md").unlink()
    (repo / "notes").mkdir()
    (repo / "notes" / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (repo / "uncommitted.txt").write_text("never committed\n")


def _tree(root: Path) -> dict[str, bytes]:
    """Every regular file under `root`, by path, without `.git` and the
    worker's `./artifacts` link (which is never the agent's work)."""
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts[0] in (".git", "artifacts") or path.is_symlink() or not path.is_file():
            continue
        out[rel.as_posix()] = path.read_bytes()
    return out


def _apply_onto_base(tmp_path: Path, origin_repo: Path, base: str, patch: Path) -> Path:
    target = tmp_path / "recovered"
    subprocess.run(["git", "clone", "-q", f"file://{origin_repo}", str(target)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(target), "checkout", "-q", base],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(target), "apply", "--binary", str(patch)],
                   check=True, capture_output=True)
    return target


def test_a_failed_publish_fetch_on_a_depth_fetched_merge_still_uploads_the_patch(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """The TOKEN-A / U01 shape: no confirmer, so the publish-fetch fails."""
    xs = _origin_with_a_sibling(origin, tmp_path)
    worker, repo, base = _cloned(worker_factory, monkeypatch, origin, task_id="t-2544-fetch")
    _fetch_sibling_with_depth_and_merge(origin, xs)(repo)
    _agent_adds_binary_untracked_and_deletes(repo)

    summary, error = _finish(worker)
    git = summary["git"]

    # The control: this is the publish-fetch failure, not some other one.
    assert str(git.get("error", "")).startswith(
        "could not fetch the agent's work into the worker's clean repository"
    ), git
    assert git["published"] is False
    # MUTATION: drop `out.update(self._harvest_tree_patch(...))` from the
    # harvest's GitError handler -> no patch, and these fail.
    assert git["patch"] == PATCH_NAME, git
    assert git.get("patch_from") == "tree", git
    uri = _patch_uri(summary)
    assert uri, summary.get("artifacts")
    # MUTATION: `_publish_failed` stops reading the artifacts -> the error
    # says "No swarm-work.patch was uploaded" again.
    assert error is not None and error.startswith("publish_failed: "), error
    assert uri in error and "swarm_apply" in error, error
    assert f"No {PATCH_NAME} was uploaded" not in error, error

    # The patch is the agent's tree: applied onto the base it reproduces it,
    # the depth-fetched sibling's files, the binary and the deletion included.
    # MUTATION: diff from the agent's HEAD instead of its working tree -> the
    # uncommitted and untracked files are missing from the recovered tree.
    recovered = _apply_onto_base(tmp_path, origin, base, worker.ws.artifacts / PATCH_NAME)
    assert _tree(recovered) == _tree(repo)
    assert {"x1.txt", "x5.txt", "notes/blob.bin", "uncommitted.txt", "committed.txt"} <= set(
        _tree(recovered)
    )
    assert "README.md" not in _tree(recovered)


def test_a_failing_push_still_uploads_the_patch_and_the_error_names_it(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    def refuse(**_kwargs: Any) -> str:
        raise GitError("push failed with exit 1: error: failed to push some refs")

    monkeypatch.setattr(lifecycle, "push_branch", refuse)
    worker, repo, base = _cloned(worker_factory, monkeypatch, origin, task_id="t-2544-push")
    _agent_adds_binary_untracked_and_deletes(repo)

    summary, error = _finish(worker)
    git = summary["git"]

    # The control: the push is what failed.
    assert git.get("publish_failed") is True, git
    assert "push failed" in str(git.get("publish_reason")), git
    # MUTATION: write the harvest's patch after the upload (or only on a
    # successful publish) -> no artifact, and the error says none was uploaded.
    assert git["patch"] == PATCH_NAME, git
    uri = _patch_uri(summary)
    assert uri, summary.get("artifacts")
    assert error is not None and uri in error and "swarm_apply" in error, error

    recovered = _apply_onto_base(tmp_path, origin, base, worker.ws.artifacts / PATCH_NAME)
    assert _tree(recovered) == _tree(repo)


def test_an_over_cap_tree_patch_records_why_in_patch_note(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    xs = _origin_with_a_sibling(origin, tmp_path)
    worker, repo, _base = _cloned(
        worker_factory, monkeypatch, origin, task_id="t-2544-cap", max_patch_bytes=64,
    )
    _fetch_sibling_with_depth_and_merge(origin, xs)(repo)
    _agent_adds_binary_untracked_and_deletes(repo)

    summary, error = _finish(worker)
    git = summary["git"]

    assert str(git.get("error", "")).startswith("could not fetch"), git
    # MUTATION: drop the over-cap branch of `_harvest_tree_patch` -> no
    # patch_note, and the error says only that nothing was uploaded.
    assert git["patch"] is None and git["patch_omitted"] is True, git
    assert "over the 64 cap" in str(git.get("patch_note")), git
    assert not (worker.ws.artifacts / PATCH_NAME).exists()
    assert _patch_uri(summary) is None
    assert error is not None and git["patch_note"] in error, error


def test_a_tree_patch_that_cannot_be_taken_says_why(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """Both repositories fail to build: `patch_note` names the second failure."""
    calls: list[str | None] = []

    def fail(**kwargs: Any):
        calls.append(kwargs.get("work_head"))
        raise GitError("could not initialise the worker's publish repository")

    worker, repo, base = _cloned(worker_factory, monkeypatch, origin, task_id="t-2544-none")
    (repo / "agent.txt").write_text("work\n")
    monkeypatch.setattr(lifecycle, "prepare_publish_repo", fail)

    summary, error = _finish(worker)
    git = summary["git"]

    # The publish repository, then the base-only one.
    assert calls == [None, base], calls
    # MUTATION: drop the `except` in `_harvest_tree_patch` -> the harvest
    # raises out, and `_upload_outputs` records only "failed unexpectedly".
    note = str(git.get("patch_note"))
    assert "could not be taken from the agent's tree" in note, git
    assert "could not initialise" in note, git
    assert error is not None and note in error, error
