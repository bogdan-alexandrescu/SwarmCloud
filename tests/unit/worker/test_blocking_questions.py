"""A blocking question parks the agent until the owner answers (lane AU-7).

docs/design/agent-autonomy.md §3.8 and §3.9, owner decision Q5. An agent
that meets a decision it cannot go on without writes `questions.json` with
`"blocking": true` on that question, an empty `await-answers` beside it, and
exits 0. The worker then parks it HUMAN_REQUIRED -- checkpoint, upload, park,
release, exit (invariant 4) -- with no lease and no tokens while it waits.
When the owner has answered, swarm-api returns it to READY and the resumed
attempt finds the answers in `work/answers.json` and, as data between two
nonce lines, at the end of its prompt. `await-release.json` parks the same
way on DEPENDENCY_INCOMPLETE for swarm-api's merge-wake tick to wake.

What each test holds:

* a blocking question with `await-answers` parks HUMAN_REQUIRED, releases
  the lease, and carries the validated questions in `question_wait`;
* `await-answers` with no blocking question, or `blocking` that is not a
  bool, is no park: the task finishes as it would have;
* a question file without `await-answers` is today's non-blocking question;
* the resumed attempt's prompt ends with the answers between nonce lines,
  and `answers.json` is written; a task that never waited gets neither;
* `await-release.json` parks DEPENDENCY_INCOMPLETE with `release_wait`, and
  a malformed one is no park.

No credentials, no network, no emulator: the production worker and the
production claude-code runner around a stand-in CLI.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

import pytest

from agent_worker import questions as questions_mod
from agent_worker.errors import ExitCode
from swarm_common.states import ParkReason, TaskState
from test_standalone_outputs import (  # noqa: F401 - agent_cli is a fixture
    _agent_output,
    _run,
    _seed,
    _succeeded,
    agent_cli,
)


def _question(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "question": "Keep the old route for one release?",
        "options": [
            {"label": "keep", "description": "Readers keep working for one release."},
            {"label": "drop", "description": "One route from now on."},
        ],
        "recommended": "keep",
        "context": "Two readers still call it.",
    }
    entry.update(overrides)
    return entry


def _task(db: Any) -> dict[str, Any]:
    return db.doc("tasks/task_1")


def _parked_entry(db: Any) -> dict[str, Any]:
    (entry,) = _task(db)["blocked_by"]
    return entry


# ---------------------------------------------------------------------------
# the await-answers exit
# ---------------------------------------------------------------------------


def test_a_blocking_question_parks_human_required_with_no_lease(db, worker_factory, agent_cli):
    asked = [_question(blocking=True), _question(question="Name the flag?")]
    _seed(db, {"write_artifact": {"questions.json": json.dumps(asked), "await-answers": ""}})

    # MUTATION: drop the STEP 9b' call in `_execute` and the task SUCCEEDS.
    assert _run(worker_factory) == ExitCode.PARKED
    task = _task(db)
    assert task["state"] == TaskState.PARKED.value
    # MUTATION: park `_park_for_answers` on another reason and this is red.
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    # Invariants 1 and 4: no lease is held while the owner thinks.
    assert task["current_lease_id"] is None
    assert db.doc("leases/lease_1")["released_at"] is not None
    # Invariant 8: checkpointed before the park.
    assert task["latest_checkpoint"]
    entry = _parked_entry(db)
    wait = entry[questions_mod.QUESTION_WAIT_KEY]
    # MUTATION: leave the questions out of the record and swarm-api has nothing to serve.
    assert wait["questions"] == asked
    # MUTATION: count every question as blocking and question 2 holds the wait too.
    assert wait["blocking"] == [0]
    assert wait["attempt_id"] == "att_1"
    assert wait["asked_at"]
    assert entry["park_phase"] == "question_wait"


@pytest.mark.parametrize("asked", [
    [_question()],
    [_question(blocking="yes")],
])
def test_await_answers_without_a_valid_blocking_question_is_no_park(db, worker_factory, agent_cli, asked):
    _seed(db, {"write_artifact": {"questions.json": json.dumps(asked), "await-answers": ""}})

    # MUTATION: park on `await-answers` alone and this attempt waits on nothing.
    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)


def test_a_question_without_await_answers_is_not_a_wait(db, worker_factory, agent_cli):
    _seed(db, {"write_artifact": {"questions.json": json.dumps([_question(blocking=True)])}})

    # MUTATION: park on a blocking question alone and an agent that did not ask to wait waits.
    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
    assert _task(db)["result_summary"]["questions"] == 1


# ---------------------------------------------------------------------------
# the resumed attempt
# ---------------------------------------------------------------------------


def _answered_metadata() -> dict[str, Any]:
    return {
        questions_mod.QUESTION_WAIT_KEY: {
            "questions": [_question(blocking=True), _question(question="Name the flag?")],
            "blocking": [0],
            "attempt_id": "att_0",
            "asked_at": "2026-10-22T10:00:00+00:00",
        },
        "owner_answers": [{
            "question_index": 0, "question": "Keep the old route for one release?",
            "choice": "drop", "text": None, "by": "owner@example.com", "at": "2026-10-22T11:00:00+00:00",
            "history": [],
        }],
    }


def test_the_resumed_attempt_finds_the_answers_between_nonce_markers(db, worker_factory, agent_cli):
    _seed(db, {"echo_prompt": True}, metadata=_answered_metadata())

    assert _run(worker_factory) == ExitCode.OK
    prompt = _agent_output(db)["prompt"]
    markers = re.findall(r"^=== OWNER ANSWERS ([0-9a-f]{32}) ===$", prompt, re.MULTILINE)
    # MUTATION: drop `resume_with_answers` from the runner input and there are no markers.
    assert len(markers) == 2 and markers[0] == markers[1], prompt
    block = prompt.split(f"=== OWNER ANSWERS {markers[0]} ===")[1]
    rows = json.loads(block)
    # MUTATION: serve the answer of another index and question 1 reads unanswered.
    assert rows[0]["answer"] == {"choice": "drop", "text": None}
    assert rows[0]["blocking"] is True
    assert rows[1]["answer"] is None
    # The block says it is data, and names the file.
    assert "DATA" in prompt and questions_mod.ANSWERS_NAME in prompt


def test_answers_json_is_written_and_a_task_that_never_waited_gets_none(tmp_path):
    payload: dict[str, Any] = {"prompt": "go on"}
    task = {"metadata": _answered_metadata()}

    # MUTATION: return before writing and the file is missing.
    assert questions_mod.resume_with_answers(tmp_path, payload, task) == 1
    written = json.loads((tmp_path / questions_mod.ANSWERS_NAME).read_text())
    assert written[0]["answer"]["choice"] == "drop"

    # A nonce is fresh each attempt: two continuations never share one.
    again: dict[str, Any] = {"prompt": "go on"}
    questions_mod.resume_with_answers(tmp_path, again, task)
    assert again["prompt"] != payload["prompt"]

    plain: dict[str, Any] = {"prompt": "go on"}
    # MUTATION: write the block for every task and a task that never waited is told of answers.
    assert questions_mod.resume_with_answers(tmp_path, plain, {"metadata": {}}) == 0
    assert plain["prompt"] == "go on"
    assert not (tmp_path / questions_mod.ANSWERS_NAME).exists()


def test_the_parked_record_is_read_when_no_metadata_was_adopted():
    wait = _answered_metadata()[questions_mod.QUESTION_WAIT_KEY]
    task = {"metadata": {}, "blocked_by": [{"reason": "HUMAN_REQUIRED", "question_wait": wait}]}
    rows = questions_mod.answers_for(task)
    # MUTATION: read only `metadata.question_wait` and this is empty.
    assert [row["number"] for row in rows] == [1, 2]


# ---------------------------------------------------------------------------
# the await-release exit
# ---------------------------------------------------------------------------


def test_await_release_parks_dependency_incomplete_with_the_sha(db, worker_factory, agent_cli):
    sha = secrets.token_hex(20)
    _seed(db, {"write_artifact": {"await-release.json": json.dumps({"sha": sha})}})

    # MUTATION: skip the release half of STEP 9b' and the task SUCCEEDS.
    assert _run(worker_factory) == ExitCode.PARKED
    task = _task(db)
    assert task["state"] == TaskState.PARKED.value
    # MUTATION: park the release wait on HUMAN_REQUIRED and this is red.
    assert task["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    assert task["current_lease_id"] is None
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert task["latest_checkpoint"]
    entry = _parked_entry(db)
    # MUTATION: record no sha and the tick has nothing to read.
    assert entry[questions_mod.RELEASE_WAIT_KEY]["sha"] == sha
    assert entry["park_phase"] == "release_wait"


@pytest.mark.parametrize("content", [
    "not json",
    json.dumps({"sha": "main"}),
    json.dumps({"sha": "a" * 40, "run": "rm"}),
])
def test_a_malformed_await_release_is_no_park(db, worker_factory, agent_cli, content):
    _seed(db, {"write_artifact": {"await-release.json": content}})

    # MUTATION: accept any `sha` and a branch name parks the agent on nothing GitHub can answer.
    assert _run(worker_factory) == ExitCode.OK
    _succeeded(db)
