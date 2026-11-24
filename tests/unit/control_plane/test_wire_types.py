"""`wire_types.Task` is the task every route serves (part of #72).

The console's `Task` is GENERATED from this model (`scripts/gen_api_types.py`
writes `apps/swarm-ui/src/generated/api-types.ts`, and
`tests/unit/scripts/test_api_types_drift.py` holds that file to the model). So
the model is the one place the task's wire shape is declared, and these tests
hold it to what the routes actually serve, in both directions:

  * every key a task route serves is a field of the model -- a field the
    console cannot see is a feature nobody can reach;
  * every REQUIRED field of the model is in every served task, the summary
    listing included -- a required field nobody sends renders as an em dash
    forever.

This replaces, for the task shape only, the regex reading of `types.ts` in
`test_ui_api_field_contract.py`: the TypeScript side is now generated, and
`tsc` holds the console's narrowing to it.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.codec import SUMMARY_DROPPED_KEYS, task_from_dict, task_to_api
from swarm_api.wire_types import Task, TaskEnvelope

from .conftest import auth_header, seed_tenant, seed_task

NOW = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
FIELDS = set(Task.model_fields)
REQUIRED = {name for name, field in Task.model_fields.items() if field.is_required()}


@pytest.fixture
def served(client, db):
    """One populated task as each route serves it: optional fields carry
    values, so "served" and "not served" are distinguishable."""
    seed_tenant(db, "eng", credentials=("anthropic",))
    task = seed_task(db, task_id="task_1", tenant_id="eng", state="RUNNING",
                     provider="anthropic", workflow_id="wf_1")
    task.update({
        "model": "claude-opus-4",
        "started_at": NOW - timedelta(minutes=5),
        "next_eligible_at": NOW + timedelta(minutes=5),
        "last_error": "dispatch_failed (attempt att_1)",
        "result_summary": {"artifacts": ["out.txt"], "exit_code": 0},
        "latest_checkpoint": "gs://swarm-artifacts-example-project/checkpoints/task_1/3",
        "repository_url": "https://github.com/example-org/example-repo",
        "repository_ref": "main",
        "metadata": {"title": "a task", "dispatch": {"strategy": "collect",
                                                      "carrier": "checkpoints"}},
        "blocked_by": [{"reason": "TENANT_LIMIT", "pool": "tenant:eng", "limit": 20, "active": 20}],
        "step_id": "s1",
        "current_lease_id": "lease_1",
        "current_generation": 3,
        "input": {"prompt": "do the thing"},
    })

    def get(path: str) -> dict:
        response = client.get(path, headers=auth_header("alice"))
        assert response.status_code == 200, response.text
        return response.json()

    def only(rows: list[dict], what: str) -> dict:
        assert rows, f"{what} served no task; the comparison would be vacuous"
        return rows[0]

    return {
        "GET /v1/tasks/{id}": get("/v1/tasks/task_1")["task"],
        "GET /v1/tasks": only(get("/v1/tasks?limit=50")["tasks"], "GET /v1/tasks"),
        "GET /v1/tasks?view=summary": only(
            get("/v1/tasks?limit=50&view=summary")["tasks"], "GET /v1/tasks?view=summary"),
        "task": task,
    }


def test_the_model_declares_every_key_a_task_route_serves(served):
    problems = [
        f"{route}: {sorted(set(row) - FIELDS)}"
        for route, row in served.items() if route != "task" and set(row) - FIELDS
    ]
    assert not problems, (
        "these routes serve task keys `wire_types.Task` does not declare, so the console's "
        f"generated type cannot see them: {problems}. Declare them there and run "
        "`uv run python scripts/gen_api_types.py`."
    )


def test_every_required_field_is_in_every_served_task(served):
    problems = [
        f"{route}: {sorted(REQUIRED - set(row))}"
        for route, row in served.items() if route != "task" and REQUIRED - set(row)
    ]
    assert not problems, (
        f"`wire_types.Task` requires fields these routes do not serve: {problems}. "
        "Make them optional there (a default), or serve them."
    )


def test_every_key_task_to_api_builds_is_declared(served):
    """The codec directly, both views, so a key reaches the model even before a
    route test seeds the data that makes it appear."""
    task = task_from_dict(served["task"])
    full = task_to_api(task)
    summary = task_to_api(task, summary=True)
    assert set(full) - FIELDS == set(), sorted(set(full) - FIELDS)
    assert set(summary) == set(full) - set(SUMMARY_DROPPED_KEYS)
    assert REQUIRED <= set(summary), sorted(REQUIRED - set(summary))


def test_the_trouble_board_fields_are_declared():
    """`types.ts` once declared 21 of `task_to_api`'s 31 keys, and the ten it
    omitted were the ones the remaining screens are built on."""
    for field in ("last_error", "result_summary", "latest_checkpoint", "next_eligible_at",
                  "metadata", "input", "model", "timeout_seconds", "repository_ref"):
        assert field in FIELDS, f"wire_types.Task does not declare {field}"


def test_the_route_documents_the_shape_without_filtering_what_it_serves(served):
    """`responses=` documents; `response_model=` would validate and drop every
    key the model lacks. The detail must still carry keys only a route adds."""
    detail = served["GET /v1/tasks/{id}"]
    assert "attempts_read" in detail and "links" in detail
    assert TaskEnvelope.model_fields["task"].annotation is Task
