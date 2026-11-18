"""An issue run carries the Claude Code session that asked for it onto
everything it starts (SC-SESSION-SCOPE, owner decision 2026-10-14).

The sc plugin considers only work tagged with its own session for `/sc check`,
`/sc attach --all` and the watch. A run's caller writes no metadata, so
`RunCreate.claude_session` carries the tag; the run stores it and copies it
onto its planner task, its compiled workflow (every step task), each CI-fix
round and its merge workflow -- so a session's own fix rounds count as its
own. Each beside the control: a run with no tag starts nothing tagged.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import uuid

import pytest

from swarm_api import issueci, issueruns, sessiontag
from swarm_api.schemas import RunCreate

from .conftest import auth_header
from .test_issue_runs import (  # noqa: F401 -- fixtures, used by name
    REF,
    _approve,
    _create,
    _docs,
    _planned,
    _run,
    api_context,
    forge_tokens,
    github,
)

HEAD = "b" * 40


def _sid() -> str:
    return str(uuid.uuid4())


def test_a_run_takes_a_session_id_and_refuses_anything_else():
    sid = _sid()
    assert RunCreate(issue=REF, claude_session=sid).claude_session == sid
    assert RunCreate(issue=REF).claude_session is None
    for junk in ("short", "has a space", "x" * 200, "../etc"):
        with pytest.raises(ValueError):
            RunCreate(issue=REF, claude_session=junk)


def test_the_run_and_its_planner_carry_the_session(client, db, objects):
    sid = _sid()
    created = _create(client, claude_session=sid)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    assert run["claude_session"] == sid
    planner = db.docs[f"tasks/{run['planner_task_id']}"]
    assert planner["metadata"]["claude_session"] == sid
    assert _run(client, run["id"]).json()["run"]["claude_session"] == sid
    stored = issueruns.IssueRun.from_firestore(db.docs[f"issue_runs/{run['id']}"])
    assert stored.claude_session == sid
    assert db.docs[f"issue_runs/{run['id']}"]["metadata"] == {"claude_session": sid}
    served = client.get(f"/v1/tasks/{run['planner_task_id']}", headers=auth_header("alice")).json()["task"]
    assert served["claude_session"] == sid


def test_the_compiled_workflow_carries_the_runs_session_on_every_step(client, db, objects):
    sid = _sid()
    run = _planned(client, db, objects, claude_session=sid)
    approved = _approve(client, run["id"], run["plan_digest"]).json()["run"]
    steps = [t for t in _docs(db, "tasks").values() if t.get("workflow_id") == approved["workflow_id"]]
    assert steps and all(t["metadata"]["claude_session"] == sid for t in steps)
    served = client.get(f"/v1/workflows/{approved['workflow_id']}", headers=auth_header("alice")).json()
    assert served["workflow"]["claude_session"] == sid


def test_a_run_with_no_session_starts_nothing_tagged(client, db, objects):
    run = _planned(client, db, objects)
    assert run["claude_session"] is None
    approved = _approve(client, run["id"], run["plan_digest"]).json()["run"]
    for task in _docs(db, "tasks").values():
        assert sessiontag.KEY not in (task.get("metadata") or {}), task["id"]
    served = client.get(f"/v1/workflows/{approved['workflow_id']}", headers=auth_header("alice")).json()
    assert served["workflow"]["claude_session"] is None


def _stored_run(client, db, objects, sid):
    run = _planned(client, db, objects, claude_session=sid)
    approved = _approve(client, run["id"], run["plan_digest"]).json()["run"]
    stored = issueruns.IssueRun.from_firestore(db.docs[f"issue_runs/{approved['id']}"])
    stored.pr_task_id = next(t["id"] for t in _docs(db, "tasks").values()
                             if t.get("workflow_id") == approved["workflow_id"])
    return stored


@pytest.mark.parametrize("tagged", [True, False], ids=["tagged", "control-untagged"])
def test_the_runs_ci_fix_rounds_and_merge_workflow_inherit_its_session(client, db, objects, tagged):
    sid = _sid() if tagged else None
    stored = _stored_run(client, db, objects, sid)
    fix = issueci.ci_fix_workflow(stored, 1, "a red excerpt", HEAD)
    merge = issueci.merge_workflow(stored, stored.pr_task_id, HEAD)
    for spec in (fix, merge):
        assert spec.metadata.get(sessiontag.KEY) == sid
