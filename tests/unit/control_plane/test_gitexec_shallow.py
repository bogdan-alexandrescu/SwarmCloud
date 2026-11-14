"""The git_fetch route serves a shallow clone (#2901; docs/design/git-fetch-shallow-clones.md).

WHAT WENT WRONG. A depth-1 clone hands its head H as its only have. The
scratch repository fetched `ref` with no depth, so it landed the remote's
whole history, and the bundle `want ^H` held every commit down to the root:
over the cap on a large repository, and even when it fitted the clone found
no merge base, because H stays grafted in its `.git/shallow`.

WHAT IS PINNED (design §5, option (a)): the worker sends its shallow
boundary; swarm-api fetches `ref` and each boundary commit at a depth that
doubles from 64 until they share a merge base (at most 8 rounds, a fetch
total of 512 MiB measured from the empty scratch repository), and bundles
the slice less the boundary commits' trees, with the scratch repository's
new `shallow` lines, the `unshallow` boundary commits and the `merge_base`
in the answer. Every forge fetch on that path carries `--depth=`.

Real git against `file://` bare repositories; no network, no credentials.
"""
from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path

import pytest

from swarm_api.gitexec import WANT_REF, GitExec, GitExecError, run_git

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


def _git(cwd: Path, *args: str, stdin: str | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                         capture_output=True, text=True, input=stdin)
    return out.stdout.strip()


def _token() -> str:
    return "ghs" + "_" + secrets.token_hex(18)


def _linear_history(work: Path, commits: int, *, big: bool = True) -> None:
    """`commits` commits on main, each changing `log.txt`; the first adds a
    1 MiB incompressible blob, so a whole-history bundle is not small.
    Built with fast-import: hundreds of commits in one git process."""
    blob = os.urandom(1024 * 1024) if big else b""
    stream = bytearray()
    for i in range(commits):
        msg = f"c{i}\n".encode()
        log = f"line {i}\n".encode()
        stream += b"commit refs/heads/main\n"
        stream += f"committer t <t@example.invalid> {1_700_000_000 + i} +0000\n".encode()
        stream += f"data {len(msg)}\n".encode() + msg
        stream += f"M 100644 inline log.txt\ndata {len(log)}\n".encode() + log + b"\n"
        if i == 0 and blob:
            stream += f"M 100644 inline big.bin\ndata {len(blob)}\n".encode() + blob + b"\n"
    subprocess.run(["git", "fast-import", "--quiet"], cwd=work, env=_ENV, check=True,
                   input=bytes(stream), capture_output=True)


def _origin(tmp_path: Path, *, commits: int, gap: int) -> tuple[Path, str, str]:
    """A bare origin: `commits` on main; `feature` = main~gap plus one commit H.
    Returns (origin, H, M) with M = the fork point."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True,
                   env=_ENV)
    _linear_history(origin, commits)
    fork = _git(origin, "rev-parse", f"main~{gap}")
    tree = _git(origin, "rev-parse", f"{fork}^{{tree}}")
    head = _git(origin, "commit-tree", tree, "-p", fork, "-m", "H", stdin="")
    _git(origin, "update-ref", "refs/heads/feature", head)
    return origin, head, fork


def _clone(tmp_path: Path, origin: Path) -> Path:
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--single-branch", "--branch",
                    "feature", f"file://{origin}", str(clone)], check=True, env=_ENV,
                   capture_output=True)
    return clone


def _shallow(clone: Path) -> list[str]:
    path = clone / ".git" / "shallow"
    return path.read_text().split() if path.exists() else []


def _apply(clone: Path, fetched, bundle: Path) -> None:
    """What the worker does (design §5.7): write `(old - unshallow) | shallow`,
    then fetch the bundle with the object checks on."""
    old = _shallow(clone)
    new = [s for s in old if s not in fetched.unshallow]
    new += [s for s in fetched.boundary if s not in new]
    (clone / ".git" / "shallow").write_text("".join(f"{s}\n" for s in new))
    _git(clone, "-c", "transfer.fsckObjects=true", "fetch", "-q", str(bundle),
         f"+{WANT_REF}:refs/remotes/origin/main")


def _exec(tmp_path: Path, **kwargs) -> GitExec:
    return GitExec(scratch_root=tmp_path / "scratch", local_remotes=True, **kwargs)


def _fetch(tmp_path: Path, gx: GitExec, origin: Path, head: str, **kwargs):
    out = tmp_path / "got.bundle"

    def deliver(path: Path, size: int) -> Path:
        out.write_bytes(path.read_bytes())
        return out

    fetched = gx.fetch_bundle(url=f"file://{origin}", token=_token(), ref="main",
                              haves=[head], deliver=deliver, **kwargs)
    return fetched, out


def test_depth1_clone_three_behind_gets_a_small_bundle_that_applies(tmp_path: Path):
    origin, head, fork = _origin(tmp_path, commits=300, gap=3)
    clone = _clone(tmp_path, origin)
    assert _shallow(clone) == [head]
    fetched, bundle = _fetch(tmp_path, _exec(tmp_path), origin, head, shallow=[head])
    # MUTATION: fetch `ref` with no depth -> the whole history, over a MiB.
    assert fetched.size < 16 * 1024, fetched.size
    assert fetched.merge_base == fork
    assert fetched.unshallow == (head,)
    assert fetched.depth == 64
    _apply(clone, fetched, bundle)
    assert _git(clone, "merge-base", "HEAD", "origin/main") == fork
    _git(clone, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "merge", "-q", "--no-edit", "origin/main")


def test_no_forge_fetch_without_a_depth(tmp_path: Path):
    origin, head, _fork = _origin(tmp_path, commits=120, gap=3)
    argvs: list[list[str]] = []

    def recording(argv, **kwargs):
        argvs.append(list(argv))
        return run_git(argv, **kwargs)

    _fetch(tmp_path, _exec(tmp_path, runner=recording), origin, head, shallow=[head])
    fetches = [a for a in argvs if "fetch" in a and any(x.startswith("file://") for x in a)]
    assert fetches
    for argv in fetches:
        assert any(x.startswith("--depth=") for x in argv), argv


def test_rounds_double_until_a_merge_base(tmp_path: Path):
    origin, head, fork = _origin(tmp_path, commits=200, gap=100)
    fetched, _bundle = _fetch(tmp_path, _exec(tmp_path), origin, head, shallow=[head])
    assert fetched.merge_base == fork
    assert (fetched.depth, fetched.rounds) == (128, 2)


def test_round_budget_refuses_too_large_naming_what_was_fetched(tmp_path: Path):
    origin, head, _fork = _origin(tmp_path, commits=400, gap=300)
    clone = _clone(tmp_path, origin)
    fetched, bundle = _fetch(tmp_path, _exec(tmp_path, shallow_max_rounds=2), origin, head,
                             shallow=[head])
    # Q2 = A: delivered at the depth reached, without a merge base.
    assert fetched.merge_base is None and fetched.depth == 128
    _apply(clone, fetched, bundle)
    assert _git(clone, "rev-parse", "origin/main") == fetched.sha
    with pytest.raises(GitExecError) as raised:
        _fetch(tmp_path, _exec(tmp_path, max_fetch_round_bytes=64 * 1024), origin, head,
               shallow=[head])
    assert raised.value.code == "too_large"
    assert "fetched" in str(raised.value)


def test_unrelated_ref_is_delivered_without_a_merge_base(tmp_path: Path):
    origin, head, _fork = _origin(tmp_path, commits=20, gap=3)
    tree = _git(origin, "rev-parse", "main^{tree}")
    lone = _git(origin, "commit-tree", tree, "-m", "lone", stdin="")
    _git(origin, "update-ref", "refs/heads/lone", lone)
    out = tmp_path / "lone.bundle"
    fetched = _exec(tmp_path).fetch_bundle(
        url=f"file://{origin}", token=_token(), ref="lone", haves=[head], shallow=[head],
        deliver=lambda path, size: out.write_bytes(path.read_bytes()))
    assert fetched.sha == lone and fetched.merge_base is None


@pytest.mark.parametrize("lines", [
    ["a" * 40] * 1 + [f"{i:040x}" for i in range(17)],
    ["abc123"],
    ["--upload-pack=x"],
])
def test_shallow_lines_are_bounded_full_shas(tmp_path: Path, lines: list[str]):
    with pytest.raises(GitExecError) as raised:
        _exec(tmp_path).fetch_bundle(url="file:///nowhere", token=_token(), ref="main",
                                     shallow=lines)
    assert raised.value.code == "bad_ref"


def test_boundary_unknown_to_the_remote_is_skipped(tmp_path: Path):
    origin, head, fork = _origin(tmp_path, commits=40, gap=3)
    stranger = "0123456789abcdef" * 2 + "01234567"
    fetched, _bundle = _fetch(tmp_path, _exec(tmp_path), origin, head,
                              shallow=[stranger, head])
    assert fetched.merge_base == fork
    assert stranger not in fetched.unshallow


def test_no_boundary_keeps_todays_bundle(tmp_path: Path):
    origin, head, _fork = _origin(tmp_path, commits=30, gap=3)
    fetched, _bundle = _fetch(tmp_path, _exec(tmp_path), origin, head)
    assert (fetched.boundary, fetched.unshallow, fetched.merge_base, fetched.depth) \
        == ((), (), None, None)
