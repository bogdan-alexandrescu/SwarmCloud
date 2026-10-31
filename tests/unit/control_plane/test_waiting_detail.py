"""#1782 WAIT-API: a waiting task says why, what resumes it and when it is checked next.

`swarm_api.waiting_detail.build_waiting` is a PURE function of the task, its
newest events, the `waiting_for` reading (#362) and the tenant's related tasks
the route read in one batch. These tests build every reason from fixture tasks
and events -- task_31c301ecb33940eeb278's real CI_PENDING shape among them --
and hold the rules: a fact not recorded is null, never invented; nothing is
read from GitHub; a CREDENTIAL_MISSING detail never carries a value; the
listing reads nothing per row; another tenant's documents are never read.

Each assertion carries a MUTATION comment: the change to the builder that
would turn it red.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_common.models import Task, TaskEvent, Workflow, WorkflowStep
from swarm_common.states import EventType, ParkReason, TaskState

from swarm_api.codec import event_to_firestore, workflow_to_api
from swarm_api.waiting_detail import (
    HISTORY_MAX,
    READY_CAPACITY,
    build_waiting,
    waiting_row,
)

from .conftest import auth_header, seed_task, seed_tenant

READ_AT = datetime(2026, 10, 21, 20, 10, tzinfo=timezone.utc)
REPO = "https://github.com/bogdan-alexandrescu/SwarmCloud.git"
HEAD = "1968b677" + "c0ffee12" * 4
PENDING = ["checkov", "ci-gate", "python (unit)", "ui", "integration"]


def task(
    *,
    state: TaskState = TaskState.PARKED,
    park_reason: ParkReason | None = None,
    next_eligible_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    blocked_by: list[dict[str, Any]] | None = None,
    task_id: str = "task_w",
    tenant_id: str = "eng",
    **extra: Any,
) -> Task:
    return Task(
        id=task_id,
        tenant_id=tenant_id,
        created_at=READ_AT - timedelta(hours=2),
        updated_at=READ_AT - timedelta(minutes=30),
        state=state,
        runner_profile="mock",
        resource_class="standard",
        input={},
        submitted_by="alice@example.com",
        park_reason=park_reason,
        next_eligible_at=next_eligible_at,
        metadata=dict(metadata or {}),
        blocked_by=list(blocked_by or []),
        **extra,
    )


def event(kind: EventType, at: datetime, detail: dict[str, Any], *, task_id: str = "task_w",
          n: int = 0) -> TaskEvent:
    return TaskEvent(
        event_id=f"ev_{int(at.timestamp())}_{n}",
        task_id=task_id,
        tenant_id="eng",
        type=kind,
        at=at,
        detail=detail,
    )


# --------------------------------------------------------------------------
# CI_PENDING: task_31c301ecb33940eeb278, 2026-10-21
# --------------------------------------------------------------------------

FIRST_PARK = datetime(2026, 10, 21, 19, 15, tzinfo=timezone.utc)
SECOND_PARK = datetime(2026, 10, 21, 19, 31, 25, tzinfo=timezone.utc)
ELIGIBLE = datetime(2026, 10, 21, 19, 46, 25, tzinfo=timezone.utc)


def ci_task() -> tuple[Task, list[TaskEvent]]:
    t = task(
        task_id="task_31c301ecb33940eeb278",
        park_reason=ParkReason.CI_PENDING,
        next_eligible_at=ELIGIBLE,
        repository_url=REPO,
        workflow_id="wf_sizes_sched_2",
        step_id="merge",
        attempt_count=1,
        metadata={
            "merge_wait": {
                "code": "checks_pending",
                "head": HEAD,
                "pull_request": 1779,
                "pending": PENDING,
                "wakes": 2,
                "slot_waits": 0,
                "train_waits": 0,
                "updates": 0,
                "first_parked_at": FIRST_PARK,
                "parked_at": SECOND_PARK,
            }
        },
        blocked_by=[{"reason": "CI_PENDING", "code": "checks_pending", "head": HEAD,
                     "pending": PENDING}],
    )
    first = {"code": "checks_pending", "reason": "CI_PENDING", "head": HEAD, "pending": PENDING,
             "wakes": 1, "next_eligible_at": FIRST_PARK + timedelta(minutes=15),
             "attempt_refunded": True}
    second = {**first, "wakes": 2, "next_eligible_at": ELIGIBLE}
    ready = {"reason": "fallback_due", "park_reason": "CI_PENDING",
             "was_eligible_at": (FIRST_PARK + timedelta(minutes=15)).isoformat()}
    tid = t.id
    events = [
        event(EventType.PARKED, FIRST_PARK, first, task_id=tid),
        event(EventType.READY, FIRST_PARK + timedelta(minutes=15, seconds=20), ready, task_id=tid),
        event(EventType.LEASE_ACQUIRED, FIRST_PARK + timedelta(minutes=15, seconds=30), {},
              task_id=tid),
        event(EventType.RUNNING, FIRST_PARK + timedelta(minutes=16), {}, task_id=tid),
        event(EventType.PARKED, SECOND_PARK, second, task_id=tid),
    ]
    return t, events


def test_ci_pending_serves_the_pr_head_checks_and_recheck_schedule():
    t, events = ci_task()
    w = build_waiting(t, events=events, as_of=READ_AT)
    d = w["detail"]
    # MUTATION: serving the ParkReason under another name (e.g. "ci") turns this red.
    assert w["reason"] == "CI_PENDING"
    # MUTATION: dropping the PR, or building its URL with ".git" left in, turns this red.
    assert d["pull_request"] == {
        "number": 1779,
        "url": "https://github.com/bogdan-alexandrescu/SwarmCloud/pull/1779",
    }
    # MUTATION: reading head off anything but the recorded park turns this red.
    assert d["head"] == HEAD
    # MUTATION: truncating or re-sorting the pending names turns this red.
    assert d["pending"] == PENDING
    # MUTATION: inventing a failed list when none was recorded turns this red.
    assert d["failed"] is None
    # MUTATION: reading wakes from the first park instead of the newest turns this red.
    assert d["wakes"] == 2
    # MUTATION: ignoring attempt_refunded (or inverting it) turns this red.
    assert d["attempts_counted"] is False
    # MUTATION: labelling the CI detail with the read time, not the park's, turns this red.
    assert d["last_checked_at"] == SECOND_PARK
    # MUTATION: serving the first park as next check, or null, turns this red.
    assert w["next_check_at"] == ELIGIBLE
    # MUTATION: starting the wait at the newest park instead of the CI clock's start turns this red.
    assert w["since"] == FIRST_PARK
    # MUTATION: dropping the PR number, the count or a check name from the sentence turns this red.
    assert w["summary"].startswith(
        "CI on PR #1779: 5 checks pending (checkov, ci-gate, python (unit), ...); "
        "re-checked 2 times; next check 19:46Z"
    )
    # MUTATION: a resumes_when that does not name the checks settling turns this red.
    assert "checks" in w["resumes_when"]
    assert w["read_at"] == READ_AT


def test_ci_pending_past_its_check_says_it_is_still_parked_and_why():
    """'Eligible since X but still parked' explains itself (decision 2)."""
    t, events = ci_task()
    w = build_waiting(t, events=events, as_of=READ_AT)
    # MUTATION: removing the eligible-but-not-resumed clause turns this red.
    assert "eligible since 19:46Z, still parked: waiting for the scheduler's next pass" in w["summary"]
    # MUTATION: leaving the history without its closing "still parked" entry turns this red.
    assert w["history"][-1]["kind"] == "eligible"
    assert "still parked" in w["history"][-1]["text"]
    assert w["history"][-1]["at"] == ELIGIBLE


def test_eligible_but_parked_names_the_newest_other_blocker_when_there_is_one():
    t, events = ci_task()
    t.blocked_by = [{"reason": "TENANT_LIMIT", "pool": "tenant:eng", "active": 20, "limit": 20}]
    w = build_waiting(t, events=events, as_of=READ_AT)
    # MUTATION: always saying "the scheduler's next pass" when a blocker is recorded turns this red.
    assert "still parked: the last scheduler pass found tenant:eng refusing (TENANT_LIMIT)" in w["summary"]


def test_ci_pending_history_is_newest_last_and_names_each_park_and_wake():
    t, events = ci_task()
    w = build_waiting(t, events=events, as_of=READ_AT)
    kinds = [h["kind"] for h in w["history"]]
    # MUTATION: serving the history newest first, or dropping the ready/re-check rows, turns this red.
    assert kinds == ["parked", "ready", "running", "parked", "eligible"]
    ats = [h["at"] for h in w["history"]]
    # MUTATION: an unsorted history turns this red.
    assert ats == sorted(ats)
    # MUTATION: a ready row that does not say why it woke turns this red.
    assert "fallback" in w["history"][1]["text"]
    # MUTATION: a park row that drops the recorded pending count turns this red.
    assert "5 checks pending" in w["history"][3]["text"]


def test_history_is_capped_at_twenty_newest():
    t, _ = ci_task()
    many = [
        event(EventType.PARKED if i % 2 == 0 else EventType.READY,
              FIRST_PARK - timedelta(hours=2) + timedelta(minutes=i),
              {"reason": "CI_PENDING", "wakes": i}, n=i)
        for i in range(40)
    ]
    w = build_waiting(t, events=many, as_of=READ_AT)
    # MUTATION: raising or removing the cap turns this red.
    assert HISTORY_MAX == 20 and len(w["history"]) == 20
    # MUTATION: keeping the oldest twenty instead of the newest turns this red.
    assert w["history"][-1]["kind"] == "eligible"
    assert w["history"][-2]["at"] == FIRST_PARK - timedelta(hours=2) + timedelta(minutes=39)


def test_ci_pending_without_events_reads_the_merge_wait_record():
    """The listing reads no events: the row's summary comes from the task document."""
    t, _ = ci_task()
    w = build_waiting(t, events=None, as_of=READ_AT)
    # MUTATION: a listing summary that needs the events to name the PR turns this red.
    assert w["summary"].startswith("CI on PR #1779: 5 checks pending")
    # MUTATION: claiming refunded attempts when no event recorded it turns this red.
    assert w["detail"]["attempts_counted"] is None
    # MUTATION: a last_checked_at not taken from merge_wait.parked_at turns this red.
    assert w["detail"]["last_checked_at"] == SECOND_PARK
    # MUTATION: serving an empty history as if nothing happened turns this red.
    assert w["history_read"] is False


def test_ci_pending_with_nothing_recorded_serves_nulls_not_guesses():
    t = task(park_reason=ParkReason.CI_PENDING, next_eligible_at=None)
    w = build_waiting(t, events=[], as_of=READ_AT)
    d = w["detail"]
    # MUTATION: inventing a PR, a head, a pending list or a wake count turns this red.
    assert d["pull_request"] is None
    assert d["head"] is None
    assert d["pending"] is None
    assert d["wakes"] is None
    assert d["last_checked_at"] is None
    # MUTATION: inventing a next check time turns this red.
    assert w["next_check_at"] is None
    # MUTATION: a summary that claims a PR number turns this red.
    assert "#" not in w["summary"]


def test_ci_pending_pr_from_a_named_merge_target_when_the_park_recorded_none():
    t = task(park_reason=ParkReason.CI_PENDING, repository_url=REPO,
             metadata={"dispatch": {"merge_target": {"number": 1500, "head_sha": HEAD}}})
    w = build_waiting(t, events=[], as_of=READ_AT)
    # MUTATION: ignoring the signed dispatch block's named target turns this red.
    assert w["detail"]["pull_request"]["number"] == 1500


def test_ci_pending_records_failed_checks_when_the_park_did():
    t, events = ci_task()
    events[-1].detail["failed"] = ["checkov"]
    w = build_waiting(t, events=events, as_of=READ_AT)
    # MUTATION: dropping a recorded failed list turns this red.
    assert w["detail"]["failed"] == ["checkov"]
    # MUTATION: a summary that hides a failed check turns this red.
    assert "1 failed (checkov)" in w["summary"]


# --------------------------------------------------------------------------
# READY behind capacity
# --------------------------------------------------------------------------

def waiting_for_full() -> dict[str, Any]:
    lead = {"pool": "runner:merge", "state": "full", "active": 4, "limit": 4, "units": 1,
            "reason": "PROFILE_LIMIT"}
    return {"as_of": READ_AT, "admissible_now": False, "holds_capacity": False, "lead": lead,
            "pools": [lead], "complete": True}


def test_ready_behind_capacity_names_the_binding_pool_and_its_units():
    t = task(state=TaskState.READY)
    ev = [event(EventType.READY, READ_AT - timedelta(minutes=7), {"reason": "dependency_succeeded"})]
    w = build_waiting(t, events=ev, waiting_for=waiting_for_full(), as_of=READ_AT)
    d = w["detail"]
    # MUTATION: serving READY instead of READY_CAPACITY turns this red.
    assert w["reason"] == READY_CAPACITY == "READY_CAPACITY"
    # MUTATION: not reusing waiting_for's lead turns this red.
    assert d["pool"] == "runner:merge"
    assert d["pool_state"] == "full"
    assert d["active"] == 4 and d["limit"] == 4
    # MUTATION: dropping the live reading from the detail turns this red.
    assert d["waiting_for"]["lead"]["pool"] == "runner:merge"
    # MUTATION: inventing a place in line, or dropping the reason it is null, turns this red.
    assert d["place_in_line"] is None
    assert d["place_in_line_unavailable"]
    # MUTATION: a summary that does not name the binding pool turns this red.
    assert w["summary"] == "Ready; capacity binds on runner:merge (4 of 4 units in use)"
    # MUTATION: starting a READY wait anywhere but its ready event turns this red.
    assert w["since"] == READ_AT - timedelta(minutes=7)
    assert w["next_check_at"] is None


def test_ready_with_room_everywhere_waits_for_the_next_pass():
    t = task(state=TaskState.READY)
    wf = {**waiting_for_full(), "admissible_now": True, "lead": None, "pools": []}
    w = build_waiting(t, events=[], waiting_for=wf, as_of=READ_AT)
    # MUTATION: claiming a pool binds when every pool has room turns this red.
    assert w["summary"] == "Ready; every pool has room: waiting for the scheduler's next pass"


def test_ready_without_a_live_reading_falls_back_to_the_last_pass_record():
    t = task(state=TaskState.READY,
             blocked_by=[{"reason": "TENANT_LIMIT", "pool": "tenant:eng", "active": 20, "limit": 20}])
    w = build_waiting(t, events=None, waiting_for=None, as_of=READ_AT)
    # MUTATION: reading the record as if it were live (no "last scheduler pass") turns this red.
    assert w["summary"] == "Ready; the last scheduler pass found tenant:eng refusing (TENANT_LIMIT)"
    # MUTATION: inventing a pool reading the route never made turns this red.
    assert w["detail"]["waiting_for"] is None


# --------------------------------------------------------------------------
# Provider parks
# --------------------------------------------------------------------------

def test_provider_quota_names_provider_account_and_until():
    until = READ_AT + timedelta(minutes=50)
    t = task(park_reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED, next_eligible_at=until,
             provider="anthropic")
    ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=10), {
        "reason": "PROVIDER_QUOTA_EXHAUSTED", "provider": "anthropic", "provider_state": "EXHAUSTED",
        "source": "rate_limit_headers", "wait_seconds": 3600, "next_eligible_at": until,
        "account_id": "acct_team_1",
    })]
    w = build_waiting(t, events=ev, as_of=READ_AT)
    d = w["detail"]
    # MUTATION: dropping the recorded provider or account turns this red.
    assert d["provider"] == "anthropic"
    assert d["account"] == "acct_team_1"
    # MUTATION: an until not taken from next_eligible_at turns this red.
    assert d["until"] == until and w["next_check_at"] == until
    assert d["provider_state"] == "EXHAUSTED"
    # MUTATION: a summary without the provider and the window's end turns this red.
    assert w["summary"] == "Provider quota exhausted on anthropic (account acct_team_1); until 21:00Z"


def test_provider_cooldown_and_outage_with_no_account_serve_null():
    for reason in (ParkReason.PROVIDER_COOLDOWN, ParkReason.PROVIDER_OUTAGE):
        t = task(park_reason=reason, next_eligible_at=READ_AT + timedelta(minutes=5))
        ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=1),
                    {"reason": reason.value, "provider": "openai"})]
        w = build_waiting(t, events=ev, as_of=READ_AT)
        # MUTATION: inventing an account label turns this red.
        assert w["detail"]["account"] is None
        # MUTATION: losing the reason across the three provider parks turns this red.
        assert w["reason"] == reason.value
        assert "openai" in w["summary"]


# --------------------------------------------------------------------------
# SCHEDULED_RETRY
# --------------------------------------------------------------------------

def test_scheduled_retry_serves_attempt_n_of_n_and_the_next_try():
    nxt = READ_AT + timedelta(minutes=3)
    t = task(park_reason=ParkReason.SCHEDULED_RETRY, next_eligible_at=nxt, attempt_count=1,
             max_attempts=3, last_error="worker interrupted by SIGTERM")
    ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=2),
                {"reason": "SCHEDULED_RETRY", "cause": "worker_interrupted"})]
    w = build_waiting(t, events=ev, as_of=READ_AT)
    d = w["detail"]
    # MUTATION: serving attempts used as the next attempt (or N wrong) turns this red.
    assert d["attempt"] == 1 and d["next_attempt"] == 2 and d["max_attempts"] == 3
    # MUTATION: a next_try_at not from next_eligible_at turns this red.
    assert d["next_try_at"] == nxt
    # MUTATION: dropping the last error turns this red.
    assert d["last_error"] == "worker interrupted by SIGTERM"
    assert d["cause"] == "worker_interrupted"
    # MUTATION: a summary without the attempt numbers or time turns this red.
    assert w["summary"] == "Retry scheduled: attempt 2 of 3 at 20:13Z (worker_interrupted)"


# --------------------------------------------------------------------------
# DEPENDENCY_INCOMPLETE and CHILDREN_INCOMPLETE
# --------------------------------------------------------------------------

def test_dependency_incomplete_lists_each_upstream_step_and_its_state():
    t = task(park_reason=ParkReason.DEPENDENCY_INCOMPLETE, depends_on=["task_a", "task_b", "task_c"])
    ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=4),
                {"reason": "DEPENDENCY_INCOMPLETE", "waiting_on": ["task_a", "task_b"]})]
    related = {
        "task_a": task(task_id="task_a", state=TaskState.RUNNING, step_id="build"),
        "task_b": task(task_id="task_b", state=TaskState.QUEUED, step_id="test"),
    }
    w = build_waiting(t, events=ev, related=related, as_of=READ_AT)
    on = w["detail"]["waiting_on"]
    # MUTATION: serving depends_on instead of the recorded waiting_on turns this red.
    assert [x["task_id"] for x in on] == ["task_a", "task_b"]
    # MUTATION: serving the states recorded at the park instead of the current ones turns this red.
    assert [x["state"] for x in on] == ["RUNNING", "QUEUED"]
    assert [x["step_id"] for x in on] == ["build", "test"]
    # MUTATION: a summary that does not name the steps and their states turns this red.
    assert w["summary"] == "Waiting on 2 upstream steps: build (RUNNING), test (QUEUED)"


def test_a_dependency_not_read_is_null_not_a_state():
    t = task(park_reason=ParkReason.DEPENDENCY_INCOMPLETE, depends_on=["task_a"])
    w = build_waiting(t, events=[], related=None, as_of=READ_AT)
    # MUTATION: guessing a state for an unread (or another tenant's) task turns this red.
    assert w["detail"]["waiting_on"] == [{"task_id": "task_a", "step_id": None, "state": None}]
    assert w["detail"]["states_read"] is False


def test_children_incomplete_counts_the_unfinished_children():
    t = task(park_reason=ParkReason.CHILDREN_INCOMPLETE)
    kids = {
        "task_k1": task(task_id="task_k1", state=TaskState.RUNNING, parent_task_id="task_w"),
        "task_k2": task(task_id="task_k2", state=TaskState.SUCCEEDED, parent_task_id="task_w"),
        "task_k3": task(task_id="task_k3", state=TaskState.QUEUED, parent_task_id="task_w"),
    }
    w = build_waiting(t, events=[], related=kids, as_of=READ_AT)
    d = w["detail"]
    # MUTATION: counting finished children as waited-on turns this red.
    assert d["unfinished"] == 2 and d["total"] == 3
    assert {c["task_id"]: c["state"] for c in d["children"]} == {
        "task_k1": "RUNNING", "task_k2": "SUCCEEDED", "task_k3": "QUEUED"}
    # MUTATION: a summary without the counts turns this red.
    assert w["summary"] == "Waiting for 2 of 3 children to finish (1 QUEUED, 1 RUNNING)"
    # MUTATION: serving the park instant as a "next check" for a non-timed park turns this red.
    assert w["next_check_at"] is None


# --------------------------------------------------------------------------
# AWAITING_INPUT and HUMAN_REQUIRED
# --------------------------------------------------------------------------

def test_awaiting_input_says_who_may_answer_how_and_until_when():
    expires = READ_AT + timedelta(hours=3)
    opened = READ_AT - timedelta(hours=1)
    t = task(park_reason=ParkReason.AWAITING_INPUT, next_eligible_at=opened,
             workflow_id="wf_review", step_id="approve",
             metadata={"await_input": {"opened_at": opened, "expires_at": expires}})
    w = build_waiting(t, events=[], as_of=READ_AT)
    d = w["detail"]
    # MUTATION: naming anyone but the task's own tenant turns this red.
    assert d["who_may_answer"] == {"tenant_id": "eng", "text": "any member of workspace eng"}
    # MUTATION: dropping the window's end turns this red.
    assert d["expires_at"] == expires
    # MUTATION: an answer command with the wrong workflow or step turns this red.
    assert d["answer_command"] == "/sc answer wf_review approve"
    # MUTATION: starting the wait anywhere but the window's opening turns this red.
    assert w["since"] == opened
    # MUTATION: the park instant served as a next check turns this red.
    assert w["next_check_at"] is None
    assert w["summary"] == (
        "Waiting for a member of eng to answer; expires 23:10Z; /sc answer wf_review approve")


def test_human_required_says_a_person_resumes_it_from_the_console():
    t = task(park_reason=ParkReason.HUMAN_REQUIRED, next_eligible_at=READ_AT - timedelta(minutes=9))
    ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=9),
                {"reason": "HUMAN_REQUIRED", "cause": "hold_expired"})]
    w = build_waiting(t, events=ev, as_of=READ_AT)
    d = w["detail"]
    # MUTATION: an answer path that is not the human-ready route turns this red.
    assert d["answer_route"] == "POST /v1/tasks/task_w/human-ready"
    # MUTATION: inventing an expiry nobody recorded turns this red.
    assert d["expires_at"] is None
    assert d["who_may_answer"]["tenant_id"] == "eng"
    assert d["cause"] == "hold_expired"
    # MUTATION: an "eligible since, still parked" clause on a park only a person ends turns this red.
    assert "still parked" not in w["summary"]
    # MUTATION: a hand-off park described as an owner-question hold turns this red.
    assert w["summary"] == "Waiting for a person: sign in on its console page, then say it is ready"
    assert w["resumes_when"] == "a member of the workspace calls human-ready (the console's Ready button)"
    assert "park_phase" not in d


# --------------------------------------------------------------------------
# HUMAN_REQUIRED on the owner's questions: a merge step's hold (#2313)
# --------------------------------------------------------------------------

HELD = [
    {"task_id": "task_impl", "step_id": "implement", "question_index": 0,
     "question": "Keep the old route?"},
    {"task_id": "task_fix", "step_id": "fix", "question_index": 2,
     "question": "Rename the flag?"},
]


def owner_park(questions: list[dict[str, Any]], *, unreadable: int = 0) -> dict[str, Any]:
    """The park the merge step writes (`agent_worker.lifecycle._park_for_owner_questions`)."""
    return {"reason": "HUMAN_REQUIRED", "park_phase": "owner_questions",
            "owner_questions": {"code": "owner_questions_open", "questions": questions,
                                "unreadable": unreadable}}


def merge_task(**extra: Any) -> Task:
    return task(park_reason=ParkReason.HUMAN_REQUIRED, next_eligible_at=READ_AT - timedelta(minutes=9),
                workflow_id="wf_move", step_id="merge", **extra)


def test_a_merge_held_by_owner_questions_names_them_and_serves_no_ready_route():
    ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=9), owner_park(HELD))]
    w = build_waiting(merge_task(), events=ev, as_of=READ_AT)
    d = w["detail"]
    # MUTATION: today's sign-in sentence on an owner-question hold turns this red.
    assert w["summary"] == "Waiting for answers to 2 owner questions before merging (implement: 1, fix: 1)"
    assert "sign in" not in w["summary"]
    # MUTATION: serving the human-ready route, which only re-parks it, turns this red.
    assert "answer_route" not in d
    assert d["park_phase"] == "owner_questions"
    # MUTATION: dropping a question, or its asking step, task or index, turns this red.
    assert d["questions"] == [
        {"task_id": "task_impl", "step_id": "implement", "question_index": 0,
         "question": "Keep the old route?"},
        {"task_id": "task_fix", "step_id": "fix", "question_index": 2,
         "question": "Rename the flag?"},
    ]
    assert d["unreadable"] == 0
    # MUTATION: "any member of the tenant" (the answer route's rule is the submitter or an admin).
    assert d["who_may_answer"] == {
        "submitted_by": "alice@example.com", "admin": True,
        "text": "alice@example.com, who launched workflow wf_move, or an admin"}
    assert w["resumes_when"] == "the last named question is answered (it resumes on its own)"
    assert w["since"] == READ_AT - timedelta(minutes=9)
    assert w["next_check_at"] is None


def test_the_owner_question_hold_is_read_from_blocked_by_when_no_events_were_read():
    w = build_waiting(merge_task(blocked_by=[owner_park(HELD[:1])]), events=None, as_of=READ_AT)
    assert w["summary"] == "Waiting for answers to 1 owner question before merging (implement: 1)"
    assert [q["question_index"] for q in w["detail"]["questions"]] == [0]
    assert "answer_route" not in w["detail"]


def test_an_owner_question_hold_the_answer_route_cannot_read_still_says_ready():
    # FAIL CLOSED in swarm-api's answer route (`owner_questions_hold`): an
    # unreadable entry wakes nothing, so only human-ready returns it.
    ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=9), owner_park(HELD[:1], unreadable=1))]
    w = build_waiting(merge_task(), events=ev, as_of=READ_AT)
    assert w["summary"] == (
        "Waiting for answers to 1 owner question before merging (implement: 1); 1 entry the "
        "merge cannot read: say it is ready once it is answered")
    assert w["detail"]["answer_route"] == "POST /v1/tasks/task_w/human-ready"
    assert w["detail"]["unreadable"] == 1
    assert w["resumes_when"] == (
        "a member of the workspace calls human-ready once the questions are answered: "
        "the hold names an entry an answer cannot clear on its own")


def test_a_malformed_owner_question_record_serves_no_invented_question():
    bad = [{"task_id": "task_impl", "question_index": "0"}, "x", {"question_index": 1}]
    ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=9), owner_park(bad))]
    w = build_waiting(merge_task(), events=ev, as_of=READ_AT)
    # Every entry that is not a task id and an integer index is counted unreadable.
    assert w["detail"]["questions"] == []
    assert w["detail"]["unreadable"] == 3
    assert w["detail"]["answer_route"] == "POST /v1/tasks/task_w/human-ready"
    assert w["summary"].startswith("Waiting for the owner questions the review named before merging")


# --------------------------------------------------------------------------
# HUMAN_REQUIRED on the task's own blocking questions: an answer, not a sign-in (#2859)
# --------------------------------------------------------------------------

ASKED_AT = READ_AT - timedelta(hours=3)
LONG_QUESTION = ("Should the verdict step refuse a merge when the review names a blocking "
                 "owner question that was asked by a step outside the workflow, or only warn?")


def question_park(*, blocking: list[int] | None = None) -> dict[str, Any]:
    """The park the worker writes (`agent_worker.lifecycle._park_for_answers`)."""
    return {"reason": "HUMAN_REQUIRED", "park_phase": "question_wait",
            "question_wait": {
                "questions": [
                    {"question": LONG_QUESTION, "options": [{"label": "refuse", "description": "r"}],
                     "recommended": "refuse", "context": "c", "blocking": True},
                    {"question": "Name the flag?", "options": [], "recommended": None,
                     "context": None},
                    {"question": "Keep the old route?", "options": [], "recommended": None,
                     "context": None, "blocking": True},
                ],
                "blocking": [0, 2] if blocking is None else blocking,
                "attempt_id": "att_ask", "asked_at": ASKED_AT.isoformat()}}


def question_task(**extra: Any) -> Task:
    return task(park_reason=ParkReason.HUMAN_REQUIRED, next_eligible_at=ASKED_AT,
                workflow_id="wf_verdict", step_id="implement",
                blocked_by=[question_park()], **extra)


def test_a_question_wait_says_it_waits_for_the_owners_answer_not_a_sign_in():
    # task_aaf56ef0026746928cdf, 2026-10-30: `park_phase: question_wait` served the hand-off line.
    ev = [event(EventType.PARKED, ASKED_AT, question_park())]
    w = build_waiting(question_task(), events=ev, as_of=READ_AT)
    d = w["detail"]
    # MUTATION: today's sign-in sentence on a question wait turns this red.
    assert "sign in" not in w["summary"]
    # MUTATION: counting every question, not the blocking unanswered ones, says 3 and turns this red.
    assert w["summary"].startswith("Waiting for the owner's answer to 2 questions: \u201cShould the verdict")
    # MUTATION: quoting the question whole turns this red.
    assert LONG_QUESTION not in w["summary"]
    assert "\u2026\u201d" in w["summary"]
    # MUTATION: naming no answer path turns this red.
    assert w["summary"].endswith("; answer on its console page (Questions) or in Claude Code with /sc check")
    assert d["park_phase"] == "question_wait"
    # MUTATION: serving the human-ready route, which resumes it unanswered, turns this red.
    assert d["answer_route"] == "POST /v1/tasks/task_w/answers"
    assert d["questions_route"] == "GET /v1/tasks/task_w/questions"
    assert d["blocking"] == [0, 2] and d["unanswered"] == [0, 2]
    assert [q["question_index"] for q in d["questions"]] == [0, 2]
    assert d["questions"][1]["question"] == "Keep the old route?"
    assert d["who_may_answer"]["submitted_by"] == "alice@example.com"
    assert d["expires_at"] == ASKED_AT + timedelta(hours=72)
    assert w["since"] == ASKED_AT
    assert w["resumes_when"] == ("the owner answers the last blocking question (it resumes on its own); "
                                 "unanswered for 72 hours it is dead-lettered")


def test_a_question_wait_counts_only_the_questions_still_unanswered():
    adopted = question_park()["question_wait"]
    answers = [{"question_index": 0, "choice": "refuse"}]
    t = question_task(metadata={"question_wait": adopted, "owner_answers": answers})
    w = build_waiting(t, events=None, as_of=READ_AT)
    # MUTATION: ignoring the stored answer still says 2 and quotes question 0.
    assert w["summary"].startswith("Waiting for the owner's answer to 1 question: \u201cKeep the old route?\u201d;")
    assert w["detail"]["unanswered"] == [2]


def test_answers_to_an_earlier_wait_do_not_count_for_this_one():
    earlier = {**question_park()["question_wait"], "attempt_id": "att_before"}
    t = question_task(metadata={"question_wait": earlier,
                                "owner_answers": [{"question_index": 0, "choice": "refuse"}]})
    w = build_waiting(t, events=None, as_of=READ_AT)
    # MUTATION: reading another attempt's answers as this wait's says 1.
    assert w["summary"].startswith("Waiting for the owner's answer to 2 questions:")


def test_a_question_wait_quotes_its_question_redacted():
    value = "ghp_" + secrets.token_hex(18)
    park = question_park()
    park["question_wait"]["questions"][0]["question"] = f"Rotate {value}?"
    t = task(park_reason=ParkReason.HUMAN_REQUIRED, next_eligible_at=ASKED_AT, blocked_by=[park])
    w = build_waiting(t, events=None, as_of=READ_AT)
    # MUTATION: quoting the question unredacted turns this red.
    assert value not in json.dumps(w, default=str)
    assert w["summary"].startswith("Waiting for the owner's answer to 2 questions: \u201cRotate ")


def test_a_hand_off_park_keeps_the_sign_in_line():
    park = {"reason": "HUMAN_REQUIRED", "park_phase": "human_wait", "cause": "captcha"}
    t = task(park_reason=ParkReason.HUMAN_REQUIRED, next_eligible_at=ASKED_AT, blocked_by=[park])
    w = build_waiting(t, events=None, as_of=READ_AT)
    # MUTATION: the question line on a hand-off turns this red.
    assert w["summary"] == "Waiting for a person: sign in on its console page, then say it is ready"
    assert w["detail"]["answer_route"] == "POST /v1/tasks/task_w/human-ready"


def test_get_task_serves_a_question_waits_line(client, db):
    seed_tenant(db, "eng", max_active=20)
    seed_task(db, task_id="task_ask", tenant_id="eng", state="PARKED", park_reason="HUMAN_REQUIRED",
              next_eligible_at=ASKED_AT, workflow_id="wf_verdict")
    db.docs["tasks/task_ask"]["blocked_by"] = [question_park()]
    ev = event(EventType.PARKED, ASKED_AT, question_park(), task_id="task_ask")
    db.docs[f"tasks/task_ask/events/{ev.event_id}"] = event_to_firestore(ev)
    res = client.get("/v1/tasks/task_ask", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    w = res.json()["task"]["waiting"]
    assert w["reason"] == "HUMAN_REQUIRED"
    # MUTATION: the route serving the hand-off line for a question wait turns this red.
    assert w["summary"].startswith("Waiting for the owner's answer to 2 questions: ")
    assert w["detail"]["answer_route"] == "POST /v1/tasks/task_ask/answers"
    rows = {t["id"]: t for t in client.get("/v1/tasks", headers=auth_header("alice")).json()["tasks"]}
    # MUTATION: the listing's one line keeping the sign-in text turns this red.
    assert rows["task_ask"]["waiting"]["summary"] == w["summary"]


# --------------------------------------------------------------------------
# CREDENTIAL_MISSING: the name, never a value
# --------------------------------------------------------------------------

def test_credential_missing_serves_names_and_never_a_value():
    value = "sk-" + secrets.token_hex(24)
    t = task(park_reason=ParkReason.CREDENTIAL_MISSING, provider="anthropic")
    ev = [event(EventType.PARKED, READ_AT - timedelta(minutes=3), {
        "reason": "CREDENTIAL_MISSING",
        "provider": "anthropic",
        "account_pool": "no_account",
        "forge_connection": "refresh_failed",
        "workspace_secrets": [{"consumer": "tool:figma", "slot": "token", "value": value}],
        "mcp_servers": [{"name": "linear", "code": "mcp_credential_missing", "why": value}],
        "secret_value": value,
        "token": value,
    })]
    w = build_waiting(t, events=ev, as_of=READ_AT)
    d = w["detail"]
    # MUTATION: serving the detail as recorded (any value-bearing key) turns this red.
    assert value not in json.dumps(w, default=str)
    assert value[3:12] not in json.dumps(w, default=str)
    # MUTATION: dropping a recorded secret name turns this red.
    assert d["provider"] == "anthropic"
    assert d["secret_names"] == ["anthropic key", "tool:figma/token", "mcp:linear"]
    assert d["workspace_secrets"] == [{"consumer": "tool:figma", "slot": "token"}]
    assert d["mcp_servers"] == [{"name": "linear", "code": "mcp_credential_missing"}]
    assert d["forge_connection"] == "refresh_failed"
    assert d["account_pool"] == "no_account"
    # MUTATION: a summary without the missing names turns this red.
    assert w["summary"] == (
        "Credential missing: anthropic key, tool:figma/token, mcp:linear")
    assert w["resumes_when"] == (
        "the workspace registers the credential and the scheduler's credential sweep finds it")


def test_no_waiting_text_a_person_reads_says_tenant():
    """VQ-056 (BGD, 2026-10-26): the console speaks of workspaces. The
    `tenant_id` field keeps its name; the words a person reads do not say
    'tenant'."""
    opened = READ_AT - timedelta(hours=1)
    waits = [
        build_waiting(task(park_reason=ParkReason.AWAITING_INPUT, next_eligible_at=opened,
                           workflow_id="wf_review", step_id="approve",
                           metadata={"await_input": {"opened_at": opened}}),
                      events=[], as_of=READ_AT),
        build_waiting(task(park_reason=ParkReason.HUMAN_REQUIRED, next_eligible_at=opened),
                      events=[], as_of=READ_AT),
        build_waiting(merge_task(), as_of=READ_AT, events=[
            event(EventType.PARKED, opened, owner_park(HELD[:1], unreadable=1))]),
        build_waiting(task(park_reason=ParkReason.CREDENTIAL_MISSING, provider="anthropic"),
                      as_of=READ_AT, events=[
                          event(EventType.PARKED, opened, {"reason": "CREDENTIAL_MISSING"})]),
    ]
    for w in waits:
        read = [w["summary"], w["resumes_when"],
                str((w["detail"].get("who_may_answer") or {}).get("text") or "")]
        assert not [line for line in read if re.search(r"\btenants?\b", line, re.I)], read
    assert len(waits) == 4


# --------------------------------------------------------------------------
# BUDGET_EXHAUSTED and MANUAL_PAUSE
# --------------------------------------------------------------------------

def test_budget_exhausted_names_the_budget_or_null():
    t = task(park_reason=ParkReason.BUDGET_EXHAUSTED)
    ev = [event(EventType.PARKED, READ_AT, {"reason": "BUDGET_EXHAUSTED", "budget": "monthly_budget_usd"})]
    # MUTATION: dropping the recorded budget name turns this red.
    assert build_waiting(t, events=ev, as_of=READ_AT)["detail"]["budget"] == "monthly_budget_usd"
    # MUTATION: inventing a budget nobody recorded turns this red.
    assert build_waiting(t, events=[], as_of=READ_AT)["detail"]["budget"] is None


def test_manual_pause_names_who_and_when_if_recorded():
    at = READ_AT - timedelta(hours=1)
    t = task(park_reason=ParkReason.MANUAL_PAUSE)
    ev = [event(EventType.PARKED, at, {"reason": "MANUAL_PAUSE", "error": "tenant is missing or disabled"})]
    w = build_waiting(t, events=ev, as_of=READ_AT)
    # MUTATION: inventing who paused it turns this red.
    assert w["detail"]["paused_by"] is None
    # MUTATION: a paused_at not from the park event turns this red.
    assert w["detail"]["paused_at"] == at
    assert w["detail"]["error"] == "tenant is missing or disabled"
    assert w["summary"] == "Paused: tenant is missing or disabled"
    ev[0].detail["paused_by"] = "root@example.com"
    # MUTATION: ignoring a recorded pauser turns this red.
    assert build_waiting(t, events=ev, as_of=READ_AT)["detail"]["paused_by"] == "root@example.com"


# --------------------------------------------------------------------------
# QUEUED, and what is not waiting
# --------------------------------------------------------------------------

def test_queued_and_states_that_are_not_waiting():
    q = task(state=TaskState.QUEUED)
    w = build_waiting(q, events=[], as_of=READ_AT)
    # MUTATION: serving no waiting for QUEUED turns this red.
    assert w["reason"] == "QUEUED"
    assert w["since"] == q.created_at
    for state in (TaskState.RUNNING, TaskState.LEASED, TaskState.SUCCEEDED, TaskState.FAILED):
        # MUTATION: serving a wait for a task that holds or finished its lease turns this red.
        assert build_waiting(task(state=state), events=[], as_of=READ_AT) is None


def test_waiting_row_is_reason_and_summary_only():
    t, events = ci_task()
    row = waiting_row(build_waiting(t, events=None, as_of=READ_AT))
    # MUTATION: serving the full detail on every list row turns this red.
    assert set(row) == {"reason", "summary"}
    assert waiting_row(None) is None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def _seed_ci(db) -> None:
    seed_tenant(db, "eng", max_active=20)
    t, events = ci_task()
    seed_task(db, task_id=t.id, tenant_id="eng", state="PARKED", park_reason="CI_PENDING",
              next_eligible_at=ELIGIBLE, workflow_id="wf_sizes_sched_2")
    doc = db.docs[f"tasks/{t.id}"]
    doc.update({"metadata": t.metadata, "repository_url": REPO, "attempt_count": 1,
                "step_id": "merge", "blocked_by": t.blocked_by})
    for e in events:
        db.docs[f"tasks/{t.id}/events/{e.event_id}"] = event_to_firestore(e)


def test_get_task_serves_waiting_in_full(client, db):
    _seed_ci(db)
    res = client.get("/v1/tasks/task_31c301ecb33940eeb278", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    w = res.json()["task"]["waiting"]
    # MUTATION: not serving `waiting` on the GET, or serving the row form, turns this red.
    assert w["reason"] == "CI_PENDING"
    assert w["detail"]["pull_request"]["number"] == 1779
    assert w["detail"]["pending"] == PENDING
    # MUTATION: the GET not reading the task's events turns this red.
    assert w["detail"]["attempts_counted"] is False
    assert w["history_read"] is True
    assert [h["kind"] for h in w["history"]][:2] == ["parked", "ready"]


def test_list_tasks_serves_the_row_form_with_no_event_reads(client, db, monkeypatch):
    _seed_ci(db)
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")
    from swarm_api.store import Store

    def no_events(*args, **kwargs):
        raise AssertionError("a listing reads no events per row")

    monkeypatch.setattr(Store, "list_events", no_events)
    for view in ("", "?view=summary"):
        res = client.get(f"/v1/tasks{view}", headers=auth_header("alice"))
        assert res.status_code == 200, res.text
        rows = {t["id"]: t for t in res.json()["tasks"]}
        # MUTATION: a listing without the one-line reason turns this red.
        assert rows["task_31c301ecb33940eeb278"]["waiting"]["reason"] == "CI_PENDING"
        assert rows["task_31c301ecb33940eeb278"]["waiting"]["summary"].startswith("CI on PR #1779")
        # MUTATION: the full detail on a list row turns this red.
        assert set(rows["task_31c301ecb33940eeb278"]["waiting"]) == {"reason", "summary"}
        # MUTATION: a wait served for a finished task turns this red.
        assert rows["task_done"]["waiting"] is None


def test_the_get_reads_no_other_tenants_task(client, db):
    """Invariant 9: a dependency id naming another tenant's task is not read into the answer."""
    seed_tenant(db, "eng", max_active=20)
    seed_tenant(db, "research", max_active=5)
    seed_task(db, task_id="task_theirs", tenant_id="research", state="RUNNING")
    seed_task(db, task_id="task_mine", tenant_id="eng", state="PARKED",
              park_reason="DEPENDENCY_INCOMPLETE", depends_on=("task_theirs",))
    res = client.get("/v1/tasks/task_mine", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    on = res.json()["task"]["waiting"]["detail"]["waiting_on"]
    # MUTATION: reading the dependency without the tenant filter serves RUNNING here.
    assert on == [{"task_id": "task_theirs", "step_id": None, "state": None}]


def test_the_get_serves_a_parents_children_from_its_own_tenant(client, db):
    """CHILDREN_INCOMPLETE through the route: one tenant-scoped read of the children."""
    seed_tenant(db, "eng", max_active=20)
    seed_tenant(db, "research", max_active=5)
    seed_task(db, task_id="task_parent", tenant_id="eng", state="PARKED",
              park_reason="CHILDREN_INCOMPLETE")
    seed_task(db, task_id="task_k1", tenant_id="eng", state="RUNNING")
    seed_task(db, task_id="task_k2", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="task_kx", tenant_id="research", state="QUEUED")
    for kid in ("task_k1", "task_k2", "task_kx"):
        db.docs[f"tasks/{kid}"]["parent_task_id"] = "task_parent"
    res = client.get("/v1/tasks/task_parent", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    d = res.json()["task"]["waiting"]["detail"]
    # MUTATION: a children read that raises (list_tasks without submitted_by) serves
    # states_read False and children None, and turns this red.
    assert d["states_read"] is True
    # MUTATION: reading the children without the tenant filter serves task_kx here.
    assert d["children"] == [{"task_id": "task_k1", "state": "RUNNING"},
                             {"task_id": "task_k2", "state": "SUCCEEDED"}]
    assert d["unfinished"] == 1 and d["total"] == 2


def test_a_get_with_waiting_writes_nothing(client, db):
    _seed_ci(db)
    before = db.dump()
    begun = db.transactions_begun
    assert client.get("/v1/tasks/task_31c301ecb33940eeb278", headers=auth_header("alice")).status_code == 200
    # MUTATION: any write or transaction on the read path turns this red.
    assert db.transactions_begun == begun
    assert db.dump() == before


def test_served_waiting_is_masked(client, db):
    """Everything served passes the task's masker: a recorded error naming a secret is masked."""
    seed_tenant(db, "eng", max_active=20)
    value = "ghp_" + secrets.token_hex(18)
    seed_task(db, task_id="task_p", tenant_id="eng", state="PARKED", park_reason="MANUAL_PAUSE")
    ev = event(EventType.PARKED, READ_AT, {"reason": "MANUAL_PAUSE", "error": f"token {value} refused"},
               task_id="task_p")
    db.docs[f"tasks/task_p/events/{ev.event_id}"] = event_to_firestore(ev)
    res = client.get("/v1/tasks/task_p", headers=auth_header("alice"))
    assert res.status_code == 200, res.text
    # MUTATION: serving the builder's output without the masker turns this red.
    assert value not in res.text


def test_workflow_steps_serve_the_waiting_summary():
    t, _ = ci_task()
    wf = Workflow(
        workflow_id="wf_sizes_sched_2", tenant_id="eng", created_at=READ_AT, updated_at=READ_AT,
        state=TaskState.RUNNING, submitted_by="alice@example.com",
        steps=[WorkflowStep(step_id="merge", runner_profile="mock", input={}, task_id=t.id),
               WorkflowStep(step_id="later", runner_profile="mock", input={}, task_id=None)],
    )
    served = workflow_to_api(wf, None, step_tasks={t.id: t}, summary=True)
    steps = {s["step_id"]: s for s in served["steps"]}
    # MUTATION: a workflow step view without the waiting summary turns this red.
    assert steps["merge"]["waiting"]["reason"] == "CI_PENDING"
    assert steps["merge"]["waiting"]["summary"].startswith("CI on PR #1779")
    # MUTATION: inventing a wait for a step with no task turns this red.
    assert steps["later"]["waiting"] is None


# --------------------------------------------------------------------------
# The wake's words come from merge_wait.wake_reason (F05, part of #2379)
# --------------------------------------------------------------------------

WOKE_AT = SECOND_PARK + timedelta(minutes=11, seconds=45)


def woken_task(wake_reason: str | None) -> tuple[Task, list[TaskEvent]]:
    """task_c9c3a5301531437bad42's shape: parked CI_PENDING, then woken by the tick."""
    t, events = ci_task()
    t.state = TaskState.READY
    t.park_reason = None
    wait = dict(t.metadata["merge_wait"])
    wait["wake_requested_at"] = WOKE_AT - timedelta(seconds=5)
    if wake_reason is not None:
        wait["wake_reason"] = wake_reason
    t.metadata = {"merge_wait": wait}
    ready = {"reason": "wake_requested", "park_reason": "CI_PENDING",
             "was_eligible_at": ELIGIBLE.isoformat()}
    return t, [*events, event(EventType.READY, WOKE_AT, ready, task_id=t.id, n=1)]


def _ready_texts(w: dict[str, Any]) -> list[str]:
    return [h["text"] for h in w["history"] if h["kind"] == "ready"]


def test_a_wake_on_head_moved_says_the_head_moved_not_that_the_checks_settled():
    t, events = woken_task("head_moved")
    w = build_waiting(t, events=events, as_of=READ_AT)
    newest = _ready_texts(w)[-1]
    # MUTATION: mapping every wake_requested to "read the checks settled" turns this red.
    assert "the head moved" in newest
    assert "checks settled" not in newest
    # MUTATION: dropping the park the step woke from turns this red.
    assert newest.endswith("(was CI_PENDING)")


def test_a_wake_on_settled_checks_still_says_the_checks_settled():
    for settled in ("green", "red"):
        t, events = woken_task(settled)
        w = build_waiting(t, events=events, as_of=READ_AT)
        # MUTATION: losing the settled words for a green or red reading turns this red.
        assert _ready_texts(w)[-1] == (
            "ready: woke: swarm-api's tick read the checks settled (was CI_PENDING)")


def test_a_wake_whose_reason_is_not_recorded_claims_no_reason():
    t, events = woken_task(None)
    w = build_waiting(t, events=events, as_of=READ_AT)
    newest = _ready_texts(w)[-1]
    # MUTATION: inventing "checks settled" for a wake with no recorded reason turns this red.
    assert "checks settled" not in newest
    assert "reason not recorded" in newest


def test_the_wake_reason_names_only_the_wake_it_was_recorded_for():
    """merge_wait holds the newest wake's reason: an older wake does not inherit it."""
    t, events = woken_task("head_moved")
    old = event(EventType.READY, FIRST_PARK + timedelta(minutes=5),
                {"reason": "wake_requested", "park_reason": "CI_PENDING"}, task_id=t.id, n=2)
    w = build_waiting(t, events=[old, *events], as_of=READ_AT)
    texts = _ready_texts(w)
    # MUTATION: giving every wake_requested row the newest wake_reason turns this red.
    assert "the head moved" not in texts[0] and "reason not recorded" in texts[0]
    assert "the head moved" in texts[-1]


def test_the_wake_requested_row_of_a_parked_step_words_the_reason():
    t, events = ci_task()
    t.metadata["merge_wait"] = {**t.metadata["merge_wait"],
                                "wake_requested_at": READ_AT - timedelta(minutes=1),
                                "wake_reason": "head_moved"}
    w = build_waiting(t, events=events, as_of=READ_AT)
    wake = [h for h in w["history"] if h["kind"] == "wake"]
    # MUTATION: serving the raw code, or "checks settled", on the wake row turns this red.
    assert wake and wake[0]["text"] == (
        "wake requested: swarm-api's tick read that the head moved")
