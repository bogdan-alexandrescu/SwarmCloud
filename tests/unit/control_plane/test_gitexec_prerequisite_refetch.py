"""A push bundle's base is fetched again before it is refused (A peer application's report, 2026-11-06).

MEASURED (2026-11-05/06): two lanes whose branches touched a workflow file were
refused at publish with `bad_bundle: the bundle is built on <sha>, which the
remote does not have; fetch again`, though both bases were on GitHub (pushed
22:17 and 22:44 UTC), while GitHub was failing pushes ('fatal error in
commit_refs') around 00:15 UTC. "The remote does not have" was judged from ONE
depth-1 fetch of the sha, and ANY non-zero exit of it -- a 5xx, a dropped
connection, a replica that had not caught up -- was reported as the bundle's
fault.

Now both readers of a bundle's prerequisites (`GitExec.push_bundle` and
`GitTools._workflow_paths`, the §3.3 listing that runs first) go through
`GitExec._fetch_prerequisites`:

  * a base the first fetch misses but a fresh fetch finds is used, and the
    push goes on;
  * a base a fresh fetch still does not have (git's own "not our ref") is
    refused `bad_bundle`, as before;
  * any other failure of the fresh fetch is `remote_unavailable`, which
    swarm-api answers 503 so the worker asks again -- never `bad_bundle`;
  * no message carries the token, bare or in a URL.

The pushed ref, its argv and its checks are unchanged. Real git against a bare
`file://` origin; a failure is injected by the runner, never the network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from swarm_api.gitexec import GitExec, GitExecError, GitRun
from swarm_api.gittools import _EXEC_STATUS, GitTools

from .test_gitexec import Recorder, _bundle, _commit, _exec, _git, _remote_tip, _token, world  # noqa: F401

WORKFLOW = ".github/workflows/ci.yml"


class Flaky(Recorder):
    """The real runner, except that the first `fails` depth-1 fetches of `sha` fail."""

    def __init__(self, sha: str, stderr: str, *, fails: int = 1) -> None:
        super().__init__()
        self.sha, self.stderr, self.fails = sha, stderr, fails
        self.fetches = 0

    def __call__(self, argv, *, cwd, env, timeout):  # noqa: ANN001
        if "fetch" in argv and "--depth=1" in argv and argv[-1] == self.sha:
            self.fetches += 1
            if self.fetches <= self.fails:
                self.calls.append(list(argv))
                return GitRun(128, "", self.stderr)
        return super().__call__(argv, cwd=cwd, env=env, timeout=timeout)


def _not_our_ref(sha: str) -> str:
    return f"fatal: remote error: upload-pack: not our ref {sha}\n"


def _server_error(token: str) -> str:
    return (f"error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500\n"
            f"fatal: unable to access 'https://x-access-token:{token}@github.com/o/r.git/': "
            "The requested URL returned error: 500\n")


def _on_c(world) -> tuple[str, bytes]:
    c = _commit(world["work"], "c")
    return c, _bundle(world["work"], world["tmp"] / "c.bundle", c, world["b"])


# ---------------------------------------------------------------------------
# push_bundle
# ---------------------------------------------------------------------------


def test_a_base_the_first_fetch_missed_is_fetched_again_and_the_push_goes_on(world) -> None:
    c, data = _on_c(world)
    rec = Flaky(world["b"], _not_our_ref(world["b"]))
    pushed = _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=data,
                                           branch="swarm/x")
    assert (pushed.old_sha, pushed.new_sha) == (world["b"], c)
    assert _remote_tip(world) == c
    assert rec.fetches == 2


def test_a_base_a_transient_error_hid_is_fetched_again_and_the_push_goes_on(world) -> None:
    c, data = _on_c(world)
    token = _token()
    rec = Flaky(world["b"], _server_error(token))
    pushed = _exec(world, rec).push_bundle(url=world["url"], token=token, bundle=data,
                                           branch="swarm/x")
    assert pushed.new_sha == c
    assert _remote_tip(world) == c


def test_the_pushed_ref_and_argv_are_unchanged_by_the_refetch(world) -> None:
    c, data = _on_c(world)
    rec = Flaky(world["b"], _not_our_ref(world["b"]))
    _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=data, branch="swarm/x")
    [push] = rec.pushes()
    tail = push[push.index("push"):]
    assert tail == ["push", "--no-verify", "--no-follow-tags", "--porcelain", "--",
                    world["url"], f"{c}:refs/heads/swarm/x"]


def test_a_base_a_fresh_fetch_still_lacks_is_bad_bundle_after_exactly_two_fetches(world) -> None:
    c, data = _on_c(world)
    rec = Flaky(world["b"], _not_our_ref(world["b"]), fails=99)
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=data,
                                      branch="swarm/x")
    assert caught.value.code == "bad_bundle"
    assert "the remote does not have" in caught.value.message
    assert rec.fetches == 2
    assert rec.pushes() == []
    assert _remote_tip(world) == world["b"]


def test_a_base_truly_absent_from_the_remote_is_still_bad_bundle(world) -> None:
    lone = world["tmp"] / "lone"
    _git(world["tmp"], "init", "-q", str(lone))
    base = _commit(lone, "z")
    tip = _commit(lone, "y")
    data = _bundle(lone, world["tmp"] / "lone.bundle", tip, base)
    rec = Recorder()
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec).push_bundle(url=world["url"], token=_token(), bundle=data,
                                      branch="swarm/x")
    assert caught.value.code == "bad_bundle"
    assert sum(1 for argv in rec.calls if argv[-1] == base) == 2
    assert rec.pushes() == []


def test_a_remote_error_on_the_fresh_fetch_is_remote_unavailable_and_no_token_leaks(
        world, caplog) -> None:
    _, data = _on_c(world)
    token = _token()
    rec = Flaky(world["b"], _server_error(token), fails=99)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(GitExecError) as caught:
        _exec(world, rec).push_bundle(url=world["url"], token=token, bundle=data,
                                      branch="swarm/x")
    assert caught.value.code == "remote_unavailable"
    assert world["b"] in caught.value.message
    assert "500" in caught.value.message
    for text in (caught.value.message, str(caught.value), caplog.text):
        assert token not in text
        assert "x-access-token:" not in text
    assert rec.fetches == 2
    assert rec.pushes() == []
    assert _remote_tip(world) == world["b"]


def test_remote_unavailable_is_a_retryable_503_at_the_route() -> None:
    assert _EXEC_STATUS["remote_unavailable"] == 503
    assert _EXEC_STATUS["bad_bundle"] == 422


# ---------------------------------------------------------------------------
# GitTools._workflow_paths: the §3.3 listing reads the same prerequisites first
# ---------------------------------------------------------------------------


def _workflow_bundle(world) -> tuple[str, bytes]:
    work = world["work"]
    target = work / WORKFLOW
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("on: push\n")
    _git(work, "add", WORKFLOW)
    _git(work, "commit", "-q", "-m", "ci")
    tip = _git(work, "rev-parse", "HEAD")
    return tip, _bundle(work, world["tmp"] / "ci.bundle", tip, world["b"])


def _paths(gx: GitExec, world, token: str, data: bytes) -> list[str]:
    return GitTools._workflow_paths(SimpleNamespace(gitexec=gx), world["url"], token, data)


def test_the_workflow_listing_fetches_a_missed_base_again(world) -> None:
    _, data = _workflow_bundle(world)
    rec = Flaky(world["b"], _not_our_ref(world["b"]))
    assert _paths(_exec(world, rec), world, _token(), data) == [WORKFLOW]
    assert rec.fetches == 2


def test_the_workflow_listing_refuses_a_base_a_fresh_fetch_still_lacks(world) -> None:
    _, data = _workflow_bundle(world)
    rec = Flaky(world["b"], _not_our_ref(world["b"]), fails=99)
    with pytest.raises(GitExecError) as caught:
        _paths(_exec(world, rec), world, _token(), data)
    assert caught.value.code == "bad_bundle"
    assert rec.fetches == 2


def test_the_workflow_listing_answers_a_remote_error_remote_unavailable(world) -> None:
    _, data = _workflow_bundle(world)
    token = _token()
    rec = Flaky(world["b"], _server_error(token), fails=99)
    with pytest.raises(GitExecError) as caught:
        _paths(_exec(world, rec), world, token, data)
    assert caught.value.code == "remote_unavailable"
    assert token not in caught.value.message
    assert "x-access-token:" not in caught.value.message
