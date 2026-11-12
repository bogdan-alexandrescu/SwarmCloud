"""The sc plugin pairs an owner answer with the question it answered, not its index (#3349).

An answer is stored under its `question_index`, and a later attempt may ask
another question at that index. swarm-api's questions route already serves
only the answers that match (`agent_questions.answers_to`), and the task
read's `owner_answers` entries carry the `question` each answered. The build
board (`buildboard._answered`) and the question listing
(`progress.stored_answers`) used to pair by index alone, so a re-asked index
was hidden as answered:

* an answer recorded for another question leaves the new question open;
* the same question asked again reads answered;
* an answer that records no question text is still counted by index.
"""

from __future__ import annotations

from typing import Any

from swarm_mcp import buildboard, progress
from swarm_mcp.client import SwarmError

EARLIER = "Should the retry budget be per task or per workflow?"
LATER = "Rename the tab now or in the next wave?"


def _asked(text: str) -> dict[str, Any]:
    return {"question": text, "options": [{"label": "yes", "description": ""}, {"label": "no", "description": ""}],
            "recommended": "yes", "context": ""}


def _answer(question: str | None) -> dict[str, Any]:
    answer = {"question_index": 0, "choice": "yes", "text": None, "by": "alice@example.com",
              "at": "2026-11-05T09:00:00+00:00", "history": []}
    if question is not None:
        answer["question"] = question
    return answer


def _task(answer: dict[str, Any]) -> dict[str, Any]:
    return {"id": "task_a", "state": "SUCCEEDED", "owner_answers": [answer]}


class _Client:
    """The questions route, answering as swarm-api's `answers_to` does."""

    def __init__(self, served: Any) -> None:
        self.served = served

    def request(self, method: str, path: str, **_: Any) -> Any:
        assert (method, path) == ("GET", "/v1/tasks/task_a/questions")
        if isinstance(self.served, Exception):
            raise self.served
        return self.served


def _route(text: str, answer: dict[str, Any] | None) -> _Client:
    return _Client({"status": "ok", "questions": [{**_asked(text), "answer": answer}]})


def _board_items(answer: dict[str, Any], now_asked: str) -> list[str]:
    lane = {"id": "wf_1", "label": "Lane 1"}
    done = {"questions": [_asked(now_asked)], "answered": [], "ended": "2026-11-05T10:00:00+00:00"}
    return [item["item"] for item in buildboard._questions(lane, [("review", _task(answer), done)])]


# -- the build board ----------------------------------------------------------


def test_the_board_lists_a_reasked_index_open():
    assert _board_items(_answer(EARLIER), LATER) == [LATER]


def test_the_board_hides_the_same_question_asked_again():
    assert _board_items(_answer(LATER), LATER) == []


def test_the_board_counts_an_answer_without_its_question_by_index():
    assert _board_items(_answer(None), LATER) == []


def test_the_board_still_hides_the_earlier_question_its_answer_names():
    assert buildboard._answered(_task(_answer(EARLIER)), [_asked(LATER)]) == {EARLIER}


# -- the question listing -----------------------------------------------------


def _listed(answer: dict[str, Any], client: _Client) -> list[dict[str, Any]]:
    return progress.asked_with_answers(client, _task(answer), {"questions": [_asked(LATER)]})


def test_the_listing_reads_a_reasked_index_unanswered():
    got = _listed(_answer(EARLIER), _route(LATER, None))
    assert [q["answered"] for q in got] == [False]
    assert progress.stored_answers(_route(LATER, None), _task(_answer(EARLIER))) == (set(), None)


def test_the_listing_reads_the_same_question_answered():
    stored = {"choice": "yes"}
    assert [q["answered"] for q in _listed(_answer(LATER), _route(LATER, stored))] == [True]


def test_the_listing_trusts_the_route_over_the_documents_index():
    # An answer recorded without its question, but the route serves none: open.
    assert [q["answered"] for q in _listed(_answer(None), _route(LATER, None))] == [False]


def test_the_listing_counts_an_answer_without_its_question_by_index_when_the_route_fails():
    client = _Client(SwarmError("boom"))
    indexes, why = progress.stored_answers(client, _task(_answer(None)))
    assert indexes == {0} and why
    assert [q["answered"] for q in _listed(_answer(None), client)] == [True]


def test_the_listing_never_reads_a_reasked_index_answered_when_the_route_fails():
    # The document's answer names another question: not answered, and since
    # the route could not say, not known open either.
    client = _Client(SwarmError("boom"))
    (entry,) = _listed(_answer(EARLIER), client)
    # MUTATION: the fallback by index alone reads this True.
    assert entry["answered"] is None and entry["answer_unknown_because"]
    indexes, why = progress.stored_answers(client, _task(_answer(EARLIER)), questions=[_asked(LATER)])
    assert indexes == set() and why


def test_the_listing_reads_the_same_question_answered_when_the_route_fails():
    client = _Client({"status": "error"})
    assert [q["answered"] for q in _listed(_answer(LATER), client)] == [True]
    # Without the questions asked now, a recorded question cannot be matched.
    assert progress.stored_answers(client, _task(_answer(LATER)))[0] == set()


def test_the_board_lists_a_reasked_index_open_when_the_route_fails():
    reader = buildboard._Reader(_Client(SwarmError("boom")), {"tasks": {
        "task_a": {"pull_request": None, "questions": [_asked(LATER)], "ended": None}}}, None)
    done = reader.finished(_task(_answer(EARLIER)))
    assert done["answered"] == [] and done["answer_unknown_because"]
    lane = {"id": "wf_1", "label": "Lane 1"}
    items = [item["item"] for item in buildboard._questions(lane, [("review", _task(_answer(EARLIER)), done)])]
    assert items == [LATER]
