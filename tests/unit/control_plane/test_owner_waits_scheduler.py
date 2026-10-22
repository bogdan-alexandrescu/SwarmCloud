"""The scheduler leaves an agent's owner waits to swarm-api (lane AU-7, agent-autonomy §3.8-§3.9).

A worker parks an agent's release wait as PARKED(DEPENDENCY_INCOMPLETE) with a
`release_wait` record in `blocked_by`, and a blocking question as
PARKED(HUMAN_REQUIRED) with a `question_wait` record. Both are swarm-api's to
end: the merge-wake tick returns a release wait on a terminal release, and the
question sweep holds a blocking question for 72 hours. The scheduler's own
sweeps would otherwise promote the first at once (it has no `depends_on`) and
dead-letter the second at the 24-hour hand-off maximum.

Each test carries its control: the same park without the record is ended or
promoted by the same drain, so the survival is the record's doing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .conftest import seed_task, seed_tenant


def _park(db, task_id: str, reason: str, *, hours_ago: float, block: dict) -> dict:
    doc = seed_task(
        db,
        task_id=task_id,
        tenant_id="eng",
        state="PARKED",
        park_reason=reason,
        next_eligible_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )
    doc.update({"attempt_count": 1, "blocked_by": [block]})
    return doc


def test_a_release_wait_is_not_promoted_by_the_dependency_sweep(db, make_scheduler):
    seed_tenant(db, "eng")
    waiting = _park(
        db, "task-release-wait", "DEPENDENCY_INCOMPLETE", hours_ago=0.1,
        block={"park_phase": "release_wait",
               "release_wait": {"sha": "a" * 40, "attempt_id": "att-1",
                                "asked_at": datetime.now(timezone.utc).isoformat()}},
    )
    # The control: a dependency park with no dependencies and no release wait.
    plain = _park(db, "task-plain", "DEPENDENCY_INCOMPLETE", hours_ago=0.1,
                  block={"park_phase": "dependencies"})
    make_scheduler().drain()
    # MUTATION: removing the `owner_wait(task, "release_wait")` check in
    # `Scheduler._resolve_dependency` promotes the release wait as "no_dependencies".
    assert waiting["state"] == "PARKED" and waiting["park_reason"] == "DEPENDENCY_INCOMPLETE"
    assert plain["state"] != "PARKED"


def test_a_blocking_question_outlives_the_hand_off_maximum(db, make_scheduler):
    seed_tenant(db, "eng")
    asking = _park(
        db, "task-question-wait", "HUMAN_REQUIRED", hours_ago=25,
        block={"park_phase": "question_wait",
               "question_wait": {"questions": [{"id": "q1", "text": "Which base branch?"}],
                                 "blocking": ["q1"], "attempt_id": "att-1",
                                 "asked_at": datetime.now(timezone.utc).isoformat()}},
    )
    # The control: a 25-hour live-browser hand-off park, past its 24-hour maximum.
    handoff = _park(db, "task-handoff", "HUMAN_REQUIRED", hours_ago=25,
                    block={"park_phase": "handoff"})
    make_scheduler().drain()
    # MUTATION: removing the `owner_wait(task, "question_wait")` check in
    # `Scheduler._expire_human_waits` dead-letters the question at 24 hours.
    assert asking["state"] == "PARKED" and asking["park_reason"] == "HUMAN_REQUIRED"
    assert handoff["state"] == "DEAD_LETTERED"
