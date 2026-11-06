"""`swarm_task_answers` and `swarm_task_answer`'s `reply`: answers recorded at fold-time cost.

AI Studio's request on BGD's decision of 6 November 2026 (part of #2688):
about 404 questions on about 189 finished tasks are closed as moot, and each
`swarm_task_answer` reply served the whole task's questions (about 4,000
tokens). These tests hold the bridge to:

* `reply: "short"` sending `?reply=short` on the same route, and the default
  sending exactly what it sent before;
* `swarm_task_answers` sending each entry through that same route with
  `reply=short`, `via: claude-code` and this session, never stopping on a
  refusal, and answering counts and one short line per entry;
* a list over 200 refused before anything is sent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmError

SHORT = {"status": "recorded", "by": "dana@example.com", "recorded_at": "2026-11-06T04:30:00+00:00"}


class RecordingApi:
    """Speaks `request` as `SwarmClient` does; refuses the paths in `refuse`."""

    def __init__(self, refuse: dict[str, SwarmError] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.refuse = refuse or {}

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.calls.append((method, path, payload))
        for task_id, error in self.refuse.items():
            if f"/{task_id}/" in path:
                raise error
        task_id = path.split("/")[3]
        return {**SHORT, "task_id": task_id, "question_index": payload["question_index"]}


def _tool(name: str) -> dict[str, Any]:
    return next(t for t in server.TOOLS if t["name"] == name)


def test_swarm_task_answer_takes_reply_short_and_sends_it(claude_session):
    assert _tool("swarm_task_answer")["inputSchema"]["properties"]["reply"]["enum"] == ["short", "full"]
    api = RecordingApi()

    reply = json.loads(server._call(api, "swarm_task_answer", {
        "task_id": "task_a", "question_index": 2, "text": "moot", "reply": "short"}))

    assert api.calls == [("POST", "/v1/tasks/task_a/answers?reply=short", {
        "question_index": 2, "choice": None, "text": "moot",
        "via": "claude-code", "session": claude_session,
    })]
    assert reply["status"] == "recorded" and "questions" not in reply


def test_swarm_task_answer_by_default_sends_what_it_sent_before(claude_session):
    api = RecordingApi()

    server._call(api, "swarm_task_answer", {"task_id": "task_a", "question_index": 0, "choice": "keep"})
    server._call(api, "swarm_task_answer", {
        "task_id": "task_a", "question_index": 0, "choice": "keep", "reply": "full"})

    assert [path for _, path, _ in api.calls] == ["/v1/tasks/task_a/answers"] * 2


def test_an_unknown_reply_is_refused_and_nothing_is_sent():
    api = RecordingApi()

    with pytest.raises(SwarmError):
        server._call(api, "swarm_task_answer", {
            "task_id": "task_a", "question_index": 0, "choice": "keep", "reply": "tiny"})

    assert api.calls == []


def test_the_bulk_tool_records_three_and_reports_a_refused_one_without_stopping(claude_session):
    refusal = SwarmError("403 forbidden: only the submitter or an admin may answer",
                         status=403, code="forbidden")
    api = RecordingApi(refuse={"task_b": refusal})
    entries = [
        {"task_id": "task_a", "question_index": 0, "choice": "keep"},
        {"task_id": "task_b", "question_index": 1, "text": "moot"},
        {"task_id": "task_c", "question_index": 0, "text": "moot: the lane finished"},
        {"task_id": "task_d", "question_index": 3, "choice": "drop", "text": "and why"},
    ]

    reply = json.loads(server._call(api, "swarm_task_answers", {"entries": entries}))

    # Every entry was sent, the refused one included, each through the single route, short.
    assert [path for _, path, _ in api.calls] == [
        f"/v1/tasks/{e['task_id']}/answers?reply=short" for e in entries]
    assert all(p["via"] == "claude-code" and p["session"] == claude_session for _, _, p in api.calls)
    assert api.calls[2][2] == {"question_index": 0, "choice": None, "text": "moot: the lane finished",
                               "via": "claude-code", "session": claude_session}
    assert (reply["entries"], reply["recorded"], reply["refused"]) == (4, 3, 1)
    assert reply["results"] == [
        {"task_id": "task_a", "question_index": 0, "status": "recorded"},
        {"task_id": "task_b", "question_index": 1, "refused": "forbidden"},
        {"task_id": "task_c", "question_index": 0, "status": "recorded"},
        {"task_id": "task_d", "question_index": 3, "status": "recorded"},
    ]
    assert reply["claude_session"] == claude_session


def test_a_malformed_entry_is_its_own_line_and_nothing_is_sent_for_it():
    api = RecordingApi()
    entries = [
        {"task_id": "task_a", "question_index": 0},  # no answer
        {"task_id": "task_a", "question_index": 1, "choice": "keep", "via": "console"},
        {"task_id": "task_a/../admin", "question_index": 0, "choice": "keep"},
        {"task_id": "task_c", "question_index": 0, "choice": "keep"},
    ]

    reply = json.loads(server._call(api, "swarm_task_answers", {"entries": entries}))

    assert [path for _, path, _ in api.calls] == ["/v1/tasks/task_c/answers?reply=short"]
    assert [r.get("refused") for r in reply["results"]] == ["invalid_entry"] * 3 + [None]
    assert (reply["recorded"], reply["refused"]) == (1, 3)


def test_over_200_entries_are_refused_before_anything_is_sent():
    api = RecordingApi()
    entries = [{"task_id": f"task_{i}", "question_index": 0, "text": "moot"} for i in range(201)]

    with pytest.raises(SwarmError, match="200"):
        server._call(api, "swarm_task_answers", {"entries": entries})

    assert api.calls == []
    assert _tool("swarm_task_answers")["inputSchema"]["properties"]["entries"]["maxItems"] == 200


def test_the_bulk_tool_takes_no_via_or_session_from_the_caller():
    api = RecordingApi()

    for extra in ({"via": "console"}, {"session": "someone-elses-session"}):
        with pytest.raises(SwarmError):
            server._call(api, "swarm_task_answers", {
                "entries": [{"task_id": "task_a", "question_index": 0, "text": "moot"}], **extra})

    assert api.calls == []


def test_the_readme_and_the_sc_skill_name_the_bulk_tool():
    from pathlib import Path

    plugin = Path(__file__).resolve().parents[3] / "plugin"
    for doc in (plugin / "README.md", plugin / "skills" / "sc" / "SKILL.md"):
        assert "swarm_task_answers" in doc.read_text(encoding="utf-8"), doc
