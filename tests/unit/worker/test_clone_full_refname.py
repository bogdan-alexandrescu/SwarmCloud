"""A `repository_ref` that is a full refname is cloned, not refused at clone (#3347).

MEASURED 2026-11-05 (issue 3347): two tasks with `repository_ref`
`refs/pull/1267/head` were admitted and failed at clone, "Could not find
remote branch refs/pull/1267/head to clone": `git clone --branch` takes a
branch or tag NAME, and GitHub serves a pull request's head and merge commit
only as `refs/pull/<n>/head` and `refs/pull/<n>/merge`, which no branch name
reaches. A ref starting `refs/` now takes the explicit path a sha takes --
init, remote add, a depth-1 fetch of the ref, checkout FETCH_HEAD -- and a
branch, a tag and a sha clone as they did.

Real git against a bare origin on disk that carries the refs GitHub serves for
a pull request; `local_urls` lets git reach it and nothing else. The argv tests
fake `run_child` only to pin the steps each kind of ref runs.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_worker import gitops, lifecycle, workspace as workspace_mod
from agent_worker.gitops import GitError, validate_ref
from agent_worker.logs import build_logger

from worker_seeds import TENANT
from test_strategy_end_to_end import local_urls  # noqa: F401 - fixture used by name

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PULL_HEAD = "refs/pull/7/head"
PULL_MERGE = "refs/pull/7/merge"


def _logger():
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="mock", stream=io.StringIO(),
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def pull_origin(tmp_path: Path) -> dict[str, object]:
    """A bare origin as GitHub serves a repository with one open pull request:
    `main`, a tag `v1` behind it, and the pull request's head and merge commit
    under `refs/pull/7/`, with the contributor's branch itself NOT on origin
    (a fork's, or one deleted since)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("first\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "first")
    tagged = _git(seed, "rev-parse", "HEAD")
    _git(seed, "tag", "v1")
    (seed / "README.md").write_text("second\n")
    _git(seed, "commit", "--quiet", "-am", "second")
    main = _git(seed, "rev-parse", "HEAD")
    _git(seed, "checkout", "--quiet", "-b", "contributor")
    (seed / "feature.txt").write_text("the pull request's change\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "the pull request's change")
    head = _git(seed, "rev-parse", "HEAD")
    _git(seed, "checkout", "--quiet", "main")
    _git(seed, "merge", "--quiet", "--no-ff", "-m", "merge pull request 7", "contributor")
    merge = _git(seed, "rev-parse", "HEAD")
    _git(seed, "reset", "--quiet", "--hard", main)

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
                   check=True, capture_output=True)
    _git(bare, "update-ref", PULL_HEAD, head)
    _git(bare, "update-ref", PULL_MERGE, merge)
    _git(bare, "update-ref", "-d", "refs/heads/contributor")
    return {"path": bare, "main": main, "tag": tagged, "head": head, "merge": merge}


def _clone(tmp_path: Path, origin: Path, ref: str, **kwargs):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    destination = ws.work / "repo"
    result = gitops.shallow_clone(
        url=f"file://{origin}", ref=ref, destination=destination, private_dir=ws.private,
        logs_dir=ws.logs, timeout_seconds=60, logger=_logger(), **kwargs,
    )
    return result, destination


@pytest.mark.parametrize(
    ("ref", "lands_on"),
    [
        (PULL_HEAD, "head"),
        (PULL_MERGE, "merge"),
        ("refs/heads/main", "main"),
        ("refs/tags/v1", "tag"),
        ("main", "main"),
        ("v1", "tag"),
    ],
)
def test_every_kind_of_ref_clones_onto_its_commit(
    tmp_path, local_urls, pull_origin, ref, lands_on  # noqa: F811 - pytest fixture
):
    result, destination = _clone(tmp_path, pull_origin["path"], ref)
    want = pull_origin[lands_on]
    assert result.commit == want
    assert _git(destination, "rev-parse", "HEAD") == want
    assert result.ref == ref
    # One commit deep, as every other clone.
    assert _git(destination, "rev-list", "--count", "HEAD") == "1"


def test_a_sha_still_clones_onto_its_commit(tmp_path, local_urls, pull_origin):  # noqa: F811
    result, destination = _clone(tmp_path, pull_origin["path"], str(pull_origin["main"]))
    assert result.commit == pull_origin["main"]
    assert _git(destination, "rev-parse", "HEAD") == pull_origin["main"]


def test_a_pull_request_clone_tracks_every_branch_as_a_branch_clone_does(
    tmp_path, local_urls, pull_origin  # noqa: F811
):
    """`git fetch origin main` afterwards moves `origin/main` (#453), and the
    clone moved nothing but the pull request's commit: no tag, no branch."""
    result, destination = _clone(tmp_path, pull_origin["path"], PULL_HEAD)
    assert result.commit == pull_origin["head"]
    assert _git(destination, "tag", "--list") == ""
    assert _git(destination, "for-each-ref", "refs/remotes/") == ""
    _git(destination, "fetch", "--quiet", "--depth", "1", "origin", "main")
    assert _git(destination, "rev-parse", "refs/remotes/origin/main") == pull_origin["main"]


def test_a_pull_request_clone_fetches_its_base_as_a_branch_clone_does(
    tmp_path, local_urls, pull_origin  # noqa: F811
):
    """`pull_base` (#2901) runs after the fetch path exactly as after a clone:
    the base lands as `origin/main`, deep enough to reach the merge base."""
    result, destination = _clone(tmp_path, pull_origin["path"], PULL_HEAD, pull_base="main")
    assert result.base is not None
    assert result.base["merge_base"] == pull_origin["main"], result.base
    assert _git(destination, "rev-parse", "refs/remotes/origin/main") == pull_origin["main"]
    assert _git(destination, "rev-parse", "HEAD") == pull_origin["head"]


# -- the steps each kind of ref runs ------------------------------------------


def _record_steps(monkeypatch, *, fail_when=lambda argv: False) -> list[list[str]]:
    calls: list[list[str]] = []

    class _Result:
        timed_out = False
        duration_seconds = 0.1

        def __init__(self, code: int) -> None:
            self.exit_code = code

    def fake_run_child(argv, **kwargs):
        calls.append(list(argv))
        kwargs["stdout_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["stdout_path"].write_text("9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304\n")
        failed = fail_when(argv)
        kwargs["stderr_path"].write_text(
            "fatal: remote error: upload-pack: not our ref\n" if failed else "")
        return _Result(128 if failed else 0)

    monkeypatch.setattr(gitops, "run_child", fake_run_child)
    return calls


def _verbs(calls: list[list[str]], destination: Path) -> list[list[str]]:
    """Each call from its verb on: what follows `-C <destination>`, or `clone`."""
    out = []
    for argv in calls:
        if "clone" in argv:
            out.append(argv[argv.index("clone"):])
        elif "init" in argv:
            out.append(argv[argv.index("init"):])
        elif str(destination) in argv:
            out.append(argv[argv.index(str(destination)) + 1:])
    return out


@pytest.mark.parametrize("ref", [PULL_HEAD, PULL_MERGE, "refs/heads/main"])
def test_a_full_refname_is_fetched_and_checked_out_as_a_sha_is(tmp_path, monkeypatch, ref):
    calls = _record_steps(monkeypatch)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    destination = ws.work / "repo"
    url = "https://github.com/example-org/repo.git"
    gitops.shallow_clone(
        url=url, ref=ref, destination=destination, private_dir=ws.private,
        logs_dir=ws.logs, timeout_seconds=30, logger=_logger(),
    )
    steps = _verbs(calls, destination)
    assert steps[:4] == [
        ["init", "--quiet", str(destination)],
        ["remote", "add", "origin", url],
        ["fetch", "--depth", "1", "--no-tags", "origin", ref],
        ["checkout", "--quiet", "FETCH_HEAD"],
    ]
    assert not any("--branch" in argv or "clone" in argv for argv in calls)


@pytest.mark.parametrize("ref", ["main", "lane/x", "v1"])
def test_a_branch_or_tag_name_is_still_cloned_with_branch(tmp_path, monkeypatch, ref):
    calls = _record_steps(monkeypatch)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    destination = ws.work / "repo"
    gitops.shallow_clone(
        url="https://github.com/example-org/repo.git", ref=ref, destination=destination,
        private_dir=ws.private, logs_dir=ws.logs, timeout_seconds=30, logger=_logger(),
    )
    clone = _verbs(calls, destination)[0]
    assert clone[:clone.index("--")] == [
        "clone", "--depth", "1", "--no-tags", "--single-branch", "--branch", ref]


def test_a_sha_is_still_fetched_and_checked_out(tmp_path, monkeypatch):
    sha = "9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304"
    calls = _record_steps(monkeypatch)
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    destination = ws.work / "repo"
    gitops.shallow_clone(
        url="https://github.com/example-org/repo.git", ref=sha, destination=destination,
        private_dir=ws.private, logs_dir=ws.logs, timeout_seconds=30, logger=_logger(),
    )
    steps = _verbs(calls, destination)
    assert steps[2] == ["fetch", "--depth", "1", "--no-tags", "origin", sha]
    assert steps[3] == ["checkout", "--quiet", "FETCH_HEAD"]


@pytest.mark.parametrize(
    ("branch", "fetched"),
    [
        (PULL_HEAD, PULL_HEAD),
        ("refs/heads/main", "refs/heads/main"),
        ("main", "refs/heads/main"),
        (None, "HEAD"),
    ],
)
def test_the_base_pins_fall_back_fetches_a_full_refname_as_it_is(
    tmp_path, monkeypatch, branch, fetched
):
    """`clone_at_commit` falls back to the ref's history when the forge refuses
    a fetch by sha; a full refname is fetched as itself, never as the branch
    `refs/heads/refs/pull/7/head`, which does not exist."""
    pinned = "9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304"
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    destination = ws.work / "repo"
    calls = _record_steps(
        monkeypatch,
        fail_when=lambda argv: "fetch" in argv and "--depth" in argv and argv[-1] == pinned,
    )
    result = gitops.clone_at_commit(
        url="https://github.com/example-org/repo.git", branch=branch, commit=pinned,
        destination=destination, private_dir=ws.private, logs_dir=ws.logs,
        timeout_seconds=30, logger=_logger(),
    )
    assert result.commit == pinned
    fallback = [s for s in _verbs(calls, destination) if s[:1] == ["fetch"] and "--depth" not in s]
    assert fallback == [["fetch", "--no-tags", "origin", fetched]]


@pytest.mark.parametrize(
    "ref",
    [
        "refs/pull/7/../../heads/main",
        "-refs/pull/7/head",
        "refs/pull/7/head:refs/heads/main",
        "+refs/pull/7/head",
        "refs/pull/*/head",
        "refs/pull/7/head extra",
    ],
)
def test_a_full_refname_is_held_to_the_same_rules_as_any_ref(ref):
    """A refspec's `:`, `+` or `*`, a `..` or a leading `-` would let a caller
    write a local ref or reach an option through the fetch; all stay refused."""
    with pytest.raises(GitError):
        validate_ref(ref)


# -- the pre-publish rebase reads the ref as a branch ---------------------------


def _rebase_ref(ref: str | None, checkout: Path) -> str | None:
    worker = SimpleNamespace(
        _dispatch_carrier=lambda: None, _pr_role=lambda: None,
        _dispatch_strategy=lambda: "direct-pr",
    )
    return lifecycle.Worker._prepublish_rebase_ref(
        worker, ref=ref, checkout=checkout, fixed_base=False)


@pytest.mark.parametrize(
    ("ref", "onto"),
    [
        ("main", "main"),
        ("refs/heads/main", "main"),
        ("refs/heads/lane/x", "lane/x"),
        (PULL_HEAD, None),
        (PULL_MERGE, None),
        ("refs/tags/v1", None),
        ("9f3a1c2b4d5e6f70819a2b3c4d5e6f7081920304", None),
    ],
)
def test_only_a_branch_is_offered_as_the_pre_publish_rebase_base(tmp_path, ref, onto):
    """The runner rebases onto `origin/<name>` (`prepublish`), a branch: a full
    branch refname is offered as its name, and a pull request's ref -- no
    branch, and a merge commit GitHub rewrites -- is offered nothing, as a sha."""
    assert _rebase_ref(ref, tmp_path) == onto
