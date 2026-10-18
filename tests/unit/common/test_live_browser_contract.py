"""The live browser's frozen-contract changes, requests 68-71 (LB-A..LB-D, #1030).

docs/design/live-browser.md section 8.4 asked for four changes to
`apps/common/swarm_common/`; the owner accepted all four on 2026-10-11, with
Q2 answered as the FIELD, not a new task state. What this file holds:

  * LB-A `ParkReason.HUMAN_REQUIRED` exists and is a PARK: it names no task
    state, so it is in no capacity set, and the twelve task states and their
    partition are exactly what they were (invariants 1 and 3);
  * LB-B the four `human_*` event types, by their stored values;
  * LB-C `Task.human_wait`, None by default, stored as the dict it is, and no
    `WAITING_FOR_HUMAN` state anywhere;
  * LB-D `RunnerProfile.live_browser`: False by default, True on
    `claude-code-browser` and nowhere else, and refused at construction on a
    profile whose backend is not GKE_AUTOPILOT (AUTO included, because the
    `browser` class resolves AUTO to Cloud Run);
  * the restatements check-contract-parity.sh reads still carry the new park
    reason, so the parity script stays green.

MUTATION: drop `HUMAN_REQUIRED` from types.ts and
`test_the_ui_restatements_carry_the_new_park_reason` fails; delete the backend
check in `RunnerProfile.__post_init__` and the two refusal tests fail; set
`live_browser=True` on `browser` and `test_live_browser_is_true_only_on_claude_code_browser`
fails.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_common.models import Task
from swarm_common.profiles import RUNNER_PROFILES, Backend, RunnerProfile
from swarm_common.states import (
    CONCURRENCY_STATES,
    PENDING_STATES,
    TERMINAL_STATES,
    EventType,
    ParkReason,
    TaskState,
    holds_capacity,
)

ROOT = Path(__file__).resolve().parents[3]
TYPES_TS = ROOT / "apps" / "swarm-ui" / "src" / "types.ts"
STATE_PILL = ROOT / "apps" / "swarm-ui" / "src" / "components" / "StatePill.tsx"
REQUESTS = ROOT / "docs" / "contract-change-requests.md"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# LB-A: ParkReason.HUMAN_REQUIRED, a park that costs nothing
# --------------------------------------------------------------------------


def test_human_required_is_a_park_reason():
    assert ParkReason.HUMAN_REQUIRED.value == "HUMAN_REQUIRED"


def test_human_required_is_not_in_any_capacity_set():
    """A hand-off parked past its hold has released its lease (invariant 1)."""
    capacity_values = {s.value for s in CONCURRENCY_STATES}
    assert ParkReason.HUMAN_REQUIRED.value not in capacity_values
    assert ParkReason.HUMAN_REQUIRED.value not in {s.value for s in TaskState}
    # The state a HUMAN_REQUIRED task is in holds nothing.
    assert TaskState.PARKED in PENDING_STATES
    assert not holds_capacity(TaskState.PARKED)


def test_the_lifecycle_and_its_partition_are_unchanged():
    """Q2 chose the field: no state was added, so no reader had to learn one."""
    assert {s.value for s in TaskState} == {
        "SUBMITTED", "QUEUED", "PARKED", "READY", "LEASED", "DISPATCHED",
        "STARTING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED",
    }
    assert {s.value for s in CONCURRENCY_STATES} == {
        "LEASED", "DISPATCHED", "STARTING", "RUNNING",
    }
    assert {s.value for s in PENDING_STATES} == {"SUBMITTED", "QUEUED", "PARKED", "READY"}
    assert {s.value for s in TERMINAL_STATES} == {
        "SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED",
    }
    assert not any("HUMAN" in s.value for s in TaskState)


# --------------------------------------------------------------------------
# LB-B: the four hand-off event types
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("HUMAN_HANDOFF_REQUESTED", "human_handoff_requested"),
        ("HUMAN_CONTROL_TAKEN", "human_control_taken"),
        ("HUMAN_CONTROL_RETURNED", "human_control_returned"),
        ("HUMAN_HANDOFF_RESOLVED", "human_handoff_resolved"),
    ],
)
def test_the_hand_off_event_types_exist(member, value):
    assert EventType[member].value == value


def test_exactly_four_human_event_types():
    assert sorted(e.name for e in EventType if e.name.startswith("HUMAN_")) == [
        "HUMAN_CONTROL_RETURNED",
        "HUMAN_CONTROL_TAKEN",
        "HUMAN_HANDOFF_REQUESTED",
        "HUMAN_HANDOFF_RESOLVED",
    ]


# --------------------------------------------------------------------------
# LB-C: Task.human_wait, the field form
# --------------------------------------------------------------------------


def _task(**overrides) -> Task:
    fields = dict(
        id="task_lb0",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.RUNNING,
        runner_profile="claude-code-browser",
        resource_class="browser",
        input={"prompt": "sign in and check the dashboard"},
        submitted_by="someone@example.com",
    )
    fields.update(overrides)
    return Task(**fields)


def test_human_wait_is_a_task_field_that_defaults_to_none():
    names = {f.name for f in dataclasses.fields(Task)}
    assert "human_wait" in names
    task = _task()
    assert task.human_wait is None
    assert task.to_firestore()["human_wait"] is None


def test_human_wait_round_trips_as_the_dict_it_is():
    wait = {
        "reason": "Sign in to GitHub as the bot account",
        "expect": "github.com",
        "requested_at": NOW,
        "deadline": NOW + timedelta(minutes=10),
        "controller": None,
    }
    task = _task(human_wait=wait)
    stored = task.to_firestore()["human_wait"]
    assert stored == wait
    # A copy, so a writer mutating the stored form cannot reach the task.
    stored["controller"] = {"email": "someone@example.com"}
    assert task.human_wait["controller"] is None


def test_a_waiting_task_stays_running():
    """The hold is RUNNING with the field set; it is counted because it is RUNNING."""
    task = _task(human_wait={"reason": "r", "expect": None, "requested_at": NOW,
                             "deadline": NOW, "controller": None})
    assert task.state is TaskState.RUNNING
    assert holds_capacity(task.state)


# --------------------------------------------------------------------------
# LB-D: RunnerProfile.live_browser
# --------------------------------------------------------------------------


def test_live_browser_defaults_to_false():
    field = {f.name: f for f in dataclasses.fields(RunnerProfile)}["live_browser"]
    assert field.default is False


def test_live_browser_is_true_only_on_claude_code_browser():
    live = sorted(name for name, p in RUNNER_PROFILES.items() if p.live_browser)
    assert live == ["claude-code-browser"]
    assert RUNNER_PROFILES["claude-code-browser"].backend is Backend.GKE_AUTOPILOT


def _profile(backend: Backend, live_browser: bool) -> RunnerProfile:
    return RunnerProfile(
        name="lb-probe",
        image="agent-runtime-browser",
        resource_class="browser",
        backend=backend,
        runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
        live_browser=live_browser,
    )


@pytest.mark.parametrize("backend", [b for b in Backend if b is not Backend.GKE_AUTOPILOT])
def test_live_browser_is_refused_on_a_non_gke_profile(backend):
    with pytest.raises(ValueError, match="live_browser"):
        _profile(backend, live_browser=True)


def test_auto_is_refused_because_the_browser_class_resolves_to_cloud_run():
    """Not hypothetical: AUTO would put this class on Cloud Run Jobs."""
    from swarm_common.profiles import resolve_backend

    assert resolve_backend(_profile(Backend.AUTO, live_browser=False)) is Backend.CLOUD_RUN_JOB
    with pytest.raises(ValueError, match="GKE_AUTOPILOT"):
        _profile(Backend.AUTO, live_browser=True)


def test_live_browser_on_a_gke_profile_is_accepted():
    assert _profile(Backend.GKE_AUTOPILOT, live_browser=True).live_browser is True
    assert _profile(Backend.CLOUD_RUN_JOB, live_browser=False).live_browser is False


# --------------------------------------------------------------------------
# The restatements check-contract-parity.sh holds to the frozen enum
# --------------------------------------------------------------------------


def test_the_ui_restatements_carry_the_new_park_reason():
    src = TYPES_TS.read_text(encoding="utf-8")
    union = re.search(r"export type ParkReason\s*=([^\n]*(?:\n\s*\|[^\n]*)*)", src)
    assert union is not None
    assert sorted(re.findall(r"'([^']+)'", union.group(1))) == sorted(
        r.value for r in ParkReason
    )
    people = re.search(r"export const PARK_NEEDS_A_PERSON\b.*?\]\)", src, re.S)
    assert people is not None and "'HUMAN_REQUIRED'" in people.group(0), (
        "only a person promotes a HUMAN_REQUIRED park; no sweep or timer does"
    )
    pill = STATE_PILL.read_text(encoding="utf-8")
    assert re.search(r"^\s*HUMAN_REQUIRED:", pill, re.M)


def test_the_requests_are_filed_as_accepted():
    text = REQUESTS.read_text(encoding="utf-8")
    for tag in ("LB-A", "LB-B", "LB-C", "LB-D"):
        rows = [line for line in text.splitlines() if line.startswith("| ") and tag in line]
        assert rows, f"{tag} has no index row"
        assert "ACCEPTED by the owner 2026-10-11" in rows[0], rows[0]
