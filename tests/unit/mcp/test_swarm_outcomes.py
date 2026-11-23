"""`swarm_outcomes`: one short object per task, and `swarm_wait` keeps the verdict.

Improvement plan of 2026-11-23, item 3. A session that launched a batch read
each task's outcome through `swarm_result` (every output, every attempt's
spend, every question) when it wanted six facts: the state, the pull request,
the review's verdict, why a merge step was refused, what it cost and took, and
whether the owner has questions open. `swarm_outcomes` answers those, for
task ids or one workflow's steps, from what the API already serves: the task
document, the task's `verdict.json` artifact and the questions route. No new
route. `swarm_wait` also hands back the review verdict `swarm_result` shows.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmError

REVIEW = {
    "id": "task_review",
    "state": "SUCCEEDED",
    "runner_profile": "claude-code",
    "attempts": 2,
    "attempts_with_cost": 2,
    "cost_usd_total": 3.25,
    "cost_incomplete": False,
    "duration_s_total": 812.4,
    "result_summary": {
        "git": {"commit_count": 1, "pull_request": {"number": 41, "url": "https://example.com/acme/app/pull/41"}},
    },
}

#: Two blockers by severity, one unmarked (the convention reads it as a
#: blocker), one major and one minor: four blockers and majors.
VERDICT = {
    "verdict": "NOT_YET",
    "findings": [
        {"severity": "blocker", "problem": "a"},
        {"severity": "Blocker", "problem": "b"},
        "an unmarked finding",
        {"severity": "major", "problem": "c"},
        {"severity": "minor", "problem": "d"},
    ],
}

MERGE_STEP = {
    "id": "task_merge",
    "state": "FAILED",
    "runner_profile": "merge",
    "result_summary": {
        "action": "merge",
        "pull_request": 41,
        "verdict": "MERGE",
        "refusal": {"code": "checks_failed", "message": "required check `unit` failed at 0abc1234"},
    },
}

ASKING = {
    "id": "task_asking",
    "state": "SUCCEEDED",
    "runner_profile": "claude-code",
    "result_summary": {"questions": 2},
}

QUESTIONS = [
    {"question": "Keep the old key?", "options": [{"label": "keep", "description": "x"}],
     "recommended": "keep", "context": "c"},
    {"question": "Rename the flag?", "options": [{"label": "yes", "description": "y"}],
     "recommended": "yes", "context": "c"},
]


class Api:
    """The four reads `swarm_outcomes` makes, from canned replies."""

    tenant = None

    def __init__(self, tasks: list[dict[str, Any]], artifacts: dict[str, dict[str, Any]] | None = None,
                 answered: dict[str, list[int]] | None = None, workflow: dict[str, Any] | None = None) -> None:
        self.tasks = {t["id"]: t for t in tasks}
        self.files = artifacts or {}
        self.answered = answered or {}
        self.workflow = workflow
        self.calls: list[str] = []

    def request(self, method: str, path: str, **_: Any) -> Any:
        self.calls.append(f"{method} {path}")
        parts = path.split("?")[0].strip("/").split("/")
        if parts[1] == "workflows" and self.workflow is not None:
            return self.workflow
        if parts[1] == "tasks" and len(parts) == 4 and parts[3] == "questions":
            task_id = parts[2]
            return {"status": "ok", "questions": [
                {**q, "answer": {"choice": "x"} if i in self.answered.get(task_id, []) else None}
                for i, q in enumerate(QUESTIONS)]}
        raise SwarmError(f"no canned reply for {path}", status=404)

    def task(self, task_id: str) -> dict[str, Any]:
        self.calls.append(f"task {task_id}")
        if task_id not in self.tasks:
            raise SwarmError(f"GET /v1/tasks/{task_id}: 404 not_found", status=404)
        return self.tasks[task_id]

    def artifacts(self, task_id: str) -> dict[str, Any]:
        self.calls.append(f"artifacts {task_id}")
        return {"artifacts": [{"name": name} for name in self.files.get(task_id, {})]}

    def artifact_content(self, task_id: str, name: str, *, limit_bytes: int) -> dict[str, Any]:
        self.calls.append(f"content {task_id} {name}")
        return {"status": "ok", "content": json.dumps(self.files[task_id][name]), "truncated": False}


def _tool(name: str) -> dict[str, Any]:
    return next(t for t in server.TOOLS if t["name"] == name)


def _outcomes(api: Api, **args: Any) -> list[dict[str, Any]]:
    return json.loads(server._call(api, "swarm_outcomes", args))["outcomes"]


def test_swarm_outcomes_is_a_tool_taking_task_ids_or_one_workflow_id() -> None:
    properties = _tool("swarm_outcomes")["inputSchema"]["properties"]
    assert set(properties) == {"task_ids", "workflow_id"}


def test_a_reviewed_task_reports_state_pull_request_verdict_cost_and_duration() -> None:
    api = Api([REVIEW], artifacts={"task_review": {"verdict.json": VERDICT}})

    [outcome] = _outcomes(api, task_ids=["task_review"])

    assert outcome == {
        "task_id": "task_review",
        "state": "SUCCEEDED",
        "pull_request": {"number": 41, "url": "https://example.com/acme/app/pull/41"},
        "review": {"verdict": "NOT_YET", "blockers_and_majors": 4},
        "cost_usd": 3.25,
        "duration_s": 812.4,
        "owner_questions_open": 0,
    }


def test_a_refused_merge_step_names_the_failing_check_and_its_verdict() -> None:
    [outcome] = _outcomes(Api([MERGE_STEP]), task_ids=["task_merge"])

    assert outcome["state"] == "FAILED"
    assert outcome["pull_request"] == {"number": 41, "url": None}
    assert outcome["review"] == {"verdict": "MERGE", "blockers_and_majors": None}
    assert outcome["merge_refused"] == {
        "code": "checks_failed", "message": "required check `unit` failed at 0abc1234"}


def test_a_merge_step_waiting_on_owner_questions_says_they_are_open() -> None:
    waiting = {"id": "task_wait", "state": "PARKED", "runner_profile": "merge",
               "result_summary": {"action": "merge", "pull_request": 41,
                                  "wait": {"code": "owner_questions_open", "message": "2 open"},
                                  "open_questions": [{"task": "task_asking", "index": 0},
                                                     {"task": "task_asking", "index": 1}]}}

    [outcome] = _outcomes(Api([waiting]), task_ids=["task_wait"])

    assert outcome["owner_questions_open"] == 2
    assert "merge_refused" not in outcome


def test_open_owner_questions_count_only_the_unanswered_ones() -> None:
    api = Api([ASKING], artifacts={"task_asking": {"questions.json": QUESTIONS}},
              answered={"task_asking": [0]})

    [outcome] = _outcomes(api, task_ids=["task_asking"])

    assert outcome["owner_questions_open"] == 1


def test_unmeasured_cost_and_duration_are_null_never_zero() -> None:
    [outcome] = _outcomes(Api([ASKING | {"result_summary": {}}]), task_ids=["task_asking"])

    assert outcome["cost_usd"] is None
    assert outcome["duration_s"] is None
    assert outcome["review"] is None
    assert outcome["pull_request"] is None


def test_a_task_that_cannot_be_read_says_so_and_the_others_still_answer() -> None:
    api = Api([REVIEW])

    outcomes = _outcomes(api, task_ids=["task_gone", "task_review"])

    assert outcomes[0]["task_id"] == "task_gone"
    assert "404" in outcomes[0]["read_error"]
    assert outcomes[1]["state"] == "SUCCEEDED"


def test_a_workflow_id_answers_one_outcome_per_step_in_step_order_from_one_read() -> None:
    envelope = {
        "workflow": {"id": "wf_1", "steps": [
            {"step_id": "merge", "task_id": "task_merge"},
            {"step_id": "review", "task_id": "task_review"},
            {"step_id": "later"},
        ]},
        "tasks": [REVIEW, MERGE_STEP],
    }
    api = Api([], workflow=envelope)

    outcomes = _outcomes(api, workflow_id="wf_1")

    assert [o.get("step_id") for o in outcomes] == ["merge", "review", "later"]
    assert [o.get("task_id") for o in outcomes] == ["task_merge", "task_review", None]
    assert outcomes[2]["state"] is None
    assert not [c for c in api.calls if c.startswith("task ")]


@pytest.mark.parametrize("args", [{}, {"task_ids": ["t1"], "workflow_id": "wf_1"}, {"task_ids": []}])
def test_exactly_one_of_task_ids_and_workflow_id_is_required(args: dict[str, Any]) -> None:
    with pytest.raises(SwarmError, match="task_ids.*workflow_id"):
        server._call(Api([]), "swarm_outcomes", args)


def test_swarm_wait_returns_the_review_verdict_it_used_to_drop() -> None:
    api = Api([REVIEW], artifacts={"task_review": {"verdict.json": VERDICT}})

    reply = json.loads(server._call(api, "swarm_wait", {"task_ids": ["task_review"], "timeout_seconds": 0}))

    [finished] = reply["finished"]
    assert finished["verdict"]["verdict"] == "NOT_YET"
    assert finished["verdict"]["findings"] == 5
    assert finished["verdict"]["blockers_and_majors"] == 4
