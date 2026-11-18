"""One question, counted once, whatever its options (the owner's overnight grant, 6 November 2026).

Two follow-ups of the merged "count a question only while it holds work"
change. (1) The worker writes `result_summary.question_text_digests`, one key
per counted question of its normalized words alone; `codec.step_needs` serves
it beside `questions_unread` (and each open wait question's own `text_digest`),
and the bell counts an implement and a fix step asking the same words with
other option labels once. A task summarised before the key counts as before.
(2) `notifications.handoffs` leaves a PARKED HUMAN_REQUIRED task parked on a
question wait to its question item, as the console's `stepNeedItems` does: a
blocking question is answered, not "take over its browser".

MUTATIONS: key the text on its options; drop a task without the key; serve a
key list that does not match the count; ring a question wait as a hand-off;
drop a browser hand-off park.
"""

from __future__ import annotations

import hashlib
from typing import Any

from swarm_api import notifications as notif

from .conftest import seed_tenant
from .test_notifications import _ago, _reading, _task, _viewer, _workflow
from .test_notifications_holds_work import _parked, _wait
from .test_workflow_step_needs import _needs, _one_step


def _key(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).lower().encode("utf-8")).hexdigest()[:16]


def _summary(*texts: str, keyed: bool = True) -> dict[str, Any]:
    summary: dict[str, Any] = {"questions": len(texts),
                               "question_digests": [f"{i:016x}" for i in range(1, len(texts) + 1)]}
    if keyed:
        summary["question_text_digests"] = [_key(t) for t in texts]
    return summary


def _questions(db) -> list[str]:
    return [i["id"] for i in notif.agent_questions(_reading(db, _viewer()))]


# --------------------------------------------------------------------------
# step_needs serves the key
# --------------------------------------------------------------------------

def test_step_needs_serves_the_text_keys_beside_questions_unread(client, db):
    seed_tenant(db, "eng")
    _one_step(db, "wf_keyed", result_summary=_summary("Ship it?", "Which region?"))
    needs = _needs(client, "wf_keyed")
    assert needs["questions_unread"] == [0, 1]
    assert needs["question_text_digests"] == [_key("Ship it?"), _key("Which region?")]


def test_two_steps_asking_the_same_text_with_other_options_share_a_key(client, db):
    seed_tenant(db, "eng")
    _one_step(db, "wf_impl", result_summary=_summary("Which database  should it use?"))
    _one_step(db, "wf_fix", result_summary=_summary("which database should it use?"))
    assert _needs(client, "wf_impl")["question_text_digests"] == _needs(client, "wf_fix")["question_text_digests"]


def test_a_task_without_the_key_serves_none_and_counts_as_today(client, db):
    seed_tenant(db, "eng")
    _one_step(db, "wf_old", result_summary=_summary("Ship it?", "Which region?", keyed=False))
    _one_step(db, "wf_bad", result_summary={**_summary("Ship it?", "Which region?"),
                                            "question_text_digests": [_key("Ship it?"), "<b>not a key</b>"]})
    for workflow in ("wf_old", "wf_bad"):
        needs = _needs(client, workflow)
        assert needs["questions_unread"] == [0, 1]
        assert needs["question_text_digests"] is None


def test_an_open_wait_question_serves_its_text_key(client, db):
    seed_tenant(db, "eng")
    wait = {"attempt_id": "att_1", "blocking": [0],
            "questions": [{"question": "Which  REGION?", "options": [{"label": "eu"}]}]}
    _one_step(db, "wf_wait", state="PARKED", park_reason="HUMAN_REQUIRED",
              blocked_by=[{"reason": "HUMAN_REQUIRED", "question_wait": wait}])
    needs = _needs(client, "wf_wait")
    assert [q["text_digest"] for q in needs["questions"]] == [_key("which region?")]
    # Only an ended task's artifact questions are served by key.
    assert needs["question_text_digests"] is None


# --------------------------------------------------------------------------
# the bell counts it once
# --------------------------------------------------------------------------

def test_the_bell_rings_the_same_text_from_implement_and_fix_once(db):
    _workflow(db)
    _task(db, "task_implement", workflow_id="wf_open", step_id="implement",
          created_at=_ago(minutes=10), result_summary=_summary("Which database  should it use?"))
    _task(db, "task_fix", workflow_id="wf_open", step_id="fix",
          created_at=_ago(minutes=2), result_summary=_summary("which database should it use?"))
    assert _questions(db) == ["task:questions:task_implement"]


def test_the_bell_still_rings_a_step_with_one_new_question(db):
    _workflow(db)
    _task(db, "task_implement", workflow_id="wf_open", created_at=_ago(minutes=10),
          result_summary=_summary("Ship it?"))
    _task(db, "task_fix", workflow_id="wf_open", created_at=_ago(minutes=2),
          result_summary=_summary("ship it?", "Which region?"))
    assert sorted(_questions(db)) == ["task:questions:task_fix", "task:questions:task_implement"]


def test_an_ended_repeat_of_a_waiting_question_rings_once(db):
    _workflow(db)
    _parked(db, "task_implement", _wait("Ship  it?"), minutes=10)
    _task(db, "task_fix", workflow_id="wf_open", created_at=_ago(minutes=2),
          result_summary=_summary("ship it?"))
    assert _questions(db) == ["task:questions:task_implement"]


def test_tasks_without_the_key_ring_as_today(db):
    _workflow(db)
    _task(db, "task_implement", workflow_id="wf_open", created_at=_ago(minutes=10),
          result_summary=_summary("Ship it?", keyed=False))
    _task(db, "task_fix", workflow_id="wf_open", created_at=_ago(minutes=2),
          result_summary=_summary("Ship it?", keyed=False))
    assert sorted(_questions(db)) == ["task:questions:task_fix", "task:questions:task_implement"]


def test_the_same_text_in_two_workflows_rings_twice(db):
    _workflow(db, "wf_a")
    _workflow(db, "wf_b")
    _task(db, "task_a", workflow_id="wf_a", created_at=_ago(minutes=10), result_summary=_summary("Ship it?"))
    _task(db, "task_b", workflow_id="wf_b", created_at=_ago(minutes=2), result_summary=_summary("Ship it?"))
    assert sorted(_questions(db)) == ["task:questions:task_a", "task:questions:task_b"]


# --------------------------------------------------------------------------
# a question wait is no hand-off
# --------------------------------------------------------------------------

def test_a_question_wait_park_is_not_a_hand_off_item(db):
    _workflow(db)
    _parked(db, "task_asks", _wait("Which region?"))
    reading = _reading(db, _viewer())
    assert notif.handoffs(reading) == []
    assert [i["id"] for i in notif.agent_questions(reading)] == ["task:questions:task_asks"]


def test_a_browser_hand_off_park_still_is(db):
    _task(db, "task_browser", state="PARKED", park_reason="HUMAN_REQUIRED", current_generation=4,
          blocked_by=[{"reason": "HUMAN_REQUIRED", "park_phase": "handoff"}])
    (item,) = notif.handoffs(_reading(db, _viewer()))
    assert item["id"] == "task:handoff:task_browser:4"
    assert item["href"] == "/agents/waiting/task_browser/browser"

