"""`swarm_task_answer`: an answer given in Claude Code is recorded on the task.

Owner decision 2026-10-19 ("Plugin records it, marked as from Claude Code";
lane ANSWER-SYNC, part of #1644). A question an agent asked, answered by the
person in the orchestrating Claude Code session, stayed unanswered in the
console: the bridge had no tool for `POST /v1/tasks/{id}/answers`. These
tests hold the new tool to:

* posting `via: "claude-code"` and the session id the bridge tags its
  submissions with (`CLAUDE_CODE_SESSION_ID`, read by `swarm_mcp.session`);
* answering with the questions as the API serves them, and surfacing the
  API's refusal -- a 403 for someone who may not answer -- word for word;
* a description that says, in so many words, that it carries only the
  person's own pick, never an answer the agent chose itself.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmError

TASK = "task_152bcd31dd444aba9916"

SERVED = {
    "task_id": TASK,
    "status": "ok",
    "count": 1,
    "may_answer": True,
    "questions": [{
        "question": "Which logo?",
        "options": [{"label": "compass", "description": "the compass"}],
        "recommended": "compass",
        "context": None,
        "answer": {"question_index": 0, "choice": "compass", "text": None, "via": "claude-code"},
    }],
}


class RecordingApi:
    """Speaks `request` as `SwarmClient` does, recording every call."""

    def __init__(self, *, refuse: SwarmError | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.refuse = refuse

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.calls.append((method, path, payload))
        if self.refuse is not None:
            raise self.refuse
        return SERVED


def _tool() -> dict[str, Any]:
    return next(t for t in server.TOOLS if t["name"] == "swarm_task_answer")


def test_the_tool_is_served_with_the_arguments_it_takes():
    schema = _tool()["inputSchema"]
    assert set(schema["properties"]) == {"task_id", "question_index", "choice", "text"}
    assert set(schema["required"]) == {"task_id", "question_index"}
    # Where it was answered is the bridge's to say, never the caller's.
    assert "via" not in schema["properties"] and "session" not in schema["properties"]


def test_it_posts_via_claude_code_with_the_session_from_the_environment(claude_session):
    api = RecordingApi()

    reply = json.loads(server._call(api, "swarm_task_answer", {
        "task_id": TASK, "question_index": 0, "choice": "compass", "text": "the round one"}))

    assert api.calls == [("POST", f"/v1/tasks/{TASK}/answers", {
        "question_index": 0,
        "choice": "compass",
        "text": "the round one",
        "via": "claude-code",
        "session": claude_session,
    })]
    # The answer as the API served it, unchanged.
    assert reply["questions"] == SERVED["questions"]
    assert reply["claude_session"] == claude_session


def test_without_a_session_it_still_records_the_answer_and_says_why_none_was_sent():
    api = RecordingApi()

    reply = json.loads(server._call(api, "swarm_task_answer", {
        "task_id": TASK, "question_index": 0, "choice": "compass"}))

    (_, _, payload), = api.calls
    assert payload["via"] == "claude-code"
    assert payload["session"] is None
    assert reply["claude_session"] is None
    assert reply["claude_session_note"]


def test_a_session_the_api_would_not_take_is_not_sent(monkeypatch):
    """`session.current()` accepts ids the API refuses (`.` and `:`); the
    answer is still the person's, so it is recorded without the session."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ce05454f.resumed:2")
    api = RecordingApi()

    reply = json.loads(server._call(api, "swarm_task_answer", {
        "task_id": TASK, "question_index": 0, "text": "the round one"}))

    (_, _, payload), = api.calls
    assert payload["session"] is None
    assert "ce05454f.resumed:2" in reply["claude_session_note"]


def test_a_403_is_surfaced_verbatim(claude_session):
    message = ("403 forbidden: only dana@saga.xyz, who launched this task, or an admin "
               "may answer its questions")
    api = RecordingApi(refuse=SwarmError(message, status=403, code="forbidden"))

    with pytest.raises(SwarmError) as caught:
        server._call(api, "swarm_task_answer", {
            "task_id": TASK, "question_index": 0, "choice": "compass"})

    assert server._tool_error_text(caught.value) == message
    assert len(api.calls) == 1


@pytest.mark.parametrize("args", [
    {"question_index": 0, "choice": "compass"},
    {"task_id": "", "question_index": 0, "choice": "compass"},
    {"task_id": TASK, "choice": "compass"},
    {"task_id": TASK, "question_index": "0", "choice": "compass"},
    {"task_id": TASK, "question_index": True, "choice": "compass"},
    {"task_id": TASK, "question_index": 0},
    {"task_id": TASK, "question_index": 0, "choice": 3},
    {"task_id": f"{TASK}/../../v1/admin", "question_index": 0, "choice": "compass"},
])
def test_a_malformed_call_is_refused_and_nothing_is_sent(args):
    api = RecordingApi()

    with pytest.raises(SwarmError):
        server._call(api, "swarm_task_answer", args)

    assert api.calls == []


def test_it_takes_no_via_or_session_from_the_caller():
    api = RecordingApi()

    for extra in ({"via": "console"}, {"session": "someone-elses-session"}):
        with pytest.raises(SwarmError):
            server._call(api, "swarm_task_answer", {
                "task_id": TASK, "question_index": 0, "choice": "compass", **extra})

    assert api.calls == []


def test_the_description_says_it_carries_only_the_persons_own_pick():
    text = " ".join(_tool()["description"].split())

    assert ("only the person's own pick, verbatim, from a question this session asked them; "
            "never an answer the agent chose itself") in text


def test_the_sc_skill_and_command_say_to_record_an_answer_given_here():
    from pathlib import Path

    plugin = Path(__file__).resolve().parents[3] / "plugin"
    for doc in (plugin / "skills" / "sc" / "SKILL.md", plugin / "commands" / "sc.md"):
        text = " ".join(doc.read_text(encoding="utf-8").split())
        assert "swarm_task_answer" in text, doc
        assert "never an answer" in text, doc
