"""`GET /v1/tasks/open-questions` serves a session's unanswered owner questions in one call.

THE DEFECT (#2835). The sc plugin's check found owner questions by paging the
session's SUCCEEDED tasks of the last 7 days, at most 10 pages of 50, so a
busy session's older tasks were never read and their questions never listed
(`unchecked: more than 10 pages ...`). This route asks the store for the
caller's tasks of the window whose agent asked questions
(`result_summary.questions > 0`) and serves the ones of this session with an
index the owner has not answered (`metadata.owner_answers`).

Offline: the real route over FakeFirestore.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .conftest import auth_header, seed_task, seed_tenant

MINE = "0f0e0d0c-aaaa-4bbb-8ccc-0000000000b1"
THEIRS = "0f0e0d0c-aaaa-4bbb-8ccc-0000000000b2"
ALICE = "alice@example.com"


def _task(db, task_id: str, *, ended: datetime, sid: str | None = MINE, asked: int = 0,
          answered: tuple[int, ...] = (), state: str = "SUCCEEDED", by: str = ALICE) -> None:
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state=state,
                    created_at=ended - timedelta(minutes=20))
    doc["submitted_by"] = by
    doc["completed_at"] = ended
    doc["metadata"] = {"claude_session": sid} if sid else {}
    if answered:
        doc["metadata"]["owner_answers"] = [
            {"question_index": i, "question": f"q{i}", "choice": "yes", "text": None,
             "by": ALICE, "at": ended.isoformat()}
            for i in answered
        ]
    doc["result_summary"] = {"questions": asked} if asked else {"exit_code": 0}
    db.docs[f"tasks/{task_id}"] = doc


def _busy_session(db) -> datetime:
    """600 succeeded tasks of this session in the last few hours -- twelve
    pages of 50, past the plugin's old 10-page walk -- and, six days ago,
    the one task whose agent asked two questions nobody answered."""
    seed_tenant(db, "eng")
    now = datetime.now(timezone.utc)
    for i in range(600):
        _task(db, f"task_busy_{i:03d}", ended=now - timedelta(seconds=10 * i))
    _task(db, "task_old_asked", ended=now - timedelta(days=6), asked=2)
    return now


def _open(client, **params) -> dict:
    response = client.get("/v1/tasks/open-questions", params={"claude_session": MINE, **params},
                          headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()


def test_an_old_tasks_open_question_is_served_behind_hundreds_of_newer_tasks(db, client):
    _busy_session(db)
    body = _open(client)
    # MUTATION: page the finished tasks newest first with a page ceiling, as
    # the plugin did, and the six-day-old task is never reached.
    assert [t["id"] for t in body["tasks"]] == ["task_old_asked"]
    assert body["complete"] is True
    assert body["filter"] == {"claude_session": MINE, "since": body["filter"]["since"],
                              "open_questions": True}
    row = body["tasks"][0]
    # The row is the one `GET /v1/tasks` serves: the plugin reads the count
    # and the answers from it, and its console link.
    assert row["result_summary"]["questions"] == 2
    assert row["owner_answers"] == []


def test_answered_questions_are_not_served_and_a_partial_answer_still_is(db, client):
    seed_tenant(db, "eng")
    now = datetime.now(timezone.utc)
    _task(db, "task_all_answered", ended=now - timedelta(hours=1), asked=2, answered=(0, 1))
    _task(db, "task_half_answered", ended=now - timedelta(hours=2), asked=2, answered=(1,))
    _task(db, "task_none_answered", ended=now - timedelta(hours=3), asked=1, state="FAILED")
    body = _open(client)
    # MUTATION: drop the answers filter and task_all_answered comes back.
    # Newest end first.
    assert [t["id"] for t in body["tasks"]] == ["task_half_answered", "task_none_answered"]


def test_only_the_callers_own_tasks_of_this_session_in_the_window_and_finished(db, client):
    seed_tenant(db, "eng")
    now = datetime.now(timezone.utc)
    _task(db, "task_mine", ended=now - timedelta(hours=1), asked=1)
    _task(db, "task_other_session", ended=now - timedelta(hours=1), asked=1, sid=THEIRS)
    _task(db, "task_untagged", ended=now - timedelta(hours=1), asked=1, sid=None)
    _task(db, "task_someone_else", ended=now - timedelta(hours=1), asked=1, by="root@example.com")
    _task(db, "task_too_old", ended=now - timedelta(days=8), asked=1)
    _task(db, "task_still_running", ended=now - timedelta(hours=1), asked=1, state="RUNNING")
    assert [t["id"] for t in _open(client)["tasks"]] == ["task_mine"]
    # `since` widens the window, up to 31 days.
    since = (now - timedelta(days=9)).isoformat()
    assert [t["id"] for t in _open(client, since=since)["tasks"]] == ["task_mine", "task_too_old"]


def test_a_bad_session_or_a_window_past_31_days_is_refused(db, client):
    seed_tenant(db, "eng")
    bad = client.get("/v1/tasks/open-questions", params={"claude_session": "x y"},
                     headers=auth_header("alice"))
    assert bad.status_code == 422, bad.text
    far = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    wide = client.get("/v1/tasks/open-questions", params={"claude_session": MINE, "since": far},
                      headers=auth_header("alice"))
    assert wide.status_code == 422, wide.text


def test_a_read_that_reaches_its_bound_says_it_is_not_complete(db, client, monkeypatch):
    from swarm_api import agent_questions

    seed_tenant(db, "eng")
    now = datetime.now(timezone.utc)
    for i in range(3):
        _task(db, f"task_asked_{i}", ended=now - timedelta(hours=i + 1), asked=1, answered=(0,))
    monkeypatch.setattr(agent_questions, "OPEN_QUESTIONS_SCAN", 3)
    # MUTATION: report `complete: true` whatever was read and a bound that
    # cut the read is a silent short list.
    assert _open(client)["complete"] is False


def test_the_query_has_its_composite_index():
    repo = Path(__file__).resolve().parents[3]
    text = (repo / "terraform/modules/firestore/indexes.tf").read_text()
    match = re.search(r'"tasks-tenant-submitter-completed-questions"\s*=\s*\{(.*?)\n    \}', text, re.S)
    assert match, "tasks-tenant-submitter-completed-questions is not declared in indexes.tf"
    block = match.group(1)
    assert 'collection  = "tasks"' in block
    assert 'query_scope = "COLLECTION"' in block
    fields = re.findall(r'field_path = "([^"]+)", order = "([A-Z]+)"', block)
    assert fields == [
        ("tenant_id", "ASCENDING"), ("submitted_by", "ASCENDING"),
        ("completed_at", "ASCENDING"), ("result_summary.questions", "ASCENDING"),
    ]
