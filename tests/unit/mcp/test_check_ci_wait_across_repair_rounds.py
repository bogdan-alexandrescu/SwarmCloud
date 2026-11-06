"""swarm_check ages a merge step's CI wait across its repair rounds' heads (part of #2688).

Reported by BGD on 2026-11-06 (about 05:20Z): a speech relay lane's merge
step (task_ffe63d609c5d4662ba8e) waited on CI for 8 hours with 28 attempts
and `/sc check` never flagged it. The merge step re-checks about every 16
minutes and each re-check is a new attempt; `first_parked_at` (the API's
`waiting.since`) restarts at every head a repair round pushes, and a merge
train ejection bounds the run of CI parks on the events, so neither source
read the wait as older than 90 minutes. The park now records the whole wait
(`merge_wait.wait_started_at`, `rechecks`, `failed_heads`, `main_red`), and
the rule ages it from there with that evidence.

No credentials, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_mcp import check

NOW = datetime(2026, 11, 6, 5, 20, tzinfo=timezone.utc)
STARTED = NOW - timedelta(hours=8)
CHECK = "unit (worker)"


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


def _events() -> list[dict[str, Any]]:
    """Newest first: two re-checks at the newest head, then the merge train's
    ejection park, which bounds the run of CI parks the events can see."""
    events: list[dict[str, Any]] = []
    for i in range(2):
        at = NOW - timedelta(minutes=5 + 16 * i)
        events.append({"type": "parked", "at": _iso(at), "detail": {"reason": "CI_PENDING"}})
        events.append({"type": "running", "at": _iso(at - timedelta(minutes=1))})
    events.append({"type": "parked", "at": _iso(NOW - timedelta(minutes=45)),
                   "detail": {"reason": "CI_PENDING", "code": check.TRAIN_WAITING}})
    return events


def _merge_step(*, started: datetime, rechecks: int, asked: int = 2, ran: int = 2) -> dict[str, Any]:
    wait = {"code": "checks_pending", "head": "c" * 40, "pull_request": 3412,
            "first_parked_at": _iso(NOW - timedelta(minutes=21)),
            "wait_started_at": _iso(started), "rechecks": rechecks,
            "failed_heads": {CHECK: ["a" * 40, "b" * 40]},
            "main_red": {"checks": [CHECK], "head": "d" * 40}}
    rounds = [{"round": n + 1, "head": h} for n, h in enumerate(["a" * 40, "b" * 40][:ran])]
    return {"id": "task_ffe63d609c5d4662ba8e", "state": "PARKED", "park_reason": "CI_PENDING",
            "runner_profile": "merge", "created_at": _iso(STARTED - timedelta(minutes=30)),
            "metadata": {"merge_wait": wait, "merge_fix_rounds": asked,
                         "merge_fix": {"rounds": rounds}},
            "waiting": {"reason": "CI_PENDING", "since": _iso(NOW - timedelta(minutes=21)),
                        "summary": "CI on PR #3412: 1 checks pending (ci-gate)",
                        "detail": {"head": "c" * 40, "pending": ["ci-gate"], "failed": [CHECK],
                                   "wakes": 12}}}


def test_a_merge_step_re_checked_30_times_over_8_hours_is_flagged_with_its_true_age():
    task = _merge_step(started=STARTED, rechecks=30)

    flag = check.classify_task(task, _Reads(_events()), now=NOW, window=20)

    # MUTATION (check.py `_ci_entered`): ignore `merge_wait.wait_started_at`
    # -> 21 minutes (the newest head's `first_parked_at`), not flagged.
    assert flag is not None and flag["rule"] == "parked", flag
    evidence = flag["evidence"]
    assert evidence["age_minutes"] == 8 * 60, evidence
    assert evidence["in_state_since"] == _iso(STARTED)
    assert evidence["age_is"] == "exact"
    # MUTATION (check.py `_ci_history`): drop the wait's record -> the
    # refunded `wakes` (12) and no rounds or main.
    assert evidence["re_checks"] == 30
    assert evidence["checks_failed"] == [CHECK]
    assert evidence["fix_rounds"] == {"ran": 2, "asked": 2, "spent": True}
    assert evidence["main_red_on"] == [CHECK]
    reason = flag["reason"]
    assert "CI_PENDING for exact 480 min" in reason
    assert "30 re-checks" in reason and CHECK in reason
    assert "fix rounds 2 of 2 spent" in reason and "main red on" in reason


def test_a_short_wait_is_not_flagged():
    task = _merge_step(started=NOW - timedelta(minutes=40), rechecks=3, ran=1)

    flag = check.classify_task(task, _Reads(_events()), now=NOW, window=20)

    # MUTATION: flag every recorded wait -> fails.
    assert flag is None, flag


def test_a_wait_handed_to_a_person_is_flagged_with_its_one_line():
    """The merge step parked HUMAN_REQUIRED (`ci_wait_stuck`): the API's line
    is the flag's reason, so it reaches the owner list as the evidence."""
    line = ("CI on PR #3412 waited 8 h 0 m over 30 re-checks; failing: unit (worker); "
            "fix rounds 2 of 2 spent; main is red on unit (worker) too")
    task = {"id": "task_ffe63d609c5d4662ba8e", "state": "PARKED", "park_reason": "HUMAN_REQUIRED",
            "runner_profile": "merge", "created_at": _iso(STARTED),
            "waiting": {"reason": "HUMAN_REQUIRED", "summary": line, "since": _iso(STARTED),
                        "detail": {"park_phase": "ci_wait_stuck", "re_checks": 30}}}
    events = [{"type": "parked", "at": _iso(NOW - timedelta(minutes=2)),
               "detail": {"reason": "HUMAN_REQUIRED"}}]

    flag = check.classify_task(task, _Reads(events), now=NOW, window=20)

    assert flag is not None and flag["rule"] == "parked" and flag["kind"] == check.STUCK
    assert line in flag["reason"]
