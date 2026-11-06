"""A text-only key per question (the follow-up BGD's overnight grant picked on 6 November 2026).

An implement step and its fix step often ask the same question with other
option labels. `question_digests` hash the text and the options, so they tell
the two apart; the worker also writes `result_summary.question_text_digests`,
one digest per counted question of its normalized words alone (case and
spacing aside, as the bell's `_question_key` and the console's `questionKey`
read them), and swarm-api's bell and the Workflows list count the two once.

MUTATIONS: hash the options into the text key; stop normalizing case or
spacing; write the key for a rejected or missing file; let swarm-api's digest
of a question's words drift from the worker's.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_worker import questions

TEXT_DIGESTS_KEY = "question_text_digests"


def _write(folder: Path, entries: list[dict]) -> Path:
    (folder / questions.NAME).write_text(json.dumps(entries), encoding="utf-8")
    return folder


def test_one_text_key_per_question_its_words_alone(tmp_path: Path):
    folder = _write(tmp_path, [
        {"question": "Which database  should it use?", "options": [{"label": "Postgres"}]},
        {"question": "which database should it use?", "options": [{"label": "SQLite (small)"}]},
        {"question": "Which region?", "options": [{"label": "eu"}]},
    ])
    summary = questions.summarise(folder, uploaded={questions.NAME})
    keys = summary[TEXT_DIGESTS_KEY]
    assert len(keys) == 3
    # Other options, other case and spacing: the same words, one key.
    assert keys[0] == keys[1] != keys[2]
    # The full digests still tell them apart.
    assert summary["question_digests"][0] != summary["question_digests"][1]
    assert all(len(k) == questions.DIGEST_CHARS and int(k, 16) >= 0 for k in keys)


def test_no_file_and_a_rejected_file_write_no_text_keys(tmp_path: Path):
    assert TEXT_DIGESTS_KEY not in questions.summarise(tmp_path, uploaded={questions.NAME})
    (tmp_path / questions.NAME).write_text("{}", encoding="utf-8")
    assert TEXT_DIGESTS_KEY not in questions.summarise(tmp_path, uploaded={questions.NAME})


def test_swarm_api_keys_a_questions_words_as_the_worker_does():
    from swarm_api.notifications import question_text_digest

    for text in ("Ship it?", "  Which   Region?\n", "Ünïcode — ok?"):
        assert question_text_digest(text) == questions.text_digest({"question": text})
    assert question_text_digest("   ") is None
