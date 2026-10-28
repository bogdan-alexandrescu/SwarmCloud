"""A running codex attempt's live tail reads as steps (CODEX-TRANSCRIPT, part of #2459).

The codex profile runs `codex exec --json` (CODEX-JSON, PR 2515). The worker
renders the capture readable once the run ends, but while it RUNS the Logs
tab's live tail is the raw event stream, and the transcript reader sniffed it
as `ndjson` and drew every event as an empty `other` row. BGD, 2026-10-27
(PR 2515 question 2): teach swarm-api codex's events.

Pinned here:

  1. A window of codex events sniffs as `codex-exec-json`, the name the
     worker's finished transcript carries; a Claude window still sniffs as
     `claude-stream-json`, and other JSON lines as `ndjson`.
  2. Each event is a step the drawer already draws, in order, and an
     `item.updated` / `item.completed` REPLACES the step its `item.id`
     started -- a todo list ticking is one row, not three.
  3. Redaction is the Claude path's: a credential in a command and in its
     output is masked after decoding, and in an MCP call's arguments by
     their structure.
  4. A window cut mid-line counts the fragment in `skipped_lines`.
  5. (CODEX-ROWS, part of #2459; BGD, 2026-10-28, proposal B) a
     `turn.completed` is a `usage` row whose figures are in meta -- an absent
     figure absent, never 0 -- and numbered only when the window can count
     its turns; `turn.failed`, a top-level `error` and an `error` item are
     `error` rows whose text is the message and whose `meta.subtype` names
     the shape. A Claude window's rows are unchanged.

MUTATIONS: drop `CODEX_EVENT_TYPES` from the sniff (the format reads
`ndjson`); append every item event as its own step (the replacement test
counts three todo rows); serve a command's input without `scrubber.text`
(the token is in the response).

Offline: FakeFirestore and the in-memory object reader; the stream is
codex-cli 0.154.0's shapes, the same as tests/unit/worker/test_codex_json.py's.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from swarm_api.agent_streams import TRUNCATION_MARK
from swarm_api.transcript import parse_window

from .test_transcript_route import (
    A1, INIT, RESULT, a_task, get, live_key, ndjson,
)

SESSION = "01a17ac4-e427-7213-b3b6-75d33a4d17b7"


def _gh_token() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "ghp_" + secrets.token_hex(18)


def _usage(inp: int, cached: int, out: int) -> dict[str, int]:
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}


def _command(item_id: str, command: str, *, output: str = "", exit_code: int | None = None,
             status: str = "in_progress") -> dict[str, Any]:
    return {"id": item_id, "type": "command_execution", "command": command,
            "aggregated_output": output, "exit_code": exit_code, "status": status}


#: One `codex exec --json` run in codex-cli 0.154.0's shapes, as
#: test_codex_json.RECORDED has it, plus a failing command, a web search and
#: a turn that failed.
RECORDED: list[dict[str, Any]] = [
    {"type": "thread.started", "thread_id": SESSION},
    {"type": "turn.started"},
    {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning",
                                        "text": "**Reading the repository layout**"}},
    {"type": "item.started", "item": _command("item_1", "bash -lc ls")},
    {"type": "item.completed", "item": _command("item_1", "bash -lc ls", output="README.md\napps\n",
                                                exit_code=0, status="completed")},
    {"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "status": "completed",
                                        "changes": [{"path": "README.md", "kind": "update"}]}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "mcp_tool_call", "server": "swarm-git",
                                        "tool": "git_fetch", "status": "completed"}},
    {"type": "item.started", "item": {"id": "item_4", "type": "todo_list",
                                      "items": [{"text": "edit README", "completed": False}]}},
    {"type": "item.updated", "item": {"id": "item_4", "type": "todo_list",
                                      "items": [{"text": "edit README", "completed": True}]}},
    {"type": "item.completed", "item": {"id": "item_4", "type": "todo_list",
                                        "items": [{"text": "edit README", "completed": True}]}},
    {"type": "item.completed", "item": {"id": "item_5", "type": "web_search", "query": "codex exec json"}},
    {"type": "item.completed", "item": _command("item_6", "bash -lc false", exit_code=1,
                                                status="failed")},
    {"type": "item.completed", "item": {"id": "item_7", "type": "agent_message",
                                        "text": "Updated the README's first paragraph."}},
    {"type": "turn.completed", "usage": _usage(12_000, 9_000, 800)},
    {"type": "turn.failed", "error": {"message": "usage limit reached"}},
]


def test_a_codex_stream_sniffs_as_codex_exec_json_and_maps_to_steps_in_order(client, db, objects):
    a_task(db, state="RUNNING", runner_profile="codex", completed=False)
    objects.put(live_key(), "#swarm-tail offset=0 size=4000 at=2026-10-27T01:00:00Z\n" + ndjson(RECORDED))

    response = get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format"] == "codex-exec-json"
    assert body["skipped_lines"] == 0

    steps = body["steps"]
    assert [(s["kind"], (s["meta"] or {}).get("subtype")) for s in steps] == [
        ("init", None),
        ("system", "turn_started"),
        ("error", "error"),
        ("thinking", None),
        ("tool_call", None), ("tool_result", None),
        ("system", "file_change"),
        ("tool_call", None), ("tool_result", None),
        ("system", "todo_list"),
        ("system", "web_search"),
        ("tool_call", None), ("tool_result", None),
        ("text", None),
        ("usage", None),
        ("error", "turn_failed"),
    ]
    assert len({s["id"] for s in steps}) == len(steps), "step ids are unique"
    assert not any(s["kind"] == "other" for s in steps), "no codex event is an empty row"

    assert steps[0]["meta"] == {"thread_id": SESSION}
    # MUTATION: serve codex's retry notice whole as the text (no retry meta)
    # and the drawer prints the counter twice, once before the message.
    assert steps[2]["text"] == "stream disconnected before completion"
    assert steps[2]["meta"] == {"subtype": "error", "retry_attempt": 1, "retry_limit": 5}
    assert steps[3]["text"] == "**Reading the repository layout**"

    call, result = steps[4], steps[5]
    assert call["tool"] == {"id": "item_1", "name": "command", "input": "bash -lc ls"}
    assert result["tool_result"] == {
        "tool_use_id": "item_1", "is_error": False, "content": "README.md\napps\nexit 0", "images": 0,
    }
    assert result["meta"] == {"exit_code": 0, "status": "completed"}

    assert steps[6]["text"] == "files (completed): update README.md"
    assert steps[7]["tool"]["name"] == "swarm-git.git_fetch"
    assert steps[8]["tool_result"]["tool_use_id"] == steps[7]["tool"]["id"]
    assert steps[8]["tool_result"]["is_error"] is False
    assert steps[9]["text"] == "plan:\n    [x] edit README"
    assert steps[10]["text"] == "web search: codex exec json"
    assert steps[12]["tool_result"]["is_error"] is True
    assert steps[12]["tool_result"]["content"] == "exit 1"

    message = steps[13]
    assert message["role"] == "assistant"
    assert message["text"] == "Updated the README's first paragraph."

    usage = steps[14]
    assert usage["text"] == "turn completed: 12,000 input tokens (9,000 cached), 800 output tokens"
    # MUTATION: map `turn.completed` back onto a `system` row (the kind reads
    # `system`, and meta carries a `subtype` and no `turn`).
    assert usage["meta"] == {
        "input_tokens": 12_000, "cached_input_tokens": 9_000, "output_tokens": 800, "turn": 1,
    }
    # MUTATION: prefix the message with `turn failed: ` again (the drawer's
    # label already says it).
    assert steps[15]["text"] == "usage limit reached"


def test_an_item_update_replaces_the_step_its_id_started():
    """A command still running is a call with no result; its completion, and
    a todo list's every tick, replace that row in place."""
    events = [
        {"type": "item.started", "item": _command("item_1", "pytest -q")},
        {"type": "item.started", "item": {"id": "item_2", "type": "todo_list",
                                          "items": [{"text": "a", "completed": False},
                                                    {"text": "b", "completed": False}]}},
        {"type": "item.updated", "item": {"id": "item_2", "type": "todo_list",
                                          "items": [{"text": "a", "completed": True},
                                                    {"text": "b", "completed": False}]}},
        {"type": "item.completed", "item": _command("item_1", "pytest -q", output="1 passed\n",
                                                    exit_code=0, status="completed")},
    ]
    data = ndjson(events).encode()

    running = parse_window(ndjson(events[:1]).encode(), base_offset=0, whole_object=False)
    assert running.format == "codex-exec-json"
    assert [s["kind"] for s in running.steps] == ["tool_call"], "no result while it runs"

    parsed = parse_window(data, base_offset=0, whole_object=False, include_raw=True)
    assert [s["kind"] for s in parsed.steps] == ["tool_call", "tool_result", "system"]
    call, result, todo = parsed.steps
    assert call["line_offset"] == 0 and call["id"] == "L0:0", "the row stays where it started"
    assert result["tool_result"]["content"] == "1 passed\nexit 0"
    assert todo["text"] == "plan:\n    [x] a\n    [ ] b"
    assert json.loads(todo["raw"])["type"] == "item.updated", "the record is the latest event"
    assert json.loads(call["raw"])["type"] == "item.completed"


def test_a_credential_in_a_command_and_its_output_is_masked(client, db, objects):
    token = _gh_token()
    escaped = token.replace("g", "\\u0067", 1)
    command = f"GH_TOKEN={token} gh pr list"
    events = [
        {"type": "item.started", "item": _command("item_1", command)},
        {"type": "item.completed", "item": _command("item_1", command, output=f"token: {token}\n",
                                                    exit_code=0, status="completed")},
        {"type": "item.completed", "item": {"id": "item_2", "type": "mcp_tool_call",
                                            "server": "s", "tool": "t", "status": "completed",
                                            "arguments": {"password": "hunter2-" + secrets.token_hex(4)}}},
    ]
    stream = ndjson(events).replace(token, escaped)
    assert token not in stream, "the fixture hides the token from a raw-text regex"
    a_task(db, state="RUNNING", runner_profile="codex", completed=False)
    objects.put(live_key(), "#swarm-tail offset=0 size=900 at=2026-10-27T01:00:00Z\n" + stream)

    response = get(client, "?include_raw=true")
    body = response.json()
    assert body["format"] == "codex-exec-json"
    assert token[4:] not in response.text
    assert "hunter2-" not in response.text, "an argument under a credential's name is masked whole"
    call, result, mcp, _ = body["steps"]
    assert call["tool"]["input"].startswith("GH_TOKEN=")
    assert "gh pr list" in call["tool"]["input"]
    assert result["tool_result"]["content"].startswith("token: ")
    assert "********" in mcp["tool"]["input"]
    assert body["redaction_count"] >= 3


def test_a_claude_window_still_sniffs_as_before():
    claude = parse_window(ndjson([INIT, A1, RESULT]).encode(), base_offset=0, whole_object=False)
    assert claude.format == "claude-stream-json"
    assert [s["kind"] for s in claude.steps] == ["init", "thinking", "text", "tool_call", "result"]
    other = parse_window(ndjson([{"event": "start"}]).encode(), base_offset=0, whole_object=False)
    assert other.format == "ndjson"


def test_a_window_cut_mid_line_counts_the_fragment_as_skipped():
    whole = ndjson(RECORDED[:4]).encode()
    cut_at = whole.index(b"\n") // 2
    parsed = parse_window(whole[cut_at:], base_offset=cut_at, whole_object=False)
    assert parsed.format == "codex-exec-json"
    assert parsed.skipped_lines == 1
    assert [s["kind"] for s in parsed.steps] == ["system", "error", "thinking"]


# --- CODEX-ROWS (part of #2459): usage and errors are kinds of their own -----


def _one(events: list[dict[str, Any]], *, base_offset: int = 0) -> list[dict[str, Any]]:
    parsed = parse_window(ndjson(events).encode(), base_offset=base_offset, whole_object=False)
    assert parsed.format == "codex-exec-json"
    return parsed.steps


def test_a_turn_completed_is_a_usage_row_with_its_figures_and_turn_in_meta():
    steps = _one([
        {"type": "thread.started", "thread_id": SESSION},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": _usage(48_210, 41_900, 2_316)},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": _usage(1, 0, 2)},
    ])
    usage = [s for s in steps if s["kind"] == "usage"]
    # MUTATION: emit `turn.completed` as `kind="system"` again: no `usage` rows.
    assert len(usage) == 2
    # MUTATION: drop a figure from meta (the reader has only the worded line).
    assert usage[0]["meta"] == {
        "input_tokens": 48_210, "cached_input_tokens": 41_900, "output_tokens": 2_316, "turn": 1,
    }
    # MUTATION: never increment the turn counter (the second turn reads 1).
    assert usage[1]["meta"]["turn"] == 2
    # MUTATION: blank the text (a reader not knowing `usage` draws nothing).
    assert usage[0]["text"] == "turn completed: 48,210 input tokens (41,900 cached), 2,316 output tokens"
    assert usage[0]["role"] == "system"


def test_an_absent_usage_figure_is_absent_from_meta_never_zero():
    steps = _one([
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": {"input_tokens": 500, "output_tokens": 7}},
        {"type": "turn.started"},
        {"type": "turn.completed"},
    ])
    first, second = (s for s in steps if s["kind"] == "usage")
    # MUTATION: write every figure, `None` or `or 0` for an absent one (meta
    # gains `cached_input_tokens`).
    assert first["meta"] == {"input_tokens": 500, "output_tokens": 7, "turn": 1}
    assert "cached_input_tokens" not in first["meta"]
    assert second["meta"] == {"turn": 2}


def test_a_turn_is_numbered_only_when_the_window_can_count_its_turns():
    events = [
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": _usage(10, 0, 1)},
    ]
    # A page that starts mid-stream cannot know how many turns came before it.
    # MUTATION: count turns whatever the window's offset (meta gains `turn: 1`).
    (later,) = (s for s in _one(events, base_offset=4096) if s["kind"] == "usage")
    assert later["meta"] == {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}

    # A capture notice dropped turns nobody can count: after it, no number.
    data = (ndjson(events[:1]).encode() + TRUNCATION_MARK + b" 1024 bytes\n"
            + ndjson(events).encode())
    parsed = parse_window(data, base_offset=0, whole_object=False)
    (after_cut,) = (s for s in parsed.steps if s["kind"] == "usage")
    # MUTATION: keep counting across a capture notice (meta gains `turn`).
    assert "turn" not in after_cut["meta"]


def test_every_codex_error_shape_is_an_error_row_named_by_its_subtype():
    steps = _one([
        {"type": "turn.started"},
        {"type": "error", "message": "Reconnecting... 2/5 (stream disconnected before completion)"},
        {"type": "error", "message": "model overloaded"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                            "message": "command timed out"}},
        {"type": "turn.failed", "error": {"message": "usage limit reached"}},
        {"type": "turn.failed"},
    ])
    rows = [(s["kind"], s["text"], s["meta"]) for s in steps[1:]]
    # MUTATION: map any one shape back to `system` (its kind reads `system`),
    # or name an `error` item `turn_failed` (its subtype is wrong).
    assert rows == [
        ("error", "stream disconnected before completion",
         {"subtype": "error", "retry_attempt": 2, "retry_limit": 5}),
        ("error", "model overloaded", {"subtype": "error"}),
        ("error", "command timed out", {"subtype": "error"}),
        ("error", "usage limit reached", {"subtype": "turn_failed"}),
        ("error", "(no message)", {"subtype": "turn_failed"}),
    ]
    assert all(s["role"] == "system" for s in steps[1:])


def test_a_claude_window_keeps_its_rows_unchanged():
    """No Claude event becomes a `usage` or an `error` row: those kinds are
    codex's, and a Claude `result` still carries its usage in its own row."""
    stray = {"type": "error", "message": "not a codex window"}
    claude = parse_window(ndjson([INIT, A1, RESULT, stray]).encode(), base_offset=0,
                          whole_object=False)
    assert claude.format == "claude-stream-json"
    # MUTATION: route a Claude window's events through `map_codex_event` (the
    # stray `error` becomes an `error` row, and the kinds change).
    assert [s["kind"] for s in claude.steps] == [
        "init", "thinking", "text", "tool_call", "result", "other",
    ]
    assert claude.steps[-1]["meta"] == {"raw_type": "error"}
