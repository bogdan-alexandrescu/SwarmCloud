"""Workflow pause: a step parked for a paused workflow says so (docs/design/workflow-pause.md, lane P1).

`waiting_detail.build_waiting` gains a WORKFLOW_PAUSED branch (sections 4.4
and 9): the line names who paused the workflow, says it resumes when the
workflow is resumed, and carries the recorded workflow id, pauser and pause
time, null when not recorded. The console reads this line from swarm-api, so
the GET serves it too. No other park reason's line changes.

Offline: no credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.states import EventType, ParkReason

from swarm_api.codec import event_to_firestore
from swarm_api.waiting_detail import build_waiting

from .conftest import auth_header, seed_task, seed_tenant
from .test_waiting_detail import READ_AT, event, task

PAUSED_AT = READ_AT - timedelta(hours=3)
PARKED_AT = READ_AT - timedelta(hours=2)
RESUMES_WHEN = "the workflow is resumed"


def _scheduler_park() -> dict[str, object]:
    """What `Scheduler._park_for_workflow_pause` records on the PARKED event."""
    return {"reason": "WORKFLOW_PAUSED", "workflow_id": "wf_paused",
            "paused_by": "alice@example.com", "paused_at": PAUSED_AT.isoformat()}


def test_a_step_parked_for_a_paused_workflow_names_who_and_what_resumes_it():
    t = task(park_reason=ParkReason.WORKFLOW_PAUSED, workflow_id="wf_paused",
             next_eligible_at=PARKED_AT)
    ev = [event(EventType.PARKED, PARKED_AT, _scheduler_park())]
    w = build_waiting(t, events=ev, as_of=READ_AT)
    assert w["reason"] == "WORKFLOW_PAUSED"
    # MUTATION: no WORKFLOW_PAUSED branch ("Parked: WORKFLOW_PAUSED") turns this red.
    assert w["summary"] == "Workflow paused by alice@example.com"
    assert w["resumes_when"] == RESUMES_WHEN
    # MUTATION: a paused_at not from the recorded park turns this red.
    assert w["detail"] == {"workflow_id": "wf_paused", "paused_by": "alice@example.com",
                           "paused_at": PAUSED_AT}
    assert w["since"] == PARKED_AT
    # Not a timed park: resume, not a clock, ends it.
    # MUTATION: adding WORKFLOW_PAUSED to `_TIMED` turns these red.
    assert w["next_check_at"] is None
    assert "still parked" not in w["summary"]


def test_the_worker_parks_copy_in_blocked_by_serves_the_line_without_events():
    """The listing reads no events; the worker's park writes
    `blocked_by=[{reason, **detail}]` (design section 5.2's detail)."""
    t = task(park_reason=ParkReason.WORKFLOW_PAUSED, workflow_id="wf_paused",
             blocked_by=[{"reason": "WORKFLOW_PAUSED", "park_phase": "workflow_pause",
                          "paused_at": PAUSED_AT.isoformat()}])
    w = build_waiting(t, events=None, as_of=READ_AT)
    # MUTATION: inventing a pauser nobody recorded turns this red.
    assert w["summary"] == "Workflow paused"
    assert w["detail"] == {"workflow_id": "wf_paused", "paused_by": None, "paused_at": PAUSED_AT}
    assert w["resumes_when"] == RESUMES_WHEN


def test_nothing_recorded_is_null_never_invented():
    t = task(park_reason=ParkReason.WORKFLOW_PAUSED)
    w = build_waiting(t, events=[], as_of=READ_AT)
    assert w["summary"] == "Workflow paused"
    # MUTATION: a paused_at taken from the read time turns this red.
    assert w["detail"] == {"workflow_id": None, "paused_by": None, "paused_at": None}


def test_a_manual_pause_keeps_its_own_line():
    t = task(park_reason=ParkReason.MANUAL_PAUSE)
    ev = [event(EventType.PARKED, PARKED_AT, {"reason": "MANUAL_PAUSE",
                                              "paused_by": "root@example.com"})]
    w = build_waiting(t, events=ev, as_of=READ_AT)
    # MUTATION: routing MANUAL_PAUSE through the workflow branch turns this red.
    assert w["summary"] == "Paused by root@example.com"
    assert w["resumes_when"] == ("what paused it clears and the scheduler's pause "
                                 "sweep returns it")
    assert w["detail"] == {"paused_by": "root@example.com", "paused_at": PARKED_AT,
                           "error": None}


@pytest.mark.parametrize("reason", [r for r in ParkReason if r is not ParkReason.WORKFLOW_PAUSED])
def test_no_other_park_reasons_line_takes_the_workflow_wording(reason):
    t = task(park_reason=reason, workflow_id="wf_paused")
    # A park event carrying the pause's facts: only the reason may pick the branch.
    ev = [event(EventType.PARKED, PARKED_AT, {**_scheduler_park(), "reason": reason.value})]
    w = build_waiting(t, events=ev, as_of=READ_AT)
    assert w["reason"] == reason.value
    # MUTATION: choosing the branch by the detail's paused_by (or workflow_id) turns this red.
    assert not w["summary"].startswith("Workflow paused")
    assert w["resumes_when"] != RESUMES_WHEN


def test_the_unknown_reason_fallback_is_unchanged():
    t = task(park_reason=None)
    w = build_waiting(t, events=[], as_of=READ_AT)
    assert w["summary"] == "Parked: UNKNOWN"
    assert w["resumes_when"] == "the scheduler's sweep for this reason returns it"


def test_get_task_serves_the_paused_steps_line(client, db):
    seed_tenant(db, "eng", max_active=20)
    seed_task(db, task_id="task_paused", tenant_id="eng", state="PARKED",
              park_reason="WORKFLOW_PAUSED", workflow_id="wf_paused")
    ev = event(EventType.PARKED, datetime.now(timezone.utc), _scheduler_park(),
               task_id="task_paused")
    db.docs[f"tasks/task_paused/events/{ev.event_id}"] = event_to_firestore(ev)
    res = client.get("/v1/tasks/task_paused", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    w = res.json()["task"]["waiting"]
    # MUTATION: no WORKFLOW_PAUSED branch and the console reads "Parked: WORKFLOW_PAUSED".
    assert w["reason"] == "WORKFLOW_PAUSED"
    assert w["summary"] == "Workflow paused by alice@example.com"
    assert w["resumes_when"] == RESUMES_WHEN
    assert w["detail"]["workflow_id"] == "wf_paused"
