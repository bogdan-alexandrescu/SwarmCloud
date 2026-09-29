"""The leak scans and the replay run in a repository the worker made (#259).

WHAT WENT WRONG. Three security reviews of #259 each found the per-commit and
final-tree secret scans bypassable through git state the agent writes in its
own clone: `refs/replace/*`, `.git/info/grafts`, `.git/shallow`, and last a
forged `.git/objects/info/commit-graph`. `git diff` takes a commit's root tree
from the commit-graph when one is present, so an agent that rewrote the graph
entry of a commit showed the scan a clean tree while `commit-tree` and the
push carried the real one, key and all. Each fix closed one file; the next
review found another.

WHAT IS PINNED (owner decision, 2026-09-29). After the agent's processes are
reaped the worker creates a fresh repository outside `work/`, fetches the
agent's HEAD and the clone base into it with object checking, and runs the
kept-commit list, the per-commit scan, the fold, the final-tree scan, the
replay, the authorship check and the push there. No alternates point into
the agent's clone, and nothing the agent wrote under `.git` -- its config,
hooks, grafts, shallow file, replace refs or commit-graph -- is read by any
of those steps. Each attack below gets the secret caught, and nothing secret
is pushed.

Real git against a local `file://` remote; only the HTTP forge is faked.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
import struct
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle

from test_agent_commits_are_kept import (
    KEY,
    _all_object_bytes,
    _attempt,
    _branch_exists,
    _commit,
    _rev,
)
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    forge,
    local_urls,
    origin,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write_commit_graph(repo: Path) -> Path:
    """`git commit-graph write --reachable` in the agent's clone.

    git ignores a commit-graph in a shallow repository, so the agent first
    deletes `.git/shallow` -- which it can: the clone is its to edit, and a
    depth-1 clone of a one-commit origin has no history past the base.
    """
    (repo / ".git" / "shallow").unlink(missing_ok=True)
    _git(repo, "commit-graph", "write", "--reachable")
    graph = repo / ".git" / "objects" / "info" / "commit-graph"
    assert graph.is_file(), "git wrote no commit-graph"
    return graph


def _forge_commit_graph(repo: Path, trees: dict[str, str]) -> None:
    """Point each commit's commit-graph entry at a tree of the agent's choosing.

    The reviewer's forgery (scratchpad/review259b/probe_cg.py): the graph's
    CDAT chunk holds, per commit, the root tree git uses in place of reading
    the commit object. Rewriting it and re-signing the file's trailing hash
    makes `git diff A B` compare the forged trees, while `cat-file commit`,
    `commit-tree` and pack-objects still see the real ones.
    """
    graph = _write_commit_graph(repo)
    data = bytearray(graph.read_bytes())
    chunk_count = data[6]
    chunks = {}
    for i in range(chunk_count + 1):
        entry = 8 + 12 * i
        chunks[bytes(data[entry : entry + 4])] = struct.unpack(">Q", data[entry + 4 : entry + 12])[0]
    fanout, oids, cdat = chunks[b"OIDF"], chunks[b"OIDL"], chunks[b"CDAT"]
    count = struct.unpack(">I", data[fanout + 255 * 4 : fanout + 256 * 4])[0]
    listed = [bytes(data[oids + 20 * i : oids + 20 * i + 20]).hex() for i in range(count)]
    for commit, tree in trees.items():
        index = listed.index(commit)
        data[cdat + 36 * index : cdat + 36 * index + 20] = bytes.fromhex(tree)
    data[-20:] = hashlib.sha1(bytes(data[:-20])).digest()
    graph.chmod(stat.S_IRUSR | stat.S_IWUSR)
    graph.write_bytes(bytes(data))
    # The control that the forgery took: in the clone, git now reports the
    # forged tree for each commit.
    for commit, tree in trees.items():
        assert _git(repo, "rev-parse", f"{commit}^{{tree}}") == tree, "the forgery did not take"


# -- the forged commit-graph (#259, third security review) ------------------


def test_a_forged_commit_graph_does_not_hide_an_intermediate_commits_key(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """C1 adds the key, C2 an unrelated file, C3 deletes the key. The agent
    writes a commit-graph saying C1's tree is the base's and C2's is C3's, so
    in the clone every per-commit diff reads clean and the history was kept
    one commit at a time -- C1's rewrite, whose REAL tree holds `.env`, was
    pushed. In a clean repository there is no graph: C1's diff shows the key
    and the history folds to the clean final tree."""
    def edit(repo: Path) -> None:
        base = _rev(repo, "HEAD")
        (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        _commit(repo, "Configure the client")
        first = _rev(repo, "HEAD")
        (repo / "a.txt").write_text("a\n")
        _commit(repo, "Add a")
        second = _rev(repo, "HEAD")
        (repo / ".env").unlink()
        _commit(repo, "Stop committing the env file")
        third = _rev(repo, "HEAD")
        _forge_commit_graph(
            repo,
            {first: _rev(repo, f"{base}^{{tree}}"), second: _rev(repo, f"{third}^{{tree}}")},
        )

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-graph-commit", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert out.get("agent_commits_folded") == 3, out
    assert "agent_commits_kept" not in out, out
    files = tree_at(origin, branch)
    assert "a.txt" in files and ".env" not in files, files
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


def test_a_forged_commit_graph_does_not_hide_a_key_in_the_final_tree(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The agent commits `.env` and keeps it, then forges the BASE's graph
    entry to the final tree. `git diff base HEAD` then reads nothing added, so
    both the per-commit scan and the final-tree scan passed and the key was
    pushed. From a clean repository the final tree adds `.env`, and nothing
    is published."""
    def edit(repo: Path) -> None:
        base = _rev(repo, "HEAD")
        (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        _commit(repo, "Configure the client")
        _forge_commit_graph(repo, {base: _rev(repo, "HEAD^{tree}")})

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-graph-final", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is False, out
    assert out.get("final_tree_leak") == "the final tree adds a credential in .env; remove it", out
    assert not _branch_exists(origin, branch), "a branch was pushed"
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


# -- grafts, replace refs and the shallow file ------------------------------


def _key_added_then_deleted(repo: Path) -> tuple[str, str, str, str]:
    """base, then C1 adds the key, C2 an unrelated file, C3 deletes the key."""
    base = _rev(repo, "HEAD")
    (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
    _commit(repo, "Configure the client")
    first = _rev(repo, "HEAD")
    (repo / "a.txt").write_text("a\n")
    _commit(repo, "Add a")
    second = _rev(repo, "HEAD")
    (repo / ".env").unlink()
    _commit(repo, "Stop committing the env file")
    return base, first, second, _rev(repo, "HEAD")


def _assert_folded_and_clean(origin: Path, config, out: dict) -> None:
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert "agent_commits_kept" not in out, out
    assert out.get("agent_commits_folded"), out
    files = tree_at(origin, branch)
    assert "a.txt" in files and ".env" not in files, files
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


def test_a_graft_file_and_a_replace_graft_together_still_fold(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The reviewer's `replace_graft` probe: a `git replace --graft` puts C2
    on the base and `.git/info/grafts` puts C3 there too, so any walk that
    honours either skips the key's commit."""
    def edit(repo: Path) -> None:
        base, _first, second, third = _key_added_then_deleted(repo)
        _git(repo, "replace", "--graft", second, base)
        (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "info" / "grafts").write_text(f"{third} {base}\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-replace-graft", edit=edit, register=(KEY,)
    )
    _assert_folded_and_clean(origin, config, out)


def test_a_graft_that_makes_a_later_commit_a_root_still_folds(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """`.git/info/grafts` naming C2 with no parents: the walk from HEAD stops
    at C2 and never reaches the key's commit."""
    def edit(repo: Path) -> None:
        _base, _first, second, _third = _key_added_then_deleted(repo)
        (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "info" / "grafts").write_text(f"{second}\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-graft-root", edit=edit, register=(KEY,)
    )
    _assert_folded_and_clean(origin, config, out)


def test_a_replace_ref_over_the_key_commit_still_folds(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """`refs/replace/<C1>` naming a clean commit: any reader that honours
    replacement reads the clean commit's tree for C1."""
    def edit(repo: Path) -> None:
        base, first, _second, _third = _key_added_then_deleted(repo)
        clean = _git(repo, "commit-tree", _rev(repo, f"{base}^{{tree}}"), "-p", base, "-m", "clean")
        _git(repo, "replace", first, clean)

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-replace-commit", edit=edit, register=(KEY,)
    )
    _assert_folded_and_clean(origin, config, out)


@pytest.mark.parametrize("which", ["second", "head"])
def test_a_shallow_file_the_agent_edited_does_not_hide_the_key(
    worker_factory, monkeypatch, origin, local_urls, forge, which
):
    """`.git/shallow` naming C2 (or HEAD) makes git treat it as a root: the
    walk from HEAD never reaches the key's commit. The worker's repository
    takes its shallow boundary from the clone base it recorded, never from
    the agent's file."""
    def edit(repo: Path) -> None:
        _base, _first, second, third = _key_added_then_deleted(repo)
        (repo / ".git" / "shallow").write_text(f"{second if which == 'second' else third}\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id=f"t-shallow-{which}", edit=edit, register=(KEY,)
    )
    _assert_folded_and_clean(origin, config, out)


# -- a hostile .git/config ---------------------------------------------------


def _marker_program(tmp: Path, name: str) -> tuple[Path, Path]:
    """A program that records it ran, and prints nothing."""
    marker = tmp / f"{name}.ran"
    program = tmp / f"{name}.sh"
    program.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 0\n")
    program.chmod(0o755)
    return program, marker


@pytest.mark.parametrize(
    "setting",
    ["diff.external", "core.fsmonitor", "diff.submodule", "core.hooksPath"],
)
def test_a_hostile_git_config_does_not_hide_the_key_or_run(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path, setting
):
    """The agent's `.git/config`: an external diff program that prints
    nothing (so a diff read through it adds nothing), an fsmonitor hook, a
    `diff.submodule=diff`, a hooks directory. None of them reaches the scan,
    none of them runs, and the key is caught."""
    program, marker = _marker_program(tmp_path, setting.replace(".", "-"))

    def edit(repo: Path) -> None:
        _key_added_then_deleted(repo)
        if setting == "diff.submodule":
            _git(repo, "config", "diff.submodule", "diff")
            _git(repo, "config", "diff.external", str(program))
        elif setting == "core.hooksPath":
            hooks = tmp_path / "hooks"
            hooks.mkdir()
            for hook in ("pre-commit", "post-commit", "reference-transaction", "post-checkout"):
                shutil.copy(program, hooks / hook)
            _git(repo, "config", "core.hooksPath", str(hooks))
        else:
            _git(repo, "config", setting, str(program))
        (repo / "notes.txt").write_text("left uncommitted, so the worker commits in the clone\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id=f"t-config-{setting.replace('.', '-')}",
        edit=edit, register=(KEY,),
    )
    _assert_folded_and_clean(origin, config, out)
    assert not marker.exists(), f"the agent's {setting} program ran during the publish"


# -- the structure itself: scan, replay and push share one clean repository --


def test_the_scans_the_replay_and_the_push_run_in_one_worker_owned_repository(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Every step that decides what is pushed runs in the repository the
    worker built -- not in the clone, and not in a repository that borrows
    the clone's object store through alternates -- and that repository lives
    outside `work/`."""
    seen: dict[str, list[Path]] = {"prepare": [], "replay": [], "final": [], "verify": [], "push": []}
    real = {
        "prepare": lifecycle.prepare_publish_repo,
        "replay": lifecycle.replay_agent_commits,
        "final": lifecycle.final_tree_leak,
        "verify": lifecycle.verify_worker_authorship,
        "push": lifecycle.push_branch,
    }

    def recording(name: str, attribute: str):
        def wrapper(**kwargs):
            result = real[name](**kwargs)
            seen[name].append(Path(result if name == "prepare" else kwargs["repo"]))
            return result
        monkeypatch.setattr(lifecycle, attribute, wrapper)

    recording("prepare", "prepare_publish_repo")
    recording("replay", "replay_agent_commits")
    recording("final", "final_tree_leak")
    recording("verify", "verify_worker_authorship")
    recording("push", "push_branch")

    def edit(repo: Path) -> None:
        (repo / "one.txt").write_text("one\n")
        _commit(repo, "Add one")
        (repo / "two.txt").write_text("two\n")

    worker, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-clean-repo", edit=edit,
    )

    assert out["published"] is True, out.get("publish_reason")
    assert len(seen["prepare"]) == 1, seen
    clean = seen["prepare"][0]
    for step in ("replay", "final", "verify", "push"):
        assert seen[step] == [clean], (step, seen)
    clone = worker.ws.work / lifecycle.REPO_DIR_NAME
    assert clean.resolve() != clone.resolve()
    assert not clean.resolve().is_relative_to(worker.ws.work.resolve()), clean
    assert not (clean / ".git" / "objects" / "info" / "alternates").exists(), (
        "the worker's repository borrows the agent's object store"
    )
