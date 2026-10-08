"""A forge 401 makes the worker read the task's forge secret again and retry once.

MEASURED 2026-10-08 08:20Z: the owner's user slot swarm-tenant-eng-git-u-...
reached 35 versions within minutes, and a claude-code task holding one of
them failed "could not fetch issue #780: the forge refused the credential
(401: Bad credentials)". Every refresh -- swarm-api's on every call then, the
sweep's still -- makes GitHub drop the access token it replaces, so a token a
worker read a moment earlier can be dead by the time it is used.

Owner decision 2026-10-08, the worker's half: on a 401 from the forge, read
the slot's latest version again and retry the call ONCE. Pinned here, for the
issue fetch, the clone and the push probe the publish and the carrier make:

  * 401, then the re-read token -> the call succeeds with the NEW token;
  * 401 twice -> a clear failure that says the token was re-read and refused
    again, so the person reconnects rather than waits;
  * anything that is not the credential (a 404, a missing repository) is
    never retried: a re-read cannot fix it.

Every fake token is built at runtime, never written as one literal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_worker import forge as forge_mod
from agent_worker import issue as issue_mod
from agent_worker import lifecycle
from agent_worker import secrets as secrets_mod
from agent_worker.gitops import GitError
from swarm_api import gittokens

from fakes import FakeSecretClient
from worker_seeds import TENANT, seed_attempt

PERSON = "ada@example.com"
GITHUB_URL = "https://github.com/saga/widgets.git"
REFUSED_ISSUE = ("could not fetch issue #780 of saga/widgets: the forge refused the "
                 "credential (401: Bad credentials)")


def token(fill: str) -> str:
    return "ghu_" + fill * 36


def slot() -> str:
    return gittokens.provider_suffix(gittokens.Scope.USER, user=PERSON)


def secret_name() -> str:
    return f"swarm-tenant-{TENANT}-{slot()}"


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []

    def warning(self, message: str, **fields: Any) -> None:
        self.lines.append(("warning", message, fields))

    def error(self, message: str, **fields: Any) -> None:
        self.lines.append(("error", message, fields))

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append(("info", message, fields))


@pytest.fixture
def user_worker(db, worker_factory):
    seed_attempt(db)
    client = FakeSecretClient({secret_name(): token("a")})
    worker, _, _ = worker_factory(secret_client=client)
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": slot()}
    worker._forge_signed = True
    worker._repo_url = GITHUB_URL
    return worker, client


# --------------------------------------------------------------------------
# What counts as the forge refusing the credential
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    REFUSED_ISSUE,
    "the token was rejected (401)",
    "git step 0 failed with exit 128: remote: Invalid username or password.\n"
    "fatal: Authentication failed for 'https://github.com/saga/widgets.git/'",
    "fatal: unable to access 'https://github.com/saga/widgets.git/': The requested URL "
    "returned error: 401",
])
def test_a_401_or_gits_authentication_failure_is_a_refused_credential(text):
    assert secrets_mod.forge_credential_refused(text)
    assert secrets_mod.forge_credential_refused(RuntimeError(text))


@pytest.mark.parametrize("text", [
    "could not fetch issue #780 of saga/widgets: the forge has no such issue (404)",
    "could not fetch issue #780 of saga/widgets: the forge refused the credential (403: "
    "Resource not accessible by integration)",
    "the token cannot see this repository (404)",
    "remote: Repository not found.",
    "git step 0 timed out after 300s",
    "",
])
def test_anything_else_is_not(text):
    assert not secrets_mod.forge_credential_refused(text)


# --------------------------------------------------------------------------
# The helper every call site goes through
# --------------------------------------------------------------------------


def test_a_refused_call_is_tried_once_more_and_its_answer_returned():
    calls: list[bool] = []

    def call(again: bool) -> str:
        calls.append(again)
        if not again:
            raise issue_mod.IssueUnavailable(REFUSED_ISSUE)
        return "fetched"

    log = _Log()
    assert secrets_mod.call_reading_again(
        call, logger=log, what="the issue fetch",
        twice=lambda exc: RuntimeError(str(exc)),
    ) == "fetched"
    assert calls == [False, True]
    assert any(level == "warning" for level, _, _ in log.lines)


def test_a_call_refused_twice_ends_with_the_callers_clear_error():
    calls: list[bool] = []

    def call(again: bool) -> str:
        calls.append(again)
        raise issue_mod.IssueUnavailable(REFUSED_ISSUE)

    with pytest.raises(issue_mod.IssueUnavailable) as raised:
        secrets_mod.call_reading_again(
            call, logger=_Log(), what="the issue fetch",
            twice=lambda exc: issue_mod.IssueUnavailable(
                f"{exc}; {secrets_mod.FORGE_REFUSED_TWICE}"),
        )
    assert calls == [False, True], "retried exactly once, never a third time"
    assert secrets_mod.FORGE_REFUSED_TWICE in str(raised.value)
    assert "reconnect" in secrets_mod.FORGE_REFUSED_TWICE.lower()


def test_a_failure_that_is_not_the_credential_is_never_retried():
    calls: list[bool] = []

    def call(again: bool) -> str:
        calls.append(again)
        raise GitError("remote: Repository not found.")

    with pytest.raises(GitError):
        secrets_mod.call_reading_again(call, logger=_Log(), what="the clone",
                                       twice=lambda exc: exc)
    assert calls == [False]


# --------------------------------------------------------------------------
# The issue fetch (the measured failure)
# --------------------------------------------------------------------------


def test_the_issue_fetch_rereads_the_slot_after_a_401_and_succeeds(user_worker, monkeypatch,
                                                                   tmp_path):
    worker, client = user_worker
    seen: list[str | None] = []

    def stage_issue(**kwargs: Any) -> Path:
        seen.append(kwargs["token"])
        if len(seen) == 1:
            # swarm-api refreshed meanwhile: a new version is the latest, and
            # GitHub no longer accepts the one this worker read.
            client.values[secret_name()] = token("b")
            raise issue_mod.IssueUnavailable(REFUSED_ISSUE)
        assert kwargs["prefetched"] is None, "the retry fetches anew, not the failed prefetch"
        return tmp_path / "issue.md"

    monkeypatch.setattr(lifecycle.issue_mod, "stage_issue", stage_issue)
    assert worker._stage_issue(780, tmp_path, None) == tmp_path / "issue.md"
    assert seen == [token("a"), token("b")]
    assert client.accessed == [secret_name(), secret_name()]


def test_the_issue_fetch_refused_twice_fails_clearly(user_worker, monkeypatch, tmp_path):
    worker, client = user_worker
    seen: list[str | None] = []

    def stage_issue(**kwargs: Any) -> Path:
        seen.append(kwargs["token"])
        raise issue_mod.IssueUnavailable(REFUSED_ISSUE)

    monkeypatch.setattr(lifecycle.issue_mod, "stage_issue", stage_issue)
    with pytest.raises(issue_mod.IssueUnavailable) as raised:
        worker._stage_issue(780, tmp_path, None)
    assert len(seen) == 2
    message = str(raised.value)
    assert "401" in message and secrets_mod.FORGE_REFUSED_TWICE in message
    assert token("a") not in message


def test_an_issue_that_is_not_there_is_not_fetched_twice(user_worker, monkeypatch, tmp_path):
    worker, _ = user_worker
    seen: list[str | None] = []

    def stage_issue(**kwargs: Any) -> Path:
        seen.append(kwargs["token"])
        raise issue_mod.IssueUnavailable(
            "could not fetch issue #780 of saga/widgets: the forge has no such issue (404)")

    monkeypatch.setattr(lifecycle.issue_mod, "stage_issue", stage_issue)
    with pytest.raises(issue_mod.IssueUnavailable):
        worker._stage_issue(780, tmp_path, None)
    assert len(seen) == 1


# --------------------------------------------------------------------------
# The push probe (publish and carrier): the token the push then uses
# --------------------------------------------------------------------------


def _access(can_push: bool, reason: str) -> forge_mod.RepoAccess:
    ref = forge_mod.parse_repo(GITHUB_URL)
    assert ref is not None
    return forge_mod.RepoAccess(ref=ref, default_branch="main" if can_push else "",
                                can_push=can_push, reason=reason)


def test_the_push_probe_rereads_after_a_401_and_the_push_uses_the_new_token(user_worker,
                                                                           monkeypatch):
    worker, client = user_worker
    seen: list[str | None] = []

    def probe(*, url: str, token: str | None) -> forge_mod.RepoAccess:
        seen.append(token)
        if len(seen) == 1:
            client.values[secret_name()] = token_b
            return _access(False, "the token was rejected (401)")
        return _access(True, "the token has write permission on this repository")

    token_b = token("b")
    monkeypatch.setattr(lifecycle, "probe_repository", probe)
    used, access = worker._probe_reading_again(GITHUB_URL, worker._git_token(), "the probe")
    assert seen == [token("a"), token("b")]
    assert used == token("b") and access is not None and access.can_push


def test_the_push_probe_refused_twice_says_so(user_worker, monkeypatch):
    worker, _ = user_worker
    seen: list[str | None] = []

    def probe(*, url: str, token: str | None) -> forge_mod.RepoAccess:
        seen.append(token)
        return _access(False, "the token was rejected (401)")

    monkeypatch.setattr(lifecycle, "probe_repository", probe)
    _, access = worker._probe_reading_again(GITHUB_URL, worker._git_token(), "the probe")
    assert len(seen) == 2
    assert access is not None and not access.can_push
    assert secrets_mod.FORGE_REFUSED_TWICE in access.reason


def test_a_probe_that_cannot_push_for_another_reason_is_asked_once(user_worker, monkeypatch):
    worker, _ = user_worker
    seen: list[str | None] = []

    def probe(*, url: str, token: str | None) -> forge_mod.RepoAccess:
        seen.append(token)
        return _access(False, "the token cannot see this repository (404)")

    monkeypatch.setattr(lifecycle, "probe_repository", probe)
    worker._probe_reading_again(GITHUB_URL, worker._git_token(), "the probe")
    assert len(seen) == 1
