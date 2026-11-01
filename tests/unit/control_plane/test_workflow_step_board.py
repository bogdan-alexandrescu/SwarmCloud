"""The Workflows board draws from ONE read (console load speed, 2026-11-01).

MEASURED in the owner's browser, 2026-11-01: Work › Workflows gated on
`GET /v1/tasks?limit=200` with no view (328 KB, 3.8 s) to join each step to
its task for its state, title, start, end, cost and error, then made 18
`GET /v1/workflows/{id}` and 12 `GET /v1/tasks/{id}/attempts` for the rows
that join missed. Every one of those figures was already in the server's hand
when it built the list: the rollup reads every step's task document to derive
the workflow's state, and `attempt_totals.totals_for` sums a page's attempts
in one query per 30 tasks.

So each step of `GET /v1/workflows?view=summary`, and of the `?ids=` batch and
`GET /v1/workflows/{id}`, which read the same tasks, carries `task_summary`:
the step task's state, title and unit, start and end, its totals across every
attempt, and the first line of its `last_error`, masked by the task's own
masker. Never its input. The list's attempts read is batched -- one query per
30 step tasks, run concurrently -- never one per step, and it reads no task
document the rollup did not already read.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

import json
import math
import secrets
import threading
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.codec import workflow_to_firestore
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from . import fakes
from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
PROMPT = "write the board report "
BLOCK_KEYS = {
    "state", "title", "unit", "started_at", "completed_at", "last_error_line",
    "attempts", "attempts_with_cost", "cost_usd_total", "cost_incomplete",
    "duration_s_total", "duration_incomplete", "first_started_at",
    "last_attempt_cost_usd", "last_attempt_duration_s", "attempts_read",
}


def _workflow(db, workflow_id: str, steps: int, created_at: datetime, *,
              last_error: str | None = None, secret: str | None = None) -> Workflow:
    task_ids = [f"task_{workflow_id}_{i:02d}" for i in range(steps)]
    step_input = {"prompt": PROMPT * 5}
    for i, task_id in enumerate(task_ids):
        doc = seed_task(db, task_id=task_id, tenant_id="eng", state="FAILED" if last_error else "SUCCEEDED",
                        created_at=created_at, workflow_id=workflow_id)
        doc.update({
            "input": step_input,
            "step_id": f"s{i:02d}",
            "metadata": {"title": f"Step {i} of {workflow_id}", "unit": f"Lane {workflow_id}",
                         "workflow_step": f"s{i:02d}"},
            "started_at": created_at + timedelta(minutes=1),
            "completed_at": created_at + timedelta(minutes=4),
            "last_error": last_error,
            "attempt_count": 1,
        })
        db.docs[f"attempts/att_{task_id}"] = {
            "attempt_id": f"att_{task_id}", "task_id": task_id, "tenant_id": "eng",
            "generation": 1, "lease_id": f"lease_{task_id}", "backend": "CLOUD_RUN_JOB",
            "execution_name": None, "created_at": created_at, "error": None,
            "started_at": created_at + timedelta(minutes=1),
            "completed_at": created_at + timedelta(minutes=4),
            "exit_code": 0, "peak_rss_bytes": None, "oom_near_miss": False, "checkpoints": [],
            "cost_usd": 0.25,
        }
    workflow = Workflow(
        workflow_id=workflow_id, tenant_id="eng", created_at=created_at, updated_at=created_at,
        state=TaskState.FAILED if last_error else TaskState.SUCCEEDED,
        submitted_by="alice@example.com",
        steps=[
            WorkflowStep(step_id=f"s{i:02d}", runner_profile="mock", input=step_input,
                         depends_on=[], task_id=task_id)
            for i, task_id in enumerate(task_ids)
        ],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)
    return workflow


def _list(client, **params):
    response = client.get("/v1/workflows", headers=auth_header("alice"), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_each_summary_step_carries_what_the_board_draws_and_no_input(client, db):
    seed_tenant(db, "eng")
    _workflow(db, "wf_a", 2, T0, last_error="expected outputs missing\nTraceback (most recent call last):")

    body = _list(client, view="summary")

    row = body["workflows"][0]
    for i, step in enumerate(row["steps"]):
        block = step["task_summary"]
        assert set(block) == BLOCK_KEYS
        assert block["state"] == "FAILED"
        assert block["title"] == f"Step {i} of wf_a"
        assert block["unit"] == "Lane wf_a"
        assert block["started_at"].startswith("2026-11-01T12:01:00")
        assert block["completed_at"].startswith("2026-11-01T12:04:00")
        assert block["attempts"] == 1
        assert block["cost_usd_total"] == 0.25
        assert block["cost_incomplete"] is False
        assert block["duration_s_total"] == 180.0
        assert block["attempts_read"] == "ok"
        # The first line, which is what the board's failure cause reads.
        assert block["last_error_line"] == "expected outputs missing"
    # No step input anywhere in the response, the block's included.
    assert PROMPT not in json.dumps(body)


def test_the_block_is_masked_by_the_step_tasks_own_masker(client, db):
    seed_tenant(db, "eng")
    # Built at runtime: a GitHub token's shape, which the rules mask anywhere.
    secret = "gh" + "p_" + secrets.token_hex(18)
    _workflow(db, "wf_s", 1, T0, last_error=f"auth failed with {secret}\nmore")

    body = _list(client, view="summary")

    line = body["workflows"][0]["steps"][0]["task_summary"]["last_error_line"]
    assert secret not in line
    assert line.startswith("auth failed with ")
    assert secret not in json.dumps(body)
    # Exactly as the task's own read serves it, cut to its first line.
    task = client.get("/v1/tasks/task_wf_s_00", headers=auth_header("alice")).json()["task"]
    assert task["last_error"].splitlines()[0] == line


def test_a_step_with_no_task_read_has_a_null_block(client, db):
    seed_tenant(db, "eng")
    workflow = _workflow(db, "wf_gone", 2, T0)
    del db.docs[f"tasks/{workflow.steps[1].task_id}"]

    steps = _list(client, view="summary")["workflows"][0]["steps"]

    assert steps[0]["task_summary"]["state"] == "SUCCEEDED"
    assert steps[1]["task_summary"] is None


def test_the_full_list_is_unchanged(client, db):
    seed_tenant(db, "eng")
    _workflow(db, "wf_a", 1, T0)
    for step in _list(client)["workflows"][0]["steps"]:
        assert "task_summary" not in step


def test_ids_and_the_single_read_carry_the_same_block(client, db):
    seed_tenant(db, "eng")
    _workflow(db, "wf_a", 2, T0, last_error="boom")
    _workflow(db, "wf_b", 1, T0 - timedelta(minutes=1))

    listed = {w["workflow_id"]: w for w in _list(client, view="summary")["workflows"]}
    by_ids = _list(client, ids=["wf_a", "wf_b"], view="summary")
    one = client.get("/v1/workflows/wf_a", headers=auth_header("alice")).json()

    for row in by_ids["workflows"]:
        assert [s["task_summary"] for s in row["steps"]] == [
            s["task_summary"] for s in listed[row["workflow_id"]]["steps"]
        ]
    assert [s["task_summary"] for s in one["workflow"]["steps"]] == [
        s["task_summary"] for s in listed["wf_a"]["steps"]
    ]


@pytest.fixture
def reads(monkeypatch, db):
    """Every Firestore round trip the request makes, by collection."""
    seen: list[tuple[str, str]] = []
    real_stream = fakes.FakeQuery.stream
    real_get = fakes.FakeQuery.get
    real_get_all = db.get_all

    def stream(self, *args, **kwargs):
        seen.append(("query", self._path))
        return real_stream(self, *args, **kwargs)

    def get(self, *args, **kwargs):
        seen.append(("query", self._path))
        return real_get(self, *args, **kwargs)

    def get_all(references, *args, **kwargs):
        refs = list(references)
        seen.append(("get_all", refs[0].path.split("/", 1)[0] if refs else ""))
        return real_get_all(refs, *args, **kwargs)

    monkeypatch.setattr(fakes.FakeQuery, "stream", stream)
    monkeypatch.setattr(fakes.FakeQuery, "get", get)
    monkeypatch.setattr(db, "get_all", get_all)
    return seen


def test_the_list_reads_attempts_in_batches_and_no_task_beyond_the_rollup(client, db, reads):
    """250 steps over 5 workflows: 3 task `get_all`s (the rollup's), 9 attempts queries of 30."""
    seed_tenant(db, "eng")
    for i in range(5):
        _workflow(db, f"wf_{i}", 50, T0 - timedelta(minutes=i))

    body = _list(client, view="summary", limit=10)

    assert all(s["task_summary"]["attempts"] == 1 for w in body["workflows"] for s in w["steps"])
    attempts = [r for r in reads if r[1] == "attempts"]
    assert attempts == [("query", "attempts")] * math.ceil(250 / 30)
    tasks = [r for r in reads if r[1] == "tasks"]
    assert tasks == [("get_all", "tasks")] * 3



def test_the_attempts_batches_run_at_once_not_one_after_another(client, db, monkeypatch):
    """Four chunks of 30 meet at a barrier: run serially, the first would wait alone and break it."""
    from swarm_api.routes import workflows as route

    seed_tenant(db, "eng")
    for i in range(4):
        _workflow(db, f"wf_{i}", 30, T0 - timedelta(minutes=i))
    barrier = threading.Barrier(4, timeout=5)
    real = route.totals_for

    def meeting(db_, tenant_id, task_ids):
        barrier.wait()
        return real(db_, tenant_id, task_ids)

    monkeypatch.setattr(route, "totals_for", meeting)
    body = _list(client, view="summary", limit=10)

    assert sum(1 for w in body["workflows"] for s in w["steps"] if s["task_summary"]["attempts"] == 1) == 120
