"""A fix-loop agent is parked, never leased, and its person-park expires after 7 days (FL-SCHED).

docs/design/fix-loop.md §4.5, §4.7 invariant 1 and §9 Q3 (c), BGD 2026-10-27.
A workflow's `fix-loop` step is a `control_plane` profile: no container runs
it, swarm-api drives its review-fix / re-review rounds as agents of their own
and ends it. The scheduler's whole part:

    a READY one -- promoted by its dependencies, or handed to admission --
        is PARKED(FIX_ROUNDS_RUNNING) with zero leases, whatever the headroom
        (the frozen ParkReason, Q2 (a)); never AWAITING_INPUT, which would
        tell every reader it waits for a person
    no sweep promotes or expires a FIX_ROUNDS_RUNNING park
    a HUMAN_REQUIRED park of the loop -- `park_phase` `fix_loop_*`, or one of
        §4.4's other phases on a fix-loop agent -- is spared the 25-hour
        hand-off dead-letter and the owner-questions "never", and is ended
        FAILED `fix_loop_exhausted` 7 days after it opened, as a person's
        "stop" would; one younger than that is spared

RED FIRST. Against main before this lane, a READY `fix-loop` agent was parked
AWAITING_INPUT (the await-input backstop matched every control_plane profile)
and the human-wait sweep dead-lettered a 26-hour-old loop park with
`human_wait_expired`, so every test that names FIX_ROUNDS_RUNNING or
`fix_loop_exhausted` failed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from scheduler.loop import FIX_LOOP_PARK_MAX_SECONDS, HUMAN_PARK_MAX_SECONDS, DrainReport
from swarm_common.models import EndCause
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import ParkReason

from .conftest import seed_pool, seed_task, seed_tenant

LOOP = "fix-loop"
WF = "wf_loop"
RUNNING = ParkReason.FIX_ROUNDS_RUNNING.value
HUMAN = ParkReason.HUMAN_REQUIRED.value
EXHAUSTED = EndCause.FIX_LOOP_EXHAUSTED.value
#: §4.4's person-parks that are not spelled `fix_loop_*`.
OTHER_PHASES = ("fix_changed_nothing", "round_failed", "review_unreadable", "round_refused")
DAY = 24.0


def _base(db, *, headroom: int = 50) -> None:
    seed_tenant(db, "eng", max_active=headroom)
    seed_pool(db, "global", hard_limit=headroom)


def _workflow(db, policy: str = "fail_workflow") -> None:
    db.docs[f"workflows/{WF}"] = {
        "id": WF,
        "tenant_id": "eng",
        "on_step_failure": policy,
        "created_at": datetime.now(timezone.utc) - timedelta(days=30),
    }


def _no_capacity_held(db) -> None:
    assert not any(key.startswith("leases/") for key in db.docs), sorted(db.docs)
    assert not any(key.startswith("attempts/") for key in db.docs), sorted(db.docs)
    assert db.docs["pools/global"]["active"] == 0
    assert db.docs["pools/tenant:eng"]["active"] == 0


def _running(doc: dict) -> None:
    assert (doc["state"], doc["park_reason"]) == ("PARKED", RUNNING), doc
    assert doc["current_lease_id"] is None


def _events(db, task_id: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return sorted((doc for key, doc in db.docs.items() if key.startswith(prefix)),
                  key=lambda event: event["at"])


def test_the_premise_fix_loop_is_control_plane_and_the_bound_is_seven_days():
    """Read from the frozen catalogue (FL-CONTRACT), never restated."""
    assert RUNNER_PROFILES[LOOP].control_plane is True
    assert FIX_LOOP_PARK_MAX_SECONDS == 7 * 24 * 3600
    assert FIX_LOOP_PARK_MAX_SECONDS > HUMAN_PARK_MAX_SECONDS


# -- (1) parked FIX_ROUNDS_RUNNING, never leased ---------------------------------


def test_a_ready_fix_loop_is_parked_by_admission_and_never_leased(db, make_scheduler, dispatcher):
    _base(db, headroom=500)
    doc = seed_task(db, task_id="task_loop", tenant_id="eng", runner_profile=LOOP)
    scheduler = make_scheduler()
    task = scheduler.store.get_task("task_loop")
    report = DrainReport()

    assert scheduler._admit_one(task, report) is False

    # MUTATION: dropping the fix-loop branch of `_park_control_plane` parks it
    # AWAITING_INPUT, with an await-input window it never declared.
    _running(doc)
    assert "await_input" not in doc["metadata"]
    assert report.leased == 0 and report.denied == 0 and report.parked == 1
    assert dispatcher.dispatched == []
    _no_capacity_held(db)
    assert [e["type"] for e in _events(db, "task_loop")] == ["parked"]
    assert _events(db, "task_loop")[0]["detail"]["reason"] == RUNNING


def test_a_ready_fix_loop_is_never_leased_by_a_drain_whatever_the_headroom(
    db, make_scheduler, dispatcher
):
    _base(db, headroom=500)
    seed_task(db, task_id="task_loop", tenant_id="eng", runner_profile=LOOP)
    seed_task(db, task_id="task_other", tenant_id="eng")

    scheduler = make_scheduler()
    first = scheduler.drain()
    second = scheduler.drain()

    _running(db.docs["tasks/task_loop"])
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_other"]
    assert first.leased == 1 and second.leased == 0, (first.to_dict(), second.to_dict())
    assert not any(doc.get("task_id") == "task_loop"
                   for key, doc in db.docs.items() if key.startswith("leases/"))


def _review_then_loop(db, *, review_state: str = "SUCCEEDED") -> None:
    _workflow(db)
    seed_task(db, task_id="task_review", tenant_id="eng", state=review_state, workflow_id=WF)
    seed_task(db, task_id="task_loop", tenant_id="eng", state="PARKED",
              park_reason="DEPENDENCY_INCOMPLETE", runner_profile=LOOP,
              depends_on=("task_review",), workflow_id=WF)


def test_when_the_review_succeeds_the_finish_event_parks_the_loop_with_zero_leases(
    db, make_scheduler, dispatcher
):
    _base(db)
    _review_then_loop(db)

    report = make_scheduler().release_dependants("task_review")

    _running(db.docs["tasks/task_loop"])
    assert report.leased == 0 and report.parked == 1, report.to_dict()
    assert dispatcher.dispatched == []
    _no_capacity_held(db)
    # It passes through READY for the one promotion write and holds nothing.
    assert [e["type"] for e in _events(db, "task_loop")] == ["ready", "parked"]


def test_when_the_review_succeeds_the_tick_parks_the_loop_with_zero_leases(
    db, make_scheduler, dispatcher
):
    _base(db)
    _review_then_loop(db)

    report = make_scheduler().drain()

    _running(db.docs["tasks/task_loop"])
    assert report.leased == 0, report.to_dict()
    assert dispatcher.dispatched == []
    _no_capacity_held(db)


def test_the_await_input_step_is_still_parked_awaiting_input(db, make_scheduler):
    """The control: the other control_plane profile keeps its own park."""
    _base(db)
    doc = seed_task(db, task_id="task_ask", tenant_id="eng", runner_profile="await-input")
    doc["metadata"] = {"dispatch": {"await_input": {
        "form": "verdict", "artifact": "review.json", "on_expire": "fail",
        "expires_after_seconds": 3600}}}

    make_scheduler().drain()

    assert (doc["state"], doc["park_reason"]) == ("PARKED", "AWAITING_INPUT")


def test_a_fix_rounds_running_park_is_never_promoted_or_ended_however_old(
    db, make_scheduler, dispatcher
):
    _base(db, headroom=500)
    _workflow(db)
    then = datetime.now(timezone.utc) - timedelta(days=30)
    doc = seed_task(db, task_id="task_loop", tenant_id="eng", state="PARKED",
                    park_reason=RUNNING, runner_profile=LOOP, next_eligible_at=then,
                    created_at=then, workflow_id=WF)
    before = dict(doc)

    report = make_scheduler().drain()

    assert doc == before, report.to_dict()
    assert not _events(db, "task_loop")
    assert dispatcher.dispatched == []
    _no_capacity_held(db)


# -- (2) the loop's person-park expires after 7 days ------------------------------


def _human_park(db, task_id: str, phase: str, *, hours_ago: float,
                profile: str = LOOP, cancel_requested: bool = False) -> dict:
    then = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state="PARKED", park_reason=HUMAN,
                    runner_profile=profile, next_eligible_at=then, created_at=then,
                    workflow_id=WF, cancel_requested=cancel_requested)
    doc["blocked_by"] = [{"park_phase": phase, "verdict": "NOT_YET", "findings_count": 2}]
    return doc


def _ended_exhausted(doc: dict) -> None:
    assert doc["state"] == "FAILED", doc
    assert doc["end_cause"] == EXHAUSTED
    assert doc["park_reason"] is None and doc["current_lease_id"] is None
    assert doc["last_error"].startswith(EXHAUSTED)
    assert doc["completed_at"] is not None


@pytest.mark.parametrize("phase", ["fix_loop_exhausted", *OTHER_PHASES])
def test_a_loop_park_older_than_seven_days_ends_failed_fix_loop_exhausted(
    db, make_scheduler, phase
):
    _base(db)
    _workflow(db)
    doc = _human_park(db, "task_loop", phase, hours_ago=7 * DAY + 1)

    report = DrainReport()
    ended = make_scheduler()._expire_human_waits(report)

    # MUTATION: removing the fix-loop branch of `_expire_human_waits` ends it
    # DEAD_LETTERED `human_wait_expired`, end_cause None.
    _ended_exhausted(doc)
    assert ended == 0 and report.fix_loops_expired == 1, report.to_dict()
    failed = [e for e in _events(db, "task_loop") if e["type"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["detail"]["park_phase"] == phase
    assert failed[0]["detail"]["cause"] == EXHAUSTED
    _no_capacity_held(db)


@pytest.mark.parametrize("phase", ["fix_loop_exhausted", *OTHER_PHASES])
def test_a_loop_park_younger_than_seven_days_is_spared_past_the_25_hours(
    db, make_scheduler, phase
):
    _base(db)
    _workflow(db)
    doc = _human_park(db, "task_loop", phase, hours_ago=7 * DAY - 1)
    before = dict(doc)

    # MUTATION: the 25-hour hand-off bound in place of FIX_LOOP_PARK_MAX_SECONDS
    # dead-letters it here.
    assert make_scheduler()._expire_human_waits(DrainReport()) == 0

    assert doc == before
    assert not _events(db, "task_loop")


def test_a_fix_loop_star_phase_is_the_loops_whatever_comes_after_the_prefix(
    db, make_scheduler
):
    _base(db)
    _workflow(db)
    young = _human_park(db, "task_young", "fix_loop_stopped_by_policy", hours_ago=2 * DAY)
    old = _human_park(db, "task_old", "fix_loop_stopped_by_policy", hours_ago=8 * DAY)

    make_scheduler()._expire_human_waits(DrainReport())

    assert young["state"] == "PARKED"
    _ended_exhausted(old)


def test_the_loop_park_is_not_spared_as_an_owner_questions_hold_is(db, make_scheduler):
    """The control: an owner-questions hold at the same age is never expired
    (owner decision 2026-10-23); the loop's park is (Q3 (c), not (a))."""
    _base(db)
    _workflow(db)
    loop = _human_park(db, "task_loop", "fix_loop_exhausted", hours_ago=8 * DAY)
    merge = _human_park(db, "task_merge", "owner_questions", hours_ago=8 * DAY, profile="mock")
    merge["blocked_by"] = [{"park_phase": "owner_questions",
                            "owner_questions": {"questions": [{"id": "q1"}]}}]

    make_scheduler()._expire_human_waits(DrainReport())

    _ended_exhausted(loop)
    assert (merge["state"], merge["park_reason"]) == ("PARKED", HUMAN)


@pytest.mark.parametrize("phase", OTHER_PHASES)
def test_the_other_phases_on_an_ordinary_agent_keep_the_25_hour_dead_letter(
    db, make_scheduler, phase
):
    """The control: §4.4's other phase names are the loop's only on a fix-loop agent."""
    _base(db)
    _workflow(db)
    doc = _human_park(db, "task_agent", phase, hours_ago=26, profile="mock")
    doc["attempt_count"] = 1

    assert make_scheduler()._expire_human_waits(DrainReport()) == 1

    assert doc["state"] == "DEAD_LETTERED"
    assert doc["end_cause"] is None


def test_a_cancel_requested_loop_park_past_seven_days_is_cancelled(db, make_scheduler):
    _base(db)
    _workflow(db)
    doc = _human_park(db, "task_loop", "fix_loop_exhausted", hours_ago=8 * DAY,
                      cancel_requested=True)

    report = DrainReport()
    make_scheduler()._expire_human_waits(report)

    assert doc["state"] == "CANCELLED", doc
    assert report.fix_loops_expired == 0


def test_a_whole_drain_ends_the_loop_and_its_merge_behind_it(db, make_scheduler, dispatcher):
    """The loop ends as a person's "stop" would: the merge step behind it does
    not run, and nothing is leased or reserved on the way (invariants 1-3)."""
    _base(db, headroom=500)
    _workflow(db, "continue")
    loop = _human_park(db, "task_loop", "fix_loop_exhausted", hours_ago=8 * DAY)
    merge = seed_task(db, task_id="task_merge", tenant_id="eng", state="PARKED",
                      park_reason="DEPENDENCY_INCOMPLETE", depends_on=("task_loop",),
                      workflow_id=WF)

    report = make_scheduler().drain()

    _ended_exhausted(loop)
    assert merge["state"] == "CANCELLED", report.to_dict()
    assert dispatcher.dispatched == []
    _no_capacity_held(db)
