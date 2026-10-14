"""swarm-api's git execution library (docs/runner-git-tools.md §1.3, lane G4 of #1225).

Every case runs real git against `file://` bare repositories in `tmp_path`:
no network, no credentials, no emulator. `GitExec(local_remotes=True)` is the
seam that lets a `file://` origin stand in for github.com; production builds
`GitExec` without it, and then refuses any URL the forge token may not go to.

The token is built at runtime (CLAUDE.md: nothing added may look like a
credential), and every case that holds one asserts it never reached an argv.

What each case pins, from §7's row G4:

* a non-fast-forward bundle is refused `non_fast_forward`, the remote is
  unchanged, and the refusal carries both shas;
* the push argv is asserted: never `--force`, `-f`, a `+` refspec, `--mirror`,
  `--all`, `--tags` or `--delete`. The owner's Q1 answer (A, 2026-10-13) adds
  exactly one guarded form, `--force-with-lease=refs/heads/<branch>:<sha>`,
  only when the caller hands the tip swarm-api recorded; a wrong lease is
  `non_fast_forward` too;
* the credential file exists, 0600, while a remote command runs, and is gone
  after success, after a failure and after an exception;
* git's stderr comes back with `https://x-access-token:<token>@` scrubbed;
* a fetch bundle carries only what the haves lack;
* a bundle over the cap is `too_large` before any git runs.
"""

from __future__ import annotations

import os
import secrets
import stat
import struct
import subprocess
from pathlib import Path

import pytest

from swarm_api.gitexec import (
    GitExec,
    GitExecError,
    GitRun,
    run_git,
    scrub,
)

#: The most a forced-looking element could be. Anything here in a push argv
#: would let a push replace history it did not descend from.
FORCING = {"--force", "-f", "--mirror", "--all", "--tags", "--delete", "-d", "--prune"}

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


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def _commit(work: Path, name: str) -> str:
    (work / name).write_text(name + "\n")
    _git(work, "add", name)
    _git(work, "commit", "-q", "-m", name)
    return _git(work, "rev-parse", "HEAD")


def _token() -> str:
    # Runtime-built: no line of this file holds a credential-shaped literal.
    return "ghs" + "_" + secrets.token_hex(18)


class Recorder:
    """The real runner, with every argv kept and the credential file observed."""

    def __init__(self, *, raise_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.credential_seen: list[tuple[Path, int, str]] = []
        self.credential_present: list[bool] = []
        self.raise_on = raise_on

    def __call__(self, argv, *, cwd, env, timeout):  # noqa: ANN001
        self.calls.append(list(argv))
        self.credential_present.append((Path(cwd) / "credentials").exists())
        for item in argv:
            if item.startswith("credential.helper=store --file="):
                path = Path(item.split("--file=", 1)[1])
                info = path.lstat()
                self.credential_seen.append((path, stat.S_IMODE(info.st_mode), path.read_text()))
        if self.raise_on and self.raise_on in argv:
            raise RuntimeError("the runner blew up mid-call")
        return run_git(argv, cwd=cwd, env=env, timeout=timeout)

    def pushes(self) -> list[list[str]]:
        return [argv for argv in self.calls if "push" in argv]


@pytest.fixture
def world(tmp_path: Path):
    """A bare origin whose `swarm/x` is at B, with history A-B, and a work clone."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", str(work))
    a = _commit(work, "a")
    b = _commit(work, "b")
    _git(work, "push", "-q", str(remote), "HEAD:refs/heads/swarm/x", "HEAD:refs/heads/main")
    return {"tmp": tmp_path, "remote": remote, "work": work, "a": a, "b": b,
            "url": f"file://{remote}"}


def _bundle(work: Path, out: Path, tip: str, *bases: str) -> bytes:
    _git(work, "bundle", "create", str(out), "HEAD", *[f"^{b}" for b in bases])
    return out.read_bytes()


def _exec(world, recorder: Recorder | None = None, **kw) -> GitExec:
    return GitExec(scratch_root=world["tmp"] / "scratch", local_remotes=True,
                   runner=recorder or Recorder(), **kw)


def _remote_tip(world, branch: str = "swarm/x") -> str:
    return _git(world["remote"], "rev-parse", f"refs/heads/{branch}")


def _pack_object_count(bundle: bytes) -> int:
    pack = bundle.index(b"\n\n") + 2
    assert bundle[pack:pack + 4] == b"PACK"
    return struct.unpack(">I", bundle[pack + 8:pack + 12])[0]


def _scratch_is_empty(world) -> bool:
    root = world["tmp"] / "scratch"
    return not root.exists() or not any(root.iterdir())


# ---------------------------------------------------------------------------
# push: never forced
# ---------------------------------------------------------------------------


def test_a_fast_forward_bundle_is_pushed(world) -> None:
    """The control: a bundle that descends from the branch's tip lands."""
    c = _commit(world["work"], "c")
    data = _bundle(world["work"], world["tmp"] / "c.bundle", c, world["b"])
    rec = Recorder()
    pushed = _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=data,
                                           branch="swarm/x")
    assert (pushed.branch, pushed.old_sha, pushed.new_sha, pushed.rewrite) == (
        "swarm/x", world["b"], c, False)
    assert _remote_tip(world) == c


def test_a_non_fast_forward_bundle_is_refused_and_the_remote_is_unchanged(world) -> None:
    # Someone else moved swarm/x to C1; the agent's D is built on B.
    other = world["tmp"] / "other"
    _git(world["tmp"], "clone", "-q", world["url"], str(other))
    _git(other, "checkout", "-q", "swarm/x")
    c1 = _commit(other, "c1")
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/swarm/x")
    d = _commit(world["work"], "d")
    data = _bundle(world["work"], world["tmp"] / "d.bundle", d, world["b"])
    rec = Recorder()

    with pytest.raises(GitExecError) as caught:
        _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=data,
                                      branch="swarm/x")

    assert caught.value.code == "non_fast_forward"
    assert caught.value.detail == {"branch": "swarm/x", "remote_sha": c1, "new_sha": d}
    assert _remote_tip(world) == c1
    assert len(rec.pushes()) == 1


def test_a_push_never_sends_force_and_the_argv_is_exactly_this(world) -> None:
    c = _commit(world["work"], "c")
    data = _bundle(world["work"], world["tmp"] / "c.bundle", c, world["b"])
    rec = Recorder()
    token = _token()
    _exec(world, rec).push_bundle(url=world["url"], token=token, bundle=data, branch="swarm/x")

    [push] = rec.pushes()
    tail = push[push.index("push"):]
    assert tail == ["push", "--no-verify", "--no-follow-tags", "--porcelain", "--",
                    world["url"], f"{c}:refs/heads/swarm/x"]
    assert not FORCING & set(push)
    assert not [item for item in push if item.startswith("--force")]
    assert not [item for item in push if item.startswith("+")]
    assert "core.hooksPath=/dev/null" in push
    assert "credential.helper=" in push
    for argv in rec.calls:
        assert not [item for item in argv if token in item], argv


def test_a_lease_rewrites_only_over_the_recorded_tip(world) -> None:
    """Q1 = A: `--force-with-lease=<branch>:<sha>`, the sha swarm-api recorded.

    The rewrite replaces the task's own earlier push. A lease naming any other
    sha -- here B, after the branch moved on to C1 -- is refused and the
    remote is unchanged."""
    other = world["tmp"] / "other"
    _git(world["tmp"], "clone", "-q", world["url"], str(other))
    _git(other, "checkout", "-q", "swarm/x")
    c1 = _commit(other, "c1")
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/swarm/x")
    d = _commit(world["work"], "d")
    data = _bundle(world["work"], world["tmp"] / "d.bundle", d, world["b"])

    stale = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, stale).push_bundle(url=world["url"], token=_token(), bundle=data,
                                        branch="swarm/x", lease=world["b"])
    assert caught.value.code == "non_fast_forward"
    assert _remote_tip(world) == c1

    rec = Recorder()
    pushed = _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=data,
                                           branch="swarm/x", lease=c1)
    assert (pushed.old_sha, pushed.new_sha, pushed.rewrite) == (c1, d, True)
    assert _remote_tip(world) == d
    [push] = rec.pushes()
    assert [item for item in push if item.startswith("--force")] == [
        f"--force-with-lease=refs/heads/swarm/x:{c1}"]
    assert not FORCING & set(push)
    assert not [item for item in push if item.startswith("+")]


@pytest.mark.parametrize("lease", ["", "HEAD", "b" * 39, "refs/heads/main", "-x"])
def test_a_lease_that_is_not_a_full_sha_is_refused_before_any_git(world, lease) -> None:
    rec = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=b"x",
                                      branch="swarm/x", lease=lease)
    assert caught.value.code == "bad_ref"
    assert rec.calls == []


@pytest.mark.parametrize("branch", ["main", "feature/x", "swarm/../main", "-swarm/x",
                                    "swarm/", "swarm/x.lock", "swarm/a b"])
def test_a_branch_outside_swarm_is_refused_before_any_git(world, branch) -> None:
    """Q5 = A: the library pushes `swarm/` branches only, whatever G5 derived."""
    rec = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=b"x",
                                      branch=branch)
    assert caught.value.code == "bad_ref"
    assert rec.calls == []


# ---------------------------------------------------------------------------
# the credential file
# ---------------------------------------------------------------------------


def _assert_credential_lived_and_died(rec: Recorder, token: str, world) -> None:
    assert rec.credential_seen, "no remote command was handed the credential file"
    for path, mode, text in rec.credential_seen:
        assert mode == 0o600
        assert text.strip() == f"https://x-access-token:{token}@github.com"
        assert not path.exists()
    assert _scratch_is_empty(world)


def test_the_credential_file_is_gone_after_success(world) -> None:
    c = _commit(world["work"], "c")
    data = _bundle(world["work"], world["tmp"] / "c.bundle", c, world["b"])
    rec, token = Recorder(), _token()
    _exec(world, rec).push_bundle(url=world["url"], token=token, bundle=data, branch="swarm/x")
    _assert_credential_lived_and_died(rec, token, world)


def test_the_credential_file_is_gone_after_a_failure(world) -> None:
    rec, token = Recorder(), _token()
    with pytest.raises(GitExecError):
        _exec(world, rec).fetch_bundle(url=world["url"], token=token, ref="no-such-branch",
                                       haves=[world["a"]])
    _assert_credential_lived_and_died(rec, token, world)


def test_the_credential_file_is_gone_after_an_exception(world) -> None:
    c = _commit(world["work"], "c")
    data = _bundle(world["work"], world["tmp"] / "c.bundle", c, world["b"])
    rec, token = Recorder(raise_on="push"), _token()
    with pytest.raises(RuntimeError):
        _exec(world, rec).push_bundle(url=world["url"], token=token, bundle=data,
                                      branch="swarm/x")
    _assert_credential_lived_and_died(rec, token, world)
    assert _remote_tip(world) == world["b"]


def test_the_credential_file_is_removed_as_soon_as_the_last_remote_command_ran(world) -> None:
    """Not merely with the scratch directory at the end: `bundle create` runs
    after the fetches and must find no credential beside it."""
    _commit(world["work"], "c")
    _git(world["work"], "push", "-q", str(world["remote"]), "HEAD:refs/heads/main")
    rec = Recorder()
    _exec(world, rec).fetch_bundle(url=world["url"], token=_token(), ref="main",
                                   haves=[world["b"]])
    [(present)] = [p for argv, p in zip(rec.calls, rec.credential_present) if "bundle" in argv]
    assert present is False
    assert any(rec.credential_present)


def test_the_fence_recheck_runs_before_the_push_and_its_refusal_pushes_nothing(world) -> None:
    """§1.4: the fence is checked again after the bundle is unpacked and
    before `git push`. Its exception reaches the caller; nothing is pushed and
    the credential is gone."""
    c = _commit(world["work"], "c")
    data = _bundle(world["work"], world["tmp"] / "c.bundle", c, world["b"])
    rec, token = Recorder(), _token()

    class Fenced(Exception):
        pass

    def fence() -> None:
        raise Fenced()

    with pytest.raises(Fenced):
        _exec(world, rec).push_bundle(url=world["url"], token=token, bundle=data,
                                      branch="swarm/x", before_push=fence)
    assert rec.pushes() == []
    assert _remote_tip(world) == world["b"]
    _assert_credential_lived_and_died(rec, token, world)


# ---------------------------------------------------------------------------
# scrubbing
# ---------------------------------------------------------------------------


def test_git_stderr_carrying_the_token_in_a_url_comes_back_scrubbed(tmp_path: Path) -> None:
    token = _token()
    leaked = (f"fatal: unable to access 'https://x-access-token:{token}@github.com/o/r.git/': "
              "The requested URL returned error: 403\n")

    def runner(argv, *, cwd, env, timeout):  # noqa: ANN001
        if "init" in argv:
            return run_git(argv, cwd=cwd, env=env, timeout=timeout)
        return GitRun(code=128, stdout="", stderr=leaked)

    gx = GitExec(scratch_root=tmp_path / "scratch", runner=runner)
    with pytest.raises(GitExecError) as caught:
        gx.fetch_bundle(url="https://github.com/o/r.git", token=token, ref="main")

    text = str(caught.value) + caught.value.message + repr(caught.value.detail)
    assert token not in text
    assert "x-access-token:" not in text
    assert "error: 403" in text


def test_scrub_strips_url_userinfo_it_was_not_told_about() -> None:
    other = "ghp" + "_" + secrets.token_hex(18)
    text = scrub(f"remote: see https://someone:{other}@github.com/o/r and http://u@host/x", ())
    assert other not in text
    assert "someone:" not in text and "u@host" not in text
    assert "github.com/o/r" in text


def test_scrub_masks_the_registered_token_bare_and_percent_encoded() -> None:
    token = "tok" + secrets.token_hex(8) + "/+="
    from urllib.parse import quote

    text = scrub(f"a {token} b {quote(token, safe='')} c", (token,))
    assert token not in text and quote(token, safe="") not in text


def test_production_refuses_a_url_the_token_may_not_go_to(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv, *, cwd, env, timeout):  # noqa: ANN001
        calls.append(argv)
        return GitRun(code=0, stdout="", stderr="")

    gx = GitExec(scratch_root=tmp_path / "scratch", runner=runner)
    for url in ("file:///tmp/r.git", "http://github.com/o/r.git", "https://evil.example/o/r.git",
                "https://github.com:444/o/r.git", "https://a@github.com/o/r.git",
                "ext::sh -c id", "-uhttps://github.com/o/r.git"):
        with pytest.raises(GitExecError) as caught:
            gx.fetch_bundle(url=url, token=_token(), ref="main")
        assert caught.value.code == "bad_remote", url
    assert calls == []


# ---------------------------------------------------------------------------
# fetch → bundle
# ---------------------------------------------------------------------------


def test_a_fetch_bundle_excludes_the_haves(world) -> None:
    c = _commit(world["work"], "c")
    _git(world["work"], "push", "-q", str(world["remote"]), "HEAD:refs/heads/main")
    gx = _exec(world)

    fetched = gx.fetch_bundle(url=world["url"], token=_token(), ref="main", haves=[world["b"]])
    assert fetched.sha == c
    # C, its tree and the one new blob: nothing B already had.
    assert _pack_object_count(fetched.bundle) == 3
    header = fetched.bundle[:fetched.bundle.index(b"\n\n")].decode()
    assert f"-{world['b']}" in header
    assert fetched.size == len(fetched.bundle)

    # The control: with no have, the same fetch carries the whole history.
    whole = gx.fetch_bundle(url=world["url"], token=_token(), ref="main")
    assert _pack_object_count(whole.bundle) > 3

    # And it applies in a clone that holds B.
    clone = world["tmp"] / "clone"
    _git(world["tmp"], "clone", "-q", "--branch", "swarm/x", world["url"], str(clone))
    (world["tmp"] / "f.bundle").write_bytes(fetched.bundle)
    _git(clone, "fetch", "-q", str(world["tmp"] / "f.bundle"),
         f"{fetched.ref}:refs/remotes/origin/main")
    assert _git(clone, "rev-parse", "refs/remotes/origin/main") == c


def test_a_fetch_by_sha_and_a_fetch_of_what_the_worker_has(world) -> None:
    gx = _exec(world)
    fetched = gx.fetch_bundle(url=world["url"], token=_token(), ref=world["a"])
    assert fetched.sha == world["a"]
    nothing = gx.fetch_bundle(url=world["url"], token=_token(), ref="swarm/x", haves=[world["b"]])
    assert (nothing.sha, nothing.bundle, nothing.size) == (world["b"], None, 0)
    assert _scratch_is_empty(world)


@pytest.mark.parametrize("ref", ["-upload-pack=x", "a..b", "main:refs/heads/x", "+main",
                                 "ma in", "", "refs/heads/@{-1}"])
def test_a_bad_fetch_ref_is_refused_before_any_git(world, ref) -> None:
    rec = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec).fetch_bundle(url=world["url"], token=_token(), ref=ref)
    assert caught.value.code == "bad_ref"
    assert rec.calls == []


def test_a_fetch_bundle_over_the_cap_is_too_large(world) -> None:
    _commit(world["work"], "c")
    _git(world["work"], "push", "-q", str(world["remote"]), "HEAD:refs/heads/main")
    with pytest.raises(GitExecError) as caught:
        _exec(world, max_bundle_bytes=64).fetch_bundle(url=world["url"], token=_token(),
                                                       ref="main", haves=[world["b"]])
    assert caught.value.code == "too_large"
    assert "64" in caught.value.message
    assert _scratch_is_empty(world)


# ---------------------------------------------------------------------------
# bundle limits and shape
# ---------------------------------------------------------------------------


def test_a_push_bundle_over_the_cap_is_too_large_before_anything_is_unpacked(world) -> None:
    c = _commit(world["work"], "c")
    data = _bundle(world["work"], world["tmp"] / "c.bundle", c, world["b"])
    rec = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec, max_bundle_bytes=len(data) - 1).push_bundle(
            url=world["url"], token=_token(), bundle=data, branch="swarm/x")
    assert caught.value.code == "too_large"
    assert str(len(data) - 1) in caught.value.message
    assert rec.calls == []
    assert _scratch_is_empty(world)
    assert _remote_tip(world) == world["b"]


def test_a_push_bundle_file_over_the_cap_is_too_large_before_it_is_read(world) -> None:
    c = _commit(world["work"], "c")
    path = world["tmp"] / "c.bundle"
    data = _bundle(world["work"], path, c, world["b"])
    rec = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec, max_bundle_bytes=len(data) - 1).push_bundle(
            url=world["url"], token=_token(), bundle=path, branch="swarm/x")
    assert caught.value.code == "too_large"
    assert rec.calls == []


@pytest.mark.parametrize("header", [
    b"not a bundle\n\nPACK",
    b"# v2 git bundle\n\nPACK",                                           # no ref
    b"# v2 git bundle\n" + b"a" * 40 + b" HEAD\n" + b"b" * 40 + b" refs/heads/y\n\nPACK",
    b"# v2 git bundle\n" + b"a" * 40 + b" refs/heads/../x\n\nPACK",
    b"# v2 git bundle\n" + b"a" * 40 + b" -HEAD\n\nPACK",
    b"# v2 git bundle\n-zz HEAD\n" + b"a" * 40 + b" HEAD\n\nPACK",
    b"# v2 git bundle\n" + b"a" * 40 + b" HEAD\n",                          # no pack
])
def test_a_malformed_bundle_is_refused_before_any_git(world, header) -> None:
    rec = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=header,
                                      branch="swarm/x")
    assert caught.value.code == "bad_bundle"
    assert rec.calls == []


def test_a_bundle_whose_base_the_remote_lacks_is_bad_bundle(world) -> None:
    lone = world["tmp"] / "lone"
    _git(world["tmp"], "init", "-q", str(lone))
    base = _commit(lone, "z")
    tip = _commit(lone, "y")
    data = _bundle(lone, world["tmp"] / "lone.bundle", tip, base)
    with pytest.raises(GitExecError) as caught:
        _exec(world).push_bundle(url=world["url"], token=_token(), bundle=data, branch="swarm/x")
    assert caught.value.code == "bad_bundle"
    assert _remote_tip(world) == world["b"]
    assert _scratch_is_empty(world)


def test_a_bundle_carrying_a_malformed_object_is_refused_by_fsck(world) -> None:
    """The unbundle is the one step that reads objects the agent built, so it
    runs with `fetch.fsckObjects`: a commit with no committer never reaches a
    push."""
    work = world["work"]
    tree = _git(work, "rev-parse", "HEAD^{tree}")
    raw = f"tree {tree}\nparent {world['b']}\nauthor t <t@example.invalid> 1 +0000\n\nbad\n"
    bad = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--literally", "--stdin"],
                         cwd=work, env=_ENV, input=raw, capture_output=True, text=True,
                         check=True).stdout.strip()
    _git(work, "update-ref", "HEAD", bad)
    data = _bundle(work, world["tmp"] / "bad.bundle", bad, world["b"])
    with pytest.raises(GitExecError) as caught:
        _exec(world).push_bundle(url=world["url"], token=_token(), bundle=data, branch="swarm/x")
    assert caught.value.code == "bad_bundle"
    assert _remote_tip(world) == world["b"]


# ---------------------------------------------------------------------------
# bounded in the kernel (the security review of 2026-10-13)
# ---------------------------------------------------------------------------


def test_every_git_runs_under_the_memory_and_file_limits(world) -> None:
    c = _commit(world["work"], "c")
    data = _bundle(world["work"], world["tmp"] / "c.bundle", c, world["b"])
    rec = Recorder()
    gx = _exec(world, rec, max_git_memory_bytes=300 * 1024 * 1024, max_bundle_bytes=10 ** 7)
    gx.push_bundle(url=world["url"], token=_token(), bundle=data, branch="swarm/x")
    assert rec.calls
    for argv in rec.calls:
        assert argv[1:3] == ["-I", "-c"] and "RLIMIT_AS" in argv[3], argv[:4]
        assert argv[4:7] == [str(300 * 1024 * 1024), str(10 ** 7), "git"]
        assert "core.bigFileThreshold=1m" in argv and "transfer.unpackLimit=1" in argv


def test_a_fetch_whose_pack_outgrows_the_cap_is_stopped_while_it_is_written(world) -> None:
    """The cap holds what a fetch WRITES, not only the bundle made afterwards:
    a 2 MB pack under a 64 KiB cap stops the fetch itself."""
    (world["work"] / "big").write_bytes(os.urandom(2_000_000))
    _git(world["work"], "add", "big")
    _git(world["work"], "commit", "-q", "-m", "big")
    _git(world["work"], "push", "-q", str(world["remote"]), "HEAD:refs/heads/main")
    rec = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec, max_bundle_bytes=64 * 1024).fetch_bundle(
            url=world["url"], token=_token(), ref="main")
    assert caught.value.code == "too_large"
    assert not [argv for argv in rec.calls if "bundle" in argv]
    assert _scratch_is_empty(world)


def test_a_call_over_the_concurrency_limit_is_busy_and_runs_no_git(world) -> None:
    import threading

    slots = threading.BoundedSemaphore(1)
    entered, release = threading.Event(), threading.Event()

    def holding(argv, *, cwd, env, timeout):  # noqa: ANN001
        entered.set()
        release.wait(30)
        return run_git(argv, cwd=cwd, env=env, timeout=timeout)

    first = threading.Thread(target=lambda: _exec(world, holding, slots=slots).fetch_bundle(
        url=world["url"], token=_token(), ref="main"))
    first.start()
    try:
        assert entered.wait(30)
        rec = Recorder()
        with pytest.raises(GitExecError) as caught:
            _exec(world, rec, slots=slots).fetch_bundle(url=world["url"], token=_token(),
                                                        ref="main")
        assert caught.value.code == "busy"
        assert rec.calls == []
    finally:
        release.set()
        first.join(30)
    # And the slot is given back.
    assert _exec(world, slots=slots).fetch_bundle(url=world["url"], token=_token(),
                                                  ref="main").sha == world["b"]


def test_one_deadline_bounds_every_git_in_a_call(world) -> None:
    timeouts: list[float] = []

    def runner(argv, *, cwd, env, timeout):  # noqa: ANN001
        timeouts.append(timeout)
        return run_git(argv, cwd=cwd, env=env, timeout=timeout)

    _exec(world, runner, deadline_seconds=5).fetch_bundle(
        url=world["url"], token=_token(), ref="main", haves=[world["a"]])
    assert timeouts and max(timeouts) <= 5

    with pytest.raises(GitExecError) as caught:
        _exec(world, runner, deadline_seconds=0).fetch_bundle(url=world["url"], token=_token(),
                                                              ref="main")
    assert caught.value.code == "timed_out"
    assert _scratch_is_empty(world)


def test_a_timeout_kills_git_s_children_too(tmp_path: Path) -> None:
    """A grandchild that does not hold git's pipes (as `git-remote-https`
    need not) is killed with the group, not left running."""
    import time

    pidfile = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(GitExecError) as caught:
        run_git(["sh", "-c", f"sleep 60 >/dev/null 2>&1 & echo $! > {pidfile}; wait"],
                cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, timeout=1)
    assert caught.value.code == "timed_out"
    assert time.monotonic() - started < 20
    pid = int(pidfile.read_text())

    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the grandchild outlived the timeout")


def test_a_fetch_bundle_can_be_delivered_as_a_file_instead_of_bytes(world) -> None:
    c = _commit(world["work"], "c")
    _git(world["work"], "push", "-q", str(world["remote"]), "HEAD:refs/heads/main")
    seen: list[tuple[bytes, int]] = []

    def deliver(path: Path, size: int) -> str:
        seen.append((path.read_bytes(), size))
        return "gs://tenant-prefix/object"

    fetched = _exec(world).fetch_bundle(url=world["url"], token=_token(), ref="main",
                                        haves=[world["b"]], deliver=deliver)
    assert (fetched.sha, fetched.bundle, fetched.delivered) == (c, None, "gs://tenant-prefix/object")
    [(data, size)] = seen
    assert size == len(data) == fetched.size and _pack_object_count(data) == 3
    assert _scratch_is_empty(world)
