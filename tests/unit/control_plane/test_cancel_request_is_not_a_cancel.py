"""A cancel that is only requested is recorded as a request, never as `cancelled`.

Contract request 17 in docs/contract-change-requests.md (filed from incident
wf_ebb3ab2d65664707a559 as CR-1), accepted by the owner on 2026-09-24.

THE DEFECT. `Store.request_cancel` handles a task that holds capacity (LEASED,
DISPATCHED, STARTING, RUNNING) by setting `cancel_requested` and nothing else,
because only the worker or the reconciler may release the lease. It still wrote
an event of type `cancelled`, and only `detail.phase == "cancel_requested"` told
it apart from a real cancel. In the incident, check-2 to check-5 each carried a
`cancelled` event from 07:40Z and were still DISPATCHED, leases held, at
08:55Z: for more than an hour every reader of the event TYPE saw four cancelled
tasks and the task documents said otherwise.

WHAT IS PINNED HERE, each through the real route over the in-memory Firestore:

  1. the flag-only cancel writes `cancel_requested` and no `cancelled`;
  2. the immediate cancel (a task holding nothing) still writes `cancelled`;
  3. the `cancelled` of a task that held capacity is written ONCE, by whoever
     actually finished it -- here the reconciler's F-3 path -- and after the
     request, so the event history reads in the order things happened;
  4. history written before the change (`type: cancelled, phase:
     cancel_requested`) is SERVED as `cancel_requested`, so no reader of the API
     has to know the old shape existed. Nothing stored is rewritten;
  5. the terminal events are the frozen states' and a request is not one of
     them; benchstat's restatement agrees. The console's is held to the
     generated constants by its own `contract.constants.test.ts` (part of #72).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_common.states import TERMINAL_STATES, EventType, TaskState

from .conftest import auth_header, seed_task, seed_tenant
from .test_reconciler_gke_namespaced import CLOUD_RUN, FlatBackend, reconciler, seed_stranded

REPO = Path(__file__).resolve().parents[3]

#: The literal spelling, not `EventType.CANCEL_REQUESTED.value`, so that on a
#: tree without the new member these tests fail on the ASSERTION that names the
#: defect rather than on an AttributeError that names nothing.
CANCEL_REQUESTED = "cancel_requested"


def stored_events(db, task_id: str) -> list[dict]:
    return sorted(db.collection_docs(f"tasks/{task_id}/events"), key=lambda e: e["at"])


def served_events(client, task_id: str) -> list[dict]:
    response = client.get(f"/v1/tasks/{task_id}/events", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()["events"]


# --------------------------------------------------------------------------
# 1 and 2: what the API writes
# --------------------------------------------------------------------------

def test_a_cancel_of_a_task_holding_capacity_is_recorded_as_a_request(client, db) -> None:
    """The incident's shape: DISPATCHED, cancel pressed, nothing released yet."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_held", tenant_id="eng", state="DISPATCHED")

    response = client.post("/v1/tasks/task_held/cancel", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert response.json()["released_immediately"] is False
    assert db.docs["tasks/task_held"]["state"] == "DISPATCHED"

    events = stored_events(db, "task_held")
    types = [e["type"] for e in events]
    assert "cancelled" not in types, (
        f"the flag-only cancel wrote a `cancelled` event ({types}) while the task is "
        "still DISPATCHED and its lease unreleased -- the reading that showed an "
        "operator four cancelled tasks for over an hour in wf_ebb3ab2d65664707a559"
    )
    assert types == [CANCEL_REQUESTED], types
    detail = events[0]["detail"]
    assert detail["from_state"] == "DISPATCHED"
    assert detail["requested_by"] == "alice@example.com"
    # Kept so that a reader written against the old discriminator still reads
    # the new event correctly, and so a served legacy event and a new one are
    # the same shape (see test 4).
    assert detail["phase"] == CANCEL_REQUESTED


def test_a_cancel_of_a_task_holding_nothing_is_still_recorded_as_cancelled(client, db) -> None:
    """The immediate path is a real cancel and keeps its event. A guard: green before and after."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_idle", tenant_id="eng", state="QUEUED")

    response = client.post("/v1/tasks/task_idle/cancel", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert response.json()["released_immediately"] is True
    assert db.docs["tasks/task_idle"]["state"] == "CANCELLED"
    events = stored_events(db, "task_idle")
    assert [e["type"] for e in events] == ["cancelled"]
    assert events[0]["detail"]["phase"] == "cancelled"
    assert events[0]["detail"]["from_state"] == "QUEUED"


@pytest.mark.parametrize("state", sorted(s.value for s in TaskState))
def test_the_cancel_route_refuses_exactly_the_terminal_states(client, db, state) -> None:
    """The console offers its stop on the states outside TERMINAL_STATES; this
    is the server's half of that agreement."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_any", tenant_id="eng", state=state)

    response = client.post("/v1/tasks/task_any/cancel", headers=auth_header("alice"))

    refused = TaskState(state) in TERMINAL_STATES
    assert response.status_code == (409 if refused else 200), response.text


# --------------------------------------------------------------------------
# 3: the terminal event comes from whoever finishes the task
# --------------------------------------------------------------------------

def test_the_cancelled_event_is_written_once_by_the_component_that_finished_the_task(
    client, db
) -> None:
    """Request through the API, finish through the reconciler (incident item F-3).

    Before the change the history read `cancelled` (API, 07:40Z) ... `cancelled`
    (reconciler, whenever it got there): two terminal events for one ending, the
    first of them false. After it, the API's row says what the API did and the
    only `cancelled` is the reconciler's, written when the lease was released.
    """
    seed_tenant(db, "eng")
    # The API decodes the full task document; `seed_stranded` writes only the
    # fields the reconciler reads, so the two are merged -- the stranded lease,
    # attempt and pool units on top of a complete task.
    full = seed_task(
        db, task_id="task_637eb5eaae9445a6b186", tenant_id="eng", state="DISPATCHED",
        runner_profile="browser", resource_class="browser", provider="anthropic",
    )
    # A worker that started and then went silent: the API only flags it. One
    # that never started is ended by the API itself since archived issue 560, and that
    # ending is pinned in test_cancel_without_a_worker.py.
    ids = seed_stranded(
        db, "task_637eb5eaae9445a6b186", backend=CLOUD_RUN, heartbeat_seconds_ago=900
    )
    db.docs[f"tasks/{ids['task']}"] = {**full, **db.docs[f"tasks/{ids['task']}"]}

    response = client.post(f"/v1/tasks/{ids['task']}/cancel", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert response.json()["released_immediately"] is False
    assert [e["type"] for e in served_events(client, ids["task"])] == [CANCEL_REQUESTED], (
        "between the request and the ending, the history must say a cancel was "
        "REQUESTED and nothing else"
    )

    rec, _ = reconciler(db, FlatBackend(CLOUD_RUN))
    rec.run_once()

    assert db.docs[f"tasks/{ids['task']}"]["state"] == "CANCELLED"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None

    events = served_events(client, ids["task"])
    types = [e["type"] for e in events]
    assert types.count("cancelled") == 1, (
        f"{types.count('cancelled')} `cancelled` events for one ending: {types}"
    )
    assert types.count(CANCEL_REQUESTED) == 1, types
    assert types.index(CANCEL_REQUESTED) < types.index("cancelled"), (
        f"the history reads out of order: {types}"
    )
    ending = events[types.index("cancelled")]
    assert ending["detail"]["source"] == "reconciler", (
        "the `cancelled` event must come from the component that released the "
        f"lease, not from the API: {ending['detail']}"
    )
    assert ending["detail"]["phase"] == "cancelled"


# --------------------------------------------------------------------------
# 4: history written before the change
# --------------------------------------------------------------------------

def _legacy_event(db, task_id: str, event_id: str, *, at: datetime, detail: dict) -> None:
    """An event exactly as the API wrote it before 2026-09-24: type `cancelled`."""
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {
        "event_id": event_id,
        "task_id": task_id,
        "tenant_id": "eng",
        "type": "cancelled",
        "at": at,
        "attempt_id": None,
        "lease_id": None,
        "generation": None,
        "detail": detail,
    }


def test_a_stored_legacy_request_is_served_as_a_request_and_real_cancels_are_not(
    client, db
) -> None:
    """Events already written keep their old shape; the API reads them truthfully.

    No migration rewrites them -- request 17 proposes none, and this lane writes
    no live data. The reading is in `swarm_api.codec.event_from_dict`, the one
    decoder every event route goes through, so the UI, `swarm_follow` and every
    other API reader get one vocabulary whatever year the event was written in.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_old", tenant_id="eng", state="CANCELLED")
    t0 = datetime(2026, 9, 24, 7, 40, 20, tzinfo=timezone.utc)
    # check-2's row from the incident, as stored.
    _legacy_event(db, "task_old", "ev_a_request", at=t0, detail={
        "requested_by": "alice@example.com", "from_state": "DISPATCHED",
        "phase": "cancel_requested",
    })
    # A real cancel written by the old API's immediate path, and the scheduler's
    # cascade cancel, which has never carried a phase. Neither may be touched.
    _legacy_event(db, "task_old", "ev_b_immediate", at=t0 + timedelta(minutes=1), detail={
        "requested_by": "alice@example.com", "from_state": "QUEUED", "phase": "cancelled",
    })
    _legacy_event(db, "task_old", "ev_c_cascade", at=t0 + timedelta(minutes=2), detail={
        "reason": "an upstream workflow step did not succeed",
    })

    events = served_events(client, "task_old")

    assert [(e["event_id"], e["type"]) for e in events] == [
        ("ev_a_request", CANCEL_REQUESTED),
        ("ev_b_immediate", "cancelled"),
        ("ev_c_cascade", "cancelled"),
    ], "a stored flag-only cancel was served as a cancel, or a real cancel was served as a request"
    # The detail is served as stored: the reading changes the type, not the record.
    assert events[0]["detail"] == {
        "requested_by": "alice@example.com", "from_state": "DISPATCHED",
        "phase": "cancel_requested",
    }
    # Nothing stored was rewritten by reading it.
    assert db.docs["tasks/task_old/events/ev_a_request"]["type"] == "cancelled"


# --------------------------------------------------------------------------
# 5: the restatements of "a terminal event"
# --------------------------------------------------------------------------

def _tuple_literal(path: Path, name: str) -> set[str]:
    source = path.read_text()
    match = re.search(r"^%s\b[^=]*=\s*\((.*?)\)" % name, source, re.S | re.M)
    assert match is not None, f"no `{name} = (...)` in {path.relative_to(REPO)}"
    return set(re.findall(r"\"([^\"]+)\"", match.group(1)))


def test_a_cancel_request_is_an_event_and_not_an_ending() -> None:
    """The event each TERMINAL state is announced with (`control.finish`'s map).

    `terminal_events` in the generated constants is this set, and the console
    is held to it by its own `contract.constants.test.ts`.
    """
    terminal = {EventType[state.name].value for state in TERMINAL_STATES}
    assert CANCEL_REQUESTED in {e.value for e in EventType}, (
        "EventType has no cancel_requested member"
    )
    assert CANCEL_REQUESTED not in terminal, "a cancel REQUEST is a terminal event"


def test_the_benchmark_engine_restates_the_terminal_events_of_the_frozen_states() -> None:
    """benchstat runs in an image with no swarm_common, so it copies the set.

    `cancel_requested` must not be in the copy -- a request is not an ending,
    and a copy that counted it as one would read a task whose real ending is
    off the page as complete.
    """
    frozen = {EventType[state.name].value for state in TERMINAL_STATES}
    copy = _tuple_literal(REPO / "scripts/benchstat.py", "TERMINAL_EVENTS")
    assert copy == frozen, (
        f"scripts/benchstat.py restates {sorted(copy)}; the frozen states give {sorted(frozen)}"
    )
    assert CANCEL_REQUESTED not in copy, "benchstat counts a cancel REQUEST as a terminal event"
