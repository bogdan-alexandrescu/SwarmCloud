"""What cancelling ONE step actually does to the rest of a workflow.

WHY THIS FILE EXISTS. The console is gaining a stop control (B28), and a
confirmation dialog is a set of CLAIMS about what is about to happen. Those
claims were about to be written from what `Workflow.on_step_failure` reads like
rather than from what the platform does, so they are pinned here first and the
copy is written from these assertions.

THE FINDING THIS FILE RECORDS, and it is the answer to "with on_step_failure
'continue', can one step be cancelled without failing the whole workflow?":

    `on_step_failure` IS NOT READ BY ANYTHING.

    It is declared on the frozen `Workflow` (models.py:347), accepted by the
    API (`schemas.py:87`, a `Literal["fail_workflow", "continue"]`), written by
    `service.py:388` and round-tripped by `codec.py:524`/`:561` -- and a grep
    across `apps/` finds no reader. The scheduler does not consult it, the
    reconciler does not consult it, `rollup.py` does not consult it. So the two
    settings behave identically, and `test_on_step_failure_continue_changes_
    nothing_today` pins that rather than letting a dialog imply otherwise.

WHAT ACTUALLY HAPPENS, which is what the copy says:

  * THE STOP IS A REQUEST, NOT AN EVENT, for any step that holds capacity.
    `Store.request_cancel` sets `cancel_requested` and leaves the state alone,
    because releasing the lease from the API would decrement a pool that a live
    container still occupies. The step stays DISPATCHED/RUNNING until the
    worker sees the flag at its next heartbeat, at which point
    `lifecycle` terminates the child, CHECKPOINTS, uploads artifacts and logs,
    and finishes the attempt as CANCELLED (lifecycle.py:611-625);
  * ONLY THEN do its DEPENDENTS fall over, because
    `scheduler.loop._FAILED_PARENT_STATES` contains CANCELLED -- the state, not
    the flag -- and both the admission path (loop.py:295) and the dependency
    sweep (loop.py:494) test the parent's STATE. In the window between the
    button and the worker, nothing downstream has moved at all;
  * a step that does NOT depend on it is untouched and keeps running;
  * the workflow's derived state stays RUNNING while any sibling still holds
    capacity -- `rollup.derive` tests capacity before terminal severity -- and
    settles on the WORST terminal step afterwards, so a FAILED sibling outranks
    the cancellation.
"""

from __future__ import annotations

from swarm_api.rollup import derive_for
from swarm_common.states import TaskState

from .conftest import auth_header, seed_pool, seed_task, seed_tenant


def submit_fan_out(client, *, on_step_failure: str = "fail_workflow") -> dict:
    """Two independent steps and a join, which is the shape the finding needs.

    `left` and `right` share no dependency; `join` depends on both. Cancelling
    `left` therefore has one node that must die with it and one that must not.
    """
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "on_step_failure": on_step_failure,
            "steps": [
                {"step_id": "left", "runner_profile": "mock"},
                {"step_id": "right", "runner_profile": "mock"},
                {
                    "step_id": "join",
                    "runner_profile": "mock",
                    "depends_on": ["left", "right"],
                },
            ],
        },
    )
    assert created.status_code == 201, created.text
    workflow = created.json()["workflow"]
    return {
        "workflow_id": workflow["workflow_id"],
        "steps": {s["step_id"]: s["task_id"] for s in workflow["steps"]},
    }


# --------------------------------------------------------------------------
# What the dialog is allowed to say
# --------------------------------------------------------------------------

def worker_acts_on_the_flag(db, task_id: str) -> None:
    """What `lifecycle` does when it sees `cancel_requested` mid-run.

    Terminate the child, checkpoint, upload, `control.finish(CANCELLED)` --
    lifecycle.py:611-625. Only the resulting STATE is modelled here, because
    the state is the only part the scheduler's dependency rules read.
    """
    db.docs[f"tasks/{task_id}"]["state"] = "CANCELLED"


def test_nothing_downstream_moves_until_the_worker_has_acted(
    client, db, make_scheduler
) -> None:
    """THE WINDOW THE DIALOG MUST NOT PAPER OVER.

    A step holding capacity is FLAGGED, not stopped. Its state is unchanged,
    and every dependency rule downstream tests the parent's STATE rather than
    the flag -- so between pressing the button and the worker's next heartbeat,
    the dependent step has not moved and the agent is still running and still
    spending. A dialog that said "this will cancel the following steps" in the
    present tense would be describing a future that has not happened yet.
    """
    seed_pool(db, "global", hard_limit=10)
    wf = submit_fan_out(client)
    scheduler = make_scheduler()
    scheduler.drain()

    client.post(f"/v1/tasks/{wf['steps']['left']}/cancel", headers=auth_header("alice"))
    scheduler.drain()

    left = db.docs[f"tasks/{wf['steps']['left']}"]
    assert left["state"] == "DISPATCHED"
    assert left["cancel_requested"] is True
    assert db.docs[f"tasks/{wf['steps']['join']}"]["state"] == "PARKED"


def test_cancelling_one_step_cancels_the_steps_that_depend_on_it(
    client, db, make_scheduler
) -> None:
    """THE SENTENCE THE CONFIRMATION LEADS WITH, once the worker has stopped.

    `join` is PARKED on DEPENDENCY_INCOMPLETE. Once `left` actually reaches
    CANCELLED, the next drain's dependency sweep finds a parent in
    `_FAILED_PARENT_STATES` and cancels it with "an upstream workflow step did
    not succeed".
    """
    seed_pool(db, "global", hard_limit=10)
    wf = submit_fan_out(client)
    scheduler = make_scheduler()
    scheduler.drain()
    assert db.docs[f"tasks/{wf['steps']['join']}"]["state"] == "PARKED"

    cancelled = client.post(
        f"/v1/tasks/{wf['steps']['left']}/cancel", headers=auth_header("alice")
    )
    assert cancelled.status_code == 200, cancelled.text
    worker_acts_on_the_flag(db, wf["steps"]["left"])
    scheduler.drain()

    assert db.docs[f"tasks/{wf['steps']['join']}"]["state"] == "CANCELLED"
    assert (
        db.docs[f"tasks/{wf['steps']['join']}"]["last_error"]
        == "an upstream workflow step did not succeed"
    )


def test_a_step_that_does_not_depend_on_it_keeps_running(
    client, db, make_scheduler
) -> None:
    """THE SECOND SENTENCE, and the one that makes the control usable.

    If cancelling one agent stopped the whole run, an operator would never
    press it to stop a single agent burning tokens on the wrong thing -- which
    is the case the control exists for.
    """
    seed_pool(db, "global", hard_limit=10)
    wf = submit_fan_out(client)
    scheduler = make_scheduler()
    scheduler.drain()
    assert db.docs[f"tasks/{wf['steps']['right']}"]["state"] == "DISPATCHED"

    client.post(f"/v1/tasks/{wf['steps']['left']}/cancel", headers=auth_header("alice"))
    scheduler.drain()

    assert db.docs[f"tasks/{wf['steps']['right']}"]["state"] == "DISPATCHED"
    assert db.docs[f"tasks/{wf['steps']['right']}"]["cancel_requested"] is False


def test_on_step_failure_continue_changes_nothing_today(
    client, db, make_scheduler
) -> None:
    """THE FINDING, pinned so the dialog cannot imply a setting that is inert.

    Same workflow, same cancellation, `on_step_failure: "continue"`. If a
    reader of this repository ever implements the field, this test fails and
    the confirmation copy in `Workflows.tsx` and `AgentDetail.tsx` has to be
    revisited in the same change -- which is the point of pinning it.
    """
    seed_pool(db, "global", hard_limit=10)
    wf = submit_fan_out(client, on_step_failure="continue")
    assert (
        db.docs[f"workflows/{wf['workflow_id']}"]["on_step_failure"] == "continue"
    )
    scheduler = make_scheduler()
    scheduler.drain()

    client.post(f"/v1/tasks/{wf['steps']['left']}/cancel", headers=auth_header("alice"))
    worker_acts_on_the_flag(db, wf["steps"]["left"])
    scheduler.drain()

    # Identical to `fail_workflow`: the dependent dies, the independent lives.
    assert db.docs[f"tasks/{wf['steps']['join']}"]["state"] == "CANCELLED"
    assert db.docs[f"tasks/{wf['steps']['right']}"]["state"] == "DISPATCHED"


def test_the_workflow_stays_running_while_a_sibling_still_holds_capacity(
    client, db, make_scheduler
) -> None:
    """The header must not read CANCELLED while an agent is still spending.

    `rollup.derive` tests capacity-holding states BEFORE terminal severity for
    exactly this reason, and the stop dialog says "the run keeps going" on the
    strength of it.
    """
    from swarm_api.codec import workflow_from_dict

    seed_pool(db, "global", hard_limit=10)
    wf = submit_fan_out(client)
    scheduler = make_scheduler()
    scheduler.drain()
    client.post(f"/v1/tasks/{wf['steps']['left']}/cancel", headers=auth_header("alice"))
    scheduler.drain()

    workflow = workflow_from_dict(db.docs[f"workflows/{wf['workflow_id']}"])
    states = {
        step.task_id: TaskState(db.docs[f"tasks/{step.task_id}"]["state"])
        for step in workflow.steps
    }
    rollup = derive_for(workflow, states)

    assert rollup.state == TaskState.RUNNING.value
    assert rollup.reason == "steps_hold_capacity"


# --------------------------------------------------------------------------
# Immediate versus requested -- the other thing the dialog must not overstate
# --------------------------------------------------------------------------

def test_a_step_holding_capacity_is_flagged_rather_than_stopped(
    client, db, make_scheduler
) -> None:
    """`released_immediately: false` is the dialog's "it stops shortly" case.

    Releasing the lease from the API would decrement a pool that a live
    container still occupies, so the flag is set and the worker acts on it at
    its next heartbeat. A dialog promising an instant stop would be wrong by
    however long that heartbeat is.
    """
    seed_pool(db, "global", hard_limit=10)
    wf = submit_fan_out(client)
    make_scheduler().drain()
    assert db.docs[f"tasks/{wf['steps']['left']}"]["state"] == "DISPATCHED"

    body = client.post(
        f"/v1/tasks/{wf['steps']['left']}/cancel", headers=auth_header("alice")
    ).json()

    assert body["released_immediately"] is False
    assert body["task"]["state"] == "DISPATCHED"
    assert body["task"]["cancel_requested"] is True


def test_a_step_holding_no_capacity_is_cancelled_outright(client, db) -> None:
    """`released_immediately: true`: nothing ran, so there is nothing to keep."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_idle", tenant_id="eng", state="QUEUED")

    body = client.post(
        "/v1/tasks/task_idle/cancel", headers=auth_header("alice")
    ).json()

    assert body["released_immediately"] is True
    assert body["task"]["state"] == "CANCELLED"


def test_cancelling_an_already_terminal_step_is_a_conflict(client, db) -> None:
    """The control has to be hidden or refused on a finished step.

    A 409 rather than a cheerful 200: telling someone they stopped an agent
    that had already succeeded is a claim about work that did not happen.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")

    response = client.post("/v1/tasks/task_done/cancel", headers=auth_header("alice"))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["state"] == "SUCCEEDED"


def test_another_tenant_cannot_stop_this_tenants_step(client, db) -> None:
    """The stop control is a WRITE, and the scope is what makes it safe.

    `tasks.py`'s own docstring records that cancelling was reachable across a
    tenant-id collision until `tenant_scope` was applied here. The console is
    about to put a button on this route, so the boundary is re-pinned beside
    the button's own tests.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_eng", tenant_id="eng", state="RUNNING")

    response = client.post("/v1/tasks/task_eng/cancel", headers=auth_header("bob"))

    assert response.status_code == 404, response.text
    assert db.docs["tasks/task_eng"]["cancel_requested"] is False
