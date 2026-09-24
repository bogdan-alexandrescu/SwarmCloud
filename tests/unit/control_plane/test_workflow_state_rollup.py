"""Workflow state: derived from the steps, written back, drift reported.

THE DEFECT THESE COVER. Nothing ever advanced `Workflow.state`. `create_workflow`
set it once and `cancel_workflow` only touched `cancel_requested`, so every
workflow read QUEUED forever while its steps ran, failed and finished. The web UI
worked around it by computing its own rollup; anything reading the API did not.

Every test below fails against that code: the derivation did not exist, the
write did not exist, and there was no drift check to report the two records
disagreeing.

The rules that are NOT obvious and are therefore pinned here rather than left to
the implementation:

  * a step that could not be read makes the whole answer UNKNOWN. Never
    "SUCCEEDED because the failures did not load".
  * a live step outranks a failed one. A workflow with a sibling still burning a
    lease is RUNNING, not FAILED.
  * a disagreement is reported even after it is repaired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import PENDING_STATES, TERMINAL_STATES, TaskState

from swarm_api.rollup import (
    UNKNOWN,
    WorkflowRollups,
    derive_for,
    drift_of,
    effective_state,
)
from swarm_api.store import Store

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_workflow(
    *,
    workflow_id: str = "wf_test",
    tenant_id: str = "eng",
    state: TaskState = TaskState.QUEUED,
    steps: list[tuple[str, str | None]],
    cancel_requested: bool = False,
    on_step_failure: str = "fail_workflow",
) -> Workflow:
    """`steps` is [(step_id, task_id_or_None)] in dependency order."""
    return Workflow(
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        created_at=NOW,
        updated_at=NOW,
        state=state,
        submitted_by="alice@saga.xyz",
        steps=[
            WorkflowStep(
                step_id=step_id,
                runner_profile="mock",
                input={},
                depends_on=[],
                task_id=task_id,
            )
            for step_id, task_id in steps
        ],
        on_step_failure=on_step_failure,
        cancel_requested=cancel_requested,
    )


def seed_workflow(db, workflow: Workflow) -> Workflow:
    from swarm_api.codec import workflow_to_firestore

    db.docs[f"workflows/{workflow.workflow_id}"] = workflow_to_firestore(workflow)
    return workflow


def states_of(**pairs: str) -> dict[str, TaskState]:
    return {task_id: TaskState(value) for task_id, value in pairs.items()}


# --------------------------------------------------------------------------
# The derivation itself. Pure -- no db, no clock, no credentials.
# --------------------------------------------------------------------------

def test_every_step_succeeded_derives_succeeded():
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2")])
    rollup = derive_for(workflow, states_of(t1="SUCCEEDED", t2="SUCCEEDED"))

    assert rollup.state == TaskState.SUCCEEDED.value
    assert rollup.complete is True
    assert rollup.reason == "all_steps_succeeded"
    assert rollup.counts == {"SUCCEEDED": 2}


def test_a_mixed_terminal_set_derives_failed_not_cancelled():
    """SUCCEEDED + FAILED + CANCELLED -> FAILED.

    This is `wf_bcdc9180e4fb4a209f31`, the workflow that read QUEUED in
    production while holding exactly these three step states.

    FAILED rather than CANCELLED because the cancellations are the CONSEQUENCE:
    `scheduler/loop.py` cancels the dependents of a failed parent with "an
    upstream workflow step did not succeed", and under `fail_workflow` it
    cancels every step that has not started. Reporting CANCELLED would hand an
    operator the second fault instead of the first.
    """
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2"), ("c", "t3")])
    rollup = derive_for(
        workflow, states_of(t1="SUCCEEDED", t2="FAILED", t3="CANCELLED")
    )

    assert rollup.state == TaskState.FAILED.value
    assert rollup.reason == "worst_terminal_step_failed"
    assert rollup.counts == {"SUCCEEDED": 1, "FAILED": 1, "CANCELLED": 1}


def test_dead_lettered_outranks_failed():
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2")])
    rollup = derive_for(workflow, states_of(t1="FAILED", t2="DEAD_LETTERED"))

    assert rollup.state == TaskState.DEAD_LETTERED.value


def test_every_step_cancelled_derives_cancelled():
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2")])
    rollup = derive_for(workflow, states_of(t1="CANCELLED", t2="CANCELLED"))

    assert rollup.state == TaskState.CANCELLED.value


@pytest.mark.parametrize("live", ["LEASED", "DISPATCHED", "STARTING", "RUNNING"])
def test_a_workflow_with_one_live_step_does_not_read_terminal(live):
    """Invariant 3's vocabulary, at the workflow level.

    All four of these hold capacity, so all four mean the workflow is still
    running -- and crucially it must not read terminal even when a sibling has
    already finished.
    """
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2")])
    rollup = derive_for(workflow, states_of(t1="SUCCEEDED", t2=live))

    assert rollup.state == TaskState.RUNNING.value
    assert rollup.terminal is False
    assert rollup.reason == "steps_hold_capacity"


def test_a_live_sibling_outranks_a_failed_step():
    """A failure does NOT end a workflow whose other branch is still burning a lease.

    docs/workflows.md is explicit that a running step is not killed when a
    sibling fails, so reporting FAILED here would tell an operator the work had
    stopped while a container was still costing money.
    """
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2")])
    rollup = derive_for(workflow, states_of(t1="FAILED", t2="RUNNING"))

    assert rollup.state == TaskState.RUNNING.value
    assert rollup.counts["FAILED"] == 1


def test_an_unreadable_step_yields_unknown_not_a_cheerful_answer():
    """The bug class this repository keeps producing, pointed at a derivation.

    Two steps SUCCEEDED and one whose task could not be read. The tempting
    answer is SUCCEEDED. It is also the wrong one: the unread step may be the
    failure.
    """
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2"), ("c", "t3")])
    rollup = derive_for(
        workflow, states_of(t1="SUCCEEDED", t2="SUCCEEDED"), absent=["t3"]
    )

    assert rollup.state == UNKNOWN
    assert rollup.complete is False
    assert rollup.reason == "step_tasks_missing"
    assert rollup.unreadable_steps == ["c"]
    assert effective_state(rollup) == UNKNOWN


def test_a_step_that_was_never_read_is_distinguished_from_one_that_is_missing():
    """"We stopped reading" and "the document is not there" are different faults."""
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2")])

    unread = derive_for(workflow, states_of(t1="SUCCEEDED"))
    assert unread.reason == "step_tasks_unread"

    missing = derive_for(workflow, states_of(t1="SUCCEEDED"), absent=["t2"])
    assert missing.reason == "step_tasks_missing"


def test_a_step_with_no_task_is_not_started_not_unknown():
    """`unstarted` and `unknown` must not render alike -- the UI makes the same
    distinction in `stepState`, and the server must agree with it."""
    workflow = make_workflow(steps=[("a", "t1"), ("b", None)])
    rollup = derive_for(workflow, states_of(t1="SUCCEEDED"))

    assert rollup.complete is True
    assert rollup.state == TaskState.QUEUED.value
    assert rollup.reason == "steps_not_started"
    assert rollup.unstarted_steps == ["b"]


def test_a_stepless_workflow_is_unknown_not_succeeded():
    """"All of nothing succeeded" is the purest form of the cheerful answer.

    `validate_dag` rejects a stepless submission, so reaching this means the
    document is corrupt -- which is a thing to say, not a thing to smooth over.
    """
    workflow = make_workflow(steps=[])
    rollup = derive_for(workflow, {})

    assert rollup.state == UNKNOWN
    assert rollup.reason == "no_steps"


def test_pending_precedence_prefers_the_step_closest_to_running():
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2"), ("c", "t3")])

    ready = derive_for(workflow, states_of(t1="SUCCEEDED", t2="PARKED", t3="READY"))
    assert ready.state == TaskState.READY.value

    parked = derive_for(workflow, states_of(t1="SUCCEEDED", t2="PARKED", t3="QUEUED"))
    assert parked.state == TaskState.PARKED.value


def test_a_failed_step_with_dependents_still_parked_is_not_yet_terminal():
    """The instant between a step failing and the scheduler cancelling its children.

    The workflow is genuinely not over -- the next drain will cancel the parked
    dependents -- so PARKED is the truthful answer and `counts` carries the
    failure for anyone who needs it sooner.
    """
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2")])
    rollup = derive_for(workflow, states_of(t1="FAILED", t2="PARKED"))

    assert rollup.state == TaskState.PARKED.value
    assert rollup.terminal is False
    assert rollup.counts["FAILED"] == 1


def test_cancel_requested_does_not_by_itself_make_a_workflow_cancelled():
    """`cancel_requested` is a REQUEST, not an outcome.

    `Store.request_cancel` terminates only idle tasks immediately; one holding
    capacity keeps its lease until the worker or the reconciler releases it. So
    between the request and the release the workflow really is still running and
    a container really is still costing money. Folding the boolean into the state
    would report the work as stopped while it had not stopped.
    """
    workflow = make_workflow(steps=[("a", "t1")], cancel_requested=True)
    rollup = derive_for(workflow, states_of(t1="RUNNING"))

    assert rollup.state == TaskState.RUNNING.value
    assert workflow.cancel_requested is True

    landed = derive_for(workflow, states_of(t1="CANCELLED"))
    assert landed.state == TaskState.CANCELLED.value


def test_on_step_failure_does_not_change_the_derivation():
    """The setting acts on the STEPS, never on the derivation.

    Since 2026-09-24 the scheduler honours `fail_workflow` by cancelling every
    step that has not started (test_on_step_failure.py). The derivation then
    reaches FAILED from those step states alone. It must not reach it by
    reading the setting: a step still RUNNING holds a lease under either
    setting, because the scheduler never kills a live step. A derivation that
    reported FAILED because the caller ASKED for fail_workflow would be
    describing intent while a container was still costing money.
    """
    args = dict(steps=[("a", "t1"), ("b", "t2")])
    fail = make_workflow(on_step_failure="fail_workflow", **args)
    cont = make_workflow(on_step_failure="continue", **args)
    states = states_of(t1="FAILED", t2="RUNNING")

    assert derive_for(fail, states).state == derive_for(cont, states).state


def test_the_ranking_covers_every_state_the_frozen_contract_defines():
    """Guards the one way this file can go quietly wrong.

    A terminal state added to `swarm_common.states` and not to the severity
    ranking would fall off the end and be ranked BELOW SUCCEEDED -- a workflow
    containing it would read SUCCEEDED. `rollup._check_coverage` refuses to
    import in that case; this is the test that says so out loud.
    """
    from swarm_api import rollup as mod

    assert TERMINAL_STATES <= set(mod._TERMINAL_SEVERITY)
    assert PENDING_STATES <= set(mod._PENDING_PRECEDENCE)
    mod._check_coverage()  # must not raise


# --------------------------------------------------------------------------
# The drift check
# --------------------------------------------------------------------------

def test_agreement_is_reported_as_agreement():
    workflow = make_workflow(state=TaskState.SUCCEEDED, steps=[("a", "t1")])
    rollup = derive_for(workflow, states_of(t1="SUCCEEDED"))

    assert drift_of(rollup, workflow.state)["agrees"] is True


def test_a_deliberate_disagreement_is_detected():
    """The stored value says QUEUED; the steps say the workflow finished."""
    workflow = make_workflow(state=TaskState.QUEUED, steps=[("a", "t1")])
    rollup = derive_for(workflow, states_of(t1="SUCCEEDED"))
    drift = drift_of(rollup, workflow.state)

    assert drift["agrees"] is False
    assert drift["stored"] == "QUEUED"
    assert drift["derived"] == "SUCCEEDED"


def test_an_incomplete_read_is_not_evidence_of_drift():
    """Modelled on the accounting-drift panel in Holders.tsx and its central
    caution: a delta computed over a truncated page is not evidence.

    `agrees` is None -- the two records were not compared, they merely failed to
    be. Reporting False here would manufacture a finding out of a failed read.
    """
    workflow = make_workflow(state=TaskState.QUEUED, steps=[("a", "t1")])
    rollup = derive_for(workflow, {}, absent=["t1"])
    drift = drift_of(rollup, workflow.state)

    assert drift["agrees"] is None
    assert drift["derived"] == UNKNOWN


# --------------------------------------------------------------------------
# The write-back, against the in-memory Firestore
# --------------------------------------------------------------------------

def test_the_written_value_and_the_derived_value_agree_after_a_normal_run(db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="t2", tenant_id="eng", state="SUCCEEDED")
    workflow = seed_workflow(
        db, make_workflow(steps=[("a", "t1"), ("b", "t2")], state=TaskState.QUEUED)
    )
    rollups = WorkflowRollups(store=Store(db))

    first = rollups.for_workflow_from_tasks(
        workflow, [Store(db).get_task("eng", tid) for tid in ("t1", "t2")]
    )
    assert first.drift["agrees"] is False
    assert first.written is True
    assert db.docs["workflows/wf_test"]["state"] == "SUCCEEDED"

    # Read again with what Firestore now holds: the two records agree and the
    # second pass writes nothing, so the steady state costs no Firestore writes.
    reread = Store(db).get_workflow("eng", "wf_test")
    second = rollups.for_workflow_from_tasks(
        reread, [Store(db).get_task("eng", tid) for tid in ("t1", "t2")]
    )
    assert second.drift["agrees"] is True
    assert second.written is False


def test_a_repaired_disagreement_is_still_reported_as_a_disagreement(db):
    """The repair must leave a trace, or it is a silent resolution."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="eng", state="FAILED")
    workflow = seed_workflow(db, make_workflow(steps=[("a", "t1")]))

    result = WorkflowRollups(store=Store(db)).for_workflow(
        workflow, states_of(t1="FAILED")
    )

    assert result.written is True
    assert result.drift["agrees"] is False       # the verdict AT THE READ
    assert result.drift["repaired"] is True
    assert result.drift["stored"] == "QUEUED"    # the evidence, not the fix


def test_an_unknown_state_is_never_written(db):
    """A write of UNKNOWN would make the document permanently unreadable:
    `workflow_from_dict` decodes the field with `TaskState(...)`."""
    seed_tenant(db, "eng")
    workflow = seed_workflow(db, make_workflow(steps=[("a", "t1")]))

    result = WorkflowRollups(store=Store(db)).for_workflow(workflow, {}, absent=["t1"])

    assert result.rollup.state == UNKNOWN
    assert result.written is False
    assert db.docs["workflows/wf_test"]["state"] == "QUEUED"
    # And the document is still decodable, which is the thing that would break.
    assert Store(db).get_workflow("eng", "wf_test").state is TaskState.QUEUED


def test_an_incomplete_rollup_is_refused_even_if_the_drift_verdict_says_write(db):
    """Pins the completeness guard on its own, independently of `drift_of`.

    `drift_of` returns None for an incomplete rollup today, so the "agrees is
    False" guard in `_persist` already blocks this path and the completeness
    check looks redundant. It is not: it is what keeps the refusal true if
    `drift_of` is ever changed to report a verdict over a partial read. A
    mutation that removed the completeness check survived the rest of this file
    precisely because every other test reaches it through `drift_of`, so this one
    builds the inconsistent state by hand.
    """
    from swarm_api.rollup import RollupResult, derive_for

    seed_tenant(db, "eng")
    workflow = seed_workflow(db, make_workflow(steps=[("a", "t1")]))
    rollup = derive_for(workflow, {}, absent=["t1"])
    assert rollup.state == UNKNOWN

    forged = RollupResult(
        workflow=workflow,
        rollup=rollup,
        # What a future `drift_of` that compared over a partial read would say.
        drift={"stored": "QUEUED", "derived": UNKNOWN, "agrees": False,
               "reason": rollup.reason, "repaired": False},
    )
    assert WorkflowRollups(store=Store(db))._persist(forged) is False
    assert db.docs["workflows/wf_test"]["state"] == "QUEUED"


def test_drift_is_counted_before_it_is_repaired(db):
    """A repaired disagreement still moves the counter, so a systemic problem is
    visible on a dashboard even though every instance is fixed a moment later."""
    from swarm_api.metrics import ApiMetrics

    seed_tenant(db, "eng")
    workflow = seed_workflow(db, make_workflow(steps=[("a", "t1")]))
    metrics = ApiMetrics()

    WorkflowRollups(store=Store(db), metrics=metrics).for_workflow(
        workflow, states_of(t1="SUCCEEDED")
    )

    rendered = metrics.render()[0].decode()
    assert 'swarm_api_workflow_state_drift_total{direction="disagree"} 1.0' in rendered


def test_the_step_read_budget_degrades_to_unknown_not_to_a_wrong_answer(db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="t2", tenant_id="eng", state="FAILED")
    workflow = make_workflow(steps=[("a", "t1"), ("b", "t2")])

    read = Store(db).workflow_step_states("eng", [workflow], budget=1)

    assert read.unread == ["t2"]
    assert read.reads == 1
    rollup = derive_for(workflow, read.states, absent=read.absent)
    assert rollup.state == UNKNOWN
    assert rollup.reason == "step_tasks_unread"


def test_a_step_task_owned_by_another_tenant_reads_as_absent(db):
    """Invariant 9 does not get an exception for a derived field."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="research", state="SUCCEEDED")
    workflow = make_workflow(steps=[("a", "t1")])

    read = Store(db).workflow_step_states("eng", [workflow])

    assert read.states == {}
    assert read.absent == ["t1"]
    assert derive_for(workflow, read.states, absent=read.absent).state == UNKNOWN


def test_the_sweep_skips_terminal_workflows_and_reports_truncation(db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="eng", state="RUNNING")
    seed_workflow(
        db,
        make_workflow(workflow_id="wf_live", steps=[("a", "t1")], state=TaskState.QUEUED),
    )
    done = make_workflow(
        workflow_id="wf_done", steps=[("a", "t9")], state=TaskState.SUCCEEDED
    )
    done.created_at = NOW - timedelta(minutes=5)
    seed_workflow(db, done)

    _, report = WorkflowRollups(store=Store(db)).sweep("eng", limit=10)

    # `wf_done` is terminal: its steps cannot move again, so re-reading its tasks
    # every sweep would make the sweep cost grow with history rather than work.
    assert report.examined == 1
    assert report.written == 1
    assert report.truncated is False
    assert db.docs["workflows/wf_live"]["state"] == "RUNNING"
    assert db.docs["workflows/wf_done"]["state"] == "SUCCEEDED"

    _, truncated = WorkflowRollups(store=Store(db)).sweep("eng", limit=1)
    assert truncated.truncated is True


# --------------------------------------------------------------------------
# Through the real API, over the real routes
# --------------------------------------------------------------------------

def test_the_api_serves_the_derived_state_and_the_stored_one_beside_it(db, client):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="eng", state="SUCCEEDED",
              workflow_id="wf_test")
    seed_task(db, task_id="t2", tenant_id="eng", state="FAILED", workflow_id="wf_test")
    seed_task(db, task_id="t3", tenant_id="eng", state="CANCELLED",
              workflow_id="wf_test")
    seed_workflow(
        db, make_workflow(steps=[("a", "t1"), ("b", "t2"), ("c", "t3")])
    )

    response = client.get("/v1/workflows/wf_test", headers=auth_header("alice"))
    assert response.status_code == 200
    workflow = response.json()["workflow"]

    # The reproduction of the reported defect: stored says QUEUED, the steps say
    # the workflow failed, and `state` -- what every consumer reads -- is now the
    # latter.
    assert workflow["state"] == "FAILED"
    assert workflow["stored_state"] == "QUEUED"
    assert workflow["state_source"] == "derived"
    assert workflow["drift"]["agrees"] is False
    assert workflow["drift"]["repaired"] is True
    assert workflow["rollup"]["counts"] == {
        "SUCCEEDED": 1, "FAILED": 1, "CANCELLED": 1
    }

    # ... and it is now queryable, which is the other half of the decision.
    assert db.docs["workflows/wf_test"]["state"] == "FAILED"


def test_the_list_route_derives_too(db, client):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="eng", state="RUNNING", workflow_id="wf_test")
    seed_workflow(db, make_workflow(steps=[("a", "t1")]))

    body = client.get("/v1/workflows", headers=auth_header("alice")).json()

    assert body["workflows"][0]["state"] == "RUNNING"
    assert body["workflows"][0]["stored_state"] == "QUEUED"
    assert body["rollup_report"]["disagreed"] == 1
    assert body["rollup_report"]["step_read_budget_exhausted"] is False


def test_the_api_says_unknown_when_a_step_task_cannot_be_read(db, client):
    """The step names a task that is not there. The two steps that ARE there
    both succeeded, so the cheerful answer is SUCCEEDED."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="eng", state="SUCCEEDED",
              workflow_id="wf_test")
    seed_task(db, task_id="t2", tenant_id="eng", state="SUCCEEDED",
              workflow_id="wf_test")
    seed_workflow(
        db, make_workflow(steps=[("a", "t1"), ("b", "t2"), ("c", "t_gone")])
    )

    workflow = client.get(
        "/v1/workflows/wf_test", headers=auth_header("alice")
    ).json()["workflow"]

    assert workflow["state"] == UNKNOWN
    assert workflow["stored_state"] == "QUEUED"
    assert workflow["drift"]["agrees"] is None
    assert workflow["rollup"]["unreadable_steps"] == ["c"]
    # Nothing was written: an UNKNOWN must never overwrite a real value.
    assert db.docs["workflows/wf_test"]["state"] == "QUEUED"


def test_the_admin_sweep_converges_a_workflow_nobody_looked_at(db, client):
    seed_tenant(db, "eng")
    seed_task(db, task_id="t1", tenant_id="eng", state="SUCCEEDED")
    seed_workflow(db, make_workflow(steps=[("a", "t1")]))

    body = client.post(
        "/v1/admin/workflows/rollup?tenant_id=eng", headers=auth_header("root")
    ).json()

    assert body["report"]["examined"] == 1
    assert body["report"]["written"] == 1
    assert body["drifted"] == [
        {
            "workflow_id": "wf_test",
            "stored": "QUEUED",
            "derived": "SUCCEEDED",
            "agrees": False,
            "repaired": True,
            "reason": "all_steps_succeeded",
        }
    ]
    assert db.docs["workflows/wf_test"]["state"] == "SUCCEEDED"


def test_the_admin_sweep_is_admin_only(db, client):
    seed_tenant(db, "eng")
    response = client.post(
        "/v1/admin/workflows/rollup?tenant_id=eng", headers=auth_header("alice")
    )
    assert response.status_code == 403


def test_one_tenant_cannot_read_anothers_workflow_state(db, client):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_task(db, task_id="t1", tenant_id="eng", state="SUCCEEDED")
    seed_workflow(db, make_workflow(steps=[("a", "t1")]))

    assert client.get(
        "/v1/workflows/wf_test", headers=auth_header("bob")
    ).status_code == 404
