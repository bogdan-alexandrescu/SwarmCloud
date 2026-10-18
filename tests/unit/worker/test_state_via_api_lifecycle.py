"""A whole attempt on the state routes: `state/open` first, the agent after.

Lane ISO-WORKER-CORE (docs/design/tenant-settings-integrity.md §4.3, §4.7,
§6; part of #1457). The production `Worker` runs the genuine mock runner with
its control plane swapped for `RemoteControlPlane`, whose transport is the
worker's own `ChildApi` answered by swarm-api's real app over its in-memory
Firestore (`state_routes_harness`). Every task, lease, attempt and event write
of the attempt lands there; the worker's own in-memory Firestore keeps only
what has not moved to the routes yet (the tenant read, lane
ISO-WORKER-READS).

  * `state/open` is the first state call, before any other and before the
    agent is spawned;
  * a 409 at `state/open` exits 70 (fenced) without running the agent and
    without another state call (invariant 5).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from agent_worker import lifecycle
from agent_worker.errors import ExitCode

import spec_keys
from control_plane.fakes import FakeFirestore as ApiFirestore
from fakes import ExplodingChildProcess
from state_routes_harness import RoutesOpener, api_client, remote_control
from worker_seeds import TENANT, seed_attempt


def _mirror(worker_db: Any, api_db: ApiFirestore, ids: dict[str, str]) -> dict[str, Any]:
    """The attempt's control-plane documents, where swarm-api reads them,
    the task signed as swarm-api signs it at submission."""
    for path, doc in worker_db.documents.items():
        if path.startswith(("tasks/", "leases/", "pools/", "tenants/")):
            api_db.docs[path] = copy.deepcopy(doc)
    task = api_db.docs[f"tasks/{ids['task_id']}"]
    spec_keys.sign_document(task, ids["task_id"])
    return task


def _switched(worker_factory, db, api_db) -> tuple[Any, RoutesOpener]:
    ids = seed_attempt(db)
    _mirror(db, api_db, ids)
    worker, _config, _exporter = worker_factory(sign_spec=False)
    opener = RoutesOpener(api_client(api_db))
    worker.control = remote_control(opener, task_id=ids["task_id"],
                                    attempt_id=ids["attempt_id"], lease_id=ids["lease_id"])
    return worker, opener


def test_state_open_comes_first_and_the_agent_only_after_running(
    db, worker_factory, monkeypatch
):
    api_db = ApiFirestore()
    worker, opener = _switched(worker_factory, db, api_db)
    spawned_after: list[list[str]] = []
    real = lifecycle.ChildProcess

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            spawned_after.append(opener.routes())
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "ChildProcess", Recording)

    code = worker.run()

    assert code == ExitCode.OK, code
    routes = opener.routes()
    assert routes[0] == "open" and routes.count("open") == 1
    assert "child-key" not in routes  # the nonce is spent once, by state/open
    assert spawned_after, "the agent never ran"
    before_agent = spawned_after[0]
    assert before_agent[0] == "open"
    # DISPATCHED, STARTING and RUNNING were all written before the agent existed.
    assert before_agent.count("transition") == 3
    task = api_db.docs["tasks/task_1"]
    assert task["state"] == "SUCCEEDED" and task["tenant_id"] == TENANT
    assert api_db.docs["leases/lease_1"]["released_at"] is not None
    # One event per step of the walk and one for the finish, each written by
    # the route with its own write (`request_id` in its detail). The spec
    # check's own RUNNING event, which the direct path writes too, is not one.
    walk = [e["type"] for p, e in sorted(api_db.docs.items())
            if p.startswith("tasks/task_1/events/") and "request_id" in e["detail"]]
    assert sorted(walk) == ["dispatched", "running", "starting", "succeeded"]
    # The direct path wrote nothing: its task is as it was seeded.
    assert db.documents["tasks/task_1"]["state"] == "LEASED"


def test_a_409_at_state_open_exits_fenced_without_running_the_agent(
    db, worker_factory, monkeypatch
):
    api_db = ApiFirestore()
    worker, opener = _switched(worker_factory, db, api_db)
    api_db.docs["tasks/task_1"]["current_generation"] = 2
    before = copy.deepcopy(api_db.docs)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    code = worker.run()

    assert code == ExitCode.GENERATION_FENCED
    assert opener.routes() == ["open"]
    assert api_db.docs == before


@pytest.mark.parametrize("state", ["SUCCEEDED", "CANCELLED"])
def test_a_terminal_task_at_state_open_is_never_run(db, worker_factory, monkeypatch, state):
    api_db = ApiFirestore()
    worker, opener = _switched(worker_factory, db, api_db)
    api_db.docs["tasks/task_1"]["state"] = state
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    code = worker.run()

    assert code == ExitCode.GENERATION_FENCED
    assert opener.routes() == ["open"]
