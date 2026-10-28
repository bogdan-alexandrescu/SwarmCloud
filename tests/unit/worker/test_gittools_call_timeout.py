"""The git-tools relay waits as long as swarm-api's git does (#2576).

swarm-api runs `git_push_branch` and `git_fetch` (and so `git_rebase_onto`,
served as the fetch it begins) against the forge under
`gitexec.CALL_DEADLINE_SECONDS`; the worker posted them through the
attempt's `ChildApi` with its 10 s default, so a workflow-touching push came
back as a retryable `api_unavailable` while swarm-api was still pushing, and
the step failed `publish_failed`. These tests drive the real `ChildApi` with a
fake opener that records the `timeout=` each request is opened with.
"""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import gittools
from agent_worker.gitmcp import server as shim
from agent_worker.logs import build_logger
from swarm_api import gitexec

TASK = "task_timeout"


class _Answer:
    status = 200

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


class Opener:
    """Records each request's `timeout=`; answers 200 `{}` or raises `fail`."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.timeouts: list[float] = []
        self.fail = fail

    def __call__(self, req, *, timeout):
        self.timeouts.append(timeout)
        if self.fail is not None:
            raise self.fail
        return _Answer()


class Clock:
    """A monotonic clock the opener moves, so the seconds waited are known."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tools(tmp_path: Path, opener: Opener, clock: Clock | None = None) -> gittools.GitTools:
    client = children_mod.ChildApi("https://api.invalid", token_fetcher=lambda _a: "id",
                                   opener=opener)
    cfg = gittools.GitToolsConfig(
        tenant_id="acme", task_id=TASK, attempt_id="att_1", lease_id="lease_1",
        generation=3, repository="acme/widgets", clone_base="0" * 40,
        pushable=("swarm/x",), author=("Agent", "agent@example.invalid"),
    )
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {"clock": clock} if clock is not None else {}
    return gittools.GitTools(
        cfg, key=children_mod.AttemptKey(), api=client, clone=tmp_path / "clone",
        private_dir=tmp_path / "private", logs_dir=logs,
        logger=build_logger(task_id=TASK, attempt_id="att_1", tenant_id="acme",
                            generation=3, runner_profile="mock", stream=io.StringIO()),
        **extra,
    )


def test_git_push_branch_waits_past_swarm_apis_call_deadline(tmp_path: Path) -> None:
    opener = Opener()
    _tools(tmp_path, opener).call("git_push_branch", {"branch": "swarm/x"}, request_id="r1")
    assert opener.timeouts[0] >= gitexec.CALL_DEADLINE_SECONDS
    assert opener.timeouts == [gittools.GIT_CALL_TIMEOUT_SECONDS]
    # A margin for the bundle upload and the answer, not the deadline itself.
    assert opener.timeouts[0] > gitexec.CALL_DEADLINE_SECONDS


@pytest.mark.parametrize("tool", ["git_fetch", "git_rebase_onto"])
def test_git_fetch_and_rebase_onto_wait_past_the_deadline(tmp_path: Path, tool: str) -> None:
    opener = Opener()
    _tools(tmp_path, opener).call(tool, {"ref": "main"}, request_id="r1")
    assert opener.timeouts[0] > gitexec.CALL_DEADLINE_SECONDS
    assert opener.timeouts == [gittools.GIT_CALL_TIMEOUT_SECONDS]


@pytest.mark.parametrize("tool", ["pr_checks", "read_private", "pr_view", "issue_comment"])
def test_other_tools_keep_the_short_timeout(tmp_path: Path, tool: str) -> None:
    opener = Opener()
    _tools(tmp_path, opener).call(tool, {}, request_id="r1")
    assert opener.timeouts == [children_mod._TIMEOUT]
    assert getattr(gittools, "GIT_CALL_TOOLS", None) == {"git_push_branch", "git_fetch", "git_rebase_onto"}
    # The child-path routes call `ChildApi.call` with no per-call timeout.
    direct = Opener()
    client = children_mod.ChildApi("https://api.invalid", token_fetcher=lambda _a: "id",
                                   opener=direct)
    client.call("GET", "/v1/anything")
    client.call("GET", "/v1/anything", timeout=None)
    assert direct.timeouts == [children_mod._TIMEOUT, children_mod._TIMEOUT]


def test_the_relay_timeout_is_below_the_shims_wait() -> None:
    # The agent's MCP shim waits WAIT_SECONDS for the relay's answer: past it
    # the agent is told nothing came, while the relay is still waiting.
    assert gitexec.CALL_DEADLINE_SECONDS < gittools.GIT_CALL_TIMEOUT_SECONDS < shim.WAIT_SECONDS


def test_a_client_timeout_names_its_class_and_seconds_and_a_5xx_its_status(
        tmp_path: Path) -> None:
    clock = Clock()

    class Slow(Opener):
        def __call__(self, req, *, timeout):
            clock.now += timeout
            return super().__call__(req, timeout=timeout)

    tools = _tools(tmp_path, Slow(fail=TimeoutError("timed out")), clock)
    with pytest.raises(gittools.GitToolRefused) as refused:
        tools.call("git_push_branch", {"branch": "swarm/x"}, request_id="r1")
    assert refused.value.code == "api_unavailable"
    assert refused.value.retryable is True
    assert "TimeoutError" in refused.value.message
    assert f"after {gittools.GIT_CALL_TIMEOUT_SECONDS} s" in refused.value.message
    assert "HTTP" not in refused.value.message

    failing = Opener(fail=urllib.error.HTTPError(
        "https://api.invalid", 503, "x", {}, io.BytesIO(b'{"code": "busy"}')))
    with pytest.raises(gittools.GitToolRefused) as refused:
        _tools(tmp_path, failing).call("git_fetch", {"ref": "main"}, request_id="r2")
    assert refused.value.code == "api_unavailable"
    assert refused.value.retryable is True
    assert "HTTP 503 busy" in refused.value.message
    assert "TimeoutError" not in refused.value.message
