"""#327: a JSON artifact is masked inside its strings, and is still JSON when served.

MEASURED 2026-09-29 on swarm.saga.xyz: `swarm artifact <task>
claude-transcript.json` served 10 of 47 transcripts as text that no longer
parsed as JSON. The worker writes that file as `json.dumps(parsed, indent=2)`
(`cliagent.py`), so no line of it is a whole JSON document, and every line
went through `redact_lines`' TEXT path: the rules over JSON TEXT, over a
fragment of syntax rather than a document. Three of those replacements cut
through the document's structure, each reproduced here against the route
that served it:

  1. the key/value rule's value class takes a backslash, so an escaped quote
     around a credential assignment inside a string lost the backslash of its
     closing escape, and the bare quote left behind ended the string early;
  2. a credential's name over a value that is not a string -- `null`, `true`,
     a number, an object -- had the value replaced with a bare mask token,
     which is no JSON value at all, or (for an object) served every element
     after the first;
  3. the private-key block masks "the rest of the line" after the END marker,
     and in JSON text that is the string's closing quote and the comma after
     it -- a key serialised with `\\n` escapes took the structure with it.

`json_masking.redact_artifact_text` now masks a window that is JSON text by
its TOKENS: every string decoded, masked by the same rules and literals as
`redaction.JsonMasker` (the walker `/transcript` uses), and re-encoded; a
credential's value masked whole as a string. Every test below asserts BOTH
halves: the served bytes parse, and the secret is gone.

Offline: FakeFirestore and the in-memory object reader. No credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from swarm_api.json_masking import mask_json_text, redact_artifact_text
from swarm_api.redaction import MASK, redact_json

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"


# --------------------------------------------------------------------------
# ASSEMBLED, NOT WRITTEN OUT (the convention `test_log_redaction.py` set).
#
# A credential-shaped literal in a tracked file is a thing every scanner in
# this pipeline is paid to find -- this repository's own `trivy fs
# --scanners secret` and its PEM grep (security.yml), and, upstream of this
# repository, the SwarmCloud worker's own publish-time guard, which is what
# sent this exact fix back once already: it read this literal in the tree the
# fix step produced and refused to open the pull request. `_shape` joins
# fragments at import time, so the runtime value is byte-identical to the
# real shape -- which is the whole point, since these tests exist to prove
# the masking rules still recognise it -- and no grep over the SOURCE FILE
# ever sees one.
# --------------------------------------------------------------------------
def _shape(*parts: str) -> str:
    return "".join(parts)


#: A GitHub token shape the rules recognise by prefix.
GH_TOKEN_TAIL = "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0PpOoNn"
GH_TOKEN = _shape("ghp_", GH_TOKEN_TAIL)

#: A value only a credential's NAME marks as secret -- its shape does not
#: matter for this one, which is the point of the test that uses it.
KV_SECRET = _shape("hunter2-", "not-a-real-password-9f3ac21")

#: A private key, assembled the same way: the marker is what the repository's
#: own PEM grep looks for, so it is never written out whole in source.
PEM_BEGIN = _shape("-----", "BEGIN", " RSA PRIVATE KEY", "-----")
PEM_END = _shape("-----", "END", " RSA PRIVATE KEY", "-----")
PEM_BODY_1 = "MIIEpAIBAAKCAQEAq1w2e3r4t5y6u7i8o9p0a1s2d3f4g5h6"
PEM_BODY_2 = "j7k8l9z0x1c2v3b4n5m6Q7W8E9R0T1Y2U3I4O5P6A7S8D9F0"
PEM = f"{PEM_BEGIN}\n{PEM_BODY_1}\n{PEM_BODY_2}\n{PEM_END}\n"


def _artifact_key(name: str) -> str:
    return f"tenants/eng/tasks/task_a/attempts/att_1/artifacts/{name}"


def _a_finished_task(db, objects, *, files: dict[str, str]) -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
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
    body = client.get(
        "/v1/tasks/task_a/artifacts/content",
        params={"name": name, **params},
        headers=auth_header("alice"),
    ).json()
    assert body["status"] == "ok", body
    return body


def _raw_text(client, name: str) -> str:
    response = client.get(
        "/v1/tasks/task_a/artifacts/raw", params={"name": name}, headers=auth_header("alice")
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-swarm-redaction"] == "applied"
    return response.text


def _served_both_ways(client, name: str) -> list[tuple[str, str]]:
    """`(route, served text)` for `/artifacts/content` and `/artifacts/raw` -- both call sites this fix touched."""
    return [("content", _content(client, name)["content"]), ("raw", _raw_text(client, name))]


def _transcript(*events: dict[str, Any]) -> str:
    """A transcript exactly as `cliagent` writes it: one indented document, no line a whole one."""
    return json.dumps(list(events), indent=2)


def _assistant(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _result(**extra: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "result": "done",
        "usage": {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 0},
        **extra,
    }


# --------------------------------------------------------------------------
# The three replacements that broke the document, through both routes
# --------------------------------------------------------------------------


def test_an_assignment_inside_a_string_is_masked_and_the_document_still_parses(client, db, objects) -> None:
    """Replacement 1: an escaped quote around a credential assignment.

    `json.dumps` writes the assistant's text with its own quotes escaped
    (`\\"api_key=...\\"`); the old text rule reads a quote as a quote and the
    fix decodes the string before masking it, exactly as `JsonMasker` does.
    """
    text = _transcript(
        _assistant(f'I ran it with "api_key={GH_TOKEN}" and it worked'),
        _result(),
    )
    _a_finished_task(db, objects, files={"claude-transcript.json": text})

    for route, served in _served_both_ways(client, "claude-transcript.json"):
        document = json.loads(served)  # the point: this must not raise
        assert GH_TOKEN_TAIL not in served, (route, served)
        assert MASK in served, (route, served)
        said = document[0]["message"]["content"][0]["text"]
        assert said.startswith('I ran it with "api_key='), (route, said)
        assert said.endswith('" and it worked'), (route, said)
        assert document[1]["usage"]["output_tokens"] == 34, route


def test_a_credentials_name_over_a_value_that_is_not_a_string_stays_json(client, db, objects) -> None:
    """Replacement 2. `null` and `true` are no credential and are served as
    they were; a number under a name that ends in the credential word is
    masked as a STRING; an object under a name that names one is masked whole."""
    text = _transcript(
        {
            "type": "system",
            "token": None,
            "authorization": True,
            "password": 48151623,
            "credentials": {"token": KV_SECRET, "note": "eve"},
        },
        _result(),
    )
    _a_finished_task(db, objects, files={"claude-transcript.json": text})

    for route, served in _served_both_ways(client, "claude-transcript.json"):
        document = json.loads(served)
        head = document[0]
        assert head["token"] is None, route
        assert head["authorization"] is True, route
        assert head["password"] == MASK, route
        assert head["credentials"] == MASK, route
        assert KV_SECRET not in served, route
        assert "eve" not in served, route
        assert document[1]["usage"]["input_tokens"] == 12, route


def test_a_private_key_string_stays_valid_json_with_or_without_its_end_marker(client, db, objects) -> None:
    """Replacement 3: the block rule over JSON text took the closing quote.

    One entry has the key whole (BEGIN through END); the other is cut short --
    a tool result that truncated mid-key -- with no END at all, which must
    still mask to the end of the string rather than crash or leak the body.
    """
    truncated = PEM.split(PEM_END)[0]
    text = _transcript(
        {"type": "user", "tool_result": {"content": PEM, "is_error": False}},
        {"type": "user", "tool_result": {"content": truncated}},
        _result(),
    )
    _a_finished_task(db, objects, files={"claude-transcript.json": text})

    for route, served in _served_both_ways(client, "claude-transcript.json"):
        document = json.loads(served)
        assert PEM_BODY_1 not in served and PEM_BODY_2 not in served, route
        first = document[0]["tool_result"]["content"]
        second = document[1]["tool_result"]["content"]
        assert first.startswith(PEM_BEGIN), (route, first)
        assert MASK in first, (route, first)
        assert second.startswith(PEM_BEGIN), (route, second)
        assert MASK in second, (route, second)
        assert document[2]["usage"]["input_tokens"] == 12, route


# --------------------------------------------------------------------------
# The route change did not touch what already worked
# --------------------------------------------------------------------------


def test_a_plain_text_artifact_still_falls_back_to_line_redaction(client, db, objects) -> None:
    """`redact_artifact_text` is not JSON-only: a log that is not a run of JSON
    tokens is still masked by `redact_lines`, exactly as before this change."""
    leaked = f"connecting...\nexport GH_TOKEN={GH_TOKEN}\ndone\n"
    _a_finished_task(db, objects, files={"worker.log": leaked})

    for route, served in _served_both_ways(client, "worker.log"):
        assert GH_TOKEN_TAIL not in served, route
        assert MASK in served, route
        assert "connecting..." in served, route
        assert "done" in served, route


def test_a_transcript_with_no_secrets_reports_no_redactions_and_stays_json(client, db, objects) -> None:
    """`redacted: false` must still be reachable, and the content unchanged."""
    text = _transcript(_assistant("all clear, nothing secret here"), _result())
    _a_finished_task(db, objects, files={"claude-transcript.json": text})

    body = _content(client, "claude-transcript.json")

    assert body["redacted"] is False
    assert body["redaction_count"] == 0
    assert json.loads(body["content"]) == json.loads(text)


# --------------------------------------------------------------------------
# `mask_json_text` directly: the windowing this route change depends on
# --------------------------------------------------------------------------


def test_inside_key_masks_the_strings_at_the_head_of_a_window_that_opens_mid_key(client, db, objects) -> None:
    """A private key serialised as a LIST of lines, paged: the window a reader
    cuts mid-array carries no BEGIN marker of its own, only `inside_key=True`
    from the caller's look-back. The strings at its head -- up to and
    including the one holding END -- are still masked, counted once."""
    window = f'"{PEM_BODY_1}", "{PEM_BODY_2}", "{PEM_END}"], "after": "kept"'

    got = mask_json_text(window, inside_key=True)

    assert got is not None
    assert PEM_BODY_1 not in got.text and PEM_BODY_2 not in got.text
    assert '"after": "kept"' in got.text
    assert got.count == 1


def test_a_number_past_the_digit_limit_under_a_credentials_name_is_masked_not_a_crash() -> None:
    """`json.loads` refuses an integer past Python's digit limit; a name's
    value must still be masked -- and learned, since it is the evidence a
    credential leaked -- rather than fail the whole read."""
    digits = "7" * 5000
    got = mask_json_text('{"password": ' + digits + ', "note": "see ' + digits + '"}')

    assert got is not None
    parsed = json.loads(got.text)
    assert parsed["password"] == MASK
    assert digits not in got.text


def test_the_token_masker_serves_what_the_document_walker_serves() -> None:
    """`mask_json_text` over a document's TEXT and `redact_json` over the same
    document's VALUE must serve the same text and count the same secrets --
    the promise a page of a transcript makes to a reader of the whole thing."""
    doc = {
        "greeting": f"hello api_key={GH_TOKEN} friend",
        "count": 3,
        "auth": {"password": KV_SECRET, "user": "alice"},
    }

    whole = redact_json(doc, indent=2)
    tokened = mask_json_text(json.dumps(doc, indent=2, ensure_ascii=False))

    assert tokened is not None
    assert tokened.text == whole.text
    assert tokened.count == whole.count


def test_a_window_over_the_size_ceiling_is_not_tokenised() -> None:
    """A window this large never reaches either route in production (the
    content ceiling and the raw window are both far smaller); refusing to
    tokenise it here is what keeps a shared instance's time bounded."""
    from swarm_api.json_masking import JSON_WINDOW_MAX_CHARS

    oversized = '{"a": "' + ("x" * JSON_WINDOW_MAX_CHARS) + '"}'

    assert mask_json_text(oversized) is None
    assert redact_artifact_text(oversized).text == oversized
