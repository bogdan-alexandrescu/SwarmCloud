"""A branch that merged another task's branch publishes; a failed one keeps a bundle (#3174).

WHAT WENT WRONG (4 November 2026, task_a0ce1d9f5a634aa6afa7). A `publish_to_pr`
step's agent fetched another task's branch and merged it before committing,
and the publish failed at the clean repository's fetch: "Could not read
<sha>" / "unable to parse commit". The walk from HEAD crossed the merge into
the sibling's history, and the sibling joins the BASE's history below the
base, so the walk needed every shallow cut-off a deepened clone holds there
(one per side branch the deepen cut). The publish boundary took the recorded
base plus the lines origin confirmed (#2559), and origin is asked about at
most 16 lines -- or none at all where the attempt has no git tools relay --
so the rest were never added. The work was kept only as `swarm-work.patch`,
a flat patch that loses the merge.

WHAT IS PINNED (owner decision, 4 November 2026):

1. a shallow line that is an ANCESTOR OF THE RECORDED BASE joins the boundary
   without a question: the base is origin's and names its parents by hash.
   The sibling publishes whether the agent fetched it shallow or at full
   depth, with no relay at all, and its merge commit is what is published.
   A line the agent forged on its own commits is no ancestor of the base and
   is still never added unless origin confirms it.
2. a publish that still fails keeps `swarm-work.bundle` beside the patch:
   the agent's branch from the recorded base, merge commits included, one
   ref, named in the failure text and in the result, scanned commit by
   commit and under the patch's cap.

Real git against local repositories; only the forge is faked.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops, lifecycle

from test_agent_commits_are_kept import KEY
from test_publish_failure_uploads_patch import _cloned, _finish, _origin_confirms_no_cut_off
from test_publish_scans_a_clean_repository import (
    _fetch_sibling_with_depth_and_merge,
    _key_added_then_deleted,
    _origin_confirmer,
    _origin_with_a_sibling,
)
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    forge,
    local_urls,
    origin,
)

#: Spelled out rather than read from `gitops`, so the tests do not follow a rename.
BUNDLE_NAME = "swarm-work.bundle"
BUNDLE_REF = "refs/heads/swarm-work"
#: Side branches merged into main: past the 16 lines origin is asked about.
SIDE_BRANCHES = 20


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
         "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _empty_commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _deepened_clone_of_merged_main(tmp_path: Path) -> tuple[Path, Path, str]:
    """origin: main merges `SIDE_BRANCHES` side branches of 30 commits each,
    sibling S (s0..s2) leaves main after the last merge, and main moves on
    by two commits, so S joins the base's history below the base. The
    agent's clone is `--depth 1` of main, deepened to 25 as a pull-base or
    ancestry fetch deepens it, so its `.git/shallow` holds a cut-off on
    every side branch. Returns (origin, clone, base)."""
    src = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _empty_commit(src, "root")
    for i in range(SIDE_BRANCHES):
        _git(src, "checkout", "-q", "-b", f"side-{i}", "main")
        for j in range(30):
            _empty_commit(src, f"side {i}.{j}")
        _git(src, "checkout", "-q", "main")
        _git(src, "merge", "-q", "--no-ff", "-m", f"merge side {i}", f"side-{i}")
    _git(src, "checkout", "-q", "-b", "S")
    for k in range(3):
        (src / f"s{k}.txt").write_text(f"sibling {k}\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", f"s{k}")
    _git(src, "checkout", "-q", "main")
    for k in range(2):
        _empty_commit(src, f"main after S {k}")
    clone = tmp_path / "work" / "repo"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "main",
                    f"file://{src}", str(clone)], check=True, capture_output=True)
    base = _git(clone, "rev-parse", "HEAD")
    _git(clone, "fetch", "-q", "--depth=25", "origin", "main")
    return src, clone, base


def _merge_sibling_and_commit(clone: Path, *fetch_args: str) -> str:
    """The agent's shell: fetch S as given, merge it, commit on top. HEAD."""
    _git(clone, "fetch", "-q", *fetch_args, "origin", "S")
    _git(clone, "merge", "-q", "--no-ff", "--allow-unrelated-histories",
         "-m", "merge the sibling", "FETCH_HEAD")
    (clone / "mine.txt").write_text("the agent's own work\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "the agent's commit")
    return _git(clone, "rev-parse", "HEAD")


def _shallow(clone: Path) -> list[str]:
    return (clone / ".git" / "shallow").read_text().split()


def _logger():
    from agent_worker.logs import build_logger

    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id="acme",
                        generation=1, runner_profile="mock", stream=io.StringIO())


def _prepare(tmp_path: Path, clone: Path, base: str, **kwargs) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    return gitops.prepare_publish_repo(
        source_repo=clone, work_head=None, base=base, private_dir=tmp_path / "private",
        logs_dir=logs, timeout_seconds=60, logger=_logger(), **kwargs,
    )


def _is_ancestor(repo: Path, sha: str, of: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, of],
                          capture_output=True).returncode == 0


def _assert_merge_published(repo: Path, head: str) -> None:
    """The publish repository holds the agent's HEAD on its branch, and the
    merge under it with both parents."""
    assert _git(repo, "rev-parse", "HEAD") == head
    merge = _git(repo, "rev-parse", "HEAD~1")
    parents = _git(repo, "rev-list", "--parents", "-n", "1", merge).split()[1:]
    assert len(parents) == 2, parents
    for parent in parents:
        _git(repo, "cat-file", "-e", f"{parent}^{{commit}}")


# -- 1. the fetch -------------------------------------------------------------


def test_a_sibling_fetched_at_full_depth_publishes_with_its_merge_and_no_question(
    tmp_path: Path,
):
    src, clone, base = _deepened_clone_of_merged_main(tmp_path)
    head = _merge_sibling_and_commit(clone)
    lines = [sha for sha in _shallow(clone) if sha != base]
    # The control: more cut-offs than origin is ever asked about, every one
    # under the base.
    assert len(lines) > 16, len(lines)
    assert all(_is_ancestor(src, sha, base) for sha in lines)

    asked: list[list[str]] = []
    # MUTATION: drop `proven` from the boundary -> "Could not read <sha>".
    repo = _prepare(tmp_path, clone, base, confirm_on_origin=_origin_confirmer(src, asked))
    _assert_merge_published(repo, head)
    # Every line was proven an ancestor of the base, so none was asked.
    assert asked == [], asked
    boundary = (repo / ".git" / "swarm-source-shallow").read_text().split()
    assert boundary[0] == base and sorted(boundary[1:]) == sorted(lines)


def test_a_sibling_merge_publishes_with_no_relay_to_ask(tmp_path: Path):
    """The attempt is not offered the git tools: no confirmer at all."""
    _src, clone, base = _deepened_clone_of_merged_main(tmp_path)
    head = _merge_sibling_and_commit(clone)
    repo = _prepare(tmp_path, clone, base)
    _assert_merge_published(repo, head)


def test_a_shallow_sibling_asks_origin_only_about_lines_the_walk_meets(tmp_path: Path):
    """`--depth=10` reaches past where S left main, so S's fetch cuts the
    base's history above the deepen's cut-offs: those are proven, and the
    twenty deeper ones are now stale -- the walk from HEAD never meets them.
    A second sibling X, off the root, is cut where the base never reaches.
    Origin is asked about X's line alone, not about the first 16 lines."""
    src, clone, base = _deepened_clone_of_merged_main(tmp_path)
    root = _git(src, "rev-list", "--max-parents=0", "main")
    _git(src, "checkout", "-q", "-b", "X", root)
    xs = []
    for i in range(4):
        (src / f"x{i}.txt").write_text(f"x{i}\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", f"x{i}")
        xs.append(_git(src, "rev-parse", "HEAD"))
    _git(src, "checkout", "-q", "main")
    _git(clone, "fetch", "-q", "--depth=2", "origin", "X")
    _git(clone, "merge", "-q", "--no-ff", "--allow-unrelated-histories", "-m", "merge X",
         "FETCH_HEAD")
    head = _merge_sibling_and_commit(clone, "--depth=10")
    lines = [sha for sha in _shallow(clone) if sha != base]
    assert len(lines) > 16 + 1 and xs[2] in lines, (len(lines), xs)

    asked: list[list[str]] = []
    repo = _prepare(tmp_path, clone, base, confirm_on_origin=_origin_confirmer(src, asked))
    _assert_merge_published(repo, head.strip())
    # MUTATION: drop `_lines_head_reaches` -> the question is the first 16
    # lines in the file, stale ones among them.
    assert asked == [[xs[2]]], asked
    boundary = (repo / ".git" / "swarm-source-shallow").read_text().split()
    assert xs[2] in boundary


def test_without_origin_an_unproven_line_still_fails_the_publish(tmp_path: Path):
    """The guard: X's cut-off is no ancestor of the base, and nobody asked
    origin, so it is not added and the fetch fails as before."""
    src, clone, base = _deepened_clone_of_merged_main(tmp_path)
    root = _git(src, "rev-list", "--max-parents=0", "main")
    _git(src, "checkout", "-q", "-b", "X", root)
    for i in range(4):
        _empty_commit(src, f"x{i}")
    _git(src, "checkout", "-q", "main")
    _git(clone, "fetch", "-q", "--depth=2", "origin", "X")
    _git(clone, "merge", "-q", "--no-ff", "--allow-unrelated-histories", "-m", "merge X",
         "FETCH_HEAD")
    with pytest.raises(gitops.GitError) as raised:
        _prepare(tmp_path, clone, base)
    assert str(raised.value).startswith(
        "could not fetch the agent's work into the worker's clean repository"), raised.value
    with pytest.raises(gitops.GitError):
        _prepare(tmp_path, clone, base, confirm_on_origin=lambda shas: [])


@pytest.mark.parametrize("depth", [None, "--depth=2"])
def test_a_merge_of_a_sibling_publishes_at_either_depth(tmp_path: Path, depth: str | None):
    src, clone, base = _deepened_clone_of_merged_main(tmp_path)
    head = _merge_sibling_and_commit(clone, *([depth] if depth else []))
    asked: list[list[str]] = []
    repo = _prepare(tmp_path, clone, base, confirm_on_origin=_origin_confirmer(src, asked))
    _assert_merge_published(repo, head)
    for name in ("s0.txt", "s1.txt", "s2.txt", "mine.txt"):
        _git(repo, "cat-file", "-e", f"HEAD:{name}")


def test_a_forged_line_in_a_deepened_clone_is_never_added(tmp_path: Path):
    """The base-ancestry proof is the only new way a line joins, and a line
    the agent forged on its own commit is no ancestor of the base: without
    origin's word it stays out, and the key's commit is fetched (and so
    scanned)."""
    src, clone, base = _deepened_clone_of_merged_main(tmp_path)
    _merge_sibling_and_commit(clone)
    _base, first, second, third = _key_added_then_deleted(clone)
    with (clone / ".git" / "shallow").open("a") as handle:
        handle.write(f"{second}\n")

    for confirm in (None, _origin_confirmer(src, [])):
        kwargs = {} if confirm is None else {"confirm_on_origin": confirm}
        repo = _prepare(tmp_path, clone, base, **kwargs)
        boundary = (repo / ".git" / "swarm-source-shallow").read_text().split()
        # MUTATION: prove a line by its presence in the clone rather than by
        # `rev-list <base>` -> `second` joins and the key's commit is cut off.
        assert second not in boundary, boundary
        assert _git(repo, "rev-parse", "HEAD") == third
        _git(repo, "cat-file", "-e", f"{first}^{{commit}}")


def test_the_publish_fetch_keeps_its_object_checks():
    argv = gitops._publish_fetch_argv("git", Path("/clone"), "/boundary", ["+a:refs/x"])
    assert ["-c", "transfer.fsckObjects=true"] == argv[argv.index("transfer.fsckObjects=true") - 1:
                                                          argv.index("transfer.fsckObjects=true") + 1]
    assert "fetch.fsckObjects=true" in argv
    assert "--update-shallow" in argv
    upload = next(arg for arg in argv if arg.startswith("--upload-pack="))
    assert "GIT_SHALLOW_FILE=/boundary" in upload
    assert "core.hooksPath=/dev/null" in upload


# -- 2. the bundle a failed publish keeps -------------------------------------


def _keep(tmp_path: Path, clone: Path, base: str, *, leaks=lambda repo, commits: None,
          max_bytes: int = 64 * 1024 * 1024) -> tuple[gitops.WorkBundle, Path]:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    path = tmp_path / "artifacts" / BUNDLE_NAME
    kept = gitops.keep_work_bundle(
        source_repo=clone, base=base, private_dir=tmp_path / "private", logs_dir=logs,
        bundle_path=path, max_bytes=max_bytes, timeout_seconds=60, logger=_logger(),
        leaks=leaks,
    )
    return kept, path


def _recreate(tmp_path: Path, origin_repo: Path, bundle: Path) -> tuple[Path, str]:
    """A full clone of origin, as a hand push would start from: the bundle
    verifies there, and its one ref is fetched. (clone, the fetched sha)."""
    target = tmp_path / "recreated"
    subprocess.run(["git", "clone", "-q", "--no-local", str(origin_repo), str(target)],
                   check=True, capture_output=True)
    _git(target, "bundle", "verify", str(bundle))
    heads = _git(target, "bundle", "list-heads", str(bundle)).splitlines()
    assert [line.split()[1] for line in heads] == [BUNDLE_REF], heads
    _git(target, "fetch", "-q", str(bundle), BUNDLE_REF)
    return target, _git(target, "rev-parse", "FETCH_HEAD")


def test_a_failed_publish_of_a_merged_branch_keeps_a_bundle_that_recreates_the_merge(
    tmp_path: Path,
):
    """The #2559 shape with no relay: the sibling's own cut-off is not the
    base's, origin was not asked, so the publish fetch still fails -- and the
    bundle carries the branch, merge included."""
    src = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _empty_commit(src, "m1")
    _git(src, "checkout", "-q", "-b", "X")
    for i in range(1, 6):
        (src / f"x{i}.txt").write_text(f"x{i}\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", f"x{i}")
    _git(src, "checkout", "-q", "main")
    for i in (2, 3):
        (src / f"m{i}.txt").write_text(f"m{i}\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", f"m{i}")
    clone = tmp_path / "work" / "repo"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "main",
                    f"file://{src}", str(clone)], check=True, capture_output=True)
    base = _git(clone, "rev-parse", "HEAD")
    _git(clone, "fetch", "-q", "--depth=3", "origin", "X")
    _git(clone, "merge", "-q", "--no-ff", "--allow-unrelated-histories", "-m", "merge X",
         "FETCH_HEAD")
    merge = _git(clone, "rev-parse", "HEAD")
    (clone / "mine.txt").write_text("mine\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "mine")
    head = _git(clone, "rev-parse", "HEAD")
    # The control: this publish still fails.
    with pytest.raises(gitops.GitError):
        _prepare(tmp_path, clone, base)

    scanned: list[list[str]] = []
    kept, path = _keep(tmp_path, clone, base,
                       leaks=lambda repo, commits: scanned.append(list(commits)))
    assert kept.name == BUNDLE_NAME and path.is_file(), kept
    assert kept.base == base and kept.bytes == path.stat().st_size
    # Every bundled commit was scanned: the agent's, the merge, X's three.
    assert len(scanned) == 1 and kept.commits == len(scanned[0]) == 5, scanned
    _target, fetched = _recreate(tmp_path, src, path)
    assert fetched == head
    assert _git(_target, "rev-parse", f"{head}~1") == merge
    assert len(_git(_target, "rev-list", "--parents", "-n", "1", merge).split()) == 3


def test_a_bundle_is_not_kept_when_a_commit_in_it_leaks_or_it_is_over_the_cap(
    tmp_path: Path,
):
    _src, clone, base = _deepened_clone_of_merged_main(tmp_path)
    _merge_sibling_and_commit(clone)

    kept, path = _keep(tmp_path, clone, base, leaks=lambda repo, commits: commits[0])
    assert kept.name is None and not path.exists(), kept
    assert "credential-shaped" in str(kept.note), kept

    def broken(repo: Path, commits: list[str]) -> str | None:
        raise gitops.GitError("could not diff")

    kept, path = _keep(tmp_path, clone, base, leaks=broken)
    assert kept.name is None and not path.exists() and "could not be scanned" in str(kept.note)

    kept, path = _keep(tmp_path, clone, base, max_bytes=64)
    assert kept.name is None and kept.omitted and not path.exists(), kept
    assert "over the 64 cap" in str(kept.note), kept
    # No scratch is left behind in the worker's private folder.
    assert not list((tmp_path / "private").glob("bundle-*")), list((tmp_path / "private").iterdir())


def _bundle_uri(summary: dict[str, Any]) -> str | None:
    for entry in summary.get("artifacts") or []:
        if isinstance(entry, dict) and entry.get("name") == BUNDLE_NAME and entry.get("uri"):
            return entry["uri"]
    return None


def test_the_worker_keeps_and_names_the_bundle_when_the_publish_fetch_fails(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _origin_confirms_no_cut_off(monkeypatch)
    xs = _origin_with_a_sibling(origin, tmp_path)
    worker, repo, base = _cloned(worker_factory, monkeypatch, origin, task_id="t-3174-bundle")
    _fetch_sibling_with_depth_and_merge(origin, xs)(repo)
    merge = _git(repo, "rev-parse", "HEAD")
    (repo / "committed.txt").write_text("committed by the agent\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "agent commit")
    head = _git(repo, "rev-parse", "HEAD")

    summary, error = _finish(worker)
    git = summary["git"]
    # The control: the publish fetch failed.
    assert str(git.get("error", "")).startswith("could not fetch the agent's work"), git
    # MUTATION: drop `self._keep_work_bundle` from the harvest's GitError
    # handler -> no bundle, and nothing names one.
    assert git["bundle"] == BUNDLE_NAME, git
    assert git["bundle_base"] == base and git["bundle_ref"] == "swarm-work", git
    assert BUNDLE_NAME in git["error"] and BUNDLE_NAME in git["publish_reason"], git
    uri = _bundle_uri(summary)
    assert uri, summary.get("artifacts")
    assert error is not None and uri in error, error
    # The patch is still kept beside it.
    assert git["patch"] == "swarm-work.patch", git

    target, fetched = _recreate(tmp_path, origin, worker.ws.artifacts / BUNDLE_NAME)
    assert fetched == head
    assert _git(target, "rev-parse", f"{head}~1") == merge


def test_the_worker_keeps_no_bundle_of_a_branch_whose_commit_adds_a_key(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    _origin_confirms_no_cut_off(monkeypatch)
    xs = _origin_with_a_sibling(origin, tmp_path)
    worker, repo, _base = _cloned(worker_factory, monkeypatch, origin, task_id="t-3174-key")
    _fetch_sibling_with_depth_and_merge(origin, xs)(repo)
    (repo / "config.py").write_text(f'API_KEY = "{KEY}"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a key")
    (repo / "config.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the key removed")

    summary, error = _finish(worker)
    git = summary["git"]
    assert str(git.get("error", "")).startswith("could not fetch the agent's work"), git
    # MUTATION: pass a `leaks` that finds nothing -> the key's commit is
    # bundled and uploaded.
    assert git["bundle"] is None, git
    assert "credential-shaped" in str(git.get("bundle_note")), git
    assert not (worker.ws.artifacts / BUNDLE_NAME).exists()
    assert _bundle_uri(summary) is None
    assert KEY not in str(summary) and (error is None or KEY not in error)
    assert lifecycle.WORK_BUNDLE_NAME == BUNDLE_NAME
