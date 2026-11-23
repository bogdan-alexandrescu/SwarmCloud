"""A dispatch or workflow submission waits 120 seconds, and a timeout says the
first call may have been accepted (improvement plan 2026-11-23, item 4).

A batch POST that outlives its timeout can still have been written: sending
it again duplicates every task. So the error tells the caller to read the
running workflows first. Offline: the real `SwarmClient` over a replaced
`client._open`, the seam test_client_gzip.py uses.
"""

from __future__ import annotations

import email.message
import io
import json
import socket
import urllib.error

import pytest

from swarm_mcp import client as mcp_client
from swarm_mcp import workflows
from swarm_mcp.client import SUBMIT_TIMEOUT_SECONDS, SwarmClient, SwarmError


class _Response:
    status = 200

    def __init__(self, payload) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode())
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture()
def wire(monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    state = type("Wire", (), {})()
    state.timeouts = []
    state.fail = None
    state.answer = {"tasks": [{"task_id": "t-1"}]}

    def _opener(req, timeout=None):
        state.timeouts.append((req.get_method(), req.full_url, timeout))
        if state.fail is not None:
            raise state.fail
        return _Response(state.answer)

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return state


def test_the_submission_timeout_is_120_seconds() -> None:
    assert SUBMIT_TIMEOUT_SECONDS == 120


def test_dispatch_and_batch_post_with_the_submission_timeout(wire):
    client = SwarmClient(base_url="http://api.invalid")
    client.dispatch_batch([{"prompt": "x"}])
    wire.answer = {"task_id": "t-2"}
    client.dispatch(repository_url="https://example.invalid/o/r", prompt="x", runner_profile="claude-code")
    assert [t for (_m, _u, t) in wire.timeouts] == [120, 120]


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), urllib.error.URLError(socket.timeout("timed out"))],
    ids=["read-timeout", "connect-timeout"],
)
def test_a_timed_out_batch_says_read_the_running_workflows_first(wire, failure):
    wire.fail = failure
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="http://api.invalid").dispatch_batch([{"prompt": "x"}])
    text = str(caught.value)
    assert "120s" in text
    assert "may have been accepted" in text
    assert "read the running workflows" in text
    assert "before sending again" in text


def test_a_timed_out_workflow_submission_says_the_same(wire):
    wire.fail = TimeoutError("timed out")
    with pytest.raises(SwarmError) as caught:
        workflows.submit(SwarmClient(base_url="http://api.invalid"), steps=[{"id": "a", "prompt": "x"}])
    assert "read the running workflows" in str(caught.value)
    assert wire.timeouts[0][2] == 120


def test_workflow_submission_goes_through_submit() -> None:
    import inspect

    source = inspect.getsource(workflows.submit)
    assert 'send_submission("/v1/workflows", body,' in source
    assert '"POST", "/v1/workflows", payload=keyed, timeout=SUBMIT_TIMEOUT_SECONDS)' in source


def test_a_refusal_is_not_called_a_timeout(wire):
    headers = email.message.Message()
    wire.fail = urllib.error.HTTPError("http://api.invalid/v1/tasks/batch", 400, "Bad", headers,
                                       io.BytesIO(b'{"code": "bad", "message": "no"}'))
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="http://api.invalid").dispatch_batch([{"prompt": "x"}])
    assert "may have been accepted" not in str(caught.value)
