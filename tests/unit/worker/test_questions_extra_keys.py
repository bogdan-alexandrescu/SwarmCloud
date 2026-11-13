"""A well-formed question that carries a key the validator does not know is taken.

Issue #3086, 2026-11-12. A build step wrote one well-formed question -- options,
`recommended`, `built` -- plus one field of its own. The validator rejected the
WHOLE file for that field, so the worker recorded `questions: 0` with
`questions_rejected`, swarm-api said the task asked nothing, the review could
not cite the question and the merge parked on it with no answer able to clear
it. Now an unknown key on a question or an option is dropped from what
`validate` returns, and the summary says where (`questions_ignored_keys`,
positions only, never the key: it is the agent's text). Every other rule is as
strict as it was.

The keys are spelled out rather than imported: they are fields of a document
that outlives the process that wrote it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_worker import questions

NAME = "questions.json"
COUNT_KEY = "questions"
REJECTED_KEY = "questions_rejected"
IGNORED_KEY = "questions_ignored_keys"
DIGESTS_KEY = "question_digests"
TEXT_DIGESTS_KEY = "question_text_digests"


def _question(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "question": "Should the export be one file or one file per tenant?",
        "options": [
            {"label": "one file", "description": "A single export for the deployment."},
            {"label": "per tenant", "description": "One export under each tenant's prefix."},
        ],
        "recommended": "per tenant",
        "context": "Each tenant owns its own prefix.",
        "built": "per tenant",
    }
    entry.update(overrides)
    return entry


def _with_extras() -> list[dict[str, Any]]:
    """Question 1 carries an extra key; question 2's second option carries one."""
    first = _question(severity="high")
    second = _question(question="Keep the old route for one release?", built=None)
    second["options"][1]["note"] = "drawn by hand"
    return [first, second]


def _bytes(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")


def _write(tmp_path: Path, value: Any) -> Path:
    folder = tmp_path / "artifacts"
    folder.mkdir()
    (folder / NAME).write_bytes(_bytes(value))
    return folder


def test_an_extra_key_is_dropped_and_the_question_is_taken():
    taken = questions.validate(_bytes(_with_extras()))

    # MUTATION: reject on an unknown key (the #3086 bug) and this raises.
    assert len(taken) == 2
    # MUTATION: return the entries as written and a reader sees the agent's key.
    assert "severity" not in taken[0]
    assert "note" not in taken[1]["options"][1]
    # The rows are exactly the same questions without the extra keys.
    clean = [_question(), _question(question="Keep the old route for one release?", built=None)]
    assert taken == clean


def test_the_digests_are_those_of_the_same_question_without_the_key(tmp_path: Path):
    clean = [_question(), _question(question="Keep the old route for one release?", built=None)]
    got = questions.summarise(_write(tmp_path, _with_extras()), uploaded={NAME})

    assert got[COUNT_KEY] == 2, got
    assert REJECTED_KEY not in got, got
    # An answer stamped with this digest matches the question as every reader
    # serves it, extra key or none.
    assert got[DIGESTS_KEY] == [questions.digest(q) for q in clean]
    assert got[TEXT_DIGESTS_KEY] == [questions.text_digest(q) for q in clean]


def test_the_summary_names_the_positions_that_carried_an_extra_key(tmp_path: Path):
    got = questions.summarise(_write(tmp_path, _with_extras()), uploaded={NAME})

    # MUTATION: drop silently and the operator cannot tell the file was trimmed.
    assert got[IGNORED_KEY] == ["question 1", "question 2: option 2"], got
    # MUTATION: quote the key and the agent's text reaches the summary.
    assert "severity" not in json.dumps(got) and "drawn by hand" not in json.dumps(got)


def test_a_file_with_no_extra_key_says_nothing_of_ignored_keys(tmp_path: Path):
    got = questions.summarise(_write(tmp_path, [_question()]), uploaded={NAME})

    assert got[COUNT_KEY] == 1
    # MUTATION: write the key unconditionally and every summary grows an empty list.
    assert IGNORED_KEY not in got


def test_validate_does_not_change_the_list_it_was_given():
    # The file's own entries are not trimmed in place: a caller that keeps
    # the parsed file (the question-wait record) is handed new dicts.
    value = _with_extras()
    data = _bytes(value)
    questions.validate(data)
    assert json.loads(data) == value


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (_bytes([_question(severity="high", recommended="neither")]), "question 1: `recommended`"),
        (_bytes([_question(severity="high", built="neither")]), "question 1: `built`"),
        (_bytes({"question": "one, not a list", "severity": "high"}), "not a JSON list"),
        (_bytes([_question(severity="high", options="one file or per tenant")]),
         "question 1: `options`"),
        (_bytes([_question(severity="high", options=[])]), "question 1: `options` holds 0"),
        (_bytes([_question(severity="high", question=7)]), "question 1: `question`"),
        (_bytes([_question(severity="x" * questions.MAX_BYTES)]), "larger than"),
        (_bytes([_question(severity="high")] * (questions.MAX_QUESTIONS + 1)), "more than"),
        (_bytes([_question(options=[{"label": "a", "description": "x", "extra": 1},
                                    {"label": "a", "description": "y"}])]),
         "two options share a label"),
        (b"[{\"question\": ", "not JSON"),
    ],
    ids=["recommended-not-an-option", "built-not-an-option", "not-a-list", "options-not-a-list",
         "no-options", "question-not-text", "oversize", "too-many", "duplicate-labels",
         "not-json"],
)
def test_every_other_rule_still_rejects_the_file(tmp_path: Path, content: bytes, reason: str):
    with pytest.raises(questions.Rejected) as caught:
        questions.validate(content)
    assert reason in str(caught.value), str(caught.value)

    folder = tmp_path / "artifacts"
    folder.mkdir()
    (folder / NAME).write_bytes(content)
    got = questions.summarise(folder, uploaded={NAME})
    assert got[COUNT_KEY] == 0
    assert reason in got[REJECTED_KEY]
    assert IGNORED_KEY not in got


def test_swarm_api_serves_the_raw_file_with_known_keys_only():
    """swarm-api reads the uploaded file itself, not what `validate` returned:
    its `_question` picks the known keys, and every question is still in the
    list, so its `len(parsed) != count` check holds for a file with extra keys."""
    from swarm_api import agent_questions

    raw = json.loads(_bytes(_with_extras()))
    served = [agent_questions._question(entry) for entry in raw]

    assert len(raw) == len(questions.validate(_bytes(_with_extras())))
    assert "severity" not in served[0]
    assert set(served[0]) == {"question", "options", "recommended", "context"}
    assert served[0]["options"][0] == {"label": "one file",
                                       "description": "A single export for the deployment."}
