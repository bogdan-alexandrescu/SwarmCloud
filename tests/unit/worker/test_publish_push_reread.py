"""A publish push refused as a stale credential is retried once with a freshly
read forge token (part 2 of #3223).

MEASURED: task_a4f1d69c probed `can_push: true` and then its branch push failed
exit 128 "remote: Invalid username or token". The publish reads the token once
(`_publish_git`) and re-read it only at the probe and the pull request; swarm-
api's 15-minute refresh sweep ended the token between the probe and the push
(each refresh makes GitHub end the access token it replaces).

What is pinned:

  * refused, then accepted: the slot is read again ONCE, the second push and
    the pull request use the new token, and a `forge_token_reread` mark
    (reason `push_401`) is on the task's events;
  * refused twice: one re-read, no third push, and the publish fails with
    `FORGE_REFUSED_TWICE` in its reason;
  * a refused write grant or a refused token is not read past;
  * a push refused for anything but the credential -- a non-fast-forward, a
    lease that no longer holds, a 403 -- is never re-read;
  * no token value is in any log line, event or task field.

MUTATIONS: drop the re-read -- the refused-then-accepted test sees FAILED.
Re-read on any GitError -- the non-credential tests see a read. Skip the gates
-- the refusal tests see a read.

Every fake token is built at runtime, never written as one literal.
"""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker import forge as forge_mod, gitops, lifecycle
from agent_worker import secrets as secrets_mod
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    forge,
    local_urls,
    origin,
)
from test_forge_token_reread import (  # noqa: F401 -- fixtures and helpers
    secret_name,
    token,
    user_worker,
)
from test_pr_open_reread import INTEGRATOR, TITLED, _Forge, _no_token_anywhere, _rereads

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: git's answer to a push with a token GitHub has ended.
STALE = ("git push failed with exit 128: remote: Invalid username or token. Password "
         "authentication is not supported for Git operations.\nfatal: Authentication "
         "failed for 'https://github.com/example-org/widgets.git/'")


class _Reads:
    """`Worker._git_token`: a new version of the slot at every read."""

    def __init__(self) -> None:
        self.values: list[str] = []

    def __call__(self, **_: object) -> str:
        value = "ghu_" + secrets.token_hex(18)
        self.values.append(value)
        return value


class _Pushes:
    """`push_branch`, raising `refusals` in turn before pushing for real."""

    def __init__(self, real, refusals: list[BaseException]) -> None:
        self.real = real
        self.refusals = list(refusals)
        self.tokens: list[str | None] = []

    def __call__(self, **kwargs: Any) -> str | None:
        self.tokens.append(kwargs.get("token"))
        if self.refusals:
            raise self.refusals.pop(0)
        return self.real(**kwargs)


def _run(db, worker_factory, monkeypatch, origin: Path, refusals: list[BaseException]):
    seed_attempt(db, task_input=TITLED)
    db.doc("tasks/task_1")["metadata"] = {"dispatch": dict(INTEGRATOR)}
    worker, _config, _ = worker_factory(repository_url=f"file://{origin}")
    reads = _Reads()
    monkeypatch.setattr(worker, "_git_token", reads)
    fake = _Forge([201])
    monkeypatch.setattr(forge_mod, "_request", fake)
    monkeypatch.setattr(lifecycle, "open_pull_request", forge_mod.open_pull_request)
    pushes = _Pushes(lifecycle.push_branch, refusals)
    monkeypatch.setattr(lifecycle, "push_branch", pushes)
    return worker, reads, fake, pushes


@needs_git
def test_a_stale_token_push_rereads_once_and_publishes(
    db, worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    worker, reads, fake, pushes = _run(
        db, worker_factory, monkeypatch, origin, [gitops.GitError(STALE)]
    )

    assert worker.run() == ExitCode.OK, log_stream.getvalue()[-3000:]
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    git = task["result_summary"]["git"]
    assert git["published"] is True
    assert len(pushes.tokens) == 2
    # The probe's read, then the push's re-read: the slot read twice, and the
    # second push used the read made AFTER the refusal.
    first = reads.values.index(pushes.tokens[0])
    assert pushes.tokens[1] == reads.values[first + 1]
    assert len(reads.values) == first + 2, "the slot was read more than once again"
    # The pull request went with the fresh token, not the dead one.
    assert fake.posts == [pushes.tokens[1]]
    assert git["pull_request"]["number"] == 7
    marks = _rereads(db)
    assert len(marks) == 1
    assert marks[0]["detail"]["reason"] == "push_401"
    assert marks[0]["detail"]["outcome"] == "read"
    _no_token_anywhere(db, log_stream, reads.values)


@needs_git
def test_a_push_refused_twice_fails_saying_so(
    db, worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    worker, reads, fake, pushes = _run(
        db, worker_factory, monkeypatch, origin,
        [gitops.GitError(STALE), gitops.GitError(STALE)],
    )

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task["state"]
    git = task["result_summary"]["git"]
    assert git["published"] is False
    assert secrets_mod.FORGE_REFUSED_TWICE in git["publish_reason"]
    assert len(pushes.tokens) == 2, "exactly one more push"
    first = reads.values.index(pushes.tokens[0])
    assert len(reads.values) == first + 2, "exactly one re-read"
    assert fake.posts == [], "no pull request after a refused push"
    assert len(_rereads(db)) == 1
    _no_token_anywhere(db, log_stream, reads.values)


# --------------------------------------------------------------------------
# The re-read itself: whose token, and the gates before it
# --------------------------------------------------------------------------


def _granted(worker, monkeypatch) -> None:
    monkeypatch.setattr(worker, "_forge_write_refusal", lambda: None)


def _fenceless(worker, monkeypatch) -> list[str]:
    owned: list[str] = []
    monkeypatch.setattr(worker, "_own_before_forge_write", owned.append)
    return owned


def _push(refusals: list[BaseException]):
    used: list[str] = []

    def push(with_token: str) -> str:
        used.append(with_token)
        if refusals:
            raise refusals.pop(0)
        return "a" * 40

    return push, used


def test_a_persons_slot_is_reread_through_the_broker(user_worker, monkeypatch):
    worker, routes = user_worker
    _granted(worker, monkeypatch)
    owned = _fenceless(worker, monkeypatch)
    held = worker._git_token()
    released = len(routes.accessed)
    routes.values[secret_name()] = token("b")
    push, used = _push([gitops.GitError(STALE)])

    pushed, fresh = worker._push_reading_again(push, held, branch="swarm/task_1")

    assert pushed == "a" * 40
    assert fresh == token("b")
    assert used == [token("a"), token("b")]
    assert routes.accessed[released:] == [secret_name()], "not re-read through the broker once"
    assert owned == ["branch push"], "the fence is read again before the second push"


def test_a_refused_write_grant_is_not_reread(user_worker, monkeypatch):
    worker, routes = user_worker
    _fenceless(worker, monkeypatch)
    held = worker._git_token()
    released = len(routes.accessed)
    monkeypatch.setattr(worker, "_forge_write_refusal",
                        lambda: (lifecycle.FORGE_GRANT_REVOKED, "the grant was removed"))
    push, used = _push([gitops.GitError(STALE)])

    with pytest.raises(gitops.GitError) as raised:
        worker._push_reading_again(push, held, branch="swarm/task_1")

    assert routes.accessed[released:] == [], "a refused write still read a token"
    assert len(used) == 1
    message = str(raised.value)
    assert "the grant was removed" in message
    assert lifecycle.FORGE_GRANT_REVOKED in message
    assert token("a") not in message


def test_a_refused_token_is_not_reread(user_worker, monkeypatch):
    worker, routes = user_worker
    _granted(worker, monkeypatch)
    _fenceless(worker, monkeypatch)
    held = worker._git_token()
    released = len(routes.accessed)
    monkeypatch.setattr(worker, "_git_token_refusal", lambda: "an agent process is alive")
    push, used = _push([gitops.GitError(STALE)])

    with pytest.raises(gitops.GitError):
        worker._push_reading_again(push, held, branch="swarm/task_1")

    assert routes.accessed[released:] == []
    assert len(used) == 1


def test_a_push_refused_twice_raises_with_the_marker(user_worker, monkeypatch):
    worker, routes = user_worker
    _granted(worker, monkeypatch)
    _fenceless(worker, monkeypatch)
    held = worker._git_token()
    released = len(routes.accessed)
    routes.values[secret_name()] = token("b")
    push, used = _push([gitops.GitError(STALE), gitops.GitError(STALE)])

    with pytest.raises(gitops.GitError) as raised:
        worker._push_reading_again(push, held, branch="swarm/task_1")

    assert len(used) == 2
    assert routes.accessed[released:] == [secret_name()]
    message = str(raised.value)
    assert secrets_mod.FORGE_REFUSED_TWICE in message
    assert token("a") not in message and token("b") not in message


@pytest.mark.parametrize("refusal", [
    gitops.PushRejected("git push failed: ! [rejected] swarm/task_1 (non-fast-forward)"),
    gitops.PushRejected("git push failed: ! [rejected] swarm/task_1 (stale info)"),
    gitops.GitError("fatal: unable to access 'https://github.com/example-org/widgets.git/': "
                    "The requested URL returned error: 403"),
], ids=["non_fast_forward", "lease", "forbidden"])
def test_a_push_refused_for_anything_else_is_not_reread(user_worker, monkeypatch, refusal):
    worker, routes = user_worker
    _granted(worker, monkeypatch)
    owned = _fenceless(worker, monkeypatch)
    held = worker._git_token()
    released = len(routes.accessed)
    push, used = _push([refusal])

    with pytest.raises(gitops.GitError) as raised:
        worker._push_reading_again(push, held, branch="swarm/task_1")

    assert raised.value is refusal, "raised as it came"
    assert len(used) == 1
    assert routes.accessed[released:] == [], "a non-credential refusal was re-read"
    assert owned == []
