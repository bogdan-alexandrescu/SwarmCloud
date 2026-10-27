"""A git tool's `api_unavailable` names what the platform answered (#2452).

MEASURED (task_1297d8f4, task_f2b72856, 2026-10-26): both steps' summaries
read only "it answered api_unavailable: the platform did not answer; ask
again". `GitTools.call` dropped the `ChildApiError` it caught, so nobody could
tell a timeout from a 503 from swarm-api failing on a merge bundle, and the
broker's side was unreadable from the step.

The refusal keeps its code (`api_unavailable`, retryable: callers key on it)
and its message now carries the HTTP status and swarm-api's error code, or
the exception class when nothing answered. Never swarm-api's free-text
message: it is not this worker's to repeat.
"""

from __future__ import annotations

import shutil

import pytest

from agent_worker import children as children_mod, gittools

from test_gittools import World

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


class _Raising:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def call(self, *_a, **_k):
        raise self.exc


def _refusal(tmp_path, exc: Exception) -> gittools.GitToolRefused:
    world = World(tmp_path)
    world.tools._api = _Raising(exc)
    with pytest.raises(gittools.GitToolRefused) as caught:
        world.tools.call("git_push_branch", {"branch": "x"}, request_id="r1")
    return caught.value


def test_a_5xx_names_its_status_and_code(tmp_path):
    secret_text = "internal detail " + "z" * 12
    refusal = _refusal(tmp_path, children_mod.ChildApiError(
        503, "internal_error", secret_text, retryable=True))
    assert refusal.code == "api_unavailable" and refusal.retryable is True
    # MUTATION: keep the bare message -> neither the status nor the code is named.
    assert "HTTP 503" in refusal.message and "internal_error" in refusal.message
    assert "ask again" in refusal.message
    # MUTATION: echo swarm-api's message -> its text reaches the step summary.
    assert secret_text not in refusal.message


def test_no_answer_names_the_exception_class(tmp_path):
    refusal = _refusal(tmp_path, children_mod.ChildApiError(
        None, "api_unavailable", "TimeoutError", retryable=True))
    assert refusal.code == "api_unavailable" and refusal.retryable is True
    # MUTATION: keep the bare message -> a timeout reads like any other blip.
    assert "TimeoutError" in refusal.message and "no answer" in refusal.message


def test_a_non_retryable_refusal_keeps_its_own_code(tmp_path):
    refusal = _refusal(tmp_path, children_mod.ChildApiError(
        422, "workflow_scope_missing", "the push touches x", retryable=False))
    assert refusal.code == "workflow_scope_missing" and refusal.retryable is False
    assert refusal.message == "the push touches x"
