"""The notification feed (#1206, lane NOTIF-API; docs/notifications.md).

`GET /v1/notifications` is DERIVED on every read from records other paths
already write; the only document it owns is one `notification_reads/<key>` per
person, holding ids and times, never a title. What is held here:

* each source, alone, against an in-memory Firestore: admin requests, approval
  records and planned runs, hand-offs, agent questions, the GitHub connection
  and expired tokens, the person's workspace, finished tasks, workflows and
  merged pull requests;
* admin items reach admins only, and `admin_pending` is 0 for everyone else;
* another tenant's work and another person's work never appear (invariant 9);
* read and read-all persist, and survive re-derivation because ids are stable;
* a `needs_you` item drops out once its underlying record is resolved;
* one source failing serves the others with `partial: true`;
* the order: needs-you oldest first, then updates newest first, at most 50,
  from the last 7 days.

The people (conftest): `root` is an admin (and in eng), `alice` is in eng,
`bob` is in research, `carol` has only her personal tenant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import pytest

from swarm_api import approvals, notifications as notif
from swarm_api import workspaces as ws
from swarm_api.forgeapp import CONNECTIONS, connection_id_for
from swarm_api.gittokens import COLLECTION as GIT_TOKENS
from swarm_api.issueruns import IssueRun, RunState, plan_digest
from swarm_api.store import Store
from swarm_api.validation import parse_issue_ref

from .conftest import auth_header, seed_task, seed_tenant

ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
ROOT = "root@example.com"
PLAN = {"summary": "make the thing", "steps": [{"id": "impl-a", "prompt": "do it"}]}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(**delta: float) -> datetime:
    return _now() - timedelta(**delta)


def _viewer(email: str = ALICE, tenant_id: str = "eng", *, admin: bool = False,
            owner: bool = False) -> notif.Viewer:
    return notif.Viewer(email=email, tenant_id=tenant_id, is_admin=admin, is_owner=owner)


def _reading(db, viewer: notif.Viewer) -> notif.Reading:
    return notif.Reading(db=db, store=Store(db), viewer=viewer, now=_now())


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [i["id"] for i in items]


@pytest.fixture(autouse=True)
def _tenants(db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------

def _workspace(db, email: str, *, state: str = ws.REQUESTED, workspace_id: str = "w-c0ffee",
               requested_at: datetime | None = None, **extra: Any) -> dict[str, Any]:
    tenant_id = ws.personal_tenant_id(email)
    doc = {
        "tenant_id": tenant_id, "workspace_id": workspace_id, "principal": email,
        "state": state, "request_id": f"req-{workspace_id}",
        "requested_at": requested_at or _ago(hours=1), "requested_via": "console",
        "decision": None, "history": [], "limits": dict(ws.DEFAULT_LIMITS), "run": None,
        "steps": {}, "failure": None, "ready_at": None, "migrated": False,
    }
    doc.update(extra)
    db.docs[f"{ws.WORKSPACES}/{tenant_id}"] = doc
    db.docs[f"{ws.WORKSPACE_IDS}/{workspace_id}"] = {"tenant_id": tenant_id}
    return doc


def _loan(db, email: str, *, state: str = ws.REQUESTED, workspace_id: str = "w-c0ffee") -> None:
    tenant_id = ws.personal_tenant_id(email)
    db.docs[f"{ws.LOAN_REQUESTS}/{tenant_id}"] = {
        "tenant_id": tenant_id, "workspace_id": workspace_id, "principal": email,
        "state": state, "request_id": f"loan-{workspace_id}", "requested_at": _ago(minutes=30),
        "requested_via": "console", "decision": None, "history": [],
    }


def _task(db, task_id: str, *, tenant_id: str = "eng", by: str = ALICE, state: str = "SUCCEEDED",
          ended: datetime | None = None, **extra: Any) -> dict[str, Any]:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state=state,
                    created_at=(ended or _now()) - timedelta(minutes=5))
    doc["submitted_by"] = by
    if state in ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"):
        doc["completed_at"] = ended or _ago(minutes=1)
        doc["updated_at"] = doc["completed_at"]
    doc.update(extra)
    return doc


def _approval(db, key: str, *, tenant_id: str = "eng", kind: str = approvals.RUN,
              approvers: Any = "members", state: str = approvals.PENDING) -> str:
    approval_id = approvals.approval_id(kind, key)
    db.docs[f"{approvals.COLLECTION}/{approval_id}"] = {
        "approval_id": approval_id, "tenant_id": tenant_id, "kind": kind,
        "subject": {"schedule_id": "sch_1", "firing_id": key}, "digest": "d",
        "summary": "nightly: issue-sweep waits to run", "approvers": approvers,
        "state": state, "requested_at": _ago(hours=2), "expires_at": _now() + timedelta(hours=20),
        "decided_by": None, "decided_at": None, "reason": None,
    }
    return approval_id


def _run(db, run_id: str, *, tenant_id: str = "eng", by: str = ALICE,
         state: RunState = RunState.PLANNED, **extra: Any) -> IssueRun:
    now = _ago(minutes=10)
    run = IssueRun(
        id=run_id, tenant_id=tenant_id, created_by=by, created_at=now, updated_at=now,
        state=state, issue=parse_issue_ref("example-org/swarm#12"), plan_approval="required",
        auto_merge=False, fix_rounds=3, planner_task_id="task_planner", plan=PLAN,
        plan_digest=plan_digest(PLAN), **extra,
    )
    db.docs[f"issue_runs/{run_id}"] = run.to_firestore()
    return run


# --------------------------------------------------------------------------
# each source, alone
# --------------------------------------------------------------------------

def test_admin_requests_list_pending_workspaces_and_loans_for_an_admin(db):
    _workspace(db, CAROL, workspace_id="w-c0ffee")
    _workspace(db, BOB, workspace_id="w-b0b000", state=ws.READY)
    _loan(db, CAROL)
    items = notif.admin_requests(_reading(db, _viewer(ROOT, admin=True)))
    assert {i["group"] for i in items} == {"admin"}
    assert all(i["needs_you"] for i in items)
    assert all(i["href"] == "/admin/people" for i in items)
    titles = " ".join(i["title"] for i in items)
    assert "workspace" in titles and "Claude account" in titles
    assert len(items) == 2  # bob's ready workspace waits on nobody


def test_admin_requests_are_empty_for_a_non_admin_without_a_read(db, monkeypatch):
    _workspace(db, CAROL)
    read: list[str] = []
    real = db.collection
    monkeypatch.setattr(db, "collection", lambda path: read.append(path) or real(path))
    assert notif.admin_requests(_reading(db, _viewer(ALICE))) == []
    assert read == []


def test_approvals_list_the_tenants_pending_records_and_planned_runs(db):
    mine = _approval(db, "fir_1")
    _approval(db, "fir_2", tenant_id="research")
    _approval(db, "fir_3", state=approvals.APPROVED)
    _run(db, "run_a")
    _run(db, "run_b", tenant_id="research")
    _run(db, "run_c", state=RunState.RUNNING)
    items = notif.approval_items(_reading(db, _viewer()))
    ids = _ids(items)
    assert f"approval:{mine}" in ids
    assert any(i.startswith("approval:run:run_a:") for i in ids)
    assert len(items) == 2
    run_item = next(i for i in items if "run_a" in i["id"])
    # Approvals lives under Automate (APPROVALS-MOVE, PR 2486); the console's
    # `/approvals/<id>` redirect is only for links already sent.
    assert run_item["href"] == "/automate/approvals/run%3Arun_a"
    assert next(i for i in items if i["id"] == f"approval:{mine}")["href"] \
        == f"/automate/approvals/{quote(mine, safe='')}"
    assert "example-org/swarm#12" in run_item["title"]


def test_an_approval_only_the_owner_or_named_people_decide_is_not_everyones(db):
    _approval(db, "fir_owner", approvers="owner_only")
    _approval(db, "fir_named", approvers=[BOB])
    assert notif.approval_items(_reading(db, _viewer(ALICE))) == []
    owner_items = notif.approval_items(_reading(db, _viewer(ROOT, owner=True, admin=True)))
    assert len(owner_items) == 1


def test_a_firing_awaiting_approval_without_a_record_has_the_records_id(db):
    db.docs[f"{approvals.schedulefire.FIRINGS}/fir_9"] = {
        "firing_id": "fir_9", "schedule_id": "sch_1", "tenant_id": "eng", "type": "issue-sweep",
        "state": "awaiting_approval", "params_digest": "d", "approval_id": None, "fired_at": _ago(hours=1),
    }
    db.docs["schedules/sch_1"] = {"schedule_id": "sch_1", "tenant_id": "eng", "name": "nightly",
                                  "type": "issue-sweep", "gate": {"approvers": "members"}}
    before = dict(db.docs)
    items = notif.approval_items(_reading(db, _viewer()))
    assert _ids(items) == [f"approval:{approvals.approval_id(approvals.RUN, 'fir_9')}"]
    assert items[0]["href"] == "/automate/approvals"
    assert db.docs == before  # derived: nothing is written by reading


def test_handoffs_are_the_callers_own_waiting_tasks_with_the_browser_tab(db):
    _task(db, "task_live", state="RUNNING", human_wait={
        "reason": "log in to the bank", "requested_at": _ago(minutes=2), "generation": 3})
    _task(db, "task_parked", state="PARKED", park_reason="HUMAN_REQUIRED", current_generation=2)
    _task(db, "task_bobs", by=BOB, state="PARKED", park_reason="HUMAN_REQUIRED")
    _task(db, "task_other_park", state="PARKED", park_reason="CI_PENDING")
    items = notif.handoffs(_reading(db, _viewer()))
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {"task:handoff:task_live:3", "task:handoff:task_parked:2"}
    assert by_id["task:handoff:task_live:3"]["href"] == "/agents/live/task_live/browser"
    assert by_id["task:handoff:task_parked:2"]["href"].endswith("/task_parked/browser")
    assert all(i["needs_you"] and i["group"] == "task" for i in items)


def _held_merge(db, named: list[tuple[str, int]], *, unreadable: int = 0,
                task_id: str = "task_merge") -> None:
    """A workflow's merge step PARKED(HUMAN_REQUIRED) on the owner questions
    its review named, as the worker records the hold in `blocked_by`
    (`agent_worker.lifecycle._park_for_owner_questions`, #2070)."""
    questions = [{"task_id": t, "step_id": t.removeprefix("task_"), "question_index": i,
                  "question": f"question {i} of {t}?"} for t, i in named]
    _task(db, task_id, state="PARKED", park_reason="HUMAN_REQUIRED", current_generation=1,
          workflow_id="wf_lane", step_id="merge", runner_profile="merge",
          metadata={"unit": "GA-MOVE-VIEWS"},
          blocked_by=[{"reason": "HUMAN_REQUIRED", "park_phase": "owner_questions",
                       "owner_questions": {"code": "owner_questions_open",
                                           "questions": questions, "unreadable": unreadable}}])


def _bell(db) -> list[dict[str, Any]]:
    reading = _reading(db, _viewer())
    return notif.handoffs(reading) + notif.agent_questions(reading)


def test_a_merge_held_by_questions_that_already_ring_adds_no_hand_off_item(db):
    """MERGE-PARK-BELL (part of #2313): each of the two questions holding the
    merge rings from the step that asked it, so the merge adds nothing -- no
    "take over its browser" for a step that has no browser, and no second
    item for either question."""
    _task(db, "task_implement", workflow_id="wf_lane", step_id="implement",
          result_summary={"questions": 1})
    _task(db, "task_fix", workflow_id="wf_lane", step_id="fix", result_summary={"questions": 1})
    _held_merge(db, [("task_implement", 0), ("task_fix", 0)])
    items = _bell(db)
    assert sorted(_ids(items)) == ["task:questions:task_fix", "task:questions:task_implement"]
    assert notif.handoffs(_reading(db, _viewer())) == []


def test_a_merge_held_by_questions_that_do_not_ring_says_so_and_opens_details(db):
    """An asking step that ended past `WINDOW`, and one that reported no question
    count (`result_summary.questions`), ring nothing themselves: the merge's own item
    names them, and opens the merge's Details, where the questions are."""
    _task(db, "task_implement", workflow_id="wf_lane", step_id="implement",
          result_summary={"questions": 1}, ended=_ago(days=8))
    _task(db, "task_fix", workflow_id="wf_lane", step_id="fix", result_summary={"questions": 0})
    _held_merge(db, [("task_implement", 0), ("task_fix", 0)])
    items = _bell(db)
    assert _ids(items) == ["task:handoff:task_merge:1"]
    (item,) = items
    assert item["title"] == "GA-MOVE-VIEWS needs your answers to 2 questions to merge"
    assert item["href"] == "/agents/waiting/task_merge/details"
    assert "browser" not in item["href"] and item["needs_you"] is True


def test_a_merge_counts_only_the_questions_no_other_item_carries(db):
    """One question rings from its step and one does not: one item each,
    never two for the same question and never none for an open one."""
    _task(db, "task_implement", workflow_id="wf_lane", step_id="implement",
          result_summary={"questions": 1})
    _task(db, "task_fix", workflow_id="wf_lane", step_id="fix",
          result_summary={"questions": 1}, ended=_ago(days=8))
    _held_merge(db, [("task_implement", 0), ("task_fix", 0)])
    by_id = {i["id"]: i for i in _bell(db)}
    assert set(by_id) == {"task:questions:task_implement", "task:handoff:task_merge:1"}
    assert by_id["task:handoff:task_merge:1"]["title"] == (
        "GA-MOVE-VIEWS needs your answers to 1 question to merge")


def test_a_merge_whose_hold_cannot_be_read_whole_is_always_an_item(db):
    """No answer wakes a hold with an entry it cannot read (fail closed,
    `agent_questions.owner_questions_hold`): only a person's re-run does, so
    the bell says so even when the readable questions ring elsewhere."""
    _task(db, "task_implement", workflow_id="wf_lane", step_id="implement",
          result_summary={"questions": 1})
    _held_merge(db, [("task_implement", 0)], unreadable=1)
    (item,) = notif.handoffs(_reading(db, _viewer()))
    assert item["title"] == "GA-MOVE-VIEWS needs your answers to 1 question to merge"
    assert item["href"] == "/agents/waiting/task_merge/details"


def test_a_real_hand_off_park_keeps_its_browser_item(db):
    _task(db, "task_parked", state="PARKED", park_reason="HUMAN_REQUIRED", current_generation=2,
          blocked_by=[{"reason": "HUMAN_REQUIRED", "cause": "sign in to GitHub"}])
    (item,) = notif.handoffs(_reading(db, _viewer()))
    assert item["title"] == "Your agent parked until you take over its browser"
    assert item["href"] == "/agents/waiting/task_parked/browser"


def _waiting(db, task_id: str, *, by: str = ALICE, tenant_id: str = "eng",
             request_id: str = "signin-1", opened: bool | None = None) -> None:
    """A RUNNING task whose agent asked for a person, with its live session
    (`live_sessions/<session_id_for(attempt)>`) when `opened` says how."""
    from swarm_api.live import LIVE_SESSIONS, session_id_for

    attempt = f"att_{task_id}"
    _task(db, task_id, by=by, tenant_id=tenant_id, state="RUNNING", current_generation=1,
          human_wait={"reason": "Sign in to GitHub", "requested_at": _ago(seconds=5),
                      "deadline": _ago(seconds=-55), "controller": None,
                      "request_id": request_id, "attempt_id": attempt, "generation": 1})
    if opened is not None:
        db.docs[f"{LIVE_SESSIONS}/{session_id_for(attempt)}"] = {
            "id": session_id_for(attempt), "tenant_id": tenant_id, "task_id": task_id,
            "attempt_id": attempt, "generation": 1, "state": "open",
            "open_to_members": opened}


def test_a_hand_off_is_agent_needs_you_and_each_request_is_a_new_item(db):
    """HANDOFF-POLICY (part of #1030): the person is told when a hand-off
    opens, and a second request in the same attempt is unread again."""
    _waiting(db, "task_h", request_id="signin-1")
    (item,) = notif.handoffs(_reading(db, _viewer()))
    assert item["title"] == "Agent needs you"
    assert item["detail"] == "Sign in to GitHub"
    assert item["href"] == "/agents/live/task_h/browser"
    assert item["id"] == "task:handoff:task_h:1:signin-1"
    db.docs["tasks/task_h"]["human_wait"]["request_id"] = "signin-2"
    (again,) = notif.handoffs(_reading(db, _viewer()))
    assert again["id"] != item["id"]


def test_let_any_member_take_control_tells_every_member_and_off_tells_the_submitter_only(db):
    _waiting(db, "task_h", opened=False)
    assert _ids(notif.handoffs(_reading(db, _viewer(ROOT)))) == []
    assert len(notif.handoffs(_reading(db, _viewer()))) == 1
    _waiting(db, "task_h", opened=True)
    (item,) = notif.handoffs(_reading(db, _viewer(ROOT)))
    assert item["title"] == "Agent needs you" and item["needs_you"] is True
    # Invariant 9: never another tenant's member, opened or not.
    assert notif.handoffs(_reading(db, _viewer(BOB, "research"))) == []


def test_a_member_scoped_reader_is_told_of_their_own_hand_offs_only(db):
    """A personal workspace's member reads only their own tasks
    (`AuthContext.member_scope`); the bell never reaches past that."""
    _waiting(db, "task_h", opened=True)
    scoped = notif.Viewer(email=ROOT, tenant_id="eng", member_scope=True)
    assert notif.handoffs(_reading(db, scoped)) == []


def test_an_agents_questions_are_a_needs_you_item(db):
    _task(db, "task_q", result_summary={"questions": 2})
    _task(db, "task_none", result_summary={"questions": 0})
    items = notif.agent_questions(_reading(db, _viewer()))
    assert _ids(items) == ["task:questions:task_q"]
    assert items[0]["needs_you"] is True
    assert items[0]["href"] == "/agents/recent/task_q#questions"


def test_a_questions_item_names_the_step_not_only_the_task_id(db):
    """QA14-UI (owner QA 2026-10-13): the item said "Task <id>" and nothing
    about which agent asked. It leads with the step's name, or the runner
    profile when the task is no workflow step."""
    _task(db, "task_step", result_summary={"questions": 1}, step_id="fix-heartbeat")
    _task(db, "task_lone", result_summary={"questions": 1}, step_id=None)
    by_id = {i["id"]: i for i in notif.agent_questions(_reading(db, _viewer()))}
    assert by_id["task:questions:task_step"]["detail"] == "fix-heartbeat · Task task_step"
    assert by_id["task:questions:task_lone"]["detail"] == "mock · Task task_lone"


def test_a_task_whose_every_question_is_answered_is_no_longer_listed(db):
    """TASK-ANSWERS (part of #1206): an answer is stored on the task as
    `metadata.owner_answers`; the bell asks only while a question is open."""
    from swarm_api.validation import OWNER_ANSWERS_METADATA_KEY as KEY

    def answer(index: int) -> dict[str, Any]:
        return {"question_index": index, "question": "q", "choice": "a", "text": None,
                "by": ALICE, "at": _now().isoformat(), "history": []}

    _task(db, "task_all", result_summary={"questions": 2},
          metadata={KEY: [answer(0), answer(1)]})
    _task(db, "task_half", result_summary={"questions": 2}, metadata={KEY: [answer(1)]})
    # An answer to an index beyond the count does not close a real question.
    _task(db, "task_odd", result_summary={"questions": 1}, metadata={KEY: [answer(3)]})
    items = notif.agent_questions(_reading(db, _viewer()))
    assert sorted(_ids(items)) == ["task:questions:task_half", "task:questions:task_odd"]


def test_github_needing_reconnect_and_an_expired_token_are_account_items(db):
    conn = connection_id_for("eng", ALICE)
    db.docs[f"{CONNECTIONS}/{conn}"] = {
        "connection_id": conn, "tenant_id": "eng", "user": ALICE, "state": "refresh_failed",
        "failed_at": _ago(hours=1), "failure": {"code": "REFRESH_FAILED", "recovery": "Reconnect."}}
    db.docs[f"{GIT_TOKENS}/tok_aaaaaaaaaaaaaaaa"] = {
        "token_id": "tok_aaaaaaaaaaaaaaaa", "tenant_id": "eng", "scope": "user", "user": ALICE,
        "kind": "fine_grained_pat", "state": "expired", "expires_at": _ago(days=1),
        "registered_at": _ago(days=90)}
    db.docs[f"{GIT_TOKENS}/tok_bbbbbbbbbbbbbbbb"] = {
        "token_id": "tok_bbbbbbbbbbbbbbbb", "tenant_id": "eng", "scope": "user", "user": BOB,
        "kind": "fine_grained_pat", "state": "expired", "expires_at": _ago(days=1),
        "registered_at": _ago(days=90)}
    items = notif.github_items(_reading(db, _viewer()))
    assert len(items) == 2
    assert {i["group"] for i in items} == {"account"}
    assert all(i["needs_you"] and i["href"] == "/access" for i in items)
    # bob's connection is his; carol has none.
    assert notif.github_items(_reading(db, _viewer(CAROL, "u-carol"))) == []


def test_the_workspace_becoming_ready_or_denied_is_an_account_item(db):
    _workspace(db, CAROL, state=ws.READY, ready_at=_ago(hours=3))
    items = notif.workspace_items(_reading(db, _viewer(CAROL, "u-carol")))
    assert [i["group"] for i in items] == ["account"]
    assert "ready" in items[0]["title"].lower()
    _workspace(db, CAROL, state=ws.DENIED, decision={"verdict": "denied", "at": _ago(hours=1),
                                                      "by": ROOT, "reason": "use the team's"})
    items = notif.workspace_items(_reading(db, _viewer(CAROL, "u-carol")))
    assert "denied" in items[0]["title"].lower()
    assert items[0]["detail"] == "use the team's"
    assert ROOT not in str(items)  # decision.by never leaves Firestore


def test_a_declined_loan_request_is_an_account_item_for_the_requester(db):
    # LOAN-DECLINE: the person is told, with the reason, never who declined.
    _loan(db, CAROL, state="declined")
    db.docs[f"{ws.LOAN_REQUESTS}/u-carol"]["decision"] = {
        "verdict": "declined", "by": ROOT, "at": _ago(hours=1), "reason": "use the team's"}
    [item] = notif.loan_items(_reading(db, _viewer(CAROL, "u-carol")))
    assert item["group"] == "account" and item["needs_you"] is False
    assert item["title"] == "Your request to borrow a Claude account was declined"
    assert item["detail"] == "use the team's"
    assert item["id"] == "account:loan:w-c0ffee:declined:loan-w-c0ffee"
    assert item["href"] == "/setup#claude-account"
    assert ROOT not in str(item)
    # Only the requester sees it, and only within the window.
    assert notif.loan_items(_reading(db, _viewer(ALICE))) == []
    db.docs[f"{ws.LOAN_REQUESTS}/u-carol"]["decision"]["at"] = _ago(days=8)
    assert notif.loan_items(_reading(db, _viewer(CAROL, "u-carol"))) == []


def test_an_open_or_lent_loan_request_is_no_account_item(db):
    _loan(db, CAROL)
    assert notif.loan_items(_reading(db, _viewer(CAROL, "u-carol"))) == []
    _loan(db, CAROL, state="lent")
    assert notif.loan_items(_reading(db, _viewer(CAROL, "u-carol"))) == []


def test_declining_a_loan_request_replaces_the_admin_item_with_the_requesters(client, db):
    _workspace(db, CAROL, state=ws.READY, ready_at=_ago(days=9))
    client.post("/v1/workspace/loan-request", headers=auth_header("carol"))
    root = _feed(client, "root")
    assert [i["title"] for i in root["items"] if i["group"] == "admin"] == [
        "carol@example.com asks for a Claude account"]
    response = client.post("/v1/admin/people/w-c0ffee/loan/decline",
                           headers=auth_header("root"), json={"reason": "not this week"})
    assert response.status_code == 200, response.text
    root = _feed(client, "root")
    assert [i for i in root["items"] if i["group"] == "admin"] == []
    assert root["admin_pending"] == 0
    carol = _feed(client, "carol")
    [item] = [i for i in carol["items"] if i["group"] == "account"]
    assert item["title"] == "Your request to borrow a Claude account was declined"
    assert item["detail"] == "not this week"
    assert carol["unread_updates"] >= 1


def test_finished_lists_own_tasks_workflows_and_merged_prs_from_the_last_7_days(db):
    _task(db, "task_ok")
    _task(db, "task_bad", state="FAILED")
    _task(db, "task_old", ended=_ago(days=8))
    _task(db, "task_step", workflow_id="wf_1")  # its workflow says it
    _task(db, "task_running", state="RUNNING")
    db.docs["workflows/wf_1"] = {
        "workflow_id": "wf_1", "tenant_id": "eng", "created_at": _ago(hours=1),
        "updated_at": _ago(minutes=3), "state": "SUCCEEDED", "submitted_by": ALICE, "steps": [],
        "on_step_failure": "fail_workflow", "priority": 0, "cancel_requested": False}
    _run(db, "run_merged", state=RunState.DONE,
         pull_request={"number": 77, "url": "u", "merged": True, "merged_at": _ago(hours=2).isoformat()})
    items = notif.finished_items(_reading(db, _viewer()))
    ids = set(_ids(items))
    assert ids == {"finished:task:task_ok:SUCCEEDED", "finished:task:task_bad:FAILED",
                   "finished:workflow:wf_1:SUCCEEDED", "finished:pr:run_merged:77"}
    assert not any(i["needs_you"] for i in items)


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

def _feed(client, user: str = "alice", **headers: str) -> dict[str, Any]:
    response = client.get("/v1/notifications", headers={**auth_header(user), **headers})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_feed_has_the_shared_shape(client, db):
    _task(db, "task_ok")
    body = _feed(client)
    assert set(body) >= {"items", "unread_needs_you", "unread_updates", "admin_pending",
                         "read_at", "partial"}
    item = body["items"][0]
    assert set(item) == {"id", "group", "topic", "needs_you", "title", "detail", "href", "at",
                         "read"}
    assert item["group"] in notif.GROUPS
    datetime.fromisoformat(item["at"])
    assert body["unread_updates"] == 1 and body["unread_needs_you"] == 0
    assert body["partial"] is False


def test_admin_items_are_for_admins_only(client, db):
    _workspace(db, CAROL)
    _loan(db, CAROL)
    alice = _feed(client, "alice")
    assert [i for i in alice["items"] if i["group"] == "admin"] == []
    assert alice["admin_pending"] == 0
    root = _feed(client, "root")
    assert len([i for i in root["items"] if i["group"] == "admin"]) == 2
    assert root["admin_pending"] == 2
    assert root["unread_needs_you"] >= 2


def test_another_tenants_and_another_persons_work_never_appears(client, db):
    _task(db, "task_research_secret", tenant_id="research", by=BOB)
    _task(db, "task_bob_in_eng", by=BOB)
    _run(db, "run_research", tenant_id="research", by=BOB)
    _approval(db, "fir_research", tenant_id="research")
    _run(db, "run_bobs_merged", by=BOB, state=RunState.DONE,
         pull_request={"number": 5, "url": "u", "merged": True, "merged_at": _ago(hours=1).isoformat()})
    body = _feed(client, "alice")
    text = str(body)
    assert "task_research_secret" not in text
    assert "run_research" not in text
    assert "task_bob_in_eng" not in text  # finished work is the submitter's own
    assert "run_bobs_merged" not in text
    assert body["items"] == []


def test_read_marks_persist_and_survive_re_derivation(client, db):
    _task(db, "task_ok")
    _task(db, "task_q", result_summary={"questions": 1})
    first = _feed(client)
    assert first["unread_needs_you"] == 1 and first["unread_updates"] == 2
    ids = [i["id"] for i in first["items"]]
    assert ids == [i["id"] for i in _feed(client)["items"]]  # stable

    response = client.post("/v1/notifications/read", json={"ids": ["task:questions:task_q"]},
                           headers=auth_header("alice"))
    assert response.status_code == 200 and response.json() == {"ok": True}
    after = _feed(client)
    assert after["unread_needs_you"] == 0
    assert next(i for i in after["items"] if i["id"] == "task:questions:task_q")["read"] is True
    # Only ids and times are stored, never a title.
    stored = [d for p, d in db.docs.items() if p.startswith(notif.COLLECTION + "/")]
    assert len(stored) == 1
    assert "asked" not in str(stored) and "task_ok" not in str(stored)

    assert client.post("/v1/notifications/read-all", headers=auth_header("alice")).json() == {"ok": True}
    done = _feed(client)
    assert done["unread_needs_you"] == 0 and done["unread_updates"] == 0
    assert all(i["read"] for i in done["items"])
    assert done["read_at"] is not None
    # Someone else's marks are theirs.
    assert _feed(client, "root")["read_at"] is None


def test_a_resolved_request_drops_out_of_the_feed(client, db):
    record = _workspace(db, CAROL)
    assert any(i["group"] == "admin" for i in _feed(client, "root")["items"])
    record["state"] = ws.APPROVED
    record["decision"] = {"verdict": "approved", "at": _now(), "by": ROOT}
    assert not any(i["group"] == "admin" for i in _feed(client, "root")["items"])


def test_the_order_is_needs_you_oldest_first_then_updates_newest_first(client, db):
    _task(db, "task_q_new", result_summary={"questions": 1}, ended=_ago(minutes=1))
    _task(db, "task_q_old", result_summary={"questions": 1}, ended=_ago(hours=5))
    _task(db, "task_upd_old", ended=_ago(hours=4))
    _task(db, "task_upd_new", ended=_ago(minutes=2))
    ids = [i["id"] for i in _feed(client)["items"]]
    assert ids[:2] == ["task:questions:task_q_old", "task:questions:task_q_new"]
    updates = [i for i in ids if i.startswith("finished:")]
    # The tasks that asked also finished: every update, newest first.
    assert updates == ["finished:task:task_q_new:SUCCEEDED", "finished:task:task_upd_new:SUCCEEDED",
                       "finished:task:task_upd_old:SUCCEEDED", "finished:task:task_q_old:SUCCEEDED"]


def test_at_most_50_updates(client, db):
    for n in range(60):
        _task(db, f"task_{n:03d}", ended=_ago(minutes=n + 1))
    body = _feed(client)
    assert len([i for i in body["items"] if not i["needs_you"]]) == notif.MAX_UPDATES == 50


def test_one_source_failing_serves_the_others_as_partial(db, caplog):
    _task(db, "task_ok")

    def broken(reading):
        raise RuntimeError("firestore said no")

    service = notif.Notifications(db, Store(db), now=_now,
                                  sources=(("broken", broken), ("finished", notif.finished_items)))
    body = service.feed(_viewer())
    assert body["partial"] is True
    assert [i["id"] for i in body["items"]] == ["finished:task:task_ok:SUCCEEDED"]
    assert "broken" in caplog.text


def test_read_refuses_a_bad_body(client):
    for body in ({"ids": []}, {"ids": ["x" * 500]}, {"ids": ["a"] * 501}, {"nope": 1}):
        response = client.post("/v1/notifications/read", json=body, headers=auth_header("alice"))
        assert response.status_code == 422, body


def test_the_feed_needs_a_signed_in_person(client):
    assert client.get("/v1/notifications").status_code == 401
    assert client.post("/v1/notifications/read-all").status_code == 401


def test_read_marks_older_than_30_days_are_pruned(db):
    service = notif.Notifications(db, Store(db), now=_now)
    viewer = _viewer()
    service.mark_read(viewer, ["old-one"])
    doc_path = next(p for p in db.docs if p.startswith(notif.COLLECTION + "/"))
    db.docs[doc_path]["marks"][0]["at"] = _ago(days=31)
    service.mark_read(viewer, ["new-one"])
    assert [m["id"] for m in db.docs[doc_path]["marks"]] == ["new-one"]
    assert ALICE not in doc_path  # the key is a hash, not the address


# --------------------------------------------------------------------------
# the Admin panel's two platform counts (CONFIG-ADMIN-POLISH; part of #2157, S1b)
# --------------------------------------------------------------------------

class _Meta:
    """A metadata reader: each secret's enabled-version times, or the error it raises."""

    def __init__(self, versions: dict[str, Any]) -> None:
        self.versions = versions
        self.calls = 0

    def enabled_versions(self, secret_id: str) -> list[datetime | None]:
        from swarm_api import platform_secrets as ps

        self.calls += 1
        answer = self.versions.get(secret_id, [_now()])
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise ps.NotDeclared(secret_id)
        return answer


def _catalogue(*ids: str):
    from swarm_api import platform_secrets as ps

    return tuple(ps.PlatformSecret(i, ps.TERRAFORM, "scripts/create-secrets.sh") for i in ids)


@pytest.fixture
def catalogue(monkeypatch):
    monkeypatch.setattr(notif, "PLATFORM_SECRET_CATALOGUE",
                        _catalogue("swarm-a-key", "swarm-b-key", "swarm-c-key"))


def test_tenants_waiting_counts_the_workspaces_waiting_to_be_created_for_an_admin(db):
    _workspace(db, CAROL, workspace_id="w-c0ffee")
    _workspace(db, ALICE, workspace_id="w-a11ce0")
    _workspace(db, BOB, workspace_id="w-b0b000", state=ws.READY)
    _loan(db, CAROL)  # a Claude-account request is People and roles', not Tenants'
    service = notif.Notifications(db, Store(db), now=_now)
    root = service.feed(_viewer(ROOT, admin=True))
    # MUTATION: count admin_pending (workspaces and loans) -> 3 and this fails.
    assert root["tenants_waiting"] == 2
    assert root["admin_pending"] == 3
    # Nothing waiting is a measured 0, not "not read".
    db.docs = {k: v for k, v in db.docs.items() if not k.startswith(ws.WORKSPACES + "/")}
    assert service.feed(_viewer(ROOT, admin=True))["tenants_waiting"] == 0


def test_tenants_waiting_is_not_read_when_the_admin_source_failed_or_for_a_non_admin(db):
    _workspace(db, CAROL)

    def broken(reading):
        raise RuntimeError("firestore said no")

    service = notif.Notifications(db, Store(db), now=_now, sources=(("admin", broken),))
    # MUTATION: count the items that did arrive -> 0 and this fails.
    assert service.feed(_viewer(ROOT, admin=True))["tenants_waiting"] is None
    plain = notif.Notifications(db, Store(db), now=_now)
    assert plain.feed(_viewer())["tenants_waiting"] is None


def test_platform_secrets_unset_counts_the_catalogue_rows_with_no_enabled_version(db, catalogue):
    meta = _Meta({"swarm-a-key": [], "swarm-b-key": None})
    service = notif.Notifications(db, Store(db), now=_now, secret_metadata=meta)
    # MUTATION: count only `not_set`, or count `set` rows -> fails.
    assert service.feed(_viewer(ROOT, admin=True))["platform_secrets_unset"] == 2
    # A non-admin is never told, and nothing is read for them.
    calls = meta.calls
    assert service.feed(_viewer())["platform_secrets_unset"] is None
    assert meta.calls == calls


def test_platform_secrets_unset_is_not_read_when_a_row_could_not_be_read(db, catalogue):
    from swarm_api import platform_secrets as ps

    meta = _Meta({"swarm-a-key": [], "swarm-c-key": ps.MetadataUnreadable(ps.PERMISSION_DENIED)})
    service = notif.Notifications(db, Store(db), now=_now, secret_metadata=meta)
    # MUTATION: count the rows that were read -> 1 and this fails.
    assert service.feed(_viewer(ROOT, admin=True))["platform_secrets_unset"] is None
    # No reader at all (no project to read) is not read either, never 0.
    bare = notif.Notifications(db, Store(db), now=_now)
    assert bare.feed(_viewer(ROOT, admin=True))["platform_secrets_unset"] is None


def test_platform_secrets_unset_is_read_once_per_period_not_per_feed(db, catalogue):
    meta = _Meta({"swarm-a-key": []})
    clock = [_now()]
    service = notif.Notifications(db, Store(db), now=lambda: clock[0], secret_metadata=meta)
    assert service.feed(_viewer(ROOT, admin=True))["platform_secrets_unset"] == 1
    calls = meta.calls
    meta.versions["swarm-b-key"] = []
    # MUTATION: read Secret Manager on every feed -> calls grow and this fails.
    assert service.feed(_viewer(ROOT, admin=True))["platform_secrets_unset"] == 1
    assert meta.calls == calls
    clock[0] += notif.PLATFORM_SECRETS_TTL + timedelta(seconds=1)
    # Past the period the last count is served while one background read
    # refreshes it (LOAD-SPEED-NOTIFICATIONS); the next feed has the new one.
    assert service.feed(_viewer(ROOT, admin=True))["platform_secrets_unset"] == 1
    assert notif.wait_platform_secrets_refresh(meta, timeout=10)
    assert service.feed(_viewer(ROOT, admin=True))["platform_secrets_unset"] == 2


def test_the_route_serves_both_counts(client, db):
    _workspace(db, CAROL)
    root = _feed(client, "root")
    assert root["tenants_waiting"] == 1
    assert "platform_secrets_unset" in root
    alice = _feed(client, "alice")
    assert alice["tenants_waiting"] is None and alice["platform_secrets_unset"] is None


def test_the_route_hands_the_service_the_apps_own_secret_metadata_reader(client, catalogue):
    # CONFIG-ADMIN-POLISH-2 (part of #2157): one reader, the one Admin ›
    # Platform secrets reads (`routes.admin.secret_metadata`, kept on
    # `app.state`), not a second one built from the Firestore client's project.
    meta = _Meta({"swarm-a-key": [], "swarm-b-key": None})
    client.app.state.secret_metadata = meta
    # MUTATION: build `Notifications` without `secret_metadata=` -> None, fails.
    assert _feed(client, "root")["platform_secrets_unset"] == 2
    assert meta.calls
    # A member's feed reads nothing from it.
    calls = meta.calls
    assert _feed(client, "alice")["platform_secrets_unset"] is None
    assert meta.calls == calls
    # MUTATION: keep the project fallback -> a second reader, and this fails.
    assert not hasattr(notif, "_reader_for")
