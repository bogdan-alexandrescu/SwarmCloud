"""The reconciler never promotes a root step that waits for a pull request (SPEED-D3).

A workflow submitted with `after: {pr: N}` parks its root steps on
DEPENDENCY_INCOMPLETE with no `depends_on`, carrying `metadata.after_prs`
(swarm_api.prgate). The stall check's rule (a) reads such a step as "it
depends on no step" and, past the grace, promotes it -- which would start a
workflow whose pull request has not merged. So a step whose gate is not
`merged` is not a DEPENDENCIES_MET stall; one whose gate is `merged` still is,
as a backstop for the scheduler's sweep.

Offline: the worker suite's in-memory Firestore, no backend executions.
"""

from __future__ import annotations

from datetime import timedelta

from fakes import FakeFirestore

from reconciler.detect import WorkflowStallKind
from swarm_common.states import ParkReason, TaskState

from test_workflow_stall import WF, config, detect, reconciler, seed_task, seed_workflow  # noqa: F401

KEY = "after_prs"


def gated_root(db: FakeFirestore, state: str) -> None:
    seed_workflow(db, [("implement", "task_root", ())], state=TaskState.PARKED)
    seed_task(db, "task_root", TaskState.PARKED, ago=timedelta(hours=1),
              park_reason=ParkReason.DEPENDENCY_INCOMPLETE)
    doc = db.doc("tasks/task_root")
    doc["metadata"] = {KEY: {"repository": "octo-org/widget-shop", "pull_requests": [41],
                             "merged": [], "state": state}}
    db.seed("tasks/task_root", doc)


def _met(db, config) -> list:  # noqa: F811
    return [s for s in detect(db, config) if s.kind is WorkflowStallKind.DEPENDENCIES_MET]


def test_a_root_waiting_for_a_pull_request_is_not_a_stall(db, config):  # noqa: F811
    gated_root(db, "waiting")

    assert _met(db, config) == []
    reconciler(db, config).run_once()
    assert db.doc("tasks/task_root")["state"] == TaskState.PARKED.value


def test_a_root_whose_pull_request_closed_is_not_promoted_either(db, config):  # noqa: F811
    """The scheduler dead-letters it; promoting it would run the workflow."""
    gated_root(db, "closed")
    assert _met(db, config) == []


def test_a_root_whose_pull_request_merged_is_still_the_backstops(db, config):  # noqa: F811
    """The control: the same root, released, is found as before."""
    gated_root(db, "merged")
    (stall,) = _met(db, config)
    assert stall.task_id == "task_root" and stall.workflow_id == WF
