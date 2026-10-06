"""`swarm_workflows` reads one page; `swarm_trouble` leaves dependency waits out.

Owner decision 2026-10-06 (observer proposals P4 + P5), from two measurements:

* `swarm_workflows` paged through 4 pages of 50 workflows to list 7 running
  ones and still answered `complete: false, incomplete_because: stopped after
  4 pages of 50 workflows`. The bridge now asks `GET /v1/workflows?active=true`,
  which the route serves from an indexed query on the stored state, so the
  running set is one page however long the SUCCEEDED/CANCELLED history is.
  Newer FAILED or DEAD_LETTERED history, which the query must keep because a
  retry can revive it, is walked past rather than called the running set.
* `swarm_trouble` listed steps PARKED on DEPENDENCY_INCOMPLETE as trouble. They
  hold no capacity (invariant 1) and are the ordinary shape of a workflow whose
  earlier steps are still running: nothing is wrong with them. Every other park
  reason -- CREDENTIAL_MISSING, quota, cooldown -- is still a finding.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from swarm_api.codec import workflow_to_firestore
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState
from swarm_mcp import sc, server
from swarm_mcp.client import SwarmClient
from swarm_mcp.render import Snapshot, Style, find_trouble

# The real application and SwarmClient's real transport into it.
from test_against_the_real_api import DAG, api, db, swarm  # noqa: F401

WIDE = Style(width=200, color=False, unicode=False)


def _finished_history(db, count: int, newest: datetime, state: str = "SUCCEEDED",  # noqa: F811
                      prefix: str = "done") -> None:
    """`count` finished workflows of tenant eng, all NEWER than `newest`, so a
    newest-first walk meets every one of them before a running workflow."""
    for i in range(count):
        at = newest + timedelta(minutes=count - i)
        task_id = f"task_{prefix}_{i:03d}"
        db.docs[f"tasks/{task_id}"] = {
            "id": task_id, "tenant_id": "eng", "state": state,
            "created_at": at, "updated_at": at, "runner_profile": "mock",
            "resource_class": "standard", "input": {}, "metadata": {},
            "submitted_by": "alice@saga.xyz", "workflow_id": f"wf_{prefix}_{i:03d}",
        }
        workflow = Workflow(
            workflow_id=f"wf_{prefix}_{i:03d}", tenant_id="eng", created_at=at, updated_at=at,
            state=TaskState(state), submitted_by="alice@saga.xyz",
            steps=[WorkflowStep(step_id="only", runner_profile="mock", input={},
                                depends_on=[], task_id=task_id)],
        )
        db.docs[f"workflows/wf_{prefix}_{i:03d}"] = workflow_to_firestore(workflow)


def test_the_running_set_is_one_page_behind_a_long_finished_history(swarm, db, monkeypatch):  # noqa: F811
    """Three running workflows behind 220 finished ones: before, the bridge
    read 4 pages of 50 finished rows, listed none of the three and said
    `complete: false`. Now one request lists all three, complete."""
    running = [json.loads(server._call(swarm, "swarm_workflow", json.loads(json.dumps(DAG))))
               for _ in range(3)]
    _finished_history(db, 220, datetime.now(timezone.utc) + timedelta(minutes=1))

    asked: list[str] = []
    real = SwarmClient.workflows

    def counting(self, **kwargs):
        asked.append(json.dumps(kwargs, sort_keys=True))
        return real(self, **kwargs)

    monkeypatch.setattr(SwarmClient, "workflows", counting)
    listing = sc.running_workflows(swarm)

    assert len(asked) == 1, asked
    assert listing["complete"] is True and "incomplete_because" not in listing
    assert {w["workflow_id"] for w in listing["workflows"]} == {
        r["workflow_id"] for r in running
    }


def test_newer_failed_and_dead_lettered_history_is_walked_past(swarm, db, monkeypatch):  # noqa: F811
    """The query must keep stored FAILED and DEAD_LETTERED workflows (a retry
    can revive them), so newer ones come back from it and derive terminal.
    Three running workflows behind 60 DEAD_LETTERED and 30 FAILED newer ones:
    the first indexed page holds none of the three, and stopping there
    answered `count: 0` with a false "more than 50 are unfinished". The walk
    goes on past the short page and lists all three, complete."""
    running = [json.loads(server._call(swarm, "swarm_workflow", json.loads(json.dumps(DAG))))
               for _ in range(3)]
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    _finished_history(db, 30, base, state="FAILED", prefix="failed")
    _finished_history(db, 60, base + timedelta(minutes=31), state="DEAD_LETTERED", prefix="dead")
    _finished_history(db, 220, base + timedelta(minutes=200))

    asked: list[str] = []
    real = SwarmClient.workflows

    def counting(self, **kwargs):
        asked.append(json.dumps(kwargs, sort_keys=True))
        return real(self, **kwargs)

    monkeypatch.setattr(SwarmClient, "workflows", counting)
    listing = sc.running_workflows(swarm)

    assert 1 < len(asked) <= sc.WORKFLOW_LIST_PAGES, asked
    assert listing["complete"] is True and "incomplete_because" not in listing
    assert listing["count"] == 3
    assert {w["workflow_id"] for w in listing["workflows"]} == {
        r["workflow_id"] for r in running
    }


class _Fake:
    workflows = SwarmClient.workflows


def test_the_bridge_asks_for_active_workflows():
    seen: list[str] = []

    class _Client(_Fake):
        def request(self, method, path, **_):
            seen.append(path)
            return {"workflows": [], "next_page_token": None, "tenant_id": "eng",
                    "filter": {"active": True, "states": None, "stored_states": ["RUNNING"]}}

    sc.running_workflows(_Client())
    (path,) = seen
    assert "active=true" in path.split("?", 1)[1].split("&")


def _indexed_client(rows_per_page: int, seen: list[str]):
    cursor = "more"  # a position, as the route's keyset token is
    class _Client(_Fake):
        def request(self, method, path, **_):
            if path.startswith("/v1/workflows?"):
                seen.append(path)
                rows = [{"workflow_id": f"wf_{len(seen)}_{i}", "state": "RUNNING", "steps": []}
                        for i in range(rows_per_page)]
                return {"workflows": rows, "next_page_token": cursor, "tenant_id": "eng",
                        "filter": {"active": True, "states": None, "stored_states": ["RUNNING"]}}
            return {"workflow": {"workflow_id": path.rsplit("/", 1)[-1], "state": "RUNNING",
                                 "steps": []}, "tasks": []}

    return _Client()


def test_an_indexed_page_full_of_unfinished_workflows_reads_no_second_page():
    """The route said it filtered in the query and its page is FULL of
    unfinished workflows: more than a page of them is running, which is said,
    not walked."""
    seen: list[str] = []
    listing = sc.running_workflows(_indexed_client(sc.WORKFLOW_PAGE_SIZE, seen))
    assert len(seen) == 1
    assert listing["complete"] is False
    assert "was full" in listing["incomplete_because"]


def test_a_short_indexed_page_with_a_token_is_paged_on():
    """A short page with a token means rows the stored state kept (FAILED,
    DEAD_LETTERED) derived terminal and were dropped: older running workflows
    may still be behind them, so the walk goes on, and the reason says it was
    the page budget that stopped it, not a full page."""
    seen: list[str] = []
    listing = sc.running_workflows(_indexed_client(1, seen))
    assert len(seen) == sc.WORKFLOW_LIST_PAGES
    assert listing["count"] == sc.WORKFLOW_LIST_PAGES
    assert listing["complete"] is False
    assert f"stopped after {sc.WORKFLOW_LIST_PAGES} pages" in listing["incomplete_because"]


def test_a_deployment_without_the_filter_is_still_paged():
    """An older API serves no `filter` and ignores `active`: the bridge keeps
    walking pages, as before, rather than call one page of history complete."""
    seen: list[str] = []

    class _Client(_Fake):
        def request(self, method, path, **_):
            seen.append(path)
            return {"workflows": [], "next_page_token": "more", "tenant_id": "eng"}

    listing = sc.running_workflows(_Client())
    assert len(seen) == sc.WORKFLOW_LIST_PAGES
    assert listing["complete"] is False


# --------------------------------------------------------------------------
# swarm_trouble
# --------------------------------------------------------------------------


def _parked(task_id: str, reason: str) -> dict:
    return {"id": task_id, "state": "PARKED", "park_reason": reason}


def test_trouble_omits_dependency_parks_and_keeps_every_other_reason():
    snap = Snapshot(tasks=[
        _parked("t1", "DEPENDENCY_INCOMPLETE"),
        _parked("t2", "DEPENDENCY_INCOMPLETE"),
        _parked("t3", "CREDENTIAL_MISSING"),
        _parked("t4", "PROVIDER_QUOTA_EXHAUSTED"),
    ])
    parked = [f.what for f in find_trouble(snap, WIDE) if f.where == "parked"]
    assert len(parked) == 2, parked
    assert any("credential missing" in d for d in parked)
    assert any("provider quota exhausted" in d for d in parked)
    assert not any("dependency" in d for d in parked)


def test_only_dependency_parks_are_no_trouble():
    snap = Snapshot(tasks=[_parked("t1", "DEPENDENCY_INCOMPLETE")])
    assert [f for f in find_trouble(snap, WIDE) if f.where == "parked"] == []


def test_the_tool_text_says_dependency_waits_are_left_out():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_trouble"]
    assert "DEPENDENCY_INCOMPLETE" in tool["description"]
