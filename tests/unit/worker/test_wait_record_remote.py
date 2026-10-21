"""WAIT-RECORD over swarm-api's state routes (part of #1782).

A tenant switched to the state routes parks through `RemoteControlPlane`:
swarm-api writes the park, its event and `merge_wait` in one transaction.
Its `ci` body is strict (`attemptstate.CiWait`), so the new facts ride in the
park's `detail`, which swarm-api merges into the PARKED event as it does for
every park. The event is what `waiting_detail` reads first.

Offline: the worker's own `ChildApi`, answered in process by swarm-api's
real app (`state_routes_harness`). Each assertion names the mutation it
catches.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_worker import handoff as handoff_mod
from agent_worker.control import RemoteControlPlane

from control_plane.fakes import FakeFirestore as ApiFirestore
from state_routes_harness import (
    TASK,
    RoutesOpener,
    api_client,
    events,
    remote_control,
    seed_attempt,
)

CHECK = "ci / unit"
LINT = "ci / lint"


@pytest.fixture
def api_db() -> ApiFirestore:
    return ApiFirestore()


@pytest.fixture
def opener(api_db) -> RoutesOpener:
    return RoutesOpener(api_client(api_db))


def _running(api_db, opener) -> RemoteControlPlane:
    seed_attempt(api_db)
    control = remote_control(opener)
    control.validate_generation()
    control.advance_to_running()
    return control


def test_a_remote_ci_park_records_failed_and_the_pull_request_in_its_event(api_db, opener):
    control = _running(api_db, opener)

    control.park_ci_pending(code="ci_fix_pending", head="abc", pull_request=7, pending=[LINT],
                            failed=[CHECK], max_wakes=60, fallback_seconds=900)

    (parked,) = events(api_db, kind="parked")
    # MUTATION: `RemoteControlPlane.park_ci_pending` drops `failed` -> fails.
    assert parked["detail"]["failed"] == [CHECK]
    # MUTATION: it sends no `pull_request` beside the `ci` body -> fails.
    assert parked["detail"]["pull_request"] == 7
    # MUTATION: the facts overwrite what swarm-api's park records -> fails.
    assert parked["detail"]["pending"] == [LINT] and parked["detail"]["code"] == "ci_fix_pending"
    wait = api_db.docs[f"tasks/{TASK}"]["metadata"]["merge_wait"]
    assert (wait["pull_request"], wait["pending"]) == (7, [LINT])


def test_a_remote_ci_park_with_nothing_read_records_no_failed(api_db, opener):
    """The control: no reading, no `failed` -- absent, not an empty list."""
    control = _running(api_db, opener)

    control.park_ci_pending(code="merge_conflict_pending", head="abc", pull_request=7,
                            pending=[], max_wakes=60, fallback_seconds=900)

    (parked,) = events(api_db, kind="parked")
    # MUTATION: `failed: []` is sent when nothing was read -> fails.
    assert "failed" not in parked["detail"]
    assert parked["detail"]["pull_request"] == 7


def test_a_remote_human_park_records_when_it_expires(api_db, opener):
    control = _running(api_db, opener)

    handoff_mod.park_human_required(control, max_resumes=handoff_mod.MAX_HUMAN_RESUMES,
                                    detail={"cause": "hold_expired"})

    (parked,) = events(api_db, kind="parked")
    detail = parked["detail"]
    bound = timedelta(seconds=handoff_mod.HUMAN_PARK_MAX_SECONDS)
    # The park instant is swarm-api's clock, read after the worker's: the
    # worker's `expires_at` is at most the request's own latency earlier.
    # MUTATION: no `expires_at` on this path -> KeyError.
    # MUTATION: another bound (say the hold's seconds) -> fails.
    lag = detail["next_eligible_at"] + bound - detail["expires_at"]
    assert timedelta(0) <= lag < timedelta(seconds=30), lag
    assert detail["cause"] == "hold_expired"
