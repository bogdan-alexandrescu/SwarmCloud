"""A one-commit clone bundle: write it, clone a sha from it, fetch a branch's delta onto it (#940).

WHY. #721 measured a step's clone: a median 36.6 s (p90 69.5 s) TCP connect to
GitHub, against 1.7 s of transfer. A bundle of a commit the platform has
already cloned, kept in the tenant's own GCS prefix, lets a later step that
needs that exact commit start without contacting GitHub at all. These tests
hold the git half of that, with no GCS and no network:

  * `write_clone_bundle` bundles exactly the one commit a depth-1 clone holds,
    and nothing of the clone's config, remote URL or credential -- and refuses
    a deeper clone (an index run's 90-day history must never be bundled);
  * `clone_from_bundle` lands the same commit and tree as `clone_at_commit`,
    with `origin` set to the forge URL and the repository shallow at the sha,
    never waiting for egress and never holding a token; a corrupt bundle or
    one of another commit raises and leaves the destination empty;
  * `fetch_tip_onto_bundle` moves a bundle-seeded repository to a branch's
    new tip, and only the delta crosses.

MUTATIONS: drop the depth check in `write_clone_bundle` -- the deep clone is
bundled. Write `.git/shallow` after the fetch instead of before -- the round
trip fails. Skip the head check -- the wrong-sha bundle "lands". Call
`await_egress` in `clone_from_bundle` -- the egress test sees the call. Drop
`--depth 1` or the seed from the delta fetch -- the 1 MB blob crosses again.

Real git against bare repositories on disk, as in test_base_pin.py.
"""

from __future__ import annotations

import inspect
import io
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops
from agent_worker.logs import build_logger

from test_strategy_end_to_end import local_urls, refs  # noqa: F401 -- fixture

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: The random blob in the base commit. Incompressible, so a fetch that sends
#: it again grows the repository by about this much.
BIG_BLOB_BYTES = 1_000_000


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _logger():
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="eng", generation=1,
        runner_profile="mock", stream=io.StringIO(),
    )


def _fake_token() -> str:
    # Built at runtime: no literal in this file looks like a credential.
    return "ghp_" + "x" * 36


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repository standing in for GitHub: two commits on `main`, the
    first holding an incompressible 1 MB blob."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "big.bin").write_bytes(os.urandom(BIG_BLOB_BYTES))
    (seed / "README.md").write_text("first\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "one")
    (seed / "README.md").write_text("second\n")
    (seed / "src").mkdir()
    (seed / "src" / "app.py").write_text("print('hello')\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "two")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
                   check=True, capture_output=True)
    return bare


def _advance_main(origin: Path, tmp_path: Path) -> str:
    work = tmp_path / "advance"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)],
                   check=True, capture_output=True)
    (work / "README.md").write_text("third: landed after the bundle was written\n")
    _git(work, "commit", "--quiet", "-am", "three")
    _git(work, "push", "--quiet", "origin", "HEAD:main")
    return refs(origin)["main"]


def _dirs(tmp_path: Path, name: str) -> dict[str, Any]:
    private = tmp_path / f"private-{name}"
    logs = tmp_path / f"logs-{name}"
    private.mkdir()
    logs.mkdir()
    return {"private_dir": private, "logs_dir": logs, "timeout_seconds": 60,
            "logger": _logger()}


def _pinned_clone(origin: Path, tmp_path: Path, name: str, commit: str) -> gitops.CloneResult:
    return gitops.clone_at_commit(
        url=f"file://{origin}", branch="main", commit=commit,
        destination=tmp_path / name, **_dirs(tmp_path, name),
    )


def _bundle(clone: Path, commit: str, tmp_path: Path, name: str = "b") -> Path:
    out = tmp_path / f"{name}.swarm-clone.bundle"
    size = gitops.write_clone_bundle(clone=clone, commit=commit, out=out,
                                     **_dirs(tmp_path, f"bundle-{name}"))
    assert size == out.stat().st_size > 0
    return out


def _from_bundle(bundle: Path, origin: Path, tmp_path: Path, name: str,
                 commit: str) -> gitops.CloneResult:
    return gitops.clone_from_bundle(
        bundle=bundle, url=f"file://{origin}", branch="main", commit=commit,
        destination=tmp_path / name, **_dirs(tmp_path, name),
    )


def _tree(repo: Path) -> str:
    return _git(repo, "ls-tree", "-r", "HEAD")


def _repo_bytes(repo: Path) -> int:
    return sum(p.stat().st_size for p in (repo / ".git" / "objects").rglob("*") if p.is_file())


def test_a_bundle_round_trip_checks_out_the_same_commit_and_tree_as_clone_at_commit(
    origin, tmp_path, local_urls,  # noqa: F811
):
    sha = refs(origin)["main"]
    pinned = _pinned_clone(origin, tmp_path, "pinned", sha)
    bundle = _bundle(pinned.path, sha, tmp_path)

    result = _from_bundle(bundle, origin, tmp_path, "from-bundle", sha)

    assert result.commit == sha == _git(result.path, "rev-parse", "HEAD")
    assert _tree(result.path) == _tree(pinned.path)
    assert (result.path / "src" / "app.py").read_text() == "print('hello')\n"
    assert _git(result.path, "status", "--porcelain") == ""
    assert result.phases["source"] == "bundle"
    assert isinstance(result.phases["total_seconds"], float)
    # Exactly what clone_at_commit leaves: a detached HEAD, no branch, the same
    # remote-tracking refspec, and no record of where the bundle was on disk.
    assert _git(result.path, "for-each-ref") == _git(pinned.path, "for-each-ref") == ""
    assert (_git(result.path, "config", "--get-all", "remote.origin.fetch")
            == _git(pinned.path, "config", "--get-all", "remote.origin.fetch"))
    assert not (result.path / ".git" / "FETCH_HEAD").exists()


def test_the_bundled_repo_has_origin_set_to_the_forge_url_and_is_shallow_at_the_sha(
    origin, tmp_path, local_urls,  # noqa: F811
):
    sha = refs(origin)["main"]
    pinned = _pinned_clone(origin, tmp_path, "pinned", sha)
    bundle = _bundle(pinned.path, sha, tmp_path)

    result = _from_bundle(bundle, origin, tmp_path, "from-bundle", sha)

    assert _git(result.path, "remote", "get-url", "origin") == f"file://{origin}"
    assert _git(result.path, "rev-parse", "--is-shallow-repository") == "true"
    assert (result.path / ".git" / "shallow").read_text().split() == [sha]
    assert _git(result.path, "rev-list", "--count", "HEAD") == "1"
    assert _git(result.path, "fsck", "--connectivity-only", "--no-dangling") == ""
    # Every later fetch goes to the forge exactly as after clone_at_commit.
    _git(result.path, "fetch", "--quiet", "--no-tags", "origin", "main")
    assert _git(result.path, "rev-parse", "refs/remotes/origin/main") == sha


def test_the_bundle_bytes_hold_no_token_and_no_remote_url(
    origin, tmp_path, local_urls,  # noqa: F811
):
    sha = refs(origin)["main"]
    pinned = _pinned_clone(origin, tmp_path, "pinned", sha)
    token = _fake_token()
    forge_url = f"https://x-access-token:{token}@github.com/octo/widgets.git"
    # The worst a clone could hold: the token in its remote URL, in its config
    # and in a credential file inside .git. None of it may reach the bundle.
    _git(pinned.path, "remote", "set-url", "origin", forge_url)
    _git(pinned.path, "config", "http.extraHeader", f"Authorization: bearer {token}")
    (pinned.path / ".git" / ".git-credentials").write_text(forge_url + "\n")

    bundle = _bundle(pinned.path, sha, tmp_path)

    data = bundle.read_bytes()
    for needle in (token, "x-access-token", "github.com", str(origin), "origin",
                   "extraHeader", "refs/heads/"):
        assert needle.encode() not in data, needle
    heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], check=True,
                           capture_output=True, text=True).stdout.split()
    assert heads == [sha, gitops.CLONE_BUNDLE_REF]
    # The temporary ref the bundle was written from is gone again.
    assert _git(pinned.path, "for-each-ref") == ""


def test_a_deep_clone_is_refused_for_bundling(origin, tmp_path, local_urls):  # noqa: F811
    sha = refs(origin)["main"]
    full = tmp_path / "full"
    subprocess.run(["git", "clone", "--quiet", f"file://{origin}", str(full)],
                   check=True, capture_output=True)
    deeper = tmp_path / "deeper"
    subprocess.run(["git", "clone", "--quiet", "--depth", "2", f"file://{origin}", str(deeper)],
                   check=True, capture_output=True)
    pinned = _pinned_clone(origin, tmp_path, "pinned", sha)
    first = _git(full, "rev-parse", "HEAD~1")

    for clone, commit in ((full, sha), (deeper, sha), (pinned.path, first)):
        out = tmp_path / f"refused-{clone.name}.bundle"
        with pytest.raises(gitops.GitError):
            gitops.write_clone_bundle(clone=clone, commit=commit, out=out,
                                      **_dirs(tmp_path, f"refuse-{clone.name}-{commit[:7]}"))
        assert not out.exists()

    # A repository whose whole history is one root commit is depth 1 already.
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main", ".")
    (root / "only.txt").write_text("the only commit\n")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "root")
    root_sha = _git(root, "rev-parse", "HEAD")
    assert _bundle(root, root_sha, tmp_path, "root").exists()


def test_a_corrupt_or_wrong_sha_bundle_raises_and_empties_the_destination(
    origin, tmp_path, local_urls,  # noqa: F811
):
    sha = refs(origin)["main"]
    pinned = _pinned_clone(origin, tmp_path, "pinned", sha)
    bundle = _bundle(pinned.path, sha, tmp_path)

    corrupt = tmp_path / "corrupt.bundle"
    data = bundle.read_bytes()
    corrupt.write_bytes(data[: len(data) // 2])
    garbage = tmp_path / "garbage.bundle"
    garbage.write_bytes(b"not a bundle at all\n")
    # A real commit of the same repository, just not the one the bundle holds.
    first = subprocess.run(["git", "rev-parse", "main~1"], cwd=str(origin), check=True,
                           capture_output=True, text=True).stdout.strip()
    assert first != sha

    cases = ((corrupt, sha), (garbage, sha), (bundle, first),
             (tmp_path / "missing.bundle", sha))
    for index, (candidate, commit) in enumerate(cases):
        destination = tmp_path / f"dest-{index}"
        with pytest.raises(gitops.GitError) as caught:
            _from_bundle(candidate, origin, tmp_path, destination.name, commit)
        # A bundle error is never a forge outage: no retry, the caller falls back.
        assert not isinstance(caught.value, gitops.GitTransient)
        assert not destination.exists() or list(destination.iterdir()) == []


def test_clone_from_bundle_never_waits_for_egress_or_reads_a_token(
    origin, tmp_path, local_urls, monkeypatch,  # noqa: F811
):
    params = inspect.signature(gitops.clone_from_bundle).parameters
    assert "token" not in params and "egress" not in params and "peers" not in params

    sha = refs(origin)["main"]
    pinned = _pinned_clone(origin, tmp_path, "pinned", sha)
    bundle = _bundle(pinned.path, sha, tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("clone_from_bundle touched the forge's path or a credential")

    monkeypatch.setattr(gitops, "await_egress", forbidden)
    monkeypatch.setattr(gitops, "_write_credentials", forbidden)

    result = _from_bundle(bundle, origin, tmp_path, "from-bundle", sha)

    assert result.commit == sha
    assert not (tmp_path / "private-from-bundle" / ".git-credentials").exists()


def test_fetch_tip_onto_bundle_lands_the_new_tip_and_transfers_only_the_delta(
    origin, tmp_path, local_urls, monkeypatch,  # noqa: F811
):
    base = refs(origin)["main"]
    pinned = _pinned_clone(origin, tmp_path, "pinned", base)
    bundle = _bundle(pinned.path, base, tmp_path)
    seeded = _from_bundle(bundle, origin, tmp_path, "seeded", base)
    before = _repo_bytes(seeded.path)
    assert before > BIG_BLOB_BYTES // 2          # the blob arrived from the bundle

    tip = _advance_main(origin, tmp_path)
    assert tip != base

    waited: list[str] = []
    real_await = gitops.await_egress

    def recording_await(egress: Any, url: str, logger: Any) -> bool:
        waited.append(url)
        return real_await(egress, url, logger)

    monkeypatch.setattr(gitops, "await_egress", recording_await)
    dirs = _dirs(tmp_path, "tip")
    result = gitops.fetch_tip_onto_bundle(
        destination=seeded.path, url=f"file://{origin}", ref="main",
        token=_fake_token(), **dirs,
    )

    assert result.commit == tip == _git(seeded.path, "rev-parse", "HEAD")
    assert _git(seeded.path, "status", "--porcelain") == ""
    assert (seeded.path / "README.md").read_text().startswith("third:")
    assert result.phases["source"] == "bundle+delta"
    assert waited == [f"file://{origin}"]
    assert not (dirs["private_dir"] / ".git-credentials").exists()
    # As after shallow_clone of a branch: on the branch, tracking origin's.
    assert _git(seeded.path, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(seeded.path, "rev-parse", "refs/remotes/origin/main") == tip
    # Only the delta crossed: the 1 MB blob the bundle seeded was not sent again.
    grown = _repo_bytes(seeded.path) - before
    assert grown < BIG_BLOB_BYTES // 4, grown


def test_fetch_tip_onto_bundle_empties_the_destination_when_the_branch_is_missing(
    origin, tmp_path, local_urls,  # noqa: F811
):
    base = refs(origin)["main"]
    pinned = _pinned_clone(origin, tmp_path, "pinned", base)
    seeded = _from_bundle(_bundle(pinned.path, base, tmp_path), origin, tmp_path, "seeded", base)

    with pytest.raises(gitops.GitError):
        gitops.fetch_tip_onto_bundle(
            destination=seeded.path, url=f"file://{origin}", ref="no-such-branch",
            **_dirs(tmp_path, "missing"),
        )
    assert list(seeded.path.iterdir()) == []
