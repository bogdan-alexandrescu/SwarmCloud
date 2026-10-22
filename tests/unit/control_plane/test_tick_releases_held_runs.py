"""The run-advance tick releases held child runs (lane AU6-WIRE; part of #1933).

    POST /v1/admin/runs/advance?tenant_id=<t>

docs/design/agent-autonomy.md §3.6: an `after_merged` child issue run is
created HELD -- no run, no planner, no workflow, so no capacity (invariant 1)
-- and "released by the schedule tick when the named run reaches DONE with
its PR merged". AU6-CHILD-RUNS (PR 1950) built the release
(`children.release_held_runs`); nothing called it.

Decided 2026-10-22 08:59Z under BGD's overnight rule: the existing
per-tenant `issue_run_advance` tick calls it after advancing the tenant's
runs, and returns its counts as `held`. So the tick that moves a predecessor
to DONE starts its held successor in the same response.

Drives the real route over the in-memory Firestore. The predecessor's own
advance is stubbed to "CI green, pull request merged" -- reading CI is
`test_issue_run_tick`'s subject, not this one's. No credentials, no network.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import children
from swarm_api.issueruns import IssueRun
from swarm_api.main import create_app
from swarm_api.routes import runs as runs_routes

from .test_child_runs import _chain, _context, _ledger, _runs
from .test_child_runs import github  # noqa: F401 -- a fixture
from .test_child_tasks_api import Attempt, ready_parent, worker_tokens  # noqa: F401
from .conftest import seed_tenant

SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_TOKEN = "token-sweeper"


@pytest.fixture
def ctx(db, worker_tokens, group_map, objects, github):  # noqa: F811
    tokens = {**worker_tokens,
              SWEEPER_TOKEN: {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}}
    return _context(db, tokens, group_map, objects, github, rollup_sweeper_users=(SWEEPER,))


@pytest.fixture
def client(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def parent(client, db) -> Attempt:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    attempt = Attempt()
    ready_parent(client, db, attempt)
    return attempt


def _tick(client: TestClient, tenant_id: str = "eng") -> dict[str, Any]:
    response = client.post(f"/v1/admin/runs/advance?tenant_id={tenant_id}",
                           headers={"Authorization": f"Bearer {SWEEPER_TOKEN}"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def ends(db, monkeypatch):
    """Make the tick's advance of a run end it DONE, merged or not: what a
    green CI read and a merge (or no merge) would leave."""
    merged: dict[str, bool] = {"value": True}

    def _advance(ctx, tenant_id, run):
        doc = db.docs[f"issue_runs/{run.id}"]
        doc.update({"state": "DONE",
                    "pull_request": {"number": 3, "merged": merged["value"]}})
        return IssueRun.from_firestore(doc)

    monkeypatch.setattr(runs_routes, "_advance_state", _advance)
    monkeypatch.setattr(runs_routes, "sync_issue", lambda ctx, run: run)
    return merged


def _row(db, child: dict) -> dict[str, Any]:
    return _ledger(db)[f"{children.CHILD_RUNS}/{child['child_id']}"]


def test_one_tick_merges_the_predecessor_and_starts_its_held_run(client, db, parent, ends):
    a, b = _chain(client, db, parent)
    assert _row(db, b)["state"] == children.HELD
    assert len(_runs(db)) == 1

    body = _tick(client)

    # The predecessor moved on this tick ...
    assert body["report"]["moved"] == 1
    assert db.docs[f"issue_runs/{a['run_id']}"]["state"] == "DONE"
    # ... and its successor started on the same one.
    # MUTATION: drop the `release_held_runs` call from `advance_runs` and B
    # stays HELD with no run, and `held` is missing from the response.
    assert body["held"]["started"] == 1, body
    row = _row(db, b)
    assert row["state"] == children.STARTED and row["run_id"]
    assert len(_runs(db)) == 2
    assert db.docs[f"issue_runs/{row['run_id']}"]["issue"]["number"] == 7
    # MUTATION: release before advancing and the tick reads A still live, so
    # B waits a whole tick; `held` then says waiting, not started.
    assert body["held"]["waiting"] == 0


def test_a_predecessor_done_unmerged_cancels_its_held_run(client, db, parent, ends):
    ends["value"] = False
    a, b = _chain(client, db, parent)

    body = _tick(client)

    assert db.docs[f"issue_runs/{a['run_id']}"]["state"] == "DONE"
    # MUTATION: drop the release call and B is HELD for ever, its parent
    # awaiting a run that can never start.
    assert body["held"]["cancelled"] == 1, body
    row = _row(db, b)
    assert row["state"] == children.CANCELLED
    assert row["error"] == "predecessor_ended_unmerged"
    assert row["run_id"] is None and len(_runs(db)) == 1


def test_another_tenants_tick_leaves_a_held_run_untouched(client, db, parent, monkeypatch):
    a, b = _chain(client, db, parent)
    db.docs[f"issue_runs/{a['run_id']}"].update(
        {"state": "DONE", "pull_request": {"number": 3, "merged": True}})

    body = _tick(client, "research")

    # MUTATION: release under the caller's tenant, or any tenant but the
    # `tenant_id` the tick names, and research's tick starts eng's run.
    assert body["held"]["visited"] == 0, body
    assert _row(db, b)["state"] == children.HELD
    assert len(_runs(db)) == 1

    # eng's own tick still releases it: the row was waiting, not lost.
    monkeypatch.setattr(runs_routes, "sync_issue", lambda ctx, run: run)
    assert _tick(client, "eng")["held"]["started"] == 1
    assert _row(db, b)["state"] == children.STARTED


def test_a_tick_with_nothing_held_says_so(client, db, parent):
    body = _tick(client)
    # A healthy tick with nothing held returns zeros, which is an answer.
    # MUTATION: return `held` only when something was released and this
    # key is missing.
    assert body["held"] == {"visited": 0, "started": 0, "cancelled": 0, "failed": 0,
                            "waiting": 0}
