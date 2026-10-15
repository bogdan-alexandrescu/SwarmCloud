"""J5's hold: a likely-duplicate plan waits for a person (lane CTXQ-JW).

Owner decision 2026-10-14 (context-quality.md §8 Q5 A): when the planner's
worker judged, through Jev, that the plan duplicates an in-flight lane at
p >= 0.85, an AUTO-approved plan is held for a human. The worker writes that
judgement to `plan-advisory.json`; the planner post-step
(`routes/runs.py::_from_planner`) reads it and sets the run's
`plan_approval` to `required`, so a person approves it like any required
plan. Jev never decides.

And it is advisory: no file, a malformed one, or one with no hold moves the
run exactly as before Jev existed.
"""

from __future__ import annotations

import json

import pytest

from swarm_api.routes import runs as runs_routes

from .test_issue_run_tick import (  # noqa: F401 -- fixtures are used by name
    _stored,
    _tick,
    api_context,
    client,
    github,
)
from .test_issue_runs import BUCKET, PLAN, _create, _docs, _finish_planner

HOLD = {"schema": "swarm.plan-advisory/v1", "advisory": True, "hold_for_human": True,
        "duplicate": {"title": "Sort widgets by name", "ref": "swarm/sort", "p": 0.91},
        "coverage": {"requirements": 1, "flagged": []}}


def _advise(db, objects, run: dict, advisory) -> None:
    task_id = run["planner_task_id"]
    doc = db.docs[f"tasks/{task_id}"]
    raw = advisory if isinstance(advisory, str) else json.dumps(advisory)
    key = f"tenants/{doc['tenant_id']}/tasks/{task_id}/attempts/att_1/artifacts/{runs_routes.PLAN_ADVISORY_FILE}"
    objects.put(key, raw)
    doc["result_summary"]["artifacts"].append(
        {"name": runs_routes.PLAN_ADVISORY_FILE, "bytes": len(raw.encode()),
         "uri": f"gs://{BUCKET}/{key}"})


def test_a_likely_duplicate_holds_an_auto_plan_for_a_person(client, db, objects):
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, PLAN)
    _advise(db, objects, run, HOLD)

    _tick(client)

    stored = _stored(db, run["id"])
    assert stored["state"] == "PLANNED"
    assert stored["plan_approval"] == "required"
    # Invariant 1: a held plan creates no demand.
    assert not _docs(db, "workflows")


@pytest.mark.parametrize("advisory", [
    None,
    "{not json",
    {**HOLD, "hold_for_human": False, "duplicate": None},
    {**HOLD, "advisory": False},
    {**HOLD, "duplicate": "swarm/sort"},
])
def test_no_hold_moves_the_auto_run_exactly_as_before(client, db, objects, advisory):
    run = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, run, PLAN)
    if advisory is not None:
        _advise(db, objects, run, advisory)

    _tick(client)

    stored = _stored(db, run["id"])
    assert stored["state"] == "RUNNING"
    assert stored["plan_approval"] == "auto"


def test_a_required_run_is_untouched_by_the_advisory(client, db, objects):
    run = _create(client).json()["run"]
    _finish_planner(db, objects, run, PLAN)
    _advise(db, objects, run, HOLD)

    _tick(client)

    stored = _stored(db, run["id"])
    assert stored["state"] == "PLANNED" and stored["plan_approval"] == "required"
    assert [h["to"] for h in stored["history"]] == ["PLANNING", "PLANNED"]
