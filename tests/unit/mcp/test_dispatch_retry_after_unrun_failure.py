"""Re-sending work the platform failed before it ran (part of #2688).

The Product Oracle session, 6 November 2026: its task was failed by the
reconciler before it ever ran -- no attempt, no cost -- and dispatching the
same work again was refused by the repeat guard ("this exact prompt ... was
already dispatched once in this session"), so the session had to change its
prompt to get past it. The guard now reads the earlier task: FAILED or
CANCELLED with no attempt that started is let through and answered as a
retry of it; anything that ran or is running is refused naming it, and
`retry_of` marks a deliberate retry, recorded on the new task.

Every test drives `server._call` against a fake control plane that RECORDS
what it was sent, so "refused before anything was sent" is a measurement.
"""

from __future__ import annotations

import json

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmError


class FakeApi:
    """Answers a dispatch, a batch, a task read and an attempts read; records each."""

    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.batches: list[list[dict]] = []
        self.tasks: dict[str, dict] = {}
        self.attempt_rows: dict[str, list[dict]] = {}
        self.attempts_error: SwarmError | None = None

    def request(self, method, path, **kwargs):
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": 5}}
        raise SwarmError(f"{method} {path} -> 500: no such route", status=500)

    def dispatch(self, **kwargs):
        self.dispatched.append(kwargs)
        task_id = f"task_{len(self.dispatched)}"
        self.tasks[task_id] = {
            "id": task_id,
            "state": "QUEUED",
            "links": {"console": f"https://console.invalid/tasks/{task_id}"},
        }
        return dict(self.tasks[task_id])

    def dispatch_batch(self, payloads):
        self.batches.append(list(payloads))
        return [{"id": f"task_b{i}", "state": "QUEUED"} for i, _ in enumerate(payloads, start=1)]

    def task(self, task_id):
        if task_id not in self.tasks:
            raise SwarmError(f"GET /v1/tasks/{task_id} -> 404: no such task", status=404)
        return dict(self.tasks[task_id])

    def attempts(self, task_id, *, limit=20):
        if self.attempts_error is not None:
            raise self.attempts_error
        return list(self.attempt_rows.get(task_id, []))

    def end(self, task_id, state, *, started=False):
        self.tasks[task_id]["state"] = state
        if started:
            self.tasks[task_id]["started_at"] = "2026-11-06T07:30:00Z"
            self.attempt_rows[task_id] = [{"attempt_id": "a1", "started_at": "2026-11-06T07:30:00Z"}]


def _dispatch(api, **args):
    return json.loads(server._call(api, "swarm_dispatch", {"prompt": "do the work", **args}))


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED"])
def test_a_same_prompt_dispatch_after_a_failure_before_any_attempt_is_accepted_and_linked(state):
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], state)
    # A lease the runner never started is an attempt that did not start.
    api.attempt_rows[first["task_id"]] = [{"attempt_id": "a0", "started_at": None}]

    second = _dispatch(api)

    assert len(api.dispatched) == 2  # MUTATION: the old guard refused the second send
    assert second["task_id"] == "task_2"
    assert second["retry_of"]["task_id"] == first["task_id"]
    assert second["retry_of"]["kind"] == "unrun"
    assert state in second["retry_of"]["why"]
    metadata = api.dispatched[1]["metadata"]
    assert metadata["retry_of"] == first["task_id"] and metadata["retry"] == "unrun"


def test_a_same_prompt_dispatch_after_a_success_is_refused_naming_the_task():
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], "SUCCEEDED", started=True)

    with pytest.raises(SwarmError) as refused:
        _dispatch(api)

    text = str(refused.value)
    assert "already dispatched" in text
    assert first["task_id"] in text and "SUCCEEDED" in text
    assert f"https://console.invalid/tasks/{first['task_id']}" in text
    assert f'retry_of: "{first["task_id"]}"' in text
    assert len(api.dispatched) == 1


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING"])
def test_a_same_prompt_dispatch_while_the_task_is_live_is_refused(state):
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], state)

    with pytest.raises(SwarmError, match=state):
        _dispatch(api)
    assert len(api.dispatched) == 1


def test_a_failure_after_an_attempt_started_is_still_refused():
    """Never weakened for a task that ran: its work was paid for."""
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], "FAILED", started=True)
    del api.tasks[first["task_id"]]["started_at"]  # only the attempt says it ran

    with pytest.raises(SwarmError, match="already dispatched"):
        _dispatch(api)
    assert len(api.dispatched) == 1


def test_an_unread_earlier_task_is_refused_not_retried():
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], "FAILED")
    api.attempts_error = SwarmError("GET attempts -> 503", status=503)

    with pytest.raises(SwarmError, match="already dispatched"):
        _dispatch(api)
    assert len(api.dispatched) == 1


def test_a_deliberate_retry_passes_and_is_recorded():
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], "SUCCEEDED", started=True)

    second = _dispatch(api, retry_of=first["task_id"])

    assert len(api.dispatched) == 2
    assert second["retry_of"]["task_id"] == first["task_id"]
    assert second["retry_of"]["kind"] == "deliberate"
    metadata = api.dispatched[1]["metadata"]
    assert metadata["retry_of"] == first["task_id"] and metadata["retry"] == "deliberate"


def test_a_retry_naming_another_task_is_refused():
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], "SUCCEEDED", started=True)

    with pytest.raises(SwarmError) as refused:
        _dispatch(api, retry_of="task_other")
    assert first["task_id"] in str(refused.value)
    assert len(api.dispatched) == 1


def test_a_third_send_is_judged_against_the_retry_not_the_first():
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], "FAILED")
    second = _dispatch(api)
    api.end(second["task_id"], "SUCCEEDED", started=True)

    with pytest.raises(SwarmError) as refused:
        _dispatch(api)
    assert second["task_id"] in str(refused.value)


def test_a_batch_task_after_an_unrun_failure_is_accepted_and_linked():
    api = FakeApi()
    first = _dispatch(api)
    api.end(first["task_id"], "FAILED")

    reply = json.loads(server._call(api, "swarm_dispatch", {"tasks": [{"prompt": "do the work"}]}))

    (payload,), = api.batches
    assert payload["metadata"]["retry_of"] == first["task_id"]
    assert reply["tasks"][0]["retry_of"]["task_id"] == first["task_id"]


def test_retry_of_is_in_the_tool_schema_for_one_task_and_a_batch_entry():
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_dispatch")["inputSchema"]
    assert "retry_of" in schema["properties"]
    assert "retry_of" in schema["properties"]["tasks"]["items"]["properties"]
