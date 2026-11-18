"""A held merge wait's waiting line shows the last CI check, never a next check already past.

Owner decision 2026-11-10 (answering the merged lane "A merge step waiting on
CI holds no capacity lease between checks"): while the scheduler holds a
merge step's CI wait -- the wake tick's own reading of the park
(`merge_wait.reading`) is fresh and says CI has no answer, so
`scheduler.loop.merge_wait_due` does not wake it at `next_eligible_at` -- the
waiting line must not say "next check HH:MM" from `next_eligible_at`: that
instant is the worker's fallback, which the scheduler is holding, and is
often already past. The line serves the tick's last check instead. Every
other wait keeps today's line.

Pure: no credentials, no network, no emulator. Each assertion carries a
MUTATION comment: the change to the builder that would turn it red.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_common.models import Task, TaskEvent
from swarm_common.states import EventType, ParkReason, TaskState

from scheduler import loop as loop_mod
from swarm_api import mergewake, waiting_detail
from swarm_api.waiting_detail import build_waiting

AS_OF = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
HEAD = "a1b2c3d4" * 5
PARKED = AS_OF - timedelta(minutes=40)
#: The worker's fallback instant: the park plus 900 s, 25 minutes ago.
FALLBACK = PARKED + timedelta(seconds=loop_mod.MERGE_CI_FALLBACK_SECONDS)
READ = AS_OF - timedelta(minutes=1)


def _task(*, reading: dict[str, Any] | None, next_eligible_at: datetime | None = FALLBACK) -> Task:
    wait: dict[str, Any] = {"head": HEAD, "parked_at": PARKED, "first_parked_at": PARKED,
                            "pull_request": 41, "pending": ["ci-gate"], "wakes": 2}
    if reading is not None:
        wait[mergewake.READING_KEY] = reading
    return Task(
        id="task_w", tenant_id="eng",
        created_at=AS_OF - timedelta(hours=2), updated_at=PARKED,
        state=TaskState.PARKED, runner_profile="mock", resource_class="standard",
        input={}, submitted_by="alice@example.com",
        park_reason=ParkReason.CI_PENDING, next_eligible_at=next_eligible_at,
        metadata={"merge_wait": wait},
        blocked_by=[{"reason": "CI_PENDING", "code": "checks_pending", "head": HEAD}],
    )


def _park() -> TaskEvent:
    return TaskEvent(event_id="ev_park", task_id="task_w", tenant_id="eng",
                     type=EventType.PARKED, at=PARKED,
                     detail={"reason": "CI_PENDING", "code": "checks_pending", "head": HEAD,
                             "pending": ["ci-gate"], "wakes": 2})


def _reading(**over: Any) -> dict[str, Any]:
    return {"head": HEAD, "read_at": READ, "pending": ["ci-gate"], "failed": [], **over}


def _hhmm(at: datetime) -> str:
    return at.strftime("%H:%MZ")


def test_a_held_merge_wait_serves_its_last_check_and_no_past_next_check():
    task = _task(reading=_reading())
    # The scheduler holds this park: the shape the test is about.
    assert loop_mod.merge_wait_due(task.metadata["merge_wait"], FALLBACK, AS_OF) is None
    w = build_waiting(task, events=[_park()], as_of=AS_OF)

    # MUTATION: serving next_eligible_at as next_check_at during a held wait.
    assert w["next_check_at"] is None, w
    # MUTATION: dropping the tick's last check, or serving the park's instead.
    assert w["detail"]["held_last_check_at"] == READ, w["detail"]
    assert w["detail"]["last_checked_at"] == READ, w["detail"]
    # MUTATION: keeping "next check HH:MM", "eligible since HH:MM" or the
    # fallback re-check time in the line or in what resumes it.
    for text in (w["summary"], w["resumes_when"]):
        assert "next check" not in text, text
        assert "eligible since" not in text, text
        assert _hhmm(FALLBACK) not in text, text
    assert f"read {_hhmm(READ)}" in w["summary"], w["summary"]
    # MUTATION: still appending the past fallback instant to the history.
    assert all(r["kind"] != "eligible" for r in w["history"]), w["history"]


def test_a_held_wait_with_a_fallback_still_ahead_serves_no_next_check_either():
    # The scheduler holds past the fallback instant too, so even a future
    # next_eligible_at is not when the step is next checked.
    task = _task(reading=_reading(), next_eligible_at=AS_OF + timedelta(minutes=5))
    assert loop_mod.merge_wait_due(task.metadata["merge_wait"], task.next_eligible_at, AS_OF) is None
    w = build_waiting(task, events=[_park()], as_of=AS_OF)
    assert w["next_check_at"] is None
    assert "next check" not in w["summary"]
    assert w["detail"]["held_last_check_at"] == READ


def test_an_ordinary_ci_wait_keeps_todays_line():
    task = _task(reading=None)
    assert loop_mod.merge_wait_due(task.metadata["merge_wait"], FALLBACK, AS_OF) == "fallback_due"
    w = build_waiting(task, events=[_park()], as_of=AS_OF)
    # MUTATION: hiding the next check on a wait the scheduler does not hold.
    assert w["next_check_at"] == FALLBACK
    assert w["detail"]["held_last_check_at"] is None
    assert w["detail"]["last_checked_at"] == PARKED
    assert f"next check {_hhmm(FALLBACK)}" in w["summary"], w["summary"]
    assert f"eligible since {_hhmm(FALLBACK)}, still parked" in w["summary"], w["summary"]
    assert f"fallback re-check at {_hhmm(FALLBACK)}" in w["resumes_when"]
    assert any(r["kind"] == "eligible" for r in w["history"])


def test_a_reading_the_scheduler_would_not_trust_does_not_hold_the_line():
    """The hold mirrors `merge_wait_due`: a reading at another head, older than
    the park, from the future or older than the fallback window leaves
    today's line, exactly where the scheduler wakes the park itself."""
    stale = AS_OF - timedelta(seconds=loop_mod.MERGE_CI_FALLBACK_SECONDS)
    for reading in (_reading(head="f" * 40), _reading(read_at=PARKED - timedelta(seconds=1)),
                    _reading(read_at=AS_OF + timedelta(minutes=5)), _reading(read_at=stale)):
        task = _task(reading=reading)
        assert loop_mod.merge_wait_due(task.metadata["merge_wait"], FALLBACK, AS_OF) == "fallback_due"
        w = build_waiting(task, events=[_park()], as_of=AS_OF)
        # MUTATION: holding the line on a reading the scheduler ignores.
        assert w["next_check_at"] == FALLBACK, reading
        assert w["detail"]["held_last_check_at"] is None, reading
        assert "next check" in w["summary"], reading


def test_the_line_restates_the_schedulers_freshness_window():
    assert waiting_detail.CI_READING_FRESH_SECONDS == loop_mod.MERGE_CI_FALLBACK_SECONDS
    assert loop_mod.MERGE_READING_KEY == mergewake.READING_KEY
