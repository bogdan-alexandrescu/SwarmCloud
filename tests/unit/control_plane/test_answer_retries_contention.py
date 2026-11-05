"""Answers to different questions of one task, sent at once, are all stored (part of #2688).

FOUND 5 November 2026, 02:49 UTC: the sc plugin recorded four of a design
lane's owner questions with four `swarm_task_answer` calls sent at once
(`POST /v1/tasks/{id}/answers`, one per question index). Three landed; the
fourth answered `500 Internal Server Error`, and the same call sent alone a
few seconds later landed.

The answer is one Firestore transaction that re-reads `metadata.owner_answers`
and merges its own entry in, so answers to DIFFERENT questions never overwrite
each other. But four transactions on one task document contend for it, and
contention has two shapes `firestore.transactional` does not turn into a
success: `Aborted` on the transactional READ of the task, which it does not
retry at all, and its own commit retries running out, which it reports as a
`ValueError("Failed to commit transaction in N attempts.")`. Neither has
committed, so both are run again here -- the cancel's rule (CANCEL-RETRY,
`store._is_contention`) -- and contention that outlasts the retries answers
503 `answer_contended` with a sentence and a `Retry-After`, never a bare 500.

WHAT THE FAKE MODELS. `FaultyFirestore` (the cancel's) raises on a
transactional read of one document and aborts a number of commits; the REAL
`firestore.transactional` decorator drives it. `InterleavingFirestore`
additionally lets a second answer commit between the first one's read and
its commit, and aborts the first one's commit then, as Firestore does when a
document a transaction read has changed.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from google.api_core import exceptions as gexc

from swarm_api import agent_questions

from .fakes import FakeDocumentRef
from .test_cancel_retries_contention import FaultyFirestore, FaultyTransaction
from .test_task_answers_route import TASK, _answer, _asked, _stored

PATH = f"tasks/{TASK}"


class InterleavingTransaction(FaultyTransaction):
    def get(self, ref: Any, **kwargs: Any) -> Any:
        result = super().get(ref, **kwargs)
        if isinstance(ref, FakeDocumentRef) and ref.path == PATH:
            hook, self._db.on_read = self._db.on_read, None
            if hook is not None:
                hook()
                # The document this transaction read changed under it.
                self._db.commit_aborts += 1
        return result


class InterleavingFirestore(FaultyFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.on_read: Callable[[], None] | None = None

    def transaction(self, **kwargs: Any) -> InterleavingTransaction:
        return InterleavingTransaction(self)


@pytest.fixture
def db() -> InterleavingFirestore:
    """Overrides conftest's `db`, so `client` runs against it."""
    return InterleavingFirestore()


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    """Every pause the retry takes, recorded instead of slept.

    `raising=False`: before the retry existed the module had no pause to
    replace, and the test must fail on its assertions, not on this fixture.
    """
    taken: list[float] = []
    monkeypatch.setattr(agent_questions, "_contention_sleep", taken.append, raising=False)
    return taken


def _choices(db) -> dict[int, str]:
    return {entry["question_index"]: entry["choice"] for entry in _stored(db)}


def test_an_answer_whose_read_meets_contention_is_stored_beside_the_other(
    client, db, objects, pauses
) -> None:
    _asked(db, objects)
    first = _answer(client, {"question_index": 0, "choice": "per workflow"})
    assert first.status_code == 200, first.text
    # The second answer's read of the task meets the first one's lock.
    db.fail_reads(PATH, gexc.Aborted("transaction lock timeout"))

    second = _answer(client, {"question_index": 1, "choice": "keep"})

    # MUTATION: run the transaction once, as before, and this is the 500.
    assert second.status_code == 200, second.text
    assert _choices(db) == {0: "per workflow", 1: "keep"}
    assert second.json()["questions"][0]["answer"]["choice"] == "per workflow"
    assert second.json()["questions"][1]["answer"]["choice"] == "keep"
    # MUTATION: retry at once, with no pause, and this list is empty.
    assert len(pauses) == 1 and pauses[0] >= 0, pauses


def test_two_answers_interleaved_between_read_and_commit_are_both_stored(
    client, db, objects, pauses
) -> None:
    _asked(db, objects)
    sent: list[int] = []
    # Answer 1 commits after answer 0 has read the task, before answer 0 commits.
    db.on_read = lambda: sent.append(
        _answer(client, {"question_index": 1, "choice": "drop"}).status_code
    )

    response = _answer(client, {"question_index": 0, "choice": "per task"})

    assert sent == [200]
    assert response.status_code == 200, response.text
    # MUTATION: write the answers read before the other committed, and
    # question 1's answer is lost here.
    assert _choices(db) == {0: "per task", 1: "drop"}
    assert all(entry["history"] == [] for entry in _stored(db))


def test_a_second_answer_to_the_same_question_under_contention_keeps_the_first_in_history(
    client, db, objects, pauses
) -> None:
    _asked(db, objects)
    assert _answer(client, {"question_index": 0, "choice": "per task"}).status_code == 200
    db.fail_reads(PATH, gexc.Aborted("transaction lock timeout"))

    response = _answer(client, {"question_index": 0, "choice": "per workflow"})

    assert response.status_code == 200, response.text
    (entry,) = _stored(db)
    assert entry["choice"] == "per workflow"
    # MUTATION: a retry that re-applies its first run's entry records the
    # replaced answer twice; it is recorded once.
    assert [h["choice"] for h in entry["history"]] == ["per task"]


def test_contention_on_every_read_answers_503_with_a_sentence(client, db, objects, pauses) -> None:
    _asked(db, objects)
    db.fail_every_read(PATH, gexc.Aborted("transaction lock timeout"))

    response = _answer(client, {"question_index": 1, "choice": "keep"})

    # MUTATION: let the last `Aborted` escape and this is a bare 500.
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "answer_contended", body
    assert "question 2" in body["message"] and "retry" in body["message"], body
    assert int(response.headers["Retry-After"]) >= 1
    db.heal(PATH)
    assert _stored(db) == []
    # MUTATION: retry without bound and this is far more than four.
    assert db.reads[PATH] == agent_questions.ANSWER_TRANSACTION_ATTEMPTS
    assert len(pauses) == agent_questions.ANSWER_TRANSACTION_ATTEMPTS - 1


def test_commit_retries_running_out_answers_503(client, db, objects, pauses) -> None:
    _asked(db, objects)
    db.commit_aborts = 10_000

    response = _answer(client, {"question_index": 0, "choice": "per task"})

    # MUTATION: treat the decorator's `ValueError` as anything but contention
    # and this is a 500.
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "answer_contended"
    db.commit_aborts = 0
    assert _stored(db) == []


def test_a_deadline_is_not_retried(client, db, objects, pauses) -> None:
    _asked(db, objects)
    db.fail_reads(PATH, gexc.DeadlineExceeded("deadline"))

    response = _answer(client, {"question_index": 0, "choice": "per task"})

    # A deadline may expire after a commit landed: never run again here.
    assert response.status_code != 200
    assert pauses == []
    assert db.reads[PATH] == 1
