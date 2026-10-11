"""#385: a private key with no END in reach, split across JSON containers.

`JsonMasker` (`/input`, and `/logs` through `redact_lines`) followed a key
whose END it could not see only through a FLAT list of strings. A key whose
BEGIN sits in one object member and whose body sits in the next ones -- or in
a list of objects, nested lists, a list with a number in it, or the object's
KEYS -- served every body line in clear, under a count of 1.

What is pinned:

  * every body-shaped string after the BEGIN, in document order and across
    containers, is masked, and the key counts once;
  * an ordinary string after a truncated key (`"after the key"`, `"kept"`)
    ends the run and is served, as the text path stops at the first line not
    shaped like a key's body;
  * a short padded final line (`"AB=="`) is still body;
  * the run ends after `PEM_BLOCK_MAX_CHARS` of string content;
  * body lines stored as object KEYS are masked, and the text still parses;
  * the END-in-reach and flat-list behaviour is unchanged.

Every marker and body line is built at runtime (`_shape`, hashes): nothing
here may read as a credential literal to a secret scan.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from swarm_api.redaction import MASK, PEM_BLOCK_MAX_CHARS, JsonMasker, redact_lines

from .conftest import auth_header, seed_task, seed_tenant


def _shape(*parts: str) -> str:
    """Join fragments into a credential-shaped value at import time."""
    return "".join(parts)


def _body(seed: int) -> str:
    """One 64-character base64 line, derived at runtime: no literal key material."""
    digest = hashlib.sha512(_shape("issue-", "385-", str(seed)).encode()).digest()
    return base64.b64encode(digest).decode()[:64]


PEM_BEGIN = _shape("-----", "BEGIN", " RSA PRIVATE KEY", "-----")
PEM_END = _shape("-----", "END", " RSA PRIVATE KEY", "-----")
BODY0, BODY1, BODY2 = _body(0), _body(1), _body(2)
#: A short final line, padded: base64's own end-of-data mark.
SHORT_PADDED = _shape("AB", "==")


def _masked_json(document: Any) -> tuple[str, int]:
    got = JsonMasker(document).json(document)
    json.loads(got.text)  # still JSON
    return got.text, got.count


def _masked_value(document: Any) -> tuple[str, int]:
    value, count = JsonMasker(document).value(document)
    return json.dumps(value, ensure_ascii=False), count


def _masked_log(document: Any) -> tuple[str, int]:
    line = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    got = redact_lines(line + "\n")
    json.loads(got.text)
    return got.text, got.count


ROUTES = [_masked_json, _masked_value, _masked_log]


def _assert_masked(text: str, *served: str, withheld: tuple[str, ...] = (BODY0, BODY1)) -> None:
    for line in withheld:
        assert line not in text, text
    assert PEM_BEGIN in text, "the BEGIN marker is kept, as every rule keeps its prefix"
    for kept in served:
        assert kept in text, text


@pytest.mark.parametrize("route", ROUTES)
def test_jsonmasker_masks_a_no_end_key_split_across_object_members(route):
    # (a) the issue's reproduction.
    text, count = route({"a": PEM_BEGIN, "b": BODY0, "c": BODY1})
    _assert_masked(text)
    assert count == 1, "one key, one count"
    # (b) an ordinary string after the key ends the run and is served.
    text, count = route({"a": PEM_BEGIN, "b": BODY0, "c": BODY1, "after": "after the key"})
    _assert_masked(text, '"after the key"')
    assert count == 1
    # Blank strings between body lines keep the run going.
    text, count = route({"a": PEM_BEGIN, "b": BODY0, "gap": "  ", "c": BODY1, "after": "done"})
    _assert_masked(text, '"done"')
    assert count == 1


@pytest.mark.parametrize("route", ROUTES)
def test_jsonmasker_masks_a_no_end_key_in_a_list_of_objects_and_nested_lists(route):
    # (c) a list of objects.
    text, count = route(
        {"k": [{"line": PEM_BEGIN}, {"line": BODY0}, {"line": BODY1}], "after": "kept"}
    )
    _assert_masked(text, '"kept"')
    assert count == 1
    # (d) nested lists.
    text, count = route([[PEM_BEGIN], [BODY0], [BODY1]])
    _assert_masked(text)
    assert count == 1
    # (e) a list with a non-string element, which ended the flat-list run.
    text, count = route([PEM_BEGIN, 1, BODY0, BODY1])
    _assert_masked(text)
    assert count == 1
    assert json.loads(text)[1] == 1, "a number is neutral: served as it is"


@pytest.mark.parametrize("with_end", [True, False])
@pytest.mark.parametrize("route", ROUTES)
def test_jsonmasker_masks_body_lines_stored_as_object_keys(route, with_end):
    # (f) a PEM pasted into a .properties file, then converted to JSON.
    document: dict[str, Any] = {"PRIVATE_KEY": PEM_BEGIN, BODY0: "", BODY1: ""}
    if with_end:
        document[PEM_END] = ""
    text, count = route(document)
    for line in (BODY0, BODY1):
        assert line not in text, text
    assert PEM_END not in text, text
    assert count == 1, "the credential-named value counts once; its body lines count 0"
    # The same with a key that is not a credential's name: the BEGIN string opens the run.
    document = {"KEY": PEM_BEGIN, BODY0: "", BODY1: "", "after": "kept"}
    text, count = route(document)
    _assert_masked(text, '"kept"')
    assert count == 1


def test_masked_body_keys_stay_distinct_entries_in_value():
    """`value()` cannot hold one key twice: a colliding mask is numbered."""
    document = {"KEY": PEM_BEGIN, BODY0: "", BODY1: "", BODY2: ""}
    value, count = JsonMasker(document).value(document)
    assert len(value) == 4
    assert MASK in value and f"{MASK} (2)" in value and f"{MASK} (3)" in value
    assert count == 1


@pytest.mark.parametrize("route", ROUTES)
def test_an_ordinary_string_after_a_truncated_key_is_served(route):
    # (g) a short padded final line is body; a short unpadded word is not.
    text, count = route({"a": PEM_BEGIN, "b": BODY0, "c": SHORT_PADDED, "d": "kept", "e": BODY1})
    assert BODY0 not in text and SHORT_PADDED not in text, text
    assert '"kept"' in text
    assert BODY1 in text, "the run ended at \"kept\": what follows is not the key's"
    assert count == 1
    # A non-body key is neutral; a non-body VALUE ends the run.
    text, count = route([{"a": PEM_BEGIN}, {"after the key": BODY0}, {"x": "ok"}, {"y": BODY1}])
    assert BODY0 not in text and '"after the key"' in text and '"ok"' in text, text
    assert BODY1 in text
    assert count == 1


def test_the_run_stops_at_pem_block_max_chars():
    # (h) the bound a text block has: past it, a body-shaped string is not the key's.
    lines = [_body(10 + n) for n in range(PEM_BLOCK_MAX_CHARS // 64 + 16)]
    tail = _body(9)
    document = {"k": [{"l": PEM_BEGIN}] + [{"l": line} for line in lines], "tail": tail}
    for route in ROUTES:
        text, count = route(document)
        assert lines[0] not in text and lines[100] not in text, route.__name__
        assert tail in text, route.__name__
        assert count == 1


@pytest.mark.parametrize("route", ROUTES)
def test_end_in_reach_and_flat_list_behaviour_is_unchanged(route):
    # (i) END in reach in a flat list: masked through it, "after" served.
    text, count = route({"key_lines": [PEM_BEGIN, BODY0, BODY1, PEM_END, "after"]})
    _assert_masked(text, '"after"')
    assert PEM_END not in text
    assert count == 1
    # A flat list with no END: masked to the end of its run of strings.
    text, count = route({"k": [PEM_BEGIN, BODY0, "hello there"], "x": "plain words"})
    _assert_masked(text, '"plain words"')
    assert "hello there" not in text
    assert count == 1
    # A key in one string with no END: masked to the string's end, and "kept" served.
    text, count = route({"content": PEM_BEGIN + "\n" + BODY0 + "\n", "after": "kept"})
    _assert_masked(text, '"kept"')
    assert count == 1
    # An END across containers ends the run: what follows is served.
    text, count = route([{"a": PEM_BEGIN}, {"b": BODY0}, {"c": PEM_END}, {"d": BODY1}])
    assert BODY0 not in text and PEM_END not in text and BODY1 in text, text
    assert count == 1


def test_one_masker_masks_the_run_in_every_block_drawn_from_it():
    """`/input` draws `rest` without the prompt: a body line there is still the key's."""
    document = {"prompt": PEM_BEGIN, "b": BODY0, "c": BODY1}
    masker = JsonMasker(document)
    rest = masker.json({"b": BODY0, "c": BODY1})
    assert BODY0 not in rest.text and BODY1 not in rest.text, rest.text
    assert rest.count == 0
    assert masker.json(document).count == masker.text(PEM_BEGIN).count + rest.count


def _a_task(db, input_doc: dict[str, Any]) -> None:
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", runner_profile="claude-code")
    db.docs["tasks/task_a"]["input"] = input_doc


@pytest.mark.parametrize(
    "input_doc",
    [
        {"prompt": "rotate it", "key": {"a": PEM_BEGIN, "b": BODY0, "c": BODY1}, "after": "after the key"},
        {"prompt": "rotate it", "env": {"PRIVATE_KEY": PEM_BEGIN, BODY0: "", BODY1: "", PEM_END: ""}},
    ],
    ids=["object-members", "object-keys"],
)
def test_input_and_logs_mask_a_split_no_end_key(client, db, input_doc):
    _a_task(db, input_doc)
    response = client.get("/v1/tasks/task_a/input", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    for line in (BODY0, BODY1):
        assert line not in response.text, response.text
    body = response.json()
    full = json.loads(body["full"]["text"])
    rest = json.loads(body["rest"]["text"])
    assert full["prompt"] == "rotate it" and "prompt" not in rest
    assert body["full"]["redaction_count"] == 1
    if "after" in input_doc:
        assert full["after"] == "after the key"
    # /logs: the same document written as one line of agent output.
    text, count = _masked_log(input_doc)
    for line in (BODY0, BODY1):
        assert line not in text, text
    assert count == 1


# --------------------------------------------------------------------------
# The artifact token path (`json_masking`): a window, a page, both routes
# --------------------------------------------------------------------------


def _no_end_layouts() -> dict[str, Any]:
    """Each split layout the issue names, with no END and an ordinary string after."""
    return {
        "object": {"a": PEM_BEGIN, "b": BODY0, "c": BODY1, "after": "after the key"},
        "list-of-objects": {
            "k": [{"line": PEM_BEGIN}, {"line": BODY0}, {"line": BODY1}],
            "after": "kept",
        },
        "nested-lists": {"k": [[PEM_BEGIN], [BODY0], [BODY1]], "after": "kept"},
        "list-with-a-number": {"k": [PEM_BEGIN, 1, BODY0, BODY1], "after": "kept"},
        "keys-as-body": {"env": {"KEY": PEM_BEGIN, BODY0: "", BODY1: "", "after": "kept"}},
    }


@pytest.mark.parametrize("indent", [2, None], ids=["pretty", "one-line"])
@pytest.mark.parametrize("layout", list(_no_end_layouts()))
def test_a_window_masks_a_no_end_key_in_every_split_layout(layout, indent):
    from swarm_api import json_masking

    document = _no_end_layouts()[layout]
    got = json_masking.redact_json_window(json.dumps(document, indent=indent) + "\n")
    _assert_masked(got.text)
    parsed = json.loads(got.text)  # still JSON
    served = json.dumps(parsed)
    assert '"after the key"' in served or '"kept"' in served, got.text
    assert got.count == 1, "one key, one count: its body lines count 0"


def _a_finished_task(db, objects, name: str, body: str) -> None:
    """A terminal task with one artifact (`test_json_artifact_masking.py`'s shape)."""
    from datetime import datetime, timezone

    from .conftest import PROJECT

    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    doc["metadata"] = {}
    key = f"tenants/eng/tasks/task_a/attempts/att_1/artifacts/{name}"
    data = body.encode("utf-8")
    objects.put(key, data)
    doc["result_summary"] = {
        "artifacts": [{"name": name, "bytes": len(data), "uri": f"gs://swarm-artifacts-{PROJECT}/{key}"}],
        "artifact_bytes": len(data),
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


def _both_routes(client, name: str) -> dict[str, str]:
    """The whole artifact as the content route and the raw download serve it."""
    body = _content(client, name)
    assert body["truncated"] is False, "the fixture must fit one content window"
    response = client.get(
        "/v1/tasks/task_a/artifacts/raw", params={"name": name}, headers=auth_header("alice")
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-swarm-redaction"] == "applied"
    return {"content": body["content"], "raw": response.text}


def _pages(client, name: str, *, limit_bytes: int = 4096) -> list[dict[str, Any]]:
    """Every content window in order (the loop of `test_a_page_inside_a_masked_value_reports_it_redacted`)."""
    pages: list[dict[str, Any]] = []
    offset = 0
    while True:
        body = _content(client, name, offset=offset, limit_bytes=limit_bytes)
        pages.append(body)
        following = body.get("next_offset")
        if not body["truncated"] or not isinstance(following, int) or following <= offset:
            return pages
        offset = following


#: Enough 64-character lines that a key spans three 4096-byte pages.
LONG_BODY = [_body(100 + n) for n in range(160)]


def _paged_transcript(layout: str) -> str:
    """A pretty-printed transcript holding one long key, END present, then an ordinary string."""
    lines = [PEM_BEGIN, *LONG_BODY, PEM_END]
    if layout == "object":
        key: Any = {f"l{n:03d}": line for n, line in enumerate(lines)}
    else:
        key = [{"line": line} for line in lines]
    events = [{"type": "text", "text": "before"}, {"type": "tool_result", "key": key}]
    events.append({"type": "text", "text": "after the key"})
    return json.dumps({"type": "transcript", "events": events}, indent=2) + "\n"


def _key_pages(client, db, objects, layout: str) -> tuple[str, list[dict[str, Any]]]:
    stored = _paged_transcript(layout)
    _a_finished_task(db, objects, "claude-transcript.json", stored)
    pages = _pages(client, "claude-transcript.json")
    for page in pages:
        for line in LONG_BODY:
            assert line not in page["content"], (page["offset"], line)
        assert PEM_END not in page["content"], page["offset"]
    assert "".join(p["content"] for p in pages).count("after the key") == 1, "served after the key"
    return stored, pages


@pytest.mark.parametrize("layout", ["object", "list-of-objects"])
def test_a_page_holding_begin_but_not_end_masks_its_body_lines(client, db, objects, layout):
    stored, pages = _key_pages(client, db, objects, layout)
    begin_at, end_at = stored.index(PEM_BEGIN), stored.index(PEM_END)
    opening = [
        p for p in pages if p["offset"] <= begin_at < p["offset"] + p["returned_bytes"] <= end_at
    ]
    assert len(opening) == 1, "control: one page holds the BEGIN and not the END"
    page = opening[0]
    assert PEM_BEGIN in page["content"], "the BEGIN marker is kept"
    assert page["content"].count(json.dumps(MASK)) >= 8, "control: the page holds body lines"
    assert page["redaction_count"] == 1, "one key, one count"


@pytest.mark.parametrize("layout", ["object", "list-of-objects"])
def test_the_middle_page_of_a_three_page_key_masks_its_lines_and_says_so(client, db, objects, layout):
    stored, pages = _key_pages(client, db, objects, layout)
    begin_at, end_at = stored.index(PEM_BEGIN), stored.index(PEM_END)
    middle = [p for p in pages if begin_at < p["offset"] and p["offset"] + p["returned_bytes"] <= end_at]
    assert middle, "control: a page lies wholly inside the key"
    for page in middle:
        assert page["redacted"] is True, page["offset"]
        assert page["redaction_count"] == 1, "it withheld one key's lines: counted once"
        assert PEM_BEGIN not in page["content"], "control: neither marker is on the page"
    closing = [p for p in pages if p["offset"] <= end_at < p["offset"] + p["returned_bytes"]]
    assert len(closing) == 1 and closing[0]["offset"] > begin_at, "control: END on its own page"
    assert closing[0]["redacted"] is True
    assert closing[0]["redaction_count"] >= 1


def test_body_lines_stored_as_keys_are_masked_through_both_routes(client, db, objects):
    # A PEM pasted into a .properties file, then converted to JSON: with and without END.
    with_end = {"PRIVATE_KEY": PEM_BEGIN, BODY0: "", BODY1: "", PEM_END: ""}
    no_end = {"KEY": PEM_BEGIN, BODY0: "", BODY1: "", "after": "kept"}
    stored = json.dumps({"type": "transcript", "env": [with_end, no_end]}, indent=2) + "\n"
    _a_finished_task(db, objects, "claude-transcript.json", stored)
    for route, served in _both_routes(client, "claude-transcript.json").items():
        for line in (BODY0, BODY1, PEM_END):
            assert line not in served, (route, line)
        parsed = json.loads(served)
        assert parsed["env"][1]["after"] == "kept", route


def test_the_issue_repro_is_masked_through_both_routes(client, db, objects):
    # #385's reproduction: BEGIN in one member, the body in the next ones, no END.
    stored = json.dumps({"a": PEM_BEGIN, "b": BODY0, "c": BODY1, "after": "after the key"}) + "\n"
    _a_finished_task(db, objects, "out.json", stored)
    for route, served in _both_routes(client, "out.json").items():
        assert BODY0 not in served and BODY1 not in served, (route, served)
        parsed = json.loads(served)
        assert parsed["after"] == "after the key" and parsed["a"].startswith(PEM_BEGIN[:16]), route


@pytest.mark.parametrize(
    "window",
    [
        '{"secret": [1, 2, ], "k": 1}\n{"a":1}\n',
        '{"token": [1 2], "k": 1}\n',
        '{"token": {1}, "k": 1}\n',
        '{"token": {,,1}, "k": 1}\n',
        '{"token": {"a": 1, 2}, "k": 1}\n',
        '[{"token": {1}}]\n{"a":1}\n',
    ],
    ids=[
        "trailing-comma",
        "missing-comma",
        "member-with-no-key",
        "keyless-after-stray-commas",
        "keyless-after-a-member",
        "keyless-in-a-list",
    ],
)
def test_a_fragment_masks_a_malformed_value_under_a_credential_name(window):
    """A fragment page is scanned tolerantly, so a value masked whole need not
    be JSON: it is masked and the page served, not a 500 (the #385 review)."""
    from swarm_api import json_masking

    got = json_masking.redact_json_window(window, fragment=True)
    assert "[1" not in got.text, got.text
    assert MASK in got.text
    assert got.count == 1


def test_a_begin_in_a_malformed_masked_value_still_opens_the_run():
    """The value is rebuilt from the scanner's tokens when it does not decode,
    and read as a decoded one is: a BEGIN inside it opens the run (the flat
    list carrying it past `"x"`), and the body after it is masked."""
    from swarm_api import json_masking

    document = {"secret": [PEM_BEGIN, "x"], "k": BODY0, "after": "kept"}
    window = json.dumps(document).replace('"x"]', '"x", ]') + "\n"
    assert window != json.dumps(document) + "\n"
    got = json_masking.redact_json_window(window, fragment=True)
    assert BODY0 not in got.text, got.text
    assert '"kept"' in got.text, got.text


@pytest.mark.parametrize(
    "inner",
    ["{\"a\": %s, 2}", "{\"a\": %s, {}}", "{\"a\": %s \"\"}"],
    ids=["keyless-number-after-it", "keyless-object-after-it", "blank-value-after-it"],
)
def test_a_begin_beside_a_keyless_member_still_opens_the_run(inner):
    """A member written with no key is filed under a stand-in key of its own
    (`json_masking._keyless`), so it neither crashes the scan (`None` read as
    a string: a 500) nor erases the value before it -- the BEGIN that opens
    the run, so the body after it was served (the #385 leftover)."""
    from swarm_api import json_masking

    window = '{"secret": ' + inner % json.dumps(PEM_BEGIN) + ', "k": "' + BODY0 + '", "after": "kept"}\n'
    got = json_masking.redact_json_window(window, fragment=True)
    assert BODY0 not in got.text, got.text
    assert PEM_BEGIN not in got.text, got.text
    assert '"kept"' in got.text, got.text
