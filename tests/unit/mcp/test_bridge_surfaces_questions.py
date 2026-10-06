"""The bridge surfaces an agent's `questions.json` (lane OQ, owner decision 2026-10-05).

The worker counts a valid `questions.json` as `result_summary.questions`
(`agent_worker.questions`). The bridge reads the file back through the
artifacts route -- redacted there, like any artifact -- and hands the
questions to whoever reads the result:

* the follow outcome carries `questions`, a list, and `swarm_result` carries
  them as `owner_questions` (owner decision 2026-10-06, observer P15);
* a row says `? N question(s) for the owner` beside the task's final line;
* the `sc:step` result (`compact.step_result`) carries them, with no null.

A task with none carries `questions: []` and no line, and costs no extra read.
The questions are data for the operator: nothing here answers or acts on them.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_mcp import compact, progress, server

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
        "options": [{"label": "now", "description": ""}, {"label": "next wave", "description": ""}],
        "recommended": None,
        "context": None,
    },
]


def _finished(world: World, task_id: str = "task_a", *, questions: list[dict[str, Any]] | None,
              count: int | None = None) -> None:
    """A SUCCEEDED task whose manifest and bucket agree, as the worker leaves them."""
    world.task(task_id, state="SUCCEEDED")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["step_id"] = "review"
    doc["started_at"] = NOW
    doc["completed_at"] = NOW.replace(minute=4)
    entries = []
    if questions is not None:
        body = json.dumps(questions)
        key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att_1/artifacts/{NAME}"
        world.objects.put(key, body)
        entries.append({"name": NAME, "bytes": len(body.encode("utf-8")), "uri": f"gs://{BUCKET}/{key}"})
    doc["result_summary"] = {
        "runner": {"summary": "done"},
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
        "questions": len(questions or []) if count is None else count,
    }


def test_swarm_result_carries_the_questions(swarm, world):
    _finished(world, questions=QUESTIONS)

    reply = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))

    assert reply["owner_questions"] == QUESTIONS
    assert "questions_unavailable_because" not in reply


def test_the_follow_outcome_carries_the_questions(swarm, world):
    _finished(world, questions=QUESTIONS)

    got = progress.outcome(swarm, swarm.task("task_a"))

    assert got["questions"] == QUESTIONS


def test_a_task_with_no_questions_carries_an_empty_list_and_reads_nothing_more(swarm, world, monkeypatch):
    _finished(world, questions=None)

    def _no_read(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a task with no questions must not read questions.json")

    monkeypatch.setattr(swarm, "artifact_content", _no_read)
    got = progress.outcome(swarm, swarm.task("task_a"))
    assert got["questions"] == []
    reply = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))
    assert "owner_questions" not in reply


def test_a_counted_file_that_cannot_be_read_says_so(swarm, world):
    # Counted by the worker, but the object is not in the bucket.
    _finished(world, questions=None, count=2)
    world.db.docs["tasks/task_a"]["result_summary"]["artifacts"] = [
        {"name": NAME, "bytes": 10, "uri": f"gs://{BUCKET}/tenants/{TENANT}/tasks/task_a/attempts/att_1/artifacts/{NAME}"}
    ]

    got = progress.outcome(swarm, swarm.task("task_a"))

    assert got["questions"] == []
    assert "2 question(s)" in got["questions_unavailable_because"], got


def test_the_progress_row_says_how_many_and_the_step_result_carries_them(swarm, world):
    _finished(world, questions=QUESTIONS)

    got = compact.watch_progress(swarm, ["task_a"])

    assert any("? 2 question(s) for the owner" in line for line in got["progress"]), got["progress"]
    assert got["tasks"][0]["outcome"]["questions"] == QUESTIONS
    result = got["result"]
    assert len(result["questions"]) == 2
    # The step result holds no null anywhere (`step_result`'s rule).
    assert "null" not in json.dumps(result), result
    assert result["questions"][1]["recommended"] == ""
    assert result["questions"][1]["context"] == ""
    assert result["questions"][0] == QUESTIONS[0]


def test_the_lines_format_says_how_many(swarm, world):
    _finished(world, questions=QUESTIONS)

    got = progress.watch(swarm, ["task_a"])

    assert any(line.endswith("? 2 question(s) for the owner") for line in got["lines"]), got["lines"]


def test_no_questions_no_line(swarm, world):
    _finished(world, questions=None)

    got = compact.watch_progress(swarm, ["task_a"])

    assert not any("question(s) for the owner" in line for line in got["progress"]), got["progress"]
    assert got["result"]["questions"] == []
