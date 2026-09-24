"""`on_step_failure`, read by the scheduler.

THE DEFECT. `Workflow.on_step_failure` was accepted on submit (`schemas.py`,
a `Literal["fail_workflow", "continue"]`), stored on the workflow document,
served back by the API and advertised by the MCP `swarm_workflow` tool. Nothing
read it. `fail_workflow`, the default, behaved exactly like `continue`: a failed
step took its dependents with it and every independent branch kept starting.
docs/workflows.md said so; the MCP schema said the opposite.

THE DECISION (owner, 2026-09-24), which these tests pin:

  * `fail_workflow`: as soon as any step is FAILED or DEAD_LETTERED, the
    scheduler cancels every step of that workflow that has not started
    (SUBMITTED, QUEUED, READY, PARKED), with an event naming the failed step.
    A step already holding capacity is NOT touched. It is not cancelled, not
    flagged and not written to, and it runs to completion. Invariant 1: nothing
    here may release capacity from under a live worker. The workflow then
    derives FAILED.
  * `continue`: only the transitive dependents of the failed step are
    cancelled. That was already the behaviour under both settings, and
    `test_continue_cancels_only_the_transitive_dependents` confirms it rather
    than assuming it.
  * CANCELLED is not a failure. A step stopped by hand takes its dependents
    with it under both settings and nothing else, so the stop dialog's copy
    (test_cancel_semantics.py) stays true.

RED FIRST. The fail_workflow tests were pushed before the scheduler read the
field, and failed: the independent READY step was DISPATCHED, the step parked
behind a running sibling stayed PARKED, and the workflow derived RUNNING after
every live step had finished. The guards (continue, the running step, another
tenant, a missing workflow document, a stop by hand) could not go red against
that code, because it touched nothing. They exist to hold the fix to its
limits, and were proven by a mutation pushed after the fix.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from .conftest import auth_header, seed_pool, seed_task, seed_tenant

FAIL = "fail_workflow"
CONTINUE = "continue"


def step(step_id: str, *depends_on: str) -> dict:
    body: dict = {"step_id": step_id, "runner_profile": "mock"}
    if depends_on:
        body["depends_on"] = list(depends_on)
    return body


#: Two independent roots besides the one that breaks, so the failure has a
#: dependent (`after_broken`), a not-started independent step (`waiting`,
#: READY), an independent step that is running (`busy`), and a not-started
#: step parked behind the running one (`after_busy`). Each is a case the
#: decision names separately.
FIVE_STEPS = [
    step("broken"),
    step("busy"),
    step("waiting"),
    step("after_busy", "busy"),
    step("after_broken", "broken"),
]


def submit(client, steps: list[dict], *, on_step_failure: str) -> tuple[str, dict[str, str]]:
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={"on_step_failure": on_step_failure, "steps": steps},
    )
    assert created.status_code == 201, created.text
    workflow = created.json()["workflow"]
    assert workflow["on_step_failure"] == on_step_failure
    return workflow["workflow_id"], {s["step_id"]: s["task_id"] for s in workflow["steps"]}


def worker_is_running(db, task_id: str) -> None:
    """What a live attempt looks like to the scheduler: RUNNING, holding a lease."""
    db.docs[f"tasks/{task_id}"].update(
        {
            "state": "RUNNING",
            "current_lease_id": f"lease_{task_id}",
            "current_generation": 1,
            "attempt_count": 1,
            "started_at": datetime.now(timezone.utc),
        }
    )


def worker_finished(db, task_id: str, state: str) -> None:
    """What `control.finish` writes, as far as the scheduler reads it."""
    db.docs[f"tasks/{task_id}"].update(
        {
            "state": state,
            "current_lease_id": None,
            "completed_at": datetime.now(timezone.utc),
        }
    )


def state_of(db, task_id: str) -> str:
    return db.docs[f"tasks/{task_id}"]["state"]


def events_of(db, task_id: str) -> dict[str, dict]:
    prefix = f"tasks/{task_id}/events/"
    return {path: copy.deepcopy(doc) for path, doc in db.docs.items() if path.startswith(prefix)}


def cancel_events(db, task_id: str) -> list[dict]:
    return [e for e in events_of(db, task_id).values() if e["type"] == "cancelled"]


def cancelled_metric(scheduler, reason: str) -> float:
    rendered = scheduler.metrics.render()[0].decode("utf-8")
    needle = f'swarm_scheduler_cancelled_total{{reason="{reason}"}} '
    for line in rendered.splitlines():
        if line.startswith(needle):
            return float(line[len(needle):])
    return 0.0


# --------------------------------------------------------------------------
# fail_workflow
# --------------------------------------------------------------------------

@pytest.mark.parametrize("failure", ["FAILED", "DEAD_LETTERED"])
def test_fail_workflow_cancels_every_step_that_has_not_started(
    client, db, make_scheduler, dispatcher, failure
) -> None:
    """THE DECISION. One step fails; nothing that has not started may start.

    `waiting` shares no dependency with `broken` and was READY, so before this
    change the same drain admitted it. That was the observed defect: the
    default setting promised the workflow would stop and it kept starting work.
    """
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(client, FIVE_STEPS, on_step_failure=FAIL)
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], failure)

    scheduler = make_scheduler()
    report = scheduler.drain()

    assert state_of(db, steps["waiting"]) == "CANCELLED", (
        "an independent READY step started after its workflow had failed"
    )
    assert state_of(db, steps["after_busy"]) == "CANCELLED", (
        "a step parked behind a RUNNING sibling is not started, and must not start"
    )
    assert state_of(db, steps["after_broken"]) == "CANCELLED"
    assert state_of(db, steps["broken"]) == failure
    assert state_of(db, steps["busy"]) == "RUNNING"
    assert dispatcher.dispatched == [], "nothing may be dispatched once the workflow failed"

    # Counted in the drain's own summary and in the metric, which is what PR #31
    # made every cancel path do. The report and the metric must agree.
    assert report.cancelled == 3, report.to_dict()
    assert cancelled_metric(scheduler, "workflow_failed") == 3.0


def test_every_cancel_names_the_step_that_failed(client, db, make_scheduler) -> None:
    """A cancelled step's own record says WHICH step failed, not merely that one did.

    An operator reading `waiting`'s timeline has no dependency edge to follow
    back to `broken`, so the event is the only place that connection exists.
    """
    seed_pool(db, "global", hard_limit=10)
    workflow_id, steps = submit(client, FIVE_STEPS, on_step_failure=FAIL)
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "FAILED")

    make_scheduler().drain()

    for name in ("waiting", "after_busy", "after_broken"):
        events = cancel_events(db, steps[name])
        assert len(events) == 1, (name, events)
        detail = events[0]["detail"]
        assert detail["workflow_id"] == workflow_id
        assert detail["on_step_failure"] == FAIL
        assert detail["failed_steps"] == [
            {"step_id": "broken", "task_id": steps["broken"], "state": "FAILED"}
        ], name
        assert "broken" in db.docs[f"tasks/{steps[name]}"]["last_error"], name


def test_the_workflow_derives_failed_once_its_live_steps_finish(
    client, db, make_scheduler
) -> None:
    """RUNNING while a sibling still holds capacity, then FAILED, never anything else.

    Before this change `waiting` and `after_busy` were started, so the workflow
    derived RUNNING long after the failure and could end SUCCEEDED-with-a-
    failure-in-it rather than FAILED.
    """
    seed_pool(db, "global", hard_limit=10)
    workflow_id, steps = submit(client, FIVE_STEPS, on_step_failure=FAIL)
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "FAILED")
    scheduler = make_scheduler()
    scheduler.drain()

    live = client.get(f"/v1/workflows/{workflow_id}", headers=auth_header("alice"))
    assert live.status_code == 200, live.text
    # `busy` holds a lease: the workflow is not over and a container is still
    # costing money, so it must not read FAILED yet.
    assert live.json()["workflow"]["state"] == "RUNNING"

    worker_finished(db, steps["busy"], "SUCCEEDED")
    scheduler.drain()

    done = client.get(f"/v1/workflows/{workflow_id}", headers=auth_header("alice"))
    workflow = done.json()["workflow"]
    assert workflow["state"] == "FAILED", workflow["rollup"]
    assert workflow["rollup"]["counts"] == {"FAILED": 1, "SUCCEEDED": 1, "CANCELLED": 3}


def test_with_no_step_running_the_workflow_is_failed_after_one_drain(
    client, db, make_scheduler
) -> None:
    seed_pool(db, "global", hard_limit=10)
    workflow_id, steps = submit(
        client,
        [step("broken"), step("waiting"), step("after_waiting", "waiting")],
        on_step_failure=FAIL,
    )
    worker_finished(db, steps["broken"], "FAILED")

    make_scheduler().drain()

    workflow = client.get(
        f"/v1/workflows/{workflow_id}", headers=auth_header("alice")
    ).json()["workflow"]
    assert workflow["state"] == "FAILED", workflow["rollup"]
    assert state_of(db, steps["waiting"]) == "CANCELLED"
    assert state_of(db, steps["after_waiting"]) == "CANCELLED"


def test_a_step_that_fails_mid_drain_stops_its_siblings_in_the_same_drain(
    client, db, make_scheduler, dispatcher
) -> None:
    """"As soon as" includes a failure the drain itself causes.

    `first` is on its last attempt and its dispatch fails, so
    `return_to_ready_after_failed_dispatch` writes FAILED inside this drain.
    `second` comes after it in the same pass. A verdict read before `first`
    failed and then trusted for the rest of the drain would admit `second`.
    """
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(client, [step("first"), step("second")], on_step_failure=FAIL)
    now = datetime.now(timezone.utc)
    # Older, so it is tried first; one attempt left, so this failure is final.
    db.docs[f"tasks/{steps['first']}"].update(
        {"created_at": now - timedelta(minutes=10), "attempt_count": 2, "max_attempts": 3}
    )
    db.docs[f"tasks/{steps['second']}"]["created_at"] = now - timedelta(minutes=1)
    dispatcher.fail_for.add(steps["first"])

    report = make_scheduler().drain()

    assert state_of(db, steps["first"]) == "FAILED"
    assert state_of(db, steps["second"]) == "CANCELLED"
    assert dispatcher.dispatched == []
    assert report.dispatch_failures == 1
    assert report.cancelled == 1


# --------------------------------------------------------------------------
# The limits. These could not be red before the fix: that code touched none
# of these steps. They hold the fix to what the decision allows.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("policy", [FAIL, CONTINUE])
def test_a_running_step_is_untouched_under_either_setting(
    client, db, make_scheduler, policy
) -> None:
    """Invariant 1. Not cancelled, not flagged, not written to, no event.

    Cancelling a RUNNING step from the scheduler would write CANCELLED over a
    task whose lease is still counted in every pool and whose container is
    still running, and nothing would ever release that capacity. The only
    components that may finish a live step are the worker and the reconciler.
    """
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(client, FIVE_STEPS, on_step_failure=policy)
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "FAILED")
    before = copy.deepcopy(db.docs[f"tasks/{steps['busy']}"])
    events_before = events_of(db, steps["busy"])
    pools_before = copy.deepcopy(db.dump("pools/"))

    make_scheduler().drain()

    assert db.docs[f"tasks/{steps['busy']}"] == before
    assert events_of(db, steps["busy"]) == events_before
    assert db.docs[f"tasks/{steps['busy']}"]["cancel_requested"] is False
    if policy == FAIL:
        # Nothing was admitted either, so no pool may have moved at all.
        assert db.dump("pools/") == pools_before


def test_continue_cancels_only_the_transitive_dependents(
    client, db, make_scheduler, dispatcher
) -> None:
    """`continue` is the behaviour both settings used to share. Confirmed here.

    `after_broken` depends on the failure and `after_after_broken` depends on
    that, so both die, the second because its parent is now CANCELLED. `waiting`
    and `after_busy` share nothing with `broken`, so both run.
    """
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(
        client,
        FIVE_STEPS + [step("after_after_broken", "after_broken")],
        on_step_failure=CONTINUE,
    )
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "FAILED")
    scheduler = make_scheduler()

    # Two drains: the sweep reads its PARKED slice once, so a grandchild can
    # see its parent's cancel on this drain or the next, depending on order.
    scheduler.drain()
    scheduler.drain()

    for name in ("after_broken", "after_after_broken"):
        assert state_of(db, steps[name]) == "CANCELLED", name
        assert (
            db.docs[f"tasks/{steps[name]}"]["last_error"]
            == "an upstream workflow step did not succeed"
        ), name
    assert state_of(db, steps["waiting"]) == "DISPATCHED"
    assert state_of(db, steps["after_busy"]) == "PARKED"

    worker_finished(db, steps["busy"], "SUCCEEDED")
    scheduler.drain()

    assert state_of(db, steps["after_busy"]) == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == [
        steps["waiting"], steps["after_busy"]
    ]


def test_a_step_stopped_by_hand_is_not_a_failure(client, db, make_scheduler) -> None:
    """CANCELLED does not trigger fail_workflow. Only FAILED and DEAD_LETTERED do.

    Otherwise pressing stop on one agent would end the whole run, and the stop
    dialog's "the rest of the run keeps going" would be false under the default.
    """
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(client, FIVE_STEPS, on_step_failure=FAIL)
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "CANCELLED")

    make_scheduler().drain()

    assert state_of(db, steps["after_broken"]) == "CANCELLED"
    assert state_of(db, steps["waiting"]) == "DISPATCHED"
    assert state_of(db, steps["after_busy"]) == "PARKED"


def test_a_step_of_another_tenant_is_never_cancelled_by_this_workflow(
    client, db, make_scheduler
) -> None:
    """Invariant 9. A task that names this workflow but belongs to another tenant.

    The API never writes one, so it can only come from a corrupt or forged
    document. The cancel must still stay inside the tenant whose step failed.
    """
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, "research")
    workflow_id, steps = submit(
        client, [step("broken"), step("waiting")], on_step_failure=FAIL
    )
    worker_finished(db, steps["broken"], "FAILED")
    seed_task(db, task_id="task_foreign", tenant_id="research", workflow_id=workflow_id)

    make_scheduler().drain()

    assert state_of(db, steps["waiting"]) == "CANCELLED"
    assert state_of(db, "task_foreign") == "DISPATCHED"
    assert cancel_events(db, "task_foreign") == []


def test_a_workflow_whose_document_is_missing_keeps_the_dependency_rule(
    db, make_scheduler
) -> None:
    """No document, no policy. The scheduler does not guess `fail_workflow`.

    A cancel cannot be undone, so an unreadable policy falls back to the rule
    that needs no policy: the dependents of a failed step are cancelled, and
    nothing else is touched.
    """
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=10)
    seed_task(db, task_id="t_failed", tenant_id="eng", state="FAILED", workflow_id="wf_gone")
    seed_task(db, task_id="t_ready", tenant_id="eng", workflow_id="wf_gone")
    seed_task(
        db,
        task_id="t_child",
        tenant_id="eng",
        state="PARKED",
        park_reason="DEPENDENCY_INCOMPLETE",
        depends_on=("t_failed",),
        workflow_id="wf_gone",
    )

    make_scheduler().drain()

    assert state_of(db, "t_child") == "CANCELLED"
    assert state_of(db, "t_ready") == "DISPATCHED"


# --------------------------------------------------------------------------
# The guarded cancel itself
# --------------------------------------------------------------------------

def test_the_guarded_cancel_refuses_a_step_that_started_after_it_was_read(db) -> None:
    """The race the transaction exists for.

    The sweep reads a step READY, and a concurrent drain leases it before the
    cancel lands. A blind write would put CANCELLED over a LEASED task, and the
    pools would count that lease forever. The cancel re-reads the step inside a
    transaction and writes only if it has still not started.
    """
    from scheduler.codec import task_from_dict
    from scheduler.store import SchedulerStore

    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="t_raced", tenant_id="eng")
    stale = task_from_dict(copy.deepcopy(doc))
    db.docs["tasks/t_raced"].update({"state": "LEASED", "current_lease_id": "lease_raced"})
    before = copy.deepcopy(db.docs["tasks/t_raced"])

    store = SchedulerStore(db)
    assert store.cancel_if_not_started(stale, "a workflow step failed", {}) is False
    assert db.docs["tasks/t_raced"] == before
    assert events_of(db, "t_raced") == {}

    seed_task(db, task_id="t_parked", tenant_id="eng", state="PARKED",
              park_reason="PROVIDER_COOLDOWN")
    parked = task_from_dict(copy.deepcopy(db.docs["tasks/t_parked"]))
    assert store.cancel_if_not_started(parked, "a workflow step failed", {"k": "v"}) is True
    assert state_of(db, "t_parked") == "CANCELLED"
    assert db.docs["tasks/t_parked"]["park_reason"] is None
    (event,) = cancel_events(db, "t_parked")
    assert event["detail"]["from_state"] == "PARKED"
    assert event["detail"]["k"] == "v"


# --------------------------------------------------------------------------
# One vocabulary, and a tool that says what the scheduler does
# --------------------------------------------------------------------------

def test_the_policy_vocabulary_agrees_everywhere_it_is_stated() -> None:
    """Four statements of one vocabulary, none of them in the frozen contract.

    The frozen `Workflow` carries a bare `str` defaulting to "fail_workflow".
    The API restates the set as a Literal, the MCP tool as a JSON-schema enum,
    and the scheduler names the one value it acts on. A rename in any one of
    them would make the scheduler silently ignore the setting again, which is
    the defect this file exists for. Contract request 19 asks for one home.
    """
    import dataclasses
    from typing import get_args

    from swarm_api.schemas import WorkflowCreate
    from swarm_common.models import Workflow
    from swarm_mcp import server

    from scheduler.loop import FAIL_WORKFLOW

    field = WorkflowCreate.model_fields["on_step_failure"]
    api = set(get_args(field.annotation))
    frozen_default = next(
        f.default for f in dataclasses.fields(Workflow) if f.name == "on_step_failure"
    )
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_workflow")
    advertised = tool["inputSchema"]["properties"]["on_step_failure"]

    assert api == {FAIL, CONTINUE}
    assert FAIL_WORKFLOW in api
    assert field.default == frozen_default == FAIL_WORKFLOW
    assert set(advertised["enum"]) == api
    assert advertised["default"] == field.default


def test_the_mcp_tool_describes_what_the_scheduler_does() -> None:
    """The old text described `fail_workflow` as what BOTH settings did.

    "`fail_workflow` cancels the dependents of a failed step" was true of
    `continue` too, and silent about the part that makes `fail_workflow`
    different: independent steps that have not started are cancelled as well.
    A model choosing a setting reads this text and nothing else.
    """
    from swarm_mcp import server

    tool = next(t for t in server.TOOLS if t["name"] == "swarm_workflow")
    text = tool["inputSchema"]["properties"]["on_step_failure"]["description"]
    lowered = text.lower()

    assert "cancels the dependents of a failed step;" not in text
    # fail_workflow: what is cancelled, what triggers it, what is spared.
    assert "not started" in lowered
    assert "independent" in lowered
    assert "dead_lettered" in lowered or "dead-lettered" in lowered
    assert "run to completion" in lowered
    # CANCELLED is not a failure, under either setting.
    assert "cancelled" in lowered
    # continue: dependents only.
    assert "continue" in lowered and "dependents" in lowered
