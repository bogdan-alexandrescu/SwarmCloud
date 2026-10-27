"""A git tool the platform did not answer says why (#2452).

WHAT WENT WRONG. On 2026-10-26 two lane steps' publishes through
`git_push_branch` ended "api_unavailable: the platform did not answer; ask
again". `GitTools.call` mapped every retryable `ChildApiError` -- a 5xx, a
429, a read timeout, a refused connection -- to that one sentence, so the
step summary could not tell a swarm-api defect from the worker's own 10 s
read timeout.

WHAT IS PINNED, through the real `ChildApi` over a fake opener:

* a read timeout names `TimeoutError` and says there was no HTTP status;
* a refused connection names `URLError`;
* a 5xx names its status and swarm-api's code, and never the response's
  message (body text) -- not even a credential-shaped one;
* a 5xx with no JSON code names the status alone;
* a code that is not a bare identifier is not quoted;
* the refusal stays `api_unavailable` and retryable, as before;
* a 4xx still answers swarm-api's own code and message, unchanged.

Every fake token is built at runtime.
"""

from __future__ import annotations

import io
import json
import secrets
import urllib.error
from pathlib import Path
from typing import Any, Callable

import pytest

from agent_worker import children as children_mod
from agent_worker import gittools
from agent_worker.logs import build_logger

TASK = "task_0123456789abcdef0123"


def _tools(tmp_path: Path, opener: Callable[..., Any]) -> gittools.GitTools:
    api = children_mod.ChildApi("https://api.invalid", token_fetcher=lambda _a: "id",
                                opener=opener)
    cfg = gittools.GitToolsConfig(
        tenant_id="acme", task_id=TASK, attempt_id="att_1", lease_id="lease_1",
        generation=3, repository="acme/widgets", clone_base="0" * 40,
        pushable=(f"swarm/{TASK}",), author=("A", "a@example.invalid"), protected=("main",),
    )
    for name in ("clone", "private", "logs"):
        (tmp_path / name).mkdir()
    return gittools.GitTools(
        cfg, key=children_mod.AttemptKey(), api=api, clone=tmp_path / "clone",
        private_dir=tmp_path / "private", logs_dir=tmp_path / "logs",
        logger=build_logger(task_id=TASK, attempt_id="att_1", tenant_id="acme", generation=3,
                            runner_profile="mock", stream=io.StringIO()),
    )


def _raising(exc: BaseException) -> Callable[..., Any]:
    def opener(req, timeout=None):
        raise exc
    return opener


def _answering(status: int, payload: Any) -> Callable[..., Any]:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(raw))
    return opener


def _refusal(tmp_path: Path, opener: Callable[..., Any]) -> gittools.GitToolRefused:
    tools = _tools(tmp_path, opener)
    with pytest.raises(gittools.GitToolRefused) as raised:
        tools.call("git_push_branch", {"branch": f"swarm/{TASK}"}, request_id="publish-1")
    return raised.value


def test_a_read_timeout_names_timeouterror_and_no_status(tmp_path: Path):
    refusal = _refusal(tmp_path, _raising(TimeoutError("timed out")))
    assert refusal.code == "api_unavailable" and refusal.retryable
    # MUTATION: answer the bare "did not answer; ask again" and this fails.
    assert refusal.message == ("the platform did not answer (TimeoutError, no HTTP status); "
                               "ask again")


def test_a_refused_connection_names_urlerror(tmp_path: Path):
    refusal = _refusal(tmp_path, _raising(urllib.error.URLError("connection refused")))
    assert "(URLError, no HTTP status)" in refusal.message
    assert "connection refused" not in refusal.message


def test_a_5xx_names_its_status_and_code_and_never_its_body(tmp_path: Path):
    token = "ghs" + "_" + secrets.token_hex(18)
    refusal = _refusal(tmp_path, _answering(504, {
        "code": "timed_out", "message": f"git ran longer than 300s with {token}"}))
    assert refusal.code == "api_unavailable" and refusal.retryable
    # MUTATION: drop the status or the code and the step summary loses its cause.
    assert refusal.message == "the platform answered HTTP 504 timed_out; ask again"
    # MUTATION: quote exc.message and the body (and its token) reaches the summary.
    assert token not in refusal.message
    assert "300s" not in refusal.message


def test_a_5xx_without_a_json_code_names_the_status_alone(tmp_path: Path):
    refusal = _refusal(tmp_path, _answering(502, b"<html>Bad Gateway</html>"))
    assert refusal.message == "the platform answered HTTP 502; ask again"


def test_a_code_that_is_not_an_identifier_is_not_quoted(tmp_path: Path):
    refusal = _refusal(tmp_path, _answering(500, {"code": "see https://x.invalid/?a=b c"}))
    assert refusal.message == "the platform answered HTTP 500; ask again"


def test_a_429_is_named_too(tmp_path: Path):
    refusal = _refusal(tmp_path, _answering(429, {"code": "rate_limited", "message": "slow"}))
    assert refusal.retryable
    assert refusal.message == "the platform answered HTTP 429 rate_limited; ask again"


def test_a_4xx_still_answers_swarm_apis_own_code_and_message(tmp_path: Path):
    refusal = _refusal(tmp_path, _answering(422, {"code": "bad_bundle",
                                                  "message": "the bundle is not base64"}))
    assert (refusal.code, refusal.message) == ("bad_bundle", "the bundle is not base64")
    assert not refusal.retryable
