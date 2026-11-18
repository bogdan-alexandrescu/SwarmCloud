"""A finished task's owner questions reach a session only while they are open.

Seen twice on 5 November 2026: a workflow row's final result and
`swarm_result` carried questions answered hours earlier with
`swarm_task_answer`, because `progress.owner_questions` read
`questions.json` and never the stored answers, and the session put them to
the person again. Every place that hands them to a session now reads the
answers from `GET /v1/tasks/{id}/questions` (as `swarm_check` reads a parked
task's), once per call and never cached:

* two questions, one answered: only the other, under its own `index`;
* all answered: none;
* an answers read that fails: the questions stay, each saying why, so
  "answered" and "could not tell" are never the same.

The task documents handed in here carry NO `owner_answers`, as a task read
before the answer was given does not: the answer is found on the route.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_mcp import buildboard, compact, progress, server
from swarm_mcp.client import SwarmError

from control_plane.conftest import PROJECT
from test_follow_cursor import NOW, TENANT, World, swarm, world  # noqa: F401 - fixtures

BUCKET = f"swarm-artifacts-{PROJECT}"
NAME = "questions.json"

QUESTIONS = [
    {
        "question": "Should the retry budget be per task or per workflow?",
        "options": [
            {"label": "per task", "description": "Each step keeps its own three attempts."},
            {"label": "per workflow", "description": "One budget shared by every step."},
        ],
        "recommended": "per task",
        "context": "CONTRACT.md fixes three attempts per task.",
    },
    {
        "question": "Rename the tab now or in the next wave?",
        "options": [{"label": "now", "description": "this lane"}, {"label": "next wave", "description": "later"}],
        "recommended": "now",
        "context": "The console says Runs.",
    },
]


def _answer(index: int, choice: str) -> dict[str, Any]:
    return {"question_index": index, "question": QUESTIONS[index]["question"], "choice": choice,
            "text": None, "by": "alice@example.com", "at": "2026-11-05T09:00:00+00:00",
            "via": "claude-code", "history": []}


def _finished(world: World, task_id: str = "task_a", *, answered: list[int]) -> None:
    """A SUCCEEDED task with both questions, the given indexes answered on the task."""
    world.task(task_id, state="SUCCEEDED")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["step_id"] = "review"
    doc["started_at"] = NOW
    doc["completed_at"] = NOW.replace(minute=4)
    body = json.dumps(QUESTIONS)
    key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att_1/artifacts/{NAME}"
    world.objects.put(key, body)
    entry = {"name": NAME, "bytes": len(body.encode("utf-8")), "uri": f"gs://{BUCKET}/{key}"}
    doc["result_summary"] = {"runner": {"summary": "done"}, "artifacts": [entry],
                             "artifact_bytes": entry["bytes"], "logs": {}, "questions": len(QUESTIONS)}
    choices = ["per task", "now"]
    doc["metadata"] = {**(doc.get("metadata") or {}),
                       "owner_answers": [_answer(i, choices[i]) for i in answered]}


def _stale(swarm: Any, task_id: str = "task_a") -> dict[str, Any]:
    """The task as a session read it before the owner answered: no answers on it."""
    task = dict(swarm.task(task_id))
    task.pop("owner_answers", None)
    return task


def _fail_answers(swarm: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    real = swarm.request

    def request(method: str, path: str, *a: Any, **k: Any) -> Any:
        if path.endswith("/questions"):
            raise SwarmError("HTTP 503 from the API", status=503)
        return real(method, path, *a, **k)

    monkeypatch.setattr(swarm, "request", request)


def _open(index: int) -> dict[str, Any]:
    return {"index": index, **QUESTIONS[index]}


# --- two questions, one answered: only the other ------------------------------


def test_the_follow_outcome_hands_on_only_the_unanswered_question(swarm, world):
    _finished(world, answered=[0])

    got = progress.outcome(swarm, _stale(swarm))

    assert got["questions"] == [_open(1)]
    assert got["questions_answered"] == 1


def test_swarm_result_hands_on_only_the_unanswered_question_and_keeps_every_one_asked(swarm, world, monkeypatch):
    _finished(world, answered=[0])
    real = swarm.task
    monkeypatch.setattr(swarm, "task", lambda tid, *a, **k: {k2: v for k2, v in real(tid, *a, **k).items()
                                                             if k2 != "owner_answers"})

    reply = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))

    assert reply["owner_questions"] == [_open(1)]
    assert reply["questions_answered"] == 1
    # The full list stays where a person browses the task.
    assert [(q["index"], q["answered"]) for q in reply["owner_questions_asked"]] == [(0, True), (1, False)]
    assert reply["owner_questions_asked"][0]["question"] == QUESTIONS[0]["question"]


def test_a_rows_result_carries_only_the_open_question_with_its_index(swarm, world):
    _finished(world, answered=[0])

    got = compact.watch_progress(swarm, ["task_a"])

    assert got["tasks"][0]["outcome"]["questions"] == [_open(1)]
    assert got["result"]["questions"] == [_open(1)]


def test_the_build_board_lists_only_the_open_question(swarm, world):
    _finished(world, answered=[0])
    reader = buildboard._Reader(swarm, {"tasks": {}}, NOW)
    task = _stale(swarm)

    items = buildboard._questions({"label": "lane"}, [("review", task, reader.finished(task))])

    assert [i["item"] for i in items] == [QUESTIONS[1]["question"]]


def test_the_build_board_reads_the_answers_again_after_caching_the_questions(swarm, world):
    _finished(world, answered=[])
    cache: dict[str, Any] = {"tasks": {}}
    reader = buildboard._Reader(swarm, cache, NOW)
    task = _stale(swarm)
    assert len(buildboard._questions({"label": "lane"}, [("review", task, reader.finished(task))])) == 2

    world.db.docs["tasks/task_a"]["metadata"]["owner_answers"] = [_answer(1, "now")]

    items = buildboard._questions({"label": "lane"}, [("review", task, reader.finished(task))])
    assert [i["item"] for i in items] == [QUESTIONS[0]["question"]]


# --- all answered: none ----------------------------------------------------------


def test_every_question_answered_hands_on_none(swarm, world, monkeypatch):
    _finished(world, answered=[0, 1])

    got = progress.outcome(swarm, _stale(swarm))
    assert got["questions"] == []
    assert got["questions_answered"] == 2

    real = swarm.task
    monkeypatch.setattr(swarm, "task", lambda tid, *a, **k: {k2: v for k2, v in real(tid, *a, **k).items()
                                                             if k2 != "owner_answers"})
    reply = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))
    assert "owner_questions" not in reply
    assert [q["answered"] for q in reply["owner_questions_asked"]] == [True, True]


# --- an answers read that fails: the questions stay, with the reason -------------


def test_an_answers_read_that_fails_keeps_the_questions_with_the_reason(swarm, world, monkeypatch):
    _finished(world, answered=[0])
    _fail_answers(swarm, monkeypatch)

    got = progress.outcome(swarm, _stale(swarm))

    assert [q["index"] for q in got["questions"]] == [0, 1]
    for question in got["questions"]:
        assert "stored answers could not be read" in question["answer_unknown_because"]
        assert "503" in question["answer_unknown_because"]
        assert len(question["answer_unknown_because"]) <= progress.ANSWER_UNKNOWN_CHARS


def test_an_answers_read_that_fails_still_drops_what_the_task_carried(swarm, world, monkeypatch):
    _finished(world, answered=[0])
    _fail_answers(swarm, monkeypatch)

    got = progress.outcome(swarm, swarm.task("task_a"))

    assert [q["index"] for q in got["questions"]] == [1]
    assert got["questions"][0]["answer_unknown_because"]


def test_swarm_result_says_could_not_tell_rather_than_open(swarm, world, monkeypatch):
    _finished(world, answered=[])
    _fail_answers(swarm, monkeypatch)

    reply = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))

    assert [q["answered"] for q in reply["owner_questions_asked"]] == [None, None]
    assert all(q["answer_unknown_because"] for q in reply["owner_questions"])


def test_the_build_board_says_whether_it_is_answered_is_unknown(swarm, world, monkeypatch):
    _finished(world, answered=[])
    _fail_answers(swarm, monkeypatch)
    reader = buildboard._Reader(swarm, {"tasks": {}}, NOW)
    task = _stale(swarm)

    items = buildboard._questions({"label": "lane"}, [("review", task, reader.finished(task))])

    assert len(items) == 2
    assert all("whether it is answered is unknown" in i["detail"] for i in items)
