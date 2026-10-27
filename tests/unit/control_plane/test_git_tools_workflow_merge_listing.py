"""swarm-api lists a merge's workflow files with `--diff-merges=combined`, as the worker does (#2452).

WHAT WENT WRONG. `GitTools._workflow_paths` read the pushed bundle with
`git log --name-only --no-renames -m`. `-m` diffs a merge against EACH parent,
so a lane's merge of `main` was diffed against its branch parent and listed
every workflow file `main` had changed since the branch was cut. A ci-fix or
conflict-fix step that merged `main` was routed to `git_push_branch` for
`main`'s own workflow changes, and twice on 2026-10-26 the step ended FAILED
with its work in `workflow-lines.patch`.

WHAT IS PINNED, with real git over a real thin bundle read the way
`git_push_branch` reads it (a scratch repository holding the bundle's
prerequisites at depth 1, `gitexec.GitExec(local_remotes=True)`):

* a clean merge of `main` lists none of `main`'s workflow files;
* a merge whose result differs from every parent in a workflow file -- an evil
  merge, or a conflict resolved to a third version -- is still listed;
* a merge that keeps the branch's own earlier version of a file `main`
  changed is NOT listed (the known consequence, docs/runner-git-tools.md):
  the worker's own push carries it;
* a non-merge commit lists as before, a rename away from `.github/workflows/`
  included;
* both of the merge's parents being shallow prerequisites (the clone base and
  the merged `main` commit) neither fails the listing nor lists everything;
* the worker's `Worker._workflow_paths` and swarm-api's listing agree on
  every one of these graphs, so they never disagree about a branch's route.

No credentials, no network: every token is built at runtime.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from agent_worker import lifecycle
from swarm_api.gitexec import GitExec
from swarm_api.gittools import GitTools

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

WF = ".github/workflows/x.yml"
OTHER_WF = ".github/workflows/y.yml"

_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "LC_ALL": "C",
}


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=check,
                         capture_output=True, text=True)
    return out.stdout.strip()


def _write(work: Path, path: str, text: str) -> None:
    (work / path).parent.mkdir(parents=True, exist_ok=True)
    (work / path).write_text(text + "\n")


def _commit(work: Path, message: str) -> str:
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD")


@dataclass
class Graph:
    """A lane's work repository and its remote: what the publish reads."""

    work: Path
    remote: Path
    base: str          # the clone base: the branch as origin had it
    floor: str | None  # the default branch's commit the lane merged, if it did


def _world(tmp: Path) -> tuple[Path, Path, str]:
    """A bare remote whose `main` holds a README, an ordinary file and `x.yml`."""
    tmp.mkdir(parents=True, exist_ok=True)
    remote = tmp / "remote.git"
    _git(tmp, "init", "-q", "--bare", str(remote))
    work = tmp / "work"
    _git(tmp, "init", "-q", "--initial-branch=main", str(work))
    _write(work, "README.md", "readme")
    _write(work, "f.txt", "f")
    _write(work, WF, "name: x v1")
    root = _commit(work, "root")
    _git(work, "push", "-q", str(remote), "HEAD:refs/heads/main")
    return work, remote, root


def _main_changes(work: Path, remote: Path, root: str, path: str, text: str) -> str:
    """`main` gains a commit after the branch was cut; the work clone is back on the branch."""
    branch = _git(work, "rev-parse", "--abbrev-ref", "HEAD")
    _git(work, "checkout", "-q", "-B", "upstream", root)
    _write(work, path, text)
    tip = _commit(work, f"main changes {path}")
    _git(work, "push", "-q", str(remote), "HEAD:refs/heads/main")
    _git(work, "checkout", "-q", branch)
    return tip


def _lane(tmp: Path, *, already_pushed: Callable[[Path], None] | None = None) -> tuple[Path, Path, str, str]:
    """The lane's branch, cut from root. `already_pushed` makes commits that
    origin's branch holds before this publish: the clone base moves past them."""
    work, remote, root = _world(tmp)
    _git(work, "checkout", "-q", "-b", "lane")
    if already_pushed is not None:
        already_pushed(work)
        _git(work, "push", "-q", str(remote), "HEAD:refs/heads/lane")
    base = _git(work, "rev-parse", "HEAD")
    return work, remote, root, base


def _merge(work: Path, tip: str, *, resolve: Callable[[Path], None] | None = None) -> None:
    """Merge `main`'s tip; `resolve` edits the result before the merge commits."""
    _git(work, "merge", "-q", "--no-ff", "--no-commit", tip, check=False)
    if resolve is not None:
        resolve(work)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "--no-edit")


# -- the graphs ---------------------------------------------------------------


def clean_merge_of_main(tmp: Path) -> Graph:
    """The lane changes an ordinary file and merges `main`, which changed `x.yml`."""
    work, remote, root, base = _lane(tmp)
    _write(work, "f.txt", "lane")
    _commit(work, "lane work")
    tip = _main_changes(work, remote, root, WF, "name: x from main")
    _merge(work, tip)
    return Graph(work, remote, base, tip)


def evil_merge(tmp: Path) -> Graph:
    """A clean merge of `main` whose result also edits `x.yml` to a third version."""
    work, remote, root, base = _lane(tmp)
    _write(work, "f.txt", "lane")
    _commit(work, "lane work")
    tip = _main_changes(work, remote, root, WF, "name: x from main")
    _merge(work, tip, resolve=lambda w: _write(w, WF, "name: x from the merge"))
    return Graph(work, remote, base, tip)


def _lane_already_changed_x(work: Path) -> None:
    _write(work, WF, "name: x from the lane")
    _commit(work, "lane changes x.yml (pushed earlier)")


def conflict_resolved_to_a_third_version(tmp: Path) -> Graph:
    """The lane's earlier, already pushed change to `x.yml` conflicts with
    `main`'s; the merge resolves it to a version neither side had."""
    work, remote, root, base = _lane(tmp, already_pushed=_lane_already_changed_x)
    tip = _main_changes(work, remote, root, WF, "name: x from main")
    _merge(work, tip, resolve=lambda w: _write(w, WF, "name: x resolved"))
    return Graph(work, remote, base, tip)


def conflict_resolved_to_mains_version(tmp: Path) -> Graph:
    """As above, resolved to `main`'s version: the branch's `x.yml` changes."""
    work, remote, root, base = _lane(tmp, already_pushed=_lane_already_changed_x)
    tip = _main_changes(work, remote, root, WF, "name: x from main")
    _merge(work, tip, resolve=lambda w: _write(w, WF, "name: x from main"))
    return Graph(work, remote, base, tip)


def conflict_kept_the_branchs_version(tmp: Path) -> Graph:
    """THE KNOWN CONSEQUENCE: resolved to the lane's own earlier version. The
    result equals the branch parent, so the merge is not listed."""
    work, remote, root, base = _lane(tmp, already_pushed=_lane_already_changed_x)
    tip = _main_changes(work, remote, root, WF, "name: x from main")
    _merge(work, tip, resolve=lambda w: _write(w, WF, "name: x from the lane"))
    return Graph(work, remote, base, tip)


def branch_commit_changes_a_workflow(tmp: Path) -> Graph:
    """A lane commit of its own adds `y.yml`, then the lane merges `main`."""
    work, remote, root, base = _lane(tmp)
    _write(work, OTHER_WF, "name: y")
    _commit(work, "lane adds y.yml")
    tip = _main_changes(work, remote, root, WF, "name: x from main")
    _merge(work, tip)
    return Graph(work, remote, base, tip)


def rename_away(tmp: Path) -> Graph:
    """A lane commit moves `x.yml` out of `.github/workflows/`; no merge."""
    work, remote, _root, base = _lane(tmp)
    (work / "ci").mkdir()
    _git(work, "mv", WF, "ci/x.yml")
    _commit(work, "lane moves x.yml away")
    return Graph(work, remote, base, None)


GRAPHS: dict[str, tuple[Callable[[Path], Graph], list[str]]] = {
    "clean_merge_of_main": (clean_merge_of_main, []),
    "evil_merge": (evil_merge, [WF]),
    "conflict_resolved_to_a_third_version": (conflict_resolved_to_a_third_version, [WF]),
    "conflict_resolved_to_mains_version": (conflict_resolved_to_mains_version, []),
    "conflict_kept_the_branchs_version": (conflict_kept_the_branchs_version, []),
    "branch_commit_changes_a_workflow": (branch_commit_changes_a_workflow, [OTHER_WF]),
    "rename_away": (rename_away, [WF]),
}


# -- the two listings -----------------------------------------------------------


def _negatives(graph: Graph) -> list[str]:
    return [sha for sha in (graph.base, graph.floor) if sha]


def swarm_api_listing(graph: Graph, tmp: Path) -> list[str]:
    """swarm-api's `GitTools._workflow_paths` over the thin bundle the worker
    would hand `git_push_branch`: HEAD over the clone base and the merged tip."""
    bundle = tmp / f"push-{secrets.token_hex(4)}.bundle"
    _git(graph.work, "bundle", "create", str(bundle), "lane",
         *[f"^{sha}" for sha in _negatives(graph)])
    tools = GitTools.__new__(GitTools)
    tools.gitexec = GitExec(scratch_root=tmp / "scratch", local_remotes=True)
    token = "ghs" + "_" + secrets.token_hex(18)  # runtime-built, never a literal
    return tools._workflow_paths(f"file://{graph.remote}", token, bundle.read_bytes())


class _Quiet:
    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


def worker_listing(graph: Graph, tmp: Path) -> list[str]:
    """The worker's `Worker._workflow_paths` over the same repository, with
    the publish's own negatives (`_workflow_negatives`)."""
    private, logs = tmp / "ws-private", tmp / "ws-logs"
    private.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    worker = SimpleNamespace(
        ws=SimpleNamespace(private=private, logs=logs),
        cfg=SimpleNamespace(git_harvest_timeout_seconds=60),
        log=_Quiet(),
        _publish_base=graph.base,
    )
    worker._workflow_negatives = lambda floor: lifecycle.Worker._workflow_negatives(worker, floor)
    return lifecycle.Worker._workflow_paths(worker, graph.work, floor=graph.floor)


def _minus_m(graph: Graph) -> list[str]:
    """What the listing read before #2452, for the controls below."""
    out = _git(graph.work, "log", "--format=", "--name-only", "--no-renames", "-m", "HEAD",
               "--not", *_negatives(graph), "--", ".github/workflows", ".github/actions")
    return sorted({line for line in out.splitlines() if line.strip()})


# -- the cases ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GRAPHS))
def test_swarm_api_lists_a_merge_only_where_its_result_differs_from_every_parent(name, tmp_path):
    build, expected = GRAPHS[name]
    graph = build(tmp_path)
    # MUTATION: put `-m` back in `GitTools._workflow_paths` -> a clean merge of
    # main lists main's x.yml, and the step routes to git_push_branch (#2452).
    # MUTATION: drop the merge listing to `--no-merges` -> the evil merge and
    # the third-version resolution are not listed and go out on the tenant token.
    # MUTATION: drop `--no-renames` -> the rename away lists only ci/x.yml.
    assert swarm_api_listing(graph, tmp_path) == expected


@pytest.mark.parametrize("name", sorted(GRAPHS))
def test_the_worker_and_swarm_api_agree_on_every_graph(name, tmp_path):
    build, expected = GRAPHS[name]
    graph = build(tmp_path)
    # MUTATION: change one of the two listings alone -> they disagree here, and
    # the worker routes a branch swarm-api reads differently.
    assert worker_listing(graph, tmp_path) == swarm_api_listing(graph, tmp_path) == expected


def test_the_controls_minus_m_listed_mains_files_through_the_merge(tmp_path):
    """The measurement could have come out the other way: on the same graphs
    `-m` lists what `--diff-merges=combined` leaves out."""
    assert _minus_m(clean_merge_of_main(tmp_path / "clean")) == [WF]
    assert _minus_m(conflict_kept_the_branchs_version(tmp_path / "kept")) == [WF]


def test_both_merge_parents_shallow_in_the_scratch_repo_still_lists_correctly(tmp_path):
    """The merge's parents are exactly the bundle's two prerequisites, which
    the scratch repository fetches at depth 1. The combined diff reads both
    trees, which a depth-1 fetch holds, so it neither fails nor lists every
    file in the tree."""
    work, remote, root, base = _lane(tmp_path)
    tip = _main_changes(work, remote, root, WF, "name: x from main")
    _merge(work, tip)
    graph = Graph(work, remote, base, tip)
    parents = _git(work, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert sorted(parents) == sorted([base, tip]), "the merge's parents are not both prerequisites"
    assert swarm_api_listing(graph, tmp_path) == []

    evil = Graph(*_evil_on_shallow_parents(tmp_path / "evil"))
    assert swarm_api_listing(evil, tmp_path / "evil") == [WF]


def _evil_on_shallow_parents(tmp: Path) -> tuple[Path, Path, str, str]:
    work, remote, root, base = _lane(tmp)
    tip = _main_changes(work, remote, root, WF, "name: x from main")
    _merge(work, tip, resolve=lambda w: _write(w, WF, "name: x from the merge"))
    return work, remote, base, tip
