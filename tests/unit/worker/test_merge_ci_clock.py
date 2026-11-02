"""The merge step's 6 h CI limit holds across re-parks, slot waits and both state paths (#3033).

Measured 2026-11-01: the Talk pane lane's merge step (task_5c00c810495e4f949212)
parked CI_PENDING 56 times at one head over 13 hours and never ended with
`checks_timeout`. Its tenant, `eng`, is on the state routes (dev's
`state_via_api_tenants`), so the step reads its own document through
swarm-api's `GET state/upstream`, which serves it as JSON: `first_parked_at`
arrived as an ISO string, and `merge._ci_wait_age` read only a datetime. And a
merge-slot (or train) wait CLEARED the clock, so a queue between two CI parks
restarted the 6 h.

What holds now: the limit reads the instant in either shape; a slot wait
pauses the clock (`ci_paused_at`) and the next CI park resumes it; the clock
restarts only at a new head.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import Any

from agent_worker import control as control_mod
from agent_worker import merge, mergeslot
from agent_worker.control import ControlPlane
from agent_worker.logs import build_logger
from swarm_common.models import utcnow
from swarm_common.states import TaskState

from fakes import FakeFirestore, FakeTransactionRunner
from merge_world import CHECK, PINNED, TASK, MergeWorld
from worker_seeds import TENANT, seed_attempt

OTHER_HEAD = "e" * 40


def _pending(world: MergeWorld) -> None:
    world.runs[0].update(status="in_progress", conclusion=None)


def _record(world: MergeWorld, **wait: Any) -> None:
    world.docs[TASK]["metadata"] = {control_mod.MERGE_WAIT_METADATA_KEY: {
        "code": "checks_pending", "head": PINNED, "wakes": 3, "updates": 0, **wait}}


# ---------------------------------------------------------------------------
# The limit, read on the next wake
# ---------------------------------------------------------------------------


def test_a_seven_hour_old_ci_park_read_through_the_state_routes_is_checks_timeout(tmp_path):
    """The incident: the state routes serve the instant as an ISO string."""
    world = MergeWorld(tmp_path)
    _pending(world)
    _record(world, first_parked_at=(utcnow() - timedelta(hours=7)).isoformat())

    outcome = merge.run_merge(world.context())

    # MUTATION: `_ci_wait_age` reads only a datetime (before #3033) -> PARKED.
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.ci_wait is None
    assert outcome.summary["refusal"]["code"] == "checks_timeout"


def test_a_seven_hour_old_ci_park_read_from_firestore_is_checks_timeout(tmp_path):
    """The control: the direct path's datetime, which was always read."""
    world = MergeWorld(tmp_path)
    _pending(world)
    _record(world, first_parked_at=utcnow() - timedelta(hours=7))

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "checks_timeout", outcome.message


def test_a_clock_paused_by_a_slot_wait_counts_only_to_the_pause(tmp_path):
    """Seven hours since the first park, five of them queued for the slot: two
    hours of CI, so the step parks again."""
    world = MergeWorld(tmp_path)
    _pending(world)
    now = utcnow()
    _record(world, first_parked_at=(now - timedelta(hours=7)).isoformat(),
            ci_paused_at=(now - timedelta(hours=5)).isoformat())

    outcome = merge.run_merge(world.context())

    # MUTATION: count the pause as CI time -> checks_timeout.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["code"] == "checks_pending"


def test_a_clock_left_at_another_head_is_a_wait_that_ended(tmp_path):
    """A head that moved (a fix round, a branch update) starts a new wait."""
    world = MergeWorld(tmp_path)
    _pending(world)
    _record(world, head=OTHER_HEAD, first_parked_at=utcnow() - timedelta(hours=7))

    outcome = merge.run_merge(world.context())

    # MUTATION: read the old head's clock at the new head -> checks_timeout.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.ci_wait["head"] == PINNED


# ---------------------------------------------------------------------------
# The park, over in-memory Firestore
# ---------------------------------------------------------------------------


def _control(db: FakeFirestore) -> ControlPlane:
    return ControlPlane(
        db, task_id="task_1", attempt_id="att_1", lease_id="lease_1", tenant_id=TENANT,
        generation=1,
        logger=build_logger(task_id="task_1", attempt_id="att_1", tenant_id=TENANT,
                            generation=1, runner_profile="merge", stream=io.StringIO()),
        txn_runner=FakeTransactionRunner(db),
    )


def _park(db: FakeFirestore, *, head: str = PINNED, slot: bool = False,
          seed: dict[str, Any] | None = None) -> dict[str, Any]:
    """One attempt that parks: seeded RUNNING (with `seed` as its last
    `merge_wait`, or the one the last park left), then the lifecycle's call."""
    last = (db.doc("tasks/task_1") or {}).get("metadata", {}).get(
        control_mod.MERGE_WAIT_METADATA_KEY) if seed is None else seed
    seed_attempt(db, state=TaskState.RUNNING, runner_profile="merge")
    if last is not None:
        db.doc("tasks/task_1").setdefault("metadata", {})[
            control_mod.MERGE_WAIT_METADATA_KEY] = dict(last)
    _control(db).park_ci_pending(
        code=mergeslot.MERGE_SLOT_WAIT if slot else "checks_pending", head=head,
        pull_request=41, pending=[] if slot else [CHECK],
        max_wakes=mergeslot.MERGE_SLOT_MAX_WAITS if slot else merge.MERGE_CI_MAX_WAKES,
        fallback_seconds=merge.MERGE_CI_FALLBACK_SECONDS,
        counter="slot_waits" if slot else "wakes", ci_clock=not slot)
    return db.doc("tasks/task_1")["metadata"][control_mod.MERGE_WAIT_METADATA_KEY]


def _first_ci(first: datetime) -> dict[str, Any]:
    return {"code": "checks_pending", "head": PINNED, "wakes": 4, "updates": 0,
            "first_parked_at": first, "parked_at": first}


def test_a_slot_wait_between_two_ci_parks_does_not_restart_the_six_hours():
    db = FakeFirestore()
    first = utcnow() - timedelta(hours=5)

    queued = _park(db, slot=True, seed=_first_ci(first))
    # MUTATION: the slot wait clears `first_parked_at` (before #3033) -> fails.
    assert queued["first_parked_at"] == first
    assert isinstance(queued["ci_paused_at"], datetime)

    again = _park(db)
    pause = again["parked_at"] - queued["ci_paused_at"]
    # Resumed: moved on by exactly the pause, never restarted at this park.
    assert again["first_parked_at"] == first + pause
    assert again["ci_paused_at"] is None
    assert again["parked_at"] - again["first_parked_at"] >= timedelta(hours=5)


def test_a_second_slot_wait_keeps_the_first_pause():
    db = FakeFirestore()
    first = utcnow() - timedelta(hours=5)
    paused = utcnow() - timedelta(hours=1)
    wait = _park(db, slot=True, seed={**_first_ci(first), "code": mergeslot.MERGE_SLOT_WAIT,
                                      "ci_paused_at": paused})
    # MUTATION: each slot park re-pauses at its own instant -> the queue's
    # first hour is counted as CI.
    assert wait["first_parked_at"] == first and wait["ci_paused_at"] == paused


def test_a_ci_re_park_keeps_the_first_time():
    db = FakeFirestore()
    first = utcnow() - timedelta(hours=3)
    wait = _park(db, seed=_first_ci(first))
    assert wait["first_parked_at"] == first and wait["ci_paused_at"] is None


def test_a_wait_at_a_new_head_starts_fresh():
    db = FakeFirestore()
    before = utcnow()
    wait = _park(db, head=OTHER_HEAD, seed=_first_ci(utcnow() - timedelta(hours=7)))
    # MUTATION: carry the clock over a head that moved -> fails.
    assert wait["first_parked_at"] >= before
    assert wait["head"] == OTHER_HEAD


def test_a_slot_wait_with_no_ci_wait_running_starts_no_clock():
    db = FakeFirestore()
    wait = _park(db, slot=True, seed={})
    assert wait["first_parked_at"] is None and wait["ci_paused_at"] is None
