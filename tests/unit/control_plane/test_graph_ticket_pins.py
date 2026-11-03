"""A graph ticket pins the version it names on the task's running workflow (#3116).

The owner chose "record-pins" on 3 November 2026: swarm-api records the
`index_sha` each live graph ticket names on the workflow document
(`graph_pins`, `graph_pin_repos`), index retention keeps those versions past
both of its rules while the workflow runs (test_repository_index_retention.py),
and the write that settles the workflow clears them. Drives the real route
over the in-memory Firestore; every key is built at runtime (CLAUDE.md).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api.repoindex import pinned_commits
from swarm_rollup import GRAPH_PIN_REPOS, GRAPH_PINS, settled_unpin
from swarm_common.states import TaskState

from .test_graph_ticket_api import (  # noqa: F401 - fixtures are used by name
    OTHER_SHA,
    REPO_ID,
    SHA,
    TASK,
    ask,
    clock,
    groups,
    ldb,
    make,
    people,
    ready,
    refused,
    relay,
    version,
)

WORKFLOW = "wf_graph_pins"


def _workflow(db, *, state: str = "RUNNING", tenant: str = "eng") -> None:
    now = datetime.now(timezone.utc)
    db.docs[f"workflows/{WORKFLOW}"] = {
        "workflow_id": WORKFLOW, "tenant_id": tenant, "state": state,
        "steps": [{"step_id": "work", "runner_profile": "mock", "depends_on": [],
                   "task_id": TASK}],
        "on_step_failure": "fail_workflow", "cancel_requested": False,
        "created_at": now, "updated_at": now,
    }
    db.docs[f"tasks/{TASK}"]["workflow_id"] = WORKFLOW


def _workflow_writes(db) -> int:
    return sum(1 for ops in db.commits for _op, path in ops if path.startswith("workflows/"))


def test_a_ticket_pins_its_commit_on_the_running_workflow(ready, ldb):
    client, attempt = ready
    _workflow(ldb)
    assert ask(client, attempt).status_code == 200
    doc = ldb.docs[f"workflows/{WORKFLOW}"]
    # MUTATION: a mint that writes no pin leaves the version to retention's two rules.
    assert doc[GRAPH_PINS] == {REPO_ID: [SHA]}
    assert doc[GRAPH_PIN_REPOS] == [REPO_ID]
    assert pinned_commits(ldb, "eng", REPO_ID) == {SHA}


def test_a_second_commit_is_added_and_a_renewal_writes_nothing(ready, ldb):
    client, attempt = ready
    _workflow(ldb)
    version(ldb, OTHER_SHA)
    assert ask(client, attempt).status_code == 200
    written = _workflow_writes(ldb)
    assert written == 1
    assert ask(client, attempt).status_code == 200
    # MUTATION: re-writing an existing pin turns every renewal into a write on the
    # workflow document every step's transition also writes.
    assert _workflow_writes(ldb) == written
    assert ask(client, attempt, index_sha=OTHER_SHA).status_code == 200
    assert ldb.docs[f"workflows/{WORKFLOW}"][GRAPH_PINS] == {REPO_ID: sorted([SHA, OTHER_SHA])}


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"])
def test_a_settled_workflow_gets_no_pin(ready, ldb, state):
    client, attempt = ready
    _workflow(ldb, state=state)
    assert ask(client, attempt).status_code == 200
    # MUTATION: pinning after the settle wrote its clear leaves a pin nothing removes.
    assert GRAPH_PINS not in ldb.docs[f"workflows/{WORKFLOW}"]


def test_another_tenants_workflow_is_never_written(ready, ldb):
    client, attempt = ready
    _workflow(ldb, tenant="research")
    assert ask(client, attempt).status_code == 200
    # MUTATION: dropping the tenant check writes into another workspace's workflow.
    assert GRAPH_PINS not in ldb.docs[f"workflows/{WORKFLOW}"]


def test_a_task_without_a_workflow_pins_nothing(ready, ldb):
    client, attempt = ready
    assert ask(client, attempt).status_code == 200
    assert not [path for path in ldb.docs if path.startswith("workflows/")]


def test_a_pin_that_cannot_be_written_is_a_503_and_no_ticket(ready, ldb, monkeypatch):
    client, attempt = ready
    _workflow(ldb)

    def broken(*_args, **_kwargs):
        raise RuntimeError("the transaction could not commit")

    monkeypatch.setattr(type(ldb), "transaction", broken)
    # MUTATION: serving the ticket anyway names a version retention may sweep.
    body = refused(ask(client, attempt), 503)
    assert body["code"] == "graph_ticket_unavailable"


def test_settled_unpin_clears_only_on_a_terminal_state():
    pinned = {GRAPH_PINS: {REPO_ID: [SHA]}, GRAPH_PIN_REPOS: [REPO_ID]}
    clear = {GRAPH_PINS: {}, GRAPH_PIN_REPOS: []}
    # MUTATION: clearing on a live state drops a running workflow's pins.
    for live in ("RUNNING", "PARKED", "QUEUED", TaskState.LEASED):
        assert settled_unpin(live, pinned) == {}
    for done in ("SUCCEEDED", "FAILED", TaskState.CANCELLED, "DEAD_LETTERED"):
        assert settled_unpin(done, pinned) == clear
        assert settled_unpin(done) == clear
        assert settled_unpin(done, {}) == {}
    assert settled_unpin("UNKNOWN", pinned) == {}


def test_the_apis_settle_write_clears_the_pins(ldb):
    from swarm_api.store import Store

    _workflow_doc = {"workflow_id": WORKFLOW, "tenant_id": "eng", "state": "RUNNING",
                     GRAPH_PINS: {REPO_ID: [SHA]}, GRAPH_PIN_REPOS: [REPO_ID]}
    ldb.docs[f"workflows/{WORKFLOW}"] = dict(_workflow_doc)
    store = Store(ldb)
    store.set_workflow_state(WORKFLOW, TaskState.PARKED)
    assert ldb.docs[f"workflows/{WORKFLOW}"][GRAPH_PINS] == {REPO_ID: [SHA]}
    # MUTATION: `set_workflow_state` without `settled_unpin`.
    store.set_workflow_state(WORKFLOW, TaskState.SUCCEEDED)
    assert ldb.docs[f"workflows/{WORKFLOW}"][GRAPH_PINS] == {}
    assert ldb.docs[f"workflows/{WORKFLOW}"][GRAPH_PIN_REPOS] == []
