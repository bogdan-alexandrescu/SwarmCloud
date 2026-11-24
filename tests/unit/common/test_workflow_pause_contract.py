"""Contract request 160 (docs/design/workflow-pause.md section 7), accepted by
the owner 2026-11-24: a park reason for a paused workflow's steps, the
workflow's three pause fields, and the two pause events.

No cloud, no emulator: the frozen package and swarm-api's codec only.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone

from swarm_api.codec import workflow_from_dict, workflow_to_firestore
from swarm_common.models import Workflow
from swarm_common.states import EventType, ParkReason, TaskState

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _workflow(**extra) -> Workflow:
    return Workflow(
        workflow_id="wf_pause_contract",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.RUNNING,
        submitted_by="alice@example.com",
        **extra,
    )


def test_workflow_paused_is_a_park_reason():
    assert ParkReason("WORKFLOW_PAUSED") is ParkReason.WORKFLOW_PAUSED


def test_the_two_pause_events_exist_with_their_values():
    assert EventType("pause_requested") is EventType.PAUSE_REQUESTED
    assert EventType("resume_requested") is EventType.RESUME_REQUESTED


def test_the_workflow_carries_exactly_the_three_pause_fields_defaulting_to_not_paused():
    names = {f.name for f in fields(Workflow)}
    assert {"paused", "paused_at", "paused_by"} <= names
    workflow = _workflow()
    assert (workflow.paused, workflow.paused_at, workflow.paused_by) == (False, None, None)


def test_the_pause_fields_round_trip_through_the_codec():
    workflow = _workflow(paused=True, paused_at=NOW, paused_by="alice@example.com")
    back = workflow_from_dict(workflow_to_firestore(workflow))
    assert (back.paused, back.paused_at, back.paused_by) == (True, NOW, "alice@example.com")


def test_a_stored_workflow_without_the_fields_reads_as_not_paused():
    stored = workflow_to_firestore(_workflow())
    for key in ("paused", "paused_at", "paused_by"):
        stored.pop(key, None)
    back = workflow_from_dict(stored)
    assert (back.paused, back.paused_at, back.paused_by) == (False, None, None)
