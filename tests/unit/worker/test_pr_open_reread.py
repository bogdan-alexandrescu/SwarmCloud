"""A pull request refused 401 after its branch was pushed is retried once with
a freshly read forge token (PR-OPEN-RETRY, part of #1623).

MEASURED 2026-10-19: task_194ad2144eb6441c8a7c (tenant u-or-9a0792, direct-pr,
product-oracle) pushed its branch and then failed "could not open a pull
request (401): Bad credentials"; the Product Oracle session opened the pull
request by hand (product-oracle #33). Other tasks in the same hour opened
theirs. The publish reads the token ONCE (`_publish_git`) and hands that one
read to the probe, the push and the pull request; only the probe re-read on a
401. A refresh between the read and the pull request -- each refresh makes
GitHub end the access token it replaces (docs/onboarding.md §3.3 item 5) --
leaves the pull request holding a dead token after the push used it.

What is pinned:

  * 401, then 201: the slot is read again ONCE, the pull request is opened
    with the new token, and a `forge_token_reread` mark (reason
    `pr_open_401`) is on the task's events;
  * 401 twice: one re-read, no third try, no sleep, and the task FAILS saying
    `branch <name> pushed; pull request not opened (401 twice)`, with the
    branch kept in the result so a person can open it by hand;
  * 403 (or 404): never re-read -- a permission problem, not a stale token;
  * a person's slot is re-read through swarm-api's broker, never Secret
    Manager and never the tenant's token;
  * a refused write grant, or a refused token, is not re-read at all;
  * no token value is in any log line, event or task field.

MUTATIONS: drop the re-read -- the 401-then-201 test sees FAILED. Re-read on
any ForgeError -- the 403 test sees two posts. Read the tenant secret for the
retry -- the broker test sees a Secret Manager access. Skip the gates -- the
refusal tests see a read.

Every fake token is built at runtime, never written as one literal.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_worker import forge as forge_mod, lifecycle
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
    GITHUB_URL,
    secret_name,
    token,
    user_worker,
)

INTEGRATOR = {"strategy": "integrate", "role": "integrator", "integrates": ["t-gone"]}
TITLED = {
    "prompt": "bring the work together", "steps": 1, "sleep_seconds": 0.01,
    "artifact_name": "pr-title.txt", "artifact_text": "Bring the implementation together\n",
}

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


class _Forge:
    """`forge._request` answering the pull request POST with `statuses` in turn."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.posts: list[str] = []

    def __call__(self, url, *, token, method="GET", payload=None):
        if method == "POST" and url.endswith("/pulls"):
            self.posts.append(token)
            status = self.statuses.pop(0)
            if status == 201:
                return 201, {"number": 7, "html_url": "https://github.com/acme/widgets/pull/7",
                             "state": "open"}
            if status == 401:
                return 401, {"message": "Bad credentials"}
            return status, {"message": "Resource not accessible by integration"}
        return 200, []


class _Reads:
    """`Worker._git_token`: a new version of the slot at every read."""

    def __init__(self) -> None:
        self.values: list[str] = []

    def __call__(self) -> str:
        value = token("abcdefghij"[len(self.values) % 10])
        self.values.append(value)
        return value


def _run(db, worker_factory, monkeypatch, origin: Path, statuses: list[int]):
    seed_attempt(db, task_input=TITLED)
    db.doc("tasks/task_1")["metadata"] = {"dispatch": dict(INTEGRATOR)}
    worker, _config, _ = worker_factory(repository_url=f"file://{origin}")
    reads = _Reads()
    monkeypatch.setattr(worker, "_git_token", reads)
    fake = _Forge(statuses)
    monkeypatch.setattr(forge_mod, "_request", fake)
    monkeypatch.setattr(lifecycle, "open_pull_request", forge_mod.open_pull_request)
    slept: list[float] = []
    worker.forge_sleep = slept.append
    return worker, reads, fake, slept


def _rereads(db) -> list[dict[str, Any]]:
    return [e for e in db.events("task_1")
            if (e.get("detail") or {}).get("cause") == "forge_token_reread"]


def _no_token_anywhere(db, log_stream, values: list[str]) -> None:
    told = repr(db.doc("tasks/task_1")) + repr(db.events("task_1")) + log_stream.getvalue()
    for value in values:
        assert value not in told, "a token value was logged or recorded"


@needs_git
def test_a_401_then_201_rereads_once_and_opens_the_pull_request(
    db, worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    worker, reads, fake, slept = _run(db, worker_factory, monkeypatch, origin, [401, 201])

    assert worker.run() == ExitCode.OK, log_stream.getvalue()[-3000:]
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    assert len(fake.posts) == 2
    # The retry used the read made AFTER the 401, and only one was made.
    first = reads.values.index(fake.posts[0])
    assert fake.posts[1] == reads.values[first + 1]
    assert len(reads.values) == first + 2, "the slot was read more than once again"
    assert task["result_summary"]["git"]["pull_request"]["number"] == 7
    marks = _rereads(db)
    assert len(marks) == 1
    assert marks[0]["detail"]["reason"] == "pr_open_401"
    assert slept == [], "a re-read is not a sleep"
    _no_token_anywhere(db, log_stream, reads.values)


@needs_git
def test_a_401_twice_ends_saying_the_branch_is_pushed_and_the_pull_request_is_not(
    db, worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    worker, reads, fake, slept = _run(db, worker_factory, monkeypatch, origin, [401, 401])

    assert worker.run() == ExitCode.FAILED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task["state"]
    git = task["result_summary"]["git"]
    branch = git["branch"]
    assert branch, "the branch name is kept for a person to open the pull request"
    assert f"branch {branch} pushed; pull request not opened (401 twice)" in task["last_error"]
    assert len(fake.posts) == 2, "exactly one retry"
    first = reads.values.index(fake.posts[0])
    assert len(reads.values) == first + 2, "exactly one re-read"
    assert slept == []
    assert len(_rereads(db)) == 1
    _no_token_anywhere(db, log_stream, reads.values)


@needs_git
@pytest.mark.parametrize("status", [403, 404])
def test_a_permission_refusal_is_never_reread(
    db, worker_factory, monkeypatch, origin, local_urls, forge, log_stream, status
):
    worker, reads, fake, _ = _run(db, worker_factory, monkeypatch, origin, [status])

    worker.run()
    assert len(fake.posts) == 1
    assert reads.values.index(fake.posts[0]) == len(reads.values) - 1, "the slot was re-read"
    assert _rereads(db) == []
    git = db.doc("tasks/task_1")["result_summary"]["git"]
    assert f"({status})" in git["publish_reason"]
    assert "401 twice" not in git["publish_reason"]


# --------------------------------------------------------------------------
# The re-read itself: whose token, and the gates before it
# --------------------------------------------------------------------------


def _access() -> forge_mod.RepoAccess:
    ref = forge_mod.parse_repo(GITHUB_URL)
    assert ref is not None
    return forge_mod.RepoAccess(ref=ref, default_branch="main", can_push=True, reason="")


def _opener(fake: _Forge):
    def open_with(with_token: str) -> forge_mod.PullRequest:
        return forge_mod.open_pull_request(
            access=_access(), token=with_token, head="swarm/task_1", base="main",
            title="t", body="b",
        )
    return open_with


def _granted(worker, monkeypatch) -> None:
    """The submitter's write grant still covers the repository. `user_worker`
    seeds no grant document, so without this the grant gate refuses the
    re-read -- which `test_a_refused_write_grant_is_not_reread` pins."""
    monkeypatch.setattr(worker, "_forge_write_refusal", lambda: None)


def test_a_persons_slot_is_reread_through_the_broker(user_worker, monkeypatch):
    worker, routes = user_worker
    _granted(worker, monkeypatch)
    fake = _Forge([401, 201])
    monkeypatch.setattr(forge_mod, "_request", fake)
    held = worker._git_token()
    released = len(routes.accessed)
    routes.values[secret_name()] = token("b")

    pr = worker._open_pull_request_reading_again(_opener(fake), held, branch="swarm/task_1")

    assert pr.number == 7
    assert fake.posts == [token("a"), token("b")]
    assert routes.accessed[released:] == [secret_name()], "not re-read through the broker once"
    # `user_worker` asserts at teardown that Secret Manager was never asked.


def test_a_refused_write_grant_is_not_reread(user_worker, monkeypatch):
    worker, routes = user_worker
    fake = _Forge([401, 201])
    monkeypatch.setattr(forge_mod, "_request", fake)
    held = worker._git_token()
    released = len(routes.accessed)
    monkeypatch.setattr(worker, "_forge_write_refusal",
                        lambda: (lifecycle.FORGE_GRANT_REVOKED, "the grant was removed"))

    with pytest.raises(forge_mod.ForgeError) as raised:
        worker._open_pull_request_reading_again(_opener(fake), held, branch="swarm/task_1")

    assert routes.accessed[released:] == [], "a refused write still read a token"
    assert len(fake.posts) == 1
    message = str(raised.value)
    assert "branch swarm/task_1 pushed; pull request not opened" in message
    assert "the grant was removed" in message
    assert token("a") not in message


def test_a_refused_token_is_not_reread(user_worker, monkeypatch):
    worker, routes = user_worker
    _granted(worker, monkeypatch)
    fake = _Forge([401, 201])
    monkeypatch.setattr(forge_mod, "_request", fake)
    held = worker._git_token()
    released = len(routes.accessed)
    monkeypatch.setattr(worker, "_git_token_refusal", lambda: "an agent process is alive")

    with pytest.raises(forge_mod.ForgeError) as raised:
        worker._open_pull_request_reading_again(_opener(fake), held, branch="swarm/task_1")

    assert routes.accessed[released:] == []
    assert len(fake.posts) == 1
    assert "an agent process is alive" in str(raised.value)


def test_a_reread_the_broker_refuses_names_the_refusal(user_worker, monkeypatch):
    worker, routes = user_worker
    _granted(worker, monkeypatch)
    fake = _Forge([401, 201])
    monkeypatch.setattr(forge_mod, "_request", fake)
    held = worker._git_token()
    routes.refuse = (403, "connection_refresh_failed")

    with pytest.raises(forge_mod.ForgeError) as raised:
        worker._open_pull_request_reading_again(_opener(fake), held, branch="swarm/task_1")

    assert len(fake.posts) == 1, "no retry without a token, and never the tenant's"
    message = str(raised.value)
    assert "branch swarm/task_1 pushed; pull request not opened" in message
    assert secret_name() in message and "connection_refresh_failed" in message
    assert token("a") not in message
    assert secrets_mod.FORGE_REFUSED_TWICE not in message
