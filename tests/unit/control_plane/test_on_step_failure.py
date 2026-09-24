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

WORKFLOWS SUBMITTED BEFORE THE RULE (review of PR #42). Every workflow already
in Firestore stores `fail_workflow`, the API default, and was submitted while
docs/workflows.md said the setting was not honoured. Applying the rule to them
cancels work nobody was told would be cancelled, and a cancel cannot be undone.
Whether to do that is the owner's decision, so the scheduler carries both
answers: unset, `ON_STEP_FAILURE_ENFORCED_SINCE` applies the rule to every
workflow, as the 2026-09-24 decision was written; set, it applies the rule only
to workflows created at or after that instant. `scheduler.on_step_failure_audit`
lists, read-only, what the next drain would cancel, so the question can be
answered from data.

THE SWEEP HOOKS. Admission and the dependency sweep are not the only touch
points: a step parked for a missing key, or for quota, reaches neither until it
is promoted. The credential and prewarm tests below are built so that the hook
under test is the ONLY path to the cancel. They were proven by deleting each
hook in a mutant commit.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from .conftest import auth_header, scheduler_settings, seed_pool, seed_task, seed_tenant

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
    # ONE workflow, although the dependency sweep met two of its PARKED steps
    # (`after_busy`, `after_broken`) in one slice and admission could have met
    # `waiting`. The second meeting reads a snapshot from before the sweep and
    # must not sweep, or count, the workflow again.
    assert report.failed_workflows_swept == 1, report.to_dict()
    assert report.to_dict()["failed_workflows_swept"] == 1


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
    the defect this file exists for. Contract request 20 asks for one home.
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


# --------------------------------------------------------------------------
# The credential and prewarm sweeps. Each test leaves the hook under test as
# the ONLY path to the cancel: the workflow has no READY step for admission and
# no DEPENDENCY_INCOMPLETE step for the dependency sweep, and the parked step
# cannot be promoted on this drain (its key is missing; its window is hours
# away). Deleting the hook leaves the step PARKED, which is what the mutant
# commit showed.
# --------------------------------------------------------------------------

def _park(db, task_id: str, reason: str, **fields) -> None:
    """A step of a provider-bearing profile, parked for `reason`."""
    db.docs[f"tasks/{task_id}"].update(
        {
            "state": "PARKED",
            "park_reason": reason,
            "runner_profile": "claude-code",
            "provider": "anthropic",
            **fields,
        }
    )


def _only_cancel_event(db, task_id: str) -> dict:
    events = cancel_events(db, task_id)
    assert len(events) == 1, events
    return events[0]


def test_the_credential_sweep_cancels_a_step_parked_for_a_missing_key(
    client, db, make_scheduler, dispatcher
) -> None:
    """A step waiting for a key would otherwise outlive its workflow's failure.

    Its tenant has no `anthropic` key, so the credential sweep would leave it
    PARKED for as long as nobody registers one, and no other touch point ever
    reads it. The workflow would derive PARKED, not FAILED, indefinitely.
    """
    seed_pool(db, "global", hard_limit=10)
    workflow_id, steps = submit(
        client, [step("broken"), step("needs_key")], on_step_failure=FAIL
    )
    _park(db, steps["needs_key"], "CREDENTIAL_MISSING")
    worker_finished(db, steps["broken"], "FAILED")

    scheduler = make_scheduler()
    report = scheduler.drain()

    assert state_of(db, steps["needs_key"]) == "CANCELLED"
    event = _only_cancel_event(db, steps["needs_key"])
    # Cancelled from PARKED by the sweep, not promoted first and cancelled at
    # admission: the key is still missing, so it could not have been promoted.
    assert event["detail"]["from_state"] == "PARKED"
    assert event["detail"]["failed_steps"] == [
        {"step_id": "broken", "task_id": steps["broken"], "state": "FAILED"}
    ]
    assert event["detail"]["workflow_id"] == workflow_id
    assert report.promoted_credentials == 0
    assert report.cancelled == 1
    assert report.failed_workflows_swept == 1
    assert cancelled_metric(scheduler, "workflow_failed") == 1.0
    assert dispatcher.dispatched == []


def test_the_prewarm_sweep_cancels_a_quota_parked_step_before_it_resumes(
    client, db, make_scheduler, dispatcher
) -> None:
    """A quota-parked step is the one with partial work, which is why it matters.

    Its window reopens in two hours, far outside the prewarm lead, so prewarm
    would skip it and no other touch point reads it. Under `fail_workflow` it
    is cancelled from PARKED on this drain, before it can resume a checkpoint
    into a workflow that has already failed.
    """
    seed_pool(db, "global", hard_limit=10)
    _, steps = submit(
        client, [step("broken"), step("resumable")], on_step_failure=FAIL
    )
    _park(
        db,
        steps["resumable"],
        "PROVIDER_QUOTA_EXHAUSTED",
        next_eligible_at=datetime.now(timezone.utc) + timedelta(hours=2),
        latest_checkpoint=f"gs://bucket/checkpoints/{steps['resumable']}/2",
        attempt_count=1,
    )
    worker_finished(db, steps["broken"], "DEAD_LETTERED")

    scheduler = make_scheduler()
    report = scheduler.drain()

    assert state_of(db, steps["resumable"]) == "CANCELLED"
    event = _only_cancel_event(db, steps["resumable"])
    assert event["detail"]["from_state"] == "PARKED"
    assert event["detail"]["failed_steps"] == [
        {"step_id": "broken", "task_id": steps["broken"], "state": "DEAD_LETTERED"}
    ]
    assert report.promoted_prewarm == 0
    assert report.cancelled == 1
    assert report.failed_workflows_swept == 1
    assert dispatcher.dispatched == []


def test_failed_workflows_swept_counts_workflows_not_cancels(
    client, db, make_scheduler
) -> None:
    """Two failed workflows, four cancels between them, one healthy workflow.

    `cancelled` already counts the cancels. This field exists to say how many
    workflows they came from, so it must read 2 here, not 4 and not 3.
    """
    seed_pool(db, "global", hard_limit=10)
    _, wide = submit(
        client,
        [step("broken"), step("a"), step("b"), step("c")],
        on_step_failure=FAIL,
    )
    _, narrow = submit(client, [step("broken"), step("a")], on_step_failure=FAIL)
    _, healthy = submit(client, [step("done"), step("a")], on_step_failure=FAIL)
    worker_finished(db, wide["broken"], "FAILED")
    worker_finished(db, narrow["broken"], "FAILED")
    worker_finished(db, healthy["done"], "SUCCEEDED")

    report = make_scheduler().drain()

    assert report.cancelled == 4, report.to_dict()
    assert report.failed_workflows_swept == 2, report.to_dict()
    assert report.to_dict()["failed_workflows_swept"] == 2
    assert state_of(db, healthy["a"]) == "DISPATCHED"


# --------------------------------------------------------------------------
# Workflows submitted before the scheduler honoured the setting
# --------------------------------------------------------------------------

def _submitted_at(db, workflow_id: str, moment: datetime) -> None:
    db.docs[f"workflows/{workflow_id}"]["created_at"] = moment


def test_a_workflow_submitted_before_the_cutoff_keeps_the_rule_it_was_submitted_under(
    client, db, make_scheduler, dispatcher
) -> None:
    """ON_STEP_FAILURE_ENFORCED_SINCE set: an older workflow gets the dependency rule.

    It stores `fail_workflow` because that was the API default, and it was
    submitted while docs/workflows.md said the setting was not honoured and
    behaved like `continue`. So the failed step's dependents are cancelled and
    nothing else is: its independent READY step runs, and the step parked
    behind a running sibling stays parked for it.
    """
    seed_pool(db, "global", hard_limit=10)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    workflow_id, steps = submit(client, FIVE_STEPS, on_step_failure=FAIL)
    _submitted_at(db, workflow_id, cutoff - timedelta(microseconds=1))
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "FAILED")

    scheduler = make_scheduler(
        settings=scheduler_settings(on_step_failure_enforced_since=cutoff)
    )
    report = scheduler.drain()

    assert state_of(db, steps["after_broken"]) == "CANCELLED"
    assert (
        db.docs[f"tasks/{steps['after_broken']}"]["last_error"]
        == "an upstream workflow step did not succeed"
    )
    assert state_of(db, steps["waiting"]) == "DISPATCHED"
    assert state_of(db, steps["after_busy"]) == "PARKED"
    assert [d["task_id"] for d in dispatcher.dispatched] == [steps["waiting"]]
    assert report.failed_workflows_swept == 0
    assert cancelled_metric(scheduler, "workflow_failed") == 0.0


def test_a_workflow_submitted_at_the_cutoff_is_swept(
    client, db, make_scheduler, dispatcher
) -> None:
    """The boundary: created exactly at the cutoff is created after the rule existed."""
    seed_pool(db, "global", hard_limit=10)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    workflow_id, steps = submit(client, FIVE_STEPS, on_step_failure=FAIL)
    _submitted_at(db, workflow_id, cutoff)
    worker_is_running(db, steps["busy"])
    worker_finished(db, steps["broken"], "FAILED")

    report = make_scheduler(
        settings=scheduler_settings(on_step_failure_enforced_since=cutoff)
    ).drain()

    for name in ("waiting", "after_busy", "after_broken"):
        assert state_of(db, steps[name]) == "CANCELLED", name
    assert dispatcher.dispatched == []
    assert report.failed_workflows_swept == 1


def test_under_a_cutoff_a_workflow_with_no_creation_time_gets_no_policy(
    client, db, make_scheduler
) -> None:
    """Cannot tell when it was submitted, so it is not cancelled on a guess.

    The API always writes `created_at`, so this is a damaged document. The
    fallback is the same as for a missing document: the dependency rule only,
    because a cancel cannot be undone.
    """
    seed_pool(db, "global", hard_limit=10)
    workflow_id, steps = submit(
        client, [step("broken"), step("waiting")], on_step_failure=FAIL
    )
    del db.docs[f"workflows/{workflow_id}"]["created_at"]
    worker_finished(db, steps["broken"], "FAILED")

    make_scheduler(
        settings=scheduler_settings(
            on_step_failure_enforced_since=datetime.now(timezone.utc) - timedelta(days=1)
        )
    ).drain()

    assert state_of(db, steps["waiting"]) == "DISPATCHED"


def _settings_from_env(monkeypatch, value: str | None):
    from scheduler.settings import SchedulerSettings

    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("REGION", "us-central1")
    if value is None:
        monkeypatch.delenv("ON_STEP_FAILURE_ENFORCED_SINCE", raising=False)
    else:
        monkeypatch.setenv("ON_STEP_FAILURE_ENFORCED_SINCE", value)
    return SchedulerSettings.from_env()


def test_the_cutoff_is_read_from_the_environment(monkeypatch) -> None:
    settings = _settings_from_env(monkeypatch, "2026-09-25T14:00:00Z")
    assert settings.on_step_failure_enforced_since == datetime(
        2026, 9, 25, 14, 0, tzinfo=timezone.utc
    )
    settings = _settings_from_env(monkeypatch, "2026-09-25T16:00:00+02:00")
    assert settings.on_step_failure_enforced_since == datetime(
        2026, 9, 25, 14, 0, tzinfo=timezone.utc
    )
    assert _settings_from_env(monkeypatch, "").on_step_failure_enforced_since is None
    assert _settings_from_env(monkeypatch, "  ").on_step_failure_enforced_since is None


@pytest.mark.parametrize(
    "value",
    ["2026-09-25T14:00:00", "2026-09-25", "yesterday", "1727272800"],
)
def test_a_cutoff_without_an_offset_or_not_a_timestamp_refuses_to_start(
    monkeypatch, value
) -> None:
    """A naive time would be read in whatever zone the process happens to run in.

    A cutoff hours off in either direction cancels work it should have spared,
    or spares work it should have cancelled, so the scheduler refuses to start
    rather than guess.
    """
    with pytest.raises(ValueError, match="ON_STEP_FAILURE_ENFORCED_SINCE"):
        _settings_from_env(monkeypatch, value)


def test_a_naive_cutoff_is_refused_when_settings_are_built_directly() -> None:
    with pytest.raises(ValueError, match="on_step_failure_enforced_since"):
        scheduler_settings(on_step_failure_enforced_since=datetime(2026, 9, 25, 14, 0))


# --------------------------------------------------------------------------
# The pre-deploy audit: what would the next drain cancel?
# --------------------------------------------------------------------------

def _seed_audit_world(db) -> dict[str, datetime]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    seed_tenant(db, "eng")

    def workflow(workflow_id: str, policy: str, created_at: datetime) -> None:
        db.docs[f"workflows/{workflow_id}"] = {
            "workflow_id": workflow_id,
            "tenant_id": "eng",
            "on_step_failure": policy,
            "created_at": created_at,
            "state": "RUNNING",
        }

    def task(task_id: str, workflow_id: str, state: str, step_id: str, **fields) -> None:
        seed_task(db, task_id=task_id, tenant_id="eng", state=state,
                  workflow_id=workflow_id, **fields)
        db.docs[f"tasks/{task_id}"]["step_id"] = step_id

    # Failed, with a READY sibling, a quota-parked sibling holding a checkpoint,
    # and a RUNNING sibling that no sweep may touch.
    workflow("wf_swept", FAIL, now)
    task("t_a_failed", "wf_swept", "FAILED", "broken")
    task("t_a_ready", "wf_swept", "READY", "waiting")
    task("t_a_quota", "wf_swept", "PARKED", "resumable",
         park_reason="PROVIDER_QUOTA_EXHAUSTED")
    db.docs["tasks/t_a_quota"]["latest_checkpoint"] = "gs://bucket/checkpoints/t_a_quota/2"
    task("t_a_running", "wf_swept", "RUNNING", "busy")
    # fail_workflow, nothing failed yet: cancelled only if a step fails later.
    workflow("wf_exposed", FAIL, now)
    task("t_b_ready", "wf_exposed", "READY", "only")
    # continue: a failure here cancels dependents only, and there are none.
    workflow("wf_continue", CONTINUE, now)
    task("t_c_failed", "wf_continue", "FAILED", "broken")
    task("t_c_ready", "wf_continue", "READY", "waiting")
    # Submitted an hour and a minute ago: before the cutoff used below.
    workflow("wf_old", FAIL, cutoff - timedelta(minutes=1))
    task("t_d_failed", "wf_old", "FAILED", "broken")
    task("t_d_ready", "wf_old", "READY", "waiting")
    # Not a workflow step at all.
    seed_task(db, task_id="t_loose", tenant_id="eng")
    return {"now": now, "cutoff": cutoff}


def test_the_audit_lists_what_the_next_drain_would_cancel_and_writes_nothing(db) -> None:
    """Retroactive (no cutoff): every fail_workflow workflow is in scope.

    `wf_old` is swept too, because nothing exempts it. That is the list to read
    before deploying without a cutoff.
    """
    from scheduler.on_step_failure_audit import audit
    from scheduler.store import SchedulerStore

    _seed_audit_world(db)
    before = copy.deepcopy(db.docs)

    result = audit(SchedulerStore(db), enforced_since=None, sweep_size=200)

    assert db.docs == before, "the audit must be read-only"
    assert result["enforced_since"] is None
    assert result["truncated"] is False
    assert result["not_started_steps_visited"] == 6
    assert result["workflows_examined"] == 4

    swept = {w["workflow_id"]: w for w in result["swept_on_next_drain"]}
    assert set(swept) == {"wf_swept", "wf_old"}
    assert swept["wf_swept"]["tenant_id"] == "eng"
    assert swept["wf_swept"]["failed_steps"] == [
        {"step_id": "broken", "task_id": "t_a_failed", "state": "FAILED"}
    ]
    assert swept["wf_swept"]["would_cancel"] == [
        {"step_id": "resumable", "task_id": "t_a_quota", "state": "PARKED",
         "park_reason": "PROVIDER_QUOTA_EXHAUSTED", "has_checkpoint": True},
        {"step_id": "waiting", "task_id": "t_a_ready", "state": "READY",
         "park_reason": None, "has_checkpoint": False},
    ]
    assert [w["workflow_id"] for w in result["exposed_if_a_step_fails"]] == ["wf_exposed"]
    assert result["before_cutoff"] == []


def test_the_audit_under_a_cutoff_names_the_workflows_it_exempts(db) -> None:
    from scheduler.on_step_failure_audit import audit
    from scheduler.store import SchedulerStore

    moments = _seed_audit_world(db)

    result = audit(SchedulerStore(db), enforced_since=moments["cutoff"], sweep_size=200)

    assert result["enforced_since"] == moments["cutoff"].isoformat()
    assert [w["workflow_id"] for w in result["swept_on_next_drain"]] == ["wf_swept"]
    assert [w["workflow_id"] for w in result["exposed_if_a_step_fails"]] == ["wf_exposed"]
    assert [w["workflow_id"] for w in result["before_cutoff"]] == ["wf_old"]


def test_the_audit_says_when_its_answer_is_incomplete(db, capsys) -> None:
    """A full page means there may be more. It must not read as the whole answer."""
    import json

    from scheduler.on_step_failure_audit import audit, main
    from scheduler.store import SchedulerStore

    _seed_audit_world(db)
    store = SchedulerStore(db)

    assert audit(store, enforced_since=None, sweep_size=1)["truncated"] is True
    assert main(["--sweep-size", "1"], store=store) == 3
    assert main(["--sweep-size", "200"], store=store) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["truncated"] is False
    assert len(printed["swept_on_next_drain"]) == 2
