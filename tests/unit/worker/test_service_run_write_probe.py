"""A service run's probes before a write read the token for write (#2907).

A service submission's run (swarm-ci-fix and every other platform service
account) is minted an App installation token by swarm-api's broker
(DAYONE-SERVICE-APP-TOKEN), `RUN_WRITE_PERMISSIONS` when the worker asks for
`write` and `RUN_READ_PERMISSIONS` when it asks for `read`. The publish probe,
and every other probe that comes before a push or a pull request, read the
token with `_git_token()`, which asked for `read`: the broker minted a
read-only token, the probe answered "the token has none but not push" or "an
App installation token whose receive-pack advertisement GitHub refused (HTTP
403)", and every CI fix round that had to push died at publish (8 in the 36
hours to 10:06Z on 5 November).

Pinned here:

  * a service run whose SIGNED `forge_access` is `write` asks the broker for
    `write` from every probe before a write: the publish, publish_to_pr, the
    push-scope and carrier-target probes, the CI fix start check, the
    no-change comment, and the re-read after a 401;
  * a service run signed for `read` (or with no `forge_access`, which the
    broker refuses a write for) still asks for `read`, and is refused with
    today's sentences;
  * a person's run asks for `read`, as before: `_forge_write_refusal` gates
    its write;
  * `_git_token()` with no argument still asks for `read` (the clone).

Every fake token is built at runtime, never written as one literal.
"""

from __future__ import annotations

import secrets
import types
from typing import Any

import pytest

from agent_worker import forge as forge_mod
from agent_worker import lifecycle

from test_forge_broker_client import FakeBrokerRoutes, broker_worker
from test_forge_credential import GITHUB_URL
from test_service_app_token_worker import GIT_SECRET, minted_token, service_worker

#: GitHub's answer to a read-only installation token's receive-pack ask,
#: the sentence the probe has always said (`forge.probe_repository`).
REFUSED_403 = ("an App installation token whose receive-pack advertisement GitHub "
               "refused (HTTP 403)")


def _access(can_push: bool, reason: str) -> forge_mod.RepoAccess:
    ref = forge_mod.parse_repo(GITHUB_URL)
    assert ref is not None
    return forge_mod.RepoAccess(ref=ref, default_branch="main" if can_push else "",
                                can_push=can_push, reason=reason)


def _refusing_probe(seen: list[str | None]):
    def probe(*, url: str, token: str | None) -> forge_mod.RepoAccess:
        seen.append(token)
        return _access(False, REFUSED_403)
    return probe


# --------------------------------------------------------------------------
# `_git_token(write=...)` and what it asks the broker for
# --------------------------------------------------------------------------


def test_a_service_runs_write_read_asks_the_broker_for_write(db, worker_factory):
    worker, client, routes = service_worker(db, worker_factory)

    assert worker._git_token(write=True) == minted_token()

    assert routes.requests[-1]["access"] == "write"
    assert client.accessed == []
    assert worker._forge_credentials_read[-1] == ("app_installation", GIT_SECRET)


def test_the_clone_still_asks_for_read(db, worker_factory):
    worker, _, routes = service_worker(db, worker_factory)

    worker._git_token()

    assert routes.requests[-1]["access"] == "read"


@pytest.mark.parametrize("access", ["read", None])
def test_a_service_run_that_may_not_write_still_asks_for_read(db, worker_factory, access):
    """`read`, or no `forge_access` at all: the broker refuses either a write
    (`decide_run_token`), so it is never asked for one."""
    worker, _, routes = service_worker(db, worker_factory)
    if access is None:
        worker._task.pop("forge_access")
    else:
        worker._task["forge_access"] = access

    assert worker._git_token(write=True) == minted_token()

    assert routes.requests[-1]["access"] == "read"


def test_a_persons_run_is_unchanged(db, worker_factory):
    routes = FakeBrokerRoutes()
    worker, _ = broker_worker(worker_factory, routes)

    worker._git_token(write=True)

    assert routes.requests[-1]["access"] == "read"


def test_a_write_the_broker_refuses_falls_back_to_the_read_token(db, worker_factory):
    """A write refused as a write (`grant_read_only`) reads what today's run
    reads, so the probe refuses the push with today's sentence, never a crash."""
    worker, client, routes = service_worker(db, worker_factory)
    original = routes.call

    def call(method, path, *, body=None, headers=None):
        # The broker refuses the write and answers the read, as
        # `decide_run_token` does for a run whose write it does not allow.
        routes.refuse = (403, "grant_read_only") if b'"write"' in (body or b"") else None
        return original(method, path, body=body, headers=headers)

    routes.call = call  # type: ignore[method-assign]

    assert worker._git_token(write=True) == minted_token()
    assert [r["access"] for r in routes.requests] == ["write", "read"]
    assert client.accessed == []


# --------------------------------------------------------------------------
# Each probe before a write
# --------------------------------------------------------------------------


@pytest.fixture
def recorded(db, worker_factory, monkeypatch):
    """A service run whose `_git_token` records the `write` it was asked with."""
    worker, _, _ = service_worker(db, worker_factory)
    worker._repo_url = GITHUB_URL
    asks: list[bool] = []
    fake = "ghs" + "_" + secrets.token_hex(18)

    def git_token(*, write: bool = False) -> str:
        asks.append(write)
        return fake

    worker._git_token = git_token  # type: ignore[method-assign]
    worker._forge_write_refusal = lambda: None  # type: ignore[method-assign]
    seen: list[str | None] = []
    monkeypatch.setattr(lifecycle, "probe_repository", _refusing_probe(seen))
    return types.SimpleNamespace(worker=worker, asks=asks, seen=seen, token=fake)


def test_the_publish_probe_asks_for_write(recorded, monkeypatch):
    worker = recorded.worker
    worker.ws = types.SimpleNamespace()
    monkeypatch.setattr(worker, "_dispatch_strategy", lambda: "direct-pr")
    monkeypatch.setattr(worker, "_pr_role", lambda: None)

    out = worker._publish_git(repo=None, work_head=None, publish=True)

    assert recorded.asks == [True]
    assert out["published"] is False and out["publish_reason"] == REFUSED_403


def test_the_push_scope_probe_asks_for_write(recorded):
    worker = recorded.worker

    failure = worker._carrier_scope_refusal()

    assert recorded.asks == [True]
    assert failure is not None and REFUSED_403 in failure.args


def test_the_carrier_target_probe_asks_for_write(recorded):
    worker = recorded.worker
    worker._publish_base = "0" * 40

    assert worker._carrier_target() == REFUSED_403
    assert recorded.asks == [True]


def test_the_publish_to_pr_probe_asks_for_write(recorded, monkeypatch):
    worker = recorded.worker
    monkeypatch.setattr(lifecycle, "probe_repository", lambda **_: None)

    with pytest.raises(lifecycle.WorkerError, match="not on a forge"):
        worker._read_pull_at_start(GITHUB_URL, types.SimpleNamespace(number=7))
    assert recorded.asks == [True]


def test_the_ci_fix_start_check_asks_for_write(recorded, monkeypatch):
    worker = recorded.worker
    worker._task["metadata"] = {"dispatch": {"ci_fix_head": {
        "pull_request": 7, "head_sha": "a" * 40}}}
    worker._restored_from = None
    monkeypatch.setattr(lifecycle, "probe_repository", lambda **_: None)

    worker._ci_fix_superseded({"from_checkpoint": False})

    assert recorded.asks == [True]


def test_the_no_change_comment_probe_asks_for_write(recorded, monkeypatch):
    worker = recorded.worker
    monkeypatch.setattr(worker, "_onto_pr_target", lambda: types.SimpleNamespace(number=7))
    summary: dict[str, Any] = {lifecycle.NO_CHANGE_REASON_SUMMARY_KEY: "nothing to change"}

    worker._post_no_change_evidence(summary)

    assert recorded.asks == [True]
    assert summary[lifecycle.NO_CHANGE_COMMENT_SUMMARY_KEY]["reason"] == REFUSED_403


def test_the_re_read_after_a_401_asks_for_write(recorded, monkeypatch):
    worker = recorded.worker
    answers = iter([_access(False, "the token was rejected (401)"),
                    _access(True, "the token has write permission on this repository")])
    monkeypatch.setattr(lifecycle, "probe_repository", lambda **_: next(answers))

    _, access = worker._probe_reading_again(GITHUB_URL, recorded.token, "the publish probe")

    assert access is not None and access.can_push
    assert recorded.asks == [True]


# --------------------------------------------------------------------------
# A run that may not write is refused as today
# --------------------------------------------------------------------------


def test_a_read_signed_service_run_is_refused_with_todays_sentence(db, worker_factory,
                                                                   monkeypatch):
    """Its push-scope probe has no grant gate before the token, so it reads
    the token: for `read`, and the probe's refusal is the sentence it was."""
    worker, _, routes = service_worker(db, worker_factory)
    worker._task["forge_access"] = "read"
    worker._repo_url = GITHUB_URL
    seen: list[str | None] = []
    monkeypatch.setattr(lifecycle, "probe_repository", _refusing_probe(seen))

    failure = worker._carrier_scope_refusal()

    assert [r["access"] for r in routes.requests] == ["read"]
    assert seen == [minted_token()]
    assert failure is not None and REFUSED_403 in failure.args


def test_a_read_signed_service_runs_publish_reads_no_token(db, worker_factory, monkeypatch):
    worker, _, routes = service_worker(db, worker_factory)
    worker._task["forge_access"] = "read"
    worker._repo_url = GITHUB_URL
    worker.ws = types.SimpleNamespace()
    monkeypatch.setattr(worker, "_dispatch_strategy", lambda: "direct-pr")
    monkeypatch.setattr(worker, "_pr_role", lambda: None)

    out = worker._publish_git(repo=None, work_head=None, publish=True)

    assert out["publish_reason"] == (
        "forge_read_grant: this task's grant on its repository is read-only")
    assert routes.requests == []
