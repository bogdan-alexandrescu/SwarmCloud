"""swarm_check flags a merge step on CI_PENDING for over 90 minutes across its re-parks (#2999).

wf_a05fb4fe2b7f4781bed3's merge step (task_5c00c810495e4f949212) sat PARKED
on CI_PENDING from 09:55Z to 22:57Z on 2026-11-01, re-checked 56 times, and
`/sc check` listed it and never flagged it, though rule (c) flags CI_PENDING
past 90 minutes. Each re-check is a promotion, an attempt and a new `parked`
event, and the rule aged the park from the NEWEST `parked` event: never more
than one re-check old. It now ages the CI wait from the park's own first
instant (the API's `waiting.since`, `merge_wait.first_parked_at`) or the
first of the unbroken CI_PENDING parks on its events, whichever is earlier,
and carries the wait's last reading as the evidence.

No credentials, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_mcp import check

NOW = datetime(2026, 11, 1, 22, 57, tzinfo=timezone.utc)
FIRST_PARK = datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc)
SUMMARY = ("CI on PR #2959: 1 checks pending (ci-gate); read 22:57Z; re-checked 56 times; "
           "next check 23:10Z")


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


class _Reads:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def events(self, task_id: str) -> list[dict[str, Any]]:  # noqa: ARG002
        return self._events

    def task(self, task_id: str) -> None:  # noqa: ARG002
        return None

    def capacity(self) -> tuple[None, str]:
        return None, "not read in this test"


def _re_checks(count: int, *, newest: datetime, every: timedelta = timedelta(minutes=15),
               reason: str = "CI_PENDING") -> list[dict[str, Any]]:
    """`count` re-checks, newest first: each a `running` attempt that parked again."""
    events: list[dict[str, Any]] = []
    for i in range(count):
        parked = newest - i * every
        events.append({"type": "parked", "at": _iso(parked), "detail": {"reason": reason}})
        events.append({"type": "running", "at": _iso(parked - timedelta(minutes=1))})
    return events


def _merge_step(**extra: Any) -> dict[str, Any]:
    return {"id": "task_5c00c810495e4f949212", "state": "PARKED", "park_reason": "CI_PENDING",
            "runner_profile": "merge", "created_at": _iso(FIRST_PARK - timedelta(minutes=40)),
            **extra}


def _waiting() -> dict[str, Any]:
    return {"reason": "CI_PENDING", "summary": SUMMARY, "since": _iso(FIRST_PARK),
            "detail": {"head": "5a5d673b7985eb05eb3e3dcf943912e69d712352",
                       "pending": ["ci-gate"], "failed": [], "wakes": 56,
                       "last_checked_at": _iso(NOW)}}


def test_a_merge_step_parked_13_hours_across_56_re_parks_is_flagged():
    """The served page holds only the last few re-checks; the newest park is
    10 minutes old. The wait's first park is 13 hours old."""
    task = _merge_step(waiting=_waiting())
    reads = _Reads(_re_checks(check.EVENT_PAGE // 2, newest=NOW - timedelta(minutes=10)))

    flag = check.classify_task(task, reads, now=NOW, window=20)

    # MUTATION (check.py `_parked`): age from the newest `parked` event, as
    # before #2999 -> 10 minutes, not flagged (None).
    assert flag is not None and flag["rule"] == "parked", flag
    evidence = flag["evidence"]
    assert evidence["age_minutes"] == 13 * 60 + 2, evidence
    assert evidence["in_state_since"] == _iso(FIRST_PARK)
    assert evidence["age_is"] == "exact"
    assert "CI_PENDING for exact 782 min" in flag["reason"]
    # Its reading is the evidence.
    assert evidence["waiting_summary"] == SUMMARY
    assert evidence["checks_pending"] == ["ci-gate"] and evidence["re_checks"] == 56
    assert evidence["checks_read_at"] == _iso(NOW)


def test_without_waiting_since_the_unbroken_ci_parks_on_its_events_age_it():
    """An API that serves no `waiting`: nine re-checks over two hours, the
    whole history on one page."""
    task = _merge_step(created_at=_iso(NOW - timedelta(hours=2, minutes=30)))
    reads = _Reads(_re_checks(9, newest=NOW - timedelta(minutes=5)))

    flag = check.classify_task(task, reads, now=NOW, window=20)

    # MUTATION (check.py `_ci_entered`): take the newest CI_PENDING park ->
    # 5 minutes, not flagged.
    assert flag is not None, "nine re-checks over 2 h 5 min must be flagged"
    assert flag["evidence"]["age_minutes"] == 125
    assert flag["evidence"]["age_is"] == "exact"


def test_a_park_for_another_reason_ends_the_ci_wait():
    """A merge-slot wait between two CI waits restarts the CI clock: the
    first CI park after it is where this wait began."""
    events = (_re_checks(2, newest=NOW - timedelta(minutes=10))
              + _re_checks(8, newest=NOW - timedelta(minutes=40), reason="MERGE_SLOT"))
    task = _merge_step(created_at=_iso(NOW - timedelta(hours=4)))

    flag = check.classify_task(task, _Reads(events), now=NOW, window=20)

    # MUTATION (check.py `_ci_entered`): walk past the other park -> flagged
    # at the oldest event.
    assert flag is None, flag


def test_a_ci_wait_under_90_minutes_is_not_flagged():
    waiting = {**_waiting(), "since": _iso(NOW - timedelta(minutes=80))}
    task = _merge_step(waiting=waiting)

    flag = check.classify_task(task, _Reads(_re_checks(4, newest=NOW - timedelta(minutes=5))),
                               now=NOW, window=20)

    assert flag is None, flag
