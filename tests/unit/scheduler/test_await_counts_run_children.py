"""The await sweep counts a parent's run children (lane AU6-WIRE; part of #1933).

docs/design/agent-autonomy.md §3.6: a running agent may start child issue
runs and child workflows, recorded in swarm-api's `child_runs` ledger, and
"waiting is today's": the parent parks CHILDREN_INCOMPLETE and is restarted
when every child has ended. The scheduler's await sweep read only `tasks`, so
a parent waiting on run children alone was promoted on the next drain, with
its runs still going.

Decided 2026-10-22 08:59Z under BGD's overnight rule: a ledger row that has
not ended -- HELD, STARTING, or STARTED with its run or workflow not terminal
-- counts as outstanding, exactly as an unfinished agent child does, read in
the parent's tenant only (invariant 9).

Driven through `Scheduler.drain()` over the in-memory Firestore, with the
production `SchedulerStore`. No credentials, no emulator.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.conftest import (  # noqa: E402
    RecordingDispatcher,
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from control_plane.fakes import FakeFirestore  # noqa: E402

from swarm_api import children as api_children  # noqa: E402
from swarm_api import issueruns as api_issueruns  # noqa: E402
from swarm_api import store as api_store  # noqa: E402
from swarm_common.states import ParkReason  # noqa: E402

from scheduler import children as children_mod  # noqa: E402
from scheduler.dispatch import BackendRouter  # noqa: E402
from scheduler.loop import Scheduler  # noqa: E402
from scheduler.metrics import SchedulerMetrics  # noqa: E402
from scheduler.store import SchedulerStore  # noqa: E402

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
PARENT = "task_parent"


@pytest.fixture
def db() -> FakeFirestore:
    store = FakeFirestore()
    # A global pool at 0, so admission leases nothing: what is measured is
    # the await sweep alone.
    seed_pool(store, "global", hard_limit=0)
    seed_tenant(store, "eng")
    seed_tenant(store, "research")
    doc = seed_task(
        store, task_id=PARENT, tenant_id="eng", state="PARKED",
        park_reason=ParkReason.CHILDREN_INCOMPLETE.value, next_eligible_at=NOW,
    )
    doc["attempt_count"] = 1
    return store


def _drain(db: FakeFirestore) -> str:
    settings = scheduler_settings()
    dispatcher = RecordingDispatcher()
    Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(),
        now=lambda: NOW,
    ).drain()
    return db.docs[f"tasks/{PARENT}"]["state"]


def _row(db: FakeFirestore, child_id: str, state: str, *, tenant: str = "eng",
         run_id: str | None = None, workflow_id: str | None = None) -> dict:
    """A `child_runs` ledger row in the shape swarm-api writes it."""
    doc = {
        "child_id": child_id, "tenant_id": tenant, "parent_task_id": PARENT,
        "kind": "workflow" if workflow_id else "issue_run", "state": state,
        "run_id": run_id, "workflow_id": workflow_id, "after_merged": None,
        "created_at": NOW, "updated_at": NOW,
    }
    db.docs[f"child_runs/{child_id}"] = doc
    return doc


def _run(db: FakeFirestore, run_id: str, state: str, *, tenant: str = "eng") -> dict:
    doc = {"id": run_id, "tenant_id": tenant, "state": state}
    db.docs[f"issue_runs/{run_id}"] = doc
    return doc


def _workflow(db: FakeFirestore, workflow_id: str, state: str, *, tenant: str = "eng") -> dict:
    doc = {"workflow_id": workflow_id, "tenant_id": tenant, "state": state}
    db.docs[f"workflows/{workflow_id}"] = doc
    return doc


def test_a_parent_with_one_live_run_child_stays_awaiting_until_the_run_ends(db):
    _row(db, "crun_a", "STARTED", run_id="run_a")
    run = _run(db, "run_a", "RUNNING")
    # MUTATION: drop the child_runs read from `children_of` (or ignore
    # `ChildList.runs` in `decide_await`) and a parent with no agent children
    # is promoted here, its run still going.
    assert _drain(db) == "PARKED"

    run["state"] = "CHECKING"
    # MUTATION: count only RUNNING as live and a run reading its CI promotes it.
    assert _drain(db) == "PARKED"

    run["state"] = "DONE"
    # MUTATION: count every STARTED row as outstanding whatever its run's
    # state and the parent waits for ever.
    assert _drain(db) == "READY"


@pytest.mark.parametrize("state", ["HELD", "STARTING"])
def test_a_row_that_has_not_started_yet_is_outstanding(db, state):
    row = _row(db, "crun_b", state)
    # MUTATION: count only STARTED rows and a HELD `after_merged` run, or one
    # mid-start, lets its parent go before it ever ran.
    assert _drain(db) == "PARKED"

    row["state"] = "CANCELLED"
    # MUTATION: treat a CANCELLED (or FAILED) row as live and the parent of a
    # run that will never start waits for ever.
    assert _drain(db) == "READY"


def test_a_workflow_child_is_outstanding_until_its_workflow_is_terminal(db):
    _row(db, "crun_w", "STARTED", workflow_id="wf_1")
    workflow = _workflow(db, "wf_1", "RUNNING")
    # MUTATION: read only `run_id` and a STARTED workflow row reads as ended.
    assert _drain(db) == "PARKED"

    workflow["state"] = "SUCCEEDED"
    # MUTATION: compare against the run states and a SUCCEEDED workflow is live.
    assert _drain(db) == "READY"


def test_a_run_child_of_another_tenant_is_never_counted(db):
    # Another tenant's row naming this parent: a forged or misfiled ledger
    # row, which must not hold eng's parent (invariant 9).
    _row(db, "crun_x", "STARTED", tenant="research", run_id="run_x")
    _run(db, "run_x", "RUNNING", tenant="research")
    _row(db, "crun_h", "HELD", tenant="research")
    # MUTATION: query child_runs without the tenant filter (and drop the
    # per-row tenant check) and research's rows keep eng's parent parked.
    assert _drain(db) == "READY"


def test_a_run_of_another_tenant_named_by_an_eng_row_is_not_read_as_live(db):
    # eng's row names a run that is research's: its state is never read
    # across the tenant boundary, and the row reads as ended (`missing`),
    # as swarm-api's own listing reads it.
    _row(db, "crun_y", "STARTED", run_id="run_y")
    _run(db, "run_y", "RUNNING", tenant="research")
    # MUTATION: drop the run document's tenant check and research's RUNNING
    # run holds eng's parent.
    assert _drain(db) == "READY"


def test_an_agent_child_and_a_run_child_must_both_end(db):
    child = seed_task(db, task_id="task_c1", tenant_id="eng", state="SUCCEEDED")
    child.update({"parent_task_id": PARENT, "parent_attempt_id": "att_1",
                  "result_summary": {"artifacts": []}})
    _row(db, "crun_a", "STARTED", run_id="run_a")
    run = _run(db, "run_a", "FIXING")
    # MUTATION: let the run children replace the agent children's answer
    # instead of adding to it, and either one alone settles the parent.
    assert _drain(db) == "PARKED"
    run["state"] = "FAILED"
    assert _drain(db) == "READY"


def test_decide_await_names_the_outstanding_run_children():
    rows = children_mod.ChildList([], runs=("crun_a",))
    decision = children_mod.decide_await(rows)
    # MUTATION: leave run children out of `outstanding` and this is settled.
    assert decision.outstanding == ("crun_a",)
    assert not decision.settled
    # A plain list (no ledger read) still decides as before.
    assert children_mod.decide_await([]).settled


def test_the_restated_ledger_constants_are_swarm_apis():
    """This image does not carry swarm-api, so the names are restated; this
    holds them equal to the originals."""
    # MUTATION: rename any one on either side and this fails.
    assert children_mod.CHILD_RUNS == api_children.CHILD_RUNS
    assert children_mod.STARTED == api_children.STARTED
    assert children_mod.LEDGER_ENDED == api_children.LEDGER_ENDED
    assert children_mod.ISSUE_RUNS == api_issueruns.RUNS_COLLECTION
    assert children_mod.WORKFLOWS == api_store.WORKFLOWS
    assert children_mod.TERMINAL_RUN_STATES == frozenset(
        s.value for s in api_issueruns.TERMINAL_RUN_STATES
    )
