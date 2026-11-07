"""`swarm_dispatch` sends a single task's description (owner, 2026-11-07, part of #2688).

One task and each entry of `tasks` take an optional `description`, checked
by the workflow's `workflows.check_description` (plain text, at most
DESCRIPTION_MAX_CHARS, refused, never cut) before anything is sent, and sent
as `metadata.description`, as a workflow's is. A dispatch without one sends
what it always sent.

Every tool test drives `server._call` against a fake control plane that
RECORDS what it was sent, so "nothing was sent" is a measurement.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from swarm_mcp import server, workflows
from swarm_mcp.client import SwarmError, task_payload

DESCRIPTION = ("Rebuilds the size chart from the catalogue. The chart reads one table, "
               "and the page shows it under the product name.")


def _prompt() -> str:
    # Unique per call: the bridge refuses a repeat of a dispatch this session made.
    return "summarise the source " + secrets.token_hex(6)


class RecordingApi:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.sent.append((method, path, payload))
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": 5}}
        raise SwarmError(f"{method} {path} -> 404: no such route", status=404)

    def dispatch(self, **fields: Any) -> dict[str, Any]:
        self.sent.append(("POST", "/v1/tasks", task_payload(**fields)))
        return {"id": f"task_{len(self.sent)}", "state": "QUEUED"}

    def dispatch_batch(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.sent.append(("POST", "/v1/tasks/batch", {"tasks": list(payloads)}))
        return [{"id": f"task_b{i}", "state": "QUEUED"} for i, _ in enumerate(payloads, start=1)]


def _posted(api: RecordingApi, path: str) -> list[Any]:
    return [body for method, p, body in api.sent if method == "POST" and p == path]


def test_the_schema_offers_a_description_for_one_task_and_each_entry():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_dispatch"]
    properties = tool["inputSchema"]["properties"]
    assert properties["description"]["type"] == "string"
    assert properties["tasks"]["items"]["properties"]["description"]["type"] == "string"


def test_one_task_sends_its_description_as_metadata():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": _prompt(), "label": "Size chart",
                                         "description": f"  {DESCRIPTION} "})
    (body,) = _posted(api, "/v1/tasks")
    assert body["metadata"]["description"] == DESCRIPTION
    assert body["metadata"]["unit"] == "Size chart"
    assert "description" not in body["input"]


def test_a_dispatch_without_one_sends_no_key():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": _prompt()})
    (body,) = _posted(api, "/v1/tasks")
    assert "description" not in body["metadata"]


def test_each_batch_entry_sends_its_own():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"tasks": [
        {"prompt": _prompt(), "description": DESCRIPTION},
        {"prompt": _prompt()},
    ]})
    (batch,) = _posted(api, "/v1/tasks/batch")
    first, second = batch["tasks"]
    assert first["metadata"]["description"] == DESCRIPTION
    assert "description" not in second["metadata"]


@pytest.mark.parametrize("value, match", [
    ("a" * (workflows.DESCRIPTION_MAX_CHARS + 1), "holds at most 600"),
    ("", "non-empty text"),
    ("Adds a <b>size</b> chart.", "plain text"),
])
def test_a_bad_description_is_refused_before_any_request(value, match):
    api = RecordingApi()
    with pytest.raises(SwarmError, match=match):
        server._call(api, "swarm_dispatch", {"prompt": _prompt(), "description": value})
    assert not _posted(api, "/v1/tasks")
    with pytest.raises(SwarmError, match=match):
        server._call(api, "swarm_dispatch", {"tasks": [{"prompt": _prompt()},
                                                       {"prompt": _prompt(), "description": value}]})
    assert not _posted(api, "/v1/tasks/batch")


def test_a_description_beside_tasks_is_refused():
    api = RecordingApi()
    with pytest.raises(SwarmError, match="description"):
        server._call(api, "swarm_dispatch", {"description": DESCRIPTION, "tasks": [{"prompt": _prompt()}]})
    assert not api.sent


def test_the_client_sends_a_description_only_when_given():
    assert task_payload(prompt="p", description=DESCRIPTION)["metadata"]["description"] == DESCRIPTION
    assert "description" not in task_payload(prompt="p")["metadata"]
