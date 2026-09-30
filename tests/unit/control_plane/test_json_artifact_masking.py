"""A JSON artifact is still JSON after read-time masking (#327).

The #323 measurement read `claude-transcript.json` for 47 tasks through the
API and 10 of the served files no longer parsed. The worker writes that file
with `json.dumps(indent=2)`, so no line of it is a JSON document and
`redact_lines` ran the text rules over raw JSON text. The chains that took
#327 measured four ways that breaks a file, and one way it LEAKS:

  1. a value closed by an escaped quote (`echo \\"PASSWORD=<v>\\"`): the
     key/value rule takes `<v>\\` and leaves a bare quote;
  2. a number, `null`, a list or an object under a credential's name:
     served as a bare `********`, or with its `[` gone;
  3. a private key in one string, its newlines written `\\n`: the block mask
     ran across the closing quote, with or without an END marker;
  4. a value the task named as secret, holding a quote, a newline or a
     non-ASCII character: JSON text writes it escaped, the text path's
     substring search never matched it, and it was SERVED IN CLEAR.

Every one is checked through BOTH routes that serve an artifact's bytes --
`/v1/tasks/{id}/artifacts/content` (`inspect.InspectionService.read_artifact`,
the route #327 was measured on) and `/v1/tasks/{id}/artifacts/raw`
(`agent_output`) -- asserting the served text parses AND the secret is gone.

Every credential-shaped value is assembled at import time with `_shape`, as
`test_log_redaction.py` does, so no fragment on disk matches the repository's
secret scan (`.github/workflows/security.yml`).

Offline: FakeFirestore and the in-memory object reader. No credentials.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api.redaction import MASK, redact_json, redact_lines

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"


def _shape(*parts: str) -> str:
    """Join fragments into a credential-shaped value at import time (see the module docstring)."""
    return "".join(parts)


def _masking():
    """The module under test, imported where it is used: before the fix it
    does not exist, and the route tests must still fail on what they SERVE."""
    from swarm_api import json_masking

    return json_masking


# --------------------------------------------------------------------------
# Synthetic credentials: random strings in the right alphabet, none real
# --------------------------------------------------------------------------

#: Closed by an escaped quote in JSON text (breaker 1).
V_QUOTED = _shape("Qv8n", "Rt2wLz", "5Kp7xB")
V_EXPORTED = _shape("Mc4d", "Hs9qTe", "3Wy6uJ")
#: Under credential names as a list and as an object (breaker 2).
V_LISTED = _shape("Hj3m", "Xq9Tb6", "Wn4zAa")
V_NESTED = _shape("Fd2k", "Ps8Yc5", "Ru7mGg")
#: A PIN: a number under a name the credential word ends.
PIN = int(_shape("7351", "9264"))
PIN_TEXT = str(PIN)

PEM_BEGIN = _shape("-----", "BEGIN", " RSA PRIVATE KEY", "-----")
PEM_END = _shape("-----", "END", " RSA PRIVATE KEY", "-----")
KEY_BODY = [
    _shape("MIIE", "pAIBAAKCAQEA", "u3Xk9Lq2Zr7Wm4Pd8Tn1Vb6Hc5Jf0Gy"),
    _shape("q9Rt", "2Lm7Xk4Pz8Wc", "3Nb6Vd1Hf5Jg0KyT7sQ2wE4rU8iO1pA"),
]

#: A value only the task's metadata names as secret, holding a quote, a
#: newline and a non-ASCII character: JSON text writes all three escaped
#: (breaker 4, the leak).
LITERAL = _shape("amber", '"', "quill-", "é", "\n", "vortex-", "3371")
LITERAL_MARKS = ("quill-", "vortex-3371")


# --------------------------------------------------------------------------
# Seeding and reading (the shape of test_masked_everywhere_pr229_followup.py)
# --------------------------------------------------------------------------

def _artifact_key(name: str) -> str:
    return f"tenants/eng/tasks/task_a/attempts/att_1/artifacts/{name}"


def _a_finished_task(
    db, objects, *, files: dict[str, str], metadata: dict[str, Any] | None = None
) -> None:
    """A terminal task whose manifest and bucket agree."""
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    doc["metadata"] = dict(metadata or {})
    entries = []
    for name, body in files.items():
        raw_bytes = body.encode("utf-8")
        key = _artifact_key(name)
        objects.put(key, raw_bytes)
        entries.append({"name": name, "bytes": len(raw_bytes), "uri": f"gs://{BUCKET}/{key}"})
    doc["result_summary"] = {
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
    }
    doc["completed_at"] = datetime.now(timezone.utc)


def _content(client, name: str, **params: Any) -> dict[str, Any]:
    response = client.get(
        "/v1/tasks/task_a/artifacts/content",
        params={"name": name, **params},
        headers=auth_header("alice"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok", body
    return body


def _raw(client, name: str) -> str:
    response = client.get(
        "/v1/tasks/task_a/artifacts/raw",
        params={"name": name},
        headers=auth_header("alice"),
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-swarm-redaction"] == "applied"
    return response.text


def _both_routes(client, name: str) -> dict[str, str]:
    """The whole artifact as each route serves it."""
    body = _content(client, name)
    assert body["truncated"] is False, "the fixture must fit one content window"
    return {"content": body["content"], "raw": _raw(client, name)}


def _paged(client, name: str, *, limit_bytes: int = 4096) -> tuple[str, int]:
    """Every content window in order, joined, as `swarm artifact` joins them; and how many."""
    pieces: list[str] = []
    offset = 0
    while True:
        body = _content(client, name, offset=offset, limit_bytes=limit_bytes)
        pieces.append(body["content"])
        following = body.get("next_offset")
        if not body["truncated"] or not isinstance(following, int) or following <= offset:
            return "".join(pieces), len(pieces)
        offset = following


def _transcript(*events: dict[str, Any]) -> str:
    """A transcript as the worker writes it: pretty-printed, ASCII-escaped."""
    return json.dumps({"type": "transcript", "events": list(events)}, indent=2) + "\n"


# --------------------------------------------------------------------------
# Breaker 1: a value closed by an escaped quote
# --------------------------------------------------------------------------

def _escaped_quote_transcript() -> str:
    return _transcript(
        {"type": "tool_use", "input": {"command": 'echo "PASSWORD=' + V_QUOTED + '"'}},
        {"type": "tool_use", "input": {"command": 'export DB_PASSWORD="' + V_EXPORTED + '"'}},
        {"type": "text", "text": "done"},
    )


def test_a_value_closed_by_an_escaped_quote_is_masked_and_the_file_parses(client, db, objects):
    stored = _escaped_quote_transcript()
    assert '\\"PASSWORD=' + V_QUOTED + '\\"' in stored, "control: the stored text escapes the quotes"
    _a_finished_task(db, objects, files={"claude-transcript.json": stored})

    for route, served in _both_routes(client, "claude-transcript.json").items():
        parsed = json.loads(served)  # the defect: this raised
        assert V_QUOTED not in served and V_EXPORTED not in served, (route, served)
        commands = [e["input"]["command"] for e in parsed["events"][:2]]
        assert commands == ['echo "PASSWORD=' + MASK + '"', 'export DB_PASSWORD="' + MASK + '"'], route
        assert parsed["events"][2] == {"type": "text", "text": "done"}, route


# --------------------------------------------------------------------------
# Breaker 2: null, a number, a list, an object under a credential's name
# --------------------------------------------------------------------------

def _typed_values_transcript() -> str:
    return _transcript({
        "type": "config",
        "api_key": None,
        "token": True,
        "password": PIN,
        "secrets": [V_LISTED],
        "credential": {"value": V_NESTED},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })


def test_a_non_string_under_a_credentials_name_is_served_as_json(client, db, objects):
    _a_finished_task(db, objects, files={"claude-transcript.json": _typed_values_transcript()})

    for route, served in _both_routes(client, "claude-transcript.json").items():
        event = json.loads(served)["events"][0]  # the defect: this raised
        assert V_LISTED not in served and V_NESTED not in served and PIN_TEXT not in served, route
        # A number, a list and an object are ONE mask each, as the JSON string.
        assert event["password"] == MASK, route
        assert event["secrets"] == MASK, route
        assert event["credential"] == MASK, route
        # null and a boolean cannot be a credential: served as they are.
        assert event["api_key"] is None, route
        assert event["token"] is True, route
        # Counts under a wider name are counts.
        assert event["usage"] == {"input_tokens": 10, "output_tokens": 5}, route


def test_the_content_route_counts_each_masked_value_once(client, db, objects):
    _a_finished_task(db, objects, files={"claude-transcript.json": _typed_values_transcript()})

    body = _content(client, "claude-transcript.json")
    assert body["redacted"] is True
    assert body["redaction_count"] == 3, body["content"]


# --------------------------------------------------------------------------
# Breaker 3: a private key in one string, newlines escaped
# --------------------------------------------------------------------------

@pytest.mark.parametrize("with_end", [True, False], ids=["with-end", "without-end"])
def test_a_private_key_in_one_string_is_masked_inside_that_string(client, db, objects, with_end):
    key = PEM_BEGIN + "\n" + "\n".join(KEY_BODY) + "\n" + (PEM_END + "\n" if with_end else "")
    stored = _transcript(
        {"type": "tool_result", "content": key},
        {"type": "text", "text": "after the key"},
    )
    assert "\\n" + KEY_BODY[0] in stored, "control: the newlines are escaped in the stored text"
    _a_finished_task(db, objects, files={"claude-transcript.json": stored})

    for route, served in _both_routes(client, "claude-transcript.json").items():
        parsed = json.loads(served)  # the defect: this raised
        for line in KEY_BODY:
            assert line not in served, (route, line)
        assert parsed["events"][0]["content"].startswith(PEM_BEGIN), route
        assert MASK in parsed["events"][0]["content"], route
        assert parsed["events"][1] == {"type": "text", "text": "after the key"}, route


# --------------------------------------------------------------------------
# Breaker 4: a learned literal written with JSON escapes (the leak)
# --------------------------------------------------------------------------

def test_a_learned_literal_written_escaped_is_masked(client, db, objects):
    stored = _transcript({"type": "text", "text": "the agent echoed " + LITERAL + " back"})
    # Control: in the stored JSON text the literal does not appear as itself,
    # which is why a substring search over that text never found it.
    assert LITERAL not in stored
    assert all(mark in stored for mark in LITERAL_MARKS)
    _a_finished_task(
        db, objects,
        files={"claude-transcript.json": stored},
        metadata={"db_password": LITERAL},
    )

    for route, served in _both_routes(client, "claude-transcript.json").items():
        parsed = json.loads(served)
        for mark in LITERAL_MARKS:
            assert mark not in served, (route, mark, served)
        assert parsed["events"][0]["text"] == "the agent echoed " + MASK + " back", route


# --------------------------------------------------------------------------
# JSONL: an agent's stream-json stdout
# --------------------------------------------------------------------------

def test_every_line_of_a_jsonl_artifact_still_parses(client, db, objects):
    lines = [
        {"type": "system", "api_key": None},
        {"type": "tool_use", "input": {"command": 'echo "PASSWORD=' + V_QUOTED + '"'}},
        {"type": "tool_use", "input": {"password": PIN, "secrets": [V_LISTED]}},
        {"type": "text", "text": "the agent echoed " + LITERAL},
        {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    stored = "".join(json.dumps(line) + "\n" for line in lines)
    _a_finished_task(
        db, objects, files={"stdout.jsonl": stored}, metadata={"db_password": LITERAL}
    )

    for route, served in _both_routes(client, "stdout.jsonl").items():
        got = [json.loads(line) for line in served.splitlines() if line.strip()]
        assert len(got) == len(lines), route
        for secret in (V_QUOTED, V_LISTED, PIN_TEXT, *LITERAL_MARKS):
            assert secret not in served, (route, secret)
        assert got[0] == {"type": "system", "api_key": None}, route
        assert got[2]["input"] == {"password": MASK, "secrets": MASK}, route
        assert got[4] == lines[4], route


# --------------------------------------------------------------------------
# Paging: a transcript read window by window
# --------------------------------------------------------------------------

def _long_transcript(*specials: dict[str, Any]) -> str:
    """Enough ordinary events around `specials` to need several 4 KiB windows."""
    events: list[dict[str, Any]] = []
    for n in range(48):
        events.append({"type": "text", "index": n, "text": f"step {n}: " + "ordinary words " * 16})
        if n % 12 == 5 and specials:
            events.append(specials[(n // 12) % len(specials)])
    return _transcript(*events)


def test_a_transcript_read_in_pages_joins_into_json_with_every_secret_masked(client, db, objects):
    stored = _long_transcript(
        {"type": "tool_use", "input": {"command": 'echo "PASSWORD=' + V_QUOTED + '"'}},
        {"type": "config", "api_key": None, "password": PIN, "secrets": [V_LISTED, V_NESTED]},
        {"type": "tool_result", "content": PEM_BEGIN + "\n" + "\n".join(KEY_BODY) + "\n" + PEM_END},
    )
    assert len(stored) > 3 * 4096, "control: the transcript needs several windows"
    _a_finished_task(db, objects, files={"claude-transcript.json": stored})

    joined, windows = _paged(client, "claude-transcript.json")
    assert windows >= 3
    json.loads(joined)  # the defect: this raised
    for secret in (V_QUOTED, V_LISTED, V_NESTED, PIN_TEXT, *KEY_BODY):
        assert secret not in joined, secret


def test_a_key_written_as_a_list_across_windows_is_masked_in_every_window(client, db, objects):
    """A private key stored as a list of lines, longer than a window: the
    window that opens inside it (`inside_key`) masks its lines too."""
    body = [hashlib.sha256(f"key line {n}".encode()).hexdigest() for n in range(120)]
    stored = _transcript(
        {"type": "text", "text": "before"},
        {"type": "tool_result", "key_lines": [PEM_BEGIN, *body, PEM_END], "after": "kept"},
    )
    assert len(stored) > 2 * 4096, "control: the key must span windows"
    _a_finished_task(db, objects, files={"claude-transcript.json": stored})

    joined, windows = _paged(client, "claude-transcript.json")
    assert windows >= 2
    parsed = json.loads(joined)
    for line in body:
        assert line not in joined, line
    assert parsed["events"][1]["after"] == "kept"

    for route, served in _both_routes(client, "claude-transcript.json").items():
        assert json.loads(served)["events"][1]["key_lines"][1:] == [MASK] * (len(body) + 1), route


# --------------------------------------------------------------------------
# What must NOT change
# --------------------------------------------------------------------------

def test_a_clean_transcript_is_served_byte_for_byte(client, db, objects):
    stored = _transcript(
        {"type": "text", "text": "café — nothing secret here", "n": 1.50},
        {"type": "tool_use", "input": {"path": "src/app.py", "tokens": 3}},
    )
    stored = stored.replace('"src/app.py"', '"src\\/app.py"')  # an escape json.loads accepts
    assert "\\u00e9" in stored, "control: the stored text keeps its escapes"
    _a_finished_task(db, objects, files={"claude-transcript.json": stored})

    body = _content(client, "claude-transcript.json")
    assert body["content"] == stored
    assert body["redacted"] is False
    assert body["redaction_count"] == 0
    assert _raw(client, "claude-transcript.json") == stored


def test_a_plain_text_artifact_still_goes_through_redact_lines(client, db, objects):
    stored = (
        "# notes\n"
        "DB_PASSWORD=" + V_EXPORTED + "\n"
        + PEM_BEGIN + "\n" + "\n".join(KEY_BODY) + "\n" + PEM_END + "\n"
        + "the end\n"
    )
    _a_finished_task(db, objects, files={"notes.md": stored})

    expected = redact_lines(stored).text
    assert V_EXPORTED not in expected and KEY_BODY[0] not in expected, "control"
    for route, served in _both_routes(client, "notes.md").items():
        assert served == expected, route
    assert _masking().redact_json_window(stored).text == expected


# --------------------------------------------------------------------------
# The function itself
# --------------------------------------------------------------------------

def _parity_documents() -> list[Any]:
    return [
        {"command": 'echo "PASSWORD=' + V_QUOTED + '"', "n": 1},
        {"api_key": None, "token": True, "password": PIN, "secrets": [V_LISTED],
         "credential": {"value": V_NESTED, "id": 7}, "input_tokens": 10},
        {"content": PEM_BEGIN + "\n" + "\n".join(KEY_BODY) + "\n", "after": "kept"},
        {"key_lines": [PEM_BEGIN, *KEY_BODY, PEM_END, "after"]},
        # A literal named in one place, masked where it appears elsewhere.
        {"prompt": "use " + V_NESTED + " to deploy", "deploy_token": V_NESTED},
        {"notes": ["DB_PASSWORD=" + V_EXPORTED + " is set", "then " + V_EXPORTED + " again"]},
        {"DB_PASSWORD=" + V_EXPORTED: "a key that holds a credential"},
        [{"password": "", "secret": "   "}, {"deeper": {"private_key": [1, 2]}}],
        {"clean": ["nothing", 1, 2.5, None, False, {"x": "y"}]},
    ]


@pytest.mark.parametrize("index", range(len(_parity_documents())))
def test_the_token_masker_agrees_with_a_whole_document_walk(index):
    """Parity with `redact_json`, the walk `/input` masks a decoded document
    with: the same values masked, the same count, over the pretty-printed
    text the worker writes."""
    document = _parity_documents()[index]
    stored = json.dumps(document, indent=2)
    walked = redact_json(document)

    got = _masking().redact_json_window(stored)
    assert json.loads(got.text) == json.loads(walked.text), got.text
    assert got.count == walked.count


def test_a_window_opening_inside_a_key_masks_its_leading_lines():
    """A page that begins inside a key written as a list, with no text before
    it to say so except the look-back's `inside_key`."""
    page = "".join(f'      "{line}",\n' for line in KEY_BODY) + f'      "{PEM_END}"\n    ],\n    "after": "kept"\n'
    got = _masking().redact_json_window(page, inside_key=True, fragment=True)
    for line in KEY_BODY:
        assert line not in got.text, got.text
    assert '"after": "kept"' in got.text


def test_a_value_opened_before_the_window_is_not_served():
    """The page before masked `"secrets": [` to its end; this page, told what
    came before it, serves nothing of that list, and the two join into JSON."""
    masking = _masking()
    stored = json.dumps({"a": 1, "secrets": [V_LISTED, V_NESTED], "b": 2}, indent=2) + "\n"
    cut = stored.index(V_LISTED)
    cut = stored.rindex("\n", 0, cut) + 1  # a page boundary inside the list
    first, second = stored[:cut], stored[cut:]

    one = masking.redact_json_window(first, fragment=True)
    two = masking.redact_json_window(second, fragment=True, context=first)
    joined = one.text + two.text
    assert V_LISTED not in joined and V_NESTED not in joined, joined
    assert json.loads(joined) == {"a": 1, "secrets": MASK, "b": 2}
    assert one.count + two.count == 1


def test_a_window_over_the_bound_goes_through_redact_lines(monkeypatch):
    masking = _masking()
    stored = _escaped_quote_transcript()
    monkeypatch.setattr(masking, "JSON_WINDOW_MAX_CHARS", len(stored) - 1)
    assert masking.redact_json_window(stored).text == redact_lines(stored).text
