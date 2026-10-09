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
