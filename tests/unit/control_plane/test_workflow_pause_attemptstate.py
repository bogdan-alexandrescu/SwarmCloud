"""Workflow pause, swarm-api's worker side (docs/design/workflow-pause.md, lane P1).

* `fence_view` carries `pause_requested` from the step's raw
  `pause_requested_at` mirror (sections 2.1 and 5.1): set while the pause is
  requested, cleared once resume cleared the mirror, and never served to an
  attempt of another generation (a stale worker gets the fenced answer and
  learns nothing of the pause).
* `AttemptState.park` with `WORKFLOW_PAUSED` refunds the attempt per the
  owner's decision Q1 (section 10): bounded at five per step in
  `metadata.pause_refunds`; past it the park counts. Any other reason's park
  is unchanged.

Offline: no credentials, no emulator. Each assertion names the mutation that
turns it red.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import attemptstate
from swarm_api.attemptstate import fence_view
from swarm_api.childkey import AttemptTuple

from .test_attemptstate import (  # noqa: F401 - fixtures
    ATTEMPT,
    LEASE,
    TASK,
    Worker,
    _events,
    client,
    opened,
    worker_tokens,
)

PAUSED_AT = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
#: Decision Q1's key and bound, spelled here so a server that changed either
#: fails `test_the_bound_is_the_owners_five_per_step`.
PAUSE_REFUNDS = "pause_refunds"
PAUSE_MAX_REFUNDS = 5


def _pause_park(request_id: str = "pause-1") -> dict[str, Any]:
    """The worker's pause park (design section 5.2): no number in it."""
    return {"request_id": request_id, "reason": "WORKFLOW_PAUSED",
            "next_eligible_at": datetime.now(timezone.utc).isoformat(),
            "detail": {"park_phase": "workflow_pause", "paused_at": PAUSED_AT.isoformat()}}


def _poll(client, worker: Worker) -> dict[str, Any]:
    response = worker.get(client, "poll")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# fence_view: the pause signal
# --------------------------------------------------------------------------


def test_fence_view_carries_the_flag_only_while_the_mirror_is_set():
    task = {"state": "RUNNING", "current_generation": 1, "tenant_id": "eng",
            "pause_requested_at": PAUSED_AT}
    # MUTATION: leave `pause_requested` out of `fence_view` and this is red.
    assert fence_view(task)["pause_requested"] is True
    # MUTATION: `"pause_requested": True` (or reading the key's presence) turns these red.
    assert fence_view({**task, "pause_requested_at": None})["pause_requested"] is False
    assert fence_view({k: v for k, v in task.items() if k != "pause_requested_at"})[
        "pause_requested"] is False
    assert fence_view(None) is None


def test_fence_view_never_carries_the_flag_to_another_generation():
    task = {"state": "RUNNING", "current_generation": 2, "tenant_id": "eng",
            "pause_requested_at": PAUSED_AT}
    stale = AttemptTuple("eng", TASK, ATTEMPT, LEASE, 1)
    current = AttemptTuple("eng", TASK, ATTEMPT, LEASE, 2)
    # MUTATION: ignore the attempt's generation in `fence_view` and this is red.
    assert fence_view(task, stale)["pause_requested"] is False
    assert fence_view(task, current)["pause_requested"] is True


def test_poll_carries_the_flag_for_a_step_whose_pause_was_requested(client, db):
    worker = Worker()
    opened(client, db, worker, pause_requested_at=PAUSED_AT)
    body = _poll(client, worker)
    # MUTATION: drop the flag from `fence_view` and the worker never learns of the pause.
    assert body["fenced"] is None
    assert body["task"]["pause_requested"] is True


def test_poll_does_not_carry_the_flag_once_resume_cleared_the_mirror(client, db):
    worker = Worker()
    opened(client, db, worker, pause_requested_at=PAUSED_AT)
    # Resume stage 2 (`Store.resume_workflow`) writes the mirror back to None.
    db.docs[f"tasks/{TASK}"]["pause_requested_at"] = None
    body = _poll(client, worker)
    # MUTATION: serve the flag from the key's presence rather than its value and this is red.
    assert body["task"]["pause_requested"] is False


def test_a_stale_generations_poll_is_fenced_and_learns_nothing_of_the_pause(client, db):
    worker = Worker()
    opened(client, db, worker, pause_requested_at=PAUSED_AT)
    # The reconciler fenced this attempt: the task is at generation 2 now.
    db.docs[f"tasks/{TASK}"].update({"current_generation": 2, "current_lease_id": "lease_new"})
    body = _poll(client, worker)
    assert body["fenced"] == "task has moved to a newer generation"
    # MUTATION: serve the mirror to every reader of the view and this is red.
    assert body["task"]["pause_requested"] is False


def test_a_fenced_poll_of_this_generation_learns_nothing_of_the_pause(client, db):
    worker = Worker()
    opened(client, db, worker, pause_requested_at=PAUSED_AT)
    db.docs[f"leases/{LEASE}"]["released_at"] = datetime.now(timezone.utc)
    body = _poll(client, worker)
    assert body["fenced"] == "lease has already been released"
    # MUTATION: gate the flag on the generation alone and this is red.
    assert body["task"]["pause_requested"] is False


def test_a_stale_generations_pause_park_is_fenced_and_writes_nothing(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=2, pause_requested_at=PAUSED_AT)
    db.docs[f"tasks/{TASK}"].update({"current_generation": 2, "current_lease_id": "lease_new"})
    before = copy.deepcopy(db.docs)
    response = worker.post(client, "park", _pause_park())
    # MUTATION: refund before the fence (or skip `_fenced` for this reason) and this is red.
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "attempt_fenced"
    assert "pause" not in response.text
    assert db.docs == before


def test_a_repeated_pause_park_after_a_newer_generation_does_not_serve_the_flag(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=2, pause_requested_at=PAUSED_AT)
    first = worker.post(client, "park", _pause_park("pause-r"))
    assert first.status_code == 200, first.text
    # The step was promoted and leased again at generation 2, and paused again.
    db.docs[f"tasks/{TASK}"].update({"current_generation": 2, "current_lease_id": "lease_new",
                                     "state": "RUNNING"})
    again = worker.post(client, "park", _pause_park("pause-r"))
    assert again.status_code == 200, again.text
    assert again.json()["repeat"] is True
    # MUTATION: serve the repeat's view without the attempt's generation and this is red.
    assert again.json()["task"]["pause_requested"] is False


# --------------------------------------------------------------------------
# AttemptState.park: the refund rule for WORKFLOW_PAUSED (decision Q1)
# --------------------------------------------------------------------------


def test_the_bound_is_the_owners_five_per_step():
    # MUTATION: any other bound, or another key than Q1's, turns this red.
    assert getattr(attemptstate, "PAUSE_MAX_REFUNDS", None) == PAUSE_MAX_REFUNDS
    assert getattr(attemptstate, "PAUSE_REFUNDS", None) == PAUSE_REFUNDS


def test_a_pause_park_refunds_within_the_bound_in_one_transaction(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=2, pause_requested_at=PAUSED_AT)
    committed = db.transactions_committed
    response = worker.post(client, "park", _pause_park())
    assert response.status_code == 200, response.text
    # One transaction: the park, the refund, the attempt's end and the release.
    assert db.transactions_committed == committed + 1
    task = db.docs[f"tasks/{TASK}"]
    # MUTATION: no WORKFLOW_PAUSED branch in `_park_payload` and nothing is refunded.
    assert response.json()["refunded"] is True
    assert task["attempt_count"] == 1
    assert task["metadata"][PAUSE_REFUNDS] == 1
    assert task["state"] == "PARKED" and task["park_reason"] == "WORKFLOW_PAUSED"
    assert task["current_lease_id"] is None
    # The capacity is released all-or-nothing, through the one release function.
    assert db.docs[f"leases/{LEASE}"]["released_at"] is not None
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["pools/tenant:eng"]["active"] == 0
    (parked,) = _events(db, "parked")
    assert parked["detail"]["attempt_refunded"] is True
    assert parked["detail"][PAUSE_REFUNDS] == 1
    assert parked["detail"]["reason"] == "WORKFLOW_PAUSED"
    assert len(_events(db, "lease_released")) == 1


def test_the_last_refund_inside_the_bound_is_given(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=3,
           metadata={PAUSE_REFUNDS: PAUSE_MAX_REFUNDS - 1})
    response = worker.post(client, "park", _pause_park())
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    # MUTATION: `<=` off by one the other way (`used < bound - 1`) and this is red.
    assert response.json()["refunded"] is True
    assert task["attempt_count"] == 2
    assert task["metadata"][PAUSE_REFUNDS] == PAUSE_MAX_REFUNDS


def test_a_pause_park_past_the_bound_counts(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=2,
           metadata={PAUSE_REFUNDS: PAUSE_MAX_REFUNDS})
    response = worker.post(client, "park", _pause_park())
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    # MUTATION: refund without the bound (`refund = attempt_count > 0`) and this is red.
    assert response.json()["refunded"] is False
    assert task["attempt_count"] == 2
    assert task["metadata"][PAUSE_REFUNDS] == PAUSE_MAX_REFUNDS
    assert task["state"] == "PARKED" and task["park_reason"] == "WORKFLOW_PAUSED"
    (parked,) = _events(db, "parked")
    assert parked["detail"]["attempt_refunded"] is False
    # Still released: the count decides the refund, never the release.
    assert db.docs[f"leases/{LEASE}"]["released_at"] is not None


def test_a_pause_park_never_refunds_below_zero(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=0)
    response = worker.post(client, "park", _pause_park())
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    # MUTATION: drop `attempt_count > 0` and the count goes negative.
    assert response.json()["refunded"] is False
    assert task["attempt_count"] == 0
    assert task.get("metadata", {}).get(PAUSE_REFUNDS, 0) == 0


def test_a_pause_park_does_not_spend_another_reasons_counter(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=2, metadata={"quota_park_resumes": 5})
    response = worker.post(client, "park", _pause_park())
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    # MUTATION: count the pause against the quota park's counter and this is red.
    assert response.json()["refunded"] is True
    assert task["metadata"]["quota_park_resumes"] == 5
    assert task["metadata"][PAUSE_REFUNDS] == 1


@pytest.mark.parametrize("reason", ["PROVIDER_COOLDOWN", "PROVIDER_OUTAGE", "SCHEDULED_RETRY",
                                    "MANUAL_PAUSE"])
def test_another_reasons_park_is_unchanged(client, db, reason):
    worker = Worker()
    opened(client, db, worker, attempt_count=2, pause_requested_at=PAUSED_AT)
    response = worker.post(client, "park", {
        "request_id": "other-1", "reason": reason,
        "next_eligible_at": datetime.now(timezone.utc).isoformat()})
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    # MUTATION: refund every park of a paused step (by the mirror, not the reason) and this is red.
    assert response.json()["refunded"] is False
    assert task["attempt_count"] == 2
    assert PAUSE_REFUNDS not in (task.get("metadata") or {})
    (parked,) = _events(db, "parked")
    assert "attempt_refunded" not in parked["detail"]
    assert PAUSE_REFUNDS not in parked["detail"]


def test_a_quota_park_still_counts_against_its_own_bound_only(client, db):
    worker = Worker()
    opened(client, db, worker, attempt_count=2, metadata={PAUSE_REFUNDS: 0})
    response = worker.post(client, "park", {
        "request_id": "quota-1", "reason": "PROVIDER_QUOTA_EXHAUSTED",
        "next_eligible_at": datetime.now(timezone.utc).isoformat()})
    assert response.status_code == 200, response.text
    task = db.docs[f"tasks/{TASK}"]
    assert response.json()["refunded"] is True
    assert task["metadata"]["quota_park_resumes"] == 1
    # MUTATION: share one counter between the quota park and the pause park and this is red.
    assert task["metadata"][PAUSE_REFUNDS] == 0
