"""`GET /v1/tasks/{id}/transcript` -- the agent's stdout as readable steps.

#184. The agent's transcript sat in `claude-code.stdout.log` and
`claude-transcript.json`, listed by name with "copy gsutil". This route parses
the same object `/logs?stream=agent_stdout` would serve into one step per
content block: the init record, text, thinking, tool calls paired with their
results, sub-agent steps under the call that spawned them, rate-limit
readings, and the result.

What is pinned, in the order it matters:

  1. REDACTION AFTER DECODING. A credential written with a JSON escape
     (`\\u0067hp_`) is a credential once decoded, and a regex over the raw
     line never sees it. Every string field is redacted after `json.loads`,
     and the raw record is only served on request.
  2. THE TENANT BOUNDARY: another tenant's task is the 404 a missing one gets.
  3. HONEST WINDOWS: cut on newlines only; `complete` only for the final
     record read whole; a live tail says earlier steps are not in it; a line
     longer than the window is one step with no text, and paging moves past
     it; an unparseable line is counted, never dropped silently.
  4. THE FORMAT IS SNIFFED: stream-json, the single json-format object of an
     attempt made before the switch, other NDJSON, or text (no steps).

Offline: FakeFirestore and the in-memory object reader.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
GH_TOKEN = "ghp_" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMm"

INIT = {
    "type": "system", "subtype": "init", "session_id": "s1", "uuid": "u-init",
    "cwd": "/workspace/att_1/work", "model": "claude-opus-4-1",
    "tools": ["Bash", "Read", "Task"], "mcp_servers": [],
    "permissionMode": "bypassPermissions", "apiKeySource": "none",
}
A1 = {
    "type": "assistant", "uuid": "u-a1", "session_id": "s1", "parent_tool_use_id": None,
    "message": {"id": "m1", "role": "assistant", "content": [
        {"type": "thinking", "thinking": "plan the work"},
        {"type": "text", "text": "I will run the tests."},
        {"type": "tool_use", "id": "toolu_1", "name": "Bash",
         "input": {"command": f"GH_TOKEN={GH_TOKEN} pytest -q"}},
    ]},
}
U1 = {
    "type": "user", "uuid": "u-u1", "session_id": "s1", "parent_tool_use_id": None,
    "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": "1 failed\n"},
    ]},
}
A2 = {
    "type": "assistant", "uuid": "u-a2", "parent_tool_use_id": None,
    "message": {"content": [
        {"type": "tool_use", "id": "toolu_2", "name": "Task", "input": {"prompt": "look deeper"}},
    ]},
}
SUB = {
    "type": "assistant", "uuid": "u-sub", "parent_tool_use_id": "toolu_2",
    "message": {"content": [{"type": "text", "text": "sub-agent says hi"}]},
}
U2 = {
    "type": "user", "uuid": "u-u2", "parent_tool_use_id": None,
    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": [
        {"type": "text", "text": "found it"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "iVBORw0KGgoAAAANSUhEUg=="}},
    ]}]},
}
RATE = {
    "type": "rate_limit_event", "uuid": "u-rl",
    "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.4},
}
HOOK = {"type": "system", "subtype": "hook_started", "uuid": "u-hook"}
REDACTED_THINKING = {
    "type": "assistant", "uuid": "u-red",
    "message": {"content": [{"type": "redacted_thinking", "data": "opaque"}]},
}
UNKNOWN = {"type": "something_new", "uuid": "u-odd", "x": 1}
RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "uuid": "u-res",
    "result": "# Done\n\nAll green.", "num_turns": 7, "duration_ms": 89579,
    "total_cost_usd": 0.42, "stop_reason": "end_turn", "terminal_reason": "completed",
}

STREAM_EVENTS = [INIT, A1, U1, A2, SUB, U2, RATE, HOOK, REDACTED_THINKING, UNKNOWN]


def ndjson(events, *, garbage: bool = False) -> str:
    lines = [json.dumps(e) for e in events]
    if garbage:
        lines.append("this line is not JSON")
    return "\n".join(lines) + "\n"


STREAM = ndjson(STREAM_EVENTS, garbage=True) + json.dumps(RESULT) + "\n"


def base(attempt="att_1") -> str:
    return f"tenants/eng/tasks/task_a/attempts/{attempt}"


def final_key(attempt="att_1") -> str:
    return f"{base(attempt)}/logs/agent_stdout.log"


def live_key(attempt="att_1") -> str:
    return f"{base(attempt)}/logs/live/agent_stdout.tail.log"


def _attempt(db, attempt_id="att_1", *, completed=True):
    db.collection("attempts").document(attempt_id).set({
        "attempt_id": attempt_id, "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "created_at": NOW, "started_at": NOW + timedelta(seconds=5),
        "completed_at": NOW + timedelta(minutes=2) if completed else None,
        "exit_code": 0 if completed else None, "checkpoints": [],
    })


def a_task(db, *, state="SUCCEEDED", runner_profile="claude-code", summary=None, completed=True):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state=state, runner_profile=runner_profile)
    if summary is not None:
        db.docs["tasks/task_a"]["result_summary"] = summary
    _attempt(db, completed=completed)


def get(client, query="", user="alice"):
    return client.get(f"/v1/tasks/task_a/transcript{query}", headers=auth_header(user))


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def test_a_stream_json_transcript_becomes_one_step_per_block(client, db, objects):
    a_task(db)
    objects.put(final_key(), STREAM)

    response = get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format"] == "claude-stream-json"
    assert body["stream"]["status"] == "ok"
    assert body["stream"]["source"] == "final"
    assert body["complete"] is True
    assert body["window_starts_mid_stream"] is False
    assert body["skipped_lines"] == 1, "the one line that is not JSON is counted"
    assert body["answer_in_window"] is True

    steps = body["steps"]
    assert [s["kind"] for s in steps] == [
        "init", "thinking", "text", "tool_call", "tool_result", "tool_call", "text",
        "tool_result", "rate_limit", "system", "thinking", "other", "result",
    ]
    assert [s["id"] for s in steps[:4]] == ["u-init:0", "u-a1:0", "u-a1:1", "u-a1:2"]
    assert len({s["id"] for s in steps}) == len(steps), "step ids are unique"

    init = steps[0]
    assert init["meta"] == {
        "model": "claude-opus-4-1", "permission_mode": "bypassPermissions", "tool_count": 3,
    }
    assert init["role"] == "system"
    assert init["raw"] is None, "the raw record is opt-in"

    call = steps[3]
    assert call["tool"]["id"] == "toolu_1" and call["tool"]["name"] == "Bash"
    assert "pytest -q" in call["tool"]["input"]
    assert json.loads(call["tool"]["input"].replace("********", "x")), "input is pretty JSON"

    first_result = steps[4]
    assert first_result["tool_result"] == {
        "tool_use_id": "toolu_1", "is_error": True, "content": "1 failed\n", "images": 0,
    }
    assert steps[6]["parent_tool_use_id"] == "toolu_2", "a sub-agent step names its spawner"
    assert steps[6]["text"] == "sub-agent says hi"
    second_result = steps[7]
    assert second_result["tool_result"]["content"] == "found it"
    assert second_result["tool_result"]["images"] == 1
    assert "iVBORw0KGgo" not in response.text, "image bytes are counted, never served"

    assert steps[8]["meta"]["rate_limit_info.status"] == "allowed"
    assert steps[8]["meta"]["rate_limit_info.utilization"] == 0.4
    assert steps[9]["meta"] == {"subtype": "hook_started"}
    assert steps[10]["text"] is None and steps[10]["meta"] == {"redacted": True}
    assert steps[11]["meta"] == {"raw_type": "something_new"}

    result = steps[12]
    assert result["text"] == "# Done\n\nAll green."
    assert result["meta"] == {
        "subtype": "success", "is_error": False, "num_turns": 7, "duration_ms": 89579,
        "total_cost_usd": 0.42, "stop_reason": "end_turn", "terminal_reason": "completed",
    }


def test_every_string_is_redacted_after_json_decoding(client, db, objects):
    """`\\u0067hp_` is `ghp_` once decoded. A regex over the raw line never
    matches it, so a server that redacted bytes and let the browser decode
    would hand the credential over."""
    escaped = GH_TOKEN.replace("g", "\\u0067", 1)
    line = '{"type":"assistant","uuid":"u-esc","message":{"content":[{"type":"text","text":"key ' \
        + escaped + '"}]}}'
    assert GH_TOKEN not in line, "the fixture must hide the token from a raw-text regex"
    a_task(db)
    objects.put(final_key(), line + "\n" + ndjson([A1]))

    response = get(client)
    body = response.json()
    assert "Aa1Bb2Cc3" not in response.text
    assert body["steps"][0]["text"].startswith("key ghp_")
    assert body["redaction_count"] >= 2, "the escaped token and the one in the tool input"


def test_the_raw_record_is_served_only_when_asked_and_redacted(client, db, objects):
    a_task(db)
    objects.put(final_key(), ndjson([INIT, A1]))

    body = get(client, "?include_raw=true").json()
    init, *rest = body["steps"]
    assert "/workspace/att_1/work" in init["raw"], "cwd is only in the opt-in record"
    assert "cwd" not in (init["meta"] or {})
    assert all(step["raw"] for step in rest)
    assert GH_TOKEN not in json.dumps(body)


def test_a_field_over_the_cap_is_cut_and_named(client, db, objects):
    huge = {"type": "user", "uuid": "u-big", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "y" * 20000}]}}
    a_task(db)
    objects.put(final_key(), ndjson([huge]))

    step = get(client, "?limit_bytes=65536").json()["steps"][0]
    assert len(step["tool_result"]["content"]) == 16 * 1024
    assert step["truncated_fields"] == ["tool_result.content"]


# --------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------

#: The reference task's shape: ONE `--output-format json` object, a 7,124
#: character Markdown answer. Seven turns ran and none were recorded.
ANSWER = ("# Findings\n\n" + "- a finding about the code base that matters\n" * 200)[:7123] + "\n"
REFERENCE = {
    "type": "result", "subtype": "success", "is_error": False, "result": ANSWER,
    "num_turns": 7, "duration_ms": 89579, "stop_reason": "end_turn",
    "terminal_reason": "completed", "total_cost_usd": 0.61, "session_id": "s",
    "uuid": "u-ref", "usage": {"input_tokens": 10, "output_tokens": 2000},
}


def _legacy(db, objects, *, stdout: str) -> None:
    key = f"{base()}/artifacts/claude-code.stdout.log"
    objects.put(key, stdout)
    a_task(db, summary={
        "artifacts": [{"name": "claude-code.stdout.log", "bytes": len(stdout),
                       "uri": f"gs://{BUCKET}/{key}"}],
        "artifact_bytes": len(stdout),
        "logs": {},
    })


def test_the_reference_task_is_one_result_step_from_its_artifact(client, db, objects):
    """task_73b5f4d9ca3641fbb914: no agent log objects, its stdout an artifact
    holding ONE json object. It is a single `result` step, labelled
    `claude-json` -- never an empty transcript."""
    assert len(ANSWER) == 7124
    _legacy(db, objects, stdout=json.dumps(REFERENCE) + "\n")

    body = get(client).json()
    assert body["stream"]["source"] == "artifact"
    assert body["format"] == "claude-json"
    assert [s["kind"] for s in body["steps"]] == ["result"]
    assert body["steps"][0]["text"] == ANSWER
    assert body["steps"][0]["meta"]["num_turns"] == 7
    assert body["complete"] is True
    assert body["answer_in_window"] is True


def test_a_pretty_printed_single_object_is_still_claude_json(client, db, objects):
    a_task(db)
    objects.put(final_key(), json.dumps(REFERENCE, indent=2))

    body = get(client).json()
    assert body["format"] == "claude-json"
    assert [s["kind"] for s in body["steps"]] == ["result"]


def test_other_json_lines_are_ndjson(client, db, objects):
    a_task(db)
    objects.put(final_key(), ndjson([{"event": "start"}, {"event": "stop"}]))

    body = get(client).json()
    assert body["format"] == "ndjson"
    assert [s["kind"] for s in body["steps"]] == ["other", "other"]


def test_plain_text_output_has_no_steps(client, db, objects):
    """codex `exec` prints prose. There are no steps to draw; the reader shows
    the raw `/logs` window instead, and nothing was dropped."""
    a_task(db)
    objects.put(final_key(), "Reading the repository...\nDone.\n")

    body = get(client).json()
    assert body["format"] == "text"
    assert body["steps"] is None
    assert body["skipped_lines"] == 0


def test_an_empty_stdout_is_measured_empty(client, db, objects):
    a_task(db)
    objects.put(final_key(), "")

    body = get(client).json()
    assert body["stream"]["status"] == "ok"
    assert body["steps"] == []
    assert body["format"] is None
    assert body["complete"] is True


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

def test_a_live_tail_says_earlier_steps_are_not_in_its_window(client, db, objects):
    a_task(db, state="RUNNING", completed=False)
    published = "2026-09-25T12:03:00Z"
    objects.put(live_key(), f"#swarm-tail offset=5000 size=5600 at={published}\n" + ndjson([A1, U1]))

    body = get(client).json()
    assert body["stream"]["source"] == "live"
    assert body["stream"]["tail_window"] == {
        "object_offset": 5000, "stream_size": 5600, "published_at": published,
    }
    assert body["window_starts_mid_stream"] is True
    assert body["complete"] is False
    assert body["steps"][0]["line_offset"] == 5000, "offsets are the stream's, stable across polls"
    assert body["steps"][0]["id"] == "u-a1:0"


def test_windows_cut_on_newlines_and_page_losslessly(client, db, objects):
    events = [
        {"type": "assistant", "uuid": f"u-{n:03d}",
         "message": {"content": [{"type": "text", "text": f"step number {n}"}]}}
        for n in range(200)
    ]
    a_task(db)
    objects.put(final_key(), ndjson(events))

    ids: list[str] = []
    offset = 0
    pages = 0
    while offset is not None:
        body = get(client, f"?limit_bytes=4096&offset={offset}").json()
        assert body["skipped_lines"] == 0, "no line was split by a page boundary"
        if pages == 0:
            assert body["complete"] is False, "a first page that is not the whole is not complete"
        else:
            assert body["window_starts_mid_stream"] is True
        ids.extend(step["id"] for step in body["steps"])
        offset = body["stream"]["next_offset"]
        pages += 1
        assert pages < 50, "paging never ended"
    assert pages > 1
    assert ids == [f"u-{n:03d}:0" for n in range(200)]


def test_a_line_longer_than_the_window_is_one_step_and_paging_moves_past_it(
    client, db, objects
):
    long_line = {"type": "assistant", "uuid": "u-long",
                 "message": {"content": [{"type": "text", "text": "z" * 10240}]}}
    a_task(db)
    objects.put(final_key(), ndjson([long_line, RESULT]))

    first = get(client, "?limit_bytes=4096").json()
    assert [s["kind"] for s in first["steps"]] == ["other"]
    assert first["steps"][0]["text"] is None
    assert "longer than this window" in first["stream"]["detail"]

    collected = list(first["steps"])
    offset = first["stream"]["next_offset"]
    guard = 0
    while offset is not None:
        body = get(client, f"?limit_bytes=4096&offset={offset}").json()
        collected.extend(body["steps"])
        offset = body["stream"]["next_offset"]
        guard += 1
        assert guard < 10, "paging did not move past the long line"
    assert collected[-1]["kind"] == "result"
    assert sum(1 for s in collected if s["kind"] == "other") == 1


# --------------------------------------------------------------------------
# Three answers, and the boundary
# --------------------------------------------------------------------------

def test_nothing_written_yet_is_absent_not_an_empty_transcript(client, db, objects):
    a_task(db, state="RUNNING", completed=False)

    body = get(client).json()
    assert body["stream"]["status"] == "absent"
    assert body["steps"] is None
    assert body["format"] is None


def test_a_failed_read_is_unreadable_not_absent(client, db, objects):
    a_task(db)
    objects.put(final_key(), STREAM)
    objects.fail_on(final_key())

    body = get(client).json()
    assert body["stream"]["status"] == "unreadable"
    assert body["steps"] is None
    assert body["stream"]["detail"]


def test_a_runner_with_no_agent_cli_is_not_applicable(client, db, objects):
    a_task(db, runner_profile="mock", summary={"artifacts": [], "logs": {}, "agent_streams": None})

    body = get(client).json()
    assert body["stream"]["status"] == "not_applicable"
    assert body["steps"] is None


def test_another_tenants_transcript_is_the_404_a_missing_task_gets(client, db, objects):
    a_task(db)
    objects.put(final_key(), STREAM)

    response = get(client, user="bob")
    assert response.status_code == 404, response.text
    assert "All green" not in response.text


def test_bad_parameters_are_refused(client, db, objects):
    a_task(db)
    assert get(client, "?source=guess").status_code == 422
    assert get(client, "?attempt_id=../../x").status_code == 422
    assert get(client, "?offset=-1").status_code == 422
