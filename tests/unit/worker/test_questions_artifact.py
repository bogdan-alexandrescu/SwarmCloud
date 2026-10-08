"""`questions.json`: a remote agent asks the owner instead of guessing (lane OQ).

Owner decision, 2026-10-05. Until this, a remote agent that met a decision
that was the owner's to make wrote it into its answer and shipped `part of #N`
(I310, W532): the question was in prose, somewhere in an answer a workflow row
cuts to 500 characters. Now an agent may write `questions.json` into
`$SWARM_ARTIFACTS_DIR`: a JSON list of `{question, options: [{label,
description}], recommended, context}`.

What each test holds:

* a valid file is uploaded like any artifact and counted as
  `result_summary.questions`;
* an invalid one -- wrong shape, too large, not JSON -- is NOT counted, the
  summary says why in `questions_rejected`, and the task still finishes as it
  would have;
* no file is `questions: 0`;
* the prompt the agent gets says, in one sentence, that it may ask.

The file is DATA FOR THE OPERATOR. Nothing in the worker acts on it: the task's
state is the same with or without it.

The keys are spelled out rather than imported: they are fields of a document
that outlives the process that wrote it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore

from test_standalone_outputs import (  # noqa: F401 - agent_cli is a fixture
    _agent_output,
    _names,
    _object,
    _run,
    _seed,
    _succeeded,
    _summary,
    agent_cli,
)

#: The file's name in `$SWARM_ARTIFACTS_DIR`.
NAME = "questions.json"

#: `result_summary` keys: how many questions, and why a file was not taken.
COUNT_KEY = "questions"
REJECTED_KEY = "questions_rejected"

#: The file's size cap, in bytes.
MAX_BYTES = 64 * 1024


def _question(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "question": "Should the retry budget be per task or per workflow?",
        "options": [
            {"label": "per task", "description": "Each step keeps its own three attempts."},
            {"label": "per workflow", "description": "One budget shared by every step."},
        ],
        "recommended": "per task",
        "context": "CONTRACT.md fixes three attempts per task; a shared budget would change it.",
    }
    entry.update(overrides)
    return entry


VALID = [
    _question(),
    _question(question="Rename the tab now or in the next wave?", recommended=None, context=None),
]


# ---------------------------------------------------------------------------
# through the production worker and the production claude-code runner
# ---------------------------------------------------------------------------


def test_a_valid_file_is_uploaded_and_counted(db, store: LocalObjectStore, worker_factory, agent_cli):
    text = json.dumps(VALID)
    _seed(db, {"write_artifact": {NAME: text}})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)

    summary = _summary(db)
    assert summary[COUNT_KEY] == 2, summary
    assert REJECTED_KEY not in summary, summary
    assert NAME in _names(db)
    assert json.loads(_object(store, NAME).decode("utf-8")) == VALID


def test_no_file_is_zero_questions(db, worker_factory, agent_cli):
    _seed(db, {"write_artifact": {"answer.md": "done\n"}})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)

    summary = _summary(db)
    assert summary[COUNT_KEY] == 0, summary
    assert REJECTED_KEY not in summary, summary


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("this is not json", "not JSON"),
        (json.dumps({"question": "one, not a list"}), "not a JSON list"),
        (json.dumps([_question(options="per task or per workflow")]), "question 1: `options`"),
        (json.dumps([_question(), _question(recommended="neither")]), "question 2: `recommended`"),
        (json.dumps([_question(run="rm -rf /")]), "question 1: a key"),
        (json.dumps([_question(context="x" * (MAX_BYTES + 1))]), "larger than"),
    ],
    ids=["not-json", "not-a-list", "options-not-a-list", "recommended-not-an-option",
         "unknown-key", "too-large"],
)
def test_an_invalid_file_is_rejected_with_a_reason_and_the_task_still_finishes(
    db, worker_factory, agent_cli, content, reason
):
    _seed(db, {"write_artifact": {NAME: content}})

    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)

    summary = _summary(db)
    assert summary[COUNT_KEY] == 0, summary
    assert reason in summary[REJECTED_KEY], summary[REJECTED_KEY]


def test_the_prompt_says_once_that_the_agent_may_ask(db, worker_factory, agent_cli):
    _seed(db, {"echo_prompt": True})

    assert _run(worker_factory) == ExitCode.OK
    prompt = _agent_output(db)["prompt"]
    lines = [line for line in prompt.splitlines() if NAME in line]
    assert len(lines) == 1, prompt
    assert "owner" in lines[0] and "guess" in lines[0], lines[0]
    # It follows the deliverables line and does not name the folder again:
    # "there" is the folder that line named.
    assert prompt.count("$SWARM_ARTIFACTS_DIR") == 1, prompt


# ---------------------------------------------------------------------------
# the validator alone
# ---------------------------------------------------------------------------


def test_the_validator_takes_a_valid_list_and_an_empty_one():
    from agent_worker import questions

    assert questions.validate(json.dumps(VALID).encode("utf-8")) == VALID
    assert questions.validate(b"[]") == []


def test_the_validator_refuses_deep_nesting_without_raising_anything_else():
    from agent_worker import questions

    with pytest.raises(questions.Rejected):
        questions.validate(b"[" * 50_000 + b"]" * 50_000)


def test_a_link_named_questions_json_is_not_followed(tmp_path: Path):
    from agent_worker import questions

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(VALID))
    folder = tmp_path / "artifacts"
    folder.mkdir()
    os.symlink(outside, folder / NAME)

    got = questions.summarise(folder, uploaded={NAME})
    assert got[COUNT_KEY] == 0
    assert "not a regular file" in got[REJECTED_KEY]


def test_a_file_that_was_not_uploaded_is_not_counted(tmp_path: Path):
    from agent_worker import questions

    folder = tmp_path / "artifacts"
    folder.mkdir()
    (folder / NAME).write_text(json.dumps(VALID))

    because = "written but not uploaded: over the artifact cap (cap)"
    got = questions.summarise(folder, uploaded=set(), cause=because)
    assert got == {COUNT_KEY: 0, REJECTED_KEY: because}
