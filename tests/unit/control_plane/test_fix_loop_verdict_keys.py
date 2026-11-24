"""A fix loop reads a verdict whose masks sit only in keys it does not read (issue 338).

docs/design/fix-loop.md §4.4 and §4.5. The loop reads a review's
`verdict.json` through the API's own tenant-checked, redacted artifact read
(invariant 9). Before issue 338 it declined the whole file when the redaction
masked anything in it, even prose in keys the loop never reads (`checks`,
`needs_owner`, `open_questions`, `minors`), and parked a clean MERGE
`review_unreadable`. What is held here, through the real read path:

  1. the issue's exact file is read as MERGE: extra keys alone never decline;
  2. a MERGE whose extra keys hold masked prose ends the loop SUCCEEDED;
  3. a NOT_YET whose `open_questions` hold masked prose goes on to its next
     round with its findings unchanged;
  4. masked `findings` still park `review_unreadable`, and the park names the
     code `credential` and the key `findings`, never a value;
  5. an unreadable or absent file parks with its code, and round 0's park
     carries the head;
  6. the same holds for a `re-review:1` verdict;
  7. `verdictpublish._read_text` without `allow_redacted` still declines any
     mask: the publish path, which writes text to a forge, is unchanged.

No credentials, no network, no emulator. Every credential-looking value is
built at runtime.
"""

from __future__ import annotations

import json
import secrets
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_api import fixloop, verdictpublish
from swarm_api.verdictpublish import Decline

from .test_fix_loop_drive import (  # noqa: F401 -- fixtures, used by name
    FIX_HEAD, NOT_YET, _members_hold_grants, clock, finish, github,
    integrate_to_first_round, loop_client, loop_enabled, push, round_task,
)

#: The verdict file both live loops held (issue 338, "Steps to reproduce").
ISSUE_FILE = {"verdict": "MERGE", "findings": [], "minors": [], "checks": [],
              "needs_owner": [], "open_questions": []}


def _secret() -> str:
    return secrets.token_hex(16)


def masked_prose() -> dict[str, list[str]]:
    """Prose the redaction masks (`json_masking.redact_json_window`), by key."""
    return {
        "checks": [f"token: {_secret()}"],
        "needs_owner": [f"password = '{_secret()}'"],
        "open_questions": [f"Authorization: Bearer {_secret()}"],
    }


def _all_values(detail: Any) -> str:
    return json.dumps(detail, default=str)


def test_the_issues_exact_file_ends_the_loop_merge_with_no_round(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects, review=ISSUE_FILE)

    doc = loop.doc()
    assert doc["state"] == "SUCCEEDED", fixloop.park_detail_of(doc)
    assert doc["result_summary"]["fix_loop"]["verdict"] == "MERGE"
    assert loop.round_steps() == {}


def test_a_merge_with_masked_prose_in_extra_keys_ends_the_loop_succeeded(
        loop_client, db, objects):
    prose = masked_prose()
    file = {**ISSUE_FILE, **prose, "minors": [f"api_key: {_secret()}"]}

    loop = integrate_to_first_round(loop_client, db, objects, review=file)

    doc = loop.doc()
    assert fixloop.park_phase_of(doc) is None, fixloop.park_detail_of(doc)
    assert doc["state"] == "SUCCEEDED"
    assert doc["result_summary"]["fix_loop"]["verdict"] == "MERGE"
    assert loop.round_steps() == {}


def test_a_not_yet_with_masked_open_questions_goes_on_with_its_findings(
        loop_client, db, objects):
    file = {**NOT_YET, "open_questions": masked_prose()["open_questions"]}

    loop = integrate_to_first_round(loop_client, db, objects, review=file)

    steps = loop.round_steps()
    assert list(steps) == ["re-review:1"], fixloop.park_detail_of(loop.doc())
    review = loop.doc(task_id=steps["re-review:1"]["task_id"])
    assert review["repository_ref"] == FIX_HEAD
    prompt = review["input"]["prompt"]
    assert "the cache never expires entries" in prompt
    assert "the expiry test asserts nothing" in prompt


def test_masked_findings_park_review_unreadable_naming_the_key(loop_client, db, objects):
    value = _secret()
    file = {"verdict": "NOT_YET", "findings": [
        {"summary": f"the config logs password = '{value}'", "severity": "blocker"}]}

    loop = integrate_to_first_round(loop_client, db, objects, review=file)

    doc = loop.doc()
    assert doc["state"] == "PARKED"
    assert fixloop.park_phase_of(doc) == "review_unreadable"
    detail = fixloop.park_detail_of(doc)
    assert detail["code"] == "credential"
    assert list(detail["keys"]) == ["findings"]
    assert "findings" in detail["error"]
    assert detail["round"] == 0 and detail["head"] == FIX_HEAD
    text = _all_values(detail)
    assert value not in text and "********" not in text
    assert loop.round_steps() == {}


@pytest.mark.parametrize("verdict, code", [
    (json.dumps({"verdict": "MAYBE", "findings": []}), "verdict_unreadable"),
    ("not json", "verdict_unreadable"),
    (None, "verdict_absent"),
])
def test_an_unreadable_first_verdict_parks_with_its_code_and_head(
        loop_client, db, objects, verdict, code):
    loop = integrate_to_first_round(loop_client, db, objects, review=verdict)

    doc = loop.doc()
    assert fixloop.park_phase_of(doc) == "review_unreadable"
    detail = fixloop.park_detail_of(doc)
    assert detail["code"] == code
    assert detail["round"] == 0
    assert detail["head"] == FIX_HEAD
    assert isinstance(detail["error"], str) and detail["error"]


def test_a_re_reviews_masked_extra_keys_are_read_and_end_the_loop(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict={**ISSUE_FILE, **masked_prose()})

    push(loop_client, rereview)

    doc = loop.doc()
    assert fixloop.park_phase_of(doc) is None, fixloop.park_detail_of(doc)
    assert doc["state"] == "SUCCEEDED"
    assert doc["result_summary"]["fix_loop"]["verdict"] == "MERGE"


def test_a_re_reviews_masked_findings_park_with_the_key(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict={"verdict": "NOT_YET", "findings": [
        f"the handler logs token: {_secret()}"]})

    push(loop_client, rereview)

    detail = fixloop.park_detail_of(loop.doc())
    assert fixloop.park_phase_of(loop.doc()) == "review_unreadable"
    assert detail["code"] == "credential" and list(detail["keys"]) == ["findings"]
    assert detail["head"] == FIX_HEAD


def _ctx(row: dict[str, Any]) -> Any:
    def read_artifact(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return row

    return SimpleNamespace(inspection=SimpleNamespace(read_artifact=read_artifact))


def test_read_text_without_the_flag_still_declines_any_mask():
    content = json.dumps({**ISSUE_FILE, "checks": ["token: ********"]})
    row = {"status": "ok", "total_bytes": len(content), "truncated": False,
           "invalid_utf8_bytes": 0, "redacted": True, "content": content}

    with pytest.raises(Decline) as caught:
        verdictpublish._read_text(_ctx(row), "eng", "task-1", "verdict.json",
                                  limit=verdictpublish.MAX_VERDICT_BYTES, required=True,
                                  what="verdict")
    assert caught.value.code == "credential"

    # The fix loop's flag returns the masked text: it never saw the raw bytes.
    text = verdictpublish._read_text(_ctx(row), "eng", "task-1", "verdict.json",
                                     limit=verdictpublish.MAX_VERDICT_BYTES, required=True,
                                     what="verdict", allow_redacted=True)
    assert text == content
