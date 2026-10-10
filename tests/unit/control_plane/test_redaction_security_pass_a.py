"""Security pass SEC-REDACT-A: five ways a credential was served unmasked.

Each box is an epic comment (#227, #361) from the PR #229 security review and
the #378 re-review. Every test below reproduced its leak on main before the
fix; each names the box it pins.

BOX 1 (#227 5863061029). A learned literal holding a NEWLINE was split when a
window was cut at that newline -- `/artifacts/raw` (`_redacted_text`), and
the paged `/artifacts/content` and `/logs` (`inspect._align`) -- so neither
half was the literal and both were served. A window boundary now never falls
inside a literal (`redaction.straddled`), at its tail or at a head a caller
chose with `offset`.

BOX 2 (#227 5863061208). A line longer than one window was cut at whitespace,
so `password= <v>` cut after the `=` served `<v>` at the head of the next
window, and a JSON line cut that way skipped the structural pass. The raw
download now carries a line whole (up to `MAX_TEXT_CARRY`), and no window
boundary falls between a credential's name and the end of its value.

BOX 3 (#227 5863061376). A JSON line past `JSON_LINE_MAX_CHARS`, or one with
a duplicate key, fell back to the text rule, which masked a quoted value up
to its first space. The text path now masks a credential-named JSON member's
whole string, list or object (`redaction._mask_credential_members`).

BOX 4 (#227 5863061516). `_mask_literals` looked for a literal only as
written, so one holding `"`, `\\` or a non-ASCII character was not found in
JSON-escaped text. Its escaped forms are looked for too.

BOX 5 (#361 5905384100). A BEGIN marker inside a list masked whole under a
credential's name, with the key's body in the members after it, served the
body on `/logs` and `/input` (`JsonMasker`); the artifact path already masked
it (#378). `JsonMasker` now masks every string from such a BEGIN through its
END, as the artifact path does.

Every secret, value and marker here is built at runtime: nothing in this file
may read as a credential literal to a secret scan.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api.redaction import JSON_LINE_MAX_CHARS, MASK, JsonMasker, redact, redact_lines

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"
PASSWORD = "pass" + "word"
#: The smallest window `/logs` and `/artifacts/content` serve (`min_log_bytes`).
PAGE = 4096


def _word(tag: str) -> str:
    """A value no rule masks on its own, different on every run."""
    return f"{tag}-{secrets.token_hex(6)}"


def _pad(n: int) -> str:
    """`n` bytes of ordinary lines, ending on a newline."""
    full, rest = divmod(n, 64)
    text = ("x" * 63 + "\n") * full
    if rest:
        text += "x" * (rest - 1) + "\n"
    assert len(text) == n
    return text


def _spaces(n: int) -> str:
    """`n` bytes of ordinary words on ONE line, ending on a space."""
    assert n % 2 == 0
    return "w " * (n // 2)


# --------------------------------------------------------------------------
# Seeding and reading, through the shipped routes
# --------------------------------------------------------------------------

def _finished_task(db, objects, *, files: dict[str, str] | None = None, metadata: dict[str, Any] | None = None,
                   log: str | None = None, input_doc: dict[str, Any] | None = None) -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    doc["metadata"] = dict(metadata or {})
    if input_doc is not None:
        doc["input"] = input_doc
    entries = []
    for name, body in (files or {}).items():
        key = f"tenants/eng/tasks/task_a/attempts/att_1/artifacts/{name}"
        objects.put(key, body.encode("utf-8"))
        entries.append({"name": name, "bytes": len(body.encode()), "uri": f"gs://{BUCKET}/{key}"})
    doc["result_summary"] = {"artifacts": entries, "artifact_bytes": 0, "logs": {}}
    doc["completed_at"] = datetime.now(timezone.utc)
    if log is not None:
        moment = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        db.collection("attempts").document("att_1").set({
            "attempt_id": "att_1", "task_id": "task_a", "tenant_id": "eng", "generation": 1,
            "lease_id": "lease_att_1", "backend": "CLOUD_RUN_JOB", "execution_name": "x",
            "created_at": moment, "started_at": moment, "completed_at": moment, "exit_code": 0,
            "error": None, "peak_rss_bytes": 1, "oom_near_miss": False, "checkpoints": [],
        })
        objects.put("tenants/eng/tasks/task_a/attempts/att_1/logs/stdout.log", log)


def _log_page(client, offset: int, limit: int = PAGE) -> dict[str, Any]:
    response = client.get(
        "/v1/tasks/task_a/logs",
        params={"stream": "stdout", "offset": offset, "limit_bytes": limit},
        headers=auth_header("alice"),
    )
    assert response.status_code == 200, response.text
    return next(s for s in response.json()["streams"] if s["stream"] == "stdout")


def _content_page(client, name: str, offset: int, limit: int = PAGE) -> dict[str, Any]:
    response = client.get(
        "/v1/tasks/task_a/artifacts/content",
        params={"name": name, "offset": offset, "limit_bytes": limit},
        headers=auth_header("alice"),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _every_page(read) -> list[str]:
    """Page from offset 0 to the end, as a client following `next_offset` does."""
    pages: list[str] = []
    offset: int | None = 0
    while offset is not None and len(pages) < 50:
        page = read(offset)
        pages.append(page["content"])
        offset = page.get("next_offset")
    return pages


def _raw(client, name: str) -> str:
    response = client.get(
        "/v1/tasks/task_a/artifacts/raw", params={"name": name}, headers=auth_header("alice")
    )
    assert response.status_code == 200, response.text
    return response.text


@pytest.fixture
def small_windows(client):
    """`/artifacts/raw` in 64-byte windows, through FastAPI's own injection point."""
    from swarm_api.agent_output import AgentOutputService
    from swarm_api.routes.tasks import agent_output_service

    context = client.app.state.ctx
    client.app.dependency_overrides[agent_output_service] = lambda: AgentOutputService(
        context.inspection, chunk_bytes=64
    )
    yield
    client.app.dependency_overrides.pop(agent_output_service, None)


# --------------------------------------------------------------------------
# Box 1: a learned literal holding a newline, cut at that newline
# --------------------------------------------------------------------------

HALF_A = _word("brass")
HALF_B = _word("lantern")
SPLIT_LITERAL = HALF_A + "\n" + HALF_B


def _assert_no_half(*texts: str) -> None:
    for text in texts:
        assert HALF_A not in text, text[-300:]
        assert HALF_B not in text, text[-300:]


def test_box1_control_the_literal_is_learned_and_neither_half_is_masked_alone():
    """The control: each half alone matches no rule, so only the whole literal masks it."""
    masker = JsonMasker({"metadata": {PASSWORD: SPLIT_LITERAL}})
    assert SPLIT_LITERAL in masker.literals
    assert redact(HALF_A).count == 0 and redact(HALF_B).count == 0


def test_box1_raw_download_never_cuts_a_learned_literal_at_its_newline(client, db, objects, small_windows):
    # The literal's newline is the last one inside the first 64-byte window.
    body = "filler " * 6 + SPLIT_LITERAL + " tail\n" + "more words\n"
    assert body.index("\n") < 64 < body.index(HALF_B) + len(HALF_B)
    _finished_task(db, objects, files={"notes.txt": body}, metadata={PASSWORD: SPLIT_LITERAL})

    served = _raw(client, "notes.txt")
    _assert_no_half(served)
    assert MASK in served and "tail" in served


def test_box1_logs_pages_never_cut_a_learned_literal_at_its_newline(client, db, objects):
    body = _pad(PAGE - len(HALF_A) - 4) + SPLIT_LITERAL + " tail\n"
    assert body.index("\n" + HALF_B) < PAGE < body.index(HALF_B) + len(HALF_B)
    _finished_task(db, objects, log=body, metadata={PASSWORD: SPLIT_LITERAL})

    pages = _every_page(lambda at: _log_page(client, at))
    _assert_no_half(*pages)
    assert any(MASK in page for page in pages)


def test_box1_logs_offset_inside_a_learned_literal_does_not_serve_its_tail(client, db, objects):
    """A caller choosing the offset right after the literal's newline: the look-back sees the literal."""
    body = _pad(256) + SPLIT_LITERAL + " tail\n" + _pad(256)
    _finished_task(db, objects, log=body, metadata={PASSWORD: SPLIT_LITERAL})

    page = _log_page(client, body.index(HALF_B))
    _assert_no_half(page["content"])
    assert "tail" in page["content"]


def test_box1_artifact_content_pages_never_cut_a_learned_literal_at_its_newline(client, db, objects):
    body = _pad(PAGE - len(HALF_A) - 4) + SPLIT_LITERAL + " tail\n"
    _finished_task(db, objects, files={"notes.txt": body}, metadata={PASSWORD: SPLIT_LITERAL})

    pages = _every_page(lambda at: _content_page(client, "notes.txt", at))
    _assert_no_half(*pages)
    page = _content_page(client, "notes.txt", body.index(HALF_B))
    _assert_no_half(page["content"])


# --------------------------------------------------------------------------
# Box 2: a line longer than a window, cut after `password=`
# --------------------------------------------------------------------------

VALUE = "Zq" + secrets.token_hex(8)


def test_box2_control_the_whole_line_masks_the_value():
    line = _spaces(54) + PASSWORD + "= " + VALUE + " and more\n"
    assert VALUE not in redact(line).text
    assert redact(VALUE).count == 0


def test_box2_raw_download_does_not_cut_a_long_line_after_the_equals(client, db, objects, small_windows):
    line = _spaces(54) + PASSWORD + "= " + VALUE + " and more words here\n"
    assert line.index(VALUE) == 64, "the first window ends on the space after the `=`"
    _finished_task(db, objects, files={"run.log": line + "next line\n"})

    served = _raw(client, "run.log")
    assert VALUE not in served, served
    assert MASK in served and "next line" in served


def test_box2_raw_download_keeps_a_long_json_line_whole_for_the_structural_pass(
    client, db, objects, small_windows
):
    words = [_word("v1"), _word("v2"), _word("v3")]
    line = json.dumps({"note": _spaces(80), PASSWORD: " ".join(words), "n": 1}) + "\n"
    _finished_task(db, objects, files={"events.jsonl": line + line})

    served = _raw(client, "events.jsonl")
    for word in words:
        assert word not in served, served
    for row in served.splitlines():
        assert json.loads(row)[PASSWORD] == MASK


def test_box2_logs_pages_do_not_cut_a_long_line_after_the_equals(client, db, objects):
    line = _spaces(PAGE - len(PASSWORD) - 2) + PASSWORD + "= " + VALUE + " and more\n"
    assert line.index(VALUE) == PAGE
    _finished_task(db, objects, log=line)

    pages = _every_page(lambda at: _log_page(client, at))
    for page in pages:
        assert VALUE not in page, page[-200:]
    assert any(MASK in page for page in pages)


def test_box2_logs_offset_at_the_value_does_not_serve_it(client, db, objects):
    line = _spaces(200) + PASSWORD + "= " + VALUE + " and more\n"
    _finished_task(db, objects, log=line)

    page = _log_page(client, line.index(VALUE))
    assert VALUE not in page["content"], page["content"]


def test_box2_artifact_content_pages_do_not_cut_a_long_line_after_the_equals(client, db, objects):
    line = _spaces(PAGE - len(PASSWORD) - 2) + PASSWORD + "= " + VALUE + " and more\n"
    _finished_task(db, objects, files={"run.log": line})

    pages = _every_page(lambda at: _content_page(client, "run.log", at))
    for page in pages:
        assert VALUE not in page, page[-200:]


def test_box2_artifact_content_pages_do_not_cut_inside_a_quoted_credential_value(client, db, objects):
    """A JSON line longer than a page, cut by whitespace inside `"password": "<a> <b>"`."""
    words = [_word("v1"), _word("v2")]
    prefix = '{"note": "' + _spaces(PAGE - 30) + '", "' + PASSWORD + '": "'
    line = prefix + " ".join(words) + '"}\n'
    assert line.index(words[1]) > PAGE > line.index(words[0])
    _finished_task(db, objects, files={"big.txt": line})

    pages = _every_page(lambda at: _content_page(client, "big.txt", at))
    for page in pages:
        for word in words:
            assert word not in page, page[-200:]


# --------------------------------------------------------------------------
# Box 3: an over-long or duplicate-keyed JSON line on the text rule
# --------------------------------------------------------------------------

def _three() -> list[str]:
    return [_word("first"), _word("second"), _word("third")]


def test_box3_a_json_line_past_the_decode_bound_masks_the_whole_value():
    words = _three()
    line = json.dumps({"pad": "x" * JSON_LINE_MAX_CHARS, PASSWORD: " ".join(words), "n": 1})
    got = redact_lines(line + "\n")
    for word in words:
        assert word not in got.text
    assert got.count >= 1
    assert got.text.endswith('"' + PASSWORD + '": "' + MASK + '", "n": 1}\n')


@pytest.mark.parametrize(
    "build",
    [
        lambda w: '{"' + PASSWORD + '":"' + " ".join(w) + '","' + PASSWORD + '":"ok"}',
        lambda w: '{"api_key": "' + " ".join(w) + '", "api_key": 1}',
        lambda w: '{"secret":["' + w[0] + " " + w[1] + '","' + w[2] + '"],"secret":1}',
        lambda w: '{"cmd":"run {\\"api_key\\": \\"' + " ".join(w) + '\\"}","cmd":"x"}',
    ],
    ids=["string", "spaced-colon", "list", "escaped-json-in-a-string"],
)
def test_box3_a_duplicate_keyed_json_line_masks_the_whole_value(build):
    words = _three()
    line = build(words)
    json.loads(line)  # each is JSON; only the duplicate key sends it to the text rule
    got = redact_lines(line + "\n")
    for word in words:
        assert word not in got.text, got.text
    assert got.count >= 1


def test_box3_a_duplicate_keyed_line_is_masked_on_logs(client, db, objects):
    words = _three()
    line = '{"' + PASSWORD + '":"' + " ".join(words) + '","' + PASSWORD + '":"ok"}\n'
    _finished_task(db, objects, log=line)

    content = _log_page(client, 0)["content"]
    for word in words:
        assert word not in content, content


# --------------------------------------------------------------------------
# Box 4: a literal holding `"`, `\` or non-ASCII, in JSON-escaped text
# --------------------------------------------------------------------------

ODD_LITERAL = (
    "q" + secrets.token_hex(4) + '"' + secrets.token_hex(4) + "\\" + secrets.token_hex(4)
    + "é" + secrets.token_hex(4)
)
#: Every run of the literal between its odd characters.
ODD_PIECES = ODD_LITERAL.replace('"', " ").replace("\\", " ").replace("é", " ").split()


@pytest.mark.parametrize(
    "form",
    [
        json.dumps(ODD_LITERAL)[1:-1],
        json.dumps(ODD_LITERAL, ensure_ascii=False)[1:-1],
        json.dumps(json.dumps(ODD_LITERAL))[1:-1],
        json.dumps(json.dumps(ODD_LITERAL, ensure_ascii=False), ensure_ascii=False)[1:-1],
        # An encoder that writes upper-case hex (`\u00E9`).
        json.dumps(ODD_LITERAL)[1:-1].replace("\\u00e9", "\\u00E9"),
    ],
    ids=["ascii", "utf8", "double-ascii", "double-utf8", "upper-hex"],
)
def test_box4_an_escaped_literal_is_found_in_text(form):
    line = "agent said: " + form + " and stopped\n"
    got = redact_lines(line, literals=(ODD_LITERAL,))
    for piece in ODD_PIECES:
        assert piece not in got.text, got.text
    assert got.count >= 1


def test_box4_an_escaped_literal_is_masked_on_logs_and_raw(client, db, objects):
    assert JsonMasker({"m": {PASSWORD: ODD_LITERAL}}).literals[0] == ODD_LITERAL
    # Not a whole document (a prefix), so the line takes the text rule.
    line = "event: " + json.dumps({"said": ODD_LITERAL}) + "\n"
    _finished_task(db, objects, log=line, files={"out.txt": line}, metadata={PASSWORD: ODD_LITERAL})

    for content in (_log_page(client, 0)["content"], _raw(client, "out.txt")):
        for piece in ODD_PIECES:
            assert piece not in content, content


# --------------------------------------------------------------------------
# Box 5: a BEGIN inside a masked list under a credential's name
# --------------------------------------------------------------------------

PEM_BEGIN = "-----" + "BEGIN" + " RSA PRIVATE KEY" + "-----"
PEM_END = "-----" + "END" + " RSA PRIVATE KEY" + "-----"
BODY0 = "MIIE" + secrets.token_hex(30)
BODY1 = "MIIB" + secrets.token_hex(30)


def _split_key_document() -> dict[str, Any]:
    return {"secret": [PEM_BEGIN], "x": BODY0, "y": BODY1, "z": PEM_END, "after": "kept"}


def test_box5_control_a_body_line_alone_matches_no_rule():
    assert redact(BODY0, decoded=True).count == 0 and redact(BODY1, decoded=True).count == 0


@pytest.mark.parametrize("route", ["json", "value", "logs"])
def test_box5_a_begin_in_a_masked_list_masks_the_body_outside_it(route):
    document = _split_key_document()
    if route == "json":
        got = JsonMasker(document).json(document)
        text, count = got.text, got.count
    elif route == "value":
        value, count = JsonMasker(document).value(document)
        text = json.dumps(value)
    else:
        got = redact_lines(json.dumps(document, separators=(",", ":")) + "\n")
        text, count = got.text, got.count
    assert BODY0 not in text and BODY1 not in text, text
    served = json.loads(text)
    assert served["after"] == "kept", "the run ends at the END marker"
    assert count == 1, "the masked list counts once; its key's lines count 0"


def test_box5_is_masked_on_input(client, db, objects):
    _finished_task(db, objects, input_doc={"prompt": "rotate it", "env": _split_key_document()})
    response = client.get("/v1/tasks/task_a/input", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert BODY0 not in response.text and BODY1 not in response.text, response.text


def test_box5_is_masked_on_logs_and_stays_masked_on_artifacts(client, db, objects):
    line = json.dumps(_split_key_document(), separators=(",", ":")) + "\n"
    _finished_task(db, objects, log=line, files={"env.json": line})

    for content in (
        _log_page(client, 0)["content"],
        _content_page(client, "env.json", 0)["content"],
        _raw(client, "env.json"),
    ):
        assert BODY0 not in content and BODY1 not in content, content
        assert json.loads(content)["after"] == "kept"
