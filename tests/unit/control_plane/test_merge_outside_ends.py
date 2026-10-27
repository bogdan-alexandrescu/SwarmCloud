"""A merge held on owner questions ends when its pull request ends outside the workflow.

MERGE-OUTSIDE-ENDS, part of #2441 (BGD 2026-10-26). JOIN-UI-ADMIN's merge step
parked PARKED(HUMAN_REQUIRED) on one owner question; its PR 2387 was merged by
hand, and the step stayed parked: only an answer wakes that park, and the
scheduler's human-wait expiry spares it. Now:

  1. swarm-api's merge wake tick reads the tenant's owner-question holds too,
     with that tenant's token, and marks a hold whose pull request is merged
     or closed (`pull_request_merged` / `pull_request_closed`), binding the
     mark to the park instant it read. An open pull request is a reading, not
     a mark. Another tenant's hold is never read (invariant 9). The tick never
     moves the task (invariant 1).
  2. The scheduler returns a hold to READY on a mark bound to the park it
     reads, and only then; a hold with an open pull request is still spared
     by the human-wait expiry, as before.

The worker's half -- the woken step ends SUCCEEDED, CANCELLED or parks again --
is tests/unit/worker/test_merge_ends_outside_the_workflow.py.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_worker import merge as worker_merge
from scheduler import loop as loop_mod
from swarm_api import mergewake
from swarm_common.states import EventType, ParkReason

from .conftest import seed_pool, seed_task, seed_tenant
# The merge wake tick's own fixtures: one tenant's token, a fake GitHub, a clock.
from .test_merge_wake import (  # noqa: F401 - pytest fixtures
    NOW,
    NUMBER,
    HEAD,
    REPO_URL,
    _reads,
    _tick,
    clock,
    github,
    tenant_tokens,
    wake_client,
)

PARKED_AT = NOW - timedelta(hours=5)
#: The opener's id as swarm-api mints them (`continuation.TASK_ID_RE`).
OPENER = "task_" + "1" * 20
RESEARCH_OPENER = "task_" + "2" * 20
QUESTION = {"task_id": "task_impl", "step_id": "implement", "question_index": 0,
            "question": "Join the list?"}


def held_merge(db, task_id: str = "task_merge", *, tenant_id: str = "eng",
               opener: str = OPENER, parked_at: datetime = PARKED_AT) -> dict:
    """A merge step PARKED on the owner-questions hold, as the worker writes it."""
    if f"tasks/{opener}" not in db.docs:
        opened = seed_task(db, task_id=opener, tenant_id=tenant_id, state="SUCCEEDED")
        opened["result_summary"] = {"git": {"pushed_head": HEAD,
                                            "pull_request": {"number": NUMBER}}}
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="PARKED",
                    runner_profile="merge", park_reason=ParkReason.HUMAN_REQUIRED.value,
                    next_eligible_at=parked_at)
    doc["repository_url"] = REPO_URL
    doc["blocked_by"] = [{"reason": ParkReason.HUMAN_REQUIRED.value,
                          "park_phase": "owner_questions",
                          "owner_questions": {"code": "owner_questions_open",
                                              "questions": [dict(QUESTION)], "unreadable": 0}}]
    doc["metadata"] = {"dispatch": {"merge_target": {"pull_request": opener}}}
    return doc


def _hold(db, task_id: str = "task_merge") -> dict:
    wait = db.docs[f"tasks/{task_id}"]["metadata"].get(mergewake.MERGE_WAIT_METADATA_KEY) or {}
    return wait.get(mergewake.OWNER_HOLD_KEY) or {}


def _end_pull(github, *, merged: bool) -> None:
    github.pulls[NUMBER].update(state="closed", merged=merged)


# --------------------------------------------------------------------------
# 1. the tick
# --------------------------------------------------------------------------

def test_a_hold_whose_pull_request_merged_is_marked_and_not_moved(db, wake_client, github):
    held_merge(db)
    _end_pull(github, merged=True)
    before = {k: v for k, v in db.docs["tasks/task_merge"].items() if k != "metadata"}

    response = _tick(wake_client)

    assert response.status_code == 200, response.text
    report = response.json()["report"]
    # MUTATION: read no owner-questions hold (the #2441 bug) and nothing is marked.
    assert report["owner_holds"] == {"visited": 1, "woken": 1, "waiting": 0}
    hold = _hold(db)
    assert hold[mergewake.WAKE_MARKER] == NOW
    assert hold["wake_reason"] == mergewake.PULL_REQUEST_MERGED
    # MUTATION: bind the mark to no park and the scheduler cannot tell it from a stale one.
    assert hold["parked_at"] == PARKED_AT
    assert hold["pull_request"] == NUMBER
    wait = db.docs["tasks/task_merge"]["metadata"][mergewake.MERGE_WAIT_METADATA_KEY]
    assert wait[mergewake.WAKE_MARKER] == NOW
    assert wait["wake_reason"] == mergewake.PULL_REQUEST_MERGED
    # The tick never moves the task: READY is the scheduler's write (invariant 1).
    after = {k: v for k, v in db.docs["tasks/task_merge"].items() if k != "metadata"}
    assert after == before
    # It read the pull request alone: no checks, no rules.
    assert _reads(github) == [f"https://api.github.com/repos/example-org/widgets/pulls/{NUMBER}"]


def test_a_hold_whose_pull_request_closed_unmerged_is_marked_closed(db, wake_client, github):
    held_merge(db)
    _end_pull(github, merged=False)

    report = _tick(wake_client).json()["report"]

    assert report["owner_holds"]["woken"] == 1
    # MUTATION: read a closed pull request as merged and this says merged.
    assert _hold(db)["wake_reason"] == mergewake.PULL_REQUEST_CLOSED


def test_a_hold_whose_pull_request_is_open_is_read_not_marked(db, wake_client, github):
    held_merge(db)

    body = _tick(wake_client).json()
    report = body["report"]

    assert report["owner_holds"] == {"visited": 1, "woken": 0, "waiting": 1}, body
    hold = _hold(db)
    # MUTATION: mark every hold read and an open pull request wakes the step.
    assert mergewake.WAKE_MARKER not in hold
    assert hold["checked_at"] == NOW and hold["parked_at"] == PARKED_AT
    wait = db.docs["tasks/task_merge"]["metadata"][mergewake.MERGE_WAIT_METADATA_KEY]
    assert mergewake.WAKE_MARKER not in wait


def test_a_hold_is_read_at_most_once_per_ci_read_seconds(db, wake_client, github, clock):
    held_merge(db)
    _tick(wake_client)
    first = len(_reads(github))

    clock.now = NOW + timedelta(seconds=10)
    _tick(wake_client)

    # MUTATION: drop the `_due` bound and every tick reads every hold again.
    assert len(_reads(github)) == first == 1


def test_a_mark_bound_to_an_earlier_park_does_not_stop_the_reads(db, wake_client, github):
    doc = held_merge(db)
    doc["metadata"][mergewake.MERGE_WAIT_METADATA_KEY] = {mergewake.OWNER_HOLD_KEY: {
        "parked_at": PARKED_AT - timedelta(hours=1), "checked_at": NOW,
        mergewake.WAKE_MARKER: NOW - timedelta(hours=1),
        "wake_reason": mergewake.PULL_REQUEST_MERGED}}
    _end_pull(github, merged=False)

    report = _tick(wake_client).json()["report"]

    # MUTATION: honour a mark from another park and the closed pull request is never seen.
    assert report["owner_holds"]["woken"] == 1
    hold = _hold(db)
    assert hold["parked_at"] == PARKED_AT
    assert hold["wake_reason"] == mergewake.PULL_REQUEST_CLOSED


def test_another_tenants_hold_is_never_read(db, wake_client, github, tenant_tokens):
    held_merge(db, "task_research", tenant_id="research", opener=RESEARCH_OPENER)
    _end_pull(github, merged=True)

    report = _tick(wake_client, "eng").json()["report"]

    # MUTATION: drop the tenant filter and research's hold is read with eng's token.
    assert "owner_holds" not in report
    assert _hold(db, "task_research") == {}
    assert _reads(github) == []
    assert tenant_tokens.asked == []


def test_only_the_named_tenants_hold_is_read_with_its_own_token(
    db, wake_client, github, tenant_tokens
):
    held_merge(db, "task_eng")
    held_merge(db, "task_research", tenant_id="research", opener=RESEARCH_OPENER)
    _end_pull(github, merged=True)

    report = _tick(wake_client, "eng").json()["report"]

    assert report["owner_holds"]["woken"] == 1
    assert _hold(db, "task_eng")["wake_reason"] == mergewake.PULL_REQUEST_MERGED
    assert _hold(db, "task_research") == {}
    assert tenant_tokens.asked == ["swarm-tenant-eng-git"]
    eng_token = tenant_tokens.issued["swarm-tenant-eng-git"]
    for _, url, headers, _ in github.calls:
        assert eng_token in headers["Authorization"], url


def test_an_ordinary_human_park_is_not_read(db, wake_client, github, tenant_tokens):
    doc = held_merge(db)
    doc["blocked_by"] = [{"reason": ParkReason.HUMAN_REQUIRED.value, "park_phase": "handoff"}]
    _end_pull(github, merged=True)

    report = _tick(wake_client).json()["report"]

    # MUTATION: read every HUMAN_REQUIRED park and a browser hand-off is marked.
    assert "owner_holds" not in report
    assert _reads(github) == [] and tenant_tokens.asked == []


# --------------------------------------------------------------------------
# 2. the scheduler
# --------------------------------------------------------------------------

def _scheduler_world(db) -> None:
    seed_pool(db, "global", hard_limit=0)
    seed_tenant(db, "eng")


def _scheduler_hold(db, *, mark_parked_at: datetime | None, marked: bool = True) -> dict:
    """A hold parked 25 hours ago, past the hand-off maximum, as the tick left it."""
    parked_at = datetime.now(timezone.utc) - timedelta(hours=25)
    doc = held_merge(db, parked_at=parked_at)
    doc["runner_profile"] = "mock"
    doc.update({"attempt_count": 1, "max_attempts": 10})
    hold = {"parked_at": mark_parked_at or parked_at, "pull_request": NUMBER,
            "checked_at": parked_at}
    if marked:
        hold.update({loop_mod.MERGE_WAKE_MARKER: parked_at,
                     "wake_reason": mergewake.PULL_REQUEST_MERGED})
    doc["metadata"][loop_mod.MERGE_WAIT_METADATA_KEY] = {loop_mod.OWNER_HOLD_KEY: hold}
    return doc


def _events(db, task_id: str = "task_merge") -> list[dict]:
    return [d for path, d in db.docs.items() if path.startswith(f"tasks/{task_id}/events/")]


def test_the_scheduler_returns_a_marked_hold_to_ready(db, make_scheduler):
    _scheduler_world(db)
    _scheduler_hold(db, mark_parked_at=None)

    make_scheduler().drain()

    task = db.docs["tasks/task_merge"]
    # MUTATION: leave the hold to an answer alone (the #2441 bug) and it stays PARKED.
    assert task["state"] == "READY", task["state"]
    # READY alone: no lease, nothing counted (invariant 1).
    assert task["current_lease_id"] is None
    assert db.docs["pools/global"].get("active", 0) == 0
    ready = [e for e in _events(db) if e.get("type") == EventType.READY.value]
    assert ready and ready[-1]["detail"]["reason"] == "wake_requested"
    assert ready[-1]["detail"]["wake_reason"] == mergewake.PULL_REQUEST_MERGED


def test_a_mark_from_an_earlier_park_wakes_nothing(db, make_scheduler):
    _scheduler_world(db)
    _scheduler_hold(db, mark_parked_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    make_scheduler().drain()

    task = db.docs["tasks/task_merge"]
    # MUTATION: wake on any mark and a step parked again on an open pull
    # request is woken by its old mark on every drain.
    assert task["state"] == "PARKED"
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value


def test_a_hold_with_an_open_pull_request_still_outlives_the_hand_off_maximum(
    db, make_scheduler
):
    _scheduler_world(db)
    _scheduler_hold(db, mark_parked_at=None, marked=False)

    make_scheduler().drain()

    task = db.docs["tasks/task_merge"]
    # As before #2441: neither promoted nor dead-lettered at 24 hours.
    assert task["state"] == "PARKED"
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value


# --------------------------------------------------------------------------
# 3. the restatements
# --------------------------------------------------------------------------

def test_the_three_images_name_the_hold_and_its_reasons_alike():
    assert mergewake.OWNER_HOLD_KEY == loop_mod.OWNER_HOLD_KEY == worker_merge.OWNER_HOLD_KEY
    assert mergewake.OWNER_QUESTIONS_OPEN == worker_merge.OWNER_QUESTIONS_OPEN
    assert mergewake.PULL_REQUEST_MERGED == worker_merge.PULL_REQUEST_MERGED
    assert mergewake.PULL_REQUEST_CLOSED == worker_merge.PULL_REQUEST_CLOSED
    assert mergewake.WAKE_MARKER == loop_mod.MERGE_WAKE_MARKER
    assert mergewake.MERGE_WAIT_METADATA_KEY == loop_mod.MERGE_WAIT_METADATA_KEY
